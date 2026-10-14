// What Studio judges about an imported report, for the editor and the chat: the page review
// (docs/imported-report-review.md), report tests on pinned numbers (docs/imported-report-tests.md) and
// "explain this number" (docs/imported-number-explain.md), plus the planner's plan card
// (docs/imported-report-editing-api.md, `edit_plan`). Pure readers and request builders, tested in node;
// the transport is injected. Nothing here imports a value from importedEditing.ts (types only), so both
// modules can use each other.

import type { Operation, RawResponse } from "./importedEditing";

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const str = (value: unknown): string | undefined => typeof value === "string" && value.trim() ? value : undefined;
const strList = (value: unknown): string[] => Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
const num = (value: unknown): number | undefined => typeof value === "number" && Number.isFinite(value) ? value : undefined;
const asOps = (value: unknown): Operation[] => Array.isArray(value) ? value.filter((v): v is Operation => isRecord(v) && typeof v.op === "string") : [];

export type CellValue = string | number | boolean | null;
const cell = (value: unknown): CellValue | undefined =>
  value === null || typeof value === "string" || typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value)) ? value : undefined;

// --- review ---------------------------------------------------------------------------------------

export type ReviewFinding = {
  id: string; rule: string; severity: "problem" | "suggestion"; visuals: string[]; page?: string;
  message: string; fix?: Operation[]; fixSummary?: string;
};
export type Review = {
  page?: string; pageName?: string; findings: ReviewFinding[]; problems: number; suggestions: number;
  skipped: { rule: string; reason: string }[]; notes: string[];
};

/** Findings as the review and the edit receipt send them; a fix that is not a list of operations is no fix. */
export function readFindings(value: unknown): ReviewFinding[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): ReviewFinding[] => {
    if (!isRecord(item)) return [];
    const message = str(item.message);
    if (!message) return [];
    const fix = asOps(item.fix);
    return [{
      id: str(item.id) ?? `${str(item.rule) ?? "finding"}-${message.length}`, rule: str(item.rule) ?? "", severity: item.severity === "problem" ? "problem" : "suggestion",
      visuals: strList(item.visuals), page: str(item.page), message, ...(fix.length ? { fix, fixSummary: str(item.fix_summary) } : {}),
    }];
  });
}

export function readReview(body: unknown): Review {
  const source = isRecord(body) ? body : {};
  const findings = readFindings(source.findings);
  const skipped = isRecord(source.skipped) ? Object.entries(source.skipped).flatMap(([rule, reason]) => typeof reason === "string" ? [{ rule, reason }] : []) : [];
  return {
    page: str(source.page), pageName: str(source.page_name), findings,
    problems: findings.filter(f => f.severity === "problem").length, suggestions: findings.filter(f => f.severity === "suggestion").length,
    skipped, notes: strList(source.notes),
  };
}

/** A model health finding a model edit introduced (receipt `model_health.new`, docs/imported-model-health.md): names objects only. */
export type HealthNote = { id: string; severity: string; category?: string; message: string; why?: string };
export function readHealthNotes(value: unknown): HealthNote[] {
  const list = isRecord(value) ? value.new : value;
  if (!Array.isArray(list)) return [];
  return list.flatMap((item): HealthNote[] => isRecord(item) && str(item.message)
    ? [{ id: str(item.id) ?? String(item.message), severity: str(item.severity) ?? "suggestion", category: str(item.category), message: String(item.message), why: str(item.why) }] : []);
}

/**
 * The Review panel's state. `head` is the revision the shown review was read on; `asked` the one the request in flight
 * names. A review of an older head is read again for the new one (after its own Fix, or any edit), and a request that
 * the report outran (409 head_moved) is not an error: it is asked again for the head the editor has now.
 */
export type ReviewState = { loading: boolean; review?: Review; error?: string; fixed: string[]; head?: string; asked?: string };

/** A judgement request refused because the report moved to a newer head while it was on its way. */
export const isHeadMoved = (error: unknown): boolean =>
  typeof error === "object" && error !== null && ((error as { code?: unknown }).code === "head_moved" || (error as { status?: unknown }).status === 409);

/** Whether an open panel should review again: its review (or the request in flight) is for another head than the editor's. */
export function reviewNeedsRerun(state: ReviewState | undefined, head: string | undefined): boolean {
  if (!state || !head) return false;
  return state.loading ? state.asked !== undefined && state.asked !== head : state.head !== head;
}

