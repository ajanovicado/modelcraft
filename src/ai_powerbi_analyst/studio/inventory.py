"""Safe local folder inventory and file-signature classification."""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import os
import stat
import zipfile
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Literal

from .contracts import SourceInventoryItem

_OLE = bytes.fromhex("D0CF11E0A1B11AE1")
_PARQUET = b"PAR1"
_ZIP = b"PK\x03\x04"
# ZIP packages that are never workbooks. Listing an 11 GB .zip's central directory
# took 11 s on every scan of a Downloads folder, stalling each workspace load.
_NON_WORKBOOK_ARCHIVES = {
    ".zip",
    ".pbix",
    ".pbit",
    ".docx",
    ".pptx",
    ".jar",
    ".whl",
    ".nupkg",
    ".vsix",
    ".apk",
}
DetectedFormat = Literal["csv", "xls", "xlsx", "xlsm", "parquet", "pbip", "unsupported"]


class InventoryError(RuntimeError):
    """A folder cannot be safely inventoried."""


def inventory_folder(folder: str | Path) -> list[SourceInventoryItem]:
    root = Path(folder).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise InventoryError("Selected path is not a folder")
    items: list[SourceInventoryItem] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories if not _skip_entry(current_path / name)]
        for name in files:
            path = current_path / name
            if _skip_entry(path):
                continue
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    continue
                info = resolved.stat()
                detected, warning = detect_format(resolved)
            except (OSError, ValueError):
                continue
            relative = resolved.relative_to(root).as_posix()
            item_hash = hashlib.sha256(relative.casefold().encode("utf-8")).hexdigest()[:20]
            items.append(
                SourceInventoryItem(
                    id=f"source-{item_hash}",
                    name=resolved.name,
                    relative_path=relative,
                    absolute_path=str(resolved),
                    size_bytes=info.st_size,
                    modified_at=datetime.fromtimestamp(info.st_mtime, tz=UTC),
                    detected_format=detected,
                    selectable=detected in {"csv", "xls", "xlsx", "xlsm", "parquet"},
                    warning=warning,
                )
            )
    return sorted(items, key=lambda item: item.relative_path.casefold())


def detect_format(path: str | Path) -> tuple[DetectedFormat, str | None]:
    source = Path(path)
    size = source.stat().st_size
    with source.open("rb") as stream:
        head = stream.read(16)
        if size >= 4:
            stream.seek(-4, os.SEEK_END)
            tail = stream.read(4)
        else:
            tail = b""
    extension = source.suffix.casefold()
    detected: DetectedFormat = "unsupported"
    if head.startswith(_OLE):
        detected = "xls"
    elif head.startswith(_PARQUET) and tail == _PARQUET:
        detected = "parquet"
    elif head.startswith(_ZIP):
        if extension not in _NON_WORKBOOK_ARCHIVES:
            try:
                with zipfile.ZipFile(source) as archive:
                    names = set(archive.namelist())
                    if "xl/workbook.xml" in names:
                        detected = "xlsm" if "xl/vbaProject.bin" in names else "xlsx"
            except (OSError, zipfile.BadZipFile):
                detected = "unsupported"
    elif extension == ".pbip":
        detected = "pbip"
    elif _looks_like_csv(source):
        detected = "csv"
    warning = None
    expected = {
        ".csv": "csv",
        ".xls": "xls",
        ".xlsx": "xlsx",
        ".xlsm": "xlsm",
        ".parquet": "parquet",
        ".pbip": "pbip",
    }.get(extension)
    if detected != "unsupported" and expected is not None and expected != detected:
        warning = (
            f"File extension says {expected.upper()}, signature says {detected.upper()}; "
            "signature will be used"
        )
    return detected, warning


def _looks_like_csv(path: Path) -> bool:
    with path.open("rb") as stream:
        sample = stream.read(131_072)
    if not sample:
        return False
    for encoding in _candidate_encodings(sample):
        try:
            text = codecs.getincrementaldecoder(encoding)().decode(
                sample,
                final=len(sample) < 131_072,
            )
        except UnicodeDecodeError:
            continue
        if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
            continue
        for delimiter in (",", "\t", ";", "|"):
            rows = [
                row for row in islice(csv.reader(io.StringIO(text), delimiter=delimiter), 20) if row
            ]
            widths = [len(row) for row in rows]
            if widths and min(widths) > 1 and max(widths) - min(widths) <= 1:
                return True
        # A one-column dimension is valid CSV too; extension disambiguates it
        # from arbitrary plain text after decoding and structural checks.
        if (
            path.suffix.casefold() == ".csv"
            and text.strip()
            and not any(delimiter in text for delimiter in (",", "\t", ";", "|"))
        ):
            return True
    return False


def csv_encoding(path: Path) -> str:
    with path.open("rb") as stream:
        sample = stream.read(131_072)
    for encoding in _candidate_encodings(sample):
        try:
            codecs.getincrementaldecoder(encoding)().decode(sample, final=len(sample) < 131_072)
            return encoding
        except UnicodeDecodeError:
            continue
    raise InventoryError(f"Could not determine CSV encoding for {path.name}")


def _candidate_encodings(sample: bytes) -> tuple[str, ...]:
    if sample.startswith(b"\xef\xbb\xbf"):
        return ("utf-8-sig",)
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return ("utf-16",)
    if b"\x00" in sample[:256]:
        return ("utf-16", "utf-8", "cp1252")
    return ("utf-8", "cp1252")


def _skip_entry(path: Path) -> bool:
    name = path.name
    if name.startswith(".") or name.casefold() in {"$recycle.bin", "system volume information"}:
        return True
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            return True
        attributes = int(getattr(info, "st_file_attributes", 0))
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        hidden = int(getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2))
        system = int(getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4))
        return bool(attributes & (reparse | hidden | system))
    except OSError:
        return True
