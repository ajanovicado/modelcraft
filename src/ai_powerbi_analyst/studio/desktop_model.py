"""Model changes through Studio's TOM runner: the Python side of ``scripts/studio_tom_runner.py``.

``pbi`` (pbi-cli 3.11.1) exposes only part of the model on its command line (no column properties,
no cross-filter, no role filters, no hierarchy levels, no partition M), but it ships Microsoft's
Tabular Object Model and pythonnet in its own environment. Studio runs its own runner script with
that environment's Python, and uses pbi-cli only to load the libraries. This module:

- finds that Python next to the pinned ``pbi.exe`` (a uv tool: the launcher in ``.local\\bin`` names
  the tool's own ``python.exe``) and pins it: the pbi-cli-tool distribution version and the SHA-256
  of the libraries and loader the runner uses must be the ones Studio was checked with;
- turns typed operations into the runner's generic changes, against a snapshot of the live model
  taken first: exact spellings, the formulas a column or table rename must rewrite (measures,
  calculated columns and tables, role filters, calculation items, all in the same save as the
  rename), and a test query per changed object;
- runs the runner (no shell, a timeout, JSON on stdin, JSON lines back) and reads its answer, and
  when the runner never answers, sends the inverse changes it had already printed.

Measures keep going through ``pbi measure`` when a batch has nothing else (that path is covered by
the Desktop contract receipt and the live battery); a batch with any other model change sends every
model operation, measures included, through the runner, because one save with one rollback is more
reliable than two tools with separate undo paths.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .pbir_document import DocumentError
from .report_edit_model import (
    AUTO_DATE_TIME_REFUSAL,
    CULTURE_REFUSAL,
    DATA_CATEGORIES,
    DATA_TYPES,
    SUMMARIZE_BY,
    CreateCalculatedColumn,
    CreateCalculatedTable,
    CreateCalculationGroup,
    CreateFieldParameter,
    CreateHierarchy,
    CreateRelationship,
    CreateRole,
    DeleteCalculationItem,
    DeleteColumn,
    DeleteHierarchy,
    DeleteNamedExpression,
    DeleteRelationship,
    DeleteRole,
    DeleteTable,
    MarkDateTable,
    ModelStructure,
    ParameterField,
    ParameterProblem,
    RefreshData,
    RenameColumn,
    RenameTable,
    SetCalculationItem,
    SetColumnProperties,
    SetModelOptions,
    SetNamedExpression,
    SetQueryParameter,
    SetTableProperties,
    SetTableQuery,
    UpdateCalculationGroup,
    UpdateFieldParameter,
    UpdateHierarchy,
    UpdateRelationship,
    UpdateRole,
    refresh_policy,
    relationship_settings,
    rename_dax_column,
    rename_dax_table,
    set_parameter_value,
)
from .report_edit_ops import CreateMeasure, DeleteMeasure, RenameMeasure, UpdateMeasure
from .report_refs import _tokenize, dax_references

LOGGER = logging.getLogger(__name__)


class ModelChangeError(RuntimeError):
    """Base of this module's failures; ``desktop_session`` re-raises them as its own errors."""


class ModelRunnerUnavailable(ModelChangeError):
    """The runner's environment is missing or is not the one Studio was checked with."""


class ModelChangeFailed(ModelChangeError):
    """Power BI did not keep the change. ``rolled_back`` is False when undoing it failed too
    (the model in Desktop may hold part of it: never save it)."""

    def __init__(self, message: str, *, stage: str = "", rolled_back: bool = True) -> None:
        super().__init__(message)
        self.stage = stage
        self.rolled_back = rolled_back


class ModelRefreshFailed(ModelChangeFailed):
    """The data could not be reloaded (a source that cannot be reached, typically) and the batch
    was undone; ``reason`` is Power BI's own message."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message, stage="refresh")
        self.reason = reason


class ModelOutcomeUnknown(ModelChangeFailed):
    """The runner never answered; Studio sent the inverse of what it reported applying."""


# --- the pinned environment ---------------------------------------------------------------------

PBI_CLI_VERSION = "3.11.1"
# SHA-256 of what the runner loads from pbi-cli's environment, read from the pinned pbi-cli-tool
# 3.11.1 on 2026-09-29 (Microsoft.AnalysisServices.Tabular 19.84.1.0). Paths are relative to
# site-packages/pbi_cli.
PINNED_FILES: dict[str, str] = {
    "dlls/Microsoft.AnalysisServices.Tabular.dll": (
        "e5d5e814a3f385bd3ffcdde385c7700958d4771303f99120da49e22c37bf1d5f"
    ),
    "dlls/Microsoft.AnalysisServices.Core.dll": (
        "62cd242cc2bb5bd8784065e1d38da8714fb2a62587a9991759761109b6e6cf4b"
    ),
    "dlls/Microsoft.AnalysisServices.dll": (
        "9bcb91e544cbaec7580dbc9ed7025c9c66fd80983d0334f641dc042548572873"
    ),
    "dlls/Microsoft.AnalysisServices.Tabular.Json.dll": (
        "ab388b2cec8fe794e129f6d1c1c801c3b7de26e1c616188c3177d99d04798c70"
    ),
    "dlls/Microsoft.AnalysisServices.AdomdClient.dll": (
        "92b4eda6672d82a2cac8ecc05a570e84195797bcaed362288e3a36d41aafa3cb"
    ),
    "core/dotnet_loader.py": "fcf9534e00aa5520f8bab690de7e9c660dd1b84f60ff6d06362fe76fa3fd768c",
}
RUNNER_NAME = "studio_tom_runner.py"
PROTOCOL = 1
SNAPSHOT_TIMEOUT = 120.0
APPLY_TIMEOUT = 600.0  # a refresh of a large table can take minutes
MAX_PROBES = 25
MAX_VIEWS = 10  # role views (two informational probes each) per batch
_PYTHON_IN_LAUNCHER = re.compile(rb"[A-Za-z]:\\[^\x00-\x1f\"<>|?*]{1,400}?\\Scripts\\python\.exe")
_hash_cache: dict[tuple[str, int, int], str] = {}


@dataclass(frozen=True)
class RunnerEnvironment:
    python: Path
    script: Path


def default_runner_script() -> Path:
    """The runner script: next to the sources in a checkout, in the bundle when frozen."""
    bundled = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundled:
        return Path(str(bundled)) / "scripts" / RUNNER_NAME
    return Path(__file__).resolve().parents[3] / "scripts" / RUNNER_NAME


def _has_pbi_cli(python: Path) -> bool:
    return (_site_packages(python) / "pbi_cli" / "core" / "dotnet_loader.py").is_file()


def _site_packages(python: Path) -> Path:
    return python.parent.parent / "Lib" / "site-packages"


def python_candidates(pbi: str) -> list[Path]:
    """Where pbi-cli's Python can be, most direct first: beside ``pbi.exe`` (the tool's Scripts
    folder), the interpreter a uv launcher names, then uv's default tool folder."""
    found: list[Path] = []
    if pbi:
        exe = Path(pbi)
        found.append(exe.parent / "python.exe")
        try:
            data = exe.read_bytes()[-2_000_000:] if exe.is_file() else b""
        except OSError:
            data = b""
        for match in _PYTHON_IN_LAUNCHER.finditer(data):
            try:
                found.append(Path(match.group().decode("utf-8")))
            except UnicodeDecodeError:
                continue
    tool_dir = os.environ.get("UV_TOOL_DIR")
    if tool_dir:
        found.append(Path(tool_dir) / "pbi-cli-tool" / "Scripts" / "python.exe")
    roaming = os.environ.get("APPDATA")
    if roaming:
        found.append(Path(roaming) / "uv" / "tools" / "pbi-cli-tool" / "Scripts" / "python.exe")
    unique: list[Path] = []
    for candidate in found:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _hash_cache:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        _hash_cache[key] = digest.hexdigest()
    return _hash_cache[key]


