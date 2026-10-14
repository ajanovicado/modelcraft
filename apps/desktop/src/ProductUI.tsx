import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { AskAiButton } from "./AskAiButton";
import type { ModelInstallState } from "./ModelInstallCard";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronDown,
  Circle,
  Clock3,
  Command,
  Copy,
  Database,
  Download,
  LoaderCircle,
  FileBarChart2,
  FileCheck2,
  FileSearch,
  Hash,
  Pencil,
  Search,
  ShieldCheck,
  Sparkles,
  Table2,
  Trash2,
  X,
} from "lucide-react";
import type { Build, Draft, Snapshot, StudioEvent, Workspace } from "./types";

const localDate = (value: string) => new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);

const BUILD_WORKFLOW_DETAIL: Record<Build["state"], string> = {
  queued: "Queued for local compilation",
  compiling: "Compiling the PBIP project",
  validating: "Running local validators",
  committed: "Validated and ready",
  failed: "Failed safely",
  rolled_back: "Rolled back to the previous project",
};

const DRAFT_BUILD_DETAIL: Record<Draft["status"], string> = {
  draft: "Approve the reviewed draft first",
  approved: "Ready to build",
  building: "Build preparation is in progress",
  validated: "Validated draft is ready",
  failed: "Resolve draft validation errors",
};

const APPROVED_DRAFT_STATES = new Set<Draft["status"]>(["approved", "building", "validated"]);

export type CommandAction = {
  id: string;
  label: string;
  description: string;
  icon: ReactNode;
  onSelect: () => void;
  keywords?: string;
  shortcut?: string;
};

