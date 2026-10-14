import { AlertTriangle, ArrowDown, CheckCircle2, Clock3, GitBranch } from "lucide-react";
import type { ModelWorkbenchRelationship, ModelWorkbenchTable } from "./ModelWorkbench";

export function RelationshipInspector({ relationship: r, tables, onReview, onBack }: {
  relationship: ModelWorkbenchRelationship; tables: readonly ModelWorkbenchTable[];
  onReview: () => void; onBack: () => void;
}) {
  const from = tables.find(t => t.id === r.fromTableId);
  const to = tables.find(t => t.id === r.toTableId);
  const column = (table: ModelWorkbenchTable | undefined, id: string) => table?.columns?.find(c => c.id === id)?.name || id;
  const status = r.blockers.length ? "Blocked" : r.reviewStatus === "approved" ? "Approved" : r.reviewStatus === "rejected" ? "Rejected" : "Pending review";
  const Status = r.blockers.length ? AlertTriangle : r.reviewStatus === "approved" ? CheckCircle2 : Clock3;
  return <>
    <h3><GitBranch size={16}/>Relationship</h3>
    <span className={`relationship-inspector-status ${r.blockers.length ? "blocked" : r.reviewStatus}`}><Status size={14}/>{status}</span>
    <div className="relationship-endpoints">
      <div><small>{r.cardinality === "many-to-many" ? "Many" : "One"} side</small><strong>{to?.displayName || to?.name || r.toTableId}</strong><code>{column(to, r.toColumnId)}</code></div>
      <ArrowDown size={16}/>
      <div><small>{r.cardinality === "one-to-one" ? "One" : "Many"} side</small><strong>{from?.displayName || from?.name || r.fromTableId}</strong><code>{column(from, r.fromColumnId)}</code></div>
    </div>
    <h4>Filter behavior</h4>
    <p className="relationship-filter-copy">{r.crossFilterDirection === "both" ? "Filters travel in both directions." : `Filters on ${to?.displayName || to?.name || r.toTableId} flow to ${from?.displayName || from?.name || r.fromTableId}.`}{r.active === false ? " This relationship is inactive." : ""}{r.blockers.length ? " Resolve the blockers before using it." : r.reviewStatus !== "approved" ? " Review is required before use." : ""}</p>
    {!!r.blockers.length && <section className="relationship-blockers"><h4>Needs attention</h4><ul>{r.blockers.map((blocker, i) => <li key={i}>{blocker}</li>)}</ul></section>}
    {!!r.evidence?.length && <details className="relationship-evidence"><summary>Validation evidence ({r.evidence.length})</summary><ul>{r.evidence.map((evidence, i) => <li key={i}>{evidence}</li>)}</ul></details>}
    <button className="secondary" onClick={onReview}>Review relationship</button>
    <button className="relationship-back" onClick={onBack}>Back to table properties</button>
  </>;
}
