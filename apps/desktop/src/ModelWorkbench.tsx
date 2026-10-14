import { useId, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";

import {

  AlertTriangle,

  Ban,

  Check,

  CheckCircle2,

  Clock3,

  GitBranch,

  Info,

  ListTree,

  ShieldCheck,

  Sigma,

  Sparkles,

  Table2,

} from "lucide-react";

import { ModelDiagram } from "./ModelDiagram";

import { AskAiButton } from "./AskAiButton";

import type { ConversationContext } from "./UnifiedWorkspace";

import "./ModelWorkbench.css";



export type ModelWorkbenchReviewState = "draft" | "review" | "approved" | "blocked";

export type RelationshipReviewStatus = "pending" | "approved" | "rejected";

export type MeasureTier = "governed" | "advanced_dax";

export type MeasureApprovalStatus = "not_required" | "pending" | "approved" | "rejected";

export type MeasureValidationStatus = "pending" | "valid" | "warning" | "invalid";



export interface ModelWorkbenchColumn {

  id: string;

  name: string;

  dataType?: string;

  candidateKey?: boolean;

  hidden?: boolean;

}



export interface ModelWorkbenchTable {

  id: string;

  name: string;

  displayName?: string;

  role?: string;

  rowCount?: number;

  columns?: readonly ModelWorkbenchColumn[];

}



export interface ModelWorkbenchRelationship {

  id: string;

  fromTableId: string;

  fromColumnId: string;

  toTableId: string;

  toColumnId: string;

  cardinality: string;

  confidence: number;

  evidence: readonly string[];

  blockers: readonly string[];

  reviewStatus: RelationshipReviewStatus;

  crossFilterDirection?: "single" | "both";

  active?: boolean;

  recommendedByModel?: boolean;

}



export interface ModelWorkbenchMeasureValidation {

  status: MeasureValidationStatus;

  messages?: readonly string[];

  dependencies?: readonly string[];

}



export interface ModelWorkbenchMeasure {

  id: string;

  name: string;

  tableId: string;

  tier: MeasureTier;

  operation?: string;

  expression?: string;

  formatString?: string;

  assumptions?: readonly string[];

  approvalStatus: MeasureApprovalStatus;

  validation: ModelWorkbenchMeasureValidation;

}



export interface ModelWorkbenchFinding {

  id: string;

  severity: "error" | "warning" | "info";

  message: string;

  targetId?: string;

}



export interface ModelWorkbenchProps {

  tables: readonly ModelWorkbenchTable[];

  relationships: readonly ModelWorkbenchRelationship[];

  measures: readonly ModelWorkbenchMeasure[];

  selectedRelationshipIds: ReadonlySet<string> | readonly string[];

  selectedMeasureId?: string;

  reviewState: ModelWorkbenchReviewState;

  findings?: readonly ModelWorkbenchFinding[];

  readOnly?: boolean;

  promptDisabled?: boolean;

  isPrompting?: boolean;

  onRelationshipToggle: (relationshipId: string, approved: boolean) => void | Promise<void>;

  onDaxReview: (measureId: string, decision: "approved" | "rejected") => void | Promise<void>;

  onMeasureSelect: (measureId: string) => void;

  onAskContext?: (context: ConversationContext) => void;

}



type ModelView = "tables" | "relationships" | "measures";



const normalizedConfidence = (value: number) => Math.max(0, Math.min(1, value > 1 ? value / 100 : value));



const shortColumnName = (value: string) => {

  const segments = value.split(/[.[\]]/).filter(Boolean);

  return segments.at(-1) ?? value;

};



const validationLabel: Record<MeasureValidationStatus, string> = {

  pending: "Awaiting validation",

  valid: "Validated",

  warning: "Review warning",

  invalid: "Invalid",

};



function StatusIcon({ status }: { status: MeasureValidationStatus }) {

  if (status === "valid") return <CheckCircle2 aria-hidden="true" />;

  if (status === "invalid") return <Ban aria-hidden="true" />;

  if (status === "warning") return <AlertTriangle aria-hidden="true" />;

  return <Clock3 aria-hidden="true" />;

}



export function ModelWorkbench({

  tables,

  relationships,

  measures,

  selectedRelationshipIds,

  selectedMeasureId,

  reviewState,

  findings = [],

  readOnly = false,

  promptDisabled = readOnly,

  isPrompting = false,

  onRelationshipToggle,

  onDaxReview,

  onMeasureSelect,

  onAskContext,

}: ModelWorkbenchProps) {

  const [view, setView] = useState<ModelView>("tables");

  const [focusedRelationshipId, setFocusedRelationshipId] = useState<string>();

  const [reviewSearch, setReviewSearch] = useState("");

  const [reviewPage, setReviewPage] = useState(0);

  const instanceId = useId();

  const [pendingAction, setPendingAction] = useState<string>();

  const [actionError, setActionError] = useState<string>();

  const measureOptionRefs = useRef<Array<HTMLButtonElement | null>>([]);



  const selectedRelationships = useMemo(() => new Set<string>(selectedRelationshipIds), [selectedRelationshipIds]);

  const tablesById = useMemo(() => new Map(tables.map((table) => [table.id, table])), [tables]);

  const matchingMeasures = measures.filter(m => m.name.toLowerCase().includes(reviewSearch.toLowerCase()));

  const visibleMeasures = matchingMeasures.slice(reviewPage * 50, (reviewPage + 1) * 50);

  const matchingRelationships = relationships.filter(r => (!focusedRelationshipId || r.id === focusedRelationshipId) && [tablesById.get(r.fromTableId)?.name, tablesById.get(r.toTableId)?.name, r.fromColumnId, r.toColumnId, ...r.blockers].join(" ").toLowerCase().includes(reviewSearch.toLowerCase()));

  const visibleRelationships = matchingRelationships.slice(reviewPage * 50, (reviewPage + 1) * 50);

  const activeMeasure = visibleMeasures.find(m => m.id === selectedMeasureId) ?? visibleMeasures[0];



  const summary = useMemo(() => {

    const blockedRelationships = relationships.filter(

      (relationship) => relationship.reviewStatus !== "rejected" && relationship.blockers.length > 0,

    ).length;

    const pendingRelationships = relationships.filter(

      (relationship) => relationship.blockers.length === 0 && relationship.reviewStatus !== "rejected" && !selectedRelationships.has(relationship.id),

    ).length;

    const advancedPending = measures.filter(

      (measure) => measure.tier === "advanced_dax" && measure.approvalStatus === "pending",

    ).length;

    const invalidMeasures = measures.filter((measure) => measure.validation.status === "invalid").length;

    const pendingMeasureValidation = measures.filter((measure) => measure.validation.status === "pending").length;

    const warningMeasures = measures.filter((measure) => measure.validation.status === "warning").length;

    return { blockedRelationships, pendingRelationships, advancedPending, invalidMeasures, pendingMeasureValidation, warningMeasures };

  }, [measures, relationships, selectedRelationships]);



  const isReady =

    reviewState !== "blocked" &&

    summary.blockedRelationships === 0 &&

    summary.advancedPending === 0 &&

    summary.invalidMeasures === 0 &&

    summary.pendingMeasureValidation === 0 &&

    !findings.some((finding) => finding.severity === "error");

  const readinessLabel = reviewState === "approved" ? "Ready to build" : isReady ? "Ready to approve" : "Review";

  const readinessDetail = reviewState === "approved" ? "Approved content only" : isReady ? "Checklist complete" : "Review required";



  const tableLabel = (tableId: string) => tablesById.get(tableId)?.displayName || tablesById.get(tableId)?.name || tableId;

  const columnLabel = (tableId: string, columnId: string) => tablesById.get(tableId)?.columns?.find((column) => column.id === columnId)?.name ?? shortColumnName(columnId);



  const filterFlowLabel = (relationship: ModelWorkbenchRelationship) => {

    if (relationship.crossFilterDirection === "both") return "filters both ways";

    if (relationship.cardinality !== "many-to-one") return "single direction";

    return `${tableLabel(relationship.toTableId)} filters ${tableLabel(relationship.fromTableId)}`;

  };



  const runAction = async (key: string, action: () => void | Promise<void>) => {

    setActionError(undefined);

    setPendingAction(key);

    try {

      await action();

    } catch (error) {

      setActionError(error instanceof Error ? error.message : "The model review action could not be completed.");

    } finally {

      setPendingAction(undefined);

    }

  };





  const handleMeasureKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {

    let nextIndex: number | undefined;

    if (event.key === "ArrowDown") nextIndex = Math.min(index + 1, visibleMeasures.length - 1);

    else if (event.key === "ArrowUp") nextIndex = Math.max(index - 1, 0);

    else if (event.key === "Home") nextIndex = 0;

    else if (event.key === "End") nextIndex = visibleMeasures.length - 1;

    if (nextIndex === undefined) return;

    event.preventDefault();

    if (nextIndex === index) return;

    const nextMeasure = visibleMeasures[nextIndex];

    if (!nextMeasure) return;

    onMeasureSelect(nextMeasure.id);

    measureOptionRefs.current[nextIndex]?.focus();

  };



  return (

    <section className="model-workbench" aria-labelledby={`${instanceId}-title`}>

      <header className="mwb-header compact-authoring-toolbar">

        <h2 id={`${instanceId}-title`} >Model</h2>

            <div className="mwb-view-switch" role="group" aria-label="Semantic model view">

              <button type="button" className={view === "tables" ? "active" : ""} aria-pressed={view === "tables"} onClick={() => { setView("tables"); setReviewSearch(""); setFocusedRelationshipId(undefined); setReviewPage(0); }}>

                <Table2 aria-hidden="true" />Diagram

              </button>

              <button type="button" className={view === "relationships" ? "active" : ""} aria-pressed={view === "relationships"} onClick={() => { setView("relationships"); setReviewSearch(""); setFocusedRelationshipId(undefined); setReviewPage(0); }}>

                <ListTree aria-hidden="true" />Relationships

              </button>

              <button type="button" className={view === "measures" ? "active" : ""} aria-pressed={view === "measures"} onClick={() => { setView("measures"); setReviewSearch(""); setFocusedRelationshipId(undefined); setReviewPage(0); }}>

                <Sigma aria-hidden="true" />Measures

              </button>

            </div>

        {onAskContext && <AskAiButton about="the data model" disabled={promptDisabled || isPrompting} disabledReason="Finish the current action or save your report edits first" onClick={() => onAskContext({ label: "Data model" })} />}

      </header>



      {view !== "tables" && <div className="mwb-status-line"><span>{tables.length} tables · {relationships.length} relationships · {measures.length} measures</span><button type="button" onClick={() => setView(summary.blockedRelationships || summary.pendingRelationships ? "relationships" : "measures")}>

        {summary.blockedRelationships ? `${summary.blockedRelationships} relationship${summary.blockedRelationships === 1 ? "" : "s"} blocked · Review` : `${readinessLabel} · ${readinessDetail}`}

      </button></div>}

      {(findings.length > 0 || actionError) && (

        <details className="mwb-findings-disclosure" open={Boolean(actionError || findings.some(f => f.severity === "error"))}><summary>{findings.length + (actionError ? 1 : 0)} model notices</summary><div className="mwb-findings" role={actionError || findings.some((finding) => finding.severity === "error") ? "alert" : "status"}>

          {[...(actionError ? [{ id: "action-error", severity: "error" as const, message: actionError }] : []), ...findings].map((finding) => (

            <div key={finding.id} className={`mwb-finding mwb-finding-${finding.severity}`}>

              {finding.severity === "error" ? <Ban aria-hidden="true" /> : finding.severity === "warning" ? <AlertTriangle aria-hidden="true" /> : <Info aria-hidden="true" />}

              <span>{finding.message}</span>

            </div>

          ))}

        </div></details>

      )}



      <div className="mwb-layout">

        <section className="mwb-panel mwb-model-panel" aria-labelledby={`${instanceId}-model`}>

          {view !== "tables" && <div className="model-review-search">{view === "relationships" && focusedRelationshipId && <button className="secondary" onClick={() => { setFocusedRelationshipId(undefined); setReviewPage(0); }}>Show all relationships</button>}<input aria-label={`Search ${view}`} placeholder={`Search ${view}…`} value={reviewSearch} onChange={e => { setReviewSearch(e.target.value); setFocusedRelationshipId(undefined); setReviewPage(0); }}/><span>{(view === "measures" ? matchingMeasures : matchingRelationships).length} results</span><button className="secondary" disabled={!reviewPage} onClick={() => setReviewPage(reviewPage - 1)}>Previous</button><button className="secondary" disabled={(reviewPage + 1) * 50 >= (view === "measures" ? matchingMeasures : matchingRelationships).length} onClick={() => setReviewPage(reviewPage + 1)}>Next</button></div>}

          {view === "tables" && <ModelDiagram measureCount={measures.length} tables={tables} relationships={relationships} selectedIds={selectedRelationships} onReview={id => { setFocusedRelationshipId(id); setReviewSearch(""); setReviewPage(0); setView("relationships"); }} />}



          {view === "relationships" && (

            <div className="mwb-relationship-review">

              {visibleRelationships.length ? visibleRelationships.map((relationship) => {

                const hasBlockers = relationship.blockers.length > 0;

                const rejected = relationship.reviewStatus === "rejected";

                const blocked = hasBlockers && !rejected;

                const selected = selectedRelationships.has(relationship.id);

                const persistedApproval = relationship.reviewStatus === "approved";

                const confidence = normalizedConfidence(relationship.confidence);

                const inputId = `${instanceId}-relationship-${relationship.id}`;

                const actionPending = pendingAction === `relationship:${relationship.id}`;

                return (

                  <article key={relationship.id} className={`mwb-relationship-card ${blocked ? "blocked" : rejected ? "rejected" : selected ? "approved" : ""}`}>

                    <div className="mwb-relationship-main">

                      <input

                        id={inputId}

                        type="checkbox"

                        checked={selected}

                        disabled={readOnly || hasBlockers || Boolean(pendingAction)}

                        onChange={(event) => {

                          const checked = event.currentTarget.checked;

                          void runAction(`relationship:${relationship.id}`, () => onRelationshipToggle(relationship.id, checked));

                        }}

                      />

                      <label htmlFor={inputId}>

                        <span className="mwb-join-title">

                          <strong>{tableLabel(relationship.fromTableId)}</strong>

                          <code>{columnLabel(relationship.fromTableId, relationship.fromColumnId)}</code>

                          <GitBranch aria-hidden="true" />

                          <strong>{tableLabel(relationship.toTableId)}</strong>

                          <code>{columnLabel(relationship.toTableId, relationship.toColumnId)}</code>

                        </span>

                        <span className="mwb-join-meta">

                          {relationship.cardinality} · {filterFlowLabel(relationship)} · {rejected ? "excluded from model" : relationship.active === false ? "inactive" : "active"}

                        </span>

                      </label>

                      {relationship.recommendedByModel && relationship.reviewStatus === "pending" && (

                        <span className="mwb-reco" title="Recommended by Local AI — still requires your approval"><Sparkles aria-hidden="true" />Recommended</span>

                      )}

                      <span className={`mwb-review-state ${blocked ? "blocked" : rejected ? "rejected" : selected ? "approved" : relationship.reviewStatus}`}>

                        {actionPending ? "Updating…" : rejected ? "Rejected" : blocked ? "Blocked" : selected ? persistedApproval ? "Approved" : "Selected" : persistedApproval ? "Remove on approval" : relationship.reviewStatus}

                      </span>

                    </div>

                    <div className="mwb-confidence" style={{ "--mwb-confidence": `${confidence * 100}%` } as CSSProperties}>

                      <span><i />Confidence</span><strong>{Math.round(confidence * 100)}%</strong>

                    </div>

                    {onAskContext && <AskAiButton about={`relationship ${tableLabel(relationship.fromTableId)} → ${tableLabel(relationship.toTableId)}`} disabled={promptDisabled || isPrompting} disabledReason="Finish the current action or save your report edits first" onClick={() => onAskContext({ relationship_id: relationship.id, label: `${tableLabel(relationship.fromTableId)} → ${tableLabel(relationship.toTableId)}` })} />}

                    {relationship.blockers.length > 0 && (

                      <ul className="mwb-blockers" aria-label="Relationship blockers">

                        {relationship.blockers.map((blocker) => <li key={blocker}><Ban aria-hidden="true" />{blocker}</li>)}

                      </ul>

                    )}

                    {hasBlockers && (

                      <p className="mwb-relationship-note">

                        {rejected

                          ? "Excluded from the model — the integrity findings stay on record. To analyze this data, relate both tables through a shared key table instead."

                          : "Referential integrity requires every key on the many side to exist on the one side. Power BI guidance: relate both tables through a shared key table, or leave them unrelated rather than forcing this join."}

                      </p>

                    )}

                    {/* Orphan-key warnings and role-playing dates matter at review time; do not bury them in evidence. */}

                    {!hasBlockers && relationship.evidence.filter((item) => /^(Warning:|Role-playing date:)/.test(item)).map((item) => <p key={item} className="mwb-relationship-note">{item.replace(/^Warning: /, "")}</p>)}

                    {relationship.evidence.length > 0 && (

                      <details className="mwb-evidence">

                        <summary>{relationship.evidence.length} evidence item{relationship.evidence.length === 1 ? "" : "s"}</summary>

                        <ul>{relationship.evidence.map((item) => <li key={item}>{item}</li>)}</ul>

                      </details>

                    )}

                  </article>

                );

              }) : <EmptyState icon={<GitBranch aria-hidden="true" />} title="No relationship proposals" message="Local AI can rank candidates, while deterministic profiling decides whether each join is valid." />}

            </div>

          )}



          {view === "measures" && (

            visibleMeasures.length ? (

            <div className="mwb-measure-workspace">

              <div className="mwb-measure-list" role="listbox" aria-label="Measures" aria-orientation="vertical">

                {visibleMeasures.map((measure, index) => {

                  const selected = activeMeasure?.id === measure.id;

                  const status = validationLabel[measure.validation.status];

                  return (

                    <button

                      key={measure.id}

                      ref={(element) => { measureOptionRefs.current[index] = element; }}

                      type="button"

                      role="option"

                      aria-selected={selected}

                      aria-label={`${measure.name}, ${tableLabel(measure.tableId)} table, ${measure.tier === "advanced_dax" ? "Advanced DAX" : "Governed"}, ${status}`}

                      tabIndex={selected ? 0 : -1}

                      className={selected ? "selected" : ""}

                      onClick={() => onMeasureSelect(measure.id)}

                      onKeyDown={(event) => handleMeasureKeyDown(event, index)}

                    >

                      <span className="mwb-measure-glyph"><Sigma aria-hidden="true" /></span>

                      <span><strong>{measure.name}</strong><small>{tableLabel(measure.tableId)} · {measure.tier === "advanced_dax" ? "Advanced DAX" : "Governed"}</small></span>

                      <span className={`mwb-validation-dot ${measure.validation.status}`} aria-hidden="true" />

                    </button>

                  );

                })}

              </div>



              {activeMeasure && (

                <article className="mwb-measure-detail" aria-label={`${activeMeasure.name} measure details`}>

                  <div className="mwb-measure-detail-title">

                    <div>

                      <span className={`mwb-tier ${activeMeasure.tier}`}>{activeMeasure.tier === "advanced_dax" ? "Advanced DAX" : "Governed DSL"}</span>

                      <h4>{activeMeasure.name}</h4>

                    </div>

                    <div className="mwb-measure-detail-actions"><span className={`mwb-validation-badge ${activeMeasure.validation.status}`}>

                      <StatusIcon status={activeMeasure.validation.status} />{validationLabel[activeMeasure.validation.status]}

                    </span>{onAskContext && <AskAiButton about={`measure ${activeMeasure.name}`} disabled={promptDisabled || isPrompting} disabledReason="Finish the current action or save your report edits first" onClick={() => onAskContext({ measure_id: activeMeasure.id, label: activeMeasure.name })} />}</div>

                  </div>

                  <dl className="mwb-measure-meta">

                    <div><dt>Home table</dt><dd>{tableLabel(activeMeasure.tableId)}</dd></div>

                    {activeMeasure.operation && <div><dt>Operation</dt><dd>{activeMeasure.operation}</dd></div>}

                    {activeMeasure.formatString && <div><dt>Format</dt><dd><code>{activeMeasure.formatString}</code></dd></div>}

                  </dl>

                  <div className="mwb-expression">

                    <span>Expression</span>

                    <pre><code>{activeMeasure.expression || activeMeasure.operation || "Expression generated during deterministic compilation"}</code></pre>

                  </div>

                  {activeMeasure.validation.dependencies?.length ? (

                    <div className="mwb-dependencies"><span>Dependencies</span><div>{activeMeasure.validation.dependencies.map((dependency) => <code key={dependency}>{dependency}</code>)}</div></div>

                  ) : null}

                  {activeMeasure.assumptions?.length ? (

                    <div className="mwb-assumptions"><span>Assumptions to review</span><ul>{activeMeasure.assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}</ul></div>

                  ) : null}

                  {activeMeasure.validation.messages?.length ? (

                    <ul className={`mwb-measure-messages ${activeMeasure.validation.status}`}>

                      {activeMeasure.validation.messages.map((message) => <li key={message}>{message}</li>)}

                    </ul>

                  ) : null}



                  {activeMeasure.tier === "advanced_dax" && (

                    <div className="mwb-dax-review">

                      <div><ShieldCheck aria-hidden="true" /><span><strong>Additional approval required</strong><small>Advanced DAX is never committed from model output alone.</small></span></div>

                      <div className="mwb-dax-actions">

                        <button

                          type="button"

                          className="mwb-reject"

                          disabled={readOnly || Boolean(pendingAction) || activeMeasure.approvalStatus === "rejected"}

                          onClick={() => void runAction(`dax:${activeMeasure.id}:rejected`, () => onDaxReview(activeMeasure.id, "rejected"))}

                        >

                          <Ban aria-hidden="true" />{pendingAction === `dax:${activeMeasure.id}:rejected` ? "Saving…" : "Reject"}

                        </button>

                        <button

                          type="button"

                          className="mwb-approve"

                          disabled={readOnly || Boolean(pendingAction) || activeMeasure.validation.status === "invalid" || activeMeasure.approvalStatus === "approved"}

                          onClick={() => void runAction(`dax:${activeMeasure.id}:approved`, () => onDaxReview(activeMeasure.id, "approved"))}

                        >

                          <Check aria-hidden="true" />{pendingAction === `dax:${activeMeasure.id}:approved` ? "Saving…" : activeMeasure.approvalStatus === "approved" ? "Approved" : "Approve DAX"}

                        </button>

                      </div>

                    </div>

                  )}

                </article>

              )}

            </div>

            ) : <EmptyState icon={<Sigma aria-hidden="true" />} title="No measures proposed" message="Ask Local AI for a business calculation or let dataset analysis propose governed measures." />

          )}

        </section>





      </div>



    </section>

  );

}



function EmptyState({ icon, title, message }: { icon: React.ReactNode; title: string; message: string }) {

  return (

    <div className="mwb-empty-state">

      <span>{icon}</span>

      <strong>{title}</strong>

      <p>{message}</p>

    </div>

  );

}



export default ModelWorkbench;

