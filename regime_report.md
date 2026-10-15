# Regime Analysis Report

**Trades analyzed:** 2

## Descriptive Statistics

| Metric | Value |
|--------|-------|
| Mean net R | -1.0825 |
| Min net R | -1.1365 |
| Max net R | -1.0285 |

## Correlation with Net R-Score

| Factor | Pearson r | Direction |
|--------|-----------|-----------|
| atr_pct | N/A | — |
| atr_percentile | N/A | — |
| volume_ratio_20d | N/A | — |
| rsi14 | N/A | — |
| macd_hist | N/A | — |
| bb_percent_b | N/A | — |
| direction_num | N/A | — |

## OLS Regression

*Skipped: insufficient data (n=2, predictors=0)*

## Performance by Category

| Category | Mean net R | Count |
|----------|------------|-------|
| alt | -1.0825 | 2 |

## Performance by Direction

| Direction | Mean net R | Count |
|-----------|------------|-------|
| LONG | -1.0825 | 2 |

## Trade Detail

| # | Symbol | Dir | Entry | Exit | Gross R | Net R | grad | ATR%ile | vol_ratio | pat_str | RSI |
|---|--------|-----|-------|------|---------|-------|------|---------|-----------|---------|-----|
| 4 | ZEC | LONG | 860.76 | 789.44 | -1.00 | -1.0285 | — | 93 | 2.84 | — | 88.4 |
| 6 | TRX | LONG | 0.34 | 0.34 | -1.00 | -1.1365 | — | 2 | 1.13 | — | 64.7 |

## Limitations

- **Sample size:** n=2 — far below the n≥50 threshold for statistical significance.
- Many v2-only fields (gradient_trend, pat_side_strength, stochrsi, net_volume_flow) are NULL for trades opened during v1 scans.
- All closed trades are SL_HIT (r=-1.00), so gross R variance is zero — regression cannot explain variance that does not exist.
- Net R variance comes entirely from slippage + fee modeling, not from signal quality differences.
- **Recommendation:** Re-run this analysis after n≥50 closed v2 trades with mixed LONG/SHORT outcomes.
