"""Re-seal the developer runtime catalog after changing pinned source files.

Regenerates repo/runtime/profiles/*.json and catalog.json in place:
- re-hashes every existing pin (fails loudly if a pinned file disappeared),
- adds new files to the seal (--add, repo-relative or absolute),
- records the change in each profile's lineage,
- recomputes the catalog's per-profile sha256.

Use after editing any file listed under "hashes" in a qualified profile —
the guarded runtime is fail-closed and will refuse to start on stale pins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / "runtime"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--add",
        action="append",
        default=[],
        help="additional file to seal (absolute or repo-relative); repeatable",
    )
    parser.add_argument("--note", required=True, help="lineage note for the reseal")
    parser.add_argument(
        "--remove", action="append", default=[],
        help="explicitly remove a deleted file's pin (repo-relative or absolute); repeatable",
    )
    args = parser.parse_args()

    catalog_path = RUNTIME / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    added = [str((REPO / path).resolve()) for path in args.add]
    removed = {str((REPO / path).resolve()) for path in args.remove}
    present = [path for path in removed if Path(path).exists()]
    if present:
        raise SystemExit(f"Refusing to remove pins for files that still exist: {present}")

    for key, entry in catalog["profiles"].items():
        if entry.get("qualified") is not True:
            continue
        profile_path = RUNTIME / entry["config"]
        cfg = json.loads(profile_path.read_text(encoding="utf-8"))
        changed, missing = [], []
        hashes = cfg["hashes"]
        for pin in list(hashes):
            if pin in removed:
                del hashes[pin]
                changed.append(Path(pin).name + " (deleted pin)")
                continue
            path = Path(pin)
            if not path.is_file():
                missing.append(pin)
                continue
            actual = sha256(path)
            if actual != hashes[pin]:
                changed.append(path.name)
                hashes[pin] = actual
        if missing:
            raise SystemExit(f"{key}: pinned files missing: {missing}")
        for pin in added:
            if pin not in hashes:
                hashes[pin] = sha256(Path(pin))
                changed.append(Path(pin).name + " (new)")
        cfg.setdefault("lineage", {})
        cfg["lineage"].update(
            {
                "source": profile_path.name,
                "sha256": entry["sha256"],
                "scope": args.note,
                "resealed": time.time(),
            }
        )
        profile_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        entry["sha256"] = sha256(profile_path)
        print(f"{key}: resealed ({len(hashes)} pins; changed: {', '.join(changed) or 'none'})")

    catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    print("catalog.json updated")


if __name__ == "__main__":
    main()
