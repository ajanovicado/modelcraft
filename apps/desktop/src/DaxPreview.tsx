import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChartLine, ListFilter, Loader2, Search, Table2, X } from "lucide-react";
import { evaluateDax } from "./api";
import type { DaxDatePreview, DaxPreviewContext, DaxResult, Snapshot } from "./types";
import { DaxResults } from "./DaxResults";
import { PreviewChart } from "./PreviewChart";
import { breakdownRows, breakdownChartRows } from "./daxBreakdown";

export function DaxPreview({ result, snapshot, expression, workspaceId, disabled }: {
  result: DaxResult; snapshot?: Snapshot; expression: string; workspaceId: string; disabled: boolean;
}) {
  const [context, setContext] = useState<DaxPreviewContext>();
  const [preview, setPreview] = useState<DaxResult>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [view, setView] = useState<"visual" | "table">("visual");
  const [selected, setSelected] = useState("");
  const [search, setSearch] = useState("");
  const [details, setDetails] = useState<DaxResult>();
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState("");
  const request = useRef<AbortController | null>(null);
  const detailRequest = useRef<AbortController | null>(null);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => { request.current?.abort(); detailRequest.current?.abort(); clearTimeout(searchTimer.current); }, []);
  const fields = result.preview_fields ?? [];
  const field = fields.find(item => item.column_id === context?.column_id);
  const isDate = field?.data_type === "date" || field?.data_type === "datetime";
  const rows = useMemo(() => breakdownRows(preview), [preview]);
  const series = useMemo(() => breakdownChartRows(preview), [preview]);
  const metadata = preview?.breakdown;

  const clearRequests = () => {
    request.current?.abort(); detailRequest.current?.abort(); clearTimeout(searchTimer.current);
    setPreview(undefined); setError(""); setSelected(""); setDetails(undefined);
    setDetailsLoading(false); setDetailsError("");
  };
  const changeContext = async (next?: DaxPreviewContext) => {
    clearRequests();
    setContext(next); setSearch(next?.search ?? ""); setLoading(Boolean(next));
    if (!next) return;
    const controller = new AbortController(); request.current = controller;
    try {
      const value = await evaluateDax(workspaceId, expression, 100, next, undefined, controller.signal);
      if (!controller.signal.aborted) setPreview(value);
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not calculate this breakdown.");
    } finally { if (!controller.signal.aborted) setLoading(false); }
  };
  const searchGroups = (value: string) => {
    if (!context) return;
    clearRequests(); setSearch(value); setLoading(true);
    // Search the complete imported column, not just the first 100 returned groups.
    searchTimer.current = setTimeout(() => void changeContext({ ...context, search: value }), 300);
  };
  const inspectGroup = useCallback(async (key: string) => {
    const row = rows.find(item => item.key === key);
    if (!context || disabled || !row || (isDate && !row.period)) return;
    detailRequest.current?.abort();
    const controller = new AbortController(); detailRequest.current = controller;
    setSelected(key); setDetails(undefined); setDetailsError(""); setDetailsLoading(true);
    try {
      const value = await evaluateDax(workspaceId, expression, 50, context, row.period, controller.signal, isDate ? undefined : { value: row.raw });
      if (!controller.signal.aborted) setDetails(value);
    } catch (reason) {
      if (!controller.signal.aborted) setDetailsError(reason instanceof Error ? reason.message : "Could not load source rows.");
    } finally { if (!controller.signal.aborted) setDetailsLoading(false); }
  }, [context, disabled, expression, workspaceId, rows, isDate]);
  const datumClicked = useCallback((datum: Record<string, unknown>) => {
    // Vega parses temporal fields; a separate key retains the original value and type.
    if (typeof datum.key === "string") void inspectGroup(datum.key);
  }, [inspectGroup]);
  const spec = useMemo(() => isDate ? {
    data: { values: series },
    mark: { type: "line", point: { filled: true, size: 55 }, color: "#4a78b8", cursor: "pointer" },
    encoding: {
      x: { field: "period", type: "temporal", title: metadata?.field.name, scale: { type: "utc" }, axis: { format: context?.grain === "day" ? "%d %b %Y" : context?.grain === "year" ? "%Y" : "%b %Y" } },
      y: { field: "value", type: "quantitative", title: metadata?.value_column, scale: { zero: true } },
      tooltip: [{ field: "label", type: "nominal", title: "Period" }, { field: "value", type: "quantitative", title: metadata?.value_column }],
    },
  } : {
    data: { values: series }, mark: { type: "bar", color: "#4a78b8", cursor: "pointer" },
    encoding: {
      x: { field: "value", type: "quantitative", title: metadata?.value_column, scale: { zero: true } },
      y: { field: "label", type: "nominal", title: metadata?.field.name, sort: null },
      tooltip: [{ field: "label", type: "nominal", title: metadata?.field.name }, { field: "value", type: "quantitative", title: metadata?.value_column }],
    },
  }, [series, metadata, context?.grain, isDate]);
  const inspectedRow = rows.find(row => row.key === selected);
  const inspectable = rows.filter(row => !isDate || row.period);

  if (!fields.length) return <DaxResults result={result} snapshot={snapshot} />;
  return <div className="dax-date-preview">
    <div className="dax-preview-controls" aria-label="Preview breakdown">
      <ListFilter size={16} aria-hidden="true" />
      <label>View by<select aria-label="View by" value={context?.column_id ?? ""} disabled={disabled} onChange={event => {
        const next = fields.find(item => item.column_id === event.target.value);
        void changeContext(next ? { column_id: next.column_id, ...(next.data_type === "date" || next.data_type === "datetime" ? { grain: "month" } : {}) } : undefined);
      }}>
        <option value="">Overall total</option>
        {fields.map(item => <option key={item.column_id} value={item.column_id}>{item.name}</option>)}
      </select></label>
      {isDate && context && <label>Group<select aria-label="Date grouping" value={context.grain ?? "month"} disabled={disabled} onChange={event => void changeContext({ ...context, grain: event.target.value as DaxDatePreview["grain"] })}>
        <option value="day">Day</option><option value="month">Month</option><option value="quarter">Quarter</option><option value="year">Year</option>
      </select></label>}
      {context && !isDate && <label className="dax-group-search"><Search size={14} aria-hidden="true" /><input type="search" aria-label={`Search ${field?.name ?? "groups"}`} placeholder={`Find ${field?.name ?? "a value"}…`} value={search} maxLength={100} disabled={disabled} onChange={event => searchGroups(event.target.value)} /></label>}
      <span className="dax-preview-note">Preview only · Formula unchanged</span>
    </div>
    {!context ? <DaxResults result={result} snapshot={snapshot} /> : <section className="dax-lab-results" aria-label="Breakdown results" aria-busy={loading}>
      <header className="dax-results-heading"><div><h2>Results</h2><span>{metadata && preview ? `${metadata.field.name} · ${preview.row_count} ${isDate ? "periods" : "groups"} · ${preview.elapsed_ms} ms` : `Calculating by ${field?.name ?? "field"}`}</span></div>
        <div className="dax-result-switch" role="group" aria-label="Breakdown display">
          <button type="button" aria-pressed={view === "visual"} onClick={() => setView("visual")}><ChartLine size={14} />Chart</button>
          <button type="button" aria-pressed={view === "table"} onClick={() => setView("table")}><Table2 size={14} />Table</button>
        </div>
      </header>
      {loading && <div className="dax-results-empty" role="status"><Loader2 className="spin" size={22} /><span>Calculating your breakdown…</span></div>}
      {error && <div className="dax-lab-error" role="alert">{error}<button className="secondary" onClick={() => void changeContext(context)}>Retry</button></div>}
      {preview && metadata && <>
        {preview.truncated && <p className="dax-lab-truncated">{isDate ? "Showing up to the first 100 periods. Choose a broader grouping to see more of the timeline." : "Showing up to 100 groups, highest values first. Search to find a specific value across all source rows."}</p>}
        {view === "visual" ? <>
          {series.length ? <div className="dax-date-chart" style={isDate ? undefined : { height: Math.max(250, Math.min(660, series.length * 30 + 65)), flexBasis: Math.max(250, Math.min(660, series.length * 30 + 65)) }}><PreviewChart spec={spec} label={`${metadata.value_column} by ${metadata.field.name}`} onDatumClick={datumClicked} /></div> : <p>{rows.length ? "No dated results to plot. Check the table for blank dates." : "No matching results. Try another search."}</p>}
          <p className="dax-chart-note">{isDate ? "Each point" : "Each bar"} recalculates the measure for that {isDate ? "period" : "group"}. Click to inspect its source rows.{!isDate && rows.length > series.length ? ` Showing ${series.length} of ${rows.length} returned groups; see all returned groups in Table.` : ""}</p>
          {isDate && rows.some(row => row.raw === null) && <p className="dax-chart-note">Rows without a date appear in the table under “(Blank)”.</p>}
        </> : <div className="dax-lab-grid-wrap"><table className="dax-lab-grid"><thead><tr><th>{isDate ? "Period" : metadata.field.name}</th><th>{metadata.value_column}</th></tr></thead><tbody>
          {rows.map(row => <tr key={row.key}><td>{!isDate || row.period ? <button className="dax-period-link" disabled={disabled} onClick={() => void inspectGroup(row.key)}>{row.label}</button> : row.label}</td><td>{row.value === null ? "(blank)" : row.value.toLocaleString(undefined, { maximumFractionDigits: 6 })}</td></tr>)}
        </tbody></table>{!rows.length && <p className="dax-lab-hint">No matching results. Try another search.</p>}</div>}
        {inspectable.length > 0 && <label className="dax-inspect-select">Inspect {isDate ? "period" : "group"}<select aria-label={isDate ? "Inspect period" : "Inspect group"} value={selected} disabled={disabled} onChange={event => { if (event.target.value) void inspectGroup(event.target.value); }}><option value="">Choose {isDate ? "a period" : "a group"} to see source rows</option>{inspectable.map(row => <option value={row.key} key={row.key}>{row.label}</option>)}</select></label>}
        {inspectedRow && <section className="dax-period-details" aria-label="Group source rows"><header><div><strong>{inspectedRow.label}</strong><p>Source rows used with your formula’s filters · {metadata.field.table}</p></div><button className="icon-button" aria-label="Close source rows" onClick={() => { detailRequest.current?.abort(); setSelected(""); setDetails(undefined); }}><X size={16} /></button></header>
          {detailsLoading && <p role="status">Loading source rows…</p>}{detailsError && <p role="alert">{detailsError}</p>}
          {details && <DaxResults key={`${selected}:${details.elapsed_ms}`} result={details} initialView="table" />}
        </section>}
        <details className="dax-result-plan"><summary>Evaluation details</summary><code>{preview.plan}</code><p>Preview fields come from the measure’s table. Explicitly filtered fields are excluded. Full relationship behavior requires Power BI.</p></details>
      </>}
    </section>}
  </div>;
}
