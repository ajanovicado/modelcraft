// The editor's round-3 panes as pure rules: what the page render says about bookmarks, layers, interactions, synced
// slicers, the phone layout and the page type (docs/imported-report-render-contract.md, sections 1, 16-20), and the
// operations each pane sends (docs/imported-report-editing-api.md, navigation and extras families). Tested in node.

import type { Operation } from "./importedEditing";
import type { Rect } from "./importedGeometry";

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const str = (value: unknown): string | undefined => typeof value === "string" && value.trim() ? value : undefined;
const strList = (value: unknown): string[] => Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
const num = (value: unknown): number | undefined => typeof value === "number" && Number.isFinite(value) ? value : undefined;

// --- what a formatting render says about the page ---------------------------------------------------------------

export type Bookmark = {
  name: string; displayName: string; group?: { name: string; displayName: string }; page?: string; targets: string[] | null;
  captures: { data: boolean; display: boolean; page: boolean }; hidden: string[]; shown: string[];
};
export type Interaction = "filter" | "highlight" | "none" | "unsupported";
export type SyncMember = { page: string; visual: string; hidden: boolean };
export type Drillthrough = { page: string; title: string; fields: { field: string; table?: string; column?: string }[]; keepAllFilters: boolean; crossReport: boolean };
export type PageType = "default" | "drillthrough" | "tooltip";
export type PageBinding = { type: PageType; fields: { field: string; table?: string; column?: string }[]; keepAllFilters: boolean; crossReport: boolean };
export type Action = { type: string; target?: string; url?: string; valid: boolean; tooltip?: string };
export type LayerVisual = {
  id: string; title: string; kind: string; visualType?: string; rank: number; hidden: boolean; hiddenOwn: boolean; parentGroup?: string;
  children: string[]; position?: Rect; action?: Action; syncGroup?: { groupName: string; fieldChanges: boolean; filterChanges: boolean }; tooltipPage?: string;
};
export type PaneMeta = {
  pageType: PageType; binding?: PageBinding; bookmarks: Bookmark[]; interactions: Record<string, Record<string, Interaction>>;
  syncGroups: Record<string, SyncMember[]>; mobile?: Record<string, Rect & { z?: number }>; drillthroughs: Drillthrough[];
  notes: string[]; visuals: Record<string, LayerVisual>;
};

const readFields = (value: unknown) => Array.isArray(value) ? value.flatMap(item => isRecord(item) && str(item.field) ? [{ field: String(item.field), table: str(item.table), column: str(item.column) }] : []) : [];
const readRect = (value: unknown): Rect | undefined => {
  if (!isRecord(value)) return undefined;
  const x = num(value.x), y = num(value.y), width = num(value.width), height = num(value.height);
  return x !== undefined && y !== undefined && width !== undefined && height !== undefined ? { x, y, width, height } : undefined;
};

export function readAction(value: unknown): Action | undefined {
  if (!isRecord(value) || !str(value.type)) return undefined;
  return { type: String(value.type), target: str(value.target), url: str(value.url), valid: value.valid !== false, tooltip: str(value.tooltip) };
}

function readLayer(id: string, raw: unknown): LayerVisual | undefined {
  if (!isRecord(raw)) return undefined;
  const sync = isRecord(raw.syncGroup) && str(raw.syncGroup.groupName)
    ? { groupName: String(raw.syncGroup.groupName), fieldChanges: raw.syncGroup.fieldChanges !== false, filterChanges: raw.syncGroup.filterChanges !== false } : undefined;
  return {
    id, title: str(raw.title) ?? id, kind: str(raw.kind) ?? "unsupported", visualType: str(raw.visual_type), rank: num(raw.rank) ?? 0,
    hidden: raw.hidden === true, hiddenOwn: raw.hidden_own === true || (raw.hidden_own === undefined && raw.hidden === true),
    parentGroup: str(raw.parent_group), children: strList(raw.group_children), position: readRect(raw.position),
    action: readAction(raw.action), syncGroup: sync, tooltipPage: isRecord(raw.tooltipPage) ? str(raw.tooltipPage.page) : undefined,
  };
}

