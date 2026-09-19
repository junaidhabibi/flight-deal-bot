"""Carry-on fit and fee modelling.

Two separate problems, both of which change which fare is actually cheapest:

1. FEES. Plenty of cheap transatlantic fares (Finnair Economy Superlight,
   SAS Go Light, US basic economy) include only a small personal item. A
   headline $238 fare that needs a $55 cabin bag on each of three separate
   tickets is a $403 fare. Scoring the headline number would rank fares
   in the wrong order.

2. FIT. A bag that exceeds the sizer gets gate-checked, which costs money
   at the gate and means it rides in the hold anyway. Airlines differ by
   enough centimetres that this genuinely changes which carrier to prefer.

All dimensions here are stored in centimetres, including wheels and handles,
because that is how airlines measure and how their sizers are cut.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

CM_PER_INCH = 2.54
KG_PER_LB = 0.453592


@dataclass
class BagSpec:
    """The bag being carried, measured the way an airline measures it."""

    name: str
    height_cm: float
    width_cm: float
    depth_cm: float
    empty_weight_kg: float
    expandable_cm: float = 0.0

    @classmethod
    def from_inches(
        cls,
        name: str,
        height_in: float,
        width_in: float,
        depth_in: float,
        weight_lb: float,
        expandable_in: float = 0.0,
    ) -> "BagSpec":
        return cls(
            name=name,
            height_cm=round(height_in * CM_PER_INCH, 1),
            width_cm=round(width_in * CM_PER_INCH, 1),
            depth_cm=round(depth_in * CM_PER_INCH, 1),
            empty_weight_kg=round(weight_lb * KG_PER_LB, 2),
            expandable_cm=round(expandable_in * CM_PER_INCH, 1),
        )

    @property
    def dims_cm(self) -> Tuple[float, float, float]:
        return (self.height_cm, self.width_cm, self.depth_cm)

    def describe(self) -> str:
        return (
            f"{self.name}: {self.height_cm} x {self.width_cm} x {self.depth_cm} cm "
            f"({self.height_cm / CM_PER_INCH:.0f} x {self.width_cm / CM_PER_INCH:.0f} "
            f"x {self.depth_cm / CM_PER_INCH:.0f} in), "
            f"{self.empty_weight_kg} kg empty"
        )


# How an airline behaves in practice, which is not the same as its published
# policy. Sourced from traveler reports, not from carrier marketing:
#   lenient  - bags routinely go through unmeasured
#   moderate - spot checks; usually fine, occasionally not
#   strict   - sizers or scales in routine use at the gate
ENFORCEMENT_WEIGHT = {"lenient": 0.15, "moderate": 0.5, "strict": 1.0}


@dataclass
class CarryOnPolicy:
    """One airline's cabin baggage rules, and how it really behaves."""

    code: str
    name: str
    max_h_cm: float
    max_w_cm: float
    max_d_cm: float
    max_weight_kg: Optional[float] = None
    # Enforcement is tracked separately for size and weight, because several
    # carriers are relaxed about one and rigorous about the other. SAS is the
    # clearest case: it weighs bags but rarely measures them.
    size_enforcement: str = "moderate"
    weight_enforcement: str = "moderate"
    # Does the CHEAPEST fare this airline sells on a long-haul route include a
    # full-size cabin bag, or only a personal item?
    cabin_bag_in_cheapest_fare: bool = True
    # What it costs to add one, per ticket, per direction (USD, approximate).
    cabin_bag_fee_usd: float = 0.0
    # Some carriers are famously strict with the sizer.
    strict_sizer: bool = False
    # True where ordinary economy includes a cabin bag but the carrier also
    # sells a transatlantic basic-economy fare that allows a personal item
    # only. Distinct from cabin_bag_in_cheapest_fare=False, which means the
    # carrier charges for a cabin bag across the board.
    #
    # This exists because the fee table was wrong in both directions until
    # 2026-09-19: it had Finnair charging $55 on a DFW-ARN fare that in fact
    # includes a cabin bag, and United including one on a DFW-LHR fare that
    # in fact does not. Inclusion follows the FARE CLASS, which is not in the
    # data the bot receives -- so this flag produces a warning, never a fee.
    long_haul_basic_economy_strips_bag: bool = False
    notes: str = ""

    def fits(self, bag: BagSpec, allow_rotation: bool = True) -> Tuple[bool, List[str]]:
        """Does the bag fit, and if not, by how much is it over?

        Airlines measure against a rigid sizer, so a hardside bag either goes
        in or it doesn't -- there is no squeezing. Rotation is allowed because
        a sizer doesn't care which face is which, only that all three
        dimensions fit within the opening.
        """
        limits = sorted([self.max_h_cm, self.max_w_cm, self.max_d_cm], reverse=True)
        dims = sorted(bag.dims_cm, reverse=True)

        if not allow_rotation:
            limits = [self.max_h_cm, self.max_w_cm, self.max_d_cm]
            dims = list(bag.dims_cm)

        problems: List[str] = []
        for dim, limit in zip(dims, limits):
            if dim > limit:
                problems.append(f"{dim:.1f}cm vs {limit:.0f}cm limit (+{dim - limit:.1f})")

        return (not problems), problems

    def weight_headroom_kg(self, bag: BagSpec) -> Optional[float]:
        """How much you can actually pack before hitting the weight cap."""
        if self.max_weight_kg is None:
            return None
        return round(self.max_weight_kg - bag.empty_weight_kg, 1)

    def size_risk_score(self, bag: BagSpec) -> float:
        """0-1 chance of trouble over SIZE, blending overage with enforcement.

        A bag 3cm over on a carrier that never measures is a smaller problem
        than a bag 1cm over on one that sizers every passenger. Published
        dimensions alone would rank those the wrong way round.
        """
        fits, problems = self.fits(bag)
        if fits:
            return 0.0
        overage = 0.0
        for p in problems:
            try:
                overage = max(overage, float(p.split("+")[1].rstrip(")")))
            except (IndexError, ValueError):
                pass
        # Past ~5cm over, even a relaxed gate agent notices.
        severity = min(1.0, overage / 5.0)
        return round(severity * ENFORCEMENT_WEIGHT.get(self.size_enforcement, 0.5), 3)

    def weight_risk_score(self, bag: BagSpec, packed_kg: Optional[float]) -> float:
        """0-1 chance of trouble over WEIGHT."""
        if self.max_weight_kg is None or packed_kg is None:
            return 0.0
        total = bag.empty_weight_kg + packed_kg
        if total <= self.max_weight_kg:
            return 0.0
        over = (total - self.max_weight_kg) / self.max_weight_kg
        severity = min(1.0, over / 0.5)   # 50% over the cap = certain trouble
        return round(
            severity * ENFORCEMENT_WEIGHT.get(self.weight_enforcement, 0.5), 3
        )

    def fee_for(self, segments: int = 2) -> float:
        """Estimated cabin-bag cost across an itinerary.

        Charged per one-way flight, which is how airlines actually sell it.
        A normal round trip is 2 segments on one ticket. A stitched stopover
        (DFW-HEL, HEL-ARN, ARN-DFW) is 3 segments on 3 separate tickets --
        and that third fee is what makes a cheap-looking stopover not cheap.
        """
        if self.cabin_bag_in_cheapest_fare:
            return 0.0
        return round(self.cabin_bag_fee_usd * max(1, segments), 2)


