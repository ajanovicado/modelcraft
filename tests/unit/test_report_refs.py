from __future__ import annotations

import copy
import json
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import report_refs as refs
from ai_powerbi_analyst.studio.pbir_document import (
    PAGES,
    REPORT,
    ROOT,
    DocumentError,
    PageDoc,
    PbirDocument,
)
from ai_powerbi_analyst.studio.pbix_package import PbixPackage

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
TWIN = FIXTURES / "structure-twin.pbix"
MULTIPAGE = FIXTURES / "multipage.pbix"
HOME = "ReportSectionHome"
DETAILS = "ReportSectionDetails"
TIP = "ReportSectionTip"
F_RANK = "Filter" + "a" * 20
F_TOP = "Filter" + "b" * 20
F_DRILL = "Filter" + "d" * 20


def measure(entity: str, prop: str, source: str | None = None) -> dict[str, Any]:
    ref = {"Source": source} if source else {"Entity": entity}
    return {"Measure": {"Expression": {"SourceRef": ref}, "Property": prop}}


def column(entity: str, prop: str, source: str | None = None) -> dict[str, Any]:
    ref = {"Source": source} if source else {"Entity": entity}
    return {"Column": {"Expression": {"SourceRef": ref}, "Property": prop}}


def literal(text: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": "'" + text.replace("'", "''") + "'"}}}


def projection(field_expr: dict[str, Any], query_ref: str, native: str, **extra: Any) -> Any:
    return {"field": field_expr, "queryRef": query_ref, "nativeQueryRef": native, **extra}


def visual(
    name: str,
    visual_type: str,
    projections: list[Any],
    tab: int = 0,
    sort: list[Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    inner: dict[str, Any] = {
        "visualType": visual_type,
        "query": {"queryState": {"Values": {"projections": projections}}},
    }
    if sort:
        inner["query"]["sortDefinition"] = {"sort": sort}
    inner.update(extra.pop("inner", {}))
    return {
        "name": name,
        "position": {"x": 0, "y": 0, "z": tab, "width": 100, "height": 100, "tabOrder": tab},
        "visual": inner,
        **extra,
    }


def group(name: str, title: str, parent: str | None = None, tab: int = 0) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": name,
        "position": {"x": 0, "y": 0, "z": tab, "width": 100, "height": 100, "tabOrder": tab},
        "visualGroup": {"displayName": title, "groupMode": "ScaleMode"},
    }
    if parent:
        data["parentGroupName"] = parent
    return data


def rank_filter_body() -> dict[str, Any]:
    """A filter like Desktop writes it: the field names the entity, the Where uses an alias."""
    return {
        "name": F_RANK,
        "field": measure("Measures", "Rank Dyn"),
        "type": "Advanced",
        "filter": {
            "Version": 2,
            "From": [{"Name": "m", "Entity": "Measures", "Type": 0}],
            "Where": [
                {
                    "Condition": {
                        "Comparison": {
                            "ComparisonKind": 0,
                            "Left": measure("Measures", "Rank Dyn", "m"),
                            "Right": {"Literal": {"Value": "1L"}},
                        }
                    }
                }
            ],
        },
        "howCreated": "User",
    }


def top_filter_body() -> dict[str, Any]:
    """Top N: a subquery with its own From that orders by a measure through its own alias."""
    return {
        "name": F_TOP,
        "field": column("Agents", "Name"),
        "type": "TopN",
        "filter": {
            "Version": 2,
            "From": [
                {
                    "Name": "subquery",
                    "Expression": {
                        "Subquery": {
                            "Query": {
                                "Version": 2,
                                "From": [
                                    {"Name": "d", "Entity": "Agents", "Type": 0},
                                    {"Name": "q", "Entity": "Measures", "Type": 0},
                                ],
                                "Select": [
                                    {**column("Agents", "Name", "d"), "Name": "field"},
                                ],
                                "OrderBy": [
                                    {
                                        "Direction": 2,
                                        "Expression": measure("Measures", "Sales", "q"),
                                    }
                                ],
                                "Top": 10,
                            }
                        }
                    },
                    "Type": 2,
                },
                {"Name": "a", "Entity": "Agents", "Type": 0},
            ],
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [column("Agents", "Name", "a")],
                            "Table": {"SourceRef": {"Source": "subquery"}},
                        }
                    }
                }
            ],
        },
        "howCreated": "User",
    }


def page_json(name: str, title: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "displayName": title, "width": 1280, "height": 720, **extra}


def bookmark_json(name: str, title: str, sections: dict[str, Any], **extra: Any) -> dict[str, Any]:
    first = next(iter(sections))
    return {
        "name": name,
        "displayName": title,
        "explorationState": {"version": "1.3", "activeSection": first, "sections": sections},
        **extra,
    }


