import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowRight, Calculator, Loader2, RefreshCw, X } from "lucide-react";
import {
  filterSourceLabel, matchPoint, measureChoices, pointChoices, stepValueText, type CellValue, type Explanation, type JudgementClient, type RenderedVisual,
} from "./judgement";

const valueText = (explanation: Explanation) => explanation.formatted ?? (explanation.value === null || explanation.value === undefined ? "(Blank)" : String(explanation.value));

/**
 * What Studio found about one number (docs/imported-number-explain.md): the value, the formula (shown here only, never sent
 * to the language model), each step with its value, the filters that reach it and the joins they take, and why it has
 * its colour. Used by the canvas side card and by the chat card of the `explanation` event.
 */
export function ExplainView({ explanation, compact = false }: { explanation: Explanation; compact?: boolean }) {
  const point = Object.entries(explanation.point);
  return <div className={`ie-explain${compact ? " compact" : ""}`}>
    <p className="ie-explain-value"><strong>{valueText(explanation)}</strong>
      {explanation.measure && <span> · {explanation.measure}</span>}
      {!explanation.computed && <span className="ie-explain-warn"><AlertTriangle size={12} aria-hidden="true" />Not computed</span>}</p>
    {(explanation.visual || point.length > 0) && <p className="ie-note">{explanation.visual}{point.length > 0 && ` · ${point.map(([key, value]) => `${key}: ${value === null ? "(Blank)" : String(value)}`).join(", ")}`}</p>}
    {explanation.period && <p className="ie-note">{explanation.period}</p>}
    {explanation.meaning && <p>{explanation.meaning}</p>}
    {explanation.blank && <p className="ie-warning">{explanation.blank}</p>}
    {explanation.formula && <details open={!compact}><summary>Formula</summary><pre className="ie-formula" aria-label={`DAX of ${explanation.measure ?? "the measure"}`}>{explanation.formula}</pre></details>}
    {explanation.steps.length > 0 && <details open={!compact}><summary>Steps ({explanation.steps.length})</summary>
      <ol className="ie-explain-steps">{explanation.steps.map((step, index) => {
        const noValue = step.usage === "per_row" || step.usage === "shifted";
        return <li key={index} style={{ paddingLeft: step.depth * 14 }} className={noValue ? "muted" : undefined}>
          <span className="ie-explain-step-name">{step.name}</span>
          <span className="ie-explain-step-value">{!step.computed && !noValue && <AlertTriangle size={12} aria-label="Not computed" />}{stepValueText(step)}</span>
          {step.meaning && <small>{step.meaning}</small>}
          {!step.computed && step.reason && <small className="ie-warning">{step.reason}</small>}
        </li>;
      })}</ol></details>}
    {explanation.filters.length > 0 && <details open={!compact}><summary>Filters in effect ({explanation.filters.length})</summary>
      <ul className="ie-explain-filters">{explanation.filters.map((filter, index) => <li key={index} className={filter.reaches === false ? "no-effect" : undefined}>
        <span className="ie-badge">{filterSourceLabel(filter.source)}</span>
        <span>{filter.field}{filter.condition ? ` ${filter.condition}` : ""}</span>
        {filter.reaches === false && <small>It does not change this number.</small>}
        {filter.how && filter.reaches !== false && <small>{filter.how}</small>}
      </li>)}</ul></details>}
    {explanation.relationships.length > 0 && <details open={!compact}><summary>Relationships used</summary>
      <ul className="ie-explain-joins">{explanation.relationships.map((join, index) => <li key={index}>
        <span className="ie-join-chain">{join.tables.map((table, at) => <span key={at}>{at > 0 && <ArrowRight size={11} aria-hidden="true" />}{table}</span>)}</span>
        {join.why && <small>{join.why}</small>}</li>)}</ul></details>}
    {explanation.formatting && <div className="ie-explain-colour">
      {explanation.formatting.color && <span className="ie-swatch" style={{ background: explanation.formatting.color }} aria-hidden="true" />}
      <span><strong>{explanation.formatting.name ?? explanation.formatting.color ?? "Colour"}</strong>{explanation.formatting.why ? `: ${explanation.formatting.why}` : ""}{explanation.formatting.rule ? ` (${explanation.formatting.rule})` : ""}</span>
    </div>}
    {explanation.notes.map(note => <p key={note} className="ie-note">{note}</p>)}
    {explanation.engine && <p className="ie-note">Computed by {explanation.engine}. No AI model made these figures.</p>}
  </div>;
}

