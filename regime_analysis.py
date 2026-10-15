"""Regime analysis for crypto signal engine.

Loads closed trades, joins them to the closest signal snapshot at entry time,
computes regime tags, runs regression analysis on net_r_score, and outputs
a markdown report + CSV.

Usage:
    uv run python regime_analysis.py

Output:
    regime_report.md   — markdown summary of findings
    trade_regime.csv   — trades with regime tags for further analysis
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

DB_PATH = Path(__file__).resolve().parent / "dashboard.db"
OUTPUT_DIR = Path(__file__).resolve().parent

# ── Regime tag definitions ──────────────────────────────────────────────────
# These are the fields we extract from the signal snapshot at trade entry.
# All come from existing columns — no new indicators invented.

REGIME_FIELDS = [
    "gradient_trend",        # signed trend strength (v2 only, NULL for v1 scans)
    "atr_pct",               # ATR as % of price (available in all scans)
    "volume_ratio_20d",      # 20-day volume ratio (available in all scans)
    "pat_bullish_strength",  # bullish pattern strength (v2 only)
    "pat_bearish_strength",  # bearish pattern strength (v2 only)
    "rsi14",                 # RSI 14 (available in all scans)
    "macd_hist",             # MACD histogram (available in all scans)
    "bb_percent_b",          # Bollinger %B (available in all scans)
    "net_volume_flow",       # directional volume flow (v2 only)
    "stochrsi_k",            # StochRSI %K (v2 only)
    "stochrsi_d",            # StochRSI %D (v2 only)
]

# Major coins (top 5 by historical dominance) vs altcoins
MAJORS = {"BTC", "ETH", "BNB", "XRP", "SOL"}


# ── Phase 1: Data loading ───────────────────────────────────────────────────


def load_closed_trades(db: sqlite3.Connection, source: str | None = None) -> pd.DataFrame:
    """Load closed trades with r_multiple and net_r_score.

    Optionally filter by source ('live' or 'backtest'). Default (None) returns
    all sources so a joint analysis is possible; pass --source to isolate one.

    Trades archived as DATA_UNAVAILABLE are always excluded, even if an R value
    is accidentally present, since they carry no real market outcome. Trades
    excluded by a forward-test reset (perf_excluded=1) are also skipped so
    historic pre-reset samples never leak into research.
    """
    query = """
        SELECT id, opened_at, closed_at, symbol, interval, direction,
               entry, stop_loss, take_profit, exit_price,
               r_multiple, net_r_score, fee_paid, source
        FROM trades
        WHERE r_multiple IS NOT NULL
          AND status != 'DATA_UNAVAILABLE'
          AND perf_excluded = 0
    """
    params: tuple = ()
    if source:
        query += " AND source = ?"
        params = (source,)
    query += " ORDER BY opened_at"
    df = pd.read_sql_query(query, db, params=params)
    df["opened_at"] = pd.to_datetime(df["opened_at"], utc=True)
    df["closed_at"] = pd.to_datetime(df["closed_at"], utc=True)
    return df


def load_all_signals(db: sqlite3.Connection) -> pd.DataFrame:
    """Load all signals with their indicator snapshots, joined to scan timestamps."""
    rows = pd.read_sql_query(
        """
        SELECT s.scan_id, sc.ts as scan_ts, s.symbol, s.direction, s.entry,
               s.indicators, s.factors
        FROM signals s
        JOIN scans sc ON s.scan_id = sc.id
        ORDER BY sc.ts
        """,
        db,
    )
    rows["scan_ts"] = pd.to_datetime(rows["scan_ts"], utc=True)

    # Flatten the indicators JSON into columns
    ind_records = []
    for _, row in rows.iterrows():
        ind = json.loads(row["indicators"])
        record = {
            "scan_id": row["scan_id"],
            "scan_ts": row["scan_ts"],
            "symbol": row["symbol"],
            "signal_direction": row["direction"],
            "signal_entry": row["entry"],
        }
        # Extract all known regime fields (missing keys become NaN)
        for field in REGIME_FIELDS:
            record[field] = ind.get(field)
        ind_records.append(record)

    return pd.DataFrame(ind_records)


def load_all_atr_history(db: sqlite3.Connection) -> pd.DataFrame:
    """Load ATR% history per symbol for percentile computation."""
    rows = pd.read_sql_query(
        """
        SELECT sc.ts as scan_ts, s.symbol, s.indicators
        FROM signals s
        JOIN scans sc ON s.scan_id = sc.id
        ORDER BY sc.ts
        """,
        db,
    )
    rows["scan_ts"] = pd.to_datetime(rows["scan_ts"], utc=True)
    records = []
    for _, row in rows.iterrows():
        ind = json.loads(row["indicators"])
        atr_pct = ind.get("atr_pct")
        if atr_pct is not None:
            records.append({
                "scan_ts": row["scan_ts"],
                "symbol": row["symbol"],
                "atr_pct": atr_pct,
            })
    return pd.DataFrame(records)


# ── Phase 2: Regime tagging ─────────────────────────────────────────────────


def match_trade_to_snapshot(
    trade: pd.Series,
    signals: pd.DataFrame,
    atr_history: pd.DataFrame,
) -> dict[str, Any]:
    """Find the closest signal snapshot at or before trade open time.

    For ATR percentile: compute percentile of ATR% over the 60-day window
    ending at trade open time, across ALL symbols (market-wide regime).
    """
    symbol = trade["symbol"]
    opened = trade["opened_at"]

    # Find the closest scan at or before trade open
    eligible = signals[(signals["symbol"] == symbol) & (signals["scan_ts"] <= opened)]
    if eligible.empty:
        # Fallback: take the closest scan after open (first available)
        eligible = signals[(signals["symbol"] == symbol) & (signals["scan_ts"] >= opened)]
    if eligible.empty:
        return {field: None for field in REGIME_FIELDS}

    # Take the latest eligible snapshot
    snap = eligible.sort_values("scan_ts").iloc[-1]
    result = {field: snap.get(field) for field in REGIME_FIELDS}

    # ATR percentile: market-wide over 60-day rolling window
    window_start = opened - pd.Timedelta(days=60)
    window = atr_history[
        (atr_history["scan_ts"] >= window_start) & (atr_history["scan_ts"] <= opened)
    ]
    if not window.empty:
        current_atr = result.get("atr_pct")
        if current_atr is not None:
            result["atr_percentile"] = float(
                (window["atr_pct"] < current_atr).sum() / len(window) * 100
            )
        else:
            result["atr_percentile"] = None
    else:
        result["atr_percentile"] = None

    # Determine which pattern strength to use based on trade direction
    direction = trade["direction"]
    if direction == "LONG":
        result["pat_side_strength"] = result.pop("pat_bullish_strength", None)
    else:
        result["pat_side_strength"] = result.pop("pat_bearish_strength", None)
    # Remove the unused side
    result.pop("pat_bullish_strength", None)
    result.pop("pat_bearish_strength", None)

    return result


def build_regime_table(
    trades: pd.DataFrame,
    signals: pd.DataFrame,
    atr_history: pd.DataFrame,
) -> pd.DataFrame:
    """Tag every closed trade with regime context at entry time."""
    tagged = []
    for _, trade in trades.iterrows():
        tags = match_trade_to_snapshot(trade, signals, atr_history)
        record = trade.to_dict()
        record.update(tags)
        # Derived fields
        record["symbol_category"] = "major" if trade["symbol"] in MAJORS else "alt"
        record["direction_num"] = 1 if trade["direction"] == "LONG" else 0
        tagged.append(record)
    return pd.DataFrame(tagged)


# ── Phase 3: Save trade_regime table ────────────────────────────────────────


def save_trade_regime(db: sqlite3.Connection, regime_df: pd.DataFrame) -> None:
    """Write the regime-tagged trades to a table, merging with any existing
    rows. Idempotent per (id, source): re-running overwrites matching rows
    instead of duplicating, and preserves rows from other source splits.

    Since backtest trades are marked source='backtest' and live trades
    source='live', running regime analysis for each source populates a shared
    `trade_regime` table that supports joint analysis without contamination.
    """
    cols_to_save = [
        "id", "symbol", "direction", "opened_at", "closed_at",
        "entry", "exit_price", "r_multiple", "net_r_score", "fee_paid",
        "source",
        "symbol_category", "direction_num",
        "gradient_trend", "atr_pct", "atr_percentile", "volume_ratio_20d",
        "pat_side_strength", "rsi14", "macd_hist", "bb_percent_b",
        "net_volume_flow", "stochrsi_k", "stochrsi_d",
    ]
    # Only save columns that exist
    cols_to_save = [c for c in cols_to_save if c in regime_df.columns]
    subset = regime_df[cols_to_save].copy()

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_regime (
            id INTEGER, symbol TEXT, direction TEXT, opened_at TEXT,
            closed_at TEXT, entry REAL, exit_price REAL, r_multiple REAL,
            net_r_score REAL, fee_paid REAL, source TEXT,
            symbol_category TEXT, direction_num INTEGER,
            gradient_trend REAL, atr_pct REAL, atr_percentile REAL,
            volume_ratio_20d REAL, pat_side_strength REAL, rsi14 REAL,
            macd_hist REAL, bb_percent_b REAL, net_volume_flow REAL,
            stochrsi_k REAL, stochrsi_d REAL,
            PRIMARY KEY (id, source)
        )
        """
    )
    # Enforce the (id, source) primary key: upsert only the incoming source's
    # rows, preserving rows from other sources (live vs backtest) so a shared
    # table supports joint analysis without contamination.
    temp = "trade_regime_tmp"
    subset.to_sql(temp, db, if_exists="replace", index=False)
    db.execute(
        f"INSERT OR REPLACE INTO trade_regime ({', '.join(cols_to_save)}) "
        f"SELECT {', '.join(cols_to_save)} FROM {temp}"
    )
    db.execute(f"DROP TABLE {temp}")
    db.commit()


