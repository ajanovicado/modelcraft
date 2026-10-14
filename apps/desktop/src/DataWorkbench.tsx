import { useEffect, useState, type ReactNode } from "react";
import { AlertTriangle, ArrowDown, ArrowUp, ArrowUpDown, CheckCircle2, ChevronLeft, ChevronRight, Database, Plus, Search, Table2 } from "lucide-react";
import { api } from "./api";
import { AskAiButton } from "./AskAiButton";
import { DatabaseSources } from "./DatabaseSources";
import type { Snapshot, Workspace } from "./types";
import "./data-workbench.css";
import { DataPreviewControls, type RowFilter, type RowSort } from "./DataPreviewControls";
const EMPTY_QUERY: { filters: RowFilter[]; sort?: RowSort } = { filters: [] };

type Preview = { columns: { id: string; name: string; data_type: string }[]; rows: (string | number | boolean | null)[][]; offset: number; total_rows: number; snapshot_rows: number; has_more: boolean; truncated_cells: boolean; elapsed_ms: number };

const dataNumber = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 12 });
const dataInteger = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const cellLabel = (value: string | number | boolean, type?: string) => {
  if (typeof value === "number") return (Number.isSafeInteger(value) ? dataInteger : dataNumber).format(value);
  if (typeof value === "string" && (type === "date" || type === "datetime")) return value.replace("T", " ").replace(/ 00:00:00$/, "");
  return String(value);
};

function PowerBiSourceHelp({ busy, onImportReport }: { busy: boolean; onImportReport?: () => void }) {
  return <details className="local-source-section powerbi-source-help"><summary><Database size={16}/>Power BI connectors <span>SharePoint, PostgreSQL, Fabric and more</span></summary>
    <p>Set up the source with Get data in Power BI Desktop, sign in and load or refresh successfully. Save the PBIX, then open it here. Connector availability and connection modes depend on your Desktop environment.</p>
    <div className="source-report-actions">{onImportReport && <button className="secondary" disabled={busy} onClick={onImportReport}>Open Power BI report</button>}<a href="https://learn.microsoft.com/en-us/power-query/connectors/" target="_blank" rel="noreferrer">View connector catalog</a></div>
  </details>;
}

