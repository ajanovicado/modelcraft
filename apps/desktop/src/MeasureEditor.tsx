import { useEffect, useMemo, useState } from "react";
import { ChevronRight, Pencil, Plus, Trash2 } from "lucide-react";
import { createMeasureOp, measureNameProblem, measureUpdateOp, pendingStore, type MeasureInfo } from "./importedEditing";

const FORMATS = ["#,0", "#,0.00", "0.0%", "0%", "$#,0.00", "0.00"];

/**
 * View, change, rename and delete one measure. Nothing here touches the report: each action queues
 * a change in "Pending Power BI changes", which the user applies in one Desktop cycle. When a
 * measure is still used somewhere the server refuses the delete at that point and says where.
 */
export function MeasureEditor({ workspaceId, measure, all, disabled, onDax }: {
  workspaceId: string; measure: MeasureInfo; all: MeasureInfo[]; disabled: boolean; onDax?: (expression: string) => void;
}) {
  const [expression, setExpression] = useState(measure.Expression);
  const [format, setFormat] = useState(measure.FormatString ?? "");
  const [description, setDescription] = useState(measure.Description ?? "");
  const [renaming, setRenaming] = useState(false);
  const [newName, setNewName] = useState(measure.Name);
  const [notice, setNotice] = useState<string>();
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  useEffect(() => {
    setExpression(measure.Expression); setFormat(measure.FormatString ?? ""); setDescription(measure.Description ?? "");
    setRenaming(false); setConfirmingDelete(false); setNewName(measure.Name); setNotice(undefined);
  }, [measure]);
  const names = useMemo(() => all.map(item => item.Name), [all]);
  const update = measureUpdateOp(measure, { expression, format, description });
  const nameProblem = renaming && newName.trim() !== measure.Name ? measureNameProblem(newName, names, measure.Name) : undefined;
  return <div className="ie-measure">
    <h2>{measure.Name}</h2>
    <p className="ie-note">{measure.TableName}</p>
    <label className="ie-field-block">Formula
      <textarea value={expression} rows={8} spellCheck={false} disabled={disabled} aria-label={`DAX formula of ${measure.Name}`} onChange={event => { setExpression(event.target.value); setNotice(undefined); }} /></label>
    <div className="ie-measure-grid">
      <label className="ie-field-block">Number format
        <input list="ie-measure-formats" value={format} disabled={disabled} placeholder="Auto" spellCheck={false} onChange={event => setFormat(event.target.value)} />
        <datalist id="ie-measure-formats">{FORMATS.map(item => <option key={item} value={item} />)}</datalist></label>
      <label className="ie-field-block">Description
        <input value={description} disabled={disabled} onChange={event => setDescription(event.target.value)} /></label>
    </div>
    <div className="ie-measure-actions">
      <button type="button" className="primary yellow" disabled={disabled || !update} onClick={() => { if (update) { pendingStore.add(workspaceId, update); setNotice("Queued. Apply it from Pending Power BI changes."); } }}><Pencil size={14} />Queue this change</button>
      {onDax && <button type="button" className="secondary" disabled={!expression.trim()} onClick={() => onDax(expression)}>Try in DAX Lab<ChevronRight size={13} /></button>}
      <button type="button" className="secondary" disabled={disabled} aria-expanded={renaming} onClick={() => setRenaming(value => !value)}>Rename…</button>
      <button type="button" className="secondary danger" disabled={disabled} aria-expanded={confirmingDelete} onClick={() => setConfirmingDelete(true)}><Trash2 size={14} />Delete…</button>
    </div>
    {confirmingDelete && <div className="ie-rename-form" role="group" aria-label={`Delete ${measure.Name}`}>
      <p className="ie-note">Delete the measure {measure.Name}? Nothing changes until you apply your pending changes, and Power BI refuses if a visual or another measure still uses it.</p>
      <button type="button" className="secondary" data-autofocus onClick={() => setConfirmingDelete(false)}>Keep it</button>
      <button type="button" className="secondary danger" onClick={() => { pendingStore.add(workspaceId, { op: "delete_measure", name: measure.Name }); setConfirmingDelete(false); setNotice("Queued the delete. Power BI checks that nothing uses this measure when you apply it."); }}>Queue the delete</button>
    </div>}
    {renaming && <form className="ie-rename-form" onSubmit={event => {
      event.preventDefault();
      if (nameProblem || !newName.trim() || newName.trim() === measure.Name) return;
      pendingStore.add(workspaceId, { op: "rename_measure", name: measure.Name, new_name: newName.trim() });
      setRenaming(false); setNotice("Queued the rename. Every visual that uses it is updated when you apply it.");
    }}>
      <label className="ie-field-block">New name<input value={newName} maxLength={200} aria-invalid={Boolean(nameProblem)} onChange={event => setNewName(event.target.value)} /></label>
      {nameProblem && <small role="alert" className="ie-error">{nameProblem}</small>}
      <button type="submit" className="secondary" disabled={Boolean(nameProblem) || !newName.trim() || newName.trim() === measure.Name}>Queue rename</button>
    </form>}
    {notice && <p className="ie-note" role="status">{notice}</p>}
    <p className="ie-note">Original DAX is preserved until you apply a change. Changes run in Power BI Desktop, are checked there, and can be undone from History.</p>
  </div>;
}

/** A new measure in a table of the model. */
export function NewMeasureForm({ workspaceId, tables, all, disabled, onDone, onDax }: {
  workspaceId: string; tables: string[]; all: MeasureInfo[]; disabled: boolean; onDone: () => void; onDax?: (expression: string) => void;
}) {
  const [name, setName] = useState("");
  const [table, setTable] = useState(tables[0] ?? "");
  const [expression, setExpression] = useState("");
  const [format, setFormat] = useState("");
  const problem = name ? measureNameProblem(name, all.map(item => item.Name)) : undefined;
  const ready = !problem && name.trim() && expression.trim() && table;
  return <form className="ie-measure" onSubmit={event => {
    event.preventDefault();
    if (!ready) return;
    pendingStore.add(workspaceId, createMeasureOp({ table, name, expression, format }));
    onDone();
  }}>
    <h2><Plus size={16} aria-hidden="true" />New measure</h2>
    <label className="ie-field-block">Name<input value={name} maxLength={200} aria-invalid={Boolean(problem)} onChange={event => setName(event.target.value)} /></label>
    {problem && <small role="alert" className="ie-error">{problem}</small>}
    <label className="ie-field-block">Table<select value={table} onChange={event => setTable(event.target.value)}>{tables.map(item => <option key={item} value={item}>{item}</option>)}</select></label>
    <label className="ie-field-block">Formula<textarea value={expression} rows={6} spellCheck={false} placeholder="SUM(Sales[Amount])" onChange={event => setExpression(event.target.value)} /></label>
    <label className="ie-field-block">Number format<input list="ie-new-measure-formats" value={format} placeholder="Auto" spellCheck={false} onChange={event => setFormat(event.target.value)} />
      <datalist id="ie-new-measure-formats">{FORMATS.map(item => <option key={item} value={item} />)}</datalist></label>
    <div className="ie-measure-actions">
      <button type="submit" className="primary yellow" disabled={disabled || !ready}>Queue new measure</button>
      {onDax && <button type="button" className="secondary" disabled={!expression.trim()} onClick={() => onDax(expression)}>Try in DAX Lab<ChevronRight size={13} /></button>}
      <button type="button" className="secondary" onClick={onDone}>Cancel</button>
    </div>
  </form>;
}
