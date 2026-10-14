import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Maximize2, Search, Table2, ZoomIn, ZoomOut } from "lucide-react";
import { RelationshipInspector } from "./RelationshipInspector";
import { ModelCanvas } from "./ModelCanvas";
import type { ModelWorkbenchTable, ModelWorkbenchRelationship } from "./ModelWorkbench";

export function ModelDiagram({ tables, relationships, selectedIds, onReview, measureCount }: {
  tables: readonly ModelWorkbenchTable[]; relationships: readonly ModelWorkbenchRelationship[];
  selectedIds: ReadonlySet<string>; onReview: (id?: string) => void; measureCount?: number;
}) {
  const [relationshipId, setRelationshipId] = useState<string>();
  const [inspector, setInspector] = useState(false);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState(tables[0]?.id);
  const [columns, setColumns] = useState("");
  const [page, setPage] = useState(0);
  const [zoom, setZoom] = useState(1);
  const footerRef = useRef<HTMLElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState({ width: 960, height: 560, footerHeight: 64 });
  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return;
    const observer = new ResizeObserver(() => {
      const width = element.clientWidth;
      const height = element.clientHeight;
      const footerHeight = footerRef.current?.offsetHeight || 64;
      if (width && height) setViewport(previous => previous.width === width && previous.height === height && previous.footerHeight === footerHeight ? previous : { width, height, footerHeight });
    });
    observer.observe(element);
    if (footerRef.current) observer.observe(footerRef.current);
    return () => observer.disconnect();
  }, []);
  const activeRelationship = relationships.find(r => r.id === relationshipId);
  const active = tables.find(t => t.id === selected) ?? tables[0];
  const filtered = useMemo(() => tables.filter(t => (t.displayName || t.name).toLowerCase().includes(search.toLowerCase())), [tables, search]);
  const connected = useMemo(() => new Set(relationships.filter(r => r.reviewStatus !== "rejected" && (r.fromTableId === active?.id || r.toTableId === active?.id)).flatMap(r => [r.fromTableId, r.toTableId])), [relationships, active]);
  const visible = tables.length <= 12 ? tables : [active, ...tables.filter(t => t.id !== active?.id && connected.has(t.id))].filter((t): t is ModelWorkbenchTable => Boolean(t)).slice(0, 12);
  const visibleIds = new Set(visible.map(t => t.id));
  const relatedJoins = relationships.filter(r => r.reviewStatus !== "rejected" && visibleIds.has(r.fromTableId) && visibleIds.has(r.toTableId));
  const joins = relatedJoins.slice(0, 60);
  const selectedColumns = active?.columns?.filter(c => c.name.toLowerCase().includes(columns.toLowerCase())) ?? [];
  return <div className={`model-diagram-workspace ${inspector ? "inspector-open" : ""}`} style={{ "--model-footer-height": `${viewport.footerHeight}px` } as CSSProperties}>
    <aside className="workbench-explorer" aria-label="Model table explorer">
      <h3>Tables <span>{tables.length}</span></h3>
      <label className="workbench-search"><Search size={14}/><input aria-label="Search model tables" placeholder="Search tables…" value={search} onChange={e => { setSearch(e.target.value); setPage(0); }}/></label>
      <div className="workbench-table-list">{filtered.slice(page * 50, (page + 1) * 50).map(t => <button key={t.id} className={t.id === active?.id ? "active" : ""} aria-pressed={t.id === active?.id} onClick={() => { setSelected(t.id); setColumns(""); setRelationshipId(undefined); }}><Table2 size={16}/><span><strong>{t.displayName || t.name}</strong><small>{t.rowCount?.toLocaleString() ?? "Unknown"} rows</small></span></button>)}{!filtered.length && <p>No matching tables.</p>}</div>
      {filtered.length > 50 && <div className="workbench-paging"><button className="secondary" disabled={!page} onClick={() => setPage(page - 1)}>Previous</button><button className="secondary" disabled={(page + 1) * 50 >= filtered.length} onClick={() => setPage(page + 1)}>Next</button></div>}
    </aside>
    <div className="model-diagram-center">
      <div className="model-diagram-scroll" ref={viewportRef}><ModelCanvas tables={visible} relationships={joins} selectedIds={selectedIds} tableLabel={id => tables.find(t => t.id === id)?.displayName || tables.find(t => t.id === id)?.name || id} activeTableId={active?.id} onTableSelect={id => { setSelected(id); setColumns(""); setRelationshipId(undefined); }} activeRelationshipId={activeRelationship?.id} onRelationshipSelect={id => { setRelationshipId(id); setInspector(true); }} viewportWidth={viewport.width} viewportHeight={viewport.height} zoom={zoom}/></div>
      <footer ref={footerRef} className="model-diagram-footer"><div className="model-diagram-summary"><span>{visible.length} of {tables.length} tables · {joins.length} relationships{measureCount !== undefined ? ` · ${measureCount} measures` : ""}{relatedJoins.length > 60 ? " · See Relationships for all" : ""}{tables.length > 12 ? " · Selected table and related tables" : ""}</span>{joins.some(r => r.blockers.length > 0) && <button className="model-blocker-link" onClick={() => onReview()}>{joins.filter(r => r.blockers.length > 0).length} blocked · Review</button>}</div><div><button className="secondary inspector-toggle" aria-expanded={inspector} onClick={() => setInspector(!inspector)}>Properties</button><button className="secondary icon-only" aria-label="Zoom out model" disabled={zoom > 0 && zoom <= .5} onClick={() => setZoom(Math.max(.5, (zoom || 1) - .25))}><ZoomOut size={15}/></button><button className="secondary" onClick={() => setZoom(1)} title="Reset zoom">{zoom ? `${Math.round(zoom * 100)}%` : "Fit"}</button><button className="secondary icon-only" aria-label="Zoom in model" disabled={zoom >= 2} onClick={() => setZoom(Math.min(2, (zoom || 1) + .25))}><ZoomIn size={15}/></button><button className="secondary icon-only" aria-label="Fit model to view" title="Fit model to view" onClick={() => setZoom(0)}><Maximize2 size={15}/></button></div></footer>
    </div>
    {active && <aside className="model-inspector" aria-label={activeRelationship ? "Relationship properties" : "Table properties"}><button className="secondary inspector-close" onClick={() => setInspector(false)}>Close properties</button>{activeRelationship ? <RelationshipInspector relationship={activeRelationship} tables={tables} onReview={() => onReview(activeRelationship.id)} onBack={() => setRelationshipId(undefined)}/> : <><h3><Table2 size={16}/>{active.displayName || active.name}</h3><h4>Table properties</h4><dl><div><dt>Rows</dt><dd>{active.rowCount?.toLocaleString() ?? "Unknown"}</dd></div><div><dt>Role</dt><dd>{active.role || "Unassigned"}</dd></div><div><dt>Columns</dt><dd>{active.columns?.length ?? 0}</dd></div></dl><h4>Columns ({active.columns?.length ?? 0})</h4><label className="workbench-search"><Search size={14}/><input aria-label="Search model columns" placeholder="Search columns…" value={columns} onChange={e => setColumns(e.target.value)}/></label><div className="model-field-list">{selectedColumns.slice(0, 100).map(c => <div key={c.id}><span title={c.name}>{c.name}<small>{c.candidateKey ? "Key candidate" : c.hidden ? "Hidden" : ""}</small></span><code>{c.dataType}</code></div>)}{selectedColumns.length > 100 && <p>Showing 100 columns. Search to narrow the list.</p>}</div></>}</aside>}
  </div>;
}
