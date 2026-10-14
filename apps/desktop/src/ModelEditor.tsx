import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Database, HeartPulse, Layers, ListTree, Network, Settings2, Shield, Sigma, SlidersHorizontal, Table2, X } from "lucide-react";
import type { ImportedReport } from "./ImportedReport";
import type { Snapshot } from "./types";
import { editTransport, evaluateDax } from "./api";
import { EditProgress, useDelayedFlag } from "./EditProgress";
import { ProblemDialog } from "./EditorDialog";
import { MeasureEditor, NewMeasureForm } from "./MeasureEditor";
import { MAX_PENDING_BATCH, PendingModelChanges } from "./PendingModelChanges";
import { measureHomeTables } from "./daxMeasureSave";
import { describePending, describeReceipt, pendingStore, type Operation } from "./importedEditing";
import type { ImportedEditing } from "./useImportedEditing";
import {
  createModelClient, describeModelOp, modelStatusNote, queueModelChange, readModelFacts, readSupportCoverage, relationshipKey,
  type HealthObject, type ModelFacts, type ReportUse,
} from "./modelEditing";
import type { ModelContext } from "./ModelEditorShared";
import { ModelEditorTables } from "./ModelEditorTables";
import { ModelEditorRelationships } from "./ModelEditorRelationships";
import { ModelEditorCalculationGroups, ModelEditorFieldParameters, ModelEditorHierarchies, ModelEditorRoles } from "./ModelEditorObjects";
import { ModelEditorQuery, ModelEditorSettings, type FullFactsState } from "./ModelEditorQuery";
import { ModelEditorHealth } from "./ModelEditorHealth";
import { ModelStorageSummary } from "./ModelStorageSummary";

export type ModelSection = "tables" | "relationships" | "measures" | "hierarchies" | "parameters" | "calculation" | "roles" | "query" | "settings" | "health";
const SECTIONS: { id: ModelSection; label: string; Icon: typeof Table2 }[] = [
  { id: "tables", label: "Tables and columns", Icon: Table2 }, { id: "relationships", label: "Relationships", Icon: Network },
  { id: "measures", label: "Measures", Icon: Sigma }, { id: "hierarchies", label: "Hierarchies", Icon: ListTree },
  { id: "parameters", label: "Field parameters", Icon: SlidersHorizontal }, { id: "calculation", label: "Calculation groups", Icon: Layers },
  { id: "roles", label: "Roles", Icon: Shield }, { id: "query", label: "Power Query", Icon: Database }, { id: "settings", label: "Model settings", Icon: Settings2 },
  { id: "health", label: "Model health", Icon: HeartPulse },
];
// Sections that work without the model's facts (an unknown or still-reading model): the measures come from the report itself.
const WITHOUT_FACTS: ModelSection[] = ["measures"];
const SECTION_KEY = "studio.modelEditor.section";

const client = createModelClient(editTransport);

/**
 * The model workbench of an imported report: tables and columns, relationships, measures, hierarchies, field parameters,
 * calculation groups, roles, Power Query, model settings and model health. Each editor queues its change in "Pending
 * Power BI changes"; the tray applies them in one Power BI Desktop cycle only when the user asks, with the staged progress
 * strip, and the conflict dialogs of the editing session.
 */
