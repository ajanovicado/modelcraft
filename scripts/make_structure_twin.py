"""Write a committable structure twin of an imported Power BI report (PBIX).

    .venv/Scripts/python.exe scripts/make_structure_twin.py [SOURCE.pbix] [--out PATH]

Studio's report editor was verified in Power BI Desktop 2.157 on one real PBIR-in-PBIX
file, the Agents Performance dashboard (docs/imported-report-editing.md). That file cannot
be committed because its DataModel holds employee names. The twin keeps what the editor's
tests depend on and drops everything personal.

What stays exactly as Desktop wrote it:
- the zip member order, compression types and timestamps;
- the encodings: UTF-16LE without a BOM for Version, DiagramLayout, Settings, Metadata and
  daxQueries.json, UTF-8 with a BOM for [Content_Types].xml, compact UTF-8 for PBIR JSON;
- every Report/definition part, token for token. Only string literals outside a reviewed
  list of generic captions and formatting tokens are replaced, so number spellings such as
  966.14173228346453 survive. Parsing and re-serializing would shorten them.

What is replaced:
- DataModel: 64 KiB of deterministic filler, STORED. Studio streams it and never parses it.
- SecurityBindings: filler of the original length. Studio drops it on every write.
- images under Report/StaticResources: a 1x1 PNG with the same name.
- DAX query files: a generic query in the original encoding.

Before writing, the script reads the source model with pbixray. It refuses to write if any
value of a name, e-mail or phone column appears as a word in the twin's text parts.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import re
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"
DEFAULT_OUT = REPO / "fixtures" / "imported" / "structure-twin.pbix"
MAX_TWIN_BYTES = 1024 * 1024
DATA_MODEL_BYTES = 64 * 1024

# Every string literal of the Agents report was reviewed on 2026-09-28. They are business
# captions, helper-table category values and formatting tokens; none names a person. A
# literal that is not listed here (a new caption after the report changes) is replaced.
REVIEWED_LITERALS = frozenset(
    {
        # captions
        "Agents Avg MTD % Change",
        "Agents Avg MTD Sales ",
        "Agents with Positive Change %",
        "Avg Discount per Transaction",
        "Avg MTD % change by Agents",
        "Avg MTD Sales by Agents",
        "Country",
        "Number of Agents Sold above 100K",
        "PM: ",
        "PM:",
        "Target: ",
        "Top/Bottom Field",
        "TopN/BottomN",
        "Total MTD Sales",
        # values of the model's helper tables, used in selectors and filters
        "BottomN",
        "MOM% Sales",
        "Others",
        "TopN",
        # resource names
        "Background-1-01.png",
        # formatting tokens
        "Bottom",
        "Center",
        "Custom",
        "Dropdown",
        "Fit",
        "Left",
        "Middle",
        "Minimal",
        "aboveValue",
        "below",
        "bottom",
        "center",
        "custom",
        "imageUrl",
        "left",
        "middle",
        "rectangleRoundedByPixel",
    }
)
_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
# Desktop's font stacks: 'Segoe UI Bold', wf_segoe-ui_bold, helvetica, arial, sans-serif
_FONT_STACK = re.compile(r"'[A-Za-z ]+', wf_[a-z_-]+(?:, [a-z -]+)*")
# A JSON string token. Numbers and punctuation never contain a quote, so scanning from the
# start of a part visits exactly the part's strings.
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
# A text box run: {"value":"text", ...}. Its text is plain, not a quoted literal.
_TEXT_RUN = re.compile(r'("value":)("(?:[^"\\]|\\.)*")')
# Model columns whose values identify people.
_PERSON_COLUMN = re.compile(
    r"(first|last|middle|given|family|sur|full|employee|person|customer|contact|user)"
    r"\s*_?name|e-?mail|phone",
    re.IGNORECASE,
)
# Root parts without an extension that hold JSON (UTF-16LE in Desktop's files).
_JSON_ROOT_PARTS = frozenset({"DiagramLayout", "Settings", "Metadata"})
_GENERIC_DAX_QUERY = (
    "// Structure twin: the original DAX query text is not part of this fixture.\n"
    "EVALUATE\n"
    "    { 1 }\n"
)


class TwinError(RuntimeError):
    """The twin would not be a faithful, personal-data-free copy."""


def filler(label: str, size: int) -> bytes:
    """Deterministic bytes, so regenerating the twin from the same source is a no-op."""
    return hashlib.shake_256(f"structure-twin/{label}".encode()).digest(size)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def tiny_png() -> bytes:
    """A valid 1x1 RGB PNG in the page's background grey (#F8F8F8)."""
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(bytes([0, 0xF8, 0xF8, 0xF8]))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", pixels)
        + _png_chunk(b"IEND", b"")
    )


