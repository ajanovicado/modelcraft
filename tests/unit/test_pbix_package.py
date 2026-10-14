"""The lossless PBIX package: what it keeps, what it drops, and how it fails."""

from __future__ import annotations

import hashlib
import os
import warnings
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest
from pbix_factory import content_types, dump, make_pbix

from ai_powerbi_analyst.studio import pbix_package
from ai_powerbi_analyst.studio.pbix_package import (
    PackageError,
    PbixPackage,
    data_model_digest,
    decode_json,
)

TWIN = Path(__file__).resolve().parents[2] / "fixtures" / "imported" / "structure-twin.pbix"
TYPES_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _simple(tmp_path: Path, **kwargs: Any) -> Path:
    parts = {
        "Version": b"1",
        "Report/definition/report.json": dump({"a": 1}),
        "Report/StaticResources/RegisteredResources/logo.png": b"\x89PNG-old",
    }
    return make_pbix(tmp_path / "in.pbix", parts, **kwargs)


def _content_type_map(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("[Content_Types].xml").decode("utf-8-sig"))
    found = {}
    for element in root:
        kind = element.tag.removeprefix(TYPES_NS)
        key = element.get("Extension") or element.get("PartName") or ""
        found[f"{kind}:{key}"] = element.get("ContentType", "")
    return found


def _bad_name_zip(tmp_path: Path, bad: bytes) -> Path:
    """A zip with a member whose name the zipfile module would not let us write."""
    placeholder = b"Report/" + b"x" * (len(bad) - len(b"Report/"))
    path = tmp_path / "bad.pbix"
    make_pbix(path, {placeholder.decode(): b"{}"}, data_model=None, security=False)
    path.write_bytes(path.read_bytes().replace(placeholder, bad))
    return path


# --- the round trip ------------------------------------------------------------------------


def test_round_trip_of_the_twin_changes_nothing_but_security_bindings(tmp_path: Path) -> None:
    package = PbixPackage(TWIN)
    assert not package.changed
    out = tmp_path / "copy.pbix"
    written = package.write(out)
    assert written == _sha(out.read_bytes())
    with zipfile.ZipFile(TWIN) as before, zipfile.ZipFile(out) as after:
        old = [e for e in before.infolist() if e.filename != "SecurityBindings"]
        new = after.infolist()
        assert [e.filename for e in new] == [e.filename for e in old]
        assert [e.compress_type for e in new] == [e.compress_type for e in old]
        assert [e.date_time for e in new] == [e.date_time for e in old]
        for first, second in zip(old, new, strict=True):
            if first.filename == "[Content_Types].xml":
                continue
            assert before.read(first) == after.read(second), first.filename
        assert after.getinfo("DataModel").compress_type == zipfile.ZIP_STORED
        assert after.testzip() is None
    assert data_model_digest(TWIN) == data_model_digest(out)


def test_content_types_lose_only_the_security_bindings_override(tmp_path: Path) -> None:
    out = tmp_path / "copy.pbix"
    PbixPackage(TWIN).write(out)
    before, after = _content_type_map(TWIN), _content_type_map(out)
    assert "Override:/SecurityBindings" in before
    assert {k: v for k, v in before.items() if k != "Override:/SecurityBindings"} == after
    with zipfile.ZipFile(out) as archive:
        raw = archive.read("[Content_Types].xml")
    assert raw.startswith(b"\xef\xbb\xbf<?xml")


def test_unchanged_parts_keep_bytes_order_and_compression(tmp_path: Path) -> None:
    source = _simple(tmp_path)
    package = PbixPackage(source)
    package.write_json("Report/definition/report.json", {"a": 2})
    out = tmp_path / "out.pbix"
    package.write(out)
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(out) as after:
        assert [e.filename for e in after.infolist()] == [
            n for n in before.namelist() if n != "SecurityBindings"
        ]
        assert after.read("Version") == b"1"
        assert after.read("DataModel") == before.read("DataModel")
        assert after.getinfo("DataModel").compress_type == zipfile.ZIP_STORED
        assert after.getinfo("Version").date_time == before.getinfo("Version").date_time
        assert after.read("Report/definition/report.json") == b'{"a":2}'


# --- writing parts ------------------------------------------------------------------------


