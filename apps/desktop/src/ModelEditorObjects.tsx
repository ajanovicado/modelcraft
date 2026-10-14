import { useMemo, useState } from "react";
import { Layers, ListTree, Plus, Shield, SlidersHorizontal } from "lucide-react";
import {
  calculationGroupOp, calculationItemDraft, calculationItemOp, columnsOf, createCalculationGroupOp, createHierarchyOp, deleteCalculationItemOp,
  deleteHierarchyOp, deleteRoleOp, deleteTableOp, fieldParameterOp, modelNameProblem, moveItem, ordinalProblem, parameterFields, roleOp,
  tableNamesTaken, updateHierarchyOp, visibleTables,
  type CalculationItemDraft, type FactCalculationGroup, type FactCalculationItem, type FactFieldParameter, type FactHierarchy, type FactRole,
  type LevelDraft, type ParameterFieldDraft, type RoleFilterDraft,
} from "./modelEditing";
import { DeleteConfirm, OrderedList, QueueButton, RemoveButton, TRUNCATED_NOTE, type ModelContext } from "./ModelEditorShared";

const hierarchyKey = (h: Pick<FactHierarchy, "table" | "name">) => `${h.table}|${h.name}`;

// --- hierarchies ------------------------------------------------------------------------------------------------------

export function ModelEditorHierarchies({ ctx }: { ctx: ModelContext }) {
  const [selected, setSelected] = useState<string>();
  const [creating, setCreating] = useState(false);
  const hierarchies = ctx.facts.hierarchies;
  const current = hierarchies.find(h => hierarchyKey(h) === selected);
  return <div className="me-split">
    <nav className="me-list" aria-label="Hierarchies">
      {hierarchies.map(h => <button type="button" key={hierarchyKey(h)} aria-current={!creating && hierarchyKey(h) === selected ? "true" : undefined} onClick={() => { setCreating(false); setSelected(hierarchyKey(h)); }}>
        <ListTree size={14} aria-hidden="true" /><span>{h.name}<small>{h.table} · {h.levels.length} levels</small></span></button>)}
      {!hierarchies.length && <p className="ie-note">This model has no hierarchies.</p>}
      <button type="button" className="secondary me-new" disabled={ctx.locked} aria-pressed={creating} onClick={() => { setCreating(true); setSelected(undefined); }}><Plus size={13} aria-hidden="true" />New hierarchy</button>
    </nav>
    <section className="me-detail">
      {creating ? <HierarchyEditor key="new" ctx={ctx} onDone={() => setCreating(false)} />
        : current ? <HierarchyEditor key={selected} ctx={ctx} original={current} onDone={() => setSelected(undefined)} />
        : <p className="ie-note">Pick a hierarchy to change its levels, or make a new one. Drag the levels (or use the arrows) to order them.</p>}
    </section>
  </div>;
}

