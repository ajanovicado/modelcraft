"""The conditional family on the Agents twin: conditional formatting, number formats, columns and
table layout, written the way Power BI Desktop writes them.

The twin keeps the real report's Desktop-authored conditional formatting (four ``Measure`` font
colors on the table and data bars on its unnamed ``vs Previous Month`` column), so a clear and a
set of the same thing must give back Desktop's own JSON.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from agents_twin_model import (
    REAL_AGENTS,
    TWIN_COLUMN_TYPES,
    TWIN_MEASURE_TYPES,
    real_model,
    real_types,
    twin_model,
)

from ai_powerbi_analyst.studio import edit_tools, report_edits
from ai_powerbi_analyst.studio import pbir_expr as expr
from ai_powerbi_analyst.studio import report_edit_conditional as cf
from ai_powerbi_analyst.studio.pbir_document import DocumentError, PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_captions import DYNAMIC_TITLE, visible_caption
from ai_powerbi_analyst.studio.report_edit_context import (
    ModelInfo,
    NeedsChoice,
    NeedsConfirmation,
    VisibleIn,
)
from ai_powerbi_analyst.studio.report_edit_validation import capture_baseline, validate_document
from ai_powerbi_analyst.studio.report_tokens import property_is_rendered

TWIN = Path(__file__).resolve().parents[2] / "fixtures" / "imported" / "structure-twin.pbix"
TABLE = "e82e02f93055d6b0b544"
CHART = "b404a461ede7604a2774"  # 'Total MTD Sales', per-category TopN/BottomN/Others fills
CHART_2 = "da32cdbb0090e885649b"  # 'Avg MTD Sales by Agents', a plain fill too
CARD = "fb4c5aa34e0b084b62b9"  # 'Agents with Positive Change %'
TITLED_CARD = "8a8ba163374a72762030"  # 'TopN/BottomN', a title with a fixed font color
M = "# Measures"
WILDCARD = {"data": [{"dataViewWildcard": {"matchingOption": 1}}]}

MEASURES = list(TWIN_MEASURE_TYPES)
# A synthetic Sales table some tests add to the twin (not in the Agents model), with its import
# rows and its types: Amount is a fixed decimal, which pandas cannot say.
SALES_COLUMNS = [
    ("Sales", "Units", "int64"),
    ("Sales", "Amount", "object"),
    ("Sales", "Price", "float64"),
    ("Sales", "OrderDate", "datetime64[ns]"),
    ("Sales", "Region", "object"),
]
SALES_TYPES = [
    {"table": "Sales", "name": "Units", "dataType": 6, "summarizeBy": "sum"},
    {"table": "Sales", "name": "Amount", "dataType": 10, "summarizeBy": "sum"},
    {"table": "Sales", "name": "Price", "dataType": 8, "summarizeBy": "average"},
    {"table": "Sales", "name": "OrderDate", "dataType": 9, "summarizeBy": "none"},
    {"table": "Sales", "name": "Region", "dataType": 2, "summarizeBy": "none"},
]


def model() -> ModelInfo:
    """The twin's model as the editor builds it (ReportEditor._model_info), with the real
    model's data types: MTD Sales is text, Rank a whole number, vs PM % a variant."""
    return twin_model(
        [{"TableName": t, "ColumnName": c, "PandasDataType": d} for t, c, d in SALES_COLUMNS],
        SALES_TYPES,
    )


def twin() -> PbirDocument:
    return PbirDocument(PbixPackage(TWIN))


def run(document: PbirDocument, *operations: dict[str, Any]) -> Any:
    return cf.apply_conditional(document, cf.parse_conditional(list(operations)), model())


def run_clean(document: PbirDocument, *operations: dict[str, Any]) -> Any:
    """Apply and hold the result to the validator: only what the edit introduced counts."""
    baseline = capture_baseline(document, model())
    outcome = run(document, *operations)
    assert validate_document(document, model(), baseline=baseline) == []
    return outcome


def body(document: PbirDocument, visual: str) -> dict[str, Any]:
    found: dict[str, Any] = document.visual(visual)[1]["visual"]
    return found


def entry(
    document: PbirDocument,
    visual: str,
    object_name: str,
    selector: dict[str, Any] | None,
    where: str = "objects",
) -> dict[str, Any]:
    for item in body(document, visual).get(where, {}).get(object_name, []):
        if (item.get("selector") or None) == selector:
            return dict(item)
    raise AssertionError(f"no {object_name} entry under {selector}")


def column(query_ref: str) -> dict[str, Any]:
    return {**WILDCARD, "metadata": query_ref}


def measure(name: str) -> dict[str, Any]:
    return {"Measure": {"Expression": {"SourceRef": {"Entity": M}}, "Property": name}}


def literal(text: str) -> dict[str, Any]:
    return {"Literal": {"Value": text}}


def fill(inner: dict[str, Any]) -> dict[str, Any]:
    return {"solid": {"color": {"expr": inner}}}


def range_percent(source: dict[str, Any], percent: float) -> dict[str, Any]:
    def over(function: int) -> dict[str, Any]:
        per_row = {"ScopedEval": {"Expression": source, "Scope": [{"AllRolesRef": {}}]}}
        return {
            "ScopedEval": {
                "Expression": {"Aggregation": {"Expression": per_row, "Function": function}},
                "Scope": [],
            }
        }

    return {"RangePercent": {"Min": over(3), "Max": over(4), "Percent": percent}}


def add_visual(
    document: PbirDocument, name: str, visual_type: str, roles: dict[str, list[dict[str, Any]]]
) -> None:
    """A visual built in memory on the twin's page, with the fields the typed tests need."""
    page = next(iter(document.pages.values()))
    page.visuals[name] = {
        "$schema": document.schema("visualContainer"),
        "name": name,
        "position": {"x": 10, "y": 10, "z": 90000, "width": 300, "height": 200, "tabOrder": 90000},
        "visual": {
            "visualType": visual_type,
            "query": {"queryState": {r: {"projections": p} for r, p in roles.items()}},
        },
    }
    document.touch_visual(page, name)


def sales_column(name: str) -> dict[str, Any]:
    return {
        "field": expr.column_ref("Sales", name),
        "queryRef": f"Sales.{name}",
        "nativeQueryRef": name,
    }


def sales_sum(name: str, function: int = 0, word: str = "Sum") -> dict[str, Any]:
    return {
        "field": {
            "Aggregation": {"Expression": expr.column_ref("Sales", name), "Function": function}
        },
        "queryRef": f"{word}(Sales.{name})",
        "nativeQueryRef": f"{word} of {name}",
    }


def sales_table(document: PbirDocument) -> None:
    add_visual(
        document,
        "salestable0000000001",
        "tableEx",
        {
            "Values": [
                sales_column("Region"),
                sales_column("OrderDate"),
                sales_sum("Units"),
                sales_sum("Amount"),
                sales_sum("Price"),
            ]
        },
    )


# --- field values: Desktop's own shape ---------------------------------------------------------


def test_a_field_value_font_color_is_exactly_the_shape_desktop_wrote() -> None:
    document = twin()
    original = entry(document, TABLE, "values", column("DimEmployee.EmployeeKey"))
    run(document, {"op": "clear_conditional_format", "visual": TABLE, "field": "Agent ID"})
    with pytest.raises(AssertionError):
        entry(document, TABLE, "values", column("DimEmployee.EmployeeKey"))
    outcome = run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "field_value",
            "target": "font_color",
            "field": "Agent ID",
            "measure": {"table": M, "name": "CF Table"},
        },
    )
    assert entry(document, TABLE, "values", column("DimEmployee.EmployeeKey")) == original
    assert outcome.summaries == [
        "Set the font color of the Agent ID column in the table of Agent ID to follow the "
        "measure [CF Table]."
    ]
    drawn = property_is_rendered("tableEx", "values", "fontColor")
    assert outcome.visible_in == [
        VisibleIn(
            "set_conditional_format", "studio" if drawn else "powerbi_only", "values.fontColor"
        )
    ]


def test_a_field_value_can_name_the_measure_as_text_or_a_column_first_value() -> None:
    document = twin()
    run(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "field_value",
            "target": "background",
            "field": "Agent Name",
            "measure": "'# Measures'[CF Table]",
        },
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "field_value",
            "target": "web_url",
            "field": "Agent Name",
            "measure": "Clustered Employees[Clustered Employees]",
            "summarization": "last",
        },
    )
    cell = entry(document, TABLE, "values", column("# Measures.Employee Name"))["properties"]
    assert cell["backColor"] == fill(measure("CF Table"))
    assert cell["webURL"] == {
        "expr": {
            "Aggregation": {
                "Expression": expr.column_ref("Clustered Employees", "Clustered Employees"),
                "Function": 4,
            }
        }
    }


# --- data bars: Desktop's own shape ------------------------------------------------------------


def test_data_bars_are_exactly_the_shape_desktop_wrote() -> None:
    document = twin()
    selector = {"metadata": "# Measures.vs PM"}
    original = entry(document, TABLE, "columnFormatting", selector)["properties"]["dataBars"]
    cleared = run(
        document,
        {"op": "clear_conditional_format", "visual": TABLE, "target": "data_bars"},
    )
    assert cleared.summaries == [
        "Removed the data bars of the vs Previous Month column in the table of Agent ID; the "
        "plain formatting shows again."
    ]
    # The entry held only the bars, so it is gone with them.
    with pytest.raises(AssertionError):
        entry(document, TABLE, "columnFormatting", selector)
    run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "data_bars",
            "field": "vs Previous Month",
            "positive_color": "#1E90FF",
            "negative_color": "#DC143C",
            "axis_color": "#b3b3b3",
            "direction": "left_to_right",
            "bar_only": True,
        },
    )
    produced = entry(document, TABLE, "columnFormatting", selector)["properties"]["dataBars"]
    expected = copy.deepcopy(original)
    expected["axisColor"] = fill(literal("'#B3B3B3'"))  # Studio writes hex in capitals
    assert produced == expected
    assert list(produced) == list(original)


