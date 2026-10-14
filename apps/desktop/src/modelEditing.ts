import type { Operation, PendingChange, RawResponse, Transport, WhatIf } from "./importedEditing";
import { readModelStorage, type ModelStorage } from "./modelStorage";

// The model workbench of an imported report (docs/imported-report-editing-api.md, "The model" and "The model beyond
// measures"; docs/imported-model-health.md). Everything here is pure: reading the payload's `model` facts, the
// operations each editor sends, the guards Studio can check before a change is queued, Microsoft's supported list
// for external tools, and the model health findings. Nothing is ever applied from here: every operation goes into the
// "Pending Power BI changes" tray and is applied in one Desktop cycle when the user asks.

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): string | undefined => typeof value === "string" && value !== "" ? value : undefined;
const flag = (value: unknown): boolean => value === true;
const items = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.filter(isRecord) : [];
const fold = (value: string | undefined) => (value ?? "").trim().toLowerCase();
const same = (a: string | undefined, b: string | undefined) => fold(a) === fold(b);

// --- the facts --------------------------------------------------------------------------------------------------------

export type ModelStatus = "current" | "reading" | "unknown" | "none";
export type FactTable = {
  name: string; hidden: boolean; description?: string; dataCategory?: string; isDateTable: boolean; dateColumn?: string;
  source?: string; calculated: boolean; expression?: string; calculationGroup: boolean; autoDateTable?: string; privateTable: boolean; truncated: boolean;
};
export type ColumnKind = "data" | "calculated" | "calculatedTableColumn";
export type FactColumn = {
  table: string; name: string; kind: ColumnKind; dataType?: string; expression?: string; formatString?: string; sortByColumn?: string;
  dataCategory?: string; summarizeBy?: string; hidden: boolean; key: boolean; displayFolder?: string; description?: string; error?: string; truncated: boolean;
};
export type FactMeasure = { table: string; name: string; expression?: string; formatString?: string; displayFolder?: string; description?: string; hidden: boolean };
export type FactRelationship = {
  name?: string; fromTable: string; fromColumn: string; toTable: string; toColumn: string; cardinality: string; crossFilter: string;
  active: boolean; relyOnReferentialIntegrity: boolean; securityFilter?: string;
};
export type FactHierarchy = { table: string; name: string; hidden: boolean; levels: { name: string; column: string; ordinal: number }[] };
export type FactRole = { name: string; description?: string; permission?: string; tables: { table: string; filter: string; truncated: boolean }[] };
export type FactCalculationItem = { name: string; expression: string; ordinal?: number; formatStringExpression?: string; truncated: boolean };
export type FactCalculationGroup = { table: string; precedence?: number; description?: string; items: FactCalculationItem[] };
export type FactParameterRow = { label?: string; table?: string; name: string; order?: number; kind?: string };
export type FactFieldParameter = { table: string; rows: FactParameterRow[] | null };
export type FactPartition = { table: string; name: string; source?: string; mode?: string; expression?: string; truncated: boolean };
export type FactExpression = { name: string; kind?: string; expression?: string; description?: string; parameter: boolean; truncated: boolean };
export type ModelSettings = { culture?: string; discourageImplicitMeasures?: boolean; autoDateTime?: boolean | null; defaultMode?: string };
export type StudioNotes = {
  rowLevelSecurity?: string; powerBiOnly: { table: string; kind: string; detail: string }[];
  changedCalculations: { table: string; column?: string }[]; data?: { known?: boolean; stale?: boolean; detail?: string }; actions: string[];
};
export type ModelFacts = {
  status: ModelStatus; truncated: string[]; settings: ModelSettings;
  tables: FactTable[]; columns: FactColumn[]; measures: FactMeasure[]; relationships: FactRelationship[]; hierarchies: FactHierarchy[];
  roles: FactRole[]; calculationGroups: FactCalculationGroup[]; fieldParameters: FactFieldParameter[]; partitions: FactPartition[];
  expressions: FactExpression[]; studio?: StudioNotes; storage?: ModelStorage;
};

const STATUSES: ModelStatus[] = ["current", "reading", "unknown", "none"];

/** The payload's `model` (or the full-facts route's answer) as typed facts; missing sections are empty lists. */
export function readModelFacts(raw: unknown): ModelFacts {
  const body = isRecord(raw) ? raw : {};
  const status = STATUSES.includes(body.status as ModelStatus) ? body.status as ModelStatus : isRecord(raw) && Array.isArray(body.tables) ? "current" : "unknown";
  const settings = isRecord(body.model) ? body.model : {};
  const studio = isRecord(body.studio) ? body.studio : undefined;
  return {
    status,
    storage: readModelStorage(body.storage, status),
    truncated: Array.isArray(body.truncated) ? body.truncated.filter((s): s is string => typeof s === "string") : [],
    settings: {
      culture: text(settings.culture), discourageImplicitMeasures: typeof settings.discourageImplicitMeasures === "boolean" ? settings.discourageImplicitMeasures : undefined,
      autoDateTime: typeof settings.autoDateTime === "boolean" ? settings.autoDateTime : null, defaultMode: text(settings.defaultMode),
    },
    tables: items(body.tables).flatMap((t): FactTable[] => text(t.name) ? [{
      name: String(t.name), hidden: flag(t.hidden), description: text(t.description), dataCategory: text(t.dataCategory), isDateTable: flag(t.isDateTable),
      dateColumn: text(t.dateColumn), source: text(t.source), calculated: flag(t.calculated), expression: text(t.expression), calculationGroup: flag(t.calculationGroup),
      autoDateTable: text(t.autoDateTable), privateTable: flag(t.private), truncated: flag(t.truncated),
    }] : []),
    columns: items(body.columns).flatMap((c): FactColumn[] => text(c.table) && text(c.name) ? [{
      table: String(c.table), name: String(c.name), kind: c.kind === "calculated" || c.kind === "calculatedTableColumn" ? c.kind : "data",
      dataType: text(c.dataTypeName) ?? text(c.dataType), expression: text(c.expression), formatString: text(c.formatString), sortByColumn: text(c.sortByColumn),
      dataCategory: text(c.dataCategory), summarizeBy: text(c.summarizeBy), hidden: flag(c.hidden), key: flag(c.key), displayFolder: text(c.displayFolder),
      description: text(c.description), error: text(c.error), truncated: flag(c.truncated),
    }] : []),
    measures: items(body.measures).flatMap((m): FactMeasure[] => text(m.table) && text(m.name) ? [{
      table: String(m.table), name: String(m.name), expression: text(m.expression), formatString: text(m.formatString), displayFolder: text(m.displayFolder),
      description: text(m.description), hidden: flag(m.hidden),
    }] : []),
    relationships: items(body.relationships).flatMap((r): FactRelationship[] => text(r.fromTable) && text(r.fromColumn) && text(r.toTable) && text(r.toColumn) ? [{
      name: text(r.name), fromTable: String(r.fromTable), fromColumn: String(r.fromColumn), toTable: String(r.toTable), toColumn: String(r.toColumn),
      cardinality: text(r.cardinality) ?? "M:1", crossFilter: text(r.crossFilter) ?? "single", active: r.active !== false,
      relyOnReferentialIntegrity: flag(r.relyOnReferentialIntegrity), securityFilter: text(r.securityFilter),
    }] : []),
    hierarchies: items(body.hierarchies).flatMap((h): FactHierarchy[] => text(h.table) && text(h.name) ? [{
      table: String(h.table), name: String(h.name), hidden: flag(h.hidden),
      levels: items(h.levels).flatMap(l => text(l.column) ? [{ name: text(l.name) ?? String(l.column), column: String(l.column), ordinal: typeof l.ordinal === "number" ? l.ordinal : 0 }] : [])
        .sort((a, b) => a.ordinal - b.ordinal),
    }] : []),
    roles: items(body.roles).flatMap((r): FactRole[] => text(r.name) ? [{
      name: String(r.name), description: text(r.description), permission: text(r.permission),
      tables: items(r.tables).flatMap(t => text(t.table) ? [{ table: String(t.table), filter: typeof t.filter === "string" ? t.filter : "", truncated: flag(t.truncated) }] : []),
    }] : []),
    calculationGroups: items(body.calculationGroups).flatMap((g): FactCalculationGroup[] => text(g.table) ? [{
      table: String(g.table), precedence: typeof g.precedence === "number" ? g.precedence : undefined, description: text(g.description),
      items: items(g.items).flatMap(i => text(i.name) ? [{
        name: String(i.name), expression: typeof i.expression === "string" ? i.expression : "", ordinal: typeof i.ordinal === "number" ? i.ordinal : undefined,
        formatStringExpression: text(i.formatStringExpression), truncated: flag(i.truncated),
      }] : []),
    }] : []),
    fieldParameters: items(body.fieldParameters).flatMap((p): FactFieldParameter[] => text(p.table) ? [{
      table: String(p.table),
      rows: Array.isArray(p.rows) ? items(p.rows).flatMap(r => text(r.name) ? [{ label: text(r.label), table: text(r.table), name: String(r.name), order: typeof r.order === "number" ? r.order : undefined, kind: text(r.kind) }] : []) : null,
    }] : []),
    partitions: items(body.partitions).flatMap((p): FactPartition[] => text(p.table) ? [{
      table: String(p.table), name: text(p.name) ?? String(p.table), source: text(p.source), mode: text(p.mode), expression: text(p.expression), truncated: flag(p.truncated),
    }] : []),
    expressions: items(body.expressions).flatMap((e): FactExpression[] => text(e.name) ? [{
      name: String(e.name), kind: text(e.kind), expression: text(e.expression), description: text(e.description), parameter: flag(e.parameter), truncated: flag(e.truncated),
    }] : []),
    studio: studio ? {
      rowLevelSecurity: text(studio.rowLevelSecurity),
      powerBiOnly: items(studio.powerBiOnly).map(p => ({ table: String(p.table ?? ""), kind: String(p.kind ?? ""), detail: String(p.detail ?? "") })),
      changedCalculations: items(studio.changedCalculations).map(c => ({ table: String(c.table ?? ""), column: text(c.column) })),
      data: isRecord(studio.data) ? { known: studio.data.known === true, stale: studio.data.stale === true, detail: text(studio.data.detail) } : undefined,
      actions: Array.isArray(studio.actions) ? studio.actions.filter((a): a is string => typeof a === "string") : [],
    } : undefined,
  };
}