def test_write_json_is_compact_utf8_without_a_bom(tmp_path: Path) -> None:
    package = PbixPackage(_simple(tmp_path))
    package.write_json("Report/definition/report.json", {"caf\u00e9": [1, 2], "b": None})
    data = package.read("Report/definition/report.json")
    assert data == '{"caf\u00e9":[1,2],"b":null}'.encode()
    assert not data.startswith(b"\xef\xbb\xbf")


def test_write_part_replaces_an_existing_part_and_adds_a_new_one(tmp_path: Path) -> None:
    package = PbixPackage(_simple(tmp_path))
    package.write_part("Report/StaticResources/RegisteredResources/logo.png", b"NEW")
    package.write_part("Report/definition/pages/p1/page.json", b'{"name":"p1"}')
    assert package.changed
    assert package.has("Report/definition/pages/p1/page.json")
    assert package.names()[-1] == "Report/definition/pages/p1/page.json"
    out = tmp_path / "out.pbix"
    package.write(out)
    with zipfile.ZipFile(out) as archive:
        assert archive.read("Report/StaticResources/RegisteredResources/logo.png") == b"NEW"
        assert archive.read("Report/definition/pages/p1/page.json") == b'{"name":"p1"}'
        assert archive.getinfo("Report/definition/pages/p1/page.json").compress_type == (
            zipfile.ZIP_DEFLATED
        )
        order = archive.namelist()
    # Replaced parts stay where they were; new parts come after the originals.
    assert order.index("Report/StaticResources/RegisteredResources/logo.png") < order.index(
        "DataModel"
    )
    assert order[-1] == "Report/definition/pages/p1/page.json"


def test_a_new_extension_gets_a_default_and_a_known_one_does_not(tmp_path: Path) -> None:
    package = PbixPackage(_simple(tmp_path))
    package.write_part("Report/StaticResources/RegisteredResources/pic.webp", b"RIFF")
    package.write_part("Report/definition/extra.json", b"{}")
    out = tmp_path / "out.pbix"
    package.write(out)
    types = _content_type_map(out)
    assert types["Default:webp"] == ""
    assert list(types).count("Default:json") == 1
    assert "Override:/SecurityBindings" not in types


def test_remove_part_drops_the_part_and_its_override(tmp_path: Path) -> None:
    types = content_types(overrides=("Version", "DataModel", "SecurityBindings", "Extra"))
    source = make_pbix(tmp_path / "in.pbix", {"Version": b"1", "Extra": b"x"}, types=types)
    package = PbixPackage(source)
    package.remove_part("Extra")
    assert package.changed
    assert not package.has("Extra")
    with pytest.raises(KeyError):
        package.read("Extra")
    out = tmp_path / "out.pbix"
    package.write(out)
    with zipfile.ZipFile(out) as archive:
        assert "Extra" not in archive.namelist()
    assert "Override:/Extra" not in _content_type_map(out)


def test_a_removed_part_can_be_written_again(tmp_path: Path) -> None:
    package = PbixPackage(_simple(tmp_path))
    package.remove_part("Version")
    package.write_part("Version", b"2")
    assert package.read("Version") == b"2"
    assert package.names().count("Version") == 1


def test_removing_a_new_part_forgets_it(tmp_path: Path) -> None:
    package = PbixPackage(_simple(tmp_path))
    package.write_part("Report/definition/new.json", b"{}")
    package.remove_part("Report/definition/new.json")
    assert not package.has("Report/definition/new.json")
    assert not package.changed


def test_changed_is_false_for_a_fresh_package(tmp_path: Path) -> None:
    assert not PbixPackage(_simple(tmp_path)).changed


def test_is_modified_tracks_replaced_and_removed_parts(tmp_path: Path) -> None:
    package = PbixPackage(_simple(tmp_path))
    part = "Report/definition/report.json"
    assert not package.is_modified(part)
    package.write_json(part, {"a": 1})
    assert package.is_modified(part)
    package.remove_part(part)
    assert package.is_modified(part)
    assert not package.is_modified("DataModel")


@pytest.mark.parametrize("name", ["DataModel", "[Content_Types].xml", "SecurityBindings"])
def test_studio_never_writes_the_protected_parts(tmp_path: Path, name: str) -> None:
    package = PbixPackage(_simple(tmp_path))
    with pytest.raises(PackageError, match="never writes"):
        package.write_part(name, b"x")


