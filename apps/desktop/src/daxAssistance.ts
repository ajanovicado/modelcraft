import type { DaxResult, Snapshot } from "./types";

/** Obvious adjacent expressions from accidental schema clicks; never rewrite user DAX. */
export function joinedDaxReferences(expression: string) {
  return /\]\s*(?:SUM|AVERAGE|MIN|MAX|COUNT|CALCULATE|DIVIDE)\s*\(/i.test(expression)
    || /\)\s*\[[^\]]+\]\s*$/.test(expression);
}

/** Imported column IDs are unique within a table, not across the whole model. */
export function nativeGroupingFields(tables: Snapshot["tables"]) {
  return tables.flatMap(t => t.table.columns.map(c => ({
    id: JSON.stringify([t.id, c.id]), tableId: t.id, table: t.table.name, name: c.name,
  })));
}

export function nativeBreakdownQuery(expression: string, table: string, column: string) {
  const field = `'${table.replaceAll("'", "''")}'[${column.replaceAll("]", "]]")}]`;
  const query = `EVALUATE SUMMARIZECOLUMNS(${field}, "Value", (${expression}), "__StudioNumeric", IGNORE(ISNUMBER((${expression})))) ORDER BY ${field}`;
  if (query.length > 4000) throw new Error("This formula is too long for a grouped preview. Run the overall result or shorten the expression.");
  return query;
}

export function daxAssistantPrompt(goal: string, expression: string, error?: string) {
  const prompt = [
    "Explain and suggest DAX only. Do not change, save, or build the report or draft.",
    "Use only fields in the supplied workspace schema. Return one formula in a fenced ```dax code block, followed by a brief explanation. Return the expression without a measure-name assignment. Do not invent results.",
    "For a local preview, prefer SUM, AVERAGE, MIN, MAX, COUNT, DISTINCTCOUNT, COUNTROWS, DIVIDE, CALCULATE with simple filters, or EVALUATE SUMMARIZECOLUMNS. Say when full Power BI execution is needed.",
    "For grouped rows, use this syntax: EVALUATE SUMMARIZECOLUMNS('Table'[Group], \"Total\", SUM('Table'[Amount])). These names are placeholders: replace them with real fields. The first argument is a grouping COLUMN, not a table. A scalar aggregation alone cannot return grouped rows.",
    expression.trim() ? `Current editor formula (reference):\n${expression.trim()}` : "The editor is empty.",
    error ? `Local evaluation error (reference):\n${error.slice(0, 600)}` : "",
    `My request: ${goal.trim()}`,
  ].filter(Boolean).join("\n\n");
  if (prompt.length > 4000) throw new Error("This formula and request are too long to send together. Shorten the request or select a smaller formula.");
  return prompt;
}

export function normalizeDaxSuggestion(code: string) {
  const text = code.trim();
  const assignment = text.match(/^([A-Za-z_][\w ]*)\s*=\s*([\s\S]+)$/);
  if (assignment && !/^(?:VAR|RETURN|EVALUATE|DEFINE)\b/i.test(assignment[1])) {
    return { expression: assignment[2].trim(), name: assignment[1].replace(/^\[|\]$/g, "").trim() };
  }
  return { expression: text, name: "" };
}

/** Apply only an unambiguous, completed code block; never stream partial DAX into the editor. */
export function extractDaxFormula(answer: string) {
  if ((answer.match(/^```/gm) ?? []).length !== 2) return;
  const blocks = [...answer.matchAll(/^```([^\r\n]*)\r?\n([\s\S]*?)^```\s*$/gm)];
  if (blocks.length !== 1 || !/^dax\s*$/i.test(blocks[0][1])) return;
  const formula = normalizeDaxSuggestion(blocks[0][2]);
  return formula.expression && formula.expression.length <= 4000 ? formula : undefined;
}

export function daxResultDefaults(result: DaxResult, snapshot?: Snapshot) {
  const numeric = numericResultColumns(result);
  const keys = new Set(snapshot?.tables.flatMap(t => t.table.columns.filter(c => c.candidate_key).map(c => c.name.toLowerCase())) ?? []);
  const identifier = (name: string) => (numeric.includes(name) && keys.has(name.toLowerCase())) || /(?:^|[\s_[])\b(?:id|key|code)\b(?:$|\])/i.test(name) || /(?:ID|Key)$/.test(name);
  const metric = numeric.find(column => !identifier(column)) ?? "";
  const dimensions = result.columns.filter(column => column !== result.grand_total_column && !numeric.includes(column));
  const category = dimensions.find(column => column !== metric)
    ?? result.columns.find(column => column !== metric && identifier(column)) ?? "";
  const view = !metric || result.grand_total_column || dimensions.length > 1 || result.truncated || result.rows.length > 20 || identifier(category) ? "table" : "visual";
  return { metric, category, view: view as "table" | "visual" };
}

export function isDaxTableExpression(expression: string) {
  return /^\s*(?:(?:EVALUATE|DEFINE)\b|(?:SUMMARIZECOLUMNS|VALUES|TOPN)\s*\()/i.test(expression);
}

export function daxRequestSummary(message: string) {
  if (!message.startsWith("Explain and suggest DAX only. Do not change, save, or build the report or draft.\n\n")) return;
  const split = message.lastIndexOf("\n\nMy request: ");
  if (split < 0) return;
  return { request: message.slice(split + "\n\nMy request: ".length), context: message.slice(0, split) };
}

export function daxExamples(snapshot?: Snapshot) {
  const table = snapshot?.tables.find(item => item.table.columns.some(column => ["number", "integer"].includes(column.data_type)))?.table ?? snapshot?.tables[0]?.table;
  if (!table) return [];
  const quoted = `'${table.name.replaceAll("'", "''")}'`;
  const numeric = table.columns.find(column => ["number", "integer"].includes(column.data_type) && !column.candidate_key);
  const group = table.columns.find(column => column.data_type === "string" && column.distinct_count > 1 && column.distinct_count <= 30 && !column.candidate_key);
  const field = (name: string) => `${quoted}[${name.replaceAll("]", "]]")}]`;
  return [
    { label: "Row scan", expression: `EVALUATE ${quoted}` },
    { label: "Row count", expression: `COUNTROWS(${quoted})` },
    ...(numeric ? [{ label: "Total", expression: `SUM(${field(numeric.name)})` }] : []),
    ...(numeric && group ? [{ label: "Grouped", expression: `EVALUATE SUMMARIZECOLUMNS(${field(group.name)}, "Total ${numeric.name.replaceAll('"', '""')}", SUM(${field(numeric.name)}))` }] : []),
  ];
}

export function numericResultColumns(result: DaxResult) {
  return result.columns.filter(column => {
    const values = result.rows.map(row => row[column]).filter(value => value !== null && value !== undefined);
    return values.length > 0 && values.every(value => typeof value === "number" && Number.isFinite(value));
  });
}

export function daxChartRows(result: DaxResult, category: string, metric: string) {
  const values = result.rows.filter(row => !result.grand_total_column || row[result.grand_total_column] !== true).map((row, index) => ({
    label: category ? String(row[category] ?? "(blank)") : `Row ${index + 1}`,
    value: row[metric], index,
  }));
  const labels = new Map<string, number>();
  for (const row of values) labels.set(row.label, (labels.get(row.label) ?? 0) + 1);
  return values.filter((row): row is { label: string; value: number; index: number } => typeof row.value === "number" && Number.isFinite(row.value))
    .slice(0, 20).map(row => ({ category: (labels.get(row.label) ?? 0) > 1 ? `${row.label} · row ${row.index + 1}` : row.label, value: row.value }));
}
