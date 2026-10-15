# TradingBot

A crypto research and paper-trading toolkit: a deterministic signal engine, a
self-hosted dashboard with a paper-trade journal, a bar-by-bar backtester, and
LLM agents (DeepSeek + Agno) for market analysis and reporting.

> **Research / paper-trading only.** Nothing in this repository places live
> orders, and nothing in it is financial advice.

---

## Table of contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Run guide](#run-guide)
  - [1. Clone the repository](#1-clone-the-repository)
  - [2. Install dependencies](#2-install-dependencies)
  - [3. Configure environment variables](#3-configure-environment-variables)
  - [4. Start the dashboard](#4-start-the-dashboard)
  - [5. Run the market signal agent](#5-run-the-market-signal-agent)
  - [6. Run a backtest](#6-run-a-backtest)
  - [7. Regime analysis](#7-regime-analysis)
  - [8. MCP client agent](#8-mcp-client-agent)
- [Configuration reference](#configuration-reference)
- [API reference](#api-reference)
- [Project structure](#project-structure)
- [Documentation](#documentation)

---

## Features

| Component            | Command              | Description                                                        |
| -------------------- | -------------------- | ------------------------------------------------------------------ |
| **Signal engine**    | (library)            | Deterministic multi-factor TA — EMA/RSI/StochRSI/MACD/ATR/Bollinger, candlestick patterns, structural ATR risk (SL/TP). |
| **Dashboard**        | `crypto-dashboard`   | FastAPI + web UI: scheduled scans, paper-trade journal, scorecard, SQLite persistence. |
| **Signal agent**     | `crypto-signals`     | DeepSeek-powered senior-analyst agent that scans the market and writes a per-asset signal report. |
| **Backtester**       | `crypto-backtest`    | Bar-by-bar replay of historical Binance OHLCV through the frozen engine (no look-ahead). |
| **Regime analysis**  | `regime_analysis.py` | Tags trades with market-regime context and writes reports/CSVs.    |
| **MCP agent**        | `crypto-mcp`         | Agno Model Context Protocol client for the Agno docs server.       |

Two strategies run side by side in the dashboard:

- **`v2_swing`** — 1h candles, active paper trading (auto-open enabled).
- **`v3_scalp`** — 5m candles, frozen in **OBSERVE** mode: signals and
  hypothetical sizes are recorded, but no trades open until the flag is changed.

Execution is `PAPER` for every strategy; the `execution_mode` field is reserved
for future LIVE routing. No live orders are ever placed.

---

## Architecture

```
                 ┌──────────────────────────────────────────────┐
  data layer     │ CoinGecko  → top coins, price/volume/24h/7d  │
                 │ yfinance   → OHLCV candles (15m/1h/4h/1d)    │
                 │ Binance    → historical OHLCV (backtests)    │
                 └───────────────────┬──────────────────────────┘
                                     │
                 ┌───────────────────▼──────────────────────────┐
  signal engine  │ indicators.py  deterministic TA              │
                 │ patterns.py    engulfing / pinbar / sweeps   │
                 │ risk.py        structural stops + vol TP     │
                 │ signals.py     composite engine (v1/v2/v3)   │
                 └───────────────────┬──────────────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        │                            │                            │
┌───────▼────────┐        ┌──────────▼─────────┐       ┌──────────▼─────────┐
│ crypto-dashboard│       │ crypto-backtest    │       │ crypto-signals     │
│ FastAPI + UI    │       │ historical replay  │       │ DeepSeek agent     │
│ SQLite journal  │       │ → backtest_results │       │ markdown report    │
└────────────────┘        └────────────────────┘       └────────────────────┘
```

---

## Requirements

- **Python 3.12+** (the project pins `3.12` in `.python-version`)
- **[uv](https://docs.astral.sh/uv/)** — Python package and project manager
- **Git**
- Network access to CoinGecko and Yahoo Finance (and Binance for backtests)
- A **DeepSeek API key** — only required for `crypto-signals` (the dashboard
  and backtester work without any API key)

---

## Run guide

### 1. Clone the repository

```bash
git clone https://github.com/irXan/TradingBot.git
cd TradingBot
```

### 2. Install dependencies

```bash
uv sync
```

This creates `.venv/` and installs everything from `uv.lock` (agno, FastAPI,
uvicorn, pandas, yfinance, statsmodels, httpx, python-dotenv) plus the local
`crypto` package, which exposes four console commands:

`crypto-dashboard`, `crypto-signals`, `crypto-backtest`, `crypto-mcp`.

Verify the install:

```bash
uv run crypto-backtest --help
```

### 3. Configure environment variables

Create a `.env` file in the project root (it is gitignored):

```env
# Required only for the LLM signal agent (crypto-signals)
DEEPSEEK_API_KEY=sk-...

# Optional overrides (defaults shown)
# DEEPSEEK_MODEL_ID=deepseek-v4-flash
# DASHBOARD_HOST=127.0.0.1
# DASHBOARD_PORT=8787
# DASHBOARD_SCAN_SECONDS=60
# DASHBOARD_STRATEGY=v2_swing
# EXECUTION_MODE=SPOT
```

`.env` is loaded automatically — no need to export variables manually.

### 4. Start the dashboard

```bash
uv run crypto-dashboard
```

Then open **http://127.0.0.1:8787** (append `/docs` for interactive OpenAPI).

The dashboard starts background scan loops for `v2_swing` (1h) and `v3_scalp`
(5m), stores signals and paper trades in a local `dashboard.db` (SQLite,
gitignored), and auto-resolves open trades to `TP_HIT` (+2R) or `SL_HIT` (-1R)
on each scan.

**Windows one-liner** (runs detached with logs under `logs/`):

```bat
start_dashboard.bat
```

If the port is already in use, run on another one:

```powershell
$env:DASHBOARD_PORT = '8788'; uv run crypto-dashboard
```

Stop it with `Ctrl+C` (or end the process if started via the `.bat`).

### 5. Run the market signal agent

Requires `DEEPSEEK_API_KEY` in `.env`.

```bash
uv run crypto-signals                            # top-10 coins, 1d candles
uv run crypto-signals --limit 5 --interval 4h    # top-5 coins on 4h candles
uv run crypto-signals --message "Focus on BTC and ETH only"
```

Output per asset: **direction** (LONG/SHORT/NEUTRAL), **confidence** (0–100),
**entry**, **stop-loss** and **take-profit** (ATR-based, ~2:1 R:R), plus market
regime and risk warnings.

### 6. Run a backtest

Research only — results are tagged `source='backtest'` and do **not** count
toward the live n≥50 forward-test sample.

```bash
uv run crypto-backtest ^
  --symbols BTC,ETH,SOL ^
  --interval 1h ^
  --start 2025-01-01 --end 2025-12-31 ^
  --equity 10000 --risk-pct 1.0 ^
  --out backtest_results
```

On bash/zsh use `\` instead of `^`.

Outputs written to `--out`:

- `backtest_trades.csv` — every trade with entry/exit/SL/TP and R-multiple
- `backtest_report.md` / `backtest_report.json` — summary statistics

Methodology: signals are computed on the close of bar `t` using only data up to
`t`; entries fill at the open of `t+1`; SL is checked before TP (conservative).
Re-runs are idempotent (no duplicate DB rows). Add `--no-db` to skip SQLite.

### 7. Regime analysis

Tags trades in `dashboard.db` with market-regime context:

```bash
uv run python regime_analysis.py --source backtest   # backtest trades
uv run python regime_analysis.py --source live       # live/paper trades
```

Writes `regime_report.md` (or `regime_report_backtest.md`) and
`trade_regime.csv`, populated into a shared `trade_regime` table keyed by
`(id, source)` so backtest and live samples never contaminate each other.

### 8. MCP client agent

Connects to the Agno documentation MCP server over streamable-http:

```bash
uv run crypto-mcp --list-tools              # list available tools
uv run crypto-mcp "What is MCP support in Agno?"
```

---

## Configuration reference

| Variable                 | Default                            | Purpose                                        |
| ------------------------ | ---------------------------------- | ---------------------------------------------- |
| `DEEPSEEK_API_KEY`       | — (required for `crypto-signals`)  | DeepSeek API key, read from `.env`             |
| `DEEPSEEK_MODEL_ID`      | `deepseek-v4-flash`                | DeepSeek model id                              |
| `CRYPTO_MODEL_ID`        | `gpt-5.2`                          | LLM model id used by the MCP agent             |
| `AGNO_MCP_URL`           | `https://docs.agno.com/mcp`        | MCP server endpoint                            |
| `COINGECKO_URL`          | `https://api.coingecko.com/api/v3` | CoinGecko API base URL                         |
| `ENGINE_VERSION`         | `v1`                               | Signal engine: `v1` legacy or `v2` low-noise matrix |
| `EXECUTION_MODE`         | `SPOT`                             | `SPOT` or `FUTURES` (fee schedule + regime guard) |
| `SPOT_TAKER_FEE`         | `0.001`                            | Spot taker fee (0.10%)                         |
| `FUTURES_TAKER_FEE`      | `0.0004`                           | Futures taker fee (0.04%)                      |
| `SLIPPAGE_BUFFER`        | `0.0005`                           | Order-book slippage penalty (0.05%)            |
| `DASHBOARD_HOST`         | `127.0.0.1`                        | Dashboard bind address (`0.0.0.0` to expose)   |
| `DASHBOARD_PORT`         | `8787`                             | Dashboard port                                 |
| `DASHBOARD_STRATEGY`     | `v2_swing`                         | Default strategy shown by the UI               |
| `DASHBOARD_SCAN_SECONDS` | `60`                               | Seconds between automatic scans                |
| `DASHBOARD_INTERVAL`     | `1d`                               | Candle interval for the scan loop              |
| `DASHBOARD_SCAN_LIMIT`   | `10`                               | Top-N coins per scan                           |
| `CRYPTO_DB`              | `<repo>/dashboard.db`              | SQLite database location                       |
| `MIN_CASH_TO_OPEN`       | `10.0`                             | Minimum cash required to open a paper trade    |
| `ALLOC_FLOOR_PCT`        | `10.0`                             | Min equity slice for conviction-weighted sizing |
| `ALLOC_CEILING_PCT`      | `40.0`                             | Max equity slice for conviction-weighted sizing |

Paper-trade sizing: `notional = risk_amount / stop_distance_pct`. Every close
computes a **net R-multiple** after fees and slippage, stored next to the gross
`r_multiple`. The scorecard enforces the go/no-go bars: **n ≥ 50**, win rate
**> 40%**, expectancy **> +0.30R**, profit factor **> 1.5**.

---

## API reference

| Endpoint                | Method | Purpose                                        |
| ----------------------- | ------ | ---------------------------------------------- |
| `/`                     | GET    | Dashboard UI                                   |
| `/health`               | GET    | Liveness check                                 |
| `/api/state`            | GET    | Latest signals + open/closed trades + settings |
| `/api/scan`             | POST   | Force an immediate scan                        |
| `/api/trades`           | POST   | Open tracked paper trades (max 5 concurrent)   |
| `/api/trades/{id}`      | DELETE | Close a trade manually at the latest price     |
| `/api/strategies/{name}`| PATCH  | Toggle AUTO/MANUAL open + execution readiness  |
| `/api/scorecard`        | GET    | Win rate / expectancy / profit factor vs bars  |
| `/api/settings`         | GET/PUT| Account size, risk %, strategy config          |
| `/api/candles/{symbol}` | GET    | OHLCV candles for charting                     |
| `/api/reset`            | POST   | Reset the forward-test sample                  |

---

## Project structure

```
TradingBot/
├── src/crypto/
│   ├── app.py              # FastAPI dashboard + background scan loops
│   ├── service.py          # scan / open / resolve orchestration
│   ├── store.py            # SQLite persistence layer
│   ├── config.py           # execution mode, fees, strategy configs
│   ├── market_data.py      # CoinGecko + yfinance data layer
│   ├── indicators.py       # deterministic technical indicators
│   ├── patterns.py         # candlestick pattern detection
│   ├── risk.py             # ATR-based structural SL / vol-stretched TP
│   ├── signals.py          # composite signal engine (v1 / v2 dispatch)
│   ├── backtest.py         # bar-by-bar historical replay runner
│   ├── signal_agent.py     # DeepSeek market-signal agent CLI
│   ├── mcp_agent.py        # Agno MCP client agent CLI
│   ├── heartbeat.py        # event-loop watchdog + hang diagnostics
│   ├── strategies/
│   │   ├── v2_swing/       # 1h swing strategy (active paper trading)
│   │   └── v3_scalp/       # 5m scalp strategy (observe baseline)
│   └── static/index.html   # dashboard front end
├── designs/                # design notes (regime/slippage, derivatives, plan)
├── backtest_results/       # generated backtest reports + trade CSVs
├── regime_analysis.py      # trade regime tagging script
├── BACKTEST_DESIGN.md      # backtest methodology design document
├── start_dashboard.bat     # Windows launcher (logs to logs/)
├── pyproject.toml          # dependencies + console entry points
└── uv.lock                 # locked dependency versions
```

---

## Documentation

- [`BACKTEST_DESIGN.md`](BACKTEST_DESIGN.md) — full backtest design: no
  look-ahead rules, fill model, SL-before-TP ordering, DB tagging.
- [`designs/02_regime_slippage.md`](designs/02_regime_slippage.md) — regime
  tagging and slippage model.
- [`designs/03_derivatives_overlay.md`](designs/03_derivatives_overlay.md) —
  derivatives data overlay notes.
- [`designs/04_dual_track_plan.md`](designs/04_dual_track_plan.md) — dual-track
  research / live-test plan.

---

## Disclaimer

This project is for **education and research**. Crypto assets are volatile and
leverage amplifies losses. Signals, reports, backtests, and paper trades
produced here are hypothetical and **are not financial advice**. Do not trade
real capital on the basis of this software without your own validation and
risk controls.