def _distribution_version(site: Path) -> str | None:
    for metadata in site.glob("pbi_cli_tool-*.dist-info/METADATA"):
        try:
            for line in metadata.read_text(encoding="utf-8").splitlines():
                if line.startswith("Version: "):
                    return line.removeprefix("Version: ").strip()
        except OSError:
            continue
    return None


def runner_environment(
    pbi: str,
    *,
    script: Path | None = None,
    pinned: Mapping[str, str] = PINNED_FILES,
    version: str = PBI_CLI_VERSION,
) -> RunnerEnvironment:
    """The pinned environment to run the runner in, or ModelRunnerUnavailable saying why not."""
    runner = script or default_runner_script()
    if not runner.is_file():
        raise ModelRunnerUnavailable("Studio's model tool is missing from this installation")
    python = next((c for c in python_candidates(pbi) if c.is_file() and _has_pbi_cli(c)), None)
    if python is None:
        raise ModelRunnerUnavailable(
            "Studio could not find the Power BI command-line tool's own Python, which it needs "
            "for changes to columns, tables and relationships"
        )
    site = _site_packages(python)
    found = _distribution_version(site)
    if found != version:
        raise ModelRunnerUnavailable(
            f"The Power BI command-line tool is version {found or 'unknown'}; Studio was checked "
            f"with {version}"
        )
    for relative, expected in pinned.items():
        path = site / "pbi_cli" / relative
        try:
            actual = _sha256(path)
        except OSError:
            raise ModelRunnerUnavailable(
                f"The Power BI command-line tool is incomplete ({relative} is missing)"
            ) from None
        if actual != expected:
            raise ModelRunnerUnavailable(
                "The Power BI command-line tool's model libraries changed since Studio was "
                f"checked with them ({relative})"
            )
    return RunnerEnvironment(python, runner)


# --- running it ---------------------------------------------------------------------------------


@dataclass
class RunnerAnswer:
    """What the runner printed: its events and, unless it never finished, its result.
    ``finished`` is False only when the process was stopped at the timeout; ``exit_code`` is
    its exit code when it ended by itself; ``stderr`` is the end of what it wrote there (a
    traceback, for the local log only: it is never shown to the model or the user as is)."""

    events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    result: dict[str, Any] | None = None
    finished: bool = True
    exit_code: int | None = None
    stderr: str = ""

    @property
    def saved(self) -> bool:
        """The runner reported that its changes reached Desktop's model."""
        return any(event.get("event") == "saved" for event in self.events)

    def why_no_result(self) -> str:
        """Plain words for a run without a result: a timeout is not a crash."""
        if not self.finished:
            return "Power BI Desktop's model did not answer in time"
        code = "" if self.exit_code is None else f" (exit {self.exit_code})"
        return f"Studio's model tool stopped unexpectedly{code}"


STDERR_TAIL = 2000


# A runner takes the request and a timeout in seconds; tests pass one that runs the runner module
# in-process over a fake object model.
Runner = Callable[[dict[str, Any], float], RunnerAnswer]


def parse_events(text: str) -> RunnerAnswer:
    answer = RunnerAnswer()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a line cut short when the process ended
        if not isinstance(event, dict):
            continue
        if event.get("event") == "result":
            answer.result = event
        else:
            answer.events.append(event)
    return answer


