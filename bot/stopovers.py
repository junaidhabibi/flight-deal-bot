"""Build itineraries with a deliberate multi-day stop in a third city.

This module produces the OVERNIGHT band only (24h+, see bot/layovers.py).
Quick connections under 5 hours are fine and need nothing built -- airlines
already sell those as a single ticket, and they arrive through the ordinary
fare scan. What no airline will sell you is the shape below, because past
~24 hours a connection stops being a connection and becomes two journeys:

    DFW --(nonstop)--> HEL   [stay 1-5 days]   HEL --(nonstop)--> ARN
                                                  [stay 5-21 nights]
    ARN --------------(return)--------------> DFW

Every hop is priced separately from the one-way fare calendar, so the total
is what you'd actually pay booking them as separate tickets. That also means
the stopover gap is guaranteed real: the bot literally cannot produce a
90-minute connection, because it only ever pairs flights on different days.

Two caveats the emails carry, because they're real and you should know:
  * Separate tickets = no protection if leg 1 is delayed and you miss leg 2.
    The 24h+ gap is itself the buffer, which is why the minimum is a full day.
  * Bags usually must be re-checked at the stopover.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .layovers import DEAD_ZONE, QUICK, STOPOVER, LayoverRules
from .models import Deal, Leg

log = logging.getLogger(__name__)


def _hours_between(a: datetime, b: datetime) -> float:
    """Hours from a to b, tolerating naive/aware mixtures."""
    if a.tzinfo and not b.tzinfo:
        b = b.replace(tzinfo=a.tzinfo)
    elif b.tzinfo and not a.tzinfo:
        a = a.replace(tzinfo=b.tzinfo)
    return (b - a).total_seconds() / 3600.0


def _arrival_estimate(departure: datetime, duration_minutes: Optional[Any]) -> datetime:
    """Arrival time, using flight duration when the API gives us one.

    When duration is missing we assume 10 hours for a transatlantic hop --
    deliberately generous, so a missing value can only ever *shrink* the
    computed layover and push a marginal itinerary below the threshold,
    never inflate one above it.
    """
    try:
        minutes = int(duration_minutes) if duration_minutes else None
    except (TypeError, ValueError):
        minutes = None
    return departure + timedelta(minutes=minutes if minutes else 600)


class StopoverBuilder:
    def __init__(
        self,
        rules: Optional[LayoverRules] = None,
        min_nights: int = 5,
        max_nights: int = 21,
        max_extra_stops: int = 1,
        min_layover_hours: Optional[float] = None,
        max_layover_hours: Optional[float] = None,
    ):
        if rules is None:
            rules = LayoverRules(
                overnight_min_hours=(
                    24 if min_layover_hours is None else float(min_layover_hours)
                ),
                overnight_max_hours=(
                    120 if max_layover_hours is None else float(max_layover_hours)
                ),
            )
        self.rules = rules
        # This builder only ever produces the OVERNIGHT band. Quick
        # connections need no stitching -- an airline already sells those as
        # a single ticket, and they come through the ordinary fare scan.
        # Between overnight_min_hours and 24h the duration alone isn't
        # enough; pair_legs also checks the gap covers a night.
        self.min_layover = rules.overnight_min_hours
        self.max_layover = rules.overnight_max_hours
        self.min_nights = int(min_nights)
        self.max_nights = int(max_nights)
        self.max_extra_stops = int(max_extra_stops)

    # ---------- the core pairing ----------

    def pair_legs(
        self,
        leg_a_by_date: Dict[date, Dict[str, Any]],
        leg_b_by_date: Dict[date, Dict[str, Any]],
    ) -> List[Tuple[Dict[str, Any], Dict[str, Any], float]]:
        """Every (leg A, leg B, layover_hours) pairing inside the window.

        Returns the cheapest combination per (departure date, layover length)
        so one great outbound doesn't flood the results.
        """
        pairs: List[Tuple[Dict[str, Any], Dict[str, Any], float]] = []
        if not leg_a_by_date or not leg_b_by_date:
            return pairs

        b_dates = sorted(leg_b_by_date.keys())

        for a_date in sorted(leg_a_by_date.keys()):
            a = leg_a_by_date[a_date]
            a_depart = a["departure_at"]
            a_arrive = _arrival_estimate(a_depart, a.get("duration"))

            # Only look at B departures in the plausible date band.
            lo = a_arrive.date()
            hi = (a_arrive + timedelta(hours=self.max_layover)).date()

            for b_date in b_dates:
                if b_date < lo or b_date > hi:
                    continue
                b = leg_b_by_date[b_date]
                layover = _hours_between(a_arrive, b["departure_at"])
                if layover > self.max_layover:
                    continue

                # Local clock times AT THE HUB, which is what decides whether
                # a sub-24h gap is a night in a hotel or a day in a terminal.
                # The onward departure is already local to the hub, so the
                # arrival is simply that minus the layover -- no timezone
                # table needed, and no risk of comparing a Dallas clock to a
                # Helsinki one.
                depart_local = b["departure_at"]
                arrive_local = depart_local - timedelta(hours=layover)

                if self.rules.classify(layover, arrive_local, depart_local) != STOPOVER:
                    continue
                if a.get("transfers", 0) > self.max_extra_stops:
                    continue
                if b.get("transfers", 0) > self.max_extra_stops:
                    continue
                pairs.append((a, b, layover))
        return pairs

    # ---------- full itineraries ----------

    def build(
        self,
        origin: str,
        hub: str,
        destination: str,
        destination_city: str,
        hub_city: str,
        leg_a: Dict[date, Dict[str, Any]],
        leg_b: Dict[date, Dict[str, Any]],
        return_legs: Optional[Dict[date, Dict[str, Any]]] = None,
        booking_url_fn=None,
        max_results: int = 25,
    ) -> List[Deal]:
        """Assemble priced Deals for origin -> hub -> destination (-> origin)."""
        deals: List[Deal] = []

        for a, b, layover in self.pair_legs(leg_a, leg_b):
            outbound_price = a["price"] + b["price"]
            arrive_dest = _arrival_estimate(b["departure_at"], b.get("duration"))

            candidates: List[Tuple[Optional[Dict[str, Any]], float, Optional[date]]] = []
            if return_legs:
                for r_date, r in sorted(return_legs.items()):
                    nights = (r_date - arrive_dest.date()).days
                    if nights < self.min_nights or nights > self.max_nights:
                        continue
                    if r.get("transfers", 0) > self.max_extra_stops:
                        continue
                    candidates.append((r, outbound_price + r["price"], r_date))
                if not candidates:
                    continue
                # Cheapest qualifying return only.
                candidates.sort(key=lambda t: t[1])
                candidates = candidates[:1]
            else:
                candidates = [(None, outbound_price, None)]

            for r, total, r_date in candidates:
                legs = [
                    Leg(
                        origin=origin,
                        destination=hub,
                        depart_at=a["departure_at"],
                        arrive_at=_arrival_estimate(a["departure_at"], a.get("duration")),
                        airline=a.get("airline", ""),
                        flight_number=a.get("flight_number", ""),
                        stops=int(a.get("transfers", 0) or 0),
                        price_usd=a["price"],
                        duration_minutes=a.get("duration"),
                    ),
                    Leg(
                        origin=hub,
                        destination=destination,
                        depart_at=b["departure_at"],
                        arrive_at=arrive_dest,
                        airline=b.get("airline", ""),
                        flight_number=b.get("flight_number", ""),
                        stops=int(b.get("transfers", 0) or 0),
                        price_usd=b["price"],
                        duration_minutes=b.get("duration"),
                    ),
                ]
                if r:
                    legs.append(
                        Leg(
                            origin=destination,
                            destination=origin,
                            depart_at=r["departure_at"],
                            arrive_at=_arrival_estimate(
                                r["departure_at"], r.get("duration")
                            ),
                            airline=r.get("airline", ""),
                            flight_number=r.get("flight_number", ""),
                            stops=int(r.get("transfers", 0) or 0),
                            price_usd=r["price"],
                            duration_minutes=r.get("duration"),
                        )
                    )

                url = ""
                if booking_url_fn:
                    try:
                        url = booking_url_fn(a.get("raw", a))
                    except Exception:
                        url = ""

                deals.append(
                    Deal(
                        origin=origin,
                        destination=destination,
                        destination_city=destination_city,
                        price_usd=round(total, 2),
                        depart_date=a["departure_at"].date(),
                        return_date=r_date,
                        stopover_code=hub,
                        stopover_city=hub_city,
                        stopover_hours=round(layover, 1),
                        legs=legs,
                        airline=a.get("airline", ""),
                        booking_url=url,
                        source="travelpayouts",
                        notes=[
                            f"Separate tickets: {origin}->{hub}, {hub}->{destination}"
                            + (f", {destination}->{origin}" if r else ""),
                            f"{layover / 24:.1f} days in {hub_city}",
                        ],
                    )
                )

        # Cheapest first, and only keep the best few per route.
        deals.sort(key=lambda d: d.price_usd)
        return self._dedupe(deals)[:max_results]

    @staticmethod
    def _dedupe(deals: List[Deal]) -> List[Deal]:
        """One entry per (departure date, stopover length in whole days)."""
        seen = set()
        out: List[Deal] = []
        for d in deals:
            key = (
                d.depart_date,
                d.stopover_code,
                int((d.stopover_hours or 0) // 24),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
        return out


def verify_layover_rule(deal: Deal, rules_or_min_hours) -> bool:
    """Belt-and-braces check before anything is emailed.

    Recomputes the gap from the actual leg times rather than trusting the
    stored value, so a bug upstream can't let a dead-zone layover reach you.

    Accepts either a LayoverRules or a bare minimum-hours float, so older
    callers keep working.
    """
    if not deal.stopover_code:
        return True  # plain round trip, checked separately
    if len(deal.legs) < 2:
        return False

    leg_a, leg_b = deal.legs[0], deal.legs[1]
    arrive = leg_a.arrive_at or _arrival_estimate(leg_a.depart_at, leg_a.duration_minutes)
    gap = _hours_between(arrive, leg_b.depart_at)

    if isinstance(rules_or_min_hours, LayoverRules):
        # A built stopover must be in the overnight band specifically, and
        # a sub-24h one must genuinely span a night. Leg B's departure is
        # local to the hub, so the arrival derives from it.
        depart_local = leg_b.depart_at
        arrive_local = depart_local - timedelta(hours=gap)
        return (
            rules_or_min_hours.classify(gap, arrive_local, depart_local)
            == STOPOVER
        )
    return gap >= float(rules_or_min_hours)