export const reviewStarted = (state: ReviewState | undefined, head: string | undefined): ReviewState =>
  ({ loading: true, review: state?.review, fixed: state?.fixed ?? [], head: state?.head, asked: head });

export const reviewDone = (state: ReviewState | undefined, review: Review, head: string | undefined): ReviewState =>
  ({ loading: false, review, fixed: state?.fixed ?? [], head });

/**
 * A failed review. A head that moved keeps what was shown and asks again when the editor has a newer head than the one
 * sent (`retry`); otherwise the panel waits for the head to arrive (`reviewNeedsRerun` then asks). Any other failure is shown.
 */
export function reviewFailed(state: ReviewState | undefined, error: unknown, sent: string | undefined, current: string | undefined): { state: ReviewState; retry: boolean } {
  if (isHeadMoved(error)) {
    const retry = current !== undefined && current !== sent;
    // Nothing newer known yet: stay "loading" for the head that was sent, so the next head the editor learns re-runs it
    // (and nothing is asked again while the head stays the same).
    return { state: { loading: true, review: state?.review, fixed: state?.fixed ?? [], head: state?.head, asked: retry ? current : sent }, retry };
  }
  return { state: { loading: false, review: state?.review, fixed: state?.fixed ?? [], head: state?.head, error: error instanceof Error ? error.message : "The page could not be reviewed." }, retry: false };
}

/** One line for a finding's badge. */
export const severityLabel = (finding: Pick<ReviewFinding, "severity">) => finding.severity === "problem" ? "Problem" : "Suggestion";

/** "1 problem and 4 suggestions", "Nothing to fix on this page." */
export function reviewHeadline(review: Pick<Review, "problems" | "suggestions">): string {
  const parts = [
    review.problems ? `${review.problems} problem${review.problems === 1 ? "" : "s"}` : "",
    review.suggestions ? `${review.suggestions} suggestion${review.suggestions === 1 ? "" : "s"}` : "",
  ].filter(Boolean);
  return parts.length ? `Studio noticed ${parts.join(" and ")}` : "Nothing to fix on this page";
}

// --- report tests ----------------------------------------------------------------------------------

export type TestFilter = { table: string; column: string; op: "in" | "not_in" | "between"; values: CellValue[]; date?: boolean; point?: boolean; measure?: boolean };
export type Expectation = { equals: CellValue; tolerance_abs?: number | null; tolerance_rel?: number | null } | { between: [number | null, number | null] } | { not_blank: true };
export type TestDefinition = {
  id: string; name: string; measure?: { table: string; name: string }; expression?: string; filters: TestFilter[];
  expect: Expectation; source?: string; format?: string; origin?: { page?: string; visual?: string }; createdAt?: string;
};
export type TestStatus = "passed" | "failed" | "not_checked";
export type TestResult = { id: string; name: string; status: TestStatus; value?: CellValue; shown?: string; expected?: string; message: string; engine?: string };
export type TestRun = { revision?: string; engine?: string; ranAt?: string; results: TestResult[]; text?: string };
export type TestsState = { tests: TestDefinition[]; lastRun?: TestRun; stale: boolean };

function readFilter(raw: unknown): TestFilter | undefined {
  if (!isRecord(raw)) return undefined;
  const table = str(raw.table), column = str(raw.column);
  const op = raw.op === "not_in" || raw.op === "between" ? raw.op : "in";
  if (!table || !column || !Array.isArray(raw.values)) return undefined;
  return { table, column, op, values: raw.values.map(value => cell(value) ?? null), ...(raw.date === true ? { date: true } : {}), ...(raw.point === true ? { point: true } : {}), ...(raw.measure === true ? { measure: true } : {}) };
}

function readExpect(raw: unknown): Expectation {
  if (isRecord(raw)) {
    if (raw.not_blank === true) return { not_blank: true };
    if (Array.isArray(raw.between) && raw.between.length === 2) return { between: [num(raw.between[0]) ?? null, num(raw.between[1]) ?? null] };
    if ("equals" in raw) return { equals: cell(raw.equals) ?? null, tolerance_abs: num(raw.tolerance_abs) ?? null, tolerance_rel: num(raw.tolerance_rel) ?? null };
  }
  return { not_blank: true };
}

