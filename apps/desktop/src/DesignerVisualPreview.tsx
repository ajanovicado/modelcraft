import { memo, useMemo } from "react";
import { PreviewChart } from "./PreviewChart";
import type { Draft, Measure, Visual } from "./types";

type Preview = Draft["previews"][number];

// Layout, titles and display styles can change without changing the query.
// Never attach old evidence to a different field, measure or grain.
export function samePreviewBinding(visual: Visual, saved?: Visual): boolean {
  if (!saved) return false;
  const binding = (item: Visual) => [item.recipe, item.table_id, item.measure_id ?? null,
    item.category_column_id ?? null, item.detail_column_ids, item.date_filter ?? null];
  return JSON.stringify(binding(visual)) === JSON.stringify(binding(saved));
}

export function formatPreviewValue(value: unknown, formatString?: string): string {
  if (value == null) return "—";
  if (typeof value !== "number" || !Number.isFinite(value)) return String(value);
  const decimals = formatString?.match(/\.(0+)/)?.[1].length;
  return value.toLocaleString(undefined, formatString?.includes("%")
    ? { style: "percent", minimumFractionDigits: decimals ?? 1, maximumFractionDigits: decimals ?? 1 }
    : { maximumFractionDigits: decimals ?? 2 });
}

export function previewEmptyCopy(visual: Visual, preview?: Preview, measure?: Measure): { title: string; detail: string } {
  const reason = preview?.empty_reason;
  // Simple lab formulas now preview locally, so a specific reason (such as an
  // unapproved relationship) must win over the generic advanced-DAX copy.
  if (reason === "advanced_dax" || (measure?.tier === "advanced_dax" && (!reason || reason === "unsupported_operation"))) {
    return { title: "Calculated in Power BI", detail: "Local preview cannot evaluate this formula. Review the DAX, then validate in Desktop." };
  }
  if (!measure && visual.recipe !== "detail") {
    return { title: "Missing binding", detail: "Choose a reviewed measure, then Save and revalidate." };
  }
  if (reason === "pending_relationship") {
    return { title: "Relationship not approved", detail: "Approve the table relationship in Review & build, then save to preview this chart." };
  }
  if (reason === "unsupported_operation") {
    return { title: "No local preview", detail: "This governed formula is compiled in Power BI. The logical canvas cannot calculate it here." };
  }
  if (reason === "empty_result" || (preview?.query_plan_hash && !preview.rows.length)) {
    return { title: "Empty query result", detail: "The bounded aggregate returned no rows for this grain." };
  }
  if (reason === "aggregate_plan_error" || (preview && !preview.query_plan_hash)) {
    return { title: "Could not compute preview", detail: "Inspect the binding, approve relationships if this chart is cross-table, then Save and revalidate." };
  }
  return { title: "No preview data", detail: "No local result is available for this binding. Inspect its source and measure, or validate in Power BI." };
}

function PreviewMessage({ title, detail }: { title: string; detail: string }) {
  return <div className="designer-preview-message" role="status"><span>{title}</span><small>{detail}</small></div>;
}

// Use only the bounded, saved evidence. This is a presentation adapter, not an
// analytical engine: it performs no aggregation, inference or DAX evaluation.
// Mark colors come from the draft theme through PreviewChart's config.
export function designerChartSpec(visual: Visual, preview: Preview, metric: string, category: string) {
  const tooltip = [
    { field: "group_1", type: visual.recipe === "trend" ? "temporal" : "nominal", title: category },
    { field: "value", type: "quantitative", title: metric },
  ];
  const data = { values: preview.rows };
  if (visual.recipe === "trend") return {
    data,
    mark: { type: visual.visual_type === "lineChart" ? "line" : "area", point: true },
    encoding: { x: { field: "group_1", type: "temporal", title: category },
      y: { field: "value", type: "quantitative", title: metric }, tooltip },
  };
  const columns = ["clusteredColumnChart", "columnChart"].includes(visual.visual_type);
  if (columns || ["clusteredBarChart", "barChart"].includes(visual.visual_type)) return {
    data, mark: { type: "bar" },
    encoding: columns
      ? { x: { field: "group_1", type: "nominal", title: category, sort: "-y" },
        y: { field: "value", type: "quantitative", title: metric }, tooltip }
      : { y: { field: "group_1", type: "nominal", title: category, sort: "-x" },
        x: { field: "value", type: "quantitative", title: metric }, tooltip },
  };
  // Funnel, waterfall and composition styles need their own rendering semantics.
  // Show their actual aggregate table rather than inventing a misleading shape.
  return null;
}

type DesignerVisualPreviewProps = {
  visual: Visual; saved?: Visual; preview?: Preview; measure?: Measure; category: string;
  view?: "visual" | "table"; rowOffset?: number; rowLimit?: number;
};

function DesignerVisualPreviewContent({ visual, saved, preview, measure, category, view = "visual", rowOffset = 0, rowLimit = 8 }: DesignerVisualPreviewProps) {
  const fresh = samePreviewBinding(visual, saved);
  const spec = useMemo(() => preview && fresh && ["trend", "category"].includes(visual.recipe)
    ? designerChartSpec(visual, preview, measure?.name || "Value", category) : null,
  [visual, preview, fresh, measure?.name, category]);
  if (!fresh) return <PreviewMessage title="Preview needs refresh" detail="Save and revalidate to calculate these bindings." />;
  if (!preview || !preview.rows.length) {
    const copy = previewEmptyCopy(visual, preview, measure);
    return <PreviewMessage title={copy.title} detail={copy.detail} />;
  }
  if (measure?.tier === "advanced_dax" && preview.rows[0]?.value == null) {
    return <PreviewMessage title="Calculated in Power BI" detail="This DAX needs Desktop validation." />;
  }
  if (visual.recipe === "kpi" && view === "visual") return <div className="designer-preview-kpi" title={`Exact value: ${String(preview.rows[0]?.value ?? "No value")}`}>
    <span>{formatPreviewValue(preview.rows[0]?.value, measure?.format_string)}</span>
  </div>;
  if (spec && view === "visual") return <PreviewChart spec={spec} label={visual.title} formatString={measure?.format_string} />;
  const columns = Object.keys(preview.rows[0]);
  const rows = preview.rows.slice(rowOffset, rowOffset + Math.max(1, Math.min(50, rowLimit)));
  return <div className="designer-preview-table">
    <table aria-label={`${visual.title} snapshot values`}><thead><tr>{columns.map((column) => <th key={column}>{column === "group_1" ? category : column === "value" ? measure?.name || "Value" : column}</th>)}</tr></thead>
    <tbody>{rows.map((row, index) => <tr key={index}>{columns.map((column) => <td key={column} title={String(row[column] ?? "")}>{formatPreviewValue(row[column], column === "value" ? measure?.format_string : undefined)}</td>)}</tr>)}</tbody>
    </table>
    <p>{view === "visual" && visual.recipe !== "detail" ? "Aggregate values · final shape in Power BI" : `Showing ${rows.length} of ${preview.rows.length} preview rows`}</p>
  </div>;
}

export const DesignerVisualPreview = memo(function DesignerVisualPreview(props: DesignerVisualPreviewProps) {
  const content = <DesignerVisualPreviewContent {...props} />;
  return props.visual.forecast ? <div className="designer-forecast-preview">
    <p className="designer-forecast-status">Forecast enabled · calculated in Power BI</p>
    {content}
  </div> : content;
});
