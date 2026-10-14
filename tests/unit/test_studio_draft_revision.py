from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ai_powerbi_analyst.contracts import DslMeasureSpec
from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import (
    AdvancedDaxProposal,
    ApproveDraftRequest,
    ChatRequest,
    ConversationContext,
    CreateWorkspaceRequest,
    DraftRequest,
    DraftRevisionProposal,
    GovernedMeasureSpec,
    IngestRequest,
    PageVisualAddition,
    ReviseDraftRequest,
)
from ai_powerbi_analyst.studio.planner import (
    StudioPlanningError,
    StudioPlanningService,
    _merge_revision,
    compile_powerbi_plan,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError


def _approved_draft(tmp_path: Path):
    source_root = tmp_path / "selected"
    source_root.mkdir()
    source = source_root / "orders.csv"
    source.write_text(
        "Order ID,Order Date,Region,Segment,Sales,Profit\n"
        "A-1,2025-01-01,East,Consumer,100.00,20.00\n"
        "A-2,2025-02-01,West,Corporate,250.00,40.00\n"
        "A-3,2025-03-01,East,Consumer,150.00,30.00\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Governed revision",
            selected_folder=str(source_root),
            idempotency_key="create-revision-workspace",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="ingest-revision-source",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Show sales and profit trends by region",
            idempotency_key="create-revision-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    approved = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[],
            idempotency_key="approve-revision-draft",
            expected_base_hash=draft.base_hash,
        ),
    )
    return service, service.workspace(workspace.id), snapshot, approved


def _margin_request(workspace_id: str, base_hash: str) -> ReviseDraftRequest:
    return ReviseDraftRequest(
        workspace_id=workspace_id,
        prompt=(
            "Add a Profit Margin % formula and a new Margin Analysis page with a monthly "
            "trend and a breakdown by Segment."
        ),
        idempotency_key="revise-profit-margin",
        expected_base_hash=base_hash,
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "on detail page, add appropriate visual blow the table",
        "Add a bar chart of Sales by Region below the table on the Detail page",
        "Add a line chart to the Detail page",
    ],
)
def test_add_visual_targets_existing_page_with_real_preview(tmp_path, prompt):
    service, workspace, snapshot, approved = _approved_draft(tmp_path)
    request = ReviseDraftRequest(
        workspace_id=workspace.id,
        prompt=prompt,
        idempotency_key="detail-visual",
        expected_base_hash=approved.base_hash,
    )
    revised = service.revise_draft(approved.id, request)
    detail = next(p for p in revised.pages if p.id == "detail")
    original = next(p for p in approved.pages if p.id == "detail")
    assert len(revised.pages) == len(approved.pages)
    assert len(detail.visuals) == len(original.visuals) + 1
    assert [p for p in revised.pages if p.id != "detail"] == [
        p for p in approved.pages if p.id != "detail"
    ]
    table, chart = detail.visuals
    assert table.detail_column_ids == original.visuals[0].detail_column_ids
    assert table.height >= 6 and chart.y >= table.y + table.height
    assert chart.y + chart.height <= 12
    preview = next(p for p in revised.previews if p.visual_id == chart.id)
    assert preview.rows and sum(row["value"] for row in preview.rows) == pytest.approx(500)
    assert preview.query_result_hash
    assert revised.status == "draft" and not revised.semantic_model.approved
    assert service.revise_draft(approved.id, request) == revised
    assert not service.builds(workspace.id)
    manifest = service.ingestion.load_manifest(snapshot)
    reviewed = revised.model_copy(
        update={
            "status": "approved",
            "semantic_model": revised.semantic_model.model_copy(update={"approved": True}),
        }
    )
    plan = compile_powerbi_plan(reviewed, manifest)
    assert len(plan.pages) == len(revised.pages)
    assert len(plan.pages[2].visuals) == 2


@pytest.mark.parametrize(
    "prompt",
    [
        "Add a visual to the Missing page",
        "Add a visual to Overview and Detail pages",
        "Add a visual below the table",
        "Add a visual above the table on the Detail page",
    ],
)
def test_existing_page_visual_rejects_ambiguous_or_unsupported_target(tmp_path, prompt):
    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    with pytest.raises(StudioPlanningError):
        service.revise_draft(
            approved.id,
            ReviseDraftRequest(
                workspace_id=workspace.id,
                prompt=prompt,
                idempotency_key="invalid-visual",
                expected_base_hash=approved.base_hash,
            ),
        )
    assert service.draft(approved.id).base_hash == approved.base_hash


