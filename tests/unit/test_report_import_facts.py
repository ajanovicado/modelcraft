"""The import keeps the model's facts and the digest of the model its rows came from; old stored
reports still load and keep their engine fingerprint; the data can be read again after a refresh
in Power BI Desktop."""

from __future__ import annotations

import hashlib
import json
import os
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.studio import model_facts, report_import
from ai_powerbi_analyst.studio.ingestion import StudioIngestionService
from ai_powerbi_analyst.studio.report_import import (
    ImportedPage,
    ImportedReport,
    cached_facts,
    import_report,
    model_fingerprint,
    model_key,
    reread_data,
    store_facts,
    with_model_facts,
)

FACTS: dict[str, Any] = {
    "version": 1,
    "tables": [{"name": "Orders", "source": "m", "calculated": False, "lineageTag": "t-o"}],
    "columns": [
        {"table": "Orders", "name": "Region", "kind": "data", "dataType": 2, "lineageTag": "c-r"},
        {"table": "Orders", "name": "Sales", "kind": "data", "dataType": 8, "lineageTag": "c-s"},
        {"table": "Orders", "name": "Big", "kind": "calculated", "dataType": 11,
         "expression": "[Sales] > 100", "lineageTag": "c-b"},
    ],
    "measures": [{"table": "Orders", "name": "Total", "expression": "SUM(Orders[Sales])"}],
    "relationships": [],
    "partitions": [
        {"table": "Orders", "name": "Orders", "source": "m", "mode": "import",
         "expression": "let x = 1 in x",
         "refreshedTime": "2024-01-01T00:00:00+00:00"},
    ],
}  # fmt: skip


def pbix(path: Path, model: bytes = b"model-bytes") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Report/definition/pages/pages.json", json.dumps({"pageOrder": ["first"]}))
        archive.writestr(
            "Report/definition/pages/first/page.json",
            json.dumps({"name": "first", "displayName": "First"}),
        )
        archive.writestr("DataModel", model)
    return path


SCHEMA_ROW = {"TableName": "Orders", "ColumnName": "Sales", "PandasDataType": "Float64"}
MEASURE_ROW = {"TableName": "Orders", "Name": "Total", "Expression": "SUM(Orders[Sales])"}


def fake_extraction(facts: dict[str, Any] | None, rows: list[float]) -> Any:
    def extract(_source: Path, destination: Path) -> dict[str, Any]:
        frame = pd.DataFrame({"Region": ["East", "West"][: len(rows)], "Sales": rows})
        frame.to_parquet(destination / "table-0.parquet", index=False)
        metadata: dict[str, Any] = {
            "tables": [{"name": "Orders", "file": "table-0.parquet", "rows": len(rows)}],
            "schema": [SCHEMA_ROW],
            "measures": [MEASURE_ROW],
            "relationships": [],
        }
        if facts is not None:
            metadata["facts"] = facts
        return metadata

    return extract


def test_the_import_keeps_the_facts_and_the_digest_of_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report_import, "extract_model", fake_extraction(FACTS, [100.0, 250.0]))
    state = tmp_path / "state"
    state.mkdir()
    source = pbix(tmp_path / "Orders.pbix")
    report, snapshot = import_report(str(source), state, "ws-1", StudioIngestionService(state))
    assert snapshot is not None and report.snapshot_id == snapshot.id
    key = model_key(Path(report.archived_path))
    assert report.model == model_facts.stored(FACTS)
    assert report.snapshot_model == model_facts.digest(FACTS, key)
    assert report.model_read_version == 1
    # the full facts are kept beside the history, keyed by the model
    assert cached_facts(Path(report.archived_path).parent, key) == FACTS
    # the import's own rows stay what they were (the extraction's schema, not the facts')
    assert report.tables == [
        {"TableName": "Orders", "ColumnName": "Sales", "PandasDataType": "Float64"}
    ]


def test_an_extraction_without_facts_leaves_them_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report_import, "extract_model", fake_extraction(None, [1.0]))
    state = tmp_path / "state"
    state.mkdir()
    report, _ = import_report(
        str(pbix(tmp_path / "Orders.pbix")), state, "ws-1", StudioIngestionService(state)
    )
    assert (report.model, report.snapshot_model) == (None, None)
    assert ImportedReport.model_validate_json(report.model_dump_json()).model_read_version == 1
    assert not (Path(report.archived_path).parent / report_import.FACTS_DIR).exists()


