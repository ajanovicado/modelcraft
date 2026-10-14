from __future__ import annotations

from ai_powerbi_analyst.studio.planner import _fallback_measures


def _field(name: str, kind: str = "integer", *, candidate_key: bool = False) -> dict[str, object]:
    column_id = name.casefold().replace(" ", "_")
    return {
        "id": f"sales.{column_id}",
        "column_id": column_id,
        "name": name,
        "type": kind,
        "nulls": 0,
        "distinct": 50,
        "candidate_key": candidate_key,
        "geographic_role": None,
        "samples": [],
    }


def test_camel_case_key_and_id_columns_never_become_additive_measures() -> None:
    catalog = {
        "tables": [
            {
                "id": "sales",
                "name": "Sales",
                "row_count": 100,
                "fields": [
                    _field("ProductKey"),
                    _field("CustomerID"),
                    _field("StoreCode"),
                    _field("Sales", "number"),
                    _field("Units"),
                ],
            }
        ]
    }

    names = [measure.name for measure in _fallback_measures(catalog).measures]  # type: ignore[arg-type]

    assert names == ["Total Sales", "Total Units"]


def test_charts_follow_the_measure_the_request_names() -> None:
    from ai_powerbi_analyst.studio.planner import _deterministic_understanding, _fallback_pages

    catalog = {
        "tables": [
            {
                "id": "sales",
                "name": "Sales",
                "row_count": 100,
                "fields": [
                    _field("Order Date", "date"),
                    {**_field("Region", "string"), "distinct": 4},
                    _field("Sales", "number"),
                    _field("Gross Profit", "number"),
                ],
            }
        ]
    }
    measures = _fallback_measures(catalog)  # type: ignore[arg-type]
    understanding = _deterministic_understanding(catalog)  # type: ignore[arg-type]

    default = _fallback_pages(catalog, measures, understanding)  # type: ignore[arg-type]
    focused = _fallback_pages(  # type: ignore[arg-type]
        catalog, measures, understanding, "Why did gross profits slip?"
    )

    def trend_of(pages):  # type: ignore[no-untyped-def]
        overview = next(page for page in pages if page.id == "overview")
        return next(visual for visual in overview.visuals if visual.recipe == "trend")

    trend = trend_of(default)
    assert trend.title == "Total Sales over time"
    focused_trend = trend_of(focused)
    assert focused_trend.title == "Total Gross Profit over time"
    # The stored measure proposal keeps its reviewed order.
    assert [m.name for m in measures.measures][:2] == ["Total Sales", "Total Gross Profit"]
