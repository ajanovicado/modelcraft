"""Exact PBIR query-state fragments for the frozen visual recipe set."""

from __future__ import annotations

from ai_powerbi_analyst.powerbi.recipes import FieldBinding

ROLE_MAP: dict[str, dict[str, str]] = {
    "cardVisual": {"field": "Data", "value": "Data"},
    "lineChart": {"category": "Category", "value": "Y", "legend": "Series"},
    "clusteredBarChart": {
        "category": "Category",
        "value": "Y",
        "legend": "Series",
    },
    "pivotTable": {"row": "Rows", "value": "Values"},
    "slicer": {"field": "Values"},
    "textbox": {},
}


def build_query_state(
    visual_type: str,
    bindings: tuple[FieldBinding, ...],
) -> dict[str, object]:
    """Compile allowlisted semantic bindings into official visual data roles."""

    roles = ROLE_MAP.get(visual_type)
    if roles is None:
        raise ValueError(f"PBIR contract does not support visual type: {visual_type}")
    state: dict[str, object] = {}
    for binding in bindings:
        role = roles.get(binding.role)
        if role is None:
            raise ValueError(f"{visual_type} does not support role {binding.role}")
        role_state = state.setdefault(role, {"projections": []})
        assert isinstance(role_state, dict)
        projections = role_state["projections"]
        assert isinstance(projections, list)
        projections.append(_projection(binding))
    return state


def _projection(binding: FieldBinding) -> dict[str, object]:
    table, name = split_reference(binding.reference)
    kind = "Measure" if binding.kind == "measure" else "Column"
    return {
        "field": {
            kind: {
                "Expression": {"SourceRef": {"Entity": table}},
                "Property": name,
            }
        },
        "queryRef": f"{table}.{name}",
        "nativeQueryRef": name,
    }


def split_reference(reference: str) -> tuple[str, str]:
    if not reference.endswith("]") or "[" not in reference:
        raise ValueError(f"invalid Power BI reference: {reference}")
    table, name = reference[:-1].split("[", 1)
    if not table or not name:
        raise ValueError(f"invalid Power BI reference: {reference}")
    return table.strip("'"), name
