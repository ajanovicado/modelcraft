import { useSyncExternalStore } from "react";
import { Boxes, Sigma, Trash2, X } from "lucide-react";
import { pendingStore, type EditCapabilities, type PendingChange } from "./importedEditing";
import { describeModelOp, expressionLanguage, isModelFamilyOperation, supportState, type ModelFacts, type SupportCoverage } from "./modelEditing";
import { SupportNote } from "./ModelEditorShared";

/** The changes waiting for a Power BI cycle for one report; re-renders when DAX Lab, chat or the measure editor queue one. */
export function usePendingChanges(workspaceId: string): PendingChange[] {
  return useSyncExternalStore(pendingStore.subscribe, () => pendingStore.get(workspaceId), () => pendingStore.get(workspaceId));
}

/** Power BI applies at most this many changes in one request; the rest stay queued for the next Apply. */
export const MAX_PENDING_BATCH = 50;

/**
 * "Pending Power BI changes (n)" (P32, P33). Measure and model changes need Power BI Desktop, so they queue
 * here and run together in one Desktop cycle only when the user asks: never automatically, and
 * never as a side effect of a report edit. Each change shows its DAX (or Power Query), and its what-if answer when
 * the assistant produced one. With the model's facts, a change outside Microsoft's supported list says so.
 */
export function PendingModelChanges({ workspaceId, capabilities, disabled, onApply, onDax, facts, coverage }: {
  workspaceId: string; capabilities?: EditCapabilities; disabled: boolean;
  onApply: (changes: PendingChange[]) => void; onDax?: (expression: string) => void;
  facts?: ModelFacts; coverage?: SupportCoverage;
}) {
  const changes = usePendingChanges(workspaceId);
  if (!changes.length) return null;
  const canApply = capabilities?.edit_model !== false;
  const blocked = facts && coverage ? changes.filter(change => supportState(change.op, facts, coverage).blocked) : [];
  return <section className="ie-pending" aria-label={`Pending Power BI changes (${changes.length})`}>
    <header><strong>Pending Power BI changes ({changes.length})</strong>
      <button type="button" className="icon-button" aria-label="Discard all pending changes" title="Discard all pending changes" disabled={disabled} onClick={() => pendingStore.clear(workspaceId)}><Trash2 size={14} /></button></header>
    <ul>{changes.map(change => {
      const title = describeModelOp(change.op) ?? change.title;
      const language = expressionLanguage(change.op);
      const code = typeof change.op.expression === "string" ? change.op.expression : change.dax;
      return <li key={change.id}>
        <div className="ie-pending-title">{isModelFamilyOperation(change.op) ? <Boxes size={13} aria-hidden="true" /> : <Sigma size={13} aria-hidden="true" />}<span>{title}</span>
          <button type="button" className="icon-button" aria-label={`Remove: ${title}`} disabled={disabled} onClick={() => pendingStore.remove(workspaceId, change.id)}><X size={13} /></button></div>
        {facts && coverage && <SupportNote state={supportState(change.op, facts, coverage)} />}
        {code && <pre aria-label={language === "m" ? "Power Query (M)" : "DAX"}>{code}</pre>}
        {change.whatIf && <p className="ie-whatif"><strong>What if:</strong> {change.whatIf.question}<br />{change.whatIf.answer}</p>}
        {code && language !== "m" && onDax && <button type="button" className="secondary" onClick={() => onDax(code)}>Try it in DAX Lab first</button>}
      </li>;
    })}</ul>
    {!canApply && <p className="ie-note" role="status">Model changes are not available right now{capabilities?.reasons.length ? `: ${capabilities.reasons.join("; ")}` : "."}</p>}
    {blocked.length > 0 && <p className="ie-note ie-error" role="status">Remove the {blocked.length === 1 ? "change" : `${blocked.length} changes`} Studio does not make with this Power BI Desktop before applying.</p>}
    {changes.length > MAX_PENDING_BATCH && <p className="ie-note" role="status">Power BI applies at most {MAX_PENDING_BATCH} changes at a time: the first {MAX_PENDING_BATCH} are applied now, the rest stay here.</p>}
    <button type="button" className="primary yellow full" disabled={disabled || !canApply || blocked.length > 0} onClick={() => onApply(changes)}>Apply to report (needs Power BI, about 1 min)</button>
    <small className="ie-note">Power BI Desktop opens briefly to make these changes and saves them into your report&rsquo;s history. You can undo them afterwards.</small>
  </section>;
}