function HierarchyEditor({ ctx, original, onDone }: { ctx: ModelContext; original?: FactHierarchy; onDone: () => void }) {
  const tables = visibleTables(ctx.facts);
  const [table, setTable] = useState(original?.table ?? tables[0]?.name ?? "");
  const [name, setName] = useState(original?.name ?? "");
  const [levels, setLevels] = useState<LevelDraft[]>(() => original?.levels.map(l => ({ column: l.column, name: l.name })) ?? []);
  const [adding, setAdding] = useState("");
  const [deleting, setDeleting] = useState(false);
  const columns = columnsOf(ctx.facts, table);
  const free = columns.filter(c => !levels.some(l => l.column === c.name));
  const taken = ctx.facts.hierarchies.filter(h => h.table === table).map(h => h.name);
  const nameProblem = name && name.trim() !== original?.name ? modelNameProblem(name, taken, "hierarchy", original?.name) : undefined;
  const op = !levels.length || nameProblem || !name.trim() ? undefined : original ? updateHierarchyOp(original, { name, levels }) : createHierarchyOp(table, name, levels);
  const used = original ? ctx.report.pages.flatMap(p => p.visuals.filter(v => v.bindings.some(b => b.kind === "hierarchy" && b.table === original.table && b.name.startsWith(original.name))).map(v => v.title || "Untitled")) : [];
  return <section className="me-editor" aria-label={original ? `Hierarchy ${original.name}` : "New hierarchy"}>
    <h3><ListTree size={14} aria-hidden="true" />{original ? `${original.table} · ${original.name}` : "New hierarchy"}</h3>
    <div className="me-grid">
      {!original && <label className="ie-field-block">Table<select value={table} onChange={e => { setTable(e.target.value); setLevels([]); }}>{tables.map(t => <option key={t.name} value={t.name}>{t.name}</option>)}</select></label>}
      <label className="ie-field-block">Name<input value={name} maxLength={128} aria-invalid={Boolean(nameProblem)} disabled={ctx.locked} onChange={e => setName(e.target.value)} /></label>
    </div>
    {nameProblem && <small role="alert" className="ie-error">{nameProblem}</small>}
    <OrderedList items={levels} keyOf={l => l.column} label="Levels, top first" disabled={ctx.locked} onMove={(from, to) => setLevels(list => moveItem(list, from, to))}
      render={(level, index) => <div className="me-level">
        <span className="me-level-column">{index + 1}. {level.column}</span>
        <label><span className="sr-only">Level name for {level.column}</span><input value={level.name} placeholder={level.column} disabled={ctx.locked} onChange={e => setLevels(list => list.map((l, i) => i === index ? { ...l, name: e.target.value } : l))} /></label>
        <RemoveButton label={`Remove the level ${level.column}`} disabled={ctx.locked} onClick={() => setLevels(list => list.filter((_, i) => i !== index))} />
      </div>} />
    {!levels.length && <p className="ie-note">Add at least one level.</p>}
    {free.length > 0 && <div className="me-actions">
      <label className="ie-field-block">Add a level<select value={adding} disabled={ctx.locked} onChange={e => setAdding(e.target.value)}><option value="">Choose a column…</option>{free.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}</select></label>
      <button type="button" className="secondary" disabled={!adding || ctx.locked} onClick={() => { setLevels(list => [...list, { column: adding, name: adding }]); setAdding(""); }}>Add level</button>
    </div>}
    {used.length > 0 && <p className="ie-note">Visuals use this hierarchy ({used.slice(0, 3).join(", ")}). Renaming it is refused while they do; changing its levels is fine.</p>}
    <div className="me-actions">
      <QueueButton ctx={ctx} op={op} label={original ? "Queue this change" : "Queue new hierarchy"} onQueued={onDone} />
      {original && <button type="button" className="secondary danger" disabled={ctx.locked} aria-expanded={deleting} onClick={() => setDeleting(true)}>Delete…</button>}
      <button type="button" className="secondary" onClick={onDone}>{original ? "Close" : "Cancel"}</button>
    </div>
    {deleting && original && <DeleteConfirm ctx={ctx} what={`the hierarchy ${original.name}`} op={deleteHierarchyOp(original)} onDone={() => { setDeleting(false); onDone(); }}
      guard={{ blocking: used.length ? [`Visuals use it: ${used.slice(0, 3).join(", ")}.`] : [], warnings: [] }} />}
  </section>;
}

// --- field parameters ----------------------------------------------------------------------------------------------------

