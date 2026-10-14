from __future__ import annotations

from pathlib import Path

import pytest

from ai_powerbi_analyst.data.catalog import CatalogSafetyError, load_semantic_catalog

CATALOG_PATH = Path(__file__).parents[2] / "fixtures" / "data" / "semantic_catalog.yaml"


def test_catalog_loads_expected_governed_vocabulary() -> None:
    catalog = load_semantic_catalog(CATALOG_PATH)

    assert catalog.version == "1.0.0"
    assert catalog.resolve_metric("revenue").id == "net_sales"
    assert catalog.resolve_metric("margin").id == "gross_margin_pct"
    assert catalog.resolve_dimension("market").id == "region"
    assert {metric.id for metric in catalog.metrics} >= {
        "net_sales",
        "units",
        "gross_profit",
        "gross_margin_pct",
        "average_selling_price",
        "discount_rate",
        "unit_cost",
        "sales_growth",
    }


def test_catalog_rejects_executable_or_multi_statement_sql(tmp_path: Path) -> None:
    malicious = tmp_path / "catalog.yaml"
    malicious.write_text(
        """
version: 1.0.0
dimensions: []
metrics:
  - id: danger
    name: Danger
    aliases: []
    description: Unsafe expression.
    format: '$,.0f'
    powerbi_ref: "'Measures'[Danger]"
    sql_expression: "SUM(net_sales); DROP TABLE sales"
    allowed_dimensions: []
    dependencies: []
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(CatalogSafetyError):
        load_semantic_catalog(malicious)


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(net_sales) + read_csv('/tmp/secrets.csv')",
        "SUM(net_sales) + (SELECT count(*) FROM sales)",
        "SUM(net_sales) + SUM(unit_cost)",
        "SUM(net_sales) FILTER (WHERE region = 'West')",
    ],
)
def test_catalog_rejects_noncanonical_expression_for_known_metric(
    tmp_path: Path, expression: str
) -> None:
    malicious = tmp_path / "catalog.yaml"
    baseline = CATALOG_PATH.read_text(encoding="utf-8")
    replaced = baseline.replace(
        'sql_expression: "SUM(net_sales)"',
        f'sql_expression: "{expression}"',
        1,
    )
    malicious.write_text(
        replaced,
        encoding="utf-8",
    )

    with pytest.raises(CatalogSafetyError, match="canonical governed expression"):
        load_semantic_catalog(malicious)


def test_catalog_rejects_remapped_dimension_column(tmp_path: Path) -> None:
    malicious = tmp_path / "catalog.yaml"
    baseline = CATALOG_PATH.read_text(encoding="utf-8")
    malicious.write_text(
        baseline.replace("column: region", "column: transaction_id", 1),
        encoding="utf-8",
    )

    with pytest.raises(CatalogSafetyError, match="governed source column"):
        load_semantic_catalog(malicious)