def base_report(**more: Any) -> ImportedReport:
    fields: dict[str, Any] = {
        "id": "report-1",
        "workspace_id": "ws",
        "name": "R",
        "source_path": "s.pbix",
        "archived_path": "o.pbix",
        "source_hash": "a" * 64,
        "base_hash": "a" * 64,
        "format": "PBIR",
        "pages": [ImportedPage(id="p", title="One")],
        "tables": [{"TableName": "Orders", "ColumnName": "Sales", "PandasDataType": "Float64"}],
        "measures": [{"Name": "Total", "TableName": "Orders", "Expression": "SUM(Orders[Sales])"}],
        "relationships": [],
        "snapshot_id": "snap-1",
        "relationships_version": 2,
    }
    fields.update(more)
    return ImportedReport(**fields)


def test_an_old_stored_report_loads_and_keeps_its_engine_fingerprint() -> None:
    stored = base_report().model_dump(mode="json")
    del stored["model"], stored["snapshot_model"], stored["model_read_version"]
    old = ImportedReport.model_validate(stored)
    assert (old.model, old.snapshot_model) == (None, None)
    assert old.model_read_version == 0

    # The fingerprint as it was computed before facts existed: a report without them keeps
    # its cached engine model.
    def canonical(items: list[dict[str, Any]]) -> list[str]:
        return sorted(json.dumps(item, sort_keys=True, default=str) for item in items)

    payload = {
        "measures": canonical(
            [
                {"name": m.get("Name"), "table": m.get("TableName"),
                 "expression": m.get("Expression"), "format": m.get("FormatString")}
                for m in old.measures
            ]
        ),
        "schema": canonical(old.tables),
        "relationships": canonical(old.relationships),
        "relationships_version": old.relationships_version,
        "snapshot": old.snapshot_id,
    }  # fmt: skip
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert model_fingerprint(old) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    # A digest alone (the head's read failed) decides whether the engine may answer at all,
    # so it is part of the key.
    digest = model_facts.digest(FACTS, "crc")
    assert model_fingerprint(old.model_copy(update={"snapshot_model": digest})) != (
        model_fingerprint(old)
    )
    # A failed current read must also invalidate a cached legacy snapshot evaluator.
    assert model_fingerprint(old.model_copy(update={"model_read_version": 1})) != (
        model_fingerprint(old)
    )


def test_the_fingerprint_follows_the_facts_the_engine_builds_from() -> None:
    known = base_report(
        model=model_facts.stored(FACTS), snapshot_model=model_facts.digest(FACTS, "k")
    )
    assert model_fingerprint(known) != model_fingerprint(base_report())
    changed = json.loads(json.dumps(FACTS))
    changed["columns"][2]["expression"] = "[Sales] > 200"
    recalculated = known.model_copy(update={"model": model_facts.stored(changed)})
    assert model_fingerprint(recalculated) != model_fingerprint(known)
    reworded = json.loads(json.dumps(FACTS))
    reworded["columns"][0]["description"] = "Where it was sold"
    assert model_fingerprint(known.model_copy(update={"model": model_facts.stored(reworded)})) == (
        model_fingerprint(known)
    )


