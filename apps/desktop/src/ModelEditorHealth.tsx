import { useCallback, useEffect, useRef, useState } from "react";
import { AlertOctagon, CheckCircle2, HeartPulse, Lightbulb, Loader2, RefreshCw, Wrench } from "lucide-react";
import { EditorDialog } from "./EditorDialog";
import {
  CATEGORY_LABELS, combinedSupport, groupFindings, HEALTH_CATEGORIES, healthFix, objectLabel,
  type Health, type HealthCategory, type HealthFinding, type HealthObject,
} from "./modelEditing";
import { SupportNote, type ModelContext } from "./ModelEditorShared";

/**
 * Model health (docs/imported-model-health.md): the checks an experienced Power BI developer runs, grouped by category,
 * each with its severity, why it matters, the objects it names, and a fix. A fix is never applied from here: "Queue the
 * fix" puts its changes in Pending Power BI changes after the user confirms, like every other model change.
 */
export function ModelEditorHealth({ ctx, headId, load, onFocusObject }: {
  ctx: ModelContext; headId?: string; load: (stats: boolean) => Promise<Health>; onFocusObject: (object: HealthObject) => void;
}) {
  const [stats, setStats] = useState(false);
  const [state, setState] = useState<{ loading: boolean; health?: Health; error?: string }>({ loading: true });
  const [filter, setFilter] = useState<{ category?: HealthCategory; severity?: "problem" | "suggestion" }>({});
  const [confirming, setConfirming] = useState<HealthFinding>();
  const [queued, setQueued] = useState<Set<string>>(() => new Set());
  const request = useRef(0);
  const refresh = useCallback(async () => {
    const mine = ++request.current;
    setState(current => ({ ...current, loading: true, error: undefined }));
    try {
      const health = await load(stats);
      if (mine === request.current) setState({ loading: false, health });
    } catch (reason) {
      if (mine === request.current) setState({ loading: false, error: reason instanceof Error ? reason.message : "Studio could not check the model." });
    }
  }, [load, stats]);
  useEffect(() => { void refresh(); }, [refresh, headId]);

  const health = state.health;
  const groups = health ? groupFindings(health.findings, filter) : [];
  const queueFix = (finding: HealthFinding) => {
    const fix = healthFix(finding);
    if (!fix.ops) return;
    fix.ops.forEach(op => ctx.queue(op));
    setQueued(current => new Set(current).add(finding.id));
    setConfirming(undefined);
  };
  return <section className="me-health" aria-label="Model health" aria-busy={state.loading}>
    <header className="me-health-head">
      <h2><HeartPulse size={16} aria-hidden="true" />Model health</h2>
      <div className="me-actions">
        <label className="me-toggle"><input type="checkbox" checked={stats} onChange={e => setStats(e.target.checked)} />Use data statistics</label>
        <button type="button" className="secondary" disabled={state.loading} onClick={() => void refresh()}>{state.loading ? <Loader2 size={13} className="spin" aria-hidden="true" /> : <RefreshCw size={13} aria-hidden="true" />}Check again</button>
      </div>
    </header>
    {state.error && <p className="ie-note ie-error" role="alert">{state.error}</p>}
    {health && !health.known && <p className="ie-note" role="status">Studio could not read this model's tables and columns, so it could not check it.</p>}
    {health && health.known && <>
      <div className="me-health-summary" role="group" aria-label="Filter the findings">
        <button type="button" className="me-chip problem" aria-pressed={filter.severity === "problem"} onClick={() => setFilter(f => ({ ...f, severity: f.severity === "problem" ? undefined : "problem" }))}>
          <AlertOctagon size={12} aria-hidden="true" />{health.summary.problems} problem{health.summary.problems === 1 ? "" : "s"}</button>
        <button type="button" className="me-chip suggestion" aria-pressed={filter.severity === "suggestion"} onClick={() => setFilter(f => ({ ...f, severity: f.severity === "suggestion" ? undefined : "suggestion" }))}>
          <Lightbulb size={12} aria-hidden="true" />{health.summary.suggestions} suggestion{health.summary.suggestions === 1 ? "" : "s"}</button>
        {HEALTH_CATEGORIES.filter(c => health.summary.by_category[c] > 0).map(c => <button type="button" key={c} className="me-chip" aria-pressed={filter.category === c}
          onClick={() => setFilter(f => ({ ...f, category: f.category === c ? undefined : c }))}>{CATEGORY_LABELS[c]} {health.summary.by_category[c]}</button>)}
      </div>
      {!health.findings.length && <p className="me-check-ok" role="status"><CheckCircle2 size={14} aria-hidden="true" />No findings: this model follows the practices Studio checks.</p>}
      {groups.map(group => <section key={group.category} className="me-health-group" aria-label={`${group.label} findings`}>
        <h3>{group.label} <small>({group.findings.length})</small></h3>
        <ul>{group.findings.map(finding => <FindingCard key={finding.id} ctx={ctx} finding={finding} queued={queued.has(finding.id)} onFocusObject={onFocusObject} onFix={() => setConfirming(finding)} />)}</ul>
      </section>)}
      {health.findings.length > 0 && !groups.length && <p className="ie-note">No findings match this filter.</p>}
    </>}
    {health && health.notes.length > 0 && <details className="me-section"><summary>What was not checked</summary><ul className="me-notes">{health.notes.map(n => <li key={n}>{n}</li>)}</ul></details>}
    {confirming && <FixDialog ctx={ctx} finding={confirming} onCancel={() => setConfirming(undefined)} onConfirm={() => queueFix(confirming)} />}
  </section>;
}

function FindingCard({ ctx, finding, queued, onFocusObject, onFix }: { ctx: ModelContext; finding: HealthFinding; queued: boolean; onFocusObject: (o: HealthObject) => void; onFix: () => void }) {
  const fix = healthFix(finding);
  const support = fix.ops ? combinedSupport(fix.ops, ctx.facts, ctx.coverage) : { outside: false, blocked: false };
  const outside = support.outside || finding.fix_outside_supported;
  const shown = finding.objects.slice(0, 12);
  return <li className={`me-finding ${finding.severity}`}>
    <div className="me-finding-head">
      <span className={`me-badge ${finding.severity}`}>{finding.severity === "problem" ? "Problem" : "Suggestion"}</span>
      <p>{finding.message}</p>
    </div>
    {finding.why && <details><summary>Why it matters</summary><p className="ie-note">{finding.why}</p></details>}
    {shown.length > 0 && <div className="me-objects" role="group" aria-label="Objects">
      {shown.map(o => <button type="button" key={objectLabel(o)} className="me-chip object" onClick={() => onFocusObject(o)} title="Show it in the workbench">{objectLabel(o)}</button>)}
      {finding.objects.length > shown.length && <span className="ie-note">and {finding.objects.length - shown.length} more</span>}
    </div>}
    {fix.ops ? <div className="me-actions">
      {outside && <SupportNote state={support.outside ? support : { outside: true, blocked: false, label: "Outside Microsoft's supported list" }} />}
      <button type="button" className="secondary" disabled={ctx.locked || queued || support.blocked} onClick={onFix}><Wrench size={13} aria-hidden="true" />{queued ? "Fix queued" : "Queue the fix…"}</button>
      <small className="ie-note">{fix.ops.length} change{fix.ops.length === 1 ? "" : "s"} · needs Power BI Desktop</small>
    </div> : <details className="me-howto"><summary>How to fix</summary><p className="ie-note">{fix.byHand}</p></details>}
  </li>;
}

function FixDialog({ ctx, finding, onCancel, onConfirm }: { ctx: ModelContext; finding: HealthFinding; onCancel: () => void; onConfirm: () => void }) {
  const ops = finding.fix ?? [];
  const support = combinedSupport(ops, ctx.facts, ctx.coverage);
  return <EditorDialog title="Queue this fix?" tone="warn" onCancel={onCancel} footer={<>
    <button type="button" className="secondary" data-autofocus onClick={onCancel}>Cancel</button>
    <button type="button" className="primary yellow" disabled={support.blocked} onClick={onConfirm}>Queue the fix</button>
  </>}>
    <p className="ie-dialog-text">{finding.fix_summary}</p>
    <p className="ie-dialog-text"><strong>Needs Power BI Desktop.</strong> The {ops.length === 1 ? "change goes" : `${ops.length} changes go`} into Pending Power BI changes; nothing happens until you apply them there. You can undo them afterwards from History.</p>
    {(support.outside || finding.fix_outside_supported) && <p className="ie-dialog-text">{support.detail ?? "This fix uses a change outside Microsoft's supported list for tools like Studio. Studio makes it only where it passed a test with your Power BI Desktop version."}</p>}
    {support.blocked && support.steps && <p className="ie-dialog-text"><strong>In Power BI Desktop:</strong> {support.steps}</p>}
    {ops.length > 50 && <p className="ie-dialog-text">Power BI applies at most 50 changes at a time; apply the tray twice.</p>}
  </EditorDialog>;
}