export function readTest(raw: unknown): TestDefinition | undefined {
  if (!isRecord(raw)) return undefined;
  const id = str(raw.id), name = str(raw.name);
  if (!id || !name) return undefined;
  const measure = isRecord(raw.measure) && str(raw.measure.table) && str(raw.measure.name) ? { table: String(raw.measure.table), name: String(raw.measure.name) } : undefined;
  const origin = isRecord(raw.origin) ? { page: str(raw.origin.page), visual: str(raw.origin.visual) } : undefined;
  return {
    id, name, ...(measure ? { measure } : {}), ...(str(raw.expression) ? { expression: String(raw.expression) } : {}),
    filters: Array.isArray(raw.filters) ? raw.filters.map(readFilter).filter((f): f is TestFilter => f !== undefined) : [],
    expect: readExpect(raw.expect), source: str(raw.source), format: str(raw.format), ...(origin ? { origin } : {}), createdAt: str(raw.created_at),
  };
}

export function readResult(raw: unknown): TestResult | undefined {
  if (!isRecord(raw)) return undefined;
  const id = str(raw.id);
  if (!id) return undefined;
  const status: TestStatus = raw.status === "passed" || raw.status === "failed" ? raw.status : "not_checked";
  return { id, name: str(raw.name) ?? id, status, value: cell(raw.value) ?? null, shown: str(raw.shown), expected: str(raw.expected), message: str(raw.message) ?? "", engine: str(raw.engine) };
}
const readResults = (value: unknown): TestResult[] => Array.isArray(value) ? value.map(readResult).filter((r): r is TestResult => r !== undefined) : [];

export function readTestRun(raw: unknown): TestRun | undefined {
  if (!isRecord(raw)) return undefined;
  return { revision: str(raw.revision), engine: str(raw.engine), ranAt: str(raw.ran_at), results: readResults(raw.results), text: str(raw.text) };
}

export function readTestsState(body: unknown): TestsState {
  const source = isRecord(body) ? body : {};
  const tests = Array.isArray(source.tests) ? source.tests.map(readTest).filter((t): t is TestDefinition => t !== undefined) : [];
  return { tests, lastRun: readTestRun(source.last_run), stale: source.stale === true };
}

/** The receipt's `tests` (a model edit on a report with tests), as the lines the receipt shows. */
export function receiptTestLines(value: unknown): string[] {
  if (!isRecord(value)) return [];
  const results = readResults(value.results);
  const broken = readResults(value.broken);
  const lines: string[] = [];
  const text = str(value.text);
  if (text) lines.push(text);
  else if (results.length) {
    const passed = results.filter(r => r.status === "passed").length;
    lines.push(`${passed} of ${results.length} report test${results.length === 1 ? "" : "s"} still pass${passed === 1 && results.length === 1 ? "es" : ""}.`);
  }
  const brokenText = str(value.text_broken);
  if (brokenText && !lines.includes(brokenText)) lines.push(brokenText);
  else if (!brokenText && broken.length) lines.push(...broken.map(r => r.message));
  return lines;
}

/** Each test's latest status (from the last run), `undefined` for a test the run did not cover. */
export function testStatuses(state: Pick<TestsState, "tests" | "lastRun">): Map<string, TestResult> {
  const found = new Map<string, TestResult>();
  for (const result of state.lastRun?.results ?? []) found.set(result.id, result);
  return found;
}

/** The canvas badge for each visual that has tests: `failed` when one of them fails, else `passed`, else `not_checked`. */
export function visualTestBadges(state: Pick<TestsState, "tests" | "lastRun">): Map<string, TestStatus> {
  const statuses = testStatuses(state);
  const badges = new Map<string, TestStatus>();
  const rank: Record<TestStatus, number> = { passed: 0, not_checked: 1, failed: 2 };
  for (const test of state.tests) {
    const visual = test.origin?.visual;
    if (!visual) continue;
    const status = statuses.get(test.id)?.status ?? "not_checked";
    const current = badges.get(visual);
    if (current === undefined || rank[status] > rank[current]) badges.set(visual, status);
  }
  return badges;
}

const plain = (value: CellValue, grouping = true): string => value === null ? "blank" : typeof value === "number" ? value.toLocaleString("en-US", { maximumFractionDigits: 6, useGrouping: grouping }) : String(value);

/** "equals 850 (within 1)", "between 0 and 100", "is not blank". */
export function expectText(expect: Expectation): string {
  if ("not_blank" in expect) return "is not blank";
  if ("between" in expect) {
    const [low, high] = expect.between;
    if (low !== null && high !== null) return `is between ${plain(low)} and ${plain(high)}`;
    return low !== null ? `is at least ${plain(low)}` : high !== null ? `is at most ${plain(high)}` : "is any value";
  }
  const tolerance = expect.tolerance_abs ? ` (within ${plain(expect.tolerance_abs)})` : expect.tolerance_rel ? ` (within ${plain(expect.tolerance_rel * 100)}%)` : "";
  return expect.equals === null ? "is blank" : `equals ${plain(expect.equals)}${tolerance}`;
}

