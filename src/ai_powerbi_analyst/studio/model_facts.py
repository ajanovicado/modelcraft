"""Studio's view of an imported model's facts (``pbix_extract.read_model_facts``).

An imported report keeps two things about its model:

- ``ImportedReport.model``: the facts of the head's model, bounded (``stored``). They are read at
  import and again whenever the head's model changes, so every reader (chat, DAX Lab, the local
  engine, the format catalog, the Format pane) describes the model as it is now.
- ``ImportedReport.snapshot_model``: a digest of the model the snapshot's rows were decoded from
  (``digest``): names with their lineage tags, calculated DAX, measure hashes, the relationships
  and when each partition was refreshed. Comparing it with the head says which calculated
  columns and tables the rows no longer describe, which columns were renamed, and whether Power
  BI Desktop refreshed the data since Studio read it.

This module derives the import-era rows from the facts (``schema_rows`` is ``report.tables``,
``relationship_rows`` is ``report.relationships``) and the plain-English notes the report
payload carries (``payload``). Pure: no I/O, no DAX parsing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

FACTS_VERSION = 1
DIGEST_VERSION = 1

# Power Query, role filters and format expressions as the report keeps them: enough for the UI
# and chat to show what a query does, not the base64 rows of an "Enter data" table.
_TEXT = 4_000
# Calculated DAX is kept longer: the local engine calculates from it.
_DAX_TEXT = 20_000
_LIMITS = {
    "tables": 1_000,
    "columns": 20_000,
    "measures": 5_000,
    "relationships": 2_000,
    "hierarchies": 1_000,
    "variations": 5_000,
    "roles": 200,
    "calculationGroups": 200,
    "fieldParameters": 500,
    "whatIfParameters": 500,
    "partitions": 2_000,
    "expressions": 1_000,
}
# pbixray's AMO_PANDAS_TYPE_MAPPING: the PandasDataType the import's schema rows carry.
_PANDAS_TYPES = {
    2: "string",
    6: "Int64",
    8: "Float64",
    9: "datetime64[ns]",
    10: "decimal.Decimal",
    11: "bool",
    17: "bytes",
}
# Partitions whose rows come from outside the model (a refresh changes them); calculated and
# calculation-group partitions are computed from the model itself.
_DATA_SOURCES = frozenset({"m", "query", "entity", "policyRange", "parquet", "inferred", "none"})

RLS_NOTE = (
    "Studio's canvas and answers use every row: row-level security roles apply only when you "
    "view the report as a role in Power BI."
)


def _items(facts: Mapping[str, Any] | None, section: str) -> list[dict[str, Any]]:
    value = facts.get(section) if facts is not None else None
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _fold(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _cut(item: dict[str, Any], key: str, limit: int) -> None:
    value = item.get(key)
    if isinstance(value, str) and len(value) > limit:
        item[key] = value[:limit]
        item["truncated"] = True


def stored(facts: Mapping[str, Any]) -> dict[str, Any]:
    """The facts as ``ImportedReport.model`` keeps them.

    Every section of ``read_model_facts`` is kept, except that a measure's DAX is left out (the
    report's ``measures`` rows carry it), a calculated table's partition drops the DAX its table
    already has, Power Query, role filters and format expressions are cut at 4,000 characters
    and calculated DAX at 20,000 (an item whose text was cut says ``"truncated": true``), and
    each list stops at its limit. ``truncated`` at the top lists every section that lost items
    or text, here or in the read itself; the full facts are ``head_model_facts``'s.
    """
    kept: dict[str, Any] = {"version": facts.get("version", FACTS_VERSION)}
    if isinstance(facts.get("model"), dict):
        kept["model"] = dict(facts["model"])
    cut = (
        {str(s) for s in facts.get("truncated", []) if isinstance(s, str)}
        if isinstance(facts.get("truncated"), list)
        else set()
    )
    for section, limit in _LIMITS.items():
        if not isinstance(facts.get(section), list):
            continue
        items = _items(facts, section)
        if len(items) > limit:
            items = items[:limit]
            cut.add(section)
        kept[section] = [_stored_item(section, item) for item in items]
        if any(_lost_text(item) for item in kept[section]):
            cut.add(section)
    if cut:
        kept["truncated"] = sorted(cut)
    return kept


def _lost_text(item: dict[str, Any]) -> bool:
    return item.get("truncated") is True or any(
        _lost_text(entry)
        for value in item.values()
        if isinstance(value, list)
        for entry in value
        if isinstance(entry, dict)
    )


def known(model: Mapping[str, Any] | None) -> bool:
    """Whether the facts describe the model at all: they have its tables and columns. A read
    that gave only ``{"version": 1}`` knows nothing (no calculation group, no field parameter
    can be ruled out)."""
    return (
        model is not None
        and isinstance(model.get("tables"), list)
        and isinstance(model.get("columns"), list)
    )


_STORAGE_MODES = ("import", "direct_query", "dual", "hybrid", "direct_lake", "unknown")
_MODE_NAMES = {
    "import": "import",
    "directquery": "direct_query",
    "dual": "dual",
    "directlake": "direct_lake",
    "default": "default",
}


def storage_mode(value: object) -> str:
    """A canonical TOM mode, retaining unknown modes rather than assuming imported rows."""
    if not isinstance(value, str):
        return "unknown"
    name = value.strip().casefold().replace("_", "").replace(" ", "").replace("-", "")
    return _MODE_NAMES.get(name, "unknown")


def table_storage_modes(model: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Storage eligibility, independent of cached row coverage and DAX feature support.

    Only an explicit known model default supplies an omitted/default partition mode. A missing
    partition list, unsupported mode, or truncated partition inventory cannot prove a table
    complete. Calculated tables are Import tables; their calculation may still need Power BI.
    """
    settings = model.get("model") if model is not None else None
    default = storage_mode(settings.get("defaultMode")) if isinstance(settings, dict) else "unknown"
    if default == "default":
        default = "unknown"
    cuts = model.get("truncated") if model is not None else None
    incomplete = isinstance(cuts, list) and "partitions" in cuts
    partitions: dict[str, list[dict[str, Any]]] = {}
    for partition in _items(model, "partitions"):
        partitions.setdefault(_fold(partition.get("table")), []).append(partition)
    found: list[dict[str, Any]] = []
    for table in _items(model, "tables"):
        name = table.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        own = partitions.get(_fold(name), [])
        modes: set[str] = set()
        for partition in own:
            raw = partition.get("mode")
            mode = storage_mode(raw)
            modes.add(default if raw is None or mode == "default" else mode)
        if not own and table.get("calculated") is True:
            modes.add("import")
        mode = "unknown"
        if not incomplete and modes and "unknown" not in modes:
            if modes == {"import", "direct_query"}:
                mode = "hybrid"
            elif len(modes) == 1:
                mode = next(iter(modes))
        found.append(
            {
                "name": name,
                "mode": mode,
                "partition_modes": sorted(modes) if own else [],
                "local_evaluation": (
                    "snapshot"
                    if mode == "import"
                    else "unknown"
                    if mode == "unknown"
                    else "power_bi"
                ),
            }
        )
    return found