export function ModelEditorFieldParameters({ ctx }: { ctx: ModelContext }) {
  const [selected, setSelected] = useState<string>();
  const [creating, setCreating] = useState(false);
  const current = ctx.facts.fieldParameters.find(p => p.table === selected);
  return <div className="me-split">
    <nav className="me-list" aria-label="Field parameters">
      {ctx.facts.fieldParameters.map(p => <button type="button" key={p.table} aria-current={!creating && p.table === selected ? "true" : undefined} onClick={() => { setCreating(false); setSelected(p.table); }}>
        <SlidersHorizontal size={14} aria-hidden="true" /><span>{p.table}<small>{p.rows ? `${p.rows.length} fields` : "fields not read"}</small></span></button>)}
      {!ctx.facts.fieldParameters.length && <p className="ie-note">This model has no field parameters.</p>}
      <button type="button" className="secondary me-new" disabled={ctx.locked} aria-pressed={creating} onClick={() => { setCreating(true); setSelected(undefined); }}><Plus size={13} aria-hidden="true" />New field parameter</button>
    </nav>
    <section className="me-detail">
      {creating ? <FieldParameterEditor key="new" ctx={ctx} onDone={() => setCreating(false)} />
        : current ? <FieldParameterEditor key={current.table} ctx={ctx} original={current} onDone={() => setSelected(undefined)} />
        : <p className="ie-note">A field parameter lets report readers pick which measure or column a visual shows. Studio's canvas draws visuals that use one in Power BI only.</p>}
    </section>
  </div>;
}

function FieldParameterEditor({ ctx, original, onDone }: { ctx: ModelContext; original?: FactFieldParameter; onDone: () => void }) {
  const [name, setName] = useState(original?.table ?? "");
  const [fields, setFields] = useState<ParameterFieldDraft[]>(() => original ? parameterFields(ctx.facts, original) : []);
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState(false);
  const nameProblem = !original && name ? modelNameProblem(name, tableNamesTaken(ctx.facts), "field parameter") : undefined;
  const candidates = useMemo(() => {
    const words = search.trim().toLowerCase();
    const all: ParameterFieldDraft[] = [
      ...ctx.facts.measures.filter(m => !m.hidden).map(m => ({ table: m.table, name: m.name, label: m.name, kind: "measure" as const })),
      ...visibleTables(ctx.facts).flatMap(t => columnsOf(ctx.facts, t.name).filter(c => !c.hidden).map(c => ({ table: c.table, name: c.name, label: c.name, kind: "column" as const }))),
    ];
    return all.filter(f => !fields.some(p => p.table === f.table && p.name === f.name) && (!words || `${f.table} ${f.name}`.toLowerCase().includes(words))).slice(0, 40);
  }, [ctx.facts, fields, search]);
  const op = !fields.length || nameProblem || !name.trim() ? undefined : fieldParameterOp(name, fields, Boolean(original));
  return <section className="me-editor" aria-label={original ? `Field parameter ${original.table}` : "New field parameter"}>
    <h3><SlidersHorizontal size={14} aria-hidden="true" />{original ? original.table : "New field parameter"}</h3>
    {!original && <label className="ie-field-block">Name<input value={name} maxLength={128} aria-invalid={Boolean(nameProblem)} onChange={e => setName(e.target.value)} /></label>}
    {nameProblem && <small role="alert" className="ie-error">{nameProblem}</small>}
    {original && original.rows === null && <p className="ie-note">Studio could not read this parameter's fields. Pick the fields it should offer; they replace the current ones.</p>}
    <OrderedList items={fields} keyOf={f => `${f.table}[${f.name}]`} label="Fields, in the order the slicer shows them" disabled={ctx.locked} onMove={(from, to) => setFields(list => moveItem(list, from, to))}
      render={(f, index) => <div className="me-level">
        <span className="me-level-column">{f.kind === "measure" ? `[${f.name}]` : `${f.table}[${f.name}]`}</span>
        <label><span className="sr-only">Label for {f.name}</span><input value={f.label} placeholder={f.name} disabled={ctx.locked} onChange={e => setFields(list => list.map((p, i) => i === index ? { ...p, label: e.target.value } : p))} /></label>
        <RemoveButton label={`Remove ${f.name}`} disabled={ctx.locked} onClick={() => setFields(list => list.filter((_, i) => i !== index))} />
      </div>} />
    <details className="me-section" open={!fields.length}>
      <summary>Add fields</summary>
      <label className="me-search"><span className="sr-only">Find a measure or column</span><input placeholder="Find a measure or column…" value={search} onChange={e => setSearch(e.target.value)} /></label>
      <ul className="me-picker">{candidates.map(f => <li key={`${f.kind}:${f.table}[${f.name}]`}>
        <button type="button" className="me-link" disabled={ctx.locked} onClick={() => setFields(list => [...list, f])}><Plus size={12} aria-hidden="true" />{f.kind === "measure" ? `[${f.name}]` : `${f.table}[${f.name}]`}<small>{f.kind === "measure" ? `measure in ${f.table}` : "column"}</small></button>
      </li>)}</ul>
    </details>
    <div className="me-actions">
      <QueueButton ctx={ctx} op={op} label={original ? "Queue this change" : "Queue new field parameter"} onQueued={onDone} />
      {original && <button type="button" className="secondary danger" disabled={ctx.locked} aria-expanded={deleting} onClick={() => setDeleting(true)}>Delete…</button>}
      <button type="button" className="secondary" onClick={onDone}>{original ? "Close" : "Cancel"}</button>
    </div>
    {deleting && original && <DeleteConfirm ctx={ctx} what={`the field parameter ${original.table}`} op={deleteTableOp(original.table)} onDone={() => { setDeleting(false); onDone(); }}
      guard={{ blocking: ctx.report.pages.flatMap(p => p.visuals.filter(v => v.bindings.some(b => b.table === original.table)).map(v => `The visual “${v.title || "Untitled"}” on ${p.title} uses it.`)), warnings: [] }} />}
  </section>;
}

// --- calculation groups ----------------------------------------------------------------------------------------------------

export function ModelEditorCalculationGroups({ ctx }: { ctx: ModelContext }) {
  const [selected, setSelected] = useState<string>();
  const [creating, setCreating] = useState(false);
  const current = ctx.facts.calculationGroups.find(g => g.table === selected);
  return <div className="me-split">
    <nav className="me-list" aria-label="Calculation groups">
      {ctx.facts.calculationGroups.map(g => <button type="button" key={g.table} aria-current={!creating && g.table === selected ? "true" : undefined} onClick={() => { setCreating(false); setSelected(g.table); }}>
        <Layers size={14} aria-hidden="true" /><span>{g.table}<small>{g.items.length} items{g.precedence !== undefined ? ` · precedence ${g.precedence}` : ""}</small></span></button>)}
      {!ctx.facts.calculationGroups.length && <p className="ie-note">This model has no calculation groups.</p>}
      <button type="button" className="secondary me-new" disabled={ctx.locked} aria-pressed={creating} onClick={() => { setCreating(true); setSelected(undefined); }}><Plus size={13} aria-hidden="true" />New calculation group</button>
    </nav>
    <section className="me-detail">
      {creating ? <NewCalculationGroup ctx={ctx} onDone={() => setCreating(false)} />
        : current ? <CalculationGroupEditor key={current.table} ctx={ctx} group={current} />
        : <p className="ie-note">A calculation group changes how measures are calculated (for example Current, Last year, Year to date). Studio's canvas draws visuals that use one in Power BI only.</p>}
    </section>
  </div>;
}

function CalculationGroupEditor({ ctx, group }: { ctx: ModelContext; group: FactCalculationGroup }) {
  const [precedence, setPrecedence] = useState(group.precedence === undefined ? "" : String(group.precedence));
  const [description, setDescription] = useState(group.description ?? "");
  const [open, setOpen] = useState<string>();
  const [deleting, setDeleting] = useState(false);
  const op = calculationGroupOp(group, { precedence, description });
  const items = [...group.items].sort((a, b) => (a.ordinal ?? 0) - (b.ordinal ?? 0));
  return <section className="me-editor" aria-label={`Calculation group ${group.table}`}>
    <h3><Layers size={14} aria-hidden="true" />{group.table}</h3>
    <div className="me-grid">
      <label className="ie-field-block">Precedence<input inputMode="numeric" value={precedence} disabled={ctx.locked} onChange={e => setPrecedence(e.target.value.replace(/[^\d]/g, ""))} /></label>
      <label className="ie-field-block">Description<input value={description} disabled={ctx.locked} onChange={e => setDescription(e.target.value)} /></label>
    </div>
    <div className="me-actions">
      <QueueButton ctx={ctx} op={op} />
      <button type="button" className="secondary danger" disabled={ctx.locked} aria-expanded={deleting} onClick={() => setDeleting(true)}>Delete group…</button>
    </div>
    {deleting && <DeleteConfirm ctx={ctx} what={`the calculation group ${group.table}`} op={deleteTableOp(group.table)} onDone={() => setDeleting(false)}
      guard={{ blocking: ctx.report.pages.flatMap(p => p.visuals.filter(v => v.bindings.some(b => b.table === group.table)).map(v => `The visual “${v.title || "Untitled"}” on ${p.title} uses it.`)), warnings: [] }} />}
    <h3 className="me-subhead">Items</h3>
    <ul className="me-items">{items.map(item => <li key={item.name}>
      <button type="button" className="me-link" aria-expanded={open === item.name} onClick={() => setOpen(open === item.name ? undefined : item.name)}>{item.name}<small>{item.ordinal !== undefined ? `order ${item.ordinal}` : ""}</small></button>
      {open === item.name && <CalculationItemEditor ctx={ctx} group={group.table} original={item} onDone={() => setOpen(undefined)} />}
    </li>)}
      <li><button type="button" className="me-link" aria-expanded={open === "\u0000new"} disabled={ctx.locked} onClick={() => setOpen(open === "\u0000new" ? undefined : "\u0000new")}><Plus size={12} aria-hidden="true" />New item</button>
        {open === "\u0000new" && <CalculationItemEditor ctx={ctx} group={group.table} taken={items.map(i => i.name)} onDone={() => setOpen(undefined)} />}</li>
    </ul>
  </section>;
}

function CalculationItemEditor({ ctx, group, original, taken = [], onDone }: { ctx: ModelContext; group: string; original?: FactCalculationItem; taken?: string[]; onDone: () => void }) {
  const [draft, setDraft] = useState<CalculationItemDraft>(() => calculationItemDraft(original));
  const [deleting, setDeleting] = useState(false);
  const set = (patch: Partial<CalculationItemDraft>) => setDraft(current => ({ ...current, ...patch }));
  const nameProblem = draft.name && draft.name.trim() !== original?.name ? modelNameProblem(draft.name, original ? [] : taken, "calculation item", original?.name) : undefined;
  const orderProblem = ordinalProblem(draft.ordinal);
  if (original?.truncated) return <p className="ie-note">{TRUNCATED_NOTE}</p>;
  const op = nameProblem || orderProblem ? undefined : calculationItemOp(group, original, draft);
  return <div className="me-item-editor">
    <div className="me-grid">
      <label className="ie-field-block">Name<input value={draft.name} maxLength={128} aria-invalid={Boolean(nameProblem)} disabled={ctx.locked} onChange={e => set({ name: e.target.value })} /></label>
      <label className="ie-field-block">Order<input inputMode="numeric" value={draft.ordinal} aria-invalid={Boolean(orderProblem)} disabled={ctx.locked} onChange={e => set({ ordinal: e.target.value })} /></label>
    </div>
    {(nameProblem || orderProblem) && <small role="alert" className="ie-error">{nameProblem ?? orderProblem}</small>}
    <label className="ie-field-block">Formula (DAX; SELECTEDMEASURE() is the measure being calculated)
      <textarea value={draft.expression} rows={4} spellCheck={false} placeholder="CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))" disabled={ctx.locked} onChange={e => set({ expression: e.target.value })} /></label>
    <label className="ie-field-block">Format string expression (optional)
      <textarea value={draft.formatStringExpression} rows={2} spellCheck={false} placeholder={"\"0.0%\""} disabled={ctx.locked} onChange={e => set({ formatStringExpression: e.target.value })} /></label>
    <div className="me-actions">
      <QueueButton ctx={ctx} op={op} label={original ? "Queue this change" : "Queue new item"} onQueued={onDone} />
      {original && <button type="button" className="secondary danger" disabled={ctx.locked} onClick={() => setDeleting(true)}>Delete…</button>}
    </div>
    {deleting && original && <DeleteConfirm ctx={ctx} what={`the item ${original.name}`} op={deleteCalculationItemOp(group, original.name)} guard={{ blocking: [], warnings: [] }} onDone={() => { setDeleting(false); onDone(); }} />}
  </div>;
}

function NewCalculationGroup({ ctx, onDone }: { ctx: ModelContext; onDone: () => void }) {
  const [name, setName] = useState("");
  const [precedence, setPrecedence] = useState("");
  const [items, setItems] = useState([{ name: "Current", expression: "SELECTEDMEASURE()" }]);
  const problem = name ? modelNameProblem(name, tableNamesTaken(ctx.facts), "calculation group") : undefined;
  const op = !problem && name.trim() && items.some(i => i.name.trim() && i.expression.trim()) ? createCalculationGroupOp(name, precedence, items) : undefined;
  return <section className="me-editor" aria-label="New calculation group">
    <h3><Plus size={14} aria-hidden="true" />New calculation group</h3>
    <div className="me-grid">
      <label className="ie-field-block">Name<input value={name} maxLength={128} aria-invalid={Boolean(problem)} onChange={e => setName(e.target.value)} /></label>
      <label className="ie-field-block">Precedence<input inputMode="numeric" value={precedence} onChange={e => setPrecedence(e.target.value.replace(/[^\d]/g, ""))} /></label>
    </div>
    {problem && <small role="alert" className="ie-error">{problem}</small>}
    {items.map((item, index) => <div className="me-item-editor" key={index}>
      <label className="ie-field-block">Item name<input value={item.name} onChange={e => setItems(list => list.map((i, n) => n === index ? { ...i, name: e.target.value } : i))} /></label>
      <label className="ie-field-block">Formula (DAX)<textarea rows={3} spellCheck={false} value={item.expression} onChange={e => setItems(list => list.map((i, n) => n === index ? { ...i, expression: e.target.value } : i))} /></label>
      {items.length > 1 && <RemoveButton label={`Remove item ${item.name || index + 1}`} onClick={() => setItems(list => list.filter((_, n) => n !== index))} />}
    </div>)}
    <div className="me-actions">
      <button type="button" className="secondary" onClick={() => setItems(list => [...list, { name: "", expression: "" }])}><Plus size={13} aria-hidden="true" />Add item</button>
      <QueueButton ctx={ctx} op={op} label="Queue new calculation group" onQueued={onDone} />
      <button type="button" className="secondary" onClick={onDone}>Cancel</button>
    </div>
    <p className="ie-note">Power BI turns on “Discourage implicit measures” for a model with a calculation group.</p>
  </section>;
}

// --- roles ---------------------------------------------------------------------------------------------------------------

export function ModelEditorRoles({ ctx }: { ctx: ModelContext }) {
  const [selected, setSelected] = useState<string>();
  const [creating, setCreating] = useState(false);
  const current = ctx.facts.roles.find(r => r.name === selected);
  return <div className="me-split">
    <nav className="me-list" aria-label="Roles">
      {ctx.facts.roles.map(r => <button type="button" key={r.name} aria-current={!creating && r.name === selected ? "true" : undefined} onClick={() => { setCreating(false); setSelected(r.name); }}>
        <Shield size={14} aria-hidden="true" /><span>{r.name}<small>{r.tables.length} table filter{r.tables.length === 1 ? "" : "s"}</small></span></button>)}
      {!ctx.facts.roles.length && <p className="ie-note">This model has no roles.</p>}
      <button type="button" className="secondary me-new" disabled={ctx.locked} aria-pressed={creating} onClick={() => { setCreating(true); setSelected(undefined); }}><Plus size={13} aria-hidden="true" />New role</button>
    </nav>
    <section className="me-detail">
      <p className="ie-note">Roles limit the rows a reader sees (row-level security). Studio's canvas and answers use every row; a role applies when you view the report as that role in Power BI. People are added to roles in the Power BI service after publishing.</p>
      {creating ? <RoleEditor key="new" ctx={ctx} onDone={() => setCreating(false)} />
        : current ? <RoleEditor key={current.name} ctx={ctx} original={current} onDone={() => setSelected(undefined)} /> : null}
    </section>
  </div>;
}

function RoleEditor({ ctx, original, onDone }: { ctx: ModelContext; original?: FactRole; onDone: () => void }) {
  const tables = visibleTables(ctx.facts);
  const [name, setName] = useState(original?.name ?? "");
  const [description, setDescription] = useState(original?.description ?? "");
  const [filters, setFilters] = useState<RoleFilterDraft[]>(() => original?.tables.map(t => ({ table: t.table, expression: t.filter })) ?? []);
  const [adding, setAdding] = useState("");
  const [deleting, setDeleting] = useState(false);
  const nameProblem = name && name.trim() !== original?.name ? modelNameProblem(name, ctx.facts.roles.map(r => r.name), "role", original?.name) : undefined;
  const cut = (table: string) => original?.tables.find(t => t.table === table)?.truncated === true;
  const op = nameProblem || !name.trim() ? undefined : roleOp(original, { name, description, filters });
  const free = tables.filter(t => !filters.some(f => f.table === t.name));
  return <section className="me-editor" aria-label={original ? `Role ${original.name}` : "New role"}>
    <h3><Shield size={14} aria-hidden="true" />{original ? original.name : "New role"}</h3>
    <div className="me-grid">
      <label className="ie-field-block">Name<input value={name} maxLength={128} aria-invalid={Boolean(nameProblem)} disabled={ctx.locked} onChange={e => setName(e.target.value)} /></label>
      <label className="ie-field-block">Description<input value={description} disabled={ctx.locked} onChange={e => setDescription(e.target.value)} /></label>
    </div>
    {nameProblem && <small role="alert" className="ie-error">{nameProblem}</small>}
    <h4 className="me-subhead">Table filters (DAX that is true for the rows the role may see)</h4>
    {filters.map((f, index) => <div className="me-item-editor" key={f.table}>
      {cut(f.table) ? <><strong>{f.table}</strong><p className="ie-note">{TRUNCATED_NOTE}</p></>
        : <label className="ie-field-block">{f.table}<textarea rows={2} spellCheck={false} value={f.expression} placeholder={`[Region] = "East"`} disabled={ctx.locked}
          onChange={e => setFilters(list => list.map((item, i) => i === index ? { ...item, expression: e.target.value } : item))} /></label>}
      <RemoveButton label={`Remove the filter on ${f.table}`} disabled={ctx.locked} onClick={() => setFilters(list => list.filter((_, i) => i !== index))} />
    </div>)}
    {free.length > 0 && <div className="me-actions">
      <label className="ie-field-block">Filter another table<select value={adding} disabled={ctx.locked} onChange={e => setAdding(e.target.value)}><option value="">Choose a table…</option>{free.map(t => <option key={t.name} value={t.name}>{t.name}</option>)}</select></label>
      <button type="button" className="secondary" disabled={!adding || ctx.locked} onClick={() => { setFilters(list => [...list, { table: adding, expression: "" }]); setAdding(""); }}>Add filter</button>
    </div>}
    <div className="me-actions">
      <QueueButton ctx={ctx} op={op} label={original ? "Queue this change" : "Queue new role"} onQueued={onDone} />
      {original && <button type="button" className="secondary danger" disabled={ctx.locked} aria-expanded={deleting} onClick={() => setDeleting(true)}>Delete…</button>}
      <button type="button" className="secondary" onClick={onDone}>{original ? "Close" : "Cancel"}</button>
    </div>
    {deleting && original && <DeleteConfirm ctx={ctx} what={`the role ${original.name}`} op={deleteRoleOp(original.name)} guard={{ blocking: [], warnings: ["People assigned to this role in the Power BI service lose it when you publish."] }} onDone={() => { setDeleting(false); onDone(); }} />}
  </section>;
}
