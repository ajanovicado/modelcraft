# ruff: noqa: E501
"""Client-facing Streamlit workspace for Modelcraft."""

from __future__ import annotations

import html
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from typing import Any, Literal, cast

import altair as alt
import streamlit as st

from ai_powerbi_analyst.analytics.compiler import AnalysisValidationError
from ai_powerbi_analyst.analytics.executor import AnalyticsExecutionError
from ai_powerbi_analyst.contracts import (
    DashboardPagePlan,
    DashboardPlan,
    EvidenceAnswer,
    ProposedDaxMeasure,
    RelationshipCandidate,
    ReportChangePlan,
    RouteIntent,
    SourceManifest,
    VisualPlan,
    VisualRoleBinding,
)
from ai_powerbi_analyst.data.onboarding import DataOnboardingError, UploadedSource
from ai_powerbi_analyst.jobs.journal import JobRecord, JobStatus
from ai_powerbi_analyst.llm.runtime import LocalModelError
from ai_powerbi_analyst.orchestration.conversation import ConversationState
from ai_powerbi_analyst.powerbi.semantic_model import (
    SemanticProjectError,
    validate_advanced_dax,
)
from ai_powerbi_analyst.preflight import (
    ToolProbe,
    _powerbi_desktop_running,
    collect_environment,
    probe_powerbi_desktop,
)
from ai_powerbi_analyst.ui.models import AppWorkspace
from ai_powerbi_analyst.ui.theme import APP_CSS
from ai_powerbi_analyst.ui.view_models import (
    contribution_bar_data,
    evidence_chart_frame,
    evidence_slot_ready,
    human_label,
    idempotency_key_for_plan,
    page_summaries,
    plan_provenance,
    scenario_evidence_summary,
    trust_indicators,
)

_DEFAULT_PREFERENCES: dict[str, Any] = {
    "default_page": "conversations",
    "show_project_rail": True,
    "show_activity": True,
}
_NAV_ITEMS = (
    ("workspace", "Workspace", "⌂"),
    ("data_sources", "Data Sources", "▦"),
    ("conversations", "Conversations", "◌"),
    ("designer", "Dashboard Designer", "▥"),
    ("evidence", "Evidence", "▤"),
    ("settings", "Settings", "⚙"),
)


def _preferences_path() -> Path:
    project_root = Path(
        os.environ.get(
            "AI_POWERBI_PROJECT_ROOT",
            str(Path(__file__).resolve().parents[3]),
        )
    )
    state_root = Path(os.environ.get("AI_POWERBI_STATE_DIR", str(project_root / ".runtime")))
    return state_root / "ui_preferences.json"


def _workspace_paths() -> tuple[Path, Path]:
    project_root = Path(
        os.environ.get(
            "AI_POWERBI_PROJECT_ROOT",
            str(Path(__file__).resolve().parents[3]),
        )
    ).resolve(strict=True)
    state_root = Path(
        os.environ.get("AI_POWERBI_STATE_DIR", str(project_root / ".runtime"))
    ).resolve(strict=False)
    return project_root, state_root


@st.cache_resource(show_spinner=False)
def _user_services(project_root: str, state_root: str, mode: Literal["mock", "real"]) -> Any:
    from ai_powerbi_analyst.workspace.runtime import build_user_workspace_services

    return build_user_workspace_services(project_root, state_root, mode=mode)


def _ui_mode() -> Literal["mock", "real"]:
    mode = os.environ.get("AI_POWERBI_UI_MODE", "mock").casefold()
    if mode not in {"mock", "real"}:
        raise RuntimeError("AI_POWERBI_UI_MODE must be 'mock' or 'real'")
    return cast(Literal["mock", "real"], mode)


def _services() -> Any:
    project_root, state_root = _workspace_paths()
    return _user_services(str(project_root), str(state_root), _ui_mode())


def _load_preferences() -> dict[str, Any]:
    preferences = dict(_DEFAULT_PREFERENCES)
    path = _preferences_path()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return preferences
    if not isinstance(loaded, dict):
        return preferences
    default_page = loaded.get("default_page")
    if default_page in {item[0] for item in _NAV_ITEMS}:
        preferences["default_page"] = default_page
    for key in ("show_project_rail", "show_activity"):
        if isinstance(loaded.get(key), bool):
            preferences[key] = loaded[key]
    return preferences


