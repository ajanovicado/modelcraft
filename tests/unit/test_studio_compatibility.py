from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_powerbi_analyst.powerbi.transaction import hash_tree
from ai_powerbi_analyst.studio.compatibility import inspect_pbip


def _project(
    root: Path,
    tables: dict[str, str] | None = None,
    *,
    default: str | None = None,
    reference: dict[str, object] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    report = root / "Sales.Report"
    report.mkdir()
    model = root / "Sales.SemanticModel" / "definition"
    (model / "tables").mkdir(parents=True)
    pbip = root / "Sales.pbip"
    pbip.write_text(
        json.dumps({"version": "1.0", "artifacts": [{"report": {"path": "Sales.Report"}}]}),
        encoding="utf-8",
    )
    (report / "definition.pbir").write_text(
        json.dumps(
            {
                "version": "4.0",
                "datasetReference": reference or {"byPath": {"path": "../Sales.SemanticModel"}},
            }
        ),
        encoding="utf-8",
    )
    model_text = "model Model\n\tculture: en-US\n"
    if default is not None:
        model_text += f"\tdefaultMode: {default}\n"
    (model / "model.tmdl").write_text(model_text, encoding="utf-8")
    for name, text in (
        tables if tables is not None else {"Sales": _table("Sales", "import")}
    ).items():
        (model / "tables" / f"{name}.tmdl").write_text(text, encoding="utf-8")
    return pbip


def _table(name: str, mode: str, *, source: str | None = None) -> str:
    result = (
        f"table '{name}'\n"
        "\tcolumn Sales\n\t\tdataType: int64\n"
        f"\tpartition '{name}' = m\n\t\tmode: {mode}\n"
    )
    if source:
        result += f"\t\tdataSource: '{source}'\n"
    return (
        result
        + '\t\tsource =\n\t\t\tlet\n\t\t\t\tSource = #table({"Sales"}, {{1}})\n\t\t\tin Source\n'
    )


def test_generated_import_shape_is_supported_without_reading_rows_or_mutating(
    tmp_path: Path,
) -> None:
    pbip = _project(tmp_path)
    before = hash_tree(tmp_path)

    report = inspect_pbip(pbip)

    assert report.supported
    assert report.connection_mode == "import"
    assert report.storage_modes == ["import"]
    assert report.blocked_features == []
    assert report.project_hash == before == hash_tree(tmp_path)
    assert not any(path.suffix == ".parquet" for path in tmp_path.rglob("*"))


@pytest.mark.parametrize(
    ("mode", "expected", "blocked"),
    [
        ("directQuery", "direct_query", "DirectQuery"),
        ("DUAL", "dual", "Dual storage"),
        ("DirectLake", "direct_lake", "Direct Lake"),
    ],
)
def test_explicit_remote_capable_storage_modes_remain_outside_managed_migration(
    tmp_path: Path, mode: str, expected: str, blocked: str
) -> None:
    report = inspect_pbip(_project(tmp_path, {"Sales": _table("Sales", mode)}))

    assert not report.supported
    assert report.connection_mode == expected
    assert report.storage_modes == [expected]
    assert blocked in report.blocked_features


@pytest.mark.parametrize(
    ("default", "expected"),
    [
        ("import", "import"),
        ("directQuery", "direct_query"),
        ("dual", "dual"),
        ("directLake", "direct_lake"),
    ],
)
def test_partition_default_inherits_declared_model_mode(
    tmp_path: Path, default: str, expected: str
) -> None:
    report = inspect_pbip(
        _project(tmp_path, {"Sales": _table("Sales", "default")}, default=default)
    )

    assert report.connection_mode == expected
    assert report.storage_modes == [expected]
    assert report.supported == (expected == "import")


@pytest.mark.parametrize(
    ("left", "right", "expected_modes"),
    [
        ("import", "directQuery", ["direct_query", "import"]),
        ("dual", "directQuery", ["direct_query", "dual"]),
        ("import", "directLake", ["direct_lake", "import"]),
    ],
)
def test_composite_precedes_individual_storage_modes(
    tmp_path: Path, left: str, right: str, expected_modes: list[str]
) -> None:
    report = inspect_pbip(
        _project(tmp_path, {"Sales": _table("Sales", left), "Dates": _table("Dates", right)})
    )

    assert not report.supported
    assert report.connection_mode == "composite"
    assert report.storage_modes == expected_modes
    assert "composite model" in report.blocked_features


def test_import_and_directquery_partitions_in_one_table_are_hybrid(tmp_path: Path) -> None:
    text = _table("Sales", "import") + (
        "\tpartition Recent = m\n\t\tmode: directQuery\n"
        '\t\tsource =\n\t\t\tSql.Database("server", "database")\n'
    )
    report = inspect_pbip(_project(tmp_path, {"Sales": text}))

    assert report.connection_mode == "hybrid"
    assert report.storage_modes == ["hybrid"]
    assert "hybrid table" in report.blocked_features
    assert not report.supported


def test_hybrid_and_import_tables_are_reported_as_composite(tmp_path: Path) -> None:
    text = _table("Sales", "import") + "\tpartition Recent = m\n\t\tmode: directQuery\n"
    report = inspect_pbip(_project(tmp_path, {"Sales": text, "Dates": _table("Dates", "import")}))

    assert report.connection_mode == "composite"
    assert report.storage_modes == ["hybrid", "import"]
    assert {"composite model", "hybrid table"} <= set(report.blocked_features)


def test_multiple_explicit_directquery_data_sources_are_composite(tmp_path: Path) -> None:
    report = inspect_pbip(
        _project(
            tmp_path,
            {
                "Sales": _table("Sales", "directQuery", source="Sales source"),
                "Survey": _table("Survey", "directQuery", source="Survey source"),
            },
        )
    )

    assert report.connection_mode == "composite"
    assert report.storage_modes == ["direct_query"]
    assert not report.supported


def test_remote_report_reference_does_not_claim_underlying_storage(tmp_path: Path) -> None:
    pbip = _project(
        tmp_path,
        {"Unrelated": _table("Unrelated", "directLake")},
        reference={"byConnection": {"connectionString": "semanticmodelid=example-id"}},
    )
    report = inspect_pbip(pbip)

    assert report.connection_mode == "live"
    assert report.storage_modes == []
    assert report.blocked_features == ["live connection"]
    assert any("not established" in note for note in report.notes)
    assert not report.supported


def test_sibling_projects_and_source_text_do_not_establish_storage_modes(tmp_path: Path) -> None:
    text = (
        "/// DirectQuery composite RLS CustomConnector byConnection\n"
        + _table("Sales", "import")
        + '\tmeasure Description = "liveConnection directQuery calculationGroup"\n'
        + "\tmeasure Notes = ```\nmode: directQuery\ncalculationGroup\n```\n"
        + "\tannotation Example = Extension.Contents()\n"
    )
    pbip = _project(tmp_path, {"Sales": text})
    sibling = tmp_path / "Other.SemanticModel" / "definition"
    sibling.mkdir(parents=True)
    (sibling / "model.tmdl").write_text("model Other\n\tdefaultMode: directQuery", encoding="utf-8")
    (tmp_path / "notes.json").write_text('{"byConnection": "directQuery"}', encoding="utf-8")

    report = inspect_pbip(pbip)

    assert report.supported
    assert report.connection_mode == "import"
    assert report.storage_modes == ["import"]


def test_stale_ordering_references_do_not_declare_tables_or_roles(tmp_path: Path) -> None:
    pbip = _project(tmp_path)
    model = tmp_path / "Sales.SemanticModel" / "definition" / "model.tmdl"
    model.write_text(
        "model Model\nref table Missing\nref role DeletedRole\nref table Sales\n",
        encoding="utf-8",
    )

    report = inspect_pbip(pbip)

    assert report.supported
    assert report.connection_mode == "import"
    assert report.blocked_features == []


def test_partial_table_partition_definition_merges_by_stable_object_name(tmp_path: Path) -> None:
    pbip = _project(tmp_path, {"Sales": "table Sales\n\tcolumn Sales\n\t\tdataType: int64\n"})
    partial = tmp_path / "Sales.SemanticModel" / "definition" / "partitions.tmdl"
    partial.write_text(
        "ref table Sales\n\tpartition Sales = m\n\t\tmode: import\n"
        "\t\tsource =\n\t\t\t#table({}, {})\n",
        encoding="utf-8",
    )

    report = inspect_pbip(pbip)

    assert report.supported
    assert report.connection_mode == "import"


def test_custom_export_in_partition_source_is_blocked_while_import_mode_stays_truthful(
    tmp_path: Path,
) -> None:
    text = (
        "table Sales\n\tpartition Sales = m\n\t\tmode: import\n"
        '\t\tsource =\n\t\t\tHelloWorld.Contents("sample")\n'
    )
    report = inspect_pbip(_project(tmp_path, {"Sales": text}))

    assert report.connection_mode == "import"
    assert report.storage_modes == ["import"]
    assert "custom connector" in report.blocked_features
    assert not report.supported


@pytest.mark.parametrize(
    "feature", ["role Reader\n\tmodelPermission: read", "table Calc\n\tcalculationGroup"]
)
def test_structurally_declared_restricted_features_remain_blocked(
    tmp_path: Path, feature: str
) -> None:
    pbip = _project(tmp_path)
    (tmp_path / "Sales.SemanticModel" / "definition" / "restricted.tmdl").write_text(
        feature, encoding="utf-8"
    )

    report = inspect_pbip(pbip)

    assert not report.supported
    assert any(
        item in report.blocked_features for item in ("row-level security", "calculation group")
    )


@pytest.mark.parametrize(
    ("expression", "blocked"),
    [
        ('Extension.Contents("Connector")', True),
        ('HelloWorld.Contents("sample")', True),
        ('#"TripPin.Contents"()', True),
        ('#"Company.Web.Contents"()', True),
        ('#"Company Web.Contents"()', True),
        ("Company.Web.Contents()", True),
        ("会社.Web.Contents()", True),
        ('#"Web.Contents"("https://example.test")', False),
        ('File.Contents("orders.csv")', False),
        ('SharePoint.Contents("https://example.test")', False),
        ('"Extension.Contents(""Connector"")"', False),
        ('/* Extension.Contents("Connector") */ #table({}, {})', False),
    ],
)
def test_custom_connector_checks_calls_in_expressions_not_comments_or_data(
    tmp_path: Path, expression: str, blocked: bool
) -> None:
    pbip = _project(tmp_path)
    (tmp_path / "Sales.SemanticModel" / "definition" / "expressions.tmdl").write_text(
        f"expression Source = {expression}\n", encoding="utf-8"
    )

    report = inspect_pbip(pbip)

    assert ("custom connector" in report.blocked_features) == blocked
    assert report.supported == (not blocked)


@pytest.mark.parametrize(
    "text",
    [
        "table Sales\n\tpartition Sales = m\n\t\tmode: futureMode\n",
        "table Sales\n\tpartition Sales = m\n",
        "table Sales\n\tpartition Sales = m\n\t\tmode: import\n\t\tmode: import\n",
        "table Sales\n\tmeasure Value = 1\n\t\tpartition Broken = m\n\t\t\tmode: import\n",
        "table Sales\n\tpartition Sales = m\n\t\tmode: import\n"
        "\t\tsource = ```\nmode: directQuery\n",
        "table Sales\n",
    ],
)
def test_unknown_or_malformed_storage_metadata_is_never_assumed_import(
    tmp_path: Path, text: str
) -> None:
    report = inspect_pbip(_project(tmp_path, {"Sales": text}))

    assert not report.supported
    assert report.connection_mode == "unknown"
    assert "unknown" in report.storage_modes


@pytest.mark.parametrize(
    "artifacts",
    [[], [{"report": {"path": "Sales.Report"}}] * 2, [{"model": {"path": "Sales.SemanticModel"}}]],
)
def test_ambiguous_entrypoint_does_not_guess_a_sibling(
    tmp_path: Path, artifacts: list[object]
) -> None:
    pbip = _project(tmp_path)
    pbip.write_text(json.dumps({"artifacts": artifacts}), encoding="utf-8")

    report = inspect_pbip(pbip)

    assert report.connection_mode == "unknown"
    assert not report.supported


@pytest.mark.parametrize(
    "reference",
    [
        {},
        {"byPath": {}, "byConnection": {}},
        {"byPath": {"path": "../../Outside.SemanticModel"}},
        {"byConnection": {"connectionString": ""}},
        {"byConnection": {"unexpected": "not a connection"}},
    ],
)
def test_ambiguous_or_escaped_model_reference_is_blocked(
    tmp_path: Path, reference: dict[str, object]
) -> None:
    pbip = _project(tmp_path / "project")
    outside = tmp_path / "Outside.SemanticModel"
    outside.mkdir()
    definition = pbip.parent / "Sales.Report" / "definition.pbir"
    definition.write_text(json.dumps({"datasetReference": reference}), encoding="utf-8")

    report = inspect_pbip(pbip)

    assert not report.supported
    assert report.connection_mode == "unknown"
    assert report.storage_modes == ["unknown"]


@pytest.mark.parametrize(
    "component",
    ["Sales.pbip", "Sales.Report/definition.pbir", "Sales.SemanticModel/definition/model.tmdl"],
)
def test_invalid_or_missing_required_components_fail_closed(tmp_path: Path, component: str) -> None:
    pbip = _project(tmp_path)
    target = tmp_path / component
    if component.endswith("tmdl"):
        target.unlink()
    else:
        target.write_text("[]", encoding="utf-8")

    report = inspect_pbip(pbip)

    assert not report.supported
    assert report.connection_mode == "unknown"
