"""Deterministic PBIR-like writer for development where Desktop is unavailable."""

from __future__ import annotations

import json
from pathlib import Path

from ai_powerbi_analyst.powerbi.backend import BackendReceipt
from ai_powerbi_analyst.powerbi.pbir_contract import build_query_state
from ai_powerbi_analyst.powerbi.recipes import (
    DashboardBlueprint,
    VisualBlueprint,
)
from ai_powerbi_analyst.powerbi.theme import (
    EXECUTIVE_LIGHT_THEME_FILE,
    apply_mock_executive_theme,
)
from ai_powerbi_analyst.powerbi.transaction import hash_tree
from ai_powerbi_analyst.powerbi.validation import is_approved_populated_evidence_slot

PAGE_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json"
PAGES_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json"
VISUAL_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.7.0/schema.json"


class MockPowerBIBackend:
    """Write deterministic recipe pages without claiming Desktop compatibility."""

    requires_external_validation = False

    def __init__(self, *, theme_path: Path | None = None) -> None:
        self.theme_path = theme_path or (
            Path(__file__).resolve().parents[3] / "config" / EXECUTIVE_LIGHT_THEME_FILE
        )

    def apply(self, report_path: Path, blueprint: DashboardBlueprint) -> BackendReceipt:
        report_path.mkdir(parents=True, exist_ok=True)
        if blueprint.style_preset != "executive_light":
            raise ValueError(f"unsupported Power BI style preset: {blueprint.style_preset}")
        apply_mock_executive_theme(report_path, self.theme_path)
        pages_root = report_path / "definition" / "pages"
        pages_root.mkdir(parents=True, exist_ok=True)
        metadata_path = pages_root / "pages.json"
        existing = _read_json(metadata_path) if metadata_path.exists() else {}
        prior_order = existing.get("pageOrder", [])
        if not isinstance(prior_order, list):
            prior_order = []
        managed_ids = {page.page_id for page in blueprint.pages}
        preserved_order = [
            value for value in prior_order if isinstance(value, str) and value not in managed_ids
        ]

        for page in blueprint.pages:
            page_root = pages_root / page.page_id
            visuals_root = page_root / "visuals"
            visuals_root.mkdir(parents=True, exist_ok=True)
            _write_json(
                page_root / "page.json",
                {
                    "$schema": PAGE_SCHEMA,
                    "name": page.page_id,
                    "displayName": page.title,
                    "displayOption": "FitToPage",
                    "height": page.height,
                    "width": page.width,
                },
            )
            for visual in page.visuals:
                visual_path = visuals_root / visual.visual_id / "visual.json"
                if (
                    visual.is_evidence_slot
                    and visual.visual_type == "textbox"
                    and is_approved_populated_evidence_slot(visual_path, visual)
                ):
                    continue
                _write_visual(visuals_root, visual)

        page_order = preserved_order + [page.page_id for page in blueprint.pages]
        active = existing.get("activePageName")
        if not isinstance(active, str) or active not in page_order:
            active = page_order[0] if page_order else None
        _write_json(
            metadata_path,
            {"$schema": PAGES_SCHEMA, "pageOrder": page_order, "activePageName": active},
        )
        return BackendReceipt(
            backend="mock",
            blueprint_hash=blueprint.fingerprint,
            report_hash=hash_tree(report_path),
        )

    def apply_evidence(self, report_path: Path, blueprint: DashboardBlueprint) -> BackendReceipt:
        slots = [
            (page, visual)
            for page in blueprint.pages
            for visual in page.visuals
            if visual.is_evidence_slot
        ]
        if len(slots) != 1:
            raise ValueError("evidence mutation requires exactly one reserved slot")
        page, visual = slots[0]
        visuals_root = report_path / "definition" / "pages" / page.page_id / "visuals"
        slot_root = visuals_root / visual.visual_id
        if not slot_root.is_dir():
            raise ValueError("the prepared evidence slot is missing from the report")
        _write_visual(visuals_root, visual)
        return BackendReceipt(
            backend="mock",
            blueprint_hash=blueprint.fingerprint,
            report_hash=hash_tree(report_path),
        )


def _write_visual(visuals_root: Path, visual: VisualBlueprint) -> None:
    visual_root = visuals_root / visual.visual_id
    visual_root.mkdir(exist_ok=True)
    position = visual.position
    visual_body: dict[str, object] = {
        "visualType": visual.visual_type,
        "visualContainerObjects": {
            "border": [_boolean_property(True)],
            "background": [_boolean_property(True)],
            "title": [
                {"properties": {"text": {"expr": {"Literal": {"Value": f"'{visual.title}'"}}}}}
            ],
        },
    }
    if visual.bindings:
        visual_body["query"] = {
            "queryState": build_query_state(visual.visual_type, visual.bindings)
        }
        visual_body["drillFilterOtherVisuals"] = True
    _write_json(
        visual_root / "visual.json",
        {
            "$schema": VISUAL_SCHEMA,
            "name": visual.visual_id,
            "position": {
                "x": position.x,
                "y": position.y,
                "z": position.z,
                "height": position.height,
                "width": position.width,
                "tabOrder": position.tab_order,
            },
            "visual": visual_body,
        },
    )


def _boolean_property(value: bool) -> dict[str, object]:
    return {"properties": {"show": {"expr": {"Literal": {"Value": str(value).lower()}}}}}


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