def test_visual_patch_rejects_invented_page_field_and_full_layout(tmp_path):
    service, _workspace, snapshot, approved = _approved_draft(tmp_path)
    manifest = service.ingestion.load_manifest(snapshot)
    visual = approved.pages[0].visuals[-1].model_copy(update={"id": "new_chart"})
    for page_id, candidate in [
        ("invented", visual),
        ("detail", visual.model_copy(update={"category_column_id": "invented"})),
    ]:
        with pytest.raises(StudioPlanningError):
            _merge_revision(
                approved,
                DraftRevisionProposal(
                    visual_additions=[PageVisualAddition(page_id=page_id, visual=candidate)]
                ),
                manifest,
            )
    detail = approved.pages[2]
    crowded = detail.model_copy(
        update={
            "visuals": [
                detail.visuals[0].model_copy(update={"height": 6}),
                visual.model_copy(update={"y": 6, "height": 5}),
            ]
        }
    )
    roomy = approved.pages[0].model_copy(update={"visuals": approved.pages[0].visuals[:1]})
    full = approved.model_copy(update={"pages": [roomy, approved.pages[1], crowded]})
    with pytest.raises(StudioPlanningError, match="has no room") as error:
        _merge_revision(
            full,
            DraftRevisionProposal(
                visual_additions=[
                    PageVisualAddition(
                        page_id="detail", visual=visual.model_copy(update={"id": "another"})
                    )
                ]
            ),
            manifest,
        )
    assert "'Detail'" in str(error.value)
    assert "Pages with room for it: Overview" in str(error.value)


def test_chat_routes_page_first_visual_command(tmp_path):
    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    result = list(
        service.chat_events(
            ChatRequest(
                workspace_id=workspace.id,
                message="on detail page, add appropriate visual blow the table",
                idempotency_key="chat-detail",
                expected_base_hash=approved.base_hash,
            ),
            Event(),
        )
    )
    assert any(event["event"] == "draft" for event in result)
    assert len(service.draft(approved.id).pages[2].visuals) == 2


def test_chat_full_page_refusal_is_persisted_as_an_answer(tmp_path):
    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    request = ChatRequest(
        workspace_id=workspace.id,
        message="Add a chart to the Trends & Drivers page",
        idempotency_key="chat-full-page",
        expected_base_hash=approved.base_hash,
    )
    result = list(service.chat_events(request, Event()))
    assert not any(event["event"] == "draft" for event in result)
    text = result[-1]["data"]["text"]
    assert "couldn't apply" in text
    assert "Trends & Drivers" in text
    assert "No existing page has room" in text
    messages = service.messages(workspace.id)
    assert messages[-2]["content"] == request.message
    assert "couldn't apply" in messages[-1]["content"]
    assert service.draft(approved.id).base_hash == approved.base_hash


def test_chat_builds_real_margin_page_and_replays_without_duplication(tmp_path):
    service, workspace, snapshot, approved = _approved_draft(tmp_path)
    service.store.add_message(
        workspace.id, "user", "Create a Profit Margin formula and page by Segment"
    )
    request = ChatRequest(
        workspace_id=workspace.id,
        message="build it",
        idempotency_key="chat-margin",
        expected_base_hash=approved.base_hash,
    )
    result = list(service.chat_events(request, Event()))
    assert any(e["event"] == "draft" for e in result)
    updated = service.draft(approved.id)
    assert len(updated.pages) == len(approved.pages) + 1
    assert updated.status == "draft"
    count = len(service.messages(workspace.id))
    list(service.chat_events(request, Event()))
    assert len(service.messages(workspace.id)) == count
    assert len(service.draft(approved.id).pages) == len(updated.pages)
    assert not service.builds(workspace.id)


