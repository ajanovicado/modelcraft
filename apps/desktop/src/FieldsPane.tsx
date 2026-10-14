import { useMemo, useState } from "react";
import { ArrowDownAZ, ArrowUpAZ, ChevronRight, Plus, Search, Sigma, Table2, X } from "lucide-react";
import { EditorDialog } from "./EditorDialog";
import {
  ADDABLE_TYPES, fieldRef, fieldWells, visualTypeLabel, type FieldChoice, type FormatCatalog, type RenderColumn, type VisualInfo,
} from "./importedFormat";
import type { Operation } from "./importedEditing";
import { findFreeSpot, type Rect, type Size } from "./importedGeometry";

const AGGREGATIONS = ["Sum", "Average", "Count", "Min", "Max", "CountNonNull"] as const;
const AGGREGATION_LABELS: Record<string, string> = { Sum: "Sum", Average: "Average", Count: "Count", Min: "Minimum", Max: "Maximum", CountNonNull: "Count (not blank)" };

type Picked = { table: string; name: string; aggregation?: string };

/** Search the model's tables, columns and measures and pick one or several. */
function FieldPickerDialog({ title, choices, multiple, measureRole, confirmLabel, onCancel, onPick }: {
  title: string; choices: FieldChoice[]; multiple: boolean; measureRole?: boolean; confirmLabel: string;
  onCancel: () => void; onPick: (fields: Picked[]) => void;
}) {
  const [query, setQuery] = useState("");
  const [chosen, setChosen] = useState<Map<string, Picked>>(new Map());
  const [aggregation, setAggregation] = useState("Sum");
  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const found = new Map<string, FieldChoice[]>();
    for (const choice of choices) {
      if (needle && !`${choice.table} ${choice.name}`.toLowerCase().includes(needle)) continue;
      found.set(choice.table, [...(found.get(choice.table) ?? []), choice]);
    }
    return [...found.entries()];
  }, [choices, query]);
  const key = (choice: FieldChoice) => `${choice.table}\u0000${choice.name}`;
  const toggle = (choice: FieldChoice) => setChosen(current => {
    const next = new Map(multiple ? current : []);
    if (next.has(key(choice)) && multiple) next.delete(key(choice));
    else next.set(key(choice), fieldRef(choice, choice.kind === "column" && choice.numeric && measureRole ? aggregation : undefined));
    return next;
  });
  return <EditorDialog title={title} wide onCancel={onCancel}
    footer={<><button type="button" className="secondary" onClick={onCancel}>Cancel</button>
      <button type="button" className="primary yellow" disabled={!chosen.size} onClick={() => onPick([...chosen.values()].map(item => item.aggregation ? { ...item, aggregation } : item))}>{confirmLabel}</button></>}>
    <label className="ie-search"><Search size={14} aria-hidden="true" /><input type="search" data-autofocus aria-label="Search fields" placeholder="Search fields" value={query} onChange={event => setQuery(event.target.value)} /></label>
    {measureRole && <label className="ie-inline">Numbers are summarized by
      <select value={aggregation} onChange={event => setAggregation(event.target.value)}>{AGGREGATIONS.map(item => <option key={item} value={item}>{AGGREGATION_LABELS[item]}</option>)}</select></label>}
    <div className="ie-picker-list" role="group" aria-label="Fields">
      {groups.map(([table, fields]) => <div key={table} className="ie-picker-table"><strong><Table2 size={13} aria-hidden="true" />{table}</strong>
        {fields.slice(0, 200).map(choice => <button type="button" key={key(choice)} aria-pressed={chosen.has(key(choice))} className={chosen.has(key(choice)) ? "selected" : undefined} onClick={() => toggle(choice)}>
          {choice.kind === "measure" ? <Sigma size={13} aria-hidden="true" /> : <span className="ie-field-dot" aria-hidden="true" />}{choice.name}</button>)}</div>)}
      {!groups.length && <p className="ie-note">No fields match.</p>}
    </div>
  </EditorDialog>;
}

