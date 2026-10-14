import { useMemo, useState, type KeyboardEvent } from "react";
import { Network, Plus } from "lucide-react";
import {
  CARDINALITY_LABELS, columnsOf, createRelationshipOp, deleteRelationshipOp, relationshipDraft, relationshipKey, relationshipLabel, relationshipProblems,
  updateRelationshipOp, visibleTables, type Cardinality, type FactRelationship, type Guard, type ModelFacts, type RelationshipDraft,
} from "./modelEditing";
import { DeleteConfirm, QueueButton, type ModelContext } from "./ModelEditorShared";

/** Relationships: a small diagram, the list, and an editor for cardinality, cross filter and active; create and delete. */
export function ModelEditorRelationships({ ctx, selected, onSelect }: { ctx: ModelContext; selected?: string; onSelect: (key?: string) => void }) {
  const [creating, setCreating] = useState(false);
  const relationships = ctx.facts.relationships;
  const current = relationships.find(r => relationshipKey(relationshipDraft(r)) === selected);
  return <div className="me-relationships">
    <RelationshipDiagram facts={ctx.facts} selected={selected} onSelect={key => { setCreating(false); onSelect(key); }} />
    <div className="me-actions"><button type="button" className="secondary" disabled={ctx.locked} aria-pressed={creating} onClick={() => { setCreating(true); onSelect(undefined); }}><Plus size={13} aria-hidden="true" />New relationship</button></div>
    <div className="me-table-wrap"><table className="me-table">
      <thead><tr><th scope="col">From (many side)</th><th scope="col">To (one side)</th><th scope="col">Cardinality</th><th scope="col">Cross filter</th><th scope="col">Status</th></tr></thead>
      <tbody>{relationships.map(r => {
        const draft = relationshipDraft(r);
        const key = relationshipKey(draft);
        return <tr key={key} className={key === selected ? "selected" : undefined}>
          <td><button type="button" className="me-link" aria-current={key === selected ? "true" : undefined} onClick={() => { setCreating(false); onSelect(key); }}>{draft.fromTable}[{draft.fromColumn}]</button></td>
          <td>{draft.toTable}[{draft.toColumn}]</td><td>{CARDINALITY_LABELS[draft.cardinality]}</td><td>{draft.crossFilter === "both" ? "Both" : "Single"}</td>
          <td>{r.active ? "Active" : <span className="me-badge">Inactive</span>}</td></tr>;
      })}</tbody>
    </table>{!relationships.length && <p className="ie-note">This model has no relationships.</p>}</div>
    {creating && <RelationshipEditor key="new" ctx={ctx} onDone={() => setCreating(false)} />}
    {current && !creating && <RelationshipEditor key={selected} ctx={ctx} original={current} onDone={() => onSelect(undefined)} />}
  </div>;
}

const emptyDraft = (facts: ModelFacts): RelationshipDraft => {
  const tables = visibleTables(facts);
  const from = tables[0]?.name ?? "", to = tables[1]?.name ?? "";
  return { fromTable: from, fromColumn: columnsOf(facts, from)[0]?.name ?? "", toTable: to, toColumn: columnsOf(facts, to)[0]?.name ?? "", cardinality: "many_to_one", crossFilter: "single", active: true };
};

function relationshipGuard(ctx: ModelContext, r: FactRelationship): Guard {
  const d = relationshipDraft(r);
  const uses = ctx.report.measures.filter(m => /USERELATIONSHIP|CROSSFILTER/i.test(m.Expression) && m.Expression.toLowerCase().includes(`[${d.fromColumn.toLowerCase()}]`) && m.Expression.toLowerCase().includes(`[${d.toColumn.toLowerCase()}]`));
  return {
    blocking: [],
    warnings: [
      ...(uses.length ? [`The measure${uses.length === 1 ? "" : "s"} ${uses.map(m => m.Name).slice(0, 4).join(", ")} name${uses.length === 1 ? "s" : ""} this relationship (USERELATIONSHIP or CROSSFILTER) and would stop working.`] : []),
      ...(r.active ? ["Visuals that combine these two tables stop filtering each other."] : []),
    ],
  };
}

