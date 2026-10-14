import { useEffect, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, ArrowDown, ArrowUp, CheckCircle2, GripVertical, Loader2, ShieldAlert, Trash2 } from "lucide-react";
import type { Operation } from "./importedEditing";
import type { DaxResult } from "./types";
import { supportState, type Guard, type ModelFacts, type QueryCheck, type ReportUse, type SupportCoverage, type SupportState } from "./modelEditing";

/** What every section of the model workbench needs. `queue` puts one change in "Pending Power BI changes" (never applies it). */
export type ModelContext = {
  workspaceId: string;
  facts: ModelFacts;
  report: ReportUse;
  coverage: SupportCoverage;
  locked: boolean;
  queue: (op: Operation) => void;
  /** Runs a DAX query on Studio's engine (the DAX Lab check); rejects with the engine's message. */
  check: (query: string) => Promise<DaxResult>;
  onDax?: (expression: string) => void;
  /** Checks a Power Query formula on the server as an edit would (`POST .../model/validate-query`); nothing is queued. */
  validateQuery?: (expression: string, table?: string) => Promise<QueryCheck>;
};

/** "Outside Microsoft's supported list" next to a change, and Desktop's steps when Studio does not make it here. */
export function SupportNote({ state }: { state: SupportState }) {
  if (!state.outside) return null;
  return <div className={`me-support${state.blocked ? " blocked" : ""}`} role={state.blocked ? "note" : undefined}>
    <span className="me-badge outside" title={state.detail}><ShieldAlert size={12} aria-hidden="true" />{state.label}</span>
    {state.blocked && <p>{state.detail}</p>}
    {state.blocked && state.steps && <p><strong>In Power BI Desktop:</strong> {state.steps}</p>}
  </div>;
}

/** The queue button of one change: disabled while the workbench is locked, the change is empty, or Studio does not make it with this Desktop. */
export function QueueButton({ ctx, op, label = "Queue this change", danger = false, disabled = false, onQueued }: {
  ctx: ModelContext; op: Operation | undefined; label?: string; danger?: boolean; disabled?: boolean; onQueued?: () => void;
}) {
  const state = op ? supportState(op, ctx.facts, ctx.coverage) : { outside: false, blocked: false };
  return <>
    <SupportNote state={state} />
    <button type="button" className={danger ? "secondary danger" : "primary yellow"} disabled={ctx.locked || disabled || !op || state.blocked}
      title="Adds it to Pending Power BI changes. Nothing changes in your report until you apply them." onClick={() => { if (op) { ctx.queue(op); onQueued?.(); } }}>{label}</button>
  </>;
}

type CheckState = { status: "idle" | "running" | "ok" | "failed" | "skipped"; message?: string; sample?: string; text?: string; engine?: string };

function sampleOf(result: DaxResult, column?: string): string {
  if (!result.rows.length) return "The formula ran and gave no rows.";
  // Power BI names a query's added column "[Name]" or "Table[Name]"; Studio's engine "Name".
  const key = column ? result.columns.find(c => c === column || c.endsWith(`[${column}]`)) : undefined;
  if (key) {
    const values = result.rows.slice(0, 5).map(row => row[key]).map(v => v === null || v === undefined || v === "" ? "(blank)" : String(v));
    return `First values: ${values.join(", ")}`;
  }
  return `${result.row_count}${result.truncated ? "+" : ""} row${result.row_count === 1 ? "" : "s"}; columns ${result.columns.slice(0, 6).join(", ")}${result.columns.length > 6 ? ", ..." : ""}`;
}

/**
 * DAX Lab's check before a formula is queued: the formula runs on the imported rows first. A formula the check could
 * not run can still be queued on purpose ("Queue anyway"): Power BI checks it again when the change is applied.
 */
