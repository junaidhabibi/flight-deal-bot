"""Travelpayouts / Aviasales Data API client.

This is the bot's workhorse: free, no per-call cost, and generous limits
(600 requests/minute on the v3 endpoints). The data is Aviasales' cache of
fares its users actually found in the last 48 hours, which is exactly the
"what is cheap right now" signal we want for broad scanning.

Docs: https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API
Rate limits: https://support.travelpayouts.com/hc/en-us/articles/4402565416594
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

from ..models import Deal, Leg

log = logging.getLogger(__name__)

BASE = "https://api.travelpayouts.com"
AVIASALES_SEARCH_ROOT = "https://www.aviasales.com"

# Per-minute limits published by Travelpayouts, used to self-throttle.
RATE_LIMITS = {
    "/aviasales/v3/prices_for_dates": 600,
    "/aviasales/v3/grouped_prices": 600,
    "/aviasales/v3/get_special_offers": 600,
    "/v2/prices/month-matrix": 300,
}


class TravelpayoutsError(Exception):
    pass


class TravelpayoutsClient:
    def __init__(
        self,
        token: str,
        marker: Optional[str] = None,
        market: str = "us",
        currency: str = "USD",
        request_delay: float = 0.15,
        timeout: int = 25,
        session: Optional[requests.Session] = None,
    ):
        if not token:
            raise TravelpayoutsError("A Travelpayouts API token is required.")
        self.token = token
        self.marker = marker
        self.market = market
        self.currency = currency.lower()
        self.request_delay = request_delay
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "X-Access-Token": token,
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": "flight-deal-bot/1.0",
            }
        )
        self.call_count = 0
        self._last_call = 0.0

    # ---------- plumbing ----------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        self._last_call = time.monotonic()

    def _get(self, path: str, params: Dict[str, Any], retries: int = 3) -> Dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        params.setdefault("currency", self.currency)
        params.setdefault("market", self.market)

        for attempt in range(retries):
            self._throttle()
            try:
                resp = self.session.get(
                    f"{BASE}{path}", params=params, timeout=self.timeout
                )
                self.call_count += 1
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise TravelpayoutsError(f"{path}: network error: {e}") from e
                time.sleep(2**attempt)
                continue

            if resp.status_code == 429:
                # Rate limited. The docs say the block clears within the minute.
                wait = int(resp.headers.get("X-Rate-Limit-Reset", 5)) + 1
                log.warning("Rate limited on %s, sleeping %ss", path, wait)
                time.sleep(min(wait, 65))
                continue

            if resp.status_code >= 500:
                if attempt == retries - 1:
                    raise TravelpayoutsError(f"{path}: HTTP {resp.status_code}")
                time.sleep(2**attempt)
                continue

            if resp.status_code != 200:
                raise TravelpayoutsError(
                    f"{path}: HTTP {resp.status_code}: {resp.text[:300]}"
                )

            try:
                data = resp.json()
            except ValueError as e:
                raise TravelpayoutsError(f"{path}: invalid JSON: {e}") from e

            if not data.get("success", True):
                raise TravelpayoutsError(f"{path}: API error: {data.get('error')}")
            return data

        raise TravelpayoutsError(f"{path}: exhausted {retries} retries")

    # ---------- endpoints ----------

    def prices_for_dates(
        self,
        origin: str,
        destination: str,
        departure_at: Optional[str] = None,
        return_at: Optional[str] = None,
        one_way: bool = False,
        direct: bool = False,
        limit: int = 100,
        sorting: str = "price",
    ) -> List[Dict[str, Any]]:
        """Cheapest fares for a route, optionally for a whole month (YYYY-MM)."""
        data = self._get(
            "/aviasales/v3/prices_for_dates",
            {
                "origin": origin,
                "destination": destination,
                "departure_at": departure_at,
                "return_at": return_at,
                "one_way": str(one_way).lower(),
                "direct": str(direct).lower(),
                "sorting": sorting,
                "limit": min(limit, 1000),
                "page": 1,
            },
        )
        return data.get("data", []) or []

    def grouped_prices(
        self,
        origin: str,
        destination: str,
        departure_at: Optional[str] = None,
        group_by: str = "departure_at",
        direct: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        """Cheapest fare per departure date. Ideal for building stopovers."""
        data = self._get(
            "/aviasales/v3/grouped_prices",
            {
                "origin": origin,
                "destination": destination,
                "departure_at": departure_at,
                "group_by": group_by,
                "direct": str(direct).lower(),
            },
        )
        return data.get("data", {}) or {}

    def special_offers(
        self, origin: str, destination: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Aviasales' own 'abnormally low price' feed. Free signal worth reading."""
        data = self._get(
            "/aviasales/v3/get_special_offers",
            {"origin": origin, "destination": destination, "locale": "en"},
        )
        return data.get("data", []) or []

    # ---------- parsing ----------

    def booking_url(self, item: Dict[str, Any]) -> str:
        link = item.get("link") or ""
        if not link:
            return ""
        if link.startswith("http"):
            url = link
        else:
            url = f"{AVIASALES_SEARCH_ROOT}{link}"
        if self.marker:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}marker={self.marker}"
        return url

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        # API returns ISO8601, sometimes with a Z, sometimes with an offset.
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    def to_deal(
        self,
        item: Dict[str, Any],
        destination_city: str,
        max_extra_stops: int = 1,
    ) -> Optional[Deal]:
        """Turn one API record into a Deal, or None if it fails our filters."""
        try:
            price = float(item["price"])
        except (KeyError, TypeError, ValueError):
            return None
        if price <= 0:
            return None

        dep = self._parse_dt(item.get("departure_at"))
        if not dep:
            return None
        ret = self._parse_dt(item.get("return_at"))

        out_stops = int(item.get("transfers", 0) or 0)
        back_stops = int(item.get("return_transfers", 0) or 0)
        if out_stops > max_extra_stops or back_stops > max_extra_stops:
            return None

        origin = item.get("origin") or item.get("origin_airport") or ""
        destination = item.get("destination") or item.get("destination_airport") or ""
        if not origin or not destination:
            return None

        outbound_minutes = item.get("duration_to") or item.get("duration")

        leg = Leg(
            origin=origin,
            destination=destination,
            depart_at=dep,
            airline=item.get("airline", ""),
            flight_number=str(item.get("flight_number", "")),
            stops=out_stops,
            price_usd=price,
            duration_minutes=outbound_minutes,
        )

        return Deal(
            origin=origin,
            destination=destination,
            destination_city=destination_city,
            price_usd=price,
            depart_date=dep.date(),
            return_date=ret.date() if ret else None,
            legs=[leg],
            airline=item.get("airline", ""),
            booking_url=self.booking_url(item),
            source="travelpayouts",
        )

    # ---------- high-level scans ----------

    def scan_round_trips(
        self,
        origin: str,
        destination: str,
        destination_city: str,
        months: List[str],
        min_nights: int,
        max_nights: int,
        max_extra_stops: int = 1,
        limit_per_month: int = 100,
    ) -> List[Deal]:
        """Round-trip fares for a route across several months."""
        deals: List[Deal] = []
        for month in months:
            try:
                rows = self.prices_for_dates(
                    origin=origin,
                    destination=destination,
                    departure_at=month,
                    one_way=False,
                    direct=False,
                    limit=limit_per_month,
                )
            except TravelpayoutsError as e:
                log.warning("scan %s-%s %s failed: %s", origin, destination, month, e)
                continue

            for row in rows:
                deal = self.to_deal(row, destination_city, max_extra_stops)
                if not deal or not deal.return_date:
                    continue
                nights = (deal.return_date - deal.depart_date).days
                if nights < min_nights or nights > max_nights:
                    continue
                deals.append(deal)
        return deals

    def daily_one_way_prices(
        self,
        origin: str,
        destination: str,
        months: List[str],
        direct_only: bool = True,
    ) -> Dict[date, Dict[str, Any]]:
        """Cheapest one-way fare per calendar date, for stopover stitching.

        direct_only=True is the default because a stopover itinerary built out
        of nonstop hops is the only kind where we can be certain the gap in
        the middle is a real, deliberate stay rather than a tight connection.
        """
        by_date: Dict[date, Dict[str, Any]] = {}
        for month in months:
            try:
                grouped = self.grouped_prices(
                    origin=origin,
                    destination=destination,
                    departure_at=month,
                    group_by="departure_at",
                    direct=direct_only,
                )
            except TravelpayoutsError as e:
                log.warning(
                    "grouped %s-%s %s failed: %s", origin, destination, month, e
                )
                continue

            for day_key, row in grouped.items():
                dep = self._parse_dt(row.get("departure_at")) or self._parse_dt(day_key)
                if not dep:
                    continue
                d = dep.date()
                try:
                    price = float(row.get("price"))
                except (TypeError, ValueError):
                    continue
                existing = by_date.get(d)
                if existing is None or price < existing["price"]:
                    by_date[d] = {
                        "price": price,
                        "departure_at": dep,
                        "airline": row.get("airline", ""),
                        "flight_number": str(row.get("flight_number", "")),
                        "transfers": int(row.get("transfers", 0) or 0),
                        "duration": row.get("duration"),
                        "link": row.get("link", ""),
                        "origin": row.get("origin", origin),
                        "destination": row.get("destination", destination),
                        "raw": row,
                    }
        return by_date


def months_ahead(start_offset: int, count: int, today: Optional[date] = None) -> List[str]:
    """['2026-11', '2026-12', ...] for the scan window."""
    today = today or date.today()
    out: List[str] = []
    y, m = today.year, today.month
    for i in range(start_offset, start_offset + count):
        mm = m + i
        yy = y + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        out.append(f"{yy:04d}-{mm:02d}")
    return out
