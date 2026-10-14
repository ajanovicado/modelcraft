import { useMemo, useState } from "react";
import { CalendarDays, Columns3, EyeOff, Plus, Sigma, Table2 } from "lucide-react";
import {
  calculatedColumnOp, calculatedTableOp, checkQuery, columnDeleteGuard, columnDraft, columnNamesTaken, columnPropertiesOp, columnsOf, DATA_CATEGORY_OPTIONS,
  DATA_TYPE_OPTIONS, dataTypeLabel, deleteColumnOp, deleteTableOp, FORMAT_PRESETS, isDefineQuery, markDateTableOp, modelNameProblem, renameColumnOp,
  renameTableOp, SUMMARIZE_OPTIONS, tableDeleteGuard, tableDraft, tableKind, tableNamesTaken, tablePropertiesOp, visibleTables,
  type ColumnDraft, type FactColumn, type FactTable,
} from "./modelEditing";
import { CheckedQueue, DeleteConfirm, QueueButton, TRUNCATED_NOTE, type ModelContext } from "./ModelEditorShared";
import { StorageModeBadge, TableStorageNote } from "./ModelStorageSummary";
import { tableStorage } from "./modelStorage";

/** Tables and columns: properties, rename, delete with guard messages, date table, and new calculated columns and tables. */
export function ModelEditorTables({ ctx, selected, onSelect, rowCounts }: {
  ctx: ModelContext; selected: { table?: string; column?: string }; onSelect: (next: { table?: string; column?: string }) => void; rowCounts: Record<string, number>;
}) {
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState<"table" | "column">();
  const tables = useMemo(() => visibleTables(ctx.facts).filter(t => t.name.toLowerCase().includes(query.toLowerCase())), [ctx.facts, query]);
  const table = ctx.facts.tables.find(t => t.name === selected.table) ?? tables[0];
  return <div className="me-split">
    <nav className="me-list" aria-label="Tables">
      <label className="me-search"><span className="sr-only">Find a table</span><input placeholder="Find a table…" value={query} onChange={e => setQuery(e.target.value)} /></label>
      {tables.map(t => <button type="button" key={t.name} aria-current={!creating && table?.name === t.name ? "true" : undefined} onClick={() => { setCreating(undefined); onSelect({ table: t.name }); }}>
        {t.isDateTable ? <CalendarDays size={14} aria-hidden="true" /> : <Table2 size={14} aria-hidden="true" />}
        <span>{t.name}<small>{tableKind(ctx.facts, t)}{t.hidden ? " · hidden" : ""}</small><StorageModeBadge mode={tableStorage(ctx.facts.storage, t.name)?.mode}/></span>
        {t.hidden && <EyeOff size={12} aria-label="Hidden" />}
      </button>)}
      {!tables.length && <p className="ie-note">No matching tables</p>}
      <button type="button" className="secondary me-new" disabled={ctx.locked} aria-pressed={creating === "table"} onClick={() => setCreating("table")}><Plus size={13} aria-hidden="true" />New calculated table</button>
    </nav>
    <section className="me-detail">
      {creating === "table" ? <NewCalculatedTable ctx={ctx} onDone={name => { setCreating(undefined); if (name) onSelect({ table: name }); }} />
        : table ? <TableDetail key={table.name} ctx={ctx} table={table} rows={rowCounts[table.name]} column={selected.column} creatingColumn={creating === "column"}
          onColumn={column => { setCreating(undefined); onSelect({ table: table.name, column }); }} onNewColumn={() => setCreating("column")} onCloseNew={() => setCreating(undefined)} />
        : <p className="ie-note">This model has no tables Studio can show.</p>}
    </section>
  </div>;
}

