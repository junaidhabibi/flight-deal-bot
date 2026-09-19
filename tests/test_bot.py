"""End-to-end tests on mock data. No network, no API keys needed.

Run: python -m tests.test_bot   (from the project root)
"""

from __future__ import annotations

import os
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.baggage import (
    POLICIES,
    BaggageAdvisor,
    BagSpec,
    CarryOnPolicy,
    policy_for,
)
from bot.config import Config, ConfigError, load_env_file
from bot.emailer import build_ssl_context
from bot.history import History
from bot.models import Deal, Leg
from bot.scoring import DealScorer
from bot.sources.rss_deals import RSSDealWatcher, extract_price
from bot.sources.serpapi_verify import PriceInsight, SerpApiVerifier
from bot.sources.travelpayouts import TravelpayoutsClient, months_ahead
from bot.layovers import (
    DEAD_ZONE,
    QUICK,
    STOPOVER,
    TOO_LONG,
    LayoverRules,
    assess_api_layover,
    spans_night,
    estimate_layover_hours,
    great_circle_km,
)
from bot.stopovers import StopoverBuilder, verify_layover_rule


def leg_row(dep: datetime, price: float, duration: int = 600, transfers: int = 0):
    return {
        "price": price,
        "departure_at": dep,
        "airline": "AY",
        "flight_number": "20",
        "transfers": transfers,
        "duration": duration,
        "link": "/search/TEST",
        "raw": {"link": "/search/TEST", "price": price},
    }


# ======================================================================
#  The layover rule -- the requirement that matters most
# ======================================================================


class TestLayoverRule(unittest.TestCase):
    def setUp(self):
        self.builder = StopoverBuilder(
            LayoverRules(quick_max_hours=5, overnight_min_hours=24,
                         overnight_max_hours=120),
            min_nights=5, max_nights=21,
        )

    def test_rejects_short_connection(self):
        """A 3-hour connection must never survive."""
        depart = datetime(2026, 11, 2, 17, 0)
        leg_a = {depart.date(): leg_row(depart, 300, duration=600)}
        # A arrives 03:00 Nov 3; B departs 06:00 Nov 3 = 3h gap.
        b_dep = datetime(2026, 11, 3, 6, 0)
        leg_b = {b_dep.date(): leg_row(b_dep, 90, duration=90)}

        pairs = self.builder.pair_legs(leg_a, leg_b)
        self.assertEqual(pairs, [], "3-hour connection was not rejected")

    def test_accepts_two_day_stopover(self):
        depart = datetime(2026, 11, 2, 17, 0)
        leg_a = {depart.date(): leg_row(depart, 300, duration=600)}
        b_dep = datetime(2026, 11, 5, 9, 0)  # ~54h after arrival
        leg_b = {b_dep.date(): leg_row(b_dep, 90, duration=90)}

        pairs = self.builder.pair_legs(leg_a, leg_b)
        self.assertEqual(len(pairs), 1)
        _, _, hours = pairs[0]
        self.assertGreater(hours, 24)
        self.assertLess(hours, 120)

    def test_rejects_overlong_stopover(self):
        depart = datetime(2026, 11, 2, 17, 0)
        leg_a = {depart.date(): leg_row(depart, 300)}
        b_dep = datetime(2026, 11, 20, 9, 0)  # ~18 days
        leg_b = {b_dep.date(): leg_row(b_dep, 90)}
        self.assertEqual(self.builder.pair_legs(leg_a, leg_b), [])

    def test_boundary_just_under_24h(self):
        """23.5 hours must fail; the rule is 'longer than a day'."""
        depart = datetime(2026, 11, 2, 12, 0)
        leg_a = {depart.date(): leg_row(depart, 300, duration=600)}  # arr 22:00
        b_dep = datetime(2026, 11, 3, 21, 30)  # 23.5h later
        leg_b = {b_dep.date(): leg_row(b_dep, 90)}
        self.assertEqual(self.builder.pair_legs(leg_a, leg_b), [])

    def test_boundary_just_over_24h(self):
        depart = datetime(2026, 11, 2, 12, 0)
        leg_a = {depart.date(): leg_row(depart, 300, duration=600)}  # arr 22:00
        b_dep = datetime(2026, 11, 3, 23, 0)  # 25h later
        leg_b = {b_dep.date(): leg_row(b_dep, 90)}
        self.assertEqual(len(self.builder.pair_legs(leg_a, leg_b)), 1)

    def test_rules_require_a_real_dead_zone(self):
        """An overnight minimum at or below the quick ceiling is meaningless."""
        with self.assertRaises(ValueError):
            LayoverRules(quick_max_hours=5, overnight_min_hours=5)
        with self.assertRaises(ValueError):
            LayoverRules(quick_max_hours=10, overnight_min_hours=6)

    def test_final_verification_catches_bad_deal(self):
        """The independent re-check must catch a short gap."""
        bad = Deal(
            origin="DFW",
            destination="ARN",
            destination_city="Stockholm",
            price_usd=400,
            depart_date=date(2026, 11, 2),
            stopover_code="HEL",
            stopover_city="Helsinki",
            stopover_hours=48,  # claims 48h...
            legs=[
                Leg("DFW", "HEL", datetime(2026, 11, 2, 17, 0),
                    arrive_at=datetime(2026, 11, 3, 11, 0)),
                Leg("HEL", "ARN", datetime(2026, 11, 3, 14, 0)),  # ...but is 3h
            ],
        )
        self.assertFalse(verify_layover_rule(bad, LayoverRules()))

    def test_full_build_produces_valid_itinerary(self):
        a_dep = datetime(2026, 11, 2, 17, 0)
        b_dep = datetime(2026, 11, 5, 9, 0)
        r_dep = datetime(2026, 11, 18, 10, 0)

        deals = self.builder.build(
            origin="DFW",
            hub="HEL",
            destination="ARN",
            destination_city="Stockholm",
            hub_city="Helsinki",
            leg_a={a_dep.date(): leg_row(a_dep, 300, duration=600)},
            leg_b={b_dep.date(): leg_row(b_dep, 90, duration=60)},
            return_legs={r_dep.date(): leg_row(r_dep, 280, duration=660)},
        )
        self.assertEqual(len(deals), 1)
        d = deals[0]
        self.assertEqual(d.price_usd, 670)  # 300 + 90 + 280
        self.assertEqual(d.stopover_code, "HEL")
        self.assertGreater(d.stopover_hours, 24)
        self.assertEqual(len(d.legs), 3)
        self.assertTrue(verify_layover_rule(d, LayoverRules()))

    def test_nights_window_enforced(self):
        """A return 2 days after arrival must be rejected (min 5 nights)."""
        a_dep = datetime(2026, 11, 2, 17, 0)
        b_dep = datetime(2026, 11, 5, 9, 0)
        r_dep = datetime(2026, 11, 7, 10, 0)  # only 2 nights

        deals = self.builder.build(
            origin="DFW", hub="HEL", destination="ARN",
            destination_city="Stockholm", hub_city="Helsinki",
            leg_a={a_dep.date(): leg_row(a_dep, 300)},
            leg_b={b_dep.date(): leg_row(b_dep, 90, duration=60)},
            return_legs={r_dep.date(): leg_row(r_dep, 280)},
        )
        self.assertEqual(deals, [])


# ======================================================================
#  The two-band rule: quick connections OK, dead zone rejected
# ======================================================================


class TestTwoBandRule(unittest.TestCase):
    def setUp(self):
        self.rules = LayoverRules(
            quick_max_hours=5, overnight_min_hours=24, overnight_max_hours=120
        )

    def test_bands(self):
        cases = [
            (0.5, QUICK), (2.0, QUICK), (5.0, QUICK),
            (5.1, DEAD_ZONE), (9.0, DEAD_ZONE), (23.9, DEAD_ZONE),
            (24.0, STOPOVER), (54.0, STOPOVER), (120.0, STOPOVER),
            (121.0, TOO_LONG),
        ]
        for hours, expected in cases:
            self.assertEqual(
                self.rules.classify(hours), expected, f"{hours}h misclassified"
            )

    def test_only_quick_and_stopover_are_acceptable(self):
        self.assertTrue(self.rules.accepts(3))
        self.assertTrue(self.rules.accepts(48))
        self.assertFalse(self.rules.accepts(9))     # the dead zone
        self.assertFalse(self.rules.accepts(200))   # too long

    def test_nine_hour_frankfurt_layover_is_rejected(self):
        """The exact case this rule exists to prevent."""
        self.assertEqual(self.rules.classify(9), DEAD_ZONE)
        self.assertFalse(self.rules.accepts(9))
        self.assertIn("dead zone", self.rules.explain(9))

    def test_unknown_layover_is_not_accepted_silently(self):
        self.assertIsNone(self.rules.classify(None))
        self.assertFalse(self.rules.accepts(None))

    def test_builder_only_emits_the_overnight_band(self):
        """A 3h gap must not come back as a 'stopover'."""
        builder = StopoverBuilder(self.rules, min_nights=5, max_nights=21)
        a_dep = datetime(2026, 11, 2, 12, 0)
        b_dep = datetime(2026, 11, 2, 23, 0)   # ~1h after a 10h flight
        built = builder.build(
            origin="DFW", hub="HEL", destination="ARN",
            destination_city="Stockholm", hub_city="Helsinki",
            leg_a={a_dep.date(): leg_row(a_dep, 300, duration=600)},
            leg_b={b_dep.date(): leg_row(b_dep, 90, duration=60)},
        )
        self.assertEqual(built, [])