export function CommandPalette({ open, actions, onClose }: { open: boolean; actions: CommandAction[]; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return actions;
    return actions.filter((action) => `${action.label} ${action.description} ${action.keywords || ""}`.toLowerCase().includes(needle));
  }, [actions, query]);

  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setQuery("");
    setActive(0);
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    return () => {
      cancelAnimationFrame(frame);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [open]);

  useEffect(() => setActive((value) => Math.min(value, Math.max(filtered.length - 1, 0))), [filtered.length]);
  if (!open) return null;

  const choose = (action?: CommandAction) => {
    if (!action) return;
    onClose();
    action.onSelect();
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((value) => Math.min(value + 1, filtered.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((value) => Math.max(value - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(filtered[active]);
    } else if (event.key === "Escape") {
      onClose();
    }
  };

  return (
    <div className="command-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="command-palette" role="dialog" aria-modal="true" aria-label="Search or run a command">
        <div className="command-input"><Search size={19} /><input ref={inputRef} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Tab") { event.preventDefault(); } onKeyDown(event); }} placeholder="Search or run a command" aria-label="Command search" /><kbd>Esc</kbd></div>
        <div className="command-list" role="listbox">
          {!filtered.length ? <div className="command-empty">No matching commands</div> : filtered.map((action, index) => (
            <button key={action.id} className={index === active ? "command-row active" : "command-row"} onMouseEnter={() => setActive(index)} onClick={() => choose(action)} onKeyDown={(event) => {
              if (event.key === "Tab") { event.preventDefault(); inputRef.current?.focus(); }
            }} role="option" aria-selected={index === active}>
              <span className="command-icon">{action.icon}</span><span><strong>{action.label}</strong><small>{action.description}</small></span>{action.shortcut && <kbd>{action.shortcut}</kbd>}
            </button>
          ))}
        </div>
        <footer><span><kbd>↑</kbd><kbd>↓</kbd> Navigate</span><span><kbd>↵</kbd> Run</span><span>Everything stays local</span></footer>
      </section>
    </div>
  );
}

export function WorkspaceMenu({ open, workspaces, activeId, onSelect, onCreate, onOpenReport, onRename, onDelete, onClose }: { open: boolean; workspaces: Workspace[]; activeId?: string; onSelect: (id: string) => void; onCreate: () => void; onOpenReport: () => void; onRename: (workspace: Workspace) => void; onDelete: (workspace: Workspace) => void; onClose: () => void }) {
  if (!open) return null;
  return <div className="workspace-menu" role="menu">
    <div className="workspace-menu-heading"><span>Local workspaces</span><button onClick={onClose} aria-label="Close workspace menu"><X size={15} /></button></div>
    {workspaces.map((item) => <div key={item.id} className={item.id === activeId ? "workspace-option active" : "workspace-option"}>
      <button type="button" className="workspace-option-select" onClick={() => onSelect(item.id)} role="menuitem"><span><strong>{item.name}</strong><small>{item.kind === "imported_report" ? "Imported Power BI report" : item.kind === "existing_pbip" ? "Existing PBIP" : "Local data project"}</small></span>{item.id === activeId && <Check size={16} />}</button>
      <button type="button" className="workspace-option-action" title={`Rename ${item.name}`} aria-label={`Rename ${item.name}`} onClick={() => onRename(item)}><Pencil size={13} /></button>
      <button type="button" className="workspace-option-action danger" title={`Delete ${item.name}`} aria-label={`Delete ${item.name}`} onClick={() => onDelete(item)}><Trash2 size={13} /></button>
    </div>)}
    <button className="workspace-create" onClick={onCreate}><Sparkles size={16} />New local workspace</button>
    <button className="workspace-create" onClick={onOpenReport}><FileBarChart2 size={16} />Open Power BI report</button>
  </div>;
}

/** Native dialog keeps focus trapping and Escape dismissal consistent with the review dialog. */
export function RenameWorkspaceDialog({ workspace, busy, onSubmit, onClose }: { workspace: Workspace; busy: boolean; onSubmit: (name: string) => void; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [name, setName] = useState(workspace.name);
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = ref.current;
    dialog?.showModal();
    inputRef.current?.select();
    return () => { dialog?.close(); if (previous?.isConnected) previous.focus(); };
  }, []);
  const trimmed = name.trim();
  const valid = trimmed.length > 0 && trimmed !== workspace.name;
  return <dialog className="unified-review workspace-admin-dialog" ref={ref} aria-labelledby="rename-workspace-title" onCancel={onClose}>
    <header><div><h2 id="rename-workspace-title">Rename workspace</h2><p>Only the label changes. The folder, snapshots, drafts and Power BI files stay untouched.</p></div><button type="button" className="icon-button" aria-label="Cancel rename" onClick={onClose}><X size={18} /></button></header>
    <form onSubmit={(event) => { event.preventDefault(); if (valid && !busy) onSubmit(trimmed); }}>
      <label className="path-prompt-field"><span>Workspace name</span>
        <input ref={inputRef} value={name} onChange={(event) => setName(event.target.value)} maxLength={120} spellCheck={false} autoComplete="off" />
      </label>
      <div className="path-prompt-actions">
        <button type="button" className="secondary" onClick={onClose}>Cancel</button>
        <button type="submit" className="primary" disabled={!valid || busy}>{busy ? "Renaming…" : "Rename"}</button>
      </div>
    </form>
  </dialog>;
}

/** Deletion is irreversible, so the workspace name must be typed back exactly. */
export function DeleteWorkspaceDialog({ workspace, busy, onSubmit, onClose }: { workspace: Workspace; busy: boolean; onSubmit: () => void; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [confirm, setConfirm] = useState("");
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = ref.current;
    dialog?.showModal();
    inputRef.current?.focus();
    return () => { dialog?.close(); if (previous?.isConnected) previous.focus(); };
  }, []);
  const matches = confirm.trim() === workspace.name;
  return <dialog className="unified-review workspace-admin-dialog" ref={ref} aria-labelledby="delete-workspace-title" onCancel={onClose}>
    <header><div><h2 id="delete-workspace-title">Delete workspace</h2><p>Removes this workspace's snapshots, drafts, builds and conversation from the app. Generated PBIP folders under the app's state root are deleted too. Your source data folder and any exported project copy are never touched.</p></div><button type="button" className="icon-button" aria-label="Cancel delete" onClick={onClose}><X size={18} /></button></header>
    <form onSubmit={(event) => { event.preventDefault(); if (matches && !busy) onSubmit(); }}>
      <label className="path-prompt-field"><span>Type <strong>{workspace.name}</strong> to confirm</span>
        <input ref={inputRef} value={confirm} onChange={(event) => setConfirm(event.target.value)} maxLength={120} spellCheck={false} autoComplete="off" />
      </label>
      <div className="path-prompt-actions">
        <button type="button" className="secondary" onClick={onClose}>Cancel</button>
        <button type="submit" className="primary danger" disabled={!matches || busy}>{busy ? "Deleting…" : "Delete workspace"}</button>
      </div>
    </form>
  </dialog>;
}