/** Add a visual: a gallery of the types Studio can add, then the fields to put in it. */
export function AddVisualDialog({ choices, pageId, pageSize, existing, onCancel, onAdd }: {
  choices: FieldChoice[]; pageId: string; pageSize: Size; existing: Rect[];
  onCancel: () => void; onAdd: (operation: Operation, label: string) => void;
}) {
  const [type, setType] = useState<string>();
  if (!type) {
    return <EditorDialog title="Add a visual" wide onCancel={onCancel} footer={<button type="button" className="secondary" data-autofocus onClick={onCancel}>Cancel</button>}>
      <p className="ie-dialog-text">Choose a visual type, then the fields to show in it.</p>
      <div className="ie-gallery" role="group" aria-label="Visual types">{ADDABLE_TYPES.map(item => <button type="button" key={item.type} onClick={() => setType(item.type)}>
        <strong>{item.label}</strong>{!item.drawn && <small>Previewed in Power BI</small>}</button>)}</div>
    </EditorDialog>;
  }
  const size = { width: 400, height: 250 };
  return <FieldPickerDialog title={`Add a ${visualTypeLabel(type).toLowerCase()}`} choices={choices} multiple measureRole confirmLabel="Add to page" onCancel={onCancel}
    onPick={fields => onAdd({ op: "add_visual", page: pageId, visual_type: type, fields, ...findFreeSpot(existing, size, pageSize), ...size }, "Add visual")} />;
}

type Binding = { kind: string; table: string; name: string; role: string };

/**
 * The selected visual's fields by role, with add, replace and remove, the visual type and the sort.
 * Field changes re-run the visual's query, so each is one edit like any other.
 */
