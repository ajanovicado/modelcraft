import { ListChecks, Loader2, LocateFixed, RefreshCw, Wand2, X } from "lucide-react";
import { reviewHeadline, severityLabel, type Review, type ReviewFinding } from "./judgement";

/** One finding with its one-click fix (sent as one edit, undoable) and a way to select the visuals it is about. */
export function FindingRow({ finding, fixed, disabled, onFix, onShow }: { finding: ReviewFinding; fixed?: boolean; disabled: boolean; onFix?: (finding: ReviewFinding) => void; onShow?: (visuals: string[]) => void }) {
  return <li className={`ie-finding ${finding.severity}`}>
    <span className={`ie-badge ${finding.severity}`}>{severityLabel(finding)}</span>
    <span className="ie-finding-text">{finding.message}{finding.fixSummary && <small>{finding.fixSummary}</small>}</span>
    <span className="ie-row-tools">
      {onShow && finding.visuals.length > 0 && <button type="button" className="icon-button" aria-label="Select the visuals this is about" title="Show on the canvas" onClick={() => onShow(finding.visuals)}><LocateFixed size={12} /></button>}
      {finding.fix && onFix && (fixed ? <span className="ie-card-status">Fixed</span>
        : <button type="button" className="secondary" disabled={disabled} title={finding.fixSummary ?? "Apply Studio's fix (one change you can undo)"} onClick={() => onFix(finding)}><Wand2 size={12} />Fix</button>)}
    </span>
  </li>;
}

/**
 * "Review this page" (docs/imported-report-review.md): what a developer would notice on the page, problems first, each with
 * a one-click fix when the right fix is not a judgement call. Checks that need the page's data and could not run, and text
 * over pictures that was not judged, are listed quietly at the end.
 */
export function ReviewPanel({ review, loading, error, fixed, disabled, onFix, onShow, onRefresh, onClose }: {
  review?: Review; loading: boolean; error?: string; fixed: string[]; disabled: boolean;
  onFix: (finding: ReviewFinding) => void; onShow: (visuals: string[]) => void; onRefresh: () => void; onClose: () => void;
}) {
  const problems = review?.findings.filter(f => f.severity === "problem") ?? [];
  const suggestions = review?.findings.filter(f => f.severity === "suggestion") ?? [];
  return <aside className="ie-sidecard" aria-label="Page review">
    <header><strong><ListChecks size={14} aria-hidden="true" />Review this page</strong>
      <span className="ie-row-tools">
        <button type="button" className="icon-button" aria-label="Review again" disabled={loading} onClick={onRefresh}><RefreshCw size={13} /></button>
        <button type="button" className="icon-button" aria-label="Close the review" onClick={onClose}><X size={14} /></button></span></header>
    <div className="ie-sidecard-body" aria-busy={loading}>
      {loading && <p className="ie-note" role="status"><Loader2 size={13} className="spin" aria-hidden="true" />Checking the page…</p>}
      {error && <p className="ie-note ie-error" role="alert">{error}</p>}
      {review && !loading && <p><strong>{reviewHeadline(review)}</strong></p>}
      {problems.length > 0 && <><h5>Problems</h5><ul className="ie-finding-list">{problems.map(f => <FindingRow key={f.id} finding={f} fixed={fixed.includes(f.id)} disabled={disabled} onFix={onFix} onShow={onShow} />)}</ul></>}
      {suggestions.length > 0 && <><h5>Suggestions</h5><ul className="ie-finding-list">{suggestions.map(f => <FindingRow key={f.id} finding={f} fixed={fixed.includes(f.id)} disabled={disabled} onFix={onFix} onShow={onShow} />)}</ul></>}
      {review && (review.skipped.length > 0 || review.notes.length > 0) && <details className="ie-muted"><summary>Not checked</summary>
        <ul>{review.skipped.map(item => <li key={item.rule}>{item.reason}</li>)}{review.notes.map(note => <li key={note}>{note}</li>)}</ul></details>}
    </div>
  </aside>;
}