function RelationshipEditor({ ctx, original, onDone }: { ctx: ModelContext; original?: FactRelationship; onDone: () => void }) {
  const [draft, setDraft] = useState<RelationshipDraft>(() => original ? relationshipDraft(original) : emptyDraft(ctx.facts));
  const [deleting, setDeleting] = useState(false);
  const tables = visibleTables(ctx.facts);
  const set = (patch: Partial<RelationshipDraft>) => setDraft(current => {
    const next = { ...current, ...patch };
    if (patch.fromTable !== undefined && patch.fromColumn === undefined) next.fromColumn = columnsOf(ctx.facts, patch.fromTable)[0]?.name ?? "";
    if (patch.toTable !== undefined && patch.toColumn === undefined) next.toColumn = columnsOf(ctx.facts, patch.toTable)[0]?.name ?? "";
    return next;
  });
  const problems = relationshipProblems(ctx.facts, draft, original);
  const op = problems.blocking.length ? undefined : original ? updateRelationshipOp(original, draft) : createRelationshipOp(draft);
  const end = (side: "from" | "to") => {
    const table = side === "from" ? draft.fromTable : draft.toTable;
    const column = side === "from" ? draft.fromColumn : draft.toColumn;
    const label = side === "from" ? "From (many side)" : "To (one side)";
    return <fieldset className="me-end"><legend>{label}</legend>
      <label className="ie-field-block">Table<select value={table} disabled={Boolean(original) || ctx.locked} onChange={e => set(side === "from" ? { fromTable: e.target.value } : { toTable: e.target.value })}>
        {tables.map(t => <option key={t.name} value={t.name}>{t.name}</option>)}</select></label>
      <label className="ie-field-block">Column<select value={column} disabled={Boolean(original) || ctx.locked} onChange={e => set(side === "from" ? { fromColumn: e.target.value } : { toColumn: e.target.value })}>
        {columnsOf(ctx.facts, table).map(c => <option key={c.name} value={c.name}>{c.name}</option>)}</select></label>
    </fieldset>;
  };
  return <section className="me-editor" aria-label={original ? `Relationship ${relationshipLabel(draft)}` : "New relationship"}>
    <h3><Network size={14} aria-hidden="true" />{original ? relationshipLabel(draft) : "New relationship"}</h3>
    <div className="me-ends">{end("from")}{end("to")}</div>
    <div className="me-grid">
      <label className="ie-field-block">Cardinality<select value={draft.cardinality} disabled={ctx.locked} onChange={e => set({ cardinality: e.target.value as Cardinality })}>
        {(Object.keys(CARDINALITY_LABELS) as Cardinality[]).map(c => <option key={c} value={c}>{CARDINALITY_LABELS[c]}</option>)}</select></label>
      <label className="ie-field-block">Cross filter direction<select value={draft.crossFilter} disabled={ctx.locked} onChange={e => set({ crossFilter: e.target.value === "both" ? "both" : "single" })}>
        <option value="single">Single</option><option value="both">Both</option></select></label>
      <label className="me-toggle"><input type="checkbox" checked={draft.active} disabled={ctx.locked} onChange={e => set({ active: e.target.checked })} />Make this relationship active</label>
    </div>
    {problems.blocking.map(line => <p key={line} className="ie-note ie-error" role="alert">{line}</p>)}
    {problems.warnings.map(line => <p key={line} className="ie-note">{line}</p>)}
    <div className="me-actions">
      <QueueButton ctx={ctx} op={op} label={original ? "Queue this change" : "Queue new relationship"} onQueued={onDone} />
      {original && <button type="button" className="secondary danger" disabled={ctx.locked} aria-expanded={deleting} onClick={() => setDeleting(true)}>Delete…</button>}
      <button type="button" className="secondary" onClick={onDone}>{original ? "Close" : "Cancel"}</button>
    </div>
    {deleting && original && <DeleteConfirm ctx={ctx} what={`the relationship ${relationshipLabel(draft)}`} guard={relationshipGuard(ctx, original)} op={deleteRelationshipOp(original)} onDone={() => { setDeleting(false); onDone(); }} />}
  </section>;
}

