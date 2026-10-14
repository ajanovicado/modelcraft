"""Structural validation of an edited report (report_edit_validation).

Every rule is shown on a fixture with one seeded defect: the twin and multipage fixtures are
clean, so a finding that appears after a mutation is that mutation's. The fixtures are
Desktop-authored (the twin) or built to Desktop's shapes (multipage), which is also why a clean
run of every rule on them is a test: a rule that fires on Desktop's own files is a false alarm.
"""

from __future__ import annotations

import copy
import json
import socket
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import report_edit_validation as validation
from ai_powerbi_analyst.studio import report_edits as edits
from ai_powerbi_analyst.studio.pbir_document import ROOT, PageDoc, PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_context import ModelInfo
from ai_powerbi_analyst.studio.report_edit_validation import Finding

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
PAGE1 = "ReportSection8454c812b60b8bb83b99"
DETAILS = "ReportSection3f1c0d5e7a9b2c4d6e8f"
DRILL = "ReportSectiond2a4c6e8f0b1a3c5e7d9"
TOOLTIP = "ReportSectionb7e9f1a3c5d7e9f1a3b5"
TABLE = "e82e02f93055d6b0b544"  # tableEx with a filter on a measure through a From alias
COLUMN = "b404a461ede7604a2774"  # columnChart with a sort, per-category fills, a tooltip link
CARD = "2202e311b1493069e4eb"
BUTTON = "eff2d7b04706cc92f1db"  # actionButton with a page-navigation link
OUTER_GROUP = "40c896eda14bfa8f6a97"
INNER_GROUP = "7799291338ca3548d7d5"
MEMBER = "04ba32659627f6e8c7d7"  # a slicer in the inner group
TOOLTIP_CARD = "ab18327bb6dba268ebf5"


def load(name: str = "multipage") -> PbirDocument:
    return PbirDocument(PbixPackage(FIXTURES / f"{name}.pbix"))


def visual_part(page: str, visual: str) -> str:
    return f"{ROOT}pages/{page}/visuals/{visual}/visual.json"


def page_part(page: str) -> str:
    return f"{ROOT}pages/{page}/page.json"


def _walk(node: Any, measures: set[str], columns: dict[str, set[str]]) -> None:
    if isinstance(node, dict):
        for kind in ("Measure", "Column"):
            body = node.get(kind)
            source = (
                body.get("Expression", {}).get("SourceRef", {}) if isinstance(body, dict) else {}
            )
            if isinstance(source.get("Entity"), str):
                if kind == "Measure":
                    measures.add(str(body["Property"]))
                else:
                    columns.setdefault(source["Entity"], set()).add(str(body["Property"]))
        for value in node.values():
            _walk(value, measures, columns)
    elif isinstance(node, list):
        for value in node:
            _walk(value, measures, columns)


def model_of(document: PbirDocument) -> ModelInfo:
    """A model holding exactly the fields the document's own parts use."""
    measures: set[str] = set()
    columns: dict[str, set[str]] = {}
    for _, _, visual in document.all_visuals():
        _walk(visual, measures, columns)
    _walk(document.report, measures, columns)
    for page in document.pages.values():
        _walk(page.data, measures, columns)
    return ModelInfo.from_report(
        [
            {"TableName": t, "ColumnName": c, "PandasDataType": "object"}
            for t, cols in columns.items()
            for c in sorted(cols)
        ],
        [{"Name": m, "TableName": "# Measures"} for m in sorted(measures)],
    )


def found(document: PbirDocument, **kw: Any) -> list[Finding]:
    return validation.validate_findings(document, **kw)


def blocking(document: PbirDocument, **kw: Any) -> list[Finding]:
    return [f for f in found(document, **kw) if f.severity == "blocking"]


def only(document: PbirDocument, **kw: Any) -> Finding:
    (finding,) = blocking(document, **kw)
    return finding


def visual(document: PbirDocument, name: str) -> dict[str, Any]:
    return document.visual(name)[1]


# --- the fixtures are clean ---------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["structure-twin", "multipage"])
def test_a_desktop_authored_report_has_no_blocking_finding(fixture: str) -> None:
    document = load(fixture)
    assert blocking(document) == []
    assert blocking(document, model=model_of(document)) == []
    assert edits.validate_document(document, model_of(document)) == []


def test_the_advisories_of_the_fixtures_are_the_known_ones() -> None:
    twin = Counter(f.code for f in found(load("structure-twin")))
    assert twin == {"visuals_overlap": 1}
    multipage = Counter(f.code for f in found(load()))
    # Its two buttons are drawn since the canvas draws buttons and navigators.
    assert multipage == {"visuals_overlap": 2}
    assert all(f.severity == "advisory" for f in found(load()))


def test_validating_the_twin_takes_well_under_a_second() -> None:
    document = load("structure-twin")
    validation._filter_validator.cache_clear()  # the first call builds the filter validator
    started = time.perf_counter()
    validation.validate_findings(document, model=model_of(document))
    assert time.perf_counter() - started < 1.0
    started = time.perf_counter()
    validation.validate_findings(document, model=model_of(document))
    assert time.perf_counter() - started < 0.5