function TableDetail({ ctx, table, rows, column, creatingColumn, onColumn, onNewColumn, onCloseNew }: {
  ctx: ModelContext; table: FactTable; rows?: number; column?: string; creatingColumn: boolean;
  onColumn: (name?: string) => void; onNewColumn: () => void; onCloseNew: () => void;
}) {
  const kind = tableKind(ctx.facts, table);
  const columns = columnsOf(ctx.facts, table.name);
  const current = columns.find(c => c.name === column);
  const [draft, setDraft] = useState(() => tableDraft(table));
  const [renaming, setRenaming] = useState(false);
  const [newName, setNewName] = useState(table.name);
  const [deleting, setDeleting] = useState(false);
  const [dateColumn, setDateColumn] = useState(table.dateColumn ?? columns.find(c => /date/i.test(c.dataType ?? ""))?.name ?? "");
  const op = tablePropertiesOp(table, draft);
  const nameProblem = renaming && newName.trim() !== table.name ? modelNameProblem(newName, tableNamesTaken(ctx.facts), "table", table.name) : undefined;
  const formulaChanged = table.calculated && draft.expression !== undefined && draft.expression.trim() !== (table.expression ?? "").trim();
  const dateColumns = columns.filter(c => /date/i.test(c.dataType ?? ""));
  return <div className="me-object">
    <header className="me-object-head">
      <h2><Table2 size={16} aria-hidden="true" />{table.name}</h2>
      <p className="ie-note">{kind}{table.isDateTable ? ` · date table on ${table.dateColumn ?? "its date column"}` : ""}</p>
    </header>
    <TableStorageNote table={tableStorage(ctx.facts.storage, table.name)} cachedRows={rows}/>
    <details className="me-section" open>
      <summary>Table properties</summary>
      <div className="me-grid">
        <label className="me-toggle"><input type="checkbox" checked={draft.hidden} disabled={ctx.locked} onChange={e => setDraft({ ...draft, hidden: e.target.checked })} />Hidden in report view</label>
        <label className="ie-field-block">Description<input value={draft.description} disabled={ctx.locked} onChange={e => setDraft({ ...draft, description: e.target.value })} /></label>
      </div>
      {table.calculated && (table.truncated ? <p className="ie-note">{TRUNCATED_NOTE}</p> : <label className="ie-field-block">Formula (DAX)
        <textarea value={draft.expression ?? ""} rows={5} spellCheck={false} disabled={ctx.locked} onChange={e => setDraft({ ...draft, expression: e.target.value })} /></label>)}
      <div className="me-actions">
        {formulaChanged
          ? <CheckedQueue ctx={ctx} query={checkQuery("table", draft.expression ?? "")} expression={draft.expression ?? ""} op={op} label="Queue this change" />
          : <QueueButton ctx={ctx} op={op} />}
        {op && <button type="button" className="secondary" onClick={() => setDraft(tableDraft(table))}>Reset</button>}
      </div>
    </details>
    <div className="me-actions">
      <button type="button" className="secondary" disabled={ctx.locked} aria-expanded={renaming} onClick={() => setRenaming(v => !v)}>Rename…</button>
      {kind !== "calculation group" && kind !== "field parameter" && <button type="button" className="secondary" disabled={ctx.locked} onClick={onNewColumn}><Plus size={13} aria-hidden="true" />New calculated column</button>}
      <button type="button" className="secondary danger" disabled={ctx.locked} aria-expanded={deleting} onClick={() => setDeleting(true)}>Delete…</button>
    </div>
    {renaming && <form className="me-inline-form" onSubmit={e => e.preventDefault()}>
      <label className="ie-field-block">New name<input value={newName} maxLength={128} aria-invalid={Boolean(nameProblem)} onChange={e => setNewName(e.target.value)} /></label>
      {nameProblem && <small role="alert" className="ie-error">{nameProblem}</small>}
      <p className="ie-note">Studio rewrites the visuals and formulas that use this table when the rename is applied.</p>
      <QueueButton ctx={ctx} op={!nameProblem && newName.trim() && newName.trim() !== table.name ? renameTableOp(table.name, newName) : undefined} label="Queue the rename" onQueued={() => setRenaming(false)} />
    </form>}
    {deleting && <DeleteConfirm ctx={ctx} what={`the ${kind} ${table.name}`} guard={tableDeleteGuard(ctx.facts, ctx.report, table.name)} op={deleteTableOp(table.name)} onDone={() => setDeleting(false)} />}
    {kind === "table" || kind === "calculated table" ? <details className="me-section">
      <summary>Date table</summary>
      {table.isDateTable ? <div className="me-actions"><p className="ie-note">Marked as a date table on {table.dateColumn ?? "its date column"}.</p>
        <QueueButton ctx={ctx} op={markDateTableOp(table.name, null)} label="Queue: unmark as date table" /></div>
        : dateColumns.length ? <div className="me-actions">
          <label className="ie-field-block">Date column<select value={dateColumn} disabled={ctx.locked} onChange={e => setDateColumn(e.target.value)}>{dateColumns.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}</select></label>
          <QueueButton ctx={ctx} op={dateColumn ? markDateTableOp(table.name, dateColumn) : undefined} label="Queue: mark as date table" /></div>
          : <p className="ie-note">This table has no date column, so it cannot be a date table.</p>}
    </details> : null}
    <h3 className="me-subhead"><Columns3 size={14} aria-hidden="true" />Columns ({columns.length})</h3>
    <div className="me-table-wrap"><table className="me-table">
      <thead><tr><th scope="col">Column</th><th scope="col">Type</th><th scope="col">Kind</th><th scope="col">Format</th><th scope="col">Sort by</th></tr></thead>
      <tbody>{columns.map(c => <tr key={c.name} className={c.name === current?.name ? "selected" : undefined}>
        <td><button type="button" className="me-link" aria-current={c.name === current?.name ? "true" : undefined} onClick={() => onColumn(c.name)}>{c.name}</button>{c.hidden && <EyeOff size={11} aria-label="Hidden" />}{c.error && <span className="me-badge problem" title={c.error}>Error</span>}</td>
        <td>{dataTypeLabel(c.dataType)}</td><td>{c.kind === "data" ? "From source" : c.kind === "calculated" ? "Calculated" : "Calculated table"}</td>
        <td>{c.formatString ?? ""}</td><td>{c.sortByColumn ?? ""}</td></tr>)}</tbody>
    </table>{!columns.length && <p className="ie-note">No columns</p>}</div>
    {creatingColumn && <NewCalculatedColumn ctx={ctx} table={table.name} onDone={name => { onCloseNew(); if (name) onColumn(name); }} />}
    {current && !creatingColumn && <ColumnEditor key={`${table.name}[${current.name}]`} ctx={ctx} column={current} columns={columns} onClose={() => onColumn(undefined)} />}
  </div>;
}

