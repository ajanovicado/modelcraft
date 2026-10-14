"""An in-memory stand-in for the part of Microsoft's Tabular Object Model that Studio's runner
(``scripts/studio_tom_runner.py``) uses, and a server around it.

It keeps what the runner's guarantees depend on real:
- collections with ``Find``/``Add``/``Remove`` and objects that know their parent;
- object references (a relationship's columns, a level's column, a sort-by column, a role's
  table) that serialize as names and resolve again when a restored object is attached, as the
  real library does (checked offline against the pinned DLLs);
- ``SaveChanges`` that refuses a model whose references point at removed objects, runs the
  data refreshes that were requested (a table whose query mentions ``UNREACHABLE`` fails), and
  ``UndoLocalChanges`` that goes back to the last saved state;
- the engine's schema views and ``EVALUATE`` answered from the model: a formula that names a
  table, column or measure the model does not have is in error (SemanticError, state 5) and a
  query over one fails, which is how a rename without its fix-ups shows up in Desktop.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from ai_powerbi_analyst.studio.desktop_model import RunnerAnswer
from ai_powerbi_analyst.studio.report_refs import dax_references

RUNNER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "studio_tom_runner.py"


def load_runner() -> ModuleType:
    """The real runner script as a module (it imports nothing of Studio's)."""
    name = "studio_tom_runner_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _enum(*names: str) -> SimpleNamespace:
    space = SimpleNamespace()
    for name in names:
        setattr(space, name, name)
    return space


# --- collections and objects ---------------------------------------------------------------------


class Coll:
    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.items: list[Any] = []

    def __iter__(self) -> Iterator[Any]:
        return iter(list(self.items))

    def __len__(self) -> int:
        return len(self.items)

    @property
    def Count(self) -> int:  # noqa: N802 - TOM's name
        return len(self.items)

    def Find(self, name: str) -> Any:  # noqa: N802 - TOM's Find ignores letter case
        wanted = str(name).casefold()
        return next((i for i in self.items if str(i.Name).casefold() == wanted), None)

    def Add(self, item: Any) -> None:  # noqa: N802
        if getattr(item, "Name", None) and self.Find(item.Name) is not None:
            raise RuntimeError(f"An object named '{item.Name}' already exists")
        item.parent = self.owner
        self.items.append(item)
        _resolve(item)

    def Remove(self, item: Any) -> None:  # noqa: N802
        self.items.remove(item)
        item.parent = None

    def Clear(self) -> None:  # noqa: N802
        for item in list(self.items):
            self.Remove(item)


def _model_of(obj: Any) -> Any:
    while obj is not None and not isinstance(obj, Model):
        obj = getattr(obj, "parent", None)
    return obj


def _table_of(obj: Any) -> Any:
    while obj is not None and not isinstance(obj, Table):
        obj = getattr(obj, "parent", None)
    return obj


def _resolve(obj: Any) -> None:
    """Point the names a restored object carries at the live objects of its tree."""
    pending = getattr(obj, "_pending", None)
    model = _model_of(obj)
    if pending and model is not None:
        for attr, (table, column) in list(pending.items()):
            found_table = model.Tables.Find(table) if table else _table_of(obj)
            target = None
            if found_table is not None:
                target = found_table if column is None else found_table.Columns.Find(column)
            if target is not None:
                setattr(obj, attr, target)
                del pending[attr]
    for child in _children(obj):
        _resolve(child)


def _children(obj: Any) -> list[Any]:
    found: list[Any] = []
    for attr in ("Columns", "Measures", "Hierarchies", "Partitions", "Levels", "TablePermissions"):
        coll = getattr(obj, attr, None)
        if isinstance(coll, Coll):
            found.extend(coll)
    group = getattr(obj, "CalculationGroup", None)
    if group is not None:
        found.extend(group.CalculationItems)
    details = getattr(obj, "RelatedColumnDetails", None)
    if details is not None:
        found.extend(details.GroupByColumns)
    return found


class Obj:
    def __init__(self) -> None:
        self.Name = ""
        self.Description: str | None = None
        self.parent: Any = None
        self.Annotations = Coll(self)
        self.ExtendedProperties = Coll(self)
        self._pending: dict[str, tuple[str | None, str | None]] = {}


class Annotation(Obj):
    def __init__(self) -> None:
        super().__init__()
        self.Value: str | None = None


class JsonExtendedProperty(Annotation):
    pass


class FormatStringDefinition:
    def __init__(self) -> None:
        self.Expression = ""


class DetailRowsDefinition(FormatStringDefinition):
    pass


KPI_FIELDS = (
    ("targetExpression", "TargetExpression"),
    ("targetFormatString", "TargetFormatString"),
    ("statusExpression", "StatusExpression"),
    ("statusGraphic", "StatusGraphic"),
    ("trendExpression", "TrendExpression"),
    ("trendGraphic", "TrendGraphic"),
)


class KPI:
    def __init__(self) -> None:
        self.TargetExpression: str | None = None
        self.TargetFormatString: str | None = None
        self.StatusExpression: str | None = None
        self.StatusGraphic: str | None = None
        self.TrendExpression: str | None = None
        self.TrendGraphic: str | None = None


class Measure(Obj):
    def __init__(self) -> None:
        super().__init__()
        self.Expression = ""
        self.FormatString: str | None = None
        self.DisplayFolder: str | None = None
        self.IsHidden = False
        self.FormatStringDefinition: FormatStringDefinition | None = None
        self.DetailRowsDefinition: DetailRowsDefinition | None = None
        self.KPI: KPI | None = None


class Column(Obj):
    Type = "Data"

    def __init__(self) -> None:
        super().__init__()
        self.DataType = "String"
        self.FormatString: str | None = None
        self.SortByColumn: Any = None
        self.DataCategory: str | None = None
        self.SummarizeBy = "Default"
        self.DisplayFolder: str | None = None
        self.IsHidden = False
        self.IsKey = False
        self.RelatedColumnDetails: RelatedColumnDetails | None = None

    @property
    def Table(self) -> Any:  # noqa: N802
        return self.parent


class DataColumn(Column):
    def __init__(self) -> None:
        super().__init__()
        self.SourceColumn = ""


class CalculatedColumn(Column):
    Type = "Calculated"

    def __init__(self) -> None:
        super().__init__()
        self.Expression = ""
        self.IsDataTypeInferred = True


class CalculatedTableColumn(Column):
    Type = "CalculatedTableColumn"

    def __init__(self) -> None:
        super().__init__()
        self.SourceColumn = ""
        self.IsNameInferred = True
        self.IsDataTypeInferred = True


class GroupByColumn:
    def __init__(self) -> None:
        self.GroupingColumn: Any = None
        self.parent: Any = None
        self._pending: dict[str, tuple[str | None, str | None]] = {}


class RelatedColumnDetails:
    def __init__(self) -> None:
        self.GroupByColumns = Coll(self)
        self.parent: Any = None


class MPartitionSource:
    def __init__(self) -> None:
        self.Expression = ""


class CalculatedPartitionSource(MPartitionSource):
    pass


class CalculationGroupSource:
    pass


class Partition(Obj):
    def __init__(self) -> None:
        super().__init__()
        self.Source: Any = None
        self.Mode = "Import"

    @property
    def SourceType(self) -> str:  # noqa: N802
        if isinstance(self.Source, CalculatedPartitionSource):
            return "Calculated"
        if isinstance(self.Source, MPartitionSource):
            return "M"
        if isinstance(self.Source, CalculationGroupSource):
            return "CalculationGroup"
        return "None"


class Level(Obj):
    def __init__(self) -> None:
        super().__init__()
        self.Ordinal = 0
        self.Column: Any = None


class Hierarchy(Obj):
    def __init__(self) -> None:
        super().__init__()
        self.IsHidden = False
        self.DisplayFolder: str | None = None
        self.Levels = Coll(self)


class CalculationItem(Obj):
    def __init__(self) -> None:
        super().__init__()
        self.Expression = ""
        self.Ordinal = 0
        self.FormatStringDefinition: FormatStringDefinition | None = None


class CalculationGroup:
    def __init__(self) -> None:
        self.Precedence = 0
        self.Description: str | None = None
        self.CalculationItems = Coll(self)
        self.parent: Any = None


class Table(Obj):
    def __init__(self) -> None:
        super().__init__()
        self.IsHidden = False
        self.DataCategory: str | None = None
        self.Columns = Coll(self)
        self.Measures = Coll(self)
        self.Hierarchies = Coll(self)
        self.Partitions = Coll(self)
        self._group: CalculationGroup | None = None

    @property
    def CalculationGroup(self) -> CalculationGroup | None:  # noqa: N802
        return self._group

    @CalculationGroup.setter
    def CalculationGroup(self, group: CalculationGroup | None) -> None:  # noqa: N802
        self._group = group
        if group is not None:
            group.parent = self

    def RequestRefresh(self, kind: str) -> None:  # noqa: N802
        model = _model_of(self)
        if model is not None:
            model.pending.append((self.Name, kind))


class SingleColumnRelationship(Obj):
    def __init__(self) -> None:
        super().__init__()
        self.FromColumn: Any = None
        self.ToColumn: Any = None
        self.FromCardinality = "Many"
        self.ToCardinality = "One"
        self.CrossFilteringBehavior = "OneDirection"
        self.SecurityFilteringBehavior = "OneDirection"
        self.IsActive = True
        self.RelyOnReferentialIntegrity = False

    @property
    def FromTable(self) -> Any:  # noqa: N802
        return self.FromColumn.parent if self.FromColumn is not None else None

    @property
    def ToTable(self) -> Any:  # noqa: N802
        return self.ToColumn.parent if self.ToColumn is not None else None


class TablePermission(Obj):
    def __init__(self) -> None:
        super().__init__()
        self.Table: Any = None
        self.FilterExpression = ""

    @property
    def Name(self) -> str:  # noqa: N802 - a permission is found by its table's name
        return str(self.Table.Name) if self.Table is not None else ""

    @Name.setter
    def Name(self, value: str) -> None:  # noqa: N802
        pass


class ModelRoleMember(Obj):
    @property
    def MemberName(self) -> str:  # noqa: N802
        return str(self.Name)


class ModelRole(Obj):
    def __init__(self) -> None:
        super().__init__()
        self.ModelPermission = "Read"
        self.TablePermissions = Coll(self)
        self.Members = Coll(self)


class NamedExpression(Obj):
    def __init__(self) -> None:
        super().__init__()
        self.Kind = "M"
        self.Expression = ""


# --- serialization (the runner's before-images of deleted objects) --------------------------------


def _ref(obj: Any) -> str | None:
    return None if obj is None else str(obj.Name)


def to_tmsl(obj: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"name": obj.Name} if isinstance(obj, Obj) else {}
    if isinstance(obj, Obj):
        if obj.Description:
            base["description"] = obj.Description
        if len(obj.Annotations):
            base["annotations"] = [{"name": a.Name, "value": a.Value} for a in obj.Annotations]
        if len(obj.ExtendedProperties):
            base["extendedProperties"] = [
                {"name": p.Name, "value": p.Value} for p in obj.ExtendedProperties
            ]
    if isinstance(obj, Measure):
        base.update(
            expression=obj.Expression,
            formatString=obj.FormatString,
            displayFolder=obj.DisplayFolder,
            isHidden=obj.IsHidden,
            formatStringExpression=obj.FormatStringDefinition.Expression
            if obj.FormatStringDefinition
            else None,
            detailRowsDefinition={"expression": obj.DetailRowsDefinition.Expression}
            if obj.DetailRowsDefinition
            else None,
            kpi={key: getattr(obj.KPI, attr) for key, attr in KPI_FIELDS} if obj.KPI else None,
        )
    elif isinstance(obj, Column):
        base.update(
            type={"Data": "data", "Calculated": "calculated"}.get(
                obj.Type, "calculatedTableColumn"
            ),
            dataType=obj.DataType,
            formatString=obj.FormatString,
            sortByColumn=_ref(obj.SortByColumn),
            dataCategory=obj.DataCategory,
            summarizeBy=obj.SummarizeBy,
            displayFolder=obj.DisplayFolder,
            isHidden=obj.IsHidden,
            isKey=obj.IsKey,
            expression=getattr(obj, "Expression", None),
            sourceColumn=getattr(obj, "SourceColumn", None),
            groupBy=[_ref(g.GroupingColumn) for g in obj.RelatedColumnDetails.GroupByColumns]
            if obj.RelatedColumnDetails
            else None,
        )
    elif isinstance(obj, Table):
        base.update(
            isHidden=obj.IsHidden,
            dataCategory=obj.DataCategory,
            columns=[to_tmsl(c) for c in obj.Columns],
            measures=[to_tmsl(m) for m in obj.Measures],
            hierarchies=[to_tmsl(h) for h in obj.Hierarchies],
            partitions=[
                {
                    "name": p.Name,
                    "source": p.SourceType,
                    "expression": getattr(p.Source, "Expression", None),
                }
                for p in obj.Partitions
            ],
            calculationGroup=None
            if obj.CalculationGroup is None
            else {
                "precedence": obj.CalculationGroup.Precedence,
                "items": [to_tmsl(i) for i in obj.CalculationGroup.CalculationItems],
            },
        )
    elif isinstance(obj, Hierarchy):
        base.update(
            isHidden=obj.IsHidden,
            displayFolder=obj.DisplayFolder,
            levels=[
                {"name": lv.Name, "ordinal": lv.Ordinal, "column": _ref(lv.Column)}
                for lv in obj.Levels
            ],
        )
    elif isinstance(obj, CalculationItem):
        base.update(
            expression=obj.Expression,
            ordinal=obj.Ordinal,
            formatStringExpression=obj.FormatStringDefinition.Expression
            if obj.FormatStringDefinition
            else None,
        )
    elif isinstance(obj, SingleColumnRelationship):
        base.update(
            fromTable=_ref(obj.FromTable),
            fromColumn=_ref(obj.FromColumn),
            toTable=_ref(obj.ToTable),
            toColumn=_ref(obj.ToColumn),
            fromCardinality=obj.FromCardinality,
            toCardinality=obj.ToCardinality,
            crossFilteringBehavior=obj.CrossFilteringBehavior,
            securityFilteringBehavior=obj.SecurityFilteringBehavior,
            isActive=obj.IsActive,
            relyOnReferentialIntegrity=obj.RelyOnReferentialIntegrity,
        )
    elif isinstance(obj, ModelRole):
        base.update(
            modelPermission=obj.ModelPermission,
            tablePermissions=[
                {"name": p.Name, "filterExpression": p.FilterExpression}
                for p in obj.TablePermissions
            ],
        )
        if len(obj.Members):  # as the real serializer does: the members travel with the role
            base["members"] = [{"memberName": m.Name} for m in obj.Members]
    elif isinstance(obj, NamedExpression):
        base.update(kind=obj.Kind, expression=obj.Expression)
    return base


def from_tmsl(cls: type, shape: dict[str, Any]) -> Any:
    obj = cls()
    if isinstance(obj, Obj):
        obj.Name = shape.get("name", "")
        obj.Description = shape.get("description")
        for annotation in shape.get("annotations") or []:
            item = Annotation()
            item.Name, item.Value = annotation["name"], annotation["value"]
            obj.Annotations.Add(item)
        for prop in shape.get("extendedProperties") or []:
            item = JsonExtendedProperty()
            item.Name, item.Value = prop["name"], prop["value"]
            obj.ExtendedProperties.Add(item)
    if isinstance(obj, Measure):
        obj.Expression = shape.get("expression", "")
        obj.FormatString = shape.get("formatString")
        obj.DisplayFolder = shape.get("displayFolder")
        obj.IsHidden = bool(shape.get("isHidden"))
        if shape.get("formatStringExpression"):
            obj.FormatStringDefinition = FormatStringDefinition()
            obj.FormatStringDefinition.Expression = shape["formatStringExpression"]
        if shape.get("detailRowsDefinition"):
            obj.DetailRowsDefinition = DetailRowsDefinition()
            obj.DetailRowsDefinition.Expression = shape["detailRowsDefinition"]["expression"]
        if shape.get("kpi"):
            obj.KPI = KPI()
            for key, attr in KPI_FIELDS:
                setattr(obj.KPI, attr, shape["kpi"].get(key))
    elif isinstance(obj, Column):
        obj.DataType = shape.get("dataType", "String")
        obj.FormatString = shape.get("formatString")
        obj.DataCategory = shape.get("dataCategory")
        obj.SummarizeBy = shape.get("summarizeBy", "Default")
        obj.DisplayFolder = shape.get("displayFolder")
        obj.IsHidden = bool(shape.get("isHidden"))
        obj.IsKey = bool(shape.get("isKey"))
        if isinstance(obj, CalculatedColumn):
            obj.Expression = shape.get("expression") or ""
        if isinstance(obj, DataColumn | CalculatedTableColumn):
            obj.SourceColumn = shape.get("sourceColumn") or ""
        if shape.get("sortByColumn"):
            obj._pending["SortByColumn"] = (None, shape["sortByColumn"])
        if shape.get("groupBy"):
            details = RelatedColumnDetails()
            for name in shape["groupBy"]:
                group = GroupByColumn()
                group._pending["GroupingColumn"] = (None, name)
                details.GroupByColumns.Add(group)
            obj.RelatedColumnDetails = details
            details.parent = obj
    elif isinstance(obj, Table):
        obj.IsHidden = bool(shape.get("isHidden"))
        obj.DataCategory = shape.get("dataCategory")
        for column in shape.get("columns") or []:
            kind = {"calculated": CalculatedColumn, "data": DataColumn}.get(
                column.get("type"), CalculatedTableColumn
            )
            obj.Columns.Add(from_tmsl(kind, column))
        for measure in shape.get("measures") or []:
            obj.Measures.Add(from_tmsl(Measure, measure))
        for hierarchy in shape.get("hierarchies") or []:
            obj.Hierarchies.Add(from_tmsl(Hierarchy, hierarchy))
        for partition in shape.get("partitions") or []:
            part = Partition()
            part.Name = partition["name"]
            source_class = {
                "M": MPartitionSource,
                "Calculated": CalculatedPartitionSource,
                "CalculationGroup": CalculationGroupSource,
            }.get(partition.get("source"))
            if source_class is not None:
                part.Source = source_class()
                if partition.get("expression") is not None:
                    part.Source.Expression = partition["expression"]
            obj.Partitions.Add(part)
        group = shape.get("calculationGroup")
        if group is not None:
            obj.CalculationGroup = CalculationGroup()
            obj.CalculationGroup.Precedence = group.get("precedence", 0)
            for item in group.get("items") or []:
                obj.CalculationGroup.CalculationItems.Add(from_tmsl(CalculationItem, item))
    elif isinstance(obj, Hierarchy):
        obj.IsHidden = bool(shape.get("isHidden"))
        obj.DisplayFolder = shape.get("displayFolder")
        for level in shape.get("levels") or []:
            item = Level()
            item.Name, item.Ordinal = level["name"], level["ordinal"]
            item._pending["Column"] = (None, level["column"])
            obj.Levels.Add(item)
    elif isinstance(obj, CalculationItem):
        obj.Expression = shape.get("expression", "")
        obj.Ordinal = shape.get("ordinal", 0)
        if shape.get("formatStringExpression"):
            obj.FormatStringDefinition = FormatStringDefinition()
            obj.FormatStringDefinition.Expression = shape["formatStringExpression"]
    elif isinstance(obj, SingleColumnRelationship):
        obj._pending["FromColumn"] = (shape["fromTable"], shape["fromColumn"])
        obj._pending["ToColumn"] = (shape["toTable"], shape["toColumn"])
        obj.FromCardinality = shape.get("fromCardinality", "Many")
        obj.ToCardinality = shape.get("toCardinality", "One")
        obj.CrossFilteringBehavior = shape.get("crossFilteringBehavior", "OneDirection")
        obj.SecurityFilteringBehavior = shape.get("securityFilteringBehavior", "OneDirection")
        obj.IsActive = bool(shape.get("isActive", True))
        obj.RelyOnReferentialIntegrity = bool(shape.get("relyOnReferentialIntegrity"))
    elif isinstance(obj, ModelRole):
        obj.ModelPermission = shape.get("modelPermission", "Read")
        for permission in shape.get("tablePermissions") or []:
            item = TablePermission()
            item._pending["Table"] = (permission["name"], None)
            item.FilterExpression = permission.get("filterExpression", "")
            obj.TablePermissions.items.append(item)
            item.parent = obj
        for member in shape.get("members") or []:
            person = ModelRoleMember()
            person.Name = member["memberName"]
            obj.Members.Add(person)
    elif isinstance(obj, NamedExpression):
        obj.Kind = shape.get("kind", "M")
        obj.Expression = shape.get("expression", "")
    return obj


class _Deserializer:
    def __getitem__(self, cls: type) -> Callable[[str], Any]:
        return lambda text: from_tmsl(cls, json.loads(text))


class JsonSerializer:
    DeserializeObject = _Deserializer()

    @staticmethod
    def SerializeObject(obj: Any) -> str:  # noqa: N802
        return json.dumps(to_tmsl(obj))


# --- the model and its server ---------------------------------------------------------------------


class SaveRefused(RuntimeError):
    """What the engine answers when a save cannot be committed."""


class Model(Obj):
    def __init__(self) -> None:
        super().__init__()
        self.Tables = Coll(self)
        self.Relationships = Coll(self)
        self.Roles = Coll(self)
        self.Expressions = Coll(self)
        self.Perspectives = Coll(self)
        self.DiscourageImplicitMeasures = False
        self.Culture = "en-US"
        self.pending: list[tuple[str, str]] = []
        self.saves = 0
        self.fail_saves: list[str] = []  # messages the next saves raise, one per save
        self._saved = self.dump()

    # --- whole-model state ------------------------------------------------------------------

    def dump(self) -> dict[str, Any]:
        return {
            "tables": [to_tmsl(t) for t in self.Tables],
            "relationships": [to_tmsl(r) for r in self.Relationships],
            "roles": [to_tmsl(r) for r in self.Roles],
            "expressions": [to_tmsl(e) for e in self.Expressions],
            "annotations": [{"name": a.Name, "value": a.Value} for a in self.Annotations],
            "discourageImplicitMeasures": self.DiscourageImplicitMeasures,
            "culture": self.Culture,
        }

    def load(self, state: dict[str, Any]) -> None:
        for coll in (
            self.Tables,
            self.Relationships,
            self.Roles,
            self.Expressions,
            self.Annotations,
        ):
            coll.items.clear()
        for table in state.get("tables") or []:
            self.Tables.Add(from_tmsl(Table, table))
        for relationship in state.get("relationships") or []:
            self.Relationships.Add(from_tmsl(SingleColumnRelationship, relationship))
        for role in state.get("roles") or []:
            self.Roles.Add(from_tmsl(ModelRole, role))
        for expression in state.get("expressions") or []:
            self.Expressions.Add(from_tmsl(NamedExpression, expression))
        for annotation in state.get("annotations") or []:
            item = Annotation()
            item.Name, item.Value = annotation["name"], annotation["value"]
            self.Annotations.Add(item)
        self.DiscourageImplicitMeasures = bool(state.get("discourageImplicitMeasures"))
        self.Culture = state.get("culture", "en-US")
        _resolve(self)
        for relationship in self.Relationships:
            _resolve(relationship)
        for role in self.Roles:
            for permission in role.TablePermissions:
                _resolve(permission)

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Model:
        model = cls()
        model.load(copy.deepcopy(state))
        model._saved = model.dump()
        return model

    # --- TOM ------------------------------------------------------------------------------

    def RequestRefresh(self, kind: str) -> None:  # noqa: N802
        self.pending.append(("", kind))

    def UndoLocalChanges(self) -> None:  # noqa: N802
        self.pending.clear()
        self.load(copy.deepcopy(self._saved))

    def SaveChanges(self) -> None:  # noqa: N802
        if self.fail_saves:
            raise SaveRefused(self.fail_saves.pop(0))
        self._check_references()
        for table_name, kind in self.pending:
            if kind != "Full":
                continue
            tables = [self.Tables.Find(table_name)] if table_name else list(self.Tables)
            for table in tables:
                for partition in table.Partitions if table is not None else []:
                    if "UNREACHABLE" in str(getattr(partition.Source, "Expression", "")):
                        self.pending.clear()
                        raise SaveRefused(
                            f"The data source of '{table.Name}' could not be reached: the file "
                            "'F:\\Data\\Sales.csv' was not found."
                        )
        self.pending.clear()
        self.saves += 1
        self._saved = self.dump()

    def _check_references(self) -> None:
        live_tables = {id(t) for t in self.Tables}
        live_columns = {id(c) for t in self.Tables for c in t.Columns}
        for relationship in self.Relationships:
            if (
                id(relationship.FromColumn) not in live_columns
                or id(relationship.ToColumn) not in live_columns
            ):
                raise SaveRefused(
                    f"The relationship '{relationship.Name}' refers to a column that does not exist"
                )
        for table in self.Tables:
            for column in table.Columns:
                if column.SortByColumn is not None and id(column.SortByColumn) not in live_columns:
                    raise SaveRefused(
                        f"'{table.Name}'[{column.Name}] is sorted by a column that does not exist"
                    )
            for hierarchy in table.Hierarchies:
                for level in hierarchy.Levels:
                    if id(level.Column) not in live_columns:
                        raise SaveRefused(
                            f"The hierarchy '{hierarchy.Name}' uses a column that does not exist"
                        )
        for role in self.Roles:
            for permission in role.TablePermissions:
                if id(permission.Table) not in live_tables:
                    raise SaveRefused(f"The role '{role.Name}' filters a table that does not exist")


# --- the engine: schema views and queries ---------------------------------------------------------


def dax_problem(model: Model, text: str, own_table: str | None = None) -> str | None:
    """Why a formula cannot be evaluated against ``model``: a name it uses is missing."""
    tables = {t.Name.casefold(): t for t in model.Tables}
    measures = {m.Name.casefold() for t in model.Tables for m in t.Measures}
    for ref in dax_references(text):
        if ref.name is None:
            continue
        if ref.table is None:
            own = tables.get((own_table or "").casefold())
            if ref.name.casefold() in measures:
                continue
            if own is not None and own.Columns.Find(ref.name) is not None:
                continue
            return f"The value for '{ref.name}' cannot be determined"
        table = tables.get(ref.table.casefold())
        if table is None:
            return f"Cannot find table '{ref.table}'"
        if (
            table.Columns.Find(ref.name) is None
            and table.Measures.Find(ref.name) is None
            and not any(c.Name.casefold() == ref.name.casefold() for c in table.Columns)
        ):
            return f"The column '{ref.name}' in table '{ref.table}' cannot be found"
    return None


def _state(problem: str | None) -> dict[str, Any]:
    return {"State": 5 if problem else 1, "ErrorMessage": problem or ""}


TABLE_ROWS = 10  # every fake table holds this many rows
_COUNT_ROWS = re.compile(r"COUNTROWS\s*\(\s*'?([^')]+?)'?\s*\)", re.IGNORECASE)


def _visible_rows(model: Model, table: str, role: str | None) -> int:
    """Rows of ``table`` seen as ``role``: a role filter keeps half of them (FALSE() none)."""
    if role is None:
        return TABLE_ROWS
    found = model.Roles.Find(role)
    if found is None:
        raise RuntimeError(f"The role '{role}' does not exist")
    permission = found.TablePermissions.Find(table)
    if permission is None or not permission.FilterExpression:
        return TABLE_ROWS
    if permission.FilterExpression.replace(" ", "").upper() == "FALSE()":
        return 0
    return TABLE_ROWS // 2


def engine(
    model: Model, *, fail_queries: tuple[str, ...] = (), role: str | None = None
) -> Callable[[str], list[dict[str, Any]]]:
    """The runner's ``rows``: schema views and EVALUATE queries answered from ``model``
    (``role``: as that role sees the model). ``EVALUATE ROW("Rows", COUNTROWS('T'))`` answers
    the table's row count."""

    def rows(query: str) -> list[dict[str, Any]]:
        tables = list(model.Tables)
        ids = {id(t): index + 1 for index, t in enumerate(tables)}
        if "TMSCHEMA_TABLES" in query:
            return [{"ID": ids[id(t)], "Name": t.Name} for t in tables]
        if "TMSCHEMA_MEASURES" in query:
            return [
                {"TableID": ids[id(t)], "Name": m.Name, **_state(dax_problem(model, m.Expression))}
                for t in tables
                for m in t.Measures
            ]
        if "TMSCHEMA_COLUMNS" in query:
            return [
                {
                    "TableID": ids[id(t)],
                    "ExplicitName": c.Name,
                    **_state(
                        dax_problem(model, c.Expression, t.Name) if c.Type == "Calculated" else None
                    ),
                }
                for t in tables
                for c in t.Columns
            ]
        if "TMSCHEMA_PARTITIONS" in query:
            return [
                {
                    "TableID": ids[id(t)],
                    "Name": p.Name,
                    **_state(
                        dax_problem(model, p.Source.Expression)
                        if isinstance(p.Source, CalculatedPartitionSource)
                        else None
                    ),
                }
                for t in tables
                for p in t.Partitions
            ]
        if "TMSCHEMA_ROLES" in query:
            return [{"ID": index + 1, "Name": r.Name} for index, r in enumerate(model.Roles)]
        if "TMSCHEMA_TABLE_PERMISSIONS" in query:
            return [
                {
                    "RoleID": index + 1,
                    "TableID": ids.get(id(p.Table), 0),
                    **_state(dax_problem(model, p.FilterExpression, p.Table.Name)),
                }
                for index, r in enumerate(model.Roles)
                for p in r.TablePermissions
            ]
        if "TMSCHEMA_CALCULATION_GROUPS" in query:
            return [
                {"ID": ids[id(t)], "TableID": ids[id(t)]}
                for t in tables
                if t.CalculationGroup is not None
            ]
        if "TMSCHEMA_CALCULATION_ITEMS" in query:
            return [
                {
                    "CalculationGroupID": ids[id(t)],
                    "Name": i.Name,
                    **_state(dax_problem(model, i.Expression)),
                }
                for t in tables
                if t.CalculationGroup is not None
                for i in t.CalculationGroup.CalculationItems
            ]
        if query.lstrip().upper().startswith("EVALUATE"):
            for marker in fail_queries:
                if marker in query:
                    raise RuntimeError(f"Query failed: {marker}")
            body = query.lstrip()[len("EVALUATE") :]
            problem = dax_problem(model, body)
            if problem is None:
                for ref in dax_references(body, tables=True):
                    if ref.name is None and ref.table and model.Tables.Find(ref.table) is None:
                        known = {
                            "ROW",
                            "TOPN",
                            "SELECTCOLUMNS",
                            "SUMMARIZECOLUMNS",
                            "COUNTROWS",
                            "FILTER",
                            "NOT",
                            "ISBLANK",
                            "CALCULATE",
                        }
                        if ref.table.upper() not in known:
                            problem = f"Cannot find table '{ref.table}'"
                            break
            if problem:
                raise RuntimeError(problem)
            counted = _COUNT_ROWS.search(body)
            if counted is not None:
                return [{"[Rows]": _visible_rows(model, counted.group(1).strip(), role)}]
            return [{"value": 1}]
        raise RuntimeError(f"Unknown query: {query[:40]}")

    return rows


TOM = SimpleNamespace(
    RefreshType=_enum("Full", "ClearValues", "Calculate", "DataOnly", "Automatic"),
    AggregateFunction=_enum(
        "Default", "None", "Sum", "Min", "Max", "Count", "Average", "DistinctCount"
    ),
    DataType=_enum(
        "Automatic", "String", "Int64", "Double", "DateTime", "Decimal", "Boolean", "Binary"
    ),
    RelationshipEndCardinality=_enum("None", "One", "Many"),
    CrossFilteringBehavior=_enum("OneDirection", "BothDirections", "Automatic"),
    SecurityFilteringBehavior=_enum("OneDirection", "BothDirections", "None"),
    ModelPermission=_enum("None", "Read"),
    ExpressionKind=_enum("M"),
    JsonSerializer=JsonSerializer,
    Measure=Measure,
    KPI=KPI,
    DetailRowsDefinition=DetailRowsDefinition,
    ModelRoleMember=ModelRoleMember,
    CalculatedColumn=CalculatedColumn,
    DataColumn=DataColumn,
    CalculatedTableColumn=CalculatedTableColumn,
    Table=Table,
    Partition=Partition,
    MPartitionSource=MPartitionSource,
    CalculatedPartitionSource=CalculatedPartitionSource,
    CalculationGroupSource=CalculationGroupSource,
    SingleColumnRelationship=SingleColumnRelationship,
    Hierarchy=Hierarchy,
    Level=Level,
    ModelRole=ModelRole,
    TablePermission=TablePermission,
    CalculationGroup=CalculationGroup,
    CalculationItem=CalculationItem,
    FormatStringDefinition=FormatStringDefinition,
    NamedExpression=NamedExpression,
    Annotation=Annotation,
    JsonExtendedProperty=JsonExtendedProperty,
    RelatedColumnDetails=RelatedColumnDetails,
    GroupByColumn=GroupByColumn,
)


class Server:
    """Desktop's engine for one open model: the runner protocol answered in-process by the real
    runner module. ``hang_after`` makes the call end without a result after that many applied
    changes (the runner never answered); ``calls`` records every request."""

    def __init__(self, model: Model, *, fail_queries: tuple[str, ...] = ()) -> None:
        self.model = model
        self.fail_queries = fail_queries
        self.calls: list[dict[str, Any]] = []
        self.hang_after: int | None = None
        self.hang_on_saved = False

    def __call__(self, request: dict[str, Any], timeout: float) -> RunnerAnswer:
        self.calls.append(copy.deepcopy(request))
        module = load_runner()
        answer = RunnerAnswer()

        class _Hang(BaseException):  # not an Exception: the runner must not catch it
            pass

        def emit(event: dict[str, Any]) -> None:
            answer.events.append(json.loads(json.dumps(event, default=str)))
            applied = sum(1 for e in answer.events if e.get("event") == "applied")
            if self.hang_after is not None and applied >= self.hang_after:
                raise _Hang
            if self.hang_on_saved and event.get("event") == "saved":
                raise _Hang

        rows = engine(self.model, fail_queries=self.fail_queries)

        def rows_as(role: str) -> Callable[[str], list[dict[str, Any]]]:
            return engine(self.model, fail_queries=self.fail_queries, role=role)

        runner = module.Runner(TOM, self.model, rows, emit, rows_as)
        answer.finished = True
        answer.exit_code = 0
        if request.get("action") == "snapshot":
            answer.result = {
                "event": "result",
                "ok": True,
                "stage": "done",
                "snapshot": runner.snapshot(),
            }
            return answer
        if request.get("action") == "query":
            role = request.get("role")
            if role and self.model.Roles.Find(role) is None:
                answer.result = {
                    "event": "result",
                    "ok": False,
                    "stage": "request",
                    "error": f"The model has no role '{role}'",
                }
                return answer
            answer.result = {
                "event": "result",
                "ok": True,
                "stage": "done",
                **module.read_queries(rows_as(role) if role else rows, request),
            }
            return answer
        try:
            result = runner.apply(request)
        except _Hang:
            # The process "died" mid-run: what it had not saved is lost, as with a killed runner;
            # what it saved stays in the model.
            self.hang_after = None
            self.hang_on_saved = False
            self.model.UndoLocalChanges()
            answer.finished = False  # killed at the timeout
            answer.exit_code = None
            return answer
        answer.result = json.loads(json.dumps({"event": "result", **result}, default=str))
        return answer


# --- a small model like the Agents dashboard's ---------------------------------------------------


def _data_table(name: str, columns: list[tuple[str, str]], m: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "columns": [
            {"name": c, "type": "data", "dataType": t, "sourceColumn": c, "summarizeBy": "Default"}
            for c, t in columns
        ],
        "measures": [],
        "hierarchies": [],
        "partitions": [
            {
                "name": name,
                "source": "M",
                "expression": m
                or f'let Source = Csv.Document(File.Contents("F:\\Data\\{name}.csv")) in Source',
            }
        ],
        "calculationGroup": None,
    }


def agents_state() -> dict[str, Any]:
    """Tables, columns and relationships of a model shaped like the Agents dashboard's (the
    Contoso sales schema), with a measure table and a calculated date table."""
    date = _data_table(
        "Date",
        [("Date", "DateTime"), ("Year", "Int64"), ("Month", "String"), ("Month Number", "Int64")],
    )
    date["partitions"] = [{"name": "Date", "source": "Calculated", "expression": "CALENDARAUTO()"}]
    measures = {
        "name": "# Measures",
        "columns": [],
        "measures": [
            {"name": "Rank", "expression": "RANKX(ALL('DimChannel'), 1)"},
            {"name": "Employees Avg MTD Sales", "expression": "SUM(FactSales[SalesAmount])"},
            {"name": "dummy", "expression": "BLANK()"},
        ],
        "hierarchies": [],
        "partitions": [{"name": "# Measures", "source": "M", "expression": "let x = 1 in x"}],
        "calculationGroup": None,
    }
    return {
        "tables": [
            _data_table(
                "FactSales",
                [
                    ("SalesKey", "Int64"),
                    ("DateKey", "DateTime"),
                    ("channelKey", "Int64"),
                    ("StoreKey", "Int64"),
                    ("SalesAmount", "Double"),
                    ("TotalCost", "Double"),
                ],
            ),
            date,
            _data_table(
                "DimStore",
                [("StoreKey", "Int64"), ("StoreName", "String"), ("StoreType", "String")],
            ),
            _data_table("DimChannel", [("ChannelKey", "Int64"), ("ChannelName", "String")]),
            _data_table("DimEmployee", [("EmployeeKey", "Int64"), ("FirstName", "String")]),
            measures,
        ],
        "relationships": [
            {
                "name": "r-store",
                "fromTable": "FactSales",
                "fromColumn": "StoreKey",
                "toTable": "DimStore",
                "toColumn": "StoreKey",
            },
            {
                "name": "r-channel",
                "fromTable": "FactSales",
                "fromColumn": "channelKey",
                "toTable": "DimChannel",
                "toColumn": "ChannelKey",
            },
            {
                "name": "r-date",
                "fromTable": "FactSales",
                "fromColumn": "DateKey",
                "toTable": "Date",
                "toColumn": "Date",
            },
        ],
        "roles": [],
        "expressions": [],
        "annotations": [{"name": "__PBI_TimeIntelligenceEnabled", "value": "1"}],
        "discourageImplicitMeasures": False,
        "culture": "en-US",
    }


def snapshot_of(model: Model) -> dict[str, Any]:
    """The runner's snapshot of ``model`` (the shape the live checks and the saved facts use)."""
    module = load_runner()
    snap: dict[str, Any] = module.Runner(TOM, model, None, lambda _event: None).snapshot()
    return snap
