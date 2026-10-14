// Pure rules behind the interactive canvas (contract sections 16 to 20 and 23): button and navigator
// states, what an action does and when it is disabled, drillthrough targets and filters, report-page
// tooltips, synced slicers, bookmarks, the phone layout and the render request body. No React here.
// Tested in tests/report-round3.test.mjs.
import type { CSSProperties } from "react";

import {
  categoryColumn, cellText, display, fontStyle, safeColor,
  type Action, type Bookmark, type ButtonState, type ButtonToken, type Column, type Drillthrough, type MobileLayout, type OverlayRect,
  type RenderVisual, type SyncMember, type Value,
} from "./reportRender";

type Nullable<T> = T | null | undefined;

export type DrillFilter = { field: string; values: Value[] };
export type Selection = { visual_id: string; values: Value[] };
export type DrillSource = { page: string; visual?: string; selections?: Selection[] };
/** What a drillthrough (or a pending view after navigation) renders the target page with. */
export type DrillRequest = { drill: DrillFilter[]; drill_source?: DrillSource };

export const POWER_BI_ONLY = "This button's action works in Power BI only";

// --- Buttons and navigators (section 16) --------------------------------------------------------------

export type ButtonStateName = "default" | "hover" | "selected" | "disabled" | "press";

/** A button: disabled when disabled, `selected` ("On press") while pressed, hover under the pointer, else default. */
export function buttonStateName(state: { disabled?: boolean; pressed?: boolean; hovered?: boolean }): ButtonStateName {
  if (state.disabled) return "disabled";
  if (state.pressed) return "selected";
  if (state.hovered) return "hover";
  return "default";
}

/** A navigator's item: press, then hover, then selected (its current page or bookmark), else default. */
export function navigatorStateName(state: { pressed?: boolean; hovered?: boolean; selected?: boolean }): ButtonStateName {
  if (state.pressed) return "press";
  if (state.hovered) return "hover";
  if (state.selected) return "selected";
  return "default";
}

/** The state to draw: a styled state arrives complete (the server filled it from default), an unstyled one is null. */
export function stateToken(token: Nullable<ButtonToken>, name: ButtonStateName): ButtonState | undefined {
  return token?.states?.[name] ?? token?.states?.default ?? undefined;
}

function px(value: Nullable<number>): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

const JUSTIFY: Record<string, string> = { left: "flex-start", center: "center", right: "flex-end", top: "flex-start", middle: "center", bottom: "flex-end" };

/** The box of a button (or of a navigator's item) in one state: fill, outline, corner shape, text placement. */
export function buttonBoxStyle(token: Nullable<ButtonToken>, state: Nullable<ButtonState>): CSSProperties {
  const outline = state?.outline;
  const width = outline?.show ? px(outline.width) ?? 1 : 0;
  const shape = token?.shape ?? "rectangle";
  const radius = shape === "oval" ? "50%" : shape === "pill" ? 9999 : shape === "rectangleRounded" || shape === "rectangleRoundedByPixel" ? px(token?.radius) ?? 8 : 0;
  const text = state?.text;
  return {
    backgroundColor: safeColor(state?.fill) ?? "transparent",
    border: width ? `${width}px solid ${safeColor(outline?.color) ?? "#605E5C"}` : "none",
    borderRadius: radius,
    justifyContent: JUSTIFY[text?.alignment ?? "center"] ?? "center",
    alignItems: JUSTIFY[text?.verticalAlignment ?? "middle"] ?? "center",
  };
}

export function buttonTextStyle(state: Nullable<ButtonState>): CSSProperties {
  const text = state?.text;
  return { ...fontStyle(text), textDecoration: text?.underline ? "underline" : undefined };
}

