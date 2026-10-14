// The Visual pane's model: which settings a visual offers, how each control reads
// the value the renderer reports (docs/imported-report-render-contract.md, section 3) and which edit
// operation it sends (docs/imported-report-editing-api.md). Studio does not change styling (colors,
// fonts, titles, borders, legends, axes, themes): the pane offers number formats, the structure of
// tables and matrices, and size and position, and never a control that writes anything else, even
// when the catalog answer lists one. Pure functions, tested in node.

import type { Operation } from "./importedEditing";
import type { Rect } from "./importedGeometry";

// --- the catalog answer ---------------------------------------------------------------------------

export type ControlKind = "toggle" | "number" | "color" | "select" | "text" | "font";
export type Option = { value: string; label: string };
/** How a control's value becomes an operation: `{op: name, visual, [field]: value, ...fixed}`. */
export type OpRecipe = { name: string; field: string; fixed: Record<string, unknown> };
export type ServerControl = {
  id: string; section: string; label: string; description?: string; kind: ControlKind; object: string; property: string; type?: string;
  writable: boolean; options?: Option[]; studio_render: boolean; instance_selector?: unknown; theme_default?: unknown;
  unit?: string; min?: number; max?: number; op: OpRecipe;
  /** "field": the operation needs one of the visual's columns in `field_arg` (the pane's column picker fills it). */
  scope?: "visual" | "field"; field_arg?: string;
};
export type CatalogSection = { id: string; title: string; controls: ServerControl[] };
export type RoleSpec = { role: string; kind: string; label: string; max?: number };
export type FormatCatalog = {
  visualType: string;
  /** False for a type Studio only frames: every control still edits the report, and says "Shown in Power BI". */
  studioDraws: boolean;
  sections: CatalogSection[];
  /** The type's field wells; empty when the answer has none (see `rolesFor`). */
  roles: RoleSpec[];
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): string | undefined => typeof value === "string" && value ? value : undefined;
const KINDS = new Set<string>(["toggle", "number", "color", "select", "text", "font"]);
const humanize = (name: string) => name.replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/^./, c => c.toUpperCase());
const num = (value: unknown): number | undefined => typeof value === "number" && Number.isFinite(value) ? value : undefined;

function readOptions(value: unknown): Option[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const options = value.flatMap((item): Option[] => {
    if (typeof item === "string") return [{ value: item, label: item }];
    if (!isRecord(item)) return [];
    const v = text(item.value) ?? (typeof item.value === "number" ? String(item.value) : undefined);
    return v ? [{ value: v, label: text(item.label) ?? v }] : [];
  });
  return options.length ? options : undefined;
}
function readRecipe(value: unknown): OpRecipe | undefined {
  if (!isRecord(value)) return undefined;
  const name = text(value.name), field = text(value.field);
  return name && field ? { name, field, fixed: isRecord(value.fixed) ? { ...value.fixed } : {} } : undefined;
}
function readControl(raw: unknown): ServerControl | undefined {
  if (!isRecord(raw)) return undefined;
  const object = text(raw.object), property = text(raw.property), op = readRecipe(raw.op);
  if (!object || !property || !op) return undefined;
  const kind = KINDS.has(String(raw.kind)) ? raw.kind as ControlKind : "text";
  return {
    id: text(raw.id) ?? `${object}.${property}`, section: text(raw.section) ?? "", label: text(raw.label) ?? humanize(property), description: text(raw.description),
    kind, object, property, type: text(raw.type), writable: raw.writable !== false, options: readOptions(raw.options), studio_render: raw.studio_render !== false,
    instance_selector: raw.instance_selector ?? undefined, theme_default: raw.theme_default ?? undefined, unit: text(raw.unit), min: num(raw.min), max: num(raw.max),
    op,
    ...(raw.scope === "field" ? { scope: "field" as const, field_arg: text(raw.field_arg) ?? "field" } : {}),
  };
}

