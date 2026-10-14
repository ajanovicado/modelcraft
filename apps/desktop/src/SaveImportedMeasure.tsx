import { useEffect, useRef, useState } from "react";
import { Save } from "lucide-react";
import { measureNameProblem } from "./importedEditing";
import { queuedMeasureNote, splitMeasureDefinition } from "./daxMeasureSave";
import { AsyncActionButton } from "./AsyncActionButton";
import { AsyncActionLifetime, type AsyncActionStatus } from "./asyncAction";

const FORMATS = ["#,0", "#,0.00", "0.0%", "0%", "$#,0.00", "0.00"];

/**
 * DAX Lab's "Save as a measure" for an imported report: name, the table the measure lives in, and its number
 * format. Saving queues a new measure in "Pending Power BI changes" (nothing runs until the user applies it), so
 * the button says "Queue". DaxLab renders this in place of the draft-only save controls when it has imported
 * measures, and passes `onSave` straight to App's `saveDaxMeasure(name, expression, {table, format})`.
 */
export function SaveImportedMeasure({ tables, existing, expression, suggestedName, disabled, contextKey = "imported", onSave }: {
  /** Tables of the model a measure can live in. */
  tables: string[]; existing: string[]; expression: string;
  /** A name worth offering (the one the formula came with); it fills the box until the user types their own. */
  suggestedName?: string; disabled: boolean;
  /** Owner and data identity; a late result must not confirm a different form. */
  contextKey?: string;
  onSave: (name: string, expression: string, options: { table: string; format?: string }) => Promise<void> | void;
}) {
  const [name, setName] = useState(suggestedName ?? "");
  const [touched, setTouched] = useState(false);
  const [table, setTable] = useState(tables[0] ?? "");
  const [format, setFormat] = useState("");
  const [note, setNote] = useState<string>();
  const [failure, setFailure] = useState<string>();
  const [actionStatus, setActionStatus] = useState<AsyncActionStatus>("idle");
  const lifetime = useRef(new AsyncActionLifetime());
  const formScope = JSON.stringify([contextKey, expression, name.trim(), table, format.trim()]);
  const formScopeRef = useRef(formScope);
  formScopeRef.current = formScope;
  const pending = actionStatus === "pending";
  const resetFeedback = () => {
    lifetime.current.invalidate();
    setActionStatus("idle"); setNote(undefined); setFailure(undefined);
  };
  // The formula in the editor can carry its own "Name =" line; the measure is created from the formula alone.
  const definition = splitMeasureDefinition(expression);
  const problem = name.trim() ? measureNameProblem(name, existing) : undefined;
  const ready = Boolean(name.trim() && definition.formula && table && !problem);
  useEffect(() => { if (!touched && suggestedName) setName(suggestedName); }, [suggestedName, touched]);
  // The model can change under an open form (a table removed by an undo): keep the choice valid.
  useEffect(() => { if (!tables.includes(table)) setTable(tables[0] ?? ""); }, [tables, table]);
  useEffect(() => {
    lifetime.current.invalidate();
    setActionStatus("idle"); setNote(undefined); setFailure(undefined);
  }, [expression, contextKey]);
  useEffect(() => () => lifetime.current.invalidate(), []);
  return <details className="dax-save-details"><summary><Save size={14} aria-hidden="true" />Save as a measure</summary>
    <form className="dax-lab-save" onSubmit={async event => {
      event.preventDefault();
      if (!ready || disabled || pending) return;
      const token = lifetime.current.begin(formScope);
      if (!token) return;
      let completed = false;
      setActionStatus("pending");
      setFailure(undefined);
      setNote(undefined);
      try {
        await onSave(name.trim(), definition.formula, { table, ...(format.trim() ? { format: format.trim() } : {}) });
        if (!lifetime.current.owns(token, formScopeRef.current)) return;
        completed = true;
        setActionStatus("success");
        setNote(queuedMeasureNote(name.trim()));
        setName("");
        setTouched(false);
      } catch (reason) {
        if (!lifetime.current.owns(token, formScopeRef.current)) return;
        completed = true;
        setActionStatus("error");
        setFailure(reason instanceof Error ? reason.message : "The measure could not be queued.");
      } finally {
        if (lifetime.current.release(token) && !completed) setActionStatus("idle");
      }
    }}>
      <input value={name} onChange={event => { resetFeedback(); setName(event.target.value); setTouched(true); }} placeholder="Measure name, e.g. Profit Margin" aria-label="New measure name" aria-invalid={Boolean(problem)} spellCheck={false} disabled={disabled || pending} />
      <select aria-label="Table for the new measure" value={table} onChange={event => { resetFeedback(); setTable(event.target.value); }} disabled={disabled || pending}>{tables.map(item => <option key={item} value={item}>{item}</option>)}</select>
      <input list="dax-save-formats" value={format} onChange={event => { resetFeedback(); setFormat(event.target.value); }} placeholder="Number format (optional)" aria-label="Number format of the new measure" spellCheck={false} disabled={disabled || pending} />
      <datalist id="dax-save-formats">{FORMATS.map(item => <option key={item} value={item} />)}</datalist>
      <AsyncActionButton type="submit" status={actionStatus} label="Queue as a measure" pendingLabel="Queueing…" successLabel="Queued" retryLabel="Retry queue" icon={Save} disabled={disabled || !ready} title="Adds it to Pending Power BI changes. Nothing changes in your report until you apply them." />
    </form>
    {tables.length === 0 && <p className="dax-lab-hint" role="status">This report has no table to add a measure to.</p>}
    {problem && <p className="dax-lab-hint" role="alert">{problem}</p>}
    {failure && <p className="dax-lab-hint" role="alert">{failure}</p>}
    {note && <p className="dax-lab-hint" role="status">{note}</p>}
    <p className="dax-lab-hint">Applying needs Power BI Desktop and takes about a minute. It can be undone from History.</p>
  </details>;
}
