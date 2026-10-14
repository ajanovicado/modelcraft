// The Visual pane's round-3 editors as pure rules: conditional formatting (docs/imported-report-editing-api.md,
// `set_conditional_format`), analytics lines (`set_analytics_line`), alt text and the visual's report-page tooltip. Each
// editor keeps a draft the user fills in and turns it into one operation, or says in plain words what is missing.

import type { ColorValue, Operation } from "./importedEditing";
import { isChartKind, type Option, type VisualInfo } from "./importedFormat";

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const str = (value: unknown): string | undefined => typeof value === "string" && value.trim() ? value : undefined;
const num = (value: unknown): number | undefined => typeof value === "number" && Number.isFinite(value) ? value : undefined;

type Result = { ok: true; op: Operation } | { ok: false; message: string };
const fail = (message: string): Result => ({ ok: false, message });

/** A number the user typed: empty is "not given", anything else must be a number. */
function typed(raw: string | undefined): number | undefined | "bad" {
  const text = (raw ?? "").trim().replace(/,/g, "");
  if (!text) return undefined;
  const value = Number(text);
  return Number.isFinite(value) ? value : "bad";
}

// --- conditional formatting --------------------------------------------------------------------------

export type CfFamily = "table" | "chart" | "card";
export type CfTarget = "font_color" | "background" | "data_colors" | "icon" | "data_bars" | "web_url" | "title_color" | "visual_background";
export type CfStyle = "gradient" | "rules" | "field_value" | "data_bars" | "icons";

export function cfFamily(info: Pick<VisualInfo, "kind">): CfFamily | undefined {
  if (info.kind === "table" || info.kind === "matrix") return "table";
  if (info.kind === "card") return "card";
  if (isChartKind(info.kind)) return "chart";
  return undefined;
}

const TARGET_LABELS: Record<CfTarget, string> = {
  font_color: "Font color", background: "Background color", data_colors: "Data colors", icon: "Icons", data_bars: "Data bars",
  web_url: "Web address", title_color: "Title color", visual_background: "Visual background",
};
export const targetLabel = (target: CfTarget) => TARGET_LABELS[target];

/** What can be formatted on this visual, in Power BI's order. */
export function cfTargets(info: Pick<VisualInfo, "kind">): { value: CfTarget; label: string }[] {
  const family = cfFamily(info);
  const own: CfTarget[] = family === "table" ? ["background", "font_color", "data_bars", "icon", "web_url"]
    : family === "chart" ? ["data_colors"]
    : family === "card" ? ["font_color"] : [];
  const whole: CfTarget[] = family ? ["title_color", "visual_background"] : [];
  return [...own, ...whole].map(value => ({ value, label: family === "card" && value === "font_color" ? "Value color" : TARGET_LABELS[value] }));
}

const STYLE_LABELS: Record<CfStyle, string> = { gradient: "Gradient", rules: "Rules", field_value: "Field value", data_bars: "Data bars", icons: "Icon set" };
/** The format styles Power BI offers for a target ("Format style" in its dialog). */
export function cfStyles(target: CfTarget): { value: CfStyle; label: string }[] {
  const styles: CfStyle[] = target === "data_bars" ? ["data_bars"]
    : target === "icon" ? ["icons", "rules", "field_value"]
    : target === "web_url" || target === "title_color" || target === "visual_background" ? ["field_value"]
    : ["gradient", "rules", "field_value"];
  return styles.map(value => ({ value, label: STYLE_LABELS[value] }));
}

/** The columns a target can format: a table's shown columns, a chart's or card's measures; nothing for the whole visual. */
export function cfFields(info: Pick<VisualInfo, "kind" | "columns">, target: CfTarget): Option[] {
  if (target === "title_color" || target === "visual_background") return [];
  const family = cfFamily(info);
  const columns = (info.columns ?? []).filter(column => !column.hidden && column.kind !== "computed" && column.role !== "Formatting");
  const usable = family === "table" && target !== "web_url" && target !== "font_color" && target !== "background"
    ? columns.filter(column => column.kind === "measure" || target === "icon")
    : family === "table" ? columns : columns.filter(column => column.kind === "measure");
  const seen = new Set<string>();
  return usable.flatMap(column => {
    const value = column.label || column.name;
    if (!value || seen.has(value)) return [];
    seen.add(value);
    return [{ value, label: value }];
  });
}

