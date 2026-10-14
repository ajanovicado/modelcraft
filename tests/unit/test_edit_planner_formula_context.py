"""Current-request formula semantics survive clause splitting, without cross-action leakage."""

from __future__ import annotations

import re
from collections.abc import Sequence
from threading import Event
from typing import Any, TypeVar, cast

import pytest
from pydantic import BaseModel

from ai_powerbi_analyst.studio.edit_context import EditContext, MeasureEntry
from ai_powerbi_analyst.studio.edit_planner import (
    PROMPT_BUDGET,
    DraftAnswer,
    EditPlanner,
    PlanOutcome,
    _scoped_formula_request,
    available_cards,
)
from ai_powerbi_analyst.studio.imported_chat_edits import (
    ChatEditEnv,
    PendingEdits,
    _Turn,
)
from ai_powerbi_analyst.studio.imported_editing import EditPort

_Answer = TypeVar("_Answer", bound=BaseModel)


def context(*, crowded: bool = False) -> EditContext:
    return EditContext(
        pages=[],
        visuals=[],
        measures=[
            MeasureEntry(name=f"Existing Measure {i}", table="Ledger", expression="1")
            for i in range(50)
        ]
        if crowded
        else [],
        tables={
            "Ledger": ["GrossValue", "AcquisitionCost", "CreditValue", "Units", "PackageCost"],
            "OtherLedger": ["GrossValue", "AcquisitionCost"],
        },
        head_revision="r0001",
    )


class FormulaModel:
    def __init__(self, drafts: Sequence[str], names: Sequence[str] = ("NetValue",)) -> None:
        self.drafts = list(drafts)
        self.names = names
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[_Answer],
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: int | None = None,
        repair_attempts: int | None = None,
    ) -> _Answer:
        self.calls.append(
            {
                "system": system_prompt,
                "user": user_prompt,
                "model": response_model,
                "max_output_tokens": max_output_tokens,
                "timeout_seconds": timeout_seconds,
                "repair_attempts": repair_attempts,
            }
        )
        if issubclass(response_model, DraftAnswer):
            return response_model.model_validate({"expression": self.drafts.pop(0)})
        tool = re.search(r"^(\d+) create_calculated_column:", user_prompt, re.M)
        assert tool is not None
        return response_model.model_validate(
            {
                "kind": "plan",
                "steps": [
                    {"tool": int(tool.group(1)), "slots": {"table": 1, "name": name}}
                    for name in self.names
                ],
            }
        )

    @property
    def formula_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if issubclass(call["model"], DraftAnswer)]


def run(
    request: str,
    model: FormulaModel,
    pieces: Sequence[str] | None = None,
    *,
    crowded: bool = False,
    budget: int | None = None,
) -> PlanOutcome:
    cards = tuple(card for card in available_cards() if card.op == "create_calculated_column")
    return EditPlanner(
        context(crowded=crowded), model, cards=cards, formula_request=request, budget=budget
    ).plan(pieces or [request])


def test_following_row_arithmetic_and_later_named_fields_reach_the_formula_prompt() -> None:
    piece = "Add a calculated column NetValue to Ledger."
    request = (
        piece
        + " For every row calculate GrossValue minus AcquisitionCost. Keep the existing columns."
    )
    model = FormulaModel(["Ledger[GrossValue] - Ledger[AcquisitionCost]"])
    result = run(request, model, [piece], crowded=True)
    assert result.kind == "plan"
    assert result.steps[0].operation["expression"] == "Ledger[GrossValue] - Ledger[AcquisitionCost]"
    draft = model.formula_calls[0]
    assert "For every row calculate GrossValue minus AcquisitionCost." in draft["user"]
    assert "Ledger[GrossValue]" in draft["user"] and "Ledger[AcquisitionCost]" in draft["user"]
    assert len(draft["system"].encode()) + len(draft["user"].encode()) <= PROMPT_BUDGET


