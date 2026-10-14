"""Explicit online setup only: cache selected Microsoft Learn references for local use.

Runtime never imports or executes this downloader. No dataset or user prompt is sent.
Run with --state-root PATH. Downloaded pages are local-only and must not be packaged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DOCS = {
    "query-docs": [
        ("query-languages/dax/" + name + ".md", "https://learn.microsoft.com/en-us/dax/" + name)
        for name in [
            "divide-function-dax",
            "sumx-function-dax",
            "sum-function-dax",
            "allexcept-function-dax",
            "calculate-function-dax",
            "removefilters-function-dax",
            "userelationship-function-dax",
            "datesinperiod-function-dax",
            "distinctcount-function-dax",
            "best-practices/dax-variables",
            "dax-queries",
        ]
    ],
    "powerbi-docs": [
        ("powerbi-docs/" + name + ".md", "https://learn.microsoft.com/en-us/power-bi/" + name)
        for name in [
            "guidance/star-schema",
            "guidance/relationships-many-to-many",
            "transform-model/desktop-relationships-understand",
        ]
    ],
}


def fetch(url: str) -> str:
    with urlopen(
        Request(
            url, headers={"User-Agent": "PowerBIStudio-reference-setup", "Accept": "text/markdown"}
        ),
        timeout=40,
    ) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("Unexpectedly large reference")
    return data.decode("utf-8-sig")


def chunks(markdown: str):
    body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", markdown, count=1, flags=re.S)
    blocks, lines, fenced = [], [], False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not line.strip() and not fenced:
            if lines:
                blocks.append("\n".join(lines))
                lines = []
        else:
            lines.append(line)
    if lines:
        blocks.append("\n".join(lines))
    current = ""
    for block in blocks:
        if len(block) > 1400:  # Never clip a formula or oversized table into invalid fragments.
            continue
        if len(current) + len(block) + 2 > 1400:
            if current:
                yield current
            current = ""
        current += ("\n\n" if current else "") + block
    if current:
        yield current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()
    dest = args.state_root.resolve() / "knowledge"
    dest.mkdir(parents=True, exist_ok=True)
    originals = dest / "original-pages"
    originals.mkdir(exist_ok=True)
    references, sources = [], []
    for repo, paths in DOCS.items():
        for source_path, source_url in paths:
            markdown = fetch(source_url)
            if not markdown.startswith("---"):
                raise ValueError("Expected Microsoft Learn markdown metadata")
            revision_match = re.search(r"^git_commit_id: ([a-f0-9]{40})$", markdown, re.M)
            commit = (
                revision_match.group(1)
                if revision_match
                else hashlib.sha256(markdown.encode()).hexdigest()
            )
            title_match = re.search(r"^# (.+)$", markdown, re.M)
            title = title_match.group(1) if title_match else source_path.rsplit("/", 1)[-1]
            key = source_path.rsplit("/", 1)[-1].removesuffix(".md")
            page_hash = hashlib.sha256(markdown.encode()).hexdigest()
            (originals / (page_hash + ".md")).write_bytes(markdown.encode("utf-8"))
            sources.append(
                {
                    "url": source_url,
                    "revision": commit,
                    "sha256": hashlib.sha256(markdown.encode()).hexdigest(),
                }
            )
            for index, text in enumerate(chunks(markdown)):
                references.append(
                    {
                        "id": f"doc-{key}-{index}",
                        "title": title[:240],
                        "kind": "documentation",
                        "source_url": source_url,
                        "revision": commit,
                        "text": text,
                        "keywords": key.replace("-", " "),
                        "sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "verification": "source_snapshot",
                    }
                )
            print(f"Snapshotted {repo}/{source_path}", flush=True)
    payload = {
        "version": "1.0",
        "retrieved_at": datetime.now(UTC).isoformat(),
        "attribution": (
            "Copyright Microsoft and contributors. Local informational reference cache; "
            "not for redistribution. Original unmodified pages retained separately."
        ),
        "terms_url": "https://learn.microsoft.com/en-us/legal/termsofuse",
        "sources": sources,
        "references": references,
    }
    # Collect all sources successfully before replacing the local snapshot atomically.
    target = dest / "source-snapshot.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)
    print(f"Cached {len(sources)} documents, {len(references)} chunks locally; runtime is offline.")


if __name__ == "__main__":
    main()