def test_a_report_takes_the_rows_of_the_model_its_facts_describe() -> None:
    report = base_report()
    head = json.loads(json.dumps(FACTS))
    head["relationships"] = [
        {"fromTable": "Orders", "fromColumn": "Region", "toTable": "Regions", "toColumn": "Name",
         "cardinality": "M:1", "crossFilter": "both", "active": True},
    ]  # fmt: skip
    moved = with_model_facts(report, head, snapshot_facts=("k-rows", FACTS))
    assert moved.tables == model_facts.schema_rows(head)
    assert moved.relationships == [
        {
            "FromTableName": "Orders",
            "FromColumnName": "Region",
            "ToTableName": "Regions",
            "ToColumnName": "Name",
            "IsActive": 1,
            "Cardinality": "M:1",
            "CrossFilteringBehavior": "Both",
            "RelyOnReferentialIntegrity": 0,
            "JoinOnDateBehavior": None,
        }
    ]
    assert moved.model == model_facts.stored(head)
    assert moved.model_read_version == 1
    assert moved.snapshot_model == model_facts.digest(FACTS, "k-rows")
    assert moved.relationships_version == report_import.RELATIONSHIPS_VERSION
    # a digest already kept is never replaced; a report without rows gets none
    assert with_model_facts(moved, FACTS, snapshot_facts=("other", FACTS)).snapshot_model == (
        moved.snapshot_model
    )
    rowless = base_report(snapshot_id=None)
    assert with_model_facts(rowless, FACTS, snapshot_facts=("k", FACTS)).snapshot_model is None
    # Facts that could not be read make the facts unknown, and the joins are never left
    # describing the old model: they are to be read again from the head (version 0).
    unknown = with_model_facts(moved, None)
    assert unknown.model is None and unknown.tables == moved.tables
    assert unknown.relationships_version == 0
    assert unknown.model_read_version == 1
    # a section the facts lack keeps the old rows, and the joins are read again
    partial = with_model_facts(report, {"version": 1, "tables": []})
    assert partial.tables == report.tables and partial.relationships == report.relationships
    assert partial.model == {"version": 1, "tables": []}
    assert partial.relationships_version == 0
    assert partial.model_read_version == 1
    newer = with_model_facts(report.model_copy(update={"model_read_version": 2}), None)
    assert newer.model_read_version == 2


def test_joins_renamed_in_desktop_still_find_the_rows(tmp_path: Path) -> None:
    from types import SimpleNamespace

    rows_model = json.loads(json.dumps(FACTS))
    rows_model["tables"].append({"name": "Regions", "source": "m", "lineageTag": "t-g"})
    rows_model["columns"].append(
        {"table": "Regions", "name": "Name", "kind": "data", "dataType": 2, "lineageTag": "c-g"}
    )
    head = json.loads(json.dumps(rows_model))
    head["tables"][0]["name"] = "Sales Orders"  # renamed in Desktop: same lineage tag
    for column in head["columns"]:
        if column["table"] == "Orders":
            column["table"] = "Sales Orders"
        if column["name"] == "Region":
            column["name"] = "Area"
    head["relationships"] = [
        {"fromTable": "Sales Orders", "fromColumn": "Area", "toTable": "Regions",
         "toColumn": "Name", "cardinality": "M:1", "crossFilter": "single", "active": True},
    ]  # fmt: skip

    def table(index: int, name: str, columns: list[str]) -> Any:
        described = SimpleNamespace(
            name=name,
            columns=[SimpleNamespace(name=c, id=f"c{index}_{j}") for j, c in enumerate(columns)],
        )
        return SimpleNamespace(id=f"t{index}", table=described)

    snapshot: Any = SimpleNamespace(
        id="snap-1", tables=[table(0, "Orders", ["Region", "Sales"]), table(1, "Regions", ["Name"])]
    )
    report = with_model_facts(base_report(), head).model_copy(
        update={"snapshot_model": model_facts.digest(rows_model, "k")}
    )
    joins = report_import.imported_relationships(report, snapshot)
    assert [(j.from_table_id, j.from_column_id, j.to_table_id, j.to_column_id) for j in joins] == [
        ("t0", "c0_0", "t1", "c1_0")
    ]
    assert report_import.relationships_complete(report, snapshot) is True
    # Without the digest the new names match no rows: the join is reported missing, not guessed.
    unknown = report.model_copy(update={"snapshot_model": None})
    assert report_import.imported_relationships(unknown, snapshot) == []
    assert report_import.relationships_complete(unknown, snapshot) is False


