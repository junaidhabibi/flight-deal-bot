"""Google Flight Deals — discovery that actually works for these routes.

Why this exists: the Travelpayouts cache turned out to be far too thin for
DFW->Scandinavia. Diagnostics showed every cached destination from DFW was
domestic (Detroit, Atlanta, Denver...), while JFK->London returned plenty.
That cache is only as deep as Aviasales' own search traffic, and almost
nobody searches Dallas->Stockholm on a Russian metasearch engine. No amount
of tuning markets or months fixes a cache that has no data in it.

Google Flight Deals inverts the question. Instead of "what does DFW->ARN
cost?" (which needs a route someone already searched), it asks "what is
cheap from DFW right now?" and answers across every destination at once,
over a flexible date range. One request, many destinations.

It also hands back two fields nothing else here provides:

    average_price        what the route normally costs
    discount_percentage  how far below that this fare sits

which is precisely the normal-vs-found comparison the alerts are built
around -- computed by Google across its own history rather than inferred.

Cost: one SerpApi search per call, against a 250/month free tier. That is
why the bot makes very few of these and paces them daily.

Docs: https://serpapi.com/google-flights-deals-api
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests

from ..models import Deal, Leg

log = logging.getLogger(__name__)

ENDPOINT = "https://serpapi.com/search"


class GoogleDealsError(Exception):
    pass


class GoogleFlightDeals:
    def __init__(
        self,
        api_key: str,
        currency: str = "USD",
        country: str = "us",
        timeout: int = 60,
        session: Optional[requests.Session] = None,
    ):
        if not api_key:
            raise GoogleDealsError("A SerpApi key is required.")
        self.api_key = api_key
        self.currency = currency
        self.country = country
        self.timeout = timeout
        self.session = session or requests.Session()
        self.call_count = 0

    # ---------- transport ----------

    def _search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params)
        params.update(
            {
                "engine": "google_flights_deals",
                "api_key": self.api_key,
                "currency": self.currency,
                "hl": "en",
                "gl": self.country,
            }
        )
        try:
            resp = self.session.get(ENDPOINT, params=params, timeout=self.timeout)
            self.call_count += 1
        except requests.RequestException as e:
            raise GoogleDealsError(f"network error: {e}") from e

        if resp.status_code == 401:
            raise GoogleDealsError("SerpApi rejected the key (401).")
        if resp.status_code == 429:
            raise GoogleDealsError("SerpApi quota exhausted (429).")
        if resp.status_code != 200:
            raise GoogleDealsError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        if "error" in data:
            raise GoogleDealsError(str(data["error"]))
        return data

    # ---------- the one call that matters ----------

    def find_deals(
        self,
        origin: str,
        depart_from: date,
        depart_to: date,
        min_nights: int,
        max_nights: int,
        max_stops: Optional[int] = 1,
        max_price: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Every deal Google currently sees from `origin`, flexible dates.

        No destination is passed -- that is the entire point. Asking about a
        specific route is what failed before.
        """
        params: Dict[str, Any] = {
            "departure_id": origin,
            "type": "1",  # round trip
            "outbound_date": f"{depart_from.isoformat()},{depart_to.isoformat()}",
            "trip_length": f"{min_nights},{max_nights}",
            "travel_class": "1",
            "adults": "1",
        }
        if max_stops is not None:
            # 0=any, 1=nonstop, 2=<=1 stop, 3=<=2 stops
            params["stops"] = {0: "1", 1: "2", 2: "3"}.get(max_stops, "0")
        if max_price:
            params["max_price"] = str(int(max_price))

        data = self._search(params)

        # The payload has been served under a couple of different keys; take
        # whichever is present rather than guessing one and breaking later.
        for key in ("deals", "flight_deals", "best_deals"):
            rows = data.get(key)
            if isinstance(rows, list) and rows:
                return rows
        return []

    # ---------- parsing ----------

    @staticmethod
    def _as_date(value: Any) -> Optional[date]:
        if not value:
            return None
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).replace("$", "").replace(",", "").strip()
        try:
            return float(text)
        except ValueError:
            return None

    def to_deal(
        self,
        row: Dict[str, Any],
        city_lookup: Optional[Dict[str, str]] = None,
    ) -> Optional[Deal]:
        """Turn one Google deal into a Deal, carrying Google's own benchmark."""
        price = self._as_float(row.get("price"))
        if not price or price <= 0:
            return None

        origin = (row.get("departure_airport_code") or "").upper()
        destination = (row.get("arrival_airport_code") or "").upper()
        if not origin or not destination:
            return None

        # SerpApi's docs name these start_date/end_date, but the live
        # response uses outbound_date/return_date. Accept both: the docs may
        # be stale, or the field may have been renamed, and silently
        # returning None here would drop every single deal.
        depart = self._as_date(row.get("outbound_date") or row.get("start_date"))
        if not depart:
            return None
        ret = self._as_date(row.get("return_date") or row.get("end_date"))

        stops = row.get("stops")
        try:
            stops = int(stops) if stops is not None else 0
        except (TypeError, ValueError):
            stops = 0

        city = (city_lookup or {}).get(destination) or row.get("name") or destination

        leg = Leg(
            origin=origin,
            destination=destination,
            depart_at=datetime.combine(depart, datetime.min.time()),
            airline=row.get("airline_code", "") or "",
            stops=stops,
            price_usd=price,
            duration_minutes=row.get("flight_duration"),
        )

        deal = Deal(
            origin=origin,
            destination=destination,
            destination_city=city,
            price_usd=price,
            depart_date=depart,
            return_date=ret,
            legs=[leg],
            airline=row.get("airline_code", "") or "",
            booking_url=row.get("flight_link") or row.get("serpapi_flight_link") or "",
            source="google_deals",
        )

        # Google's own benchmark. This is better than anything the bot can
        # infer on day one, because it is drawn from Google's history of the
        # route rather than from a handful of local observations.
        avg = self._as_float(row.get("average_price"))
        if avg and avg > 0:
            deal.typical_price = round(avg, 2)
            deal.typical_basis = "Google's average price for this route"

        pct = self._as_float(row.get("discount_percentage"))
        if pct:
            deal.notes.append(f"Google rates this {abs(pct):.0f}% below average")

        return deal

    def scan(
        self,
        origin: str,
        depart_from: date,
        depart_to: date,
        min_nights: int,
        max_nights: int,
        wanted_destinations: Optional[Sequence[str]] = None,
        city_lookup: Optional[Dict[str, str]] = None,
        max_stops: Optional[int] = 1,
        max_price: Optional[float] = None,
    ) -> List[Deal]:
        """One API call -> Deals, filtered to the destinations you care about."""
        rows = self.find_deals(
            origin=origin,
            depart_from=depart_from,
            depart_to=depart_to,
            min_nights=min_nights,
            max_nights=max_nights,
            max_stops=max_stops,
            max_price=max_price,
        )
        wanted = {d.upper() for d in (wanted_destinations or [])}

        deals: List[Deal] = []
        skipped = 0
        for row in rows:
            code = (row.get("arrival_airport_code") or "").upper()
            if wanted and code not in wanted:
                skipped += 1
                continue
            d = self.to_deal(row, city_lookup)
            if d:
                deals.append(d)

        log.info(
            "Google Deals %s: %d deals returned, %d match your destinations "
            "(%d elsewhere)",
            origin, len(rows), len(deals), skipped,
        )
        return deals