def storage_summary(model: Mapping[str, Any] | None, *, has_model: bool = True) -> dict[str, Any]:
    """Sanitized storage facts for a report payload; no connection names, queries or secrets.

    Mixed storage is observable; source groups are not read, so this does not attempt to
    identify every composite model. Absence of a local model does not prove a live connection.
    """
    settings = model.get("model") if model is not None else None
    default = storage_mode(settings.get("defaultMode")) if isinstance(settings, dict) else "unknown"
    if default == "default":
        default = "unknown"
    tables = table_storage_modes(model) if has_model else []
    counts = {mode: sum(table["mode"] == mode for table in tables) for mode in _STORAGE_MODES}
    cuts = model.get("truncated") if model is not None else None
    incomplete = isinstance(cuts, list) and bool({"tables", "partitions"}.intersection(cuts))
    modes = {table["mode"] for table in tables}
    complete = known(model) and bool(tables) and not incomplete and "unknown" not in modes
    kind = next(iter(modes)) if complete and len(modes) == 1 else "mixed" if complete else "unknown"
    detail = (
        "Storage modes describe the model, not the availability or freshness of Studio's "
        "snapshot rows. Source groups are not inspected, so storage alone does not identify "
        "every composite model."
    )
    if not has_model:
        kind, complete, default = "no_local_model", True, "unknown"
        detail = (
            "This report has no embedded local model. It may use a remote model; its storage "
            "mode and live connection have not been verified. Use Power BI to view its values."
        )
    elif not complete:
        detail = (
            "Some table or partition storage metadata is missing, unsupported or truncated. "
            "Cached rows do not prove Import mode; Power BI must evaluate tables with unknown "
            "storage. Source groups are not inspected."
        )
    return {
        "known": complete,
        "kind": kind,
        "default_mode": default,
        "table_counts": counts,
        "tables": tables,
        "detail": detail,
    }


