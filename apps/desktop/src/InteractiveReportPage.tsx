import { useCallback, useEffect, useId, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { CornerUpLeft, Ellipsis, Info, Loader2, Monitor, RefreshCw, RotateCcw, Smartphone } from "lucide-react";

import { apiRaw } from "./api";
import { needsDesktop, nextCheckIn, type DesktopWait } from "./desktopConnect";

import { PreviewChart, type DatumPointer } from "./PreviewChart";
import { ReportPaletteContext } from "./reportPalette";
import {
  buildChart, CARD_TILE_PADDING, CARD_TILE_PADDING_COMPACT, cardValueStyle, categoryColumn, cellText, clientToPage, containerStyle, containerTitleView, display, drawState,
  effectiveContainer, fidelityNote, fontStyle, hitTest, layoutRects, opaque, pageLayers, panelGrid, pendingKey, runRender, safeColor,
  safeImage, sharedDomain, slicerGridStyle, slicerTileStyle, smallMultiplePanels, sortByRank, tableView, tileStyle, titleView, valueColumns, withQuery,
  type Action, type Bookmark, type Drillthrough, type FidelityNote, type Head, type LayoutVisual, type MobileLayout, type OverlayContext, type OverlayRect, type PageStyle, type Rect,
  type RenderResult, type RenderVisual as Visual, type Theme, type Value,
} from "./reportRender";
import { cardColors, cellLook, dataBarGeometry, dataBarRange, steppedRows, totalsView } from "./reportCells";
import {
  actionOutcome, actionState, bookmarkPlan, clearSyncSelections, dataPointRef, describeDrill, drillFiltersFor, drillRequest, drillTargets, dropView,
  findBookmark, floatingPosition, peekView, phoneLayout, recordSyncSelection, rememberSyncGroups, renderBody, rowOfCategory, selectionDrill,
  selectionsOf, sharedGroup, stashView, syncedSelections, tooltipDrill, tooltipKey, tooltipScale,
  type DataPointRef, type DrillFilter, type DrillRequest, type DrillSource,
} from "./reportActions";
import { ButtonBody, CfIcon, FidelityBadge, FidelityFrame, NavigatorBody, PictureBody, PointMenu, ShapeBody, TextboxBody, UrlConfirm, type MenuItem } from "./reportContent";

import "./interactive-report.css";

export type { OverlayContext, RenderVisual, RenderResult } from "./reportRender";
export type { DataPointRef, DrillFilter, DrillRequest, DrillSource } from "./reportActions";
type OriginalLayout = { width: number; height: number; visuals: LayoutVisual[] };

/** How the canvas is being used: `view` reads and filters, `edit` selects and lets an overlay move things. */
export type CanvasMode = "view" | "edit";
export type SelectMeta = { altKey: boolean; ctrlKey: boolean; shiftKey: boolean };
/** An item a caller adds to a data point's right-click menu (for example "Explain this number"). */
export type PointAction = { label: string; run: () => void; disabled?: boolean; hint?: string };

type Filters = Record<string, Value[]>;

const pending = new Map<string, Promise<unknown>>();

/** A POST whose thrown error keeps the HTTP status and the error `code` (`api()` only keeps the message):
 * the render answers 409 `head_moved` when the caller's revision is no longer the head. */
async function post<T>(path: string, body: string): Promise<T> {
  const answer = await apiRaw(path, { method: "POST", body });
  if (answer.status >= 200 && answer.status < 300) return answer.body as T;
  const payload = (answer.body && typeof answer.body === "object" ? answer.body : {}) as { detail?: unknown; message?: unknown; code?: unknown };
  const text = typeof payload.detail === "string" ? payload.detail : typeof payload.message === "string" ? payload.message : `Local sidecar returned ${answer.status}`;
  throw Object.assign(new Error(text), { status: answer.status, code: typeof payload.code === "string" ? payload.code : undefined });
}

/** One request in flight per path, body and head: identical calls share it, a new head starts a new one. */
function request<T>(path: string, body: string, headId?: string): Promise<T> {
  const key = pendingKey(path, body, headId);
  const existing = pending.get(key);
  if (existing) return existing as Promise<T>;
  const promise = post<T>(path, body).finally(() => pending.delete(key));
  pending.set(key, promise);
  return promise;
}

function token(value: Value) { return JSON.stringify(value); }

function formatted(visual: Visual, row: number, column: number) {
  return cellText(visual, row, column);
}

/** What the visual bodies can do beyond drawing: data-point menus, report-page tooltips and actions. All are
 * optional: a tooltip page or a read-only preview draws without any of them. */
type PointHandlers = {
  menu?: (visual: Visual, row: number, column: number | null, pointer: DatumPointer) => void;
  hover?: (visual: Visual, row: number | null, pointer: DatumPointer) => void;
  hasMenu?: (visual: Visual) => boolean;
  run?: (visual: Visual, action: Action) => void;
  actionState?: (visual: Visual) => { disabled: boolean; reason?: string };
  /** A small-multiple panel reports its points as the whole visual's, with the panel's row mapped back. */
  selectSource?: Visual; rowOf?: (row: number) => number;
};

const pointerOf = (event: { clientX: number; clientY: number }): DatumPointer => ({ clientX: event.clientX, clientY: event.clientY });

/** A table from the `table` token (fonts, banding, grid, per-column alignment, width and colors), the server's
 * conditional colors, icons and data bars per cell, and the totals row. `surface` is the opaque color the
 * sticky header and totals paint over scrolled rows. */
function DataTable({ visual, surface, points }: { visual: Visual; surface?: string; points?: PointHandlers }) {
  const [sort, setSort] = useState<{ index: number; descending: boolean }>();
  const table = visual.formatting?.table;
  const view = tableView(table, visual.formatting?.tableGrid);
  const legacyMinimal = !table && visual.formatting?.tableGrid === false;
  const columns = visual.columns.map((column, index) => ({ ...column, index })).filter(column => !column.hidden);
  const barRanges = useMemo(() => visual.columns.map((column, index) => column.dataBars ? dataBarRange(visual, index) : { min: 0, max: 0 }), [visual]);
  const rows = useMemo(() => {
    const indices = visual.rows.map((_, i) => i);
    if (!sort) return indices;
    return indices.sort((a, b) => {
      const x = visual.rows[a][sort.index], y = visual.rows[b][sort.index];
      const order = typeof x === "number" && typeof y === "number" ? x - y : display(x).localeCompare(display(y));
      return sort.descending ? -order : order;
    });
  }, [visual.rows, sort]);
  const cellBorders: CSSProperties = table ? { borderTop: view.horizontal ? `${view.horizontalWeight}px solid ${view.horizontal}` : "none", borderLeft: view.vertical ? `${view.verticalWeight}px solid ${view.vertical}` : "none", paddingTop: view.rowPadding, paddingBottom: view.rowPadding } : {};
  const columnToken = (c: { query_ref?: string | null }) => table?.columns?.[c.query_ref ?? ""];
  const alignOf = (c: { query_ref?: string | null; alignment?: string }) => { const a = columnToken(c)?.alignment || c.alignment; return a === "center" || a === "left" || a === "right" ? a : undefined; };
  const widthOf = (c: { query_ref?: string | null }) => { const w = columnToken(c)?.width; return typeof w === "number" ? { width: w, minWidth: w } : {}; };
  const totals = totalsView(visual, columns.map(c => c.index));
  const menu = points?.menu && points.hasMenu?.(visual) ? points.menu : undefined;
  const hover = visual.tooltipPage && points?.hover ? points.hover : undefined;
  const wraps = [table && view.headerWrap ? "wrap-headers" : "", table && view.valuesWrap ? "wrap-values" : ""].filter(Boolean).join(" ") || undefined;
  return <div className={`live-table-scroll${legacyMinimal ? " minimal-grid" : ""}${table ? " token-table" : ""}`} style={table ? view.cell : undefined} role="region" tabIndex={0} aria-label={`${visual.title} data`}><table className={wraps} style={table ? { fontSize: view.cell.fontSize, fontFamily: view.cell.fontFamily } : undefined}><thead><tr>{columns.map(c => <th key={c.index} aria-sort={sort?.index === c.index ? sort.descending ? "descending" : "ascending" : "none"} style={table ? { ...view.header, backgroundColor: view.header.backgroundColor ?? surface, ...widthOf(c), textAlign: columnToken(c)?.styleHeader ? alignOf(c) : view.header.textAlign ?? alignOf(c) } : undefined}><button aria-label={c.label.trim() ? undefined : `Sort by ${c.name}`} onClick={() => setSort({ index: c.index, descending: sort?.index === c.index ? !sort.descending : false })}>{c.label || c.name}{sort?.index === c.index ? sort.descending ? " ↓" : " ↑" : ""}</button></th>)}</tr></thead><tbody>{rows.map((i, position) => <tr key={i} style={table ? { backgroundColor: position % 2 === 0 ? view.banding.primary : view.banding.secondary, color: position % 2 === 0 ? undefined : view.banding.secondaryColor } : undefined}
    onContextMenu={menu ? event => { event.preventDefault(); menu(visual, i, null, pointerOf(event)); } : undefined}
    onPointerEnter={hover ? event => hover(visual, i, pointerOf(event)) : undefined} onPointerLeave={hover ? event => hover(visual, null, pointerOf(event)) : undefined}>{columns.map(c => {
    const raw = visual.rows[i][c.index];
    const bars = c.dataBars;
    const look = cellLook(visual, i, c.index);
    const text = formatted(visual, i, c.index);
    const style: CSSProperties = { ...cellBorders, ...widthOf(c), color: look.color, textAlign: alignOf(c) };
    if (look.background) style.backgroundColor = look.background;
    let content: ReactNode = bars?.hideText && raw == null ? "" : text;
    if (bars && typeof raw === "number") {
      const bar = dataBarGeometry(raw, bars, barRanges[c.index]);
      content = <div className="live-data-bar" title={text} aria-label={text}>{bar.axis !== null && <span className="live-data-bar-axis" style={{ left: `${bar.axis}%`, background: safeColor(bars.axis) }} />}<span className="live-data-bar-fill" style={{ left: `${bar.left}%`, width: `${bar.width}%`, background: bar.color }} />{!bars.hideText && <span className="live-data-bar-text">{text}</span>}</div>;
    }
    if (look.icon) {
      const icon = <CfIcon name={look.icon.name} />;
      content = <span className={`cf-cell cf-${look.icon.alignment}`}>{look.icon.layout === "after" ? <>{content}{icon}</> : look.icon.layout === "only" ? <span title={text} aria-label={text}>{icon}</span> : <>{icon}{content}</>}</span>;
    }
    return <td key={c.index} className={typeof raw === "number" ? "numeric" : undefined} style={style}>{content}</td>;
  })}</tr>)}</tbody>{totals && <tfoot><tr className="live-table-total" style={{ ...totals.style, backgroundColor: totals.style.backgroundColor ?? surface }}>{totals.cells.map(cell => {
    const c = visual.columns[cell.index];
    return <td key={cell.index} className={cell.numeric ? "numeric" : undefined} style={{ ...cellBorders, ...widthOf(c), textAlign: alignOf(c) }}>{cell.text}</td>;
  })}</tr></tfoot>}</table>{!rows.length && <p className="live-empty">No rows for these selections.</p>}</div>;
}

/** A matrix with rows: Power BI's stepped layout (row groups in one indented column). The server frames
 * matrices today; this draws one as soon as a payload carries its rows. */
function MatrixTable({ visual, surface }: { visual: Visual; surface?: string }) {
  const { groups, values, rows } = useMemo(() => steppedRows(visual), [visual]);
  const view = tableView(visual.formatting?.table, visual.formatting?.tableGrid);
  const header = { ...view.header, backgroundColor: view.header.backgroundColor ?? surface };
  return <div className="live-table-scroll token-table live-matrix-stepped" style={view.cell} role="region" tabIndex={0} aria-label={`${visual.title} data`}><table><thead><tr>
    <th style={header}>{groups.map(g => visual.columns[g].label).join(" / ")}</th>{values.map(v => <th key={v} style={header}>{visual.columns[v].label}</th>)}
  </tr></thead><tbody>{rows.map(line => <tr key={line.key} className={line.row === null ? "matrix-parent" : undefined}>
    <td style={{ paddingLeft: 10 + line.level * 16 }}>{line.label}</td>
    {values.map(v => <td key={v} className="numeric">{line.row === null ? "" : formatted(visual, line.row, v)}</td>)}
  </tr>)}</tbody></table></div>;
}

function referenceValue(visual: Visual, index: number) {
  const raw = visual.rows[0][index];
  if (typeof raw === "number" && Math.abs(raw) >= 1000 && !visual.columns[index].format?.includes("%")) {
    return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(raw);
  }
  if (typeof raw === "number" && !visual.columns[index].format) return raw.toLocaleString("en-US", { maximumFractionDigits: 2 });
  return formatted(visual, 0, index).replace(/^[\s‎‏]+/, "");
}

function ReportCard({ visual, points }: { visual: Visual; points?: PointHandlers }) {
  if (!visual.rows.length) return <span>No value</span>;
  const settings = visual.formatting;
  const indices = visual.columns.map((c, i) => c.hidden ? -1 : i).filter(i => i >= 0);
  const value = settings?.cardValue;
  const colors = cardColors(visual);
  const valueStyle = { ...cardValueStyle(value, settings?.fontSize), ...(colors.value ? { color: colors.value } : {}) };
  const gauge = safeImage(settings?.image);
  const menu = points?.menu && points.hasMenu?.(visual) ? points.menu : undefined;
  return <div className={`original-live-values${settings ? " formatted-card" : ""}`}>
    {(value ? value.show !== false : settings?.showValue !== false) && indices.map(i => <div key={i} style={{ textAlign: valueStyle.textAlign }} onContextMenu={menu ? event => { event.preventDefault(); menu(visual, 0, i, pointerOf(event)); } : undefined}><strong style={valueStyle} title={display(visual.rows[0][i])}>{formatted(visual, 0, i)}</strong>{indices.length > 1 && <small>{visual.columns[i].label}</small>}</div>)}
    {gauge && <img className="live-card-gauge" src={gauge} alt="Measure comparison graphic" />}
    {!!settings?.references?.length && <div className="live-card-references">{settings.references.map(reference => <div key={reference.index}><span>{reference.label}{referenceValue(visual, reference.index)}</span>{reference.detailIndex != null && <span>{referenceValue(visual, reference.detailIndex)}</span>}</div>)}</div>}
  </div>;
}

/** Column, bar, line, area, pie and donut: the spec comes from the payload's tokens (reportRender.buildChart),
 * so marks take the report's own colors, conditional colors, highlights and analytics lines. A click selects the
 * category (a second click on the only selected one clears it); a right-click opens the data point's menu. */
function Chart({ visual, theme, onSelect, disabled, compact = false, points, valueDomain }: { visual: Visual; theme?: Theme; onSelect: (id: string, values: Value[]) => void; disabled: boolean; compact?: boolean; points?: PointHandlers; valueDomain?: [number, number] }) {

  const [table, setTable] = useState(false);

  const category = categoryColumn(visual);

  const numeric = useMemo(() => valueColumns(visual), [visual]);

  const built = useMemo(() => buildChart(visual, theme, { tooltip: !visual.tooltipPage, valueDomain }), [visual, theme, valueDomain]);

  const selected = visual.selected;

  const click = useCallback((datum: Record<string, unknown>) => {

    if (disabled || !Object.hasOwn(datum, "category")) return;

    const value = datum.category as Value;

    onSelect(visual.id, selected.length === 1 && token(selected[0]) === token(value) ? [] : [value]);

  }, [disabled, onSelect, visual.id, selected]);

  const source = points?.selectSource ?? visual;
  const menu = points?.menu && points.hasMenu?.(source) ? points.menu : undefined;
  const hover = source.tooltipPage && points?.hover ? points.hover : undefined;
  const onMenu = useMemo(() => menu ? (datum: Record<string, unknown>, pointer: DatumPointer) => { if (typeof datum.row === "number") menu(source, points?.rowOf?.(datum.row) ?? datum.row, null, pointer); } : undefined, [menu, source, points]);
  const onHover = useMemo(() => hover ? (datum: Record<string, unknown> | null, pointer: DatumPointer) => hover(source, datum && typeof datum.row === "number" ? points?.rowOf?.(datum.row) ?? datum.row : null, pointer) : undefined, [hover, source, points]);

  if (category < 0 || !numeric.length || !built) return <DataTable visual={visual} points={points} />;

  return <>{!compact && <div className="live-chart-controls"><label>Filter by {visual.columns[category].label}<select aria-label={`Filter ${visual.title}`} disabled={disabled} value={visual.selected.length ? token(visual.selected[0]) : ""} onChange={e => onSelect(visual.id, e.target.value ? [JSON.parse(e.target.value) as Value] : [])}><option value="">All</option>{[...new Map([...visual.rows.map(r => r[category]), ...visual.selected].map(v => [token(v), v])).values()].map(v => <option key={token(v)} value={token(v)}>{display(v)}</option>)}</select></label><button className="secondary" aria-pressed={table} onClick={() => setTable(!table)}>{table ? "Show chart" : "Show data"}</button></div>}{table ? <DataTable visual={visual} points={points} /> : <div className="live-chart"><PreviewChart spec={built.spec} config={built.config} label={visual.title} formatString={built.formatString} onDatumClick={click} onDatumContextMenu={onMenu} onDatumHover={onHover} /></div>}</>;

}

/** A chart split by a field (contract section 21): one small chart per panel, sharing the value axis. */
function SmallMultiples({ visual, theme, onSelect, disabled, points }: { visual: Visual; theme?: Theme; onSelect: (id: string, values: Value[]) => void; disabled: boolean; points?: PointHandlers }) {
  const panels = useMemo(() => smallMultiplePanels(visual), [visual]);
  const domain = useMemo(() => sharedDomain(panels, theme), [panels, theme]);
  const grid = panelGrid(panels.length, visual.formatting?.smallMultiples?.layout);
  // A panel's datum row is its own index: map it back to the visual's row for menus and tooltips.
  const handlers = useMemo(() => panels.map(panel => points ? {
    ...points, selectSource: visual,
    rowOf: (row: number) => { const rows = visual.smallMultiples?.panels.find(p => token(p.value) === token(panel.value))?.rows ?? []; return rows.filter(r => Number.isInteger(r) && r >= 0 && r < visual.rows.length)[row] ?? row; },
  } : undefined), [panels, points, visual]);
  return <div className="live-small-multiples" style={{ gridTemplateColumns: `repeat(${grid.columns}, minmax(0, 1fr))`, gridTemplateRows: `repeat(${grid.rows}, minmax(0, 1fr))` }}>
    {panels.map((panel, index) => <section key={panel.visual.id} className="live-small-multiple" aria-label={`${visual.title}: ${panel.label}`}>
      <h4>{panel.label}</h4>
      <Chart visual={{ ...panel.visual, id: visual.id }} theme={theme} onSelect={onSelect} disabled={disabled} compact points={handlers[index]} valueDomain={domain} />
    </section>)}
  </div>;
}

function Slicer({ visual, onSelect, onSearch, disabled }: { visual: Visual; onSelect: (id: string, values: Value[]) => void; onSearch: (id: string, value: string) => void; disabled: boolean }) {

  const [search, setSearch] = useState("");

  const at = Math.max(0, visual.columns.findIndex(c => !c.hidden));

  const values = [...new Map([...visual.rows.map(r => r[at]), ...visual.selected].map(v => [token(v), v])).values()];

  const f = visual.formatting;
  const isSelected = (v: Value) => visual.selected.some(selected => token(selected) === token(v));
  const itemStyle = fontStyle(f?.slicer?.items);
  if (f?.slicerStyle === "tiles" && !visual.truncated) return <div className="live-slicer-tiles" style={slicerGridStyle(f)} role="group" aria-label={`Select ${visual.title}`}>{values.map(v => <button key={token(v)} type="button" disabled={disabled} aria-pressed={isSelected(v)} style={slicerTileStyle(f, isSelected(v))} onClick={() => onSelect(visual.id, isSelected(v) && visual.selected.length === 1 ? [] : [v])}>{display(v)}</button>)}</div>;
  if (f?.slicer?.mode === "basic" && !visual.truncated) return <div className="live-slicer-list" role="group" aria-label={`Select ${visual.title}`}>{values.map(v => <button key={token(v)} type="button" className="live-slicer-item" disabled={disabled} aria-pressed={isSelected(v)} style={itemStyle} onClick={() => onSelect(visual.id, isSelected(v) ? visual.selected.filter(s => token(s) !== token(v)) : [...visual.selected, v])}><span className="live-slicer-check" aria-hidden="true">{isSelected(v) ? "✓" : ""}</span>{display(v)}</button>)}</div>;
  return <div className="live-slicer"><label>{visual.columns[at]?.label || visual.title}<select aria-label={`Select ${visual.title}`} style={itemStyle} disabled={disabled} value={visual.selected.length === 1 ? token(visual.selected[0]) : ""} onChange={e => onSelect(visual.id, e.target.value ? [JSON.parse(e.target.value) as Value] : [])}><option value="">{visual.selected.length > 1 ? `${visual.selected.length} saved selections` : "All"}</option>{values.map(v => <option key={token(v)} value={token(v)}>{display(v)}</option>)}</select></label>{(visual.truncated || search) && <form onSubmit={e => { e.preventDefault(); onSearch(visual.id, search); }}><input aria-label={`Search ${visual.title} options`} placeholder="Find an option…" value={search} onChange={e => setSearch(e.target.value)} /><button className="secondary" disabled={disabled}>Find</button></form>}{visual.truncated && <small>First 100 options · Search to find more</small>}</div>;

}

type BodyInput = {
  visual: Visual | undefined; rect: Rect; caption: string; framed: boolean; note: FidelityNote | null; theme?: Theme; editing: boolean;
  disabled: boolean; resetKey: number; onSelect: (id: string, values: Value[]) => void; onSearch: (id: string, value: string) => void;
  points?: PointHandlers; surface: string;
};

/** What goes inside a visual's box, by kind. Shared by the page canvas and the tooltip-page preview. */
function visualBody({ visual, rect, caption, framed, note, theme, editing, disabled, resetKey, onSelect, onSearch, points, surface }: BodyInput): ReactNode {
  if (!visual) return <span className="original-live-message">{disabled ? "Loading…" : "Preview unavailable"}</span>;
  if (framed && note) return <FidelityFrame visual={visual} note={note} />;
  if (visual.error) return <span className="original-live-message">{visual.error}</span>;
  const formatting = visual.formatting;
  const run = !editing && points?.run && visual.action ? () => points.run?.(visual, visual.action as Action) : undefined;
  const state = visual.action ? points?.actionState?.(visual) ?? actionState(visual.action, false) : { disabled: false };
  // A shape or picture with an action is a button in view mode.
  const actionArea = (child: ReactNode) => run
    ? <button type="button" className="report-action-area" aria-label={visual.action?.tooltip || caption} title={state.reason ?? visual.action?.tooltip ?? undefined} aria-disabled={state.disabled || undefined} onClick={() => { if (!state.disabled) run(); }}>{child}</button>
    : child;
  switch (visual.kind) {
    case "card": return <ReportCard visual={visual} points={points} />;
    case "slicer": return <Slicer key={resetKey} visual={visual} onSelect={onSelect} onSearch={onSearch} disabled={disabled} />;
    case "table": return <DataTable visual={visual} surface={surface} points={points} />;
    case "matrix": return visual.rows.length && visual.columns.length ? <MatrixTable visual={visual} surface={surface} /> : <DataTable visual={visual} surface={surface} points={points} />;
    case "textbox": return <TextboxBody paragraphs={formatting?.content ?? []} />;
    case "shape": return actionArea(<ShapeBody shape={formatting?.shape} box={rect} />);
    case "image": return actionArea(<PictureBody src={formatting?.picture?.src} scaling={formatting?.picture?.scaling} label={caption} />);
    case "button": return <ButtonBody visual={visual} label={caption} disabled={state.disabled} reason={state.reason} onRun={editing ? undefined : run} />;
    case "navigator": return <NavigatorBody visual={visual} onRun={editing || !points?.run ? undefined : action => points.run?.(visual, action)} />;
  }
  if (visual.smallMultiples?.panels?.length) return <SmallMultiples visual={visual} theme={theme} onSelect={onSelect} disabled={disabled} points={points} />;
  return <Chart visual={visual} theme={theme} onSelect={onSelect} disabled={disabled} compact points={points} />;
}

function CanvasOptions({ zoom, onZoom, loading, canReset, onRefresh, onReset, phone, onPhone, bookmarks, bookmark, onBookmark }: {

  zoom: string; onZoom: (value: string) => void; loading: boolean; canReset: boolean;

  onRefresh: () => void; onReset: () => void;

  /** The page has a phone layout: offer the phone view. */
  phone?: { available: boolean; on: boolean }; onPhone?: (on: boolean) => void;

  bookmarks?: Bookmark[]; bookmark?: string; onBookmark?: (name: string | null) => void;

}) {

  const [open, setOpen] = useState(false);

  const root = useRef<HTMLDivElement>(null);

  const trigger = useRef<HTMLButtonElement>(null);

  const panel = useRef<HTMLDivElement>(null);

  const panelId = useId();

  useEffect(() => {

    if (!open) return;

    panel.current?.querySelector<HTMLElement>("button:not(:disabled), select")?.focus();

    const dismiss = (event: PointerEvent) => {

      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);

    };

    const escape = (event: KeyboardEvent) => {

      if (event.key === "Escape") { event.preventDefault(); setOpen(false); trigger.current?.focus(); }

    };

    document.addEventListener("pointerdown", dismiss);

    document.addEventListener("keydown", escape);

    return () => { document.removeEventListener("pointerdown", dismiss); document.removeEventListener("keydown", escape); };

  }, [open]);

  const run = (action: () => void) => { setOpen(false); trigger.current?.focus(); action(); };

  return <div className="canvas-options" ref={root} onBlurCapture={event => { if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}>

    <button type="button" ref={trigger} className="canvas-options-trigger" aria-label="Report options" title="Report options" aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? panelId : undefined} onClick={() => setOpen(value => !value)}>{loading ? <Loader2 size={16} className="spin" /> : <Ellipsis size={18} />}</button>

    {open && <div className="canvas-options-panel" id={panelId} ref={panel} role="dialog" aria-label="Report options">

      <button type="button" disabled={loading} onClick={() => run(onRefresh)}><RefreshCw size={14} />Refresh visuals</button>

      <button type="button" disabled={loading || !canReset} onClick={() => run(onReset)}><RotateCcw size={14} />Reset filters</button>

      {phone?.available && onPhone && <button type="button" aria-pressed={phone.on} onClick={() => run(() => onPhone(!phone.on))}>{phone.on ? <Monitor size={14} /> : <Smartphone size={14} />}{phone.on ? "Desktop layout" : "Phone layout"}</button>}

      {!!bookmarks?.length && onBookmark && <label>Bookmark<select aria-label="View the page as a bookmark" value={bookmark ?? ""} onChange={event => { const name = event.target.value; run(() => onBookmark(name || null)); }}><option value="">None</option>{bookmarks.map(item => <option key={item.name} value={item.name}>{item.displayName || item.name}</option>)}</select></label>}

      <label>Zoom<select aria-label="Original layout zoom" value={zoom} onChange={event => onZoom(event.target.value)}><option value="readable">Readable</option><option value="fit">Fit width</option><option value="0.75">75%</option><option value="1">100%</option></select></label>

    </div>}

    <span className="canvas-live-status" role="status">{loading ? "Updating report visuals" : "Report ready"}</span>

  </div>;

}