def test_chat_lod_clarifies_then_builds_fixed_grain_measure_and_preview(tmp_path):
    service, workspace, snapshot, approved = _approved_draft(tmp_path)
    service.store.add_message(workspace.id, "user", "Can you do LOD calculations?")
    vague = ChatRequest(
        workspace_id=workspace.id,
        message="build a page around this",
        idempotency_key="lod-vague",
        expected_base_hash=approved.base_hash,
    )
    result = list(service.chat_events(vague, Event()))
    assert "Before I create the LOD" in result[-1]["data"]["text"]
    assert service.draft(approved.id).base_hash == approved.base_hash
    specific = ChatRequest(
        workspace_id=workspace.id,
        message="Total Sales fixed by Region, ignore other filters, and add a page",
        idempotency_key="lod-specific",
        expected_base_hash=approved.base_hash,
    )
    result = list(service.chat_events(specific, Event()))
    assert any(e["event"] == "draft" for e in result)
    updated = service.draft(approved.id)
    measure = updated.measure_proposal.measures[-1]
    assert isinstance(measure, AdvancedDaxProposal)
    assert measure.expression == "CALCULATE([Total Sales], ALLEXCEPT('orders', 'orders'[Region]))"
    assert not measure.approved and updated.status == "draft"
    page = updated.pages[-1]
    assert all(v.measure_id == measure.id for v in page.visuals)
    previews = {p.visual_id: p for p in updated.previews}
    assert previews[page.visuals[0].id].rows[0]["value"] == 500
    assert {r["group_1"]: r["value"] for r in previews[page.visuals[1].id].rows} == {
        "East": 250,
        "West": 250,
    }
    with pytest.raises(StateConflictError, match="separate approval"):
        service.approve_draft(
            updated.id,
            ApproveDraftRequest(
                workspace_id=workspace.id,
                approved_relationship_ids=[],
                idempotency_key="no-dax-bypass",
                expected_base_hash=updated.base_hash,
            ),
        )
    assert not service.builds(workspace.id)
    # Plain clarification replies also replay correctly after the pending state is gone.
    count = len(service.messages(workspace.id))
    list(service.chat_events(specific, Event()))
    assert len(service.messages(workspace.id)) == count


def test_chat_rejects_stale_and_cancelled_draft_mutations(tmp_path):
    service, workspace, snapshot, approved = _approved_draft(tmp_path)
    stale = ChatRequest(
        workspace_id=workspace.id,
        message="Add a margin page",
        idempotency_key="stale-chat",
        expected_base_hash="0" * 64,
    )
    with pytest.raises(StateConflictError):
        list(service.chat_events(stale, Event()))
    cancel = Event()
    cancel.set()
    valid = stale.model_copy(
        update={"expected_base_hash": approved.base_hash, "idempotency_key": "cancel-chat"}
    )
    list(service.chat_events(valid, cancel))
    assert service.draft(approved.id).base_hash == approved.base_hash
    assert not service.messages(workspace.id)


def test_revision_adds_source_backed_margin_page_and_resets_approval(
    tmp_path: Path,
) -> None:
    service, workspace, snapshot, approved = _approved_draft(tmp_path)
    request = _margin_request(workspace.id, approved.base_hash)

    revised = service.revise_draft(approved.id, request)

    assert revised.id == approved.id
    assert revised.workspace_id == approved.workspace_id
    assert revised.snapshot_id == approved.snapshot_id
    assert revised.understanding == approved.understanding
    assert revised.semantic_model.tables == approved.semantic_model.tables
    assert revised.status == "draft"
    assert not revised.semantic_model.approved
    assert all(
        relationship.review_status == "pending"
        for relationship in revised.semantic_model.relationships
    )
    assert revised.base_hash != approved.base_hash
    assert len(revised.pages) == len(approved.pages) + 1

    margin = next(
        measure
        for measure in revised.measure_proposal.measures
        if measure.id == "profit_margin_pct"
    )
    assert margin.operation == "margin"
    assert margin.numerator_measure_id is not None
    assert margin.denominator_measure_id is not None
    assert margin.format_string == "0.0%"

    page = next(page for page in revised.pages if page.title == "Margin Analysis")
    segment = next(
        column
        for table in snapshot.tables
        for column in table.table.columns
        if column.name == "Segment"
    )
    category = next(visual for visual in page.visuals if visual.recipe == "category")
    assert category.category_column_id == segment.id
    margin_kpi = next(visual for visual in page.visuals if visual.recipe == "kpi")
    preview = next(item for item in revised.previews if item.visual_id == margin_kpi.id)
    assert preview.rows == [{"value": pytest.approx(0.18)}]
    assert preview.query_plan_hash is not None
    assert preview.query_result_hash is not None
    assert len(revised.previews) == sum(len(item.visuals) for item in revised.pages)

    manifest = service.ingestion.load_manifest(snapshot)
    approved_revision = revised.model_copy(
        update={
            "status": "approved",
            "semantic_model": revised.semantic_model.model_copy(update={"approved": True}),
        }
    )
    powerbi = compile_powerbi_plan(approved_revision, manifest)
    compiled_margin = next(
        item
        for item in powerbi.semantic_model.measures
        if isinstance(item, DslMeasureSpec) and item.id == margin.id
    )
    assert compiled_margin.numerator_measure_id == margin.numerator_measure_id
    assert compiled_margin.denominator_measure_id == margin.denominator_measure_id
    assert any(item.title == "Margin Analysis" for item in powerbi.pages)


