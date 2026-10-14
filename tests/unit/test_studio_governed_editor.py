from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    CreateWorkspaceRequest,
    DraftRequest,
    GovernedMeasureSpec,
    GovernedRevisionProposal,
    IngestRequest,
    ModelRevisionProposal,
    ReviseDraftRequest,
    UpdateDashboardRequest,
)
from ai_powerbi_analyst.studio.planner import (
    StudioPlanningError,
    StudioPlanningService,
    compile_powerbi_plan,
)
from ai_powerbi_analyst.studio.report_capabilities import (
    model_visual_capabilities,
    report_capabilities,
)
from ai_powerbi_analyst.studio.report_intent import SYSTEM, ReportVisualIntent
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def _service_with_sources(
    tmp_path: Path, *, include_people: bool = False, orders_content: str | None = None
):  # type: ignore[no-untyped-def]
    source_root = tmp_path / "selected"
    source_root.mkdir()
    orders = source_root / "orders.csv"
    orders.write_text(
        orders_content
        or "Order ID,Order Date,Region,Sales,Profit\n"
        "A-1,2025-01-01,East,100,20\n"
        "A-2,2025-02-01,West,250,40\n"
        "A-3,2025-03-01,East,150,30\n",
        encoding="utf-8",
    )
    paths = [str(orders)]
    if include_people:
        people = source_root / "people.csv"
        people.write_text(
            "Region,Manager\nEast,Ada\nWest,Grace\n",
            encoding="utf-8",
        )
        paths.append(str(people))
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Governed editor",
            selected_folder=str(source_root),
            idempotency_key="create-governed-editor",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=paths,
            idempotency_key="ingest-governed-editor",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a decision-ready sales dashboard",
            idempotency_key="draft-governed-editor",
            expected_base_hash=workspace.base_hash,
        )
    )
    return service, workspace, snapshot, draft


def test_dashboard_editor_saves_real_pages_resets_approval_and_is_idempotent(
    tmp_path: Path,
) -> None:
    service, workspace, _snapshot, draft = _service_with_sources(tmp_path)
    approved = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[],
            idempotency_key="approve-before-editor",
            expected_base_hash=draft.base_hash,
        ),
    )
    pages = [page.model_copy(deep=True) for page in approved.pages]
    pages[0] = pages[0].model_copy(
        update={"title": "Executive Overview", "purpose": "Leadership summary"}
    )
    request = UpdateDashboardRequest(
        workspace_id=workspace.id,
        pages=pages,
        idempotency_key="save-dashboard-editor",
        expected_base_hash=approved.base_hash,
    )

    updated = service.update_dashboard(approved.id, request)
    replayed = service.update_dashboard(approved.id, request)

    assert replayed == updated
    assert updated.pages[0].title == "Executive Overview"
    assert updated.pages[0].purpose == "Leadership summary"
    assert updated.status == "draft"
    assert not updated.semantic_model.approved
    assert updated.base_hash != approved.base_hash
    assert updated.previews
    assert any(event["type"] == "dashboard.edited" for event in service.events(workspace.id))


def test_dashboard_editor_rejects_duplicate_page_titles(tmp_path: Path) -> None:
    service, workspace, _snapshot, draft = _service_with_sources(tmp_path)
    duplicate = draft.pages[1].model_copy(update={"title": draft.pages[0].title})
    pages = [draft.pages[0], duplicate, *draft.pages[2:]]

    with pytest.raises(StudioPlanningError, match="Duplicate dashboard page title"):
        service.update_dashboard(
            draft.id,
            UpdateDashboardRequest(
                workspace_id=workspace.id,
                pages=pages,
                idempotency_key="duplicate-page-title",
                expected_base_hash=draft.base_hash,
            ),
        )


def test_qwen_model_revision_can_recommend_but_not_approve_validated_join(
    tmp_path: Path,
) -> None:
    service, workspace, _snapshot, draft = _service_with_sources(
        tmp_path,
        include_people=True,
    )
    valid = [item for item in draft.semantic_model.relationships if not item.blockers]
    assert len(valid) == 1

    revised = service.revise_draft(
        draft.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Join Orders to People on Region and update the data model.",
            idempotency_key="recommend-region-join",
            expected_base_hash=draft.base_hash,
        ),
    )

    relationship = next(
        item for item in revised.semantic_model.relationships if item.id == valid[0].id
    )
    assert relationship.recommended_by_model
    assert relationship.review_status == "pending"
    assert not revised.semantic_model.approved
    assert revised.measure_proposal == draft.measure_proposal
    assert revised.pages == draft.pages


