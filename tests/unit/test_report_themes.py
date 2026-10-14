from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import get_args

import pytest

from ai_powerbi_analyst.contracts import ReportThemeId
from ai_powerbi_analyst.data.onboarding import DataOnboardingService, UploadedSource
from ai_powerbi_analyst.powerbi import semantic_model as semantic_model_module
from ai_powerbi_analyst.powerbi.semantic_model import SemanticProjectBuilder
from ai_powerbi_analyst.powerbi.theme import (
    EXECUTIVE_LIGHT_THEME_SHA256,
    studio_theme_bytes,
    studio_theme_registry,
)
from ai_powerbi_analyst.preflight import ToolProbe
from ai_powerbi_analyst.studio.layout_templates import choose_theme
from ai_powerbi_analyst.workspace.planner import DashboardPlanningService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
THEMED = [theme for theme in get_args(ReportThemeId) if theme != "default"]


def _luminance(color: str) -> float:
    channels = [int(color.lstrip("#")[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_registry_matches_typed_contract_and_pins_every_file() -> None:
    registry = studio_theme_registry()
    assert set(registry) == set(get_args(ReportThemeId))
    assert studio_theme_bytes("default") is None
    for theme_id in THEMED:
        loaded = studio_theme_bytes(theme_id)
        assert loaded is not None
        name, payload = loaded
        assert hashlib.sha256(payload).hexdigest() == registry[theme_id]["sha256"]
        # Microsoft's PBIR validator: PBIR_THEME_FILE_NAME_MISMATCH otherwise.
        assert json.loads(payload)["name"] == name
    # The Studio copy of the frozen blueprint theme must stay byte-identical.
    executive = studio_theme_bytes("executive_light")
    assert executive is not None
    assert hashlib.sha256(executive[1]).hexdigest() == EXECUTIVE_LIGHT_THEME_SHA256


@pytest.mark.parametrize("theme_id", THEMED)
def test_theme_text_and_leading_data_colors_meet_wcag_contrast(theme_id: ReportThemeId) -> None:
    loaded = studio_theme_bytes(theme_id)
    assert loaded is not None
    theme = json.loads(loaded[1])
    surface = theme["secondaryBackground"]
    assert _contrast(theme["foreground"], surface) >= 4.5
    assert _contrast(theme["secondLevelElements"], surface) >= 4.5
    # Single-series Studio visuals use the leading colors; require 3:1 non-text contrast.
    # executive_light is the frozen blueprint theme (its fifth color is 2.4:1), so it
    # is checked only through its first four colors.
    leading = 4 if theme_id == "executive_light" else 5
    for color in theme["dataColors"][:leading]:
        assert _contrast(color, surface) >= 3.0, color
    # Themes may style only native visuals; custom visuals are outside the release scope.
    assert set(theme.get("visualStyles", {})) <= {"*", "page", "lineChart", "clusteredBarChart"}


def test_explicit_prompt_term_outranks_model_theme_choice() -> None:
    assert choose_theme("Show margin health for the board", "finance_neutral") == (
        "finance_neutral",
        "local_model",
    )
    assert choose_theme("Make it colorblind friendly", "calm_teal") == (
        "accessible_contrast",
        "prompt_rule",
    )
    assert choose_theme("Build a sales dashboard", "finance_neutral") == (
        "finance_neutral",
        "local_model",
    )
    assert choose_theme("Build a sales dashboard", None) == ("default", "default")


@pytest.mark.parametrize("theme_id", ["default", "accessible_contrast"])
def test_studio_build_registers_the_reviewed_theme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, theme_id: ReportThemeId
) -> None:
    manifest = DataOnboardingService(tmp_path / "data").import_uploads(
        [
            UploadedSource(
                name="sales.csv",
                content=b"Order Date,Region,Sales\n2024-07-15,East,100\n2025-02-03,West,150\n",
            )
        ]
    )
    plan = DashboardPlanningService(None).plan("Build a sales trend", manifest, [])
    plan = plan.model_copy(
        update={
            "approved": True,
            "report_theme": theme_id,
            "semantic_model": plan.semantic_model.model_copy(update={"approved": True}),
        }
    )
    monkeypatch.setattr(
        semantic_model_module, "probe_pbi_cli", lambda: ToolProbe(name="pbi", available=False)
    )
    monkeypatch.setattr(
        semantic_model_module, "probe_tool", lambda name: ToolProbe(name=name, available=False)
    )
    builder = SemanticProjectBuilder(
        tmp_path / "projects",
        PROJECT_ROOT / "fixtures" / "powerbi" / "client-wow-template",
        desktop_closed_check=lambda: None,
        require_external_validators=False,
    )

    pbip, receipt = builder.build("Themed Report", manifest, plan)

    assert receipt.passed, receipt.issues
    report = next(pbip.parent.glob("*.Report"))
    definition = json.loads((report / "definition" / "report.json").read_text(encoding="utf-8"))
    assert definition["themeCollection"]["baseTheme"]["name"] == "CY24SU06"
    resources = report / "StaticResources" / "RegisteredResources"
    if theme_id == "default":
        assert "customTheme" not in definition["themeCollection"]
        assert not resources.exists()
        return
    loaded = studio_theme_bytes(theme_id)
    assert loaded is not None
    name, payload = loaded
    assert definition["themeCollection"]["customTheme"]["name"] == name
    assert definition["themeCollection"]["customTheme"]["type"] == "RegisteredResources"
    items = next(p for p in definition["resourcePackages"] if p["name"] == "RegisteredResources")
    assert items["items"] == [{"name": name, "type": "CustomTheme", "path": name}]
    assert (resources / name).read_bytes() == payload


def test_palettes_describe_every_theme_without_paths() -> None:
    from ai_powerbi_analyst.powerbi.theme import studio_theme_palettes

    palettes = {item["id"]: item for item in studio_theme_palettes()}
    assert set(palettes) == set(get_args(ReportThemeId))
    assert palettes["default"]["data_colors"][0] == "#118DFF"
    assert palettes["accessible_contrast"]["data_colors"][0] == "#0072B2"
    assert all("file" not in item and "sha256" not in item for item in palettes.values())


def test_draft_theme_reaches_the_build_plan_without_a_theme_mutation(tmp_path: Path) -> None:
    from ai_powerbi_analyst.studio.api import create_app
    from ai_powerbi_analyst.studio.contracts import (
        ApproveDraftRequest,
        CreateWorkspaceRequest,
        DraftRequest,
        IngestRequest,
    )
    from ai_powerbi_analyst.studio.planner import StudioPlanningService, compile_powerbi_plan
    from ai_powerbi_analyst.studio.service import StudioService
    from ai_powerbi_analyst.studio.state import EMPTY_HASH

    source_root = tmp_path / "data"
    source_root.mkdir()
    (source_root / "orders.csv").write_text(
        "Order Date,Region,Sales\n2025-01-01,East,100\n2025-02-01,West,250\n", encoding="utf-8"
    )
    service = StudioService(PROJECT_ROOT, tmp_path / "state")
    service.planner = StudioPlanningService(None, service.aggregates)
    workspace = service.create_workspace(
        CreateWorkspaceRequest(
            name="Theme",
            selected_folder=str(source_root),
            idempotency_key="theme-workspace",
            expected_base_hash=EMPTY_HASH,
        )
    )
    snapshot = service.ingest(
        IngestRequest(
            workspace_id=workspace.id,
            item_paths=[str(source_root / "orders.csv")],
            idempotency_key="theme-ingest",
            expected_base_hash=workspace.base_hash,
        )
    )
    workspace = service.workspace(workspace.id)
    draft = service.create_draft(
        DraftRequest(
            workspace_id=workspace.id,
            snapshot_id=snapshot.id,
            prompt="Build a sales dashboard",
            idempotency_key="theme-draft",
            expected_base_hash=workspace.base_hash,
        )
    )
    approved = service.approve_draft(
        draft.id,
        ApproveDraftRequest(
            workspace_id=workspace.id,
            idempotency_key="theme-approve",
            expected_base_hash=draft.base_hash,
        ),
    )

    manifest = service.ingestion.load_manifest(snapshot)
    assert approved.status == "approved"
    assert compile_powerbi_plan(approved, manifest).report_theme == "default"
    assert not hasattr(service, "set_report_theme")
    paths = {route.path for route in create_app(service, "theme-test-api-token").routes}
    assert "/v1/drafts/{draft_id}/theme" not in paths
    assert "/v1/report-themes" in paths
