from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# A job is live while it can still change on its own; terminal states are the verdicts a
# client reads. `expired` is terminal: the hub gave up before the client had to.
LIVE_JOB_STATES: tuple[str, ...] = ("queued", "waiting_quota", "running")
TERMINAL_JOB_STATES: tuple[str, ...] = ("done", "failed", "expired", "cancelled")
DEFAULT_JOB_TTL_S = 6 * 3600

TABLES = (
    """
    CREATE TABLE IF NOT EXISTS usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        app TEXT NOT NULL DEFAULT 'unknown',
        provider TEXT NOT NULL DEFAULT '',
        account TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        in_tokens INTEGER NOT NULL DEFAULT 0,
        out_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'ok',
        error_code TEXT,
        attempt INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        app TEXT,
        model TEXT,
        account TEXT,
        kind TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        app TEXT NOT NULL,
        model TEXT,
        require TEXT NOT NULL DEFAULT '[]',
        priority INTEGER NOT NULL DEFAULT 5,
        request TEXT NOT NULL,
        callback_url TEXT,
        state TEXT NOT NULL DEFAULT 'queued',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        next_window_at TEXT,
        served_by TEXT,
        result TEXT,
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS exhausted (
        account TEXT NOT NULL,
        model TEXT NOT NULL,
        until_ts TEXT NOT NULL,
        reason TEXT,
        ts TEXT NOT NULL,
        PRIMARY KEY (account, model)
    )
    """,
    # A pair the vendor will not serve: an unusable route (not_found) or its own upstream
    # being down (unavailable). Same shape and expiry semantics as `exhausted`, but the
    # reason is the route, not the account's budget, so the two never overwrite each other.
    """
    CREATE TABLE IF NOT EXISTS unavailable (
        account TEXT NOT NULL,
        model TEXT NOT NULL,
        kind TEXT NOT NULL,
        code TEXT,
        reason TEXT,
        until_ts TEXT NOT NULL,
        ts TEXT NOT NULL,
        PRIMARY KEY (account, model)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observed_limits (
        account TEXT NOT NULL,
        model TEXT NOT NULL,
        window_name TEXT NOT NULL,
        metric TEXT NOT NULL,
        value INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (account, model, window_name, metric)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observed_request_caps (
        account TEXT NOT NULL,
        model TEXT NOT NULL,
        max_request_tokens INTEGER NOT NULL,
        source TEXT,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (account, model)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS unsupported_params (
        account TEXT NOT NULL,
        model TEXT NOT NULL,
        param TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (account, model, param)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_overrides (
        key TEXT PRIMARY KEY,
        disabled INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS apps (
        app TEXT PRIMARY KEY,
        paused INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # Models one app must never be routed to again, with the reason the app gave. The hub
    # cannot measure whether an answer quoted its input verbatim or held to a schema; the app
    # can, so the verdict is the app's and it binds that app only.
    """
    CREATE TABLE IF NOT EXISTS app_model_bans (
        app TEXT NOT NULL,
        model TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (app, model)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS promos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        url TEXT,
        note TEXT,
        found_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS promo_aliases (
        alias TEXT PRIMARY KEY,
        key TEXT NOT NULL,
        learned_at TEXT NOT NULL,
        source TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS promo_rejections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        identity TEXT,
        provider TEXT NOT NULL,
        reason TEXT NOT NULL,
        url TEXT,
        note_snippet TEXT,
        rejected_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scout_pages (
        url TEXT PRIMARY KEY,
        fetched_at TEXT NOT NULL,
        content_hash TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'ok'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scout_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL DEFAULT 'running',
        sources INTEGER NOT NULL DEFAULT 0,
        pages_fetched INTEGER NOT NULL DEFAULT 0,
        pages_changed INTEGER NOT NULL DEFAULT 0,
        offers INTEGER NOT NULL DEFAULT 0,
        new_count INTEGER NOT NULL DEFAULT 0,
        updated_count INTEGER NOT NULL DEFAULT 0,
        skipped_count INTEGER NOT NULL DEFAULT 0,
        models_used TEXT NOT NULL DEFAULT '{}',
        tokens TEXT NOT NULL DEFAULT '{}',
        errors TEXT NOT NULL DEFAULT '[]',
        decisions TEXT NOT NULL DEFAULT '[]',
        report_md TEXT NOT NULL DEFAULT '',
        dry_run INTEGER NOT NULL DEFAULT 0
    )
    """,
)

COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("usage", "estimated", "INTEGER NOT NULL DEFAULT 0"),
    ("usage", "stream", "INTEGER NOT NULL DEFAULT 0"),
    ("usage", "request_id", "TEXT"),
    ("jobs", "served_by", "TEXT"),
    ("events", "account", "TEXT"),
    ("promos", "source", "TEXT"),
    ("promos", "expires_at", "TEXT"),
    ("exhausted", "scope", "TEXT"),
    # "<provider>/<account_id>" once the promo key has been added as an account
    ("promos", "account_key", "TEXT"),
    # the endpoint the promo names, for a vendor the catalog has no template for: quick add
    # takes these over guessing
    ("promos", "base_url", "TEXT"),
    ("promos", "api_key_env", "TEXT"),
    # why the curator's decisions failed validation, for the run report - a count alone gives
    # no way to tell a synonym action from a genuinely broken answer
    ("scout_runs", "invalid_reasons", "TEXT NOT NULL DEFAULT '[]'"),
    # the vendor this row is about, resolved once by promo_identity; two rows sharing it are
    # the same offer under two names and are merged instead of stacked
    ("promos", "identity", "TEXT"),
    ("promos", "updates_count", "INTEGER NOT NULL DEFAULT 0"),
    ("promos", "updated_at", "TEXT"),
    # queue hygiene: every job carries a deadline, a running job carries a lease, and a job
    # backing off on a model with no window data carries the time it may be claimed again.
    # Rows written before this migration have expires_at NULL; the timer reads their deadline
    # as created_at + ttl_s instead, so nothing needs a data migration.
    ("jobs", "ttl_s", "INTEGER NOT NULL DEFAULT 21600"),
    ("jobs", "expires_at", "TEXT"),
    ("jobs", "lease_until", "TEXT"),
    ("jobs", "next_attempt_at", "TEXT"),
    ("jobs", "lease_attempts", "INTEGER NOT NULL DEFAULT 0"),
    # why the owner threw this lead out, and when. The reason is not a comment: it is read
    # back to the scout and the promo-hunt skill as a standing rule
    ("promos", "rejected_reason", "TEXT"),
    ("promos", "rejected_at", "TEXT"),
    # the other axis of "request too large": a per-request ceiling on output tokens, which the
    # caller controls with max_tokens rather than with the size of the prompt
    ("observed_request_caps", "max_out_tokens", "INTEGER"),
)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts)",
    "CREATE INDEX IF NOT EXISTS idx_usage_window ON usage(account, model, ts)",
    "CREATE INDEX IF NOT EXISTS idx_usage_app ON usage(app, ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, priority, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_promos_found ON promos(found_at)",
    "CREATE INDEX IF NOT EXISTS idx_promos_identity ON promos(identity)",
    "CREATE INDEX IF NOT EXISTS idx_scout_runs_started ON scout_runs(started_at)",
    "CREATE INDEX IF NOT EXISTS idx_promo_rejections_at ON promo_rejections(rejected_at)",
)


# How far along a promo is. A merge takes the highest rank the row has ever reached: a lead
# the owner already turned into an account does not go back to 'new' because a page repeated
# the offer, and an expiry once observed is not undone by a stale listing. 'rejected' sits
# above every automatic state so a re-post of an offer the owner threw out lands as another
# update on the rejected row instead of reopening it, and below 'used' because a rejected
# vendor that later gets an account is an account.
PROMO_STATUS_RANK: dict[str, int] = {"new": 0, "known": 1, "expired": 2, "rejected": 3, "used": 4}
REJECTED = "rejected"
NOTE_SNIPPET_CHARS = 200


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def clean_promo_note(note: str | None) -> str | None:
    """Drop the 'UPDATE:' prefix hand-written posts used to mark a re-report of a known offer.

    The hub records that as an update on the row itself now, so the prefix is noise.
    """
    text = (note or "").strip()
    if text.lower().startswith("update:"):
        text = text[len("update:") :].strip()
    return text or None


def merge_promo_note(
    current: str | None, incoming: str | None, source: str | None, seen_at: str | None
) -> str:
    """Append a dated line, so one row reads as the history of an offer rather than the last
    thing said about it."""
    text = clean_promo_note(incoming)
    kept = (current or "").strip()
    if not text or text in kept:
        return kept
    line = f"[{(seen_at or now_iso())[:10]} {source or 'unknown'}] {text}"
    return f"{kept}\n{line}" if kept else line


def merge_promo_status(current: str, incoming: str | None) -> str:
    if not incoming:
        return current
    if PROMO_STATUS_RANK.get(incoming, 0) > PROMO_STATUS_RANK.get(current, 0):
        return incoming
    return current


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _request_cap_row(row: dict[str, Any]) -> dict[str, Any]:
    """0 in the NOT NULL input-cap column means "never observed", not "a ceiling of zero"."""
    cap = row.get("max_request_tokens")
    return {**row, "max_request_tokens": cap if cap else None}


def _observed_by_window(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    for row in rows:
        window = observed.setdefault(row["window_name"], {})
        window[row["metric"]] = int(row["value"])
        window["observed_at"] = row["observed_at"]
    return observed


def parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def migrate(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            for statement in TABLES:
                cur.execute(statement)
            self._conn.commit()
            for table, column, spec in COLUMN_MIGRATIONS:
                existing = {row["name"] for row in cur.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
            self._conn.commit()
            # indexes last: an index over a column added by COLUMN_MIGRATIONS must not run first
            for statement in INDEXES:
                cur.execute(statement)
            self._conn.commit()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def start_usage(
        self,
        *,
        app: str,
        provider: str,
        account: str,
        model: str,
        status: str,
        latency_ms: int,
        attempt: int,
        error_code: str | None = None,
        stream: bool = False,
        request_id: str | None = None,
        ts: str | None = None,
    ) -> int:
        cur = self.execute(
            """
            INSERT INTO usage (ts, app, provider, account, model, latency_ms, status,
                               error_code, attempt, stream, request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts or now_iso(),
                app,
                provider,
                account,
                model,
                latency_ms,
                status,
                error_code,
                attempt,
                1 if stream else 0,
                request_id,
            ),
        )
        return int(cur.lastrowid or 0)

    def update_usage_tokens(
        self,
        usage_id: int,
        *,
        in_tokens: int = 0,
        out_tokens: int = 0,
        cached_tokens: int = 0,
        total_tokens: int | None = None,
        estimated: bool = False,
        latency_ms: int | None = None,
        status: str | None = None,
    ) -> None:
        total = total_tokens if total_tokens is not None else in_tokens + out_tokens
        sets = [
            "in_tokens = ?",
            "out_tokens = ?",
            "cached_tokens = ?",
            "total_tokens = ?",
            "estimated = ?",
        ]
        params: list[Any] = [in_tokens, out_tokens, cached_tokens, total, 1 if estimated else 0]
        if latency_ms is not None:
            sets.append("latency_ms = ?")
            params.append(latency_ms)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        params.append(usage_id)
        self.execute(f"UPDATE usage SET {', '.join(sets)} WHERE id = ?", params)

    def add_event(
        self,
        *,
        kind: str,
        message: str,
        app: str | None = None,
        model: str | None = None,
        account: str | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO events (ts, app, model, account, kind, message) VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso(), app, model, account, kind, message[:2000]),
        )

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def window_usage(self, account: str, model: str, since: datetime) -> dict[str, int]:
        """Tokens and calls this (account, model) spent in the window.

        `requests` counts every row the vendor actually served or tried to serve. A row with
        status `quota` is the vendor refusing the call, and a refused call is not charged
        against the request quota it was refused by - counting it would spend the next
        window's budget on the last window's 429s.
        """
        row = self.query_one(
            """
            SELECT COALESCE(SUM(in_tokens), 0) AS in_tokens,
                   COALESCE(SUM(out_tokens), 0) AS out_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(CASE WHEN status != 'quota' THEN 1 ELSE 0 END), 0) AS requests
            FROM usage WHERE account = ? AND model = ? AND ts >= ?
            """,
            (account, model, to_iso(since)),
        )
        return {
            "in_tokens": int(row["in_tokens"]) if row else 0,
            "out_tokens": int(row["out_tokens"]) if row else 0,
            "total_tokens": int(row["total_tokens"]) if row else 0,
            "requests": int(row["requests"]) if row else 0,
        }

    def first_usage_ts(self, account: str, model: str) -> str | None:
        row = self.query_one(
            "SELECT MIN(ts) AS ts FROM usage WHERE account = ? AND model = ?", (account, model)
        )
        return row["ts"] if row and row["ts"] else None

    def model_stats(self, account: str, model: str) -> dict[str, Any]:
        """Last ok, last failure and average latency for one pair.

        `abandoned` rows are neither: the caller stopped waiting or the owner killed the call,
        so the vendor never got the chance to succeed or fail. Every error count in this file
        skips them for the same reason - a model must not look broken because a client left.
        """
        ok_row = self.query_one(
            "SELECT ts FROM usage WHERE account = ? AND model = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
            (account, model),
        )
        err_row = self.query_one(
            "SELECT ts, status, error_code FROM usage WHERE account = ? AND model = ? "
            "AND status NOT IN ('ok', 'abandoned') ORDER BY id DESC LIMIT 1",
            (account, model),
        )
        lat_row = self.query_one(
            "SELECT AVG(latency_ms) AS avg_latency FROM usage WHERE account = ? AND model = ? "
            "AND status = 'ok' AND id > (SELECT COALESCE(MAX(id), 0) - 50 FROM usage)",
            (account, model),
        )
        avg_latency = lat_row["avg_latency"] if lat_row and lat_row["avg_latency"] else None
        return {
            "last_ok_at": ok_row["ts"] if ok_row else None,
            "last_error": (
                f"{err_row['status']}: {err_row['error_code'] or ''}".strip(": ") if err_row else None
            ),
            "last_error_at": err_row["ts"] if err_row else None,
            "avg_latency_ms": int(avg_latency) if avg_latency else None,
        }

    def avg_latency_by_model(self) -> dict[tuple[str, str], int]:
        """Average ok latency per (account, model), the whole table in one query.

        `model_stats` answers this one pair at a time, which a selection ordering a pool by
        latency would turn into a query per candidate. The tail is wider than the 50 rows a
        single pair looks at, because here the rows are shared by every pair in the registry.
        """
        rows = self.query(
            "SELECT account, model, AVG(latency_ms) AS avg_latency FROM usage "
            "WHERE status = 'ok' AND id > (SELECT COALESCE(MAX(id), 0) - 500 FROM usage) "
            "GROUP BY account, model"
        )
        return {(row["account"], row["model"]): int(row["avg_latency"]) for row in rows if row["avg_latency"]}

    def set_exhausted(
        self,
        account: str,
        model: str,
        until: datetime,
        reason: str,
        scope: str | None = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO exhausted (account, model, until_ts, reason, scope, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account, model) DO UPDATE SET until_ts = excluded.until_ts,
                reason = excluded.reason, scope = excluded.scope, ts = excluded.ts
            """,
            (account, model, to_iso(until), reason[:500], scope, now_iso()),
        )

    def exhausted_until(self, account: str, model: str, now: datetime) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM exhausted WHERE account = ? AND model = ?", (account, model))
        if not row:
            return None
        if parse_iso(row["until_ts"]) <= now:
            self.clear_exhausted(account, model)
            return None
        return row

    def clear_exhausted(self, account: str, model: str | None = None) -> int:
        if model is None:
            cur = self.execute("DELETE FROM exhausted WHERE account = ?", (account,))
        else:
            cur = self.execute("DELETE FROM exhausted WHERE account = ? AND model = ?", (account, model))
        return cur.rowcount

    def clear_exhausted_for_model(self, model: str) -> int:
        cur = self.execute("DELETE FROM exhausted WHERE model = ?", (model,))
        return cur.rowcount

    def mark_unavailable(
        self,
        account: str,
        model: str,
        kind: str,
        code: str | None,
        reason: str,
        until: datetime,
    ) -> None:
        self.execute(
            """
            INSERT INTO unavailable (account, model, kind, code, reason, until_ts, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account, model) DO UPDATE SET kind = excluded.kind, code = excluded.code,
                reason = excluded.reason, until_ts = excluded.until_ts, ts = excluded.ts
            """,
            (account, model, kind, code, reason[:500], to_iso(until), now_iso()),
        )

    def unavailable_until(self, account: str, model: str, now: datetime) -> dict[str, Any] | None:
        """The live row for this pair, or None. A read past `until_ts` drops the row."""
        row = self.query_one("SELECT * FROM unavailable WHERE account = ? AND model = ?", (account, model))
        if not row:
            return None
        if parse_iso(row["until_ts"]) <= now:
            self.clear_unavailable(model, account)
            return None
        return row

    def unavailable_all(self, now: datetime | None = None) -> dict[tuple[str, str], dict[str, Any]]:
        rows = self.query("SELECT * FROM unavailable")
        if now is not None:
            rows = [row for row in rows if parse_iso(row["until_ts"]) > now]
        return {(row["account"], row["model"]): row for row in rows}

    def clear_unavailable(self, model: str, account: str | None = None) -> int:
        if account is None:
            cur = self.execute("DELETE FROM unavailable WHERE model = ?", (model,))
        else:
            cur = self.execute("DELETE FROM unavailable WHERE model = ? AND account = ?", (model, account))
        return cur.rowcount

    def record_observed_limit(
        self,
        account: str,
        model: str,
        window: str,
        metric: str,
        value: int,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        """What the vendor actually allowed before it said no, per (account, model, window).

        Latest observation wins: a free tier that got wider is worth believing, and the cap
        keeps a model with undeclared limits from being treated as infinite.
        """
        ts = observed_at or now_iso()
        self.execute(
            """
            INSERT INTO observed_limits (account, model, window_name, metric, value, observed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account, model, window_name, metric) DO UPDATE SET
                value = excluded.value, observed_at = excluded.observed_at
            """,
            (account, model, window, metric, int(value), ts),
        )
        return {"window": window, "metric": metric, "value": int(value), "observed_at": ts}

    def observed_limits(self, account: str, model: str) -> dict[str, dict[str, Any]]:
        rows = self.query("SELECT * FROM observed_limits WHERE account = ? AND model = ?", (account, model))
        return _observed_by_window(rows)

    def observed_limits_all(self) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in self.query("SELECT * FROM observed_limits"):
            grouped.setdefault((row["account"], row["model"]), []).append(row)
        return {key: _observed_by_window(rows) for key, rows in grouped.items()}

    def clear_observed_limits(self, model: str, account: str | None = None) -> int:
        if account is None:
            cur = self.execute("DELETE FROM observed_limits WHERE model = ?", (model,))
        else:
            cur = self.execute(
                "DELETE FROM observed_limits WHERE model = ? AND account = ?", (model, account)
            )
        return cur.rowcount

    def record_request_cap(
        self,
        account: str,
        model: str,
        max_request_tokens: int | None = None,
        source: str | None = None,
        observed_at: str | None = None,
        max_out_tokens: int | None = None,
    ) -> dict[str, Any]:
        """What a 413 (or an equivalent 400/429) just said the request ceiling actually is.

        One row per (account, model): the latest observation wins, the same as observed_limits -
        a vendor that widens the cap later is worth believing over a stale, tighter number. The
        two axes are learned from different bodies, so an observation on one never erases the
        other; `max_request_tokens` is NOT NULL from the original schema and stores 0 for
        "never observed", which the readers below turn back into None.
        """
        ts = observed_at or now_iso()
        self.execute(
            """
            INSERT INTO observed_request_caps
                (account, model, max_request_tokens, max_out_tokens, source, observed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account, model) DO UPDATE SET
                max_request_tokens = CASE WHEN excluded.max_request_tokens > 0
                    THEN excluded.max_request_tokens
                    ELSE observed_request_caps.max_request_tokens END,
                max_out_tokens = COALESCE(excluded.max_out_tokens, observed_request_caps.max_out_tokens),
                source = excluded.source, observed_at = excluded.observed_at
            """,
            (
                account,
                model,
                int(max_request_tokens) if max_request_tokens else 0,
                int(max_out_tokens) if max_out_tokens else None,
                source,
                ts,
            ),
        )
        row = self.request_cap(account, model)
        return row if row is not None else {}

    def request_cap(self, account: str, model: str) -> dict[str, Any] | None:
        row = self.query_one(
            "SELECT * FROM observed_request_caps WHERE account = ? AND model = ?", (account, model)
        )
        return _request_cap_row(row) if row is not None else None

    def request_caps_all(self) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (row["account"], row["model"]): _request_cap_row(row)
            for row in self.query("SELECT * FROM observed_request_caps")
        }

    def clear_request_caps(self, model: str, account: str | None = None) -> int:
        if account is None:
            cur = self.execute("DELETE FROM observed_request_caps WHERE model = ?", (model,))
        else:
            cur = self.execute(
                "DELETE FROM observed_request_caps WHERE model = ? AND account = ?", (model, account)
            )
        return cur.rowcount

    def record_unsupported_param(
        self, account: str, model: str, param: str, observed_at: str | None = None
    ) -> None:
        """A vendor route just said it will not take this param: never send it again."""
        self.execute(
            """
            INSERT INTO unsupported_params (account, model, param, observed_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(account, model, param) DO UPDATE SET observed_at = excluded.observed_at
            """,
            (account, model, param, observed_at or now_iso()),
        )

    def unsupported_params_for(self, account: str, model: str) -> set[str]:
        return {
            row["param"]
            for row in self.query(
                "SELECT param FROM unsupported_params WHERE account = ? AND model = ?", (account, model)
            )
        }

    def unsupported_params_all(self) -> dict[tuple[str, str], set[str]]:
        grouped: dict[tuple[str, str], set[str]] = {}
        for row in self.query("SELECT account, model, param FROM unsupported_params"):
            grouped.setdefault((row["account"], row["model"]), set()).add(row["param"])
        return grouped

    def clear_unsupported_params(self, model: str, account: str | None = None) -> int:
        if account is None:
            cur = self.execute("DELETE FROM unsupported_params WHERE model = ?", (model,))
        else:
            cur = self.execute(
                "DELETE FROM unsupported_params WHERE model = ? AND account = ?", (model, account)
            )
        return cur.rowcount

    def usage_since_by_model(self, since: str) -> dict[tuple[str, str], dict[str, Any]]:
        """One grouped pass over the usage table - the status page has a row per pair."""
        rows = self.query(
            """
            SELECT account, model,
                   COUNT(*) AS requests,
                   COALESCE(SUM(in_tokens), 0) AS in_tokens,
                   COALESCE(SUM(out_tokens), 0) AS out_tokens,
                   SUM(CASE WHEN status NOT IN ('ok', 'abandoned') THEN 1 ELSE 0 END) AS errors,
                   MAX(ts) AS last_used_at
            FROM usage WHERE ts >= ? GROUP BY account, model
            """,
            (since,),
        )
        return {
            (row["account"], row["model"]): {
                "requests": int(row["requests"]),
                "in_tokens": int(row["in_tokens"]),
                "out_tokens": int(row["out_tokens"]),
                "errors": int(row["errors"] or 0),
                "last_used_at": row["last_used_at"],
            }
            for row in rows
        }

    def usage_by_app_and_model(self, since: str) -> list[dict[str, Any]]:
        """Who called what over a window, one row per (app, account, model).

        One query feeds both halves of the live view: an app's recent models, and the apps
        behind a model row. `idx_usage_ts` covers the range scan.
        """
        return [
            {
                "app": row["app"],
                "account": row["account"],
                "model": row["model"],
                "calls": int(row["calls"]),
                "out_tokens": int(row["out_tokens"] or 0),
                "last_used_at": row["last_used_at"],
            }
            for row in self.query(
                """
                SELECT app, account, model,
                       COUNT(*) AS calls,
                       COALESCE(SUM(out_tokens), 0) AS out_tokens,
                       MAX(ts) AS last_used_at
                FROM usage WHERE ts >= ? GROUP BY app, account, model
                """,
                (since,),
            )
        ]

    def last_used_by_model(self) -> dict[tuple[str, str], str]:
        rows = self.query("SELECT account, model, MAX(ts) AS ts FROM usage GROUP BY account, model")
        return {(row["account"], row["model"]): row["ts"] for row in rows if row["ts"]}

    def set_model_disabled(self, key: str, disabled: bool, note: str | None = None) -> None:
        self.execute(
            """
            INSERT INTO model_overrides (key, disabled, note, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET disabled = excluded.disabled,
                note = excluded.note, updated_at = excluded.updated_at
            """,
            (key, 1 if disabled else 0, note, now_iso()),
        )

    def disabled_models(self) -> set[str]:
        return {row["key"] for row in self.query("SELECT key FROM model_overrides WHERE disabled = 1")}

    def ensure_app(self, app: str) -> dict[str, Any]:
        row = self.query_one("SELECT * FROM apps WHERE app = ?", (app,))
        if row:
            return row
        ts = now_iso()
        self.execute(
            "INSERT OR IGNORE INTO apps (app, paused, created_at, updated_at) VALUES (?, 0, ?, ?)",
            (app, ts, ts),
        )
        return self.query_one("SELECT * FROM apps WHERE app = ?", (app,)) or {}

    def set_app_paused(self, app: str, paused: bool) -> None:
        self.ensure_app(app)
        self.execute(
            "UPDATE apps SET paused = ?, updated_at = ? WHERE app = ?",
            (1 if paused else 0, now_iso(), app),
        )

    def app_paused(self, app: str) -> bool:
        row = self.query_one("SELECT paused FROM apps WHERE app = ?", (app,))
        return bool(row["paused"]) if row else False

    def apps(self) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT app,
                   COUNT(*) AS requests,
                   COALESCE(SUM(in_tokens), 0) AS in_tokens,
                   COALESCE(SUM(out_tokens), 0) AS out_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   SUM(CASE WHEN status NOT IN ('ok', 'abandoned') THEN 1 ELSE 0 END) AS errors,
                   MAX(ts) AS last_seen
            FROM usage GROUP BY app
            """
        )
        by_app = {row["app"]: row for row in rows}
        for row in self.query("SELECT * FROM apps"):
            entry = by_app.setdefault(
                row["app"],
                {
                    "app": row["app"],
                    "requests": 0,
                    "in_tokens": 0,
                    "out_tokens": 0,
                    "total_tokens": 0,
                    "errors": 0,
                    "last_seen": None,
                },
            )
            entry["paused"] = bool(row["paused"])
        for entry in by_app.values():
            entry.setdefault("paused", False)
        return sorted(by_app.values(), key=lambda item: item["app"])

    def ban_model(self, app: str, model: str, reason: str) -> dict[str, Any]:
        """Ban a model for one app. A second ban of the same pair replaces the reason."""
        self.ensure_app(app)
        self.execute(
            """
            INSERT INTO app_model_bans (app, model, reason, created_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(app, model) DO UPDATE SET reason = excluded.reason,
                created_at = excluded.created_at
            """,
            (app, model, reason, now_iso()),
        )
        row = self.query_one("SELECT * FROM app_model_bans WHERE app = ? AND model = ?", (app, model))
        return row or {}

    def unban_model(self, app: str, model: str) -> bool:
        cur = self.execute("DELETE FROM app_model_bans WHERE app = ? AND model = ?", (app, model))
        return cur.rowcount > 0

    def bans_for(self, app: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM app_model_bans WHERE app = ? ORDER BY created_at DESC, model", (app,)
        )

    def bans_all(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in self.query("SELECT * FROM app_model_bans ORDER BY app, created_at DESC, model"):
            grouped.setdefault(row["app"], []).append(row)
        return grouped

    def promos(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM promos ORDER BY found_at DESC, id DESC")

    def promo(self, promo_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM promos WHERE id = ?", (promo_id,))

    def add_promo(
        self,
        provider: str,
        url: str | None,
        note: str | None,
        status: str = "new",
        source: str | None = None,
        expires_at: str | None = None,
        account_key: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        identity: str | None = None,
        found_at: str | None = None,
    ) -> dict[str, Any]:
        cur = self.execute(
            "INSERT INTO promos (provider, url, note, found_at, status, source, expires_at, "
            "account_key, base_url, api_key_env, identity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                provider,
                url,
                note,
                found_at or now_iso(),
                status,
                source,
                expires_at,
                account_key,
                base_url,
                api_key_env,
                identity,
            ),
        )
        return self.query_one("SELECT * FROM promos WHERE id = ?", (cur.lastrowid,)) or {}

    def promo_by_identity(self, identity: str) -> dict[str, Any] | None:
        """The row that owns this identity: the oldest one, which is what merges land on."""
        return self.query_one(
            "SELECT * FROM promos WHERE identity = ? ORDER BY found_at ASC, id ASC LIMIT 1",
            (identity,),
        )

    def upsert_promo(
        self,
        *,
        identity: str,
        provider: str,
        url: str | None = None,
        note: str | None = None,
        status: str | None = None,
        source: str | None = None,
        expires_at: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        account_key: str | None = None,
        seen_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Write one sighting of a promo. Returns (row, created).

        A second sighting of the same identity never opens a row: the note is appended with a
        date and the source that reported it, the fields the sighting carries are refreshed,
        and everything the row already had - its found_at, its url, a status further along the
        lifecycle - is kept. That way the watchlist stays one row per vendor with its history
        in place, whoever posts it.
        """
        found = self.promo_by_identity(identity)
        if found is None:
            created = self.add_promo(
                provider,
                url,
                clean_promo_note(note),
                status or "new",
                source,
                expires_at,
                account_key,
                base_url,
                api_key_env,
                identity=identity,
                found_at=seen_at,
            )
            return created, True

        promo_id = int(found["id"])
        fields: dict[str, Any] = {
            "note": merge_promo_note(found.get("note"), note, source, seen_at),
            "status": merge_promo_status(str(found.get("status") or "new"), status),
            "updates_count": int(found.get("updates_count") or 0) + 1,
            "updated_at": now_iso(),
        }
        # the row keeps the url it was filed under; the rest is refreshed by anything the new
        # sighting actually carries, so a later report can fill in a blank base_url or expiry
        if not str(found.get("url") or "").strip() and str(url or "").strip():
            fields["url"] = url
        for name, value in (
            ("base_url", base_url),
            ("api_key_env", api_key_env),
            ("expires_at", expires_at),
            ("account_key", account_key),
        ):
            if str(value or "").strip():
                fields[name] = value
        sets = ", ".join(f"{name} = ?" for name in fields)
        self.execute(f"UPDATE promos SET {sets} WHERE id = ?", (*fields.values(), promo_id))
        return self.promo(promo_id) or {}, False

    def set_promo_identity(self, promo_id: int, identity: str) -> None:
        self.execute("UPDATE promos SET identity = ? WHERE id = ?", (identity, promo_id))

    def delete_promo(self, promo_id: int) -> None:
        self.execute("DELETE FROM promos WHERE id = ?", (promo_id,))

    def promo_alias(self, alias: str) -> str | None:
        row = self.query_one("SELECT key FROM promo_aliases WHERE alias = ?", (alias,))
        return str(row["key"]) if row else None

    def promo_aliases(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM promo_aliases ORDER BY learned_at DESC, alias")

    def learn_promo_alias(self, alias: str, key: str, source: str | None = None) -> None:
        """First writer wins: a name once tied to a vendor is not re-pointed by a later guess."""
        self.execute(
            "INSERT INTO promo_aliases (alias, key, learned_at, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(alias) DO NOTHING",
            (alias, key, now_iso(), source),
        )

    def reject_promo(self, promo_id: int, reason: str) -> dict[str, Any] | None:
        """Throw a lead out, with the reason kept as the durable part.

        The row carries the current verdict; `promo_rejections` carries every rejection event,
        because a row can be rejected, reopened and rejected again, and the writers that must
        not propose the offer again read that history, not the row.
        """
        row = self.promo(promo_id)
        if row is None:
            return None
        at = now_iso()
        snippet = str(row.get("note") or "").strip()[:NOTE_SNIPPET_CHARS] or None
        self.execute(
            "UPDATE promos SET status = ?, rejected_reason = ?, rejected_at = ? WHERE id = ?",
            (REJECTED, reason, at, promo_id),
        )
        self.execute(
            "INSERT INTO promo_rejections (identity, provider, reason, url, note_snippet, "
            "rejected_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                row.get("identity"),
                str(row.get("provider") or "unknown"),
                reason,
                row.get("url"),
                snippet,
                at,
            ),
        )
        return self.promo(promo_id)

    def reopen_promo(self, promo_id: int) -> dict[str, Any] | None:
        """Back to a lead the owner still looks at. The rejection history stays: the scout is
        told what was once thrown out even when the owner changed their mind about one row."""
        row = self.promo(promo_id)
        if row is None:
            return None
        self.execute(
            "UPDATE promos SET status = 'known', rejected_reason = NULL, rejected_at = NULL WHERE id = ?",
            (promo_id,),
        )
        return self.promo(promo_id)

    def promo_rejections(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM promo_rejections ORDER BY rejected_at DESC, id DESC LIMIT ?", (limit,)
        )

    def update_promo(self, promo_id: int, **fields: Any) -> dict[str, Any] | None:
        fields = {name: value for name, value in fields.items() if value is not None}
        if fields:
            sets = ", ".join(f"{name} = ?" for name in fields)
            self.execute(f"UPDATE promos SET {sets} WHERE id = ?", (*fields.values(), promo_id))
        return self.promo(promo_id)

    def scout_page(self, url: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM scout_pages WHERE url = ?", (url,))

    def save_scout_page(self, url: str, content_hash: str, text: str, status: str = "ok") -> None:
        self.execute(
            """
            INSERT INTO scout_pages (url, fetched_at, content_hash, text, status)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET fetched_at = excluded.fetched_at,
                content_hash = excluded.content_hash, text = excluded.text, status = excluded.status
            """,
            (url, now_iso(), content_hash, text, status),
        )

    def create_scout_run(self, dry_run: bool = False, started_at: str | None = None) -> int:
        cur = self.execute(
            "INSERT INTO scout_runs (started_at, status, dry_run) VALUES (?, 'running', ?)",
            (started_at or now_iso(), 1 if dry_run else 0),
        )
        return int(cur.lastrowid or 0)

    def update_scout_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{name} = ?" for name in fields)
        self.execute(f"UPDATE scout_runs SET {sets} WHERE id = ?", (*fields.values(), run_id))

    def scout_run(self, run_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM scout_runs WHERE id = ?", (run_id,))

    def scout_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM scout_runs ORDER BY id DESC LIMIT ?", (max(1, limit),))

    def last_scout_run(self) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM scout_runs ORDER BY id DESC LIMIT 1")

    def create_job(
        self,
        *,
        job_id: str,
        app: str,
        model: str | None,
        require: list[str],
        priority: int,
        request: dict[str, Any],
        callback_url: str | None,
        ttl_s: int = DEFAULT_JOB_TTL_S,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        ts = now_iso()
        self.ensure_app(app)
        deadline = expires_at or to_iso(parse_iso(ts) + timedelta(seconds=ttl_s))
        self.execute(
            """
            INSERT INTO jobs (id, app, model, require, priority, request, callback_url,
                              state, created_at, updated_at, ttl_s, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (
                job_id,
                app,
                model,
                json.dumps(require),
                priority,
                json.dumps(request),
                callback_url,
                ts,
                ts,
                int(ttl_s),
                deadline,
            ),
        )
        return self.job(job_id) or {}

    def job(self, job_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def jobs(
        self, state: str | None = None, app: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Live rows newest first by default; `state="all"` for the history, else one state.

        The default matters: an unfiltered listing that returned the oldest 200 rows of the
        whole table hid every live job behind months of finished ones.
        """
        sql = "SELECT * FROM jobs"
        clauses: list[str] = []
        params: list[Any] = []
        if not state:
            clauses.append(f"state IN ({', '.join('?' * len(LIVE_JOB_STATES))})")
            params.extend(LIVE_JOB_STATES)
        elif state != "all":
            clauses.append("state = ?")
            params.append(state)
        if app:
            clauses.append("app = ?")
            params.append(app)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, params)

    def jobs_in_states(self, states: Iterable[str], limit: int = 1000) -> list[dict[str, Any]]:
        names = list(states)
        return self.query(
            f"SELECT * FROM jobs WHERE state IN ({', '.join('?' * len(names))}) "
            "ORDER BY priority ASC, created_at ASC LIMIT ?",
            (*names, limit),
        )

    def claimable_jobs(self, now: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Jobs that could start right now: not paused, not backing off, oldest priority first."""
        return self.query(
            """
            SELECT j.* FROM jobs j
            LEFT JOIN apps a ON a.app = j.app
            WHERE j.state IN ('queued', 'waiting_quota') AND COALESCE(a.paused, 0) = 0
              AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= ?)
            ORDER BY j.priority ASC, j.created_at ASC LIMIT ?
            """,
            (now or now_iso(), limit),
        )

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{name} = ?" for name in fields)
        self.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))

    def delete_job(self, job_id: str) -> bool:
        return self.execute("DELETE FROM jobs WHERE id = ?", (job_id,)).rowcount > 0

    def job_state_counts(self) -> dict[str, int]:
        rows = self.query("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
        return {row["state"]: int(row["n"]) for row in rows}

    def job_app_counts(self) -> dict[str, int]:
        rows = self.query(
            "SELECT app, COUNT(*) AS n FROM jobs WHERE state IN ('queued', 'waiting_quota', 'running') GROUP BY app"
        )
        return {row["app"]: int(row["n"]) for row in rows}

    def job_app_breakdown(self) -> dict[str, dict[str, int]]:
        """Live rows per app, split into what waits and what runs - the fair share reads this."""
        rows = self.query(
            f"""
            SELECT app, state, COUNT(*) AS n FROM jobs
            WHERE state IN ({", ".join("?" * len(LIVE_JOB_STATES))}) GROUP BY app, state
            """,
            LIVE_JOB_STATES,
        )
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            entry = counts.setdefault(row["app"], {"queued": 0, "running": 0})
            key = "running" if row["state"] == "running" else "queued"
            entry[key] += int(row["n"])
        return counts

    def oldest_queued_at(self) -> str | None:
        row = self.query_one(
            "SELECT MIN(created_at) AS ts FROM jobs WHERE state IN ('queued', 'waiting_quota')"
        )
        return row["ts"] if row and row["ts"] else None

    def jobs_expired_since(self, since: str) -> int:
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM jobs WHERE state = 'expired' AND COALESCE(finished_at, updated_at) >= ?",
            (since,),
        )
        return int(row["n"]) if row else 0

    def purge_terminal_jobs(self, before: str) -> int:
        """Terminal rows are kept for a while so a client can still read the verdict, then go."""
        cur = self.execute(
            f"""
            DELETE FROM jobs WHERE state IN ({", ".join("?" * len(TERMINAL_JOB_STATES))})
              AND COALESCE(finished_at, updated_at) < ?
            """,
            (*TERMINAL_JOB_STATES, before),
        )
        return cur.rowcount

    def usage_rows(self, *, since: str | None, group_by: str, app: str | None = None) -> list[dict[str, Any]]:
        group_sql = {
            "app": "app",
            "model": "model",
            "account": "account",
            "day": "substr(ts, 1, 10)",
            "provider": "provider",
        }[group_by]
        clauses: list[str] = []
        params: list[Any] = []
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if app:
            clauses.append("app = ?")
            params.append(app)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.query(
            f"""
            SELECT {group_sql} AS bucket,
                   COUNT(*) AS requests,
                   COALESCE(SUM(in_tokens), 0) AS in_tokens,
                   COALESCE(SUM(out_tokens), 0) AS out_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   SUM(CASE WHEN status NOT IN ('ok', 'abandoned') THEN 1 ELSE 0 END) AS errors
            FROM usage{where}
            GROUP BY bucket ORDER BY bucket
            """,
            params,
        )

    def usage_timeseries(
        self, *, since: str | None, bucket: str, app: str | None = None
    ) -> list[dict[str, Any]]:
        expr = "substr(ts, 1, 13) || ':00'" if bucket == "hour" else "substr(ts, 1, 10)"
        clauses: list[str] = []
        params: list[Any] = []
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if app:
            clauses.append("app = ?")
            params.append(app)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.query(
            f"""
            SELECT {expr} AS bucket,
                   COUNT(*) AS requests,
                   COALESCE(SUM(in_tokens), 0) AS in_tokens,
                   COALESCE(SUM(out_tokens), 0) AS out_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   SUM(CASE WHEN status NOT IN ('ok', 'abandoned') THEN 1 ELSE 0 END) AS errors
            FROM usage{where}
            GROUP BY bucket ORDER BY bucket
            """,
            params,
        )
