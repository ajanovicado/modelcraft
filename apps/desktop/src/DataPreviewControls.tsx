import { useState, type ReactNode } from "react";
import { ArrowDownWideNarrow, Filter, X } from "lucide-react";

export type RowFilter = { column_id: string; operator: string; value: string };
export type RowSort = { column_id: string; direction: "asc" | "desc" };
type Field = { id: string; name: string; data_type: string };
const labels: Record<string, string> = { equals: "equals", not_equals: "does not equal", contains: "contains", gt: "is greater than", gte: "is at least", lt: "is less than", lte: "is at most", is_blank: "is blank", is_not_blank: "is not blank" };

export function DataPreviewControls({ columnControls, fields, filters, sort, onFilters, onSort }: {
  columnControls: ReactNode; fields: Field[]; filters: RowFilter[]; sort?: RowSort;
  onFilters: (filters: RowFilter[]) => void; onSort: (sort?: RowSort) => void;
}) {
  const [open, setOpen] = useState(false);
  const [column, setColumn] = useState(fields[0]?.id || "");
  const [operator, setOperator] = useState("equals");
  const [value, setValue] = useState("");
  const field = fields.find(f => f.id === column);
  const blank = operator === "is_blank" || operator === "is_not_blank";
  const operators = ["equals", "not_equals", ...(field?.data_type === "string" ? ["contains"] : field?.data_type === "boolean" ? [] : ["gt", "gte", "lt", "lte"]), "is_blank", "is_not_blank"];
  return <div className="data-preview-controls">
    <div className="data-preview-toolbar data-column-controls">
      {columnControls}
      <button className="secondary" aria-expanded={open} disabled={!fields.length || filters.length >= 4} title={filters.length >= 4 ? "Up to four filters; remove one to add another" : "Filter rows across the full snapshot"} onClick={() => setOpen(!open)}><Filter size={14}/>Filter rows{filters.length ? ` (${filters.length})` : ""}</button>
      {!filters.length && !sort && <span className="data-query-hint" title="Click a column heading to sort · Full snapshot">Click a column heading to sort · Full snapshot</span>}
    </div>
    {(!!filters.length || sort) && <div className="data-filter-summary">
      {filters.map((filter, i) => <button key={i} className="data-query-chip" title="Remove this filter" onClick={() => onFilters(filters.filter((_, index) => i !== index))}><span>{fields.find(f => f.id === filter.column_id)?.name} {labels[filter.operator]}{filter.operator.startsWith("is_") ? "" : ` ${filter.value}`}</span><X size={12}/></button>)}
      {sort && <button className="data-query-chip" title="Clear sorting" onClick={() => onSort()}><ArrowDownWideNarrow size={14}/><span>{fields.find(f => f.id === sort.column_id)?.name} · {sort.direction === "asc" ? "Ascending" : "Descending"}</span><X size={12}/></button>}
      {!!filters.length && <button className="data-clear-filters" onClick={() => onFilters([])}>Clear filters</button>}
    </div>}
    {open && <form className="data-filter-form" onSubmit={event => {
      event.preventDefault();
      if (!field || filters.length >= 4) return;
      onFilters([...filters, { column_id: column, operator, value: blank ? "" : value }]);
      setOpen(false); setValue("");
    }}>
      <label>Column<select value={column} onChange={event => { setColumn(event.target.value); setOperator("equals"); setValue(""); }}>{fields.map(f => <option value={f.id} key={f.id}>{f.name}</option>)}</select></label>
      <label>Condition<select value={operator} onChange={event => { setOperator(event.target.value); setValue(""); }}>{operators.map(op => <option value={op} key={op}>{labels[op]}</option>)}</select></label>
      {!blank && <label>Value{field?.data_type === "boolean" ? <select aria-label="Filter value" value={value} required onChange={event => setValue(event.target.value)}><option value="" disabled>Select value</option><option value="true">True</option><option value="false">False</option></select> : <input aria-label="Filter value" type={field?.data_type === "date" ? "date" : field?.data_type === "datetime" ? "datetime-local" : ["number", "integer"].includes(field?.data_type || "") ? "number" : "text"} step="any" maxLength={500} required value={value} onChange={event => setValue(event.target.value)} placeholder={field?.data_type === "string" ? "Enter text…" : undefined}/>}</label>}
      <button className="primary yellow" type="submit">Apply filter</button><button className="secondary" type="button" onClick={() => setOpen(false)}>Cancel</button>
      <small>Filters combine with AND. Only this preview changes.</small>
    </form>}
  </div>;
}
