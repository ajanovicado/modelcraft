"""Locate packaged and developer runtime files without machine-absolute pins.

Qualified profile files may carry either absolute paths (developer seals) or
paths relative to the runtime root (packaged builds). The runtime root is the
profile configuration file's grandparent directory, matching the anchor the
guarded session already uses for its training/lock directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

RUNTIME_ROOT_ENV = "AI_POWERBI_RUNTIME_ROOT"
MODEL_ROOT_ENV = "AI_POWERBI_MODEL_ROOT"

_PROFILE_PATH_FIELDS = (
    "server",
    "model",
    "model_store",
    "corpus",
    "artifact_examples",
    "m_validator",
    "node",
)


def app_root() -> Path:
    """Directory that anchors runtime_config_path: install dir or repository."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resolve_runtime_config(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    override = os.environ.get(RUNTIME_ROOT_ENV)
    base = Path(override).expanduser() if override else app_root()
    return (base / path).resolve()


def runtime_guard_root(config_path: Path) -> Path:
    """Keep installed runtime locks out of the read-only application directory."""
    if not getattr(sys, "frozen", False):
        return config_path.parent.parent / "training"
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    root = local / "AI Power BI Analyst" / "runtime-guard"
    root.mkdir(parents=True, exist_ok=True)
    return root


def user_model_root() -> Path:
    """Per-user, writable store for the model installed on first launch."""
    override = os.environ.get(MODEL_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local / "AI Power BI Analyst" / "models"


def normalize_profile_config(cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """Resolve relative profile paths against the runtime root.

    Absolute entries pass through unchanged so existing developer seals keep
    working byte-for-byte. Packaged profiles additionally alias the embedded
    worked-example/M-validator paths to their payload pins and prefer the
    bundled node executable for M validation. A profile with
    ``model_location: user`` keeps its model (and that model's pin) in the
    per-user store instead: installers cannot carry the 3.4 GB model file
    (MSI and NSIS stop at 2 GB), so it is installed there on first launch.
    """
    root = config_path.parent.parent
    model_root = user_model_root() if cfg.get("model_location") == "user" else root
    out = dict(cfg)
    for key in _PROFILE_PATH_FIELDS:
        value = out.get(key)
        if isinstance(value, str) and value:
            base = model_root if key in ("model", "model_store") else root
            path = Path(value)
            out[key] = str(path if path.is_absolute() else (base / path).resolve())
    hashes = out.get("hashes")
    if isinstance(hashes, dict):
        resolved = {}
        for name, digest in hashes.items():
            path = Path(name)
            base = model_root if name == cfg.get("model") else root
            resolved[str(path if path.is_absolute() else (base / path).resolve())] = digest
        for key, filename in (
            ("artifact_examples", "artifact_examples.json"),
            ("m_validator", "m_validation.cjs"),
        ):
            pinned = out.get(key)
            if pinned and pinned in resolved:
                from .llm import artifact_policy

                resolved[str(artifact_policy.ROOT / filename)] = resolved[pinned]
        out["hashes"] = resolved
    node = out.get("node")
    if node:
        # artifact_policy resolves node via shutil.which; put the pinned copy
        # first so the executable used is the one the seal covers.
        bindir = str(Path(node).parent)
        entries = os.environ.get("PATH", "").split(os.pathsep)
        if bindir not in entries:
            os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
    return out
