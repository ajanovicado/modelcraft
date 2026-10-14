"""Public exports copy reviewed source into fresh directories, never working state."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest


def _exporter():
    path = Path(__file__).resolve().parents[2] / "scripts/prepare-public-repo.py"
    spec = importlib.util.spec_from_file_location("public_export", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(tmp_path):
    root = tmp_path / "source"
    (root / "src").mkdir(parents=True)
    (root / "src/main.py").write_text("print('source fixture')\n")
    (root / "src/ai_powerbi_analyst/runtime").mkdir(parents=True)
    (root / "src/ai_powerbi_analyst/runtime/__init__.py").write_text("# Application code\n")
    (root / "vendor/powerbi-rag-runtime").mkdir(parents=True)
    (root / "vendor/powerbi-rag-runtime/LICENSE").write_text("MIT License\n")
    (root / "README.md").write_text("# Example project\n")
    (root / ".gitattributes").write_text("* text=auto\n")
    return root


def test_export_omits_history_secrets_runtime_payload_and_unknown_data(tmp_path):
    module = _exporter()
    root = _source(tmp_path)
    hidden = {
        ".git/config": "original history",
        ".env": "private environment",
        "evidence/user-data.json": "private report",
        ".runtime/user-state.json": "saved conversation",
        "runtime/profiles/standard.json": "private runtime seal",
        "apps/desktop/src-tauri/binaries/engine.build.json": "machine receipt",
        "apps/desktop/src-tauri/binaries/engine.exe": "binary",
        "apps/desktop/src-tauri/runtime/catalog.json": "private packaged runtime",
        "apps/desktop/node_modules/private-package/index.js": "dependency",
        "fixtures/data/client.parquet": "unknown client data",
    }
    for name, value in hidden.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    destination = tmp_path / "public"
    report = module.export(root, destination, archive=True)
    assert report["files"] >= 7
    assert (destination / "src/main.py").read_bytes() == (root / "src/main.py").read_bytes()
    assert (destination / "src/ai_powerbi_analyst/runtime/__init__.py").is_file()
    assert all(
        not (destination / name).exists() for name in hidden if not name.startswith("runtime/")
    )
    assert (root / ".git/config").read_text() == "original history"
    catalog = json.loads((destination / "runtime/catalog.json").read_text())
    assert catalog["profiles"]["standard"]["qualified"] is False
    assert str(root) not in (destination / "runtime/profiles/standard.json").read_text()
    manifest = json.loads((destination / "PUBLICATION_MANIFEST.json").read_text())
    assert "before Git line-ending conversion" in manifest["identity_scope"]
    assert "runtime/** -text" in (destination / ".gitattributes").read_text()
    for name, identity in manifest["files"].items():
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == identity["sha256"]
    with zipfile.ZipFile(Path(report["zip"])) as archive:
        assert "modelcraft/src/main.py" in archive.namelist()
        assert not any("/.git/" in name or "/.env" in name for name in archive.namelist())


def test_export_redacts_documentation_account_paths_without_changing_source(tmp_path):
    module = _exporter()
    root = _source(tmp_path)
    original = "Example: C:\\Users\\private-account\\project\n"
    (root / "README.md").write_text(original)
    historical = root / "docs/research/historical.md"
    historical.parent.mkdir(parents=True)
    historical.write_text(original)
    current = root / "docs/development.md"
    current.write_text("# Current setup\n")
    destination = tmp_path / "public"
    module.export(root, destination)
    assert "private-account" not in (destination / "README.md").read_text()
    assert (root / "README.md").read_text() == original
    exported_history = (destination / "docs/research/historical.md").read_text()
    assert exported_history.startswith("> Historical local record.")
    assert "[README](../../README.md)" in exported_history
    assert "[development guide](../development.md)" in exported_history
    assert historical.read_text() == original
    assert (destination / "docs/development.md").read_text() == current.read_text()


def test_credential_shaped_source_is_reported_without_value_or_export(tmp_path):
    module = _exporter()
    root = _source(tmp_path)
    value = "ghp_" + "A" * 36
    (root / "src/main.py").write_text("token = '" + value + "'\n")
    destination = tmp_path / "public"
    with pytest.raises(ValueError, match="Credential-shaped") as error:
        module.export(root, destination)
    assert value not in str(error.value)
    assert not destination.exists()


def test_existing_exports_and_source_directories_are_never_overwritten(tmp_path):
    module = _exporter()
    root = _source(tmp_path)
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(FileExistsError):
        module.export(root, destination)
    assert sentinel.read_text() == "keep"
    with pytest.raises(ValueError, match=".runtime/publication"):
        module.export(root, root / "src/new-export")
    assert not (root / "src/new-export").exists()


def test_runtime_source_export_requires_explicit_mit_license(tmp_path):
    module = _exporter()
    root = _source(tmp_path)
    (root / "vendor/powerbi-rag-runtime/LICENSE").write_text("Reserved rights")
    with pytest.raises(ValueError, match="explicit MIT"):
        module.export(root, tmp_path / "public")


def test_export_rejects_symlinked_source_files(tmp_path):
    module = _exporter()
    root = _source(tmp_path)
    private = tmp_path / "private.py"
    private.write_text("print('private')")
    link = root / "src/link.py"
    try:
        link.symlink_to(private)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
    with pytest.raises(ValueError, match="escapes|redirected"):
        module.export(root, tmp_path / "public")
