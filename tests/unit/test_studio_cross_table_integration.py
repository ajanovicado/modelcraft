from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_powerbi_analyst.contracts import (
    ColumnProfile,
    RelationshipCandidate,
    SourceManifest,
    TableProfile,
)
from ai_powerbi_analyst.studio.aggregates import AggregateService
from ai_powerbi_analyst.studio.contracts import (
    DashboardDraft,
    DashboardPageProposal,
    DashboardVisualProposal,
    DatasetSnapshot,
    DatasetUnderstandingPlan,
    GovernedMeasureSpec,
    MeasureProposal,
    RelationshipProposal,
    SemanticModelProposal,
    SemanticTableProposal,
)
from ai_powerbi_analyst.studio.ingestion import StudioIngestionService
from ai_powerbi_analyst.studio.inventory import inventory_folder
from ai_powerbi_analyst.studio.planner import (
    StudioPlanningError,
    StudioPlanningService,
    compile_powerbi_plan,
)
from ai_powerbi_analyst.studio.relationship_engine import RelationshipGovernanceEngine


def _cross_table_inputs(
    tmp_path: Path,
) -> tuple[
    DatasetSnapshot,
    SourceManifest,
    RelationshipCandidate,
    ColumnProfile,
    ColumnProfile,
    tuple[TableProfile, TableProfile],
]:
    source_root = tmp_path / "selected"
    source_root.mkdir()
    (source_root / "orders.csv").write_text(
        "Order ID,Region,Sales\nA-1,East,100\nA-2,West,250\nA-3,East,150\n",
        encoding="utf-8",
    )
    (source_root / "regions.csv").write_text(
        "Region,Manager\nEast,Ada\nWest,Grace\n",
        encoding="utf-8",
    )
    snapshot, manifest = StudioIngestionService(tmp_path / "state").ingest(
        "workspace-test",
        [item for item in inventory_folder(source_root) if item.selectable],
    )
    orders = next(table for table in manifest.tables if table.name == "orders")
    regions = next(table for table in manifest.tables if table.name == "regions")
    sales = next(column for column in orders.columns if column.name == "Sales")
    manager = next(column for column in regions.columns if column.name == "Manager")
    relationship = next(
        item
        for item in RelationshipGovernanceEngine(tmp_path / "spill").propose(manifest).relationships
        if item.from_table_id == orders.id and item.to_table_id == regions.id
    )
    assert not relationship.blockers
    return snapshot, manifest, relationship, sales, manager, (orders, regions)


def _relationship_proposal(
    candidate: RelationshipCandidate,
    *,
    approved: bool,
) -> RelationshipProposal:
    return RelationshipProposal(
        id=candidate.id,
        from_table_id=candidate.from_table_id,
        from_column_id=candidate.from_column_id,
        to_table_id=candidate.to_table_id,
        to_column_id=candidate.to_column_id,
        cardinality=candidate.cardinality,
        cross_filter_direction=candidate.cross_filter_direction,
        is_active=candidate.is_active,
        rely_on_referential_integrity=candidate.rely_on_referential_integrity,
        confidence=candidate.confidence,
        evidence=candidate.evidence,
        blockers=candidate.blockers,
        review_status="approved" if approved else "pending",
    )


def _draft(
    snapshot: DatasetSnapshot,
    relationship: RelationshipProposal,
    sales: ColumnProfile,
    manager: ColumnProfile,
    tables: tuple[TableProfile, TableProfile],
) -> DashboardDraft:
    orders, regions = tables
    measure = GovernedMeasureSpec(
        id="total_sales",
        name="Total Sales",
        operation="sum",
        table_id=orders.id,
        column_id=sales.id,
        format_string="#,#0.00",
    )
    visual = DashboardVisualProposal(
        id="sales_by_manager",
        recipe="category",
        title="Sales by Manager",
        visual_type="clusteredColumnChart",
        table_id=regions.id,
        measure_id=measure.id,
        category_column_id=manager.id,
        x=0,
        y=0,
        width=12,
        height=6,
    )
    now = datetime.now(UTC)
    return DashboardDraft(
        id="draft-cross-table",
        workspace_id="workspace-test",
        snapshot_id=snapshot.id,
        prompt="Show sales by manager",
        understanding=DatasetUnderstandingPlan(
            summary="Orders are facts and Regions supplies the reviewed manager dimension.",
            grains={orders.id: "One row per order", regions.id: "One row per region"},
        ),
        semantic_model=SemanticModelProposal(
            tables=[
                SemanticTableProposal(
                    table_id=orders.id,
                    role="fact",
                    display_name=orders.name,
                ),
                SemanticTableProposal(
                    table_id=regions.id,
                    role="dimension",
                    display_name=regions.name,
                ),
            ],
            relationships=[relationship],
            approved=True,
        ),
        measure_proposal=MeasureProposal(measures=[measure]),
        pages=[
            DashboardPageProposal(
                id="manager_analysis",
                title="Manager Analysis",
                purpose="Group reviewed sales by the related manager dimension.",
                visuals=[visual],
            )
        ],
        status="approved",
        base_hash="0" * 64,
        created_at=now,
        updated_at=now,
    )


def test_cross_table_preview_and_powerbi_compile_require_the_same_approved_relationship(
    tmp_path: Path,
) -> None:
    snapshot, manifest, candidate, sales, manager, tables = _cross_table_inputs(tmp_path)
    approved = _draft(
        snapshot,
        _relationship_proposal(candidate, approved=True),
        sales,
        manager,
        tables,
    )
    planner = StudioPlanningService(None, AggregateService(tmp_path / "aggregate-spill"))

    previews = planner._previews(
        snapshot,
        manifest,
        approved.measure_proposal,
        approved.semantic_model,
        approved.pages,
    )
    compiled = compile_powerbi_plan(approved, manifest)

    assert sorted(previews[0].rows, key=lambda row: str(row["group_1"])) == [
        {"group_1": "Ada", "value": 250},
        {"group_1": "Grace", "value": 250},
    ]
    assert previews[0].query_plan_hash is not None
    assert previews[0].query_result_hash is not None
    compiled_visual = compiled.pages[0].visuals[0]
    assert {(binding.table_id, binding.field_kind) for binding in compiled_visual.bindings} == {
        (candidate.from_table_id, "measure"),
        (candidate.to_table_id, "column"),
    }
    assert [item.id for item in compiled.semantic_model.relationships] == [candidate.id]

    pending = approved.model_copy(
        update={
            "semantic_model": approved.semantic_model.model_copy(
                update={"relationships": [_relationship_proposal(candidate, approved=False)]}
            )
        }
    )
    pending_previews = planner._previews(
        snapshot,
        manifest,
        pending.measure_proposal,
        pending.semantic_model,
        pending.pages,
    )
    assert pending_previews[0].rows == []
    assert pending_previews[0].query_plan_hash is None
    assert pending_previews[0].empty_reason == "pending_relationship"
    with pytest.raises(StudioPlanningError, match="approved active relationship"):
        compile_powerbi_plan(pending, manifest)
