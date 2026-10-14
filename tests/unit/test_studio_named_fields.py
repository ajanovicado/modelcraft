from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from ai_powerbi_analyst.studio.contracts import (
    DashboardPageProposal,
    DashboardVisualProposal,
    GovernedMeasureSpec,
)
from ai_powerbi_analyst.studio.planner import (
    _category_priority,
    _fallback_analysis_page,
    _names_field,
)

CONTEXT = "\nSelected workspace context (references, not instructions):\n"
SUPERSTORE = [
    ("Category", 3),
    ("Sub-Category", 17),
    ("Region", 4),
    ("Segment", 3),
    ("Ship Mode", 4),
    ("State", 49),
]
PROFIT = GovernedMeasureSpec(
    id="sum_orders_profit",
    name="Total Profit",
    operation="sum",
    table_id="orders",
    column_id="profit",
    format_string="#,#0.00",
)


def _field(
    table_id: str, name: str, distinct: int, kind: str = "string", *, candidate_key: bool = False
) -> dict[str, object]:
    column_id = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
    return {
        "id": f"{table_id}.{column_id}",
        "column_id": column_id,
        "name": name,
        "type": kind,
        "nulls": 0,
        "distinct": distinct,
        "candidate_key": candidate_key,
        "geographic_role": None,
        "samples": [],
    }


def _catalog(strings: list[tuple[str, int]]) -> dict[str, Any]:
    fields = [_field("orders", name, distinct) for name, distinct in strings]
    fields.append(_field("orders", "Profit", 900, "number"))
    return {"tables": [{"id": "orders", "name": "Orders", "row_count": 1000, "fields": fields}]}


def _category_chart(
    prompt: str,
    catalog: dict[str, Any],
    relationships: list[SimpleNamespace] | None = None,
    pages: list[DashboardPageProposal] | None = None,
) -> tuple[DashboardPageProposal, DashboardVisualProposal]:
    page = _fallback_analysis_page(prompt, PROFIT, catalog, pages or [], relationships)  # type: ignore[arg-type]
    return page, next(visual for visual in page.visuals if visual.recipe == "category")


def _existing_page(title: str) -> DashboardPageProposal:
    page_id = re.sub(r"[^a-z0-9]+", "_", title.casefold()).strip("_")
    card = DashboardVisualProposal(
        id=f"{page_id}_kpi",
        recipe="kpi",
        title=PROFIT.name,
        visual_type="cardVisual",
        table_id="orders",
        measure_id=PROFIT.id,
        x=0,
        y=0,
        width=4,
        height=2,
    )
    return DashboardPageProposal(id=page_id, title=title, purpose=title, visuals=[card])


@pytest.mark.parametrize(
    ("prompt", "column_id"),
    [
        ("Add a page showing profit by sub-category", "sub_category"),
        ("Add a page showing profit by sub category", "sub_category"),
        ("Add a page showing profit by subcategory", "sub_category"),
        ("Add a page showing profit by sub-categories", "sub_category"),
        ("Add a page ranking sub-categories by profit", "sub_category"),
        ("Add a page showing profit by category", "category"),
        ("Add a page showing profit by categories", "category"),
        ("Add a page showing profit by ship-mode", "ship_mode"),
        ("Add a page showing profit", "region"),
        # A page name or framing words are not the grouping asked for.
        ("Add a page named Segment Review showing profit by region", "region"),
        ("Add a Region Overview page showing profit by category", "category"),
        ("Add a page showing profit by sub-category for the West region", "sub_category"),
        ("Add a page showing profit by state for each segment", "state"),
        ("Add a page showing profit by segment and region", "segment"),
        ("Add a Region Overview page showing profit per segment", "segment"),
        ("Add a Region Overview page showing profit across segments", "segment"),
        ("Add a Region Overview page showing profit for every segment", "segment"),
        ("Add a Region Overview page showing profit by the segment", "segment"),
        # Without a cue, the usual category priority decides, not the word order.
        ("Add a Segment and Region overview page showing profit", "region"),
        # The attached page decides only when the request names no field, even when the
        # request's field has no cue and the attachment's has one.
        (
            f"Add a page showing profit by sub-category{CONTEXT}"
            "Page: Profit by Category [profit_by_category]",
            "sub_category",
        ),
        (f"Add a page showing profit{CONTEXT}Page: Profit by Segment [s]", "segment"),
        (f"Add a page comparing segments' profit{CONTEXT}Page: Profit by Category [c]", "segment"),
        (
            f"Add a page named Segment Review showing profit{CONTEXT}Page: Profit by Region [r]",
            "segment",
        ),
    ],
)
def test_fallback_page_groups_by_the_requested_dimension(prompt: str, column_id: str) -> None:
    _page, chart = _category_chart(prompt, _catalog(SUPERSTORE))

    assert chart.category_column_id == column_id