/** The pane facts of a page render (either route): bookmarks, interactions, synced slicers, phone layout, page type, layers. */
export function readPaneMeta(body: unknown): PaneMeta {
  const source = isRecord(body) ? body : {};
  const page = isRecord(source.page) ? source.page : {};
  const pageType: PageType = page.page_type === "drillthrough" || page.page_type === "tooltip" ? page.page_type : "default";
  const binding = isRecord(page.binding) ? {
    type: (page.binding.type === "tooltip" ? "tooltip" : "drillthrough") as PageType, fields: readFields(page.binding.fields),
    keepAllFilters: page.binding.keepAllFilters !== false, crossReport: page.binding.crossReport === true,
  } : undefined;
  const bookmarks = Array.isArray(source.bookmarks) ? source.bookmarks.flatMap((raw): Bookmark[] => {
    if (!isRecord(raw) || !str(raw.name)) return [];
    const captures = isRecord(raw.captures) ? raw.captures : {};
    const group = isRecord(raw.group) && str(raw.group.name) ? { name: String(raw.group.name), displayName: str(raw.group.displayName) ?? String(raw.group.name) } : undefined;
    return [{
      name: String(raw.name), displayName: str(raw.displayName) ?? String(raw.name), ...(group ? { group } : {}), page: str(raw.page),
      targets: Array.isArray(raw.targets) ? strList(raw.targets) : null,
      captures: { data: captures.data !== false, display: captures.display !== false, page: captures.page !== false },
      hidden: strList(raw.hidden), shown: strList(raw.shown),
    }];
  }) : [];
  const interactions: Record<string, Record<string, Interaction>> = {};
  if (isRecord(source.interactions)) {
    for (const [from, targets] of Object.entries(source.interactions)) {
      if (!isRecord(targets)) continue;
      interactions[from] = {};
      for (const [to, how] of Object.entries(targets)) if (how === "filter" || how === "highlight" || how === "none" || how === "unsupported") interactions[from][to] = how;
    }
  }
  const syncGroups: Record<string, SyncMember[]> = {};
  if (isRecord(source.sync_groups)) {
    for (const [group, members] of Object.entries(source.sync_groups)) {
      syncGroups[group] = Array.isArray(members) ? members.flatMap(m => isRecord(m) && str(m.page) && str(m.visual) ? [{ page: String(m.page), visual: String(m.visual), hidden: m.hidden === true }] : []) : [];
    }
  }
  let mobile: PaneMeta["mobile"];
  if (isRecord(source.mobile) && isRecord(source.mobile.visuals)) {
    mobile = {};
    for (const [id, box] of Object.entries(source.mobile.visuals)) {
      const rect = readRect(box);
      if (rect) mobile[id] = { ...rect, z: isRecord(box) ? num(box.z) : undefined };
    }
  }
  const drillthroughs = Array.isArray(source.drillthroughs) ? source.drillthroughs.flatMap((raw): Drillthrough[] => isRecord(raw) && str(raw.page) ? [{
    page: String(raw.page), title: str(raw.title) ?? String(raw.page), fields: readFields(raw.fields), keepAllFilters: raw.keepAllFilters !== false, crossReport: raw.crossReport === true,
  }] : []) : [];
  const visuals: Record<string, LayerVisual> = {};
  // The formatting route keys rows by id; the data route lists them.
  if (isRecord(source.visuals)) for (const [id, raw] of Object.entries(source.visuals)) { const layer = readLayer(id, raw); if (layer) visuals[id] = layer; }
  else if (Array.isArray(source.visuals)) for (const raw of source.visuals) { if (isRecord(raw) && str(raw.id)) { const layer = readLayer(String(raw.id), raw); if (layer) visuals[layer.id] = layer; } }
  return { pageType, binding, bookmarks, interactions, syncGroups, mobile, drillthroughs, notes: strList(source.notes), visuals };
}

// --- bookmarks --------------------------------------------------------------------------------------------

export type BookmarkEntry = { kind: "bookmark"; bookmark: Bookmark; position: number } | { kind: "group"; name: string; displayName: string; items: { bookmark: Bookmark; position: number }[]; position: number };