@pytest.mark.parametrize("name", ["DataModel", "[Content_Types].xml"])
def test_studio_never_removes_the_data_model_or_content_types(tmp_path: Path, name: str) -> None:
    with pytest.raises(PackageError, match="never removes"):
        PbixPackage(_simple(tmp_path)).remove_part(name)


@pytest.mark.parametrize(
    "name",
    [
        "Other/new.json",
        "Report/../DataModel",
        "/Report/x.json",
        "Report\\x.json",
        "Report/a\x01b.json",
        "Report/x:y.json",
        "",
    ],
)
def test_new_part_names_are_validated(tmp_path: Path, name: str) -> None:
    with pytest.raises(PackageError):
        PbixPackage(_simple(tmp_path)).write_part(name, b"x")


def test_a_new_part_may_not_differ_from_an_existing_one_only_by_case(tmp_path: Path) -> None:
    package = PbixPackage(_simple(tmp_path))
    with pytest.raises(PackageError, match="only by case"):
        package.write_part("Report/definition/REPORT.json", b"{}")


def test_write_refuses_the_source_as_destination(tmp_path: Path) -> None:
    source = _simple(tmp_path)
    with pytest.raises(PackageError, match="new file"):
        PbixPackage(source).write(source)
    assert zipfile.is_zipfile(source)


# --- reading -----------------------------------------------------------------------------


def test_read_never_returns_the_data_model(tmp_path: Path) -> None:
    package = PbixPackage(_simple(tmp_path))
    with pytest.raises(PackageError, match="never read"):
        package.read("DataModel")
    with pytest.raises(PackageError, match="never read"):
        package.read_parts(["Version", "DataModel"])


def test_read_of_a_missing_part_is_a_key_error(tmp_path: Path) -> None:
    package = PbixPackage(_simple(tmp_path))
    with pytest.raises(KeyError):
        package.read("Nope")
    with pytest.raises(KeyError):
        package.read_parts(["Nope"])


def test_read_parts_reads_originals_and_replacements_in_one_call(tmp_path: Path) -> None:
    package = PbixPackage(_simple(tmp_path))
    package.write_part("Version", b"9")
    parts = package.read_parts(["Version", "Report/definition/report.json"])
    assert parts == {"Version": b"9", "Report/definition/report.json": b'{"a":1}'}
    assert package.read_parts([]) == {}


def test_data_model_digest_is_stable_and_none_without_a_model(tmp_path: Path) -> None:
    model = b"model-bytes" * 100
    source = _simple(tmp_path, data_model=model)
    assert data_model_digest(source) == _sha(model)
    assert data_model_digest(source) == data_model_digest(source)
    out = tmp_path / "out.pbix"
    package = PbixPackage(source)
    package.write_json("Report/definition/report.json", {"a": 3})
    package.write(out)
    assert data_model_digest(out) == _sha(model)
    thin = make_pbix(tmp_path / "thin.pbix", {"Version": b"1"}, data_model=None)
    assert data_model_digest(thin) is None


def test_fingerprint_comes_from_the_central_directory(tmp_path: Path) -> None:
    model = b"m" * 1000
    package = PbixPackage(_simple(tmp_path, data_model=model))
    crc, size = package.fingerprint("DataModel")
    assert size == 1000
    assert crc == zipfile.ZipFile(package.source).getinfo("DataModel").CRC
    package.write_part("Version", b"abc")
    assert package.fingerprint("Version")[1] == 3


def test_read_json_decodes_the_encodings_desktop_uses() -> None:
    value = {"k": "caf\u00e9"}
    assert decode_json(dump(value)) == value
    assert decode_json(dump(value, bom=True)) == value
    assert decode_json(dump(value, utf16=True)) == value
    assert decode_json(b"\xff\xfe" + dump(value, utf16=True)) == value
    with pytest.raises(PackageError, match="not valid JSON"):
        decode_json(b"{not json")
    with pytest.raises(PackageError, match="not valid JSON"):
        decode_json(b"\xff\xff\xff")


def test_deeply_nested_json_is_a_package_error_not_a_recursion_error() -> None:
    """A part nested 100000 levels is far below the size limit but blows the parser's stack."""
    with pytest.raises(PackageError, match="not valid JSON"):
        decode_json(b"[" * 100_000)
    with pytest.raises(PackageError, match="not valid JSON"):
        decode_json(b'{"a":' * 100_000)


# --- refusing bad input -----------------------------------------------------------------


