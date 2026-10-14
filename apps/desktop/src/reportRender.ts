// Pure helpers behind the imported-report canvas: payload types (docs/imported-report-render-contract.md),
// token -> CSS style objects, stacking, hit-testing, stale-response rules and the chart spec. No React
// components here so every rule is unit-tested (tests/report-render.test.mjs).
//
// Nothing in the payload is markup: colors, font families and image sources were validated on the server,
// and are checked again here before they reach a style object, so a hostile value degrades to "unset".
import type { CSSProperties } from "react";

import { axisUnitsExpr, formatUnits, formatWithString, isPercentFormat, unitSuffix } from "./reportFormat";

export type Value = string | number | boolean | null;
export type Rect = { x: number; y: number; width: number; height: number };
export type Head = { id: string; hash: string };
export type Fidelity = "exact" | "approximate" | "none";

export type DataBars = {
  positive: string; negative: string; hideText: boolean;
  axis?: Nullable<string>; reverse?: boolean; min?: Nullable<number>; max?: Nullable<number>;
};

/** Contract section 2. `kind: "computed"` columns hold colors, icon names or visual calculations the
 * server computed per row (`computed` says which); `role: "SortBy"` orders a category and is hidden. */
export type Column = {
  kind: string; table?: string; name: string; label: string; role: string; query_ref?: string | null;
  hidden?: boolean; format?: string; colorIndex?: number | null; alignment?: string;
  backgroundIndex?: number | null; icon?: { index: number; layout?: string; alignment?: string } | null;
  displayUnits?: Nullable<number>; decimals?: Nullable<number>; computed?: string;
  dataBars?: DataBars | null;
};

type Nullable<T> = T | null | undefined;
type FontFields = { fontSize?: Nullable<number>; color?: Nullable<string>; family?: Nullable<string>; bold?: boolean; italic?: boolean; alignment?: Nullable<string> };

export type TitleToken = {
  show?: boolean; text?: string; textSource?: string; automaticText?: Nullable<string>; fontSize?: Nullable<number>;
  fontColor?: Nullable<string>; fontFamily?: Nullable<string>; bold?: boolean; italic?: boolean; underline?: boolean;
  alignment?: Nullable<string>; wrap?: boolean; background?: Nullable<string>;
};
export type ContainerToken = {
  background?: Nullable<string>; border?: { show?: boolean; color?: Nullable<string>; width?: Nullable<number>; radius?: number };
  shadow?: boolean; padding?: Nullable<Nullable<number>[]>;
};
export type AxisToken = {
  show?: boolean; title?: boolean; titleText?: Nullable<string>; titleColor?: Nullable<string>; titleFontSize?: Nullable<number>;
  fontSize?: Nullable<number>; color?: Nullable<string>; family?: Nullable<string>; gridlines?: boolean; gridlineColor?: Nullable<string>;
  displayUnits?: Nullable<number>; decimals?: Nullable<number>;
};
export type LabelsToken = FontFields & { show?: boolean; displayUnits?: Nullable<number>; decimals?: Nullable<number>; position?: Nullable<string>; style?: Nullable<string>; format?: Nullable<string> };
export type LegendToken = FontFields & { show?: boolean; position?: string; alignment?: string };
export type CategoryColor = { value: Value; color: string; column?: string; metadata?: string; type?: string };
/** A conditional data color: the row's value at `index` colors the points of the series `metadata` (or every series). */
export type RuleColor = { metadata?: Nullable<string>; index: number };
export type SeriesToken = { defaultColor?: Nullable<string>; seriesColors?: Record<string, string>; categoryColors?: CategoryColor[]; varyByCategory?: boolean; ruleColors?: RuleColor[] };
export type TileState = {
  fill?: Nullable<string>; fillShown?: boolean; fillIndex?: Nullable<number>; outline?: { show?: boolean; color?: Nullable<string>; width?: Nullable<number>; transparency?: Nullable<number> };
  padding?: Nullable<Nullable<number>[]>; accentBar?: { show?: boolean; color?: Nullable<string>; width?: Nullable<number>; position?: Nullable<string> };
};
export type TileToken = { default?: Nullable<TileState>; selected?: Nullable<TileState> };
export type TableColumnToken = {
  alignment?: Nullable<string>; styleHeader?: boolean; width?: Nullable<number>; fontColor?: Nullable<string>; background?: Nullable<string>;
  displayUnits?: Nullable<number>; decimals?: Nullable<number>;
};
export type TotalToken = { show?: boolean; label?: Nullable<string>; fontSize?: Nullable<number>; color?: Nullable<string>; family?: Nullable<string>; background?: Nullable<string>; bold?: boolean };
export type TableToken = {
  header?: { fontSize?: Nullable<number>; color?: Nullable<string>; family?: Nullable<string>; background?: Nullable<string>; alignment?: Nullable<string>; wrap?: boolean };
  values?: { fontSize?: Nullable<number>; color?: Nullable<string>; family?: Nullable<string>; secondaryColor?: Nullable<string>; banding?: { primary?: Nullable<string>; secondary?: Nullable<string> }; wrap?: boolean };
  grid?: { horizontal?: boolean; vertical?: boolean; horizontalColor?: Nullable<string>; verticalColor?: Nullable<string>; horizontalWeight?: Nullable<number>; verticalWeight?: Nullable<number>; rowPadding?: Nullable<number> };
  columns?: Record<string, TableColumnToken>;
  total?: TotalToken;
};
export type ButtonTextToken = FontFields & { show?: boolean; text?: Nullable<string>; underline?: boolean; verticalAlignment?: Nullable<string> };
export type ButtonIconToken = { show?: boolean; name?: Nullable<string>; color?: Nullable<string>; width?: Nullable<number>; size?: Nullable<number>; placement?: Nullable<string>; alignment?: Nullable<string>; verticalAlignment?: Nullable<string> };
export type ButtonState = { fill?: Nullable<string>; outline?: { show?: boolean; color?: Nullable<string>; width?: Nullable<number> }; text?: ButtonTextToken; icon?: ButtonIconToken };
/** Contract section 16: a button's states are default/hover/selected/disabled, a navigator's default/hover/press/selected. */
export type ButtonToken = {
  shape?: string; tileShape?: Nullable<string>; radius?: Nullable<number>;
  states?: Partial<Record<"default" | "hover" | "selected" | "disabled" | "press", Nullable<ButtonState>>>;
  layout?: { orientation?: string; rows?: Nullable<number>; columns?: Nullable<number>; cellPadding?: Nullable<number> };
};
export type Action = { type: string; target?: Nullable<string>; valid?: boolean; tooltip?: Nullable<string>; url?: Nullable<string> };
export type NavigatorItem = { label: string; action: Action; selected?: boolean };
export type NavigatorToken = { kind?: string; items?: NavigatorItem[] };
export type SlicerToken = {
  mode?: string;
  header?: FontFields & { show?: boolean; text?: Nullable<string> };
  items?: FontFields;
  values?: { default?: Nullable<FontFields & { show?: boolean }>; selected?: Nullable<FontFields & { show?: boolean }> };
  layout?: { rows?: Nullable<number>; columns?: Nullable<number>; cellPadding?: Nullable<number>; shape?: Nullable<string>; radius?: Nullable<number> };
};
export type TextRun = { text: string; fontFamily?: Nullable<string>; fontSize?: Nullable<number>; color?: Nullable<string>; bold?: boolean; italic?: boolean; underline?: boolean; hasLink?: boolean };
export type TextParagraph = { align?: Nullable<string>; runs: TextRun[] };
export type ShapeToken = {
  shape?: string; tileShape?: Nullable<string>; fill?: Nullable<string>; outline?: { show?: boolean; color?: Nullable<string>; width?: Nullable<number> };
  radius?: Nullable<number>; rotation?: Nullable<number>;
};

export type Formatting = {
  // Legacy keys: still sent with their old meaning, read only when the typed token is absent.
  slicerStyle?: string; label?: string; showLabel?: boolean; showValue?: boolean; titleVisible?: boolean; fontSize?: number;
  background?: string; image?: Nullable<string>; imageIndex?: Nullable<number>;
  references?: { index: number; label: string; detailIndex?: number }[];
  categoryColors?: { value: Value; color: string }[]; categoryAxisTitle?: boolean; valueAxisTitle?: boolean; grid?: boolean; tableGrid?: boolean;
  // Typed tokens.
  title?: TitleToken; container?: ContainerToken; automaticTitle?: Nullable<string>;
  chart?: { orientation?: string; stacking?: string };
  labels?: LabelsToken; legend?: LegendToken; axes?: { category?: AxisToken; value?: AxisToken };
  series?: SeriesToken; line?: { strokeWidth?: Nullable<number>; markers?: boolean };
  cardValue?: FontFields & { show?: boolean; colorIndex?: Nullable<number>; displayUnits?: Nullable<number>; decimals?: Nullable<number> };
  cardLabel?: FontFields & { show?: boolean; text?: Nullable<string>; colorIndex?: Nullable<number> };
  tile?: Nullable<TileToken>; table?: TableToken; slicer?: SlicerToken;
  content?: TextParagraph[]; shape?: ShapeToken; picture?: { src?: Nullable<string>; scaling?: string };
  button?: ButtonToken; navigator?: NavigatorToken;
  smallMultiples?: { index: number; field?: string; query_ref?: Nullable<string>; layout?: { type?: string; rows?: Nullable<number>; columns?: Nullable<number> } };
  /** The paths (`title.text`, `title.fontColor`, `title.background`, `background.color`) a measure gives: values in the row's `dynamic`. */
  dynamicProperties?: string[];
  /** Computed over the (at most 100) rows drawn: gradients, percent rules, analytics lines, running calculations, panels. */
  rangeDependent?: boolean;
};

