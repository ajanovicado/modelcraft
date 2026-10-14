"""Every kind of model change through Studio's TOM runner, in real Power BI Desktop (live check).

Run it by hand, on a machine with Power BI Desktop, against the Agents dashboard (only read):

    .venv\\Scripts\\python.exe evals\\imported_edit\\model_probe.py ^
        --pbix "C:\\Users\\developer\\Downloads\\Agents Performance - Dashboard.pbix"

Optional: ``--work-dir`` (default: a new temp folder; a folder inside the repository is refused,
the copies hold the report's data), ``--out`` (receipt folder, default
``evals/imported_edit/evidence``), ``--bridge node.exe index.js``, ``--pbi pbi.exe`` and
``--desktop-exe PBIDesktop.exe`` (default: found the way Studio finds them), ``--screenshot``
(reopen the rewritten report after the renames and capture page 1 into the work folder for a
person to look at).

What it does, on its own copy of the file:
1. Opens the copy in Desktop through ``DesktopController`` and reads the live model (the runner's
   snapshot) and the file's model facts (``pbix_extract.read_model_facts``).
2. Applies, one batch each, through ``DesktopController.apply_model_operations``, every kind of
   model change (``plan_batches``): model settings, calculated columns and their properties,
   a relationship change, hierarchies (create, change, delete), a field parameter, a calculation
   group, a row-level security role, the date table, a hidden imported table, Power Query
   queries and parameters (create, change, delete; a DateTime parameter keeps its type), a table
   query whose refresh fails (undone, then kept without data), a plain refresh (expected to fail:
   the dashboard's sources are gone), column deletes (calculated and loaded), a column rename and
   a table rename. After each batch the live model is held to the batch (``saved_model_problems``
   on the snapshot) and to value checks read through DAX queries: the calculated column is not
   blank, FactSales keeps its row count, the field parameter has its rows, the calculation item
   Double is twice Current, and the role sees fewer rows than everyone (a second connection
   with Roles=<role>, Desktop's "View as").
3. Saves through Desktop's own Save, closes, reads the saved file's model facts back and holds
   them to every batch; checks the shapes Studio marks unverified (``UNVERIFIED_SHAPES``) and
   applies the report half of the renames to the saved file.
4. Reopens the saved copy, checks the calculated column is still not blank, undoes every batch
   from its before-images (``DesktopController.undo_model_operations``), checks that the live model
   is the one it started from, saves, closes, and compares the saved facts with the original's.
5. Writes ``<out>/model-probe-<Desktop version>.json``: every step with its timing and outcome,
   every check with what was expected and observed, ``passed``, and ``verified_ops``: the kinds of
   change outside Microsoft's documented list (``report_edit_model.OUTSIDE_SUPPORT``) whose own
   steps passed, listed only when the save-and-read-back and the undo passed too. Studio runs
   those kinds only for a Desktop version whose receipt lists them. The receipt holds no table
   data, and no M, DAX or role filter text: each is summarized as its SHA-256 and length, and
   file paths in error messages are replaced by "<path>".

It never ends a process by force: a Desktop it opened is saved (it is the probe's own copy) and
closed however the probe ends, and one that cannot be closed safely is left running and listed at
the end. It takes about ten minutes (two Desktop opens, two saves).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_powerbi_analyst.preflight import probe_pbi_cli, probe_powerbi_desktop, probe_tool
from ai_powerbi_analyst.studio import pbix_extract
from ai_powerbi_analyst.studio.desktop_model import ModelApplyResult
from ai_powerbi_analyst.studio.desktop_session import (
    DesktopController,
    DesktopInstance,
    DesktopRefreshFailed,
    DesktopSessionError,
)
from ai_powerbi_analyst.studio.imported_editing import bridge_argv
from ai_powerbi_analyst.studio.pbir_document import PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_model import (
    OUTSIDE_SUPPORT,
    UNVERIFIED_SHAPES,
    CalculationItemSpec,
    CreateCalculatedColumn,
    CreateCalculationGroup,
    CreateFieldParameter,
    CreateHierarchy,
    CreateRole,
    DeleteColumn,
    DeleteHierarchy,
    DeleteNamedExpression,
    HierarchyLevel,
    MarkDateTable,
    ParameterField,
    RefreshData,
    RenameColumn,
    RenameTable,
    RoleFilter,
    SetColumnProperties,
    SetModelOptions,
    SetNamedExpression,
    SetQueryParameter,
    SetTableProperties,
    SetTableQuery,
    UpdateHierarchy,
    UpdateRelationship,
    field_references,
    rename_column_references,
    rename_table_references,
    saved_model_problems,
)
from ai_powerbi_analyst.studio.report_edit_ops import CreateMeasure

DEFAULT_PBIX = Path.home() / "Downloads" / "Agents Performance - Dashboard.pbix"
RECEIPT_KIND = "model_probe"
REPO = Path(__file__).resolve().parents[2]
# Facts that change with every save and say nothing about the model. A column's state is one of
# them here: a loaded column put back by the undo has no data until the next refresh.
_VOLATILE = {"lineageTag", "refreshedTime", "error", "truncated", "version", "state"}
# Operation and fact fields that hold M, DAX or a role filter: summarized, never written out.
_CODE_KEYS = {
    "expression",
    "format_string_expression",
    "formatStringExpression",
    "filter",
    "filterExpression",
    "detailRowsExpression",
    "source",
}
_PATH = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\s'\"<>|]*")
FACT = "FactSales"


def digest(text: str) -> dict[str, Any]:
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "length": len(text)}


def redacted(value: Any) -> Any:
    """``value`` with every M, DAX and role filter text replaced by its hash and length, and
    file paths in texts replaced by "<path>" (the dashboard's sources are local files)."""
    if isinstance(value, dict):
        return {
            k: digest(v) if k in _CODE_KEYS and isinstance(v, str) else redacted(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redacted(v) for v in value]
    if isinstance(value, str):
        return _PATH.sub("<path>", value)
    return value


@dataclass
class Step:
    name: str
    status: str = "not run"  # passed | failed | error | not run
    seconds: float = 0.0
    operations: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    expected: Any = None
    observed: dict[str, Any] = field(default_factory=dict[str, Any])
    error: str | None = None
    covers: tuple[str, ...] = ()


@dataclass
class Probe:
    controller: DesktopController
    work: Path
    steps: list[Step] = field(default_factory=list[Step])
    applied: list[ModelApplyResult] = field(default_factory=list[ModelApplyResult])
    batches: list[list[Any]] = field(default_factory=list[list[Any]])
    left_open: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    opened: list[DesktopInstance] = field(default_factory=list[DesktopInstance])

    def log(self, text: str) -> None:
        print(f"[{datetime.now(UTC):%H:%M:%S}] {text}", flush=True)

    def run(self, name: str, action: Callable[[Step], None], covers: tuple[str, ...] = ()) -> Step:
        step = Step(name, covers=covers)
        self.log(f"step: {name}")
        started = time.monotonic()
        try:
            action(step)
        except Exception as error:  # noqa: BLE001 - every failure is recorded, the probe goes on
            step.status = "error"
            step.error = f"{type(error).__name__}: {error}"[:2000]
            step.observed["traceback"] = traceback.format_exc()[-4000:]
        step.seconds = round(time.monotonic() - started, 1)
        self.log(
            f"  -> {step.status} in {step.seconds}s" + (f" ({step.error})" if step.error else "")
        )
        self.steps.append(step)
        return step

    def open(self, path: Path) -> DesktopInstance:
        instance = self.controller.open(path, normalize=True)
        self.opened.append(instance)
        return instance

    def finish(self, instance: DesktopInstance) -> bool:
        """Save (it is our own copy) and close the Desktop this probe opened; never forced."""
        try:
            if self.controller.status(instance).get("hasUnsavedChanges"):
                self.controller.save(instance)
            self.controller.close(instance)
            if instance in self.opened:
                self.opened.remove(instance)
            return True
        except DesktopSessionError as error:
            self.left_open.append(
                {"pid": instance.pid, "file": str(instance.path), "reason": str(error)}
            )
            if instance in self.opened:
                self.opened.remove(instance)
            return False

    def finish_all(self) -> None:
        """Whatever happened: every Desktop this probe opened and still holds is saved and
        closed (never killed)."""
        for instance in list(self.opened):
            self.finish(instance)

    def query(self, instance: DesktopInstance, text: str, role: str | None = None) -> Any:
        """The first value of a one-row DAX query, or None."""
        answers = self.controller.model_query(instance, {"q": text}, role=role)
        answer = answers.get("q") or {}
        rows = answer.get("rows") or []
        if not answer.get("ok") or not rows or not isinstance(rows[0], dict):
            return None
        return next(iter(rows[0].values()), None)


def normalized(value: Any) -> Any:
    """Order-insensitive, without the facts that change on every save."""
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return sorted(
            (normalized(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True, default=str)
        )
    return value


def table_of(snapshot: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((t for t in snapshot.get("tables", []) if t.get("name") == name), None)


def column_names(snapshot: dict[str, Any], table: str) -> list[str]:
    found = table_of(snapshot, table)
    return [c["name"] for c in found.get("columns", [])] if found else []


def serialize(operations: Sequence[Any]) -> list[dict[str, Any]]:
    """The operations for the receipt: M, DAX and role filters summarized."""
    found: list[dict[str, Any]] = redacted([op.model_dump(mode="json") for op in operations])
    return found


# --- the batches ---------------------------------------------------------------------------------

Check = Callable[["Probe", DesktopInstance, Step], list[str]]


@dataclass
class Batch:
    """One batch of the probe: its operations, what a person should see, the kinds of change
    outside Microsoft's list it proves (``OUTSIDE_SUPPORT`` keys), whether its refresh is expected
    to fail, and value checks read through DAX after it was applied."""

    name: str
    operations: list[Any]
    expect: str
    covers: tuple[str, ...] = ()
    refresh_fails: bool = False
    checks: list[Check] = field(default_factory=list[Check])


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def check_not_blank(table: str, column: str) -> Check:
    def check(probe: Probe, instance: DesktopInstance, step: Step) -> list[str]:
        ref = f"'{table}'[{column}]"
        filled = _number(
            probe.query(
                instance, f"EVALUATE ROW(\"n\", COUNTROWS(FILTER('{table}', NOT ISBLANK({ref}))))"
            )
        )
        step.observed[f"{table}[{column}] rows not blank"] = filled
        return [] if filled else [f"{table}[{column}] is blank in every row"]

    return check


def check_rows(table: str, wanted: int) -> Check:
    def check(probe: Probe, instance: DesktopInstance, step: Step) -> list[str]:
        answers = probe.controller.model_query(instance, {"t": f"EVALUATE '{table}'"})
        rows = (answers.get("t") or {}).get("rows") or []
        step.observed[f"rows of {table}"] = len(rows)
        return [] if len(rows) == wanted else [f"'{table}' has {len(rows)} rows, not {wanted}"]

    return check


def check_double(group: str, column: str, measure: str) -> Check:
    def check(probe: Probe, instance: DesktopInstance, step: Step) -> list[str]:
        values = []
        for item in ("Current", "Double"):
            text = f'EVALUATE ROW("v", CALCULATE([{measure}], \'{group}\'[{column}] = "{item}"))'
            values.append(_number(probe.query(instance, text)))
        step.observed["Current and Double"] = values
        current, double = values
        if not current or double is None or abs(double - 2 * current) > 1e-6 * abs(current):
            return ["the calculation item Double is not twice Current"]
        return []

    return check


def check_role(role: str, table: str) -> Check:
    def check(probe: Probe, instance: DesktopInstance, step: Step) -> list[str]:
        text = f"EVALUATE ROW(\"n\", COUNTROWS('{table}'))"
        everyone = _number(probe.query(instance, text))
        viewed = _number(probe.query(instance, text, role=role))
        step.observed[f"rows of {table}: everyone, as {role}"] = [everyone, viewed]
        if everyone is None or viewed is None or not viewed < everyone:
            return [f"viewed as '{role}', '{table}' does not show fewer rows than for everyone"]
        return []

    return check


def check_parameter(name: str, type_name: str) -> Check:
    def check(probe: Probe, instance: DesktopInstance, step: Step) -> list[str]:
        live = probe.controller.model_snapshot(instance)
        text = next((e["expression"] for e in live["expressions"] if e["name"] == name), "")
        kept = f'Type="{type_name}"' in text and "IsParameterQuery=true" in text
        step.observed[f"{name} keeps Type {type_name}"] = kept
        return [] if kept else [f"the parameter '{name}' lost its type {type_name}"]

    return check


def check_query_order(name: str, present: bool) -> Check:
    def check(probe: Probe, instance: DesktopInstance, step: Step) -> list[str]:
        live = probe.controller.model_snapshot(instance)
        order = (live.get("model") or {}).get("annotations", {}).get("PBI_QueryOrder")
        if not isinstance(order, str):
            step.observed["query order"] = "the model has no PBI_QueryOrder annotation"
            return []
        listed = name in json.loads(order)
        step.observed[f"{name} in the query order"] = listed
        return [] if listed == present else [f"the query order does not match for '{name}'"]

    return check


def _formulas(snapshot: dict[str, Any]) -> str:
    texts: list[str] = []
    for table in snapshot.get("tables", []):
        texts += [str(m.get("expression") or "") for m in table.get("measures", [])]
        texts += [str(c.get("expression") or "") for c in table.get("columns", [])]
        texts += [str(p.get("expression") or "") for p in table.get("partitions", [])]
        group = table.get("calculationGroup") or {}
        texts += [str(i.get("expression") or "") for i in group.get("items", [])]
    for role in snapshot.get("roles", []):
        texts += [str(f) for f in (role.get("tablePermissions") or {}).values()]
    return "\n".join(texts)


def unused_data_column(snapshot: dict[str, Any], avoid: set[str]) -> tuple[str, str] | None:
    """A column loaded from the source that no formula, relationship, sort order or hierarchy
    of the live model names (deleting it proves "delete a loaded column"; undo restores it)."""
    formulas = _formulas(snapshot)
    related = {
        (r[f"{side}Table"], r[f"{side}Column"])
        for r in snapshot.get("relationships", [])
        for side in ("from", "to")
    }
    for table in snapshot.get("tables", []):
        if table.get("kind") != "data" or table["name"] in avoid:
            continue
        sorted_by = {c.get("sortByColumn") for c in table.get("columns", [])}
        levels = {lv["column"] for h in table.get("hierarchies", []) for lv in h["levels"]}
        for column in table.get("columns", []):
            name = column["name"]
            if (
                column.get("type") == "Data"
                and (table["name"], name) not in related
                and name not in sorted_by
                and name not in levels
                and f"[{name}]" not in formulas
            ):
                return table["name"], name
    return None


def plan_batches(snapshot: dict[str, Any]) -> list[Batch]:
    """The probe's batches for the live model ``snapshot``, in order. Names are picked from the
    model itself, so the probe says clearly when the dashboard changed."""
    for table, columns in (
        ("FactSales", ("SalesAmount", "TotalCost", "StoreKey")),
        ("Date", ("Year", "Month", "Date")),
    ):
        missing = [c for c in columns if c not in column_names(snapshot, table)]
        if missing:
            raise RuntimeError(
                f"The model has no {table}[{', '.join(missing)}]; the probe expects the Agents "
                "dashboard"
            )
    measures: list[dict[str, Any]] = next(
        (t["measures"] for t in snapshot["tables"] if t["name"] == "# Measures"), []
    )
    if len(measures) < 2:
        raise RuntimeError("The model has fewer than two measures in '# Measures'")
    relationship = next(
        (
            r
            for r in snapshot["relationships"]
            if r["fromTable"] == "FactSales" and r["crossFilteringBehavior"] == "OneDirection"
        ),
        None,
    )
    if relationship is None:
        raise RuntimeError("The model has no single-direction relationship from FactSales")
    precedences = [
        t["calculationGroup"]["precedence"] for t in snapshot["tables"] if t.get("calculationGroup")
    ]
    fact_table = table_of(snapshot, "FactSales") or {}
    fact_query = next(
        (p["expression"] for p in fact_table.get("partitions", []) if p["sourceType"] == "M"),
        None,
    )
    if fact_query is None:
        raise RuntimeError("FactSales does not load from a Power Query query")
    # The same query with one more step: valid M, the same rows, and a refresh that must reach the
    # source again (the dashboard's CSV files are gone, so it fails).
    changed_query = f"let\n    Studio = {fact_query}\nin\n    Studio"
    for table, column in (("DimChannel", "ChannelName"), ("DimStore", "StoreName")):
        if column not in column_names(snapshot, table):
            raise RuntimeError(f"The model has no {table}[{column}]; the probe expects the Agents")
    hidden_table = next(
        (
            t["name"]
            for t in snapshot["tables"]
            if t.get("kind") == "data"
            and t["name"] not in (FACT, "DimChannel", "DimStore", "Date", "# Measures")
        ),
        None,
    )
    loaded = unused_data_column(snapshot, avoid={FACT, "DimChannel", "DimStore", "Date"})
    batches = [
        Batch(
            "discourage implicit measures",
            [SetModelOptions(discourage_implicit_measures=True)],
            "the model's Discourage implicit measures property is on (Model view)",
            covers=("model_options",),
        ),
        Batch(
            "calculated columns and their properties",
            [
                CreateCalculatedColumn(
                    table=FACT,
                    name="Studio Margin",
                    expression="FactSales[SalesAmount] - FactSales[TotalCost]",
                    format_string="#,0.00",
                ),
                CreateCalculatedColumn(
                    table="DimStore", name="Studio Link", expression='"https://example.com"'
                ),
                CreateCalculatedColumn(table="DimStore", name="Studio Temp Column", expression="1"),
                SetColumnProperties(
                    table=FACT, name="Studio Margin", format_string="#,0.0", summarize_by="none"
                ),
                SetColumnProperties(table="DimStore", name="Studio Link", data_category="web_url"),
            ],
            "FactSales has Studio Margin with values (not blank), no summarization, one decimal; "
            "DimStore[Studio Link] is a Web URL",
            checks=[check_not_blank(FACT, "Studio Margin")],
        ),
        Batch(
            "relationship cross-filter change",
            [
                UpdateRelationship(
                    from_table=relationship["fromTable"],
                    from_column=relationship["fromColumn"],
                    to_table=relationship["toTable"],
                    to_column=relationship["toColumn"],
                    cross_filter="both",
                )
            ],
            "the relationship shows a double arrow in the Model view",
        ),
        Batch(
            "hierarchies on Date",
            [
                CreateHierarchy(
                    table="Date",
                    name="Studio Calendar",
                    levels=[HierarchyLevel(column="Year"), HierarchyLevel(column="Month")],
                ),
                CreateHierarchy(
                    table="Date", name="Studio Temp", levels=[HierarchyLevel(column="Year")]
                ),
            ],
            "Date has hierarchies Studio Calendar (Year > Month) and Studio Temp",
            covers=("hierarchies",),
        ),
        Batch(
            "hierarchy levels changed, one hierarchy deleted",
            [
                UpdateHierarchy(
                    table="Date",
                    name="Studio Calendar",
                    levels=[
                        HierarchyLevel(column="Year"),
                        HierarchyLevel(column="Month"),
                        HierarchyLevel(column="Date"),
                    ],
                ),
                DeleteHierarchy(table="Date", name="Studio Temp"),
            ],
            "Studio Calendar is Year > Month > Date; Studio Temp is gone",
            covers=("hierarchies",),
        ),
        Batch(
            "field parameter over two measures",
            [
                CreateFieldParameter(
                    name="Studio Metric",
                    fields=[
                        ParameterField(table="# Measures", name=m["name"]) for m in measures[:2]
                    ],
                )
            ],
            "Studio Metric appears with the field-parameter icon; a slicer on it switches the "
            "measures",
            checks=[check_rows("Studio Metric", 2)],
        ),
        Batch(
            "calculation group with two items",
            [
                CreateMeasure(
                    table="# Measures", name="Studio Rows", expression=f"COUNTROWS('{FACT}')"
                ),
                CreateCalculationGroup(
                    name="Studio Time Calc",
                    items=[
                        CalculationItemSpec(name="Current", expression="SELECTEDMEASURE()"),
                        CalculationItemSpec(
                            name="Double",
                            expression="SELECTEDMEASURE() * 2",
                            format_string_expression='"#,0.0"',
                        ),
                    ],
                    precedence=max(precedences, default=0) + 1,
                ),
            ],
            "Studio Time Calc appears as a calculation group with Current and Double",
            checks=[check_double("Studio Time Calc", "Name", "Studio Rows")],
        ),
        Batch(
            "row-level security role",
            [
                CreateRole(
                    name="Studio Probe Role",
                    filters=[
                        RoleFilter(table="DimChannel", expression='[ChannelName] <> "Online"')
                    ],
                )
            ],
            "Modeling > Manage roles lists Studio Probe Role with a DimChannel filter; View as "
            "that role shows fewer channels",
            checks=[check_role("Studio Probe Role", "DimChannel")],
        ),
        Batch(
            "mark Date as the date table",
            [MarkDateTable(table="Date", column="Date")],
            "Date is marked as a date table on [Date] (table tools)",
            covers=("mark_date_table",),
        ),
        Batch(
            "queries and parameters created",
            [
                SetNamedExpression(name="Studio Probe Query", expression='"probe"'),
                SetQueryParameter(name="Studio Probe Text", value="a", type="text"),
                SetQueryParameter(name="Studio Probe Start", value="2024-01-01", type="date_time"),
            ],
            "Transform data lists the query Studio Probe Query and the parameters Studio Probe "
            "Text (Text) and Studio Probe Start (Date/Time)",
            covers=("named_expressions", "query_parameters"),
            checks=[check_query_order("Studio Probe Query", True)],
        ),
        Batch(
            "queries and parameters changed",
            [
                SetNamedExpression(name="Studio Probe Query", expression='"probe 2"'),
                SetQueryParameter(name="Studio Probe Text", value="b"),
                SetQueryParameter(name="Studio Probe Start", value="2025-06-30"),
            ],
            "the parameters hold b and 2025-06-30 and keep their types",
            covers=("named_expressions", "query_parameters"),
            checks=[check_parameter("Studio Probe Start", "DateTime")],
        ),
        Batch(
            "query deleted",
            [DeleteNamedExpression(name="Studio Probe Query")],
            "Studio Probe Query is gone from Transform data",
            covers=("named_expressions",),
            checks=[check_query_order("Studio Probe Query", False)],
        ),
        Batch(
            "Power Query change whose refresh fails (undone)",
            [SetTableQuery(table=FACT, expression=changed_query, if_refresh_fails="undo")],
            "the refresh fails (the CSV source is gone) and FactSales keeps its query and rows",
            covers=("set_table_query",),
            refresh_fails=True,
        ),
        Batch(
            "Power Query change kept without data",
            [SetTableQuery(table=FACT, expression=changed_query, if_refresh_fails="keep")],
            "the new query is kept; FactSales keeps the rows it had",
            covers=("set_table_query",),
        ),
        Batch(
            "plain refresh (fails: the sources are gone)",
            [RefreshData(table=FACT)],
            "Studio reports that Power BI could not reload the data and keeps the model as it was "
            "(verified on the failure path only)",
            covers=("refresh_data",),
            refresh_fails=True,
        ),
        Batch(
            "calculated column deleted",
            [DeleteColumn(table="DimStore", name="Studio Temp Column")],
            "DimStore no longer has Studio Temp Column",
        ),
        Batch(
            "column rename (formulas rewritten in the same save)",
            [RenameColumn(table="DimStore", name="StoreName", new_name="Studio Store Name")],
            "DimStore[StoreName] is Studio Store Name; formulas that used it still work",
            covers=("rename_column",),
            checks=[check_not_blank("DimStore", "Studio Store Name")],
        ),
        Batch(
            "table rename (formulas rewritten in the same save)",
            [RenameTable(name="DimChannel", new_name="Studio Channel")],
            "the table is Studio Channel; measures that used DimChannel still work; the role "
            "filter moved with it",
            covers=("rename_table",),
            checks=[check_role("Studio Probe Role", "Studio Channel")],
        ),
    ]
    if hidden_table is not None:
        batches.insert(
            9,
            Batch(
                f"imported table hidden and described ({hidden_table})",
                [SetTableProperties(table=hidden_table, hidden=True, description="Studio probe")],
                f"'{hidden_table}' is hidden in report view",
                covers=("data_table_properties",),
            ),
        )
    if loaded is not None:
        batches.insert(
            -2,
            Batch(
                f"loaded column deleted ({loaded[0]}[{loaded[1]}])",
                [DeleteColumn(table=loaded[0], name=loaded[1])],
                f"{loaded[0]} no longer has [{loaded[1]}] (the undo puts it back without data)",
                covers=("delete_data_column",),
            ),
        )
    return batches


FACT_ROWS = f"EVALUATE ROW(\"rows\", COUNTROWS('{FACT}'))"


def apply_batch(probe: Probe, instance: DesktopInstance, batch: Batch, step: Step) -> None:
    controller = probe.controller
    operations = batch.operations
    step.operations = serialize(operations)
    step.expected = batch.expect
    before = controller.model_snapshot(instance)
    rows_before = probe.query(instance, FACT_ROWS)
    step.observed["fact_rows_before"] = rows_before
    try:
        result = controller.apply_model_operations(instance, operations)
    except DesktopRefreshFailed as error:
        step.observed["refresh_failed"] = redacted(error.reason[:500])
        after = controller.model_snapshot(instance)
        unchanged = normalized(after) == normalized(before)
        step.observed["model_unchanged"] = unchanged
        step.observed["fact_rows_after"] = probe.query(instance, FACT_ROWS)
        kept_rows = step.observed["fact_rows_after"] == rows_before
        step.status = "passed" if batch.refresh_fails and unchanged and kept_rows else "failed"
        return
    problems: list[str] = []
    if batch.refresh_fails:
        problems.append("the refresh was expected to fail (the dashboard's sources are gone)")
    probe.applied.append(result)
    probe.batches.append(operations)
    step.observed.update(
        done=result.done,
        warnings=redacted(result.warnings),
        fixups=result.fixups,
        probes=redacted(result.probes),
        refreshed=result.refreshed,
        refresh_error=redacted(result.refresh_error),
        kept_without_refresh=result.kept_without_refresh,
    )
    live = controller.model_snapshot(instance)
    found, unchecked = saved_model_problems(operations, live)
    problems += found
    step.observed["live_unchecked"] = unchecked
    rows_after = probe.query(instance, FACT_ROWS)
    step.observed["fact_rows_after"] = rows_after
    if rows_after != rows_before:
        problems.append(f"{FACT} has {rows_after} rows, not {rows_before}")
    for check in batch.checks:
        problems += check(probe, instance, step)
    step.observed["problems"] = problems
    step.status = "passed" if not problems else "failed"


# --- the checks on the saved file ----------------------------------------------------------------


def shape_verdicts(facts: dict[str, Any]) -> dict[str, Any]:
    """What the saved file says about each shape Studio could not confirm from a Desktop file."""
    tables = {t.get("name"): t for t in facts.get("tables", [])}
    parameters = {p.get("table"): p for p in facts.get("fieldParameters", [])}
    groups = {g.get("table"): g for g in facts.get("calculationGroups", [])}
    columns = [c for c in facts.get("columns", []) if c.get("table") == "Studio Metric"]
    expressions = {e.get("name"): e for e in facts.get("expressions", [])}
    link: dict[str, Any] = next(
        (
            c
            for c in facts.get("columns", [])
            if c.get("table") == "DimStore" and c.get("name") == "Studio Link"
        ),
        {},
    )
    start = str((expressions.get("Studio Probe Start") or {}).get("expression") or "")
    text = str((expressions.get("Studio Probe Text") or {}).get("expression") or "")
    return {
        "query_parameter": {
            "date_time_keeps_its_type": 'Type="DateTime"' in start
            and start.startswith("#datetime"),
            "text_parameter": text.startswith('"b" meta') and 'Type="Text"' in text,
            "flagged_as_parameter": (expressions.get("Studio Probe Text") or {}).get("parameter"),
        },
        "data_category": link.get("dataCategory"),
        "summarization_set_by": "not in the saved facts (a column annotation)",
        "query_order": "checked live after the query batches",
        "field_parameter": {
            "listed_as_field_parameter": "Studio Metric" in parameters,
            "columns": sorted(
                (c.get("name"), c.get("sourceColumn"), c.get("hidden"), c.get("sortByColumn"))
                for c in columns
            ),
            "rows": parameters.get("Studio Metric", {}).get("rows"),
        },
        "calculation_group": {
            "items": [i.get("name") for i in groups.get("Studio Time Calc", {}).get("items", [])],
            "columns": sorted(
                (c.get("name"), c.get("sortByColumn"), c.get("hidden"))
                for c in facts.get("columns", [])
                if c.get("table") == "Studio Time Calc"
            ),
            "discourage_implicit_measures": facts.get("model", {}).get(
                "discourageImplicitMeasures"
            ),
        },
        "date_table": {
            "is_date_table": tables.get("Date", {}).get("isDateTable"),
            "date_column": tables.get("Date", {}).get("dateColumn"),
        },
    }


def rewrite_report(probe: Probe, saved: Path) -> dict[str, Any]:
    """The report half of the column and table renames on the file Desktop saved (in the order
    they were applied); returns the counts."""
    package = PbixPackage(saved)
    document = PbirDocument(package)
    references = field_references(document)
    columns_before = sum(1 for _, t, n in references if (t, n) == ("DimStore", "StoreName"))
    tables_before = sum(1 for _, table, _ in references if table == "DimChannel")
    columns = rename_column_references(document, "DimStore", "StoreName", "Studio Store Name")
    tables = rename_table_references(document, "DimChannel", "Studio Channel")
    after = field_references(document)
    left = sum(1 for _, t, n in after if t == "DimChannel" or (t, n) == ("DimStore", "StoreName"))
    document.commit()
    target = probe.work / "renamed-report.pbix"
    package.write(target)
    return {
        "column_references_before": columns_before,
        "table_references_before": tables_before,
        "rewritten": columns + tables,
        "left": left,
        "file": target.name,
    }


# --- the run -------------------------------------------------------------------------------------

SAVE_STEP = "save, close and read the saved model back"
UNDO_STEP = "undo everything and compare with the original"


def verified_ops(steps: list[Step]) -> list[str]:
    """The kinds outside Microsoft's list whose every step passed, when the save-and-read-back
    and the undo passed too (else none: a change Desktop does not keep is not verified)."""
    by_name = {step.name: step for step in steps}
    if any(
        by_name.get(name) is None or by_name[name].status != "passed"
        for name in (SAVE_STEP, UNDO_STEP)
    ):
        return []
    kinds: dict[str, bool] = {}
    for step in steps:
        for kind in step.covers:
            kinds[kind] = kinds.get(kind, True) and step.status == "passed"
    return sorted(kind for kind, ok in kinds.items() if ok)


def run_steps(
    probe: Probe, copy: Path, original_facts: dict[str, Any], *, screenshot: bool
) -> None:
    controller, work = probe.controller, probe.work
    instance = probe.open(copy)
    snapshot_before = controller.model_snapshot(instance)
    for batch in plan_batches(snapshot_before):

        def act(step: Step, batch: Batch = batch) -> None:
            apply_batch(probe, instance, batch, step)

        probe.run(batch.name, act, covers=batch.covers)

    def save_and_check(step: Step) -> None:
        controller.save(instance)
        if not probe.finish(instance):
            raise RuntimeError("Power BI Desktop could not be closed after the save")
        facts = pbix_extract.read_model_facts(copy)
        shutil.copyfile(copy, work / "changed.pbix")
        # The whole run as one batch: names are carried through the later renames.
        everything = [op for operations in probe.batches for op in operations]
        problems, unchecked = saved_model_problems(everything, facts)
        measures = {m.get("name"): m.get("expression") for m in facts.get("measures", [])}
        fixed = [n for n, e in measures.items() if isinstance(e, str) and "Studio Channel" in e]
        stale = [n for n, e in measures.items() if isinstance(e, str) and "DimChannel" in e]
        step.expected = "the saved model has every batch; no formula still names DimChannel"
        step.observed = {
            "problems": problems,
            "unchecked": unchecked,
            "measures_rewritten_for_the_rename": fixed,
            "measures_still_naming_the_old_table": stale,
            "shapes": shape_verdicts(facts),
        }
        step.status = "passed" if not problems and not stale else "failed"

    probe.run(SAVE_STEP, save_and_check)

    def report_half(step: Step) -> None:
        step.expected = "every report reference to DimStore[StoreName] and DimChannel follows"
        counts = rewrite_report(probe, work / "changed.pbix")
        step.observed = counts
        if screenshot:
            renamed = work / counts["file"]
            reopened = probe.open(renamed)
            shot = work / "renamed-report-page.png"
            document = PbirDocument(PbixPackage(renamed))
            controller.screenshot(reopened, next(iter(document.pages)), shot)
            step.observed["screenshot"] = shot.name
            probe.finish(reopened)
        step.status = "passed" if counts["left"] == 0 and counts["rewritten"] > 0 else "failed"

    probe.run("report half of the renames", report_half)

    def undo_everything(step: Step) -> None:
        step.expected = (
            "after reopening the calculated column still has values; after the undo the live "
            "model and the saved facts equal the original's"
        )
        reopened = probe.open(copy)
        problems = check_not_blank(FACT, "Studio Margin")(probe, reopened, step)
        controller.undo_model_operations(reopened, probe.applied)
        live = controller.model_snapshot(reopened)
        # Desktop keeps its own bookkeeping in model annotations (query order, tooling, version)
        # and may update it on any save; those are recorded, not compared.
        annotations = (
            live.get("model", {}).pop("annotations", None),
            snapshot_before.get("model", {}).pop("annotations", None),
        )
        step.observed["model_annotations_after_and_before"] = annotations
        same_live = normalized(live) == normalized(snapshot_before)
        step.observed["live_model_back"] = same_live
        if not same_live:
            step.observed["live_differences"] = [
                t["name"]
                for t in live["tables"]
                if normalized(t) != normalized(table_of(snapshot_before, t["name"]))
            ] + [
                section
                for section in ("relationships", "roles", "expressions", "model")
                if normalized(live.get(section)) != normalized(snapshot_before.get(section))
            ]
        controller.save(reopened)
        if not probe.finish(reopened):
            raise RuntimeError("Power BI Desktop could not be closed after the undo")
        facts = pbix_extract.read_model_facts(copy)
        sections = sorted(k for k in original_facts if k not in _VOLATILE)
        different = [
            k for k in sections if normalized(facts.get(k)) != normalized(original_facts.get(k))
        ]
        step.observed["saved_sections_different"] = different
        step.observed["problems"] = problems
        step.status = "passed" if same_live and not different and not problems else "failed"

    probe.run(UNDO_STEP, undo_everything)


# --- main ----------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument(
        "--pbix", type=Path, default=DEFAULT_PBIX, help="the Agents dashboard (only read)"
    )
    parser.add_argument(
        "--work-dir", type=Path, default=None, help="where the copies go (default: temp)"
    )
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).parent / "evidence", help="receipt folder"
    )
    parser.add_argument(
        "--bridge", nargs="+", default=None, help="bridge argv, e.g. node.exe index.js"
    )
    parser.add_argument("--pbi", default=None, help="pbi-cli executable (pbi.exe)")
    parser.add_argument("--desktop-exe", default=None, help="PBIDesktop.exe")
    parser.add_argument(
        "--screenshot", action="store_true", help="capture the renamed report's page 1"
    )
    return parser.parse_args(argv)


def tools(args: argparse.Namespace) -> tuple[list[str], str, str]:
    bridge = list(args.bridge) if args.bridge else []
    if not bridge:
        found = probe_tool("powerbi-desktop")
        bridge = bridge_argv(found.path) if found.available and found.path else []
    pbi = args.pbi or (probe_pbi_cli().path or "")
    desktop = args.desktop_exe or (probe_powerbi_desktop().path or "")
    return bridge, pbi, desktop


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    bridge, pbi, desktop_exe = tools(args)
    if not bridge or not pbi or not desktop_exe or not args.pbix.is_file():
        print(
            f"Missing: bridge={bridge!r} pbi={pbi!r} desktop={desktop_exe!r} pbix={args.pbix}",
            file=sys.stderr,
        )
        return 2
    controller = DesktopController(bridge, pbi, desktop_exe)
    problem = controller.model_runner_problem()
    if problem is not None:
        print(f"Studio's model tool cannot run: {problem}", file=sys.stderr)
        return 2
    if args.work_dir is not None and args.work_dir.resolve().is_relative_to(REPO):
        print(
            "The work folder holds copies of the report and its data: choose one outside the "
            f"repository ({REPO}).",
            file=sys.stderr,
        )
        return 2
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="studio-model-probe-"))
    work.mkdir(parents=True, exist_ok=True)
    probe = Probe(controller, work)
    started = datetime.now(UTC)
    copy = work / "probe.pbix"
    shutil.copyfile(args.pbix, copy)
    probe.log(f"copy: {copy} (the source file is only read)")
    original_facts = pbix_extract.read_model_facts(copy)
    shutil.copyfile(copy, work / "original.pbix")
    try:
        run_steps(probe, copy, original_facts, screenshot=args.screenshot)
    finally:
        # However the probe ends, the scratch Desktop it opened is saved and closed, never
        # killed (live run 1 left one open).
        probe.finish_all()

    version = controller.version()
    passed = all(step.status == "passed" for step in probe.steps)
    receipt = {
        "kind": RECEIPT_KIND,
        "desktop_version": version,
        "started": started.isoformat(),
        "finished": datetime.now(UTC).isoformat(),
        "passed": passed,
        "verified_ops": verified_ops(probe.steps),
        "outside_support_kinds": sorted(OUTSIDE_SUPPORT),
        "unverified_shapes": sorted(UNVERIFIED_SHAPES),
        "steps": [
            {
                "name": s.name,
                "status": s.status,
                "seconds": s.seconds,
                "covers": list(s.covers),
                "operations": s.operations,
                "expected": s.expected,
                "observed": redacted(s.observed),
                "error": redacted(s.error),
            }
            for s in probe.steps
        ],
        "left_open": [{"pid": item["pid"]} for item in probe.left_open],
        "platform": sys.platform,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"model-probe-{version or 'unknown'}.json"
    path.write_text(json.dumps(receipt, indent=2, default=str), encoding="utf-8")
    print()
    for step in probe.steps:
        print(f"  {step.status.upper():8} {step.name}")
    print(f"\nreceipt: {path}\npassed: {passed}")
    print(f"work folder (holds the report's data, keep it out of git): {work}")
    if probe.left_open:
        print(
            "\nPower BI Desktop windows this probe opened that are still open (close them by hand):"
        )
        for item in probe.left_open:
            print(f"  - pid {item['pid']} on {item['file']}: {item['reason']}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