class TestOvernightStops(unittest.TestCase):
    """14-24h counts only when it genuinely spans a night."""

    def setUp(self):
        self.rules = LayoverRules(
            quick_max_hours=5,
            overnight_min_hours=14,
            overnight_max_hours=120,
            require_night_below_hours=24,
        )

    def test_evening_to_morning_is_a_stopover(self):
        """18:00 -> 08:00. A hotel, a dinner, a morning."""
        a = datetime(2026, 11, 2, 18, 0)
        d = datetime(2026, 11, 3, 8, 0)
        self.assertTrue(spans_night(a, d))
        self.assertEqual(self.rules.classify(14, a, d), STOPOVER)
        self.assertIn("overnight", self.rules.explain(14, a, d))

    def test_same_duration_in_daytime_is_rejected(self):
        """08:00 -> 22:00. Identical 14 hours, completely different day."""
        a = datetime(2026, 11, 2, 8, 0)
        d = datetime(2026, 11, 2, 22, 0)
        self.assertFalse(spans_night(a, d))
        self.assertEqual(self.rules.classify(14, a, d), DEAD_ZONE)
        self.assertIn("doesn't span a night", self.rules.explain(14, a, d))

    def test_afternoon_to_predawn_is_rejected(self):
        """14:00 -> 04:00. Long enough, but you'd leave at 4am."""
        a = datetime(2026, 11, 2, 14, 0)
        d = datetime(2026, 11, 3, 4, 0)
        self.assertEqual(self.rules.classify(14, a, d), DEAD_ZONE)

    def test_over_24h_needs_no_night_check(self):
        """Past a full day a night is guaranteed whatever the clock says."""
        a = datetime(2026, 11, 2, 12, 0)
        d = datetime(2026, 11, 3, 14, 0)
        self.assertEqual(self.rules.classify(26, a, d), STOPOVER)
        self.assertEqual(self.rules.classify(26), STOPOVER)  # no times needed

    def test_under_14h_is_still_dead_regardless_of_night(self):
        """A 9h gap that happens to cover 01:00-06:00 is still a terminal."""
        a = datetime(2026, 11, 2, 23, 0)
        d = datetime(2026, 11, 3, 8, 0)
        self.assertTrue(spans_night(a, d))
        self.assertEqual(self.rules.classify(9, a, d), DEAD_ZONE)

    def test_unverifiable_sub_24h_stays_in_the_dead_zone(self):
        """Without clock times a 16h gap can't be confirmed as a night."""
        self.assertEqual(self.rules.classify(16), DEAD_ZONE)
        self.assertFalse(self.rules.accepts(16))

    def test_builder_accepts_a_real_overnight(self):
        builder = StopoverBuilder(self.rules, min_nights=5, max_nights=21)
        # Arrive Helsinki 18:00, leave 09:00 next day = 15h overnight.
        a_dep = datetime(2026, 11, 2, 8, 0)     # +10h flight -> 18:00
        b_dep = datetime(2026, 11, 3, 9, 0)
        r_dep = datetime(2026, 11, 18, 10, 0)
        built = builder.build(
            origin="DFW", hub="HEL", destination="ARN",
            destination_city="Stockholm", hub_city="Helsinki",
            leg_a={a_dep.date(): leg_row(a_dep, 300, duration=600)},
            leg_b={b_dep.date(): leg_row(b_dep, 90, duration=60)},
            return_legs={r_dep.date(): leg_row(r_dep, 280, duration=660)},
        )
        self.assertEqual(len(built), 1)
        self.assertAlmostEqual(built[0].stopover_hours, 15.0, places=1)
        self.assertTrue(verify_layover_rule(built[0], self.rules))

    def test_builder_rejects_a_daytime_15h_gap(self):
        builder = StopoverBuilder(self.rules, min_nights=5, max_nights=21)
        # Arrive Helsinki 06:00, leave 21:00 same day = 15h, no night.
        a_dep = datetime(2026, 11, 1, 20, 0)    # +10h flight -> 06:00
        b_dep = datetime(2026, 11, 2, 21, 0)
        r_dep = datetime(2026, 11, 18, 10, 0)
        built = builder.build(
            origin="DFW", hub="HEL", destination="ARN",
            destination_city="Stockholm", hub_city="Helsinki",
            leg_a={a_dep.date(): leg_row(a_dep, 300, duration=600)},
            leg_b={b_dep.date(): leg_row(b_dep, 90, duration=60)},
            return_legs={r_dep.date(): leg_row(r_dep, 280, duration=660)},
        )
        self.assertEqual(built, [], "a 15h daytime gap was accepted")

    def test_night_window_is_configurable(self):
        loose = LayoverRules(
            quick_max_hours=5, overnight_min_hours=14,
            night_core_start=2, night_core_end=4,
        )
        a = datetime(2026, 11, 2, 15, 0)
        d = datetime(2026, 11, 3, 5, 0)   # covers 02:00-04:00 but not 01:00-06:00
        self.assertEqual(loose.classify(14, a, d), STOPOVER)
        self.assertEqual(self.rules.classify(14, a, d), DEAD_ZONE)


class TestLayoverEstimation(unittest.TestCase):
    """Fares from the API don't report layovers, so they get estimated."""

    def setUp(self):
        self.rules = LayoverRules(
            quick_max_hours=5, overnight_min_hours=24, overnight_max_hours=120
        )

    def test_great_circle_is_sane(self):
        km = great_circle_km("DFW", "ARN")
        self.assertGreater(km, 7000)
        self.assertLess(km, 8500)
        self.assertIsNone(great_circle_km("DFW", "XXX"))

    def test_nonstop_has_no_layover(self):
        est = assess_api_layover("DFW", "ARN", 600, transfers=0, rules=self.rules)
        self.assertEqual(est.band, QUICK)
        self.assertEqual(est.hours, 0.0)
        self.assertFalse(est.is_known_bad)

    def test_real_finnair_itinerary_is_not_flagged(self):
        """Verified live: DFW-HEL-ARN, 715 min total, 45-minute connection.

        The estimate runs high because the Helsinki routing is a detour, so
        this is exactly the case the margin has to absorb.
        """
        est = assess_api_layover(
            "DFW", "ARN", total_minutes=715, transfers=1, rules=self.rules
        )
        self.assertFalse(
            est.is_known_bad,
            f"a real 45-min connection was flagged ({est.reason})",
        )

    def test_obvious_dead_zone_is_caught(self):
        """Same route, but 9 hours of extra time in the middle."""
        est = assess_api_layover(
            "DFW", "ARN", total_minutes=715 + 540, transfers=1, rules=self.rules
        )
        self.assertEqual(est.band, DEAD_ZONE)
        self.assertTrue(est.is_known_bad)

    def test_unknown_airport_yields_no_estimate(self):
        est = assess_api_layover("DFW", "ZZZ", 900, transfers=1, rules=self.rules)
        self.assertIsNone(est.hours)
        self.assertFalse(est.confident)
        self.assertFalse(est.is_known_bad)

    def test_missing_duration_yields_no_estimate(self):
        est = assess_api_layover("DFW", "ARN", None, transfers=1, rules=self.rules)
        self.assertIsNone(est.hours)
        self.assertFalse(est.is_known_bad)

    def test_margin_keeps_borderline_fares(self):
        """Just past 5h is likely detour, not waiting -- keep but flag."""
        expected = estimate_layover_hours("DFW", "ARN", 715, 1) or 0
        minutes = 715 + int((6.0 - expected) * 60)
        est = assess_api_layover(
            "DFW", "ARN", total_minutes=minutes, transfers=1, rules=self.rules
        )
        self.assertEqual(est.band, DEAD_ZONE)
        self.assertFalse(est.confident, "borderline estimate should not be acted on")


