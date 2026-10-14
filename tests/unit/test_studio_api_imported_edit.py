"""The HTTP API of imported-report editing: every route, its shapes and its error codes.

The real app factory and a real StudioService over a temporary state folder; Power BI Desktop
is the in-memory fake of the edit-pipeline tests and the report is the structure twin.
"""

from __future__ import annotations

import copy
import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from editing_support import (
    TWIN,
    VISUAL,
    FakeDesktop,
    make_report,
    patch_diff,
    read_measures,
    sha,
)
from fastapi.testclient import TestClient

from ai_powerbi_analyst.studio import live_report, native_report, report_revisions
from ai_powerbi_analyst.studio import service as service_module
from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import LocalWorkspace, WorkspaceKind
from ai_powerbi_analyst.studio.desktop_session import DesktopReadiness
from ai_powerbi_analyst.studio.imported_editing import ImportedEditingService
from ai_powerbi_analyst.studio.live_report import Unsupported
from ai_powerbi_analyst.studio.report_import import ImportedReport, read_report
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH

TOKEN = "imported-edit-api-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
WS = "workspace-1"
BASE = f"/v1/workspaces/{WS}/imported-report"
READY = DesktopReadiness(True, True, True, True, "2.157.1354.0", [])
MOVE = {"op": "set_position", "visual": VISUAL, "x": 50}
CREATE = {
    "op": "create_measure",
    "table": "# Measures",
    "name": "Avg Discount",
    "expression": "1+1",
}
PAGE_ID = "ReportSection8454c812b60b8bb83b99"


@pytest.fixture(autouse=True)
def _doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_diff(monkeypatch)
    monkeypatch.setattr(report_revisions, "RETRY_DELAY", 0.01)


@dataclass
class Api:
    client: TestClient
    service: StudioService
    desktop: FakeDesktop
    root: Path
    launches: list[str]

    def post(self, path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return self.client.post(f"{BASE}/{path}", headers=HEADERS, json=body or {}, **kwargs)

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.client.get(f"{BASE}/{path}", headers=HEADERS, **kwargs)

    def edit(self, key: str, ops: list[dict[str, Any]] | None = None, **fields: Any) -> Any:
        head = self.get("history").json()[0]["id"]
        body = {
            "operations": ops or [MOVE],
            "expected_head": head,
            "idempotency_key": key,
            **fields,
        }
        return self.post("edits", body)

    @property
    def working(self) -> Path:
        return self.service.editing.editor.store(self.service.imported_report(WS)).working_copy

    def open_desktop(self, *, normalize: bool = True) -> None:
        store = self.service.editing.editor.store(self.service.imported_report(WS))
        store.materialize()
        self.desktop.open(store.working_copy, normalize=normalize)


@pytest.fixture
def api(tmp_path: Path) -> Any:
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    report, _revisions, _source = make_report(tmp_path)
    pages, layout, _ = read_report(Path(report.archived_path))
    report = report.model_copy(update={"pages": pages, "format": layout})
    now = datetime.now(UTC)
    service.store.put(
        "workspace",
        LocalWorkspace(
            id=WS,
            name="Agents",
            kind=WorkspaceKind.IMPORTED_REPORT,
            imported_report_id=report.id,
            base_hash=sha(Path(report.archived_path)),
            created_at=now,
            updated_at=now,
        ),
        workspace_id=WS,
        expected_base_hash=EMPTY_HASH,
        idempotency_key="seed-workspace",
        mutation_payload={"seed": True},
    )
    service.store.put(
        "imported_report",
        report,
        workspace_id=WS,
        expected_base_hash=EMPTY_HASH,
        idempotency_key="seed-report",
        mutation_payload={"seed": True},
    )
    desktop = FakeDesktop()
    launches: list[str] = []
    service.editing = ImportedEditingService(
        service.state_root,
        service.store,
        load_report=service.imported_report,
        port_factory=lambda: desktop,
        readiness_factory=lambda: READY,
        read_measures=read_measures,
        read_types=lambda path: {"columns": [], "measures": []},
        before_launch=lambda: launches.append("launch"),
        wait_seconds=10.0,
        disk_free=lambda _path: 10**12,
        profile_dir=tmp_path,
    )
    with TestClient(create_app(service, TOKEN)) as client:
        yield Api(client, service, desktop, tmp_path, launches)


def error_body(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert body["detail"] == body["message"] or code == "needs_confirmation"
    assert body["message"]
    return dict(body)


# --- auth ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", ""),
        ("POST", "edits"),
        ("GET", "edits/any-batch"),
        ("POST", "edits/any-batch/cancel"),
        ("POST", "undo"),
        ("GET", "history"),
        ("GET", "history/r0001/discard-preview"),
        ("POST", "restore"),
        ("POST", "save-copy"),
        ("POST", "replace-original"),
        ("GET", "format-catalog?visual_type=card"),
        ("POST", f"pages/{PAGE_ID}/render"),
        ("POST", f"pages/{PAGE_ID}/render-formatting"),
        ("POST", f"pages/{PAGE_ID}/preview"),
    ],
)
def test_every_imported_editing_route_requires_the_bearer_token(
    api: Api, method: str, path: str
) -> None:
    url = f"{BASE}/{path}".rstrip("/")
    assert api.client.request(method, url, json={} if method == "POST" else None).status_code == 401
    wrong = {"Authorization": "Bearer nope-nope-nope-nope"}
    assert api.client.request(method, url, headers=wrong).status_code == 401
    assert api.client.post(f"/v1/workspaces/{WS}/open-source-report").status_code == 401


