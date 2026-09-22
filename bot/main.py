"""Entry point. Scan -> build stopovers -> score -> verify -> alert.

Usage:
    python -m bot.main                 # full run
    python -m bot.main --dry-run       # print emails instead of sending
    python -m bot.main --digest        # send the daily digest
    python -m bot.main --stats         # show what the bot has learned
    python -m bot.main --heartbeat     # weekly "still alive" email
    python -m bot.main --bag           # carry-on fit and fees by airline
    python -m bot.main --test-email    # prove SMTP works
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import traceback
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .baggage import POLICIES, BaggageAdvisor, BagSpec
from .layovers import DEAD_ZONE, STOPOVER, assess_api_layover
from .config import Config, ConfigError, env
from .emailer import Emailer, EmailError
from .history import History
from .models import Deal, tier_rank
from .scoring import DealScorer
from .sources.google_deals import (
    AREA_EUROPE,
    DURATION_ONE_WEEK,
    GoogleDealsError,
    GoogleTravelExplore,
)
from .sources.rss_deals import RSSDealWatcher
from .sources.serpapi_verify import PriceInsight, SerpApiError, SerpApiVerifier
from .sources.travelpayouts import (
    TravelpayoutsClient,
    TravelpayoutsError,
    months_ahead,
)
from .stopovers import StopoverBuilder, verify_layover_rule

log = logging.getLogger("bot")

# Observations written during this run are excluded from record comparisons,
# so a fare is never compared against itself.
RUN_WINDOW_SECONDS = 3600


SECRET_IN_URL = re.compile(
    r"(?i)\b(api_key|apikey|token|key|password|secret)=([^&\s\"\']+)"
)


def redact(text: str) -> str:
    """Strip credentials out of anything on its way to a log or the database.

    A failed SerpApi request raises an exception containing the full request
    URL, and that URL carries api_key=... in the clear. Those strings get
    logged AND written to data/prices.db, which is committed to the repo on
    purpose so a stateless runner remembers what routes have cost.

    On a public repo -- which is what gives unlimited Actions minutes --
    that publishes the key. Found 2026-09-19 by the deploy script's own
    secret scan, sitting in the database from a single proxy error.
    """
    return SECRET_IN_URL.sub(r"\1=[REDACTED]", text or "")


def _within_days(stamp: Any, days: int) -> bool:
    """Is this ISO timestamp within the last `days`? Tolerant of junk."""
    try:
        t = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return False
    now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now()
    return (now - t).total_seconds() <= days * 86400


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


class FlightDealBot:
    def __init__(self, config: Config, dry_run: bool = False):
        self.cfg = config
        self.dry_run = dry_run
        self.errors: List[str] = []

        self.hist = History(config.db_path)
        self.scorer = DealScorer(config, self.hist)
        # Drives scan rotation: successive runs sweep different slices.
        self.run_index = self.hist.run_count()

        self.tp: Optional[TravelpayoutsClient] = None
        self.explore: Optional[GoogleTravelExplore] = None
        self.serp: Optional[SerpApiVerifier] = None
        self.rss: Optional[RSSDealWatcher] = None
        self.emailer: Optional[Emailer] = None

        self._init_sources()
        self._init_email()

        self.bag_advisor = self._init_baggage()

        trip = config.trip
        self.rules = config.layover_rules
        log.info("Layover rule: %s", self.rules.describe())
        self.builder = StopoverBuilder(
            rules=self.rules,
            min_nights=trip["min_nights"],
            max_nights=trip["max_nights"],
            max_extra_stops=trip.get("max_extra_stops", 1),
        )

    # ---------- wiring ----------

    def _init_sources(self) -> None:
        s = self.cfg.sources

        if s.get("travelpayouts", {}).get("enabled", True):
            token = env("TRAVELPAYOUTS_TOKEN")
            if token:
                self.tp = TravelpayoutsClient(
                    token=token,
                    marker=env("TRAVELPAYOUTS_MARKER"),
                    market=s["travelpayouts"].get("market", "us"),
                    currency=self.cfg.currency,
                    request_delay=s["travelpayouts"].get("request_delay_seconds", 0.15),
                )
            else:
                self._note_error("TRAVELPAYOUTS_TOKEN not set -- fare scanning is off.")

        key = env("SERPAPI_KEY")

        # The primary fare source. Both this and the verifier below draw on
        # the same SerpApi key and the same monthly allowance, which is why
        # the budget accounting lives in one place (_serpapi_room).
        if s.get("google_explore", {}).get("enabled", True):
            if key:
                self.explore = GoogleTravelExplore(key, currency=self.cfg.currency)
            else:
                self._note_error(
                    "SERPAPI_KEY not set -- the Europe sweep is off, which "
                    "means no fares. This is now the bot's main source."
                )

        if s.get("serpapi", {}).get("enabled", True):
            if key:
                self.serp = SerpApiVerifier(key, currency=self.cfg.currency)
            else:
                log.info("SERPAPI_KEY not set -- skipping Google verification.")

        if s.get("rss", {}).get("enabled", True):
            self.rss = RSSDealWatcher(s["rss"].get("feeds", []))

    def _init_email(self) -> None:
        e = self.cfg.email
        user = env("SMTP_USER")
        password = env("SMTP_PASS")
        if not user or not password:
            if not self.dry_run:
                self._note_error("SMTP_USER / SMTP_PASS not set -- cannot send email.")
            user = user or "bot@example.com"
            password = password or ""

        # The destination address lives in a secret, not in config.yml, so
        # the repo can be made public without publishing your email --
        # and public repos get unlimited free Actions minutes.
        to_address = env("ALERT_EMAIL") or e.get("to") or user

        self.emailer = Emailer(
            smtp_host=e["smtp_host"],
            smtp_port=e["smtp_port"],
            username=user,
            password=password,
            to_address=to_address,
            from_name=e.get("from_name", "Flight Deal Bot"),
            dry_run=self.dry_run or not password,
            timezone=str(self.cfg.alerts.get("timezone", "") or ""),
        )

    def _init_baggage(self) -> Optional[BaggageAdvisor]:
        c = self.cfg.carry_on
        if not c:
            log.info("No carry_on configured -- fares priced without a bag.")
            return None
        bag = BagSpec.from_inches(
            name=c.get("name", "carry-on"),
            height_in=c["height_in"],
            width_in=c["width_in"],
            depth_in=c["depth_in"],
            weight_lb=c.get("weight_lb", 0),
            expandable_in=c.get("expandable_in", 0),
        )
        log.info("Carry-on: %s", bag.describe())
        packed_lb = c.get("packed_weight_lb")
        packed_kg = round(packed_lb * 0.453592, 2) if packed_lb else None
        return BaggageAdvisor(
            bag,
            fee_overrides=c.get("fee_overrides") or {},
            included_overrides=c.get("included_overrides") or {},
            assume_fee_usd=c.get("assume_fee_usd", 45),
            packed_weight_kg=packed_kg,
        )

    def apply_layover_rule(self, deals: List[Deal]) -> List[Deal]:
        """Enforce the two-band rule across every fare, however it arrived.

        Built stopovers are checked exactly, from their own flight times.
        Fares from the API are the harder case: they report a total journey
        duration but never the layover inside it, so a 9-hour wait in
        Frankfurt looks identical to a 90-minute one. Those get an estimate
        (total time minus how long the flying should take) and are dropped
        only when the estimate is clearly past the boundary -- the estimate
        runs high, so a detour shouldn't cost you a good fare.
        """
        rules = self.cfg.layover_rules
        lay_cfg = self.cfg.layover
        margin = float(lay_cfg.get("estimate_margin_hours", 2.5))
        reject_estimated = bool(lay_cfg.get("reject_estimated_dead_zone", True))

        kept: List[Deal] = []
        dropped_built = dropped_estimated = flagged = 0

        for d in deals:
            if d.stopover_code:
                # Built by us: we know every time, so check it exactly.
                if not verify_layover_rule(d, rules):
                    dropped_built += 1
                    continue
                d.layover_hours = d.stopover_hours
                d.layover_band = STOPOVER
                # Recover the hub's local clock times for the description, so
                # a 16h overnight reads as "overnight" rather than a bare
                # number the reader has to interpret.
                arrive_local = depart_local = None
                if len(d.legs) >= 2 and d.stopover_hours:
                    depart_local = d.legs[1].depart_at
                    arrive_local = depart_local - timedelta(hours=d.stopover_hours)
                d.layover_note = rules.explain(
                    d.stopover_hours, arrive_local, depart_local
                )
                kept.append(d)
                continue

            # From the API: infer what we can.
            leg = d.legs[0] if d.legs else None
            est = assess_api_layover(
                origin=d.origin,
                destination=d.destination,
                total_minutes=leg.duration_minutes if leg else None,
                transfers=leg.stops if leg else 0,
                rules=rules,
                margin_hours=margin,
            )
            d.layover_hours = est.hours
            d.layover_band = est.band
            d.layover_estimated = est.hours is not None and (leg.stops if leg else 0) > 0
            d.layover_note = est.reason

            if est.is_known_bad and reject_estimated:
                dropped_estimated += 1
                continue

            if est.band == DEAD_ZONE:
                # Suspected but not certain -- keep it, say so in the email.
                flagged += 1
                d.notes.append(
                    f"Possible long layover ({est.reason}). The airline "
                    f"doesn't publish it -- check before booking."
                )
            elif est.hours is None and (leg.stops if leg else 0) > 0:
                d.notes.append(
                    "Layover length not published by the airline -- verify "
                    "it isn't a long wait before booking."
                )
            kept.append(d)

        if dropped_built or dropped_estimated or flagged:
            log.info(
                "Layover rule: dropped %d built, %d estimated dead-zone; "
                "flagged %d uncertain",
                dropped_built, dropped_estimated, flagged,
            )
        return kept

    def apply_baggage(self, deals: List[Deal]) -> None:
        """Price the carry-on into every fare, and flag where it won't fit.

        Done before scoring, because the bag fee changes which fare is
        actually cheapest -- and on a stitched stopover the fee is charged
        on each separate ticket, which can be three times over.
        """
        if not self.bag_advisor:
            return
        for d in deals:
            assessment = self.bag_advisor.assess(
                airlines=d.airlines,
                segments=d.bag_segments,
            )
            d.bag_fee_usd = assessment.fee_usd
            d.bag_risk = assessment.risk
            d.bag_warnings = assessment.warnings
            if assessment.fee_usd:
                d.notes.append(
                    f"+${assessment.fee_usd:,.0f} for the carry-on across "
                    f"{d.ticket_count} ticket(s) -- included in the price shown"
                )

    def _note_error(self, msg: str) -> None:
        # Redact BEFORE logging as well as before storing: GitHub Actions
        # logs are public on a public repo.
        msg = redact(str(msg))
        log.warning(msg)
        self.errors.append(msg)

    # ---------- scanning ----------

    # ---------- rotation ----------

    def _rotating_slice(self, items: List[Any], count: int, salt: int = 0) -> List[Any]:
        """A different slice of `items` on each run, wrapping around.

        A full sweep of every route is ~3,000 API requests and ~25 minutes.
        That's free on the fare API but not on a GitHub Actions private repo,
        which gets 2,000 minutes a month total. So each run takes a slice and
        successive runs walk through the rest. With runs every 2 hours the
        whole list still gets covered several times a day.
        """
        if count <= 0:
            # "No budget left" -- emphatically NOT "scan the whole list",
            # which is what this used to return and would have fired ~968
            # requests into a 15-minute job timeout.
            return []
        if count >= len(items):
            return list(items)
        start = (self.run_index * count + salt) % len(items)
        doubled = list(items) + list(items)
        return doubled[start : start + count]

    def _route_pairs(self) -> Tuple[List[Tuple[Dict, Dict]], List[Tuple[Dict, Dict]]]:
        """Split origin/destination pairs into always-scan and rotating.

        Priority destinations (Stockholm at 2.00) are never rotated out --
        you said to prioritise them, so they get looked at every single run.
        """
        s = self.cfg.scan
        threshold = float(s.get("always_scan_priority_above", 1.2))
        n_origins = int(s.get("always_scan_origins", 2))

        always: List[Tuple[Dict, Dict]] = []
        rotating: List[Tuple[Dict, Dict]] = []

        for i, origin in enumerate(self.cfg.origins):
            for dest in self.cfg.destinations:
                pair = (origin, dest)
                if i < n_origins and dest.get("priority", 1.0) >= threshold:
                    always.append(pair)
                else:
                    rotating.append(pair)
        return always, rotating

    def _serpapi_room(self, reserve: int = 0) -> int:
        """How many SerpApi searches this run may still spend.

        The Europe sweep and the verifier share one key and one free-tier
        allowance, so they need one accountant between them. `reserve` is
        what the sweep leaves behind for verification -- without it the
        sweep takes the whole daily cap on the first run of the day and the
        cross-check against Google's own price history never runs at all.
        """
        cfg = self.cfg.sources.get("serpapi", {})
        monthly = int(cfg.get("monthly_budget", 220))
        left_month = max(0, monthly - self.hist.api_calls_this_month("serpapi"))
        daily = int(cfg.get("daily_budget", max(1, monthly // 30)))
        left_today = max(0, daily - self.hist.api_calls_today("serpapi"))
        return max(0, min(left_month, left_today) - max(0, reserve))

    def scan_explore(self) -> List[Deal]:
        """"What's cheap from here to anywhere in Europe?" -- in one request.

        This is the primary fare source. A single call returns 45-50 European
        destinations, each with its own cheapest date pair, which is the only
        reason a bot on a 250-request monthly allowance can watch a whole
        continent at all. The per-route scan this replaced needed a request
        per origin x destination x month -- roughly 3,000 a run -- and still
        returned nothing, because its cache had no DFW-to-Europe data in it.

        One call per run, cycling through `rotation`, so successive runs look
        at different trip lengths and origins rather than re-asking the same
        question every four hours.
        """
        if not self.explore:
            return []

        cfg = self.cfg.sources.get("google_explore", {})
        rotation = cfg.get("rotation") or [
            {"origin": self.cfg.origins[0]["code"], "duration": DURATION_ONE_WEEK}
        ]
        want = max(1, int(cfg.get("calls_per_run", 1)))
        reserve = int(cfg.get("reserve_for_verification", 2))

        room = self._serpapi_room(reserve=reserve)
        if room <= 0:
            log.warning(
                "No SerpApi room left today (holding %d back for "
                "verification) -- skipping the Europe sweep this run.",
                reserve,
            )
            return []
        want = min(want, room)

        cities = {d["code"]: d["city"] for d in self.cfg.destinations}
        wanted = self.cfg.destination_codes
        # Off by default: the `bags` parameter is documented but unverified
        # against the live Explore engine, and a rejected request costs a
        # search either way. The carry-on is priced in by apply_baggage
        # instead, which is the tested path.
        bags = 1 if cfg.get("price_with_carry_on", False) else None

        deals: List[Deal] = []
        for i in range(want):
            slot = rotation[(self.run_index + i) % len(rotation)]
            origin = str(slot.get("origin", self.cfg.origins[0]["code"])).upper()
            duration = int(slot.get("duration", DURATION_ONE_WEEK))
            found: List[Deal] = []
            failed = False
            try:
                found = self.explore.scan(
                    origin=origin,
                    wanted_destinations=wanted,
                    city_lookup=cities,
                    area_id=cfg.get("area_id", AREA_EUROPE),
                    travel_duration=duration,
                    max_stops=self.cfg.trip.get("max_extra_stops", 1),
                    carry_on_bags=bags,
                )
            except GoogleDealsError as e:
                self._note_error(f"Europe sweep {origin} (duration {duration}): {e}")
                found, failed = [], True
            finally:
                # Bill what actually went out, including failed requests --
                # SerpApi counts a rejected search against the free tier just
                # the same, and a run that didn't bill itself would quietly
                # blow through the monthly cap.
                spent = self.explore.call_count
                if spent:
                    self.hist.bump_api_calls("serpapi", spent)
                    self.explore.call_count = 0
            if failed:
                break  # a key or quota problem won't resolve itself this run
            deals.extend(found)

        log.info("Europe sweep: %d fares to destinations on your list", len(deals))
        return deals

    def scan_direct_routes(self) -> List[Deal]:
        """Plain round trips. Broad, cheap, and the backbone of the history."""
        if not self.tp:
            return []

        trip = self.cfg.trip
        months = months_ahead(
            trip.get("search_months_start", 1),
            trip.get("search_months_ahead", 10),
        )

        s = self.cfg.scan
        budget = int(s.get("max_requests_per_run", 600))
        stopover_share = float(s.get("stopover_budget_share", 0.45))
        direct_budget = max(1, int(budget * (1 - stopover_share)))

        always, rotating = self._route_pairs()
        used_by_always = len(always) * len(months)
        room = max(0, direct_budget - used_by_always)
        n_rotating = room // max(1, len(months))

        pairs = always + self._rotating_slice(rotating, n_rotating)
        log.info(
            "Direct scan: %d pairs (%d always + %d of %d rotating) x %d months "
            "= ~%d requests",
            len(pairs), len(always), len(pairs) - len(always), len(rotating),
            len(months), len(pairs) * len(months),
        )

        deals: List[Deal] = []

        for origin, dest in pairs:
            try:
                found = self.tp.scan_round_trips(
                    origin=origin["code"],
                    destination=dest["code"],
                    destination_city=dest["city"],
                    months=months,
                    min_nights=trip["min_nights"],
                    max_nights=trip["max_nights"],
                    max_extra_stops=trip.get("max_extra_stops", 1),
                )
                deals.extend(found)
                log.debug("%s-%s: %d fares", origin["code"], dest["code"], len(found))
            except TravelpayoutsError as e:
                self._note_error(f"{origin['code']}-{dest['code']}: {e}")

        log.info("Direct scan: %d fares across %d months", len(deals), len(months))
        return deals

    def scan_stopover_routes(self) -> List[Deal]:
        """The multi-day-layover itineraries you actually asked for."""
        if not self.tp:
            return []

        trip = self.cfg.trip
        all_months = months_ahead(
            trip.get("search_months_start", 1),
            trip.get("search_months_ahead", 10),
        )

        s = self.cfg.scan
        budget = int(s.get("max_requests_per_run", 600))
        stop_budget = max(1, int(budget * float(s.get("stopover_budget_share", 0.45))))

        # Stopovers are the expensive half: every hub needs its own one-way
        # calendar in BOTH directions. So rotate the hubs and the months, and
        # keep the destination list short. Over a day every hub still gets
        # tried against every priority destination.
        months = self._rotating_slice(
            all_months, int(s.get("stopover_months_per_run", 5)), salt=3
        )

        targets = sorted(
            self.cfg.destinations, key=lambda d: d.get("priority", 1.0), reverse=True
        )[:4]
        origins = self.cfg.origins[: int(s.get("always_scan_origins", 2))]

        # Each hub costs (origins + targets) leg-pairs; plus the return legs.
        per_hub = (len(origins) + len(targets)) * len(months)
        returns_cost = len(targets) * len(origins) * len(months)
        n_hubs = max(1, (stop_budget - returns_cost) // max(1, per_hub))
        hubs = self._rotating_slice(self.cfg.stopover_hubs, n_hubs, salt=7)

        log.info(
            "Stopover scan: %d hubs x %d destinations x %d origins x %d months "
            "= ~%d requests",
            len(hubs), len(targets), len(origins), len(months),
            returns_cost + len(hubs) * per_hub,
        )

        deals: List[Deal] = []
        leg_cache: Dict[Tuple[str, str], Dict[date, Dict[str, Any]]] = {}

        def legs(a: str, b: str) -> Dict[date, Dict[str, Any]]:
            key = (a, b)
            if key not in leg_cache:
                try:
                    leg_cache[key] = self.tp.daily_one_way_prices(
                        a, b, months, direct_only=True
                    )
                except TravelpayoutsError as e:
                    self._note_error(f"legs {a}-{b}: {e}")
                    leg_cache[key] = {}
            return leg_cache[key]

        for origin in origins:
            o = origin["code"]
            for dest in targets:
                d = dest["code"]
                return_legs = legs(d, o)

                for hub in hubs:
                    h = hub["code"]
                    if h in (o, d):
                        continue

                    leg_a = legs(o, h)
                    if not leg_a:
                        continue
                    leg_b = legs(h, d)
                    if not leg_b:
                        continue

                    built = self.builder.build(
                        origin=o,
                        hub=h,
                        destination=d,
                        destination_city=dest["city"],
                        hub_city=hub["city"],
                        leg_a=leg_a,
                        leg_b=leg_b,
                        return_legs=return_legs or None,
                        booking_url_fn=self.tp.booking_url if self.tp else None,
                    )
                    if built:
                        log.debug("%s->%s->%s: %d itineraries", o, h, d, len(built))
                    deals.extend(built)

        log.info("Stopover scan: %d itineraries", len(deals))
        return deals

    def scan_rss(self) -> List:
        if not self.rss:
            return []
        cfg = self.cfg.sources.get("rss", {})
        try:
            items = self.rss.fetch_all()
        except Exception as e:
            self._note_error(f"RSS fetch failed: {e}")
            return []

        city_names = {
            d["code"]: d.get("city", d["code"]) for d in self.cfg.destinations
        }
        origin_cities = [o.get("name", "") for o in self.cfg.origins]

        # Aliases are what actually make an origin match. `name` is for the
        # email; a post says "Dallas to Oslo", not "Dallas/Fort Worth".
        origin_aliases = {
            o["code"]: list(o.get("aliases") or [o.get("name", o["code"])])
            for o in self.cfg.origins
        }
        annotated = self.rss.annotate(
            items,
            origin_codes=self.cfg.origin_codes,
            destination_codes=self.cfg.destination_codes,
            city_names=city_names,
            hot_keywords=cfg.get("hot_keywords", []),
            origin_city_names=origin_cities,
            origin_aliases=origin_aliases,
        )
        seen = {i.guid for i in annotated if self.hist.rss_seen(i.guid)}
        fresh = self.rss.relevant(
            annotated,
            max_price=self.cfg.thresholds.get("max_price_usd"),
            seen=seen,
        )
        # NOT marked seen here. Marking on FETCH meant an item found by a
        # non-digest run (6 of every 7) was recorded as seen, never emailed,
        # and then filtered out of the digest run that would have sent it.
        # The post was destroyed silently and the log read "3 relevant new
        # posts" either way. These are the mistake-fare feeds -- the fastest
        # signal the bot has. They are marked seen in dispatch(), once they
        # have actually gone out.
        log.info("RSS: %d relevant new posts (of %d)", len(fresh), len(items))
        return fresh

    # ---------- verification ----------

    def verify(self, candidates: List[Deal]) -> Dict[str, PriceInsight]:
        """Spend SerpApi searches on the best candidates only."""
        if not self.serp or not candidates:
            return {}

        cfg = self.cfg.sources.get("serpapi", {})

        # No reserve here: verification is the thing the reserve was being
        # held for, so at this point it may spend whatever is left. Pacing
        # (monthly cap, daily cap) is handled inside _serpapi_room -- the
        # month's allowance can't be burned in the first week, because a
        # fare worth verifying is as likely on the 28th as on the 2nd.
        room = self._serpapi_room()
        if room == 0:
            log.info(
                "No SerpApi searches left to verify with (%d used today, "
                "%d this month).",
                self.hist.api_calls_today("serpapi"),
                self.hist.api_calls_this_month("serpapi"),
            )
            return {}

        limit = min(int(cfg.get("max_verifications_per_run", 5)), room)
        insights: Dict[str, PriceInsight] = {}

        for deal in candidates[:limit]:
            before = self.serp.call_count
            try:
                insight = self.serp.verify(
                    origin=deal.origin,
                    destination=deal.destination,
                    outbound_date=deal.depart_date.isoformat(),
                    return_date=(
                        deal.return_date.isoformat() if deal.return_date else None
                    ),
                    max_stops=self.cfg.trip.get("max_extra_stops", 1),
                )
                insights[deal.fingerprint()] = insight
                log.info(
                    "Verified %s: Google typical %s-%s, level=%s",
                    deal.route,
                    insight.typical_low,
                    insight.typical_high,
                    insight.price_level,
                )
            except SerpApiError as e:
                self._note_error(f"SerpApi verify {deal.route}: {e}")
                break  # quota or key problem: stop burning calls
            finally:
                # Bill whatever left the machine, success or not. Billing
                # only on success under-reported the ledger during a flaky
                # week, so _serpapi_room saw headroom that wasn't there and
                # the real 250/month tier ran out early -- after which the
                # Europe sweep, the only fare source, starts 429-ing.
                spent = self.serp.call_count - before
                if spent > 0:
                    self.hist.bump_api_calls("serpapi", spent)

        return insights

    # ---------- the run ----------

    def run(self, send_digest: bool = False) -> int:
        run_id = self.hist.start_run()
        log.info("Run %d starting (dry_run=%s)", run_id, self.dry_run)

        raw: List[Deal] = []
        # Primary source first: one request, all of Europe. The two scans
        # below run on Travelpayouts, which is disabled by default because
        # its cache holds no DFW-to-Europe fares at all.
        raw.extend(self.scan_explore())
        raw.extend(self.scan_direct_routes())
        raw.extend(self.scan_stopover_routes())

        # The layover rule, applied to every fare however it arrived.
        before = len(raw)
        raw = self.apply_layover_rule(raw)
        if before != len(raw):
            log.info("Layover rule removed %d of %d fares", before - len(raw), before)

        # Price the carry-on in BEFORE scoring, so every comparison downstream
        # is on what you'd actually pay.
        self.apply_baggage(raw)

        # NOTE: observations are written at the END of the run, not here.
        # Scoring must compare a fare against what was known BEFORE this run,
        # otherwise a batch of cheap itineraries silently becomes its own
        # benchmark and makes itself look ordinary.

        # First pass: score against history, no API spend.
        scored: List[Deal] = []
        for deal in raw:
            self.scorer.score(deal, exclude_recent_seconds=RUN_WINDOW_SECONDS)
            ok, _ = self.scorer.passes_filters(deal)
            if ok:
                scored.append(deal)
        log.info("%d candidates passed filters", len(scored))

        # Second pass: verify the best, then re-score with Google's numbers.
        limit = int(
            self.cfg.sources.get("serpapi", {}).get("max_verifications_per_run", 5)
        )
        to_verify = self.scorer.verification_candidates(scored, limit)
        insights = self.verify(to_verify)

        final: List[Deal] = []
        for deal in scored:
            insight = insights.get(deal.fingerprint())
            if insight:
                self.scorer.score(
                    deal, insight=insight, exclude_recent_seconds=RUN_WINDOW_SECONDS
                )
                ok, reason = self.scorer.passes_filters(deal)
                if not ok:
                    log.info("Dropped after verification (%s): %s", reason, deal.summary())
                    continue
            final.append(deal)

        final.sort(key=lambda d: d.score, reverse=True)

        rss_items = self.scan_rss()

        # Scoring is done, so everything observed goes into the record book
        # now -- deal or not. This is what makes "lowest ever" mean anything
        # on the next run.
        #
        # Deliberately BEFORE dispatch. It used to come after, so any
        # exception while composing or sending an email (a missing field, an
        # encoding error) threw away the whole run's price observations,
        # while rows already written to `alerts` survived -- leaving the
        # dedupe record without the price record it was supposed to match.
        # Sending is the failure-prone step; the data is the valuable part.
        n_obs = self.hist.record_observations(raw)
        log.info("Recorded %d observations", n_obs)

        sent = self.dispatch(final, rss_items, send_digest=send_digest)

        self.hist.prune()
        self._check_for_silence(n_obs)
        self.hist.finish_run(
            run_id,
            observations=n_obs,
            candidates=len(final),
            alerts_sent=sent,
            errors=self.errors,
        )
        log.info("Run %d done: %d candidates, %d emails", run_id, len(final), sent)
        return sent

    def heartbeat(self) -> bool:
        """A weekly "I am alive and here is what I have seen" email.

        This is the most important signal the bot produces, and it exists
        because of an asymmetry: the bot is DESIGNED to be silent for weeks,
        so silence carries no information. A healthy quiet bot and a dead
        one look identical from the inbox.

        _check_for_silence covers the case where the bot runs but sees
        nothing. It cannot cover the cases where the bot does not run at
        all -- GitHub disabling the schedule after 60 days of inactivity,
        the workflow file breaking, Actions being turned off, the repo being
        archived, a dropped scheduled run. In every one of those the bot is
        not executing, so no check inside it can fire.

        A heartbeat inverts that. The owner stops needing to detect a
        failure and only has to notice the absence of a routine message,
        which is something people are good at. It also surfaces the slow
        rot that nothing else reports: how close fares are getting to the
        alert bar, whether the discount threshold has quietly become
        unreachable, and how much API budget is left.
        """
        runs = [r for r in self.hist.recent_runs(80) if r["finished_at"]]
        week = [r for r in runs if _within_days(r["started_at"], 7)]
        obs_week = sum((r["observations"] or 0) for r in week)
        alerts_week = sum((r["alerts_sent"] or 0) for r in week)
        failed = [r for r in week if r["errors"] and r["errors"] != "[]"]

        used_month = self.hist.api_calls_this_month("serpapi")
        budget = int(self.cfg.sources.get("serpapi", {}).get("monthly_budget", 240))

        L: List[str] = []
        L.append("Weekly check-in from the flight bot. Nothing is wrong --")
        L.append("this arrives every week so that its ABSENCE means something.")
        L.append("")
        L.append("If you ever stop getting this, the bot has stopped running.")
        L.append("Check https://github.com/ -> your flight-deal-bot repo -> Actions.")
        L.append("")
        L.append("-" * 58)
        L.append("LAST 7 DAYS")
        L.append("-" * 58)
        L.append(f"  Runs completed:      {len(week)}")
        L.append(f"  Fares recorded:      {obs_week:,}")
        L.append(f"  Alerts emailed:      {alerts_week}")
        L.append(f"  Runs with errors:    {len(failed)}")
        # A run that recorded nothing is not an error and shows a green
        # check, but it means no fare data. Four in a row happened on
        # 2026-09-20 when the rolling API cap starved the sweep, and
        # nothing surfaced it. This line is how that becomes visible.
        blank = [r for r in week if not (r["observations"] or 0)]
        L.append(f"  Runs that saw NO fares: {len(blank)}"
                 + ("   <-- worth a look" if len(blank) > 2 else ""))
        L.append(f"  SerpApi used:        {used_month} of {budget} this month")
        L.append("")

        # The part that catches the slow rot: how close is anything getting?
        L.append("-" * 58)
        L.append("HOW CLOSE ARE FARES TO ALERTING?")
        L.append("-" * 58)
        L.append("  If every route sits far below its bar for months, the")
        L.append("  thresholds have drifted out of reach and need lowering.")
        L.append("")
        home = str(self.cfg.thresholds.get("baseline_origin", "DFW")).upper()
        watch = sorted(
            self.cfg.destinations,
            key=lambda d: -float(d.get("priority", 1.0)),
        )[:8]
        any_row = False
        for d in watch:
            key = f"{home}-{d['code']}"
            st = self.hist.route_stats(key)
            if not st["count"]:
                continue
            any_row = True
            base = self.cfg.baseline(d["code"]) or 0
            gate = min(
                base * (1 - float(self.cfg.thresholds["tiers"]["watch"]) / 100),
                float(self.cfg.thresholds["max_price_usd"]),
            ) if base else float(self.cfg.thresholds["max_price_usd"])
            low = float(st["min"])
            gap = (low - gate) / gate * 100 if gate else 0
            L.append(
                f"  {d['city'][:16]:<16} best ${low:>6,.0f}   needs ${gate:>6,.0f}"
                f"   ({gap:+.0f}%)   {st['count']} seen"
            )
        if not any_row:
            L.append("  No price history yet. Normal in the first week.")
        L.append("")
        L.append("  A negative % means a fare has already beaten the bar.")
        L.append("")

        if failed:
            L.append("-" * 58)
            L.append("ERRORS THIS WEEK")
            L.append("-" * 58)
            for r in failed[:5]:
                L.append(f"  {str(r['started_at'])[:16]}  {str(r['errors'])[:110]}")
            L.append("")

        body = "\n".join(L)
        subject = (
            f"[Flights] Weekly check-in -- {obs_week:,} fares seen, "
            f"{alerts_week} alert{'s' if alerts_week != 1 else ''}"
        )
        try:
            return bool(self.emailer.send(subject, body))
        except EmailError as e:
            self._note_error(f"Heartbeat email failed: {e}")
            return False

    def _hours_since_last_fares(self) -> Optional[float]:
        """Real elapsed time since a run last saw a fare.

        Beats multiplying the streak by an assumed cadence: the schedule
        fires 7 times a day, not 6, and runs get delayed or dropped by
        GitHub, so any fixed number here would be wrong.
        """
        for row in self.hist.recent_runs(1000):
            if row["finished_at"] is None or not (row["observations"] or 0):
                continue
            try:
                seen = datetime.fromisoformat(str(row["started_at"]))
            except (TypeError, ValueError):
                return None
            now = datetime.now(seen.tzinfo) if seen.tzinfo else datetime.now()
            return max(0.0, (now - seen).total_seconds() / 3600)
        return None

    def _check_for_silence(self, observations_this_run: int) -> None:
        """Email if the bot has stopped seeing fares at all.

        The dangerous failure here is not a crash, it is a shrug. If SerpApi
        renames a field, changes how arrival_area_id works, or starts
        returning an empty list for an area it no longer supports, explore()
        returns [] with no exception. The run logs "Europe sweep: 0 fares",
        records nothing, exits 0, and GitHub shows a green check -- forever.
        Nobody reads the logs of a passing job, so the bot could be dead for
        months and look perfectly healthy.

        Zero observations on one run is normal. Zero on every run for a day
        is not, and is worth an email even though it is not a deal.
        """
        if observations_this_run > 0:
            return

        a = self.cfg.alerts
        threshold = int(a.get("silence_warning_after_runs", 6))
        if threshold <= 0:
            return

        # Count back through finished runs until one found something. The
        # lookback has to be far longer than the threshold: it used to be
        # threshold*3, so the streak SATURATED at 18 -- and 18 is a multiple
        # of 6, so the "only every Nth run" guard never suppressed anything
        # again. A dead bot sent 7 identical emails a day, forever. The
        # realistic response to that is a Gmail filter, which would also
        # bury every real deal alert. The cure was worse than the disease.
        streak = 1  # this run
        for row in self.hist.recent_runs(1000):
            if row["finished_at"] is None:
                continue
            if (row["observations"] or 0) > 0:
                break
            streak += 1

        # Escalating, not repeating: warn at 1x, 2x, 4x, 8x ... the
        # threshold. Roughly 10 emails over a year of death instead of
        # 2,500, while still nagging often enough early on to be noticed.
        if streak < threshold:
            return
        multiple, rem = divmod(streak, threshold)
        if rem != 0 or (multiple & (multiple - 1)) != 0:   # not a power of two
            return

        # Report the real elapsed time rather than assuming a cadence.
        hours = self._hours_since_last_fares()
        log.warning(
            "No fares in %d consecutive runs (%s) -- warning by email.",
            streak, f"{hours:.0f}h" if hours else "unknown",
        )
        try:
            span = f"about {hours:.0f} hours" if hours else "an unknown span"
            self.emailer.send(
                "[Flights] Heads up: the bot has stopped finding fares",
                f"The last {streak} runs ({span}) all returned "
                f"zero fares.\n\n"
                f"That is not the same as 'no deals right now' -- the bot "
                f"records every fare it sees, deal or not, so zero means it "
                f"is not seeing the data at all.\n\n"
                f"Most likely causes, in order:\n"
                f"  1. The SerpApi free tier is exhausted for the month.\n"
                f"  2. The SERPAPI_KEY secret expired or was regenerated.\n"
                f"  3. Google Travel Explore changed its response format.\n\n"
                f"Check the most recent run's log under the Actions tab.\n\n"
                f"Errors recorded this run:\n"
                + ("\n".join(f"  - {e}" for e in self.errors) or "  (none)")
                + "\n",
            )
        except EmailError as e:
            log.error("Couldn't send the silence warning: %s", e)

    def dispatch(
        self, deals: List[Deal], rss_items: List, send_digest: bool = False
    ) -> int:
        """Send what's worth sending, respecting dedupe and daily caps."""
        a = self.cfg.alerts
        dedupe_hours = int(a.get("dedupe_hours", 72))
        daily_cap = int(a.get("max_emails_per_day", 6))
        max_per_email = int(a.get("max_deals_per_email", 12))

        # Count EMAILS, not deals. record_alert() writes one row per deal, so
        # comparing that count to max_emails_per_day meant a single email
        # carrying 7 deals tripped a cap of 6 -- and the bot went silent for
        # 24 hours immediately after finding a sale, which is exactly when a
        # follow-up error fare is most likely.
        already = self.hist.emails_sent_since(24)
        if already >= daily_cap and not send_digest:
            log.info(
                "Daily email cap reached (%d of %d emails in 24h).",
                already, daily_cap,
            )
            return 0

        fresh = [
            d
            for d in deals
            if not self.hist.was_alerted(d.fingerprint(), dedupe_hours)
        ]
        if len(fresh) != len(deals):
            log.info("Suppressed %d already-alerted deals", len(deals) - len(fresh))

        urgent = [d for d in fresh if self.scorer.should_alert_now(d)]
        rest = [d for d in fresh if d not in urgent]

        sent = 0

        if urgent:
            try:
                if self.emailer.send_deals(
                    urgent, urgent=True, max_deals=max_per_email
                ):
                    # One id for the whole message: the daily cap counts
                    # messages, and it must not depend on how long this loop
                    # takes to run.
                    email_id = uuid.uuid4().hex
                    for d in urgent[:max_per_email]:
                        self.hist.record_alert(d, email_id=email_id)
                    sent += 1
            except EmailError as e:
                self._note_error(str(e))

        rss_sent: List[Any] = []

        # A hot post goes out on its own run. It used to be skipped entirely
        # whenever the same run also had an urgent deal ("and not urgent"),
        # and since it had already been marked seen it never came back.
        hot_rss = [i for i in rss_items if i.is_hot]
        if hot_rss:
            try:
                if self.emailer.send_deals([], rss_items=hot_rss):
                    rss_sent.extend(hot_rss)
                    sent += 1
            except EmailError as e:
                self._note_error(str(e))

        # Anything not already mailed above waits for the digest.
        held_rss = [i for i in rss_items if i not in rss_sent]
        if send_digest and (rest or held_rss):
            try:
                if self.emailer.send_deals(
                    rest, urgent=False, rss_items=held_rss, max_deals=max_per_email
                ):
                    email_id = uuid.uuid4().hex
                    for d in rest[:max_per_email]:
                        self.hist.record_alert(d, email_id=email_id)
                    rss_sent.extend(held_rss)
                    sent += 1
            except EmailError as e:
                self._note_error(str(e))
        elif rest or held_rss:
            log.info(
                "Holding %d deals and %d posts for the next digest",
                len(rest), len(held_rss),
            )

        # Only now is an item really "seen". Anything that didn't go out
        # stays unseen and gets another chance on the next run.
        for item in rss_sent:
            self.hist.mark_rss_seen(item.guid)

        return sent

    # ---------- reporting ----------

    def stats(self) -> str:
        lines = ["", "FLIGHT DEAL BOT -- what it has learned", "=" * 52]
        routes: Dict[str, Dict[str, Any]] = {}
        for o in self.cfg.origins[:2]:
            for d in self.cfg.destinations:
                # Plain round trips, plus each stopover variant, are separate
                # price series (see Deal.history_key).
                keys = [f"{o['code']}-{d['code']}"]
                keys += [
                    f"{o['code']}-{h['code']}-{d['code']}"
                    for h in self.cfg.stopover_hubs
                ]
                for key in keys:
                    s = self.hist.route_stats(key)
                    if s["count"]:
                        routes[key] = s

        if not routes:
            lines.append("No observations yet. Run the bot to start building history.")
        else:
            lines.append(f"{'Route':<18}{'Obs':>6}{'Low':>10}{'P10':>10}{'Median':>10}")
            lines.append("-" * 54)
            for route, s in sorted(
                routes.items(), key=lambda kv: kv[1]["min"] or 1e9
            ):
                lines.append(
                    f"{route:<18}{s['count']:>6}"
                    f"{s['min']:>10,.0f}{s['p10']:>10,.0f}{s['median']:>10,.0f}"
                )

        sp = self.cfg.sources.get("serpapi", {})
        used = self.hist.api_calls_this_month("serpapi")
        today = self.hist.api_calls_today("serpapi")
        budget = sp.get("monthly_budget", 200)
        daily = sp.get("daily_budget", max(1, budget // 30))
        lines.append("")
        lines.append("API budgets:")
        lines.append(f"  SerpApi       : {used}/{budget} this month, "
                     f"{today}/{daily} today")
        lines.append(f"     (the Europe sweep and the verifier share this)")
        tp_on = self.cfg.sources.get("travelpayouts", {}).get("enabled", False)
        lines.append(
            "  Travelpayouts : no cap"
            if tp_on
            else "  Travelpayouts : disabled (no DFW-Europe data in its cache)"
        )
        lines.append(f"  RSS feeds     : unlimited")

        lines.append("")
        lines.append("Recent runs:")
        for r in self.hist.recent_runs(5):
            lines.append(
                f"  {r['started_at'][:16]}  obs={r['observations']:<6} "
                f"candidates={r['candidates']:<4} emails={r['alerts_sent']}"
            )
        return "\n".join(lines)

    def bag_report(self) -> str:
        """Where the carry-on fits, where it doesn't, and what it costs."""
        if not self.bag_advisor:
            return "No carry_on configured in config.yml."

        bag = self.bag_advisor.bag
        lines = ["", bag.describe(), "=" * 74, ""]
        lines.append(
            f"{'Airline':<20}{'Limit':>12}{'Over':>7}"
            f"{'Enforces':>10}{'Risk':>10}{'Bag fee':>9}"
        )
        lines.append("-" * 74)

        codes = sorted(POLICIES.keys(), key=lambda c: POLICIES[c].name)
        rows = []
        for code in codes:
            a = self.bag_advisor.assess([code], segments=2)
            info = a.per_airline[code]
            over = info["max_overage_cm"]
            over_s = "fits" if info["fits"] else f"{over:.1f}cm"
            fee = f"${a.fee_usd:,.0f}" if a.fee_usd else "incl"
            rows.append((
                a.risk_score, info["airline"], info["limit_cm"], over_s,
                info["size_enforcement"], a.risk, fee,
            ))

        for score, name, limit, over_s, enf, risk, fee in sorted(rows):
            lines.append(
                f"{name:<20}{limit:>12}{over_s:>7}{enf:>10}{risk:>10}{fee:>9}"
            )

        lines.append("")
        lines.append("'Over' is versus the published limit. 'Enforces' is how")
        lines.append("strictly that airline checks SIZE in practice, from traveler")
        lines.append("reports -- which is why a bag can be 3cm over and still low")
        lines.append("risk. 'Risk' blends both, plus the weight cap against your")
        lines.append("configured packed weight. Sorted least risky first.")
        return "\n".join(lines)

    def close(self) -> None:
        self.hist.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Flight deal bot")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument(
        "--dry-run", action="store_true", help="print emails instead of sending"
    )
    parser.add_argument(
        "--digest", action="store_true", help="also send the daily digest"
    )
    parser.add_argument("--stats", action="store_true", help="show learned price history")
    parser.add_argument(
        "--bag", action="store_true",
        help="show how your carry-on measures up against each airline",
    )
    parser.add_argument("--test-email", action="store_true", help="send a test email")
    parser.add_argument(
        "--heartbeat", action="store_true",
        help="send the weekly 'still alive' email (see heartbeat())",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    try:
        config = Config.load(args.config)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    bot = FlightDealBot(config, dry_run=args.dry_run)
    try:
        if args.bag:
            print(bot.bag_report())
            return 0

        if args.stats:
            print(bot.stats())
            return 0

        if args.heartbeat:
            ok = bot.heartbeat()
            print("Heartbeat sent." if ok else "Heartbeat FAILED to send.")
            return 0 if ok else 1

        if args.test_email:
            ok = bot.emailer.send(
                "[Flights] Test email -- the bot is wired up",
                "If you're reading this, SMTP works and the bot can reach you.\n\n"
                f"Watching {len(config.destinations)} destinations from "
                f"{len(config.origins)} airports, with a "
                f"{config.layover_hours['min']:.0f}h minimum stopover.\n",
            )
            print("Sent." if ok else "Failed.")
            return 0 if ok else 1

        bot.run(send_digest=args.digest)
        # An error is an error. This used to return 0 whenever any email had
        # gone out, so a run where the Europe sweep 401'd but a stale RSS
        # post got mailed reported success and showed a green check.
        return 1 if bot.errors else 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        bot.close()


if __name__ == "__main__":
    sys.exit(main())