/** Reads the answer of `GET .../format-catalog`: the curated `sections` with their controls, and the field wells. */
export function readFormatCatalog(body: unknown, visualType: string): FormatCatalog {
  const source = isRecord(body) ? body : {};
  const sections: CatalogSection[] = Array.isArray(source.sections) ? source.sections.flatMap((item): CatalogSection[] => {
    if (!isRecord(item) || !Array.isArray(item.controls)) return [];
    const controls = item.controls.map(readControl).filter((control): control is ServerControl => control !== undefined);
    const id = text(item.id) ?? "section";
    return controls.length ? [{ id, title: text(item.title) ?? humanize(id), controls }] : [];
  }) : [];
  const roles: RoleSpec[] = Array.isArray(source.roles) ? source.roles.flatMap((item): RoleSpec[] => {
    if (!isRecord(item)) return [];
    const role = text(item.role) ?? text(item.name);
    return role ? [{ role, kind: text(item.kind) ?? "Grouping", label: text(item.label) ?? text(item.display_name) ?? role, max: num(item.max) }] : [];
  }) : [];
  return { visualType: text(source.visual_type) ?? visualType, studioDraws: source.studio_draws !== false, sections, roles };
}

// The field wells (roles) of the visual types Studio can add, from Microsoft's schema: role, kind, label shown in Power BI,
// and how many fields the well takes (null: any number). Used until the catalog answer carries `roles`.
const ROLE_TABLE: Record<string, [string, string, string, number | null][]> = {
  card: [["Values","Measure","Fields",1]],
  multiRowCard: [["Values","GroupingOrMeasure","Fields",null]],
  columnChart: [["Category","Grouping","X-axis",null],["Series","Grouping","Legend",1],["Y","Measure","Y-axis",null],["Rows","Grouping","Small multiples",null]],
  clusteredColumnChart: [["Category","Grouping","X-axis",null],["Series","Grouping","Legend",1],["Y","Measure","Y-axis",null],["Rows","Grouping","Small multiples",null]],
  hundredPercentStackedColumnChart: [["Category","Grouping","X-axis",null],["Series","Grouping","Legend",1],["Y","Measure","Y-axis",null],["Rows","Grouping","Small multiples",null]],
  barChart: [["Category","Grouping","Y-axis",null],["Series","Grouping","Legend",1],["Y","Measure","X-axis",null],["Rows","Grouping","Small multiples",null]],
  clusteredBarChart: [["Category","Grouping","Y-axis",null],["Series","Grouping","Legend",1],["Y","Measure","X-axis",null],["Rows","Grouping","Small multiples",null]],
  hundredPercentStackedBarChart: [["Category","Grouping","Y-axis",null],["Series","Grouping","Legend",1],["Y","Measure","X-axis",null],["Rows","Grouping","Small multiples",null]],
  lineChart: [["Category","Grouping","X-axis",null],["Series","Grouping","Legend",1],["Y","Measure","Y-axis",null],["Y2","Measure","Secondary y-axis",null],["Rows","Grouping","Small multiples",null]],
  areaChart: [["Category","Grouping","X-axis",null],["Series","Grouping","Legend",1],["Y","Measure","Y-axis",null],["Y2","Measure","Secondary y-axis",null],["Rows","Grouping","Small multiples",null]],
  stackedAreaChart: [["Category","Grouping","X-axis",null],["Series","Grouping","Legend",1],["Y","Measure","Y-axis",null],["Rows","Grouping","Small multiples",null]],
  pieChart: [["Category","Grouping","Legend",1],["Series","Grouping","Details",1],["Y","Measure","Values",null]],
  donutChart: [["Category","Grouping","Legend",1],["Series","Grouping","Details",1],["Y","Measure","Values",null]],
  tableEx: [["Values","GroupingOrMeasure","Columns",null]],
  pivotTable: [["Rows","Grouping","Rows",null],["Columns","Grouping","Columns",null],["Values","Measure","Values",null]],
  slicer: [["Values","Grouping","Field",1]],
  treemap: [["Group","Grouping","Category",1],["Details","Grouping","Details",1],["Values","Measure","Values",null]],
  funnel: [["Category","Grouping","Category",1],["Y","Measure","Values",null]],
  gauge: [["Y","Measure","Value",1],["MinValue","Measure","Minimum value",1],["MaxValue","Measure","Maximum value",1],["TargetValue","Measure","Target value",1]],
  lineClusteredColumnComboChart: [["Category","Grouping","X-axis",null],["Series","Grouping","Column legend",1],["Y","Measure","Column y-axis",null],["Y2","Measure","Line y-axis",null],["Rows","Grouping","Small multiples",null]],
  lineStackedColumnComboChart: [["Category","Grouping","X-axis",null],["Series","Grouping","Column legend",1],["Y","Measure","Column y-axis",null],["Y2","Measure","Line y-axis",null],["Rows","Grouping","Small multiples",null]],
  scatterChart: [["Category","Grouping","Values",1],["Series","Grouping","Legend",1],["X","GroupingOrMeasure","X Axis",null],["Y","GroupingOrMeasure","Y Axis",1],["Size","Measure","Size",1],["Play","Grouping","Play Axis",1]],
  waterfallChart: [["Category","Grouping","Category",1],["Breakdown","Grouping","Breakdown",1],["Y","Measure","Y-axis",1]],
  ribbonChart: [["Category","Grouping","X-axis",null],["Series","Grouping","Legend",1],["Y","Measure","Y-axis",null],["Rows","Grouping","Small multiples",null]],
  kpi: [["Indicator","Measure","Value",1],["TrendLine","Grouping","Trend axis",1],["Goal","Measure","Target",2]],
  cardVisual: [["Data","Measure","Value",null],["Rows","Grouping","Categories",null]],
  advancedSlicerVisual: [["Values","Grouping","Value",1],["Label","Measure","Highlight label",null]],
};

