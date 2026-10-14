"""Production pbi-cli backend for compiled recipes."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ai_powerbi_analyst.powerbi.cli import CommandReceipt, PbiCliAdapter
from ai_powerbi_analyst.powerbi.pbir_contract import build_query_state
from ai_powerbi_analyst.powerbi.recipes import DashboardBlueprint, VisualBlueprint
from ai_powerbi_analyst.powerbi.theme import (
    normalize_executive_theme_registration,
    require_frozen_executive_theme,
)
from ai_powerbi_analyst.powerbi.transaction import hash_tree
from ai_powerbi_analyst.powerbi.validation import is_approved_populated_evidence_slot


@dataclass(frozen=True, slots=True)
class BackendReceipt:
    backend: str
    blueprint_hash: str
    report_hash: str
    commands: tuple[CommandReceipt, ...] = ()


class PowerBIBackend(Protocol):
    requires_external_validation: bool

    def apply(self, report_path: Path, blueprint: DashboardBlueprint) -> BackendReceipt: ...

    def apply_evidence(
        self, report_path: Path, blueprint: DashboardBlueprint
    ) -> BackendReceipt: ...


class PbiCliBackend:
    """Upsert stable app-owned objects while preserving every unknown object."""

    requires_external_validation = True

    def __init__(self, adapter: PbiCliAdapter, *, theme_path: Path | None = None) -> None:
        self.adapter = adapter
        self.theme_path = theme_path

    def apply(self, report_path: Path, blueprint: DashboardBlueprint) -> BackendReceipt:
        receipts: list[CommandReceipt] = []
        if blueprint.style_preset != "executive_light":
            raise ValueError(f"unsupported Power BI style preset: {blueprint.style_preset}")
        if self.theme_path is None:
            raise ValueError("production Power BI backend requires the frozen executive theme")
        require_frozen_executive_theme(self.theme_path)
        receipts.append(self.adapter.set_theme(report_path, theme_path=self.theme_path))
        # pbi-cli 3.11.1 emits legacy theme metadata that the pinned Microsoft
        # validator rejects. Normalize only this frozen registration to the
        # official report/3.2 schema before any report can be promoted.
        normalize_executive_theme_registration(report_path, self.theme_path)
        pages_root = report_path / "definition" / "pages"
        for page in blueprint.pages:
            page_root = pages_root / page.page_id
            if not page_root.exists():
                receipts.append(
                    self.adapter.add_page(
                        report_path,
                        page_id=page.page_id,
                        display_name=page.title,
                        width=page.width,
                        height=page.height,
                    )
                )
            elif not any((page_root / "visuals").glob("*/visual.json")):
                # The shipped template contains one explicitly empty app-owned
                # placeholder. Replacing an empty page cannot discard a manual
                # or unsupported visual and lets pbi-cli set its final title.
                receipts.append(self.adapter.delete_page(report_path, page_id=page.page_id))
                receipts.append(
                    self.adapter.add_page(
                        report_path,
                        page_id=page.page_id,
                        display_name=page.title,
                        width=page.width,
                        height=page.height,
                    )
                )
            for visual in page.visuals:
                visual_path = page_root / "visuals" / visual.visual_id / "visual.json"
                if (
                    visual.is_evidence_slot
                    and visual.visual_type == "textbox"
                    and is_approved_populated_evidence_slot(visual_path, visual)
                ):
                    continue
                receipts.extend(self._replace_owned_visual(report_path, page.page_id, visual))
        return self._receipt(report_path, blueprint, receipts)

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
        page_root = report_path / "definition" / "pages" / page.page_id
        slot_root = page_root / "visuals" / visual.visual_id
        if not page_root.is_dir() or not slot_root.is_dir():
            raise ValueError("the prepared evidence slot is missing from the report")
        receipts = self._replace_owned_visual(report_path, page.page_id, visual)
        return self._receipt(report_path, blueprint, receipts)

    def _replace_owned_visual(
        self,
        report_path: Path,
        page_id: str,
        visual: VisualBlueprint,
    ) -> list[CommandReceipt]:
        receipts: list[CommandReceipt] = []
        visual_root = report_path / "definition" / "pages" / page_id / "visuals" / visual.visual_id
        if visual_root.exists():
            receipts.append(
                self.adapter.delete_visual(
                    report_path,
                    page_id=page_id,
                    visual_id=visual.visual_id,
                )
            )
        position = visual.position
        receipts.append(
            self.adapter.add_visual(
                report_path,
                page_id=page_id,
                visual_id=visual.visual_id,
                visual_type=visual.visual_type,
                x=position.x,
                y=position.y,
                width=position.width,
                height=position.height,
            )
        )
        if visual.bindings:
            receipts.append(
                self.adapter.bind_visual(
                    report_path,
                    page_id=page_id,
                    visual_id=visual.visual_id,
                    bindings=tuple(
                        (binding.role, binding.reference) for binding in visual.bindings
                    ),
                )
            )
        receipts.append(
            self.adapter.set_visual_container(
                report_path,
                page_id=page_id,
                visual_id=visual.visual_id,
                title=visual.title,
            )
        )
        _normalize_visual_query_contract(visual_root / "visual.json", visual)
        return receipts

    @staticmethod
    def _receipt(
        report_path: Path,
        blueprint: DashboardBlueprint,
        receipts: list[CommandReceipt],
    ) -> BackendReceipt:
        return BackendReceipt(
            backend="pbi-cli",
            blueprint_hash=blueprint.fingerprint,
            report_hash=hash_tree(report_path),
            commands=tuple(receipts),
        )


def _normalize_visual_query_contract(
    visual_path: Path,
    blueprint: VisualBlueprint,
) -> None:
    """Correct pbi-cli role/kind inference to the frozen official PBIR roles."""

    try:
        value = json.loads(visual_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"pbi-cli did not create readable visual JSON: {visual_path}") from error
    if not isinstance(value, dict) or value.get("name") != blueprint.visual_id:
        raise ValueError("pbi-cli visual identity does not match the approved recipe")
    body = value.get("visual")
    if not isinstance(body, dict) or body.get("visualType") != blueprint.visual_type:
        raise ValueError("pbi-cli visual type does not match the approved recipe")
    if blueprint.bindings:
        body["query"] = {"queryState": build_query_state(blueprint.visual_type, blueprint.bindings)}
        body["drillFilterOtherVisuals"] = True
    else:
        body.pop("query", None)
        body.pop("drillFilterOtherVisuals", None)
    temporary = visual_path.with_name(f".{visual_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, visual_path)
