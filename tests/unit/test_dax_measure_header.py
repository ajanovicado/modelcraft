from __future__ import annotations

import pytest

from ai_powerbi_analyst.studio.dax import measure_body, parse_dax


@pytest.mark.parametrize(
    "expression",
    [
        "Profit Margin % = \nDIVIDE(SUM('Sales'[Profit]), SUM('Sales'[Sales]))",
        "Profit Margin % := DIVIDE(SUM('Sales'[Profit]), SUM('Sales'[Sales]))",
        "DIVIDE(SUM('Sales'[Profit]), SUM('Sales'[Sales]))",
    ],
)
def test_chat_style_measure_definitions_reach_the_lab_as_expressions(expression: str) -> None:
    # Studio chat answers in Power BI's "Name = expression" form; the lab and the
    # saved TMDL expression both need only the body.
    assert measure_body(expression) == "DIVIDE(SUM('Sales'[Profit]), SUM('Sales'[Sales]))"
    assert type(parse_dax(expression)).__name__ == "_Ratio"


@pytest.mark.parametrize(
    "expression",
    [
        "CALCULATE(SUM('Sales'[Sales]), 'Sales'[Region] = \"East\")",
        "EVALUATE VALUES('Sales'[Region])",
        "SUM('Sales'[Sales])",
        "VAR CurrentValue = SUM('Sales'[Sales]) VAR PreviousValue = 100 "
        "RETURN CurrentValue - PreviousValue",
        "var CurrentValue = SUM('Sales'[Sales]) return CurrentValue",
        'DEFINE VAR CurrentValue = 1 EVALUATE ROW("Value", CurrentValue)',
    ],
)
def test_expressions_without_a_header_are_unchanged(expression: str) -> None:
    assert measure_body(expression) == expression


def test_header_before_a_variable_body_strips_only_the_measure_name() -> None:
    formula = "VAR CurrentValue = SUM('Sales'[Sales]) RETURN CurrentValue"
    assert measure_body("Sales Total = " + formula) == formula
    assert measure_body("Sales Total := " + formula) == formula


def test_missing_first_variable_cannot_alias_the_valid_expression_cache_key() -> None:
    formula = (
        "VAR CurrentValue = [Total Sales] VAR PreviousValue = 100 "
        "RETURN CurrentValue - PreviousValue"
    )
    malformed = "[Total Sales] VAR PreviousValue = 100 RETURN CurrentValue - PreviousValue"
    assert measure_body(formula) != measure_body(malformed)


def test_ratio_measures_named_as_percentages_default_to_percent_format() -> None:
    from ai_powerbi_analyst.studio.dax import percent_format_for

    ratio = "DIVIDE(SUM('Sales'[Profit]), SUM('Sales'[Sales]))"
    assert percent_format_for("Profit Margin %", ratio) == "0.0%"
    assert percent_format_for("Return rate", ratio) == "0.0%"
    # A ratio that is not a percentage keeps the numeric default.
    assert (
        percent_format_for("Average price", "DIVIDE(SUM('Sales'[Sales]), SUM('Sales'[Units]))")
        is None
    )
    assert percent_format_for("Margin total", "SUM('Sales'[Profit])") is None
