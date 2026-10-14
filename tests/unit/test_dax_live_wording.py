"""Regression requests from real guarded Qwen runs, checked against independent values."""

from __future__ import annotations

import jsonschema
import pandas as pd
import pytest

from ai_powerbi_analyst.studio.dax_engine import Model
from ai_powerbi_analyst.studio.dax_patterns import (
    MetricSelection,
    PatternError,
    PatternSelection,
    build_pattern,
    catalog_from_model,
    pattern_prompt,
    resolve_field,
    select_business_pattern,
)
from ai_powerbi_analyst.studio.report_engine import LocalReportEngine


@pytest.fixture
def sales_model() -> Model:
    return Model.from_frames(
        {
            "FactSales": pd.DataFrame(
                {
                    "DateKey": pd.to_datetime(
                        ["2025-01-01", "2025-01-01", "2026-01-01", "2026-01-01", "2026-02-01"]
                    ),
                    "CustomerKey": [1, 1, 2, 3, 2],
                    "Sales": [100.0, 100.0, 200.0, 0.0, 50.0],
                    "Profit": [10.0, 20.0, -20.0, 0.0, 10.0],
                }
            ),
            "DimDate": pd.DataFrame({"DateKey": pd.date_range("2025-01-01", "2026-12-31")}),
            "DimCustomer": pd.DataFrame(
                {"CustomerKey": [1, 2, 3], "Region": ["West", "East", "North"]}
            ),
        },
        [
            ("FactSales", "DateKey", "DimDate", "DateKey"),
            ("FactSales", "CustomerKey", "DimCustomer", "CustomerKey"),
        ],
        {"Total Sales": "SUM('FactSales'[Sales])"},
    )


def value(model: Model, expression: str):
    result = LocalReportEngine(model).query(None, f'EVALUATE ROW("Value", {expression})')
    return result["rows"][0]["[Value]"]


def test_unquoted_qualified_field_resolves_duplicate_names_without_suffix_match(sales_model):
    catalog = catalog_from_model(sales_model)
    assert resolve_field("FactSales[CustomerKey]", catalog).table == "FactSales"
    assert resolve_field("FactSales [CustomerKey]", catalog).table == "FactSales"
    assert resolve_field("'DimCustomer'[CustomerKey]", catalog).table == "DimCustomer"
    with pytest.raises(PatternError, match="several"):
        resolve_field("CustomerKey", catalog)
    with pytest.raises(PatternError, match="qualified reference is unknown"):
        resolve_field("WrongFactSales[CustomerKey]", catalog)
    with pytest.raises(PatternError, match="qualified reference is unknown"):
        resolve_field("WrongFactSales[Sales]", catalog)
    for unknown in ("WrongTable[Total Sales]", "WrongFactSales[ImaginaryRevenue] plus Sales"):
        with pytest.raises(PatternError, match="qualified reference is unknown"):
            resolve_field(unknown, catalog)


@pytest.mark.parametrize("condition", ["Sales is zero", "FactSales[Sales] is 0", "denominator = 0"])
def test_ratio_blank_denominator_instruction_keeps_nonzero_and_zero_contexts(
    sales_model, condition
):
    built = select_business_pattern(
        f"Calculate Profit divided by Sales. Return BLANK when {condition}.",
        catalog_from_model(sales_model),
    )
    assert built is not None
    assert value(sales_model, built.expression) == pytest.approx(20 / 450)
    assert (
        value(sales_model, f"CALCULATE({built.expression}, DimCustomer[CustomerKey] = 1)") == 0.15
    )
    assert (
        value(sales_model, f"CALCULATE({built.expression}, DimCustomer[CustomerKey] = 3)") is None
    )


@pytest.mark.parametrize(
    "tail",
    [
        "Return BLANK when Profit is zero.",
        "Return BLANK when Region is West.",
        "Return zero when Sales is zero.",
        "Return BLANK when Sales is zero for West.",
    ],
)
def test_ratio_does_not_drop_other_conditions(sales_model, tail):
    with pytest.raises(PatternError, match="filter"):
        select_business_pattern("Profit divided by Sales. " + tail, catalog_from_model(sales_model))


def test_basic_relative_clause_does_not_ask_for_an_unrelated_pattern(sales_model):
    class MustNotSelect:
        def generate(self, *args, **kwargs):
            pytest.fail("A simple sum was sent to a business-pattern selector")

    assert (
        select_business_pattern(
            "Create a DAX measure that sums FactSales[Sales].",
            catalog_from_model(sales_model),
            MustNotSelect(),
        )
        is None
    )


@pytest.mark.parametrize(
    "goal",
    [
        "Create a measure summing FactSales[ImaginaryRevenue].",
        "Create a measure summing Wrong[Sales].",
        "Create a measure summing Wrong[ImaginaryRevenue] plus Sales.",
    ],
)
def test_unknown_qualified_request_cannot_reach_model_or_freeform_substitution(sales_model, goal):
    class MustNotGenerate:
        def generate(self, *args, **kwargs):
            pytest.fail("An unknown explicit request field was sent to the model")

    with pytest.raises(PatternError, match="qualified reference is unknown"):
        select_business_pattern(goal, catalog_from_model(sales_model), MustNotGenerate())


