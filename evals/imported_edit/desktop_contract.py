"""The live contract between Studio and the installed Power BI Desktop (design review P28).

Run it by hand, against a SCRATCH copy of a report, on a machine that has Power BI Desktop:

    .venv\\Scripts\\python.exe evals\\imported_edit\\desktop_contract.py ^
        --pbix C:\\scratch\\copy.pbix --out evals\\imported_edit\\evidence ^
        --bridge C:\\path\\node.exe C:\\path\\powerbi-desktop-bridge-cli\\dist\\index.js ^
        --pbi C:\\path\\pbi.exe --desktop-exe "C:\\Program Files\\WindowsApps\\...\\PBIDesktop.exe"

What it promises:
- The PBIX you give it is only read. Every check works on its own copy under ``--work-dir``
  (a fresh temp folder by default, so the report's data never lands in the repository).
- It only touches Desktop instances it launched itself (a pid that did not exist before it
  started, on a file it copied), and never one the user had open.
- It never ends a process by force. Every Desktop it launched is closed through
  ``DesktopController.close`` (save first when dirty). One it cannot close safely, for example
  because "Save changes?" cannot be answered through UI Automation, is left running and listed
  at the end for you to close by hand.
- One receipt is written: ``<out>/desktop-<Desktop version>.json``. ``passed`` there means every
  *gate* check passed; the studio reads it (``DesktopController.readiness``) to allow model edits
  for that Desktop version. Checks that only record a fact (dirty-flag timing, rename behavior,
  the colour table, literal spelling) are not gates; each still says whether it matched what the
  design assumes. ``--only`` runs a subset and writes ``.partial.json``, which is never read.

It needs about 10 Desktop launches (roughly 20 s each) and around five minutes. The pure helpers
(PNG decoding, pixel sampling, the probe page, receipts) are importable and unit-tested without
Desktop.
"""

from __future__ import annotations

import argparse
import colorsys
import contextlib
import copy
import ctypes
import functools
import hashlib
import importlib
import json
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zlib
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

_SRC = Path(__file__).resolve().parents[2] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ai_powerbi_analyst.studio import (  # noqa: E402 - after the source-checkout path fix above
    desktop_session,
    desktop_uia,
    pbir_document,
    pbir_expr,
    pbix_extract,
)
from ai_powerbi_analyst.studio.desktop_session import (  # noqa: E402
    DesktopController,
    DesktopInstance,
    DesktopSessionError,
    SaveResult,
)
from ai_powerbi_analyst.studio.pbix_package import PbixPackage  # noqa: E402
from ai_powerbi_analyst.studio.report_edits import (  # noqa: E402
    CreateMeasure,
    RenameMeasure,
    UpdateMeasure,
)

RECEIPT_VERSION = 1
REPORT_PART = "Report/definition/report.json"
PAGES_PART = "Report/definition/pages/pages.json"

# --- PNG: decode, encode, find solid regions ----------------------------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class Image:
    """An RGB image: one ``bytes`` of 3 * width per row."""

    width: int
    height: int
    rows: tuple[bytes, ...]

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        row = self.rows[y]
        return row[3 * x], row[3 * x + 1], row[3 * x + 2]


def _unfilter(kind: int, line: bytearray, prev: bytearray, bpp: int) -> None:
    if kind == 0:
        return
    if kind == 1:
        for i in range(bpp, len(line)):
            line[i] = (line[i] + line[i - bpp]) & 255
    elif kind == 2:
        for i in range(len(line)):
            line[i] = (line[i] + prev[i]) & 255
    elif kind == 3:
        for i in range(len(line)):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
    elif kind == 4:
        for i in range(len(line)):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
            predictor = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            line[i] = (line[i] + predictor) & 255
    else:
        raise ValueError(f"unknown PNG filter {kind}")


def decode_png(data: bytes) -> Image:
    """Decode an 8-bit, non-interlaced grey/RGB/RGBA PNG (what the bridge writes) to RGB."""
    if data[:8] != _PNG_SIGNATURE:
        raise ValueError("not a PNG file")
    position, header, compressed = 8, None, bytearray()
    while position + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[position : position + 8])
        body = data[position + 8 : position + 8 + length]
        if len(body) != length:
            raise ValueError("the PNG is truncated")
        position += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            compressed += body
        elif kind == b"IEND":
            break
    if header is None:
        raise ValueError("PNG has no header")
    width, height, depth, color, _, _, interlace = header
    if depth != 8 or interlace != 0 or color not in (0, 2, 4, 6):
        raise ValueError("only 8-bit non-interlaced grey, RGB and RGBA PNGs are supported")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    stride = width * channels
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as error:
        raise ValueError("the PNG's pixel data is damaged") from error
    if len(raw) != height * (stride + 1):
        raise ValueError("PNG pixel data has the wrong size")
    rows: list[bytes] = []
    previous = bytearray(stride)
    for y in range(height):
        base = y * (stride + 1)
        line = bytearray(raw[base + 1 : base + 1 + stride])
        _unfilter(raw[base], line, previous, channels)
        previous = line
        if color == 2:
            rows.append(bytes(line))
            continue
        rgb = bytearray(3 * width)
        if color == 6:
            rgb[0::3], rgb[1::3], rgb[2::3] = line[0::4], line[1::4], line[2::4]
        else:
            grey = line[0::channels]
            rgb[0::3], rgb[1::3], rgb[2::3] = grey, grey, grey
        rows.append(bytes(rgb))
    return Image(width, height, tuple(rows))


def encode_png(width: int, height: int, rows: Sequence[bytes]) -> bytes:
    """An RGB PNG from rows of 3 * width bytes (used to build test images)."""
    if len(rows) != height or any(len(row) != 3 * width for row in rows):
        raise ValueError("rows do not match the image size")

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@dataclass(frozen=True)
class Region:
    label: str
    x0: int
    y0: int
    x1: int  # inclusive
    y1: int
    area: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0 + 1

    @property
    def height(self) -> int:
        return self.y1 - self.y0 + 1

    @property
    def fill(self) -> float:
        return self.area / (self.width * self.height)

    @property
    def center(self) -> tuple[float, float]:
        return (self.x0 + self.x1 + 1) / 2, (self.y0 + self.y1 + 1) / 2


def find_regions(image: Image, classify: Callable[[int, int, int], str | None]) -> list[Region]:
    """Connected regions of pixels that ``classify`` gives the same label (4-connected)."""
    parent: list[int] = []
    boxes: list[list[int]] = []  # x0, y0, x1, y1, area per region id

    def root(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: int, b: int) -> None:
        ra, rb = root(a), root(b)
        if ra == rb:
            return
        parent[rb] = ra
        keep, drop = boxes[ra], boxes[rb]
        keep[0], keep[1] = min(keep[0], drop[0]), min(keep[1], drop[1])
        keep[2], keep[3] = max(keep[2], drop[2]), max(keep[3], drop[3])
        keep[4] += drop[4]

    cache: dict[tuple[int, int, int], str | None] = {}
    labels: dict[int, str] = {}
    previous: list[tuple[str, int, int, int]] = []
    for y, row in enumerate(image.rows):
        current: list[tuple[str, int, int, int]] = []
        run_label: str | None = None
        run_start = 0
        pixels = list(zip(row[0::3], row[1::3], row[2::3], strict=True))
        for x in range(len(pixels) + 1):
            label = None
            if x < len(pixels):
                pixel = pixels[x]
                if pixel in cache:
                    label = cache[pixel]
                else:
                    label = cache[pixel] = classify(*pixel)
            if label != run_label:
                if run_label is not None:
                    node = len(parent)
                    parent.append(node)
                    boxes.append([run_start, y, x - 1, y, x - run_start])
                    labels[node] = run_label
                    current.append((run_label, run_start, x - 1, node))
                run_label, run_start = label, x
        j = 0
        for run in current:
            name, x0, x1, node = run
            while j < len(previous) and previous[j][2] < x0:
                j += 1
            k = j
            while k < len(previous) and previous[k][1] <= x1:
                if previous[k][0] == name:
                    union(node, previous[k][3])
                k += 1
        previous = current
    found = []
    for node in range(len(parent)):
        if root(node) == node:
            x0, y0, x1, y1, area = boxes[node]
            found.append(Region(labels[node], x0, y0, x1, y1, area))
    return found


# --- the ThemeDataColor probe page ---------------------------------------------------------------

