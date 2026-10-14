from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_powerbi_analyst.powerbi.theme import (
    EXECUTIVE_LIGHT_THEME_FILE,
    EXECUTIVE_LIGHT_THEME_SHA256,
    THEME_VERSION_AT_IMPORT,
    apply_mock_executive_theme,
    require_frozen_executive_theme,
)

THEME_PATH = Path(__file__).parents[2] / "config" / EXECUTIVE_LIGHT_THEME_FILE


def test_frozen_executive_theme_has_release_identity() -> None:
    theme = require_frozen_executive_theme(THEME_PATH)

    assert theme["name"] == EXECUTIVE_LIGHT_THEME_FILE
    assert len(EXECUTIVE_LIGHT_THEME_SHA256) == 64


def test_frozen_executive_theme_rejects_modified_bytes(tmp_path: Path) -> None:
    modified = tmp_path / EXECUTIVE_LIGHT_THEME_FILE
    modified.write_bytes(THEME_PATH.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="hash"):
        require_frozen_executive_theme(modified)


def test_mock_theme_registration_matches_pbi_cli_contract(tmp_path: Path) -> None:
    report = tmp_path / "Sales.Report"

    apply_mock_executive_theme(report, THEME_PATH)

    report_json = json.loads((report / "definition" / "report.json").read_text(encoding="utf-8"))
    assert report_json["themeCollection"]["customTheme"] == {
        "name": EXECUTIVE_LIGHT_THEME_FILE,
        "reportVersionAtImport": THEME_VERSION_AT_IMPORT,
        "type": "RegisteredResources",
    }
    registered = next(
        package
        for package in report_json["resourcePackages"]
        if package["name"] == "RegisteredResources"
    )
    assert registered["items"] == [
        {
            "name": EXECUTIVE_LIGHT_THEME_FILE,
            "type": "CustomTheme",
            "path": EXECUTIVE_LIGHT_THEME_FILE,
        }
    ]
    assert (
        report / "StaticResources" / "RegisteredResources" / EXECUTIVE_LIGHT_THEME_FILE
    ).read_bytes() == THEME_PATH.read_bytes()