def multipage_parts() -> dict[str, Any]:
    table = visual(
        "tbl",
        "tableEx",
        [
            projection(measure("Measures", "Sales"), "Measures.Sales", "Sales"),
            projection(
                measure("Measures", "Sales"), "Measures.Sales2", "Sales", displayName="Total"
            ),
            projection(measure("Measures", "Rank Dyn"), "Measures.Rank Dyn", "Rank Dyn"),
        ],
        tab=0,
        sort=[{"field": measure("Measures", "Sales"), "direction": "Descending"}],
        inner={
            "objects": {
                "values": [
                    {
                        "properties": {
                            "fontColor": {
                                "solid": {"color": {"expr": measure("Measures", "CF Color")}}
                            }
                        },
                        "selector": {
                            "data": [{"dataViewWildcard": {"matchingOption": 1}}],
                            "metadata": "Measures.Sales",
                        },
                    }
                ]
            },
        },
        filterConfig={"filters": [rank_filter_body(), top_filter_body()]},
    )
    card = visual(
        "card",
        "cardVisual",
        [
            projection(
                measure("Measures", "Sales"), "Measures.Sales", "Sales", displayName="Revenue"
            )
        ],
        tab=1000,
        inner={
            "objects": {
                "image": [{"properties": {"imageUrl": measure("Measures", "SVG Sales")}}],
                "label": [
                    {
                        "properties": {"text": literal("Revenue")},
                        "selector": {"metadata": "Measures.Sales"},
                    }
                ],
            }
        },
    )
    slicer = visual(
        "slc",
        "slicer",
        [projection(column("Fields", "Fields"), "Fields.Fields", "Fields")],
        tab=2000,
    )
    button = visual(
        "btn",
        "actionButton",
        [],
        tab=3000,
        inner={
            "visualContainerObjects": {
                "visualLink": [
                    {
                        "properties": {
                            "type": literal("PageNavigation"),
                            "navigationSection": literal(DETAILS),
                        }
                    }
                ]
            }
        },
    )
    drill = visual(
        "dbtn",
        "actionButton",
        [],
        tab=4000,
        inner={
            "visualContainerObjects": {
                "visualLink": [
                    {
                        "properties": {
                            "type": literal("Drillthrough"),
                            "drillthroughSection": literal(DETAILS),
                        }
                    }
                ]
            }
        },
    )
    details_visual = visual(
        "dvis",
        "clusteredBarChart",
        [projection(measure("Measures", "Sales"), "Measures.Sales", "Sales")],
        inner={
            "visualContainerObjects": {
                "visualTooltip": [
                    {"properties": {"type": literal("ReportPage"), "section": literal(TIP)}}
                ]
            }
        },
    )
    home = page_json(
        HOME,
        "Home",
        visualInteractions=[
            {"source": "tbl", "target": "card", "type": "NoFilter"},
            {"source": "card", "target": "leaf", "type": "NoFilter"},
            {"source": "child", "target": "slc", "type": "Filter"},
        ],
    )
    details = page_json(
        DETAILS,
        "Details",
        filterConfig={
            "filters": [
                {
                    "name": F_DRILL,
                    "field": column("Agents", "Name"),
                    "type": "Categorical",
                    "howCreated": "Drill",
                }
            ]
        },
        pageBinding={
            "name": "drill0001",
            "type": "Drillthrough",
            "parameters": [
                {
                    "name": "Param_" + F_DRILL,
                    "boundFilter": F_DRILL,
                    "fieldExpr": column("Agents", "Name"),
                }
            ],
        },
    )
    tip = page_json(TIP, "Tip", pageBinding={"name": "tip0001", "type": "Tooltip"})
    parts: dict[str, Any] = {
        REPORT: {"$schema": "report", "themeCollection": {}},
        PAGES: {"pageOrder": [HOME, DETAILS, TIP], "activePageName": HOME},
        f"{ROOT}pages/{HOME}/page.json": home,
        f"{ROOT}pages/{DETAILS}/page.json": details,
        f"{ROOT}pages/{TIP}/page.json": tip,
        f"{ROOT}bookmarks/bookmarks.json": {
            "items": [{"name": "Bookmark1"}, {"name": "Bookmark2"}]
        },
        f"{ROOT}bookmarks/Bookmark1.bookmark.json": bookmark_json(
            "Bookmark1",
            "Overview",
            {
                HOME: {
                    "visualContainers": {
                        "tbl": {
                            "singleVisual": {"visualType": "tableEx"},
                            "filters": {
                                "byExpr": [
                                    {
                                        "name": F_RANK,
                                        "expression": measure("Measures", "Rank Dyn"),
                                        "type": "Advanced",
                                    }
                                ]
                            },
                        },
                        "card": {"singleVisual": {"visualType": "cardVisual"}},
                    },
                    "visualContainerGroups": {"grp": {"isHidden": True}},
                }
            },
            options={"targetVisualNames": ["tbl", "card"], "applyOnlyToTargetVisuals": True},
        ),
        f"{ROOT}bookmarks/Bookmark2.bookmark.json": bookmark_json(
            "Bookmark2", "Detail view", {DETAILS: {"visualContainers": {"dvis": {}}}}
        ),
        "DAXQueries/Query%201.dax": (
            'EVALUATE ROW("a", [Sales], "note", "[Rank A]") // [Rank B]\n'
            "/* [Rank B] */ EVALUATE ROW(\"b\", 'Measures'[Rank A])\n"
        ).encode("utf-16-le"),
    }
    for page, visuals in (
        (
            HOME,
            [
                table,
                card,
                slicer,
                button,
                drill,
                group("grp", "Group 1", tab=5000),
                {**visual("child", "card", [], tab=6000), "parentGroupName": "grp"},
                group("sub", "Inner", parent="grp", tab=7000),
                {**visual("leaf", "card", [], tab=8000), "parentGroupName": "sub"},
            ],
        ),
        (DETAILS, [details_visual]),
    ):
        for item in visuals:
            parts[f"{ROOT}pages/{page}/visuals/{item['name']}/visual.json"] = item
    return parts


MEASURES = {
    "Sales": "SUM(Fact[Amount])",
    "Rank Dyn": 'IF(_f = "Sales", [Rank A], [Rank B])',
    "Rank A": "RANKX(ALL(Agents), [Sales])",
    "Rank B": "1",
    "Ratio": "DIVIDE([Sales], 'Fact Table'[Amount])",
    "Unused": "1 + 1",
    "SVG Sales": '"<svg>" & [Sales] & "</svg>"',
    "CF Color": 'IF([Sales] > 1, "green", "red")',
}
CALCULATED = {
    "Fact Table[Margin]": "[Sales] - 'Fact Table'[Cost]",
    "Fields": "{(\"Sales\", NAMEOF('Measures'[Sales]), 0)}",
    "Bands": "GENERATESERIES(1, 3)",
}


def make_pbix(directory: Path, parts: dict[str, Any], name: str = "report.pbix") -> Path:
    path = directory / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for part, value in parts.items():
            data = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
            archive.writestr(part, data)
    return path


def open_document(path: Path) -> PbirDocument:
    return PbirDocument(PbixPackage(path))


@pytest.fixture
def multipage(tmp_path: Path) -> PbirDocument:
    return open_document(make_pbix(tmp_path, multipage_parts()))


@pytest.fixture
def twin() -> PbirDocument:
    if not TWIN.exists():
        pytest.skip("structure twin fixture not present")
    return open_document(TWIN)


def kinds(uses: list[refs.Use]) -> dict[str, int]:
    return dict(sorted(Counter(u.kind for u in uses).items()))


# --- DAX scanner ---------------------------------------------------------------------------


def names(expression: str) -> list[tuple[str | None, str | None]]:
    return [(r.table, r.name) for r in refs.dax_references(expression)]


def test_dax_references_ignore_strings_and_comments() -> None:
    expression = (
        '"[Fake]" & [Real] // [Line]\n-- [Dash]\n/* [Block] */ + [Second] & "say ""[Quoted]"" "'
    )
    assert names(expression) == [(None, "Real"), (None, "Second")]


def test_dax_references_qualified_and_quoted_tables() -> None:
    expression = "Sales[Amount] + 'Fact Table'[Cost] + 'O''Brien Co'[Total] + [Bare]"
    assert names(expression) == [
        ("Sales", "Amount"),
        ("Fact Table", "Cost"),
        ("O'Brien Co", "Total"),
        (None, "Bare"),
    ]


def test_dax_references_keyword_before_bracket_is_not_a_table() -> None:
    expression = "VAR x = 1 RETURN [Measure A] + IF(NOT [Flag], [B], 0)"
    assert names(expression) == [(None, "Measure A"), (None, "Flag"), (None, "B")]


def test_dax_references_nameof_and_table_only_references() -> None:
    expression = '{("Sales", NAMEOF(\'Measures\'[Total Sales]), 0), ("x", NAMEOF([Bare]), 1)}'
    assert names(expression) == [("Measures", "Total Sales"), (None, "Bare")]
    only = refs.dax_references("COUNTROWS('Field Parameter') + COUNTROWS(Dates)", tables=True)
    assert [(r.table, r.name) for r in only] == [
        ("Field Parameter", None),
        ("Dates", None),
    ]
    assert refs.dax_references("COUNTROWS('Field Parameter')") == []


def test_dax_references_escaped_bracket_and_malformed_text_never_raise() -> None:
    assert names("[Rate]] Odd] + [B]") == [(None, "Rate] Odd"), (None, "B")]
    assert names("[Unclosed") == [(None, "Unclosed")]
    assert names("'Open quote [X]") == []
    assert names('"Open string [X]') == []
    assert names("/* Open comment [X]") == []
    assert refs.dax_references("") == []


def test_rename_dax_references_rewrites_only_matching_measure() -> None:
    expression = "[Old] + Facts[Old] + 'Other'[Old] + \"[Old]\" // [Old]\n + [Older]"
    text, count = refs.rename_dax_references(expression, "Facts", "old", "New]Name")
    assert count == 2
    assert text == (
        "[New]]Name] + Facts[New]]Name] + 'Other'[Old] + \"[Old]\" // [Old]\n + [Older]"
    )


# --- golden counts on the structure twin -----------------------------------------------------