def _decoded(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else ""


def runner_argv(environment: RunnerEnvironment) -> list[str]:
    """``-I`` keeps the user's Python settings out; it also ignores PYTHONIOENCODING, so UTF-8
    mode is asked for on the command line (``-X utf8`` is honoured under ``-I``); the runner
    reconfigures its pipes to UTF-8 as well."""
    return [str(environment.python), "-I", "-X", "utf8", str(environment.script)]


def subprocess_runner(environment: RunnerEnvironment) -> Runner:
    def run(request: dict[str, Any], timeout: float) -> RunnerAnswer:
        try:
            completed = subprocess.run(  # noqa: S603 - pinned interpreter and Studio's own script
                runner_argv(environment),
                input=json.dumps(request),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            answer = parse_events(_decoded(error.stdout))
            answer.finished = False
            answer.stderr = _decoded(error.stderr)[-STDERR_TAIL:]
            return answer
        except OSError as error:
            raise ModelRunnerUnavailable("Studio's model tool could not be started") from error
        answer = parse_events(completed.stdout or "")
        answer.exit_code = completed.returncode
        answer.stderr = (completed.stderr or "")[-STDERR_TAIL:]
        return answer

    return run


# --- the live model, as the snapshot describes it -------------------------------------------------


def _cf(text: str) -> str:
    return text.casefold()


class _Live:
    """The live model's names and formulas while a batch is planned: every planned change updates
    it, so a rename's fix-ups see what the earlier steps of the same batch did."""

    def __init__(self, snapshot: Mapping[str, Any]) -> None:
        self.tables: dict[str, str] = {}
        self.kinds: dict[str, str] = {}
        self.columns: dict[tuple[str, str], str] = {}  # -> own spelling
        self.calculated_columns: dict[tuple[str, str], str] = {}  # -> DAX
        self.measures: dict[tuple[str, str], str] = {}  # (table, name) -> DAX
        self.measure_names: dict[tuple[str, str], str] = {}
        # A measure's other DAX: its dynamic format string, detail rows and KPI expressions.
        self.measure_extras: dict[tuple[str, str], dict[str, Any]] = {}
        self.calculated_tables: dict[str, str] = {}  # table -> DAX
        self.roles: dict[str, dict[str, str]] = {}
        self.role_names: dict[str, str] = {}
        self.items: dict[tuple[str, str], tuple[str, str, str]] = {}  # -> (group, item, DAX)
        self.item_formats: dict[tuple[str, str], str] = {}  # -> format string DAX
        self.expressions: dict[str, str] = {}  # name -> M
        self.expression_names: dict[str, str] = {}
        self.settings: dict[str, Any] = dict(snapshot.get("model") or {})
        self.structure = ModelStructure.from_facts(snapshot)
        for table in snapshot.get("tables") or []:
            if not isinstance(table, Mapping):
                continue
            name = str(table.get("name") or "")
            self.tables[_cf(name)] = name
            self.kinds[_cf(name)] = (
                "fieldParameter" if table.get("fieldParameter") else str(table.get("kind") or "")
            )
            for column in table.get("columns") or []:
                self.columns[(_cf(name), _cf(column["name"]))] = column["name"]
                if column.get("type") == "Calculated":
                    self.calculated_columns[(_cf(name), _cf(column["name"]))] = str(
                        column.get("expression") or ""
                    )
            for measure in table.get("measures") or []:
                key = (_cf(name), _cf(measure["name"]))
                self.measures[key] = str(measure.get("expression") or "")
                self.measure_names[key] = measure["name"]
                kpi = measure.get("kpi")
                self.measure_extras[key] = {
                    "formatStringExpression": str(measure.get("formatStringExpression") or ""),
                    "detailRowsExpression": str(measure.get("detailRowsExpression") or ""),
                    "kpi": dict(kpi) if isinstance(kpi, Mapping) else None,
                }
            partitions = table.get("partitions") or []
            calculated = [p for p in partitions if p.get("sourceType") == "Calculated"]
            if len(partitions) == 1 and calculated:
                self.calculated_tables[_cf(name)] = str(calculated[0].get("expression") or "")
            group = table.get("calculationGroup")
            if isinstance(group, Mapping):
                for item in group.get("items") or []:
                    self.items[(_cf(name), _cf(item["name"]))] = (
                        name,
                        item["name"],
                        str(item.get("expression") or ""),
                    )
                    self.item_formats[(_cf(name), _cf(item["name"]))] = str(
                        item.get("formatStringExpression") or ""
                    )
        for role in snapshot.get("roles") or []:
            self.roles[_cf(role["name"])] = dict(role.get("tablePermissions") or {})
            self.role_names[_cf(role["name"])] = role["name"]
        for expression in snapshot.get("expressions") or []:
            self.expressions[_cf(expression["name"])] = str(expression.get("expression") or "")
            self.expression_names[_cf(expression["name"])] = expression["name"]

    # --- spellings ------------------------------------------------------------------------

    def table(self, name: str) -> str:
        found = self.tables.get(_cf(name))
        if found is None:
            raise ModelChangeFailed(f"The model in Power BI Desktop has no table '{name}'")
        return found

    def column(self, table: str, name: str) -> str:
        found = self.columns.get((_cf(table), _cf(name)))
        if found is None:
            raise ModelChangeFailed(
                f"The table '{table}' has no column '{name}' in Power BI Desktop"
            )
        return found

    def measure_table(self, name: str) -> str | None:
        for (table, measure), _ in self.measures.items():
            if measure == _cf(name):
                return self.tables.get(table, table)
        return None

    def measure_spelling(self, name: str) -> str:
        return next((m for (_, key), m in self.measure_names.items() if key == _cf(name)), name)

    def measure_keys(self) -> frozenset[str]:
        return frozenset(key for _, key in self.measures)

    def column_names(self, table: str | None) -> frozenset[str]:
        """The columns of ``table`` (casefolded); empty for a formula outside any table."""
        if table is None:
            return frozenset()
        return frozenset(column for (home, column) in self.columns if home == _cf(table))

    # --- keeping names current ------------------------------------------------------------

    def rename_table(self, old: str, new: str) -> None:
        old_key, new_key = _cf(old), _cf(new)
        self.tables.pop(old_key, None)
        self.tables[new_key] = new
        self.kinds[new_key] = self.kinds.pop(old_key, "")
        self.structure.relationships = [
            replace(
                r,
                from_table=new if _cf(r.from_table) == old_key else r.from_table,
                to_table=new if _cf(r.to_table) == old_key else r.to_table,
            )
            for r in self.structure.relationships
        ]

        def move(mapping: dict[Any, Any]) -> None:
            for key in [k for k in mapping if isinstance(k, tuple) and k[0] == old_key]:
                mapping[(new_key, key[1])] = mapping.pop(key)
            if old_key in mapping:
                mapping[new_key] = mapping.pop(old_key)

        for mapping in (
            self.columns,
            self.calculated_columns,
            self.measures,
            self.measure_names,
            self.measure_extras,
            self.calculated_tables,
            self.item_formats,
        ):
            move(mapping)
        for key, (_, item, text) in list(self.items.items()):
            if key[0] == old_key:
                del self.items[key]
                self.items[(new_key, key[1])] = (new, item, text)
        for filters in self.roles.values():
            for table in [t for t in filters if _cf(t) == old_key]:
                filters[new] = filters.pop(table)

    def rename_column(self, table: str, old: str, new: str) -> None:
        key = (_cf(table), _cf(old))
        self.columns.pop(key, None)
        self.columns[(_cf(table), _cf(new))] = new
        self.structure.relationships = [
            replace(
                r,
                from_column=new
                if (_cf(r.from_table), _cf(r.from_column)) == key
                else r.from_column,
                to_column=new if (_cf(r.to_table), _cf(r.to_column)) == key else r.to_column,
            )
            for r in self.structure.relationships
        ]
        if key in self.calculated_columns:
            self.calculated_columns[(_cf(table), _cf(new))] = self.calculated_columns.pop(key)


# --- turning operations into runner changes -------------------------------------------------------

_CARDINALITY = {
    "many_to_one": ("Many", "One"),
    "one_to_one": ("One", "One"),
    "many_to_many": ("Many", "Many"),
}
_CROSS_FILTER = {"single": "OneDirection", "both": "BothDirections"}


def _quote(table: str) -> str:
    return "'" + table.replace("'", "''") + "'"


def _column_ref(table: str, column: str) -> str:
    return _quote(table) + "[" + column.replace("]", "]]") + "]"


def _measure_ref(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


@dataclass
class ModelPlan:
    """The runner request of a batch, and what Studio tells the user about it."""

    changes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    refresh: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    probes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    fixups: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])
    on_refresh_failure: str = "rollback"
    asks_on_refresh_failure: bool = False
    # (role, table, probe id of everyone's row count, probe id of the role's)
    role_views: list[tuple[str, str, str, str]] = field(
        default_factory=list[tuple[str, str, str, str]]
    )


class _Planner:
    def __init__(self, snapshot: Mapping[str, Any]) -> None:
        self.live = _Live(snapshot)
        self.plan = ModelPlan()
        self._probed: set[str] = set()
        self._viewed_roles: set[str] = set()
        annotations = self.live.settings.get("annotations")
        order = None
        if isinstance(annotations, Mapping):
            order = annotations.get(QUERY_ORDER_ANNOTATION)
        self._query_names: list[str] | None = None
        try:
            parsed = json.loads(order) if isinstance(order, str) else None
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(n, str) for n in parsed):
            self._query_names = list(parsed)
        self._query_names_before = list(self._query_names or [])

    def _add(self, kind: str, action: str, path: dict[str, Any], **extra: Any) -> None:
        change: dict[str, Any] = {
            "id": f"c{len(self.plan.changes) + 1}",
            "kind": kind,
            "action": action,
            "path": path,
        }
        change.update({k: v for k, v in extra.items() if v is not None})
        self.plan.changes.append(change)

    def _probe(self, label: str, query: str) -> None:
        if query in self._probed or len(self.plan.probes) >= MAX_PROBES:
            return
        self._probed.add(query)
        self.plan.probes.append(
            {"id": f"p{len(self.plan.probes) + 1}", "label": label, "query": query}
        )

    def _probe_measure(self, name: str) -> None:
        self._probe(f"The check of [{name}]", f'EVALUATE ROW("value", {_measure_ref(name)})')

    def _probe_column(self, table: str, column: str) -> None:
        self._probe(
            f"The check of {table}[{column}]",
            f'EVALUATE TOPN(1, SELECTCOLUMNS({_quote(table)}, "value", '
            f"{_column_ref(table, column)}))",
        )

    def _probe_table(self, table: str) -> None:
        self._probe(f"The check of the table '{table}'", f"EVALUATE TOPN(1, {_quote(table)})")

    # --- fix-ups for renames ------------------------------------------------------------------

    def _measure_fixup(
        self, key: tuple[str, str], rename: Callable[[str, str | None], tuple[str, int]]
    ) -> None:
        """One measure's formula, dynamic format string, detail rows and KPI expressions."""
        live = self.live
        props: dict[str, Any] = {}
        new, count = rename(live.measures[key], None)
        if count:
            props["expression"] = new
            live.measures[key] = new
        extras = live.measure_extras.setdefault(
            key, {"formatStringExpression": "", "detailRowsExpression": "", "kpi": None}
        )
        for field_name in ("formatStringExpression", "detailRowsExpression"):
            text = str(extras.get(field_name) or "")
            changed, count = rename(text, None) if text else (text, 0)
            if count:
                props[field_name] = changed
                extras[field_name] = changed
        kpi = extras.get("kpi")
        if isinstance(kpi, Mapping):
            rewritten = dict(kpi)
            for field_name in ("targetExpression", "statusExpression", "trendExpression"):
                text = str(kpi.get(field_name) or "")
                changed, count = rename(text, None) if text else (text, 0)
                if count:
                    rewritten[field_name] = changed
            if rewritten != kpi:
                props["kpi"] = rewritten
                extras["kpi"] = rewritten
        if not props:
            return
        table = live.tables.get(key[0], key[0])
        name = live.measure_names[key]
        self._add("measure", "update", {"table": table, "name": name}, props=props)
        self.plan.fixups.append(f"the measure [{name}]")
        self._probe_measure(name)

    def _rewrite(self, rename: Callable[[str, str | None], tuple[str, int]]) -> None:
        """Rewrite every formula the rename changes; ``rename(text, own_table)``. Measures
        include their dynamic format strings, detail rows and KPI expressions; calculation items
        their format strings."""
        live = self.live
        for key in list(live.measures):
            self._measure_fixup(key, rename)
        for key, text in list(live.calculated_columns.items()):
            table = live.tables.get(key[0], key[0])
            new, count = rename(text, table)
            if count:
                name = live.columns[key]
                live.calculated_columns[key] = new
                self._add(
                    "column", "update", {"table": table, "name": name}, props={"expression": new}
                )
                self.plan.fixups.append(f"the calculated column {table}[{name}]")
        for table_key, text in list(live.calculated_tables.items()):
            new, count = rename(text, None)
            if count:
                table = live.tables.get(table_key, table_key)
                live.calculated_tables[table_key] = new
                self._add("table", "update", {"name": table}, props={"expression": new})
                self.plan.fixups.append(f"the calculated table '{table}'")
        for role_key, filters in live.roles.items():
            changed: dict[str, str] = {}
            for table, text in filters.items():
                new, count = rename(text, table)
                if count:
                    changed[table] = new
            if changed:
                filters.update(changed)
                role = live.role_names[role_key]
                self._add("role", "update", {"name": role}, props={"tablePermissions": changed})
                self.plan.fixups.append(f"the filters of the role '{role}'")
        for item_key, (group, item, text) in list(live.items.items()):
            item_props: dict[str, Any] = {}
            new, count = rename(text, None)
            if count:
                live.items[item_key] = (group, item, new)
                item_props["expression"] = new
            format_text = live.item_formats.get(item_key, "")
            new_format, count = rename(format_text, None) if format_text else (format_text, 0)
            if count:
                live.item_formats[item_key] = new_format
                item_props["formatStringExpression"] = new_format
            if item_props:
                self._add(
                    "calculationItem",
                    "update",
                    {"group": group, "name": item},
                    props=item_props,
                )
                self.plan.fixups.append(f"the calculation item '{item}' of '{group}'")

    # --- one operation -------------------------------------------------------------------------

    def add(self, op: Any) -> None:
        """Plan one operation (one branch per operation class, flat on purpose)."""
        live = self.live
        table: str
        name: str
        key: tuple[str, str]
        props: dict[str, Any]
        if isinstance(op, CreateMeasure):
            table = live.table(op.table)
            props = {"expression": op.expression}
            if op.format_string:
                props["formatString"] = op.format_string
            if op.folder:
                props["displayFolder"] = op.folder
            if op.description:
                props["description"] = op.description
            self._add("measure", "create", {"table": table, "name": op.name}, props=props)
            live.measures[(_cf(table), _cf(op.name))] = op.expression
            live.measure_names[(_cf(table), _cf(op.name))] = op.name
            self._probe_measure(op.name)
        elif isinstance(op, UpdateMeasure):
            table = self._measure_home(op.name)
            name = live.measure_spelling(op.name)
            props = _props(
                ("expression", op.expression),
                ("formatString", op.format_string),
                ("displayFolder", op.folder),
                ("description", op.description),
                ("isHidden", op.hidden),
            )
            self._add("measure", "update", {"table": table, "name": name}, props=props)
            if op.expression is not None:
                live.measures[(_cf(table), _cf(name))] = op.expression
            self._probe_measure(name)
        elif isinstance(op, RenameMeasure):
            table = self._measure_home(op.name)
            name = live.measure_spelling(op.name)
            self._add("measure", "rename", {"table": table, "name": name}, newName=op.new_name)
            key = (_cf(table), _cf(name))
            new_key = (_cf(table), _cf(op.new_name))
            live.measures[new_key] = live.measures.pop(key, "")
            live.measure_names.pop(key, None)
            live.measure_names[new_key] = op.new_name
            if key in live.measure_extras:
                live.measure_extras[new_key] = live.measure_extras.pop(key)
            # The formulas that name the measure follow it in the same save (spec review #9).
            self._rewrite(
                lambda text, own: _rename_measure_dax(
                    text, table, name, op.new_name, own_columns=live.column_names(own)
                )
            )
            self._probe_measure(op.new_name)
        elif isinstance(op, DeleteMeasure):
            table = self._measure_home(op.name)
            name = live.measure_spelling(op.name)
            self._add("measure", "delete", {"table": table, "name": name})
            live.measures.pop((_cf(table), _cf(name)), None)
            live.measure_names.pop((_cf(table), _cf(name)), None)
        elif isinstance(op, CreateCalculatedColumn):
            table = live.table(op.table)
            props = _props(
                ("expression", op.expression),
                ("formatString", op.format_string),
                ("dataType", DATA_TYPES.get(op.data_type or "")),
                ("summarizeBy", SUMMARIZE_BY.get(op.summarize_by or "")),
                ("displayFolder", op.folder),
                ("description", op.description),
                ("isHidden", op.hidden),
            )
            self._add(
                "calculatedColumn",
                "create",
                {"table": table, "name": op.name},
                props=_with_set_by(props),
            )
            live.columns[(_cf(table), _cf(op.name))] = op.name
            live.calculated_columns[(_cf(table), _cf(op.name))] = op.expression
            self._probe_column(table, op.name)
        elif isinstance(op, SetColumnProperties):
            table = live.table(op.table)
            column = live.column(table, op.name)
            props = _props(
                ("formatString", op.format_string),
                ("dataType", DATA_TYPES.get(op.data_type or "")),
                ("sortByColumn", live.column(table, op.sort_by) if op.sort_by else op.sort_by),
                ("dataCategory", DATA_CATEGORIES.get(op.data_category or "")),
                ("summarizeBy", SUMMARIZE_BY.get(op.summarize_by or "")),
                ("displayFolder", op.folder),
                ("description", op.description),
                ("isHidden", op.hidden),
                ("isKey", op.key),
                ("expression", op.expression),
            )
            self._add(
                "column", "update", {"table": table, "name": column}, props=_with_set_by(props)
            )
            if op.expression is not None:
                live.calculated_columns[(_cf(table), _cf(column))] = op.expression
            self._probe_column(table, column)
        elif isinstance(op, RenameColumn):
            table = live.table(op.table)
            column = live.column(table, op.name)
            self._add("column", "rename", {"table": table, "name": column}, newName=op.new_name)
            live.rename_column(table, column, op.new_name)
            measures = live.measure_keys()
            self._rewrite(
                lambda text, own: rename_dax_column(
                    text, table, column, op.new_name, own_table=own, measures=measures
                )
            )
            self._probe_column(table, op.new_name)
        elif isinstance(op, DeleteColumn):
            table = live.table(op.table)
            column = live.column(table, op.name)
            self._add("column", "delete", {"table": table, "name": column})
            live.columns.pop((_cf(table), _cf(column)), None)
            live.calculated_columns.pop((_cf(table), _cf(column)), None)
        elif isinstance(op, CreateCalculatedTable):
            props = {"expression": op.expression}
            if op.description:
                props["description"] = op.description
            if op.hidden:
                props["isHidden"] = True
            self._add("calculatedTable", "create", {"name": op.name}, props=props)
            live.tables[_cf(op.name)] = op.name
            live.kinds[_cf(op.name)] = "calculated"
            live.calculated_tables[_cf(op.name)] = op.expression
            self._probe_table(op.name)
        elif isinstance(op, SetTableProperties):
            table = live.table(op.table)
            props = {}
            if op.hidden is not None:
                props["isHidden"] = op.hidden
            if op.description is not None:
                props["description"] = op.description
            if op.expression is not None:
                props["expression"] = op.expression
                live.calculated_tables[_cf(table)] = op.expression
                self._probe_table(table)
            self._add("table", "update", {"name": table}, props=props)
        elif isinstance(op, RenameTable):
            table = live.table(op.name)
            self._add("table", "rename", {"name": table}, newName=op.new_name)
            live.rename_table(table, op.new_name)
            self._rewrite(lambda text, own: rename_dax_table(text, table, op.new_name))
            # A refresh runs after every change of the batch: it names the table's final name.
            for planned in self.plan.refresh:
                if _cf(str(planned.get("table") or "")) == _cf(table):
                    planned["table"] = op.new_name
            self._probe_table(op.new_name)
        elif isinstance(op, DeleteTable):
            table = live.table(op.name)
            only = [
                live.role_names[role_key]
                for role_key, filters in live.roles.items()
                if [t for t, text in filters.items() if text]
                and all(_cf(t) == _cf(table) for t, text in filters.items() if text)
            ]
            if only:
                raise ModelChangeFailed(
                    f"In Power BI Desktop, '{table}' holds the only filter of the role "
                    f"'{only[0]}': without it, people in that role would see every row. Change "
                    "the role's filters or delete the role first"
                )
            for role_key, filters in live.roles.items():
                on = [t for t in filters if _cf(t) == _cf(table)]
                if on:
                    role = live.role_names[role_key]
                    self._add(
                        "role", "update", {"name": role}, props={"tablePermissions": {on[0]: None}}
                    )
                    del filters[on[0]]
                    self.plan.warnings.append(
                        f"The role '{role}' loses its filter on '{table}'; its other filters stay."
                    )
            self._add("table", "delete", {"name": table})
            live.tables.pop(_cf(table), None)
            for mapping in (
                live.columns,
                live.calculated_columns,
                live.measures,
                live.measure_names,
                live.measure_extras,
                live.item_formats,
            ):
                for stale in [k for k in mapping if k[0] == _cf(table)]:
                    del mapping[stale]
            live.calculated_tables.pop(_cf(table), None)
            for stale in [k for k in live.items if k[0] == _cf(table)]:
                del live.items[stale]
        elif isinstance(op, MarkDateTable):
            table = live.table(op.table)
            key_column = live.column(table, op.column) if op.column else None
            self._add("dateTable", "update", {"name": table}, props={"column": key_column})
        elif isinstance(op, CreateRelationship):
            source, target = self._ends(op)
            try:
                fact = relationship_settings(live.structure, source, target, op)
            except DocumentError as error:
                raise ModelChangeFailed(str(error)) from error
            from_end, to_end = _CARDINALITY[op.cardinality]
            props = {
                "fromCardinality": from_end,
                "toCardinality": to_end,
                "crossFilteringBehavior": _CROSS_FILTER[op.cross_filter],
                "securityFilteringBehavior": _SECURITY[op.security_filter],
                "isActive": op.active,
                "relyOnReferentialIntegrity": op.assume_referential_integrity,
            }
            self._add(
                "relationship", "create", self._relationship_path(source, target), props=props
            )
            live.structure.relationships.append(fact)
            self._probe_relationship(source, target)
        elif isinstance(op, UpdateRelationship):
            source, target = self._ends(op)
            existing_relationship = live.structure.relationship(source, target)
            try:
                fact = relationship_settings(live.structure, source, target, op)
            except DocumentError as error:
                raise ModelChangeFailed(str(error)) from error
            if (
                fact.cardinality_known
                and (fact.from_many, fact.to_many) == (False, True)
                and not fact.both
                and (op.cardinality is not None or op.cross_filter == "single")
            ):
                # TOM OneDirection is physically To -> From. Metadata round-trip alone
                # cannot qualify a reversed regular relationship's filter propagation.
                raise ModelChangeFailed(
                    "This edit would leave the stored relationship from One to Many with "
                    "single-direction filtering. Modelcraft cannot verify that orientation. "
                    "Configure it in Power BI, or explicitly request both directions if that "
                    "is the intended filtering."
                )
            props = {}
            if op.cardinality is not None:
                props["fromCardinality"] = "Many" if fact.from_many else "One"
                props["toCardinality"] = "Many" if fact.to_many else "One"
            if op.cross_filter is not None:
                props["crossFilteringBehavior"] = _CROSS_FILTER[op.cross_filter]
            if op.security_filter is not None:
                props["securityFilteringBehavior"] = _SECURITY[op.security_filter]
            if op.active is not None:
                props["isActive"] = op.active
            if op.assume_referential_integrity is not None:
                props["relyOnReferentialIntegrity"] = op.assume_referential_integrity
            self._add(
                "relationship", "update", self._relationship_path(source, target), props=props
            )
            if existing_relationship is not None:
                live.structure.relationships[
                    live.structure.relationships.index(existing_relationship)
                ] = fact
            self._probe_relationship(source, target)
        elif isinstance(op, DeleteRelationship):
            source, target = self._ends(op)
            self._add("relationship", "delete", self._relationship_path(source, target))
            live.structure.relationships = [
                r for r in live.structure.relationships if not r.joins(source, target)
            ]
        elif isinstance(op, CreateHierarchy | UpdateHierarchy):
            table = live.table(op.table)
            levels = None
            if op.levels is not None:
                levels = [
                    {
                        "name": level.name or live.column(table, level.column),
                        "column": live.column(table, level.column),
                    }
                    for level in op.levels
                ]
            if isinstance(op, CreateHierarchy):
                self._add(
                    "hierarchy",
                    "create",
                    {"table": table, "name": op.name},
                    props={"levels": levels},
                )
            else:
                if levels is not None:
                    self._add(
                        "hierarchy",
                        "update",
                        {"table": table, "name": op.name},
                        props={"levels": levels},
                    )
                if op.new_name is not None:
                    self._add(
                        "hierarchy",
                        "rename",
                        {"table": table, "name": op.name},
                        newName=op.new_name,
                    )
            if levels:
                columns = ", ".join(_column_ref(table, level["column"]) for level in levels)
                self._probe(
                    f"The check of the hierarchy '{op.name}'",
                    f"EVALUATE TOPN(1, SUMMARIZECOLUMNS({columns}))",
                )
        elif isinstance(op, DeleteHierarchy):
            table = live.table(op.table)
            self._add("hierarchy", "delete", {"table": table, "name": op.name})
        elif isinstance(op, CreateFieldParameter):
            dax = self._parameter_dax(op.fields)
            names = {"label": op.name, "fields": f"{op.name} Fields", "order": f"{op.name} Order"}
            self._add(
                "fieldParameter",
                "create",
                {"name": op.name},
                props={"expression": dax, "columns": names},
            )
            live.tables[_cf(op.name)] = op.name
            live.kinds[_cf(op.name)] = "fieldParameter"
            live.calculated_tables[_cf(op.name)] = dax
            for column in names.values():
                live.columns[(_cf(op.name), _cf(column))] = column
            self._probe_table(op.name)
        elif isinstance(op, UpdateFieldParameter):
            table = live.table(op.name)
            dax = self._parameter_dax(op.fields)
            self._add("fieldParameter", "update", {"name": table}, props={"expression": dax})
            live.calculated_tables[_cf(table)] = dax
            self._probe_table(table)
        elif isinstance(op, CreateCalculationGroup):
            if not live.settings.get("discourageImplicitMeasures"):
                self._add("model", "update", {}, props={"discourageImplicitMeasures": True})
                live.settings["discourageImplicitMeasures"] = True
            items = [
                {
                    "name": item.name,
                    "expression": item.expression,
                    **(
                        {"formatStringExpression": item.format_string_expression}
                        if item.format_string_expression
                        else {}
                    ),
                }
                for item in op.items
            ]
            props = {"column": op.column, "precedence": op.precedence, "items": items}
            if op.description:
                props["description"] = op.description
            self._add("calculationGroup", "create", {"name": op.name}, props=props)
            live.tables[_cf(op.name)] = op.name
            live.kinds[_cf(op.name)] = "calculationGroup"
            for item in op.items:
                live.items[(_cf(op.name), _cf(item.name))] = (op.name, item.name, item.expression)
                live.item_formats[(_cf(op.name), _cf(item.name))] = (
                    item.format_string_expression or ""
                )
            self._probe_table(op.name)
        elif isinstance(op, SetCalculationItem):
            group = live.table(op.group)
            key = (_cf(group), _cf(op.name))
            props = {}
            if op.expression is not None:
                props["expression"] = op.expression
            if op.format_string_expression is not None:
                props["formatStringExpression"] = op.format_string_expression
            if op.ordinal is not None:
                props["ordinal"] = op.ordinal
            if key in live.items:
                name = live.items[key][1]
                if props:
                    self._add(
                        "calculationItem", "update", {"group": group, "name": name}, props=props
                    )
                if op.new_name is not None:
                    self._add(
                        "calculationItem",
                        "rename",
                        {"group": group, "name": name},
                        newName=op.new_name,
                    )
            else:
                name = op.new_name or op.name
                self._add("calculationItem", "create", {"group": group, "name": name}, props=props)
            expression = (
                op.expression if op.expression is not None else live.items.get(key, ("", "", ""))[2]
            )
            live.items.pop(key, None)
            format_text = live.item_formats.pop(key, "")
            if op.format_string_expression is not None:
                format_text = op.format_string_expression
            final = op.new_name or op.name
            live.items[(_cf(group), _cf(final))] = (group, final, expression)
            live.item_formats[(_cf(group), _cf(final))] = format_text
            self._probe_table(group)
        elif isinstance(op, DeleteCalculationItem):
            group = live.table(op.group)
            self._add("calculationItem", "delete", {"group": group, "name": op.name})
            live.items.pop((_cf(group), _cf(op.name)), None)
            live.item_formats.pop((_cf(group), _cf(op.name)), None)
        elif isinstance(op, UpdateCalculationGroup):
            group = live.table(op.name)
            props = {}
            if op.precedence is not None:
                props["precedence"] = op.precedence
            if op.description is not None:
                props["description"] = op.description
            self._add("calculationGroup", "update", {"name": group}, props=props)
        elif isinstance(op, CreateRole):
            filters = {live.table(f.table): f.expression for f in op.filters if f.expression}
            props = {"tablePermissions": filters}
            if op.description:
                props["description"] = op.description
            self._add("role", "create", {"name": op.name}, props=props)
            live.roles[_cf(op.name)] = dict(filters)
            live.role_names[_cf(op.name)] = op.name
            self._viewed_roles.add(_cf(op.name))
        elif isinstance(op, UpdateRole):
            role = live.role_names.get(_cf(op.name), op.name)
            props = {}
            filters = live.roles.setdefault(_cf(role), {})
            if op.filters is not None:
                props["tablePermissions"] = {
                    live.table(f.table): (f.expression or None) for f in op.filters
                }
                for table_name, text in props["tablePermissions"].items():
                    for known in [t for t in filters if _cf(t) == _cf(table_name)]:
                        del filters[known]
                    if text:
                        filters[table_name] = text
            if op.description is not None:
                props["description"] = op.description
            if props:
                self._add("role", "update", {"name": role}, props=props)
            final = role
            if op.new_name is not None:
                self._add("role", "rename", {"name": role}, newName=op.new_name)
                live.roles[_cf(op.new_name)] = live.roles.pop(_cf(role), {})
                live.role_names.pop(_cf(role), None)
                live.role_names[_cf(op.new_name)] = op.new_name
                self._viewed_roles.discard(_cf(role))
                final = op.new_name
            if op.filters is not None:
                self._viewed_roles.add(_cf(final))
        elif isinstance(op, DeleteRole):
            role = live.role_names.get(_cf(op.name), op.name)
            self._add("role", "delete", {"name": role})
            live.roles.pop(_cf(role), None)
            live.role_names.pop(_cf(role), None)
            self._viewed_roles.discard(_cf(role))
        elif isinstance(op, SetTableQuery):
            table = live.table(op.table)
            self._add("partition", "update", {"table": table}, props={"mExpression": op.expression})
            self._refresh({"table": table})
        elif isinstance(op, SetNamedExpression):
            exists = _cf(op.name) in live.expressions
            props = {"expression": op.expression}
            if op.description is not None:
                props["description"] = op.description
            name = live.expression_names.get(_cf(op.name), op.name)
            self._add("expression", "update" if exists else "create", {"name": name}, props=props)
            live.expressions[_cf(op.name)] = op.expression
            live.expression_names[_cf(op.name)] = name
            if not exists:
                self._query_order(add=name)
        elif isinstance(op, DeleteNamedExpression):
            name = live.expression_names.get(_cf(op.name), op.name)
            self._add("expression", "delete", {"name": name})
            live.expressions.pop(_cf(op.name), None)
            self._query_order(remove=name)
        elif isinstance(op, SetQueryParameter):
            existing = live.expressions.get(_cf(op.name))
            try:
                text = set_parameter_value(existing, op.value, op.type)
            except ParameterProblem as error:
                raise ModelChangeFailed(f"'{op.name}': {error}") from None
            props = {"expression": text}
            if op.description is not None:
                props["description"] = op.description
            name = live.expression_names.get(_cf(op.name), op.name)
            self._add(
                "expression",
                "update" if existing is not None else "create",
                {"name": name},
                props=props,
            )
            live.expressions[_cf(op.name)] = text
            live.expression_names[_cf(op.name)] = name
            if existing is None:
                self._query_order(add=name)
        elif isinstance(op, RefreshData):
            self._refresh({"table": live.table(op.table)} if op.table else {"model": True})
        elif isinstance(op, SetModelOptions):
            # Refused before Desktop is opened too (``report_edit_model``); kept here so the
            # runner path never writes them.
            if op.auto_date_time is not None:
                raise ModelChangeFailed(AUTO_DATE_TIME_REFUSAL)
            if op.culture is not None:
                raise ModelChangeFailed(CULTURE_REFUSAL)
            props = {}
            if op.discourage_implicit_measures is not None:
                props["discourageImplicitMeasures"] = op.discourage_implicit_measures
                live.settings["discourageImplicitMeasures"] = op.discourage_implicit_measures
            self._add("model", "update", {}, props=props)
        else:
            raise ModelChangeFailed(
                f"Studio cannot apply '{getattr(op, 'op', op)}' in Power BI Desktop"
            )

    # --- helpers --------------------------------------------------------------------------------

    def _measure_home(self, name: str) -> str:
        home = self.live.measure_table(name)
        if home is None:
            raise ModelChangeFailed(f"The model in Power BI Desktop has no measure [{name}]")
        return home

    def _ends(
        self, op: CreateRelationship | UpdateRelationship | DeleteRelationship
    ) -> tuple[tuple[str, str], tuple[str, str]]:
        live = self.live
        source_table, target_table = live.table(op.from_table), live.table(op.to_table)
        return (
            (source_table, live.column(source_table, op.from_column)),
            (target_table, live.column(target_table, op.to_column)),
        )

    @staticmethod
    def _relationship_path(source: tuple[str, str], target: tuple[str, str]) -> dict[str, str]:
        return {
            "fromTable": source[0],
            "fromColumn": source[1],
            "toTable": target[0],
            "toColumn": target[1],
        }

    def _probe_relationship(self, source: tuple[str, str], target: tuple[str, str]) -> None:
        self._probe(
            f"The check of the relationship between {source[0]} and {target[0]}",
            f'EVALUATE TOPN(1, SUMMARIZECOLUMNS({_column_ref(*target)}, "rows", '
            f"COUNTROWS({_quote(source[0])})))",
        )

    def _parameter_dax(self, fields: list[ParameterField]) -> str:
        rows: list[str] = []
        for order, item in enumerate(fields):
            table = self.live.table(item.table)
            key = (_cf(table), _cf(item.name))
            name = self.live.measure_names.get(key) or self.live.column(table, item.name)
            label = (item.label or name).replace('"', '""')
            rows.append(f'    ("{label}", NAMEOF({_column_ref(table, name)}), {order})')
        return "{\n" + ",\n".join(rows) + "\n}"

    def _refresh(self, target: dict[str, Any]) -> None:
        if target not in self.plan.refresh:
            self.plan.refresh.append(target)

    def _query_order(self, *, add: str | None = None, remove: str | None = None) -> None:
        """Keep Desktop's list of queries (the model's PBI_QueryOrder annotation) in step with a
        query or parameter Studio adds or deletes; an absent or unreadable list is left alone."""
        names = self._query_names
        if names is None:
            return
        if add is not None and all(_cf(n) != _cf(add) for n in names):
            names.append(add)
        if remove is not None:
            names[:] = [n for n in names if _cf(n) != _cf(remove)]

    def finish(self) -> None:
        """The changes that sum up the batch: Desktop's query list, and the views of every role
        the batch set filters on (informational probes: the role's row count against
        everyone's, compared after the run)."""
        live, plan = self.live, self.plan
        if self._query_names is not None and self._query_names != self._query_names_before:
            order = json.dumps(self._query_names, ensure_ascii=False)
            self._add("model", "update", {}, props={"annotations": {QUERY_ORDER_ANNOTATION: order}})
        for role_key in sorted(self._viewed_roles):
            role = live.role_names.get(role_key)
            if role is None:
                continue
            for table, text in sorted(live.roles.get(role_key, {}).items()):
                if not text or _cf(table) not in live.tables or len(plan.role_views) >= MAX_VIEWS:
                    continue
                query = f'EVALUATE ROW("Rows", COUNTROWS({_quote(table)}))'
                number = len(plan.role_views) + 1
                everyone, viewed = f"v{number}a", f"v{number}b"
                label = f"The view of '{table}' as the role '{role}'"
                plan.probes.append(
                    {"id": everyone, "label": label, "query": query, "informational": True}
                )
                plan.probes.append(
                    {
                        "id": viewed,
                        "label": label,
                        "query": query,
                        "role": role,
                        "informational": True,
                    }
                )
                plan.role_views.append((role, table, everyone, viewed))