/** What the workbench says about the facts before it shows them. */
export function modelStatusNote(facts: Pick<ModelFacts, "status">): { tone: "info" | "warn"; text: string } | undefined {
  switch (facts.status) {
    case "reading": return { tone: "info", text: "Studio is reading this report's model. It takes a few seconds; the model's tables and columns appear when it is done." };
    case "unknown": return { tone: "warn", text: "Studio could not read this report's model, so model changes are not offered here. Open the report in Power BI Desktop to change its model." };
    case "none": return { tone: "info", text: "This report has no local model to edit here. Check its connection in Power BI Desktop." };
    default: return undefined;
  }
}

/** The kind of table, in the words Power BI Desktop uses. */
export function tableKind(facts: ModelFacts, table: FactTable): "table" | "calculated table" | "calculation group" | "field parameter" | "date table" {
  if (table.calculationGroup || facts.calculationGroups.some(g => same(g.table, table.name))) return "calculation group";
  if (facts.fieldParameters.some(p => same(p.table, table.name))) return "field parameter";
  if (table.autoDateTable) return "date table";
  return table.calculated ? "calculated table" : "table";
}

/** The tables a person works with: Desktop's hidden automatic date tables are left out. */
export const visibleTables = (facts: ModelFacts): FactTable[] => facts.tables.filter(t => !t.autoDateTable && !t.privateTable && !/^(LocalDateTable|DateTableTemplate)_/.test(t.name));
export const columnsOf = (facts: ModelFacts, table: string): FactColumn[] => facts.columns.filter(c => same(c.table, table));
export const findTable = (facts: ModelFacts, name: string | undefined): FactTable | undefined => name === undefined ? undefined : facts.tables.find(t => same(t.name, name));
export const findColumn = (facts: ModelFacts, table: string, name: string): FactColumn | undefined => facts.columns.find(c => same(c.table, table) && same(c.name, name));

/** A table's Power Query, as the full facts give it: `editable` only when the text is whole (never `truncated`). */
export function tableQuery(facts: ModelFacts, table: string): { text: string; editable: boolean; reason?: string } | undefined {
  const partition = facts.partitions.find(p => same(p.table, table) && p.source === "m");
  if (!partition) return undefined;
  if (partition.truncated) return { text: partition.expression ?? "", editable: false, reason: "This query is too long for Studio to show in full, so it cannot be changed here. Change it in Power BI Desktop (Home > Transform data)." };
  if (partition.expression === undefined) return { text: "", editable: false, reason: "Studio could not read this query." };
  return { text: partition.expression, editable: true };
}

/** True when the facts cut text somewhere: the workbench then reads the full facts before offering a text editor. */
export const needsFullFacts = (facts: ModelFacts): boolean => facts.truncated.length > 0;

// --- values Power BI shows, and the operations' words for them ------------------------------------------------------------

export const SUMMARIZE_OPTIONS = [
  ["default", "Default"], ["none", "Don't summarize"], ["sum", "Sum"], ["average", "Average"], ["min", "Minimum"], ["max", "Maximum"],
  ["count", "Count"], ["distinct_count", "Count (distinct)"],
] as const;
export const DATA_CATEGORY_OPTIONS = [
  ["uncategorized", "Uncategorized"], ["address", "Address"], ["city", "City"], ["continent", "Continent"], ["country", "Country/Region"],
  ["county", "County"], ["place", "Place"], ["postal_code", "Postal code"], ["state_or_province", "State or province"], ["latitude", "Latitude"],
  ["longitude", "Longitude"], ["web_url", "Web URL"], ["image_url", "Image URL"], ["barcode", "Barcode"],
] as const;
export const DATA_TYPE_OPTIONS = [
  ["text", "Text"], ["whole_number", "Whole number"], ["decimal_number", "Decimal number"], ["fixed_decimal", "Fixed decimal number"],
  ["date_time", "Date/time"], ["true_false", "True/false"],
] as const;
export const FORMAT_PRESETS = ["#,0", "#,0.00", "0.0%", "0%", "$#,0.00", "0.00", "dd/mm/yyyy", "yyyy-mm-dd", "mmmm yyyy"];

const CATEGORY_FROM_FACT: Record<string, string> = {
  "": "uncategorized", uncategorized: "uncategorized", address: "address", city: "city", continent: "continent", country: "country", county: "county",
  place: "place", postalcode: "postal_code", stateorprovince: "state_or_province", latitude: "latitude", longitude: "longitude", weburl: "web_url",
  imageurl: "image_url", barcode: "barcode",
};
/** The operation's data category for the facts' string ("WebUrl" -> "web_url"); undefined for a category the operation cannot write. */
export const dataCategoryValue = (fact: string | undefined): string | undefined => CATEGORY_FROM_FACT[fold(fact).replace(/[\s_-]/g, "")];
/** The operation's summarize-by for the facts' word ("distinctCount" -> "distinct_count"). */
export function summarizeValue(fact: string | undefined): string {
  const word = fold(fact).replace(/[\s_-]/g, "");
  return word === "distinctcount" ? "distinct_count" : SUMMARIZE_OPTIONS.some(([value]) => value === word) ? word : "default";
}
const TYPE_FROM_FACT: Record<string, string> = {
  text: "text", string: "text", integer: "whole_number", int64: "whole_number", double: "decimal_number", decimal: "fixed_decimal",
  datetime: "date_time", boolean: "true_false",
};
export const dataTypeValue = (fact: string | undefined): string | undefined => TYPE_FROM_FACT[fold(fact)];
export const dataTypeLabel = (fact: string | undefined): string => DATA_TYPE_OPTIONS.find(([value]) => value === dataTypeValue(fact))?.[1] ?? (fact || "Unknown");

// --- names ---------------------------------------------------------------------------------------------------------------

const hasControl = (value: string) => [...value].some(c => c.charCodeAt(0) < 32 || c.charCodeAt(0) === 127);
/** A plain-English problem with a new name for a column, table or other model object, or undefined. */
export function modelNameProblem(name: string, taken: string[], what: string, current?: string): string | undefined {
  const trimmed = name.trim();
  if (!trimmed) return `Give the ${what} a name.`;
  if (trimmed.length > 128) return `A ${what} name can have at most 128 characters.`;
  if (hasControl(trimmed)) return `A ${what} name cannot contain line breaks or tabs.`;
  if (taken.some(item => same(item, trimmed) && !(current !== undefined && same(item, current)))) return `“${trimmed}” is already used in this model.`;
  return undefined;
}
/** The names a new column of `table` must not repeat: the table's columns and every measure (Power BI keeps both in one namespace). */
export const columnNamesTaken = (facts: ModelFacts, table: string): string[] => [...columnsOf(facts, table).map(c => c.name), ...facts.measures.map(m => m.name)];
export const tableNamesTaken = (facts: ModelFacts): string[] => facts.tables.map(t => t.name);

/** How a DAX formula names a table: quoted unless it is one plain word. */
export const daxTable = (table: string) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(table) ? table : `'${table.replaceAll("'", "''")}'`;
export const daxColumn = (table: string, column: string) => `${daxTable(table)}[${column.replaceAll("]", "]]")}]`;

// --- the operations ------------------------------------------------------------------------------------------------------