# ======================================================================
#  Record detection and scoring
# ======================================================================


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            {
                "origins": [{"code": "DFW", "name": "Dallas", "weight": 1.0}],
                "destinations": [
                    {
                        "code": "ARN",
                        "city": "Stockholm",
                        "priority": 2.0,
                        "baseline_usd": 620,
                    }
                ],
                "stopover_hubs": [{"code": "HEL", "city": "Helsinki", "appeal": 1.25}],
                "trip": {
                    "min_nights": 5,
                    "max_nights": 21,
                    "layover_hours": {"min": 24, "max": 120},
                    "max_extra_stops": 1,
                    "currency": "USD",
                },
                "thresholds": {
                    "max_price_usd": 499,
                    "min_discount_pct": 35,
                    "tiers": {"watch": 35, "good": 45, "great": 55, "insane": 65},
                    "instant_alert_tier": "great",
                    "record_tolerance_pct": 2.0,
                    "require_record": True,
                    "min_observations_for_history": 3,
                },
                "email": {"smtp_host": "h", "smtp_port": 587},
                "storage": {"db_path": f"{self.tmp.name}/test.db"},
            }
        )
        self.hist = History(self.cfg.db_path)
        self.scorer = DealScorer(self.cfg, self.hist)

    def tearDown(self):
        self.hist.close()
        self.tmp.cleanup()

    def make_deal(self, price: float, **kw) -> Deal:
        return Deal(
            origin="DFW",
            destination="ARN",
            destination_city="Stockholm",
            price_usd=price,
            depart_date=kw.pop("depart", date(2026, 11, 2)),
            return_date=kw.pop("ret", date(2026, 11, 14)),
            source="test",
            **kw,
        )

    def seed(self, prices):
        self.hist.record_observations([self.make_deal(p) for p in prices])

    def test_baseline_used_without_history(self):
        d = self.make_deal(350)
        self.scorer.score(d)
        self.assertEqual(d.reference_basis, "baseline")
        self.assertAlmostEqual(d.discount_pct, 43.5, places=0)

    def test_history_beats_baseline_when_lower(self):
        self.seed([500, 520, 540, 560, 600])
        d = self.make_deal(300)
        self.scorer.score(d)
        self.assertEqual(d.reference_basis, "history")
        self.assertLess(d.reference_price, 620)

    def test_record_detection(self):
        self.seed([480, 500, 520, 540])
        d = self.make_deal(399)
        self.scorer.score(d)
        self.assertTrue(d.is_record)
        self.assertEqual(d.previous_record, 480)

    def test_not_a_record(self):
        self.seed([300, 480, 500, 520])
        d = self.make_deal(450)
        self.scorer.score(d)
        self.assertFalse(d.is_record)

    def test_near_tie_counts_as_record(self):
        """Within the 2% tolerance, a near-tie still counts."""
        self.seed([400, 480, 500, 520])
        d = self.make_deal(407)  # 1.75% above 400
        self.scorer.score(d)
        self.assertTrue(d.is_record)

    def test_tiers(self):
        """With no history, the benchmark is the $620 baseline."""
        cases = [
            (620 * 0.80, "watch"),    # 20% off -> below the 35% floor
            (620 * 0.50, "good"),     # 50% off
            (620 * 0.40, "great"),    # 60% off
            (620 * 0.30, "insane"),   # 70% off
        ]
        for price, expected in cases:
            d = self.make_deal(price)
            self.scorer.score(d)
            if expected == "watch":
                self.assertEqual(d.tier, "", f"${price:.0f} -> {d.tier}")
            else:
                self.assertEqual(d.tier, expected, f"${price:.0f} -> {d.tier}")

    def test_price_ceiling_blocks_expensive_fare(self):
        self.seed([2000, 2100, 2200, 2300])  # pathological history
        d = self.make_deal(800)
        self.scorer.score(d)
        ok, reason = self.scorer.passes_filters(d)
        self.assertFalse(ok)
        self.assertIn("ceiling", reason)

    def test_require_record_blocks_good_but_not_record(self):
        """A 47%-off fare is still rejected if the route was once cheaper."""
        # One historic outlier at $250, the rest around $600, so the p10
        # benchmark stays high but the all-time record is low.
        self.seed([250] + [600] * 9)
        d = self.make_deal(300)
        self.scorer.score(d)
        self.assertGreater(d.discount_pct, 35)   # a real discount...
        self.assertTrue(d.tier)                  # ...that earns a tier...
        self.assertFalse(d.is_record)            # ...but isn't a record
        ok, reason = self.scorer.passes_filters(d)
        self.assertFalse(ok)
        self.assertIn("record", reason)

    def test_stockholm_priority_ranks_higher(self):
        cfg = self.cfg
        cfg.destinations.append(
            {"code": "CDG", "city": "Paris", "priority": 1.0, "baseline_usd": 620}
        )
        arn = self.make_deal(350)
        cdg = Deal(
            origin="DFW",
            destination="CDG",
            destination_city="Paris",
            price_usd=350,
            depart_date=date(2026, 11, 2),
            return_date=date(2026, 11, 14),
        )
        self.scorer.score(arn)
        self.scorer.score(cdg)
        self.assertGreater(arn.score, cdg.score)

    def test_cheap_stopovers_dont_mask_an_error_fare(self):
        """Regression: the bug the demo run exposed.

        60 normal round trips around $600, plus a batch of $383 stopover
        itineraries recorded in the same run. A $238 round trip must still
        be scored against the ~$600 round-trip series, not against $383.
        """
        self.seed([590 + (i * 7) % 140 for i in range(60)])

        stopovers = []
        for i in range(20):
            s = self.make_deal(383 + i)
            s.stopover_code = "HEL"
            s.stopover_city = "Helsinki"
            stopovers.append(s)
        self.hist.record_observations(stopovers)

        error_fare = self.make_deal(238)
        self.scorer.score(error_fare)
        self.assertGreater(
            error_fare.discount_pct, 55,
            "cheap stopovers dragged the round-trip benchmark down",
        )
        self.assertIn(error_fare.tier, ("great", "insane"))
        self.assertTrue(self.scorer.should_alert_now(error_fare))

    def test_top_tier_alerts_without_history(self):
        """Day one: no history, but an extreme fare still gets through."""
        d = self.make_deal(180)  # 71% below the $620 baseline
        self.scorer.score(d)
        self.assertEqual(d.tier, "insane")
        self.assertFalse(d.is_record)  # nothing to compare against
        ok, _ = self.scorer.passes_filters(d)
        self.assertTrue(ok, "an extreme fare was dropped for lack of history")

    def test_moderate_fare_without_history_is_held(self):
        """...but a merely-good fare with no history still waits."""
        d = self.make_deal(330)  # 47% off: good, not extraordinary
        self.scorer.score(d)
        self.assertEqual(d.tier, "good")
        ok, reason = self.scorer.passes_filters(d)
        self.assertFalse(ok)
        self.assertIn("record", reason)

    def test_typical_price_uses_median_not_the_benchmark(self):
        """'Normally costs' must be the median, not the bargain benchmark.

        reference_price is a 10th-percentile figure -- quoting that as
        'normal' would understate what the trip usually costs.
        """
        self.seed([500, 550, 600, 620, 650, 700, 750])
        d = self.make_deal(300)
        self.scorer.score(d)

        stats = self.hist.route_stats("DFW-ARN")
        self.assertEqual(d.typical_price, round(stats["median"], 2))
        self.assertGreater(d.typical_price, d.reference_price)
        self.assertIn("median of 7 observations", d.typical_basis)

    def test_savings_are_computed_against_normal(self):
        self.seed([600] * 10)
        d = self.make_deal(300)
        self.scorer.score(d)
        self.assertEqual(d.typical_price, 600)
        self.assertEqual(d.savings_usd, 300)
        self.assertEqual(d.savings_pct, 50.0)

    def test_savings_include_the_bag_fee(self):
        self.seed([600] * 10)
        d = self.make_deal(300)
        d.bag_fee_usd = 110
        self.scorer.score(d)
        self.assertEqual(d.total_price_usd, 410)
        self.assertEqual(d.savings_usd, 190)

    def test_typical_falls_back_to_google_then_baseline(self):
        # No history: Google's range is used, as the midpoint.
        d = self.make_deal(300)
        insight = PriceInsight(typical_low=500, typical_high=900)
        self.scorer.score(d, insight=insight)
        self.assertEqual(d.typical_price, 700)
        self.assertIn("Google", d.typical_basis)
        self.assertEqual(d.typical_low, 500)
        self.assertEqual(d.typical_high, 900)

        # No history and no Google: the configured baseline.
        d2 = self.make_deal(300)
        self.scorer.score(d2)
        self.assertEqual(d2.typical_price, 620)
        self.assertIn("baseline", d2.typical_basis)

    def test_history_beats_google_for_typical_price(self):
        """Opposite of the benchmark rule: for 'normal', specific wins."""
        self.seed([600] * 10)
        d = self.make_deal(300)
        self.scorer.score(d, insight=PriceInsight(typical_low=400, typical_high=500))
        self.assertEqual(d.typical_price, 600)
        self.assertIn("median", d.typical_basis)

    def test_instant_alert_threshold(self):
        great = self.make_deal(620 * 0.42)
        good = self.make_deal(620 * 0.52)
        self.scorer.score(great)
        self.scorer.score(good)
        self.assertTrue(self.scorer.should_alert_now(great))
        self.assertFalse(self.scorer.should_alert_now(good))

    def test_google_insight_tightens_reference(self):
        d = self.make_deal(350)
        insight = PriceInsight(typical_low=500, typical_high=900, history_min=420)
        self.scorer.score(d, insight=insight)
        self.assertEqual(d.reference_basis, "google_typical")
        self.assertEqual(d.reference_price, 500)
        self.assertEqual(d.verified_by, "serpapi")

    def test_verification_candidates_prefers_records(self):
        self.seed([480, 500, 520, 540])
        good_record = self.make_deal(240)
        not_record = self.make_deal(490)
        for d in (good_record, not_record):
            self.scorer.score(d)
        picks = self.scorer.verification_candidates([good_record, not_record], 5)
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].price_usd, 240)