def test_the_facts_are_kept_per_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "reports" / "report-1"
    store_facts(root, "aaaa-1", FACTS)
    assert cached_facts(root, "aaaa-1") == FACTS
    assert cached_facts(root, "bbbb-2") is None
    store_facts(root, "none", FACTS)  # a report without a model has nothing to key them by
    assert not (root / report_import.FACTS_DIR / "none.json").exists()
    kept = root / report_import.FACTS_DIR / "aaaa-1.json"
    kept.write_text(json.dumps({"v": 99, "model_key": "aaaa-1", "facts": FACTS}), "utf-8")
    assert cached_facts(root, "aaaa-1") is None  # another version is read again
    kept.write_text("not json", "utf-8")
    assert cached_facts(root, "aaaa-1") is None
    monkeypatch.setattr(report_import, "FACTS_KEEP", 3)
    long_ago = time.time() - 1000
    os.utime(kept, (long_ago, long_ago))
    for index in range(5):
        store_facts(root, f"key-{index}", {"version": 1, "n": index})
        earlier = time.time() - 100 + index
        os.utime(root / report_import.FACTS_DIR / f"key-{index}.json", (earlier, earlier))
    store_facts(root, "key-5", {"version": 1, "n": 5})
    names = sorted(p.name for p in (root / report_import.FACTS_DIR).glob("*.json"))
    assert names == ["key-3.json", "key-4.json", "key-5.json"]  # the newest three


def test_the_data_is_read_again_into_a_new_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    root = state / "reports" / "report-1"
    root.mkdir(parents=True)
    original = pbix(root / "original.pbix")
    digest = sha256_file(original)
    old_digest = model_facts.digest(FACTS, "old-key")
    report = base_report(
        archived_path=str(original),
        source_hash=digest,
        base_hash=digest,
        workspace_id="ws-1",
        model=model_facts.stored(FACTS),
        snapshot_model=old_digest,
    )
    refreshed = json.loads(json.dumps(FACTS))
    refreshed["partitions"][0]["refreshedTime"] = "2024-03-01T00:00:00+00:00"
    assert model_facts.data_status(refreshed, old_digest)["stale"] is True
    monkeypatch.setattr(report_import, "extract_model", fake_extraction(refreshed, [7.0, 8.0]))
    ingestion = StudioIngestionService(state)
    updated, snapshot = reread_data(report, state, ingestion)
    assert snapshot is not None and updated.snapshot_id == snapshot.id != "snap-1"
    frame = pd.read_parquet(snapshot.tables[0].normalized_parquet_path)
    assert frame["Sales"].tolist() == [7.0, 8.0]
    key = model_key(original)
    assert updated.snapshot_model == model_facts.digest(refreshed, key)
    assert model_facts.data_status(updated.model, updated.snapshot_model)["stale"] is False
    assert updated.model == model_facts.stored(refreshed)
    assert updated.model_read_version == 1
    assert updated.measures == [
        {"TableName": "Orders", "Name": "Total", "Expression": "SUM(Orders[Sales])"}
    ]
    assert cached_facts(root, key) == refreshed
    assert [p.name.startswith("decoded-") for p in root.iterdir() if p.is_dir()].count(True) == 1
    assert report.snapshot_id == "snap-1"  # nothing stored: the caller stores both
    # A head whose bytes are not the recorded ones is never decoded.
    with pytest.raises(ValueError, match="changed on disk"):
        reread_data(report.model_copy(update={"source_hash": "b" * 64}), state, ingestion)


def test_reread_without_metadata_marks_the_current_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "reports" / "report-1"
    root.mkdir(parents=True)
    original = pbix(root / "original.pbix")
    report = base_report(
        archived_path=str(original), source_hash=sha256_file(original), workspace_id="ws-1"
    )
    monkeypatch.setattr(report_import, "extract_model", fake_extraction(None, [1.0]))
    updated, snapshot = reread_data(report, tmp_path, StudioIngestionService(tmp_path))
    assert snapshot is not None and updated.snapshot_id == snapshot.id
    assert (updated.model, updated.snapshot_model) == (None, None)
    assert ImportedReport.model_validate_json(updated.model_dump_json()).model_read_version == 1


# --- Studio's answers once the head's model differs from the rows ----------------------------

# The rows as Desktop stored them: Margin = [Sales] / 10.
MARGIN_FACTS: dict[str, Any] = {
    **FACTS,
    "columns": [
        *FACTS["columns"][:2],
        {"table": "Orders", "name": "Margin", "kind": "calculated", "dataType": 8,
         "expression": "[Sales] / 10", "lineageTag": "c-m"},
    ],
    "measures": [
        {"table": "Orders", "name": "Total", "expression": "SUM(Orders[Sales])"},
        {"table": "Orders", "name": "Total Margin", "expression": "SUM(Orders[Margin])"},
    ],
}  # fmt: skip


