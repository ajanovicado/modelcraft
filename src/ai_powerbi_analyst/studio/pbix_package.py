"""Read a PBIX package and write an edited copy, changing only what Studio changed.

Desktop 2.157 opened PBIX files whose Report/definition JSON Studio rewrote. Three
scratch experiments (2026-09-28) showed it:
- exp1 changed a title, a bar color and the page background;
- exp3 deleted a visual, added a card and registered a custom theme;
- exp4 added a chart that took the theme's first data color.

SecurityBindings did not matter either way: Desktop writes a fresh one when it saves.
The DataModel is an XPress9 ABF backup that only Desktop can change, so it is streamed
byte for byte and stays STORED. A Desktop save rewrites the whole file and deletes
resources nothing references. That is why edits produce whole-file revisions, not patches.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
import zlib
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

MAX_ENTRIES = 20_000
MAX_PART_BYTES = 32 * 1024**2  # any part Studio reads into memory
MAX_REPORT_BYTES = 64 * 1024**2  # every Report/ part together
MAX_CONTENT_TYPES_BYTES = 256 * 1024
# Windows reports WinError 5 (access denied) or 32 (sharing violation) for a moment while a
# scanner, the indexer or a previous handle still holds the destination. The same idea as
# report_revisions._retry, kept private here so the package layer has no upward import.
_RETRY_WINERRORS = frozenset({5, 32})
_RETRY_ATTEMPTS = 20
_RETRY_DELAY = 0.25
_CONTENT_TYPES = "[Content_Types].xml"
_DATA_MODEL = "DataModel"
_SECURITY = "SecurityBindings"
_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
# Part names Studio may create: Report/ paths of plain characters, no traversal.
_NEW_PART = re.compile(r"Report/[A-Za-z0-9 _.()%\-]+(?:/[A-Za-z0-9 _.()%\-]+)*")


class PackageError(ValueError):
    """The PBIX cannot be read or written safely."""


def _check_name(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or ":" in path.parts[0]
        or len(name) > 400
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise PackageError(f"The report contains an invalid component path: {name!r}")


def decode_json(data: bytes) -> Any:
    """Parse a JSON part as Desktop wrote it: UTF-8 (with or without a BOM) or UTF-16LE."""
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-16-le" if b"\x00" in data[:16] else "utf-8-sig")
        return json.loads(text)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        # A part nested thousands of levels deep is well under the size limit but exhausts
        # the parser's stack; it must fail like any other damaged part, not crash the caller.
        raise PackageError("A report component is not valid JSON") from error


class PbixPackage:
    """A PBIX opened for editing: reads parts lazily, records replacements and removals.

    Nothing is written until ``write``; the source file is never modified.
    """

    def __init__(self, source: Path) -> None:
        self.source = Path(source)
        if not self.source.is_file():
            raise PackageError("Choose a valid Power BI report (.pbix)")
        # A sensitivity-labelled or password-protected PBIX is not a zip at all.
        if not zipfile.is_zipfile(self.source):
            raise PackageError(
                "Choose a valid Power BI report (.pbix). This file is not a readable report; "
                "it may be encrypted or damaged."
            )
        try:
            with zipfile.ZipFile(self.source) as archive:
                self._entries = archive.infolist()
        except (zipfile.BadZipFile, OSError) as error:
            raise PackageError("The report file is damaged and cannot be read") from error
        if len(self._entries) > MAX_ENTRIES:
            raise PackageError("The report archive has too many components")
        if any(e.flag_bits & 0x1 for e in self._entries):
            raise PackageError("The report is password protected, so Studio cannot edit it")
        names = [e.filename for e in self._entries]
        if len(set(names)) != len(names):
            raise PackageError("The report archive has duplicate components")
        folded = [n.casefold() for n in names]
        if len(set(folded)) != len(folded):
            raise PackageError("The report archive has components that differ only by case")
        for name in names:
            _check_name(name)
        report = sum(e.file_size for e in self._entries if e.filename.startswith("Report/"))
        if report > MAX_REPORT_BYTES:
            raise PackageError("The report definition exceeds the local editing limit")
        self._by_name = {e.filename: e for e in self._entries}
        self._replaced: dict[str, bytes] = {}
        self._removed: set[str] = set()

    # --- reading ---------------------------------------------------------------------

    def names(self) -> list[str]:
        """Current part names: originals in order, minus removals, plus new parts."""
        kept = [n for n in self._by_name if n not in self._removed]
        return kept + [n for n in self._replaced if n not in self._by_name]

    def has(self, name: str) -> bool:
        return name in self._replaced or (name in self._by_name and name not in self._removed)

    def read(self, name: str) -> bytes:
        if name in self._replaced:
            return self._replaced[name]
        if name in self._removed or name not in self._by_name:
            raise KeyError(name)
        if name == _DATA_MODEL:
            raise PackageError("The data model is copied, never read into memory")
        with zipfile.ZipFile(self.source) as archive:
            return self._read_entry(archive, name)

    def read_parts(self, names: Iterable[str]) -> dict[str, bytes]:
        """Several parts with one pass over the central directory (``read`` reopens the file)."""
        found: dict[str, bytes] = {}
        wanted = list(names)
        if not wanted:
            return found
        with zipfile.ZipFile(self.source) as archive:
            for name in wanted:
                if name in self._replaced:
                    found[name] = self._replaced[name]
                elif name in self._removed or name not in self._by_name:
                    raise KeyError(name)
                else:
                    found[name] = self._read_entry(archive, name)
        return found

    def _read_entry(self, archive: zipfile.ZipFile, name: str) -> bytes:
        if name == _DATA_MODEL:
            raise PackageError("The data model is copied, never read into memory")
        entry = self._by_name[name]
        if entry.file_size > MAX_PART_BYTES:
            raise PackageError(f"{name} exceeds the local editing limit")
        # The header's size is untrusted: a zip bomb can lie, so the read itself is bounded.
        with archive.open(entry) as reader:
            data = reader.read(MAX_PART_BYTES + 1)
        if len(data) > MAX_PART_BYTES:
            raise PackageError(f"{name} exceeds the local editing limit")
        return data

    def read_json(self, name: str) -> Any:
        return decode_json(self.read(name))

    def fingerprint(self, name: str) -> tuple[int, int]:
        """(crc32, size) of a part without decompressing it.

        For an untouched part this comes from the central directory, so even a
        multi-gigabyte DataModel costs nothing; a part Studio replaced is measured.
        """
        if name in self._replaced:
            data = self._replaced[name]
            return zlib.crc32(data), len(data)
        if name in self._removed or name not in self._by_name:
            raise KeyError(name)
        entry = self._by_name[name]
        return entry.CRC, entry.file_size

    # --- changing --------------------------------------------------------------------

    def write_part(self, name: str, data: bytes) -> None:
        _check_name(name)
        if name in (_DATA_MODEL, _CONTENT_TYPES, _SECURITY):
            raise PackageError(f"Studio never writes {name} directly")
        if name not in self._by_name and not _NEW_PART.fullmatch(name):
            raise PackageError(f"New components must live under Report/: {name!r}")
        existing = {n.casefold(): n for n in self.names()}
        if name.casefold() in existing and existing[name.casefold()] != name:
            raise PackageError(f"{name} differs from an existing component only by case")
        if len(data) > MAX_PART_BYTES:
            raise PackageError(f"{name} exceeds the local editing limit")
        self._replaced[name] = data
        self._removed.discard(name)

    def write_json(self, name: str, value: Any) -> None:
        """Compact UTF-8 without a BOM, as Desktop writes PBIR parts."""
        self.write_part(
            name, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

    def remove_part(self, name: str) -> None:
        if name in (_DATA_MODEL, _CONTENT_TYPES):
            raise PackageError(f"Studio never removes {name}")
        self._replaced.pop(name, None)
        if name in self._by_name:
            self._removed.add(name)

    @property
    def changed(self) -> bool:
        return bool(self._replaced or self._removed)

    def is_modified(self, name: str) -> bool:
        """True when this part was replaced or removed since the package was opened, so its
        bytes are no longer what the source file holds."""
        return name in self._replaced or name in self._removed

    # --- writing ---------------------------------------------------------------------

    def _content_types(self, names: list[str]) -> bytes:
        try:
            raw = self.read(_CONTENT_TYPES)
        except KeyError as error:
            raise PackageError("The report has no content-type list") from error
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise PackageError("The report's content-type list is not valid text") from error
        if (
            len(raw) > MAX_CONTENT_TYPES_BYTES
            or "<!DOCTYPE" in text.upper()
            or "<!ENTITY" in text.upper()
        ):
            raise PackageError("The report's content-type list is not a plain OPC manifest")
        ET.register_namespace("", _TYPES_NS)
        try:
            root = ET.fromstring(text)  # noqa: S314 - bounded input, DTD/entities rejected above
        except ET.ParseError as error:
            raise PackageError("The report's content-type list is not valid XML") from error
        tag = f"{{{_TYPES_NS}}}"
        present = {n.casefold() for n in names}
        for override in list(root.findall(f"{tag}Override")):
            part = override.get("PartName", "").lstrip("/")
            # Drop overrides for removed parts (SecurityBindings, deleted resources).
            if part and part.casefold() not in present:
                root.remove(override)
        defaults = {d.get("Extension", "").casefold() for d in root.findall(f"{tag}Default")}
        for name in names:
            suffix = PurePosixPath(name).suffix.lstrip(".").casefold()
            if suffix and suffix not in defaults and name.startswith("Report/"):
                element = ET.SubElement(root, f"{tag}Default")
                element.set("Extension", suffix)
                element.set("ContentType", "")
                defaults.add(suffix)
        body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
        declaration = '<?xml version="1.0" encoding="utf-8"?>'
        return b"\xef\xbb\xbf" + (declaration + body).encode("utf-8")

    def write(self, destination: Path) -> str:
        """Write the edited package to ``destination`` durably; returns the sha256 of the result.

        The bytes go to a temp file in the destination folder, are flushed and fsynced, and
        replace the destination in one ``os.replace`` (retried while Windows reports a
        transient sharing violation). The returned hash is of the file at ``destination``
        after the replace, so a caller that stores it can trust that file. A failure at any
        step removes the temp file and leaves an existing destination untouched.

        Every Studio-written revision drops SecurityBindings: it signs the previous
        content with the author's DPAPI key, and Desktop writes a new one on save.
        """
        destination = Path(destination)
        if destination.resolve() == self.source.resolve():
            raise PackageError("An edited report is always written to a new file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        removed = self._removed | {_SECURITY}
        names = [n for n in self.names() if n != _SECURITY]
        now = datetime.now().timetuple()[:6]
        handle, temporary = tempfile.mkstemp(
            prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
        )
        os.close(handle)
        try:
            with (
                zipfile.ZipFile(self.source) as source,
                zipfile.ZipFile(temporary, "w", allowZip64=True) as out,
            ):
                for entry in self._entries:
                    name = entry.filename
                    if name in removed:
                        continue
                    if name == _CONTENT_TYPES:
                        info = zipfile.ZipInfo(name, date_time=entry.date_time)
                        info.compress_type = entry.compress_type
                        out.writestr(info, self._content_types(names))
                    elif name in self._replaced:
                        info = zipfile.ZipInfo(name, date_time=now)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = entry.external_attr
                        out.writestr(info, self._replaced[name])
                    else:
                        info = zipfile.ZipInfo(name, date_time=entry.date_time)
                        info.compress_type = entry.compress_type
                        info.external_attr = entry.external_attr
                        with (
                            source.open(entry) as reader,
                            out.open(info, "w", force_zip64=entry.file_size > 0x7FFF0000) as writer,
                        ):
                            shutil.copyfileobj(reader, writer, 1024 * 1024)
                for name, data in self._replaced.items():
                    if name in self._by_name:
                        continue
                    info = zipfile.ZipInfo(name, date_time=now)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    out.writestr(info, data)
            _fsync(Path(temporary))
            _replace(Path(temporary), destination)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return _sha256(destination)


def _fsync(path: Path) -> None:
    """Force the file's bytes to disk before it becomes visible under its final name."""
    with path.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _replace(temporary: Path, destination: Path) -> None:
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            os.replace(temporary, destination)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in _RETRY_WINERRORS:
                raise
            if attempt == _RETRY_ATTEMPTS - 1:
                raise  # still locked after ~5 s: the caller reports it, the temp file is removed
            time.sleep(_RETRY_DELAY)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def data_model_digest(path: Path) -> str | None:
    """sha256 of the DataModel part: unchanged by Studio's report-layer edits."""
    with zipfile.ZipFile(path) as archive:
        try:
            entry = archive.getinfo(_DATA_MODEL)
        except KeyError:
            return None
        digest = hashlib.sha256()
        with archive.open(entry) as reader:
            for block in iter(lambda: reader.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