def test_revision_is_idempotent_and_rejects_stale_or_reused_keys(tmp_path: Path) -> None:
    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    request = _margin_request(workspace.id, approved.base_hash)
    revised = service.revise_draft(approved.id, request)
    event_count = len(service.events(workspace.id))

    replayed = service.revise_draft(approved.id, request)

    assert replayed == revised
    assert len(service.events(workspace.id)) == event_count
    with pytest.raises(StateConflictError, match="Idempotency key"):
        service.revise_draft(
            approved.id,
            request.model_copy(update={"prompt": "Add a different page"}),
        )
    with pytest.raises(StateConflictError, match="Stale draft base hash"):
        service.revise_draft(
            approved.id,
            request.model_copy(update={"idempotency_key": "stale-revision-key"}),
        )


def test_invalid_local_model_revision_uses_the_grounded_fallback(tmp_path: Path) -> None:
    service, workspace, _snapshot, approved = _approved_draft(tmp_path)

    class InventingModel:
        def health(self):
            return SimpleNamespace(model_available=True)

        def generate(self, *_args, **_kwargs):
            return DraftRevisionProposal(
                measures=[
                    GovernedMeasureSpec(
                        id="profit_margin_pct",
                        name="Profit Margin %",
                        operation="margin",
                        table_id="invented_table",
                        numerator_measure_id="invented_profit",
                        denominator_measure_id="invented_sales",
                        format_string="0.0%",
                    )
                ]
            )

    service.planner.model = InventingModel()  # type: ignore[assignment]

    revised = service.revise_draft(
        approved.id,
        _margin_request(workspace.id, approved.base_hash),
    )

    margin = next(
        item for item in revised.measure_proposal.measures if item.id == "profit_margin_pct"
    )
    assert margin.table_id != "invented_table"
    assert margin.numerator_measure_id != "invented_profit"
    assert next(page for page in revised.pages if page.title == "Margin Analysis")


def test_revision_api_is_closed_and_maps_unactionable_prompts_to_422(
    tmp_path: Path,
) -> None:
    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    token = "revision-test-token"
    client = TestClient(create_app(service, token))
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "workspace_id": workspace.id,
        "prompt": "Change the colors",
        "idempotency_key": "unactionable-revision",
        "expected_base_hash": approved.base_hash,
    }

    response = client.post(
        f"/v1/drafts/{approved.id}/revisions",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 422
    assert "measure/formula or a new page" in response.json()["detail"]

    response = client.post(
        f"/v1/drafts/{approved.id}/revisions",
        json={**payload, "unexpected": True},
        headers=headers,
    )
    assert response.status_code == 422


def test_margin_page_reuses_a_lab_saved_profit_margin_measure(tmp_path: Path) -> None:
    from ai_powerbi_analyst.studio.contracts import AddDaxMeasureRequest

    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    with_lab_measure = service.add_dax_measure(
        workspace.id,
        AddDaxMeasureRequest(
            workspace_id=workspace.id,
            name="Profit Margin %",
            # Pasted from chat in Power BI's "Name = expression" form.
            expression="Profit Margin % = DIVIDE(SUM('orders'[Profit]), SUM('orders'[Sales]))",
            idempotency_key="lab-profit-margin",
            expected_base_hash=approved.base_hash,
        ),
    )
    lab = next(m for m in with_lab_measure.measure_proposal.measures if m.name == "Profit Margin %")
    assert lab.expression == "DIVIDE(SUM('orders'[Profit]), SUM('orders'[Sales]))"
    assert lab.format_string == "0.0%"

    revised = service.revise_draft(
        with_lab_measure.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Add a new page showing Profit Margin % by Region",
            idempotency_key="revise-reuse-lab-margin",
            expected_base_hash=with_lab_measure.base_hash,
        ),
    )

    names = [m.name.casefold() for m in revised.measure_proposal.measures]
    assert names.count("profit margin %") == 1
    new_page = revised.pages[-1]
    assert any(visual.measure_id == lab.id for visual in new_page.visuals)