# ---------------------------------------------------------------------------
#  Airline policies.
#
#  Verified 2026-09-18 against carrier pages and a cross-checked round-up.
#  Fees are ESTIMATES -- they vary by route, timing and whether you add the
#  bag at booking or at the airport. Tune them in config.yml as you learn
#  your real numbers.
# ---------------------------------------------------------------------------

POLICIES: Dict[str, CarryOnPolicy] = {
    # ---- US carriers: 22 x 14 x 9 in = 55.9 x 35.6 x 22.9 cm ----
    # Size enforcement on transatlantic departures is loose in practice, but
    # not absent: Pack Hacker had this exact Freeform gate-checked by Delta
    # on a return leg and called compliance "hit or miss".
    "AA": CarryOnPolicy("AA", "American", 55.9, 35.6, 22.9,
                        size_enforcement="lenient", weight_enforcement="lenient",
                        notes="No published cabin weight limit. Carry-on "
                              "included on transatlantic basic economy."),
    "DL": CarryOnPolicy("DL", "Delta", 55.9, 35.6, 22.9,
                        size_enforcement="lenient", weight_enforcement="lenient",
                        notes="Reported to gate-check oversize bags "
                              "occasionally, usually on return legs."),
    # UNRESOLVED 2026-09-19, and deliberately left as a warning rather than a
    # fee. This table said transatlantic basic economy includes a carry-on;
    # the cheapest United DFW-LHR round trip that day came back with a
    # personal item only. That was a single third-party listing, so it is not
    # enough to flip the table -- but it is enough not to trust it silently.
    # The flag makes the email say "verify this", which is the honest output
    # when the fare class isn't in the data.
    "UA": CarryOnPolicy("UA", "United", 55.9, 35.6, 22.9,
                        size_enforcement="lenient", weight_enforcement="lenient",
                        long_haul_basic_economy_strips_bag=True,
                        notes="Domestic basic economy excludes a carry-on. "
                              "Transatlantic basic economy is disputed -- "
                              "confirm on the booking page."),
    "AS": CarryOnPolicy("AS", "Alaska", 55.9, 35.6, 22.9,
                        size_enforcement="lenient", weight_enforcement="lenient"),
    "B6": CarryOnPolicy("B6", "JetBlue", 55.9, 35.6, 22.9,
                        size_enforcement="lenient", weight_enforcement="lenient"),

    # ---- Nordic / your priority routes ----
    # CORRECTED 2026-09-19. This was cabin_bag_in_cheapest_fare=False with a
    # $55 fee, which is true of Finnair's INTRA-EUROPEAN Economy Light fare
    # and wrong for the transatlantic one. Every Finnair DFW-HEL-ARN
    # itinerary checked on that date returned an included cabin bag.
    # It matters more than most: Finnair is the cheapest carrier on the
    # Stockholm route, so a phantom $110 was landing on the top priority.
    "AY": CarryOnPolicy("AY", "Finnair", 55, 40, 23, max_weight_kg=8,
                        size_enforcement="lenient", weight_enforcement="moderate",
                        cabin_bag_in_cheapest_fare=True,
                        notes="Transatlantic economy includes a cabin bag; the "
                              "intra-European Light fare does not. Travelers "
                              "report no routine measuring at the gate, but "
                              "weight checks do happen and an overweight bag "
                              "costs EUR 60 to put in the hold."),
    "SK": CarryOnPolicy("SK", "SAS", 55, 40, 23, max_weight_kg=8,
                        size_enforcement="moderate", weight_enforcement="strict",
                        cabin_bag_in_cheapest_fare=False,
                        cabin_bag_fee_usd=40,
                        notes="Known for weighing bags at the gate, even in "
                              "SAS Plus. Less likely on a long-haul from the "
                              "US than within Europe. Usually gate-checks "
                              "free rather than charging."),
    "DY": CarryOnPolicy("DY", "Norwegian", 55, 40, 23, max_weight_kg=10,
                        size_enforcement="moderate", weight_enforcement="moderate",
                        cabin_bag_in_cheapest_fare=False,
                        cabin_bag_fee_usd=35),
    # CORRECTED 2026-09-19, same error as Finnair: their Economy Light fare
    # charges for a cabin bag on INTRA-EUROPEAN routes, not transatlantic.
    # Checked against live ORD-KEF nonstops on two separate January and
    # February 2027 date pairs -- both returned an included cabin bag.
    "FI": CarryOnPolicy("FI", "Icelandair", 55, 40, 23, max_weight_kg=10,
                        size_enforcement="moderate", weight_enforcement="moderate",
                        cabin_bag_in_cheapest_fare=True,
                        notes="Transatlantic economy includes a cabin bag; the "
                              "intra-European Light fare does not."),

    # ---- European full-service ----
    "LH": CarryOnPolicy("LH", "Lufthansa", 55, 40, 23, max_weight_kg=8,
                        size_enforcement="lenient", weight_enforcement="moderate",
                        notes="Checks by sight, and announces beforehand so "
                              "you can volunteer the bag rather than be caught."),
    "LX": CarryOnPolicy("LX", "Swiss", 55, 40, 23, max_weight_kg=8,
                        size_enforcement="moderate", weight_enforcement="moderate"),
    "OS": CarryOnPolicy("OS", "Austrian", 55, 40, 23, max_weight_kg=8,
                        size_enforcement="moderate", weight_enforcement="moderate"),
    "KL": CarryOnPolicy("KL", "KLM", 55, 35, 25, max_weight_kg=12,
                        size_enforcement="lenient", weight_enforcement="lenient"),
    "AF": CarryOnPolicy("AF", "Air France", 55, 35, 25, max_weight_kg=12,
                        size_enforcement="lenient", weight_enforcement="lenient"),
    "BA": CarryOnPolicy("BA", "British Airways", 56, 45, 25,
                        size_enforcement="lenient", weight_enforcement="lenient",
                        notes="The most permissive major in Europe -- generous "
                              "sizer and staff who rarely enforce it."),
    "IB": CarryOnPolicy("IB", "Iberia", 56, 45, 25,
                        size_enforcement="lenient", weight_enforcement="lenient"),
    "EI": CarryOnPolicy("EI", "Aer Lingus", 55, 40, 24, max_weight_kg=10,
                        size_enforcement="strict", weight_enforcement="moderate",
                        notes="Inspects cabin bags at the boarding gate."),
    "EW": CarryOnPolicy("EW", "Eurowings", 55, 40, 23, max_weight_kg=8,
                        size_enforcement="strict", weight_enforcement="moderate",
                        cabin_bag_in_cheapest_fare=False, cabin_bag_fee_usd=35,
                        notes="Sizer at every gate; EUR 50 per oversize piece. "
                              "Size matters more to them than weight."),
    "TP": CarryOnPolicy("TP", "TAP Air Portugal", 55, 40, 20, max_weight_kg=8,
                        size_enforcement="moderate", weight_enforcement="moderate"),
    "TK": CarryOnPolicy("TK", "Turkish", 55, 40, 23, max_weight_kg=8,
                        size_enforcement="moderate", weight_enforcement="moderate"),
    "LO": CarryOnPolicy("LO", "LOT", 55, 40, 23, max_weight_kg=8,
                        size_enforcement="moderate", weight_enforcement="moderate"),

    # ---- Low-cost: this is where a stopover hop actually gets you caught ----
    "FR": CarryOnPolicy("FR", "Ryanair", 55, 40, 20, max_weight_kg=10,
                        size_enforcement="strict", weight_enforcement="moderate",
                        cabin_bag_in_cheapest_fare=False,
                        cabin_bag_fee_usd=30,
                        notes="Sizers at the gate, used routinely. Priority "
                              "required for a full cabin bag; the free "
                              "allowance is 40x30x20 under the seat."),
    "W6": CarryOnPolicy("W6", "Wizz Air", 55, 40, 23, max_weight_kg=10,
                        size_enforcement="strict", weight_enforcement="moderate",
                        cabin_bag_in_cheapest_fare=False, cabin_bag_fee_usd=35),
    "U2": CarryOnPolicy("U2", "easyJet", 56, 45, 25,
                        size_enforcement="lenient", weight_enforcement="lenient",
                        cabin_bag_in_cheapest_fare=False, cabin_bag_fee_usd=30,
                        notes="Does not routinely weigh cabin bags. Essential "
                              "fare is personal item only."),
    "VY": CarryOnPolicy("VY", "Vueling", 55, 40, 20, max_weight_kg=10,
                        size_enforcement="strict", weight_enforcement="moderate",
                        cabin_bag_in_cheapest_fare=False, cabin_bag_fee_usd=30),
}

