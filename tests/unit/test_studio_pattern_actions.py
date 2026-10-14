"""Generated CSV patterns remain executable, fresh and reviewable after generation."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from test_pattern_preview import _built, _draft, _relationship

from ai_powerbi_analyst.studio.contracts import (
    AddDaxMeasureRequest,
    AdvancedDaxProposal,
    ApproveAdvancedDaxRequest,
    CreateWorkspaceRequest,
    DaxEvaluateRequest,
    IngestRequest,
    MeasureProposal,
)
from ai_powerbi_analyst.studio.dax_engine import DaxEngineLimit
from ai_powerbi_analyst.studio.dax_patterns import (
    MetricSelection,
    PatternSelection,
    build_pattern,
    catalog_from_model,
    select_business_pattern,
)
from ai_powerbi_analyst.studio.pattern_preview import preview_pattern
from ai_powerbi_analyst.studio.planner import StudioPlanningError, StudioPlanningService
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError


@pytest.fixture
def studio(tmp_path: Path):
    source = tmp_path / "orders.csv"
    source.write_text(
        "OrderId,CustomerId,Region,SalesAmount,DiscountAmount,OrderDate,ShipDate\n"
        "A,1,East,20,4,2023-12-31T16:00:00,2024-01-02T14:00:00\n"
        "B,1,West,30,6,2024-01-01T12:00:00,2024-01-02T14:00:00\n"
        "C,2,East,50,10,2024-03-02T18:00:00,2024-03-03T14:00:00\n",
        encoding="utf-8",
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)

    class NoModel:
        def generate(self, *_, **__):
            pytest.fail("Explicit patterns in these tests must not make a live model call.")

    service.model = NoModel()
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Pattern actions",
            selected_folder=str(tmp_path),
            idempotency_key="patterns-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="patterns-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    manifest = service.ingestion.load_manifest(snapshot)
    draft = _draft(snapshot, manifest).model_copy(update={"workspace_id": workspace.id})
    draft = draft.model_copy(update={"base_hash": service.store.content_hash(draft)})
    service.store.put(
        "draft",
        draft,
        workspace_id=workspace.id,
        expected_base_hash=EMPTY_HASH,
        idempotency_key="patterns-initial-draft",
        mutation_payload={"draft": draft.id},
    )
    workspace = service.workspace(workspace.id)
    updated = workspace.model_copy(update={"active_draft_id": draft.id})
    updated = updated.model_copy(update={"base_hash": service.store.content_hash(updated)})
    service.store.put(
        "workspace",
        updated,
        workspace_id=workspace.id,
        expected_base_hash=workspace.base_hash,
        idempotency_key="patterns-select-draft",
        mutation_payload={"draft": draft.id},
    )
    return service, workspace.id, snapshot, source


def _activate_draft(service, workspace_id, draft):
    draft = draft.model_copy(update={"workspace_id": workspace_id})
    draft = draft.model_copy(update={"base_hash": service.store.content_hash(draft)})
    service.store.put(
        "draft",
        draft,
        workspace_id=workspace_id,
        expected_base_hash=EMPTY_HASH,
        idempotency_key="patterns-activate-new-draft-" + draft.id,
        mutation_payload=draft,
    )
    workspace = service.workspace(workspace_id)
    updated = workspace.model_copy(update={"active_draft_id": draft.id})
    updated = updated.model_copy(update={"base_hash": service.store.content_hash(updated)})
    service.store.put(
        "workspace",
        updated,
        workspace_id=workspace_id,
        expected_base_hash=workspace.base_hash,
        idempotency_key="patterns-select-new-draft-" + draft.id,
        mutation_payload={"draft": draft.id},
    )
    return draft


def _replace_draft(service, workspace_id, **updates):
    workspace = service.workspace(workspace_id)
    draft = service.draft(workspace.active_draft_id)
    updated = draft.model_copy(update=updates)
    updated = updated.model_copy(update={"base_hash": service.store.content_hash(updated)})
    return service.store.put(
        "draft",
        updated,
        workspace_id=workspace_id,
        expected_base_hash=draft.base_hash,
        idempotency_key="change-pattern-draft-" + updated.base_hash[:12],
        mutation_payload=updated,
    )


def _compile(service, workspace_id, pattern: str):
    model = service._business_pattern_model(workspace_id)
    catalog = catalog_from_model(model)

    def field(name):
        return next(item.index for item in catalog.fields if item.name == name)

    metric = MetricSelection(field=field("Total Sales"))
    if pattern == "ratio":
        built = select_business_pattern("total DiscountAmount divided by Total Sales", catalog)
    elif pattern == "average_per":
        built = build_pattern(
            PatternSelection(pattern=pattern, base=metric, entity=field("CustomerId")), catalog
        )
    elif pattern == "distinct_count":
        built = build_pattern(
            PatternSelection(pattern=pattern, entity=field("CustomerId")), catalog
        )
    elif pattern == "top_n":
        built = build_pattern(
            PatternSelection(
                pattern=pattern,
                base=metric,
                group=field("Region"),
                scope="all",
                direction="descending",
                n=1,
            ),
            catalog,
        )
    else:
        built = select_business_pattern("YTD Total Sales", catalog)
    assert built is not None
    preview_pattern(model, built)
    service._remember_business_pattern(workspace_id, built)
    return built


def _run(service, workspace_id, built, **kwargs):
    return service.evaluate_dax(
        DaxEvaluateRequest(workspace_id=workspace_id, expression=built.expression, **kwargs)
    )


def test_generated_csv_ratio_run_returns_actual_values(studio):
    service, workspace_id, _snapshot, _source = studio
    expression, how, _reading = service.business_pattern_formula(
        workspace_id, "total DiscountAmount divided by Total Sales"
    )
    assert how == "local"
    result = service.evaluate_dax(
        DaxEvaluateRequest(workspace_id=workspace_id, expression=expression)
    )
    assert result.rows == [{"Value": 0.2}]
    assert "page slicer selections" in result.plan.casefold()


def test_registered_query_runs_with_rows_and_cannot_save_as_measure(studio):
    service, workspace_id, _snapshot, _source = studio
    built = _compile(service, workspace_id, "top_n")
    result = _run(service, workspace_id, built)
    assert result.row_count == 1
    assert result.rows[0]["Region"] == "East"
    assert result.rows[0]["__PatternValue"] == 70
    draft = service.draft(service.workspace(workspace_id).active_draft_id)
    with pytest.raises(ValueError, match="query|table|measure"):
        service.add_dax_measure(
            workspace_id,
            AddDaxMeasureRequest(
                workspace_id=workspace_id,
                name="Top Region",
                expression=built.expression,
                expected_base_hash=draft.base_hash,
                idempotency_key="patterns-save-query",
            ),
        )
    assert service.draft(draft.id).base_hash == draft.base_hash


def test_reviewed_generated_calendar_ytd_runs_but_virtual_calendar_cannot_save(studio):
    service, workspace_id, _snapshot, _source = studio
    draft = service.draft(service.workspace(workspace_id).active_draft_id)
    _replace_draft(
        service,
        workspace_id,
        semantic_model=draft.semantic_model.model_copy(update={"calendar_required": True}),
    )
    built = _compile(service, workspace_id, "period_to_date")
    assert _run(service, workspace_id, built).rows == [{"Value": 80}]
    draft = service.draft(draft.id)
    with pytest.raises(ValueError, match="calendar|Calendar|date|Date"):
        service.add_dax_measure(
            workspace_id,
            AddDaxMeasureRequest(
                workspace_id=workspace_id,
                name="QA YTD Sales",
                expression=built.expression,
                expected_base_hash=draft.base_hash,
                idempotency_key="patterns-save-virtual-calendar",
            ),
        )
    assert service.draft(draft.id).base_hash == draft.base_hash


@pytest.mark.parametrize(
    "pattern, expected, dependencies",
    [
        ("ratio", 0.2, ["total_sales"]),
        ("average_per", 50, ["total_sales"]),
        ("distinct_count", 2, []),
    ],
)
def test_scalar_pattern_save_keeps_computation_dependencies_and_review_gate(
    studio, pattern, expected, dependencies
):
    service, workspace_id, snapshot, _source = studio
    built = _compile(service, workspace_id, pattern)
    assert _run(service, workspace_id, built).rows == [{"Value": expected}]
    draft = service.draft(service.workspace(workspace_id).active_draft_id)
    saved = service.add_dax_measure(
        workspace_id,
        AddDaxMeasureRequest(
            workspace_id=workspace_id,
            name="QA " + pattern,
            expression=built.expression,
            expected_base_hash=draft.base_hash,
            idempotency_key="patterns-save-" + pattern,
        ),
    )
    proposal = saved.measure_proposal.measures[-1]
    assert isinstance(proposal, AdvancedDaxProposal)
    assert proposal.expression == built.expression
    assert proposal.table_id == snapshot.tables[0].table.id
    assert proposal.dependencies == dependencies
    assert proposal.approved is False
    assert saved.semantic_model.approved is False and saved.status == "draft"
    assert "Evaluated in the DAX lab" in proposal.assumptions[0]
    approved = service.approve_advanced_dax(
        saved.id,
        proposal.id,
        ApproveAdvancedDaxRequest(
            workspace_id=workspace_id,
            draft_id=saved.id,
            measure_id=proposal.id,
            approved=True,
            approval_note="Reviewed field binding and actual pattern result.",
            expected_base_hash=saved.base_hash,
            idempotency_key="patterns-approve-" + pattern,
        ),
    )
    assert approved.measure_proposal.measures[-1].approved is True
    assert approved.semantic_model.approved is False


def test_run_loads_a_fresh_model_each_time(studio, monkeypatch):
    service, workspace_id, _snapshot, _source = studio
    built = _compile(service, workspace_id, "ratio")
    original = service._business_pattern_model
    models = []

    def fresh(workspace):
        model = original(workspace)
        models.append(model)
        return model

    monkeypatch.setattr(service, "_business_pattern_model", fresh)
    assert _run(service, workspace_id, built).rows == [{"Value": 0.2}]
    assert _run(service, workspace_id, built).rows == [{"Value": 0.2}]
    assert len(models) == 2 and models[0] is not models[1]


def test_changed_draft_rejects_registered_pattern_before_run_or_save(studio):
    service, workspace_id, _snapshot, _source = studio
    built = _compile(service, workspace_id, "ratio")
    changed = _replace_draft(service, workspace_id, prompt="A changed dashboard draft")
    with pytest.raises((StateConflictError, ValueError), match="changed|stale|again|fresh"):
        _run(service, workspace_id, built)
    with pytest.raises((StateConflictError, ValueError), match="changed|stale|again|fresh"):
        service.add_dax_measure(
            workspace_id,
            AddDaxMeasureRequest(
                workspace_id=workspace_id,
                name="QA Stale Ratio",
                expression=built.expression,
                expected_base_hash=changed.base_hash,
                idempotency_key="patterns-save-stale-draft",
            ),
        )
    assert service.draft(changed.id).base_hash == changed.base_hash


def test_changed_snapshot_rejects_registered_pattern(studio):
    service, workspace_id, _snapshot, source = studio
    built = _compile(service, workspace_id, "ratio")
    source.write_text(
        source.read_text(encoding="utf-8").replace(",20,4,", ",200,4,"), encoding="utf-8"
    )
    workspace = service.workspace(workspace_id)
    service.ingest(
        IngestRequest(
            workspace_id=workspace_id,
            item_paths=[str(source)],
            expected_base_hash=workspace.base_hash,
            idempotency_key="patterns-reingest-changed-data",
        )
    )
    with pytest.raises((StateConflictError, ValueError), match="changed|stale|again|fresh"):
        _run(service, workspace_id, built)


def test_failed_run_does_not_fall_back_or_return_cached_pattern_result(studio, monkeypatch):
    service, workspace_id, _snapshot, _source = studio
    built = _compile(service, workspace_id, "ratio")

    def fail(_workspace):
        raise DaxEngineLimit("Current model exceeds the local engine limit.")

    monkeypatch.setattr(service, "_business_pattern_model", fail)
    with pytest.raises(DaxEngineLimit, match="limit"):
        _run(service, workspace_id, built)


def test_unregistered_plain_pasted_dax_uses_existing_lab_evaluation(studio):
    service, workspace_id, _snapshot, _source = studio
    result = service.evaluate_dax(
        DaxEvaluateRequest(workspace_id=workspace_id, expression="SUM('orders'[SalesAmount])")
    )
    assert list(result.rows[0].values()) == [100]


def test_registered_optional_measure_header_runs_same_computed_body(studio):
    service, workspace_id, _snapshot, _source = studio
    built = _compile(service, workspace_id, "ratio")
    result = service.evaluate_dax(
        DaxEvaluateRequest(
            workspace_id=workspace_id,
            expression="Discount Share = " + built.expression,
        )
    )
    assert result.rows == [{"Value": 0.2}]


def test_draft_change_during_generation_does_not_publish_old_formula(studio, monkeypatch):
    from ai_powerbi_analyst.studio import service as service_module

    service, workspace_id, _snapshot, _source = studio
    original = service_module.preview_pattern

    def changed_after_preview(model, built):
        proof = original(model, built)
        _replace_draft(service, workspace_id, prompt="Draft changed during preview")
        return proof

    monkeypatch.setattr(service_module, "preview_pattern", changed_after_preview)
    expression, how, reading = service.business_pattern_formula(
        workspace_id, "total DiscountAmount divided by Total Sales"
    )
    assert expression == "" and how == "clarify"
    assert "changed" in reading and "No formula or report change was applied" in reading


def test_reviewed_source_date_dimension_ytd_runs_and_saves_source_bound_dax(studio):
    service, workspace_id, _snapshot, source = studio
    dates = source.with_name("dates.csv")
    dates.write_text(
        "Date,Year\n"
        + "\n".join(
            f"{day:%Y-%m-%d},{day.year}" for day in pd.date_range("2023-01-01", "2024-12-31")
        )
        + "\n",
        encoding="utf-8",
    )
    for value in ("2023-12-31T16:00:00", "2024-01-01T12:00:00", "2024-03-02T18:00:00"):
        source.write_text(
            source.read_text(encoding="utf-8").replace(value, value.split("T")[0]),
            encoding="utf-8",
        )
    workspace = service.workspace(workspace_id)
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace_id,
            item_paths=[str(source), str(dates)],
            expected_base_hash=workspace.base_hash,
            idempotency_key="patterns-ingest-source-calendar",
        )
    )
    manifest = service.ingestion.load_manifest(snapshot)
    draft = _draft(
        snapshot, manifest, names={"orders": "Sales Facts", "dates": "Reviewed Dates"}
    ).model_copy(update={"id": "reviewed-calendar-draft"})
    draft = draft.model_copy(
        update={
            "semantic_model": draft.semantic_model.model_copy(
                update={"relationships": [_relationship(manifest, date=True)]}
            )
        }
    )
    _activate_draft(service, workspace_id, draft)
    built = _compile(service, workspace_id, "period_to_date")
    assert built.expression == "TOTALYTD([Total Sales], 'Reviewed Dates'[Date])"
    assert _run(service, workspace_id, built).rows == [{"Value": 80}]
    current = service.draft(draft.id)
    saved = service.add_dax_measure(
        workspace_id,
        AddDaxMeasureRequest(
            workspace_id=workspace_id,
            name="QA Source YTD",
            expression=built.expression,
            expected_base_hash=current.base_hash,
            idempotency_key="patterns-save-source-calendar-ytd",
        ),
    )
    proposal = saved.measure_proposal.measures[-1]
    assert proposal.expression == "TOTALYTD([Total Sales], dates[Date])"
    assert proposal.dependencies == ["total_sales"]
    assert proposal.approved is False and saved.semantic_model.approved is False
    approved = service.approve_advanced_dax(
        saved.id,
        proposal.id,
        ApproveAdvancedDaxRequest(
            workspace_id=workspace_id,
            draft_id=saved.id,
            measure_id=proposal.id,
            approved=True,
            approval_note="Reviewed source calendar and actual YTD value.",
            expected_base_hash=saved.base_hash,
            idempotency_key="patterns-approve-source-calendar-ytd",
        ),
    )
    model = service._business_pattern_model(workspace_id)
    assert model.measures[proposal.name.casefold()].text == built.expression
    assert preview_pattern(model, _built("[QA Source YTD]"))[1].endswith("Value = 80.")
    assert approved.measure_proposal.measures[-1].approved is True


def test_renamed_table_ratio_save_uses_source_names_and_approval_restores_semantic_names(studio):
    service, workspace_id, snapshot, _source = studio
    draft = service.draft(service.workspace(workspace_id).active_draft_id)
    semantic = draft.semantic_model.model_copy(
        update={
            "tables": [
                item.model_copy(update={"display_name": "Sales Facts"})
                for item in draft.semantic_model.tables
            ]
        }
    )
    _replace_draft(service, workspace_id, semantic_model=semantic)
    built = _compile(service, workspace_id, "ratio")
    assert built.expression == "DIVIDE(SUM('Sales Facts'[DiscountAmount]), [Total Sales])"
    current = service.draft(draft.id)
    saved = service.add_dax_measure(
        workspace_id,
        AddDaxMeasureRequest(
            workspace_id=workspace_id,
            name="QA Renamed Discount Share",
            expression=built.expression,
            expected_base_hash=current.base_hash,
            idempotency_key="patterns-save-renamed-table",
        ),
    )
    proposal = saved.measure_proposal.measures[-1]
    assert proposal.expression == "DIVIDE(SUM(orders[DiscountAmount]), [Total Sales])"
    assert proposal.table_id == snapshot.tables[0].table.id
    assert proposal.dependencies == ["total_sales"] and proposal.approved is False
    service.approve_advanced_dax(
        saved.id,
        proposal.id,
        ApproveAdvancedDaxRequest(
            workspace_id=workspace_id,
            draft_id=saved.id,
            measure_id=proposal.id,
            approved=True,
            approval_note="Reviewed source and display name bindings.",
            expected_base_hash=saved.base_hash,
            idempotency_key="patterns-approve-renamed-table",
        ),
    )
    model = service._business_pattern_model(workspace_id)
    assert model.measures[proposal.name.casefold()].text == built.expression
    assert preview_pattern(model, _built("[QA Renamed Discount Share]"))[1].endswith("Value = 0.2.")


def test_pattern_save_keeps_existing_dependency_graph_validation_and_no_mutation(studio):
    service, workspace_id, snapshot, _source = studio
    draft = service.draft(service.workspace(workspace_id).active_draft_id)
    cyclic = AdvancedDaxProposal(
        id="recursive_measure",
        name="Recursive Measure",
        table_id=snapshot.tables[0].table.id,
        expression="[Recursive Measure]",
        dependencies=["recursive_measure"],
        format_string="General",
        assumptions=["Unapproved invalid existing proposal for graph validation."],
    )
    changed = _replace_draft(
        service,
        workspace_id,
        measure_proposal=MeasureProposal(measures=[*draft.measure_proposal.measures, cyclic]),
    )
    built = _compile(service, workspace_id, "ratio")
    with pytest.raises(StudioPlanningError, match="cycle"):
        service.add_dax_measure(
            workspace_id,
            AddDaxMeasureRequest(
                workspace_id=workspace_id,
                name="QA Valid Ratio With Bad Existing Graph",
                expression=built.expression,
                expected_base_hash=changed.base_hash,
                idempotency_key="patterns-save-graph-refusal",
            ),
        )
    assert service.draft(changed.id) == changed


def test_failed_save_proof_keeps_draft_unchanged(studio, monkeypatch):
    service, workspace_id, _snapshot, _source = studio
    built = _compile(service, workspace_id, "ratio")
    draft = service.draft(service.workspace(workspace_id).active_draft_id)

    def failed_current_model(_workspace):
        raise DaxEngineLimit("Current snapshot is unavailable for proof.")

    monkeypatch.setattr(service, "_business_pattern_model", failed_current_model)
    with pytest.raises(DaxEngineLimit, match="unavailable"):
        service.add_dax_measure(
            workspace_id,
            AddDaxMeasureRequest(
                workspace_id=workspace_id,
                name="QA Failed Proof",
                expression=built.expression,
                expected_base_hash=draft.base_hash,
                idempotency_key="patterns-save-failed-proof",
            ),
        )
    assert service.draft(draft.id) == draft