def test_parser_inserted_target_quotes_do_not_make_the_same_formula_clause_unlinked() -> None:
    original = "Create a measure called Net Value that is GrossValue minus AcquisitionCost"
    parsed = 'Create a measure called "Net Value" that is GrossValue minus AcquisitionCost'
    scoped = _scoped_formula_request(parsed, original, context(), "Net Value")
    assert not scoped.problem and "GrossValue minus AcquisitionCost" in scoped.text


@pytest.mark.parametrize(
    "wrong",
    [
        "Ledger[GrossValue] - Ledger[CreditValue]",
        "Ledger[GrossValue] + Ledger[AcquisitionCost]",
        "Ledger[AcquisitionCost] - Ledger[GrossValue]",
        "OtherLedger[GrossValue] - OtherLedger[AcquisitionCost]",
        "Ledger[GrossValue]",
    ],
)
def test_known_but_wrong_operands_or_operator_get_one_bounded_repair(wrong: str) -> None:
    piece = "Add a calculated column NetValue to Ledger."
    model = FormulaModel([wrong, "'Ledger'[GrossValue] - 'Ledger'[AcquisitionCost]"])
    result = run(piece + " For each row use GrossValue minus AcquisitionCost.", model, [piece])
    assert result.kind == "plan"
    assert len(model.formula_calls) == 2
    assert "draft changed its operands or operator" in model.formula_calls[1]["user"]
    assert (
        result.steps[0].operation["expression"]
        == "'Ledger'[GrossValue] - 'Ledger'[AcquisitionCost]"
    )


def test_exhausted_wrong_binary_drafts_offer_no_operation() -> None:
    piece = "Add a calculated column NetValue to Ledger."
    model = FormulaModel(["Ledger[GrossValue] - Ledger[CreditValue]"] * 2)
    result = run(
        piece + " For every row calculate GrossValue minus AcquisitionCost.", model, [piece]
    )
    assert result.kind == "failed" and not result.steps
    assert "requested row calculation" in result.reason
    assert len(model.formula_calls) == 2


def test_simple_var_aliases_preserve_the_requested_binary_without_formula_rewriting() -> None:
    formula = (
        "VAR revenue = Ledger[GrossValue] VAR cost = Ledger[AcquisitionCost] RETURN revenue - cost"
    )
    request = "Add a calculated column NetValue to Ledger using GrossValue minus AcquisitionCost."
    model = FormulaModel([formula])
    result = run(request, model)
    assert result.kind == "plan" and result.steps[0].operation["expression"] == formula
    assert len(model.formula_calls) == 1


def test_two_actions_keep_each_target_and_its_adjacent_calculation() -> None:
    first = "Add a calculated column NetValue to Ledger."
    second = "Add a calculated column UnitNet to Ledger."
    request = (
        first
        + " For every row calculate GrossValue minus AcquisitionCost. "
        + second
        + " For each row calculate Units plus PackageCost."
    )
    model = FormulaModel(
        ["Ledger[GrossValue] - Ledger[AcquisitionCost]", "Ledger[Units] + Ledger[PackageCost]"],
        names=("NetValue", "UnitNet"),
    )
    result = run(request, model, [first, second])
    assert result.kind == "plan" and len(result.steps) == 2
    assert "Units plus PackageCost" not in model.formula_calls[0]["user"]
    assert "GrossValue minus AcquisitionCost" not in model.formula_calls[1]["user"]
    assert [step.operation["name"] for step in result.steps] == ["NetValue", "UnitNet"]


def test_one_following_detail_cannot_be_borrowed_across_two_same_sentence_actions() -> None:
    first = "Add a calculated column NetValue to Ledger"
    second = "add a calculated column UnitNet to Ledger."
    request = first + " and " + second + " For each row calculate GrossValue minus AcquisitionCost."
    scoped = _scoped_formula_request(second, request, context(), "UnitNet")
    assert scoped.problem and "Which new calculation" in scoped.problem
    assert "GrossValue" not in scoped.text