# Used when the airline code isn't recognised. Deliberately the common
# European standard rather than something permissive, so an unknown carrier
# is treated as a risk rather than waved through.
DEFAULT_POLICY = CarryOnPolicy(
    "??", "Unknown carrier", 55, 40, 23, max_weight_kg=8,
    size_enforcement="moderate", weight_enforcement="moderate",
    cabin_bag_in_cheapest_fare=False, cabin_bag_fee_usd=45,
    notes="Airline not in the policy table -- assumed European standard.",
)


def policy_for(airline_code: str) -> CarryOnPolicy:
    return POLICIES.get((airline_code or "").upper().strip(), DEFAULT_POLICY)


# Placeholders Google Travel Explore uses when an itinerary is operated by
# more than one carrier, plus our own fallback. None of these is an airline,
# so none of them can be priced.
UNKNOWN_CARRIER_CODES = {"MULTI", "??", ""}

UNKNOWN_BAG_NOTE = (
    "Operating airline not identified, so the carry-on cost is NOT included "
    "in the price above. Most transatlantic economy fares include a cabin "
    "bag; the cheapest basic-economy fares do not. Check before booking."
)

BASIC_ECONOMY_NOTE = (
    "{names}: ordinary economy includes a cabin bag, but their cheapest "
    "transatlantic fare is basic economy, which allows a personal item only. "
    "If this is that fare, add roughly $70-100 each way for a carry-on."
)


