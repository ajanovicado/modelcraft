from __future__ import annotations

from ai_powerbi_analyst.studio.contracts import DatasetNarrativePlan
from ai_powerbi_analyst.studio.planner import _deterministic_understanding


def test_model_narrative_shows_table_and_field_names_not_profile_ids() -> None:
    catalog = {
        "tables": [
            {
                "id": "superstore_orders_1_641f9576",
                "name": "Orders",
                "row_count": 3,
                "fields": [
                    {
                        "id": "superstore_orders_1_641f9576.category",
                        "name": "Category",
                        "type": "string",
                    },
                ],
            },
            {
                "id": "superstore_returns_2_641f9576",
                "name": "Returns",
                "row_count": 1,
                "fields": [],
            },
        ]
    }
    narrative = DatasetNarrativePlan(
        summary="Examine [superstore_orders_1_641f9576] alongside [superstore_returns_2_641f9576].",
        suggested_questions=[
            "Which [superstore_orders_1_641f9576.category] leads [superstore_orders_1_641f9576]?"
        ],
        caveats=["[unknown_id] stays as written; Orders [1] loses its index."],
    )

    understanding = _deterministic_understanding(catalog, narrative=narrative)

    assert understanding.summary == "Examine Orders alongside Returns."
    assert understanding.suggested_questions == ["Which Orders[Category] leads Orders?"]
    assert understanding.caveats == ["[unknown_id] stays as written; Orders loses its index."]