def _props(*pairs: tuple[str, Any]) -> dict[str, Any]:
    """The runner properties that were given (None means "leave as it is")."""
    return {key: value for key, value in pairs if value is not None}


def _rename_measure_dax(
    text: str, table: str, old: str, new: str, *, own_columns: frozenset[str]
) -> tuple[str, int]:
    """DAX with the measure ``old`` (of ``table``) called ``new``: ``[old]`` and
    ``'table'[old]``. In a formula that lives in a table with a column of that name (a
    calculated column, a role filter), a bare ``[old]`` is that column and stays."""
    pieces: list[str] = []
    last = count = 0
    for ref in dax_references(text):
        if ref.name is None or _cf(ref.name) != _cf(old):
            continue
        if ref.table is None and _cf(old) in own_columns:
            continue
        if ref.table is not None and _cf(ref.table) != _cf(table):
            continue
        pieces.append(text[last : ref.start])
        pieces.append(_measure_ref(new))
        last = ref.end
        count += 1
    pieces.append(text[last:])
    return "".join(pieces), count


def _with_set_by(props: dict[str, Any]) -> dict[str, Any]:
    """A summarization Studio sets is the user's: Desktop's own column tools write the
    SummarizationSetBy = User annotation with it, without which Desktop may reset it."""
    if "summarizeBy" in props:
        props["annotations"] = {"SummarizationSetBy": "User"}
    return props