PROBE_PAGE = "ReportSectionprobe0000000001"
PROBE_THEME_FILE = "StudioContractProbe.json"
PROBE_PERCENTS = (0, -0.4, 0.4)
PROBE_COLOR_IDS = tuple(range(12))
TILE = 100
CANVAS = (1920, 1080)
# Pure colors drawn as literal hex: they locate the tiles in the screenshot and give the
# gain the screenshot applies to every color (measured 2026-09-28: 213/255, uniform).
CALIBRATION = {
    "red": "#FF0000",
    "green": "#00FF00",
    "blue": "#0000FF",
    "yellow": "#FFFF00",
    "cyan": "#00FFFF",
    "magenta": "#FF00FF",
}
_TILE_LEFT, _TILE_STRIDE, _TILE_TOP, _ROW_STRIDE = 40, 130, 40, 140
_CAL_LEFT, _CAL_STRIDE, _CAL_TOP, _CAL_ROW = 200, 400, 520, 200
COLOR_TOLERANCE = 4  # per channel, after gain correction, for "this color is that slot"


def _hex(rgb: Sequence[float]) -> str:
    red, green, blue = (max(0, min(255, round(v))) for v in rgb)
    return f"#{red:02X}{green:02X}{blue:02X}"


def _data_colors() -> list[str]:
    """Twelve distinct, well separated hues. The lowest channel of each is above 25 so none
    can be mistaken for a pure calibration color."""
    colors = []
    for i in range(12):
        red, green, blue = colorsys.hls_to_rgb((i + 0.5) / 12, 0.42 if i % 2 == 0 else 0.55, 0.72)
        colors.append(_hex((red * 255, green * 255, blue * 255)))
    return colors


PROBE_THEME: dict[str, Any] = {
    "name": "StudioContractProbe",
    "dataColors": _data_colors(),
    "background": "#203040",
    "foreground": "#F0E0D0",
    "backgroundLight": "#305070",
    "backgroundNeutral": "#507090",
    "foregroundNeutralSecondary": "#A0A0FF",
    "foregroundNeutralTertiary": "#FFA0A0",
    "tableAccent": "#B0E070",
    "good": "#70E0B0",
    "neutral": "#E0B070",
    "bad": "#E07070",
    "maximum": "#70B0E0",
    "center": "#B070E0",
    "minimum": "#E070B0",
    "null": "#808070",
    "hyperlink": "#7080E0",
    "visitedHyperlink": "#E08070",
}


@dataclass(frozen=True)
class Tile:
    name: str
    x: int
    y: int
    color_id: int | None = None
    percent: float | None = None
    literal: str | None = None  # a calibration tile's hex

    @property
    def center(self) -> tuple[float, float]:
        return self.x + TILE / 2, self.y + TILE / 2


