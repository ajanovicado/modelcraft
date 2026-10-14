from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import (
    AddDaxMeasureRequest,
    ApproveAdvancedDaxRequest,
    ApproveDraftRequest,
    CreateWorkspaceRequest,
    DraftRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH

TOKEN = "isolated-release-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def service(tmp_path: Path) -> StudioService:
    return StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")


@pytest.fixture
def imported(service: StudioService, tmp_path: Path) -> tuple[StudioService, str, str]:
    """A workspace with one imported table: orders(Region, Sales, Qty)."""
    source = tmp_path / "orders.csv"
    source.write_text("Region,Sales,Qty\nEast,100,2\nWest,200,4\nEast,50,1\n", encoding="utf-8")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="DAX lab QA",
            selected_folder=str(tmp_path),
            idempotency_key="dax-lab-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="dax-lab-import",
            expected_base_hash=workspace.base_hash,
        )
    )
    return service, workspace.id, snapshot.tables[0].table.name


def test_dax_evaluate_aggregates_groups_filters_and_ratios(
    imported: tuple[StudioService, str, str],
) -> None:
    service, workspace_id, table = imported
    with TestClient(create_app(service, TOKEN)) as client:

        def evaluate(expression: str, top_n: int = 50) -> dict:
            response = client.post(
                f"/v1/workspaces/{workspace_id}/dax/evaluate",
                headers=HEADERS,
                json={
                    "workspace_id": workspace_id,
                    "expression": expression,
                    "top_n": top_n,
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        scalar = evaluate(f"SUM('{table}'[Sales])")
        assert scalar["rows"] == [{"SUM(Sales)": 350}]
        assert evaluate(f"SUM({table}[Sales])")["rows"] == scalar["rows"]
        assert evaluate(f"COUNTROWS({table})")["rows"] == [{"COUNTROWS()": 3}]

        counted = evaluate(f"COUNTROWS('{table}')")
        assert counted["rows"] == [{"COUNTROWS()": 3}]

        grouped = evaluate(
            f"EVALUATE SUMMARIZECOLUMNS('{table}'[Region], \"Total\", SUM('{table}'[Sales]))"
        )
        assert grouped["columns"] == ["Region", "Total"]
        assert {"Region": "West", "Total": 200} in grouped["rows"]
        assert {"Region": "East", "Total": 150} in grouped["rows"]
        assert (
            evaluate(f'EVALUATE SUMMARIZECOLUMNS({table}[Region], "Total", SUM({table}[Sales]))')[
                "rows"
            ]
            == grouped["rows"]
        )

        filtered = evaluate(f"CALCULATE(SUM('{table}'[Sales]), '{table}'[Region] = \"East\")")
        assert filtered["rows"] == [{"SUM(Sales)": 150}]
        assert (
            evaluate(f'CALCULATE(SUM({table}[Sales]), {table}[Region] = "East")')["rows"]
            == filtered["rows"]
        )

        ratio = evaluate(f"DIVIDE(SUM('{table}'[Sales]), SUM('{table}'[Qty]))")
        assert ratio["rows"] == [{"DIVIDE(Sales, Qty)": 50.0}]

        values = evaluate(f"EVALUATE VALUES('{table}'[Region])")
        assert values["columns"] == ["Region", "Rows"]
        assert sorted(row["Region"] for row in values["rows"]) == ["East", "West"]

        scan = evaluate(f"EVALUATE TOPN(2, '{table}')")
        assert scan["row_count"] == 2
        assert scan["truncated"] is True
        assert "Sales" in scan["columns"]


def test_dax_evaluate_rejects_unsupported_and_unknown_references(
    imported: tuple[StudioService, str, str],
) -> None:
    service, workspace_id, table = imported
    with TestClient(create_app(service, TOKEN)) as client:

        def evaluate(expression: str) -> int:
            return client.post(
                f"/v1/workspaces/{workspace_id}/dax/evaluate",
                headers=HEADERS,
                json={"workspace_id": workspace_id, "expression": expression},
            ).status_code

        assert evaluate("SUMX('orders', 'orders'[Sales])") == 422
        assert evaluate(f"SUM('{table}'[Missing])") == 422
        assert evaluate("SUM('nope'[Sales])") == 422
        assert evaluate("SUM(nope[Sales])") == 422
        assert evaluate(f"SUM({table}[Missing])") == 422
        assert evaluate("DROP TABLE x") == 422
        assert evaluate("") == 422


def test_dax_evaluate_resolves_draft_measures_and_save_flow(
    imported: tuple[StudioService, str, str],
) -> None:
    service, workspace_id, table = imported
    workspace = service.workspace(workspace_id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace_id,
            snapshot_id=workspace.active_snapshot_id,
            prompt="overview of sales",
            idempotency_key="dax-lab-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    assert any(m.name == "Total Sales" for m in draft.measure_proposal.measures)
    with TestClient(create_app(service, TOKEN)) as client:
        resolved = client.post(
            f"/v1/workspaces/{workspace_id}/dax/evaluate",
            headers=HEADERS,
            json={"workspace_id": workspace_id, "expression": "[Total Sales]"},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["rows"] == [{"Total Sales": 350}]

        saved = client.post(
            f"/v1/workspaces/{workspace_id}/dax/measures",
            headers=HEADERS,
            json={
                "workspace_id": workspace_id,
                "name": "Sales Per Unit",
                "expression": f"DIVIDE(SUM('{table}'[Sales]), SUM('{table}'[Qty]))",
                "idempotency_key": "dax-lab-save",
                "expected_base_hash": draft.base_hash,
            },
        )
        assert saved.status_code == 200, saved.text
        updated = saved.json()
        proposal = next(
            m for m in updated["measure_proposal"]["measures"] if m["name"] == "Sales Per Unit"
        )
        assert proposal["tier"] == "advanced_dax"
        assert proposal["approved"] is False
        assert updated["base_hash"] != draft.base_hash

        replay = client.post(
            f"/v1/workspaces/{workspace_id}/dax/measures",
            headers=HEADERS,
            json={
                "workspace_id": workspace_id,
                "name": "Sales Per Unit",
                "expression": f"DIVIDE(SUM('{table}'[Sales]), SUM('{table}'[Qty]))",
                "idempotency_key": "dax-lab-save",
                "expected_base_hash": draft.base_hash,
            },
        )
        assert replay.status_code == 200
        assert replay.json() == updated

        duplicate = client.post(
            f"/v1/workspaces/{workspace_id}/dax/measures",
            headers=HEADERS,
            json={
                "workspace_id": workspace_id,
                "name": "Sales Per Unit",
                "expression": f"SUM('{table}'[Sales])",
                "idempotency_key": "dax-lab-save-2",
                "expected_base_hash": updated["base_hash"],
            },
        )
        assert duplicate.status_code == 422

        bad_syntax = client.post(
            f"/v1/workspaces/{workspace_id}/dax/measures",
            headers=HEADERS,
            json={
                "workspace_id": workspace_id,
                "name": "Broken",
                "expression": "SUMX('orders', 'orders'[Sales])",
                "idempotency_key": "dax-lab-save-3",
                "expected_base_hash": updated["base_hash"],
            },
        )
        assert bad_syntax.status_code == 422

        events = client.get(f"/v1/workspaces/{workspace_id}/events", headers=HEADERS).json()
        assert [event["type"] for event in events].count("draft.dax_measure_added") == 1


def test_dax_lab_type_gates_and_approved_draft_reset(
    imported: tuple[StudioService, str, str],
) -> None:
    service, workspace_id, table = imported
    workspace = service.workspace(workspace_id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace_id,
            snapshot_id=workspace.active_snapshot_id,
            prompt="overview of sales",
            idempotency_key="dax-gate-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    with TestClient(create_app(service, TOKEN)) as client:

        def evaluate(expression: str) -> int:
            return client.post(
                f"/v1/workspaces/{workspace_id}/dax/evaluate",
                headers=HEADERS,
                json={"workspace_id": workspace_id, "expression": expression},
            ).status_code

        # Region is a string column: SUM and COUNT must be rejected.
        assert evaluate(f"SUM('{table}'[Region])") == 422
        assert evaluate(f"COUNT('{table}'[Region])") == 422
        assert evaluate(f"DISTINCTCOUNT('{table}'[Region])") == 200
        assert evaluate(f"MIN('{table}'[Region])") == 200

        approved = service.approve_draft(
            draft.id,
            ApproveDraftRequest(
                workspace_id=workspace_id,
                idempotency_key="dax-gate-approve",
                expected_base_hash=draft.base_hash,
                approved_relationship_ids=[],
            ),
        )
        assert approved.status == "approved"

        # Saving to an approved draft must return it to review, not keep the
        # approval stamp on a mutated draft.
        saved = client.post(
            f"/v1/workspaces/{workspace_id}/dax/measures",
            headers=HEADERS,
            json={
                "workspace_id": workspace_id,
                "name": "Late Measure",
                "expression": f"SUM('{table}'[Qty])",
                "idempotency_key": "dax-gate-save",
                "expected_base_hash": approved.base_hash,
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "draft"
        assert saved.json()["semantic_model"]["approved"] is False

        # A table scan is not a measure.
        scan = client.post(
            f"/v1/workspaces/{workspace_id}/dax/measures",
            headers=HEADERS,
            json={
                "workspace_id": workspace_id,
                "name": "Whole Table",
                "expression": f"EVALUATE '{table}'",
                "idempotency_key": "dax-gate-save-2",
                "expected_base_hash": saved.json()["base_hash"],
            },
        )
        assert scan.status_code == 422


def test_inline_dax_assistant_isolated_and_audited(imported, monkeypatch):
    from types import SimpleNamespace

    service, workspace_id, table = imported
    workspace = service.workspace(workspace_id)
    before = service.messages(workspace_id)
    calls = []

    def generate(system, message, response_model, **kwargs):
        # This scripted formula tests the retained freeform fallback after the
        # separate closed pattern selector declines the request.
        if "pattern" in response_model.model_fields:
            return response_model(pattern=None)
        calls.append((message, {**kwargs, "system_prompt": system}))
        return response_model(expression=f"AVERAGE('{table}'[Sales])")

    monkeypatch.setattr(service, "model", SimpleNamespace(generate=generate, unload=lambda: None))
    with TestClient(create_app(service, TOKEN)) as client:
        payload = {
            "workspace_id": workspace_id,
            "snapshot_id": workspace.active_snapshot_id,
            "request_id": "inline-test",
            "goal": "Build and save average sales",
        }
        response = client.post("/v1/dax/assist", headers=HEADERS, json=payload)
        assert response.status_code == 200
        assert "event: done" in response.text
        assert "event: error" not in response.text
        assert "AVERAGE" in response.text
        assert calls[0][1]["repair_attempts"] == 1
        assert table in calls[0][1]["system_prompt"]
        assert service.messages(workspace_id) == before
        assert service.workspace(workspace_id) == workspace
        events = service.events(workspace_id)
        assert any(e["type"] == "dax.assistant_requested" for e in events)
        assert any(e["type"] == "dax.assistant_answered" for e in events)
        assert client.post("/v1/dax/assist", json=payload).status_code == 401
        assert (
            client.post(
                "/v1/dax/assist", headers=HEADERS, json={**payload, "expression": "x" * 4001}
            ).status_code
            == 422
        )
        stale = client.post(
            "/v1/dax/assist", headers=HEADERS, json={**payload, "snapshot_id": "snap-stale"}
        )
        assert "Data changed" in stale.text
        assert len(calls) == 1


def test_inline_dax_assistant_cancel_closes_model_and_keeps_partial_evidence(imported, monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, _ = imported
    closed = []

    def generate(*args, **kwargs):
        try:
            yield "partial"
            yield "reply"
        finally:
            closed.append(True)

    monkeypatch.setattr(service, "model", SimpleNamespace(stream_chat=generate))
    cancel = Event()
    request = DaxAssistRequest(
        workspace_id=workspace_id,
        snapshot_id=service.workspace(workspace_id).active_snapshot_id,
        request_id="cancel-test",
        goal="Average sales",
        mode="explain",
    )
    stream = dax_assistant_events(service, request, cancel)
    assert next(stream)["event"] == "status"
    assert next(stream)["event"] == "assistant"
    assert next(stream)["data"]["text"] == "partial"
    cancel.set()
    assert list(stream) == []
    assert closed == [True]
    assert service.messages(workspace_id) == []
    assert any(e["type"] == "dax.assistant_answered" for e in service.events(workspace_id))


def test_inline_generation_omits_invalid_scratch_text_but_fix_keeps_it(imported, monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, _ = imported
    calls = []

    def generate(system, message, response_model, **kwargs):
        if "pattern" in response_model.model_fields:
            return response_model(pattern=None)
        calls.append(message)
        return response_model(expression="COUNTROWS('orders')")

    monkeypatch.setattr(service, "model", SimpleNamespace(generate=generate, unload=lambda: None))
    bad = "'orders'[Region]EVALUATE Broken"
    request = DaxAssistRequest(
        workspace_id=workspace_id,
        snapshot_id=service.workspace(workspace_id).active_snapshot_id,
        request_id="scratch-context",
        goal="Count orders",
        expression=bad,
    )
    list(dax_assistant_events(service, request, Event()))
    assert bad not in calls[-1]
    list(
        dax_assistant_events(
            service,
            request.model_copy(
                update={
                    "mode": "fix",
                    "request_id": "scratch-context-fix",
                }
            ),
            Event(),
        )
    )
    assert bad in calls[-1]


def test_inline_generation_validates_real_fields_before_emitting_code(imported, monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, table = imported

    def invalid_generation(system, message, response_model, **kwargs):
        if "pattern" in response_model.model_fields:
            return response_model(pattern=None)
        return response_model(expression=f"SUM('{table}'[Nonexistent])")

    monkeypatch.setattr(
        service, "model", SimpleNamespace(generate=invalid_generation, unload=lambda: None)
    )
    request = DaxAssistRequest(
        workspace_id=workspace_id,
        snapshot_id=service.workspace(workspace_id).active_snapshot_id,
        request_id="bad-field",
        goal="Total sales",
    )
    stream = dax_assistant_events(service, request, Event())
    assert next(stream)["event"] == "status"
    assert next(stream)["event"] == "assistant"
    with pytest.raises(ValueError):
        next(stream)
    assert service.messages(workspace_id) == []


def test_sorted_rollup_totals_use_source_rows_and_limit_after_sort(imported):
    from ai_powerbi_analyst.studio.contracts import DaxEvaluateRequest
    from ai_powerbi_analyst.studio.dax import DaxSyntaxError, describe_dax

    service, workspace_id, table = imported

    def run(aggregate="SUM", order="[IsTotal] ASC, [Total] ASC", limit=50, filter_=""):
        value = f"{aggregate}('{table}'[Sales])"
        if filter_:
            value = f"CALCULATE({value}, {filter_})"
        expression = (
            f"EVALUATE SUMMARIZECOLUMNS(ROLLUPADDISSUBTOTAL(ROLLUPGROUP('{table}'[Region]), "
            f'"IsTotal"), "Total", {value}) ORDER BY {order}'
        )
        return service.evaluate_dax(
            DaxEvaluateRequest(workspace_id=workspace_id, expression=expression, top_n=limit)
        ), expression

    total, expression = run()
    assert total.grand_total_column == "IsTotal"
    assert total.rows == [
        {"Region": "East", "Total": 150, "IsTotal": False},
        {"Region": "West", "Total": 200, "IsTotal": False},
        {"Region": None, "Total": 350, "IsTotal": True},
    ]
    assert not total.truncated
    assert run(limit=1)[0].rows == total.rows[:1]
    avg, _ = run("AVERAGE", order=f"[IsTotal] ASC, '{table}'[Region] DESC")
    assert [r["Region"] for r in avg.rows] == ["West", "East", None]
    assert avg.rows[-1]["Total"] == pytest.approx(350 / 3)  # not average of group averages
    filtered, _ = run(filter_=f"'{table}'[Sales] > 50")
    assert filtered.rows[-1]["Total"] == 300
    with pytest.raises(DaxSyntaxError, match="ORDER BY"):
        run(order="[Missing] DESC")
    with pytest.raises(DaxSyntaxError):
        run(order="[Total] DESC; DROP TABLE orders")
    snapshot = service.snapshot(service.workspace(workspace_id).active_snapshot_id)
    with pytest.raises(DaxSyntaxError, match="scalar"):
        describe_dax(snapshot, [], expression)


def test_multiple_groups_and_blank_groups_are_distinct_from_grand_total(service, tmp_path):
    from ai_powerbi_analyst.studio.contracts import DaxEvaluateRequest

    source = tmp_path / "sales.csv"
    source.write_text("Region,Category,Amount\nEast,B,10\nWest,A,20\nEast,A,5\n,A,7\nEast,B,10\n")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Grouped QA",
            selected_folder=str(tmp_path),
            idempotency_key="rollup-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="rollup-import",
            expected_base_hash=workspace.base_hash,
        )
    )
    table = snapshot.tables[0].table.name
    expr = (
        f"EVALUATE SUMMARIZECOLUMNS(ROLLUPADDISSUBTOTAL(ROLLUPGROUP('{table}'[Region], "
        f"'{table}'[Category]), \"IsTotal\"), \"Total\", SUM('{table}'[Amount])) "
        "ORDER BY [IsTotal] ASC, [Category] ASC, [Total] DESC"
    )
    result = service.evaluate_dax(DaxEvaluateRequest(workspace_id=workspace.id, expression=expr))
    assert [(r["Region"], r["Category"], r["Total"], r["IsTotal"]) for r in result.rows] == [
        ("West", "A", 20, False),
        (None, "A", 7, False),
        ("East", "A", 5, False),
        ("East", "B", 20, False),
        (None, None, 52, True),
    ]
    distinct = service.evaluate_dax(
        DaxEvaluateRequest(
            workspace_id=workspace.id, expression=expr.replace("SUM(", "DISTINCTCOUNT(")
        )
    )
    assert distinct.rows[-1]["Total"] == 4  # never sum distinct counts from each group
    for invalid in [
        expr.replace('"Total", SUM', '"Region", SUM'),
        expr.replace("[Category]),", "[Region]),"),
        expr.replace("ORDER BY [IsTotal] ASC", "ORDER BY 'wrong'[Region] ASC"),
    ]:
        with pytest.raises(ValueError):
            service.evaluate_dax(DaxEvaluateRequest(workspace_id=workspace.id, expression=invalid))


def test_dax_validation_failure_is_actionable_and_not_a_runtime_outage(imported, monkeypatch):
    from types import SimpleNamespace

    from ai_powerbi_analyst.llm.runtime import LocalModelError

    service, workspace_id, table = imported

    def failed(system, message, response_model, **kwargs):
        try:
            response_model(expression=f"SUM('{table}'[Imaginary])")
        except ValueError as error:
            raise LocalModelError("Local model failed strict schema/artifact validation") from error

    monkeypatch.setattr(service, "model", SimpleNamespace(generate=failed, unload=lambda: None))
    with TestClient(create_app(service, TOKEN)) as client:
        response = client.post(
            "/v1/dax/assist",
            headers=HEADERS,
            json={
                "workspace_id": workspace_id,
                "snapshot_id": service.workspace(workspace_id).active_snapshot_id,
                "request_id": "validation-error",
                "goal": "Calculate a total",
            },
        )
    assert "event: error" in response.text
    assert "Unknown column" in response.text
    assert "Your formula is unchanged" in response.text
    assert "Settings" not in response.text
    assert (
        next(e for e in service.events(workspace_id) if e["type"] == "dax.assistant_answered")[
            "payload"
        ]["status"]
        == "failed"
    )


def test_grouped_assistant_compiles_validated_intent_without_model_written_dax(
    imported, monkeypatch
):
    from threading import Event
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, table = imported

    def generate(system, message, response_model, **kwargs):
        if "pattern" in response_model.model_fields:
            return response_model(pattern=None)
        assert "Do not write DAX code" in system
        assert kwargs["repair_attempts"] == 1
        assert "expression" not in response_model.model_fields
        return response_model(
            table=table,
            group_by=["Region"],
            operation="sum",
            value_column="Sales",
            sort_by=[{"column": "value", "direction": "desc"}],
            grand_total="last",
        )

    monkeypatch.setattr(service, "model", SimpleNamespace(generate=generate, unload=lambda: None))
    events = list(
        dax_assistant_events(
            service,
            DaxAssistRequest(
                workspace_id=workspace_id,
                snapshot_id=service.workspace(workspace_id).active_snapshot_id,
                request_id="grouped-intent",
                goal="Total sales by Region sorted by sales, grand total last",
            ),
            Event(),
        )
    )
    formula = next(e["data"]["text"] for e in events if e["event"] == "token")
    assert "ROLLUPADDISSUBTOTAL" in formula
    assert "ORDER BY [IsGrandTotal] ASC, [Total Sales] DESC" in formula
    assert service.messages(workspace_id) == []


def test_grouped_plan_rejects_invented_sorts_and_keeps_filter_literals_quoted():
    from ai_powerbi_analyst.studio.dax import parse_dax
    from ai_powerbi_analyst.studio.dax_plan import GroupedDaxPlan

    plan = GroupedDaxPlan(
        table="Manager's Orders",
        group_by=["Region"],
        operation="sum",
        value_column="Sales",
        grand_total="last",
        filters=[{"column": "Region", "operator": "=", "value": 'say "hello"'}],
    )
    formula = plan.render()
    assert "'Manager''s Orders'" in formula
    assert '"say ""hello"""' in formula
    parse_dax(formula)
    with pytest.raises(ValueError, match="Sort by"):
        GroupedDaxPlan(
            **{**plan.model_dump(), "sort_by": [{"column": "DROP TABLE orders"}]}
        ).render()


@pytest.mark.parametrize(
    "goal",
    [
        "How do I calculate average sales?",
        "i want to write a calculation that takes my sales and i guess subtracts the profits "
        "and what will that give me?",
        "Show sales, but explain what that tells me",
        "Can you help me create something",
        "Don't change my formula, show me how filters work",
        "Sales minus profit",
    ],
)
def test_questions_and_ambiguous_intent_never_auto_apply(goal):
    from ai_powerbi_analyst.studio.dax_assistant import wants_calculation

    assert not wants_calculation(goal)


@pytest.mark.parametrize(
    "goal",
    [
        "Show average Sales by Region",
        "Please calculate total Quantity",
        "I want to create sales by category",
        "Only include West",
        "Now group it by Region",
        "Make it a percentage",
        "Instead use Profit",
        "Also add a grand total",
    ],
)
def test_clear_actions_can_generate(goal):
    from ai_powerbi_analyst.studio.dax_assistant import wants_calculation

    # Unsupported shorthand is deliberately answered first, never silently applied.
    assert wants_calculation(goal) == (goal != "Only include West")


def test_inline_generation_receives_prior_discussion(imported, monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, table = imported
    calls = []

    def generate(system, message, response_model, **kwargs):
        if "pattern" in response_model.model_fields:
            return response_model(pattern=None)
        calls.append(message)
        return response_model(expression=f"SUM('{table}'[Sales])")

    monkeypatch.setattr(service, "model", SimpleNamespace(generate=generate, unload=lambda: None))
    request = DaxAssistRequest(
        workspace_id=workspace_id,
        snapshot_id=service.workspace(workspace_id).active_snapshot_id,
        request_id="discussion-context",
        goal="Now make it average",
        mode="generate",
        discussion=f"User: show total sales\nAssistant: ```dax\nSUM('{table}'[Sales])\n```",
    )
    list(dax_assistant_events(service, request, Event()))
    assert "Prior DAX conversation" in calls[-1]
    assert f"SUM('{table}'[Sales])" in calls[-1]


def test_short_advice_reply_keeps_recent_dialogue_without_pronouns(imported, monkeypatch):
    import json
    from threading import Event
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, _ = imported
    discussion = (
        "User: How should I compare Sales?\nAssistant: "
        + "Earlier context. " * 60
        + "\nUser: Average per order, including zero sales.\nAssistant: Which region?"
    )

    def generate(system, message, response_model, **kwargs):
        payload = json.loads(message)
        from ai_powerbi_analyst.llm.artifact_policy import domain_for

        assert domain_for(message) == "dax"
        assert payload["prior_discussion"] == discussion
        assert "including zero sales" in payload["prior_discussion"]
        assert kwargs == {"max_output_tokens": 256, "repair_attempts": 1}
        assert "one focused clarification" in system
        return response_model(
            answer="Average Sales for West will include zero sales.",
            calculation_request="Calculate average Sales for West",
        )

    monkeypatch.setattr(service, "model", SimpleNamespace(generate=generate, unload=lambda: None))
    monkeypatch.setattr(
        service, "evaluate_dax", lambda *_: pytest.fail("Discussion must not run a formula")
    )
    before = service.workspace(workspace_id)
    list(
        dax_assistant_events(
            service,
            DaxAssistRequest(
                workspace_id=workspace_id,
                snapshot_id=before.active_snapshot_id,
                request_id="short-followup",
                mode="auto",
                goal="West, please",
                discussion=discussion,
            ),
            Event(),
        )
    )
    assert service.workspace(workspace_id) == before
    assert service.messages(workspace_id) == []


def test_advice_preserves_formula_results_workspace_and_offers_explicit_action(
    imported, monkeypatch
):
    from threading import Event
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, table = imported
    before = service.workspace(workspace_id)

    def generate(system, message, response_model, **kwargs):
        assert "prior_discussion" in message
        assert kwargs == {"max_output_tokens": 256, "repair_attempts": 1}
        return response_model(
            answer="Subtract quantity from total sales only if that unit comparison is intended.",
            calculation_request=f"Calculate total Sales minus total Qty in {table}",
        )

    monkeypatch.setattr(service, "model", SimpleNamespace(generate=generate, unload=lambda: None))
    monkeypatch.setattr(
        service, "evaluate_dax", lambda *_: pytest.fail("Advice must never evaluate or apply")
    )
    events = list(
        dax_assistant_events(
            service,
            DaxAssistRequest(
                workspace_id=workspace_id,
                snapshot_id=before.active_snapshot_id,
                request_id="advice",
                mode="auto",
                goal="What would sales minus quantity mean?",
                expression=f"SUM('{table}'[Sales])",
                discussion="I want help understanding a calculation",
            ),
            Event(),
        )
    )
    assert events[1]["data"]["kind"] == "advice"
    assert any(e["data"].get("calculation_request") for e in events)
    assert service.workspace(workspace_id) == before
    assert service.messages(workspace_id) == []


def test_long_dialogue_keeps_latest_question_inside_existing_context_budget(imported, monkeypatch):
    import json
    from threading import Event
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, _ = imported
    goal = "What did we agree Sales means? " + "Please use plain English. " * 30
    discussion = (
        "User: "
        + "Old unrelated context. " * 65
        + "\n\nUser: Sales is a monetary amount.\nAssistant: Understood."
    )

    def generate(system, message, response_model, **_kwargs):
        payload = json.loads(message)
        assert payload["question"] == goal
        assert "Sales is a monetary amount" in payload["prior_discussion"]
        assert len((system + message).encode("utf-8")) <= 3456
        return response_model(answer="Sales is a monetary amount.", calculation_request=None)

    monkeypatch.setattr(service, "model", SimpleNamespace(generate=generate, unload=lambda: None))
    list(
        dax_assistant_events(
            service,
            DaxAssistRequest(
                workspace_id=workspace_id,
                snapshot_id=service.workspace(workspace_id).active_snapshot_id,
                request_id="bounded-discussion",
                mode="auto",
                goal=goal,
                discussion=discussion,
            ),
            Event(),
        )
    )
    requested = next(
        e for e in service.events(workspace_id) if e["type"] == "dax.assistant_requested"
    )
    assert requested["payload"]["discussion"] == discussion


def test_new_scalar_request_does_not_inherit_old_rollup(imported, monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, table = imported

    def generate(system, message, response_model, **kwargs):
        if "pattern" in response_model.model_fields:
            return response_model(pattern=None)
        assert "expression" in response_model.model_fields
        assert "ROLLUPADDISSUBTOTAL" not in message
        return response_model(expression=f"SUM('{table}'[Sales])-SUM('{table}'[Qty])")

    monkeypatch.setattr(service, "model", SimpleNamespace(generate=generate, unload=lambda: None))
    events = list(
        dax_assistant_events(
            service,
            DaxAssistRequest(
                workspace_id=workspace_id,
                snapshot_id=service.workspace(workspace_id).active_snapshot_id,
                request_id="new-scalar",
                mode="auto",
                goal="Calculate total Sales minus total Qty",
                expression=f"EVALUATE SUMMARIZECOLUMNS(ROLLUPADDISSUBTOTAL('{table}'[Region], "
                f'"TotalFlag"), "Sales", SUM(\'{table}\'[Sales]))',
            ),
            Event(),
        )
    )
    assert events[1]["data"]["kind"] == "formula"
    assert any("```dax" in e["data"].get("text", "") for e in events)


def test_conceptual_advice_cannot_offer_an_unrelated_creation_action(imported, monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, table = imported
    old_formula = f"SUM('{table}'[Sales])"

    def generate(system, message, response_model, **kwargs):
        assert old_formula not in message
        with pytest.raises(ValueError):
            response_model(
                answer="A measure summarizes data.", calculation_request="Create total Sales"
            )
        return response_model(
            answer="A measure evaluates in filter context; an imported calculated column "
            "stores a value per row.",
            calculation_request=None,
        )

    monkeypatch.setattr(service, "model", SimpleNamespace(generate=generate, unload=lambda: None))
    events = list(
        dax_assistant_events(
            service,
            DaxAssistRequest(
                workspace_id=workspace_id,
                snapshot_id=service.workspace(workspace_id).active_snapshot_id,
                request_id="concept-no-action",
                mode="auto",
                goal="How is a measure different from a calculated column?",
                expression=old_formula,
            ),
            Event(),
        )
    )
    assert not any(e["data"].get("calculation_request") for e in events)


def test_difference_is_bounded_and_grouped_totals_use_source_aggregates(imported):
    from ai_powerbi_analyst.studio.contracts import DaxEvaluateRequest

    service, workspace_id, table = imported

    def run(expr):
        return service.evaluate_dax(DaxEvaluateRequest(workspace_id=workspace_id, expression=expr))

    expr = f"SUM('{table}'[Sales])-SUM('{table}'[Qty])"
    assert list(run(expr).rows[0].values()) == [343]
    assert list(run(f"CALCULATE({expr})").rows[0].values()) == [343]
    assert list(run(f"CALCULATE({expr}, '{table}'[Region] = \"West\")").rows[0].values()) == [196]
    grouped = run(
        f"EVALUATE SUMMARIZECOLUMNS(ROLLUPADDISSUBTOTAL('{table}'[Region], \"TotalFlag\"), "
        f'"Net", {expr}) ORDER BY [TotalFlag] ASC, [Net] ASC'
    )
    assert [r["Net"] for r in grouped.rows] == [147, 196, 343]
    assert list(run(f"CALCULATE({expr}, '{table}'[Sales] < -1)").rows[0].values()) == [None]
    # Each side keeps its own aggregate: AVERAGE(Sales)=350/3 minus SUM(Qty)=7.
    mixed = run(expr.replace("SUM(", "AVERAGE(", 1)).rows[0]
    assert list(mixed.values()) == [pytest.approx(350 / 3 - 7)]
    for invalid in [
        expr + f"-SUM('{table}'[Qty])",
        expr.replace("[Qty]", "[Missing]"),
        f"DIVIDE(SUM('{table}'[Sales]), SUM('{table}'[Qty])) - SUM('{table}'[Qty])",
        expr + "; DROP TABLE orders",
    ]:
        with pytest.raises(ValueError):
            run(invalid)


def test_subtraction_handles_one_or_both_blank_operands(service, tmp_path):
    from ai_powerbi_analyst.studio.contracts import DaxEvaluateRequest

    source = tmp_path / "blanks.csv"
    source.write_text("Group,Revenue,Cost\nA,10,\nB,,3\nC,,\nD,5,8\n")
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Blank arithmetic",
            selected_folder=str(tmp_path),
            idempotency_key="blank-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="blank-import",
            expected_base_hash=workspace.base_hash,
        )
    )
    table = snapshot.tables[0].table.name
    result = service.evaluate_dax(
        DaxEvaluateRequest(
            workspace_id=workspace.id,
            expression=f"EVALUATE SUMMARIZECOLUMNS('{table}'[Group], \"Difference\", "
            f"SUM('{table}'[Revenue])-SUM('{table}'[Cost])) ORDER BY [Group] ASC",
        )
    )
    assert [(r["Group"], r["Difference"]) for r in result.rows] == [
        ("A", 10),
        ("B", -3),
        ("C", None),
        ("D", -3),
    ]


def test_lab_saved_measure_can_be_approved(
    imported: tuple[StudioService, str, str],
) -> None:
    """Lab measures record measure-graph dependencies, so approval must not
    mistake table/column provenance for unknown measure dependencies."""
    service, workspace_id, table = imported
    workspace = service.workspace(workspace_id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace_id,
            snapshot_id=workspace.active_snapshot_id,
            prompt="overview of sales",
            idempotency_key="dax-approve-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    saved = service.add_dax_measure(
        workspace_id,
        AddDaxMeasureRequest(
            workspace_id=workspace_id,
            name="Row Count",
            expression=f"COUNTROWS('{table}')",
            idempotency_key="dax-approve-save",
            expected_base_hash=draft.base_hash,
        ),
    )
    proposal = next(m for m in saved.measure_proposal.measures if m.name == "Row Count")
    assert proposal.dependencies == []
    approved = service.approve_advanced_dax(
        saved.id,
        proposal.id,
        ApproveAdvancedDaxRequest(
            workspace_id=workspace_id,
            draft_id=saved.id,
            measure_id=proposal.id,
            approved=True,
            approval_note="Reviewed COUNTROWS over the imported table.",
            idempotency_key="dax-approve-measure",
            expected_base_hash=saved.base_hash,
        ),
    )
    assert next(m for m in approved.measure_proposal.measures if m.name == "Row Count").approved


def test_lab_measure_referencing_governed_measure_records_dependency(
    imported: tuple[StudioService, str, str],
) -> None:
    service, workspace_id, table = imported
    workspace = service.workspace(workspace_id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace_id,
            snapshot_id=workspace.active_snapshot_id,
            prompt="overview of sales",
            idempotency_key="dax-dep-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    base = next(m for m in draft.measure_proposal.measures if m.name == "Total Sales")
    saved = service.add_dax_measure(
        workspace_id,
        AddDaxMeasureRequest(
            workspace_id=workspace_id,
            name="Sales Per Unit",
            expression=f"DIVIDE([Total Sales], SUM('{table}'[Qty]))",
            idempotency_key="dax-dep-save",
            expected_base_hash=draft.base_hash,
        ),
    )
    proposal = next(m for m in saved.measure_proposal.measures if m.name == "Sales Per Unit")
    assert proposal.dependencies == [base.id]


def _repairing_model(answers: list[str], seen: list[str]):
    """Guarded legacy fallback: decline pattern slots, then allow one DAX correction."""
    from types import SimpleNamespace

    def generate(system, message, response_model, **kwargs):
        if "pattern" in response_model.model_fields:
            return response_model(pattern=None)
        seen.append(system)
        try:
            return response_model(expression=answers[0])
        except ValueError as error:
            seen.append(str(error))
            return response_model(expression=answers[1])

    return SimpleNamespace(generate=generate, unload=lambda: None)


def test_generation_keeps_filters_the_request_names(imported, monkeypatch):
    from threading import Event

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest, DaxEvaluateRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, table = imported
    total = f"SUM('{table}'[Sales])"

    def ask(goal: str, answers: list[str], seen: list[str]) -> str:
        monkeypatch.setattr(service, "model", _repairing_model(answers, seen))
        events = dax_assistant_events(
            service,
            DaxAssistRequest(
                workspace_id=workspace_id,
                snapshot_id=service.workspace(workspace_id).active_snapshot_id,
                request_id="named-filter-" + str(len(goal)),
                goal=goal,
            ),
            Event(),
        )
        return "".join(e["data"].get("text", "") for e in events)

    def value(expr: str) -> list[object]:
        result = service.evaluate_dax(
            DaxEvaluateRequest(workspace_id=workspace_id, expression=expr, top_n=1)
        )
        return list(result.rows[0].values())

    # The schema tells the model which column holds "West". A formula that runs
    # but drops the named value gets it added, without a second model call.
    seen: list[str] = []
    fixed = f"CALCULATE({total}, '{table}'[Region] = \"West\")"
    assert fixed in ask("Calculate total Sales for the West region", [total, "unused"], seen)
    assert '"Region": "string: East|West"' in seen[0]
    assert len(seen) == 1
    assert value(fixed) == [200]
    # A negation cannot become an equality filter: the model must correct it.
    seen = []
    excluded = f"CALCULATE({total}, '{table}'[Region] <> \"West\")"
    goal = "Calculate total Sales excluding the West region"
    assert excluded in ask(goal, [total, excluded], seen)
    assert f"'{table}'[Region] = \"West\"" in seen[1]
    assert value(excluded) == [150]


@pytest.mark.parametrize(
    ("goal", "first", "second", "discussion", "reason"),
    [
        (
            "Calculate total Qty",
            "SUM('{t}'[Sales])",
            "SUM('{t}'[Qty])",
            "User: Calculate total Sales\nAssistant: ```dax\nSUM('{t}'[Sales])\n```",
            "earlier request",
        ),
        (
            "Calculate Sales minus Qty",
            "SUM('{t}'[Sales]) - SUM('{t}'[Sales])",
            "SUM('{t}'[Sales]) - SUM('{t}'[Qty])",
            "",
            "from itself",
        ),
    ],
)
def test_generation_rejects_repeated_or_degenerate_formulas(
    imported, monkeypatch, goal, first, second, discussion, reason
):
    from threading import Event

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, table = imported
    seen: list[str] = []
    answers = [first.format(t=table), second.format(t=table)]
    monkeypatch.setattr(service, "model", _repairing_model(answers, seen))
    events = list(
        dax_assistant_events(
            service,
            DaxAssistRequest(
                workspace_id=workspace_id,
                snapshot_id=service.workspace(workspace_id).active_snapshot_id,
                request_id="degenerate",
                goal=goal,
                discussion=discussion.format(t=table),
            ),
            Event(),
        )
    )
    assert reason in seen[1]
    assert answers[1] in "".join(e["data"].get("text", "") for e in events)


def test_time_intelligence_requires_a_reviewed_calendar_before_emitting_dax(service, tmp_path):
    from threading import Event
    from types import SimpleNamespace

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    source = tmp_path / "orders.csv"
    source.write_text(
        "Order Date,Region,Sales\n2023-01-05,East,100\n2024-01-07,West,150\n", encoding="utf-8"
    )
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="DAX time",
            selected_folder=str(tmp_path),
            idempotency_key="dax-time-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="dax-time-import",
            expected_base_hash=workspace.base_hash,
        )
    )

    def never(*_args, **_kwargs):
        raise AssertionError("time intelligence must not be guessed by the model")

    service.model = SimpleNamespace(generate=never, unload=lambda: None)

    def ask(goal: str) -> tuple[str, str]:
        events = list(
            dax_assistant_events(
                service,
                DaxAssistRequest(
                    workspace_id=workspace.id,
                    snapshot_id=snapshot.id,
                    request_id="time-" + str(abs(hash(goal))),
                    goal=goal,
                ),
                Event(),
            )
        )
        kind = next(e for e in events if e["event"] == "assistant")["data"]["kind"]
        return kind, "".join(e["data"].get("text", "") for e in events if e["event"] == "token")

    kind, text = ask("Calculate year over year Sales growth percentage")
    assert kind == "advice"
    assert "Choose the related calendar date key" in text
    assert "No formula or report change was applied" in text
    assert "```" not in text
    kind, text = ask("Calculate Sales YTD")
    assert kind == "advice"
    assert "Choose the related calendar date key" in text
    assert "```" not in text
    kind, text = ask("Calculate year to date")
    assert kind == "advice"
    assert "Name the model field" in text
    assert "```" not in text
    answered = [e for e in service.events(workspace.id) if e["type"] == "dax.assistant_answered"]
    # Replayed from the audit log, the explanation is not an applicable formula.
    assert {e["payload"]["mode"] for e in answered} == {"advice"}


def test_divide_combines_any_two_aggregates_with_dax_blank_semantics(imported):
    from ai_powerbi_analyst.studio.contracts import DaxEvaluateRequest

    service, workspace_id, table = imported

    def value(expr):
        result = service.evaluate_dax(
            DaxEvaluateRequest(workspace_id=workspace_id, expression=expr, top_n=5)
        )
        return [list(row.values()) for row in result.rows]

    per_row = f"DIVIDE(SUM('{table}'[Sales]), COUNTROWS('{table}'))"
    assert value(per_row) == [[pytest.approx(350 / 3)]]
    assert value(f"DIVIDE(SUM('{table}'[Sales]), DISTINCTCOUNT('{table}'[Region]))") == [[175]]
    grouped = value(f"EVALUATE SUMMARIZECOLUMNS('{table}'[Region], \"Avg\", {per_row})")
    assert sorted(grouped) == [["East", 75], ["West", 200]]
    # No rows: DIVIDE returns BLANK instead of failing on a zero denominator.
    assert value(f"CALCULATE({per_row}, '{table}'[Region] = \"North\")") == [[None]]
    with pytest.raises(ValueError, match="two single aggregates"):
        value(f"DIVIDE({per_row}, SUM('{table}'[Qty]))")


def test_average_order_value_counts_orders_not_rows(service, tmp_path, monkeypatch):
    from threading import Event

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest, DaxEvaluateRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    source = tmp_path / "lines.csv"
    # Order A has two lines: the row average (116.67) is not the order value (175).
    source.write_text(
        "Order ID,Region,Sales\nA,East,100\nA,East,50\nB,West,200\n", encoding="utf-8"
    )
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="DAX orders",
            selected_folder=str(tmp_path),
            idempotency_key="dax-aov-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="dax-aov-import",
            expected_base_hash=workspace.base_hash,
        )
    )
    table = snapshot.tables[0].table.name
    seen: list[str] = []
    monkeypatch.setattr(
        service, "model", _repairing_model([f"AVERAGE('{table}'[Sales])", "unused"], seen)
    )
    text = "".join(
        e["data"].get("text", "")
        for e in dax_assistant_events(
            service,
            DaxAssistRequest(
                workspace_id=workspace.id,
                snapshot_id=snapshot.id,
                request_id="aov",
                goal="Calculate average order value",
            ),
            Event(),
        )
    )
    expected = f"DIVIDE(SUM('{table}'[Sales]), DISTINCTCOUNT('{table}'[Order ID]))"
    assert expected in text
    assert len(seen) == 1
    result = service.evaluate_dax(
        DaxEvaluateRequest(workspace_id=workspace.id, expression=expected, top_n=1)
    )
    assert list(result.rows[0].values()) == [175]
    # A filtered answer keeps its filter; only the row average is replaced.
    seen.clear()
    wrapped = f"CALCULATE(AVERAGE('{table}'[Sales]), '{table}'[Region] = \"East\")"
    monkeypatch.setattr(service, "model", _repairing_model([wrapped, "unused"], seen))
    request = DaxAssistRequest(
        workspace_id=workspace.id,
        snapshot_id=snapshot.id,
        request_id="aov-east",
        goal="Calculate average order value for East",
    )
    text = "".join(
        e["data"].get("text", "") for e in dax_assistant_events(service, request, Event())
    )
    assert f"CALCULATE({expected}, '{table}'[Region] = \"East\")" in text
    assert len(seen) == 1


def test_value_ratios_and_scoped_shares_are_answered_as_written(imported, monkeypatch):
    from threading import Event

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest, DaxEvaluateRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    service, workspace_id, table = imported
    sales, qty = f"SUM('{table}'[Sales])", f"SUM('{table}'[Qty])"
    west = f"'{table}'[Region] = \"West\""
    ratio = f"DIVIDE({qty}, {sales})"
    scoped = f"CALCULATE(DIVIDE(CALCULATE({sales}, '{table}'[Sales] > 100), {sales}), {west})"
    # West scopes each request; only a named part of a total ("from the West
    # region") would move onto the numerator of a share.
    for goal, answer, expected in (
        ("Calculate Qty as a percentage of Sales for the West region", ratio, None),
        ("Calculate Qty as a percentage of Sales in the West region", ratio, None),
        ("Calculate Qty as a percentage of Sales within the West region", ratio, None),
        ("Show Qty as a share of Sales in the West region", ratio, None),
        ("Calculate the percentage of West region sales above 100", scoped, scoped),
        ("Calculate total Sales from the West region", sales, None),
        ("Calculate the contribution margin from the West region", sales, None),
    ):
        seen: list[str] = []
        monkeypatch.setattr(service, "model", _repairing_model([answer, "unused"], seen))
        events = dax_assistant_events(
            service,
            DaxAssistRequest(
                workspace_id=workspace_id,
                snapshot_id=service.workspace(workspace_id).active_snapshot_id,
                request_id="scoped-" + str(abs(hash(goal))),
                goal=goal,
            ),
            Event(),
        )
        text = "".join(e["data"].get("text", "") for e in events)
        formula = expected or f"CALCULATE({answer}, {west})"
        assert f"```dax\n{formula}\n```" in text, goal
        assert len(seen) == 1, goal
        result = service.evaluate_dax(
            DaxEvaluateRequest(workspace_id=workspace_id, expression=formula)
        )
        number = 4 / 200 if answer == ratio else 1 if answer == scoped else 200
        assert list(result.rows[0].values()) == [pytest.approx(number)], goal


def test_share_of_total_divides_the_named_part_by_the_total_in_context(
    service, tmp_path, monkeypatch
):
    from threading import Event

    from ai_powerbi_analyst.studio.contracts import DaxAssistRequest, DaxEvaluateRequest
    from ai_powerbi_analyst.studio.dax_assistant import dax_assistant_events

    source = tmp_path / "segments.csv"
    source.write_text(
        "Segment,Region,Sales\nConsumer,East,100\nCorporate,East,50\n"
        "Consumer,West,200\nCorporate,West,150\n",
        encoding="utf-8",
    )
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="DAX shares",
            selected_folder=str(tmp_path),
            idempotency_key="dax-share-create",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source)],
            idempotency_key="dax-share-import",
            expected_base_hash=workspace.base_hash,
        )
    )
    table = snapshot.tables[0].table.name
    sales = f"SUM('{table}'[Sales])"
    consumer = f"CALCULATE({sales}, '{table}'[Segment] = \"Consumer\")"
    share = f"DIVIDE({consumer}, {sales})"
    east = f"CALCULATE({share}, '{table}'[Region] = \"East\")"

    def ask(goal: str, answers: list[str], seen: list[str]) -> str:
        monkeypatch.setattr(service, "model", _repairing_model(answers, seen))
        request = DaxAssistRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            request_id="share-" + str(abs(hash((goal, *answers)))),
            goal=goal,
        )
        return "".join(
            e["data"].get("text", "") for e in dax_assistant_events(service, request, Event())
        )

    def value(expr: str) -> object:
        rows = service.evaluate_dax(
            DaxEvaluateRequest(workspace_id=workspace.id, expression=expr, top_n=10)
        ).rows
        return rows if len(rows) != 1 else next(iter(rows[0].values()))

    # Measured on Superstore: asked for this share, the 4B model wrote DIVIDE(x, x), and
    # the Consumer filter wrapped around it reached both sides: accepted at 1.
    goal = "Share of total sales that comes from the Consumer segment"
    seen: list[str] = []
    assert f"```dax\n{share}\n```" in ask(goal, [f"DIVIDE({sales}, {sales})", "unused"], seen)
    assert len(seen) == 1
    assert value(share) == pytest.approx(300 / 500)
    # Another named value filters the whole share, after a model answer or in the
    # rescue for a total the lab cannot parse.
    goal = "Share of sales in the East region that comes from the Consumer segment"
    unparsable = f"DIVIDE({consumer}, CALCULATE({sales}, ALL('{table}')))"
    for answer in (f"DIVIDE({sales}, {sales})", unparsable):
        seen = []
        assert f"```dax\n{east}\n```" in ask(goal, [answer, "unused"], seen), answer
        assert len(seen) == 1
    assert value(east) == pytest.approx(100 / 150)
    # The total is the one in filter context: by Region, Consumer's share of each region.
    assert value(
        f"EVALUATE SUMMARIZECOLUMNS('{table}'[Region], \"Share\", {share}) "
        f"ORDER BY '{table}'[Region] ASC"
    ) == [
        {"Region": "East", "Share": pytest.approx(100 / 150)},
        {"Region": "West", "Share": pytest.approx(200 / 350)},
    ]
    # By Segment itself Power BI would divide Consumer's sales by each segment's; the
    # lab refuses that grouping rather than differ.
    with pytest.raises(ValueError, match="replaces the grouping"):
        value(f"EVALUATE SUMMARIZECOLUMNS('{table}'[Segment], \"Share\", {share})")