/** The Bookmarks pane in pane order: top-level bookmarks and groups, each with its 1-based place among its siblings. */
export function bookmarkTree(bookmarks: Bookmark[]): BookmarkEntry[] {
  const entries: BookmarkEntry[] = [];
  const groups = new Map<string, Extract<BookmarkEntry, { kind: "group" }>>();
  for (const bookmark of bookmarks) {
    if (bookmark.group) {
      let group = groups.get(bookmark.group.name);
      if (!group) {
        group = { kind: "group", name: bookmark.group.name, displayName: bookmark.group.displayName, items: [], position: entries.length + 1 };
        groups.set(bookmark.group.name, group);
        entries.push(group);
      }
      group.items.push({ bookmark, position: group.items.length + 1 });
    } else entries.push({ kind: "bookmark", bookmark, position: entries.length + 1 });
  }
  return entries;
}

/** "Bookmark 3": the first free default name, as Desktop names a new bookmark. */
export function nextBookmarkName(bookmarks: Pick<Bookmark, "displayName">[]): string {
  const taken = new Set(bookmarks.map(b => b.displayName.toLowerCase()));
  let n = bookmarks.length + 1;
  while (taken.has(`bookmark ${n}`)) n += 1;
  return `Bookmark ${n}`;
}

export type CaptureOptions = { data: boolean; display: boolean; currentPage: boolean; selectedOnly: boolean };
export const DEFAULT_CAPTURE: CaptureOptions = { data: false, display: true, currentPage: true, selectedOnly: false };

/** `add_bookmark` from the page as it is now. "Selected visuals" limits it to the canvas selection. */
export function addBookmarkOp(name: string, page: string, capture: CaptureOptions, selected: string[], group?: string): Operation | undefined {
  const trimmed = name.trim();
  if (!trimmed) return undefined;
  return {
    op: "add_bookmark", name: trimmed.slice(0, 100), page, data: capture.data, display: capture.display, current_page: capture.currentPage,
    ...(capture.selectedOnly && selected.length ? { visuals: selected } : {}), ...(group ? { group } : {}),
  };
}

/** `update_bookmark` ("Update"): takes the page again, with the capture options the user now wants. */
export const updateBookmarkOp = (bookmark: string, capture?: Partial<CaptureOptions>, selected: string[] = []): Operation => ({
  op: "update_bookmark", bookmark,
  ...(capture?.data !== undefined ? { data: capture.data } : {}), ...(capture?.display !== undefined ? { display: capture.display } : {}),
  ...(capture?.currentPage !== undefined ? { current_page: capture.currentPage } : {}),
  ...(capture?.selectedOnly !== undefined ? { visuals: capture.selectedOnly ? selected : [] } : {}),
});

/**
 * The operation a drag in the Bookmarks pane makes: `from` and `to` are places in the flat pane order (`bookmarks`). A drop
 * inside another group moves the bookmark into it; a drop among top-level entries takes it out of its group.
 */
export function moveBookmarkOp(bookmarks: Bookmark[], from: number, to: number): Operation | undefined {
  const moving = bookmarks[from];
  const target = bookmarks[to];
  if (!moving || !target || from === to) return undefined;
  const siblings = bookmarks.filter(b => (b.group?.name ?? "") === (target.group?.name ?? ""));
  const place = siblings.indexOf(target) + 1;
  if (target.group && moving.group?.name !== target.group.name) return { op: "move_bookmark", bookmark: moving.name, group: target.group.name, position: place };
  if (!target.group && moving.group) {
    const topLevel = bookmarkTree(bookmarks);
    const entry = topLevel.find(e => e.kind === "bookmark" && e.bookmark.name === target.name);
    return { op: "move_bookmark", bookmark: moving.name, top_level: true, position: entry?.position ?? 1 };
  }
  if (!target.group) {
    const topLevel = bookmarkTree(bookmarks);
    const entry = topLevel.find(e => e.kind === "bookmark" && e.bookmark.name === target.name);
    return { op: "move_bookmark", bookmark: moving.name, position: entry?.position ?? place };
  }
  return { op: "move_bookmark", bookmark: moving.name, position: place };
}