export function ModelEditor({ report, snapshot, editing, locked, busy, onDax, onReportChanged }: {
  report: ImportedReport; snapshot?: Snapshot; editing: ImportedEditing; locked: boolean; busy: boolean;
  onDax: (formula: string) => void; onReportChanged: () => Promise<void> | void;
}) {
  const workspaceId = report.workspace_id;
  const payloadFacts = useMemo(() => readModelFacts(report.model), [report]);
  const headId = report.head?.id;
  const [full, setFull] = useState<{ facts?: ModelFacts; state: FullFactsState; error?: string; head?: string }>({ state: "loading" });
  const [section, setSectionState] = useState<ModelSection>(() => {
    try { const saved = localStorage.getItem(SECTION_KEY) as ModelSection | null; return saved && SECTIONS.some(s => s.id === saved) ? saved : "tables"; } catch { return "tables"; }
  });
  const setSection = (next: ModelSection) => { setSectionState(next); try { localStorage.setItem(SECTION_KEY, next); } catch { /* a convenience only */ } };
  const [tableSelection, setTableSelection] = useState<{ table?: string; column?: string }>({});
  const [relationship, setRelationship] = useState<string>();
  const [measureName, setMeasureName] = useState<string | undefined>(report.measures[0]?.Name);
  const [measureQuery, setMeasureQuery] = useState("");
  const [creatingMeasure, setCreatingMeasure] = useState(false);
  const [notice, setNotice] = useState<string>();
  const [applyingTray, setApplyingTray] = useState(false);
  const [reread, setReread] = useState<{ running: boolean; error?: string }>({ running: false });

  // The full facts (Power Query and DAX uncut) for the head this payload describes; the payload's bounded facts meanwhile.
  const loadFull = useCallback(async () => {
    if (payloadFacts.status !== "current") return;
    setFull(current => ({ ...current, state: "loading", error: undefined }));
    try {
      const facts = await client.fullFacts(workspaceId);
      setFull({ facts, state: "ready", head: headId });
    } catch (reason) {
      setFull({ state: "failed", error: reason instanceof Error ? reason.message : "Studio could not read the model in full.", head: headId });
    }
  }, [workspaceId, headId, payloadFacts.status]);
  useEffect(() => { void loadFull(); }, [loadFull]);
  const facts = useMemo(() => full.state === "ready" && full.facts && full.head === headId && full.facts.status === "current"
    ? { ...full.facts, studio: payloadFacts.studio } : payloadFacts, [full, headId, payloadFacts]);
  const known = facts.status === "current";

  // A model Studio is still reading appears by itself: the payload is asked for again every few seconds.
  const changed = useRef(onReportChanged);
  changed.current = onReportChanged;
  useEffect(() => {
    if (payloadFacts.status !== "reading") return;
    const timer = window.setTimeout(() => void changed.current(), 4000);
    return () => window.clearTimeout(timer);
  }, [payloadFacts.status, report]);

  const coverage = useMemo(() => readSupportCoverage(report.capabilities, report.head?.desktop_version), [report.capabilities, report.head?.desktop_version]);
  const reportUse: ReportUse = useMemo(() => ({ pages: report.pages.map(p => ({ title: p.title, visuals: p.visuals.map(v => ({ title: v.title, bindings: v.bindings })) })), measures: report.measures }), [report.pages, report.measures]);
  const queue = useCallback((op: Operation) => {
    const how = queueModelChange(pendingStore, workspaceId, op);
    const what = describeModelOp(op) ?? describePending(op);
    setNotice(`${how === "merged" ? "Updated in" : "Queued in"} Pending Power BI changes: ${what}. Nothing changes until you apply them.`);
  }, [workspaceId]);
  // The same check DAX Lab runs for an imported report: the query goes to Power BI Desktop's engine on the working copy.
  const check = useCallback((query: string) => evaluateDax(workspaceId, query, 5, undefined, undefined, undefined, undefined, "powerbi"), [workspaceId]);
  const validateQuery = useCallback((expression: string, table?: string) => client.validateQuery(workspaceId, expression, table), [workspaceId]);
  const ctx: ModelContext = { workspaceId, facts, report: reportUse, coverage, locked, queue, check, onDax, validateQuery };
  const loadHealth = useCallback((stats: boolean) => client.health(workspaceId, stats), [workspaceId]);
  const rowCounts = useMemo(() => Object.fromEntries((snapshot?.tables ?? []).map(t => [t.table.name, t.table.row_count])), [snapshot]);

  const focusObject = (o: HealthObject) => {
    if (o.kind === "column" || o.kind === "table") { setTableSelection({ table: o.kind === "table" ? o.name ?? o.table : o.table, column: o.kind === "column" ? o.name : undefined }); setSection("tables"); }
    else if (o.kind === "measure") { setMeasureName(o.name); setCreatingMeasure(false); setSection("measures"); }
    else if (o.kind === "relationship" && o.table && o.name && o.to_table && o.to_column) { setRelationship(relationshipKey({ fromTable: o.table, fromColumn: o.name, toTable: o.to_table, toColumn: o.to_column })); setSection("relationships"); }
    else if (o.kind === "calculation_item") setSection("calculation");
  };

  const activeBusy = editing.activeBusy;
  const showProgress = useDelayedFlag(activeBusy !== null, 450);
  const receipt = editing.lastReceipt ? describeReceipt(editing.lastReceipt) : undefined;
  const status = modelStatusNote(payloadFacts);
  const studio = payloadFacts.studio;
  const shown = known ? section : WITHOUT_FACTS.includes(section) ? section : "measures";

  const measures = report.measures.filter(m => `${m.Name} ${m.TableName}`.toLowerCase().includes(measureQuery.toLowerCase()));
  const measure = report.measures.find(m => m.Name === measureName);
  const measureTables = measureHomeTables(report.tables, report.measures);

  return <div className="imported-model me-workbench">
    {showProgress && activeBusy && <EditProgress label={activeBusy.label} stage={activeBusy.stage} desktop={activeBusy.desktop || applyingTray} />}
    <PendingModelChanges workspaceId={workspaceId} capabilities={report.capabilities} disabled={locked} onDax={onDax} facts={facts} coverage={coverage}
      onApply={changes => {
        const batch = changes.slice(0, MAX_PENDING_BATCH);
        setApplyingTray(true);
        void editing.submit(batch.map(change => change.op), { confirm_model: true, label: "Applying your changes in Power BI" })
          .then(outcome => { if (outcome.status === "applied") { batch.forEach(change => pendingStore.remove(workspaceId, change.id)); setNotice(undefined); } })
          .finally(() => setApplyingTray(false));
      }} />
    {receipt && !activeBusy && <div className={`ie-receipt ${receipt.tone} me-receipt`} role="status">
      <strong>{receipt.headline}</strong>
      {receipt.lines.length > 0 && <ul>{receipt.lines.map(line => <li key={line}>{line}</li>)}</ul>}
      {receipt.warnings.map(w => <small key={w}>{w}</small>)}
      <button type="button" className="icon-button" aria-label="Dismiss" onClick={editing.clearReceipt}><X size={13} /></button>
    </div>}
    {status && <p className={`imported-report-notice me-status ${status.tone}`} role="status">{status.text}{payloadFacts.status === "reading" && <button type="button" className="secondary" onClick={() => void onReportChanged()}>Check again</button>}</p>}
    <ModelStorageSummary storage={facts.storage} cachedTables={snapshot?.tables.map(table => table.table.name)}/>
    {known && studio && <StudioNotes studio={studio} reread={reread} onReread={async () => {
      setReread({ running: true });
      try { await client.rereadData(workspaceId); setReread({ running: false }); await onReportChanged(); }
      catch (reason) { setReread({ running: false, error: reason instanceof Error ? reason.message : "Studio could not read the data again." }); }
    }} disabled={locked || busy} />}
    <div className="me-body">
      <nav className="me-sections" aria-label="Model workbench">
        {SECTIONS.map(({ id, label, Icon }) => {
          const available = known || WITHOUT_FACTS.includes(id);
          return <button type="button" key={id} aria-current={shown === id ? "page" : undefined} disabled={!available} title={available ? undefined : "Needs the model's facts, which Studio has not read"} onClick={() => setSection(id)}>
            <Icon size={14} aria-hidden="true" /><span>{label}</span><small>{count(id, facts, report)}</small>
          </button>;
        })}
      </nav>
      <div className="me-main">
        {notice && <p className="me-notice" role="status">{notice}<button type="button" className="icon-button" aria-label="Dismiss" onClick={() => setNotice(undefined)}><X size={12} /></button></p>}
        {shown === "tables" && <ModelEditorTables ctx={ctx} selected={tableSelection} onSelect={setTableSelection} rowCounts={rowCounts} />}
        {shown === "relationships" && <ModelEditorRelationships ctx={ctx} selected={relationship} onSelect={setRelationship} />}
        {shown === "hierarchies" && <ModelEditorHierarchies ctx={ctx} />}
        {shown === "parameters" && <ModelEditorFieldParameters ctx={ctx} />}
        {shown === "calculation" && <ModelEditorCalculationGroups ctx={ctx} />}
        {shown === "roles" && <ModelEditorRoles ctx={ctx} />}
        {shown === "query" && <ModelEditorQuery ctx={ctx} full={full.head === headId ? full.state : "loading"} fullError={full.error} onRetry={() => void loadFull()} />}
        {shown === "settings" && <ModelEditorSettings key={headId} ctx={ctx} />}
        {shown === "health" && <ModelEditorHealth ctx={ctx} headId={headId} load={loadHealth} onFocusObject={focusObject} />}
        {shown === "measures" && <div className="ie-model-tools me-measure-tools"><button type="button" className="secondary" disabled={locked} onClick={() => setCreatingMeasure(true)}>New measure</button>
          <label className="me-search"><span className="sr-only">Find a measure</span><input placeholder="Find a measure…" value={measureQuery} onChange={e => setMeasureQuery(e.target.value)} /></label></div>}
        {shown === "measures" && <div className="imported-measures">
          <nav aria-label="Measures">
            {measures.map(m => <button type="button" key={`${m.TableName}.${m.Name}`} aria-current={!creatingMeasure && m.Name === measureName ? "true" : undefined} onClick={() => { setCreatingMeasure(false); setMeasureName(m.Name); }}>
              <Sigma size={14} aria-hidden="true" /><span>{m.Name}<small>{m.TableName}</small></span></button>)}
            {!measures.length && <p>No matching measures</p>}
          </nav>
          <section>{creatingMeasure
            ? <NewMeasureForm workspaceId={workspaceId} tables={measureTables} all={report.measures} disabled={locked} onDone={() => setCreatingMeasure(false)} onDax={onDax} />
            : measure && <MeasureEditor workspaceId={workspaceId} measure={measure} all={report.measures} disabled={locked} onDax={onDax} />}</section>
        </div>}
      </div>
    </div>
    {editing.dialog && <ProblemDialog problem={editing.dialog.problem} ui={editing.dialog.ui} onDecide={editing.answerDialog} />}
  </div>;
}

