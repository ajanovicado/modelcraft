"""Export a reviewed public source snapshot without copying local Git history.

The allowlist includes code, dependency locks and synthetic test fixtures. Local
models, runtime seals, user data, evidence and build outputs are never exported.
No existing directory is overwritten or deleted; this command never publishes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT_FILES = {
    ".gitattributes",
    ".gitignore",
    ".pre-commit-config.yaml",
    ".python-version",
    "CONTRIBUTING.md",
    "LICENSE",
    "PRIVACY.md",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
    "uv.lock",
    ".github/PULL_REQUEST_TEMPLATE.md",
}
SOURCE_ROOTS = (
    "src",
    "typings",
    "tests",
    "scripts",
    "config",
    "packaging",
    "vendor/powerbi-rag-runtime",
    "apps/desktop",
    ".github/workflows",
    ".github/ISSUE_TEMPLATE",
    "docs",
    "fixtures/powerbi/client-wow-template",
    "evals/cases",
    "evals/imported_edit",
    "evals/dax_engine_differential",
)
FIXTURE_FILES = {
    "evidence/golden-answers.jsonl",
    "fixtures/data/README.md",
    "fixtures/data/semantic_catalog.yaml",
    "fixtures/data/manifest.json",
    "fixtures/data/analytics.duckdb",
    "fixtures/data/sales.parquet",
    "fixtures/data/driver_contributions.parquet",
    "fixtures/data/forecast_backtest.parquet",
    "fixtures/data/forecast_selection.parquet",
    "fixtures/data/forecast.parquet",
    "fixtures/data/scenario_grid.parquet",
    "fixtures/imported/README.md",
    "fixtures/imported/structure-twin.pbix",
    "fixtures/imported/multipage.pbix",
}
SCREENSHOTS = {f"docs/images/showcase-{view}.png" for view in ("report", "data", "model", "dax")}
PRUNED = {
    ".git",
    ".venv",
    "node_modules",
    "target",
    "dist",
    "build",
    "__pycache__",
    "evidence",
    "battery-results",
    "binaries",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "test-results",
    "playwright-report",
}
TEXT_SUFFIXES = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".cjs",
    ".css",
    ".scss",
    ".html",
    ".svg",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".lock",
    ".md",
    ".txt",
    ".ps1",
    ".sh",
    ".bat",
    ".cmd",
    ".cs",
    ".rs",
    ".sql",
    ".tmdl",
    ".pbip",
    ".pbir",
    ".pbism",
    ".typed",
}
ASSET_SUFFIXES = {".png", ".ico", ".icns", ".woff", ".woff2"}
SECRET_PATTERNS = {
    "private-key": rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "github-token": rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",
    "cloud-access-key": rb"(?:AKIA|ASIA)[A-Z0-9]{16}",
    "provider-key": (
        rb"(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|AIza[0-9A-Za-z_-]{30,}"
        rb"|xox[baprs]-[A-Za-z0-9-]{20,})"
    ),
    "url-credential": rb"https?://[^\s/<>:@]{1,80}:[^\s/<>@]{4,120}@",
    "literal-credential": (
        rb"(?i)(?:api[_-]?key|client[_-]?secret|password|access[_-]?token)"
        rb"\s*[:=]\s*[\"']([^\"'\r\n]{12,160})[\"']"
    ),
}
MAX_FILE_BYTES = 10 * 1024 * 1024


def public_markdown(relative: str, data: bytes) -> tuple[bytes, bool]:
    """Redact account paths and label historical records that cite private artifacts."""
    changed = re.sub(rb"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/\s\"'`<>]+", rb"\1developer", data)
    if changed == data:
        return data, False
    if relative.startswith("docs/"):
        parent = Path(relative).parent
        readme = os.path.relpath("README.md", parent).replace("\\", "/")
        development = os.path.relpath("docs/development.md", parent).replace("\\", "/")
        notice = (
            "> Historical local record. Referenced machine artifacts are excluded from this "
            "public source snapshot. Use the "
            f"[README]({readme}) and [development guide]({development}) for current setup "
            "and tested scope.\n\n"
        ).encode()
        changed = notice + changed
    return changed, True


def secret_findings(data: bytes) -> list[str]:
    """Return rule names only, never credential values or source excerpts."""
    return [name for name, pattern in SECRET_PATTERNS.items() if re.search(pattern, data)]


def safe_file(root: Path, path: Path) -> None:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("An export source escapes the repository")
    for entry in (path, *path.parents):
        info = entry.lstat()
        if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
            raise ValueError("Export sources cannot contain redirected files or directories")
        if entry == root:
            break


def selected_files(root: Path) -> list[Path]:
    selected = {root / name for name in ROOT_FILES | FIXTURE_FILES | SCREENSHOTS}
    for relative in SOURCE_ROOTS:
        directory = root / relative
        if not directory.is_dir():
            continue
        safe_file(root, directory)
        for base, directories, filenames in os.walk(directory, followlinks=False):
            retained = []
            for name in directories:
                if name in PRUNED or name.startswith(".runtime"):
                    continue
                if name == "runtime" and Path(base).relative_to(root).parts[0] not in {
                    "src",
                    "tests",
                }:
                    continue  # installed payloads; the application's runtime Python package ships
                safe_file(root, Path(base) / name)
                retained.append(name)
            directories[:] = retained
            for name in filenames:
                path = Path(base) / name
                rel = path.relative_to(root).as_posix()
                if rel in {"apps/desktop/vite.config.js", "apps/desktop/vite.config.d.ts"}:
                    continue  # emitted by TypeScript; vite.config.ts is the source
                if name.startswith(".env") or name == "LICENSE.proprietary":
                    continue
                if path.suffix in TEXT_SUFFIXES or name in {".gitignore", ".keep", "LICENSE"}:
                    if rel.startswith("docs/") and path.suffix != ".md":
                        continue
                    selected.add(path)
                elif path.suffix in ASSET_SUFFIXES and rel.startswith("apps/desktop/"):
                    selected.add(path)
    return sorted(path for path in selected if path.is_file())


def public_runtime() -> dict[str, bytes]:
    """Keep source/test structure while making absent runtime qualification explicit."""
    profile = {
        "resource_profile": "standard",
        "backend": "managed-llama-text",
        "tag": "qwen3.5:4b-q4_K_M",
        "context_tokens": 4096,
        "package_sha256": "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd",
        "prompt_cache_mib": 128,
        "parameter_size": "4B",
        "quantization": "Q4_K_M",
        "license": "Apache-2.0",
        "source_url": "https://huggingface.co/Qwen/Qwen3.5-4B",
        "server": "ollama/lib/ollama/llama-server.exe",
        "model_location": "user",
        "model": "blobs/sha256-81fb60c7daa80fc1123380b98970b320ae233409f0f71a72ed7b9b0d62f40490",
        "model_store": ".",
        "corpus": "corpus/worked-examples.jsonl",
        "artifact_examples": "app/artifact_examples.json",
        "m_validator": "app/m_validation.cjs",
        "node": "node/node.exe",
        "hashes": {},
        "qualification_scope": (
            "Unqualified public-source example. No installed payload or live-model approval."
        ),
    }
    profile_bytes = (json.dumps(profile, indent=2) + "\n").encode()
    catalog = {
        "adaptive_profiles": 1,
        "profiles": {
            "standard": {
                "config": "profiles/standard.json",
                "sha256": hashlib.sha256(profile_bytes).hexdigest(),
                "qualified": False,
                "qualification_scope": profile["qualification_scope"],
            }
        },
        "rejected_profiles": {"coder": "Retired; Qwen 4B is the only supported model."},
    }
    return {
        "runtime/catalog.json": (json.dumps(catalog, indent=2) + "\n").encode(),
        "runtime/profiles/standard.json": profile_bytes,
        "runtime/training/.keep": b"",
        "runtime/README.md": (
            b"# Public source runtime\n\nThis is an unqualified, path-free example. Model weights, "
            b"native binaries, private retrieval corpora and development seals are excluded. "
            b"Fixture tests and the synthetic browser demo require no model. Live inference needs "
            b"a separately configured and qualified local runtime; this catalog authorizes none.\n"
        ),
    }


def export(root: Path, destination: Path, *, archive: bool = False, init_git: bool = False) -> dict:
    root, destination = root.resolve(), destination.resolve()
    if destination.exists():
        raise FileExistsError("Use a fresh export directory; existing files are never overwritten")
    if destination.is_relative_to(root) and not destination.is_relative_to(
        root / ".runtime/publication"
    ):
        raise ValueError("In-repository exports must stay inside .runtime/publication")
    archive_path = destination.with_suffix(".zip")
    if archive and archive_path.exists():
        raise FileExistsError("The ZIP target already exists")
    vendor_license = root / "vendor/powerbi-rag-runtime/LICENSE"
    if not vendor_license.is_file() or "MIT License" not in vendor_license.read_text(
        encoding="utf-8"
    ):
        raise ValueError("Public runtime export requires the owner's explicit MIT license")
    snapshot = {}
    redacted_docs = []
    for source in selected_files(root):
        safe_file(root, source)
        if source.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(
                f"An allowlisted file exceeds the export size limit: {source.relative_to(root)}"
            )
        relative = source.relative_to(root).as_posix()
        data = source.read_bytes()
        findings = secret_findings(data) if source.suffix in TEXT_SUFFIXES else []
        if findings:
            raise ValueError(
                f"Credential-shaped content requires review: {relative} ({', '.join(findings)})"
            )
        if source.suffix == ".md":
            # Documentation examples should not disclose the development account.
            data, changed = public_markdown(relative, data)
            if changed:
                redacted_docs.append(relative)
        snapshot[relative] = data
    if ".gitignore" in snapshot:
        snapshot[".gitignore"] += (
            b"\n# Public-source repository: keep local output and credentials private.\n"
            b"evidence/\nevals/**/evidence/\nevals/**/battery-results/\n"
            b"apps/desktop/src-tauri/binaries/\nruntime/tools/\nruntime/ollama/\nruntime/models/\n"
            b"*.gguf\n*.safetensors\n*.pem\n*.pfx\n*.p12\n"
            b"\n# Reviewed deterministic synthetic evaluator fixture only.\n"
            b"!evidence/\nevidence/*\n!evidence/golden-answers.jsonl\n"
        )
    if ".gitattributes" in snapshot:
        snapshot[".gitattributes"] += (
            b"\n# The public runtime example includes a pinned profile hash.\nruntime/** -text\n"
        )
    snapshot.update(public_runtime())
    identities = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        for name, data in sorted(snapshot.items())
    }
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "scope": "Public source snapshot; no installer or live-model qualification",
        "identity_scope": (
            "Exported source and ZIP file bytes before Git line-ending conversion; "
            "not a cross-platform checkout-byte certification"
        ),
        "original_history_copied": False,
        "credential_shaped_findings": [],
        "documentation_paths_redacted": redacted_docs,
        "files": identities,
        "excluded": [
            "original Git history",
            "private evidence",
            "user data/state",
            "model weights",
            "native payloads",
            "development runtime seals",
        ],
    }
    snapshot["PUBLICATION_MANIFEST.json"] = (json.dumps(report, indent=2) + "\n").encode()
    destination.mkdir(parents=True, exist_ok=False)
    for relative, data in sorted(snapshot.items()):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if archive:
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED) as output:
            for relative, data in sorted(snapshot.items()):
                output.writestr(f"modelcraft/{relative}", data)
    if init_git:
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("Git is required when --init-git is requested")
        subprocess.run(  # noqa: S603 - fixed git argv; isolated fresh output only
            [git, "-c", "init.templateDir=", "init", "--initial-branch=main", str(destination)],
            check=True,
            capture_output=True,
            text=True,
        )
    return {
        "directory": str(destination),
        "zip": str(archive_path) if archive else None,
        "files": len(snapshot),
        "bytes": sum(map(len, snapshot.values())),
        "git_initialized": init_git,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--zip", action="store_true")
    parser.add_argument(
        "--init-git",
        action="store_true",
        help="Initialize fresh local history; no staging, commit or push",
    )
    args = parser.parse_args()
    destination = args.out or args.root / ".runtime/publication" / uuid.uuid4().hex / "modelcraft"
    print(
        json.dumps(
            export(args.root, destination, archive=args.zip, init_git=args.init_git), indent=2
        )
    )


if __name__ == "__main__":
    main()