export type RuleDraft = { min: string; max: string; unit: "number" | "percent"; color?: ColorValue; icon?: string; equals: string; blank: boolean };
export type CfDraft = {
  target: CfTarget; style: CfStyle; field?: string;
  minColor?: ColorValue; midColor?: ColorValue; maxColor?: ColorValue; useMid: boolean; minValue: string; midValue: string; maxValue: string;
  blank: "as_zero" | "no_color" | "specific_color"; blankColor?: ColorValue;
  rules: RuleDraft[]; measure?: string;
  positiveColor?: ColorValue; negativeColor?: ColorValue; axisColor?: ColorValue; direction: "left_to_right" | "right_to_left"; barOnly: boolean;
  iconSet: string; iconPosition: "left" | "right" | "icon_only"; iconAlignment: "top" | "middle" | "bottom";
  applyTo: "values" | "values_and_totals" | "totals";
};

export const emptyRule = (): RuleDraft => ({ min: "", max: "", unit: "number", color: "#D64550", equals: "", blank: false });

/** A fresh draft for a target: Power BI's own starting colors (light to dark blue, green data bars). */
export function newCfDraft(info: Pick<VisualInfo, "kind" | "columns">, target?: CfTarget): CfDraft {
  const chosen = target ?? cfTargets(info)[0]?.value ?? "font_color";
  return {
    target: chosen, style: cfStyles(chosen)[0].value, field: cfFields(info, chosen)[0]?.value,
    minColor: "#DEEFFF", midColor: "#9DC8F5", maxColor: "#118DFF", useMid: false, minValue: "", midValue: "", maxValue: "", blank: "as_zero",
    rules: [{ ...emptyRule(), min: "", max: "", color: "#D64550" }], measure: undefined,
    positiveColor: "#5B9BD5", negativeColor: "#D64550", axisColor: "#000000", direction: "left_to_right", barOnly: false,
    iconSet: "ThreeColoredArrows", iconPosition: "left", iconAlignment: "middle", applyTo: "values",
  };
}

/** Moves a rule up (-1) or down (+1); the first rule that matches decides, so order matters. */
export function moveRule(rules: RuleDraft[], index: number, step: -1 | 1): RuleDraft[] {
  const to = index + step;
  if (index < 0 || index >= rules.length || to < 0 || to >= rules.length) return rules;
  const next = [...rules];
  [next[index], next[to]] = [next[to], next[index]];
  return next;
}

function ruleOf(rule: RuleDraft, index: number, icons: boolean): { ok: true; rule: Record<string, unknown> } | { ok: false; message: string } {
  const where = `Rule ${index + 1}`;
  const out: Record<string, unknown> = {};
  if (icons) { if (!rule.icon) return { ok: false, message: `${where} has no icon.` }; out.icon = rule.icon; }
  else { if (!rule.color) return { ok: false, message: `${where} has no color.` }; out.color = rule.color; }
  if (rule.blank) return { ok: true, rule: { ...out, blank: true } };
  if (rule.equals.trim()) {
    const value = typed(rule.equals);
    return { ok: true, rule: { ...out, equals: value === "bad" || value === undefined ? rule.equals.trim() : value } };
  }
  const min = typed(rule.min), max = typed(rule.max);
  if (min === "bad" || max === "bad") return { ok: false, message: `${where}: the bounds must be numbers.` };
  if (min !== undefined && max !== undefined && min > max) return { ok: false, message: `${where}: the lower bound is above the upper bound.` };
  if (rule.unit === "percent" && ((min !== undefined && (min < 0 || min > 100)) || (max !== undefined && (max < 0 || max > 100)))) return { ok: false, message: `${where}: a percent is between 0 and 100.` };
  if (min !== undefined) out.min = min;
  if (max !== undefined) out.max = max;
  if (rule.unit === "percent") out.unit = "percent";
  return { ok: true, rule: out };
}

