from __future__ import annotations

import pytest

from ai_powerbi_analyst.contracts import ColumnDataType, ColumnProfile, DslMeasureSpec, TableProfile
from ai_powerbi_analyst.powerbi.semantic_model import SemanticProjectError, compile_dsl_measure


def _column(column_id: str, name: str, data_type: ColumnDataType) -> ColumnProfile:
    return ColumnProfile(
        id=column_id,
        name=name,
        data_type=data_type,
        row_count=3,
        null_count=0,
        distinct_count=3,
    )


@pytest.fixture
def sales_table() -> TableProfile:
    return TableProfile(
        id="sales",
        name="Sales",
        source_id="sales_source",
        row_count=3,
        columns=[
            _column("amount", "Amount", ColumnDataType.NUMBER),
            _column("units", "Units", ColumnDataType.INTEGER),
            _column("order_date", "Order Date", ColumnDataType.DATE),
            _column("region", "Region", ColumnDataType.STRING),
        ],
    )


def test_compiles_weighted_average(sales_table: TableProfile) -> None:
    expression = compile_dsl_measure(
        DslMeasureSpec(
            id="weighted_price",
            name="Weighted Price",
            operation="weighted_average",
            table_id="sales",
            column_id="amount",
            weight_column_id="units",
        ),
        sales_table,
    )

    assert expression == ("DIVIDE(SUMX(Sales, Sales[Amount] * Sales[Units]), SUM(Sales[Units]))")


def test_column_count_counts_nonblank_text_values(sales_table: TableProfile) -> None:
    expression = compile_dsl_measure(
        DslMeasureSpec(
            id="region_count",
            name="Region Count",
            operation="count",
            table_id="sales",
            column_id="region",
        ),
        sales_table,
    )

    assert expression == "COUNTA(Sales[Region])"


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (
            "running_total",
            "CALCULATE([Net Sales], FILTER(ALLSELECTED(Sales[Order Date]), "
            "Sales[Order Date] <= MAX(Sales[Order Date])))",
        ),
        ("period_to_date", "TOTALQTD([Net Sales], Sales[Order Date])"),
        (
            "period_change",
            "VAR PreviousValue = CALCULATE([Net Sales], DATEADD(Sales[Order Date], -1, QUARTER)) "
            "RETURN DIVIDE([Net Sales] - PreviousValue, PreviousValue)",
        ),
    ],
)
def test_compiles_time_intelligence(
    sales_table: TableProfile,
    operation: str,
    expected: str,
) -> None:
    expression = compile_dsl_measure(
        DslMeasureSpec(
            id=f"{operation}_sales",
            name=operation.replace("_", " ").title(),
            operation=operation,
            table_id="sales",
            date_column_id="order_date",
            base_measure_id="net_sales",
            period="quarter",
        ),
        sales_table,
        {"net_sales": "Net Sales"},
    )

    assert expression == expected


def test_compiles_variance(sales_table: TableProfile) -> None:
    expression = compile_dsl_measure(
        DslMeasureSpec(
            id="sales_variance",
            name="Sales Variance",
            operation="variance",
            table_id="sales",
            numerator_measure_id="actual_sales",
            denominator_measure_id="budget_sales",
        ),
        sales_table,
        {"actual_sales": "Actual Sales", "budget_sales": "Budget Sales"},
    )

    assert expression == "[Actual Sales] - [Budget Sales]"


def test_rejects_time_intelligence_on_non_date_column(sales_table: TableProfile) -> None:
    spec = DslMeasureSpec(
        id="invalid_running_total",
        name="Invalid running total",
        operation="running_total",
        table_id="sales",
        date_column_id="region",
        base_measure_id="net_sales",
    )

    with pytest.raises(SemanticProjectError, match="requires a date column"):
        compile_dsl_measure(spec, sales_table, {"net_sales": "Net Sales"})
