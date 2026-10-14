import io
import json
import zipfile
from pathlib import Path

import pytest

from ai_powerbi_analyst.knowledge.archive import (
    MEMBER,
    PACKAGE,
    blocks,
    compile_collection,
    import_archive,
    page_chunks,
    sha,
)
from ai_powerbi_analyst.knowledge.library import KnowledgeLibrary


def collection():
    sections = [
        {
            "heading": "Syntax",
            "text": "```\nTable.NestedJoin(table1, key1, table2, key2, name)\n```",
        },
        {"heading": "About", "text": "Joins tables by matching keys. Default is left outer join."},
    ]
    text = "\n\n".join(s["heading"] + "\n" + s["text"] for s in sections)
    page = {
        "title": "Table.NestedJoin",
        "origin": "https://learn.microsoft.com/en-us/powerquery-m/table-nestedjoin",
        "text": text,
        "text_sha256": sha(text.encode()),
        "sha256": "1" * 64,
        "sections": sections,
        "retrieved_at": "2026-09-06T00:00:00+00:00",
        "attribution": "Microsoft Learn",
        "license_note": "Local use only",
        "domain": "power_query_m",
    }
    return page


def archive(path: Path, *, tamper=False):
    raw = json.dumps(collection()).encode()
    nested_bytes = io.BytesIO()
    with zipfile.ZipFile(nested_bytes, "w") as nested:
        nested.writestr(MEMBER, raw)
        # This must never be extracted, parsed or indexed.
        nested.writestr("../../training/test.jsonl", "SECRET_TEST_ANSWER")
    package = nested_bytes.getvalue()
    manifest = {
        "packages": [
            {
                "name": PACKAGE,
                "sha256": sha(package),
                "entries": {MEMBER: {"sha256": "0" * 64 if tamper else sha(raw)}},
            }
        ]
    }
    with zipfile.ZipFile(path, "w") as outer:
        outer.writestr("root/UPLOAD-MANIFEST.json", json.dumps(manifest))
        outer.writestr("root/" + PACKAGE, package)


def test_import_is_explicit_local_and_preserves_provenance(tmp_path):
    source = tmp_path / "source.zip"
    archive(source)
    state = tmp_path / "state"
    assert import_archive(source, state)["applied"] is False
    assert not state.exists()
    result = import_archive(source, state, apply=True)
    assert result["documents"] == 1
    library = KnowledgeLibrary(state / "knowledge/index.sqlite3")
    receipt = library.retrieve("Table.NestedJoin", mode="docs", max_characters=2400)
    ref = receipt.references[0]
    assert ref.title == "Table.NestedJoin"
    assert ref.section == "Quick reference"
    assert "left outer" in ref.text
    assert ref.provenance.member_sha256
    assert all(r.kind == "documentation" for r in receipt.references)
    assert "SECRET_TEST_ANSWER" not in "".join(r.text for r in library.entries.values())
    assert receipt.context_characters <= 2400
    library.close()
    before = Path(result["target"]).read_bytes()
    import_archive(source, state, apply=True)
    assert (state / "knowledge/backups" / (sha(before) + ".json")).read_bytes() == before


def test_hash_failure_never_replaces_live_snapshot(tmp_path):
    source = tmp_path / "source.zip"
    archive(source)
    result = import_archive(source, tmp_path / "state", apply=True)
    original = Path(result["target"]).read_bytes()
    archive(source, tamper=True)
    with pytest.raises(ValueError, match="hash mismatch"):
        import_archive(source, tmp_path / "state", apply=True)
    assert Path(result["target"]).read_bytes() == original


def test_validation_failure_does_not_commit(tmp_path, monkeypatch):
    source = tmp_path / "source.zip"
    archive(source)
    state = tmp_path / "state"
    result = import_archive(source, state, apply=True)
    original = Path(result["target"]).read_bytes()

    def fail(*args, **kwargs):
        raise ValueError("injected indexing failure")

    monkeypatch.setattr(KnowledgeLibrary, "_load", fail)
    with pytest.raises(ValueError, match="injected"):
        import_archive(source, state, apply=True)
    assert Path(result["target"]).read_bytes() == original


