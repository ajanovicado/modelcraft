from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ai_powerbi_analyst.data.catalog import load_semantic_catalog
from ai_powerbi_analyst.powerbi.validation import validate_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT_ROOT / "fixtures/powerbi/client-wow-template"


def test_windows_pbip_template_has_official_by_path_shape_and_catalog_bindings() -> None:
    catalog = load_semantic_catalog(PROJECT_ROOT / "fixtures/data/semantic_catalog.yaml")
    report = FIXTURE / "ClientWow.Report"
    pbip = json.loads((FIXTURE / "ClientWow.pbip").read_text(encoding="utf-8"))
    definition = json.loads((report / "definition.pbir").read_text(encoding="utf-8"))
    bindings = json.loads((FIXTURE / "catalog-bindings.json").read_text(encoding="utf-8"))
    manifest = json.loads((FIXTURE / "fixture-manifest.json").read_text(encoding="utf-8"))
    catalog_path = PROJECT_ROOT / "fixtures/data/semantic_catalog.yaml"

    assert pbip["artifacts"] == [{"report": {"path": "ClientWow.Report"}}]
    assert definition["datasetReference"] == {"byPath": {"path": "../ClientWow.SemanticModel"}}
    assert bindings["catalog_version"] == catalog.version
    assert bindings["metrics"] == {metric.id: metric.powerbi_ref for metric in catalog.metrics}
    assert bindings["dimensions"] == {
        dimension.id: dimension.powerbi_ref for dimension in catalog.dimensions
    }
    assert bindings["report_objects"] == {
        report_object.id: report_object.powerbi_ref for report_object in catalog.report_objects
    }
    assert manifest["catalog_hash"] == hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    assert validate_report(report, catalog=catalog).passed


def test_windows_pbip_template_declares_prepared_report_object_tables() -> None:
    model_root = FIXTURE / "ClientWow.SemanticModel" / "definition"
    model = (model_root / "model.tmdl").read_text(encoding="utf-8")

    expected_measures = {
        "Driver Contributions": (
            "Driver Contribution — All",
            "Driver Contribution — West",
        ),
        "Forecast": (
            "Forecast Net Sales",
            "Forecast Net Sales Lower 80",
            "Forecast Net Sales Upper 80",
            "Forecast Gross Profit",
        ),
        "Scenario Grid": (
            "Approved Scenario Net Sales",
            "Approved Scenario Units",
            "Approved Scenario Gross Profit",
            "Approved Scenario Gross Margin %",
        ),
    }
    for table_name, measure_names in expected_measures.items():
        table_path = model_root / "tables" / f"{table_name}.tmdl"
        assert f"\nref table '{table_name}'\n" in model
        assert table_path.is_file()
        table = table_path.read_text(encoding="utf-8")
        assert "Zero-row preparation partition" in table
        assert "\t\t\t\t{}" in table
        for measure_name in measure_names:
            assert f"measure '{measure_name}'" in table
    assert "\n\tref table" not in model


def test_windows_pbip_template_is_explicitly_unverified_until_desktop_hook_passes() -> None:
    manifest = json.loads((FIXTURE / "fixture-manifest.json").read_text(encoding="utf-8"))
    readme = (FIXTURE / "README.md").read_text(encoding="utf-8")

    assert manifest["desktop_render_verified"] is False
    assert manifest["status"] == "windows-preparation-required"
    assert "NOT DESKTOP-VERIFIED" in readme
    assert "powerbi-report-author validate" in readme
    assert "Phase0KillTestRunner" in readme
