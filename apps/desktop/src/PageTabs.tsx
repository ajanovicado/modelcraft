import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronUp, Copy, Eye, EyeOff, LayoutDashboard, Pencil, Plus, Smartphone, Trash2, X } from "lucide-react";
import { EditorDialog } from "./EditorDialog";
import type { Operation } from "./importedEditing";
import { columnRef, pageTypeOp, type ColumnRef, type PageBinding, type PageType } from "./importedPanes";

export type PageInfo = { id: string; title: string; width: number; height: number; hidden?: boolean; visuals: unknown[] };

const SIZES: { label: string; width: number; height: number }[] = [
  { label: "16:9 (1280 × 720)", width: 1280, height: 720 }, { label: "4:3 (960 × 720)", width: 960, height: 720 },
  { label: "Letter (816 × 1056)", width: 816, height: 1056 }, { label: "Tooltip (320 × 240)", width: 320, height: 240 },
];

/**
 * The page rail: select, rename (double-click or F2), add, duplicate, delete, reorder (drag, or the
 * move buttons for keyboard users), hide and show, and page size. Each change is one edit.
 */
/**
 * Page type (normal, drillthrough for chosen columns, report-page tooltip), the phone layout and the page view. The page's
 * current type comes from the render (`page.page_type`, `page.binding`).
 */
function PageTypeSettings({ page, pageType, binding, columns, disabled, onEdit, onMobile }: {
  page: PageInfo; pageType: PageType; binding?: PageBinding; columns: ColumnRef[]; disabled: boolean;
  onEdit: (operations: Operation[], label: string) => Promise<boolean>; onMobile?: () => void;
}) {
  const [type, setType] = useState<PageType>(pageType);
  const [fields, setFields] = useState<ColumnRef[]>(() => binding?.fields.map(columnRef) ?? []);
  const [keepAll, setKeepAll] = useState(binding?.keepAllFilters ?? true);
  const [pick, setPick] = useState("");
  useEffect(() => { setType(pageType); setFields(binding?.fields.map(columnRef) ?? []); setKeepAll(binding?.keepAllFilters ?? true); }, [page.id, pageType, binding]);
  const key = (column: ColumnRef) => `${column.table}[${column.name}]`;
  const changed = type !== pageType || (type !== "default" && JSON.stringify(fields.map(key)) !== JSON.stringify((binding?.fields.map(columnRef) ?? []).map(key))) || (type === "drillthrough" && keepAll !== (binding?.keepAllFilters ?? true));
  const apply = () => {
    const op = pageTypeOp(page.id, type, { fields, keepAllFilters: type === "drillthrough" ? keepAll : undefined });
    if (op) void onEdit([op], type === "default" ? "Making it a normal page" : type === "drillthrough" ? "Making it a drillthrough page" : "Making it a tooltip page");
  };
  return <>
    <label>Page type
      <select value={type} disabled={disabled} onChange={event => setType(event.target.value as PageType)}>
        <option value="default">Normal page</option><option value="drillthrough">Drillthrough page</option><option value="tooltip">Tooltip page</option></select>
    </label>
    {type !== "default" && <div className="ie-page-fields">
      <span>{type === "drillthrough" ? "Drill through on" : "Show for (optional)"}</span>
      <ul>{fields.map(field => <li key={key(field)}>{key(field)}<button type="button" className="icon-button" disabled={disabled} aria-label={`Remove ${key(field)}`} onClick={() => setFields(fields.filter(item => key(item) !== key(field)))}><X size={11} /></button></li>)}</ul>
      <select aria-label="Add a column" value={pick} disabled={disabled} onChange={event => { const found = columns.find(column => key(column) === event.target.value); if (found && !fields.some(item => key(item) === key(found))) setFields([...fields, found]); setPick(""); }}>
        <option value="">Add a column…</option>{columns.map(column => <option key={key(column)} value={key(column)}>{key(column)}</option>)}</select>
      {type === "drillthrough" && <label className="ie-check"><input type="checkbox" checked={keepAll} disabled={disabled} onChange={event => setKeepAll(event.target.checked)} />Keep all filters</label>}
      {type === "tooltip" && <small>Tooltip pages are hidden from readers and sized 320 × 240.</small>}
    </div>}
    {changed && <button type="button" className="secondary" disabled={disabled || (type === "drillthrough" && !fields.length)} onClick={apply}>Apply page type</button>}
    <label>Page view
      <select value="" disabled={disabled} onChange={event => { if (event.target.value) void onEdit([{ op: "set_page_view", page: page.id, view: event.target.value }], "Changing the page view"); }}>
        <option value="">Choose…</option><option value="fit_to_page">Fit to page</option><option value="fit_to_width">Fit to width</option><option value="actual_size">Actual size</option></select>
    </label>
    {onMobile && <button type="button" className="secondary" disabled={disabled} onClick={onMobile}><Smartphone size={13} />Phone layout…</button>}
  </>;
}

