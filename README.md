> **READ THIS FIRST — much of what follows is out of date.**
>
> This README was written when the bot ran on Travelpayouts with very
> different thresholds. On 2026-09-19 the fare source was replaced and every
> number was recalibrated against real measured prices. Sections below still
> describe the old design.
>
> **`config.yml` is the truth.** It carries the current values and, for each
> one, why it is what it is. When this file and `config.yml` disagree,
> `config.yml` is right.
>
> What actually changed:
>
> | | Old (this README) | Now (`config.yml`) |
> |---|---|---|
> | Fare source | Travelpayouts / Aviasales | **Google Travel Explore** via SerpApi |
> | Why | — | Travelpayouts' cache holds no DFW→Europe fares at all |
> | Price ceiling | $499 | **$625**, all-in |
> | Minimum discount | 35% | **18%** |
> | Instant-alert tier | 55% off | **32% off** |
> | Schedule | every 3 hours | **6 runs/day**, budget-bound |
> | SerpApi budget | 220/month, 7/day | **240/month, 9/day** — and it is now the *only* source |
> | Extra | — | weekly heartbeat email, silence detector, 180-day price window |
>
> The old baselines were guesses and several were below the routes' real
> floors, which made alerts arithmetically impossible. See the comments in
> `config.yml` for the measurements.

# Flight Deal Bot

Scans for heavily discounted flights from Dallas to Europe around the clock,
prioritizes Stockholm, builds itineraries with a **multi-day stopover** so you
get a second city on the way, refuses dead-zone layovers, and emails you only
when a fare is at or below the lowest it has ever been.

---

## Setup (about 20 minutes, all free)

### Run one command

```bash
cd ~/Documents/flight-deal-bot
./setup.sh
```

It installs dependencies, runs the tests, asks for your credentials one at a
time (explaining what each is and where to get it), saves them to a `.env`
file that is gitignored and readable only by you, and sends a test email to
prove the chain works end to end.

It also cleans up the usual paste damage — wrapping quotes, a trailing comma,
an accidental `export` prefix — and refuses the literal `...` placeholder
from these docs. Re-run it any time to change a value; it remembers what you
already entered.

**Why a `.env` file rather than `export`:** shell exports vanish when you
close Terminal, so you'd re-type them constantly. And one stray comma sets a
variable to the wrong value silently, producing a failure much later and far
from the cause.

### What you'll need before running it

| | Where to get it | Required |
|---|---|---|
| **Travelpayouts token** | Register at <https://www.travelpayouts.com/>, join the **Aviasales** program, copy from <https://app.travelpayouts.com/profile/api-token> | yes |
| **Gmail app password** | Turn on 2FA at <https://myaccount.google.com/security>, then create one at <https://myaccount.google.com/apppasswords> | yes |
| **SerpApi key** | <https://serpapi.com/users/sign_up> — 250 free searches/month | no |

Gmail rejects your normal password from a script, so the app password isn't
optional. It's 16 characters in four groups; spaces are fine either way.

### Then try a real scan

```bash
python3 -m bot.main --dry-run -v    # prints what it found, sends nothing
python3 -m bot.main --stats         # what it has learned about prices
python3 -m bot.main --bag           # your carry-on vs. every airline
```

### 6. Put it on GitHub Actions (this is the always-on part)

GitHub runs the bot on **their** servers, on a schedule. Your laptop can be
closed, off, or at the bottom of a lake. Nothing runs locally.

1. Push this repo to GitHub.
2. **Settings → Secrets and variables → Actions → New repository secret.**
   Add these:

   | Secret | Required | What it is |
   |---|---|---|
   | `TRAVELPAYOUTS_TOKEN` | yes | From step 2 |
   | `SMTP_USER` | yes | Your Gmail address |
   | `SMTP_PASS` | yes | The 16-character app password from step 3 |
   | `ALERT_EMAIL` | no | Where alerts go. Defaults to `SMTP_USER`. |
   | `SERPAPI_KEY` | no | From step 4 |
   | `TRAVELPAYOUTS_MARKER` | no | Your affiliate ID |

3. **Settings → Actions → General → Workflow permissions** → **Read and write
   permissions**. Without this the bot can't save its price history and
   "lowest ever" never works.
4. **Actions** tab → *Flight deal scan* → **Run workflow**. The first step
   checks your secrets and fails loudly with the missing names if any are
   absent.

