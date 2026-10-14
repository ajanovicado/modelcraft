"""Discover integrity-checked installed Power BI tools without a global PATH."""

from __future__ import annotations

import hashlib
import json
import stat
from functools import lru_cache
from pathlib import Path
from typing import Any

from .paths import app_root

TOOL_VERSIONS = {
    "pbi": "3.11.1",
    "powerbi-report-author": "0.1.4",
    "powerbi-desktop": "0.1.2",
}


def _payload_path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Bundled tool path escapes its installation")
    if not path.is_file():
        raise ValueError("A bundled Power BI tool is missing; reinstall Modelcraft")
    for entry in (path, *path.parents):
        info = entry.lstat()
        if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
            raise ValueError("Bundled tool path contains a redirected file or directory")
        if entry == root:
            break
    return path


@lru_cache(maxsize=4096)
def _digest(path: Path, size: int, modified: int, created: int) -> str:
    # Metadata participates in the cache key. Runtime/profile verification remains
    # independent; this prevents hashing the whole toolchain on each health poll.
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installed_tool(name: str) -> dict[str, Any]:
    """Fail closed for missing, tampered or version-mismatched installed tooling."""
    if name not in TOOL_VERSIONS:
        raise ValueError("Unknown bundled Power BI tool")
    root = app_root() / "runtime" / "tools"
    manifest_path = _payload_path(root, "toolchain.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported bundled Power BI tool manifest")
    entry = manifest.get("tools", {}).get(name)
    if not isinstance(entry, dict) or entry.get("version") != TOOL_VERSIONS[name]:
        raise ValueError("Bundled Power BI tool version does not match this Modelcraft release")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Bundled Power BI tools have no integrity manifest")
    executable_name = entry.get("executable")
    if not isinstance(executable_name, str) or executable_name not in files:
        raise ValueError("Bundled Power BI executable is not pinned")
    for relative, expected in files.items():
        if not isinstance(relative, str) or not isinstance(expected, str) or len(expected) != 64:
            raise ValueError("Invalid bundled Power BI tool hash")
        path = _payload_path(root, relative)
        identity = path.stat()
        if _digest(path, identity.st_size, identity.st_mtime_ns, identity.st_ctime_ns) != expected:
            raise ValueError("A bundled Power BI tool is damaged; reinstall Modelcraft")
    if name == "pbi" and entry.get("distribution_version") != TOOL_VERSIONS[name]:
        raise ValueError("Bundled pbi-cli distribution version does not match this release")
    return {**entry, "path": str(_payload_path(root, executable_name))}