def margin_extraction(facts: dict[str, Any], rows: list[float]) -> Any:
    def extract(_source: Path, destination: Path) -> dict[str, Any]:
        frame = pd.DataFrame(
            {
                "Region": ["East", "West"][: len(rows)],
                "Sales": rows,
                "Margin": [r / 10 for r in rows],
            }
        )
        frame.to_parquet(destination / "table-0.parquet", index=False)
        return {
            "tables": [{"name": "Orders", "file": "table-0.parquet", "rows": len(rows)}],
            "schema": model_facts.schema_rows(facts),
            "measures": [
                {"TableName": m["table"], "Name": m["name"], "Expression": m["expression"]}
                for m in facts["measures"]
            ],
            "relationships": [],
            "facts": facts,
        }

    return extract


@pytest.fixture
def studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, str]]:
    from ai_powerbi_analyst.studio.report_import import ImportReportRequest
    from ai_powerbi_analyst.studio.service import StudioService

    monkeypatch.setattr(
        report_import, "extract_model", margin_extraction(MARGIN_FACTS, [100.0, 250.0])
    )
    service = StudioService(Path(__file__).resolve().parents[2], tmp_path / "state")
    try:
        workspace = service.import_existing_report(
            ImportReportRequest(
                path=str(pbix(tmp_path / "Orders.pbix")), idempotency_key="import-facts-0001"
            )
        )
        yield service, workspace.id
    finally:
        # Payload reads can start Desktop discovery even without an API lifespan.
        service.editing.close()


def move_head(service: Any, workspace_id: str, **update: Any) -> None:
    """The view after a model change Studio read back (as ``_rederive`` stores it)."""
    current = service.imported_report(workspace_id)
    service.store.put(
        "imported_report",
        current.model_copy(update=update),
        workspace_id=workspace_id,
        expected_base_hash=current.base_hash,
        idempotency_key=f"head-{sorted(update)[0]}-{len(update)}",
        mutation_payload={"moved": sorted(update)},
    )


def lab(service: Any, workspace_id: str, expression: str) -> Any:
    from ai_powerbi_analyst.studio.contracts import DaxEvaluateRequest

    result = service.evaluate_dax(
        DaxEvaluateRequest(workspace_id=workspace_id, expression=expression)
    )
    return result.rows[0][result.columns[0]]