export function navigatorGridStyle(token: Nullable<ButtonToken>, count: number): CSSProperties {
  const layout = token?.layout;
  const gap = px(layout?.cellPadding) ?? 4;
  if (layout?.orientation === "vertical") return { display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gridAutoRows: "minmax(0, 1fr)", gap };
  if (layout?.orientation === "grid") {
    const columns = Math.max(1, Math.floor(px(layout.columns) ?? Math.ceil(Math.sqrt(Math.max(1, count)))));
    return { display: "grid", gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`, gridAutoRows: "minmax(0, 1fr)", gap };
  }
  return { display: "grid", gridTemplateColumns: `repeat(${Math.max(1, count)}, minmax(0, 1fr))`, gridTemplateRows: "minmax(0, 1fr)", gap };
}

// --- Actions -----------------------------------------------------------------------------------------

export type ActionOutcome =
  | { kind: "navigate"; page: string }
  | { kind: "back" }
  | { kind: "bookmark"; name: string }
  | { kind: "drillthrough"; page: string }
  | { kind: "url"; url: string }
  | { kind: "message"; text: string }
  | { kind: "none" };

/** What running an action does. A web address is only ever proposed (the canvas asks before opening it). */
export function actionOutcome(action: Nullable<Action>): ActionOutcome {
  if (!action) return { kind: "none" };
  const target = typeof action.target === "string" && action.target ? action.target : null;
  switch (action.type) {
    case "page": return target ? { kind: "navigate", page: target } : { kind: "none" };
    case "back": return { kind: "back" };
    case "bookmark": return target ? { kind: "bookmark", name: target } : { kind: "none" };
    case "drillthrough": return target ? { kind: "drillthrough", page: target } : { kind: "none" };
    case "url": return typeof action.url === "string" && /^https?:\/\/[^\s]+$/i.test(action.url) ? { kind: "url", url: action.url } : { kind: "none" };
    default: return { kind: "message", text: POWER_BI_ONLY };
  }
}

/** Disabled when the target no longer exists (`valid: false`), and a drillthrough until a data point is selected.
 * A visual with no action, a `qna` or an unknown action is enabled. */
export function actionState(action: Nullable<Action>, drillReady: boolean): { disabled: boolean; reason?: string } {
  if (!action) return { disabled: false };
  if (action.type === "qna" || action.type === "unknown") return { disabled: false };
  if (action.valid === false) return { disabled: true, reason: "The page, bookmark or web address this button points at no longer exists" };
  if (action.type === "drillthrough" && !drillReady) return { disabled: true, reason: "Select a data point on this page to drill through" };
  return { disabled: false };
}

// --- Drillthrough (section 18) ------------------------------------------------------------------------

export function columnRefs(column: Pick<Column, "query_ref" | "table" | "name"> | undefined): string[] {
  if (!column) return [];
  return [column.query_ref ?? undefined, column.table ? `${column.table}.${column.name}` : undefined].filter((ref): ref is string => !!ref);
}

function matching(target: Drillthrough, column: Column): string | undefined {
  const refs = columnRefs(column);
  return target.fields.find(field => refs.includes(field.field))?.field;
}

/** The drillthrough pages offered for a data point of this visual: a page some bound field of which is a column
 * of the row (drawn or hidden). The page itself is never its own target. */
export function drillTargets(drillthroughs: Nullable<readonly Drillthrough[]>, visual: Pick<RenderVisual, "columns">, currentPage?: string): Drillthrough[] {
  return (drillthroughs ?? []).filter(target => target.page !== currentPage && Array.isArray(target.fields) && visual.columns.some(column => matching(target, column)));
}

/** The drill values of one row for a target: every bound field the row has. */
export function drillFiltersFor(target: Drillthrough, visual: Pick<RenderVisual, "columns" | "rows">, row: number): DrillFilter[] {
  const values = visual.rows[row];
  if (!values) return [];
  const out: DrillFilter[] = [];
  visual.columns.forEach((column, index) => {
    const field = matching(target, column);
    if (field && !out.some(item => item.field === field)) out.push({ field, values: [values[index]] });
  });
  return out.slice(0, 24);
}

/** A drillthrough button's drill values from the page's selections: a data point selected in a visual (not a
 * slicer) whose category is a bound field of the target. Null while nothing qualifies (the button is disabled). */
export function selectionDrill(target: Nullable<Drillthrough>, visuals: readonly RenderVisual[], filters: Record<string, Value[]>): DrillFilter[] | null {
  if (!target) return null;
  for (const visual of visuals) {
    const values = filters[visual.id];
    if (!values?.length || visual.kind === "slicer") continue;
    const category = categoryColumn(visual);
    const field = category >= 0 ? matching(target, visual.columns[category]) : undefined;
    if (field) return [{ field, values }];
  }
  return null;
}

export function selectionsOf(filters: Record<string, Value[]>): Selection[] {
  return Object.entries(filters).map(([visual_id, values]) => ({ visual_id, values }));
}

/** The request of a drillthrough: the drill values, and the source page's context when the target keeps all
 * filters (the default; with it off the target takes its bound fields only). */
export function drillRequest(target: Drillthrough, drill: DrillFilter[], source: DrillSource): DrillRequest {
  return target.keepAllFilters === false ? { drill } : { drill, drill_source: source };
}

// --- Report-page tooltips (section 18) ----------------------------------------------------------------

/** The hovered point's category (and series) values as drill filters for its tooltip page. */
export function tooltipDrill(visual: Pick<RenderVisual, "columns" | "rows">, row: number): DrillFilter[] {
  const values = visual.rows[row];
  if (!values) return [];
  const out: DrillFilter[] = [];
  visual.columns.forEach((column, index) => {
    if (out.length >= 2 || column.hidden || column.kind !== "column" || !column.query_ref) return;
    out.push({ field: column.query_ref, values: [values[index]] });
  });
  return out;
}

export function tooltipKey(page: string, drill: readonly DrillFilter[], source: Nullable<DrillSource>, head?: string): string {
  return JSON.stringify([page, drill, source ?? null, head ?? null]);
}

/** The scale a tooltip page is drawn at: its own size, shrunk to fit `limit` pixels. */
export function tooltipScale(size: { width?: number; height?: number }, limit = 400): number {
  const width = px(size.width) ?? 320;
  const height = px(size.height) ?? 240;
  return Math.min(1, limit / Math.max(1, width), limit / Math.max(1, height));
}

/** Where to put a floating card next to the pointer inside a frame, flipped to stay inside it. */
export function floatingPosition(pointer: { x: number; y: number }, size: { width: number; height: number }, frame: { width: number; height: number }, gap = 14): { left: number; top: number } {
  let left = pointer.x + gap;
  let top = pointer.y + gap;
  if (left + size.width > frame.width) left = Math.max(0, pointer.x - gap - size.width);
  if (top + size.height > frame.height) top = Math.max(0, frame.height - size.height);
  return { left, top };
}

// --- Data points (context menu) -------------------------------------------------------------------------

/** One data point of a visual, for the right-click menu and for callers that add their own items. */
export type DataPointRef = { visualId: string; row: number; column: number | null; label: string; values: Record<string, Value> };

export function dataPointRef(visual: Pick<RenderVisual, "id" | "columns" | "rows" | "formatted_rows">, row: number, column: number | null): DataPointRef {
  const values: Record<string, Value> = {};
  visual.columns.forEach((c, index) => { const ref = columnRefs(c)[0]; if (ref) values[ref] = visual.rows[row]?.[index] ?? null; });
  const category = visual.columns.findIndex(c => c.kind === "column" && !c.hidden);
  const parts = [category >= 0 ? cellText(visual, row, category) : "", column !== null && column !== category && visual.rows[row] ? cellText(visual, row, column) : ""].filter(Boolean);
  return { visualId: visual.id, row, column, label: parts.join(" · "), values };
}

/** The row of a chart whose category is the given value (keyboard access to the menu from a selection). */
export function rowOfCategory(visual: Pick<RenderVisual, "columns" | "rows">, value: Value): number {
  const category = categoryColumn(visual);
  if (category < 0) return -1;
  return visual.rows.findIndex(row => JSON.stringify(row[category]) === JSON.stringify(value));
}

// --- Synced slicers (section 19) ----------------------------------------------------------------------

type SyncState = { members: Record<string, SyncMember[]>; values: Record<string, Value[]> };
const syncStates = new Map<string, SyncState>();

function syncOf(workspace: string): SyncState {
  let state = syncStates.get(workspace);
  if (!state) { state = { members: {}, values: {} }; syncStates.set(workspace, state); }
  return state;
}

/** The report's sync groups, from any page response (they describe the whole report). */
export function rememberSyncGroups(workspace: string, groups: Nullable<Record<string, SyncMember[]>>): void {
  if (groups && typeof groups === "object") syncOf(workspace).members = groups;
}

/** The group a slicer shares its selection with: only when `filterChanges` is on (field-only groups keep each
 * page's selection separate). */
export function sharedGroup(visual: Pick<RenderVisual, "syncGroup">): string | null {
  const group = visual.syncGroup;
  return group && typeof group.groupName === "string" && group.filterChanges !== false ? group.groupName : null;
}

export function recordSyncSelection(workspace: string, group: string, values: Value[]): void {
  syncOf(workspace).values[group] = values;
}

export function clearSyncSelections(workspace: string, groups: readonly string[]): void {
  const state = syncOf(workspace);
  for (const group of groups) delete state.values[group];
}

/** The selections a page inherits from its synced slicers: the group's values for every member on the page
 * (a hidden synced slicer still filters its page). */
export function syncedSelections(workspace: string, page: string): Record<string, Value[]> {
  const state = syncOf(workspace);
  const out: Record<string, Value[]> = {};
  for (const [group, values] of Object.entries(state.values)) {
    for (const member of state.members[group] ?? []) if (member.page === page) out[member.visual] = values;
  }
  return out;
}

/** Test hook: forget everything remembered for a workspace. */
export function resetSync(workspace?: string): void {
  if (workspace === undefined) syncStates.clear(); else syncStates.delete(workspace);
}

// --- Pending views: what a page opened by an action renders with ---------------------------------------

export type PendingView = { bookmark?: string; drill?: DrillFilter[]; drillSource?: DrillSource; from?: string };
const pendingViews = new Map<string, PendingView & { at: number }>();
const viewKey = (workspace: string, page: string) => `${workspace}\u0000${page}`;
/** A view no page picked up soon after (the parent did not navigate) must not surprise a later visit. */
export const PENDING_VIEW_MS = 30_000;

export function stashView(workspace: string, page: string, view: PendingView, now = Date.now()): void {
  pendingViews.set(viewKey(workspace, page), { ...view, at: now });
}

export function peekView(workspace: string, page: string, now = Date.now()): PendingView | undefined {
  const found = pendingViews.get(viewKey(workspace, page));
  if (!found || now - found.at > PENDING_VIEW_MS) return undefined;
  const { at: _at, ...view } = found;
  return view;
}

export function dropView(workspace: string, page: string): void {
  pendingViews.delete(viewKey(workspace, page));
}

// --- Bookmarks (section 17) ---------------------------------------------------------------------------

export function findBookmark(bookmarks: Nullable<readonly Bookmark[]>, name: string): Bookmark | undefined {
  return (bookmarks ?? []).find(bookmark => bookmark.name === name) ?? (bookmarks ?? []).find(bookmark => bookmark.displayName === name);
}

/** Applying a bookmark: the page to open first (when it captures the page and belongs to another one), and
 * whether the page's own selections give way to the ones it saved. */
export function bookmarkPlan(bookmark: Bookmark, currentPage: string): { page: string | null; resetSelections: boolean } {
  const other = !!bookmark.page && bookmark.page !== currentPage && bookmark.captures?.page !== false;
  return { page: other ? bookmark.page ?? null : null, resetSelections: bookmark.captures?.data !== false };
}

// --- Phone layout (section 20) ------------------------------------------------------------------------

export const PHONE_WIDTH = 320;

/** The phone canvas: only the visuals placed on it, at their phone positions, stacked by their phone z. */
export function phoneLayout(mobile: Nullable<MobileLayout>, visuals: readonly Pick<RenderVisual, "id" | "kind" | "hidden" | "container_kind" | "parent_group">[]): { width: number; height: number; rects: OverlayRect[] } | null {
  const boxes = mobile?.visuals;
  if (!boxes || typeof boxes !== "object") return null;
  const byId = new Map(visuals.map(visual => [visual.id, visual]));
  const placed = Object.entries(boxes).filter(([id, box]) => byId.has(id) && box && typeof box === "object")
    .map(([id, box], index) => ({ id, box, index }))
    .sort((a, b) => (a.box.z ?? 0) - (b.box.z ?? 0) || a.index - b.index);
  if (!placed.length) return null;
  const rects: OverlayRect[] = placed.map(({ id, box }, rank) => {
    const visual = byId.get(id);
    return {
      id, x: Math.max(0, box.x), y: Math.max(0, box.y), width: Math.max(0, box.width), height: Math.max(0, box.height), rank: rank + 1,
      hidden: !!visual?.hidden, kind: visual?.container_kind === "group" ? "group" : visual?.kind ?? "unknown", parentGroup: visual?.parent_group ?? null,
    };
  });
  const height = Math.max(PHONE_WIDTH, ...rects.map(rect => rect.y + rect.height));
  return { width: PHONE_WIDTH, height, rects };
}

// --- The render request (sections 1 and 23) -----------------------------------------------------------

export type RenderRequestInput = {
  filters: Record<string, Value[]>; searches: Record<string, string>;
  bookmark?: Nullable<string>; drill?: Nullable<DrillFilter[]>; drillSource?: Nullable<DrillSource>;
  /** The canvas draws `highlights`, so it opts into cross-highlighting. */
  highlight?: boolean;
};

export function renderBody(input: RenderRequestInput): Record<string, unknown> {
  const body: Record<string, unknown> = { selections: selectionsOf(input.filters), searches: input.searches };
  if (input.bookmark) body.bookmark = input.bookmark;
  if (input.drill?.length) body.drill = input.drill.slice(0, 24);
  if (input.drillSource) body.drill_source = input.drillSource;
  if (input.highlight) body.highlight = true;
  return body;
}

/** "Region is West, East" for the strip that says what a drillthrough page shows. */
export function describeDrill(drill: readonly DrillFilter[]): string {
  return drill.map(item => `${item.field.split(".").pop() ?? item.field} is ${item.values.map(display).join(", ")}`).join("; ");
}