def test_chunker_preserves_nested_tmdl_and_never_clips_code():
    text = "```\ntable Example\n\tmeasure X = ```\n\t\tSUM(T[V])\n\t\t```\n```"
    assert blocks(text) == [text]
    with pytest.raises(ValueError, match="Unclosed"):
        blocks("```\nSUM(T[V])")
    page = collection()
    page["sections"].append({"heading": "Example", "text": "```\n" + "X" * 1800 + "\n```"})
    chunks, omitted = page_chunks(page)
    assert omitted[0]["reason"] == "oversized_atomic_block"
    assert all(len(text) <= 1400 for _, text in chunks)
    assert not any("XXXXX" in text for _, text in chunks)


def test_atomic_replace_failure_keeps_previous_corpus(tmp_path, monkeypatch):
    source = tmp_path / "source.zip"
    archive(source)
    state = tmp_path / "state"
    result = import_archive(source, state, apply=True)
    target = Path(result["target"])
    original = target.read_bytes()
    replace = Path.replace

    def fail(path, destination):
        if Path(destination) == target:
            raise OSError("injected replacement failure")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="injected"):
        import_archive(source, state, apply=True)
    assert target.read_bytes() == original
    assert (state / "knowledge/backups" / (sha(original) + ".json")).read_bytes() == original


def test_fixed_grain_does_not_retrieve_fixed_number_formatting(tmp_path):
    pages = []
    for function, text in (
        ("FIXED", "Formats a number as fixed text with decimals."),
        ("ALLEXCEPT", "Preserves explicit group filters and removes other table filters."),
    ):
        page = collection()
        page.update(
            title=function,
            domain="dax",
            origin=f"https://learn.microsoft.com/en-us/dax/{function.lower()}-function-dax",
            sections=[{"heading": "Overview", "text": text}],
            text="Overview\n" + text,
        )
        page["text_sha256"] = sha(page["text"].encode())
        pages.append(page)
    raw = "\n".join(json.dumps(p) for p in pages).encode()
    payload = compile_collection(raw, {"archive_sha256": "1" * 64, "member_sha256": "2" * 64})
    (tmp_path / "reviewed-archive.json").write_text(json.dumps(payload))
    library = KnowledgeLibrary(tmp_path / "index.sqlite3")
    refs = library.retrieve("ALLEXCEPT fixed grain Region filters", max_characters=2400).references
    assert any(r.title == "ALLEXCEPT" for r in refs)
    assert all(r.title != "FIXED" for r in refs)
    assert library.retrieve("FIXED number decimals", mode="docs").references[0].title == "FIXED"
    library.close()


def test_large_quick_reference_keeps_whole_syntax_and_declares_partial():
    page = collection()
    page["sections"].append({"heading": "Remarks", "text": "Important " * 200})
    chunks, _ = page_chunks(page)
    heading, text = chunks[0]
    assert heading == "Quick reference"
    assert "```\nTable.NestedJoin(table1, key1, table2, key2, name)\n```" in text
    assert "Partial reference" in text
    assert "Important " not in text
    assert len(text) <= 1400


def test_live_reader_does_not_use_stale_references_after_index_update(tmp_path):
    old = KnowledgeLibrary(tmp_path / "index.sqlite3")
    raw = json.dumps(collection()).encode()
    payload = compile_collection(raw, {"archive_sha256": "1" * 64, "member_sha256": "2" * 64})
    (tmp_path / "reviewed-archive.json").write_text(json.dumps(payload))
    new = KnowledgeLibrary(tmp_path / "index.sqlite3")
    with pytest.raises(RuntimeError, match="restart"):
        old.retrieve("Table.NestedJoin")
    assert new.retrieve("Table.NestedJoin").references[0].title == "Table.NestedJoin"
    old.close()
    new.close()


@pytest.mark.parametrize("mutation", ["url", "hash", "sections", "duplicate"])
def test_invalid_source_records_fail_closed(mutation):
    page = collection()
    if mutation == "url":
        page["origin"] = "https://learn.microsoft.com.evil.test/en-us/dax/sum"
    elif mutation == "hash":
        page["text_sha256"] = "0" * 64
    elif mutation == "sections":
        page["sections"][0]["text"] = "injected"
    raw = json.dumps(page).encode()
    if mutation == "duplicate":
        raw += b"\n" + raw
    with pytest.raises(ValueError):
        compile_collection(raw, {"archive_sha256": "1" * 64, "member_sha256": "2" * 64})
