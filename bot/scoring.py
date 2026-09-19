"""Decide what counts as heavily discounted, and what deserves an email.

The honest problem: no public API sells "lowest price this route has ever
been." Google shows price history in its UI but only exposes a partial
series, and the fare caches only know the last 48 hours. So the bot builds
its own record book -- every observation from every run goes into SQLite --
and layers three independent reference prices on top:

  1. Its own history        (best, once there are enough observations)
  2. Google's typical range (via SerpApi, on verified candidates)
  3. Your seeded baseline   (config.yml, used on day one)

The reference used is the LOWEST credible one, which makes the discount
figure conservative: a fare has to beat the toughest benchmark available,
not the most flattering one.
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
        stats = self.hist.route_stats(deal.history_key)
        count = int(stats["count"] or 0)
        min_obs = int(self.t.get("min_observations_for_history", 8))

        candidates: List[Reference] = []

        # 1. Our own history. Use the 10th percentile as "a good price" --
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

        # 2. Google's own idea of typical, when we bothered to verify.
        if insight and insight.typical_low:
            candidates.append(
                Reference(
                    price=float(insight.typical_low),
                    basis="google_typical",
                    observations=insight.history_points,
                    record_price=insight.history_min,
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

    # ---------- scoring ----------

    def score(
        self,
        deal: Deal,
        insight: Optional[PriceInsight] = None,
        exclude_recent_seconds: int = 0,
    ) -> Deal:
        """Fill in discount, tier, record status and priority score."""
        ref = self.reference_for(deal, insight, exclude_recent_seconds)
        deal.reference_price = ref.price
        deal.reference_basis = ref.basis
        deal.observations = ref.observations

        price = deal.total_price_usd
        discount = (ref.price - price) / ref.price * 100 if ref.price else 0.0
        deal.discount_pct = round(discount, 1)

        self._set_typical_price(deal, insight)

        # Record check against everything seen before this run.
        prior_min = self.hist.route_min(
            deal.history_key, exclude_last_seconds=exclude_recent_seconds
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
            deal.verified_by = "serpapi"
            if insight.price_level:
                deal.notes.append(f"Google rates this price level: {insight.price_level}")
            beats = insight.beats_history(price, tol)
            if beats is False:
                deal.notes.append(
                    f"Google has seen this route at ${insight.history_min:,.0f}"
                )
            elif beats is True:
                deal.notes.append("At or below the lowest price in Google's history")
            if insight.best_offer and insight.best_offer > price * 1.25:
                deal.notes.append(
                    f"Google's cheapest bookable right now is "
                    f"${insight.best_offer:,.0f} -- cached fare may be gone"
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

        Preference order is most-specific-first here, the opposite of the
        benchmark logic: real observations on this exact route beat Google's
        route-level range, which beats a hand-entered baseline.
        """
        stats = self.hist.route_stats(deal.history_key)
        count = int(stats["count"] or 0)
        min_obs = int(self.t.get("min_observations_for_history", 8))

        if insight and insight.typical_low and insight.typical_high:
            deal.typical_low = insight.typical_low
            deal.typical_high = insight.typical_high

        if count >= min_obs and stats["median"]:
            deal.typical_price = round(float(stats["median"]), 2)
            deal.typical_basis = f"median of {count} observations"
            return

        if insight and insight.typical_low and insight.typical_high:
            deal.typical_price = round(
                (insight.typical_low + insight.typical_high) / 2, 2
            )
            deal.typical_basis = (
                f"Google's typical range "
                f"(${insight.typical_low:,.0f}-${insight.typical_high:,.0f})"
            )
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
                deal.notes.append(
                    "No price history for this route yet -- alerting anyway "
                    "because the discount vs. your baseline is extreme."
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