def test_margin_page_groups_by_a_named_category_in_a_related_dimension(tmp_path: Path) -> None:
    source_root = tmp_path / "star"
    source_root.mkdir()
    (source_root / "Sales.csv").write_text(
        "Order ID,Order Date,ProductKey,Region,Sales,Profit\n"
        "A-1,2025-01-01,101,East,100.00,20.00\n"
        "A-2,2025-02-01,102,West,250.00,40.00\n"
        "A-3,2025-03-01,101,East,150.00,30.00\n",
        encoding="utf-8",
    )
    (source_root / "Products.csv").write_text(
        "ProductKey,Product,Category\n101,Pulse,Devices\n102,Atlas,Infrastructure\n",
        encoding="utf-8",
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Star revision",
            selected_folder=str(source_root),
            idempotency_key="create-star",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source_root / "Sales.csv"), str(source_root / "Products.csv")],
            idempotency_key="ingest-star",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard",
            idempotency_key="draft-star",
            expected_base_hash=workspace.base_hash,
        )
    )
    products = next(t.table.id for t in snapshot.tables if t.table.name == "Products")

    revised = service.revise_draft(
        draft.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Add a new page showing Profit Margin % by Category",
            idempotency_key="revise-star-category",
            expected_base_hash=draft.base_hash,
        ),
    )

    chart = next(v for v in revised.pages[-1].visuals if v.recipe == "category")
    assert chart.title.endswith("by Category")
    assert chart.table_id == products and chart.category_column_id == "category"


def test_lab_saved_ratio_measures_preview_through_the_governed_path(tmp_path: Path) -> None:
    from ai_powerbi_analyst.studio.contracts import AddDaxMeasureRequest

    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    saved = service.add_dax_measure(
        workspace.id,
        AddDaxMeasureRequest(
            workspace_id=workspace.id,
            name="Profit Margin %",
            expression="DIVIDE(SUM('orders'[Profit]), SUM('orders'[Sales]))",
            idempotency_key="lab-margin-preview",
            expected_base_hash=approved.base_hash,
        ),
    )
    revised = service.revise_draft(
        saved.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Add a new page showing Profit Margin % by Region",
            idempotency_key="revise-margin-preview",
            expected_base_hash=saved.base_hash,
        ),
    )

    previews = {item.visual_id: item for item in revised.previews}
    for visual in revised.pages[-1].visuals:
        preview = previews[visual.id]
        assert preview.empty_reason is None, (visual.id, preview.empty_reason)
        assert preview.rows
    kpi = next(v for v in revised.pages[-1].visuals if v.recipe == "kpi")
    # 90 profit / 500 sales across the fixture rows.
    assert previews[kpi.id].rows[0]["value"] == pytest.approx(0.18)
    stored = next(m for m in revised.measure_proposal.measures if m.name == "Profit Margin %")
    assert stored.tier == "advanced_dax" and not stored.approved


def test_a_second_margin_page_gets_a_distinct_title(tmp_path: Path) -> None:
    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    first = service.revise_draft(
        approved.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Add a new page showing Profit Margin % by Region",
            idempotency_key="margin-page-one",
            expected_base_hash=approved.base_hash,
        ),
    )
    second = service.revise_draft(
        first.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Add a new page showing Profit Margin % by Segment",
            idempotency_key="margin-page-two",
            expected_base_hash=first.base_hash,
        ),
    )

    assert [page.title for page in second.pages][-2:] == ["Margin Analysis", "Margin Analysis 2"]
    chart = next(v for v in second.pages[-1].visuals if v.recipe == "category")
    assert chart.title.endswith("by Segment")