def test_validation_never_uses_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("validation opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    validation._filter_validator.cache_clear()
    assert blocking(load()) == []


def test_the_drawn_types_are_the_ones_the_canvas_draws() -> None:
    from ai_powerbi_analyst.studio.live_report import KINDS, NAVIGATION_KINDS

    assert frozenset(KINDS) | frozenset(NAVIGATION_KINDS) == validation.DRAWN_TYPES


# --- the five rules validate_document has always had (messages unchanged) ---------------------


def test_shared_visual_names_are_reported_once() -> None:
    document = load()
    document.pages[DETAILS].visuals[TOOLTIP_CARD] = document.pages[TOOLTIP].visuals[TOOLTIP_CARD]
    problems = edits.validate_document(document)
    assert problems == ["Two visuals share a name"]
    finding = only(document)
    assert (finding.code, finding.pointer) == ("visual_name_duplicate", "/name")
    assert finding.part == visual_part(TOOLTIP, TOOLTIP_CARD)


def test_a_visual_named_differently_from_its_folder() -> None:
    document = load()
    visual(document, CARD)["name"] = "other"
    assert edits.validate_document(document) == [f"Visual {CARD} is named 'other'"]


def test_a_visual_left_of_or_above_the_page_is_outside_it() -> None:
    document = load()
    visual(document, CARD)["position"]["x"] = -5
    assert edits.validate_document(document) == [f"Visual {CARD} is outside the page"]


def test_formatting_for_a_field_the_visual_lacks() -> None:
    document = load()
    entry = visual(document, COLUMN)["visual"]["objects"]["dataPoint"][0]
    entry["selector"] = {"metadata": "Ghost.Field"}
    problems = edits.validate_document(document)
    assert problems == [f"{COLUMN}: dataPoint formats a field the visual lacks (Ghost.Field)"]
    finding = only(document)
    assert finding.code == "selector_field_missing"
    assert finding.pointer == "/visual/objects/dataPoint/0/selector/metadata"


def test_a_selector_may_name_a_model_field_or_an_aggregate_the_visual_does_not_show() -> None:
    document = load()
    entry = visual(document, COLUMN)["visual"]["objects"]["dataPoint"][0]
    entry["selector"] = {"metadata": "Sum(DimStore.StoreName)"}
    assert blocking(document) == []
    model = model_of(document)
    entry["selector"] = {"metadata": "dimstore.storename"}
    assert [f.code for f in blocking(document)] == ["selector_field_missing"]  # no model to ask
    assert blocking(document, model=model) == []


def test_pages_json_must_list_the_pages_there_are() -> None:
    document = load()
    document.pages_meta["pageOrder"].pop()
    assert edits.validate_document(document) == ["pages.json lists a different set of pages"]


# --- structure -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("holder", "part", "message"),
    [
        (lambda d: visual(d, CARD), visual_part(PAGE1, CARD), f"Visual {CARD} has no schema"),
        (lambda d: d.pages[PAGE1].data, page_part(PAGE1), f"Page {PAGE1} has no schema"),
        (lambda d: d.report, ROOT + "report.json", "The report has no schema"),
        (lambda d: d.pages_meta, ROOT + "pages/pages.json", "The page list has no schema"),
    ],
)
def test_every_part_needs_its_schema(holder: Any, part: str, message: str) -> None:
    document = load()
    holder(document).pop("$schema")
    finding = only(document)
    assert (finding.code, finding.part, finding.pointer, finding.message) == (
        "schema_missing",
        part,
        "/$schema",
        message,
    )


def test_required_keys_of_visuals_groups_and_pages() -> None:
    document = load()
    visual(document, CARD).pop("position")
    del visual(document, CARD)["visual"]["visualType"]
    visual(document, OUTER_GROUP)["visualGroup"].pop("displayName")
    document.pages[DRILL].data.pop("displayName")
    found_ = {(f.code, f.pointer, f.message) for f in blocking(document)}
    assert found_ == {
        ("required_missing", "/position", f"Visual {CARD} has no position"),
        ("required_missing", "/visual/visualType", f"Visual {CARD} has no visual type"),
        ("required_missing", "/visualGroup/displayName", f"The group {OUTER_GROUP} has no name"),
        ("required_missing", "/displayName", f"Page {DRILL} has no title"),
    }


def test_a_container_is_a_visual_or_a_group_never_both() -> None:
    document = load()
    visual(document, CARD)["visualGroup"] = {"displayName": "g", "groupMode": "ScaleMode"}
    finding = only(document)
    assert finding.code == "visual_kind_invalid"
    assert (
        finding.message == f"Visual {CARD} must be either a visual or a group, not both or neither"
    )


def test_a_visual_name_is_at_most_50_characters() -> None:
    document = load()
    long_name = "v" * 51
    page = document.pages[DRILL]
    page.visuals[long_name] = page.visuals.pop("bbdab4695655dd2489e5")
    page.visuals[long_name]["name"] = long_name
    finding = only(document)
    assert finding.code == "visual_name_invalid"


def test_page_names_match_their_folder_and_are_unique() -> None:
    document = load()
    document.pages[DRILL].data["name"] = "other"
    assert [f.code for f in blocking(document)] == ["page_name_mismatch"]
    document = load()
    document.pages[DRILL].data["name"] = DETAILS
    codes = [f.code for f in blocking(document)]
    assert codes == ["page_name_mismatch"]  # the folder decides first; a name is the folder's
    document = load()
    document.pages[DRILL].data.pop("name")
    assert [f.code for f in blocking(document)] == ["required_missing"]


def test_page_order_and_the_active_page_agree_with_the_pages() -> None:
    document = load()
    document.pages_meta["pageOrder"].append(PAGE1)
    document.pages_meta["pageOrder"].pop(0)
    document.pages_meta["pageOrder"].append(PAGE1)
    assert [f.code for f in blocking(document)] == ["page_order_duplicate"]
    document = load()
    document.pages_meta["activePageName"] = "ReportSectionGone"
    finding = only(document)
    assert (finding.code, finding.pointer) == ("active_page_missing", "/activePageName")
    assert finding.message == "The report opens on page 'ReportSectionGone', which does not exist"


def test_a_page_size_must_be_a_positive_number() -> None:
    document = load()
    document.pages[DRILL].data["width"] = 0
    document.pages[DETAILS].data["height"] = "tall"
    assert {(f.code, f.pointer) for f in blocking(document)} == {
        ("page_size_invalid", "/width"),
        ("page_size_invalid", "/height"),
    }


# --- references ---------------------------------------------------------------------------------


def test_interactions_must_name_visuals_of_their_page() -> None:
    document = load()
    interactions = document.pages[PAGE1].data["visualInteractions"]
    interactions[0]["target"] = "gone"
    finding = only(document)
    assert (finding.code, finding.pointer) == (
        "interaction_visual_missing",
        "/visualInteractions/0/target",
    )
    assert finding.part == page_part(PAGE1)


def test_a_page_binding_must_bind_a_filter_of_the_page() -> None:
    document = load()
    document.pages[DRILL].data["pageBinding"]["parameters"][0]["boundFilter"] = "FilterGone"
    finding = only(document)
    assert finding.code == "bound_filter_missing"
    assert finding.pointer == "/pageBinding/parameters/0/boundFilter"


def test_a_group_parent_must_be_a_group_on_the_page_and_never_a_cycle() -> None:
    document = load()
    visual(document, MEMBER)["parentGroupName"] = "gone"
    finding = only(document)
    assert (finding.code, finding.pointer) == ("group_parent_missing", "/parentGroupName")
    document = load()
    visual(document, MEMBER)["parentGroupName"] = COLUMN  # a visual, not a group
    assert [f.code for f in blocking(document)] == ["group_parent_missing"]
    document = load()
    visual(document, OUTER_GROUP)["parentGroupName"] = INNER_GROUP
    findings = blocking(document)
    assert {f.code for f in findings} == {"group_cycle"}
    # Every container whose chain of parents runs into the cycle is reported, the groups and
    # the visuals inside them.
    assert {Path(f.part).parent.name for f in findings} == {
        OUTER_GROUP,
        INNER_GROUP,
        MEMBER,
        "2b5b8e3a7ed15138a983",
        "dfb4645ec076d1209ebe",
    }