# ── Phase 4: Regression & correlation analysis ──────────────────────────────


def run_analysis(regime_df: pd.DataFrame) -> dict[str, Any]:
    """Run correlation + OLS regression on net_r_score.

    Returns a dict with all computed statistics for report generation.
    """
    # Work with a clean subset (drop rows where net_r_score is None)
    df = regime_df.dropna(subset=["net_r_score"]).copy()
    n = len(df)

    report: dict[str, Any] = {"n_trades": n, "trades": df}

    # ── Descriptive stats ──
    report["descriptive"] = {
        "mean_net_r": df["net_r_score"].mean(),
        "std_net_r": df["net_r_score"].mean() if n > 1 else 0,
        "min_net_r": df["net_r_score"].min(),
        "max_net_r": df["net_r_score"].max(),
    }

    # ── Correlation matrix ──
    numeric_cols = [
        "net_r_score", "gradient_trend", "atr_pct", "atr_percentile",
        "volume_ratio_20d", "pat_side_strength", "rsi14", "macd_hist",
        "bb_percent_b", "net_volume_flow", "direction_num",
    ]
    available_cols = [c for c in numeric_cols if c in df.columns and df[c].notna().any()]
    corr_matrix = df[available_cols].corr()
    report["correlation"] = corr_matrix

    # Correlation with net_r_score specifically
    net_r_corr = {}
    for col in available_cols:
        if col != "net_r_score":
            valid = df[["net_r_score", col]].dropna()
            if len(valid) >= 3:
                net_r_corr[col] = round(valid["net_r_score"].corr(valid[col]), 4)
            else:
                net_r_corr[col] = None
    report["net_r_correlations"] = dict(sorted(net_r_corr.items(), key=lambda x: abs(x[1] or 0), reverse=True))

    # ── OLS Regression ──
    # Select predictors that have enough non-null values
    predictor_candidates = [
        "gradient_trend", "atr_percentile", "volume_ratio_20d",
        "pat_side_strength", "rsi14", "macd_hist", "bb_percent_b",
        "net_volume_flow", "direction_num",
    ]
    predictors = [p for p in predictor_candidates if p in df.columns and df[p].notna().sum() >= 3]

    if predictors and n >= 3:
        try:
            import statsmodels.api as sm

            X = df[predictors].copy()
            # Standardize predictors for coefficient comparison
            X_std = (X - X.mean()) / X.std().replace(0, 1)
            X_std = sm.add_constant(X_std)
            y = df["net_r_score"]

            model = sm.OLS(y, X_std, missing="drop").fit()
            report["ols"] = {
                "r_squared": model.rsquared,
                "adj_r_squared": model.rsquared_adj,
                "f_stat": model.fvalue,
                "f_pvalue": model.f_pvalue,
                "nobs": int(model.nobs),
                "coefficients": {
                    name: {
                        "coef": round(float(model.params[name]), 4),
                        "std_err": round(float(model.bse[name]), 4),
                        "t": round(float(model.tvalues[name]), 3),
                        "p": round(float(model.pvalues[name]), 4),
                        "significant_05": bool(model.pvalues[name] < 0.05),
                    }
                    for name in predictors
                },
                "const_coef": round(float(model.params.get("const", 0)), 4),
            }
        except ImportError:
            report["ols"] = {"error": "statsmodels not installed"}
    else:
        report["ols"] = {"error": f"insufficient data (n={n}, predictors={len(predictors)})"}

    # ── Category breakdown ──
    if "symbol_category" in df.columns:
        cat_group = df.groupby("symbol_category")["net_r_score"].agg(["mean", "count", "std"])
        report["category_breakdown"] = cat_group.to_dict("index")

    # ── Direction breakdown ──
    dir_group = df.groupby("direction")["net_r_score"].agg(["mean", "count"])
    report["direction_breakdown"] = dir_group.to_dict("index")

    return report