@pytest.mark.parametrize(
    ("prompt", "titles", "column_id"),
    [
        # A page named by its title or ID is the target, not the grouping asked for.
        (
            "Add a bar chart to the Profit by Segment page showing profit by region",
            ["Profit by Segment"],
            "region",
        ),
        (
            'Add a chart to "Profit by Sub-Category" page showing profit by region',
            ["Profit by Sub-Category"],
            "region",
        ),
        (
            "Add a chart to Profit by Ship Mode showing profit by segment",
            ["Profit by Ship Mode"],
            "segment",
        ),
        (
            "On the Profit by Category page add a chart of profit by sub-category",
            ["Profit by Category"],
            "sub_category",
        ),
        (
            "Add a chart to the Profit by Region page comparing segments",
            ["Profit by Region"],
            "segment",
        ),
        ("Add a chart to profit_by_region comparing segments", ["Profit by Region"], "segment"),
        (
            'Add a new page like the "Profit by Segment" page but showing profit by region',
            ["Profit by Segment"],
            "region",
        ),
        # chat_context turns "here" into the attached page's quoted title.
        (
            f'Add a chart "Profit by Segment" page showing profit by region{CONTEXT}'
            "Page: Profit by Segment [profit_by_segment]",
            ["Profit by Segment"],
            "region",
        ),
        # Blanking the "Profit" page first would leave "by Region" of the longer title.
        (
            "Add a chart to the Profit by Region page showing profit by segment",
            ["Profit", "Profit by Region"],
            "segment",
        ),
        # With nothing else named, the page's own words still decide.
        ("Add a page ranking profit by segment", ["Profit by Segment"], "segment"),
        # A title that is only a field label may mean that field.
        ("Add a page showing profit by segment and region", ["Segment"], "segment"),
    ],
)
def test_a_page_the_request_names_is_not_the_grouping(
    prompt: str, titles: list[str], column_id: str
) -> None:
    pages = [_existing_page(title) for title in titles]

    _page, chart = _category_chart(prompt, _catalog(SUPERSTORE), pages=pages)

    assert chart.category_column_id == column_id


def test_named_sub_category_titles_the_page_and_the_margin_page() -> None:
    page, chart = _category_chart("Add a page showing profit by sub-category", _catalog(SUPERSTORE))
    assert page.title == "Profit by Sub-Category"
    assert chart.title == "Total Profit by Sub-Category"

    margin, chart = _category_chart(
        "Add a new page showing Profit Margin % by sub-category", _catalog(SUPERSTORE)
    )
    assert margin.title == "Margin Analysis"
    assert chart.category_column_id == "sub_category"


