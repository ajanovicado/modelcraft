"""The semantic model beyond measures: columns, tables, relationships, hierarchies, field
parameters, calculation groups, roles, Power Query and model settings (family ``model``).

Like the measure family, every operation here is applied through Power BI Desktop: the handlers
check it against what Studio knows of the model (``ModelInfo`` plus the model's structure, see
``ModelStructure``), rewrite the report's own references when a column or table is renamed, and
``report_editor`` sends the model half to Desktop through ``desktop_model`` (Studio's TOM runner)
in one transaction. What the handlers know can be older than the model Desktop has open, so the
runner checks again against the live model, and Desktop's own error states and test queries have
the last word; the checks here exist to refuse early, before Desktop is opened, in plain words.

How a batch flows (the same shape as a measure batch):
- ``MODEL_HANDLERS`` run in the dry run and again on the head: they validate, rewrite report
  references (renames) and update the ``ModelInfo`` view so a later step of the batch sees a new
  column or table.
- ``MODEL_REPORT_HANDLERS`` are what a model operation does to the report once Desktop applied its
  model half (``apply_report_operations`` on the file Desktop saved): only the renames of a column
  or a table touch the report.
- The formulas in the model that name a renamed column or table (measures, calculated columns and
  tables, role filters, calculation items) are rewritten by ``desktop_model`` against the live
  model, in the same save as the rename.

Registration (done by the integration agent): ``ModelFamilyOperation`` joins ``ReportOperation``,
``MODEL_FAMILY_OPERATIONS`` joins ``REPORT_OPERATIONS`` and ``MODEL_OPERATIONS`` (the editor routes
every op in it through Desktop), ``MODEL_HANDLERS`` joins ``_HANDLERS``, and
``MODEL_REPORT_HANDLERS`` is consulted like ``MEASURE_REPORT_HANDLERS`` when ``report_layer_only``.

Shapes this module writes that no Desktop-authored file in hand confirms are listed in
``UNVERIFIED_SHAPES``; ``evals/imported_edit/model_probe.py`` checks them in Desktop.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter

from ai_powerbi_analyst.contracts import ClosedModel

from .edit_tools import Slot, ToolCard
from .pbir_document import EXTENSIONS, DocumentError, PbirDocument
from .report_edit_context import FieldFacts, Handler, HandlerResult, ModelInfo, _label
from .report_edit_ops import RenameMeasure, operation_classes
from .report_refs import _tokenize, dax_references

# --- values the operations share ------------------------------------------------------------

Name = Annotated[str, Field(min_length=1, max_length=128)]
Ref = Annotated[str, Field(min_length=1, max_length=200)]
Dax = Annotated[str, Field(min_length=1, max_length=20000)]
MCode = Annotated[str, Field(min_length=1, max_length=50000)]
Note = Annotated[str, Field(max_length=2000)]
Short = Annotated[str, Field(max_length=200)]

DataCategory = Literal[
    "uncategorized",
    "address",
    "city",
    "continent",
    "country",
    "county",
    "place",
    "postal_code",
    "state_or_province",
    "latitude",
    "longitude",
    "web_url",
    "image_url",
    "barcode",
]
SummarizeBy = Literal["default", "none", "sum", "min", "max", "count", "average", "distinct_count"]
ColumnType = Literal[
    "text", "whole_number", "decimal_number", "fixed_decimal", "date_time", "true_false"
]
Cardinality = Literal["many_to_one", "one_to_one", "many_to_many"]
CrossFilter = Literal["single", "both"]
RefreshFailure = Literal["ask", "keep", "undo"]

# The strings Desktop writes for a data category (TMDL ``dataCategory``), and the aggregate and type
# enum members of TOM (read from the pinned Microsoft.AnalysisServices.Tabular on 2026-09-29).
DATA_CATEGORIES: dict[str, str] = {
    "uncategorized": "",
    "address": "Address",
    "city": "City",
    "continent": "Continent",
    "country": "Country",
    "county": "County",
    "place": "Place",
    "postal_code": "PostalCode",
    "state_or_province": "StateOrProvince",
    "latitude": "Latitude",
    "longitude": "Longitude",
    "web_url": "WebUrl",
    "image_url": "ImageUrl",
    "barcode": "Barcode",
}
SUMMARIZE_BY: dict[str, str] = {
    "default": "Default",
    "none": "None",
    "sum": "Sum",
    "min": "Min",
    "max": "Max",
    "count": "Count",
    "average": "Average",
    "distinct_count": "DistinctCount",
}
DATA_TYPES: dict[str, str] = {
    "text": "String",
    "whole_number": "Int64",
    "decimal_number": "Double",
    "fixed_decimal": "Decimal",
    "date_time": "DateTime",
    "true_false": "Boolean",
}
# The model annotation that holds a file's "Auto date/time" option (Desktop writes it on the model).
AUTO_DATE_TIME_ANNOTATION = "__PBI_TimeIntelligenceEnabled"

# Shapes Studio writes through TOM that no Desktop-authored file in hand confirms; the model probe
# (evals/imported_edit/model_probe.py) decides each in Desktop. Confirmed by the live probe on
# Desktop 2.157.1354.0 (2026-09-29) and so no longer listed: the field parameter (calculated table
# of (label, NAMEOF(field), order) rows, ParameterMetadata {"version":3,"kind":2} on the fields
# column, sort and group by), the calculation group (Name + hidden Ordinal columns, discourage
# implicit measures) and the date table (dataCategory 'Time', IsKey on the date column).
UNVERIFIED_SHAPES: dict[str, str] = {
    "query_parameter": (
        'Named M expression \'<value> meta [IsParameterQuery=true, Type="Text", '
        "IsParameterQueryRequired=true]' for a new parameter; an existing one keeps its own "
        "meta record and only its value changes"
    ),
    "data_category": "Column dataCategory strings WebUrl, ImageUrl, City, StateOrProvince, ...",
    "summarization_set_by": "Column annotation SummarizationSetBy = User with a summarize-by",
    "query_order": "Model annotation PBI_QueryOrder (a JSON list) updated for a new query",
}


# --- the operations ---------------------------------------------------------------------------


class CreateCalculatedColumn(ClosedModel):
    op: Literal["create_calculated_column"] = "create_calculated_column"
    table: Ref
    name: Name
    expression: Dax
    format_string: Short | None = None
    data_type: ColumnType | None = None
    summarize_by: SummarizeBy | None = None
    folder: Short | None = None
    description: Note | None = None
    hidden: bool | None = None


class SetColumnProperties(ClosedModel):
    """What the Column tools and the Properties pane change. ``sort_by`` "" stops sorting by
    another column; ``expression`` is only for a calculated column."""

    op: Literal["set_column_properties"] = "set_column_properties"
    table: Ref
    name: Ref
    format_string: Short | None = None
    data_type: ColumnType | None = None
    sort_by: Annotated[str, Field(max_length=200)] | None = None
    data_category: DataCategory | None = None
    summarize_by: SummarizeBy | None = None
    folder: Short | None = None
    description: Note | None = None
    hidden: bool | None = None
    key: bool | None = None
    expression: Dax | None = None


class RenameColumn(ClosedModel):
    op: Literal["rename_column"] = "rename_column"
    table: Ref
    name: Ref
    new_name: Name


class DeleteColumn(ClosedModel):
    op: Literal["delete_column"] = "delete_column"
    table: Ref
    name: Ref


class CreateCalculatedTable(ClosedModel):
    op: Literal["create_calculated_table"] = "create_calculated_table"
    name: Name
    expression: Dax
    description: Note | None = None
    hidden: bool | None = None


class SetTableProperties(ClosedModel):
    """Hide or show a table, describe it, or change a calculated table's formula."""

    op: Literal["set_table_properties"] = "set_table_properties"
    table: Ref
    hidden: bool | None = None
    description: Note | None = None
    expression: Dax | None = None


class RenameTable(ClosedModel):
    op: Literal["rename_table"] = "rename_table"
    name: Ref
    new_name: Name


class DeleteTable(ClosedModel):
    """Any table: an imported or calculated one, a field parameter or a calculation group."""

    op: Literal["delete_table"] = "delete_table"
    name: Ref


class MarkDateTable(ClosedModel):
    """Mark a table as the model's date table on ``column``; ``column`` None unmarks it."""

    op: Literal["mark_date_table"] = "mark_date_table"
    table: Ref
    column: Ref | None = None


class CreateRelationship(ClosedModel):
    """``from_*`` is the many side of a many-to-one relationship, as Desktop stores it."""

    op: Literal["create_relationship"] = "create_relationship"
    from_table: Ref
    from_column: Ref
    to_table: Ref
    to_column: Ref
    cardinality: Cardinality = "many_to_one"
    cross_filter: CrossFilter = "single"
    active: bool = True
    # Offered by Desktop only between DirectQuery tables.
    assume_referential_integrity: bool = False
    # "Apply security filter in both directions" (needs cross_filter "both").
    security_filter: CrossFilter = "single"


class UpdateRelationship(ClosedModel):
    """The relationship between the two columns, whichever way round it was created."""

    op: Literal["update_relationship"] = "update_relationship"
    from_table: Ref
    from_column: Ref
    to_table: Ref
    to_column: Ref
    cardinality: Cardinality | None = None
    cross_filter: CrossFilter | None = None
    active: bool | None = None
    assume_referential_integrity: bool | None = None
    security_filter: CrossFilter | None = None


class DeleteRelationship(ClosedModel):
    op: Literal["delete_relationship"] = "delete_relationship"
    from_table: Ref
    from_column: Ref
    to_table: Ref
    to_column: Ref


class HierarchyLevel(ClosedModel):
    column: Ref
    name: Name | None = None  # the column's name when not given


class CreateHierarchy(ClosedModel):
    op: Literal["create_hierarchy"] = "create_hierarchy"
    table: Ref
    name: Name
    levels: list[HierarchyLevel] = Field(min_length=1, max_length=20)


class UpdateHierarchy(ClosedModel):
    """``levels`` is the whole new list (add, remove and reorder in one go)."""

    op: Literal["update_hierarchy"] = "update_hierarchy"
    table: Ref
    name: Ref
    levels: list[HierarchyLevel] | None = Field(default=None, min_length=1, max_length=20)
    new_name: Name | None = None


class DeleteHierarchy(ClosedModel):
    op: Literal["delete_hierarchy"] = "delete_hierarchy"
    table: Ref
    name: Ref


class ParameterField(ClosedModel):
    table: Ref
    name: Ref
    label: Short | None = None  # shown in the slicer; the field's own name when not given


class CreateFieldParameter(ClosedModel):
    op: Literal["create_field_parameter"] = "create_field_parameter"
    name: Name
    fields: list[ParameterField] = Field(min_length=1, max_length=50)


class UpdateFieldParameter(ClosedModel):
    op: Literal["update_field_parameter"] = "update_field_parameter"
    name: Ref
    fields: list[ParameterField] = Field(min_length=1, max_length=50)


class CalculationItemSpec(ClosedModel):
    name: Name
    expression: Dax
    format_string_expression: Annotated[str, Field(max_length=4000)] | None = None


class CreateCalculationGroup(ClosedModel):
    op: Literal["create_calculation_group"] = "create_calculation_group"
    name: Name
    items: list[CalculationItemSpec] = Field(min_length=1, max_length=50)
    precedence: int = Field(default=0, ge=0, le=100000)
    column: Name = "Name"
    description: Note | None = None


class SetCalculationItem(ClosedModel):
    """Add an item (``expression`` required) or change one; ``format_string_expression`` ""
    removes the item's dynamic format string."""

    op: Literal["set_calculation_item"] = "set_calculation_item"
    group: Ref
    name: Ref
    expression: Dax | None = None
    format_string_expression: Annotated[str, Field(max_length=4000)] | None = None
    ordinal: int | None = Field(default=None, ge=0, le=1000)
    new_name: Name | None = None


class DeleteCalculationItem(ClosedModel):
    op: Literal["delete_calculation_item"] = "delete_calculation_item"
    group: Ref
    name: Ref


class UpdateCalculationGroup(ClosedModel):
    op: Literal["update_calculation_group"] = "update_calculation_group"
    name: Ref
    precedence: int | None = Field(default=None, ge=0, le=100000)
    description: Note | None = None


class RoleFilter(ClosedModel):
    """A table filter of a role; an empty ``expression`` removes the table's filter."""

    table: Ref
    expression: Annotated[str, Field(max_length=20000)]


class CreateRole(ClosedModel):
    op: Literal["create_role"] = "create_role"
    name: Name
    filters: list[RoleFilter] = Field(default_factory=list, max_length=100)
    description: Note | None = None
    # People are added to a role in the Power BI service after publishing: always refused here.
    members: list[Short] | None = None


class UpdateRole(ClosedModel):
    op: Literal["update_role"] = "update_role"
    name: Ref
    new_name: Name | None = None
    filters: list[RoleFilter] | None = Field(default=None, max_length=100)
    description: Note | None = None
    members: list[Short] | None = None


class DeleteRole(ClosedModel):
    op: Literal["delete_role"] = "delete_role"
    name: Ref


class SetTableQuery(ClosedModel):
    """A table's Power Query (M). The data is reloaded with it; ``if_refresh_fails`` says what
    happens when the source cannot be reached: ask, keep the new query anyway, or undo it."""

    op: Literal["set_table_query"] = "set_table_query"
    table: Ref
    expression: MCode
    if_refresh_fails: RefreshFailure = "ask"


class SetNamedExpression(ClosedModel):
    """Create or change a query that is not loaded as a table (a shared M expression)."""

    op: Literal["set_named_expression"] = "set_named_expression"
    name: Name
    expression: MCode
    description: Note | None = None


class DeleteNamedExpression(ClosedModel):
    op: Literal["delete_named_expression"] = "delete_named_expression"
    name: Ref


ParameterType = Literal["text", "number", "true_false", "date", "date_time", "time"]


class SetQueryParameter(ClosedModel):
    """Create a Power Query parameter or change its value (used at the next refresh). An
    existing parameter keeps its type, its list of suggested values and whether it is required;
    ``type`` only says what a new one is (dates as ``2024-01-31``, date/times as
    ``2024-01-31 08:30``, times as ``08:30``)."""

    op: Literal["set_query_parameter"] = "set_query_parameter"
    name: Name
    value: str | int | float | bool
    type: ParameterType | None = None
    description: Note | None = None


class RefreshData(ClosedModel):
    """Reload one table's data (``table``) or every table's from their sources."""

    op: Literal["refresh_data"] = "refresh_data"
    table: Ref | None = None


class SetModelOptions(ClosedModel):
    """Discourage implicit measures. ``culture`` and ``auto_date_time`` are read so a request for
    them gets Power BI Desktop's own steps: Studio does not change either (see the handler)."""

    op: Literal["set_model_options"] = "set_model_options"
    discourage_implicit_measures: bool | None = None
    culture: Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")] | None = None
    auto_date_time: bool | None = None


ModelFamilyOperation = (
    CreateCalculatedColumn
    | SetColumnProperties
    | RenameColumn
    | DeleteColumn
    | CreateCalculatedTable
    | SetTableProperties
    | RenameTable
    | DeleteTable
    | MarkDateTable
    | CreateRelationship
    | UpdateRelationship
    | DeleteRelationship
    | CreateHierarchy
    | UpdateHierarchy
    | DeleteHierarchy
    | CreateFieldParameter
    | UpdateFieldParameter
    | CreateCalculationGroup
    | SetCalculationItem
    | DeleteCalculationItem
    | UpdateCalculationGroup
    | CreateRole
    | UpdateRole
    | DeleteRole
    | SetTableQuery
    | SetNamedExpression
    | DeleteNamedExpression
    | SetQueryParameter
    | RefreshData
    | SetModelOptions
)
MODEL_FAMILY_OPERATIONS = operation_classes(ModelFamilyOperation)
_ADAPTER: TypeAdapter[Any] = TypeAdapter(Annotated[ModelFamilyOperation, Field(discriminator="op")])


def parse_model_operation(raw: Mapping[str, Any]) -> ModelFamilyOperation:
    """One operation of this family from its dict (the facade's ``parse_operations`` does this for
    every family once this one is registered)."""
    parsed: ModelFamilyOperation = _ADAPTER.validate_python(dict(raw))
    return parsed


def refresh_policy(operations: list[Any]) -> RefreshFailure | None:
    """What to do when the data refresh a batch asks for fails. None when nothing refreshes.
    A new query decides ("undo" beats "ask" beats "keep"); a plain refresh that fails fails the
    batch ("undo"), and next to a new query it is the same refresh, so the query decides."""
    queries = [op.if_refresh_fails for op in operations if isinstance(op, SetTableQuery)]
    if not queries:
        return "undo" if any(isinstance(op, RefreshData) for op in operations) else None
    if "undo" in queries:
        return "undo"
    return "ask" if "ask" in queries else "keep"


# --- what Studio knows of the model's structure -----------------------------------------------


def _cf(text: str) -> str:
    return text.casefold()


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes")
    return default


@dataclass(frozen=True)
class RelationshipFact:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    active: bool = True
    both: bool = False
    from_many: bool = True
    to_many: bool = False
    # None where the facts do not say.
    cardinality_known: bool = False
    rely: bool | None = None
    security_both: bool | None = None

    def joins(self, a: tuple[str, str], b: tuple[str, str]) -> bool:
        ends = {
            (_cf(self.from_table), _cf(self.from_column)),
            (_cf(self.to_table), _cf(self.to_column)),
        }
        return ends == {(_cf(a[0]), _cf(a[1])), (_cf(b[0]), _cf(b[1]))}

    def describe(self) -> str:
        return f"{self.from_table}[{self.from_column}] and {self.to_table}[{self.to_column}]"


@dataclass
class ModelStructure:
    """The model's objects beyond tables, columns and measures, from the model facts (the saved
    file's metadata, or the live model's snapshot). ``known`` names the sections the facts had:
    a check whose section is unknown is left to Power BI Desktop, and the receipt says so."""

    tables: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    columns: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict[tuple[str, str], dict[str, Any]]
    )
    relationships: list[RelationshipFact] = field(default_factory=list[RelationshipFact])
    hierarchies: dict[tuple[str, str], list[tuple[str, str]]] = field(
        default_factory=dict[tuple[str, str], list[tuple[str, str]]]
    )
    roles: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    role_names: dict[str, str] = field(default_factory=dict[str, str])
    role_descriptions: dict[str, str] = field(default_factory=dict[str, str])
    groups: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    partitions: dict[str, list[dict[str, str]]] = field(
        default_factory=dict[str, list[dict[str, str]]]
    )
    expressions: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    settings: dict[str, Any] = field(default_factory=dict[str, Any])
    # A field parameter's fields in order, (table, name), from the facts' rows.
    parameter_fields: dict[str, list[tuple[str, str]]] = field(
        default_factory=dict[str, list[tuple[str, str]]]
    )
    # Tables with automatic date hierarchies (Desktop's date variations) on their columns.
    variation_tables: set[str] = field(default_factory=set[str])
    known: set[str] = field(default_factory=set[str])

    # --- reading the facts --------------------------------------------------------------------

    @classmethod
    def from_facts(cls, facts: Mapping[str, Any] | None) -> ModelStructure:
        """Tolerant of both shapes Studio has: the model facts read from a saved file (flat lists
        per section) and the runner's live snapshot (tables with nested columns, hierarchies,
        partitions and calculation group). A missing or malformed section stays unknown."""
        found = cls()
        if not isinstance(facts, Mapping):
            return found
        tables = facts.get("tables")
        if isinstance(tables, list):
            found.known.add("tables")
            for row in tables:
                if isinstance(row, Mapping):
                    found._read_table(row)
        columns = facts.get("columns")
        if isinstance(columns, list):
            found.known.add("columns")
            for row in columns:
                if isinstance(row, Mapping):
                    found._read_column(_str(_first(row, "table", "TableName", "tableName")), row)
        relationships = facts.get("relationships")
        if isinstance(relationships, list):
            found.known.add("relationships")
            found.relationships = [r for r in map(_relationship, relationships) if r is not None]
        hierarchies = facts.get("hierarchies")
        if isinstance(hierarchies, list):
            found.known.add("hierarchies")
            for row in hierarchies:
                if isinstance(row, Mapping):
                    found._read_hierarchy(_str(_first(row, "table", "TableName")), row)
        roles = facts.get("roles")
        if isinstance(roles, list):
            found.known.add("roles")
            for row in roles:
                if isinstance(row, Mapping):
                    found._read_role(row)
        groups = _first(facts, "calculationGroups", "calculation_groups")
        if isinstance(groups, list):
            found.known.add("calculation_groups")
            for row in groups:
                if isinstance(row, Mapping):
                    name = _str(_first(row, "table", "name", "TableName"))
                    if name:
                        found.groups[_cf(name)] = _group(name, row)
        parameters = _first(facts, "fieldParameters", "field_parameters")
        if isinstance(parameters, list):
            found.known.add("field_parameters")
            for row in parameters:
                name = _str(_first(row, "table", "name")) if isinstance(row, Mapping) else _str(row)
                if name:
                    found.tables.setdefault(_cf(name), {"name": name})["kind"] = "fieldParameter"
                rows = row.get("rows") if isinstance(row, Mapping) else None
                # No rows (the extractor could not read the DAX) says nothing about the fields.
                if name and isinstance(rows, list) and rows:
                    found.parameter_fields[_cf(name)] = [
                        (_str(r.get("table")), _str(r.get("name")))
                        for r in rows
                        if isinstance(r, Mapping)
                    ]
        variations = facts.get("variations")
        if isinstance(variations, list):
            found.known.add("variations")
            for row in variations:
                if isinstance(row, Mapping) and _str(row.get("table")):
                    found.variation_tables.add(_cf(_str(row["table"])))
        partitions = facts.get("partitions")
        if isinstance(partitions, list):
            found.known.add("partitions")
            for row in partitions:
                if isinstance(row, Mapping):
                    table = _str(_first(row, "table", "TableName"))
                    found.partitions.setdefault(_cf(table), []).append(_partition(row))
        expressions = facts.get("expressions")
        if isinstance(expressions, list):
            found.known.add("expressions")
            for row in expressions:
                if isinstance(row, Mapping) and _str(row.get("name")):
                    entry = {
                        "name": _str(row["name"]),
                        "expression": _str(_first(row, "expression", "Expression")),
                    }
                    description = _known_text(row, "description", "Description")
                    if description is not None:
                        entry["description"] = description
                    found.expressions[_cf(_str(row["name"]))] = entry
        settings = _first(facts, "model", "settings")
        if isinstance(settings, Mapping):
            found.known.add("settings")
            found.settings = dict(settings)
        if "tables" in found.known and any("partitions" in t for t in found.tables.values()):
            found.known.add("partitions")
        if "tables" in found.known and any(
            isinstance(t, Mapping) and isinstance(t.get("columns"), list) for t in tables or []
        ):
            found.known.add("columns")
        if "tables" in found.known and any("hierarchies" in t for t in tables or []):
            found.known.add("hierarchies")
        if "tables" in found.known and any(
            isinstance(t, Mapping) and "calculationGroup" in t for t in tables or []
        ):
            found.known.add("calculation_groups")
            found.known.add("field_parameters")
        return found

    def _read_table(self, row: Mapping[str, Any]) -> None:
        name = _str(_first(row, "name", "TableName"))
        if not name:
            return
        entry = self.tables.setdefault(_cf(name), {"name": name})
        entry["name"] = name
        # None where the facts do not say: a check of that property is then left to Desktop.
        hidden = _first(row, "isHidden", "hidden", "IsHidden")
        entry["hidden"] = None if hidden is None else _bool(hidden)
        category = _first(row, "dataCategory", "DataCategory")
        marked = _first(row, "isDateTable", "dateTable")
        entry["date_table"] = (
            None if marked is None and category is None else _bool(marked) or category == "Time"
        )
        kind = _str(_first(row, "kind"))
        if _bool(row.get("fieldParameter")):
            kind = "fieldParameter"
        elif row.get("calculationGroup"):
            kind = "calculationGroup"
        expression = _first(row, "expression", "Expression")
        # ``pbix_extract --facts`` says ``calculated: true`` / ``source: "calculated"``.
        calculated = row.get("calculated") is True or _str(row.get("source")) == "calculated"
        if (calculated or (isinstance(expression, str) and expression)) and kind in ("", "data"):
            kind = "calculated"
        entry["kind"] = (
            entry.get("kind") if entry.get("kind") == "fieldParameter" else kind or "data"
        )
        if isinstance(expression, str):
            entry["expression"] = expression
        description = _known_text(row, "description", "Description")
        if description is not None:
            entry["description"] = description
        for column in row.get("columns") or []:
            if isinstance(column, Mapping):
                self._read_column(name, column)
        for hierarchy in row.get("hierarchies") or []:
            if isinstance(hierarchy, Mapping):
                self._read_hierarchy(name, hierarchy)
        partitions = row.get("partitions")
        if isinstance(partitions, list):
            entry["partitions"] = True
            self.partitions[_cf(name)] = [
                _partition(p) for p in partitions if isinstance(p, Mapping)
            ]
            dax = [p for p in self.partitions[_cf(name)] if p["kind"] == "dax"]
            if dax and "expression" not in entry:
                entry["expression"] = dax[0]["expression"]
        group = row.get("calculationGroup")
        if isinstance(group, Mapping):
            self.groups[_cf(name)] = _group(name, group)

    def _read_column(self, table: str, row: Mapping[str, Any]) -> None:
        name = _str(_first(row, "name", "ColumnName", "columnName"))
        if not table or not name:
            return
        kind = _str(_first(row, "type", "kind"))
        expression = _first(row, "expression", "calculated", "Expression")
        sort_keys = ("sortByColumn", "sortBy", "SortByColumn")
        hidden_keys = ("isHidden", "hidden")
        # A key that is present says something even when empty ("no sort-by column"); a missing
        # key leaves the property unknown.
        sort_by = _str(_first(row, *sort_keys)) if any(k in row for k in sort_keys) else None
        hidden = _bool(_first(row, *hidden_keys)) if any(k in row for k in hidden_keys) else None
        key_keys = ("isKey", "key", "IsKey")
        self.columns[(_cf(table), _cf(name))] = {
            "table": table,
            "name": name,
            "data_type": _first(row, "dataType", "DataType"),
            "calculated": kind in ("Calculated", "calculated")
            or (isinstance(expression, str) and bool(expression)),
            "expression": expression if isinstance(expression, str) else None,
            "sort_by": sort_by,
            "hidden": hidden,
            "key": _bool(_first(row, *key_keys)),
            # The properties the after-save check compares; None where the facts do not say.
            "key_known": _bool(_first(row, *key_keys)) if any(k in row for k in key_keys) else None,
            "format_string": _known_text(row, "formatString", "FormatString"),
            "data_category": _known_text(row, "dataCategory", "DataCategory"),
            "summarize_by": _known_text(row, "summarizeBy", "SummarizeBy"),
            "folder": _known_text(row, "displayFolder", "DisplayFolder"),
            "description": _known_text(row, "description", "Description"),
        }

    def _read_hierarchy(self, table: str, row: Mapping[str, Any]) -> None:
        name = _str(_first(row, "name", "HierarchyName"))
        if not table or not name:
            return
        levels: list[tuple[str, str]] = []
        for level in row.get("levels") or []:
            if isinstance(level, Mapping):
                column = _str(_first(level, "column", "ColumnName"))
                levels.append((_str(_first(level, "name", "LevelName")) or column, column))
            elif isinstance(level, str):
                levels.append((level, level))
        self.hierarchies[(_cf(table), _cf(name))] = levels
        self.tables.setdefault(_cf(table), {"name": table})

    def _read_role(self, row: Mapping[str, Any]) -> None:
        name = _str(_first(row, "name", "RoleName"))
        if not name:
            return
        filters: dict[str, str] = {}
        raw = _first(row, "tablePermissions", "filters", "tables")
        if isinstance(raw, Mapping):
            filters = {_str(k): _str(v) for k, v in raw.items()}
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, Mapping):
                    table = _str(_first(item, "table", "tableName", "TableName"))
                    filters[table] = _str(_first(item, "expression", "filterExpression", "filter"))
        self.roles[_cf(name)] = filters
        self.role_names[_cf(name)] = name
        description = _known_text(row, "description", "Description")
        if description is not None:
            self.role_descriptions[_cf(name)] = description

    # --- questions the handlers ask -------------------------------------------------------------

    def table_kind(self, table: str) -> str:
        return _str(self.tables.get(_cf(table), {}).get("kind")) or "unknown"

    def active_path(self, a: str, b: str, *, ignore: RelationshipFact | None = None) -> list[str]:
        """Tables on an existing path of active relationships between tables ``a`` and ``b``
        (either direction), [] when there is none. Another active relationship between two tables
        that are already connected this way is the ambiguity Desktop refuses."""
        graph: dict[str, list[str]] = {}
        names: dict[str, str] = {}
        for rel in self.relationships:
            if not rel.active or rel is ignore:
                continue
            left, right = _cf(rel.from_table), _cf(rel.to_table)
            names[left], names[right] = rel.from_table, rel.to_table
            graph.setdefault(left, []).append(right)
            graph.setdefault(right, []).append(left)
        start, goal = _cf(a), _cf(b)
        previous: dict[str, str | None] = {start: None}
        queue = [start]
        while queue:
            current = queue.pop(0)
            if current == goal:
                path: list[str] = []
                node: str | None = current
                while node is not None:
                    path.append(names.get(node, node))
                    node = previous[node]
                return list(reversed(path))
            for neighbour in graph.get(current, []):
                if neighbour not in previous:
                    previous[neighbour] = current
                    queue.append(neighbour)
        return []

    def relationship(self, a: tuple[str, str], b: tuple[str, str]) -> RelationshipFact | None:
        return next((r for r in self.relationships if r.joins(a, b)), None)


