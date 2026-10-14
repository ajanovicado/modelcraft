from __future__ import annotations

from pathlib import Path

from ai_powerbi_analyst.studio.inventory import csv_encoding, detect_format, inventory_folder


def test_single_column_dimension_is_csv_and_detection_never_reads_whole_file(tmp_path, monkeypatch):
    source = tmp_path / "regions.csv"
    source.write_text("Region\nEast\nWest\n", encoding="utf-8")

    def reject_full_read(*_args, **_kwargs):
        raise AssertionError("Inventory must use bounded reads")

    monkeypatch.setattr(Path, "read_bytes", reject_full_read)
    assert detect_format(source) == ("csv", None)
    assert csv_encoding(source) == "utf-8"


def test_utf8_sample_boundary_does_not_fall_back_to_windows_encoding(tmp_path):
    source = tmp_path / "boundary.csv"
    source.write_bytes(b"Field\n" + b"a" * (131071 - 6) + "é\n".encode())
    assert csv_encoding(source) == "utf-8"


def test_quoted_csv_delimiters_are_not_mistaken_for_extra_columns(tmp_path):
    source = tmp_path / "quoted.csv"
    source.write_text('Name,Sales\n"Last, First, Extra",100\nPlain,200\n', encoding="utf-8")
    assert detect_format(source) == ("csv", None)


def test_signature_detection_routes_mislabeled_ole_to_xls(tmp_path: Path) -> None:
    source = tmp_path / "Superstore.csv"
    source.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"not-a-real-workbook")

    detected, warning = detect_format(source)

    assert detected == "xls"
    assert warning == "File extension says CSV, signature says XLS; signature will be used"
    item = inventory_folder(tmp_path)[0]
    assert item.selectable
    assert item.detected_format == "xls"


def test_archives_are_not_listed_but_mislabeled_workbooks_still_are(tmp_path, monkeypatch):
    import zipfile

    workbook = tmp_path / "orders.csv"
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    archive_path = tmp_path / "backup.zip"
    archive_path.write_bytes(workbook.read_bytes())

    assert detect_format(workbook)[0] == "xlsx"
    opened: list[str] = []
    original = zipfile.ZipFile

    def tracking_zip(file, *args, **kwargs):
        opened.append(Path(file).name)
        return original(file, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", tracking_zip)
    assert detect_format(archive_path) == ("unsupported", None)
    assert opened == []


def test_inventory_skips_hidden_directories_and_detects_parquet_signature(
    tmp_path: Path,
) -> None:
    visible = tmp_path / "facts.bin"
    visible.write_bytes(b"PAR1payloadPAR1")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    items = inventory_folder(tmp_path)

    assert [item.relative_path for item in items] == ["facts.bin"]
    assert items[0].detected_format == "parquet"
