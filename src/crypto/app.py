"""FastAPI backend for the crypto signal dashboard.

Endpoints:
    GET    /api/state                  latest signals + positions + settings (one poll)
    POST   /api/scan                   force a market scan for a strategy now
    POST   /api/trades                 open tracked paper trades for chosen symbols (manual)
    PATCH  /api/strategies/{name}      toggle AUTO/MANUAL open + execution readiness
    DELETE /api/trades/{id}            close a trade manually
    GET    /api/scorecard              journal stats vs go/no-go bars for a strategy
    PUT    /api/settings               account size / risk %
    GET    /api/settings               strategy config for the dashboard
    GET    /api/candles/{symbol}       OHLCV for charting

Each strategy (v2_swing, v3_scalp) runs its own background loop on its own
interval and cadence, scanning and auto-opening fresh signals in PAPER mode.
Execution is steered by each strategy's `execution_mode` flag — reserved for
future LIVE routing; no live orders are ever placed today.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config as _cfg
from . import heartbeat
from . import service, store, strategies as strat

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("crypto.app")

SCAN_SECONDS = int(os.getenv("DASHBOARD_SCAN_SECONDS", "900"))
DEFAULT_INTERVAL = os.getenv("DASHBOARD_INTERVAL", "1d")
DEFAULT_STRATEGY = os.getenv("DASHBOARD_STRATEGY", "v2_swing")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _valid_strategy(name: str) -> str:
    if not strat.is_known(name):
        raise HTTPException(status_code=404, detail=f"Unknown strategy '{name}'")
    return name


async def _strategy_loop(name: str) -> None:
    """Scan one strategy forever at its own cadence; auto-open in PAPER/AUTO.
    One failed cycle must not kill the loop."""
    cfg = _cfg.get_strategy_config(name)
    seconds = int(cfg["scan_seconds"])
    while True:
        try:
            heartbeat.touch_event_loop()
            _t0 = asyncio.get_running_loop().time()
            summary = await asyncio.to_thread(service.scan_once, name)
            _scan_dur = asyncio.get_running_loop().time() - _t0
            heartbeat.touch_scan(name)
            logger.info("scan %s: %s coins / %s errors / %.1fs",
                        name, summary["scanned"], len(summary["errors"]), _scan_dur)
            if cfg["auto_open"] and cfg["execution_mode"].upper() == "PAPER" \
                    and not _cfg.strategy_observe(name):
                auto = await asyncio.to_thread(service.auto_open, name)
                if auto["opened"]:
                    logger.info("%s AUTO opened %d trade(s)", name, len(auto["opened"]))
        except Exception:  # noqa: BLE001
            logger.exception("scheduled scan failed for %s", name)
        await asyncio.sleep(seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.count_active_trades()  # initializes the SQLite schema on boot
    heartbeat.start_watchdog()   # faulthandler-based hang diagnostics (additive)
    try:
        resolved = await asyncio.wait_for(
            asyncio.to_thread(service.replay_missed_exits), timeout=30
        )
        if resolved:
            logger.info("replay resolved %d trades on startup", len(resolved))
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        logger.warning("startup replay skipped (%s) — will reconcile on next scan", exc)
    workers = [asyncio.create_task(_strategy_loop(name)) for name in _cfg.known_strategies()]
    yield
    for w in workers:
        w.cancel()
    for w in workers:
        with contextlib.suppress(asyncio.CancelledError):
            await w


app = FastAPI(title="crypto-signals dashboard", lifespan=lifespan)


class ScanRequest(BaseModel):
    strategy: str = DEFAULT_STRATEGY
    refresh_cache: bool = False


class TradeRequest(BaseModel):
    strategy: str = DEFAULT_STRATEGY
    symbols: list[str] = Field(min_length=1)


class StrategyRequest(BaseModel):
    auto_open: bool | None = None


class SettingsRequest(BaseModel):
    account_size: float = Field(gt=0)
    risk_pct: float = Field(gt=0, le=5)


class ResetRequest(BaseModel):
    strategy: str = "all"


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, Any]:
    """Non-trading liveness probe.

    200 -> event loop fresh and both strategy scan workers within their
    freshness windows. 503 -> stale with diagnostic timestamps so the caller
    can see exactly which worker/loop stopped progressing.
    """
    from fastapi import Response

    info = heartbeat.health()
    ok = info["loop_fresh"] and all(info["scan_fresh"].values())
    status = 200 if ok else 503
    return Response(
        content=__import__("json").dumps(info),
        status_code=status,
        media_type="application/json",
    )


@app.get("/api/state")
def get_state(strategy: str = DEFAULT_STRATEGY) -> dict[str, Any]:
    _valid_strategy(strategy)
    cfg = _cfg.get_strategy_config(strategy)
    interval = cfg["interval"]
    scan = store.latest_scan(interval, strategy=strategy)
    seconds_left = None
    if scan:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(scan["ts"])).total_seconds()
        seconds_left = max(0, round(cfg["scan_seconds"] - elapsed))
    return {
        "strategy": strategy,
        "scan": scan,
        "scan_seconds_left": seconds_left,
        "positions": service.positions_view(strategy),
        "settings": store.get_settings(),
        "strategies": {
            name: {
                "capital": c["capital"],
                "risk_pct": c["risk_pct"],
                "max_active_trades": c["max_active_trades"],
                "execution_mode": c["execution_mode"],
                "interval": c["interval"],
                "auto_open": c["auto_open"],
                "observe": c.get("observe", False),
            }
            for name in _cfg.known_strategies()
            for c in [_cfg.get_strategy_config(name)]
        },
        "active_count": store.count_active_trades(strategy),
        "max_active": cfg["max_active_trades"],
    }


@app.post("/api/scan")
def post_scan(req: ScanRequest) -> dict[str, Any]:
    _valid_strategy(req.strategy)
    if req.refresh_cache:
        service.clear_cache()
    try:
        return service.scan_once(req.strategy)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Scan failed: {exc}") from exc


@app.post("/api/trades")
def post_trades(req: TradeRequest) -> dict[str, Any]:
    _valid_strategy(req.strategy)
    try:
        return service.open_trades(req.strategy, req.symbols)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.patch("/api/strategies/{name}")
def patch_strategy(name: str, req: StrategyRequest) -> dict[str, Any]:
    """Toggle a strategy's AUTO/MANUAL open behavior (in-memory)."""
    _valid_strategy(name)
    if _cfg.strategy_observe(name):
        raise HTTPException(
            status_code=403,
            detail=f"{name} is frozen in OBSERVE mode — {_cfg.OBSERVE_BANNER}. "
                   f"auto-open cannot be re-enabled without a config change.",
        )
    cfg = _cfg.get_strategy_config(name)
    if req.auto_open is not None:
        cfg["auto_open"] = req.auto_open
        _cfg.STRATEGIES[name]["auto_open"] = req.auto_open
    return {"strategy": name, "auto_open": cfg["auto_open"],
            "execution_mode": cfg["execution_mode"]}