def test_a_file_that_is_not_a_zip_is_refused_clearly(tmp_path: Path) -> None:
    path = tmp_path / "plain.pbix"
    path.write_bytes(b"this is not a zip file at all")
    with pytest.raises(
        PackageError, match=r"Choose a valid Power BI report .*encrypted or damaged"
    ):
        PbixPackage(path)


def test_a_missing_file_is_refused_clearly(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match="Choose a valid Power BI report"):
        PbixPackage(tmp_path / "missing.pbix")


def test_an_encrypted_zip_is_refused(tmp_path: Path) -> None:
    path = _simple(tmp_path)
    data = bytearray(path.read_bytes())
    central = data.index(b"PK\x01\x02")
    data[central + 8] |= 0x1  # general-purpose flag bit 0: the entry is encrypted
    path.write_bytes(bytes(data))
    with pytest.raises(PackageError, match="password protected"):
        PbixPackage(path)


def test_a_damaged_central_directory_is_refused(tmp_path: Path) -> None:
    path = _simple(tmp_path)
    path.write_bytes(path.read_bytes().replace(b"PK\x01\x02", b"XXXX", 1))
    with pytest.raises(PackageError, match="damaged"):
        PbixPackage(path)


@pytest.mark.parametrize(
    "bad",
    [
        b"Report/../../xx",
        b"/Report/absolute",
        # zipfile turns backslashes into slashes on Windows and keeps them on POSIX; either
        # way a backslash traversal must not get through.
        b"Report\\..\\..\\xx",
        b"Report/ctrl\x01char",
        b"C:/Windows/system",
    ],
)
def test_unsafe_component_names_are_refused_on_open(tmp_path: Path, bad: bytes) -> None:
    with pytest.raises(PackageError, match="invalid component path"):
        PbixPackage(_bad_name_zip(tmp_path, bad))


def test_names_that_differ_only_by_case_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "case.pbix"
    make_pbix(path, {"Report/a.json": b"{}", "Report/A.json": b"{}"}, data_model=None)
    with pytest.raises(PackageError, match="only by case"):
        PbixPackage(path)


def test_duplicate_names_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "dup.pbix"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        make_pbix(path, [("Report/a.json", b"{}"), ("Report/a.json", b"[]")], data_model=None)
    with pytest.raises(PackageError, match="duplicate"):
        PbixPackage(path)


def test_the_entry_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _simple(tmp_path)
    monkeypatch.setattr(pbix_package, "MAX_ENTRIES", 3)
    with pytest.raises(PackageError, match="too many components"):
        PbixPackage(source)


def test_the_report_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_pbix(tmp_path / "big.pbix", {"Report/a.json": b"x" * 200})
    monkeypatch.setattr(pbix_package, "MAX_REPORT_BYTES", 100)
    with pytest.raises(PackageError, match="exceeds the local editing limit"):
        PbixPackage(source)


def test_the_part_size_limit_applies_to_reads_and_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = PbixPackage(make_pbix(tmp_path / "big.pbix", {"Report/a.json": b"x" * 200}))
    monkeypatch.setattr(pbix_package, "MAX_PART_BYTES", 100)
    with pytest.raises(PackageError, match="Report/a.json exceeds"):
        package.read("Report/a.json")
    with pytest.raises(PackageError, match="Report/a.json exceeds"):
        package.read_parts(["Report/a.json"])
    with pytest.raises(PackageError, match="Report/b.json exceeds"):
        package.write_part("Report/b.json", b"y" * 101)


@pytest.mark.parametrize(
    "xml",
    [
        b'<?xml version="1.0"?><!DOCTYPE Types [<!ENTITY a "b">]><Types xmlns="x"/>',
        b'<?xml version="1.0"?><!doctype Types><Types xmlns="x"/>',
        b'<Types xmlns="x"><!ENTITY boom "x"></Types>',
    ],
)
def test_content_types_with_a_dtd_or_entity_are_refused(tmp_path: Path, xml: bytes) -> None:
    package = PbixPackage(make_pbix(tmp_path / "in.pbix", {"Version": b"1"}, types=xml))
    out = tmp_path / "out.pbix"
    with pytest.raises(PackageError, match="not a plain OPC manifest"):
        package.write(out)
    assert not out.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_oversized_or_malformed_content_types_are_refused(tmp_path: Path) -> None:
    big = b"<Types>" + b" " * (pbix_package.MAX_CONTENT_TYPES_BYTES + 1) + b"</Types>"
    for name, xml, message in (
        ("big", big, "not a plain OPC manifest"),
        ("broken", b"<Types><Default", "not valid XML"),
        ("latin", b"<Types>\xff</Types>", "not valid text"),
    ):
        package = PbixPackage(make_pbix(tmp_path / f"{name}.pbix", {"Version": b"1"}, types=xml))
        with pytest.raises(PackageError, match=message):
            package.write(tmp_path / f"{name}-out.pbix")
    assert list(tmp_path.glob("*.tmp")) == []


