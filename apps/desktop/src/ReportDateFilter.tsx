import { useLayoutEffect, useRef, useState } from "react";
import { CalendarRange, X } from "lucide-react";
import type { Draft, Snapshot, Visual } from "./types";

export function ReportDateFilter({ pages, measures, snapshot, initialVisualId, busy, onApply, onClose }: {
  pages: Draft["pages"]; measures: Draft["measure_proposal"]["measures"]; snapshot: Snapshot;
  initialVisualId?: string; busy: boolean;
  onApply: (visualId: string, filter: Visual["date_filter"]) => Promise<void>; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const candidates = pages.flatMap(page => page.visuals.filter(v => v.recipe !== "detail").map(visual => ({ page, visual })));
  const [visualId, setVisualId] = useState(initialVisualId || "");
  const visual = candidates.find(c => c.visual.id === visualId)?.visual;
  const measure = measures.find(m => m.id === visual?.measure_id);
  const table = snapshot.tables.find(t => t.table.id === measure?.table_id)?.table;
  const dateColumns = table?.columns.filter(c => c.data_type === "date" || c.data_type === "datetime") || [];
  const [fieldId, setFieldId] = useState(visual?.date_filter?.column_id || "");
  const effectiveField = fieldId || (dateColumns.some(c => c.id === visual?.category_column_id) ? visual?.category_column_id : dateColumns.length === 1 ? dateColumns[0].id : "");
  const [start, setStart] = useState(visual?.date_filter?.start || "");
  const [end, setEnd] = useState(visual?.date_filter?.end || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const disabled = busy || saving;
  const invalidRange = Boolean(start && end && start > end);
  const supported = measure?.tier === "governed" && ["sum", "count", "distinct_count", "average", "minimum", "maximum", "ratio", "margin", "variance"].includes(measure.operation);
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const node = dialog.current;
    node?.showModal();
    return () => { node?.close(); if (previous?.isConnected) previous.focus(); };
  }, []);
  const apply = async (clear = false) => {
    if (disabled || !visual || !table || (!clear && (!supported || !effectiveField || invalidRange || (!start && !end)))) return;
    setSaving(true); setError("");
    try {
      await onApply(visual.id, clear ? null : { table_id: table.id, column_id: effectiveField!, start: start || null, end: end || null });
      onClose();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "The date filter could not be saved."); }
    finally { setSaving(false); }
  };
  return <dialog ref={dialog} className="report-date-filter" aria-labelledby="report-date-filter-title" onCancel={event => { if (disabled) event.preventDefault(); else onClose(); }}>
    <header><div><h2 id="report-date-filter-title"><CalendarRange size={19}/>Date filter</h2><p>Only the selected visual</p></div><button className="secondary icon-only" aria-label="Close date filter" disabled={disabled} onClick={onClose}><X size={17}/></button></header>
    <form onSubmit={event => { event.preventDefault(); void apply(); }}>
      <fieldset disabled={disabled}>
        <label>Visual<select value={visualId} onChange={event => {
          const next = candidates.find(c => c.visual.id === event.target.value)?.visual;
          setVisualId(event.target.value); setFieldId(next?.date_filter?.column_id || "");
          setStart(next?.date_filter?.start || ""); setEnd(next?.date_filter?.end || ""); setError("");
        }}><option value="">Choose a chart or card…</option>{candidates.map(({page, visual: v}) => <option key={v.id} value={v.id}>{page.title} · {v.title}</option>)}</select></label>
        {visual && !supported && <p className="report-filter-note">Date-filter previews are available for simple aggregate and ratio measures. This measure needs Power BI for its full evaluation.</p>}
        {visual && supported && <>
          <label>Date field<select value={effectiveField} onChange={event => setFieldId(event.target.value)} required><option value="">Choose a date field…</option>{dateColumns.map(c => <option key={c.id} value={c.id}>{table?.name} · {c.name}</option>)}</select></label>
          {!dateColumns.length && <p className="report-filter-note">This measure's source table has no date fields.</p>}
          <div className="report-filter-dates"><label>From<input type="date" max={end || "9999-12-30"} value={start} onChange={event => setStart(event.target.value)}/></label><label>Through<input type="date" min={start || undefined} max="9999-12-30" value={end} onChange={event => setEnd(event.target.value)}/></label></div>
          <p className="report-filter-note">Both dates are inclusive. Leave either date empty for an open range. The measure and other visuals stay unchanged.</p>
        </>}
        {(invalidRange || error) && <p role="alert" className="report-filter-error">{error || "From must be on or before Through."}</p>}
      </fieldset>
      <footer><button type="button" className="secondary" disabled={disabled || !visual?.date_filter} onClick={() => void apply(true)}>Clear filter</button><button type="submit" className="primary yellow" disabled={disabled || !supported || !effectiveField || invalidRange || (!start && !end)}>{saving ? "Applying…" : "Apply & preview"}</button></footer>
    </form>
  </dialog>;
}
