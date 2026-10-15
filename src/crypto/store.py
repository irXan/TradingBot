"""SQLite persistence layer for the signal dashboard.

Stores scan results (signals), tracked paper trades, and risk settings in a
single file at the project root (override with CRYPTO_DB).

Concurrency note (hang fix): a single shared connection guarded by one RLock is
NOT used anymore. If the lock-owner thread ever wedges inside a native sqlite
call, every other thread that touches the DB (FastAPI sync routes AND the scan
worker threads) blocks forever on ``_lock.acquire()`` while the event loop
keeps running -- the recurring "dashboard freezes but stays alive" symptom seen
in production. Instead each call uses a short-lived connection (SQLite handles
concurrency itself), with WAL journaling and a busy timeout so any contention
surfaces as a bounded 15s error instead of a process-wide deadlock. Semantics,
schema, and formatting of every function are unchanged.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("CRYPTO_DB", Path(__file__).resolve().parents[2] / "dashboard.db"))

MAX_ACTIVE_TRADES = 5

DEFAULT_SETTINGS: dict[str, float] = {"account_size": 10000.0, "risk_pct": 1.0}

# Serializes only the once-per-process schema init and the read-insert atomic
# backtest import. It is never held across network calls or long sqlite work.
_schema_lock = threading.Lock()
_schema_ready = False

# Guards cross-statement read-modify-write operations (idempotent backtest
# import, settings upsert). Held only for fast sqlite statements.
_atomic_lock = threading.Lock()

_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS scans (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       TEXT NOT NULL,
        interval TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_scans_interval ON scans (interval, id);

    CREATE TABLE IF NOT EXISTS signals (
        scan_id     INTEGER NOT NULL,
        strategy    TEXT NOT NULL DEFAULT 'v2_swing',
        symbol      TEXT NOT NULL,
        name        TEXT,
        rank        INTEGER,
        price       REAL,
        change_24h  REAL,
        direction   TEXT NOT NULL,
        confidence  INTEGER NOT NULL,
        score       REAL NOT NULL,
        entry       REAL NOT NULL,
        stop_loss   REAL NOT NULL,
        take_profit REAL NOT NULL,
        factors     TEXT NOT NULL,
        indicators  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        opened_at   TEXT NOT NULL,
        closed_at   TEXT,
        strategy    TEXT NOT NULL DEFAULT 'v2_swing',
        symbol      TEXT NOT NULL,
        interval    TEXT NOT NULL,
        direction   TEXT NOT NULL,
        entry       REAL NOT NULL,
        stop_loss   REAL NOT NULL,
        take_profit REAL NOT NULL,
        size_quote  REAL NOT NULL,
        risk_quote  REAL NOT NULL,
        status      TEXT NOT NULL DEFAULT 'OPEN',
        exit_price  REAL,
        r_multiple  REAL,
        net_r_score REAL,
        entry_slippage REAL,
        exit_slippage  REAL,
        fee_paid    REAL,
        source      TEXT NOT NULL DEFAULT 'live',
        note        TEXT,
        target_risk_quote REAL,
        actual_risk_quote REAL,
        risk_utilization_pct REAL,
        remaining_cash_after_entry REAL,
        cash_capped INTEGER NOT NULL DEFAULT 0,
        cash_cap_reason TEXT,
        legacy_uncapped INTEGER NOT NULL DEFAULT 0,
        perf_excluded INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status);

CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_signals_scan_strategy
        ON signals (scan_id, strategy);
    CREATE INDEX IF NOT EXISTS idx_scans_ts
        ON scans (ts);
    """

