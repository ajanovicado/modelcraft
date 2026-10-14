import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, CircleAlert, CircleHelp, FlaskConical, Lightbulb, Loader2, Pencil, Play, Plus, Trash2, X } from "lucide-react";
import { EditorDialog } from "./EditorDialog";
import {
  expectText, filtersText, matchPoint, measureChoices, pinBody, pointChoices, pointValue, suggestionBody, testStatuses, userTestBody,
  type CellValue, type JudgementClient, type RenderedVisual, type Suggestions, type TestDefinition, type TestDraft, type TestResult, type TestsState,
} from "./judgement";

export type ReportTests = {
  state: TestsState; loading: boolean; error?: string; running: boolean;
  reload: () => Promise<void>; run: () => Promise<void>; setState: (next: TestsState) => void;
};

/** The report's tests and their last run, shared by the Tests pane and the canvas badges. Reloaded when the head moves (a run may be stale). */
export function useReportTests(workspaceId: string, headId: string | undefined, client: JudgementClient, active: boolean): ReportTests {
  const [state, setState] = useState<TestsState>({ tests: [], stale: false });
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string>();
  const reload = useCallback(async () => {
    setLoading(true);
    try { setState(await client.tests(workspaceId)); setError(undefined); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "The report tests could not be read."); }
    finally { setLoading(false); }
  }, [client, workspaceId]);
  useEffect(() => { if (active) void reload(); }, [reload, headId, active]);
  const run = useCallback(async () => {
    setRunning(true);
    try {
      const result = await client.run(workspaceId, "auto");
      setState(current => ({ ...current, lastRun: result, stale: false }));
      setError(undefined);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "The tests could not run."); }
    finally { setRunning(false); }
  }, [client, workspaceId]);
  return { state, loading, error, running, reload, run, setState };
}

const STATUS: Record<TestResult["status"], { label: string; Icon: typeof CheckCircle2 }> = {
  passed: { label: "Passed", Icon: CheckCircle2 }, failed: { label: "Failed", Icon: CircleAlert }, not_checked: { label: "Not checked", Icon: CircleHelp },
};

function StatusBadge({ result }: { result?: TestResult }) {
  if (!result) return <span className="ie-badge">Not run</span>;
  const { label, Icon } = STATUS[result.status];
  return <span className={`ie-badge test-${result.status}`} title={result.message}><Icon size={11} aria-hidden="true" />{label}</span>;
}

const emptyDraft = (): TestDraft => ({ name: "", kind: "measure", expectKind: "equals", equals: "", tolerance: "", low: "", high: "", filters: [] });