@app.delete("/api/trades/{trade_id}")
def delete_trade(trade_id: int) -> dict[str, Any]:
    try:
        return service.close_trade(trade_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("close failed for trade %s", trade_id)
        raise HTTPException(status_code=500, detail=f"Close failed: {exc}") from exc


@app.get("/api/scorecard")
def get_scorecard(strategy: str = DEFAULT_STRATEGY) -> dict[str, Any]:
    _valid_strategy(strategy)
    return service.scorecard(strategy)


@app.post("/api/reset")
def reset_forward_test(req: ResetRequest | None = None) -> dict[str, Any]:
    strategy = (req.strategy if req and req.strategy and req.strategy != "all"
                else None)
    if strategy:
        _valid_strategy(strategy)
    log = service.reset_forward_test(strategy)
    return {"status": "ok", **log}


@app.put("/api/settings")
def put_settings(req: SettingsRequest) -> dict[str, float]:
    return store.set_settings({"account_size": req.account_size, "risk_pct": req.risk_pct})


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return {"settings": store.get_settings(), "strategies": _cfg.STRATEGIES}


@app.get("/api/candles/{symbol}")
def get_candles(symbol: str, interval: str = DEFAULT_INTERVAL) -> list[dict[str, Any]]:
    try:
        df = service._cached_ohlcv(symbol.upper(), interval)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"No candles for {symbol}: {exc}") from exc
    out = []
    for ts, row in df.tail(400).iterrows():
        out.append(
            {
                "time": int(ts.timestamp()),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            }
        )
    return out


def main() -> None:
    import socket

    import uvicorn

    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8787"))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if probe.connect_ex((host, port)) == 0:
            raise SystemExit(
                f"Port {port} is already in use — the dashboard is probably still running "
                f"at http://{host}:{port}. Open it, or free the port first:\n"
                f"  PowerShell: Get-NetTCPConnection -LocalPort {port} | Select OwningProcess\n"
                f"              Stop-Process -Id <pid> -Force\n"
                f"  Or run on another port: $env:DASHBOARD_PORT = '8788'"
            )
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