export function WorkflowRail({ snapshot, draft, build }: { snapshot?: Snapshot; draft?: Draft; build?: Build }) {
  const governedMeasures = draft?.measure_proposal.measures.filter((measure) => measure.tier === "governed").length ?? 0;
  const advancedMeasures = draft?.measure_proposal.measures.filter((measure) => measure.tier === "advanced_dax").length ?? 0;
  const measureDetail = advancedMeasures > 0
    ? `${governedMeasures} governed · ${advancedMeasures} advanced`
    : `${governedMeasures} governed measures`;
  const draftApproved = Boolean(build) || Boolean(draft && APPROVED_DRAFT_STATES.has(draft.status));
  const modelReviewed = Boolean(build) || Boolean(draft && (draft.semantic_model.approved || APPROVED_DRAFT_STATES.has(draft.status)));
  const steps: Array<{ label: string; detail: string; done: boolean; failed?: boolean }> = [
    { label: "Data ready", detail: snapshot ? `${snapshot.tables.length} tables · ${snapshot.tables.reduce((sum, item) => sum + item.table.row_count, 0).toLocaleString()} rows` : "Choose local sources", done: Boolean(snapshot) },
    { label: "Model reviewed", detail: draft ? measureDetail : "Run AI analysis", done: modelReviewed },
    { label: "Draft approved", detail: draft ? `${draft.pages.length} report pages${draftApproved ? " approved" : " awaiting approval"}` : "Review the dashboard", done: draftApproved },
    { label: "Power BI build", detail: build ? BUILD_WORKFLOW_DETAIL[build.state] : draft ? DRAFT_BUILD_DETAIL[draft.status] : "Create and approve a draft", done: build?.state === "committed", failed: build?.state === "failed" || (!build && draft?.status === "failed") },
  ];
  const firstPending = steps.findIndex((step) => !step.done);
  return <aside className="workflow-rail" aria-label="Dashboard workflow">
    <div className="workflow-heading"><span>Workflow</span><small>{steps.filter((step) => step.done).length} of {steps.length}</small></div>
    <ol className="workflow-steps">
      {steps.map((step, index) => {
        const current = firstPending === index;
        const accessibleState = step.done ? "Completed" : step.failed ? "Failed" : current ? "Current step" : "Upcoming";
        return <li className={`workflow-step ${step.done ? "done" : ""} ${current ? "current" : ""} ${step.failed ? "failed" : ""}`} key={step.label} aria-current={current ? "step" : undefined}>
          <span className="workflow-marker" aria-hidden="true">{step.done ? <Check size={14} /> : step.failed ? <AlertTriangle size={14} /> : current ? index + 1 : <Circle size={9} />}</span>
          <span><span className="sr-only">{accessibleState}: </span><strong>{step.label}</strong><small>{step.detail}</small></span>
        </li>;
      })}
    </ol>
  </aside>;
}

function renderInline(text: string) {
  // The local model sometimes writes LaTeX arrows ("Products $\to$ Sales").
  return text.replace(/\$\\(?:to|rightarrow|Rightarrow)\$/g, "→").split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean).map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={index}>{part.slice(1, -1)}</code>;
    return part;
  });
}

function RichCodeBlock({ language, code, onUseDax }: { language: string; code: string; onUseDax?: (expression: string) => void }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async () => {
    try { await navigator.clipboard.writeText(code); setCopyState("copied"); }
    catch { setCopyState("failed"); }
  };
  return <div className="rich-code-block"><header><span>{language || "Code"}</span><div>
    <button type="button" onClick={() => void copy()} aria-label={copyState === "copied" ? "Code copied" : "Copy code"}>{copyState === "copied" ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}{copyState === "copied" ? "Copied" : "Copy code"}</button>
    {onUseDax && <button type="button" onClick={() => onUseDax(code.trim())}>Continue in DAX Lab<ArrowRight size={13} aria-hidden="true" /></button>}
  </div></header><pre><code>{code}</code></pre>{copyState === "failed" && <p role="status">Clipboard unavailable. Select the code to copy it.</p>}</div>;
}

const tableCells = (line: string) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, "|"));

