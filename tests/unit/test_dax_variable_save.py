"""Variable formulas must survive the real pattern cache, Run and draft-save workflow."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from ai_powerbi_analyst.studio.contracts import (
    AddDaxMeasureRequest,
    ApproveDraftRequest,
    CreateWorkspaceRequest,
    DaxEvaluateRequest,
    DraftRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.planner import StudioPlanningService
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def test_yoy_variable_formula_runs_and_saves_exactly_without_malformed_cache_alias(tmp_path):
    source = tmp_path / "Sales.csv"
    source.write_text(
        "OrderDate,Sales,Region\n2025-01-01,50,East\n2025-01-01,50,West\n"
        "2026-01-01,75,East\n2026-01-01,75,West\n",
        encoding="utf-8",
    )
    dates = tmp_path / "DimDate.csv"
    dates.write_text(
        "DateKey,Year\n"
        + "".join(
            f"{day.isoformat()},{day.year}\n"
            for day in (date(2025, 1, 1) + timedelta(days=index) for index in range(730))
        ),
        encoding="utf-8",
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    service.model = None
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Variable-save regression",
            selected_folder=str(tmp_path),
            idempotency_key="variable-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source), str(dates)],
            idempotency_key="variable-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard",
            expected_base_hash=service.workspace(workspace.id).base_hash,
            idempotency_key="variable-draft",
        )
    )
    draft = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[item.id for item in draft.semantic_model.relationships],
            expected_base_hash=draft.base_hash,
            idempotency_key="variable-review",
        ),
    )
    answer = service.business_pattern_formula(
        workspace.id, "Create year-over-year absolute change in Total Sales using DimDate[DateKey]"
    )
    assert answer is not None and answer[1] == "local"
    formula = answer[0]
    assert formula.startswith("VAR CurrentValue = ")
    result = service.evaluate_dax(DaxEvaluateRequest(workspace_id=workspace.id, expression=formula))
    assert result.engine == "snapshot" and result.rows == [{"Value": 150}]
    malformed = formula.removeprefix("VAR CurrentValue = ")
    with pytest.raises(ValueError):
        service.evaluate_dax(DaxEvaluateRequest(workspace_id=workspace.id, expression=malformed))
    saved = service.add_dax_measure(
        workspace.id,
        AddDaxMeasureRequest(
            workspace_id=workspace.id,
            name="YoY Sales Difference",
            expression=formula,
            expected_base_hash=draft.base_hash,
            idempotency_key="variable-save",
        ),
    )
    measure = saved.measure_proposal.measures[-1]
    assert measure.expression == formula and measure.approved is False
    assert measure.dependencies == [
        next(item.id for item in draft.measure_proposal.measures if item.name == "Total Sales")
    ]
    assert service.draft(saved.id).measure_proposal.measures[-1].expression == formula