const NODE_W = 132, NODE_H = 30, WIDTH = 720, HEIGHT = 300;

/** The diagram's positions: the related tables on an ellipse, in the model's order. Pure, for tests. */
export function diagramLayout(tables: string[]): Record<string, { x: number; y: number }> {
  const n = tables.length;
  const positions: Record<string, { x: number; y: number }> = {};
  tables.forEach((name, i) => {
    const angle = n === 1 ? 0 : (2 * Math.PI * i) / n - Math.PI / 2;
    positions[name] = { x: WIDTH / 2 + (n === 1 ? 0 : (WIDTH / 2 - NODE_W / 2 - 12) * Math.cos(angle)), y: HEIGHT / 2 + (n === 1 ? 0 : (HEIGHT / 2 - NODE_H / 2 - 12) * Math.sin(angle)) };
  });
  return positions;
}

/** Diagram-lite: the tables that have relationships and a line per relationship (dashed when inactive); a line opens its editor. */
function RelationshipDiagram({ facts, selected, onSelect }: { facts: ModelFacts; selected?: string; onSelect: (key: string) => void }) {
  const drafts = useMemo(() => facts.relationships.map(r => ({ r, d: relationshipDraft(r), key: relationshipKey(relationshipDraft(r)) })), [facts.relationships]);
  const names = useMemo(() => [...new Set(drafts.flatMap(({ d }) => [d.fromTable, d.toTable]))], [drafts]);
  const at = useMemo(() => diagramLayout(names), [names]);
  if (!names.length) return null;
  const keyed = (key: string) => (event: KeyboardEvent<SVGGElement>) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(key); } };
  return <figure className="me-diagram">
    <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="group" aria-label="Relationship diagram">
      {drafts.map(({ r, d, key }) => {
        const a = at[d.fromTable], b = at[d.toTable];
        if (!a || !b) return null;
        const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        const near = (p: { x: number; y: number }, q: { x: number; y: number }) => ({ x: p.x + (q.x - p.x) * 0.22, y: p.y + (q.y - p.y) * 0.22 });
        const fromEnd = near(a, b), toEnd = near(b, a);
        const many = d.cardinality !== "one_to_one";
        return <g key={key} className={`me-edge${key === selected ? " selected" : ""}${r.active ? "" : " inactive"}`} role="button" tabIndex={0}
          aria-label={`${relationshipLabel(d)}, ${CARDINALITY_LABELS[d.cardinality]}, ${d.crossFilter === "both" ? "filters both ways" : "filters one way"}${r.active ? "" : ", inactive"}`}
          onClick={() => onSelect(key)} onKeyDown={keyed(key)}>
          <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} className="me-edge-hit" />
          <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} className="me-edge-line" />
          <text x={fromEnd.x} y={fromEnd.y} className="me-edge-end">{many ? "*" : "1"}</text>
          <text x={toEnd.x} y={toEnd.y} className="me-edge-end">{d.cardinality === "many_to_many" ? "*" : "1"}</text>
          <text x={mid.x} y={mid.y - 4} className="me-edge-dir">{d.crossFilter === "both" ? "⇄" : "→"}</text>
        </g>;
      })}
      {names.map(name => <g key={name} className="me-node" aria-hidden="true">
        <rect x={at[name].x - NODE_W / 2} y={at[name].y - NODE_H / 2} width={NODE_W} height={NODE_H} rx={5} />
        <text x={at[name].x} y={at[name].y + 4}>{name.length > 18 ? `${name.slice(0, 17)}…` : name}</text>
      </g>)}
    </svg>
    <figcaption className="ie-note">* marks the many side and 1 the one side; → filters one way (from the one side), ⇄ both ways; dashed lines are inactive. Select a line to edit it.</figcaption>
  </figure>;
}