def test_changing_part_of_existing_data_bars_keeps_the_rest_without_asking() -> None:
    document = twin()
    selector = {"metadata": "# Measures.vs PM"}
    before = entry(document, TABLE, "columnFormatting", selector)["properties"]["dataBars"]
    outcome = run(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "data_bars",
            "field": "vs Previous Month",
            "bar_only": False,
            "max_value": 1000,
        },
    )
    after = entry(document, TABLE, "columnFormatting", selector)["properties"]["dataBars"]
    assert after == {
        "maxValue": {"expr": literal("1000D")},
        **{k: v for k, v in before.items() if k != "hideText"},
        "hideText": {"expr": literal("false")},
    }
    assert outcome.replaced == []
    assert outcome.summaries[0].startswith("Updated data bars on the vs Previous Month column")


def test_new_data_bars_start_from_the_theme_and_merge_into_the_columns_entry() -> None:
    document = twin()
    run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "data_bars",
            "field": "Rank",
            "min_value": 0,
            "apply_to": "values_and_totals",
        },
    )
    found = entry(document, TABLE, "columnFormatting", {"metadata": "# Measures.Rank"})
    assert found["properties"] == {
        "styleHeader": {"expr": literal("true")},
        "alignment": {"expr": literal("'Center'")},
        "dataBars": {
            "minValue": {"expr": literal("0L")},  # Rank is a whole number
            "positiveColor": fill(literal("'#118DFF'")),
            "negativeColor": fill(literal("'#DEEFFF'")),
            "axisColor": fill(literal("'#252423'")),
            "reverseDirection": {"expr": literal("false")},
            "hideText": {"expr": literal("false")},
            "totalMatchingOption": {"expr": literal("0L")},
        },
    }


# --- color scales ------------------------------------------------------------------------------


def test_a_two_color_scale_with_numbers_and_a_blank_color() -> None:
    document = twin()
    outcome = run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "gradient",
            "target": "background",
            "field": "Rank",
            "min_color": "#ffffff",
            "max_color": {"theme": 2},
            "min_value": 0,
            "max_value": 250.5,
            "blank": "specific_color",
            "blank_color": "#C8C6C4",
        },
    )
    cell = entry(document, TABLE, "values", column("# Measures.Rank"))["properties"]
    assert cell["backColor"] == fill(
        {
            "FillRule": {
                "Input": measure("Rank"),
                "FillRule": {
                    "linearGradient2": {
                        # a scale's stops are doubles even on a whole-number field
                        "min": {"color": literal("'#FFFFFF'"), "value": literal("0D")},
                        "max": {"color": literal("'#118DFF'"), "value": literal("250.5D")},
                        "nullColoringStrategy": {
                            "strategy": literal("'specificColor'"),
                            "color": literal("'#C8C6C4'"),
                        },
                    }
                },
            }
        }
    )
    # The Desktop-authored font color rule in the same entry is untouched.
    assert cell["fontColor"] == fill(measure("CF Table"))
    assert outcome.summaries == [
        "Set the background of the Rank column in the table of Agent ID to follow a color "
        "scale on Rank, #FFFFFF (at 0) to #118DFF (at 250.5)."
    ]


def test_stop_colors_are_bare_literals_never_wrapped_in_expr() -> None:
    """Microsoft's validator (PBIR_FILLRULE_STOP_DOUBLE_WRAP): a wrapped stop fails to load."""
    document = twin()
    run(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "gradient",
            "target": "font_color",
            "field": "vs Previous Month",
            "mid_color": "#FFFFFF",
        },
    )
    cell = entry(document, TABLE, "values", column("# Measures.vs PM"))["properties"]
    gradient = cell["fontColor"]["solid"]["color"]["expr"]["FillRule"]["FillRule"]
    stops = gradient["linearGradient3"]
    assert [name for name in stops] == ["min", "mid", "max", "nullColoringStrategy"]
    for name in ("min", "mid", "max"):
        assert "expr" not in stops[name]["color"]
        assert "value" not in stops[name]  # lowest, middle and highest value
    # Desktop's defaults: the theme's minimum, center and maximum colors, blanks as zero.
    assert [stops[n]["color"] for n in ("min", "mid", "max")] == [
        literal("'#DEEFFF'"),
        literal("'#FFFFFF'"),
        literal("'#118DFF'"),
    ]
    assert stops["nullColoringStrategy"] == {"strategy": literal("'asZero'")}


def test_changing_one_end_of_a_scale_keeps_the_other_and_asks_nothing() -> None:
    document = twin()
    first = {
        "op": "set_conditional_format",
        "visual": TABLE,
        "style": "gradient",
        "target": "background",
        "field": "Rank",
        "min_color": "#FF0000",
        "max_value": 900,
    }
    run(document, first)
    outcome = run(document, {**first, "min_color": None, "max_color": "#00FF00"})
    cell = entry(document, TABLE, "values", column("# Measures.Rank"))["properties"]
    stops = cell["backColor"]["solid"]["color"]["expr"]["FillRule"]["FillRule"]["linearGradient2"]
    assert stops["min"] == {"color": literal("'#FF0000'")}
    assert stops["max"] == {"color": literal("'#00FF00'"), "value": literal("900D")}
    assert outcome.summaries[0].startswith("Updated the background of the Rank column")


def test_a_scale_on_a_card_needs_numbers_and_one_on_text_is_refused() -> None:
    document = twin()
    with pytest.raises(DocumentError) as raised:
        run(document, {"op": "set_conditional_format", "visual": CARD, "style": "gradient"})
    assert str(raised.value) == (
        "This visual shows a single value, so there is no lowest or highest value: give "
        "min_value and max_value"
    )
    sales_table(document)
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": "salestable0000000001",
                "style": "gradient",
                "target": "background",
                "field": "Region",
            },
        )
    assert str(raised.value) == "A color scale follows numbers, and Region holds text"


# --- rules -------------------------------------------------------------------------------------


def test_number_rules_are_conditional_cases_with_double_thresholds() -> None:
    document = twin()
    outcome = run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "rules",
            "target": "font_color",
            "field": "vs PM %",
            "rules": [
                {"max": 0, "color": "#D64550"},
                {
                    "min": 0,
                    "max": 0.5,
                    "min_inclusive": False,
                    "max_inclusive": True,
                    "color": "#FFFFFF",
                },
                {"blank": True, "color": "#C8C6C4"},
                {"color": "#1AAB40"},
            ],
            "replace_conditional": True,
        },
    )
    source = measure("vs PM MOM%")
    cell = entry(document, TABLE, "values", column("# Measures.vs PM MOM%"))["properties"]
    assert cell["fontColor"] == fill(
        {
            "Conditional": {
                "Cases": [
                    {
                        "Condition": {
                            "Comparison": {
                                "ComparisonKind": 3,
                                "Left": source,
                                "Right": literal("0D"),
                            }
                        },
                        "Value": literal("'#D64550'"),
                    },
                    {
                        "Condition": {
                            "And": {
                                "Left": {
                                    "Comparison": {
                                        "ComparisonKind": 1,
                                        "Left": source,
                                        "Right": literal("0D"),
                                    }
                                },
                                "Right": {
                                    "Comparison": {
                                        "ComparisonKind": 4,
                                        "Left": source,
                                        "Right": literal("0.5D"),
                                    }
                                },
                            }
                        },
                        "Value": literal("'#FFFFFF'"),
                    },
                    {
                        "Condition": {
                            "Comparison": {
                                "ComparisonKind": 0,
                                "Left": source,
                                "Right": literal("null"),
                            }
                        },
                        "Value": literal("'#C8C6C4'"),
                    },
                    {"Condition": literal("true"), "Value": literal("'#1AAB40'")},
                ]
            }
        }
    )
    assert outcome.replaced == [
        "the measure [Rank Filtering Employyees MTD% CF] on font color of the vs PM % column"
    ]


def test_percent_rules_measure_the_range_like_desktop_and_leave_out_0_and_100() -> None:
    document = twin()
    run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "rules",
            "target": "background",
            "field": "Rank",
            "rules": [
                {"min": 0, "max": 50, "unit": "percent", "color": "#F8696B"},
                {
                    "min": 50,
                    "max": 100,
                    "max_inclusive": True,
                    "unit": "percent",
                    "color": "#63BE7B",
                },
            ],
        },
    )
    source = measure("Rank")
    cell = entry(document, TABLE, "values", column("# Measures.Rank"))["properties"]
    cases = cell["backColor"]["solid"]["color"]["expr"]["Conditional"]["Cases"]
    assert cases == [
        {
            "Condition": {
                "Comparison": {
                    "ComparisonKind": 3,
                    "Left": source,
                    "Right": range_percent(source, 0.5),
                }
            },
            "Value": literal("'#F8696B'"),
        },
        {
            "Condition": {
                "Comparison": {
                    "ComparisonKind": 2,
                    "Left": source,
                    "Right": range_percent(source, 0.5),
                }
            },
            "Value": literal("'#63BE7B'"),
        },
    ]


def test_rules_on_a_card_need_numbers() -> None:
    document = twin()
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": CARD,
                "style": "rules",
                "rules": [{"min": 50, "unit": "percent", "color": "#1AAB40"}],
            },
        )
    assert str(raised.value) == (
        "Rule 1: This visual shows a single value, so there is no range for a percent: give the "
        "rule numbers"
    )


def test_rules_are_checked_before_anything_is_written() -> None:
    document = twin()
    before = copy.deepcopy(body(document, TABLE))
    cases = [
        ({"min": 5, "max": 5, "color": "#FF0000"}, "Rule 1: A rule from 5 to 5 matches nothing"),
        (
            {"min": 120, "unit": "percent", "color": "#FF0000"},
            "A percent runs from 0 to 100, not 120",
        ),
        ({"icon": "StarHigh"}, "Rule 1: a color rule names a color, not an icon"),
        (
            {"blank": True, "min": 1, "color": "#FF0000"},
            "Rule 1: A rule for blank values takes no numbers",
        ),
    ]
    for rule, message in cases:
        with pytest.raises(DocumentError) as raised:
            run(
                document,
                {
                    "op": "set_conditional_format",
                    "visual": TABLE,
                    "style": "rules",
                    "target": "background",
                    "field": "Rank",
                    "rules": [rule],
                },
            )
        assert message in str(raised.value)
    assert body(document, TABLE) == before