export function DataWorkbench({ workspace, snapshot, busy, onImported, onImportBusy, onAskTable, onOpenSourceReport, onImportReport, children }: {
  workspace?: Workspace; snapshot?: Snapshot; busy: boolean; onImported: () => Promise<void>; onImportBusy: (value: boolean) => void;
  onAskTable?: (id: string, name: string) => void; onOpenSourceReport?: () => void; onImportReport?: () => void; children: ReactNode;
}) {
  const [view, setView] = useState<"tables" | "sources">(snapshot ? "tables" : "sources");
  const [activeId, setActiveId] = useState(snapshot?.tables[0]?.id);
  const [search, setSearch] = useState("");
  const [tablePage, setTablePage] = useState(0);
  const [mode, setMode] = useState<"preview" | "columns" | "quality">("preview");
  const [offset, setOffset] = useState(0);
  const [columnPage, setColumnPage] = useState(0);
  const [columnSearch, setColumnSearch] = useState("");
  const [preview, setPreview] = useState<Preview>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState<{ scope: string; filters: RowFilter[]; sort?: RowSort }>({ scope: "", filters: [] });
  const [attempt, setAttempt] = useState(0);
  const firstTableId = snapshot?.tables[0]?.id;
  useEffect(() => {
    setActiveId(firstTableId); setOffset(0); setColumnPage(0); setColumnSearch("");
    setSearch(""); setTablePage(0); setQuery({ scope: "", filters: [] }); setPreview(undefined); setError("");
  }, [snapshot?.id, firstTableId]);
  const active = snapshot?.tables.find(t => t.id === activeId) ?? snapshot?.tables[0];
  const scope = `${snapshot?.id}:${active?.id}`;
  const { filters, sort } = query.scope === scope ? query : EMPTY_QUERY;
  const fields = active?.table.columns ?? [];
  const changeQuery = (next: { filters: RowFilter[]; sort?: RowSort }) => { setOffset(0); setQuery({ scope, ...next }); };
  const sortColumn = (id: string) => changeQuery({ filters, sort: sort?.column_id !== id ? { column_id: id, direction: "asc" } : sort.direction === "asc" ? { column_id: id, direction: "desc" } : undefined });
  const matchingFields = fields.filter(c => c.name.toLowerCase().includes(columnSearch.toLowerCase()));
  const shownFields = matchingFields.slice(columnPage * 40, (columnPage + 1) * 40);
  const ids = shownFields.map(c => c.id).join(",");
  useEffect(() => {
    if (!snapshot || !active || view !== "tables" || mode !== "preview" || !ids) return;
    const abort = new AbortController();
    setLoading(true); setError(""); setPreview(undefined);
    api<Preview>(`/v1/snapshots/${snapshot.id}/preview`, { method: "POST", signal: abort.signal,
      body: JSON.stringify({ table_id: active.id, column_ids: ids.split(","), offset, page_size: 50, filters, sort }) })
      .then(value => { if (!abort.signal.aborted) setPreview(value); })
      .catch(reason => { if (!abort.signal.aborted) setError(reason instanceof Error ? reason.message : "Preview could not load"); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [snapshot, active, offset, ids, view, mode, attempt, filters, sort]);
  const tables = snapshot?.tables.filter(t => t.table.name.toLowerCase().includes(search.toLowerCase())) ?? [];
  const findings = snapshot?.findings.filter(f => !f.table_id || f.table_id === active?.id) ?? [];
  const columnControls = <><label className="workbench-search"><Search size={14}/><input aria-label="Find columns" placeholder="Find columns…" value={columnSearch} onChange={e => { setColumnSearch(e.target.value); setColumnPage(0); }}/></label><span>{shownFields.length} of {matchingFields.length} columns</span>{matchingFields.length > 40 && <><button className="secondary" aria-label="Previous columns" disabled={!columnPage} onClick={() => setColumnPage(columnPage - 1)}><ChevronLeft size={14}/></button><button className="secondary" aria-label="Next columns" disabled={(columnPage + 1) * 40 >= matchingFields.length} onClick={() => setColumnPage(columnPage + 1)}><ChevronRight size={14}/></button></>}</>;
  return <section className="data-workbench" aria-label="Data workbench">
    <header className="workbench-toolbar"><h2>Data</h2><div className="workbench-tabs" role="group" aria-label="Data view"><button aria-pressed={view === "tables"} className={view === "tables" ? "active" : ""} onClick={() => setView("tables")} disabled={!snapshot}>Tables</button><button aria-pressed={view === "sources"} className={view === "sources" ? "active" : ""} onClick={() => setView("sources")}>Sources</button></div>{!workspace?.imported_report_id && <button className="secondary" onClick={() => setView("sources")} disabled={busy}><Plus size={15}/>Add source</button>}</header>
    {view === "sources" && workspace?.imported_report_id ? <div className="data-sources-scroll"><section className="source-snapshot-summary" aria-label="Power BI-managed sources"><Database size={18}/><div><strong>Power BI-managed sources</strong><p>Power BI Desktop manages this report’s connections, credentials and refresh.</p>{snapshot?.tables.length ? <p>Saved local snapshot · {snapshot.tables.length} tables imported from the PBIX. This is the data saved with the report, not a live connection.</p> : <p>No local data snapshot is available. Report structure can still be reviewed; local data preview requires saved import data.</p>}<p>Connect or refresh in Power BI Desktop, save the PBIX, then import that updated report here. DirectQuery and live connections are not reconnected by this app.</p><div className="source-report-actions">{onOpenSourceReport && <button className="secondary" disabled={busy} onClick={onOpenSourceReport}>Open in Power BI Desktop</button>}{onImportReport && <button className="secondary" disabled={busy} onClick={onImportReport}>Import updated PBIX</button>}</div></div></section></div> : view === "sources" ? <div className="data-sources-scroll">{snapshot && <div className="source-snapshot-summary"><CheckCircle2 size={18}/><div><strong>{snapshot.database_source ? `${snapshot.database_source.provider === "snowflake" ? "Snowflake" : "SQL Server / Azure SQL"} · ${snapshot.database_source.profile} · ${snapshot.database_source.schema_name}` : "Local files"}</strong><p>Active snapshot · {snapshot.tables.length} tables · Imported {new Date(snapshot.created_at).toLocaleString()}</p></div></div>}<DatabaseSources workspace={workspace} existingSource={snapshot?.database_source} busy={busy} onImportBusy={onImportBusy} onImported={async () => { await onImported(); setView("tables"); }}/><PowerBiSourceHelp busy={busy} onImportReport={onImportReport}/><details className="local-source-section" open={!snapshot}><summary><Database size={16}/>Local files <span>CSV, Excel and Parquet</span></summary>{children}</details><p className="source-mode-note">Database imports create a new local snapshot. Existing snapshots and build evidence are retained. Refreshing a remote source requires another explicit import. These database imports use snapshot mode; configure DirectQuery and live connections in Power BI Desktop.</p></div>
    : <div className="data-workbench-body">
      <aside className="workbench-explorer" aria-label="Data table explorer"><h3>Tables <span>{snapshot?.tables.length ?? 0}</span></h3><label className="workbench-search"><Search size={14}/><input aria-label="Search data tables" placeholder="Search tables…" value={search} onChange={e => { setSearch(e.target.value); setTablePage(0); }}/></label><div className="workbench-table-list">{tables.slice(tablePage * 50, (tablePage + 1) * 50).map(t => <button key={t.id} aria-pressed={t.id === active?.id} className={t.id === active?.id ? "active" : ""} onClick={() => { setActiveId(t.id); setOffset(0); setColumnPage(0); setColumnSearch(""); }}><Table2 size={16}/><span><strong>{t.table.name}</strong><small>{t.table.row_count.toLocaleString()} rows</small></span></button>)}{!tables.length && <p>No matching tables.</p>}</div>{tables.length > 50 && <div className="workbench-paging"><button className="secondary" disabled={!tablePage} onClick={() => setTablePage(tablePage - 1)}>Previous</button><button className="secondary" disabled={(tablePage + 1) * 50 >= tables.length} onClick={() => setTablePage(tablePage + 1)}>Next</button></div>}</aside>
      {active && <section className="data-table-workspace" aria-label={`${active.table.name} table`}><header className="data-table-heading"><div><h3>{active.table.name}</h3><small>{active.table.row_count.toLocaleString()} rows · {fields.length} columns · Snapshot {new Date(snapshot!.created_at).toLocaleDateString()}</small></div>{onAskTable && <AskAiButton about={`table ${active.table.name}`} disabled={busy} onClick={() => onAskTable(active.id, active.table.name)}/>}<div className="workbench-tabs" role="group" aria-label="Table view">{(["preview", "columns", "quality"] as const).map(v => <button key={v} aria-pressed={mode === v} className={mode === v ? "active" : ""} onClick={() => setMode(v)}>{v[0].toUpperCase() + v.slice(1)}</button>)}</div></header>
        {mode === "columns" && <div className="data-column-controls">{columnControls}</div>}
        {mode === "preview" && <><DataPreviewControls key={scope} columnControls={columnControls} fields={fields} filters={filters} sort={sort} onFilters={next => changeQuery({ filters: next, sort })} onSort={next => changeQuery({ filters, sort: next })}/>{!ids ? <p className="workbench-empty">No matching columns.</p> : error ? <div className="workbench-empty" role="alert"><AlertTriangle size={20}/><p>{error}</p><button className="secondary" onClick={() => setAttempt(attempt + 1)}>Retry preview</button></div> : loading ? <p className="workbench-empty" role="status">Loading preview…</p> : preview && <div className="data-grid-scroll"><table className="workbench-grid"><thead><tr>{preview.columns.map(c => <th key={c.id} aria-sort={sort?.column_id === c.id ? sort.direction === "asc" ? "ascending" : "descending" : "none"}><button className="data-sort-heading" aria-label={`Sort by ${c.name}`} onClick={() => sortColumn(c.id)}><span>{c.name}<small>{c.data_type}</small></span>{sort?.column_id === c.id ? sort.direction === "asc" ? <ArrowUp size={13}/> : <ArrowDown size={13}/> : <ArrowUpDown size={13}/>}</button></th>)}</tr></thead><tbody>{preview.rows.map((row, i) => <tr key={offset + i}>{row.map((v, j) => <td key={j} title={v == null ? "Blank" : String(v)}>{v == null ? <span className="cell-null">null</span> : cellLabel(v, preview.columns[j]?.data_type)}</td>)}</tr>)}</tbody></table>{!preview.rows.length && <p className="workbench-empty">{filters.length ? "No rows match these filters. Adjust or clear them to see more data." : "No rows in this table."}</p>}</div>}
        <footer className="data-preview-footer"><span>{preview && ids ? `${(preview.offset + (preview.rows.length ? 1 : 0)).toLocaleString()}–${(preview.offset + preview.rows.length).toLocaleString()} of ${preview.total_rows.toLocaleString()} rows${filters.length ? ` matching · ${preview.snapshot_rows.toLocaleString()} in snapshot` : ""}` : "Preview"} · 50 rows per page{preview?.truncated_cells ? " · Long text shortened" : ""}</span><div><button className="secondary" disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}><ChevronLeft size={14}/>Previous</button><button className="secondary" disabled={loading || !preview?.has_more || !ids} onClick={() => setOffset(offset + 50)}>Next<ChevronRight size={14}/></button></div></footer></>}
        {mode === "columns" && <div className="data-grid-scroll"><table className="workbench-grid"><thead><tr><th>Column</th><th>Type</th><th>Complete</th><th>Distinct values</th><th>Role</th></tr></thead><tbody>{shownFields.map(c => <tr key={c.id}><td>{c.name}</td><td>{c.data_type}</td><td>{c.row_count ? (((c.row_count - c.null_count) / c.row_count) * 100).toFixed(1) + "%" : "No rows"}</td><td>{c.distinct_count_exact === false ? "At least " : ""}{c.distinct_count.toLocaleString()}</td><td>{c.candidate_key ? "Key candidate" : "Field"}</td></tr>)}</tbody></table></div>}
        {mode === "quality" && <div className="data-quality-scroll"><h4>Profile findings</h4><p>Calculated from the imported snapshot. Relationship validation is shown in Model.</p>{findings.length ? findings.map(f => <article key={f.id}><AlertTriangle size={16}/><div><strong>{f.severity} · {f.code.replaceAll("_", " ")}</strong><p>{f.message}</p></div></article>) : <p><CheckCircle2 size={16}/> No profile findings recorded.</p>}<h4>Missing values</h4>{fields.some(c => c.null_count) ? fields.filter(c => c.null_count).slice(0, 100).map(c => <p key={c.id}>{c.name}: {c.null_count.toLocaleString()} of {c.row_count.toLocaleString()} rows</p>) : <p>No missing values in the profiled columns.</p>}</div>}
      </section>}
    </div>}
  </section>;
}