/** One step up or down in its own list (the keyboard way to reorder). */
export function nudgeBookmarkOp(bookmarks: Bookmark[], name: string, step: -1 | 1): Operation | undefined {
  const moving = bookmarks.find(b => b.name === name);
  if (!moving) return undefined;
  if (moving.group) {
    const siblings = bookmarks.filter(b => b.group?.name === moving.group?.name);
    const at = siblings.indexOf(moving) + step;
    return at < 0 || at >= siblings.length ? undefined : { op: "move_bookmark", bookmark: name, position: at + 1 };
  }
  const entries = bookmarkTree(bookmarks);
  const at = entries.findIndex(e => e.kind === "bookmark" && e.bookmark.name === name) + step;
  return at < 0 || at >= entries.length ? undefined : { op: "move_bookmark", bookmark: name, position: at + 1 };
}

/** Buttons and navigators on this page that use the bookmark (what a delete would switch off). */
export function bookmarkUsers(bookmark: Pick<Bookmark, "name" | "displayName">, visuals: Record<string, LayerVisual>): LayerVisual[] {
  return Object.values(visuals).filter(visual => (visual.action?.type === "bookmark" && (visual.action.target === bookmark.name || visual.action.target === bookmark.displayName))
    || visual.visualType === "bookmarkNavigator");
}

/** Whether a bookmark hides a visual: `true` hides, `false` shows, `undefined` when it does not touch it. */
export function bookmarkHides(bookmark: Pick<Bookmark, "hidden" | "shown" | "targets" | "captures">, visual: string): boolean | undefined {
  if (!bookmark.captures.display) return undefined;
  if (bookmark.targets && !bookmark.targets.includes(visual)) return undefined;
  if (bookmark.hidden.includes(visual)) return true;
  if (bookmark.shown.includes(visual)) return false;
  return bookmark.targets === null ? false : undefined;
}

// --- selection pane ------------------------------------------------------------------------------------------

export type LayerRow = LayerVisual & { depth: number };

/** The Selection pane: groups with their members under them, top of the stack first (as Desktop lists layers). */
export function layerRows(visuals: Record<string, LayerVisual>, fallback: { id: string; title: string; kind: string; hidden: boolean }[] = []): LayerRow[] {
  const all: Record<string, LayerVisual> = { ...visuals };
  for (const item of fallback) if (!all[item.id]) all[item.id] = { id: item.id, title: item.title, kind: item.kind, rank: 0, hidden: item.hidden, hiddenOwn: item.hidden, children: [] };
  const byRank = (a: LayerVisual, b: LayerVisual) => b.rank - a.rank || a.title.localeCompare(b.title);
  const rows: LayerRow[] = [];
  const seen = new Set<string>();
  const walk = (visual: LayerVisual, depth: number) => {
    if (seen.has(visual.id) || depth > 8) return;
    seen.add(visual.id);
    rows.push({ ...visual, depth });
    const members = Object.values(all).filter(v => v.parentGroup === visual.id);
    const ordered = visual.children.length ? [...members].sort((a, b) => visual.children.indexOf(b.id) - visual.children.indexOf(a.id)) : members.sort(byRank);
    for (const member of ordered) walk(member, depth + 1);
  };
  for (const visual of Object.values(all).filter(v => !v.parentGroup || !all[v.parentGroup]).sort(byRank)) walk(visual, 0);
  return rows;
}

// --- interactions ----------------------------------------------------------------------------------------------

const HIGHLIGHTS = new Set(["column", "bar", "pie", "donut"]);
const REACHABLE = new Set(["column", "bar", "line", "area", "pie", "donut", "card", "table", "matrix", "slicer", "unsupported"]);
/** The visuals whose selection reaches others (a slicer, a chart, a table): only they can be an interaction's source. */
export const canFilterOthers = (kind: string) => kind === "slicer" || kind === "table" || kind === "matrix" || HIGHLIGHTS.has(kind) || kind === "line" || kind === "area";
/** What a target can do when a source is selected: highlight only for column, bar, pie and donut charts. */
export function interactionChoices(targetKind: string): Exclude<Interaction, "unsupported">[] {
  if (!REACHABLE.has(targetKind)) return [];
  return HIGHLIGHTS.has(targetKind) ? ["filter", "highlight", "none"] : ["filter", "none"];
}
export function interactionOf(interactions: PaneMeta["interactions"], source: string, target: string): Interaction | undefined {
  return interactions[source]?.[target];
}
export const setInteractionOp = (source: string, target: string, type: Exclude<Interaction, "unsupported"> | "default"): Operation => ({ op: "set_interaction", source, targets: [target], type });