@dataclass
class BagAssessment:
    """What carrying this bag does to a specific itinerary."""

    fee_usd: float = 0.0
    fits_everywhere: bool = True
    worst_airline: str = ""
    warnings: List[str] = field(default_factory=list)
    per_airline: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    risk_score: float = 0.0
    # True when at least one operating carrier is unknown (Explore reports
    # "multi" for most mixed itineraries), so the bag cost cannot be priced.
    unknown_bag_carriers: bool = False
    # Carriers that include a cabin bag in ordinary economy but sell a
    # transatlantic basic-economy fare that does not.
    maybe_basic_economy: List[str] = field(default_factory=list)

    @property
    def risk(self) -> str:
        """none | low | tight | oversize -- the practical chance of hassle.

        Driven by enforcement behaviour, not by centimetres alone. A bag 3cm
        over on Finnair (which travelers report does not measure) is rated
        below the same bag on Ryanair (sizer at every gate).
        """
        if self.risk_score <= 0.01:
            return "none"
        if self.risk_score < 0.20:
            return "low"
        if self.risk_score < 0.45:
            return "tight"
        return "oversize"


class BaggageAdvisor:
    """Applies a BagSpec to an itinerary: what it costs, and what might not fit."""

    def __init__(
        self,
        bag: BagSpec,
        fee_overrides: Optional[Dict[str, float]] = None,
        included_overrides: Optional[Dict[str, bool]] = None,
        assume_fee_usd: float = 45.0,
        packed_weight_kg: Optional[float] = None,
    ):
        self.bag = bag
        self.packed_weight_kg = packed_weight_kg
        self.fee_overrides = {k.upper(): v for k, v in (fee_overrides or {}).items()}
        self.included_overrides = {
            k.upper(): v for k, v in (included_overrides or {}).items()
        }
        self.assume_fee_usd = assume_fee_usd

    def _policy(self, code: str) -> CarryOnPolicy:
        p = policy_for(code)
        code_u = (code or "").upper().strip()
        if code_u in self.fee_overrides or code_u in self.included_overrides:
            p = CarryOnPolicy(**{**p.__dict__})
            if code_u in self.fee_overrides:
                p.cabin_bag_fee_usd = self.fee_overrides[code_u]
            if code_u in self.included_overrides:
                p.cabin_bag_in_cheapest_fare = self.included_overrides[code_u]
        return p

    def assess(
        self,
        airlines: List[str],
        segments: int = 2,
    ) -> BagAssessment:
        """Fees and fit risk across every carrier on the itinerary.

        segments is the number of one-way flights: 2 for a normal round trip,
        3 for a stitched stopover. The cabin-bag fee is charged on each.
        """
        result = BagAssessment()
        unique = [a for a in dict.fromkeys(a for a in airlines if a)]
        if not unique:
            unique = ["??"]

        total_fee = 0.0
        worst_overage = 0.0

        # Spread the segments across the operating carriers, so a mixed
        # itinerary pays each airline's own fee for the legs it operates.
        segments = max(1, segments)
        fee_share = segments / len(unique)

        worst_risk = 0.0

        for code in unique:
            p = self._policy(code)
            fits, problems = p.fits(self.bag)
            overage = 0.0
            for prob in problems:
                try:
                    overage = max(overage, float(prob.split("+")[1].rstrip(")")))
                except (IndexError, ValueError):
                    pass

            if code.upper() in UNKNOWN_CARRIER_CODES or code not in POLICIES:
                # We do not know who is flying this, so we do not know what
                # the bag costs. Google Explore reports "multi" for any
                # itinerary with two operating carriers, which is most of
                # them -- inventing a fee here would put a fabricated number
                # into the all-in price and silently suppress real deals.
                #
                # Charge nothing, and say so in the email instead. See
                # UNKNOWN_BAG_NOTE.
                result.unknown_bag_carriers = True
            elif not p.cabin_bag_in_cheapest_fare:
                fee = p.cabin_bag_fee_usd or self.assume_fee_usd
                total_fee += fee * fee_share
            elif p.long_haul_basic_economy_strips_bag:
                # The airline includes a cabin bag in its ordinary economy
                # fare, but sells a cheaper transatlantic basic-economy fare
                # that does not. Verified 2026-09-19: the cheapest United
                # DFW-LHR round trip returns a personal item only.
                #
                # The cheapest fare on a deal-hunting bot is exactly the one
                # most likely to be that fare -- but the fare class is not in
                # the data, so this is a warning, not a charge.
                result.maybe_basic_economy.append(p.name)

            size_risk = p.size_risk_score(self.bag)
            weight_risk = p.weight_risk_score(self.bag, self.packed_weight_kg)
            combined = max(size_risk, weight_risk)
            worst_risk = max(worst_risk, combined)

            headroom = p.weight_headroom_kg(self.bag)
            result.per_airline[code] = {
                "airline": p.name,
                "limit_cm": f"{p.max_h_cm:.0f}x{p.max_w_cm:.0f}x{p.max_d_cm:.0f}",
                "fits": fits,
                "problems": problems,
                "max_overage_cm": round(overage, 1),
                "weight_headroom_kg": headroom,
                "size_enforcement": p.size_enforcement,
                "weight_enforcement": p.weight_enforcement,
                "size_risk": size_risk,
                "weight_risk": weight_risk,
                "risk": round(combined, 3),
                "bag_included": p.cabin_bag_in_cheapest_fare,
            }

            if not fits:
                result.fits_everywhere = False
                if overage >= worst_overage:
                    worst_overage = overage
                    result.worst_airline = p.name

            # Only warn where it's actually likely to bite. A carrier that
            # never measures doesn't need a scary warning for 3cm.
            if size_risk >= 0.25:
                result.warnings.append(
                    f"{p.name}: {overage:.1f}cm over the "
                    f"{p.max_h_cm:.0f}x{p.max_w_cm:.0f}x{p.max_d_cm:.0f}cm limit, "
                    f"and they enforce size ({p.size_enforcement})"
                )
            elif not fits and p.size_enforcement == "lenient":
                result.warnings.append(
                    f"{p.name}: {overage:.1f}cm over on paper, but travelers "
                    f"report they rarely measure -- low risk"
                )

            if weight_risk >= 0.25:
                result.warnings.append(
                    f"{p.name}: over the {p.max_weight_kg:.0f}kg cabin limit "
                    f"and they weigh bags ({p.weight_enforcement})"
                )
            elif headroom is not None and headroom <= 5.5:
                result.warnings.append(
                    f"{p.name}: {p.max_weight_kg:.0f}kg cap leaves only "
                    f"{headroom:.1f}kg ({headroom / KG_PER_LB:.0f} lb) to pack"
                )

        # Uncertainty the bot cannot price goes into the email, where it can
        # be acted on -- never into the price, where it would silently move
        # every fare past the ceiling.
        if result.unknown_bag_carriers:
            result.warnings.append(UNKNOWN_BAG_NOTE)
        if result.maybe_basic_economy:
            result.warnings.append(
                BASIC_ECONOMY_NOTE.format(
                    names=", ".join(sorted(set(result.maybe_basic_economy)))
                )
            )

        result.fee_usd = round(total_fee, 2)
        result.risk_score = round(worst_risk, 3)
        return result
