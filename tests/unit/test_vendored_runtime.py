"""A clean install must use exactly the reviewed runtime, without a sibling checkout."""

from __future__ import annotations

import hashlib
import json
import tomllib
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor" / "powerbi-rag-runtime"
PACKAGE_FILES = {
    "powerbi_rag_runtime/__init__.py",
    "powerbi_rag_runtime/backend.pyi",
    "powerbi_rag_runtime/guards.py",
    "powerbi_rag_runtime/guards.pyi",
    "powerbi_rag_runtime/py.typed",
    "powerbi_rag_runtime/server.py",
    "powerbi_rag_runtime/windows_job.py",
    "powerbi_rag_runtime/windows_paging.py",
}


def test_vendor_snapshot_matches_reviewed_bytes_and_contains_only_runtime_files() -> None:
    manifest = json.loads((VENDOR / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["package"] == "powerbi-rag-runtime"
    assert manifest["version"] == "0.1.0"
    assert set(manifest["files"]) == PACKAGE_FILES | {"pyproject.toml"}
    actual = {
        path.relative_to(VENDOR).as_posix()
        for path in (VENDOR / "powerbi_rag_runtime").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert actual == PACKAGE_FILES
    for name, identity in manifest["files"].items():
        data = (VENDOR / name).read_bytes()
        assert len(data) == identity["size_bytes"], name
        assert hashlib.sha256(data).hexdigest() == identity["sha256"], name
    digest = hashlib.sha256(
        json.dumps(manifest["files"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert digest == manifest["snapshot_sha256"]


def test_frozen_dependency_is_pinned_inside_checkout_and_is_not_editable() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "powerbi-rag-runtime==0.1.0" in project["project"]["dependencies"]
    assert project["tool"]["uv"]["sources"]["powerbi-rag-runtime"] == {
        "path": "vendor/powerbi-rag-runtime"
    }
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    runtime = next(p for p in lock["package"] if p["name"] == "powerbi-rag-runtime")
    assert runtime["version"] == "0.1.0"
    assert runtime["source"] == {"directory": "vendor/powerbi-rag-runtime"}


def test_installed_package_is_noneditable_and_contains_reviewed_runtime_bytes() -> None:
    import powerbi_rag_runtime

    distribution = metadata.distribution("powerbi-rag-runtime")
    assert distribution.version == "0.1.0"
    direct_url_text = distribution.read_text("direct_url.json")
    assert direct_url_text is not None
    direct_url = json.loads(direct_url_text)
    assert not direct_url.get("dir_info", {}).get("editable", False)
    source_url = urlparse(direct_url["url"])
    assert source_url.scheme == "file"
    assert (
        unquote(source_url.path)
        .replace("\\", "/")
        .rstrip("/")
        .endswith("/vendor/powerbi-rag-runtime")
    )
    installed = Path(powerbi_rag_runtime.__file__).resolve().parent
    assert installed != (VENDOR / "powerbi_rag_runtime").resolve()
    assert installed == Path(distribution.locate_file("powerbi_rag_runtime")).resolve()
    packaged_files = {
        path.as_posix()
        for path in distribution.files or ()
        if path.parts[0] == "powerbi_rag_runtime" and "__pycache__" not in path.parts
    }
    assert packaged_files == PACKAGE_FILES
    manifest = json.loads((VENDOR / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
    for name in PACKAGE_FILES:
        data = (installed / Path(name).name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == manifest["files"][name]["sha256"], name


def test_runtime_guard_and_server_work_without_a_model_process(tmp_path: Path) -> None:
    from powerbi_rag_runtime.guards import GIB, enforce, exclusive_lock
    from powerbi_rag_runtime.server import GuardedServer, sha

    sample = {
        "available": 9 * GIB,
        "swap_used": 0,
        "native_swapouts": 0,
        "ac": True,
        "disk_free": 9 * GIB,
    }
    assert enforce(sample, startup=True) == sample
    with pytest.raises(RuntimeError, match="Resource guard"):
        enforce({**sample, "ac": False}, startup=True)
    with pytest.raises(RuntimeError, match="paging"):
        enforce({**sample, "native_swapouts": 513 * 1024**2}, sample)
    with exclusive_lock(tmp_path / "runtime.lock"):
        assert (tmp_path / "runtime.lock").is_file()
    payload = tmp_path / "payload.txt"
    payload.write_bytes(b"reviewed-runtime-smoke-test")
    assert sha(payload) == hashlib.sha256(payload.read_bytes()).hexdigest()
    server = GuardedServer({}, lambda: sample, tmp_path)
    assert server.process is None
    server.stop()
    assert server.stopped.is_set()


def test_runtime_ownership_and_mit_license_are_recorded() -> None:
    manifest = json.loads((VENDOR / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["source_kind"] == "local_directory_snapshot"
    assert manifest["revision"] is None
    assert manifest["repository_url"] is None
    assert manifest["license_status"] == "MIT_owner_confirmed"
    assert manifest["owner_confirmation"]["date"] == "2026-10-14"
    notice = (VENDOR / manifest["owner_confirmation"]["license_notice"]).read_text(encoding="utf-8")
    assert notice == (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Permission is hereby granted, free of charge" in notice
    assert (VENDOR / manifest["owner_confirmation"]["evidence"]).is_file()
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "explicitly licensed the runtime under MIT" in notices
