from __future__ import annotations

from pathlib import Path

import pytest

from ai_powerbi_analyst.contracts import RelationshipCandidate, SourceManifest
from ai_powerbi_analyst.data.onboarding import DataOnboardingService
from ai_powerbi_analyst.studio.contracts import (
    DashboardDraft,
    DashboardPageProposal,
    DashboardVisualProposal,
    DatasetSnapshot,
)
from ai_powerbi_analyst.studio.inventory import inventory_folder
from ai_powerbi_analyst.studio.planner import (
    StudioPlanningError,
    _non_overlapping_layout,
    _validate_pages,
    compile_powerbi_plan,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def _studio_inputs(
    tmp_path: Path,
) -> tuple[StudioService, DatasetSnapshot, SourceManifest, list[RelationshipCandidate]]:
    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    service.planner.model = None
    source_root = tmp_path / "selected"
    source_root.mkdir()
    (source_root / "orders.csv").write_text(
        "Order ID,Order Date,Region,Sales,Profit\n"
        "A-1,2025-01-01,East,100.50,20.25\n"
        "A-2,2025-02-01,West,250.00,40.00\n"
        "A-3,2025-03-01,East,149.50,29.75\n",
        encoding="utf-8",
    )
    snapshot, manifest = service.ingestion.ingest(
        "workspace-test",
        [inventory_folder(source_root)[0]],
    )
    service.store.put(
        "snapshot",
        snapshot,
        workspace_id="workspace-test",
        expected_base_hash=EMPTY_HASH,
        idempotency_key="seed-layout-snapshot",
        mutation_payload={"snapshot": snapshot.id},
    )
    relationships = DataOnboardingService(tmp_path / "relationships").propose_relationships(
        manifest
    )
    return service, snapshot, manifest, relationships


def _overlap_second_visual(pages: list[DashboardPageProposal]) -> list[DashboardPageProposal]:
    target = next(page for page in pages if len(page.visuals) >= 2)
    first, second, *remaining = target.visuals
    # Leave vertical room for the intentional collision. Small datasets now retain
    # Sales/Profit KPIs, so the default full-height chart row would make this
    # fixed-column fixture impossible to pack (covered separately by overflow tests).
    remaining = [
        visual.model_copy(update={"height": min(visual.height, 8)}) for visual in remaining
    ]
    overlapping = target.model_copy(
        update={
            "visuals": [
                first,
                second.model_copy(update={"x": first.x, "y": first.y}),
                *remaining,
            ]
        }
    )
    return [overlapping if page.id == target.id else page for page in pages]


def _assert_non_overlapping(pages: list[DashboardPageProposal]) -> None:
    for page in pages:
        for index, visual in enumerate(page.visuals):
            for other in page.visuals[index + 1 :]:
                assert (
                    visual.x + visual.width <= other.x
                    or other.x + other.width <= visual.x
                    or visual.y + visual.height <= other.y
                    or other.y + other.height <= visual.y
                ), f"Visuals overlap on {page.title}: {visual.id} and {other.id}"


def test_layout_compacts_leading_rows_after_resolving_collisions() -> None:
    first = DashboardVisualProposal(
        id="first_kpi",
        recipe="kpi",
        title="First KPI",
        visual_type="cardVisual",
        table_id="orders",
        measure_id="total_sales",
        x=0,
        y=2,
        width=4,
        height=2,
    )
    second = first.model_copy(update={"id": "second_kpi", "title": "Second KPI"})

    normalized = _non_overlapping_layout([first, second])

    assert [visual.y for visual in normalized] == [0, 2]
    assert [first.y, second.y] == [2, 2]


@pytest.mark.parametrize(
    ("recipe", "visual_type", "expected"),
    [
        ("kpi", "cardVisual", (8, 10, 4, 2)),
        ("trend", "lineChart", (6, 7, 6, 5)),
        ("category", "clusteredColumnChart", (8, 7, 4, 5)),
        ("detail", "tableEx", (4, 6, 8, 6)),
    ],
)
def test_layout_clamps_to_grid_and_enforces_readability_minima(
    recipe: str,
    visual_type: str,
    expected: tuple[int, int, int, int],
) -> None:
    anchor = DashboardVisualProposal(
        id="anchor_kpi",
        recipe="kpi",
        title="Anchor KPI",
        visual_type="cardVisual",
        table_id="orders",
        measure_id="total_sales",
        x=0,
        y=0,
        width=4,
        height=2,
    )
    unreadable = DashboardVisualProposal(
        id=f"{recipe}_visual",
        recipe=recipe,  # type: ignore[arg-type]
        title=f"{recipe.title()} visual",
        visual_type=visual_type,  # type: ignore[arg-type]
        table_id="orders",
        measure_id="total_sales",
        x=12,
        y=12,
        width=1,
        height=1,
    )

    normalized = _non_overlapping_layout([anchor, unreadable])

    visual = normalized[1]
    assert (visual.x, visual.y, visual.width, visual.height) == expected
    assert normalized[0] == anchor


def test_layout_applies_minima_before_resolving_new_collision() -> None:
    first = DashboardVisualProposal(
        id="first_kpi",
        recipe="kpi",
        title="First KPI",
        visual_type="cardVisual",
        table_id="orders",
        measure_id="total_sales",
        x=0,
        y=0,
        width=1,
        height=1,
    )
    second = first.model_copy(update={"id": "second_kpi", "title": "Second KPI", "x": 3})

    normalized = _non_overlapping_layout([first, second])

    assert [(visual.x, visual.y, visual.width, visual.height) for visual in normalized] == [
        (0, 0, 4, 2),
        (3, 2, 4, 2),
    ]


def test_layout_preserves_current_readable_page_geometries() -> None:
    kpi = DashboardVisualProposal(
        id="overview_kpi",
        recipe="kpi",
        title="Overview KPI",
        visual_type="cardVisual",
        table_id="orders",
        measure_id="total_sales",
        x=0,
        y=0,
        width=4,
        height=2,
    )
    overview = [
        kpi,
        kpi.model_copy(update={"id": "overview_kpi_2", "x": 4}),
        DashboardVisualProposal(
            id="overview_trend",
            recipe="trend",
            title="Overview trend",
            visual_type="lineChart",
            table_id="orders",
            measure_id="total_sales",
            category_column_id="order_date",
            x=0,
            y=2,
            width=8,
            height=5,
        ),
        DashboardVisualProposal(
            id="overview_category",
            recipe="category",
            title="Overview category",
            visual_type="clusteredColumnChart",
            table_id="orders",
            measure_id="total_sales",
            category_column_id="region",
            x=8,
            y=2,
            width=4,
            height=5,
        ),
    ]
    trends = [
        overview[2].model_copy(update={"id": "driver_trend", "y": 0, "width": 12}),
        overview[3].model_copy(update={"id": "driver_category", "x": 0, "y": 5, "width": 12}),
    ]
    detail = [
        DashboardVisualProposal(
            id="detail_table",
            recipe="detail",
            title="Order detail",
            visual_type="tableEx",
            table_id="orders",
            detail_column_ids=["order_id"],
            x=0,
            y=0,
            width=12,
            height=10,
        )
    ]

    for visuals in (overview, trends, detail):
        assert _non_overlapping_layout(visuals) == visuals


def test_layout_reflows_upward_before_rejecting_a_page_that_fits() -> None:
    anchor = DashboardVisualProposal(
        id="anchor_kpi",
        recipe="kpi",
        title="Anchor KPI",
        visual_type="cardVisual",
        table_id="orders",
        measure_id="total_sales",
        x=0,
        y=0,
        width=4,
        height=2,
    )
    detail = DashboardVisualProposal(
        id="first_detail",
        recipe="detail",
        title="First detail",
        visual_type="tableEx",
        table_id="orders",
        detail_column_ids=["order_id"],
        x=4,
        y=6,
        width=8,
        height=6,
    )
    overlapping_detail = detail.model_copy(update={"id": "second_detail", "title": "Second detail"})

    normalized = _non_overlapping_layout([anchor, detail, overlapping_detail])

    assert [(visual.id, visual.y) for visual in normalized] == [
        ("anchor_kpi", 0),
        ("first_detail", 6),
        ("second_detail", 0),
    ]


def test_layout_rejects_only_when_minimum_visuals_cannot_fit() -> None:
    details = [
        DashboardVisualProposal(
            id=f"detail_{index}",
            recipe="detail",
            title=f"Detail {index}",
            visual_type="tableEx",
            table_id="orders",
            detail_column_ids=["order_id"],
            x=0,
            y=0,
            width=8,
            height=6,
        )
        for index in range(3)
    ]

    with pytest.raises(StudioPlanningError, match="detail_2"):
        _non_overlapping_layout(details)


def test_create_draft_normalizes_pages_before_generating_previews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, snapshot, manifest, relationships = _studio_inputs(tmp_path)
    seed = service.planner.create_draft(
        "workspace-test",
        snapshot,
        manifest,
        relationships,
        "Show sales and profit trends by region",
    )
    proposed_pages = _overlap_second_visual(seed.pages)
    target = next(page for page in proposed_pages if len(page.visuals) >= 2)
    first, second = target.visuals[:2]
    assert (second.x, second.y) == (first.x, first.y)

    monkeypatch.setattr(service.planner, "_pages", lambda *_args: proposed_pages)
    generated_preview_pages: list[DashboardPageProposal] = []
    original_previews = service.planner._previews

    def capture_previews(*args: object) -> object:
        pages = args[-1]
        assert isinstance(pages, list)
        generated_preview_pages.extend(pages)
        return original_previews(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(service.planner, "_previews", capture_previews)

    draft = service.planner.create_draft(
        "workspace-test",
        snapshot,
        manifest,
        relationships,
        "Show sales and profit trends by region",
    )

    normalized_target = next(page for page in draft.pages if page.id == target.id)
    normalized_first, normalized_second = normalized_target.visuals[:2]
    assert normalized_second.y == normalized_first.y + normalized_first.height
    assert generated_preview_pages == draft.pages
    _assert_non_overlapping(draft.pages)


@pytest.mark.parametrize(
    "visual_type",
    [
        "clusteredColumnChart",
        "clusteredBarChart",
        "barChart",
        "columnChart",
        "pieChart",
        "donutChart",
        "funnel",
        "waterfallChart",
    ],
)
def test_catalog_verified_category_visuals_compile_with_category_and_y_roles(
    tmp_path: Path,
    visual_type: str,
) -> None:
    service, snapshot, manifest, relationships = _studio_inputs(tmp_path)
    draft = service.planner.create_draft(
        "workspace-test",
        snapshot,
        manifest,
        relationships,
        "Show sales by region",
    )
    target_page = next(
        page for page in draft.pages if any(item.recipe == "category" for item in page.visuals)
    )
    target = next(item for item in target_page.visuals if item.recipe == "category")
    changed_page = target_page.model_copy(
        update={
            "visuals": [
                item.model_copy(update={"visual_type": visual_type})
                if item.id == target.id
                else item
                for item in target_page.visuals
            ]
        }
    )
    pages = _validate_pages(
        [changed_page if page.id == target_page.id else page for page in draft.pages],
        manifest,
        draft.measure_proposal,
    )
    approved = draft.model_copy(
        update={
            "pages": pages,
            "semantic_model": draft.semantic_model.model_copy(update={"approved": True}),
            "status": "approved",
        }
    )

    plan = compile_powerbi_plan(approved, manifest)
    compiled = next(
        visual for page in plan.pages for visual in page.visuals if visual.id == target.id
    )

    assert compiled.visual_type == visual_type
    assert {binding.role for binding in compiled.bindings} == {"Category", "Y"}


@pytest.mark.parametrize("visual_type", ["areaChart", "stackedAreaChart"])
def test_catalog_verified_trend_visuals_compile(
    tmp_path: Path,
    visual_type: str,
) -> None:
    service, snapshot, manifest, relationships = _studio_inputs(tmp_path)
    draft = service.planner.create_draft(
        "workspace-test",
        snapshot,
        manifest,
        relationships,
        "Show sales over time",
    )
    target_page = next(
        page for page in draft.pages if any(item.recipe == "trend" for item in page.visuals)
    )
    target = next(item for item in target_page.visuals if item.recipe == "trend")
    changed_page = target_page.model_copy(
        update={
            "visuals": [
                item.model_copy(update={"visual_type": visual_type})
                if item.id == target.id
                else item
                for item in target_page.visuals
            ]
        }
    )

    validated = _validate_pages(
        [changed_page if page.id == target_page.id else page for page in draft.pages],
        manifest,
        draft.measure_proposal,
    )

    assert (
        next(
            item for page in validated for item in page.visuals if item.id == target.id
        ).visual_type
        == visual_type
    )


def test_draft_read_normalizes_legacy_layout_without_rewriting_stored_draft(
    tmp_path: Path,
) -> None:
    service, snapshot, manifest, relationships = _studio_inputs(tmp_path)
    draft = service.planner.create_draft(
        "workspace-test",
        snapshot,
        manifest,
        relationships,
        "Show sales and profit trends by region",
    )
    legacy_draft = draft.model_copy(update={"pages": _overlap_second_visual(draft.pages)})
    detail_visual_id = next(
        visual.id
        for page in legacy_draft.pages
        for visual in page.visuals
        if visual.recipe == "detail"
    )
    detail_preview = next(
        preview for preview in legacy_draft.previews if preview.visual_id == detail_visual_id
    )
    assert detail_preview.query_plan_hash is not None
    assert detail_preview.query_result_hash is not None
    service.store.put(
        "draft",
        legacy_draft,
        workspace_id="workspace-test",
        expected_base_hash=EMPTY_HASH,
        idempotency_key="seed-legacy-layout-draft",
        mutation_payload={"draft": legacy_draft.id},
    )

    normalized = service.draft(legacy_draft.id)

    assert normalized.pages != legacy_draft.pages
    assert normalized.previews == legacy_draft.previews
    assert normalized.base_hash == legacy_draft.base_hash
    assert normalized.status == legacy_draft.status
    assert normalized.created_at == legacy_draft.created_at
    assert normalized.updated_at == legacy_draft.updated_at
    _assert_non_overlapping(normalized.pages)
    stored = service.store.get("draft", legacy_draft.id, DashboardDraft)
    assert stored == legacy_draft
