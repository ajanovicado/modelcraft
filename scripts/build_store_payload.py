"""Assemble the packaged AI runtime payload for the Store build.

Reads the developer-sealed adaptive catalog, copies every pinned payload file
(Ollama server tree, model store, worked-example corpus, app assets, node)
into the bundle layout under <src-tauri>/runtime/, rewrites each qualified
profile's path fields and hash pins to runtime-root-relative form, verifies
every copied byte against its seal, and emits a bundle catalog.json.

The packaged engine resolves runtime/catalog.json relative to its own install
directory, so the shipped layout is:

    <install>/ai-powerbi-studio-engine.exe   (pinned as ../<exe name>)
    <install>/runtime/catalog.json
    <install>/runtime/profiles/<profile>.json
    <install>/runtime/ollama/ollama.exe + lib/ollama/*.dll
    <install>/runtime/corpus/worked-examples.jsonl
    <install>/runtime/app/artifact_examples.json + m_validation.cjs
    <install>/runtime/node/node.exe
    <install>/runtime/training/.keep

The Qwen 4B model file itself is not bundled: MSI and NSIS installers stop at
2 GB and the model is one 3.4 GB file. The profile keeps its SHA-256 pin and a
download address, and the app installs it into
%LOCALAPPDATA%\\AI Power BI Analyst\\models on first launch (llm/model_install.py).

Source-file pins (*.py, plus data inputs such as report catalogs and uv.lock that
the engine build receipt covers) are dropped: in the frozen build they live inside
the engine executable, which is itself pinned and hash-verified at runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path, PureWindowsPath

REPO = Path(__file__).resolve().parents[1]

# Path-bearing profile fields rewritten to runtime-root-relative locations.
EMBEDDED_ASSETS = {
    "artifact_examples.json": "app/artifact_examples.json",
    "m_validation.cjs": "app/m_validation.cjs",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_pinned(source: Path, target: Path, expected: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    actual = sha256(target)
    if actual != expected:
        target.unlink()
        raise SystemExit(f"seal mismatch after copy: {source} -> {target}")


def classify(
    pin: str,
    *,
    ollama_root: str,
    model_store: str,
    corpus: str,
    engine_inputs: frozenset[str] = frozenset(),
) -> str | None:
    """Map one absolute sealed path to its bundle-relative location, or None."""
    normalized = pin.replace("/", "\\")
    lowered = normalized.lower()
    if lowered.startswith(ollama_root.lower() + "\\"):
        return "ollama/" + normalized[len(ollama_root) + 1 :].replace("\\", "/")
    if lowered.startswith(model_store.lower() + "\\"):
        return "models/" + normalized[len(model_store) + 1 :].replace("\\", "/")
    if lowered == corpus.lower():
        return "corpus/" + Path(normalized).name
    name = Path(normalized).name
    if name in EMBEDDED_ASSETS:
        return EMBEDDED_ASSETS[name]
    if name.lower() == "node.exe":
        return "node/node.exe"
    if lowered.endswith(".py") or lowered in engine_inputs:
        return None  # frozen into the engine; covered by the executable pin
    raise SystemExit(f"unclassified sealed file: {pin}")


def build_profile(
    source: Path,
    target: Path,
    key: str,
    engine_pin: tuple[str, str] | None,
    engine_inputs: frozenset[str] = frozenset(),
    source_pins: dict[str, str] | None = None,
) -> str:
    cfg = json.loads(source.read_text(encoding="utf-8"))
    server = Path(cfg["server"])
    if cfg.get("backend") == "managed-llama-text":
        if server.name != "llama-server.exe" or server.parent.parent.name != "lib":
            raise SystemExit("Text-only server must use the pinned Ollama lib/ollama layout")
        ollama_root = str(server.parents[2])
    else:
        ollama_root = str(server.parent)
    model_store = cfg["model_store"]
    corpus = cfg["corpus"]
    model_pin = cfg["model"]
    model_key = "blobs\\" + Path(model_pin).name

    hashes: dict[str, str] = {}
    dropped = 0
    for pin, digest in cfg["hashes"].items():
        if pin == model_pin:
            # Installers cannot carry the 3.4 GB model (MSI and NSIS stop at 2 GB), so
            # only its pin ships; the app installs the file per user on first launch.
            if sha256(Path(pin)) != digest:
                raise SystemExit(f"model seal mismatch: {pin}; rebuild and reseal first")
            hashes[model_key] = digest
            continue
        relative = classify(
            pin,
            ollama_root=ollama_root,
            model_store=model_store,
            corpus=corpus,
            engine_inputs=engine_inputs,
        )
        if relative is None:
            if sha256(Path(pin)) != digest:
                raise SystemExit(f"source seal mismatch: {pin}; rebuild and reseal first")
            dropped += 1
            if source_pins is not None:
                source_pins[portable_source_path(Path(pin))] = digest
            continue
        copy_pinned(Path(pin), target / relative, digest)
        hashes[relative.replace("/", "\\")] = digest

    if engine_pin is not None:
        engine_name, engine_sha = engine_pin
        hashes["..\\" + engine_name] = engine_sha
    cfg["hashes"] = hashes

    cfg["server"] = str(Path("ollama") / server.relative_to(ollama_root))
    # Resolved against the per-user model store (paths.user_model_root), not the install dir.
    cfg["model_location"] = "user"
    cfg["model"] = model_key
    cfg["model_store"] = "."
    repository = str(cfg.get("tag", "")).split(":", 1)[0]
    cfg["model_download"] = {
        # Ollama's registry serves blobs by content digest: exactly the pinned bytes.
        "url": (
            f"https://registry.ollama.ai/v2/library/{repository}/blobs/sha256:{hashes[model_key]}"
            if repository
            else None
        ),
        "bytes": Path(model_pin).stat().st_size,
    }
    cfg["corpus"] = "corpus\\" + Path(cfg["corpus"]).name
    cfg["artifact_examples"] = "app\\artifact_examples.json"
    cfg["m_validator"] = "app\\m_validation.cjs"
    cfg["node"] = "node\\node.exe"
    if isinstance(cfg.get("lineage"), dict) and "source" in cfg["lineage"]:
        cfg["lineage"]["source"] = Path(cfg["lineage"]["source"]).name

    profile_path = target / "profiles" / (key + ".json")
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"  {source.name}: {len(hashes)} payload pins, {dropped} source pins folded into engine")
    return sha256(profile_path)


def safe_relative(root: Path, value: str) -> Path:
    """Reject portable paths that could read/write outside their declared root."""
    windows = PureWindowsPath(value)
    if (
        windows.drive
        or windows.root
        or not windows.parts
        or any(part in {".", ".."} or ":" in part for part in windows.parts)
    ):
        raise ValueError(f"unsafe portable path: {value}")
    root = root.resolve()
    path = root.joinpath(*windows.parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"portable path escapes root: {value}")
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink() or (parent.exists() and parent.is_junction()):
            raise ValueError(f"portable path is a reparse point: {value}")
    return path


def portable_source_path(path: Path) -> str:
    import powerbi_rag_runtime

    resolved = path.resolve()
    if resolved.is_relative_to(REPO):
        relative = resolved.relative_to(REPO)
        if relative.parts[0] != ".venv":
            return relative.as_posix()
    installed = Path(powerbi_rag_runtime.__file__).resolve().parent
    if resolved.is_relative_to(installed):
        relative = Path("vendor/powerbi-rag-runtime/powerbi_rag_runtime") / resolved.relative_to(
            installed
        )
        vendored = REPO / relative
        if sha256(vendored) != sha256(resolved):
            raise ValueError("Installed runtime differs from its vendored source")
        return relative.as_posix()
    raise ValueError(f"Source pin is outside the portable source tree: {path}")


def portable_manifest(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        safe_relative(root, relative)
        if path.is_file() and relative != "input-manifest.json":
            result[relative] = sha256(path)
    return result


def build_portable(source: Path, target: Path, engine_pin: tuple[str, str]) -> None:
    manifest = json.loads((source / "input-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or portable_manifest(source) != manifest.get("files"):
        raise ValueError("Portable runtime input changed or has an unsupported manifest")
    for relative, expected in manifest["source_pins"].items():
        if sha256(safe_relative(REPO, relative)) != expected:
            raise ValueError(f"Portable runtime source seal mismatch: {relative}")
    # Copy only verified files into a fresh build, then bind its newly signed engine.
    for relative, expected in manifest["files"].items():
        copy_pinned(safe_relative(source, relative), safe_relative(target, relative), expected)
    catalog = json.loads((target / "catalog.json").read_text(encoding="utf-8"))
    for entry in catalog["profiles"].values():
        if entry.get("qualified") is not True:
            continue
        profile = safe_relative(target, entry["config"])
        if sha256(profile) != entry["sha256"]:
            raise ValueError("Portable profile seal mismatch")
        cfg = json.loads(profile.read_text(encoding="utf-8"))
        if cfg.get("model_location") != "user":
            raise ValueError("Portable model must use the per-user model store")
        for relative, expected in cfg["hashes"].items():
            # Model bytes were verified at export and are deliberately omitted.
            if relative == cfg["model"]:
                safe_relative(target, relative)
                continue
            if sha256(safe_relative(target, relative)) != expected:
                raise ValueError(f"Portable payload seal mismatch: {relative}")
        for field in ("server", "corpus", "artifact_examples", "m_validator", "node"):
            relative = cfg[field]
            safe_relative(target, relative)
            if relative.replace("/", "\\") not in cfg["hashes"]:
                raise ValueError(f"Portable execution input is not pinned: {field}")
        cfg["hashes"]["..\\" + engine_pin[0]] = engine_pin[1]
        profile.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        entry["sha256"] = sha256(profile)
    catalog["packaged"] = "windows-release-candidate"
    (target / "catalog.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")


def stage_tools(source: Path, target: Path) -> None:
    manifest_path = source / "toolchain.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or set(manifest.get("tools", {})) != {
        "pbi",
        "powerbi-report-author",
        "powerbi-desktop",
    }:
        raise ValueError("Unsupported or incomplete Power BI toolchain manifest")
    pins = manifest["files"]
    for tool in manifest["tools"].values():
        executable = tool["executable"]
        safe_relative(source, executable)
        if executable not in pins or not tool.get("version"):
            raise ValueError("Power BI tool executable/version is not pinned")
    destination = target / "tools"
    if destination.exists():
        raise ValueError("Toolchain staging target already exists")
    for relative, expected in pins.items():
        copy_pinned(safe_relative(source, relative), safe_relative(destination, relative), expected)
    copy_pinned(manifest_path, destination / "toolchain.json", sha256(manifest_path))
    catalog = json.loads((target / "catalog.json").read_text(encoding="utf-8"))
    for entry in catalog["profiles"].values():
        if entry.get("qualified") is not True:
            continue
        profile = safe_relative(target, entry["config"])
        if sha256(profile) != entry["sha256"]:
            raise ValueError("Profile changed before toolchain staging")
        cfg = json.loads(profile.read_text(encoding="utf-8"))
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                relative = path.relative_to(target).as_posix().replace("/", "\\")
                cfg["hashes"][relative] = sha256(path)
        profile.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        entry["sha256"] = sha256(profile)
    (target / "catalog.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")


def seal_native_signatures(target: Path, records_path: Path) -> None:
    """Update only verified native resource pins after explicit Authenticode signing."""
    records = json.loads(records_path.read_text(encoding="utf-8-sig"))
    changes = {}
    for record in records:
        relative = record["path"]
        path = safe_relative(target, relative)
        if path.suffix.lower() not in {".exe", ".dll", ".pyd"} or relative.startswith("tools/"):
            raise ValueError("Only native inference resources may be resealed after signing")
        if record.get("signature_status") != "Valid" or sha256(path) != record["signed_sha256"]:
            raise ValueError("Signed resource differs from its verified signing receipt")
        changes[relative.replace("/", "\\")] = record
    catalog = json.loads((target / "catalog.json").read_text(encoding="utf-8"))
    sealed = set()
    profiles = []
    for entry in catalog["profiles"].values():
        if entry.get("qualified") is not True:
            continue
        path = safe_relative(target, entry["config"])
        if sha256(path) != entry["sha256"]:
            raise ValueError("Profile changed before native signature resealing")
        cfg = json.loads(path.read_text(encoding="utf-8"))
        for relative, record in changes.items():
            if relative not in cfg["hashes"]:
                continue
            if cfg["hashes"][relative] != record["original_sha256"]:
                raise ValueError("Original native resource seal differs from signing receipt")
            cfg["hashes"][relative] = record["signed_sha256"]
            sealed.add(relative)
        profiles.append((entry, path, cfg))
    if sealed != set(changes):
        raise ValueError("Native signing receipt includes an unpinned resource")
    for entry, path, cfg in profiles:
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        entry["sha256"] = sha256(path)
    (target / "catalog.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")


def main() -> None:
    from sidecar_manifest import source_files, verify_engine

    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--export-input",
        action="store_true",
        help="Export portable sealed runtime input; model/source bytes are excluded",
    )
    modes.add_argument(
        "--portable-input", type=Path, help="Build from an exported portable runtime input"
    )
    modes.add_argument(
        "--seal-signatures",
        type=Path,
        help="Reseal explicitly verified native signatures in an assembled --out payload",
    )
    parser.add_argument(
        "--toolchain-root", type=Path, help="Verified self-contained Power BI toolchain to stage"
    )
    parser.add_argument(
        "--source-catalog",
        default=str(REPO / "runtime" / "catalog.json"),
        help="developer adaptive catalog whose profiles carry absolute pins",
    )
    parser.add_argument(
        "--engine",
        default=str(
            REPO
            / "apps/desktop/src-tauri/binaries/ai-powerbi-studio-engine-x86_64-pc-windows-msvc.exe"
        ),
        help="built sidecar executable to pin",
    )
    parser.add_argument(
        "--installed-exe",
        default="ai-powerbi-studio-engine.exe",
        help="executable name once installed next to the shell",
    )
    parser.add_argument(
        "--out",
        default=str(REPO / "apps/desktop/src-tauri/runtime"),
        help="payload output directory (becomes <install>/runtime)",
    )
    parser.add_argument(
        "--profiles",
        default="standard",
        help="comma-separated catalog profiles to ship (default: the only model, text-only 4B)",
    )
    args = parser.parse_args()

    out = Path(args.out)
    if args.seal_signatures:
        seal_native_signatures(out, args.seal_signatures)
        return
    if out.exists() and any(out.iterdir()):
        raise SystemExit("Payload output must be empty; use a new --out directory for this build")
    out.mkdir(parents=True, exist_ok=True)
    if not args.export_input:
        engine = Path(args.engine)
        if not engine.is_file():
            raise SystemExit(f"engine executable not found: {engine} (build the sidecar first)")
        verify_engine(REPO, engine)
        engine_pin = (args.installed_exe, sha256(engine))
        if args.portable_input:
            build_portable(args.portable_input, out, engine_pin)
            if args.toolchain_root:
                stage_tools(args.toolchain_root, out)
            print(f"portable payload written to {out}")
            return
    else:
        engine_pin = None
    source_catalog = Path(args.source_catalog)
    catalog = json.loads(source_catalog.read_text(encoding="utf-8"))
    # Dev seals also pin frozen data inputs (report catalogs, themes, uv.lock); the
    # verified build receipt binds them to the engine, so its pin covers them.
    engine_inputs = frozenset(str(path).lower() for path in source_files(REPO))

    wanted = [name.strip() for name in args.profiles.split(",") if name.strip()]
    shipped: dict[str, dict] = {}
    source_pins: dict[str, str] = {}
    for key in wanted:
        entry = catalog["profiles"].get(key)
        if not entry or entry.get("qualified") is not True:
            raise SystemExit(f"profile {key!r} is not qualified in {source_catalog.name}")
        profile_source = source_catalog.parent / entry["config"]
        if sha256(profile_source) != entry["sha256"]:
            raise SystemExit(f"profile seal mismatch: {profile_source}")
        digest = build_profile(
            profile_source,
            out,
            key,
            engine_pin,
            engine_inputs,
            source_pins if args.export_input else None,
        )
        shipped[key] = {
            "config": f"profiles/{key}.json",
            "sha256": digest,
            "qualified": True,
            "qualification_scope": entry.get("qualification_scope", ""),
        }

    for key in catalog["profiles"]:
        if key not in shipped:
            shipped[key] = {
                "qualified": False,
                "note": "Not shipped in the Store bundle; see build_store_payload.py --profiles.",
            }

    bundle_catalog = {
        "adaptive_profiles": catalog["adaptive_profiles"],
        "profiles": shipped,
        "rejected_profiles": catalog.get("rejected_profiles", {}),
        "packaged": "store-win32",
    }
    (out / "training").mkdir(parents=True, exist_ok=True)
    (out / "training" / ".keep").touch()
    (out / "catalog.json").write_text(json.dumps(bundle_catalog, indent=2), encoding="utf-8")
    if args.export_input:
        manifest = {"schema": 1, "source_pins": source_pins, "files": portable_manifest(out)}
        (out / "input-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    elif args.toolchain_root:
        stage_tools(args.toolchain_root, out)
    print(f"payload written to {out}")


if __name__ == "__main__":
    sys.exit(main())
