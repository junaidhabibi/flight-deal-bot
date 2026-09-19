"""Price history and alert bookkeeping, stored in SQLite.

No public API sells "lowest price ever" for a route, so the bot builds that
dataset itself: every observation from every run is written here, and a fare
only counts as a record when it beats everything previously seen.

The DB file is committed back to the repo by the GitHub Action, so the
history survives across runs on a stateless runner.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .models import Deal

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    route         TEXT    NOT NULL,        -- Deal.history_key, e.g. DFW-HEL-ARN
    origin        TEXT    NOT NULL,
    destination   TEXT    NOT NULL,
    stopover      TEXT,                    -- hub code or NULL
    price_usd     REAL    NOT NULL,        -- ALL-IN: fare + carry-on fees
    depart_date   TEXT    NOT NULL,
    return_date   TEXT,
    source        TEXT    NOT NULL,
    observed_at   TEXT    NOT NULL         -- ISO8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_obs_route     ON observations(route);
CREATE INDEX IF NOT EXISTS idx_obs_route_pr  ON observations(route, price_usd);
CREATE INDEX IF NOT EXISTS idx_obs_observed  ON observations(observed_at);

CREATE TABLE IF NOT EXISTS alerts (
    fingerprint   TEXT PRIMARY KEY,
    route         TEXT NOT NULL,
    price_usd     REAL NOT NULL,
    tier          TEXT NOT NULL,
    sent_at       TEXT NOT NULL,
    payload       TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_sent ON alerts(sent_at);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    observations  INTEGER DEFAULT 0,
    candidates    INTEGER DEFAULT 0,
    alerts_sent   INTEGER DEFAULT 0,
    errors        TEXT
);

CREATE TABLE IF NOT EXISTS api_usage (
    provider      TEXT NOT NULL,
    yyyymm        TEXT NOT NULL,
    calls         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, yyyymm)
);

-- Individual timestamped calls, so a monthly quota can also be paced daily.
CREATE TABLE IF NOT EXISTS api_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL,
    called_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_calls ON api_calls(provider, called_at);

CREATE TABLE IF NOT EXISTS seen_rss (
    guid          TEXT PRIMARY KEY,
    seen_at       TEXT NOT NULL
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class History:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "History":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---------- observations ----------

    def record_observations(self, deals: Iterable[Deal]) -> int:
        """Write every price we saw, deal or not. This IS the history."""
        rows = []
        now = _iso(_utcnow())
        for d in deals:
            rows.append(
                (
                    d.history_key,
                    d.origin,
                    d.destination,
                    d.stopover_code,
                    float(d.total_price_usd),
                    d.depart_date.isoformat(),
                    d.return_date.isoformat() if d.return_date else None,
                    d.source,
                    now,
                )
            )
        if not rows:
            return 0
        with self._tx() as c:
            c.executemany(
                "INSERT INTO observations "
                "(route, origin, destination, stopover, price_usd, depart_date, "
                " return_date, source, observed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def route_stats(self, route: str) -> Dict[str, Optional[float]]:
        """Min / percentiles / count for a route across all history."""
        cur = self._conn.execute(
            "SELECT price_usd FROM observations WHERE route = ? ORDER BY price_usd",
            (route,),
        )
        prices = [r["price_usd"] for r in cur.fetchall()]
        if not prices:
            return {"count": 0, "min": None, "p10": None, "median": None, "mean": None}

        def pct(p: float) -> float:
            if len(prices) == 1:
                return prices[0]
            idx = p * (len(prices) - 1)
            lo, hi = int(idx), min(int(idx) + 1, len(prices) - 1)
            frac = idx - lo
            return prices[lo] * (1 - frac) + prices[hi] * frac

        return {
            "count": len(prices),
            "min": prices[0],
            "p10": pct(0.10),
            "median": pct(0.50),
            "mean": sum(prices) / len(prices),
        }

    def route_min(self, route: str, exclude_last_seconds: int = 0) -> Optional[float]:
        """Cheapest price ever recorded for a route.

        exclude_last_seconds lets the caller ignore observations just written
        in this run, so a fare isn't compared against itself.
        """
        if exclude_last_seconds:
            cutoff = _iso(_utcnow() - timedelta(seconds=exclude_last_seconds))
            cur = self._conn.execute(
                "SELECT MIN(price_usd) AS m FROM observations "
                "WHERE route = ? AND observed_at < ?",
                (route, cutoff),
            )
        else:
            cur = self._conn.execute(
                "SELECT MIN(price_usd) AS m FROM observations WHERE route = ?",
                (route,),
            )
        row = cur.fetchone()
        return row["m"] if row and row["m"] is not None else None

    def route_count(self, route: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) AS c FROM observations WHERE route = ?", (route,)
        )
        return int(cur.fetchone()["c"])

    def prune(self, keep_days: int = 540) -> int:
        """Drop old rows so the DB -- and the repo that carries it -- stays small.

        This file is committed on every run, and SQLite does not delta-
        compress, so each run stores a whole new blob. Only `observations`
        and `api_calls` used to be pruned; `runs`, `alerts` and `seen_rss`
        grew forever. seen_rss was the worst: one row per feed item ever
        seen, with an infinite dedupe window, on a feed that publishes daily.
        """
        now = _utcnow()
        cutoff = _iso(now - timedelta(days=keep_days))
        with self._tx() as c:
            cur = c.execute("DELETE FROM observations WHERE observed_at < ?", (cutoff,))
            # Individual call rows are only needed for the 24h window.
            c.execute(
                "DELETE FROM api_calls WHERE called_at < ?",
                (_iso(now - timedelta(days=3)),),
            )
            # Run log: enough to spot a pattern, not a permanent archive.
            c.execute(
                "DELETE FROM runs WHERE started_at < ?",
                (_iso(now - timedelta(days=90)),),
            )
            # Alerts feed the dedupe window (72h) and the daily cap (24h).
            c.execute(
                "DELETE FROM alerts WHERE sent_at < ?",
                (_iso(now - timedelta(days=30)),),
            )
            # A deal post older than this is not coming back.
            c.execute(
                "DELETE FROM seen_rss WHERE seen_at < ?",
                (_iso(now - timedelta(days=60)),),
            )
        return cur.rowcount

    # ---------- alerts ----------

    def was_alerted(self, fingerprint: str, within_hours: int) -> bool:
        cutoff = _iso(_utcnow() - timedelta(hours=within_hours))
        cur = self._conn.execute(
            "SELECT 1 FROM alerts WHERE fingerprint = ? AND sent_at >= ?",
            (fingerprint, cutoff),
        )
        return cur.fetchone() is not None

    def record_alert(self, deal: Deal) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO alerts "
                "(fingerprint, route, price_usd, tier, sent_at, payload) "
                "VALUES (?,?,?,?,?,?)",
                (
                    deal.fingerprint(),
                    deal.history_key,
                    float(deal.total_price_usd),
                    deal.tier,
                    _iso(_utcnow()),
                    json.dumps(deal.to_dict(), default=str),
                ),
            )

    def emails_sent_since(self, hours: int) -> int:
        """How many EMAILS went out, not how many deals were in them.

        record_alert() writes a row per deal, so counting rows made one
        email carrying 7 deals look like 7 emails and tripped the daily cap
        instantly. Deals mailed together share a sent_at to the second, so
        counting distinct timestamps counts messages.
        """
        cutoff = _iso(_utcnow() - timedelta(hours=hours))
        cur = self._conn.execute(
            "SELECT COUNT(DISTINCT substr(sent_at, 1, 19)) AS c "
            "FROM alerts WHERE sent_at >= ?",
            (cutoff,),
        )
        return int(cur.fetchone()["c"])

    def alerts_sent_since(self, hours: int) -> int:
        cutoff = _iso(_utcnow() - timedelta(hours=hours))
        cur = self._conn.execute(
            "SELECT COUNT(*) AS c FROM alerts WHERE sent_at >= ?", (cutoff,)
        )
        return int(cur.fetchone()["c"])

    # ---------- api budget ----------

    def api_calls_this_month(self, provider: str) -> int:
        yyyymm = _utcnow().strftime("%Y-%m")
        cur = self._conn.execute(
            "SELECT calls FROM api_usage WHERE provider = ? AND yyyymm = ?",
            (provider, yyyymm),
        )
        row = cur.fetchone()
        return int(row["calls"]) if row else 0

    def api_calls_today(self, provider: str) -> int:
        """Calls made in the last 24h, for pacing a monthly quota.

        Spending a month's allowance greedily leaves you blind for weeks, so
        the budget is also rationed per day.
        """
        cutoff = _iso(_utcnow() - timedelta(hours=24))
        cur = self._conn.execute(
            "SELECT COUNT(*) AS c FROM api_calls "
            "WHERE provider = ? AND called_at >= ?",
            (provider, cutoff),
        )
        return int(cur.fetchone()["c"])

    def bump_api_calls(self, provider: str, n: int = 1) -> int:
        yyyymm = _utcnow().strftime("%Y-%m")
        now = _iso(_utcnow())
        with self._tx() as c:
            c.execute(
                "INSERT INTO api_usage (provider, yyyymm, calls) VALUES (?,?,?) "
                "ON CONFLICT(provider, yyyymm) DO UPDATE SET calls = calls + ?",
                (provider, yyyymm, n, n),
            )
            c.executemany(
                "INSERT INTO api_calls (provider, called_at) VALUES (?,?)",
                [(provider, now)] * n,
            )
        return self.api_calls_this_month(provider)

    # ---------- rss dedupe ----------

    def rss_seen(self, guid: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM seen_rss WHERE guid = ?", (guid,))
        return cur.fetchone() is not None

    def mark_rss_seen(self, guid: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO seen_rss (guid, seen_at) VALUES (?,?)",
                (guid, _iso(_utcnow())),
            )

    # ---------- runs ----------

    def start_run(self) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO runs (started_at) VALUES (?)", (_iso(_utcnow()),)
            )
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        observations: int = 0,
        candidates: int = 0,
        alerts_sent: int = 0,
        errors: Optional[List[str]] = None,
    ) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, observations=?, candidates=?, "
                "alerts_sent=?, errors=? WHERE id=?",
                (
                    _iso(_utcnow()),
                    observations,
                    candidates,
                    alerts_sent,
                    json.dumps(errors or []),
                    run_id,
                ),
            )

    def run_count(self) -> int:
        """How many runs have happened. Drives scan rotation, so successive
        runs sweep different slices of the route matrix."""
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM runs")
        return int(cur.fetchone()["c"])

    def recent_runs(self, limit: int = 10) -> List[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        )
        return cur.fetchall()
