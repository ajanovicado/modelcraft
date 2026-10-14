"""Registered business formula Run must read the current imported report model."""

from __future__ import annotations

import pandas as pd
import pytest
from test_imported_report_chat import _import

from ai_powerbi_analyst.studio import service as service_module
from ai_powerbi_analyst.studio.contracts import DaxEvaluateRequest
from ai_powerbi_analyst.studio.state import StateConflictError


@pytest.fixture
def imported(tmp_path, monkeypatch):
    service, workspace_id = _import(
        tmp_path,
        monkeypatch,
        frames={"Sales": pd.DataFrame({"Amount": [100, 200], "Cost": [10, 50]})},
        measures=[("Revenue", "SUM('Sales'[Amount])")],
        relationships=[],
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("Explicit ratio must not call a model or apply an edit")

    monkeypatch.setattr(service.model, "generate", forbidden, raising=False)
    monkeypatch.setattr(service.editing.editor, "apply", forbidden)
    return service, workspace_id


def _formula(service, workspace_id):
    expression, how, reading = service.business_pattern_formula(
        workspace_id,
        "total Cost divided by Revenue",
    )
    assert how == "local" and expression
    return expression, reading


def _run(service, workspace_id, expression):
    return service.evaluate_dax(
        DaxEvaluateRequest(
            workspace_id=workspace_id,
            expression=expression,
        )
    )


def test_registered_imported_run_uses_healed_metadata_not_historic_import(imported, monkeypatch):
    service, workspace_id = imported
    old = service.imported_report(workspace_id)
    current = old.model_copy(
        update={
            "measures": [
                {
                    **old.measures[0],
                    "Expression": "2 * SUM('Sales'[Amount])",
                }
            ]
        }
    )
    before = service.editing.head(workspace_id)
    monkeypatch.setattr(service.editing, "report", lambda _workspace: current)
    expression, reading = _formula(service, workspace_id)
    assert "Value = 0.1" in reading
    assert _run(service, workspace_id, expression).rows == [{"Value": 0.1}]
    assert service.imported_report(workspace_id) == old
    assert service.editing.head(workspace_id) == before


def test_registered_imported_run_rejects_measure_removed_from_current_model(imported, monkeypatch):
    service, workspace_id = imported
    expression, _ = _formula(service, workspace_id)
    old = service.imported_report(workspace_id)
    current = old.model_copy(update={"measures": []})
    monkeypatch.setattr(service.editing, "report", lambda _workspace: current)
    with pytest.raises(StateConflictError, match="changed|regenerate"):
        _run(service, workspace_id, expression)
    formula, how, reading = service.business_pattern_formula(
        workspace_id,
        "total Cost divided by Revenue",
    )
    assert formula == "" and how == "clarify"
    assert "Revenue" in reading and "No formula or report change was applied" in reading


def test_stale_healed_view_is_refused_during_generation_and_run(imported, monkeypatch):
    service, workspace_id = imported
    expression, _ = _formula(service, workspace_id)
    monkeypatch.setattr(service.editing, "head", lambda _workspace: {"hash": "b" * 64})

    def forbidden(_workspace):
        pytest.fail("An older healed view must be refused before reading its model")

    monkeypatch.setattr(service, "_business_pattern_model", forbidden)
    formula, how, reading = service.business_pattern_formula(
        workspace_id,
        "total Cost divided by Revenue",
    )
    assert formula == "" and how == "clarify"
    assert "could not read its current model" in reading
    with pytest.raises(StateConflictError, match="changed|regenerate"):
        _run(service, workspace_id, expression)


def test_imported_head_change_during_run_never_returns_old_rows(imported, monkeypatch):
    service, workspace_id = imported
    expression, _ = _formula(service, workspace_id)
    original = service_module.evaluate_pattern_model

    def moved_after_evaluation(model, request):
        result = original(model, request)
        monkeypatch.setattr(service.editing, "head", lambda _workspace: {"hash": "b" * 64})
        return result

    monkeypatch.setattr(service_module, "evaluate_pattern_model", moved_after_evaluation)
    with pytest.raises(StateConflictError, match="changed|regenerate"):
        _run(service, workspace_id, expression)