/** The `set_conditional_format` operation of a draft, or what is missing. */
export function cfOperation(visual: string, draft: CfDraft, fields: Option[] = []): Result {
  const op: Operation = { op: "set_conditional_format", visual, style: draft.style, target: draft.target };
  const wholeVisual = draft.target === "title_color" || draft.target === "visual_background";
  if (!wholeVisual) {
    if (!draft.field && fields.length) return fail("Pick the field to format.");
    if (draft.field) op.field = draft.field;
  }
  if (draft.target !== "data_colors" && !wholeVisual && draft.applyTo !== "values") op.apply_to = draft.applyTo;
  if (draft.style === "gradient") {
    if (!draft.minColor || !draft.maxColor) return fail("Pick the lowest and highest colors.");
    const min = typed(draft.minValue), mid = typed(draft.midValue), max = typed(draft.maxValue);
    if (min === "bad" || mid === "bad" || max === "bad") return fail("The values of the color scale must be numbers.");
    if (min !== undefined && max !== undefined && min >= max) return fail("The lowest value must be below the highest.");
    Object.assign(op, { min_color: draft.minColor, max_color: draft.maxColor });
    if (draft.useMid) {
      if (!draft.midColor) return fail("Pick the center color.");
      op.mid_color = draft.midColor;
      if (mid !== undefined) op.mid_value = mid;
    }
    if (min !== undefined) op.min_value = min;
    if (max !== undefined) op.max_value = max;
    op.blank = draft.blank;
    if (draft.blank === "specific_color") {
      if (!draft.blankColor) return fail("Pick the color for blank values.");
      op.blank_color = draft.blankColor;
    }
  } else if (draft.style === "rules") {
    if (!draft.rules.length) return fail("Add at least one rule.");
    const rules: Record<string, unknown>[] = [];
    for (const [index, rule] of draft.rules.entries()) {
      const read = ruleOf(rule, index, draft.target === "icon");
      if (!read.ok) return fail(read.message);
      rules.push(read.rule);
    }
    op.rules = rules;
    if (draft.target === "icon") Object.assign(op, { style: "icons", icon_position: draft.iconPosition, icon_alignment: draft.iconAlignment });
  } else if (draft.style === "field_value") {
    if (!draft.measure) return fail("Pick the measure whose values give the format.");
    op.measure = draft.measure;
  } else if (draft.style === "data_bars") {
    if (!draft.positiveColor) return fail("Pick the color of positive bars.");
    const min = typed(draft.minValue), max = typed(draft.maxValue);
    if (min === "bad" || max === "bad") return fail("The lowest and highest values must be numbers.");
    if (min !== undefined && max !== undefined && min >= max) return fail("The lowest value must be below the highest.");
    Object.assign(op, { positive_color: draft.positiveColor, direction: draft.direction, bar_only: draft.barOnly });
    if (draft.negativeColor) op.negative_color = draft.negativeColor;
    if (draft.axisColor) op.axis_color = draft.axisColor;
    if (min !== undefined) op.min_value = min;
    if (max !== undefined) op.max_value = max;
  } else {
    if (!draft.iconSet) return fail("Pick an icon set.");
    Object.assign(op, { icon_set: draft.iconSet, icon_position: draft.iconPosition, icon_alignment: draft.iconAlignment });
  }
  return { ok: true, op };
}

export const clearCfOperation = (visual: string, target: CfTarget, field?: string): Operation =>
  ({ op: "clear_conditional_format", visual, target, ...(field ? { field } : {}) });

