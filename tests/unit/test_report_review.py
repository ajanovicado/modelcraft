"""The page review ("look at what I just did"): every rule on a small page built here, the
fixes applied through the edit families, review_edit's "only what the edit did", and the
scrubbed twin and the real Agents dashboard (read-only, skipped when absent) pinned."""

from __future__ import annotations

import json
import shutil
import statistics
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import report_edits
from ai_powerbi_analyst.studio.pbir_document import PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_context import ModelInfo
from ai_powerbi_analyst.studio.report_review import (
    RULES,
    Finding,
    Review,
    describe_findings,
    review_edit,
    review_page,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
TWIN = FIXTURES / "structure-twin.pbix"
REAL = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"
PAGE = "ReportSection8454c812b60b8bb83b99"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"

# What the Agents page (and its twin) gets: two slicers on Channel, one headed "Country"; a
# chart 1.4 px off the chart grid; one chart 1.4 px bigger both ways; a slicer 1 px taller.
AGENTS_KEYS = [
    "same_field_slicers|54be5cf0783e66471540,b85aa81a8b5c60030d13|DimChannel.ChannelName",
    "near_miss|54be5cf0783e66471540,b85aa81a8b5c60030d13|row",
    "near_miss|97d1504080b699eae7d7,b404a461ede7604a2774|column",
    "near_miss|b404a461ede7604a2774,da32cdbb0090e885649b|row",
    "near_miss|ab13db00c71dcec86e99,da32cdbb0090e885649b|column",
]


# --- building a page ------------------------------------------------------------------------


def lit(value: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": value}}}


def solid(hex_color: str) -> dict[str, Any]:
    return {"solid": {"color": lit(f"'{hex_color}'")}}


def projection(kind: str, entity: str, prop: str) -> dict[str, Any]:
    return {
        "field": {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
        "queryRef": f"{entity}.{prop}",
        "nativeQueryRef": prop,
    }


def visual(
    name: str,
    visual_type: str,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    roles: dict[str, list[tuple[str, str, str]]] | None = None,
    title: str | None = None,
    title_props: dict[str, Any] | None = None,
    objects: dict[str, Any] | None = None,
    z: int = 0,
    hidden: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {"visualType": visual_type}
    if roles:
        body["query"] = {
            "queryState": {
                role: {"projections": [projection(*f) for f in fields]}
                for role, fields in roles.items()
            }
        }
    container: dict[str, Any] = {}
    props = dict(title_props or {})
    if title is not None:
        props["text"] = lit("'" + title.replace("'", "''") + "'")
    if props:
        container["title"] = [{"properties": props}]
    if container:
        body["visualContainerObjects"] = container
    if objects:
        body["objects"] = objects
    found: dict[str, Any] = {
        "$schema": SCHEMA + "visualContainer/2.7.0/schema.json",
        "name": name,
        "position": {"x": x, "y": y, "z": z, "width": width, "height": height},
        "visual": body,
    }
    if hidden:
        found["isHidden"] = True
    return found


def chart(name: str, x: float, y: float, w: float = 300, h: float = 200, **kw: Any) -> dict:
    measure = kw.pop("measure", name.capitalize())
    kw.setdefault("title", f"{measure} by Region")
    return visual(
        name,
        kw.pop("visual_type", "clusteredColumnChart"),
        x,
        y,
        w,
        h,
        roles=kw.pop(
            "roles",
            {"Category": [("Column", "Orders", "Region")], "Y": [("Measure", "Orders", measure)]},
        ),
        **kw,
    )


def card(name: str, x: float, y: float, w: float = 200, h: float = 120, **kw: Any) -> dict:
    measure = kw.pop("measure", name.capitalize())
    return visual(
        name, "cardVisual", x, y, w, h, roles={"Data": [("Measure", "Orders", measure)]}, **kw
    )


def slicer(name: str, x: float, y: float, column: str, **kw: Any) -> dict:
    return visual(
        name, "slicer", x, y, 200, 60, roles={"Values": [("Column", "Orders", column)]}, **kw
    )


def build(
    tmp_path: Path,
    visuals: list[dict[str, Any]],
    *,
    page: dict[str, Any] | None = None,
    name: str = "page.pbix",
) -> PbirDocument:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "Report/definition/report.json",
            json.dumps({"$schema": SCHEMA + "report/3.0.0/schema.json"}),
        )
        z.writestr(
            "Report/definition/pages/pages.json",
            json.dumps(
                {"$schema": SCHEMA + "pagesMetadata/1.0.0/schema.json", "pageOrder": ["one"]}
            ),
        )
        z.writestr(
            "Report/definition/pages/one/page.json",
            json.dumps(
                {
                    "$schema": SCHEMA + "page/2.0.0/schema.json",
                    "name": "one",
                    "displayName": "One",
                    "width": 1280,
                    "height": 720,
                    **(page or {}),
                }
            ),
        )
        for item in visuals:
            z.writestr(
                f"Report/definition/pages/one/visuals/{item['name']}/visual.json", json.dumps(item)
            )
    return PbirDocument(PbixPackage(path))


def rules_of(review: Review) -> list[str]:
    return [f.rule for f in review.findings]


def only(review: Review, rule: str) -> list[Finding]:
    return [f for f in review.findings if f.rule == rule]


def apply(document: PbirDocument, ops: list[dict[str, Any]]) -> None:
    report_edits.apply_operations(document, report_edits.parse_operations(ops), ModelInfo())


def assert_fix_clears(document: PbirDocument, finding: Finding) -> None:
    """The finding's fix applies through the edit families, the finding is gone, and the fix
    introduces nothing new."""
    assert finding.fix is not None
    before = review_page(document, finding.page)
    apply(document, finding.fix)
    after = review_page(document, finding.page)
    assert finding.key not in {f.key for f in after.findings}
    assert review_edit(before, after) == []


# --- a clean page -------------------------------------------------------------------------------


def test_a_tidy_page_has_no_findings_and_says_what_it_could_not_check(tmp_path):
    document = build(
        tmp_path,
        [
            card("sales", 20, 20),
            card("profit", 240, 20),
            chart("margin", 20, 160),
            slicer("region", 340, 160, "Region"),
        ],
    )
    review = review_page(document, "one")
    assert review.findings == []
    assert review.page == "one" and review.page_name == "One"
    # Without a payload the rules that read what the renderer knows are skipped, with why.
    assert set(review.skipped) == {
        "visual_error",
        "needs_power_bi",
        "mixed_number_formats",
        "empty_visual",
        "pie_slices",
    }
    assert "near_miss" in review.checked and "same_field_slicers" in review.checked
    assert review.summary == {"problems": 0, "suggestions": 0, "by_rule": {}}
    json.dumps(review.to_dict())  # the API sends it as is
    # The page by display name or position is the same page.
    assert review_page(document, "1").page == "one"


# --- geometry -----------------------------------------------------------------------------------


def test_overlap_reuses_the_validation_test_and_skips_labels_hidden_and_overlays(tmp_path):
    document = build(
        tmp_path,
        [
            chart("sales", 100, 100, z=1000),
            chart("profit", 250, 150, z=2000),  # covers 150x150 of 300x200: 37.5%
            chart("cost", 700, 100, z=0),
            chart("ghost", 720, 120, z=500, hidden=True),
            visual("note", "textbox", 710, 110, 100, 30, z=3000),
            chart("tax", 1000, 100, z=0),
            chart("fee", 1270, 300, z=0, w=300),  # 10 px on the page: off canvas, not overlap
        ],
    )
    found = only(review_page(document, "one"), "overlap")
    assert len(found) == 1
    assert found[0].visuals == ["profit", "sales"]  # the one on top first
    assert found[0].severity == "problem" and found[0].fix is None
    assert found[0].message == (
        "The 'Profit by Region' clustered column chart covers 38% of the 'Sales by Region' "
        "clustered column chart."
    )
    assert found[0].amount == pytest.approx(0.375)


def test_off_canvas_and_clipped_visuals_get_a_move_back(tmp_path):
    document = build(tmp_path, [chart("sales", 1100, 600), chart("lost", 1300, 100)])
    review = review_page(document, "one")
    found = {f.visuals[0]: f for f in only(review, "off_canvas")}
    assert found["sales"].message == (
        "The 'Sales by Region' clustered column chart is cut off by the right and bottom edges "
        "of the page."
    )
    assert found["sales"].fix == [{"op": "set_position", "visual": "sales", "x": 980.0, "y": 520.0}]
    assert found["lost"].message.endswith("is off the page, so nobody sees it.")
    assert found["sales"].amount == pytest.approx(120.0)
    assert_fix_clears(document, found["sales"])


def test_too_small_minimum_chart_and_card_number(tmp_path):
    big_number = {
        "value": [{"properties": {"fontSize": lit("45D")}, "selector": {"metadata": "Orders.Big"}}]
    }
    document = build(
        tmp_path,
        [
            chart("thin", 20, 20, h=12),
            chart("small", 20, 100, w=140, h=80),
            card("big", 400, 20, h=70, objects=big_number),
            visual("rule", "shape", 400, 300, 300, 2),  # a line: thin on purpose
        ],
    )
    found = {f.visuals[0]: f for f in only(review_page(document, "one"), "too_small")}
    assert set(found) == {"thin", "small", "big"}
    assert found["thin"].severity == "problem"
    assert found["thin"].fix == [{"op": "set_position", "visual": "thin", "height": 300}]
    assert found["small"].severity == "suggestion"
    assert found["small"].fix == [
        {"op": "set_position", "visual": "small", "width": 150.0, "height": 100.0}
    ]
    # 45 pt = 60 px of number and the 10 pt caption with their line height and 5 px padding.
    assert found["big"].fix == [{"op": "set_position", "visual": "big", "height": 98.0}]
    assert found["big"].message == (
        "The card of Big is 70 px tall, too short for its 45 pt number, which gets cut off."
    )
    assert_fix_clears(document, found["small"])


# --- alignment and spacing ----------------------------------------------------------------------


def row_of_cards(tmp_path: Path, third_y: float = 103, gap3: float = 20) -> PbirDocument:
    return build(
        tmp_path,
        [
            card("a", 100, 100),
            card("b", 320, 100),
            card("c", 540, third_y),
            card("d", 740 + gap3, 100, measure="D"),
        ],
    )


def test_near_miss_moves_the_odd_one_to_the_edge_the_page_agrees_on(tmp_path):
    document = row_of_cards(tmp_path)
    review = review_page(document, "one")
    found = only(review, "near_miss")
    assert [f.key for f in found] == ["near_miss|b,c|row", "near_miss|c,d|row"]
    first = found[0]
    assert first.visuals == ["c", "b"]
    assert first.message == "The card of C is 3 px out of line with the card of B beside it."
    assert first.fix == [{"op": "set_position", "visual": "c", "y": 100.0}]
    assert first.fix_summary == "Move it 3 px so their tops line up."
    assert_fix_clears(document, first)
    assert only(review_page(document, "one"), "near_miss") == []


def test_near_miss_after_an_edit_moves_the_edited_visual(tmp_path):
    review = review_page(row_of_cards(tmp_path), "one", focus=["b"])
    first = only(review, "near_miss")[0]
    assert first.fix == [{"op": "set_position", "visual": "b", "y": 103.0}]
    # Findings about what the edit touched come first.
    assert review.findings[0].visuals[0] == "b"


@pytest.mark.parametrize("third_y", [100.6, 110.0])
def test_sub_pixel_and_deliberate_offsets_are_not_near_misses(tmp_path, third_y):
    # 0.6 px is Desktop's float noise; 10 px apart is placed on purpose.
    assert only(review_page(row_of_cards(tmp_path, third_y=third_y), "one"), "near_miss") == []


def test_a_size_difference_with_aligned_tops_is_a_resize(tmp_path):
    document = build(
        tmp_path, [card("a", 100, 100), card("b", 320, 100, h=122), card("c", 540, 100)]
    )
    found = only(review_page(document, "one"), "near_miss")
    assert len(found) == 2 and {f.visuals[0] for f in found} == {"b"}
    assert found[0].message == "The card of B is 2 px taller than the card of A beside it."
    assert found[0].fix == [{"op": "set_position", "visual": "b", "height": 120.0}]


def test_uneven_gaps_are_spread_out_with_distribute(tmp_path):
    document = row_of_cards(tmp_path, third_y=100, gap3=30)
    found = only(review_page(document, "one"), "uneven_gaps")
    assert len(found) == 1
    assert found[0].visuals == ["a", "b", "c", "d"]
    assert found[0].message == (
        "The gaps between the card of A, the card of B, the card of C and 1 more run from 20 to "
        "30 px."
    )
    assert found[0].fix == [
        {"op": "distribute_visuals", "visuals": ["a", "b", "c", "d"], "axis": "horizontal"}
    ]
    assert_fix_clears(document, found[0])


def test_gaps_that_differ_a_lot_separate_groups_on_purpose(tmp_path):
    wide = row_of_cards(tmp_path, third_y=100, gap3=120)
    assert only(review_page(wide, "one"), "uneven_gaps") == []


# --- text -----------------------------------------------------------------------------------------


def test_same_field_slicers_names_the_mislabelled_one(tmp_path):
    country = {"header": [{"properties": {"text": lit("'Country'")}}]}
    document = build(
        tmp_path,
        [slicer("one_a", 20, 20, "Channel"), slicer("one_b", 260, 20, "Channel", objects=country)],
    )
    found = only(review_page(document, "one"), "same_field_slicers")
    assert len(found) == 1 and found[0].severity == "problem" and found[0].fix is None
    assert found[0].message == (
        "The 'Country' slicer filters Channel, the same field as the slicer of Channel, "
        "although its header says 'Country'."
    )
    plain = build(
        tmp_path, [slicer("x", 20, 20, "Region"), slicer("y", 260, 20, "Region")], name="b.pbix"
    )
    again = only(review_page(plain, "one"), "same_field_slicers")
    assert again[0].severity == "suggestion"
    assert again[0].message == (
        "The slicer of Region at 20, 20 and the slicer of Region at 260, 20 both filter Region."
    )


def test_too_many_visuals(tmp_path):
    cards = [card(f"k{i}", 20 + (i % 7) * 180, 20 + (i // 7) * 140, w=160) for i in range(21)]
    found = only(review_page(build(tmp_path, cards), "one"), "too_many_visuals")
    assert len(found) == 1 and found[0].visuals == []
    assert found[0].message.startswith("Page 'One' has 21 visuals with data")


# --- what only the payload tells -----------------------------------------------------------------


def row(visual_id: str, **extra: Any) -> dict[str, Any]:
    return {"id": visual_id, **extra}


def column(role: str, kind: str = "column", **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "role": role, "hidden": False, **extra}


def test_payload_errors_empty_visuals_and_power_bi_only(tmp_path):
    document = build(
        tmp_path,
        [
            chart("broken", 20, 20),
            chart("desk", 340, 20),
            visual("gauge", "gauge", 660, 20, 200, 150, roles={"Y": [("Measure", "Orders", "G")]}),
            chart("empty", 20, 300),
            card("blank", 340, 300),
            slicer("choice", 660, 300, "Region"),
        ],
    )
    measure = column("Y", "measure", query_ref="Orders.Empty")
    payload = {
        "page": {"id": "one"},
        "visuals": [
            row("broken", rows=[], error="A field is missing from the model"),
            row(
                "desk", rows=[], error="This page filter needs Power BI Desktop", needs_desktop=True
            ),
            row(
                "gauge", rows=[], unsupported_reason="Gauge visuals are previewed in Power BI only"
            ),
            row("empty", rows=[], columns=[column("Category"), measure]),
            row("blank", rows=[[None]], columns=[column("Data", "measure")]),
            row("choice", rows=[], columns=[column("Values")]),
        ],
    }
    review = review_page(document, "one", payload=payload)
    assert review.skipped == {}
    messages = {(f.rule, f.visuals[0]): f.message for f in review.findings}
    assert messages[("visual_error", "broken")] == (
        "The 'Broken by Region' clustered column chart shows an error: A field is missing from "
        "the model."
    )
    assert messages[("needs_power_bi", "desk")] == (
        "Studio cannot draw the 'Desk by Region' clustered column chart (This page filter needs "
        "Power BI Desktop)."
    )
    assert ("needs_power_bi", "gauge") in messages
    assert messages[("empty_visual", "empty")].endswith("shows no data with the current filters.")
    assert messages[("empty_visual", "blank")] == (
        "The card of Blank shows only blanks with the current filters."
    )
    assert messages[("empty_visual", "choice")].endswith(
        "has nothing to choose from with the current filters."
    )
    assert ("empty_visual", "broken") not in messages  # an error is not "empty"


def test_pie_slices(tmp_path):
    pie_roles = {"Category": [("Column", "Orders", "Product")], "Y": [("Measure", "Orders", "Pie")]}
    series_roles = {
        "Category": [("Column", "Orders", "Region")],
        "Series": [("Column", "Orders", "Year")],
        "Y": [("Measure", "Orders", "Sales")],
    }
    document = build(
        tmp_path,
        [
            chart("pie", 20, 20, visual_type="pieChart", roles=pie_roles),
            chart("one_year", 340, 20, roles=series_roles, title="One year"),
            chart("years", 660, 20, roles=series_roles, title="Many years"),
        ],
    )
    cols = [column("Category"), column("Series"), column("Y", "measure")]
    payload = {
        "visuals": [
            row("pie", rows=[[f"P{i}", i] for i in range(10)], columns=cols[::2]),
            row("one_year", rows=[["West", 2024, 1.0], ["East", 2024, 2.0]], columns=cols),
            row("years", rows=[["West", 2000 + i, 1.0] for i in range(9)], columns=cols),
        ]
    }
    review = review_page(document, "one", payload=payload)
    assert [(f.rule, f.visuals) for f in review.findings] == [
        ("pie_slices", ["pie"]),
    ]
    assert only(review, "pie_slices")[0].message.startswith(
        "The 'Pie by Region' pie chart has 10 slices"
    )


def test_one_measure_with_different_number_formats(tmp_path):
    roles = {"Values": [("Column", "Orders", "Region"), ("Measure", "Orders", "Sales")]}
    document = build(
        tmp_path,
        [
            visual("t1", "tableEx", 20, 20, 300, 200, roles=roles),
            visual("t2", "tableEx", 340, 20, 300, 200, roles=roles),
            visual("t3", "tableEx", 660, 20, 300, 200, roles=roles),
        ],
    )

    def sales(fmt: str) -> list[dict[str, Any]]:
        return [
            column("Values"),
            column("Values", "measure", query_ref="Orders.Sales", label="Sales", format=fmt),
        ]

    payload = {
        "visuals": {
            "t1": {"columns": sales("#,0")},
            "t2": {"columns": sales("#,0")},
            "t3": {"columns": sales("#,0.00")},
        }
    }
    found = only(review_page(document, "one", payload=payload), "mixed_number_formats")
    assert len(found) == 1
    assert found[0].visuals == ["t1", "t2", "t3"]
    assert found[0].message == (
        "Sales is formatted differently on this page: the table of Region at 660, 20 differs "
        "from the rest."
    )
    # set_number_format belongs to a family the facade may not register yet: it parses with
    # the family's own parser.
    assert found[0].fix == [
        {"op": "set_number_format", "visual": "t3", "field": "Orders.Sales", "format": "#,0"}
    ]


def test_a_payload_of_another_page_or_type_is_not_used(tmp_path):
    document = build(tmp_path, [chart("sales", 20, 20)])
    other_page = {"page": {"id": "two"}, "visuals": [row("sales", error="Boom", rows=[])]}
    assert only(review_page(document, "one", payload=other_page), "visual_error") == []
    other_type = {"visuals": [row("sales", visual_type="tableEx", error="Boom", rows=[])]}
    assert only(review_page(document, "one", payload=other_type), "visual_error") == []


# --- review_edit ----------------------------------------------------------------------------------


def test_review_edit_keeps_only_what_the_edit_introduced_or_made_worse(tmp_path):
    document = build(
        tmp_path, [chart("sales", 100, 100, z=1000), chart("profit", 380, 150, z=2000)]
    )
    before = review_page(document, "one")
    assert [f.rule for f in before.findings] == []  # 20 px of overlap: under a quarter
    assert review_edit(before, review_page(document, "one")) == []
    apply(document, [{"op": "set_position", "visual": "profit", "x": 250}])
    worse = review_page(document, "one", focus=["profit"])
    assert [f.rule for f in review_edit(before, worse)] == ["overlap"]
    # The same overlap, pushed further: made worse, so reported again.
    apply(document, [{"op": "set_position", "visual": "profit", "x": 150}])
    worst = review_page(document, "one", focus=["profit"])
    assert [f.rule for f in review_edit(worse, worst)] == ["overlap"]
    # Moved back a little: still there but better, so not repeated.
    apply(document, [{"op": "set_position", "visual": "profit", "x": 160}])
    assert review_edit(worst, review_page(document, "one", focus=["profit"])) == []


def test_every_rule_runs_or_says_why_not(tmp_path):
    document = build(tmp_path, [chart("sales", 20, 20)])
    bare = review_page(document, "one")
    assert sorted([*bare.checked, *bare.skipped]) == sorted(RULES)
    with_rows = review_page(document, "one", payload={"visuals": [row("sales", rows=[])]})
    assert with_rows.checked == list(RULES) and with_rows.skipped == {}


def test_describe_findings_limits_the_list(tmp_path):
    document = build(tmp_path, [chart(f"c{i}", 1300, 100 + i * 10) for i in range(5)])
    found = review_page(document, "one").findings
    text = describe_findings(found, limit=2)
    assert text.endswith(f"({len(found) - 2} more things to look at.)")


# --- the scrubbed twin and the real dashboard ---------------------------------------------


def twin(tmp_path: Path) -> PbirDocument:
    if not TWIN.exists():
        pytest.skip("structure-twin.pbix is not built in this checkout")
    copy = tmp_path / "twin.pbix"
    shutil.copyfile(TWIN, copy)
    return PbirDocument(PbixPackage(copy))


def test_structure_twin_gets_the_agents_findings_and_every_fix_clears_its_own(tmp_path):
    document = twin(tmp_path)
    review = review_page(document, PAGE)
    assert [f.key for f in review.findings] == AGENTS_KEYS
    assert review.summary == {
        "problems": 1,
        "suggestions": 4,
        "by_rule": {"same_field_slicers": 1, "near_miss": 4},
    }
    first = review.findings[1]
    assert (
        first.message == "The slicer of Channel is 1 px taller than the 'Country' slicer beside it."
    )
    assert first.fix == [{"op": "set_position", "visual": "b85aa81a8b5c60030d13", "height": 60.0}]
    # The label card laid over the Top-Bottom-N slicer is a caption, and the four equal gaps
    # of the cards are even.
    assert not {"overlap", "uneven_gaps"} & set(rules_of(review))
    for finding in review.findings:
        if finding.fix is not None:
            assert_fix_clears(document, finding)
    assert [f.rule for f in review_page(document, PAGE).findings] == ["same_field_slicers"]


def test_structure_twin_review_is_deterministic(tmp_path):
    document = twin(tmp_path)
    first = review_page(document, PAGE).to_dict()
    assert review_page(document, PAGE).to_dict() == first
    assert first["findings"][0]["id"].startswith("same_field_slicers-")


def agents(tmp_path: Path) -> Path:
    if not REAL.exists():
        pytest.skip("the real Agents Performance report is absent")
    copy = tmp_path / "agents.pbix"
    shutil.copyfile(REAL, copy)  # read-only: every tool here works on a copy
    return copy


def test_real_agents_dashboard_gets_few_true_findings_fast(tmp_path):
    document = PbirDocument(PbixPackage(agents(tmp_path)))
    review_page(document, PAGE)  # imports warm up
    times = []
    for _ in range(5):
        started = time.perf_counter()
        review = review_page(document, PAGE)
        times.append(time.perf_counter() - started)
    assert [f.key for f in review.findings] == AGENTS_KEYS
    assert statistics.median(times) < 0.2  # measured ~30 ms without a payload


def test_real_agents_dashboard_with_the_formatting_payload(tmp_path, monkeypatch):
    from ai_powerbi_analyst.integrity import sha256_file
    from ai_powerbi_analyst.studio import live_report
    from ai_powerbi_analyst.studio.live_report import LiveReportRequest, render_formatting_payload
    from ai_powerbi_analyst.studio.report_import import ImportedReport, read_report

    path = agents(tmp_path)
    monkeypatch.setattr(live_report, "model_formats", lambda *_: {})
    pages, _, _ = read_report(path)
    columns: set[tuple[str, str]] = set()
    measures: set[tuple[str, str]] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for kind, target in (("Column", columns), ("Measure", measures)):
                found = node.get(kind)
                if isinstance(found, dict) and "Property" in found:
                    entity = found.get("Expression", {}).get("SourceRef", {}).get("Entity")
                    if entity:
                        target.add((entity, found["Property"]))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    for page in pages:
        for item in page.visuals:
            walk(item.definition)
    report = ImportedReport(
        id="agents",
        workspace_id="w",
        name="Agents",
        source_path=str(path),
        archived_path=str(path),
        source_hash=sha256_file(path),
        base_hash="a" * 64,
        format="PBIR",
        pages=pages,
        tables=[{"TableName": t, "ColumnName": c} for t, c in sorted(columns)],
        measures=[{"TableName": t, "Name": m, "Expression": "1"} for t, m in sorted(measures)],
    )
    payload = render_formatting_payload(report, PAGE, LiveReportRequest(), None, "r1")
    document = PbirDocument(PbixPackage(path))
    review = review_page(document, PAGE, payload=payload)
    assert [f.key for f in review.findings] == AGENTS_KEYS
    assert set(review.skipped) == {"empty_visual", "pie_slices"}
