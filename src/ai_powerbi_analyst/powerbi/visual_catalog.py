"""Load the installed Microsoft PBIR visual catalog into a closed local registry."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

from ai_powerbi_analyst.contracts import VisualTypeCapability
from ai_powerbi_analyst.preflight import probe_tool
from ai_powerbi_analyst.workspace.planner import default_visual_catalog

_ONLINE_OR_EXTERNAL = {
    "aiNarratives",
    "azureMap",
    "filledMap",
    "map",
    "pythonVisual",
    "qnaVisual",
    "rdlVisual",
    "realTimeLineChart",
    "scorecard",
    "scriptVisual",
}


class VisualCatalogService:
    def __init__(self, cache_path: str | Path) -> None:
        self.cache_path = Path(cache_path).expanduser().resolve(strict=False)

    def load(self, *, refresh: bool = False) -> tuple[VisualTypeCapability, ...]:
        if not refresh and self.cache_path.is_file():
            try:
                value = json.loads(self.cache_path.read_text(encoding="utf-8"))
                cached = tuple(VisualTypeCapability.model_validate(item) for item in value)
                if all(not item.roles or item.role_kinds for item in cached):
                    return cached
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        discovered = self._discover()
        if discovered:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temporary.write_text(
                json.dumps([item.model_dump(mode="json") for item in discovered], indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.cache_path)
            return discovered
        return default_visual_catalog()

    def _discover(self) -> tuple[VisualTypeCapability, ...]:
        probe = probe_tool("powerbi-report-author")
        if not probe.available or probe.path is None:
            return ()
        listed = _run_json((probe.path, "catalog", "list"))
        data = listed.get("data") if isinstance(listed, dict) else None
        visual_types = data.get("visualTypes") if isinstance(data, dict) else None
        if not isinstance(visual_types, list):
            return ()
        capabilities: list[VisualTypeCapability] = []
        for raw_visual_type in visual_types:
            if not isinstance(raw_visual_type, str):
                continue
            described = _run_json((probe.path, "catalog", "describe", raw_visual_type))
            record = described.get("data") if isinstance(described, dict) else None
            roles_value = record.get("roles") if isinstance(record, dict) else None
            roles = list(roles_value) if isinstance(roles_value, dict) else []
            role_kinds = (
                {
                    role: details["kind"]
                    for role, details in roles_value.items()
                    if isinstance(role, str)
                    and isinstance(details, dict)
                    and details.get("kind") in {"Grouping", "Measure", "GroupingOrMeasure"}
                }
                if isinstance(roles_value, dict)
                else {}
            )
            required_value = record.get("requiredRoles") if isinstance(record, dict) else None
            required_roles = (
                [value for value in required_value if isinstance(value, str)]
                if isinstance(required_value, list)
                else []
            )
            max_value = record.get("maxPerRole") if isinstance(record, dict) else None
            max_per_role = (
                {
                    role: value
                    for role, value in max_value.items()
                    if isinstance(role, str) and isinstance(value, int) and value >= 0
                }
                if isinstance(max_value, dict)
                else {}
            )
            formatting_value = record.get("formattingObjects") if isinstance(record, dict) else None
            formatting_properties = (
                [value for value in formatting_value if isinstance(value, str)]
                if isinstance(formatting_value, list)
                else []
            )
            offline = raw_visual_type not in _ONLINE_OR_EXTERNAL
            capabilities.append(
                VisualTypeCapability(
                    visual_type=raw_visual_type,
                    display_name=_display_name(raw_visual_type),
                    roles=roles,
                    role_kinds=role_kinds,
                    required_roles=required_roles,
                    max_per_role=max_per_role,
                    formatting_properties=formatting_properties,
                    offline_capable=offline,
                    validation_status="catalogued" if offline else "unsupported",
                    source="microsoft_catalog",
                )
            )
        return tuple(capabilities)


def _run_json(argv: tuple[str, ...]) -> dict[str, object]:
    try:
        result = subprocess.run(  # noqa: S603 - pinned tool argv; shell=False
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        value = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _display_name(value: str) -> str:
    result = value[0].upper() if value else value
    for character in value[1:]:
        result += " " + character if character.isupper() else character
    return result