def _relationship(row: Any) -> RelationshipFact | None:
    if not isinstance(row, Mapping):
        return None
    ends = [
        _str(_first(row, key, pascal))
        for key, pascal in (
            ("fromTable", "FromTableName"),
            ("fromColumn", "FromColumnName"),
            ("toTable", "ToTableName"),
            ("toColumn", "ToColumnName"),
        )
    ]
    if not all(ends):
        return None
    cardinality = _str(_first(row, "cardinality", "Cardinality"))
    from_end = _str(_first(row, "fromCardinality", "FromCardinality"))
    to_end = _str(_first(row, "toCardinality", "ToCardinality"))
    if cardinality and ":" in cardinality:
        from_end, to_end = cardinality.split(":", 1)
    cross = _str(_first(row, "crossFilteringBehavior", "CrossFilteringBehavior", "crossFilter"))
    rely_keys = ("relyOnReferentialIntegrity", "RelyOnReferentialIntegrity")
    security_keys = ("securityFilteringBehavior", "SecurityFilteringBehavior", "securityFilter")
    security = _first(row, *security_keys)
    return RelationshipFact(
        ends[0],
        ends[1],
        ends[2],
        ends[3],
        active=_bool(_first(row, "isActive", "IsActive", "active"), True),
        both=cross.casefold() in ("both", "bothdirections", "2"),
        from_many=from_end.casefold() not in ("1", "one"),
        to_many=to_end.casefold() in ("m", "many", "*", "2"),
        cardinality_known=bool(from_end and to_end),
        rely=_bool(_first(row, *rely_keys)) if any(k in row for k in rely_keys) else None,
        security_both=None
        if security is None
        else _str(security).casefold() in ("both", "bothdirections", "2"),
    )


def _mode(row: Mapping[str, Any]) -> str:
    """A partition's storage mode in one spelling: import, directquery, dual, default or ''."""
    return _str(_first(row, "mode", "Mode")).casefold().replace("_", "")


def _partition(row: Mapping[str, Any]) -> dict[str, str]:
    kind = _str(_first(row, "sourceType", "source", "kind", "type")).casefold()
    normal = (
        "m"
        if kind in ("m", "mpartitionsource", "query")
        else "dax"
        if kind in ("calculated", "dax", "calculatedpartitionsource")
        else "calculationGroup"
        if kind in ("calculationgroup", "calculationgroupsource")
        else kind
    )
    return {
        "name": _str(_first(row, "name", "PartitionName")),
        "kind": normal,
        "expression": _str(_first(row, "expression", "source", "Expression", "QueryDefinition")),
        "mode": _mode(row),
    }


def _known_text(row: Mapping[str, Any], *keys: str) -> str | None:
    """The text under the first of ``keys`` the row has (None or "" is "none"); None when the
    row has none of them (the facts do not say)."""
    for key in keys:
        if key in row:
            return _str(row[key])
    return None


def _group(name: str, row: Mapping[str, Any]) -> dict[str, Any]:
    items: dict[str, dict[str, Any]] = {}
    for item in row.get("items") or row.get("calculationItems") or []:
        if isinstance(item, Mapping) and _str(item.get("name")):
            items[_cf(_str(item["name"]))] = {
                "name": _str(item["name"]),
                "expression": _str(item.get("expression")),
                "ordinal": item.get("ordinal"),
                "format_string_expression": _known_text(item, "formatStringExpression"),
            }
    precedence = _first(row, "precedence", "Precedence")
    return {
        "name": name,
        "precedence": int(precedence) if isinstance(precedence, int | float) else None,
        "description": _known_text(row, "description", "Description"),
        "items": items,
    }


# The structure travels with the ModelInfo it describes. ModelInfo has no field for it yet (the
# shared context is another owner's), so it is kept in the instance's own namespace; a copy made
# with ``with_measures`` starts without one, which only means "unknown".
_STRUCTURE_KEY = "_model_structure"


def attach_structure(model: ModelInfo, facts: Mapping[str, Any] | None) -> ModelStructure:
    """Read the model facts into ``model``'s structure (``facts`` may be the types payload with
    extra sections, the saved file's model facts or the live snapshot)."""
    structure = ModelStructure.from_facts(facts)
    vars(model)[_STRUCTURE_KEY] = structure
    return structure


def model_structure(model: ModelInfo) -> ModelStructure:
    """What ``attach_structure`` read for ``model``, else an empty (all unknown) structure that is
    kept with it so the handlers of one batch share it."""
    shared = getattr(model, "structure", None)
    if isinstance(shared, ModelStructure):
        return shared
    if isinstance(shared, Mapping):
        return attach_structure(model, shared)
    found = vars(model).get(_STRUCTURE_KEY)
    if isinstance(found, ModelStructure):
        return found
    structure = ModelStructure()
    vars(model)[_STRUCTURE_KEY] = structure
    return structure


# --- checks shared by the handlers ------------------------------------------------------------

MAX_NAME = 128
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANY_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _refuse(problem: str | None) -> None:
    if problem is not None:
        raise DocumentError(problem)


def _name_problem(name: str, what: str) -> str | None:
    if not name.strip():
        return f"Give the {what} a name"
    if name != name.strip():
        return f"The {what} name cannot start or end with a space"
    if len(name) > MAX_NAME:
        return f"The {what} name is too long: Power BI names hold at most {MAX_NAME} characters"
    if _ANY_CONTROL.search(name):
        return f"The {what} name cannot contain line breaks or other control characters"
    return None


def _table_name_problem(name: str) -> str | None:
    # Which other characters Desktop refuses in a table name was not measured; the engine refuses
    # them when the change is saved, and the runner then undoes the batch.
    return _name_problem(name, "table")


def _text_problem(text: str, what: str) -> str | None:
    if _CONTROL.search(text):
        return f"The {what} cannot contain control characters"
    return None


def _dax_problem(text: str, what: str = "formula") -> str | None:
    """Why the DAX cannot be well formed, from its quotes, brackets and comments alone. Whether it
    is right is settled by Desktop (review P36: the local engine is advisory)."""
    if not text.strip():
        return f"The {what} is empty"
    problem = _text_problem(text, what)
    if problem is not None:
        return problem
    depth, index, size = 0, 0, len(text)
    while index < size:
        char, pair = text[index], text[index : index + 2]
        if pair in ("//", "--"):
            end = text.find("\n", index)
            index = size if end < 0 else end
            continue
        if pair == "/*":
            end = text.find("*/", index + 2)
            if end < 0:
                return f"The {what} has a comment that is never closed"
            index = end + 2
            continue
        if char in "\"'[":
            closer = "]" if char == "[" else char
            j = index + 1
            while j < size:
                if text[j] == closer:
                    if text[j + 1 : j + 2] == closer:
                        j += 2
                        continue
                    break
                j += 1
            if j >= size:
                word = {'"': "text", "'": "table name", "[": "bracket"}[char]
                return f"The {what} has a {word} that is never closed"
            index = j + 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return f"The {what} closes a parenthesis that was never opened"
        index += 1
    return f"The {what} has a parenthesis that is never closed" if depth else None


def _pasted_definition(name: str, expression: str) -> str | None:
    pattern = r"^\s*'?\[?" + re.escape(name) + r"\]?'?\s*=(?!=)"
    if re.match(pattern, expression, flags=re.IGNORECASE):
        return "Give just the formula, without the name and equals sign before it"
    return None


def _quote_table(table: str) -> str:
    return "'" + table.replace("'", "''") + "'"


def _column_ref(table: str, column: str) -> str:
    return _quote_table(table) + "[" + column.replace("]", "]]") + "]"


def _and_list(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def _table(model: ModelInfo, name: str) -> str:
    found = model.table(name)
    if found is None:
        raise DocumentError(f"The model has no table {name!r}")
    return found


def _columns_of(model: ModelInfo, table: str) -> dict[str, str]:
    for known, columns in model.columns.items():
        if _cf(known) == _cf(table):
            return columns
    return {}


def _column(model: ModelInfo, table: str, name: str) -> str:
    home = _table(model, table)
    found = next((c for c in _columns_of(model, home) if _cf(c) == _cf(name)), None)
    if found is None:
        # Never a column of another table with that name: say where it is instead.
        others = [
            t
            for t, columns in model.columns.items()
            if _cf(t) != _cf(home) and any(_cf(c) == _cf(name) for c in columns)
        ]
        where = (
            f" ({_and_list(others)} {'has' if len(others) == 1 else 'have'} one)" if others else ""
        )
        raise DocumentError(f"The table {home} has no column {name!r}{where}")
    return found


def _field_taken(
    model: ModelInfo, table: str, name: str, *, except_column: str | None = None
) -> str | None:
    home = model.table(table) or table
    for column in _columns_of(model, home):
        if _cf(column) == _cf(name) and (
            except_column is None or _cf(column) != _cf(except_column)
        ):
            return f"The table {home} already has a column called {name!r}"
    measure_home = model.measure_table(name)
    if measure_home is not None:
        return f"The model already has a measure called {name!r} (in {measure_home})"
    return None


def _column_facts(
    model: ModelInfo, structure: ModelStructure, table: str, column: str
) -> dict[str, Any]:
    return structure.columns.get((_cf(table), _cf(column)), {})


def _is_calculated_column(
    model: ModelInfo, structure: ModelStructure, table: str, column: str
) -> bool | None:
    if f"{table}[{column}]" in model.calculated or any(
        _cf(k) == _cf(f"{table}[{column}]") for k in model.calculated
    ):
        return True
    facts = _column_facts(model, structure, table, column)
    if facts:
        return bool(facts.get("calculated"))
    if "columns" in structure.known or model.calculated:
        return False
    return None


def _data_type_word(model: ModelInfo, structure: ModelStructure, table: str, column: str) -> str:
    """'date', 'text', 'number', 'true_false' or '' (unknown), from the facts or the import's
    dtype."""
    raw: Any = _column_facts(model, structure, table, column).get("data_type")
    if raw is None:
        raw = model.data_type(table, column)
    text = _str(raw).casefold()
    if raw == 9 or text in ("9", "datetime", "date_time") or "datetime" in text or text == "date":
        return "date"
    if raw == 2 or text in ("2", "string", "text", "object"):
        return "text"
    if raw == 11 or text in ("11", "boolean", "bool"):
        return "true_false"
    if (
        raw in (6, 8, 10)
        or text in ("6", "8", "10", "int64", "double", "decimal")
        or any(t in text for t in ("int", "float", "decimal"))
    ):
        return "number"
    return ""


def _unknown_warning(structure: ModelStructure, section: str, what: str) -> list[str]:
    if section in structure.known:
        return []
    return [
        f"Studio could not read the model's {what}, so Power BI Desktop checks this change instead."
    ]


# --- DAX: which formulas name a column or a table, and renaming them ---------------------------

# Words that can stand alone in DAX without naming a table.
_DAX_WORDS = frozenset(
    {
        "VAR",
        "RETURN",
        "IN",
        "NOT",
        "AND",
        "OR",
        "EVALUATE",
        "DEFINE",
        "MEASURE",
        "COLUMN",
        "TABLE",
        "ORDER",
        "BY",
        "ASC",
        "DESC",
        "START",
        "AT",
        "TRUE",
        "FALSE",
        "SKIP",
        "DENSE",
        "BOTH",
        "NONE",
        "ONEWAY",
        "ONEWAY_LEFTFILTERSRIGHT",
        "ONEWAY_RIGHTFILTERSLEFT",
        "STRING",
        "INTEGER",
        "DOUBLE",
        "CURRENCY",
        "BOOLEAN",
        "DATETIME",
        "REL",
        "ABS",
        "DAY",
        "WEEK",
        "MONTH",
        "QUARTER",
        "YEAR",
        "BLANKS",
        "FIRST",
        "LAST",
        "DEFAULT",
    }
)


def _table_tokens(text: str) -> list[tuple[int, int, str]]:
    """(start, end, name) of every token that names a table: 'Quoted', Word[Column] and a bare
    word used as a table argument (not a function, a keyword or a variable)."""
    tokens = _tokenize(text)
    variables = {
        tokens[i + 1].text.casefold()
        for i, token in enumerate(tokens[:-1])
        if token.kind == "word" and token.text.upper() == "VAR" and tokens[i + 1].kind == "word"
    }
    found: list[tuple[int, int, str]] = []
    for index, token in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if token.kind == "quoted":
            found.append((token.start, token.end, token.text))
        elif token.kind == "word":
            if (
                following is not None
                and following.kind == "bracket"
                and following.start == token.end
            ):
                found.append((token.start, token.end, token.text))
                continue
            called = following is not None and following.text == "("
            keyword = token.text.upper() in _DAX_WORDS or token.text.casefold() in variables
            dotted = index > 0 and tokens[index - 1].text == "."
            if not (called or keyword or dotted):
                found.append((token.start, token.end, token.text))
    return found


def dax_uses_table(text: str, table: str) -> bool:
    return any(_cf(name) == _cf(table) for _, _, name in _table_tokens(text))


def rename_dax_table(text: str, old: str, new: str) -> tuple[str, int]:
    """DAX with every reference to table ``old`` pointed at ``new`` (always quoted)."""
    pieces: list[str] = []
    last = count = 0
    for start, end, name in _table_tokens(text):
        if _cf(name) != _cf(old):
            continue
        pieces.append(text[last:start])
        pieces.append(_quote_table(new))
        last = end
        count += 1
    pieces.append(text[last:])
    return "".join(pieces), count


def dax_uses_column(text: str, table: str, column: str, *, own_table: str | None = None) -> bool:
    return rename_dax_column(text, table, column, column, own_table=own_table)[1] > 0


def rename_dax_column(
    text: str,
    table: str,
    old: str,
    new: str,
    *,
    own_table: str | None = None,
    measures: frozenset[str] = frozenset(),
) -> tuple[str, int]:
    """DAX with ``table[old]`` pointed at ``[new]``. A bare ``[old]`` is the column only in a
    formula that lives in the same table (a calculated column or a role filter of ``table``) and
    when no measure has that name: in a measure a bare name is a measure."""
    pieces: list[str] = []
    last = count = 0
    bare_is_column = (
        own_table is not None and _cf(own_table) == _cf(table) and _cf(old) not in measures
    )
    for ref in dax_references(text):
        if ref.name is None or _cf(ref.name) != _cf(old):
            continue
        if ref.table is None and not bare_is_column:
            continue
        if ref.table is not None and _cf(ref.table) != _cf(table):
            continue
        pieces.append(text[last : ref.start])
        pieces.append("[" + new.replace("]", "]]") + "]")
        last = ref.end
        count += 1
    pieces.append(text[last:])
    return "".join(pieces), count


def dax_uses_relationship(text: str, rel: RelationshipFact) -> bool:
    """True when USERELATIONSHIP or CROSSFILTER in ``text`` names both columns of ``rel``."""
    tokens = _tokenize(text)
    for index, token in enumerate(tokens):
        if not (
            token.kind == "word"
            and token.text.upper() in ("USERELATIONSHIP", "CROSSFILTER")
            and index + 1 < len(tokens)
            and tokens[index + 1].text == "("
        ):
            continue
        depth, end = 0, len(text)
        for later in tokens[index + 1 :]:
            if later.text == "(":
                depth += 1
            elif later.text == ")":
                depth -= 1
                if depth == 0:
                    end = later.end
                    break
        inside = text[token.start : end]
        named = {(_cf(r.table or ""), _cf(r.name or "")) for r in dax_references(inside)}
        if {
            (_cf(rel.from_table), _cf(rel.from_column)),
            (_cf(rel.to_table), _cf(rel.to_column)),
        } <= named:
            return True
    return False


@dataclass(frozen=True)
class DaxSource:
    """A formula in the model: what it is (for messages), the table it lives in, its text."""

    label: str
    table: str | None
    text: str
    kind: str  # measure | column | table | role | item


def dax_sources(model: ModelInfo, structure: ModelStructure) -> list[DaxSource]:
    """Every formula Studio knows of: measures, calculated columns and tables, role filters and
    calculation items."""
    found: list[DaxSource] = []
    for name, text in model.measure_expressions.items():
        found.append(DaxSource(f"the measure [{name}]", model.measures.get(name), text, "measure"))
    for key, text in model.calculated.items():
        if key.rstrip().endswith("]"):
            table, _, rest = key.partition("[")
            column = rest[:-1]
            found.append(
                DaxSource(f"the calculated column {table}[{column}]", table, text, "column")
            )
        else:
            found.append(DaxSource(f"the calculated table '{key}'", key, text, "table"))
    for role_key, filters in structure.roles.items():
        role = structure.role_names.get(role_key, role_key)
        for table, text in filters.items():
            if text:
                found.append(
                    DaxSource(f"the role '{role}' (filter on {table})", table, text, "role")
                )
    for group in structure.groups.values():
        for item in group["items"].values():
            found.append(
                DaxSource(
                    f"the calculation item '{item['name']}' of '{group['name']}'",
                    group["name"],
                    item["expression"],
                    "item",
                )
            )
    return found


def _listing(labels: list[str]) -> str:
    shown = ", ".join(labels[:3])
    return shown + (f" and {len(labels) - 3} more" if len(labels) > 3 else "")


# --- the report's references to columns, tables and hierarchies --------------------------------


def _parts(document: PbirDocument) -> Iterator[tuple[str, dict[str, Any], bool]]:
    """(part name, live JSON, owned by the document) for every report part that can name a field."""
    for name, data, _ in document.iter_json_parts():
        yield name, data, True
    extensions = document.extensions
    if extensions:
        yield EXTENSIONS, extensions, False


def _persist(document: PbirDocument, name: str, data: dict[str, Any], owned: bool) -> None:
    if owned:
        document.mark_dirty(name)
    else:
        document.package.write_json(name, data)


def _entity(body: Mapping[str, Any], scopes: list[dict[str, str]]) -> str | None:
    expression = body.get("Expression")
    source = expression.get("SourceRef") if isinstance(expression, Mapping) else None
    if not isinstance(source, Mapping):
        return None
    entity = source.get("Entity")
    if isinstance(entity, str):
        return entity
    alias = source.get("Source")
    if isinstance(alias, str):
        for scope in reversed(scopes):
            if alias in scope:
                return scope[alias]
    return None


_Visit = Callable[[str, dict[str, Any], dict[str, Any] | None, str | None], None]


def _scan(
    node: Any, scopes: list[dict[str, str]], parent: dict[str, Any] | None, visit: _Visit
) -> None:
    """``visit(kind, body, projection, entity)`` for every Column, Measure, Hierarchy and
    HierarchyLevel expression; a ``From`` list scopes the aliases below it (filters name tables
    through ``SourceRef.Source``).

    An automatic date hierarchy (a visual on OrderDate's Year, Quarter, Month) names its column
    only inside the hierarchy: HierarchyLevel > Hierarchy > Expression > PropertyVariationSource
    {Expression: {SourceRef}, Name: "Variation", Property: "OrderDate"} (as ``report_roles``
    resolves it). That ``PropertyVariationSource`` is visited as a Column: its ``Property`` is
    the column, so renames rewrite it and delete guards, ``report_uses`` and
    ``field_references`` see it."""
    if isinstance(node, list):
        for item in node:
            _scan(item, scopes, None, visit)
        return
    if not isinstance(node, dict):
        return
    pushed = False
    sources = node.get("From")
    if isinstance(sources, list):
        scopes.append(
            {
                str(e["Name"]): str(e["Entity"])
                for e in sources
                if isinstance(e, dict)
                and isinstance(e.get("Name"), str)
                and isinstance(e.get("Entity"), str)
            }
        )
        pushed = True
    for kind in ("Column", "Measure", "Hierarchy"):
        body = node.get(kind)
        if isinstance(body, dict) and isinstance(body.get("Expression"), dict):
            visit(kind, body, parent if parent is not None else node, _entity(body, scopes))
    variation = node.get("PropertyVariationSource")
    if (
        isinstance(variation, dict)
        and isinstance(variation.get("Expression"), dict)
        and isinstance(variation.get("Property"), str)
    ):
        visit(
            "Column", variation, parent if parent is not None else node, _entity(variation, scopes)
        )
    level = node.get("HierarchyLevel")
    if isinstance(level, dict):
        inner = (
            level.get("Expression", {}).get("Hierarchy")
            if isinstance(level.get("Expression"), dict)
            else None
        )
        if isinstance(inner, dict):
            visit(
                "HierarchyLevel",
                level,
                parent if parent is not None else node,
                _entity(inner, scopes),
            )
    for key, value in node.items():
        _scan(value, scopes, node if key == "field" else None, visit)
    if pushed:
        scopes.pop()


def _part_label(document: PbirDocument, name: str, data: Mapping[str, Any]) -> str:
    match = re.fullmatch(r"Report/definition/pages/([^/]+)/visuals/([^/]+)/visual\.json", name)
    if match:
        try:
            page = document.pages[match.group(1)].display_name
        except KeyError:
            page = match.group(1)
        return f"{_label(dict(data), match.group(2)[:8])} on page '{page}'"
    match = re.fullmatch(r"Report/definition/pages/([^/]+)/page\.json", name)
    if match:
        return f"a filter on page '{data.get('displayName') or match.group(1)}'"
    if name.endswith(".bookmark.json"):
        return f"the bookmark '{data.get('displayName') or name.rsplit('/', 1)[-1]}'"
    if name == EXTENSIONS:
        return "a report measure"
    return "a report-level filter"


def report_uses(
    document: PbirDocument,
    matches: Callable[[str, dict[str, Any], str | None], bool],
) -> list[str]:
    """Where the report uses fields ``matches(kind, body, entity)`` accepts, one label per part."""
    found: list[str] = []
    for name, data, _ in _parts(document):
        if name == EXTENSIONS:
            continue
        hit = False

        def visit(
            kind: str, body: dict[str, Any], projection: dict[str, Any] | None, entity: str | None
        ) -> None:
            nonlocal hit
            if not hit and matches(kind, body, entity):
                hit = True

        _scan(data, [], None, visit)
        if hit:
            label = _part_label(document, name, data)
            if label not in found:
                found.append(label)
    return found


def _extension_dax_uses(document: PbirDocument, test: Callable[[str], bool]) -> list[str]:
    extensions = document.extensions
    found: list[str] = []
    for entity in extensions.get("entities") or [] if isinstance(extensions, dict) else []:
        for measure in entity.get("measures") or [] if isinstance(entity, dict) else []:
            expression = measure.get("expression") if isinstance(measure, dict) else None
            if isinstance(expression, str) and test(expression):
                found.append(f"the report measure [{measure.get('name')}]")
    return found


def rename_column_references(document: PbirDocument, table: str, old: str, new: str) -> int:
    """Point every report reference to ``table[old]`` at ``new``: fields in visuals, sorts,
    formatting rules, filters (also through ``From`` aliases), bookmarks and report measures.
    ``queryRef`` and selector metadata stay as they are (Desktop keeps them stable across a
    measure rename; the same is assumed for a column); ``nativeQueryRef`` follows when it was the
    old name and the field has no display name. Returns how many references changed."""
    total = 0
    for name, data, owned in _parts(document):
        count = 0

        def visit(
            kind: str, body: dict[str, Any], projection: dict[str, Any] | None, entity: str | None
        ) -> None:
            nonlocal count
            if kind != "Column" or entity is None or _cf(entity) != _cf(table):
                return
            if _cf(str(body.get("Property"))) != _cf(old):
                return
            body["Property"] = new
            if (
                projection is not None
                and projection.get("nativeQueryRef") == old
                and "displayName" not in projection
            ):
                projection["nativeQueryRef"] = new
            count += 1

        if name == EXTENSIONS:
            count += _rename_extension_dax(
                data, lambda text: rename_dax_column(text, table, old, new)
            )
        else:
            _scan(data, [], None, visit)
        if count:
            _persist(document, name, data, owned)
            total += count
    return total


def _rename_extension_dax(data: Any, rename: Callable[[str], tuple[str, int]]) -> int:
    count = 0
    for entity in data.get("entities") or [] if isinstance(data, dict) else []:
        for measure in entity.get("measures") or [] if isinstance(entity, dict) else []:
            if isinstance(measure, dict) and isinstance(measure.get("expression"), str):
                text, changed = rename(measure["expression"])
                if changed:
                    measure["expression"] = text
                    count += changed
    return count


def rename_table_references(document: PbirDocument, old: str, new: str) -> int:
    """Point every report reference to table ``old`` at ``new``: every ``SourceRef.Entity`` and
    every ``From`` entity, in visuals, filters, bookmarks and pages, and the report measures that
    live on the table or name it in DAX. Returns how many references changed."""
    total = 0

    def walk(node: Any) -> int:
        changed = 0
        if isinstance(node, dict):
            source = node.get("SourceRef")
            if (
                isinstance(source, dict)
                and isinstance(source.get("Entity"), str)
                and _cf(source["Entity"]) == _cf(old)
            ):
                source["Entity"] = new
                changed += 1
            sources = node.get("From")
            if isinstance(sources, list):
                for item in sources:
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("Entity"), str)
                        and _cf(item["Entity"]) == _cf(old)
                    ):
                        item["Entity"] = new
                        changed += 1
            for value in node.values():
                changed += walk(value)
        elif isinstance(node, list):
            for value in node:
                changed += walk(value)
        return changed

    for name, data, owned in _parts(document):
        if name == EXTENSIONS:
            count = _rename_extension_dax(data, lambda text: rename_dax_table(text, old, new))
            for entity in data.get("entities") or []:
                if (
                    isinstance(entity, dict)
                    and isinstance(entity.get("name"), str)
                    and _cf(entity["name"]) == _cf(old)
                ):
                    entity["name"] = new
                    count += 1
        else:
            count = walk(data)
        if count:
            _persist(document, name, data, owned)
            total += count
    return total