export function RichText({ text, onUseDax }: { text: string; onUseDax?: (expression: string) => void }) {
  const lines = text.split(/\r?\n/);
  const content: ReactNode[] = [];
  let list: string[] = [];
  let listKind: "ordered" | "unordered" = "unordered";
  let code: { language: string; lines: string[] } | undefined;
  const flushCode = (complete: boolean) => {
    if (!code) return;
    const expression = code.lines.join("\n").trim();
    content.push(<RichCodeBlock key={`code-${content.length}`} language={code.language} code={code.lines.join("\n")} onUseDax={complete && code.language.toLowerCase() === "dax" && expression ? onUseDax : undefined} />);
    code = undefined;
  };
  const flush = () => {
    if (!list.length) return;
    const items = list.map((item, index) => <li key={index}>{renderInline(item)}</li>);
    content.push(listKind === "ordered" ? <ol key={`list-${content.length}`}>{items}</ol> : <ul key={`list-${content.length}`}>{items}</ul>);
    list = [];
  };
  for (let index = 0; index < lines.length; index += 1) {
    const raw = lines[index];
    if (code) {
      if (/^\s*```\s*$/.test(raw)) flushCode(true);
      else code.lines.push(raw);
      continue;
    }
    const fence = raw.match(/^\s*```([\w+-]*)\s*$/);
    if (fence) { flush(); code = { language: fence[1], lines: [] }; continue; }
    const line = raw.trim();
    if (!line) { flush(); continue; }
    const bullet = line.match(/^([-*]|\d+[.)])\s+(.+)$/);
    if (bullet) {
      const kind = /^\d/.test(bullet[1]) ? "ordered" : "unordered";
      if (list.length && listKind !== kind) flush();
      listKind = kind;
      list.push(bullet[2]);
      continue;
    }
    flush();
    const cells = line.includes("|") ? tableCells(line) : [];
    const separators = index + 1 < lines.length ? tableCells(lines[index + 1]) : [];
    if (cells.length > 1 && separators.length === cells.length && separators.every(cell => /^:?-{3,}:?$/.test(cell))) {
      const rows: string[][] = [];
      index += 1;
      while (index + 1 < lines.length && lines[index + 1].includes("|")) {
        const row = tableCells(lines[index + 1]);
        if (row.length !== cells.length) break;
        rows.push(row);
        index += 1;
      }
      content.push(<div key={`table-${content.length}`} className="rich-table-scroll" role="region" aria-label="Answer table" tabIndex={0}><table><thead><tr>{cells.map((cell, cellIndex) => <th key={cellIndex} scope="col">{renderInline(cell)}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{renderInline(cell)}</td>)}</tr>)}</tbody></table></div>);
      continue;
    }
    const heading = line.match(/^#{1,4}\s+(.+)$/);
    content.push(heading ? <h4 key={content.length}>{renderInline(heading[1])}</h4> : <p key={content.length}>{renderInline(line)}</p>);
  }
  flush();
  flushCode(false);
  return <div className="rich-text">{content}</div>;
}

export function DataProfileExplorer({ snapshot, onAskTable, busy = false }: { snapshot: Snapshot; onAskTable?: (id: string, name: string) => void; busy?: boolean }) {
  const [activeId, setActiveId] = useState(snapshot.tables[0]?.id);
  const active = snapshot.tables.find((item) => item.id === activeId) || snapshot.tables[0];
  return <section className="profile-explorer">
    <div className="profile-explorer-heading"><div><h2>Profiled dataset</h2><p>Types, uniqueness and data quality are computed locally from the normalized snapshot.</p></div><span>{snapshot.tables.length} tables</span></div>
    <div className="profile-explorer-body">
      <div className="profile-table-nav">{snapshot.tables.map((item) => <button key={item.id} className={item.id === active?.id ? "active" : ""} onClick={() => setActiveId(item.id)}><Table2 size={16} /><span><strong>{item.table.name}</strong><small>{item.table.row_count.toLocaleString()} rows · {item.table.columns.length} columns</small></span><ArrowRight size={14} /></button>)}</div>
      {active && <div className="profile-table-detail"><div className="profile-table-heading"><div><h3>{active.table.name}</h3><span>{active.table.row_count.toLocaleString()} rows · {active.table.columns.length} columns</span></div>{onAskTable && <AskAiButton about={`table ${active.table.name}`} disabled={busy} disabledReason="Finish the current action or save your report edits first" onClick={() => onAskTable(active.table.id, active.table.name)} />}</div><div className="column-profile"><div className="column-head"><span>Column</span><span>Type</span><span>Completeness</span><span>Distinct</span><span>Role</span></div>{active.table.columns.map((column) => {
        const complete = column.row_count ? Math.round(((column.row_count - column.null_count) / column.row_count) * 100) : 0;
        return <div className="column-row" key={column.id}><span><strong>{column.name}</strong><small>{column.sample_values.slice(0, 2).map(String).join(" · ") || "No sample"}</small></span><code>{column.data_type}</code><span>{complete}%</span><span>{column.distinct_count.toLocaleString()}</span><span>{column.candidate_key ? "Key candidate" : "Field"}</span></div>;
      })}</div></div>}
    </div>
    {!!snapshot.findings.length && <div className="quality-findings"><h3><AlertTriangle size={16} />Data quality findings</h3>{snapshot.findings.map((finding) => <div key={finding.id}><span className={`severity ${finding.severity}`}>{finding.severity}</span><span><strong>{finding.code.replaceAll("_", " ")}</strong><small>{finding.message}</small></span></div>)}</div>}
  </section>;
}

