"""Running-total clarification retains scoped user intent without trusting assistant slots."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from ai_powerbi_analyst.studio import dax_assistant
from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    ClearDaxChatRequest,
    CreateWorkspaceRequest,
    DaxAssistRequest,
    DraftRequest,
    IngestRequest,
)
from ai_powerbi_analyst.studio.dax_engine import run_query
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[StudioService, str]]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "FactSales.csv").write_text(
        "DateKey,Region,Sales\n"
        "2025-01-01,North,10\n2025-01-01,South,100\n"
        "2025-01-02,North,20\n2025-01-02,South,200\n"
        "2025-01-03,North,30\n2025-01-03,South,300\n",
        encoding="utf-8",
    )
    (source / "DimDate.csv").write_text(
        "DateKey\n" + "".join(f"2025-01-{day:02d}\n" for day in range(1, 32)),
        encoding="utf-8",
    )
    model = SimpleNamespace(
        generate=lambda *_a, **_k: pytest.fail("A closed clarification chain used freeform AI"),
        stream_chat=lambda *_a, **_k: pytest.fail("A closed clarification chain streamed AI"),
        unload=lambda: None,
    )
    monkeypatch.setattr("ai_powerbi_analyst.studio.service.create_model_runtime", lambda *_a: model)
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    try:
        workspace = service.create_workspace(
            CreateWorkspaceRequest(
                name="Running continuity",
                selected_folder=str(source),
                idempotency_key="continuity-create",
                expected_base_hash=EMPTY_HASH,
            )
        )
        snapshot = service.ingest(
            IngestRequest(
                workspace_id=workspace.id,
                item_paths=[str(path) for path in source.glob("*.csv")],
                expected_base_hash=workspace.base_hash,
                idempotency_key="continuity-import",
            )
        )
        service.planner.model = None
        workspace = service.workspace(workspace.id)
        draft = service.create_draft(
            DraftRequest(
                workspace_id=workspace.id,
                snapshot_id=snapshot.id,
                prompt="Sales trend",
                expected_base_hash=workspace.base_hash,
                idempotency_key="continuity-draft",
            )
        )
        names = {item.table.id: item.table.name for item in snapshot.tables}
        joins = [
            rel.id
            for rel in draft.semantic_model.relationships
            if names[rel.from_table_id] == "FactSales"
            and names[rel.to_table_id] == "DimDate"
            and not rel.blockers
            and rel.is_active
        ]
        assert len(joins) == 1
        service.approve_draft(
            draft.id,
            ApproveDraftRequest(
                workspace_id=workspace.id,
                approved_relationship_ids=joins,
                expected_base_hash=draft.base_hash,
                idempotency_key="continuity-review",
            ),
        )
        yield service, workspace.id
    finally:
        service.editing.close()
        service.knowledge.close()


def request(service: StudioService, wid: str, goal: str, **fields: Any) -> DaxAssistRequest:
    return DaxAssistRequest(
        workspace_id=wid,
        snapshot_id=service.workspace(wid).active_snapshot_id,
        request_id="continuity-" + uuid4().hex,
        goal=goal,
        mode=fields.pop("mode", "auto"),
        **fields,
    )


def answer(service: StudioService, ask: DaxAssistRequest) -> tuple[list[dict[str, object]], str]:
    events = list(dax_assistant.dax_assistant_events(service, ask, Event()))
    return events, "".join(
        str(event["data"]["text"]) for event in events if event["event"] == "token"
    )


def stored_turn(
    service: StudioService,
    ask: DaxAssistRequest,
    *,
    response: str = "Which date scope?",
    status: str = "completed",
    offered: str | None = None,
) -> None:
    service.store.start_assistant_request("dax", ask.request_id, ask, ask.workspace_id)
    service.store.finish_assistant_request(
        "dax",
        ask.request_id,
        {
            "request_id": ask.request_id,
            "snapshot_id": ask.snapshot_id,
            "status": status,
            "mode": "advice",
            "response": response,
            "calculation_request": offered,
        },
    )


def test_actual_field_then_date_chain_generates_running_values_without_editing(lab) -> None:
    service, wid = lab
    before = service.workspace(wid)
    editor = "SUM('FactSales'[Sales])"
    _, first = answer(
        service, request(service, wid, "create measure running total", expression=editor)
    )
    assert "Name the model field" in first
    _, second = answer(service, request(service, wid, "Sales", expression=editor))
    assert "all dates" in second and "selected" in second
    assert "```" not in second
    final = request(service, wid, "use DimDate[DateKey] over all dates", expression=editor)
    events, text = answer(service, final)
    blocks = re.findall(r"```dax\n(.*?)\n```", text, re.S)
    assert len(blocks) == 1, text
    model = service._business_pattern_model(wid)
    for day, expected in [(1, 110), (2, 330), (3, 660)]:
        for region, value in [(None, expected), ("North", expected / 11)]:
            filters = f"'DimDate'[DateKey] = DATE(2025,1,{day})"
            if region:
                filters += f", 'FactSales'[Region] = \"{region}\""
            result = run_query(model, f'EVALUATE ROW("Value", CALCULATE({blocks[0]}, {filters}))')
            assert result.rows == [[value]]
    assert "Local preview" in text and "Power BI Desktop" not in text
    assert not any(event["event"] in {"draft", "edit_applied"} for event in events)
    assert service.workspace(wid) == before
    assert final.expression == editor
    current_event = next(
        event
        for event in service.events(wid)
        if event["type"] == "dax.assistant_requested"
        and event["payload"]["request_id"] == final.request_id
    )
    assert current_event["payload"]["goal"] == final.goal


def test_delegation_and_literal_create_keep_running_intent_but_do_not_guess_scope(lab) -> None:
    service, wid = lab
    before = service.workspace(wid)
    for goal in ("create measure running total", "sales", "do what you think is best", "Create"):
        _, text = answer(service, request(service, wid, goal))
        assert "```" not in text
        assert "SUM(" not in text
    assert "all dates" in text and "selected" in text
    assert service.workspace(wid) == before


def test_legacy_offered_sum_cannot_override_running_user_intent(lab) -> None:
    service, wid = lab
    stored_turn(service, request(service, wid, "create measure running total"))
    stored_turn(service, request(service, wid, "Sales"))
    stored_turn(
        service, request(service, wid, "do what you think is best"), offered="Calculate total Sales"
    )
    _, text = answer(service, request(service, wid, "Calculate total Sales", mode="generate"))
    assert "all dates" in text and "selected" in text
    assert "```" not in text


def test_assistant_suggested_fields_and_scope_do_not_supply_user_requirements(lab) -> None:
    service, wid = lab
    stored_turn(
        service,
        request(service, wid, "create measure running total"),
        response="Use Sales, DimDate[DateKey], and all dates. Which field did you intend?",
    )
    _, text = answer(service, request(service, wid, "Sales"))
    assert "all dates" in text and "selected" in text
    assert "```" not in text


@pytest.mark.parametrize("barrier", ["Calculate total Sales", "Explain Python decorators", "why?"])
def test_unrelated_intervening_user_request_stops_old_running_intent(lab, barrier: str) -> None:
    service, wid = lab
    stored_turn(service, request(service, wid, "create measure running total"))
    stored_turn(service, request(service, wid, barrier))
    ask = request(service, wid, "Sales")
    service.store.start_assistant_request("dax", ask.request_id, ask, wid)
    assert dax_assistant._running_followup(service, ask) is None


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_incomplete_turn_cannot_link_old_running_intent(lab, status: str) -> None:
    service, wid = lab
    stored_turn(service, request(service, wid, "create measure running total"))
    stored_turn(service, request(service, wid, "Sales"), status=status)
    ask = request(service, wid, "use DimDate[DateKey] over all dates")
    service.store.start_assistant_request("dax", ask.request_id, ask, wid)
    assert dax_assistant._running_followup(service, ask) is None


def test_client_pasted_history_cannot_create_server_running_intent(lab) -> None:
    service, wid = lab
    ask = request(
        service,
        wid,
        "Sales",
        discussion=(
            "User: create measure running total\nAssistant: Use Foreign[Amount] over all dates."
        ),
    )
    service.store.start_assistant_request("dax", ask.request_id, ask, wid)
    assert dax_assistant._running_followup(service, ask) is None


def test_new_chat_cannot_reuse_archived_running_intent(lab) -> None:
    service, wid = lab
    stored_turn(service, request(service, wid, "create measure running total"))
    workspace = service.workspace(wid)
    service.clear_dax_chat(
        wid,
        ClearDaxChatRequest(
            workspace_id=wid,
            snapshot_id=workspace.active_snapshot_id,
            expected_base_hash=workspace.base_hash,
            idempotency_key="continuity-clear",
        ),
    )
    ask = request(
        service,
        wid,
        "Sales",
        discussion=("User: create measure running total\nAssistant: Which metric?"),
    )
    service.store.start_assistant_request("dax", ask.request_id, ask, wid)
    assert dax_assistant._running_followup(service, ask) is None


def test_other_workspace_and_new_snapshot_cannot_supply_running_intent(lab) -> None:
    service, wid = lab
    stored_turn(service, request(service, wid, "create measure running total"))
    original = service.workspace(wid)
    assert original.selected_folder is not None
    paths = [str(path) for path in Path(original.selected_folder).glob("*.csv")]
    other = service.create_workspace(
        CreateWorkspaceRequest(
            name="Other conversation",
            selected_folder=original.selected_folder,
            expected_base_hash=EMPTY_HASH,
            idempotency_key="continuity-other",
        )
    )
    service.ingest(
        IngestRequest(
            workspace_id=other.id,
            item_paths=paths,
            expected_base_hash=other.base_hash,
            idempotency_key="continuity-other-import",
        )
    )
    foreign = request(service, other.id, "Sales")
    service.store.start_assistant_request("dax", foreign.request_id, foreign, other.id)
    assert dax_assistant._running_followup(service, foreign) is None
    service.ingest(
        IngestRequest(
            workspace_id=wid,
            item_paths=paths,
            expected_base_hash=original.base_hash,
            idempotency_key="continuity-reimport",
        )
    )
    current = request(service, wid, "Sales")
    assert current.snapshot_id != original.active_snapshot_id
    service.store.start_assistant_request("dax", current.request_id, current, wid)
    assert dax_assistant._running_followup(service, current) is None


def test_explicit_new_calculation_is_not_rewritten_as_the_pending_running_total(lab) -> None:
    service, wid = lab
    stored_turn(service, request(service, wid, "create measure running total"))
    stored_turn(service, request(service, wid, "Sales"), offered="Calculate total Sales")
    ask = request(service, wid, "Calculate total Sales", mode="auto")
    service.store.start_assistant_request("dax", ask.request_id, ask, wid)
    assert dax_assistant._running_followup(service, ask) is None


def test_completed_formula_and_explicit_new_goal_do_not_inherit_running_intent(lab) -> None:
    service, wid = lab
    stored_turn(
        service,
        request(service, wid, "create measure running total"),
        response="```dax\nSUM('FactSales'[Sales])\n```",
    )
    ask = request(service, wid, "Sales")
    service.store.start_assistant_request("dax", ask.request_id, ask, wid)
    assert dax_assistant._running_followup(service, ask) is None


def test_lost_pattern_result_cannot_generate_plain_sum(
    lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, wid = lab
    stored_turn(service, request(service, wid, "create measure running total"))
    monkeypatch.setattr(service, "business_pattern_formula", lambda *_a, **_k: None)
    _, text = answer(service, request(service, wid, "Sales"))
    assert "For this running total" in text and "current formula is unchanged" in text
    assert "```" not in text