export type ExistingRule = { target: CfTarget; field?: string; label: string };
/** The conditional formatting the visual has now (read from the render row), so each can be removed. */
export function existingRules(info: Pick<VisualInfo, "kind" | "columns" | "formatting">): ExistingRule[] {
  const found: ExistingRule[] = [];
  const columns = info.columns ?? [];
  const nameOf = (index: number) => columns[index]?.label || columns[index]?.name || "";
  for (const [index, column] of columns.entries()) {
    if (column.hidden) continue;
    const field = nameOf(index);
    if (typeof column.colorIndex === "number") found.push({ target: "font_color", field, label: `Font color of ${field}` });
    if (typeof column.backgroundIndex === "number") found.push({ target: "background", field, label: `Background of ${field}` });
    if (isRecord(column.icon)) found.push({ target: "icon", field, label: `Icons on ${field}` });
    if (column.dataBars) found.push({ target: "data_bars", field, label: `Data bars on ${field}` });
  }
  const formatting = info.formatting ?? {};
  const series = isRecord(formatting.series) ? formatting.series : {};
  if (Array.isArray(series.ruleColors) && series.ruleColors.length) found.push({ target: "data_colors", label: "Data colors by rule" });
  const cardValue = isRecord(formatting.cardValue) ? formatting.cardValue : {};
  if (typeof cardValue.colorIndex === "number") found.push({ target: "font_color", label: "Value color by rule" });
  const dynamic = Array.isArray(formatting.dynamicProperties) ? formatting.dynamicProperties.filter((p): p is string => typeof p === "string") : [];
  if (dynamic.includes("title.fontColor")) found.push({ target: "title_color", label: "Title color from a measure" });
  if (dynamic.includes("background.color")) found.push({ target: "visual_background", label: "Background from a measure" });
  return found;
}

