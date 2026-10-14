# Governed Price Scenario Methodology

## Purpose and labels

The scenario is a fixed sensitivity calculation for a price change from −3%
through +5% in one-percentage-point increments. The main demo value is +3%, and
the governed price-response coefficient is labelled elasticity `ε = −0.8`.

Every result must be labelled **mechanical scenario; not a prediction**. It is
not the approved forecast, a causal estimate, or a claim that customers will
respond this way.

## Forecast-anchored baseline

Let the baseline period be the first three months of the frozen approved
forecast. For the current fixture this is 2026-01-01 through 2026-03-31.

```text
NS₀ = sum of approved next-quarter Net Sales point forecasts
GP₀ = sum of approved next-quarter Gross Profit point forecasts
```

The remaining baseline inputs are reconciled from 2025 Q4 actuals:

```text
Latest net ASP = Latest-quarter Net Sales / Latest-quarter Units

U₀ = NS₀ / Latest net ASP

D₀ = 1 − Latest-quarter Net Sales / Latest-quarter gross list value

P₀ = Latest net ASP / (1 − D₀)

COGS₀ = NS₀ − GP₀

C₀ = COGS₀ / U₀
```

Here `U₀` is derived baseline volume, `D₀` is the held-constant discount rate,
`P₀` is the corresponding list price, and `C₀` is the unit cost solved so the
baseline reconciles exactly to the independently approved Net Sales and Gross
Profit forecasts.

## Scenario equations

For an allowed integer price change `p` and fraction `r = p / 100`:

```text
Uₚ = U₀ × (1 + ε × r)

Pₚ = P₀ × (1 + r)

Net Salesₚ = Uₚ × Pₚ × (1 − D₀)

COGSₚ = Uₚ × C₀

Gross Profitₚ = Net Salesₚ − COGSₚ

Gross Margin %ₚ = Gross Profitₚ / Net Salesₚ
```

At +3%, the governed formula applies a `0.976` volume multiplier and a `1.03`
price multiplier. With discount held constant, the Net Sales multiplier is
`0.976 × 1.03 = 1.00528` before display rounding.

## Assumptions and limitations

- The `−0.8` coefficient is a synthetic, approved demonstration assumption.
- The implementation uses a linear volume response, `1 + εr`; it is not a
  log-log demand model or a market-estimated elasticity curve.
- Discount rate and unit cost remain fixed across the grid.
- Product, category, segment, channel, region, and timing mix do not change.
- Competitor responses, capacity, churn, acquisition, taxes, and downstream
  effects are outside the calculation.
- Forecast uncertainty is not propagated into the scenario grid. The forecast
  intervals and the scenario results must remain visually and verbally
  separate.
- Output precision in the artifact exceeds the client display precision;
  spoken values must follow the governed display rounding.

These assumptions make the calculation useful for a bounded sensitivity
conversation, not for an unqualified recommendation or prediction.

## Provenance and validation

- Generator: `ai_powerbi_analyst.data.fixture.build_price_scenarios`.
- Artifact: `fixtures/data/scenario_grid.parquet`.
- Baseline forecast: the first three periods in `fixtures/data/forecast.parquet`.
- Version, baseline description, and hashes: `fixtures/data/manifest.json`.
- Runtime query: exact row lookup by an allowlisted integer price-change value.

Preparation tests should compare every grid row with the equations above and
verify the allowed range and step. The current local artifact does not prove
Power BI/Desktop rendering or the target-laptop release gate.