# --- durable, atomic writes ------------------------------------------------------------


def test_a_failed_write_leaves_no_temp_file_and_keeps_the_old_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = PbixPackage(_simple(tmp_path))
    out = tmp_path / "out.pbix"
    out.write_bytes(b"previous revision")

    def boom(*_: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(pbix_package.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        package.write(out)
    assert out.read_bytes() == b"previous revision"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.pbix", "out.pbix"]


def test_a_failure_while_building_leaves_no_temp_file(tmp_path: Path) -> None:
    source = make_pbix(tmp_path / "in.pbix", {"Version": b"1"}, types=b"<Types><Default")
    with pytest.raises(PackageError):
        PbixPackage(source).write(tmp_path / "sub" / "out.pbix")
    assert list((tmp_path / "sub").iterdir()) == []


def test_write_retries_a_transient_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = PbixPackage(_simple(tmp_path))
    real = os.replace
    calls: list[int] = []

    def flaky(source: Any, destination: Any) -> None:
        calls.append(1)
        if len(calls) <= 2:
            error = PermissionError(13, "Access is denied")
            error.winerror = 5  # type: ignore[attr-defined]
            raise error
        real(source, destination)

    monkeypatch.setattr(pbix_package.os, "replace", flaky)
    monkeypatch.setattr(pbix_package, "_RETRY_DELAY", 0.0)
    out = tmp_path / "out.pbix"
    digest = package.write(out)
    assert len(calls) == 3
    assert digest == _sha(out.read_bytes())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.pbix", "out.pbix"]


def test_write_gives_up_after_the_bounded_number_of_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = PbixPackage(_simple(tmp_path))
    calls: list[int] = []
    sleeps: list[float] = []

    def locked(source: Any, destination: Any) -> None:
        calls.append(1)
        error = PermissionError(13, "The process cannot access the file")
        error.winerror = 32  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(pbix_package.os, "replace", locked)
    monkeypatch.setattr(pbix_package.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError):
        package.write(tmp_path / "out.pbix")
    assert len(calls) == 20
    assert sleeps == [0.25] * 19
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.pbix"]


def test_other_permission_errors_are_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = PbixPackage(_simple(tmp_path))
    calls: list[int] = []

    def denied(source: Any, destination: Any) -> None:
        calls.append(1)
        error = PermissionError(13, "Privilege not held")
        error.winerror = 1314  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(pbix_package.os, "replace", denied)
    with pytest.raises(PermissionError):
        package.write(tmp_path / "out.pbix")
    assert calls == [1]


def test_write_flushes_to_disk_before_the_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = PbixPackage(_simple(tmp_path))
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        pbix_package.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))
    )
    monkeypatch.setattr(
        pbix_package.os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))
    )
    package.write(tmp_path / "out.pbix")
    assert events == ["fsync", "replace"]


def test_the_temp_file_is_created_next_to_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = PbixPackage(_simple(tmp_path))
    seen: list[Path] = []
    real = os.replace
    monkeypatch.setattr(
        pbix_package.os, "replace", lambda a, b: (seen.append(Path(a).parent), real(a, b))
    )
    destination = tmp_path / "deep" / "er" / "out.pbix"
    package.write(destination)
    assert seen == [destination.parent]


def test_the_returned_hash_is_of_the_file_at_the_destination(tmp_path: Path) -> None:
    package = PbixPackage(TWIN)
    out = tmp_path / "with space" / "out (edited).pbix"
    assert package.write(out) == _sha(out.read_bytes())
    again = tmp_path / "again.pbix"
    assert package.write(again) == _sha(again.read_bytes())
    # An untouched package writes the same bytes every time, so revisions de-duplicate by hash.
    assert _sha(out.read_bytes()) == _sha(again.read_bytes())
