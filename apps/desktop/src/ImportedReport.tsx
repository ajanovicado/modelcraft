import { useCallback, useEffect, useState, type MutableRefObject } from "react";
import { FileBarChart2 } from "lucide-react";
import type { Snapshot } from "./types";
import type { ConversationContext } from "./UnifiedWorkspace";
import "./imported-report.css";
import { ImportedEditor } from "./ImportedEditor";
import { ModelEditor } from "./ModelEditor";
import { useImportedEditing, type ImportedEditing } from "./useImportedEditing";
import { isFormattingOnly, touchedVisuals, type Delivery, type DesktopStatus, type EditCapabilities, type Head, type ThemePalette } from "./importedEditing";

type Binding = { kind: string; table: string; name: string; role: string };
/** `alt_text`: the visual's alt text (null when it has none). */
type NativeVisual = { id: string; title: string; visual_type: string; x: number; y: number; width: number; height: number; bindings: Binding[]; hidden?: boolean; alt_text?: string | null };
export type ImportedReport = {
  id: string; workspace_id: string; name: string; format: string; source_hash: string;
  pages: { id: string; title: string; width: number; height: number; hidden?: boolean; page_type?: "default" | "drillthrough" | "tooltip"; visuals: NativeVisual[] }[];
  measures: { TableName: string; Name: string; Expression: string; Description?: string; FormatString?: string }[];
  relationships: { FromTableName: string; FromColumnName: string; ToTableName: string; ToColumnName: string; IsActive: number; Cardinality: string; CrossFilteringBehavior: string }[];
  tables: { TableName: string; ColumnName: string; PandasDataType: string }[];
  warnings: string[];
  /** Bounded model facts, including the read-only storage receipt when identified. */
  model?: unknown;
  // Editing (docs/imported-report-editing-api.md): the revision this payload describes, what has reached the user's own
  // file, what Studio can do here, Power BI Desktop's state and the report's theme colors.
  head?: Head; delivery?: Delivery; capabilities?: EditCapabilities; desktop?: DesktopStatus; theme?: ThemePalette;
};

export function ImportedReportWorkspace({ report, snapshot, view, active = true, onOpenDesktop, onDax, onContext, onSelection, onReportChanged, onEditingBusy, editingRef, busy }: {
  report: ImportedReport; snapshot?: Snapshot; view: "report" | "model"; busy: boolean;
  active?: boolean; onOpenDesktop: () => Promise<void>; onDax: (formula: string) => void;
  /** "Ask AI": attach this visual to the conversation and open it. */
  onContext?: (context: ConversationContext) => void;
  /** The canvas selection, so the conversation's target chip follows it (undefined: nothing selected). */
  onSelection?: (context: ConversationContext | undefined) => void;
  /** Re-read the imported-report payload (a new head, delivery state, pages) after an edit. */
  onReportChanged?: () => Promise<void> | void;
  onEditingBusy?: (busy: boolean) => void;
  /** Where the report view publishes its editing session, so the conversation's Undo runs through the same queue and dialogs. */
  editingRef?: MutableRefObject<ImportedEditing | null>;
}) {
  const [pageId, setPageId] = useState(report.pages[0]?.id);
  const [format, setFormat] = useState<{ key: string; ids?: string[] }>({ key: "" });

  // A page that was deleted (or undone away) cannot stay selected.
  useEffect(() => { if (!report.pages.some(p => p.id === pageId)) setPageId(report.pages[0]?.id); }, [report.pages, pageId]);

  const reload = useCallback(async () => { await onReportChanged?.(); }, [onReportChanged]);
  const editing = useImportedEditing({
    workspaceId: report.workspace_id, head: report.head, desktopOpen: Boolean(report.desktop && report.desktop.state !== "closed"),
    onReportChanged: reload, onBusyChange: onEditingBusy,
    // A batch that only changed looks skips the data queries: the canvas is told in the same tick the head moves.
    onApplied: (receipt, operations) => { if (operations && isFormattingOnly(operations)) setFormat({ key: receipt.head.id, ids: touchedVisuals(operations) }); },
  });
  useEffect(() => {
    if (!editingRef || view !== "report") return;
    editingRef.current = editing;
    return () => { if (editingRef.current === editing) editingRef.current = null; };
  }, [editing, editingRef, view]);
  // A cycle started from the other tab locks this one too and shows the same strip.
  const activeBusy = editing.activeBusy;
  const locked = busy || activeBusy !== null || report.capabilities?.edit_report === false;
  const desktopLabel = "Open the edited copy in Power BI";
  return <section className="imported-report-workspace" aria-label="Imported Power BI report">
    <header className="imported-report-header">
      <div className="imported-report-identity">
        <div className="imported-report-title"><FileBarChart2 size={16} /><strong title={report.name}>{report.name}</strong><span className="imported-report-badge" title="Your original file is not changed until you save to it">Imported PBIX</span></div>
        <div className="imported-report-summary">{report.pages.length} {report.pages.length === 1 ? "page" : "pages"} <span>·</span> {report.pages.reduce((n, p) => n + p.visuals.length, 0)} visuals <span>·</span> {report.measures.length} measures <span>·</span> {report.relationships.length} relationships</div>
      </div>
      <button type="button" className="secondary" disabled={busy || activeBusy !== null} title="Opens Studio's edited copy in Power BI Desktop. Your original file is not changed." onClick={onOpenDesktop}><FileBarChart2 size={15} />{desktopLabel}</button>
    </header>
    {report.warnings.map(w => <p className="imported-report-notice" key={w}>{w}</p>)}
    {view === "report" ? <ImportedEditor report={report} active={active} appBusy={busy} pageId={pageId} onPage={setPageId} editing={editing}
        formatKey={format.key} formatIds={format.ids} onOpenDesktop={onOpenDesktop} onDax={onDax} onAsk={onContext} onSelection={onSelection} onReportChanged={reload} />
      : <ModelEditor report={report} snapshot={snapshot} editing={editing} locked={locked} busy={busy} onDax={onDax} onReportChanged={reload} />}
  </section>;
}