# --- the report payload ---------------------------------------------------------------------------


def test_the_report_route_keeps_its_fields_and_adds_head_delivery_capabilities_desktop(
    api: Api,
) -> None:
    response = api.get("")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "report-1" and len(body["pages"]) == 1
    assert body["pages"][0]["id"] == PAGE_ID
    assert body["head"]["id"] == "r0000" and body["head"]["edited"] is False
    assert body["delivery"]["state"] == "in_sync"
    support = body["capabilities"].pop("model_support")
    assert body["capabilities"] == {
        "edit_report": True,
        "edit_model": True,
        "reasons": [],
        "thin_report": False,
    }
    assert set(support) == {
        "desktop_version",
        "documented",
        "outside",
        "depends",
        "verified",
        "keys",
        "verified_keys",
    }
    assert support["desktop_version"] == "2.157.1354.0"
    assert body["desktop"]["state"] == "closed"
    assert response.headers["cache-control"] == "no-store"


def test_the_query_check_route_answers_without_an_edit(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_powerbi_analyst.studio import report_edit_model

    monkeypatch.setattr(
        report_edit_model, "M_CHECKER", lambda text: ["Expected 'in'"] if "oops" in text else []
    )
    ok = api.post("model/validate-query", {"expression": "let a = 1 in a"})
    assert ok.status_code == 200, ok.text
    assert ok.json() == {
        "valid": True,
        "checked": True,
        "message": None,
        "problems": [],
        "warnings": [],
        "table": None,
    }
    bad = api.post("model/validate-query", {"expression": "let oops", "table": "DimChannel"})
    assert bad.status_code == 200
    assert bad.json()["message"] == "The Power Query formula is not valid: Expected 'in'"
    assert bad.json()["table"] == "DimChannel"
    assert api.post("model/validate-query", {"expression": ""}).status_code == 422
    assert api.post("model/validate-query", {"expression": "1", "sql": "x"}).status_code == 422
    assert api.get("history").json()[0]["id"] == "r0000" and api.desktop.events == []


# --- edits ----------------------------------------------------------------------------------------


def test_an_edit_answers_200_with_the_receipt_and_the_report_moves_to_the_new_head(
    api: Api,
) -> None:
    response = api.edit("api-edit-key-01")
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["revision"]["id"] == "r0001" and receipt["revision"]["author"] == "studio"
    assert receipt["head"]["id"] == "r0001"
    assert receipt["summaries"] and receipt["revision"]["summary"] == " ".join(receipt["summaries"])
    assert receipt["outcome"] == "updated" and receipt["desktop"] == "closed"
    assert [(i["op"], i["where"]) for i in receipt["visible_in"]] == [("set_position", "studio")]
    assert receipt["adopted"] is None and receipt["protected"] == [] and receipt["warnings"] == []
    report = api.get("").json()
    assert report["head"] == {
        "id": "r0001",
        "hash": receipt["head"]["hash"],
        "edited": True,
        "desktop_version": None,
    }
    assert report["delivery"]["state"] == "edited"
    moved = next(v for p in report["pages"] for v in p["visuals"] if v["id"] == VISUAL)
    assert moved["x"] == 50.0
    assert [row["id"] for row in api.get("history").json()] == ["r0001", "r0000"]


def test_an_idempotent_replay_gives_the_same_answer_and_a_different_request_conflicts(
    api: Api,
) -> None:
    first = api.edit("api-edit-key-01")
    body = {"operations": [MOVE], "expected_head": "r0000", "idempotency_key": "api-edit-key-01"}
    replay = api.post("edits", body)  # the retry a client sends when it never saw the answer
    assert replay.status_code == 200 and replay.json() == first.json()
    assert len(api.get("history").json()) == 2
    other = api.post(
        "edits",
        {**body, "operations": [{"op": "set_position", "visual": VISUAL, "x": 99}]},
    )
    error_body(other, 409, "idempotency_conflict")


def test_the_request_body_is_closed_and_validated(api: Api) -> None:
    good = {"operations": [MOVE], "expected_head": "r0000", "idempotency_key": "api-edit-key-01"}
    assert api.post("edits", {**good, "surprise": True}).status_code == 422
    assert api.post("edits", {**good, "idempotency_key": "short"}).status_code == 422
    assert api.post("edits", {**good, "expected_head": "head"}).status_code == 422
    assert api.post("edits", {**good, "operations": []}).status_code == 422
    assert api.post("edits", {**good, "desktop_work": "always"}).status_code == 422
    assert api.post("edits", {"operations": [MOVE]}).status_code == 422
    assert len(api.get("history").json()) == 1


def test_an_answer_to_a_question_is_written_over_the_operation_it_belongs_to(api: Api) -> None:
    """The client sends the option the user picked as ``answers``; the server merges the option's
    fill into the operation at that step, so the edit is applied as if it had been sent whole."""
    body = {
        "operations": [{"op": "set_position", "visual": VISUAL, "x": 50}],
        "expected_head": "r0000",
        "idempotency_key": "api-answer-key-1",
        "answers": [{"step": 1, "fill": {"y": 77}}],
    }
    response = api.post("edits", body)
    assert response.status_code == 200, response.text
    report = api.get("").json()
    moved = next(v for p in report["pages"] for v in p["visuals"] if v["id"] == VISUAL)
    assert (moved["x"], moved["y"]) == (50.0, 77.0)
    bad = {**body, "idempotency_key": "api-answer-key-2", "expected_head": "r0001"}
    assert api.post("edits", {**bad, "answers": [{"step": 1, "fill": {}}]}).status_code == 422
    assert api.post("edits", {**bad, "answers": [{"step": 0, "fill": {"y": 1}}]}).status_code == 422
    assert api.post("edits", {**bad, "answers": [{"step": 9, "fill": {"y": 1}}]}).status_code == 422
    changed_op = {"step": 1, "fill": {"op": "delete_visual"}}
    assert api.post("edits", {**bad, "answers": [changed_op]}).status_code == 422


def test_a_stale_head_is_a_409_naming_both_heads(api: Api) -> None:
    api.edit("api-edit-key-01")
    stale = api.post(
        "edits",
        {"operations": [MOVE], "expected_head": "r0000", "idempotency_key": "api-edit-key-02"},
    )
    body = error_body(stale, 409, "head_moved")
    assert (body["expected"], body["actual"]) == ("r0000", "r0001")


def test_operations_that_cannot_apply_are_422_with_a_step_and_plain_words(api: Api) -> None:
    unknown = api.edit("api-edit-key-01", [{"op": "set_sparkles", "visual": VISUAL}])
    body = error_body(unknown, 422, "edit_unsupported")
    assert body["capability"] == "set_sparkles" and body["desktop_steps"] == []
    invalid = api.edit("api-edit-key-02", [MOVE, {"op": "add_shape", "page": "1", "fill": "nope"}])
    body = error_body(invalid, 422, "edit_rejected")
    assert body["step"] == 2 and "'fill'" in body["message"]
    missing = api.edit("api-edit-key-03", [{"op": "delete_visual", "visual": "not-there"}])
    error_body(missing, 422, "edit_rejected")
    assert api.get("").json()["head"]["id"] == "r0000"


def test_desktop_states_that_block_an_edit_are_409s_and_never_change_the_report(
    api: Api,
) -> None:
    api.open_desktop(normalize=False)  # Desktop has unsaved changes
    unsaved = api.edit("api-edit-key-01")
    body = error_body(unsaved, 409, "desktop_unsaved")
    assert body["choices"] == ["save_and_continue", "cancel"]
    # The user's answer, "save them and continue", is the same request with desktop_work set.
    saved = api.post(
        "edits",
        {
            "operations": [MOVE],
            "expected_head": "r0000",
            "idempotency_key": "api-edit-key-01",
            "desktop_work": "save",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["desktop"] == "reopened"
    api.desktop.blocking = ["Save changes?"]
    dialog = api.edit("api-edit-key-02", [{"op": "set_position", "visual": VISUAL, "x": 70}])
    body = error_body(dialog, 409, "desktop_dialog")
    assert body["dialog"] == "Save changes?"
    assert api.get("").json()["head"]["id"] == "r0001"


def test_a_model_change_needs_confirmation_and_a_ready_desktop(api: Api) -> None:
    unconfirmed = api.edit("api-model-key-1", [CREATE])
    body = error_body(unconfirmed, 409, "needs_confirmation")
    assert body["kind"] == "model_edit"
    assert body["detail"] == "Create the measure Avg Discount in # Measures."
    confirmed = api.edit("api-model-key-2", [CREATE], confirm_model=True)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["summaries"]
    report = api.get("").json()
    assert "Avg Discount" in {m["Name"] for m in report["measures"]}
    assert report["head"]["id"] == "r0001"


def test_a_model_change_without_desktop_tools_is_a_503_with_the_reasons(api: Api) -> None:
    not_ready = DesktopReadiness(True, False, True, False, "2.157", ["pbi-cli is not installed"])
    api.service.editing._readiness_factory = lambda: not_ready  # noqa: SLF001
    api.service.editing.forget_desktop()
    response = api.edit("api-model-key-1", [CREATE], confirm_model=True)
    body = error_body(response, 503, "desktop_unavailable")
    assert body["reasons"] == ["pbi-cli is not installed"]
    assert api.edit("api-edit-key-01").status_code == 200  # report edits never need it


def test_a_long_batch_answers_202_then_is_polled_and_a_queued_one_can_be_cancelled(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    editing = api.service.editing
    editing.wait_seconds = 0.05
    started, release = threading.Event(), threading.Event()
    real = editing.editor.apply

    def slow(report: ImportedReport, ops: Any, **kwargs: Any) -> Any:
        started.set()
        assert release.wait(20)
        return real(report, ops, **kwargs)

    monkeypatch.setattr(editing.editor, "apply", slow)
    accepted = api.post(
        "edits",
        {"operations": [MOVE], "expected_head": "r0000", "idempotency_key": "api-slow-key-1"},
    )
    assert accepted.status_code == 202, accepted.text
    batch = accepted.json()
    assert batch["state"] == "running" and batch["kind"] == "edit" and batch["batch_id"]
    assert started.wait(10)
    polled = api.get(f"edits/{batch['batch_id']}")
    assert polled.status_code == 200
    assert polled.json() == {
        "batch_id": batch["batch_id"],
        "state": "running",
        "kind": "edit",
        "stage": "planning",
        "label": "Getting ready",
    }
    queued = api.post(
        "edits",
        {
            "operations": [{"op": "set_position", "visual": VISUAL, "x": 70}],
            "expected_head": "r0000",
            "idempotency_key": "api-slow-key-2",
        },
    ).json()
    assert queued["state"] == "queued"
    cancelled = api.post(f"edits/{queued['batch_id']}/cancel").json()
    assert cancelled["cancelled"] is True and cancelled["state"] == "failed"
    assert cancelled["error"]["code"] == "cancelled"
    refused = api.post(f"edits/{batch['batch_id']}/cancel").json()
    assert refused["cancelled"] is False and refused["state"] == "running"
    release.set()
    handle = editing._batches[batch["batch_id"]]  # noqa: SLF001
    assert handle.wait(10)
    done = api.get(f"edits/{batch['batch_id']}").json()
    assert done["state"] == "done" and done["receipt"]["revision"]["id"] == "r0001"
    failed = api.get(f"edits/{queued['batch_id']}").json()
    assert failed["state"] == "failed" and failed["error"]["code"] == "cancelled"
    assert api.get("edits/no-such-batch").status_code == 404


# --- undo, history, restore -----------------------------------------------------------------------


def test_undo_history_and_restore_routes(api: Api) -> None:
    api.edit("api-edit-key-01")
    api.edit("api-edit-key-02", [{"op": "set_position", "visual": VISUAL, "x": 70}])
    undone = api.post("undo", {"expected_head": "r0002", "idempotency_key": "api-undo-key-1"})
    assert undone.status_code == 200, undone.text
    assert undone.json()["revision"]["author"] == "undo"
    assert undone.json()["revision"]["id"] == "r0003"
    replay = api.post("undo", {"expected_head": "r0002", "idempotency_key": "api-undo-key-1"})
    assert replay.status_code == 200 and replay.json()["revision"]["id"] == "r0003"
    stale = api.post("undo", {"expected_head": "r0001", "idempotency_key": "api-undo-key-2"})
    error_body(stale, 409, "head_moved")
    rows = api.get("history").json()
    assert [r["id"] for r in rows] == ["r0003", "r0002", "r0001", "r0000"]
    assert rows[0]["author"] == "undo" and rows[0]["restores"] == "r0001"
    assert rows[0]["is_head"] is True and rows[0]["can_undo"] is True
    assert rows[1]["operations"] == [{"op": "set_position", "summary": "Moved or resized a visual"}]
    assert set(rows[1]) >= {
        "id",
        "parent",
        "author",
        "summary",
        "created",
        "size",
        "available",
        "is_head",
        "can_undo",
        "conflict",
        "operations",
    }
    preview = api.get("history/r0000/discard-preview")
    assert preview.status_code == 200
    assert [r["id"] for r in preview.json()["would_discard"]] == ["r0001"]
    error_body(api.get("history/r0009/discard-preview"), 422, "edit_rejected")
    unconfirmed = api.post(
        "restore",
        {"revision_id": "r0000", "expected_head": "r0003", "idempotency_key": "api-restore-01"},
    )
    body = error_body(unconfirmed, 409, "needs_confirmation")
    assert body["kind"] == "restore_discard"
    assert [r["id"] for r in body["would_discard"]] == ["r0001"]
    restored = api.post(
        "restore",
        {
            "revision_id": "r0000",
            "expected_head": "r0003",
            "idempotency_key": "api-restore-01",
            "confirmed_discard": True,
        },
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["revision"]["author"] == "restore"
    assert api.get("").json()["head"]["edited"] is False  # the original's content again
    nothing = api.post(
        "restore",
        {"revision_id": "r0009", "expected_head": "r0004", "idempotency_key": "api-restore-02"},
    )
    error_body(nothing, 422, "edit_rejected")


# --- delivery -------------------------------------------------------------------------------------


def test_save_a_copy_route_writes_a_new_file_and_never_replaces_one(api: Api) -> None:
    api.edit("api-edit-key-01")
    folder = api.root / "Out"
    folder.mkdir()
    destination = str(folder / "Copy.pbix")
    first = api.post("save-copy", {"destination": destination, "idempotency_key": "api-copy-key-1"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == {"path", "hash"}
    assert Path(body["path"]).name == "Copy.pbix" and sha(Path(body["path"])) == body["hash"]
    assert body["hash"] == api.get("").json()["head"]["hash"]
    second = api.post(
        "save-copy", {"destination": destination, "idempotency_key": "api-copy-key-2"}
    )
    assert Path(second.json()["path"]).name == "Copy (2).pbix"
    same = api.post("save-copy", {"destination": destination, "idempotency_key": "api-copy-key-1"})
    assert same.json() == body
    assert sorted(p.name for p in folder.iterdir()) == ["Copy (2).pbix", "Copy.pbix"]


@pytest.mark.parametrize(
    ("destination", "message"),
    [
        ("STATE/Copy.pbix", "where Studio keeps its own files"),
        ("OUT/notes.txt", "Save the report as a .pbix file"),
        ("relative.pbix", "Choose a full path"),
        ("OUT/missing/Copy.pbix", "does not exist"),
    ],
)
def test_save_a_copy_route_refuses_places_it_may_not_write(
    api: Api, destination: str, message: str
) -> None:
    (api.root / "Out").mkdir()
    target = destination.replace("STATE", str(api.root / "state")).replace(
        "OUT", str(api.root / "Out")
    )
    response = api.post("save-copy", {"destination": target, "idempotency_key": "api-copy-key-1"})
    body = error_body(response, 422, "edit_rejected")
    assert message in body["message"]


def test_replace_original_route_needs_the_confirmation_and_an_unchanged_original(
    api: Api,
) -> None:
    api.edit("api-edit-key-01")
    assert api.post("replace-original", {"idempotency_key": "api-replace-01"}).status_code == 422
    assert (
        api.post(
            "replace-original", {"idempotency_key": "api-replace-01", "confirm": False}
        ).status_code
        == 422
    )
    source = Path(api.get("").json()["source_path"])
    done = api.post("replace-original", {"idempotency_key": "api-replace-01", "confirm": True})
    assert done.status_code == 200, done.text
    body = done.json()
    assert Path(body["path"]) == source and sha(source) == body["hash"]
    assert Path(body["backup"]).is_file() and ".studio-backup-" in body["backup"]
    assert api.get("").json()["delivery"]["state"] == "in_sync"
    api.edit("api-edit-key-02", [{"op": "set_position", "visual": VISUAL, "x": 70}])
    source.write_bytes(source.read_bytes() + b" the user kept working")
    refused = api.post("replace-original", {"idempotency_key": "api-replace-02", "confirm": True})
    body = error_body(refused, 422, "edit_rejected")
    assert "changed outside Studio" in body["message"]
    assert api.get("").json()["delivery"]["state"] == "source_changed"


def test_open_source_report_route_opens_the_head_once(api: Api) -> None:
    api.edit("api-edit-key-01")
    opened = api.client.post(f"/v1/workspaces/{WS}/open-source-report", headers=HEADERS)
    assert opened.status_code == 200, opened.text
    assert opened.json() == {"launch_requested": True, "already_open": False}
    assert api.launches == ["launch"]
    again = api.client.post(f"/v1/workspaces/{WS}/open-source-report", headers=HEADERS)
    assert again.json() == {"launch_requested": True, "already_open": True}
    assert api.launches == ["launch"]
    assert api.get("").json()["desktop"]["state"] in {"open", "open_unsaved"}
    unknown = api.client.post("/v1/workspaces/workspace-nope/open-source-report", headers=HEADERS)
    assert unknown.status_code == 404


# --- the format catalog ---------------------------------------------------------------------------


def test_format_catalog_route_returns_the_panes_sections(api: Api) -> None:
    response = api.get("format-catalog", params={"visual_type": "clusteredColumnChart"})
    assert response.status_code == 200
    body = response.json()
    assert body["visual_type"] == "clusteredColumnChart" and body["family"] == "chart"
    assert [s["id"] for s in body["sections"]] == [
        "size_position",
        "number_format",
        "conditional_formatting",
    ]
    controls = {c["id"]: c for s in body["sections"] for c in s["controls"]}
    assert controls["labels.labelDisplayUnits"]["studio_render"] is True
    assert controls["labels.labelDisplayUnits"]["op"]["name"] == "set_number_format"
    card = api.get("format-catalog", params={"visual_type": "cardVisual"}).json()
    assert card["instance_selector"] == {"id": "default", "states": ["default"]}
    unknown = api.get("format-catalog", params={"visual_type": "notAVisual"})
    assert unknown.status_code == 404
    assert "notAVisual" in unknown.json()["detail"]
    assert api.get("format-catalog").status_code == 422


# --- render and preview ---------------------------------------------------------------------------


@pytest.fixture
def stub_render(monkeypatch: pytest.MonkeyPatch, api: Api) -> list[dict[str, Any]]:
    """Studio's engine replaced by a recorder: what is tested is the route's revision handling,
    not the DAX engine (test_live_report and test_report_engine cover that)."""
    calls: list[dict[str, Any]] = []

    def render(engine: Any, report: Any, page_id: str, request: Any, **kwargs: Any) -> Any:
        calls.append({"page": page_id, **kwargs})
        return {"visuals": [], "revision": kwargs.get("revision"), "engine": "stub"}

    monkeypatch.setattr(service_module, "render_page", render)
    monkeypatch.setattr(service_module, "report_model", lambda report, snapshot: object())
    monkeypatch.setattr(api.service, "snapshot", lambda _id: object())
    monkeypatch.setattr(service_module, "LocalReportEngine", lambda model: object())
    return calls


def test_render_route_echoes_the_revision_and_refuses_an_old_head(
    api: Api, stub_render: list[dict[str, Any]]
) -> None:
    path = f"pages/{PAGE_ID}/render"
    current = api.post(path, params={"revision": "r0000"})
    assert current.status_code == 200 and current.json()["revision"] == "r0000"
    assert stub_render == [{"page": PAGE_ID, "revision": "r0000"}]
    without = api.post(path)  # a client that does not send it still works
    assert without.status_code == 200 and without.json()["revision"] is None
    api.edit("api-edit-key-01")
    stale = api.post(path, params={"revision": "r0000"})
    body = error_body(stale, 409, "head_moved")
    assert (body["expected"], body["actual"]) == ("r0000", "r0001")
    assert len(stub_render) == 2  # nothing was drawn for the stale request
    assert api.post(path, params={"revision": "r0001"}).json()["revision"] == "r0001"
    assert api.post(path, params={"revision": "head"}).status_code == 422


def test_render_route_maps_a_page_that_needs_desktop_to_its_own_code(
    api: Api, monkeypatch: pytest.MonkeyPatch, stub_render: list[dict[str, Any]]
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise Unsupported("This slicer's saved selection needs Power BI Desktop")

    monkeypatch.setattr(service_module, "render_page", refuse)
    response = api.post(f"pages/{PAGE_ID}/render", params={"revision": "r0000"})
    body = error_body(response, 422, "needs_desktop")
    assert body["needs_desktop"] is True and "saved selection" in body["message"]


def test_render_formatting_route_runs_no_query_and_checks_the_revision(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Any, ...]] = []

    def payload(report: Any, page_id: str, request: Any, only: Any, revision: Any) -> Any:
        seen.append((page_id, only, revision))
        return {"visuals": {}, "revision": revision, "head_hash": "h"}

    monkeypatch.setattr(service_module, "render_formatting_payload", payload)
    path = f"pages/{PAGE_ID}/render-formatting"
    response = api.post(path, params={"revision": "r0000", "only": [VISUAL, "other"]})
    assert response.status_code == 200 and response.json()["revision"] == "r0000"
    assert seen == [(PAGE_ID, {VISUAL, "other"}, "r0000")]
    api.post(path)
    assert seen[-1] == (PAGE_ID, None, None)
    api.edit("api-edit-key-01")
    error_body(api.post(path, params={"revision": "r0000"}), 409, "head_moved")
    assert len(seen) == 2

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise Unsupported("A slicer selection needs Power BI Desktop")

    monkeypatch.setattr(service_module, "render_formatting_payload", refuse)
    error_body(api.post(path, params={"revision": "r0001"}), 422, "needs_desktop")


def test_render_formatting_of_a_page_the_compiler_cannot_read_is_a_needs_desktop_answer(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real formatting render on the twin: its model is random bytes, so a slicer's saved
    selection cannot be compiled and the whole page would be drawn unfiltered. The compiler
    refuses (live_report.Unsupported) and the route says so instead of failing."""
    monkeypatch.setattr(live_report, "model_formats", lambda archive, blob: {})
    api.edit("api-edit-key-01", [{"op": "set_position", "visual": VISUAL, "x": 130}])
    response = api.post(f"pages/{PAGE_ID}/render-formatting", params={"revision": "r0001"})
    body = error_body(response, 422, "needs_desktop")
    assert body["needs_desktop"] is True
    assert "Power BI" in body["message"]


class FakePreviewEngine:
    """Stands in for NativeReportPreview: the screenshot itself needs a live Desktop."""

    def __init__(self, cached: bool = False) -> None:
        self.cached = cached
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None

    def preview_path(self, report: Any, page_id: str) -> Any:
        return SimpleNamespace(is_file=lambda: self.cached)

    def page(self, report: Any, page_id: str, refresh: bool, *, head_hash: str | None) -> Any:
        self.calls.append((page_id, refresh, head_hash))
        if self.error is not None:
            raise self.error
        return {"image": "data:image/png;base64,AA==", "page_id": page_id, "head_hash": head_hash}


def test_preview_route_previews_only_the_head_and_says_why_it_cannot(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakePreviewEngine()
    monkeypatch.setattr(api.service, "native_report_engine", lambda: engine)
    path = f"pages/{PAGE_ID}/preview"
    head = api.get("").json()["head"]["hash"]
    ok = api.post(path, params={"revision": "r0000", "refresh": "true"})
    assert ok.status_code == 200 and ok.json()["head_hash"] == head
    assert engine.calls == [(PAGE_ID, True, head)]
    # An older head is refused before Desktop is asked.
    api.edit("api-edit-key-01")
    body = error_body(api.post(path, params={"revision": "r0000"}), 409, "head_moved")
    assert (body["expected"], body["actual"]) == ("r0000", "r0001")
    assert len(engine.calls) == 1
    # The engine's own "out of date" refusals become 409s with the same plain words.
    engine.error = ValueError(native_report.STALE_VIEW)
    body = error_body(api.post(path, params={"revision": "r0001"}), 409, "head_moved")
    assert body["message"] == native_report.STALE_VIEW
    engine.error = ValueError(native_report.STALE_DESKTOP)
    body = error_body(api.post(path, params={"revision": "r0001"}), 409, "desktop_stale")
    assert body["message"] == native_report.STALE_DESKTOP
    engine.error = ValueError("Open this report in Power BI Desktop, wait for it to load")
    other = api.post(path, params={"revision": "r0001"})
    assert other.status_code == 422 and "wait for it to load" in other.json()["detail"]


def test_preview_route_needs_desktop_saved_unless_the_picture_is_already_kept(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakePreviewEngine()
    monkeypatch.setattr(api.service, "native_report_engine", lambda: engine)
    path = f"pages/{PAGE_ID}/preview"
    api.open_desktop(normalize=False)  # Desktop shows the head but has unsaved changes
    body = error_body(api.post(path, params={"revision": "r0000"}), 409, "desktop_unsaved")
    assert "preview would not match Studio" in body["message"]
    assert engine.calls == []
    engine.cached = True  # a picture kept for this head needs no Desktop
    assert api.post(path, params={"revision": "r0000"}).status_code == 200
    assert len(engine.calls) == 1


# --- the app around it ----------------------------------------------------------------------------


def test_the_app_recovers_interrupted_batches_at_startup_and_waits_for_batches_at_shutdown(
    tmp_path: Path,
) -> None:
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    calls: list[str] = []
    service.editing = SimpleNamespace(  # type: ignore[assignment]
        start_recovery=lambda: calls.append("recover"),
        close=lambda: calls.append("close"),
    )
    with TestClient(create_app(service, TOKEN)) as client:
        assert calls == ["recover"]
        assert client.get("/v1/ping", headers=HEADERS).status_code == 200
    assert calls == ["recover", "close"]


def test_a_second_workspace_is_not_readable_through_another_ones_batch_ids(
    api: Api, tmp_path: Path
) -> None:
    root = tmp_path / "state" / "reports" / "report-2"
    root.mkdir(parents=True)
    shutil.copyfile(TWIN, root / "original.pbix")
    digest = sha(root / "original.pbix")
    pages, layout, _ = read_report(root / "original.pbix")
    now = datetime.now(UTC)
    api.service.store.put(
        "workspace",
        LocalWorkspace(
            id="workspace-2",
            name="Second",
            kind=WorkspaceKind.IMPORTED_REPORT,
            imported_report_id="report-2",
            base_hash=digest,
            created_at=now,
            updated_at=now,
        ),
        workspace_id="workspace-2",
        expected_base_hash=EMPTY_HASH,
        idempotency_key="seed-workspace-2",
        mutation_payload={"seed": 2},
    )
    api.service.store.put(
        "imported_report",
        ImportedReport(
            id="report-2",
            workspace_id="workspace-2",
            name="Second",
            source_path=str(root / "original.pbix"),
            archived_path=str(root / "original.pbix"),
            source_hash=digest,
            base_hash=digest,
            format=layout,
            pages=pages,
            tables=copy.deepcopy([]),
        ),
        workspace_id="workspace-2",
        expected_base_hash=EMPTY_HASH,
        idempotency_key="seed-report-2",
        mutation_payload={"seed": 2},
    )
    edit = api.edit("api-edit-key-01").json()
    other = api.client.get(
        f"/v1/workspaces/workspace-2/imported-report/edits/{edit['batch_id']}", headers=HEADERS
    )
    assert other.status_code == 404
    assert api.get(f"edits/{edit['batch_id']}").json()["state"] == "done"
