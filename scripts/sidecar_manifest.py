"""Bind a frozen engine to the exact source inputs used during its build."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_files(root: Path) -> list[Path]:
    import powerbi_rag_runtime

    files = [
        root / "pyproject.toml",
        root / "uv.lock",
        root / "config/defaults.yaml",
        root / "scripts/studio_sidecar_entry.py",
        root / "scripts/build-desktop-sidecar.ps1",
        root / "scripts/sign-windows-artifact.ps1",
        root / "scripts/sidecar_manifest.py",
    ]
    for directory in (
        root / "src",
        root / "fixtures/powerbi/client-wow-template",
        Path(powerbi_rag_runtime.__file__).parent,
    ):
        files.extend(
            path
            for path in directory.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    return sorted(set(path.resolve() for path in files))


def capture(root: Path) -> dict[str, str]:
    return {str(path): digest(path) for path in source_files(root)}


def verify_sources(root: Path, expected: dict[str, str]) -> None:
    if capture(root) != expected:
        raise ValueError("Engine source inputs changed; rebuild the sidecar before packaging")


def verify_engine(root: Path, engine: Path) -> None:
    receipt = json.loads(engine.with_suffix(".build.json").read_text(encoding="utf-8"))
    if receipt.get("engine_sha256") != digest(engine):
        raise ValueError("Engine executable differs from its build receipt")
    verify_sources(root, receipt["sources"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["capture", "finalize"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--engine", type=Path)
    args = parser.parse_args()
    if args.mode == "capture":
        args.manifest.write_text(json.dumps(capture(args.root), indent=2), encoding="utf-8")
        return
    if args.engine is None:
        parser.error("finalize requires --engine")
    sources = json.loads(args.manifest.read_text(encoding="utf-8"))
    verify_sources(args.root, sources)
    args.engine.with_suffix(".build.json").write_text(
        json.dumps({"engine_sha256": digest(args.engine), "sources": sources}, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
