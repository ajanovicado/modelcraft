"""Explicit, offline import of the audited Microsoft Learn collection.

Only one named member is read. No extraction, training records, executable code,
or model answers enter the index. Original source records are retained locally.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock

from .library import KnowledgeLibrary, Reference, SourceProvenance

PACKAGE = "03-Reference-data-part-04-of-17.zip"
MEMBER = "data/data-first-001/reference/pages.jsonl"
MAX_MEMBER = 40 * 1024**2
MAX_PACKAGE = 100 * 1024**2
CHUNK_LIMIT = 1400
SKIP_HEADINGS = {"related content", "related links", "additional resources", "feedback"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_collection(path: Path) -> tuple[bytes, dict[str, str]]:
    """Bound archive reads and verify package/member hashes against the manifest."""
    with path.open("rb") as stream:
        archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    with zipfile.ZipFile(path) as outer:

        def named(suffix: str) -> zipfile.ZipInfo:
            matches = [i for i in outer.infolist() if i.filename.endswith("/" + suffix)]
            if len(matches) != 1:
                raise ValueError(f"Expected exactly one archive member: {suffix}")
            return matches[0]

        manifest_info = named("UPLOAD-MANIFEST.json")
        if manifest_info.file_size > 5 * 1024**2:
            raise ValueError("Oversized archive manifest")
        manifest = json.loads(outer.read(manifest_info))
        package_info = named(PACKAGE)
        if package_info.file_size > MAX_PACKAGE:
            raise ValueError("Oversized reference package")
        package = outer.read(package_info)
        expected = [p for p in manifest["packages"] if p["name"] == PACKAGE]
        if len(expected) != 1 or sha(package) != expected[0]["sha256"]:
            raise ValueError("Reference package hash mismatch")
        entries = expected[0]["entries"]
        if isinstance(entries, list):
            entries = {e.get("path", e.get("name")): e for e in entries}
        with zipfile.ZipFile(io.BytesIO(package)) as nested:
            matches = [i for i in nested.infolist() if i.filename == MEMBER]
            if len(matches) != 1 or matches[0].file_size > MAX_MEMBER:
                raise ValueError("Missing, duplicated or oversized reference collection")
            raw = nested.read(matches[0])
        if sha(raw) != entries[MEMBER]["sha256"]:
            raise ValueError("Reference collection hash mismatch")
    return raw, {"archive_sha256": archive_hash, "member_sha256": sha(raw)}


def blocks(text: str) -> list[str]:
    """Keep fenced formulas indivisible; split prose only on paragraph boundaries."""
    result: list[str] = []
    pending: list[str] = []
    fence = ""
    for line in text.strip().splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        is_marker = marker and (
            not fence
            or (marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip())
        )
        if is_marker and marker:
            if not fence and pending:
                result.append("\n".join(pending).strip())
                pending = []
            fence = "" if fence else marker[1]
            pending.append(line)
            if not fence:
                result.append("\n".join(pending).strip())
                pending = []
        elif not line.strip() and not fence:
            if pending:
                result.append("\n".join(pending).strip())
                pending = []
        else:
            pending.append(line.rstrip())
    if fence:
        raise ValueError("Unclosed documentation code fence")
    if pending:
        result.append("\n".join(pending).strip())
    return [b for b in result if b]


def page_chunks(page: dict[str, Any]) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    sections = page["sections"]
    output: list[tuple[str, str]] = []
    omitted: list[dict[str, Any]] = []
    # A self-contained API card puts syntax and caveats near the definition.
    core: list[str] = []
    partial = False
    notice = "Partial reference: consult the full source for additional parameters and caveats."
    for heading in ("Syntax", "Overview", "About", "Remarks", "Parameters", "Return value"):
        for section in sections:
            if section["heading"].lower() != heading.lower():
                continue
            selected: list[str] = []
            for block in blocks(section["text"]):
                trial = "\n\n".join(core + [heading + "\n" + "\n\n".join(selected + [block])])
                if len(trial) + len(notice) + 2 <= CHUNK_LIMIT:
                    selected.append(block)
                else:
                    partial = True
            if selected:
                core.append(heading + "\n" + "\n\n".join(selected))
    quick = "\n\n".join(core)
    if quick:
        if partial:
            quick += "\n\n" + notice
        output.append(("Quick reference", quick))
    for section in sections:
        heading = section["heading"][:240]
        if heading.lower() in SKIP_HEADINGS:
            continue
        current = ""
        for block in blocks(section["text"]):
            if len(block) + len(heading) + 2 > CHUNK_LIMIT:
                # Never truncate code, tables or sentences to force a context fit.
                omitted.append(
                    {
                        "heading": heading,
                        "sha256": sha(block.encode()),
                        "characters": len(block),
                        "reason": "oversized_atomic_block",
                    }
                )
                continue
            if len(current) + len(block) + len(heading) + 3 > CHUNK_LIMIT:
                output.append((heading, heading + "\n" + current))
                current = ""
            current += ("\n\n" if current else "") + block
        if current:
            output.append((heading, heading + "\n" + current))
    # Stable, byte-exact deduplication within a source.
    return list(dict.fromkeys(output)), omitted


def compile_collection(raw: bytes, hashes: dict[str, str]) -> dict[str, Any]:
    references: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in raw.decode("utf-8-sig").splitlines():
        if not line.strip():
            continue
        page = json.loads(line)
        if page["origin"] in seen:
            raise ValueError("Duplicate documentation URL")
        seen.add(page["origin"])
        if sha(page["text"].encode()) != page["text_sha256"]:
            raise ValueError("Page text hash mismatch")
        # Section text must be exactly represented in the hashed page text.
        rebuilt = "\n\n".join(s["heading"] + "\n" + s["text"] for s in page["sections"])
        if rebuilt != page["text"]:
            raise ValueError("Sections do not match the hashed page text")
        provenance = SourceProvenance(
            **hashes,
            page_text_sha256=page["text_sha256"],
            raw_page_sha256=page["sha256"],
            archive_member=MEMBER,
            retrieved_at=page["retrieved_at"],
            attribution=page["attribution"],
            license_note=page["license_note"],
        )
        chunks, omitted = page_chunks(page)
        if not chunks:
            raise ValueError(f"No retrievable content for {page['origin']}")
        for index, (heading, text) in enumerate(chunks):
            reference = Reference(
                id=f"archive-{sha(page['origin'].encode())[:24]}-{index}",
                title=page["title"][:240],
                kind="documentation",
                source_url=page["origin"],
                revision=page["text_sha256"],
                text=text,
                keywords=page["title"] + " " + heading + " " + page["domain"],
                sha256=sha(text.encode()),
                verification="source_snapshot",
                topic=page["domain"],
                section=heading,
                provenance=provenance,
            )
            references.append(reference.model_dump(mode="json"))
        sources.append(
            {
                "url": page["origin"],
                "title": page["title"],
                "topic": page["domain"],
                "chunks": len(chunks),
                "omitted_blocks": omitted,
                "provenance": provenance.model_dump(mode="json"),
            }
        )
    return {
        "version": "1.0",
        "policy": "reference-pages-only",
        "created_at": datetime.now(UTC).isoformat(),
        **hashes,
        "local_only": True,
        "sources": sources,
        "references": references,
    }


def import_archive(path: Path, state_root: Path, *, apply: bool = False) -> dict[str, Any]:
    raw, hashes = read_collection(path)
    payload = compile_collection(raw, hashes)
    summary = {
        **hashes,
        "documents": len(payload["sources"]),
        "chunks": len(payload["references"]),
        "topics": dict(Counter(s["topic"] for s in payload["sources"])),
        "omitted_blocks": sum(len(s["omitted_blocks"]) for s in payload["sources"]),
        "applied": False,
    }
    if not apply:
        return summary
    dest = state_root.resolve() / "knowledge"
    dest.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(dest).free < 512 * 1024**2:
        raise ValueError("Preserve at least 512 MiB disk headroom for knowledge import")
    with FileLock(str(dest / "import.lock"), timeout=10):  # noqa: SIM117
        with tempfile.TemporaryDirectory(prefix="knowledge-stage-", dir=dest) as temporary:
            stage = Path(temporary)
            target = dest / "reviewed-archive.json"
            staged = stage / target.name
            staged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            if (dest / "source-snapshot.json").exists():
                shutil.copy2(dest / "source-snapshot.json", stage / "source-snapshot.json")
            # Exercise the actual reader and index before replacing anything live.
            library = KnowledgeLibrary(stage / "index.sqlite3")
            library.close()
            originals = dest / "original-collections"
            originals.mkdir(exist_ok=True)
            original = originals / (hashes["member_sha256"] + ".jsonl")
            if original.exists() and sha(original.read_bytes()) != hashes["member_sha256"]:
                raise ValueError("Existing original collection hash mismatch")
            if not original.exists():
                (stage / "original.jsonl").write_bytes(raw)
                (stage / "original.jsonl").replace(original)
            if target.exists():
                backup = dest / "backups"
                backup.mkdir(exist_ok=True)
                shutil.copy2(target, backup / (sha(target.read_bytes()) + ".json"))
            staged.replace(target)
    return {**summary, "applied": True, "target": str(target)}
