"""Failsafe watchdog + diagnostics for the dashboard worker (additive only).

Purpose
-------
The dashboard historically freezes ~9-10 minutes after start: the uvicorn
process stays alive, port 8787 keeps listening, but the event loop stops
scheduling (so /api/state times out), both strategy scan loops stop
progressing at the same time, stdout/stderr logs freeze, and no
Traceback/Exception is ever logged. On Windows the process later exits.

This module adds, with zero effect on trading:
  * an event-loop heartbeat, stamped on every strategy scan cycle,
  * per-scan timing (start/end/duration), per-network-request duration,
  * a daemon watchdog thread that, when a heartbeat is stale past a
    threshold, dumps full thread stacks via faulthandler to logs/hang-trace.log
    and logs a WARNING -- so the exact blocking line/function is captured the
    next time the hang occurs.
  * a /health probe set for the event loop + both strategy workers.

Heartbeats / staleness are kept in plain datetime fields guarded by a lock.
"""
from __future__ import annotations

import datetime
import faulthandler
import logging
import os
import threading

logger = logging.getLogger("crypto.dashboard")

# Freshness windows (seconds) -- deliberately generous so normal traffic never
# trips them. A >150s event-loop silence or >180s unfinished scan means the
# loop is wedged and we dump stacks immediately (and every 15s thereafter).
LOOP_STALE_AFTER = 150.0
SCAN_STALE_AFTER = 180.0
HEALTH_STALE_TOO_OLD = 300.0         # too old => caller should treat as stale
DUMP_INTERVAL = 15.0                 # cap dump spam while hung
STARTUP_GRACE = 55.0                 # suppress watchdog dumps until first scan has had time to complete
# Debug: if CRYPTO_DUMP_EVERY is set to a number of seconds, dump all-thread
# stacks periodically regardless of staleness (used to capture blocked route
# threads while the scan loops remain healthy).
_DUMP_EVERY = os.environ.get("CRYPTO_DUMP_EVERY")
_PERIODIC_DUMP = float(_DUMP_EVERY) if _DUMP_EVERY else None
HANG_TRACE_DIR = os.getenv("CRYPTO_HANG_TRACE_DIR", "")
if not HANG_TRACE_DIR:
    # The dashboard always runs from the project root (uv run crypto-dashboard).
    HANG_TRACE_DIR = os.path.join(os.getcwd(), "logs")
HANG_TRACE_PATH = os.path.join(HANG_TRACE_DIR, "hang-trace.log")
DEBUG_DUMP_TOGGLE = os.path.join(HANG_TRACE_DIR, ".debug_dump")

# Debug: if CRYPTO_DUMP_EVERY is set to a number of seconds, dump all-thread
# stacks periodically regardless of staleness (used to capture blocked route
# threads while the scan loops remain healthy). A sentinel file
# logs/.debug_dump also enables periodic dumping (works even when the env var
# can't be passed through the launcher chain).
_DUMP_EVERY = os.environ.get("CRYPTO_DUMP_EVERY")
_PERIODIC_DUMP = float(_DUMP_EVERY) if _DUMP_EVERY else (60.0 if os.path.exists(DEBUG_DUMP_TOGGLE) else None)

# A tiny thread-safe heartbeat registry.
_lock = threading.Lock()
_heartbeat_evloop = None            # datetime of last event-loop scan tick
_scan_heartbeats: dict[str, datetime.datetime] = {}   # strategy -> last completion
_started = datetime.datetime.now(datetime.timezone.utc)
_dump_lock = threading.Lock()
_last_dump = None
_watchdog_started = None            # datetime the watchdog thread was started
_last_watchdog_run = None           # datetime of last watchdog iteration
_watchdog_exc = None                # last exception that killed the watchdog (if any)


def touch_event_loop() -> None:
    """Stamp the event-loop heartbeat. Called from the strategy loop coroutine."""
    global _heartbeat_evloop
    with _lock:
        _heartbeat_evloop = datetime.datetime.now(datetime.timezone.utc)