/** An analytics line with its value (contract section 15). */
export type ReferenceLine = {
  id?: Nullable<string>; object?: string; kind: string; axis?: string; series?: Nullable<string>;
  value: Nullable<number | string>; start?: Nullable<number>; end?: Nullable<number>;
  name?: Nullable<string>; color?: Nullable<string>; alpha?: Nullable<number>; style?: Nullable<string>; width?: Nullable<number>; position?: Nullable<string>;
  label?: { show?: boolean; text?: Nullable<string>; color?: Nullable<string>; decimals?: Nullable<number>; displayUnits?: Nullable<number>; horizontal?: Nullable<string>; vertical?: Nullable<string> };
};
export type SmallMultiplesData = { index: number; field?: string; values?: Value[]; panels: { value: Value; rows: number[] }[] };

export type RenderVisual = {
  id: string; title: string; kind: string; visual_type?: string; container_kind?: string;
  position?: Rect; z?: number; rank?: number; hidden?: boolean; hidden_own?: boolean;
  parent_group?: Nullable<string>; group_children?: string[];
  fidelity?: Fidelity; unrendered?: string[]; data_key?: string;
  columns: Column[]; rows: Value[][]; formatted_rows?: (string | null)[][]; selected: Value[]; truncated: boolean;
  sort?: { index: number; descending: boolean }[];
  formatting?: Formatting; error?: string; needs_desktop?: boolean; unsupported_reason?: string;
  // Round 3 (contract sections 16 to 23).
  action?: Nullable<Action>; tooltipPage?: Nullable<{ page: string; title?: string; width?: number; height?: number }>;
  syncGroup?: Nullable<{ groupName: string; fieldChanges?: boolean; filterChanges?: boolean }>;
  extra?: unknown[];
  totals?: Nullable<{ values: Value[]; formatted?: (string | null)[] }>;
  highlights?: Nullable<Value[][]>; referenceLines?: Nullable<ReferenceLine[]>; smallMultiples?: Nullable<SmallMultiplesData>;
  dynamic?: Nullable<Record<string, Value>>; dataNotes?: Nullable<string[]>; declined?: boolean;
};

/** The server's `live_report.DATA_KEYS`: what a formatting-only answer never carries, so a merge keeps the cached ones. */
export const DATA_KEYS = ["rows", "formatted_rows", "selected", "truncated", "sort", "extra", "totals", "highlights", "referenceLines", "smallMultiples", "dynamic", "dataNotes", "declined"] as const;
export type FormattingRow = Omit<RenderVisual, "id" | typeof DATA_KEYS[number]>;

export type Theme = {
  name?: string; dataColors?: string[]; palette?: string[]; foreground?: string; background?: string;
  textClasses?: Record<string, { fontSize?: number; fontSizePx?: number; fontFamily?: string; color?: string }>;
};
export type PageStyle = { color?: string; alpha?: number; wallpaper?: { color?: string; alpha?: number }; image?: string; scaling?: string };
export type BoundField = { field: string; table?: string; column?: string };
export type PageBinding = { type: string; fields: BoundField[]; keepAllFilters?: boolean; crossReport?: boolean };
export type PageMeta = { id: string; title: string; width: number; height: number; hidden?: boolean; page_type?: string; binding?: Nullable<PageBinding> };
export type Bookmark = {
  name: string; displayName?: string; group?: Nullable<{ name: string; displayName?: string }>; page?: Nullable<string>;
  targets?: Nullable<string[]>; captures?: { data?: boolean; display?: boolean; page?: boolean }; hidden?: string[]; shown?: string[];
};
export type AppliedBookmark = { name: string; displayName?: string; page?: Nullable<string>; captures?: Bookmark["captures"]; notes?: string[] };
export type Drillthrough = { page: string; title?: string; fields: BoundField[]; keepAllFilters?: boolean; crossReport?: boolean };
export type MobileLayout = { visuals: Record<string, { x: number; y: number; width: number; height: number; z?: number }> };
export type SyncMember = { page: string; visual: string; hidden?: boolean };

/** What the page response adds around the visuals (contract sections 1, 16 to 20 and 23). */
export type PageEnvelope = {
  page_style?: PageStyle; theme?: Theme; revision?: string | null; head_hash?: string; page?: PageMeta;
  bookmarks?: Bookmark[]; bookmark?: Nullable<AppliedBookmark>; interactions?: Record<string, Record<string, string>>;
  sync_groups?: Record<string, SyncMember[]>; mobile?: Nullable<MobileLayout>; drillthroughs?: Drillthrough[]; notes?: string[];
};
export type RenderResult = PageEnvelope & { visuals: RenderVisual[]; elapsed_ms?: number; engine?: string };
export type FormattingResponse = PageEnvelope & { visuals: Record<string, FormattingRow>; elapsed_ms?: number };
const ENVELOPE_KEYS = ["page_style", "theme", "revision", "head_hash", "page", "bookmarks", "bookmark", "interactions", "sync_groups", "mobile", "drillthroughs", "notes"] as const;

export type OverlayRect = { id: string; x: number; y: number; width: number; height: number; rank: number; hidden: boolean; kind: string; parentGroup: string | null };
export type OverlayContext = {
  scale: number; page: { width: number; height: number }; rects: OverlayRect[];
  toPage: (clientX: number, clientY: number) => { x: number; y: number };
};
export type LayoutVisual = {
  id: string; title: string; x: number; y: number; width: number; height: number;
  // Present once the imported-report payload carries them; the canvas works without.
  visual_type?: string; z?: number; hidden?: boolean; parent_group?: Nullable<string>; container_kind?: string;
};

/** Power BI's own default data colors: the last resort when a payload has no theme yet. */
export const DEFAULT_DATA_COLORS = ["#118DFF", "#12239E", "#E66C37", "#6B007B", "#E044A7", "#744EC2", "#D9B300", "#D64550"];

// --- Safe values -----------------------------------------------------------------------------------