def test_page_links_must_name_pages_that_exist() -> None:
    document = load()
    props = visual(document, BUTTON)["visual"]["visualContainerObjects"]["visualLink"][0][
        "properties"
    ]
    props["navigationSection"]["expr"]["Literal"]["Value"] = "'ReportSectionGone'"
    finding = only(document)
    assert finding.code == "link_page_missing"
    assert finding.message == f"Visual {BUTTON} links to a page that does not exist"
    document = load()
    tip = visual(document, COLUMN)["visual"]["visualContainerObjects"]["visualTooltip"][0]
    tip["properties"]["section"]["expr"]["Literal"]["Value"] = "'ReportSectionGone'"
    assert [f.code for f in blocking(document)] == ["link_page_missing"]


def test_bookmarks_must_name_pages_and_visuals_that_exist() -> None:
    document = load()
    for bookmark in document.bookmarks().values():
        state = bookmark["explorationState"]
        state["activeSection"] = "ReportSectionGone"
        state["sections"]["ReportSectionOther"] = {}
        section = next(s for s in state["sections"].values() if s.get("visualContainers"))
        section["visualContainers"]["gone"] = {"singleVisual": {}}
        bookmark.setdefault("options", {})["targetVisualNames"] = ["gone2"]
    codes = Counter(f.code for f in blocking(document))
    assert codes == {"bookmark_page_missing": 4, "bookmark_visual_missing": 4}
    finding = next(f for f in blocking(document) if f.code == "bookmark_page_missing")
    assert finding.part.startswith(ROOT + "bookmarks/")
    assert finding.pointer in {
        "/explorationState/activeSection",
        "/explorationState/sections/ReportSectionOther",
    }


BOOKMARK_BUTTON = "d6c05c8319906941fe02"  # opens the bookmark 'Channel focus'


def test_dangling_navigation_references_block_like_other_broken_references() -> None:
    document = load()
    link = visual(document, BOOKMARK_BUTTON)["visual"]["visualContainerObjects"]["visualLink"]
    link[0]["properties"]["bookmark"]["expr"]["Literal"]["Value"] = "'BookmarkGone'"
    finding = only(document)
    assert (finding.code, finding.part, finding.pointer) == (
        "link_bookmark_missing",
        visual_part(DETAILS, BOOKMARK_BUTTON),
        "/visual/visualContainerObjects/visualLink/0/properties/bookmark",
    )
    assert finding.message.endswith("on page 'Details' opens a bookmark that does not exist")
    assert edits.validate_document(document) == [finding.message]
    # A source report that already has it is not blamed on the next edit.
    assert blocking(document, baseline=validation.capture_baseline(document)) == []


def test_the_bookmark_list_and_nested_groups_are_checked() -> None:
    document = load()
    items = document.bookmarks_metadata["items"]
    dropped = items.pop()
    key = dropped.get("name") if "children" not in dropped else dropped["children"][0]
    findings = blocking(document)
    assert [(f.code, f.part, f.pointer) for f in findings] == [
        ("bookmark_unlisted", f"{ROOT}bookmarks/{key}.bookmark.json", "")
    ]
    document = load()
    section = next(
        b["explorationState"]["sections"][DETAILS]
        for b in document.bookmarks().values()
        if DETAILS in b["explorationState"]["sections"]
    )
    tree = section.setdefault("visualContainerGroups", {})
    tree[OUTER_GROUP] = {"isHidden": False, "children": {"gone": {"isHidden": True}}}
    finding = only(document)
    assert finding.code == "bookmark_group_missing"
    assert finding.pointer.endswith(f"/visualContainerGroups/{OUTER_GROUP}/children/gone")


def test_filter_names_are_unique_across_the_report() -> None:
    document = load()
    name = document.report["filterConfig"]["filters"][0]["name"]
    document.pages[DETAILS].data["filterConfig"] = {
        "filters": [{"name": name, "type": "Categorical"}]
    }
    finding = only(document)
    assert (finding.code, finding.part, finding.pointer) == (
        "filter_name_duplicate",
        page_part(DETAILS),
        "/filterConfig/filters/0/name",
    )
    assert name in finding.message


# --- fields against the model --------------------------------------------------------------------


def ghost(node: dict[str, Any], key: str) -> None:
    node[key]["Property"] = "Ghost"


def test_bindings_sorts_filters_and_expressions_must_resolve_in_the_model() -> None:
    document = load()
    model = model_of(document)
    table = visual(document, TABLE)["visual"]
    ghost(table["query"]["queryState"]["Values"]["projections"][0]["field"], "Column")
    ghost(table["query"]["sortDefinition"]["sort"][0]["field"], "Measure")
    filter_ = visual(document, TABLE)["filterConfig"]["filters"][0]
    ghost(filter_["field"], "Measure")
    ghost(filter_["filter"]["Where"][0]["Condition"]["Comparison"]["Left"], "Measure")
    _measure_nodes(table["objects"])[0]["Measure"]["Property"] = "Ghost"
    found_ = blocking(document, model=model)
    assert Counter(f.code for f in found_) == {
        "binding_field_missing": 1,
        "sort_field_missing": 1,
        "filter_field_missing": 2,
        "expression_field_missing": 1,
    }
    messages = {f.code: f.message for f in found_}
    assert messages["binding_field_missing"] == (
        f"Visual {TABLE} uses a field, DimEmployee[Ghost], that the model does not have"
    )
    assert messages["sort_field_missing"] == (
        f"Visual {TABLE} uses its sort, # Measures[Ghost], that the model does not have"
    )
    pointers = {f.pointer for f in found_ if f.code == "filter_field_missing"}
    assert pointers == {
        "/filterConfig/filters/0/field/Measure",
        "/filterConfig/filters/0/filter/Where/0/Condition/Comparison/Left/Measure",
    }
    # Without a model, or with an empty one, nothing can be missing.
    assert blocking(document) == []
    assert blocking(document, model=ModelInfo()) == []


def _measure_nodes(node: Any) -> list[dict[str, Any]]:
    found_: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "Measure" in node:
            found_.append(node)
        for value in node.values():
            found_.extend(_measure_nodes(value))
    elif isinstance(node, list):
        for value in node:
            found_.extend(_measure_nodes(value))
    return found_