function count(section: ModelSection, facts: ModelFacts, report: ImportedReport): string {
  switch (section) {
    case "tables": return facts.status === "current" ? String(facts.tables.filter(t => !t.autoDateTable && !t.privateTable).length) : "";
    case "relationships": return facts.status === "current" ? String(facts.relationships.length) : "";
    case "measures": return String(report.measures.length);
    case "hierarchies": return facts.hierarchies.length ? String(facts.hierarchies.length) : "";
    case "parameters": return facts.fieldParameters.length ? String(facts.fieldParameters.length) : "";
    case "calculation": return facts.calculationGroups.length ? String(facts.calculationGroups.length) : "";
    case "roles": return facts.roles.length ? String(facts.roles.length) : "";
    default: return "";
  }
}

/** What Studio's canvas does with this model, and the "read the data again" action when Power BI refreshed it. */
function StudioNotes({ studio, reread, onReread, disabled }: { studio: NonNullable<ModelFacts["studio"]>; reread: { running: boolean; error?: string }; onReread: () => Promise<void>; disabled: boolean }) {
  const lines = [
    ...(studio.data?.stale && studio.data.detail ? [studio.data.detail] : []),
    ...studio.powerBiOnly.map(p => p.detail),
    ...(studio.changedCalculations.length ? [`Studio calculates ${studio.changedCalculations.length === 1 ? "one changed calculated object" : `${studio.changedCalculations.length} changed calculated objects`} itself where it can do so exactly (${studio.changedCalculations.slice(0, 3).map(c => c.column ? `${c.table}[${c.column}]` : c.table).join(", ")}); questions that read the others go to Power BI.`] : []),
    ...(studio.rowLevelSecurity ? [studio.rowLevelSecurity] : []),
  ];
  if (!lines.length && !studio.actions.includes("reread_data")) return null;
  return <details className="me-studio-notes">
    <summary>How Studio's canvas uses this model{studio.actions.includes("reread_data") ? " · the data changed in Power BI" : ""}</summary>
    <ul>{lines.map(line => <li key={line}>{line}</li>)}</ul>
    {studio.actions.includes("reread_data") && <div className="me-actions">
      <button type="button" className="secondary" disabled={disabled || reread.running} onClick={() => void onReread()}>{reread.running ? "Reading the data…" : "Read the data again"}</button>
      <small className="ie-note">Studio reads the report's rows again so its canvas and answers match what Power BI has.</small>
    </div>}
    {reread.error && <p className="ie-note ie-error" role="alert">{reread.error}</p>}
  </details>;
}