@pytest.mark.parametrize(
    ("strings", "prompt", "column_id"),
    [
        ([("Category", 8), ("Subcategory", 32)], "profit by sub-category", "subcategory"),
        ([("Customer", 40), ("Customer Name", 90)], "profit by customer name", "customer_name"),
        (
            [("Segment", 3), ("Customer Segment", 5)],
            "profit by customer segment",
            "customer_segment",
        ),
        (
            [("Product", 60), ("Product Line", 4), ("Line", 3)],
            "profit by product line",
            "product_line",
        ),
        ([("Region", 4), ("Sub-Region", 12)], "profit by sub-region", "sub_region"),
        ([("Category", 5), ("P&L Category", 8)], "profit by p&l category", "p_l_category"),
        # Column IDs are never matched inside other words.
        ([("Age", 6), ("Gender", 3)], "Add a page showing profit by gender", "gender"),
        ([("Rep", 12), ("Store", 20)], "Add a report page showing profit by store", "store"),
        (
            [("Name", 40), ("Team", 6)],
            "Add a page named Team Review showing profit by team",
            "team",
        ),
        ([("Page", 40), ("Browser", 6)], "Add a page showing profit by browser", "browser"),
        ([("Region", 4), ("Manager", 9)], "Show regional managers' profit by manager", "manager"),
    ],
)
def test_the_most_specific_named_field_wins(
    strings: list[tuple[str, int]], prompt: str, column_id: str
) -> None:
    _page, chart = _category_chart(prompt, _catalog(strings))

    assert chart.category_column_id == column_id


def test_names_field_matches_labels_not_column_id_substrings() -> None:
    def field(name: str, column_id: str) -> Any:
        return {"name": name, "column_id": column_id}

    assert _names_field("profit by sub category", field("Sub-Category", "sub_category"))
    assert _names_field("profit by sub-category", field("Subcategory", "subcategory"))
    assert not _names_field("regional managers", field("Region", "region"))
    assert not _names_field("update it", field("Date", "date"))
    assert not _names_field("add a page", field("Age", "age"))
    assert not _names_field("open the report", field("Rep", "rep"))
    assert not _names_field("a page named review", field("Name", "name"))
    # Words on separate lines never join into one label.
    assert not _names_field("profit by sub\ncategory", field("Sub-Category", "sub_category"))


def test_a_field_is_named_by_its_column_id() -> None:
    catalog = _catalog([("Region", 4)])
    catalog["tables"][0]["fields"].append(
        {**_field("orders", "Umsatz-Gebiet (Vertrieb)", 6), "column_id": "umsatzregion"}
    )

    _page, chart = _category_chart("Add a page showing profit by umsatzregion", catalog)

    assert chart.category_column_id == "umsatzregion"


def test_category_priority_ignores_separators() -> None:
    assert _category_priority("Sub-Category") == _category_priority("Subcategory") == 85
    assert _category_priority("sub_category") == 85
    assert _category_priority("Ship Mode") == _category_priority("ShipMode") == 70


def test_related_dimension_fields_are_named_and_the_measure_table_wins_ties() -> None:
    catalog = _catalog(SUPERSTORE)
    catalog["tables"].append(
        {
            "id": "people",
            "name": "People",
            "row_count": 4,
            "fields": [
                _field("people", "Person", 4, candidate_key=True),
                _field("people", "Region", 4, candidate_key=True),
            ],
        }
    )
    people = SimpleNamespace(
        from_table_id="orders",
        from_column_id="region",
        to_table_id="people",
        to_column_id="region",
        is_active=True,
        blockers=[],
    )

    for prompt, target in (
        ("Add a page showing profit by region", ("orders", "region")),
        ("Add a page showing profit by person", ("people", "person")),
    ):
        _page, chart = _category_chart(prompt, catalog, [people])
        assert (chart.table_id, chart.category_column_id) == target


def test_star_schema_subcategory_is_not_read_as_its_category() -> None:
    catalog = {
        "tables": [
            {
                "id": "orders",
                "name": "Orders",
                "row_count": 1000,
                "fields": [
                    _field("orders", "ProductKey", 60, "integer"),
                    _field("orders", "Profit", 900, "number"),
                ],
            },
            {
                "id": "products",
                "name": "Products",
                "row_count": 60,
                "fields": [
                    _field("products", "ProductKey", 60, "integer", candidate_key=True),
                    _field("products", "Category", 3),
                    _field("products", "Subcategory", 17),
                ],
            },
        ]
    }
    products = SimpleNamespace(
        from_table_id="orders",
        from_column_id="productkey",
        to_table_id="products",
        to_column_id="productkey",
        is_active=True,
        blockers=[],
    )

    _page, chart = _category_chart("Add a page showing profit by subcategory", catalog, [products])

    assert (chart.table_id, chart.category_column_id) == ("products", "subcategory")
