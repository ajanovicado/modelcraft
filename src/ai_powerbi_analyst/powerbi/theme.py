"""Hash-pinned Power BI report themes and their PBIR registration."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import get_args

from ai_powerbi_analyst.contracts import ReportThemeId

EXECUTIVE_LIGHT_THEME_FILE = "ExecutiveLight-7f3a2c1d.json"
EXECUTIVE_LIGHT_THEME_SHA256 = "6f5218134acc42cceefae0e202d7fd7d8c20910accbd11b725e7684c13895743"
THEME_VERSION_AT_IMPORT = {
    "visual": "2.6.0",
    "report": "3.1.0",
    "page": "2.3.0",
}


def require_frozen_executive_theme(theme_path: Path) -> dict[str, object]:
    """Load the one approved theme and prove its immutable release identity."""

    if theme_path.name != EXECUTIVE_LIGHT_THEME_FILE:
        raise ValueError("executive-light theme filename is invalid")
    payload = theme_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != EXECUTIVE_LIGHT_THEME_SHA256:
        raise ValueError("executive-light theme hash is invalid")
    theme = json.loads(payload)
    if not isinstance(theme, dict) or theme.get("name") != EXECUTIVE_LIGHT_THEME_FILE:
        raise ValueError("executive-light theme identity is invalid")
    return theme


def normalize_executive_theme_registration(report_path: Path, theme_path: Path) -> None:
    """Write the frozen theme using the official report 3.2 PBIR contract."""

    require_frozen_executive_theme(theme_path)
    _register_custom_theme(report_path, EXECUTIVE_LIGHT_THEME_FILE, theme_path.read_bytes())


def studio_theme_registry() -> dict[str, dict[str, object]]:
    """Return a copy of the reviewed Studio theme registry keyed by theme ID."""

    return {key: dict(value) for key, value in _studio_registry().items()}


@lru_cache(maxsize=1)
def _studio_registry() -> dict[str, dict[str, object]]:
    text = (
        files("ai_powerbi_analyst.powerbi")
        .joinpath("report_themes/registry.json")
        .read_text(encoding="utf-8")
    )
    registry = json.loads(text)
    themes = {str(item["id"]): dict(item) for item in registry["themes"]}
    if sorted(themes) != sorted(get_args(ReportThemeId)):
        raise ValueError("report_themes/registry.json must define exactly the ReportThemeId values")
    return themes


def studio_theme_bytes(theme_id: ReportThemeId) -> tuple[str, bytes] | None:
    """Load one registered theme and prove its pinned identity; ``default`` has no file."""

    entry = _studio_registry()[theme_id]
    file_name, expected = entry["file"], entry["sha256"]
    if file_name is None:
        return None
    if not isinstance(file_name, str) or not isinstance(expected, str):
        raise ValueError(f"report theme {theme_id} registry entry is invalid")
    payload = (
        files("ai_powerbi_analyst.powerbi").joinpath(f"report_themes/{file_name}").read_bytes()
    )
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(f"report theme {theme_id} hash is invalid")
    if not isinstance(json.loads(payload), dict):
        raise ValueError(f"report theme {theme_id} must contain an object")
    return file_name, payload


# Power BI's built-in base theme palette, used when no custom theme is registered.
POWER_BI_DEFAULT_COLORS = [
    "#118DFF",
    "#12239E",
    "#E66C37",
    "#6B007B",
    "#E044A7",
    "#744EC2",
    "#D9B300",
    "#D64550",
]


def studio_theme_palettes() -> list[dict[str, object]]:
    """Display metadata for the theme picker and local previews; no file paths."""

    palettes: list[dict[str, object]] = []
    for theme_id, entry in _studio_registry().items():
        loaded = studio_theme_bytes(theme_id)  # type: ignore[arg-type]
        theme = json.loads(loaded[1]) if loaded is not None else {}
        palettes.append(
            {
                "id": theme_id,
                "label": entry["label"],
                "use_when": entry["use_when"],
                "data_colors": list(theme.get("dataColors", POWER_BI_DEFAULT_COLORS))[:8],
                "background": theme.get("background", "#FFFFFF"),
                "foreground": theme.get("foreground", "#252423"),
            }
        )
    return palettes


def register_studio_theme(report_path: Path, theme_id: ReportThemeId) -> None:
    """Register a reviewed Studio theme; ``default`` keeps the template base theme only."""

    loaded = studio_theme_bytes(theme_id)
    if loaded is not None:
        _register_custom_theme(report_path, *loaded)


def _register_custom_theme(report_path: Path, resource_name: str, payload: bytes) -> None:
    report_json_path = report_path / "definition" / "report.json"
    report_json_path.parent.mkdir(parents=True, exist_ok=True)
    if report_json_path.exists():
        report = json.loads(report_json_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("report.json must contain an object")
    else:
        report = {}
    collection = report.setdefault("themeCollection", {})
    if not isinstance(collection, dict):
        raise ValueError("report themeCollection must be an object")
    collection["customTheme"] = {
        "name": resource_name,
        "reportVersionAtImport": dict(THEME_VERSION_AT_IMPORT),
        "type": "RegisteredResources",
    }
    packages = report.setdefault("resourcePackages", [])
    if not isinstance(packages, list):
        raise ValueError("report resourcePackages must be an array")
    registered = next(
        (
            item
            for item in packages
            if isinstance(item, dict) and item.get("name") == "RegisteredResources"
        ),
        None,
    )
    if registered is None:
        registered = {
            "name": "RegisteredResources",
            "type": "RegisteredResources",
            "items": [],
        }
        packages.append(registered)
    items = registered.setdefault("items", [])
    if not isinstance(items, list):
        raise ValueError("registered resource items must be an array")
    items[:] = [
        item for item in items if not isinstance(item, dict) or item.get("name") != resource_name
    ]
    items.append(
        {
            "name": resource_name,
            "type": "CustomTheme",
            "path": resource_name,
        }
    )
    report_json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    destination = report_path / "StaticResources" / "RegisteredResources"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / resource_name).write_bytes(payload)


def apply_mock_executive_theme(report_path: Path, theme_path: Path) -> None:
    """Apply the same official theme registration in the deterministic mock writer."""

    normalize_executive_theme_registration(report_path, theme_path)