// The icon sets of report_edit_conditional.ICON_SETS with Desktop's own words, and a glyph per icon for the picker preview.
export const ICON_SETS: { value: string; label: string; icons: string[] }[] = [
  ["ThreeColoredArrows", "3 colored arrows", ["ColoredArrowDown", "ColoredArrowRight", "ColoredArrowUp"]],
  ["ThreeGrayedArrows", "3 grey arrows", ["GreyArrowDown", "GreyArrowRight", "GreyArrowUp"]],
  ["ThreeTrafficLightsUnrimmed", "3 traffic lights", ["CircleRedPatternFill", "CircleYellowPatternFill", "CircleGreenPatternFill"]],
  ["ThreeTrafficLightsRimmed", "3 rimmed traffic lights", ["CircleRedBlackBackgroundPatternFill", "CircleYellowBlackBackgroundPatternFill", "CircleGreenBlackBackgroundPatternFill"]],
  ["ThreeTrafficLightsLightRimmed", "3 light-rimmed traffic lights", ["CircleRedBlackOutlinePatternFill", "CircleYellowBlackOutlinePatternFill", "CircleGreenBlackOutlinePatternFill"]],
  ["ThreeColoredShapes", "3 colored shapes", ["SignLow", "SignMedium", "CircleHigh"]],
  ["ThreeColoredFlags", "3 flags", ["FlagRedPatternFill", "FlagYellowPatternFill", "FlagGreenPatternFill"]],
  ["ThreeColoredSymbols", "3 circled symbols", ["CircleSymbolLow", "CircleSymbolMedium", "CircleSymbolHigh"]],
  ["ThreeUncircledColorSymbols", "3 symbols", ["SymbolLow", "SymbolMedium", "SymbolHigh"]],
  ["ThreeGrayStars", "3 grey stars", ["StarLow", "StarMedium", "StarHigh"]],
  ["ThreeGoldStars", "3 gold stars", ["StarLow", "StarMediumLight", "StarHighLight"]],
  ["ThreeTriangles", "3 triangles", ["TriangleLow", "TriangleMedium", "TriangleHigh"]],
  ["FourColoredArrows", "4 colored arrows", ["ColoredArrowDown", "ColoredArrowDownRight", "ColoredArrowUpRight", "ColoredArrowUp"]],
  ["FourGrayArrows", "4 grey arrows", ["GreyArrowDown", "GreyArrowDownRight", "GreyArrowUpRight", "GreyArrowUp"]],
  ["FourColoredCirclesBlackToRed", "4 circles, black to red", ["CircleBlackFill", "CircleGrayPatternFill", "CirclePurplePatternFill", "CircleRedPatternFill"]],
  ["FourGraySignalMeters", "4 grey signal bars", ["SignalBarLow", "SignalBarMedium", "SignalBarMedium2", "SignalBarFull"]],
  ["FourColoredSignalMeters", "4 colored signal bars", ["SignalBarLowColored", "SignalBarMediumColored", "SignalBarMedium2Colored", "SignalBarFullColored"]],
  ["FourColoredCircles", "4 colored circles", ["CircleBlackFill", "CircleRedPatternFill", "CircleYellowPatternFill", "CircleGreenPatternFill"]],
  ["FiveColoredArrows", "5 colored arrows", ["ColoredArrowDown", "ColoredArrowDownRight", "ColoredArrowRight", "ColoredArrowUpRight", "ColoredArrowUp"]],
  ["FiveArrows", "5 grey arrows", ["GreyArrowDown", "GreyArrowDownRight", "GreyArrowRight", "GreyArrowUpRight", "GreyArrowUp"]],
  ["FiveSignalMeters", "5 grey signal bars", ["SignalBarEmpty", "SignalBarLow", "SignalBarMedium", "SignalBarMedium2", "SignalBarFull"]],
  ["FiveColoredSignalMeters", "5 colored signal bars", ["SignalBarEmpty", "SignalBarLowColored", "SignalBarMediumColored", "SignalBarMedium2Colored", "SignalBarFullColored"]],
  ["FivePieCharts", "5 pie charts", ["CircleEmpty", "Circle75", "CircleHalf", "Circle25", "CircleFilled"]],
  ["FiveBoxes", "5 boxes", ["QuadrantEmpty", "Quadrant25", "Quadrant50", "Quadrant75", "QuadrantFull"]],
  ["FiveColoredBoxes", "5 colored boxes", ["QuadrantEmpty", "Quadrant25Colored", "Quadrant50Colored", "Quadrant75Colored", "QuadrantFullColored"]],
].map(([value, label, icons]) => ({ value: value as string, label: label as string, icons: icons as string[] }));

/** Every icon a rule can take (the ones the sets use), for the rules table's icon picker. */
export const ICON_NAMES: string[] = [...new Set(ICON_SETS.flatMap(set => set.icons))];

/** A small preview glyph and color for an icon name (the canvas has its own drawing; this is only the picker's). */
export function iconGlyph(name: string): { glyph: string; color: string } {
  const color = /Red/.test(name) || /Low$/.test(name) && /Symbol|Sign|Triangle/.test(name) ? "#D64550"
    : /Yellow|Medium/.test(name) && !/Signal/.test(name) ? "#D9B300"
    : /Green|High/.test(name) && !/Star/.test(name) ? "#1A9E5A"
    : /Gold|StarHighLight|StarMediumLight/.test(name) ? "#D9A300"
    : /Purple/.test(name) ? "#744EC2"
    : /Colored/.test(name) ? (/Down/.test(name) ? "#D64550" : /Up/.test(name) ? "#1A9E5A" : "#D9B300") : "#556172";
  const glyph = /ArrowUpRight/.test(name) ? "↗" : /ArrowDownRight/.test(name) ? "↘" : /ArrowUp/.test(name) ? "↑" : /ArrowDown/.test(name) ? "↓" : /ArrowRight/.test(name) ? "→"
    : /Star/.test(name) ? (/Low/.test(name) ? "☆" : "★") : /Flag/.test(name) ? "⚑" : /Triangle/.test(name) ? (/Low/.test(name) ? "▼" : /High/.test(name) ? "▲" : "▬")
    : /Signal/.test(name) ? (/Empty/.test(name) ? "▁" : /Low/.test(name) ? "▂" : /Medium2/.test(name) ? "▆" : /Medium/.test(name) ? "▄" : "█")
    : /Quadrant|Circle(Empty|25|Half|75|Filled)/.test(name) ? (/Empty/.test(name) ? "○" : /25/.test(name) ? "◔" : /50|Half/.test(name) ? "◑" : /75/.test(name) ? "◕" : "●")
    : /Symbol|Sign/.test(name) ? (/Low/.test(name) ? "✖" : /Medium/.test(name) ? "!" : "✔") : "●";
  return { glyph, color };
}