That's it. It now runs every 3 hours plus a 7am digest, indefinitely.

### Public or private repo? It changes the frequency you can afford

`config.yml` contains **no personal information** — your email lives in the
`ALERT_EMAIL` secret — so the repo is safe to make public. That matters:

| | Actions minutes | Practical frequency |
|---|---|---|
| **Public repo** | unlimited, free | hourly or better |
| **Private repo** | 2,000/month | every 3 hours |

GitHub rounds every run up to a whole minute, and with runner startup each
scan bills about 5 minutes. So on a private repo:

```
every 3 hours   9 runs/day   ~1,350 min/month   <- the default, safe
every 2 hours  13 runs/day   ~1,950 min/month   <- too close to the cap
every 1 hour   25 runs/day   ~3,750 min/month   <- public repo only
```

To go faster, make the repo public and change the cron in
`.github/workflows/scan.yml` to `15 * * * *`.

### Why a run is only ~4 minutes

A full sweep of every origin × destination × month is ~3,000 API requests and
about 25 minutes — which would blow a private repo's whole monthly quota in
six days. So each run scans a **slice** and rotates:

- **Stockholm and the other priority destinations are scanned every run**,
  never rotated out.
- Everything else cycles. The full 120-route list is covered about every 11
  hours, and all 18 stopover hubs about every 17 hours.
- Raise `scan.max_requests_per_run` in `config.yml` if you go public.

### It won't quietly stop

Two things to know:

- GitHub disables scheduled workflows in repos with **60 days of no
  activity**. The bot commits its price database on every run, which counts
  as activity, so this won't bite you.
- If a run fails, GitHub emails you. Check the **Actions** tab for the log.

---

## How it decides something is "heavily discounted"

You said you meant it, so the bar is deliberately high. A fare must clear
**all** of these:

| Gate | Default | Meaning |
|---|---|---|
| Hard ceiling | `$499` | **All-in** (fare + carry-on fee) |
| Minimum discount | `35%` | Below the toughest available benchmark |
| Must be a record | `on` | At or below the lowest ever seen (±2%) |
| Tier for instant email | `great` (55% off) | Anything less waits for the digest |

### Every alert shows normal vs. found

```
Normally:   $653   (Google range $580-$980)
You pay:    $238
You save:   $415 (64% off normal)
Based on:   median of 61 observations
```

"Normally" is the **median** of what the bot has observed for that exact
route — deliberately not the same number as the alert threshold below it.
The threshold is a 10th-percentile figure, a deliberately tough bar a fare
must beat; quoting that as "normal" would understate what the trip usually
costs you. Where there's no history yet, it falls back to Google's typical
range midpoint, then your configured baseline, and says which it used.

### The benchmark problem, and how it's handled

No public API sells "lowest price this route has ever been." Google shows a
price graph in its UI but exposes only a partial series. Fare caches know the
last 48 hours. So the bot **builds its own record book**: every price it sees,
on every run, goes into `data/prices.db`.

Three reference prices are computed, and **the lowest one wins** — so a fare
has to beat the toughest benchmark available, not the most flattering:

1. **The bot's own history** (10th percentile), once it has 8+ observations
2. **Google's typical price range** (via SerpApi, on verified candidates)
3. **Your seeded baseline** from `config.yml`, used on day one

The baselines shipped in `config.yml` were checked against live fares on
2026-09-18: DFW→Stockholm round trips for Nov–Feb were running **$609–675**
on Finnair via Helsinki. The $609 fare carried NO cabin bag; the ~$620-625
ones did, so $650 all-in is the seeded normal for ARN.

### What to expect early on

**The first few runs will be quiet, by design.** A route with no history can't
produce a record, and "lowest ever" is what you asked for. Two exceptions keep
it from being useless on day one:

- A fare in the top tier (65%+ below baseline) alerts immediately even with no
  history — a $238 Stockholm fare shouldn't be dropped because the bot is new.
- The error-fare feeds work from the very first run.

After about a week the bot has real history and the record logic does the
heavy lifting. Check progress any time with:

```bash
python -m bot.main --stats
```

### If it's too quiet or too noisy

In `config.yml` under `thresholds`:

- **Too quiet?** Set `require_record: false`, or drop `min_discount_pct` to 25,
  or raise `max_price_usd`.