def test_lookup_tables_are_dimensions_even_with_numeric_columns(tmp_path: Path) -> None:
    source_root = tmp_path / "roles"
    source_root.mkdir()
    (source_root / "Sales.csv").write_text(
        "Order ID,ProductKey,Sales\nA-1,101,100\nA-2,102,250\nA-3,101,150\n", encoding="utf-8"
    )
    (source_root / "Products.csv").write_text(
        "ProductKey,Product,List Price\n101,Pulse,145\n102,Atlas,420\n", encoding="utf-8"
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Roles",
            selected_folder=str(source_root),
            idempotency_key="create-roles",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source_root / "Sales.csv"), str(source_root / "Products.csv")],
            idempotency_key="ingest-roles",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard",
            idempotency_key="draft-roles",
            expected_base_hash=workspace.base_hash,
        )
    )

    roles = {table.display_name: table.role for table in draft.semantic_model.tables}
    assert roles == {"Sales": "fact", "Products": "dimension"}
    hidden = {table.display_name: table.hidden_column_ids for table in draft.semantic_model.tables}
    assert "productkey" in hidden["Sales"]

    from ai_powerbi_analyst.studio.planner import unhide_unbuilt_foreign_keys

    def sales_hidden(semantic) -> list[str]:
        return next(t.hidden_column_ids for t in semantic.tables if t.display_name == "Sales")

    # Excluded at review: the key is the only way left to slice Sales by product.
    assert "productkey" not in sales_hidden(unhide_unbuilt_foreign_keys(draft.semantic_model))
    approved = draft.semantic_model.model_copy(
        update={
            "relationships": [
                item.model_copy(update={"review_status": "approved"})
                for item in draft.semantic_model.relationships
            ]
        }
    )
    assert "productkey" in sales_hidden(unhide_unbuilt_foreign_keys(approved))


def test_subset_list_key_stays_visible_on_the_fact(tmp_path: Path) -> None:
    """Returns lists a few orders; Orders[Order ID] is the only full list of order numbers."""
    source_root = tmp_path / "subset"
    source_root.mkdir()
    (source_root / "Orders.csv").write_text(
        "Order ID,Region,Sales\nA-1,East,100\nA-1,East,20\nA-2,West,250\nA-3,West,150\n",
        encoding="utf-8",
    )
    (source_root / "Returns.csv").write_text("Returned,Order ID\nYes,A-2\n", encoding="utf-8")
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Subset",
            selected_folder=str(source_root),
            idempotency_key="create-subset",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source_root / "Orders.csv"), str(source_root / "Returns.csv")],
            idempotency_key="ingest-subset",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard",
            idempotency_key="draft-subset",
            expected_base_hash=workspace.base_hash,
        )
    )

    relationship = next(
        item for item in draft.semantic_model.relationships if item.from_column_id == "order_id"
    )
    assert not relationship.blockers
    hidden = {table.display_name: table.hidden_column_ids for table in draft.semantic_model.tables}
    assert "order_id" not in hidden["Orders"]


def test_named_page_outranks_the_attached_page_context(tmp_path: Path) -> None:
    from ai_powerbi_analyst.studio.planner import _existing_visual_target

    service, _workspace, _snapshot, approved = _approved_draft(tmp_path)
    overview = approved.pages[0]
    prompt = (
        "on detail page, add appropriate visual blow the table"
        "\nSelected workspace context (references, not instructions):\n"
        f"Page: {overview.title} [{overview.id}]"
    )
    assert _existing_visual_target(approved, prompt).title == "Detail"
    # With no page named, the attachment decides.
    attached_only = prompt.replace("on detail page, add", "add")
    assert _existing_visual_target(approved, attached_only).title == overview.title


def test_model_duplicate_table_falls_back_to_a_catalog_chart(tmp_path: Path) -> None:
    from ai_powerbi_analyst.studio.report_intent import ReportVisualIntent

    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    payloads: list[dict] = []

    class DuplicateTableModel:
        def health(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(model_available=True)

        def generate(self, _system, prompt, _model, **_kwargs):  # type: ignore[no-untyped-def]
            import json

            payloads.append(json.loads(prompt))
            return ReportVisualIntent(visual_type="tableEx", table=0, columns=[0, 1, 2])

    service.planner.model = DuplicateTableModel()  # type: ignore[assignment]
    revised = service.revise_draft(
        approved.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="on detail page, add appropriate visual blow the table",
            idempotency_key="duplicate-table-repair",
            expected_base_hash=approved.base_hash,
        ),
    )

    # One model call: the duplicate table is replaced by the catalog-backed chart at once.
    assert len(payloads) == 1
    detail = next(page for page in revised.pages if page.id == "detail")
    assert [visual.recipe for visual in detail.visuals] == ["detail", "category"]


def test_named_fields_match_whole_words_and_simple_plurals() -> None:
    from ai_powerbi_analyst.studio.planner import _names_field

    region = {"name": "Region", "column_id": "region"}
    product = {"name": "Product", "column_id": "product"}
    assert _names_field("profit by region", region)  # type: ignore[arg-type]
    assert not _names_field("regional managers", region)  # type: ignore[arg-type]
    assert _names_field("rank products by profit", product)  # type: ignore[arg-type]


