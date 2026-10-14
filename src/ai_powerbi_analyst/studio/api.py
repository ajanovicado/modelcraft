"""Authenticated loopback HTTP/SSE API for the Tauri desktop shell."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from threading import Event
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from ai_powerbi_analyst.llm.model_install import (
    ModelInstaller,
    ModelInstallError,
    ModelInstallRequest,
    model_target,
)
from ai_powerbi_analyst.llm.runtime import LocalModelError
from ai_powerbi_analyst.powerbi.semantic_model import SemanticProjectError
from ai_powerbi_analyst.powerbi.theme import studio_theme_palettes

from .connections import (
    DatabaseBrowseRequest,
    DatabaseImportRequest,
    DatabaseTableRequest,
    drivers,
)
from .connections import (
    browse as browse_database,
)
from .connections import (
    preview as preview_database,
)
from .contracts import (
    AddDaxMeasureRequest,
    AddReportTestRequest,
    ApproveAdvancedDaxRequest,
    ApproveDraftRequest,
    BuildRequest,
    ChatEditChoiceRequest,
    ChatEditConfirmRequest,
    ChatRequest,
    ClearChatRequest,
    ClearDaxChatRequest,
    CompatibilityRequest,
    CreateWorkspaceRequest,
    DaxAssistRequest,
    DaxEvaluateRequest,
    DeleteWorkspaceRequest,
    DraftRequest,
    ExplainNumberRequest,
    ExportBuildRequest,
    ImportedEditRequest,
    ImportedReplaceOriginalRequest,
    ImportedRestoreRequest,
    ImportedReviewRequest,
    ImportedSaveCopyRequest,
    ImportedUndoRequest,
    IngestRequest,
    InventoryRequest,
    ModelHealthFixRequest,
    OpenBuildRequest,
    PinReportTestRequest,
    RelinkBuildRequest,
    RenameWorkspaceRequest,
    RestoreDraftRequest,
    ReviseDraftRequest,
    RunReportTestsRequest,
    SetWorkspaceFolderRequest,
    UndoBuildRequest,
    UpdateDashboardRequest,
    UpdateReportTestRequest,
)
from .data_access import TablePreviewRequest, preview_table
from .dax_assistant import dax_assistant_events
from .imported_chat_edits import collect_answer
from .imported_editing import (
    BatchOutcome,
    ImportedEditError,
    ImportedRereadRequest,
    ModelQueryCheckRequest,
)
from .ingestion import StudioIngestionError
from .inventory import InventoryError
from .live_report import LiveReportRequest
from .model_status import model_failure_message
from .planner import StudioPlanningError
from .report_capabilities import report_capabilities
from .report_import import ImportReportRequest
from .service import StudioService
from .state import StateConflictError

CHAT_HEARTBEAT_SECONDS = 5.0
LOGGER = logging.getLogger(__name__)
# A revision id as the history writes it (r0007); the render and preview routes echo it.
REVISION = r"^r\d{4,}$"
_CLIENT_SAFE_ERRORS = (
    StudioPlanningError,
    StudioIngestionError,
    InventoryError,
    SemanticProjectError,
    StateConflictError,
    ValueError,
    KeyError,
    FileNotFoundError,
    FileExistsError,
    PermissionError,
)


def _bearer_digest(value: str) -> bytes:
    """Hash tokens before compare_digest so unequal lengths cannot raise."""
    return hashlib.sha256(value.encode("utf-8")).digest()


def _client_error_message(error: Exception) -> str:
    if isinstance(error, LocalModelError):
        return model_failure_message(str(error))
    if isinstance(error, _CLIENT_SAFE_ERRORS):
        text = str(error).strip()
        if text:
            return text
    return "The local engine could not complete this request. Check Settings and try again."


def create_app(
    service: StudioService,
    bearer_token: str,
    *,
    dev_browser_origins: list[str] | None = None,
) -> FastAPI:
    if len(bearer_token) < 16:
        raise ValueError("Sidecar bearer token must contain at least 16 characters")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Batches a crash left half done are finished or undone in the background (P9). A
        # stand-in service without the editing part (some tests use one) has nothing to recover.
        editing = getattr(service, "editing", None)
        if editing is not None:
            editing.start_recovery()
        try:
            yield
        finally:
            if editing is not None:
                await asyncio.to_thread(editing.close)
            await asyncio.to_thread(service.model.unload)

    app = FastAPI(
        title="Modelcraft local sidecar",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    origins = ["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"]
    for dev_browser_origin in dev_browser_origins or ():
        parsed = urlsplit(dev_browser_origin)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Development browser origin must be a loopback HTTP origin")
        origins.append(dev_browser_origin)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        # PATCH and DELETE: the report tests routes (edit and remove a test).
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    @app.middleware("http")
    async def authenticate(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)
        authorization = request.headers.get("Authorization", "")
        supplied = (
            authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        )
        if not secrets.compare_digest(_bearer_digest(supplied), _bearer_digest(bearer_token)):
            LOGGER.warning("Rejected unauthenticated request to %s", request.url.path)
            return JSONResponse(status_code=401, content={"detail": "Unauthorized local client"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(ImportedEditError)
    async def imported_edit_handler(_request: Request, error: ImportedEditError) -> JSONResponse:
        # {code, message, ...} as the editing contract defines, plus the ``detail`` every
        # other route uses.
        return JSONResponse(status_code=error.status, content=error.payload())

    @app.exception_handler(StateConflictError)
    async def conflict_handler(_request: Request, error: StateConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(KeyError)
    async def missing_handler(_request: Request, error: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(ValueError)
    @app.exception_handler(StudioPlanningError)
    @app.exception_handler(StudioIngestionError)
    @app.exception_handler(InventoryError)
    @app.exception_handler(SemanticProjectError)
    async def invalid_input_handler(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @app.exception_handler(FileExistsError)
    @app.exception_handler(PermissionError)
    async def file_conflict_handler(_request: Request, error: OSError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"Local file operation could not complete: {error}. "
                "Choose an unused destination or close the application holding the file."
            },
        )

    @app.exception_handler(FileNotFoundError)
    async def missing_file_handler(_request: Request, _error: FileNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    "The selected local file or folder no longer exists. "
                    "Select it again or rescan the source folder."
                )
            },
        )

    @app.get("/v1/health")
    async def health() -> object:
        return await asyncio.to_thread(service.health)

    @app.get("/v1/model-health")
    async def model_health() -> object:
        # Recovery polling must not repeatedly launch Desktop/tool discovery.
        # This checks readiness only; it does not load or run the model.
        return await asyncio.to_thread(service.model.health)

    # First-launch install of the one pinned model (installers cannot carry it).
    installer = ModelInstaller(model_target(service.model))

    @app.get("/v1/model/install")
    async def model_install_status() -> object:
        return await asyncio.to_thread(installer.status)

    @app.post("/v1/model/install")
    async def start_model_install(request: ModelInstallRequest) -> object:
        try:
            return await asyncio.to_thread(installer.start, request.source, request.path)
        except ModelInstallError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/model/install/cancel")
    async def cancel_model_install() -> object:
        return await asyncio.to_thread(installer.cancel)

    @app.get("/v1/report-capabilities")
    async def report_visual_capabilities() -> object:
        return report_capabilities()

    @app.get("/v1/report-themes")
    async def report_themes() -> object:
        return studio_theme_palettes()

    @app.get("/v1/ping")
    async def ping() -> object:
        return {
            "ready": True,
            "features": [
                "chat-context",
                "draft-restore",
                "chat-clear",
                "chat-history-pages",
                "dax-chat-clear",
                "workspace-admin",
            ],
        }

    @app.get("/v1/knowledge")
    async def knowledge_status() -> object:
        return service.knowledge.status()

    @app.get("/v1/knowledge/search")
    async def knowledge_search(
        q: str = Query(min_length=1, max_length=1000),
        mode: Literal["docs", "recipes", "hybrid"] = "hybrid",
    ) -> object:
        return await asyncio.to_thread(service.knowledge.retrieve, q, mode=mode)

    @app.post("/v1/reports/import")
    async def import_existing_report(request: ImportReportRequest) -> object:
        return await asyncio.to_thread(service.import_existing_report, request)

    def answer(outcome: BatchOutcome) -> JSONResponse:
        """200 with the batch's result, or 202 with its id while it is still running."""
        return JSONResponse(status_code=outcome.status, content=outcome.body)

    @app.get("/v1/workspaces/{workspace_id}/imported-report")
    async def imported_report(workspace_id: str) -> object:
        return await asyncio.to_thread(service.imported_report_payload, workspace_id)

    @app.post("/v1/workspaces/{workspace_id}/imported-report/pages/{page_id}/preview")
    async def preview_imported_page(
        workspace_id: str,
        page_id: str,
        refresh: bool = False,
        revision: str | None = Query(default=None, pattern=REVISION),
    ) -> object:
        return await asyncio.to_thread(
            service.preview_imported_page, workspace_id, page_id, refresh, revision
        )

    @app.post("/v1/workspaces/{workspace_id}/imported-report/pages/{page_id}/render")
    async def render_imported_page(
        workspace_id: str,
        page_id: str,
        request: LiveReportRequest,
        revision: str | None = Query(default=None, pattern=REVISION),
    ) -> object:
        return await asyncio.to_thread(
            service.render_imported_page, workspace_id, page_id, request, revision
        )

    @app.post("/v1/workspaces/{workspace_id}/imported-report/pages/{page_id}/render-formatting")
    async def render_imported_formatting(
        workspace_id: str,
        page_id: str,
        request: LiveReportRequest,
        revision: str | None = Query(default=None, pattern=REVISION),
        only: Annotated[list[str] | None, Query()] = None,
    ) -> object:
        return await asyncio.to_thread(
            service.render_imported_formatting,
            workspace_id,
            page_id,
            request,
            set(only) if only else None,
            revision,
        )

    @app.post("/v1/workspaces/{workspace_id}/imported-report/edits")
    async def imported_edit(workspace_id: str, request: ImportedEditRequest) -> JSONResponse:
        return answer(await asyncio.to_thread(service.imported_edit, workspace_id, request))

    @app.get("/v1/workspaces/{workspace_id}/imported-report/edits/{batch_id}")
    async def imported_edit_status(workspace_id: str, batch_id: str) -> object:
        return await asyncio.to_thread(service.editing.status, workspace_id, batch_id)

    @app.post("/v1/workspaces/{workspace_id}/imported-report/edits/{batch_id}/cancel")
    async def imported_edit_cancel(workspace_id: str, batch_id: str) -> object:
        return await asyncio.to_thread(service.editing.cancel, workspace_id, batch_id)

    @app.post("/v1/workspaces/{workspace_id}/imported-report/undo")
    async def imported_undo(workspace_id: str, request: ImportedUndoRequest) -> JSONResponse:
        return answer(await asyncio.to_thread(service.imported_undo, workspace_id, request))

    @app.get("/v1/workspaces/{workspace_id}/imported-report/history")
    async def imported_history(workspace_id: str) -> object:
        return await asyncio.to_thread(service.editing.history, workspace_id)

    @app.get("/v1/workspaces/{workspace_id}/imported-report/history/{revision_id}/discard-preview")
    async def imported_discard_preview(workspace_id: str, revision_id: str) -> object:
        return await asyncio.to_thread(service.editing.discard_preview, workspace_id, revision_id)

    @app.post("/v1/workspaces/{workspace_id}/imported-report/restore")
    async def imported_restore(workspace_id: str, request: ImportedRestoreRequest) -> JSONResponse:
        return answer(await asyncio.to_thread(service.imported_restore, workspace_id, request))

    @app.post("/v1/workspaces/{workspace_id}/imported-report/save-copy")
    async def imported_save_copy(
        workspace_id: str, request: ImportedSaveCopyRequest
    ) -> JSONResponse:
        return answer(await asyncio.to_thread(service.imported_save_copy, workspace_id, request))

    @app.post("/v1/workspaces/{workspace_id}/imported-report/replace-original")
    async def imported_replace_original(
        workspace_id: str, request: ImportedReplaceOriginalRequest
    ) -> JSONResponse:
        return answer(
            await asyncio.to_thread(service.imported_replace_original, workspace_id, request)
        )

    @app.post("/v1/workspaces/{workspace_id}/imported-report/reread-data")
    async def imported_reread_data(
        workspace_id: str, request: ImportedRereadRequest
    ) -> JSONResponse:
        return answer(
            await asyncio.to_thread(
                service.reread_imported_data, workspace_id, request.idempotency_key
            )
        )

    @app.get("/v1/workspaces/{workspace_id}/imported-report/model")
    async def imported_model_facts(
        workspace_id: str, table: str | None = Query(default=None, min_length=1, max_length=512)
    ) -> object:
        # The model workbench's full facts (Power Query in full); never chat context.
        return await asyncio.to_thread(service.editing.full_model_facts, workspace_id, table)

    @app.post("/v1/workspaces/{workspace_id}/imported-report/model/validate-query")
    async def validate_imported_model_query(
        workspace_id: str, request: ModelQueryCheckRequest
    ) -> object:
        # The checks a Power Query edit gets, without the edit; the M is never logged.
        return await asyncio.to_thread(service.editing.validate_model_query, workspace_id, request)

    # --- judging the report (docs/imported-report-editing-api.md, "Judgement") ---------------

    @app.post("/v1/workspaces/{workspace_id}/imported-report/pages/{page_id}/review")
    async def review_imported_page(
        workspace_id: str,
        page_id: str,
        request: ImportedReviewRequest,
        revision: str | None = Query(default=None, pattern=REVISION),
    ) -> object:
        return await asyncio.to_thread(
            service.review_imported_page, workspace_id, page_id, request, revision
        )

    @app.get("/v1/workspaces/{workspace_id}/imported-report/model-health")
    async def imported_model_health(workspace_id: str, stats: bool = False) -> object:
        return await asyncio.to_thread(
            lambda: service.imported_model_health(workspace_id, stats=stats)
        )

    @app.post("/v1/workspaces/{workspace_id}/imported-report/model-health/fix")
    async def imported_model_health_fix(
        workspace_id: str, request: ModelHealthFixRequest
    ) -> JSONResponse:
        return answer(
            await asyncio.to_thread(service.imported_model_health_fix, workspace_id, request)
        )

    @app.get("/v1/imported-report/model-health/rules")
    async def model_health_rules() -> object:
        return service.editing.model_health_rules()

    @app.get("/v1/workspaces/{workspace_id}/imported-report/tests")
    async def imported_tests(workspace_id: str) -> object:
        return await asyncio.to_thread(service.imported_report_tests, workspace_id)

    @app.post("/v1/workspaces/{workspace_id}/imported-report/tests")
    async def add_imported_test(workspace_id: str, request: AddReportTestRequest) -> object:
        return await asyncio.to_thread(service.add_imported_test, workspace_id, request)

    @app.post("/v1/workspaces/{workspace_id}/imported-report/tests/pin")
    async def pin_imported_test(workspace_id: str, request: PinReportTestRequest) -> object:
        return await asyncio.to_thread(service.pin_imported_test, workspace_id, request)

    @app.post("/v1/workspaces/{workspace_id}/imported-report/tests/run")
    async def run_imported_tests(workspace_id: str, request: RunReportTestsRequest) -> object:
        return await asyncio.to_thread(service.run_imported_tests, workspace_id, request)

    @app.get("/v1/workspaces/{workspace_id}/imported-report/tests/suggestions")
    async def suggest_imported_tests(
        workspace_id: str,
        page: Annotated[list[str] | None, Query(max_length=50)] = None,
    ) -> object:
        return await asyncio.to_thread(service.suggest_imported_tests, workspace_id, page)

    @app.patch("/v1/workspaces/{workspace_id}/imported-report/tests/{test_id}")
    async def update_imported_test(
        workspace_id: str, test_id: str, request: UpdateReportTestRequest
    ) -> object:
        return await asyncio.to_thread(service.update_imported_test, workspace_id, test_id, request)

    @app.delete("/v1/workspaces/{workspace_id}/imported-report/tests/{test_id}")
    async def delete_imported_test(workspace_id: str, test_id: str) -> object:
        return await asyncio.to_thread(service.delete_imported_test, workspace_id, test_id)

    @app.post("/v1/workspaces/{workspace_id}/imported-report/explain")
    async def explain_imported_number(workspace_id: str, request: ExplainNumberRequest) -> object:
        return await asyncio.to_thread(service.explain_number, workspace_id, request)

    @app.get("/v1/workspaces/{workspace_id}/imported-report/format-catalog")
    async def imported_format_catalog(
        workspace_id: str, visual_type: str = Query(min_length=1, max_length=80)
    ) -> object:
        return await asyncio.to_thread(service.editing.format_catalog, workspace_id, visual_type)

    @app.post("/v1/workspaces/{workspace_id}/open-source-report")
    async def open_source_report(workspace_id: str) -> JSONResponse:
        return answer(await asyncio.to_thread(service.open_imported_report_outcome, workspace_id))

    @app.get("/v1/browse")
    async def browse(path: str | None = Query(default=None, max_length=1000)) -> object:
        return await asyncio.to_thread(service.browse, path)

    @app.post("/v1/workspaces/{workspace_id}/dax/evaluate")
    async def evaluate_dax_expression(workspace_id: str, request: DaxEvaluateRequest) -> object:
        if request.workspace_id != workspace_id:
            raise HTTPException(status_code=422, detail="Workspace mismatch")
        return await asyncio.to_thread(service.evaluate_dax, request)

    @app.post("/v1/workspaces/{workspace_id}/dax/measures")
    async def add_dax_measure(workspace_id: str, request: AddDaxMeasureRequest) -> object:
        return await asyncio.to_thread(service.add_dax_measure, workspace_id, request)

    @app.get("/v1/workspaces")
    async def list_workspaces() -> object:
        return service.list_workspaces()

    @app.post("/v1/workspaces")
    async def create_workspace(request: CreateWorkspaceRequest) -> object:
        return service.create_workspace(request)

    @app.get("/v1/workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str) -> object:
        return service.workspace(workspace_id)

    @app.post("/v1/workspaces/{workspace_id}/folder")
    async def set_workspace_folder(workspace_id: str, request: SetWorkspaceFolderRequest) -> object:
        return await asyncio.to_thread(service.set_workspace_folder, workspace_id, request)

    @app.post("/v1/workspaces/{workspace_id}/name")
    async def rename_workspace(workspace_id: str, request: RenameWorkspaceRequest) -> object:
        return await asyncio.to_thread(service.rename_workspace, workspace_id, request)

    @app.post("/v1/workspaces/{workspace_id}/delete")
    async def delete_workspace(workspace_id: str, request: DeleteWorkspaceRequest) -> object:
        return await asyncio.to_thread(service.delete_workspace, workspace_id, request)

    @app.post("/v1/inventory")
    async def inventory(request: InventoryRequest) -> object:
        return await asyncio.to_thread(service.inventory, request.folder)

    @app.post("/v1/ingestions")
    async def ingest(request: IngestRequest) -> object:
        return await asyncio.to_thread(service.ingest, request)

    @app.get("/v1/snapshots/{snapshot_id}")
    async def get_snapshot(snapshot_id: str) -> object:
        return service.snapshot(snapshot_id)

    @app.post("/v1/drafts")
    async def create_draft(request: DraftRequest) -> object:
        return await asyncio.to_thread(service.create_draft, request)

    @app.post("/v1/snapshots/{snapshot_id}/preview")
    async def table_preview(snapshot_id: str, request: TablePreviewRequest) -> object:
        return await asyncio.to_thread(
            preview_table,
            service.snapshot(snapshot_id),
            request,
            service.state_root / "duckdb-spill",
        )

    @app.get("/v1/connections/drivers")
    async def connection_drivers() -> object:
        return drivers()

    @app.post("/v1/connections/browse")
    async def connection_browse(request: DatabaseBrowseRequest) -> object:
        return await asyncio.to_thread(browse_database, request)

    @app.post("/v1/connections/preview")
    async def connection_preview(request: DatabaseTableRequest) -> object:
        return await asyncio.to_thread(preview_database, request)

    @app.post("/v1/connections/import")
    async def connection_import(request: DatabaseImportRequest) -> object:
        return await asyncio.to_thread(service.import_database, request)

    @app.get("/v1/drafts/{draft_id}")
    async def get_draft(draft_id: str) -> object:
        return service.draft(draft_id)

    @app.post("/v1/drafts/{draft_id}/revisions")
    async def revise_draft(draft_id: str, request: ReviseDraftRequest) -> object:
        try:
            return await asyncio.to_thread(service.revise_draft, draft_id, request)
        except (StudioPlanningError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.put("/v1/drafts/{draft_id}/approval")
    async def approve_draft(draft_id: str, request: ApproveDraftRequest) -> object:
        return service.approve_draft(draft_id, request)

    @app.post("/v1/drafts/{draft_id}/restore")
    async def restore_draft(draft_id: str, request: RestoreDraftRequest) -> object:
        try:
            return await asyncio.to_thread(service.restore_draft, draft_id, request)
        except StudioPlanningError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.put("/v1/drafts/{draft_id}/dashboard")
    async def update_dashboard(
        draft_id: str,
        request: UpdateDashboardRequest,
    ) -> object:
        try:
            return await asyncio.to_thread(
                service.update_dashboard,
                draft_id,
                request,
            )
        except StudioPlanningError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.put("/v1/drafts/{draft_id}/measures/{measure_id}/approval")
    async def approve_advanced_dax(
        draft_id: str,
        measure_id: str,
        request: ApproveAdvancedDaxRequest,
    ) -> object:
        try:
            return service.approve_advanced_dax(draft_id, measure_id, request)
        except (StudioPlanningError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/v1/builds")
    async def build(request: BuildRequest) -> object:
        return await asyncio.to_thread(service.build, request)

    @app.get("/v1/workspaces/{workspace_id}/builds")
    async def builds(workspace_id: str) -> object:
        return service.builds(workspace_id)

    @app.get("/v1/workspaces/{workspace_id}/events")
    async def events(workspace_id: str, after: int = 0) -> object:
        return service.events(workspace_id, after)

    @app.get("/v1/workspaces/{workspace_id}/approvals")
    async def approvals(workspace_id: str) -> object:
        return service.approvals(workspace_id)

    @app.get("/v1/workspaces/{workspace_id}/messages")
    async def messages(
        workspace_id: str,
        after: int | None = Query(default=None, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> object:
        return service.messages(workspace_id, after=after, limit=limit)

    @app.post("/v1/workspaces/{workspace_id}/chat/clear")
    async def clear_chat(workspace_id: str, request: ClearChatRequest) -> object:
        return await asyncio.to_thread(service.clear_chat, workspace_id, request)

    @app.post("/v1/workspaces/{workspace_id}/dax/chat/clear")
    async def clear_dax_chat(workspace_id: str, request: ClearDaxChatRequest) -> object:
        return await asyncio.to_thread(service.clear_dax_chat, workspace_id, request)

    def assistant_stream(
        request: ChatRequest | DaxAssistRequest | ChatEditChoiceRequest | ChatEditConfirmRequest,
        http_request: Request,
        *,
        source: Callable[[Event], Iterator[dict[str, Any]]] | None = None,
        workspace_id: str | None = None,
    ) -> StreamingResponse:
        # ``source`` makes the events of a request that is not a chat message (the answer to
        # a question an imported-report edit asked); it streams the same protocol.
        async def stream() -> AsyncIterator[str]:
            cancel = Event()
            iterator = None
            pending = None
            try:
                if source is not None:
                    iterator = source(cancel)
                elif isinstance(request, DaxAssistRequest):
                    iterator = dax_assistant_events(service, request, cancel)
                elif isinstance(request, ChatRequest):
                    iterator = service.chat_events(request, cancel)
                else:
                    raise ValueError("This request needs its own event source")
                while True:
                    if await http_request.is_disconnected():
                        break
                    pending = asyncio.create_task(asyncio.to_thread(_next_chunk, iterator))
                    while not pending.done():
                        done, _ = await asyncio.wait(
                            {pending},
                            timeout=CHAT_HEARTBEAT_SECONDS,
                        )
                        if done:
                            break
                        if await http_request.is_disconnected():
                            return
                        yield _sse("heartbeat", {"state": "generating"})
                    has_chunk, chunk = await pending
                    if not has_chunk:
                        break
                    yield _sse(str(chunk["event"]), chunk["data"])
                yield _sse("done", {"cancelled": await http_request.is_disconnected()})
            except Exception as error:
                if not isinstance(error, _CLIENT_SAFE_ERRORS):
                    LOGGER.exception(
                        "Chat stream failed for workspace %s",
                        workspace_id or getattr(request, "workspace_id", ""),
                    )
                yield _sse("error", {"message": _client_error_message(error)})
            finally:
                cancel.set()
                # A generator cannot be closed while its worker is advancing it.
                if iterator is not None:
                    close = getattr(iterator, "close", None)
                    if callable(close):
                        if pending is not None and not pending.done():
                            pending.add_done_callback(lambda _task: close())
                        else:
                            close()

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/v1/chat/stream")
    async def chat(request: ChatRequest, http_request: Request) -> StreamingResponse:
        return assistant_stream(request, http_request)

    async def edit_answer(
        workspace_id: str,
        request: ChatEditChoiceRequest | ChatEditConfirmRequest,
        http_request: Request,
        events: Callable[[Event], Iterator[dict[str, Any]]],
    ) -> Response:
        """The answer to a question an imported-report edit asked in chat. A client that asks
        for ``text/event-stream`` gets the chat protocol (progress while the change is made);
        any other gets one JSON body when it is done (``collect_answer``)."""
        if "text/event-stream" in http_request.headers.get("accept", ""):
            return assistant_stream(request, http_request, source=events, workspace_id=workspace_id)
        status, body = await asyncio.to_thread(lambda: collect_answer(events(Event())))
        return JSONResponse(status_code=status, content=body)

    @app.post("/v1/workspaces/{workspace_id}/chat/edit-choice")
    async def chat_edit_choice(
        workspace_id: str, request: ChatEditChoiceRequest, http_request: Request
    ) -> Response:
        return await edit_answer(
            workspace_id,
            request,
            http_request,
            lambda cancel: service.chat_edit_choice_events(workspace_id, request, cancel),
        )

    @app.post("/v1/workspaces/{workspace_id}/chat/edit-confirm")
    async def chat_edit_confirm(
        workspace_id: str, request: ChatEditConfirmRequest, http_request: Request
    ) -> Response:
        return await edit_answer(
            workspace_id,
            request,
            http_request,
            lambda cancel: service.chat_edit_confirm_events(workspace_id, request, cancel),
        )

    @app.post("/v1/dax/assist")
    async def dax_assist(request: DaxAssistRequest, http_request: Request) -> StreamingResponse:
        return assistant_stream(request, http_request)

    @app.post("/v1/compatibility")
    async def compatibility(request: CompatibilityRequest) -> object:
        return await asyncio.to_thread(service.compatibility, request.pbip_path)

    @app.post("/v1/builds/{build_id}/open")
    async def open_build(build_id: str, request: OpenBuildRequest) -> object:
        workspace = service.workspace(request.workspace_id)
        if not any(job.id == build_id for job in service.builds(workspace.id)):
            raise HTTPException(status_code=404, detail="Build does not belong to this workspace")
        if workspace.base_hash != request.expected_base_hash:
            raise StateConflictError("Workspace changed before Power BI launch")
        return await asyncio.to_thread(service.open_powerbi, build_id)

    @app.post("/v1/builds/{build_id}/export")
    async def export_build(build_id: str, request: ExportBuildRequest) -> object:
        return await asyncio.to_thread(service.export_build, build_id, request)

    @app.post("/v1/builds/{build_id}/relink")
    async def relink_build(build_id: str, request: RelinkBuildRequest) -> object:
        return await asyncio.to_thread(service.relink_build, build_id, request)

    @app.post("/v1/builds/{build_id}/undo")
    async def undo_build(build_id: str, request: UndoBuildRequest) -> object:
        return await asyncio.to_thread(service.undo_build, build_id, request)

    return app


def _sse(event: str, payload: object) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _next_chunk[Chunk](iterator: Iterator[Chunk]) -> tuple[bool, Chunk]:
    try:
        return True, next(iterator)
    except StopIteration:
        return False, None  # type: ignore[return-value]