// --- synced slicers ---------------------------------------------------------------------------------------------

export type SyncCell = { synced: boolean; visible: boolean; own: boolean };
export type SyncRow = { visual: string; title: string; group?: string; cells: Record<string, SyncCell> };

/** The Sync slicers matrix: the page's slicers against every page (synced there, and visible there). */
export function syncMatrix(slicers: { id: string; title: string; syncGroup?: string }[], pages: { id: string }[], currentPage: string, groups: PaneMeta["syncGroups"]): SyncRow[] {
  return slicers.map(slicer => {
    const members = slicer.syncGroup ? groups[slicer.syncGroup] ?? [] : [];
    const cells: Record<string, SyncCell> = {};
    for (const page of pages) {
      if (page.id === currentPage) { cells[page.id] = { synced: members.length > 0, visible: true, own: true }; continue; }
      const member = members.find(m => m.page === page.id);
      cells[page.id] = { synced: Boolean(member), visible: Boolean(member && !member.hidden), own: false };
    }
    return { visual: slicer.id, title: slicer.title, group: slicer.syncGroup, cells };
  });
}

/** The operation a click in the matrix makes: sync onto a page (visible or hidden there), show or hide the copy, or stop syncing. */
export function syncOp(row: SyncRow, page: string, change: "sync" | "unsync" | "show" | "hide"): Operation | undefined {
  const cell = row.cells[page];
  if (!cell || cell.own) return undefined;
  if (change === "unsync") return cell.synced ? { op: "unsync_slicer", visual: row.visual, pages: [page] } : undefined;
  if (change === "sync") return cell.synced ? undefined : { op: "sync_slicer", visual: row.visual, pages: [page] };
  if (change === "hide") return { op: "sync_slicer", visual: row.visual, hidden_on: [page] };
  return { op: "sync_slicer", visual: row.visual, pages: [page] };
}

// --- phone layout ----------------------------------------------------------------------------------------------

export const PHONE_WIDTH = 320;
/** Where a visual dropped on the phone canvas goes: inside the 320 px width, 20 px minimum, snapped to 10 px. */
export function phoneBox(point: { x: number; y: number }, size: { width: number; height: number }): Rect {
  const width = Math.max(20, Math.min(PHONE_WIDTH, Math.round(size.width / 10) * 10));
  const height = Math.max(20, Math.min(5000, Math.round(size.height / 10) * 10));
  const x = Math.max(0, Math.min(PHONE_WIDTH - width, Math.round(point.x / 10) * 10));
  const y = Math.max(0, Math.min(20000, Math.round(point.y / 10) * 10));
  return { x, y, width, height };
}
/** The size a desktop visual takes on the phone: the full width, its height scaled like its width (40 to 400 px). */
export function phoneSize(desktop: { width: number; height: number } | undefined): { width: number; height: number } {
  if (!desktop || desktop.width <= 0) return { width: PHONE_WIDTH, height: 120 };
  const height = Math.round(desktop.height * Math.min(1.5, PHONE_WIDTH / desktop.width) / 10) * 10;
  return { width: PHONE_WIDTH, height: Math.max(40, Math.min(400, height)) };
}
export const placeOnMobileOp = (visual: string, box: Rect): Operation => ({ op: "place_on_mobile", visual, x: box.x, y: box.y, width: box.width, height: box.height });
/** The lowest free spot under the visuals already on the phone. */
export function nextPhoneY(mobile: PaneMeta["mobile"]): number {
  return Object.values(mobile ?? {}).reduce((bottom, box) => Math.max(bottom, box.y + box.height), 0) + (mobile && Object.keys(mobile).length ? 10 : 0);
}

