"""Whole-record prompt compaction; never truncate an ID or a JSON document."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any

# Optional lists, least useful first; each loses its least relevant items.
_DROPPABLE = (
    "approved_definitions",
    "approved_relationships",
    "existing_measures",
    "other_measures",
    "visuals",
    "definitions",
    "measures",
)
# Last resort, in order: what the model can best do without. Tables go down to the first,
# then these, then any other key but "tables".
_SHED = (
    "join_paths",
    "join_legend",
    "not_joined",
    "builder_capabilities",
    "memory_policy",
    "joins",
    "other_tables",
    "note",
    "context_notice",
    "report",
    "joins_note",
    "model",
    "data_dates",
)
# Draft measures the model summary already names: "Total Sales = SUM(Orders[Sales])".
_PLAIN_AGGREGATION = re.compile(
    r"^[^=]{1,160}=\s*(?:SUM|AVERAGE|MIN|MAX|COUNT|COUNTA|COUNTROWS|DISTINCTCOUNT)\s*\([^()]*\)$",
    re.I,
)
_QUESTION_NOISE = frozenset(
    {"the", "what", "which", "how", "and", "for", "are", "was", "were", "this", "that", "with"}
    | {"does", "did", "is", "of", "to", "in", "a", "an", "by", "me", "show", "give"}
)


def compact_workspace_context(
    payload: dict[str, Any], query: str = "", *, max_characters: int = 5000
) -> str:
    """The payload as JSON within ``max_characters``, trimming the least relevant parts.

    Fields go before tables: once every column or field that can go has gone, each
    table keeps its name (an imported report's question that named a measure used to
    empty the list to ``tables: []``, and the model denied that FactSales existed). Only
    then do the last tables become one "other_tables" line: the result never exceeds
    ``max_characters`` (a wide model's 4,769-character "2,200" profile outlasted the
    CPU's 90 s budget) and never lists no table at all. A budget too small for even the
    first table's name raises ``ValueError``.
    """
    data = deepcopy(payload)

    def encoded() -> str:
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    def over() -> bool:
        return len(encoded()) > max_characters

    if not over():
        return encoded()
    data["context_notice"] = (
        "Partial workspace context: some metadata/fields omitted for the local model budget. "
        "Do not invent missing fields; ask the user to select a table or narrow the question."
    )
    for table in data.get("tables", []):
        for column in table.get("columns", []):
            for key in ("samples", "nulls", "distinct"):
                column.pop(key, None)
    # Drop least relevant complete records first, keeping exact catalog IDs intact.
    tokens = set(re.findall(r"[\w]+", query.casefold()))
    for key in _DROPPABLE:
        values = data.get(key, [])
        while isinstance(values, list) and values and over():
            _pop_least_relevant(values, tokens)
    tables = [table for table in data.get("tables", []) if isinstance(table, dict)]
    while over():
        candidates = [
            (len(tokens.intersection(re.findall(r"[\w]+", column["name"].casefold()))), ti, ci)
            for ti, table in enumerate(tables)
            for ci, column in enumerate(table.get("columns", []))
        ]
        if candidates:
            _, ti, ci = min(candidates)
            tables[ti]["columns"].pop(ci)
            continue
        if not _trim_fields(tables, tokens):
            break  # every table is down to its name
    if over():
        _hard_cap(data, over)
    if over():
        raise ValueError("Workspace metadata exceeds the context budget")
    return encoded()


def _hard_cap(data: dict[str, Any], over: Callable[[], bool]) -> None:
    """Row counts, then the last tables (the fact comes first) as one names line, then
    optional keys, then the first table's field list. The first table always stays."""
    tables = data.get("tables")
    if not isinstance(tables, list):
        tables = []
    for table in tables:
        if isinstance(table, dict):
            table.pop("rows", None)
            for key in ("fields", "columns"):
                if table.get(key) in ("", []):
                    table.pop(key)
    dropped: list[str] = []

    def others(kept: int) -> str:
        more = len(dropped) - kept
        if not kept:
            return f"{more} more tables"
        return ", ".join(dropped[:kept]) + (f" … (+{more} more)" if more else "")

    while len(tables) > 1 and over():
        table = tables.pop()
        name = (table.get("table") or table.get("name")) if isinstance(table, dict) else None
        dropped.insert(0, str(name or "table"))
        data["other_tables"] = others(len(dropped))
    kept = len(dropped)
    while dropped and kept > 0 and over():
        kept -= 1
        data["other_tables"] = others(kept)
    for key in [*_SHED, *(key for key in list(data) if key not in _SHED and key != "tables")]:
        if not over():
            return
        value = data.get(key)
        # Joins go one by one, from the least central (the end of the list).
        while key == "joins" and isinstance(value, list) and len(value) > 1 and over():
            value.pop()
        if over():
            data.pop(key, None)
    for table in tables:
        # Last: the first table keeps its name and nothing else.
        if isinstance(table, dict) and over():
            for key in [key for key in table if key not in {"table", "name", "table_id"}]:
                table.pop(key)


def _pop_least_relevant(values: list[Any], tokens: set[str]) -> None:
    """Drop the item sharing fewest words with the question; among those, a plain
    aggregation the model summary already names before a lab formula, and a later
    item before an earlier one."""
    asked = {token for token in tokens if token not in _QUESTION_NOISE}

    def rank(index: int) -> tuple[int, bool, int]:
        item = values[index]
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        overlap = len(asked.intersection(re.findall(r"[\w]+", text.casefold())))
        return (overlap, not _PLAIN_AGGREGATION.match(text.strip()), -index)

    values.pop(min(range(len(values)), key=rank))


def _trim_fields(tables: list[dict[str, Any]], tokens: set[str]) -> bool:
    """Drop one field from a names-only list: the last table's first, question words last."""
    for prefer_unasked in (True, False):
        for table in reversed(tables):
            fields = table.get("fields")
            if not isinstance(fields, str) or not fields:
                continue
            items = [item for item in fields.split(", ") if item and item != "…"]
            for index in range(len(items) - 1, -1, -1):
                asked = tokens.intersection(re.findall(r"[\w]+", items[index].casefold()))
                if prefer_unasked and asked:
                    continue
                items.pop(index)
                table["fields"] = ", ".join([*items, "…"]) if items else ""
                return True
    return False
