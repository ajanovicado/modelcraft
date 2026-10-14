// Pure helpers for what the canvas draws inside tables and cards (contract sections 2, 3, 12 to 14): the
// server computes every conditional color and icon per row, so the canvas only reads indexes, maps icon
// names to glyphs, lays out data bars, totals and stepped rows. Tested in tests/report-round3.test.mjs.
import type { CSSProperties } from "react";

import { cellText, display, fontStyle, safeColor, type Column, type DataBars, type RenderVisual, type TableColumnToken, type TableToken, type Value } from "./reportRender";

type Nullable<T> = T | null | undefined;

/** A row value that is a validated color, or undefined (null means "no format: your default"). */
export function colorAt(row: readonly Value[] | undefined, index: Nullable<number>): string | undefined {
  if (!row || typeof index !== "number" || index < 0 || index >= row.length) return undefined;
  const value = row[index];
  return typeof value === "string" ? safeColor(value) : undefined;
}

export function columnToken(table: Nullable<TableToken>, column: Pick<Column, "query_ref">): TableColumnToken | undefined {
  return column.query_ref ? table?.columns?.[column.query_ref] : undefined;
}

export type IconCell = { name: string; layout: "before" | "after" | "only"; alignment: "top" | "middle" | "bottom" };
export type CellLook = { color?: string; background?: string; icon?: IconCell };

/** How one table cell looks: a conditional color (the row's value at `colorIndex`/`backgroundIndex`) wins over
 * the column's own plain color, which wins over banding; the icon name is the row's value at `icon.index`. */
export function cellLook(visual: Pick<RenderVisual, "columns" | "rows" | "formatting">, row: number, column: number): CellLook {
  const values = visual.rows[row];
  const col = visual.columns[column];
  if (!col || !values) return {};
  const token = columnToken(visual.formatting?.table, col);
  const look: CellLook = {};
  const color = colorAt(values, col.colorIndex) ?? safeColor(token?.fontColor);
  const background = colorAt(values, col.backgroundIndex) ?? safeColor(token?.background);
  if (color) look.color = color;
  if (background) look.background = background;
  const icon = col.icon;
  const name = icon && typeof icon.index === "number" ? values[icon.index] : undefined;
  if (icon && typeof name === "string" && name) {
    look.icon = {
      name, layout: icon.layout === "after" || icon.layout === "only" ? icon.layout : "before",
      alignment: icon.alignment === "top" || icon.alignment === "bottom" ? icon.alignment : "middle",
    };
  }
  return look;
}

// --- Icons -------------------------------------------------------------------------------------------

export type IconGlyph = {
  shape: "arrow" | "circle" | "flag" | "star" | "triangle" | "diamond" | "dash" | "bars" | "pie" | "quadrant" | "symbol";
  color: string;
  /** Arrows: degrees from pointing right (-90 up, 90 down). Triangles: "up" or "down" via `direction`. */
  angle?: number; direction?: "up" | "down";
  /** Stars, pies and quadrants: how much is filled (0 to 1); bars: how many of four are lit. */
  fill?: number;
  /** Traffic lights with a dark rim or a dark outline. */
  rim?: "dark" | "outline";
  symbol?: "x" | "!" | "check";
};

const RED = "#D64550";
const YELLOW = "#F2C80F";
const GREEN = "#1AAB40";
const GREY = "#8A8886";
const DARK = "#252423";
const GOLD = "#F2B200";
const BLUE = "#118DFF";
const COLORS: Record<string, string> = { Red: RED, Yellow: YELLOW, Green: GREEN, Gray: GREY, Grey: GREY, Purple: "#8764B8", Black: DARK };
const LEVEL: Record<string, string> = { Low: RED, Medium: YELLOW, High: GREEN };
const ANGLES: Record<string, number> = { Up: -90, UpRight: -45, Right: 0, DownRight: 45, Down: 90 };
const ARROW_COLORS: Record<string, string> = { Up: GREEN, UpRight: "#8AB833", Right: YELLOW, DownRight: "#E66C37", Down: RED };
const BARS: Record<string, number> = { Empty: 0, Low: 1, Medium: 2, Medium2: 3, Full: 4 };
const PIES: Record<string, number> = { CircleEmpty: 0, Circle75: 0.25, CircleHalf: 0.5, Circle25: 0.75, CircleFilled: 1 };
const QUADRANTS: Record<string, number> = { Empty: 0, "25": 0.25, "50": 0.5, "75": 0.75, Full: 1 };