// --- page type -------------------------------------------------------------------------------------------------

export type ColumnRef = { table: string; name: string };
/** The operation that makes a page normal, a drillthrough page for `fields`, or a report-page tooltip. */
export function pageTypeOp(page: string, type: PageType, options: { fields?: ColumnRef[]; keepAllFilters?: boolean; crossReport?: boolean; tooltipSize?: boolean } = {}): Operation | undefined {
  if (type === "default") return { op: "clear_page_type", page };
  if (type === "drillthrough") {
    if (!options.fields?.length) return undefined;
    return { op: "set_drillthrough", page, fields: options.fields, mode: "replace", ...(options.keepAllFilters !== undefined ? { keep_all_filters: options.keepAllFilters } : {}), ...(options.crossReport !== undefined ? { cross_report: options.crossReport } : {}) };
  }
  return { op: "set_tooltip_page", page, size: options.tooltipSize === false ? "keep" : "single_visual", ...(options.fields?.length ? { fields: options.fields } : {}) };
}
/** "DimStore.StoreName" to a column reference. */
export function columnRef(field: { field: string; table?: string; column?: string }): ColumnRef {
  if (field.table && field.column) return { table: field.table, name: field.column };
  const at = field.field.indexOf(".");
  return at > 0 ? { table: field.field.slice(0, at), name: field.field.slice(at + 1) } : { table: "", name: field.field };
}

// --- insert menu, buttons and actions -------------------------------------------------------------------------------

export const BUTTON_KINDS: { value: string; label: string }[] = [
  { value: "blank", label: "Blank" }, { value: "back", label: "Back" }, { value: "bookmark", label: "Bookmark" }, { value: "information", label: "Information" },
  { value: "help", label: "Help" }, { value: "qna", label: "Q&A" }, { value: "left_arrow", label: "Left arrow" }, { value: "right_arrow", label: "Right arrow" },
  { value: "reset", label: "Reset" }, { value: "apply_all_slicers", label: "Apply all slicers" }, { value: "clear_all_slicers", label: "Clear all slicers" },
];
export const SHAPES: { value: string; label: string }[] = [
  // report_edit_align.ShapeName (Desktop's tileShape values).
  ["rectangle", "Rectangle"], ["rectangleRounded", "Rounded rectangle"], ["oval", "Oval"], ["line", "Line"], ["triangleIsoc", "Triangle"],
  ["triangleRight", "Right triangle"], ["arrow", "Arrow"], ["arrowChevron", "Chevron"], ["pentagon", "Pentagon"], ["hexagon", "Hexagon"],
  ["octagon", "Octagon"], ["heart", "Heart"], ["pill", "Pill"], ["speechbubbleRectangle", "Speech bubble"], ["trapezoid", "Trapezoid"],
].map(([value, label]) => ({ value, label }));

export type InsertChoice = { kind: "button"; button: string } | { kind: "navigator"; navigator: "page" | "bookmark" } | { kind: "shape"; shape: string } | { kind: "textbox" };
/** The operation of an Insert menu choice: placed in free space by the server (no position given). */
export function insertOp(page: string, choice: InsertChoice): Operation {
  if (choice.kind === "button") return { op: "add_button", page, kind: choice.button };
  if (choice.kind === "navigator") return { op: "add_navigator", page, kind: choice.navigator };
  if (choice.kind === "shape") return { op: "add_shape", page, shape: choice.shape };
  return { op: "add_textbox", page, text: "Text" };
}