def probe_tiles() -> tuple[list[Tile], list[Tile]]:
    """(theme tiles, calibration tiles) with their positions on the 1920 x 1080 page."""
    tiles = []
    for row, percent in enumerate(PROBE_PERCENTS):
        for color_id in PROBE_COLOR_IDS:
            tiles.append(
                Tile(
                    f"probe{color_id:02d}{'abc'[row]}".ljust(20, "0"),
                    _TILE_LEFT + color_id * _TILE_STRIDE,
                    _TILE_TOP + row * _ROW_STRIDE,
                    color_id,
                    percent,
                )
            )
    calibration = []
    for index, (label, value) in enumerate(CALIBRATION.items()):
        calibration.append(
            Tile(
                f"cal{label}".ljust(20, "0"),
                _CAL_LEFT + (index % 3) * _CAL_STRIDE,
                _CAL_TOP + (index // 3) * _CAL_ROW,
                literal=value,
            )
        )
    return tiles, calibration


def _shape(schema: str, tile: Tile, fill: dict[str, Any]) -> dict[str, Any]:
    # A shape only honors formatting written under the "default" state selector (measured
    # 2026-09-28): without it the fill falls back to dataColors[0], which would ruin the probe.
    default = {"id": "default"}
    return {
        "$schema": schema,
        "name": tile.name,
        "position": {
            "x": tile.x,
            "y": tile.y,
            "z": 1000,
            "height": TILE,
            "width": TILE,
            "tabOrder": 1000,
        },
        "visual": {
            "visualType": "shape",
            "objects": {
                "shape": [
                    {
                        "properties": {"tileShape": pbir_expr.string("rectangle")},
                        "selector": default,
                    }
                ],
                "fill": [
                    {
                        "properties": {
                            "fillColor": fill,
                            "transparency": pbir_expr.double(0),
                            "show": pbir_expr.boolean(True),
                        },
                        "selector": default,
                    }
                ],
                "outline": [
                    {"properties": {"show": pbir_expr.boolean(False)}, "selector": default}
                ],
            },
            "drillFilterOtherVisuals": True,
        },
    }


def probe_fill(color_id: int, percent: float) -> dict[str, Any]:
    """A ThemeDataColor fill for the probe.

    Ids 10 and 11 are invalid on purpose: the probe records what Desktop draws for them
    (measured: the default fill), so they bypass the guarded ``pbir_expr.theme_color``.
    """
    if color_id in pbir_expr.VERIFIED_THEME_COLOR_IDS:
        return pbir_expr.theme_color(color_id, percent)
    themed = {"ThemeDataColor": {"ColorId": color_id, "Percent": percent}}
    return {"solid": {"color": {"expr": themed}}}


def build_probe_pbix(source: Path, destination: Path) -> dict[str, Any]:
    """Copy ``source`` to ``destination`` with a black probe page and a custom theme.

    The page holds ``shape`` tiles filled with ThemeDataColor ids 0..11 at Percent 0, -0.4 and
    +0.4, plus literal-color calibration tiles. Returns the layout (also stored in the receipt).
    """
    package = PbixPackage(source)
    document = pbir_document.PbirDocument(package)
    page_schema, visual_schema = document.schema("page"), document.schema("visualContainer")
    report = document.report
    base = report.get("themeCollection", {}).get("baseTheme", {})
    collection = report.setdefault("themeCollection", {})
    collection["customTheme"] = {
        "name": PROBE_THEME_FILE,
        "reportVersionAtImport": copy.deepcopy(base.get("reportVersionAtImport", {})),
        "type": "RegisteredResources",
    }
    packages = report.setdefault("resourcePackages", [])
    registered = next((p for p in packages if p.get("type") == "RegisteredResources"), None)
    if registered is None:
        registered = {"name": "RegisteredResources", "type": "RegisteredResources", "items": []}
        packages.append(registered)
    registered["items"] = [
        i for i in registered.get("items", []) if i.get("name") != PROBE_THEME_FILE
    ]
    registered["items"].append(
        {"name": PROBE_THEME_FILE, "path": PROBE_THEME_FILE, "type": "CustomTheme"}
    )
    document.touch_report()
    document.write_resource("RegisteredResources", PROBE_THEME_FILE, PROBE_THEME)

    order = [p for p in document.pages_meta.get("pageOrder", []) if p != PROBE_PAGE]
    document.pages_meta["pageOrder"] = [*order, PROBE_PAGE]
    document.touch_pages()
    document.commit()

    page = {
        "$schema": page_schema,
        "name": PROBE_PAGE,
        "displayName": "Studio contract probe",
        "displayOption": "FitToPage",
        "height": CANVAS[1],
        "width": CANVAS[0],
        "objects": {
            "background": [
                {
                    "properties": {
                        "color": pbir_expr.color("#000000"),
                        "transparency": pbir_expr.double(0),
                    }
                }
            ]
        },
    }
    folder = f"{pbir_document.ROOT}pages/{PROBE_PAGE}/"
    package.write_json(folder + "page.json", page)
    tiles, calibration = probe_tiles()
    for tile in tiles:
        assert tile.color_id is not None and tile.percent is not None
        fill = probe_fill(tile.color_id, tile.percent)
        package.write_json(
            f"{folder}visuals/{tile.name}/visual.json", _shape(visual_schema, tile, fill)
        )
    for tile in calibration:
        assert tile.literal is not None
        package.write_json(
            f"{folder}visuals/{tile.name}/visual.json",
            _shape(visual_schema, tile, pbir_expr.color(tile.literal)),
        )
    package.write(destination)
    return {
        "page": PROBE_PAGE,
        "canvas": list(CANVAS),
        "tile_size": TILE,
        "theme": PROBE_THEME,
        "tiles": [
            {"name": t.name, "color_id": t.color_id, "percent": t.percent, "x": t.x, "y": t.y}
            for t in tiles
        ],
        "calibration": [
            {"name": t.name, "hex": t.literal, "x": t.x, "y": t.y} for t in calibration
        ],
    }


def classify_calibration(red: int, green: int, blue: int) -> str | None:
    """Which pure primary or secondary a pixel is (each channel clearly high or clearly zero).

    The screenshot dims every color to about 83%, so "high" starts well under 255; theme
    colors always keep a channel above the "zero" limit, so they never match.
    """
    high, low = 120, 8
    channels = (red, green, blue)
    if any(low < c < high for c in channels):
        return None
    on = tuple(c >= high for c in channels)
    if on == (True, True, True) or on == (False, False, False):
        return None
    if max(channels) - min(c for c in channels if c >= high) > 25:
        return None  # two "on" channels must agree: yellow, cyan and magenta are equal-channel
    names: dict[tuple[bool, ...], str] = {
        (True, False, False): "red",
        (False, True, False): "green",
        (False, False, True): "blue",
        (True, True, False): "yellow",
        (False, True, True): "cyan",
        (True, False, True): "magenta",
    }
    return names[on]


@dataclass(frozen=True)
class Fit:
    """Layout coordinates to screenshot pixels: ``pixel = scale * layout + offset``, per axis,
    and the gain the screenshot applies to colors."""

    scale_x: float
    offset_x: float
    scale_y: float
    offset_y: float
    gain: float
    found: tuple[str, ...]

    def to_pixels(self, x: float, y: float) -> tuple[float, float]:
        return self.scale_x * x + self.offset_x, self.scale_y * y + self.offset_y


def _line_fit(pairs: Sequence[tuple[float, float]]) -> tuple[float, float]:
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    spread = sum((x - mean_x) ** 2 for x in xs)
    if spread == 0:
        raise ValueError("the calibration tiles do not vary along an axis")
    scale = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / spread
    return scale, mean_y - scale * mean_x


def median_color(image: Image, cx: float, cy: float, half: int) -> tuple[int, int, int]:
    """Per-channel median of the (2 * half + 1) square around a pixel."""
    x0, y0 = int(round(cx)), int(round(cy))
    samples: list[tuple[int, int, int]] = []
    for y in range(max(0, y0 - half), min(image.height, y0 + half + 1)):
        for x in range(max(0, x0 - half), min(image.width, x0 + half + 1)):
            samples.append(image.pixel(x, y))
    if not samples:
        raise ValueError("the sample point is outside the screenshot")
    red, green, blue = (int(statistics.median(s[i] for s in samples)) for i in range(3))
    return red, green, blue


def fit_screenshot(image: Image, calibration: Sequence[Tile]) -> Fit:
    """Find the calibration tiles in the screenshot and derive the layout-to-pixel mapping."""
    regions = find_regions(image, classify_calibration)
    pairs_x: list[tuple[float, float]] = []
    pairs_y: list[tuple[float, float]] = []
    gains: list[float] = []
    found: list[str] = []
    sizes: list[float] = []
    for tile in calibration:
        label = tile.name.removeprefix("cal").rstrip("0")
        candidates = [r for r in regions if r.label == label and r.area >= 200 and r.fill >= 0.9]
        if not candidates:
            continue
        region = max(candidates, key=lambda r: r.area)
        cx, cy = region.center
        pairs_x.append((tile.center[0], cx))
        pairs_y.append((tile.center[1], cy))
        sizes.append(statistics.fmean((region.width, region.height)) / TILE)
        observed = median_color(image, cx, cy, max(1, min(region.width, region.height) // 5))
        true = _parse_hex(tile.literal or "#000000")
        gains += [o / t for o, t in zip(observed, true, strict=True) if t == 255]
        found.append(label)
    if len(found) < 3:
        raise ValueError(f"only {len(found)} of {len(calibration)} calibration tiles were found")
    scale_x, offset_x = _line_fit(pairs_x)
    scale_y, offset_y = _line_fit(pairs_y)
    if max(sizes) - min(sizes) > 0.15 * max(sizes) or abs(scale_x - scale_y) > 0.1 * scale_x:
        raise ValueError("the calibration tiles do not line up with the page layout")
    return Fit(scale_x, offset_x, scale_y, offset_y, statistics.fmean(gains), tuple(found))


def _parse_hex(value: str) -> tuple[int, int, int]:
    text = pbir_expr.hex_color(value)[1:]
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _distance(a: str, b: str) -> int:
    return max(abs(x - y) for x, y in zip(_parse_hex(a), _parse_hex(b), strict=True))


def theme_slots(theme: dict[str, Any]) -> dict[str, str]:
    """Every color the theme names, by slot, plus the two fixed colors."""
    slots = {"fixed:white": "#FFFFFF", "fixed:black": "#000000"}
    for key, value in theme.items():
        if (
            key != "dataColors"
            and isinstance(value, str)
            and re.fullmatch(r"#[0-9A-Fa-f]{6}", value)
        ):
            slots[key] = value.upper()
    for index, value in enumerate(theme.get("dataColors", [])):
        slots[f"dataColors[{index}]"] = value.upper()
    return slots


def measured_palette(theme: dict[str, Any]) -> list[str]:
    """ColorId 0..9 as measured on Desktop 2.157 (design doc, Measured decisions)."""
    return ["#FFFFFF", "#000000", *[c.upper() for c in theme["dataColors"][:8]]]


def _percent_key(percent: float) -> str:
    return f"{percent:g}"


def analyze_probe(image: Image, theme: dict[str, Any] = PROBE_THEME) -> dict[str, Any]:
    """Sample the probe screenshot: observed hex per (ColorId, Percent), the theme slot each
    equals, and which ColorIds match the palette Studio relies on."""
    tiles, calibration = probe_tiles()
    fit = fit_screenshot(image, calibration)
    half = max(2, int(TILE * fit.scale_x * 0.2))
    slots, palette = theme_slots(theme), measured_palette(theme)
    mapping: dict[str, dict[str, Any]] = {}
    for tile in tiles:
        assert tile.color_id is not None and tile.percent is not None
        cx, cy = fit.to_pixels(*tile.center)
        raw = median_color(image, cx, cy, half)
        observed = _hex([c / fit.gain for c in raw])
        slot = None
        best = COLOR_TOLERANCE + 1
        for name, value in slots.items():
            gap = _distance(observed, pbir_expr.shade(value, tile.percent))
            if gap < best:
                slot, best = name, gap
        expected = (
            pbir_expr.shade(palette[tile.color_id], tile.percent)
            if tile.color_id < len(palette)
            else None
        )
        entry = {
            "observed": observed,
            "raw": _hex(raw),
            "slot": slot,
            "expected": expected,
            "matches_expected": expected is not None
            and _distance(observed, expected) <= COLOR_TOLERANCE,
        }
        mapping.setdefault(str(tile.color_id), {})[_percent_key(tile.percent)] = entry
    verified = [
        int(color_id)
        for color_id, by_percent in mapping.items()
        if int(color_id) < len(palette) and all(e["matches_expected"] for e in by_percent.values())
    ]
    disagreements = []
    for color_id in range(len(palette)):
        for percent in PROBE_PERCENTS:
            try:
                claimed = pbir_expr.resolve_theme_color(theme, color_id, percent)
            except pbir_expr.ExpressionError:
                continue
            seen = mapping[str(color_id)][_percent_key(percent)]["observed"]
            if _distance(claimed, seen) > COLOR_TOLERANCE:
                disagreements.append(
                    {
                        "color_id": color_id,
                        "percent": percent,
                        "pbir_expr": claimed,
                        "observed": seen,
                    }
                )
    return {
        "gain": round(fit.gain, 4),
        "calibration_tiles_found": list(fit.found),
        "mapping": mapping,
        "theme_color_ids_verified": sorted(verified),
        "pbir_expr_disagreements": disagreements,
    }


# --- literal and selector goldens (checks 9 and 10) ----------------------------------------------

FILTER_NAMES = {"decimal": "studiocontractdec00001", "datetime": "studiocontractdt000001"}


def categorical_filter(name: str, table: str, column: str, literal: str) -> dict[str, Any]:
    """A visual filter on one column with one value, spelled as Desktop writes filters."""
    column_ref = {"Expression": {"SourceRef": {"Source": "t"}}, "Property": column}
    return {
        "name": name,
        "field": {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": column}},
        "type": "Categorical",
        "filter": {
            "Version": 2,
            "From": [{"Name": "t", "Entity": table, "Type": 0}],
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [{"Column": column_ref}],
                            "Values": [[{"Literal": {"Value": literal}}]],
                        }
                    }
                }
            ],
        },
    }