/** "Region is West, Year from 2019 to 2020" */
export function filtersText(filters: TestFilter[]): string {
  if (!filters.length) return "No filters";
  return filters.map(filter => {
    const name = filter.measure ? filter.column : `${filter.table}[${filter.column}]`;
    // Filter values are keys (a year, an id), written as they are.
    if (filter.op === "between") {
      const [low, high] = filter.values;
      return `${name} from ${low === null || low === undefined ? "the start" : plain(low, false)} to ${high === null || high === undefined ? "the end" : plain(high, false)}`;
    }
    const values = filter.values.map(value => plain(value, false)).join(", ");
    return `${name} ${filter.op === "not_in" ? "is not" : "is"} ${values}`;
  }).join("; ");
}

export type TestDraft = {
  name: string; kind: "measure" | "expression"; measure?: { table: string; name: string }; expression?: string;
  expectKind: "equals" | "between" | "not_blank"; equals?: string; tolerance?: string; low?: string; high?: string;
  filters: { table: string; column: string; values: string }[];
};

const numberOrText = (raw: string): CellValue => {
  const text = raw.trim();
  if (!text) return null;
  const parsed = Number(text.replace(/,/g, ""));
  return Number.isFinite(parsed) && /^-?[\d,]*\.?\d+(e-?\d+)?$/i.test(text) ? parsed : text;
};

/** The body of `POST .../tests` for what the user typed, or the first problem in plain words. */
export function userTestBody(draft: TestDraft): { ok: true; body: Record<string, unknown> } | { ok: false; message: string } {
  const name = draft.name.trim();
  if (!name) return { ok: false, message: "Give the test a name." };
  const body: Record<string, unknown> = { name };
  if (draft.kind === "measure") {
    if (!draft.measure) return { ok: false, message: "Pick the measure to test." };
    body.measure = draft.measure;
  } else {
    const expression = draft.expression?.trim();
    if (!expression) return { ok: false, message: "Type the DAX expression to test." };
    if (/^\s*EVALUATE\b/i.test(expression)) return { ok: false, message: "Type an expression, not a query: leave out EVALUATE." };
    body.expression = expression;
  }
  if (draft.expectKind === "not_blank") body.expect = { not_blank: true };
  else if (draft.expectKind === "between") {
    const low = draft.low?.trim() ? Number(draft.low) : null, high = draft.high?.trim() ? Number(draft.high) : null;
    if ((low !== null && !Number.isFinite(low)) || (high !== null && !Number.isFinite(high))) return { ok: false, message: "The range ends must be numbers." };
    if (low === null && high === null) return { ok: false, message: "Give at least one end of the range." };
    if (low !== null && high !== null && low > high) return { ok: false, message: "The lowest value is above the highest." };
    body.expect = { between: [low, high] };
  } else {
    const equals = numberOrText(draft.equals ?? "");
    const tolerance = draft.tolerance?.trim() ? Number(draft.tolerance) : null;
    if (tolerance !== null && (!Number.isFinite(tolerance) || tolerance < 0)) return { ok: false, message: "The tolerance must be a positive number." };
    body.expect = { equals, ...(tolerance !== null ? { tolerance_abs: tolerance } : {}) };
  }
  const filters: TestFilter[] = [];
  for (const row of draft.filters) {
    if (!row.table || !row.column) continue;
    const values = row.values.split(/[;\n]/).map(part => part.trim()).filter(Boolean).map(numberOrText);
    if (!values.length) return { ok: false, message: `Give the values of ${row.table}[${row.column}], separated by semicolons.` };
    filters.push({ table: row.table, column: row.column, op: "in", values });
  }
  body.filters = filters;
  return { ok: true, body };
}

const ADD_KEYS = ["name", "measure", "expression", "filters", "expect", "format"] as const;
/** A suggested test as the add route takes it (the route's body is closed: ids, origins and dates stay out). */
export function suggestionBody(raw: unknown): Record<string, unknown> | undefined {
  if (!isRecord(raw)) return undefined;
  const body: Record<string, unknown> = {};
  for (const key of ADD_KEYS) if (raw[key] !== undefined && raw[key] !== null) body[key] = raw[key];
  return typeof body.name === "string" && body.expect ? body : undefined;
}