export type ActionType = "none" | "back" | "bookmark" | "page" | "drillthrough" | "web_url" | "qna";
export const ACTION_TYPES: { value: ActionType; label: string }[] = [
  { value: "none", label: "No action" }, { value: "back", label: "Back" }, { value: "bookmark", label: "Bookmark" }, { value: "page", label: "Page navigation" },
  { value: "drillthrough", label: "Drill through" }, { value: "web_url", label: "Web URL" }, { value: "qna", label: "Q&A" },
];
export type ActionDraft = { type: ActionType; page?: string; bookmark?: string; url?: string; tooltip: string };
/** What the render row says the button does now. */
export function actionDraft(action: Action | undefined): ActionDraft {
  if (!action) return { type: "none", tooltip: "" };
  const type: ActionType = action.type === "url" ? "web_url" : action.type === "page" || action.type === "bookmark" || action.type === "back" || action.type === "drillthrough" || action.type === "qna" ? action.type : "none";
  return { type, page: type === "page" || type === "drillthrough" ? action.target : undefined, bookmark: type === "bookmark" ? action.target : undefined, url: action.url, tooltip: action.tooltip ?? "" };
}
/** `set_button_action`, or what is missing (a page, a bookmark, an http(s) address). */
export function actionOp(visual: string, draft: ActionDraft): { ok: true; op: Operation } | { ok: false; message: string } {
  const action: Record<string, unknown> = { type: draft.type };
  if (draft.type === "page" || draft.type === "drillthrough") {
    if (!draft.page) return { ok: false, message: draft.type === "page" ? "Pick the page to go to." : "Pick the drillthrough page." };
    action.page = draft.page;
  }
  if (draft.type === "bookmark") {
    if (!draft.bookmark) return { ok: false, message: "Pick the bookmark." };
    action.bookmark = draft.bookmark;
  }
  if (draft.type === "web_url") {
    const url = draft.url?.trim() ?? "";
    if (!/^https?:\/\/\S+$/i.test(url)) return { ok: false, message: "Give a web address that starts with http:// or https://." };
    action.url = url;
  }
  // An empty box keeps the tooltip the button has (`tooltip` left out); "Remove tooltip" sends `""`.
  return { ok: true, op: { op: "set_button_action", visual, action, ...(draft.tooltip.trim() ? { tooltip: draft.tooltip.trim().slice(0, 500) } : {}) } };
}
/** `set_button_action` with `tooltip: ""`: the tooltip goes, the action stays. */
export const removeTooltipOp = (visual: string): Operation => ({ op: "set_button_action", visual, tooltip: "" });

// --- the canvas context menu and toolbar arrangements ------------------------------------------------------------------

export type Side = "left_of" | "right_of" | "above" | "below";
export const alignOp = (visuals: string[], edge: "left" | "right" | "top" | "bottom" | "center_h" | "center_v"): Operation => ({ op: "align_visuals", visuals, edge, to: "selection" });
export const distributeOp = (visuals: string[], axis: "horizontal" | "vertical"): Operation => ({ op: "distribute_visuals", visuals, axis });
export const swapOp = (a: string, b: string): Operation => ({ op: "swap_visuals", a, b });
export const placeNextToOp = (visual: string, relativeTo: string, side: Side, gap = 10): Operation => ({ op: "place_visual", visual, relative_to: relativeTo, side, gap });
export const groupOp = (visuals: string[], name?: string): Operation => ({ op: "group_visuals", visuals, ...(name?.trim() ? { name: name.trim() } : {}) });
export const ungroupOp = (group: string): Operation => ({ op: "ungroup_visuals", group });

/** The group to ungroup for a selection: the selected group itself, else the group the selected visual is in. */
export function groupToUngroup(selected: string[], visuals: Record<string, LayerVisual>): string | undefined {
  if (selected.length !== 1) return undefined;
  const visual = visuals[selected[0]];
  if (!visual) return undefined;
  if (visual.kind === "group") return visual.id;
  return visual.parentGroup && visuals[visual.parentGroup] ? visual.parentGroup : undefined;
}
/** Visuals can be grouped when two or more are selected and they share a parent (the server refuses otherwise). */
export function canGroup(selected: string[], visuals: Record<string, LayerVisual>): boolean {
  if (selected.length < 2) return false;
  const parents = new Set(selected.map(id => visuals[id]?.parentGroup ?? ""));
  return parents.size === 1;
}

export type MenuItem = { id: string; label: string; disabled?: boolean; hint?: string; danger?: boolean; separator?: boolean; children?: MenuItem[] };