# ======================================================================
#  History store
# ======================================================================


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hist = History(f"{self.tmp.name}/h.db")

    def tearDown(self):
        self.hist.close()
        self.tmp.cleanup()

    def deal(self, price):
        return Deal(
            origin="DFW",
            destination="ARN",
            destination_city="Stockholm",
            price_usd=price,
            depart_date=date(2026, 11, 2),
            return_date=date(2026, 11, 14),
            source="test",
        )

    def test_stats(self):
        self.hist.record_observations([self.deal(p) for p in [400, 500, 600, 700]])
        s = self.hist.route_stats("DFW-ARN")
        self.assertEqual(s["count"], 4)
        self.assertEqual(s["min"], 400)
        self.assertEqual(s["median"], 550)

    def test_empty_route(self):
        s = self.hist.route_stats("DFW-XXX")
        self.assertEqual(s["count"], 0)
        self.assertIsNone(s["min"])

    def test_dedupe_window(self):
        d = self.deal(350)
        self.assertFalse(self.hist.was_alerted(d.fingerprint(), 72))
        self.hist.record_alert(d)
        self.assertTrue(self.hist.was_alerted(d.fingerprint(), 72))

    def test_fingerprint_buckets_price(self):
        """A few dollars of drift must not re-alert."""
        a, b = self.deal(352), self.deal(357)
        self.assertEqual(a.fingerprint(), b.fingerprint())
        c = self.deal(390)
        self.assertNotEqual(a.fingerprint(), c.fingerprint())

    def test_fingerprint_distinguishes_stopover(self):
        a = self.deal(350)
        b = self.deal(350)
        b.stopover_code = "HEL"
        self.assertNotEqual(a.fingerprint(), b.fingerprint())

    def test_stopovers_get_their_own_price_series(self):
        """A stitched stopover must not pollute the round-trip benchmark."""
        plain = self.deal(600)
        via_hel = self.deal(380)
        via_hel.stopover_code = "HEL"
        via_hel.stopover_city = "Helsinki"

        self.assertEqual(plain.history_key, "DFW-ARN")
        self.assertEqual(via_hel.history_key, "DFW-HEL-ARN")

        self.hist.record_observations([plain, via_hel])
        self.assertEqual(self.hist.route_stats("DFW-ARN")["min"], 600)
        self.assertEqual(self.hist.route_stats("DFW-HEL-ARN")["min"], 380)

    def test_api_budget_tracking(self):
        self.assertEqual(self.hist.api_calls_this_month("serpapi"), 0)
        self.hist.bump_api_calls("serpapi", 3)
        self.hist.bump_api_calls("serpapi", 2)
        self.assertEqual(self.hist.api_calls_this_month("serpapi"), 5)

    def test_daily_budget_is_tracked_separately(self):
        """A monthly quota spent greedily leaves weeks with no verification."""
        self.assertEqual(self.hist.api_calls_today("serpapi"), 0)
        self.hist.bump_api_calls("serpapi", 4)
        self.assertEqual(self.hist.api_calls_today("serpapi"), 4)
        self.assertEqual(self.hist.api_calls_this_month("serpapi"), 4)
        # A different provider shares neither counter.
        self.assertEqual(self.hist.api_calls_today("other"), 0)

    def test_run_lifecycle(self):
        rid = self.hist.start_run()
        self.hist.finish_run(rid, observations=10, candidates=2, alerts_sent=1)
        runs = self.hist.recent_runs(1)
        self.assertEqual(runs[0]["observations"], 10)
        self.assertEqual(runs[0]["alerts_sent"], 1)


# ======================================================================
#  Travelpayouts parsing
# ======================================================================


class TestTravelpayoutsParsing(unittest.TestCase):
    def setUp(self):
        self.client = TravelpayoutsClient(token="fake", marker="12345")

    def test_parses_documented_response(self):
        """Exact shape from the Travelpayouts docs."""
        row = {
            "origin": "DFW",
            "destination": "ARN",
            "origin_airport": "DFW",
            "destination_airport": "ARN",
            "price": 412,
            "airline": "AY",
            "flight_number": "20",
            "departure_at": "2026-11-02T17:35:00+02:00",
            "return_at": "2026-11-14T14:30:00+02:00",
            "transfers": 1,
            "return_transfers": 1,
            "duration": 165,
            "duration_to": 80,
            "duration_back": 85,
            "link": "/search/DFW0211ARN14111?t=AY123",
        }
        deal = self.client.to_deal(row, "Stockholm")
        self.assertIsNotNone(deal)
        self.assertEqual(deal.price_usd, 412)
        self.assertEqual(deal.origin, "DFW")
        self.assertEqual(deal.depart_date, date(2026, 11, 2))
        self.assertEqual(deal.return_date, date(2026, 11, 14))
        self.assertIn("marker=12345", deal.booking_url)
        self.assertTrue(deal.booking_url.startswith("https://www.aviasales.com"))

    def test_rejects_too_many_stops(self):
        row = {
            "origin": "DFW", "destination": "ARN", "price": 300,
            "departure_at": "2026-11-02T17:35:00+02:00", "transfers": 3,
        }
        self.assertIsNone(self.client.to_deal(row, "Stockholm", max_extra_stops=1))

    def test_rejects_malformed(self):
        for row in [{}, {"price": "abc"}, {"price": 300}, {"price": -5}]:
            self.assertIsNone(self.client.to_deal(row, "Stockholm"))

    def test_date_parsing_variants(self):
        for text in [
            "2026-11-02T17:35:00+02:00",
            "2026-11-02T17:35:00Z",
            "2026-11-02T17:35:00",
            "2026-11-02",
        ]:
            self.assertIsNotNone(
                self.client._parse_dt(text), f"failed to parse {text}"
            )

    def test_months_ahead(self):
        months = months_ahead(1, 4, today=date(2026, 11, 15))
        self.assertEqual(months, ["2026-12", "2027-01", "2027-02", "2027-03"])


# ======================================================================
#  SerpApi parsing
# ======================================================================


class TestSerpApiParsing(unittest.TestCase):
    def test_parses_price_insights(self):
        """Shape taken from SerpApi's published example."""
        data = {
            "price_insights": {
                "lowest_price": 1339,
                "price_level": "high",
                "typical_price_range": [570, 1050],
                "price_history": [
                    [1691013600, 575],
                    [1691100000, 540],
                    [1696197600, 1339],
                ],
            },
            "best_flights": [{"price": 1339}, {"price": 1400}],
            "search_metadata": {"google_flights_url": "https://google.com/travel"},
        }
        i = SerpApiVerifier._parse(data)
        self.assertEqual(i.typical_low, 570)
        self.assertEqual(i.typical_high, 1050)
        self.assertEqual(i.history_min, 540)
        self.assertEqual(i.history_points, 3)
        self.assertEqual(i.best_offer, 1339)
        self.assertEqual(i.price_level, "high")

    def test_handles_missing_insights(self):
        i = SerpApiVerifier._parse({})
        self.assertIsNone(i.typical_low)
        self.assertFalse(i.has_typical)
        self.assertIsNone(i.beats_history(300))

    def test_discount_vs_typical(self):
        i = PriceInsight(typical_low=600, typical_high=900)
        self.assertAlmostEqual(i.discount_vs_typical(300), 50.0)

    def test_beats_history(self):
        i = PriceInsight(history_min=400)
        self.assertTrue(i.beats_history(395))
        self.assertTrue(i.beats_history(407, tolerance_pct=2))
        self.assertFalse(i.beats_history(500))


# ======================================================================
#  RSS parsing
# ======================================================================

SAMPLE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>The Flight Deal</title>
<item>
  <title>Error Fare: Dallas to Stockholm, Sweden for $238 roundtrip</title>
  <link>https://example.com/deal1</link>
  <guid>https://example.com/deal1</guid>
  <pubDate>Fri, 18 Sep 2026 10:00:00 +0000</pubDate>
  <description>Scandinavian Airlines has a mistake fare from DFW.</description>
</item>
<item>
  <title>Chicago to Tokyo, Japan for $512 roundtrip</title>
  <link>https://example.com/deal2</link>
  <guid>https://example.com/deal2</guid>
  <pubDate>Fri, 18 Sep 2026 09:00:00 +0000</pubDate>
  <description>Not Europe.</description>
</item>
<item>
  <title>Dallas to Paris, France for $1,890 roundtrip in business</title>
  <link>https://example.com/deal3</link>
  <guid>https://example.com/deal3</guid>
  <pubDate>Fri, 18 Sep 2026 08:00:00 +0000</pubDate>
  <description>Premium cabin.</description>
