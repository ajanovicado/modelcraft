# Governed Gross Profit Driver Bridge

## Purpose and scope

The prepared driver artifact explains the change in **Gross Profit dollars**
between 2024 Q4 and 2025 Q4. It is a fixed arithmetic bridge, not a causal
model. The current release supports the all-region view and the governed region
views stored in `driver_contributions.parquet`; conversational report placement
is limited to the frozen West bridge.

The source transactions are cent-rounded before aggregation. Every displayed
bridge value is prepared offline from those stored transactions and shared by
DuckDB and the prepared Power BI model.

## Grain and notation

For each cell `k`, the aggregation grain is:

`Region × Product × Category × Segment × Channel`

Let period `0` be 2024 Q4 and period `1` be 2025 Q4. For each period and cell:

- `U` = units.
- `G` = gross list value, or `Σ(units × list price)`.
- `P = G / U` = weighted list price per unit.
- `D = 1 − Net Sales / G` = weighted discount rate.
- `C = COGS / U` = weighted unit cost.
- `M = P × (1 − D) − C` = Gross Profit per unit.

Cells present in only one period are retained through an outer join and their
missing aggregate inputs are filled with zero.

## Contribution formulas

The base aggregate Gross Profit per unit is:

```text
Base margin per unit = Gross Profit₀ / ΣU₀
```

The five reported contributions are calculated in this fixed order:

```text
Volume = (ΣU₁ − ΣU₀) × Base margin per unit

Detailed volume = Σ[(U₁,k − U₀,k) × (P₀,k × (1 − D₀,k) − C₀,k)]

Price = Σ[U₁,k × (P₁,k − P₀,k) × (1 − D₀,k)]

Discount = Σ[−U₁,k × P₁,k × (D₁,k − D₀,k)]

Cost = Σ[−U₁,k × (C₁,k − C₀,k)]
```

The preliminary composition effect is `Detailed volume − Volume`. The stored
Mix value then receives the exact reconciliation residual:

```text
Gross Profit change = Gross Profit₁ − Gross Profit₀

Mix = Gross Profit change − Price − Volume − Discount − Cost
```

This construction guarantees, subject only to the stored floating-point
representation, that:

```text
Price + Volume + Mix + Discount + Cost = Gross Profit change
```

## Residual treatment

“Mix” is therefore a governed **composition plus reconciliation residual**. It
contains the detailed product/segment/channel composition effect and absorbs
rounding and interaction terms left by the fixed sequential decomposition. The
prepared artifact does not store a separate residual column, so the residual
must not be described as independently measured.

The bridge supports statements such as “discount and cost contributed to the
decline.” It does not prove that a business action caused the outcome. Use
“contributed to” or “was associated with,” never unqualified causal language.

## Provenance and validation

- Generator: `ai_powerbi_analyst.data.fixture.build_driver_bridge`.
- Source periods: 2024-10-01 through 2024-12-31 and 2025-10-01 through
  2025-12-31.
- Artifact: `fixtures/data/driver_contributions.parquet`.
- Artifact and source hashes: `fixtures/data/manifest.json`.
- Runtime table: `driver_contributions` in the read-only analytical mirror.

Preparation tests should verify one row per driver and governed region,
five-driver reconciliation to the stored Gross Profit change, and exact
agreement between the evidence answer and the artifact. Power BI/Desktop
rendering and cross-engine reconciliation remain separate release gates.