def is_generic(text: str) -> bool:
    return (
        text in REVIEWED_LITERALS
        or _HEX_COLOR.fullmatch(text) is not None
        or _FONT_STACK.fullmatch(text) is not None
    )


def scrub_definition(raw: bytes, replaced: dict[str, str]) -> bytes:
    """Replace unreviewed string literals and text-box runs; every other byte stays."""
    bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
    text = raw[len(bom) :].decode("utf-8")

    def literal(match: re.Match[str]) -> str:
        value = json.loads(match.group(0))
        if not (isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == "'"):
            return match.group(0)
        inner = value[1:-1].replace("''", "'")
        if is_generic(inner):
            return match.group(0)
        substitute = replaced.setdefault(inner, f"Text {len(replaced) + 1}")
        return json.dumps("'" + substitute.replace("'", "''") + "'", ensure_ascii=False)

    def text_run(match: re.Match[str]) -> str:
        value = json.loads(match.group(2))
        if not isinstance(value, str) or not value.strip() or is_generic(value):
            return match.group(0)
        substitute = replaced.setdefault(value, f"Text {len(replaced) + 1}")
        return match.group(1) + json.dumps(substitute, ensure_ascii=False)

    text = _TEXT_RUN.sub(text_run, text)
    return bom + _JSON_STRING.sub(literal, text).encode("utf-8")


def decode_text(data: bytes) -> str | None:
    """A part's text as Desktop encoded it, or None for binary parts."""
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", errors="strict")
    if b"\x00" in data[:16]:
        try:
            return data.decode("utf-16-le")
        except UnicodeDecodeError:
            return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def replace_dax_query(data: bytes) -> bytes:
    """The generic query, in the source file's encoding (Desktop: UTF-16LE, no BOM)."""
    if data.startswith(codecs.BOM_UTF16_LE):
        return codecs.BOM_UTF16_LE + _GENERIC_DAX_QUERY.encode("utf-16-le")
    if b"\x00" in data[:16]:
        return _GENERIC_DAX_QUERY.encode("utf-16-le")
    return _GENERIC_DAX_QUERY.encode("utf-8")


def twin_part(name: str, data: bytes, replaced: dict[str, str]) -> tuple[bytes, int | None]:
    """(bytes, forced compression or None to keep the source's) for one part."""
    if name == "DataModel":
        return filler(name, DATA_MODEL_BYTES), zipfile.ZIP_STORED
    if name == "SecurityBindings":
        return filler(name, len(data)), None
    if name.startswith("Report/StaticResources/") and name.lower().endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp")
    ):
        return tiny_png(), None
    if name.lower().endswith(".dax"):
        return replace_dax_query(data), None
    if name.startswith("Report/definition/") and name.endswith(".json"):
        return scrub_definition(data, replaced), None
    return data, None


def shape(value: Any) -> Any:
    """The JSON structure with every string reduced to its type: keys, order, lengths."""
    if isinstance(value, dict):
        return [(key, shape(item)) for key, item in value.items()]
    if isinstance(value, list):
        return [shape(item) for item in value]
    return type(value).__name__


def person_values(source: Path) -> set[str]:
    """Values of the model's name, e-mail and phone columns, read locally with pbixray."""
    from pbixray import PBIXRay  # type: ignore[import-untyped]

    model = PBIXRay(str(source))
    schema = model.schema
    values: set[str] = set()
    for row in schema.itertuples():
        if row.PandasDataType == "string" and _PERSON_COLUMN.search(str(row.ColumnName)):
            column = model.get_table(str(row.TableName))[str(row.ColumnName)]
            values |= {str(v).strip() for v in column.dropna().unique() if len(str(v)) >= 3}
    return values