export type Suggestions = { tests: { raw: Record<string, unknown>; test: TestDefinition; text: string }[] };
export function readSuggestions(body: unknown): Suggestions {
  const source = isRecord(body) ? body : {};
  const raw = Array.isArray(source.tests) ? source.tests : [];
  const texts = strList(source.text);
  return {
    tests: raw.flatMap((item, index) => {
      const test = readTest(item);
      return test && isRecord(item) ? [{ raw: item, test, text: texts[index] ?? `${test.name} ${expectText(test.expect)}` }] : [];
    }),
  };
}

// --- picking a data point of a visual (explain and pin) ------------------------------------------------

type RenderedColumn = { kind: string; name: string; label: string; query_ref?: string | null; hidden?: boolean; role?: string };
export type RenderedVisual = { id: string; kind: string; title?: string; columns?: RenderedColumn[]; rows?: CellValue[][]; formatted_rows?: (string | null)[][] };
export type PointChoice = { key: string; label: string; values: Record<string, CellValue> };
export type MeasureChoice = { ref: string; label: string; index: number };

const refOf = (column: RenderedColumn) => column.query_ref || column.name;

/** The number columns a visual shows (what "this number" can be), in well order. */
export function measureChoices(visual: RenderedVisual): MeasureChoice[] {
  return (visual.columns ?? []).flatMap((column, index) => column.kind === "measure" && !column.hidden && column.role !== "Formatting" && column.role !== "Tooltips"
    ? [{ ref: refOf(column), label: column.label || column.name, index }] : []);
}

/** The data points of a chart or table: one per row, keyed by its category values (a card has none). */
export function pointChoices(visual: RenderedVisual, limit = 100): PointChoice[] {
  const columns = visual.columns ?? [];
  const categories = columns.map((column, index) => ({ column, index })).filter(({ column }) => column.kind === "column" && !column.hidden && column.role !== "SortBy" && column.role !== "Tooltips");
  if (!categories.length) return [];
  const seen = new Set<string>();
  const choices: PointChoice[] = [];
  for (const [rowIndex, row] of (visual.rows ?? []).entries()) {
    const values: Record<string, CellValue> = {};
    const words: string[] = [];
    for (const { column, index } of categories) {
      const value = row[index] ?? null;
      values[refOf(column)] = value;
      words.push(visual.formatted_rows?.[rowIndex]?.[index] ?? (value === null ? "(Blank)" : String(value)));
    }
    const key = JSON.stringify(values);
    if (seen.has(key)) continue;
    seen.add(key);
    choices.push({ key, label: words.join(" · "), values });
    if (choices.length >= limit) break;
  }
  return choices;
}

/** The point whose category values are those of a clicked data point (which may carry more columns than the categories). */
export function matchPoint(points: PointChoice[], values: Record<string, CellValue> | undefined): PointChoice | undefined {
  if (!values) return undefined;
  return points.find(point => Object.entries(point.values).every(([key, value]) => key in values && JSON.stringify(values[key]) === JSON.stringify(value)));
}

/** The value a visual shows for a point and a measure (the pin route takes it), and how it reads. */
export function pointValue(visual: RenderedVisual, point: PointChoice | undefined, measure: MeasureChoice | undefined): { value: CellValue; shown: string } | undefined {
  const rows = visual.rows ?? [];
  const at = measure?.index ?? measureChoices(visual)[0]?.index;
  if (at === undefined) return undefined;
  const rowIndex = point ? rows.findIndex(row => (visual.columns ?? []).every((column, index) => !(refOf(column) in point.values) || JSON.stringify(row[index] ?? null) === JSON.stringify(point.values[refOf(column)]))) : 0;
  if (rowIndex < 0 || !rows[rowIndex]) return undefined;
  const value = rows[rowIndex][at] ?? null;
  return { value, shown: visual.formatted_rows?.[rowIndex]?.[at] ?? (value === null ? "(Blank)" : String(value)) };
}

/** `POST .../tests/pin` */
export function pinBody(pageId: string, visual: RenderedVisual, point: PointChoice | undefined, measure: MeasureChoice | undefined, name: string): Record<string, unknown> | undefined {
  const found = pointValue(visual, point, measure);
  if (!found) return undefined;
  const choices = measureChoices(visual);
  return {
    page_id: pageId, visual_id: visual.id, point: point?.values ?? {}, value: found.value,
    ...(measure && choices.length > 1 ? { measure: measure.ref } : {}), ...(name.trim() ? { name: name.trim().slice(0, 200) } : {}),
  };
}

// --- explain this number ----------------------------------------------------------------------------------