TWIN_MEASURES = {
    "Rank Filtering Dynamics": (
        'IF(_Selected_Field = "MTD Sales", [Rank Filtering Employyees MTD], '
        "[Rank Filtering Employyees MTD%])"
    ),
    "Rank Filtering Employyees MTD": "RANKX(ALL(DimEmployee), [Table MTD Sales])",
    "Rank Filtering Employyees MTD%": "RANKX(ALL(DimEmployee), [vs PM MOM%])",
    "Table MTD Sales": "SUM(FactSales[Sales])",
    "vs PM MOM%": "DIVIDE([Table MTD Sales], 1)",
}
TABLE_VISUAL = "e82e02f93055d6b0b544"


def test_twin_golden_counts_per_kind(twin: PbirDocument) -> None:
    index = refs.ReferenceIndex.build(twin, measures=TWIN_MEASURES)
    assert kinds(index.measure_uses("Rank Filtering Dynamics")) == {
        "filter_field": 1,
        "filter_where": 1,
    }
    assert kinds(index.measure_uses("Rank")) == {"projection": 1, "selector": 3, "sort": 1}
    assert kinds(index.measure_uses("CF Table")) == {"object_expression": 2}
    assert kinds(index.measure_uses("Table MTD Sales")) == {
        "measure_dax": 2,
        "projection": 1,
        "selector": 3,
    }
    assert kinds(index.measure_uses("Rank Filtering Employyees MTD")) == {"measure_dax": 1}
    assert kinds(index.measure_uses("Rank Filtering Employyees MTD%")) == {"measure_dax": 1}


def test_twin_from_alias_use_is_found_with_its_pointer(twin: PbirDocument) -> None:
    index = refs.ReferenceIndex.build(twin, measures=TWIN_MEASURES)
    where = [u for u in index.measure_uses("Rank Filtering Dynamics") if u.kind == "filter_where"]
    assert [u.where for u in where] == [
        "/filterConfig/filters/0/filter/Where/0/Condition/Comparison/Left/Measure"
    ]
    assert where[0].part.endswith(f"visuals/{TABLE_VISUAL}/visual.json")
    assert where[0].label == "table 'Agent ID' (filter)"


def test_twin_measure_only_used_through_other_measures(twin: PbirDocument) -> None:
    index = refs.ReferenceIndex.build(twin, measures=TWIN_MEASURES)
    inner = "Rank Filtering Employyees MTD"
    assert index.measure_dependents(inner) == ["Rank Filtering Dynamics"]
    assert index.visual_users_of_measure(inner) == [TABLE_VISUAL]
    # Transitive: the base measure feeds the inner ones, which feed the filter's measure.
    assert index.measure_dependents("Table MTD Sales") == [
        "Rank Filtering Dynamics",
        "Rank Filtering Employyees MTD",
        "Rank Filtering Employyees MTD%",
        "vs PM MOM%",
    ]
    assert index.measure_dependents("vs PM MOM%") == [
        "Rank Filtering Dynamics",
        "Rank Filtering Employyees MTD%",
    ]


def test_twin_delete_guard_names_dependents_and_the_visual(twin: PbirDocument) -> None:
    index = refs.ReferenceIndex.build(twin, measures=TWIN_MEASURES)
    assert refs.delete_guard(index, "Rank Filtering Employyees MTD") == [
        "used by the measure 'Rank Filtering Dynamics'",
        "needed indirectly by 1 visual: table 'Agent ID'",
    ]
    assert refs.delete_guard(index, "Rank Filtering Dynamics") == [
        "used by 1 visual: table 'Agent ID'",
        "used in 1 filter",
    ]
    assert refs.delete_guard(index, "No Such Measure") == []


def test_twin_dax_query_counts_as_a_use(twin: PbirDocument) -> None:
    index = refs.ReferenceIndex.build(twin)
    assert kinds(index.measure_uses("Anything")) == {}
    assert index.measure_uses("Rank Filtering Dynamics")[0].part.startswith(ROOT)


def test_twin_rename_through_from_alias_and_stable_query_refs(twin: PbirDocument) -> None:
    before = copy.deepcopy(twin.visual(TABLE_VISUAL)[1])
    result = refs.rename_measure_references(
        twin, "# Measures", "Rank Filtering Dynamics", "Zzz Renamed"
    )
    assert result.counts == {"filter_field": 1, "filter_where": 1}
    page, after = twin.visual(TABLE_VISUAL)
    assert result.parts == {f"{page.folder}visuals/{TABLE_VISUAL}/visual.json"}
    config = after["filterConfig"]["filters"][0]
    assert config["field"]["Measure"]["Property"] == "Zzz Renamed"
    left = config["filter"]["Where"][0]["Condition"]["Comparison"]["Left"]["Measure"]
    assert left["Property"] == "Zzz Renamed"
    # The alias itself and everything else are untouched.
    assert left["Expression"]["SourceRef"] == {"Source": "#"}
    assert "Rank Filtering Dynamics" not in json.dumps(after)
    assert json.dumps(after).replace("Zzz Renamed", "Rank Filtering Dynamics") == json.dumps(before)
    assert twin.commit() == sorted(result.parts)


def test_twin_rename_leaves_query_ref_and_selector_metadata(twin: PbirDocument) -> None:
    result = refs.rename_measure_references(twin, "# Measures", "Rank", "Position")
    assert result.counts == {"projection": 1, "sort": 1}
    _, table = twin.visual(TABLE_VISUAL)
    projection_ = table["visual"]["query"]["queryState"]["Values"]["projections"][2]
    assert projection_["field"]["Measure"]["Property"] == "Position"
    assert projection_["queryRef"] == "# Measures.Rank"
    # No displayName on this projection, and its nativeQueryRef equalled the old name.
    assert projection_["nativeQueryRef"] == "Position"
    text = json.dumps(table)
    assert text.count('"metadata": "# Measures.Rank"') == 3


def test_twin_rename_keeps_a_native_query_ref_that_is_not_the_old_name(
    twin: PbirDocument,
) -> None:
    refs.rename_measure_references(twin, "# Measures", "Employee Name", "Agent")
    _, table = twin.visual(TABLE_VISUAL)
    name_projection = table["visual"]["query"]["queryState"]["Values"]["projections"][1]
    assert name_projection["field"]["Measure"]["Property"] == "Agent"
    assert name_projection["nativeQueryRef"] == "Agent Name"
    assert name_projection["displayName"] == "Agent Name"


def test_twin_rename_ignores_a_measure_in_another_table(twin: PbirDocument) -> None:
    result = refs.rename_measure_references(twin, "Other Table", "Rank", "Position")
    assert result.counts == {}
    assert result.parts == set()
    assert twin.commit() == []


# --- the synthetic multi-page report -----------------------------------------------------------


def visual_part(page: str, name: str) -> str:
    return f"{ROOT}pages/{page}/visuals/{name}/visual.json"


def test_multipage_measure_uses_golden(multipage: PbirDocument) -> None:
    index = refs.ReferenceIndex.build(multipage, measures=MEASURES, calculated=CALCULATED)
    assert kinds(index.measure_uses("Sales")) == {
        "calculated_column": 1,  # Fact Table[Margin]
        "dax_query": 1,  # the string "[Rank A]" and the comments are not uses
        "field_parameter": 1,  # NAMEOF('Measures'[Sales])
        "filter_where": 1,  # the Top N subquery orders by it through its own alias
        "measure_dax": 4,  # Rank A, Ratio, SVG Sales, CF Color
        "projection": 4,
        "selector": 2,
        "sort": 1,
    }
    assert kinds(index.measure_uses("Rank Dyn")) == {
        "bookmark": 1,
        "filter_field": 1,
        "filter_where": 1,
        "projection": 1,
    }
    assert kinds(index.measure_uses("CF Color")) == {"object_expression": 1}
    assert kinds(index.measure_uses("SVG Sales")) == {"object_expression": 1}
    assert kinds(index.measure_uses("Rank A")) == {"dax_query": 1, "measure_dax": 1}
    assert kinds(index.measure_uses("Rank B")) == {"measure_dax": 1}
    assert index.measure_uses("Unused") == []


