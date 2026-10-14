"""Quoted category values never become filter commands, field references or targets."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_edit_intents import CHART, PAGE, SLICER_STORE, ParseKind, contexts, only_clause, parse
from test_report_edit_filters import load, run

from ai_powerbi_analyst.studio.report_edit_context import ModelInfo


@pytest.mark.parametrize(
    "literal",
    [
        "last 6 months",
        "this year",
        "Top 3",
        "above 1000",
        "between 2018 and 2020",
        "ChannelName",
        "Total MTD Sales",
        "clear filters",
        "tomorrow",
        "bar",
        "legend",
    ],
)
@pytest.mark.parametrize(
    "prefix",
    [
        "filter this page to ChannelName",
        "filter ChannelName to",
        'filter "ChannelName" to',
    ],
)
@pytest.mark.parametrize("quote", ['"', "'"])
def test_literal_is_data_and_canonical_field_stays_explicit(
    literal: str,
    prefix: str,
    quote: str,
) -> None:
    clause = only_clause(parse(f"{prefix} {quote}{literal}{quote} on this page"))
    assert clause.kind is ParseKind.APPLY, clause.question or clause.summary
    assert clause.ops == (
        {
            "op": "set_page_filter",
            "page": PAGE,
            "filter": {
                "kind": "categorical",
                "field": {"table": "DimChannel", "name": "ChannelName"},
                "values": [literal],
                "exclude": False,
            },
        },
    )


@pytest.mark.parametrize("literal", ["Total MTD Sales", "ChannelName", "last 6 months"])
def test_literal_caption_is_not_a_visual_target(literal: str) -> None:
    clause = only_clause(parse(f'keep only ChannelName "{literal}"'))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops[0]["op"] == "set_page_filter"
    assert clause.ops[0]["page"] == PAGE
    assert clause.ops[0]["filter"]["values"] == [literal]


def test_multiple_quoted_values_survive_field_name_overlap() -> None:
    clause = only_clause(parse('filter this page to ChannelName "Store" and "ChannelName"'))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops[0]["filter"]["values"] == ["Store", "ChannelName"]


def test_unquoted_field_before_values_preserves_report_scope() -> None:
    clause = only_clause(parse("filter ChannelName to Store and Online throughout the report"))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops[0]["op"] == "set_report_filter"
    assert clause.ops[0]["filter"]["field"] == {
        "table": "DimChannel",
        "name": "ChannelName",
    }
    assert clause.ops[0]["filter"]["values"] == ["Store", "Online"]


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("literal", ["last 6 months", "Top 3", "Total MTD Sales"])
def test_quoted_visual_scope_and_categorical_literal_remain_distinct(
    quote: str,
    literal: str,
) -> None:
    clause = only_clause(
        parse(f'filter ChannelName to {quote}{literal}{quote} on the "Total MTD Sales" chart')
    )
    assert clause.kind is ParseKind.APPLY
    assert clause.ops == (
        {
            "op": "set_visual_filter",
            "visual": CHART,
            "filter": {
                "kind": "categorical",
                "field": {"table": "DimChannel", "name": "ChannelName"},
                "values": [literal],
                "exclude": False,
            },
        },
    )


@pytest.mark.parametrize("literal", ["last 6 months", "clear filters", "ChannelName"])
def test_exclusion_values_keep_command_and_field_words_as_data(literal: str) -> None:
    clause = only_clause(parse(f'exclude "{literal}" for ChannelName on this page'))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops[0]["op"] == "set_page_filter"
    assert clause.ops[0]["filter"] == {
        "kind": "categorical",
        "field": {"table": "DimChannel", "name": "ChannelName"},
        "values": [literal],
        "exclude": True,
    }


@pytest.mark.parametrize("suffix", ["on this page", "on the current page", ""])
def test_category_matching_another_page_name_does_not_change_scope(suffix: str) -> None:
    context = contexts.context_for("agents-sync-qa")
    context = replace(context, pages=[context.pages[0], replace(context.pages[1], name="Forecast")])
    clause = only_clause(parse(f'filter ChannelName to "Forecast" {suffix}', context))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops[0]["op"] == "set_page_filter"
    assert clause.ops[0]["page"] == PAGE
    assert clause.ops[0]["filter"]["values"] == ["Forecast"]


@pytest.mark.parametrize("scope", ['on page "Forecast"', 'on the "Forecast" page'])
def test_quoted_page_target_does_not_become_a_category(scope: str) -> None:
    context = contexts.context_for("agents-sync-qa")
    context = replace(context, pages=[context.pages[0], replace(context.pages[1], name="Forecast")])
    destination = next(page.id for page in context.pages if page.name == "Forecast")
    clause = only_clause(parse(f'filter ChannelName to "Online" {scope}', context))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops[0]["op"] == "set_page_filter"
    assert clause.ops[0]["page"] == destination
    assert clause.ops[0]["filter"]["values"] == ["Online"]


@pytest.mark.parametrize("scope", ['on "Total MTD Sales"', 'in the "Total MTD Sales" chart'])
def test_quoted_visual_target_does_not_become_a_category(scope: str) -> None:
    clause = only_clause(parse(f'filter ChannelName to "Online" {scope}'))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops[0]["op"] == "set_visual_filter"
    assert clause.ops[0]["visual"] == CHART
    assert clause.ops[0]["filter"]["values"] == ["Online"]


def test_quoted_filter_commands_are_written_as_literal_data() -> None:
    clause = only_clause(
        parse('filter ChannelName to "last 6 months" and "clear filters" throughout the report')
    )
    assert clause.kind is ParseKind.APPLY
    document = load()
    model = ModelInfo(columns={"DimChannel": {"ChannelName": "object"}})
    run(document, clause.ops[0], model=model)
    filters = document.report["filterConfig"]["filters"]
    channel = next(
        item for item in filters if item["field"].get("Column", {}).get("Property") == "ChannelName"
    )
    condition = channel["filter"]["Where"][0]["Condition"]["In"]
    assert condition["Values"] == [
        [{"Literal": {"Value": "'last 6 months'"}}],
        [{"Literal": {"Value": "'clear filters'"}}],
    ]


@pytest.mark.parametrize("scope", ['on the "StoreName" slicer', 'on "StoreName"'])
def test_quoted_target_field_does_not_replace_explicit_filter_column(scope: str) -> None:
    clause = only_clause(parse(f'filter ChannelName to "Online" {scope}'))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops[0] == {
        "op": "set_visual_filter",
        "visual": SLICER_STORE,
        "filter": {
            "kind": "categorical",
            "field": {"table": "DimChannel", "name": "ChannelName"},
            "values": ["Online"],
            "exclude": False,
        },
    }