export function CheckedQueue({ ctx, query, expression, column, op, label, disabled = false, onQueued }: {
  ctx: ModelContext; query: string | undefined; expression: string; column?: string; op: Operation | undefined; label: string; disabled?: boolean; onQueued?: () => void;
}) {
  const [state, setState] = useState<CheckState>({ status: "idle" });
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const current = state.text === expression ? state : { status: "idle" as const };
  const run = async () => {
    if (!query) { setState({ status: "skipped", text: expression, message: "This formula is too long for the DAX Lab check. Power BI checks it when you apply it." }); return; }
    setState({ status: "running", text: expression });
    try {
      const result = await ctx.check(query);
      if (alive.current) setState({ status: "ok", text: expression, sample: sampleOf(result, column), engine: result.engine });
    } catch (reason) {
      if (alive.current) setState({ status: "failed", text: expression, message: reason instanceof Error ? reason.message : "The formula could not be checked." });
    }
  };
  const blank = !expression.trim();
  return <div className="me-check">
    <div className="me-actions">
      <button type="button" className="secondary" disabled={ctx.locked || blank || current.status === "running"} onClick={() => void run()}>
        {current.status === "running" ? <Loader2 size={13} className="spin" aria-hidden="true" /> : null}Check the formula</button>
      {ctx.onDax && <button type="button" className="secondary" disabled={blank} onClick={() => ctx.onDax?.(expression)}>Open in DAX Lab</button>}
      {current.status === "ok" && <QueueButton ctx={ctx} op={op} label={label} disabled={disabled} onQueued={onQueued} />}
      {(current.status === "failed" || current.status === "skipped") && <QueueButton ctx={ctx} op={op} label={`${label} anyway`} disabled={disabled} onQueued={onQueued} />}
    </div>
    {current.status === "idle" && !blank && <p className="ie-note">Check the formula before queueing it (the same check as DAX Lab).</p>}
    {current.status === "ok" && <p className="me-check-ok" role="status"><CheckCircle2 size={13} aria-hidden="true" />{current.engine === "powerbi" ? "Checked by Power BI Desktop." : "Checked on the imported data."} {current.sample}</p>}
    {current.status === "failed" && <p className="me-check-failed" role="alert"><AlertTriangle size={13} aria-hidden="true" />{current.message} You can still queue it: Power BI checks it again when you apply.</p>}
    {current.status === "skipped" && <p className="ie-note" role="status">{current.message}</p>}
  </div>;
}

/** A delete that asks first and names what still uses the object (Power BI refuses while it is used). */
export function DeleteConfirm({ ctx, what, guard, op, onDone }: { ctx: ModelContext; what: string; guard: Guard; op: Operation; onDone: () => void }) {
  return <div className="me-confirm" role="group" aria-label={`Delete ${what}`}>
    <p className="ie-note">Delete {what}? Nothing changes until you apply your pending changes.</p>
    {guard.blocking.length > 0 && <ul className="me-guard">{guard.blocking.map(line => <li key={line}><AlertTriangle size={12} aria-hidden="true" />{line}</li>)}</ul>}
    {guard.blocking.length > 0 && <p className="ie-note ie-error">Power BI refuses this delete while these use it.</p>}
    {guard.warnings.map(line => <p className="ie-note" key={line}>{line}</p>)}
    <div className="me-actions">
      <button type="button" className="secondary" data-autofocus onClick={onDone}>Keep it</button>
      <QueueButton ctx={ctx} op={op} danger label="Queue the delete" disabled={guard.blocking.length > 0} onQueued={onDone} />
    </div>
  </div>;
}

/** A list the user orders by dragging, or with the move buttons from the keyboard. */
export function OrderedList<T>({ items, keyOf, label, onMove, render, disabled = false }: {
  items: T[]; keyOf: (item: T) => string; label: string; onMove: (from: number, to: number) => void; render: (item: T, index: number) => ReactNode; disabled?: boolean;
}) {
  const [dragging, setDragging] = useState<number>();
  return <ol className="me-ordered" aria-label={label}>
    {items.map((item, index) => <li key={keyOf(item)} className={dragging === index ? "dragging" : undefined} draggable={!disabled}
      onDragStart={event => { setDragging(index); event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", String(index)); }}
      onDragOver={event => { if (dragging !== undefined) event.preventDefault(); }}
      onDrop={event => { event.preventDefault(); if (dragging !== undefined) onMove(dragging, index); setDragging(undefined); }}
      onDragEnd={() => setDragging(undefined)}>
      <GripVertical size={13} aria-hidden="true" className="me-grip" />
      <div className="me-ordered-body">{render(item, index)}</div>
      <button type="button" className="icon-button" aria-label={`Move ${keyOf(item)} up`} disabled={disabled || index === 0} onClick={() => onMove(index, index - 1)}><ArrowUp size={13} /></button>
      <button type="button" className="icon-button" aria-label={`Move ${keyOf(item)} down`} disabled={disabled || index === items.length - 1} onClick={() => onMove(index, index + 1)}><ArrowDown size={13} /></button>
    </li>)}
  </ol>;
}

/** A small "remove" icon button with a spoken label. */
export function RemoveButton({ label, onClick, disabled }: { label: string; onClick: () => void; disabled?: boolean }) {
  return <button type="button" className="icon-button" aria-label={label} title={label} disabled={disabled} onClick={onClick}><Trash2 size={13} /></button>;
}

/** Why an item cannot be edited: its text was cut when Studio read it. */
export const TRUNCATED_NOTE = "Studio could not read this text in full, so it cannot be changed here. Change it in Power BI Desktop.";