</item>
</channel></rss>"""


class TestRSS(unittest.TestCase):
    def setUp(self):
        self.watcher = RSSDealWatcher([])
        self.city_names = {"ARN": "Stockholm", "CDG": "Paris", "LHR": "London"}

    def test_parses_feed(self):
        items = self.watcher.parse(SAMPLE_RSS, "TFD")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].feed_name, "TFD")
        self.assertIsNotNone(items[0].published)

    def test_price_extraction(self):
        self.assertEqual(extract_price("to Stockholm for $238 roundtrip"), 238)
        self.assertEqual(extract_price("only $1,890 roundtrip"), 1890)
        self.assertEqual(extract_price("from £297"), 297)
        self.assertIsNone(extract_price("no price here"))
        # Years must not be mistaken for prices.
        self.assertIsNone(extract_price("back in 2019 this route"))

    def test_annotation_and_filtering(self):
        items = self.watcher.parse(SAMPLE_RSS)
        annotated = self.watcher.annotate(
            items,
            origin_codes=["DFW", "ORD"],
            destination_codes=["ARN", "CDG", "LHR"],
            city_names=self.city_names,
            hot_keywords=["error fare", "mistake fare"],
            origin_city_names=["Dallas"],
        )

        stockholm = annotated[0]
        self.assertTrue(stockholm.is_hot)
        self.assertEqual(stockholm.price_usd, 238)
        self.assertIn("ARN", stockholm.matched_destinations)
        self.assertTrue(stockholm.is_roundtrip)

        tokyo = annotated[1]
        self.assertEqual(tokyo.matched_destinations, [])

        relevant = self.watcher.relevant(annotated, max_price=499)
        links = [i.link for i in relevant]
        self.assertIn("https://example.com/deal1", links)
        self.assertNotIn("https://example.com/deal2", links)  # wrong continent
        self.assertNotIn("https://example.com/deal3", links)  # too expensive

    def test_ranking_puts_best_first(self):
        items = self.watcher.annotate(
            self.watcher.parse(SAMPLE_RSS),
            origin_codes=["DFW"],
            destination_codes=["ARN", "CDG"],
            city_names=self.city_names,
            hot_keywords=["error fare"],
        )
        relevant = self.watcher.relevant(items, max_price=2000)
        self.assertIn("Stockholm", relevant[0].title)

    def test_seen_guids_skipped(self):
        items = self.watcher.annotate(
            self.watcher.parse(SAMPLE_RSS),
            origin_codes=["DFW"],
            destination_codes=["ARN"],
            city_names=self.city_names,
            hot_keywords=["error fare"],
        )
        relevant = self.watcher.relevant(
            items, seen={"https://example.com/deal1"}
        )
        self.assertEqual(relevant, [])

    def test_malformed_feed_raises_cleanly(self):
        with self.assertRaises(ValueError):
            self.watcher.parse(b"not xml at all")


# ======================================================================
#  Carry-on: fit and fees
# ======================================================================


# The actual bag: Samsonite Freeform Carry-On Spinner, overall dimensions.
FREEFORM = BagSpec.from_inches(
    "Samsonite Freeform", height_in=23, width_in=15, depth_in=10, weight_lb=6.5
)


class TestBagSpec(unittest.TestCase):
    def test_inch_to_cm_conversion(self):
        self.assertAlmostEqual(FREEFORM.height_cm, 58.4, places=1)
        self.assertAlmostEqual(FREEFORM.width_cm, 38.1, places=1)
        self.assertAlmostEqual(FREEFORM.depth_cm, 25.4, places=1)
        self.assertAlmostEqual(FREEFORM.empty_weight_kg, 2.95, places=2)


class TestCarryOnFit(unittest.TestCase):
    """The bag exceeds every relevant carrier. These lock that in."""

    def test_fails_us_majors(self):
        for code in ("AA", "DL", "UA"):
            fits, problems = policy_for(code).fits(FREEFORM)
            self.assertFalse(fits, f"{code} should reject the bag")
            self.assertEqual(len(problems), 3, f"{code}: over on all three")

    def test_fails_finnair_and_sas(self):
        """The two carriers that own the Stockholm routes."""
        for code in ("AY", "SK"):
            fits, problems = policy_for(code).fits(FREEFORM)
            self.assertFalse(fits)
            self.assertEqual(len(problems), 2)  # too tall and too deep

    def test_british_airways_is_closest(self):
        fits, problems = policy_for("BA").fits(FREEFORM)
        self.assertFalse(fits)
        # 58.4 vs 56 and 25.4 vs 25 -- over, but only just.
        worst = max(float(p.split("+")[1].rstrip(")")) for p in problems)
        self.assertLess(worst, 2.5)

    def test_no_configured_airline_accepts_it(self):
        """Documents the finding: nothing in the table takes this bag."""
        accepting = [
            code for code, p in POLICIES.items() if p.fits(FREEFORM)[0]
        ]
        self.assertEqual(
            accepting, [], f"unexpectedly accepted by {accepting}"
        )

    def test_a_compliant_bag_passes(self):
        """Sanity check that fits() isn't just always False."""
        compliant = BagSpec.from_inches("21-inch", 21, 13.5, 8.5, 6.0)
        for code in ("AA", "AY", "SK", "BA"):
            fits, problems = policy_for(code).fits(compliant)
            self.assertTrue(fits, f"{code} rejected a compliant bag: {problems}")

    def test_weight_headroom(self):
        """8 kg cap minus a 2.95 kg bag leaves very little."""
        headroom = policy_for("AY").weight_headroom_kg(FREEFORM)
        self.assertAlmostEqual(headroom, 5.05, places=1)

    def test_unknown_airline_is_treated_as_risky(self):
        p = policy_for("ZZ")
        self.assertFalse(p.cabin_bag_in_cheapest_fare)
        self.assertGreater(p.cabin_bag_fee_usd, 0)


