from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ai_powerbi_analyst.contracts import ProposedDaxMeasure
from ai_powerbi_analyst.data.onboarding import DataOnboardingService, UploadedSource
from ai_powerbi_analyst.powerbi.semantic_model import SemanticProjectBuilder
from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import (
    AdvancedDaxProposal,
    ApproveAdvancedDaxRequest,
    ApproveDraftRequest,
    CreateWorkspaceRequest,
    DashboardDraft,
    DatasetSnapshot,
    DraftRequest,
    DraftRevisionProposal,
    IngestRequest,
    LocalWorkspace,
    ReviseDraftRequest,
)
from ai_powerbi_analyst.studio.planner import (
    StudioPlanningError,
    StudioPlanningService,
    compile_powerbi_plan,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StateConflictError
from ai_powerbi_analyst.workspace.planner import DashboardPlanningService


def _studio_with_approved_draft(
    tmp_path: Path,
) -> tuple[StudioService, LocalWorkspace, DatasetSnapshot, DashboardDraft]:
    source_root = tmp_path / "selected"
    source_root.mkdir()
    source = source_root / "orders.csv"
    source.write_text(
        "Order ID,Order Date,Region,Sales,Profit\n"
        "A-1,2025-01-01,East,100.00,20.00\n"
        "A-2,2025-02-01,West,250.00,40.00\n"
        "A-3,2025-03-01,West,150.00,30.00\n"
        "A-4,2025-04-01,East,100.00,20.00\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[2]
    service = StudioService(repository, tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Advanced DAX",
            selected_folder=str(source_root),
            idempotency_key="create-advanced-dax-workspace",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="ingest-advanced-dax-source",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Show sales and profit by region",
            idempotency_key="create-advanced-dax-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    approved = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[],
            idempotency_key="approve-initial-advanced-dax-draft",
            expected_base_hash=draft.base_hash,
        ),
    )
    return service, service.workspace(workspace.id), snapshot, approved


def _advanced_response(service: StudioService, snapshot_id: str) -> DraftRevisionProposal:
    snapshot = service.snapshot(snapshot_id)
    table = snapshot.tables[0].table
    draft = service.draft(service.workspace(snapshot.workspace_id).active_draft_id or "")
    sales = next(
        item for item in draft.measure_proposal.measures if "sales" in item.name.casefold()
    )
    escaped_table = table.name.replace("'", "''")
    return DraftRevisionProposal(
        measures=[
            AdvancedDaxProposal(
                id="west_sales",
                name="West Sales",
                table_id=table.id,
                expression=(f"CALCULATE([{sales.name}], '{escaped_table}'[Region] = \"West\")"),
                dependencies=[sales.id],
                format_string="$#,0.00",
                assumptions=["Region value West identifies the requested sales segment."],
            )
        ]
    )


class _FixedModel:
    def __init__(self, response: DraftRevisionProposal) -> None:
        self.response = response

    def health(self) -> SimpleNamespace:
        return SimpleNamespace(model_available=True)

    def generate(self, *_args: object, **_kwargs: object) -> object:
        response_model = _args[2] if len(_args) > 2 else None
        if response_model is AdvancedDaxProposal:
            return next(
                item for item in self.response.measures if isinstance(item, AdvancedDaxProposal)
            )
        return self.response


class _SequenceModel:
    def __init__(self, responses: list[AdvancedDaxProposal]) -> None:
        self.responses = responses
        self.calls = 0
        self.payloads: list[dict] = []

    def health(self) -> SimpleNamespace:
        return SimpleNamespace(model_available=True)

    def generate(self, *_args: object, **_kwargs: object) -> object:
        self.payloads.append(json.loads(str(_args[1])))
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


def test_invalid_aggregate_repair_receives_proposal_error_and_references(tmp_path):
    service, workspace, snapshot, approved = _studio_with_approved_draft(tmp_path)
    table = snapshot.tables[0].table
    valid = AdvancedDaxProposal(
        id="weighted_sales",
        name="Weighted Sales",
        table_id=table.id,
        expression="DIVIDE(SUMX('orders','orders'[Sales]*'orders'[Profit]),SUM('orders'[Profit]))",
        dependencies=[],
        format_string="0.00",
        assumptions=["Artificial QA example: Profit supplies the weight; not a business metric."],
    )
    invalid = valid.model_copy(
        update={"expression": "DIVIDE(SUM('orders'[Sales]*'orders'[Profit]),SUM('orders'[Profit]))"}
    )
    model = _SequenceModel([invalid, valid])
    service.planner.model = model
    revised = service.revise_draft(
        approved.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Create an advanced DAX weighted average of Sales using Profit as weight.",
            idempotency_key="repair-invalid-aggregate",
            expected_base_hash=approved.base_hash,
        ),
    )
    assert model.calls == 2
    assert "requires one column" in model.payloads[1]["repair"]
    assert model.payloads[1]["previous_unapproved_proposal"]["expression"] == invalid.expression
    assert "recipe-weighted" in model.payloads[1]["reference_material"]
    proposal = next(m for m in revised.measure_proposal.measures if m.id == valid.id)
    assert proposal.expression == valid.expression
    assert not proposal.approved
    assert revised.status == "draft"


