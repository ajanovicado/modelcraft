"""Bounded, read-only previews tied to the imported report's own Desktop process."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
from filelock import FileLock, Timeout

from .contracts import DatasetSnapshot, DaxEvaluateRequest, DaxResult
from .report_import import ImportedReport, report_archive
from .report_revisions import RevisionStore

MAX_PREVIEW_BYTES = 12 * 1024 * 1024
_PNG = b"\x89PNG\r\n\x1a\n"
_HEAD_FOLDER = re.compile(r"[0-9a-f]{16}")
_LEGACY_PREVIEW = re.compile(r"page-\d+\.png")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")
STALE_VIEW = (
    "This view of the report is out of date because the report changed. "
    "Refresh the report, then preview it again."
)
STALE_DESKTOP = (
    "Power BI Desktop is not showing the current version of this report. Open the current "
    "version in Power BI Desktop, wait for it to load, then refresh the preview."
)


def working_copy(report: ImportedReport) -> Path:
    """The file Desktop has open for this report (never the preserved original)."""
    return Path(report.archived_path).parent / "desktop" / "Report.pbix"


def preview_name(page_id: str) -> str:
    """A file name for a page id: the id when it is already safe, else a sanitized form
    with a short hash so that two ids never share a file. Report-provided text is never
    used as a path."""
    safe = _UNSAFE_NAME.sub("_", page_id)[:80] or "page"
    if safe == page_id:
        return safe
    return f"{safe}-{hashlib.sha256(page_id.encode('utf-8')).hexdigest()[:8]}"


def command(argv: list[str], timeout: int = 30) -> dict[str, Any]:
    # Invoke npm's JavaScript entry point directly: report names must never go through cmd.exe.
    if Path(argv[0]).suffix.lower() == ".cmd":
        directory = Path(argv[0]).parent
        node = directory / "node.exe"
        entry = directory / "node_modules/@microsoft/powerbi-desktop-bridge-cli/dist/index.js"
        if not node.is_file() or not entry.is_file():
            raise ValueError(
                "The Power BI preview tooling is incomplete. Reinstall the Desktop bridge."
            )
        argv = [str(node), str(entry), *argv[1:]]
    try:
        result = subprocess.run(  # noqa: S603 - attested executables, argv only, query in owned file
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(
            "Power BI preview timed out. Try a smaller query or refresh the preview."
        ) from error
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise ValueError(
            "Power BI could not complete the preview. Check the open report and retry."
        ) from error
    if result.returncode or not isinstance(payload, dict) or payload.get("error"):
        detail = payload.get("error", "") if isinstance(payload, dict) else ""
        raise ValueError(f"Power BI could not evaluate this request: {str(detail)[:800]}")
    return payload


class NativeReportPreview:
    def __init__(self, root: Path, bridge: str, pbi: str):
        self.root, self.bridge, self.pbi = root, bridge, pbi

    def instance(self, report: ImportedReport) -> int:
        expected = working_copy(report).resolve()
        for attempt in range(5):
            status = command([self.bridge, "status"], 15)
            for instance in status.get("instances", []):
                path = instance.get("currentFilePath")
                if (
                    path
                    and Path(path).resolve() == expected
                    and instance.get("bridgeStatus") == "connected"
                ):
                    pid = instance.get("pid")
                    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                        return pid
            # Bound startup waits. Never open or select another report.
            if not any(
                i.get("bridgeStatus") == "not_connected" for i in status.get("instances", [])
            ):
                break
            if attempt < 4:
                time.sleep(0.5)
        raise ValueError(
            "Open this report in Power BI Desktop, wait for it to load, then refresh. "
            "Studio only connects to this report's working copy."
        )

    def lock(self) -> FileLock:
        return FileLock(self.root / "native-preview.lock", timeout=0)

    def head_placed(self, report: ImportedReport) -> bool:
        """Whether the report's head is what the working copy holds, so that a Desktop
        window on that file shows the head (and not an older version of the report).

        An edit or an undo moves the head; the working copy follows only when Studio
        writes it there. A report never edited has no history file and its working copy
        is a copy of the original.
        """
        _, head = report_archive(report)
        original = Path(report.archived_path)
        store = RevisionStore(original.parent, original, report.source_hash)
        if not store.index_path.is_file():
            return report.revision_path is None
        if store.head().blob != head:
            return False
        placed = store.placed()
        return placed is not None and store.revision(placed[0]).blob == head

    def preview_path(self, report: ImportedReport, page_id: str) -> Path:
        """Where the preview of ``page_id`` for the report's head is stored:
        previews/<first 16 hex of the head's hash>/<page id, made safe>.png. Keyed by the
        head, so an edit, an undo or a page reorder never shows another version's image."""
        _, head = report_archive(report)
        if not re.fullmatch(r"[0-9a-f]{64}", head):
            raise ValueError("The report's current version has no valid fingerprint")
        previews = Path(report.archived_path).parent / "previews"
        return previews / head[:16] / f"{preview_name(page_id)}.png"

    @staticmethod
    def _prune(previews: Path, keep: str) -> None:
        """Remove previews of versions that are not the head (and the old per-index files)."""
        try:
            entries = list(previews.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_dir() and _HEAD_FOLDER.fullmatch(entry.name) and entry.name != keep:
                shutil.rmtree(entry, ignore_errors=True)
            elif entry.is_file() and _LEGACY_PREVIEW.fullmatch(entry.name):
                entry.unlink(missing_ok=True)

    def page(
        self,
        report: ImportedReport,
        page_id: str,
        refresh: bool = False,
        *,
        head_hash: str | None = None,
    ) -> dict[str, Any]:
        """A Desktop screenshot of one page of the report's head.

        ``head_hash`` is the head the caller believes in; another head means the caller's
        view is stale and nothing is served. A capture is taken only while Desktop has the
        head open: a screenshot of an older version would be filed under the newest hash.
        """
        page = next((p for p in report.pages if p.id == page_id), None)
        if page is None:
            raise ValueError("This page does not belong to the imported report")
        _, head = report_archive(report)
        if head_hash is not None and head_hash != head:
            raise ValueError(STALE_VIEW)
        target = self.preview_path(report, page.id)
        folder = target.parent
        self._prune(folder.parent, head[:16])
        folder.mkdir(parents=True, exist_ok=True)
        cached = target.is_file() and not refresh
        if cached:
            with target.open("rb") as image:
                cached = target.stat().st_size <= MAX_PREVIEW_BYTES and image.read(8) == _PNG
        if not cached:
            if not self.head_placed(report):
                raise ValueError(STALE_DESKTOP)
            try:
                with self.lock():
                    pid = self.instance(report)
                    temporary = folder / f"capture-{uuid.uuid4().hex}.png"
                    try:
                        receipt = command(
                            [
                                self.bridge,
                                "screenshot",
                                page.id,
                                "--pid",
                                str(pid),
                                "--output",
                                str(temporary),
                                "--scale",
                                "1",
                                "--wait-seconds",
                                "20",
                            ],
                            30,
                        )
                        if (
                            receipt.get("status") != "ok"
                            or receipt.get("pid") != pid
                            or receipt.get("pageId") != page.id
                        ):
                            raise ValueError(
                                "Power BI returned a preview for a different report page"
                            )
                        self.instance(
                            report
                        )  # Switching reports during capture must not leak a wrong image.
                        # An edit that landed while Desktop was drawing must not have its
                        # older picture filed under the new head's hash.
                        if not self.head_placed(report):
                            raise ValueError(STALE_DESKTOP)
                        if not temporary.is_file() or temporary.stat().st_size > MAX_PREVIEW_BYTES:
                            raise ValueError("The report preview exceeds the image size limit")
                        if not temporary.read_bytes().startswith(_PNG):
                            raise ValueError("Power BI did not return a valid page image")
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
            except Timeout as error:
                raise ValueError(
                    "Another Power BI preview is running. Retry when it finishes."
                ) from error
        return {
            "image": "data:image/png;base64," + base64.b64encode(target.read_bytes()).decode(),
            "captured_at": datetime.fromtimestamp(target.stat().st_mtime, UTC).isoformat(),
            "cached": cached,
            "page_id": page.id,
            "head_hash": head,
            "engine": "Power BI Desktop",
        }

    def connection(self, report: ImportedReport) -> str:
        pid = self.instance(report)
        try:
            children = psutil.Process(pid).children()
            engines = [p for p in children if p.name().casefold() == "msmdsrv.exe"]
            if not engines:
                raise ValueError(
                    "This report has no local calculation engine. Wait for Power BI Desktop to "
                    "finish loading. If it uses a live connection to a remote model, Studio can "
                    "show Desktop page previews but cannot run DAX through this local connection."
                )
            if len(engines) > 1:
                raise ValueError(
                    "More than one calculation engine belongs to this report's Desktop process. "
                    "Reopen its working copy in Desktop so Studio can select one safely."
                )
            args = engines[0].cmdline()
            if "-s" not in args:
                raise ValueError(
                    "The report's calculation engine is unavailable. "
                    "Reopen its working copy in Desktop."
                )
            directory = Path(args[args.index("-s") + 1])
            port_file = directory / "msmdsrv.port.txt"
            if port_file.stat().st_size > 64:
                raise ValueError("Unexpected calculation-engine port file")
            port = port_file.read_text(encoding="utf-16-le").strip("\x00\r\n \ufeff")
            if not port.isdecimal() or not 1 <= int(port) <= 65535:
                raise ValueError("The report's calculation-engine port is invalid")
        except (psutil.Error, OSError, IndexError) as error:
            raise ValueError(
                "The report's calculation engine is unavailable. "
                "Reopen its working copy in Desktop."
            ) from error
        name = f"studio-{report.id}-{pid}"
        connected = command(
            [self.pbi, "--json", "connect", "--data-source", f"localhost:{port}", "--name", name]
        )
        if connected.get("status") != "connected" or connected.get("connection") != name:
            raise ValueError("Could not connect to this report's calculation engine")
        return name

    def query(self, report: ImportedReport, query: str, max_rows: int = 101) -> dict[str, Any]:
        """Execute a bounded, internally compiled read-only DAX query."""
        if len(query) > 200_000 or not 1 <= max_rows <= 5000:
            raise ValueError("This page exceeds the live preview query limit")
        try:
            with self.lock():
                if not self.head_placed(report):
                    raise ValueError(STALE_DESKTOP)
                connection = self.connection(report)
                query_path = self.root / f"native-query-{uuid.uuid4().hex}.dax"
                try:
                    query_path.write_text(query, encoding="utf-8")
                    payload = command(
                        [
                            self.pbi,
                            "--json",
                            "--connection",
                            connection,
                            "dax",
                            "execute",
                            "--max-rows",
                            str(max_rows),
                            "--timeout",
                            "15",
                            "--file",
                            str(query_path),
                        ],
                        25,
                    )
                    self.instance(report)
                    if not self.head_placed(report):
                        raise ValueError(STALE_DESKTOP)
                    return payload
                finally:
                    query_path.unlink(missing_ok=True)
        except Timeout as error:
            raise ValueError(
                "Another Power BI preview is running. Retry when it finishes."
            ) from error

    def evaluate(
        self, report: ImportedReport, snapshot: DatasetSnapshot | None, request: DaxEvaluateRequest
    ) -> DaxResult:
        expression = request.expression.strip()
        # XMLA/metadata/external queries are outside the scratchpad boundary.
        if re.search(
            r"\b(?:INFO\s*[.(]|EXTERNALMEASURE\s*\(|OPENROWSET\s*\(|EXECUTE\b)", expression, re.I
        ):
            raise ValueError(
                "Use a calculation or EVALUATE query. "
                "Metadata and external-source queries are not supported here."
            )
        if request.preview or request.detail_group or request.detail_period:
            raise ValueError(
                "For this native query, group with EVALUATE SUMMARIZECOLUMNS. "
                "The saved measure stays unchanged."
            )
        table_query = bool(re.match(r"^(?:EVALUATE|DEFINE)\b", expression, re.I))
        query = (
            expression
            if table_query
            else (
                f' EVALUATE VAR __StudioValue = ({expression}) RETURN ROW("Value", __StudioValue, '
                '"__StudioNumeric", ISNUMBER(__StudioValue), '
                '"__StudioBoolean", ISLOGICAL(__StudioValue))'
            )
        )
        started = time.monotonic()
        payload = self.query(report, query, request.top_n + 1)
        columns, rows = payload.get("columns"), payload.get("rows")
        if (
            not isinstance(columns, list)
            or not isinstance(rows, list)
            or len(columns) > 40
            or not all(isinstance(row, dict) for row in rows)
        ):
            raise ValueError("Limit this query to 40 columns and 100 preview rows")
        if not table_query or "[__StudioNumeric]" in columns:
            for row in rows:
                value = row.get("[Value]")
                if str(row.get("[__StudioNumeric]")).lower() == "true" and value is not None:
                    value = float(value)
                    value = value if math.isfinite(value) else None
                elif str(row.get("[__StudioBoolean]")).lower() == "true" and value is not None:
                    value = str(value).lower() == "true"
                row.pop("[Value]", None)
                row.pop("[__StudioNumeric]", None)
                row.pop("[__StudioBoolean]", None)
                row["Value"] = value
            columns = [
                c for c in columns if c not in {"[Value]", "[__StudioNumeric]", "[__StudioBoolean]"}
            ] + ["Value"]
        return DaxResult(
            columns=columns,
            rows=rows[: request.top_n],
            row_count=min(len(rows), request.top_n),
            truncated=len(rows) > request.top_n,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            engine="powerbi",
            plan=(
                "Power BI Desktop engine. Uses the working-copy model. "
                "Page slicer selections are not applied to this scratchpad query."
            ),
        )