export type ColumnDraft = {
  formatString: string; sortBy: string; dataCategory: string; summarizeBy: string; hidden: boolean; folder: string; description: string;
  dataType?: string; key?: boolean; expression?: string;
};
export function columnDraft(column: FactColumn): ColumnDraft {
  return {
    formatString: column.formatString ?? "", sortBy: column.sortByColumn ?? "", dataCategory: dataCategoryValue(column.dataCategory) ?? "",
    summarizeBy: summarizeValue(column.summarizeBy), hidden: column.hidden, folder: column.displayFolder ?? "", description: column.description ?? "",
    dataType: dataTypeValue(column.dataType), key: column.key, expression: column.kind === "calculated" ? column.expression ?? "" : undefined,
  };
}
/** `set_column_properties` with only what changed, or undefined when nothing did (a no-op would still cost a Desktop cycle). */
export function columnPropertiesOp(column: FactColumn, draft: ColumnDraft): Operation | undefined {
  const base = columnDraft(column);
  const op: Operation = { op: "set_column_properties", table: column.table, name: column.name };
  if (draft.formatString.trim() !== base.formatString.trim()) op.format_string = draft.formatString.trim();
  if (!same(draft.sortBy, base.sortBy)) op.sort_by = draft.sortBy;
  if (draft.dataCategory && draft.dataCategory !== base.dataCategory) op.data_category = draft.dataCategory;
  if (draft.summarizeBy !== base.summarizeBy) op.summarize_by = draft.summarizeBy;
  if (draft.hidden !== base.hidden) op.hidden = draft.hidden;
  if (draft.folder.trim() !== base.folder.trim()) op.folder = draft.folder.trim();
  if (draft.description.trim() !== base.description.trim()) op.description = draft.description.trim();
  if (column.kind === "calculated") {
    if (draft.dataType && draft.dataType !== base.dataType) op.data_type = draft.dataType;
    if (draft.expression !== undefined && draft.expression.trim() && draft.expression.trim() !== (base.expression ?? "").trim()) op.expression = draft.expression.trim();
  }
  if (draft.key !== undefined && draft.key !== base.key) op.key = draft.key;
  return Object.keys(op).length > 3 ? op : undefined;
}

export type TableDraft = { hidden: boolean; description: string; expression?: string };
export const tableDraft = (table: FactTable): TableDraft => ({ hidden: table.hidden, description: table.description ?? "", expression: table.calculated ? table.expression ?? "" : undefined });
export function tablePropertiesOp(table: FactTable, draft: TableDraft): Operation | undefined {
  const op: Operation = { op: "set_table_properties", table: table.name };
  if (draft.hidden !== table.hidden) op.hidden = draft.hidden;
  if (draft.description.trim() !== (table.description ?? "").trim()) op.description = draft.description.trim();
  if (table.calculated && draft.expression !== undefined && draft.expression.trim() && draft.expression.trim() !== (table.expression ?? "").trim()) op.expression = draft.expression.trim();
  return Object.keys(op).length > 2 ? op : undefined;
}

export const renameColumnOp = (table: string, name: string, newName: string): Operation => ({ op: "rename_column", table, name, new_name: newName.trim() });
export const renameTableOp = (name: string, newName: string): Operation => ({ op: "rename_table", name, new_name: newName.trim() });
export const deleteColumnOp = (table: string, name: string): Operation => ({ op: "delete_column", table, name });
export const deleteTableOp = (name: string): Operation => ({ op: "delete_table", name });
export const markDateTableOp = (table: string, column: string | null): Operation => ({ op: "mark_date_table", table, column });

export function calculatedColumnOp(draft: { table: string; name: string; expression: string; formatString?: string; dataType?: string; summarizeBy?: string; folder?: string; description?: string; hidden?: boolean }): Operation {
  return {
    op: "create_calculated_column", table: draft.table, name: draft.name.trim(), expression: draft.expression.trim(),
    ...(draft.formatString?.trim() ? { format_string: draft.formatString.trim() } : {}), ...(draft.dataType ? { data_type: draft.dataType } : {}),
    ...(draft.summarizeBy && draft.summarizeBy !== "default" ? { summarize_by: draft.summarizeBy } : {}),
    ...(draft.folder?.trim() ? { folder: draft.folder.trim() } : {}), ...(draft.description?.trim() ? { description: draft.description.trim() } : {}),
    ...(draft.hidden ? { hidden: true } : {}),
  };
}
export function calculatedTableOp(draft: { name: string; expression: string; description?: string; hidden?: boolean }): Operation {
  return {
    op: "create_calculated_table", name: draft.name.trim(), expression: stripEvaluate(draft.expression),
    ...(draft.description?.trim() ? { description: draft.description.trim() } : {}), ...(draft.hidden ? { hidden: true } : {}),
  };
}

/** A DAX query pasted as a table formula: "EVALUATE x" is the table x. A DEFINE block is not a table formula (undefined). */
export function stripEvaluate(expression: string): string {
  const trimmed = expression.trim();
  const match = /^EVALUATE\b\s*/i.exec(trimmed);
  return match ? trimmed.slice(match[0].length).trim() : trimmed;
}
export const isDefineQuery = (expression: string) => /^\s*DEFINE\b/i.test(expression);

/**
 * The DAX Lab check of a formula before it is queued: the query Studio's engine runs to try it on the imported rows. A
 * calculated column is tried on the table's rows (the first few), a calculated table as it is. Undefined when the text is
 * too long for the lab (4,000 characters): Power BI checks it when the change is applied.
 */
export function checkQuery(kind: "column" | "table", expression: string, table?: string, name = "Value"): string | undefined {
  const formula = kind === "table" ? stripEvaluate(expression) : expression.trim();
  if (!formula) return undefined;
  const query = kind === "table" ? `EVALUATE ${formula}` : `EVALUATE ADDCOLUMNS(${daxTable(table ?? "")}, "${name.replaceAll("\"", "\"\"")}", ${formula})`;
  return query.length <= 4000 ? query : undefined;
}

// Relationships ---------------------------------------------------------------------------------------------------------------

export type Cardinality = "many_to_one" | "one_to_one" | "many_to_many";
export type RelationshipDraft = { fromTable: string; fromColumn: string; toTable: string; toColumn: string; cardinality: Cardinality; crossFilter: "single" | "both"; active: boolean };
export const CARDINALITY_LABELS: Record<Cardinality, string> = { many_to_one: "Many to one (*:1)", one_to_one: "One to one (1:1)", many_to_many: "Many to many (*:*)" };

/** A relationship as the operations name it: the many side first (Desktop's "from"), whatever way round the facts store it. */
export function relationshipDraft(r: FactRelationship): RelationshipDraft {
  const flipped = r.cardinality === "1:M";
  const cardinality: Cardinality = r.cardinality === "1:1" ? "one_to_one" : r.cardinality === "M:M" ? "many_to_many" : "many_to_one";
  return {
    fromTable: flipped ? r.toTable : r.fromTable, fromColumn: flipped ? r.toColumn : r.fromColumn,
    toTable: flipped ? r.fromTable : r.toTable, toColumn: flipped ? r.fromColumn : r.toColumn,
    cardinality, crossFilter: r.crossFilter === "both" ? "both" : "single", active: r.active,
  };
}
export const relationshipKey = (r: Pick<RelationshipDraft, "fromTable" | "fromColumn" | "toTable" | "toColumn">): string => {
  const ends = [`${fold(r.fromTable)}[${fold(r.fromColumn)}]`, `${fold(r.toTable)}[${fold(r.toColumn)}]`].sort();
  return ends.join("~");
};
export const relationshipLabel = (r: Pick<RelationshipDraft, "fromTable" | "fromColumn" | "toTable" | "toColumn">) => `${r.fromTable}[${r.fromColumn}] → ${r.toTable}[${r.toColumn}]`;
const ends = (r: RelationshipDraft) => ({ from_table: r.fromTable, from_column: r.fromColumn, to_table: r.toTable, to_column: r.toColumn });

export function createRelationshipOp(draft: RelationshipDraft): Operation {
  return { op: "create_relationship", ...ends(draft), cardinality: draft.cardinality, cross_filter: draft.crossFilter, active: draft.active };
}
export function updateRelationshipOp(original: FactRelationship, draft: RelationshipDraft): Operation | undefined {
  const base = relationshipDraft(original);
  const op: Operation = { op: "update_relationship", ...ends(base) };
  if (draft.cardinality !== base.cardinality) op.cardinality = draft.cardinality;
  if (draft.crossFilter !== base.crossFilter) op.cross_filter = draft.crossFilter;
  if (draft.active !== base.active) op.active = draft.active;
  return Object.keys(op).length > 5 ? op : undefined;
}
export const deleteRelationshipOp = (r: FactRelationship): Operation => ({ op: "delete_relationship", ...ends(relationshipDraft(r)) });