# Knowledge Graph IDs for regions, used by arrival_area_id.
AREA_EUROPE = "/m/02j9z"

# travel_duration values the Explore engine accepts.
DURATION_WEEKEND = 1
DURATION_ONE_WEEK = 2
DURATION_TWO_WEEKS = 3


class GoogleTravelExplore:
    """"What's cheap from DFW to anywhere in Europe?" -- one call.

    The Deals engine above answers "what's cheap from here", full stop, and
    from Dallas the answer is always Las Vegas and New Orleans: it returns
    the globally cheapest deals, and domestic short-haul wins every time.
    Useful, but it will never surface a $450 Stockholm fare while a $38
    Vegas fare exists.

    Explore takes `arrival_area_id`, so the question becomes "what's cheap
    from DFW *to Europe*" and the $38 Vegas fare is simply out of scope.
    That is the difference between a tool that occasionally mentions Europe
    and one that is actually looking for it.

    Docs: https://serpapi.com/google-travel-explore-api
    """

    def __init__(
        self,
        api_key: str,
        currency: str = "USD",
        country: str = "us",
        timeout: int = 60,
        session: Optional[requests.Session] = None,
    ):
        if not api_key:
            raise GoogleDealsError("A SerpApi key is required.")
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
                "engine": "google_travel_explore",
                "api_key": self.api_key,
                "currency": self.currency,
                "hl": "en",
                "gl": self.country,
            }
        )
        try:
            resp = self.session.get(ENDPOINT, params=params, timeout=self.timeout)
            self.call_count += 1
        except requests.RequestException as e:
            raise GoogleDealsError(f"network error: {e}") from e

        if resp.status_code == 401:
            raise GoogleDealsError("SerpApi rejected the key (401).")
        if resp.status_code == 429:
            raise GoogleDealsError("SerpApi quota exhausted (429).")
        if resp.status_code != 200:
            raise GoogleDealsError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        if "error" in data:
            raise GoogleDealsError(str(data["error"]))
        return data

    def explore(
        self,
        origin: str,
        area_id: str = AREA_EUROPE,
        month: Optional[int] = None,
        travel_duration: int = DURATION_ONE_WEEK,
        max_stops: Optional[int] = 1,
        max_price: Optional[float] = None,
        carry_on_bags: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Destinations in `area_id` reachable cheaply from `origin`."""
        params: Dict[str, Any] = {
            "departure_id": origin,
            "arrival_area_id": area_id,
            "type": "1",           # round trip
            "travel_class": "1",   # economy
            "travel_mode": "1",    # flights only
            "adults": "1",
            "travel_duration": str(travel_duration),
        }
        if month:
            params["month"] = str(month)
        if max_stops is not None:
            params["stops"] = {0: "1", 1: "2", 2: "3"}.get(max_stops, "0")
        if max_price:
            params["max_price"] = str(int(max_price))
        # Pricing WITH the carry-on where the engine supports it, so the
        # figure quoted already reflects the bag rather than a basic fare
        # that would cost more once it's added.
        if carry_on_bags:
            params["bags"] = str(int(carry_on_bags))

        data = self._search(params)
        for key in ("destinations", "results", "explore_results"):
            rows = data.get(key)
            if isinstance(rows, list) and rows:
                return rows
        return []

    @staticmethod
    def to_deal(
        row: Dict[str, Any],
        origin: str,
        city_lookup: Optional[Dict[str, str]] = None,
    ) -> Optional[Deal]:
        price = GoogleFlightDeals._as_float(
            row.get("flight_price") if row.get("flight_price") is not None
            else row.get("price")
        )
        if not price or price <= 0:
            return None

        airport = row.get("destination_airport") or {}
        code = (airport.get("code") if isinstance(airport, dict) else None) or ""
        code = (code or row.get("arrival_airport_code") or "").upper()
        if not code:
            return None

        depart = GoogleFlightDeals._as_date(
            row.get("start_date") or row.get("outbound_date")
        )
        if not depart:
            return None
        ret = GoogleFlightDeals._as_date(
            row.get("end_date") or row.get("return_date")
        )

        stops = row.get("number_of_stops", row.get("stops"))
        try:
            stops = int(stops) if stops is not None else 0
        except (TypeError, ValueError):
            stops = 0

        city = (city_lookup or {}).get(code) or row.get("name") or code

        leg = Leg(
            origin=origin,
            destination=code,
            depart_at=datetime.combine(depart, datetime.min.time()),
            airline=row.get("airline_code", "") or "",
            stops=stops,
            price_usd=price,
            duration_minutes=row.get("flight_duration"),
        )

        return Deal(
            origin=origin,
            destination=code,
            destination_city=city,
            price_usd=price,
            depart_date=depart,
            return_date=ret,
            legs=[leg],
            airline=row.get("airline_code", "") or "",
            source="google_explore",
        )

    def scan(
        self,
        origin: str,
        wanted_destinations: Optional[Sequence[str]] = None,
        city_lookup: Optional[Dict[str, str]] = None,
        area_id: str = AREA_EUROPE,
        month: Optional[int] = None,
        travel_duration: int = DURATION_ONE_WEEK,
        max_stops: Optional[int] = 1,
        max_price: Optional[float] = None,
        carry_on_bags: Optional[int] = None,
    ) -> List[Deal]:
        rows = self.explore(
            origin=origin, area_id=area_id, month=month,
            travel_duration=travel_duration, max_stops=max_stops,
            max_price=max_price, carry_on_bags=carry_on_bags,
        )
        wanted = {d.upper() for d in (wanted_destinations or [])}

        deals, elsewhere = [], 0
        for row in rows:
            d = self.to_deal(row, origin, city_lookup)
            if not d:
                continue
            if wanted and d.destination not in wanted:
                elsewhere += 1
                continue
            deals.append(d)

        log.info(
            "Google Explore %s->Europe (month=%s): %d destinations, "
            "%d on your list, %d elsewhere in Europe",
            origin, month or "any", len(rows), len(deals), elsewhere,
        )
        return deals
