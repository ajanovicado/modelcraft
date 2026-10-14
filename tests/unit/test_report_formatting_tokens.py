"""Typed style tokens, selector specificity, theme merging and fidelity of the canvas payload.

Each test builds the visual JSON Desktop writes (literals as ``{"expr": {"Literal": ..}}``,
colors as ``{"solid": {"color": ..}}``) and asserts the exact token a canvas would draw.
"""

from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import report_formatting as rf
from ai_powerbi_analyst.studio.live_report import Unsupported
from ai_powerbi_analyst.studio.report_import import ImportedVisual
from ai_powerbi_analyst.studio.report_props import (
    color,
    data_target,
    properties,
    shade,
    theme_palette,
)
from ai_powerbi_analyst.studio.report_theme import (
    DEFAULT_THEME,
    css_font_family,
    deep_merge,
    merged_theme,
    theme_payload,
)
from ai_powerbi_analyst.studio.report_tokens import (
    SHAPE_NAMES,
    automatic_title,
    property_is_rendered,
    unrendered_properties,
)

DATA_COLORS = [
    "#0B6E4F",
    "#F26419",
    "#33658A",
    "#86BBD8",
    "#F6AE2D",
    "#758E4F",
    "#2F4858",
    "#9A031E",
]


def lit(value: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": value}}}


def solid(value: str) -> dict[str, Any]:
    return {"solid": {"color": lit(f"'{value}'")}}


def themed(color_id: int, percent: float = 0) -> dict[str, Any]:
    return {
        "solid": {"color": {"expr": {"ThemeDataColor": {"ColorId": color_id, "Percent": percent}}}}
    }


def entry(props: dict[str, Any], **selector: Any) -> dict[str, Any]:
    return {"properties": props, **({"selector": selector} if selector else {})}


class NoFields:
    def field(self, field: dict[str, Any], aliases: dict[str, Any] | None = None) -> Any:
        raise Unsupported("no model in this test")


def theme_dict(**extra: Any) -> dict[str, Any]:
    return {**DEFAULT_THEME, "dataColors": DATA_COLORS, **extra}


def make_fields(*items: tuple[str, str, str]) -> list[dict[str, Any]]:
    """(role, kind, label) triples as ``projections`` returns them."""
    return [
        {
            "ref": f"'T'[{label}]",
            "kind": kind,
            "table": "T",
            "name": label,
            "label": label,
            "role": role,
            "query_ref": f"T.{label}",
        }
        for role, kind, label in items
    ]


def fmt(
    visual_type: str,
    objects: dict[str, Any] | None = None,
    container: dict[str, Any] | None = None,
    fields: list[dict[str, Any]] | None = None,
    theme: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"visualType": visual_type}
    if objects is not None:
        body["objects"] = objects
    if container is not None:
        body["visualContainerObjects"] = container
    visual = ImportedVisual(
        id="v",
        title="Visual",
        visual_type=visual_type,
        definition={"visual": body, **(extra or {})},
    )
    return rf.visual_format(NoFields(), visual, fields or [], {}, theme or theme_dict())


# --- specificity (P11, renderer side) --------------------------------------------------------


def test_metadata_entry_before_a_default_entry_still_wins_like_desktop():
    # fb4c5aa3's shape: [{metadata}, {id: default}]. Merged in list order the default entry
    # hid an edit to the metadata one; Desktop resolves by specificity.
    objects = {
        "value": [
            entry({"fontSize": lit("40D"), "horizontalAlignment": lit("'left'")}, metadata="T.A"),
            entry({"fontSize": lit("34D"), "fontFamily": lit("'Arial'")}, id="default"),
        ]
    }
    merged = properties(objects, "value", "T.A")
    assert merged["fontSize"] == lit("40D")  # the metadata entry
    assert merged["fontFamily"] == lit("'Arial'")  # only the default sets it
    assert properties(objects, "value", "T.B")["fontSize"] == lit("34D")
    assert properties(objects, "value")["fontSize"] == lit("34D")


def test_card_value_token_follows_the_metadata_entry():
    objects = {
        "value": [
            entry({"fontSize": lit("40D")}, metadata="T.Sales"),
            entry({"fontSize": lit("34D")}, id="default"),
        ]
    }
    tokens = fmt("cardVisual", objects, fields=make_fields(("Data", "measure", "Sales")))
    assert tokens["cardValue"]["fontSize"] == 53.33  # 40 pt
    assert tokens["fontSize"] == 53.33


def test_specificity_ranks_default_metadata_data_and_state():
    top = entry({"fill": solid("#111111")}, data=[_scope("TopN")])
    both = entry({"fill": solid("#222222")}, data=[_scope("TopN")], metadata="T.Sales")
    objects = {
        "dataPoint": [
            both,
            top,
            entry({"fill": solid("#333333")}, metadata="T.Sales"),
            entry({"fill": solid("#444444")}, id="default"),
            entry({"fill": solid("#555555")}),
            entry({"fill": solid("#666666")}, id="selection:selected"),
        ]
    }
    fill = lambda **kw: properties(objects, "dataPoint", **kw)["fill"]  # noqa: E731
    assert fill() == solid("#444444")  # default beats the selector-less entry
    assert fill(metadata="T.Sales") == solid("#333333")
    assert fill(metadata="T.Sales", data=data_target("TopN")) == solid("#222222")
    assert fill(data=data_target("TopN")) == solid("#111111")
    assert fill(data=data_target("Other")) == solid("#444444")
    assert fill(metadata="T.Sales", identity="selection:selected") == solid("#666666")


def test_typed_scope_literals_match_l_and_d_spellings():
    objects = {"dataPoint": [entry({"fill": solid("#111111")}, data=[_scope("2019L", raw=True)])]}
    assert properties(objects, "dataPoint", data=data_target(2019.0)).get("fill") == solid(
        "#111111"
    )
    assert properties(objects, "dataPoint", data=data_target(2020)) == {}


def _scope(value: str, *, raw: bool = False) -> dict[str, Any]:
    return {
        "scopeId": {
            "Comparison": {
                "ComparisonKind": 0,
                "Left": {"Column": {"Expression": {"SourceRef": {"Entity": "T"}}, "Property": "C"}},
                "Right": {"Literal": {"Value": value if raw else f"'{value}'"}},
            }
        }
    }


# --- colors and the theme palette (P12/P17) -----------------------------------------------------


def test_theme_color_ids_follow_the_measured_palette():
    palette = theme_palette(DATA_COLORS + ["#010203"])
    assert palette[:3] == ["#ffffff", "#000000", "#0b6e4f"]
    assert len(palette) == 10  # ids 10 and up are invalid
    assert color(themed(0), palette) == "#ffffff"  # never the theme background
    assert color(themed(1), palette) == "#000000"
    assert color(themed(2), palette) == "#0b6e4f"  # dataColors[0]
    assert color(themed(9), palette) == "#9a031e"  # dataColors[7]
    assert color(themed(10), palette) is None  # Desktop paints the default fill


def test_shading_rounds_half_up_like_power_bi():
    # white * 0.7 = 178.5: Power BI gives 179 (#b3), Python's round() gives 178 (#b2).
    assert shade("#ffffff", -0.3) == "#b3b3b3"
    assert shade("#ffffff", -0.2) == "#cccccc"
    assert shade("#000000", 0.4) == "#666666"
    assert color(themed(0, -0.3), ["#ffffff"]) == "#b3b3b3"


def test_default_series_color_reads_the_selector_less_fill():
    objects = {"dataPoint": [entry({"fill": themed(2)})]}
    tokens = fmt("clusteredColumnChart", objects)
    assert tokens["series"]["defaultColor"] == "#0b6e4f"
    # No explicit color at all: the canvas paints theme.dataColors[0], not a hard-coded blue.
    bare = fmt("clusteredColumnChart", {})
    assert bare["series"]["defaultColor"] is None
    assert theme_payload(theme_dict())["dataColors"][0] == "#0B6E4F"


def test_invalid_theme_color_id_is_flagged_approximate():
    tokens = fmt("columnChart", {"dataPoint": [entry({"fill": themed(11)})]})
    assert tokens["series"]["defaultColor"] is None
    assert tokens["fidelity"] == "approximate"


def test_series_and_category_colors_decode_typed_literals():
    objects = {
        "dataPoint": [
            entry({"fill": solid("#010101")}, metadata="T.Sales"),
            entry({"fill": solid("#020202")}, data=[_scope("'Store 1'".strip("'"))]),
            entry({"fill": solid("#030303")}, data=[_scope("2019L", raw=True)]),
            entry({"fill": solid("#040404")}, data=[_scope("19.5M", raw=True)]),
            entry(
                {"fill": solid("#050505")}, data=[_scope("datetime'2019-01-01T00:00:00'", raw=True)]
            ),
            entry({"fill": themed(0, -0.2)}, data=[_scope("Others")], metadata="T.Sales"),
        ]
    }
    series = fmt("clusteredColumnChart", objects)["series"]
    assert series["seriesColors"] == {"T.Sales": "#010101"}
    assert series["categoryColors"] == [
        {"value": "Store 1", "color": "#020202", "column": "T.C"},
        {"value": 2019, "color": "#030303", "column": "T.C"},
        {"value": 19.5, "color": "#040404", "column": "T.C"},
        {
            "value": "2019-01-01T00:00:00",
            "color": "#050505",
            "type": "datetime",
            "column": "T.C",
        },
        {"value": "Others", "color": "#cccccc", "column": "T.C", "metadata": "T.Sales"},
    ]


def test_pie_and_donut_vary_by_category():
    assert fmt("pieChart", {})["series"]["varyByCategory"] is True
    assert fmt("clusteredColumnChart", {})["series"]["varyByCategory"] is False
    on = {"dataPoint": [entry({"showAllDataPoints": lit("true")})]}
    assert fmt("clusteredColumnChart", on)["series"]["varyByCategory"] is True


def test_clustered_and_stacked_charts_are_told_apart():
    assert fmt("clusteredColumnChart")["chart"] == {
        "orientation": "vertical",
        "stacking": "clustered",
    }
    assert fmt("columnChart")["chart"] == {"orientation": "vertical", "stacking": "stacked"}
    assert fmt("clusteredBarChart")["chart"] == {
        "orientation": "horizontal",
        "stacking": "clustered",
    }


# --- themes (P17) --------------------------------------------------------------------------------


def write_report(
    path: Path, report: dict[str, Any], parts: dict[str, dict[str, Any]], extra: bytes = b""
) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Report/definition/report.json", json.dumps(report))
        for name, body in parts.items():
            z.writestr(name, json.dumps(body))
        if extra:
            z.writestr("DataModel", extra)
    return path


def theme_report(custom_type: str = "RegisteredResources") -> dict[str, Any]:
    return {
        "themeCollection": {
            "baseTheme": {"name": "Base", "type": "SharedResources"},
            "customTheme": {"name": "Mine.json", "type": custom_type},
        },
        "resourcePackages": [
            {
                "name": "SharedResources",
                "type": "SharedResources",
                "items": [{"name": "Base", "path": "BaseThemes/Base.json", "type": "BaseTheme"}],
            },
            {
                "name": "RegisteredResources",
                "type": "RegisteredResources",
                "items": [{"name": "Mine.json", "path": "Mine.json", "type": "CustomTheme"}],
            },
        ],
    }


BASE = {
    "dataColors": ["#111111", "#222222"],
    "foreground": "#252423",
    "background": "#FFFFFF",
    "textClasses": {
        "title": {"fontSize": 12, "fontFace": "DIN", "color": "#252423"},
        "label": {"fontSize": 10, "fontFace": "Segoe UI", "color": "#252423"},
    },
    "visualStyles": {"*": {"*": {"title": [{"show": True, "fontSize": 12}]}}},
}
CUSTOM = {
    "dataColors": ["#AA0000", "#00AA00", "#0000AA"],
    "textClasses": {"title": {"fontSize": 20}},
    "visualStyles": {"columnChart": {"*": {"legend": [{"show": False}]}}},
}


def test_custom_theme_deep_merges_over_the_base_theme(tmp_path):
    path = write_report(
        tmp_path / "a.pbix",
        theme_report(),
        {
            "Report/StaticResources/SharedResources/BaseThemes/Base.json": BASE,
            "Report/StaticResources/RegisteredResources/Mine.json": CUSTOM,
        },
    )
    merged = merged_theme(str(path), "h1")
    assert merged["dataColors"] == ["#AA0000", "#00AA00", "#0000AA"]  # lists replaced
    assert merged["textClasses"]["title"] == {"fontSize": 20, "fontFace": "DIN", "color": "#252423"}
    assert merged["textClasses"]["label"]["fontFace"] == "Segoe UI"  # base entry kept
    assert set(merged["visualStyles"]) == {"*", "columnChart"}  # per-key merge
    assert merged["foreground"] == "#252423"


def test_custom_theme_can_live_in_shared_resources(tmp_path):
    report = theme_report("SharedResources")
    report["themeCollection"]["customTheme"]["name"] = "Gallery"
    report["resourcePackages"][0]["items"].append(
        {"name": "Gallery", "path": "BaseThemes/Gallery.json", "type": "CustomTheme"}
    )
    path = write_report(
        tmp_path / "b.pbix",
        report,
        {
            "Report/StaticResources/SharedResources/BaseThemes/Base.json": BASE,
            "Report/StaticResources/SharedResources/BaseThemes/Gallery.json": CUSTOM,
        },
    )
    assert merged_theme(str(path), "h2")["dataColors"][0] == "#AA0000"


def test_theme_is_keyed_by_the_file_hash_and_missing_themes_fall_back(tmp_path):
    path = write_report(
        tmp_path / "c.pbix",
        theme_report(),
        {
            "Report/StaticResources/SharedResources/BaseThemes/Base.json": BASE,
            "Report/StaticResources/RegisteredResources/Mine.json": CUSTOM,
        },
    )
    first = merged_theme(str(path), "one")
    first["dataColors"].append("#123456")  # a caller's copy never leaks into the cache
    assert merged_theme(str(path), "one")["dataColors"] == ["#AA0000", "#00AA00", "#0000AA"]
    write_report(
        path, theme_report(), {"Report/StaticResources/SharedResources/BaseThemes/Base.json": BASE}
    )
    assert merged_theme(str(path), "one")["dataColors"][0] == "#AA0000"  # same hash: cached
    assert merged_theme(str(path), "two")["dataColors"] == ["#111111", "#222222"]  # new hash
    bare = write_report(tmp_path / "d.pbix", {}, {})
    assert merged_theme(str(bare), "three")["dataColors"] == DEFAULT_THEME["dataColors"]


def test_deep_merge_replaces_lists_and_scalars_only():
    merged = deep_merge({"a": {"b": 1, "c": [1]}, "d": 1}, {"a": {"c": [2]}, "d": {"e": 1}})
    assert merged == {"a": {"b": 1, "c": [2]}, "d": {"e": 1}}


def test_theme_payload_carries_colors_text_classes_and_palette():
    payload = theme_payload(theme_dict(tableAccent="#0B6E4F"))
    assert payload["dataColors"] == DATA_COLORS
    assert payload["palette"][:3] == ["#ffffff", "#000000", "#0b6e4f"]
    assert payload["tableAccent"] == "#0B6E4F"
    assert payload["textClasses"]["callout"]["fontSizePx"] == 60.0  # 45 pt
    assert "visualStyles" not in payload


def test_unsafe_font_names_never_leave_python():
    assert css_font_family("'Segoe UI Bold', wf_segoe-ui_bold, helvetica, arial, sans-serif") == (
        '"Segoe UI Bold", wf_segoe-ui_bold, helvetica, arial, sans-serif'
    )
    assert css_font_family("Arial; } body { background: url(http://x)") is None
    assert css_font_family('x" onload="alert(1)') is None


# --- title (P17, P34) -----------------------------------------------------------------------


def test_title_defaults_follow_the_visual_type():
    fields = make_fields(("Category", "column", "Region"), ("Y", "measure", "Sales"))
    assert fmt("clusteredColumnChart", fields=fields)["title"]["show"] is True
    for kind in ("tableEx", "cardVisual", "slicer", "advancedSlicerVisual", "card"):
        assert fmt(kind, fields=fields)["title"]["show"] is False, kind
    assert fmt("clusteredColumnChart", fields=fields)["titleVisible"] is True


def test_automatic_title_joins_measures_then_category_and_series():
    fields = make_fields(
        ("Y", "measure", "Total Sales"),
        ("Category", "column", "ChannelName"),
        ("Y", "measure", "Profit"),
        ("Series", "column", "Region"),
    )
    assert automatic_title(fields) == "Total Sales and Profit by ChannelName and Region"
    tokens = fmt("clusteredColumnChart", fields=fields)
    assert tokens["title"]["textSource"] == "automatic"
    assert tokens["title"]["text"] == "Total Sales and Profit by ChannelName and Region"
    assert automatic_title(make_fields(("Y", "measure", "Total"))) == "Total"
    assert automatic_title([]) is None


def test_literal_title_wins_and_text_without_show_counts_as_shown():
    fields = make_fields(("Category", "column", "Region"), ("Y", "measure", "Sales"))
    container = {
        "title": [
            entry(
                {
                    "text": lit("'Agent''s Sales'"),
                    "fontSize": lit("13D"),
                    "fontColor": solid("#AA0000"),
                    "bold": lit("true"),
                    "alignment": lit("'center'"),
                    "fontFamily": lit("'''Segoe UI Semibold'', helvetica, sans-serif'"),
                }
            )
        ]
    }
    title = fmt("slicer", container=container, fields=fields)["title"]
    assert title["show"] is True and title["textSource"] == "literal"
    assert title["text"] == "Agent's Sales"
    assert title["fontSize"] == 17.33 and title["fontColor"] == "#AA0000"
    assert title["bold"] is True and title["alignment"] == "center"
    assert title["fontFamily"] == '"Segoe UI Semibold", helvetica, sans-serif'
    hidden = {"title": [entry({"show": lit("false"), "text": lit("'x'")})]}
    assert fmt("clusteredColumnChart", container=hidden, fields=fields)["title"]["show"] is False


def test_title_falls_back_to_theme_styles_then_text_classes():
    theme = theme_dict(
        textClasses={"title": {"fontSize": 14, "fontFace": "Georgia", "color": "#334455"}},
        visualStyles={
            "*": {"*": {"title": [{"show": True}]}},
            "tableEx": {"*": {"title": [{"fontSize": 20}]}},
        },
    )
    card = fmt("cardVisual", theme=theme, fields=make_fields(("Data", "measure", "Sales")))["title"]
    assert card["show"] is True  # the theme turns titles on for every visual
    assert card["fontSize"] == 18.67 and card["fontColor"] == "#334455"  # 14 pt from the class
    assert card["fontFamily"] == "Georgia"
    table = fmt("tableEx", theme=theme, fields=make_fields(("Values", "column", "A")))["title"]
    assert table["fontSize"] == 26.67  # visualStyles for the type beat the text class


def test_dynamic_title_is_flagged_not_invented():
    dynamic = {
        "text": {
            "expr": {
                "Measure": {"Expression": {"SourceRef": {"Entity": "T"}}, "Property": "Caption"}
            }
        }
    }
    tokens = fmt("clusteredColumnChart", container={"title": [entry(dynamic)]})
    assert tokens["title"]["textSource"] == "dynamic" and tokens["title"]["text"] == ""
    assert tokens["fidelity"] == "approximate"


# --- container and tile (P17) ---------------------------------------------------------------


def test_container_background_carries_transparency_as_alpha():
    container = {
        "background": [entry({"color": solid("#FF8800"), "transparency": lit("40D")})],
        "border": [
            entry(
                {
                    "show": lit("true"),
                    "color": solid("#123456"),
                    "width": lit("2D"),
                    "radius": lit("8D"),
                }
            )
        ],
        "dropShadow": [entry({"show": lit("true")})],
        "padding": [entry({"top": lit("5D"), "left": lit("7D")})],
    }
    box = fmt("clusteredColumnChart", container=container)["container"]
    assert box["background"] == "#ff880099"  # 60% opaque
    assert box["border"] == {"show": True, "color": "#123456", "width": 2.0, "radius": 8.0}
    assert box["shadow"] is True
    assert box["padding"] == [5.0, None, None, 7.0]


def test_container_background_defaults_and_off_switch():
    assert fmt("clusteredColumnChart")["container"]["background"] == "#ffffffff"  # theme background
    off = {"background": [entry({"show": lit("false")})]}
    box = fmt("clusteredColumnChart", container=off)["container"]
    assert box["background"] is None and box["border"]["show"] is False
    assert fmt("clusteredColumnChart", container=off)["background"] == "transparent"
    # The base theme turns shape and textbox backgrounds off.
    styles = {"shape": {"*": {"background": [{"show": False}]}}}
    assert fmt("shape", theme=theme_dict(visualStyles=styles))["container"]["background"] is None


def test_card_tile_tokens_per_state_with_transparency():
    objects = {
        "fillCustom": [
            entry({"fillColor": themed(0), "transparency": lit("0D")}, id="selection:selected"),
            entry({"show": lit("true")}),
            entry({"fillColor": solid("#F1F2F2"), "transparency": lit("50D")}, id="default"),
        ],
        "outline": [
            entry(
                {"show": lit("true"), "lineColor": themed(0, -0.3), "weight": lit("2D")},
                id="selection:selected",
            ),
            entry({"show": lit("false")}, id="default"),
        ],
        "padding": [
            entry({"paddingSelection": lit("'Custom'"), "leftMargin": lit("13L")}, id="default")
        ],
        "accentBar": [
            entry(
                {
                    "show": lit("true"),
                    "color": solid("#AA0000"),
                    "width": lit("6L"),
                    "position": lit("'Bottom'"),
                },
                id="default",
            )
        ],
        "value": [entry({"fontColor": solid("#222222")}, id="selection:selected")],
    }
    slicer = fmt(
        "advancedSlicerVisual", objects, fields=make_fields(("Values", "column", "Country"))
    )
    tile = slicer["tile"]
    assert tile["default"]["fill"] == "#f1f2f280"
    assert tile["default"]["outline"]["show"] is False
    assert tile["default"]["padding"] == [None, None, None, 13.0]
    assert tile["default"]["accentBar"] == {
        "show": True,
        "color": "#AA0000",
        "width": 6.0,
        "position": "Bottom",
    }
    assert tile["selected"]["fill"] == "#ffffffff"
    assert tile["selected"]["outline"] == {
        "show": True,
        "color": "#b3b3b3",
        "width": 2.0,
        "transparency": 0.0,
    }
    assert slicer["slicer"]["values"]["selected"]["color"] == "#222222"
    off = fmt("cardVisual", {"fillCustom": [entry({"show": lit("false")})]})
    assert off["tile"]["default"]["fill"] is None and off["tile"]["default"]["fillShown"] is False


def test_card_value_and_label_default_to_the_theme_text_classes():
    theme = theme_dict(
        textClasses={
            "callout": {"fontSize": 28, "fontFace": "DIN", "color": "#112233"},
            "label": {"fontSize": 11, "fontFace": "Segoe UI", "color": "#445566"},
        }
    )
    tokens = fmt("cardVisual", {}, fields=make_fields(("Data", "measure", "Sales")), theme=theme)
    assert tokens["cardValue"]["fontSize"] == 37.33  # the callout class, not a fixed 28 pt
    assert tokens["cardValue"]["color"] == "#112233"
    assert tokens["cardLabel"]["fontSize"] == 14.67 and tokens["cardLabel"]["color"] == "#445566"


# --- charts: labels, legend, axes -----------------------------------------------------------


def test_data_label_legend_and_axis_tokens():
    objects = {
        "labels": [
            entry(
                {
                    "show": lit("true"),
                    "fontSize": lit("16D"),
                    "color": solid("#AA0000"),
                    "labelDisplayUnits": lit("1000D"),
                    "labelPrecision": lit("1L"),
                    "labelPosition": lit("'OutsideEnd'"),
                }
            )
        ],
        "legend": [
            entry({"show": lit("true"), "position": lit("'BottomCenter'"), "fontSize": lit("9D")})
        ],
        "categoryAxis": [
            entry(
                {
                    "showAxisTitle": lit("false"),
                    "fontSize": lit("10D"),
                    "labelColor": themed(1),
                    "gridlineShow": lit("true"),
                }
            )
        ],
        "valueAxis": [entry({"titleText": lit("'Revenue'"), "gridlineShow": lit("false")})],
    }
    tokens = fmt("lineChart", objects)
    assert tokens["labels"] == {
        "show": True,
        "fontSize": 21.33,
        "color": "#AA0000",
        "family": '"Segoe UI"',
        "bold": False,
        "italic": False,
        "displayUnits": 1000.0,
        "decimals": 1,
        "position": "OutsideEnd",
        "style": None,
    }
    assert tokens["legend"]["position"] == "bottom" and tokens["legend"]["alignment"] == "center"
    assert tokens["legend"]["fontSize"] == 12.0
    category = tokens["axes"]["category"]
    assert (
        category["title"] is False
        and category["color"] == "#000000"
        and category["gridlines"] is True
    )
    value = tokens["axes"]["value"]
    assert (
        value["titleText"] == "Revenue" and value["gridlines"] is False and value["title"] is True
    )
    assert tokens["line"] == {"strokeWidth": 3.0, "markers": False}
    # Labels are off unless switched on, and the axis text defaults to the label class.
    quiet = fmt("clusteredColumnChart")
    assert quiet["labels"]["show"] is False
    assert (
        quiet["axes"]["value"]["fontSize"] == 13.33 and quiet["axes"]["value"]["gridlines"] is True
    )


def test_legacy_top_level_keys_are_kept_for_older_clients():
    objects = {
        "categoryAxis": [entry({"showAxisTitle": lit("false")})],
        "valueAxis": [entry({"gridlineShow": lit("false")})],
        "grid": [entry({"gridHorizontal": lit("false")})],
    }
    tokens = fmt("clusteredColumnChart", objects)
    assert tokens["categoryAxisTitle"] is False and tokens["valueAxisTitle"] is True
    assert tokens["grid"] is False and tokens["tableGrid"] is False


# --- tables ---------------------------------------------------------------------------------


def test_table_font_no_longer_depends_on_the_horizontal_grid():
    grid_off = {"grid": [entry({"gridHorizontal": lit("false"), "rowPadding": lit("9D")})]}
    off = fmt("tableEx", grid_off, fields=make_fields(("Values", "column", "Agent")))["table"]
    on = fmt("tableEx", {}, fields=make_fields(("Values", "column", "Agent")))["table"]
    assert off["values"]["fontSize"] == on["values"]["fontSize"] == 13.33  # the 10 pt label class
    assert off["header"]["fontSize"] == 16.0  # the 12 pt header class
    assert off["grid"]["horizontal"] is False and off["grid"]["rowPadding"] == 9.0
    assert on["grid"]["horizontal"] is True


def test_table_header_values_banding_and_column_tokens():
    objects = {
        "values": [
            entry(
                {
                    "fontSize": lit("12D"),
                    "fontColorPrimary": themed(1),
                    "fontColorSecondary": solid("#555555"),
                    "backColorPrimary": solid("#FFFFFF"),
                    "backColorSecondary": solid("#F3F2F1"),
                }
            )
        ],
        "columnHeaders": [
            entry(
                {
                    "fontSize": lit("14D"),
                    "fontColor": solid("#FFFFFF"),
                    "backColor": solid("#0B6E4F"),
                }
            )
        ],
        "columnFormatting": [
            entry({"alignment": lit("'Center'"), "styleHeader": lit("true")}, metadata="T.Rank")
        ],
        "columnWidth": [entry({"value": lit("78.915D")}, metadata="T.Rank")],
    }
    table = fmt("tableEx", objects, fields=make_fields(("Values", "column", "Rank")))["table"]
    assert table["values"]["fontSize"] == 16.0 and table["values"]["color"] == "#000000"
    assert table["values"]["secondaryColor"] == "#555555"
    assert table["values"]["banding"] == {"primary": "#FFFFFF", "secondary": "#F3F2F1"}
    assert table["header"]["fontSize"] == 18.67 and table["header"]["background"] == "#0B6E4F"
    assert table["columns"] == {
        "T.Rank": {
            "alignment": "center",
            "styleHeader": True,
            "width": 78.92,
            "fontColor": None,
            "background": None,
            "displayUnits": None,
            "decimals": None,
        }
    }


# --- textbox, shape, image ------------------------------------------------------------------


def test_textbox_runs_are_plain_text_with_links_stripped():
    paragraphs = [
        {
            "horizontalTextAlignment": "center",
            "textRuns": [
                {
                    "value": "Hello <script>alert(1)</script>World",
                    "textStyle": {
                        "fontFamily": "Segoe UI Semibold",
                        "fontSize": "14pt",
                        "color": "#AA0000",
                        "fontWeight": "bold",
                        "fontStyle": "italic",
                        "textDecoration": "underline",
                    },
                },
                {
                    "value": "click me",
                    "url": "javascript:alert(1)",
                    "textStyle": {"color": "url(javascript:x)", "fontFamily": "x; y {"},
                },
                {"value": "raw \x00control\x1b"},
            ],
        }
    ]
    tokens = fmt("textbox", {"general": [entry({"paragraphs": paragraphs})]})
    (paragraph,) = tokens["content"]
    assert paragraph["align"] == "center"
    first, second, third = paragraph["runs"]
    assert first == {
        "text": "Hello alert(1)World",
        "fontFamily": '"Segoe UI Semibold"',
        "fontSize": 18.67,
        "color": "#AA0000",
        "bold": True,
        "italic": True,
        "underline": True,
        "hasLink": False,
    }
    assert "<" not in json.dumps(tokens["content"]) and "javascript" not in json.dumps(
        tokens["content"]
    )
    assert second["hasLink"] is True and second["color"] is None and second["fontFamily"] is None
    assert third["text"] == "raw control"


def test_shape_only_reads_state_selector_entries():
    theme = theme_dict()
    without = {"fill": [entry({"fillColor": solid("#FF0000")})]}
    ignored = fmt("shape", without, theme=theme)["shape"]
    # Desktop ignored a shape fill written without {"id": "default"}: the default fill shows.
    assert ignored["fill"] == "#0b6e4fff"
    honored = fmt(
        "shape", {"fill": [entry({"fillColor": solid("#FF0000")}, id="default")]}, theme=theme
    )
    assert honored["shape"]["fill"] == "#ff0000ff"
    outlined = {
        "shape": [entry({"tileShape": lit("'oval'")}, id="default")],
        "outline": [
            entry(
                {"show": lit("true"), "lineColor": solid("#000000"), "weight": lit("3D")},
                id="default",
            )
        ],
        "fill": [entry({"show": lit("false")}, id="default")],
    }
    shape = fmt("shape", outlined, theme=theme)["shape"]
    assert shape["shape"] == "oval" and shape["fill"] is None
    assert shape["outline"] == {"show": True, "color": "#000000", "width": 3.0}


def test_image_visual_reads_report_resources(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    report = {
        "resourcePackages": [
            {
                "name": "RegisteredResources",
                "type": "RegisteredResources",
                "items": [
                    {"name": "Logo.png", "path": "Logo.png", "type": "Image"},
                    {"name": "Bad.png", "path": "Bad.png", "type": "Image"},
                ],
            }
        ]
    }
    path = tmp_path / "img.pbix"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Report/definition/report.json", json.dumps(report))
        z.writestr("Report/StaticResources/RegisteredResources/Logo.png", png)
        z.writestr("Report/StaticResources/RegisteredResources/Bad.png", b"<html>not an image")
    context = rf.format_context(str(path), "img-hash")

    def picture(item: str) -> dict[str, Any]:
        node = {
            "expr": {
                "ResourcePackageItem": {
                    "PackageName": "RegisteredResources",
                    "PackageType": 1,
                    "ItemName": item,
                }
            }
        }
        objects = {
            "general": [entry({"imageUrl": node})],
            "imageScaling": [entry({"imageScalingType": lit("'Fill'")})],
        }
        visual = ImportedVisual(
            id="i",
            title="i",
            visual_type="image",
            definition={"visual": {"visualType": "image", "objects": objects}},
        )
        return rf.visual_format(NoFields(), visual, [], {}, None, context=context)["picture"]

    good = picture("Logo.png")
    assert good["src"].startswith("data:image/png;base64,") and good["scaling"] == "fill"
    assert picture("Bad.png")["src"] is None  # by signature, never by file name
    assert picture("Missing.png")["src"] is None


# --- fidelity -------------------------------------------------------------------------------


def test_fidelity_lists_set_properties_the_canvas_does_not_draw():
    objects = {
        "dataPoint": [entry({"fill": solid("#111111")})],
        "categoryAxis": [entry({"innerPadding": lit("45L"), "fontSize": lit("10D")})],
        "plotArea": [entry({"transparency": lit("10D")})],
    }
    container = {
        "title": [entry({"text": lit("'x'")})],
        "spacing": [entry({"verticalSpacing": lit("10D")})],
        "visualTooltip": [entry({"show": lit("true")})],
    }
    tokens = fmt("clusteredColumnChart", objects, container)
    assert tokens["unrendered"] == [
        "categoryAxis.innerPadding",
        "plotArea.transparency",
        "spacing.verticalSpacing",
    ]
    assert tokens["fidelity"] == "approximate"
    exact = fmt(
        "clusteredColumnChart", {"dataPoint": objects["dataPoint"]}, {"title": container["title"]}
    )
    assert exact["unrendered"] == [] and exact["fidelity"] == "exact"


def test_rendered_table_answers_the_format_pane():
    assert property_is_rendered("clusteredColumnChart", "labels", "fontSize")
    assert property_is_rendered("clusteredColumnChart", "title", "fontSize", container=True)
    assert property_is_rendered("clusteredColumnChart", "title", "fontSize")
    assert not property_is_rendered("clusteredColumnChart", "categoryAxis", "innerPadding")
    assert not property_is_rendered("tableEx", "labels", "fontSize")
    assert property_is_rendered("cardVisual", "fillCustom", "fillColor", container=False)
    assert not property_is_rendered("cardVisual", "title", "fontSize", container=False)
    assert not property_is_rendered("gauge", "labels", "fontSize")
    assert property_is_rendered("gauge", "background", "color", container=True)  # the frame
    assert unrendered_properties("gauge", {"objects": {"axis": [entry({"min": lit("0D")})]}}) == [
        "axis.min"
    ]


def test_unsupported_types_are_framed_and_none():
    tokens = fmt(
        "gauge",
        {"axis": [entry({"min": lit("0D")})]},
        {"background": [entry({"color": solid("#EEEEEE")})]},
    )
    assert tokens["fidelity"] == "none"
    assert tokens["container"]["background"] == "#eeeeeeff"
    assert tokens["unrendered"] == ["axis.min"]


def test_group_format_has_no_box_of_its_own():
    visual = ImportedVisual(
        id="g",
        title="Group",
        visual_type="visualGroup",
        container_kind="group",
        definition={
            "visualGroup": {
                "displayName": "Group",
                "objects": {
                    "background": [
                        entry(
                            {
                                "show": lit("true"),
                                "color": solid("#EEEEEE"),
                                "transparency": lit("50D"),
                            }
                        )
                    ]
                },
            }
        },
    )
    tokens = rf.visual_format(NoFields(), visual, [], {}, theme_dict())
    assert tokens["container"]["background"] == "#eeeeee80" and tokens["fidelity"] == "exact"
    bare = visual.model_copy(update={"definition": {"visualGroup": {"displayName": "Group"}}})
    assert (
        rf.visual_format(NoFields(), bare, [], {}, theme_dict())["container"]["background"] is None
    )


# --- page style -----------------------------------------------------------------------------


def test_page_style_reads_alpha_wallpaper_and_the_custom_theme(tmp_path):
    report = theme_report()
    page = {
        "name": "p",
        "objects": {
            "background": [entry({"color": themed(2), "transparency": lit("100D")})],
            "outspace": [entry({"color": solid("#101010"), "transparency": lit("25D")})],
        },
    }
    path = write_report(
        tmp_path / "p.pbix",
        report,
        {
            "Report/StaticResources/SharedResources/BaseThemes/Base.json": BASE,
            "Report/StaticResources/RegisteredResources/Mine.json": CUSTOM,
            "Report/definition/pages/p/page.json": page,
        },
    )
    style, palette = rf.page_style(str(path), "page-hash", "p")
    assert palette[:4] == ["#ffffff", "#000000", "#aa0000", "#00aa00"]  # the custom theme's colors
    assert style["color"] == "#aa0000"  # ColorId 2 = dataColors[0] of the custom theme
    assert style["alpha"] == 0.0
    assert style["wallpaper"] == {"color": "#101010", "alpha": 0.75}


def test_page_without_a_background_takes_the_themes_transparency(tmp_path):
    base = {**BASE, "visualStyles": {"page": {"*": {"background": [{"transparency": 100}]}}}}
    path = write_report(
        tmp_path / "q.pbix",
        theme_report(),
        {
            "Report/StaticResources/SharedResources/BaseThemes/Base.json": base,
            "Report/StaticResources/RegisteredResources/Mine.json": {"dataColors": ["#AA0000"]},
            "Report/definition/pages/p/page.json": {"name": "p"},
        },
    )
    style, _ = rf.page_style(str(path), "page-hash-2", "p")
    assert style["alpha"] == 0.0 and style["color"] == "#ffffff"
    assert style["wallpaper"] == {"color": "#ffffff", "alpha": 1.0}


def test_model_formats_are_keyed_by_the_model_not_the_file(tmp_path, monkeypatch):
    calls: list[str] = []

    class Done:
        returncode = 0
        stdout = json.dumps([{"TableName": "T", "Name": "Sales", "FormatString": "0.0"}])

    def fake_run(argv: list[str], **kwargs: Any) -> Done:
        calls.append(argv[-2])
        return Done()

    monkeypatch.setattr(rf.subprocess, "run", fake_run)
    rf._FORMATS.clear()
    model = b"the same model bytes" * 10
    first = write_report(tmp_path / "r1.pbix", {}, {}, extra=model)
    second = write_report(tmp_path / "r2.pbix", {"edited": True}, {}, extra=model)
    other = write_report(tmp_path / "r3.pbix", {}, {}, extra=b"a different model" * 10)
    assert rf.model_formats(str(first), "h1") == {("T", "Sales"): "0.0"}
    assert rf.model_formats(str(second), "h2") == {("T", "Sales"): "0.0"}  # a report-layer revision
    assert len(calls) == 1
    rf.model_formats(str(other), "h3")
    assert len(calls) == 2
    nomodel = write_report(tmp_path / "r4.pbix", {}, {})
    assert rf.model_formats(str(nomodel), "h4") == {}
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["slicer", "advancedSlicerVisual"])
def test_slicer_header_reads_text_size(kind):
    objects = {"header": [entry({"textSize": lit("18D"), "text": lit("'Country'")})]}
    header = fmt(kind, objects, fields=make_fields(("Values", "column", "Country")))["slicer"][
        "header"
    ]
    assert header["fontSize"] == 24.0 and header["text"] == "Country"


def test_label_like_and_free_text_values_are_validated_and_bounded():
    hostile = 'x"><script>alert(1)</script>'
    tokens = fmt(
        "slicer",
        {"data": [entry({"mode": lit(f"'{hostile}'")})]},
        fields=make_fields(("Values", "column", "Country")),
    )
    assert tokens["slicer"]["mode"] == "dropdown"
    chart = fmt(
        "clusteredColumnChart",
        {"labels": [entry({"labelStyle": lit(f"'{hostile}'"), "labelPosition": lit("'Nowhere'")})]},
    )
    assert chart["labels"]["style"] is None and chart["labels"]["position"] is None
    long_title = {"title": [entry({"text": lit("'" + "a" * 5000 + "'")})]}
    assert len(fmt("clusteredColumnChart", container=long_title)["title"]["text"]) == 2000
    legend = fmt("clusteredColumnChart", {"legend": [entry({"position": lit("'Diagonal'")})]})
    assert legend["legend"]["position"] == "top"


# --- shapes use Desktop's own names ---------------------------------------------------------


def shape_of(name: str, **extra: str) -> dict[str, Any]:
    props = {"tileShape": lit(f"'{name}'"), **{k: lit(v) for k, v in extra.items()}}
    return fmt("shape", {"shape": [entry(props, id="default")]}, theme=theme_dict())


def test_shape_names_are_the_ones_desktop_writes():
    catalog = json.loads(
        (Path(rf.__file__).with_name("pbir_catalog.json")).read_text(encoding="utf-8")
    )
    names = catalog["visualTypes"]["shape"]["objects"]["shape"]["tileShape"]["values"]
    assert list(SHAPE_NAMES) == names


def test_a_rounded_rectangle_is_drawn_and_exact():
    tokens = shape_of("rectangleRounded", roundEdge="20D")
    assert tokens["shape"]["shape"] == "rectangleRounded"
    assert tokens["shape"]["tileShape"] == "rectangleRounded" and tokens["shape"]["radius"] == 20.0
    assert tokens["fidelity"] == "exact" and tokens["unrendered"] == []
    # Match without regard to case, as Desktop reads the enum.
    assert shape_of("OVAL")["shape"]["shape"] == "oval"
    pill = shape_of("pill")
    assert pill["shape"]["shape"] == "pill" and pill["fidelity"] == "exact"


@pytest.mark.parametrize("name", ["triangleIsoc", "octagon", "arrow", "speechbubbleRectangle"])
def test_a_shape_the_canvas_cannot_draw_is_a_rectangle_and_approximate(name):
    tokens = shape_of(name)
    assert tokens["shape"]["shape"] == "rectangle" and tokens["shape"]["tileShape"] == name
    assert tokens["fidelity"] == "approximate"


def test_a_shape_name_desktop_does_not_have_is_approximate_and_null():
    tokens = shape_of("rightarrow")
    assert tokens["shape"]["shape"] == "rectangle" and tokens["shape"]["tileShape"] is None
    assert tokens["fidelity"] == "approximate"
    assert shape_of("rectangle")["fidelity"] == "exact"


# --- a property scoped to one series or column is not drawn from the unscoped value ---------


def test_a_metadata_scoped_chart_property_is_not_drawn():
    scoped = {"labels": [entry({"show": lit("true"), "color": solid("#FF0000")}, metadata="T.M")]}
    body = {"objects": scoped}
    assert unrendered_properties("clusteredColumnChart", body) == ["labels.color", "labels.show"]
    tokens = fmt("clusteredColumnChart", scoped)
    assert tokens["labels"]["show"] is False  # the token reads only the unscoped value
    assert tokens["fidelity"] == "approximate"
    assert tokens["unrendered"] == ["labels.color", "labels.show"]
    unscoped = {
        "labels": [entry({"show": lit("true")}), entry({"color": solid("#FF0000")}, id="default")]
    }
    assert fmt("clusteredColumnChart", unscoped)["fidelity"] == "exact"


def test_a_metadata_scoped_table_font_is_not_drawn_but_the_rule_and_column_entries_are():
    literal_font = {"values": [entry({"fontColor": solid("#FF0000")}, metadata="T.C")]}
    tokens = fmt("tableEx", literal_font)
    assert tokens["table"]["values"]["color"] != "#ff0000"
    assert tokens["unrendered"] == ["values.fontColor"]
    wildcard = {"dataViewWildcard": {"matchingOption": 1}}
    rule = {"solid": {"color": {"expr": {"Measure": {"Property": "Profit"}}}}}
    resolved = {
        "values": [entry({"fontColor": rule}, data=[wildcard], metadata="T.C")],
        "columnFormatting": [entry({"alignment": lit("'Right'")}, metadata="T.C")],
        "columnWidth": [entry({"value": lit("120D")}, metadata="T.C")],
    }
    assert unrendered_properties("tableEx", {"objects": resolved}) == []
    # A wildcard-free data selector on the same property is a single cell: not drawn.
    cell = {"values": [entry({"fontColor": solid("#FF0000")}, data=["1L"], metadata="T.C")]}
    assert unrendered_properties("tableEx", {"objects": cell}) == ["values.fontColor"]


def test_card_and_slicer_scopes_that_tokens_resolve_are_not_flagged():
    card = {
        "value": [entry({"fontSize": lit("40D")}, metadata="T.A"), entry({}, id="default")],
        "label": [entry({"show": lit("false")}, metadata="T.A")],
        "fillCustom": [entry({"fillColor": solid("#FFFFFF")}, id="default")],
    }
    assert unrendered_properties("cardVisual", {"objects": card}) == []
    # A selected card is not drawn differently; a selected slicer tile is.
    selected = {"fillCustom": [entry({"fillColor": solid("#FFFFFF")}, id="selection:selected")]}
    assert unrendered_properties("cardVisual", {"objects": selected}) == ["fillCustom.fillColor"]
    assert unrendered_properties("advancedSlicerVisual", {"objects": selected}) == []
    tile = {"value": [entry({"fontSize": lit("9D")}, metadata="T.A")]}
    assert unrendered_properties("advancedSlicerVisual", {"objects": tile}) == ["value.fontSize"]


def test_data_point_fill_scopes_are_all_resolved():
    scopes = {
        "dataPoint": [
            entry({"fill": solid("#111111")}, metadata="T.Sales"),
            entry({"fill": solid("#222222")}, data=[{"dataViewWildcard": {"matchingOption": 1}}]),
        ]
    }
    assert unrendered_properties("clusteredColumnChart", {"objects": scopes}) == []


# --- fonts in any script ---------------------------------------------------------------------


def test_font_names_keep_letters_of_any_script_and_still_reject_markup():
    assert css_font_family("'微软雅黑', 'Segoe UI'") == '"微软雅黑", "Segoe UI"'
    assert css_font_family("メイリオ") == '"メイリオ"'
    assert css_font_family("Arial, sans-serif") == "Arial, sans-serif"
    assert css_font_family("3270 Nerd") == '"3270 Nerd"'
    hostile = 'a"; } body { x: y'
    assert css_font_family(hostile) is None
    assert css_font_family("Font(1), <b>x</b>, url(http://x)") is None
    assert css_font_family("Segoe UI (Body)") is None


# --- images ----------------------------------------------------------------------------------


def picture_of(node: dict[str, Any] | None, tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "pic.pbix"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Report/definition/report.json", json.dumps({"resourcePackages": []}))
    context = rf.format_context(str(path), "pic-hash")
    objects = {"general": [entry({"imageUrl": node})]} if node is not None else {}
    visual = ImportedVisual(
        id="i",
        title="i",
        visual_type="image",
        definition={"visual": {"visualType": "image", "objects": objects}},
    )
    return rf.visual_format(NoFields(), visual, [], {}, None, context=context)


def test_an_image_that_cannot_be_drawn_is_approximate_and_one_that_can_is_exact(tmp_path):
    png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 16).decode()
    drawn = picture_of(lit(f"'{png}'"), tmp_path)
    assert drawn["picture"]["src"] == png and drawn["fidelity"] == "exact"
    # The claimed type never decides: bytes that are not an image are refused.
    fake = "data:image/png;base64," + base64.b64encode(b"<html>x</html>").decode()
    svg_script = base64.b64encode(
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    ).decode()
    cases = [
        lit("'https://example.com/x.png'"),  # a web address is never fetched
        lit(f"'{fake}'"),
        lit(f"'data:image/svg+xml;base64,{svg_script}'"),
        {"expr": {"Measure": {"Property": "Logo"}}},  # driven by data
        {
            "expr": {
                "ResourcePackageItem": {"PackageName": "RegisteredResources", "ItemName": "gone"}
            }
        },
    ]
    for node in cases:
        result = picture_of(node, tmp_path)
        assert result["picture"]["src"] is None, node
        assert result["fidelity"] == "approximate", node
    assert picture_of(None, tmp_path)["fidelity"] == "exact"


# --- a malformed visual never fails the page -------------------------------------------------


def test_malformed_selectors_and_scopes_are_dropped_not_raised():
    bad = {
        "dataPoint": [
            entry({"fill": solid("#111111")}),
            {"properties": {"fill": solid("#222222")}, "selector": "oops"},
            {"properties": {"fill": solid("#333333")}, "selector": {"data": "oops", "metadata": 3}},
            {
                "properties": {"fill": solid("#444444")},
                "selector": {"data": [{"scopeId": {"Comparison": {"Left": "x", "Right": 1}}}]},
            },
            {
                "properties": {"fill": solid("#555555")},
                "selector": {
                    "data": [
                        {
                            "scopeId": {
                                "Comparison": {
                                    "ComparisonKind": 0,
                                    "Left": {"Column": {"Expression": {"SourceRef": "s"}}},
                                    "Right": {"Literal": {"Value": "'A'"}},
                                }
                            }
                        }
                    ]
                },
            },
            "not an entry",
        ]
    }
    tokens = fmt("clusteredColumnChart", bad)
    assert tokens["series"]["defaultColor"] == "#111111"
    assert "labels.show" not in tokens["unrendered"]
    # The dropped entries were set but never drawn, and the signal says so.
    assert unrendered_properties("clusteredColumnChart", {"objects": bad}) == ["dataPoint.fill"]