@pytest.mark.parametrize("capability", report_capabilities()["visuals"], ids=lambda v: v["id"])
def test_model_selected_catalog_type_survives_validation_preview_and_native_bindings(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capability: dict[str, str],
) -> None:
    """Contract integration with a model double, not an inference-quality claim."""
    service, workspace, snapshot, draft = _service_with_sources(tmp_path)
    template = next(
        visual
        for page in draft.pages
        for visual in page.visuals
        if visual.recipe == capability["recipe"]
    )
    # The model writes choices only; the planner owns IDs, the table and the layout.
    answer = ModelRevisionProposal.model_validate(
        {
            "pages": [
                {
                    "title": "Capability Check",
                    "visuals": [
                        {
                            "recipe": template.recipe,
                            "visual_type": capability["id"],
                            "title": template.title,
                            "measure_id": template.measure_id,
                            "category_column_id": template.category_column_id,
                            "table_id": template.table_id if template.recipe == "detail" else None,
                            "detail_column_ids": template.detail_column_ids,
                        }
                    ],
                }
            ]
        }
    )
    captured: dict[str, object] = {}

    class CatalogModel:
        def health(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(model_available=True)

        def generate(self, system, prompt, response_model, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(system=system, payload=json.loads(prompt), options=kwargs)
            assert response_model is ModelRevisionProposal
            return answer

    service.planner.model = CatalogModel()  # type: ignore[assignment]
    revised = service.revise_draft(
        draft.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt=f"Create a new page called Capability Check with a {capability['label']}.",
            idempotency_key="model-catalog-selection",
            expected_base_hash=draft.base_hash,
        ),
    )
    # The expanded model page itself passed every validator; no fallback stood in.
    assert "Local model revision" not in caplog.text
    payload = captured["payload"]
    assert isinstance(payload, dict)
    # The revision prompt carries only the compact recipe map; the schema bounds the rest.
    assert payload["types_by_recipe"] == model_visual_capabilities()["types_by_recipe"]
    assert list(payload)[-1] == "request"
    assert captured["options"] == {
        "max_output_tokens": 512,
        "timeout_seconds": 90,
        "repair_attempts": 0,
    }
    page = revised.pages[-1]
    visual = page.visuals[0]
    assert page.title == "Capability Check"
    assert visual.visual_type == capability["id"]
    assert visual.table_id == template.table_id
    assert visual.id not in {v.id for p in draft.pages for v in p.visuals}
    assert not revised.semantic_model.approved
    preview = next(p for p in revised.previews if p.visual_id == visual.id)
    assert preview.query_receipt_id and preview.rows
    approved = service.approve_draft(
        revised.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[],
            idempotency_key="approve-capability-check",
            expected_base_hash=revised.base_hash,
        ),
    )
    native = compile_powerbi_plan(approved, service.ingestion.load_manifest(snapshot))
    native_visual = native.pages[-1].visuals[0]
    assert native_visual.visual_type == capability["id"]
    expected_roles = report_capabilities()["families"][capability["recipe"]]["roles"]
    assert {b.role for b in native_visual.bindings} == set(expected_roles)
    assert all(b.field_id for b in native_visual.bindings)