def test_explicit_unquoted_calendar_and_metric_clauses_preserve_yoy_units(sales_model):
    built = select_business_pattern(
        "Create year-over-year absolute change in Total Sales using DimDate[DateKey].",
        catalog_from_model(sales_model),
    )
    assert built is not None
    assert (
        value(sales_model, f"CALCULATE({built.expression}, DimDate[DateKey] = DATE(2026,1,1))") == 0
    )
    assert (
        value(sales_model, f"CALCULATE({built.expression}, DimDate[DateKey] = DATE(2025,1,1))")
        is None
    )


def test_fixed_grain_qualified_key_requires_then_honors_explicit_filter_decision(sales_model):
    goal = "Create fixed Total Sales by FactSales[CustomerKey]"
    catalog = catalog_from_model(sales_model)
    with pytest.raises(PatternError, match="ignore other filters while keeping"):
        select_business_pattern(goal + ".", catalog)
    built = select_business_pattern(goal + ", ignore other filters", catalog)
    assert built is not None
    filters = "FactSales[CustomerKey] = 2, FactSales[Sales] = 50"
    assert value(sales_model, f"CALCULATE([Total Sales], {filters})") == 50
    assert value(sales_model, f"CALCULATE({built.expression}, {filters})") == 250


def test_share_and_rank_qualified_scope_have_correct_customer_results(sales_model):
    catalog = catalog_from_model(sales_model)
    share = select_business_pattern(
        "Create share of Total Sales by FactSales[CustomerKey] across all CustomerKey values.",
        catalog,
    )
    rank = select_business_pattern(
        "Rank Total Sales by FactSales[CustomerKey] across all CustomerKey values, "
        "descending dense rank.",
        catalog,
    )
    assert share is not None and rank is not None
    for key, expected_share, expected_rank in ((1, 200 / 450, 2), (2, 250 / 450, 1), (3, 0, 3)):
        assert value(
            sales_model, f"CALCULATE({share.expression}, FactSales[CustomerKey] = {key})"
        ) == pytest.approx(expected_share)
        assert (
            value(sales_model, f"CALCULATE({rank.expression}, FactSales[CustomerKey] = {key})")
            == expected_rank
        )


@pytest.mark.parametrize(
    "goal",
    [
        "share of Total Sales plus Profit by FactSales[CustomerKey] across all CustomerKey values",
        "share of Total Sales by FactSales[CustomerKey] across all Region values",
        "share of Total Sales by FactSales[CustomerKey] across all CustomerKey values for West",
        "Rank -Total Sales by FactSales[CustomerKey] across all CustomerKey values, "
        "descending dense rank.",
        "share of Total Sales + Total Sales by FactSales[CustomerKey] "
        "across all CustomerKey values",
        "share of Total Sales by -FactSales[CustomerKey] across all CustomerKey values",
        "share of Total Sales by FactSales[CustomerKey] across all -CustomerKey values",
    ],
)
def test_group_parser_rejects_unrepresented_arithmetic_scope_and_filters(sales_model, goal):
    with pytest.raises(PatternError):
        select_business_pattern(goal, catalog_from_model(sales_model))


@pytest.mark.parametrize(
    "operand", ["Sales plus 10", "Sales + 10", "-Sales", "Sales + Sales", "Total Sales / 2"]
)
def test_ratio_blank_directive_cannot_hide_operand_arithmetic(sales_model, operand):
    with pytest.raises(PatternError, match="operand"):
        select_business_pattern(
            f"Profit divided by {operand}. Return BLANK when denominator is zero.",
            catalog_from_model(sales_model),
        )


def test_pattern_schema_prevents_wrong_slot_and_field_kinds_before_compilation(sales_model):
    catalog = catalog_from_model(sales_model)
    schema = pattern_prompt(
        "compare total sales by customer", catalog
    ).response_model.model_json_schema()
    validator = jsonschema.Draft202012Validator(schema)
    validator.check_schema(schema)

    def find(name, table=""):
        return next(
            item.index
            for item in catalog.fields
            if item.name == name and (not table or item.table == table)
        )

    metric = MetricSelection(field=find("Total Sales"))
    selection = PatternSelection(
        pattern="running_total", base=metric, date=find("DateKey", "DimDate"), scope="all"
    )
    validator.validate(selection.model_dump())
    for wrong in (
        selection.model_copy(update={"numerator": metric}),
        selection.model_copy(update={"date": find("CustomerKey", "FactSales")}),
        selection.model_copy(
            update={"base": MetricSelection(field=metric.field, aggregation="sum")}
        ),
        selection.model_copy(
            update={"base": MetricSelection(field=find("Sales"), aggregation=None)}
        ),
    ):
        assert list(validator.iter_errors(wrong.model_dump()))
    # Pydantic's runtime compiler remains a second independent fail-closed boundary.
    with pytest.raises(PatternError, match="couldn't combine"):
        build_pattern(selection.model_copy(update={"numerator": metric}), catalog)