# ── Phase 5: Report generation ──────────────────────────────────────────────


def generate_report(report: dict[str, Any]) -> str:
    """Generate a markdown report from the analysis results."""
    lines = []
    lines.append("# Regime Analysis Report")
    lines.append("")
    lines.append(f"**Trades analyzed:** {report['n_trades']}")
    lines.append("")

    # Descriptive
    desc = report["descriptive"]
    lines.append("## Descriptive Statistics")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Mean net R | {desc['mean_net_r']:.4f} |")
    lines.append(f"| Min net R | {desc['min_net_r']:.4f} |")
    lines.append(f"| Max net R | {desc['max_net_r']:.4f} |")
    lines.append("")

    # Correlations
    lines.append("## Correlation with Net R-Score")
    lines.append("")
    if report["net_r_correlations"]:
        lines.append("| Factor | Pearson r | Direction |")
        lines.append("|--------|-----------|-----------|")
        for name, r in report["net_r_correlations"].items():
            if r is not None:
                direction = "hurts" if r < 0 else "helps"
                lines.append(f"| {name} | {r:+.4f} | {direction} |")
            else:
                lines.append(f"| {name} | N/A | — |")
        lines.append("")

    # OLS
    ols = report.get("ols", {})
    if "error" not in ols:
        lines.append("## OLS Regression (standardized coefficients)")
        lines.append("")
        lines.append(f"R² = {ols['r_squared']:.4f} | Adj R² = {ols['adj_r_squared']:.4f} | F = {ols['f_stat']:.2f} (p = {ols['f_pvalue']:.4f})")
        lines.append(f"Observations: {ols['nobs']}")
        lines.append("")
        lines.append("| Predictor | Coef | Std Err | t | p | Sig (α=0.05) |")
        lines.append("|-----------|------|---------|---|---|---------------|")
        for name, stats in sorted(ols["coefficients"].items(), key=lambda x: abs(x[1]["coef"]), reverse=True):
            sig = "YES" if stats["significant_05"] else "no"
            lines.append(f"| {name} | {stats['coef']:+.4f} | {stats['std_err']:.4f} | {stats['t']:.3f} | {stats['p']:.4f} | {sig} |")
        lines.append("")
    else:
        lines.append(f"## OLS Regression\n\n*Skipped: {ols['error']}*\n")

    # Category breakdown
    if "category_breakdown" in report:
        lines.append("## Performance by Category")
        lines.append("")
        lines.append("| Category | Mean net R | Count |")
        lines.append("|----------|------------|-------|")
        for cat, stats in report["category_breakdown"].items():
            lines.append(f"| {cat} | {stats['mean']:.4f} | {int(stats['count'])} |")
        lines.append("")

    # Direction breakdown
    if "direction_breakdown" in report:
        lines.append("## Performance by Direction")
        lines.append("")
        lines.append("| Direction | Mean net R | Count |")
        lines.append("|-----------|------------|-------|")
        for d, stats in report["direction_breakdown"].items():
            lines.append(f"| {d} | {stats['mean']:.4f} | {int(stats['count'])} |")
        lines.append("")

    # Trade detail
    df = report["trades"]
    lines.append("## Trade Detail")
    lines.append("")
    lines.append("| # | Symbol | Dir | Entry | Exit | Gross R | Net R | grad | ATR%ile | vol_ratio | pat_str | RSI |")
    lines.append("|---|--------|-----|-------|------|---------|-------|------|---------|-----------|---------|-----|")
    for _, t in df.iterrows():
        grad = f"{t.get('gradient_trend', 0):.3f}" if pd.notna(t.get("gradient_trend")) else "—"
        atr_p = f"{t.get('atr_percentile', 0):.0f}" if pd.notna(t.get("atr_percentile")) else "—"
        vol = f"{t.get('volume_ratio_20d', 0):.2f}" if pd.notna(t.get("volume_ratio_20d")) else "—"
        pat = f"{t.get('pat_side_strength', 0):.3f}" if pd.notna(t.get("pat_side_strength")) else "—"
        rsi = f"{t.get('rsi14', 0):.1f}" if pd.notna(t.get("rsi14")) else "—"
        nr = t.get("net_r_score")
        nr_str = f"{nr:.4f}" if pd.notna(nr) else "—"
        lines.append(
            f"| {t['id']} | {t['symbol']} | {t['direction']} | "
            f"{t['entry']:.2f} | {t['exit_price']:.2f} | "
            f"{t['r_multiple']:.2f} | {nr_str} | "
            f"{grad} | {atr_p} | {vol} | {pat} | {rsi} |"
        )
    lines.append("")

    # Limitations
    lines.append("## Limitations")
    lines.append("")
    lines.append(f"- **Sample size:** n={report['n_trades']} — far below the n≥50 threshold for statistical significance.")
    lines.append("- Many v2-only fields (gradient_trend, pat_side_strength, stochrsi, net_volume_flow) are NULL for trades opened during v1 scans.")
    lines.append("- All closed trades are SL_HIT (r=-1.00), so gross R variance is zero — regression cannot explain variance that does not exist.")
    lines.append("- Net R variance comes entirely from slippage + fee modeling, not from signal quality differences.")
    lines.append("- **Recommendation:** Re-run this analysis after n≥50 closed v2 trades with mixed LONG/SHORT outcomes.")
    lines.append("")

    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────────────


