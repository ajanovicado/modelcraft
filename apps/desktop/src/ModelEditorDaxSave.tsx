import { useEffect, useMemo, useState } from "react";
import { Columns3 } from "lucide-react";
import { pendingStore } from "./importedEditing";
import type { DaxResult } from "./types";
import { calculatedColumnOp, calculatedTableOp, checkQuery, isDefineQuery, modelNameProblem, NO_COVERAGE, queueModelChange, readModelFacts } from "./modelEditing";
import { CheckedQueue, type ModelContext } from "./ModelEditorShared";

/**
 * DAX Lab's "Save as a calculated column or table" for an imported report. Like "Save as a measure" it only queues the
 * change in "Pending Power BI changes"; the formula is checked on the imported data first (the DAX Lab check).
 */
export function SaveImportedCalculation({ workspaceId, tables, columnsOf, measureNames, expression, isQuery, disabled, onCheck, onDax }: {
  workspaceId: string; tables: string[]; columnsOf: (table: string) => string[]; measureNames: string[]; expression: string; isQuery: boolean;
  disabled: boolean; onCheck: (query: string) => Promise<DaxResult>; onDax?: (expression: string) => void;
}) {
  const [kind, setKind] = useState<"column" | "table">(isQuery ? "table" : "column");
  const [name, setName] = useState("");
  const [table, setTable] = useState(tables[0] ?? "");
  const [note, setNote] = useState<string>();
  useEffect(() => { if (!tables.includes(table)) setTable(tables[0] ?? ""); }, [tables, table]);
  useEffect(() => { setKind(isQuery ? "table" : "column"); }, [isQuery]);
  const taken = kind === "table" ? tables : [...columnsOf(table), ...measureNames];
  const problem = name ? modelNameProblem(name, taken, kind === "table" ? "table" : "column") : undefined;
  const define = kind === "table" && isDefineQuery(expression);
  const ready = Boolean(name.trim() && expression.trim() && !problem && !define && (kind === "table" || table));
  const op = ready ? kind === "table" ? calculatedTableOp({ name, expression }) : calculatedColumnOp({ table, name, expression }) : undefined;
  const ctx: ModelContext = useMemo(() => ({
    workspaceId, facts: readModelFacts({ status: "current", tables: [], columns: [] }), report: { pages: [], measures: [] }, coverage: NO_COVERAGE,
    locked: disabled, check: onCheck, onDax,
    queue: op => { queueModelChange(pendingStore, workspaceId, op); setNote(`Queued “${String(op.name)}”. Apply it from Pending Power BI changes.`); setName(""); },
  }), [workspaceId, disabled, onCheck, onDax]);
  return <details className="dax-save-details"><summary><Columns3 size={14} aria-hidden="true" />Save as a calculated column or table</summary>
    <div className="dax-lab-save me-dax-save">
      <fieldset className="me-kind"><legend className="sr-only">Save as</legend>
        <label><input type="radio" name="dax-save-kind" checked={kind === "column"} onChange={() => setKind("column")} />Calculated column</label>
        <label><input type="radio" name="dax-save-kind" checked={kind === "table"} onChange={() => setKind("table")} />Calculated table</label>
      </fieldset>
      <input value={name} onChange={event => { setName(event.target.value); setNote(undefined); }} placeholder={kind === "table" ? "Table name" : "Column name"} aria-label={kind === "table" ? "New table name" : "New column name"} aria-invalid={Boolean(problem)} spellCheck={false} />
      {kind === "column" && <select aria-label="Table for the new column" value={table} onChange={event => setTable(event.target.value)}>{tables.map(item => <option key={item} value={item}>{item}</option>)}</select>}
    </div>
    {problem && <p className="dax-lab-hint" role="alert">{problem}</p>}
    {define && <p className="dax-lab-hint" role="alert">A query with DEFINE is not a table formula. Keep only the table expression.</p>}
    <CheckedQueue ctx={ctx} query={kind === "table" ? checkQuery("table", expression) : checkQuery("column", expression, table, name.trim() || "Value")} expression={expression}
      column={kind === "column" ? name.trim() || "Value" : undefined} op={op} label={kind === "table" ? "Queue as a table" : "Queue as a column"} />
    {note && <p className="dax-lab-hint" role="status">{note}</p>}
    <p className="dax-lab-hint">{kind === "column" ? "A calculated column is worked out for each row of its table." : "A calculated table is stored in the model and refreshed with it."} Applying needs Power BI Desktop and can be undone from History.</p>
  </details>;
}
