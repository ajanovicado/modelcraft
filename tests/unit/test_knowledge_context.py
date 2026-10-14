"""Chat context compaction: bounded, relevant, never an empty table list."""

from __future__ import annotations

import json

import pytest

from ai_powerbi_analyst.knowledge.context import compact_workspace_context

PLAIN = [
    "Total Sales = SUM(Orders[Sales])",
    "Total Profit = SUM(Orders[Profit])",
    "Total Quantity = SUM(Orders[Quantity])",
    "Average Discount = AVERAGE(Orders[Discount])",
    "Distinct Order ID = DISTINCTCOUNT(Orders[Order ID])",
]
LAB = [
    "Consumer West Profit Margin = DIVIDE(CALCULATE(SUM('Orders'[Profit]), "
    "'Orders'[Segment] = \"Consumer\", 'Orders'[Region] = \"West\"), "
    "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Segment] = \"Consumer\"))",
    "Technology Share of Sales = DIVIDE(CALCULATE(SUM('Orders'[Sales]), "
    "'Orders'[Category] = \"Technology\"), SUM('Orders'[Sales]))",
    "Return Rate = DIVIDE(CALCULATE(DISTINCTCOUNT('Orders'[Order ID]), "
    "'Returns'[Returned] = \"Yes\"), DISTINCTCOUNT('Orders'[Order ID]))",
]


def _managed(definitions: list[str]) -> dict[str, object]:
    """The compact managed chat payload's shape (service._chat_context at <= 2,000)."""
    return {
        "model": (
            "Facts: Orders. Dimensions: Returns, People. Measures: [Total Sales], "
            "[Total Profit], [Return Rate]. Verified joins: Orders[Region] *-1 People[Region]; "
            "Orders[Order ID] *-1 Returns[Order ID]"
        ),
        "tables": [
            {
                "table": "Orders",
                "rows": 240,
                "fields": "Order ID, Order Date (date), Segment, Region, Category, Sales "
                "(number), Quantity (number), Discount (number), Profit (number)",
            },
            {"table": "Returns", "rows": 18, "fields": "Returned (true/false), Order ID"},
            {"table": "People", "rows": 4, "fields": "Person, Region"},
        ],
        "definitions": definitions,
    }


def _size(payload: dict[str, object]) -> int:
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


@pytest.mark.parametrize("question", ["what is the return rate formula?", "Explain Return Rate"])
def test_the_definition_a_question_names_outlasts_the_others(question: str) -> None:
    """Review: at 1,400 characters the Return Rate formula went first (draft order, from
    the end) and five plain SUM lines the model summary already names were kept."""
    payload = _managed([*PLAIN, *LAB])
    budget = _size(payload) - 60
    kept = json.loads(compact_workspace_context(payload, question, max_characters=budget))
    definitions = kept["definitions"]
    assert any(item.startswith("Return Rate = DIVIDE(") for item in definitions)
    assert [table["table"] for table in kept["tables"]] == ["Orders", "Returns", "People"]
    # Plain aggregations go before lab formulas, the question's words decide among those.
    assert not any(item in definitions for item in PLAIN)
    assert not any(item.startswith("Technology Share") for item in definitions)


def test_unrelated_questions_drop_the_latest_definitions_first() -> None:
    """As before the relevance order: with nothing asked, the end of the list goes first."""
    payload = _managed(list(LAB))
    budget = _size(payload) - 10
    kept = json.loads(compact_workspace_context(payload, "hello", max_characters=budget))
    assert 1 <= len(kept["definitions"]) < len(LAB)
    assert kept["definitions"] == LAB[: len(kept["definitions"])]
    assert _size(kept) <= budget


def test_a_context_that_fits_is_returned_unchanged() -> None:
    payload = _managed([*PLAIN, *LAB])
    encoded = compact_workspace_context(payload, "anything", max_characters=5000)
    assert json.loads(encoded) == payload
