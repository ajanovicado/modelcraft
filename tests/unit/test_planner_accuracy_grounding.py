"""Request meaning survives imperfect model proposals and the report writer."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from test_planner_live_regressions import TABLE, contexts, field, plan_with, planner, visual
from test_report_edit_conditional import body, run_clean, twin


@pytest.mark.parametrize(
    "message",
    [
        "leave the Store channel and the Online channel out of every page",
        "exclude the Online channel and the Store channel across the whole report",
        "filter every page to exclude the Store and Online channels",
    ],
)
@pytest.mark.parametrize("model_value", ["Store channel", "Online", "Store, Online"])
def test_all_named_categories_survive_a_partial_model_answer(
    message: str,
    model_value: str,
) -> None:
    result, _, _ = plan_with(
        message,
        "set_report_filter",
        lambda p: {
            "field": field(p, "ChannelName"),
            "values": model_value,
            "exclude": True,
        },
    )
    assert result.kind == "plan", result.reason
    filtered = result.ops[0]["filter"]
    assert set(filtered["values"]) == {"Store", "Online"}
    assert filtered["field"] == {"table": "DimChannel", "name": "ChannelName"}
    assert filtered["exclude"] is True


def dictionaries(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


@pytest.mark.parametrize(
    ("comparison", "kind"),
    [
        ("above", 1),
        ("greater than", 1),
        ("over", 1),
        ("at least", 2),
        ("from 0 up", 2),
    ],
)
@pytest.mark.parametrize("model_color", ["green", "#00FF00", "green (#00FF00)"])
def test_only_requested_color_and_comparison_reach_the_report(
    comparison: str,
    kind: int,
    model_color: str,
) -> None:
    condition = comparison if comparison == "from 0 up" else f"{comparison} 0"
    result, _, _ = plan_with(
        f"set the font color of vs PM % values to green when they are {condition}",
        "set_conditional_format",
        lambda p: {
            "visual": visual(p, TABLE),
            "field": "vs PM %",
            "style": "rules",
            "target": "font_color",
            "threshold": 0,
            "above_color": model_color,
            "below_color": "red",
        },
    )
    assert result.kind == "plan", result.reason
    rules = result.ops[0]["rules"]
    assert len(rules) == 1 and rules[0]["min"] == 0
    assert rules[0]["color"] == "#107C10"
    document = twin()
    run_clean(
        document,
        {
            "op": "clear_conditional_format",
            "visual": TABLE,
            "target": "font_color",
            "field": "vs PM %",
        },
    )
    run_clean(document, result.ops[0])
    zero_comparisons = [
        d
        for d in dictionaries(body(document, TABLE))
        if d.get("Right") == {"Literal": {"Value": "0D"}} and "ComparisonKind" in d
    ]
    assert any(d["ComparisonKind"] == kind for d in zero_comparisons)
    # The added rule has the requested green and no invented red fallback.
    assert "#D13438" not in str(result.ops[0])


@pytest.mark.parametrize(
    "message",
    [
        "why is the Rank column red?",
        "How many agents sold above 100K?",
        "Can you explain the green values?",
        "looks good",
        "Looks great!",
        "Thank you.",
        "This looks great.",
        "Looks good, thanks.",
        "That looks perfect, thank you.",
    ],
)
def test_read_only_messages_do_not_call_the_edit_model(message: str) -> None:
    class NoModel:
        def generate(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("A read-only message reached the editing model")

    made = planner.EditPlanner(contexts.context_for("agents"), NoModel())
    outcome = made.plan([message])
    assert not outcome.ops and outcome.calls == 0
    assert outcome.kind == "failed" and outcome.reason == "not an edit request"


def test_css_label_disagreement_does_not_change_the_requested_hue() -> None:
    result, _, _ = plan_with(
        "make the vs PM % values green when they are above 0",
        "set_conditional_format",
        lambda p: {
            "visual": visual(p, TABLE),
            "field": "vs PM %",
            "style": "rules",
            "threshold": 0,
            "above_color": "green (#FF0000)",
        },
    )
    assert not result.ops