def test_only_approved_workspace_definitions_enter_chat_memory(tmp_path):
    service, workspace, _snapshot, approved = _studio_with_approved_draft(tmp_path)
    context = json.loads(service._chat_context(workspace).split(": ", 1)[1])
    assert context["approved_definitions"]
    assert "not engine proof" in context["memory_policy"]
    _propose_advanced(service, workspace.id, approved.id, approved.base_hash)
    context = json.loads(service._chat_context(service.workspace(workspace.id)).split(": ", 1)[1])
    assert not context["approved_definitions"]


def _propose_advanced(
    service: StudioService,
    workspace_id: str,
    draft_id: str,
    base_hash: str,
) -> DashboardDraft:
    response = _advanced_response(service, service.draft(draft_id).snapshot_id)
    service.planner.model = _FixedModel(response)  # type: ignore[assignment]
    return service.revise_draft(
        draft_id,
        ReviseDraftRequest(
            workspace_id=workspace_id,
            prompt=(
                "Create an advanced DAX measure with CALCULATE for West Sales using "
                "the reviewed Region and Total Sales."
            ),
            idempotency_key="propose-west-sales-dax",
            expected_base_hash=base_hash,
        ),
    )


def test_advanced_dax_requires_separate_approval_and_compiles(
    tmp_path: Path,
) -> None:
    service, workspace, snapshot, approved = _studio_with_approved_draft(tmp_path)

    revised = _propose_advanced(service, workspace.id, approved.id, approved.base_hash)

    proposal = next(
        item for item in revised.measure_proposal.measures if isinstance(item, AdvancedDaxProposal)
    )
    assert proposal.table_id == snapshot.tables[0].table.id
    assert proposal.format_string == "$#,0.00"
    assert proposal.assumptions
    assert not proposal.approved
    assert revised.status == "draft"
    assert not revised.semantic_model.approved
    with pytest.raises(StateConflictError, match="separate approval"):
        service.approve_draft(
            revised.id,
            ApproveDraftRequest(
                workspace_id=workspace.id,
                approved_relationship_ids=[],
                idempotency_key="premature-dashboard-approval",
                expected_base_hash=revised.base_hash,
            ),
        )

    approval = ApproveAdvancedDaxRequest(
        workspace_id=workspace.id,
        draft_id=revised.id,
        measure_id=proposal.id,
        approved=True,
        approval_note="Reviewed formula, dependencies, target table, and currency format.",
        idempotency_key="approve-west-sales-dax",
        expected_base_hash=revised.base_hash,
    )
    dax_approved = service.approve_advanced_dax(revised.id, proposal.id, approval)
    replayed = service.approve_advanced_dax(revised.id, proposal.id, approval)

    assert replayed == dax_approved
    assert dax_approved.status == "draft"
    approved_proposal = next(
        item
        for item in dax_approved.measure_proposal.measures
        if isinstance(item, AdvancedDaxProposal)
    )
    assert approved_proposal.approved
    records = service.approvals(workspace.id)
    assert len(records) == 1
    assert records[0].object_type == "advanced_dax"
    assert records[0].object_id == proposal.id
    events = service.events(workspace.id)
    assert sum(item["type"] == "measure.advanced_dax_approved" for item in events) == 1

    dashboard_approved = service.approve_draft(
        dax_approved.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            approved_relationship_ids=[],
            idempotency_key="approve-dashboard-with-west-sales",
            expected_base_hash=dax_approved.base_hash,
        ),
    )
    manifest = service.ingestion.load_manifest(snapshot)
    powerbi = compile_powerbi_plan(dashboard_approved, manifest)
    compiled = next(
        item for item in powerbi.semantic_model.measures if isinstance(item, ProposedDaxMeasure)
    )
    assert compiled.table_id == proposal.table_id
    assert compiled.format_string == proposal.format_string
    assert compiled.expression == proposal.expression
    assert compiled.dependencies == proposal.dependencies
    assert compiled.assumptions == proposal.assumptions