def check_no_person_values(parts: dict[str, bytes], values: set[str]) -> None:
    if not values:
        return
    words = re.compile(
        r"\b(?:" + "|".join(re.escape(v) for v in sorted(values, key=len, reverse=True)) + r")\b",
        re.IGNORECASE,
    )
    leaks = []
    for name, data in parts.items():
        text = decode_text(data)
        if text is None:
            continue
        for match in words.finditer(text):
            # Masked: the report of a leak must not repeat it.
            leaks.append(f"{name} at {match.start()}: {match.group(0)[0]}… ({len(match.group(0))})")
    if leaks:
        raise TwinError("Person values appear in the twin:\n  " + "\n  ".join(leaks[:20]))


def verify(source: Path, twin: Path) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(twin) as copy:
        before, after = original.infolist(), copy.infolist()
        if [e.filename for e in before] != [e.filename for e in after]:
            raise TwinError("The twin's member order differs from the source")
        for old, new in zip(before, after, strict=True):
            expected = zipfile.ZIP_STORED if old.filename == "DataModel" else old.compress_type
            if new.compress_type != expected or new.date_time != old.date_time:
                raise TwinError(f"{old.filename}: compression or timestamp changed")
            if old.filename == "DataModel":
                continue
            old_data, new_data = original.read(old), copy.read(new)
            if old.filename.endswith(".json") or old.filename in _JSON_ROOT_PARTS:
                old_text, new_text = decode_text(old_data), decode_text(new_data)
                if old_text is None or new_text is None:
                    raise TwinError(f"{old.filename}: not text")
                if shape(json.loads(old_text)) != shape(json.loads(new_text)):
                    raise TwinError(f"{old.filename}: JSON structure changed")
                if (b"\x00" in old_data[:16]) != (b"\x00" in new_data[:16]):
                    raise TwinError(f"{old.filename}: encoding changed")
                if old_data.startswith(codecs.BOM_UTF8) != new_data.startswith(codecs.BOM_UTF8):
                    raise TwinError(f"{old.filename}: byte order mark changed")
    if twin.stat().st_size > MAX_TWIN_BYTES:
        raise TwinError(f"The twin is {twin.stat().st_size} bytes, above the 1 MiB limit")


def make_twin(source: Path, out: Path) -> dict[str, Any]:
    replaced: dict[str, str] = {}
    plan: list[tuple[zipfile.ZipInfo, bytes, int | None]] = []
    unchanged = 0
    with zipfile.ZipFile(source) as archive:
        for entry in archive.infolist():
            if entry.filename == "DataModel":
                # Never read into memory: the real model is replaced wholesale.
                data, compression = twin_part(entry.filename, b"", replaced)
            else:
                original = archive.read(entry)
                data, compression = twin_part(entry.filename, original, replaced)
                unchanged += data == original
            plan.append((entry, data, compression))
    check_no_person_values(
        {e.filename: d for e, d, _ in plan if e.filename not in {"DataModel", "SecurityBindings"}},
        person_values(source),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(out.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as twin:
            for entry, data, compression in plan:
                info = zipfile.ZipInfo(entry.filename, date_time=entry.date_time)
                info.compress_type = entry.compress_type if compression is None else compression
                info.external_attr = entry.external_attr
                info.create_system = entry.create_system
                twin.writestr(info, data)
        verify(source, temporary)
        temporary.replace(out)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "parts": len(plan),
        "unchanged": unchanged,
        "scrubbed_literals": len(replaced),
        "bytes": out.stat().st_size,
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if not args.source.is_file():
        print(f"No PBIX at {args.source}", file=sys.stderr)
        return 2
    try:
        summary = make_twin(args.source, args.out)
    except TwinError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    print(
        f"Wrote {args.out} ({summary['bytes']} bytes, sha256 {summary['sha256'][:16]}): "
        f"{summary['parts']} parts, {summary['unchanged']} byte-identical, "
        f"{summary['scrubbed_literals']} literal(s) scrubbed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