def test_multipage_table_qualifier_narrows_field_uses(multipage: PbirDocument) -> None:
    index = refs.ReferenceIndex.build(multipage, measures=MEASURES, calculated=CALCULATED)
    assert kinds(index.measure_uses("Sales", "measures")) == kinds(index.measure_uses("Sales"))
    elsewhere = kinds(index.measure_uses("Sales", "Nope"))
    assert "projection" not in elsewhere and "sort" not in elsewhere and "selector" not in elsewhere


def test_multipage_thin_report_has_no_model_uses(multipage: PbirDocument) -> None:
    index = refs.ReferenceIndex.build(multipage)
    assert kinds(index.measure_uses("Sales")) == {
        "dax_query": 1,
        "filter_where": 1,
        "projection": 4,
        "selector": 2,
        "sort": 1,
    }
    assert index.measure_dependents("Sales") == []
    assert index.visual_users_of_measure("Sales") == ["tbl", "card", "dvis"]


def test_multipage_dependents_are_transitive_and_cover_columns_and_field_parameters(
    multipage: PbirDocument,
) -> None:
    index = refs.ReferenceIndex.build(multipage, measures=MEASURES, calculated=CALCULATED)
    assert index.measure_dependents("Sales") == [
        "CF Color",
        "Rank A",
        "Rank Dyn",
        "Ratio",
        "SVG Sales",
        "Fact Table[Margin]",
        "Fields",
    ]
    assert index.measure_dependents("Rank B") == ["Rank Dyn"]
    assert index.measure_dependents("Unused") == []
    # The slicer only shows the field parameter table, which names [Sales] through NAMEOF.
    assert index.visual_users_of_measure("Sales") == ["tbl", "card", "slc", "dvis"]
    assert index.visual_users_of_measure("Rank A") == ["tbl"]


def test_multipage_visual_page_and_filter_uses(multipage: PbirDocument) -> None:
    index = refs.ReferenceIndex.build(multipage)
    assert kinds(index.visual_uses("tbl")) == {"bookmark": 2, "visual_interaction": 1}
    assert kinds(index.visual_uses("card")) == {"bookmark": 2, "visual_interaction": 2}
    assert kinds(index.visual_uses("grp")) == {"bookmark": 1, "group_member": 2}
    assert kinds(index.visual_uses("sub")) == {"group_member": 1}
    assert kinds(index.visual_uses("dvis")) == {"bookmark": 1}
    # A tab order is a property of the visual, not a reference to it.
    assert index.visual_uses("btn") == []
    assert kinds(index.page_uses(DETAILS)) == {
        "bookmark": 2,
        "drillthrough": 1,
        "page_binding": 1,
        "visual_link": 1,
    }
    assert kinds(index.page_uses(TIP)) == {"page_binding": 1, "tooltip": 1}
    assert kinds(index.page_uses(HOME)) == {"bookmark": 2}
    assert index.page_uses("Details") == []  # pages are named by their folder, not their title
    assert kinds(index.filter_uses(F_RANK)) == {"bookmark": 1, "filter_field": 1}
    assert kinds(index.filter_uses(F_TOP)) == {"filter_field": 1}
    assert kinds(index.filter_uses(F_DRILL)) == {"bound_filter": 1, "page_filter": 1}


def test_multipage_delete_guard(multipage: PbirDocument) -> None:
    index = refs.ReferenceIndex.build(multipage, measures=MEASURES, calculated=CALCULATED)
    assert refs.delete_guard(index, "Sales") == [
        "used by 3 visuals: table 'Sales', card 'Revenue', clustered bar chart 'Sales'",
        "used in 1 filter",
        "used in 1 DAX query",
        "used by the measure 'CF Color', the measure 'Rank A', the measure 'Rank Dyn' and 4 more",
        "needed indirectly by 1 visual: slicer 'Fields'",
    ]
    assert refs.delete_guard(index, "Rank Dyn") == [
        "used by 1 visual: table 'Sales'",
        "used in 1 filter",
        "used in 1 bookmark",
    ]
    assert refs.delete_guard(index, "Rank A") == [
        "used in 1 DAX query",
        "used by the measure 'Rank Dyn'",
        "needed indirectly by 1 visual: table 'Sales'",
    ]
    assert refs.delete_guard(index, "Unused") == []


def test_rename_rewrites_every_layer_and_reports_what_it_could_not(
    multipage: PbirDocument,
) -> None:
    index = refs.ReferenceIndex.build(multipage, measures=MEASURES, calculated=CALCULATED)
    result = refs.rename_measure_references(multipage, "Measures", "Sales", "Revenue", index=index)
    assert result.counts == {"projection": 4, "sort": 1, "filter_where": 1}
    assert result.parts == {
        visual_part(HOME, "tbl"),
        visual_part(HOME, "card"),
        visual_part(DETAILS, "dvis"),
    }
    _, table = multipage.visual("tbl")
    projections = table["visual"]["query"]["queryState"]["Values"]["projections"]
    assert projections[0]["field"]["Measure"]["Property"] == "Revenue"
    assert projections[0]["nativeQueryRef"] == "Revenue"  # equalled the old name, no displayName
    assert projections[0]["queryRef"] == "Measures.Sales"  # Desktop keeps queryRef stable
    assert projections[1]["field"]["Measure"]["Property"] == "Revenue"
    assert projections[1]["nativeQueryRef"] == "Sales"  # displayName 'Total' is what shows
    order = table["filterConfig"]["filters"][1]["filter"]["From"][0]["Expression"]["Subquery"][
        "Query"
    ]["OrderBy"][0]["Expression"]["Measure"]
    assert order["Property"] == "Revenue"
    assert order["Expression"]["SourceRef"] == {"Source": "q"}
    assert table["visual"]["objects"]["values"][0]["selector"]["metadata"] == "Measures.Sales"
    _, card = multipage.visual("card")
    assert card["visual"]["objects"]["label"][0]["selector"]["metadata"] == "Measures.Sales"
    # DAX in the model and DAX query files cannot be rewritten from the report layer.
    assert kinds(result.unrewritten) == {
        "calculated_column": 1,
        "dax_query": 1,
        "field_parameter": 1,
        "measure_dax": 4,
    }
    assert multipage.commit() == sorted(result.parts)
    left = refs.ReferenceIndex.build(multipage).measure_uses("Sales", "Measures")
    assert kinds(left) == {"dax_query": 1}  # only the query file still says [Sales]


def test_rename_reaches_bookmarks_and_marks_them_for_writing(multipage: PbirDocument) -> None:
    result = refs.rename_measure_references(multipage, "Measures", "Rank Dyn", "Rank Dynamic")
    bookmark = f"{ROOT}bookmarks/Bookmark1.bookmark.json"
    assert result.counts == {"bookmark": 1, "filter_field": 1, "filter_where": 1, "projection": 1}
    assert result.parts == {bookmark, visual_part(HOME, "tbl")}
    live = multipage.bookmark("Bookmark1")
    filters = live["explorationState"]["sections"][HOME]["visualContainers"]["tbl"]["filters"]
    assert filters["byExpr"][0]["expression"]["Measure"]["Property"] == "Rank Dynamic"
    assert filters["byExpr"][0]["name"] == F_RANK
    assert multipage.commit() == sorted([bookmark, visual_part(HOME, "tbl")])
    stored = multipage.package.read_json(bookmark)
    stored_filters = stored["explorationState"]["sections"][HOME]["visualContainers"]["tbl"]
    assert stored_filters["filters"]["byExpr"][0]["expression"]["Measure"]["Property"] == (
        "Rank Dynamic"
    )
    index = refs.ReferenceIndex.build(multipage)
    assert kinds(index.measure_uses("Rank Dynamic")) == {
        "bookmark": 1,
        "filter_field": 1,
        "filter_where": 1,
        "projection": 1,
    }
    assert index.measure_uses("Rank Dyn") == []