def power_bi_only(model: Mapping[str, Any] | None) -> dict[str, str] | None:
    """{table: "calculationGroup"|"fieldParameter"} for the tables whose visuals only Power BI
    draws; None while the facts are unknown, so a caller can refuse rather than assume there
    are none."""
    if not known(model):
        return None
    found = {str(g.get("table")): "calculationGroup" for g in _items(model, "calculationGroups")}
    found.update(
        {str(t.get("name")): "calculationGroup" for t in _items(model, "tables")
         if t.get("calculationGroup")}
    )  # fmt: skip
    found.update({str(p.get("table")): "fieldParameter" for p in _items(model, "fieldParameters")})
    return found


def _stored_item(section: str, item: dict[str, Any]) -> dict[str, Any]:
    copy: dict[str, Any] = json.loads(json.dumps(item))
    if section == "measures":
        copy.pop("expression", None)
        _cut(copy, "formatStringExpression", _TEXT)
    elif section in {"tables", "columns"}:
        _cut(copy, "expression", _DAX_TEXT)
    elif section == "partitions":
        if copy.get("source") == "calculated":
            copy["expression"] = None
        _cut(copy, "expression", _TEXT)
    elif section == "expressions":
        _cut(copy, "expression", _TEXT)
    elif section == "roles":
        for table in _items(copy, "tables"):
            _cut(table, "filter", _TEXT)
    elif section == "calculationGroups":
        for entry in _items(copy, "items") + _items(copy, "selectionExpressions"):
            _cut(entry, "expression", _DAX_TEXT)
            _cut(entry, "formatStringExpression", _TEXT)
    elif section == "whatIfParameters":
        _cut(copy, "expression", _DAX_TEXT)
    return copy


def schema_rows(facts: Mapping[str, Any] | None) -> list[dict[str, Any]] | None:
    """``report.tables`` for the model the facts describe: one ``{TableName, ColumnName,
    PandasDataType}`` row per column, as the import's schema has them. None when the facts
    have no columns section (unknown, not "no columns")."""
    if facts is None or not isinstance(facts.get("columns"), list):
        return None
    rows = []
    for column in _items(facts, "columns"):
        if not column.get("table") or not column.get("name"):
            continue
        code = column.get("dataType")
        rows.append(
            {
                "TableName": column["table"],
                "ColumnName": column["name"],
                "PandasDataType": _PANDAS_TYPES.get(code, "object")
                if isinstance(code, int)
                else "object",
            }
        )
    return rows


def relationship_rows(facts: Mapping[str, Any] | None) -> list[dict[str, Any]] | None:
    """``report.relationships`` for the model the facts describe, in the shape the import's
    metadata reader gives (``report_import.report_relationships`` reads it). Automatic cross
    filtering counts as single, the conservative reading for filter propagation."""
    if facts is None or not isinstance(facts.get("relationships"), list):
        return None
    rows = []
    for item in _items(facts, "relationships"):
        ends = [item.get(key) for key in ("fromTable", "fromColumn", "toTable", "toColumn")]
        if not all(isinstance(end, str) and end for end in ends):
            continue
        rows.append(
            {
                "FromTableName": item["fromTable"],
                "FromColumnName": item["fromColumn"],
                "ToTableName": item["toTable"],
                "ToColumnName": item["toColumn"],
                "IsActive": 1 if item.get("active") else 0,
                "Cardinality": str(item.get("cardinality") or "M:1"),
                "CrossFilteringBehavior": "Both" if item.get("crossFilter") == "both" else "Single",
                "RelyOnReferentialIntegrity": 1 if item.get("relyOnReferentialIntegrity") else 0,
                # "datePartOnly" joins a date/time column by its date alone (auto date/time).
                "JoinOnDateBehavior": item.get("joinOnDate"),
            }
        )
    return rows