// --- reading the render row --------------------------------------------------------------------------

export type Formatting = Record<string, unknown>;
export type CellValue = string | number | boolean | null;
export type RenderColumn = {
  kind: string; table?: string; name: string; label: string; role: string; query_ref?: string | null; hidden?: boolean;
  // Present when the visual formats the column (render contract, sections 2 and 12).
  colorIndex?: number | null; backgroundIndex?: number | null; icon?: { index: number; layout?: string; alignment?: string } | null;
  dataBars?: unknown; computed?: string; format?: string; displayUnits?: number | null; decimals?: number | null;
};
export type VisualInfo = {
  id: string; kind: string; visualType: string; title?: string; rect?: Rect; hidden?: boolean;
  formatting?: Formatting; columns?: RenderColumn[]; rows?: CellValue[][]; unrendered?: string[];
};

export function dig(source: unknown, path: string): unknown {
  let cursor: unknown = source;
  for (const key of path.split(".")) {
    if (!isRecord(cursor)) return undefined;
    cursor = cursor[key];
  }
  return cursor === null ? undefined : cursor;
}

const DISPLAY_UNITS: [number, string][] = [[0, "auto"], [1, "none"], [1000, "thousands"], [1_000_000, "millions"], [1_000_000_000, "billions"], [1_000_000_000_000, "trillions"]];
/** Display units in the words `set_number_format` takes. */
export const displayUnitOptions = [
  { value: "auto", label: "Auto" }, { value: "none", label: "None" }, { value: "thousands", label: "Thousands" },
  { value: "millions", label: "Millions" }, { value: "billions", label: "Billions" }, { value: "trillions", label: "Trillions" },
];
const unitName = (divisor: unknown) => typeof divisor === "number" ? DISPLAY_UNITS.find(([d]) => d === divisor)?.[1] : "auto";

// --- the pane's controls -------------------------------------------------------------------------------

export type ControlInput = "toggle" | "number" | "select" | "text";
export type ControlSpec = {
  id: string; label: string; input: ControlInput;
  /** `object.property` keys this control changes: used for the "Auto (theme)" and "Shown in Power BI" tags. */
  props: string[];
  unit?: string; min?: number; max?: number; step?: number; options?: Option[]; placeholder?: string;
  /** The catalog says Studio's canvas does not draw this property (it is still saved): the pane tags it "Shown in Power BI". */
  studioRender?: boolean;
  /** What the theme gives this property when the report sets nothing (from the catalog): a value equal to it reads as "Auto (theme)". */
  themeDefault?: unknown;
  read?: (info: VisualInfo, field?: string) => unknown;
  /** `field` is the column a field-scoped control applies to (the section's column picker). */
  write?: (info: VisualInfo, value: unknown, field?: string) => Operation | undefined;
  /** The control needs one of the visual's columns: the section shows a column picker, and nothing is sent without one. */
  fieldScoped?: boolean;
};
export type SectionSpec = { id: string; title: string; controls: ControlSpec[] };

const CHART_KINDS = new Set(["column", "bar", "line", "area", "pie", "donut"]);
export const isChartKind = (kind: string) => CHART_KINDS.has(kind);

function positionControls(): ControlSpec[] {
  const field = (key: keyof Rect, label: string, min: number): ControlSpec => ({
    id: `position.${key}`, label, input: "number", unit: "px", min, step: 1, props: [],
    read: i => i.rect?.[key],
    write: (i, v) => typeof v === "number" ? { op: "set_position", visual: i.id, [key]: v } : undefined,
  });
  return [field("x", "Left", 0), field("y", "Top", 0), field("width", "Width", 10), field("height", "Height", 10)];
}

