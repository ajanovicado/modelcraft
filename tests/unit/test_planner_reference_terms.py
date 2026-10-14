"""The planner's reference lookups treat the workspace's names as vocabulary."""

from __future__ import annotations

from typing import Any

from ai_powerbi_analyst.studio.planner import StudioPlanningService, _catalog_names

CATALOG: Any = {
    "tables": [
        {"name": "Orders", "fields": [{"name": "Profit"}, {"name": "Region"}]},
        {"name": "People", "fields": [{"name": "Person"}]},
    ]
}


class _Receipt:
    def context(self) -> str:
        return ""


class _Knowledge:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, query: str, **kwargs: Any) -> _Receipt:
        self.calls.append({"query": query, **kwargs})
        return _Receipt()


def test_catalog_names_lists_every_table_and_field_name() -> None:
    assert _catalog_names(CATALOG) == ["Orders", "Profit", "Region", "People", "Person"]


def test_advanced_dax_references_pass_the_catalog_names() -> None:
    knowledge = _Knowledge()
    stages: list[str] = []
    planner = StudioPlanningService(
        None, None, knowledge, on_retrieval=lambda _ws, stage, _r: stages.append(stage)
    )  # type: ignore[arg-type]
    planner._references("Profit by People", "workspace", "advanced_dax", CATALOG)
    assert knowledge.calls == [
        {
            "query": "Profit by People",
            "max_characters": 2400,
            "context_terms": ["Orders", "Profit", "Region", "People", "Person"],
        }
    ]
    assert stages == ["advanced_dax"]