/** Problems Studio can see before a relationship is queued (Power BI checks the rest, including every active path). */
export function relationshipProblems(facts: ModelFacts, draft: RelationshipDraft, original?: FactRelationship): { blocking: string[]; warnings: string[] } {
  const blocking: string[] = [];
  const warnings: string[] = [];
  if (!draft.fromTable || !draft.fromColumn || !draft.toTable || !draft.toColumn) blocking.push("Pick a column on both ends.");
  else {
    if (same(draft.fromTable, draft.toTable)) blocking.push("Both ends are in the same table. A relationship joins two tables.");
    if (!findColumn(facts, draft.fromTable, draft.fromColumn) || !findColumn(facts, draft.toTable, draft.toColumn)) blocking.push("One of the columns is not in the model.");
    const key = relationshipKey(draft);
    const twin = facts.relationships.find(r => relationshipKey(relationshipDraft(r)) === key);
    if (!original && twin) blocking.push("These two columns are already related. Edit that relationship instead.");
    const pair = (r: { fromTable: string; toTable: string }) => [fold(r.fromTable), fold(r.toTable)].sort().join("~");
    if (draft.active && facts.relationships.some(r => r.active && pair(r) === pair(draft) && relationshipKey(relationshipDraft(r)) !== key)) {
      blocking.push("These two tables already have an active relationship. Power BI allows one active path between two tables: make this one inactive, or make the other inactive first.");
    }
    if (draft.crossFilter === "both") warnings.push("Filtering in both directions can give unexpected totals and slow the report down. Use it only when you need it.");
    if (draft.cardinality === "many_to_many") warnings.push("A many-to-many relationship can give totals that do not add up. Check the numbers after you apply it.");
  }
  return { blocking, warnings };
}

// Hierarchies --------------------------------------------------------------------------------------------------------------

export type LevelDraft = { column: string; name: string };
/** The list with the item at `from` moved to `to` (drag and drop, or the move up / down buttons). */
export function moveItem<T>(list: T[], from: number, to: number): T[] {
  if (from === to || from < 0 || from >= list.length) return list;
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(Math.max(0, Math.min(next.length, to)), 0, item);
  return next;
}
const levelsOp = (levels: LevelDraft[]) => levels.map(l => l.name.trim() && l.name.trim() !== l.column ? { column: l.column, name: l.name.trim() } : { column: l.column });
export const createHierarchyOp = (table: string, name: string, levels: LevelDraft[]): Operation => ({ op: "create_hierarchy", table, name: name.trim(), levels: levelsOp(levels) });
export function updateHierarchyOp(original: FactHierarchy, draft: { name: string; levels: LevelDraft[] }): Operation | undefined {
  const op: Operation = { op: "update_hierarchy", table: original.table, name: original.name };
  const before = original.levels.map(l => `${fold(l.column)}:${l.name}`).join("|");
  const after = draft.levels.map(l => `${fold(l.column)}:${l.name.trim() || l.column}`).join("|");
  if (before !== after) op.levels = levelsOp(draft.levels);
  if (draft.name.trim() && draft.name.trim() !== original.name) op.new_name = draft.name.trim();
  return Object.keys(op).length > 3 ? op : undefined;
}
export const deleteHierarchyOp = (h: FactHierarchy): Operation => ({ op: "delete_hierarchy", table: h.table, name: h.name });

// Field parameters ---------------------------------------------------------------------------------------------------------

export type ParameterFieldDraft = { table: string; name: string; label: string; kind: "column" | "measure" };
export function fieldParameterOp(name: string, fields: ParameterFieldDraft[], existing: boolean): Operation {
  return {
    op: existing ? "update_field_parameter" : "create_field_parameter", name: name.trim(),
    fields: fields.map(f => ({ table: f.table, name: f.name, ...(f.label.trim() && f.label.trim() !== f.name ? { label: f.label.trim() } : {}) })),
  };
}
/** The fields a parameter shows today, as the picker's draft (in its order). */
export function parameterFields(facts: ModelFacts, parameter: FactFieldParameter): ParameterFieldDraft[] {
  return [...(parameter.rows ?? [])].sort((a, b) => (a.order ?? 0) - (b.order ?? 0)).map(row => {
    const measure = facts.measures.find(m => same(m.name, row.name) && (!row.table || same(m.table, row.table)));
    const table = row.table ?? measure?.table ?? "";
    return { table, name: row.name, label: row.label ?? row.name, kind: row.kind === "measure" || (!row.kind && measure) ? "measure" : "column" };
  });
}

// Calculation groups -------------------------------------------------------------------------------------------------------

export type CalculationItemDraft = { name: string; expression: string; ordinal: string; formatStringExpression: string };
export const calculationItemDraft = (item?: FactCalculationItem): CalculationItemDraft => ({
  name: item?.name ?? "", expression: item?.expression ?? "", ordinal: item?.ordinal === undefined ? "" : String(item.ordinal), formatStringExpression: item?.formatStringExpression ?? "",
});
/** `set_calculation_item` for a new item (`original` undefined) or only what changed in one. */
export function calculationItemOp(group: string, original: FactCalculationItem | undefined, draft: CalculationItemDraft): Operation | undefined {
  const ordinal = draft.ordinal.trim() === "" ? undefined : Number(draft.ordinal);
  const validOrdinal = ordinal !== undefined && Number.isInteger(ordinal) && ordinal >= 0 && ordinal <= 1000 ? ordinal : undefined;
  if (!original) {
    if (!draft.name.trim() || !draft.expression.trim()) return undefined;
    return {
      op: "set_calculation_item", group, name: draft.name.trim(), expression: draft.expression.trim(),
      ...(draft.formatStringExpression.trim() ? { format_string_expression: draft.formatStringExpression.trim() } : {}),
      ...(validOrdinal !== undefined ? { ordinal: validOrdinal } : {}),
    };
  }
  const op: Operation = { op: "set_calculation_item", group, name: original.name };
  if (draft.expression.trim() && draft.expression.trim() !== original.expression.trim()) op.expression = draft.expression.trim();
  if (draft.formatStringExpression.trim() !== (original.formatStringExpression ?? "").trim()) op.format_string_expression = draft.formatStringExpression.trim();
  if (validOrdinal !== undefined && validOrdinal !== original.ordinal) op.ordinal = validOrdinal;
  if (draft.name.trim() && draft.name.trim() !== original.name) op.new_name = draft.name.trim();
  return Object.keys(op).length > 3 ? op : undefined;
}
export const ordinalProblem = (value: string): string | undefined => value.trim() === "" || (/^\d+$/.test(value.trim()) && Number(value) <= 1000) ? undefined : "The order is a whole number from 0 to 1000.";
export const deleteCalculationItemOp = (group: string, name: string): Operation => ({ op: "delete_calculation_item", group, name });
export function calculationGroupOp(original: FactCalculationGroup, draft: { precedence: string; description: string }): Operation | undefined {
  const op: Operation = { op: "update_calculation_group", name: original.table };
  const precedence = Number(draft.precedence);
  if (draft.precedence.trim() !== "" && Number.isInteger(precedence) && precedence >= 0 && precedence !== original.precedence) op.precedence = precedence;
  if (draft.description.trim() !== (original.description ?? "").trim()) op.description = draft.description.trim();
  return Object.keys(op).length > 2 ? op : undefined;
}
export function createCalculationGroupOp(name: string, precedence: string, items: { name: string; expression: string }[]): Operation {
  const value = Number(precedence);
  return {
    op: "create_calculation_group", name: name.trim(), items: items.filter(i => i.name.trim() && i.expression.trim()).map(i => ({ name: i.name.trim(), expression: i.expression.trim() })),
    ...(precedence.trim() && Number.isInteger(value) && value >= 0 ? { precedence: value } : {}),
  };
}

// Roles -----------------------------------------------------------------------------------------------------------------

export type RoleFilterDraft = { table: string; expression: string };
/** `create_role`, or `update_role` with only the table filters that changed (an emptied filter is sent as "", which removes it). */
export function roleOp(original: FactRole | undefined, draft: { name: string; description: string; filters: RoleFilterDraft[] }): Operation | undefined {
  const filters = draft.filters.filter(f => f.table);
  if (!original) {
    return { op: "create_role", name: draft.name.trim(), filters: filters.filter(f => f.expression.trim()).map(f => ({ table: f.table, expression: f.expression.trim() })),
      ...(draft.description.trim() ? { description: draft.description.trim() } : {}) };
  }
  const op: Operation = { op: "update_role", name: original.name };
  const changed: RoleFilterDraft[] = [];
  for (const f of filters) {
    const before = original.tables.find(t => same(t.table, f.table));
    if ((before?.filter ?? "").trim() !== f.expression.trim()) changed.push({ table: f.table, expression: f.expression.trim() });
  }
  for (const before of original.tables) {
    if (!filters.some(f => same(f.table, before.table))) changed.push({ table: before.table, expression: "" });
  }
  if (changed.length) op.filters = changed;
  if (draft.name.trim() && draft.name.trim() !== original.name) op.new_name = draft.name.trim();
  if (draft.description.trim() !== (original.description ?? "").trim()) op.description = draft.description.trim();
  return Object.keys(op).length > 2 ? op : undefined;
}
export const deleteRoleOp = (name: string): Operation => ({ op: "delete_role", name });