/** Display units and decimals of a chart's data labels or a card's value, through `set_number_format`. */
function numberFormatControls(info: VisualInfo): ControlSpec[] {
  const chart = isChartKind(info.kind);
  if (!chart && info.kind !== "card") return [];
  const target = chart ? "data_labels" : "card_value";
  const token = chart ? "labels" : "cardValue";
  const object = chart || info.visualType !== "cardVisual" ? "labels" : "value";
  const op = (i: VisualInfo, patch: Record<string, unknown>): Operation => ({ op: "set_number_format", visual: i.id, target, ...patch });
  return [
    { id: "number.units", label: "Display units", input: "select", options: displayUnitOptions, props: [`${object}.labelDisplayUnits`], read: i => unitName(dig(i.formatting, `${token}.displayUnits`)), write: (i, v) => typeof v === "string" ? op(i, { display_units: v }) : undefined },
    { id: "number.decimals", label: "Decimal places", input: "number", min: 0, max: 6, step: 1, placeholder: "Auto", props: [`${object}.labelPrecision`], read: i => dig(i.formatting, `${token}.decimals`), write: (i, v) => typeof v === "number" ? op(i, { decimals: v }) : undefined },
  ];
}

/** The pane's own sections, used until the catalog answers or when it cannot: number format, then size and position. */
export function curatedSections(info: VisualInfo): SectionSpec[] {
  if (info.kind === "group") return [];
  const number = numberFormatControls(info);
  return [
    ...(number.length ? [{ id: "number_format", title: "Number format", controls: number }] : []),
    { id: "size_position", title: "Size & position", controls: positionControls() },
  ];
}

// --- the catalog's sections ---------------------------------------------------------------------------------------

// The operations the pane may send. Every styling operation (colors, fonts, titles, data labels, legends, axes,
// background and border, themes, per-column colors, alignment and width, the generic property route) was removed
// from Studio, so a catalog control that writes one is never shown. `set_table_layout` also carries banding, grid
// lines, padding and word wrap: only its structural fields are offered.
const PANE_OPERATIONS = new Set(["set_number_format", "set_table_layout", "move_column", "set_field_display_name", "set_position", "arrange", "set_visual_hidden"]);
const TABLE_LAYOUT_FIELDS = new Set(["totals", "row_subtotals", "column_subtotals", "stepped", "layout", "values_on_rows"]);
type PaneControl = ServerControl & { kind: Exclude<ControlKind, "color"> };

/** Whether the pane offers a catalog control: one of the operations above, never a color. */
export function isPaneControl(control: ServerControl): control is PaneControl {
  if (control.kind === "color" || !PANE_OPERATIONS.has(control.op.name)) return false;
  if (control.op.name !== "set_table_layout") return true;
  return TABLE_LAYOUT_FIELDS.has(control.op.field) && Object.keys(control.op.fixed).every(key => TABLE_LAYOUT_FIELDS.has(key));
}

/** The renderer's tokens for one catalog property (docs/imported-report-render-contract.md, section 3): the value the canvas draws. */
export function tokenValue(info: VisualInfo, object: string, property: string): unknown {
  const at = (path: string) => dig(info.formatting, path);
  const card = info.kind === "card";
  switch (`${object}.${property}`) {
    case "labels.labelDisplayUnits": return displayUnitValue(card ? at("cardValue.displayUnits") : at("labels.displayUnits"));
    case "labels.labelPrecision": return card ? at("cardValue.decimals") : at("labels.decimals");
    case "value.labelDisplayUnits": return displayUnitValue(at("cardValue.displayUnits"));
    case "value.labelPrecision": return at("cardValue.decimals");
    case "valueAxis.labelDisplayUnits": return displayUnitValue(at("axes.value.displayUnits"));
    case "valueAxis.labelPrecision": return at("axes.value.decimals");
    case "labels.valueCustomFormatString": return at("labels.format");
    case "value.customFormatString": return at("cardValue.format");
    case "position.x": return info.rect?.x;
    case "position.y": return info.rect?.y;
    case "position.width": return info.rect?.width;
    case "position.height": return info.rect?.height;
    case "position.hidden": return info.hidden;
    default: return undefined;
  }
}
/** Display units in the words both the catalog and `set_number_format` use. */
function displayUnitValue(divisor: unknown): string | undefined {
  if (divisor === undefined) return undefined;
  return unitName(divisor);
}

