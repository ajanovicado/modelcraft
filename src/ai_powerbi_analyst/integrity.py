"""Content-addressed project integrity checks.

Runtime analysis and report writes fail closed when their prepared inputs no
longer match the manifest produced during project preparation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import Field, field_validator, model_validator

from ai_powerbi_analyst.contracts import ClosedModel, Sha256


class ManifestEntry(ClosedModel):
    path: str
    sha256: Sha256
    size_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def path_stays_inside_project(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            path.is_absolute()
            or PureWindowsPath(normalized).drive
            or not normalized
            or path == PurePosixPath(".")
            or ".." in path.parts
        ):
            raise ValueError("manifest entry must use a safe relative project path")
        return path.as_posix()


class IntegrityManifest(ClosedModel):
    version: str = Field(pattern=r"^\d+\.\d+$")
    entries: list[ManifestEntry]

    @model_validator(mode="after")
    def paths_are_unique(self) -> IntegrityManifest:
        paths = [entry.path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("manifest paths must be unique")
        return self


class IntegrityResult(ClosedModel):
    changed: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    unexpected: list[str] = Field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not (self.changed or self.missing or self.unexpected)


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a regular file without loading it fully into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(root: Path, path: Path) -> str:
    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    if not resolved_path.is_file():
        raise ValueError(f"manifest inputs must be regular files: {path}")
    try:
        return resolved_path.relative_to(resolved_root).as_posix()
    except ValueError as error:
        raise ValueError(f"manifest input is outside project root: {path}") from error


def build_manifest(root: Path, paths: Iterable[Path]) -> IntegrityManifest:
    """Create a stable manifest for an explicit set of prepared artifacts."""

    entries: list[ManifestEntry] = []
    for path in paths:
        relative = _safe_relative(root, path)
        entries.append(
            ManifestEntry(
                path=relative,
                sha256=sha256_file(path),
                size_bytes=path.stat().st_size,
            )
        )
    entries.sort(key=lambda entry: entry.path)
    return IntegrityManifest(version="1.0", entries=entries)


def verify_manifest(
    root: Path,
    manifest: IntegrityManifest,
    *,
    expected_paths: Iterable[str] | None = None,
) -> IntegrityResult:
    """Compare prepared artifacts with their frozen hashes.

    ``expected_paths`` is an optional additional required-file allowlist used
    by preflight checks.  Paths are validated through ``ManifestEntry`` before
    touching the filesystem. Every candidate is resolved and confined to the
    project root before file metadata or contents are read.
    """

    root = root.resolve(strict=True)
    expected = {
        ManifestEntry.path_stays_inside_project(entry.path): entry for entry in manifest.entries
    }
    required = set(expected)
    unexpected: list[str] = []
    if expected_paths is not None:
        required = set()
        for raw_path in expected_paths:
            validated = ManifestEntry(path=raw_path, sha256="0" * 64, size_bytes=0)
            required.add(validated.path)
        unexpected = sorted(path for path in expected if path not in required)

    candidates: dict[str, Path] = {}
    for relative in sorted(required | set(expected)):
        try:
            candidate = (root / relative).resolve(strict=False)
            candidate.relative_to(root)
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError(
                f"manifest input is outside project root or cannot resolve: {relative}"
            ) from error
        candidates[relative] = candidate

    present = {relative for relative, candidate in candidates.items() if candidate.is_file()}
    missing = sorted(required - present)
    changed: list[str] = []
    for relative, entry in expected.items():
        if relative not in present:
            continue
        candidate = candidates[relative]
        if candidate.stat().st_size != entry.size_bytes or sha256_file(candidate) != entry.sha256:
            changed.append(relative)

    return IntegrityResult(changed=sorted(changed), missing=missing, unexpected=unexpected)