// Power Query, parameters and model settings -------------------------------------------------------------------------------

export type RefreshFailure = "ask" | "keep" | "undo";
export const REFRESH_WARNING = "Power BI reloads this table from its data source when the change is applied. If the source (a file, a database or a web address) cannot be reached from this computer, the reload fails; Studio then asks whether to keep the new query without its data or to undo it.";
export const REFRESH_DATA_WARNING = "Power BI reloads the data from its sources. Sources that cannot be reached from this computer (a file that moved, a database you are not signed in to) make the refresh fail, and nothing changes.";
export const tableQueryOp = (table: string, expression: string, ifFails: RefreshFailure = "ask"): Operation =>
  ({ op: "set_table_query", table, expression: expression.replace(/\r\n/g, "\n").trim(), ...(ifFails !== "ask" ? { if_refresh_fails: ifFails } : {}) });
export const refreshDataOp = (table?: string): Operation => table ? { op: "refresh_data", table } : { op: "refresh_data" };
export const queryParameterOp = (name: string, value: string): Operation => ({ op: "set_query_parameter", name, value });

/**
 * Obvious slips in a Power Query text that Studio can point at before it is queued: a `let` without `in`, brackets or
 * quotes that do not close. Power BI's own validator runs on Apply and has the last word; its message is shown then.
 */
/**
 * The answer of `POST .../imported-report/model/validate-query`: what an edit with this Power Query formula would be told
 * (Microsoft's Power Query parser, and the table's own checks), without queueing one. `checked` false: the parser could
 * not run, so `valid` only means nothing refused it and `warnings` say Power BI checks it when it loads the data.
 */
export type QueryCheck = { valid: boolean; checked: boolean; message?: string; problems: string[]; warnings: string[]; table?: string };
export function readQueryCheck(raw: unknown): QueryCheck {
  const body = isRecord(raw) ? raw : {};
  return {
    valid: body.valid === true, checked: body.checked === true, message: text(body.message), table: text(body.table),
    problems: Array.isArray(body.problems) ? body.problems.filter((p): p is string => typeof p === "string") : [],
    warnings: Array.isArray(body.warnings) ? body.warnings.filter((w): w is string => typeof w === "string") : [],
  };
}
/** What the Power Query editor shows for a check: the refusal first, then the parser's problems, or the "not checked" warnings. */
export function queryCheckView(check: QueryCheck): { tone: "ok" | "error" | "warn"; lines: string[] } {
  if (!check.valid) {
    const lines = [check.message ?? "Power BI would refuse this query.", ...check.problems.filter(p => !(check.message ?? "").includes(p))];
    return { tone: "error", lines };
  }
  if (!check.checked) return { tone: "warn", lines: check.warnings.length ? check.warnings : ["Studio could not run the Power Query parser here; Power BI checks the query when it loads the data."] };
  return { tone: "ok", lines: ["Power BI's Power Query parser found no problems.", ...check.warnings] };
}

export function mQueryProblems(expression: string): string[] {
  const problems: string[] = [];
  const source = expression.trim();
  if (!source) return ["The query is empty."];
  if (source.length > 50000) problems.push("The query is longer than 50,000 characters.");
  const depth: Record<string, number> = { "(": 0, "[": 0, "{": 0 };
  const closer: Record<string, string> = { ")": "(", "]": "[", "}": "{" };
  let inString = false, inComment: "" | "line" | "block" = "", mismatched = false;
  const words: string[] = [];
  let word = "";
  for (let i = 0; i < source.length; i++) {
    const c = source[i], next = source[i + 1];
    if (inComment === "line") { if (c === "\n") inComment = ""; continue; }
    if (inComment === "block") { if (c === "*" && next === "/") { inComment = ""; i++; } continue; }
    if (inString) { if (c === "\"") { if (next === "\"") i++; else inString = false; } continue; }
    if (c === "/" && next === "/") { inComment = "line"; i++; continue; }
    if (c === "/" && next === "*") { inComment = "block"; i++; continue; }
    if (c === "#" && next === "\"") { inString = true; i++; continue; }
    if (c === "\"") { inString = true; continue; }
    if (/[A-Za-z_]/.test(c)) { word += c; continue; }
    if (word) { words.push(word.toLowerCase()); word = ""; }
    if (c in depth) depth[c] += 1;
    else if (c in closer) { depth[closer[c]] -= 1; if (depth[closer[c]] < 0) mismatched = true; }
  }
  if (word) words.push(word.toLowerCase());
  if (inString) problems.push("A text in quotes is not closed.");
  if (inComment === "block") problems.push("A /* comment */ is not closed.");
  if (mismatched || Object.values(depth).some(d => d !== 0)) problems.push("The brackets do not match: check every (, [ and { has its closing partner.");
  const lets = words.filter(w => w === "let").length, ins = words.filter(w => w === "in").length;
  if (lets > ins) problems.push("A “let” has no matching “in”: the query must end with “in” and the step it returns.");
  return problems;
}

export function modelOptionsOp(settings: ModelSettings, draft: { discourageImplicitMeasures: boolean }): Operation | undefined {
  return draft.discourageImplicitMeasures !== Boolean(settings.discourageImplicitMeasures)
    ? { op: "set_model_options", discourage_implicit_measures: draft.discourageImplicitMeasures } : undefined;
}
export const AUTO_DATE_TIME_STEPS = "In Power BI Desktop, use File > Options and settings > Options > Current file > Data load, and turn Auto date/time on or off.";
export const CULTURE_STEPS = "In Power BI Desktop, use File > Options and settings > Options > Current file > Regional settings.";

// --- guards before a delete ---------------------------------------------------------------------------------------------

export type ReportUse = { pages: { title: string; visuals: { title: string; bindings: { kind: string; table: string; name: string }[] }[] }[]; measures: { TableName: string; Name: string; Expression: string }[] };
export type Guard = { blocking: string[]; warnings: string[] };

const mentionsColumn = (dax: string | undefined, table: string, column: string, ownTable?: string): boolean => {
  if (!dax) return false;
  const escaped = column.replaceAll("]", "]]").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const tableName = table.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replaceAll("'", "''");
  const qualified = new RegExp(`(?:'${tableName}'|\\b${tableName})\\s*\\[${escaped}\\]`, "i");
  if (qualified.test(dax)) return true;
  return ownTable !== undefined && same(ownTable, table) && new RegExp(`(^|[^\\]'\\w])\\[${escaped}\\]`, "i").test(dax);
};
const mentionsTable = (dax: string | undefined, table: string): boolean => {
  if (!dax) return false;
  const name = table.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replaceAll("'", "''");
  return new RegExp(`'${name}'|(^|[^\\w'\\[])${name}\\s*\\[|RELATEDTABLE\\s*\\(\\s*${name}\\b|\\(\\s*${name}\\s*[,)]`, "i").test(dax);
};
const listing = (names: string[], limit = 4) => names.length <= limit ? names.join(", ") : `${names.slice(0, limit).join(", ")} and ${names.length - limit} more`;

/** What still uses a column. Power BI refuses the delete while visuals, formulas, relationships, sort-by columns or hierarchy levels use it. */
export function columnDeleteGuard(facts: ModelFacts, report: ReportUse, table: string, column: string): Guard {
  const blocking: string[] = [];
  const warnings: string[] = [];
  const relationships = facts.relationships.filter(r => (same(r.fromTable, table) && same(r.fromColumn, column)) || (same(r.toTable, table) && same(r.toColumn, column)));
  if (relationships.length) blocking.push(`It is an end of ${relationships.length === 1 ? "a relationship" : `${relationships.length} relationships`} (${listing(relationships.map(r => relationshipLabel(r)))}). Delete ${relationships.length === 1 ? "it" : "them"} first.`);
  const sorted = facts.columns.filter(c => same(c.table, table) && same(c.sortByColumn, column));
  if (sorted.length) blocking.push(`${listing(sorted.map(c => c.name))} ${sorted.length === 1 ? "is" : "are"} sorted by it. Change the sort first.`);
  const hierarchies = facts.hierarchies.filter(h => same(h.table, table) && h.levels.some(l => same(l.column, column)));
  if (hierarchies.length) blocking.push(`It is a level of the hierarchy ${listing(hierarchies.map(h => h.name))}.`);
  const visuals = report.pages.flatMap(p => p.visuals.filter(v => v.bindings.some(b => b.kind !== "measure" && same(b.table, table) && same(b.name, column))).map(v => `“${v.title || "Untitled"}” on ${p.title}`));
  if (visuals.length) blocking.push(`${visuals.length === 1 ? "A visual uses" : `${visuals.length} visuals use`} it: ${listing(visuals, 3)}.`);
  const formulas = [
    ...report.measures.filter(m => mentionsColumn(m.Expression, table, column, m.TableName)).map(m => `the measure ${m.Name}`),
    ...facts.columns.filter(c => c.expression && !(same(c.table, table) && same(c.name, column)) && mentionsColumn(c.expression, table, column, c.table)).map(c => `the column ${c.table}[${c.name}]`),
    ...facts.tables.filter(t => t.calculated && mentionsColumn(t.expression, table, column)).map(t => `the table ${t.name}`),
    ...facts.roles.filter(r => r.tables.some(f => mentionsColumn(f.filter, table, column, f.table))).map(r => `the role ${r.name}`),
  ];
  if (formulas.length) blocking.push(`Formulas name it: ${listing(formulas)}.`);
  if (findTable(facts, table)?.dateColumn && same(findTable(facts, table)?.dateColumn, column)) warnings.push("It is the date column of a marked date table; the table is unmarked with it.");
  return { blocking, warnings };
}

