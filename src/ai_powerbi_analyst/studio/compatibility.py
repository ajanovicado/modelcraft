"""Fail-closed, metadata-only compatibility inspection of the selected PBIP model.

This is a bounded structural inspection, not a TMDL deserializer or source evaluator.
Report connection references and model storage modes are different contracts. Neither
source expressions nor the presence of cached rows establish a model's storage mode.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Literal

from ai_powerbi_analyst.powerbi.transaction import PromotionPolicyViolation, hash_tree

from .contracts import ProjectCompatibilityReport

StorageMode = Literal["import", "direct_query", "dual", "hybrid", "direct_lake", "unknown"]
ConnectionMode = Literal[
    "import", "direct_query", "dual", "hybrid", "direct_lake", "live", "composite", "unknown"
]
_MODES: dict[str, StorageMode] = {
    "import": "import",
    "directquery": "direct_query",
    "dual": "dual",
    "directlake": "direct_lake",
}
_MODE_LABELS = {
    "direct_query": "DirectQuery",
    "dual": "Dual storage",
    "hybrid": "hybrid table",
    "direct_lake": "Direct Lake",
    "unknown": "Unknown semantic model storage mode",
}
_IDENTIFIER = r"(?:'(?:[^']|'')*'|[^\s:=]+)"
_OBJECT = re.compile(
    rf"^(?P<ref>ref\s+)?(?P<kind>[A-Za-z]+)(?:\s+(?P<name>{_IDENTIFIER}))?"
    r"(?:\s*=\s*(?P<value>.*))?$",
    re.IGNORECASE,
)
_PROPERTY = re.compile(r"^([A-Za-z]+)\s*:\s*(.*?)\s*$")
_MAX_COMPONENT_BYTES = 8 * 1024 * 1024
_MAX_MODEL_BYTES = 64 * 1024 * 1024
_MAX_MODEL_FILES = 2000
# Documented M library functions; other Contents exports need connector validation.
# This is deliberately not a claim that all installed Power BI connectors are known.
_BUILTIN_CONTENTS = {
    "cdm.contents",
    "exchange.contents",
    "file.contents",
    "folder.contents",
    "hdfs.contents",
    "hdinsight.contents",
    "sharepoint.contents",
    "web.contents",
}


@dataclass
class _Partition:
    modes: list[str] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)
    source_kinds: list[str] = field(default_factory=list)


@dataclass
class _Model:
    names: set[str] = field(default_factory=set)
    defaults: list[str] = field(default_factory=list)
    tables: dict[str, dict[str, _Partition]] = field(default_factory=dict)
    features: set[str] = field(default_factory=set)
    malformed: bool = False


def inspect_pbip(path: str | Path) -> ProjectCompatibilityReport:
    pbip = Path(path).expanduser().resolve(strict=True)
    if not pbip.is_file() or pbip.suffix.casefold() != ".pbip":
        raise ValueError("Select a Power BI Project (.pbip), not a PBIX file")
    root = pbip.parent
    blocked: list[str] = []
    notes: list[str] = []
    mode: ConnectionMode = "unknown"
    storage_modes: list[StorageMode] = []
    project_hash: str | None = None
    try:
        # Also rejects symlinks/junctions before any metadata is followed or read.
        project_hash = hash_tree(root)
        project = _json_object(pbip, "PBIP entry point")
        artifacts = project.get("artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != 1:
            raise ValueError("PBIP must identify exactly one report artifact")
        artifact = artifacts[0]
        report_value = artifact.get("report") if isinstance(artifact, dict) else None
        relative = report_value.get("path") if isinstance(report_value, dict) else None
        report = _inside_directory(root, relative, root, ".report")
        definition = _json_object(report / "definition.pbir", "PBIR report definition")
        reference = definition.get("datasetReference")
        if not isinstance(reference, dict):
            raise ValueError("PBIR semantic model reference is missing")
        if ("byConnection" in reference) == ("byPath" in reference):
            raise ValueError("PBIR must identify exactly one semantic model reference")
        if "byConnection" in reference:
            connection = reference["byConnection"]
            connection_string = (
                connection.get("connectionString") if isinstance(connection, dict) else None
            )
            if not isinstance(connection_string, str) or not connection_string.strip():
                raise ValueError("PBIR remote semantic model reference is invalid")
            mode = "live"
            blocked.append("live connection")
            notes.append(
                "The report references a remote semantic model; its storage modes are not "
                "established by this report connection reference"
            )
        else:
            by_path = reference["byPath"]
            relative_model = by_path.get("path") if isinstance(by_path, dict) else None
            semantic = _inside_directory(report, relative_model, root, ".semanticmodel")
            model = _inspect_model(semantic / "definition", root)
            mode, storage_modes = _classify(model)
            blocked.extend(sorted(model.features))
            if mode == "composite":
                blocked.append("composite model")
            blocked.extend(_MODE_LABELS[item] for item in storage_modes if item != "import")
            if model.malformed:
                blocked.append("Ambiguous or malformed TMDL storage metadata")
            notes.append(
                "Storage metadata does not establish connector availability, credentials "
                "or source accessibility; unverified Contents exports remain blocked"
            )
            if "direct_query" in storage_modes:
                notes.append(
                    "DirectQuery source groups are compared only when explicit data-source "
                    "references are available; source expressions are not evaluated"
                )
        if not blocked:
            notes.append("Thick local import project structure is available for staged migration")
        else:
            notes.append(
                "Inspection does not change storage modes or authorize changes to a remote "
                "semantic model; unsupported projects remain outside managed migration"
            )
    except (OSError, UnicodeDecodeError, ValueError, PromotionPolicyViolation) as error:
        blocked.append(str(error))
        storage_modes = ["unknown"]
        mode = "unknown"
    return ProjectCompatibilityReport(
        pbip_path=str(pbip),
        supported=not blocked,
        project_hash=project_hash,
        connection_mode=mode,
        storage_modes=storage_modes,
        blocked_features=sorted(set(blocked)),
        notes=notes,
    )


def _json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(_text(path))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"{label} is missing, unreadable or invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _text(path: Path) -> str:
    if path.stat().st_size > _MAX_COMPONENT_BYTES:
        raise ValueError("Project metadata exceeds the inspection size limit")
    return path.read_text(encoding="utf-8-sig")


def _inside_directory(base: Path, relative: object, root: Path, suffix: str) -> Path:
    if (
        not isinstance(relative, str)
        or not relative.strip()
        or Path(relative).is_absolute()
        or PureWindowsPath(relative).drive
    ):
        raise ValueError("Project component path must be relative and remain inside the PBIP root")
    candidate = (base / relative.replace("\\", "/")).resolve(strict=True)
    if (
        not candidate.is_relative_to(root)
        or not candidate.is_dir()
        or candidate.suffix.casefold() != suffix
    ):
        raise ValueError("Project component path must remain inside the PBIP root")
    return candidate


def _inspect_model(definition: Path, root: Path) -> _Model:
    if not (definition / "model.tmdl").is_file():
        raise ValueError("TMDL semantic model definition is missing")
    files = sorted(definition.rglob("*.tmdl"))
    if (
        len(files) > _MAX_MODEL_FILES
        or sum(item.stat().st_size for item in files) > _MAX_MODEL_BYTES
    ):
        raise ValueError("Semantic model metadata exceeds the inspection size limit")
    model = _Model()
    for item in files:
        if not item.resolve(strict=True).is_relative_to(root):
            raise ValueError("Semantic model definition escapes the PBIP root")
        _scan_tmdl(_text(item), model)
    return model


def _name(value: str | None) -> str:
    if value is None:
        return ""
    if value.startswith("'") and value.endswith("'"):
        value = value[1:-1].replace("''", "'")
    return value.casefold()


def _scan_tmdl(text: str, model: _Model) -> None:
    """Inspect only declared model/partition properties, skipping expression bodies.

    A full TOM deserializer remains necessary for complete model validation. Unknown
    or conflicting mode properties cannot qualify a model for managed migration.
    """
    stack: list[tuple[int, str, str]] = []
    expression_indent: int | None = None
    fenced = False
    m_expression = False
    expressions: list[str] = []
    for raw in text.splitlines():
        content = raw.lstrip(" \t")
        indent = len(raw.expandtabs(4)) - len(content)
        if fenced:
            if content.strip() == "```":
                fenced = False
            elif m_expression:
                expressions.append(content)
            continue
        if not content or content.startswith("//"):
            continue
        if expression_indent is not None:
            if indent > expression_indent:
                if m_expression:
                    expressions.append(content)
                continue
            expression_indent = None
        while stack and stack[-1][0] >= indent:
            stack.pop()
        prop = _PROPERTY.fullmatch(content)
        if prop:
            kind, value = prop.group(1).casefold(), prop.group(2).casefold()
            if stack and stack[-1][1] == "model" and kind == "defaultmode":
                model.defaults.append(value)
            elif stack and stack[-1][1] == "partition" and kind in {"mode", "datasource"}:
                table = next((name for _, owner, name in reversed(stack) if owner == "table"), None)
                if table is None:
                    model.malformed = True
                    continue
                partition = model.tables[table].get(stack[-1][2])
                if partition is None:
                    model.malformed = True
                    continue
                if kind == "mode":
                    partition.modes.append(value)
                else:
                    partition.sources.add(_name(value))
            elif kind in {"mode", "defaultmode"}:
                model.malformed = True
            continue
        declaration = _OBJECT.fullmatch(content)
        if declaration:
            kind = declaration.group("kind").casefold()
            name = _name(declaration.group("name"))
            value = declaration.group("value")
            if declaration.group("ref"):
                # Ordering refs to missing objects are ignored by TOM. A ref table
                # with child declarations can also be used for a partial definition.
                if kind in {"model", "table"}:
                    stack.append((indent, kind, name))
                continue
            if kind == "model" and name:
                model.names.add(name)
            elif kind == "table" and name:
                model.tables.setdefault(name, {})
            elif kind == "partition":
                if not stack or stack[-1][1] != "table" or not name:
                    model.malformed = True
                else:
                    partitions = model.tables.setdefault(stack[-1][2], {})
                    partition = partitions.setdefault(name, _Partition())
                    if value is not None:
                        partition.source_kinds.append(value.casefold())
            elif kind in {"role", "tablepermission"}:
                model.features.add("row-level security")
            elif kind == "calculationgroup":
                model.features.add("calculation group")
            stack.append((indent, kind, name))
            if value is not None and kind not in {"partition", "datasource"}:
                m_expression = kind == "expression" or (
                    kind == "source" and any(owner == "partition" for _, owner, _ in stack)
                )
                if m_expression:
                    expressions.append(value)
                fenced = value.strip() == "```"
                if not value.strip():
                    expression_indent = indent
            continue
        # Expression properties (source/query/etc.) are opaque, even when their
        # text happens to contain a TMDL-looking "mode: directQuery" line.
        expression = re.fullmatch(r"[A-Za-z]+\s*=\s*(.*)", content)
        if expression:
            value = expression.group(1)
            m_expression = False
            fenced = value.strip() == "```"
            if not value.strip():
                expression_indent = indent
    if fenced:
        model.malformed = True
    if _custom_connector("\n".join(expressions)):
        model.features.add("custom connector")


def _custom_connector(expressions: str) -> bool:
    # Do not treat comments, strings, descriptions or data values as connector calls.
    def mask(match: re.Match[str]) -> str:
        token = match.group()
        if not token.startswith('#"'):
            return " "
        identifier = token[2:-1].replace('""', '"')
        if identifier.casefold().endswith(".contents"):
            return (
                identifier if identifier.casefold() in _BUILTIN_CONTENTS else "Unverified.Contents"
            )
        return " "

    code = re.sub(r'#?"(?:[^"]|"")*"|/\*.*?\*/|//[^\n]*', mask, expressions, flags=re.S)
    if re.search(r"\bCustomConnector\s*\(", code, re.I):
        return True
    return any(
        name.casefold() not in _BUILTIN_CONTENTS
        for name in re.findall(r"\b([^\W\d]\w*(?:\.[^\W\d]\w*)*\.Contents)\s*\(", code, re.I)
    )


def _classify(model: _Model) -> tuple[ConnectionMode, list[StorageMode]]:
    if (
        len(model.names) != 1
        or len(model.defaults) > 1
        or any(value not in _MODES for value in model.defaults)
    ):
        model.malformed = True
    default = model.defaults[0] if model.defaults else "import"
    tables: list[StorageMode] = []
    direct_sources: set[str] = set()
    for partitions in model.tables.values():
        modes: set[StorageMode] = set()
        if not partitions:
            modes.add("unknown")
        for partition in partitions.values():
            if len(partition.source_kinds) != 1 or partition.source_kinds[0] not in {
                "m",
                "query",
                "calculated",
                "entity",
                "none",
            }:
                model.malformed = True
            if len(partition.modes) != 1:
                model.malformed = True
                modes.add("unknown")
                continue
            value = partition.modes[0]
            if value == "default":
                value = default
            resolved = _MODES.get(value, "unknown")
            modes.add(resolved)
            if resolved == "unknown" or len(partition.sources) > 1:
                model.malformed = True
            if resolved == "direct_query":
                direct_sources.update(partition.sources)
        if modes == {"import", "direct_query"}:
            tables.append("hybrid")
        elif len(modes) == 1:
            tables.append(next(iter(modes)))
        else:
            tables.append("unknown")
            model.malformed = True
    storage_modes: list[StorageMode] = sorted(set(tables)) if tables else ["unknown"]
    if model.malformed or "unknown" in storage_modes:
        return "unknown", sorted(set([*storage_modes, "unknown"]))
    if len(storage_modes) > 1 or len(direct_sources) > 1:
        return "composite", storage_modes
    return storage_modes[0], storage_modes