/** Props of the scaled page shared by the canvas and its tests. */
export type OriginalCanvasProps = {
  layout: OriginalLayout; pageStyle?: PageStyle; theme?: Theme; visuals: Visual[]; mode: CanvasMode; selectedVisualId?: string; onInspect?: (id: string) => void;
  onSelectVisual?: (id: string | null, meta: SelectMeta) => void; overlay?: (context: OverlayContext) => ReactNode; layoutOverride?: Record<string, Rect>;
  onSelect: (id: string, values: Value[]) => void; onSearch: (id: string, value: string) => void;
  disabled: boolean; resetKey: number; loading: boolean; canReset: boolean;
  onRefresh: () => void; onReset: () => void;
  /** Round 3: menus, tooltips and actions of the visuals (view mode). */
  points?: PointHandlers;
  /** The page's phone layout (contract section 20) and whether the phone view is on. */
  mobile?: MobileLayout | null; phone?: boolean; onPhone?: (on: boolean) => void;
  bookmarks?: Bookmark[]; bookmark?: string; onBookmark?: (name: string | null) => void;
};

/** The scaled page: visuals by rank, the optional overlay above them. Exported for the render tests. */
export function OriginalCanvas({ layout, pageStyle, theme, visuals, mode, selectedVisualId, onInspect, onSelectVisual, overlay, layoutOverride, onSelect, onSearch, disabled, resetKey, loading, canReset, onRefresh, onReset, points, mobile, phone = false, onPhone, bookmarks, bookmark, onBookmark }: OriginalCanvasProps) {

  const viewport = useRef<HTMLDivElement>(null);

  const canvas = useRef<HTMLDivElement>(null);

  const [width, setWidth] = useState(960);

  const [zoom, setZoom] = useState("readable");

  useEffect(() => {

    if (!viewport.current) return;

    const observer = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)));

    observer.observe(viewport.current);

    return () => observer.disconnect();

  }, []);

  const phoneView = useMemo(() => phone ? phoneLayout(mobile, visuals) : null, [phone, mobile, visuals]);

  const box = phoneView ?? layout;

  const fit = Math.max(.15, Math.min(1, (width - 24) / box.width));

  const scale = zoom === "fit" ? fit : zoom === "readable" ? Math.max(.5, fit) : Number(zoom);

  // The phone view is a preview: nothing is picked or moved on it.
  const editing = mode === "edit" && !phoneView;

  const byId = useMemo(() => new Map(visuals.map(v => [v.id, v])), [visuals]);

  const titles = useMemo(() => new Map(layout.visuals.map(v => [v.id, v.title])), [layout.visuals]);

  // Hit testing and the overlay work from these rectangles: the imported layout is the fallback, so
  // selection does not wait for the render.
  const rects: OverlayRect[] = useMemo(() => phoneView ? phoneView.rects : layoutRects(layout.visuals, visuals, layoutOverride), [phoneView, layout.visuals, visuals, layoutOverride]);

  const selected = selectedVisualId ? byId.get(selectedVisualId) : undefined;

  const category = selected ? categoryColumn(selected) : -1;

  const toPage = useCallback((clientX: number, clientY: number) => {
    const bounds = canvas.current?.getBoundingClientRect();
    return bounds ? clientToPage(bounds, box, scale, clientX, clientY) : { x: 0, y: 0 };
  }, [box, scale]);

  const meta = (event: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }): SelectMeta => ({ altKey: event.altKey, ctrlKey: event.ctrlKey || event.metaKey, shiftKey: event.shiftKey });

  // In edit mode the visuals are inert and the canvas picks by rank at the pointer: the topmost visible
  // visual, Alt to reach the one underneath, empty canvas to clear.
  const pick = (event: ReactPointerEvent) => {
    if (event.button !== 0 || !onSelectVisual) return;
    onSelectVisual(hitTest(rects, toPage(event.clientX, event.clientY), { altKey: event.altKey, selectedId: selectedVisualId }), meta(event));
  };

  // The keyboard's menu key (or Shift+F10) opens the menu of the focused visual's selected data point.
  const menuRef = useRef(points);
  useEffect(() => { menuRef.current = points; });
  useEffect(() => {
    const node = viewport.current;
    if (!node || mode === "edit") return;
    const onKey = (event: KeyboardEvent) => {
      if (!(event.key === "ContextMenu" || (event.shiftKey && event.key === "F10"))) return;
      const host = event.target instanceof Element ? event.target.closest("[data-visual-id]") : null;
      const visual = host ? byId.get(host.getAttribute("data-visual-id") ?? "") : undefined;
      const handlers = menuRef.current;
      if (!host || !visual || !handlers?.menu || !handlers.hasMenu?.(visual)) return;
      const row = visual.kind === "card" ? 0 : visual.selected.length ? rowOfCategory(visual, visual.selected[0]) : -1;
      if (row < 0 || !visual.rows[row]) return;
      event.preventDefault();
      const bounds = host.getBoundingClientRect();
      handlers.menu(visual, row, null, { clientX: bounds.left + 24, clientY: bounds.top + 24 });
    };
    node.addEventListener("keydown", onKey);
    return () => node.removeEventListener("keydown", onKey);
  }, [byId, mode]);

  const fade = editing ? !!disabled && !loading : disabled;

  return <>

    {!editing && !phoneView && selected && !selected.error && category >= 0 && <fieldset className="original-selected-controls"><legend>{selected.title}</legend>{selected.kind === "slicer" ? <Slicer key={`${selected.id}:${resetKey}`} visual={selected} onSelect={onSelect} onSearch={onSearch} disabled={disabled} /> : <label>Filter by {selected.columns[category].label}<select aria-label={`Filter selected ${selected.title}`} disabled={disabled} value={selected.selected.length ? token(selected.selected[0]) : ""} onChange={event => onSelect(selected.id, event.target.value ? [JSON.parse(event.target.value) as Value] : [])}><option value="">All</option>{[...new Map([...selected.rows.map(row => row[category]), ...selected.selected].map(value => [token(value), value])).values()].map(value => <option key={token(value)} value={token(value)}>{display(value)}</option>)}</select></label>}</fieldset>}

    <div className="original-canvas-frame">

    <CanvasOptions zoom={zoom} onZoom={setZoom} loading={loading} canReset={canReset} onRefresh={onRefresh} onReset={onReset}
      phone={mobile ? { available: !!phoneLayout(mobile, visuals), on: !!phoneView } : undefined} onPhone={onPhone} bookmarks={bookmarks} bookmark={bookmark} onBookmark={onBookmark} />

    {phoneView && <p className="report-phone-note" role="status"><Smartphone size={13} aria-hidden="true" />Phone layout{mode === "edit" ? ": a preview. Switch back to the desktop layout to edit the page." : ""}</p>}

    <div ref={viewport} className={`original-live-scroll${phoneView ? " phone-view" : ""}`} {...(editing ? { role: "application", "aria-label": "Report canvas, editing. Tab moves between visuals and Enter selects one.", tabIndex: 0 } : { role: "region", "aria-label": phoneView ? "Report canvas, phone layout" : "Original report canvas", tabIndex: 0 })}>

      <div ref={canvas} className={`original-live-canvas${fade ? " stale" : ""}${editing ? " editing" : ""}`} style={{ width: box.width, height: box.height, zoom: scale, color: safeColor(theme?.foreground) }}>

        {pageLayers(pageStyle).map(layer => <div key={layer.key} className="report-page-layer" aria-hidden="true" style={layer.style} />)}

        <div className="report-visuals" {...(editing ? { role: "listbox", "aria-multiselectable": true, "aria-label": "Visuals on this page", onPointerDown: pick } : {})}>

        {rects.map(rect => {

          const visual = byId.get(rect.id);

          const state = drawState(rect.hidden, editing ? "edit" : "view");

          if (state === "skip") return null;

          const ghost = state === "ghost";

          const formatting = visual?.formatting;

          // A group has no box and no content: its members carry page coordinates. Only a background of its own is drawn.
          if (rect.kind === "group") {
            const background = safeColor(formatting?.container?.background);
            return background ? <div key={rect.id} className="report-group-box" aria-hidden="true" style={{ left: rect.x, top: rect.y, width: rect.width, height: rect.height, zIndex: rect.rank, ...containerStyle(formatting?.container, "group", false), opacity: ghost ? .35 : undefined }} /> : null;
          }

          const tiny = rect.height < 70;
          const title = visual ? titleView(visual) : undefined;
          const hiddenTitle = !!title && !title.show;
          const caption = titles.get(rect.id) ?? visual?.title ?? rect.id;
          const note = visual ? fidelityNote(visual) : null;
          // Power BI only types, a filter Studio cannot compile and matrices are framed calmly, never as a failure.
          const framed = !!visual && !!note && (note.level === "desktop" || !!visual.unsupported_reason || visual.kind === "unsupported" || (visual.fidelity === "none" && !visual.error));
          const tile = visual?.kind === "card" && !framed && !visual.error;
          const isSelected = selectedVisualId === rect.id;
          const container = visual ? effectiveContainer(visual) : formatting?.container;
          const legacy = !container;
          const style: CSSProperties = {
            left: rect.x, top: rect.y, width: rect.width, height: rect.height, zIndex: rect.rank,
            ...(legacy ? { background: safeColor(formatting?.background) ?? (pageStyle?.image ? "transparent" : undefined) } : containerStyle(container, visual?.kind ?? "", tiny)),
            ...(ghost ? { opacity: .35 } : {}),
          };
          const button = visual?.kind === "button" || visual?.kind === "navigator";
          const className = `original-live-visual${isSelected ? " selected" : ""}${tiny ? " compact" : ""}${legacy ? " legacy-visual" : ""}${visual ? ` kind-${visual.kind}` : ""}${visual?.kind === "card" ? " styled-card" : ""}${visual?.kind === "slicer" ? " styled-slicer" : ""}${hiddenTitle ? " hidden-title" : ""}${ghost ? " ghost" : ""}`;

          // A card's caption can take a conditional color (the first row's value at `cardLabel.colorIndex`).
          const labelColor = visual?.kind === "card" ? cardColors(visual).label : undefined;
          const titleStyle = title ? { ...title.style, ...(labelColor ? { color: labelColor } : {}) } : undefined;
          // The title button stays the keyboard-accessible inspect control, drawn from the title token.
          // In edit mode the option itself is the control (the canvas picks), so the title is plain text: no nested button.
          const heading = editing
            ? <span className="original-live-title" style={titleStyle}>{title ? title.text : caption}</span>
            : <button className="original-live-title" style={titleStyle} title={`Inspect ${caption}`} aria-label={`Inspect ${caption}`} aria-pressed={isSelected} onClick={() => onInspect?.(rect.id)}>{title ? title.text : caption}</button>;
          const containerTitle = visual ? containerTitleView(visual) : null;
          const titleRow = containerTitle ? <div className="original-live-container-title" style={containerTitle.style}>{containerTitle.text}</div> : null;

          const surface = opaque(container?.background) ?? safeColor(theme?.background) ?? "#ffffff";
          const body = visualBody({ visual, rect, caption, framed, note, theme, editing, disabled, resetKey, onSelect, onSearch, points: editing ? undefined : points, surface });

          // `inert` in edit mode: slicer selects, table sort buttons and chart filters must be neither clickable nor tab stops there.
          const inner = <>{(!button || !hiddenTitle) && heading}<div className="original-live-content" inert={editing}>{body}</div></>;

          // Edit mode: visuals are options of a listbox that the canvas picks from; view mode: articles that inspect on click.
          const behavior = editing
            ? { role: "option", "aria-selected": isSelected, tabIndex: 0, onKeyDown: (event: ReactKeyboardEvent) => { if (event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); onSelectVisual?.(rect.id, meta(event)); } } }
            : { onClickCapture: (event: { target: EventTarget }) => {
              // The title remains the keyboard-accessible selection control. Leave data
              // interactions (slicers, chart filtering, table sorting, buttons) with their controls.
              if (event.target instanceof Element && !event.target.closest("button, input, select, textarea, a, .vega-embed, .live-table-scroll")) onInspect?.(rect.id);
            } };
          const Frame = editing ? "div" : "article";
          // Honest badges: every approximation in edit mode; in view mode only the numbers' own (dataNotes).
          const badge = note?.level === "approximate" && (editing || note.data);

          return <Frame key={rect.id} className={className} aria-label={ghost ? `${caption} (hidden)` : caption} data-visual-id={rect.id} style={style} {...behavior}>

            {titleRow}

            {tile ? <div className="report-tile" style={{ ...tileStyle(visual?.formatting?.tile?.default, tiny ? CARD_TILE_PADDING_COMPACT : CARD_TILE_PADDING), ...(visual ? cardFill(visual) : {}) }}>{inner}</div> : inner}

            {badge && note && <FidelityBadge note={note} />}

            {ghost && <span className="ghost-caption">Hidden</span>}

          </Frame>;

        })}

        </div>

        {overlay && !phoneView && <div className="report-overlay-layer">{overlay({ scale, page: { width: layout.width, height: layout.height }, rects, toPage })}</div>}

      </div>

    </div></div></>;

}