def filter_literals(visual: dict[str, Any]) -> dict[str, str | None]:
    """Filter name to the first literal of its ``In`` condition."""
    found: dict[str, str | None] = {}
    for item in visual.get("filterConfig", {}).get("filters", []):
        literal = None
        try:
            values = item["filter"]["Where"][0]["Condition"]["In"]["Values"]
            literal = values[0][0]["Literal"]["Value"]
        except (KeyError, IndexError, TypeError):
            pass
        found[str(item.get("name"))] = literal
    return found


def filter_literals_by_field(visual: dict[str, Any]) -> dict[str, str | None]:
    """ "Table.Column" to the first literal of the filter on that column (Desktop may rename
    filters, so what a filter says matters more than what it is called)."""
    found: dict[str, str | None] = {}
    literals = filter_literals(visual)
    for item in visual.get("filterConfig", {}).get("filters", []):
        column = item.get("field", {}).get("Column", {})
        entity = column.get("Expression", {}).get("SourceRef", {}).get("Entity")
        if isinstance(entity, str) and isinstance(column.get("Property"), str):
            found[f"{entity}.{column['Property']}"] = literals.get(str(item.get("name")))
    return found


def add_literal_filters(
    source: Path, destination: Path, columns: dict[str, tuple[str, str]]
) -> dict[str, Any]:
    """Write a fixed-decimal and a datetime filter literal into the report's first visual.

    ``columns`` maps "decimal" and "datetime" to (table, column). Returns where they went and
    the exact literal text written, for comparison after Desktop saves.
    """
    package = PbixPackage(source)
    document = pbir_document.PbirDocument(package)
    page, visual_id, visual = document.all_visuals()[0]
    values = {
        "decimal": pbir_expr.data_literal(12.5, "decimal"),
        "datetime": pbir_expr.data_literal("2019-01-01", "datetime"),
    }
    config = visual.setdefault("filterConfig", {}).setdefault("filters", [])
    for kind, (table, column) in columns.items():
        config.append(categorical_filter(FILTER_NAMES[kind], table, column, values[kind]))
    document.touch_visual(page, visual_id)
    document.commit()
    package.write(destination)
    return {
        "page": page.name,
        "visual": visual_id,
        "written": {FILTER_NAMES[k]: values[k] for k in columns},
        "by_field": {f"{table}.{column}": values[k] for k, (table, column) in columns.items()},
        "columns": {k: list(v) for k, v in columns.items()},
    }


def find_selected_entry(visual: dict[str, Any]) -> dict[str, Any] | None:
    """The ``selection:selected`` entry of an advancedSlicerVisual's fillCustom, if any."""
    for entry in visual.get("visual", {}).get("objects", {}).get("fillCustom", []):
        if entry.get("selector") == {"id": "selection:selected"}:
            return entry  # type: ignore[no-any-return]
    return None


def add_selected_selector(source: Path, destination: Path, color: str) -> dict[str, Any] | None:
    """Give the first advancedSlicerVisual a ``selection:selected`` fill of ``color``.

    Returns where it went and what was written, or None when the report has no such visual.
    """
    package = PbixPackage(source)
    document = pbir_document.PbirDocument(package)
    for page, visual_id, visual in document.all_visuals():
        if visual.get("visual", {}).get("visualType") != "advancedSlicerVisual":
            continue
        entries = visual["visual"].setdefault("objects", {}).setdefault("fillCustom", [])
        entry = find_selected_entry(visual)
        existed = entry is not None
        if entry is None:
            entry = {"properties": {}, "selector": {"id": "selection:selected"}}
            entries.append(entry)
        entry["properties"]["fillColor"] = pbir_expr.color(color)
        document.touch_visual(page, visual_id)
        document.commit()
        package.write(destination)
        return {
            "page": page.name,
            "visual": visual_id,
            "entry_existed": existed,
            "written": pbir_expr.quote(pbir_expr.hex_color(color)),
        }
    return None


def selected_fill_literal(visual: dict[str, Any]) -> str | None:
    entry = find_selected_entry(visual)
    if entry is None:
        return None
    try:
        return entry["properties"]["fillColor"]["solid"]["color"]["expr"]["Literal"]["Value"]  # type: ignore[no-any-return]
    except (KeyError, TypeError):
        return None


def measure_refs(node: Any) -> list[tuple[str, str]]:
    """(entity, measure) for every measure a visual's JSON references, in order, without repeats."""
    found: list[tuple[str, str]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            measure = value.get("Measure")
            if isinstance(measure, dict) and isinstance(measure.get("Property"), str):
                entity = measure.get("Expression", {}).get("SourceRef", {}).get("Entity")
                if isinstance(entity, str) and (entity, measure["Property"]) not in found:
                    found.append((entity, measure["Property"]))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found


def schema_versions(package: PbixPackage) -> dict[str, list[str]]:
    """Schema versions the report's parts declare, by kind (visualContainer, page, report, ...)."""
    pattern = re.compile(r"/definition/([A-Za-z]+)/(\d+(?:\.\d+)*)/schema\.json")
    found: dict[str, set[str]] = {}
    for name in package.names():
        if not (name.startswith("Report/definition/") and name.endswith(".json")):
            continue
        try:
            value = package.read_json(name)
        except (ValueError, KeyError):
            continue
        match = pattern.search(value.get("$schema", "")) if isinstance(value, dict) else None
        if match:
            found.setdefault(match.group(1), set()).add(match.group(2))
    return {kind: sorted(versions) for kind, versions in sorted(found.items())}


def write_studio_copy(source: Path, destination: Path) -> None:
    """A copy the way Studio writes a revision: re-serialized report.json, no SecurityBindings."""
    package = PbixPackage(source)
    package.write_json(REPORT_PART, package.read_json(REPORT_PART))
    package.write(destination)


# --- checks and the receipt ----------------------------------------------------------------------


@dataclass
class Check:
    id: str
    title: str
    gate: bool  # a gate must pass for model edits to be allowed on this Desktop version
    expected: Any = None
    observed: Any = None
    status: str = "not_run"  # passed | failed | skipped | error | not_run
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "gate": self.gate,
            "passed": self.passed,
            "status": self.status,
            "expected": self.expected,
            "observed": self.observed,
            "seconds": self.seconds,
            "notes": self.notes,
            "error": self.error,
        }


def receipt_passed(checks: Sequence[Check]) -> bool:
    gates = [c for c in checks if c.gate]
    return bool(gates) and all(c.passed for c in gates)


def build_receipt(
    version: str | None,
    checks: Sequence[Check],
    header: dict[str, Any],
    *,
    partial: bool,
    started: datetime,
    finished: datetime,
) -> dict[str, Any]:
    return {
        "receipt_version": RECEIPT_VERSION,
        "desktop_version": version,
        "passed": receipt_passed(checks) and not partial,
        "partial": partial,
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        **header,
        "checks": [c.to_json() for c in checks],
    }


def receipt_path(out_dir: Path, version: str | None, *, partial: bool = False) -> Path:
    name = version if version and re.fullmatch(r"[0-9A-Za-z._-]{1,40}", version) else "unknown"
    return out_dir / f"desktop-{name}{'.partial' if partial else ''}.json"


