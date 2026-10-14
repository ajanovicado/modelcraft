"""Numeric field names do not become date periods or ranking instructions."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_edit_intents import PAGE, TABLE, ParseKind, contexts, only_clause, parse

from ai_powerbi_analyst.studio.edit_context import EditContext, MeasureEntry


def collision_context() -> EditContext:
    context = contexts.agents_context()
    measures = ["Previous Year Revenue", "Top 3 Revenue", "Last Month", "Top 3"]
    columns = ["vs Next Week", "Previous Month", "Month", "Last Year Variance"]
    return replace(
        context,
        measures=[*context.measures, *(MeasureEntry(name, "Metrics", "0") for name in measures)],
        tables={**context.tables, "Metrics": columns},
        column_types={**context.column_types, **{f"Metrics.{name}": "float64" for name in columns}},
    )


def advanced(
    field: str, conditions: list[dict[str, object]], table: str = "Metrics"
) -> dict[str, object]:
    return {
        "kind": "advanced",
        "field": {"table": table, "name": field},
        "logic": "and",
        "conditions": conditions,
    }


@pytest.mark.parametrize(
    "message",
    [
        "Filter the table to vs Previous Month above 0",
        "Only show vs Previous Month above 0 on the table",
        'Filter the table to "vs Previous Month" above 0',
        "Filter the table to 'vs Previous Month' greater than 0",
        "Filter the table to above 0 for vs Previous Month",
    ],
)
def test_real_measure_name_preserves_numeric_filter_and_exact_visual(message: str) -> None:
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops == (
        {
            "op": "set_visual_filter",
            "visual": TABLE,
            "filter": advanced(
                "vs Previous Month", [{"operator": "greater_than", "value": 0}], "# Measures"
            ),
        },
    )


@pytest.mark.parametrize(
    "message,field,operator,value",
    [
        (
            "Keep only Previous Year Revenue at least 20",
            "Previous Year Revenue",
            "greater_or_equal",
            20,
        ),
        (
            "Filter this page to Last Year Variance below -10",
            "Last Year Variance",
            "less_than",
            -10,
        ),
        ("Filter this page to vs Next Week greater than 7", "vs Next Week", "greater_than", 7),
        ("Filter this page to Previous Month above 0", "Previous Month", "greater_than", 0),
        (
            'Filter this page to "Previous Month" no more than 5',
            "Previous Month",
            "less_or_equal",
            5,
        ),
        (
            "Filter Metrics[Previous Month] to above 0 on this page",
            "Previous Month",
            "greater_than",
            0,
        ),
        ("Filter the measure Last Month to above 0 on this page", "Last Month", "greater_than", 0),
        ("Filter this page to Top 3 Revenue above 1000", "Top 3 Revenue", "greater_than", 1000),
        ("Filter Top 3 to above 0 on this page", "Top 3", "greater_than", 0),
        ('Filter this page to "Top 3" above 0', "Top 3", "greater_than", 0),
    ],
)
def test_full_numeric_field_names_keep_the_requested_comparison(
    message: str,
    field: str,
    operator: str,
    value: int,
) -> None:
    clause = only_clause(parse(message, collision_context()))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops == (
        {
            "op": "set_page_filter",
            "page": PAGE,
            "filter": advanced(field, [{"operator": operator, "value": value}]),
        },
    )


def test_named_period_measure_range_preserves_both_numeric_bounds() -> None:
    clause = only_clause(parse("Filter vs Previous Month to between -5 and 0 on this page"))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops == (
        {
            "op": "set_page_filter",
            "page": PAGE,
            "filter": advanced(
                "vs Previous Month",
                [
                    {"operator": "greater_or_equal", "value": -5},
                    {"operator": "less_or_equal", "value": 0},
                ],
                "# Measures",
            ),
        },
    )


@pytest.mark.parametrize(
    "message,unit,direction,count",
    [
        ("Only show last month", "calendar_months", "last", 1),
        ("Filter this page to previous month", "calendar_months", "last", 1),
        ("Filter Date to the last 6 months on this page", "months", "last", 6),
        ("Limit this page to the past 3 months", "months", "last", 3),
        ("Only show next 2 weeks", "weeks", "next", 2),
        ("Filter this page to last year", "calendar_years", "last", 1),
    ],
)
def test_periods_and_partial_date_word_hits_remain_real_relative_filters(
    message: str,
    unit: str,
    direction: str,
    count: int,
) -> None:
    clause = only_clause(parse(message, collision_context()))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops == (
        {
            "op": "set_page_filter",
            "page": PAGE,
            "filter": {
                "kind": "relative_date",
                "field": {"table": "Date", "name": "Date"},
                "unit": unit,
                "direction": direction,
                "count": count,
            },
        },
    )


def test_actual_ranking_cue_outside_a_numeric_field_name_is_preserved() -> None:
    clause = only_clause(
        parse("Filter the table to the top 3 MonthName by Top 3 Revenue", collision_context())
    )
    assert clause.kind is ParseKind.APPLY
    assert clause.ops == (
        {
            "op": "set_visual_filter",
            "visual": TABLE,
            "filter": {
                "kind": "top_n",
                "field": {"table": "Date", "name": "MonthName"},
                "n": 3,
                "by": {"table": "Metrics", "name": "Top 3 Revenue"},
                "direction": "top",
            },
        },
    )


@pytest.mark.parametrize(
    "message",
    [
        "Filter the table to last month with Total Sales above 0",
        "Filter this page to the last 6 months and vs Previous Month over 5",
        'Filter this page to this year with "vs Previous Month" between 0 and 10',
    ],
)
def test_combined_date_and_number_filters_never_apply_only_one_condition(message: str) -> None:
    clause = only_clause(parse(message))
    assert clause.kind is ParseKind.CLARIFY
    assert not clause.ops and not clause.choices
    assert "both" in clause.question and "separate" in clause.question


def test_separate_filter_clauses_preserve_both_complete_operations() -> None:
    result = parse(
        "Filter the table to last month and filter the table to vs Previous Month above 0"
    )
    assert [clause.kind for clause in result.clauses] == [ParseKind.APPLY, ParseKind.APPLY]
    assert result.ops == [
        {
            "op": "set_visual_filter",
            "visual": TABLE,
            "filter": {
                "kind": "relative_date",
                "field": {"table": "Date", "name": "Date"},
                "unit": "calendar_months",
                "direction": "last",
                "count": 1,
            },
        },
        {
            "op": "set_visual_filter",
            "visual": TABLE,
            "filter": advanced(
                "vs Previous Month", [{"operator": "greater_than", "value": 0}], "# Measures"
            ),
        },
    ]


def test_quoted_category_with_a_numeric_field_name_is_still_one_literal_value() -> None:
    clause = only_clause(parse('Filter ChannelName to "vs Previous Month" on this page'))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops == (
        {
            "op": "set_page_filter",
            "page": PAGE,
            "filter": {
                "kind": "categorical",
                "field": {"table": "DimChannel", "name": "ChannelName"},
                "values": ["vs Previous Month"],
                "exclude": False,
            },
        },
    )