_SECURITY = {"single": "OneDirection", "both": "BothDirections"}
QUERY_ORDER_ANNOTATION = "PBI_QueryOrder"


def _still_there(live: _Live, query: str) -> bool:
    """True when every table, column and measure a probe names exists at the end of the batch (a
    later step can delete or rename what an earlier step's probe would read)."""
    measures = live.measure_keys()
    # Probes always quote table names, so a quoted token is a table.
    if any(t.kind == "quoted" and _cf(t.text) not in live.tables for t in _tokenize(query)):
        return False
    for ref in dax_references(query):
        if ref.name is None:
            continue
        if ref.table is None:
            if _cf(ref.name) not in measures:
                return False
        elif (_cf(ref.table), _cf(ref.name)) not in live.columns and _cf(ref.name) not in measures:
            return False
    return True


def plan_changes(operations: list[Any], snapshot: Mapping[str, Any]) -> ModelPlan:
    """The runner changes for ``operations`` against the live model ``snapshot``."""
    planner = _Planner(snapshot)
    for op in operations:
        planner.add(op)
    kept = [p for p in planner.plan.probes if _still_there(planner.live, p["query"])]
    planner.plan.probes = [{**p, "id": f"p{i + 1}"} for i, p in enumerate(kept)]
    planner.finish()
    policy = refresh_policy(operations)
    planner.plan.on_refresh_failure = "keep" if policy == "keep" else "rollback"
    planner.plan.asks_on_refresh_failure = policy == "ask"
    return planner.plan