_MIGRATIONS = {
    "net_r_score": "ALTER TABLE trades ADD COLUMN net_r_score REAL",
    "entry_slippage": "ALTER TABLE trades ADD COLUMN entry_slippage REAL",
    "exit_slippage": "ALTER TABLE trades ADD COLUMN exit_slippage REAL",
    "fee_paid": "ALTER TABLE trades ADD COLUMN fee_paid REAL",
    "source": "ALTER TABLE trades ADD COLUMN source TEXT NOT NULL DEFAULT 'live'",
    "strategy": "ALTER TABLE trades ADD COLUMN strategy TEXT NOT NULL DEFAULT 'v2_swing'",
    "note": "ALTER TABLE trades ADD COLUMN note TEXT",
    "target_risk_quote": "ALTER TABLE trades ADD COLUMN target_risk_quote REAL",
    "actual_risk_quote": "ALTER TABLE trades ADD COLUMN actual_risk_quote REAL",
    "risk_utilization_pct": "ALTER TABLE trades ADD COLUMN risk_utilization_pct REAL",
    "remaining_cash_after_entry": "ALTER TABLE trades ADD COLUMN remaining_cash_after_entry REAL",
    "cash_capped": "ALTER TABLE trades ADD COLUMN cash_capped INTEGER NOT NULL DEFAULT 0",
    "cash_cap_reason": "ALTER TABLE trades ADD COLUMN cash_cap_reason TEXT",
    "legacy_uncapped": "ALTER TABLE trades ADD COLUMN legacy_uncapped INTEGER NOT NULL DEFAULT 0",
    "perf_excluded": "ALTER TABLE trades ADD COLUMN perf_excluded INTEGER NOT NULL DEFAULT 0",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    """Open a brand-new short-lived connection (rollback journal, busy timeout, 3.12 rows).

    Note: journal mode is set once per process in `_ensure_schema`. Setting it
    on every connect is avoided: on Windows, issuing ``PRAGMA journal_mode=WAL``
    from many concurrent connections can wedge natively inside sqlite (holding an
    exclusive file lock that every other reader queues behind indefinitely).
    """
    global _schema_ready
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000;")
    if not _schema_ready:
        with _schema_lock:
            if not _schema_ready:
                _ensure_schema(conn)
                _schema_ready = True
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create/migrate the schema once per process (idempotent, no data churn)."""
    conn.executescript(
        _SCHEMA_SQL
    )
    # Rollback-journal mode (one-time). Deletes any leftover -wal/-shm files and
    # avoids the per-connection WAL switch that can wedge readers on Windows.
    conn.execute("PRAGMA journal_mode=DELETE;")
    # Schema migration: ensure new columns exist for pre-existing DBs
    cur = conn.execute("PRAGMA table_info(signals)")
    sig_cols = {r[1] for r in cur.fetchall()}
    if "strategy" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN strategy TEXT NOT NULL DEFAULT 'v2_swing'")
    cur = conn.execute("PRAGMA table_info(trades)")
    cols = {r[1] for r in cur.fetchall()}
    for col, stmt in _MIGRATIONS.items():
        if col not in cols:
            conn.execute(stmt)
    conn.commit()


class _db(object):
    """Context manager yielding a fresh short-lived connection (auto-close).

    Acquires no shared lock, so a wedged native sqlite call in one thread can
    never block other threads' database access (the previous global RLock +
    shared connection could and did).
    """

    def __enter__(self) -> sqlite3.Connection:
        self._conn: sqlite3.Connection = _connect()
        return self._conn

    def __exit__(self, *exc) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        return None


def save_scan(interval: str, rows: list[dict[str, Any]], strategy: str = "v2_swing") -> str:
    """Persist one full scan snapshot under a fresh autoincrement id; returns ts."""
    ts = _utcnow()
    with _db() as conn:
        cur = conn.execute("INSERT INTO scans (ts, interval) VALUES (?, ?)", (ts, interval))
        scan_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO signals (
                scan_id, strategy, symbol, name, rank, price, change_24h,
                direction, confidence, score, entry, stop_loss, take_profit,
                factors, indicators
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    scan_id,
                    strategy,
                    r["symbol"],
                    r.get("name"),
                    i + 1,
                    r["price"],
                    r.get("change_24h"),
                    r["direction"],
                    r["confidence"],
                    r["score"],
                    r["entry"],
                    r["stop_loss"],
                    r["take_profit"],
                    json.dumps(r["factors"]),
                    json.dumps(r["indicators"]),
                )
                for i, r in enumerate(rows)
            ],
        )
        conn.commit()
    return ts


def latest_scan(interval: str, strategy: str = "v2_swing") -> dict[str, Any] | None:
    """Return the most recent scan for `interval`+`strategy` as {ts, interval, signals}."""
    with _db() as conn:
        row = conn.execute(
            "SELECT id, ts FROM scans WHERE interval = ? "
            "AND EXISTS (SELECT 1 FROM signals s WHERE s.scan_id = scans.id AND s.strategy = ?) "
            "ORDER BY id DESC LIMIT 1",
            (interval, strategy),
        ).fetchone()
        if row is None:
            return None
        ts = row["ts"]
        raws = conn.execute(
            "SELECT * FROM signals WHERE scan_id = ? AND strategy = ? ORDER BY rank",
            (row["id"], strategy),
        ).fetchall()
    signals = []
    for r in raws:
        d = dict(r)
        d["factors"] = json.loads(d["factors"])
        d["indicators"] = json.loads(d["indicators"])
        signals.append(d)
    return {"ts": ts, "interval": interval, "strategy": strategy, "signals": signals}


def open_trade(payload: dict[str, Any]) -> dict[str, Any]:
    with _db() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                opened_at, strategy, symbol, interval, direction, entry, stop_loss,
                take_profit, size_quote, risk_quote, source,
                target_risk_quote, actual_risk_quote, risk_utilization_pct,
                remaining_cash_after_entry, cash_capped, cash_cap_reason, legacy_uncapped
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utcnow(),
                payload.get("strategy", "v2_swing"),
                payload["symbol"],
                payload["interval"],
                payload["direction"],
                payload["entry"],
                payload["stop_loss"],
                payload["take_profit"],
                payload["size_quote"],
                payload["risk_quote"],
                payload.get("source", "live"),
                payload.get("target_risk_quote"),
                payload.get("actual_risk_quote"),
                payload.get("risk_utilization_pct"),
                payload.get("remaining_cash_after_entry"),
                1 if payload.get("cash_capped") else 0,
                payload.get("cash_cap_reason"),
                1 if payload.get("legacy_uncapped") else 0,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def count_active_trades(strategy: str = "v2_swing") -> int:
    with _db() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'OPEN' AND strategy = ?"
            " AND perf_excluded = 0", (strategy,)
        ).fetchone()
    return int(n)


def get_trade(trade_id: int) -> dict[str, Any] | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None


def set_legacy_uncapped(trade_id: int) -> dict[str, Any] | None:
    """Flag an existing paper trade as legacy (opened before cash-only SPOT sizing).

    The recorded entry/size/history are left untouched; only the marker is set
    so the dashboard can warn that a position predates the cash cap.
    """
    with _db() as conn:
        conn.execute("UPDATE trades SET legacy_uncapped = 1 WHERE id = ?", (trade_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None


def exclude_from_perf(trade_id: int, note: str | None = None) -> dict[str, Any] | None:
    """Mark a trade as excluded from all performance evaluation (perf_excluded=1).

    Used when resetting the forward-test: the trade row stays in the DB for
    audit/history, but is filtered out of every metric and of the cash account.
    """
    with _db() as conn:
        conn.execute(
            "UPDATE trades SET perf_excluded = 1, note = COALESCE(?, note) WHERE id = ?",
            (note, trade_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None


def count_signals_since(strategy: str, direction: str, since_iso: str) -> int:
    """Count stored signals for `strategy`+`direction` scanned at/after an ISO
    UTC timestamp. Used to tally observed-but-not-opened signals in OBSERVE
    mode (their scan rows are stored exactly like any other signal)."""
    with _db() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM signals "
            "JOIN scans ON scans.id = signals.scan_id "
            "WHERE signals.strategy = ? AND signals.direction = ? AND scans.ts >= ?",
            (strategy, direction, since_iso),
        ).fetchone()
    return int(n)


def list_trades(status: str | None = None, source: str | None = None,
                strategy: str | None = None,
                include_excluded: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM trades"
    after: list[str] = []
    params: list[Any] = []
    if status:
        after.append("status = ?")
        params.append(status)
    if source:
        after.append("source = ?")
        params.append(source)
    if strategy:
        after.append("strategy = ?")
        params.append(strategy)
    if not include_excluded:
        after.append("perf_excluded = 0")
    if after:
        query += " WHERE " + " AND ".join(after)
    query += " ORDER BY id DESC"
    with _db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def insert_backtest_trades(rows: list[dict[str, Any]]) -> int:
    """Insert completed backtest trades verbatim (explicit timestamps, source='backtest').

    Returns the number of rows inserted. Reuses the trades schema so backtest
    trades are queryable alongside live ones but separable via source.

    Idempotent: rows already present for the same (symbol, opened_at) with
    source='backtest' are skipped, so re-running a backtest does not duplicate
    trades in the journal.
    """
    if not rows:
        return 0
    with _atomic_lock, _db() as conn:
        existing = {
            (r["symbol"], r["opened_at"])
            for r in conn.execute(
                "SELECT symbol, opened_at FROM trades WHERE source = 'backtest'"
            ).fetchall()
        }
        fresh = [r for r in rows if (r["symbol"], r["opened_at"]) not in existing]
        if not fresh:
            return 0
        conn.executemany(
            """
            INSERT INTO trades (
                opened_at, closed_at, strategy, symbol, interval, direction,
                entry, stop_loss, take_profit, size_quote, risk_quote,
                status, exit_price, r_multiple, net_r_score,
                entry_slippage, exit_slippage, fee_paid, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["opened_at"], r["closed_at"], r.get("strategy", "v2_swing"),
                    r["symbol"], r["interval"], r["direction"], r["entry"],
                    r["stop_loss"], r["take_profit"], r["size_quote"], r["risk_quote"],
                    r["status"], r["exit_price"], r.get("r_multiple"), r.get("net_r_score"),
                    r.get("entry_slippage"), r.get("exit_slippage"),
                    r.get("fee_paid"), "backtest",
                )
                for r in fresh
            ],
        )
        conn.commit()
        return len(fresh)