class TestBagFees(unittest.TestCase):
    def setUp(self):
        self.advisor = BaggageAdvisor(FREEFORM)

    def test_no_fee_when_bag_is_included(self):
        a = self.advisor.assess(["AA"], segments=2)
        self.assertEqual(a.fee_usd, 0.0)

    # NOTE: these used Finnair until 2026-09-19, when it turned out Finnair
    # includes a cabin bag on transatlantic fares. They use SAS now, which
    # genuinely charges ($40 a segment). See the AY entry in baggage.py.

    def test_fee_charged_per_segment_when_excluded(self):
        """SAS Go Light has no cabin bag: $40 each way."""
        a = self.advisor.assess(["SK"], segments=2)
        self.assertEqual(a.fee_usd, 80.0)

    def test_one_way_is_half(self):
        a = self.advisor.assess(["SK"], segments=1)
        self.assertEqual(a.fee_usd, 40.0)

    def test_extra_stopover_segment_adds_one_fee(self):
        """A 3-leg stopover pays three fees, not six."""
        rt = self.advisor.assess(["SK"], segments=2)
        stopover = self.advisor.assess(["SK"], segments=3)
        self.assertEqual(rt.fee_usd, 80.0)
        self.assertEqual(stopover.fee_usd, 120.0)

    def test_mixed_carriers_split_the_segments(self):
        """AA includes a bag, SAS doesn't -- only the SK legs are charged."""
        a = self.advisor.assess(["AA", "SK"], segments=2)
        self.assertEqual(a.fee_usd, 40.0)

    def test_fee_override_is_respected(self):
        advisor = BaggageAdvisor(FREEFORM, fee_overrides={"SK": 20})
        a = advisor.assess(["SK"], segments=2)
        self.assertEqual(a.fee_usd, 40.0)

    def test_included_override_zeroes_the_fee(self):
        advisor = BaggageAdvisor(FREEFORM, included_overrides={"SK": True})
        a = advisor.assess(["SK"], segments=2)
        self.assertEqual(a.fee_usd, 0.0)

    # ---- the bug that made it to a live rehearsal ----

    def test_unknown_carrier_is_never_charged_an_invented_fee(self):
        """Google Explore returns airline_code "multi" whenever two carriers
        operate an itinerary, which was 18 of 20 European routes. That hit
        the unknown-airline fallback and added $45 a segment out of thin
        air -- about 15% of the fare, enough to push every destination past
        the price ceiling and silence the bot completely."""
        for code in ("multi", "MULTI", "??", "ZZ"):
            a = self.advisor.assess([code], segments=2)
            self.assertEqual(a.fee_usd, 0.0, f"invented a fee for {code!r}")
            self.assertTrue(
                a.unknown_bag_carriers, f"{code!r} not flagged as unknown"
            )
            self.assertTrue(
                any("not included in the price" in w.lower() for w in a.warnings),
                f"{code!r} priced at zero without telling the reader why",
            )

    def test_known_carrier_is_not_flagged_as_unknown(self):
        a = self.advisor.assess(["AA"], segments=2)
        self.assertFalse(a.unknown_bag_carriers)

    def test_finnair_transatlantic_includes_the_bag(self):
        """Corrected 2026-09-19 against live fare data. Finnair is the
        cheapest carrier to Stockholm, so a phantom fee here landed
        squarely on the top-priority destination."""
        a = self.advisor.assess(["AY"], segments=2)
        self.assertEqual(a.fee_usd, 0.0)

    def test_risk_reflects_enforcement_not_just_size(self):
        """The correction: centimetres alone ranked these wrong.

        The bag is MORE over on Finnair (3.4cm) than on American (2.5cm),
        but what matters is who actually checks. Ryanair sizers every gate,
        so the same bag is a real problem there and a non-issue on AA.
        """
        aa = self.advisor.assess(["AA"]).risk_score
        ay = self.advisor.assess(["AY"]).risk_score
        fr = self.advisor.assess(["FR"]).risk_score
        self.assertLess(aa, ay)
        self.assertLess(ay, fr)
        self.assertEqual(self.advisor.assess(["AA"]).risk, "low")
        self.assertEqual(self.advisor.assess(["FR"]).risk, "oversize")

    def test_us_majors_are_low_risk(self):
        """Matches real-world experience: these bags go through."""
        for code in ("AA", "DL", "UA"):
            self.assertEqual(self.advisor.assess([code]).risk, "low")

    def test_lenient_carriers_get_a_reassuring_warning(self):
        a = self.advisor.assess(["AA"])
        self.assertTrue(
            any("rarely measure" in w for w in a.warnings), a.warnings
        )

    def test_strict_carriers_get_a_real_warning(self):
        a = self.advisor.assess(["FR"])
        self.assertTrue(
            any("enforce size" in w for w in a.warnings), a.warnings
        )

    def test_weight_risk_fires_on_nordic_carriers(self):
        """15 lb packed + 6.5 lb bag = 9.8 kg, over the 8 kg cap."""
        advisor = BaggageAdvisor(FREEFORM, packed_weight_kg=6.8)
        sas = advisor.assess(["SK"])
        self.assertGreater(sas.per_airline["SK"]["weight_risk"], 0)
        # SAS weighs strictly, American doesn't publish a cabin weight limit.
        self.assertEqual(advisor.assess(["AA"]).per_airline["AA"]["weight_risk"], 0)

    def test_compliant_bag_is_no_risk(self):
        """A bag inside 55x40x20cm clears even Ryanair's tight 20cm depth."""
        compliant = BaggageAdvisor(BagSpec.from_inches("small", 20, 13, 7.5, 5))
        for code in ("AA", "AY", "SK", "BA", "FR"):
            self.assertEqual(
                compliant.assess([code]).risk, "none", f"{code} flagged a risk"
            )

    def test_ryanair_depth_is_the_tightest_constraint(self):
        """20cm depth catches bags that clear every other carrier."""
        bag = BagSpec.from_inches("21-inch", 21, 13.5, 8.5, 6.0)
        self.assertTrue(policy_for("AY").fits(bag)[0])
        self.assertTrue(policy_for("BA").fits(bag)[0])
        self.assertFalse(policy_for("FR").fits(bag)[0])

    def test_warnings_mention_the_weight_cap(self):
        a = self.advisor.assess(["AY"])
        self.assertTrue(
            any("kg cap" in w or "kg cabin limit" in w for w in a.warnings),
            a.warnings,
        )


class TestBagChangesRanking(unittest.TestCase):
    """The bag fee must actually change which fare wins."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            {
                "origins": [{"code": "DFW", "weight": 1.0}],
                "destinations": [
                    {"code": "ARN", "city": "Stockholm", "priority": 1.0,
                     "baseline_usd": 650}
                ],
                "stopover_hubs": [{"code": "HEL", "city": "Helsinki", "appeal": 1.0}],
                "trip": {"min_nights": 5, "max_nights": 21,
                         "layover_hours": {"min": 24, "max": 120}},
                "thresholds": {
                    "max_price_usd": 499,
                    "min_discount_pct": 35,
                    "tiers": {"watch": 35, "good": 45, "great": 55, "insane": 65},
                    "require_record": False,
                    "min_observations_for_history": 3,
                },
                "email": {"smtp_host": "h", "smtp_port": 587},
                "storage": {"db_path": f"{self.tmp.name}/bag.db"},
            }
        )
        self.hist = History(self.cfg.db_path)
        self.scorer = DealScorer(self.cfg, self.hist)
        self.advisor = BaggageAdvisor(FREEFORM)

    def tearDown(self):
        self.hist.close()
        self.tmp.cleanup()

    def apply(self, d: Deal) -> Deal:
        a = self.advisor.assess(d.airlines, segments=d.bag_segments)
        d.bag_fee_usd = a.fee_usd
        d.bag_risk = a.risk
        d.bag_warnings = a.warnings
        return d

    def test_total_price_includes_the_bag(self):
        d = Deal(
            origin="DFW", destination="ARN", destination_city="Stockholm",
            price_usd=300, depart_date=date(2026, 11, 2),
            return_date=date(2026, 11, 14), airline="SK",
        )
        self.apply(d)
        self.assertEqual(d.bag_fee_usd, 80.0)
        self.assertEqual(d.total_price_usd, 380.0)

    def test_cheaper_fare_can_lose_once_the_bag_is_priced(self):
        """A $300 SAS fare is worse than a $350 American fare."""
        sas = Deal(
            origin="DFW", destination="ARN", destination_city="Stockholm",
            price_usd=300, depart_date=date(2026, 11, 2),
            return_date=date(2026, 11, 14), airline="SK",
        )
        american = Deal(
            origin="DFW", destination="ARN", destination_city="Stockholm",
            price_usd=350, depart_date=date(2026, 11, 2),
            return_date=date(2026, 11, 14), airline="AA",
        )
        self.apply(sas)
        self.apply(american)
        self.assertLess(sas.price_usd, american.price_usd)
        self.assertGreater(sas.total_price_usd, american.total_price_usd)

        for d in (sas, american):
            self.scorer.score(d)
        self.assertGreater(american.score, sas.score)

    def test_stopover_pays_the_fee_three_times(self):
        legs = [
            Leg("DFW", "HEL", datetime(2026, 11, 2, 17, 0), airline="SK"),
            Leg("HEL", "ARN", datetime(2026, 11, 5, 9, 0), airline="SK"),
            Leg("ARN", "DFW", datetime(2026, 11, 20, 8, 0), airline="SK"),
        ]
        d = Deal(
            origin="DFW", destination="ARN", destination_city="Stockholm",
            price_usd=383, depart_date=date(2026, 11, 2),
            return_date=date(2026, 11, 20),
            stopover_code="HEL", stopover_city="Helsinki",
            stopover_hours=54, legs=legs,
        )
        self.assertEqual(d.ticket_count, 3)
        self.assertEqual(d.bag_segments, 3)
        self.apply(d)
        self.assertEqual(d.bag_fee_usd, 120.0)   # $40 on each of 3 one-way tickets
        self.assertEqual(d.total_price_usd, 503.0)

        self.scorer.score(d)
        ok, reason = self.scorer.passes_filters(d)
        self.assertFalse(ok, "a $548 all-in trip should not pass a $499 ceiling")
        self.assertIn("ceiling", reason)

    def test_strict_carrier_ranks_below_lenient_one(self):
        """Same fare, same bag: Ryanair should rank below American."""
        def make(airline):
            return Deal(
                origin="DFW", destination="ARN", destination_city="Stockholm",
                price_usd=300, depart_date=date(2026, 11, 2),
                return_date=date(2026, 11, 14), airline=airline,
            )
        aa, fr = make("AA"), make("FR")
        for d in (aa, fr):
            self.apply(d)
            self.scorer.score(d)
        self.assertGreater(aa.score, fr.score)

    def test_skip_oversize_gate(self):
        """The opt-in gate drops strict carriers, not lenient ones."""
        self.cfg.thresholds["skip_oversize_bag_routes"] = True

        ryanair = Deal(
            origin="DFW", destination="ARN", destination_city="Stockholm",
            price_usd=200, depart_date=date(2026, 11, 2),
            return_date=date(2026, 11, 14), airline="FR",
        )
        self.apply(ryanair)
        self.scorer.score(ryanair)
        ok, reason = self.scorer.passes_filters(ryanair)
        self.assertFalse(ok)
        self.assertIn("fit", reason)

        american = Deal(
            origin="DFW", destination="ARN", destination_city="Stockholm",
            price_usd=200, depart_date=date(2026, 11, 2),
            return_date=date(2026, 11, 14), airline="AA",
        )
        self.apply(american)
        self.scorer.score(american)
        ok, _ = self.scorer.passes_filters(american)
        self.assertTrue(ok, "a lenient carrier should not be dropped")


# ======================================================================
#  .env loading (so nobody has to get `export` right)
# ======================================================================


class TestEnvFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"
        self.keys = [
            "FDB_TOKEN", "FDB_USER", "FDB_PASS", "FDB_TO", "FDB_EMPTY",
        ]
        for k in self.keys:
            os.environ.pop(k, None)

    def tearDown(self):
        for k in self.keys:
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")
        import bot.config as c
        c._ENV_FILE_LOADED = False
        return load_env_file(self.path)

    def test_parses_plain_values(self):
        self.write("FDB_USER=me@gmail.com\n")
        self.assertEqual(os.environ["FDB_USER"], "me@gmail.com")

    def test_strips_trailing_comma_outside_quotes(self):
        """The exact damage a pasted `export A="x", export B="y"` leaves."""
        self.write('FDB_TOKEN="abc123",\n')
        self.assertEqual(os.environ["FDB_TOKEN"], "abc123")

    def test_strips_export_prefix_and_quotes(self):
        self.write("export FDB_USER='me@gmail.com'\n")
        self.assertEqual(os.environ["FDB_USER"], "me@gmail.com")

    def test_preserves_spaces_in_app_passwords(self):
        """Gmail app passwords are four space-separated groups."""
        # A Gmail app password's SHAPE (four space-separated groups of four)
        # without being anyone's actual password. This line held a real one
        # until 2026-09-19, in a file about to be pushed to a public repo.
        self.write("FDB_PASS=aaaa bbbb cccc dddd\n")
        self.assertEqual(os.environ["FDB_PASS"], "aaaa bbbb cccc dddd")

    def test_ignores_comments_and_blank_lines(self):
        n = self.write("# a comment\n\nFDB_USER=x\n\n# another\n")
        self.assertEqual(n, 1)

    def test_real_environment_wins(self):
        """GitHub Actions secrets must never be overridden by a stray file."""
        os.environ["FDB_USER"] = "from-real-env"
        self.write("FDB_USER=from-file\n")
        self.assertEqual(os.environ["FDB_USER"], "from-real-env")

    def test_missing_file_is_not_an_error(self):
        import bot.config as c
        c._ENV_FILE_LOADED = False
        self.assertEqual(load_env_file(Path(self.tmp.name) / "nope.env"), 0)


# ======================================================================
#  TLS context (the macOS empty-trust-store trap)
# ======================================================================


class TestSSLContext(unittest.TestCase):
    def test_uses_platform_store_when_it_has_cas(self):
        ctx = build_ssl_context()
        self.assertGreater(ctx.cert_store_stats()["x509_ca"], 0)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_falls_back_to_certifi_when_store_is_empty(self):
        """A python.org macOS install has no CAs until you run its script."""
        import certifi
        empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(empty.cert_store_stats()["x509_ca"], 0)

        real = ssl.create_default_context(cafile=certifi.where())
        with patch.object(ssl, "create_default_context", side_effect=[empty, real]):
            ctx = build_ssl_context()
        self.assertGreater(ctx.cert_store_stats()["x509_ca"], 0)

    def test_fallback_never_weakens_verification(self):
        """The fix must not become 'skip certificate checking'."""
        import certifi
        empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        real = ssl.create_default_context(cafile=certifi.where())
        with patch.object(ssl, "create_default_context", side_effect=[empty, real]):
            ctx = build_ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)


# ======================================================================
#  Scan budget and rotation
# ======================================================================


class TestScanRotation(unittest.TestCase):
    """A full sweep blows the GitHub Actions free tier, so runs rotate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import os
        os.environ["TRAVELPAYOUTS_TOKEN"] = "fake"
        self.cfg = Config.load(
            Path(__file__).resolve().parents[1] / "config.yml"
        )
        self.cfg._d["storage"]["db_path"] = f"{self.tmp.name}/rot.db"
        from bot.main import FlightDealBot
        self.bot = FlightDealBot(self.cfg, dry_run=True)

    def tearDown(self):
        self.bot.close()
        self.tmp.cleanup()

    def test_priority_destinations_are_never_rotated_out(self):
        """Stockholm is priority 2.0 -- it must be scanned every run."""
        always, rotating = self.bot._route_pairs()
        always_dests = {d["code"] for _, d in always}
        self.assertIn("ARN", always_dests)
        self.assertNotIn("ARN", {d["code"] for _, d in rotating
                                 if d["code"] == "ARN"} - always_dests)

    def test_rotation_moves_between_runs(self):
        items = list(range(20))
        self.bot.run_index = 0
        first = self.bot._rotating_slice(items, 5)
        self.bot.run_index = 1
        second = self.bot._rotating_slice(items, 5)
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 5)

    def test_rotation_covers_everything_eventually(self):
        items = list(range(20))
        seen = set()
        for run in range(4):
            self.bot.run_index = run
            seen.update(self.bot._rotating_slice(items, 5))
        self.assertEqual(seen, set(items), "rotation left routes unvisited")

    def test_rotation_returns_all_when_budget_exceeds_list(self):
        items = list(range(3))
        self.bot.run_index = 7
        self.assertEqual(self.bot._rotating_slice(items, 10), items)

    def test_run_index_advances_with_run_count(self):
        self.assertEqual(self.bot.hist.run_count(), 0)
        self.bot.hist.start_run()
        self.assertEqual(self.bot.hist.run_count(), 1)