def test_report_level_extension_measures_are_indexed_and_renamed(tmp_path: Path) -> None:
    parts = multipage_parts()
    extension = f"{ROOT}reportExtensions.json"
    parts[extension] = {
        "name": "extension",
        "entities": [
            {
                "name": "Measures",
                "measures": [
                    {
                        "name": "Ext Ratio",
                        "dataType": "Double",
                        "expression": "DIVIDE([Sales], Measures[Rank B]) + [Rank A]",
                    }
                ],
            }
        ],
    }
    document = open_document(make_pbix(tmp_path, parts))
    index = refs.ReferenceIndex.build(document, measures=MEASURES)
    assert "Ext Ratio" in index.measure_dependents("Sales")
    assert index.measure_dependents("Rank B") == ["Ext Ratio", "Rank Dyn"]
    result = refs.rename_measure_references(document, "Measures", "Rank B", "Rank Two")
    assert result.counts == {"measure_dax": 1}
    assert result.parts == {extension}
    stored = document.package.read_json(extension)
    assert stored["entities"][0]["measures"][0]["expression"] == (
        "DIVIDE([Sales], Measures[Rank Two]) + [Rank A]"
    )
    renamed = refs.rename_measure_references(document, "Measures", "Ext Ratio", "Ratio 2")
    assert renamed.counts == {"measure_dax": 1}
    assert document.package.read_json(extension)["entities"][0]["measures"][0]["name"] == "Ratio 2"


# --- deleting visuals ----------------------------------------------------------------------------


def test_removing_a_group_deletes_nested_descendants_and_their_references(
    multipage: PbirDocument,
) -> None:
    home = multipage.page(HOME)
    result = refs.remove_visual_references(multipage, home, "grp")
    assert result.deleted == ["grp", "child", "sub", "leaf"]
    assert list(home.visuals) == ["tbl", "card", "slc", "btn", "dbtn"]
    assert home.data["visualInteractions"] == [
        {"source": "tbl", "target": "card", "type": "NoFilter"}
    ]
    assert result.counts == {"visual_interaction": 2, "bookmark": 1}
    bookmark = f"{ROOT}bookmarks/Bookmark1.bookmark.json"
    assert result.parts == {f"{ROOT}pages/{HOME}/page.json", bookmark}
    live = multipage.bookmark("Bookmark1")
    assert live["explorationState"]["sections"][HOME]["visualContainerGroups"] == {}
    assert multipage.commit() == sorted(result.parts)
    stored = multipage.package.read_json(bookmark)
    assert stored["explorationState"]["sections"][HOME]["visualContainerGroups"] == {}
    remaining = {n for n in multipage.package.names() if "/visuals/" in n}
    assert visual_part(HOME, "leaf") not in remaining
    assert visual_part(HOME, "sub") not in remaining
    assert visual_part(HOME, "tbl") in remaining


def test_removing_a_visual_clears_bookmark_states_and_target_lists(
    multipage: PbirDocument,
) -> None:
    result = refs.remove_visual_references(multipage, HOME, "card")
    assert result.deleted == ["card"]
    home = multipage.page(HOME)
    assert home.data["visualInteractions"] == [
        {"source": "child", "target": "slc", "type": "Filter"}
    ]
    stored = multipage.bookmark("Bookmark1")
    assert list(stored["explorationState"]["sections"][HOME]["visualContainers"]) == ["tbl"]
    assert stored["options"]["targetVisualNames"] == ["tbl"]
    assert refs.ReferenceIndex.build(multipage).visual_uses("card") == []


def test_removing_the_last_interaction_drops_the_list(tmp_path: Path) -> None:
    parts = multipage_parts()
    parts[f"{ROOT}pages/{HOME}/page.json"]["visualInteractions"] = [
        {"source": "tbl", "target": "card", "type": "NoFilter"}
    ]
    document = open_document(make_pbix(tmp_path, parts))
    refs.remove_visual_references(document, HOME, "tbl")
    assert "visualInteractions" not in document.page(HOME).data


def test_removing_a_group_without_its_members_promotes_them(multipage: PbirDocument) -> None:
    home = multipage.page(HOME)
    result = refs.remove_visual_references(multipage, home, "sub", group_children=False)
    assert result.deleted == ["sub"]
    assert home.visuals["leaf"]["parentGroupName"] == "grp"
    result = refs.remove_visual_references(multipage, home, "grp", group_children=False)
    assert result.deleted == ["grp"]
    assert "parentGroupName" not in home.visuals["child"]
    assert "parentGroupName" not in home.visuals["leaf"]


def test_removing_an_unknown_visual_is_refused(multipage: PbirDocument) -> None:
    with pytest.raises(DocumentError):
        refs.remove_visual_references(multipage, HOME, "missing")


# --- duplicating visuals and pages ---------------------------------------------------------------


def hex_names(filters: list[Any]) -> list[str]:
    return [f["name"] for f in filters]


def test_duplicate_visual_gets_new_filter_names_and_the_next_tab_order(
    multipage: PbirDocument,
) -> None:
    home = multipage.page(HOME)
    copy_ = copy.deepcopy(home.visuals["tbl"])
    copy_["name"] = "tbl2"
    home.visuals["tbl2"] = copy_
    refs.remap_duplicate_visual(multipage, home, "tbl2")
    new_names = hex_names(copy_["filterConfig"]["filters"])
    assert len(set(new_names)) == 2 and not {F_RANK, F_TOP} & set(new_names)
    for name in new_names:
        assert name.startswith("Filter") and len(name) == 26 and int(name[6:], 16) >= 0
    assert hex_names(home.visuals["tbl"]["filterConfig"]["filters"]) == [F_RANK, F_TOP]
    assert copy_["position"]["tabOrder"] == 9000  # the highest on the page is leaf at 8000
    assert visual_part(HOME, "tbl2") in multipage.commit()


def test_duplicate_visual_drops_its_group_unless_the_group_was_copied(
    multipage: PbirDocument,
) -> None:
    home = multipage.page(HOME)
    for name in ("child2", "child3"):
        copy_ = copy.deepcopy(home.visuals["child"])
        copy_["name"] = name
        home.visuals[name] = copy_
    refs.remap_duplicate_visual(multipage, home, "child2")
    refs.remap_duplicate_visual(multipage, home, "child3", copied_groups={"grp"})
    assert "parentGroupName" not in home.visuals["child2"]
    assert home.visuals["child3"]["parentGroupName"] == "grp"


def test_duplicate_visual_accepts_a_copy_that_is_not_on_a_page_yet(
    multipage: PbirDocument,
) -> None:
    home = multipage.page(HOME)
    detached = copy.deepcopy(home.visuals["tbl"])
    detached["name"] = "later"
    refs.remap_duplicate_visual(multipage, home, detached)
    assert detached["position"]["tabOrder"] == 9000
    assert not {F_RANK, F_TOP} & set(hex_names(detached["filterConfig"]["filters"]))
    other = multipage.page(DETAILS)
    onto_details = copy.deepcopy(home.visuals["tbl"])
    onto_details["name"] = "moved"
    other.visuals["moved"] = onto_details
    refs.remap_duplicate_visual(multipage, home, "moved")
    assert onto_details["position"]["tabOrder"] == 1000  # after 'dvis' at 0, on its own page
    with pytest.raises(DocumentError):
        refs.remap_duplicate_visual(multipage, home, "nope")