const eventSummary = (event: StudioEvent) => {
  const names: Record<string, string> = {
    "workspace.created": "Workspace created",
    "ingestion.started": "Local import started",
    "ingestion.completed": "Snapshot profiled",
    "draft.analysis_started": "AI analysis started",
    "draft.created": "Dashboard draft created",
    "draft.approved": "Draft approved",
    "build.compiling": "Power BI build started",
    "build.committed": "Validated PBIP committed",
    "build.failed": "Build failed safely",
    "build.rolled_back": "Previous project restored",
  };
  return names[event.type] || event.type.replaceAll(".", " · ").replaceAll("_", " ");
};

export function EvidenceScreen({ snapshot, draft, events, builds }: { snapshot?: Snapshot; draft?: Draft; events: StudioEvent[]; builds: Build[] }) {
  const [filter, setFilter] = useState<"all" | "data" | "ai" | "build">("all");
  const visible = events.filter((event) => filter === "all" || (filter === "data" ? event.type.startsWith("ingestion") || event.type.startsWith("workspace") : filter === "ai" ? event.type.startsWith("draft") : event.type.startsWith("build")));
  const latestValidation = [...events].reverse().find((event) => event.type === "build.committed")?.payload.validation as Record<string, unknown> | undefined;
  const validators = Array.isArray(latestValidation?.validators) ? latestValidation.validators.filter((item): item is string => typeof item === "string") : [];
  return <section className="content-page evidence-page">
    <div className="page-header"><div><h1>Evidence</h1><p>Trace every metric, model decision and project change back to local reviewed inputs.</p></div><div className="evidence-trust"><ShieldCheck size={18} /><span><strong>Local audit trail</strong><small>{events.length} recorded events</small></span></div></div>
    <div className="evidence-summary"><article><Database size={19} /><span><small>Source snapshot</small><strong>{snapshot ? `${snapshot.tables.length} tables` : "Not created"}</strong><code>{snapshot?.base_hash.slice(0, 14) || "—"}</code></span></article><article><FileSearch size={19} /><span><small>Aggregate receipts</small><strong>{draft?.previews.filter((item) => item.query_result_hash).length || 0} validated</strong><code>{draft?.id.slice(0, 20) || "—"}</code></span></article><article><FileCheck2 size={19} /><span><small>Latest validation</small><strong>{latestValidation?.passed ? "Passed" : builds[0]?.state || "Not built"}</strong><code>{String(latestValidation?.project_hash || "—").slice(0, 14)}</code></span></article></div>
    <div className="evidence-grid">
      <section className="activity-panel"><div className="panel-heading"><div><h2>Activity</h2><p>Append-only lifecycle events from this workspace.</p></div><div className="filter-tabs">{(["all", "data", "ai", "build"] as const).map((item) => <button key={item} className={filter === item ? "active" : ""} onClick={() => setFilter(item)}>{item}</button>)}</div></div><div className="event-timeline">{[...visible].reverse().map((event) => <details key={event.sequence}><summary><span className={`event-dot ${event.type.includes("failed") ? "failed" : ""}`}><Activity size={13} /></span><span><strong>{eventSummary(event)}</strong><small>{localDate(event.created_at).toLocaleString()}</small></span><ChevronDown size={15} /></summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>)}</div></section>
      <aside className="receipt-panel"><h2>Query evidence</h2><p>Bounded aggregate results used by the logical preview.</p>{!draft?.previews.length ? <div className="empty-mini">No query receipts yet.</div> : draft.previews.map((item) => <div className="receipt-row" key={item.visual_id}><CheckCircle2 size={15} /><span><strong>{item.visual_id.replaceAll("_", " ")}</strong><small>{item.rows.length} rows · {item.query_elapsed_ms ?? 0} ms</small><code>{item.query_result_hash?.slice(0, 18) || "No query required"}</code></span></div>)}{latestValidation && <><h2 className="receipt-heading">Validators</h2>{validators.map((validator) => <div className="validator-row" key={validator}><Check size={13} />{validator}</div>)}</>}</aside>
    </div>
  </section>;
}