- **Too noisy?** Raise `min_discount_pct`, or set `instant_alert_tier: insane`
  so only probable error fares interrupt you.

---

## Your carry-on

The bot is configured for a **Samsonite Freeform Carry-On Spinner**:
23 × 15 × 10 in = **58.4 × 38.1 × 25.4 cm**, 2.95 kg empty — the overall
dimensions, which is what airline sizers measure.

That is over the **published** limit everywhere. But published limits are not
what happens at the gate, so the bot rates risk by **enforcement**, from
traveler reports rather than carrier marketing:

```bash
python -m bot.main --bag      # the full table, least risky first
```

| Enforcement | Airlines | Your risk |
|---|---|---|
| **Lenient** — rarely measured | American, Delta, United, British Airways, Iberia, KLM, Air France, easyJet, Lufthansa, Finnair | **low** |
| **Moderate** — spot checks | SAS, Swiss, Austrian, Turkish, Norwegian, Icelandair, TAP | **tight** |
| **Strict** — sizer at every gate | Ryanair, Eurowings, Wizz Air, Aer Lingus, Vueling | **oversize** |

Two things this gets right that a pure centimetre comparison did not:

- **US majors are low risk**, matching real experience. Not zero, though:
  Pack Hacker tested this exact bag and had it gate-checked by Delta on a
  return leg, calling compliance "hit or miss."
- **Finnair is lenient on size but does weigh.** A traveler on the Rick
  Steves forum: *"No evidence of weighing or even measuring at gate."* SAS is
  the opposite — known for weighing bags at the gate, even in SAS Plus,
  though less likely on a long-haul from the US than within Europe.

### Where you'd actually get caught

Not the transatlantic leg — the **intra-Europe hop**, which is exactly what
the stopover feature adds. Ryanair, Wizz and Eurowings put a sizer at every
gate, and Ryanair's 20 cm depth limit is the tightest in the table. A
DFW→Helsinki leg is a non-issue; Helsinki→Stockholm on a budget carrier is
where the bag becomes a problem.

### Weight is the other constraint

Finnair, SAS and Lufthansa cap cabin baggage at **8 kg total**. Your bag is
2.95 kg empty, leaving about **5 kg (11 lb)**. Set `packed_weight_lb` in
`config.yml` to what you actually carry and the bot factors it into the risk
score. Never use the 1" expansion at the gate.

### What the bot does about it

1. **Prices the bag into every fare** — the $499 ceiling, discounts and
   records all run on fare **+ carry-on fee**.
2. **Charges the fee per one-way ticket.** A 3-leg Finnair stopover is
   3 × $55 = **$165**, turning a $383 headline into $548 all-in.
3. **Ranks by enforcement risk**: +10% for lenient carriers, −20% for strict
   ones. Set `skip_oversize_bag_routes: true` to drop strict carriers
   entirely — that gate now targets Ryanair and friends, not American.

### On the fee estimates

The per-airline fees in `bot/baggage.py` are **estimates** and vary by route
and timing. Tune them as you learn real numbers:

```yaml
carry_on:
  fee_overrides:
    AY: 55      # Finnair
    SK: 40      # SAS
  included_overrides:
    AY: false   # force "cheapest fare has no cabin bag"
```

One correction worth knowing: the EU's new passenger rules, approved July
2026 and effective 2027, guarantee a free **personal item of 40 × 30 × 15 cm**
— *not* a free full-size cabin bag. Your bag will still cost extra on
low-cost carriers.

---

## The layover rule

Two acceptable shapes, with a deliberate gap between them:

| Layover | Verdict | Why |
|---|---|---|
| **0 – 5h** | Fine | A normal connection. Nothing special needed. |
| **5 – 14h** | **Rejected** | The dead zone. Too long to sit in a terminal, too short to be worth a hotel. |
| **14 – 24h** | **Depends on the clock** | A stopover only if it actually spans a night. See below. |
| **24 – 120h** | The good one | A real stay. Sleep there, see the city, fly on. |
| **over 120h** | Rejected | That's two trips, not a stopover. |

The dead zone is the point. A 9-hour layover in Frankfurt is the worst thing
a cheap fare can hide, and a price-sorted search hands them out constantly.

### Why 14–24h depends on the clock, not the duration

Below a full day, duration alone is a bad test. Two layovers of **exactly 14
hours**:

| Layover | Spans a night? | Verdict |
|---|---|---|
| 18:00 → 08:00 next day | yes | **Stopover.** A hotel, a dinner, a morning. |
| 08:00 → 22:00 same day | no | **Rejected.** A long day dragging a suitcase with nowhere to leave it. |
| 14:00 → 04:00 next day | no | **Rejected.** You'd be leaving at 4am. |

Identical on a stopwatch, completely different trips. So the bot checks
whether the gap covers the quiet hours (01:00–06:00 local **at the stopover
city**, configurable via `night_core_start` / `night_core_end`). Past 24
hours the check is skipped — a night is guaranteed whatever the clock says.

The times compared are local to the hub. The onward flight's departure time
already is, so the arrival is derived from it — no timezone table, and no
risk of comparing a Dallas clock against a Helsinki one.

One consequence worth knowing: a 14-hour **daytime** layover gets rejected
even though you could see the city. That follows your "stay the night"
framing, and without a hotel you'd be carrying that oversized Samsonite all
day. Set `overnight_min_hours: 24` to go back to full-day stopovers only, or
widen `night_core_*` to be more permissive.

Tune it all under `trip.layover` in `config.yml`. The config refuses to load
if `overnight_min_hours` doesn't exceed `quick_max_hours`, because then
there'd be no dead zone and the rule would mean nothing.

### Stopovers are built, not found

No airline sells an overnight stopover — past a few hours a connection stops
being a connection and becomes two journeys. So the bot constructs them from
separate one-way fares:

```
DFW ──nonstop──> Helsinki   [2.4 days]   Helsinki ──nonstop──> Stockholm
                                              [12 nights]
Stockholm ──────────────────────────────────────────> DFW
```

Because it only ever pairs flights on **different days**, a short connection
is structurally impossible here. The gap is re-verified from actual flight
times — length *and* whether it spans a night — before anything is emailed.

Quick connections need none of this — airlines already sell those as one
ticket, and they arrive through the ordinary fare scan.

### Catching dead-zone layovers the airline won't disclose

This is the hard part. The fare API reports a **total journey duration** but
never the layover inside it, so a 9-hour wait in Frankfurt looks identical to
a 90-minute one. The bot estimates it: total time minus how long the flying
*should* take, from great-circle distance.

Checked against a real itinerary (DFW→Helsinki→Stockholm, 715 min, 45-minute
connection), the estimate lands at 0.9h — about 10 minutes off:

| Total journey | Estimated layover | Verdict |
|---|---|---|
| 715 min (the live one) | 0.9h | keep |
| 900 min | 4.0h | keep |
| 1000 min | 5.7h | keep, but flagged |
| 1255 min | 9.9h | **dropped** |

Note that an estimated 14–24h layover stays in the dead zone: without clock
times the bot can't confirm it covers a night, and unverifiable isn't the
same as fine. Only stopovers the bot builds itself get the overnight check.

The estimate runs slightly high, because a routing detour looks like waiting
time. So a fare is only dropped when it clears the 5h line by more than
`estimate_margin_hours` (2.5h by default) — a false drop costs you one fare,
but a false keep costs you a day in an airport. Borderline cases are kept
with a note telling you to check.

Set `reject_estimated_dead_zone: false` to flag rather than drop.

### Two caveats every email repeats

- **Separate tickets.** A missed connection on a built stopover is on you,
  not the airline. The 24h+ gap is itself the buffer.
- **Bags usually need re-checking** at the stopover.

---

## Stockholm priority

Stockholm carries a `priority: 2.00` multiplier in `config.yml`, so an ARN deal
outranks an identical fare to Paris and lands at the top of the email. Nearby
alternatives are boosted too: Gothenburg 1.35, Copenhagen 1.30 (35 min flight
or ~5h train to Stockholm), Oslo 1.25, Helsinki 1.20.

---

## Where the data comes from — and whether it runs out

Short answer: **the source doing 99% of the work has no monthly quota at
all**, only a rate limit you're nowhere near.

| Source | Role | Limit | Your usage |
|---|---|---|---|
| **Travelpayouts / Aviasales** | All fare scanning | 600 requests **per minute**. No monthly cap. | ~110/min peak — **18% of the ceiling** |
| **SerpApi (Google Flights)** | Verifies record candidates | 250/month, hard cap | Self-limited to 220/month, 7/day |
| **The Flight Deal RSS** | Human-spotted error fares | none | unlimited |

