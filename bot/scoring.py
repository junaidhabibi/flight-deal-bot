"""Decide what counts as heavily discounted, and what deserves an email.

The honest problem: no public API sells "lowest price this route has ever
been." Google shows price history in its UI but only exposes a partial
series, and the fare caches only know the last 48 hours. So the bot builds
its own record book -- every observation from every run goes into SQLite --
and layers three reference prices on top:

  1. Google's typical range (via SerpApi: fetched by the calibration step
                             and stored per route, or live on a verified
                             candidate)
  2. Its own history        (the 10th percentile, once there are enough
                             observations)
  3. Your seeded baseline   (config.yml, used on day one)

Google's range wins when it is fresh, because it is the only one of the
three that is not derived from the bot's own recent observations. The old
rule -- lowest credible reference wins -- sounded conservative but had a
failure mode that produced nothing at all: after a week of flat prices the
bot's own 10th percentile IS the current price, so "18% below it" can only
ever be an error fare, and a genuine seasonal low on a route that has been
expensive all year is invisible. Between history and baseline, the lowest
still wins, for the same reason as before: a hand-typed baseline must not
be allowed to manufacture a discount out of thin air.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import Config
from .history import History
from .models import Deal, tier_rank
from .sources.serpapi_verify import PriceInsight

log = logging.getLogger(__name__)


@dataclass
class Reference:
    price: float
    basis: str          # history | google_typical | baseline
    observations: int = 0
    record_price: Optional[float] = None


class DealScorer:
    def __init__(self, config: Config, history: History):
        self.cfg = config
        self.hist = history
        self.t = config.thresholds

    # ---------- reference price ----------

    def reference_for(
        self,
        deal: Deal,
        insight: Optional[PriceInsight] = None,
        exclude_recent_seconds: int = 0,
    ) -> Reference:
        """The price this fare has to beat.

        Benchmarks come from the fare's own price series (see
        Deal.history_key): stopover itineraries are compared against other
        stopover itineraries, not against normal round trips.
        """
        window = self.t.get("history_window_days") or None
        stats = self.hist.route_stats(deal.history_key, window_days=window)
        count = int(stats["count"] or 0)
        min_obs = int(self.t.get("min_observations_for_history", 8))

        # 1. Google's own idea of typical. Live from a verification, or the
        #    stored sample the calibration step keeps per route. If present
        #    and fresh it IS the reference -- see the module docstring.
        insight = insight or self.stored_insight(deal)
        if insight and insight.typical_low:
            ref = Reference(
                price=float(insight.typical_low),
                basis="google_typical",
                observations=insight.history_points,
                record_price=insight.history_min,
            )
            # Our own all-time min is still the record bar, and the count
            # says how much of our own data backs the email.
            if stats["min"]:
                ref.record_price = float(stats["min"])
            ref.observations = max(ref.observations, count)
            return ref

        candidates: List[Reference] = []

        # 2. Our own history. Use the 10th percentile as "a good price" --
        #    the all-time min is the record bar, not the normal-price bar.
        if count >= min_obs and stats["p10"]:
            candidates.append(
                Reference(
                    price=float(stats["p10"]),
                    basis="history",
                    observations=count,
                    record_price=float(stats["min"]) if stats["min"] else None,
                )
            )

        # 3. The seeded baseline from config -- home origin only.
        baseline = self._baseline_for(deal)
        if baseline:
            candidates.append(
                Reference(price=float(baseline), basis="baseline", observations=0)
            )

        if not candidates:
            # Nothing real to compare against. The ceiling is still used so
            # the fare gets a number and can be ranked, but the basis is
            # recorded honestly -- passes_filters refuses to alert on it.
            #
            # Comparing a fare to a ceiling manufactures a discount out of a
            # preference. It is how a routine $393 ORD->KEF fare came out as
            # "37% off": no Chicago history, no Chicago baseline, so it was
            # measured against "the most Junaid would pay" and looked like a
            # bargain against it.
            return Reference(
                price=float(self.t.get("max_price_usd", 500)), basis="ceiling"
            )

        # Toughest (lowest) benchmark wins.
        best = min(candidates, key=lambda r: r.price)

        # Carry the all-time min from history even when another basis was
        # chosen as the reference -- record detection needs it.
        if best.record_price is None and stats["min"]:
            best.record_price = float(stats["min"])
        best.observations = max(best.observations, count)
        return best

    def stored_insight(self, deal: Deal) -> Optional[PriceInsight]:
        """Google's stored typical range for this deal's route, if fresh.

        Only for a plain round trip. Google's range describes the city pair
        as one product; a stitched stopover (history_key != route) is priced
        from separate one-ways and is systematically cheaper, so measuring
        it against the round-trip range would flatter every stopover.
        """
        if deal.history_key != deal.route:
            return None
        cal = self.cfg.sources.get("serpapi", {}).get("calibration", {}) or {}
        max_age = float(cal.get("max_age_days", 14))
        row = self.hist.route_insight(deal.route, max_age_days=max_age)
        if not row:
            return None
        return PriceInsight(
            lowest_price=row.get("lowest_price"),
            price_level=row.get("price_level") or "",
            typical_low=row.get("typical_low"),
            typical_high=row.get("typical_high"),
            history_min=row.get("history_min"),
            history_points=int(row.get("history_points") or 0),
        )

    # ---------- scoring ----------

    def score(
        self,
        deal: Deal,
        insight: Optional[PriceInsight] = None,
        exclude_recent_seconds: int = 0,
    ) -> Deal:
        """Fill in discount, tier, record status and priority score."""
        # A live insight comes from verifying THIS fare on Google just now.
        # A stored one is the route's calibration sample, days old. Both
        # can set the benchmark; only the live one may claim "verified".
        live = insight
        insight = insight or self.stored_insight(deal)

        ref = self.reference_for(deal, insight, exclude_recent_seconds)
        deal.reference_price = ref.price
        deal.reference_basis = ref.basis
        deal.observations = ref.observations

        price = deal.total_price_usd
        discount = (ref.price - price) / ref.price * 100 if ref.price else 0.0
        deal.discount_pct = round(discount, 1)

        self._set_typical_price(deal, insight)

        # Record check against what was seen before this run, within the
        # history window. The window is what stops the bar ratcheting down
        # permanently -- see History.route_min.
        prior_min = self.hist.route_min(
            deal.history_key,
            exclude_last_seconds=exclude_recent_seconds,
            window_days=self.t.get("history_window_days") or None,
        )
        if prior_min is None and ref.record_price:
            prior_min = ref.record_price
        deal.previous_record = prior_min

        tol = float(self.t.get("record_tolerance_pct", 2.0))
        if prior_min is None:
            # Never seen this route. Can't claim a record; let the discount
            # vs. baseline carry it, and say so in the email.
            deal.is_record = False
            deal.notes.append("First observation for this route -- no history yet.")
        else:
            deal.is_record = price <= prior_min * (1 + tol / 100)

        # Google cross-check, when we have it.
        if insight:
            if live:
                deal.verified_by = "serpapi"
                if live.price_level:
                    deal.notes.append(
                        f"Google rates this price level: {live.price_level}"
                    )
                if live.best_offer and live.best_offer > price * 1.25:
                    deal.notes.append(
                        f"Google's cheapest bookable right now is "
                        f"${live.best_offer:,.0f} -- cached fare may be gone"
                    )
            beats = insight.beats_history(price, tol)
            if beats is False:
                deal.notes.append(
                    f"Google has seen this route at ${insight.history_min:,.0f}"
                )
            elif beats is True:
                deal.notes.append("At or below the lowest price in Google's history")
            # Say how much of OUR data sits behind the comparison, so a
            # persistent price reads as what it is.
            if deal.observations and deal.previous_record is not None:
                deal.notes.append(
                    f"Cheapest this bot has seen in {deal.observations} scans: "
                    f"${deal.previous_record:,.0f}"
                )

        deal.tier = self._tier(deal.discount_pct)
        deal.score = self._priority(deal)

        # score() runs twice for any deal that gets verified -- once before
        # the Google cross-check and once after -- and both passes append
        # notes. The result was that the deals given the MOST scrutiny were
        # the ones whose emails repeated themselves. Order-preserving, so
        # the first occurrence keeps its position.
        deal.notes = list(dict.fromkeys(deal.notes))
        return deal

    def _set_typical_price(
        self, deal: Deal, insight: Optional[PriceInsight] = None
    ) -> None:
        """What this route normally costs, for the side-by-side in the email.

        Distinct from reference_price on purpose. The reference is the
        toughest bar a fare has to clear (a 10th-percentile price); quoting
        that as "normal" would understate what you'd actually have paid.
        The median is the honest answer to "what does this usually run?".

        Google's range comes first when we have it. The bot's own median
        used to, but with a short history that median IS the current price:
        the email would say "$567 -- normally $567" and the discount line
        beside it would say 25% off, computed against a benchmark the
        reader could not see. The quoted "normal" and the benchmark must
        be the same number, or the comparison is not honest.
        """
        window = self.t.get("history_window_days") or None
        stats = self.hist.route_stats(deal.history_key, window_days=window)
        count = int(stats["count"] or 0)
        min_obs = int(self.t.get("min_observations_for_history", 8))

        if insight and insight.typical_low and insight.typical_high:
            deal.typical_low = insight.typical_low
            deal.typical_high = insight.typical_high
            deal.typical_price = round(
                (insight.typical_low + insight.typical_high) / 2, 2
            )
            deal.typical_basis = (
                f"Google's typical range "
                f"(${insight.typical_low:,.0f}-${insight.typical_high:,.0f})"
            )
            if count:
                deal.typical_basis += (
                    f"; this bot's median over {count} scans is "
                    f"${float(stats['median']):,.0f}"
                )
            return

        if count >= min_obs and stats["median"]:
            deal.typical_price = round(float(stats["median"]), 2)
            deal.typical_basis = f"median of {count} observations"
            return

        baseline = self._baseline_for(deal)
        if baseline:
            deal.typical_price = float(baseline)
            deal.typical_basis = "your configured baseline"

    def _baseline_for(self, deal) -> Optional[float]:
        """The configured baseline, but ONLY for the origin it was measured from.

        Every baseline_usd in config.yml came from a Dallas sweep. Applying a
        Dallas number to a Chicago fare compares two different markets and
        invents a discount out of the difference between them.

        The case that caught this: ORD->KEF at $393 was scored 40% off, on a
        $815 baseline derived from DFW->KEF. Chicago has an Icelandair
        nonstop and Dallas does not, so $393 is simply Chicago's ordinary
        winter fare -- confirmed identical three weeks apart. The bot's first
        email would have called a routine price EXCEPTIONAL.

        Other origins therefore get no baseline at all and must build their
        own per-pair history first (history_key is already origin-specific).
        They stay quiet for a couple of weeks, which is the correct answer
        when there is genuinely nothing to compare against.
        """
        home = str(self.t.get("baseline_origin", "DFW")).upper()
        if home and deal.origin.upper() != home:
            return None
        return self.cfg.baseline(deal.destination)

    def _tier(self, discount_pct: float) -> str:
        tiers = self.t["tiers"]
        if discount_pct >= tiers["insane"]:
            return "insane"
        if discount_pct >= tiers["great"]:
            return "great"
        if discount_pct >= tiers["good"]:
            return "good"
        if discount_pct >= tiers["watch"]:
            return "watch"
        return ""

    def _priority(self, deal: Deal) -> float:
        """Ranking score. Higher sorts first in the email."""
        score = float(deal.discount_pct or 0)
        score *= self.cfg.priority(deal.destination)
        score *= self.cfg.origin_weight(deal.origin)
        if deal.stopover_code:
            score *= self.cfg.hub_appeal(deal.stopover_code)
            score *= 1.10  # you asked for these, so nudge them up
        if deal.is_record:
            score *= 1.25
        if deal.verified_by:
            score *= 1.10
        # Prefer carriers that won't give you grief over the bag. Weighted by
        # how strictly each airline actually enforces, not by centimetres --
        # 3cm over on a carrier that never measures barely matters.
        score *= {
            "none": 1.15,
            "low": 1.10,
            "tight": 0.95,
            "oversize": 0.80,
        }.get(deal.bag_risk, 1.0)
        return round(score, 2)

    # ---------- gates ----------

    def passes_filters(self, deal: Deal) -> tuple[bool, str]:
        """Final yes/no before a deal is allowed anywhere near your inbox."""
        max_price = float(self.t.get("max_price_usd", 10**9))
        if deal.total_price_usd > max_price:
            return False, f"above ${max_price:,.0f} ceiling"

        min_disc = float(self.t.get("min_discount_pct", 0))
        if (deal.discount_pct or 0) < min_disc:
            return False, f"discount {deal.discount_pct}% < {min_disc}%"

        if not deal.tier:
            return False, "below watch tier"

        if deal.reference_basis == "ceiling":
            # No history and no baseline for this origin-destination pair, so
            # there is no honest "normally costs" to put in the email -- and
            # that comparison is the whole point of the alert.
            return False, "no price history for this route yet"

        if self.t.get("skip_oversize_bag_routes", False) and deal.bag_risk == "oversize":
            return False, "carry-on won't fit this carrier"

        if self.t.get("require_record", True) and not deal.is_record:
            # Exemption: a fare in the top tier is extraordinary on its face.
            # Without this, the bot is silent on its very first runs, because
            # a route with no history can't produce a record -- and a $238
            # Stockholm fare shouldn't be dropped just because the bot is new.
            no_history = deal.previous_record is None
            floor = self.t.get("no_history_alert_tier", "great")
            if no_history and tier_rank(deal.tier) >= tier_rank(floor):
                vs = {"google_typical": "Google's typical range",
                      "baseline": "your baseline"}.get(deal.reference_basis,
                                                       "the benchmark")
                deal.notes.append(
                    "No price history for this route yet -- alerting anyway "
                    f"because the discount vs. {vs} is extreme."
                )
                return True, ""
            return False, "not a record fare"

        return True, ""

    def should_alert_now(self, deal: Deal) -> bool:
        """Instant email, or hold for the digest?"""
        threshold = self.t.get("instant_alert_tier", "great")
        return tier_rank(deal.tier) >= tier_rank(threshold)

    # ---------- verification budget ----------

    def verification_candidates(
        self, deals: List[Deal], limit: int
    ) -> List[Deal]:
        """Which candidates are worth spending a SerpApi search on.

        Only fares that already look like records, best first, so the free
        250/month goes to the ones that would actually wake you up.
        """
        worthy = [
            d
            for d in deals
            if d.is_record and tier_rank(d.tier) >= tier_rank("good")
        ]
        worthy.sort(key=lambda d: d.score, reverse=True)

        # Don't spend two searches on the same route in one run.
        seen_routes = set()
        out: List[Deal] = []
        for d in worthy:
            if d.route in seen_routes:
                continue
            seen_routes.add(d.route)
            out.append(d)
            if len(out) >= limit:
                break
        return out