# --- the whole call -------------------------------------------------------------------------------


@dataclass
class ModelApplyResult:
    """What the runner did for a batch: a line per operation for the log, the changes sent and
    their before-images, the data refresh, the test queries and what the user should know."""

    done: list[str]
    changes: list[dict[str, Any]]
    results: list[dict[str, Any]]
    refreshed: bool | None = None
    refresh_error: str | None = None
    kept_without_refresh: bool = False
    probes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    fixups: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])
    snapshot_before: dict[str, Any] = field(default_factory=dict[str, Any])


def _log_stderr(answer: RunnerAnswer) -> None:
    """A runner that ended without a result: its stderr tail goes to the local log only."""
    if answer.stderr.strip():
        LOGGER.warning(
            "Studio's model tool ended without a result (finished=%s, exit=%s): %s",
            answer.finished,
            answer.exit_code,
            answer.stderr[-STDERR_TAIL:],
        )


def snapshot(runner: Runner, server: str, *, timeout: float = SNAPSHOT_TIMEOUT) -> dict[str, Any]:
    """The live model's metadata (the runner's ``snapshot`` action)."""
    answer = runner({"version": PROTOCOL, "action": "snapshot", "server": server}, timeout)
    result = answer.result
    if result is None:
        _log_stderr(answer)
        raise ModelChangeFailed(answer.why_no_result())
    if not result.get("ok") or not isinstance(result.get("snapshot"), dict):
        raise ModelChangeFailed(_plain(result))
    found: dict[str, Any] = result["snapshot"]
    return found


