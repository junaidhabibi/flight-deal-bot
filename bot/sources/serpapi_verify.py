"""SerpApi Google Flights client -- used as a verifier, not a scanner.

Why it exists: Travelpayouts serves a cache of fares users found recently,
which is great for breadth but can be stale or unbookable. Before the bot
wakes you up, it re-checks the handful of candidates that look like records
against Google Flights, which also hands back two things nothing else does:

  price_insights.typical_price_range  -> Google's own idea of a normal fare
  price_insights.price_history        -> timestamped price points
  price_insights.price_level          -> "low" / "typical" / "high"

Why it isn't the scanner: the free tier is 250 searches/month, so the bot
spends them only on fares that already passed every cheap filter.

Docs: https://serpapi.com/google-flights-api
      https://serpapi.com/google-flights-price-insights
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger(__name__)

ENDPOINT = "https://serpapi.com/search"


@dataclass
class PriceInsight:
    """Google's view of a route, used to sanity-check a candidate."""

    lowest_price: Optional[float] = None
    price_level: str = ""
    typical_low: Optional[float] = None
    typical_high: Optional[float] = None
    history_min: Optional[float] = None
    history_points: int = 0
    best_offer: Optional[float] = None
    booking_url: str = ""

    @property
    def has_typical(self) -> bool:
        return self.typical_low is not None and self.typical_high is not None

    def discount_vs_typical(self, price: float) -> Optional[float]:
        """Percent below the bottom of Google's typical range."""
        if self.typical_low is None or self.typical_low <= 0:
            return None
        return (self.typical_low - price) / self.typical_low * 100

    def beats_history(self, price: float, tolerance_pct: float = 2.0) -> Optional[bool]:
        if self.history_min is None:
            return None
        return price <= self.history_min * (1 + tolerance_pct / 100)


class SerpApiError(Exception):
    pass


class SerpApiVerifier:
    def __init__(
        self,
        api_key: str,
        currency: str = "USD",
        country: str = "us",
        timeout: int = 40,
        session: Optional[requests.Session] = None,
    ):
        if not api_key:
            raise SerpApiError("A SerpApi key is required.")
        self.api_key = api_key
        self.currency = currency
        self.country = country
        self.timeout = timeout
        self.session = session or requests.Session()
        self.call_count = 0

    def _search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params)
        params.update(
            {
                "engine": "google_flights",
                "api_key": self.api_key,
                "currency": self.currency,
                "hl": "en",
                "gl": self.country,
            }
        )
        try:
            resp = self.session.get(ENDPOINT, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise SerpApiError(f"network error: {e}") from e

        if resp.status_code == 401:
            raise SerpApiError("SerpApi rejected the key (401).")
        if resp.status_code == 429:
            raise SerpApiError("SerpApi monthly quota exhausted (429).")
        if resp.status_code != 200:
            raise SerpApiError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        if "error" in data:
            raise SerpApiError(str(data["error"]))

        # Bill only a search SerpApi actually charges for. Their FAQ: "Only
        # successful searches are counted toward your monthly searches.
        # Cached, errored, and failed searches are not."
        #
        # This counter previously did not exist at all, while main.verify()
        # had been changed to bill the DELTA of it -- so verifications were
        # billed zero and the ledger under-reported against a 250/month
        # tier that is already tight.
        self.call_count += 1
        return data

    def verify(
        self,
        origin: str,
        destination: str,
        outbound_date: str,
        return_date: Optional[str] = None,
        max_stops: Optional[int] = None,
    ) -> PriceInsight:
        """One search. Costs one unit of the monthly quota."""
        params: Dict[str, Any] = {
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": outbound_date,
            # 1 = round trip, 2 = one way
            "type": "1" if return_date else "2",
            "travel_class": "1",  # economy
            "adults": "1",
            "deep_search": "true",
        }
        if return_date:
            params["return_date"] = return_date
        if max_stops is not None:
            # 0=any, 1=nonstop, 2=<=1 stop, 3=<=2 stops
            params["stops"] = {0: "1", 1: "2", 2: "3"}.get(max_stops, "0")

        data = self._search(params)
        return self._parse(data)

    @staticmethod
    def _parse(data: Dict[str, Any]) -> PriceInsight:
        insight = PriceInsight()

        pi = data.get("price_insights") or {}
        if pi:
            lp = pi.get("lowest_price")
            if isinstance(lp, (int, float)):
                insight.lowest_price = float(lp)
            insight.price_level = str(pi.get("price_level", ""))

            tpr = pi.get("typical_price_range")
            if isinstance(tpr, (list, tuple)) and len(tpr) == 2:
                try:
                    insight.typical_low = float(tpr[0])
                    insight.typical_high = float(tpr[1])
                except (TypeError, ValueError):
                    pass

            hist = pi.get("price_history")
            if isinstance(hist, list) and hist:
                prices: List[float] = []
                for point in hist:
                    if isinstance(point, (list, tuple)) and len(point) == 2:
                        try:
                            prices.append(float(point[1]))
                        except (TypeError, ValueError):
                            continue
                if prices:
                    insight.history_min = min(prices)
                    insight.history_points = len(prices)

        # Cheapest actual offer on the page.
        offers: List[float] = []
        for key in ("best_flights", "other_flights"):
            for f in data.get(key, []) or []:
                p = f.get("price")
                if isinstance(p, (int, float)):
                    offers.append(float(p))
        if offers:
            insight.best_offer = min(offers)

        gf = data.get("search_metadata", {}).get("google_flights_url", "")
        insight.booking_url = gf or ""

        return insight
