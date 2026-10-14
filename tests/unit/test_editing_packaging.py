"""The data files and dependencies the imported-report editor needs in the packaged app."""

from __future__ import annotations

import json
import re
import tomllib
from importlib.resources import files
from pathlib import Path

from ai_powerbi_analyst.studio import pbir_expr, report_edit_validation

ROOT = Path(__file__).resolve().parents[2]
BUILD = (ROOT / "scripts" / "build-desktop-sidecar.ps1").read_text(encoding="utf-8")
SCHEMAS = ("filterConfiguration-1.3.0.embedded.json", "semanticQuery-1.4.0.json")
BS = "\\"


def test_the_catalog_is_read_through_package_resources_not_a_source_path() -> None:
    pbir_expr.catalog.cache_clear()
    loaded = pbir_expr.catalog()
    on_disk = json.loads(
        files("ai_powerbi_analyst.studio").joinpath("pbir_catalog.json").read_text(encoding="utf-8")
    )
    assert loaded == on_disk
    assert isinstance(pbir_expr._CATALOG, str)
    assert "tableEx" in loaded["visualTypes"]


def test_every_packaged_schema_exists_under_src_and_parses() -> None:
    folder = files("ai_powerbi_analyst.studio").joinpath("pbir_schemas")
    for name in SCHEMAS:
        parsed = json.loads(folder.joinpath(name).read_text(encoding="utf-8"))
        assert parsed["$id"].startswith("https://developer.microsoft.com/json-schemas/")


def test_the_filter_schema_check_is_live_with_the_packaged_files() -> None:
    report_edit_validation._filter_validator.cache_clear()
    assert report_edit_validation._filter_validator() is not None


def test_the_build_script_bundles_schemas_and_the_catalog_and_keeps_jsonschema() -> None:
    package = f"ai_powerbi_analyst{BS}studio"
    assert f'--add-data "$pbirSchemas;{package}{BS}pbir_schemas"' in BUILD
    assert f'--add-data "$pbirCatalog;{package}"' in BUILD
    assert f"'src{BS}{package}{BS}pbir_schemas'" in BUILD
    assert "--collect-data jsonschema_specifications" in BUILD
    assert (ROOT / "src" / "ai_powerbi_analyst" / "studio" / "pbir_schemas").is_dir()


def test_jsonschema_is_a_declared_dependency_with_the_registry_floor() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pins = [d for d in project["dependencies"] if re.match(r"jsonschema\b", d)]
    assert pins == ["jsonschema>=4.18,<5"]


def test_the_notices_name_both_schemas() -> None:
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for name in SCHEMAS:
        assert name in notices