def test_advanced_dax_preserves_an_explicit_formula_and_normalizes_currency_label(
    tmp_path: Path,
) -> None:
    service, workspace, snapshot, approved = _studio_with_approved_draft(tmp_path)
    correct = next(
        item
        for item in _advanced_response(service, snapshot.id).measures
        if isinstance(item, AdvancedDaxProposal)
    )
    incomplete = correct.model_copy(
        update={"expression": "[Total Sales]", "format_string": "Currency"}
    )
    model = _SequenceModel([incomplete])
    service.planner.model = model  # type: ignore[assignment]

    revised = service.revise_draft(
        approved.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt=(
                f"Create an advanced DAX measure named West Sales using exactly "
                f"{correct.expression}. "
                "Use currency formatting and do not add a page."
            ),
            idempotency_key="repair-exact-west-sales-dax",
            expected_base_hash=approved.base_hash,
        ),
    )

    proposal = next(
        item for item in revised.measure_proposal.measures if isinstance(item, AdvancedDaxProposal)
    )
    assert model.calls == 1
    assert proposal.expression == correct.expression
    assert proposal.format_string == "$#,0.00;($#,0.00);-"
    assert any("exact instruction" in item for item in proposal.assumptions)


@pytest.mark.parametrize(
    ("expression", "dependencies", "message"),
    [
        (
            "CALCULATE([Total Sales], 'orders'[Imaginary] = 1)",
            ["sales"],
            "unknown table or column",
        ),
        ("COUNTROWS(UnknownTable)", [], "unknown table reference"),
        ("SUM('orders'[Sales] * 'orders'[Profit])", [], "requires one column"),
        (
            "NOT_A_REAL_DAX_FUNCTION('orders'[Sales])",
            [],
            "unsupported DAX function",
        ),
        ("[Recursive Measure]", ["recursive_measure"], "cycle"),
    ],
)
def test_invalid_advanced_dax_is_rejected_before_storage(
    tmp_path: Path,
    expression: str,
    dependencies: list[str],
    message: str,
) -> None:
    service, workspace, snapshot, approved = _studio_with_approved_draft(tmp_path)
    table = snapshot.tables[0].table
    draft = service.draft(approved.id)
    sales = next(
        item for item in draft.measure_proposal.measures if "sales" in item.name.casefold()
    )
    measure_id = "recursive_measure" if "Recursive" in expression else "invalid_column"
    name = "Recursive Measure" if "Recursive" in expression else "Invalid Column"
    resolved_dependencies = [sales.id if item == "sales" else item for item in dependencies]
    response = DraftRevisionProposal(
        measures=[
            AdvancedDaxProposal(
                id=measure_id,
                name=name,
                table_id=table.id,
                expression=expression,
                dependencies=resolved_dependencies,
                format_string="#,#0.00",
                assumptions=["This intentionally invalid proposal must fail closed."],
            )
        ]
    )
    service.planner.model = _FixedModel(response)  # type: ignore[assignment]

    with pytest.raises(StudioPlanningError, match=message):
        service.revise_draft(
            approved.id,
            ReviseDraftRequest(
                workspace_id=workspace.id,
                prompt="Create an advanced DAX measure using CALCULATE for this formula.",
                idempotency_key=f"reject-{measure_id}",
                expected_base_hash=approved.base_hash,
            ),
        )

    assert service.draft(approved.id) == approved


def test_governed_formula_does_not_accept_model_supplied_advanced_dax(
    tmp_path: Path,
) -> None:
    service, workspace, snapshot, approved = _studio_with_approved_draft(tmp_path)
    service.planner.model = _FixedModel(  # type: ignore[assignment]
        _advanced_response(service, snapshot.id)
    )

    revised = service.revise_draft(
        approved.id,
        ReviseDraftRequest(
            workspace_id=workspace.id,
            prompt="Add an average formula for Sales.",
            idempotency_key="governed-sales-formula",
            expected_base_hash=approved.base_hash,
        ),
    )

    assert not any(
        isinstance(item, AdvancedDaxProposal) for item in revised.measure_proposal.measures
    )