/** Power BI's icon names (the names of Desktop's icon sets, `report_edit_conditional.ICON_SETS`) to a glyph the
 * canvas draws. An unknown name draws nothing. */
export function iconGlyph(name: Value | undefined): IconGlyph | null {
  if (typeof name !== "string" || name.length > 80) return null;
  let m: RegExpExecArray | null;
  if ((m = /^(Colored|Grey|Gray)Arrow(UpRight|DownRight|Up|Down|Right)$/.exec(name))) return { shape: "arrow", angle: ANGLES[m[2]], color: m[1] === "Colored" ? ARROW_COLORS[m[2]] : GREY };
  if ((m = /^Circle(Red|Yellow|Green|Gray|Purple)(BlackBackground|BlackOutline)?PatternFill$/.exec(name))) return { shape: "circle", color: COLORS[m[1]], ...(m[2] ? { rim: m[2] === "BlackBackground" ? "dark" as const : "outline" as const } : {}) };
  if (name === "CircleBlackFill") return { shape: "circle", color: DARK };
  if ((m = /^Circle(Low|Medium|High)$/.exec(name))) return { shape: "circle", color: LEVEL[m[1]] };
  if (name === "SignLow") return { shape: "diamond", color: RED };
  if (name === "SignMedium") return { shape: "triangle", direction: "up", color: YELLOW };
  if ((m = /^Flag(Red|Yellow|Green)PatternFill$/.exec(name))) return { shape: "flag", color: COLORS[m[1]] };
  if ((m = /^CircleSymbol(Low|Medium|High)$/.exec(name))) return { shape: "circle", color: LEVEL[m[1]], symbol: m[1] === "Low" ? "x" : m[1] === "Medium" ? "!" : "check" };
  if ((m = /^Symbol(Low|Medium|High)$/.exec(name))) return { shape: "symbol", color: LEVEL[m[1]], symbol: m[1] === "Low" ? "x" : m[1] === "Medium" ? "!" : "check" };
  if ((m = /^Star(Low|Medium|High)(Light)?$/.exec(name))) return { shape: "star", color: m[2] ? GOLD : GREY, fill: m[1] === "Low" ? 0 : m[1] === "Medium" ? 0.5 : 1 };
  if (name === "TriangleLow") return { shape: "triangle", direction: "down", color: RED };
  if (name === "TriangleMedium") return { shape: "dash", color: YELLOW };
  if (name === "TriangleHigh") return { shape: "triangle", direction: "up", color: GREEN };
  if ((m = /^SignalBar(Empty|Low|Medium2|Medium|Full)(Colored)?$/.exec(name))) return { shape: "bars", fill: BARS[m[1]], color: m[2] ? BLUE : DARK };
  if (name in PIES) return { shape: "pie", fill: PIES[name], color: DARK };
  if ((m = /^Quadrant(Empty|25|50|75|Full)(Colored)?$/.exec(name))) return { shape: "quadrant", fill: QUADRANTS[m[1]], color: m[2] ? BLUE : DARK };
  return null;
}

// --- Data bars ---------------------------------------------------------------------------------------

export type BarRange = { min: number; max: number };
/** Percentages of the cell's width: where the bar starts, how wide it is, and where the axis is (null: no axis). */
export type BarGeometry = { left: number; width: number; axis: number | null; color: string | undefined; negative: boolean };

/** A column's bar bounds: the rule's own `min`/`max`, else the lowest and highest value of the column with 0 in
 * the range (the axis when there are negative values). */
export function dataBarRange(visual: Pick<RenderVisual, "columns" | "rows">, column: number): BarRange {
  const bars = visual.columns[column]?.dataBars;
  const values = visual.rows.map(row => row[column]).filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  const min = typeof bars?.min === "number" && Number.isFinite(bars.min) ? bars.min : Math.min(0, ...values);
  const max = typeof bars?.max === "number" && Number.isFinite(bars.max) ? bars.max : Math.max(0, ...values);
  return min <= max ? { min, max } : { min: max, max: min };
}

