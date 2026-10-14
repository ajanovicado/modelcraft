"""How the edit layer's messages name a visual (``_label``) and how ``_set`` writes a property.

A person points at a visual by what it shows: its caption, else its kind and first field, else
its kind and place. The internal type (``tableEx``, ``cardVisual``) and the short id are for
nobody but the developer, and never appear while a caption or a field exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_powerbi_analyst.studio import report_edit_context as context
from ai_powerbi_analyst.studio.pbir_document import PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
TWIN = FIXTURES / "structure-twin.pbix"


def visual(kind: str, **more: Any) -> dict[str, Any]:
    return {"visual": {"visualType": kind}, **more}


def with_title(text: str, kind: str = "columnChart") -> dict[str, Any]:
    entry = {
        "properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "text": {"expr": {"Literal": {"Value": f"'{text}'"}}},
        }
    }
    return {"visual": {"visualType": kind, "visualContainerObjects": {"title": [entry]}}}


def field(name: str) -> dict[str, Any]:
    return {
        "field": {"Column": {"Expression": {"SourceRef": {"Entity": "T"}}, "Property": name}},
        "queryRef": f"T.{name}",
        "nativeQueryRef": name,
    }


def with_field(kind: str, name: str) -> dict[str, Any]:
    return {
        "visual": {
            "visualType": kind,
            "query": {"queryState": {"Values": {"projections": [field(name)]}}},
        }
    }


def test_the_caption_comes_first_and_a_long_one_is_cut() -> None:
    assert context._label(with_title("Sales by channel"), "abc12345") == "'Sales by channel'"
    long = context._label(with_title("x" * 90), "abc12345")
    assert long == "'" + "x" * 59 + "…'"


def test_a_dynamic_title_is_not_a_caption() -> None:
    dynamic = with_field("tableEx", "Agent ID")
    body = dynamic["visual"]
    body["visualContainerObjects"] = {
        "title": [{"properties": {"text": {"expr": {"Measure": {"Property": "Title"}}}}}]
    }
    assert context._label(dynamic, "abc12345") == "the table of Agent ID"


def test_without_a_caption_it_is_the_kind_a_person_knows_and_the_first_field() -> None:
    assert context._label(with_field("tableEx", "Agent ID"), "x") == "the table of Agent ID"
    assert context._label(with_field("cardVisual", "Sales"), "x") == "the card of Sales"
    assert context._label(with_field("clusteredColumnChart", "Sales"), "x") == (
        "the clustered column chart of Sales"
    )
    assert context._label(with_field("advancedSlicerVisual", "Store"), "x") == (
        "the button slicer of Store"
    )
    # a type nobody named gets its words from its camel case, never the raw name
    assert (
        context._label(with_field("someNewVisual", "Sales"), "x") == "the some new visual of Sales"
    )


def test_a_visual_with_no_caption_and_no_field_is_named_by_its_place_and_never_its_id() -> None:
    button = visual("actionButton", position={"x": 40.4, "y": 19.6, "width": 50, "height": 30})
    assert context._label(button, "eff2d7b0") == "the action button at 40, 20"
    member = visual(
        "actionButton", position={"x": 5, "y": 5}, parentGroupName="grp"
    )  # its position is relative to its group, so it says nothing to a person
    assert context._label(member, "eff2d7b0") == "the action button eff2d7b0"
    assert context._label(visual("image"), "81ab7627") == "the image 81ab7627"


def test_a_group_is_named_by_its_display_name() -> None:
    group = {"visualGroup": {"displayName": "Header block"}, "name": "g"}
    assert context._label(group, "g") == "the group 'Header block'"
    assert context._label({"visualGroup": {}, "name": "g"}, "g") == "a group of visuals"


def test_the_real_dashboards_visuals_are_named_by_caption_or_field() -> None:
    document = PbirDocument(PbixPackage(TWIN))
    _, table = document.visual("e82e02f93055d6b0b544")
    _, chart = document.visual("b404a461ede7604a2774")
    assert context._label(table, "e82e02f9") == "the table of Agent ID"
    assert context._label(chart, "b404a461") == "'Total MTD Sales'"


def test_set_writes_under_the_default_selector_where_the_type_only_honors_it() -> None:
    """A shape's fill written without a state selector is ignored by Power BI Desktop (measured
    2026-09-28), so the helper passes the visual's type on and the entry gets the selector."""
    document = PbirDocument(PbixPackage(TWIN))
    shape: dict[str, Any] = {"name": "s", "visual": {"visualType": "shape", "objects": {}}}
    context._set(document, shape, "fill", "fillColor", "#112233")
    entries = shape["visual"]["objects"]["fill"]
    assert [e.get("selector") for e in entries] == [{"id": "default"}]
    assert "fillColor" in entries[0]["properties"]
    # a selector given by the caller is used as it is
    other: dict[str, Any] = {"name": "s", "visual": {"visualType": "shape", "objects": {}}}
    context._set(document, other, "fill", "show", True, {"id": "hover"})
    assert [e.get("selector") for e in other["visual"]["objects"]["fill"]] == [{"id": "hover"}]
