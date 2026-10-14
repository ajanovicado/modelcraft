from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai_powerbi_analyst import bundled_tools, preflight


def _installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "runtime/tools"
    executable = root / "bin/pbi.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"compiled pbi fixture")
    manifest = {
        "schema_version": 1,
        "tools": {
            "pbi": {
                "executable": "bin/pbi.exe",
                "version": "3.11.1",
                "distribution_version": "3.11.1",
            }
        },
        "files": {"bin/pbi.exe": hashlib.sha256(executable.read_bytes()).hexdigest()},
    }
    (root / "toolchain.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(bundled_tools, "app_root", lambda: tmp_path)
    return root


def test_installed_tool_does_not_need_global_python_node_or_path(tmp_path, monkeypatch):
    root = _installation(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
    monkeypatch.setattr(preflight, "_uv_tool_distribution_version", lambda _: None)
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="pbi-cli, version 3.10.10\n", stderr=""
        ),
    )
    tool = preflight.probe_pbi_cli()
    assert tool.available and tool.path == str(root / "bin/pbi.exe")
    assert tool.distribution_version == "3.11.1"


def test_tampered_tool_is_rejected_after_an_earlier_success(tmp_path, monkeypatch):
    root = _installation(tmp_path, monkeypatch)
    bundled_tools.installed_tool("pbi")
    (root / "bin/pbi.exe").write_bytes(b"changed fixture with a new size")
    with pytest.raises(ValueError, match="damaged"):
        bundled_tools.installed_tool("pbi")


@pytest.mark.parametrize("relative", ["../../../outside.exe", "bin/missing.exe"])
def test_manifest_cannot_escape_or_name_a_missing_file(tmp_path, monkeypatch, relative):
    root = _installation(tmp_path, monkeypatch)
    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"untrusted")
    path = root / "toolchain.json"
    manifest = json.loads(path.read_text())
    manifest["tools"]["pbi"]["executable"] = relative
    manifest["files"] = {relative: hashlib.sha256(outside.read_bytes()).hexdigest()}
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        bundled_tools.installed_tool("pbi")


def test_installed_build_never_falls_back_to_a_global_unqualified_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(bundled_tools, "app_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(preflight.shutil, "which", lambda _: "some-global-pbi.exe")
    assert not preflight.probe_pbi_cli().available


def test_packaged_tool_distribution_must_match_the_release(tmp_path, monkeypatch):
    root = _installation(tmp_path, monkeypatch)
    path = root / "toolchain.json"
    manifest = json.loads(path.read_text())
    manifest["tools"]["pbi"]["distribution_version"] = "3.12.0"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="distribution version"):
        bundled_tools.installed_tool("pbi")