def test_the_automatic_date_tables_desktop_hides_are_not_looked_up() -> None:
    document = load()
    model = model_of(document)
    projection = visual(document, TABLE)["visual"]["query"]["queryState"]["Values"]["projections"]
    column = next(p["field"]["Column"] for p in projection if "Column" in p["field"])
    column["Expression"]["SourceRef"]["Entity"] = "LocalDateTable_0a1b2c3d"
    column["Property"] = "Date"
    assert blocking(document, model=model) == []
    column["Expression"]["SourceRef"]["Entity"] = "DateTable_0a1b2c3d"
    assert [f.code for f in blocking(document, model=model)] == ["binding_field_missing"]


def test_a_measure_lives_in_its_own_table_and_a_column_in_a_known_one() -> None:
    document = load()
    model = model_of(document)
    projection = visual(document, TABLE)["visual"]["query"]["queryState"]["Values"]["projections"]
    measure = next(p["field"]["Measure"] for p in projection if "Measure" in p["field"])
    measure["Expression"]["SourceRef"]["Entity"] = "DimStore"  # another table's name
    finding = only(document, model=model)
    assert finding.code == "binding_field_missing"
    measure["Expression"]["SourceRef"]["Entity"] = "# measures"  # any letter case is one table
    assert blocking(document, model=model) == []


def test_a_report_level_measure_of_a_thin_report_is_known() -> None:
    document = load()
    model = model_of(document)
    projection = visual(document, CARD)["visual"]["query"]["queryState"]["Data"]["projections"][0]
    projection["field"]["Measure"]["Property"] = "Extension"
    projection["field"]["Measure"]["Expression"]["SourceRef"]["Entity"] = "Report"
    assert blocking(document, model=model) != []
    document.package.write_json(
        ROOT + "reportExtensions.json",
        {"entities": [{"name": "Report", "measures": [{"name": "Extension", "expression": "1"}]}]},
    )
    thin = PbirDocument(document.package)
    visual(thin, CARD)["visual"]["query"]["queryState"]["Data"]["projections"][0] = projection
    assert blocking(thin, model=model) == []


def test_a_filter_reaches_its_table_through_an_alias_it_declares() -> None:
    document = load("structure-twin")
    filter_ = visual(document, TABLE)["filterConfig"]["filters"][0]["filter"]
    left = filter_["Where"][0]["Condition"]["Comparison"]["Left"]["Measure"]
    left["Expression"] = {"SourceRef": {"Source": "zz"}}
    finding = only(document)
    assert finding.code == "filter_source_unknown"
    assert (
        finding.message
        == f"A filter in visual {TABLE} uses a table alias its query does not declare"
    )
    left["Expression"] = {"SourceRef": {"Entity": "# Measures"}}
    finding = only(document)
    assert finding.code == "filter_entity_in_where"
    assert finding.pointer.endswith("/Where/0/Condition/Comparison/Left/Measure")


# --- Microsoft's filter schema -----------------------------------------------------------------


def test_a_filter_that_breaks_the_published_filter_schema_is_blocking() -> None:
    document = load()
    visual(document, TABLE)["filterConfig"]["filters"][0]["type"] = "Nonsense"
    finding = only(document)
    assert finding.code == "filter_schema"
    assert finding.pointer == "/filterConfig/filters/0/type"
    assert finding.message.startswith(
        f"A filter in visual {TABLE} is not in a form Power BI accepts"
    )


def test_the_filter_schema_check_is_skipped_when_the_library_or_files_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = load()
    visual(document, TABLE)["filterConfig"]["filters"][0]["type"] = "Nonsense"
    monkeypatch.setattr(validation, "_filter_validator", lambda: None)
    assert blocking(document) == []


def test_the_schemas_ship_with_the_package() -> None:
    names = {p.name for p in validation._SCHEMA_DIR.iterdir()}
    assert names == {"filterConfiguration-1.3.0.embedded.json", "semanticQuery-1.4.0.json"}


def test_every_filter_studio_writes_passes_the_schema() -> None:
    document = load()
    model = ModelInfo.from_report(
        [
            {"TableName": "Sales", "ColumnName": "Country", "PandasDataType": "object"},
            {"TableName": "Sales", "ColumnName": "OrderDate", "PandasDataType": "datetime64[ns]"},
            {"TableName": "Sales", "ColumnName": "Year", "PandasDataType": "int64"},
        ],
        [{"Name": "Total", "TableName": "# Measures"}],
    )
    country = {"table": "Sales", "name": "Country"}
    steps: list[dict[str, Any]] = [
        {
            "op": "set_visual_filter",
            "visual": COLUMN,
            "filter": {
                "kind": "categorical",
                "field": country,
                "values": ["A", None],
                "exclude": True,
            },
        },
        {
            "op": "set_visual_filter",
            "visual": CARD,
            "locked": True,
            "hidden": True,
            "filter": {
                "kind": "advanced",
                "field": {"name": "Year"},
                "conditions": [
                    {"operator": "greater_than", "value": 2010},
                    {"operator": "is_not", "value": 2015},
                ],
                "logic": "or",
            },
        },
        {
            "op": "set_page_filter",
            "page": DETAILS,
            "filter": {
                "kind": "top_n",
                "field": country,
                "n": 5,
                "by": {"name": "Total"},
                "direction": "bottom",
            },
        },
        {
            "op": "set_report_filter",
            "filter": {
                "kind": "relative_date",
                "field": {"table": "Sales", "name": "OrderDate"},
                "unit": "calendar_months",
                "direction": "last",
                "count": 3,
                "include_today": True,
            },
        },
        {
            "op": "set_page_filter",
            "page": TOOLTIP,
            "filter": {
                "kind": "advanced",
                "field": country,
                "conditions": [{"operator": "contains", "value": "an"}],
            },
        },
    ]
    edits.apply_operations(document, edits.parse_operations(steps), model)
    assert blocking(document) == []


# --- literals Studio wrote ---------------------------------------------------------------------


def set_label_show(document: PbirDocument, node: dict[str, Any]) -> None:
    objects = visual(document, COLUMN)["visual"]["objects"]
    objects.setdefault("labels", [{"properties": {}}])[0]["properties"]["show"] = node


BAD_BOOL = {"expr": {"Literal": {"Value": "'yes'"}}}


def test_a_literal_of_the_wrong_type_in_a_changed_part_is_blocking() -> None:
    document = load()
    set_label_show(document, BAD_BOOL)
    assert blocking(document) == []  # not this batch's: the part was not touched
    document.touch_visual(document.pages[PAGE1], COLUMN)
    finding = only(document)
    assert finding.code == "literal_type_mismatch"
    assert finding.pointer == "/visual/objects/labels/show"
    assert finding.message == f"Visual {COLUMN}: show is not true or false"