def calculated(facts: Mapping[str, Any] | None) -> dict[str, str] | None:
    """``{"Table": dax}`` for calculated tables and ``{"Table[Column]": dax}`` for calculated
    columns, as ``pbix_extract.metadata_calculated`` gives them; None when unknown."""
    if facts is None or not (
        isinstance(facts.get("tables"), list) and isinstance(facts.get("columns"), list)
    ):
        return None
    found: dict[str, str] = {}
    for table in _items(facts, "tables"):
        if table.get("calculated") and isinstance(table.get("expression"), str):
            found[str(table["name"])] = table["expression"]
    for column in _items(facts, "columns"):
        if column.get("kind") == "calculated" and isinstance(column.get("expression"), str):
            found[f"{column['table']}[{column['name']}]"] = column["expression"]
    return found


def _hash(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def relationships_hash(rows: list[dict[str, Any]]) -> str:
    """One hash over the joins that decide filter propagation (names folded, order ignored)."""
    canonical = sorted(
        (
            _fold(r.get("FromTableName")),
            _fold(r.get("FromColumnName")),
            _fold(r.get("ToTableName")),
            _fold(r.get("ToColumnName")),
            str(r.get("Cardinality")),
            str(r.get("CrossFilteringBehavior")),
            int(bool(r.get("IsActive"))),
            # Only the non-default join changes the hash, so digests kept before joins carried
            # it still match their models.
            *(["datePartOnly"] if r.get("JoinOnDateBehavior") == "datePartOnly" else []),
        )
        for r in rows
    )
    return _hash(canonical)


def measure_hashes(measures: list[dict[str, Any]]) -> dict[str, str]:
    """``{folded measure name: hash of its DAX}`` over rows in either shape (``Name`` and
    ``Expression`` of the report's measures, or ``name`` and ``expression`` of the facts)."""
    found: dict[str, str] = {}
    for row in measures:
        name = row.get("Name", row.get("name"))
        expression = row.get("Expression", row.get("expression"))
        if isinstance(name, str) and name:
            found[_fold(name)] = _hash(expression if isinstance(expression, str) else None)
    return found


def digest(facts: Mapping[str, Any], model_key: str) -> dict[str, Any]:
    """What Studio keeps about the model a snapshot's rows were decoded from."""
    relationships = relationship_rows(facts)
    return {
        "v": DIGEST_VERSION,
        "model_key": model_key,
        "tables": [
            {
                "name": t.get("name"),
                "lineageTag": t.get("lineageTag"),
                "source": t.get("source"),
                "expression": t.get("expression") if t.get("calculated") else None,
            }
            for t in _items(facts, "tables")
        ],
        "columns": [
            {
                "table": c.get("table"),
                "name": c.get("name"),
                "lineageTag": c.get("lineageTag"),
                "kind": c.get("kind"),
                "sourceColumn": c.get("sourceColumn"),
                "expression": c.get("expression"),
                "dataType": c.get("dataType"),
            }
            for c in _items(facts, "columns")
        ],
        "measures": measure_hashes(_items(facts, "measures"))
        if isinstance(facts.get("measures"), list)
        else None,
        "relationships": relationships_hash(relationships) if relationships is not None else None,
        "partitions": [
            {
                "table": p.get("table"),
                "name": p.get("name"),
                "source": p.get("source"),
                "refreshedTime": p.get("refreshedTime"),
            }
            for p in _items(facts, "partitions")
        ],
    }


def engine_inputs(
    model: Mapping[str, Any] | None, snapshot: Mapping[str, Any] | None
) -> str | None:
    """A hash of what the local engine builds from besides the rows, measures, schema and joins:
    the names, lineage and calculated DAX of the head's tables and columns, its calculation
    groups and field parameters, and the snapshot's digest. None when both are unknown (the
    engine then works from the snapshot alone, as before facts were read)."""
    if model is None:
        # A digest alone (the head's read failed) still decides whether the engine may answer.
        return None if snapshot is None else _hash({"snapshot": dict(snapshot)})
    return _hash(
        {
            "tables": sorted(
                json.dumps(
                    [
                        t.get("name"),
                        t.get("lineageTag"),
                        t.get("source"),
                        t.get("expression"),
                        bool(t.get("calculationGroup")),
                    ]
                )
                for t in _items(model, "tables")
            ),
            "columns": sorted(
                json.dumps(
                    [
                        c.get("table"),
                        c.get("name"),
                        c.get("lineageTag"),
                        c.get("kind"),
                        c.get("sourceColumn"),
                        c.get("expression"),
                        c.get("dataType"),
                        c.get("error"),
                        c.get("state"),
                    ]
                )
                for c in _items(model, "columns")
            ),
            "measures": sorted(
                json.dumps([m.get("name"), m.get("error"), m.get("state")])
                for m in _items(model, "measures")
                if m.get("error") or m.get("state")
            ),
            "modes": sorted(
                json.dumps([p.get("table"), p.get("mode")]) for p in _items(model, "partitions")
            ),
            "storage": table_storage_modes(model),
            "settings": model.get("model") if isinstance(model.get("model"), dict) else None,
            "known": known(model),
            "groups": [
                [g.get("table"), len(_items(g, "selectionExpressions"))]
                for g in _items(model, "calculationGroups")
            ],
            "parameters": sorted(str(p.get("table")) for p in _items(model, "fieldParameters")),
            "snapshot": _hash(dict(snapshot)) if snapshot is not None else None,
        }
    )


def snapshot_table(
    snapshot: Mapping[str, Any] | None, table: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The snapshot digest's entry for a head table: by lineage tag (survives a rename), else by
    name when one side has no tag. Two different tags are two different tables: a table renamed
    away and a new one given its old name must not share the old rows."""
    entries = _items(snapshot, "tables")
    tag = table.get("lineageTag")
    if tag:
        for entry in entries:
            if entry.get("lineageTag") == tag:
                return entry
    return next(
        (
            e
            for e in entries
            if _fold(e.get("name")) == _fold(table.get("name"))
            and not (tag and e.get("lineageTag"))
        ),
        None,
    )


def _same_type(entry: Mapping[str, Any], column: Mapping[str, Any]) -> bool:
    """A column deleted and added again as another type is another column (rows of the old type
    would be read as the new)."""
    before, now = entry.get("dataType"), column.get("dataType")
    return before is None or now is None or before == now


def snapshot_column(
    snapshot: Mapping[str, Any] | None, table: Mapping[str, Any], column: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The snapshot digest's entry for a head column of ``table``: by lineage tag; else, when
    one side has no tag, by name in the same (possibly renamed) table, then for a data column by
    its source column. The data type must agree (a digest kept before types were known says
    nothing and agrees)."""
    entries = _items(snapshot, "columns")
    tag = column.get("lineageTag")
    if tag:
        for entry in entries:
            if entry.get("lineageTag") == tag:
                return entry if _same_type(entry, column) else None
    owner = snapshot_table(snapshot, table)
    if owner is None:
        return None
    own = [
        e
        for e in entries
        if _fold(e.get("table")) == _fold(owner.get("name"))
        and not (tag and e.get("lineageTag"))
        and _same_type(e, column)
    ]
    by_name = next((e for e in own if _fold(e.get("name")) == _fold(column.get("name"))), None)
    if by_name is not None or column.get("kind") != "data" or not column.get("sourceColumn"):
        return by_name
    same = [
        e
        for e in own
        if e.get("kind") == "data" and _fold(e.get("sourceColumn")) == _fold(column["sourceColumn"])
    ]
    return same[0] if len(same) == 1 else None


def data_status(
    model: Mapping[str, Any] | None, snapshot: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Whether Power BI Desktop changed the model's data since Studio decoded its rows:
    ``{"known", "stale", "tables", "detail"}``. A data table the rows lack, or one whose
    partitions were refreshed at another time, is stale; calculated tables are not (they are
    computed from the model)."""
    if model is None or snapshot is None or not isinstance(model.get("partitions"), list):
        return {"known": False, "stale": False, "tables": [], "detail": None}
    refreshed: dict[str, list[str | None]] = {}
    for entry in _items(snapshot, "partitions"):
        if entry.get("source") in _DATA_SOURCES:
            refreshed.setdefault(_fold(entry.get("table")), []).append(entry.get("refreshedTime"))
    head: dict[str, list[str | None]] = {}
    for partition in _items(model, "partitions"):
        if partition.get("source") in _DATA_SOURCES:
            head.setdefault(_fold(partition.get("table")), []).append(
                partition.get("refreshedTime")
            )
    stale: list[str] = []
    for table in _items(model, "tables"):
        name = str(table.get("name") or "")
        times = head.get(_fold(name))
        if not name or times is None:
            continue  # a calculated table, or a table without partitions
        source = snapshot_table(snapshot, table)
        before = refreshed.get(_fold(source.get("name"))) if source is not None else None
        if before is None or sorted(map(str, before)) != sorted(map(str, times)):
            stale.append(name)
    detail = None
    if stale:
        named = ", ".join(stale[:5]) + (f" and {len(stale) - 5} more" if len(stale) > 5 else "")
        detail = (
            f"Power BI Desktop refreshed or added {named} since Studio read the data. Studio's "
            "canvas and answers still use the rows it read then; read the data again to use "
            "the new rows."
        )
    return {"known": True, "stale": bool(stale), "tables": stale, "detail": detail}


def changed_calculations(
    model: Mapping[str, Any] | None, snapshot: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Calculated columns and tables whose DAX the snapshot's rows do not reflect (new, or
    changed since the rows were read): ``[{"table", "column"}]``, ``column`` None for a table.
    With the rows' model unknown (no digest) every calculated object is listed: none of their
    stored values can be trusted (the engine treats them the same way)."""
    if not known(model):
        return []
    tables = {_fold(t.get("name")): t for t in _items(model, "tables")}
    found: list[dict[str, Any]] = []
    for table in tables.values():
        if table.get("calculated"):
            entry = snapshot_table(snapshot, table) if snapshot is not None else None
            if entry is None or entry.get("expression") != table.get("expression"):
                found.append({"table": table.get("name"), "column": None})
    for column in _items(model, "columns"):
        owner = tables.get(_fold(column.get("table")))
        if column.get("kind") != "calculated" or owner is None:
            continue
        before = snapshot_column(snapshot, owner, column) if snapshot is not None else None
        if before is None or before.get("expression") != column.get("expression"):
            found.append({"table": column.get("table"), "column": column.get("name")})
    return found


def notes(
    model: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    *,
    rows: bool = True,
    reread: bool = True,
) -> dict[str, Any]:
    """What Studio's canvas does with the model's objects, in plain words (the payload's
    ``model.studio``). ``rows``: the report has imported rows; ``reread``: Studio can read them
    again (the route exists), which it offers when Desktop refreshed the data or when the
    rows' own model is unknown (reading them again records it)."""
    power_bi_only = [
        {
            "table": g.get("table"),
            "kind": "calculationGroup",
            "detail": f"Visuals that use the calculation group {g.get('table')} show in Power BI "
            "only; Studio's canvas does not apply calculation items.",
        }
        for g in _items(model, "calculationGroups")
    ] + [
        {
            "table": p.get("table"),
            "kind": "fieldParameter",
            "detail": f"Visuals that use the field parameter {p.get('table')} show in Power BI "
            "only; Studio's canvas does not switch fields.",
        }
        for p in _items(model, "fieldParameters")
    ]
    data = data_status(model, snapshot)
    offer = rows and reread and (bool(data["stale"]) or snapshot is None)
    settings = model.get("model") if model is not None else None
    return {
        "rowLevelSecurity": RLS_NOTE if _items(model, "roles") else None,
        "autoDateTime": settings.get("autoDateTime") if isinstance(settings, dict) else None,
        "powerBiOnly": power_bi_only,
        "changedCalculations": changed_calculations(model, snapshot),
        "data": data,
        "actions": ["reread_data"] if offer else [],
    }


def payload(
    model: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    *,
    status: str,
    rows: bool = True,
    reread: bool = True,
) -> dict[str, Any]:
    """The report payload's ``model``: ``status`` ("current": the facts describe the head;
    "reading": Studio is reading them; "unknown": they could not be read, or say nothing of
    the model's tables and columns; "none": the report has no model of its own), the stored
    facts' sections when current, and ``studio`` (``notes``)."""
    if status == "current" and not known(model):
        status = "unknown"
    found: dict[str, Any] = {"status": status}
    if model is not None and status == "current":
        found.update({k: v for k, v in model.items() if k != "status"})
        found["studio"] = notes(model, snapshot, rows=rows, reread=reread)
    found["storage"] = storage_summary(
        model if status == "current" else None, has_model=status != "none"
    )
    return found