export type ExplainStep = { name: string; kind: string; owner?: string; formula?: string; meaning?: string; value?: CellValue; formatted?: string; computed: boolean; usage?: string; depth: number; reason?: string };
export type ExplainFilter = { source: string; field: string; condition?: string; origin?: string; onMeasure: boolean; reaches: boolean | null; how?: string };
export type ExplainJoin = { tables: string[]; joins: string[]; why?: string };
export type ExplainColour = { target?: string; color?: string; name?: string; rule?: string; why?: string; measure?: string };
export type Explanation = {
  pageId?: string; visualId?: string; visual?: string; kind?: string; point: Record<string, CellValue>;
  measure?: string; formula?: string; meaning?: string; value?: CellValue; formatted?: string; computed: boolean; period?: string;
  steps: ExplainStep[]; filters: ExplainFilter[]; relationships: ExplainJoin[]; formatting?: ExplainColour; blank?: string;
  notes: string[]; engine?: string; text?: string;
};

export function readExplanation(body: unknown): Explanation | undefined {
  if (!isRecord(body)) return undefined;
  const steps = Array.isArray(body.steps) ? body.steps.flatMap((raw): ExplainStep[] => {
    if (!isRecord(raw) || !str(raw.name)) return [];
    return [{ name: String(raw.name), kind: str(raw.kind) ?? "", owner: str(raw.owner), formula: str(raw.formula), meaning: str(raw.meaning), value: cell(raw.value) ?? null,
      formatted: str(raw.formatted), computed: raw.computed !== false, usage: str(raw.usage), depth: Math.max(0, Math.min(6, num(raw.depth) ?? 0)), reason: str(raw.reason) }];
  }) : [];
  const filters = Array.isArray(body.filters) ? body.filters.flatMap((raw): ExplainFilter[] => isRecord(raw) && str(raw.field) ? [{
    source: str(raw.source) ?? "other", field: String(raw.field), condition: str(raw.condition), origin: str(raw.origin), onMeasure: raw.on_measure === true,
    reaches: raw.reaches === true ? true : raw.reaches === false ? false : null, how: str(raw.how),
  }] : []) : [];
  const relationships = Array.isArray(body.relationships) ? body.relationships.flatMap((raw): ExplainJoin[] => isRecord(raw) ? [{ tables: strList(raw.tables), joins: strList(raw.joins), why: str(raw.why) }] : []) : [];
  const colour = isRecord(body.formatting) ? { target: str(body.formatting.target), color: str(body.formatting.color), name: str(body.formatting.name), rule: str(body.formatting.rule), why: str(body.formatting.why), measure: str(body.formatting.measure) } : undefined;
  const point: Record<string, CellValue> = {};
  if (isRecord(body.point)) for (const [key, value] of Object.entries(body.point)) point[key] = cell(value) ?? null;
  return {
    pageId: str(body.page_id), visualId: str(body.visual_id), visual: str(body.visual), kind: str(body.kind), point, measure: str(body.measure), formula: str(body.formula),
    meaning: str(body.meaning), value: cell(body.value) ?? null, formatted: str(body.formatted), computed: body.computed !== false, period: str(body.period),
    steps, filters, relationships, formatting: colour, blank: str(body.blank), notes: strList(body.notes), engine: str(body.engine), text: str(body.text),
  };
}

const SOURCE_LABELS: Record<string, string> = {
  report: "Report filter", page: "Page filter", visual: "Visual filter", slicer: "Slicer", cross_filter: "Selection", drill: "Drill", bookmark: "Bookmark", point: "This point", other: "Other",
};
export const filterSourceLabel = (source: string) => SOURCE_LABELS[source] ?? source.replaceAll("_", " ");

/** A step's value as the side card shows it: the formatted value, or why it has none. */
export function stepValueText(step: ExplainStep): string {
  if (step.usage === "per_row" || step.usage === "shifted") return "no single value here";
  if (!step.computed) return "not computed";
  return step.formatted ?? (step.value === null || step.value === undefined ? "(Blank)" : String(step.value));
}

/** `POST .../imported-report/explain` */
export function explainBody(pageId: string, visualId: string, point: PointChoice | undefined, measure: MeasureChoice | undefined, canvas: Record<string, unknown> | undefined, focus: "value" | "colour" | "blank" = "value"): Record<string, unknown> {
  return {
    page_id: pageId, visual_id: visualId,
    ...(point || measure ? { point: { values: point?.values ?? {}, ...(measure ? { measure: measure.ref } : {}) } } : {}),
    ...(canvas ? { canvas } : {}), focus,
  };
}