def field_references(document: PbirDocument) -> list[tuple[str, str, str]]:
    """(kind, table, name) of every Column and Measure the report parts refer to, with the table
    resolved through ``From`` aliases; report measures' DAX is not included."""
    found: list[tuple[str, str, str]] = []
    for name, data, _ in _parts(document):
        if name == EXTENSIONS:
            continue

        def visit(
            kind: str, body: dict[str, Any], projection: dict[str, Any] | None, entity: str | None
        ) -> None:
            prop = body.get("Property")
            if kind in ("Column", "Measure") and entity is not None and isinstance(prop, str):
                found.append((kind, entity, prop))

        _scan(data, [], None, visit)
    return found


# --- keeping the ModelInfo view current within a batch ----------------------------------------


def _move_column(model: ModelInfo, table: str, old: str, new: str) -> None:
    columns = _columns_of(model, table)
    real = next((c for c in columns if _cf(c) == _cf(old)), old)
    columns[new] = columns.pop(real, "")
    for key in [k for k in model.calculated if _cf(k) == _cf(f"{table}[{real}]")]:
        model.calculated[f"{table}[{new}]"] = model.calculated.pop(key)
    facts = model.facts.pop((_cf(table), _cf(real)), None)
    if facts is not None:
        model.facts[(_cf(table), _cf(new))] = facts


def _drop_column(model: ModelInfo, table: str, name: str) -> None:
    columns = _columns_of(model, table)
    for column in [c for c in columns if _cf(c) == _cf(name)]:
        del columns[column]
    for key in [k for k in model.calculated if _cf(k) == _cf(f"{table}[{name}]")]:
        del model.calculated[key]
    model.facts.pop((_cf(table), _cf(name)), None)


def _add_table(
    model: ModelInfo, name: str, columns: Mapping[str, str], expression: str | None
) -> None:
    model.columns[name] = dict(columns)
    if expression is not None:
        model.calculated[name] = expression


def _rename_table_in_model(model: ModelInfo, old: str, new: str) -> None:
    real = model.table(old) or old
    if real in model.columns:
        model.columns[new] = model.columns.pop(real)
    for measure, home in list(model.measures.items()):
        if _cf(home) == _cf(real):
            model.measures[measure] = new
    for text in list(model.calculated):
        table, bracket, rest = text.partition("[")
        if _cf(table) == _cf(real):
            model.calculated[new + bracket + rest] = model.calculated.pop(text)
    for key in [k for k in model.facts if k[0] == _cf(real)]:
        model.facts[(_cf(new), key[1])] = model.facts.pop(key)


def _drop_table(model: ModelInfo, name: str) -> None:
    real = model.table(name) or name
    model.columns.pop(real, None)
    for measure in [m for m, home in model.measures.items() if _cf(home) == _cf(real)]:
        del model.measures[measure]
        model.measure_expressions.pop(measure, None)
    for text in list(model.calculated):
        if _cf(text.partition("[")[0]) == _cf(real):
            del model.calculated[text]
    for key in [k for k in model.facts if k[0] == _cf(real)]:
        del model.facts[key]


# --- handlers: columns ------------------------------------------------------------------------


def _check_formula(expression: str, name: str) -> None:
    _refuse(_dax_problem(expression))
    _refuse(_pasted_definition(name, expression))


def _unknown_refs_warning(model: ModelInfo, expression: str) -> list[str]:
    missing: list[str] = []
    for ref in dax_references(expression):
        if ref.table is None or ref.name is None:
            continue
        table = model.table(ref.table)
        if table is None:
            continue
        if any(_cf(c) == _cf(ref.name) for c in _columns_of(model, table)) or model.measure_table(
            ref.name
        ):
            continue
        text = f"{ref.table}[{ref.name}]"
        if text not in missing:
            missing.append(text)
    if not missing:
        return []
    return [
        f"The formula uses {_listing(missing)}, which Studio cannot find in the model. Power BI "
        "checks it, and the change is not kept if Power BI rejects it."
    ]


def _create_calculated_column(
    document: PbirDocument, op: CreateCalculatedColumn, model: ModelInfo
) -> HandlerResult:
    _refuse(_name_problem(op.name, "column"))
    table = _table(model, op.table)
    _refuse(_field_taken(model, table, op.name))
    _check_formula(op.expression, op.name)
    for text, what in (
        (op.format_string, "format string"),
        (op.folder, "display folder"),
        (op.description, "description"),
    ):
        if text is not None:
            _refuse(_text_problem(text, what))
    if op.format_string is not None and op.format_string.count('"') % 2:
        raise DocumentError("The format string has a quote that is never closed")
    warnings = _unknown_refs_warning(model, op.expression)
    home = next((known for known in model.columns if _cf(known) == _cf(table)), None)
    if home is None:  # a table that held only measures until now
        model.columns[table] = {}
        home = table
    model.columns[home][op.name] = ""
    model.calculated[f"{table}[{op.name}]"] = op.expression
    structure = model_structure(model)
    structure.columns[(_cf(table), _cf(op.name))] = {
        "table": table,
        "name": op.name,
        "calculated": True,
    }
    return HandlerResult(f"Created the calculated column {table}[{op.name}].", warnings=warnings)


_COLUMN_FIELDS = (
    "format_string",
    "data_type",
    "sort_by",
    "data_category",
    "summarize_by",
    "folder",
    "description",
    "hidden",
    "key",
    "expression",
)


def _set_column_properties(
    document: PbirDocument, op: SetColumnProperties, model: ModelInfo
) -> HandlerResult:
    table = _table(model, op.table)
    column = _column(model, table, op.name)
    changed = [f for f in _COLUMN_FIELDS if getattr(op, f) is not None]
    if not changed:
        raise DocumentError("Say what to change in the column")
    structure = model_structure(model)
    calculated = _is_calculated_column(model, structure, table, column)
    warnings: list[str] = []
    if op.expression is not None:
        if calculated is False:
            raise DocumentError(
                f"{table}[{column}] is loaded from the data source, so it has no formula. Change "
                "it "
                "in Power Query, or create a calculated column."
            )
        _check_formula(op.expression, column)
        warnings += _unknown_refs_warning(model, op.expression)
        model.calculated[f"{table}[{column}]"] = op.expression
    if op.data_type is not None and calculated is not True:
        raise DocumentError(
            f"{table}[{column}] is loaded from the data source, so its data type is set in Power "
            "Query (Transform data), where Power BI converts the values as it loads them."
        )
    if op.sort_by:
        target = _column(model, table, op.sort_by)
        if _cf(target) == _cf(column):
            raise DocumentError("A column cannot be sorted by itself")
        other = _column_facts(model, structure, table, target).get("sort_by")
        if other and _cf(str(other)) == _cf(column):
            raise DocumentError(
                f"[{target}] is already sorted by [{column}], so the two would sort by each other"
            )
    word = _data_type_word(model, structure, table, column)
    if op.summarize_by in ("sum", "average", "min", "max") and word in ("text", "true_false"):
        raise DocumentError(
            f"{table}[{column}] holds {'text' if word == 'text' else 'true/false values'}, so it "
            f"can be counted but not summarized by {op.summarize_by}"
        )
    for text, what in (
        (op.format_string, "format string"),
        (op.folder, "display folder"),
        (op.description, "description"),
    ):
        if text is not None:
            _refuse(_text_problem(text, what))
    if op.format_string is not None and op.format_string.count('"') % 2:
        raise DocumentError("The format string has a quote that is never closed")
    if op.hidden is not None:
        facts = model.facts.get((_cf(table), _cf(column)))
        model.facts[(_cf(table), _cf(column))] = FieldFacts(
            "column",
            facts.data_type if facts else None,
            facts.summarize_by if facts else None,
            op.hidden,
        )
    return HandlerResult(
        f"Changed the column {table}[{column}] "
        f"({', '.join(c.replace('_', ' ') for c in changed)}).",
        warnings=warnings,
    )


def _rename_column(document: PbirDocument, op: RenameColumn, model: ModelInfo) -> HandlerResult:
    table = _table(model, op.table)
    column = _column(model, table, op.name)
    if op.new_name == column:
        raise DocumentError(f"The column is already called {op.new_name!r}")
    _refuse(_name_problem(op.new_name, "column"))
    if _cf(op.new_name) != _cf(column):
        _refuse(_field_taken(model, table, op.new_name))
    rename_column_references(document, table, column, op.new_name)
    _move_column(model, table, column, op.new_name)
    structure = model_structure(model)
    facts = structure.columns.pop((_cf(table), _cf(column)), None)
    if facts is not None:
        structure.columns[(_cf(table), _cf(op.new_name))] = {**facts, "name": op.new_name}
    return HandlerResult(_rename_column_summary(op, table, column))


def _rename_column_summary(op: RenameColumn, table: str, column: str) -> str:
    return (
        f"Renamed the column {table}[{column}] to [{op.new_name}] and updated the visuals, filters "
        "and formulas that use it."
    )


def _rename_column_report_part(
    document: PbirDocument, op: RenameColumn, model: ModelInfo
) -> HandlerResult:
    """The report half of a column rename, on the file Desktop saved after renaming it."""
    table = model.table(op.table) or op.table
    rename_column_references(document, table, op.name, op.new_name)
    return HandlerResult(_rename_column_summary(op, table, op.name))


def _column_dependents(
    model: ModelInfo, structure: ModelStructure, table: str, column: str
) -> list[str]:
    measures = frozenset(_cf(m) for m in model.measures)
    found: list[str] = []
    for source in dax_sources(model, structure):
        if source.kind == "column" and source.label.endswith(f"{table}[{column}]"):
            continue
        if rename_dax_column(
            source.text, table, column, column, own_table=source.table, measures=measures
        )[1]:
            found.append(source.label)
    return found


def _delete_column(document: PbirDocument, op: DeleteColumn, model: ModelInfo) -> HandlerResult:
    table = _table(model, op.table)
    column = _column(model, table, op.name)
    structure = model_structure(model)
    reasons: list[str] = []
    used = report_uses(
        document,
        lambda kind, body, entity: (
            kind == "Column"
            and entity is not None
            and _cf(entity) == _cf(table)
            and _cf(str(body.get("Property"))) == _cf(column)
        ),
    )
    if used:
        reasons.append("used by " + _listing(used))
    dax = _column_dependents(model, structure, table, column)
    dax += _extension_dax_uses(
        document, lambda text: rename_dax_column(text, table, column, column)[1] > 0
    )
    if dax:
        reasons.append("used in the formula of " + _listing(dax))
    joined = [
        r.describe()
        for r in structure.relationships
        if (_cf(r.from_table), _cf(r.from_column)) == (_cf(table), _cf(column))
        or (_cf(r.to_table), _cf(r.to_column)) == (_cf(table), _cf(column))
    ]
    if joined:
        reasons.append("used by the relationship between " + _listing(joined))
    sorted_by = [
        f"[{facts['name']}]"
        for (t, _), facts in structure.columns.items()
        if t == _cf(table) and _cf(str(facts.get("sort_by") or "")) == _cf(column)
    ]
    if sorted_by:
        reasons.append("the sort order of " + _listing(sorted_by))
    levels = [
        f"the hierarchy '{name}'"
        for (t, name), items in structure.hierarchies.items()
        if t == _cf(table) and any(_cf(c) == _cf(column) for _, c in items)
    ]
    if levels:
        reasons.append("a level of " + _listing(levels))
    if reasons:
        raise DocumentError(
            f"{table}[{column}] is {'; '.join(reasons)}. Remove or change those first."
        )
    warnings = _unknown_warning(structure, "relationships", "relationships") + _unknown_warning(
        structure, "hierarchies", "hierarchies and sort orders"
    )
    _drop_column(model, table, column)
    structure.columns.pop((_cf(table), _cf(column)), None)
    return HandlerResult(f"Deleted the column {table}[{column}].", warnings=warnings)


# --- handlers: tables -------------------------------------------------------------------------


def _create_calculated_table(
    document: PbirDocument, op: CreateCalculatedTable, model: ModelInfo
) -> HandlerResult:
    _refuse(_table_name_problem(op.name))
    if model.table(op.name) is not None:
        raise DocumentError(f"The model already has a table called {op.name!r}")
    _check_formula(op.expression, op.name)
    if op.description is not None:
        _refuse(_text_problem(op.description, "description"))
    warnings = _unknown_refs_warning(model, op.expression)
    _add_table(model, op.name, {}, op.expression)
    model_structure(model).tables[_cf(op.name)] = {
        "name": op.name,
        "kind": "calculated",
        "expression": op.expression,
    }
    return HandlerResult(f"Created the calculated table '{op.name}'.", warnings=warnings)


def _set_table_properties(
    document: PbirDocument, op: SetTableProperties, model: ModelInfo
) -> HandlerResult:
    table = _table(model, op.table)
    changed = [f for f in ("hidden", "description", "expression") if getattr(op, f) is not None]
    if not changed:
        raise DocumentError("Say what to change in the table")
    structure = model_structure(model)
    warnings: list[str] = []
    if op.expression is not None:
        kind = structure.table_kind(table)
        if kind not in ("calculated", "unknown") and table not in model.calculated:
            raise DocumentError(
                f"'{table}' is not a calculated table. Its rows come from its Power Query query."
            )
        _check_formula(op.expression, table)
        warnings += _unknown_refs_warning(model, op.expression)
        model.calculated[table] = op.expression
    if op.description is not None:
        _refuse(_text_problem(op.description, "description"))
    words = {
        "hidden": "hidden" if op.hidden else "shown",
        "description": "description",
        "expression": "formula",
    }
    return HandlerResult(
        f"Changed the table '{table}' ({', '.join(words[c] for c in changed)}).", warnings=warnings
    )


def _rename_table(document: PbirDocument, op: RenameTable, model: ModelInfo) -> HandlerResult:
    table = _table(model, op.name)
    if op.new_name == table:
        raise DocumentError(f"The table is already called {op.new_name!r}")
    _refuse(_table_name_problem(op.new_name))
    if _cf(op.new_name) != _cf(table) and model.table(op.new_name) is not None:
        raise DocumentError(f"The model already has a table called {op.new_name!r}")
    rename_table_references(document, table, op.new_name)
    _rename_table_in_model(model, table, op.new_name)
    structure = model_structure(model)
    entry = structure.tables.pop(_cf(table), None)
    if entry is not None:
        structure.tables[_cf(op.new_name)] = {**entry, "name": op.new_name}
    for key in [k for k in structure.columns if k[0] == _cf(table)]:
        structure.columns[(_cf(op.new_name), key[1])] = {
            **structure.columns.pop(key),
            "table": op.new_name,
        }
    structure.relationships = [
        replace(
            r,
            from_table=op.new_name if _cf(r.from_table) == _cf(table) else r.from_table,
            to_table=op.new_name if _cf(r.to_table) == _cf(table) else r.to_table,
        )
        for r in structure.relationships
    ]
    for filters in structure.roles.values():
        for filtered in [t for t in filters if _cf(t) == _cf(table)]:
            filters[op.new_name] = filters.pop(filtered)
    for place in [k for k in structure.hierarchies if k[0] == _cf(table)]:
        structure.hierarchies[(_cf(op.new_name), place[1])] = structure.hierarchies.pop(place)
    if _cf(table) in structure.groups:
        structure.groups[_cf(op.new_name)] = {
            **structure.groups.pop(_cf(table)),
            "name": op.new_name,
        }
    if _cf(table) in structure.partitions:
        structure.partitions[_cf(op.new_name)] = structure.partitions.pop(_cf(table))
    return HandlerResult(_rename_table_summary(table, op.new_name))


def _rename_table_summary(old: str, new: str) -> str:
    return (
        f"Renamed the table '{old}' to '{new}' and updated the visuals, filters and formulas that "
        "use it."
    )


def _rename_table_report_part(
    document: PbirDocument, op: RenameTable, model: ModelInfo
) -> HandlerResult:
    rename_table_references(document, op.name, op.new_name)
    return HandlerResult(_rename_table_summary(op.name, op.new_name))


def _table_noun(structure: ModelStructure, model: ModelInfo, table: str) -> str:
    kind = structure.table_kind(table)
    if kind == "fieldParameter" or table in model.field_parameter_tables():
        return "field parameter"
    if kind == "calculationGroup" or _cf(table) in structure.groups:
        return "calculation group"
    if kind == "calculated" or table in model.calculated:
        return "calculated table"
    return "table"


def _delete_table(document: PbirDocument, op: DeleteTable, model: ModelInfo) -> HandlerResult:
    table = _table(model, op.name)
    structure = model_structure(model)
    own_measures = {_cf(m) for m, home in model.measures.items() if _cf(home) == _cf(table)}
    used = report_uses(
        document,
        lambda kind, body, entity: entity is not None and _cf(entity) == _cf(table),
    )
    reasons: list[str] = []
    if used:
        reasons.append("used by " + _listing(used))
    dependents: list[str] = []
    for source in dax_sources(model, structure):
        if source.table is not None and _cf(source.table) == _cf(table):
            continue  # goes with the table
        if dax_uses_table(source.text, table) or any(
            ref.table is None and ref.name is not None and _cf(ref.name) in own_measures
            for ref in dax_references(source.text)
        ):
            dependents.append(source.label)
    dependents += _extension_dax_uses(document, lambda text: dax_uses_table(text, table))
    if dependents:
        reasons.append("used in the formula of " + _listing(dependents))
    if reasons:
        raise DocumentError(f"'{table}' is {'; '.join(reasons)}. Remove or change those first.")
    only, also = roles_filtering(structure, table)
    if only:
        raise DocumentError(
            f"'{table}' holds the only filter of the role {_listing(only)}: without it, people in "
            "that role would see every row. Change the role's filters or delete the role first."
        )
    noun = _table_noun(structure, model, table)
    joined = [
        r for r in structure.relationships if _cf(table) in (_cf(r.from_table), _cf(r.to_table))
    ]
    _drop_table(model, table)
    structure.tables.pop(_cf(table), None)
    structure.relationships = [r for r in structure.relationships if r not in joined]
    structure.groups.pop(_cf(table), None)
    for filters in structure.roles.values():
        for key in [t for t in filters if _cf(t) == _cf(table)]:
            del filters[key]
    extra = (
        f" and its {len(joined)} relationship{'s' if len(joined) != 1 else ''}" if joined else ""
    )
    warnings = _unknown_warning(structure, "relationships", "relationships")
    warnings += _unknown_warning(structure, "roles", "roles")
    if also:
        warnings.append(
            f"The role {_listing(also)} loses its filter on '{table}'; its other filters stay."
        )
    return HandlerResult(f"Deleted the {noun} '{table}'{extra}.", warnings=warnings)


def roles_filtering(structure: ModelStructure, table: str) -> tuple[list[str], list[str]]:
    """(roles whose only row filter is on ``table``, roles that also filter other tables)."""
    only: list[str] = []
    also: list[str] = []
    for key, filters in structure.roles.items():
        on = [t for t, text in filters.items() if text]
        if not any(_cf(t) == _cf(table) for t in on):
            continue
        name = structure.role_names.get(key, key)
        (only if all(_cf(t) == _cf(table) for t in on) else also).append(f"'{name}'")
    return only, also


def _mark_date_table(document: PbirDocument, op: MarkDateTable, model: ModelInfo) -> HandlerResult:
    table = _table(model, op.table)
    structure = model_structure(model)
    if op.column is None:
        entry = structure.tables.setdefault(_cf(table), {"name": table})
        if entry.get("date_table") is False:
            raise DocumentError(f"'{table}' is not marked as a date table")
        entry["date_table"] = False
        return HandlerResult(f"'{table}' is no longer marked as a date table.")
    column = _column(model, table, op.column)
    word = _data_type_word(model, structure, table, column)
    if word and word != "date":
        raise DocumentError(
            f"{table}[{column}] does not hold dates, so it cannot be the date table's date column"
        )
    warnings = (
        []
        if word
        else [
            f"Studio could not read the data type of {table}[{column}]; Power BI Desktop checks "
            "that it "
            "holds unique dates."
        ]
    )
    if "variations" not in structure.known or _cf(table) in structure.variation_tables:
        warnings.append(
            f"Studio leaves any automatic date hierarchy on the columns of '{table}' (Power BI "
            "Desktop removes them when you mark the table yourself), so visuals that use them "
            "keep working."
        )
    structure.tables.setdefault(_cf(table), {"name": table})["date_table"] = True
    return HandlerResult(f"Marked '{table}' as a date table using [{column}].", warnings=warnings)


# --- handlers: relationships ------------------------------------------------------------------


def _ends(
    model: ModelInfo, op: CreateRelationship | UpdateRelationship | DeleteRelationship
) -> tuple[tuple[str, str], tuple[str, str]]:
    source_table = _table(model, op.from_table)
    target_table = _table(model, op.to_table)
    return (
        (source_table, _column(model, source_table, op.from_column)),
        (target_table, _column(model, target_table, op.to_column)),
    )


def _no_relationship(source: tuple[str, str], target: tuple[str, str]) -> str:
    return f"There is no relationship between {source[0]}[{source[1]}] and {target[0]}[{target[1]}]"


_ONE_TO_ONE = (
    "A one-to-one relationship always filters in both directions in Power BI; ask for both "
    "directions"
)
_SECURITY_BOTH = (
    "A relationship applies row-level security in both directions only when it filters in both "
    "directions; ask for both directions too"
)


def _storage_mode(structure: ModelStructure, table: str) -> str | None:
    """'directquery', 'import', 'dual' or another mode of ``table``; None when unknown."""
    partitions = structure.partitions.get(_cf(table))
    if not partitions:
        return None
    default = _str(structure.settings.get("defaultMode")).casefold().replace("_", "")
    modes = {_mode(p) or "default" for p in partitions}
    if "default" in modes and not default:
        return None
    modes = {default if m == "default" else m for m in modes}
    return modes.pop() if len(modes) == 1 else "mixed"


def _referential_integrity(
    structure: ModelStructure,
    tables: tuple[str, str],
    wanted: bool | None,
    *,
    many: tuple[bool, bool] | None = None,
) -> list[str]:
    """Never mistake queryability or storage metadata for a verified source-key constraint.

    Desktop requires a regular relationship within one DirectQuery source group and valid
    nonblank, matching keys. Studio does not inspect source groups or validate remote keys.
    Disabling the assumption is supported; enabling it must be verified in Power BI first.
    """
    if not wanted:
        return []
    if many == (True, True):
        raise DocumentError(
            "Assume referential integrity is not supported for many-to-many relationships"
        )
    modes = {table: _storage_mode(structure, table) for table in tables}
    other = [t for t, mode in modes.items() if mode is not None and mode != "directquery"]
    if other:
        raise DocumentError(
            f"'{other[0]}' is not DirectQuery, and Power BI Desktop "
            "assumes referential integrity only for relationships between DirectQuery tables"
        )
    raise DocumentError(
        "Modelcraft cannot verify the same DirectQuery source group or nonblank, matching "
        "relationship keys. Verify and enable Assume referential integrity in Power BI first; "
        "an unverified assumption can omit rows from results."
    )


_CARDINALITY_WORDS = {
    "many_to_one": "many-to-one",
    "one_to_one": "one-to-one",
    "many_to_many": "many-to-many",
}


