// The pure parts of DAX Lab's "Save as a measure" for an imported report (SaveImportedMeasure, DaxLab).

const HEADER = /^\s*([A-Za-z_][^=\n[\]'"(){}]{0,120}?)\s*:?=(?!=)/;
// "VAR total = SUM(...)" and "DEFINE VAR x = 1" look like "Name =" to the header rule, but the "=" belongs to a
// variable: the whole text is a formula (or a query), not a measure definition.
const KEYWORD_FIRST = /^(var|define|evaluate)\s/i;

function measureHeader(expression: string): { name: string; length: number } | undefined {
  const match = expression.match(HEADER);
  const name = match?.[1].trim();
  if (!match || !name || KEYWORD_FIRST.test(name)) return undefined;
  return { name, length: match[0].length };
}

/** "Profit Margin % = DIVIDE(...)" gives "Profit Margin %" (mirrors the engine's header rule). */
export function measureHeaderName(expression: string): string | undefined {
  return measureHeader(expression)?.name;
}

/**
 * A formula pasted with its own "Name =" line, split into that name and the formula alone. Power BI stores a
 * measure's name apart from its formula, and the edit layer refuses a formula that still carries a definition.
 * A formula that starts with a variable ("VAR total = ...") is kept whole.
 */
export function splitMeasureDefinition(expression: string): { name?: string; formula: string } {
  const header = measureHeader(expression);
  if (!header) return { formula: expression.trim() };
  const formula = expression.slice(header.length).trim();
  return formula ? { name: header.name, formula } : { formula: expression.trim() };
}

/**
 * The tables a new measure can live in, in the order the picker shows them: the tables that already hold measures
 * first (usually a dedicated measures table, which is where a Power BI developer would look), then the other tables
 * of the model, each once. Power BI's automatic date tables are not offered: nobody puts a measure in one.
 */
export function measureHomeTables(tables: { TableName: string }[], measures: { TableName: string }[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const item of [...measures, ...tables]) {
    if (!item.TableName || seen.has(item.TableName) || /^(LocalDateTable|DateTableTemplate)_/.test(item.TableName)) continue;
    seen.add(item.TableName);
    result.push(item.TableName);
  }
  return result;
}

/** What DAX Lab says after queueing a measure of an imported report (nothing has reached the report yet). */
export function queuedMeasureNote(name: string): string {
  return `Queued “${name}”. Apply it from Pending Power BI changes.`;
}
