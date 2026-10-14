# Approved Forecast Methodology

## Purpose and boundary

The application looks up a frozen six-month forecast prepared before the
client session. It does not fit or select a model while the demo is running.
Net Sales and Gross Profit are modeled independently at monthly grain from the
48 complete synthetic months ending 2025-12-31.

Forecast values and their 80% and 95% intervals are statistical-model outputs.
They are not scenario results, causal estimates, or guarantees.

## Candidate set

Each metric evaluates the same four candidates:

- StatsForecast `AutoETS`, season length 12.
- StatsForecast `AutoARIMA`, season length 12.
- Seasonal drift baseline.
- Seasonal naive baseline.

The current generated manifest records the resolved StatsForecast and runtime
versions and whether all four candidates were present in every fold. A missing
candidate cannot be silently represented as a complete comparison.

Seasonal naive reuses the corresponding month from the previous year. Seasonal
drift adds the median year-over-year monthly difference to the corresponding
month from the previous year.

## Rolling-origin folds

Each fold forecasts three months, and the origin advances by three months:

| Fold | Training window | Evaluation window |
|---|---|---|
| 1 | 2022-01 through 2025-03 | 2025-04 through 2025-06 |
| 2 | 2022-01 through 2025-06 | 2025-07 through 2025-09 |
| 3 | 2022-01 through 2025-09 | 2025-10 through 2025-12 |

For actual values `y` and forecasts `ŷ`, each fold uses:

```text
WAPE = Σ|y − ŷ| / Σ|y|
```

A candidate beats seasonal naive in a fold only when its WAPE is strictly
lower than seasonal naive for that metric and fold.

## Approval and selection

A non-naive candidate is eligible only when all of the following hold:

- it has results for all three folds;
- it beats seasonal naive in at least two of the three folds; and
- its mean three-fold WAPE is at most 12% for Net Sales or 15% for Gross
  Profit.

The eligible model with the lowest mean WAPE wins. Differences within `1e-9`
are treated as numerical ties, resolved in this order: AutoETS, AutoARIMA,
seasonal drift, then seasonal naive. Seasonal naive is the benchmark and is
not eligible for selection in the current implementation.

When all four candidates were evaluated in every fold, the status is
`approved`. If the statistical candidates were unavailable but a deterministic
fallback still passed the quantitative gates, the preparation code can record
`fallback_approved`; that label is not equivalent to a complete candidate
comparison and must remain visible in provenance.

The current fixture manifest records AutoARIMA for both metrics, three wins
against seasonal naive for each, and mean WAPE values of approximately 1.55%
for Net Sales and 0.90% for Gross Profit. Those values describe the current
synthetic artifact only. The manifest and its hashes, not this prose, are the
authority after regeneration.

## Final fit and horizon

The selected model for each metric is refit on all 48 months and produces six
monthly points for 2026-01 through 2026-06. The first three points are summed
for the next-quarter cards and form the approved baseline for the price
scenario.

For AutoETS and AutoARIMA, preparation asks StatsForecast for 80% and 95%
levels. The stored implementation derives a non-negative scale from the
model's upper 80% bound:

```text
scale = (upper 80% − point) / 1.281551565545
```

It then stores symmetric normal-quantile intervals:

```text
80%: point ± 1.281551565545 × scale
95%: point ± 1.95996398454 × scale
```

Lower bounds are clipped at zero. For seasonal naive, the scale is based on
the standard deviation of year-over-year residuals, with a 2.5% floor. For
seasonal drift, that scale expands by `sqrt(1 + horizon_month / 6)`.

These are frozen preparation intervals. The current gate evaluates point WAPE;
it does not separately certify empirical interval coverage or calibration.

## Provenance and release evidence

- Backtest: `fixtures/data/forecast_backtest.parquet`.
- Selection: `fixtures/data/forecast_selection.parquet`.
- Frozen outlook: `fixtures/data/forecast.parquet`.
- Versions, decision, completeness, and hashes: `fixtures/data/manifest.json`.
- Generator functions: `build_forecast_backtest`, `select_forecast_models`,
  and `build_forecast`.

The generated local artifacts can establish deterministic preparation. They do
not establish target-laptop latency, memory behavior, strict-offline behavior,
Power BI reconciliation, or Desktop rendering; those require separate release
evidence from the frozen target revision.