def duplicate_page_of(document: PbirDocument, source: str, name: str) -> PageDoc:
    original = document.page(source)
    data = copy.deepcopy(original.data)
    data.update({"name": name, "displayName": name})
    return PageDoc(name, data)


def test_duplicate_page_remaps_visuals_interactions_and_filters(multipage: PbirDocument) -> None:
    source = multipage.page(HOME)
    new = duplicate_page_of(multipage, HOME, "ReportSectionCopy")
    multipage.pages[new.name] = new
    mapping = refs.remap_duplicate_page(multipage, source, new)
    assert list(mapping) == list(source.visuals)
    assert list(new.visuals) == list(mapping.values())
    assert not set(mapping.values()) & set(source.visuals)
    assert all(len(v) == 20 and new.visuals[v]["name"] == v for v in mapping.values())
    assert new.data["visualInteractions"] == [
        {"source": mapping["tbl"], "target": mapping["card"], "type": "NoFilter"},
        {"source": mapping["card"], "target": mapping["leaf"], "type": "NoFilter"},
        {"source": mapping["child"], "target": mapping["slc"], "type": "Filter"},
    ]
    assert source.data["visualInteractions"][0]["source"] == "tbl"
    assert new.visuals[mapping["leaf"]]["parentGroupName"] == mapping["sub"]
    assert new.visuals[mapping["sub"]]["parentGroupName"] == mapping["grp"]
    copied = hex_names(new.visuals[mapping["tbl"]]["filterConfig"]["filters"])
    assert not {F_RANK, F_TOP} & set(copied)
    assert hex_names(source.visuals["tbl"]["filterConfig"]["filters"]) == [F_RANK, F_TOP]
    dirty = multipage.commit()
    assert f"{ROOT}pages/ReportSectionCopy/page.json" in dirty
    assert visual_part("ReportSectionCopy", mapping["tbl"]) in dirty


def test_duplicate_page_mints_a_new_binding_identity(multipage: PbirDocument) -> None:
    source = multipage.page(DETAILS)
    new = duplicate_page_of(multipage, DETAILS, "ReportSectionDetails2")
    refs.remap_duplicate_page(multipage, source, new)
    old_filter = source.data["filterConfig"]["filters"][0]["name"]
    new_filter = new.data["filterConfig"]["filters"][0]["name"]
    assert old_filter == F_DRILL and new_filter != F_DRILL and new_filter.startswith("Filter")
    binding = new.data["pageBinding"]
    assert binding["name"] != "drill0001" and len(binding["name"]) == 20
    assert binding["type"] == "Drillthrough"
    assert binding["parameters"][0]["boundFilter"] == new_filter
    assert binding["parameters"][0]["name"] == "Param_" + new_filter
    assert source.data["pageBinding"]["parameters"][0]["boundFilter"] == F_DRILL
    assert source.data["pageBinding"]["name"] == "drill0001"


def test_duplicate_page_pairs_copies_the_caller_already_made(multipage: PbirDocument) -> None:
    source = multipage.page(HOME)
    new = duplicate_page_of(multipage, HOME, "ReportSectionCopy")
    # A straight copy that kept the source's visual names (they must be unique report-wide).
    new.visuals = {n: copy.deepcopy(v) for n, v in source.visuals.items()}
    mapping = refs.remap_duplicate_page(multipage, source, new)
    assert not set(mapping.values()) & set(source.visuals)
    assert list(new.visuals) == list(mapping.values())
    assert new.visuals[mapping["leaf"]]["parentGroupName"] == mapping["sub"]
    short = duplicate_page_of(multipage, HOME, "ReportSectionShort")
    short.visuals = {"only": copy.deepcopy(source.visuals["tbl"])}
    with pytest.raises(DocumentError):
        refs.remap_duplicate_page(multipage, source, short)


# --- deleting pages ------------------------------------------------------------


def test_page_references_are_listed_in_plain_english(multipage: PbirDocument) -> None:
    assert refs.remove_page_references(multipage, DETAILS) == [
        "On page 'Home', button btn navigates to this page",
        "On page 'Home', button dbtn drills through to this page",
        "bookmark 'Detail view' opens it",
    ]
    assert refs.remove_page_references(multipage, "Details") == refs.remove_page_references(
        multipage, DETAILS
    )
    assert refs.remove_page_references(multipage, TIP) == [
        "On page 'Details', clustered bar chart 'Sales' uses this page as its tooltip"
    ]


def test_clearing_page_references_switches_off_actions_and_drops_bookmarks(
    multipage: PbirDocument,
) -> None:
    changes = refs.clear_page_references(multipage, DETAILS)
    assert changes == [
        "Removed the action of button btn that used the deleted page.",
        "Removed the action of button dbtn that used the deleted page.",
        "Deleted the bookmark 'Detail view' because it only covered the deleted page.",
    ]
    home = multipage.page(HOME)
    assert "visualLink" not in home.visuals["btn"]["visual"]["visualContainerObjects"]
    assert refs.remove_page_references(multipage, DETAILS) == []
    assert multipage.commit() == [
        f"{ROOT}bookmarks/bookmarks.json",
        visual_part(HOME, "btn"),
        visual_part(HOME, "dbtn"),
    ]
    assert not multipage.package.has(f"{ROOT}bookmarks/Bookmark2.bookmark.json")
    assert multipage.package.read_json(f"{ROOT}bookmarks/bookmarks.json") == {
        "items": [{"name": "Bookmark1"}]
    }


def test_clearing_a_tooltip_page_reference(multipage: PbirDocument) -> None:
    assert refs.clear_page_references(multipage, TIP) == [
        "Removed the tooltip of clustered bar chart 'Sales' that used the deleted page."
    ]
    _, dvis = multipage.visual("dvis")
    assert "visualTooltip" not in dvis["visual"]["visualContainerObjects"]


def test_a_bookmark_with_other_pages_keeps_them_and_moves_its_active_page(
    tmp_path: Path,
) -> None:
    parts = multipage_parts()
    parts[f"{ROOT}bookmarks/Bookmark2.bookmark.json"] = bookmark_json(
        "Bookmark2",
        "Detail view",
        {DETAILS: {"visualContainers": {"dvis": {}}}, HOME: {"visualContainers": {"tbl": {}}}},
    )
    document = open_document(make_pbix(tmp_path, parts))
    changes = refs.clear_page_references(document, DETAILS)
    assert "Removed the deleted page from the bookmark 'Detail view'." in changes
    stored = document.bookmark("Bookmark2")
    assert list(stored["explorationState"]["sections"]) == [HOME]
    assert stored["explorationState"]["activeSection"] == HOME
    assert document.commit() == [
        f"{ROOT}bookmarks/Bookmark2.bookmark.json",
        visual_part(HOME, "btn"),
        visual_part(HOME, "dbtn"),
    ]


# --- fixtures and module hygiene --------------------------------------------------


def test_the_module_does_not_import_the_edit_layer() -> None:
    source = Path(refs.__file__).read_text(encoding="utf-8")
    imports = [ln for ln in source.splitlines() if ln.startswith(("from ", "import "))]
    assert imports and not any("report_edits" in ln for ln in imports)