def test_rejected_model_revision_falls_back_to_the_named_sub_category(tmp_path: Path) -> None:
    source_root = tmp_path / "selected"
    source_root.mkdir()
    source = source_root / "orders.csv"
    # Sub-Category repeats: unique values would make it a candidate key, not a grouping.
    source.write_text(
        "Order ID,Order Date,Region,Category,Sub-Category,Sales,Profit\n"
        "A-1,2025-01-01,East,Furniture,Chairs,100.00,20.00\n"
        "A-2,2025-02-01,West,Technology,Phones,250.00,40.00\n"
        "A-3,2025-03-01,East,Furniture,Tables,150.00,30.00\n"
        "A-4,2025-03-02,West,Technology,Phones,120.00,10.00\n"
        "A-5,2025-04-01,East,Furniture,Chairs,90.00,15.00\n"
        "A-6,2025-04-02,West,Furniture,Tables,80.00,5.00\n",
        encoding="utf-8",
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Sub-category revision",
            selected_folder=str(source_root),
            idempotency_key="create-sub-category",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="ingest-sub-category",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard",
            idempotency_key="draft-sub-category",
            expected_base_hash=workspace.base_hash,
        )
    )
    calls: list[str] = []

    class TimeoutModel:
        def health(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(model_available=True)

        def generate(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append("generate")
            raise TimeoutError("Generation ended timeout")

    service.planner.model = TimeoutModel()  # type: ignore[assignment]
    revised = service.revise_draft(
        draft.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Add a page showing profit by sub-category",
            idempotency_key="revise-sub-category",
            expected_base_hash=draft.base_hash,
        ),
    )

    # The model timed out; the grounded fallback still honours the named dimension.
    assert calls == ["generate"]
    page = revised.pages[-1]
    chart = next(visual for visual in page.visuals if visual.recipe == "category")
    assert page.title == "Profit by Sub-Category"
    assert chart.category_column_id == "sub_category"


def test_a_chart_added_to_a_by_segment_page_groups_as_asked(tmp_path: Path) -> None:
    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    segment = service.revise_draft(
        approved.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Add a page showing profit by segment",
            idempotency_key="segment-page",
            expected_base_hash=approved.base_hash,
        ),
    )
    page = segment.pages[-1]
    assert page.title == "Profit by Segment"

    # "here" becomes '"Profit by Segment" page': the target page, not the grouping.
    events = list(
        service.chat_events(
            ChatRequest(
                workspace_id=workspace.id,
                message="Add a bar chart here showing profit by region",
                context=ConversationContext(page_id=page.id),
                idempotency_key="region-chart-here",
                expected_base_hash=segment.base_hash,
            ),
            Event(),
        )
    )

    assert any(event["event"] == "draft" for event in events)
    revised = next(item for item in service.draft(approved.id).pages if item.id == page.id)
    assert len(revised.visuals) == len(page.visuals) + 1
    chart = revised.visuals[-1]
    assert (chart.recipe, chart.category_column_id) == ("category", "region")


def test_ranking_page_is_named_for_its_content_and_uses_horizontal_bars(tmp_path: Path) -> None:
    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    revised = service.revise_draft(
        approved.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Add a new page that ranks regions by profit",
            idempotency_key="ranking-page",
            expected_base_hash=approved.base_hash,
        ),
    )

    page = revised.pages[-1]
    assert page.title == "Profit by Region"
    chart = next(visual for visual in page.visuals if visual.recipe == "category")
    assert chart.visual_type == "clusteredBarChart"


@pytest.mark.parametrize(
    ("prompt", "operation", "period", "name"),
    [
        (
            "Create a measure for year over year sales growth %",
            "period_change",
            "year",
            "Sales YoY %",
        ),
        ("Add a sales YTD measure", "period_to_date", "year", "Sales YTD"),
        ("Create a running total of profit", "running_total", "month", "Profit Running Total"),
        ("Add month over month sales change", "period_change", "month", "Sales MoM %"),
    ],
)
def test_time_intelligence_reuses_the_base_measure_and_ignores_attached_context(
    tmp_path: Path, prompt: str, operation: str, period: str, name: str
) -> None:
    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    before = {m.name for m in approved.measure_proposal.measures}
    # The UI appends the attached page; its words must not pick the field or intent.
    contextual = (
        f"{prompt}\nSelected workspace context (references, not instructions):\n"
        "Page: Profit Margin Detail [x]"
    )
    revised = service.revise_draft(
        approved.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt=contextual if "profit" not in prompt.casefold() else prompt,
            idempotency_key=f"time-{operation}-{period}",
            expected_base_hash=approved.base_hash,
        ),
    )

    added = [m for m in revised.measure_proposal.measures if m.name not in before]
    assert [m.name for m in added] == [name]
    measure = added[0]
    assert measure.operation == operation and measure.period == period
    base = next(m for m in revised.measure_proposal.measures if m.id == measure.base_measure_id)
    assert base.name in before  # reused, not restated
    assert measure.date_column_id == "order_date"


