"""Compile reviewed date ranges to native, visual-scoped PBIR filters."""

import uuid
from datetime import timedelta

from ai_powerbi_analyst.contracts import TableProfile, VisualDateFilter


def date_filter_config(
    selected: VisualDateFilter, *, visual_id: str, table: TableProfile, semantic_name: str
) -> dict[str, object]:
    column = next((c for c in table.columns if c.id == selected.column_id), None)
    if (
        table.id != selected.table_id
        or column is None
        or column.data_type.value not in {"date", "datetime"}
    ):
        raise ValueError("Visual date filter requires a reviewed date column")

    def field(reference: dict[str, str]) -> dict[str, object]:
        return {"Column": {"Expression": {"SourceRef": reference}, "Property": column.name}}

    conditions: list[dict[str, object]] = []
    for kind, bound in [
        (2, selected.start),
        (3, selected.end + timedelta(days=1) if selected.end else None),
    ]:
        if bound:
            conditions.append(
                {
                    "Comparison": {
                        "ComparisonKind": kind,
                        "Left": field({"Source": "d"}),
                        "Right": {"Literal": {"Value": f"datetime'{bound.isoformat()}T00:00:00'"}},
                    }
                }
            )
    condition = (
        conditions[0]
        if len(conditions) == 1
        else {"And": {"Left": conditions[0], "Right": conditions[1]}}
    )
    return {
        "filters": [
            {
                "name": uuid.uuid5(uuid.NAMESPACE_URL, f"{visual_id}:date-filter").hex[:20],
                "field": field({"Entity": semantic_name}),
                "type": "Advanced",
                "howCreated": "User",
                "filter": {
                    "Version": 2,
                    "From": [{"Name": "d", "Entity": semantic_name, "Type": 0}],
                    "Where": [{"Condition": condition}],
                },
            }
        ]
    }