/** What still uses a table. A table's measures are deleted with it. */
export function tableDeleteGuard(facts: ModelFacts, report: ReportUse, table: string): Guard {
  const blocking: string[] = [];
  const warnings: string[] = [];
  const relationships = facts.relationships.filter(r => same(r.fromTable, table) || same(r.toTable, table));
  if (relationships.length) blocking.push(`It has ${relationships.length === 1 ? "a relationship" : `${relationships.length} relationships`} (${listing(relationships.map(r => relationshipLabel(r)))}). Delete them first.`);
  const visuals = report.pages.flatMap(p => p.visuals.filter(v => v.bindings.some(b => same(b.table, table))).map(v => `“${v.title || "Untitled"}” on ${p.title}`));
  if (visuals.length) blocking.push(`${visuals.length === 1 ? "A visual uses" : `${visuals.length} visuals use`} its fields: ${listing(visuals, 3)}.`);
  const own = report.measures.filter(m => same(m.TableName, table));
  const formulas = [
    ...report.measures.filter(m => !same(m.TableName, table) && mentionsTable(m.Expression, table)).map(m => `the measure ${m.Name}`),
    ...facts.columns.filter(c => c.expression && !same(c.table, table) && mentionsTable(c.expression, table)).map(c => `the column ${c.table}[${c.name}]`),
    ...facts.tables.filter(t => t.calculated && !same(t.name, table) && mentionsTable(t.expression, table)).map(t => `the table ${t.name}`),
  ];
  if (formulas.length) blocking.push(`Formulas name it: ${listing(formulas)}.`);
  const roles = facts.roles.filter(r => r.tables.some(f => same(f.table, table)));
  if (roles.length) warnings.push(`The role ${listing(roles.map(r => r.name))} filters it; that filter goes with the table.`);
  if (own.length) warnings.push(`Its ${own.length === 1 ? "measure" : `${own.length} measures`} (${listing(own.map(m => m.Name))}) ${own.length === 1 ? "is" : "are"} deleted with it.`);
  return { blocking, warnings };
}

// --- Microsoft's supported list for external tools -----------------------------------------------------------------------

// Mirrors report_edit_model.MICROSOFT_SUPPORT / OUTSIDE_SUPPORT / outside_support. A change outside the documented list
// runs only when a passing model probe for the running Power BI Desktop version covers it; otherwise the server refuses it
// before Desktop opens, with Desktop's own steps.
export const MICROSOFT_SUPPORT: Record<string, "documented" | "outside" | "depends"> = {
  create_calculated_column: "documented", set_column_properties: "documented", rename_column: "outside", delete_column: "depends",
  create_calculated_table: "documented", set_table_properties: "depends", rename_table: "outside", delete_table: "depends", mark_date_table: "outside",
  create_relationship: "documented", update_relationship: "documented", delete_relationship: "documented", create_hierarchy: "outside",
  update_hierarchy: "outside", delete_hierarchy: "outside", create_field_parameter: "documented", update_field_parameter: "documented",
  create_calculation_group: "documented", set_calculation_item: "documented", delete_calculation_item: "documented", update_calculation_group: "documented",
  create_role: "documented", update_role: "documented", delete_role: "documented", set_table_query: "outside", set_named_expression: "outside",
  delete_named_expression: "outside", set_query_parameter: "outside", refresh_data: "outside", set_model_options: "outside",
};
const QUERY_STEPS = "In Power BI Desktop, use Home > Transform data, change the query in the Power Query Editor, then Close & Apply.";
export const OUTSIDE_SUPPORT: Record<string, { what: string; steps: string }> = {
  rename_column: { what: "Renaming a column", steps: "In Power BI Desktop, right-click the column in the Data pane and choose Rename; Desktop updates the visuals and formulas that use it." },
  rename_table: { what: "Renaming a table", steps: "In Power BI Desktop, right-click the table in the Data pane and choose Rename." },
  delete_data_column: { what: "Deleting a column loaded from the data source", steps: "In Power BI Desktop, right-click the column in the Data pane and choose Delete from model, or remove it in Power Query (Home > Transform data)." },
  delete_data_table: { what: "Deleting a table loaded from the data source", steps: "In Power BI Desktop, right-click the table in the Data pane and choose Delete from model." },
  data_table_properties: { what: "Changing a table loaded from the data source", steps: "In Power BI Desktop, right-click the table in the Data pane and choose Hide in report view, or set its description in the Model view's Properties pane." },
  mark_date_table: { what: "Marking a date table", steps: "In Power BI Desktop, select the table, then Table tools > Mark as date table." },
  hierarchies: { what: "Changing hierarchies", steps: "In Power BI Desktop's Model view, right-click a column and choose Create hierarchy, or change a hierarchy's levels in the Properties pane." },
  set_table_query: { what: "Changing a table's Power Query query", steps: QUERY_STEPS },
  named_expressions: { what: "Changing Power Query queries", steps: QUERY_STEPS },
  query_parameters: { what: "Changing Power Query parameters", steps: "In Power BI Desktop, use Home > Transform data > Edit parameters." },
  refresh_data: { what: "Refreshing the data", steps: "In Power BI Desktop, use Home > Refresh." },
  model_options: { what: "Changing the model's settings", steps: "In Power BI Desktop's Model view, select the model and set Discourage implicit measures in the Properties pane." },
};
const OUTSIDE_KEYS: Record<string, string> = {
  rename_column: "rename_column", rename_table: "rename_table", mark_date_table: "mark_date_table", create_hierarchy: "hierarchies", update_hierarchy: "hierarchies",
  delete_hierarchy: "hierarchies", set_table_query: "set_table_query", set_named_expression: "named_expressions", delete_named_expression: "named_expressions",
  set_query_parameter: "query_parameters", refresh_data: "refresh_data", set_model_options: "model_options",
};

export type SupportCoverage = {
  /** False until the payload carries `capabilities.model_support`: nothing is disabled here then (the server still refuses). */
  known: boolean; version?: string;
  /** Operations (`outside` or `depends`) whose probe key the installed Desktop's receipt covers. */
  verified: string[]; verifiedKeys: string[];
  outside: string[]; depends: string[]; keys: Record<string, string>;
};
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];

/**
 * Which model operations the installed Power BI Desktop may be sent: the payload's `capabilities.model_support`
 * `{desktop_version, documented, outside, depends, verified, keys, verified_keys}` (docs/imported-report-editing-api.md),
 * from the same model probe receipt the editor gates them with.
 */
export function readSupportCoverage(capabilities: unknown, fallbackVersion?: string): SupportCoverage {
  const caps = isRecord(capabilities) ? capabilities : {};
  const source = isRecord(caps.model_support) ? caps.model_support : undefined;
  if (!source) return { known: false, version: fallbackVersion, verified: [], verifiedKeys: [], outside: [], depends: [], keys: {} };
  const keys = isRecord(source.keys) ? Object.fromEntries(Object.entries(source.keys).filter((e): e is [string, string] => typeof e[1] === "string")) : {};
  return {
    known: true, version: text(source.desktop_version) ?? undefined, verified: strings(source.verified), verifiedKeys: strings(source.verified_keys),
    outside: strings(source.outside), depends: strings(source.depends), keys,
  };
}

/**
 * The probe key of a change outside Microsoft's documented list, or undefined for a documented one. A `depends`
 * operation (deleting a column or table, a table's properties) is documented on a calculated object and outside on one
 * loaded from the data source. The server's lists win over Studio's own mirror when the payload has them.
 */
export function supportKey(op: Operation, facts: ModelFacts, coverage?: SupportCoverage): string | undefined {
  const server = coverage?.known && (coverage.outside.length > 0 || coverage.depends.length > 0) ? coverage : undefined;
  const tag = server ? (server.outside.includes(op.op) ? "outside" : server.depends.includes(op.op) ? "depends" : "documented") : MICROSOFT_SUPPORT[op.op];
  const serverKey = coverage?.known ? coverage.keys[op.op] : undefined;
  if (tag === "outside") return serverKey ?? OUTSIDE_KEYS[op.op] ?? op.op;
  if (tag !== "depends") return undefined;
  const dataKey = serverKey;
  if (op.op === "delete_column") {
    const column = findColumn(facts, String(op.table ?? ""), String(op.name ?? ""));
    return column && column.kind === "data" ? dataKey ?? "delete_data_column" : undefined;
  }
  if (op.op === "delete_table" || op.op === "set_table_properties") {
    const table = findTable(facts, String(op.op === "delete_table" ? op.name ?? "" : op.table ?? ""));
    if (!table || tableKind(facts, table) !== "table") return undefined;
    return dataKey ?? (op.op === "delete_table" ? "delete_data_table" : "data_table_properties");
  }
  return undefined;
}