# --- icons -------------------------------------------------------------------------------------


def test_an_icon_set_splits_the_range_like_desktop_highest_rule_first() -> None:
    document = twin()
    run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "icons",
            "field": "vs PM %",
            "icon_set": "ThreeColoredArrows",
            "icon_position": "right",
            "icon_alignment": "middle",
        },
    )
    source = measure("vs PM MOM%")
    icon = entry(document, TABLE, "values", column("# Measures.vs PM MOM%"))["properties"]["icon"]

    def compare(kind: int, percent: float) -> dict[str, Any]:
        return {
            "Comparison": {
                "ComparisonKind": kind,
                "Left": source,
                "Right": range_percent(source, percent),
            }
        }

    assert icon == {
        "kind": "Icon",
        "layout": {"expr": literal("'After'")},
        "verticalAlignment": {"expr": literal("'Middle'")},
        "value": {
            "expr": {
                "Conditional": {
                    "Cases": [
                        {"Condition": compare(2, 0.67), "Value": literal("'ColoredArrowUp'")},
                        {
                            "Condition": {
                                "And": {"Left": compare(2, 0.33), "Right": compare(3, 0.67)}
                            },
                            "Value": literal("'ColoredArrowRight'"),
                        },
                        {"Condition": compare(3, 0.33), "Value": literal("'ColoredArrowDown'")},
                    ]
                }
            }
        },
    }


def test_icon_rules_name_icons_and_moving_icons_keeps_their_rules() -> None:
    document = twin()
    run(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "rules",
            "target": "icon",
            "field": "Rank",
            "rules": [{"max": 4, "icon": "starhigh"}, {"min": 4, "icon": "StarLow"}],
        },
    )
    icon = entry(document, TABLE, "values", column("# Measures.Rank"))["properties"]["icon"]
    cases = icon["value"]["expr"]["Conditional"]["Cases"]
    assert [c["Value"] for c in cases] == [literal("'StarHigh'"), literal("'StarLow'")]
    assert icon["layout"] == {"expr": literal("'Before'")}
    outcome = run(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "icons",
            "field": "Rank",
            "icon_position": "icon_only",
        },
    )
    moved = entry(document, TABLE, "values", column("# Measures.Rank"))["properties"]["icon"]
    assert moved["layout"] == {"expr": literal("'IconOnly'")}
    assert moved["value"] == icon["value"]
    assert outcome.replaced == []
    assert outcome.summaries == ["Moved the icons of the Rank column in the table of Agent ID."]
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": TABLE,
                "style": "rules",
                "target": "icon",
                "field": "Rank",
                "rules": [{"icon": "Unicorn"}],
            },
        )
    assert "Power BI has no icon called 'Unicorn'" in str(raised.value)


# --- charts, cards, titles and backgrounds -----------------------------------------------------


def test_chart_data_colors_go_under_the_instances_wildcard_and_name_kept_categories() -> None:
    document = twin()
    outcome = run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": CHART,
            "style": "gradient",
            "min_color": "#C8C6C4",
            "max_color": "#12239E",
        },
    )
    rule = entry(document, CHART, "dataPoint", WILDCARD)["properties"]["fill"]
    assert rule["solid"]["color"]["expr"]["FillRule"]["Input"] == measure("TopN/BottomN/ MTD Sales")
    assert outcome.warnings == [
        "The 'Total MTD Sales' stacked column chart also colors Others, TopN, BottomN one by one; "
        "those colors were kept, and Power BI may show them instead on those bars."
    ]
    # One series of the chart: the wildcard names its field.
    run(
        document,
        {
            "op": "set_conditional_format",
            "visual": CHART_2,
            "style": "rules",
            "field": "TopN/BottomN/ Avg MTD Sales",
            "rules": [{"min": 50000, "color": "#1AAB40"}],
        },
    )
    entry(document, CHART_2, "dataPoint", column("# Measures.TopN/BottomN/ Avg MTD Sales"))
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": CHART_2,
                "style": "rules",
                "field": "Clustered Employees",
                "rules": [{"min": 1, "color": "#1AAB40"}],
            },
        )
    assert str(raised.value) == (
        "Clustered Employees is what the 'Avg MTD Sales by Agents' stacked column chart is split "
        "by; name one of the values it shows"
    )


def test_a_chart_with_a_legend_field_is_refused() -> None:
    document = twin()
    chart = body(document, CHART_2)
    chart["query"]["queryState"]["Series"] = {
        "projections": [
            {
                "field": expr.column_ref("DimChannel", "ChannelName"),
                "queryRef": "DimChannel.ChannelName",
            }
        ]
    }
    with pytest.raises(DocumentError) as raised:
        run(document, {"op": "set_conditional_format", "visual": CHART_2, "style": "gradient"})
    assert str(raised.value) == (
        "The 'Avg MTD Sales by Agents' stacked column chart has a legend field, and Power BI "
        "colors bars by value only on a chart without one"
    )


def test_a_card_value_takes_a_field_value_under_its_fields_wildcard() -> None:
    document = twin()
    run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": CARD,
            "style": "field_value",
            "measure": "CF Table",
        },
    )
    selector = column("# Measures.Number of Employees with Positive Change %")
    assert entry(document, CARD, "value", selector) == {
        "properties": {"fontColor": fill(measure("CF Table"))},
        "selector": selector,
    }


def test_a_title_color_from_a_measure_says_which_fixed_color_it_replaced() -> None:
    document = twin()
    outcome = run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": TITLED_CARD,
            "style": "field_value",
            "target": "title_color",
            "measure": {"table": M, "name": "CF Table"},
        },
    )
    title = entry(document, TITLED_CARD, "title", None, "visualContainerObjects")
    assert title["properties"]["fontColor"] == fill(measure("CF Table"))
    assert title["properties"]["text"] == {"expr": literal("'TopN/BottomN'")}
    assert outcome.replaced == ["the fixed title color #252423"]
    drawn = property_is_rendered("cardVisual", "title", "fontColor", container=True)
    assert outcome.visible_in[0].where == ("studio" if drawn else "powerbi_only")


def test_a_title_bound_to_a_measure_becomes_dynamic() -> None:
    document = twin()
    operation = {
        "op": "set_conditional_format",
        "visual": CHART,
        "style": "field_value",
        "target": "title_text",
        "measure": "Employee Name",
    }
    outcome = run_clean(document, operation)
    assert visible_caption(document.visual(CHART)[1]) == DYNAMIC_TITLE
    assert entry(document, CHART, "title", None, "visualContainerObjects")["properties"][
        "text"
    ] == {"expr": measure("Employee Name")}
    # The receipt names the visual as the person knew it before the change.
    assert outcome.summaries == [
        "Set the title text in the 'Total MTD Sales' stacked column chart to follow the measure "
        "[Employee Name]. This replaced the fixed title text 'Total MTD Sales'."
    ]
    assert outcome.warnings == []


def test_a_hidden_title_bound_to_a_measure_is_pointed_out() -> None:
    document = twin()
    body(document, CHART)["visualContainerObjects"]["title"][0]["properties"]["show"] = {
        "expr": literal("false")
    }
    outcome = run(
        document,
        {
            "op": "set_conditional_format",
            "visual": CHART,
            "style": "field_value",
            "target": "title_text",
            "measure": "Employee Name",
        },
    )
    assert visible_caption(document.visual(CHART)[1]) is None
    assert outcome.warnings == [
        "The title is turned off, so the text only shows once the title is turned on in "
        "Power BI Desktop's Format pane."
    ]


def test_title_and_background_only_follow_a_field() -> None:
    document = twin()
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": CHART,
                "style": "gradient",
                "target": "visual_background",
            },
        )
    assert str(raised.value) == "Power BI sets the background by field value, not by gradient"


# --- never destroying what the user has -------------------------------------------------------


def test_replacing_existing_conditional_formatting_asks_first_and_changes_nothing() -> None:
    document = twin()
    before = copy.deepcopy(body(document, TABLE))
    operation = {
        "op": "set_conditional_format",
        "visual": TABLE,
        "style": "gradient",
        "target": "font_color",
        "field": "Rank",
    }
    with pytest.raises(NeedsConfirmation) as raised:
        run(document, {**operation, "field": "vs Previous Month"}, operation)
    assert raised.value.kind == "replace_conditional"
    assert raised.value.step == 2
    assert str(raised.value) == (
        "The font color of the Rank column in the table of Agent ID already follows the measure "
        "[CF Table]. Replace it with a color scale on Rank, #DEEFFF (lowest) to #118DFF "
        "(highest)?"
    )
    # The first step ran on the document (the caller discards it); the refused one left the
    # Rank column alone.
    assert entry(document, TABLE, "values", column("# Measures.Rank")) == next(
        e
        for e in before["objects"]["values"]
        if (e.get("selector") or {}).get("metadata") == "# Measures.Rank"
    )
    outcome = run(document, {**operation, "replace_conditional": True})
    assert outcome.replaced == ["the measure [CF Table] on font color of the Rank column"]
    assert (
        "This replaced the measure [CF Table] on font color of the Rank column."
        in (outcome.summaries[0])
    )


def test_setting_the_same_field_value_again_changes_nothing() -> None:
    document = twin()
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": TABLE,
                "style": "field_value",
                "target": "font_color",
                "field": "Rank",
                "measure": {"table": M, "name": "CF Table"},
            },
        )
    assert str(raised.value) == "The table of Agent ID already looks like that: nothing to change"


def test_a_plain_write_over_a_rule_asks_first() -> None:
    document = twin()
    body(document, CHART_2)["objects"]["labels"][0]["properties"]["labelDisplayUnits"] = fill(
        measure("CF Table")
    )
    with pytest.raises(NeedsConfirmation) as raised:
        run(
            document,
            {"op": "set_number_format", "visual": CHART_2, "display_units": "thousands"},
        )
    assert raised.value.kind == "replace_conditional"