const NUMERIC = /^-?\d+(\.\d+)?$/;
/** A select's value as the operation takes it: numeric option lists (display units) are numbers. */
export function optionValue(options: Option[] | undefined, chosen: string): string | number {
  return options?.length && options.every(option => NUMERIC.test(option.value)) ? Number(chosen) : chosen;
}
const INPUT: Record<PaneControl["kind"], ControlInput> = { toggle: "toggle", number: "number", select: "select", text: "text", font: "text" };

/**
 * The operation a catalog control writes: `{op, visual, ...fixed, [field]: value}`, plus the column a field-scoped control
 * names (`{[field_arg]: column}`). A field-scoped control without a column writes nothing.
 */
export function controlOperation(control: Pick<ServerControl, "op" | "options" | "scope" | "field_arg">, visual: string, value: unknown, column?: string): Operation | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  if (control.scope === "field" && !column) return undefined;
  const chosen = typeof value === "string" ? optionValue(control.options, value) : value;
  return { op: control.op.name, visual, ...control.op.fixed, ...(control.scope === "field" && column ? { [control.field_arg ?? "field"]: column } : {}), [control.op.field]: chosen };
}

/** The columns a field-scoped control can name: the visual's shown fields, by the name its header reads. */
export function fieldColumns(info: Pick<VisualInfo, "columns">): Option[] {
  const seen = new Set<string>();
  return (info.columns ?? []).flatMap(column => {
    if (column.hidden || column.kind === "computed" || column.role === "Formatting") return [];
    const value = column.label || column.name;
    if (!value || seen.has(value)) return [];
    seen.add(value);
    return [{ value, label: value }];
  });
}

/** What a field-scoped control of a table shows for one column: the name its header reads. */
function columnTokenValue(info: VisualInfo, property: string, column: string | undefined): unknown {
  if (!column || property !== "displayName") return undefined;
  return (info.columns ?? []).find(c => (c.label || c.name) === column)?.label;
}

function themeDefaultText(value: unknown): string | undefined {
  if (typeof value === "number") return String(value);
  if (typeof value === "string" && value.trim() && value.length <= 40) return value;
  return undefined;
}

/** A catalog control as a pane control: it reads the renderer's tokens and writes the catalog's operation. */
export function toControlSpec(control: PaneControl): ControlSpec {
  const fallback = themeDefaultText(control.theme_default);
  return {
    id: control.id, label: control.label, input: INPUT[control.kind], props: [`${control.object}.${control.property}`],
    unit: control.unit, min: control.min, max: control.max, step: control.kind === "number" && control.unit ? 1 : undefined,
    options: control.options, placeholder: fallback ? `Auto (${fallback})` : undefined, studioRender: control.studio_render, themeDefault: control.theme_default,
    // A preset changes several properties; display units alone cannot identify the preset now in use.
    read: (info, field) => control.op.field === "preset" ? undefined : control.scope === "field" ? columnTokenValue(info, control.property, field) : tokenValue(info, control.object, control.property),
    write: (info, value, field) => controlOperation(control, info.id, value, field),
    ...(control.scope === "field" ? { fieldScoped: true } : {}),
  };
}

// Number formats and the table's structure first, size and position last.
const SECTION_ORDER = ["number_format", "columns", "table_layout"];
const sectionRank = (id: string) => id === "size_position" ? SECTION_ORDER.length + 1 : SECTION_ORDER.includes(id) ? SECTION_ORDER.indexOf(id) : SECTION_ORDER.length;

/**
 * What the pane shows: the catalog's sections, keeping only the controls the pane may offer (`isPaneControl`: each
 * names the property, its type and the operation that writes it). The catalog's own conditional formatting controls
 * give way to the pane's full editor. Until the catalog answers, or when it cannot, the built-in sections stand in.
 */
export function sectionsFor(info: VisualInfo, catalog: FormatCatalog | undefined): SectionSpec[] {
  if (!catalog?.sections.length) return curatedSections(info);
  // A field-scoped control is listed `writable: false` until a pane can name the column: this pane can (its column picker).
  return catalog.sections
    .filter(section => section.id !== "conditional_formatting")
    .map(section => ({ id: section.id, title: section.title, controls: section.controls.filter(isPaneControl).filter(control => control.writable || control.scope === "field").map(toControlSpec) }))
    .filter(section => section.controls.length)
    .sort((a, b) => sectionRank(a.id) - sectionRank(b.id));
}

/** Is a control's change something Studio's canvas does not draw? Then the pane says "Shown in Power BI". */
export function shownInPowerBiOnly(spec: Pick<ControlSpec, "props" | "studioRender">, info: VisualInfo): boolean {
  if (!spec.props.length) return false;
  if (spec.studioRender === false) return true;
  return spec.props.some(key => (info.unrendered ?? []).includes(key));
}