/** A test the user writes: a measure or a DAX expression, simple filters, and what it should be. */
function AddTestDialog({ measures, columns, onCancel, onAdd }: {
  measures: { table: string; name: string }[]; columns: { table: string; name: string }[];
  onCancel: () => void; onAdd: (body: Record<string, unknown>) => Promise<string | undefined>;
}) {
  const [draft, setDraft] = useState<TestDraft>(emptyDraft);
  const [problem, setProblem] = useState<string>();
  const [saving, setSaving] = useState(false);
  const tables = useMemo(() => [...new Set(columns.map(column => column.table))], [columns]);
  const submit = async () => {
    const built = userTestBody(draft);
    if (!built.ok) { setProblem(built.message); return; }
    setSaving(true);
    setProblem(await onAdd(built.body));
    setSaving(false);
  };
  return <EditorDialog title="Add a report test" wide onCancel={onCancel}
    footer={<><button type="button" className="secondary" onClick={onCancel}>Cancel</button><button type="button" className="primary yellow" disabled={saving} onClick={() => void submit()}>{saving ? "Adding…" : "Add test"}</button></>}>
    <label className="ie-field-block">Name<input data-autofocus value={draft.name} maxLength={200} placeholder="Total sales in 2009" onChange={event => setDraft({ ...draft, name: event.target.value })} /></label>
    <fieldset className="ie-capture"><legend>What to compute</legend>
      <label><input type="radio" name="test-kind" checked={draft.kind === "measure"} onChange={() => setDraft({ ...draft, kind: "measure" })} />A measure</label>
      <label><input type="radio" name="test-kind" checked={draft.kind === "expression"} onChange={() => setDraft({ ...draft, kind: "expression" })} />A DAX expression</label>
    </fieldset>
    {draft.kind === "measure"
      ? <label className="ie-field-block">Measure<select value={draft.measure ? String(measures.findIndex(m => m.table === draft.measure?.table && m.name === draft.measure?.name)) : ""} onChange={event => setDraft({ ...draft, measure: event.target.value === "" ? undefined : measures[Number(event.target.value)] })}>
          <option value="">Pick a measure</option>{measures.map((measure, index) => <option key={`${measure.table}.${measure.name}`} value={String(index)}>{measure.name} ({measure.table})</option>)}</select></label>
      : <label className="ie-field-block">DAX expression<textarea rows={3} spellCheck={false} value={draft.expression ?? ""} placeholder="CALCULATE([Total Sales], 'Date'[Year] = 2009)" onChange={event => setDraft({ ...draft, expression: event.target.value })} /></label>}
    <div className="ie-field-block">Filters
      {draft.filters.map((filter, index) => <div key={index} className="ie-filter-row">
        <select aria-label={`Filter ${index + 1} table`} value={filter.table} onChange={event => setDraft({ ...draft, filters: draft.filters.map((f, i) => i === index ? { ...f, table: event.target.value, column: "" } : f) })}><option value="">Table</option>{tables.map(table => <option key={table} value={table}>{table}</option>)}</select>
        <select aria-label={`Filter ${index + 1} column`} value={filter.column} onChange={event => setDraft({ ...draft, filters: draft.filters.map((f, i) => i === index ? { ...f, column: event.target.value } : f) })}><option value="">Column</option>{columns.filter(column => column.table === filter.table).map(column => <option key={column.name} value={column.name}>{column.name}</option>)}</select>
        <input aria-label={`Filter ${index + 1} values`} placeholder="Values; separated by semicolons" value={filter.values} onChange={event => setDraft({ ...draft, filters: draft.filters.map((f, i) => i === index ? { ...f, values: event.target.value } : f) })} />
        <button type="button" className="icon-button" aria-label={`Remove filter ${index + 1}`} onClick={() => setDraft({ ...draft, filters: draft.filters.filter((_, i) => i !== index) })}><X size={12} /></button>
      </div>)}
      <button type="button" className="secondary" onClick={() => setDraft({ ...draft, filters: [...draft.filters, { table: "", column: "", values: "" }] })}><Plus size={12} />Add a filter</button>
    </div>
    <fieldset className="ie-capture"><legend>It should</legend>
      <label><input type="radio" name="test-expect" checked={draft.expectKind === "equals"} onChange={() => setDraft({ ...draft, expectKind: "equals" })} />Equal</label>
      <label><input type="radio" name="test-expect" checked={draft.expectKind === "between"} onChange={() => setDraft({ ...draft, expectKind: "between" })} />Be between</label>
      <label><input type="radio" name="test-expect" checked={draft.expectKind === "not_blank"} onChange={() => setDraft({ ...draft, expectKind: "not_blank" })} />Not be blank</label>
    </fieldset>
    {draft.expectKind === "equals" && <div className="ie-filter-row"><input aria-label="Expected value" placeholder="Value (empty: blank)" value={draft.equals} onChange={event => setDraft({ ...draft, equals: event.target.value })} /><input aria-label="Tolerance" placeholder="Within (optional)" value={draft.tolerance} onChange={event => setDraft({ ...draft, tolerance: event.target.value })} /></div>}
    {draft.expectKind === "between" && <div className="ie-filter-row"><input aria-label="Lowest value" placeholder="From" value={draft.low} onChange={event => setDraft({ ...draft, low: event.target.value })} /><input aria-label="Highest value" placeholder="To" value={draft.high} onChange={event => setDraft({ ...draft, high: event.target.value })} /></div>}
    {problem && <p className="ie-note ie-error" role="alert">{problem}</p>}
  </EditorDialog>;
}