def _plain(result: Mapping[str, Any]) -> str:
    error = " ".join(str(result.get("error") or "").split())
    stage = result.get("stage")
    if stage in ("load", "request"):
        return f"Studio's model tool could not start ({error or 'no details'})"
    if stage == "connect":
        return (
            f"Studio could not connect to the model in Power BI Desktop ({error or 'no details'})"
        )
    return f"Power BI rejected the change: {error or 'no details'}"


def query(
    runner: Runner,
    server: str,
    queries: Mapping[str, str],
    *,
    role: str | None = None,
    timeout: float = SNAPSHOT_TIMEOUT,
) -> dict[str, Any]:
    """Read-only DAX queries (EVALUATE only) against the live model: id -> {ok, rows | error}.
    With ``role`` the model is seen as that role sees it (Desktop's "View as")."""
    request: dict[str, Any] = {
        "version": PROTOCOL,
        "action": "query",
        "server": server,
        "queries": [{"id": key, "query": text} for key, text in queries.items()],
    }
    if role:
        request["role"] = role
    answer = runner(request, timeout)
    result = answer.result
    if result is None:
        _log_stderr(answer)
        raise ModelChangeFailed(answer.why_no_result())
    if not result.get("ok"):
        raise ModelChangeFailed(_plain(result))
    answers: dict[str, Any] = result.get("answers") or {}
    return answers


def undo_applied(
    applied: list[ModelApplyResult],
    *,
    server: str,
    runner: Runner,
    timeout: float = APPLY_TIMEOUT,
) -> dict[str, Any]:
    """Put the model back from the before-images of batches that were applied, newest first, in
    one save (each inverse skips an object that is already as it was)."""
    steps = [
        step
        for batch in reversed(applied)
        for record in reversed(batch.results)
        for step in record.get("undo") or []
        if isinstance(step, dict)
    ]
    if not steps:
        return {"ok": True, "results": []}
    request = {
        "version": PROTOCOL,
        "action": "apply",
        "server": server,
        "changes": [{**step, "id": f"u{index + 1}"} for index, step in enumerate(steps)],
        "tolerant": True,
    }
    answer = runner(request, timeout)
    result = answer.result
    if result is None or not result.get("ok"):
        if result is None:
            _log_stderr(answer)
        reason = answer.why_no_result() if result is None else _plain(result)
        raise ModelChangeFailed(
            "Studio could not put the model back: " + reason,
            stage="undo",
            rolled_back=False,
        )
    return result