/**
 * "Auto (theme)": nothing reads back (the report author set nothing and the renderer has no value of its own), or what
 * reads back is exactly what the theme gives (the catalog's `theme_default`), so the pane shows the theme's value as a hint.
 */
export function isAuto(current: unknown, themeDefault?: unknown): boolean {
  if (current === undefined || current === null || current === "") return true;
  if (themeDefault === undefined || themeDefault === null) return false;
  if (typeof current === "number" && typeof themeDefault === "number") return Math.abs(current - themeDefault) < 0.01;
  if (typeof current === "string" && typeof themeDefault === "string") return current.toLowerCase() === themeDefault.toLowerCase();
  return false;
}

// --- fields, addable visuals -------------------------------------------------------------------------------

export type AddableType = { type: string; label: string; drawn: boolean };
const ADDABLE: [string, string, boolean][] = [
  ["clusteredColumnChart", "Clustered column chart", true], ["columnChart", "Stacked column chart", true],
  ["clusteredBarChart", "Clustered bar chart", true], ["barChart", "Stacked bar chart", true],
  ["lineChart", "Line chart", true], ["areaChart", "Area chart", true], ["stackedAreaChart", "Stacked area chart", true],
  ["pieChart", "Pie chart", true], ["donutChart", "Donut chart", true], ["card", "Card", true], ["multiRowCard", "Multi-row card", false],
  ["tableEx", "Table", true], ["pivotTable", "Matrix", false], ["slicer", "Slicer", true],
  ["hundredPercentStackedColumnChart", "100% stacked column", false], ["hundredPercentStackedBarChart", "100% stacked bar", false],
  ["lineClusteredColumnComboChart", "Line and clustered column", false], ["lineStackedColumnComboChart", "Line and stacked column", false],
  ["scatterChart", "Scatter chart", false], ["treemap", "Treemap", false], ["funnel", "Funnel", false], ["gauge", "Gauge", false],
  ["waterfallChart", "Waterfall chart", false], ["ribbonChart", "Ribbon chart", false], ["kpi", "KPI", false],
];
export const ADDABLE_TYPES: AddableType[] = ADDABLE.map(([type, label, drawn]) => ({ type, label, drawn }));
export const visualTypeLabel = (type: string) => ADDABLE_TYPES.find(item => item.type === type)?.label ?? humanize(type);

export type Well = { role: RoleSpec; fields: RenderColumn[] };
/** The wells of a visual type: the catalog's roles when its answer has them, else the built-in table for the types Studio can add. */
export function rolesFor(visualType: string, catalog: FormatCatalog | undefined): RoleSpec[] {
  if (catalog?.roles.length) return catalog.roles;
  return (ROLE_TABLE[visualType] ?? []).map(([role, kind, label, max]) => ({ role, kind, label, ...(max === null ? {} : { max }) }));
}
/** The selected visual's fields grouped into the wells its type has, in Power BI's order; a role the table does not know still shows. */
export function fieldWells(visualType: string, catalog: FormatCatalog | undefined, columns: RenderColumn[] | undefined): Well[] {
  const shown = (columns ?? []).filter(c => !c.hidden && c.role !== "Formatting");
  const roles = rolesFor(visualType, catalog);
  const known = new Set(roles.map(r => r.role));
  const extra = [...new Set(shown.map(c => c.role))].filter(role => role && !known.has(role)).map(role => ({ role, kind: "Grouping", label: role }));
  return [...roles, ...extra].map(role => ({ role, fields: shown.filter(c => c.role === role.role) }));
}

export type FieldChoice = { table: string; name: string; kind: "column" | "measure"; numeric: boolean };
export const fieldRef = (choice: Pick<FieldChoice, "table" | "name">, aggregation?: string) => ({ table: choice.table, name: choice.name, ...(aggregation ? { aggregation } : {}) });

export function fieldChoices(tables: { TableName: string; ColumnName: string; PandasDataType: string }[], measures: { TableName: string; Name: string }[]): FieldChoice[] {
  const numeric = (type: string) => /int|float|decimal|number/i.test(type);
  return [
    ...tables.map(t => ({ table: t.TableName, name: t.ColumnName, kind: "column" as const, numeric: numeric(t.PandasDataType) })),
    ...measures.map(m => ({ table: m.TableName, name: m.Name, kind: "measure" as const, numeric: true })),
  ];
}
