from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from ai_powerbi_analyst import integrity
from ai_powerbi_analyst.integrity import (
    IntegrityManifest,
    ManifestEntry,
    build_manifest,
    sha256_file,
    verify_manifest,
)


def test_sha256_file_matches_known_value(tmp_path: Path) -> None:
    path = tmp_path / "fixture.txt"
    path.write_bytes(b"sales-and-margin\n")

    assert sha256_file(path) == "ed01fc7754d8f12555530ff7d664c9d7ad32ac64362bb4ae980ab9b86cba7e84"


def test_manifest_detects_changed_bytes(tmp_path: Path) -> None:
    data = tmp_path / "data.parquet"
    catalog = tmp_path / "catalog.yaml"
    data.write_bytes(b"frozen-data")
    catalog.write_text("version: 1.0.0\n", encoding="utf-8")
    manifest = build_manifest(tmp_path, [data, catalog])

    assert verify_manifest(tmp_path, manifest).is_valid

    data.write_bytes(b"tampered-data")
    result = verify_manifest(tmp_path, manifest)
    assert not result.is_valid
    assert result.changed == ["data.parquet"]


def test_manifest_detects_missing_and_unexpected_files(tmp_path: Path) -> None:
    one = tmp_path / "one.txt"
    one.write_text("one", encoding="utf-8")
    manifest = build_manifest(tmp_path, [one])
    one.unlink()

    result = verify_manifest(tmp_path, manifest, expected_paths=["one.txt", "two.txt"])

    assert result.missing == ["one.txt", "two.txt"]


def test_manifest_rejects_parent_traversal() -> None:
    with pytest.raises(ValueError, match="relative project path"):
        ManifestEntry(path="../secret.txt", sha256="a" * 64, size_bytes=1)


@pytest.mark.parametrize(
    "path",
    [
        "C:/outside.txt",
        r"C:\outside.txt",
        "C:outside.txt",
        "C:../outside.txt",
        r"\\server\share\outside.txt",
        "/outside.txt",
        "",
        ".",
        "./",
    ],
)
def test_manifest_rejects_nonrelative_paths_on_every_platform(path: str) -> None:
    with pytest.raises(ValueError, match="relative project path"):
        ManifestEntry(path=path, sha256="a" * 64, size_bytes=1)


def test_manifest_normalizes_safe_relative_windows_separators() -> None:
    entry = ManifestEntry(path=r"fixtures\nested\data.parquet", sha256="a" * 64, size_bytes=1)

    assert entry.path == "fixtures/nested/data.parquet"


@pytest.mark.parametrize("in_required_paths", [False, True])
def test_verify_manifest_rejects_owned_outside_file_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_required_paths: bool
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"owned fixture outside project")
    entry = ManifestEntry(path="safe.txt", sha256="a" * 64, size_bytes=1)
    # Revalidate even model instances changed after their initial construction.
    entry = entry.model_copy(update={"path": str(outside)})
    manifest = IntegrityManifest(version="1.0", entries=[] if in_required_paths else [entry])

    def unexpected_hash(path: Path) -> str:
        pytest.fail(f"must not hash an out-of-root file: {path}")

    monkeypatch.setattr(integrity, "sha256_file", unexpected_hash)
    with pytest.raises(ValueError, match="relative project path"):
        verify_manifest(
            root,
            manifest,
            expected_paths=[str(outside)] if in_required_paths else None,
        )


def _directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("fixture directory links are unavailable")
        # Windows junctions do not require the symbolic-link privilege.
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            pytest.skip("fixture directory links and junctions are unavailable")


@pytest.mark.parametrize("source", ["manifest", "unexpected", "required_missing"])
def test_verify_manifest_rejects_directory_link_escape_before_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    content = b"owned fixture outside project"
    (outside / "data.txt").write_bytes(content)
    _directory_link(root / "linked", outside)
    entry = ManifestEntry(
        path="linked/data.txt", sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content)
    )
    manifest = IntegrityManifest(
        version="1.0", entries=[] if source == "required_missing" else [entry]
    )
    required = None
    if source == "unexpected":
        required = ["safe.txt"]
    elif source == "required_missing":
        required = ["linked/missing.txt"]

    def unexpected_hash(path: Path) -> str:
        pytest.fail(f"must not hash an out-of-root file: {path}")

    def unexpected_stat(path: Path) -> bool:
        pytest.fail(f"must confine all candidates before checking file metadata: {path}")

    monkeypatch.setattr(integrity, "sha256_file", unexpected_hash)
    monkeypatch.setattr(Path, "is_file", unexpected_stat)
    with pytest.raises(ValueError, match="outside project root"):
        verify_manifest(root, manifest, expected_paths=required)


def test_verify_manifest_allows_directory_link_within_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    target = root / "data"
    target.mkdir()
    content = b"owned fixture inside project"
    (target / "data.txt").write_bytes(content)
    _directory_link(root / "linked", target)
    manifest = IntegrityManifest(
        version="1.0",
        entries=[
            ManifestEntry(
                path="linked/data.txt",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
            )
        ],
    )

    assert verify_manifest(root, manifest).is_valid


def test_missing_unexpected_manifest_entry_is_reported_without_reading_it(tmp_path: Path) -> None:
    manifest = IntegrityManifest(
        version="1.0",
        entries=[ManifestEntry(path="unexpected.txt", sha256="a" * 64, size_bytes=1)],
    )

    result = verify_manifest(tmp_path, manifest, expected_paths=["required.txt"])

    assert result.missing == ["required.txt"]
    assert result.unexpected == ["unexpected.txt"]


def test_manifest_round_trips_as_closed_contract() -> None:
    manifest = IntegrityManifest(
        version="1.0",
        entries=[ManifestEntry(path="fixtures/data.duckdb", sha256="a" * 64, size_bytes=42)],
    )

    assert IntegrityManifest.model_validate_json(manifest.model_dump_json()) == manifest