def test_advanced_dax_approval_api_is_strict(tmp_path: Path) -> None:
    service, workspace, _snapshot, approved = _studio_with_approved_draft(tmp_path)
    revised = _propose_advanced(service, workspace.id, approved.id, approved.base_hash)
    proposal = next(
        item for item in revised.measure_proposal.measures if isinstance(item, AdvancedDaxProposal)
    )
    token = "advanced-dax-test-token"
    client = TestClient(create_app(service, token))
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "workspace_id": workspace.id,
        "draft_id": revised.id,
        "measure_id": proposal.id,
        "approved": True,
        "approval_note": "Reviewed locally.",
        "idempotency_key": "approve-dax-through-api",
        "expected_base_hash": revised.base_hash,
    }
    path = f"/v1/drafts/{revised.id}/measures/{proposal.id}/approval"

    rejected = client.put(path, json={**payload, "unexpected": True}, headers=headers)
    accepted = client.put(path, json=payload, headers=headers)

    assert rejected.status_code == 422
    assert accepted.status_code == 200
    assert next(
        item
        for item in accepted.json()["measure_proposal"]["measures"]
        if item["tier"] == "advanced_dax"
    )["approved"]
    approvals = client.get(
        f"/v1/workspaces/{workspace.id}/approvals",
        headers=headers,
    )
    assert approvals.status_code == 200
    assert len(approvals.json()) == 1


def test_advanced_dax_can_be_rejected_and_removed(tmp_path: Path) -> None:
    service, workspace, _snapshot, approved = _studio_with_approved_draft(tmp_path)
    revised = _propose_advanced(service, workspace.id, approved.id, approved.base_hash)
    proposal = next(
        item for item in revised.measure_proposal.measures if isinstance(item, AdvancedDaxProposal)
    )

    rejected = service.approve_advanced_dax(
        revised.id,
        proposal.id,
        ApproveAdvancedDaxRequest(
            workspace_id=workspace.id,
            draft_id=revised.id,
            measure_id=proposal.id,
            approved=False,
            approval_note="Formula does not match the intended business definition.",
            idempotency_key="reject-west-sales-dax",
            expected_base_hash=revised.base_hash,
        ),
    )

    assert all(item.id != proposal.id for item in rejected.measure_proposal.measures)
    assert all(
        visual.measure_id != proposal.id for page in rejected.pages for visual in page.visuals
    )
    assert service.approvals(workspace.id)[0].approved is False
    assert any(
        event["type"] == "measure.advanced_dax_rejected" for event in service.events(workspace.id)
    )


def test_advanced_dax_is_written_to_its_target_table_with_reviewed_format(
    tmp_path: Path,
) -> None:
    manifest = DataOnboardingService(tmp_path / "managed-data").import_uploads(
        [
            UploadedSource(
                name="orders.csv",
                content=b"Order ID,Sales\nA-1,100\nA-2,200\n",
            ),
            UploadedSource(
                name="targets.csv",
                content=b"Region,Target\nEast,120\nWest,180\n",
            ),
        ]
    )
    plan = DashboardPlanningService(None).plan("Build a sales dashboard", manifest, [])
    target_table = next(table for table in manifest.tables if table.name == "targets")
    advanced = ProposedDaxMeasure(
        id="total_target",
        name="Total Target",
        table_id=target_table.id,
        expression="SUM('targets'[Target])",
        format_string="$#,0;($#,0)",
        assumptions=["Target is additive across the reviewed target rows."],
        approved=True,
    )
    approved = plan.model_copy(
        update={
            "approved": True,
            "semantic_model": plan.semantic_model.model_copy(
                update={
                    "approved": True,
                    "measures": [*plan.semantic_model.measures, advanced],
                }
            ),
        }
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        Path(__file__).resolve().parents[2] / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )
    # Unit scope: never depend on installed validators or their timing under CPU load.
    builder._external_validation = lambda _project: ([], [])  # type: ignore[method-assign]

    pbip, receipt = builder.build("Targeted Advanced DAX", manifest, approved)

    assert receipt.passed
    table_files = list(pbip.parent.glob("*.SemanticModel/definition/tables/*.tmdl"))
    target_tmdl = next(path for path in table_files if path.stem == "targets").read_text(
        encoding="utf-8"
    )
    other_tmdl = next(path for path in table_files if path.stem == "orders").read_text(
        encoding="utf-8"
    )
    assert "measure 'Total Target' = SUM('targets'[Target])" in target_tmdl
    assert "formatString: $#,0;($#,0)" in target_tmdl
    assert "Total Target" not in other_tmdl
