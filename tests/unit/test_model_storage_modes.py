"""An embedded row cache does not establish the complete semantic model's storage mode."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from ai_powerbi_analyst.studio import model_facts, report_engine
from ai_powerbi_analyst.studio.dax_engine import run_query
from ai_powerbi_analyst.studio.report_engine import DaxUnavailable, LocalReportEngine
from ai_powerbi_analyst.studio.report_import import ImportedPage, ImportedReport, model_fingerprint


def facts(modes: list[object], default: object = None) -> dict[str, Any]:
    return {
        "version": 1,
        "model": {"defaultMode": default},
        "tables": [{"name": name, "calculated": False} for name in ("Fact", "Local")],
        "columns": [
            {"table": name, "name": "Amount", "kind": "data", "dataType": 6}
            for name in ("Fact", "Local")
        ],
        "measures": [{"table": "Fact", "name": "Total", "expression": "SUM(Fact[Amount])"}],
        "relationships": [],
        "partitions": [
            {"table": "Fact", "name": f"P{i}", "source": "m", "mode": mode}
            for i, mode in enumerate(modes)
        ]
        + [{"table": "Local", "name": "P", "source": "m", "mode": "import"}],
    }


def report(head: dict[str, Any]) -> ImportedReport:
    return ImportedReport(
        id="report-storage",
        workspace_id="ws-storage",
        name="Storage",
        source_path="fixture.pbix",
        archived_path="fixture.pbix",
        source_hash="a" * 64,
        base_hash="a" * 64,
        format="PBIR",
        pages=[ImportedPage(id="p", title="Page")],
        tables=model_facts.schema_rows(head) or [],
        measures=[{"TableName": "Fact", "Name": "Total", "Expression": "SUM(Fact[Amount])"}],
        relationships=[],
        relationships_version=2,
        snapshot_id="snapshot-storage",
        model=head,
    )


@pytest.fixture
def snapshot(tmp_path: Path) -> Any:
    tables = []
    for name in ("Fact", "Local"):
        path = tmp_path / f"{name}.parquet"
        pd.DataFrame({"Amount": [10, 20]}).to_parquet(path, index=False)
        tables.append(
            SimpleNamespace(
                table=SimpleNamespace(
                    name=name, row_count=2, columns=[SimpleNamespace(name="Amount")]
                ),
                normalized_parquet_path=str(path),
            )
        )
    return SimpleNamespace(id="snapshot-storage", tables=tables)


@pytest.mark.parametrize(
    ("modes", "default", "expected"),
    [
        (["IMPORT"], None, "import"),
        ([" direct_query "], None, "direct_query"),
        (["dIrEcTqUeRy"], "Import", "direct_query"),
        (["DIRECTLAKE"], None, "direct_lake"),
        (["Dual"], None, "dual"),
        (["Default"], "DirectQuery", "direct_query"),
        ([None], "DIRECT_LAKE", "direct_lake"),
        (["Default", "import"], "Import", "import"),
        (["import", "directQuery"], "Import", "hybrid"),
        (["DirectQuery", "Import"], None, "hybrid"),
        ([None], None, "unknown"),
        (["Default"], "Default", "unknown"),
        (["newFutureMode"], "Import", "unknown"),
        (["Push"], "Import", "unknown"),
        ([], "Import", "unknown"),
        (["Import", "Dual"], None, "unknown"),
    ],
)
def test_effective_modes_are_case_independent_and_fail_closed(modes, default, expected):
    table = model_facts.table_storage_modes(facts(modes, default))[0]
    assert table["mode"] == expected
    assert table["local_evaluation"] == (
        "snapshot" if expected == "import" else "unknown" if expected == "unknown" else "power_bi"
    )


@pytest.mark.parametrize(
    ("modes", "default", "why"),
    [
        (["DirectQuery"], None, "DirectQuery"),
        (["Default"], "DirectQuery", "DirectQuery"),
        ([None], "DirectQuery", "DirectQuery"),
        (["DUAL"], None, "dual mode"),
        (["direct_lake"], None, "Direct Lake"),
        (["import", "directQuery"], None, "hybrid mode"),
        (["future"], "Import", "unknown or incomplete"),
        ([None], None, "unknown or incomplete"),
        ([], "Import", "unknown or incomplete"),
    ],
)
def test_cached_rows_never_enable_live_or_unknown_table_answers(snapshot, modes, default, why):
    imported = report(facts(modes, default))
    engine = LocalReportEngine(report_engine._build(imported, snapshot))
    with pytest.raises(DaxUnavailable, match=why):
        engine.query(imported, 'EVALUATE ROW("Total", [Total])')
    assert engine.query(imported, 'EVALUATE ROW("Local", SUM(Local[Amount]))')["rows"] == [
        {"[Local]": 30}
    ]


def test_verified_import_and_legacy_unknown_facts_keep_snapshot_evaluation(snapshot):
    imported = report(facts(["Import"]))
    model = report_engine._build(imported, snapshot)
    assert run_query(model, 'EVALUATE ROW("Total", [Total])').rows == [[30]]
    legacy = imported.model_copy(update={"model": None})
    assert run_query(
        report_engine._build(legacy, snapshot), 'EVALUATE ROW("Total", [Total])'
    ).rows == [[30]]
    assert model_facts.storage_summary(None)["kind"] == "unknown"


def test_hybrid_summary_and_cache_follow_complete_partition_evidence(snapshot):
    summary = model_facts.storage_summary(facts(["import", "DIRECTQUERY"]))
    assert summary["known"] is True and summary["kind"] == "mixed"
    assert summary["table_counts"]["hybrid"] == 1
    assert summary["tables"][0]["partition_modes"] == ["direct_query", "import"]
    assert summary["tables"][0]["local_evaluation"] == "power_bi"
    imported = report(facts(["import"]))
    truncated = imported.model_copy(
        update={"model": {**imported.model, "truncated": ["partitions"]}}
    )
    assert model_fingerprint(imported) != model_fingerprint(truncated)
    assert model_facts.storage_summary(truncated.model)["kind"] == "unknown"
    assert all(
        table["mode"] == "unknown" for table in model_facts.table_storage_modes(truncated.model)
    )
    with pytest.raises(DaxUnavailable, match="unknown or incomplete"):
        LocalReportEngine(report_engine._build(truncated, snapshot)).query(
            truncated, 'EVALUATE ROW("Total", [Total])'
        )


def test_summary_excludes_queries_and_does_not_guess_remote_storage():
    head = facts(["DirectQuery"])
    head["partitions"][0]["expression"] = 'Sql.Database("private-server", "private-database")'
    head["model"]["connectionString"] = "password=secret"
    summary = model_facts.storage_summary(head)
    assert not any(
        secret in json.dumps(summary) for secret in ("private-server", "private-database", "secret")
    )
    assert summary["kind"] == "mixed"
    assert "composite" in summary["detail"]
    for status in ("reading", "unknown", "none"):
        payload = model_facts.payload(head, None, status=status)
        assert payload["storage"]["tables"] == []
        assert "private-server" not in json.dumps(payload)
        assert payload["storage"]["kind"] == ("no_local_model" if status == "none" else "unknown")
    missing = model_facts.storage_summary(None, has_model=False)
    assert missing["known"] is True and missing["kind"] == "no_local_model"
    assert "not been verified" in missing["detail"]


def test_calculated_import_and_truncated_inventory_are_not_misclassified():
    head = facts([])
    head["tables"][0]["calculated"] = True
    assert model_facts.table_storage_modes(head)[0]["mode"] == "import"
    assert model_facts.storage_summary(head)["kind"] == "import"
    head["truncated"] = ["tables"]
    assert model_facts.storage_summary(head)["known"] is False
    assert model_facts.storage_summary(head)["kind"] == "unknown"
