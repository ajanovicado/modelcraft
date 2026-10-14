"""Install the pinned official Qwen3.5 4B package without starting inference."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
import urllib.request
import uuid
from pathlib import Path

# The only local model; the digest is the manifest pin in runtime/profiles/standard.json.
PACKAGES = {
    "4b-q4_K_M": "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd",
}
REGISTRY = "https://registry.ollama.ai/v2/library/qwen3.5"


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def install(store: Path, evidence: Path) -> None:
    evidence.mkdir(parents=True, exist_ok=False)
    store.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(store).free < 8 * 1024**3:
        raise RuntimeError("At least 8 GiB free disk space is required before installation")
    installed = []
    family = "qwen3.5"
    for tag, expected in PACKAGES.items():
        with urllib.request.urlopen(  # noqa: S310 - fixed HTTPS registry and pinned tag allowlist
            f"{REGISTRY}/manifests/{tag}", timeout=30
        ) as response:
            raw = response.read(64 * 1024)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise RuntimeError(f"Official package changed: {tag}; review before updating its pin")
        manifest = json.loads(raw)
        (evidence / f"{tag}.manifest.json").write_bytes(raw)
        for layer in [manifest["config"], *manifest["layers"]]:
            digest = layer["digest"]
            if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith("sha256:"):
                raise ValueError("Invalid package digest")
            target = store / "blobs" / digest.replace(":", "-")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.stat().st_size != layer["size"] or sha(target) != digest[7:]:
                    raise RuntimeError(f"Existing model blob failed verification: {target.name}")
                continue
            partial = target.with_name(target.name + ".partial-" + uuid.uuid4().hex)
            count, last = 0, time.monotonic()
            checksum = hashlib.sha256()
            with (
                urllib.request.urlopen(  # noqa: S310 - fixed HTTPS registry; hashed manifest
                    f"{REGISTRY}/blobs/{digest}", timeout=60
                ) as response,
                partial.open("xb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > layer["size"]:
                        raise RuntimeError("Download exceeds pinned size")
                    if shutil.disk_usage(store).free < 8 * 1024**3:
                        raise RuntimeError("Download stopped at the 8 GiB free-disk floor")
                    checksum.update(chunk)
                    output.write(chunk)
                    if time.monotonic() - last >= 10:
                        print(
                            json.dumps({"tag": tag, "downloaded_mib": count // 1024**2}), flush=True
                        )
                        last = time.monotonic()
            if count != layer["size"] or checksum.hexdigest() != digest[7:]:
                raise RuntimeError("Downloaded package failed size/hash verification")
            if target.exists():
                raise RuntimeError("Another installer created this blob; refusing overwrite")
            partial.rename(target)
        target = store / "manifests/registry.ollama.ai/library" / family / tag
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != raw:
                raise RuntimeError("Existing tag differs; refusing overwrite")
        else:
            with target.open("xb") as stream:
                stream.write(raw)
        installed.append({"tag": family + ":" + tag, "sha256": expected, "manifest": str(target)})
        print(json.dumps(installed[-1]), flush=True)
    with (evidence / "receipt.json").open("x", encoding="utf-8") as stream:
        json.dump({"packages": installed, "inference_started": False}, stream, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path.home() / ".ollama/models")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    install(args.store, args.evidence)