# --- clearing ---------------------------------------------------------------------------------


def test_clearing_a_rule_restores_the_plain_value_underneath() -> None:
    document = twin()
    original = copy.deepcopy(body(document, CHART_2)["objects"]["dataPoint"])
    run(document, {"op": "set_conditional_format", "visual": CHART_2, "style": "gradient"})
    assert body(document, CHART_2)["objects"]["dataPoint"] != original
    outcome = run(document, {"op": "clear_conditional_format", "visual": CHART_2})
    assert body(document, CHART_2)["objects"]["dataPoint"] == original
    plain = expr.effective_property(body(document, CHART_2)["objects"], "dataPoint", "fill", None)
    assert plain is not None and plain.value == {"hex": "#118DFF"}
    assert outcome.summaries == [
        "Removed the conditional data colors (a color scale) in the 'Avg MTD Sales by Agents' "
        "stacked column chart; the plain formatting shows again."
    ]


def test_clearing_desktops_own_rule_leaves_the_tables_plain_colors() -> None:
    document = twin()
    run_clean(
        document,
        {
            "op": "clear_conditional_format",
            "visual": TABLE,
            "target": "font_color",
            "field": "Rank",
        },
    )
    objects = body(document, TABLE)["objects"]
    target = {"metadata": "# Measures.Rank", "data": ["1L"]}
    assert expr.effective_property(objects, "values", "fontColor", target) is None
    plain = expr.effective_property(objects, "values", "fontColorPrimary", target)
    assert plain is not None and plain.value == {"theme": 1, "percent": 0}


def test_clearing_everything_keeps_bindings_and_plain_column_formatting() -> None:
    document = twin()
    outcome = run(document, {"op": "clear_conditional_format", "visual": TABLE})
    assert outcome.summaries == [
        "Removed the conditional font color of the Agent ID column (the measure [CF Table]); "
        "the conditional font color of the Rank column (the measure [CF Table]); the conditional "
        "font color of the MTD Sales column (the measure [Rank Filtering Employyees Sales MTD "
        "CF]); the conditional font color of the vs PM % column (the measure [Rank Filtering "
        "Employyees MTD% CF]); the data bars of the vs Previous Month column in the table of "
        "Agent ID; the plain formatting shows again."
    ]
    objects = body(document, TABLE)["objects"]
    assert len(objects["values"]) == 1  # the plain entry
    assert [e["selector"]["metadata"] for e in objects["columnFormatting"]] == [
        "DimEmployee.EmployeeKey",
        "# Measures.vs PM MOM%",
        "# Measures.Table MTD Sales",
        "# Measures.Rank",
    ]
    image_before = copy.deepcopy(body(document, CARD)["objects"]["image"])
    with pytest.raises(DocumentError) as raised:
        run(document, {"op": "clear_conditional_format", "visual": CARD})
    assert str(raised.value) == (
        "The 'Agents with Positive Change %' card has no conditional formatting to remove"
    )
    assert body(document, CARD)["objects"]["image"] == image_before


# --- typed values -----------------------------------------------------------------------------


def test_data_bar_limits_are_typed_by_the_field_and_rule_thresholds_are_doubles() -> None:
    document = twin()
    sales_table(document)
    table = "salestable0000000001"
    for field, expected in (
        ("Sum of Units", ("3L", "10L")),  # whole numbers round half up
        ("Sum of Amount", ("2.5M", "10M")),  # fixed decimal
        ("Sum of Price", ("2.5D", "10D")),
    ):
        run_clean(
            document,
            {
                "op": "set_conditional_format",
                "visual": table,
                "style": "data_bars",
                "field": field,
                "min_value": 2.5,
                "max_value": 10,
            },
        )
        bars = next(
            e["properties"]["dataBars"]
            for e in body(document, table)["objects"]["columnFormatting"]
            if e["selector"]["metadata"].endswith(field.split(" of ")[1] + ")")
        )
        assert (bars["minValue"], bars["maxValue"]) == (
            {"expr": literal(expected[0])},
            {"expr": literal(expected[1])},
        )
    run(
        document,
        {
            "op": "set_conditional_format",
            "visual": table,
            "style": "rules",
            "target": "background",
            "field": "Sum of Units",
            "rules": [{"min": 5, "color": "#1AAB40"}],
        },
    )
    cell = entry(document, table, "values", column("Sum(Sales.Units)"))["properties"]
    case = cell["backColor"]["solid"]["color"]["expr"]["Conditional"]["Cases"][0]
    assert case["Condition"]["Comparison"]["Right"] == literal("5D")


def test_dates_are_refused_and_text_takes_equal_rules_on_its_first_value() -> None:
    document = twin()
    sales_table(document)
    table = "salestable0000000001"
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": table,
                "style": "data_bars",
                "field": "OrderDate",
            },
        )
    assert str(raised.value) == "Data bars show numbers, and OrderDate holds dates"
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": table,
                "style": "rules",
                "target": "font_color",
                "field": "Region",
                "rules": [{"min": 1, "color": "#FF0000"}],
            },
        )
    assert str(raised.value) == (
        "Rule 1: Region holds text: a rule on it can only match one value (equals, as text) or "
        "blank values"
    )
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": table,
                "style": "rules",
                "target": "font_color",
                "field": "OrderDate",
                "rules": [{"equals": 1, "color": "#FF0000"}],
            },
        )
    assert str(raised.value) == "Rule 1: OrderDate holds dates: rules follow numbers or text"
    run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": table,
            "style": "rules",
            "target": "font_color",
            "field": "Region",
            "rules": [
                {"equals": "West", "color": "#FF0000"},
                {"blank": True, "color": "#C8C6C4"},
                {"color": "#00FF00"},
            ],
        },
    )
    source = {"Aggregation": {"Expression": expr.column_ref("Sales", "Region"), "Function": 3}}

    def text_rule(right: dict[str, Any], operator: int) -> dict[str, Any]:
        # Desktop's text dialog tags each comparison: ordinal evaluation and the operator chosen.
        return {
            "Comparison": {"ComparisonKind": 0, "Left": source, "Right": right},
            "Annotations": {
                "PowerBI.SQExprEvaluationKind": 1,
                "PowerBI.SQExprTextOperatorOption": operator,
            },
        }

    cell = entry(document, table, "values", column("Sales.Region"))["properties"]
    assert cell["fontColor"] == fill(
        {
            "Conditional": {
                "Cases": [
                    {"Condition": text_rule(literal("'West'"), 2), "Value": literal("'#FF0000'")},
                    {"Condition": text_rule(literal("null"), 5), "Value": literal("'#C8C6C4'")},
                    {"Condition": literal("true"), "Value": literal("'#00FF00'")},
                ]
            }
        }
    )


def test_a_measure_the_model_does_not_type_takes_text_rules_when_compared_with_text() -> None:
    document = twin()
    run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "rules",
            "target": "background",
            "field": "Agent Name",
            "rules": [{"equals": "Ann", "color": "#FF0000"}],
        },
    )
    cell = entry(document, TABLE, "values", column("# Measures.Employee Name"))["properties"]
    case = cell["backColor"]["solid"]["color"]["expr"]["Conditional"]["Cases"][0]
    assert case["Condition"]["Annotations"]["PowerBI.SQExprTextOperatorOption"] == 2
    # Typed as a number by the model, text is refused.
    typed = ModelInfo.from_report(
        [],
        [{"Name": name, "TableName": M} for name in MEASURES],
        {"measures": [{"table": M, "name": "Rank", "dataType": 6}]},
    )
    with pytest.raises(DocumentError) as raised:
        cf.apply_conditional(
            twin(),
            cf.parse_conditional(
                [
                    {
                        "op": "set_conditional_format",
                        "visual": TABLE,
                        "style": "rules",
                        "target": "background",
                        "field": "Rank",
                        "rules": [{"equals": "top", "color": "#FF0000"}],
                    }
                ]
            ),
            typed,
        )
    assert str(raised.value) == "Rule 1: 'top' is text, but Rank holds numbers"


def test_based_on_another_field_uses_its_summarization() -> None:
    document = twin()
    sales_table(document)
    table = "salestable0000000001"
    for summarization, function in (("average", 1), ("count", 5), ("count_distinct", 2)):
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": table,
                "style": "gradient",
                "target": "background",
                "field": "Region",
                "based_on": {"table": "Sales", "name": "Units"},
                "summarization": summarization,
                "replace_conditional": True,
            },
        )
        cell = entry(document, table, "values", column("Sales.Region"))["properties"]
        source = cell["backColor"]["solid"]["color"]["expr"]["FillRule"]["Input"]
        assert source == {
            "Aggregation": {"Expression": expr.column_ref("Sales", "Units"), "Function": function}
        }
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": table,
                "style": "gradient",
                "target": "background",
                "field": "Region",
                "based_on": {"table": "Sales", "name": "Nothing"},
                "replace_conditional": True,
            },
        )
    assert str(raised.value) == "Sales has no column or measure named 'Nothing'"


# --- asking and refusing ------------------------------------------------------------------------