def test_use_kinds_cover_everything_the_index_reports(multipage: PbirDocument) -> None:
    index = refs.ReferenceIndex.build(multipage, measures=MEASURES, calculated=CALCULATED)
    seen: set[str] = set()
    for measure_name in MEASURES:
        seen.update(u.kind for u in index.measure_uses(measure_name))
    for name in ("tbl", "card", "grp", "sub"):
        seen.update(u.kind for u in index.visual_uses(name))
    for name in (HOME, DETAILS, TIP):
        seen.update(u.kind for u in index.page_uses(name))
    for name in (F_RANK, F_TOP, F_DRILL):
        seen.update(u.kind for u in index.filter_uses(name))
    assert seen <= set(refs.USE_KINDS)
    assert {"visual_link", "tooltip", "drillthrough", "bound_filter", "group_member"} <= seen


def test_real_multipage_fixture_golden_counts() -> None:
    if not MULTIPAGE.exists():
        pytest.skip("multipage fixture not present")
    document = open_document(MULTIPAGE)
    index = refs.ReferenceIndex.build(document)
    found = Counter(f.use.kind for f in index._fields)
    assert dict(sorted(found.items())) == {
        "bookmark": 1,
        "filter_field": 2,
        "filter_where": 4,
        "object_expression": 26,
        "page_binding": 2,
        "page_filter": 4,
        "projection": 29,
        "report_filter": 2,
        "selector": 12,
        "sort": 14,
    }
    assert sum(found.values()) == 96
    home = "ReportSection8454c812b60b8bb83b99"
    details = "ReportSection3f1c0d5e7a9b2c4d6e8f"
    drill = "ReportSectiond2a4c6e8f0b1a3c5e7d9"
    tooltip = "ReportSectionb7e9f1a3c5d7e9f1a3b5"
    assert kinds(index.page_uses(home)) == {"bookmark": 2}
    assert kinds(index.page_uses(details)) == {"bookmark": 2, "visual_link": 1}
    assert kinds(index.page_uses(drill)) == {"page_binding": 1}
    assert kinds(index.page_uses(tooltip)) == {"page_binding": 1, "tooltip": 1}
    bound = {
        "Filterf77304be5b57466a8b00": {"bound_filter": 1, "page_filter": 1},
        "Filter4959aceadddc26faec5a": {"bound_filter": 1, "page_filter": 1},
        "Filter4713a55be3dfa9b6f20b": {"report_filter": 1},
        "Filter16d155b792f832600712": {"page_filter": 1},
        "Filterff831044ab8c98051120": {"filter_field": 1},
        "Filter664c2d8498d449e53048": {"filter_field": 1},
    }
    for name, expected in bound.items():
        assert kinds(index.filter_uses(name)) == expected
    assert kinds(index.visual_uses("40c896eda14bfa8f6a97")) == {"group_member": 2}
    assert kinds(index.visual_uses("7799291338ca3548d7d5")) == {"group_member": 2}


def test_delete_guard_counts_a_measure_that_formats_a_page_or_the_report(
    multipage: PbirDocument,
) -> None:
    expr = {"expr": measure("Measures", "Total Sales")}
    fill = {"properties": {"color": {"solid": {"color": expr}}}}
    multipage.page(HOME).data.setdefault("objects", {})["background"] = [fill]
    multipage.mark_dirty(f"{ROOT}pages/{HOME}/page.json")
    index = refs.ReferenceIndex.build(multipage)
    assert [u.kind for u in index.measure_uses("Total Sales") if "page.json" in u.part] == [
        "object_expression"
    ]
    assert "used by the formatting of 1 page" in refs.delete_guard(index, "Total Sales")
    report = multipage.report
    report.setdefault("objects", {})["outspace"] = [fill]
    multipage.mark_dirty(REPORT)
    index = refs.ReferenceIndex.build(multipage)
    assert "used by the formatting of the report" in refs.delete_guard(index, "Total Sales")


def test_split_key_handles_apostrophes_and_brackets_in_names() -> None:
    assert refs._split_key("Bob's Table[M]") == ("Bob's Table", "M")
    assert refs._split_key("'Bob''s Table'[M]") == ("Bob's Table", "M")
    assert refs._split_key("Agent Table[M]") == ("Agent Table", "M")
    assert refs._split_key("Measures[Total [USD]]") == ("Measures", "Total [USD]")
    assert refs._split_key("Base") == ("Base", None)
    assert refs._split_key("'My Table'") == ("My Table", None)


def test_dependents_follow_a_table_with_an_apostrophe(multipage: PbirDocument) -> None:
    index = refs.ReferenceIndex.build(
        multipage,
        measures={
            "Measures[Base]": "1",
            "Bob's Measures[Dep]": "[Base]*2",
            "Measures[Top]": "[Dep]+1",
        },
    )
    assert index.measure_dependents("Base") == ["Dep", "Top"]
    assert index.measure_dependents("Dep") == ["Top"]


def test_clearing_a_page_drops_its_visuals_from_other_bookmark_target_lists(
    tmp_path: Path,
) -> None:
    parts = multipage_parts()
    parts[f"{ROOT}bookmarks/Bookmark2.bookmark.json"] = bookmark_json(
        "Bookmark2",
        "Detail view",
        {DETAILS: {"visualContainers": {"dvis": {}}}, HOME: {"visualContainers": {"tbl": {}}}},
        options={"targetVisualNames": ["dvis", "tbl"]},
    )
    document = open_document(make_pbix(tmp_path, parts))
    refs.clear_page_references(document, DETAILS)
    document.remove_page(DETAILS)
    assert document.bookmark("Bookmark2")["options"] == {"targetVisualNames": ["tbl"]}
    assert refs.ReferenceIndex.build(document).visual_uses("dvis") == []


def test_multipage_fixture_indexes_without_dangling_parts() -> None:
    if not MULTIPAGE.exists():
        pytest.skip("multipage fixture not present")
    document = open_document(MULTIPAGE)
    index = refs.ReferenceIndex.build(document)
    parts = set(document.package.names())
    for _, name, _ in document.all_visuals():
        for use in [*index.visual_uses(name)]:
            assert use.part in parts
            assert use.kind in refs.USE_KINDS
    for page_name in document.pages:
        for use in index.page_uses(page_name):
            assert use.part in parts
            assert use.kind in refs.USE_KINDS
    for page_name in list(document.pages):
        assert isinstance(refs.remove_page_references(document, page_name), list)


# --- bookmark references, navigator pages and group trees (multipage fixture file) -----------

FX_HOME = "ReportSection8454c812b60b8bb83b99"
FX_DETAILS = "ReportSection3f1c0d5e7a9b2c4d6e8f"
FX_CHANNEL_FOCUS = "Bookmarkc7a5beae9c2a6a44bb3c"  # covers page 1 only; a Details button opens it
FX_DETAILS_VIEW = "Bookmark213a31c763c98e893e32"  # covers Details only
FX_BUTTON = "d6c05c8319906941fe02"  # on Details, opens Channel focus
FX_OUTER, FX_INNER = "40c896eda14bfa8f6a97", "7799291338ca3548d7d5"


def fixture_document() -> PbirDocument:
    if not MULTIPAGE.exists():
        pytest.skip("multipage fixture not present")
    return open_document(MULTIPAGE)


def navigator(document: PbirDocument, page: str, name: str, kind: str, objects: Any) -> None:
    owner = document.pages[page]
    owner.visuals[name] = {
        "name": name,
        "position": {"x": 0, "y": 0, "z": 99, "width": 100, "height": 50, "tabOrder": 99},
        "visual": {"visualType": kind, "objects": objects},
    }