def _save_preferences(preferences: dict[str, Any]) -> None:
    path = _preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".tmp")
    staging.write_text(
        json.dumps(preferences, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    staging.replace(path)


def _svg(name: str) -> str:
    paths = {
        "brand": '<rect x="3" y="12" width="4" height="9" fill="currentColor" stroke="none"/><rect x="10" y="7" width="4" height="14" fill="currentColor" stroke="none"/><rect x="17" y="3" width="4" height="18" fill="currentColor" stroke="none"/>',
        "home": '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10.5V21h13V10.5M9 21v-6h6v6"/>',
        "chat": '<path d="M4 5.5h12a3 3 0 0 1 3 3v5a3 3 0 0 1-3 3H9l-4.5 3v-3.2A3 3 0 0 1 2 13.5v-5a3 3 0 0 1 2-3Z"/><path d="M19 10h1a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-1v2l-3-2h-3"/>',
        "document": '<path d="M6 2h8l5 5v15H6z"/><path d="M14 2v6h5M9 13h7M9 17h7"/>',
        "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/>',
        "page": '<path d="M3 20h18M5 17V9h4v8M11 17V4h4v13M17 17v-6h4v6"/>',
    }
    path = paths[name]
    fill = 'fill="none"' if name != "brand" else 'fill="currentColor"'
    return f'<svg viewBox="0 0 24 24" width="24" height="24" {fill} stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{path}</svg>'


def _initialize(workspace: AppWorkspace) -> None:
    fallback_conversation = workspace.initial_conversation or ConversationState(
        project_id=workspace.config.project_id
    )
    conversation = workspace.orchestrator.restore_state(
        workspace.config.project_id,
        fallback_conversation,
    )
    if workspace.initial_evidence is not None and conversation.last_evidence is None:
        conversation = conversation.record_evidence(
            workspace.initial_evidence,
            analysis_plan=workspace.initial_analysis_plan,
        )
    live_plan = workspace.initial_plan
    if workspace.initial_prompt:
        try:
            live_plan = workspace.orchestrator.planner.plan(
                workspace.initial_prompt,
                ConversationState(
                    project_id=workspace.config.project_id,
                    last_route=RouteIntent.BUILD,
                ),
            )
        except (KeyError, RuntimeError, ValueError):
            live_plan = workspace.initial_plan
    live_active_plan = (
        live_plan
        if isinstance(live_plan, ReportChangePlan)
        and workspace.orchestrator.report_plan_installed(live_plan)
        else None
    )
    live_undo_available = workspace.orchestrator.undo_available
    last_question = next(
        (turn.content for turn in reversed(conversation.turns) if turn.role == "user"),
        "Revenue is up, so why did margin fall in the West last quarter?",
    )
    preferences = _load_preferences()
    initial_evidence_history = (
        [{"question": last_question, "answer": conversation.last_evidence}]
        if conversation.last_evidence is not None
        else []
    )
    try:
        initial_manifest = _services().onboarding.load_latest()
    except DataOnboardingError:
        initial_manifest = None
    defaults: dict[str, Any] = {
        "conversation": conversation,
        "prompt_text": workspace.initial_prompt,
        "pending_plan": (None if live_active_plan is not None else live_plan),
        "active_report_plan": live_active_plan,
        "active_plan_history": [],
        "last_evidence": conversation.last_evidence,
        "last_question": last_question,
        "validated": live_active_plan is not None,
        "grounded": (
            conversation.last_evidence is not None
            or getattr(conversation.last_route, "value", None) == "EXPLAIN"
        ),
        "undo_available": live_undo_available,
        "last_job_id": None,
        "pending_evidence_review": False,
        "last_interpretation": None,
        "active_page": preferences["default_page"],
        "preferences": preferences,
        "settings_saved": False,
        "evidence_history": initial_evidence_history,
        "activity": [
            {
                "title": "Indexed",
                "copy": "Prepared data and semantic catalog loaded",
                "time": "Ready",
            },
            {
                "title": "Planned",
                "copy": "Three-page report reviewed and approved",
                "time": "Now",
            },
        ],
        "source_manifest": initial_manifest,
        "relationship_candidates": (
            _services().onboarding.propose_relationships(initial_manifest)
            if initial_manifest is not None
            else []
        ),
        "dashboard_plan": None,
        "user_pbip_path": None,
        "user_project_validated": False,
        "user_validation_receipt": None,
        "local_ai_history": [],
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _header(workspace: AppWorkspace) -> None:
    active_plan = st.session_state.get("active_report_plan")
    fixture_reveal_ready = (
        workspace.config.can_open_power_bi
        and bool(st.session_state.validated)
        and isinstance(active_plan, ReportChangePlan)
        and workspace.orchestrator.report_plan_installed(active_plan)
    )
    user_pbip = st.session_state.get("user_pbip_path")
    mode = _ui_mode()
    desktop_probe = (
        probe_powerbi_desktop()
        if mode == "real"
        else ToolProbe(name="powerbi-desktop", available=False)
    )
    user_reveal_ready = (
        isinstance(user_pbip, str)
        and Path(user_pbip).is_file()
        and bool(st.session_state.get("user_project_validated"))
        and desktop_probe.available
        and not _powerbi_desktop_running()
    )
    reveal_ready = fixture_reveal_ready or user_reveal_ready
    reveal_help = (
        "Open the validated local PBIP in Power BI Desktop"
        if reveal_ready
        else (
            "Close Power BI Desktop before revealing the validated local project"
            if mode == "real" and isinstance(user_pbip, str) and _powerbi_desktop_running()
            else "Import data, approve a dashboard plan, and build a validated PBIP first"
        )
    )
    left, middle, action = st.columns([3.4, 6.4, 2.4])
    with left:
        st.markdown(
            f'<div class="app-topbar"><div class="brand">{_svg("brand")}<span>Modelcraft</span></div></div>',
            unsafe_allow_html=True,
        )
    with middle:
        date_label = workspace.config.data_through.strftime("%b %d, %Y")
        st.markdown(
            '<div class="project-meta">'
            f"<strong>{html.escape(workspace.config.project_name)}</strong>"
            '<span>⌄</span><span class="meta-divider"></span>'
            f"<span>Data through: {date_label}</span></div>",
            unsafe_allow_html=True,
        )
    with action:
        st.markdown('<span class="top-action-marker"></span>', unsafe_allow_html=True)
        if st.button(
            "Open in Power BI",
            key="open_power_bi",
            disabled=not reveal_ready,
            help=reveal_help,
            width="stretch",
        ):
            if user_reveal_ready:
                result = _open_user_project(Path(cast(str, user_pbip)), desktop_probe.path)
                if result[0]:
                    st.toast("Opening the validated report in Power BI Desktop")
                else:
                    st.toast(result[1], icon="ℹ️")
                return
            active_hash = (
                active_plan.base_report_hash
                if isinstance(active_plan, ReportChangePlan)
                else "none"
            )
            job = _run_progress(
                "Preparing Power BI Desktop reveal",
                lambda listener: workspace.orchestrator.open_power_bi(
                    workspace.config.project_id,
                    idempotency_key=(
                        f"open-{workspace.config.project_id}-{active_hash}-{uuid.uuid4()}"
                    ),
                    progress_listener=listener,
                ),
                success_label="Power BI Desktop launch confirmed",
            )
            st.session_state.last_job_id = job.id
            if job.status is JobStatus.SUCCEEDED:
                st.toast("Opening the validated report in Power BI Desktop")
            else:
                st.toast(job.error or "Desktop launch is unavailable", icon="ℹ️")


def _open_user_project(pbip_path: Path, desktop_path: str | None) -> tuple[bool, str]:
    if _ui_mode() == "mock":
        return False, "Power BI Desktop launch is unavailable in fixture mode"
    if desktop_path is None or not Path(desktop_path).is_file():
        return False, "Power BI Desktop is not discoverable"
    if _powerbi_desktop_running():
        return False, "Close Power BI Desktop before opening the validated project"
    try:
        health = _services().model.health()
        if health.service_available:
            _services().model.unload()
        subprocess.Popen(  # noqa: S603 - paths are validated local artifacts
            (desktop_path, str(pbip_path.resolve(strict=True))),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except (OSError, LocalModelError, ValueError) as error:
        return False, str(error)
    return True, str(pbip_path)


def _navigate(page: str) -> None:
    st.session_state.active_page = page
    st.rerun()


def _navigation() -> None:
    active_page = st.session_state.active_page
    st.markdown(
        '<div class="left-rail-marker" aria-hidden="true"></div>'
        '<h2 class="sr-only">Primary navigation</h2>',
        unsafe_allow_html=True,
    )
    for page, label, icon in _NAV_ITEMS:
        if st.button(
            f"{icon}  {label}",
            key=f"nav_{page}",
            type="primary" if page == active_page else "secondary",
            width="stretch",
        ):
            _navigate(page)


def _page_header(title: str, copy: str, *, eyebrow: str = "LOCAL ANALYST") -> None:
    st.markdown(
        '<header class="page-header">'
        f'<div class="page-eyebrow">{html.escape(eyebrow)}</div>'
        f"<h1>{html.escape(title)}</h1>"
        f"<p>{html.escape(copy)}</p></header>",
        unsafe_allow_html=True,
    )


def _workspace_plan() -> ReportChangePlan | None:
    active = st.session_state.get("active_report_plan")
    if isinstance(active, ReportChangePlan):
        return active
    pending = st.session_state.get("pending_plan")
    return pending if isinstance(pending, ReportChangePlan) else None


def _render_workspace(workspace: AppWorkspace) -> None:
    _page_header(
        "Workspace",
        "Project health, report structure, and the next actions available in this local workspace.",
        eyebrow=workspace.config.project_name,
    )
    report_state = "Validated" if st.session_state.validated else "Plan ready"
    evidence_count = len(st.session_state.evidence_history)
    data_through = workspace.config.data_through.strftime("%b %d, %Y")
    report_detail = (
        "Ready for Power BI" if st.session_state.validated else "Awaiting approved apply"
    )
    st.markdown(
        '<section class="summary-grid" aria-label="Workspace summary">'
        f'<div class="summary-card"><span>Report state</span><strong>{report_state}</strong><small>{report_detail}</small></div>'
        f'<div class="summary-card"><span>Data through</span><strong>{data_through}</strong><small>Prepared local dataset</small></div>'
        f'<div class="summary-card"><span>Evidence records</span><strong>{evidence_count}</strong><small>Governed analytical answers</small></div>'
        "</section>",
        unsafe_allow_html=True,
    )

    plan = _workspace_plan()
    st.markdown('<h2 class="section-heading">Report pages</h2>', unsafe_allow_html=True)
    if plan is None:
        st.info("No report plan is available yet. Start a conversation to create one.")
    else:
        cards = "".join(
            '<article class="workspace-page-card">'
            f"<span>{index}</span><div><strong>{html.escape(page.title)}</strong>"
            f"<p>{html.escape(page.description)}</p></div></article>"
            for index, page in enumerate(page_summaries(plan), start=1)
        )
        st.markdown(f'<div class="workspace-page-grid">{cards}</div>', unsafe_allow_html=True)

    st.markdown('<h2 class="section-heading">Quick actions</h2>', unsafe_allow_html=True)
    conversation_action, evidence_action = st.columns(2, gap="small")
    with conversation_action:
        if st.button(
            "Continue analysis",
            key="workspace_open_conversations",
            type="primary",
            width="stretch",
        ):
            _navigate("conversations")
    with evidence_action:
        if st.button(
            "Review evidence",
            key="workspace_open_evidence",
            disabled=evidence_count == 0,
            width="stretch",
        ):
            _navigate("evidence")

    st.markdown('<h2 class="section-heading">Recent activity</h2>', unsafe_allow_html=True)
    recent = "".join(
        '<div class="workspace-activity"><span class="activity-dot">✓</span><div>'
        f"<strong>{html.escape(item['title'])}</strong><p>{html.escape(item['copy'])}</p>"
        f"</div><time>{html.escape(item['time'])}</time></div>"
        for item in reversed(st.session_state.activity[-5:])
    )
    st.markdown(f'<div class="workspace-activity-list">{recent}</div>', unsafe_allow_html=True)


def _render_data_sources() -> None:
    _page_header(
        "Data Sources",
        "Create a versioned local snapshot, inspect its profile, and review relationship proposals.",
    )
    uploads = st.file_uploader(
        "CSV or Excel files",
        type=["csv", "xlsx", "xlsm"],
        accept_multiple_files=True,
        help="Files are copied into a content-addressed local snapshot. Excel macros are never executed.",
    )
    if st.button(
        "Import and profile",
        type="primary",
        disabled=not uploads,
        key="import_sources",
    ):
        try:
            with st.status("Snapshotting and profiling local data", expanded=True) as status:
                status.write("Hashing selected files and creating immutable snapshots")
                manifest = _services().onboarding.import_uploads(
                    UploadedSource(name=item.name, content=item.getvalue()) for item in uploads
                )
                status.write("Inferring data types, keys, and bounded samples")
                relationships = _services().onboarding.propose_relationships(manifest)
                status.update(label="Local data profile ready", state="complete", expanded=False)
            st.session_state.source_manifest = manifest
            st.session_state.relationship_candidates = relationships
            st.session_state.dashboard_plan = None
            st.session_state.user_project_validated = False
            st.session_state.activity.append(
                {
                    "title": "Imported",
                    "copy": f"Profiled {len(manifest.tables)} local table(s)",
                    "time": "Now",
                }
            )
            st.rerun()
        except DataOnboardingError as error:
            st.error(str(error))

    manifest = st.session_state.get("source_manifest")
    if not isinstance(manifest, SourceManifest):
        st.info("Import at least one CSV or Excel file to create a real semantic-model workspace.")
        return

    source_rows = [
        {
            "File": source.original_name,
            "Type": source.kind.value.upper(),
            "Size (MB)": round(source.size_bytes / 1024**2, 2),
            "SHA-256": source.sha256[:16] + "…",
            "Snapshot": source.snapshot_path,
        }
        for source in manifest.sources
    ]
    st.subheader("Versioned snapshot")
    st.dataframe(source_rows, hide_index=True, width="stretch")
    with st.expander("Relink a source to its original local file", expanded=False):
        source_id = st.selectbox(
            "Source",
            [source.id for source in manifest.sources],
            format_func=lambda selected: next(
                source.original_name for source in manifest.sources if source.id == selected
            ),
            key="relink_source_id",
        )
        selected_source = next(source for source in manifest.sources if source.id == source_id)
        original_path = st.text_input(
            "Original file path",
            value=selected_source.original_path or "",
            key="relink_original_path",
        )
        if st.button(
            "Review original path",
            disabled=not original_path.strip(),
            key="review_relink_path",
        ):
            try:
                updated_manifest = _services().onboarding.relink_source(
                    manifest,
                    source_id,
                    original_path,
                )
                st.session_state.source_manifest = updated_manifest
                st.success("Original path matches the reviewed snapshot hash.")
                st.rerun()
            except (DataOnboardingError, OSError) as error:
                st.error(str(error))
        all_linked = all(source.original_path for source in manifest.sources)
        pbip_path = st.session_state.get("user_pbip_path")
        confirm_relink = st.checkbox(
            "Update the built Power BI project to read from the reviewed original paths",
            key="confirm_project_relink",
            disabled=not all_linked or not isinstance(pbip_path, str),
        )
        if st.button(
            "Relink validated PBIP",
            disabled=not confirm_relink,
            key="apply_project_relink",
        ):
            try:
                receipt = _services().projects.relink(cast(str, pbip_path), manifest)
                st.session_state.user_validation_receipt = receipt
                st.session_state.user_project_validated = receipt.passed
                st.success("Power Query paths were relinked and the project revalidated.")
            except (SemanticProjectError, OSError, ValueError) as error:
                st.error(str(error))

    st.subheader("Table profiles")
    for table in manifest.tables:
        with st.expander(f"{table.name} · {table.row_count:,} rows", expanded=False):
            st.dataframe(
                [
                    {
                        "Column": column.name,
                        "Type": column.data_type.value,
                        "Nulls": column.null_count,
                        "Distinct": (
                            column.distinct_count
                            if column.distinct_count_exact
                            else f">={column.distinct_count}"
                        ),
                        "Candidate key": column.candidate_key,
                        "Samples": ", ".join(str(value) for value in column.sample_values[:4]),
                    }
                    for column in table.columns
                ],
                hide_index=True,
                width="stretch",
            )

    relationships = [
        item
        for item in st.session_state.get("relationship_candidates", [])
        if isinstance(item, RelationshipCandidate)
    ]
    st.subheader("Relationship review")
    if not relationships:
        st.caption("No cross-table relationship candidates were found.")
        return
    reviewable = [item for item in relationships if not item.blockers]
    approved = st.multiselect(
        "Approved relationships",
        [item.id for item in reviewable],
        default=[item.id for item in reviewable if item.review_status == "approved"],
        format_func=lambda relationship_id: _relationship_label(
            next(item for item in reviewable if item.id == relationship_id), manifest
        ),
        help="Only explicitly approved, non-blocked relationships can enter the semantic model.",
    )
    st.dataframe(
        [
            {
                "Candidate": _relationship_label(item, manifest),
                "Cardinality": item.cardinality.replace("_", " "),
                "Confidence": f"{item.confidence:.0%}",
                "Status": "Blocked" if item.blockers else item.review_status.title(),
                "Reason": "; ".join(item.blockers or item.evidence),
            }
            for item in relationships
        ],
        hide_index=True,
        width="stretch",
    )
    if st.button("Save relationship review", key="save_relationship_review"):
        approved_set = set(approved)
        st.session_state.relationship_candidates = [
            item.model_copy(
                update={
                    "review_status": (
                        "rejected"
                        if item.blockers
                        else "approved"
                        if item.id in approved_set
                        else "rejected"
                    )
                }
            )
            for item in relationships
        ]
        st.session_state.dashboard_plan = None
        st.success("Relationship review saved. Generate a new dashboard plan next.")


def _relationship_label(item: RelationshipCandidate, manifest: SourceManifest) -> str:
    tables = {table.id: table for table in manifest.tables}
    left = tables[item.from_table_id]
    right = tables[item.to_table_id]
    left_column = next(column for column in left.columns if column.id == item.from_column_id)
    right_column = next(column for column in right.columns if column.id == item.to_column_id)
    return f"{left.name}.{left_column.name} → {right.name}.{right_column.name}"


def _add_advanced_measure(plan: DashboardPlan, manifest: SourceManifest) -> None:
    with st.expander("Advanced AI-written DAX", expanded=False):
        st.caption(
            "Use this only when the governed measure operations are insufficient. "
            "The expression is parsed against the reviewed catalog and needs separate approval."
        )
        name = st.text_input("Measure name", key="advanced_measure_name")
        expression = st.text_area(
            "DAX expression",
            placeholder="DIVIDE([Total Revenue], [Total Cost])",
            key="advanced_measure_expression",
        )
        dependency_options = {
            f"{measure.name} · {measure.id}": measure.id for measure in plan.semantic_model.measures
        }
        selected_dependencies = st.multiselect(
            "Referenced measures",
            list(dependency_options),
            key="advanced_measure_dependencies",
        )
        approved = st.checkbox(
            "I approve this AI-written DAX expression as an advanced measure",
            key="advanced_measure_approval",
        )
        if st.button(
            "Add advanced measure",
            disabled=not approved or not name.strip() or not expression.strip(),
            key="add_advanced_measure",
        ):
            measure = ProposedDaxMeasure(
                id=f"advanced_{uuid.uuid4().hex[:12]}",
                name=name.strip(),
                expression=expression.strip(),
                dependencies=[dependency_options[item] for item in selected_dependencies],
                approved=True,
                assumptions=["The user separately approved this AI-written DAX expression."],
            )
            measures = [*plan.semantic_model.measures, measure]
            validate_advanced_dax(
                measure,
                manifest,
                {item.id for item in measures},
                {item.id: item.name for item in measures},
            )
            updated_semantic = plan.semantic_model.model_copy(
                update={"measures": measures, "approved": False}
            )
            st.session_state.dashboard_plan = plan.model_copy(
                update={"semantic_model": updated_semantic, "approved": False}
            )
            st.session_state.user_project_validated = False
            st.success(f"Added advanced measure: {measure.name}")
            st.rerun()


def _add_catalog_visual(plan: DashboardPlan, manifest: SourceManifest) -> None:
    catalog = tuple(item for item in _services().planner.visual_catalog if item.offline_capable)
    unavailable = tuple(
        item for item in _services().planner.visual_catalog if not item.offline_capable
    )
    with st.expander("Add any offline native visual", expanded=False):
        st.caption(
            f"{len(catalog)} Microsoft-catalogued visual types are available offline. "
            f"{len(unavailable)} online, external, or unsupported types are excluded."
        )
        by_label = {f"{item.display_name} · {item.visual_type}": item for item in catalog}
        selected_label = st.selectbox(
            "Native visual type",
            list(by_label),
            key="catalog_visual_type",
        )
        capability = by_label[selected_label]
        if capability.formatting_properties:
            st.caption(
                "Formatting catalog: "
                + ", ".join(capability.formatting_properties[:10])
                + ("…" if len(capability.formatting_properties) > 10 else "")
            )
        column_options: dict[str, tuple[str, str, Literal["column", "measure"]]] = {
            f"Column · {table.name}.{column.name}": (table.id, column.id, "column")
            for table in manifest.tables
            for column in table.columns
        }
        measure_options: dict[str, tuple[str, str, Literal["column", "measure"]]] = {
            f"Measure · {measure.name}": (
                getattr(measure, "table_id", manifest.tables[0].id),
                measure.id,
                "measure",
            )
            for measure in plan.semantic_model.measures
        }
        bindings: list[VisualRoleBinding] = []
        missing_required: list[str] = []
        for role in capability.roles:
            kind = capability.role_kinds.get(role, "GroupingOrMeasure")
            role_options = (
                column_options
                if kind == "Grouping"
                else measure_options
                if kind == "Measure"
                else {**column_options, **measure_options}
            )
            required = role in capability.required_roles
            options = list(role_options)
            if not required:
                options = ["None", *options]
            selected = st.selectbox(
                f"{role}{' · required' if required else ''}",
                options or ["No compatible reviewed field"],
                key=f"catalog_role_{capability.visual_type}_{role}",
            )
            if not role_options:
                if required:
                    missing_required.append(role)
                continue
            if selected == "None":
                continue
            table_id, field_id, field_kind = role_options[selected]
            bindings.append(
                VisualRoleBinding(
                    role=role,
                    table_id=table_id,
                    field_id=field_id,
                    field_kind=field_kind,
                )
            )
        title = st.text_input(
            "Visual title",
            value=capability.display_name,
            key=f"catalog_title_{capability.visual_type}",
        )
        if st.button(
            "Add visual to dashboard",
            disabled=bool(missing_required) or not title.strip(),
            key=f"add_catalog_visual_{capability.visual_type}",
        ):
            target_index = next(
                (index for index, item in enumerate(plan.pages) if len(item.visuals) < 6),
                None,
            )
            pages = list(plan.pages)
            if target_index is None:
                if len(pages) >= 8:
                    st.error("The dashboard already has the maximum eight pages.")
                    return
                index = 0
            else:
                index = len(pages[target_index].visuals)
            visual = VisualPlan(
                id=f"visual_{uuid.uuid4().hex[:12]}",
                visual_type=capability.visual_type,
                title=title.strip(),
                bindings=bindings,
                x=(index % 2) * 630,
                y=(index // 2) * 230,
                width=610,
                height=210,
            )
            if target_index is None:
                updated_page = DashboardPagePlan(
                    id=f"page_{uuid.uuid4().hex[:12]}",
                    title=f"Analysis {len(pages) + 1}",
                    visuals=[visual],
                )
                pages.append(updated_page)
            else:
                page = pages[target_index]
                updated_page = page.model_copy(update={"visuals": [*page.visuals, visual]})
                pages[target_index] = updated_page
            st.session_state.dashboard_plan = plan.model_copy(
                update={"pages": pages, "approved": False}
            )
            st.session_state.user_project_validated = False
            st.success(
                f"Added {capability.display_name} to {updated_page.title} using exact catalog roles."
            )
            st.rerun()


def _render_dashboard_designer() -> None:
    _page_header(
        "Dashboard Designer",
        "Describe the report, review every semantic and visual binding, then build a staged PBIP project.",
    )
    manifest = st.session_state.get("source_manifest")
    if not isinstance(manifest, SourceManifest):
        st.info("Import and profile data before designing a dashboard.")
        if st.button("Open Data Sources", key="designer_open_sources"):
            _navigate("data_sources")
        return
    prompt = st.text_area(
        "Dashboard request",
        value=(
            "Build an exploratory dashboard showing current performance, trends over time, "
            "important category comparisons, and source detail."
        ),
        height=110,
        key="dashboard_request",
    )
    model_health = _services().model.health()
    model_label = (
        f"Local AI ready · {model_health.identity.name}"
        if model_health.model_available and model_health.identity is not None
        else "Deterministic fallback · local model unavailable"
    )
    st.caption(model_label)
    if st.button("Generate review plan", type="primary", key="generate_dashboard_plan"):
        relationships = [
            item
            for item in st.session_state.get("relationship_candidates", [])
            if isinstance(item, RelationshipCandidate)
        ]
        try:
            with st.status("Planning locally", expanded=True) as status:
                status.write("Binding the request to reviewed tables, fields, and visual types")
                plan = _services().planner.plan(prompt, manifest, relationships)
                status.update(
                    label="Dashboard plan ready for review", state="complete", expanded=False
                )
            st.session_state.dashboard_plan = plan
            st.session_state.user_project_validated = False
            st.rerun()
        except (LocalModelError, ValueError) as error:
            st.error(str(error))

    plan = st.session_state.get("dashboard_plan")
    if not isinstance(plan, DashboardPlan):
        return
    if plan.generation_source == "local_model":
        st.success(
            "Plan source: pinned local Qwen model · exact bindings compiled deterministically"
        )
        metrics = _services().model.last_generation
        if metrics is not None:
            st.caption(
                f"Generation: {metrics.generated_tokens} tokens · "
                f"{metrics.tokens_per_second:.2f} tokens/s · "
                f"{metrics.total_seconds:.2f}s total"
            )
            if 0 < metrics.tokens_per_second < 6:
                st.error(
                    "This sample fell below the 6 tokens/s qualification gate. "
                    "The Phi fallback must be evaluated before changing the pinned model."
                )
            elif metrics.tokens_per_second < 8:
                st.warning(
                    "Local generation is in the accepted 6–8 tokens/s warning band. "
                    "The plan remains reviewable and no project write occurs automatically."
                )
    else:
        st.warning(
            "Plan source: deterministic fallback · the local model timed out or failed strict validation"
        )
    st.subheader("Semantic model")
    semantic_rows = []
    for table_plan in plan.semantic_model.tables:
        table = next(table for table in manifest.tables if table.id == table_plan.table_id)
        semantic_rows.append(
            {
                "Table": table_plan.display_name,
                "Rows": table.row_count,
                "Columns": len(table.columns),
                "Measures": sum(
                    1
                    for measure in plan.semantic_model.measures
                    if getattr(measure, "table_id", None) == table.id
                ),
            }
        )
    st.dataframe(semantic_rows, hide_index=True, width="stretch")
    if plan.semantic_model.relationships:
        st.caption(
            f"{len(plan.semantic_model.relationships)} approved relationship(s) will be written."
        )
    else:
        st.caption("No relationships will be written until one is explicitly approved.")

    st.subheader("Report pages and visual bindings")
    for page in plan.pages:
        with st.expander(f"{page.title} · {len(page.visuals)} visuals", expanded=True):
            st.dataframe(
                [
                    {
                        "Visual": visual.title,
                        "Type": visual.visual_type,
                        "Bindings": ", ".join(
                            f"{binding.role}: {binding.table_id}.{binding.field_id}"
                            for binding in visual.bindings
                        ),
                        "Position": f"{visual.x},{visual.y} · {visual.width}×{visual.height}",
                    }
                    for visual in page.visuals
                ],
                hide_index=True,
                width="stretch",
            )
    _add_advanced_measure(plan, manifest)
    _add_catalog_visual(plan, manifest)
    approved = st.checkbox(
        "I reviewed the source snapshot, relationships, measures, pages, and visual bindings",
        key="approve_dashboard_plan",
    )
    project_name = st.text_input("Power BI project name", value="Local AI Dashboard")
    desktop_running = _powerbi_desktop_running()
    if desktop_running:
        st.warning(
            "Power BI Desktop is open. Planning and review remain available, but project "
            "writes are locked until Desktop is closed."
        )
    if st.button(
        "Build validated PBIP",
        type="primary",
        disabled=not approved or desktop_running,
        key="build_user_pbip",
    ):
        approved_plan = plan.model_copy(
            update={
                "approved": True,
                "semantic_model": plan.semantic_model.model_copy(update={"approved": True}),
            }
        )
        try:
            with st.status("Building Power BI project", expanded=True) as status:
                status.write("Compiling approved Power Query, TMDL, and PBIR artifacts")
                pbip_path, receipt = _services().projects.build(
                    project_name,
                    manifest,
                    approved_plan,
                )
                status.write("Verifying source hashes, semantic references, JSON, and layout")
                status.update(
                    label="Validated PBIP project committed", state="complete", expanded=False
                )
            st.session_state.dashboard_plan = approved_plan
            st.session_state.user_pbip_path = str(pbip_path)
            st.session_state.user_validation_receipt = receipt
            st.session_state.user_project_validated = receipt.passed
            st.session_state.activity.append(
                {"title": "Built", "copy": f"Validated {pbip_path.name}", "time": "Now"}
            )
            st.success(f"Power BI project ready: {pbip_path}")
            st.rerun()
        except (SemanticProjectError, OSError, ValueError) as error:
            st.session_state.user_project_validated = False
            st.error(str(error))
    pbip_path = st.session_state.get("user_pbip_path")
    if isinstance(pbip_path, str) and Path(pbip_path).is_file():
        st.success(f"Validated project: {pbip_path}")
        if _powerbi_desktop_running():
            st.warning("Close Power BI Desktop to enable the Open in Power BI action.")


def _render_local_model_chat() -> None:
    health = _services().model.health()
    if not health.model_available:
        st.warning(health.error or "The configured local model is not ready.")
        return
    for message in st.session_state.local_ai_history:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    with st.form("local_ai_chat_form", border=False):
        message = st.text_area(
            "Message the local model",
            placeholder="Describe the dashboard you want or ask about the imported schema…",
            height=90,
        )
        submitted = st.form_submit_button("Send to local AI", type="primary")
    if not submitted or not message.strip():
        return
    manifest = st.session_state.get("source_manifest")
    source_context = (
        manifest.model_dump_json(
            exclude={"sources": {"__all__": {"snapshot_path", "original_path"}}}
        )
        if isinstance(manifest, SourceManifest)
        else "No user dataset has been imported."
    )
    system_prompt = (
        "You are the local planning assistant inside a Power BI authoring workspace. "
        "Use only the supplied reviewed schema. Do not invent values, formulas, completed "
        "writes, or Power BI validation results. Explain proposed actions; the application "
        "will require review before any write. Reviewed schema: " + source_context[:12000]
    )
    st.session_state.local_ai_history.append({"role": "user", "content": message})
    with st.chat_message("assistant"):
        try:
            response = st.write_stream(
                _services().model.stream_chat(message, system_prompt=system_prompt)
            )
        except LocalModelError as error:
            st.error(str(error))
            return
    if isinstance(response, str):
        st.session_state.local_ai_history.append({"role": "assistant", "content": response})
    st.rerun()


def _append_evidence_record(question: str, answer: EvidenceAnswer) -> None:
    history = st.session_state.evidence_history
    if any(
        record["answer"].query_plan_hash == answer.query_plan_hash
        for record in history
        if isinstance(record, dict) and isinstance(record.get("answer"), EvidenceAnswer)
    ):
        return
    history.append({"question": question, "answer": answer})


def _render_evidence_library(workspace: AppWorkspace) -> None:
    _page_header(
        "Evidence",
        "Browse grounded answers, inspect their data proof, and manage approved report insertions.",
    )
    manifest = st.session_state.get("source_manifest")
    dashboard = st.session_state.get("dashboard_plan")
    receipt = st.session_state.get("user_validation_receipt")
    if isinstance(manifest, SourceManifest):
        st.subheader("Local build evidence")
        st.dataframe(
            [
                {
                    "Source": source.original_name,
                    "SHA-256": source.sha256,
                    "Snapshot": source.snapshot_path,
                    "Imported": manifest.imported_at,
                }
                for source in manifest.sources
            ],
            hide_index=True,
            width="stretch",
        )
        with st.expander("Source profile and semantic plan", expanded=False):
            st.json(manifest.model_dump(mode="json"), expanded=False)
            if isinstance(dashboard, DashboardPlan):
                st.json(dashboard.model_dump(mode="json"), expanded=False)
        if receipt is not None:
            passed = bool(getattr(receipt, "passed", False))
            st.success("PBIP validation receipt passed") if passed else st.error(
                "PBIP validation receipt failed"
            )
            st.json(receipt.model_dump(mode="json"), expanded=False)
        _project_root, state_root = _workspace_paths()
        attestation_path = state_root / "model-attestation.json"
        if attestation_path.is_file():
            with st.expander("Pinned local model identity", expanded=False):
                try:
                    st.json(
                        json.loads(attestation_path.read_text(encoding="utf-8")),
                        expanded=False,
                    )
                except (OSError, json.JSONDecodeError):
                    st.warning("The model attestation could not be read.")

    st.subheader("Governed analytical evidence")
    history = [
        record
        for record in st.session_state.evidence_history
        if isinstance(record, dict) and isinstance(record.get("answer"), EvidenceAnswer)
    ]
    if not history:
        st.markdown("**No evidence yet**")
        st.caption("Ask a numerical, driver, forecast, or scenario question to create one.")
        if st.button(
            "Start an analysis",
            key="evidence_open_conversations",
            type="primary",
        ):
            _navigate("conversations")
        return

    options = list(range(len(history) - 1, -1, -1))
    selected = st.selectbox(
        "Evidence record",
        options,
        format_func=lambda index: (
            f"{history[index]['answer'].chart.title} — {history[index]['question']}"
        ),
        key="selected_evidence_record",
    )
    record = history[selected]
    _render_evidence(
        workspace,
        record["answer"],
        question=record["question"],
    )


def _render_settings(workspace: AppWorkspace) -> None:
    _page_header(
        "Settings",
        "Choose how this local workspace opens and which project context stays visible.",
    )
    preferences = st.session_state.preferences
    st.markdown('<h2 class="section-heading">Interface preferences</h2>', unsafe_allow_html=True)
    with st.form("ui_preferences", border=True):
        page_labels = {item[0]: item[1] for item in _NAV_ITEMS}
        default_pages = list(page_labels)
        default_page = st.selectbox(
            "Default landing view",
            default_pages,
            index=default_pages.index(preferences["default_page"]),
            format_func=lambda page: page_labels[page],
            key="preference_default_page",
        )
        show_project_rail = st.toggle(
            "Show project status rail",
            value=bool(preferences["show_project_rail"]),
            key="preference_show_project_rail",
            help="Keeps approved metrics, compatibility, activity, and trust status visible.",
        )
        show_activity = st.toggle(
            "Show recent activity in the status rail",
            value=bool(preferences["show_activity"]),
            key="preference_show_activity",
            disabled=not show_project_rail,
        )
        save = st.form_submit_button(
            "Save preferences",
            type="primary",
            width="stretch",
        )
    if save:
        updated = {
            "default_page": default_page,
            "show_project_rail": show_project_rail,
            "show_activity": show_activity,
        }
        _save_preferences(updated)
        st.session_state.preferences = updated
        st.session_state.settings_saved = True
        st.session_state.activity.append(
            {"title": "Settings", "copy": "Interface preferences saved locally", "time": "Now"}
        )
        st.rerun()
    if st.session_state.settings_saved:
        st.success("Preferences saved locally for the next app session.")

    if st.button("Reset interface preferences", key="reset_preferences"):
        reset = dict(_DEFAULT_PREFERENCES)
        _save_preferences(reset)
        st.session_state.preferences = reset
        st.session_state.settings_saved = True
        st.rerun()

    st.markdown('<h2 class="section-heading">Connection</h2>', unsafe_allow_html=True)
    mode = os.environ.get("AI_POWERBI_UI_MODE", "mock").casefold()
    st.markdown(
        '<section class="connection-card">'
        "<div><span>Engine</span><strong>Local AI + PBIP authoring</strong></div>"
        f"<div><span>Workspace data</span><strong>{'Fixture demo until Desktop attestation' if mode == 'mock' else 'Attested project'}</strong></div>"
        f"<div><span>Project</span><strong>{html.escape(workspace.config.project_name)}</strong></div>"
        f"<div><span>Compatibility</span><strong>{html.escape(workspace.config.compatibility_label)}</strong></div>"
        "</section>",
        unsafe_allow_html=True,
    )
    st.markdown('<h2 class="section-heading">Local runtime health</h2>', unsafe_allow_html=True)
    if _ui_mode() == "mock":
        st.info(
            "Fixture mode uses prepared examples. Local model and Power BI tool checks are disabled."
        )
        return
    environment = collect_environment()
    model_health = _services().model.health()
    desktop = environment.tools["powerbi-desktop"]
    st.dataframe(
        [
            {
                "Component": "Ollama",
                "Status": "Ready" if model_health.service_available else "Unavailable",
                "Identity": model_health.endpoint,
            },
            {
                "Component": "Local model",
                "Status": "Ready" if model_health.model_available else "Missing",
                "Identity": (
                    f"{model_health.identity.name} · {(model_health.identity.digest or '')[:16]}…"
                    if model_health.identity is not None
                    else _services().settings.model.name
                ),
            },
            {
                "Component": "Power BI Desktop",
                "Status": "Open"
                if environment.desktop_running
                else "Ready"
                if desktop.available
                else "Missing",
                "Identity": desktop.path or "Not discovered",
            },
            {
                "Component": "pbi-cli",
                "Status": "Ready" if environment.tools["pbi"].available else "Missing",
                "Identity": environment.tools["pbi"].distribution_version
                or environment.tools["pbi"].version
                or "",
            },
            {
                "Component": "Microsoft PBIR validator",
                "Status": "Ready"
                if environment.tools["powerbi-report-author"].available
                else "Missing",
                "Identity": environment.tools["powerbi-report-author"].version or "",
            },
            {
                "Component": "Microsoft Desktop bridge",
                "Status": "Ready"
                if environment.tools["powerbi-desktop-bridge"].available
                else "Missing",
                "Identity": environment.tools["powerbi-desktop-bridge"].version or "",
            },
        ],
        hide_index=True,
        width="stretch",
    )
    # Memory is informational only; it never pauses or blocks the local model.
    st.caption(
        f"Available memory: {environment.available_memory_gb:.2f} GiB of "
        f"{environment.total_memory_gb:.2f} GiB"
    )
    _project_root, state_root = _workspace_paths()
    attestation_path = state_root / "model-attestation.json"
    if attestation_path.is_file():
        st.caption(f"Model attestation: `{attestation_path}`")
    if st.button("Record current model attestation", key="record_model_attestation"):
        try:
            attestation = _services().model.write_attestation(attestation_path)
            st.success(
                f"Recorded {attestation.identity.name} · "
                f"{attestation.identity.digest or 'digest unavailable'}"
            )
        except LocalModelError as error:
            st.error(str(error))
    if st.button(
        "Unload local model memory",
        disabled=not model_health.service_available,
        key="unload_local_model",
    ):
        try:
            _services().model.unload()
            st.success("Local model memory released.")
        except LocalModelError as error:
            st.error(str(error))


def _plan_html(workspace: AppWorkspace, plan: ReportChangePlan) -> str:
    rows: list[str] = []
    for page in page_summaries(plan):
        metrics = ", ".join(page.metric_labels) or "Approved metrics"
        dimensions = ", ".join(page.dimension_labels) or "Report context"
        rows.append(
            '<div class="plan-row">'
            f'<div class="page-number">{page.number}</div>'
            f'<div class="page-icon">{_svg("page")}</div>'
            '<div class="page-copy">'
            f'<div class="page-title">{html.escape(page.title)}</div>'
            f'<div class="page-description">{html.escape(page.description)}</div></div>'
            f'<div class="plan-detail">Metrics<strong>{html.escape(metrics)}</strong></div>'
            f'<div class="plan-detail">Filters / grouping<strong>{html.escape(dimensions)}</strong></div>'
            "</div>"
        )
    provenance = "".join(
        '<div class="plan-provenance-item" '
        f'title="{html.escape(item.detail, quote=True)}">'
        f"<span>{html.escape(item.label)}</span>{html.escape(item.value)}</div>"
        for item in plan_provenance(
            plan,
            forecast_version=workspace.config.forecast_version,
            approved_price_change_pct=workspace.config.approved_price_change_pct,
            price_elasticity=workspace.config.price_elasticity,
        )
    )
    return (
        '<h2 class="plan-heading">Reviewed plan '
        '<span class="approved"><span class="check">✓</span>Approved</span></h2>'
        f'<div class="plan-count">{len(plan.pages)} pages</div>'
        '<div class="plan-provenance" aria-label="Plan assumptions and provenance">'
        f"{provenance}</div>"
        f'<div class="plan-list">{"".join(rows)}</div>'
    )


def _run_progress(
    action_label: str,
    callback: Callable[[Callable[[str, str, float], None]], JobRecord],
    *,
    success_label: str = "Validated and saved",
) -> JobRecord:
    progress = st.progress(0, text=f"{action_label}…")
    status = st.status(action_label, expanded=True)

    def listener(_phase: str, message: str, value: float) -> None:
        progress.progress(value, text=message)
        status.write(message)

    result = callback(listener)
    if result.status is JobStatus.SUCCEEDED:
        progress.progress(1.0, text=success_label)
        status.update(label=success_label, state="complete", expanded=False)
    else:
        status.update(label=result.error or "The operation failed safely", state="error")
    return result


def _job_result(job: Any) -> dict[str, Any]:
    if not job.result_json:
        return {}
    value = json.loads(job.result_json)
    return value if isinstance(value, dict) else {}


def _merge_active_report_plan(
    current: ReportChangePlan | None,
    applied: ReportChangePlan,
    report_hash: str,
) -> ReportChangePlan:
    if current is None:
        return applied.model_copy(update={"base_report_hash": report_hash})
    updated_by_key = {page.key: page for page in applied.pages}
    pages = [updated_by_key.pop(page.key, page) for page in current.pages]
    pages.extend(updated_by_key.values())
    return current.model_copy(
        update={
            "base_report_hash": report_hash,
            "pages": pages,
            "style_preset": applied.style_preset,
            "assumptions": applied.assumptions,
        }
    )


def _record_report_hash(workspace: AppWorkspace, report_hash: str) -> None:
    conversation = st.session_state.conversation.record_report_hash(report_hash)
    st.session_state.conversation = conversation
    workspace.orchestrator.persist_state(conversation)


def _render_plan(workspace: AppWorkspace, plan: ReportChangePlan) -> None:
    prompt = st.session_state.prompt_text or "Build the approved three-page executive report."
    st.markdown(
        '<div class="prompt-row"><div class="avatar">◯</div>'
        f'<div class="prompt-bubble">{html.escape(prompt)}</div></div>',
        unsafe_allow_html=True,
    )
    st.markdown(_plan_html(workspace, plan), unsafe_allow_html=True)
    if st.button("Apply to Power BI", type="primary", key="apply_report"):
        was_validated = bool(st.session_state.validated)
        had_undo = bool(st.session_state.undo_available)
        key = idempotency_key_for_plan(workspace.config.project_id, plan)
        job = _run_progress(
            "Applying approved report plan",
            lambda listener: workspace.orchestrator.apply_plan(
                workspace.config.project_id,
                plan,
                idempotency_key=key,
                progress_listener=listener,
            ),
        )
        st.session_state.last_job_id = job.id
        if job.status is JobStatus.SUCCEEDED:
            st.session_state.validated = True
            result = _job_result(job)
            st.session_state.undo_available = bool(result.get("undo_available", False))
            report_hash = result.get("report_hash")
            if isinstance(report_hash, str):
                if bool(result.get("changed", False)):
                    st.session_state.active_plan_history.append(
                        st.session_state.get("active_report_plan")
                    )
                st.session_state.active_report_plan = _merge_active_report_plan(
                    st.session_state.get("active_report_plan"),
                    plan,
                    report_hash,
                )
                st.session_state.pending_plan = None
                _record_report_hash(workspace, report_hash)
            st.session_state.activity.append(
                {"title": "Applied", "copy": "Three pages validated and saved", "time": "Now"}
            )
            st.rerun()
        else:
            st.session_state.validated = was_validated
            st.session_state.undo_available = had_undo
            st.session_state.last_message = (
                "The report change was not applied. The previous validated report and "
                "Undo state were preserved."
            )


def _render_active_report_status(plan: ReportChangePlan) -> None:
    pages = " · ".join(page.title for page in plan.pages)
    st.markdown(
        '<section class="report-ready" aria-label="Validated report ready">'
        '<div class="report-ready-head"><span class="check">✓</span>'
        "Report ready for analysis</div>"
        '<div class="report-ready-copy">The governed three-page report is validated, '
        "saved, and reversible.</div>"
        f'<div class="report-ready-pages">{html.escape(pages)}</div>'
        '<div class="report-ready-next">Ask a numerical or explanatory question below. '
        "Supported evidence can be reviewed and added to the reserved Margin Drivers slot."
        "</div></section>",
        unsafe_allow_html=True,
    )


def _evidence_chart(answer: EvidenceAnswer) -> alt.Chart:
    data = contribution_bar_data(answer)
    x_field = data.x_field
    y_field = data.y_field
    return cast(
        alt.Chart,
        alt.Chart(data.frame)
        .mark_bar(size=15, cornerRadiusEnd=1)
        .encode(
            x=alt.X(
                f"{x_field}:Q",
                title=None,
                stack=None,
                scale=alt.Scale(domain=list(data.domain), nice=False, zero=False),
                axis=alt.Axis(grid=False, labels=True, ticks=True, format="~s"),
            ),
            y=alt.Y(f"{y_field}:N", title=None, sort=None, axis=alt.Axis(labelColor="#414b5d")),
            color=alt.condition(
                f"datum['{x_field}'] >= 0",
                alt.value("#1d58b7"),
                alt.value("#f36a21"),
            ),
            tooltip=[alt.Tooltip(f"{y_field}:N"), alt.Tooltip(f"{x_field}:Q", format="+.1f")],
        )
        .properties(height=196)
        .configure_view(strokeWidth=0)
        .configure_axis(domainColor="#aab3c1", labelFont="Segoe UI", labelFontSize=12),
    )


def _render_evidence_visual(answer: EvidenceAnswer) -> None:
    scenario = scenario_evidence_summary(answer)
    if scenario is not None:
        cards = "".join(
            '<div class="scenario-card">'
            f'<div class="scenario-card-label">{html.escape(card.label)}</div>'
            f'<div class="scenario-card-value">{html.escape(card.value)}</div>'
            f'<div class="scenario-card-delta">{html.escape(card.delta)}</div></div>'
            for card in scenario.cards
        )
        st.markdown(
            '<div class="scenario-panel" role="group" aria-label="Governed scenario evidence">'
            f'<div class="scenario-card-grid">{cards}</div>'
            '<div class="scenario-context"><strong>Baseline</strong>'
            f"{html.escape(scenario.baseline)}</div>"
            '<div class="scenario-context scenario-assumptions"><strong>Assumptions</strong>'
            f"{html.escape(scenario.assumptions)}</div></div>",
            unsafe_allow_html=True,
        )
        return
    frame = evidence_chart_frame(answer)
    if answer.chart.kind == "forecast":
        columns = [
            column
            for column in (
                "period",
                "metric_id",
                "point_forecast",
                "lower_80",
                "upper_80",
                "lower_95",
                "upper_95",
                "method",
            )
            if column in frame.columns
        ]
        forecast = frame.loc[:, columns].rename(
            columns={
                "period": "Period",
                "metric_id": "Metric",
                "point_forecast": "Point forecast",
                "lower_80": "80% low",
                "upper_80": "80% high",
                "lower_95": "95% low",
                "upper_95": "95% high",
                "method": "Approved model",
            }
        )
        st.dataframe(forecast, hide_index=True, width="stretch")
        st.caption(
            "Monthly point estimates with approved 80% and 95% uncertainty ranges; "
            "quarterly summed bounds are descriptive, not calibrated aggregate-quarter intervals."
        )
        return
    if answer.chart.kind in {"metric", "table"}:
        st.dataframe(frame, hide_index=True, width="stretch")
        return
    if answer.chart.kind == "line":
        x_field = answer.chart.x_field or "period"
        y_field = answer.chart.y_field or "value"
        color_field = answer.chart.color_field
        encodings: dict[str, Any] = {
            "x": alt.X(f"{x_field}:T", title=None),
            "y": alt.Y(f"{y_field}:Q", title=None),
            "tooltip": [
                alt.Tooltip(x_field),
                alt.Tooltip(y_field, format=",.1f"),
            ],
        }
        if color_field:
            encodings["color"] = alt.Color(f"{color_field}:N", title=None)
        chart = (
            alt.Chart(frame)
            .mark_line(point=True, strokeWidth=2.2)
            .encode(**encodings)
            .properties(height=196)
            .configure_view(strokeWidth=0)
        )
        st.altair_chart(chart, width="stretch")
        return
    try:
        chart = _evidence_chart(answer)
    except ValueError:
        st.info("No finite governed values are available for this evidence chart.")
        return
    st.altair_chart(chart, width="stretch")


def _apply_evidence(workspace: AppWorkspace, answer: EvidenceAnswer) -> bool:
    report_plan = st.session_state.get("active_report_plan")
    if not isinstance(report_plan, ReportChangePlan):
        raise RuntimeError("there is no reviewed active report plan")
    was_validated = bool(st.session_state.validated)
    had_undo = bool(st.session_state.undo_available)
    key = (
        f"evidence-{workspace.config.project_id}-{answer.query_plan_hash}-"
        f"{report_plan.base_report_hash}"
    )
    job = _run_progress(
        "Adding grounded evidence",
        lambda listener: workspace.orchestrator.apply_evidence(
            workspace.config.project_id,
            answer,
            idempotency_key=key,
            report_plan=report_plan,
            analysis_plan=st.session_state.conversation.last_analysis_plan,
            progress_listener=listener,
        ),
    )
    st.session_state.last_job_id = job.id
    if job.status is not JobStatus.SUCCEEDED:
        st.session_state.validated = was_validated
        st.session_state.undo_available = had_undo
        st.session_state.last_message = (
            "Evidence was not added. The previous validated report and Undo remain available."
        )
        return False

    st.session_state.validated = True
    result = _job_result(job)
    st.session_state.undo_available = bool(result.get("undo_available", False))
    report_hash = result.get("report_hash")
    if isinstance(report_hash, str):
        if bool(result.get("changed", False)):
            st.session_state.active_plan_history.append(report_plan)
        st.session_state.active_report_plan = report_plan.model_copy(
            update={"base_report_hash": report_hash}
        )
        st.session_state.pending_plan = None
        _record_report_hash(workspace, report_hash)
    st.session_state.pending_evidence_review = False
    st.session_state.activity.append(
        {
            "title": "Evidence added",
            "copy": "Margin Drivers slot updated and validated",
            "time": "Now",
        }
    )
    return True


def _render_evidence(
    workspace: AppWorkspace,
    answer: EvidenceAnswer,
    *,
    question: str | None = None,
) -> None:
    st.markdown('<div class="answer-wrap">', unsafe_allow_html=True)
    question_copy = html.escape(question or st.session_state.last_question)
    conclusion = html.escape(answer.conclusion)
    facts = "".join(f"<li>{html.escape(fact)}</li>" for fact in answer.facts[:8])
    st.markdown(
        '<div class="answer-head"><div class="bot-avatar">AI</div><div>'
        f'<h2 class="answer-question">{question_copy}</h2>'
        f'<div class="answer-text">{conclusion}<ul class="evidence-facts">{facts}</ul></div>'
        f'<div class="evidence-title">{html.escape(answer.chart.title)}</div></div></div>',
        unsafe_allow_html=True,
    )
    chart_col, meta_col = st.columns([3.4, 1.4])
    with chart_col:
        _render_evidence_visual(answer)
    with meta_col:
        filters = "<br>".join(
            f"{html.escape(key.replace('_', ' ').title())}: {html.escape(str(value))}"
            for key, value in answer.filters.items()
        )
        metric_labels = " · ".join(
            html.escape(metric_id.replace("_", " ").title()) for metric_id in answer.metric_ids
        )
        definition_title = html.escape(" | ".join(answer.metric_definition_refs), quote=True)
        active_plan = st.session_state.get("active_report_plan")
        installed_slot_ready = (
            isinstance(active_plan, ReportChangePlan)
            and evidence_slot_ready(
                active_plan,
                validated=bool(st.session_state.validated),
            )
            and workspace.orchestrator.evidence_slot_ready(active_plan)
        )
        st.markdown(
            '<div class="evidence-meta"><strong>Comparison period</strong><br>'
            f"{html.escape(answer.comparison_period)}<br><br><strong>Filters</strong><br>"
            f"{filters or 'None'}<br><br><strong>Data through</strong><br>"
            f"{html.escape(answer.data_through.isoformat() if answer.data_through else 'Prepared fixture')}<br><br>"
            f'<strong>Metric definitions</strong><br><span title="{definition_title}">'
            f"{metric_labels}</span><br><br>"
            f"<strong>Data proof</strong><br>{html.escape(answer.data_hash[:12])}…<br><br>"
            f"<strong>Catalog proof</strong><br>{html.escape(answer.catalog_hash[:12])}…<br><br>"
            f"<strong>Query proof</strong><br>{html.escape(answer.query_plan_hash[:12])}…</div>",
            unsafe_allow_html=True,
        )
        if st.button(
            "Add to Margin Drivers",
            key="add_evidence",
            disabled=not answer.add_to_report_supported or not installed_slot_ready,
            width="stretch",
        ):
            st.session_state.pending_evidence_review = True
            st.rerun()
        if st.session_state.pending_evidence_review:
            grouping = answer.chart.y_field or answer.chart.x_field or "approved grouping"
            review_filters = (
                " · ".join(f"{human_label(key)}: {value}" for key, value in answer.filters.items())
                or "None"
            )
            st.markdown(
                '<div class="evidence-review"><strong>Review change</strong><br>'
                f"Metric: {html.escape(', '.join(answer.metric_ids))}<br>"
                f"Grouping: {html.escape(grouping)}<br>"
                f"Filters: {html.escape(review_filters)}<br>"
                f"Chart: {html.escape(answer.chart.kind.replace('_', ' '))}<br>"
                "Destination: Margin Drivers · reserved evidence slot</div>",
                unsafe_allow_html=True,
            )
            apply_clicked = st.button(
                "Apply evidence",
                type="primary",
                key="apply_evidence",
                width="stretch",
            )
            if apply_clicked and _apply_evidence(workspace, answer):
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def _handle_prompt(workspace: AppWorkspace) -> None:
    prompt = st.chat_input("Ask a follow-up question…", key="follow_up")
    if not prompt:
        return
    st.session_state.prompt_text = prompt
    st.session_state.last_question = prompt
    st.session_state.pop("last_explanation", None)
    st.session_state.pop("last_message", None)
    st.session_state.last_interpretation = None
    try:
        outcome = _review_with_progress(
            workspace,
            prompt,
            st.session_state.conversation,
        )
    except (
        AnalysisValidationError,
        AnalyticsExecutionError,
        EOFError,
        TimeoutError,
    ):
        failed_state = (
            st.session_state.conversation.record_message("user", prompt)
            .record_route(RouteIntent.CLARIFY)
            .clear_evidence()
        )
        workspace.orchestrator.persist_state(failed_state)
        st.session_state.conversation = failed_state
        st.session_state.last_evidence = None
        st.session_state.grounded = False
        st.session_state.pending_evidence_review = False
        st.session_state.last_message = (
            "Analysis stopped safely because the governed local query could not complete. "
            "No report changes were made; please retry."
        )
        st.rerun()
        return
    st.session_state.conversation = outcome.state
    st.session_state.last_interpretation = outcome.interpretation
    if outcome.plan is not None:
        st.session_state.pending_plan = outcome.plan
    if outcome.evidence is not None:
        st.session_state.last_evidence = outcome.evidence
        _append_evidence_record(prompt, outcome.evidence)
        st.session_state.grounded = True
        requested_review = bool(outcome.requires_review)
        active_plan = st.session_state.get("active_report_plan")
        installed_slot_ready = (
            isinstance(active_plan, ReportChangePlan)
            and evidence_slot_ready(
                active_plan,
                validated=bool(st.session_state.validated),
            )
            and workspace.orchestrator.evidence_slot_ready(active_plan)
        )
        st.session_state.pending_evidence_review = (
            requested_review and installed_slot_ready and outcome.evidence.add_to_report_supported
        )
        if requested_review and not outcome.evidence.add_to_report_supported:
            st.session_state.last_message = (
                "This governed evidence is not approved for report insertion. "
                "The existing validated report was not changed."
            )
        elif requested_review and not installed_slot_ready:
            st.session_state.last_message = (
                "Apply and validate the reviewed report before adding evidence to its "
                "reserved Margin Drivers slot."
            )
    elif outcome.decision.intent.value != "ADD_EVIDENCE":
        # Never render an earlier chart as though it answered the new prompt.
        st.session_state.last_evidence = None
        st.session_state.grounded = outcome.explanation is not None
        st.session_state.pending_evidence_review = False
    if outcome.explanation:
        st.session_state.last_explanation = outcome.explanation
    if outcome.message:
        st.session_state.last_message = outcome.message
    st.rerun()


def _review_with_progress(
    workspace: AppWorkspace,
    prompt: str,
    conversation: ConversationState,
) -> Any:
    """Keep the Streamlit main thread responsive while local interpretation runs."""

    status = st.status("Interpreting and grounding locally", expanded=True)
    status.write("Resolving the request against approved local metrics and operations")
    completed = Event()
    outcome: dict[str, Any] = {}
    failure: dict[str, Exception] = {}

    def review() -> None:
        try:
            outcome["value"] = workspace.orchestrator.review(prompt, conversation)
        except Exception as error:  # noqa: BLE001 - worker failure is surfaced to the review result
            failure["error"] = error
        finally:
            completed.set()

    worker = Thread(target=review, name="local-request-review", daemon=True)
    worker.start()
    next_update = time.monotonic() + 4.0
    while not completed.wait(timeout=0.2):
        if time.monotonic() >= next_update:
            status.write("Still grounding the request; no report files are being changed")
            next_update = time.monotonic() + 4.0
    worker.join(timeout=0.1)
    if "error" in failure:
        status.update(label="Local review stopped safely", state="error")
        raise failure["error"]
    status.update(label="Local interpretation grounded", state="complete", expanded=False)
    return outcome["value"]


def _right_rail(workspace: AppWorkspace) -> None:
    metric_lines = "".join(
        f'<div class="metric-line"><span class="check">✓</span>{html.escape(metric)}</div>'
        for metric in workspace.config.approved_metrics
    )
    activity = "".join(
        '<div class="activity-item"><div class="activity-dot">✓</div><div>'
        f'<div class="activity-title">{html.escape(item["title"])}</div>'
        f'<div class="activity-copy">{html.escape(item["copy"])}</div></div>'
        f'<div class="activity-time">{html.escape(item["time"])}</div></div>'
        for item in st.session_state.activity[-4:]
    )
    indicators = trust_indicators(
        is_local=True,
        grounded=bool(st.session_state.grounded),
        validated=bool(st.session_state.validated),
        undo_available=bool(st.session_state.undo_available),
    )
    trust = "".join(
        '<div class="trust-line"><span>✓</span>'
        f"<span>{html.escape(item.label)}</span>"
        f'<span class="trust-value {"ok" if item.ok else "pending"}">{html.escape(item.value)}</span></div>'
        for item in indicators
    )
    activity_section = ""
    if st.session_state.preferences.get("show_activity", True):
        activity_section = (
            f'<div class="rail-divider"></div><h2 class="right-heading">Activity</h2>{activity}'
        )
    st.markdown(
        '<aside class="right-rail" aria-label="Trust and project status">'
        '<h2 class="right-heading">Approved metrics</h2>'
        f'{metric_lines}<div class="rail-divider"></div>'
        '<h2 class="right-heading">Project compatibility</h2>'
        f'<div class="metric-line"><span class="check">✓</span>{html.escape(workspace.config.compatibility_label)}</div>'
        f'<div class="compat-detail">{html.escape(workspace.config.compatibility_detail)}</div>'
        f'{activity_section}<div class="rail-divider"></div>{trust}</aside>',
        unsafe_allow_html=True,
    )
    if st.button(
        "Undo",
        key="undo_report",
        disabled=not st.session_state.undo_available,
        width="stretch",
    ):
        job = _run_progress(
            "Restoring previous report",
            lambda listener: workspace.orchestrator.undo(
                workspace.config.project_id,
                idempotency_key=f"undo-{uuid.uuid4()}",
                progress_listener=listener,
            ),
        )
        if job.status is JobStatus.SUCCEEDED:
            result = _job_result(job)
            restored_hash = result.get("report_hash")
            if isinstance(restored_hash, str):
                history = st.session_state.active_plan_history
                previous = history.pop() if history else None
                candidate = (
                    previous if isinstance(previous, ReportChangePlan) else workspace.initial_plan
                )
                restored_plan = (
                    candidate.model_copy(update={"base_report_hash": restored_hash})
                    if isinstance(candidate, ReportChangePlan)
                    else None
                )
                if restored_plan is not None and workspace.orchestrator.report_plan_installed(
                    restored_plan
                ):
                    st.session_state.active_report_plan = restored_plan
                    st.session_state.pending_plan = None
                else:
                    st.session_state.active_report_plan = None
                    st.session_state.pending_plan = restored_plan
                _record_report_hash(workspace, restored_hash)
            st.session_state.undo_available = bool(result.get("undo_available", False))
            st.session_state.validated = True
            st.session_state.pending_evidence_review = False
            st.session_state.activity.append(
                {"title": "Restored", "copy": "Previous valid report restored", "time": "Now"}
            )
            st.rerun()


def _render_conversations(workspace: AppWorkspace) -> None:
    _page_header(
        "Conversations",
        "Plan report changes and ask questions grounded in approved local metrics.",
    )
    governed_tab, local_ai_tab = st.tabs(["Governed analysis", "Local AI"])
    with governed_tab:
        plan = st.session_state.pending_plan
        interpretation = st.session_state.get("last_interpretation")
        if interpretation is not None:
            prefix = (
                "Local AI interpretation"
                if getattr(interpretation, "source", "") == "local_model"
                else "Local interpretation"
            )
            st.caption(f"{prefix}: {interpretation.summary}")
        if isinstance(plan, ReportChangePlan):
            _render_plan(workspace, plan)
        evidence = st.session_state.last_evidence
        active_plan = st.session_state.get("active_report_plan")
        if (
            not isinstance(plan, ReportChangePlan)
            and isinstance(active_plan, ReportChangePlan)
            and not isinstance(evidence, EvidenceAnswer)
        ):
            _render_active_report_status(active_plan)
        if isinstance(evidence, EvidenceAnswer):
            _render_evidence(workspace, evidence)
        explanation = st.session_state.get("last_explanation")
        if explanation:
            st.info(explanation)
        message = st.session_state.get("last_message")
        if message:
            st.warning(message)
        _handle_prompt(workspace)
    with local_ai_tab:
        _render_local_model_chat()


def _render_current_page(workspace: AppWorkspace) -> None:
    page = st.session_state.active_page
    if page == "workspace":
        _render_workspace(workspace)
    elif page == "data_sources":
        _render_data_sources()
    elif page == "designer":
        _render_dashboard_designer()
    elif page == "evidence":
        _render_evidence_library(workspace)
    elif page == "settings":
        _render_settings(workspace)
    else:
        _render_conversations(workspace)


def render_app(workspace: AppWorkspace) -> None:
    st.set_page_config(
        page_title="Modelcraft — Your AI workspace for Power BI",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(APP_CSS, unsafe_allow_html=True)
    _initialize(workspace)
    _header(workspace)

    show_project_rail = bool(st.session_state.preferences.get("show_project_rail", True))
    if show_project_rail:
        nav, main, right = st.columns([1.8, 7.65, 2.45])
    else:
        nav, main = st.columns([1.8, 10.1])
    with nav:
        _navigation()
    with main:
        page_label = next(
            label for page, label, _icon in _NAV_ITEMS if page == st.session_state.active_page
        )
        st.markdown(
            f'<main class="main-workspace-marker" aria-label="{html.escape(page_label)}">'
            '<h1 class="sr-only">Modelcraft workspace</h1></main>',
            unsafe_allow_html=True,
        )
        _render_current_page(workspace)
    if show_project_rail:
        with right:
            _right_rail(workspace)


@st.cache_resource(show_spinner="Validating the local Power BI workspace…")
def _real_workspace_from_environment() -> AppWorkspace:
    """Assemble heavy real-mode services once; UI state is refreshed separately."""

    from ai_powerbi_analyst.ui.production_runtime import (
        build_real_workspace_from_environment,
    )

    return build_real_workspace_from_environment()


@st.cache_resource(show_spinner=False)
def _mock_workspace(project_root: str, state_root: str) -> AppWorkspace:
    from pathlib import Path

    from ai_powerbi_analyst.runtime import build_local_workspace

    return build_local_workspace(
        Path(project_root),
        Path(state_root),
        powerbi_mode="mock",
    )


def main() -> None:
    mode = os.environ.get("AI_POWERBI_UI_MODE", "mock").casefold()
    if mode == "mock":
        from pathlib import Path

        project_root = Path(
            os.environ.get(
                "AI_POWERBI_PROJECT_ROOT",
                str(Path(__file__).resolve().parents[3]),
            )
        )
        state_root = Path(os.environ.get("AI_POWERBI_STATE_DIR", str(project_root / ".runtime")))
        workspace = _mock_workspace(str(project_root), str(state_root))
    elif mode == "real":
        workspace = _real_workspace_from_environment()
    else:
        raise RuntimeError("AI_POWERBI_UI_MODE must be 'mock' or 'real'")
    render_app(workspace)


if __name__ == "__main__":
    main()