# ======================================================================
#  Config validation
# ======================================================================


class TestConfig(unittest.TestCase):
    def base(self):
        return {
            "origins": [{"code": "DFW"}],
            "destinations": [{"code": "ARN", "city": "Stockholm", "baseline_usd": 620}],
            "trip": {
                "min_nights": 5,
                "max_nights": 21,
                "layover": {
                    "quick_max_hours": 5,
                    "overnight_min_hours": 24,
                    "overnight_max_hours": 120,
                },
            },
            "thresholds": {
                "tiers": {"watch": 35, "good": 45, "great": 55, "insane": 65}
            },
            "email": {"smtp_host": "h", "smtp_port": 587},
        }

    def test_valid_config(self):
        cfg = Config(self.base())
        self.assertEqual(cfg.origin_codes, ["DFW"])
        self.assertEqual(cfg.baseline("ARN"), 620)

    def test_rejects_rules_with_no_dead_zone(self):
        data = self.base()
        data["trip"]["layover"] = {
            "quick_max_hours": 10,
            "overnight_min_hours": 8,
        }
        with self.assertRaises(ConfigError) as ctx:
            Config(data)
        self.assertIn("dead zone", str(ctx.exception))

    def test_rejects_unordered_tiers(self):
        data = self.base()
        data["thresholds"]["tiers"] = {
            "watch": 60, "good": 45, "great": 55, "insane": 65
        }
        with self.assertRaises(ConfigError):
            Config(data)

    def test_rejects_missing_section(self):
        data = self.base()
        del data["email"]
        with self.assertRaises(ConfigError):
            Config(data)

    def test_real_config_file_is_valid(self):
        path = Path(__file__).resolve().parents[1] / "config.yml"
        if path.exists():
            cfg = Config.load(path)
            self.assertIn("ARN", cfg.destination_codes)
            r = cfg.layover_rules
            self.assertEqual(r.quick_max_hours, 5)
            self.assertEqual(r.overnight_min_hours, 14)
            self.assertEqual(r.require_night_below_hours, 24)
            # Stockholm must outrank everything else.
            top = max(cfg.destinations, key=lambda d: d.get("priority", 1))
            self.assertEqual(top["code"], "ARN")

            # The real carry-on must be configured and match the bag.
            c = cfg.carry_on
            self.assertEqual(c["height_in"], 23)
            self.assertEqual(c["width_in"], 15)
            self.assertEqual(c["depth_in"], 10)
            bag = BagSpec.from_inches(
                c["name"], c["height_in"], c["width_in"], c["depth_in"],
                c["weight_lb"],
            )
            # And it must be flagged as not fitting Finnair, the cheapest
            # carrier on the Stockholm route.
            self.assertFalse(policy_for("AY").fits(bag)[0])




# ======================================================================
#  The Europe sweep -- the primary fare source
# ======================================================================