// --- the transport ------------------------------------------------------------------------------------------

export type JudgementTransport = (method: "GET" | "POST" | "PATCH" | "DELETE", path: string, body?: unknown) => Promise<RawResponse>;

function problemCode(response: RawResponse): string | undefined {
  const body = response.body;
  if (!isRecord(body)) return undefined;
  return str(body.code) ?? (isRecord(body.detail) ? str(body.detail.code) : undefined);
}

function problemText(response: RawResponse): string {
  const body = response.body;
  if (isRecord(body)) {
    const inner = isRecord(body.detail) ? body.detail : body;
    const text = str(inner.message) ?? (typeof body.detail === "string" ? body.detail : undefined) ?? (Array.isArray(body.detail) ? "The request was not accepted." : undefined);
    if (text) return text;
  }
  return response.status === 404 ? "Studio could not find that." : `The local engine answered ${response.status}.`;
}

/** The judgement routes (docs/imported-report-editing-api.md, "Judgement"). Every method throws an Error with a plain message. */
export function createJudgementClient(transport: JudgementTransport) {
  const base = (workspaceId: string) => `/v1/workspaces/${encodeURIComponent(workspaceId)}/imported-report`;
  async function call(method: "GET" | "POST" | "PATCH" | "DELETE", path: string, body?: unknown): Promise<unknown> {
    let response: RawResponse;
    try { response = await transport(method, path, body); }
    catch (reason) { throw new Error(reason instanceof Error ? reason.message : "The local engine could not be reached."); }
    if (response.status >= 400) throw Object.assign(new Error(problemText(response)), { status: response.status, code: problemCode(response) });
    return response.body;
  }
  return {
    async review(workspaceId: string, pageId: string, revision?: string, withData = true): Promise<Review> {
      const query = revision ? `?revision=${encodeURIComponent(revision)}` : "";
      return readReview(await call("POST", `${base(workspaceId)}/pages/${encodeURIComponent(pageId)}/review${query}`, { with_data: withData }));
    },
    async tests(workspaceId: string): Promise<TestsState> { return readTestsState(await call("GET", `${base(workspaceId)}/tests`)); },
    async addTest(workspaceId: string, body: Record<string, unknown>): Promise<TestDefinition | undefined> { return readTest(await call("POST", `${base(workspaceId)}/tests`, body)); },
    async pin(workspaceId: string, body: Record<string, unknown>): Promise<{ test?: TestDefinition; result?: TestResult }> {
      const answer = await call("POST", `${base(workspaceId)}/tests/pin`, body);
      return isRecord(answer) ? { test: readTest(answer.test), result: readResult(answer.result) } : {};
    },
    async run(workspaceId: string, engine: "local" | "desktop" | "auto" = "auto"): Promise<TestRun | undefined> { return readTestRun(await call("POST", `${base(workspaceId)}/tests/run`, { engine })); },
    async suggest(workspaceId: string, pageIds: string[] = []): Promise<Suggestions> {
      const query = pageIds.length ? `?${pageIds.map(id => `page=${encodeURIComponent(id)}`).join("&")}` : "";
      return readSuggestions(await call("GET", `${base(workspaceId)}/tests/suggestions${query}`));
    },
    async updateTest(workspaceId: string, testId: string, body: Record<string, unknown>): Promise<TestDefinition | undefined> { return readTest(await call("PATCH", `${base(workspaceId)}/tests/${encodeURIComponent(testId)}`, body)); },
    async deleteTest(workspaceId: string, testId: string): Promise<boolean> {
      const answer = await call("DELETE", `${base(workspaceId)}/tests/${encodeURIComponent(testId)}`);
      return isRecord(answer) && answer.removed === true;
    },
    async explain(workspaceId: string, body: Record<string, unknown>): Promise<Explanation> {
      const found = readExplanation(await call("POST", `${base(workspaceId)}/explain`, body));
      if (!found) throw new Error("Studio could not explain this number.");
      return found;
    },
  };
}
export type JudgementClient = ReturnType<typeof createJudgementClient>;

// --- the planner's plan (chat `edit_plan`) -------------------------------------------------------------------

/**
 * A planned step. `outsideSupported`: a model change outside Microsoft's documented list for external tools (the server
 * decides, as the editor's gate does); `verifiedOn`: the installed Desktop version whose model probe covers it, null when
 * none does (Apply would then be refused with Desktop's steps).
 */
