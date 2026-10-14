"""The contract script's pure helpers and its check logic, against fakes: no Desktop is started.

Real Desktop is exercised only by a human running ``evals/imported_edit/desktop_contract.py``;
these tests pin what the script computes from what Desktop would say: the screenshot sampler
(built from a synthetic PNG), the probe page it writes, the receipt Studio's readiness reads,
and how each check turns observations into pass/fail without ever forcing a process.
"""

from __future__ import annotations

import importlib.util
import json
import struct
import sys
import zlib
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import psutil
import pytest

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.studio import desktop_session, pbir_document, pbir_expr
from ai_powerbi_analyst.studio.desktop_session import (
    DesktopInstance,
    DesktopReadiness,
    DesktopSessionError,
    SaveResult,
)
from ai_powerbi_analyst.studio.pbix_package import PbixPackage

ROOT = Path(__file__).resolve().parents[2]
TWIN = ROOT / "fixtures" / "imported" / "structure-twin.pbix"


def _load() -> ModuleType:
    path = ROOT / "evals" / "imported_edit" / "desktop_contract.py"
    spec = importlib.util.spec_from_file_location("desktop_contract_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dc = _load()


# --- PNG ----


def _filter_row(kind: int, row: bytes, prev: bytes, bpp: int) -> bytes:
    out = bytearray()
    for i, value in enumerate(row):
        a = row[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        if kind == 0:
            predictor = 0
        elif kind == 1:
            predictor = a
        elif kind == 2:
            predictor = b
        elif kind == 3:
            predictor = (a + b) // 2
        else:
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            predictor = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
        out.append((value - predictor) & 255)
    return bytes(out)


def _png(width: int, height: int, color: int, rows: list[bytes], kinds: list[int]) -> bytes:
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    raw = bytearray()
    prev = bytes(width * channels)
    for row, kind in zip(rows, kinds, strict=True):
        raw += bytes([kind]) + _filter_row(kind, row, prev, channels)
        prev = row

    def chunk(name: bytes, body: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(name + body) & 0xFFFFFFFF)
        return struct.pack(">I", len(body)) + name + body + crc

    header = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


def test_png_round_trip_keeps_every_pixel() -> None:
    rows = [bytes([(x * 7 + y * 13) % 256 for x in range(3 * 5)]) for y in range(4)]
    image = dc.decode_png(dc.encode_png(5, 4, rows))
    assert (image.width, image.height) == (5, 4)
    assert list(image.rows) == rows
    assert image.pixel(2, 3) == tuple(rows[3][6:9])


@pytest.mark.parametrize("kind", [0, 1, 2, 3, 4])
def test_decode_png_undoes_every_scanline_filter(kind: int) -> None:
    rows = [bytes((x * 31 + y * 17 + 5) % 256 for x in range(3 * 6)) for y in range(5)]
    image = dc.decode_png(_png(6, 5, 2, rows, [kind] * 5))
    assert list(image.rows) == rows


def test_decode_png_mixes_filters_between_rows() -> None:
    rows = [bytes((x * 11 + y * 29) % 256 for x in range(3 * 4)) for y in range(5)]
    image = dc.decode_png(_png(4, 5, 2, rows, [1, 2, 3, 4, 0]))
    assert list(image.rows) == rows


def test_decode_png_drops_alpha_and_expands_grey() -> None:
    rgba = [bytes([10, 20, 30, 255, 40, 50, 60, 128]), bytes([70, 80, 90, 0, 1, 2, 3, 255])]
    image = dc.decode_png(_png(2, 2, 6, rgba, [0, 4]))
    assert list(image.rows) == [bytes([10, 20, 30, 40, 50, 60]), bytes([70, 80, 90, 1, 2, 3])]
    grey = [bytes([5, 200]), bytes([9, 9])]
    image = dc.decode_png(_png(2, 2, 0, grey, [2, 1]))
    assert list(image.rows) == [bytes([5, 5, 5, 200, 200, 200]), bytes([9, 9, 9, 9, 9, 9])]


def test_decode_png_rejects_what_it_cannot_read() -> None:
    with pytest.raises(ValueError, match="not a PNG"):
        dc.decode_png(b"GIF89a....")
    good = dc.encode_png(1, 1, [b"\x01\x02\x03"])
    with pytest.raises(ValueError):
        dc.decode_png(good[:-20])
    sixteen = good.replace(struct.pack(">IIBB", 1, 1, 8, 2), struct.pack(">IIBB", 1, 1, 16, 2))
    with pytest.raises(ValueError):
        dc.decode_png(sixteen)
    with pytest.raises(ValueError):
        dc.encode_png(2, 1, [b"\x00" * 3])


# --- regions ----


def _canvas(
    width: int, height: int, color: tuple[int, int, int] = (250, 250, 250)
) -> list[bytearray]:
    return [bytearray(bytes(color) * width) for _ in range(height)]


def _fill(
    rows: list[bytearray], x: int, y: int, w: int, h: int, color: tuple[int, int, int]
) -> None:
    for row in rows[y : y + h]:
        row[3 * x : 3 * (x + w)] = bytes(color) * w


def _image(rows: list[bytearray]) -> Any:
    return dc.decode_png(dc.encode_png(len(rows[0]) // 3, len(rows), [bytes(r) for r in rows]))


def _paint_label(red: int, green: int, blue: int) -> str | None:
    return "x" if (red, green, blue) == (255, 0, 0) else None


def test_find_regions_reports_boxes_areas_and_keeps_apart_regions_apart() -> None:
    rows = _canvas(40, 20)
    _fill(rows, 2, 3, 10, 5, (255, 0, 0))
    _fill(rows, 20, 10, 6, 6, (255, 0, 0))
    regions = sorted(dc.find_regions(_image(rows), _paint_label), key=lambda r: r.x0)
    assert [(r.x0, r.y0, r.x1, r.y1, r.area) for r in regions] == [
        (2, 3, 11, 7, 50),
        (20, 10, 25, 15, 36),
    ]
    assert regions[0].fill == 1.0 and regions[0].center == (7.0, 5.5)
    assert (regions[0].width, regions[0].height) == (10, 5)


def test_find_regions_joins_the_two_arms_of_a_u_shape() -> None:
    rows = _canvas(20, 10)
    _fill(rows, 2, 1, 3, 8, (255, 0, 0))
    _fill(rows, 10, 1, 3, 8, (255, 0, 0))
    _fill(rows, 2, 8, 11, 1, (255, 0, 0))  # the bottom joins them only after both arms started
    (region,) = dc.find_regions(_image(rows), _paint_label)
    assert (region.x0, region.y0, region.x1, region.y1) == (2, 1, 12, 8)
    assert region.area == 3 * 8 + 3 * 8 + 5
    assert region.fill < 0.9


def test_find_regions_treats_diagonal_neighbours_as_separate() -> None:
    rows = _canvas(10, 4)
    _fill(rows, 1, 1, 2, 2, (255, 0, 0))
    _fill(rows, 3, 3, 2, 1, (255, 0, 0))  # touches only at a corner
    assert len(dc.find_regions(_image(rows), _paint_label)) == 2


def test_find_regions_separates_labels() -> None:
    rows = _canvas(20, 6)
    _fill(rows, 0, 0, 5, 6, (255, 0, 0))
    _fill(rows, 5, 0, 5, 6, (0, 255, 0))
    labels = dc.find_regions(_image(rows), dc.classify_calibration)
    assert sorted((r.label, r.x0, r.x1) for r in labels) == [("green", 5, 9), ("red", 0, 4)]


@pytest.mark.parametrize(
    ("rgb", "label"),
    [
        ((213, 0, 0), "red"),
        ((0, 213, 0), "green"),
        ((0, 0, 213), "blue"),
        ((213, 213, 0), "yellow"),
        ((0, 213, 213), "cyan"),
        ((213, 0, 213), "magenta"),
        ((255, 0, 0), "red"),
        ((213, 0, 5), "red"),
        ((213, 213, 213), None),  # white
        ((0, 0, 0), None),  # black
        ((213, 100, 0), None),  # an in-between channel is not a pure color
        ((213, 213, 40), None),
        ((250, 200, 0), None),  # two on-channels that disagree
        ((32, 48, 64), None),
    ],
)
def test_classify_calibration(rgb: tuple[int, int, int], label: str | None) -> None:
    assert dc.classify_calibration(*rgb) == label


def test_no_theme_color_can_be_mistaken_for_a_calibration_color() -> None:
    gain = 213 / 255
    colors = dc.theme_slots(dc.PROBE_THEME).values()
    for hex_color in colors:
        for percent in dc.PROBE_PERCENTS:
            shaded = pbir_expr.shade(hex_color, percent)
            observed = tuple(round(v * gain) for v in dc._parse_hex(shaded))
            if hex_color in ("#FFFFFF", "#000000"):
                continue  # white and black are recognised as neither
            assert dc.classify_calibration(*observed) is None, (hex_color, percent)
    assert all(min(dc._parse_hex(c)) >= 30 for c in dc.PROBE_THEME["dataColors"])


def test_probe_theme_colors_are_all_distinct_enough_to_tell_apart() -> None:
    colors = list(dc.theme_slots(dc.PROBE_THEME).items())
    assert len(dc.PROBE_THEME["dataColors"]) == 12
    for i, (name_a, a) in enumerate(colors):
        for name_b, b in colors[i + 1 :]:
            assert dc._distance(a, b) > 12, (name_a, name_b)
    for slot in ("background", "foreground", "backgroundLight", "foregroundNeutralSecondary"):
        assert slot in dc.PROBE_THEME


# --- the screenshot sampler, on a synthetic Desktop screenshot ----


def _measured(theme: dict[str, Any], color_id: int, percent: float) -> str:
    colors = ["#FFFFFF", "#000000", *theme["dataColors"][:8]]
    if color_id >= len(colors):
        return str(theme["dataColors"][0])  # Desktop's fallback for an invalid id
    return pbir_expr.shade(colors[color_id], percent)


def _old_belief(theme: dict[str, Any], color_id: int, percent: float) -> str:
    colors = [theme["background"], theme["foreground"], *theme["dataColors"]]
    return pbir_expr.shade(colors[color_id], percent)


def _screenshot(
    color_of: Any,
    *,
    scale: float = 0.4,
    origin: tuple[int, int] = (24, 36),
    gain: float = 213 / 255,
    with_calibration: bool = True,
) -> Any:
    width, height = (
        int(dc.CANVAS[0] * scale) + 2 * origin[0],
        int(dc.CANVAS[1] * scale) + 2 * origin[1],
    )
    rows = _canvas(width, height, (236, 236, 236))  # Desktop's grey around the page
    for x in range(0, width, 40):  # a small yellow logo and a thin yellow rule: not tiles
        _fill(rows, x, 2, 3, 3, (213, 213, 0))
    _fill(rows, 100, 8, 60, 3, (213, 213, 0))
    _fill(rows, *origin, int(dc.CANVAS[0] * scale), int(dc.CANVAS[1] * scale), (0, 0, 0))
    tiles, calibration = dc.probe_tiles()

    def paint(tile: Any, hex_color: str) -> None:
        color = tuple(min(255, round(v * gain)) for v in dc._parse_hex(hex_color))
        x, y = origin[0] + round(tile.x * scale), origin[1] + round(tile.y * scale)
        _fill(rows, x, y, round(dc.TILE * scale), round(dc.TILE * scale), color)  # type: ignore[arg-type]

    theme = dc.PROBE_THEME
    for tile in tiles:
        paint(tile, color_of(theme, tile.color_id, tile.percent))
    if with_calibration:
        for tile in calibration:
            paint(tile, tile.literal)
    return _image(rows)


@pytest.fixture(scope="module")
def measured_result() -> dict[str, Any]:
    return dc.analyze_probe(_screenshot(_measured))  # type: ignore[no-any-return]


def test_sampler_recovers_the_gain_and_the_measured_palette(
    measured_result: dict[str, Any],
) -> None:
    result = measured_result
    assert result["gain"] == pytest.approx(213 / 255, abs=0.004)
    assert sorted(result["calibration_tiles_found"]) == sorted(dc.CALIBRATION)
    assert result["theme_color_ids_verified"] == list(range(10))
    mapping = result["mapping"]
    assert mapping["0"]["0"]["observed"] == "#FFFFFF"
    assert mapping["1"]["0"]["observed"] == "#000000"
    assert mapping["0"]["-0.4"]["observed"] == "#999999"  # white shaded 40% darker
    assert mapping["0"]["0.4"]["observed"] == "#FFFFFF"
    for color_id in range(2, 10):
        entry = mapping[str(color_id)]["0"]
        expected = dc.PROBE_THEME["dataColors"][color_id - 2]
        assert dc._distance(entry["observed"], expected) <= 2
        assert entry["slot"] == f"dataColors[{color_id - 2}]"
        assert entry["matches_expected"] is True


def test_sampler_names_the_slot_a_shaded_color_came_from(measured_result: dict[str, Any]) -> None:
    mapping = measured_result["mapping"]
    assert mapping["0"]["-0.4"]["slot"] == "fixed:white"
    assert mapping["1"]["0.4"]["observed"] == pbir_expr.shade("#000000", 0.4)
    assert mapping["1"]["0.4"]["slot"] == "fixed:black"
    assert mapping["9"]["0.4"]["slot"] == "dataColors[7]"
    assert mapping["4"]["-0.4"]["slot"] == "dataColors[2]"
    assert mapping["4"]["-0.4"]["expected"] == pbir_expr.shade(
        dc.PROBE_THEME["dataColors"][2], -0.4
    )


def test_sampler_reports_ids_past_nine_as_falling_back(measured_result: dict[str, Any]) -> None:
    for color_id in ("10", "11"):
        entry = measured_result["mapping"][color_id]["0"]
        assert entry["slot"] == "dataColors[0]"
        assert entry["expected"] is None
        assert entry["matches_expected"] is False
    assert 10 not in measured_result["theme_color_ids_verified"]


def test_sampler_tells_the_old_belief_from_what_desktop_drew() -> None:
    """If ColorId 0 and 1 were the theme's background and foreground, the probe would say so."""
    result = dc.analyze_probe(_screenshot(_old_belief, origin=(10, 20)))
    assert 0 not in result["theme_color_ids_verified"]
    assert 1 not in result["theme_color_ids_verified"]
    assert result["theme_color_ids_verified"] == list(range(2, 10))
    assert result["mapping"]["0"]["0"]["slot"] == "background"
    assert result["mapping"]["1"]["0"]["slot"] == "foreground"
    assert result["mapping"]["0"]["-0.4"]["slot"] == "background"


def test_sampler_works_at_another_scale_and_offset() -> None:
    result = dc.analyze_probe(_screenshot(_measured, scale=0.5, origin=(50, 11), gain=0.9))
    assert result["gain"] == pytest.approx(0.9, abs=0.005)
    assert result["theme_color_ids_verified"] == list(range(10))


def test_sampler_refuses_a_screenshot_without_the_calibration_tiles() -> None:
    with pytest.raises(ValueError, match="only 0 of 6 calibration tiles"):
        dc.analyze_probe(_screenshot(_measured, with_calibration=False))


def test_sampler_flags_where_pbir_expr_disagrees_with_desktop(
    measured_result: dict[str, Any],
) -> None:
    theme = dc.PROBE_THEME
    claims = []
    for entry in measured_result["pbir_expr_disagreements"]:
        assert set(entry) == {"color_id", "percent", "pbir_expr", "observed"}
        claims.append((entry["color_id"], entry["percent"]))
        assert entry["pbir_expr"] == pbir_expr.resolve_theme_color(
            theme, entry["color_id"], entry["percent"]
        )
    # Whatever pbir_expr currently believes, ids 2..9 are data colors in both models.
    assert all(color_id in (0, 1) for color_id, _ in claims)


# --- the probe page ----


def test_probe_layout_fits_the_page_and_never_overlaps() -> None:
    tiles, calibration = dc.probe_tiles()
    assert len(tiles) == 36 and len(calibration) == 6
    everything = [*tiles, *calibration]
    assert len({t.name for t in everything}) == 42
    assert all(len(t.name) == 20 for t in everything)
    boxes = [(t.x, t.y, t.x + dc.TILE, t.y + dc.TILE) for t in everything]
    assert all(
        x0 >= 0 and x1 <= dc.CANVAS[0] and y0 >= 0 and y1 <= dc.CANVAS[1]
        for x0, y0, x1, y1 in boxes
    )
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
    assert {(t.color_id, t.percent) for t in tiles} == {
        (i, p) for i in range(12) for p in (0, -0.4, 0.4)
    }


def test_build_probe_pbix_writes_the_page_theme_and_tiles(tmp_path: Path) -> None:
    source_hash = sha256_file(TWIN)
    out = tmp_path / "probe.pbix"
    layout = dc.build_probe_pbix(TWIN, out)
    assert sha256_file(TWIN) == source_hash  # the source is only read
    package = PbixPackage(out)
    document = pbir_document.PbirDocument(package)
    page = document.pages[dc.PROBE_PAGE]
    assert page.data["displayName"] == "Studio contract probe"
    assert (page.data["width"], page.data["height"]) == (1920, 1080)
    assert page.data["objects"]["background"][0]["properties"]["color"] == pbir_expr.color(
        "#000000"
    )
    assert len(page.visuals) == 42
    assert list(document.pages)[-1] == dc.PROBE_PAGE  # after the report's own page
    # the theme is registered as the custom theme and stored where the report looks for it
    assert document.report["themeCollection"]["customTheme"] == {
        "name": "StudioContractProbe.json",
        "reportVersionAtImport": {"visual": "1.8.87", "report": "2.0.87", "page": "1.3.87"},
        "type": "RegisteredResources",
    }
    location = document.custom_theme_location()
    assert location == (
        "RegisteredResources",
        "Report/StaticResources/RegisteredResources/StudioContractProbe.json",
    )
    assert document.theme()["dataColors"] == dc.PROBE_THEME["dataColors"]
    # one shape tile, exactly as a shape needs it: fill under the default state selector
    tile = page.visuals["probe03b".ljust(20, "0")]
    assert tile["visual"]["visualType"] == "shape"
    assert tile["visual"]["objects"]["fill"] == [
        {
            "properties": {
                "fillColor": {
                    "solid": {
                        "color": {"expr": {"ThemeDataColor": {"ColorId": 3, "Percent": -0.4}}}
                    }
                },
                "transparency": pbir_expr.double(0),
                "show": pbir_expr.boolean(True),
            },
            "selector": {"id": "default"},
        }
    ]
    assert tile["position"] == {
        "x": 40 + 3 * 130,
        "y": 40 + 140,
        "z": 1000,
        "height": 100,
        "width": 100,
        "tabOrder": 1000,
    }
    red = page.visuals["calred".ljust(20, "0")]
    assert red["visual"]["objects"]["fill"][0]["properties"]["fillColor"] == pbir_expr.color(
        "#FF0000"
    )
    assert layout["page"] == dc.PROBE_PAGE and len(layout["tiles"]) == 36
    assert layout["calibration"][0] == {
        "name": "calred".ljust(20, "0"),
        "hex": "#FF0000",
        "x": 200,
        "y": 520,
    }
    # the model and the rest of the report are untouched
    assert package.fingerprint("DataModel") == PbixPackage(TWIN).fingerprint("DataModel")
    assert set(PbixPackage(TWIN).names()) - {"SecurityBindings"} <= set(package.names())


def test_probe_writes_the_invalid_ids_10_and_11_that_pbir_expr_refuses(tmp_path: Path) -> None:
    # The probe exists to record what Desktop draws for ids it should not be given, so they
    # go in as raw expressions; ids 0..9 still go through the guarded helper.
    with pytest.raises(pbir_expr.ExpressionError):
        pbir_expr.theme_color(10, 0)
    assert dc.probe_fill(9, 0.4) == pbir_expr.theme_color(9, 0.4)
    assert dc.probe_fill(11, -0.4) == {
        "solid": {"color": {"expr": {"ThemeDataColor": {"ColorId": 11, "Percent": -0.4}}}}
    }
    out = tmp_path / "probe.pbix"
    dc.build_probe_pbix(TWIN, out)
    page = pbir_document.PbirDocument(PbixPackage(out)).pages[dc.PROBE_PAGE]
    for color_id, name in ((10, "probe10a"), (11, "probe11c")):
        tile = page.visuals[name.ljust(20, "0")]
        fill = tile["visual"]["objects"]["fill"][0]["properties"]["fillColor"]
        assert fill["solid"]["color"]["expr"]["ThemeDataColor"]["ColorId"] == color_id


def test_probe_pbix_can_be_built_twice_from_its_own_output(tmp_path: Path) -> None:
    once = tmp_path / "once.pbix"
    twice = tmp_path / "twice.pbix"
    dc.build_probe_pbix(TWIN, once)
    dc.build_probe_pbix(once, twice)
    document = pbir_document.PbirDocument(PbixPackage(twice))
    assert list(document.pages).count(dc.PROBE_PAGE) == 1
    items = document.report["resourcePackages"]
    registered = next(p for p in items if p["type"] == "RegisteredResources")
    assert [i["name"] for i in registered["items"]].count("StudioContractProbe.json") == 1


# --- literal and selector goldens ----


def test_add_literal_filters_spells_literals_the_way_desktop_does(tmp_path: Path) -> None:
    out = tmp_path / "literals.pbix"
    columns = {"decimal": ("Sales", "Unit Price"), "datetime": ("Calendar", "Date")}
    written = dc.add_literal_filters(TWIN, out, columns)
    assert written["written"] == {
        "studiocontractdec00001": "12.5M",
        "studiocontractdt000001": "datetime'2019-01-01T00:00:00'",
    }
    document = pbir_document.PbirDocument(PbixPackage(out))
    _, visual = document.visual(written["visual"])
    assert dc.filter_literals(visual) == {
        "Filterff831044ab8c98051120": None,  # the visual's own Advanced filter is kept
        **written["written"],
    }
    added = [f for f in visual["filterConfig"]["filters"] if f["name"] == "studiocontractdec00001"][
        0
    ]
    assert added == dc.categorical_filter("studiocontractdec00001", "Sales", "Unit Price", "12.5M")
    assert added["filter"]["Where"][0]["Condition"]["In"]["Values"] == [
        [{"Literal": {"Value": "12.5M"}}]
    ]
    assert added["filter"]["From"] == [{"Name": "t", "Entity": "Sales", "Type": 0}]
    # only what was asked for is written
    only = dc.add_literal_filters(TWIN, tmp_path / "one.pbix", {"datetime": ("Calendar", "Date")})
    assert list(only["written"]) == ["studiocontractdt000001"]


def test_filter_literals_tolerates_filters_it_does_not_understand() -> None:
    visual = {"filterConfig": {"filters": [{"name": "a"}, {"name": "b", "filter": {"Where": []}}]}}
    assert dc.filter_literals(visual) == {"a": None, "b": None}
    assert dc.filter_literals({}) == {}


def test_add_selected_selector_rewrites_the_existing_entry_and_keeps_the_others(
    tmp_path: Path,
) -> None:
    out = tmp_path / "selected.pbix"
    written = dc.add_selected_selector(TWIN, out, "#12ab34")
    assert written == {
        "page": "ReportSection8454c812b60b8bb83b99",
        "visual": "29ba010b20b2162d1536",
        "entry_existed": True,
        "written": "'#12AB34'",
    }
    document = pbir_document.PbirDocument(PbixPackage(out))
    _, visual = document.visual("29ba010b20b2162d1536")
    assert dc.selected_fill_literal(visual) == "'#12AB34'"
    entries = visual["visual"]["objects"]["fillCustom"]
    assert len(entries) == 3
    assert entries[0]["properties"]["transparency"] == pbir_expr.double(0)  # untouched sibling
    assert entries[2]["selector"] == {"id": "default"}


def test_add_selected_selector_adds_the_entry_when_there_is_none(tmp_path: Path) -> None:
    bare = tmp_path / "bare.pbix"
    package = PbixPackage(TWIN)
    document = pbir_document.PbirDocument(package)
    _, visual = document.visual("29ba010b20b2162d1536")
    visual["visual"]["objects"]["fillCustom"] = []
    document.touch_visual(document.visual("29ba010b20b2162d1536")[0], "29ba010b20b2162d1536")
    document.commit()
    package.write(bare)
    out = tmp_path / "added.pbix"
    written = dc.add_selected_selector(bare, out, "#000001")
    assert written is not None and written["entry_existed"] is False
    reread = pbir_document.PbirDocument(PbixPackage(out)).visual("29ba010b20b2162d1536")[1]
    assert reread["visual"]["objects"]["fillCustom"] == [
        {
            "properties": {"fillColor": pbir_expr.color("#000001")},
            "selector": {"id": "selection:selected"},
        }
    ]


def test_add_selected_selector_returns_none_without_an_advanced_slicer(tmp_path: Path) -> None:
    package = PbixPackage(TWIN)
    package.remove_part(
        "Report/definition/pages/ReportSection8454c812b60b8bb83b99/visuals/29ba010b20b2162d1536/visual.json"
    )
    plain = tmp_path / "plain.pbix"
    package.write(plain)
    assert dc.add_selected_selector(plain, tmp_path / "out.pbix", "#000001") is None
    assert not (tmp_path / "out.pbix").exists()


def test_measure_refs_and_reference_counts() -> None:
    def measure(entity: str, name: str) -> dict[str, Any]:
        return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}

    visual = {
        "query": {
            "Y": [
                {"field": measure("# Measures", "Sales"), "queryRef": "# Measures.Sales"},
                {"field": measure("# Measures", "Sales"), "queryRef": "# Measures.Sales"},
                {"field": measure("Other", "Margin"), "queryRef": "Other.Margin"},
            ]
        },
        "column": {"Column": {"Expression": {"SourceRef": {"Entity": "T"}}, "Property": "Sales"}},
    }
    assert dc.measure_refs(visual) == [("# Measures", "Sales"), ("Other", "Margin")]
    assert dc.count_measure_references(visual, "Sales") == {"fields": 2, "query_refs": 2}
    assert dc.count_measure_references(visual, "Margin") == {"fields": 1, "query_refs": 1}
    assert dc.count_measure_references(visual, "Nope") == {"fields": 0, "query_refs": 0}


def test_schema_versions_of_the_structure_twin() -> None:
    assert dc.schema_versions(PbixPackage(TWIN)) == {
        "page": ["2.1.0"],
        "pagesMetadata": ["1.1.0"],
        "report": ["3.3.0"],
        "versionMetadata": ["1.0.0"],
        "visualContainer": ["2.12.0"],
    }


def test_write_studio_copy_is_a_studio_style_revision(tmp_path: Path) -> None:
    out = tmp_path / "studio.pbix"
    dc.write_studio_copy(TWIN, out)
    package = PbixPackage(out)
    assert not package.has("SecurityBindings")
    assert package.read_json(dc.REPORT_PART) == PbixPackage(TWIN).read_json(dc.REPORT_PART)
    assert package.read(dc.REPORT_PART) == json.dumps(
        PbixPackage(TWIN).read_json(dc.REPORT_PART), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


# --- UI Automation summaries ----


def test_summaries_and_the_dont_save_search() -> None:
    dump = [
        {
            "type": "Window",
            "name": "Report",
            "className": "W",
            "enabled": False,
            "topLevel": True,
            "buttons": [{"id": "save", "name": "Save", "enabled": True}, {"id": "", "name": ""}],
        },
        {
            "type": "Document",
            "name": "Microsoft Power BI Desktop",
            "enabled": True,
            "topLevel": False,
            "buttons": [
                {"id": "", "name": "Save", "enabled": True},
                {"id": "", "name": "Don’t save", "enabled": True},
                {"id": "dontSaveBtn", "name": "", "enabled": True},
                {"id": "", "name": "Cancel", "enabled": True},
            ],
        },
    ]
    summary = dc._summarize_windows(dump)
    assert summary[0] == {
        "type": "Window",
        "name": "Report",
        "className": "W",
        "enabled": False,
        "topLevel": True,
        "button_ids": ["save"],
        "button_names": ["Save"],
    }
    assert summary[1]["button_names"] == ["Save", "Don’t save", "Cancel"]
    assert dc.find_dont_save(dump) == [
        {"id": "", "name": "Don’t save", "window": "Microsoft Power BI Desktop"},
        {"id": "dontSaveBtn", "name": "", "window": "Microsoft Power BI Desktop"},
    ]
    assert dc.find_dont_save([]) == []


# --- receipts ----


def _check(identifier: str, gate: bool, status: str) -> Any:
    return dc.Check(
        identifier, identifier, gate, status=status, expected={"x": 1}, observed={"x": 1}
    )


def test_receipt_passes_only_when_every_gate_passes() -> None:
    passing = [
        _check("a", True, "passed"),
        _check("b", False, "failed"),
        _check("c", True, "passed"),
    ]
    assert dc.receipt_passed(passing) is True  # a record-only check may differ
    assert dc.receipt_passed([*passing, _check("d", True, "error")]) is False
    assert dc.receipt_passed([_check("a", True, "skipped")]) is False
    assert dc.receipt_passed([_check("b", False, "passed")]) is False  # no gate ran at all
    assert dc.receipt_passed([]) is False


def test_receipt_content_and_file_name(tmp_path: Path) -> None:
    checks = [_check("01", True, "passed"), _check("02", False, "failed")]
    checks[0].seconds = 1.5
    checks[0].notes.append("n")
    started = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    receipt = dc.build_receipt(
        "2.157.1354.0",
        checks,
        {"bridge_manifest_hash": "abc", "left_open": []},
        partial=False,
        started=started,
        finished=started,
    )
    assert receipt["passed"] is True and receipt["partial"] is False
    assert receipt["desktop_version"] == "2.157.1354.0" and receipt["bridge_manifest_hash"] == "abc"
    assert receipt["checks"][0] == {
        "id": "01",
        "title": "01",
        "gate": True,
        "passed": True,
        "status": "passed",
        "expected": {"x": 1},
        "observed": {"x": 1},
        "seconds": 1.5,
        "notes": ["n"],
        "error": None,
    }
    assert receipt["checks"][1]["passed"] is False
    path = dc.write_receipt(receipt, tmp_path / "evidence")
    assert path == tmp_path / "evidence" / "desktop-2.157.1354.0.json"
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(json.dumps(receipt))
    assert [p.name for p in path.parent.iterdir()] == ["desktop-2.157.1354.0.json"]  # no temp files


def test_partial_runs_never_pass_and_use_another_file(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    receipt = dc.build_receipt(
        "2.157.1354.0", [_check("a", True, "passed")], {}, partial=True, started=now, finished=now
    )
    assert receipt["passed"] is False
    path = dc.write_receipt(receipt, tmp_path)
    assert path.name == "desktop-2.157.1354.0.partial.json"
    assert desktop_session.read_receipt("2.157.1354.0", tmp_path) is None  # readiness ignores it


def test_receipt_of_an_unreadable_version_goes_to_a_safe_name(tmp_path: Path) -> None:
    assert dc.receipt_path(tmp_path, None) == tmp_path / "desktop-unknown.json"
    assert dc.receipt_path(tmp_path, "..\\..\\evil") == tmp_path / "desktop-unknown.json"


def test_readiness_accepts_the_receipt_the_script_writes(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    receipt = dc.build_receipt(
        "2.157.1354.0", [_check("a", True, "passed")], {}, partial=False, started=now, finished=now
    )
    dc.write_receipt(receipt, tmp_path)
    found = desktop_session.read_receipt("2.157.1354.0", tmp_path)
    assert found is not None and found["passed"] is True
    failing = dc.build_receipt(
        "2.158.0.0", [_check("a", True, "failed")], {}, partial=False, started=now, finished=now
    )
    dc.write_receipt(failing, tmp_path)
    assert desktop_session.read_receipt("2.158.0.0", tmp_path)["passed"] is False  # type: ignore[index]


def test_run_check_records_an_exception_and_lets_the_next_check_run() -> None:
    def boom(check: Any) -> None:
        raise RuntimeError("Desktop vanished")

    check = dc.run_check(dc.Check("x", "boom", True), boom)
    assert check.status == "error" and check.error == "RuntimeError: Desktop vanished"
    assert check.passed is False and check.seconds >= 0

    def fine(check: Any) -> None:
        check.status = "passed"

    assert dc.run_check(dc.Check("y", "fine", True), fine).passed is True


# --- sessions and checks, against a fake controller ----


class FakeController:
    """Stands in for DesktopController: no process, no bridge."""

    def __init__(self, root: Path) -> None:
        self.bridge = ["node", "index.js"]
        self.pbi = "pbi"
        self.desktop_exe = "PBIDesktop.exe"
        self.root = root
        self.instances: dict[Path, DesktopInstance] = {}
        self.dirty: dict[int, bool] = {}
        self.calls: list[str] = []
        self.next_pid = 9000
        self.dirty_after_open = False
        self.save_changes = True
        self.close_error: DesktopSessionError | None = None
        self.op_errors: dict[str, DesktopSessionError] = {}
        self.blocking: list[str] = []
        self.saved_hook: Any = None

    def version(self) -> str:
        return "2.157.1354.0"

    def readiness(self) -> DesktopReadiness:
        return DesktopReadiness(True, True, True, False, "2.157.1354.0", ["not checked"])

    def launch_route(self) -> str:
        return "breakaway"

    def find(self, path: Path) -> DesktopInstance | None:
        return self.instances.get(path)

    def open(self, path: Path, *, timeout: float = 180, normalize: bool = True) -> DesktopInstance:
        assert normalize is False, "the contract script measures the raw behavior"
        self.next_pid += 1
        instance = DesktopInstance(self.next_pid, 1000.0 + self.next_pid, path)
        self.instances[path] = instance
        self.dirty[instance.pid] = self.dirty_after_open
        self.calls.append(f"open {path.name}")
        return instance

    def status(self, instance: DesktopInstance) -> dict[str, Any]:
        return {"pid": instance.pid, "hasUnsavedChanges": self.dirty[instance.pid]}

    def manifest(self, instance: DesktopInstance) -> dict[str, Any]:
        return {"manifest": {"methods": [{"name": "application.state.get/v1"}]}}

    def blocking_windows(self, instance: DesktopInstance) -> list[str]:
        return list(self.blocking)

    def save(self, instance: DesktopInstance, *, timeout: float = 90) -> SaveResult:
        self.calls.append("save")
        digest = sha256_file(instance.path)
        if self.save_changes:
            instance.path.write_bytes(instance.path.read_bytes() + b"x")
        self.dirty[instance.pid] = False
        if self.saved_hook:
            self.saved_hook(instance.path)
        return SaveResult(
            instance.path, sha256_file(instance.path), 4.2, self.save_changes and digest != ""
        )

    def close(self, instance: DesktopInstance, *, timeout: float = 60) -> None:
        self.calls.append("close")
        if self.close_error is not None:
            raise self.close_error
        del self.instances[instance.path]

    def apply_measure_operations(
        self, instance: DesktopInstance, operations: list[Any], tables: dict[str, str]
    ) -> list[str]:
        names = [op.name for op in operations]
        self.calls.append("ops " + ",".join(f"{op.op}:{op.name}" for op in operations))
        self.dirty[instance.pid] = True
        for name in names:
            if name in self.op_errors:
                raise self.op_errors[name]
        return [f"{op.op} [{op.name}]" for op in operations]


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(dc, "desktop_pids", lambda: {111})
    monkeypatch.setattr(dc.time, "sleep", lambda seconds: None)
    source = tmp_path / "scratch.pbix"
    source.write_bytes(TWIN.read_bytes())
    fake = FakeController(tmp_path)
    return dc.Session(fake, source, tmp_path / "work", tmp_path / "out")


def test_session_only_works_on_copies_of_the_scratch_file(session: Any) -> None:
    before = sha256_file(session.source)
    first, second = session.copy("a"), session.copy("b")
    assert first != second and first.parent == session.work and first.name == "01-a.pbix"
    assert sha256_file(first) == before
    session.launch(first)
    session.finish(session.launched[session.controller.instances[first].pid])
    assert sha256_file(session.source) == before


def test_session_refuses_a_desktop_that_was_already_running(session: Any) -> None:
    session.controller.next_pid = 110  # the fake's next instance gets the user's pid 111
    with pytest.raises(DesktopSessionError, match="already running"):
        session.launch(session.copy("a"))
    assert session.launched == {}


def test_session_refuses_a_file_a_desktop_already_has_open(session: Any) -> None:
    path = session.copy("a")
    session.controller.instances[path] = DesktopInstance(5, 1.0, path)
    with pytest.raises(DesktopSessionError, match="already has this scratch copy"):
        session.launch(path)


def test_session_records_the_bridge_manifest_hash_once(session: Any) -> None:
    first = session.launch(session.copy("a"))
    assert len(session.manifest_hash) == 64
    remembered = session.manifest_hash
    session.controller.manifest = lambda instance: {"manifest": {"different": True}}
    session.remember_manifest(first)
    assert session.manifest_hash == remembered


def test_session_will_not_touch_an_instance_it_did_not_launch(session: Any) -> None:
    foreign = DesktopInstance(4242, 5.0, session.work / "theirs.pbix")
    with pytest.raises(DesktopSessionError, match="did not launch"):
        session.own(foreign)
    with pytest.raises(DesktopSessionError, match="did not launch"):
        session.finish(foreign)
    assert session.controller.calls == []


def test_finish_saves_a_dirty_scratch_copy_then_closes_it(session: Any) -> None:
    instance = session.launch(session.copy("a"))
    session.controller.dirty[instance.pid] = True
    assert session.finish(instance) is True
    assert session.controller.calls[-2:] == ["save", "close"]
    assert session.launched == {} and session.left_open == []


def test_finish_leaves_a_desktop_open_instead_of_forcing_it(session: Any) -> None:
    instance = session.launch(session.copy("a"))
    session.controller.close_error = DesktopSessionError("Power BI Desktop is asking a question")
    assert session.finish(instance) is False
    assert session.left_open == [
        {
            "pid": instance.pid,
            "file": str(instance.path),
            "reason": "Power BI Desktop is asking a question",
        }
    ]
    assert instance.pid in session.launched  # still ours, still running
    assert session.controller.calls[-1] == "close"  # nothing was attempted after the refusal


def test_wait_for_flag_polls_until_it_flips(session: Any) -> None:
    instance = session.launch(session.copy("a"))
    polls = {"n": 0}

    def status(inst: DesktopInstance) -> dict[str, Any]:
        polls["n"] += 1
        return {"hasUnsavedChanges": polls["n"] >= 3}

    session.controller.status = status
    assert session.wait_for_flag(instance, True, 25) is not None
    assert polls["n"] == 3
    session.controller.status = lambda inst: {"hasUnsavedChanges": False}
    assert session.wait_for_flag(instance, True, 0.0) is None


def test_dirty_flag_check_compares_against_the_baseline(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.controller.dirty_after_open = False
    dirtied: set[str] = set()

    def status(inst: DesktopInstance) -> dict[str, Any]:
        return {"hasUnsavedChanges": inst.path.name in dirtied}

    def sleep(seconds: float) -> None:
        # Desktop dirties the Studio-written file during the 10 s wait, and only that one
        opened = [c for c in session.controller.calls if c.startswith("open")][-1]
        if "studio_written" in opened:
            dirtied.add(opened.removeprefix("open "))

    session.controller.status = status
    monkeypatch.setattr(dc.time, "sleep", sleep)
    check = dc.run_check(dc.Check("02", "dirty", False), lambda c: dc.check_dirty_flag(session, c))
    assert check.status == "passed", check.error
    observed = check.observed
    assert (
        observed["original"]["at_connect"] is False and observed["original"]["after_10s"] is False
    )
    assert observed["studio_written"]["at_connect"] is False
    assert observed["studio_written"]["after_10s"] is True
    assert session.left_open == []


def test_dirty_flag_check_fails_when_the_original_is_dirtied_too(session: Any) -> None:
    session.controller.status = lambda inst: {"hasUnsavedChanges": True}
    check = dc.run_check(dc.Check("02", "dirty", False), lambda c: dc.check_dirty_flag(session, c))
    assert check.status == "failed"
    assert check.observed["original"]["at_connect"] is True


_FACTS = dc.ModelFacts(
    measures={"Rank": {"table": "# Measures", "expression": "1"}},
    formats={"Rank": ""},
    cardinalities={"DimChannel.ChannelName": 7},
    row_counts={"DimChannel": 7},
    tables=["# Measures", "DimChannel"],
)


def test_uia_save_check_passes_on_a_real_change_and_records_timing(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dc, "read_model", lambda path: _FACTS)
    session.controller.dirty_after_open = True  # Desktop dirtied the file by itself
    check = dc.run_check(dc.Check("04", "save", True), lambda c: dc.check_uia_save(session, c))
    assert check.status == "passed", check.error
    assert check.observed["self_dirtied_after_s"] == 0.0
    assert check.observed["save_seconds"] == 4.2 and check.observed["changed"] is True
    assert check.observed["file_hash_changed"] is True and check.observed["flag_after"] is False
    assert not any(call.startswith("ops") for call in session.controller.calls)


def test_uia_save_check_dirties_with_a_measure_when_desktop_does_not(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dc, "read_model", lambda path: _FACTS)
    monkeypatch.setattr(session, "wait_for_flag", lambda *args: None)
    check = dc.run_check(dc.Check("04", "save", True), lambda c: dc.check_uia_save(session, c))
    assert check.status == "passed", check.error
    assert "ops create_measure:Studio contract save" in session.controller.calls
    assert any("throwaway measure" in note for note in check.notes)


def test_uia_save_check_fails_when_the_save_wrote_nothing(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dc, "read_model", lambda path: _FACTS)
    session.controller.dirty_after_open = True
    session.controller.save_changes = False
    check = dc.run_check(dc.Check("04", "save", True), lambda c: dc.check_uia_save(session, c))
    assert check.status == "failed"
    assert check.observed["changed"] is False


def _after(measure: dict[str, str] | None, *, cardinality: int = 7, rows: int = 7) -> Any:
    measures = dict(_FACTS.measures)
    formats = dict(_FACTS.formats)
    if measure is not None:
        measures["Studio contract measure"] = {"table": "# Measures", "expression": measure["e"]}
        formats["Studio contract measure"] = measure["f"]
    return dc.ModelFacts(
        measures,
        formats,
        {"DimChannel.ChannelName": cardinality},
        {"DimChannel": rows},
        _FACTS.tables,
    )


def _tom_run(
    session: Any, monkeypatch: pytest.MonkeyPatch, after: Any, *, reject: bool = True
) -> Any:
    reads = iter([_FACTS, after])
    monkeypatch.setattr(dc, "read_model", lambda path: next(reads))
    if reject:
        session.controller.op_errors["Studio contract measure broken"] = DesktopSessionError(
            "Power BI rejected the change: SUM(NoSuchTable[Nope])"
        )
    return dc.run_check(dc.Check("05", "tom", True), lambda c: dc.check_tom_roundtrip(session, c))


def test_tom_check_passes_when_the_measure_persists_and_the_model_is_unchanged(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _tom_run(session, monkeypatch, _after({"e": "2", "f": "0.00"}))
    assert check.status == "passed", (check.error, check.observed)
    assert check.observed["operations"] == [
        "create_measure [Studio contract measure]",
        "update_measure [Studio contract measure]",
    ]
    assert check.observed["measure_persisted"] == {"expression": "2", "format_string": "0.00"}
    assert check.observed["broken_measure_rejected_and_absent"] is True
    assert check.observed["cardinalities_unchanged"] and check.observed["row_counts_unchanged"]
    assert check.observed["columns_compared"] == 1
    assert session.controller.calls[-2:] == ["save", "close"]  # finish() found it clean
    assert any("rejected the broken measure" in note for note in check.notes)


@pytest.mark.parametrize(
    "after",
    [
        None,  # the measure did not survive the save
        {"e": "1", "f": "#,##0"},  # the update was lost
        {"e": "2", "f": "0.00"},  # persisted, but the model changed under it (below)
    ],
)
def test_tom_check_fails_when_anything_is_lost_or_changed(
    session: Any, monkeypatch: pytest.MonkeyPatch, after: dict[str, str] | None
) -> None:
    changed = after == {"e": "2", "f": "0.00"}
    check = _tom_run(session, monkeypatch, _after(after, cardinality=8 if changed else 7))
    assert check.status == "failed"
    if changed:
        assert check.observed["cardinalities_unchanged"] is False


def test_tom_check_fails_when_a_broken_measure_is_accepted(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _tom_run(session, monkeypatch, _after({"e": "2", "f": "0.00"}), reject=False)
    assert check.status == "failed"
    assert check.observed["broken_measure_rejected_and_absent"] is False


def test_tom_check_fails_when_row_counts_change(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _tom_run(session, monkeypatch, _after({"e": "2", "f": "0.00"}, rows=6))
    assert check.status == "failed" and check.observed["row_counts_unchanged"] is False


class _Answering:
    """The Desktop side of the close prompt: windows, dump and the process."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, session: Any) -> None:
        self.session = session
        self.posted: list[int] = []
        self.pressed: list[dict[str, str]] = []
        self.exits_on_answer = True
        self.dump_items: list[dict[str, Any]] = []
        self.answered = False
        monkeypatch.setattr(dc.desktop_uia, "windows", self.windows)
        monkeypatch.setattr(dc.desktop_uia, "dump", lambda pid: self.dump_items)
        monkeypatch.setattr(dc.desktop_uia, "invoke_any", self.invoke_any)
        monkeypatch.setattr(dc.desktop_session, "post_close", self.post_close)
        monkeypatch.setattr(dc.psutil, "Process", self.process)
        monkeypatch.setattr(dc, "read_model", lambda path: _FACTS)

    def windows(self, pid: int) -> list[dict[str, Any]]:
        enabled = not self.posted
        return [
            {
                "name": "Report",
                "className": "W",
                "enabled": enabled,
                "buttons": [{"id": "save", "name": "Save", "enabled": True}],
            }
        ]

    def post_close(self, pid: int) -> int:
        self.posted.append(pid)
        self.session.controller.blocking = ["a dialog in Power BI Desktop"]
        return 1

    def invoke_any(self, pid: int, automation_id: str = "", name: str = "") -> dict[str, Any]:
        self.pressed.append({"id": automation_id, "name": name})
        self.answered = True
        return {"id": automation_id, "name": name}

    def process(self, pid: int) -> Any:
        outer = self

        class Fake:
            def wait(self, timeout: float | None = None) -> None:
                if outer.answered and outer.exits_on_answer:
                    raise psutil.NoSuchProcess(pid)
                raise psutil.TimeoutExpired(timeout or 0)

        return Fake()


def test_close_prompt_check_answers_dont_save_when_it_is_reachable(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop = _Answering(monkeypatch, session)
    desktop.dump_items = [
        {
            "type": "Document",
            "name": "Microsoft Power BI Desktop",
            "enabled": True,
            "topLevel": False,
            "buttons": [{"id": "", "name": "Save"}, {"id": "", "name": "Don't save"}],
        }
    ]
    check = dc.run_check(
        dc.Check("03", "prompt", True), lambda c: dc.check_close_prompt(session, c)
    )
    assert check.status == "passed", (check.error, check.observed)
    assert desktop.pressed == [{"id": "", "name": "Don't save"}]
    assert check.observed["dont_save_reachable"] is True
    assert check.observed["dont_save_button"]["name"] == "Don't save"
    assert check.observed["blocking_windows_at_open"] == []
    assert check.observed["blocking_windows_after_close"] == ["a dialog in Power BI Desktop"]
    assert check.observed["windows_after_close"][0]["enabled"] is False
    assert session.left_open == [] and session.launched == {}


def test_close_prompt_check_leaves_the_instance_running_when_it_cannot_answer(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop = _Answering(monkeypatch, session)  # no buttons found anywhere
    check = dc.run_check(
        dc.Check("03", "prompt", True), lambda c: dc.check_close_prompt(session, c)
    )
    assert check.status == "passed", (check.error, check.observed)  # the prompt was still detected
    assert check.observed["dont_save_reachable"] is False
    assert check.observed["dont_save_candidates"] == []
    assert desktop.pressed == []
    assert len(session.left_open) == 1
    assert "'Save changes?' prompt could not be answered" in session.left_open[0]["reason"]
    assert any("NOT reachable" in note for note in check.notes)
    assert len(session.launched) == 1  # still running, still listed for the human
    assert "close" not in session.controller.calls  # and Studio's own close was never attempted


def test_close_prompt_check_keeps_the_instance_when_the_button_does_nothing(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop = _Answering(monkeypatch, session)
    desktop.exits_on_answer = False
    desktop.dump_items = [{"name": "prompt", "buttons": [{"id": "dontsave", "name": ""}]}]
    check = dc.run_check(
        dc.Check("03", "prompt", True), lambda c: dc.check_close_prompt(session, c)
    )
    assert desktop.pressed == [{"id": "dontsave", "name": ""}]
    assert check.observed["dont_save_reachable"] is False
    assert len(session.left_open) == 1
    assert any("stayed open" in note for note in check.notes)


def test_close_prompt_check_fails_when_no_dialog_is_ever_detected(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop = _Answering(monkeypatch, session)
    monkeypatch.setattr(desktop, "post_close", lambda pid: 1)  # WM_CLOSE changed nothing
    monkeypatch.setattr(dc.desktop_session, "post_close", desktop.post_close)
    check = dc.run_check(
        dc.Check("03", "prompt", True), lambda c: dc.check_close_prompt(session, c)
    )
    assert check.status == "failed"
    assert check.observed["blocking_windows_after_close"] == []


def test_close_prompt_check_fails_when_the_dirty_state_cannot_be_provoked(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop = _Answering(monkeypatch, session)
    session.controller.apply_measure_operations = lambda inst, ops, tables: []  # stays clean
    check = dc.run_check(
        dc.Check("03", "prompt", True), lambda c: dc.check_close_prompt(session, c)
    )
    assert check.status == "failed"
    assert desktop.posted == []  # nothing was closed
    assert "close" in session.controller.calls  # a clean instance is closed the normal way


def test_check_environment_records_versions_and_readiness(session: Any) -> None:
    check = dc.run_check(dc.Check("01", "env", True), lambda c: dc.check_environment(session, c))
    assert check.status == "passed", check.error
    observed = check.observed
    assert observed["desktop_version"] == "2.157.1354.0"
    assert observed["schema_versions"]["visualContainer"] == ["2.12.0"]
    assert observed["pbix_sha256"] == sha256_file(session.source)
    assert observed["readiness_without_receipt"] == ["not checked"]
    assert observed["launch_route"] == "breakaway"


def test_rename_check_skips_when_no_visual_uses_a_measure(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    unused = dc.ModelFacts({"Unused": {"table": "T", "expression": "1"}}, {}, {}, {}, ["T"])
    monkeypatch.setattr(dc, "read_model", lambda path: unused)  # no visual binds "Unused"
    check = dc.run_check(dc.Check("06", "rename", False), lambda c: dc.check_rename(session, c))
    assert check.status == "skipped"
    assert check.observed == {"reason": "no visual in the scratch file uses a measure"}
    assert session.controller.calls == []  # nothing was launched


def test_rename_check_reads_whether_desktop_rewrote_the_references(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = dc.ModelFacts(
        {"Rank Filtering Dynamics": {"table": "# Measures", "expression": "1"}},
        {},
        {},
        {},
        ["# Measures"],
    )
    monkeypatch.setattr(dc, "read_model", lambda path: facts)

    def desktop_renames(path: Path) -> None:
        # what a Desktop that rewrites references would leave behind
        package = PbixPackage(path)
        document = pbir_document.PbirDocument(package)
        for page, visual_id, visual in document.all_visuals():
            text = json.dumps(visual).replace(
                "Rank Filtering Dynamics", "Rank Filtering Dynamics Renamed"
            )
            visual.clear()
            visual.update(json.loads(text))
            document.touch_visual(page, visual_id)
        document.commit()
        package.write(path.with_suffix(".new"))
        path.with_suffix(".new").replace(path)

    session.controller.saved_hook = desktop_renames
    check = dc.run_check(dc.Check("06", "rename", False), lambda c: dc.check_rename(session, c))
    assert check.observed["measure"] == "Rank Filtering Dynamics"
    assert check.observed["references_before"]["old"] >= 1
    assert check.observed["references_after"]["old"] == 0
    assert check.observed["desktop_rewrote_references"] is True
    assert check.observed["outcome"] == "rewritten"
    assert check.status == "passed"  # either outcome is a finding, not a failure
    assert check.gate is False


def test_rename_check_passes_when_desktop_leaves_the_references_alone(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = dc.ModelFacts(
        {"Rank Filtering Dynamics": {"table": "# Measures", "expression": "1"}},
        {},
        {},
        {},
        ["# Measures"],
    )
    monkeypatch.setattr(dc, "read_model", lambda path: facts)
    check = dc.run_check(dc.Check("06", "rename", False), lambda c: dc.check_rename(session, c))
    assert check.status == "passed", check.error
    assert check.observed["outcome"] == "left_dangling"
    assert check.observed["references_after"]["old"] == check.observed["references_before"]["old"]
    assert "ops rename_measure:Rank Filtering Dynamics" in session.controller.calls


def test_literal_check_skips_a_model_without_decimal_or_datetime_columns(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dc.pbix_extract, "types", lambda path: {"columns": [{"dataType": 2}]})
    check = dc.run_check(
        dc.Check("09", "literals", False), lambda c: dc.check_literal_golden(session, c)
    )
    assert check.status == "skipped"


def test_literal_check_reads_back_what_desktop_kept(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = {
        "columns": [
            {"table": "Sales", "name": "Price", "dataType": 10, "hidden": False},
            {"table": "Calendar", "name": "Date", "dataType": 9, "hidden": False},
        ]
    }
    monkeypatch.setattr(dc.pbix_extract, "types", lambda path: columns)
    monkeypatch.setattr(dc, "read_model", lambda path: _FACTS)
    check = dc.run_check(
        dc.Check("09", "literals", False), lambda c: dc.check_literal_golden(session, c)
    )
    assert check.status == "passed", (check.error, check.observed)
    assert check.observed["kept"] is True
    assert check.observed["after_desktop_save"] == check.observed["written"]
    assert check.observed["columns"] == {
        "decimal": ["Sales", "Price"],
        "datetime": ["Calendar", "Date"],
    }


def test_literal_check_matches_filters_by_column_when_desktop_renames_them(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = {"columns": [{"table": "Sales", "name": "Price", "dataType": 10, "hidden": False}]}
    monkeypatch.setattr(dc.pbix_extract, "types", lambda path: columns)
    monkeypatch.setattr(dc, "read_model", lambda path: _FACTS)

    def rename(path: Path) -> None:
        package = PbixPackage(path)
        document = pbir_document.PbirDocument(package)
        page, visual_id, visual = document.all_visuals()[0]
        for item in visual["filterConfig"]["filters"]:
            item["name"] = "Filter" + item["name"][-8:]
        document.touch_visual(page, visual_id)
        document.commit()
        package.write(path.with_suffix(".new"))
        path.with_suffix(".new").replace(path)

    session.controller.saved_hook = rename
    check = dc.run_check(
        dc.Check("09", "literals", False), lambda c: dc.check_literal_golden(session, c)
    )
    assert check.status == "passed", (check.error, check.observed)
    assert check.observed["after_desktop_save"] == {"Sales.Price": "12.5M"}


def test_literal_check_fails_when_desktop_drops_a_filter(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = {"columns": [{"table": "Sales", "name": "Price", "dataType": 10, "hidden": False}]}
    monkeypatch.setattr(dc.pbix_extract, "types", lambda path: columns)
    monkeypatch.setattr(dc, "read_model", lambda path: _FACTS)

    def drop(path: Path) -> None:
        package = PbixPackage(path)
        document = pbir_document.PbirDocument(package)
        page, visual_id, visual = document.all_visuals()[0]
        visual["filterConfig"]["filters"] = []
        document.touch_visual(page, visual_id)
        document.commit()
        package.write(path.with_suffix(".new"))
        path.with_suffix(".new").replace(path)

    session.controller.saved_hook = drop
    check = dc.run_check(
        dc.Check("09", "literals", False), lambda c: dc.check_literal_golden(session, c)
    )
    assert check.status == "failed"
    assert check.observed["after_desktop_save"] == {"Sales.Price": "<dropped>"}


def test_goldens_do_not_pass_when_desktop_never_rewrote_the_file(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading back an untouched file would 'confirm' anything, so that is an error."""
    monkeypatch.setattr(dc, "read_model", lambda path: _FACTS)
    session.controller.save_changes = False
    check = dc.run_check(
        dc.Check("10", "selected", False), lambda c: dc.check_selected_selector(session, c)
    )
    assert check.status == "error"
    assert check.error == "DesktopSessionError: Desktop saved without rewriting the file"


def test_selection_check_survives_or_reports_the_loss(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dc, "read_model", lambda path: _FACTS)
    check = dc.run_check(
        dc.Check("10", "selected", False), lambda c: dc.check_selected_selector(session, c)
    )
    assert check.status == "passed", (check.error, check.observed)
    assert check.observed["survived"] is True and check.observed["written"] == "'#12AB34'"

    def strip(path: Path) -> None:
        package = PbixPackage(path)
        document = pbir_document.PbirDocument(package)
        page, visual_id, visual = (
            document.visual("29ba010b20b2162d1536")[0],
            "29ba010b20b2162d1536",
            document.visual("29ba010b20b2162d1536")[1],
        )
        visual["visual"]["objects"]["fillCustom"] = []
        document.touch_visual(page, visual_id)
        document.commit()
        package.write(path.with_suffix(".new"))
        path.with_suffix(".new").replace(path)

    session.controller.saved_hook = strip
    lost = dc.run_check(
        dc.Check("10", "selected", False), lambda c: dc.check_selected_selector(session, c)
    )
    assert lost.status == "failed" and lost.observed["after_desktop_save"] is None


def test_theme_probe_check_reads_the_bridge_screenshot(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = dc.encode_png(*_png_parts(_screenshot(_measured)))
    launched: list[str] = []

    def screenshot(instance: DesktopInstance, page_id: str, output: Path) -> Path:
        launched.append(page_id)
        output.write_bytes(png)
        return output

    session.controller.screenshot = screenshot
    check = dc.run_check(dc.Check("08", "probe", False), lambda c: dc.check_theme_probe(session, c))
    assert check.status == "passed", (check.error, check.observed)
    assert launched == [dc.PROBE_PAGE]
    assert check.observed["theme_color_ids_verified"] == list(range(10))
    assert check.observed["unverified_color_ids"] == [10, 11]
    assert check.observed["screenshot"] == "desktop-2.157.1354.0-theme-probe.png"
    assert (session.out / "desktop-2.157.1354.0-theme-probe.png").read_bytes() == png


def _png_parts(image: Any) -> tuple[int, int, list[bytes]]:
    return image.width, image.height, list(image.rows)


def test_main_writes_a_receipt_and_lists_desktops_left_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "scratch.pbix"
    source.write_bytes(TWIN.read_bytes())
    monkeypatch.setattr(dc, "desktop_pids", lambda: set())
    monkeypatch.setattr(dc, "DesktopController", lambda bridge, pbi, exe: FakeController(tmp_path))

    def gate(session: Any, check: Any) -> None:
        check.status = "passed"
        session.leave_open(
            DesktopInstance(77, 1.0, tmp_path / "x.pbix"), "cannot answer the prompt"
        )

    def record(session: Any, check: Any) -> None:
        check.status = "failed"

    monkeypatch.setattr(
        dc, "CHECKS", [("01_a", "first", True, gate), ("02_b", "second", False, record)]
    )
    out = tmp_path / "evidence"
    code = dc.main(
        [
            "--pbix", str(source), "--out", str(out), "--bridge", "node", "index.js",
            "--pbi", "pbi", "--desktop-exe", "PBIDesktop.exe",
        ]
    )  # fmt: skip
    text = capsys.readouterr().out
    receipt = json.loads((out / "desktop-2.157.1354.0.json").read_text(encoding="utf-8"))
    assert code == 0 and receipt["passed"] is True
    assert [c["status"] for c in receipt["checks"]] == ["passed", "failed"]
    assert receipt["left_open"] == [
        {"pid": 77, "file": str(tmp_path / "x.pbix"), "reason": "cannot answer the prompt"}
    ]
    assert "Close each by hand (nothing was forced)" in text
    assert "pid 77" in text and "cannot answer the prompt" in text


def test_main_fills_the_manifest_hash_into_check_01_and_finds_the_exe_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "scratch.pbix"
    source.write_bytes(TWIN.read_bytes())
    monkeypatch.setattr(dc, "desktop_pids", lambda: set())
    seen_exe: list[str] = []

    def controller(bridge: Any, pbi: Any, exe: str) -> FakeController:
        seen_exe.append(exe)
        return FakeController(tmp_path)

    monkeypatch.setattr(dc, "DesktopController", controller)
    monkeypatch.setattr(dc, "find_desktop_exe", lambda: r"C:\found\PBIDesktop.exe")

    def environment(session: Any, item: Any) -> None:
        session.manifest_hash = "abc123"  # what the first launch learns
        item.observed = {"bridge_manifest_hash": None}
        item.status = "passed"

    monkeypatch.setattr(dc, "CHECKS", [("01_environment", "env", True, environment)])
    out = tmp_path / "e"
    assert dc.main(["--pbix", str(source), "--out", str(out), "--bridge", "n", "--pbi", "p"]) == 0
    receipt = json.loads((out / "desktop-2.157.1354.0.json").read_text(encoding="utf-8"))
    assert receipt["checks"][0]["observed"]["bridge_manifest_hash"] == "abc123"
    assert receipt["bridge_manifest_hash"] == "abc123"
    assert seen_exe == [r"C:\found\PBIDesktop.exe"]


def test_find_desktop_exe_uses_the_override_then_preflight_never_a_folder_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "PBIDesktop.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("PBI_DESKTOP_PATH", str(exe))
    assert dc.find_desktop_exe() == str(exe)
    monkeypatch.delenv("PBI_DESKTOP_PATH")

    from ai_powerbi_analyst import preflight

    probe = SimpleNamespace(available=True, path=r"C:\Store\bin\PBIDesktop.exe")
    monkeypatch.setattr(preflight, "probe_powerbi_desktop", lambda: probe)
    assert dc.find_desktop_exe() == r"C:\Store\bin\PBIDesktop.exe"
    absent = SimpleNamespace(available=False, path=None)
    monkeypatch.setattr(preflight, "probe_powerbi_desktop", lambda: absent)
    assert dc.find_desktop_exe() is None


def test_main_keeps_its_copies_out_of_the_repository_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "scratch.pbix"
    source.write_bytes(TWIN.read_bytes())
    monkeypatch.setattr(dc, "desktop_pids", lambda: set())
    monkeypatch.setattr(dc, "DesktopController", lambda bridge, pbi, exe: FakeController(tmp_path))
    monkeypatch.setattr(dc.tempfile, "tempdir", str(tmp_path / "temp"))
    (tmp_path / "temp").mkdir()
    seen: list[Path] = []

    def check(session: Any, item: Any) -> None:
        seen.append(session.work)
        item.status = "passed"

    monkeypatch.setattr(dc, "CHECKS", [("01_a", "first", True, check)])
    args = ["--pbix", str(source), "--out", str(tmp_path / "e"), "--bridge", "n"]
    assert dc.main([*args, "--pbi", "p", "--desktop-exe", "d"]) == 0
    (work,) = seen
    assert work.parent == tmp_path / "temp" and work.name.startswith("studio-contract-")
    assert ROOT not in work.parents


def test_main_fails_when_a_gate_fails_and_ignores_receipts_for_subsets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "scratch.pbix"
    source.write_bytes(TWIN.read_bytes())
    monkeypatch.setattr(dc, "desktop_pids", lambda: set())
    monkeypatch.setattr(dc, "DesktopController", lambda bridge, pbi, exe: FakeController(tmp_path))

    def failing(session: Any, check: Any) -> None:
        check.status = "failed"

    monkeypatch.setattr(
        dc, "CHECKS", [("01_a", "first", True, failing), ("02_b", "second", True, failing)]
    )
    args = [
        "--pbix",
        str(source),
        "--out",
        str(tmp_path / "e"),
        "--bridge",
        "node",
        "--pbi",
        "p",
        "--desktop-exe",
        "d",
    ]
    assert dc.main(args) == 1
    assert (tmp_path / "e" / "desktop-2.157.1354.0.json").is_file()
    assert dc.main([*args, "--only", "2"]) == 1
    partial = json.loads(
        (tmp_path / "e" / "desktop-2.157.1354.0.partial.json").read_text(encoding="utf-8")
    )
    assert [c["id"] for c in partial["checks"]] == ["02_b"] and partial["partial"] is True


def test_main_refuses_to_run_without_its_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        dc.main(
            [
                "--pbix",
                str(tmp_path / "nope.pbix"),
                "--bridge",
                "n",
                "--pbi",
                "p",
                "--desktop-exe",
                "d",
            ]
        )
        == 2
    )
    assert "nope.pbix" in capsys.readouterr().err


def test_the_check_list_covers_the_contract_and_gates_what_model_edits_need() -> None:
    ids = [item[0] for item in dc.CHECKS]
    names = [
        "environment",
        "dirty_flag",
        "close_prompt",
        "uia_save",
        "tom_roundtrip",
        "rename",
        "launch_survival",
        "theme_color_probe",
        "literal_golden",
        "selection_selected",
    ]
    assert ids == [f"{i:02d}_{name}" for i, name in enumerate(names, start=1)]
    gates = {item[0] for item in dc.CHECKS if item[2]}
    assert gates == {
        "01_environment",
        "03_close_prompt",
        "04_uia_save",
        "05_tom_roundtrip",
        "07_launch_survival",
    }


def test_the_script_source_never_forces_a_desktop_to_end() -> None:
    source = (ROOT / "evals" / "imported_edit" / "desktop_contract.py").read_text(encoding="utf-8")
    assert "Stop-Process" not in source and ".kill(" not in source and ".terminate(" not in source
    lines = [line for line in source.splitlines() if "taskkill" in line]
    assert lines and all("helper" in line for line in lines)  # only ever the stand-in host


def test_windows_only_helpers_are_not_executed_on_import() -> None:
    assert hasattr(dc, "Job") and hasattr(dc, "_HELPER")
    assert "controller.launch(" in dc._HELPER and "launch_survives_parent" in dc._HELPER
    assert "time.sleep(600)" in dc._HELPER  # idles like a sidecar until it is ended
