"""Fakes and builders shared by the revision, journal, editor and delivery tests.

``FakeDesktop`` stands in for Power BI Desktop (the ``DesktopPort`` protocol). It keeps what
matters for the edit pipeline real:
- an open instance holds the working copy's file handle, so on Windows the store's
  ``os.replace`` over it fails exactly as it does with Desktop open (access denied);
- Desktop dirties a file Studio wrote by itself, and ``open(normalize=True)`` saves that;
- a save rewrites the whole package (key order, member order, a new DataModel, a new
  SecurityBindings) without changing what the report says;
- ``close`` refuses while dirty and never kills;
- measure operations can fail, and a failure rolls the in-memory model back but leaves the
  flag set, as the real port does;
- every other model operation goes through the real ``desktop_model`` planner and the real TOM
  runner script, run in-process over ``fake_tom`` (``apply_model_operations``).
The DataModel of the fake's saved files is JSON ({"measures": [...], "model": {...}}), so a test
can read the model back; the fixture's own DataModel is random bytes, read as
``INITIAL_MEASURES`` and, for the rest of the model, ``fake_tom.agents_state()``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fake_tom import Measure, Model, Server, Table, agents_state, snapshot_of

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.studio import desktop_model, report_edit_model, report_revisions
from ai_powerbi_analyst.studio.desktop_session import (
    DesktopInstance,
    DesktopRefreshFailed,
    DesktopSessionError,
    UnsavedDesktopWork,
)
from ai_powerbi_analyst.studio.report_editor import ReportEditor
from ai_powerbi_analyst.studio.report_edits import (
    CreateMeasure,
    DeleteMeasure,
    RenameMeasure,
    UpdateMeasure,
)
from ai_powerbi_analyst.studio.report_import import ImportedReport
from ai_powerbi_analyst.studio.report_revisions import RevisionStore

TWIN = Path(__file__).resolve().parents[2] / "fixtures" / "imported" / "structure-twin.pbix"
PAGE = "ReportSection8454c812b60b8bb83b99"
VISUAL = "54be5cf0783e66471540"  # a slicer, captioned 'Country'
OTHER_VISUAL = "953a59a3cad98b0b5a39"
VISUAL_PART = f"Report/definition/pages/{PAGE}/visuals/{VISUAL}/visual.json"
OTHER_VISUAL_PART = f"Report/definition/pages/{PAGE}/visuals/{OTHER_VISUAL}/visual.json"
MEASURE_TABLE = "# Measures"
INITIAL_MEASURES: list[dict[str, Any]] = [
    {"Name": "Rank", "TableName": MEASURE_TABLE, "Expression": "RANKX(ALL('DimChannel'), 1)"},
    {"Name": "Employees Avg MTD Sales", "TableName": MEASURE_TABLE, "Expression": "1"},
    {"Name": "dummy", "TableName": MEASURE_TABLE, "Expression": "BLANK()"},
]
TABLES: list[dict[str, Any]] = [
    {"TableName": "DimChannel", "ColumnName": "ChannelName", "PandasDataType": "object"},
]
DESKTOP_VERSION = "2.157.1354.0"


# --- packages ----------------------------------------------------------------------------------


def sha(path: Path) -> str:
    return sha256_file(path)


def make_zip(path: Path, members: dict[str, bytes]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return sha(path)


def tiny_pbix(path: Path, tag: str) -> str:
    """A small valid ZIP whose content (and so hash) depends on ``tag``."""
    return make_zip(
        path,
        {
            "Report/definition/report.json": json.dumps({"tag": tag}).encode(),
            "DataModel": f"model-{tag}".encode(),
        },
    )


def thin_pbix(path: Path) -> str:
    """The structure twin without a DataModel: a live-connection report."""
    with zipfile.ZipFile(TWIN) as source:
        members = {n: source.read(n) for n in source.namelist() if n != "DataModel"}
    return make_zip(path, members)


def read_part(path: Path, name: str) -> Any:
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read(name).decode("utf-8-sig"))


def part_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def model_of(data: bytes) -> list[dict[str, Any]]:
    try:
        value = json.loads(data.decode("utf-8"))
        return list(value["measures"])
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        return copy.deepcopy(INITIAL_MEASURES)


def read_measures(path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        return model_of(archive.read("DataModel"))


def state_of(data: bytes) -> dict[str, Any] | None:
    """The rest of the model (tables, relationships, roles...) a fake DataModel holds, if any."""
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    state = value.get("model") if isinstance(value, dict) else None
    return copy.deepcopy(state) if isinstance(state, dict) else None


def tom_model(state: dict[str, Any] | None, measures: list[dict[str, Any]]) -> Model:
    """The fake engine's model: the model state (or the Agents-like default) with ``measures``
    (the fake's own measure rows) as its measures, in their order."""
    model = Model.from_state(copy.deepcopy(state) if state is not None else agents_state())
    for table in model.Tables:
        table.Measures.items.clear()
    for row in measures:
        table = model.Tables.Find(str(row["TableName"]))
        if table is None:
            table = Table()
            table.Name = str(row["TableName"])
            model.Tables.Add(table)
        measure = Measure()
        measure.Name = str(row["Name"])
        measure.Expression = str(row.get("Expression", ""))
        measure.FormatString = row.get("FormatString")
        table.Measures.Add(measure)
    model._saved = model.dump()  # this is the state the engine has saved
    return model


def measure_rows(model: Model) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in model.Tables:
        for measure in table.Measures:
            row: dict[str, Any] = {
                "Name": measure.Name,
                "TableName": table.Name,
                "Expression": measure.Expression,
            }
            if measure.FormatString:
                row["FormatString"] = measure.FormatString
            rows.append(row)
    return rows


def read_model_facts(path: Path) -> dict[str, Any]:
    """The saved file's model facts, the shape ``pbix_extract --facts`` reads: the runner's
    snapshot of the fake engine's model stored in the DataModel."""
    with zipfile.ZipFile(path) as archive:
        data = archive.read("DataModel")
    return snapshot_of(tom_model(state_of(data), model_of(data)))


def model_types(report: ImportedReport) -> tuple[dict[str, Any], dict[str, str]]:
    """The model facts the editor checks an edit against (``ReportEditor(model_facts=...)``): the
    whole snapshot as the types payload, and the DAX of calculated columns and tables."""
    facts = read_model_facts(Path(report.revision_path or report.archived_path))
    calculated: dict[str, str] = {}
    for table in facts["tables"]:
        for partition in table["partitions"]:
            if partition["sourceType"] == "Calculated" and len(table["partitions"]) == 1:
                calculated[table["name"]] = partition["expression"]
        for column in table["columns"]:
            if column.get("expression"):
                calculated[f"{table['name']}[{column['name']}]"] = column["expression"]
    return facts, calculated


def model_tables(state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The import's table rows (TableName, ColumnName, PandasDataType) of a model state."""
    dtypes = {
        "Int64": "int64",
        "Double": "float64",
        "DateTime": "datetime64[ns]",
        "String": "object",
    }
    return [
        {
            "TableName": table["name"],
            "ColumnName": column["name"],
            "PandasDataType": dtypes.get(column.get("dataType", ""), "object"),
        }
        for table in (state or agents_state())["tables"]
        for column in table["columns"]
    ]


# --- the pbix_diff test doubles ----------------------------------------------------------------


def _canonical(path: Path) -> dict[str, Any]:
    found: dict[str, Any] = {}
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name in ("SecurityBindings", "[Content_Types].xml"):
                continue
            data = archive.read(name)
            if name == "DataModel":
                found[name] = model_of(data)
            elif name.startswith("Report/definition/") and name.endswith(".json"):
                found[name] = json.loads(data.decode("utf-8-sig"))
            else:
                found[name] = hashlib.sha256(data).hexdigest()
    return found


def semantic_equal(a: Path, b: Path) -> bool:
    """Same report ignoring key order, DataModel bytes and SecurityBindings."""
    return _canonical(a) == _canonical(b)


def _missing(written: Any, saved: Any, where: str) -> str | None:
    if isinstance(written, dict):
        if not isinstance(saved, dict):
            return where
        for key, value in written.items():
            if key not in saved:
                return f"{where}/{key}"
            found = _missing(value, saved[key], f"{where}/{key}")
            if found:
                return found
        return None
    return None if written == saved else where


SURVIVAL_CALLS: list[tuple[Path, Path, Path | None]] = []


def survival(written: Path, saved: Path, baseline: Path | None = None) -> list[str]:
    """Every JSON property in ``written`` still present, with the same value, in ``saved``."""
    SURVIVAL_CALLS.append((written, saved, baseline))
    before, after = _canonical(written), _canonical(saved)
    dropped = []
    for name, value in before.items():
        if not name.endswith(".json"):
            continue
        if name not in after:
            dropped.append(name)
            continue
        found = _missing(value, after[name], name)
        if found:
            dropped.append(found)
    return dropped


def patch_diff(monkeypatch: Any) -> None:
    """Route the store's pbix_diff bridge to the doubles above."""
    monkeypatch.setattr(report_revisions, "semantic_equal", semantic_equal)
    monkeypatch.setattr(report_revisions, "survival", survival)


# --- Power BI Desktop --------------------------------------------------------------------------


@dataclass
class _Instance:
    instance: DesktopInstance
    handle: Any
    measures: list[dict[str, Any]]
    dirty: bool = True
    normalized: bool = False
    patches: list[tuple[str, Callable[[Any], None]]] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)
    # The rest of the model as the fake engine keeps it (None: the file's DataModel has none).
    state: dict[str, Any] | None = None
    saved_state: dict[str, Any] | None = None


class FakeDesktop:
    def __init__(self) -> None:
        self.instances: dict[Path, _Instance] = {}
        self.blocking: list[str] = []
        self.fail_open: DesktopSessionError | None = None
        self.fail_close: DesktopSessionError | None = None
        self.fail_save: DesktopSessionError | None = None
        self.fail_ops: set[str] = set()  # operation names that Desktop rejects
        self.keep_measures = True  # False: a save does not persist the model change
        self.drop_on_save: list[tuple[str, str]] = []  # (part, top-level key Desktop loses)
        self.events: list[str] = []
        self.saves = 0
        self._pid = 4000
        # The engine behind apply_model_operations: queries that fail, a save the engine refuses,
        # and a save that loses the model change (the file keeps the model as it was opened).
        self.fail_queries: tuple[str, ...] = ()
        self.fail_model_saves: list[str] = []
        self.keep_model = True
        self.runner_calls: list[dict[str, Any]] = []
        # The kinds of change outside Microsoft's list a model probe receipt covers for this
        # Desktop version (None: every kind, as after a passing probe).
        self.probe_verified: frozenset[str] | None = None

    # --- test controls ---------------------------------------------------------------------

    def user_edit(self, path: Path, part: str, mutate: Callable[[Any], None]) -> None:
        """The user changes the report in Desktop (unsaved until they, or Studio, save)."""
        inst = self.instances[path.resolve()]
        inst.patches.append((part, mutate))
        inst.dirty = True

    def user_removes(self, path: Path, prefix: str) -> None:
        """The user deletes a visual or a page (every part under ``prefix``)."""
        inst = self.instances[path.resolve()]
        inst.removals.append(prefix)
        inst.dirty = True

    def user_saves(self, path: Path) -> None:
        self.save(self.instances[path.resolve()].instance)

    def user_closes(self, path: Path) -> None:
        inst = self.instances.pop(path.resolve())
        inst.handle.close()

    def open_paths(self) -> list[Path]:
        return list(self.instances)

    # --- DesktopPort -------------------------------------------------------------------------

    def find(self, path: Path) -> DesktopInstance | None:
        found = self.instances.get(Path(path).resolve())
        return None if found is None else found.instance

    def _get(self, instance: DesktopInstance) -> _Instance:
        found = self.instances.get(instance.path.resolve())
        if found is None or found.instance != instance:
            raise DesktopSessionError("Power BI Desktop was closed")
        return found

    def is_normalized(self, instance: DesktopInstance) -> bool:
        return self._get(instance).normalized

    def status(self, instance: DesktopInstance) -> dict[str, Any]:
        return {"pid": instance.pid, "hasUnsavedChanges": self._get(instance).dirty}

    def open(self, path: Path, *, normalize: bool = True) -> DesktopInstance:
        resolved = Path(path).resolve()
        if resolved in self.instances:
            return self.instances[resolved].instance
        if self.fail_open is not None:
            raise self.fail_open
        self._pid += 1
        instance = DesktopInstance(self._pid, 1000.0 + self._pid, resolved)
        with zipfile.ZipFile(resolved) as archive:
            data = archive.read("DataModel") if "DataModel" in archive.namelist() else None
        measures = model_of(data) if data is not None else []
        state = state_of(data) if data is not None else None
        inst = _Instance(instance, resolved.open("rb"), measures, state=state, saved_state=state)
        self.instances[resolved] = inst
        self.events.append(f"open normalize={normalize}")
        if normalize:
            self._write(inst)
            inst.normalized = True
        return instance

    def _write(self, inst: _Instance) -> None:
        """Rewrite the whole package the way a Desktop save does."""
        path = inst.instance.path
        inst.handle.close()
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            members = {name: archive.read(name) for name in names}
        patched: dict[str, bytes] = {}
        for name, data in members.items():
            if name == "SecurityBindings" or any(name.startswith(p) for p in inst.removals):
                continue
            if name == "DataModel":
                keep = self.keep_measures
                model = inst.measures if keep else model_of(data)
                body: dict[str, Any] = {"measures": model}
                state = inst.state if self.keep_model else state_of(data)
                if state is not None:
                    body["model"] = state
                patched[name] = json.dumps(body).encode()
            elif name.startswith("Report/definition/") and name.endswith(".json"):
                value = json.loads(data.decode("utf-8-sig"))
                for part, mutate in inst.patches:
                    if part == name:
                        mutate(value)
                for part, key in self.drop_on_save:
                    if part == name:
                        value.pop(key, None)
                patched[name] = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            else:
                patched[name] = data
        self.saves += 1
        patched["SecurityBindings"] = f"binding-{self.saves}".encode()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
            for name in reversed(list(patched)):
                info = zipfile.ZipInfo(name)
                info.compress_type = (
                    zipfile.ZIP_STORED if name == "DataModel" else zipfile.ZIP_DEFLATED
                )
                out.writestr(info, patched[name])
        inst.patches.clear()
        inst.removals.clear()
        inst.dirty = False
        inst.handle = path.open("rb")

    def save(self, instance: DesktopInstance) -> None:
        inst = self._get(instance)
        if self.fail_save is not None:
            raise self.fail_save
        if self.blocking:
            raise DesktopSessionError("Power BI Desktop is showing a dialog")
        self._write(inst)
        self.events.append("save")

    def close(self, instance: DesktopInstance) -> None:
        inst = self._get(instance)
        if self.fail_close is not None:
            raise self.fail_close
        if inst.dirty:
            raise UnsavedDesktopWork("Power BI Desktop has unsaved changes to this report.")
        inst.handle.close()
        del self.instances[instance.path.resolve()]
        self.events.append("close")

    def apply_measure_operations(
        self, instance: DesktopInstance, operations: list[Any], tables: dict[str, str]
    ) -> list[str]:
        inst = self._get(instance)
        before = copy.deepcopy(inst.measures)
        done: list[str] = []
        try:
            for op in operations:
                if isinstance(op, CreateMeasure):
                    inst.measures.append(
                        {"Name": op.name, "TableName": op.table, "Expression": op.expression}
                        | ({"FormatString": op.format_string} if op.format_string else {})
                    )
                    done.append(f"created [{op.name}]")
                elif isinstance(op, UpdateMeasure):
                    row = next(m for m in inst.measures if m["Name"] == op.name)
                    if op.expression is not None:
                        row["Expression"] = op.expression
                    done.append(f"updated [{op.name}]")
                elif isinstance(op, RenameMeasure):
                    row = next(m for m in inst.measures if m["Name"] == op.name)
                    row["Name"] = op.new_name
                    done.append(f"renamed [{op.name}]")
                elif isinstance(op, DeleteMeasure):
                    inst.measures = [m for m in inst.measures if m["Name"] != op.name]
                    done.append(f"deleted [{op.name}]")
                if op.op in self.fail_ops:
                    raise DesktopSessionError(f"The measure {op.name} has an error")
        except DesktopSessionError:
            inst.measures = before  # rolled back; the flag stays set, as in Desktop
            inst.dirty = True
            self.events.append("ops rolled back")
            raise
        inst.dirty = True
        self.events.append("ops applied")
        return done

    def apply_model_operations(
        self, instance: DesktopInstance, operations: list[Any]
    ) -> desktop_model.ModelApplyResult:
        """The real planner and runner over the fake engine, like DesktopController does."""
        inst = self._get(instance)
        model = tom_model(inst.state, inst.measures)
        model.fail_saves = list(self.fail_model_saves)
        server = Server(model, fail_queries=self.fail_queries)
        inst.dirty = True  # a TOM write sets the flag, even when it is undone again
        try:
            result = desktop_model.apply_model_operations(
                operations, server="localhost:5000", runner=server
            )
        except desktop_model.ModelRefreshFailed as error:
            self.events.append("model rolled back")
            raise DesktopRefreshFailed(str(error), error.reason) from error
        except desktop_model.ModelChangeError as error:
            self.events.append("model rolled back")
            raise DesktopSessionError(str(error)) from error
        finally:
            self.runner_calls.extend(server.calls)
        dumped = model.dump()
        for table in dumped["tables"]:
            table["measures"] = []
        inst.state = dumped
        inst.measures = measure_rows(model)
        self.events.append("model applied")
        return result

    def screenshot(self, instance: DesktopInstance, page_id: str, output: Path) -> Path:
        self._get(instance)
        output.write_bytes(b"png" * 50)
        return output

    def blocking_windows(self, instance: DesktopInstance) -> list[str]:
        self._get(instance)
        return list(self.blocking)

    def version(self) -> str | None:
        return DESKTOP_VERSION

    def model_probe_coverage(self) -> tuple[str | None, frozenset[str]]:
        verified = self.probe_verified
        if verified is None:
            verified = frozenset(report_edit_model.OUTSIDE_SUPPORT)
        return DESKTOP_VERSION, verified


# --- an imported report on disk ----------------------------------------------------------------


@dataclass
class Env:
    root: Path
    report: ImportedReport
    store: RevisionStore
    desktop: FakeDesktop
    editor: ReportEditor
    source: Path
    rederived: list[tuple[str, bool]]
    crash_at: list[str] = field(default_factory=list)

    @property
    def working(self) -> Path:
        return self.store.working_copy


class Crash(BaseException):
    """A simulated process death: the editor's failure handlers do not catch it."""


MODEL_MEASURES: list[dict[str, Any]] = [
    {
        "Name": "Total Sales",
        "TableName": MEASURE_TABLE,
        "Expression": "SUM(FactSales[SalesAmount])",
    },
    {
        "Name": "Stores",
        "TableName": MEASURE_TABLE,
        "Expression": "DISTINCTCOUNT(DimStore[StoreName])",
    },
]


def model_state(**changes: Any) -> dict[str, Any]:
    """The Agents-like model state without measures (the fake keeps those in its own rows)."""
    state = agents_state()
    for table in state["tables"]:
        table["measures"] = []
    state.update(changes)
    return state


def twin_with_model(path: Path, state: dict[str, Any], measures: list[dict[str, Any]]) -> str:
    """The structure twin whose DataModel is the fake's JSON: these measures and this model."""
    with zipfile.ZipFile(TWIN) as source:
        members = {n: source.read(n) for n in source.namelist()}
    members["DataModel"] = json.dumps({"measures": measures, "model": state}).encode()
    return make_zip(path, members)


def make_report(
    tmp_path: Path,
    *,
    thin: bool = False,
    tiny: bool = False,
    name: str = "Agents",
    source_file: Path | None = None,
    model: dict[str, Any] | None = None,
) -> tuple[ImportedReport, RevisionStore, Path]:
    root = tmp_path / "state" / "reports" / "report-1"
    root.mkdir(parents=True)
    source = tmp_path / "Downloads" / f"{name}.pbix"
    source.parent.mkdir()
    measures = copy.deepcopy(INITIAL_MEASURES)
    if source_file is not None:
        shutil.copyfile(source_file, source)
    elif thin:
        thin_pbix(source)
    elif tiny:
        tiny_pbix(source, "orig")
    elif model is not None:
        measures += copy.deepcopy(MODEL_MEASURES)
        twin_with_model(source, model, measures)
    else:
        shutil.copyfile(TWIN, source)
    original = root / "original.pbix"
    shutil.copyfile(source, original)
    digest = sha(original)
    report = ImportedReport(
        id="report-1",
        workspace_id="workspace-1",
        name=name,
        source_path=str(source),
        archived_path=str(original),
        source_hash=digest,
        base_hash=digest,
        format="PBIR",
        pages=[],
        tables=[] if thin else model_tables(model) if model is not None else copy.deepcopy(TABLES),
        measures=[] if thin else measures,
    )
    return report, RevisionStore(root, original, digest), source


def make_env(
    tmp_path: Path,
    *,
    thin: bool = False,
    tiny: bool = False,
    disk_free: Callable[[Path], int] | None = None,
    reopen: bool = True,
    source_file: Path | None = None,
    model: dict[str, Any] | None = None,
) -> Env:
    """An imported report on disk with a fake Desktop. With ``model`` (a model state, see
    ``model_state``) the twin carries that model and the editor reads its facts (types,
    calculated DAX and structure) and holds model batches to the saved file's facts."""
    report, store, source = make_report(
        tmp_path, thin=thin, tiny=tiny, source_file=source_file, model=model
    )
    desktop = FakeDesktop()
    rederived: list[tuple[str, bool]] = []

    def rederive(
        current: ImportedReport, path: Path, blob: str, model_changed: bool
    ) -> ImportedReport:
        rederived.append((blob, model_changed))
        update: dict[str, Any] = {"revision_path": str(path), "revision_hash": blob}
        if model_changed and not thin:
            update["measures"] = read_measures(path)
        return current.model_copy(update=update)

    env_crash: list[str] = []

    def checkpoint(state: str) -> None:
        if state in env_crash:
            raise Crash(state)

    editor = ReportEditor(
        tmp_path / "state",
        rederive,
        lambda: desktop,
        read_measures=read_measures,
        disk_free=disk_free or (lambda _path: 10**12),
        profile_dir=tmp_path,
        reopen_desktop=reopen,
        checkpoint=checkpoint,
        model_facts=model_types if model is not None else None,
        read_model_facts=read_model_facts,
    )
    return Env(tmp_path, report, store, desktop, editor, source, rederived, env_crash)


def visual_x(path: Path, part: str = VISUAL_PART) -> float:
    return float(read_part(path, part)["position"]["x"])


def rows_at(index_path: Path) -> list[str]:
    return [row["id"] for row in json.loads(index_path.read_text("utf-8"))["revisions"]]


def unlocked(path: Path) -> bool:
    """True when ``path`` can be renamed (no handle without share-delete is open on it)."""
    moved = path.with_name(path.name + ".probe")
    try:
        os.replace(path, moved)
    except PermissionError:
        return False
    os.replace(moved, path)
    return True