Travelpayouts' own docs say access is *"allowed without restrictions, but you
must have a token."* The only published limits are per-minute rate caps, and
the bot self-throttles well below them. At ~119,000 requests a month you are
not going to exhaust anything.

### The one that can run out, and how it's paced

SerpApi's free tier is a genuine hard cap of 250 searches/month. A naive
implementation burns it fast: 6 verifications × 9 runs/day would spend the
whole allowance in **four days**, leaving 26 days with no cross-check.

So the budget is rationed **per day as well as per month** (`daily_budget: 7`).
The fare worth verifying is as likely to show up on the 28th as the 2nd.

Check where you stand any time:

```bash
python -m bot.main --stats
```

### If a source disappears anyway

No third-party API is guaranteed forever — that's worth being honest about.
The bot degrades rather than dies:

- **No SerpApi key or quota exhausted** → scanning continues, records still
  work off the bot's own history. You lose only Google's cross-check.
- **Travelpayouts unavailable** → the RSS error-fare feed still runs, and the
  price history you've already built stays intact.
- **Rate limited (HTTP 429)** → the client backs off and retries, reading the
  `X-Rate-Limit-Reset` header rather than hammering.

Every source is behind an `enabled` flag in `config.yml`, and a new one only
needs to return `Deal` objects to slot in.

**A note on Amadeus:** the usual recommendation for a project like this is the
Amadeus self-service API. It was **decommissioned on July 17, 2026** and new
signups are gone, so any tutorial telling you to start there is now dead. That
shutdown is why this bot leans on Travelpayouts as its primary source.

Travelpayouts data is a cache of fares Aviasales users found in the last 48
hours, which is why the SerpApi cross-check matters: it catches fares that
have already sold out. When Google's cheapest bookable fare is far above the
cached price, the email says so.

**On the deal feeds:** most guides tell you to subscribe to The Flight Deal's
per-city feeds (`/category/flight-deals/dfw/feed/`). Those now return zero
items — the site restructured its categories. The bot uses the site-wide feed
and filters by your own origins and destinations, which works better anyway.
Secret Flying has no usable feed at all any more (`/usa-deals/feed/` redirects
in a loop), so subscribe to their email alerts directly at
<https://www.secretflying.com/alerts/> — it's free and covers what the bot
can't reach.

---

## Commands

```bash
python -m bot.main                 # full run
python -m bot.main --dry-run       # print emails instead of sending
python -m bot.main --digest        # include the daily digest
python -m bot.main --stats         # price history the bot has built
python -m bot.main --bag           # carry-on fit and fees, by airline
python -m bot.main --test-email    # confirm SMTP works
python -m bot.main -v              # verbose logging
python -m tests.test_bot           # 122 tests
python -m tests.demo_run           # simulated run, no keys needed
```

---

## Layout

```
setup.sh                 One-command setup (deps, credentials, test email)
config.yml               Everything tunable: routes, thresholds, layover rule
bot/
  main.py                Orchestration: scan -> score -> verify -> alert
  config.py              Config loading and validation
  models.py              Deal and Leg; all-in pricing, history_key, fingerprint
  baggage.py             Carry-on fit, enforcement risk and fees, by airline
  history.py             SQLite price history, dedupe, API budget
  scoring.py             Reference prices, record detection, tiers
  layovers.py            The two-band rule, and estimating undisclosed layovers
  stopovers.py           Builds the multi-day-stopover itineraries
  emailer.py             Plain-text + HTML alerts
  sources/
    travelpayouts.py     Main fare scanner
    serpapi_verify.py    Google Flights cross-check
    rss_deals.py         Error-fare feed watcher
tests/
  test_bot.py            122 tests
  demo_run.py            End-to-end simulation with mock data
.github/workflows/scan.yml   Runs every 2 hours, free
data/prices.db           The record book (committed by the Action)
```

---

## Booking an error fare

If you get an `insane`-tier alert, it's probably a mistake fare. The usual
advice:

1. **Book fast.** These last hours.
2. **Book direct with the airline** where possible — an OTA in the middle gives
   the airline an easy excuse to void it.
3. **Don't call to ask about it.** That's how they find out.
4. **Don't book hotels for a week.** Airlines sometimes cancel these, and in
   the US the DOT's 24-hour rule protects your cancellation, not theirs.
5. Pay with a card that has trip protection if you have one.