export function FieldsPane({ info, catalog, choices, bindings, measures, disabled, onEdit, onDax, onAsk }: {
  info: VisualInfo | undefined; catalog?: FormatCatalog; choices: FieldChoice[]; bindings?: Binding[];
  measures: { TableName: string; Name: string; Expression: string }[]; disabled: boolean;
  onEdit: (operation: Operation, label: string) => void; onDax?: (expression: string) => void; onAsk?: () => void;
}) {
  const [picking, setPicking] = useState<{ role: string; label: string; kind: string; replace: boolean }>();
  const [sortField, setSortField] = useState("");
  const [direction, setDirection] = useState<"ascending" | "descending">("descending");
  const wells = useMemo(() => fieldWells(info?.visualType ?? "", catalog, info?.columns), [catalog, info?.visualType, info?.columns]);
  if (!info) return <p className="ie-empty">Select a visual to see its fields, or add a new visual from the toolbar.</p>;
  const columns = (info.columns ?? []).filter(c => !c.hidden && c.role !== "Formatting");
  const applySort = (ref: string, direction: "ascending" | "descending") => {
    const column = columns.find(c => (c.query_ref ?? c.name) === ref);
    if (column) onEdit({ op: "set_sort", visual: info.id, field: fieldRef({ table: column.table ?? "", name: column.name }), direction }, "Sort");
  };
  const removeField = (role: string, field: RenderColumn) => onEdit({ op: "set_fields", visual: info.id, role, fields: [fieldRef({ table: field.table ?? "", name: field.name })], mode: "remove" }, "Remove field");
  return <div className="ie-fields">
    {onAsk && <button type="button" className="primary yellow full" onClick={onAsk}>Ask AI about this visual</button>}
    <label className="ie-inline">Visual type
      <select value={ADDABLE_TYPES.some(t => t.type === info.visualType) ? info.visualType : ""} disabled={disabled || !ADDABLE_TYPES.some(t => t.type === info.visualType)}
        onChange={event => { if (event.target.value && event.target.value !== info.visualType) onEdit({ op: "change_visual_type", visual: info.id, visual_type: event.target.value }, "Change visual type"); }}>
        {!ADDABLE_TYPES.some(t => t.type === info.visualType) && <option value="">{visualTypeLabel(info.visualType)}</option>}
        {ADDABLE_TYPES.map(t => <option key={t.type} value={t.type}>{t.label}{t.drawn ? "" : " (Power BI only preview)"}</option>)}</select>
    </label>
    {wells.map(({ role, fields }) => <section className="ie-well" key={role.role} aria-label={role.label}>
      <header><strong>{role.label}</strong><button type="button" className="secondary" disabled={disabled || (role.max !== undefined && fields.length >= role.max && role.max > 1)} aria-label={`${fields.length === 1 && role.max === 1 ? "Replace" : "Add"} a field in ${role.label}`} onClick={() => setPicking({ role: role.role, label: role.label, kind: role.kind, replace: role.max === 1 && fields.length === 1 })}>{role.max === 1 && fields.length === 1 ? "Replace" : <><Plus size={13} />Add</>}</button></header>
      {fields.map(field => <div className="ie-field" key={field.query_ref ?? field.name}>
        <span>{field.kind === "measure" ? <Sigma size={13} aria-hidden="true" /> : <Table2 size={13} aria-hidden="true" />}{field.label || field.name}<small>{field.table}</small></span>
        <button type="button" className="icon-button" disabled={disabled} aria-label={`Remove ${field.label || field.name} from ${role.label}`} onClick={() => removeField(role.role, field)}><X size={13} /></button>
      </div>)}
      {!fields.length && <small className="ie-note">Empty</small>}
    </section>)}
    {columns.length > 0 && <fieldset className="ie-sort"><legend>Sort by</legend>
      <label className="ie-inline">Field<select value={sortField} disabled={disabled} onChange={event => { setSortField(event.target.value); applySort(event.target.value, direction); }}>
        <option value="">Choose a field…</option>{columns.map(c => <option key={c.query_ref ?? c.name} value={c.query_ref ?? c.name}>{c.label || c.name}</option>)}</select></label>
      <div className="ie-sort-direction" role="group" aria-label="Sort direction">
        {(["ascending", "descending"] as const).map(item => <button type="button" key={item} className="secondary" aria-pressed={direction === item} disabled={disabled}
          onClick={() => { setDirection(item); applySort(sortField || (columns.find(c => c.kind === "measure") ?? columns[0]).query_ref || (columns.find(c => c.kind === "measure") ?? columns[0]).name, item); }}>
          {item === "ascending" ? <ArrowDownAZ size={14} /> : <ArrowUpAZ size={14} />}{item === "ascending" ? "Low to high" : "High to low"}</button>)}
      </div>
    </fieldset>}
    {bindings && bindings.some(b => b.kind === "measure") && <div className="ie-bindings"><strong>Measures in this visual</strong>{bindings.filter(b => b.kind === "measure").map((b, i) => {
      const measure = measures.find(m => m.Name === b.name && m.TableName === b.table);
      return <div key={i} className="imported-binding"><span><Sigma size={14} />{b.name}</span><small>{b.table} · {b.role || b.kind}</small>{measure && <><pre>{measure.Expression}</pre>{onDax && <button type="button" className="secondary" onClick={() => onDax(measure.Expression)}>Explore in DAX Lab<ChevronRight size={13} /></button>}</>}</div>;
    })}</div>}
    {picking && <FieldPickerDialog title={`${picking.replace ? "Replace the field in" : "Add a field to"} ${picking.label}`} choices={choices} multiple={!picking.replace && picking.kind !== "Grouping"} measureRole={picking.kind === "Measure" || picking.kind === "GroupingOrMeasure"} confirmLabel={picking.replace ? "Replace" : "Add"} onCancel={() => setPicking(undefined)}
      onPick={fields => { const target = picking; setPicking(undefined); onEdit({ op: "set_fields", visual: info.id, role: target.role, fields, mode: target.replace ? "replace" : "add" }, target.replace ? "Replace field" : "Add field"); }} />}
  </div>;
}
