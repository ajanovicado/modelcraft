"""Fail-closed checks used by the one-click Windows launcher."""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from ai_powerbi_analyst.preflight import EnvironmentReport, collect_environment, evaluate_readiness


@dataclass(frozen=True, slots=True)
class LaunchGate:
    ready: bool
    mode: str
    blockers: tuple[str, ...]


def evaluate_launch_gate(
    environment: EnvironmentReport,
    *,
    required_model: str,
    installed_models: set[str],
    real_mode: bool,
    selected_digest: str | None = None,
    installed_digests: Mapping[str, str] | None = None,
    required_desktop_version: str | None = None,
) -> LaunchGate:
    version_parts = environment.python_version.split(".")
    try:
        python_version = (int(version_parts[0]), int(version_parts[1]))
    except (IndexError, ValueError):
        python_version = (0, 0)
    blockers: list[str] = []
    if not (3, 12) <= python_version < (3, 14):
        blockers.append("Python 3.12 or 3.13 is required")

    if real_mode:
        readiness = evaluate_readiness(environment)
        if not readiness.can_write_real_powerbi or not readiness.can_run_demo:
            blockers.extend(readiness.blockers)
        if required_model not in installed_models:
            blockers.append(
                f"Required local model {required_model!r} is not installed; "
                "prepare it before going offline"
            )
        elif selected_digest is not None:
            actual_digest = (installed_digests or {}).get(required_model)
            if actual_digest != selected_digest:
                blockers.append(
                    f"Local model digest mismatch for {required_model!r}; "
                    f"expected {selected_digest}"
                )
        desktop = environment.tools.get("powerbi-desktop")
        if required_desktop_version is not None:
            discovered_versions = set(
                re.findall(
                    r"(?<!\d)(\d+\.\d+\.\d+(?:\.\d+)?)(?!\d)",
                    desktop.version if desktop and desktop.version else "",
                )
            )
            if (
                desktop is None
                or not desktop.available
                or required_desktop_version not in discovered_versions
            ):
                blockers.append(f"Power BI Desktop version {required_desktop_version} is required")
    return LaunchGate(
        ready=not blockers,
        mode="real" if real_mode else "mock",
        blockers=tuple(dict.fromkeys(blockers)),
    )


def installed_ollama_inventory(
    endpoint: str = "http://127.0.0.1:11434",
) -> dict[str, str]:
    """Read full model digests from Ollama's loopback API.

    ``ollama list`` exposes a shortened display ID on common versions, which
    cannot prove the attested full digest and is therefore never trusted here.
    """

    if endpoint not in {"http://127.0.0.1:11434", "http://localhost:11434"}:
        return {}
    try:
        request = urllib.request.Request(  # noqa: S310 - endpoint allowlisted to loopback Ollama
            f"{endpoint}/api/tags",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310
            raw = response.read(2 * 1024 * 1024 + 1)
    except (OSError, TimeoutError, urllib.error.URLError):
        return {}
    if len(raw) > 2 * 1024 * 1024:
        return {}
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or len(entries) > 1_000:
        return {}
    models: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model")
        digest = entry.get("digest")
        if not isinstance(name, str) or not name or len(name) > 256:
            continue
        models[name] = (
            digest
            if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            else ""
        )
    return models


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("real", "mock"), default="real")
    parser.add_argument("--model", default="qwen3.5:4b-q4_K_M")
    parser.add_argument("--digest")
    parser.add_argument("--desktop-version")
    args = parser.parse_args()
    environment = collect_environment()
    inventory = installed_ollama_inventory() if args.mode == "real" else {}
    gate = evaluate_launch_gate(
        environment,
        required_model=args.model,
        installed_models=set(inventory),
        real_mode=args.mode == "real",
        selected_digest=args.digest,
        installed_digests=inventory,
        required_desktop_version=args.desktop_version,
    )
    print(json.dumps(asdict(gate), indent=2))
    return 0 if gate.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
