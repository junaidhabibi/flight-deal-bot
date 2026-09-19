"""Core data structures shared across the bot."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional, List, Dict, Any


@dataclass
class Leg:
    """One flight segment (or one bookable hop) in an itinerary."""

    origin: str
    destination: str
    depart_at: datetime
    arrive_at: Optional[datetime] = None
    airline: str = ""
    flight_number: str = ""
    stops: int = 0
    price_usd: Optional[float] = None
    duration_minutes: Optional[int] = None

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["depart_at"] = self.depart_at.isoformat() if self.depart_at else None
        d["arrive_at"] = self.arrive_at.isoformat() if self.arrive_at else None
        return d


@dataclass
class Deal:
    """A candidate fare the bot may alert on."""

    origin: str
    destination: str
    destination_city: str
    price_usd: float
    depart_date: date
    return_date: Optional[date] = None

    # Stopover detail (None for a plain round trip).
    stopover_code: Optional[str] = None
    stopover_city: Optional[str] = None
    stopover_hours: Optional[float] = None

    legs: List[Leg] = field(default_factory=list)
    airline: str = ""
    booking_url: str = ""
    source: str = ""                 # travelpayouts | serpapi | rss | kiwi

    # Layover, for fares where the airline didn't disclose one.
    layover_hours: Optional[float] = None
    layover_band: Optional[str] = None      # quick | dead_zone | stopover
    layover_estimated: bool = False
    layover_note: str = ""

    # Carry-on: what the bag adds, and whether it will actually fit.
    bag_fee_usd: float = 0.0
    bag_risk: str = "unknown"        # none | tight | oversize | unknown
    bag_warnings: List[str] = field(default_factory=list)

    # Filled in by scoring.
    reference_price: Optional[float] = None
    reference_basis: str = ""        # history | baseline | google_typical
    discount_pct: Optional[float] = None

    # What this route NORMALLY costs -- the median, not the bargain benchmark.
    # reference_price is deliberately the toughest bar a fare must beat;
    # typical_price is the honest "you'd usually pay about this" number.
    typical_price: Optional[float] = None
    typical_basis: str = ""
    typical_low: Optional[float] = None    # Google's typical range, if known
    typical_high: Optional[float] = None
    is_record: bool = False
    previous_record: Optional[float] = None
    observations: int = 0
    tier: str = ""                   # watch | good | great | insane
    score: float = 0.0
    verified_by: str = ""            # e.g. "serpapi"
    notes: List[str] = field(default_factory=list)

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"

    @property
    def total_price_usd(self) -> float:
        """What you actually pay, carry-on included.

        Everything downstream -- benchmarks, discounts, records, the price
        ceiling -- uses this rather than the headline fare. A $238 ticket
        that needs a $55 cabin bag on each of three separate bookings is not
        a $238 trip, and ranking it as one would put the wrong deal on top.
        """
        return round(self.price_usd + (self.bag_fee_usd or 0.0), 2)

    @property
    def airlines(self) -> List[str]:
        """Every operating carrier on the itinerary."""
        codes = [leg.airline for leg in self.legs if leg.airline]
        if not codes and self.airline:
            codes = [self.airline]
        return list(dict.fromkeys(codes))

    @property
    def savings_usd(self) -> Optional[float]:
        """Dollars off what the route normally costs."""
        if self.typical_price is None:
            return None
        return round(self.typical_price - self.total_price_usd, 2)

    @property
    def savings_pct(self) -> Optional[float]:
        """Percent off the normal price. Not the same as discount_pct, which
        measures against the tougher benchmark used to gate alerts."""
        if not self.typical_price:
            return None
        return round(
            (self.typical_price - self.total_price_usd) / self.typical_price * 100, 1
        )

    @property
    def bag_segments(self) -> int:
        """One-way flights the carry-on fee is charged on.

        A stitched stopover stores one Leg per separate ticket, and those
        legs already cover both directions -- so the leg count IS the
        segment count. A plain round trip is a single ticket stored as one
        Leg, but you still pay the bag each way, hence 2.
        """
        if self.stopover_code and self.legs:
            return len(self.legs)
        return 2 if self.return_date else 1

    @property
    def ticket_count(self) -> int:
        """Separate bookings you'd have to make."""
        if self.stopover_code and self.legs:
            return len(self.legs)
        return 1

    @property
    def history_key(self) -> str:
        """The price series this fare belongs to.

        A stitched multi-ticket stopover is a different product from a normal
        round trip on the same city pair -- it is priced from separate one-way
        fares and is systematically cheaper. Pooling them into one series
        would poison the benchmark for both: the cheap stopovers would drag
        the round-trip baseline down until a genuine error fare no longer
        looked remarkable. So each gets its own key.
        """
        if self.stopover_code:
            return f"{self.origin}-{self.stopover_code}-{self.destination}"
        return f"{self.origin}-{self.destination}"

    @property
    def nights(self) -> Optional[int]:
        if self.return_date and self.depart_date:
            return (self.return_date - self.depart_date).days
        return None

    def fingerprint(self) -> str:
        """Stable ID used for de-duplicating alerts.

        Price is bucketed to $10 so a $3 wobble doesn't re-alert, and the
        stopover is part of the identity (same route via a different city is
        a genuinely different trip).
        """
        bucket = int(self.total_price_usd // 10) * 10
        raw = "|".join(
            [
                self.origin,
                self.destination,
                self.stopover_code or "-",
                self.depart_date.isoformat(),
                self.return_date.isoformat() if self.return_date else "-",
                str(bucket),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

    def summary(self) -> str:
        via = f" via {self.stopover_city}" if self.stopover_city else ""
        stay = ""
        if self.stopover_hours:
            days = self.stopover_hours / 24
            stay = f" ({days:.1f}d stopover)"
        dates = self.depart_date.strftime("%b %d")
        if self.return_date:
            dates += f" - {self.return_date.strftime('%b %d')}"
        bag = f" (+${self.bag_fee_usd:,.0f} bag)" if self.bag_fee_usd else ""
        return (
            f"${self.total_price_usd:,.0f}{bag} {self.origin}->"
            f"{self.destination_city}{via}{stay}, {dates}"
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["total_price_usd"] = self.total_price_usd
        d["depart_date"] = self.depart_date.isoformat()
        d["return_date"] = self.return_date.isoformat() if self.return_date else None
        d["legs"] = [leg.to_dict() for leg in self.legs]
        d["fingerprint"] = self.fingerprint()
        return d


# Tier ordering, lowest to highest.
TIER_ORDER = ["watch", "good", "great", "insane"]


def tier_rank(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return -1