def relationship_settings(
    structure: ModelStructure,
    source: tuple[str, str],
    target: tuple[str, str],
    op: CreateRelationship | UpdateRelationship,
) -> RelationshipFact:
    """Validate effective settings and return them in the stored relationship's orientation.

    Update endpoints identify an unordered pair, but cardinality is relative to the request's
    from/to columns. Both preflight and the native planner use this calculation, including
    changes earlier in the same batch. No saved facts are presented as key-data validation.
    """
    if _cf(source[0]) == _cf(target[0]):
        raise DocumentError("A relationship joins two different tables")
    existing = structure.relationship(source, target)
    creating = isinstance(op, CreateRelationship)
    if creating and existing is not None:
        raise DocumentError(f"{existing.describe()} are already related")
    if not creating and existing is None and "relationships" in structure.known:
        raise DocumentError(_no_relationship(source, target))
    fact = existing or RelationshipFact(*source, *target)
    many = (fact.from_many, fact.to_many)
    known = fact.cardinality_known
    if op.cardinality is not None:
        many = (op.cardinality != "one_to_one", op.cardinality == "many_to_many")
        if existing is not None and (_cf(fact.from_table), _cf(fact.from_column)) != (
            _cf(source[0]),
            _cf(source[1]),
        ):
            many = many[::-1]
        known = True
    both = fact.both if op.cross_filter is None else op.cross_filter == "both"
    security = fact.security_both if op.security_filter is None else op.security_filter == "both"
    rely = fact.rely if op.assume_referential_integrity is None else op.assume_referential_integrity
    active = fact.active if op.active is None else op.active
    if known and many == (False, False) and not both:
        raise DocumentError(_ONE_TO_ONE)
    if security and not both:
        raise DocumentError(_SECURITY_BOTH + "; explicitly change the security filter too")
    if rely and known and many == (True, True):
        raise DocumentError(
            "Assume referential integrity is not supported for many-to-many relationships"
        )
    if op.assume_referential_integrity is True and not (existing and existing.rely is True):
        _referential_integrity(
            structure, (source[0], target[0]), True, many=many if known else None
        )
    if active and (creating or (existing is not None and not existing.active)):
        path = structure.active_path(source[0], target[0], ignore=existing)
        if path:
            raise DocumentError(
                f"'{source[0]}' and '{target[0]}' are already connected through active "
                f"relationships ({' - '.join(path)}). Modelcraft requires an unambiguous active "
                "path for this edit. Create it as inactive or deactivate the other path first."
            )
    return replace(
        fact,
        active=active,
        both=both,
        from_many=many[0],
        to_many=many[1],
        cardinality_known=known,
        rely=rely,
        security_both=security,
    )


def _create_relationship(
    document: PbirDocument, op: CreateRelationship, model: ModelInfo
) -> HandlerResult:
    source, target = _ends(model, op)
    structure = model_structure(model)
    fact = relationship_settings(structure, source, target, op)
    warnings = _unknown_warning(structure, "relationships", "relationships")
    left = _data_type_word(model, structure, *source)
    right = _data_type_word(model, structure, *target)
    if left and right and left != right:
        raise DocumentError(
            f"{source[0]}[{source[1]}] holds {left} values and {target[0]}[{target[1]}] holds "
            f"{right} values; the columns of a relationship must have the same data type"
        )
    structure.relationships.append(fact)
    state = "active" if op.active else "inactive"
    direction = "both directions" if op.cross_filter == "both" else "single direction"
    return HandlerResult(
        f"Created a {_CARDINALITY_WORDS[op.cardinality]} relationship from "
        f"{source[0]}[{source[1]}] to {target[0]}[{target[1]}] ({direction}, {state}).",
        warnings=warnings,
    )


def _update_relationship(
    document: PbirDocument, op: UpdateRelationship, model: ModelInfo
) -> HandlerResult:
    source, target = _ends(model, op)
    changed = [
        f
        for f in (
            "cardinality",
            "cross_filter",
            "active",
            "assume_referential_integrity",
            "security_filter",
        )
        if getattr(op, f) is not None
    ]
    if not changed:
        raise DocumentError("Say what to change in the relationship")
    structure = model_structure(model)
    existing = structure.relationship(source, target)
    warnings = _unknown_warning(structure, "relationships", "relationships")
    fact = relationship_settings(structure, source, target, op)
    cardinality = op.cardinality
    if existing is not None:
        index = structure.relationships.index(existing)
        structure.relationships[index] = fact
    words = {
        "cardinality": _CARDINALITY_WORDS.get(cardinality or "", "cardinality"),
        "cross_filter": "both directions" if op.cross_filter == "both" else "single direction",
        "active": "active" if op.active else "inactive",
        "assume_referential_integrity": "assume referential integrity"
        if op.assume_referential_integrity
        else "no referential integrity assumed",
        "security_filter": "security filter in both directions"
        if op.security_filter == "both"
        else "security filter in one direction",
    }
    return HandlerResult(
        f"Changed the relationship between {source[0]}[{source[1]}] and {target[0]}[{target[1]}] "
        f"({', '.join(words[c] for c in changed)}).",
        warnings=warnings,
    )


def _delete_relationship(
    document: PbirDocument, op: DeleteRelationship, model: ModelInfo
) -> HandlerResult:
    source, target = _ends(model, op)
    structure = model_structure(model)
    existing = structure.relationship(source, target)
    if existing is None and "relationships" in structure.known:
        raise DocumentError(_no_relationship(source, target))
    fact = existing or RelationshipFact(source[0], source[1], target[0], target[1])
    users = [s.label for s in dax_sources(model, structure) if dax_uses_relationship(s.text, fact)]
    if users:
        raise DocumentError(
            f"The relationship between {fact.describe()} is used by USERELATIONSHIP or "
            f"CROSSFILTER in {_listing(users)}. Change those formulas first."
        )
    if existing is not None:
        structure.relationships.remove(existing)
    warnings = _unknown_warning(structure, "relationships", "relationships")
    return HandlerResult(
        f"Deleted the relationship between {source[0]}[{source[1]}] and {target[0]}[{target[1]}].",
        warnings=warnings,
    )


# --- handlers: hierarchies --------------------------------------------------------------------