def test_a_table_rule_without_a_column_or_a_target_asks() -> None:
    document = twin()
    with pytest.raises(NeedsChoice) as raised:
        run(document, {"op": "set_conditional_format", "visual": TABLE, "style": "gradient"})
    assert (
        raised.value.question == "Color the text or the cell background of the table of Agent ID?"
    )
    assert [c.fill for c in raised.value.options] == [
        {"target": "font_color"},
        {"target": "background"},
    ]
    with pytest.raises(NeedsChoice) as asked:
        run(document, {"op": "set_conditional_format", "visual": TABLE, "style": "data_bars"})
    assert asked.value.question == "Which column of the table of Agent ID should get the data bars?"
    assert [c.label for c in asked.value.options] == [
        "Agent Name",
        "Rank",
        "MTD Sales",
        "vs PM %",
        "vs Previous Month",
    ]


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            {"visual": TABLE, "style": "gradient", "target": "font_color", "field": "Sales Amount"},
            "The table of Agent ID has no column or field called 'Sales Amount'. Its fields: "
            "'Agent ID', 'Agent Name', 'Rank', 'MTD Sales', 'vs PM %', 'vs Previous Month'",
        ),
        (
            {"visual": TABLE, "style": "gradient", "target": "data_bars", "field": "Rank"},
            "Power BI sets the data bars by data bars, not by gradient",
        ),
        (
            {"visual": CHART, "style": "data_bars"},
            "The 'Total MTD Sales' stacked column chart takes conditional data colors (target "
            "data_colors), and a title or visual background from a field, not data bars",
        ),
        (
            {
                "visual": CARD,
                "style": "rules",
                "target": "background",
                "rules": [{"color": "#FF0000"}],
            },
            "The 'Agents with Positive Change %' card takes a conditional value color (target "
            "font_color), and a title or visual background from a field, not background",
        ),
        (
            {
                "visual": TABLE,
                "style": "gradient",
                "target": "font_color",
                "field": "Rank",
                "rules": [{"color": "#FF0000"}],
            },
            "A gradient format does not use rules",
        ),
        (
            {"visual": CHART, "style": "field_value", "target": "web_url", "measure": "CF Table"},
            "The 'Total MTD Sales' stacked column chart takes conditional data colors (target "
            "data_colors), and a title or visual background from a field, not web address",
        ),
        (
            {"visual": CHART, "style": "gradient", "apply_to": "totals"},
            "Only a table or matrix column has totals to format separately",
        ),
        (
            {"visual": "54be5cf0783e66471540", "style": "rules", "rules": [{"color": "#FF0000"}]},
            "The 'Country' slicer has no data colors, columns or value Power BI formats by rules; "
            "only its title and background can follow a field (target title_text, title_color or "
            "visual_background with style field_value)",
        ),
        (
            {"visual": TABLE, "style": "field_value", "target": "font_color", "field": "Rank"},
            "Name the measure (or column) whose values give the font color, for example a measure "
            'that returns "#FF0000" or "red"',
        ),
    ],
)
def test_refusals_are_plain(operation: dict[str, Any], message: str) -> None:
    document = twin()
    with pytest.raises(DocumentError) as raised:
        run(document, {"op": "set_conditional_format", **operation})
    assert str(raised.value) == message


# --- number formats -----------------------------------------------------------------------------


def test_data_label_units_decimals_and_a_format_code() -> None:
    document = twin()
    outcome = run_clean(
        document,
        {"op": "set_number_format", "visual": CHART, "preset": "thousands", "decimals": 1},
        {"op": "set_number_format", "visual": CHART_2, "target": "data_labels", "format": "#,0.0"},
    )
    assert entry(document, CHART, "labels", None)["properties"] == {
        "fontSize": {"expr": literal("16D")},
        "labelDisplayUnits": {"expr": literal("1000D")},
        "labelPrecision": {"expr": literal("1L")},
    }
    labels = entry(document, CHART_2, "labels", None)["properties"]
    assert labels["labelDisplayUnits"] == {"expr": literal("-1D")}  # Desktop's Custom unit
    assert labels["valueCustomFormatString"] == {"expr": literal("'#,0.0'")}
    assert outcome.summaries == [
        "Set the data labels of the 'Total MTD Sales' stacked column chart to thousands, "
        "1 decimal.",
        "Set the data labels of the 'Avg MTD Sales by Agents' stacked column chart to the format "
        "#,0.0.",
    ]


def test_a_series_with_its_own_display_units_is_named_when_the_visuals_units_replace_them() -> None:
    document = twin()
    body(document, CHART)["objects"]["labels"].append(
        {
            "properties": {"labelDisplayUnits": {"expr": literal("1D")}},
            "selector": {"metadata": "# Measures.TopN/BottomN/ MTD Sales"},
        }
    )
    outcome = run(document, {"op": "set_number_format", "visual": CHART, "preset": "millions"})
    series = entry(document, CHART, "labels", {"metadata": "# Measures.TopN/BottomN/ MTD Sales"})
    assert series["properties"]["labelDisplayUnits"] == {"expr": literal("1000000D")}
    assert outcome.replaced == ["the data label display units 1 field or category had of its own"]


def test_the_value_axis_takes_units_and_decimals_but_not_a_code() -> None:
    document = twin()
    run_clean(
        document,
        {
            "op": "set_number_format",
            "visual": CHART_2,
            "target": "value_axis",
            "display_units": "millions",
            "decimals": 2,
        },
    )
    axis = entry(document, CHART_2, "valueAxis", None)["properties"]
    assert (axis["labelDisplayUnits"], axis["labelPrecision"]) == (
        {"expr": literal("1000000D")},
        {"expr": literal("2L")},
    )
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_number_format",
                "visual": CHART_2,
                "target": "value_axis",
                "preset": "percent",
            },
        )
    assert str(raised.value) == (
        "The axis labels follow the field's format: set a format code on the data labels or the "
        "field instead"
    )
    # The axis is the chart's, not one field's: a field named with it resolves, then is refused.
    chart = body(document, CHART_2)
    chart["query"]["queryState"]["Y"]["projections"].append(
        {"field": measure("Rank"), "queryRef": "# Measures.Rank", "nativeQueryRef": "Rank"}
    )
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_number_format",
                "visual": CHART_2,
                "target": "value_axis",
                "field": "Rank",
                "display_units": "millions",
            },
        )
    assert str(raised.value) == (
        "The value axis of the 'Avg MTD Sales by Agents' stacked column chart is shared by all "
        "its values; leave out the field"
    )
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_number_format",
                "visual": CHART_2,
                "target": "value_axis",
                "field": "No Such Field",
                "display_units": "millions",
            },
        )
    assert "has no column or field called 'No Such Field'" in str(raised.value)


def test_a_card_value_takes_a_custom_code_in_its_default_state() -> None:
    document = twin()
    run_clean(
        document, {"op": "set_number_format", "visual": CARD, "preset": "percent", "decimals": 1}
    )
    default = entry(document, CARD, "value", {"id": "default"})["properties"]
    assert default["labelDisplayUnits"] == {"expr": literal("-1D")}
    assert default["customFormatString"] == {"expr": literal("'0.0%'")}
    per_field = entry(
        document,
        CARD,
        "value",
        {"metadata": "# Measures.Number of Employees with Positive Change %"},
    )["properties"]
    assert "customFormatString" not in per_field


def test_table_columns_take_units_in_their_entry_and_codes_in_the_visual_query() -> None:
    document = twin()
    selector = {"metadata": "# Measures.Table MTD Sales"}
    original = entry(document, TABLE, "columnFormatting", selector)
    outcome = run_clean(
        document,
        {
            "op": "set_number_format",
            "visual": TABLE,
            "field": "MTD Sales",
            "display_units": "thousands",
            "decimals": 1,
        },
        {
            "op": "set_number_format",
            "visual": TABLE,
            "field": "Rank",
            "preset": "currency",
            "currency_symbol": "€",
        },
        {"op": "set_number_format", "visual": TABLE, "field": "vs PM %", "preset": "currency"},
    )
    assert entry(document, TABLE, "columnFormatting", selector)["properties"] == {
        **original["properties"],
        "labelDisplayUnits": {"expr": literal("1000D")},
        "labelPrecision": {"expr": literal("1L")},
    }
    formats = {
        p["queryRef"]: p.get("format")
        for p in body(document, TABLE)["query"]["queryState"]["Values"]["projections"]
    }
    assert formats["# Measures.Rank"] == "€#,0.00;(€#,0.00);€#,0.00"
    assert formats["# Measures.vs PM MOM%"] == "\\$#,0.00;(\\$#,0.00);\\$#,0.00"
    assert formats["# Measures.Table MTD Sales"] is None
    assert outcome.visible_in[-1] == VisibleIn("set_number_format", "powerbi_only", "query.format")
    # Only the visual-level format can say a currency here, and Desktop keeps that one only
    # behind a preview switch: the person is told, once per step that writes it.
    assert outcome.warnings == [cf.FORMAT_WARNING, cf.FORMAT_WARNING]
    assert cf.FORMAT_WARNING == (
        "Power BI Desktop keeps this only when visual-level format strings are turned on "
        "(Options > Preview features); otherwise it uses the measure's own format."
    )
    # Units and decimals of a column never need it.
    units_only = run(
        twin(),
        {"op": "set_number_format", "visual": TABLE, "field": "Rank", "preset": "one_decimal"},
    )
    assert units_only.warnings == []
    run(
        document,
        {"op": "set_number_format", "visual": TABLE, "field": "MTD Sales", "preset": "general"},
    )
    assert entry(document, TABLE, "columnFormatting", selector) == original


def test_a_field_target_uses_the_visuals_own_setting_that_desktop_keeps() -> None:
    document = twin()
    outcome = run_clean(
        document,
        {
            "op": "set_number_format",
            "visual": CHART,
            "target": "field",
            "field": "TopN/BottomN/ MTD Sales",
            "preset": "millions",
            "decimals": 1,
        },
    )
    # The chart's only value: its data labels, for the whole visual, and no visual-level format.
    labels = entry(document, CHART, "labels", None)["properties"]
    assert (labels["labelDisplayUnits"], labels["labelPrecision"]) == (
        {"expr": literal("1000000D")},
        {"expr": literal("1L")},
    )
    assert "format" not in body(document, CHART)["query"]["queryState"]["Y"]["projections"][0]
    assert outcome.warnings == []


