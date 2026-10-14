"""Studio's model-change runner: typed changes applied to the model Power BI Desktop has open.

It runs under pbi-cli's own Python, the environment that ships pythonnet and Microsoft's Analysis
Services client libraries (Tabular Object Model and ADOMD.NET), and uses pbi-cli only to load those
libraries. Studio's side of it is ``ai_powerbi_analyst.studio.desktop_model``, which builds the
request, pins this environment by hash and reads the answer. Nothing here imports Studio: the two
share only the protocol below.

Protocol (version 1). One JSON request on stdin; JSON lines on stdout, the last one the result.

    request = {"version": 1, "action": "snapshot" | "apply" | "query",
               "server": "localhost:<port>",
               "queries": [{"id": "...", "query": "EVALUATE ..."}],   # query only (read only)
               "role": "...",                       # query only: see the model as this role
               "changes": [change, ...],            # apply only
               "recalculate": true,                 # RequestRefresh(Calculate) with the changes
               "refresh": [{"table": "T"} | {"model": true}],   # data refresh (Full) afterwards
               "on_refresh_failure": "rollback" | "keep",
               "probes": [{"id": "...", "query": "EVALUATE ...",
                           "role": "...", "informational": false}],
               "tolerant": false}                   # skip a change whose target is gone or exists
    change  = {"id": "c1", "kind": <kind>, "action": "create" | "update" | "rename" | "delete"
               | "restore", "path": {...}, "props": {...}, "newName": "...", "tmsl": "...",
               "expect": {...}}                     # a delete: only while the object holds this

    events: {"event": "connected", "database": ..., "compatibilityLevel": ...}
            {"event": "applied", "id": ..., "undo": [change, ...], "before": {...} | null}
            {"event": "saved"}                      # the changes are in Desktop's model now
            {"event": "result", "ok": bool, "stage": ..., "error": ..., "results": [...],
             "problems": [...], "refresh": {...} | null, "probes": [...], "rolled_back": bool,
             "rollback_errors": [...], "kept_without_refresh": bool, "checked_states": bool,
             "warnings": [...], "snapshot": {...}}

A probe with a role or marked informational only reports its first rows; it never undoes the
run. The undo of a create carries ``expect`` (what the created object holds), so replaying it
after a run that never saved cannot delete the object that had the name before. A role's members
are never read, printed or restored.

Kinds: measure, calculatedColumn (create), column, calculatedTable (create), table, dateTable
(update), relationship, hierarchy, fieldParameter, calculationGroup, calculationItem, role,
partition (update), expression, model (update). ``restore`` puts back an object deleted in the same
run from its TMSL (JsonSerializer), which re-attaches its references by name (checked offline on
the pinned libraries, 2026-09-29: a hierarchy's levels, a sort-by column and a relationship's ends
all resolved to the live objects).

What a run guarantees:
- Every change is applied locally, then ONE ``SaveChanges`` sends them with the recalculation that
  calculated columns and tables need (without it Desktop shows them blank until a refresh). A
  failure before that save is undone with ``UndoLocalChanges``: nothing reached the model.
- After the save, objects the run broke (a measure, column, partition, role filter or calculation
  item newly in an error state, read from the engine's own schema views) or a probe that fails
  undo the run with the inverse of every change, applied the same way (compensation). Objects
  that were already broken before the run are not blamed on it.
- A data refresh is a second step: when it fails, ``on_refresh_failure`` says whether the
  changes are kept ("keep") or undone ("rollback").
- The inverse of each change (``undo``) is printed before the save, so the caller can still
  compensate when this process never answers.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import sys
import uuid
from collections.abc import Callable, Mapping
from typing import Any

PROTOCOL = 1
# ObjectState as the engine's schema views report it (the TOM enum's values, read from the pinned
# Microsoft.AnalysisServices.Tabular 19.84.1.0 on 2026-09-29).
STATE_NAMES = {
    1: "Ready",
    3: "NoData",
    4: "CalculationNeeded",
    5: "SemanticError",
    6: "EvaluationError",
    7: "DependencyError",
    8: "Incomplete",
    10: "ForceCalculationNeeded",
}
ERROR_STATES = frozenset({"SemanticError", "EvaluationError", "DependencyError"})
# Fixed schema queries (no caller text): every object that holds DAX and can break.
_SCHEMA_QUERIES = {
    "tables": "SELECT * FROM $SYSTEM.TMSCHEMA_TABLES",
    "measures": "SELECT * FROM $SYSTEM.TMSCHEMA_MEASURES",
    "columns": "SELECT * FROM $SYSTEM.TMSCHEMA_COLUMNS",
    "partitions": "SELECT * FROM $SYSTEM.TMSCHEMA_PARTITIONS",
    "roles": "SELECT * FROM $SYSTEM.TMSCHEMA_ROLES",
    "permissions": "SELECT * FROM $SYSTEM.TMSCHEMA_TABLE_PERMISSIONS",
    "groups": "SELECT * FROM $SYSTEM.TMSCHEMA_CALCULATION_GROUPS",
    "items": "SELECT * FROM $SYSTEM.TMSCHEMA_CALCULATION_ITEMS",
}

Emit = Callable[[dict[str, Any]], None]
# Runs a DAX query or a schema query and returns its rows (column name -> value).
Rows = Callable[[str], list[dict[str, Any]]]


class ChangeError(Exception):
    """A change that cannot be applied as asked; the message is for a Power BI user."""


class Missing(ChangeError):
    """The object a change names is not in the model."""


class Exists(ChangeError):
    """The object a change would create is already in the model."""


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - a .NET value that cannot be shown is shown as empty
        return ""


def _message(error: BaseException) -> str:
    """One readable line from a .NET or Python exception."""
    text = " ".join(_text(error).split())
    return text[:600] or type(error).__name__


def _flat(text: Any) -> str:
    return " ".join(_text(text).split())


def _without_members(text: str) -> str:
    """A role's TMSL without its members: the people a role names are never printed (the undo
    lines reach Studio) and a restored role gets none back (members belong to the service)."""
    try:
        shape = json.loads(text)
    except ValueError:
        return text
    if isinstance(shape, dict) and "members" in shape:
        shape.pop("members")
        return json.dumps(shape)
    return text


class Runner:
    """Applies one request to ``model`` (a TOM Model). ``tom`` is the TOM namespace (or a fake with
    the same names), ``rows`` runs a query against the same model, ``emit`` prints an event."""

    def __init__(
        self,
        tom: Any,
        model: Any,
        rows: Rows | None,
        emit: Emit,
        rows_as: Callable[[str], Rows] | None = None,
    ) -> None:
        self.tom = tom
        self.model = model
        self.rows = rows
        self.emit = emit
        # Queries as a role (a second connection with Roles=<name>); None where not available.
        self.rows_as = rows_as
        self._actions: dict[tuple[str, str], Callable[[dict[str, Any]], tuple[list[Any], Any]]] = {
            ("measure", "create"): self._measure_create,
            ("measure", "update"): self._measure_update,
            ("measure", "rename"): self._measure_rename,
            ("measure", "delete"): self._measure_delete,
            ("calculatedColumn", "create"): self._calculated_column_create,
            ("column", "update"): self._column_update,
            ("column", "rename"): self._column_rename,
            ("column", "delete"): self._column_delete,
            ("calculatedTable", "create"): self._calculated_table_create,
            ("table", "update"): self._table_update,
            ("table", "rename"): self._table_rename,
            ("table", "delete"): self._table_delete,
            ("dateTable", "update"): self._date_table_update,
            ("relationship", "create"): self._relationship_create,
            ("relationship", "update"): self._relationship_update,
            ("relationship", "delete"): self._relationship_delete,
            ("hierarchy", "create"): self._hierarchy_create,
            ("hierarchy", "update"): self._hierarchy_update,
            ("hierarchy", "rename"): self._hierarchy_rename,
            ("hierarchy", "delete"): self._hierarchy_delete,
            ("fieldParameter", "create"): self._field_parameter_create,
            ("fieldParameter", "update"): self._field_parameter_update,
            ("calculationGroup", "create"): self._calculation_group_create,
            ("calculationGroup", "update"): self._calculation_group_update,
            ("calculationItem", "create"): self._calculation_item_create,
            ("calculationItem", "update"): self._calculation_item_update,
            ("calculationItem", "rename"): self._calculation_item_rename,
            ("calculationItem", "delete"): self._calculation_item_delete,
            ("role", "create"): self._role_create,
            ("role", "update"): self._role_update,
            ("role", "rename"): self._role_rename,
            ("role", "delete"): self._role_delete,
            ("partition", "update"): self._partition_update,
            ("expression", "create"): self._expression_create,
            ("expression", "update"): self._expression_update,
            ("expression", "delete"): self._expression_delete,
            ("model", "update"): self._model_update,
        }

    # --- TOM helpers -------------------------------------------------------------------------

    def _enum(self, enum: str, member: str) -> Any:
        return getattr(getattr(self.tom, enum), member)

    def _new(self, cls: str) -> Any:
        return getattr(self.tom, cls)()

    def _tmsl(self, obj: Any) -> str:
        text = _text(self.tom.JsonSerializer.SerializeObject(obj))
        return _without_members(text)

    def _from_tmsl(self, cls: str, text: str) -> Any:
        return self.tom.JsonSerializer.DeserializeObject[getattr(self.tom, cls)](
            _without_members(text)
        )

    @staticmethod
    def _other(found: Any, current: Any) -> bool:
        """True when ``found`` (a Find by the new name) is another object than ``current``. TOM's
        Find ignores letter case, so a rename that only changes case finds the object itself."""
        return found is not None and _text(found.Name).casefold() != _text(current.Name).casefold()

    def _annotations(self, owner: Any, values: Mapping[str, Any]) -> dict[str, Any]:
        """Set annotations (None removes one); returns what they were (None: absent)."""
        previous: dict[str, Any] = {}
        for name, value in values.items():
            existing = owner.Annotations.Find(_text(name))
            previous[_text(name)] = _text(existing.Value) if existing is not None else None
            if value is None:
                if existing is not None:
                    owner.Annotations.Remove(existing)
                continue
            if existing is None:
                existing = self._new("Annotation")
                existing.Name = _text(name)
                owner.Annotations.Add(existing)
            existing.Value = _text(value)
        return previous

    def _table(self, name: Any) -> Any:
        table = self.model.Tables.Find(_text(name))
        if table is None:
            raise Missing(f"The model has no table '{_text(name)}'")
        return table

    def _column(self, table: Any, name: Any) -> Any:
        column = table.Columns.Find(_text(name))
        if column is None:
            raise Missing(f"The table '{_text(table.Name)}' has no column '{_text(name)}'")
        return column

    def _measure(self, path: Mapping[str, Any]) -> tuple[Any, Any]:
        wanted = _text(path.get("name"))
        tables = [self._table(path["table"])] if path.get("table") else list(self.model.Tables)
        for table in tables:
            found = table.Measures.Find(wanted)
            if found is not None:
                return table, found
        raise Missing(f"The model has no measure [{wanted}]")

    def _relationship(self, path: Mapping[str, Any]) -> Any:
        ends = (
            (_text(path.get("fromTable")), _text(path.get("fromColumn"))),
            (_text(path.get("toTable")), _text(path.get("toColumn"))),
        )
        for relationship in self.model.Relationships:
            source, target = relationship.FromColumn, relationship.ToColumn
            if source is None or target is None:
                continue
            found = (
                (_text(relationship.FromTable.Name), _text(source.Name)),
                (_text(relationship.ToTable.Name), _text(target.Name)),
            )
            if found == ends or found == (ends[1], ends[0]):
                return relationship
        raise Missing(
            f"There is no relationship between {ends[0][0]}[{ends[0][1]}] and "
            f"{ends[1][0]}[{ends[1][1]}]"
        )

    def _hierarchy(self, path: Mapping[str, Any]) -> tuple[Any, Any]:
        table = self._table(path.get("table"))
        found = table.Hierarchies.Find(_text(path.get("name")))
        if found is None:
            raise Missing(
                f"The table '{_text(table.Name)}' has no hierarchy '{_text(path.get('name'))}'"
            )
        return table, found

    def _group(self, name: Any) -> Any:
        table = self._table(name)
        if table.CalculationGroup is None:
            raise ChangeError(f"The table '{_text(name)}' is not a calculation group")
        return table

    def _item(self, path: Mapping[str, Any]) -> tuple[Any, Any]:
        group = self._group(path.get("group"))
        item = group.CalculationGroup.CalculationItems.Find(_text(path.get("name")))
        if item is None:
            wanted = _text(path.get("name"))
            raise Missing(f"The calculation group '{_text(group.Name)}' has no item '{wanted}'")
        return group, item

    def _role(self, name: Any) -> Any:
        role = self.model.Roles.Find(_text(name))
        if role is None:
            raise Missing(f"The model has no role '{_text(name)}'")
        return role

    def role_exists(self, name: str) -> None:
        """Raises Missing when the model has no role of that name."""
        self._role(name)

    def _expression(self, name: Any) -> Any:
        found = self.model.Expressions.Find(_text(name))
        if found is None:
            raise Missing(f"The model has no query or parameter '{_text(name)}'")
        return found

    def _partition(self, path: Mapping[str, Any]) -> tuple[Any, Any]:
        table = self._table(path.get("table"))
        partitions = list(table.Partitions)
        if path.get("name"):
            partitions = [p for p in partitions if _text(p.Name) == _text(path["name"])]
        if len(partitions) != 1:
            raise ChangeError(
                f"The table '{_text(table.Name)}' has {len(partitions)} partitions; Studio edits "
                "tables that load from one query"
            )
        return table, partitions[0]

    # --- describing objects (before-images, snapshots) ------------------------------------------

    @staticmethod
    def _source_type(partition: Any) -> str:
        source = partition.Source
        kind = _text(partition.SourceType)
        if kind:
            return kind
        return type(source).__name__ if source is not None else "None"

    def _describe_column(self, column: Any) -> dict[str, Any]:
        sort_by = column.SortByColumn
        kind = _text(column.Type)
        described: dict[str, Any] = {
            "name": _text(column.Name),
            "type": kind,
            "dataType": _text(column.DataType),
            "formatString": _text(column.FormatString),
            "sortByColumn": _text(sort_by.Name) if sort_by is not None else "",
            "dataCategory": _text(column.DataCategory),
            "summarizeBy": _text(column.SummarizeBy),
            "displayFolder": _text(column.DisplayFolder),
            "description": _text(column.Description),
            "isHidden": bool(column.IsHidden),
            "isKey": bool(column.IsKey),
            "extendedProperties": [_text(p.Name) for p in column.ExtendedProperties],
        }
        if kind == "Calculated":
            described["expression"] = _text(column.Expression)
        return described

    @staticmethod
    def _format_expression(owner: Any) -> str:
        definition = owner.FormatStringDefinition
        return _text(definition.Expression) if definition is not None else ""

    @staticmethod
    def _describe_kpi(measure: Any) -> dict[str, Any] | None:
        kpi = measure.KPI
        if kpi is None:
            return None
        return {
            "targetExpression": _text(kpi.TargetExpression),
            "targetFormatString": _text(kpi.TargetFormatString),
            "statusExpression": _text(kpi.StatusExpression),
            "statusGraphic": _text(kpi.StatusGraphic),
            "trendExpression": _text(kpi.TrendExpression),
            "trendGraphic": _text(kpi.TrendGraphic),
        }

    def _describe_measure(self, measure: Any) -> dict[str, Any]:
        details = measure.DetailRowsDefinition
        return {
            "name": _text(measure.Name),
            "expression": _text(measure.Expression),
            "formatString": _text(measure.FormatString),
            "displayFolder": _text(measure.DisplayFolder),
            "description": _text(measure.Description),
            "isHidden": bool(measure.IsHidden),
            "formatStringExpression": self._format_expression(measure),
            "detailRowsExpression": _text(details.Expression) if details is not None else "",
            "kpi": self._describe_kpi(measure),
        }

    def _describe_item(self, item: Any) -> dict[str, Any]:
        return {
            "name": _text(item.Name),
            "expression": _text(item.Expression),
            "ordinal": int(item.Ordinal),
            "formatStringExpression": self._format_expression(item),
        }

    def _describe_table(self, table: Any) -> dict[str, Any]:
        group = table.CalculationGroup
        partitions = [
            {
                "name": _text(p.Name),
                "sourceType": self._source_type(p),
                "expression": _text(getattr(p.Source, "Expression", ""))
                if p.Source is not None
                else "",
                "mode": _text(p.Mode),
            }
            for p in table.Partitions
        ]
        kinds = {p["sourceType"] for p in partitions}
        columns = [self._describe_column(c) for c in table.Columns if _text(c.Type) != "RowNumber"]
        return {
            "name": _text(table.Name),
            "isHidden": bool(table.IsHidden),
            "description": _text(table.Description),
            "dataCategory": _text(table.DataCategory),
            "kind": "calculationGroup"
            if group is not None
            else "calculated"
            if "Calculated" in kinds
            else "data",
            "fieldParameter": any("ParameterMetadata" in c["extendedProperties"] for c in columns),
            "partitions": partitions,
            "columns": columns,
            "measures": [self._describe_measure(m) for m in table.Measures],
            "hierarchies": [
                {
                    "name": _text(h.Name),
                    "description": _text(h.Description),
                    "isHidden": bool(h.IsHidden),
                    "displayFolder": _text(h.DisplayFolder),
                    "levels": [
                        {
                            "name": _text(level.Name),
                            "column": _text(level.Column.Name) if level.Column is not None else "",
                        }
                        for level in sorted(h.Levels, key=lambda lv: int(lv.Ordinal))
                    ],
                }
                for h in table.Hierarchies
            ],
            "calculationGroup": None
            if group is None
            else {
                "precedence": int(group.Precedence),
                "description": _text(group.Description),
                "items": [
                    self._describe_item(i)
                    for i in sorted(group.CalculationItems, key=lambda i: int(i.Ordinal))
                ],
            },
        }

    def _describe_relationship(self, relationship: Any) -> dict[str, Any]:
        source, target = relationship.FromColumn, relationship.ToColumn
        return {
            "name": _text(relationship.Name),
            "fromTable": _text(relationship.FromTable.Name) if source is not None else "",
            "fromColumn": _text(source.Name) if source is not None else "",
            "toTable": _text(relationship.ToTable.Name) if target is not None else "",
            "toColumn": _text(target.Name) if target is not None else "",
            "fromCardinality": _text(relationship.FromCardinality),
            "toCardinality": _text(relationship.ToCardinality),
            "crossFilteringBehavior": _text(relationship.CrossFilteringBehavior),
            "securityFilteringBehavior": _text(relationship.SecurityFilteringBehavior),
            "isActive": bool(relationship.IsActive),
            "relyOnReferentialIntegrity": bool(relationship.RelyOnReferentialIntegrity),
        }

    @staticmethod
    def _describe_role(role: Any) -> dict[str, Any]:
        # A role's members (the people it names) are never read: Studio edits what a role sees.
        return {
            "name": _text(role.Name),
            "description": _text(role.Description),
            "modelPermission": _text(role.ModelPermission),
            "tablePermissions": {
                _text(p.Table.Name): _text(p.FilterExpression) for p in role.TablePermissions
            },
        }

    def snapshot(self) -> dict[str, Any]:
        """The model's metadata as plain JSON: what Studio checks and fixes names against."""
        annotations = {_text(a.Name): _text(a.Value) for a in self.model.Annotations}
        return {
            "tables": [self._describe_table(t) for t in self.model.Tables],
            "relationships": [
                self._describe_relationship(r)
                for r in self.model.Relationships
                if r.FromColumn is not None and r.ToColumn is not None
            ],
            "roles": [self._describe_role(r) for r in self.model.Roles],
            "expressions": [
                {
                    "name": _text(e.Name),
                    "kind": _text(e.Kind),
                    "expression": _text(e.Expression),
                    "description": _text(e.Description),
                }
                for e in self.model.Expressions
            ],
            "model": {
                "culture": _text(self.model.Culture),
                "discourageImplicitMeasures": bool(self.model.DiscourageImplicitMeasures),
                "annotations": annotations,
            },
        }

    # --- property setters shared by several kinds --------------------------------------------

    def _set_measure_props(self, measure: Any, props: Mapping[str, Any]) -> None:
        if "expression" in props:
            measure.Expression = _text(props["expression"])
        if "formatString" in props:
            measure.FormatString = _text(props["formatString"]) or None
        if "displayFolder" in props:
            measure.DisplayFolder = _text(props["displayFolder"]) or None
        if "description" in props:
            measure.Description = _text(props["description"]) or None
        if "isHidden" in props:
            measure.IsHidden = bool(props["isHidden"])
        if "formatStringExpression" in props:
            self._format_string_definition(measure, _text(props["formatStringExpression"]))
        if "detailRowsExpression" in props:
            text = _text(props["detailRowsExpression"])
            if text:
                details = self._new("DetailRowsDefinition")
                details.Expression = text
                measure.DetailRowsDefinition = details
            else:
                measure.DetailRowsDefinition = None
        if "kpi" in props:
            self._set_kpi(measure, props["kpi"])

    def _set_kpi(self, measure: Any, shape: Any) -> None:
        if not isinstance(shape, Mapping):
            measure.KPI = None
            return
        kpi = measure.KPI
        if kpi is None:
            kpi = self._new("KPI")
            measure.KPI = kpi
        for key, attribute in (
            ("targetExpression", "TargetExpression"),
            ("targetFormatString", "TargetFormatString"),
            ("statusExpression", "StatusExpression"),
            ("statusGraphic", "StatusGraphic"),
            ("trendExpression", "TrendExpression"),
            ("trendGraphic", "TrendGraphic"),
        ):
            if key in shape:
                setattr(kpi, attribute, _text(shape[key]) or None)

    def _set_column_props(self, table: Any, column: Any, props: Mapping[str, Any]) -> None:
        calculated = _text(column.Type) == "Calculated"
        if "expression" in props:
            if not calculated:
                raise ChangeError(f"'{_text(column.Name)}' is not a calculated column")
            column.Expression = _text(props["expression"])
        if "dataType" in props:
            if not calculated:
                raise ChangeError(
                    f"'{_text(column.Name)}' is loaded from the data source, so its type is set "
                    "in Power Query"
                )
            column.DataType = self._enum("DataType", _text(props["dataType"]))
            column.IsDataTypeInferred = False
        if "formatString" in props:
            column.FormatString = _text(props["formatString"]) or None
        if "sortByColumn" in props:
            wanted = _text(props["sortByColumn"])
            column.SortByColumn = self._column(table, wanted) if wanted else None
        if "dataCategory" in props:
            column.DataCategory = _text(props["dataCategory"]) or None
        if "summarizeBy" in props:
            column.SummarizeBy = self._enum("AggregateFunction", _text(props["summarizeBy"]))
        if "displayFolder" in props:
            column.DisplayFolder = _text(props["displayFolder"]) or None
        if "description" in props:
            column.Description = _text(props["description"]) or None
        if "isHidden" in props:
            column.IsHidden = bool(props["isHidden"])
        if "isKey" in props:
            column.IsKey = bool(props["isKey"])
        if isinstance(props.get("annotations"), Mapping):
            # SummarizationSetBy=User, for one: without it Desktop may reset a summarization.
            self._annotations(column, props["annotations"])

    @staticmethod
    def _before(described: Mapping[str, Any], props: Mapping[str, Any]) -> dict[str, Any]:
        return {key: described.get(key, "") for key in props}

    @staticmethod
    def _annotation_values(owner: Any, names: Any) -> dict[str, Any]:
        """The current value of each named annotation (None: absent), the before-image."""
        values: dict[str, Any] = {}
        for name in names if isinstance(names, Mapping) else {}:
            found = owner.Annotations.Find(_text(name))
            values[_text(name)] = _text(found.Value) if found is not None else None
        return values

    def _format_string_definition(self, owner: Any, expression: str) -> None:
        if expression:
            definition = self._new("FormatStringDefinition")
            definition.Expression = expression
            owner.FormatStringDefinition = definition
        else:
            owner.FormatStringDefinition = None

    # --- measures ----------------------------------------------------------------------------

    def _measure_create(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path, props = change["path"], change.get("props", {})
        table = self._table(path["table"])
        name = _text(path["name"])
        if table.Measures.Find(name) is not None:
            raise Exists(f"The table '{_text(table.Name)}' already has a measure [{name}]")
        measure = self._new("Measure")
        measure.Name = name
        self._set_measure_props(measure, props)
        table.Measures.Add(measure)
        where = {"table": path["table"], "name": name}
        undo = {"kind": "measure", "action": "delete", "path": where}
        return [{**undo, "expect": self._content("measure", where)}], None

    def _measure_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        props = change.get("props", {})
        table, measure = self._measure(change["path"])
        before = self._before(self._describe_measure(measure), props)
        self._set_measure_props(measure, props)
        path = {"table": _text(table.Name), "name": _text(measure.Name)}
        return [{"kind": "measure", "action": "update", "path": path, "props": before}], before

    def _measure_rename(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        table, measure = self._measure(change["path"])
        old, new = _text(measure.Name), _text(change["newName"])
        for other in self.model.Tables:
            if self._other(other.Measures.Find(new), measure):
                raise Exists(f"The model already has a measure [{new}]")
        if self._other(table.Columns.Find(new), measure):
            raise Exists(f"The table '{_text(table.Name)}' already has a field called '{new}'")
        measure.Name = new
        path = {"table": _text(table.Name), "name": new}
        return [{"kind": "measure", "action": "rename", "path": path, "newName": old}], None

    def _measure_delete(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        table, measure = self._measure(change["path"])
        tmsl = self._tmsl(measure)
        table.Measures.Remove(measure)
        undo = {"kind": "measure", "action": "restore", "path": {"table": _text(table.Name)}}
        return [{**undo, "tmsl": tmsl}], {"tmsl": tmsl}

    # --- columns -----------------------------------------------------------------------------

    def _calculated_column_create(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path, props = change["path"], dict(change.get("props", {}))
        table = self._table(path["table"])
        name = _text(path["name"])
        if table.Columns.Find(name) is not None or table.Measures.Find(name) is not None:
            raise Exists(f"The table '{_text(table.Name)}' already has a field called '{name}'")
        column = self._new("CalculatedColumn")
        column.Name = name
        column.Expression = _text(props.pop("expression", ""))
        sort_by = props.pop("sortByColumn", None)
        table.Columns.Add(column)
        self._set_column_props(table, column, props)
        if sort_by:
            column.SortByColumn = self._column(table, sort_by)
        where = {"table": path["table"], "name": name}
        undo = {"kind": "column", "action": "delete", "path": where}
        return [{**undo, "expect": self._content("column", where)}], None

    def _column_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path, props = change["path"], change.get("props", {})
        table = self._table(path["table"])
        column = self._column(table, path["name"])
        before = self._before(self._describe_column(column), props)
        if "annotations" in props:
            before["annotations"] = self._annotation_values(column, props["annotations"])
        self._set_column_props(table, column, props)
        return [{"kind": "column", "action": "update", "path": dict(path), "props": before}], before

    def _column_rename(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path = change["path"]
        table = self._table(path["table"])
        column = self._column(table, path["name"])
        new = _text(change["newName"])
        if self._other(table.Columns.Find(new), column) or self._other(
            table.Measures.Find(new), column
        ):
            raise Exists(f"The table '{_text(table.Name)}' already has a field called '{new}'")
        column.Name = new
        back = {"table": _text(table.Name), "name": new}
        return [{"kind": "column", "action": "rename", "path": back, "newName": path["name"]}], None

    def _column_delete(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path = change["path"]
        table = self._table(path["table"])
        column = self._column(table, path["name"])
        tmsl = self._tmsl(column)
        table.Columns.Remove(column)
        undo = {"kind": "column", "action": "restore", "path": {"table": _text(table.Name)}}
        return [{**undo, "tmsl": tmsl}], {"tmsl": tmsl}

    # --- tables ------------------------------------------------------------------------------

    def _new_calculated_table(self, name: str, expression: str) -> Any:
        if self.model.Tables.Find(name) is not None:
            raise Exists(f"The model already has a table '{name}'")
        table = self._new("Table")
        table.Name = name
        partition = self._new("Partition")
        partition.Name = name
        source = self._new("CalculatedPartitionSource")
        source.Expression = expression
        partition.Source = source
        table.Partitions.Add(partition)
        return table

    def _calculated_table_create(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        props = change.get("props", {})
        name = _text(change["path"]["name"])
        table = self._new_calculated_table(name, _text(props.get("expression")))
        if props.get("description"):
            table.Description = _text(props["description"])
        if props.get("isHidden"):
            table.IsHidden = True
        self.model.Tables.Add(table)
        return [self._delete_table_undo(name)], None

    def _delete_table_undo(self, name: str) -> dict[str, Any]:
        where = {"name": name}
        return {
            "kind": "table",
            "action": "delete",
            "path": where,
            "expect": self._content("table", where),
        }

    def _table_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        props = change.get("props", {})
        table = self._table(change["path"]["name"])
        described = self._describe_table(table)
        before: dict[str, Any] = {}
        if "expression" in props:
            partitions = list(table.Partitions)
            if len(partitions) != 1 or self._source_type(partitions[0]) != "Calculated":
                raise ChangeError(f"'{_text(table.Name)}' is not a calculated table")
            before["expression"] = _text(partitions[0].Source.Expression)
            partitions[0].Source.Expression = _text(props["expression"])
        for key in ("isHidden", "description", "dataCategory"):
            if key in props:
                before[key] = described[key]
        if "isHidden" in props:
            table.IsHidden = bool(props["isHidden"])
        if "description" in props:
            table.Description = _text(props["description"]) or None
        if "dataCategory" in props:
            table.DataCategory = _text(props["dataCategory"]) or None
        path = {"name": _text(table.Name)}
        return [{"kind": "table", "action": "update", "path": path, "props": before}], before

    def _table_rename(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        table = self._table(change["path"]["name"])
        old, new = _text(table.Name), _text(change["newName"])
        if self._other(self.model.Tables.Find(new), table):
            raise Exists(f"The model already has a table '{new}'")
        table.Name = new
        # A partition is named after its table by Desktop; the name is not a reference, so it
        # stays as it is (renaming it would not change what Desktop shows).
        return [{"kind": "table", "action": "rename", "path": {"name": new}, "newName": old}], None

    def _table_delete(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        table = self._table(change["path"]["name"])
        name = _text(table.Name)
        # Desktop removes a table's relationships with it; they come back with it on undo.
        joined = [
            r
            for r in list(self.model.Relationships)
            if (r.FromColumn is not None and _text(r.FromTable.Name) == name)
            or (r.ToColumn is not None and _text(r.ToTable.Name) == name)
        ]
        undo: list[Any] = []
        restored_relationships: list[Any] = []
        for relationship in joined:
            text = self._tmsl(relationship)
            self.model.Relationships.Remove(relationship)
            restored_relationships.append(
                {"kind": "relationship", "action": "restore", "path": {}, "tmsl": text}
            )
        tmsl = self._tmsl(table)
        self.model.Tables.Remove(table)
        undo.append({"kind": "table", "action": "restore", "path": {}, "tmsl": tmsl})
        undo.extend(restored_relationships)
        return undo, {"tmsl": tmsl, "relationships": len(joined)}

    def _date_table_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        props = change.get("props", {})
        table = self._table(change["path"]["name"])
        before = {
            "column": None,
            "dataCategory": _text(table.DataCategory),
            "keys": [_text(c.Name) for c in table.Columns if bool(c.IsKey)],
        }
        if _text(table.DataCategory) == "Time" and before["keys"]:
            before["column"] = before["keys"][0]
        if "keys" in props:  # the inverse: put back exactly what was there
            table.DataCategory = _text(props.get("dataCategory")) or None
            keys = {_text(k) for k in props["keys"]}
            for column in table.Columns:
                if _text(column.Type) != "RowNumber":
                    column.IsKey = _text(column.Name) in keys
        elif props.get("column"):
            key = self._column(table, props["column"])
            for column in table.Columns:
                if _text(column.Type) != "RowNumber" and bool(column.IsKey):
                    column.IsKey = False
            table.DataCategory = "Time"
            key.IsKey = True
        else:
            table.DataCategory = None
            for column in table.Columns:
                if _text(column.Type) != "RowNumber" and bool(column.IsKey):
                    column.IsKey = False
        path = {"name": _text(table.Name)}
        return [{"kind": "dateTable", "action": "update", "path": path, "props": before}], before

    # --- relationships -----------------------------------------------------------------------

    def _set_relationship_props(self, relationship: Any, props: Mapping[str, Any]) -> None:
        if "fromCardinality" in props:
            relationship.FromCardinality = self._enum(
                "RelationshipEndCardinality", _text(props["fromCardinality"])
            )
        if "toCardinality" in props:
            relationship.ToCardinality = self._enum(
                "RelationshipEndCardinality", _text(props["toCardinality"])
            )
        if "crossFilteringBehavior" in props:
            relationship.CrossFilteringBehavior = self._enum(
                "CrossFilteringBehavior", _text(props["crossFilteringBehavior"])
            )
        if "securityFilteringBehavior" in props:
            relationship.SecurityFilteringBehavior = self._enum(
                "SecurityFilteringBehavior", _text(props["securityFilteringBehavior"])
            )
        if "isActive" in props:
            relationship.IsActive = bool(props["isActive"])
        if "relyOnReferentialIntegrity" in props:
            relationship.RelyOnReferentialIntegrity = bool(props["relyOnReferentialIntegrity"])

    def _relationship_create(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path, props = change["path"], change.get("props", {})
        try:
            self._relationship(path)
        except Missing:
            pass
        else:
            raise Exists("These two columns are already related")
        source = self._column(self._table(path["fromTable"]), path["fromColumn"])
        target = self._column(self._table(path["toTable"]), path["toColumn"])
        relationship = self._new("SingleColumnRelationship")
        relationship.Name = _text(props.get("name")) or str(uuid.uuid4())
        relationship.FromColumn = source
        relationship.ToColumn = target
        self._set_relationship_props(relationship, props)
        self.model.Relationships.Add(relationship)
        undo = {"kind": "relationship", "action": "delete", "path": dict(path)}
        return [{**undo, "expect": self._content("relationship", path)}], None

    def _relationship_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path, props = change["path"], change.get("props", {})
        relationship = self._relationship(path)
        before = self._before(self._describe_relationship(relationship), props)
        self._set_relationship_props(relationship, props)
        undo = {"kind": "relationship", "action": "update", "path": dict(path), "props": before}
        return [undo], before

    def _relationship_delete(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        relationship = self._relationship(change["path"])
        tmsl = self._tmsl(relationship)
        self.model.Relationships.Remove(relationship)
        return [{"kind": "relationship", "action": "restore", "path": {}, "tmsl": tmsl}], {
            "tmsl": tmsl
        }

    # --- hierarchies -------------------------------------------------------------------------

    def _set_levels(self, table: Any, hierarchy: Any, levels: list[Any]) -> None:
        wanted = [(_text(level.get("name")) or _text(level["column"]), level) for level in levels]
        keep = {name for name, _ in wanted}
        for existing in list(hierarchy.Levels):
            if _text(existing.Name) not in keep:
                hierarchy.Levels.Remove(existing)
        for ordinal, (name, level) in enumerate(wanted):
            found = hierarchy.Levels.Find(name)
            if found is None:
                found = self._new("Level")
                found.Name = name
                hierarchy.Levels.Add(found)
            found.Column = self._column(table, level["column"])
            found.Ordinal = ordinal

    def _set_hierarchy_props(self, table: Any, hierarchy: Any, props: Mapping[str, Any]) -> None:
        if "levels" in props:
            self._set_levels(table, hierarchy, list(props["levels"]))
        if "description" in props:
            hierarchy.Description = _text(props["description"]) or None
        if "isHidden" in props:
            hierarchy.IsHidden = bool(props["isHidden"])
        if "displayFolder" in props:
            hierarchy.DisplayFolder = _text(props["displayFolder"]) or None

    def _hierarchy_create(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path, props = change["path"], change.get("props", {})
        table = self._table(path["table"])
        name = _text(path["name"])
        if table.Hierarchies.Find(name) is not None:
            raise Exists(f"The table '{_text(table.Name)}' already has a hierarchy '{name}'")
        hierarchy = self._new("Hierarchy")
        hierarchy.Name = name
        table.Hierarchies.Add(hierarchy)
        self._set_hierarchy_props(table, hierarchy, props)
        where = {"table": path["table"], "name": name}
        undo = {"kind": "hierarchy", "action": "delete", "path": where}
        return [{**undo, "expect": self._content("hierarchy", where)}], None

    def _hierarchy_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path, props = change["path"], change.get("props", {})
        table, hierarchy = self._hierarchy(path)
        own = _text(hierarchy.Name)
        described = next(h for h in self._describe_table(table)["hierarchies"] if h["name"] == own)
        before = self._before(described, props)
        self._set_hierarchy_props(table, hierarchy, props)
        undo = {"kind": "hierarchy", "action": "update", "path": dict(path), "props": before}
        return [undo], before

    def _hierarchy_rename(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        table, hierarchy = self._hierarchy(change["path"])
        old, new = _text(hierarchy.Name), _text(change["newName"])
        if self._other(table.Hierarchies.Find(new), hierarchy):
            raise Exists(f"The table '{_text(table.Name)}' already has a hierarchy '{new}'")
        hierarchy.Name = new
        back = {"table": _text(table.Name), "name": new}
        return [{"kind": "hierarchy", "action": "rename", "path": back, "newName": old}], None

    def _hierarchy_delete(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        table, hierarchy = self._hierarchy(change["path"])
        tmsl = self._tmsl(hierarchy)
        table.Hierarchies.Remove(hierarchy)
        undo = {"kind": "hierarchy", "action": "restore", "path": {"table": _text(table.Name)}}
        return [{**undo, "tmsl": tmsl}], {"tmsl": tmsl}

    # --- field parameters --------------------------------------------------------------------

    def _field_parameter_create(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        """A field parameter the way Desktop builds one: a calculated table of (label, NAMEOF,
        order) rows whose three columns are the visible label, the hidden fields column that
        carries the ``ParameterMetadata`` extended property, and the hidden order both sort by."""
        props = change.get("props", {})
        name = _text(change["path"]["name"])
        table = self._new_calculated_table(name, _text(props.get("expression")))
        names = props.get("columns") or {}
        label = _text(names.get("label")) or name
        fields = _text(names.get("fields")) or f"{name} Fields"
        order = _text(names.get("order")) or f"{name} Order"
        columns = []
        for column_name, source, data_type, hidden in (
            (label, "[Value1]", "String", False),
            (fields, "[Value2]", "String", True),
            (order, "[Value3]", "Int64", True),
        ):
            column = self._new("CalculatedTableColumn")
            column.Name = column_name
            column.SourceColumn = source
            column.IsNameInferred = False
            column.IsDataTypeInferred = True
            column.DataType = self._enum("DataType", data_type)
            column.IsHidden = hidden
            table.Columns.Add(column)
            columns.append(column)
        label_column, fields_column, order_column = columns
        label_column.SortByColumn = order_column
        fields_column.SortByColumn = order_column
        details = self._new("RelatedColumnDetails")
        grouping = self._new("GroupByColumn")
        grouping.GroupingColumn = fields_column
        details.GroupByColumns.Add(grouping)
        label_column.RelatedColumnDetails = details
        metadata = self._new("JsonExtendedProperty")
        metadata.Name = "ParameterMetadata"
        metadata.Value = json.dumps({"version": 3, "kind": 2}, separators=(",", ":"))
        fields_column.ExtendedProperties.Add(metadata)
        self.model.Tables.Add(table)
        return [self._delete_table_undo(name)], None

    def _field_parameter_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        table = self._table(change["path"]["name"])
        _, partition = self._partition({"table": _text(table.Name)})
        before = {"expression": _text(partition.Source.Expression)}
        partition.Source.Expression = _text(change.get("props", {}).get("expression"))
        path = {"name": _text(table.Name)}
        undo = {"kind": "fieldParameter", "action": "update", "path": path, "props": before}
        return [undo], before

    # --- calculation groups ------------------------------------------------------------------

    def _set_item_props(self, item: Any, props: Mapping[str, Any]) -> None:
        if "expression" in props:
            item.Expression = _text(props["expression"])
        if "ordinal" in props and props["ordinal"] is not None:
            item.Ordinal = int(props["ordinal"])
        if "formatStringExpression" in props:
            self._format_string_definition(item, _text(props["formatStringExpression"]))

    def _calculation_group_create(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        props = change.get("props", {})
        name = _text(change["path"]["name"])
        if self.model.Tables.Find(name) is not None:
            raise Exists(f"The model already has a table '{name}'")
        table = self._new("Table")
        table.Name = name
        if props.get("description"):
            table.Description = _text(props["description"])
        group = self._new("CalculationGroup")
        group.Precedence = int(props.get("precedence") or 0)
        table.CalculationGroup = group
        column = self._new("DataColumn")
        column.Name = _text(props.get("column")) or "Name"
        column.DataType = self._enum("DataType", "String")
        column.SourceColumn = "Name"
        table.Columns.Add(column)
        ordinal = self._new("DataColumn")
        ordinal.Name = "Ordinal"
        ordinal.DataType = self._enum("DataType", "Int64")
        ordinal.SourceColumn = "Ordinal"
        ordinal.IsHidden = True
        table.Columns.Add(ordinal)
        column.SortByColumn = ordinal
        partition = self._new("Partition")
        partition.Name = name
        partition.Source = self._new("CalculationGroupSource")
        table.Partitions.Add(partition)
        for index, spec in enumerate(props.get("items") or []):
            item = self._new("CalculationItem")
            item.Name = _text(spec["name"])
            self._set_item_props(item, {"ordinal": index, **spec})
            group.CalculationItems.Add(item)
        self.model.Tables.Add(table)
        return [self._delete_table_undo(name)], None

    def _calculation_group_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        props = change.get("props", {})
        table = self._group(change["path"]["name"])
        group = table.CalculationGroup
        before: dict[str, Any] = {}
        if "precedence" in props:
            before["precedence"] = int(group.Precedence)
            group.Precedence = int(props["precedence"])
        if "description" in props:
            before["description"] = _text(table.Description)
            table.Description = _text(props["description"]) or None
        path = {"name": _text(table.Name)}
        undo = {"kind": "calculationGroup", "action": "update", "path": path, "props": before}
        return [undo], before

    def _calculation_item_create(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path, props = change["path"], change.get("props", {})
        table = self._group(path["group"])
        items = table.CalculationGroup.CalculationItems
        name = _text(path["name"])
        if items.Find(name) is not None:
            raise Exists(f"The calculation group '{_text(table.Name)}' already has '{name}'")
        item = self._new("CalculationItem")
        item.Name = name
        ordinal = props.get("ordinal")
        self._set_item_props(
            item, {**props, "ordinal": len(list(items)) if ordinal is None else ordinal}
        )
        items.Add(item)
        undo = {"kind": "calculationItem", "action": "delete", "path": dict(path)}
        return [{**undo, "expect": self._content("calculationItem", path)}], None

    def _calculation_item_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path, props = change["path"], change.get("props", {})
        _, item = self._item(path)
        before = self._before(self._describe_item(item), props)
        self._set_item_props(item, props)
        undo = {"kind": "calculationItem", "action": "update", "path": dict(path), "props": before}
        return [undo], before

    def _calculation_item_rename(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path = change["path"]
        table, item = self._item(path)
        old, new = _text(item.Name), _text(change["newName"])
        if self._other(table.CalculationGroup.CalculationItems.Find(new), item):
            raise Exists(f"The calculation group '{_text(table.Name)}' already has an item '{new}'")
        item.Name = new
        back = {"group": _text(table.Name), "name": new}
        return [{"kind": "calculationItem", "action": "rename", "path": back, "newName": old}], None

    def _calculation_item_delete(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        table, item = self._item(change["path"])
        tmsl = self._tmsl(item)
        table.CalculationGroup.CalculationItems.Remove(item)
        where = {"group": _text(table.Name)}
        undo = {"kind": "calculationItem", "action": "restore", "path": where, "tmsl": tmsl}
        return [undo], {"tmsl": tmsl}

    # --- roles -------------------------------------------------------------------------------

    def _set_role_filters(self, role: Any, filters: Mapping[str, Any]) -> dict[str, Any]:
        before: dict[str, Any] = {}
        for table_name, expression in filters.items():
            table = self._table(table_name)
            existing = role.TablePermissions.Find(_text(table.Name))
            before[_text(table.Name)] = (
                _text(existing.FilterExpression) if existing is not None else None
            )
            if expression is None or _text(expression) == "":
                if existing is not None:
                    role.TablePermissions.Remove(existing)
                continue
            if existing is None:
                existing = self._new("TablePermission")
                existing.Table = table
                role.TablePermissions.Add(existing)
            existing.FilterExpression = _text(expression)
        return before

    def _role_create(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        props = change.get("props", {})
        name = _text(change["path"]["name"])
        if self.model.Roles.Find(name) is not None:
            raise Exists(f"The model already has a role '{name}'")
        role = self._new("ModelRole")
        role.Name = name
        role.ModelPermission = self._enum("ModelPermission", "Read")
        if props.get("description"):
            role.Description = _text(props["description"])
        self.model.Roles.Add(role)
        self._set_role_filters(role, props.get("tablePermissions") or {})
        where = {"name": name}
        undo = {"kind": "role", "action": "delete", "path": where}
        return [{**undo, "expect": self._content("role", where)}], None

    def _role_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        props = change.get("props", {})
        role = self._role(change["path"]["name"])
        before: dict[str, Any] = {}
        if "description" in props:
            before["description"] = _text(role.Description)
            role.Description = _text(props["description"]) or None
        if "tablePermissions" in props:
            before["tablePermissions"] = self._set_role_filters(role, props["tablePermissions"])
        path = {"name": _text(role.Name)}
        return [{"kind": "role", "action": "update", "path": path, "props": before}], before

    def _role_rename(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        role = self._role(change["path"]["name"])
        old, new = _text(role.Name), _text(change["newName"])
        if self._other(self.model.Roles.Find(new), role):
            raise Exists(f"The model already has a role '{new}'")
        role.Name = new
        return [{"kind": "role", "action": "rename", "path": {"name": new}, "newName": old}], None

    def _role_delete(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        role = self._role(change["path"]["name"])
        tmsl = self._tmsl(role)
        self.model.Roles.Remove(role)
        return [{"kind": "role", "action": "restore", "path": {}, "tmsl": tmsl}], {"tmsl": tmsl}

    # --- Power Query ---------------------------------------------------------------------------

    def _partition_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        path, props = change["path"], change.get("props", {})
        table, partition = self._partition(path)
        kind = self._source_type(partition)
        if "mExpression" in props:
            if kind not in ("M", "MPartitionSource"):
                raise ChangeError(f"'{_text(table.Name)}' does not load from a Power Query query")
            key = "mExpression"
        elif "daxExpression" in props:
            if kind not in ("Calculated", "CalculatedPartitionSource"):
                raise ChangeError(f"'{_text(table.Name)}' is not a calculated table")
            key = "daxExpression"
        else:
            raise ChangeError("Say what the table's query becomes")
        before = {key: _text(partition.Source.Expression)}
        partition.Source.Expression = _text(props[key])
        back = {"table": _text(table.Name), "name": _text(partition.Name)}
        return [{"kind": "partition", "action": "update", "path": back, "props": before}], before

    def _expression_create(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        props = change.get("props", {})
        name = _text(change["path"]["name"])
        if self.model.Expressions.Find(name) is not None:
            raise Exists(f"The model already has a query or parameter '{name}'")
        expression = self._new("NamedExpression")
        expression.Name = name
        expression.Kind = self._enum("ExpressionKind", "M")
        expression.Expression = _text(props.get("expression"))
        if props.get("description"):
            expression.Description = _text(props["description"])
        self.model.Expressions.Add(expression)
        where = {"name": name}
        undo = {"kind": "expression", "action": "delete", "path": where}
        return [{**undo, "expect": self._content("expression", where)}], None

    def _expression_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        props = change.get("props", {})
        expression = self._expression(change["path"]["name"])
        before: dict[str, Any] = {}
        if "expression" in props:
            before["expression"] = _text(expression.Expression)
            expression.Expression = _text(props["expression"])
        if "description" in props:
            before["description"] = _text(expression.Description)
            expression.Description = _text(props["description"]) or None
        path = {"name": _text(expression.Name)}
        return [{"kind": "expression", "action": "update", "path": path, "props": before}], before

    def _expression_delete(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        expression = self._expression(change["path"]["name"])
        tmsl = self._tmsl(expression)
        self.model.Expressions.Remove(expression)
        undo = {"kind": "expression", "action": "restore", "path": {}, "tmsl": tmsl}
        return [undo], {"tmsl": tmsl}

    # --- the model itself ----------------------------------------------------------------------

    def _model_update(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        props = change.get("props", {})
        before: dict[str, Any] = {}
        if "discourageImplicitMeasures" in props:
            before["discourageImplicitMeasures"] = bool(self.model.DiscourageImplicitMeasures)
            self.model.DiscourageImplicitMeasures = bool(props["discourageImplicitMeasures"])
        if "culture" in props:
            before["culture"] = _text(self.model.Culture)
            self.model.Culture = _text(props["culture"])
        if "annotations" in props:
            before["annotations"] = self._annotations(self.model, dict(props["annotations"]))
        return [{"kind": "model", "action": "update", "path": {}, "props": before}], before

    # --- what a created object holds (so an undo never deletes a different object) -------------

    def _content(self, kind: str, path: Mapping[str, Any]) -> dict[str, Any]:
        """The parts of an object that tell it apart from another one with the same name: an
        undo of a create deletes the object only while it still holds them. (A run that dies
        before its save leaves the model as it was; replaying its undo must not delete the
        object that had the name first.)"""
        if kind == "measure":
            _, measure = self._measure(path)
            return {"expression": _flat(measure.Expression)}
        if kind == "column":
            column = self._column(self._table(path.get("table")), path.get("name"))
            expression = getattr(column, "Expression", "")
            return {"type": _text(column.Type), "expression": _flat(expression)}
        if kind == "table":
            # Not the columns: the engine adds a calculated table's columns when it saves.
            table = self._table(path.get("name"))
            group = table.CalculationGroup
            sources = [
                _flat(getattr(p.Source, "Expression", "")) if p.Source is not None else ""
                for p in table.Partitions
            ]
            items = [] if group is None else sorted(_text(i.Name) for i in group.CalculationItems)
            return {"sources": sources, "items": items}
        if kind == "hierarchy":
            _, hierarchy = self._hierarchy(path)
            levels = sorted(hierarchy.Levels, key=lambda lv: int(lv.Ordinal))
            columns = [_text(lv.Column.Name) if lv.Column is not None else "" for lv in levels]
            return {"levels": columns}
        if kind == "relationship":
            return {"name": _text(self._relationship(path).Name)}
        if kind == "calculationItem":
            _, item = self._item(path)
            return {"expression": _flat(item.Expression)}
        if kind == "role":
            role = self._role(path.get("name"))
            filters = {
                _text(p.Table.Name): _flat(p.FilterExpression) for p in role.TablePermissions
            }
            return {"filters": filters}
        if kind == "expression":
            return {"expression": _flat(self._expression(path.get("name")).Expression)}
        return {}

    def _still_created(self, change: Mapping[str, Any]) -> bool:
        expect = change.get("expect")
        if not isinstance(expect, Mapping):
            return True
        try:
            found = self._content(_text(change.get("kind")), change.get("path") or {})
        except (Missing, ChangeError):
            return False
        return all(found.get(key) == value for key, value in expect.items())

    # --- restoring a deleted object --------------------------------------------------------------

    _RESTORE_CLASSES = {
        "measure": "Measure",
        "hierarchy": "Hierarchy",
        "table": "Table",
        "relationship": "SingleColumnRelationship",
        "role": "ModelRole",
        "calculationItem": "CalculationItem",
        "expression": "NamedExpression",
    }
    _COLUMN_CLASSES = {
        "calculated": "CalculatedColumn",
        "calculatedTableColumn": "CalculatedTableColumn",
    }

    def _restore(self, change: dict[str, Any]) -> tuple[list[Any], Any]:
        kind, path, text = change["kind"], change.get("path", {}), _text(change.get("tmsl"))
        try:
            shape = json.loads(text)
        except ValueError as error:
            raise ChangeError("The saved copy of the deleted object is unreadable") from error
        name = _text(shape.get("name")) if isinstance(shape, dict) else ""
        if kind == "column":
            table = self._table(path["table"])
            if table.Columns.Find(name) is not None:
                raise Exists(f"The table '{_text(table.Name)}' already has a column '{name}'")
            cls = self._COLUMN_CLASSES.get(_text(shape.get("type")), "DataColumn")
            table.Columns.Add(self._from_tmsl(cls, text))
        elif kind == "measure":
            table = self._table(path["table"])
            if table.Measures.Find(name) is not None:
                raise Exists(f"The table '{_text(table.Name)}' already has a measure [{name}]")
            table.Measures.Add(self._from_tmsl("Measure", text))
        elif kind == "hierarchy":
            table = self._table(path["table"])
            if table.Hierarchies.Find(name) is not None:
                raise Exists(f"The table '{_text(table.Name)}' already has a hierarchy '{name}'")
            table.Hierarchies.Add(self._from_tmsl("Hierarchy", text))
        elif kind == "calculationItem":
            items = self._group(path["group"]).CalculationGroup.CalculationItems
            if items.Find(name) is not None:
                raise Exists(f"The calculation item '{name}' already exists")
            items.Add(self._from_tmsl("CalculationItem", text))
        elif kind in ("table", "relationship", "role", "expression"):
            collection = {
                "table": self.model.Tables,
                "relationship": self.model.Relationships,
                "role": self.model.Roles,
                "expression": self.model.Expressions,
            }[kind]
            if kind == "relationship":
                ends = {
                    "fromTable": shape.get("fromTable"),
                    "fromColumn": shape.get("fromColumn"),
                    "toTable": shape.get("toTable"),
                    "toColumn": shape.get("toColumn"),
                }
                try:
                    self._relationship(ends)
                except Missing:
                    pass
                else:
                    raise Exists("These two columns are already related")
            elif collection.Find(name) is not None:
                raise Exists(f"'{name}' already exists")
            collection.Add(self._from_tmsl(self._RESTORE_CLASSES[kind], text))
        else:
            raise ChangeError(f"Studio cannot restore a {kind}")
        return [], None

    # --- running a request ---------------------------------------------------------------------

    def apply_change(self, change: dict[str, Any], *, tolerant: bool) -> tuple[list[Any], Any]:
        """Apply one change locally; returns (its inverse changes, its before-image)."""
        action = _text(change.get("action"))
        try:
            if action == "restore":
                return self._restore(change)
            handler = self._actions.get((_text(change.get("kind")), action))
            if handler is None:
                raise ChangeError(
                    f"Studio does not know how to {action} a {_text(change.get('kind'))}"
                )
            if action == "delete" and not self._still_created(change):
                raise Missing("The object Studio added is no longer there as it was added")
            return handler(change)
        except (Missing, Exists):
            # A compensation that finds the object already gone (or already back) has
            # nothing left to do for it.
            if tolerant:
                return [], None
            raise

    def _schema_rows(self) -> dict[str, list[dict[str, Any]]] | None:
        """Every schema view; one that fails (an engine without calculation groups) is empty.
        None when the tables view itself cannot be read: nothing could be named then."""
        if self.rows is None:
            return None
        views: dict[str, list[dict[str, Any]]] = {}
        for key, query in _SCHEMA_QUERIES.items():
            try:
                views[key] = self.rows(query)
            except Exception:  # noqa: BLE001 - without a view only the probes check its objects
                if key == "tables":
                    return None
                views[key] = []
        return views

    @staticmethod
    def _state(row: Mapping[str, Any]) -> str:
        raw: Any = row.get("State")
        try:
            return STATE_NAMES.get(int(raw), _text(raw))
        except (TypeError, ValueError):
            return _text(raw)

    def broken(self) -> dict[str, str] | None:
        """Objects in an error state, as 'measure Table[Name]' -> the engine's message; None when
        the schema views cannot be read."""
        views = self._schema_rows()
        if views is None:
            return None
        tables = {_text(r.get("ID")): _text(r.get("Name")) for r in views["tables"]}
        roles = {_text(r.get("ID")): _text(r.get("Name")) for r in views["roles"]}

        def table_of(row: Mapping[str, Any]) -> str:
            return tables.get(_text(row.get("TableID")), "")

        groups = {_text(r.get("ID")): table_of(r) for r in views["groups"]}
        found: dict[str, str] = {}

        def note(label: str, row: Mapping[str, Any]) -> None:
            if self._state(row) in ERROR_STATES:
                found[label] = _text(row.get("ErrorMessage")) or self._state(row)

        for row in views["measures"]:
            note(f"measure {table_of(row)}[{_text(row.get('Name'))}]", row)
        for row in views["columns"]:
            name = _text(row.get("ExplicitName")) or _text(row.get("InferredName"))
            note(f"column {table_of(row)}[{name}]", row)
        for row in views["partitions"]:
            note(f"table {table_of(row)}", row)
        for row in views["permissions"]:
            role = roles.get(_text(row.get("RoleID")), "")
            note(f"role {role} on {table_of(row)}", row)
        for row in views["items"]:
            group = groups.get(_text(row.get("CalculationGroupID")), "")
            note(f"calculation item {group}[{_text(row.get('Name'))}]", row)
        return found

    def _compensate(self, results: list[dict[str, Any]]) -> list[str]:
        """Undo saved changes with their inverses, newest first, in one save."""
        errors: list[str] = []
        for record in reversed(results):
            for undo in record.get("undo") or []:
                try:
                    self.apply_change(undo, tolerant=True)
                except Exception as error:  # noqa: BLE001 - keep undoing the others
                    errors.append(_message(error))
        try:
            self.model.RequestRefresh(self._enum("RefreshType", "Calculate"))
            self.model.SaveChanges()
        except Exception as error:  # noqa: BLE001 - reported to the caller
            errors.append(_message(error))
            self._undo_local()
        return errors

    def _undo_local(self) -> None:
        try:
            self.model.UndoLocalChanges()
        except Exception:  # noqa: BLE001 - nothing more can be done from here
            return

    def _refresh(self, targets: list[Any]) -> None:
        full = self._enum("RefreshType", "Full")
        for target in targets:
            if isinstance(target, dict) and target.get("table"):
                self._table(target["table"]).RequestRefresh(full)
            else:
                self.model.RequestRefresh(full)
        self.model.SaveChanges()

    def apply(self, request: Mapping[str, Any]) -> dict[str, Any]:
        tolerant = bool(request.get("tolerant"))
        changes = [c for c in request.get("changes") or [] if isinstance(c, dict)]
        broken_before = self.broken()
        results: list[dict[str, Any]] = []
        outcome: dict[str, Any] = {
            "ok": False,
            "stage": "apply",
            "error": None,
            "results": results,
            "problems": [],
            "refresh": None,
            "probes": [],
            "rolled_back": False,
            "rollback_errors": [],
            "kept_without_refresh": False,
            "checked_states": broken_before is not None,
            "warnings": [],
        }
        if broken_before is None and self.rows is not None:
            outcome["warnings"].append(
                "Studio could not read which model objects had errors before the change"
            )
        try:
            for change in changes:
                undo, before = self.apply_change(change, tolerant=tolerant)
                record = {"id": change.get("id"), "undo": undo, "before": before}
                results.append(record)
                self.emit({"event": "applied", **record})
            outcome["stage"] = "save"
            if request.get("recalculate", True):
                self.model.RequestRefresh(self._enum("RefreshType", "Calculate"))
            self.model.SaveChanges()
        except Exception as error:  # noqa: BLE001 - every failure before the save is undone locally
            self._undo_local()
            outcome.update(error=_message(error), rolled_back=True, results=[])
            return outcome
        self.emit({"event": "saved"})
        outcome["stage"] = "validate"
        after = self.broken()
        if after is None and broken_before is not None:
            # The views answered before the save and not after it: nothing was checked.
            outcome["checked_states"] = False
            outcome["warnings"].append(
                "Studio could not read which model objects have errors after the change, so "
                "only its own checks confirmed it"
            )
        if after is not None and broken_before is not None:
            fresh = {k: v for k, v in after.items() if k not in broken_before}
            if fresh:
                outcome["problems"] = [{"object": k, "error": v} for k, v in fresh.items()]
                outcome["error"] = "; ".join(f"{k}: {v}" for k, v in list(fresh.items())[:3])
                outcome["rollback_errors"] = self._compensate(results)
                outcome["rolled_back"] = True
                return outcome
        targets = [t for t in request.get("refresh") or [] if isinstance(t, dict)]
        if targets:
            outcome["stage"] = "refresh"
            try:
                self._refresh(targets)
            except Exception as error:  # noqa: BLE001 - a source that cannot be reached, typically
                self._undo_local()
                outcome["refresh"] = {"ok": False, "error": _message(error)}
                if request.get("on_refresh_failure") == "keep":
                    outcome["kept_without_refresh"] = True
                else:
                    outcome["error"] = _message(error)
                    outcome["rollback_errors"] = self._compensate(results)
                    outcome["rolled_back"] = True
                    return outcome
            else:
                outcome["refresh"] = {"ok": True, "error": None}
        outcome["stage"] = "probe"
        for probe in request.get("probes") or []:
            if not isinstance(probe, dict) or self.rows is None:
                continue
            if probe.get("role") or probe.get("informational"):
                outcome["probes"].append(self._look(probe))
                continue
            try:
                count = len(self.rows(_text(probe.get("query"))))
            except Exception as error:  # noqa: BLE001 - a failing probe undoes the run
                failed = {"id": probe.get("id"), "ok": False, "error": _message(error)}
                outcome["probes"].append(failed)
                outcome["error"] = f"{_text(probe.get('label')) or 'A check'}: {_message(error)}"
                outcome["rollback_errors"] = self._compensate(results)
                outcome["rolled_back"] = True
                return outcome
            outcome["probes"].append({"id": probe.get("id"), "ok": True, "rows": count})
        outcome.update(ok=True, stage="done", error=None)
        return outcome

    def _look(self, probe: Mapping[str, Any]) -> dict[str, Any]:
        """An informational probe (optionally as a role): its first rows, never a rollback."""
        answer: dict[str, Any] = {"id": probe.get("id"), "informational": True}
        role = _text(probe.get("role"))
        try:
            if role:
                if self.rows_as is None:
                    return {**answer, "ok": None, "error": "Queries as a role are not available"}
                self._role(role)
                rows = self.rows_as(role)
            elif self.rows is not None:
                rows = self.rows
            else:
                return {**answer, "ok": None, "error": "Queries are not available"}
            found = rows(_text(probe.get("query")))
        except Exception as error:  # noqa: BLE001 - reported, never undoes the run
            return {**answer, "ok": False, "error": _message(error)}
        return {**answer, "ok": True, "rows": len(found), "values": found[:5]}


# --- the process around it ----------------------------------------------------------------------


def _load_tom() -> tuple[Any, Any, Any]:
    """TOM and ADOMD.NET through pbi-cli's loader (the only part of pbi-cli this uses)."""
    from pbi_cli.core import dotnet_loader  # type: ignore[import-not-found,unused-ignore]

    server = dotnet_loader.get_server_class()
    connection = dotnet_loader.get_adomd_connection_class()
    command = dotnet_loader.get_adomd_command_class()
    tom = importlib.import_module("Microsoft.AnalysisServices.Tabular")
    return tom, server, (connection, command)


def _query_runner(connection: Any, command_class: Any) -> Rows:
    def rows(query: str) -> list[dict[str, Any]]:
        command = command_class(query, connection)
        command.CommandTimeout = 60
        reader = command.ExecuteReader()
        try:
            names = [_text(reader.GetName(i)) for i in range(int(reader.FieldCount))]
            found: list[dict[str, Any]] = []
            while reader.Read():
                row: dict[str, Any] = {}
                for index, name in enumerate(names):
                    value = reader.GetValue(index)
                    row[name] = value if isinstance(value, bool | int | float) else _text(value)
                found.append(row)
                if len(found) >= 100000:
                    break
            return found
        finally:
            reader.Close()

    return rows


def read_queries(rows: Rows, request: Mapping[str, Any]) -> dict[str, Any]:
    """The ``query`` action: read-only DAX queries (EVALUATE only), the first rows of each."""
    answers: dict[str, Any] = {}
    for item in request.get("queries") or []:
        if not isinstance(item, dict):
            continue
        key, query = _text(item.get("id")), _text(item.get("query"))
        if not query.lstrip().upper().startswith("EVALUATE"):
            answers[key] = {"ok": False, "error": "Only EVALUATE queries are read"}
            continue
        try:
            answers[key] = {"ok": True, "rows": rows(query)[:10]}
        except Exception as error:  # noqa: BLE001 - reported per query
            answers[key] = {"ok": False, "error": _message(error)}
    return {"answers": answers}


def role_connection_text(connection_text: str, role: str) -> str:
    """A connection string that sees the model as one role does (Desktop's "View as" uses the
    same property). The value is quoted, quotes doubled; Roles is a comma-separated list, so a
    role whose name has a comma cannot be named alone and is refused."""
    if "," in role or not role.strip():
        raise ChangeError(f"Studio cannot view the model as the role '{role}'")
    quoted = '"' + role.replace('"', '""') + '"'
    return f"{connection_text};Roles={quoted}"


def _write(event: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def run(request: Mapping[str, Any], emit: Emit = _write) -> dict[str, Any]:
    """Connect to the server the request names, run it, and return the result event."""
    if request.get("version") != PROTOCOL:
        return {"event": "result", "ok": False, "stage": "request", "error": "Unknown protocol"}
    server_name = _text(request.get("server"))
    host, _, port = server_name.partition(":")
    if host.lower() != "localhost" or not port.isdecimal():
        return {"event": "result", "ok": False, "stage": "request", "error": "Not a local model"}
    try:
        tom, server_class, (connection_class, command_class) = _load_tom()
    except Exception as error:  # noqa: BLE001 - the libraries could not be loaded
        return {"event": "result", "ok": False, "stage": "load", "error": _message(error)}
    connection_text = f"Provider=MSOLAP;Data Source={server_name}"
    server = server_class()
    connections: list[Any] = []
    try:
        server.Connect(connection_text)
        databases = list(server.Databases)
        if len(databases) != 1:
            return {
                "event": "result",
                "ok": False,
                "stage": "connect",
                "error": "Power BI Desktop's model engine has more than one model open",
            }
        database = databases[0]
        emit(
            {
                "event": "connected",
                "database": _text(database.Name),
                "compatibilityLevel": int(database.CompatibilityLevel),
            }
        )
        connection = connection_class(connection_text)
        connections.append(connection)
        connection.Open()
        rows = _query_runner(connection, command_class)
        as_role: dict[str, Rows] = {}

        def rows_as(role: str) -> Rows:
            if role not in as_role:
                other = connection_class(role_connection_text(connection_text, role))
                connections.append(other)
                other.Open()
                as_role[role] = _query_runner(other, command_class)
            return as_role[role]

        runner = Runner(tom, database.Model, rows, emit, rows_as)
        if request.get("action") == "snapshot":
            return {"event": "result", "ok": True, "stage": "done", "snapshot": runner.snapshot()}
        if request.get("action") == "query":
            role = _text(request.get("role"))
            if role:
                try:
                    runner.role_exists(role)
                    rows = rows_as(role)
                except ChangeError as error:
                    return {"event": "result", "ok": False, "stage": "request", "error": str(error)}
            return {"event": "result", "ok": True, "stage": "done", **read_queries(rows, request)}
        if request.get("action") != "apply":
            return {"event": "result", "ok": False, "stage": "request", "error": "Unknown action"}
        result = runner.apply(request)
        if result.get("ok") and request.get("snapshot_after"):
            result["snapshot"] = runner.snapshot()
        return {"event": "result", **result}
    except Exception as error:  # noqa: BLE001 - reported as a result, never as a traceback
        return {"event": "result", "ok": False, "stage": "connect", "error": _message(error)}
    finally:
        # Closing is best effort: the answer is already decided.
        for opened in connections:
            with contextlib.suppress(Exception):
                opened.Close()
        with contextlib.suppress(Exception):
            server.Disconnect()


def main() -> int:
    # -I ignores PYTHONIOENCODING, so the pipes would use the ANSI code page and fail on names
    # like "⋮"; the protocol is UTF-8 both ways.
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    try:
        request = json.loads(sys.stdin.read())
    except ValueError:
        _write({"event": "result", "ok": False, "stage": "request", "error": "Unreadable request"})
        return 2
    if not isinstance(request, dict):
        _write({"event": "result", "ok": False, "stage": "request", "error": "Unreadable request"})
        return 2
    _write(run(request))
    return 0


if __name__ == "__main__":
    sys.exit(main())
