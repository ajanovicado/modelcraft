"""Candidate isolation, portable seals and exact-byte qualification."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


@pytest.fixture
def portable(tmp_path, monkeypatch):
    builder = load("build_store_payload")
    root = tmp_path / "repository"
    monkeypatch.setattr(builder, "REPO", root)
    server = root / "Ollama/lib/ollama/llama-server.exe"
    model = root / "models/blobs/pinned-model"
    source = root / "src/app.py"
    payload = [
        server,
        model,
        source,
        root / "worked-examples.jsonl",
        root / "artifact_examples.json",
        root / "m_validation.cjs",
        root / "node.exe",
    ]
    for file in payload:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(file.name.encode())
    cfg = {
        "backend": "managed-llama-text",
        "tag": "qwen3.5:4b-q4_K_M",
        "server": str(server),
        "model": str(model),
        "model_store": str(root / "models"),
        "corpus": str(root / "worked-examples.jsonl"),
        "hashes": {str(file): builder.sha256(file) for file in payload},
    }
    config = root / "profile.json"
    config.write_text(json.dumps(cfg), encoding="utf-8")
    out = tmp_path / "portable"
    pins = {}
    profile_digest = builder.build_profile(config, out, "standard", None, source_pins=pins)
    catalog = {
        "profiles": {
            "standard": {
                "config": "profiles/standard.json",
                "qualified": True,
                "sha256": profile_digest,
            }
        }
    }
    (out / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    manifest = {"schema": 1, "source_pins": pins, "files": builder.portable_manifest(out)}
    (out / "input-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return builder, root, out


def test_portable_runtime_excludes_source_and_weights_and_binds_final_signed_engine(
    portable, tmp_path
):
    builder, root, exported = portable
    assert not list(exported.rglob("*.py")) and not (exported / "models").exists()
    built = tmp_path / "bundled"
    builder.build_portable(exported, built, ("engine.exe", "signed-engine-digest"))
    profile = json.loads((built / "profiles/standard.json").read_text())
    assert profile["hashes"]["..\\engine.exe"] == "signed-engine-digest"
    catalog = json.loads((built / "catalog.json").read_text())
    assert catalog["profiles"]["standard"]["sha256"] == builder.sha256(
        built / "profiles/standard.json"
    )
    assert (root / "src/app.py").read_bytes() == b"app.py"


@pytest.mark.parametrize("tamper", ["runtime", "source", "extra"])
def test_portable_input_rejects_stale_or_added_bytes(portable, tmp_path, tamper):
    builder, root, exported = portable
    if tamper == "runtime":
        (exported / "ollama/lib/ollama/llama-server.exe").write_bytes(b"changed")
    elif tamper == "source":
        (root / "src/app.py").write_bytes(b"changed")
    else:
        (exported / "unapproved.dll").write_bytes(b"extra")
    with pytest.raises(ValueError, match="changed|seal mismatch"):
        builder.build_portable(exported, tmp_path / "bundle", ("engine.exe", "new-hash"))


@pytest.mark.parametrize(
    "path", ["../outside", "C:\\outside", "C:relative", "\\rooted", "tools/../../outside"]
)
def test_portable_manifest_cannot_escape_declared_root(tmp_path, path):
    builder = load("build_store_payload")
    with pytest.raises(ValueError, match="unsafe"):
        builder.safe_relative(tmp_path, path)


def test_native_signing_reseals_only_matching_original_pins(portable, tmp_path):
    builder, _root, exported = portable
    built = tmp_path / "bundled"
    builder.build_portable(exported, built, ("engine.exe", "signed-engine-digest"))
    relative = "ollama/lib/ollama/llama-server.exe"
    executable = built / relative
    original = builder.sha256(executable)
    executable.write_bytes(b"signed fixture bytes")
    record = {
        "path": relative,
        "original_sha256": original,
        "signed_sha256": builder.sha256(executable),
        "signature_status": "Valid",
    }
    receipt = tmp_path / "native-signatures.json"
    record["original_sha256"] = "unapproved original"
    receipt.write_text(json.dumps([record]), encoding="utf-8")
    profile = built / "profiles/standard.json"
    before = profile.read_bytes()
    with pytest.raises(ValueError, match="Original native resource seal"):
        builder.seal_native_signatures(built, receipt)
    assert profile.read_bytes() == before
    record["original_sha256"] = original
    receipt.write_text(json.dumps([record]), encoding="utf-8")
    builder.seal_native_signatures(built, receipt)
    cfg = json.loads(profile.read_text())
    assert cfg["hashes"][relative.replace("/", "\\")] == builder.sha256(executable)
    assert cfg["hashes"]["..\\engine.exe"] == "signed-engine-digest"
    assert json.loads((built / "catalog.json").read_text())["profiles"]["standard"][
        "sha256"
    ] == builder.sha256(profile)


def create_repo(root, release):
    root.mkdir()
    files = {
        "pyproject.toml": '[project]\nname="app"\nversion="0.1.0"\n',
        "uv.lock": '[[package]]\nname="app"\nversion="0.1.0"\n',
        "apps/desktop/src-tauri/Cargo.toml": '[package]\nname="app"\nversion="0.1.0"\n',
        "apps/desktop/src-tauri/Cargo.lock": '[[package]]\nname="app"\nversion="0.1.0"\n',
        "apps/desktop/src-tauri/tauri.conf.json": '{"version":"0.1.0"}',
        "apps/desktop/package.json": '{"version":"0.1.0"}',
        "apps/desktop/package-lock.json": '{"version":"0.1.0","packages":{"":{"version":"0.1.0"}}}',
        "app.py": "original",
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
    release.git(root, "init", "--quiet")
    release.git(root, "add", ".")
    release.git(
        root,
        "-c",
        "user.name=Release test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )


def test_snapshot_preserves_dirty_work_and_rejects_stale_source_or_output(tmp_path):
    release = load("windows_release")
    root = tmp_path / "repo"
    create_repo(root, release)
    (root / "app.py").write_text("user work", encoding="utf-8")
    output = tmp_path / "candidate"
    with pytest.raises(ValueError, match="dirty"):
        release.snapshot(root, output, False)
    release.snapshot(root, output, True)
    assert (root / "app.py").read_text() == "user work"
    assert (output / "source/app.py").read_text() == "user work"
    release.verify_snapshot(output)
    with pytest.raises(ValueError, match="already exists"):
        release.snapshot(root, output, True)
    extra = output / "source/unreviewed.py"
    extra.write_text("new source", encoding="utf-8")
    with pytest.raises(ValueError, match="added"):
        release.verify_snapshot(output)
    extra.unlink()
    (output / "source/app.py").write_text("late source change", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        release.verify_snapshot(output)


def test_version_drift_is_rejected_before_snapshot_creation(tmp_path):
    release = load("windows_release")
    root = tmp_path / "repo"
    create_repo(root, release)
    (root / "apps/desktop/src-tauri/Cargo.lock").write_text(
        '[[package]]\nname="app"\nversion="0.0.9"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="versions disagree"):
        release.snapshot(root, tmp_path / "candidate", True)
    assert not (tmp_path / "candidate").exists()


def test_qualification_rejects_changed_installer_and_unsigned_candidate(tmp_path):
    release = load("windows_release")
    downloads = tmp_path / "downloads"
    review = tmp_path / "review"
    downloads.mkdir()
    review.mkdir()
    (downloads / "app.exe").write_bytes(b"installer fixture")
    (downloads / "app.msi").write_bytes(b"msi fixture")
    (downloads / "source-manifest.json").write_text("{}", encoding="utf-8")
    (review / "release-notes.md").write_text("Fixture release", encoding="utf-8")
    candidate = {
        "version": "0.1.0",
        "commit": "reviewed-commit",
        "dirty_source": False,
        "signing": "authenticode-verified",
        "engineering_checks_passed": True,
        "bundled_tools_complete": True,
        "source_manifest_sha256": release.digest(downloads / "source-manifest.json"),
        "artifacts": [
            {"file": name, "sha256": release.digest(downloads / name)}
            for name in ("app.exe", "app.msi")
        ],
    }
    path = downloads / "candidate.json"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    qualification = {
        "schema": 1,
        "approved_for_distribution": True,
        "source_commit": "reviewed-commit",
        "candidate_manifest_sha256": release.digest(path),
        "reviewed_by": "fixture reviewer",
        "reviewed_at": "fixture date",
        "release_notes_sha256": release.digest(review / "release-notes.md"),
        "gates": {
            name: {"passed": True, "evidence": "fixture receipt"}
            for name in (
                "clean_windows_install",
                "upgrade_uninstall",
                "real_model_corpus",
                "powerbi_desktop",
                "offline_operation",
                "licenses_reviewed",
                "security_review",
            )
        },
    }
    (review / "qualification.json").write_text(json.dumps(qualification), encoding="utf-8")
    assert release.qualify_artifacts(downloads, review, "reviewed-commit") == "0.1.0"
    (downloads / "app.exe").write_bytes(b"changed installer")
    with pytest.raises(ValueError, match="bytes changed"):
        release.qualify_artifacts(downloads, review, "reviewed-commit")
    candidate["signing"] = "unsigned-local-candidate"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    with pytest.raises(ValueError, match="unsigned"):
        release.qualify_artifacts(downloads, review, "reviewed-commit")
