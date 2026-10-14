"""The live Channel sync failure: source visuals and destination pages have distinct scopes."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import edit_planner as planner
from ai_powerbi_analyst.studio.edit_context import EditContext

EVALS = Path(__file__).resolve().parents[2] / "evals" / "imported_edit"
spec = importlib.util.spec_from_file_location("sync_scope_contexts", EVALS / "contexts.py")
assert spec is not None and spec.loader is not None
contexts = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = contexts
spec.loader.exec_module(contexts)

MESSAGE = "Sync the Channel slicer to Sync QA"


@pytest.fixture
def context() -> EditContext:
    return contexts.context_for("agents-sync-qa")


def channel_number(prompt: planner.Prompt) -> int:
    return next(
        n for n, visual in enumerate(prompt.lists.visuals, 1) if visual.id.startswith("b85aa81a")
    )


def properties(prompt: planner.Prompt) -> dict[str, Any]:
    number = next(n for n, card in enumerate(prompt.cards) if card.op == "sync_slicer")
    return dict(
        prompt.schema["properties"]["steps"]["items"]["anyOf"][number]["properties"]["slots"][
            "properties"
        ]
    )


class Stub:
    def __init__(self, slots: Callable[[planner.Prompt], dict[str, Any]]) -> None:
        self.slots = slots
        self.planner: planner.EditPlanner | None = None

    def generate(
        self,
        _system: str,
        _user: str,
        response_model: type[Any],
        **_options: Any,
    ) -> Any:
        assert self.planner is not None and self.planner.prompt is not None
        prompt = self.planner.prompt
        number = next(n for n, card in enumerate(prompt.cards, 1) if card.op == "sync_slicer")
        return response_model.model_validate(
            {
                "kind": "plan",
                "steps": [
                    {"tool": number, "slots": self.slots(prompt)},
                ],
            }
        )


def run(
    context: EditContext,
    slots: Callable[[planner.Prompt], dict[str, Any]],
) -> tuple[planner.PlanOutcome, planner.Prompt]:
    stub = Stub(slots)
    made = planner.EditPlanner(context, stub)
    stub.planner = made
    outcome = made.plan([MESSAGE])
    assert made.prompt is not None
    return outcome, made.prompt


def test_live_fixture_is_active_agents_with_an_unselected_blank_destination(
    context: EditContext,
) -> None:
    assert context.active_page == context.pages[0].id
    assert context.selection is None
    assert [(page.id, page.name) for page in context.pages[1:]] == [("page4", "Sync QA")]
    assert context.visuals_on("page4") == []
    channel = next(v for v in context.visuals if v.id.startswith("b85aa81a"))
    assert channel.page_id == context.active_page
    assert channel.visual_type == "slicer"
    assert "ChannelName" in channel.fields
    assert contexts.multipage_context().pages[-1].name == "Forecast"


def test_live_sync_case_keeps_the_verbatim_request_and_only_requested_gold_slots() -> None:
    case = next(
        json.loads(line)
        for line in (EVALS / "planner_cases.jsonl")
        .read_text(
            encoding="utf-8",
        )
        .splitlines()
        if '"id": "lv12"' in line
    )
    assert case["message"] == MESSAGE
    assert case["context"] == "agents-sync-qa"
    assert case["expect"]["steps"] == [
        {
            "op": "sync_slicer",
            "slots": {
                "visual": "b85aa81a",
                "pages": "Sync QA",
            },
        }
    ]


def test_named_destination_does_not_empty_the_active_page_source_visual_grammar(
    context: EditContext,
) -> None:
    sync = next(c for c in planner.available_cards() if c.op == "sync_slicer")
    prompt = planner.build_prompt(MESSAGE, context, [sync])
    assert prompt.lists.page is not None
    assert prompt.lists.page.id == context.active_page
    assert prompt.lists.visuals
    assert all(v.page_id == context.active_page for v in prompt.lists.visuals)
    assert properties(prompt)["visual"]["enum"] == [channel_number(prompt)]
    assert "group" not in properties(prompt)


@pytest.mark.parametrize("unasked", [{}, {"group": "Invented by the model"}])
def test_omitted_named_destination_is_recovered_without_an_invented_group_or_page(
    context: EditContext,
    unasked: dict[str, Any],
) -> None:
    outcome, _prompt = run(context, lambda p: {"visual": channel_number(p), **unasked})
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops == (
        {
            "op": "sync_slicer",
            "visual": next(v.id for v in context.visuals if v.id.startswith("b85aa81a")),
            "pages": ["Sync QA"],
        },
    )


def test_unasked_destination_cannot_mutate_another_page(context: EditContext) -> None:
    outcome, _prompt = run(
        context,
        lambda p: {
            "visual": channel_number(p),
            "pages": "Forecast",
            "hidden_on": "Forecast",
        },
    )
    # An invalid stub answer may be refused; if completed, only the named destination is safe.
    assert outcome.kind in {"failed", "clarify", "plan"}
    if outcome.kind == "plan":
        assert outcome.ops[0]["pages"] == ["Sync QA"]
        assert "hidden_on" not in outcome.ops[0]
    else:
        assert not outcome.ops


def test_missing_model_source_is_recovered_from_the_active_page_channel_alias(
    context: EditContext,
) -> None:
    outcome, _prompt = run(context, lambda _p: {"pages": "Sync QA"})
    assert outcome.kind == "plan", outcome.reason
    assert outcome.ops[0]["visual"] == next(
        v.id for v in context.visuals if v.id.startswith("b85aa81a")
    )
    assert outcome.ops[0]["pages"] == ["Sync QA"]
    assert "group" not in outcome.ops[0]