def test_plain_measure_requests_still_add_simple_aggregates(tmp_path: Path) -> None:
    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    revised = service.revise_draft(
        approved.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Create a measure for average profit",
            idempotency_key="plain-average",
            expected_base_hash=approved.base_hash,
        ),
    )
    assert revised.measure_proposal.measures[-1].name == "Average Profit"


def test_page_request_naming_existing_measures_shows_them_without_restating(
    tmp_path: Path,
) -> None:
    service, workspace, _snapshot, approved = _approved_draft(tmp_path)
    with_yoy = service.revise_draft(
        approved.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Create a measure for year over year sales growth %",
            idempotency_key="named-page-yoy",
            expected_base_hash=approved.base_hash,
        ),
    )
    with_running = service.revise_draft(
        with_yoy.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Create a running total of profit",
            idempotency_key="named-page-running",
            expected_base_hash=with_yoy.base_hash,
        ),
    )
    measures = {m.name: m.id for m in with_running.measure_proposal.measures}

    # Previously: "A measure named 'Profit YoY %' already exists" and nothing changed.
    revised = service.revise_draft(
        with_running.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Create a new page called Growth Trends showing Sales YoY % and "
            "Profit Running Total over time",
            idempotency_key="named-page-growth",
            expected_base_hash=with_running.base_hash,
        ),
    )

    assert revised.measure_proposal == with_running.measure_proposal
    page = revised.pages[-1]
    assert page.title == "Growth Trends"
    shown = {visual.measure_id for visual in page.visuals}
    assert {measures["Sales YoY %"], measures["Profit Running Total"]} <= shown
    trends = [visual for visual in page.visuals if visual.recipe == "trend"]
    assert {visual.measure_id for visual in trends} == {
        measures["Sales YoY %"],
        measures["Profit Running Total"],
    }
    assert all(visual.y + visual.height <= 12 for visual in page.visuals)


def test_relationship_request_that_changes_nothing_explains_instead_of_resetting(tmp_path):
    source_root = tmp_path / "selected"
    source_root.mkdir()
    (source_root / "customers.csv").write_text(
        "CustomerID,Customer Name,Segment\n1,Ann,Retail\n2,Bo,Retail\n3,Cy,Trade\n",
        encoding="utf-8",
    )
    (source_root / "orders.csv").write_text(
        "OrderID,CustomerID,Qty,Amount\n10,1,2,20\n11,2,1,15\n12,1,3,30\n13,3,2,25\n",
        encoding="utf-8",
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Relationship no-op",
            selected_folder=str(source_root),
            idempotency_key="create-relationship-noop",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(path) for path in sorted(source_root.glob("*.csv"))],
            idempotency_key="ingest-relationship-noop",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Orders by customer segment",
            idempotency_key="draft-relationship-noop",
            expected_base_hash=workspace.base_hash,
        )
    )
    usable = [item.id for item in draft.semantic_model.relationships if not item.blockers]
    assert len(usable) == 1
    approved = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=usable,
            idempotency_key="approve-relationship-noop",
            expected_base_hash=draft.base_hash,
        ),
    )
    with pytest.raises(StudioPlanningError) as raised:
        service.revise_draft(
            approved.id,
            ReviseDraftRequest(
                workspace_id=workspace.id,
                prompt="Add a relationship from Orders CustomerID to Customers CustomerID",
                idempotency_key="revise-relationship-noop",
                expected_base_hash=approved.base_hash,
            ),
        )
    message = str(raised.value)
    assert "No change made" in message
    assert "orders[CustomerID] → customers[CustomerID]" in message
    assert "already approved in the model" in message
    # Approval and its previews stay as they were.
    assert service.draft(approved.id).status == "approved"