function cardFill(visual: Visual): CSSProperties {
  const fill = cardColors(visual).fill;
  return fill ? { backgroundColor: fill } : {};
}

/** A report page drawn small and read-only: the report-page tooltip of a hovered data point. */
function MiniPage({ result, width, height, scale }: { result: RenderResult; width: number; height: number; scale: number }) {
  const visuals = sortByRank(result.visuals);
  const rects = layoutRects(undefined, visuals);
  const byId = new Map(visuals.map(v => [v.id, v]));
  const noop = () => undefined;
  return <div className="report-tooltip-page" style={{ width: width * scale, height: height * scale }}>
    <div className="report-tooltip-canvas" style={{ width, height, zoom: scale, color: safeColor(result.theme?.foreground) }}>
      {pageLayers(result.page_style).map(layer => <div key={layer.key} className="report-page-layer" aria-hidden="true" style={layer.style} />)}
      {rects.map(rect => {
        const visual = byId.get(rect.id);
        if (!visual || rect.hidden || rect.kind === "group") return null;
        const title = titleView(visual);
        const note = fidelityNote(visual);
        const framed = !!note && (note.level === "desktop" || !!visual.unsupported_reason || visual.kind === "unsupported" || (visual.fidelity === "none" && !visual.error));
        const container = effectiveContainer(visual);
        const tiny = rect.height < 70;
        return <div key={rect.id} className={`original-live-visual kind-${visual.kind}${tiny ? " compact" : ""}${container ? "" : " legacy-visual"}`} style={{ left: rect.x, top: rect.y, width: rect.width, height: rect.height, zIndex: rect.rank, ...containerStyle(container, visual.kind, tiny) }}>
          {title.show && <span className="original-live-title" style={title.style}>{title.text}</span>}
          <div className="original-live-content">{visualBody({ visual, rect, caption: visual.title, framed, note, theme: result.theme, editing: true, disabled: false, resetKey: 0, onSelect: noop, onSearch: noop, surface: opaque(container?.background) ?? "#ffffff" })}</div>
        </div>;
      })}
    </div>
  </div>;
}