def touch_scan(strategy: str) -> None:
    """Stamp the most-recent scan completion for a strategy."""
    with _lock:
        _scan_heartbeats[strategy] = datetime.datetime.now(datetime.timezone.utc)


def health() -> dict:
    """Snapshot for the /health endpoint (no side effects)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    with _lock:
        ev = _heartbeat_evloop
        scans = dict(_scan_heartbeats)
    loop_age = (now - ev).total_seconds() if ev else None
    scan_age = {k: ((now - v).total_seconds() if v else None) for k, v in scans.items()}
    return {
        "started_at": _started.isoformat(),
        "now": now.isoformat(),
        "loop_last_tick": ev.isoformat() if ev else None,
        "loop_age_seconds": loop_age,
        "loop_fresh": (loop_age is not None and loop_age < LOOP_STALE_AFTER),
        "scan_last": {k: (v.isoformat() if v else None) for k, v in scans.items()},
        "scan_age_seconds": scan_age,
        "scan_fresh": {
            k: (v is not None and v < SCAN_STALE_AFTER) for k, v in scan_age.items()
        },
        "watchdog_started": _watchdog_started.isoformat() if _watchdog_started else None,
        "watchdog_last_run": _last_watchdog_run.isoformat() if _last_watchdog_run else None,
        "watchdog_exc": _watchdog_exc,
        "periodic_dump": _PERIODIC_DUMP,
    }


def _dump_stacks(reason: str) -> None:
    """Write full thread stacks to the hang-trace log (thread-safe, flushed)."""
    global _last_dump
    with _dump_lock:
        now = datetime.datetime.now(datetime.timezone.utc)
        if _last_dump and (now - _last_dump).total_seconds() < DUMP_INTERVAL:
            return
        _last_dump = now
        try:
            os.makedirs(os.path.dirname(HANG_TRACE_PATH), exist_ok=True)
            with open(HANG_TRACE_PATH, "a", encoding="utf-8") as fh:
                fh.write(f"\n===== HANG-TRACE {now.isoformat()} :: {reason} =====\n")
                fh.flush()
                faulthandler.dump_traceback(all_threads=True, file=fh)
                fh.flush()
            logger.warning("HANG-TRACE written to %s :: %s", HANG_TRACE_PATH, reason)
        except Exception:  # noqa: BLE001 - watchdog must never crash the app
            logger.exception("failed to write hang trace")


def watchdog_loop() -> None:
    """Daemon thread: every 10s, if the loop is stale, dump thread stacks."""
    global _last_watchdog_run, _watchdog_exc
    while True:
        try:
            _last_watchdog_run = datetime.datetime.now(datetime.timezone.utc)
            # During the startup grace window, heartbeats haven't been set yet
            # and no scan has completed -- suppress dumps to avoid false-positives.
            uptime = (_last_watchdog_run - _started).total_seconds()
            if uptime < STARTUP_GRACE:
                threading.Event().wait(10.0)
                continue
            info = health()
            stale = []
            if not info["loop_fresh"]:
                stale.append(f"event-loop stale {info['loop_age_seconds']}s")
            for k, v in info["scan_age_seconds"].items():
                if v is not None and v >= SCAN_STALE_AFTER:
                    stale.append(f"{k} scan stale {v:.0f}s")
            if stale:
                _dump_stacks("; ".join(stale))
            elif _PERIODIC_DUMP:  # debug capture of routinely-blocked route threads
                _dump_stacks("periodic (debug)")
        except Exception as exc:  # noqa: BLE001 - watchdog must never die silently
            _watchdog_exc = f"{type(exc).__name__}: {exc}"
            logger.exception("watchdog iteration failed")
        threading.Event().wait(10.0)


def start_watchdog() -> threading.Thread:
    global _watchdog_started
    _watchdog_started = datetime.datetime.now(datetime.timezone.utc)
    t = threading.Thread(target=watchdog_loop, name="dashboard-watchdog", daemon=True)
    t.start()
    logger.info(
        "watchdog started (periodic_dump=%s, toggle=%s)",
        _PERIODIC_DUMP, os.path.exists(DEBUG_DUMP_TOGGLE),
    )
    touch_event_loop()
    return t