export function dataBarGeometry(value: number, bars: DataBars, range: BarRange): BarGeometry {
  const span = range.max - range.min || 1;
  const clamp = (v: number) => Math.min(range.max, Math.max(range.min, v));
  const base = clamp(0);
  const v = clamp(value);
  let left = (Math.min(v, base) - range.min) / span;
  const width = Math.abs(v - base) / span;
  let axis: number | null = range.min < 0 && range.max > 0 ? (0 - range.min) / span : null;
  if (bars.reverse) { left = 1 - left - width; if (axis !== null) axis = 1 - axis; }
  const negative = value < 0;
  return { left: left * 100, width: width * 100, axis: axis === null ? null : axis * 100, color: safeColor(negative ? bars.negative : bars.positive), negative };
}

// --- Totals ------------------------------------------------------------------------------------------

export type TotalsView = { cells: { index: number; text: string; numeric: boolean }[]; style: CSSProperties };

/** The totals row under a table (contract section 14): drawn when `total.show` is on and the row has `totals`.
 * Category cells are null; the first drawn column shows the total's label. */
export function totalsView(visual: Pick<RenderVisual, "columns" | "totals" | "formatting">, shown: readonly number[]): TotalsView | null {
  const totals = visual.totals;
  const token = visual.formatting?.table?.total;
  if (!totals || !Array.isArray(totals.values) || token?.show === false || !shown.length) return null;
  const cells = shown.map((index, position) => {
    const raw = totals.values[index];
    const text = totals.formatted?.[index] ?? (raw === null || raw === undefined ? "" : display(raw));
    return { index, text: position === 0 && (raw === null || raw === undefined) ? (token?.label ?? "Total") : text, numeric: typeof raw === "number" };
  });
  const style: CSSProperties = {
    ...fontStyle({ fontSize: token?.fontSize, color: token?.color, family: token?.family }),
    fontWeight: token?.bold === false ? 400 : 700, backgroundColor: safeColor(token?.background),
  };
  return { cells, style };
}

// --- Cards -------------------------------------------------------------------------------------------

/** A card's conditional colors (contract section 12): value and caption font colors and the new card's tile fill,
 * each the first row's value at its index. */
export function cardColors(visual: Pick<RenderVisual, "rows" | "formatting">): { value?: string; label?: string; fill?: string } {
  const row = visual.rows[0];
  const f = visual.formatting;
  const out: { value?: string; label?: string; fill?: string } = {};
  const value = colorAt(row, f?.cardValue?.colorIndex);
  const label = colorAt(row, f?.cardLabel?.colorIndex);
  const fill = colorAt(row, f?.tile?.default?.fillIndex);
  if (value) out.value = value;
  if (label) out.label = label;
  if (fill) out.fill = fill;
  return out;
}

// --- Matrix ------------------------------------------------------------------------------------------

export type SteppedRow = { key: string; level: number; label: string; row: number | null };

/** A stepped matrix layout from flat rows: the row-group columns become one indented header column, a parent
 * line whenever an outer group changes, and the values on the innermost line. */
export function steppedRows(visual: Pick<RenderVisual, "columns" | "rows" | "formatted_rows">): { groups: number[]; values: number[]; rows: SteppedRow[] } {
  const groups = visual.columns.map((c, i) => !c.hidden && c.kind === "column" ? i : -1).filter(i => i >= 0);
  const values = visual.columns.map((c, i) => !c.hidden && c.kind !== "column" ? i : -1).filter(i => i >= 0);
  const rows: SteppedRow[] = [];
  let previous: string[] = [];
  visual.rows.forEach((row, index) => {
    const path = groups.map(g => JSON.stringify(row[g]));
    for (let level = 0; level < groups.length - 1; level++) {
      if (path.slice(0, level + 1).join("|") !== previous.slice(0, level + 1).join("|")) {
        rows.push({ key: `${path.slice(0, level + 1).join("|")}#${index}`, level, label: cellText(visual, index, groups[level]), row: null });
      }
    }
    rows.push({ key: `${path.join("|")}#${index}`, level: Math.max(0, groups.length - 1), label: groups.length ? cellText(visual, index, groups[groups.length - 1]) : "", row: index });
    previous = path;
  });
  return { groups, values, rows };
}
