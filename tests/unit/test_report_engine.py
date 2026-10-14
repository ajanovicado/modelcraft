"""An imported report in Studio's DAX engine: what it computes, and what it leaves to
Power BI Desktop instead of risking a different number."""

from __future__ import annotations

import pandas as pd

from ai_powerbi_analyst.studio.dax_engine import Model
from ai_powerbi_analyst.studio.report_engine import (
    _column_names,
    measure_fact_table,
    unrelated_filter,
)


def _model(with_date_join: bool = True) -> Model:
    joins = [("FactSales", "StoreKey", "DimStore", "StoreKey")]
    if with_date_join:
        joins.append(("FactSales", "DateKey", "Date", "Date"))
    return Model.from_frames(
        {
            "FactSales": pd.DataFrame(
                {
                    "DateKey": pd.to_datetime(["2009-01-01", "2009-02-01"]),
                    "StoreKey": [1, 2],
                    "SalesAmount": [10.0, 20.0],
                    "TotalCost": [4.0, 5.0],
                }
            ),
            "DimStore": pd.DataFrame({"StoreKey": [1, 2], "StoreName": ["A", "B"]}),
            "Date": pd.DataFrame({"Date": pd.date_range("2009-01-01", "2009-12-31")}),
            "Top-Bottom-N": pd.DataFrame({"N": [1, 2, 3]}),
        },
        joins,
        {
            "Total Sales": "SUM(FactSales[SalesAmount])",
            "Profit": "SUMX(FactSales, FactSales[SalesAmount] - FactSales[TotalCost])",
            "Profit Margin": "DIVIDE([Profit], [Total Sales])",
            "MTD Sales": "CALCULATE([Total Sales], DATESMTD('Date'[Date]))",
            "Top Sales": "IF(SELECTEDVALUE('Top-Bottom-N'[N], 1) = 1, [Total Sales])",
        },
    )


def test_plain_measures_name_the_table_they_aggregate() -> None:
    model = _model()
    assert measure_fact_table(model, "Profit") == "FactSales"
    assert measure_fact_table(model, "Profit Margin") == "FactSales"
    # Time intelligence and slicer measures change meaning under the planner's filters.
    assert measure_fact_table(model, "MTD Sales") is None
    assert measure_fact_table(model, "Top Sales") is None


def test_a_filter_that_cannot_reach_the_summed_table_is_a_formula_fault() -> None:
    """Power BI would silently return every year's sales for this filter."""
    formula = "EVALUATE ROW(\"v\", CALCULATE([Total Sales], 'Date'[Date] >= DATE(2009,2,1)))"
    assert unrelated_filter(_model(), formula) is None
    message = unrelated_filter(_model(with_date_join=False), formula)
    assert message is not None and message.startswith(
        "No active many-to-one relationship connects 'Date' to 'FactSales'"
    )
    # A parameter table the measure itself reads is a real filter, related or not.
    slicer = "EVALUATE ROW(\"v\", CALCULATE([Top Sales], 'Top-Bottom-N'[N] = 2))"
    assert unrelated_filter(_model(), slicer) is None
    grouped = 'EVALUATE SUMMARIZECOLUMNS(DimStore[StoreName], "S", [Total Sales])'
    assert unrelated_filter(_model(), grouped) is None


def test_result_columns_are_named_as_the_lab_shows_them() -> None:
    assert _column_names(["DimStore[StoreName]", "[Sales]"]) == ["StoreName", "Sales"]
    assert _column_names(["A[Name]", "B[Name]", "[Value]"]) == ["A[Name]", "B[Name]", "Value"]