def test_a_field_format_code_goes_to_the_visual_query_only_where_nothing_else_says_it() -> None:
    document = twin()
    add_visual(
        document,
        "pie00000000000000001",
        "pieChart",
        {
            "Category": [sales_column("Region")],
            "Y": [{"field": measure("Table MTD Sales"), "queryRef": "# Measures.Table MTD Sales"}],
        },
    )
    outcome = run_clean(
        document, {"op": "set_number_format", "visual": "pie00000000000000001", "format": "0.0%"}
    )
    y = body(document, "pie00000000000000001")["query"]["queryState"]["Y"]["projections"][0]
    assert y["format"] == "0.0%"
    assert outcome.warnings == [cf.FORMAT_WARNING]
    # "General" gives the measure's own format back, the visual-level one included.
    run(
        document, {"op": "set_number_format", "visual": "pie00000000000000001", "preset": "general"}
    )
    reset = body(document, "pie00000000000000001")["query"]["queryState"]["Y"]["projections"][0]
    assert "format" not in reset


def test_a_named_field_scopes_the_labels_or_card_value_and_an_unknown_one_is_refused() -> None:
    document = twin()
    chart = body(document, CHART_2)
    chart["query"]["queryState"]["Y"]["projections"].append(
        {"field": measure("Rank"), "queryRef": "# Measures.Rank", "nativeQueryRef": "Rank"}
    )
    outcome = run(
        document,
        {"op": "set_number_format", "visual": CHART_2, "field": "Rank", "format": "0.0"},
    )
    scoped = entry(document, CHART_2, "labels", {"metadata": "# Measures.Rank"})["properties"]
    assert scoped == {
        "labelDisplayUnits": {"expr": literal("-1D")},
        "valueCustomFormatString": {"expr": literal("'0.0'")},
    }
    assert "labelDisplayUnits" not in entry(document, CHART_2, "labels", None)["properties"]
    assert outcome.summaries == [
        "Set the data labels of Rank of the 'Avg MTD Sales by Agents' stacked column chart to "
        "the format 0.0."
    ]
    card = twin()
    add_visual(
        card,
        "card0000000000000001",
        "cardVisual",
        {
            "Data": [
                {"field": measure("Table MTD Sales"), "queryRef": "# Measures.Table MTD Sales"},
                {"field": measure("Rank"), "queryRef": "# Measures.Rank"},
            ]
        },
    )
    run_clean(
        card,
        {
            "op": "set_number_format",
            "visual": "card0000000000000001",
            "field": "Rank",
            "decimals": 0,
        },
    )
    assert entry(card, "card0000000000000001", "value", {"metadata": "# Measures.Rank"}) == {
        "properties": {"labelPrecision": {"expr": literal("0L")}},
        "selector": {"metadata": "# Measures.Rank"},
    }
    for visual in (CHART_2, CARD):
        with pytest.raises(DocumentError) as raised:
            run(
                twin(),
                {
                    "op": "set_number_format",
                    "visual": visual,
                    "field": "No Such Field",
                    "display_units": "thousands",
                },
            )
        assert "has no column or field called 'No Such Field'" in str(raised.value)
    with pytest.raises(DocumentError) as raised:
        run(
            twin(),
            {
                "op": "set_number_format",
                "visual": CHART_2,
                "field": "Clustered Employees",
                "display_units": "thousands",
            },
        )
    assert str(raised.value) == (
        "Clustered Employees is what the 'Avg MTD Sales by Agents' stacked column chart is split "
        "by; name one of the values it shows"
    )


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            {"visual": CHART, "format": "0.0%", "display_units": "thousands"},
            "A format code sets the units itself; give a code or display units",
        ),
        (
            {"visual": CHART, "format": "0.0%", "decimals": 1},
            "A format code sets the decimals itself; give a code or decimals",
        ),
        (
            {"visual": CHART, "preset": "thousands", "currency_symbol": "€"},
            "A currency symbol goes with the currency preset",
        ),
        (
            {"visual": CHART},
            "Say how the numbers should read: a preset, a format code, units or decimals",
        ),
        (
            {"visual": TABLE, "field": "MTD Sales", "display_units": "auto"},
            "A table column has no automatic display units; say none",
        ),
        ({"visual": CHART, "format": '0.0 "K'}, "The format code '0.0 \"K' has an unclosed quote"),
        (
            {"visual": TABLE, "field": "MTD Sales", "preset": "currency", "currency_symbol": 'a"b'},
            "'a\"b' cannot be a currency symbol: give one like $, € or CHF",
        ),
        (
            {"visual": TABLE, "field": "MTD Sales", "preset": "currency", "currency_symbol": "\\"},
            "'\\\\' cannot be a currency symbol: give one like $, € or CHF",
        ),
        (
            {"visual": "54be5cf0783e66471540", "preset": "thousands"},
            "The 'Country' slicer has no data labels or values with a number format; name a field "
            "(target field) to format it in this visual",
        ),
    ],
)
def test_number_format_refusals(operation: dict[str, Any], message: str) -> None:
    with pytest.raises(DocumentError) as raised:
        run(twin(), {"op": "set_number_format", **operation})
    assert str(raised.value) == message


# --- columns, layout and order ------------------------------------------------------------------


def test_table_layout_totals_and_matrix_only_settings() -> None:
    document = twin()
    run_clean(
        document,
        {
            "op": "set_table_layout",
            "visual": TABLE,
            "totals": True,
        },
    )
    objects = body(document, TABLE)["objects"]
    assert objects["total"] == [{"properties": {"totals": {"expr": literal("true")}}}]
    for setting, message in (
        ({"row_subtotals": False}, "Only a matrix has row subtotals"),
        ({"layout": "tabular"}, "Only a matrix has a compact, outline or tabular layout"),
        ({}, "Say what to change in the table's layout"),
    ):
        with pytest.raises(DocumentError) as raised:
            run(document, {"op": "set_table_layout", "visual": TABLE, **setting})
        assert str(raised.value) == message


def test_a_matrix_takes_subtotals_layout_and_values_on_rows() -> None:
    document = twin()
    add_visual(
        document,
        "matrix00000000000001",
        "pivotTable",
        {
            "Rows": [
                {
                    "field": expr.column_ref("Clustered Employees", "Clustered Employees"),
                    "queryRef": "Clustered Employees.Clustered Employees",
                }
            ],
            "Values": [
                {"field": measure("Table MTD Sales"), "queryRef": "# Measures.Table MTD Sales"}
            ],
        },
    )
    outcome = run_clean(
        document,
        {
            "op": "set_table_layout",
            "visual": "matrix00000000000001",
            "row_subtotals": False,
            "column_subtotals": True,
            "stepped": True,
            "values_on_rows": True,
        },
    )
    assert body(document, "matrix00000000000001")["objects"] == {
        "subTotals": [
            {
                "properties": {
                    "rowSubtotals": {"expr": literal("false")},
                    "columnSubtotals": {"expr": literal("true")},
                }
            }
        ],
        "values": [{"properties": {"valuesOnRow": {"expr": literal("true")}}}],
        "general": [{"properties": {"layout": {"expr": literal("'Compact'")}}}],
    }
    assert "compact layout" in outcome.summaries[0]
    with pytest.raises(DocumentError) as raised:
        run(document, {"op": "set_table_layout", "visual": "matrix00000000000001", "totals": True})
    assert str(raised.value) == (
        "A matrix shows its totals as row and column subtotals: use row_subtotals and "
        "column_subtotals"
    )
    # A matrix formats the cells of its values, not its row headers, and says so.
    for operation in (
        {"op": "set_conditional_format", "style": "data_bars", "field": "Clustered Employees"},
        {"op": "set_number_format", "field": "Clustered Employees", "display_units": "thousands"},
    ):
        with pytest.raises(DocumentError) as refused:
            run(document, {**operation, "visual": "matrix00000000000001"})
        assert str(refused.value) == (
            "Clustered Employees is a rows field of the matrix of Clustered Employees; this "
            "formats its values: 'Table MTD Sales'"
        )
    with pytest.raises(DocumentError) as refused:
        run(
            document,
            {
                "op": "move_column",
                "visual": "matrix00000000000001",
                "field": "Clustered Employees",
                "to_index": 0,
            },
        )
    assert str(refused.value) == (
        "The Clustered Employees field is the only one in its well: it has nowhere to go"
    )


def test_moving_a_column_reorders_its_well() -> None:
    document = twin()
    outcome = run_clean(
        document, {"op": "move_column", "visual": TABLE, "field": "Rank", "to_index": 0}
    )
    refs = [
        p["queryRef"] for p in body(document, TABLE)["query"]["queryState"]["Values"]["projections"]
    ]
    assert refs == [
        "# Measures.Rank",
        "DimEmployee.EmployeeKey",
        "# Measures.Employee Name",
        "# Measures.Table MTD Sales",
        "# Measures.vs PM MOM%",
        "# Measures.vs PM",
    ]
    assert outcome.summaries == ["Moved the Rank column of the table of Agent ID to first."]
    for to_index, message in (
        (6, "The table of Rank has 6 fields there; places run from 0 (first) to 5 (last)"),
        (0, "The Rank column is already there: nothing to change"),
    ):
        with pytest.raises(DocumentError) as raised:
            run(
                document,
                {"op": "move_column", "visual": TABLE, "field": "Rank", "to_index": to_index},
            )
        assert str(raised.value) == message


# --- tool cards and probes ----------------------------------------------------------------------


def test_every_operation_has_a_card_with_real_examples() -> None:
    cards = edit_tools.tool_cards(("report_edit_conditional",))
    assert [c.op for c in cards] == list(cf.CONDITIONAL_HANDLERS)
    assert {c.op for c in cards} == {
        cls.model_fields["op"].default for cls in cf.CONDITIONAL_OPERATIONS
    }
    for card in cards:
        assert card.family == cf.FAMILY
        assert len(card.examples) >= 3, card.op
        assert len({s.name for s in card.slots}) == len(card.slots), card.op
        assert all(s.choices for s in card.slots if s.kind == "enum"), card.op
        assert not card.needs_desktop and not card.confirm
        for text, operation in card.examples:
            document = twin()
            outcome = run_clean(document, dict(operation))
            assert outcome.report_changed, (card.op, text)