function ColumnEditor({ ctx, column, columns, onClose }: { ctx: ModelContext; column: FactColumn; columns: FactColumn[]; onClose: () => void }) {
  const [draft, setDraft] = useState<ColumnDraft>(() => columnDraft(column));
  const [renaming, setRenaming] = useState(false);
  const [newName, setNewName] = useState(column.name);
  const [deleting, setDeleting] = useState(false);
  const op = columnPropertiesOp(column, draft);
  const set = (patch: Partial<ColumnDraft>) => setDraft(current => ({ ...current, ...patch }));
  const calculated = column.kind === "calculated";
  const formulaChanged = calculated && draft.expression !== undefined && draft.expression.trim() !== (column.expression ?? "").trim();
  const taken = columnNamesTaken(ctx.facts, column.table);
  const nameProblem = renaming && newName.trim() !== column.name ? modelNameProblem(newName, taken, "column", column.name) : undefined;
  const unknownCategory = column.dataCategory && !draft.dataCategory;
  return <section className="me-editor" aria-label={`Column ${column.name}`}>
    <header className="me-object-head"><h3><Sigma size={14} aria-hidden="true" />{column.table}[{column.name}]</h3>
      <button type="button" className="secondary" onClick={onClose}>Close</button></header>
    {column.error && <p className="ie-note ie-error">Power BI reports a problem with this column: {column.error}</p>}
    <div className="me-grid">
      <label className="ie-field-block">Format<input list="me-formats" value={draft.formatString} placeholder="Auto" spellCheck={false} disabled={ctx.locked} onChange={e => set({ formatString: e.target.value })} /></label>
      <datalist id="me-formats">{FORMAT_PRESETS.map(f => <option key={f} value={f} />)}</datalist>
      <label className="ie-field-block">Sort by column<select value={draft.sortBy} disabled={ctx.locked} onChange={e => set({ sortBy: e.target.value })}>
        <option value="">(itself)</option>{columns.filter(c => c.name !== column.name).map(c => <option key={c.name} value={c.name}>{c.name}</option>)}</select></label>
      <label className="ie-field-block">Data category<select value={draft.dataCategory} disabled={ctx.locked} onChange={e => set({ dataCategory: e.target.value })}>
        {unknownCategory && <option value="">{column.dataCategory} (kept)</option>}
        {DATA_CATEGORY_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="ie-field-block">Summarize by<select value={draft.summarizeBy} disabled={ctx.locked} onChange={e => set({ summarizeBy: e.target.value })}>
        {SUMMARIZE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="ie-field-block">Display folder<input value={draft.folder} disabled={ctx.locked} onChange={e => set({ folder: e.target.value })} /></label>
      <label className="ie-field-block">Description<input value={draft.description} disabled={ctx.locked} onChange={e => set({ description: e.target.value })} /></label>
      {calculated && <label className="ie-field-block">Data type<select value={draft.dataType ?? ""} disabled={ctx.locked} onChange={e => set({ dataType: e.target.value })}>
        {!draft.dataType && <option value="">{dataTypeLabel(column.dataType)}</option>}
        {DATA_TYPE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>}
      {!calculated && <p className="ie-note">Type: {dataTypeLabel(column.dataType)} (set in Power Query)</p>}
      <label className="me-toggle"><input type="checkbox" checked={draft.hidden} disabled={ctx.locked} onChange={e => set({ hidden: e.target.checked })} />Hidden in report view</label>
    </div>
    {calculated && (column.truncated ? <p className="ie-note">{TRUNCATED_NOTE}</p> : <label className="ie-field-block">Formula (DAX)
      <textarea value={draft.expression ?? ""} rows={4} spellCheck={false} disabled={ctx.locked} aria-label={`DAX formula of ${column.name}`} onChange={e => set({ expression: e.target.value })} /></label>)}
    <div className="me-actions">
      {formulaChanged
        ? <CheckedQueue ctx={ctx} query={checkQuery("column", draft.expression ?? "", column.table, `${column.name} (new)`)} expression={draft.expression ?? ""} column={`${column.name} (new)`} op={op} label="Queue this change" />
        : <QueueButton ctx={ctx} op={op} />}
      {op && <button type="button" className="secondary" onClick={() => setDraft(columnDraft(column))}>Reset</button>}
      <button type="button" className="secondary" disabled={ctx.locked} aria-expanded={renaming} onClick={() => setRenaming(v => !v)}>Rename…</button>
      <button type="button" className="secondary danger" disabled={ctx.locked} aria-expanded={deleting} onClick={() => setDeleting(true)}>Delete…</button>
    </div>
    {renaming && <form className="me-inline-form" onSubmit={e => e.preventDefault()}>
      <label className="ie-field-block">New name<input value={newName} maxLength={128} aria-invalid={Boolean(nameProblem)} onChange={e => setNewName(e.target.value)} /></label>
      {nameProblem && <small role="alert" className="ie-error">{nameProblem}</small>}
      <p className="ie-note">Studio rewrites the visuals and formulas that use this column when the rename is applied.</p>
      <QueueButton ctx={ctx} op={!nameProblem && newName.trim() && newName.trim() !== column.name ? renameColumnOp(column.table, column.name, newName) : undefined} label="Queue the rename" onQueued={() => setRenaming(false)} />
    </form>}
    {deleting && <DeleteConfirm ctx={ctx} what={`the column ${column.table}[${column.name}]`} guard={columnDeleteGuard(ctx.facts, ctx.report, column.table, column.name)} op={deleteColumnOp(column.table, column.name)} onDone={() => setDeleting(false)} />}
  </section>;
}

function NewCalculatedColumn({ ctx, table, onDone }: { ctx: ModelContext; table: string; onDone: (name?: string) => void }) {
  const [name, setName] = useState("");
  const [expression, setExpression] = useState("");
  const [format, setFormat] = useState("");
  const [dataType, setDataType] = useState("");
  const problem = name ? modelNameProblem(name, columnNamesTaken(ctx.facts, table), "column") : undefined;
  const ready = !problem && name.trim() && expression.trim();
  const op = ready ? calculatedColumnOp({ table, name, expression, formatString: format, dataType: dataType || undefined }) : undefined;
  return <section className="me-editor" aria-label="New calculated column">
    <h3><Plus size={14} aria-hidden="true" />New calculated column in {table}</h3>
    <div className="me-grid">
      <label className="ie-field-block">Name<input value={name} maxLength={128} aria-invalid={Boolean(problem)} onChange={e => setName(e.target.value)} /></label>
      <label className="ie-field-block">Format<input list="me-formats" value={format} placeholder="Auto" spellCheck={false} onChange={e => setFormat(e.target.value)} /></label>
      <label className="ie-field-block">Data type<select value={dataType} onChange={e => setDataType(e.target.value)}><option value="">Automatic</option>
        {DATA_TYPE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    </div>
    {problem && <small role="alert" className="ie-error">{problem}</small>}
    <label className="ie-field-block">Formula (DAX), worked out for each row of {table}
      <textarea value={expression} rows={4} spellCheck={false} placeholder={`${table}[Amount] * 1.2`} onChange={e => setExpression(e.target.value)} /></label>
    <CheckedQueue ctx={ctx} query={checkQuery("column", expression, table, name.trim() || "Value")} expression={expression} column={name.trim() || "Value"} op={op} label="Queue new column" onQueued={() => onDone(name.trim())} />
    <button type="button" className="secondary" onClick={() => onDone()}>Cancel</button>
  </section>;
}

function NewCalculatedTable({ ctx, onDone }: { ctx: ModelContext; onDone: (name?: string) => void }) {
  const [name, setName] = useState("");
  const [expression, setExpression] = useState("");
  const [hidden, setHidden] = useState(false);
  const problem = name ? modelNameProblem(name, tableNamesTaken(ctx.facts), "table") : undefined;
  const define = isDefineQuery(expression);
  const ready = !problem && !define && name.trim() && expression.trim();
  const op = ready ? calculatedTableOp({ name, expression, hidden }) : undefined;
  return <section className="me-editor" aria-label="New calculated table">
    <h2><Plus size={16} aria-hidden="true" />New calculated table</h2>
    <label className="ie-field-block">Name<input value={name} maxLength={128} aria-invalid={Boolean(problem)} onChange={e => setName(e.target.value)} /></label>
    {problem && <small role="alert" className="ie-error">{problem}</small>}
    <label className="ie-field-block">Formula (DAX table expression)
      <textarea value={expression} rows={6} spellCheck={false} placeholder="DISTINCT('Date'[Year])" onChange={e => setExpression(e.target.value)} /></label>
    {define && <small role="alert" className="ie-error">A query with DEFINE is not a table formula. Keep only the table expression.</small>}
    <label className="me-toggle"><input type="checkbox" checked={hidden} onChange={e => setHidden(e.target.checked)} />Hidden in report view</label>
    <CheckedQueue ctx={ctx} query={checkQuery("table", expression)} expression={expression} op={op} label="Queue new table" onQueued={() => onDone(name.trim())} />
    <button type="button" className="secondary" onClick={() => onDone()}>Cancel</button>
  </section>;
}
