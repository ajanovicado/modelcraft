"""Snapshot Microsoft's PBIR visual catalog for Studio's report editor (offline use).

    .venv/Scripts/python.exe scripts/snapshot_formatting_catalog.py

Runs the pinned powerbi-report-author CLI (0.1.4) once per visual type and writes
src/ai_powerbi_analyst/studio/pbir_catalog.json. The file holds each type's data roles
(kind, max per role, required) in the CLI's authoring order, its formatting objects with
every property's type, enum values and, for the "formatting" type, its subtype, and the
shared visual-container objects stored once. Studio reads existing report formatting and
builds the retained Visual pane controls from it without the Node tool at runtime.
The snapshot preserves styling metadata for rendering; it does not grant styling edits.
Re-run after upgrading the CLI.

Two things the CLI hides and this script restores:
- Subtypes. capabilities.json says {"formatting": {"fontSize": true}}, but the CLI
  (0.1.4) tests `typeof t.formatting === "string"`, never true for that shape, so every
  fontSize, fontFamily, alignment, altText, dashArray, labelDisplayUnits and formatString
  comes out as a bare "formatting". The number-or-text choice of the value literal
  depends on it (fontSize is 34D, fontFamily is text), so the subtype is read from the
  schema package next to the CLI and stored as `subtype`.
- Order. Roles are kept in the order the CLI lists them (Category, Series, Y, ...), which
  is the order Power BI's field wells use. The old snapshot sorted every key, so KPI's
  roles came out as Goal, Indicator, TrendLine and Studio filled the wrong well first.

It also stores `instanceSelectors` ({visual type: {object: [states]}}) from the schema
package's instance-selectors.json: the objects that only take effect under a state
selector such as {"id": "default"}, with the states each one has.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from ai_powerbi_analyst.preflight import probe_tool  # noqa: E402

OUT = REPO / "src" / "ai_powerbi_analyst" / "studio" / "pbir_catalog.json"
_CLI_PACKAGE = Path("node_modules/@microsoft/powerbi-report-authoring-cli")
_SCHEMA_PACKAGE = Path("node_modules/@microsoft/powerbi-core-visual-schema/data")


def _node_directory() -> Path:
    tool = probe_tool("powerbi-report-author")
    if not tool.available or not tool.path:
        raise SystemExit("powerbi-report-author is not installed")
    return Path(tool.path).parent


def schema_data(name: str) -> dict[str, Any]:
    """A data file of the schema package the CLI reads (nested under the CLI, or hoisted)."""
    directory = _node_directory()
    for base in (directory / _CLI_PACKAGE, directory):
        path = base / _SCHEMA_PACKAGE / name
        if path.exists():
            loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return loaded
    raise SystemExit(f"powerbi-core-visual-schema {name} not found next to the CLI")


def formatting_subtype(raw: dict[str, Any]) -> str | None:
    """fontSize, fontFamily, ... for a raw capabilities property of type 'formatting'."""
    kind = (raw.get("type") or {}).get("formatting")
    if isinstance(kind, str):
        return kind
    if isinstance(kind, dict):
        names = [name for name, on in kind.items() if on]
        if len(names) > 1:
            raise SystemExit(f"formatting property with several subtypes: {sorted(names)}")
        return names[0] if names else None
    return None


def add_subtypes(properties: dict[str, Any], raw_properties: dict[str, Any]) -> dict[str, Any]:
    """The CLI's property specs, with `subtype` on every 'formatting' property."""
    result: dict[str, Any] = {}
    for name, spec in properties.items():
        spec = dict(spec)
        if spec.get("type") == "formatting" and "subtype" not in spec:
            subtype = formatting_subtype(raw_properties.get(name) or {})
            if subtype:
                spec["subtype"] = subtype
        result[name] = spec
    return result


def cli(*args: str) -> dict[str, Any]:
    directory = _node_directory()
    argv = [
        str(directory / "node.exe"),
        str(directory / "node_modules/@microsoft/powerbi-report-authoring-cli/dist/cli.js"),
        *args,
    ]
    result = subprocess.run(  # noqa: S603 - pinned local tool, argv only
        argv, capture_output=True, text=True, encoding="utf-8", timeout=60, check=False
    )
    payload = json.loads(result.stdout)
    if "error" in payload:
        raise RuntimeError(f"{args}: {payload['error']}")
    data: dict[str, Any] = payload["data"]
    return data


def main() -> None:
    listing = cli("catalog", "list")
    deprecated = {d["visualType"] for d in listing.get("deprecated", [])}
    capabilities = schema_data("capabilities.json")
    vco_capabilities = schema_data("vco-capabilities.json")
    visuals: dict[str, dict[str, Any]] = {}
    shared: dict[str, dict[str, Any]] = {}
    for visual_type in listing["visualTypes"]:
        entry = cli("catalog", "describe", visual_type)
        props = cli("formatting", "effective-properties", visual_type)
        raw_objects = capabilities.get(entry.get("capabilityKey", visual_type), {}).get(
            "objects", {}
        )
        for name, properties in props.get("visualContainerObjects", {}).items():
            typed = add_subtypes(properties, vco_capabilities.get(name, {}).get("properties", {}))
            previous = shared.get(name)
            if previous is not None and previous != typed:
                # Keep the union: a container object's properties do not vary by type.
                previous.update(typed)
            else:
                shared[name] = dict(typed)
        # No sorting anywhere below: dict order is the CLI's authoring order, and
        # `roleOrder` repeats it as a list so a reader cannot lose it.
        roles = {
            name: {
                "kind": role.get("kind"),
                "displayName": role.get("displayName", name),
            }
            for name, role in entry.get("roles", {}).items()
        }
        visuals[visual_type] = {
            "roles": roles,
            "roleOrder": list(roles),
            "requiredRoles": entry.get("requiredRoles", []),
            "optionalRoles": entry.get("optionalRoles", []),
            "maxPerRole": entry.get("maxPerRole", {}),
            "objects": {
                object_name: add_subtypes(
                    properties, raw_objects.get(object_name, {}).get("properties", {})
                )
                for object_name, properties in props.get("visualObjects", {}).items()
            },
            "containerObjects": sorted(props.get("visualContainerObjects", {})),
            "deprecated": visual_type in deprecated or bool(entry.get("deprecated")),
        }
    # Which objects of a visual type only take effect under a state selector, and the states
    # (instance-selectors.json in the schema package; `_metadata` is bookkeeping). A `shape`
    # fill written without {"id": "default"} drew in the default fill in Desktop.
    instance_selectors = {
        visual_type: states
        for visual_type, states in schema_data("instance-selectors.json").items()
        if not visual_type.startswith("_") and isinstance(states, dict)
    }
    payload = {
        "source": "powerbi-report-author 0.1.4",
        "visualTypes": visuals,
        "containerObjects": shared,
        "instanceSelectors": instance_selectors,
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, {len(visuals)} visual types)")


if __name__ == "__main__":
    main()
