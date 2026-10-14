"""Scope phrases stay outside data values in actual parsed filter operations."""

from __future__ import annotations

import pytest
from test_edit_intents import PAGE, ParseKind, only_clause, parse
from test_report_edit_filters import load, run

from ai_powerbi_analyst.studio.report_edit_context import ModelInfo


@pytest.mark.parametrize(
    "suffix",
    [
        "throughout the report",
        "throughout the whole report",
        "throughout report",
        "across the report",
        "across the whole report",
        "on every page",
        "throughout this report",
        "throughout my report",
        "throughout this dashboard",
        "across that report",
        "across our report",
    ],
)
@pytest.mark.parametrize("exclude", [False, True])
def test_scope_phrase_cannot_become_a_category(suffix: str, exclude: bool) -> None:
    verb = "exclude" if exclude else "keep only"
    clause = only_clause(parse(f"Please {verb} Store and Online channels {suffix}."))
    assert clause.kind is ParseKind.APPLY
    assert len(clause.ops) == 1
    operation = clause.ops[0]
    assert operation["op"] == "set_report_filter"
    assert operation["filter"] == {
        "kind": "categorical",
        "field": {"table": "DimChannel", "name": "ChannelName"},
        "values": ["Store", "Online"],
        "exclude": exclude,
    }


@pytest.mark.parametrize(
    "literal",
    [
        "throughout the report",
        "across the report",
        "the report",
        "this page",
        "current page",
        "Online throughout",
        "Across",
        "Throughout",
    ],
)
def test_quoted_scope_words_remain_data_without_changing_scope(literal: str) -> None:
    clause = only_clause(parse(f'filter this page to ChannelName "{literal}"'))
    assert clause.kind is ParseKind.APPLY
    assert clause.ops[0]["op"] == "set_page_filter"
    assert clause.ops[0]["page"] == PAGE
    assert clause.ops[0]["filter"]["values"] == [literal]


def test_scope_repair_writes_both_exact_report_filter_literals() -> None:
    clause = only_clause(parse("keep only Store and Online channels throughout the report"))
    document = load()
    model = ModelInfo(columns={"DimChannel": {"ChannelName": "object"}})
    run(document, clause.ops[0], model=model)
    filters = document.report["filterConfig"]["filters"]
    channel = next(
        item for item in filters if item["field"].get("Column", {}).get("Property") == "ChannelName"
    )
    condition = channel["filter"]["Where"][0]["Condition"]["In"]
    assert condition["Values"] == [
        [{"Literal": {"Value": "'Store'"}}],
        [{"Literal": {"Value": "'Online'"}}],
    ]