def test_a_card_builds_rules_from_one_threshold() -> None:
    card = next(c for c in cf.TOOL_CARDS if c.op == "set_conditional_format")
    built = card.to_operation(
        {
            "visual": TABLE,
            "style": "rules",
            "target": "font_color",
            "field": "MTD Sales",
            "threshold": 100000,
            "below_color": "#D64550",
        }
    )
    # only the rule whose color the request names: no green above it nobody asked for
    assert built == {
        "op": "set_conditional_format",
        "visual": TABLE,
        "style": "rules",
        "target": "font_color",
        "field": "MTD Sales",
        "rules": [{"max": 100000, "color": "#D64550"}],
    }
    assert cf.parse_conditional([built])[0].rules[0].max == 100000
    both = card.to_operation(
        {
            "visual": TABLE,
            "style": "rules",
            "field": "MTD Sales",
            "threshold": 100000,
            "below_color": "#D64550",
            "above_color": "#1AAB40",
        }
    )
    assert both["rules"] == [
        {"max": 100000, "color": "#D64550"},
        {"min": 100000, "color": "#1AAB40"},
    ]
    assert cf.parse_conditional([both])[0].rules[1].min == 100000
    uncolored = card.to_operation(
        {"visual": TABLE, "style": "rules", "field": "MTD Sales", "threshold": 100000}
    )
    assert "rules" not in uncolored  # no color said: no rule written (the planner asks)


def test_every_probe_applies_to_the_twin_and_its_check_passes_on_what_was_written() -> None:
    covered_styles: set[str] = set()
    covered_targets: set[str] = set()
    covered_ops: set[str] = set()
    for probe in cf.DESKTOP_PROBES:
        assert probe.family == cf.FAMILY and probe.page == "1"
        document = twin()
        baseline = capture_baseline(document, model())
        for operation in probe.operations:
            covered_ops.add(str(operation["op"]))
            if operation["op"] in cf.CONDITIONAL_HANDLERS:
                run(document, dict(operation))
            else:
                report_edits.apply_operations(
                    document, report_edits.parse_operations([operation]), model()
                )
            if operation["op"] == "set_conditional_format":
                parsed = cf.parse_conditional([operation])[0]
                covered_styles.add(parsed.style)
                covered_targets.add(
                    parsed.target
                    or {"data_bars": "data_bars", "icons": "icon"}.get(parsed.style, "default")
                )
        assert validate_document(document, model(), baseline=baseline) == [], probe.name
        if probe.check is not None:
            parts = {name: data for name, data, _ in document.iter_json_parts()}
            assert probe.check(json.loads(json.dumps(parts))) == [], probe.name
    assert covered_styles == {"gradient", "rules", "field_value", "data_bars", "icons"}
    assert covered_targets >= {
        "font_color",
        "background",
        "data_colors",
        "icon",
        "data_bars",
        "web_url",
        "title_text",
        "title_color",
        "visual_background",
    }
    assert covered_ops >= set(cf.CONDITIONAL_HANDLERS)


def _field_refs(node: Any, found: set[tuple[str, str, str]]) -> set[tuple[str, str, str]]:
    """(kind, table, name) of every Measure and Column reference under ``node``."""
    if isinstance(node, dict):
        for kind in ("Measure", "Column"):
            ref = node.get(kind)
            if isinstance(ref, dict) and isinstance(ref.get("Property"), str):
                table = ref.get("Expression", {}).get("SourceRef", {}).get("Entity")
                if isinstance(table, str):
                    found.add((kind, table, ref["Property"]))
        for value in node.values():
            _field_refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _field_refs(value, found)
    return found


def test_the_twin_model_types_every_field_the_twin_uses() -> None:
    """The twin's model carries the real types of every field its report uses, so an edit the
    real dashboard refuses (a scale on the text measure MTD Sales) is refused here too."""
    used: set[tuple[str, str, str]] = set()
    for _, data, _ in twin().iter_json_parts():
        _field_refs(data, used)
    typed = {("Measure", M, name) for name in TWIN_MEASURE_TYPES} | {
        ("Column", table, name) for table, name in TWIN_COLUMN_TYPES
    }
    assert used and used <= typed
    info = model()
    assert info.data_type(M, "Table MTD Sales") == 2
    assert info.data_type(M, "Rank") == 6
    with pytest.raises(DocumentError) as raised:
        run(
            twin(),
            {
                "op": "set_conditional_format",
                "visual": TABLE,
                "style": "data_bars",
                "field": "MTD Sales",
            },
        )
    assert str(raised.value) == "Data bars show numbers, and MTD Sales holds text"


@pytest.mark.skipif(not REAL_AGENTS.is_file(), reason="the real Agents dashboard is absent")
def test_the_twin_model_types_are_the_real_models() -> None:
    real = real_types()
    measures = {(row["table"], row["name"]): row for row in real["measures"]}
    columns = {(row["table"], row["name"]): row for row in real["columns"]}
    for name, data_type in TWIN_MEASURE_TYPES.items():
        assert measures[(M, name)]["dataType"] == data_type, name
    for key, (data_type, summarize) in TWIN_COLUMN_TYPES.items():
        assert (columns[key]["dataType"], columns[key]["summarizeBy"]) == (
            data_type,
            summarize,
        ), key


@pytest.mark.skipif(not REAL_AGENTS.is_file(), reason="the real Agents dashboard is absent")
def test_every_probe_applies_to_the_real_dashboard_with_its_model_types() -> None:
    """The probes as the editor runs them on the real file: its model built by
    ReportEditor._model_info from pbix_extract.types. Read-only: the edits stay in memory."""
    info = real_model()
    for probe in cf.DESKTOP_PROBES:
        document = PbirDocument(PbixPackage(REAL_AGENTS))
        baseline = capture_baseline(document, info)
        for operation in probe.operations:
            if operation["op"] in cf.CONDITIONAL_HANDLERS:
                cf.apply_conditional(document, cf.parse_conditional([dict(operation)]), info)
            else:
                report_edits.apply_operations(
                    document, report_edits.parse_operations([operation]), info
                )
        assert validate_document(document, info, baseline=baseline) == [], probe.name
        if probe.check is not None:
            parts = {name: data for name, data, _ in document.iter_json_parts()}
            assert probe.check(json.loads(json.dumps(parts))) == [], probe.name
    for card in cf.TOOL_CARDS:
        for text, operation in card.examples:
            document = PbirDocument(PbixPackage(REAL_AGENTS))
            outcome = cf.apply_conditional(document, cf.parse_conditional([dict(operation)]), info)
            assert outcome.report_changed, (card.op, text)


def test_a_probe_check_reports_what_desktop_dropped() -> None:
    probe = next(p for p in cf.DESKTOP_PROBES if p.name == "cf_gradient_table_background")
    assert probe.check is not None
    document = twin()
    run(document, *probe.operations)
    parts = {name: copy.deepcopy(data) for name, data, _ in document.iter_json_parts()}
    visual = next(v for k, v in parts.items() if k.endswith(f"/visuals/{TABLE}/visual.json"))
    for item in visual["visual"]["objects"]["values"]:
        item.get("properties", {}).pop("backColor", None)
    assert probe.check(parts) == [f"values.backColor of {TABLE} is gone after the save"]


def test_the_shape_lists_name_their_evidence() -> None:
    assert cf.DESKTOP_AUTHORED_SHAPES and cf.UNVERIFIED_SHAPES
    assert not set(cf.DESKTOP_AUTHORED_SHAPES) & set(cf.UNVERIFIED_SHAPES)
    assert all(isinstance(v, str) and v for v in cf.UNVERIFIED_SHAPES.values())
    assert "FillRule gradient" in cf.UNVERIFIED_SHAPES
    assert "columnFormatting.dataBars" in cf.DESKTOP_AUTHORED_SHAPES
    assert "total.totals" in cf.DESKTOP_AUTHORED_SHAPES
    assert "Annotations" in cf.UNVERIFIED_SHAPES["text rule"]
    assert all(len(icons) in (3, 4, 5) for icons in cf.ICON_SETS.values())
    assert {icon for icons in cf.ICON_SETS.values() for icon in icons} <= cf.ICON_NAMES


def test_op_names_do_not_clash_with_the_existing_operations() -> None:
    # Once the family is registered its own classes are in the list too; only others count.
    existing = {
        cls.model_fields["op"].default
        for cls in report_edits.REPORT_OPERATIONS
        if cls not in cf.CONDITIONAL_OPERATIONS
    }
    ours = {cls.model_fields["op"].default for cls in cf.CONDITIONAL_OPERATIONS}
    assert not existing & ours


def test_parse_names_the_step_and_the_setting() -> None:
    with pytest.raises(DocumentError) as raised:
        cf.parse_conditional(
            [{"op": "set_conditional_format", "visual": TABLE, "style": "sparkles"}]
        )
    assert str(raised.value).startswith("Step 1 (set_conditional_format): style: ")


# --- review fixes (2026-09-29): each repro of the two reviews --------------------------------


def test_changing_a_scales_color_keeps_the_field_it_follows() -> None:
    """Review item 3: a partial edit of a scale based on another field kept its stops but went
    back to the column's own field (Rank thresholds applied to sales)."""
    document = twin()
    run(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "gradient",
            "target": "background",
            "field": "MTD Sales",
            "based_on": {"table": M, "name": "Rank"},
            "min_value": 1,
            "max_value": 10,
        },
    )
    outcome = run(
        document,
        {
            "op": "set_conditional_format",
            "visual": TABLE,
            "style": "gradient",
            "target": "background",
            "field": "MTD Sales",
            "max_color": "#00FF00",
        },
    )
    cell = entry(document, TABLE, "values", column("# Measures.Table MTD Sales"))["properties"]
    rule = cell["backColor"]["solid"]["color"]["expr"]["FillRule"]
    assert rule["Input"] == measure("Rank")
    assert rule["FillRule"]["linearGradient2"]["max"] == {
        "color": literal("'#00FF00'"),
        "value": literal("10D"),
    }
    assert outcome.summaries[0] == (
        "Updated the background of the MTD Sales column in the table of Agent ID to follow a "
        "color scale on [Rank], #DEEFFF (at 1) to #00FF00 (at 10)."
    )
    # A new stop above the kept maximum is refused, not written.
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": TABLE,
                "style": "gradient",
                "target": "background",
                "field": "MTD Sales",
                "min_value": 20,
            },
        )
    assert str(raised.value) == "The color scale's numbers have to rise from minimum to maximum"