export function InitialLoading() {
  return <div className="initial-loading" role="status" aria-live="polite"><div className="loading-mark"><Sparkles size={23} /></div><h1>Opening your local workspace</h1><p>Connecting to the model, evidence store and Power BI tools…</p><div className="loading-line"><span /></div></div>;
}

export function ProductNotice({ message, onClose }: { message?: string; onClose: () => void }) {
  if (!message) return null;
  return <div className="product-notice" role="status"><CheckCircle2 size={17} /><span>{message}</span><button onClick={onClose} aria-label="Dismiss notification"><X size={14} /></button></div>;
}

export function HeaderCommandButton({ onClick }: { onClick: () => void }) {
  return <button className="header-command" onClick={onClick}><Search size={16} /><span>Search or run a command</span><kbd>Ctrl K</kbd></button>;
}

export function WorkspaceTrigger({ workspace, onClick, expanded }: { workspace?: Workspace; onClick: () => void; expanded: boolean }) {
  return <button className="workspace-trigger" onClick={onClick} aria-expanded={expanded}><span>Workspace:</span><strong>{workspace?.name || "Not selected"}</strong><ChevronDown size={14} /></button>;
}

/** Readiness only: RAM and power never pause local AI, so there is no paused state to name. */
export function RuntimeStatus({ ready, error, installNeeded, onInstall, setupState }: { ready: boolean; error?: string | null; installNeeded?: boolean; onInstall?: () => void; setupState?: ModelInstallState }) {
  if (setupState && ["starting", "copying", "downloading", "verifying"].includes(setupState)) {
    return <span className="status"><LoaderCircle className="spin" size={15} />{setupState === "verifying" ? "Checking local AI…" : "Setting up local AI…"}</span>;
  }
  if (setupState && ["damaged", "cancelled", "failed"].includes(setupState) && onInstall) {
    return <button type="button" className="status warning status-action" onClick={onInstall}><Download size={15} />{setupState === "damaged" ? "Repair local AI" : setupState === "cancelled" ? "Resume local AI setup" : "Retry local AI setup"}</button>;
  }
  if (!ready && /checking|verifying/i.test(error ?? "")) {
    return <span className="status" title={error ?? undefined}><LoaderCircle className="spin" size={15} />Checking local AI…</span>;
  }
  if (!ready && /damaged|fingerprint|repair|does not match/i.test(error ?? "") && onInstall) {
    return <button type="button" className="status warning status-action" title={error ?? undefined} onClick={onInstall}><Download size={15} />Repair local AI</button>;
  }
  // First launch: the one model is installed from Settings (installers cannot carry it).
  if (!ready && installNeeded && onInstall) {
    return <button type="button" className="status warning status-action" title={error ?? undefined} onClick={onInstall}><Download size={15} />Install Qwen 4B</button>;
  }
  return <span className={ready ? "status success" : "status warning"} title={error ?? undefined}>{ready ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}{ready ? "Local AI ready" : "Local model unavailable"}</span>;
}

export function HashLabel({ value }: { value?: string }) {
  return <span className="hash-label"><Hash size={12} />{value?.slice(0, 12) || "unavailable"}</span>;
}

export function Timestamp({ value }: { value: string }) {
  return <span className="timestamp"><Clock3 size={12} />{localDate(value).toLocaleString()}</span>;
}

export function LocalOnlyLabel() {
  return <span className="local-only"><Database size={13} />Local only</span>;
}

export function KeyboardHint() {
  return <span className="keyboard-hint"><Command size={13} />Ctrl K</span>;
}
