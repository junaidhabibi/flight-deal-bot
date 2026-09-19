"""The layover rule, and how to police it on fares that don't disclose one.

Your rule has two acceptable shapes and a gap in the middle:

    0 - 5h     QUICK      a normal connection. Fine, nothing special.
    5 - 14h    DEAD ZONE  rejected. Too long to sit in a terminal, too short
                          to be worth leaving the airport and getting a room.
    14 - 24h   depends    a stopover ONLY if it actually spans a night.
                          14h of 18:00-08:00 is a hotel and a morning;
                          14h of 08:00-22:00 is a long day with a suitcase.
    24 - 120h  STOPOVER   a real stay. Sleep there, see the city, fly on.
    > 120h                that's two trips, not a stopover.

The dead zone is the whole point of this module. A 9-hour layover in
Frankfurt is the single worst thing a cheap fare can hide, and it is exactly
what a price-sorted search will hand you.

Enforcing it is easy on itineraries the bot builds itself, because it knows
every flight time. It is harder on fares that come back from the fare API,
which report a total journey duration but never the layover inside it. So
this module also estimates the layover from the total duration minus how long
the flight should take, using great-circle distance. That estimate is crude
and biased high (a routing detour looks like waiting time), so it only
rejects a fare when it is well past the boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Dict, Optional, Tuple

# Layover classifications.
QUICK = "quick"
DEAD_ZONE = "dead_zone"
STOPOVER = "stopover"
TOO_LONG = "too_long"

ACCEPTABLE = (QUICK, STOPOVER)


def spans_night(
    arrive_local: datetime,
    depart_local: datetime,
    core_start_hour: int = 1,
    core_end_hour: int = 6,
) -> bool:
    """Does this layover actually contain a night you could sleep through?

    The reason this exists: duration alone is a bad test below 24 hours.
    A 14-hour layover from 08:00 to 22:00 is a long day dragging a suitcase
    around with nowhere to leave it. The same 14 hours from 18:00 to 08:00
    is a hotel, a dinner, a night's sleep and a morning. Identical on a
    stopwatch, completely different trips.

    A night counts as covered when the layover fully contains the quiet
    hours -- 01:00 to 06:00 by default -- of some night in the middle.

    Both times must be LOCAL to the stopover city. Comparing a Dallas clock
    against a Helsinki one would be meaningless.
    """
    if depart_local <= arrive_local:
        return False

    # Work in naive local time; offsets are already baked in by the caller.
    a = arrive_local.replace(tzinfo=None)
    d = depart_local.replace(tzinfo=None)

    day = a.date()
    last = d.date()
    while day <= last:
        night_start = datetime.combine(day, time(hour=core_start_hour))
        night_end = datetime.combine(day, time(hour=core_end_hour))
        if a <= night_start and d >= night_end:
            return True
        day += timedelta(days=1)
    return False


@dataclass
class LayoverRules:
    """The acceptable bands, and the gap between them."""

    quick_max_hours: float = 5.0
    overnight_min_hours: float = 14.0
    overnight_max_hours: float = 120.0

    # Below this many hours, a layover only counts as a stopover if it
    # genuinely spans a night (see spans_night). Past it, the stay is long
    # enough that a night is guaranteed whatever the clock times are.
    require_night_below_hours: float = 24.0
    night_core_start: int = 1
    night_core_end: int = 6

    def __post_init__(self) -> None:
        if self.quick_max_hours <= 0:
            raise ValueError("quick_max_hours must be positive")
        if self.overnight_min_hours <= self.quick_max_hours:
            raise ValueError(
                "overnight_min_hours must exceed quick_max_hours -- otherwise "
                "there is no dead zone and the rule means nothing"
            )
        if self.overnight_max_hours <= self.overnight_min_hours:
            raise ValueError("overnight_max_hours must exceed overnight_min_hours")

    def classify(
        self,
        hours: Optional[float],
        arrive_local: Optional[datetime] = None,
        depart_local: Optional[datetime] = None,
    ) -> Optional[str]:
        """Which band a layover falls into. None if unknown.

        Pass the local arrival and departure times when you have them. For a
        layover between overnight_min_hours and require_night_below_hours
        they decide it: long enough on the clock, but only a stopover if it
        actually covers a night. Without them, such a layover stays in the
        dead zone -- unverifiable is not the same as fine.
        """
        if hours is None:
            return None
        if hours <= self.quick_max_hours:
            return QUICK
        if hours < self.overnight_min_hours:
            return DEAD_ZONE

        if hours < self.require_night_below_hours:
            if arrive_local is None or depart_local is None:
                return DEAD_ZONE
            if not spans_night(
                arrive_local, depart_local,
                self.night_core_start, self.night_core_end,
            ):
                return DEAD_ZONE

        if hours <= self.overnight_max_hours:
            return STOPOVER
        return TOO_LONG

    def accepts(
        self,
        hours: Optional[float],
        arrive_local: Optional[datetime] = None,
        depart_local: Optional[datetime] = None,
    ) -> bool:
        return self.classify(hours, arrive_local, depart_local) in ACCEPTABLE

    def describe(self) -> str:
        return (
            f"quick connections up to {self.quick_max_hours:.0f}h, or "
            f"stopovers of {self.overnight_min_hours:.0f}-"
            f"{self.overnight_max_hours:.0f}h -- and under "
            f"{self.require_night_below_hours:.0f}h it must actually span a "
            f"night ({self.night_core_start:02d}:00-{self.night_core_end:02d}:00). "
            f"Nothing else."
        )

    def explain(
        self,
        hours: Optional[float],
        arrive_local: Optional[datetime] = None,
        depart_local: Optional[datetime] = None,
    ) -> str:
        band = self.classify(hours, arrive_local, depart_local)
        if band is None:
            return "layover length unknown"
        if band == QUICK:
            return f"{hours:.1f}h connection"
        if band == DEAD_ZONE:
            if hours >= self.overnight_min_hours:
                return (
                    f"{hours:.1f}h layover, but it doesn't span a night -- "
                    f"a long day with nowhere to put the bag"
                )
            return (
                f"{hours:.1f}h layover -- in the dead zone "
                f"({self.quick_max_hours:.0f}-{self.overnight_min_hours:.0f}h)"
            )
        if band == STOPOVER:
            if hours < 24:
                return f"{hours:.0f}h overnight stop"
            return f"{hours / 24:.1f}-day stopover"
        return f"{hours / 24:.1f} days -- longer than a stopover should be"


# ---------------------------------------------------------------------------
#  Airport coordinates, for estimating how long a flight *should* take.
#  Only the airports this bot searches. Precision beyond a few km is
#  irrelevant here -- this feeds a duration estimate, not navigation.
# ---------------------------------------------------------------------------

AIRPORTS: Dict[str, Tuple[float, float]] = {
    # US origins
    "DFW": (32.8998, -97.0403),
    "DAL": (32.8471, -96.8518),
    "AUS": (30.1975, -97.6664),
    "IAH": (29.9902, -95.3368),
    "ORD": (41.9786, -87.9048),
    "JFK": (40.6413, -73.7781),
    "EWR": (40.6895, -74.1745),
    "BOS": (42.3656, -71.0096),
    # Nordics
    "ARN": (59.6519, 17.9186),
    "GOT": (57.6628, 12.2798),
    "CPH": (55.6180, 12.6560),
    "OSL": (60.1939, 11.1004),
    "HEL": (60.3172, 24.9633),
    "KEF": (63.9850, -22.6056),
    # Western Europe
    "LHR": (51.4700, -0.4543),
    "LGW": (51.1537, -0.1821),
    "DUB": (53.4213, -6.2701),
    "CDG": (49.0097, 2.5479),
    "AMS": (52.3105, 4.7683),
    "BRU": (50.9014, 4.4844),
    "FRA": (50.0379, 8.5622),
    "MUC": (48.3538, 11.7861),
    "BER": (52.3667, 13.5033),
    "ZRH": (47.4647, 8.5492),
    "VIE": (48.1103, 16.5697),
    # Southern Europe
    "MAD": (40.4983, -3.5676),
    "BCN": (41.2974, 2.0833),
    "LIS": (38.7742, -9.1342),
    "FCO": (41.8003, 12.2389),
    "MXP": (45.6306, 8.7281),
    "ATH": (37.9364, 23.9445),
    "IST": (41.2753, 28.7519),
    # Central / Eastern Europe
    "PRG": (50.1008, 14.2600),
    "WAW": (52.1657, 20.9671),
    "BUD": (47.4369, 19.2556),
}

EARTH_RADIUS_KM = 6371.0

# Typical cruise speed for a jet on these routes, plus fixed time on the
# ground at each end (taxi, climb, descent). Both deliberately generous, so
# the estimated flight time is on the long side and the inferred layover on
# the short side -- an estimate that errs toward keeping a fare, not binning it.
CRUISE_KMH = 820.0
GROUND_MINUTES = 40.0


def great_circle_km(a: str, b: str) -> Optional[float]:
    """Distance between two airports, or None if either is unknown."""
    pa, pb = AIRPORTS.get(a.upper()), AIRPORTS.get(b.upper())
    if not pa or not pb:
        return None

    lat1, lon1 = math.radians(pa[0]), math.radians(pa[1])
    lat2, lon2 = math.radians(pb[0]), math.radians(pb[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1

    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, h)))


def expected_flight_minutes(origin: str, destination: str) -> Optional[float]:
    """How long the flying itself should take, nonstop."""
    km = great_circle_km(origin, destination)
    if km is None:
        return None
    return (km / CRUISE_KMH) * 60 + GROUND_MINUTES


def estimate_layover_hours(
    origin: str,
    destination: str,
    total_minutes: Optional[float],
    transfers: int,
) -> Optional[float]:
    """Rough layover time inside a connecting itinerary.

    Total journey time minus the time the flying should take. The remainder
    is waiting -- plus whatever detour the routing adds, which is why this
    runs high and why callers apply a margin before acting on it.

    Returns None when it can't be computed, which callers treat as "unknown"
    rather than "fine".
    """
    if not total_minutes or transfers < 1:
        return None
    expected = expected_flight_minutes(origin, destination)
    if expected is None:
        return None

    # Each extra stop costs a descent and a climb even with no waiting.
    expected += 25 * transfers

    excess = float(total_minutes) - expected
    return round(max(0.0, excess) / 60, 2)


@dataclass
class LayoverEstimate:
    """A layover inferred rather than observed, with its caveats attached."""

    hours: Optional[float]
    band: Optional[str]
    confident: bool
    reason: str = ""

    @property
    def is_known_bad(self) -> bool:
        """A band we refuse, confident enough to act on.

        TOO_LONG was missing from this until 2026-09-19, so a fare with an
        estimated 500-hour layover was kept silently. The dead zone needs
        the confidence check because its estimate sits near a boundary;
        TOO_LONG is never a near miss.
        """
        if self.band == TOO_LONG:
            return True
        return self.band == DEAD_ZONE and self.confident


def assess_api_layover(
    origin: str,
    destination: str,
    total_minutes: Optional[float],
    transfers: int,
    rules: LayoverRules,
    margin_hours: float = 2.5,
) -> LayoverEstimate:
    """Judge a fare whose layover the API didn't tell us.

    margin_hours absorbs the estimate's upward bias. A fare is only called
    dead-zone when it clears the quick-connection ceiling by more than the
    margin, so a nonstop-ish routing with a long detour isn't binned for a
    layover it doesn't have.
    """
    if transfers is None:
        # The source didn't say. Not the same as nonstop, and the difference
        # decides whether the whole layover rule applies.
        return LayoverEstimate(
            None, None, False, "stop count not reported -- layover unknown"
        )

    if transfers < 1:
        return LayoverEstimate(0.0, QUICK, True, "nonstop -- no layover")

    hours = estimate_layover_hours(origin, destination, total_minutes, transfers)
    if hours is None:
        return LayoverEstimate(
            None, None, False,
            "airline didn't report a layover and it couldn't be estimated",
        )

    band = rules.classify(hours)

    # Only confident when the estimate is clear of the boundary by the margin.
    if band == DEAD_ZONE:
        confident = hours > rules.quick_max_hours + margin_hours
        reason = (
            f"estimated ~{hours:.1f}h layover"
            if confident
            else f"estimated ~{hours:.1f}h layover, close enough to "
                 f"{rules.quick_max_hours:.0f}h to be routing detour"
        )
        return LayoverEstimate(hours, band, confident, reason)

    if band == TOO_LONG:
        # Past overnight_max_hours. This was returned as is_known_bad=False
        # and therefore never rejected and never even flagged -- a fare with
        # an estimated 544-hour layover would have been kept and the email
        # would have printed it. It is also what a units mix-up looks like
        # (duration delivered in seconds rather than minutes), so treating
        # it as bad catches a whole class of upstream change.
        return LayoverEstimate(
            hours, band, True,
            f"estimated ~{hours:.0f}h layover -- beyond the "
            f"{rules.overnight_max_hours:.0f}h limit",
        )

    return LayoverEstimate(
        hours, band, True, f"estimated ~{hours:.1f}h layover"
    )