def test_bookmark_ids_are_indexed_with_their_pointers() -> None:
    document = fixture_document()
    navigator(
        document,
        FX_HOME,
        "bnav",
        "bookmarkNavigator",
        {
            "bookmarks": [
                {
                    "properties": {
                        "bookmarkGroup": literal("G1"),
                        "deselectionBookmark": literal(FX_CHANNEL_FOCUS),
                    }
                }
            ]
        },
    )
    index = refs.ReferenceIndex.build(document)
    uses = index.bookmark_uses(FX_CHANNEL_FOCUS)
    assert [(u.kind, u.where, u.label) for u in uses] == [
        (
            "bookmark_navigator",
            "/visual/objects/bookmarks/0/properties/deselectionBookmark",
            "bookmark navigator bnav opens this bookmark on deselection",
        ),
        (
            "bookmark_action",
            "/visual/visualContainerObjects/visualLink/0/properties/bookmark",
            "button d6c05c83 opens this bookmark",
        ),
    ]
    assert kinds(index.bookmark_uses("G1")) == {"bookmark_navigator": 1}
    assert index.bookmark_uses(FX_DETAILS_VIEW) == []
    assert {u.kind for u in uses} <= set(refs.USE_KINDS)
    assert [r.label for r in refs.bookmark_references(document, FX_CHANNEL_FOCUS)] == [
        "bookmark navigator bnav on page '1' opens it on deselection",
        "button d6c05c83 on page 'Details' opens it",
    ]


def test_deleting_a_page_names_and_switches_off_what_opens_its_bookmark() -> None:
    document = fixture_document()
    assert refs.remove_page_references(document, FX_HOME) == [
        "bookmark 'Channel focus' opens it",
        "button d6c05c83 on page 'Details' opens the bookmark 'Channel focus'",
    ]
    changes = refs.clear_page_references(document, FX_HOME)
    assert changes == [
        "Deleted the bookmark 'Channel focus' because it only covered the deleted page.",
        "Switched off the action of button d6c05c83 on page 'Details', which opened the "
        "bookmark 'Channel focus'.",
    ]
    link = document.visual(FX_BUTTON)[1]["visual"]["visualContainerObjects"]["visualLink"]
    assert link == [
        {
            "properties": {
                "show": {"expr": {"Literal": {"Value": "false"}}},
                "type": {"expr": {"Literal": {"Value": "'Bookmark'"}}},
            }
        }
    ]
    assert refs.bookmark_references(document, FX_CHANNEL_FOCUS) == []


def test_deleting_a_page_drops_it_from_page_navigators_and_releases_emptied_groups() -> None:
    document = fixture_document()
    navigator(
        document,
        FX_HOME,
        "pnav",
        "pageNavigator",
        {
            "pages": [
                {"properties": {"showHiddenPages": {"expr": {"Literal": {"Value": "true"}}}}},
                {
                    "properties": {"showPage": {"expr": {"Literal": {"Value": "false"}}}},
                    "selector": {"id": FX_DETAILS},
                },
            ]
        },
    )
    navigator(
        document,
        FX_HOME,
        "bnav",
        "bookmarkNavigator",
        {"bookmarks": [{"properties": {"bookmarkGroup": literal("Views")}}]},
    )
    document.bookmarks_metadata["items"] = [
        {"name": FX_CHANNEL_FOCUS},
        {"name": "Views", "displayName": "Detail views", "children": [FX_DETAILS_VIEW]},
    ]
    changes = refs.clear_page_references(document, FX_DETAILS)
    assert changes[:2] == [
        "Removed the action of button eff2d7b0 that used the deleted page.",
        "Removed the deleted page from the list of page navigator pnav.",
    ]
    assert changes[-3:] == [
        "Deleted the bookmark 'Details view' because it only covered the deleted page.",
        "Removed the bookmark group 'Detail views', which it left empty.",
        "1 bookmark navigator lists every bookmark again.",
    ]
    objects = document.visual("pnav")[1]["visual"]["objects"]
    assert objects["pages"] == [
        {"properties": {"showHiddenPages": {"expr": {"Literal": {"Value": "true"}}}}}
    ]
    group = document.visual("bnav")[1]["visual"]["objects"]["bookmarks"][0]["properties"]
    assert group["bookmarkGroup"] == {"expr": {"Literal": {"Value": "''"}}}
    assert document.bookmarks_metadata["items"] == [{"name": FX_CHANNEL_FOCUS}]


def nested_tree_bookmark(document: PbirDocument) -> dict[str, Any]:
    bookmark = document.bookmark(FX_DETAILS_VIEW)
    bookmark["explorationState"]["sections"][FX_DETAILS]["visualContainerGroups"] = {
        FX_OUTER: {"isHidden": False, "children": {FX_INNER: {"isHidden": True}}}
    }
    return bookmark


def test_deleting_a_nested_group_prunes_it_from_bookmark_group_trees() -> None:
    document = fixture_document()
    bookmark = nested_tree_bookmark(document)
    result = refs.remove_visual_references(document, FX_DETAILS, FX_INNER)
    section = bookmark["explorationState"]["sections"][FX_DETAILS]
    assert section["visualContainerGroups"] == {FX_OUTER: {"isHidden": False}}
    assert result.counts == {"bookmark": 1}
    assert refs.ReferenceIndex.build(document).visual_uses(FX_INNER) == []


def test_deleting_an_outer_group_alone_moves_its_inner_group_up_in_bookmarks() -> None:
    document = fixture_document()
    bookmark = nested_tree_bookmark(document)
    refs.remove_visual_references(document, FX_DETAILS, FX_OUTER, group_children=False)
    section = bookmark["explorationState"]["sections"][FX_DETAILS]
    assert section["visualContainerGroups"] == {FX_INNER: {"isHidden": True}}


def raw_literal(value: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": value}}}


def test_navigation_findings_of_a_clean_report_are_none() -> None:
    assert refs.navigation_findings(fixture_document()) == []


def test_navigation_findings_name_each_dangling_or_mistyped_reference() -> None:
    document = fixture_document()
    link = document.visual(FX_BUTTON)[1]["visual"]["visualContainerObjects"]["visualLink"][0]
    link["properties"] = {
        "show": raw_literal("true"),
        "type": literal("Drillthrough"),
        "drillthroughSection": literal(FX_HOME),  # a normal page
    }
    navigator(
        document,
        FX_HOME,
        "bnav",
        "bookmarkNavigator",
        {"bookmarks": [{"properties": {"bookmarkGroup": literal("GroupGone")}}]},
    )
    navigator(
        document,
        FX_HOME,
        "pnav",
        "pageNavigator",
        {"pages": [{"properties": {"showPage": raw_literal("false")}, "selector": {"id": "Gone"}}]},
    )
    document.bookmarks_metadata["items"].append({"name": FX_CHANNEL_FOCUS})
    found = [(f.code, f.pointer, f.message) for f in refs.navigation_findings(document)]
    assert found == [
        (
            "bookmark_listed_twice",
            "/items/2",
            "The bookmark 'Channel focus' is listed more than once",
        ),
        (
            "navigator_group_missing",
            "/visual/objects/bookmarks/0/properties/bookmarkGroup",
            "Bookmark navigator bnav on page '1' lists a bookmark group that does not exist",
        ),
        (
            "navigator_page_missing",
            "/visual/objects/pages/0/selector/id",
            "Page navigator pnav on page '1' has a setting for a page that does not exist",
        ),
        (
            "link_not_drillthrough",
            "/visual/visualContainerObjects/visualLink/0/properties/drillthroughSection",
            "Button d6c05c83 on page 'Details' drills through to a page that is not a "
            "drillthrough page",
        ),
    ]
    # A caller names visuals its own way.
    named = refs.navigation_findings(document, lambda page, name, visual: f"the visual {name}")
    assert named[1].message == (
        "The visual bnav on page '1' lists a bookmark group that does not exist"
    )