/** "Pin this number": the visual's value (at a data point, for a chart or table) becomes a test under the canvas' filters. */
export function PinTestDialog({ workspaceId, pageId, visual, client, initial, onCancel, onPinned }: {
  workspaceId: string; pageId: string; visual: RenderedVisual; client: JudgementClient;
  /** The data point that was right-clicked (its column values) and the number asked about. */
  initial?: { values?: Record<string, CellValue>; measure?: string };
  onCancel: () => void; onPinned: (message: string) => void;
}) {
  const points = useMemo(() => pointChoices(visual), [visual]);
  const measures = useMemo(() => measureChoices(visual), [visual]);
  const [pointKey, setPointKey] = useState(() => (matchPoint(points, initial?.values) ?? points[0])?.key ?? "");
  const [measureRef, setMeasureRef] = useState(() => (measures.find(m => m.ref === initial?.measure) ?? measures[0])?.ref ?? "");
  const [name, setName] = useState(visual.title ?? "");
  const [problem, setProblem] = useState<string>();
  const [saving, setSaving] = useState(false);
  const point = points.find(item => item.key === pointKey);
  const measure = measures.find(item => item.ref === measureRef);
  const shown = pointValue(visual, point, measure);
  const pin = async () => {
    const body = pinBody(pageId, visual, point, measure, name);
    if (!body) { setProblem("This visual shows no number to pin."); return; }
    setSaving(true);
    try {
      const answer = await client.pin(workspaceId, body);
      const result = answer.result;
      onPinned(result && result.status !== "passed"
        ? `Pinned. Studio calculates ${result.shown ?? String(result.value)} here; the visual showed ${shown?.shown ?? "another value"}. ${result.message}`
        : `Pinned “${answer.test?.name ?? name}”: ${result?.message ?? "it passes now."}`);
    } catch (reason) { setProblem(reason instanceof Error ? reason.message : "The number could not be pinned."); }
    finally { setSaving(false); }
  };
  return <EditorDialog title="Pin this number as a test" onCancel={onCancel}
    footer={<><button type="button" className="secondary" onClick={onCancel}>Cancel</button><button type="button" className="primary yellow" disabled={saving || !shown} onClick={() => void pin()}>{saving ? "Pinning…" : "Pin"}</button></>}>
    <p className="ie-dialog-text">A report test remembers this number under the page's current filters. Studio checks it after every model change and asks before a change breaks it.</p>
    <label className="ie-field-block">Name<input data-autofocus value={name} maxLength={200} onChange={event => setName(event.target.value)} /></label>
    {points.length > 0 && <label className="ie-field-block">Data point<select value={pointKey} onChange={event => setPointKey(event.target.value)}>{points.map(item => <option key={item.key} value={item.key}>{item.label}</option>)}</select></label>}
    {measures.length > 1 && <label className="ie-field-block">Number<select value={measureRef} onChange={event => setMeasureRef(event.target.value)}>{measures.map(item => <option key={item.ref} value={item.ref}>{item.label}</option>)}</select></label>}
    <p className="ie-dialog-text">{shown ? <>It shows <strong>{shown.shown}</strong> now.</> : "This visual shows no number here."}</p>
    {problem && <p className="ie-note ie-error" role="alert">{problem}</p>}
  </EditorDialog>;
}

/**
 * The Tests pane (docs/imported-report-tests.md): the report's tests with their last result (passed, failed, not checked
 * with the reason on hover), Run, Add (a measure or a DAX expression, filters, expectation), Suggest (numbers worth
 * pinning, added one by one), edit a test's name and expectation, and delete. A run that is older than the report says so.
 */