def test_unlinked_or_question_context_does_not_supply_formula_operands() -> None:
    piece = "Add a calculated column NetValue to Ledger."
    unlinked = _scoped_formula_request(
        piece, "Use GrossValue minus AcquisitionCost.", context(), "NetValue"
    )
    assert unlinked.text == piece and unlinked.problem
    scoped = _scoped_formula_request(
        piece, piece + " What is GrossValue minus AcquisitionCost?", context(), "NetValue"
    )
    assert "AcquisitionCost" not in scoped.text


@pytest.mark.parametrize(
    "later",
    [
        "Calculate another measure OtherValue using Units plus PackageCost.",
        "Create a new calculated column OtherValue using Units plus PackageCost.",
        "Use a new measure OtherValue for Units plus PackageCost.",
    ],
)
def test_independent_calculation_action_does_not_become_formula_context(later: str) -> None:
    piece = "Add a calculated column NetValue to Ledger."
    request = piece + " For every row calculate GrossValue minus AcquisitionCost. " + later
    scoped = _scoped_formula_request(piece, request, context(), "NetValue")
    assert not scoped.problem and "GrossValue minus AcquisitionCost" in scoped.text
    assert "Units" not in scoped.text and "OtherValue" not in scoped.text


def test_malformed_binary_still_fails_the_real_syntax_validator() -> None:
    request = "Add a calculated column NetValue to Ledger using GrossValue minus AcquisitionCost."
    model = FormulaModel(["Ledger[GrossValue] -"] * 2)
    result = run(request, model)
    assert result.kind == "failed" and not result.steps
    assert "not valid DAX" in result.reason
    assert len(model.formula_calls) == 2


def test_following_explicit_formula_stays_typed_and_avoids_formula_generation() -> None:
    piece = "Add a calculated column NetValue to Ledger."
    formula = "Ledger[GrossValue] - Ledger[AcquisitionCost]"
    model = FormulaModel([])
    result = run(piece + f" Use `{formula}`.", model, [piece])
    assert result.kind == "plan" and result.steps[0].operation["expression"] == formula
    assert not model.formula_calls


def test_formula_request_that_cannot_fit_is_refused_without_truncating_its_meaning() -> None:
    piece = "Add a calculated column NetValue to Ledger."
    request = (
        piece
        + " For every row calculate GrossValue minus AcquisitionCost "
        + ("with the exact supplied row semantics " * 15)
        + "."
    )
    model = FormulaModel([])
    result = run(request, model, [piece])
    assert result.kind == "failed" and not result.steps
    assert "calculation detail is too long" in result.reason
    assert not model.formula_calls


def test_small_formula_budget_cannot_send_a_prompt_without_the_required_operands() -> None:
    card = next(card for card in available_cards() if card.op == "create_calculated_column")
    slot = card.slot("expression")
    assert slot is not None
    model = FormulaModel([])
    planner = EditPlanner(context(crowded=True), model, cards=(card,), budget=450)
    result = planner._formula(
        card,
        slot,
        {"table": "Ledger", "name": "NetValue"},
        "Add a calculated column NetValue to Ledger using GrossValue minus AcquisitionCost.",
    )
    assert isinstance(result, str) and "explicitly requested fields" in result
    assert not model.calls


def test_imported_chat_model_tier_uses_current_user_detail_after_parser_drops_it() -> None:
    piece = "Add a calculated column NetValue to Ledger."
    request = piece + " For every row calculate GrossValue minus AcquisitionCost."
    model = FormulaModel(["Ledger[GrossValue] - Ledger[AcquisitionCost]"])
    env = ChatEditEnv(
        pending=PendingEdits(),
        record=lambda *_args: None,
        what_if=lambda _text: None,
        answer=lambda *_args: iter(()),
        model=model,
    )
    turn = _Turn(
        port=cast(EditPort, object()),
        env=env,
        request=object(),
        workspace_id="workspace-test",
        user_text=request,
        key="test-formula-context",
        cancel=Event(),
        emit=lambda _event: None,
        context=context(),
        report=None,
        snapshot=None,
        selection=None,
        page=None,
    )
    turn.model_tier([piece])
    assert turn.plan is not None and turn.plan.kind == "plan"
    assert "GrossValue minus AcquisitionCost" in model.formula_calls[0]["user"]