/**
 * The side card "Explain this number" opens from the canvas: pick a data point (a chart's bar, a table's row) and the number
 * when the visual shows several, then Studio computes the explanation with the report's own engine.
 */
export function ExplainPanel({ workspaceId, pageId, visual, client, canvas, initial, onClose }: {
  workspaceId: string; pageId: string; visual: RenderedVisual; client: JudgementClient;
  canvas?: Record<string, unknown>;
  /** The data point that was right-clicked (its column values) and the number asked about. */
  initial?: { values?: Record<string, CellValue>; measure?: string };
  onClose: () => void;
}) {
  const points = useMemo(() => pointChoices(visual), [visual]);
  const measures = useMemo(() => measureChoices(visual), [visual]);
  const [pointKey, setPointKey] = useState(() => (matchPoint(points, initial?.values) ?? points[0])?.key ?? "");
  const [measureRef, setMeasureRef] = useState(() => (measures.find(m => m.ref === initial?.measure) ?? measures[0])?.ref ?? "");
  const [focus, setFocus] = useState<"value" | "colour" | "blank">("value");
  const [state, setState] = useState<{ loading: boolean; explanation?: Explanation; error?: string }>({ loading: false });
  const [asked, setAsked] = useState(0);
  // The request is keyed by what it asks, so a parent that re-renders (new rows, a new canvas object) does not ask again.
  const request = useMemo(() => {
    const point = points.find(item => item.key === pointKey);
    const measure = measures.length > 1 ? measures.find(item => item.ref === measureRef) : undefined;
    return JSON.stringify({
      page_id: pageId, visual_id: visual.id,
      ...(point || measure ? { point: { values: point?.values ?? {}, ...(measure ? { measure: measure.ref } : {}) } } : {}),
      ...(canvas ? { canvas } : {}), focus,
    });
  }, [points, measures, pointKey, measureRef, pageId, visual.id, canvas, focus]);

  useEffect(() => {
    let cancelled = false;
    setState(current => ({ loading: true, explanation: current.explanation }));
    client.explain(workspaceId, JSON.parse(request) as Record<string, unknown>)
      .then(explanation => { if (!cancelled) setState({ loading: false, explanation }); })
      .catch(reason => { if (!cancelled) setState({ loading: false, error: reason instanceof Error ? reason.message : "Studio could not explain this number." }); });
    return () => { cancelled = true; };
  }, [client, workspaceId, request, asked]);

  return <aside className="ie-sidecard" aria-label="Explain this number">
    <header><strong><Calculator size={14} aria-hidden="true" />Explain this number</strong>
      <button type="button" className="icon-button" aria-label="Close the explanation" onClick={onClose}><X size={14} /></button></header>
    <p className="ie-note">{visual.title || visual.id}</p>
    <div className="ie-sidecard-controls">
      {points.length > 0 && <label className="ie-inline">Data point<select value={pointKey} onChange={event => setPointKey(event.target.value)}>{points.map(point => <option key={point.key} value={point.key}>{point.label}</option>)}</select></label>}
      {measures.length > 1 && <label className="ie-inline">Number<select value={measureRef} onChange={event => setMeasureRef(event.target.value)}>{measures.map(measure => <option key={measure.ref} value={measure.ref}>{measure.label}</option>)}</select></label>}
      <label className="ie-inline">Explain<select value={focus} onChange={event => setFocus(event.target.value as typeof focus)}><option value="value">Why this value</option><option value="colour">Why this colour</option><option value="blank">Why it is blank</option></select></label>
    </div>
    <div className="ie-sidecard-body" aria-busy={state.loading}>
      {state.loading && <p className="ie-note" role="status"><Loader2 size={13} className="spin" aria-hidden="true" />Studio is computing every step (a few seconds)…</p>}
      {state.error && <div className="ie-note ie-error" role="alert">{state.error}<button type="button" className="secondary" onClick={() => setAsked(n => n + 1)}><RefreshCw size={12} />Try again</button></div>}
      {state.explanation && <ExplainView explanation={state.explanation} />}
    </div>
  </aside>;
}