type TooltipState = { key: string; visual: Visual; row: number; pointer: DatumPointer; result?: RenderResult; error?: string };
type MenuState = { title: string; items: MenuItem[]; pointer: DatumPointer };

// Report-page tooltips already drawn, by page, drill values, source and head.
const tooltipCache = new Map<string, RenderResult>();
const TOOLTIP_DELAY = 350;

export type InteractiveReportPageProps = {
  workspaceId: string; pageId: string; active: boolean; onOpenDesktop: () => Promise<void>;
  layout?: OriginalLayout; selectedVisualId?: string; onInspect?: (id: string) => void;
  /** The report's head revision (`report.head`): part of every render request, so a new head fetches again and
   * a response drawn from any other head is dropped. */
  head?: Head;
  /** The render answered 409 head_moved (or echoed another revision): re-read the imported report to get the new head. */
  onHeadMoved?: () => void;
  /** `edit` draws hidden visuals as ghosts, picks visuals by rank on pointer down and keeps the visuals inert. */
  mode?: CanvasMode;
  /** Bump after a Format-pane edit: when only this changed, formatting is re-read without running any query. */
  formattingOnlyKey?: string;
  /** Accepted for the editor's benefit and ignored: a formatting-only read always asks for the whole page,
   * because one edit can re-rank or move rows it did not name (arrange, a group's move or hide) and the
   * route runs no query anyway. */
  formattingOnlyIds?: readonly string[];
  onSelectVisual?: (id: string | null, meta: SelectMeta) => void;
  /** Rendered inside the scaled canvas above the visuals. The layer ignores the pointer: elements that take it must set `pointer-events: auto`. */
  overlay?: (context: OverlayContext) => ReactNode;
  /** Optimistic page rectangles (a drag in flight), applied without refetching. */
  layoutOverride?: Record<string, Rect>;
  onVisualsRendered?: (visuals: Visual[]) => void;
  /** The whole page response (bookmarks, interactions, sync groups, phone layout, drillthroughs, notes) each time it changes. */
  onPageRendered?: (result: RenderResult) => void;
  /** View mode: open another page (a page button or navigator, a bookmark of another page, a drillthrough, Back).
   * The page it opens picks up the bookmark or drill values by itself; `bookmark`/`drill` props override that. */
  onNavigate?: (pageId: string) => void;
  /** A bookmark was applied (by a button, a bookmark navigator or the options menu). Called before any navigation. */
  onBookmark?: (name: string) => void;
  /** A drillthrough was started from this page (a data point's menu or a drillthrough button). Called before `onNavigate(pageId)`. */
  onDrillthrough?: (pageId: string, filter: DrillRequest) => void;
  /** A Back button (or the drill strip's Back). Without it Back returns to the page a drillthrough came from. */
  onBack?: () => void;
  /** Draw the page as this bookmark shows it (contract section 17). */
  bookmark?: string;
  /** Draw this drillthrough or tooltip page for these values, with the source page's context (section 18). */
  drill?: DrillFilter[]; drillSource?: DrillSource;
  /** Items a caller adds to a data point's right-click menu (view mode), after the drillthrough targets. */
  pointActions?: (point: DataPointRef, visual: Visual) => PointAction[];
};