def close_trade(trade_id: int, exit_price: float | None, status: str,
                r_multiple: float | None = None, net_r_score: float | None = None,
                note: str | None = None) -> dict[str, Any] | None:
    with _db() as conn:
        trade = get_trade(trade_id)
        if trade is None:
            return None
        from . import config as _cfg
        direction = trade["direction"]
        entry = trade["entry"]
        buf = _cfg.SLIPPAGE_BUFFER
        fee = _cfg.active_taker_fee()
        if exit_price is not None:
            entry_slip = entry * (1 + buf) if direction == "LONG" else entry * (1 - buf)
            exit_slip = exit_price * (1 - buf) if direction == "LONG" else exit_price * (1 + buf)
            fee_total = entry_slip * fee + exit_slip * fee
        else:
            entry_slip = exit_slip = fee_total = None
        conn.execute(
            """UPDATE trades SET status = ?, closed_at = ?, exit_price = ?,
               r_multiple = ?, net_r_score = ?,
               entry_slippage = ?, exit_slippage = ?, fee_paid = ?, note = ?
               WHERE id = ?""",
            (status, _utcnow(), exit_price,
             round(r_multiple, 4) if r_multiple is not None else None,
             round(net_r_score, 4) if net_r_score is not None else None,
             round(entry_slip, 8) if entry_slip is not None else None,
             round(exit_slip, 8) if exit_slip is not None else None,
             round(fee_total, 8) if fee_total is not None else None,
             note, trade_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None


def get_settings() -> dict[str, float]:
    with _db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULT_SETTINGS)
    for r in rows:
        if r["key"] in out:
            try:
                out[r["key"]] = float(r["value"])
            except ValueError:
                pass
    return out


def set_settings(values: dict[str, float]) -> dict[str, float]:
    with _atomic_lock, _db() as conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, str(v)) for k, v in values.items()],
        )
        conn.commit()
    return get_settings()