// --- analytics lines ---------------------------------------------------------------------------------------

export type AnalyticsKind = "constant" | "x_constant" | "min" | "max" | "average" | "median" | "percentile" | "trend" | "forecast";
const ANALYTICS: { value: AnalyticsKind; label: string; lineOnly?: boolean }[] = [
  { value: "constant", label: "Y-axis constant line" }, { value: "x_constant", label: "X-axis constant line", lineOnly: true },
  { value: "min", label: "Min line" }, { value: "max", label: "Max line" }, { value: "average", label: "Average line" },
  { value: "median", label: "Median line" }, { value: "percentile", label: "Percentile line" }, { value: "trend", label: "Trend line" },
  { value: "forecast", label: "Forecast", lineOnly: true },
];

/** The lines Power BI's Analytics pane offers for a chart (constant lines on the X axis and forecasts only on line and area charts). */
export function analyticsKinds(info: Pick<VisualInfo, "kind">): { value: AnalyticsKind; label: string }[] {
  if (!isChartKind(info.kind) || info.kind === "pie" || info.kind === "donut") return [];
  const lineLike = info.kind === "line" || info.kind === "area";
  return ANALYTICS.filter(item => !item.lineOnly || lineLike).map(({ value, label }) => ({ value, label }));
}
export const analyticsLabel = (kind: string) => ANALYTICS.find(item => item.value === kind)?.label ?? (kind === "measure" ? "Y-axis constant line (measure)" : kind);

export type AnalyticsLine = { id?: string; kind: string; name?: string; value?: number | string | null; color?: string; style?: string; series?: string; axis?: string };
/** The chart's analytics lines as the render row sends them (`referenceLines`, render contract section 15). */
export function readAnalyticsLines(row: unknown): AnalyticsLine[] {
  if (!isRecord(row) || !Array.isArray(row.referenceLines)) return [];
  return row.referenceLines.flatMap((raw): AnalyticsLine[] => {
    if (!isRecord(raw) || !str(raw.kind)) return [];
    const value = typeof raw.value === "number" || typeof raw.value === "string" ? raw.value : null;
    return [{ id: str(raw.id), kind: String(raw.kind), name: str(raw.name), value, color: str(raw.color), style: str(raw.style), series: str(raw.series), axis: str(raw.axis) }];
  });
}

export type LineDraft = {
  kind: AnalyticsKind; value: string; percentile: string; name: string; color?: ColorValue; style: "solid" | "dashed" | "dotted";
  measure?: string; dataLabel: boolean; position: "front" | "back"; forecastLength: string;
};
export const newLineDraft = (kind: AnalyticsKind = "constant"): LineDraft => ({ kind, value: "", percentile: "50", name: "", color: "#D64550", style: "dashed", dataLabel: false, position: "front", forecastLength: "10" });

/** The draft of a line the chart already has (so it can be edited in place). */
export function lineDraftOf(line: AnalyticsLine): LineDraft {
  const kind = (line.kind === "measure" ? "constant" : ANALYTICS.some(item => item.value === line.kind) ? line.kind : "constant") as AnalyticsKind;
  return { ...newLineDraft(kind), value: line.value === null || line.value === undefined ? "" : String(line.value), name: line.name ?? "", color: line.color?.slice(0, 7), style: line.style === "solid" || line.style === "dotted" ? line.style : "dashed" };
}

