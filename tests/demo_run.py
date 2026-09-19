"""Simulate a complete run with mock fare data and print the real email.

No API keys, no network. This exercises the actual scoring, stopover and
email code paths so you can see exactly what would land in your inbox.

Run: python -m tests.demo_run
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.baggage import BaggageAdvisor, BagSpec
from bot.config import Config
from bot.emailer import Emailer
from bot.history import History
from bot.models import Deal
from bot.scoring import DealScorer
from bot.sources.rss_deals import RSSDealWatcher
from bot.sources.serpapi_verify import PriceInsight
from bot.layovers import assess_api_layover, spans_night
from bot.stopovers import StopoverBuilder, verify_layover_rule

SAMPLE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item>
  <title>Error Fare: Dallas to Stockholm, Sweden for $238 roundtrip</title>
  <link>https://example.com/deal1</link><guid>d1</guid>
  <pubDate>Fri, 18 Sep 2026 10:00:00 +0000</pubDate>
  <description>SAS mistake fare departing DFW.</description>
</item>
</channel></rss>"""


def leg(dep: datetime, price: float, duration: int, airline="AY", num="20"):
    return {
        "price": price,
        "departure_at": dep,
        "airline": airline,
        "flight_number": num,
        "transfers": 0,
        "duration": duration,
        "link": f"/search/DEMO{num}",
        "raw": {"link": f"/search/DEMO{num}", "price": price},
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    cfg = Config.load(root / "config.yml")

    tmp = tempfile.TemporaryDirectory()
    hist = History(f"{tmp.name}/demo.db")
    scorer = DealScorer(cfg, hist)

    c = cfg.carry_on
    bag = BagSpec.from_inches(
        c["name"], c["height_in"], c["width_in"], c["depth_in"],
        c.get("weight_lb", 0), c.get("expandable_in", 0),
    )
    packed = c.get("packed_weight_lb")
    advisor = BaggageAdvisor(
        bag,
        fee_overrides=c.get("fee_overrides") or {},
        included_overrides=c.get("included_overrides") or {},
        assume_fee_usd=c.get("assume_fee_usd", 45),
        packed_weight_kg=round(packed * 0.453592, 2) if packed else None,
    )

    print("=" * 70)
    print("STEP 0  Your carry-on vs. the airlines")
    print("=" * 70)
    print(f"  {bag.describe()}")
    print()
    for code in ("AA", "AY", "SK", "BA", "KL", "FR"):
        a = advisor.assess([code])
        info = a.per_airline[code]
        over = "fits" if info["fits"] else f"{info['max_overage_cm']}cm over"
        print(
            f"  {info['airline']:<18}{info['limit_cm']:>12}{over:>12}"
            f"   enforces {info['size_enforcement']:<9} risk: {a.risk}"
        )

    print()
    print("=" * 70)
    print("STEP 0b  The layover rule")
    print("=" * 70)
    rules = cfg.layover_rules
    print(f"  {rules.describe()}")
    print()
    print("  Under 24h, the clock times decide -- not the duration:")
    print()
    print(f"  {'layover':<26}{'hours':>7}{'night?':>9}{'verdict':>12}")
    print("  " + "-" * 54)
    for label, a, d in [
        ("18:00 -> 08:00 next day", datetime(2026, 11, 2, 18, 0),
         datetime(2026, 11, 3, 8, 0)),
        ("08:00 -> 22:00 same day", datetime(2026, 11, 2, 8, 0),
         datetime(2026, 11, 2, 22, 0)),
        ("14:00 -> 04:00 next day", datetime(2026, 11, 2, 14, 0),
         datetime(2026, 11, 3, 4, 0)),
        ("12:00 -> 14:00 +1 (26h)", datetime(2026, 11, 2, 12, 0),
         datetime(2026, 11, 3, 14, 0)),
    ]:
        h = (d - a).total_seconds() / 3600
        night = "yes" if spans_night(a, d) else "no"
        print(
            f"  {label:<26}{h:>6.0f}h{night:>9}"
            f"{str(rules.classify(h, a, d)):>12}"
        )

    print()
    print("  Fares from the API don't report their layover, so it's estimated")
    print("  from total journey time minus how long the flying should take:")
    print()
    print(f"  {'total':>7}{'est. layover':>14}{'band':>12}{'verdict':>10}")
    print("  " + "-" * 43)
    for total, label in [(715, "live DFW-HEL-ARN"), (900, ""), (1000, ""),
                         (1255, ""), (1500, "")]:
        e = assess_api_layover("DFW", "ARN", total, 1, rules,
                               margin_hours=cfg.layover.get(
                                   "estimate_margin_hours", 2.5))
        verdict = "DROP" if e.is_known_bad else "keep"
        note = f"   <- {label}" if label else ""
        print(
            f"  {total:>7}{e.hours:>13.1f}h{str(e.band):>12}{verdict:>10}{note}"
        )

    print()
    print("=" * 70)
    print("STEP 1  Seed 60 days of ordinary DFW-ARN prices")
    print("=" * 70)
    # Realistic history: the live Kiwi check on 2026-09-18 showed $609-675.
    seed = []
    for i in range(60):
        price = 590 + (i * 7) % 140          # $590-$730
        seed.append(
            Deal(
                origin="DFW",
                destination="ARN",
                destination_city="Stockholm",
                price_usd=price,
                depart_date=date(2026, 11, 2) + timedelta(days=i % 30),
                return_date=date(2026, 11, 14) + timedelta(days=i % 30),
                source="seed",
            )
        )
    hist.record_observations(seed)
    stats = hist.route_stats("DFW-ARN")
    print(f"  observations : {stats['count']}")
    print(f"  all-time low : ${stats['min']:,.0f}")
    print(f"  p10 benchmark: ${stats['p10']:,.0f}")
    print(f"  median       : ${stats['median']:,.0f}")

    print()
    print("=" * 70)
    print("STEP 2  Build overnight stopovers")
    print("=" * 70)
    builder = StopoverBuilder(
        rules=cfg.layover_rules,
        min_nights=cfg.trip["min_nights"],
        max_nights=cfg.trip["max_nights"],
    )

    # DFW -> HEL one-ways, then HEL -> ARN one-ways a few days later.
    leg_a = {
        (date(2026, 11, 2) + timedelta(days=i)): leg(
            datetime(2026, 11, 2, 17, 35) + timedelta(days=i),
            [168, 205, 189][i % 3],
            605,
        )
        for i in range(6)
    }
    leg_b = {
        (date(2026, 11, 4) + timedelta(days=i)): leg(
            datetime(2026, 11, 4, 12, 25) + timedelta(days=i),
            [44, 61, 52][i % 3],
            65,
            airline="AY",
            num="809",
        )
        for i in range(8)
    }
    returns = {
        (date(2026, 11, 20) + timedelta(days=i)): leg(
            datetime(2026, 11, 20, 8, 50) + timedelta(days=i),
            [171, 198][i % 2],
            660,
            airline="AY",
            num="19",
        )
        for i in range(6)
    }

    built = builder.build(
        origin="DFW",
        hub="HEL",
        destination="ARN",
        destination_city="Stockholm",
        hub_city="Helsinki",
        leg_a=leg_a,
        leg_b=leg_b,
        return_legs=returns,
    )
    print(f"  built {len(built)} itineraries")
    for d in built[:4]:
        print(
            f"    ${d.price_usd:>6,.0f}  dep {d.depart_date}  "
            f"{d.stopover_hours / 24:>4.1f}d in {d.stopover_city}  "
            f"{d.nights}n in Stockholm"
        )

    # Prove the rule holds on every single one.
    violations = [d for d in built if not verify_layover_rule(d, cfg.layover_rules)]
    print(f"  layover-rule violations: {len(violations)}  <-- must be 0")
    assert not violations, "layover rule was violated"

    print()
    print("=" * 70)
    print("STEP 3  Add a plain round trip and an error fare, then score")
    print("=" * 70)
    candidates = list(built)
    candidates.append(
        Deal(
            origin="DFW",
            destination="ARN",
            destination_city="Stockholm",
            price_usd=238,
            depart_date=date(2027, 1, 14),
            return_date=date(2027, 1, 26),
            airline="AA",
            booking_url="https://www.aviasales.com/search/DEMO-ERROR",
            source="travelpayouts",
        )
    )
    candidates.append(
        Deal(
            origin="DFW",
            destination="CDG",
            destination_city="Paris",
            price_usd=352,
            depart_date=date(2027, 2, 3),
            return_date=date(2027, 2, 15),
            airline="AF",
            booking_url="https://www.aviasales.com/search/DEMO-CDG",
            source="travelpayouts",
        )
    )

    # Price the carry-on into every fare before scoring.
    for d in candidates:
        a = advisor.assess(
            d.airlines,
            segments=d.bag_segments,
        )
        d.bag_fee_usd = a.fee_usd
        d.bag_risk = a.risk
        d.bag_warnings = a.warnings
        if a.fee_usd:
            d.notes.append(
                f"+${a.fee_usd:,.0f} for the carry-on across "
                f"{d.ticket_count} ticket(s) -- included in the price shown"
            )

    # Observations are written AFTER scoring in a real run, so a batch of
    # fares never becomes its own benchmark.
    passed = []
    for d in candidates:
        scorer.score(d, exclude_recent_seconds=3600)
        ok, reason = scorer.passes_filters(d)
        flag = "PASS" if ok else f"drop ({reason})"
        bag = f"+{d.bag_fee_usd:>3,.0f}" if d.bag_fee_usd else "    "
        print(
            f"  ${d.price_usd:>5,.0f}{bag} =${d.total_price_usd:>6,.0f}  "
            f"{d.history_key:<14}{str(d.discount_pct) + '%':>7}  "
            f"{d.tier or '-':<7} {'RECORD' if d.is_record else '      '}  {flag}"
        )
        if ok:
            passed.append(d)

    hist.record_observations(candidates)

    print()
    print("=" * 70)
    print("STEP 4  Verify the top candidate against Google (mocked)")
    print("=" * 70)
    to_verify = scorer.verification_candidates(passed, 3)
    print(f"  would spend {len(to_verify)} SerpApi search(es) this run")
    if to_verify:
        fake = PriceInsight(
            lowest_price=612,
            price_level="low",
            typical_low=580,
            typical_high=980,
            history_min=498,
            history_points=142,
            best_offer=245,
        )
        top = to_verify[0]
        scorer.score(top, insight=fake, exclude_recent_seconds=3600)
        print(f"  {top.route}: Google typical ${fake.typical_low}-${fake.typical_high}")
        print(f"  re-scored -> {top.discount_pct}% off, tier={top.tier}")

    passed.sort(key=lambda d: d.score, reverse=True)

    print()
    print("=" * 70)
    print("STEP 5  Read the error-fare feeds")
    print("=" * 70)
    watcher = RSSDealWatcher([])
    items = watcher.annotate(
        watcher.parse(SAMPLE_RSS, "The Flight Deal"),
        origin_codes=cfg.origin_codes,
        destination_codes=cfg.destination_codes,
        city_names={d["code"]: d["city"] for d in cfg.destinations},
        hot_keywords=cfg.sources["rss"]["hot_keywords"],
        origin_city_names=[o.get("name", "") for o in cfg.origins],
    )
    rss_hits = watcher.relevant(items, max_price=cfg.thresholds["max_price_usd"])
    for i in rss_hits:
        print(f"  [{'HOT' if i.is_hot else '   '}] ${i.price_usd:,.0f}  {i.title}")

    print()
    print("=" * 70)
    print("STEP 6  The email you would actually receive")
    print("=" * 70)
    emailer = Emailer(
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        username="bot@example.com",
        password="",
        to_address=cfg.email.get("to", "you@example.com"),
        dry_run=True,
    )
    urgent = [d for d in passed if scorer.should_alert_now(d)]
    emailer.send_deals(urgent or passed, urgent=bool(urgent), rss_items=rss_hits)

    # Save the HTML so it can be eyeballed in a browser.
    out = Path(__file__).resolve().parent / "sample_email.html"
    if emailer.last_message:
        for part in emailer.last_message.walk():
            if part.get_content_type() == "text/html":
                out.write_text(part.get_content(), encoding="utf-8")
                print(f"HTML version written to: {out}")

    hist.close()
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
