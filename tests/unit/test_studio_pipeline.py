from __future__ import annotations

from pathlib import Path

from ai_powerbi_analyst.data.onboarding import DataOnboardingService
from ai_powerbi_analyst.studio.aggregates import AggregateService
from ai_powerbi_analyst.studio.contracts import PreviewDataset, ReadOnlyAggregatePlan
from ai_powerbi_analyst.studio.ingestion import StudioIngestionService
from ai_powerbi_analyst.studio.inventory import inventory_folder
from ai_powerbi_analyst.studio.planner import StudioPlanningService, compile_powerbi_plan
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def test_csv_to_preview_to_powerbi_plan_is_source_backed(tmp_path: Path) -> None:
    source_root = tmp_path / "selected"
    source_root.mkdir()
    source = source_root / "orders.csv"
    source.write_text(
        "Order Date,Region,Sales,Profit\n"
        "2025-01-01,East,100.50,20.25\n"
        "2025-02-01,West,250.00,40.00\n"
        "2025-03-01,East,149.50,29.75\n",
        encoding="utf-8",
    )
    item = inventory_folder(source_root)[0]
    ingestion = StudioIngestionService(tmp_path / "state")

    snapshot, manifest = ingestion.ingest("workspace-test", [item])

    assert snapshot.original_bytes == source.stat().st_size
    assert snapshot.tables[0].table.row_count == 3
    assert Path(snapshot.tables[0].original_snapshot_path).read_bytes() == source.read_bytes()
    assert Path(snapshot.tables[0].normalized_parquet_path).is_file()

    table = manifest.tables[0]
    sales = next(column for column in table.columns if column.name == "Sales")
    receipt = AggregateService(tmp_path / "spill").execute(
        snapshot,
        ReadOnlyAggregatePlan(
            table_id=table.id,
            operation="sum",
            value_column_id=sales.id,
        ),
    )
    assert receipt.rows == [{"value": 500.0}]

    relationships = DataOnboardingService(tmp_path / "relationships").propose_relationships(
        manifest
    )
    planner = StudioPlanningService(None, AggregateService(tmp_path / "spill"))
    draft = planner.create_draft(
        "workspace-test",
        snapshot,
        manifest,
        relationships,
        "Show sales and profit trends by region",
    )

    assert 1 <= len(draft.pages) <= 3
    assert draft.previews
    approved = draft.model_copy(
        update={
            "status": "approved",
            "semantic_model": draft.semantic_model.model_copy(update={"approved": True}),
        }
    )
    powerbi = compile_powerbi_plan(approved, manifest)
    assert powerbi.approved
    assert powerbi.semantic_model.approved
    assert all(page.visuals for page in powerbi.pages)
    for page in powerbi.pages:
        for index, visual in enumerate(page.visuals):
            for other in page.visuals[index + 1 :]:
                assert (
                    visual.x + visual.width <= other.x
                    or other.x + other.width <= visual.x
                    or visual.y + visual.height <= other.y
                    or other.y + other.height <= visual.y
                ), f"Visuals overlap on {page.title}: {visual.id} and {other.id}"

    drivers = next(page for page in approved.pages if page.id == "trends_drivers")
    overlapping_drivers = drivers.model_copy(
        update={"visuals": [visual.model_copy(update={"y": 0}) for visual in drivers.visuals]}
    )
    overlapping_draft = approved.model_copy(
        update={
            "pages": [
                overlapping_drivers if page.id == "trends_drivers" else page
                for page in approved.pages
            ]
        }
    )
    normalized = compile_powerbi_plan(overlapping_draft, manifest)
    normalized_drivers = next(page for page in normalized.pages if page.id == "trends_drivers")
    first_driver, second_driver = normalized_drivers.visuals
    assert first_driver.y + first_driver.height <= second_driver.y


def test_draft_read_repairs_legacy_detail_count_without_mutating_stored_state(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    service.planner.model = None
    source_root = tmp_path / "selected"
    source_root.mkdir()
    source = source_root / "orders.csv"
    source.write_text(
        "Order ID,Order Date,Region,Sales\n"
        "A-1,2025-01-01,East,100.50\n"
        "A-2,2025-02-01,West,250.00\n"
        "A-3,2025-03-01,East,149.50\n",
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
        idempotency_key="seed-preview-snapshot",
        mutation_payload={"snapshot": snapshot.id},
    )
    relationships = DataOnboardingService(tmp_path / "relationships").propose_relationships(
        manifest
    )
    draft = service.planner.create_draft(
        "workspace-test",
        snapshot,
        manifest,
        relationships,
        "Show sales trends and order details",
    )
    detail_visual = next(
        visual for page in draft.pages for visual in page.visuals if visual.recipe == "detail"
    )
    legacy_preview = PreviewDataset(
        visual_id=detail_visual.id,
        query_receipt_id="query-legacy-detail-count",
        rows=[{"value": snapshot.tables[0].table.row_count}],
        vega_lite_spec={"mark": "text", "data": {"values": [{"value": 3}]}},
    )
    legacy_draft = draft.model_copy(
        update={
            "previews": [
                legacy_preview if item.visual_id == detail_visual.id else item
                for item in draft.previews
            ]
        }
    )
    service.store.put(
        "draft",
        legacy_draft,
        workspace_id="workspace-test",
        expected_base_hash=EMPTY_HASH,
        idempotency_key="seed-legacy-preview-draft",
        mutation_payload={"draft": legacy_draft.id},
    )

    repaired = service.draft(legacy_draft.id)

    repaired_preview = next(
        item for item in repaired.previews if item.visual_id == detail_visual.id
    )
    assert repaired_preview.rows == [
        {
            "Order ID": "A-1",
            "Order Date": "2025-01-01T00:00:00",
            "Region": "East",
            "Sales": 100.5,
        },
        {
            "Order ID": "A-2",
            "Order Date": "2025-02-01T00:00:00",
            "Region": "West",
            "Sales": 250.0,
        },
        {
            "Order ID": "A-3",
            "Order Date": "2025-03-01T00:00:00",
            "Region": "East",
            "Sales": 149.5,
        },
    ]
    assert repaired_preview.query_plan_hash is not None
    assert repaired_preview.query_result_hash is not None
    assert repaired.base_hash == legacy_draft.base_hash
    assert repaired.status == legacy_draft.status
    assert repaired.created_at == legacy_draft.created_at
    assert repaired.updated_at == legacy_draft.updated_at
    stored = service.store.get("draft", legacy_draft.id, type(legacy_draft))
    assert stored == legacy_draft
