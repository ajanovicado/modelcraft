"""Direct planning refuses unsupported requests before a model can substitute a tool."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import edit_planner as planner
from ai_powerbi_analyst.studio.edit_capabilities import STYLING_KEYS, STYLING_REASON
from ai_powerbi_analyst.studio.edit_context import EditContext
from ai_powerbi_analyst.studio.edit_types import ParseKind

EVALS = Path(__file__).resolve().parents[2] / "evals" / "imported_edit"
spec = importlib.util.spec_from_file_location("planner_boundary_contexts", EVALS / "contexts.py")
assert spec is not None and spec.loader is not None
contexts = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = contexts
spec.loader.exec_module(contexts)

TABLE = "e82e02f93055d6b0b544"
CHART = "b404a461ede7604a2774"
AVG = "da32cdbb0090e885649b"
SLICER = "5b0dbccd61c6c4adb415"


class MustNotCall:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("An unsupported request must never reach model selection")


@pytest.mark.parametrize(
    "message",
    [
        'make the title of Avg MTD Sales by Agents say "Average sales per agent"',
        'retitle the Total MTD Sales chart as "Revenue"',
        'the title of the table should read "Agents"',
        "hide the title of Avg MTD Sales by Agents",
        "turn off the legend on the Total MTD Sales chart",
        "move the legend below the Total MTD Sales chart",
        "show the data labels on the Total MTD Sales chart",
        "make the data labels on the chart bigger",
        "set the table font to 14 pt",
        "use Arial for the table",
        "make the title bold",
        "paint the bars blue",
        "set the page background to white",
        "add a navy border to the StoreName slicer",
        "give the StoreName slicer rounded corners",
        "copy the chart formatting to the table",
        "make the Rank column wider",
        "right align the Rank column",
        "turn off visual header icons",
        "end the y axis of Total MTD Sales at 50000",
        "remove the axis gridlines",
        "use a brand palette",
        "translate every chart title to Spanish",
        "give the bars more breathing room",
        "make it look nicer",
    ],
)
def test_deleted_styling_never_reaches_selection_or_substitute_tools(message: str) -> None:
    model = MustNotCall()
    engine = planner.EditPlanner(contexts.context_for("agents", {"selection": CHART}), model)
    # The boundary must run even if retrieval would offer an adversarial substitute.
    engine.shortlist = lambda _: pytest.fail("Unsupported requests must precede retrieval")
    outcome = engine.plan([message])
    assert outcome.kind == "unsupported", outcome.reason
    assert outcome.clause is not None and outcome.clause.kind is ParseKind.UNSUPPORTED
    assert outcome.clause.capability in STYLING_KEYS
    assert outcome.reason == outcome.clause.reason == STYLING_REASON
    assert not outcome.ops and not outcome.clause.ops and not outcome.clause.choices
    assert model.calls == outcome.calls == outcome.prompt_bytes == 0
    assert engine.prompt is None


@pytest.mark.parametrize(
    "message",
    [
        "publish this report to the Power BI service",
        "sign in to the SQL server",
        "install a custom visual from AppSource",
    ],
)
def test_other_recognized_unsupported_capabilities_are_also_terminal(message: str) -> None:
    model = MustNotCall()
    outcome = planner.EditPlanner(contexts.context_for("agents", {}), model).plan([message])
    assert outcome.kind == "unsupported" and outcome.clause is not None
    assert outcome.reason == outcome.clause.reason and outcome.reason
    assert not outcome.ops and outcome.calls == model.calls == 0


@pytest.mark.parametrize(
    "pieces",
    [
        ["paint the chart blue", "make the table 500 pixels wide"],
        ["make the table 500 pixels wide", "hide the chart title"],
        ["paint the chart blue and make the table 500 pixels wide"],
        ["make the title bold and add data bars to Rank in the table"],
    ],
)
def test_direct_mixed_requests_do_not_claim_a_partial_plan(pieces: list[str]) -> None:
    model = MustNotCall()
    outcome = planner.EditPlanner(contexts.context_for("agents", {"selection": CHART}), model).plan(
        pieces
    )
    assert outcome.kind == "unsupported" and outcome.reason == STYLING_REASON
    assert not outcome.ops and outcome.calls == model.calls == 0
    if len(pieces) == 2:
        assert outcome.left_out == tuple(p for p in pieces if "500" in p)
    else:
        assert any("500" in p or "data bars" in p for p in outcome.left_out)


def test_unsupported_piece_after_step_limit_still_precedes_selection() -> None:
    model = MustNotCall()
    kept = ["move the table left"] * planner.MAX_STEPS
    outcome = planner.EditPlanner(contexts.context_for("agents", {}), model).plan(
        [*kept, "paint the chart blue"]
    )
    assert outcome.kind == "unsupported" and outcome.reason == STYLING_REASON
    assert outcome.left_out == tuple(kept)
    assert not outcome.ops and outcome.calls == model.calls == 0


class Choose:
    def __init__(self, slots: Callable[[planner.Prompt], dict[str, Any]]) -> None:
        self.slots = slots
        self.engine: planner.EditPlanner | None = None
        self.calls = 0

    def generate(self, _system: str, _user: str, response_model: type[Any], **kwargs: Any) -> Any:
        self.calls += 1
        assert self.engine is not None and self.engine.prompt is not None
        return response_model.model_validate(
            {
                "kind": "plan",
                "steps": [{"tool": 1, "slots": self.slots(self.engine.prompt)}],
            }
        )


def _visual(prompt: planner.Prompt, visual_id: str) -> int:
    return next(n for n, v in enumerate(prompt.lists.visuals, 1) if v.id == visual_id)


def _field(prompt: planner.Prompt, name: str) -> int:
    return next(n for n, f in enumerate(prompt.lists.fields, 1) if f[2] == name)


def _run(
    message: str,
    op: str,
    slots: Callable[[planner.Prompt], dict[str, Any]],
    context: EditContext | None = None,
) -> planner.PlanOutcome:
    model = Choose(slots)
    engine = planner.EditPlanner(context or contexts.context_for("agents", {}), model)
    model.engine = engine
    offered = next(c for c in engine.cards if c.op == op)
    engine.shortlist = lambda _: [offered]
    outcome = engine.plan([message])
    assert outcome.kind == "plan", outcome.reason
    assert model.calls == outcome.calls == 1
    assert len(outcome.ops) == 1 and outcome.ops[0]["op"] == op
    return outcome


def test_retained_conditional_formatting_uses_requested_field_and_values() -> None:
    outcome = _run(
        "Color the Rank column in the table with a gradient from red to green",
        "set_conditional_format",
        lambda p: {
            "visual": _visual(p, TABLE),
            "style": "gradient",
            "field": "Rank",
            "min_color": "#FF0000",
            "max_color": "#00FF00",
        },
    )
    assert outcome.ops[0]["visual"] == TABLE and outcome.ops[0]["field"] == "Rank"
    assert outcome.ops[0]["min_color"] == "#D13438"
    assert outcome.ops[0]["max_color"] == "#107C10"


def test_retained_label_number_format_does_not_change_label_visibility() -> None:
    outcome = _run(
        "show the Total MTD Sales data labels in millions",
        "set_number_format",
        lambda p: {
            "visual": _visual(p, CHART),
            "target": "data_labels",
            "display_units": "millions",
        },
    )
    assert outcome.ops[0] == {
        "op": "set_number_format",
        "visual": CHART,
        "target": "data_labels",
        "display_units": "millions",
    }


def test_retained_field_header_is_not_a_visual_title() -> None:
    outcome = _run(
        "call the Employee Name column Agent in the table",
        "set_field_display_name",
        lambda p: {"visual": _visual(p, TABLE), "field": "Employee Name", "name": "Agent"},
    )
    assert outcome.ops[0]["visual"] == TABLE and outcome.ops[0]["name"] == "Agent"


def test_retained_model_column_rename_preserves_table_identity() -> None:
    outcome = _run(
        "rename the StoreName column to Store",
        "rename_column",
        lambda p: {
            "table": p.lists.tables.index("DimStore") + 1,
            "column": _field(p, "StoreName"),
            "new_name": "Store",
        },
    )
    assert outcome.ops[0] == {
        "op": "rename_column",
        "table": "DimStore",
        "name": "StoreName",
        "new_name": "Store",
    }


@pytest.mark.parametrize(
    ("message", "op", "slots", "expected"),
    [
        (
            "make the Total MTD Sales chart 400 pixels wide",
            "set_position",
            {"width": 400},
            {"width": 400},
        ),
        ("hide the Total MTD Sales chart", "set_visual_hidden", {"hidden": True}, {"hidden": True}),
        ("send the Total MTD Sales chart to the back", "arrange", {"to": "back"}, {"to": "back"}),
    ],
)
def test_retained_whole_visual_changes_are_not_styling(
    message: str,
    op: str,
    slots: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    outcome = _run(message, op, lambda p: {"visual": _visual(p, CHART), **slots})
    assert outcome.ops[0] == {"op": op, "visual": CHART, **expected}


@pytest.mark.parametrize("name", ["Color", "Title", "Product Color"])
def test_source_field_names_do_not_become_styling_intent(name: str) -> None:
    context = contexts.context_for("agents", {})
    context.tables["DimEmployee"].append(name)
    outcome = _run(
        f"add {name} to the table",
        "set_fields",
        lambda p: {
            "visual": _visual(p, TABLE),
            "role": "Values",
            "mode": "add",
            "fields": [_field(p, name)],
        },
        context,
    )
    assert outcome.ops[0]["fields"] == [{"table": "DimEmployee", "name": name}]


@pytest.mark.parametrize("name", ["Color", "Title"])
def test_ambiguous_styling_like_source_field_names_clarify_instead_of_refusal(name: str) -> None:
    context = contexts.context_for("agents", {})
    context.tables["DimEmployee"].append(name)
    context.tables["DimStore"].append(name)
    model = Choose(lambda p: {"visual": _visual(p, TABLE), "role": "Values", "mode": "add"})
    engine = planner.EditPlanner(context, model)
    model.engine = engine
    engine.shortlist = lambda _: [next(c for c in engine.cards if c.op == "set_fields")]
    outcome = engine.plan([f"add {name} to the table"])
    assert outcome.kind == "clarify" and outcome.clause is not None
    assert outcome.clause.question and not outcome.ops and model.calls == 1