def test_chat_and_dax_lab_answer_from_the_head_not_from_stale_rows(
    studio: tuple[Any, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Desktop changed Margin to [Sales] / 5: the stored rows still hold [Sales] / 10."""
    from ai_powerbi_analyst.studio.data_answers import ImportedAnswerPlan, ImportedQuery

    service, workspace_id = studio
    # The plan chat's planner writes for "total margin" (this toy model has no date or
    # dimension for the planner to read it by itself).
    plan = ImportedAnswerPlan(
        measure="Total Margin",
        queries=(ImportedQuery("[Total Margin]", "[Total Margin]", None),),
    )
    monkeypatch.setattr(service, "_imported_plan", lambda *_args: plan)
    workspace = service.workspace(workspace_id)
    assert lab(service, workspace_id, "[Total Margin]") == 35.0  # rows and head agree
    agreed = service._imported_data_answer(workspace, "What is the Total Margin?")
    assert agreed is not None and "35" in agreed
    head = json.loads(json.dumps(MARGIN_FACTS))
    head["columns"][2]["expression"] = "[Sales] / 5"
    move_head(service, workspace_id, model=model_facts.stored(head))
    assert lab(service, workspace_id, "[Total Margin]") == 70.0
    answer = service._imported_data_answer(workspace, "What is the Total Margin?")
    assert answer is not None and "70" in answer and "35" not in answer


def test_dax_lab_keeps_the_engines_refusal_once_the_head_is_known(
    studio: tuple[Any, str],
) -> None:
    from ai_powerbi_analyst.studio.dax_engine import DaxUnsupported
    from ai_powerbi_analyst.studio.report_engine import DaxUnavailable

    service, workspace_id = studio
    report = service.imported_report(workspace_id)
    # The head's read failed after Desktop saved another model: the rows' digest is known and
    # the head is another model. The lab subset would answer from the old rows; it must not.
    stale = {**(report.snapshot_model or {}), "model_key": "another-model"}
    move_head(service, workspace_id, model=None, snapshot_model=stale)
    with pytest.raises(DaxUnavailable, match="current model's metadata"):
        lab(service, workspace_id, "[Total]")
    # A calculated column Studio cannot calculate: refused, never read from the old rows.
    head = json.loads(json.dumps(MARGIN_FACTS))
    head["columns"][2]["expression"] = "PATH([Sales], [Sales])"
    move_head(
        service,
        workspace_id,
        model=model_facts.stored(head),
        snapshot_model=report.snapshot_model,
        relationships_version=2,
    )
    with pytest.raises(DaxUnsupported, match="Studio cannot calculate the calculated column"):
        lab(service, workspace_id, "[Total Margin]")


def test_the_data_is_read_again_through_the_service_and_the_route(
    studio: tuple[Any, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from ai_powerbi_analyst.studio.api import create_app

    service, workspace_id = studio
    before = service.workspace(workspace_id).active_snapshot_id
    # Desktop refreshed Orders: its partition was refreshed later than the rows Studio holds.
    refreshed = json.loads(json.dumps(MARGIN_FACTS))
    refreshed["partitions"][0]["refreshedTime"] = "2024-03-01T00:00:00+00:00"
    move_head(service, workspace_id, model=model_facts.stored(refreshed))
    studio_notes = service.imported_report_payload(workspace_id)["model"]["studio"]
    assert studio_notes["data"]["tables"] == ["Orders"]
    assert studio_notes["actions"] == ["reread_data"]
    monkeypatch.setattr(report_import, "extract_model", margin_extraction(refreshed, [7.0, 8.0]))
    token = "reread-route-test-token"
    with TestClient(create_app(service, token)) as client:
        headers = {"Authorization": f"Bearer {token}"}
        answer = client.post(
            f"/v1/workspaces/{workspace_id}/imported-report/reread-data",
            headers=headers,
            json={"idempotency_key": "reread-key-0001"},
        )
        assert answer.status_code == 200, answer.text
        body = answer.json()
        after = service.workspace(workspace_id).active_snapshot_id
        assert body["snapshot_id"] == after != before
        assert body["model"]["studio"]["data"]["stale"] is False
        assert body["model"]["studio"]["actions"] == []
        assert lab(service, workspace_id, "[Total]") == 15.0  # the new rows
        # the model workbench reads the full facts, one table at a time
        full = client.get(
            f"/v1/workspaces/{workspace_id}/imported-report/model",
            headers=headers,
            params={"table": "orders"},
        )
        assert full.status_code == 200, full.text
        orders = full.json()
        assert orders["table"]["name"] == "Orders" and orders["status"] == "current"
        assert [c["name"] for c in orders["columns"]] == ["Region", "Sales", "Margin"]
        assert orders["partitions"][0]["expression"] == "let x = 1 in x"
        assert [m["expression"] for m in orders["measures"]] == [
            "SUM(Orders[Sales])",
            "SUM(Orders[Margin])",
        ]
        missing = client.get(
            f"/v1/workspaces/{workspace_id}/imported-report/model",
            headers=headers,
            params={"table": "Nope"},
        )
        assert missing.status_code == 404


def test_the_full_facts_keep_power_query_the_payload_cuts(studio: tuple[Any, str]) -> None:
    service, workspace_id = studio
    report = service.imported_report(workspace_id)
    root = Path(report.archived_path).parent
    key = model_key(Path(report.archived_path))
    long_m = "let Source = " + "x" * 10_000 + " in Source"
    facts = json.loads(json.dumps(MARGIN_FACTS))
    facts["partitions"][0]["expression"] = long_m
    store_facts(root, key, facts)
    move_head(service, workspace_id, model=model_facts.stored(facts))
    payload = service.imported_report_payload(workspace_id)["model"]
    assert payload["truncated"] == ["partitions"]
    assert payload["partitions"][0]["truncated"] is True
    assert len(payload["partitions"][0]["expression"]) == 4_000
    full = service.editing.full_model_facts(workspace_id)
    assert full["storage"]["kind"] == "import"
    assert full["partitions"][0]["expression"] == long_m and "truncated" not in full