@pytest.mark.parametrize(
    ("prop", "node", "problem"),
    [
        ("fontSize", {"expr": {"Literal": {"Value": "'big'"}}}, "fontSize is not a number"),
        (
            "color",
            {"solid": {"color": {"expr": {"Literal": {"Value": "12L"}}}}},
            "color is not a color",
        ),
        (
            "color",
            {"solid": {"color": {"expr": {"ThemeDataColor": {"ColorId": 12, "Percent": 0}}}}},
            "color uses a theme color that does not exist",
        ),
    ],
)
def test_the_catalog_type_of_a_property_decides_what_its_literal_may_be(
    prop: str, node: dict[str, Any], problem: str
) -> None:
    document = load()
    objects = visual(document, COLUMN)["visual"]["objects"]
    objects.setdefault("labels", [{"properties": {}}])[0]["properties"][prop] = node
    document.touch_visual(document.pages[PAGE1], COLUMN)
    finding = only(document)
    assert (finding.code, finding.message) == (
        "literal_type_mismatch",
        f"Visual {COLUMN}: {problem}",
    )


def test_d_and_l_numbers_are_interchangeable_and_desktops_own_spelling_is_never_blamed() -> None:
    document = load()
    set_label_show(document, {"expr": {"Literal": {"Value": "true"}}})
    objects = visual(document, COLUMN)["visual"]["objects"]
    objects["labels"][0]["properties"]["fontSize"] = {"expr": {"Literal": {"Value": "12L"}}}
    objects["labels"][0]["properties"]["labelPrecision"] = {"expr": {"Literal": {"Value": "2D"}}}
    document.touch_visual(document.pages[PAGE1], COLUMN)
    assert blocking(document) == []
    # An odd spelling that was already in the file when it was loaded is not this edit's.
    document.commit()
    reloaded = PbirDocument(document.package)
    reloaded.pages[PAGE1].visuals[COLUMN]["visual"]["objects"]["labels"][0]["properties"][
        "show"
    ] = BAD_BOOL
    document.package.write_json(visual_part(PAGE1, COLUMN), reloaded.pages[PAGE1].visuals[COLUMN])
    fresh = PbirDocument(document.package)
    fresh.touch_visual(fresh.pages[PAGE1], COLUMN)
    assert blocking(fresh) == []
    visual(fresh, COLUMN)["visual"]["objects"]["labels"][0]["properties"]["fontSize"] = {
        "expr": {"Literal": {"Value": "'huge'"}}
    }
    assert [f.code for f in blocking(fresh)] == ["literal_type_mismatch"]


def test_an_unknown_object_or_property_in_a_changed_entry_is_blocking() -> None:
    document = load()
    objects = visual(document, COLUMN)["visual"]["objects"]
    objects.setdefault("labels", [{"properties": {}}])[0]["properties"]["sparkle"] = {
        "expr": {"Literal": {"Value": "true"}}
    }
    objects["notAnObject"] = [{"properties": {"x": {"expr": {"Literal": {"Value": "1D"}}}}}]
    document.touch_visual(document.pages[PAGE1], COLUMN)
    assert {(f.code, f.pointer) for f in blocking(document)} == {
        ("property_unknown", "/visual/objects/labels/sparkle"),
        ("property_unknown", "/visual/objects/notAnObject/x"),
    }


def test_a_batch_of_ordinary_operations_leaves_a_valid_report() -> None:
    document = load()
    model = model_of(document)
    steps = [
        {"op": "set_alt_text", "visual": COLUMN, "text": "Sales"},
        {
            "op": "set_sort",
            "visual": COLUMN,
            "field": {"table": "# Measures", "name": "TopN/BottomN/ MTD Sales"},
        },
        {"op": "set_visual_hidden", "visual": CARD, "hidden": False},
        {"op": "set_position", "visual": CARD, "x": 30},
    ]
    edits.apply_operations(document, edits.parse_operations(steps), model)
    assert blocking(document, model=model) == []


# --- positions ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "pointer"),
    [
        (lambda p: p.pop("width"), "/position/width"),
        (lambda p: p.update(x=float("nan")), "/position/x"),
        (lambda p: p.update(y=float("inf")), "/position/y"),
        (lambda p: p.update(height="tall"), "/position/height"),
        (lambda p: p.update(width=True), "/position/width"),
    ],
)
def test_a_position_is_four_finite_numbers(change: Any, pointer: str) -> None:
    document = load()
    change(visual(document, CARD)["position"])
    finding = only(document)
    assert (finding.code, finding.pointer) == ("position_invalid", pointer)


def test_a_visual_needs_a_size() -> None:
    document = load()
    visual(document, CARD)["position"]["height"] = 0
    finding = only(document)
    assert finding.code == "position_invalid"
    assert finding.message == f"Visual {CARD} has no size"


def test_the_right_and_bottom_edges_are_checked_with_a_pixel_of_slack() -> None:
    document = load()
    position = visual(document, CARD)["position"]
    page_width = document.pages[PAGE1].data["width"]
    position["x"] = page_width - position["width"] + 1  # one pixel over: Desktop's rounding
    document.touch_visual(document.pages[PAGE1], CARD)
    assert blocking(document) == []
    position["x"] += 0.5
    finding = only(document)
    assert (finding.code, finding.message) == (
        "position_outside_page",
        f"Visual {CARD} runs past the right edge of the page",
    )
    position["x"] = 10
    position["y"] = document.pages[PAGE1].data["height"] - position["height"] + 2
    finding = only(document)
    assert finding.message == f"Visual {CARD} runs past the bottom edge of the page"


def test_a_visual_over_the_edge_is_only_blocking_when_this_batch_changed_it() -> None:
    document = load()
    visual(document, CARD)["position"]["x"] = 5000  # the source's own defect, loaded as it was
    assert blocking(document) == []
    document.touch_visual(document.pages[PAGE1], CARD)
    assert [f.code for f in blocking(document)] == ["position_outside_page"]
    # A visual that sits in a group the batch changed is changed with it.
    document = load()
    visual(document, OUTER_GROUP)["position"]["x"] = 1100
    assert blocking(document) == []
    document.touch_visual(document.pages[DETAILS], OUTER_GROUP)
    assert {Path(f.part).parent.name for f in blocking(document)} >= {OUTER_GROUP, MEMBER}
    # A page that shrinks does not make its untouched visuals the edit's problem.
    document = load()
    document.pages[TOOLTIP].data["width"] = 200
    document.touch_page(document.pages[TOOLTIP])
    assert blocking(document) == []