def _levels(model: ModelInfo, table: str, levels: list[HierarchyLevel]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for level in levels:
        column = _column(model, table, level.column)
        name = level.name or column
        _refuse(_name_problem(name, "level"))
        if any(_cf(n) == _cf(name) for n, _ in found):
            raise DocumentError(f"The hierarchy has two levels called {name!r}")
        if any(_cf(c) == _cf(column) for _, c in found):
            raise DocumentError(
                f"[{column}] is used by two levels; each level needs its own column"
            )
        found.append((name, column))
    return found


def _hierarchy_uses(document: PbirDocument, table: str, name: str) -> list[str]:
    def matches(kind: str, body: dict[str, Any], entity: str | None) -> bool:
        if entity is None or _cf(entity) != _cf(table):
            return False
        if kind == "Hierarchy":
            return _cf(str(body.get("Hierarchy"))) == _cf(name)
        if kind == "HierarchyLevel":
            inner = body.get("Expression", {}).get("Hierarchy", {})
            return isinstance(inner, dict) and _cf(str(inner.get("Hierarchy"))) == _cf(name)
        return False

    return report_uses(document, matches)


def _create_hierarchy(
    document: PbirDocument, op: CreateHierarchy, model: ModelInfo
) -> HandlerResult:
    table = _table(model, op.table)
    _refuse(_name_problem(op.name, "hierarchy"))
    structure = model_structure(model)
    if (_cf(table), _cf(op.name)) in structure.hierarchies:
        raise DocumentError(f"The table {table} already has a hierarchy called {op.name!r}")
    _refuse(_field_taken(model, table, op.name))
    levels = _levels(model, table, op.levels)
    structure.hierarchies[(_cf(table), _cf(op.name))] = levels
    return HandlerResult(
        f"Created the hierarchy '{op.name}' in {table} ({' > '.join(n for n, _ in levels)}).",
        warnings=_unknown_warning(structure, "hierarchies", "hierarchies"),
    )


def _update_hierarchy(
    document: PbirDocument, op: UpdateHierarchy, model: ModelInfo
) -> HandlerResult:
    table = _table(model, op.table)
    structure = model_structure(model)
    key = (_cf(table), _cf(op.name))
    if key not in structure.hierarchies and "hierarchies" in structure.known:
        raise DocumentError(f"The table {table} has no hierarchy called {op.name!r}")
    if op.levels is None and op.new_name is None:
        raise DocumentError("Say what to change in the hierarchy")
    used = _hierarchy_uses(document, table, op.name)
    before = structure.hierarchies.get(key, [])
    parts: list[str] = []
    levels = before
    if op.levels is not None:
        levels = _levels(model, table, op.levels)
        dropped = [n for n, _ in before if all(_cf(n) != _cf(m) for m, _ in levels)]
        if dropped and used:
            raise DocumentError(
                f"The levels {_listing([repr(d) for d in dropped])} of '{op.name}' are removed or "
                f"renamed, but the hierarchy is used by {_listing(used)}. Change those visuals "
                "first."
            )
        parts.append("levels " + " > ".join(n for n, _ in levels))
    name = op.name
    if op.new_name is not None:
        _refuse(_name_problem(op.new_name, "hierarchy"))
        if used:
            raise DocumentError(
                f"'{op.name}' is used by {_listing(used)}, and Studio cannot point visuals at a "
                "renamed hierarchy yet. Rename it in Power BI Desktop, which updates them."
            )
        if (_cf(table), _cf(op.new_name)) in structure.hierarchies and _cf(op.new_name) != _cf(
            op.name
        ):
            raise DocumentError(f"The table {table} already has a hierarchy called {op.new_name!r}")
        name = op.new_name
        parts.append(f"renamed to '{op.new_name}'")
    structure.hierarchies.pop(key, None)
    structure.hierarchies[(_cf(table), _cf(name))] = levels
    return HandlerResult(
        f"Changed the hierarchy '{op.name}' in {table} ({'; '.join(parts)}).",
        warnings=_unknown_warning(structure, "hierarchies", "hierarchies"),
    )


def _delete_hierarchy(
    document: PbirDocument, op: DeleteHierarchy, model: ModelInfo
) -> HandlerResult:
    table = _table(model, op.table)
    structure = model_structure(model)
    key = (_cf(table), _cf(op.name))
    if key not in structure.hierarchies and "hierarchies" in structure.known:
        raise DocumentError(f"The table {table} has no hierarchy called {op.name!r}")
    used = _hierarchy_uses(document, table, op.name)
    if used:
        raise DocumentError(
            f"The hierarchy '{op.name}' is used by {_listing(used)}. Remove it from those first."
        )
    structure.hierarchies.pop(key, None)
    return HandlerResult(
        f"Deleted the hierarchy '{op.name}' from {table}.",
        warnings=_unknown_warning(structure, "hierarchies", "hierarchies"),
    )


# --- handlers: field parameters ---------------------------------------------------------------


def field_parameter_dax(model: ModelInfo, fields: list[ParameterField]) -> str:
    """The calculated table DAX of a field parameter, in Desktop's layout: one ("label",
    NAMEOF('Table'[Field]), order) row per field."""
    rows: list[str] = []
    for order, item in enumerate(fields):
        _, table, name = model.resolve(item)
        label = (item.label or name).replace('"', '""')
        rows.append(f'    ("{label}", NAMEOF({_column_ref(table, name)}), {order})')
    return "{\n" + ",\n".join(rows) + "\n}"


def _check_fields(model: ModelInfo, fields: list[ParameterField]) -> None:
    seen: set[tuple[str, str]] = set()
    labels: set[str] = set()
    for item in fields:
        _, table, name = model.resolve(item)
        if (_cf(table), _cf(name)) in seen:
            raise DocumentError(f"{table}[{name}] is in the list twice")
        seen.add((_cf(table), _cf(name)))
        label = item.label or name
        _refuse(_text_problem(label, "label"))
        if _cf(label) in labels:
            raise DocumentError(f"Two fields would show as {label!r}; give them different labels")
        labels.add(_cf(label))


def _create_field_parameter(
    document: PbirDocument, op: CreateFieldParameter, model: ModelInfo
) -> HandlerResult:
    _refuse(_table_name_problem(op.name))
    if model.table(op.name) is not None:
        raise DocumentError(f"The model already has a table called {op.name!r}")
    _check_fields(model, op.fields)
    dax = field_parameter_dax(model, op.fields)
    _add_table(
        model,
        op.name,
        {op.name: "object", f"{op.name} Fields": "object", f"{op.name} Order": "int64"},
        dax,
    )
    model_structure(model).tables[_cf(op.name)] = {
        "name": op.name,
        "kind": "fieldParameter",
        "expression": dax,
    }
    count = len(op.fields)
    return HandlerResult(
        f"Created the field parameter '{op.name}' with {count} field{'s' if count != 1 else ''}."
    )


def _update_field_parameter(
    document: PbirDocument, op: UpdateFieldParameter, model: ModelInfo
) -> HandlerResult:
    table = _table(model, op.name)
    structure = model_structure(model)
    if (
        structure.table_kind(table) != "fieldParameter"
        and table not in model.field_parameter_tables()
    ):
        raise DocumentError(f"'{table}' is not a field parameter")
    _check_fields(model, op.fields)
    model.calculated[table] = field_parameter_dax(model, op.fields)
    count = len(op.fields)
    return HandlerResult(
        f"Changed the fields of the field parameter '{table}' "
        f"({count} field{'s' if count != 1 else ''})."
    )


# --- handlers: calculation groups -------------------------------------------------------------


def _check_item(name: str, expression: str | None, format_expression: str | None) -> None:
    _refuse(_name_problem(name, "calculation item"))
    if expression is not None:
        _refuse(_dax_problem(expression))
    if format_expression:
        _refuse(_dax_problem(format_expression, "format string formula"))


def _create_calculation_group(
    document: PbirDocument, op: CreateCalculationGroup, model: ModelInfo
) -> HandlerResult:
    _refuse(_table_name_problem(op.name))
    if model.table(op.name) is not None:
        raise DocumentError(f"The model already has a table called {op.name!r}")
    _refuse(_name_problem(op.column, "column"))
    if _cf(op.column) == "ordinal":
        raise DocumentError(
            "'Ordinal' is the calculation group's sort column; name the column something else"
        )
    names: set[str] = set()
    for item in op.items:
        _check_item(item.name, item.expression, item.format_string_expression)
        if _cf(item.name) in names:
            raise DocumentError(f"Two calculation items are called {item.name!r}")
        names.add(_cf(item.name))
    structure = model_structure(model)
    taken = [g["name"] for g in structure.groups.values() if g.get("precedence") == op.precedence]
    if taken:
        raise DocumentError(
            f"The calculation group '{taken[0]}' already has precedence {op.precedence}; each "
            "calculation group needs its own"
        )
    _add_table(model, op.name, {op.column: "object", "Ordinal": "int64"}, None)
    structure.tables[_cf(op.name)] = {"name": op.name, "kind": "calculationGroup"}
    structure.groups[_cf(op.name)] = {
        "name": op.name,
        "precedence": op.precedence,
        "items": {
            _cf(i.name): {"name": i.name, "expression": i.expression, "ordinal": n}
            for n, i in enumerate(op.items)
        },
    }
    warnings: list[str] = []
    if not structure.settings.get("discourageImplicitMeasures"):
        warnings.append(
            "Power BI needs 'discourage implicit measures' on for a calculation group, so Studio "
            "turns it on: visuals that sum a column directly keep working, new ones need a measure."
        )
    warnings += _unknown_warning(structure, "calculation_groups", "calculation groups")
    count = len(op.items)
    return HandlerResult(
        f"Created the calculation group '{op.name}' with {count} item{'s' if count != 1 else ''}.",
        warnings=warnings,
    )


def _group_of(model: ModelInfo, name: str) -> tuple[str, dict[str, Any] | None]:
    table = _table(model, name)
    structure = model_structure(model)
    group = structure.groups.get(_cf(table))
    if group is None and (
        "calculation_groups" in structure.known
        or structure.table_kind(table) not in ("unknown", "calculationGroup")
    ):
        raise DocumentError(f"'{table}' is not a calculation group")
    return table, group


def _set_calculation_item(
    document: PbirDocument, op: SetCalculationItem, model: ModelInfo
) -> HandlerResult:
    table, group = _group_of(model, op.group)
    items: dict[str, Any] = group["items"] if group is not None else {}
    existing = items.get(_cf(op.name))
    if existing is None and group is not None and op.expression is None:
        raise DocumentError(f"'{table}' has no item called {op.name!r}; give a formula to add it")
    if (
        op.expression is None
        and op.format_string_expression is None
        and op.ordinal is None
        and op.new_name is None
    ):
        raise DocumentError("Say what to change in the calculation item")
    _check_item(op.new_name or op.name, op.expression, op.format_string_expression)
    if op.new_name is not None and _cf(op.new_name) != _cf(op.name) and _cf(op.new_name) in items:
        raise DocumentError(f"'{table}' already has an item called {op.new_name!r}")
    if existing is None:
        items[_cf(op.name)] = {
            "name": op.name,
            "expression": op.expression or "",
            "ordinal": op.ordinal,
        }
        return HandlerResult(f"Added the calculation item '{op.name}' to '{table}'.")
    if op.expression is not None:
        existing["expression"] = op.expression
    if op.new_name is not None:
        items[_cf(op.new_name)] = {**items.pop(_cf(op.name)), "name": op.new_name}
    changed = [
        w
        for w, v in (
            ("formula", op.expression),
            ("format string", op.format_string_expression),
            ("order", op.ordinal),
            (f"renamed to '{op.new_name}'", op.new_name),
        )
        if v is not None
    ]
    return HandlerResult(
        f"Changed the calculation item '{op.name}' of '{table}' ({', '.join(changed)})."
    )


def _delete_calculation_item(
    document: PbirDocument, op: DeleteCalculationItem, model: ModelInfo
) -> HandlerResult:
    table, group = _group_of(model, op.group)
    if group is not None:
        if _cf(op.name) not in group["items"]:
            raise DocumentError(f"'{table}' has no item called {op.name!r}")
        if len(group["items"]) == 1:
            raise DocumentError(
                f"'{op.name}' is the only item of '{table}'; delete the calculation group instead"
            )
        del group["items"][_cf(op.name)]
    return HandlerResult(f"Deleted the calculation item '{op.name}' from '{table}'.")


def _update_calculation_group(
    document: PbirDocument, op: UpdateCalculationGroup, model: ModelInfo
) -> HandlerResult:
    table, group = _group_of(model, op.name)
    if op.precedence is None and op.description is None:
        raise DocumentError("Say what to change in the calculation group")
    structure = model_structure(model)
    if op.precedence is not None:
        taken = [
            g["name"]
            for k, g in structure.groups.items()
            if k != _cf(table) and g.get("precedence") == op.precedence
        ]
        if taken:
            raise DocumentError(
                f"The calculation group '{taken[0]}' already has precedence {op.precedence}"
            )
        if group is not None:
            group["precedence"] = op.precedence
    if op.description is not None:
        _refuse(_text_problem(op.description, "description"))
    parts = [f"precedence {op.precedence}"] if op.precedence is not None else []
    parts += ["description"] if op.description is not None else []
    return HandlerResult(f"Changed the calculation group '{table}' ({', '.join(parts)}).")


# --- handlers: roles --------------------------------------------------------------------------

_MEMBERS_REFUSAL = (
    "People are added to a role in the Power BI service after the report is published (the "
    "dataset's Security page), not in the file. Studio sets up the role and its filters here."
)


def _role_filters(model: ModelInfo, filters: list[RoleFilter]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in filters:
        table = _table(model, item.table)
        if _cf(table) in {_cf(t) for t in found}:
            raise DocumentError(f"The role has two filters on {table}")
        if item.expression:
            _refuse(_dax_problem(item.expression, "filter"))
        found[table] = item.expression
    return found


def _create_role(document: PbirDocument, op: CreateRole, model: ModelInfo) -> HandlerResult:
    if op.members:
        raise DocumentError(_MEMBERS_REFUSAL)
    _refuse(_name_problem(op.name, "role"))
    structure = model_structure(model)
    if _cf(op.name) in structure.roles:
        raise DocumentError(f"The model already has a role called {op.name!r}")
    filters = _role_filters(model, op.filters)
    if op.description is not None:
        _refuse(_text_problem(op.description, "description"))
    structure.roles[_cf(op.name)] = {t: f for t, f in filters.items() if f}
    structure.role_names[_cf(op.name)] = op.name
    on = [t for t, f in filters.items() if f]
    detail = f" with a filter on {_listing(on)}" if on else " with no filters (it sees every row)"
    warnings = _unknown_warning(structure, "roles", "roles")
    if on:
        warnings.append(
            "After the change Studio counts the rows the role sees in each filtered table and "
            "says so when a filter keeps every row or none; Modeling > View as in Power BI "
            "Desktop shows the whole report as that role."
        )
    return HandlerResult(f"Created the role '{op.name}'{detail}.", warnings=warnings)


def _update_role(document: PbirDocument, op: UpdateRole, model: ModelInfo) -> HandlerResult:
    if op.members:
        raise DocumentError(_MEMBERS_REFUSAL)
    structure = model_structure(model)
    if _cf(op.name) not in structure.roles and "roles" in structure.known:
        raise DocumentError(f"The model has no role called {op.name!r}")
    if op.new_name is None and op.filters is None and op.description is None:
        raise DocumentError("Say what to change in the role")
    parts: list[str] = []
    current = structure.roles.get(_cf(op.name), {})
    if op.filters is not None:
        filters = _role_filters(model, op.filters)
        for table, text in filters.items():
            if text:
                current[table] = text
            else:
                current.pop(table, None)
        parts.append("filters on " + _listing(list(filters)))
    if op.description is not None:
        _refuse(_text_problem(op.description, "description"))
        parts.append("description")
    name = op.name
    if op.new_name is not None:
        _refuse(_name_problem(op.new_name, "role"))
        if _cf(op.new_name) != _cf(op.name) and _cf(op.new_name) in structure.roles:
            raise DocumentError(f"The model already has a role called {op.new_name!r}")
        name = op.new_name
        parts.append(f"renamed to '{op.new_name}'")
    structure.roles.pop(_cf(op.name), None)
    structure.role_names.pop(_cf(op.name), None)
    structure.roles[_cf(name)] = current
    structure.role_names[_cf(name)] = name
    return HandlerResult(
        f"Changed the role '{op.name}' ({'; '.join(parts)}).",
        warnings=_unknown_warning(structure, "roles", "roles"),
    )


def _delete_role(document: PbirDocument, op: DeleteRole, model: ModelInfo) -> HandlerResult:
    structure = model_structure(model)
    if _cf(op.name) not in structure.roles and "roles" in structure.known:
        raise DocumentError(f"The model has no role called {op.name!r}")
    structure.roles.pop(_cf(op.name), None)
    structure.role_names.pop(_cf(op.name), None)
    return HandlerResult(
        f"Deleted the role '{op.name}'.", warnings=_unknown_warning(structure, "roles", "roles")
    )


# --- handlers: Power Query ----------------------------------------------------------------------

M_TIMEOUT = 15


def _node() -> str | None:
    return shutil.which("node")


def check_m_with_validator(text: str) -> list[str] | None:
    """Problems Microsoft's Power Query parser finds in ``text`` (``llm/m_validation.cjs``, run
    under the bundled node like ``llm.artifact_checks``); None when it cannot be run."""
    node = _node()
    script = Path(__file__).resolve().parents[1] / "llm" / "m_validation.cjs"
    if node is None or not script.is_file():
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed local parser; the M goes through stdin
            [node, str(script)],
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=M_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if result.returncode:
            return None
        problems = json.loads(result.stdout).get("problems")
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None
    return [str(p) for p in problems] if isinstance(problems, list) else None


# The checker the handlers use; tests replace it (it starts a process).
M_CHECKER: Callable[[str], list[str] | None] = check_m_with_validator


M_UNCHECKED = (
    "Studio could not check the Power Query formula; Power BI checks it when it loads the data."
)


@dataclass(frozen=True)
class MCheck:
    """What Studio's own checks say of a Power Query formula, worded as an edit would be.

    ``refusal`` is the sentence an edit with this formula is refused with (None: it would go
    ahead); ``problems`` every problem Microsoft's Power Query parser found; ``warnings`` what the
    receipt would add; ``checked`` whether the parser ran at all."""

    refusal: str | None
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    checked: bool = False


def check_m(text: str) -> MCheck:
    """The checks ``set_table_query`` and ``set_named_expression`` make of their formula, without
    raising (the model workbench validates a query before it is sent). The text only goes to the
    local parser (``M_CHECKER``)."""
    problem = _text_problem(text, "query")
    if problem is not None:
        return MCheck(problem)
    problems = M_CHECKER(text)
    if problems is None:
        return MCheck(None, warnings=(M_UNCHECKED,))
    if problems:
        refusal = "The Power Query formula is not valid: " + "; ".join(problems[:3])
        return MCheck(refusal, tuple(problems), checked=True)
    return MCheck(None, checked=True)


def _check_m(text: str) -> list[str]:
    """Refuses M the parser rejects; returns a warning when it could not be checked."""
    found = check_m(text)
    _refuse(found.refusal)
    return list(found.warnings)


def table_query_target(model: ModelInfo, name: str) -> tuple[str, list[str]]:
    """The table whose Power Query query ``set_table_query`` may change (its own spelling) and the
    warnings that go with it; DocumentError when it has none Studio can change."""
    table = _table(model, name)
    structure = model_structure(model)
    kind = structure.table_kind(table)
    partitions = structure.partitions.get(_cf(table))
    if kind in ("calculated", "fieldParameter", "calculationGroup") or table in model.calculated:
        raise DocumentError(f"'{table}' is calculated with DAX, so it has no Power Query query")
    if partitions is not None and (len(partitions) != 1 or partitions[0]["kind"] != "m"):
        raise DocumentError(
            f"'{table}' does not load from a single Power Query query, so Studio cannot change it"
        )
    return table, _unknown_warning(structure, "partitions", "queries")


def _set_table_query(document: PbirDocument, op: SetTableQuery, model: ModelInfo) -> HandlerResult:
    table, unknown = table_query_target(model, op.table)
    warnings = _check_m(op.expression)
    warnings += unknown
    if op.if_refresh_fails == "keep":
        warnings.append(
            "If Power BI cannot reach the data source, the new query is kept without reloading "
            "the data."
        )
    return HandlerResult(
        f"Changed the Power Query query of '{table}' and asked Power BI to reload its data.",
        warnings=warnings,
    )


def _m_identifier_used(text: str, name: str) -> bool:
    quoted = re.escape('#"' + name.replace('"', '""') + '"')
    plain = r"(?<![\w#\"])" + re.escape(name) + r"(?![\w\"])"
    return re.search(quoted, text) is not None or (
        re.fullmatch(r"[A-Za-z_][\w.]*", name) is not None and re.search(plain, text) is not None
    )


def _set_named_expression(
    document: PbirDocument, op: SetNamedExpression, model: ModelInfo
) -> HandlerResult:
    _refuse(_name_problem(op.name, "query"))
    if model.table(op.name) is not None:
        raise DocumentError(f"{op.name!r} is the name of a table; give the query another name")
    warnings = _check_m(op.expression)
    if op.description is not None:
        _refuse(_text_problem(op.description, "description"))
    structure = model_structure(model)
    exists = _cf(op.name) in structure.expressions
    structure.expressions[_cf(op.name)] = {"name": op.name, "expression": op.expression}
    verb = "Changed" if exists else "Created" if "expressions" in structure.known else "Saved"
    return HandlerResult(f"{verb} the query '{op.name}'.", warnings=warnings)


def _delete_named_expression(
    document: PbirDocument, op: DeleteNamedExpression, model: ModelInfo
) -> HandlerResult:
    structure = model_structure(model)
    if _cf(op.name) not in structure.expressions and "expressions" in structure.known:
        raise DocumentError(f"The model has no query or parameter called {op.name!r}")
    users = [
        f"the query of '{table}'"
        for table, items in structure.partitions.items()
        for item in items
        if item["kind"] == "m" and _m_identifier_used(item["expression"], op.name)
    ] + [
        f"the query '{other['name']}'"
        for key, other in structure.expressions.items()
        if key != _cf(op.name) and _m_identifier_used(other["expression"], op.name)
    ]
    if users:
        raise DocumentError(f"{op.name!r} is used by {_listing(users)}. Change those first.")
    structure.expressions.pop(_cf(op.name), None)
    warnings = _unknown_warning(structure, "partitions", "queries")
    return HandlerResult(f"Deleted the query '{op.name}'.", warnings=warnings)


# A parameter's Type in its meta record (Manage Parameters' types) and the kind Studio calls it.
PARAMETER_TYPES: dict[str, str] = {
    "Text": "text",
    "Number": "number",
    "Logical": "true_false",
    "Date": "date",
    "DateTime": "date_time",
    "Time": "time",
}
_TYPE_WORDS = {
    "text": "text",
    "number": "a number",
    "true_false": "true or false",
    "date": "a date",
    "date_time": "a date and time",
    "time": "a time",
}
_EDIT_PARAMETERS = (
    "In Power BI Desktop, use Home > Transform data > Edit parameters (or Manage parameters in "
    "the Power Query Editor)."
)


class ParameterProblem(ValueError):
    """A parameter value Studio will not write; the message is for the user."""


@dataclass(frozen=True)
class ParameterSettings:
    """A Power Query parameter's M, split: the value literal, the meta record verbatim, its
    Type and the M literals of its suggested values (None when it has no list)."""

    literal: str
    meta: str
    type_name: str | None
    choices: list[str] | None


def _m_literal_end(text: str, start: int) -> int | None:
    """Where the M literal at ``start`` ends: text, #date(...)-style, number, logical, null."""
    if start >= len(text):
        return None
    if text[start] == '"':
        index = start + 1
        while index < len(text):
            if text[index] == '"':
                if text[index + 1 : index + 2] == '"':
                    index += 2
                    continue
                return index + 1
            index += 1
        return None
    if text[start] == "#":
        if re.match(r"#\w+\s*\(", text[start:]) is None:
            return None
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    return index + 1
        return None
    found = re.match(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|true\b|false\b|null\b", text[start:])
    return start + found.end() if found else None


def _top_level_items(text: str) -> list[str]:
    """The comma-separated items of an M list body, commas inside texts and calls kept."""
    items: list[str] = []
    depth, start, index, quoted = 0, 0, 0, False
    while index < len(text):
        char = text[index]
        if quoted:
            if char == '"':
                if text[index + 1 : index + 2] == '"':
                    index += 1
                else:
                    quoted = False
        elif char == '"':
            quoted = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            items.append(text[start:index].strip())
            start = index + 1
        index += 1
    tail = text[start:].strip()
    if tail:
        items.append(tail)
    return items


def parameter_settings(text: str) -> ParameterSettings | None:
    """The parts of a parameter's M (``<literal> meta [IsParameterQuery=true, ...]``); None when
    ``text`` is not a parameter Studio can read."""
    stripped = text.strip()
    end = _m_literal_end(stripped, 0)
    if end is None:
        return None
    rest = stripped[end:].lstrip()
    if not rest.startswith("meta"):
        return None
    meta = rest[len("meta") :].strip()
    if not (meta.startswith("[") and meta.endswith("]")):
        return None
    if re.search(r"\bIsParameterQuery\s*=\s*true\b", meta) is None:
        return None
    found_type = re.search(r'\bType\s*=\s*"(\w+)"', meta)
    choices: list[str] | None = None
    listed = re.search(r"\bList\s*=\s*\{", meta)
    if listed is not None:
        depth, index, quoted = 1, listed.end(), False
        while index < len(meta):
            char = meta[index]
            if quoted:
                if char == '"':
                    if meta[index + 1 : index + 2] == '"':
                        index += 1
                    else:
                        quoted = False
            elif char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    choices = _top_level_items(meta[listed.end() : index])
                    break
            index += 1
    return ParameterSettings(
        stripped[:end], meta, found_type.group(1) if found_type else None, choices
    )


def _m_text(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _parse_moment(value: str) -> datetime | None:
    text = value.strip().replace("T", " ")
    for shape in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, shape)
        except ValueError:
            continue
    return None


def parameter_literal(value: str | int | float | bool, kind: str) -> str:
    """The M literal for ``value`` as a parameter of ``kind``; ParameterProblem when the value
    is not one."""
    if kind == "true_false":
        if isinstance(value, bool):
            return "true" if value else "false"
        word = str(value).strip().casefold()
        if word in ("true", "yes", "on", "1"):
            return "true"
        if word in ("false", "no", "off", "0"):
            return "false"
        raise ParameterProblem(f"{value!r} is not true or false")
    if kind == "number":
        if isinstance(value, bool):
            raise ParameterProblem(f"{value!r} is not a number")
        text = str(value).strip()
        if re.fullmatch(r"-?\d+", text):
            return str(int(text))
        try:
            number = float(text)
        except ValueError:
            raise ParameterProblem(f"{value!r} is not a number") from None
        if number != number or number in (float("inf"), float("-inf")):
            raise ParameterProblem(f"{value!r} is not a number")
        return repr(number)
    if kind in ("date", "date_time"):
        moment = _parse_moment(str(value))
        if moment is None or isinstance(value, bool):
            example = "2024-01-31" if kind == "date" else "2024-01-31 08:30"
            raise ParameterProblem(f"{value!r} is not a date; write it as {example}")
        if kind == "date":
            day: date = moment.date()
            return f"#date({day.year}, {day.month}, {day.day})"
        return (
            f"#datetime({moment.year}, {moment.month}, {moment.day}, {moment.hour}, "
            f"{moment.minute}, {moment.second})"
        )
    if kind == "time":
        clock: time | None = None
        for shape in ("%H:%M:%S", "%H:%M"):
            try:
                clock = datetime.strptime(str(value).strip(), shape).time()
                break
            except ValueError:
                continue
        if clock is None or isinstance(value, bool):
            raise ParameterProblem(f"{value!r} is not a time; write it as 08:30")
        return f"#time({clock.hour}, {clock.minute}, {clock.second})"
    return _m_text(str(value))


def _same_literal(a: str, b: str) -> bool:
    return "".join(a.split()) == "".join(b.split())


def set_parameter_value(
    existing: str | None, value: str | int | float | bool, kind: str | None
) -> str:
    """A parameter's M with ``value``. A new parameter (``existing`` None) is written the way
    Manage Parameters writes one (required, the type ``kind`` or the value's own); an existing one
    keeps its whole meta record (type, suggested values, required) and only its value changes.
    ParameterProblem says why Studio will not write it."""
    if existing is None:
        own = kind or (
            "true_false"
            if isinstance(value, bool)
            else "number"
            if isinstance(value, int | float)
            else "text"
        )
        type_name = next(t for t, k in PARAMETER_TYPES.items() if k == own)
        return (
            f"{parameter_literal(value, own)} meta [IsParameterQuery=true, "
            f'Type="{type_name}", IsParameterQueryRequired=true]'
        )
    settings = parameter_settings(existing)
    if settings is None:
        raise ParameterProblem(
            "Studio cannot read how this parameter is set up, so it leaves it as it is. "
            + _EDIT_PARAMETERS
        )
    own_kind = PARAMETER_TYPES.get(settings.type_name or "")
    if own_kind is None:
        raise ParameterProblem(
            f"The parameter holds {settings.type_name or 'values of any type'}, which Studio does "
            "not change. " + _EDIT_PARAMETERS
        )
    if kind is not None and kind != own_kind:
        raise ParameterProblem(
            f"The parameter holds {_TYPE_WORDS[own_kind]}; Studio keeps a parameter's type. "
            "To change the type: " + _EDIT_PARAMETERS
        )
    literal = parameter_literal(value, own_kind)
    if settings.choices is not None and not any(
        _same_literal(literal, choice) for choice in settings.choices
    ):
        shown = ", ".join(settings.choices[:6])
        raise ParameterProblem(f"The parameter takes one of its listed values: {shown}")
    return f"{literal} meta {settings.meta}"


def parameter_m(value: str | int | float | bool, kind: str | None) -> str:
    """A new Power Query parameter's M, as Desktop's Manage Parameters writes it."""
    return set_parameter_value(None, value, kind)


def _set_query_parameter(
    document: PbirDocument, op: SetQueryParameter, model: ModelInfo
) -> HandlerResult:
    _refuse(_name_problem(op.name, "parameter"))
    if model.table(op.name) is not None:
        raise DocumentError(f"{op.name!r} is the name of a table; give the parameter another name")
    if isinstance(op.value, str):
        _refuse(_text_problem(op.value, "value"))
    structure = model_structure(model)
    current = structure.expressions.get(_cf(op.name))
    existing = current["expression"] if current is not None else None
    if (
        existing is not None
        and existing.strip()
        and re.search(r"\bIsParameterQuery\s*=\s*true\b", existing) is None
    ):
        raise DocumentError(f"{op.name!r} is a query, not a parameter")
    try:
        text = set_parameter_value(existing, op.value, op.type)
    except ParameterProblem as error:
        raise DocumentError(f"'{op.name}': {error}") from None
    structure.expressions[_cf(op.name)] = {
        "name": current["name"] if current is not None else op.name,
        "expression": text,
    }
    warnings = ["The new value is used the next time the data is refreshed."]
    if current is None and "expressions" not in structure.known:
        warnings.append(
            "Studio could not read the model's queries; if the parameter exists, Power BI "
            "Desktop keeps its type."
        )
    return HandlerResult(
        f"Set the parameter '{op.name}' to {json.dumps(op.value)}.", warnings=warnings
    )


def _refresh_data(document: PbirDocument, op: RefreshData, model: ModelInfo) -> HandlerResult:
    if op.table is None:
        return HandlerResult("Asked Power BI to refresh the data of every table.")
    table = _table(model, op.table)
    return HandlerResult(f"Asked Power BI to refresh the data of '{table}'.")


AUTO_DATE_TIME_REFUSAL = (
    "Studio does not switch Auto date/time: Power BI Desktop also adds or removes a hidden date "
    "table for every date column when you do. In Power BI Desktop, go to File > Options and "
    "settings > Options > Current File > Data Load and tick or clear Auto date/time."
)
CULTURE_REFUSAL = (
    "Studio does not change the model's language: Power BI Desktop sets it when a file is "
    "created. The setting for new files is File > Options and settings > Options > Global > "
    "Regional settings > Model language."
)


def _set_model_options(
    document: PbirDocument, op: SetModelOptions, model: ModelInfo
) -> HandlerResult:
    # Both are refused whatever else the step asks: turning Auto date/time on or off in Desktop
    # also creates or deletes hidden LocalDateTable tables and their column variations, which the
    # model annotation alone does not (spec review, 2026-09-29).
    if op.auto_date_time is not None:
        raise DocumentError(AUTO_DATE_TIME_REFUSAL)
    if op.culture is not None:
        raise DocumentError(CULTURE_REFUSAL)
    parts: list[str] = []
    warnings: list[str] = []
    structure = model_structure(model)
    if op.discourage_implicit_measures is not None:
        if op.discourage_implicit_measures is False and structure.groups:
            raise DocumentError(
                "A model with a calculation group must discourage implicit measures; delete the "
                "calculation group first"
            )
        parts.append(
            "implicit measures " + ("discouraged" if op.discourage_implicit_measures else "allowed")
        )
        if op.discourage_implicit_measures:
            warnings.append(
                "Visuals that already sum a column directly keep working; new ones need a measure."
            )
    if not parts:
        raise DocumentError("Say which model setting to change")
    return HandlerResult(f"Changed the model settings ({', '.join(parts)}).", warnings=warnings)


# --- the family's tables ----------------------------------------------------------------------

MODEL_HANDLERS: dict[str, Handler] = {
    "create_calculated_column": _create_calculated_column,
    "set_column_properties": _set_column_properties,
    "rename_column": _rename_column,
    "delete_column": _delete_column,
    "create_calculated_table": _create_calculated_table,
    "set_table_properties": _set_table_properties,
    "rename_table": _rename_table,
    "delete_table": _delete_table,
    "mark_date_table": _mark_date_table,
    "create_relationship": _create_relationship,
    "update_relationship": _update_relationship,
    "delete_relationship": _delete_relationship,
    "create_hierarchy": _create_hierarchy,
    "update_hierarchy": _update_hierarchy,
    "delete_hierarchy": _delete_hierarchy,
    "create_field_parameter": _create_field_parameter,
    "update_field_parameter": _update_field_parameter,
    "create_calculation_group": _create_calculation_group,
    "set_calculation_item": _set_calculation_item,
    "delete_calculation_item": _delete_calculation_item,
    "update_calculation_group": _update_calculation_group,
    "create_role": _create_role,
    "update_role": _update_role,
    "delete_role": _delete_role,
    "set_table_query": _set_table_query,
    "set_named_expression": _set_named_expression,
    "delete_named_expression": _delete_named_expression,
    "set_query_parameter": _set_query_parameter,
    "refresh_data": _refresh_data,
    "set_model_options": _set_model_options,
}

# What a model operation does to the report after Desktop applied its model half: only renames.
MODEL_REPORT_HANDLERS: dict[str, Handler] = {
    "rename_column": _rename_column_report_part,
    "rename_table": _rename_table_report_part,
}


def check_model_operation(document: PbirDocument, op: Any, model: ModelInfo) -> HandlerResult:
    """Run one operation's handler (what the facade does once the family is registered)."""
    result = MODEL_HANDLERS[op.op](document, op, model)
    return result if isinstance(result, HandlerResult) else HandlerResult(result)


def apply_model_family(
    document: PbirDocument, operations: list[Any], model: ModelInfo
) -> list[HandlerResult]:
    """This family's own dispatcher, mirroring ``report_edits.apply_operations`` for its ops."""
    results: list[HandlerResult] = []
    for index, op in enumerate(operations):
        try:
            results.append(check_model_operation(document, op, model))
        except DocumentError as error:
            if len(operations) > 1 and not hasattr(error, "step"):
                raise DocumentError(f"Step {index + 1}: {error}") from error
            raise
    return results


# --- Microsoft's list of what external tools may change in Power BI Desktop -------------------

# "External tools in Power BI Desktop" (Microsoft Learn) lists the modeling changes an external
# tool may make to the model Desktop has open: measures, calculated columns, calculated tables,
# relationships, calculation groups, perspectives, translations, row- and object-level security,
# annotations, and column properties. It lists renaming columns, table-level changes and Power
# Query (M) as not supported; hierarchies and data refresh are not listed. Every operation is
# tagged: "documented", "outside" or "depends" (on the object: deleting a calculated table is
# documented, deleting a table loaded from its source is not). An operation outside the list runs
# only when a passing model probe receipt for the running Desktop version lists its key
# (``outside_support``, ``desktop_session.DesktopController.model_probe_coverage``).
MICROSOFT_SUPPORT: dict[str, str] = {
    "create_calculated_column": "documented",
    "set_column_properties": "documented",
    "rename_column": "outside",
    "delete_column": "depends",
    "create_calculated_table": "documented",
    "set_table_properties": "depends",
    "rename_table": "outside",
    "delete_table": "depends",
    "mark_date_table": "outside",
    "create_relationship": "documented",
    "update_relationship": "documented",
    "delete_relationship": "documented",
    "create_hierarchy": "outside",
    "update_hierarchy": "outside",
    "delete_hierarchy": "outside",
    "create_field_parameter": "documented",
    "update_field_parameter": "documented",
    "create_calculation_group": "documented",
    "set_calculation_item": "documented",
    "delete_calculation_item": "documented",
    "update_calculation_group": "documented",
    "create_role": "documented",
    "update_role": "documented",
    "delete_role": "documented",
    "set_table_query": "outside",
    "set_named_expression": "outside",
    "delete_named_expression": "outside",
    "set_query_parameter": "outside",
    "refresh_data": "outside",
    "set_model_options": "outside",
}
# The probe key of each kind of change outside the list: (what it is, Power BI Desktop's steps).
_QUERY_STEPS = (
    "In Power BI Desktop, use Home > Transform data, change the query in the Power Query Editor, "
    "then Close & Apply."
)
OUTSIDE_SUPPORT: dict[str, tuple[str, str]] = {
    "rename_column": (
        "Renaming a column",
        "In Power BI Desktop, right-click the column in the Data pane and choose Rename; Desktop "
        "updates the visuals and formulas that use it.",
    ),
    "rename_table": (
        "Renaming a table",
        "In Power BI Desktop, right-click the table in the Data pane and choose Rename.",
    ),
    "delete_data_column": (
        "Deleting a column loaded from the data source",
        "In Power BI Desktop, right-click the column in the Data pane and choose Delete from "
        "model, or remove it in Power Query (Home > Transform data).",
    ),
    "delete_data_table": (
        "Deleting a table loaded from the data source",
        "In Power BI Desktop, right-click the table in the Data pane and choose Delete from model.",
    ),
    "data_table_properties": (
        "Changing a table loaded from the data source",
        "In Power BI Desktop, right-click the table in the Data pane and choose Hide in report "
        "view, or set its description in the Model view's Properties pane.",
    ),
    "mark_date_table": (
        "Marking a date table",
        "In Power BI Desktop, select the table, then Table tools > Mark as date table.",
    ),
    "hierarchies": (
        "Changing hierarchies",
        "In Power BI Desktop's Model view, right-click a column and choose Create hierarchy, or "
        "change a hierarchy's levels in the Properties pane.",
    ),
    "set_table_query": ("Changing a table's Power Query query", _QUERY_STEPS),
    "named_expressions": ("Changing Power Query queries", _QUERY_STEPS),
    "query_parameters": (
        "Changing Power Query parameters",
        "In Power BI Desktop, use Home > Transform data > Edit parameters.",
    ),
    "refresh_data": ("Refreshing the data", "In Power BI Desktop, use Home > Refresh."),
    "model_options": (
        "Changing the model's settings",
        "In Power BI Desktop's Model view, select the model and set Discourage implicit measures "
        "in the Properties pane.",
    ),
}
_OUTSIDE_KEYS = {
    "rename_column": "rename_column",
    "rename_table": "rename_table",
    "mark_date_table": "mark_date_table",
    "create_hierarchy": "hierarchies",
    "update_hierarchy": "hierarchies",
    "delete_hierarchy": "hierarchies",
    "set_table_query": "set_table_query",
    "set_named_expression": "named_expressions",
    "delete_named_expression": "named_expressions",
    "set_query_parameter": "query_parameters",
    "refresh_data": "refresh_data",
    "set_model_options": "model_options",
}
# The probe key of an operation tagged "depends" when its object is loaded from the data source
# (on a calculated column or table it is documented): what ``outside_support`` gives it.
DEPENDS_KEYS: dict[str, str] = {
    "delete_column": "delete_data_column",
    "delete_table": "delete_data_table",
    "set_table_properties": "data_table_properties",
}


def support_keys() -> dict[str, str]:
    """Operation -> the probe key that gates it: always for one outside Microsoft's list, and for
    one tagged "depends" when its object comes from the data source (``DEPENDS_KEYS``)."""
    return {**_OUTSIDE_KEYS, **DEPENDS_KEYS}


def outside_support(operations: list[Any], model: ModelInfo) -> list[str]:
    """The probe keys of the changes in ``operations`` that are outside Microsoft's documented
    list, in order and each once. ``model`` is Studio's view before the batch; a column or table
    an earlier step of the batch creates counts as calculated."""
    structure = model_structure(model)
    created_columns: set[tuple[str, str]] = set()
    created_tables: set[str] = set()
    found: list[str] = []
    for op in operations:
        name = str(getattr(op, "op", ""))
        tag = MICROSOFT_SUPPORT.get(name)
        key: str | None = None
        if tag == "outside":
            key = _OUTSIDE_KEYS[name]
        elif isinstance(op, DeleteColumn):
            table = model.table(op.table) or op.table
            calculated = (_cf(table), _cf(op.name)) in created_columns or (
                _is_calculated_column(model, structure, table, op.name) is True
            )
            key = None if calculated else "delete_data_column"
        elif isinstance(op, DeleteTable | SetTableProperties):
            table = model.table(op.name if isinstance(op, DeleteTable) else op.table) or (
                op.name if isinstance(op, DeleteTable) else op.table
            )
            data = (
                _cf(table) not in created_tables and _table_noun(structure, model, table) == "table"
            )
            if data:
                key = (
                    "delete_data_table" if isinstance(op, DeleteTable) else "data_table_properties"
                )
        if isinstance(op, CreateCalculatedColumn):
            created_columns.add((_cf(model.table(op.table) or op.table), _cf(op.name)))
        elif isinstance(op, CreateCalculatedTable | CreateFieldParameter | CreateCalculationGroup):
            created_tables.add(_cf(op.name))
        if key is not None and key not in found:
            found.append(key)
    return found


def support_refusal(key: str, version: str | None) -> str:
    """Why an operation outside Microsoft's list was not made, with Desktop's own steps."""
    what, steps = OUTSIDE_SUPPORT[key]
    tested = f"Power BI Desktop {version}" if version else "this Power BI Desktop"
    return (
        f"{what} is outside the changes Microsoft supports for tools like Studio in Power BI "
        f"Desktop, and Studio has not tested it with {tested} yet, so nothing was changed. {steps}"
    )


def support_note(key: str, version: str | None) -> str:
    """The receipt's note for an operation outside the list that a probe receipt covers."""
    what, _ = OUTSIDE_SUPPORT[key]
    return (
        f"{what} is outside Microsoft's supported list; Studio tested it with Power BI Desktop "
        f"{version or 'on this computer'}."
    )


# --- did Desktop keep it: the saved file's model against the batch ----------------------------


def _flat(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def _same(a: str, b: str) -> bool:
    return _cf(a) == _cf(b)


# An object is named by a tuple: ("column", table, name), ("table", name), ("hierarchy", table,
# name), ("item", group, name), ("role", name), ("relationship", table, column, table, column),
# ("expression", name), ("query", table) or ("settings",). Names keep their spelling.
Identity = tuple[str, ...]
_TABLE_PARTS = {"column": (1,), "hierarchy": (1,), "item": (1,), "query": (1,), "table": (1,)}


def _set_fields(op: Any, names: tuple[str, ...]) -> set[str]:
    return {name for name in names if getattr(op, name, None) is not None}


@dataclass
class _Fate:
    """What the rest of a batch does to one object: its identity at the end (names carried
    through later renames), whether a later step deletes it or its table, and which of its
    properties a later step sets again (those are the later step's to check)."""

    identity: Identity
    gone: bool = False
    touched: set[str] = field(default_factory=set[str])


def _renamed_table(identity: Identity, old: str, new: str) -> Identity:
    parts = list(identity)
    places = (1, 3) if identity[0] == "relationship" else _TABLE_PARTS.get(identity[0], ())
    for place in places:
        if _same(parts[place], old):
            parts[place] = new
    return tuple(parts)


def _on_table(identity: Identity, table: str) -> bool:
    places = (1, 3) if identity[0] == "relationship" else _TABLE_PARTS.get(identity[0], ())
    return any(_same(identity[place], table) for place in places)


def _same_ends(identity: Identity, op: Any) -> bool:
    ends = {
        (_cf(op.from_table), _cf(op.from_column)),
        (_cf(op.to_table), _cf(op.to_column)),
    }
    return ends == {(_cf(identity[1]), _cf(identity[2])), (_cf(identity[3]), _cf(identity[4]))}


def _table_of_step(op: Any) -> str:
    """The table a step changes as a whole ("" for a step that changes no table)."""
    if isinstance(op, SetTableProperties | MarkDateTable):
        return op.table
    if isinstance(op, UpdateFieldParameter | UpdateCalculationGroup):
        return op.name
    if isinstance(op, SetCalculationItem | DeleteCalculationItem):
        return op.group
    return ""


def _fate(identity: Identity, later: list[Any]) -> _Fate:
    """``identity`` carried through the steps after the one that named it."""
    fate = _Fate(identity)
    for op in later:
        current = fate.identity
        kind = current[0]
        if isinstance(op, RenameTable):
            fate.identity = _renamed_table(current, op.name, op.new_name)
        elif isinstance(op, DeleteTable):
            fate.gone = _on_table(current, op.name)
        elif kind == "column" and isinstance(op, RenameColumn | DeleteColumn | SetColumnProperties):
            if not (_same(op.table, current[1]) and _same(op.name, current[2])):
                continue
            if isinstance(op, RenameColumn):
                fate.identity = ("column", current[1], op.new_name)
            elif isinstance(op, DeleteColumn):
                fate.gone = True
            else:
                fate.touched |= _set_fields(op, _COLUMN_FIELDS)
        elif kind == "relationship":
            if isinstance(op, RenameColumn):
                parts = list(current)
                for place in (1, 3):
                    if _same(parts[place], op.table) and _same(parts[place + 1], op.name):
                        parts[place + 1] = op.new_name
                fate.identity = tuple(parts)
            elif isinstance(op, DeleteColumn):
                fate.gone = any(
                    _same(current[place], op.table) and _same(current[place + 1], op.name)
                    for place in (1, 3)
                )
            elif isinstance(op, DeleteRelationship) and _same_ends(current, op):
                fate.gone = True
            elif isinstance(op, UpdateRelationship) and _same_ends(current, op):
                fate.touched |= _set_fields(op, _RELATIONSHIP_FIELDS)
        elif kind == "hierarchy" and isinstance(op, UpdateHierarchy | DeleteHierarchy):
            if not (_same(op.table, current[1]) and _same(op.name, current[2])):
                continue
            if isinstance(op, DeleteHierarchy):
                fate.gone = True
                continue
            if op.levels is not None:
                fate.touched.add("levels")
            if op.new_name is not None:
                fate.identity = ("hierarchy", current[1], op.new_name)
        elif kind == "item" and isinstance(op, SetCalculationItem | DeleteCalculationItem):
            if not (_same(op.group, current[1]) and _same(op.name, current[2])):
                continue
            if isinstance(op, DeleteCalculationItem):
                fate.gone = True
                continue
            fate.touched |= _set_fields(op, ("expression", "format_string_expression", "ordinal"))
            if op.new_name is not None:
                fate.identity = ("item", current[1], op.new_name)
        elif kind == "table" and _same(_table_of_step(op), current[1]):
            if isinstance(op, SetTableProperties):
                fate.touched |= _set_fields(op, ("hidden", "description", "expression"))
            elif isinstance(op, MarkDateTable):
                fate.touched.add("date_table")
            elif isinstance(op, UpdateFieldParameter):
                fate.touched.add("fields")
            elif isinstance(op, UpdateCalculationGroup):
                fate.touched |= _set_fields(op, ("precedence", "description"))
            elif isinstance(op, SetCalculationItem | DeleteCalculationItem):
                fate.touched.add("items")
        elif kind == "role" and isinstance(op, UpdateRole | DeleteRole):
            if not _same(op.name, current[1]):
                continue
            if isinstance(op, DeleteRole):
                fate.gone = True
                continue
            if op.description is not None:
                fate.touched.add("description")
            for item in op.filters or []:
                fate.touched.add("filter:" + _cf(item.table))
            if op.new_name is not None:
                fate.identity = ("role", op.new_name)
        elif kind == "expression" and isinstance(
            op, SetNamedExpression | SetQueryParameter | DeleteNamedExpression
        ):
            if not _same(op.name, current[1]):
                continue
            if isinstance(op, DeleteNamedExpression):
                fate.gone = True
            else:
                fate.touched.add("expression")
                if op.description is not None:
                    fate.touched.add("description")
        elif kind == "query" and isinstance(op, SetTableQuery) and _same(op.table, current[1]):
            fate.touched.add("query")
        elif kind == "settings" and isinstance(op, SetModelOptions):
            fate.touched |= _set_fields(op, ("discourage_implicit_measures",))
        if fate.gone:
            break
    return fate


def _taken_again(identity: Identity, later: list[Any]) -> bool:
    """True when a later step gives ``identity``'s name to an object again (the column a rename
    freed is created anew), so the name being there proves nothing about this step."""
    current = identity
    for op in later:
        if isinstance(op, RenameTable):
            if current[0] == "table" and _same(op.new_name, current[1]):
                return True
            current = _renamed_table(current, op.name, op.new_name)
            continue
        if _gives_name(op, current):
            return True
    return False


def _gives_name(op: Any, current: Identity) -> bool:
    kind = current[0]
    given: str | None = None
    if kind == "column" and isinstance(op, CreateCalculatedColumn | RenameColumn):
        given = op.new_name if isinstance(op, RenameColumn) else op.name
        return _same(op.table, current[1]) and _same(given, current[2])
    if kind == "table" and isinstance(
        op, CreateCalculatedTable | CreateFieldParameter | CreateCalculationGroup
    ):
        return _same(op.name, current[1])
    if kind == "hierarchy" and isinstance(op, CreateHierarchy | UpdateHierarchy):
        given = op.new_name if isinstance(op, UpdateHierarchy) else op.name
        return given is not None and _same(op.table, current[1]) and _same(given, current[2])
    if kind == "item" and isinstance(op, SetCalculationItem):
        return _same(op.group, current[1]) and _same(op.new_name or op.name, current[2])
    if kind == "role" and isinstance(op, CreateRole | UpdateRole):
        given = op.new_name if isinstance(op, UpdateRole) else op.name
        return given is not None and _same(given, current[1])
    if kind == "relationship" and isinstance(op, CreateRelationship):
        return _same_ends(current, op)
    if kind == "expression" and isinstance(op, SetNamedExpression | SetQueryParameter):
        return _same(op.name, current[1])
    return False


def _forward_dax(text: str, own_table: str | None, later: list[Any]) -> str:
    """A formula as the later renames of the batch leave it (Studio rewrites every formula that
    names a renamed table or column in the same save)."""
    own = own_table
    for op in later:
        if isinstance(op, RenameTable):
            text = rename_dax_table(text, op.name, op.new_name)[0]
            if own is not None and _same(own, op.name):
                own = op.new_name
        elif isinstance(op, RenameColumn):
            text = rename_dax_column(text, op.table, op.name, op.new_name, own_table=own)[0]
    return text


def _forward_field(table: str, name: str, later: list[Any]) -> tuple[str, str]:
    """A field parameter's field (a column or a measure) through the later renames."""
    for op in later:
        if isinstance(op, RenameTable) and _same(op.name, table):
            table = op.new_name
        elif (
            isinstance(op, RenameColumn) and _same(op.table, table) or isinstance(op, RenameMeasure)
        ) and _same(op.name, name):
            name = op.new_name
    return table, name


_RELATIONSHIP_FIELDS = (
    "cardinality",
    "cross_filter",
    "active",
    "assume_referential_integrity",
    "security_filter",
)
_TYPE_NORMAL = {
    "string": "text",
    "2": "text",
    "int64": "integer",
    "6": "integer",
    "8": "double",
    "10": "decimal",
    "9": "datetime",
    "11": "boolean",
}


def _text_value(value: Any) -> str:
    return _str(value).strip()


def _word_value(value: Any) -> str:
    return _str(value).casefold().replace("_", "").replace(" ", "")


def _type_value(value: Any) -> str:
    text = _str(value).casefold()
    return _TYPE_NORMAL.get(text, text)


def _flag_value(value: Any) -> str:
    return "true" if _bool(value) else "false"


_SECTION_WORDS = {
    "tables": "tables",
    "columns": "columns",
    "relationships": "relationships",
    "hierarchies": "hierarchies",
    "roles": "roles",
    "calculation_groups": "calculation groups",
    "field_parameters": "field parameters",
    "partitions": "queries",
    "expressions": "queries and parameters",
    "settings": "settings",
}


class _SavedCheck:
    """One pass of ``saved_model_problems``: the saved model's structure, what it lacks, and
    what the facts could not show (a whole section, or one property)."""

    def __init__(self, facts: Mapping[str, Any] | None) -> None:
        self.structure = ModelStructure.from_facts(facts)
        self.problems: list[str] = []
        self.unchecked: list[str] = []

    def unknown(self, word: str) -> None:
        if word not in self.unchecked:
            self.unchecked.append(word)

    def known(self, section: str) -> bool:
        if section in self.structure.known:
            return True
        self.unknown(_SECTION_WORDS[section])
        return False

    def problem(self, text: str) -> None:
        if text not in self.problems:
            self.problems.append(text)

    def same(
        self,
        label: str,
        word: str,
        wanted: Any,
        found: Any,
        normal: Callable[[Any], str] = _text_value,
    ) -> None:
        """``label`` is a problem when the saved value differs; an unknown one is unchecked."""
        if wanted is None:
            return
        if found is None:
            self.unknown(word)
        elif normal(found) != normal(wanted):
            self.problem(label)

    def column(self, table: str, name: str) -> dict[str, Any] | None:
        return self.structure.columns.get((_cf(table), _cf(name)))

    def table(self, name: str) -> dict[str, Any] | None:
        return self.structure.tables.get(_cf(name))

    def formula(
        self, label: str, wanted: str, found: Any, own: str | None, later: list[Any]
    ) -> None:
        """A formula as the batch left it: the step's own text, or the text the later renames
        rewrote it to."""
        if found is None:
            self.unknown("formulas")
            return
        if _flat(found) not in (_flat(wanted), _flat(_forward_dax(wanted, own, later))):
            self.problem(label)


def _check_column(check: _SavedCheck, op: Any, later: list[Any]) -> None:
    fate = _fate(("column", op.table, op.name), later)
    if fate.gone or not check.known("columns"):
        return
    _, table, name = fate.identity
    found = check.column(table, name)
    if found is None:
        check.problem(f"the column {table}[{name}] is missing")
        return
    touched = fate.touched
    label = f"{table}[{name}]"
    if op.expression is not None and "expression" not in touched:
        check.formula(
            f"the formula of {label} was not kept",
            op.expression,
            found.get("expression"),
            op.table,
            later,
        )
    for attr, key, word, normal, wanted in (
        ("format_string", "format_string", "column formats", _text_value, op.format_string),
        ("data_type", "data_type", "data types", _type_value, DATA_TYPES.get(op.data_type or "")),
        (
            "data_category",
            "data_category",
            "data categories",
            _word_value,
            DATA_CATEGORIES.get(getattr(op, "data_category", None) or "")
            if getattr(op, "data_category", None) is not None
            else None,
        ),
        (
            "summarize_by",
            "summarize_by",
            "summarizations",
            _word_value,
            SUMMARIZE_BY.get(op.summarize_by or ""),
        ),
        ("folder", "folder", "display folders", _text_value, op.folder),
        ("description", "description", "descriptions", _text_value, op.description),
        ("hidden", "hidden", "column visibility", _flag_value, op.hidden),
        ("key", "key_known", "key columns", _flag_value, getattr(op, "key", None)),
    ):
        if attr in touched or wanted is None:
            continue
        said = {
            "hidden": f"{label} is not {'hidden' if wanted else 'shown'}",
            "key": f"{label} is {'not ' if wanted else ''}a key column",
            "summarize_by": f"the summarization of {label} was not kept",
        }.get(attr, f"the {attr.replace('_', ' ')} of {label} was not kept")
        check.same(said, word, wanted, found.get(key), normal)
    sort_by = getattr(op, "sort_by", None)
    if sort_by is not None and "sort_by" not in touched:
        wanted_sort = _fate(("column", op.table, sort_by), later).identity[2] if sort_by else ""
        check.same(
            f"{label} is not sorted by [{sort_by}]" if sort_by else f"{label} is still sorted",
            "sort orders",
            wanted_sort,
            found.get("sort_by"),
            _word_value,
        )


def _check_gone(check: _SavedCheck, identity: Identity, later: list[Any], section: str) -> None:
    """A deleted object, or the name a rename gave up, is not in the saved model (unless a later
    step names something that way again)."""
    fate = _fate(identity, later)
    if fate.gone or _taken_again(identity, later) or not check.known(section):
        return
    kind, *names = fate.identity
    structure = check.structure
    there = {
        "column": lambda: check.column(names[0], names[1]) is not None,
        "table": lambda: check.table(names[0]) is not None,
        "hierarchy": lambda: (_cf(names[0]), _cf(names[1])) in structure.hierarchies,
        "item": lambda: _cf(names[1]) in structure.groups.get(_cf(names[0]), {}).get("items", {}),
        "role": lambda: _cf(names[0]) in structure.roles,
        "expression": lambda: _cf(names[0]) in structure.expressions,
        "relationship": lambda: (
            structure.relationship((names[0], names[1]), (names[2], names[3])) is not None
        ),
    }[kind]()
    if there:
        check.problem(f"{_identity_words(fate.identity)} is still there")


def _identity_words(identity: Identity) -> str:
    kind, names = identity[0], identity[1:]
    if kind == "column":
        return f"the column {names[0]}[{names[1]}]"
    if kind == "hierarchy":
        return f"the hierarchy '{names[1]}' of {names[0]}"
    if kind == "item":
        return f"the calculation item '{names[1]}' of '{names[0]}'"
    if kind == "relationship":
        return f"the relationship between {names[0]}[{names[1]}] and {names[2]}[{names[3]}]"
    if kind == "role":
        return f"the role '{names[0]}'"
    if kind == "expression":
        return f"the query '{names[0]}'"
    return f"the table '{names[0]}'"


def _check_relationship(check: _SavedCheck, op: Any, later: list[Any]) -> None:
    identity = ("relationship", op.from_table, op.from_column, op.to_table, op.to_column)
    fate = _fate(identity, later)
    if fate.gone or not check.known("relationships"):
        return
    _, a_table, a_column, b_table, b_column = fate.identity
    rel = check.structure.relationship((a_table, a_column), (b_table, b_column))
    if rel is None:
        check.problem(
            f"the relationship between {a_table}[{a_column}] and {b_table}[{b_column}] is missing"
        )
        return
    touched, label = fate.touched, f"the relationship {rel.describe()}"
    if op.active is not None and "active" not in touched and rel.active != op.active:
        check.problem(f"{label} is not {'active' if op.active else 'inactive'}")
    wanted_both = op.cross_filter == "both"
    if op.cross_filter is not None and "cross_filter" not in touched and rel.both != wanted_both:
        check.problem(f"{label} does not filter as asked")
    if op.cardinality is not None and "cardinality" not in touched:
        if not rel.cardinality_known:
            check.unknown("relationship cardinality")
        else:
            many = {"many_to_one": (True, False), "one_to_one": (False, False)}.get(
                op.cardinality, (True, True)
            )
            straight = _same(rel.from_table, a_table) and _same(rel.from_column, a_column)
            found = (rel.from_many, rel.to_many) if straight else (rel.to_many, rel.from_many)
            if found != many:
                check.problem(f"{label} is not {_CARDINALITY_WORDS[op.cardinality]}")
    if (
        op.assume_referential_integrity is not None
        and "assume_referential_integrity" not in touched
    ):
        check.same(
            f"{label} does not assume referential integrity as asked",
            "referential integrity",
            op.assume_referential_integrity,
            rel.rely,
            _flag_value,
        )
    if op.security_filter is not None and "security_filter" not in touched:
        check.same(
            f"{label} does not apply the security filter as asked",
            "security filter directions",
            op.security_filter == "both",
            rel.security_both,
            _flag_value,
        )


def _parameter_fields(check: _SavedCheck, table: str) -> list[tuple[str, str]] | None:
    rows = check.structure.parameter_fields.get(_cf(table))
    if rows is not None:
        return rows
    text = (check.table(table) or {}).get("expression")
    if not isinstance(text, str) or not text:
        return None
    return [(r.table, r.name) for r in dax_references(text) if r.table and r.name]


def _saved_fields(check: _SavedCheck, op: Any, later: list[Any]) -> None:
    fate = _fate(("table", op.name), later)
    if fate.gone or "fields" in fate.touched:
        return
    table = fate.identity[1]
    found = _parameter_fields(check, table)
    if found is None:
        check.unknown("field parameter fields")
        return
    wanted = [_forward_field(f.table, f.name, later) for f in op.fields]
    if [(_cf(t), _cf(n)) for t, n in found] != [(_cf(t), _cf(n)) for t, n in wanted]:
        check.problem(f"the field parameter '{table}' does not list the fields asked for")


def _saved_item(check: _SavedCheck, group: str, name: str, spec: Any, later: list[Any]) -> None:
    fate = _fate(("item", group, name), later)
    if fate.gone:
        return
    _, group, name = fate.identity
    item = check.structure.groups.get(_cf(group), {}).get("items", {}).get(_cf(name))
    if item is None:
        check.problem(f"the calculation item '{name}' of '{group}' is missing")
        return
    expression = getattr(spec, "expression", None)
    if expression is not None and "expression" not in fate.touched:
        check.formula(
            f"the formula of the calculation item '{name}' was not kept",
            expression,
            item.get("expression"),
            None,
            later,
        )
    format_expression = getattr(spec, "format_string_expression", None)
    if format_expression is not None and "format_string_expression" not in fate.touched:
        found = item.get("format_string_expression")
        if found is None:
            check.unknown("format string formulas")
        elif _flat(found) not in (
            _flat(format_expression),
            _flat(_forward_dax(format_expression, None, later)),
        ):
            check.problem(f"the format string of the calculation item '{name}' was not kept")
    ordinal = getattr(spec, "ordinal", None)
    if ordinal is not None and "ordinal" not in fate.touched:
        check.same(
            f"the calculation item '{name}' is not in place {ordinal + 1}",
            "calculation item order",
            ordinal,
            item.get("ordinal"),
            _text_value,
        )


def _check_role(check: _SavedCheck, op: Any, later: list[Any]) -> None:
    name = getattr(op, "new_name", None) or op.name
    fate = _fate(("role", name), later)
    if fate.gone or not check.known("roles"):
        return
    role = fate.identity[1]
    filters = check.structure.roles.get(_cf(role))
    if filters is None:
        check.problem(f"the role '{role}' is missing")
        return
    have = {_cf(t): f for t, f in filters.items()}
    for item in op.filters or []:
        table_fate = _fate(("table", item.table), later)
        if table_fate.gone or "filter:" + _cf(item.table) in fate.touched:
            continue
        table = table_fate.identity[1]
        found = have.get(_cf(table), "")
        wanted = item.expression
        if _flat(found) not in (_flat(wanted), _flat(_forward_dax(wanted, item.table, later))):
            check.problem(f"the filter of '{role}' on {table} was not kept")
    if op.description is not None and "description" not in fate.touched:
        check.same(
            f"the description of the role '{role}' was not kept",
            "descriptions",
            op.description,
            check.structure.role_descriptions.get(_cf(role)),
        )


def _check_expression(check: _SavedCheck, op: Any, later: list[Any]) -> None:
    fate = _fate(("expression", op.name), later)
    if fate.gone or not check.known("expressions"):
        return
    found = check.structure.expressions.get(_cf(op.name))
    if found is None:
        check.problem(f"the query '{op.name}' is missing")
        return
    touched = fate.touched
    if "expression" not in touched:
        if isinstance(op, SetNamedExpression):
            if _flat(found["expression"]) != _flat(op.expression):
                check.problem(f"the query '{op.name}' was not kept as written")
        else:
            settings = parameter_settings(found["expression"])
            kind = PARAMETER_TYPES.get(settings.type_name or "") if settings else None
            if settings is None or kind is None:
                check.problem(f"'{op.name}' is no longer a parameter Studio can read")
            else:
                try:
                    wanted = parameter_literal(op.value, kind)
                except ParameterProblem:
                    wanted = None
                if wanted is None or not _same_literal(wanted, settings.literal):
                    check.problem(f"the parameter '{op.name}' does not hold the new value")
    if op.description is not None and "description" not in touched:
        check.same(
            f"the description of the query '{op.name}' was not kept",
            "descriptions",
            op.description,
            found.get("description"),
        )


def saved_model_problems(
    operations: list[Any], facts: Mapping[str, Any] | None
) -> tuple[list[str], list[str]]:
    """(what the saved model lacks of what the batch asked, what the facts could not show).

    Each model operation is held to every property it set: a column it created exists with its
    formula, format, type, summarization, category, folder, description, visibility and key; a
    relationship has the cardinality, direction, state, referential integrity and security filter
    asked; a calculation group its precedence, items, formulas and format strings; a field
    parameter its fields; a parameter its value; a role its filters. Names are carried forward
    through the renames later in ``operations`` (a role filter on a table the batch renames is
    looked up under the new name); a property a later step sets again is that step's to check,
    and an object a later step deletes is not checked. A section or property the facts do not
    show goes to the second list. ``operations`` may hold measure operations too: a measure
    rename is carried into a field parameter's fields."""
    check = _SavedCheck(facts)
    structure = check.structure
    for index, op in enumerate(operations):
        later = operations[index + 1 :]
        if isinstance(op, CreateCalculatedColumn | SetColumnProperties):
            _check_column(check, op, later)
        elif isinstance(op, RenameColumn):
            _check_column_rename(check, op, later)
        elif isinstance(op, DeleteColumn):
            _check_gone(check, ("column", op.table, op.name), later, "columns")
        elif isinstance(op, CreateCalculatedTable | CreateFieldParameter | CreateCalculationGroup):
            _check_new_table(check, op, later)
        elif isinstance(op, SetTableProperties):
            _check_table_properties(check, op, later)
        elif isinstance(op, RenameTable):
            fate = _fate(("table", op.new_name), later)
            if not fate.gone and check.known("tables"):
                if check.table(fate.identity[1]) is None:
                    check.problem(f"the table '{fate.identity[1]}' is missing")
                if not _same(op.name, op.new_name):
                    _check_gone(check, ("table", op.name), later, "tables")
        elif isinstance(op, DeleteTable):
            _check_gone(check, ("table", op.name), later, "tables")
        elif isinstance(op, MarkDateTable):
            _check_date_table(check, op, later)
        elif isinstance(op, CreateRelationship | UpdateRelationship):
            _check_relationship(check, op, later)
        elif isinstance(op, DeleteRelationship):
            identity = ("relationship", op.from_table, op.from_column, op.to_table, op.to_column)
            _check_gone(check, identity, later, "relationships")
        elif isinstance(op, CreateHierarchy | UpdateHierarchy):
            _check_hierarchy(check, op, later)
        elif isinstance(op, DeleteHierarchy):
            _check_gone(check, ("hierarchy", op.table, op.name), later, "hierarchies")
        elif isinstance(op, UpdateFieldParameter):
            _saved_fields(check, op, later)
        elif isinstance(op, SetCalculationItem) and check.known("calculation_groups"):
            _saved_item(check, op.group, op.new_name or op.name, op, later)
        elif isinstance(op, DeleteCalculationItem):
            _check_gone(check, ("item", op.group, op.name), later, "calculation_groups")
        elif isinstance(op, UpdateCalculationGroup) and check.known("calculation_groups"):
            fate = _fate(("table", op.name), later)
            group = structure.groups.get(_cf(fate.identity[1]))
            if not fate.gone and group is not None:
                if "precedence" not in fate.touched:
                    check.same(
                        f"the precedence of '{fate.identity[1]}' is not {op.precedence}",
                        "calculation group precedence",
                        op.precedence,
                        group.get("precedence"),
                    )
                if "description" not in fate.touched:
                    check.same(
                        f"the description of '{fate.identity[1]}' was not kept",
                        "descriptions",
                        op.description,
                        group.get("description"),
                    )
        elif isinstance(op, CreateRole | UpdateRole):
            _check_role(check, op, later)
        elif isinstance(op, DeleteRole):
            _check_gone(check, ("role", op.name), later, "roles")
        elif isinstance(op, SetTableQuery) and check.known("partitions"):
            fate = _fate(("query", op.table), later)
            if not fate.gone and "query" not in fate.touched:
                found = structure.partitions.get(_cf(fate.identity[1])) or []
                if not any(_flat(p["expression"]) == _flat(op.expression) for p in found):
                    check.problem(f"the query of '{fate.identity[1]}' was not kept")
        elif isinstance(op, SetNamedExpression | SetQueryParameter):
            _check_expression(check, op, later)
        elif isinstance(op, DeleteNamedExpression):
            _check_gone(check, ("expression", op.name), later, "expressions")
        elif isinstance(op, SetModelOptions) and check.known("settings"):
            fate = _fate(("settings",), later)
            if "discourage_implicit_measures" not in fate.touched:
                check.same(
                    "the implicit measures setting was not kept",
                    "model settings",
                    op.discourage_implicit_measures,
                    structure.settings.get("discourageImplicitMeasures"),
                    _flag_value,
                )
    return check.problems, check.unchecked


def _check_column_rename(check: _SavedCheck, op: RenameColumn, later: list[Any]) -> None:
    fate = _fate(("column", op.table, op.new_name), later)
    if fate.gone or not check.known("columns"):
        return
    _, table, name = fate.identity
    if check.column(table, name) is None:
        check.problem(f"the column {table}[{name}] is missing")
    if not _same(op.name, op.new_name):
        _check_gone(check, ("column", op.table, op.name), later, "columns")


def _check_new_table(check: _SavedCheck, op: Any, later: list[Any]) -> None:
    fate = _fate(("table", op.name), later)
    if fate.gone:
        return
    name = fate.identity[1]
    structure = check.structure
    if check.known("tables") and check.table(name) is None and _cf(name) not in structure.groups:
        check.problem(f"the table '{name}' is missing")
        return
    if isinstance(op, CreateCalculatedTable):
        found = check.table(name) or {}
        check.formula(
            f"the formula of the table '{name}' was not kept",
            op.expression,
            found.get("expression"),
            None,
            later,
        )
        _check_table_properties(check, op, later, table=name)
    elif isinstance(op, CreateFieldParameter):
        if check.known("field_parameters") and structure.table_kind(name) != "fieldParameter":
            check.problem(f"'{name}' is not a field parameter")
        _saved_fields(check, op, later)
    elif isinstance(op, CreateCalculationGroup) and check.known("calculation_groups"):
        group = structure.groups.get(_cf(name))
        if group is None:
            check.problem(f"'{name}' is not a calculation group")
            return
        if "precedence" not in fate.touched:
            check.same(
                f"the precedence of '{name}' is not {op.precedence}",
                "calculation group precedence",
                op.precedence,
                group.get("precedence"),
            )
        if op.description is not None and "description" not in fate.touched:
            check.same(
                f"the description of '{name}' was not kept",
                "descriptions",
                op.description,
                group.get("description"),
            )
        for spec in op.items:
            _saved_item(check, op.name, spec.name, spec, later)


def _check_table_properties(
    check: _SavedCheck, op: Any, later: list[Any], *, table: str | None = None
) -> None:
    name = table or op.table
    fate = _fate(("table", name), later)
    if fate.gone or not check.known("tables"):
        return
    name = fate.identity[1]
    found = check.table(name)
    if found is None:
        check.problem(f"the table '{name}' is missing")
        return
    touched = fate.touched
    if op.hidden is not None and "hidden" not in touched:
        check.same(
            f"'{name}' is not {'hidden' if op.hidden else 'shown'}",
            "table visibility",
            op.hidden,
            found.get("hidden"),
            _flag_value,
        )
    if op.description is not None and "description" not in touched:
        check.same(
            f"the description of '{name}' was not kept",
            "descriptions",
            op.description,
            found.get("description"),
        )
    new_formula = op.expression if isinstance(op, SetTableProperties) else None
    if new_formula is not None and "expression" not in touched:
        check.formula(
            f"the formula of the table '{name}' was not kept",
            new_formula,
            found.get("expression"),
            None,
            later,
        )


def _check_date_table(check: _SavedCheck, op: MarkDateTable, later: list[Any]) -> None:
    fate = _fate(("table", op.table), later)
    if fate.gone or "date_table" in fate.touched or not check.known("tables"):
        return
    name = fate.identity[1]
    flag = (check.table(name) or {}).get("date_table")
    if flag is None:
        check.unknown("date tables")
    elif flag != (op.column is not None):
        check.problem(f"'{name}' is {'not ' if op.column else ''}marked as a date table")
    if op.column is not None and flag:
        column = _fate(("column", op.table, op.column), later).identity
        found = check.column(column[1], column[2])
        if found is not None:
            check.same(
                f"[{column[2]}] is not the date column of '{name}'",
                "key columns",
                True,
                found.get("key_known"),
                _flag_value,
            )


def _check_hierarchy(check: _SavedCheck, op: Any, later: list[Any]) -> None:
    name = getattr(op, "new_name", None) or op.name
    fate = _fate(("hierarchy", op.table, name), later)
    if fate.gone or not check.known("hierarchies"):
        return
    _, table, name = fate.identity
    levels = check.structure.hierarchies.get((_cf(table), _cf(name)))
    if levels is None:
        check.problem(f"the hierarchy '{name}' of {table} is missing")
        return
    if op.levels is not None and "levels" not in fate.touched:
        wanted = [
            _cf(_fate(("column", op.table, lv.column), later).identity[2]) for lv in op.levels
        ]
        if [_cf(c) for _, c in levels] != wanted:
            check.problem(f"the hierarchy '{name}' does not have the levels asked for")


# --- the model Studio's examples are written for ----------------------------------------------

# A small model with the tables and fields the structure twin's report names, plus the date and
# sales tables of the Contoso-style schema the Agents dashboard is built on. The tool-card examples
# are checked against it (``example_model``).
EXAMPLE_FACTS: dict[str, Any] = {
    "tables": [
        {"name": "FactSales", "kind": "data"},
        {
            "name": "Date",
            "kind": "calculated",
            "expression": "DateAutoTemplate",
            "dataCategory": "Time",
        },
        {"name": "DimStore", "kind": "data"},
        {"name": "DimChannel", "kind": "data"},
        {"name": "DimEmployee", "kind": "data"},
        {"name": "DimPromotion", "kind": "data"},
        {"name": "# Measures", "kind": "data"},
        {
            "name": "Metric",
            "kind": "calculated",
            "fieldParameter": True,
            "expression": (
                "{\n    (\"Avg sales\", NAMEOF('# Measures'[Employees Avg MTD Sales]), 0)\n}"
            ),
        },
        {"name": "Time Calc", "kind": "calculationGroup"},
        # The helper tables the structure twin's slicers and charts use.
        {"name": "Top-Bottom-N", "kind": "calculated", "expression": "GENERATESERIES(1, 30, 1)"},
        {
            "name": "Actuals-vs-PctChange",
            "kind": "calculated",
            "expression": (
                'DATATABLE("Top-Bottom-Field", STRING, "Order", INTEGER, '
                '{{"MOM% Sales", 1}, {"MTD Sales", 2}})'
            ),
        },
        {"name": "Clustered Employees", "kind": "data"},
    ],
    "columns": [
        {"table": "Top-Bottom-N", "name": "Top-Bottom-N", "dataType": 6},
        {"table": "Actuals-vs-PctChange", "name": "Top-Bottom-Field", "dataType": 2},
        {"table": "Actuals-vs-PctChange", "name": "Order", "dataType": 6},
        {"table": "Clustered Employees", "name": "Clustered Employees", "dataType": 2},
        {"table": "Clustered Employees", "name": "Order", "dataType": 6},
    ]
    + [
        {"table": "FactSales", "name": name, "dataType": dtype}
        for name, dtype in (
            ("SalesKey", 6),
            ("DateKey", 9),
            ("channelKey", 6),
            ("StoreKey", 6),
            ("PromotionKey", 6),
            ("SalesAmount", 8),
            ("TotalCost", 8),
            ("DiscountAmount", 8),
            ("SalesQuantity", 6),
        )
    ]
    + [
        {"table": "Date", "name": name, "dataType": dtype}
        for name, dtype in (
            ("Date", 9),
            ("Year", 6),
            ("Quarter", 2),
            ("Month", 2),
            ("Month Number", 6),
        )
    ]
    + [
        {"table": "DimStore", "name": name, "dataType": dtype}
        for name, dtype in (
            ("StoreKey", 6),
            ("StoreName", 2),
            ("StoreType", 2),
            ("Status", 2),
            ("EmployeeKey", 6),
        )
    ]
    + [
        {"table": "DimChannel", "name": name, "dataType": dtype}
        for name, dtype in (("ChannelKey", 6), ("ChannelName", 2))
    ]
    + [
        {"table": "DimEmployee", "name": name, "dataType": dtype}
        for name, dtype in (
            ("EmployeeKey", 6),
            ("FirstName", 2),
            ("LastName", 2),
            ("DepartmentName", 2),
        )
    ]
    + [
        {"table": "DimPromotion", "name": name, "dataType": dtype}
        for name, dtype in (("PromotionKey", 6), ("PromotionName", 2))
    ]
    + [
        {"table": "Metric", "name": "Metric", "dataType": 2},
        {"table": "Metric", "name": "Metric Fields", "dataType": 2},
        {"table": "Metric", "name": "Metric Order", "dataType": 6},
        {"table": "Time Calc", "name": "Name", "dataType": 2},
        {"table": "Time Calc", "name": "Ordinal", "dataType": 6},
    ],
    "relationships": [
        {
            "fromTable": "FactSales",
            "fromColumn": "StoreKey",
            "toTable": "DimStore",
            "toColumn": "StoreKey",
        },
        {
            "fromTable": "FactSales",
            "fromColumn": "channelKey",
            "toTable": "DimChannel",
            "toColumn": "ChannelKey",
        },
        {"fromTable": "FactSales", "fromColumn": "DateKey", "toTable": "Date", "toColumn": "Date"},
    ],
    "hierarchies": [
        {"table": "Date", "name": "Calendar", "levels": ["Year", "Quarter", "Month"]},
    ],
    "roles": [
        {"name": "Regional Managers", "filters": {"DimStore": '[StoreType] = "Store"'}},
        {"name": "Auditors", "filters": {}},
    ],
    "calculationGroups": [
        {
            "table": "Time Calc",
            "precedence": 5,
            "items": [
                {"name": "Current", "expression": "SELECTEDMEASURE()", "ordinal": 0},
                {
                    "name": "YTD",
                    "expression": "CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))",
                    "ordinal": 1,
                },
                {
                    "name": "PY",
                    "expression": "CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))",
                    "ordinal": 2,
                },
            ],
        }
    ],
    "fieldParameters": [{"table": "Metric"}],
    "partitions": [
        {
            "table": "FactSales",
            "sourceType": "M",
            "mode": "import",
            "expression": "let Source = 1 in Source",
        },
        {
            "table": "DimStore",
            "sourceType": "M",
            "mode": "import",
            "expression": "let Source = 2 in Source",
        },
    ],
    "expressions": [
        # M has no escapes in text: the text F:\Data.
        {"name": "Source Folder", "expression": '"F:\\Data"'},
        {
            "name": "Region",
            "expression": (
                '"West" meta [IsParameterQuery=true, List={"West", "East", "North"}, '
                'DefaultValue="West", Type="Text", IsParameterQueryRequired=true]'
            ),
        },
        {
            "name": "RangeStart",
            "expression": (
                "#datetime(2024, 1, 1, 0, 0, 0) meta [IsParameterQuery=true, "
                'Type="DateTime", IsParameterQueryRequired=true]'
            ),
        },
        {
            "name": "Top N",
            "expression": (
                '10 meta [IsParameterQuery=true, Type="Number", IsParameterQueryRequired=false]'
            ),
        },
    ],
    "model": {"discourageImplicitMeasures": True, "culture": "en-US"},
}
EXAMPLE_MEASURES: list[dict[str, Any]] = [
    {
        "Name": "Employees Avg MTD Sales",
        "TableName": "# Measures",
        "Expression": "AVERAGEX(VALUES(DimEmployee[EmployeeKey]), [Table MTD Sales])",
    },
    {
        "Name": "Table MTD Sales",
        "TableName": "# Measures",
        "Expression": "SUM(FactSales[SalesAmount])",
    },
    {"Name": "Rank", "TableName": "# Measures", "Expression": "RANKX(ALL('DimChannel'), 1)"},
]


def example_model() -> ModelInfo:
    """The ModelInfo the tool-card examples are written for, with its structure attached."""
    tables = [
        {"TableName": row["table"], "ColumnName": row["name"], "PandasDataType": ""}
        for row in EXAMPLE_FACTS["columns"]
    ]
    calculated = {
        str(row["name"]): str(row["expression"])
        for row in EXAMPLE_FACTS["tables"]
        if isinstance(row.get("expression"), str)
    }
    model = ModelInfo.from_report(
        tables,
        [dict(m) for m in EXAMPLE_MEASURES],
        {"columns": EXAMPLE_FACTS["columns"], "measures": []},
        calculated=calculated,
    )
    attach_structure(model, EXAMPLE_FACTS)
    return model


# --- tool cards -------------------------------------------------------------------------------


def _slot_column(value: Any) -> tuple[str | None, Any]:
    """(the column's own table or None, its name) from {table, name}, "Table[Column]" or a bare
    name."""
    if isinstance(value, Mapping):
        own = value.get("table")
        return (str(own).strip() if own else None), value.get("name")
    if isinstance(value, str) and value.endswith("]") and "[" in value:
        head, _, rest = value.partition("[")
        own = head.strip()
        if own.startswith("'") and own.endswith("'") and len(own) > 1:
            own = own[1:-1].replace("''", "'")
        return (own or None), rest[:-1]
    return None, value


def _column_slot(
    filled: Mapping[str, Any], table_slot: str = "table", column_slot: str = "column"
) -> tuple[Any, Any]:
    """(table, column) from a column slot that may be {table, name}, "Table[Column]" or a bare name
    with the table in its own slot.

    The table slot and the column's own table must agree: a column of FactSales is never changed
    because the request named DimStore (the live bug "hide the StoreKey column of DimStore in the
    model" hid FactSales[StoreKey]; both tables have StoreKey). A bare name with no table at all
    is refused too: several tables can have it (``column_owner`` names them where the model is
    known)."""
    own, name = _slot_column(filled.get(column_slot))
    table = filled.get(table_slot)
    table = str(table).strip() if table else None
    if name is None:
        return table, None
    if own is not None and table is not None and _cf(own) != _cf(table):
        raise DocumentError(
            f"The column [{name}] you named is in '{own}', but the request says '{table}': the "
            "tables differ, so say which table's column to change"
        )
    home = table or own
    if not home:
        raise DocumentError(f"Say which table's [{name}] column to change")
    return home, name


def column_owner(model: ModelInfo, name: str, table: str | None = None) -> tuple[str, str]:
    """(table, column spelling) of the column ``name``: in ``table`` when one is given, else in
    the one table that has it. Several tables with that column and no table said is refused, and
    the refusal names them ("FactSales and DimStore each have StoreKey; say which table")."""
    if table:
        return _table(model, table), _column(model, table, name)
    owners = [
        home for home, columns in model.columns.items() if any(_cf(c) == _cf(name) for c in columns)
    ]
    if not owners:
        raise DocumentError(f"The model has no column {name!r}")
    if len(owners) > 1:
        raise DocumentError(f"{_and_list(owners)} each have {name}; say which table")
    return owners[0], _column(model, owners[0], name)


def _slot_name(value: Any, table: Any, what: str) -> Any:
    """A column slot that lands in a text field of the operation (a sort-by column, a date
    column): its name, when its own table is the operation's table."""
    own, name = _slot_column(value)
    if own is not None and table and _cf(own) != _cf(str(table)):
        raise DocumentError(
            f"The {what} [{name}] you named is in '{own}', not in '{table}': the tables differ, "
            f"and it must be a column of '{table}'"
        )
    return name


def _builder(
    op: str, *, column: bool = False, rename: str | None = None, extra: tuple[str, ...] = ()
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    def build(filled: Mapping[str, Any]) -> dict[str, Any]:
        operation: dict[str, Any] = {"op": op}
        if column:
            table, name = _column_slot(filled)
            operation["table"], operation["name"] = table, name
        for key in extra:
            if key in filled and filled[key] is not None:
                value = filled[key]
                if key == "sort_by":
                    value = _slot_name(value, operation.get("table"), "sort-by column")
                operation[key] = value
        if rename is not None and rename in filled:
            operation["new_name"] = filled[rename]
        return operation

    return build


def _hierarchy_build(op: str, filled: Mapping[str, Any]) -> dict[str, Any]:
    """A hierarchy card's operation: the planner fills ``levels`` as fields ({table, name},
    "Table[Column]" or a column name) and each becomes a level on that column. Every level must
    be a column of the hierarchy's table."""
    operation: dict[str, Any] = {"op": op}
    for key in ("table", "name", "new_name"):
        if filled.get(key) is not None:
            operation[key] = filled[key]
    levels = filled.get("levels")
    if isinstance(levels, list):
        found: list[dict[str, Any]] = []
        for level in levels:
            if isinstance(level, Mapping) and "column" in level:
                found.append(dict(level))
                continue
            table, column = _column_slot({"table": operation.get("table"), "column": level})
            operation.setdefault("table", table)
            found.append({"column": column})
        operation["levels"] = found
    return operation


def _date_table_build(filled: Mapping[str, Any]) -> dict[str, Any]:
    operation: dict[str, Any] = {"op": "mark_date_table"}
    if filled.get("date_column") is not None:
        table, column = _column_slot(filled, column_slot="date_column")
        operation["table"], operation["column"] = table, column
    elif filled.get("table") is not None:
        operation["table"] = filled["table"]
    return operation


def _relationship_build(op: str) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """A relationship card's operation: each end is a column slot (``from``, ``to``: {table,
    name}, so the column keeps its own table), checked against its table slot when one is given.
    Filled operation fields (``from_column`` as a name) are read too."""

    def build(filled: Mapping[str, Any]) -> dict[str, Any]:
        operation: dict[str, Any] = {"op": op}
        for side in ("from", "to"):
            slot = side if filled.get(side) is not None else f"{side}_column"
            table, column = _column_slot(filled, table_slot=f"{side}_table", column_slot=slot)
            operation[f"{side}_table"], operation[f"{side}_column"] = table, column
        for key in (
            "cardinality",
            "cross_filter",
            "active",
            "assume_referential_integrity",
            "security_filter",
        ):
            if filled.get(key) is not None:
                operation[key] = filled[key]
        if operation.get("cardinality") == "one_to_many":
            # The public typed operation uses many-to-one; preserve the user's two ends.
            for suffix in ("table", "column"):
                operation[f"from_{suffix}"], operation[f"to_{suffix}"] = (
                    operation[f"to_{suffix}"],
                    operation[f"from_{suffix}"],
                )
            operation["cardinality"] = "many_to_one"
        return operation

    return build


# Calculation items the planner can name without writing DAX: the words of a request ("Current,
# YTD and PY") become these formulas over the date column the request names.
_ITEM_PATTERNS: tuple[tuple[str, str, str | None], ...] = (
    (r"current|actual|value|selected", "SELECTEDMEASURE()", None),
    (r"ytd|year to date", "CALCULATE(SELECTEDMEASURE(), DATESYTD({date}))", None),
    (r"qtd|quarter to date", "CALCULATE(SELECTEDMEASURE(), DATESQTD({date}))", None),
    (r"mtd|month to date", "CALCULATE(SELECTEDMEASURE(), DATESMTD({date}))", None),
    (
        r"py|ly|prior year|previous year|last year|same period last year",
        "CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR({date}))",
        None,
    ),
    (
        r"pm|prior month|previous month|last month",
        "CALCULATE(SELECTEDMEASURE(), DATEADD({date}, -1, MONTH))",
        None,
    ),
    (
        r"yoy ?%|yoy percent|year over year ?%|year over year percent",
        "VAR PriorYear = CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR({date}))\n"
        "RETURN DIVIDE(SELECTEDMEASURE() - PriorYear, PriorYear)",
        '"0.0%"',
    ),
    (
        r"yoy|year over year|vs py|change vs prior year",
        "SELECTEDMEASURE() - CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR({date}))",
        None,
    ),
)


def _column_text(value: Any) -> str | None:
    """'Table'[Column] for a column slot value ({table, name} or "Table[Column]"); None for a
    bare name (the table is not guessed)."""
    try:
        table, column = _column_slot({"column": value})
    except DocumentError:
        return None
    if not table or not column:
        return None
    return _column_ref(str(table), str(column))


def calculation_items(names: Any, date: Any = None) -> list[dict[str, Any]]:
    """Items for a list of names ("Current, YTD and PY" or a list): each known name gets its
    formula over ``date``; an unknown name, or a time item without a date column, gets none (the
    operation then fails its check and the planner asks)."""
    if isinstance(names, list):
        found: list[dict[str, Any]] = []
        for given in names:
            if isinstance(given, Mapping) and "name" in given:
                found.append(dict(given))
            elif isinstance(given, str):
                found.extend(calculation_items(given, date))
        return found
    if not isinstance(names, str):
        return []
    date_ref = _column_text(date) if date is not None else None
    items: list[dict[str, Any]] = []
    for raw in re.split(r"\s*(?:,|;|\band\b|&)\s*", names):
        name = raw.strip().strip("'\"")
        if not name:
            continue
        item: dict[str, Any] = {"name": name}
        for pattern, formula, format_string in _ITEM_PATTERNS:
            if re.fullmatch(pattern, name, flags=re.IGNORECASE) is None:
                continue
            if "{date}" not in formula or date_ref is not None:
                item["expression"] = formula.format(date=date_ref)
                if format_string is not None:
                    item["format_string_expression"] = format_string
            break
        items.append(item)
    return items


def _calculation_group_build(filled: Mapping[str, Any]) -> dict[str, Any]:
    operation: dict[str, Any] = {"op": "create_calculation_group"}
    for key in ("name", "precedence", "description"):
        if filled.get(key) is not None:
            operation[key] = filled[key]
    operation["items"] = calculation_items(filled.get("items"), filled.get("date"))
    return operation


def _role_filters_from(filled: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """A role card's filters: a list as given, or the one ``filter`` DAX on ``table`` (the table
    of the filter's first column when ``table`` is not given)."""
    given = filled.get("filters")
    if isinstance(given, list):
        return [dict(f) for f in given if isinstance(f, Mapping)]
    text = filled.get("filter")
    if not isinstance(text, str):
        return None
    table = filled.get("table")
    if not text.strip():  # an empty filter removes the table's filter
        return [{"table": str(table), "expression": ""}] if table else None
    if not table:
        named = [ref.table for ref in dax_references(text) if ref.table]
        table = named[0] if named else None
    if not table:
        return [{"table": "", "expression": text}]
    return [{"table": str(table), "expression": text}]


def _role_build(op: str) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    def build(filled: Mapping[str, Any]) -> dict[str, Any]:
        operation: dict[str, Any] = {"op": op}
        for key in ("name", "new_name", "description"):
            if filled.get(key) is not None:
                operation[key] = filled[key]
        filters = _role_filters_from(filled)
        if filters is not None:
            operation["filters"] = filters
        return operation

    return build


def _card(
    op: str,
    summary: str,
    slots: tuple[Slot, ...],
    cues: tuple[str, ...],
    examples: tuple[tuple[str, Mapping[str, Any]], ...],
    build: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    *,
    capability: str = "model",
    planner: bool = True,
) -> ToolCard:
    return ToolCard(
        op=op,
        family="model",
        summary=summary,
        slots=slots,
        cues=cues,
        examples=examples,
        needs_desktop=True,
        confirm=True,
        build=build,
        capability=capability,
        planner=planner,
    )


def _end_slots(first: str, second: str) -> tuple[Slot, ...]:
    """A relationship's two ends: column slots that keep their own table ({table, name}); the
    table slots are optional and, when filled, must agree with them."""
    return (
        Slot("from", "column", first),
        Slot("from_table", "table", "its table", required=False),
        Slot("to", "column", second),
        Slot("to_table", "table", "its table", required=False),
    )


_TABLE = Slot("table", "table", "the table")
_DAX = Slot("expression", "dax", "the DAX formula")
_COLUMN = Slot("column", "column", "the column (in the table above)")

TOOL_CARDS: tuple[ToolCard, ...] = (
    _card(
        "create_calculated_column",
        "Add a calculated column (a DAX formula computed per row) to a table.",
        (
            _TABLE,
            Slot("name", "text", "the new column's name"),
            _DAX,
            Slot("format_string", "text", "number format, e.g. #,0.00", required=False),
        ),
        ("calculated column", "new column", "add a column", "column formula"),
        (
            (
                "add a Margin column to FactSales that is SalesAmount minus TotalCost",
                {
                    "op": "create_calculated_column",
                    "table": "FactSales",
                    "name": "Margin",
                    "expression": "FactSales[SalesAmount] - FactSales[TotalCost]",
                    "format_string": "#,0.00",
                },
            ),
            (
                "create a column Big Sale in FactSales: true when the sale is over 1000",
                {
                    "op": "create_calculated_column",
                    "table": "FactSales",
                    "name": "Big Sale",
                    "expression": "FactSales[SalesAmount] > 1000",
                },
            ),
            (
                "add a Full Name column to DimEmployee with the first and last name",
                {
                    "op": "create_calculated_column",
                    "table": "DimEmployee",
                    "name": "Full Name",
                    "expression": 'DimEmployee[FirstName] & " " & DimEmployee[LastName]',
                },
            ),
            (
                "add a hidden Year Month column to Date for sorting, as a whole number",
                {
                    "op": "create_calculated_column",
                    "table": "Date",
                    "name": "Year Month",
                    "expression": "'Date'[Year] * 100 + 'Date'[Month Number]",
                    "data_type": "whole_number",
                    "summarize_by": "none",
                    "hidden": True,
                },
            ),
        ),
        capability="calculated_columns",
    ),
    _card(
        "set_column_properties",
        "Change a column's format, sort-by column, data category, summarization, folder, "
        "description, visibility or key.",
        (
            _TABLE,
            _COLUMN,
            Slot("format_string", "text", "number or date format", required=False),
            Slot("sort_by", "column", "the column to sort it by", required=False),
            Slot(
                "data_category",
                "enum",
                "what the values are",
                required=False,
                choices=tuple(DATA_CATEGORIES),
            ),
            Slot(
                "summarize_by",
                "enum",
                "default summarization",
                required=False,
                choices=tuple(SUMMARIZE_BY),
            ),
            Slot("hidden", "bool", "hide it from the Fields list", required=False),
        ),
        (
            "sort by column",
            "data category",
            "summarize by",
            "hide column",
            "column format",
            "web url",
            "image url",
            "don't summarize",
        ),
        (
            (
                "sort the Month column by Month Number",
                {
                    "op": "set_column_properties",
                    "table": "Date",
                    "name": "Month",
                    "sort_by": "Month Number",
                },
            ),
            (
                "don't summarize SalesKey",
                {
                    "op": "set_column_properties",
                    "table": "FactSales",
                    "name": "SalesKey",
                    "summarize_by": "none",
                },
            ),
            (
                "hide DimStore StoreKey",
                {
                    "op": "set_column_properties",
                    "table": "DimStore",
                    "name": "StoreKey",
                    "hidden": True,
                },
            ),
            (
                "show SalesAmount with a thousands separator and two decimals",
                {
                    "op": "set_column_properties",
                    "table": "FactSales",
                    "name": "SalesAmount",
                    "format_string": "#,0.00",
                },
            ),
            (
                "StoreType holds store categories: put it in the Store details folder",
                {
                    "op": "set_column_properties",
                    "table": "DimStore",
                    "name": "StoreType",
                    "folder": "Store details",
                },
            ),
        ),
        build=_builder(
            "set_column_properties",
            column=True,
            extra=(
                "format_string",
                "sort_by",
                "data_category",
                "summarize_by",
                "hidden",
                "folder",
                "description",
                "key",
                "data_type",
                "expression",
            ),
        ),
    ),
    _card(
        "rename_column",
        "Rename a column; visuals, filters and formulas that use it follow.",
        (_TABLE, _COLUMN, Slot("new_name", "text", "the new name")),
        ("rename column", "call the column"),
        (
            (
                "rename StoreName to Store",
                {
                    "op": "rename_column",
                    "table": "DimStore",
                    "name": "StoreName",
                    "new_name": "Store",
                },
            ),
            (
                "call the SalesAmount column Sales Amount",
                {
                    "op": "rename_column",
                    "table": "FactSales",
                    "name": "SalesAmount",
                    "new_name": "Sales Amount",
                },
            ),
            (
                "rename ChannelName in DimChannel to Channel",
                {
                    "op": "rename_column",
                    "table": "DimChannel",
                    "name": "ChannelName",
                    "new_name": "Channel",
                },
            ),
        ),
        build=_builder("rename_column", column=True, rename="new_name"),
    ),
    _card(
        "delete_column",
        "Delete a column no visual, filter, formula, relationship or hierarchy uses.",
        (_TABLE, _COLUMN),
        ("delete column", "remove column"),
        (
            (
                "delete the Status column of DimStore",
                {"op": "delete_column", "table": "DimStore", "name": "Status"},
            ),
            (
                "remove DiscountAmount from FactSales, nobody uses it",
                {"op": "delete_column", "table": "FactSales", "name": "DiscountAmount"},
            ),
            (
                "delete the DepartmentName column",
                {"op": "delete_column", "table": "DimEmployee", "name": "DepartmentName"},
            ),
        ),
        build=_builder("delete_column", column=True),
    ),
    _card(
        "create_calculated_table",
        "Add a table computed by a DAX formula.",
        (Slot("name", "text", "the new table's name"), _DAX),
        ("calculated table", "new table", "table formula"),
        (
            (
                "create a table Years with the distinct years of Date",
                {
                    "op": "create_calculated_table",
                    "name": "Years",
                    "expression": "DISTINCT('Date'[Year])",
                },
            ),
            (
                "add a Calendar table from 2020 to 2025",
                {
                    "op": "create_calculated_table",
                    "name": "Calendar",
                    "expression": "CALENDAR(DATE(2020, 1, 1), DATE(2025, 12, 31))",
                },
            ),
            (
                "make a hidden table of the store types",
                {
                    "op": "create_calculated_table",
                    "name": "Store Types",
                    "expression": "DISTINCT(DimStore[StoreType])",
                    "hidden": True,
                },
            ),
        ),
        capability="calculated_tables",
    ),
    _card(
        "set_table_properties",
        "Hide or show a table, describe it, or change a calculated table's formula.",
        (
            _TABLE,
            Slot("hidden", "bool", "hide it from the Fields list", required=False),
            Slot("description", "text", "description", required=False),
        ),
        ("hide table", "show table", "table description"),
        (
            (
                "hide the DimChannel table",
                {"op": "set_table_properties", "table": "DimChannel", "hidden": True},
            ),
            (
                "show the Top-Bottom-N table in the fields list again",
                {"op": "set_table_properties", "table": "Top-Bottom-N", "hidden": False},
            ),
            (
                "describe FactSales as one row per sale",
                {
                    "op": "set_table_properties",
                    "table": "FactSales",
                    "description": "One row per sale",
                },
            ),
        ),
    ),
    _card(
        "rename_table",
        "Rename a table; visuals, filters and formulas follow.",
        (Slot("name", "table", "the table"), Slot("new_name", "text", "the new name")),
        ("rename table", "call the table"),
        (
            (
                "rename DimStore to Stores",
                {"op": "rename_table", "name": "DimStore", "new_name": "Stores"},
            ),
            (
                "call the FactSales table Sales",
                {"op": "rename_table", "name": "FactSales", "new_name": "Sales"},
            ),
            (
                "rename the DimEmployee table to Employees",
                {"op": "rename_table", "name": "DimEmployee", "new_name": "Employees"},
            ),
        ),
    ),
    _card(
        "delete_table",
        "Delete a table, calculated table, field parameter or calculation group that nothing uses.",
        (Slot("name", "table", "the table"),),
        ("delete table", "remove table", "delete field parameter", "delete calculation group"),
        (
            ("delete the DimPromotion table", {"op": "delete_table", "name": "DimPromotion"}),
            ("remove the Metric field parameter", {"op": "delete_table", "name": "Metric"}),
            ("delete the Time Calc calculation group", {"op": "delete_table", "name": "Time Calc"}),
        ),
    ),
    _card(
        "mark_date_table",
        "Mark a table as the date table (on its date column), or unmark it.",
        (_TABLE, Slot("date_column", "column", "the date column (in that table)", required=False)),
        ("mark as date table", "date table"),
        (
            (
                "mark Date as the date table on Date",
                {"op": "mark_date_table", "table": "Date", "column": "Date"},
            ),
            (
                "use the Date table as the model's calendar",
                {"op": "mark_date_table", "table": "Date", "column": "Date"},
            ),
            ("Date should no longer be the date table", {"op": "mark_date_table", "table": "Date"}),
        ),
        build=_date_table_build,
    ),
    _card(
        "create_relationship",
        "Relate two tables on their requested columns and cardinality.",
        (
            *_end_slots("the requested from column", "the requested to column"),
            Slot(
                "cardinality",
                "enum",
                "cardinality",
                required=False,
                choices=("many_to_one", "one_to_many", "one_to_one", "many_to_many"),
            ),
            Slot(
                "cross_filter",
                "enum",
                "filter direction",
                required=False,
                choices=("single", "both"),
            ),
            Slot("active", "bool", "active", required=False),
            Slot(
                "assume_referential_integrity",
                "bool",
                "assume referential integrity (enable only after Power BI verification)",
                required=False,
            ),
            Slot(
                "security_filter",
                "enum",
                "security filter direction",
                required=False,
                choices=("single", "both"),
            ),
        ),
        ("relationship", "relate", "join", "connect tables"),
        (
            (
                "relate DimStore to DimEmployee on EmployeeKey",
                {
                    "op": "create_relationship",
                    "from_table": "DimStore",
                    "from_column": "EmployeeKey",
                    "to_table": "DimEmployee",
                    "to_column": "EmployeeKey",
                },
            ),
            (
                "add an inactive relationship from FactSales to DimPromotion on PromotionKey",
                {
                    "op": "create_relationship",
                    "from_table": "FactSales",
                    "from_column": "PromotionKey",
                    "to_table": "DimPromotion",
                    "to_column": "PromotionKey",
                    "active": False,
                },
            ),
            (
                "connect FactSales to DimPromotion on PromotionKey, filtering both ways",
                {
                    "op": "create_relationship",
                    "from_table": "FactSales",
                    "from_column": "PromotionKey",
                    "to_table": "DimPromotion",
                    "to_column": "PromotionKey",
                    "cross_filter": "both",
                },
            ),
        ),
        build=_relationship_build("create_relationship"),
        capability="relationships",
    ),
    _card(
        "update_relationship",
        "Change a relationship's direction, activity, cardinality or referential integrity.",
        (
            *_end_slots("one column", "the other column"),
            Slot(
                "cardinality",
                "enum",
                "cardinality relative to the requested from/to ends",
                required=False,
                choices=("many_to_one", "one_to_many", "one_to_one", "many_to_many"),
            ),
            Slot(
                "cross_filter",
                "enum",
                "filter direction",
                required=False,
                choices=("single", "both"),
            ),
            Slot("active", "bool", "active", required=False),
            Slot(
                "assume_referential_integrity",
                "bool",
                "assume referential integrity (enable only after Power BI verification)",
                required=False,
            ),
            Slot(
                "security_filter",
                "enum",
                "security filter direction",
                required=False,
                choices=("single", "both"),
            ),
        ),
        (
            "both directions",
            "cross filter",
            "make active",
            "deactivate relationship",
            "bidirectional",
        ),
        (
            (
                "make the store relationship filter both ways",
                {
                    "op": "update_relationship",
                    "from_table": "FactSales",
                    "from_column": "StoreKey",
                    "to_table": "DimStore",
                    "to_column": "StoreKey",
                    "cross_filter": "both",
                },
            ),
            (
                "deactivate the relationship between FactSales and Date",
                {
                    "op": "update_relationship",
                    "from_table": "FactSales",
                    "from_column": "DateKey",
                    "to_table": "Date",
                    "to_column": "Date",
                    "active": False,
                },
            ),
            (
                "let the channel relationship filter both ways and apply security both ways too",
                {
                    "op": "update_relationship",
                    "from_table": "FactSales",
                    "from_column": "channelKey",
                    "to_table": "DimChannel",
                    "to_column": "ChannelKey",
                    "cross_filter": "both",
                    "security_filter": "both",
                },
            ),
        ),
        build=_relationship_build("update_relationship"),
        capability="relationships",
    ),
    _card(
        "delete_relationship",
        "Delete a relationship no USERELATIONSHIP or CROSSFILTER uses.",
        _end_slots("one column", "the other column"),
        ("delete relationship", "remove relationship"),
        (
            (
                "remove the relationship between FactSales and DimChannel",
                {
                    "op": "delete_relationship",
                    "from_table": "FactSales",
                    "from_column": "channelKey",
                    "to_table": "DimChannel",
                    "to_column": "ChannelKey",
                },
            ),
            (
                "delete the link from FactSales to DimStore",
                {
                    "op": "delete_relationship",
                    "from_table": "FactSales",
                    "from_column": "StoreKey",
                    "to_table": "DimStore",
                    "to_column": "StoreKey",
                },
            ),
            (
                "drop the relationship between FactSales DateKey and Date",
                {
                    "op": "delete_relationship",
                    "from_table": "FactSales",
                    "from_column": "DateKey",
                    "to_table": "Date",
                    "to_column": "Date",
                },
            ),
        ),
        build=_relationship_build("delete_relationship"),
        capability="relationships",
    ),
    _card(
        "create_hierarchy",
        "Create a hierarchy of columns in one table (top level first).",
        (
            _TABLE,
            Slot("name", "text", "the hierarchy's name"),
            Slot("levels", "fields", "the columns, top level first"),
        ),
        ("hierarchy", "drill levels"),
        (
            (
                "create a Date Hierarchy on Date: Year, Quarter, Month",
                {
                    "op": "create_hierarchy",
                    "table": "Date",
                    "name": "Date Hierarchy",
                    "levels": [{"column": "Year"}, {"column": "Quarter"}, {"column": "Month"}],
                },
            ),
            (
                "add a Store hierarchy to DimStore with StoreType then StoreName",
                {
                    "op": "create_hierarchy",
                    "table": "DimStore",
                    "name": "Store",
                    "levels": [
                        {"column": "StoreType", "name": "Type"},
                        {"column": "StoreName", "name": "Store"},
                    ],
                },
            ),
            (
                "make a drill path Department > Employee in DimEmployee",
                {
                    "op": "create_hierarchy",
                    "table": "DimEmployee",
                    "name": "Department",
                    "levels": [{"column": "DepartmentName"}, {"column": "FirstName"}],
                },
            ),
        ),
        build=lambda filled: _hierarchy_build("create_hierarchy", filled),
    ),
    _card(
        "update_hierarchy",
        "Change a hierarchy's levels (the whole new list) or rename it.",
        (
            _TABLE,
            Slot("name", "text", "the hierarchy"),
            Slot("levels", "fields", "the new levels", required=False),
            Slot("new_name", "text", "new name", required=False),
        ),
        ("hierarchy levels", "add level", "remove level", "reorder levels"),
        (
            (
                "in the Date table, make the Calendar hierarchy Year then Month",
                {
                    "op": "update_hierarchy",
                    "table": "Date",
                    "name": "Calendar",
                    "levels": [{"column": "Year"}, {"column": "Month"}],
                },
            ),
            (
                "add Month Number under Month in the Calendar hierarchy",
                {
                    "op": "update_hierarchy",
                    "table": "Date",
                    "name": "Calendar",
                    "levels": [
                        {"column": "Year"},
                        {"column": "Quarter"},
                        {"column": "Month"},
                        {"column": "Month Number"},
                    ],
                },
            ),
            (
                "rename the Calendar hierarchy to Fiscal Calendar",
                {
                    "op": "update_hierarchy",
                    "table": "Date",
                    "name": "Calendar",
                    "new_name": "Fiscal Calendar",
                },
            ),
        ),
        build=lambda filled: _hierarchy_build("update_hierarchy", filled),
    ),
    _card(
        "delete_hierarchy",
        "Delete a hierarchy no visual uses.",
        (_TABLE, Slot("name", "text", "the hierarchy")),
        ("delete hierarchy", "remove hierarchy"),
        (
            (
                "delete the Calendar hierarchy",
                {"op": "delete_hierarchy", "table": "Date", "name": "Calendar"},
            ),
            (
                "remove the Calendar drill path from Date",
                {"op": "delete_hierarchy", "table": "Date", "name": "Calendar"},
            ),
            (
                "we don't need the Date table's Calendar hierarchy any more",
                {"op": "delete_hierarchy", "table": "Date", "name": "Calendar"},
            ),
        ),
    ),
    _card(
        "create_field_parameter",
        "Create a field parameter: a slicer that switches the fields a visual shows.",
        (
            Slot("name", "text", "the parameter's name"),
            Slot("fields", "fields", "the measures or columns to switch between"),
        ),
        ("field parameter", "switch measures", "measure selector"),
        (
            (
                "create a field parameter Sales Metric with Employees Avg MTD Sales and Table "
                "MTD Sales",
                {
                    "op": "create_field_parameter",
                    "name": "Sales Metric",
                    "fields": [
                        {
                            "table": "# Measures",
                            "name": "Employees Avg MTD Sales",
                            "label": "Avg sales",
                        },
                        {"table": "# Measures", "name": "Table MTD Sales", "label": "MTD sales"},
                    ],
                },
            ),
            (
                "let people switch the axis between store, channel and employee",
                {
                    "op": "create_field_parameter",
                    "name": "Breakdown",
                    "fields": [
                        {"table": "DimStore", "name": "StoreName", "label": "Store"},
                        {"table": "DimChannel", "name": "ChannelName", "label": "Channel"},
                        {"table": "DimEmployee", "name": "FirstName", "label": "Employee"},
                    ],
                },
            ),
            (
                "add a measure selector with Rank and Table MTD Sales",
                {
                    "op": "create_field_parameter",
                    "name": "Measure Selector",
                    "fields": [
                        {"table": "# Measures", "name": "Rank"},
                        {"table": "# Measures", "name": "Table MTD Sales"},
                    ],
                },
            ),
        ),
        capability="field_parameters",
    ),
    _card(
        "update_field_parameter",
        "Change the list of fields a field parameter switches between.",
        (Slot("name", "table", "the field parameter"), Slot("fields", "fields", "the new list")),
        ("field parameter", "add to field parameter"),
        (
            (
                "put Rank into the Metric parameter too",
                {
                    "op": "update_field_parameter",
                    "name": "Metric",
                    "fields": [
                        {"table": "# Measures", "name": "Employees Avg MTD Sales"},
                        {"table": "# Measures", "name": "Rank"},
                    ],
                },
            ),
            (
                "the Metric parameter should switch between Table MTD Sales and Avg sales",
                {
                    "op": "update_field_parameter",
                    "name": "Metric",
                    "fields": [
                        {"table": "# Measures", "name": "Table MTD Sales"},
                        {
                            "table": "# Measures",
                            "name": "Employees Avg MTD Sales",
                            "label": "Avg sales",
                        },
                    ],
                },
            ),
            (
                "show only Table MTD Sales in the Metric parameter",
                {
                    "op": "update_field_parameter",
                    "name": "Metric",
                    "fields": [{"table": "# Measures", "name": "Table MTD Sales"}],
                },
            ),
        ),
        capability="field_parameters",
    ),
    _card(
        "create_calculation_group",
        "Create a calculation group with items (DAX using SELECTEDMEASURE()).",
        (
            Slot("name", "text", "the group's name"),
            Slot(
                "items",
                "text",
                "the item names, e.g. Current, YTD, PY, MTD, YoY %",
            ),
            Slot("date", "column", "the date column time items use", required=False),
            Slot("precedence", "integer", "precedence", required=False, minimum=0),
        ),
        ("calculation group", "time intelligence items", "selectedmeasure"),
        (
            (
                "create a calculation group Time Intelligence with Current and YTD",
                {
                    "op": "create_calculation_group",
                    "name": "Time Intelligence",
                    "items": [
                        {"name": "Current", "expression": "SELECTEDMEASURE()"},
                        {
                            "name": "YTD",
                            "expression": "CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))",
                        },
                    ],
                    "precedence": 10,
                },
            ),
            (
                "add a calculation group Period with MTD and PY over Date[Date]",
                {
                    "op": "create_calculation_group",
                    "name": "Period",
                    "items": [
                        {
                            "name": "MTD",
                            "expression": "CALCULATE(SELECTEDMEASURE(), DATESMTD('Date'[Date]))",
                        },
                        {
                            "name": "PY",
                            "expression": (
                                "CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))"
                            ),
                        },
                    ],
                    "precedence": 20,
                },
            ),
            (
                "create a Comparison calculation group with Current and YoY %",
                {
                    "op": "create_calculation_group",
                    "name": "Comparison",
                    "items": [
                        {"name": "Current", "expression": "SELECTEDMEASURE()"},
                        {
                            "name": "YoY %",
                            "expression": (
                                "VAR PriorYear = CALCULATE(SELECTEDMEASURE(), "
                                "SAMEPERIODLASTYEAR('Date'[Date]))\n"
                                "RETURN DIVIDE(SELECTEDMEASURE() - PriorYear, PriorYear)"
                            ),
                            "format_string_expression": '"0.0%"',
                        },
                    ],
                    "precedence": 30,
                },
            ),
        ),
        build=_calculation_group_build,
        capability="calculation_groups",
    ),
    _card(
        "set_calculation_item",
        "Add a calculation item to a group, or change its formula, format string, order or name.",
        (
            Slot("group", "table", "the calculation group"),
            Slot("name", "text", "the item"),
            Slot("expression", "dax", "the DAX", required=False),
        ),
        ("calculation item", "add item"),
        (
            (
                "add PY to Time Calc",
                {
                    "op": "set_calculation_item",
                    "group": "Time Calc",
                    "name": "PY",
                    "expression": "CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))",
                },
            ),
            (
                "add an MTD item to Time Calc",
                {
                    "op": "set_calculation_item",
                    "group": "Time Calc",
                    "name": "MTD",
                    "expression": "CALCULATE(SELECTEDMEASURE(), DATESMTD('Date'[Date]))",
                },
            ),
            (
                "show the YTD item of Time Calc with one decimal",
                {
                    "op": "set_calculation_item",
                    "group": "Time Calc",
                    "name": "YTD",
                    "format_string_expression": '"#,0.0"',
                },
            ),
        ),
        capability="calculation_groups",
    ),
    _card(
        "delete_calculation_item",
        "Delete one item of a calculation group.",
        (Slot("group", "table", "the calculation group"), Slot("name", "text", "the item")),
        ("delete calculation item", "remove item"),
        (
            (
                "remove the PY item from Time Calc",
                {"op": "delete_calculation_item", "group": "Time Calc", "name": "PY"},
            ),
            (
                "delete YTD from the Time Calc calculation group",
                {"op": "delete_calculation_item", "group": "Time Calc", "name": "YTD"},
            ),
            (
                "Time Calc does not need the Current item",
                {"op": "delete_calculation_item", "group": "Time Calc", "name": "Current"},
            ),
        ),
        capability="calculation_groups",
    ),
    _card(
        "update_calculation_group",
        "Change a calculation group's precedence or description.",
        (
            Slot("name", "table", "the calculation group"),
            Slot("precedence", "integer", "precedence", required=False, minimum=0),
        ),
        ("precedence",),
        (
            (
                "set the precedence of Time Calc to 20",
                {"op": "update_calculation_group", "name": "Time Calc", "precedence": 20},
            ),
            (
                "apply Time Calc first: precedence 1",
                {"op": "update_calculation_group", "name": "Time Calc", "precedence": 1},
            ),
            (
                "describe Time Calc as the report's time comparisons",
                {
                    "op": "update_calculation_group",
                    "name": "Time Calc",
                    "description": "The report's time comparisons",
                },
            ),
        ),
        capability="calculation_groups",
    ),
    _card(
        "create_role",
        "Create a row-level security role with a DAX filter on a table.",
        (
            Slot("name", "text", "the role's name"),
            Slot("table", "table", "the table the role filters", required=False),
            Slot("filter", "dax", 'the row filter, e.g. [StoreType] = "Store"', required=False),
        ),
        ("row level security", "rls", "security role", "role"),
        (
            (
                "create a role Store Managers that only sees stores of type Store",
                {
                    "op": "create_role",
                    "name": "Store Managers",
                    "filters": [{"table": "DimStore", "expression": '[StoreType] = "Store"'}],
                },
            ),
            (
                "add a role Online Team that sees only the Online channel",
                {
                    "op": "create_role",
                    "name": "Online Team",
                    "filters": [{"table": "DimChannel", "expression": '[ChannelName] = "Online"'}],
                },
            ),
            (
                "create a role Big Sales that sees only sales over 1000",
                {
                    "op": "create_role",
                    "name": "Big Sales",
                    "filters": [{"table": "FactSales", "expression": "[SalesAmount] > 1000"}],
                },
            ),
        ),
        build=_role_build("create_role"),
        capability="rls",
    ),
    _card(
        "update_role",
        "Change a role's table filters, rename it or describe it (members are managed in the "
        "Power BI service).",
        (
            Slot("name", "text", "the role"),
            Slot("table", "table", "the table whose filter changes", required=False),
            Slot("filter", "dax", "the new row filter (empty removes it)", required=False),
            Slot("new_name", "text", "new name", required=False),
        ),
        ("role filter", "rename role", "rls filter"),
        (
            (
                "rename the role Regional Managers to Region Leads",
                {"op": "update_role", "name": "Regional Managers", "new_name": "Region Leads"},
            ),
            (
                "let the Auditors role see only online sales",
                {
                    "op": "update_role",
                    "name": "Auditors",
                    "filters": [{"table": "DimChannel", "expression": '[ChannelName] = "Online"'}],
                },
            ),
            (
                "Regional Managers should see every store type again",
                {
                    "op": "update_role",
                    "name": "Regional Managers",
                    "filters": [{"table": "DimStore", "expression": ""}],
                },
            ),
        ),
        build=_role_build("update_role"),
        capability="rls",
    ),
    _card(
        "delete_role",
        "Delete a row-level security role.",
        (Slot("name", "text", "the role"),),
        ("delete role", "remove role"),
        (
            ("delete the Auditors role", {"op": "delete_role", "name": "Auditors"}),
            (
                "remove the Regional Managers role",
                {"op": "delete_role", "name": "Regional Managers"},
            ),
            (
                "we no longer need the Auditors security role",
                {"op": "delete_role", "name": "Auditors"},
            ),
        ),
        capability="rls",
    ),
    _card(
        "set_table_query",
        "Replace a table's Power Query (M) query and reload its data.",
        (
            _TABLE,
            Slot("expression", "m", "the M query"),
            Slot(
                "if_refresh_fails",
                "enum",
                "if the source cannot be reached",
                required=False,
                choices=("ask", "keep", "undo"),
            ),
        ),
        ("power query", "m query", "change the source", "query of the table"),
        (
            (
                'change the FactSales query to: let Source = Csv.Document(File.Contents("F:\\Data'
                '\\FactSales.csv")), Kept = Table.SelectRows(Source, each [Column2] = "2019") in '
                "Kept",
                {
                    "op": "set_table_query",
                    "table": "FactSales",
                    "expression": (
                        'let Source = Csv.Document(File.Contents("F:\\Data\\FactSales.csv")), '
                        'Kept = Table.SelectRows(Source, each [Column2] = "2019") in Kept'
                    ),
                },
            ),
            (
                'set the DimStore query to let Source = Excel.Workbook(File.Contents("F:\\Data'
                '\\Stores.xlsx")), Stores = Source{[Name="Stores"]}[Data] in Stores and keep it '
                "if the file cannot be reached",
                {
                    "op": "set_table_query",
                    "table": "DimStore",
                    "expression": (
                        'let Source = Excel.Workbook(File.Contents("F:\\Data\\Stores.xlsx")), '
                        'Stores = Source{[Name="Stores"]}[Data] in Stores'
                    ),
                    "if_refresh_fails": "keep",
                },
            ),
            (
                'replace the FactSales query with: let Source = Sql.Database("sales-db", '
                '"Contoso"), Sales = Source{[Schema="dbo", Item="FactSales"]}[Data] in Sales; undo '
                "it if the refresh fails",
                {
                    "op": "set_table_query",
                    "table": "FactSales",
                    "expression": (
                        'let Source = Sql.Database("sales-db", "Contoso"), '
                        'Sales = Source{[Schema="dbo", Item="FactSales"]}[Data] in Sales'
                    ),
                    "if_refresh_fails": "undo",
                },
            ),
        ),
        # The local model never writes a table's query: only M the person typed is used.
        capability="power_query",
        planner=False,
    ),
    _card(
        "set_named_expression",
        "Create or change a Power Query query that is not loaded as a table.",
        (Slot("name", "text", "the query's name"), Slot("expression", "m", "the M")),
        ("shared query", "helper query", "named expression"),
        (
            (
                'create a query Source Folder that returns the text "F:\\Data"',
                {
                    "op": "set_named_expression",
                    "name": "Source Folder",
                    # M has no escapes in text: this is the text F:\Data.
                    "expression": '"F:\\Data"',
                },
            ),
            (
                'add a query Server Name returning "sales-db"',
                {"op": "set_named_expression", "name": "Server Name", "expression": '"sales-db"'},
            ),
            (
                "make a helper query Years with the list 2023, 2024 and 2025",
                {"op": "set_named_expression", "name": "Years", "expression": "{2023, 2024, 2025}"},
            ),
        ),
        capability="power_query",
        planner=False,
    ),
    _card(
        "delete_named_expression",
        "Delete a Power Query query or parameter nothing uses.",
        (Slot("name", "text", "the query"),),
        ("delete query", "delete parameter"),
        (
            (
                "delete the Source Folder query",
                {"op": "delete_named_expression", "name": "Source Folder"},
            ),
            ("remove the Region parameter", {"op": "delete_named_expression", "name": "Region"}),
            (
                "we don't use the Top N parameter any more, delete it",
                {"op": "delete_named_expression", "name": "Top N"},
            ),
        ),
        capability="power_query",
    ),
    _card(
        "set_query_parameter",
        "Create a Power Query parameter or change its value (an existing one keeps its type).",
        (
            Slot("name", "text", "the parameter"),
            Slot("value", "text", "its value (dates as 2024-01-31)"),
            Slot(
                "type",
                "enum",
                "its type, for a new parameter",
                required=False,
                choices=("text", "number", "true_false", "date", "date_time", "time"),
            ),
        ),
        ("parameter", "query parameter", "set parameter"),
        (
            (
                "set the parameter Region to East",
                {"op": "set_query_parameter", "name": "Region", "value": "East"},
            ),
            (
                "move RangeStart to 2025-01-01",
                {"op": "set_query_parameter", "name": "RangeStart", "value": "2025-01-01"},
            ),
            (
                "add a number parameter Threshold set to 1000",
                {
                    "op": "set_query_parameter",
                    "name": "Threshold",
                    "value": 1000,
                    "type": "number",
                },
            ),
        ),
        capability="power_query",
    ),
    _card(
        "refresh_data",
        "Reload one table's data, or every table's, from the data sources.",
        (Slot("table", "table", "the table (all when empty)", required=False),),
        ("refresh", "reload data", "update the data"),
        (
            ("refresh FactSales", {"op": "refresh_data", "table": "FactSales"}),
            ("refresh everything", {"op": "refresh_data"}),
            (
                "reload the DimStore data from its source",
                {"op": "refresh_data", "table": "DimStore"},
            ),
        ),
        capability="power_query",
    ),
    _card(
        "set_model_options",
        "Discourage implicit measures (Auto date/time and the model's language are changed in "
        "Power BI Desktop's options).",
        (
            Slot(
                "discourage_implicit_measures",
                "bool",
                "discourage implicit measures",
                required=False,
            ),
            Slot("culture", "text", "culture such as en-US", required=False),
            Slot("auto_date_time", "bool", "auto date/time", required=False),
        ),
        ("implicit measures", "culture", "auto date time", "model settings"),
        (
            (
                "discourage implicit measures",
                {"op": "set_model_options", "discourage_implicit_measures": True},
            ),
            (
                "make visuals use measures only, not summed columns",
                {"op": "set_model_options", "discourage_implicit_measures": True},
            ),
            (
                "turn on discourage implicit measures in the model",
                {"op": "set_model_options", "discourage_implicit_measures": True},
            ),
        ),
    ),
)
