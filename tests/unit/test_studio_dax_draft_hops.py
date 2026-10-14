"""A draft's lab measures filter through one direct relationship, as the build check does.

Orders -> Customers -> Regions, both joins approved. Power BI would propagate a
Regions filter to Orders through Customers, but the build check
(planner.validate_cross_table_bindings) accepts only a direct relationship for a saved
Advanced DAX measure. When the lab followed the two-hop path for drafts, a measure
saved and was approved, then the whole draft failed approval and could not be built
until the measure was deleted. The lab now refuses it up front, as it did before
paths existed; an imported report's own joins are still followed across hops.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_powerbi_analyst.studio.contracts import (
    AddDaxMeasureRequest,
    AdvancedDaxProposal,
    ApproveAdvancedDaxRequest,
    ApproveDraftRequest,
    CreateWorkspaceRequest,
    DashboardDraft,
    DaxEvaluateRequest,
    DraftRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.dax import DaxSyntaxError, describe_dax, evaluate_dax
from ai_powerbi_analyst.studio.planner import (
    StudioPlanningError,
    StudioPlanningService,
    validate_cross_table_bindings,
)
from ai_powerbi_analyst.studio.report_import import (
    RELATIONSHIPS_VERSION,
    ImportedPage,
    ImportedReport,
    imported_relationships,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH

REGIONS = ("East", "West", "North")
# Orders O-1..O-40; customer C-(i % 6); Sales 10 * (i % 7 + 1). West (Bob) has C-1, C-4.
BOB_SALES = sum(10 * (i % 7 + 1) for i in range(1, 41) if REGIONS[(i % 6) % 3] == "West")
TWO_HOP = 'CALCULATE(SUM(Orders[Sales]), Regions[Manager] = "Bob")'
ONE_HOP = 'CALCULATE(SUM(Orders[Sales]), Customers[Region] = "West")'
NOT_DIRECT = "No active many-to-one relationship lets Regions filter Orders"


@pytest.fixture
def chain(tmp_path: Path) -> tuple[StudioService, str]:
    root = tmp_path / "chain"
    root.mkdir()
    (root / "Orders.csv").write_text(
        "Order ID,Customer ID,Sales\n"
        + "".join(f"O-{i},C-{i % 6},{10 * (i % 7 + 1)}\n" for i in range(1, 41)),
        encoding="utf-8",
    )
    (root / "Customers.csv").write_text(
        "Customer ID,Customer Name,Region\n"
        + "".join(f"C-{i},Name {i},{REGIONS[i % 3]}\n" for i in range(6)),
        encoding="utf-8",
    )
    (root / "Regions.csv").write_text(
        "Region,Manager\nEast,Ann\nWest,Bob\nNorth,Cy\n", encoding="utf-8"
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Chain",
            selected_folder=str(root),
            idempotency_key="hops-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[
                str(root / name) for name in ("Orders.csv", "Customers.csv", "Regions.csv")
            ],
            idempotency_key="hops-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard",
            idempotency_key="hops-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    draft = _draft(service, workspace.id)
    shape = {
        (item.from_table_id.split("_")[0], item.to_table_id.split("_")[0], item.cardinality)
        for item in draft.semantic_model.relationships
    }
    assert shape == {
        ("orders", "customers", "many_to_one"),
        ("customers", "regions", "many_to_one"),
    }
    _approve_all(service, workspace.id, "hops-approve")
    return service, workspace.id


def _draft(service: StudioService, workspace_id: str) -> DashboardDraft:
    return service.draft(service.workspace(workspace_id).active_draft_id or "")


def _approve_all(service: StudioService, workspace_id: str, key: str) -> DashboardDraft:
    draft = _draft(service, workspace_id)
    return service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace_id,
            approved_relationship_ids=[item.id for item in draft.semantic_model.relationships],
            idempotency_key=key,
            expected_base_hash=draft.base_hash,
        ),
    )


def _save(service: StudioService, workspace_id: str, name: str, expression: str) -> None:
    draft = _draft(service, workspace_id)
    service.add_dax_measure(
        workspace_id,
        AddDaxMeasureRequest(
            workspace_id=workspace_id,
            name=name,
            expression=expression,
            idempotency_key=f"hops-save-{name}",
            expected_base_hash=draft.base_hash,
        ),
    )


def test_a_draft_filter_two_hops_away_is_refused_before_it_can_be_saved(chain) -> None:
    service, workspace_id = chain
    with pytest.raises(DaxSyntaxError, match=NOT_DIRECT):
        service.evaluate_dax(DaxEvaluateRequest(workspace_id=workspace_id, expression=TWO_HOP))
    with pytest.raises(DaxSyntaxError, match=NOT_DIRECT):
        service.evaluate_dax(
            DaxEvaluateRequest(
                workspace_id=workspace_id,
                expression='EVALUATE SUMMARIZECOLUMNS(Regions[Manager], "S", SUM(Orders[Sales]))',
            )
        )
    before = _draft(service, workspace_id)
    with pytest.raises(DaxSyntaxError, match=NOT_DIRECT):
        _save(service, workspace_id, "Bob Sales", TWO_HOP)
    after = _draft(service, workspace_id)
    assert after.base_hash == before.base_hash
    assert not any(m.name == "Bob Sales" for m in after.measure_proposal.measures)
    # The draft is not stuck: it still approves as a whole.
    assert _approve_all(service, workspace_id, "hops-approve-again").status == "approved"


def test_a_one_hop_lab_measure_saves_and_the_whole_draft_still_approves(chain) -> None:
    service, workspace_id = chain
    west = service.evaluate_dax(DaxEvaluateRequest(workspace_id=workspace_id, expression=ONE_HOP))
    assert next(iter(west.rows[0].values())) == BOB_SALES
    _save(service, workspace_id, "West Sales", ONE_HOP)
    draft = _draft(service, workspace_id)
    measure = next(m for m in draft.measure_proposal.measures if m.name == "West Sales")
    service.approve_advanced_dax(
        draft.id,
        measure.id,
        ApproveAdvancedDaxRequest(
            workspace_id=workspace_id,
            draft_id=draft.id,
            measure_id=measure.id,
            approved=True,
            approval_note="One-hop filter reviewed",
            idempotency_key="hops-approve-measure",
            expected_base_hash=draft.base_hash,
        ),
    )
    assert _approve_all(service, workspace_id, "hops-approve-with-measure").status == "approved"


def test_what_the_lab_accepts_for_a_draft_is_what_the_build_check_approves(chain) -> None:
    service, workspace_id = chain
    draft = _draft(service, workspace_id)
    snapshot = service.snapshot(draft.snapshot_id)
    orders = next(t.id for t in snapshot.tables if t.table.name == "Orders")
    for expression in (
        ONE_HOP,
        TWO_HOP,
        'CALCULATE(SUM(Orders[Sales]), Regions[Manager] <> "Bob")',
        'CALCULATE(SUM(Orders[Sales]), Customers[Customer Name] = "Name 1")',
    ):
        try:
            describe_dax(
                snapshot,
                draft.measure_proposal.measures,
                expression,
                draft.semantic_model.relationships,
            )
            lab_accepts = True
        except DaxSyntaxError:
            lab_accepts = False
        saved = AdvancedDaxProposal(
            id="lab_measure",
            name="Lab Measure",
            table_id=orders,
            expression=expression,
            format_string="0",
            assumptions=["Evaluated in the DAX lab"],
            approved=True,
        )
        candidate = draft.model_copy(
            update={
                "measure_proposal": draft.measure_proposal.model_copy(
                    update={"measures": [*draft.measure_proposal.measures, saved]}
                )
            }
        )
        try:
            validate_cross_table_bindings(candidate)
            build_accepts = True
        except StudioPlanningError:
            build_accepts = False
        assert lab_accepts == build_accepts, expression
        assert lab_accepts == ("Regions[" not in expression), expression


def test_an_imported_reports_own_joins_are_still_followed_across_hops(chain) -> None:
    service, workspace_id = chain
    draft = _draft(service, workspace_id)
    snapshot = service.snapshot(draft.snapshot_id)
    names = {t.id: t.table.name for t in snapshot.tables}
    columns = {(t.id, c.id): c.name for t in snapshot.tables for c in t.table.columns}
    # The same two joins, as the report's author built them in Power BI.
    report = ImportedReport(
        id="report-hops",
        workspace_id="workspace-hops",
        name="Chain",
        source_path="chain.pbix",
        archived_path="chain-original.pbix",
        source_hash="0" * 64,
        base_hash="0" * 64,
        format="PBIR",
        pages=[ImportedPage(id="p1", title="Page 1")],
        relationships=[
            {
                "FromTableName": names[item.from_table_id],
                "FromColumnName": columns[(item.from_table_id, item.from_column_id)],
                "ToTableName": names[item.to_table_id],
                "ToColumnName": columns[(item.to_table_id, item.to_column_id)],
                "IsActive": 1,
                "Cardinality": "M:1",
                "CrossFilteringBehavior": "Single",
            }
            for item in draft.semantic_model.relationships
        ],
        relationships_version=RELATIONSHIPS_VERSION,
    )
    authored = imported_relationships(report, snapshot)
    assert len(authored) == 2

    def value(expression: str, relationships) -> object:  # type: ignore[no-untyped-def]
        result = evaluate_dax(
            snapshot, [], expression, 5, service.aggregates, relationships=relationships
        )
        return next(iter(result.rows[0].values()))

    orders = next(t.id for t in snapshot.tables if t.table.name == "Orders")
    assert value(TWO_HOP, authored) == BOB_SALES
    assert describe_dax(snapshot, [], TWO_HOP, authored)[0] == orders
    # The draft's own copies of those joins stay one hop, and one hop is unchanged.
    with pytest.raises(DaxSyntaxError, match=NOT_DIRECT):
        value(TWO_HOP, draft.semantic_model.relationships)
    assert value(ONE_HOP, draft.semantic_model.relationships) == BOB_SALES
    assert value(ONE_HOP, authored) == BOB_SALES