def _undo_steps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The inverse changes of every change the runner reported applying, newest first."""
    steps: list[dict[str, Any]] = []
    for event in reversed(events):
        if event.get("event") == "applied":
            steps.extend(u for u in event.get("undo") or [] if isinstance(u, dict))
    return steps


def _unordered(value: Any) -> Any:
    """A snapshot with every list sorted: an object put back from its before-image joins the
    end of its collection, which is the same model."""
    if isinstance(value, Mapping):
        return {str(k): _unordered(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [_unordered(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True, default=str))
    return value


def _same_model(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return bool(_unordered(a) == _unordered(b))


def _unknown_outcome(
    answer: RunnerAnswer, before: Mapping[str, Any], *, server: str, runner: Runner
) -> ModelOutcomeUnknown:
    """The runner ended without a result. Its printed undo steps are replayed only when the
    changes may have reached Desktop's model: after its "saved" event, or when a fresh snapshot
    differs from the one taken before (a save can land without its event being printed). A run
    that never saved changed nothing: replaying then could delete an object that had the name
    first (bug review #3). ``rolled_back`` is what a fresh snapshot shows, not a hope."""
    why = answer.why_no_result()
    steps = _undo_steps(answer.events)

    def fresh() -> dict[str, Any] | None:
        try:
            return snapshot(runner, server)
        except ModelChangeError:
            return None

    if not answer.saved:
        now = fresh()
        if now is not None and _same_model(now, before):
            return ModelOutcomeUnknown(
                f"{why}; the change did not reach the model", stage="timeout", rolled_back=True
            )
        if now is None:
            return ModelOutcomeUnknown(
                f"{why}, and Studio could not read the model afterwards: do not save the report",
                stage="timeout",
                rolled_back=False,
            )
    if steps:
        undo = {
            "version": PROTOCOL,
            "action": "apply",
            "server": server,
            "changes": [{**s, "id": f"u{i + 1}"} for i, s in enumerate(steps)],
            "tolerant": True,
        }
        with contextlib.suppress(ModelChangeError):
            runner(undo, SNAPSHOT_TIMEOUT)
    after = fresh()
    if after is not None and _same_model(after, before):
        return ModelOutcomeUnknown(
            f"{why}; Studio undid the part that reached the model",
            stage="timeout",
            rolled_back=True,
        )
    return ModelOutcomeUnknown(
        f"{why}. Studio could not undo all of it: do not save the report",
        stage="timeout",
        rolled_back=False,
    )


def apply_model_operations(
    operations: list[Any],
    *,
    server: str,
    runner: Runner,
    timeout: float = APPLY_TIMEOUT,
) -> ModelApplyResult:
    """Apply ``operations`` to the model at ``server`` in one transaction. Raises
    ModelRefreshFailed (the data could not be reloaded and the batch was undone),
    ModelOutcomeUnknown (the runner never answered) or ModelChangeFailed; in each case Studio has
    undone what reached the model, unless ``rolled_back`` is False."""
    before = snapshot(runner, server)
    plan = plan_changes(operations, before)
    request: dict[str, Any] = {
        "version": PROTOCOL,
        "action": "apply",
        "server": server,
        "changes": plan.changes,
        "recalculate": True,
        "refresh": plan.refresh,
        "on_refresh_failure": plan.on_refresh_failure,
        "probes": plan.probes,
    }
    answer = runner(request, timeout)
    result = answer.result
    if result is None:
        _log_stderr(answer)
        raise _unknown_outcome(answer, before, server=server, runner=runner)
    if not result.get("ok"):
        rolled_back = not result.get("rollback_errors")
        if result.get("stage") == "refresh" and rolled_back:
            reason = str((result.get("refresh") or {}).get("error") or result.get("error") or "")
            raise ModelRefreshFailed(
                "Power BI could not reload the data (" + " ".join(reason.split())[:300] + ")",
                reason,
            )
        message = _plain(result)
        if not rolled_back:
            message += (
                ". Studio could not undo all of it in Power BI Desktop: do not save the report"
            )
        raise ModelChangeFailed(
            message, stage=str(result.get("stage") or ""), rolled_back=rolled_back
        )
    warnings = list(plan.warnings)
    runner_warnings = [str(w) for w in result.get("warnings") or [] if isinstance(w, str)]
    if not result.get("checked_states", True) and not runner_warnings:
        warnings.append(
            "Studio could not read Power BI's error states for this change; it relied on its "
            "test queries."
        )
    warnings += [w + ("" if w.endswith(".") else ".") for w in runner_warnings]
    probes = [p for p in result.get("probes") or [] if isinstance(p, dict)]
    warnings += _role_view_warnings(plan, probes)
    refresh = result.get("refresh")
    refreshed = None if not plan.refresh else bool(isinstance(refresh, dict) and refresh.get("ok"))
    refresh_error = (
        str(refresh.get("error")) if isinstance(refresh, dict) and refresh.get("error") else None
    )
    if result.get("kept_without_refresh"):
        warnings.append(
            "Power BI could not reload the data ("
            + " ".join((refresh_error or "no details").split())[:300]
            + "). The new query is saved and takes effect at the next successful refresh."
        )
    if plan.fixups:
        shown = ", ".join(plan.fixups[:3]) + (
            f" and {len(plan.fixups) - 3} more" if len(plan.fixups) > 3 else ""
        )
        warnings.append(f"Studio also updated the formulas that used the old name: {shown}.")
    return ModelApplyResult(
        done=[f"{c['action']} {c['kind']}" for c in plan.changes],
        changes=plan.changes,
        results=[r for r in result.get("results") or [] if isinstance(r, dict)],
        refreshed=refreshed,
        refresh_error=refresh_error,
        kept_without_refresh=bool(result.get("kept_without_refresh")),
        probes=probes,
        fixups=list(plan.fixups),
        warnings=warnings,
        snapshot_before=before,
    )


def _probe_count(probe: Mapping[str, Any] | None) -> int | None:
    """The row count an informational ``ROW("Rows", COUNTROWS(...))`` probe read."""
    if not probe or probe.get("ok") is not True:
        return None
    values = probe.get("values")
    if not isinstance(values, list) or not values or not isinstance(values[0], Mapping):
        return None
    first = next(iter(values[0].values()), None)
    try:
        return int(first) if first is not None and not isinstance(first, bool) else None
    except (TypeError, ValueError):
        return None


def _role_view_warnings(plan: ModelPlan, probes: list[dict[str, Any]]) -> list[str]:
    """What viewing the model as each role the batch filters showed (Desktop's "View as" over a
    second connection): a filter that keeps every row, or none, is worth a look."""
    found = {str(p.get("id")): p for p in probes}
    warnings: list[str] = []
    unseen: list[str] = []
    for role, table, everyone_id, role_id in plan.role_views:
        everyone = _probe_count(found.get(everyone_id))
        seen = _probe_count(found.get(role_id))
        if everyone is None or seen is None:
            if role not in unseen:
                unseen.append(role)
        elif everyone > 0 and seen == everyone:
            warnings.append(
                f"Viewed as the role '{role}', '{table}' still shows all {everyone} of its rows: "
                "check the role's filter."
            )
        elif everyone > 0 and seen == 0:
            warnings.append(
                f"Viewed as the role '{role}', '{table}' shows none of its rows: check the "
                "role's filter."
            )
    for role in unseen:
        warnings.append(
            f"Studio could not view the model as the role '{role}'; use Modeling > View as in "
            "Power BI Desktop to check its filter."
        )
    return warnings