/** No word from the payload yet: nothing is disabled on the client. */
export const NO_COVERAGE: SupportCoverage = { known: false, verified: [], verifiedKeys: [], outside: [], depends: [], keys: {} };

export type SupportState = { outside: boolean; blocked: boolean; key?: string; label?: string; detail?: string; steps?: string };
/** What the workbench shows next to a change: nothing for a documented one; the "outside" fact, and a disabled button with Desktop's steps when the installed Desktop has no passing probe for it. */
export function supportState(op: Operation, facts: ModelFacts, coverage: SupportCoverage): SupportState {
  const key = supportKey(op, facts, coverage);
  if (!key) return { outside: false, blocked: false };
  const info = OUTSIDE_SUPPORT[key] ?? { what: "This change", steps: "Make it in Power BI Desktop." };
  const desktop = coverage.version ? `Power BI Desktop ${coverage.version}` : "this Power BI Desktop";
  if (coverage.known && (coverage.verifiedKeys.includes(key) || (MICROSOFT_SUPPORT[op.op] === "outside" && coverage.verified.includes(op.op)))) {
    return { outside: true, blocked: false, key, label: `Outside Microsoft's supported list; tested with ${desktop}`, detail: `${info.what} is not on Microsoft's list of changes supported for tools like Studio. Studio tested it with ${desktop}.` };
  }
  if (coverage.known) {
    return { outside: true, blocked: true, key, label: `Outside Microsoft's supported list; not tested with ${desktop}`, detail: `${info.what} is not on Microsoft's list of changes supported for tools like Studio, and Studio has not tested it with ${desktop}, so Studio does not make it.`, steps: info.steps };
  }
  return { outside: true, blocked: false, key, label: "Outside Microsoft's supported list", detail: `${info.what} is not on Microsoft's list of changes supported for tools like Studio. Studio makes it only where it passed a test with your Power BI Desktop version; otherwise Power BI's steps are shown when you apply.`, steps: info.steps };
}
/** The strictest state among several operations (a health fix, the tray). */
export function combinedSupport(ops: Operation[], facts: ModelFacts, coverage: SupportCoverage): SupportState {
  const states = ops.map(op => supportState(op, facts, coverage)).filter(s => s.outside);
  return states.find(s => s.blocked) ?? states[0] ?? { outside: false, blocked: false };
}

// --- the tray ---------------------------------------------------------------------------------------------------------

const MODEL_FAMILY = new Set(Object.keys(MICROSOFT_SUPPORT));
export const isModelFamilyOperation = (op: Operation) => MODEL_FAMILY.has(op.op);
const M_OPS = new Set(["set_table_query", "set_named_expression"]);
/** What kind of text an operation's `expression` is, for the tray's code block. */
export const expressionLanguage = (op: Operation): "dax" | "m" | undefined => typeof op.expression !== "string" ? undefined : M_OPS.has(op.op) ? "m" : "dax";

const q = (value: unknown) => String(value ?? "");
const field = (table: unknown, name: unknown) => `${q(table)}[${q(name)}]`;
/** One plain line for a model change in the tray (measure changes keep the tray's own wording: undefined). */
export function describeModelOp(op: Operation): string | undefined {
  switch (op.op) {
    case "create_calculated_column": return `Add the calculated column ${field(op.table, op.name)}`;
    case "set_column_properties": {
      const parts = [op.format_string !== undefined && "format", op.sort_by !== undefined && (op.sort_by === "" ? "stop sorting by another column" : `sort by ${q(op.sort_by)}`),
        op.data_category !== undefined && "data category", op.summarize_by !== undefined && "summarization", op.hidden !== undefined && (op.hidden ? "hide" : "show"),
        op.folder !== undefined && "folder", op.description !== undefined && "description", op.data_type !== undefined && "data type", op.expression !== undefined && "formula",
        op.key !== undefined && "key"].filter(Boolean);
      return `Column ${field(op.table, op.name)}: ${parts.join(", ") || "settings"}`;
    }
    case "rename_column": return `Rename the column ${field(op.table, op.name)} to ${q(op.new_name)}`;
    case "delete_column": return `Delete the column ${field(op.table, op.name)}`;
    case "create_calculated_table": return `Add the calculated table ${q(op.name)}`;
    case "set_table_properties": return `Table ${q(op.table)}: ${[op.hidden !== undefined && (op.hidden ? "hide" : "show"), op.description !== undefined && "description", op.expression !== undefined && "formula"].filter(Boolean).join(", ") || "settings"}`;
    case "rename_table": return `Rename the table ${q(op.name)} to ${q(op.new_name)}`;
    case "delete_table": return `Delete the table ${q(op.name)}`;
    case "mark_date_table": return op.column ? `Mark ${q(op.table)} as the date table (on ${q(op.column)})` : `Unmark ${q(op.table)} as a date table`;
    case "create_relationship": return `Relate ${field(op.from_table, op.from_column)} to ${field(op.to_table, op.to_column)}`;
    case "update_relationship": return `Change the relationship ${field(op.from_table, op.from_column)} → ${field(op.to_table, op.to_column)}`;
    case "delete_relationship": return `Delete the relationship ${field(op.from_table, op.from_column)} → ${field(op.to_table, op.to_column)}`;
    case "create_hierarchy": return `Add the hierarchy ${q(op.name)} to ${q(op.table)}`;
    case "update_hierarchy": return `Change the hierarchy ${q(op.name)} of ${q(op.table)}${op.new_name ? ` (rename to ${q(op.new_name)})` : ""}`;
    case "delete_hierarchy": return `Delete the hierarchy ${q(op.name)} of ${q(op.table)}`;
    case "create_field_parameter": return `Add the field parameter ${q(op.name)}`;
    case "update_field_parameter": return `Change the fields of the parameter ${q(op.name)}`;
    case "create_calculation_group": return `Add the calculation group ${q(op.name)}`;
    case "set_calculation_item": return `Calculation item ${q(op.name)} of ${q(op.group)}${op.new_name ? ` (rename to ${q(op.new_name)})` : ""}`;
    case "delete_calculation_item": return `Delete the calculation item ${q(op.name)} of ${q(op.group)}`;
    case "update_calculation_group": return `Change the calculation group ${q(op.name)}`;
    case "create_role": return `Add the role ${q(op.name)}`;
    case "update_role": return `Change the role ${q(op.name)}${op.new_name ? ` (rename to ${q(op.new_name)})` : ""}`;
    case "delete_role": return `Delete the role ${q(op.name)}`;
    case "set_table_query": return `Change the Power Query of ${q(op.table)} and reload it`;
    case "set_named_expression": return `Change the query ${q(op.name)}`;
    case "delete_named_expression": return `Delete the query ${q(op.name)}`;
    case "set_query_parameter": return `Set the parameter ${q(op.name)} to ${q(op.value)}`;
    case "refresh_data": return op.table ? `Refresh the data of ${q(op.table)}` : "Refresh all the data";
    case "set_model_options": return op.discourage_implicit_measures === undefined ? "Change the model's settings" : `${op.discourage_implicit_measures ? "Discourage" : "Allow"} implicit measures`;
    default: return undefined;
  }
}

// Changes that restate the same object's settings fold into one entry, so the tray stays what Desktop will actually do.
const FOLD_KEYS: Record<string, (op: Operation) => string> = {
  set_column_properties: op => `${fold(q(op.table))}[${fold(q(op.name))}]`,
  set_table_properties: op => fold(q(op.table)),
  update_relationship: op => relationshipKey({ fromTable: q(op.from_table), fromColumn: q(op.from_column), toTable: q(op.to_table), toColumn: q(op.to_column) }),
  update_calculation_group: op => fold(q(op.name)),
  set_table_query: op => fold(q(op.table)),
  set_model_options: () => "model",
  set_query_parameter: op => fold(q(op.name)),
  update_field_parameter: op => fold(q(op.name)),
};
const foldKey = (op: Operation): string | undefined => {
  if (op.op === "set_calculation_item" && !op.new_name) return `${fold(q(op.group))}|${fold(q(op.name))}`;
  const key = FOLD_KEYS[op.op];
  return key ? key(op) : undefined;
};
export type PendingLike = {
  get: (workspaceId: string) => PendingChange[];
  add: (workspaceId: string, op: Operation, extra?: { dax?: string; whatIf?: WhatIf }) => PendingChange[];
  remove: (workspaceId: string, id: string) => void;
};
/** Queues a model change in the tray. A later change to the same object's settings replaces the earlier entry with both merged. */
export function queueModelChange(store: PendingLike, workspaceId: string, op: Operation): "added" | "merged" {
  const key = foldKey(op);
  const earlier = key === undefined ? undefined : store.get(workspaceId).find(change => change.op.op === op.op && foldKey(change.op) === key);
  if (!earlier) { store.add(workspaceId, op); return "added"; }
  const merged: Operation = { ...earlier.op };
  for (const [name, value] of Object.entries(op)) if (value !== undefined) merged[name] = value;
  store.remove(workspaceId, earlier.id);
  store.add(workspaceId, merged);
  return "merged";
}

