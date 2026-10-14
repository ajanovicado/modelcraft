"""Ground closed DAX proposal choices in current imported metadata, without reading rows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from . import model_facts
from .dax_patterns import PatternCatalog, PatternError, PatternField, PatternRelationship, _dtype
from .dax_syntax import (
    Binary,
    Call,
    ColRef,
    Name,
    Node,
    TableCtor,
    Unary,
    VarBlock,
    parse_expression,
)

if TYPE_CHECKING:
    from .report_import import ImportedReport

_NATIVE = frozenset({"direct_query", "dual", "hybrid", "direct_lake"})
_TYPES = {2: "string", 6: "int64", 8: "double", 9: "date", 10: "decimal", 11: "boolean"}


def _items(facts: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = facts.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _name(value: object) -> str:
    return value if isinstance(value, str) else ""


def catalog_for_native_proposal(
    report: ImportedReport,
    *,
    local: PatternCatalog | None = None,
    revision: str | None = None,
) -> PatternCatalog:
    """Extend a local catalog only with recognized native tables and grounded measures.

    `report` must be the caller's verified current head. No snapshot/model is opened here.
    `available` means usable as a closed proposal slot, not locally executable. Preserve
    this distinction by returning names-only receipts and evaluating in Power BI later.
    """
    facts = report.model
    if not model_facts.known(facts) or facts is None or facts.get("status", "current") != "current":
        raise PatternError("Review the current model metadata before drafting this calculation.")
    cuts = {cut for cut in facts.get("truncated") or [] if isinstance(cut, str)}
    if cuts.intersection(
        {"tables", "columns", "measures", "partitions", "calculationGroups", "fieldParameters"}
    ):
        raise PatternError(
            "The current model metadata is truncated; review it before drafting DAX."
        )
    if not all(
        isinstance(facts.get(key), list) and len(facts[key]) == len(_items(facts, key))
        for key in ("tables", "columns", "partitions", "measures")
    ):
        raise PatternError("The current schema or partition inventory is not complete.")
    if local is not None and revision is not None and local.revision not in {None, revision}:
        raise PatternError("The local catalog describes a different model revision.")
    tables = _items(facts, "tables")
    by_table = {str(table.get("name", "")).casefold(): table for table in tables}
    if len(by_table) != len(tables) or any(
        not _name(table.get("name")).strip() for table in tables
    ):
        raise PatternError("Review missing or duplicate table identities in the current model.")
    columns = _items(facts, "columns")
    by_column = {
        (_name(column.get("table")).casefold(), _name(column.get("name")).casefold()): column
        for column in columns
    }
    if len(by_column) != len(columns) or any(
        not key[0] or not key[1] or key[0] not in by_table for key in by_column
    ):
        raise PatternError("Review missing or duplicate column identities in the current model.")
    modes = {
        table["name"].casefold(): table["mode"] for table in model_facts.table_storage_modes(facts)
    }
    special = model_facts.power_bi_only(facts) or {}
    blocked_tables = {table.casefold() for table in special}
    blocked_tables.update(
        table_key
        for table_key, table in by_table.items()
        if table.get("truncated") or table.get("error") or table.get("state")
    )
    blocked_tables.update(
        _name(partition.get("table")).casefold()
        for partition in _items(facts, "partitions")
        if partition.get("truncated") or partition.get("error") or partition.get("state")
    )
    native = {
        table for table, mode in modes.items() if mode in _NATIVE and table not in blocked_tables
    }
    fields = list(local.fields if local is not None else ())
    positions: dict[tuple[str, str, str], int] = {
        (field.kind, field.table.casefold(), field.name.casefold()): at
        for at, field in enumerate(fields)
    }
    next_index = max((field.index for field in fields), default=0) + 1
    columns_by_table: dict[str, list[dict[str, Any]]] = {}
    for (column_table, _), column in by_column.items():
        columns_by_table.setdefault(column_table, []).append(column)

    def put(item: PatternField) -> None:
        nonlocal next_index
        identity = item.kind, item.table.casefold(), item.name.casefold()
        match = positions.get(identity)
        if match is None:
            positions[identity] = len(fields)
            fields.append(replace(item, index=next_index))
            next_index += 1
        else:
            fields[match] = replace(item, index=fields[match].index)

    for table_key, table in by_table.items():
        # Unknown/Import metadata cannot revive unavailable or missing snapshot choices.
        if table_key not in native:
            continue
        table_name = table["name"]
        put(PatternField(0, "table", table_name, table_name, source_tables=(table_name,)))
        for column in columns_by_table.get(table_key, ()):
            code = column.get("dataType")
            raw_type = column.get("dataTypeName") or (
                _TYPES.get(code) if isinstance(code, int) else None
            )
            data_type = _dtype(raw_type) if isinstance(raw_type, str) else "unknown"
            problem = bool(
                column.get("truncated")
                or column.get("error")
                or column.get("state")
                or data_type == "unknown"
            )
            put(
                PatternField(
                    0,
                    "column",
                    table_name,
                    column["name"],
                    data_type,
                    (table_name,),
                    available=not problem,
                    unavailable_reason="Review this column's complete type and metadata first."
                    if problem
                    else "",
                    date_key=not problem
                    and data_type == "date"
                    and table.get("isDateTable") is True
                    and _name(table.get("dateColumn")).casefold() == column["name"].casefold(),
                    calendar_contiguous=None,
                )
            )

    def field_for(kind: str, table: str, name: str) -> PatternField | None:
        at = positions.get((kind, table, name))
        return fields[at] if at is not None else None

    measure_rows = [measure for measure in report.measures if _name(measure.get("Name")).strip()]
    measures = {_name(measure.get("Name")).casefold(): measure for measure in measure_rows}
    if len(measures) != len(measure_rows):
        raise PatternError("Review duplicate measure identities in the current model.")
    measure_facts = {
        _name(measure.get("name")).casefold(): measure for measure in _items(facts, "measures")
    }
    dependencies: dict[str, tuple[str, ...]] = {}

    def measure_sources(key: str, seen: frozenset[str] = frozenset()) -> tuple[str, ...]:
        if key in seen or len(seen) >= 32:
            raise PatternError(
                "Review circular or excessively deep measure dependencies before drafting DAX."
            )
        if key in dependencies:
            return dependencies[key]
        measure = measures[key]
        metadata = measure_facts.get(key)
        if (
            metadata is None
            or _name(metadata.get("table")).casefold() != _name(measure.get("TableName")).casefold()
        ):
            raise PatternError("This measure's current metadata is not confirmed.")
        if metadata.get("truncated") or metadata.get("error") or metadata.get("state"):
            raise PatternError("Review this measure's current metadata before using it.")
        expression = measure.get("Expression")
        if not isinstance(expression, str) or not expression.strip():
            raise PatternError("This measure's complete expression is not available.")
        try:
            node = parse_expression(expression)
        except (ValueError, RecursionError) as error:
            raise PatternError("This measure's dependencies could not be checked.") from error
        found: set[str] = set()
        home = _name(measure.get("TableName")).casefold()

        def table_reference(table: str) -> None:
            item = field_for("table", table, table)
            if item is None or not item.available:
                raise PatternError(
                    "This measure reads a table without confirmed proposal metadata."
                )
            found.add(item.table)

        def walk(value: Node, variables: frozenset[str] = frozenset()) -> None:
            if isinstance(value, ColRef):
                name = value.column.casefold()
                table = value.table.casefold() if value.table else home
                nested = measures.get(name)
                if nested is not None and (
                    value.table is None or table == _name(nested.get("TableName")).casefold()
                ):
                    found.update(measure_sources(name, seen | {key}))
                    return
                item = field_for("column", table, name)
                if item is None or not item.available:
                    raise PatternError(
                        "This measure reads a missing, unsupported or unreviewed column."
                    )
                found.add(item.table)
            elif isinstance(value, Name):
                if value.name.casefold() not in variables:
                    table_reference(value.name.casefold())
            elif isinstance(value, Call):
                for argument in value.args:
                    walk(argument, variables)
            elif isinstance(value, Binary):
                walk(value.left, variables)
                walk(value.right, variables)
            elif isinstance(value, Unary):
                walk(value.operand, variables)
            elif isinstance(value, TableCtor):
                for row in value.rows:
                    for element in row:
                        walk(element, variables)
            elif isinstance(value, VarBlock):
                scoped = variables
                for variable, expression in value.variables:
                    walk(expression, scoped)
                    scoped |= {variable.casefold()}
                walk(value.body, scoped)

        walk(node)
        dependencies[key] = tuple(sorted(found, key=str.casefold))
        return dependencies[key]

    for key, measure in measures.items():
        home_name = _name(measure.get("TableName"))
        existing = field_for("measure", home_name.casefold(), key)
        try:
            sources = measure_sources(key)
            eligible = home_name.casefold() in native or any(
                source.casefold() in native for source in sources
            )
            if not eligible:
                continue
            put(
                PatternField(
                    0,
                    "measure",
                    home_name,
                    measure["Name"],
                    source_tables=sources,
                    format_string=_name(measure.get("FormatString")),
                )
            )
        except PatternError as error:
            # Keep local refusal evidence; never lift an unrelated calculation failure.
            if existing is None:
                put(
                    PatternField(
                        0,
                        "measure",
                        home_name,
                        measure["Name"],
                        available=False,
                        unavailable_reason=str(error),
                    )
                )

    joins: list[PatternRelationship] = []
    relationships = facts.get("relationships")
    complete = (
        isinstance(relationships, list)
        and len(relationships) == len(_items(facts, "relationships"))
        and "relationships" not in cuts
    )
    for relation in _items(facts, "relationships"):
        if relation.get("active") is False:
            continue
        shape, direction = relation.get("cardinality"), relation.get("crossFilter")
        ends = [
            (
                _name(relation.get(f"{side}Table")).casefold(),
                _name(relation.get(f"{side}Column")).casefold(),
            )
            for side in ("from", "to")
        ]
        valid = (
            relation.get("active") is True
            and shape in {"M:1", "1:M"}
            and direction in {"single", "automatic"}
        )
        valid = valid and not (
            relation.get("truncated")
            or relation.get("error")
            or relation.get("state")
            or relation.get("joinOnDate") not in {None, "dateAndTime"}
        )
        valid = valid and all(
            (field := field_for("column", table, column)) is not None and field.available
            for table, column in ends
        )
        if not valid:
            complete = False
            continue
        if shape == "1:M":
            ends.reverse()
        joins.append(
            PatternRelationship(by_table[ends[0][0]]["name"], by_table[ends[1][0]]["name"])
        )
    return PatternCatalog(
        tuple(fields),
        tuple(joins),
        complete,
        revision or report.revision_hash or report.source_hash,
    )
