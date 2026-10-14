import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, HardDrive, History, LoaderCircle, RefreshCw, RotateCcw, Undo2 } from "lucide-react";
import { EditorDialog } from "./EditorDialog";
import { historyView, restorePrompt, type HistoryRow, type HistoryRowView, type RestorePrompt } from "./importedEditing";
import type { ImportedEditing } from "./useImportedEditing";

const when = (value: string) => {
  const date = new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
};

type Pending = { target: HistoryRow; prompt: RestorePrompt };

/**
 * Every revision, newest first. Undo exists only on the newest change (P21): an older row would
 * silently discard everything after it, so older rows offer a restore that first lists the later
 * changes, with who made them, and asks.
 */
export function HistoryPane({ workspaceId, editing, disabled }: { workspaceId: string; editing: ImportedEditing; disabled: boolean }) {
  const { history, historyState, loadHistory, head, client } = editing;
  const [pending, setPending] = useState<Pending>();
  const [message, setMessage] = useState<string>();
  const headId = head?.id ?? "";
  useEffect(() => { void loadHistory(); }, [loadHistory, headId]);
  const rows = useMemo<HistoryRowView[]>(() => historyView(history, headId), [history, headId]);

  const askRestore = async (target: HistoryRow, clicked?: HistoryRow) => {
    setMessage(undefined);
    try {
      const later = await client.discardPreview(workspaceId, target.id);
      setPending({ target, prompt: restorePrompt(target, later, clicked?.summary) });
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "Could not check what a restore would leave out."); }
  };
  const confirm = async () => {
    const current = pending;
    setPending(undefined);
    if (!current) return;
    const outcome = await editing.restore(current.target.id, current.prompt.needsConfirmation);
    if (outcome.status === "applied") setMessage("Restored. The earlier version is now the report; everything else stays in History.");
    await loadHistory();
  };

  return <div className="ie-history">
    <div className="ie-history-head"><strong><History size={14} aria-hidden="true" />History</strong>
      <button type="button" className="icon-button" aria-label="Refresh history" disabled={historyState.loading} onClick={() => void loadHistory()}>{historyState.loading ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}</button></div>
    {message && <p className="ie-note" role="status">{message}</p>}
    {historyState.error && <p className="ie-note ie-error" role="alert">{historyState.error}</p>}
    {!rows.length && !historyState.loading && !historyState.error && <p className="ie-empty">No changes yet. Everything you change appears here, and every change can be undone.</p>}
    <ol className="ie-history-list">{rows.map(row => <li key={row.id} className={row.is_head ? "head" : undefined}>
      <div className="ie-history-line"><span className={`ie-badge ${row.badge.tone}`}>{row.badge.label}</span>{row.is_head && <span className="ie-badge current">Current</span>}<time dateTime={row.created}>{when(row.created)}</time></div>
      <p>{row.summary || "Change"}</p>
      {row.operations.length > 1 && <details className="ie-history-ops"><summary>{row.operations.length} edits</summary><ul>{row.operations.map((op, index) => <li key={index}>{op.summary}</li>)}</ul></details>}
      {row.conflict && <p className="ie-conflict"><AlertTriangle size={13} aria-hidden="true" />Saved in Power BI while Studio had newer changes; both are kept here.</p>}
      <div className="ie-history-actions">
        {!row.keptOnDisk && <span className="ie-note"><HardDrive size={12} aria-hidden="true" />Not kept on disk</span>}
        {row.keptOnDisk && <span className="ie-note"><HardDrive size={12} aria-hidden="true" />Kept on disk</span>}
        {row.canUndo && <button type="button" className="secondary" disabled={disabled} onClick={() => void editing.undo().then(() => loadHistory())}><Undo2 size={13} />Undo</button>}
        {row.canRestore && row.parent && <button type="button" className="secondary" disabled={disabled} onClick={() => { const parent = history.find(item => item.id === row.parent); if (parent) void askRestore(parent, row); }}><RotateCcw size={13} />Restore to before this</button>}
        {row.canRestore && !row.parent && <button type="button" className="secondary" disabled={disabled} onClick={() => void askRestore(row)}><RotateCcw size={13} />Restore this version</button>}
      </div>
    </li>)}</ol>
    {pending && <EditorDialog title={pending.prompt.title} tone="warn" wide onCancel={() => setPending(undefined)}
      footer={<><button type="button" className="secondary" data-autofocus onClick={() => setPending(undefined)}>Cancel</button><button type="button" className="primary yellow" onClick={() => void confirm()}>{pending.prompt.confirmLabel}</button></>}>
      <p className="ie-dialog-text">{pending.prompt.body}</p>
      {pending.prompt.items.length > 0 && <ul className="ie-discard-list">{pending.prompt.items.map((item, index) => <li key={index}><span className="ie-badge">{item.author}</span>{item.summary}<time>{when(item.created)}</time></li>)}</ul>}
    </EditorDialog>}
  </div>;
}