def test_group_members_are_measured_in_page_coordinates() -> None:
    document = load()
    page = document.pages[DETAILS]
    # The outer group moves to x=1100 of a 1280-wide page: its members' own x stays small.
    visual(document, OUTER_GROUP)["position"]["x"] = 1100
    document.touch_visual(page, OUTER_GROUP)
    findings = blocking(document)
    flagged = {Path(f.part).parent.name for f in findings}
    assert flagged == {
        OUTER_GROUP,
        INNER_GROUP,
        MEMBER,
        "2b5b8e3a7ed15138a983",
        "dfb4645ec076d1209ebe",
    }
    assert {f.code for f in findings} == {"position_outside_page"}
    assert visual(document, MEMBER)["position"]["x"] == 40  # small in its own coordinates
    # A member left of its group's origin is fine while it is still on the page.
    document = load()
    visual(document, MEMBER)["position"]["x"] = -30  # page x = 40 + 40 - 30 = 50
    assert blocking(document) == []
    visual(document, MEMBER)["position"]["x"] = -90  # page x = -10
    assert only(document).message == f"Visual {MEMBER} is outside the page"
    assert page.visuals[MEMBER]["parentGroupName"] == INNER_GROUP


def test_a_page_size_is_what_positions_are_checked_against() -> None:
    document = load()
    document.pages[TOOLTIP].data["width"] = 200  # the card there is 300 wide
    document.touch_visual(document.pages[TOOLTIP], TOOLTIP_CARD)
    finding = only(document)
    assert finding.part == visual_part(TOOLTIP, TOOLTIP_CARD)
    assert finding.message == f"Visual {TOOLTIP_CARD} runs past the right edge of the page"


# --- advisory findings ---------------------------------------------------------------------------


def advisories(document: PbirDocument) -> list[Finding]:
    return [f for f in found(document) if f.severity == "advisory"]


def test_overlapping_visuals_and_tiny_ones_are_advisory() -> None:
    document = load()
    baseline = {f.key for f in advisories(document)}
    card = visual(document, CARD)["position"]
    other = visual(document, "9479353528b0eb22b958")["position"]
    card.update(x=other["x"], y=other["y"])
    visual(document, "71a164765d9c63200302")["position"].update(width=5)
    new = [f for f in advisories(document) if f.key not in baseline]
    assert {(f.code, Path(f.part).parent.name) for f in new} == {
        ("visuals_overlap", "9479353528b0eb22b958"),  # the later of the two is reported
        ("size_below_minimum", "71a164765d9c63200302"),
    }
    assert edits.validate_document(document) == []  # advisory never blocks


def test_tab_order_gaps_and_duplicates_are_advisory() -> None:
    document = load()
    visual(document, CARD)["position"].pop("tabOrder")
    visual(document, COLUMN)["position"]["tabOrder"] = visual(document, "da32cdbb0090e885649b")[
        "position"
    ]["tabOrder"]
    codes = {(f.code, Path(f.part).parent.name) for f in advisories(document)}
    assert ("tab_order_gap", CARD) in codes
    assert ("tab_order_duplicate", "da32cdbb0090e885649b") in codes  # the later one is reported


def test_visual_types_the_canvas_cannot_draw_are_advisory() -> None:
    document = load()
    visual(document, CARD)["visual"]["visualType"] = "gauge"
    pairs = {
        (f.code, f.pointer, Path(f.part).parent.name)
        for f in advisories(document)
        if f.code == "powerbi_only_visual"
    }
    # Buttons (BUTTON, d6c05c83...) are drawn now; a gauge is not.
    assert pairs == {("powerbi_only_visual", "/visual/visualType", CARD)}


def test_a_file_format_newer_than_the_pinned_one_is_advisory_and_older_is_not() -> None:
    document = load()
    page = document.pages[DRILL].data
    page["$schema"] = page["$schema"].replace("/2.1.0/", "/2.9.0/")
    visual(document, CARD)["$schema"] = visual(document, CARD)["$schema"].replace(
        "/2.12.0/", "/2.7.0/"
    )
    document.report["$schema"] = document.report["$schema"].replace("/3.3.0/", "/4.0.0/")
    found_ = [f for f in advisories(document) if f.code == "schema_version_unknown"]
    assert {f.part for f in found_} == {page_part(DRILL), ROOT + "report.json"}
    assert found_[0].message.startswith("This report uses a newer file format (")
    assert blocking(document) == []


# --- baselines ---------------------------------------------------------------------------------


def defect_off_the_page(document: PbirDocument, name: str = CARD) -> None:
    visual(document, name)["position"]["x"] = -5


def test_a_baseline_is_keyed_by_code_part_and_pointer_not_by_message() -> None:
    document = load()
    defect_off_the_page(document)
    keys = validation.capture_baseline(document)
    assert keys >= {
        ("position_outside_page", f"pages/{PAGE1}/visuals/{CARD}/visual.json", "/position")
    }
    assert isinstance(keys, set)
    finding = only(document)
    assert finding.key == (
        "position_outside_page",
        f"pages/{PAGE1}/visuals/{CARD}/visual.json",
        "/position",
    )
    reworded = Finding(finding.code, finding.severity, finding.part, finding.pointer, "other words")
    assert reworded.key == finding.key


def test_a_problem_the_source_already_had_does_not_block_an_edit() -> None:
    document = load()
    defect_off_the_page(document)
    baseline = validation.capture_baseline(document)
    assert edits.validate_document(document) != []  # without a baseline it still counts
    assert edits.validate_document(document, baseline=baseline) == []
    # A resize is still the same problem at the same place, whatever the numbers say.
    visual(document, CARD)["position"]["x"] = -50
    assert edits.validate_document(document, baseline=baseline) == []
    # A new problem is found beside it.
    defect_off_the_page(document, COLUMN)
    assert edits.validate_document(document, baseline=baseline) == [
        f"Visual {COLUMN} is outside the page"
    ]
    assert [f.code for f in found(document, baseline=baseline) if f.severity == "blocking"] == [
        "position_outside_page"
    ]


def test_a_baseline_also_covers_the_advisories() -> None:
    document = load()
    baseline = validation.capture_baseline(document)
    assert found(document, baseline=baseline) == []