// --- model health (docs/imported-model-health.md) -----------------------------------------------------------------------

export type HealthCategory = "correctness" | "performance" | "usability" | "formatting" | "naming";
export const HEALTH_CATEGORIES: HealthCategory[] = ["correctness", "performance", "usability", "formatting", "naming"];
export const CATEGORY_LABELS: Record<HealthCategory, string> = { correctness: "Correctness", performance: "Performance", usability: "Usability", formatting: "Formatting", naming: "Naming" };
export type HealthObject = { kind: "model" | "table" | "column" | "measure" | "calculation_item" | "relationship"; table?: string; name?: string; to_table?: string; to_column?: string };
export type HealthFinding = {
  id: string; rule: string; category: HealthCategory; severity: "problem" | "suggestion"; objects: HealthObject[]; message: string; why: string;
  fix: Operation[] | null; fix_summary: string; fix_needs_desktop: boolean; fix_outside_supported: boolean;
};
export type Health = {
  known: boolean; findings: HealthFinding[]; summary: { problems: number; suggestions: number; by_category: Record<HealthCategory, number> };
  checked: Record<string, number>; notes: string[]; head?: { id: string; hash: string };
};
const KINDS = new Set(["model", "table", "column", "measure", "calculation_item", "relationship"]);

export function readHealth(raw: unknown): Health {
  const body = isRecord(raw) ? raw : {};
  const findings = items(body.findings).flatMap((f): HealthFinding[] => {
    const category = HEALTH_CATEGORIES.includes(f.category as HealthCategory) ? f.category as HealthCategory : undefined;
    if (!text(f.id) || !category || !text(f.message)) return [];
    return [{
      id: String(f.id), rule: text(f.rule) ?? "", category, severity: f.severity === "problem" ? "problem" : "suggestion",
      objects: items(f.objects).flatMap(o => KINDS.has(String(o.kind)) ? [{ kind: o.kind as HealthObject["kind"], table: text(o.table), name: text(o.name), to_table: text(o.to_table), to_column: text(o.to_column) }] : []),
      message: String(f.message), why: text(f.why) ?? "",
      fix: Array.isArray(f.fix) ? f.fix.filter((op): op is Operation => isRecord(op) && typeof op.op === "string") : null,
      fix_summary: text(f.fix_summary) ?? "", fix_needs_desktop: f.fix_needs_desktop !== false, fix_outside_supported: f.fix_outside_supported === true,
    }];
  });
  const summary = isRecord(body.summary) ? body.summary : {};
  const byCategory = isRecord(summary.by_category) ? summary.by_category : {};
  return {
    known: body.known !== false, findings,
    summary: {
      problems: typeof summary.problems === "number" ? summary.problems : findings.filter(f => f.severity === "problem").length,
      suggestions: typeof summary.suggestions === "number" ? summary.suggestions : findings.filter(f => f.severity === "suggestion").length,
      by_category: Object.fromEntries(HEALTH_CATEGORIES.map(c => [c, typeof byCategory[c] === "number" ? byCategory[c] as number : findings.filter(f => f.category === c).length])) as Record<HealthCategory, number>,
    },
    checked: isRecord(body.checked) ? Object.fromEntries(Object.entries(body.checked).filter(([, v]) => typeof v === "number")) as Record<string, number> : {},
    notes: Array.isArray(body.notes) ? body.notes.filter((n): n is string => typeof n === "string") : [],
    head: isRecord(body.head) && text(body.head.id) ? { id: String(body.head.id), hash: text(body.head.hash) ?? "" } : undefined,
  };
}

/** Findings grouped by category in the fixed order (problems first inside each group), empty groups left out. */
export function groupFindings(findings: HealthFinding[], filter?: { category?: HealthCategory; severity?: "problem" | "suggestion" }): { category: HealthCategory; label: string; findings: HealthFinding[] }[] {
  return HEALTH_CATEGORIES.filter(c => !filter?.category || filter.category === c).map(category => ({
    category, label: CATEGORY_LABELS[category],
    findings: findings.filter(f => f.category === category && (!filter?.severity || f.severity === filter.severity))
      .sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "problem" ? -1 : 1)),
  })).filter(group => group.findings.length > 0);
}

export function objectLabel(o: HealthObject): string {
  switch (o.kind) {
    case "column": return `${o.table ?? ""}[${o.name ?? ""}]`;
    case "measure": return `[${o.name ?? ""}]`;
    case "table": return o.name ?? o.table ?? "";
    case "calculation_item": return `${o.name ?? ""} (${o.table ?? ""})`;
    case "relationship": return `${o.table ?? ""}[${o.name ?? ""}] → ${o.to_table ?? ""}[${o.to_column ?? ""}]`;
    default: return "The model";
  }
}

/** The fix a finding offers: its operations to queue (at most 50 go in one Apply), or the words of what to do by hand. */
export function healthFix(finding: HealthFinding): { ops: Operation[]; byHand?: undefined } | { ops?: undefined; byHand: string } {
  return finding.fix && finding.fix.length ? { ops: finding.fix } : { byHand: finding.fix_summary || "Studio has no automatic fix for this; it is a judgement call." };
}

// --- the routes ---------------------------------------------------------------------------------------------------------

export type ModelClient = ReturnType<typeof createModelClient>;
const problemMessage = (response: RawResponse, fallback: string): string => {
  const body = isRecord(response.body) ? response.body : {};
  const detail = isRecord(body.detail) ? body.detail : body;
  return text(detail.message) ?? text(body.detail) ?? fallback;
};

export function createModelClient(transport: Transport, options: { sleep?: (ms: number) => Promise<void>; pollMs?: number; maxPolls?: number; key?: () => string } = {}) {
  const base = (workspaceId: string) => `/v1/workspaces/${encodeURIComponent(workspaceId)}/imported-report`;
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  const get = async (path: string, fallback: string): Promise<unknown> => {
    const response = await transport("GET", path);
    if (response.status >= 400) throw new Error(problemMessage(response, fallback));
    return response.body;
  };
  return {
    /** The head's full facts (Power Query and DAX uncut), the whole model or one table's part. Never chat context. */
    async fullFacts(workspaceId: string, table?: string): Promise<ModelFacts> {
      return readModelFacts(await get(`${base(workspaceId)}/model${table ? `?table=${encodeURIComponent(table)}` : ""}`, "Studio could not read this report's model."));
    },
    async health(workspaceId: string, stats = false): Promise<Health> {
      return readHealth(await get(`${base(workspaceId)}/model-health?stats=${stats ? "true" : "false"}`, "Studio could not check the model."));
    },
    /** Reads the report's rows again (after Power BI refreshed or added data); waits while the server runs it. */
    /** Checks a Power Query formula as an edit would (a table's query with `table`, else a shared query); nothing is queued. */
    async validateQuery(workspaceId: string, expression: string, table?: string): Promise<QueryCheck> {
      const response = await transport("POST", `${base(workspaceId)}/model/validate-query`, { expression, ...(table ? { table } : {}) });
      if (response.status >= 400) throw new Error(problemMessage(response, "Studio could not check this query."));
      return readQueryCheck(response.body);
    },
    async rereadData(workspaceId: string): Promise<void> {
      const response = await transport("POST", `${base(workspaceId)}/reread-data`, { idempotency_key: (options.key ?? (() => crypto.randomUUID()))() });
      if (response.status >= 400) throw new Error(problemMessage(response, "Studio could not read the data again."));
      const batch = isRecord(response.body) && response.status === 202 ? text(response.body.batch_id) : undefined;
      if (!batch) return;
      for (let i = 0; i < (options.maxPolls ?? 200); i++) {
        await sleep(options.pollMs ?? 1500);
        const status = await transport("GET", `${base(workspaceId)}/edits/${encodeURIComponent(batch)}`);
        const body = isRecord(status.body) ? status.body : {};
        if (status.status >= 400) throw new Error(problemMessage(status, "Studio could not read the data again."));
        if (body.state === "done") return;
        if (body.state === "failed") throw new Error(isRecord(body.error) ? text(body.error.message) ?? "Studio could not read the data again." : "Studio could not read the data again.");
      }
      throw new Error("Reading the data again is taking longer than usual. It keeps running; check again in a moment.");
    },
  };
}