const COLOR = /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
const FONT = /^[\w\s,"'.-]{1,240}$/;
const IMAGE = /^data:image\/(?:png|jpeg|gif|svg\+xml)(?:;[\w=.-]+)*;base64,[A-Za-z0-9+/=\s]+$/i;

export function safeColor(value: Nullable<string>): string | undefined {
  return typeof value === "string" && COLOR.test(value) ? value : undefined;
}

export function safeFont(value: Nullable<string>): string | undefined {
  return typeof value === "string" && FONT.test(value) ? value : undefined;
}

/** A data URI for an `<img>` or a CSS `url()`. Anything else (a web address, script) is dropped. */
export function safeImage(value: Nullable<string>): string | undefined {
  return typeof value === "string" && value.length <= 8_000_000 && IMAGE.test(value) ? value : undefined;
}

function px(value: Nullable<number>): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function rgba(color: string): { hex: string; alpha: number } {
  let hex = color.slice(1);
  if (hex.length <= 4) hex = [...hex].map(c => c + c).join("");
  return { hex: hex.slice(0, 6).toLowerCase(), alpha: hex.length === 8 ? parseInt(hex.slice(6), 16) / 255 : 1 };
}

/** `#RRGGBB` with an extra alpha folded in (multiplied with any alpha the color already has). */
export function withAlpha(color: Nullable<string>, alpha: number): string | undefined {
  const safe = safeColor(color);
  if (!safe) return undefined;
  const parsed = rgba(safe);
  const value = Math.round(Math.min(1, Math.max(0, parsed.alpha * alpha)) * 255);
  return `#${parsed.hex}${value.toString(16).padStart(2, "0")}`;
}

/** The color without its alpha: a sticky table header needs an opaque surface. */
export function opaque(color: Nullable<string>): string | undefined {
  const safe = safeColor(color);
  return safe ? `#${rgba(safe).hex}` : undefined;
}

export function themePalette(theme: Nullable<Theme>): string[] {
  if (theme?.palette?.length) return theme.palette;
  return ["#FFFFFF", "#000000", ...seriesPalette(theme).slice(0, 8)];
}

export function seriesPalette(theme: Nullable<Theme>): string[] {
  return theme?.dataColors?.length ? theme.dataColors : DEFAULT_DATA_COLORS;
}

// --- Text -------------------------------------------------------------------------------------------

export function display(value: Value): string {
  if (value === null) return "(Blank)";
  if (typeof value === "number") return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return String(value);
}

export function cellText(visual: Pick<RenderVisual, "formatted_rows" | "rows">, row: number, column: number): string {
  return visual.formatted_rows?.[row]?.[column] ?? display(visual.rows[row][column]);
}

/** Data label text. A label format of its own (Power BI's "Custom" display units) wins; otherwise display
 * units and decimals apply to plain numbers, and percent formats keep their text. */
export function formatDataLabel(raw: number, text: string, labels: Nullable<LabelsToken>, format?: string): string {
  if (labels?.format) return formatWithString(raw, labels.format);
  const percent = !!format && format.includes("%");
  const units = labels?.displayUnits ?? null;
  const decimals = labels?.decimals ?? null;
  if (!percent && units && units > 1) {
    return (raw / units).toLocaleString("en-US", { minimumFractionDigits: decimals ?? 0, maximumFractionDigits: decimals ?? 1 }) + unitSuffix(units);
  }
  if (decimals !== null) {
    const digits = { minimumFractionDigits: decimals, maximumFractionDigits: decimals };
    return percent ? `${(raw * 100).toLocaleString("en-US", digits)}%` : raw.toLocaleString("en-US", digits);
  }
  return text;
}

// --- Stacking, hidden, hit testing -----------------------------------------------------------------

/** `rank` orders every visual of the page (z, then tab order, then file order); the raw z is never used. */
export function sortByRank<T extends { rank?: number }>(visuals: readonly T[]): T[] {
  return visuals.map((visual, index) => ({ visual, index })).sort((a, b) => (a.visual.rank ?? Infinity) - (b.visual.rank ?? Infinity) || a.index - b.index).map(item => item.visual);
}

export type DrawState = "draw" | "ghost" | "skip";

/** Hidden visuals are not drawn in view mode and are ghosts in edit mode. Groups never draw a box of their own. */
export function drawState(hidden: boolean | undefined, mode: "view" | "edit"): DrawState {
  if (!hidden) return "draw";
  return mode === "edit" ? "ghost" : "skip";
}

/** The rectangles selection and hit testing use. Position precedence: an in-flight drag override, then the
 * rendered row (group members already carry page coordinates), then the imported layout, so nothing depends
 * on the payload having loaded. Rows the layout does not list are still drawn; layout entries without a row
 * rank above the rows so they stay reachable. */
export function layoutRects(
  layout: readonly LayoutVisual[] | undefined,
  visuals: readonly RenderVisual[],
  override?: Record<string, Rect>,
): OverlayRect[] {
  const rows = new Map(visuals.map(visual => [visual.id, visual]));
  const fromLayout = new Map((layout ?? []).map(item => [item.id, item]));
  const ranked = sortByRank(visuals);
  const maxRank = ranked.reduce((most, visual) => Math.max(most, visual.rank ?? 0), 0);
  const rects: OverlayRect[] = [];
  for (const row of ranked) {
    const base = fromLayout.get(row.id);
    const rect = override?.[row.id] ?? row.position ?? (base ? { x: base.x, y: base.y, width: base.width, height: base.height } : { x: 0, y: 0, width: 0, height: 0 });
    rects.push({
      id: row.id, ...rect, rank: row.rank ?? maxRank + rects.length + 1, hidden: !!(row.hidden ?? base?.hidden),
      kind: row.container_kind === "group" ? "group" : row.kind, parentGroup: row.parent_group ?? base?.parent_group ?? null,
    });
  }
  const extra = (layout ?? []).map((item, index) => ({ item, index })).filter(({ item }) => !rows.has(item.id))
    .sort((a, b) => (a.item.z ?? 0) - (b.item.z ?? 0) || a.index - b.index);
  extra.forEach(({ item }, offset) => {
    const rect = override?.[item.id] ?? { x: item.x, y: item.y, width: item.width, height: item.height };
    rects.push({ id: item.id, ...rect, rank: maxRank + offset + 1, hidden: !!item.hidden, kind: item.container_kind === "group" ? "group" : "unknown", parentGroup: item.parent_group ?? null });
  });
  return rects;
}

/** Selectable visuals under a point, topmost first. Hidden visuals and group containers are skipped:
 * a group's members are selected one by one. */
export function hitCandidates(rects: readonly OverlayRect[], point: { x: number; y: number }): OverlayRect[] {
  return rects.map((rect, index) => ({ rect, index }))
    .filter(({ rect }) => !rect.hidden && rect.kind !== "group" && point.x >= rect.x && point.x < rect.x + rect.width && point.y >= rect.y && point.y < rect.y + rect.height)
    .sort((a, b) => b.rect.rank - a.rect.rank || b.index - a.index)
    .map(({ rect }) => rect);
}

/** The visual a click selects: the topmost by rank; with Alt, the next one underneath the selected visual
 * (wrapping to the top). Empty canvas gives null. */
export function hitTest(rects: readonly OverlayRect[], point: { x: number; y: number }, options: { altKey?: boolean; selectedId?: string | null } = {}): string | null {
  const stack = hitCandidates(rects, point);
  if (!stack.length) return null;
  if (!options.altKey) return stack[0].id;
  const at = stack.findIndex(rect => rect.id === options.selectedId);
  return stack[at < 0 ? 0 : (at + 1) % stack.length].id;
}

/** Client (screen) coordinates to page pixels, from the canvas' on-screen box. The measured ratio is used
 * instead of the zoom setting so it stays right however the browser applies CSS zoom. */
export function clientToPage(box: { left: number; top: number; width: number }, page: { width: number }, scale: number, clientX: number, clientY: number): { x: number; y: number } {
  const ratio = box.width > 0 && page.width > 0 ? box.width / page.width : scale || 1;
  return { x: (clientX - box.left) / ratio, y: (clientY - box.top) / ratio };
}

// --- Requests, revisions ---------------------------------------------------------------------------

/** In-flight requests are shared by path, body and head: a new head starts a new request. */
export function pendingKey(path: string, body: string, headId?: string): string {
  return `${path}${body}${headId ?? ""}`;
}

/** A response is stale when it echoes a revision that is not the current head. A server that does not echo
 * one (null or missing) cannot be judged and is accepted. */
export function isStaleResponse(echo: Nullable<string>, headId: Nullable<string>): boolean {
  return !!headId && echo !== null && echo !== undefined && echo !== headId;
}

/** The render answered "the report moved on" (HTTP 409 head_moved). `api()` only keeps the message, so the
 * status and code are read when the error carries them and the message otherwise. */
export function isHeadMoved(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const { status, code, message } = error as { status?: unknown; code?: unknown; message?: unknown };
  if (code === "head_moved") return true;
  const text = typeof message === "string" ? message : "";
  // The sidecar's own sentence for a render of a head that is no longer current.
  if (/head_moved|head moved|report changed while (this view|it) was loading/i.test(text)) return true;
  return status === 409 && /revision|head|newer|changed|out of date/i.test(text);
}

/** `formatting.image` (a new card's measure-driven graphic) comes from a query, so `render-formatting` never
 * has it. It stays valid while the row still points at the same measure (`imageIndex`). */
function keepQueryFormatting(before: Formatting | undefined, next: Formatting | undefined): Formatting | undefined {
  const image = before?.image;
  if (!next || !image || next.image || next.imageIndex == null || next.imageIndex !== before?.imageIndex) return next;
  return { ...next, image };
}

export const FORMATTING_KEEPS = DATA_KEYS;

const FIDELITY_ORDER: Fidelity[] = ["exact", "approximate", "none"];
function worst(a: Fidelity | undefined, b: Fidelity | undefined): Fidelity | undefined {
  if (!a) return b;
  if (!b) return a;
  return FIDELITY_ORDER.indexOf(a) >= FIDELITY_ORDER.indexOf(b) ? a : b;
}

/** One cached row with a formatting row laid over it (contract sections 10 and 23): every data key stays the
 * cached one; a row the engine declined keeps its error, `needs_desktop` and `fidelity: "none"` (the formatting
 * route runs no engine and cannot know), and a row whose data carries notes stays at least approximate. */
export function mergeRow(before: RenderVisual, row: FormattingRow): RenderVisual {
  const layout: Record<string, unknown> = { ...row };
  for (const key of DATA_KEYS) delete layout[key];
  const merged: RenderVisual = { ...before, ...(layout as FormattingRow), id: before.id, formatting: keepQueryFormatting(before.formatting, row.formatting) };
  if (before.declined) {
    merged.error = before.error; merged.needs_desktop = before.needs_desktop; merged.fidelity = "none";
  } else if (before.dataNotes?.length) {
    merged.fidelity = worst(row.fidelity, "approximate");
  }
  return merged;
}

/** Contract section 10: replace formatting, columns, position, rank and fidelity of a cached row when its
 * `data_key` did not move, keep its data. Rows whose data changed (or that are new) come back in `refetch`.
 * The answer always describes the whole page (the page never sends `only`): one edit re-ranks every visual
 * and a group's move or hide changes its members, so no cached row can be kept on trust. Cached rows the
 * answer omits were deleted. */
export function mergeFormatting(current: RenderResult | undefined, response: FormattingResponse): { result: RenderResult | undefined; refetch: string[] } {
  if (!current) return { result: current, refetch: Object.keys(response.visuals) };
  const cached = new Map(current.visuals.map(visual => [visual.id, visual]));
  const refetch: string[] = [];
  const merged = new Map<string, RenderVisual>();
  for (const [id, row] of Object.entries(response.visuals)) {
    const before = cached.get(id);
    if (before && before.data_key === row.data_key && before.columns.length === row.columns.length) merged.set(id, mergeRow(before, row));
    else refetch.push(id);
  }
  const visuals = sortByRank([...merged.values()]);
  const envelope: Record<string, unknown> = {};
  // A key the answer carries replaces the cached one, `null` included (no bookmark, no phone layout).
  for (const key of ENVELOPE_KEYS) if (response[key] !== undefined) envelope[key] = response[key];
  return {
    result: {
      ...current, ...envelope, visuals, page_style: response.page_style ?? current.page_style, theme: response.theme ?? current.theme,
      revision: response.revision ?? current.revision, head_hash: response.head_hash ?? current.head_hash, page: response.page ?? current.page,
    },
    refetch,
  };
}

export function withQuery(path: string, params: readonly (readonly string[])[]): string {
  return params.length ? `${path}?${params.map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`).join("&")}` : path;
}

export type RenderRun = {
  post: <T>(path: string, body: string, headId?: string) => Promise<T>;
  path: string; formattingPath: string; requestBody: Record<string, unknown>; headId?: string;
  /** The head as it is when a response arrives, not when the request left. */
  head: () => string | undefined;
  /** The rows drawn now: formatting is merged into these. */
  current: () => RenderResult | undefined;
  /** Only the formatting changed since the last render: ask `render-formatting` (no queries, whole page). */
  formattingOnly: boolean;
  cancelled: () => boolean;
  onResult: (result: RenderResult) => void; onError: (reason: unknown) => void; onMoved: () => void;
};

/** One render of a page (contract sections 9 and 10). A response for another head is dropped and reported
 * through `onMoved`; a formatting-only answer is merged into the cache, and anything whose data key moved,
 * or any failure of the formatting route, falls back to drawing the whole page from a full render. */
export async function runRender(run: RenderRun): Promise<void> {
  // The revision travels as a query parameter: the body is the closed selections/searches model.
  const body = JSON.stringify(run.requestBody);
  const revision = run.headId ? [["revision", run.headId]] : [];
  const full = async (): Promise<void> => {
    let next: RenderResult;
    try {
      next = await run.post<RenderResult>(withQuery(run.path, revision), body, run.headId);
    } catch (reason) {
      if (run.cancelled()) return;
      return isHeadMoved(reason) ? run.onMoved() : run.onError(reason);
    }
    if (run.cancelled()) return;
    if (isStaleResponse(next.revision, run.head())) return run.onMoved();
    run.onResult(next);
  };
  if (!run.formattingOnly) return full();
  // Never `only`: the route runs no DAX, and an arrange, group move or group hide changes rows the edit did not name.
  let response: FormattingResponse;
  try {
    response = await run.post<FormattingResponse>(withQuery(run.formattingPath, revision), body, run.headId);
  } catch (reason) {
    if (run.cancelled()) return;
    return isHeadMoved(reason) ? run.onMoved() : full();
  }
  if (run.cancelled()) return;
  if (isStaleResponse(response.revision, run.head())) return run.onMoved();
  const { result, refetch } = mergeFormatting(run.current(), response);
  if (!result || refetch.length) return full();
  run.onResult(result);
}

// --- Style objects ---------------------------------------------------------------------------------

type Sides = [number, number, number, number];

/** Default inner padding of a visual's box by kind (px, top right bottom left) when the file sets none. */
export function defaultPadding(kind: string, compact: boolean): Sides {
  if (kind === "shape" || kind === "image" || kind === "group" || kind === "button" || kind === "navigator") return [0, 0, 0, 0];
  if (kind === "card") return [0, 0, 0, 0];
  if (compact) return [3, 6, 3, 6];
  return kind === "textbox" ? [5, 5, 5, 5] : [12, 12, 12, 12];
}

function sides(padding: Nullable<Nullable<number>[]>, fallback: Sides): Sides {
  return [0, 1, 2, 3].map(i => px(padding?.[i]) ?? fallback[i]) as Sides;
}

/** The visual's box from `container`. Without the token (an older engine) nothing is set and the legacy
 * stylesheet look applies. */
export function containerStyle(container: Nullable<ContainerToken>, kind: string, compact: boolean): CSSProperties {
  if (!container) return {};
  const [top, right, bottom, left] = sides(container.padding, defaultPadding(kind, compact));
  const border = container.border;
  const width = border?.show ? px(border.width) ?? 1 : 0;
  return {
    backgroundColor: safeColor(container.background) ?? "transparent",
    border: width ? `${width}px solid ${safeColor(border?.color) ?? "#000000"}` : "none",
    borderRadius: border?.show ? px(border.radius) ?? 0 : 0,
    boxShadow: container.shadow ? "0 2px 8px #00000040" : "none",
    paddingTop: top, paddingRight: right, paddingBottom: bottom, paddingLeft: left,
  };
}

function align(value: Nullable<string>): CSSProperties["textAlign"] {
  return value === "left" || value === "center" || value === "right" ? value : undefined;
}

function font(token: Nullable<FontFields>): CSSProperties {
  return {
    fontSize: px(token?.fontSize), color: safeColor(token?.color), fontFamily: safeFont(token?.family),
    fontWeight: token?.bold === undefined ? undefined : token.bold ? 700 : 400,
    fontStyle: token?.italic ? "italic" : undefined, textAlign: align(token?.alignment),
  };
}

export const fontStyle = font;

/** One tile of a tile slicer in its current state: the tile's fill and outline, the value's font and the
 * corner shape. Undefined without tile tokens (an older engine), so the legacy stylesheet look applies. */
export function slicerTileStyle(formatting: Nullable<Formatting>, selected: boolean): CSSProperties | undefined {
  const tile = formatting?.tile;
  const values = formatting?.slicer?.values;
  if (!tile && !values) return undefined;
  const state = (selected ? tile?.selected : null) ?? tile?.default;
  const text = (selected ? values?.selected : null) ?? values?.default;
  const layout = formatting?.slicer?.layout;
  const shape = layout?.shape;
  const radius = shape === "oval" || shape === "pill" ? 9999 : shape === "rectangle" ? 0 : px(layout?.radius) ?? 0;
  return { ...tileStyle(state, [16, 16, 16, 16]), ...font(text), borderRadius: radius, ...(text?.show === false ? { color: "transparent" } : {}) };
}

/** The tile grid of a tile slicer (`rows` are implied by the item count). */
export function slicerGridStyle(formatting: Nullable<Formatting>): CSSProperties | undefined {
  const layout = formatting?.slicer?.layout;
  const columns = px(layout?.columns);
  if (!columns || columns < 1) return undefined;
  return { display: "grid", gridTemplateColumns: `repeat(${Math.floor(columns)}, minmax(0, 1fr))`, gap: px(layout?.cellPadding) ?? 8 };
}

export type TitleView = { show: boolean; text: string; style: CSSProperties; dynamic: boolean };

/** A value a measure gives for the whole visual (contract section 21), when the formatting lists the path. */
export function dynamicValue(visual: Pick<RenderVisual, "formatting" | "dynamic">, path: string): Value | undefined {
  const value = visual.dynamic?.[path];
  return value === undefined || value === null ? undefined : value;
}

function fromTitleToken(token: TitleToken, fallback: string, dynamic?: Nullable<Record<string, Value>>): TitleView {
  const measured = dynamic?.["title.text"];
  const isDynamic = token.textSource === "dynamic";
  // Without the measure's value (its piece failed) a measure-driven title shows the visual's name.
  const text = typeof measured === "string" ? measured : token.text || fallback;
  const color = typeof dynamic?.["title.fontColor"] === "string" ? dynamic["title.fontColor"] : token.fontColor;
  const background = typeof dynamic?.["title.background"] === "string" ? dynamic["title.background"] : token.background;
  return {
    show: !!token.show, dynamic: isDynamic, text,
    style: {
      ...font({ fontSize: token.fontSize, color, family: token.fontFamily, bold: token.bold, italic: token.italic, alignment: token.alignment }),
      textDecoration: token.underline ? "underline" : undefined, backgroundColor: safeColor(background),
      whiteSpace: token.wrap === false ? "nowrap" : "normal",
    },
  };
}

/** A card or slicer names itself through its label or header (`titleView`), but its container can carry a
 * title of its own too (`title.show`, a Format-pane or chat edit). That one is a separate row above the tile. */
export function containerTitleView(visual: Pick<RenderVisual, "title" | "kind" | "formatting"> & { dynamic?: RenderVisual["dynamic"] }): TitleView | null {
  const token = visual.formatting?.title;
  if ((visual.kind !== "card" && visual.kind !== "slicer") || !token?.show) return null;
  return fromTitleToken(token, visual.title, visual.dynamic);
}

/** The container token with a measure-driven background (`background.color` in `dynamic`) laid over it. */
export function effectiveContainer(visual: Pick<RenderVisual, "formatting" | "dynamic">): Nullable<ContainerToken> {
  const container = visual.formatting?.container;
  const measured = visual.dynamic?.["background.color"];
  if (!container || typeof measured !== "string" || !safeColor(measured)) return container;
  return { ...container, background: measured };
}

/** What the title row draws for a visual: the typed `title` token, or the legacy keys for an older engine. */
export function titleView(visual: Pick<RenderVisual, "title" | "kind" | "formatting"> & { dynamic?: RenderVisual["dynamic"] }): TitleView {
  const f = visual.formatting;
  const token = f?.title;
  if (token && visual.kind !== "card" && visual.kind !== "slicer") return fromTitleToken(token, visual.title, visual.dynamic);
  if (visual.kind === "card") {
    const label = f?.cardLabel;
    return { show: label ? label.show !== false : !!f?.showLabel, dynamic: false, text: label?.text || f?.label || visual.title, style: font(label) };
  }
  if (visual.kind === "slicer") {
    const header = f?.slicer?.header;
    return { show: header ? header.show !== false : true, dynamic: false, text: header?.text || f?.label || visual.title, style: font(header) };
  }
  return { show: !!f?.titleVisible, dynamic: false, text: visual.title, style: {} };
}

export type TableView = {
  header: CSSProperties; cell: CSSProperties; rowPadding: number | undefined;
  horizontal: string | undefined; vertical: string | undefined;
  /** Grid line widths in px (the token's weights, else 1). */
  horizontalWeight: number; verticalWeight: number;
  /** Word wrap of the headers and of the cells (Power BI wraps headers by default, not values). */
  headerWrap: boolean; valuesWrap: boolean;
  banding: { primary?: string; secondary?: string; secondaryColor?: string };
};

export function tableView(table: Nullable<TableToken>, tableGridLegacy: boolean | undefined): TableView {
  const grid = table?.grid;
  const horizontal = table ? grid?.horizontal ? safeColor(grid.horizontalColor) ?? "#e6e6e6" : undefined : tableGridLegacy === false ? undefined : "#eef1f5";
  const weight = (value: Nullable<number>) => { const w = px(value); return w !== undefined && w >= 0 ? w : 1; };
  return {
    header: { ...font({ fontSize: table?.header?.fontSize, color: table?.header?.color, family: table?.header?.family, alignment: table?.header?.alignment }), backgroundColor: safeColor(table?.header?.background) },
    cell: font({ fontSize: table?.values?.fontSize, color: table?.values?.color, family: table?.values?.family }),
    rowPadding: px(grid?.rowPadding), horizontal,
    vertical: grid?.vertical ? safeColor(grid.verticalColor) ?? "#e6e6e6" : undefined,
    horizontalWeight: weight(grid?.horizontalWeight), verticalWeight: weight(grid?.verticalWeight),
    headerWrap: table?.header?.wrap !== false, valuesWrap: table?.values?.wrap === true,
    banding: { primary: safeColor(table?.values?.banding?.primary), secondary: safeColor(table?.values?.banding?.secondary), secondaryColor: safeColor(table?.values?.secondaryColor) },
  };
}

/** A card tile / tile slicer state: fill, outline and padding. `fallback` supplies the sides the file leaves null. */
export function tileStyle(state: Nullable<TileState>, fallback: Sides): CSSProperties {
  const [top, right, bottom, left] = sides(state?.padding, fallback);
  const outline = state?.outline;
  const width = outline?.show ? px(outline.width) ?? 1 : 0;
  const alpha = 1 - Math.min(100, Math.max(0, px(outline?.transparency) ?? 0)) / 100;
  return {
    backgroundColor: state?.fillShown === false ? "transparent" : safeColor(state?.fill) ?? "transparent",
    border: width ? `${width}px solid ${withAlpha(outline?.color, alpha) ?? "#e6e6e6"}` : "none",
    paddingTop: top, paddingRight: right, paddingBottom: bottom, paddingLeft: left,
  };
}

/** Old canvas measurement of a new card's padding inside its tile; explicit tokens override it. */
export const CARD_TILE_PADDING: Sides = [24, 24, 20, 40];
export const CARD_TILE_PADDING_COMPACT: Sides = [3, 6, 3, 6];

export function cardValueStyle(token: Nullable<FontFields>, legacySize: number | undefined): CSSProperties {
  return { ...font(token), fontSize: px(token?.fontSize) ?? legacySize };
}

export function runStyle(run: TextRun): CSSProperties {
  return {
    fontFamily: safeFont(run.fontFamily), fontSize: px(run.fontSize), color: safeColor(run.color),
    fontWeight: run.bold ? 700 : undefined, fontStyle: run.italic ? "italic" : undefined,
    textDecoration: run.underline || run.hasLink ? "underline" : undefined,
  };
}

export function paragraphStyle(paragraph: TextParagraph): CSSProperties {
  return { textAlign: align(paragraph.align), margin: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere" };
}

/** The drawable subset of `shape.shape`: rectangle, rounded rectangle, oval, pill and line. */
export function shapeStyle(shape: Nullable<ShapeToken>, box: { width: number; height: number }): CSSProperties {
  const kind = shape?.shape ?? "rectangle";
  const outline = shape?.outline;
  const width = outline?.show ? px(outline.width) ?? 1 : 0;
  const color = safeColor(outline?.color) ?? "#000000";
  const rotation = px(shape?.rotation);
  const transform = rotation ? `rotate(${rotation}deg)` : undefined;
  if (kind === "line") {
    return { position: "absolute", left: 0, right: 0, top: "50%", height: 0, borderTop: `${width || 1}px solid ${safeColor(shape?.fill) ?? color}`, transform };
  }
  const radius = kind === "oval" ? "50%" : kind === "pill" ? Math.max(box.width, box.height) : kind === "rectangleRounded" || kind === "rectangleRoundedByPixel" ? px(shape?.radius) ?? 8 : 0;
  return {
    position: "absolute", inset: 0, backgroundColor: safeColor(shape?.fill) ?? "transparent",
    border: width ? `${width}px solid ${color}` : "none", borderRadius: radius, transform,
  };
}

export function pictureFit(scaling: Nullable<string>): CSSProperties["objectFit"] {
  return scaling === "fill" ? "cover" : scaling === "fit" ? "contain" : "none";
}

export type PageLayer = { key: string; style: CSSProperties };

/** Wallpaper behind the page color, the page color over it, then the background image. */
export function pageLayers(page: Nullable<PageStyle>): PageLayer[] {
  if (!page) return [];
  const layers: PageLayer[] = [];
  const wallpaper = withAlpha(page.wallpaper?.color, page.wallpaper?.alpha ?? 1);
  if (wallpaper) layers.push({ key: "wallpaper", style: { backgroundColor: wallpaper } });
  const color = withAlpha(page.color, page.alpha ?? 1);
  if (color) layers.push({ key: "page", style: { backgroundColor: color } });
  const image = safeImage(page.image);
  if (image) {
    layers.push({
      key: "image",
      style: {
        backgroundImage: `url("${image}")`, backgroundRepeat: "no-repeat",
        backgroundSize: page.scaling === "Fill" ? "cover" : page.scaling === "Normal" ? "auto" : "contain",
        backgroundPosition: page.scaling === "Normal" ? "top left" : "center",
      },
    });
  }
  return layers;
}

// --- Fidelity --------------------------------------------------------------------------------------

/** `categoryAxis.innerPadding` -> "category axis: inner padding". */
export function describeUnrendered(names: readonly string[]): string {
  const words = (text: string) => text.replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase();
  const items = names.map(name => {
    const [object, ...rest] = name.split(".");
    return rest.length ? `${words(object)}: ${words(rest.join(" "))}` : words(object);
  });
  return items.length > 8 ? `${items.slice(0, 8).join(", ")} and ${items.length - 8} more` : items.join(", ");
}

export type FidelityNote = { level: "approximate" | "none" | "desktop"; label: string; detail: string; data?: boolean };

/** The honest badge of a visual. The server's own plain reasons come first: `error` for a visual Power BI
 * must draw, `dataNotes` when the values themselves are approximate; `unrendered` names the formatting
 * the canvas does not draw. `data` marks a note about the numbers (shown in view mode too). */
export function fidelityNote(visual: Pick<RenderVisual, "fidelity" | "unrendered" | "error" | "needs_desktop" | "unsupported_reason" | "kind"> & { dataNotes?: RenderVisual["dataNotes"] }): FidelityNote | null {
  if (visual.needs_desktop) return { level: "desktop", label: visual.error || "This page filter needs Power BI Desktop", detail: "Power BI Desktop can draw this visual." };
  if (visual.unsupported_reason || visual.kind === "unsupported" || visual.fidelity === "none") {
    return { level: "none", label: "Preview in Power BI", detail: visual.unsupported_reason || visual.error || "Studio does not draw this visual." };
  }
  const notes = (visual.dataNotes ?? []).filter(note => typeof note === "string" && note.trim());
  if (notes.length) {
    const formatting = visual.unrendered?.length ? ` Not drawn here: ${describeUnrendered(visual.unrendered)}.` : "";
    const sentences = notes.map(note => /[.!?]$/.test(note.trim()) ? note.trim() : `${note.trim()}.`);
    return { level: "approximate", label: "Values may differ from Power BI", detail: `${sentences.join(" ")}${formatting}`, data: true };
  }
  if (visual.fidelity === "approximate") {
    return { level: "approximate", label: "Some formatting is shown in Power BI only", detail: visual.unrendered?.length ? describeUnrendered(visual.unrendered) : "Part of this visual's formatting is not drawn here." };
  }
  return null;
}

// --- Chart ------------------------------------------------------------------------------------------

function sameValue(entry: CategoryColor, actual: Value): boolean {
  const value = entry.value;
  if (entry.type === "datetime") return typeof actual === "string" && dateNumber(String(value)) === dateNumber(actual) && !Number.isNaN(dateNumber(actual));
  if (typeof value === "number" && typeof actual === "number") return value === actual;
  if (value === null || actual === null) return value === actual;
  return String(value) === String(actual);
}

/** Dates arrive as `2019-01-01`, `2019-01-01T00:00:00` or with a space; all mean the same wall-clock time. */
export function dateNumber(text: string): number {
  const found = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}):(\d{2}))?/.exec(text);
  if (!found) return Number.NaN;
  const [, y, mo, d, h = "0", mi = "0", s = "0"] = found;
  return Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s));
}

export type ColorScope = { refs: string[]; value: Value };
export type ColorContext = { scopes: ColorScope[]; measureRefs: string[]; seriesIndex: number; categoryIndex: number };

/** Contract section 3 `series`: the entry that colors this mark, else the measure's own color, else the
 * default, else the theme's data color for the series index (or category index when colors vary by category). */
export function markColor(series: Nullable<SeriesToken>, theme: Nullable<Theme>, context: ColorContext): string {
  const palette = seriesPalette(theme);
  for (const entry of series?.categoryColors ?? []) {
    const color = safeColor(entry.color);
    if (!color) continue;
    if (entry.metadata && !context.measureRefs.includes(entry.metadata)) continue;
    if (context.scopes.some(scope => (!entry.column || scope.refs.includes(entry.column)) && sameValue(entry, scope.value))) return color;
  }
  if (series?.varyByCategory) return palette[context.categoryIndex % palette.length];
  for (const ref of context.measureRefs) {
    const color = safeColor(series?.seriesColors?.[ref]);
    if (color) return color;
  }
  return safeColor(series?.defaultColor) ?? palette[context.seriesIndex % palette.length];
}

export type ChartPoint = {
  /** The index of the row in `visual.rows`: what a right-click or hover resolves the data point from. */
  row: number;
  category: Value; categoryLabel: string; series: string; measureRef: string; value: number; formatted: string; label: string; color: string;
  /** The highlighted part of the value (contract section 19): null when nothing of the row is selected, absent without highlights. */
  highlight?: number | null; opacity?: number;
  start?: number; end?: number; mid?: number;
};
export type ChartPlan = { points: ChartPoint[]; categoryIndex: number; measures: number[]; seriesNames: string[]; matched: boolean; vary: boolean; highlighted: boolean };

function refsOf(column: Column | undefined): string[] {
  if (!column) return [];
  return [column.query_ref ?? undefined, column.table ? `${column.table}.${column.name}` : undefined, column.name].filter((ref): ref is string => !!ref);
}

/** The value columns a chart draws: measures and visual calculations (contract section 21) that hold numbers. */
export function valueColumns(visual: Pick<RenderVisual, "columns" | "rows">): number[] {
  return visual.columns.map((c, i) => !c.hidden && (c.kind === "measure" || (c.kind === "computed" && c.computed === "visualCalculation")) && visual.rows.some(r => typeof r[i] === "number") ? i : -1).filter(i => i >= 0);
}

/** The column a chart's categories come from: the first shown field column. */
export function categoryColumn(visual: Pick<RenderVisual, "columns">): number {
  return visual.columns.findIndex(c => c.kind === "column" && !c.hidden);
}

/** A conditional data color of this row for this measure (contract section 12): the row's value at the rule's index. */
function ruleColor(rules: readonly RuleColor[], row: readonly Value[], refs: readonly string[]): string | undefined {
  for (const rule of rules) {
    if (rule.metadata && !refs.includes(rule.metadata)) continue;
    const value = row[rule.index];
    const color = typeof value === "string" ? safeColor(value) : undefined;
    if (color) return color;
  }
  return undefined;
}

/** The category, series and measure columns of a chart row, and one point per row and measure. */
export function planChart(visual: RenderVisual, theme: Nullable<Theme>): ChartPlan | null {
  const category = categoryColumn(visual);
  const measures = valueColumns(visual);
  if (category < 0 || !measures.length) return null;
  const series = visual.columns.findIndex((c, i) => i !== category && c.kind === "column" && !c.hidden);
  const token = visual.formatting?.series;
  const legacy = visual.formatting?.categoryColors;
  const base: SeriesToken | undefined = token ?? (legacy?.length ? { categoryColors: legacy } : undefined);
  const vary = visual.kind === "pie" || visual.kind === "donut" || !!base?.varyByCategory;
  const typed: SeriesToken | undefined = vary ? { ...base, varyByCategory: true } : base;
  const rules = (token?.ruleColors ?? []).filter(rule => Number.isInteger(rule.index) && rule.index >= 0 && rule.index < visual.columns.length);
  const highlights = visual.highlights?.length ? visual.highlights : null;
  const usedMeasures = vary ? measures.slice(0, 1) : measures;
  const names: string[] = [];
  const categories = new Map<string, number>();
  const points: ChartPoint[] = [];
  let matched = false;
  visual.rows.forEach((row, rowIndex) => {
    const key = JSON.stringify(row[category]);
    if (!categories.has(key)) categories.set(key, categories.size);
    for (const measure of usedMeasures) {
      const value = row[measure];
      if (typeof value !== "number") continue;
      const seriesLabel = series >= 0 ? cellText(visual, rowIndex, series) : visual.columns[measure].label;
      const name = series >= 0 && usedMeasures.length > 1 ? `${seriesLabel} - ${visual.columns[measure].label}` : seriesLabel;
      if (!names.includes(name)) names.push(name);
      const scopes: ColorScope[] = [{ refs: refsOf(visual.columns[category]), value: row[category] }];
      if (series >= 0) scopes.push({ refs: refsOf(visual.columns[series]), value: row[series] });
      const refs = refsOf(visual.columns[measure]);
      const context: ColorContext = { scopes, measureRefs: refs, seriesIndex: names.indexOf(name), categoryIndex: categories.get(key) ?? 0 };
      // A rule's color wins over every other (contract section 3 `ruleColors`).
      const ruled = ruleColor(rules, row, refs);
      const color = ruled ?? markColor(typed, theme, context);
      if (ruled || (!vary && typed?.categoryColors?.some(entry => scopes.some(scope => sameValue(entry, scope.value))))) matched = true;
      const text = cellText(visual, rowIndex, measure);
      const point: ChartPoint = {
        row: rowIndex, category: row[category], categoryLabel: cellText(visual, rowIndex, category), series: name, measureRef: refs[0] ?? "", value, formatted: text,
        label: formatDataLabel(value, text, visual.formatting?.labels, visual.columns[measure].format), color,
      };
      if (highlights) {
        const part = highlights[rowIndex]?.[measure];
        point.highlight = typeof part === "number" ? part : null;
        point.opacity = point.highlight === null ? DIMMED : 1;
      }
      points.push(point);
    }
  });
  return { points, categoryIndex: category, measures: usedMeasures, seriesNames: names, matched, vary, highlighted: !!highlights };
}

/** How much a mark that the selection does not reach is dimmed (Power BI draws it at about a third). */
export const DIMMED = 0.35;

/** Drops unset keys (Vega-Lite rejects `undefined`); a `null` is kept because `title: null` means "no title". */
function compact<T extends Record<string, unknown>>(value: T): Record<string, unknown> {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== undefined));
}

function axisSpec(token: Nullable<AxisToken>, fallbackTitle: string | undefined, legacyTitle: boolean | undefined, legacyGrid: boolean | undefined, defaultGrid: boolean, labelExpr?: string): Record<string, unknown> {
  if (!token) return compact({ title: legacyTitle === false ? null : fallbackTitle, grid: legacyGrid ?? defaultGrid, labelExpr });
  const hidden = token.show === false;
  return compact({
    title: !hidden && token.title ? token.titleText || fallbackTitle : null,
    titleColor: safeColor(token.titleColor), titleFontSize: px(token.titleFontSize), titleFont: safeFont(token.family),
    labels: hidden ? false : undefined, ticks: hidden ? false : undefined, domain: hidden ? false : undefined,
    labelColor: safeColor(token.color), labelFontSize: px(token.fontSize), labelFont: safeFont(token.family),
    grid: token.gridlines ?? defaultGrid, gridColor: safeColor(token.gridlineColor), labelExpr,
  });
}

function legendSpec(token: Nullable<LegendToken>): Record<string, unknown> | null {
  if (token && token.show === false) return null;
  const position = token?.position;
  return {
    title: null, symbolType: "square",
    ...compact({
      orient: position === "top" || position === "bottom" || position === "left" || position === "right" ? position : undefined,
      labelColor: safeColor(token?.color), labelFontSize: px(token?.fontSize), labelFont: safeFont(token?.family),
    }),
  };
}

function labelMark(kind: string, horizontal: boolean, stacked: boolean, token: Nullable<LabelsToken>): Record<string, unknown> {
  const position = token?.position ?? "Auto";
  const base = compact({ type: "text", color: safeColor(token?.color), fontSize: px(token?.fontSize), font: safeFont(token?.family), fontWeight: token?.bold ? "bold" : undefined, fontStyle: token?.italic ? "italic" : undefined });
  if (stacked || position === "InsideCenter") return { ...base, baseline: "middle", align: "center" };
  if (kind === "line" || kind === "area") return { ...base, baseline: "bottom", dy: -6 };
  const inside = position === "InsideEnd" || position === "InsideBase";
  return horizontal
    ? { ...base, baseline: "middle", align: inside ? "right" : "left", dx: inside ? -4 : 4 }
    : { ...base, baseline: inside ? "top" : "bottom", dy: inside ? 4 : -4 };
}

// --- Analytics lines (contract section 15) ------------------------------------------------------------

const LINE_NAMES: Record<string, string> = {
  constant: "Constant line", measure: "Constant line", min: "Min line", max: "Max line", average: "Average line",
  median: "Median line", percentile: "Percentile line", trend: "Trend line",
};

/** The text of an analytics line's label: its value (with the label's decimals and units), its name, or both. */
export function lineText(line: ReferenceLine, format?: string): string {
  const name = line.name || LINE_NAMES[line.kind] || "Line";
  const value = typeof line.value === "number"
    ? formatUnits(line.value, { displayUnits: line.label?.displayUnits, decimals: line.label?.decimals, format })
    : line.value === null || line.value === undefined ? "" : String(line.value);
  const which = line.label?.text ?? "Value";
  if (which === "Name" || !value) return name;
  return which === "ValueAndName" ? `${name} ${value}` : value;
}

function lineMark(type: string, line: ReferenceLine, color: string): Record<string, unknown> {
  const dash = line.style === "dotted" ? [1, 3] : line.style === "dashed" || line.style === "custom" ? [6, 4] : undefined;
  const alpha = typeof line.alpha === "number" && Number.isFinite(line.alpha) ? Math.min(1, Math.max(0, line.alpha)) : undefined;
  return compact({ type, color, strokeWidth: px(line.width) ?? 1, strokeDash: dash, strokeCap: line.style === "dotted" ? "round" : undefined, opacity: alpha, tooltip: true });
}

/** The category label an X-axis constant line stands at: the drawn category equal to its value (dates by wall-clock time). */
function categoryAt(points: readonly ChartPoint[], value: Value | undefined): string | undefined {
  if (value === undefined) return undefined;
  const date = typeof value === "string" ? dateNumber(value) : Number.NaN;
  return points.find(point => point.category === value || (point.category !== null && value !== null && String(point.category) === String(value))
    || (!Number.isNaN(date) && typeof point.category === "string" && dateNumber(point.category) === date))?.categoryLabel;
}

export type LineLayers = { back: Record<string, unknown>[]; front: Record<string, unknown>[]; usesCategories: boolean };

/** The analytics lines of a column, bar, line or area chart as Vega-Lite layers. Each layer brings its own
 * data and switches off every channel it would otherwise inherit from the chart (`null` drops a channel), so
 * a rule is never offset, colored by series or given the bars' range. */
export function lineLayers(lines: readonly ReferenceLine[] | null | undefined, plan: ChartPlan, options: { horizontal: boolean; inherited: readonly string[]; format?: string; palette: readonly string[] }): LineLayers {
  const result: LineLayers = { back: [], front: [], usesCategories: false };
  if (!lines?.length) return result;
  const value = options.horizontal ? "x" : "y";
  const dimension = options.horizontal ? "y" : "x";
  const off = Object.fromEntries(options.inherited.map(key => [key, null]));
  const labels = [...new Set(plan.points.map(point => point.categoryLabel))];
  const seriesColor = (ref: Nullable<string>) => ref ? plan.points.find(point => point.measureRef === ref)?.color : undefined;
  for (const line of lines) {
    if (!line || typeof line !== "object") continue;
    const color = safeColor(line.color) ?? seriesColor(line.series) ?? (line.kind === "trend" ? options.palette[0] : undefined) ?? "#605E5C";
    const text = lineText(line, options.format);
    const tooltip = { value: text === (line.name || LINE_NAMES[line.kind]) ? text : `${line.name || LINE_NAMES[line.kind] || "Line"}: ${text}` };
    const layers: Record<string, unknown>[] = [];
    if (line.kind === "trend") {
      if (typeof line.start !== "number" || typeof line.end !== "number" || !labels.length) continue;
      layers.push({
        data: { values: [{ categoryLabel: labels[0], ruleValue: line.start }, { categoryLabel: labels[labels.length - 1], ruleValue: line.end }] },
        mark: lineMark("line", line, color),
        encoding: { ...off, [dimension]: { field: "categoryLabel", type: "nominal" }, [value]: { field: "ruleValue", type: "quantitative" }, tooltip },
      });
      result.usesCategories = true;
    } else if (line.axis === "category") {
      const at = categoryAt(plan.points, line.value ?? undefined);
      if (at === undefined) continue;
      layers.push({ data: { values: [{ categoryLabel: at, text }] }, mark: lineMark("rule", line, color), encoding: { ...off, [dimension]: { field: "categoryLabel", type: "nominal" }, tooltip } });
      if (line.label?.show) {
        layers.push({
          data: { values: [{ categoryLabel: at, text }] },
          mark: compact({ type: "text", color: safeColor(line.label.color) ?? color, fontSize: 11, align: "left", baseline: "top", dx: 3, dy: 3 }),
          encoding: { ...off, [dimension]: { field: "categoryLabel", type: "nominal" }, [value]: { value: options.horizontal ? "width" : 0 }, text: { field: "text", type: "nominal" } },
        });
      }
      result.usesCategories = true;
    } else {
      if (typeof line.value !== "number" || !Number.isFinite(line.value)) continue;
      layers.push({ data: { values: [{ ruleValue: line.value, text }] }, mark: lineMark("rule", line, color), encoding: { ...off, [value]: { field: "ruleValue", type: "quantitative" }, tooltip } });
      if (line.label?.show) {
        const right = line.label.horizontal === "right";
        const under = line.label.vertical === "under" || line.label.vertical === "below";
        const place = options.horizontal
          ? { [dimension]: { value: under ? "height" : 0 } }
          : { [dimension]: { value: right ? "width" : 0 } };
        const mark = options.horizontal
          ? { align: "left", baseline: under ? "bottom" : "top", dx: 3 }
          : { align: right ? "right" : "left", baseline: under ? "top" : "bottom", dy: under ? 3 : -3, dx: right ? -2 : 2 };
        layers.push({
          data: { values: [{ ruleValue: line.value, text }] },
          mark: compact({ type: "text", color: safeColor(line.label.color) ?? color, fontSize: 11, ...mark }),
          encoding: { ...off, [value]: { field: "ruleValue", type: "quantitative" }, ...place, text: { field: "text", type: "nominal" } },
        });
      }
    }
    (line.position === "back" ? result.back : result.front).push(...layers);
  }
  return result;
}

export type ChartBuild = { spec: Record<string, unknown>; config: Record<string, unknown>; formatString?: string };
export type ChartOptions = {
  /** False when the visual shows a report page as its tooltip: the canvas draws that instead of Vega's. */
  tooltip?: boolean;
  /** A value axis shared by small-multiple panels. */
  valueDomain?: [number, number];
};

/** The Vega-Lite spec of a column, bar, line, area, pie or donut row, drawn from the typed tokens, with its
 * highlights (section 19) and analytics lines (section 15). */
export function buildChart(visual: RenderVisual, theme: Nullable<Theme>, options: ChartOptions = {}): ChartBuild | null {
  const plan = planChart(visual, theme);
  if (!plan) return null;
  const f = visual.formatting;
  const { points, categoryIndex, measures, seriesNames } = plan;
  const kind = visual.kind;
  const categoryTitle = visual.columns[categoryIndex]?.label;
  const valueTitle = measures.length === 1 ? visual.columns[measures[0]].label : "Value";
  const formatString = visual.columns[measures[0]]?.format;
  const horizontal = kind === "bar";
  const stacking = f?.chart?.stacking ?? (kind === "column" || kind === "bar" ? "clustered" : "none");
  const multi = seriesNames.length > 1;
  const legend = f?.legend;
  const data = { values: points };
  const showTips = options.tooltip !== false;
  const tooltip = [{ field: "categoryLabel", title: categoryTitle }, { field: "series", title: "Measure / series" }, { field: "formatted", type: "nominal", title: "Value" }];
  const config: Record<string, unknown> = compact({ font: safeFont(f?.axes?.value?.family) ?? safeFont(f?.labels?.family) });

  if (kind === "pie" || kind === "donut") {
    const colors = new Map<string, string>();
    for (const point of points) if (!colors.has(point.categoryLabel)) colors.set(point.categoryLabel, point.color);
    const color = { field: "categoryLabel", type: "nominal", sort: null, scale: { domain: [...colors.keys()], range: [...colors.values()] }, legend: legendSpec(legend) };
    const radius = { expr: "min(width, height) / 2" };
    const arc = compact({ type: "arc", tooltip: showTips, cursor: "pointer", outerRadius: radius, innerRadius: kind === "donut" ? { expr: "min(width, height) / 2 * 0.55" } : undefined });
    const layers: Record<string, unknown>[] = [{ mark: arc }];
    if (f?.labels?.show) {
      layers.push({ mark: { ...labelMark(kind, false, false, f.labels), baseline: "middle", align: "center", radius: { expr: kind === "donut" ? "min(width, height) / 2 * 0.78" : "min(width, height) / 2 * 0.7" } }, encoding: { text: { field: "label", type: "nominal" }, color: { value: safeColor(f.labels.color) ?? "#252423" } } });
    }
    const encoding: Record<string, unknown> = { theta: { field: "value", type: "quantitative", stack: true }, color };
    if (showTips) encoding.tooltip = tooltip;
    // A slice the selection does not reach is dimmed; the ones it reaches keep their color.
    if (plan.highlighted) encoding.opacity = { field: "opacity", type: "quantitative", scale: null, legend: null };
    return { spec: { data, layer: layers, encoding }, config, formatString };
  }

  const isBar = kind === "column" || kind === "bar";
  // Stacked bars are stacked here rather than by Vega-Lite: the first series sits at the bottom (as in
  // Power BI) and a label can be centered on its own segment.
  const stackedBars = isBar && stacking !== "clustered" && multi;
  const drawn = stackedBars ? stackSegments(points, seriesNames) : points;
  const labels = [...new Set(points.map(point => point.categoryLabel))];
  const dimension: Record<string, unknown> = { field: "categoryLabel", type: "nominal", sort: null, axis: axisSpec(f?.axes?.category, categoryTitle, f?.categoryAxisTitle, undefined, false) };
  const stack = stackedBars || kind === "line" || (kind === "area" && stacking !== "stacked") || (isBar && stacking === "clustered") ? null : "zero";
  const unitsExpr = formatString && isPercentFormat(formatString) ? undefined : axisUnitsExpr(f?.axes?.value?.displayUnits, f?.axes?.value?.decimals);
  const metric: Record<string, unknown> = { field: stackedBars ? "start" : "value", type: "quantitative", stack, axis: axisSpec(f?.axes?.value, valueTitle, f?.valueAxisTitle, f?.grid, true, unitsExpr) };
  if (options.valueDomain) metric.scale = { domain: options.valueDomain };
  const valueChannel = horizontal ? "x" : "y";
  const encoding: Record<string, unknown> = { [horizontal ? "y" : "x"]: dimension, [valueChannel]: metric };
  if (showTips) encoding.tooltip = tooltip;
  const labelled = !!f?.labels?.show;
  // The range end belongs to the bars only: a text mark cannot take it.
  const range = stackedBars ? { [`${valueChannel}2`]: { field: "end" } } : {};
  if (!labelled) Object.assign(encoding, range);
  // Overlay marks take the series color explicitly: Vega-Lite would paint them its own default blue.
  const first = points[0]?.color;
  const mark: Record<string, unknown> = { type: kind === "line" ? "line" : kind === "area" ? "area" : "bar", tooltip: showTips, cursor: "pointer" };
  // Marks that take their own color: a category or rule entry matched, or "vary colors by category" on
  // bars (a line has one stroke, so it keeps the first color).
  const perMark = plan.matched || (plan.vary && isBar);
  const single = !multi && !perMark && !!first;
  if (kind === "line") { mark.strokeWidth = px(f?.line?.strokeWidth) ?? 2; mark.point = f?.line?.markers || !f?.line ? (single ? { filled: true, color: first } : true) : false; }
  if (kind === "area") mark.line = single ? { color: first } : true;
  if (isBar) mark.cornerRadiusEnd = 2;

  if (perMark) {
    // Rule-, category- or vary-driven colors are per mark, so a series legend cannot describe them. Clustered
    // series still need their own slot in the category, or the series bars would sit on top of each other.
    encoding.color = { field: "color", type: "nominal", scale: null, legend: null };
    if (isBar && multi && stacking === "clustered") encoding[horizontal ? "yOffset" : "xOffset"] = { field: "series", type: "nominal", sort: null };
  } else if (multi) {
    const colors = seriesNames.map(name => points.find(point => point.series === name)?.color ?? DEFAULT_DATA_COLORS[0]);
    encoding.color = { field: "series", type: "nominal", sort: null, scale: { domain: seriesNames, range: colors }, legend: legendSpec(legend) };
    if (isBar && stacking === "clustered") encoding[horizontal ? "yOffset" : "xOffset"] = { field: "series", type: "nominal", sort: null };
  } else {
    mark.color = first;
  }

  // Highlights (contract section 19): the full value dimmed and the highlighted part in full color on top. A
  // stacked segment cannot hold a part of itself, so it is dimmed as a whole when nothing of it is selected.
  let highlightLayer: Record<string, unknown> | undefined;
  if (plan.highlighted && isBar) {
    if (stackedBars) encoding.opacity = { field: "opacity", type: "quantitative", scale: null, legend: null };
    else {
      mark.opacity = DIMMED;
      highlightLayer = { transform: [{ filter: "isValid(datum.highlight)" }], mark: { ...mark, opacity: 1 }, encoding: { [valueChannel]: { field: "highlight", type: "quantitative" } } };
    }
  }

  const lines = lineLayers(visual.referenceLines, plan, { horizontal, inherited: Object.keys(encoding), format: formatString, palette: seriesPalette(theme) });
  // Layers that bring categories of their own must not reorder the axis: the domain is the drawn order.
  if (lines.usesCategories) dimension.scale = { domain: labels };
  const layered = labelled || !!highlightLayer || lines.back.length > 0 || lines.front.length > 0;
  if (!layered) return { spec: { data: { values: drawn }, mark, encoding }, config, formatString };

  const main: Record<string, unknown> = { mark, ...(stackedBars && labelled ? { encoding: range } : {}) };
  const layers: Record<string, unknown>[] = [...lines.back, main];
  if (highlightLayer) layers.push(highlightLayer);
  let padding: Record<string, number> | undefined;
  if (labelled && f?.labels) {
    const text = labelMark(kind, horizontal, stackedBars, f.labels);
    const at = stackedBars ? { [valueChannel]: { field: "mid", type: "quantitative", stack: null } } : {};
    layers.push({ mark: text, encoding: { ...at, text: { field: "label", type: "nominal" }, color: { value: safeColor(f.labels.color) ?? "#252423" } } });
    // Labels outside the marks need room past the plot's edge or they are clipped.
    const room = 8 + (px(f.labels.fontSize) ?? 12) * (horizontal ? Math.max(3, ...drawn.map(point => point.label.length)) * 0.6 : 1.4);
    padding = stackedBars || (f.labels.position === "InsideEnd" || f.labels.position === "InsideBase" || f.labels.position === "InsideCenter") ? undefined : horizontal ? { left: 5, top: 5, bottom: 5, right: Math.ceil(room) } : { left: 5, right: 5, bottom: 5, top: Math.ceil(room) };
  }
  layers.push(...lines.front);
  return { spec: { data: { values: drawn }, layer: layers, encoding, ...(padding ? { padding } : {}) }, config, formatString };
}

/** `start`, `end` and `mid` of each point in a stacked bar: positives stack up from zero, negatives down. */
export function stackSegments(points: readonly ChartPoint[], seriesOrder: readonly string[]): ChartPoint[] {
  const totals = new Map<string, { up: number; down: number }>();
  return points.map((point, index) => ({ point, index }))
    .sort((a, b) => seriesOrder.indexOf(a.point.series) - seriesOrder.indexOf(b.point.series) || a.index - b.index)
    .map(({ point }) => {
      const key = JSON.stringify(point.category);
      const total = totals.get(key) ?? { up: 0, down: 0 };
      const start = point.value >= 0 ? total.up : total.down;
      const end = start + point.value;
      if (point.value >= 0) total.up = end; else total.down = end;
      totals.set(key, total);
      return { ...point, start, end, mid: (start + end) / 2 };
    });
}

// --- Small multiples (contract section 21) --------------------------------------------------------------

export type Panel = { value: Value; label: string; visual: RenderVisual };

/** One chart row per small-multiple panel: the panel's rows (and highlights), the split column hidden so it is
 * neither the category nor a series. Panels arrive in ascending order. */
export function smallMultiplePanels(visual: RenderVisual): Panel[] {
  const data = visual.smallMultiples;
  const index = data?.index ?? visual.formatting?.smallMultiples?.index;
  if (!data?.panels?.length || typeof index !== "number" || index < 0 || index >= visual.columns.length) return [];
  const columns = visual.columns.map((column, i) => i === index ? { ...column, hidden: true } : column);
  return data.panels.map(panel => {
    const rows = (panel.rows ?? []).filter(row => Number.isInteger(row) && row >= 0 && row < visual.rows.length);
    const pick = <T,>(list: readonly T[] | null | undefined) => list ? rows.map(row => list[row]) : undefined;
    const first = rows[0];
    return {
      value: panel.value, label: first !== undefined ? cellText(visual, first, index) : display(panel.value),
      visual: {
        ...visual, id: `${visual.id}:${String(panel.value)}`, columns, rows: rows.map(row => visual.rows[row]),
        formatted_rows: pick(visual.formatted_rows) as (string | null)[][] | undefined, highlights: pick(visual.highlights ?? undefined) as Value[][] | undefined,
        referenceLines: null, smallMultiples: null,
      },
    };
  });
}

/** The value axis every panel shares (Power BI's default): the lowest and highest drawn value, with zero for bars. */
export function sharedDomain(panels: readonly Panel[], theme: Nullable<Theme>): [number, number] | undefined {
  let low = Infinity;
  let high = -Infinity;
  for (const panel of panels) {
    const plan = planChart(panel.visual, theme);
    if (!plan) continue;
    const kind = panel.visual.kind;
    const stacked = (kind === "column" || kind === "bar") && (panel.visual.formatting?.chart?.stacking ?? "clustered") !== "clustered" && plan.seriesNames.length > 1;
    const values = stacked ? stackSegments(plan.points, plan.seriesNames).flatMap(point => [point.start ?? 0, point.end ?? 0]) : plan.points.map(point => point.value);
    for (const value of values) { low = Math.min(low, value); high = Math.max(high, value); }
  }
  if (!Number.isFinite(low) || !Number.isFinite(high)) return undefined;
  return [Math.min(0, low), Math.max(0, high)];
}

/** The panel grid: the layout's rows and columns, or Power BI's automatic near-square grid. */
export function panelGrid(count: number, layout: Nullable<{ rows?: Nullable<number>; columns?: Nullable<number> }>): { columns: number; rows: number } {
  const wanted = px(layout?.columns);
  const columns = wanted && wanted >= 1 ? Math.min(Math.floor(wanted), Math.max(1, count)) : Math.max(1, Math.ceil(Math.sqrt(count)));
  return { columns, rows: Math.max(1, Math.ceil(count / columns)) };
}