export type PlanStep = { summary: string; op: string; needsDesktop: boolean; confirm: boolean; source?: string; outsideSupported: boolean; verifiedOn: string | null };

// Every operation Power BI Desktop applies (report_edits.MODEL_OPERATIONS: the measure operations and the model family).
const DESKTOP_OPS = new Set([
  "create_measure", "update_measure", "rename_measure", "delete_measure", "create_calculated_column", "set_column_properties", "rename_column",
  "delete_column", "create_calculated_table", "set_table_properties", "rename_table", "delete_table", "mark_date_table", "create_relationship",
  "update_relationship", "delete_relationship", "create_hierarchy", "update_hierarchy", "delete_hierarchy", "create_field_parameter",
  "update_field_parameter", "create_calculation_group", "set_calculation_item", "delete_calculation_item", "update_calculation_group",
  "create_role", "update_role", "delete_role", "set_table_query", "set_named_expression", "delete_named_expression", "set_query_parameter",
  "refresh_data", "set_model_options",
]);
/** An operation Power BI Desktop has to apply (a model change): it needs Desktop and about a minute. */
export const needsDesktopOp = (op: string) => DESKTOP_OPS.has(op);

export function readPlanSteps(value: unknown): PlanStep[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((raw): PlanStep[] => isRecord(raw) && str(raw.summary) ? [{
    summary: String(raw.summary), op: str(raw.op) ?? "", needsDesktop: raw.needs_desktop === true, confirm: raw.confirm === true, source: str(raw.source),
    outsideSupported: raw.outside_supported === true, verifiedOn: str(raw.verified_on) ?? null,
  }] : []);
}

export type PlanView = { heading: string; applyLabel: string; needsDesktop: boolean; blocked: boolean; badges: { step: number; labels: string[] }[] };

/**
 * The words of a plan card: what it needs is decided by its steps (a plan without a Desktop step applies in seconds; the
 * interim card used to say "needs Power BI" for every plan). A step outside Microsoft's supported list says which Desktop
 * version it was tested with, or that no probe covers it on this Desktop (Apply would be refused).
 */
export function planView(steps: PlanStep[], needsDesktopFlag?: boolean): PlanView {
  const needsDesktop = steps.length ? steps.some(step => step.needsDesktop) : needsDesktopFlag === true;
  const blocked = steps.some(step => step.outsideSupported && !step.verifiedOn);
  return {
    heading: blocked ? "Planned change: a step is not verified on your Power BI Desktop, so Apply will be refused. Nothing changes."
      : needsDesktop ? "Planned change: needs Power BI. Nothing changes until you apply it." : "Planned change. Nothing changes until you apply it.",
    applyLabel: needsDesktop ? "Apply to report (needs Power BI, about 1 min)" : "Apply to report",
    needsDesktop, blocked,
    badges: steps.map((step, index) => ({
      step: index + 1,
      labels: [
        ...(step.needsDesktop ? ["Needs Power BI"] : []),
        ...(step.outsideSupported ? [step.verifiedOn
          ? `Outside Microsoft's supported list; tested with Desktop ${step.verifiedOn}`
          : "Outside Microsoft's supported list; not verified on your Desktop (do it in Power BI Desktop)"] : []),
        ...(step.confirm && !step.needsDesktop ? ["Asks first"] : []),
      ],
    })),
  };
}

/** The full formulas a plan would write (a formula step shows its whole DAX, never cut). */
export function planFormulas(ops: Operation[]): { title: string; expression: string; language: "DAX" | "M" }[] {
  const found: { title: string; expression: string; language: "DAX" | "M" }[] = [];
  for (const op of ops) {
    const name = str(op.name) ?? str(op.table) ?? str(op.visual) ?? "";
    const language = op.op === "set_table_query" || op.op === "set_named_expression" ? "M" : "DAX";
    if (typeof op.expression === "string" && op.expression.trim()) found.push({ title: `${op.op.replaceAll("_", " ")}${name ? `: ${name}` : ""}`, expression: op.expression.trim(), language });
    if (Array.isArray(op.items)) for (const item of op.items) if (isRecord(item) && typeof item.expression === "string") found.push({ title: `Calculation item ${str(item.name) ?? ""}`.trim(), expression: item.expression, language: "DAX" });
    if (Array.isArray(op.filters)) for (const filter of op.filters) if (isRecord(filter) && typeof filter.expression === "string") found.push({ title: `Role filter on ${str(filter.table) ?? "a table"}`, expression: filter.expression, language: "DAX" });
  }
  return found;
}