export function PageTabs({ pages, pageId, disabled, onSelect, onEdit, pageType = "default", binding, columns = [], onMobile }: {
  pages: PageInfo[]; pageId?: string; disabled: boolean;
  onSelect: (id: string) => void; onEdit: (operations: Operation[], label: string) => Promise<boolean>;
  pageType?: PageType; binding?: PageBinding; columns?: ColumnRef[]; onMobile?: () => void;
}) {
  const [renaming, setRenaming] = useState<string>();
  const [draft, setDraft] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<PageInfo>();
  const [dragging, setDragging] = useState<string>();
  const input = useRef<HTMLInputElement>(null);
  const known = useRef(new Set(pages.map(p => p.id)));
  const selectNew = useRef(false);
  const page = pages.find(p => p.id === pageId) ?? pages[0];
  const index = page ? pages.findIndex(p => p.id === page.id) : -1;

  useEffect(() => { if (renaming) { input.current?.focus(); input.current?.select(); } }, [renaming]);
  // After Add or Duplicate the new page becomes the selected one.
  useEffect(() => {
    const fresh = pages.find(p => !known.current.has(p.id));
    known.current = new Set(pages.map(p => p.id));
    if (fresh && selectNew.current) { selectNew.current = false; onSelect(fresh.id); }
  }, [pages, onSelect]);

  const startRename = (target: PageInfo) => { if (disabled) return; cancelled.current = false; setRenaming(target.id); setDraft(target.title); };
  // Escape unmounts the box while it has focus; some browsers then report a blur, which must not save the name.
  const cancelled = useRef(false);
  const commitRename = (target: PageInfo) => {
    const name = draft.trim();
    setRenaming(undefined);
    if (cancelled.current) { cancelled.current = false; return; }
    if (name && name !== target.title) void onEdit([{ op: "rename_page", page: target.id, name }], "Rename page");
  };
  const nextName = () => { let n = pages.length + 1; while (pages.some(p => p.title === `Page ${n}`)) n += 1; return `Page ${n}`; };
  const size = page ? SIZES.find(s => s.width === page.width && s.height === page.height) : undefined;
  const move = (target: PageInfo, to: number) => { if (to >= 0 && to < pages.length && to !== pages.indexOf(target)) void onEdit([{ op: "move_page", page: target.id, index: to }], "Move page"); };

  return <nav className="ie-pages" aria-label="Report pages">
    <strong>Pages</strong>
    <ul className="ie-page-items">
      {pages.map((item, position) => <li key={item.id} className={dragging === item.id ? "dragging" : undefined} draggable={!disabled && renaming !== item.id}
        onDragStart={event => { setDragging(item.id); event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", item.id); }}
        onDragOver={event => { if (dragging) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; } }}
        onDrop={event => { event.preventDefault(); const source = pages.find(p => p.id === dragging); setDragging(undefined); if (source) move(source, position); }}
        onDragEnd={() => setDragging(undefined)}>
        {renaming === item.id
          ? <input ref={input} className="ie-rename" aria-label={`Rename ${item.title}`} value={draft} maxLength={100} onChange={event => setDraft(event.target.value)} onBlur={() => commitRename(item)}
            onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); commitRename(item); } if (event.key === "Escape") { event.preventDefault(); cancelled.current = true; setRenaming(undefined); } }} />
          : <button type="button" aria-current={page?.id === item.id ? "page" : undefined} title="Double-click to rename" onClick={() => onSelect(item.id)} onDoubleClick={() => startRename(item)}
            onKeyDown={event => { if (event.key === "F2") { event.preventDefault(); startRename(item); } }}>
            <LayoutDashboard size={15} aria-hidden="true" /><span>{item.title}<small>{item.visuals.length} visuals{item.hidden ? " · hidden" : ""}</small></span></button>}
      </li>)}
    </ul>
    <div className="ie-page-actions" role="group" aria-label="Page actions">
      <button type="button" className="secondary" disabled={disabled} title="Add a page" aria-label="Add a page" onClick={() => { selectNew.current = true; void onEdit([{ op: "add_page", name: nextName(), width: page?.width ?? 1280, height: page?.height ?? 720, ...(page ? { after: page.id } : {}) }], "Add page"); }}><Plus size={14} /></button>
      <button type="button" className="secondary" disabled={disabled || !page} title="Duplicate this page" aria-label="Duplicate this page" onClick={() => { if (!page) return; selectNew.current = true; void onEdit([{ op: "duplicate_page", page: page.id }], "Duplicate page"); }}><Copy size={14} /></button>
      <button type="button" className="secondary" disabled={disabled || !page} title="Rename this page" aria-label="Rename this page" onClick={() => page && startRename(page)}><Pencil size={14} /></button>
      <button type="button" className="secondary" disabled={disabled || !page} title={page?.hidden ? "Show this page" : "Hide this page"} aria-label={page?.hidden ? "Show this page" : "Hide this page"} aria-pressed={Boolean(page?.hidden)} onClick={() => page && void onEdit([{ op: "set_page_hidden", page: page.id, hidden: !page.hidden }], page.hidden ? "Show page" : "Hide page")}>{page?.hidden ? <Eye size={14} /> : <EyeOff size={14} />}</button>
      <button type="button" className="secondary" disabled={disabled || !page || index <= 0} title="Move this page up" aria-label="Move this page up" onClick={() => page && move(page, index - 1)}><ChevronUp size={14} /></button>
      <button type="button" className="secondary" disabled={disabled || !page || index < 0 || index >= pages.length - 1} title="Move this page down" aria-label="Move this page down" onClick={() => page && move(page, index + 1)}><ChevronDown size={14} /></button>
      <button type="button" className="secondary danger" disabled={disabled || !page || pages.length < 2} title={pages.length < 2 ? "A report needs at least one page" : "Delete this page"} aria-label="Delete this page" onClick={() => page && setConfirmDelete(page)}><Trash2 size={14} /></button>
    </div>
    {page && <details className="ie-page-settings">
      <summary>Page settings</summary>
      <label>Page size
        <select value={size ? `${size.width}x${size.height}` : "custom"} disabled={disabled} onChange={event => {
          const chosen = SIZES.find(s => `${s.width}x${s.height}` === event.target.value);
          if (chosen) void onEdit([{ op: "set_page_size", page: page.id, width: chosen.width, height: chosen.height }], "Change page size");
        }}>{!size && <option value="custom">Custom ({page.width} × {page.height})</option>}{SIZES.map(s => <option key={s.label} value={`${s.width}x${s.height}`}>{s.label}</option>)}</select>
      </label>
      <PageTypeSettings page={page} pageType={pageType} binding={binding} columns={columns} disabled={disabled} onEdit={onEdit} onMobile={onMobile} />
    </details>}
    {confirmDelete && <EditorDialog title={`Delete “${confirmDelete.title}”?`} tone="warn" onCancel={() => setConfirmDelete(undefined)}
      footer={<><button type="button" className="secondary" data-autofocus onClick={() => setConfirmDelete(undefined)}>Cancel</button>
        <button type="button" className="primary yellow" onClick={() => { const target = confirmDelete; setConfirmDelete(undefined); void onEdit([{ op: "delete_page", page: target.id }], "Delete page"); }}>Delete page</button></>}>
      <p className="ie-dialog-text">This removes the page and its {confirmDelete.visuals.length} visual{confirmDelete.visuals.length === 1 ? "" : "s"}. If buttons or bookmarks point at it you will be asked before they are cleaned up. You can undo this from History.</p>
    </EditorDialog>}
  </nav>;
}