export function TestsPane({ workspaceId, pageId, tests, client, measures, columns, disabled }: {
  workspaceId: string; pageId: string; tests: ReportTests; client: JudgementClient;
  measures: { table: string; name: string }[]; columns: { table: string; name: string }[]; disabled: boolean;
}) {
  const { state } = tests;
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<{ test: TestDefinition; name: string; equals: string }>();
  const [deleting, setDeleting] = useState<TestDefinition>();
  const [suggestions, setSuggestions] = useState<{ loading: boolean; found?: Suggestions; added: string[]; error?: string }>();
  const [message, setMessage] = useState<string>();
  const statuses = testStatuses(state);
  const failing = state.tests.filter(test => statuses.get(test.id)?.status === "failed").length;

  const add = async (body: Record<string, unknown>) => {
    try { await client.addTest(workspaceId, body); await tests.reload(); setAdding(false); setMessage("Test added. Run the tests to check it."); return undefined; }
    catch (reason) { return reason instanceof Error ? reason.message : "The test could not be added."; }
  };
  const suggest = async () => {
    setSuggestions({ loading: true, added: [] });
    try { setSuggestions({ loading: false, found: await client.suggest(workspaceId, [pageId]), added: [] }); }
    catch (reason) { setSuggestions({ loading: false, added: [], error: reason instanceof Error ? reason.message : "Studio could not suggest tests." }); }
  };
  const addSuggestion = async (raw: Record<string, unknown>, id: string) => {
    const body = suggestionBody(raw);
    if (!body) return;
    try { await client.addTest(workspaceId, body); setSuggestions(current => current ? { ...current, added: [...current.added, id] } : current); await tests.reload(); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "The test could not be added."); }
  };
  const saveEdit = async () => {
    const target = editing;
    if (!target) return;
    setEditing(undefined);
    const body: Record<string, unknown> = {};
    if (target.name.trim() && target.name.trim() !== target.test.name) body.name = target.name.trim().slice(0, 200);
    if ("equals" in target.test.expect && target.equals.trim() !== "") {
      const value = Number(target.equals.replace(/,/g, ""));
      body.expect = { ...target.test.expect, equals: Number.isFinite(value) ? value : target.equals.trim() };
    }
    if (!Object.keys(body).length) return;
    try { await client.updateTest(workspaceId, target.test.id, body); await tests.reload(); setMessage("Test updated."); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "The test could not be changed."); }
  };
  const remove = async () => {
    const target = deleting;
    setDeleting(undefined);
    if (!target) return;
    try { await client.deleteTest(workspaceId, target.id); await tests.reload(); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "The test could not be deleted."); }
  };

  return <div className="ie-tests">
    <div className="ie-pane-actions">
      <button type="button" className="secondary" disabled={tests.running || !state.tests.length} onClick={() => void tests.run()}>{tests.running ? <Loader2 size={13} className="spin" /> : <Play size={13} />}Run tests</button>
      <button type="button" className="secondary" disabled={disabled} onClick={() => setAdding(true)}><Plus size={13} />Add</button>
      <button type="button" className="secondary" disabled={suggestions?.loading} title="Numbers on this page worth pinning" onClick={() => void suggest()}><Lightbulb size={13} />Suggest</button>
    </div>
    {tests.error && <p className="ie-note ie-error" role="alert">{tests.error}</p>}
    {message && <p className="ie-note" role="status">{message}</p>}
    {state.lastRun && <p className={`ie-note${failing ? " ie-error" : ""}`}>{state.stale ? "The last run was for an earlier version of the report. Run the tests again. " : ""}{state.lastRun.text ?? `${state.lastRun.results.length} checked`}{state.lastRun.engine ? ` (${state.lastRun.engine})` : ""}</p>}
    {tests.loading && !state.tests.length && <p className="ie-note" role="status"><Loader2 size={12} className="spin" aria-hidden="true" />Reading the tests…</p>}
    {!tests.loading && !state.tests.length && <p className="ie-empty"><FlaskConical size={13} aria-hidden="true" /> No report tests yet. Pin a number from a visual (right-click it, Pin this number), Suggest, or Add one yourself.</p>}
    <ul className="ie-test-list">{state.tests.map(test => {
      const result = statuses.get(test.id);
      return <li key={test.id} className={result?.status === "failed" ? "failed" : undefined}>
        <div className="ie-test-head"><StatusBadge result={result} /><strong>{test.name}</strong>
          <span className="ie-row-tools">
            {"equals" in test.expect && <button type="button" className="icon-button" aria-label={`Edit ${test.name}`} title="Edit the name or the expected value" onClick={() => setEditing({ test, name: test.name, equals: test.expect && "equals" in test.expect && test.expect.equals !== null ? String(test.expect.equals) : "" })}><Pencil size={12} /></button>}
            <button type="button" className="icon-button" aria-label={`Delete ${test.name}`} title="Delete" onClick={() => setDeleting(test)}><Trash2 size={12} /></button>
          </span></div>
        <small>{test.measure ? test.measure.name : "DAX expression"} {expectText(test.expect)} · {filtersText(test.filters)}</small>
        {result && result.status !== "passed" && result.message && <small className={result.status === "failed" ? "ie-error" : "ie-note"}>{result.message}</small>}
      </li>;
    })}</ul>
    {suggestions && <section className="ie-suggestions-box" aria-label="Suggested tests">
      <header><strong><Lightbulb size={13} aria-hidden="true" />Suggested tests</strong><button type="button" className="icon-button" aria-label="Close the suggestions" onClick={() => setSuggestions(undefined)}><X size={12} /></button></header>
      {suggestions.loading && <p className="ie-note" role="status"><Loader2 size={12} className="spin" aria-hidden="true" />Studio is reading the numbers on this page…</p>}
      {suggestions.error && <p className="ie-note ie-error" role="alert">{suggestions.error}</p>}
      {suggestions.found && !suggestions.found.tests.length && <p className="ie-note">No numbers on this page to suggest.</p>}
      <ul className="ie-suggestions">{suggestions.found?.tests.map(item => <li key={item.test.id}>
        <span>{item.text}</span>
        {suggestions.added.includes(item.test.id) || state.tests.some(test => test.id === item.test.id)
          ? <span className="ie-card-status">Added</span>
          : <button type="button" className="secondary" onClick={() => void addSuggestion(item.raw, item.test.id)}>Add</button>}
      </li>)}</ul>
    </section>}
    {adding && <AddTestDialog measures={measures} columns={columns} onCancel={() => setAdding(false)} onAdd={add} />}
    {editing && <EditorDialog title="Edit the test" onCancel={() => setEditing(undefined)}
      footer={<><button type="button" className="secondary" onClick={() => setEditing(undefined)}>Cancel</button><button type="button" className="primary yellow" onClick={() => void saveEdit()}>Save</button></>}>
      <label className="ie-field-block">Name<input data-autofocus value={editing.name} maxLength={200} onChange={event => setEditing({ ...editing, name: event.target.value })} /></label>
      <label className="ie-field-block">Expected value<input value={editing.equals} onChange={event => setEditing({ ...editing, equals: event.target.value })} /></label>
      <p className="ie-note">What the test computes (its measure and filters) stays the same; pin a new number for a different one.</p>
    </EditorDialog>}
    {deleting && <EditorDialog title={`Delete “${deleting.name}”?`} tone="warn" onCancel={() => setDeleting(undefined)}
      footer={<><button type="button" className="secondary" data-autofocus onClick={() => setDeleting(undefined)}>Cancel</button><button type="button" className="primary yellow" onClick={() => void remove()}>Delete test</button></>}>
      <p className="ie-dialog-text">Studio stops checking this number. The report itself does not change.</p>
    </EditorDialog>}
  </div>;
}