def main(source: str | None = None) -> None:
    print("Loading data...", flush=True)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    trades = load_closed_trades(db, source=source)
    print(f"  Closed trades: {len(trades)} (source={source or 'all'})", flush=True)

    signals = load_all_signals(db)
    print(f"  Signal snapshots: {len(signals)}", flush=True)

    atr_history = load_all_atr_history(db)
    print(f"  ATR history points: {len(atr_history)}", flush=True)

    print("Building regime table...", flush=True)
    regime_df = build_regime_table(trades, signals, atr_history)
    print(f"  Tagged {len(regime_df)} trades", flush=True)

    print("Saving trade_regime table...", flush=True)
    save_trade_regime(db, regime_df)
    print("  Done", flush=True)

    print("Running analysis...", flush=True)
    report = run_analysis(regime_df)

    print("Generating report...", flush=True)
    md = generate_report(report)
    label = source or "all"
    report_path = OUTPUT_DIR / f"regime_report_{label}.md"
    report_path.write_text(md, encoding="utf-8")
    print(f"  Report: {report_path}", flush=True)

    csv_path = OUTPUT_DIR / f"trade_regime_{label}.csv"
    regime_df.to_csv(csv_path, index=False)
    print(f"  CSV: {csv_path}", flush=True)

    db.close()
    print("\nDone.", flush=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("--source", "-s"):
        main(source=sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        main()