/** The canvas context menu for the current selection, in Power BI's order. `number` says whether the first visual shows numbers. */
export function contextMenuItems(selected: string[], visuals: Record<string, LayerVisual>, options: { number: boolean; locked: boolean; testsAvailable: boolean }): MenuItem[] {
  const one = selected.length === 1;
  const items: MenuItem[] = [];
  const locked = options.locked;
  if (one && options.number) {
    items.push({ id: "explain", label: "Explain this number" });
    items.push({ id: "pin", label: "Pin this number as a test", disabled: !options.testsAvailable, hint: options.testsAvailable ? undefined : "Report tests need Studio's data for this report" });
    items.push({ id: "sep-numbers", label: "", separator: true });
  }
  if (selected.length >= 2) {
    items.push({ id: "group", label: "Group", disabled: locked || !canGroup(selected, visuals), hint: canGroup(selected, visuals) ? undefined : "Only visuals in the same group can be grouped together" });
    items.push({ id: "align", label: "Align", disabled: locked, children: [
      { id: "align:left", label: "Align left" }, { id: "align:center_h", label: "Align center" }, { id: "align:right", label: "Align right" },
      { id: "align:top", label: "Align top" }, { id: "align:center_v", label: "Align middle" }, { id: "align:bottom", label: "Align bottom" },
      { id: "distribute:horizontal", label: "Distribute horizontally", disabled: selected.length < 3 }, { id: "distribute:vertical", label: "Distribute vertically", disabled: selected.length < 3 },
    ] });
  }
  if (selected.length === 2) {
    items.push({ id: "swap", label: "Swap positions", disabled: locked });
    items.push({ id: "place", label: "Place next to the other", disabled: locked, children: [
      { id: "place:left_of", label: "To its left" }, { id: "place:right_of", label: "To its right" }, { id: "place:above", label: "Above it" }, { id: "place:below", label: "Below it" },
    ] });
  }
  const ungroup = groupToUngroup(selected, visuals);
  if (ungroup) items.push({ id: "ungroup", label: "Ungroup", disabled: locked });
  if (one) items.push({ id: "interactions", label: "Edit interactions", disabled: !canFilterOthers(visuals[selected[0]]?.kind ?? "") });
  if (selected.length) {
    items.push({ id: "sep-order", label: "", separator: true });
    items.push({ id: "arrange:front", label: "Bring to front", disabled: locked || !one }, { id: "arrange:back", label: "Send to back", disabled: locked || !one });
    items.push({ id: "duplicate", label: "Duplicate", disabled: locked }, { id: "delete", label: "Delete", disabled: locked, danger: true });
  }
  return items;
}

/** The operations of a menu choice that is a plain edit (the rest open a panel or change a mode). */
export function menuOperations(choice: string, selected: string[], visuals: Record<string, LayerVisual>): Operation[] | undefined {
  const [head, arg] = choice.split(":");
  if (head === "align" && arg) return [alignOp(selected, arg as Parameters<typeof alignOp>[1])];
  if (head === "distribute" && (arg === "horizontal" || arg === "vertical")) return [distributeOp(selected, arg)];
  if (head === "swap" && selected.length === 2) return [swapOp(selected[0], selected[1])];
  if (head === "place" && selected.length === 2 && arg) return [placeNextToOp(selected[0], selected[1], arg as Side)];
  if (head === "group") return [groupOp(selected)];
  if (head === "ungroup") { const group = groupToUngroup(selected, visuals); return group ? [ungroupOp(group)] : undefined; }
  if (head === "arrange" && selected.length === 1 && (arg === "front" || arg === "back")) return [{ op: "arrange", visual: selected[0], to: arg }];
  if (head === "duplicate") return selected.map(visual => ({ op: "duplicate_visual", visual }));
  if (head === "delete") return selected.map(visual => ({ op: "delete_visual", visual }));
  return undefined;
}

/** The topmost visible visual under a page point (Alt-free: the context menu picks what is on top). */
export function visualAt(rects: { id: string; x: number; y: number; width: number; height: number; rank: number; hidden: boolean; kind: string }[], point: { x: number; y: number }): string | undefined {
  const hits = rects.filter(r => r.kind !== "group" && !r.hidden && point.x >= r.x && point.x <= r.x + r.width && point.y >= r.y && point.y <= r.y + r.height);
  hits.sort((a, b) => b.rank - a.rank);
  return hits[0]?.id;
}