def write_receipt(receipt: dict[str, Any], out_dir: Path) -> Path:
    """Write the receipt atomically as ``desktop-<version>.json`` and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = receipt_path(
        out_dir, receipt.get("desktop_version"), partial=bool(receipt.get("partial"))
    )
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=out_dir)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(receipt, stream, indent=2, default=str)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return path


def run_check(check: Check, action: Callable[[Check], None]) -> Check:
    """Run one check; whatever it raises is recorded and the next check still runs."""
    started = time.monotonic()
    try:
        action(check)
    except Exception as error:  # noqa: BLE001 - a failing check must not stop the others
        check.status = "error"
        check.error = f"{type(error).__name__}: {error}"
        check.notes.append(traceback.format_exc(limit=4)[-800:])
    check.seconds = round(time.monotonic() - started, 1)
    return check


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# --- reading the model ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelFacts:
    measures: dict[str, dict[str, str]]  # name -> {table, expression}
    formats: dict[str, str]  # measure name -> format string
    cardinalities: dict[str, int]  # "Table.Column" -> distinct values
    row_counts: dict[str, int | None]  # table -> rows (None when it could not be counted)
    tables: list[str]

    def home_table(self) -> str:
        for row in self.measures.values():
            if row["table"]:
                return row["table"]
        return self.tables[0]


def read_model(path: Path) -> ModelFacts:
    """Measures, format strings, column cardinalities and table row counts of a saved PBIX."""
    pbixray: Any = importlib.import_module("pbixray")  # heavy, and only this script counts rows

    measures = {
        str(r["Name"]): {
            "table": str(r.get("TableName", "")),
            "expression": str(r.get("Expression", "")),
        }
        for r in pbix_extract.measures(path)
    }
    formats = {str(r["Name"]): str(r.get("FormatString") or "") for r in pbix_extract.formats(path)}
    cardinalities: dict[str, int] = {}
    row_counts: dict[str, int | None] = {}
    with (
        tempfile.TemporaryDirectory() as tmp,
        pbixray.PBIXRay(str(path), on_disk=True, temp_dir=tmp) as model,
    ):
        tables = [str(t) for t in model.tables]
        for row in json.loads(model.statistics.to_json(orient="records")):
            cardinalities[f"{row['TableName']}.{row['ColumnName']}"] = int(row["Cardinality"] or 0)
        for table in tables:
            try:
                row_counts[table] = sum(
                    len(frame)
                    for frame in model.iter_table(
                        table, chunk_size=50000, strings_as_categorical=False
                    )
                )
            except Exception:  # noqa: BLE001 - one undecodable table must not hide the others
                row_counts[table] = None
    return ModelFacts(measures, formats, cardinalities, row_counts, tables)


# --- Windows job objects: a stand-in for the sidecar's host --------------------------------------

_JOB_KILL_ON_CLOSE = 0x2000
_JOB_BREAKAWAY_OK = 0x800
_PROCESS_SET_QUOTA_TERMINATE = 0x0100 | 0x0001
_PROCESS_QUERY_LIMITED = 0x1000


def _kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


class Job:
    """A kill-on-close job object, like the one the packaged app's host keeps its sidecar in."""

    def __init__(self, *, allow_breakaway: bool) -> None:
        self._k = _kernel32()
        self.handle = self._k.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        buffer = (ctypes.c_ubyte * 256)()
        flags = _JOB_KILL_ON_CLOSE | (_JOB_BREAKAWAY_OK if allow_breakaway else 0)
        struct.pack_into("<I", buffer, 16, flags)
        size = 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112
        if not self._k.SetInformationJobObject(self.handle, 9, buffer, size):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, pid: int) -> None:
        process = self._k.OpenProcess(_PROCESS_SET_QUOTA_TERMINATE, False, pid)
        if not process:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not self._k.AssignProcessToJobObject(self.handle, process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            self._k.CloseHandle(process)

    def contains(self, pid: int) -> bool:
        process = self._k.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
        if not process:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            inside = wintypes.BOOL()
            if not self._k.IsProcessInJob(process, self.handle, ctypes.byref(inside)):
                raise OSError(ctypes.get_last_error(), "IsProcessInJob failed")
            return bool(inside.value)
        finally:
            self._k.CloseHandle(process)

    def close(self) -> None:
        """Closing the handle ends every process still in the job (the host's exit)."""
        if self.handle:
            self._k.CloseHandle(self.handle)
            self.handle = None


# The "sidecar": waits for a go line, launches Desktop the way Studio does, then idles until
# the script ends it. Nothing here touches Desktop after the launch.
_HELPER = r"""
import json, sys, time
from pathlib import Path
config = json.loads(sys.argv[1])
sys.stdin.readline()
from ai_powerbi_analyst.studio.desktop_session import DesktopController
controller = DesktopController(config["bridge"], config["pbi"], config["desktop_exe"])
route = controller.launch(Path(config["path"]))
print(json.dumps({"route": route, "survives": controller.launch_survives_parent()}), flush=True)
time.sleep(600)
"""


# --- the run -------------------------------------------------------------------------------------


def desktop_pids() -> set[int]:
    return {
        p.pid
        for p in psutil.process_iter(["name"])
        if (p.info["name"] or "").casefold() == desktop_session.DESKTOP_PROCESS
    }


class Session:
    """Everything a check may touch: copies of the scratch file and the Desktops it launched."""

    def __init__(self, controller: DesktopController, source: Path, work: Path, out: Path) -> None:
        self.controller, self.source, self.work, self.out = controller, source, work, out
        work.mkdir(parents=True, exist_ok=True)
        self.preexisting = desktop_pids()  # the user's own Desktops: never touched
        self.launched: dict[int, DesktopInstance] = {}
        self.left_open: list[dict[str, Any]] = []
        self.manifest_hash: str | None = None
        self._facts: ModelFacts | None = None
        self.started = time.monotonic()
        self._counter = 0

    def log(self, message: str) -> None:
        print(f"[{time.monotonic() - self.started:6.1f}s] {message}", flush=True)

    def copy(self, label: str) -> Path:
        self._counter += 1
        target = self.work / f"{self._counter:02d}-{label}.pbix"
        shutil.copyfile(self.source, target)
        return target

    def path_for(self, label: str) -> Path:
        self._counter += 1
        return self.work / f"{self._counter:02d}-{label}.pbix"

    def launch(self, path: Path) -> DesktopInstance:
        """Open a scratch copy in a new Desktop and remember it as ours."""
        if self.controller.find(path) is not None:
            raise DesktopSessionError("A Desktop already has this scratch copy open")
        self.log(f"opening {path.name} in Power BI Desktop ...")
        instance = self.controller.open(path, normalize=False)
        if instance.pid in self.preexisting:
            raise DesktopSessionError("Refusing to use a Desktop that was already running")
        self.launched[instance.pid] = instance
        self.remember_manifest(instance)
        return instance

    def remember_manifest(self, instance: DesktopInstance) -> None:
        if self.manifest_hash is not None:
            return
        with contextlib.suppress(DesktopSessionError, ValueError):
            manifest = self.controller.manifest(instance).get("manifest", {})
            text = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
            self.manifest_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    def flag(self, instance: DesktopInstance) -> bool:
        return bool(self.controller.status(instance).get("hasUnsavedChanges"))

    def own(self, instance: DesktopInstance) -> None:
        if instance.pid not in self.launched:
            raise DesktopSessionError("Refusing to touch a Desktop this script did not launch")

    def finish(self, instance: DesktopInstance) -> bool:
        """Save (scratch copy) if dirty, then close through the safe path. Never forces."""
        self.own(instance)
        try:
            if self.flag(instance):
                self.controller.save(instance)
            self.controller.close(instance)
        except (DesktopSessionError, desktop_uia.DesktopAutomationError, OSError) as error:
            self.leave_open(instance, str(error))
            return False
        self.launched.pop(instance.pid, None)
        return True

    def leave_open(self, instance: DesktopInstance, reason: str) -> None:
        self.log(f"leaving pid {instance.pid} running: {reason}")
        # Left to the person running the check: listed once, never retried.
        if all(item["pid"] != instance.pid for item in self.left_open):
            self.left_open.append(
                {"pid": instance.pid, "file": str(instance.path), "reason": reason}
            )

    def model_facts(self) -> ModelFacts:
        """The scratch file's model, read once (every copy starts from the same model)."""
        if self._facts is None:
            self._facts = read_model(self.source)
        return self._facts

    def dirty(self, instance: DesktopInstance, name: str) -> None:
        """Make the open report unsaved with a real model edit (a throwaway measure)."""
        table = self.model_facts().home_table()
        self.controller.apply_measure_operations(
            instance, [CreateMeasure(table=table, name=name, expression="1")], {}
        )

    def rewrite_by_saving(self, instance: DesktopInstance) -> SaveResult:
        """Have Desktop rewrite the whole file, so a later read shows what Desktop keeps.

        A clean report is not rewritten by a save, so it is made dirty first.
        """
        self.dirty(instance, "Studio contract rewrite")
        result = self.controller.save(instance)
        if not result.changed:
            raise DesktopSessionError("Desktop saved without rewriting the file")
        return result

    def wait_for_flag(
        self, instance: DesktopInstance, wanted: bool, seconds: float
    ) -> float | None:
        """Seconds until the unsaved-changes flag equals ``wanted``, or None if it never did."""
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            if self.flag(instance) == wanted:
                return round(time.monotonic() - started, 1)
            time.sleep(1)
        return None


def _summarize_windows(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for item in items:
        buttons = [b for b in item.get("buttons", []) if b.get("id") or b.get("name")]
        summary.append(
            {
                "type": item.get("type", "Window"),
                "name": item.get("name"),
                "className": item.get("className"),
                "enabled": item.get("enabled"),
                "topLevel": item.get("topLevel", True),
                "button_ids": [b["id"] for b in buttons if b.get("id")][:60],
                "button_names": [b["name"] for b in buttons if b.get("name")][:60],
            }
        )
    return summary


_DONT_SAVE = re.compile(r"don[\u2019']?t\s*save|discard", re.IGNORECASE)


def find_dont_save(items: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """Buttons that look like "Don't save", from a UI Automation dump."""
    found: list[dict[str, str]] = []
    for item in items:
        for button in item.get("buttons", []):
            name, ident = str(button.get("name") or ""), str(button.get("id") or "")
            if _DONT_SAVE.search(name) or _DONT_SAVE.search(ident):
                found.append({"id": ident, "name": name, "window": str(item.get("name") or "")})
    return found


def check_environment(s: Session, c: Check) -> None:
    c.expected = {"desktop_version": "readable", "schema_versions": "present in the PBIX"}
    package = PbixPackage(s.source)
    schemas = schema_versions(package)
    c.observed = {
        "desktop_version": s.controller.version(),
        "desktop_exe": s.controller.desktop_exe,
        "pbix": s.source.name,
        "pbix_sha256": sha256_of(s.source),
        "pbix_parts": len(package.names()),
        "schema_versions": schemas,
        "readiness_without_receipt": s.controller.readiness().reasons,
        "job_limit_flags": desktop_session.job_limit_flags(),
        "launch_route": s.controller.launch_route(),
        "bridge_manifest_hash": None,  # known after the first launch; main() fills it in
    }
    c.status = "passed" if c.observed["desktop_version"] and schemas else "failed"


def check_dirty_flag(s: Session, c: Check) -> None:
    # Measured 2026-09-29: Desktop marks even the untouched original as changed while it
    # loads, so only the Studio-written copy has an expectation; the original is recorded.
    baseline = {
        "studio_written": {"at_connect": False, "after_10s": True},
    }
    c.expected = baseline
    c.observed = {}
    for label, prepare in (
        ("original", lambda dest: shutil.copyfile(s.source, dest)),
        ("studio_written", lambda dest: write_studio_copy(s.source, dest)),
    ):
        path = s.path_for(f"dirty-{label}")
        prepare(path)
        started = time.monotonic()
        instance = s.launch(path)
        connected = round(time.monotonic() - started, 1)
        at_connect = s.flag(instance)
        time.sleep(10)
        after = s.flag(instance)
        c.observed[label] = {
            "at_connect": at_connect,
            "after_10s": after,
            "open_seconds": connected,
        }
        s.finish(instance)
    c.notes.append(
        "Studio never treats the flag as evidence of user work: it saves Desktop's own change "
        "after every open (normalize) and compares files, not flags."
    )
    matches = all(
        {k: v for k, v in c.observed[label].items() if k != "open_seconds"} == baseline[label]
        for label in baseline
    )
    c.status = "passed" if matches else "failed"


def check_close_prompt(s: Session, c: Check) -> None:
    c.expected = {"prompt_detected": True, "blocking_windows_at_open": []}
    instance = s.launch(s.copy("close-prompt"))
    pid = instance.pid
    windows = desktop_uia.windows(pid)
    c.observed = {
        "windows_at_open": _summarize_windows(windows),
        "blocking_windows_at_open": s.controller.blocking_windows(instance),
        "save_button_present": any(b.get("id") == "save" for w in windows for b in w["buttons"]),
    }
    s.dirty(instance, "Studio contract prompt")
    c.observed["dirty_after_tom_edit"] = s.flag(instance)
    if not c.observed["dirty_after_tom_edit"]:
        c.status = "failed"
        c.notes.append("The report never became dirty, so the prompt could not be provoked.")
        s.finish(instance)
        return
    posted = desktop_session.post_close(pid)
    c.observed["wm_close_posted_to_windows"] = posted
    detected: list[str] = []
    for _ in range(12):
        time.sleep(1)
        with contextlib.suppress(DesktopSessionError):
            detected = s.controller.blocking_windows(instance)
        if detected:
            break
    c.observed["blocking_windows_after_close"] = detected
    top = desktop_uia.windows(pid)
    deep = desktop_uia.dump(pid)
    c.observed["windows_after_close"] = _summarize_windows(top)
    c.observed["all_roots_after_close"] = _summarize_windows(deep)
    candidates = find_dont_save(deep) or find_dont_save(top)
    c.observed["dont_save_candidates"] = candidates
    c.observed["dont_save_reachable"] = False
    answered = False
    for candidate in candidates:
        try:
            if candidate["id"]:
                desktop_uia.invoke_any(pid, automation_id=candidate["id"])
            else:
                desktop_uia.invoke_any(pid, name=candidate["name"])
        except desktop_uia.DesktopAutomationError as error:
            c.notes.append(f"Could not press {candidate}: {error}")
            continue
        try:
            psutil.Process(pid).wait(timeout=45)
            answered = True
        except psutil.NoSuchProcess:
            answered = True
        except psutil.TimeoutExpired:
            c.notes.append(f"Pressed {candidate} but Desktop stayed open.")
        if answered:
            c.observed["dont_save_button"] = candidate
            break
    if answered:
        c.observed["dont_save_reachable"] = True
        s.launched.pop(pid, None)
        c.notes.append("Desktop closed after 'Don't save' was pressed through UI Automation.")
    else:
        s.leave_open(
            instance,
            "the 'Save changes?' prompt could not be answered through UI Automation: choose "
            "'Don't save' in that Power BI Desktop window yourself",
        )
        c.notes.append(
            "'Don't save' was NOT reachable: Studio keeps refusing to close a dirty Desktop, and "
            "the instance was left open for you to close by hand."
        )
    c.status = "passed" if detected and not c.observed["blocking_windows_at_open"] else "failed"


def check_uia_save(s: Session, c: Check) -> None:
    c.expected = {"changed": True, "flag_after": False, "max_seconds": 30}
    path = s.copy("uia-save")
    instance = s.launch(path)
    dirtied = s.wait_for_flag(instance, True, 25)
    c.observed = {"self_dirtied_after_s": dirtied}
    if dirtied is None:
        s.dirty(instance, "Studio contract save")
        c.notes.append("Desktop did not dirty the file on its own; a throwaway measure did.")
    before = sha256_of(path)
    result = s.controller.save(instance)
    c.observed.update(
        {
            "save_seconds": round(result.seconds, 1),
            "changed": result.changed,
            "file_hash_changed": result.sha256 != before,
            "flag_after": s.flag(instance),
        }
    )
    ok = result.changed and not c.observed["flag_after"] and result.seconds <= 30
    c.status = "passed" if ok else "failed"
    s.finish(instance)


def check_tom_roundtrip(s: Session, c: Check) -> None:
    before = s.model_facts()
    name = "Studio contract measure"
    c.expected = {
        "measure_persisted": {"expression": "2", "format_string": "0.00"},
        "broken_measure_rejected_and_absent": True,
        "cardinalities_unchanged": True,
        "row_counts_unchanged": True,
    }
    path = s.copy("tom")
    instance = s.launch(path)
    table = before.home_table()
    done = s.controller.apply_measure_operations(
        instance,
        [
            CreateMeasure(table=table, name=name, expression="1", format_string="#,##0"),
            UpdateMeasure(name=name, expression="2", format_string="0.00"),
        ],
        {},
    )
    rejected = False
    try:
        s.controller.apply_measure_operations(
            instance,
            [
                CreateMeasure(
                    table=table, name=name + " broken", expression="SUM(NoSuchTable[Nope])"
                )
            ],
            {},
        )
    except DesktopSessionError as error:
        rejected = True
        c.notes.append(f"Desktop rejected the broken measure: {str(error)[:200]}")
    s.controller.save(instance)
    closed = s.finish(instance)
    after = read_model(path)
    c.observed = {
        "operations": done,
        "measure_persisted": {
            "expression": after.measures.get(name, {}).get("expression"),
            "format_string": after.formats.get(name),
        },
        "broken_measure_rejected_and_absent": rejected and (name + " broken") not in after.measures,
        "cardinalities_unchanged": all(
            after.cardinalities.get(k) == v for k, v in before.cardinalities.items()
        ),
        "row_counts_unchanged": all(
            after.row_counts.get(k) == v for k, v in before.row_counts.items()
        ),
        "measures_before": len(before.measures),
        "measures_after": len(after.measures),
        "tables": len(before.tables),
        "columns_compared": len(before.cardinalities),
        "desktop_closed": closed,
    }
    persisted = c.observed["measure_persisted"] == c.expected["measure_persisted"]
    others_kept = all(after.measures.get(k) == v for k, v in before.measures.items())
    c.observed["existing_measures_unchanged"] = others_kept
    ok = (
        persisted
        and others_kept
        and c.observed["broken_measure_rejected_and_absent"]
        and c.observed["cardinalities_unchanged"]
        and c.observed["row_counts_unchanged"]
    )
    c.status = "passed" if ok else "failed"


def count_measure_references(node: Any, name: str) -> dict[str, int]:
    """How many times a visual's JSON uses measure ``name``: field references and queryRefs."""
    counts = {"fields": 0, "query_refs": 0}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            measure = value.get("Measure")
            if isinstance(measure, dict) and measure.get("Property") == name:
                counts["fields"] += 1
            query_ref = value.get("queryRef")
            if isinstance(query_ref, str) and query_ref.endswith("." + name):
                counts["query_refs"] += 1
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return counts


def _measure_reference_counts(path: Path, old: str, new: str) -> dict[str, Any]:
    document = pbir_document.PbirDocument(PbixPackage(path))
    counts: dict[str, Any] = {"old": 0, "new": 0, "query_refs_old": 0, "query_refs_new": 0}
    for _, _, visual in document.all_visuals():
        before, after = count_measure_references(visual, old), count_measure_references(visual, new)
        counts["old"] += before["fields"]
        counts["query_refs_old"] += before["query_refs"]
        counts["new"] += after["fields"]
        counts["query_refs_new"] += after["query_refs"]
    return counts


def check_rename(s: Session, c: Check) -> None:
    c.expected = {"desktop_rewrote_references": False}
    facts = s.model_facts()
    document = pbir_document.PbirDocument(PbixPackage(s.source))
    target = next(
        (
            (entity, prop)
            for _, _, visual in document.all_visuals()
            for entity, prop in measure_refs(visual)
            if prop in facts.measures
        ),
        None,
    )
    if target is None:
        c.status = "skipped"
        c.observed = {"reason": "no visual in the scratch file uses a measure"}
        return
    entity, prop = target
    new_name = prop + " Renamed"
    path = s.copy("rename")
    before = _measure_reference_counts(path, prop, new_name)
    instance = s.launch(path)
    tables = {prop: facts.measures[prop]["table"] or entity}
    s.controller.apply_measure_operations(
        instance, [RenameMeasure(name=prop, new_name=new_name)], tables
    )
    s.controller.save(instance)
    s.finish(instance)
    after = _measure_reference_counts(path, prop, new_name)
    rewrote = after["new"] > 0 and after["old"] == 0
    c.observed = {
        "measure": prop,
        "entity": entity,
        "renamed_to": new_name,
        "references_before": before,
        "references_after": after,
        "desktop_rewrote_references": rewrote,
        "outcome": "rewritten" if rewrote else ("left_dangling" if after["old"] else "removed"),
    }
    c.notes.append(
        "If Desktop rewrote the references, Studio must not rewrite them again; if it left them "
        "dangling, Studio's rename op must rewrite every reference itself (design P5)."
    )
    # Either outcome is a valid finding (the receipt header carries the answer), so a
    # completed run is not a failure.
    c.status = "passed"


def check_launch_survival(s: Session, c: Check) -> None:
    c.expected = {"desktop_alive_after_parent_killed": True, "desktop_in_job": False}
    if sys.platform != "win32":
        c.status, c.observed = "skipped", {"reason": "Windows only"}
        return
    path = s.copy("survival")
    config = {
        "bridge": s.controller.bridge,
        "pbi": s.controller.pbi,
        "desktop_exe": s.controller.desktop_exe,
        "path": str(path),
    }
    job = Job(allow_breakaway=True)
    helper = subprocess.Popen(  # noqa: S603 - this interpreter, a fixed script, JSON config
        [sys.executable, "-c", _HELPER, json.dumps(config)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(filter(None, [str(_SRC), os.environ.get("PYTHONPATH")])),
        },
    )
    instance: DesktopInstance | None = None
    try:
        job.assign(helper.pid)
        assert helper.stdin is not None and helper.stdout is not None
        helper.stdin.write("go\n")
        helper.stdin.flush()
        answer: list[str] = []
        stdout = helper.stdout
        reader = threading.Thread(target=lambda: answer.append(stdout.readline()), daemon=True)
        reader.start()
        reader.join(90)
        launched = json.loads(answer[0]) if answer and answer[0].strip() else {}
        deadline = time.monotonic() + 180
        while instance is None and time.monotonic() < deadline:
            with contextlib.suppress(DesktopSessionError):
                instance = s.controller.find(path)
            if instance is not None and instance.pid in s.preexisting:
                raise DesktopSessionError("Refusing to use a Desktop that was already running")
            if instance is None:
                if helper.poll() is not None and not answer:
                    raise DesktopSessionError("The stand-in host exited before launching Desktop")
                time.sleep(2)
        if instance is None:
            raise DesktopSessionError("Desktop did not open from the helper")
        s.launched[instance.pid] = instance
        s.remember_manifest(instance)
        parent = psutil.Process(instance.pid).parent()
        in_job: bool | None = None  # unknown when Windows refuses to describe the process
        with contextlib.suppress(OSError):
            in_job = job.contains(instance.pid)
        c.observed = {
            "route": launched.get("route"),
            "launch_survives_parent_claim": launched.get("survives"),
            "desktop_pid": instance.pid,
            "helper_pid": helper.pid,
            "desktop_parent_pid": parent.pid if parent else None,
            "desktop_in_job": in_job,
        }
        # The host exits: the helper's process tree goes and the job is closed with it.
        assert psutil.Process(helper.pid).name().casefold().startswith("python")
        subprocess.run(  # noqa: S603 - taskkill of our own helper pid only
            ["taskkill", "/PID", str(helper.pid), "/T", "/F"],  # noqa: S607
            capture_output=True,
            check=False,
        )
        job.close()
        time.sleep(4)
        alive = (
            psutil.pid_exists(instance.pid)
            and abs(psutil.Process(instance.pid).create_time() - instance.create_time) < 1.0
        )
        connected = False
        if alive:
            with contextlib.suppress(DesktopSessionError):
                connected = s.controller.status(instance).get("bridgeStatus") == "connected"
        c.observed.update(
            {
                "helper_alive_after": helper.poll() is None,
                "desktop_alive_after_parent_killed": alive,
                "bridge_still_connected": connected,
            }
        )
        c.status = "passed" if alive and connected and in_job is not True else "failed"
        c.notes.append(
            "A job that forbids breakaway is not covered here: the packaged sidecar's own job "
            "flags are in check 1 ('job_limit_flags'); run Studio once and end its host to confirm."
        )
    finally:
        job.close()
        if helper.poll() is None:
            subprocess.run(  # noqa: S603 - taskkill of our own helper pid only
                ["taskkill", "/PID", str(helper.pid), "/T", "/F"],  # noqa: S607
                capture_output=True,
                check=False,
            )
        if instance is not None and instance.pid in s.launched:
            if psutil.pid_exists(instance.pid):
                s.finish(instance)
            else:
                s.launched.pop(instance.pid, None)  # it did not survive: nothing left to close


def check_theme_probe(s: Session, c: Check) -> None:
    c.expected = {
        "theme_color_ids_verified": list(range(10)),
        "measured_palette": "#FFFFFF, #000000, dataColors[0..7]",
    }
    probe = s.path_for("theme-probe")
    layout = build_probe_pbix(s.source, probe)
    instance = s.launch(probe)
    time.sleep(6)  # let the tiles render
    shot = s.out / f"desktop-{s.controller.version() or 'unknown'}-theme-probe.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    s.controller.screenshot(instance, PROBE_PAGE, shot)
    s.finish(instance)
    result = analyze_probe(decode_png(shot.read_bytes()), layout["theme"])
    result["screenshot"] = shot.name
    result["unverified_color_ids"] = [
        i for i in PROBE_COLOR_IDS if i not in result["theme_color_ids_verified"]
    ]
    result["ids_10_and_up"] = {k: v for k, v in result["mapping"].items() if int(k) >= 10}
    c.observed = result
    c.status = "passed" if result["theme_color_ids_verified"] == list(range(10)) else "failed"
    if result["pbir_expr_disagreements"]:
        c.notes.append(
            "pbir_expr.resolve_theme_color disagrees with what Desktop drew for some ids: fix "
            "pbir_expr.theme_palette (see 'pbir_expr_disagreements')."
        )


def _first_column_of(types: dict[str, Any], code: int) -> tuple[str, str] | None:
    for column in types.get("columns", []):
        if column.get("dataType") == code and not column.get("hidden"):
            return str(column["table"]), str(column["name"])
    return None


def check_literal_golden(s: Session, c: Check) -> None:
    types = pbix_extract.types(s.source)
    columns = {}
    decimal, when = _first_column_of(types, 10), _first_column_of(types, 9)
    if decimal:
        columns["decimal"] = decimal
    if when:
        columns["datetime"] = when
    if not columns:
        c.status = "skipped"
        c.observed = {"reason": "the model has no fixed-decimal or datetime column"}
        return
    path = s.path_for("literals")
    written = add_literal_filters(s.source, path, columns)
    c.expected = {"literals_kept_as_written": written["by_field"]}
    instance = s.launch(path)
    s.rewrite_by_saving(instance)
    s.finish(instance)
    document = pbir_document.PbirDocument(PbixPackage(path))
    _, visual = document.visual(written["visual"])
    after = filter_literals_by_field(visual)
    c.observed = {
        "columns": written["columns"],
        "written": written["by_field"],
        "after_desktop_save": {f: after.get(f, "<dropped>") for f in written["by_field"]},
    }
    c.observed["kept"] = all(after.get(f) == v for f, v in written["by_field"].items())
    c.status = "passed" if c.observed["kept"] else "failed"
    if len(columns) < 2:
        c.notes.append(
            f"Only {sorted(columns)} could be probed: the model has no other column type."
        )


def check_selected_selector(s: Session, c: Check) -> None:
    c.expected = {"selection_selected_fill_survives_save": True}
    path = s.path_for("selected")
    written = add_selected_selector(s.source, path, "#12AB34")
    if written is None:
        c.status = "skipped"
        c.observed = {"reason": "the scratch file has no advancedSlicerVisual"}
        return
    instance = s.launch(path)
    s.rewrite_by_saving(instance)
    s.finish(instance)
    document = pbir_document.PbirDocument(PbixPackage(path))
    _, visual = document.visual(written["visual"])
    after = selected_fill_literal(visual)
    c.observed = {**written, "after_desktop_save": after, "survived": after == written["written"]}
    c.status = "passed" if c.observed["survived"] else "failed"


CHECKS: list[tuple[str, str, bool, Callable[[Session, Check], None]]] = [
    ("01_environment", "Desktop version, schema versions, launch route", True, check_environment),
    (
        "02_dirty_flag",
        "Unsaved-changes flag at open: untouched original vs Studio-written file",
        False,
        check_dirty_flag,
    ),
    (
        "03_close_prompt",
        "Blocking windows, the Save changes? prompt and whether Don't save is reachable",
        True,
        check_close_prompt,
    ),
    ("04_uia_save", "UI Automation Save: time, file changed, flag cleared", True, check_uia_save),
    (
        "05_tom_roundtrip",
        "Model edits over TOM survive a save; rows and cardinalities unchanged",
        True,
        check_tom_roundtrip,
    ),
    (
        "06_rename",
        "Does Desktop rewrite report references when a measure is renamed",
        False,
        check_rename,
    ),
    (
        "07_launch_survival",
        "Desktop outlives the process that launched it",
        True,
        check_launch_survival,
    ),
    (
        "08_theme_color_probe",
        "ThemeDataColor ids 0..11 at Percent 0/-0.4/0.4 by pixel sampling",
        False,
        check_theme_probe,
    ),
    (
        "09_literal_golden",
        "Fixed-decimal and datetime filter literals keep their spelling",
        False,
        check_literal_golden,
    ),
    (
        "10_selection_selected",
        "selection:selected selector survives a save",
        False,
        check_selected_selector,
    ),
]


def observed_value(results: Sequence[Check], number: str, key: str) -> Any:
    """``observed[key]`` of the check whose id starts with ``number``, or None."""
    for check in results:
        if check.id.startswith(number) and isinstance(check.observed, dict):
            return check.observed.get(key)
    return None


def find_desktop_exe() -> str | None:
    override = os.environ.get("PBI_DESKTOP_PATH")
    if override and Path(override).is_file():
        return override
    # The Store install lives under C:\Program Files\WindowsApps, which a normal account cannot
    # list (WinError 5), so globbing it never worked; the app-path registry key and the Appx
    # package lookup in preflight see the same file without listing that folder.
    from ai_powerbi_analyst.preflight import probe_powerbi_desktop

    with contextlib.suppress(Exception):
        probe = probe_powerbi_desktop()
        if probe.available and probe.path:
            return probe.path
    return None


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument(
        "--pbix", required=True, type=Path, help="a scratch PBIX (only read; checks use copies)"
    )
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).parent / "evidence", help="receipt folder"
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="where the copies live (default: a new temp folder, never inside the repository)",
    )
    parser.add_argument(
        "--bridge", nargs="+", default=None, help="bridge argv, e.g. node.exe index.js"
    )
    parser.add_argument("--pbi", default=shutil.which("pbi"), help="pbi-cli executable")
    parser.add_argument(
        "--desktop-exe",
        default=None,
        help="PBIDesktop.exe (default: PBI_DESKTOP_PATH, the registry, or the installed package)",
    )
    parser.add_argument("--only", default="", help="comma-separated check numbers to run, e.g. 4,5")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.desktop_exe:
        args.desktop_exe = find_desktop_exe()  # not in parse_args: it starts PowerShell
    missing = [
        flag
        for flag, value in (
            ("--bridge", args.bridge),
            ("--pbi", args.pbi),
            ("--desktop-exe", args.desktop_exe),
        )
        if not value
    ]
    if missing or not args.pbix.is_file():
        print(f"Missing or unusable: {', '.join(missing) or args.pbix}", file=sys.stderr)
        return 2
    only = {int(x) for x in args.only.split(",") if x.strip().isdigit()}
    selected = [item for item in CHECKS if not only or int(item[0][:2]) in only]
    controller = DesktopController(list(args.bridge), str(args.pbi), str(args.desktop_exe))
    # The copies hold the scratch report's data: keep them out of the repository.
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="studio-contract-"))
    session = Session(controller, args.pbix, work, args.out)
    session.log(f"scratch file: {args.pbix} (read only; copies go to {session.work})")
    started = datetime.now(UTC)
    results = []
    for identifier, title, gate, action in selected:
        session.log(f"check {identifier}: {title}")
        check = run_check(Check(identifier, title, gate), functools.partial(action, session))
        session.log(
            f"  -> {check.status} in {check.seconds}s"
            + (f" ({check.error})" if check.error else "")
        )
        results.append(check)
        # A check that raised left its Desktop behind: close it the safe way, or list it.
        handed_over = {item["pid"] for item in session.left_open}
        for instance in list(session.launched.values()):
            if instance.pid not in handed_over:
                session.finish(instance)
    version = controller.version()
    for check in results:
        if check.id.startswith("01") and isinstance(check.observed, dict):
            check.observed["bridge_manifest_hash"] = session.manifest_hash
    header = {
        "bridge_manifest_hash": session.manifest_hash,
        "schema_versions": observed_value(results, "01", "schema_versions"),
        "theme_color_ids_verified": observed_value(results, "08", "theme_color_ids_verified"),
        "dont_save_reachable": observed_value(results, "03", "dont_save_reachable"),
        "rename_rewrites_report_references": observed_value(
            results, "06", "desktop_rewrote_references"
        ),
        "left_open": session.left_open,
        "platform": sys.platform,
    }
    receipt = build_receipt(
        version, results, header, partial=bool(only), started=started, finished=datetime.now(UTC)
    )
    path = write_receipt(receipt, args.out)
    print()
    for check in results:
        print(f"  {check.status.upper():8} {check.id}  {check.title}")
    print(f"\nreceipt: {path}\npassed: {receipt['passed']}")
    if session.left_open:
        print("\nThese Power BI Desktop windows were launched by this script and are still open.")
        print("Close each by hand (nothing was forced):")
        for item in session.left_open:
            print(f"  - pid {item['pid']} on {item['file']}: {item['reason']}")
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