export function InteractiveReportPage({ workspaceId, pageId, active, onOpenDesktop, layout, selectedVisualId, onInspect, head, onHeadMoved, mode = "view", formattingOnlyKey, onSelectVisual, overlay, layoutOverride, onVisualsRendered, onPageRendered, onNavigate, onBookmark, onDrillthrough, onBack, bookmark: bookmarkProp, drill: drillProp, drillSource: drillSourceProp, pointActions }: InteractiveReportPageProps) {

  const [result, setResult] = useState<RenderResult>();

  const [filters, setFilters] = useState<Filters>({});

  const [searches, setSearches] = useState<Record<string, string>>({});

  const [revision, setRevision] = useState(0);

  const [resetKey, setResetKey] = useState(0);

  const [loading, setLoading] = useState(true);

  const [error, setError] = useState("");
  // Waiting for Desktop to load the working copy; the page re-checks on its own.
  const [wait, setWait] = useState<DesktopWait | null>(null);
  const [timedOut, setTimedOut] = useState(false);

  // What an action that opened this page asked for (a bookmark of this page, drill values), unless the props say.
  const [opened] = useState(() => peekView(workspaceId, pageId));
  useEffect(() => { dropView(workspaceId, pageId); }, [workspaceId, pageId]);
  const [bookmark, setBookmark] = useState<string | undefined>(bookmarkProp ?? opened?.bookmark);
  useEffect(() => { if (bookmarkProp !== undefined) setBookmark(bookmarkProp || undefined); }, [bookmarkProp]);
  const drill = drillProp ?? opened?.drill;
  const drillSource = drillSourceProp ?? opened?.drillSource;
  const cameFrom = opened?.from ?? drillSource?.page;

  // Synced slicers share their selection across pages (contract section 19): the group's values apply here too.
  const [synced, setSynced] = useState(() => syncedSelections(workspaceId, pageId));
  const refreshSync = useCallback(() => {
    const now = syncedSelections(workspaceId, pageId);
    setSynced(current => JSON.stringify(current) === JSON.stringify(now) ? current : now);
  }, [workspaceId, pageId]);
  const effective = useMemo(() => ({ ...synced, ...filters }), [synced, filters]);

  const [phone, setPhone] = useState(false);
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [tip, setTip] = useState<TooltipState | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const tipTimer = useRef<number | undefined>(undefined);

  const path = `/v1/workspaces/${workspaceId}/imported-report/pages/${encodeURIComponent(pageId)}/render`;

  const formattingPath = `${path}-formatting`;

  const headId = head?.id;

  // The canvas draws `highlights`, so it asks for cross-highlighting (contract section 23).
  const requestBody = useMemo(() => renderBody({ filters: effective, searches, bookmark, drill, drillSource, highlight: true }), [effective, searches, bookmark, drill, drillSource]);

  const selection = useMemo(() => JSON.stringify(requestBody), [requestBody]);

  // Latest values for the async handlers below: a response is judged against the head at the time it
  // arrives, and formatting merges into the rows drawn now.
  const headRef = useRef(headId);
  const resultRef = useRef(result);
  const movedRef = useRef(onHeadMoved);
  const renderedRef = useRef(onVisualsRendered);
  const pageRenderedRef = useRef(onPageRendered);
  const lastRun = useRef<{ path: string; selection: string; revision: number; key?: string } | undefined>(undefined);

  useEffect(() => {
    headRef.current = headId;
    resultRef.current = result;
    movedRef.current = onHeadMoved;
    renderedRef.current = onVisualsRendered;
    pageRenderedRef.current = onPageRendered;
  });

  useEffect(() => {

    if (!active) return;

    let cancelled = false;

    const previous = lastRun.current;

    lastRun.current = { path, selection, revision, key: formattingOnlyKey };

    // Only the editor's formatting key moved (the head moves with it): the data is unchanged, so skip the queries.
    const formattingOnly = !!previous && previous.path === path && previous.selection === selection && previous.revision === revision && previous.key !== formattingOnlyKey && !!resultRef.current?.visuals.length;

    // Keep a "waiting for Desktop" message up while re-checking, so it does not flicker.
    setLoading(true);

    // runRender drops a response for a head that is no longer current (never merging it). The requests of older
    // heads are cancelled by this effect's cleanup, so whatever reaches these callbacks is the current request.
    void runRender({
      post: request, path, formattingPath, requestBody, headId, formattingOnly,
      head: () => headRef.current, current: () => resultRef.current, cancelled: () => cancelled,
      onResult: next => { setResult(next); setError(""); setWait(null); setTimedOut(false); setLoading(false); },
      onMoved: () => {
        setLoading(false);
        if (movedRef.current) movedRef.current();
        else setError("This report changed while it was loading. Refresh the visuals to see the latest version.");
      },
      onError: reason => {
        const message = reason instanceof Error ? reason.message : "The visuals could not load.";
        setError(message);
        if (needsDesktop(message)) setWait(current => current ?? { since: Date.now(), opened: false });
        setLoading(false);
      },
    });

    return () => { cancelled = true; };

  }, [active, path, formattingPath, requestBody, selection, headId, revision, formattingOnlyKey]);

  useEffect(() => {
    if (!result) return;
    renderedRef.current?.(result.visuals);
    pageRenderedRef.current?.(result);
    // The sync groups describe the whole report: remember them for the pages opened next, and apply a group's
    // selection here if this answer is the first to say which slicers of this page belong to it.
    rememberSyncGroups(workspaceId, result.sync_groups);
    refreshSync();
  }, [result, workspaceId, refreshSync]);

  useEffect(() => {
    if (!active || loading || !wait || !needsDesktop(error)) return;
    const delay = nextCheckIn(wait, Date.now());
    if (delay === null) { setTimedOut(true); return; }
    const timer = window.setTimeout(() => setRevision(r => r + 1), delay);
    return () => window.clearTimeout(timer);
  }, [active, loading, wait, error]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => () => window.clearTimeout(tipTimer.current), []);

  const openDesktop = useCallback(async () => {
    try {
      await onOpenDesktop();
      setTimedOut(false);
      setWait({ since: Date.now(), opened: true });
      setRevision(r => r + 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [onOpenDesktop]);

  const visuals = useMemo(() => result?.visuals ?? [], [result]);
  const byId = useMemo(() => new Map(visuals.map(v => [v.id, v])), [visuals]);

  const select = useCallback((id: string, values: Value[]) => {
    const visual = byId.get(id);
    const group = visual ? sharedGroup(visual) : null;
    setLoading(true);
    if (group) recordSyncSelection(workspaceId, group, values);
    // A synced slicer the report's sync groups list: the group holds the selection, so every page with one of its
    // slicers follows. Otherwise (an answer without `sync_groups`) the page keeps it as its own.
    if (group && id in syncedSelections(workspaceId, pageId)) {
      setFilters(current => { const next = { ...current }; delete next[id]; return next; });
      refreshSync();
    } else setFilters(current => ({ ...current, [id]: values }));
    setRevision(r => r + 1);
  }, [byId, workspaceId, pageId, refreshSync]);

  const search = useCallback((id: string, value: string) => { setLoading(true); setSearches(current => ({ ...current, [id]: value })); setRevision(r => r + 1); }, []);

  const reset = useCallback(() => {
    setLoading(true);
    clearSyncSelections(workspaceId, visuals.map(sharedGroup).filter((group): group is string => !!group));
    setFilters({}); setSearches({}); setBookmark(undefined); setResetKey(k => k + 1); refreshSync(); setRevision(r => r + 1);
  }, [visuals, workspaceId, refreshSync]);

  // --- Actions: navigation, bookmarks, drillthrough, Back, web addresses --------------------------------

  const applyBookmark = useCallback((name: string | null) => {
    if (!name) { setBookmark(undefined); setLoading(true); setRevision(r => r + 1); return; }
    const found = findBookmark(result?.bookmarks, name);
    if (!found) { setNotice("This bookmark is no longer in the report."); return; }
    const plan = bookmarkPlan(found, pageId);
    onBookmark?.(found.name);
    if (plan.page) {
      if (!onNavigate) { setNotice("This bookmark opens another page. Open that page to see it."); return; }
      stashView(workspaceId, plan.page, { bookmark: found.name, from: pageId });
      onNavigate(plan.page);
      return;
    }
    setLoading(true);
    if (plan.resetSelections) { setFilters({}); setSearches({}); setResetKey(k => k + 1); }
    setBookmark(found.name);
    setRevision(r => r + 1);
  }, [result?.bookmarks, pageId, onBookmark, onNavigate, workspaceId]);

  const drillthroughs = result?.drillthroughs;
  const source = useCallback((visualId?: string): DrillSource => ({ page: pageId, ...(visualId ? { visual: visualId } : {}), selections: selectionsOf(effective) }), [pageId, effective]);

  const openDrillthrough = useCallback((target: Drillthrough, values: DrillFilter[], visualId?: string) => {
    if (!values.length) { setNotice("Select a data point to drill through."); return; }
    const req = drillRequest(target, values, source(visualId));
    if (!onNavigate && !onDrillthrough) { setNotice(`Open "${target.title ?? "the drillthrough page"}" to drill through.`); return; }
    stashView(workspaceId, target.page, { drill: req.drill, drillSource: req.drill_source, from: pageId });
    onDrillthrough?.(target.page, req);
    onNavigate?.(target.page);
  }, [source, onNavigate, onDrillthrough, workspaceId, pageId]);

  const goBack = useCallback(() => {
    if (onBack) return onBack();
    if (cameFrom && onNavigate) return onNavigate(cameFrom);
    setNotice("There is no previous page to go back to.");
  }, [onBack, cameFrom, onNavigate]);

  const runAction = useCallback((_visual: Visual, action: Action) => {
    const outcome = actionOutcome(action);
    switch (outcome.kind) {
      case "navigate":
        if (outcome.page === pageId) return;
        if (onNavigate) onNavigate(outcome.page); else setNotice("Page navigation works in the report view.");
        return;
      case "back": return goBack();
      case "bookmark": return applyBookmark(outcome.name);
      case "drillthrough": {
        const target = drillthroughs?.find(item => item.page === outcome.page);
        const values = target ? selectionDrill(target, visuals, effective) : null;
        if (target && values) openDrillthrough(target, values);
        else setNotice("Select a data point on this page to drill through.");
        return;
      }
      case "url": setUrl(outcome.url); return;
      case "message": setNotice(outcome.text); return;
    }
  }, [pageId, onNavigate, goBack, applyBookmark, drillthroughs, visuals, effective, openDrillthrough]);

  const stateOf = useCallback((visual: Visual) => {
    const action = visual.action;
    if (action?.type !== "drillthrough") return actionState(action, true);
    const target = drillthroughs?.find(item => item.page === action.target);
    return actionState(action, !!selectionDrill(target, visuals, effective));
  }, [drillthroughs, visuals, effective]);

  // --- Data point menus and report-page tooltips ------------------------------------------------------

  const hasMenu = useCallback((visual: Visual) => !!pointActions || drillTargets(drillthroughs, visual, pageId).length > 0, [pointActions, drillthroughs, pageId]);

  const openMenu = useCallback((visual: Visual, row: number, column: number | null, pointer: DatumPointer) => {
    window.clearTimeout(tipTimer.current);
    setTip(null);
    const items: MenuItem[] = drillTargets(drillthroughs, visual, pageId).map(target => ({
      label: `Drill through to ${target.title || "page"}`, run: () => openDrillthrough(target, drillFiltersFor(target, visual, row), visual.id),
    }));
    const ref = dataPointRef(visual, row, column);
    for (const extra of pointActions?.(ref, visual) ?? []) items.push({ label: extra.label, run: extra.run, disabled: extra.disabled, hint: extra.hint });
    if (items.length) setMenu({ title: ref.label || visual.title, items, pointer });
  }, [drillthroughs, pageId, openDrillthrough, pointActions]);

  const tipPath = useCallback((page: string) => withQuery(`/v1/workspaces/${workspaceId}/imported-report/pages/${encodeURIComponent(page)}/render`, headId ? [["revision", headId]] : []), [workspaceId, headId]);

  const hover = useCallback((visual: Visual, row: number | null, pointer: DatumPointer) => {
    window.clearTimeout(tipTimer.current);
    const target = visual.tooltipPage;
    if (row === null || !target?.page || menu) { setTip(null); return; }
    const values = tooltipDrill(visual, row);
    const from = { page: pageId, visual: visual.id, selections: selectionsOf(effective) };
    const key = tooltipKey(target.page, values, from, headId);
    // Debounced: a pointer passing over points does not start a render per point; leaving cancels it.
    tipTimer.current = window.setTimeout(() => {
      const cached = tooltipCache.get(key);
      setTip({ key, visual, row, pointer, result: cached });
      if (cached) return;
      const body = JSON.stringify(renderBody({ filters: {}, searches: {}, drill: values, drillSource: from }));
      request<RenderResult>(tipPath(target.page), body, headId).then(answer => {
        if (tooltipCache.size > 40) tooltipCache.delete(tooltipCache.keys().next().value as string);
        tooltipCache.set(key, answer);
        setTip(current => current?.key === key ? { ...current, result: answer } : current);
      }).catch(reason => setTip(current => current?.key === key ? { ...current, error: reason instanceof Error ? reason.message : "The tooltip page could not load." } : current));
    }, TOOLTIP_DELAY);
  }, [menu, pageId, effective, headId, tipPath]);

  const points: PointHandlers | undefined = useMemo(() => mode === "edit" ? undefined : { menu: openMenu, hover, hasMenu, run: runAction, actionState: stateOf }, [mode, openMenu, hover, hasMenu, runAction, stateOf]);

  const slicers = visuals.filter(v => v.kind === "slicer" && !v.error);

  const cards = visuals.filter(v => v.kind === "card" && !v.error);

  const content = visuals.filter(v => v.kind !== "slicer" && v.kind !== "card" || v.error).sort((a, b) => Number(a.kind === "table") - Number(b.kind === "table"));

  const chips = Object.entries(effective).filter(([, values]) => values.length);

  const notes = [...(result?.notes ?? []), ...(result?.bookmark?.notes ?? [])].filter((note, index, all) => typeof note === "string" && note && all.indexOf(note) === index);

  const appliedBookmark = result?.bookmark;

  return <section className={`live-report${layout ? " live-original-report" : ""}`} aria-label={layout ? "Live original report layout" : "Interactive Studio report"} aria-busy={loading}>



    {error && needsDesktop(error) && <div className="live-desktop" role="status">{wait?.opened && !timedOut
      ? <><Loader2 size={18} className="spin" /><p><strong>Opening Power BI Desktop…</strong> Your dashboard appears here as soon as the report has loaded, usually within a minute.</p></>
      : <><p><strong>{timedOut ? "Power BI Desktop has not connected yet." : "This page needs Power BI Desktop."}</strong> {timedOut ? "Check that the report finished loading in Desktop and close any sign-in or update prompts there." : "Studio draws most pages itself, but it cannot reproduce this one exactly. It opens a private working copy of your report in Desktop and shows its live visuals here. Your original file is never changed."}</p><button className="secondary" onClick={() => void openDesktop()}>{timedOut ? "Check again" : "Open in Power BI Desktop"}</button></>}</div>}

    {error && !needsDesktop(error) && <div className="live-error" role="alert"><p>{error}</p><button className="secondary" disabled={loading} onClick={() => setRevision(r => r + 1)}>Try again</button></div>}

    {!layout && !result && !error && <div className="live-loading"><Loader2 size={24} className="spin" /><p>Calculating your report’s visuals…</p></div>}

    {!!drill?.length && <div className="report-drill-strip" role="status"><span>Drillthrough: {describeDrill(drill)}</span>{(onBack || (cameFrom && onNavigate)) && <button type="button" className="secondary" onClick={goBack}><CornerUpLeft size={13} />Back</button>}</div>}

    {appliedBookmark && <div className="report-bookmark-strip" role="status"><span>Showing bookmark <strong>{appliedBookmark.displayName || appliedBookmark.name}</strong></span><button type="button" className="secondary" disabled={loading} onClick={() => applyBookmark(null)}>Clear</button></div>}

    {!!notes.length && <div className="report-page-notes" role="note"><Info size={14} aria-hidden="true" /><ul>{notes.map(note => <li key={note}>{note}</li>)}</ul></div>}

    {notice && <p className="report-notice" role="status">{notice}</p>}

    {!layout && !!slicers.length && <div className="live-slicers">{slicers.map(v => <Slicer key={`${v.id}:${resetKey}`} visual={v} onSelect={select} onSearch={search} disabled={loading} />)}</div>}

    {!!chips.length && <div className="live-filter-chips">{chips.map(([id, values]) => <button className="secondary" key={id} disabled={loading} onClick={() => select(id, [])}>{visuals.find(v => v.id === id)?.title ?? id}: {values.map(display).join(", ")} ×</button>)}</div>}

    {layout ? <ReportPaletteContext.Provider value={result?.theme?.dataColors}><OriginalCanvas layout={layout} pageStyle={result?.page_style} theme={result?.theme} visuals={visuals} mode={mode} selectedVisualId={selectedVisualId} onInspect={onInspect} onSelectVisual={onSelectVisual} overlay={overlay} layoutOverride={layoutOverride} onSelect={select} onSearch={search} disabled={loading || !!error} resetKey={resetKey} loading={loading} canReset={!!chips.length || !!Object.keys(searches).length || !!bookmark} onRefresh={() => setRevision(r => r + 1)} onReset={reset}
      points={points} mobile={result?.mobile} phone={phone} onPhone={setPhone} bookmarks={result?.bookmarks} bookmark={bookmark} onBookmark={applyBookmark} /></ReportPaletteContext.Provider> : <div className={`live-visual-grid${loading || error ? " stale" : ""}`} aria-label={loading || error ? "Previous results; refresh pending" : "Report visuals"}>{!!cards.length && <div className="live-cards">{cards.map(v => <article key={v.id} className="live-visual live-card"><h3>{v.title}</h3>{v.rows.length ? v.columns.map((c, i) => <div key={i}><strong title={String(v.rows[0][i])} className="live-card-value">{display(v.rows[0][i])}</strong>{(v.columns.length > 1 || c.name !== v.title) && <small>{c.name}</small>}</div>) : <p>No value for these selections</p>}</article>)}</div>}{content.map(v => <article key={v.id} className={`live-visual live-${v.kind}`}><h3>{v.title}</h3>{v.error ? <p className="live-unsupported">{v.error}. Use Power BI Desktop for this visual.</p> : v.kind === "card" ? v.rows.length ? v.columns.map((c, i) => <div key={i}><strong className="live-card-value">{display(v.rows[0][i])}</strong>{v.columns.length > 1 && <small>{c.label}</small>}</div>) : <p>No value for these selections</p> : v.kind === "table" || v.kind === "matrix" ? <DataTable visual={v} /> : <Chart visual={v} theme={result?.theme} onSelect={select} disabled={loading} />}{v.truncated && <small className="live-limit">Showing the first 100 results in the report’s sort order. Filter to narrow the results.</small>}</article>)}</div>}

    {menu && typeof document !== "undefined" && createPortal(<PointMenu title={menu.title} items={menu.items} position={floatingPosition({ x: menu.pointer.clientX, y: menu.pointer.clientY }, { width: 240, height: 40 + menu.items.length * 34 }, { width: window.innerWidth, height: window.innerHeight }, 2)} onClose={() => setMenu(null)} />, document.body)}

    {tip && typeof document !== "undefined" && createPortal(<TooltipCard tip={tip} />, document.body)}

    {url && typeof document !== "undefined" && createPortal(<UrlConfirm url={url} onCancel={() => setUrl(null)} onOpen={() => { window.open(url, "_blank", "noopener,noreferrer"); setUrl(null); }} />, document.body)}

  </section>;

}

/** The report page a hovered data point shows as its tooltip, next to the pointer. */
function TooltipCard({ tip }: { tip: TooltipState }) {
  const target = tip.visual.tooltipPage;
  const width = target?.width ?? tip.result?.page?.width ?? 320;
  const height = target?.height ?? tip.result?.page?.height ?? 240;
  const scale = tooltipScale({ width, height });
  const size = tip.result ? { width: width * scale + 2, height: height * scale + 2 } : { width: 220, height: 44 };
  const position = floatingPosition({ x: tip.pointer.clientX, y: tip.pointer.clientY }, size, { width: window.innerWidth, height: window.innerHeight });
  return <div className="report-tooltip-card" role="tooltip" aria-label={target?.title ? `Tooltip: ${target.title}` : "Tooltip"} style={position}>
    {tip.result ? <ReportPaletteContext.Provider value={tip.result.theme?.dataColors}><MiniPage result={tip.result} width={width} height={height} scale={scale} /></ReportPaletteContext.Provider>
      : tip.error ? <span className="report-tooltip-message">{tip.error}</span>
      : <span className="report-tooltip-message"><Loader2 size={13} className="spin" aria-hidden="true" />Loading {target?.title ?? "tooltip"}…</span>}
  </div>;
}