def test_the_baseline_and_the_check_use_the_same_model() -> None:
    document = load()
    model = model_of(document)
    baseline = validation.capture_baseline(document, model)
    visual(document, CARD)["visual"]["query"]["queryState"]["Data"]["projections"][0]["field"][
        "Measure"
    ]["Property"] = "Ghost"
    assert [f.code for f in blocking(document, model=model, baseline=baseline)] == [
        "binding_field_missing"
    ]


def test_a_copied_visual_inherits_its_sources_baseline_through_lineage() -> None:
    document = load()
    defect_off_the_page(document, COLUMN)
    baseline = validation.capture_baseline(document)
    page = document.pages[PAGE1]
    copy_name = "c0c0c0c0c0c0c0c0c0c0"
    page.visuals[copy_name] = copy.deepcopy(page.visuals[COLUMN])
    page.visuals[copy_name]["name"] = copy_name
    assert [f.code for f in blocking(document, baseline=baseline)] == ["position_outside_page"]
    assert blocking(document, baseline=baseline, lineage={copy_name: COLUMN}) == []
    # ...and only that problem is inherited.
    page.visuals[copy_name]["parentGroupName"] = "gone"
    lineage = {copy_name: COLUMN}
    assert [f.code for f in blocking(document, baseline=baseline, lineage=lineage)] == [
        "group_parent_missing"
    ]


def test_a_copied_page_inherits_the_baseline_of_the_page_and_of_its_visuals() -> None:
    document = load()
    visual(document, TOOLTIP_CARD)["position"]["x"] = -5  # the source page has a defect
    baseline = validation.capture_baseline(document)
    source = document.pages[TOOLTIP]
    new_page, new_visual = "ReportSectionCopyOfTooltip", "c1c1c1c1c1c1c1c1c1c1"
    data = copy.deepcopy(source.data)
    data["name"] = new_page
    data["pageBinding"]["name"] = "aaaaaaaaaaaaaaaaaaaa"
    data.pop("filterConfig")
    data["pageBinding"]["parameters"] = []
    moved = copy.deepcopy(source.visuals[TOOLTIP_CARD])
    moved["name"] = new_visual
    document.pages[new_page] = PageDoc(new_page, data, {new_visual: moved})
    document.pages_meta["pageOrder"].append(new_page)
    unexplained = blocking(document, baseline=baseline)
    assert {f.code for f in unexplained} == {"position_outside_page"}
    assert (
        blocking(
            document,
            baseline=baseline,
            lineage={new_page: TOOLTIP, new_visual: TOOLTIP_CARD},
        )
        == []
    )


def test_validate_findings_returns_typed_records() -> None:
    document = load()
    defect_off_the_page(document)
    (finding,) = [f for f in found(document) if f.severity == "blocking"]
    assert finding == Finding(
        "position_outside_page",
        "blocking",
        visual_part(PAGE1, CARD),
        "/position",
        f"Visual {CARD} is outside the page",
    )
    assert edits.Finding is Finding
    assert edits.validate_findings is validation.validate_findings
    assert edits.capture_baseline is validation.capture_baseline
    assert json.dumps(finding.key)  # a key is plain data


# --- review fixes ---------------------------------------------------------------------------------


def commit_source_defect(document: PbirDocument, name: str = COLUMN, **position: float) -> None:
    """Make the defect part of the file as loaded, the way a report from Desktop has it."""
    visual(document, name)["position"].update(position)
    document.touch_visual(document.pages[PAGE1], name)
    document.commit()


def over_the_right_edge(document: PbirDocument, by: float) -> float:
    position = visual(document, COLUMN)["position"]
    return float(document.pages[PAGE1].data["width"]) - position["width"] + by


def test_a_source_visual_over_the_right_edge_does_not_block_an_edit_of_it() -> None:
    document = load()
    model = model_of(document)
    commit_source_defect(document, x=over_the_right_edge(document, 12))
    baseline = validation.capture_baseline(document, model)
    ops = edits.parse_operations([{"op": "set_alt_text", "visual": COLUMN, "text": "Sales"}])
    edits.apply_operations(document, ops, model)
    assert blocking(document, model=model, baseline=baseline) == []
    # Nor does it block without a baseline: the edit did not make the overflow worse.
    assert blocking(document, model=model) == []
    assert edits.validate_document(document, model) == []


def test_a_source_visual_over_the_bottom_edge_can_still_have_alt_text() -> None:
    document = load()
    model = model_of(document)
    height = float(document.pages[PAGE1].data["height"])
    commit_source_defect(document, y=height - visual(document, COLUMN)["position"]["height"] + 9)
    ops = edits.parse_operations([{"op": "set_alt_text", "visual": COLUMN, "text": "Sales"}])
    edits.apply_operations(document, ops, model)
    assert blocking(document, model=model) == []


def test_moving_a_source_visual_further_over_the_edge_is_this_edits_problem() -> None:
    document = load()
    commit_source_defect(document, x=over_the_right_edge(document, 12))
    visual(document, COLUMN)["position"]["x"] += 40
    document.touch_visual(document.pages[PAGE1], COLUMN)
    finding = only(document)
    assert finding.message == f"Visual {COLUMN} runs past the right edge of the page"
    # Moving it back to where it was (within the pixel of slack) is fine again.
    visual(document, COLUMN)["position"]["x"] -= 40
    assert blocking(document) == []
    visual(document, COLUMN)["position"]["x"] -= 30  # and well inside the page
    assert blocking(document) == []


def test_duplicating_a_source_visual_over_the_edge_is_not_refused_for_its_overhang() -> None:
    document = load()
    model = model_of(document)
    commit_source_defect(document, x=over_the_right_edge(document, 12))
    baseline = validation.capture_baseline(document, model)
    ops = edits.parse_operations([{"op": "duplicate_visual", "visual": COLUMN}])
    outcome = edits.apply_operations(document, ops, model)
    (copy_name,) = outcome.lineage
    assert outcome.lineage[copy_name] == COLUMN
    assert blocking(document, model=model, baseline=baseline, lineage=outcome.lineage) == []
    # The copy's own position is measured against its source's, with or without the baseline.
    assert blocking(document, model=model, lineage=outcome.lineage) == []
    visual(document, copy_name)["position"]["x"] = over_the_right_edge(document, 200)
    assert [f.code for f in blocking(document, model=model, lineage=outcome.lineage)] == [
        "position_outside_page"
    ]