/** `set_analytics_line` for a new line, or for `existing` (named by its name, else its number among lines of its kind). */
export function analyticsOperation(visual: string, draft: LineDraft, existing?: { line: AnalyticsLine; number: number }): Result {
  const op: Operation = { op: "set_analytics_line", visual, kind: draft.kind };
  if (existing) op.line = existing.line.name ?? existing.number;
  else op.new = true;
  if (draft.kind === "constant" || draft.kind === "x_constant") {
    if (draft.measure) op.measure = draft.measure;
    else {
      const text = draft.value.trim();
      if (!text) return fail("Give the line's value.");
      const value = typed(text);
      if (value === "bad") {
        if (draft.kind === "x_constant" && /^\d{4}-\d{2}-\d{2}$/.test(text)) op.value = text;
        else return fail(draft.kind === "x_constant" ? "Give a number, or a date as YYYY-MM-DD." : "The value must be a number.");
      } else if (value !== undefined) op.value = value;
    }
  }
  if (draft.kind === "percentile") {
    const value = typed(draft.percentile);
    if (value === undefined || value === "bad" || value < 0 || value > 100) return fail("The percentile is a number from 0 to 100.");
    op.percentile = value;
  }
  if (draft.kind === "forecast") {
    const length = typed(draft.forecastLength);
    if (length === undefined || length === "bad" || length < 1) return fail("Forecast how many points ahead? Give a whole number.");
    op.forecast_length = Math.round(length);
  }
  if (draft.name.trim()) op.name = draft.name.trim().slice(0, 100);
  if (draft.color) op.color = draft.color;
  if (draft.kind !== "forecast") op.style = draft.style;
  op.position = draft.position;
  op.data_label = draft.dataLabel;
  return { ok: true, op };
}

export function removeLineOperation(visual: string, line: AnalyticsLine, number: number): Operation {
  const kind = line.kind === "measure" ? "constant" : line.kind;
  return { op: "remove_analytics_line", visual, kind, line: line.name ?? number };
}

/** Numbers each line among the lines of its kind (1-based), which is how an unnamed line is named in an operation. */
export function numberLines(lines: AnalyticsLine[]): { line: AnalyticsLine; number: number }[] {
  const counts = new Map<string, number>();
  return lines.map(line => {
    const kind = line.kind === "measure" ? "constant" : line.kind;
    const next = (counts.get(kind) ?? 0) + 1;
    counts.set(kind, next);
    return { line, number: next };
  });
}

// --- accessibility, tooltip page, number shown ----------------------------------------------------------

/** `set_alt_text`: empty text removes it. */
export function altTextOperation(visual: string, text: string): Operation {
  const trimmed = text.trim().slice(0, 1000);
  return { op: "set_alt_text", visual, text: trimmed || null };
}

/** `set_visual_tooltip`: a report page as the visual's tooltip, or back to Power BI's default tooltip. */
export const visualTooltipOperation = (visual: string, page: string | undefined): Operation => ({ op: "set_visual_tooltip", visual, ...(page ? { page } : {}) });

/** What the render row says about the visual's report-page tooltip. */
export function tooltipPageOf(row: unknown): string | undefined {
  return isRecord(row) && isRecord(row.tooltipPage) ? str(row.tooltipPage.page) : undefined;
}

export const DATA_KINDS = new Set(["column", "bar", "line", "area", "pie", "donut", "card", "table", "matrix", "slicer"]);
/** A visual whose numbers can be explained or pinned (a chart, card or table that drew rows). */
export const hasNumbers = (kind: string) => kind !== "slicer" && DATA_KINDS.has(kind);

/** A number in the render row's formatting, for a control's current value. */
export const formattingNumber = (formatting: Record<string, unknown> | undefined, path: string): number | undefined =>
  num(path.split(".").reduce<unknown>((cursor, key) => isRecord(cursor) ? cursor[key] : undefined, formatting));