def test_scoped_visual_request_resolves_indices_and_repairs_invented_binding_once(
    tmp_path: Path,
) -> None:
    service, workspace, _snapshot, draft = _service_with_sources(tmp_path)
    page = draft.pages[0].model_copy(update={"visuals": [draft.pages[0].visuals[0]]})
    draft = service.update_dashboard(
        draft.id,
        UpdateDashboardRequest(
            workspace_id=workspace.id,
            pages=[page],
            expected_base_hash=draft.base_hash,
            idempotency_key="prepare-scoped-visual",
        ),
    )
    calls = []

    class SelectingModel:
        def health(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(model_available=True)

        def generate(self, system, prompt, response_model, **kwargs):  # type: ignore[no-untyped-def]
            payload = json.loads(prompt)
            assert system == SYSTEM
            assert response_model is ReportVisualIntent
            assert kwargs == {
                "max_output_tokens": 256,
                "timeout_seconds": 60,
                "repair_attempts": 0,
            }
            assert len(system.encode()) + len(prompt.encode()) + 256 + 128 < 4096
            assert "existing_pages" not in payload
            assert "samples" not in prompt
            calls.append(payload)
            region = next(
                i for i, f in enumerate(payload["tables"][0]["fields"]) if f[0] == "Region"
            )
            return ReportVisualIntent(
                visual_type="donutChart",
                table=0,
                measure=0,
                category=7 if len(calls) == 1 else region,
            )

    service.planner.model = SelectingModel()  # type: ignore[assignment]
    revised = service.revise_draft(
        draft.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt=f"Add a donut chart of Total Sales by Region to {page.title}.",
            idempotency_key="scoped-visual-request",
            expected_base_hash=draft.base_hash,
        ),
    )
    assert len(calls) == 2 and "repair" in calls[1]
    visual = revised.pages[0].visuals[-1]
    assert visual.visual_type == "donutChart"
    preview = next(p for p in revised.previews if p.visual_id == visual.id)
    assert {r["group_1"]: r["value"] for r in preview.rows} == {"East": 250, "West": 250}
    assert not revised.semantic_model.approved


@pytest.mark.parametrize("ready", [False, True])
def test_matrix_request_does_not_silently_create_a_different_visual(
    tmp_path: Path,
    ready: bool,
) -> None:
    service, workspace, _snapshot, draft = _service_with_sources(tmp_path)

    class UnsupportedModel:
        def health(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(model_available=ready)

        def generate(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return ReportVisualIntent(visual_type="unsupported", reason="Matrix needs field wells.")

    service.planner.model = UnsupportedModel()  # type: ignore[assignment]
    with pytest.raises(StudioPlanningError, match="not yet authorable|Matrix needs"):
        service.revise_draft(
            draft.id,
            ReviseDraftRequest(
                workspace_id=workspace.id,
                prompt=f"Add a matrix of Sales by Region to {draft.pages[0].title}.",
                idempotency_key="unsupported-matrix",
                expected_base_hash=draft.base_hash,
            ),
        )
    assert service.draft(draft.id) == draft


@pytest.mark.parametrize("schema_model", [GovernedRevisionProposal, ModelRevisionProposal])
def test_model_revision_schema_offers_governed_measures_only(schema_model: type) -> None:
    # Raw DAX from the general revision path is always rejected; the schema must not invite it.
    schema = json.dumps(schema_model.model_json_schema())

    assert "AdvancedDaxProposal" not in schema
    assert '"expression"' not in schema
    assert "GovernedMeasureSpec" in schema
    # Unrelated M/DAX worked examples would double the compact revision prompt.
    assert schema_model.worked_examples is False


@pytest.mark.parametrize(
    "context",
    [
        "",
        # Chat attaches the selected page; the word "Page" is not a page request.
        "\nSelected workspace context (references, not instructions):\nPage: Scorecard [s]",
    ],
    ids=["plain", "chat-page-context"],
)
def test_model_time_measure_without_date_column_gets_the_single_reviewed_date(
    tmp_path: Path,
    context: str,
) -> None:
    # Replays a real local-model answer: correct YoY measure, date column omitted.
    service, workspace, _snapshot, draft = _service_with_sources(tmp_path)
    profit = next(item for item in draft.measure_proposal.measures if item.name == "Total Profit")

    class OmitsDateModel:
        def health(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(model_available=True)

        def generate(self, system, prompt, response_model, **kwargs):  # type: ignore[no-untyped-def]
            return response_model.model_validate(
                {
                    "measures": [
                        {
                            "id": "profit_yoy_pct",
                            "name": "Profit YoY %",
                            "operation": "period_change",
                            "table_id": profit.table_id,
                            "base_measure_id": profit.id,
                            "period": "year",
                        }
                    ]
                }
            )

    service.planner.model = OmitsDateModel()  # type: ignore[assignment]
    revised = service.revise_draft(
        draft.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Create a measure for year over year profit growth %" + context,
            idempotency_key="model-yoy-without-date",
            expected_base_hash=draft.base_hash,
        ),
    )

    added = next(item for item in revised.measure_proposal.measures if item.id == "profit_yoy_pct")
    assert type(added) is GovernedMeasureSpec
    assert added.date_column_id == "order_date"
    assert added.base_measure_id == profit.id
    assert added.format_string == "0.0%"