def test_the_left_and_top_edges_have_the_same_slack_and_the_same_source_rule() -> None:
    document = load()
    visual(document, CARD)["position"]["x"] = -1e-14  # float noise from a snapped position
    visual(document, CARD)["position"]["y"] = -0.5
    assert blocking(document) == []
    visual(document, CARD)["position"]["x"] = -5
    assert edits.validate_document(document) == [f"Visual {CARD} is outside the page"]
    # A source report with a visual parked off the left edge is not this edit's to answer for.
    document = load()
    commit_source_defect(document, name=CARD, x=-40)
    assert blocking(document) == []
    visual(document, COLUMN)["position"]["width"] += 1
    document.touch_visual(document.pages[PAGE1], COLUMN)
    assert blocking(document) == []
    visual(document, CARD)["position"]["x"] = -90  # further out
    assert only(document).message == f"Visual {CARD} is outside the page"


def test_a_page_copy_made_by_apply_operations_carries_its_sources_lineage() -> None:
    document = load()
    model = model_of(document)
    commit_source_defect(document, name=TABLE, x=-5)
    baseline = validation.capture_baseline(document, model)
    ops = edits.parse_operations([{"op": "duplicate_page", "page": PAGE1}])
    outcome = edits.apply_operations(document, ops, model)
    new_page = next(name for name in outcome.lineage if name in document.pages)
    assert outcome.lineage[new_page] == PAGE1
    copies = {new: old for new, old in outcome.lineage.items() if new != new_page}
    assert set(copies.values()) == set(document.pages[PAGE1].visuals)
    assert TABLE in copies.values()
    unexplained = blocking(document, model=model, baseline=baseline)
    assert {f.code for f in unexplained} == {"position_outside_page"}
    assert blocking(document, model=model, baseline=baseline, lineage=outcome.lineage) == []


def test_a_copy_of_a_copy_inherits_the_baseline_of_the_first_source() -> None:
    document = load()
    defect_off_the_page(document, COLUMN)
    baseline = validation.capture_baseline(document)
    page = document.pages[PAGE1]
    first, second = "c2c2c2c2c2c2c2c2c2c2", "c3c3c3c3c3c3c3c3c3c3"
    for name in (first, second):
        page.visuals[name] = copy.deepcopy(page.visuals[COLUMN])
        page.visuals[name]["name"] = name
    assert blocking(document, baseline=baseline, lineage={second: first, first: COLUMN}) == []
    assert blocking(document, baseline=baseline, lineage={first: COLUMN, second: first}) == []


def two_filters_document() -> tuple[PbirDocument, ModelInfo]:
    from test_report_edit_filters import sales_model, visual_filter

    document = load()
    steps = [
        visual_filter(
            COLUMN,
            {"kind": "categorical", "field": {"table": "Sales", "name": name}, "values": [value]},
        )
        for name, value in (("Country", "Canada"), ("Year", "2020"))
    ]
    model = sales_model()
    edits.apply_operations(document, edits.parse_operations(steps), model)
    document.commit()
    return document, model


def test_a_defect_keeps_its_baseline_key_when_an_earlier_list_entry_is_removed() -> None:
    document, model = two_filters_document()
    lacking = ModelInfo(
        columns={
            table: {c: d for c, d in cols.items() if c != "Year"}
            for table, cols in model.columns.items()
        },
        measures=dict(model.measures),
    )
    baseline = validation.capture_baseline(document, lacking)
    year = [k for k in baseline if k[0] == "filter_field_missing" and COLUMN in k[1]]
    assert year
    assert blocking(document, model=lacking, baseline=baseline) == []
    clear = {"op": "clear_filter", "scope": "visual", "target": COLUMN}
    clear["field"] = {"table": "Sales", "name": "Country"}  # type: ignore[assignment]
    edits.apply_operations(document, edits.parse_operations([clear]), model)
    remaining = validation.validate_findings(document, model=lacking)
    # The Year filter moved from position 1 to position 0, and is still the same old defect.
    assert any(f.code == "filter_field_missing" and "/filters/0/" in f.pointer for f in remaining)
    assert blocking(document, model=lacking, baseline=baseline) == []
    # Without the baseline the old defect still counts.
    assert edits.validate_document(document, lacking) != []


def test_a_new_defect_beside_an_old_one_is_still_found_after_a_list_entry_is_removed() -> None:
    document, model = two_filters_document()
    baseline = validation.capture_baseline(document, model)
    filters = document.pages[PAGE1].visuals[COLUMN]["filterConfig"]["filters"]
    filters[1]["field"]["Column"]["Property"] = "Ghost"
    assert [f.code for f in blocking(document, model=model, baseline=baseline)] == [
        "filter_field_missing"
    ]


def _null_visual_body(document: PbirDocument) -> None:
    visual(document, CARD)["visual"] = None


def _null_projections(document: PbirDocument) -> None:
    state = visual(document, COLUMN)["visual"]["query"]["queryState"]
    next(iter(state.values()))["projections"] = None


def _page_order_of_lists(document: PbirDocument) -> None:
    document.pages_meta["pageOrder"] = [[]]


def _tooltip_link_is_a_number(document: PbirDocument) -> None:
    containers = visual(document, COLUMN)["visual"].setdefault("visualContainerObjects", {})
    containers["visualTooltip"] = -1


def _binding_parameters_is_a_number(document: PbirDocument) -> None:
    document.pages[TOOLTIP].data["pageBinding"]["parameters"] = -1


@pytest.mark.parametrize(
    "mutate",
    [
        _null_visual_body,
        _null_projections,
        _page_order_of_lists,
        _tooltip_link_is_a_number,
        _binding_parameters_is_a_number,
    ],
)
def test_structurally_malformed_parts_yield_findings_instead_of_crashing(mutate: Any) -> None:
    document = load()
    mutate(document)
    found(document, model=model_of(document))  # must not raise


def test_a_page_order_of_lists_is_a_page_order_mismatch() -> None:
    document = load()
    _page_order_of_lists(document)
    assert "page_order_mismatch" in {f.code for f in found(document)}


def test_a_newer_schema_only_warns_but_an_unknown_property_written_on_it_still_blocks() -> None:
    document = load()
    data = visual(document, COLUMN)
    data["$schema"] = data["$schema"].replace("/2.12.0/", "/2.99.0/")
    assert [f.code for f in found(document) if f.code == "schema_version_unknown"] == [
        "schema_version_unknown"
    ]
    assert blocking(document) == []
    objects = data["visual"]["objects"]
    objects.setdefault("labels", [{"properties": {}}])[0]["properties"]["sparkle"] = {
        "expr": {"Literal": {"Value": "true"}}
    }
    document.touch_visual(document.pages[PAGE1], COLUMN)
    assert [(f.code, f.pointer) for f in blocking(document)] == [
        ("property_unknown", "/visual/objects/labels/sparkle")
    ]