class TestEuropeSweep(unittest.TestCase):
    """Google Travel Explore is now where every fare comes from.

    These exist because the source it replaced looked healthy for weeks
    while silently returning nothing.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SERPAPI_KEY"] = "fake-key"
        os.environ.pop("TRAVELPAYOUTS_TOKEN", None)
        self.cfg = Config.load(Path(__file__).resolve().parents[1] / "config.yml")
        self.cfg._d["storage"]["db_path"] = f"{self.tmp.name}/sweep.db"
        from bot.main import FlightDealBot
        self.bot = FlightDealBot(self.cfg, dry_run=True)

    def tearDown(self):
        self.bot.close()
        self.tmp.cleanup()
        os.environ.pop("SERPAPI_KEY", None)

    # ---------- budget ----------

    def test_room_holds_back_the_verification_reserve(self):
        cfg = self.cfg.sources["serpapi"]
        cfg["daily_budget"], cfg["monthly_budget"] = 8, 240
        self.assertEqual(self.bot._serpapi_room(), 8)
        self.assertEqual(self.bot._serpapi_room(reserve=2), 6)

    def test_room_respects_the_daily_cap(self):
        cfg = self.cfg.sources["serpapi"]
        cfg["daily_budget"], cfg["monthly_budget"] = 8, 240
        self.bot.hist.bump_api_calls("serpapi", 7)
        self.assertEqual(self.bot._serpapi_room(), 1)
        # Reserve can't push it negative.
        self.assertEqual(self.bot._serpapi_room(reserve=5), 0)

    def test_room_respects_the_monthly_cap(self):
        cfg = self.cfg.sources["serpapi"]
        cfg["daily_budget"], cfg["monthly_budget"] = 100, 3
        self.bot.hist.bump_api_calls("serpapi", 3)
        self.assertEqual(self.bot._serpapi_room(), 0)

    def test_sweep_is_skipped_when_the_budget_is_gone(self):
        self.cfg.sources["serpapi"]["daily_budget"] = 1
        self.bot.hist.bump_api_calls("serpapi", 1)
        called = []
        self.bot.explore.scan = lambda **kw: called.append(kw) or []
        self.assertEqual(self.bot.scan_explore(), [])
        self.assertEqual(called, [], "spent a search it didn't have")

    # ---------- rotation ----------

    def test_rotation_advances_with_the_run_index(self):
        seen = []
        self.bot.explore.scan = lambda **kw: seen.append(
            (kw["origin"], kw["travel_duration"])
        ) or []
        rotation = self.cfg.sources["google_explore"]["rotation"]
        for i in range(len(rotation)):
            self.bot.run_index = i
            self.bot.hist.bump_api_calls("serpapi", -self.bot.hist.api_calls_today("serpapi"))
            self.bot.scan_explore()
        expected = [(s["origin"], s["duration"]) for s in rotation]
        self.assertEqual(seen, expected)

    def test_rotation_wraps_around(self):
        rotation = self.cfg.sources["google_explore"]["rotation"]
        seen = []
        self.bot.explore.scan = lambda **kw: seen.append(kw["origin"]) or []
        self.bot.run_index = len(rotation)          # one full lap later
        self.bot.scan_explore()
        self.assertEqual(seen, [rotation[0]["origin"]])

    # ---------- billing ----------

    def test_a_failed_request_is_still_billed(self):
        """SerpApi charges for rejected searches. If the bot doesn't count
        them it will sail past the free tier and start getting 429s."""
        from bot.sources.google_deals import GoogleDealsError

        def boom(**kw):
            self.bot.explore.call_count += 1     # the request did go out
            raise GoogleDealsError("HTTP 500")

        self.bot.explore.scan = boom
        before = self.bot.hist.api_calls_today("serpapi")
        self.bot.scan_explore()
        self.assertEqual(self.bot.hist.api_calls_today("serpapi"), before + 1)

    def test_a_failure_stops_the_run_rather_than_retrying(self):
        from bot.sources.google_deals import GoogleDealsError
        self.cfg.sources["google_explore"]["calls_per_run"] = 3
        attempts = []

        def boom(**kw):
            attempts.append(kw)
            self.bot.explore.call_count += 1
            raise GoogleDealsError("quota exhausted (429)")

        self.bot.explore.scan = boom
        self.bot.scan_explore()
        self.assertEqual(len(attempts), 1, "kept burning searches after a 429")

    def test_successful_sweep_is_billed_once_per_call(self):
        self.cfg.sources["google_explore"]["calls_per_run"] = 2

        def ok(**kw):
            self.bot.explore.call_count += 1
            return []

        self.bot.explore.scan = ok
        before = self.bot.hist.api_calls_today("serpapi")
        self.bot.scan_explore()
        self.assertEqual(self.bot.hist.api_calls_today("serpapi"), before + 2)

    # ---------- what it hands downstream ----------

    def test_fares_survive_the_layover_rule(self):
        """Explore reports a total duration and a stop count but never the
        layover itself. The estimator has to let a normal 1-stop
        transatlantic itinerary through, or the sweep feeds nothing on."""
        from bot.sources.google_deals import GoogleTravelExplore
        row = {
            "destination_airport": {"code": "ARN"},
            "name": "Stockholm",
            "flight_price": 567,
            "start_date": "2026-11-29",
            "end_date": "2026-12-08",
            "number_of_stops": 1,
            "airline_code": "AY",
            "flight_duration": 825,
        }
        deal = GoogleTravelExplore.to_deal(row, "DFW", {"ARN": "Stockholm"})
        self.assertIsNotNone(deal)
        self.assertEqual(deal.destination, "ARN")
        self.assertEqual(deal.price_usd, 567)
        kept = self.bot.apply_layover_rule([deal])
        self.assertEqual(len(kept), 1, f"dropped: {deal.layover_note}")

    def test_a_request_that_never_reached_serpapi_is_not_billed(self):
        """The flip side of the test above. A connection refused by a proxy
        never reaches SerpApi, so it costs nothing -- billing it anyway
        would starve the real budget on a bad network day."""
        import requests
        from bot.sources.google_deals import GoogleDealsError

        def unreachable(*a, **kw):
            raise requests.RequestException("Tunnel connection failed: 403")

        self.bot.explore.session.get = unreachable
        before = self.bot.hist.api_calls_today("serpapi")
        with self.assertRaises(GoogleDealsError):
            self.bot.explore.explore(origin="DFW")
        self.assertEqual(self.bot.explore.call_count, 0)
        self.assertEqual(self.bot.hist.api_calls_today("serpapi"), before)


class TestBaselineIsOriginSpecific(unittest.TestCase):
    """A Dallas baseline must not be used to judge a Chicago fare.

    The first live run scored a routine ORD->Reykjavik fare at "40% off"
    against a baseline measured from DFW. Chicago has an Icelandair
    nonstop and Dallas does not, so $393 is Chicago's ordinary winter
    price -- identical on two date pairs three weeks apart.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config.load(Path(__file__).resolve().parents[1] / "config.yml")
        self.cfg._d["storage"]["db_path"] = f"{self.tmp.name}/origin.db"
        self.hist = History(self.cfg.db_path)
        self.scorer = DealScorer(self.cfg, self.hist)

    def tearDown(self):
        self.hist.close()
        self.tmp.cleanup()

    def _deal(self, origin):
        return Deal(
            origin=origin, destination="KEF", destination_city="Reykjavik",
            price_usd=393, depart_date=date(2027, 1, 14),
            return_date=date(2027, 1, 28), airline="FI",
        )

    def test_home_origin_uses_the_configured_baseline(self):
        d = self._deal("DFW")
        self.scorer.score(d)
        self.assertEqual(d.typical_price, self.cfg.baseline("KEF"))

    def test_other_origins_get_no_baseline(self):
        d = self._deal("ORD")
        self.scorer.score(d)
        self.assertIsNone(
            d.typical_price,
            "a Chicago fare was scored against a Dallas baseline",
        )

    def test_the_actual_false_positive_no_longer_alerts(self):
        """The exact fare from the first live run."""
        ord_deal = self._deal("ORD")
        self.scorer.score(ord_deal)
        ok, _ = self.scorer.passes_filters(ord_deal)
        self.assertFalse(ok, "routine ORD fare still reaches the inbox")


class TestIcelandairBag(unittest.TestCase):
    def test_transatlantic_includes_the_cabin_bag(self):
        advisor = BaggageAdvisor(FREEFORM)
        self.assertEqual(advisor.assess(["FI"], segments=2).fee_usd, 0.0)



class TestRedaction(unittest.TestCase):
    """Errors are written to data/prices.db, which is committed to a PUBLIC
    repo so the runner remembers prices. A SerpApi failure carries the whole
    request URL, api_key and all."""

    def test_api_key_is_stripped_from_an_error(self):
        from bot.main import redact
        msg = ("network error: HTTPSConnectionPool(host='serpapi.com') "
               "url: /search?engine=google_travel_explore"
               "&api_key=681aa0bf3488243019782ca9059a9df6&currency=USD")
        out = redact(msg)
        self.assertNotIn("681aa0bf", out)
        self.assertIn("api_key=[REDACTED]", out)
        self.assertIn("serpapi.com", out, "redaction ate the useful part")

    def test_other_credential_shapes(self):
        from bot.main import redact
        for field in ("token", "apikey", "password", "secret", "key"):
            out = redact(f"failed {field}=s3cr3tvalue99 rest")
            self.assertNotIn("s3cr3tvalue99", out, field)

    def test_harmless_text_is_untouched(self):
        from bot.main import redact
        self.assertEqual(redact("0 candidates passed"), "0 candidates passed")
        self.assertEqual(redact(""), "")



# ======================================================================
#  NOTE: this block must stay at the very END of the file.
#
#  It used to sit mid-file, and `python -m tests.test_bot` -- which is how
#  the GitHub Action runs the suite -- executes top to bottom, so
#  unittest.main() fired before the classes below it had been defined. The
#  suite reported "OK, 122 tests" while silently skipping 11 of them.
#  `unittest discover` imports the module instead of running it, so it saw
#  all 133 and the two disagreed without either one failing.
#
#  Add new tests ABOVE this block.
# ======================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
