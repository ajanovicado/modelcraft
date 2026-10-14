import { useMemo, useState } from "react";
import { BarChart3, Table2 } from "lucide-react";
import type { DaxResult, Snapshot } from "./types";
import { PreviewChart } from "./PreviewChart";
import { daxChartRows, daxResultDefaults, numericResultColumns } from "./daxAssistance";

export function DaxResults({ result, snapshot, initialView }: { result: DaxResult; snapshot?: Snapshot; initialView?: "visual" | "table" }) {
  const numeric = useMemo(() => numericResultColumns(result), [result]);
  const columns = result.columns.filter(column => column !== result.grand_total_column);
  const defaults = useMemo(() => daxResultDefaults(result, snapshot), [result, snapshot]);
  const [view, setView] = useState<"visual" | "table">(initialView ?? defaults.view);
  const [metricChoice, setMetric] = useState("");
  const [categoryChoice, setCategory] = useState<string>();
  const metric = numeric.includes(metricChoice) ? metricChoice : defaults.metric || numeric[0] || "";
  const requestedCategory = categoryChoice ?? defaults.category;
  const category = requestedCategory !== metric && result.columns.includes(requestedCategory) ? requestedCategory : "";
  const scalar = result.rows.length === 1 && result.columns.length === 1 && numeric.length === 1;
  const chartRows = useMemo(() => daxChartRows(result, category, metric), [result, category, metric]);
  const spec = useMemo(() => ({
    data: { values: chartRows }, mark: { type: "bar", color: "#4a78b8" },
    encoding: {
      x: { field: "value", type: "quantitative", title: metric, scale: { zero: true } },
      y: { field: "category", type: "nominal", title: category || "Row", sort: null },
      tooltip: [{ field: "category", type: "nominal", title: category || "Row" }, { field: "value", type: "quantitative", title: metric }],
    },
  }), [chartRows, category, metric]);
  const visual = view === "visual" && numeric.length > 0;
  return <section className="dax-lab-results" aria-label="DAX results">
    <header className="dax-results-heading"><div><h2>Results</h2><span>{result.row_count.toLocaleString()} returned row{result.row_count === 1 ? "" : "s"} · {result.elapsed_ms} ms</span></div>
      {numeric.length > 0 && <div className="dax-result-switch" role="group" aria-label="Result display">
        <button type="button" aria-pressed={visual} onClick={() => setView("visual")}><BarChart3 size={14} />Visual</button>
        <button type="button" aria-pressed={!visual} onClick={() => setView("table")}><Table2 size={14} />Table</button>
      </div>}
    </header>
    {result.truncated && <p className="dax-lab-truncated">Preview limited to the first {result.row_count} rows.</p>}
    {visual ? scalar ? <div className="dax-result-scalar"><span>{metric}</span><strong title={String(result.rows[0][metric])}>{(result.rows[0][metric] as number).toLocaleString(undefined, { maximumFractionDigits: 6 })}</strong><small>{result.engine === "powerbi" ? "Calculated by Power BI Desktop" : "Calculated from your imported snapshot"}</small></div> : <>
      <div className="dax-chart-fields">
        <label>Category<select value={category} onChange={event => setCategory(event.target.value)}><option value="">Row number</option>{columns.filter(column => column !== metric).map(column => <option key={column}>{column}</option>)}</select></label>
        <label>Value<select value={metric} onChange={event => { setMetric(event.target.value); if (categoryChoice === event.target.value) setCategory(""); }}>{numeric.map(column => <option key={column}>{column}</option>)}</select></label>
      </div>
      <div className="dax-result-chart" style={{ height: Math.max(260, Math.min(560, chartRows.length * 30 + 65)) }}><PreviewChart spec={spec} label={`${metric} by ${category || "row"}`} /></div>
      <p className="dax-chart-note">One bar per detail row.{result.grand_total_column ? " Grand totals appear in the table." : ""}{result.rows.length !== chartRows.length ? ` Showing ${chartRows.length} numeric values; blank values are omitted and charts show up to 20 bars.` : ""}</p>
    </> : <div className="dax-lab-grid-wrap"><table className="dax-lab-grid"><thead><tr>{columns.map(column => <th key={column}>{column}</th>)}</tr></thead><tbody>{result.rows.map((row, index) => {
      const total = !!result.grand_total_column && row[result.grand_total_column] === true;
      return <tr key={index} className={total ? "dax-grand-total" : undefined}>{columns.map((column, columnIndex) => <td key={column} title={typeof row[column] === "number" ? String(row[column]) : undefined}>{total && columnIndex === 0 ? "Grand total" : row[column] === null || row[column] === undefined ? <span className="dax-lab-blank">{total ? "—" : "(blank)"}</span> : typeof row[column] === "number" ? row[column].toLocaleString(undefined, { maximumFractionDigits: 6 }) : String(row[column])}</td>)}</tr>;
    })}</tbody></table>{!result.rows.length && <p className="dax-lab-hint">The expression returned no rows.</p>}</div>}
    <details className="dax-result-plan"><summary>Evaluation details</summary><code>{result.plan}</code><p>{result.engine === "powerbi" ? "Uses the open working copy’s model and relationships. Page slicer selections are separate from this query." : "Local preview supports a subset of DAX. Confirm full model behavior in Power BI before building."}</p></details>
  </section>;
}