def test_a_summarized_column_is_formatted_by_its_own_summary() -> None:
    """Review item 9: a standard deviation column was formatted by its sum."""
    document = twin()
    add_visual(
        document,
        "stdtable000000000001",
        "tableEx",
        {"Values": [sales_column("Region"), sales_sum("Price", 7, "StandardDeviation")]},
    )
    run_clean(
        document,
        {
            "op": "set_conditional_format",
            "visual": "stdtable000000000001",
            "style": "gradient",
            "target": "background",
            "field": "StandardDeviation of Price",
        },
    )
    cell = entry(
        document, "stdtable000000000001", "values", column("StandardDeviation(Sales.Price)")
    )["properties"]
    assert cell["backColor"]["solid"]["color"]["expr"]["FillRule"]["Input"] == {
        "Aggregation": {"Expression": expr.column_ref("Sales", "Price"), "Function": 7}
    }


@pytest.mark.parametrize(
    ("limit", "message"),
    [
        (1e20, "1e+20 is out of range for data bars"),
        (float("nan"), "nan is out of range for data bars"),
        (float("inf"), "inf is out of range for data bars"),
        (-1e300, "-1e+300 is out of range for data bars"),
    ],
)
def test_data_bar_limits_stay_in_power_bis_range(limit: float, message: str) -> None:
    """Review item 5: 1e20 became a 21-digit L literal, NaN and inf crashed, 1e300 a 302-char M."""
    document = twin()
    sales_table(document)
    for field in ("Sum of Units", "Sum of Amount", "Sum of Price"):
        with pytest.raises(DocumentError) as raised:
            run(
                document,
                {
                    "op": "set_conditional_format",
                    "visual": "salestable0000000001",
                    "style": "data_bars",
                    "field": field,
                    "max_value": limit,
                },
            )
        assert str(raised.value) == message


def test_data_bars_of_a_typed_measure_are_typed_by_the_measure() -> None:
    typed = ModelInfo.from_report(
        [],
        [{"Name": name, "TableName": M} for name in MEASURES],
        {"measures": [{"table": M, "name": "Rank", "dataType": 6}]},
    )
    document = twin()
    cf.apply_conditional(
        document,
        cf.parse_conditional(
            [
                {
                    "op": "set_conditional_format",
                    "visual": TABLE,
                    "style": "data_bars",
                    "field": "Rank",
                    "min_value": 0.5,
                    "max_value": 10,
                }
            ]
        ),
        typed,
    )
    bars = entry(document, TABLE, "columnFormatting", {"metadata": "# Measures.Rank"})
    assert bars["properties"]["dataBars"]["minValue"] == {"expr": literal("1L")}
    assert bars["properties"]["dataBars"]["maxValue"] == {"expr": literal("10L")}


def test_a_partial_data_bar_edit_is_held_to_the_other_end() -> None:
    """Review item 11: a new minimum above the bars' existing maximum was written (200D/100D)."""
    document = twin()
    sales_table(document)
    operation = {
        "op": "set_conditional_format",
        "visual": "salestable0000000001",
        "style": "data_bars",
        "field": "Sum of Price",
    }
    run(document, {**operation, "max_value": 100})
    with pytest.raises(DocumentError) as raised:
        run(document, {**operation, "min_value": 200})
    assert str(raised.value) == (
        "The data bars would start at 200 and end at 100: the minimum has to be below the maximum"
    )


def test_a_theme_colored_title_says_the_color_it_had() -> None:
    """Review item 11: 'replaced the fixed title color None.'"""
    document = twin()
    title = body(document, CHART_2)["visualContainerObjects"]["title"][0]["properties"]
    title["fontColor"] = expr.theme_color(3, 0)
    outcome = run(
        document,
        {
            "op": "set_conditional_format",
            "visual": CHART_2,
            "style": "field_value",
            "target": "title_color",
            "measure": "CF Table",
        },
    )
    assert outcome.replaced == ["the fixed title color #12239E"]


def test_clearing_a_new_card_clears_every_fields_rule_and_nothing_else() -> None:
    """Review item 7: only the first field of a card with several was cleared."""
    document = twin()
    add_visual(
        document,
        "card0000000000000001",
        "cardVisual",
        {
            "Data": [
                {"field": measure("Table MTD Sales"), "queryRef": "# Measures.Table MTD Sales"},
                {"field": measure("Rank"), "queryRef": "# Measures.Rank"},
            ]
        },
    )
    for field in ("Table MTD Sales", "Rank"):
        run(
            document,
            {
                "op": "set_conditional_format",
                "visual": "card0000000000000001",
                "style": "field_value",
                "field": field,
                "measure": "CF Table",
            },
        )
    assert len(body(document, "card0000000000000001")["objects"]["value"]) == 2
    outcome = run(document, {"op": "clear_conditional_format", "visual": "card0000000000000001"})
    assert "value" not in body(document, "card0000000000000001").get("objects", {})
    assert outcome.summaries == [
        "Removed the conditional value's font color of Table MTD Sales (the measure [CF Table]); "
        "the conditional value's font color of Rank (the measure [CF Table]) in the card of "
        "Table MTD Sales; the plain formatting shows again."
    ]


def test_a_stepped_matrix_follows_desktops_mapping_and_drops_the_old_flag() -> None:
    """Spec item 5: stepped off is Outline with row subtotals on top, else Tabular, and the old
    rowHeaders.stepped goes when general.layout is written."""
    document = twin()
    add_visual(
        document,
        "matrix00000000000001",
        "pivotTable",
        {
            "Rows": [sales_column("Region")],
            "Values": [{"field": measure("Rank"), "queryRef": "# Measures.Rank"}],
        },
    )
    objects = body(document, "matrix00000000000001").setdefault("objects", {})
    objects["rowHeaders"] = [{"properties": {"stepped": {"expr": literal("true")}}}]
    objects["subTotals"] = [{"properties": {"rowSubtotalsPosition": {"expr": literal("'Top'")}}}]
    run_clean(
        document, {"op": "set_table_layout", "visual": "matrix00000000000001", "stepped": False}
    )
    objects = body(document, "matrix00000000000001")["objects"]
    assert objects["general"] == [{"properties": {"layout": {"expr": literal("'Outline'")}}}]
    assert "rowHeaders" not in objects
    objects["subTotals"][0]["properties"]["rowSubtotalsPosition"] = {"expr": literal("'Bottom'")}
    run(document, {"op": "set_table_layout", "visual": "matrix00000000000001", "stepped": False})
    assert body(document, "matrix00000000000001")["objects"]["general"][0]["properties"] == {
        "layout": {"expr": literal("'Tabular'")}
    }
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_table_layout",
                "visual": "matrix00000000000001",
                "stepped": False,
                "layout": "compact",
            },
        )
    assert str(raised.value) == (
        "A stepped layout off is the tabular layout here; give one of the two"
    )


def test_a_percent_rule_starts_below_100_and_ends_above_0() -> None:
    """Spec item 9: Desktop's dialog refuses a lower bound of 100% (and an upper one of 0%)."""
    for rule, message in (
        (
            {"min": 100, "unit": "percent", "color": "#FF0000"},
            "A percent rule has to start below 100",
        ),
        ({"max": 0, "unit": "percent", "color": "#FF0000"}, "A percent rule has to end above 0"),
    ):
        with pytest.raises(DocumentError) as raised:
            run(
                twin(),
                {
                    "op": "set_conditional_format",
                    "visual": TABLE,
                    "style": "rules",
                    "target": "background",
                    "field": "Rank",
                    "rules": [rule],
                },
            )
        assert str(raised.value) == f"Rule 1: {message}"


def test_move_column_cards_count_places_from_one() -> None:
    """Review item 1 (card side): 'to column 3' is the third place, index 2."""
    card = next(c for c in cf.TOOL_CARDS if c.op == "move_column")
    assert [s.name for s in card.slots] == ["visual", "field", "place"]
    built = card.to_operation({"visual": TABLE, "field": "Rank", "place": 4})
    assert built == {"op": "move_column", "visual": TABLE, "field": "Rank", "to_index": 3}
    document = twin()
    outcome = run(document, built)
    assert outcome.summaries == ["Moved the Rank column of the table of Agent ID to place 4."]
    refs = [
        p["queryRef"] for p in body(document, TABLE)["query"]["queryState"]["Values"]["projections"]
    ]
    assert refs.index("# Measures.Rank") == 3


def test_every_card_names_its_capability_and_no_example_needs_an_unfillable_setting() -> None:
    capabilities = {card.op: card.capability for card in cf.TOOL_CARDS}
    assert capabilities == {
        "set_conditional_format": "conditional_formatting",
        "clear_conditional_format": "conditional_formatting",
        "set_number_format": "visual_number_formats",
        "set_table_layout": "table_layout",
        "move_column": "table_layout",
    }
    for card in cf.TOOL_CARDS:
        for _, operation in card.examples:
            assert "replace_conditional" not in operation, card.op
    conditional = next(c for c in cf.TOOL_CARDS if c.op == "set_conditional_format")
    assert {"mid_value", "threshold_unit", "apply_to", "blank", "summarization"} <= {
        s.name for s in conditional.slots
    }
    built = conditional.to_operation(
        {
            "visual": TABLE,
            "style": "rules",
            "target": "background",
            "field": "Rank",
            "threshold": 50,
            "threshold_unit": "percent",
            "apply_to": "values_and_totals",
            "below_color": "#D64550",
            "above_color": "#1AAB40",
        }
    )
    assert built["rules"] == [
        {"max": 50, "color": "#D64550", "unit": "percent"},
        {"min": 50, "color": "#1AAB40", "unit": "percent"},
    ]
    assert built["apply_to"] == "values_and_totals"
    run_clean(twin(), built)
