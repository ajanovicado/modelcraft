"""The judgement features wired into Studio: routes, service methods, edit-pipeline hooks and chat.

Page review, model health, report tests and "explain this number" are pure modules with their own
tests; this file checks the wiring with fakes (the in-memory Desktop of the edit-pipeline tests,
the structure twin, small synthetic models). Nothing here starts Power BI Desktop, a browser or
the language model: every chat test fails if the model is asked.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from threading import Event
from typing import Any

import pytest
import test_imported_data_answers as data_fixture
import test_model_health as health_fixture
from editing_support import (
    MEASURE_TABLE,
    PAGE,
    VISUAL,
    FakeDesktop,
    make_report,
    patch_diff,
    read_measures,
)
from fastapi.testclient import TestClient

from ai_powerbi_analyst.studio import imported_editing, report_review, report_revisions
from ai_powerbi_analyst.studio import number_explain as ne
from ai_powerbi_analyst.studio import report_tests as rt
from ai_powerbi_analyst.studio import service as service_module
from ai_powerbi_analyst.studio.api import create_app
from ai_powerbi_analyst.studio.contracts import (
    ChatRequest,
    ExplainNumberRequest,
    ImportedEditRequest,
)
from ai_powerbi_analyst.studio.dax_engine import EngineMeasure, Model
from ai_powerbi_analyst.studio.desktop_session import DesktopReadiness
from ai_powerbi_analyst.studio.imported_editing import ImportedEditError, ImportedEditingService
from ai_powerbi_analyst.studio.pbir_document import PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_import import ImportedReport, read_report
from ai_powerbi_analyst.studio.state import EMPTY_HASH, StudioStateStore

WORKSPACE = "workspace-1"
READY = DesktopReadiness(True, True, True, True, "2.157.1354.0", [])
COVERED_SLICER = "5b0dbccd61c6c4adb415"  # the twin's right-hand slicer, 1668,40 210x60
CARD_MEASURE = "Employees Avg MTD Sales"  # "1" in the fake model
TOKEN = "judgement-wiring-token-0001"
REAL = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"
REAL_CARD = "fb4c5aa34e0b084b62b9"  # Agents with Positive Change %
HIDDEN = frozenset({"Rank", "Helper"})
HELPER = {"op": "create_measure", "table": MEASURE_TABLE, "name": "Helper", "expression": "1"}


def _no_model(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the language model was asked")


def _explanation(text: str, formula: str) -> ne.Explanation:
    return ne.Explanation(
        page_id="p1",
        visual_id="v1",
        visual="Sales",
        kind="card",
        point={},
        measure="Sales",
        value=1.0,
        formatted="1",
        computed=True,
        formula=formula,
        meaning=None,
        period=None,
        steps=(),
        filters=(),
        relationships=(),
        formatting=None,
        blank=None,
        notes=(),
        engine="Studio's DAX engine",
        text=text,
    )


@pytest.fixture(autouse=True)
def _doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_diff(monkeypatch)
    monkeypatch.setattr(report_revisions, "RETRY_DELAY", 0.01)


# --- a service over the structure twin with a fake Desktop ----------------------------------------


def _facts(path: Path) -> dict[str, Any]:
    """The model facts (``pbix_extract --facts`` shape) of the fake's DataModel: its measures in
    '# Measures'; [Rank] and a [Helper] a test creates are hidden."""
    measures = [
        health_fixture.measure(
            str(row["Name"]),
            str(row.get("Expression") or ""),
            hidden=row["Name"] in HIDDEN,
        )
        for row in read_measures(path)
    ]
    return {
        **health_fixture.sales_facts(),
        "tables": [health_fixture.table("# Measures")],
        "columns": [],
        "measures": measures,
        "relationships": [],
    }


def _engine_model(report: ImportedReport) -> Model:
    """Studio's engine over the report's measures alone (the fake's measures are constants)."""
    return Model(
        [],
        [],
        [
            EngineMeasure(str(m["Name"]), str(m["TableName"]), str(m.get("Expression") or ""))
            for m in report.measures
        ],
    )


def _service(tmp_path: Path) -> tuple[ImportedEditingService, StudioStateStore, FakeDesktop]:
    report, _store, _source = make_report(tmp_path)
    state = StudioStateStore(tmp_path / "state" / "studio.sqlite3")
    pages, layout, _ = read_report(Path(report.archived_path))
    state.put(
        "imported_report",
        report.model_copy(update={"pages": pages, "format": layout}),
        workspace_id=report.workspace_id,
        expected_base_hash=EMPTY_HASH,
        idempotency_key=f"seed-{report.id}",
        mutation_payload={"seed": report.id},
    )
    desktop = FakeDesktop()

    def load(workspace: str) -> ImportedReport:
        found = state.list_objects("imported_report", ImportedReport, workspace)
        if not found:
            raise KeyError(workspace)
        return found[0]

    service = ImportedEditingService(
        tmp_path / "state",
        state,
        load_report=load,
        port_factory=lambda: desktop,
        readiness_factory=lambda: READY,
        read_measures=read_measures,
        read_types=lambda _path: {"columns": [], "measures": [], "calculated": {}},
        read_facts=_facts,
        disk_free=lambda _path: 10**12,
        profile_dir=tmp_path,
        tests_evaluator=lambda report: rt.LocalEvaluator(report, model=_engine_model(report)),
    )
    return service, state, desktop


def _edit(service: ImportedEditingService, ops: list[dict[str, Any]], key: str, **kw: Any) -> Any:
    head = service.head(WORKSPACE)["id"]
    handle = service.submit_edit(WORKSPACE, ops, expected_head=head, idempotency_key=key, **kw)
    return service.outcome(handle).body


# --- page review ----------------------------------------------------------------------------------


def test_the_review_route_is_the_modules_review_of_the_head(tmp_path: Path) -> None:
    service, _state, _desktop = _service(tmp_path)
    found = service.review_page(WORKSPACE, "1")
    expected = report_review.review_page(
        PbirDocument(PbixPackage(Path(service.report(WORKSPACE).archived_path))), PAGE
    )
    assert found["page"] == PAGE and found["head"]["id"] == "r0000"
    assert [f["id"] for f in found["findings"]] == [f.id for f in expected.findings]
    assert found["summary"] == {
        "problems": 1,
        "suggestions": 4,
        "by_rule": {"same_field_slicers": 1, "near_miss": 4},
    }
    with pytest.raises(ImportedEditError) as stale:
        service.review_page(WORKSPACE, PAGE, revision="r0009")
    assert (stale.value.code, stale.value.status) == ("head_moved", 409)
    with pytest.raises(ImportedEditError) as unknown:
        service.review_page(WORKSPACE, "No such page")
    assert (unknown.value.code, unknown.value.status) == ("unknown_page", 422)


def test_an_edit_that_makes_an_overlap_says_so_in_its_receipt_and_nothing_old(
    tmp_path: Path,
) -> None:
    service, _state, desktop = _service(tmp_path)
    body = _edit(
        service, [{"op": "set_position", "visual": VISUAL, "x": 1668, "y": 40}], "review-key-01"
    )
    assert [(f["rule"], f["visuals"], f["message"]) for f in body["review"]] == [
        (
            "overlap",
            [VISUAL, COVERED_SLICER],
            "The 'Country' slicer covers 100% of the slicer of Store.",
        ),
        # Moved away, it leaves the Channel slicer next to the Store slicer, 1 px taller.
        (
            "near_miss",
            ["b85aa81a8b5c60030d13", COVERED_SLICER],
            "The slicer of Channel is 1 px taller than the slicer of Store beside it.",
        ),
    ]
    # What the page already had (two slicers over one field, the charts' near misses) is not
    # repeated; a report-only edit carries no model or tests check.
    assert "model_health" not in body and "tests" not in body
    assert desktop.events == []
    # An edit that only moves a visual back into line introduces nothing.
    assert (
        _edit(
            service,
            [{"op": "set_position", "visual": VISUAL, "x": 1394.4477611940297, "y": 40}],
            "review-key-02",
        )["review"]
        == []
    )


def test_chat_shows_the_edits_review_and_model_findings_after_its_receipt() -> None:
    receipt = {"review": [{"id": "overlap-1"}], "model_health": {"new": [{"id": "dax_division"}]}}
    events = [
        {"event": "edit_progress", "data": {}},
        {"event": "edit_applied", "data": {"revision_id": "r0003", "receipt": receipt}},
        {"event": "edit_applied", "data": {"revision_id": "r0004", "receipt": {"review": []}}},
        {"event": "token", "data": {"text": "Done."}},
    ]
    got = list(service_module.StudioService._with_judgement(iter(events)))
    assert [e["event"] for e in got] == [
        "edit_progress",
        "edit_applied",
        "edit_review",
        "model_health",
        "edit_applied",
        "token",
    ]
    assert got[2]["data"] == {"revision_id": "r0003", "findings": [{"id": "overlap-1"}]}
    assert got[3]["data"] == {"revision_id": "r0003", "new": [{"id": "dax_division"}]}


# --- model health ---------------------------------------------------------------------------------


def test_model_health_reads_the_heads_full_facts_and_offers_confirmed_fixes(
    tmp_path: Path,
) -> None:
    service, _state, desktop = _service(tmp_path)
    assert service.model_health(WORKSPACE)["findings"] == []
    _edit(service, [HELPER], "health-key-00", confirm_model=True)
    events = len(desktop.events)
    health = service.model_health(WORKSPACE)
    assert health["known"] is True and health["head"]["id"] == "r0001"
    fixable = health["findings"][0]
    assert (fixable["rule"], fixable["fix"]) == (
        "unused_hidden_measure",
        [{"op": "delete_measure", "name": "Helper"}],
    )
    assert fixable["fix_confirm"] is True and fixable["fix_needs_desktop"] is True
    base = health["head"]["hash"]
    with pytest.raises(ImportedEditError) as asked:
        service.model_health_fix(
            WORKSPACE,
            fixable["id"],
            base_hash=base,
            confirmed=False,
            idempotency_key="health-key-01",
        )
    assert (asked.value.code, asked.value.extra["kind"]) == (
        "needs_confirmation",
        "model_health_fix",
    )
    assert asked.value.extra["detail"] == fixable["fix_summary"]
    with pytest.raises(ImportedEditError) as moved:
        service.model_health_fix(
            WORKSPACE,
            fixable["id"],
            base_hash="0" * 64,
            confirmed=True,
            idempotency_key="health-key-02",
        )
    assert (moved.value.code, moved.value.status) == ("head_moved", 409)
    with pytest.raises(ImportedEditError) as gone:
        service.model_health_fix(
            WORKSPACE,
            "no-such-finding",
            base_hash=base,
            confirmed=True,
            idempotency_key="health-key-03",
        )
    assert (gone.value.code, gone.value.status) == ("finding_gone", 409)
    assert len(desktop.events) == events and service.head(WORKSPACE)["id"] == "r0001"
    done = service.outcome(
        service.model_health_fix(
            WORKSPACE,
            fixable["id"],
            base_hash=base,
            confirmed=True,
            idempotency_key="health-key-04",
        )
    ).body
    assert done["outcome"] == "updated" and done["revision"]["id"] == "r0002"
    assert service.model_health(WORKSPACE)["findings"] == []
    # Asked again under the same key, the batch that ran is the answer (the finding is gone).
    again = service.model_health_fix(
        WORKSPACE, fixable["id"], base_hash=base, confirmed=True, idempotency_key="health-key-04"
    )
    assert service.outcome(again).body["revision"]["id"] == "r0002"
    rules = service.model_health_rules()
    assert rules[0] == {
        "id": "broken_object",
        "category": "correctness",
        "severity": "problem",
        "title": "Objects Power BI marks as in error",
    }


def test_a_measure_edit_reports_only_the_model_findings_it_introduced(tmp_path: Path) -> None:
    service, _state, _desktop = _service(tmp_path)
    before = {f["id"] for f in service.model_health(WORKSPACE)["findings"]}
    body = _edit(
        service,
        [
            {
                "op": "create_measure",
                "table": MEASURE_TABLE,
                "name": "Sales per Rank",
                "expression": "[dummy] / [Rank]",
                "format_string": "0.0",
            }
        ],
        "health-key-10",
        confirm_model=True,
    )
    new = body["model_health"]["new"]
    # The new measure divides with "/" and nothing uses it yet; the model had neither before.
    assert [f["rule"] for f in new] == ["dax_division", "unused_measure"]
    for finding in new:
        assert finding["id"] not in before
        assert {"kind": "measure", "table": MEASURE_TABLE, "name": "Sales per Rank"} in finding[
            "objects"
        ]
    # No formula is in what the receipt says about the model.
    assert "[dummy] / [Rank]" not in json.dumps(new)


# --- report tests ---------------------------------------------------------------------------------


def _card_test(service: ImportedEditingService) -> dict[str, Any]:
    return service.add_report_test(
        WORKSPACE,
        {
            "name": "Agents Avg MTD Sales",
            "measure": {"table": MEASURE_TABLE, "name": CARD_MEASURE},
            "expect": {"equals": 1},
            "filters": [],
        },
    )


def test_tests_are_kept_run_edited_and_removed_through_the_service(tmp_path: Path) -> None:
    service, _state, _desktop = _service(tmp_path)
    assert service.report_tests(WORKSPACE) == {"tests": [], "last_run": None, "stale": False}
    added = _card_test(service)
    run = service.run_report_tests(WORKSPACE, "local")
    assert [r["status"] for r in run["results"]] == ["passed"]
    assert run["text"] == "The report test passes.\n- Agents Avg MTD Sales is still 1."
    assert run["revision"] == "r0000" and run["engine"] == "Studio's DAX engine"
    listed = service.report_tests(WORKSPACE)
    assert [t["id"] for t in listed["tests"]] == [added["id"]] and listed["stale"] is False
    updated = service.update_report_test(WORKSPACE, added["id"], {"expect": {"equals": 2}})
    assert updated["id"] == added["id"] and updated["expect"]["equals"] == 2
    again = service.run_report_tests(WORKSPACE, "auto")
    assert again["results"][0]["message"] == "Agents Avg MTD Sales is 1 now instead of 2."
    # Power BI Desktop does not have the report open: "desktop" checks nothing, opens nothing.
    desktop_run = service.run_report_tests(WORKSPACE, "desktop")
    assert desktop_run["results"][0]["status"] == "not_checked"
    with pytest.raises(ImportedEditError) as invalid:
        service.add_report_test(WORKSPACE, {"name": "x", "expect": {"equals": 1}})
    assert invalid.value.code == "invalid_test"
    assert "either a measure or a DAX expression" in invalid.value.message
    assert service.delete_report_test(WORKSPACE, added["id"]) == {"removed": True}
    assert service.delete_report_test(WORKSPACE, added["id"]) == {"removed": False}


def test_a_measure_change_that_breaks_a_pinned_number_asks_before_desktop_opens(
    tmp_path: Path,
) -> None:
    service, _state, desktop = _service(tmp_path)
    test = _card_test(service)
    change = [{"op": "update_measure", "name": CARD_MEASURE, "expression": "2"}]
    with pytest.raises(ImportedEditError) as asked:
        _edit(service, change, "tests-key-01", confirm_model=True)
    error = asked.value
    assert (error.code, error.status, error.extra["kind"]) == (
        "needs_confirmation",
        409,
        "report_tests_broken",
    )
    assert error.extra["detail"] == (
        "This change would break a report test:\n"
        "- Agents Avg MTD Sales is 2 now instead of 1.\nApply it anyway?"
    )
    assert [t["id"] for t in error.extra["tests"]] == [test["id"]]
    assert error.extra["confirm"] == {"confirmed": ["report_tests_broken"]}
    # Nothing was written and Power BI Desktop was never opened.
    assert desktop.events == [] and service.head(WORKSPACE)["id"] == "r0000"
    body = _edit(
        service, change, "tests-key-02", confirm_model=True, confirmed=["report_tests_broken"]
    )
    assert body["outcome"] == "updated"
    assert [r["status"] for r in body["tests"]["broken"]] == ["failed"]
    assert any("breaks report tests" in w for w in body["warnings"])
    last = service.report_tests(WORKSPACE)["last_run"]
    assert last["revision"] == body["revision"]["id"] and last["results"][0]["value"] == 2


def test_a_model_change_is_checked_on_the_model_power_bi_saved_before_it_is_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change beyond measures (here a calculated column) cannot be projected: the tests run on
    the model Desktop saved, and a broken one stops the batch before the revision is committed
    (the head stays; Desktop's copy is put back)."""
    service, _state, desktop = _service(tmp_path)
    number = {"value": 1}

    def evaluator(_report: ImportedReport) -> Any:
        return lambda _test: number["value"]

    monkeypatch.setattr(service, "_tests_evaluator", evaluator)
    real_apply = desktop.apply_model_operations

    def apply(instance: Any, operations: list[Any]) -> Any:
        number["value"] = 2  # what the saved model computes now
        return real_apply(instance, operations)

    monkeypatch.setattr(desktop, "apply_model_operations", apply)
    _card_test(service)
    column = [
        {
            "op": "create_calculated_column",
            "table": "DimChannel",
            "name": "Upper",
            "expression": "UPPER(DimChannel[ChannelName])",
        }
    ]
    with pytest.raises(ImportedEditError) as asked:
        _edit(service, column, "tests-key-30", confirm_model=True)
    assert asked.value.extra["kind"] == "report_tests_broken"
    assert "model applied" in desktop.events  # it got as far as Power BI's saved model
    assert service.head(WORKSPACE)["id"] == "r0000"
    number["value"] = 1
    body = _edit(
        service, column, "tests-key-31", confirm_model=True, confirmed=["report_tests_broken"]
    )
    assert body["revision"]["id"] == "r0001"
    assert [r["message"] for r in body["tests"]["broken"]] == [
        "Agents Avg MTD Sales is 2 now instead of 1."
    ]


def test_a_rename_is_a_refactor_the_tests_follow(tmp_path: Path) -> None:
    service, _state, _desktop = _service(tmp_path)
    _card_test(service)
    body = _edit(
        service,
        [{"op": "rename_measure", "name": CARD_MEASURE, "new_name": "Avg MTD Sales"}],
        "tests-key-10",
        confirm_model=True,
    )
    assert body["tests"]["broken"] == []
    assert [r["status"] for r in body["tests"]["results"]] == ["passed"]
    stored = service.report_tests(WORKSPACE)["tests"]
    assert stored[0]["measure"] == {"table": MEASURE_TABLE, "name": "Avg MTD Sales"}


def test_a_report_edit_is_never_gated_and_a_report_without_tests_never_runs_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _state, _desktop = _service(tmp_path)
    ran: list[int] = []
    real = rt.run_tests

    def counted(tests: Any, evaluate: Any, **kw: Any) -> Any:
        ran.append(len(tests))
        return real(tests, evaluate, **kw)

    monkeypatch.setattr(rt, "run_tests", counted)
    _edit(
        service,
        [{"op": "create_measure", "table": MEASURE_TABLE, "name": "One", "expression": "1"}],
        "tests-key-20",
        confirm_model=True,
    )
    assert ran == []
    _card_test(service)
    _edit(service, [{"op": "set_position", "visual": VISUAL, "x": 1390}], "tests-key-21")
    assert ran == []


def test_a_pin_is_checked_and_kept_and_a_refusal_says_why(tmp_path: Path) -> None:
    service, _state, _desktop = _service(tmp_path)
    with pytest.raises(ImportedEditError) as refused:
        service.pin_report_test(WORKSPACE, PAGE, VISUAL, {}, 5)
    assert refused.value.code == "pin_refused" and refused.value.status == 422
    assert refused.value.message
    assert service.report_tests(WORKSPACE)["tests"] == []


def test_suggestions_come_from_the_rendered_pages_and_are_not_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _state, _desktop = _service(tmp_path)
    proposed = rt.new_user_test(
        "Sales",
        expect=rt.ExpectEquals(equals=1),
        measure=rt.MeasureRef(table=MEASURE_TABLE, name=CARD_MEASURE),
    )
    seen: list[Any] = []

    def suggest(document: Any, payloads: Any, *, report: Any) -> list[rt.TestDefinition]:
        seen.append((document, list(payloads)))
        return [proposed]

    monkeypatch.setattr(rt, "suggest_tests", suggest)
    found = service.suggest_report_tests(
        WORKSPACE, [PAGE], render=lambda _report, page: {"page": {"id": page}, "visuals": []}
    )
    assert found["text"] == ["Sales ([Employees Avg MTD Sales]) should be 1."]
    assert seen == [(None, [{"page": {"id": PAGE}, "visuals": []}])]
    assert service.report_tests(WORKSPACE)["tests"] == []


# --- the HTTP routes ------------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any, str]:
    """A Studio service over the synthetic sales report of the data-answer tests (page "1", no
    visuals), its report definition made readable, and a client of the HTTP routes."""
    plain = data_fixture._pbix

    def with_report(folder: Path, name: str) -> Path:
        file = plain(folder, name)
        with zipfile.ZipFile(file, "a") as archive:
            archive.writestr("Report/definition/report.json", json.dumps({"themeCollection": {}}))
        return file

    monkeypatch.setattr(data_fixture, "_pbix", with_report)
    studio, workspace_id = data_fixture._import(
        tmp_path,
        monkeypatch,
        data_fixture._sales_frames(),
        data_fixture.SALES_MEASURES,
        data_fixture.SALES_RELATIONSHIPS,
    )
    monkeypatch.setattr(studio, "chat", _no_model)
    client = TestClient(create_app(studio, TOKEN))
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client, studio, workspace_id


def test_every_judgement_route_answers_with_its_documented_shape(api: Any) -> None:
    client, _studio, ws = api
    base = f"/v1/workspaces/{ws}/imported-report"
    review = client.post(f"{base}/pages/p1/review", json={"with_data": False})
    assert review.status_code == 200 and review.json()["findings"] == []
    assert client.post(f"{base}/pages/p9/review", json={}).status_code == 422
    assert client.post(f"{base}/pages/p1/review?revision=r0009", json={}).status_code == 409
    health = client.get(f"{base}/model-health?stats=1").json()
    assert set(health) == {"known", "findings", "summary", "checked", "notes", "head"}
    rules = client.get("/v1/imported-report/model-health/rules").json()
    assert len(rules) == 26 and rules[0]["id"] == "broken_object"
    fix = client.post(
        f"{base}/model-health/fix",
        json={"finding_id": "x", "base_hash": "0" * 64, "idempotency_key": "route-key-01"},
    )
    assert fix.status_code == 409 and fix.json()["code"] == "head_moved"
    assert client.get(f"{base}/tests").json() == {"tests": [], "last_run": None, "stale": False}
    total = float(data_fixture._sales_frames()["FactSales"]["SalesAmount"].sum())
    added = client.post(
        f"{base}/tests",
        json={
            "name": "Total",
            "measure": {"table": "# Measures", "name": "Total Sales"},
            "filters": [],
            "expect": {"equals": total},
        },
    )
    assert added.status_code == 200
    test_id = added.json()["id"]
    run = client.post(f"{base}/tests/run", json={"engine": "local"}).json()
    assert run["results"][0]["status"] == "passed" and run["text"].startswith("The report test")
    patched = client.patch(f"{base}/tests/{test_id}", json={"name": "Grand total"})
    assert patched.status_code == 200 and patched.json()["name"] == "Grand total"
    assert client.patch(f"{base}/tests/nope", json={"name": "x"}).status_code == 404
    pin = client.post(f"{base}/tests/pin", json={"page_id": "p1", "visual_id": "v1", "value": 3})
    assert pin.status_code == 422 and pin.json()["code"] == "pin_refused"
    assert client.get(f"{base}/tests/suggestions?page=p1").json() == {"tests": [], "text": []}
    assert client.delete(f"{base}/tests/{test_id}").json() == {"removed": True}
    # The synthetic report has no number formats Studio can read: said, not guessed.
    explained = client.post(f"{base}/explain", json={"page_id": "p1", "visual_id": "nope"})
    assert explained.status_code == 422 and explained.json()["code"] == "cannot_explain"
    bad_point = client.post(
        f"{base}/explain", json={"page_id": "p1", "visual_id": "v", "point": {"values": {"a": [1]}}}
    )
    assert bad_point.status_code == 422


def test_the_explain_route_maps_a_missing_visual_and_hands_the_explanation_over(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, studio, ws = api
    calls: list[tuple[Any, ...]] = []

    def explain(report: Any, page: str, visual: str, point: Any, **kw: Any) -> Any:
        calls.append((page, visual, point, kw["focus"], type(kw["request"]).__name__))
        if visual == "gone":
            raise LookupError("This visual is not on this page")
        return _explanation("Sales is 1 here.", "SUM(x)")

    monkeypatch.setattr(ne, "explain_point", explain)
    url = f"/v1/workspaces/{ws}/imported-report/explain"
    missing = client.post(url, json={"page_id": "p1", "visual_id": "gone"})
    assert missing.status_code == 404 and missing.json()["message"] == (
        "This visual is not on this page"
    )
    found = client.post(
        url,
        json={
            "page_id": "p1",
            "visual_id": "v1",
            "focus": "colour",
            "point": {"values": {"DimChannel.ChannelName": "Store"}, "measure": "Total Sales"},
            "canvas": {},
        },
    )
    assert found.status_code == 200 and found.json()["text"] == "Sales is 1 here."
    assert calls[-1] == (
        "p1",
        "v1",
        {"values": {"DimChannel.ChannelName": "Store"}, "measure": "Total Sales"},
        "colour",
        "LiveReportRequest",
    )
    assert any(e["type"] == "report.explain" for e in studio.store.events(ws))
    invalid = client.post(
        url, json={"page_id": "p1", "visual_id": "v1", "canvas": {"no_such_field": 1}}
    )
    assert invalid.status_code == 422 and invalid.json()["code"] == "invalid_canvas"


def test_a_confirmation_is_sent_with_the_edit_request() -> None:
    request = ImportedEditRequest.model_validate(
        {
            "operations": [{"op": "update_measure", "name": "A", "expression": "1"}],
            "expected_head": "r0001",
            "idempotency_key": "confirm-key-01",
            "confirmed": ["report_tests_broken"],
        }
    )
    assert request.confirmed == ["report_tests_broken"]
    with pytest.raises(ValueError):
        ImportedEditRequest.model_validate({**request.model_dump(), "confirmed": ["anything"]})


def test_the_report_tests_confirmation_names_its_tests_and_its_answer() -> None:
    result = rt.TestResult(
        id="t1",
        name="Sales",
        status="failed",
        value=2,
        expected=rt.ExpectEquals(equals=1),
        message="Sales is 2 now.",
    )
    error = imported_editing.classify(rt.ReportTestsBroken([result]))
    assert error.payload() == {
        "detail": rt.confirmation_text([result]),
        "code": "needs_confirmation",
        "message": rt.confirmation_text([result]),
        "kind": "report_tests_broken",
        "tests": [result.model_dump(mode="json")],
        "confirm": {"confirmed": ["report_tests_broken"]},
    }


# --- chat -----------------------------------------------------------------------------------------


def _say(studio: Any, workspace_id: str, message: str, **context: str) -> list[dict[str, Any]]:
    request = ChatRequest.model_validate(
        {
            "workspace_id": workspace_id,
            "message": message,
            **({"context": context} if context else {}),
        }
    )
    return list(studio.chat_events(request, Event()))


def _text(events: list[dict[str, Any]]) -> str:
    return "".join(str(e["data"]["text"]) for e in events if e["event"] == "token")


def test_review_health_and_tests_are_answered_without_the_model(api: Any) -> None:
    _client, studio, ws = api
    assert _text(_say(studio, ws, "review this page")) == (
        "Studio looked at page '1' and found nothing to fix."
    )
    assert _text(_say(studio, ws, "Can you check the model?")).startswith(
        "Studio checked 6 tables, 19 columns, 12 measures and 4 relationships: "
        "no problems and 8 suggestions."
    )
    assert _text(_say(studio, ws, "run the tests")) == "This report has no tests yet."
    assert _text(_say(studio, ws, "what tests do I have?")).startswith(
        "This report has no tests yet."
    )
    assert _text(_say(studio, ws, "pin this number")) == (
        "Select the card on the canvas first, then ask again."
    )
    # "fix it" with no review shown goes to the edit paths as before.
    assert (
        studio._review_fix_events(
            ChatRequest(workspace_id=ws, message="fix it"), studio.workspace(ws)
        )
        is None
    )


def test_fix_it_after_a_review_offers_the_reviews_fixes_as_one_change(api: Any) -> None:
    _client, studio, ws = api
    head = studio.editing.head(ws)["id"]
    fix = [{"op": "set_position", "visual": "v1", "height": 60}]
    studio._chat_reviews[ws] = (
        head,
        [
            {"message": "The slicer is 1 px taller.", "fix": fix, "fix_summary": "Make it 60 px."},
            {"message": "Two slicers filter Channel.", "fix": None, "fix_summary": "Look at them."},
        ],
    )
    events = _say(studio, ws, "fix it")
    confirm = next(e["data"] for e in events if e["event"] == "edit_confirm")
    assert confirm["kind"] == "review_fixes" and confirm["ops"] == fix
    assert confirm["detail"] == "Apply these fixes as one change?\n- Make it 60 px."
    pending = studio.chat_edits.take(ws, confirm["pending_id"])
    assert pending is not None and pending.action.ops == tuple(fix)


def test_an_explanation_reaches_the_ui_and_its_formula_never_the_history(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sentinel (like test_model_facts_privacy): the measure's formula goes to the Explain
    panel in the ``explanation`` event and nowhere else: not in the reply, not in the chat
    history a later prompt is built from, and the model is never asked."""
    _client, studio, ws = api
    secret = "SENTINEL_FORMULA_DIVIDE([Secret Numerator], [Secret Denominator])"
    found = _explanation("Sales is 1 here.", secret)
    monkeypatch.setattr(ne, "answer_explanation", lambda _message, _context: found)
    events = _say(studio, ws, "Why does this card show 1?")
    assert [e["event"] for e in events] == ["explanation", "token"]
    assert events[0]["data"]["formula"] == secret and _text(events) == "Sales is 1 here."
    history = json.dumps(studio.messages(ws))
    assert "SENTINEL_FORMULA" not in history and "Sales is 1 here." in history


def test_an_explain_question_nothing_on_the_page_fits_goes_on_to_the_other_answers(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, studio, ws = api
    monkeypatch.setattr(ne, "answer_explanation", lambda _message, _context: None)
    workspace = studio.workspace(ws)
    request = ChatRequest(workspace_id=ws, message="why is Store blank?")
    assert studio._judgement_events(request, workspace) is None


# --- computed answers before the model ------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "plain"),
    [
        ("could you name the top agent by MTD sales?", "show the top agent by MTD sales?"),
        ("Please name the top 3 agents by total sales", "show the top 3 agents by total sales"),
        ("Would you please find the lowest store?", "show the lowest store?"),
        ("Can you tell me the top agent?", "show the top agent?"),
        # Anything that is no polite data request is left alone.
        ("could this be seasonal?", "could this be seasonal?"),
        ("could you explain why sales dropped?", "could you explain why sales dropped?"),
        ("name the top agent", "name the top agent"),
    ],
)
def test_a_polite_request_is_read_as_the_question_inside_it(message: str, plain: str) -> None:
    assert service_module._plain_request(message) == plain


def test_could_you_name_the_top_agent_is_computed_not_left_to_the_model(api: Any) -> None:
    """Live 2026-09-29: the message was read as a question (right) and fell through to the
    model, which talked about joins and never named the agent. "could" made the planner read
    it as a discussion; the computed path now answers it with the agent and the value."""
    _client, studio, ws = api
    text = _text(_say(studio, ws, "could you name the top agent by MTD sales?"))
    assert text.startswith("**Bo Chen** has the highest Total Sales in December 2009: **120,000**.")
    # The answer says which measure it is: the planner reads "MTD" as a period of [Total Sales],
    # and the report's own [MTD Total Sales] gives the same number for that row.
    assert text.endswith(
        "Measure used: [Total Sales] for that period, which gives the same numbers as the "
        "report's own [MTD Total Sales] measure for every row listed."
    )
    total = _text(_say(studio, ws, "Please name the top 3 agents by total sales"))
    assert "1. Ana Lopez: 181,155" in total and "3. Kim Lee: 34,815" in total
    assert "Measure used" not in total  # no period measure was named


def test_a_report_measure_that_disagrees_with_the_answer_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    measures = [
        (name, "[Total Sales] * 2" if name == "MTD Total Sales" else expression)
        for name, expression in data_fixture.SALES_MEASURES
    ]
    studio, ws = data_fixture._import(
        tmp_path,
        monkeypatch,
        data_fixture._sales_frames(),
        measures,
        data_fixture.SALES_RELATIONSHIPS,
    )
    monkeypatch.setattr(studio, "chat", _no_model)
    text = _text(_say(studio, ws, "who is the top agent by MTD sales?"))
    assert text.endswith(
        "Note: the report's own measure [MTD Total Sales] gives other numbers here (Bo Chen: "
        "303,024 instead of 120,000). The answer above is [Total Sales] for the period in the "
        "note."
    )


# --- the real dashboard (read-only; skipped where it is absent) -----------------------------------


def test_explain_on_the_real_agents_card(tmp_path: Path) -> None:
    if not REAL.is_file():
        pytest.skip("the real Agents Performance report is not on this machine")
    from ai_powerbi_analyst.studio.report_import import ImportReportRequest

    studio = service_module.StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    studio.chat = _no_model  # type: ignore[method-assign]
    workspace = studio.import_existing_report(
        ImportReportRequest(path=str(REAL), idempotency_key="explain-real-0001")
    )
    report = studio.imported_report(workspace.id)
    found = studio.explain_number(
        workspace.id,
        ExplainNumberRequest(page_id=report.pages[0].id, visual_id=REAL_CARD),
    )
    assert found["formatted"] == "46.76%" and found["value"] == pytest.approx(137 / 293)
    assert "- So 46.76% = 137 ÷ 293." in found["text"]
    assert found["formula"] and found["formula"] not in found["text"]
