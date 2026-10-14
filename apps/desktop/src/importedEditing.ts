import { readFormatCatalog, type FormatCatalog } from "./importedFormat";
import {
  needsDesktopOp, readExplanation, readFindings, readHealthNotes, readPlanSteps, readResult, readSuggestions, receiptTestLines,
  type Explanation, type HealthNote, type PlanStep, type ReviewFinding, type Suggestions, type TestResult,
} from "./judgement";

// Editing an imported Power BI report: the HTTP contract (docs/imported-report-editing-api.md),
// the state machine around one edit request, and the small pure rules the editor UI shares.
// Nothing here touches React, the DOM or the network directly: the transport is injected so
// the whole request flow (idempotency keys, expected head, 202 polling, error mapping) is
// tested in node.

// --- vocabulary -------------------------------------------------------------------------------

export type Head = { id: string; hash: string; edited?: boolean; desktop_version?: string };
export type Operation = { op: string } & Record<string, unknown>;
export type DesktopWork = "ask" | "save";

export type Delivery = {
  state: "in_sync" | "edited" | "source_changed";
  source_path?: string;
  changes_not_in_source: number;
};
export type EditCapabilities = { edit_report: boolean; edit_model: boolean; reasons: string[]; thin_report: boolean };
export type DesktopStatus = { state: "closed" | "open" | "open_unsaved" | "dialog"; detail?: string };

export type ThemePalette = {
  name?: string;
  dataColors: string[];
  palette?: string[];
  foreground?: string;
  background?: string;
  fontFamily?: string;
};

export type EditRequest = {
  operations: Operation[];
  expected_head: string;
  idempotency_key: string;
  summary?: string;
  desktop_work?: DesktopWork;
  confirm_replace?: boolean;
  confirm_model?: boolean;
  /** Answers to a `needs_choice` question: the fill merged over one step (1-based). */
  answers?: { step: number; fill: Record<string, unknown> }[];
  /** Batch-wide yeses, such as `report_tests_broken` (a model change that breaks report tests). */
  confirmed?: string[];
};

export type ReceiptRevision = { id: string; parent?: string | null; author?: string; summary?: string; created?: string };
export type VisibleIn = { op: string; where: "studio" | "powerbi_only" | "covered_by_background" };
export type EditReceipt = {
  revision: ReceiptRevision;
  head: Head;
  summaries: string[];
  warnings: string[];
  adopted?: unknown;
  protected: { visual: string; what: string }[];
  visible_in: VisibleIn[];
  outcome: "updated" | "partial" | "failed";
  desktop?: "closed" | "reopened" | "open";
  /** What a whole-visual write overwrote ("TopN blue, BottomN red"). */
  replaced?: string[];
  /** The page findings the edit introduced or made worse (docs/imported-report-review.md), each with its fix. */
  review?: ReviewFinding[];
  /** What the report tests say after a model edit ("2 report tests still pass", the ones the user let it break). */
  testLines?: string[];
  /** Model health findings the edit introduced (`model_health.new`). */
  modelHealth?: HealthNote[];
};

export type HistoryAuthor = "import" | "studio" | "desktop" | "undo" | "restore";
export type HistoryRow = {
  id: string;
  parent: string | null;
  author: HistoryAuthor | string;
  summary: string;
  created: string;
  size?: number;
  available?: boolean;
  is_head: boolean;
  can_undo: boolean;
  conflict?: boolean;
  restores?: string | null;
  operations: { op: string; summary: string }[];
};

// --- transport and errors ---------------------------------------------------------------------

export type RawResponse = { status: number; body: unknown };
export type Transport = (method: "GET" | "POST", path: string, body?: unknown) => Promise<RawResponse>;

export type ProblemAction =
  | "redo" | "save_and_continue" | "cancel" | "retry" | "dismiss" | "replace" | "keep" | "wait";

export type EditProblem =
  | { kind: "head_moved"; message: string; expected?: string; actual?: string }
  | { kind: "desktop_unsaved"; message: string }
  | { kind: "desktop_dialog"; message: string; dialog?: string }
  | { kind: "busy"; message: string }
  | { kind: "needs_confirmation"; confirm: ConfirmKind; message: string; detail?: unknown; tests?: TestResult[] }
  | { kind: "needs_choice"; message: string; question: string; choices: { value: string; label: string; fill?: Record<string, unknown> }[] }
  | { kind: "rejected"; message: string; step?: string }
  | { kind: "unsupported"; message: string; capability?: string; steps: string[] }
  | { kind: "desktop_unavailable"; message: string }
  | { kind: "network"; message: string }
  | { kind: "still_running"; message: string; batchId: string }
  | { kind: "other"; message: string; status?: number };

/**
 * What a `needs_confirmation` asks. The edit layer's own questions are answered by a flag on the operation that asked
 * (`delete_page` / `bookmark_references` / `page_links` / `cleanup_references` set `cleanup_references`); `model_edit` by
 * `confirm_model`; `report_tests_broken` by the request's `confirmed`.
 */
export type ConfirmKind =
  | "model_edit" | "replace_conditional" | "restore_discard" | "delete_page" | "bookmark_references" | "page_links"
  | "cleanup_references" | "report_tests_broken" | "model_health_fix" | "unknown";

export type EditResult = { ok: true; receipt: EditReceipt } | { ok: false; problem: EditProblem };

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const str = (value: unknown): string | undefined => typeof value === "string" && value.trim() ? value : undefined;
const strList = (value: unknown): string[] => Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];

/** The error object of a response: `{code, message}` at the top, or wrapped in FastAPI's `detail`. */
export function errorBody(body: unknown): Record<string, unknown> {
  if (isRecord(body)) {
    if (typeof body.code === "string") return body;
    if (isRecord(body.detail)) return body.detail;
    if (typeof body.detail === "string") return { message: body.detail };
    return body;
  }
  return {};
}

/** Maps an error response (or a failed batch's `error`) to what the UI should do next. */
export function classifyEditProblem(status: number | undefined, body: unknown): EditProblem {
  const error = errorBody(body);
  const code = str(error.code) ?? "";
  const message = str(error.message) ?? (typeof body === "string" ? body : undefined);
  if (code === "head_moved" || (status === 409 && /head/i.test(code))) {
    return { kind: "head_moved", message: message ?? "The report changed while you were editing.", expected: str(error.expected), actual: str(error.actual) };
  }
  if (code === "desktop_unsaved") return { kind: "desktop_unsaved", message: message ?? "Power BI Desktop has unsaved changes to this report." };
  if (code === "desktop_dialog") return { kind: "desktop_dialog", message: message ?? "Power BI Desktop is showing a dialog.", dialog: str(error.dialog) };
  if (code === "edit_busy") return { kind: "busy", message: message ?? "Another change to this report is still being saved." };
  if (code === "desktop_busy") return { kind: "busy", message: message ?? "Power BI Desktop is busy with this report." };
  if (code === "needs_confirmation") {
    const kind = str(error.kind);
    const confirm: ConfirmKind = kind === "model_edit" || kind === "replace_conditional" || kind === "restore_discard"
      || kind === "cleanup_references" || kind === "report_tests_broken" || kind === "model_health_fix" ? kind
      : kind === "delete_page" || kind === "page_references" || kind === "remove_page_references" ? "delete_page"
      : kind === "remove_bookmark_references" ? "bookmark_references"
      : kind === "clear_page_links" ? "page_links" : "unknown";
    const tests = Array.isArray(error.tests) ? error.tests.map(readResult).filter((r): r is TestResult => r !== undefined) : undefined;
    return { kind: "needs_confirmation", confirm, message: message ?? "Please confirm this change.", detail: error.detail, ...(tests ? { tests } : {}) };
  }
  if (code === "needs_choice") {
    // Each choice is `{index, label, fill?}`: `fill` holds the operation fields that make this answer (for example `{field: "Sales"}`).
    const choices = Array.isArray(error.choices) ? error.choices.flatMap((c): { value: string; label: string; fill?: Record<string, unknown> }[] => {
      if (typeof c === "string") return [{ value: c, label: c }];
      if (!isRecord(c)) return [];
      const value = str(c.value) ?? str(c.id) ?? (typeof c.index === "number" ? String(c.index) : undefined);
      const label = str(c.label) ?? value;
      return value && label ? [{ value, label, ...(isRecord(c.fill) ? { fill: c.fill } : {}) }] : [];
    }) : [];
    return { kind: "needs_choice", message: message ?? "Choose how to continue.", question: str(error.question) ?? message ?? "Choose how to continue.", choices };
  }
  if (code === "edit_unsupported") {
    return { kind: "unsupported", message: str(error.reason) ?? message ?? "Studio cannot make this change yet.", capability: str(error.capability), steps: strList(error.desktop_steps) };
  }
  if (code === "edit_rejected" || status === 422) return { kind: "rejected", message: message ?? "This change cannot be applied.", step: str(error.step) };
  if (code === "desktop_unavailable" || status === 503) return { kind: "desktop_unavailable", message: message ?? "Power BI Desktop is not available." };
  if (status === 409) return { kind: "other", status, message: message ?? "The report is busy. Try again in a moment." };
  return { kind: "other", status, message: message ?? (status ? `The local engine answered ${status}.` : "The change could not be applied.") };
}

export type ProblemUi = { tone: "error" | "warn" | "info"; title: string; body: string; steps?: string[]; actions: ProblemAction[] };

/** Plain-English dialog copy for a problem; the first action is the default (safest) one. */
export function problemUi(problem: EditProblem, jobKind: "edit" | "undo" | "restore" = "edit"): ProblemUi {
  switch (problem.kind) {
    case "head_moved":
      // Only an edit can be re-sent on the new head; an Undo or a restore aimed at a head that moved could take back the wrong change.
      if (jobKind !== "edit") {
        return { tone: "warn", title: "The report changed first", body: `Another change was saved before this ${jobKind === "undo" ? "undo" : "restore"}, so nothing was ${jobKind === "undo" ? "undone" : "restored"}. The view was refreshed. Look at History and try again.`, actions: ["dismiss"] };
      }
      return { tone: "warn", title: "The report changed first", body: "Another change was saved before yours (in Studio or in Power BI). The view was refreshed and your change was not applied.", actions: ["redo", "dismiss"] };
    case "desktop_unsaved":
      return { tone: "warn", title: "Power BI Desktop has unsaved changes to this report", body: "To apply your change Studio has to close Power BI. Save those changes into the history and continue, or cancel and finish your work in Power BI first. Studio never saves them without asking.", actions: ["cancel", "save_and_continue"] };
    case "desktop_dialog":
      return { tone: "warn", title: problem.dialog ? `Power BI Desktop is showing “${problem.dialog}”` : "Power BI Desktop is showing a dialog", body: "Finish or close that dialog in Power BI Desktop, then try again. Nothing was changed.", actions: ["retry", "dismiss"] };
    case "busy":
      return { tone: "info", title: "Still saving the last change", body: "Another change to this report is still being saved. Try again in a moment.", actions: ["retry", "dismiss"] };
    case "needs_confirmation":
      if (problem.confirm === "replace_conditional") {
        return { tone: "warn", title: "Replace conditional formatting?", body: describeConfirmation(problem.detail) ?? problem.message, actions: ["keep", "replace"] };
      }
      if (problem.confirm === "delete_page") {
        return { tone: "warn", title: "Remove this page?", body: describeConfirmation(problem.detail) ?? problem.message, actions: ["cancel", "replace"] };
      }
      if (problem.confirm === "bookmark_references") {
        return { tone: "warn", title: "Buttons or navigators use this bookmark", body: describeConfirmation(problem.detail) ?? problem.message, actions: ["cancel", "replace"] };
      }
      if (problem.confirm === "page_links") {
        return { tone: "warn", title: "Buttons or tooltips use this page", body: describeConfirmation(problem.detail) ?? problem.message, actions: ["cancel", "replace"] };
      }
      if (problem.confirm === "cleanup_references") {
        return { tone: "warn", title: "Ungroup these visuals?", body: describeConfirmation(problem.detail) ?? problem.message, actions: ["cancel", "replace"] };
      }
      if (problem.confirm === "report_tests_broken") {
        // The server's message lists the tests ("This change would break 2 report tests: ..."); the dialog shows it as is.
        const lines = problem.tests?.map(test => `- ${test.message}`) ?? [];
        const body = describeConfirmation(problem.detail) ?? problem.message;
        return { tone: "warn", title: "This change would break report tests", body: lines.length && !lines.every(line => body.includes(line.slice(2))) ? `${body}\n${lines.join("\n")}` : body, actions: ["cancel", "replace"] };
      }
      return { tone: "warn", title: "Please confirm", body: describeConfirmation(problem.detail) ?? problem.message, actions: ["cancel", "replace"] };
    case "needs_choice":
      return { tone: "info", title: problem.question, body: "", actions: ["cancel"] };
    case "rejected":
      return { tone: "error", title: "This change was not applied", body: problem.message, actions: ["dismiss"] };
    case "unsupported":
      return { tone: "info", title: "Studio cannot do this yet", body: problem.message, steps: problem.steps.length ? problem.steps : undefined, actions: ["dismiss"] };
    case "desktop_unavailable":
      return { tone: "error", title: "Power BI Desktop is needed for this change", body: problem.message, actions: ["dismiss"] };
    case "network":
      return { tone: "error", title: "The local engine did not answer", body: `${problem.message} Your change may or may not have been saved. Trying again is safe: it cannot be applied twice.`, actions: ["retry", "dismiss"] };
    case "still_running":
      return { tone: "info", title: "Still working", body: problem.message, actions: ["wait", "dismiss"] };
    default:
      return { tone: "error", title: "The change could not be applied", body: problem.message, actions: ["dismiss"] };
  }
}

/** The server's `detail` for a confirmation: text, a list of protected formatting, or nested text. */
function describeConfirmation(detail: unknown): string | undefined {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const lines = detail.map(describeConfirmation).filter((line): line is string => Boolean(line));
    return lines.length ? lines.join("\n") : undefined;
  }
  if (isRecord(detail)) {
    const text = str(detail.message) ?? str(detail.text);
    if (text) return text;
    const what = str(detail.what);
    if (what) return `This replaces ${what}.`;
    if (Array.isArray(detail.protected)) return describeConfirmation(detail.protected);
  }
  return undefined;
}

// --- receipts ---------------------------------------------------------------------------------

export function readReceipt(body: unknown): EditReceipt | undefined {
  if (!isRecord(body) || !isRecord(body.revision) || !isRecord(body.head)) return undefined;
  const revisionId = str(body.revision.id);
  const headId = str(body.head.id);
  if (!revisionId || !headId) return undefined;
  const outcome = body.outcome === "partial" || body.outcome === "failed" ? body.outcome : "updated";
  const desktop = body.desktop === "closed" || body.desktop === "reopened" || body.desktop === "open" ? body.desktop : undefined;
  return {
    revision: { id: revisionId, parent: typeof body.revision.parent === "string" ? body.revision.parent : null,
      author: str(body.revision.author), summary: str(body.revision.summary), created: str(body.revision.created) },
    head: { id: headId, hash: str(body.head.hash) ?? "" },
    summaries: strList(body.summaries),
    warnings: strList(body.warnings),
    adopted: body.adopted ?? null,
    protected: Array.isArray(body.protected) ? body.protected.flatMap(item => isRecord(item) && str(item.what) ? [{ visual: str(item.visual) ?? "", what: String(item.what) }] : []) : [],
    visible_in: Array.isArray(body.visible_in) ? body.visible_in.flatMap((item): VisibleIn[] => {
      if (!isRecord(item) || typeof item.op !== "string") return [];
      const where = item.where === "powerbi_only" || item.where === "covered_by_background" ? item.where : "studio";
      return [{ op: item.op, where }];
    }) : [],
    outcome, desktop,
    ...(strList(body.replaced).length ? { replaced: strList(body.replaced) } : {}),
    ...(readFindings(body.review).length ? { review: readFindings(body.review) } : {}),
    ...(receiptTestLines(body.tests).length ? { testLines: receiptTestLines(body.tests) } : {}),
    ...(readHealthNotes(body.model_health).length ? { modelHealth: readHealthNotes(body.model_health) } : {}),
  };
}

const OP_NAMES: Record<string, string> = {
  set_position: "Position", arrange: "Order",
  set_visual_hidden: "Visibility", delete_visual: "Delete", duplicate_visual: "Duplicate", add_visual: "New visual",
  change_visual_type: "Visual type", set_fields: "Fields", set_sort: "Sort", rename_page: "Page name", add_page: "New page",
  duplicate_page: "Duplicate page", delete_page: "Delete page", move_page: "Page order", set_page_size: "Page size",
  set_page_hidden: "Page visibility",
  create_measure: "New measure", update_measure: "Measure", rename_measure: "Measure name", delete_measure: "Delete measure",
  set_conditional_format: "Conditional formatting", clear_conditional_format: "Conditional formatting", set_number_format: "Number format",
  set_table_layout: "Table layout", move_column: "Column order", set_field_display_name: "Column name",
  set_analytics_line: "Analytics line", remove_analytics_line: "Analytics line", add_bookmark: "Bookmark", update_bookmark: "Bookmark",
  rename_bookmark: "Bookmark name", move_bookmark: "Bookmark order", delete_bookmark: "Delete bookmark", group_bookmarks: "Bookmark group",
  ungroup_bookmarks: "Bookmark group", set_bookmark_visual_hidden: "Bookmark", add_button: "New button", set_button_action: "Button action",
  add_navigator: "New navigator", set_navigator: "Navigator", set_drillthrough: "Drillthrough page",
  set_tooltip_page: "Tooltip page", clear_page_type: "Page type", set_visual_tooltip: "Tooltip", set_interaction: "Interactions",
  reset_interactions: "Interactions", sync_slicer: "Synced slicer", unsync_slicer: "Synced slicer", place_on_mobile: "Phone layout",
  remove_from_mobile: "Phone layout", arrange_mobile_layout: "Phone layout", group_visuals: "Group", ungroup_visuals: "Ungroup", set_group: "Group",
  set_alt_text: "Alt text", set_tab_order: "Tab order", align_visuals: "Align", distribute_visuals: "Distribute",
  swap_visuals: "Swap", place_visual: "Position", add_textbox: "New text box", add_shape: "New shape", set_page_view: "Page view",
  set_small_multiples: "Small multiples", set_visual_calculation: "Visual calculation",
};
export const operationName = (op: string) => OP_NAMES[op] ?? op.replaceAll("_", " ");

export type ReceiptView = {
  tone: "ok" | "warn" | "error";
  headline: string;
  lines: string[];
  warnings: string[];
  chips: { label: string; tone: "info" | "warn" }[];
  desktopNote?: string;
  /** What the report tests say after a model change. */
  tests: string[];
  /** Model health findings the change introduced (plain sentences naming objects). */
  health: string[];
};

/** What a receipt says to the user: what changed, what only Power BI shows, what was kept. */
export function describeReceipt(receipt: Pick<EditReceipt, "summaries" | "warnings" | "protected" | "visible_in" | "outcome"> & { desktop?: EditReceipt["desktop"]; testLines?: string[]; modelHealth?: HealthNote[] }): ReceiptView {
  const lines = receipt.summaries;
  const chips: ReceiptView["chips"] = [];
  const seen = new Set<string>();
  for (const item of receipt.visible_in) {
    if (item.where === "studio") continue;
    const label = item.where === "powerbi_only"
      ? `${operationName(item.op)}: visible in Power BI only`
      : `${operationName(item.op)}: hidden behind the background`;
    if (!seen.has(label)) { seen.add(label); chips.push({ label, tone: "info" }); }
  }
  for (const item of receipt.protected) {
    const label = `Kept ${item.what}`;
    if (!seen.has(label)) { seen.add(label); chips.push({ label, tone: "info" }); }
  }
  const headline = receipt.outcome === "failed" ? "Nothing was changed"
    : receipt.outcome === "partial" ? "Saved, with a problem afterwards"
    : lines.length === 1 ? lines[0]
    : lines.length > 1 ? `Made ${lines.length} changes`
    : "Report updated";
  return {
    tone: receipt.outcome === "failed" ? "error" : receipt.outcome === "partial" || receipt.warnings.length ? "warn" : "ok",
    headline,
    lines: lines.length > 1 || receipt.outcome !== "updated" ? lines : [],
    // A broken test the user let through is also a warning (`text_broken`): shown once.
    warnings: receipt.warnings,
    chips,
    desktopNote: receipt.desktop === "reopened" ? "Power BI was closed for this change and reopened with it." : undefined,
    tests: (receipt.testLines ?? []).filter(line => !receipt.warnings.includes(line)),
    health: (receipt.modelHealth ?? []).map(note => note.message),
  };
}

// --- the client -------------------------------------------------------------------------------

export type Progress = { stage?: string; label?: string; state?: string };
export type ClientOptions = {
  sleep?: (ms: number) => Promise<void>;
  pollMs?: number;
  /** After this long without an answer the UI offers "keep waiting"; the batch itself keeps running. */
  pollTimeoutMs?: number;
  now?: () => number;
  onProgress?: (progress: Progress) => void;
  key?: () => string;
};

const defaultSleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));

export type Attempt = { key: string; request: Omit<EditRequest, "idempotency_key"> };

export function createEditingClient(transport: Transport, options: ClientOptions = {}) {
  const sleep = options.sleep ?? defaultSleep;
  const pollMs = options.pollMs ?? 1500;
  const pollTimeoutMs = options.pollTimeoutMs ?? 240_000;
  const now = options.now ?? Date.now;
  const base = (workspaceId: string) => `/v1/workspaces/${encodeURIComponent(workspaceId)}/imported-report`;

  async function send(method: "GET" | "POST", path: string, body?: unknown): Promise<RawResponse | EditProblem> {
    try { return await transport(method, path, body); }
    catch (reason) { return { kind: "network", message: reason instanceof Error ? reason.message : "The local engine could not be reached." }; }
  }
  const isProblem = (value: RawResponse | EditProblem): value is EditProblem => "kind" in value;

  async function poll(workspaceId: string, batchId: string): Promise<EditResult> {
    const started = now();
    for (;;) {
      const response = await send("GET", `${base(workspaceId)}/edits/${encodeURIComponent(batchId)}`);
      if (isProblem(response)) return { ok: false, problem: response };
      if (response.status >= 400) return { ok: false, problem: classifyEditProblem(response.status, response.body) };
      const body = isRecord(response.body) ? response.body : {};
      options.onProgress?.({ stage: str(body.stage), label: str(body.label), state: str(body.state) });
      if (body.state === "done") {
        const receipt = readReceipt(body.receipt);
        return receipt ? { ok: true, receipt } : { ok: false, problem: { kind: "other", message: "The change finished, but the engine returned no receipt. Check History." } };
      }
      if (body.state === "failed") return { ok: false, problem: classifyEditProblem(undefined, body.error ?? body) };
      if (now() - started > pollTimeoutMs) {
        return { ok: false, problem: { kind: "still_running", batchId, message: "This change is taking longer than usual. It keeps running in the background; you can wait for it here." } };
      }
      await sleep(pollMs);
    }
  }

  async function finish(workspaceId: string, response: RawResponse | EditProblem): Promise<EditResult> {
    if (isProblem(response)) return { ok: false, problem: response };
    if (response.status === 202 && isRecord(response.body) && typeof response.body.batch_id === "string") {
      options.onProgress?.({ state: str(response.body.state) });
      return poll(workspaceId, response.body.batch_id);
    }
    if (response.status >= 400) return { ok: false, problem: classifyEditProblem(response.status, response.body) };
    const receipt = readReceipt(response.body);
    return receipt ? { ok: true, receipt } : { ok: false, problem: { kind: "other", message: "The engine answered without a receipt. Check History before trying again." } };
  }

  return {
    newAttempt(operations: Operation[], expectedHead: string, extra: Partial<Omit<EditRequest, "operations" | "expected_head" | "idempotency_key">> = {}): Attempt {
      return { key: (options.key ?? (() => crypto.randomUUID()))(), request: { operations, expected_head: expectedHead, ...extra } };
    },
    /** Same operations on a newer head: a new request, so a new key ("Redo my change"). */
    rebase(attempt: Attempt, expectedHead: string): Attempt {
      return { key: (options.key ?? (() => crypto.randomUUID()))(), request: { ...attempt.request, expected_head: expectedHead } };
    },
    /** The same request with an extra flag and the SAME key: the server records one outcome per key. */
    withOptions(attempt: Attempt, extra: Partial<EditRequest>, sameKey: boolean): Attempt {
      return { key: sameKey ? attempt.key : (options.key ?? (() => crypto.randomUUID()))(), request: { ...attempt.request, ...extra } };
    },
    async edit(workspaceId: string, attempt: Attempt): Promise<EditResult> {
      return finish(workspaceId, await send("POST", `${base(workspaceId)}/edits`, { ...attempt.request, idempotency_key: attempt.key }));
    },
    async undo(workspaceId: string, attempt: { key: string; expected_head: string; desktop_work?: DesktopWork }): Promise<EditResult> {
      return finish(workspaceId, await send("POST", `${base(workspaceId)}/undo`, { expected_head: attempt.expected_head, idempotency_key: attempt.key, ...(attempt.desktop_work ? { desktop_work: attempt.desktop_work } : {}) }));
    },
    async restore(workspaceId: string, attempt: { key: string; revision_id: string; expected_head: string; confirmed_discard: boolean; desktop_work?: DesktopWork }): Promise<EditResult> {
      const { key, ...rest } = attempt;
      return finish(workspaceId, await send("POST", `${base(workspaceId)}/restore`, { ...rest, idempotency_key: key }));
    },
    /** Keeps waiting for a batch the server is still running (after `still_running`). */
    wait: poll,
    async history(workspaceId: string): Promise<HistoryRow[]> {
      const response = await send("GET", `${base(workspaceId)}/history`);
      if (isProblem(response)) throw new Error(response.message);
      if (response.status >= 400) throw new Error(classifyEditProblem(response.status, response.body).message);
      return readHistory(response.body);
    },
    async discardPreview(workspaceId: string, revisionId: string): Promise<HistoryRow[]> {
      const response = await send("GET", `${base(workspaceId)}/history/${encodeURIComponent(revisionId)}/discard-preview`);
      if (isProblem(response)) throw new Error(response.message);
      if (response.status >= 400) throw new Error(classifyEditProblem(response.status, response.body).message);
      return readHistory(isRecord(response.body) ? response.body.would_discard : []);
    },
    async formatCatalog(workspaceId: string, visualType: string): Promise<FormatCatalog> {
      const response = await send("GET", `${base(workspaceId)}/format-catalog?visual_type=${encodeURIComponent(visualType)}`);
      if (isProblem(response)) throw new Error(response.message);
      if (response.status >= 400) throw new Error(classifyEditProblem(response.status, response.body).message);
      return readFormatCatalog(response.body, visualType);
    },
    async saveCopy(workspaceId: string, destination: string, key: string = (options.key ?? (() => crypto.randomUUID()))()): Promise<DeliveryResult> {
      return deliver(await send("POST", `${base(workspaceId)}/save-copy`, { destination, idempotency_key: key }));
    },
    async replaceOriginal(workspaceId: string, key: string = (options.key ?? (() => crypto.randomUUID()))()): Promise<DeliveryResult> {
      return deliver(await send("POST", `${base(workspaceId)}/replace-original`, { idempotency_key: key, confirm: true }));
    },
    /** Answers to chat questions (`edit_choices`, `edit_confirm`). The reply is a receipt or a plain answer. */
    async answerChoice(workspaceId: string, pendingId: string, index: number): Promise<RawResponse | EditProblem> {
      return send("POST", `/v1/workspaces/${encodeURIComponent(workspaceId)}/chat/edit-choice`, { pending_id: pendingId, index });
    },
    async answerConfirm(workspaceId: string, pendingId: string, accept: boolean): Promise<RawResponse | EditProblem> {
      return send("POST", `/v1/workspaces/${encodeURIComponent(workspaceId)}/chat/edit-confirm`, { pending_id: pendingId, accept });
    },
  };
}
export type EditingClient = ReturnType<typeof createEditingClient>;

export type DeliveryResult = { ok: true; path: string; hash?: string; backup?: string } | { ok: false; message: string };
function deliver(response: RawResponse | EditProblem): DeliveryResult {
  if ("kind" in response) return { ok: false, message: response.message };
  if (response.status >= 400) return { ok: false, message: classifyEditProblem(response.status, response.body).message };
  const body = isRecord(response.body) ? response.body : {};
  const path = str(body.path);
  return path ? { ok: true, path, hash: str(body.hash), backup: str(body.backup) } : { ok: false, message: "The engine did not say where the file was written." };
}

/** Turns the JSON answer of a chat edit-choice / edit-confirm POST into what the card shows. */
/**
 * The answer to a chat question (`edit-choice`, `edit-confirm`, JSON): the receipt, the reply text and `events`, the same
 * `edit_*` events the chat stream sends (a choice can lead to a confirmation, a plan, a receipt, findings, a new question).
 * Every event is read, so the conversation shows what the answer led to, not only what was chosen.
 */
export function readChatAnswer(response: RawResponse | EditProblem): { receipt?: EditReceipt; message?: string; problem?: EditProblem; events: ChatEditEvent[] } {
  if ("kind" in response) return { problem: response, events: [] };
  const body = isRecord(response.body) ? response.body : {};
  const events = readAnswerEvents(body.events);
  if (response.status >= 400) return { problem: classifyEditProblem(response.status, response.body), events, message: str(body.text) };
  const receipt = readReceipt(body.receipt) ?? readReceipt(body);
  return { receipt, message: str(body.message) ?? str(body.text), events };
}

/** `[{event, data}]` as chat events; progress is left out (the answer is already finished). */
export function readAnswerEvents(value: unknown): ChatEditEvent[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap(item => {
    if (!isRecord(item) || typeof item.event !== "string") return [];
    const event = parseChatEditEvent(item.event, item.data);
    return event && event.type !== "progress" ? [event] : [];
  });
}

// --- history ----------------------------------------------------------------------------------

export function readHistory(body: unknown): HistoryRow[] {
  const rows = Array.isArray(body) ? body : isRecord(body) && Array.isArray(body.revisions) ? body.revisions : [];
  return rows.flatMap((item): HistoryRow[] => {
    if (!isRecord(item) || typeof item.id !== "string") return [];
    return [{
      id: item.id, parent: typeof item.parent === "string" ? item.parent : null, author: str(item.author) ?? "studio",
      summary: str(item.summary) ?? "", created: str(item.created) ?? "", size: typeof item.size === "number" ? item.size : undefined,
      available: typeof item.available === "boolean" ? item.available : undefined,
      is_head: item.is_head === true, can_undo: item.can_undo === true, conflict: item.conflict === true,
      restores: typeof item.restores === "string" ? item.restores : null,
      operations: Array.isArray(item.operations) ? item.operations.flatMap(o => isRecord(o) && typeof o.op === "string" ? [{ op: o.op, summary: str(o.summary) ?? operationName(o.op) }] : []) : [],
    }];
  });
}

const AUTHORS: Record<string, { label: string; tone: string }> = {
  studio: { label: "Studio", tone: "studio" }, desktop: { label: "Power BI Desktop", tone: "desktop" },
  undo: { label: "Undo", tone: "undo" }, restore: { label: "Restore", tone: "restore" }, import: { label: "Import", tone: "import" },
};
export const authorBadge = (author: string) => AUTHORS[author] ?? { label: author, tone: "studio" };

export type HistoryRowView = HistoryRow & { badge: { label: string; tone: string }; canUndo: boolean; canRestore: boolean; keptOnDisk: boolean };

/** Undo only on the newest change (P21); older rows restore to "before this" instead. */
export function historyView(rows: HistoryRow[], headId: string): HistoryRowView[] {
  return rows.map(row => {
    const isHead = row.id === headId;
    const kept = row.available !== false;
    return { ...row, badge: authorBadge(row.author), canUndo: isHead && row.can_undo, canRestore: !isHead && kept, keptOnDisk: kept };
  });
}

/** Redo is a restore of the change an Undo took back: only while that Undo is still the head. */
export function redoTarget(rows: HistoryRow[], headId: string): string | undefined {
  const head = rows.find(row => row.id === headId);
  if (!head || head.author !== "undo" || !head.parent) return undefined;
  const target = rows.find(row => row.id === head.parent);
  return target && target.available !== false ? target.id : undefined;
}

export type RestorePrompt = { title: string; body: string; items: { author: string; summary: string; created: string }[]; confirmLabel: string; needsConfirmation: boolean };
/** `label` names the row the user clicked when the restore target is that row's parent ("Restore to before this"). */
export function restorePrompt(target: Pick<HistoryRow, "summary">, wouldDiscard: HistoryRow[], label?: string): RestorePrompt {
  const n = wouldDiscard.length;
  const named = label !== undefined ? label : target.summary;
  return {
    title: n ? `Go back to before “${named || "this change"}”?` : "Restore this version?",
    body: n
      ? `This leaves ${n} later change${n === 1 ? "" : "s"} out of the report. They stay in History, so you can restore them again.`
      : "Nothing later would be lost.",
    items: wouldDiscard.map(row => ({ author: authorBadge(row.author).label, summary: row.summary || "Change", created: row.created })),
    confirmLabel: n ? `Restore and leave out ${n} change${n === 1 ? "" : "s"}` : "Restore",
    needsConfirmation: n > 0,
  };
}

// --- colors -----------------------------------------------------------------------------------

export type SwatchColor = { id: number; hex: string; label: string };
const HEX = /^#?([0-9a-f]{6})$/i;
export function normalizeHex(value: string): string | undefined {
  const text = value.trim();
  const short = /^#?([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(text);
  if (short) return `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`.toUpperCase();
  const match = HEX.exec(text);
  return match ? `#${match[1].toUpperCase()}` : undefined;
}

/** ThemeDataColor ids Studio may write: 0 white, 1 black, 2..9 the theme's first eight data colors (measured on Desktop 2.157). */
export function themeSwatches(theme?: Pick<ThemePalette, "dataColors" | "palette">): SwatchColor[] {
  const data = theme?.palette && theme.palette.length >= 10 ? theme.palette.slice(2, 10) : theme?.dataColors ?? [];
  const colors = ["#FFFFFF", "#000000", ...Array.from({ length: 8 }, (_, i) => data[i] ?? data[i % Math.max(1, data.length)] ?? DEFAULT_DATA_COLORS[i])];
  return colors.map((hex, id) => ({ id, hex: normalizeHex(hex) ?? "#000000", label: id === 0 ? "White" : id === 1 ? "Black" : `Theme color ${id - 1}` }));
}

/** Power BI's shading of a theme color, half-up per channel (measured). */
export function shadeHex(hex: string, percent: number): string {
  const normalized = normalizeHex(hex) ?? "#000000";
  const channels = [1, 3, 5].map(i => parseInt(normalized.slice(i, i + 2), 16));
  const shaded = channels.map(c => {
    const value = percent < 0 ? c * (1 + percent) : c + (255 - c) * percent;
    return Math.max(0, Math.min(255, Math.floor(value + 0.5)));
  });
  return `#${shaded.map(c => c.toString(16).padStart(2, "0")).join("")}`.toUpperCase();
}
export const SHADES = [0.8, 0.6, 0.4, 0, -0.25, -0.5] as const;

export type ColorValue = string | { theme: number; percent: number };
/** What an op carries for a picked color: a hex string, or a theme reference limited to ids 0..9. */
export function themeColorValue(id: number, percent = 0): ColorValue {
  if (!Number.isInteger(id) || id < 0 || id > 9) throw new RangeError("Theme color ids 0 to 9 are the only ones Power BI resolves");
  return { theme: id, percent: Math.max(-1, Math.min(1, percent)) };
}
export const colorHex = (value: ColorValue | undefined, theme?: ThemePalette): string | undefined =>
  value === undefined ? undefined : typeof value === "string" ? normalizeHex(value) : shadeHex(themeSwatches(theme)[value.theme]?.hex ?? "#000000", value.percent);

export function pushRecentColor(recent: string[], color: string, limit = 8): string[] {
  const hex = normalizeHex(color);
  if (!hex) return recent;
  return [hex, ...recent.filter(item => item !== hex)].slice(0, limit);
}

// --- delivery ---------------------------------------------------------------------------------

export function deliveryStatus(delivery: Delivery | undefined): { text: string; tone: "ok" | "warn"; canSaveToFile: boolean; canSaveCopy: boolean } {
  if (!delivery || delivery.state === "in_sync") return { text: "Your file matches this report", tone: "ok", canSaveToFile: false, canSaveCopy: true };
  if (delivery.state === "source_changed") {
    return { text: "The original file changed after you opened it. Save a copy so nothing of yours is overwritten", tone: "warn", canSaveToFile: false, canSaveCopy: true };
  }
  const n = delivery.changes_not_in_source;
  return { text: `Edited in Studio · ${n === 1 ? "1 change is" : `${n} changes are`} not in your file`, tone: "warn", canSaveToFile: true, canSaveCopy: true };
}

export function defaultCopyName(sourcePath: string | undefined, reportName: string): string {
  const source = sourcePath?.replaceAll("\\", "/");
  const file = source ? source.slice(source.lastIndexOf("/") + 1) : "";
  const stem = (file || reportName).replace(/\.pbix$/i, "") || "Report";
  const folder = source && source.includes("/") ? source.slice(0, source.lastIndexOf("/") + 1) : "";
  return `${folder}${stem} (edited).pbix`;
}

// --- the Studio-side stage strip ----------------------------------------------------------------

export const STAGES = ["planning", "saving_desktop", "closing_desktop", "writing", "opening_desktop", "applying", "saving"] as const;
const STAGE_LABELS: Record<string, string> = {
  planning: "Planning the change", saving_desktop: "Saving your Desktop work", closing_desktop: "Closing Power BI",
  writing: "Writing the change", opening_desktop: "Opening Power BI", applying: "Applying it in Power BI", saving: "Saving",
};
export const stageLabel = (stage: string | undefined, label?: string) => label || (stage ? STAGE_LABELS[stage] : undefined) || "Working";
export const DESKTOP_WARNING = "Power BI Desktop will open briefly; don't use it until Studio closes it.";
/** Stages that involve Desktop windows appearing or disappearing (the warning shows for these). */
export const desktopStage = (stage: string | undefined) => stage === "saving_desktop" || stage === "closing_desktop" || stage === "opening_desktop" || stage === "applying";

// --- one busy state per report, across editor instances ----------------------------------------------------------------

/**
 * The Report and Model tabs each run their own editing session, so a Desktop cycle started in one would leave the other
 * editable. Every session publishes its busy state here, keyed by report, and every session reads the union.
 */
export function createBusyStore<T>() {
  const instances = new Map<string, Map<string, T>>();
  const snapshots = new Map<string, T | null>();
  const listeners = new Set<() => void>();
  return {
    set(workspaceId: string, instance: string, value: T | null) {
      const mine = instances.get(workspaceId) ?? new Map<string, T>();
      if (value === null) mine.delete(instance); else mine.set(instance, value);
      if (mine.size) instances.set(workspaceId, mine); else instances.delete(workspaceId);
      const next = mine.size ? [...mine.values()][0] : null;
      if ((snapshots.get(workspaceId) ?? null) === next) return;
      if (next === null) snapshots.delete(workspaceId); else snapshots.set(workspaceId, next);
      listeners.forEach(listener => listener());
    },
    /** A stable value between changes, as `useSyncExternalStore` needs. */
    get: (workspaceId: string): T | null => snapshots.get(workspaceId) ?? null,
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
  };
}

// --- pending model changes (P32, P33) -----------------------------------------------------------

export type WhatIf = { question: string; answer: string };
export type PendingChange = { id: string; op: Operation; title: string; dax?: string; whatIf?: WhatIf };

export function describePending(op: Operation): string {
  const name = String(op.name ?? "");
  switch (op.op) {
    case "create_measure": return `Add the measure ${name}${op.table ? ` to ${String(op.table)}` : ""}`;
    case "update_measure": return `Change ${[op.expression ? "the formula" : "", op.format_string ? "the number format" : "", op.description ? "the description" : "", op.folder ? "the folder" : ""].filter(Boolean).join(" and ") || "the settings"} of ${name}`;
    case "rename_measure": return `Rename ${name} to ${String(op.new_name ?? "")}`;
    case "delete_measure": return `Delete the measure ${name}`;
    default: return operationName(String(op.op));
  }
}
/** The formulas an operation list would write, each named by what it does, for the DAX the user is asked to approve. */
export function operationFormulas(ops: Operation[]): { title: string; expression: string }[] {
  return ops.flatMap(op => typeof op.expression === "string" && op.expression.trim() ? [{ title: describePending(op), expression: op.expression.trim() }] : []);
}
/** A change Power BI Desktop applies (a measure or any other model operation): it needs Desktop and the user's yes. */
export const isModelOperation = (op: Operation) => needsDesktopOp(op.op);

export function createPendingStore(makeId: () => string = () => crypto.randomUUID()) {
  const byWorkspace = new Map<string, PendingChange[]>();
  const listeners = new Set<() => void>();
  const EMPTY: PendingChange[] = [];
  const set = (workspaceId: string, next: PendingChange[]) => {
    if (next.length) byWorkspace.set(workspaceId, next); else byWorkspace.delete(workspaceId);
    listeners.forEach(listener => listener());
  };
  return {
    get: (workspaceId: string): PendingChange[] => byWorkspace.get(workspaceId) ?? EMPTY,
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    /** Later changes to the same measure fold into the earlier one, so the tray stays what Desktop will actually do. */
    add(workspaceId: string, op: Operation, extra: { dax?: string; whatIf?: WhatIf } = {}): PendingChange[] {
      const current = [...(byWorkspace.get(workspaceId) ?? [])];
      const name = String(op.name ?? "");
      const findIndex = (kinds: string[]) => current.findIndex(item => kinds.includes(item.op.op) && String(item.op.name) === name);
      if (op.op === "update_measure") {
        const at = findIndex(["create_measure", "update_measure"]);
        if (at >= 0) {
          const merged: Operation = { ...current[at].op, ...Object.fromEntries(Object.entries(op).filter(([key, value]) => key !== "op" && key !== "name" && value !== undefined)) };
          current[at] = { ...current[at], op: merged, title: describePending(merged), dax: extra.dax ?? (typeof merged.expression === "string" ? merged.expression : current[at].dax), whatIf: extra.whatIf ?? current[at].whatIf };
          set(workspaceId, current);
          return current;
        }
      }
      if (op.op === "delete_measure") {
        const created = current.findIndex(item => item.op.op === "create_measure" && String(item.op.name) === name);
        if (created >= 0) { current.splice(created, 1); set(workspaceId, current.filter(item => !(item.op.op === "update_measure" && String(item.op.name) === name))); return byWorkspace.get(workspaceId) ?? EMPTY; }
      }
      // Queueing the same new name again replaces the earlier one: two "create" entries with one name would make Apply refuse the whole batch.
      if (op.op === "create_measure") {
        const same = current.findIndex(item => item.op.op === "create_measure" && String(item.op.name).toLowerCase() === name.toLowerCase());
        if (same >= 0) current.splice(same, 1);
      }
      current.push({ id: makeId(), op, title: describePending(op), dax: extra.dax ?? (typeof op.expression === "string" ? op.expression : undefined), whatIf: extra.whatIf });
      set(workspaceId, current);
      return current;
    },
    remove(workspaceId: string, id: string) { set(workspaceId, (byWorkspace.get(workspaceId) ?? []).filter(item => item.id !== id)); },
    clear(workspaceId: string) { set(workspaceId, []); },
  };
}
export const pendingStore = createPendingStore();

// --- chat events --------------------------------------------------------------------------------

export type ChatEditEvent =
  | { type: "progress"; stage: string; label: string }
  | { type: "applied"; revisionId: string; parentId?: string; head: Head; ops: Operation[]; summaries: string[]; warnings: string[]; visibleIn: VisibleIn[]; canUndo: boolean; protectedItems: { visual: string; what: string }[]; review?: ReviewFinding[]; testLines?: string[]; modelHealth?: HealthNote[] }
  | { type: "choices"; question: string; choices: { index: number; label: string }[]; pendingId: string }
  | { type: "confirm"; kind: string; whatIf?: WhatIf; ops: Operation[]; detail: string; pendingId: string; steps?: PlanStep[]; tests?: TestResult[] }
  | { type: "plan"; steps: PlanStep[]; ops: Operation[]; detail: string; needsDesktop: boolean; leftOut: string[]; whatIf?: WhatIf; pendingId: string }
  | { type: "review"; revisionId?: string; findings: ReviewFinding[] }
  | { type: "explanation"; explanation: Explanation }
  | { type: "suggestions"; suggestions: Suggestions }
  | { type: "failed"; message: string; code?: string };

const asOps = (value: unknown): Operation[] => Array.isArray(value) ? value.filter((v): v is Operation => isRecord(v) && typeof v.op === "string") : [];
const readWhatIf = (value: unknown): WhatIf | undefined => isRecord(value) && str(value.question) && str(value.answer)
  ? { question: String(value.question), answer: String(value.answer) } : undefined;

export function parseChatEditEvent(name: string, payload: unknown): ChatEditEvent | undefined {
  if (!isRecord(payload)) return undefined;
  if (name === "edit_progress") {
    const stage = str(payload.stage) ?? "";
    return { type: "progress", stage, label: stageLabel(stage, str(payload.label)) };
  }
  if (name === "edit_applied") {
    const revisionId = str(payload.revision_id);
    const head = isRecord(payload.head) && str(payload.head.id) ? { id: String(payload.head.id), hash: str(payload.head.hash) ?? "" } : revisionId ? { id: revisionId, hash: "" } : undefined;
    if (!revisionId || !head) return undefined;
    return {
      type: "applied", revisionId, parentId: str(payload.parent_id), head, ops: asOps(payload.ops), summaries: strList(payload.summaries),
      warnings: strList(payload.warnings), canUndo: payload.can_undo !== false,
      visibleIn: Array.isArray(payload.visible_in) ? payload.visible_in.flatMap((item): VisibleIn[] => isRecord(item) && typeof item.op === "string" ? [{ op: item.op, where: item.where === "powerbi_only" || item.where === "covered_by_background" ? item.where : "studio" }] : []) : [],
      protectedItems: Array.isArray(payload.protected) ? payload.protected.flatMap(item => isRecord(item) && str(item.what) ? [{ visual: str(item.visual) ?? "", what: String(item.what) }] : []) : [],
      ...(isRecord(payload.receipt) && readFindings(payload.receipt.review).length ? { review: readFindings(payload.receipt.review) } : {}),
      ...(isRecord(payload.receipt) && receiptTestLines(payload.receipt.tests).length ? { testLines: receiptTestLines(payload.receipt.tests) } : {}),
      ...(isRecord(payload.receipt) && readHealthNotes(payload.receipt.model_health).length ? { modelHealth: readHealthNotes(payload.receipt.model_health) } : {}),
    };
  }
  if (name === "edit_plan") {
    const pendingId = str(payload.pending_id);
    const steps = readPlanSteps(payload.steps);
    if (!pendingId || !steps.length) return undefined;
    return {
      type: "plan", steps, ops: asOps(payload.ops), detail: str(payload.detail) ?? "", needsDesktop: steps.some(step => step.needsDesktop) || payload.needs_desktop === true,
      leftOut: strList(payload.left_out), whatIf: readWhatIf(payload.what_if), pendingId,
    };
  }
  if (name === "edit_review") {
    const findings = readFindings(payload.findings);
    return findings.length ? { type: "review", revisionId: str(payload.revision_id), findings } : undefined;
  }
  if (name === "explanation") {
    const explanation = readExplanation(payload);
    return explanation ? { type: "explanation", explanation } : undefined;
  }
  if (name === "test_suggestions") {
    const suggestions = readSuggestions(payload);
    return suggestions.tests.length ? { type: "suggestions", suggestions } : undefined;
  }
  if (name === "edit_choices") {
    const pendingId = str(payload.pending_id);
    const question = str(payload.question);
    if (!pendingId || !question || !Array.isArray(payload.choices)) return undefined;
    const choices = payload.choices.flatMap(c => isRecord(c) && typeof c.index === "number" && str(c.label) ? [{ index: c.index, label: String(c.label) }] : []);
    return choices.length ? { type: "choices", question, choices, pendingId } : undefined;
  }
  if (name === "edit_confirm") {
    const pendingId = str(payload.pending_id);
    if (!pendingId) return undefined;
    const steps = readPlanSteps(payload.steps);
    const tests = Array.isArray(payload.tests) ? payload.tests.map(readResult).filter((r): r is TestResult => r !== undefined) : [];
    return {
      type: "confirm", kind: str(payload.kind) ?? "model_edit", whatIf: readWhatIf(payload.what_if), ops: asOps(payload.ops), detail: str(payload.detail) ?? "", pendingId,
      ...(steps.length ? { steps } : {}), ...(tests.length ? { tests } : {}),
    };
  }
  if (name === "edit_failed") return { type: "failed", message: str(payload.message) ?? "The change could not be applied.", code: str(payload.code) };
  return undefined;
}

export type EditCard =
  | { id: string; type: "applied"; event: Extract<ChatEditEvent, { type: "applied" }>; undone?: boolean; fixed?: string[] }
  // `reply`: the text the server answered the question with (what the answer led to).
  | { id: string; type: "choices"; event: Extract<ChatEditEvent, { type: "choices" }>; answered?: string; reply?: string }
  | { id: string; type: "confirm"; event: Extract<ChatEditEvent, { type: "confirm" }>; answered?: "applied" | "cancelled" | string; reply?: string }
  | { id: string; type: "plan"; event: Extract<ChatEditEvent, { type: "plan" }>; answered?: "applied" | "cancelled" | string; reply?: string }
  | { id: string; type: "review"; event: Extract<ChatEditEvent, { type: "review" }>; fixed?: string[] }
  | { id: string; type: "explanation"; event: Extract<ChatEditEvent, { type: "explanation" }> }
  | { id: string; type: "suggestions"; event: Extract<ChatEditEvent, { type: "suggestions" }>; added?: string[] }
  | { id: string; type: "failed"; event: Extract<ChatEditEvent, { type: "failed" }> };

/**
 * Folds a chat event into the cards shown under the conversation. Progress is not a card. The planner's plan comes twice
 * (`edit_plan`, and an interim `edit_confirm` of kind "plan" with the same pending id): only the plan card is kept. The
 * findings of an edit (`edit_review`) join the receipt of the same revision when it has none of its own.
 */
export function addEditCard(cards: EditCard[], event: ChatEditEvent, id: string): EditCard[] {
  if (event.type === "progress") return cards;
  if (event.type === "confirm" && event.kind === "plan" && cards.some(card => card.type === "plan" && card.event.pendingId === event.pendingId)) return cards;
  if (event.type === "plan") {
    const without = cards.filter(card => !(card.type === "confirm" && card.event.kind === "plan" && card.event.pendingId === event.pendingId));
    return [...without, { id, type: "plan", event } as EditCard].slice(-40);
  }
  if (event.type === "review") {
    const at = cards.findIndex(card => card.type === "applied" && event.revisionId !== undefined && card.event.revisionId === event.revisionId);
    if (at >= 0) {
      const target = cards[at] as Extract<EditCard, { type: "applied" }>;
      if (target.event.review?.length) return cards;
      return cards.map((card, index) => index === at ? { ...target, event: { ...target.event, review: event.findings } } : card);
    }
  }
  const card = { id, type: event.type, event } as EditCard;
  return [...cards, card].slice(-40);
}

/**
 * The cards after a question was answered: the answered card says what was chosen and what the server replied, and every
 * event of the answer becomes a card as it would from the chat stream (a confirmation with Apply and Cancel, a plan, a
 * receipt, findings, a new question, a failure). A receipt that came without its `edit_applied` event still shows. A
 * refused answer leaves the question open and says why. `applied` says whether the report changed.
 */
export function foldChatAnswer(cards: EditCard[], answeredId: string, answered: string, reply: ReturnType<typeof readChatAnswer>, nextId: () => string): { cards: EditCard[]; applied: boolean } {
  let next = reply.problem ? cards
    : cards.map(card => card.id === answeredId && (card.type === "choices" || card.type === "confirm" || card.type === "plan") ? { ...card, answered, ...(reply.message ? { reply: reply.message } : {}) } as EditCard : card);
  for (const event of reply.events) next = addEditCard(next, event, nextId());
  const appliedEvent = reply.events.some(event => event.type === "applied");
  if (reply.receipt && !appliedEvent) next = addEditCard(next, receiptToApplied(reply.receipt), nextId());
  if (reply.problem && !reply.events.some(event => event.type === "failed")) next = addEditCard(next, { type: "failed", message: reply.problem.message }, nextId());
  return { cards: next, applied: appliedEvent || Boolean(reply.receipt) };
}

/** Undo appears on a receipt only while that receipt made the current head. */
export function receiptCanUndo(card: EditCard, currentHeadId: string | undefined): boolean {
  return card.type === "applied" && card.event.canUndo && !card.undone && card.event.head.id === currentHeadId;
}

/**
 * A measure change the chat asked the user to confirm can also wait with the others in "Pending Power BI changes", so
 * several can be applied in one Power BI cycle. Only a `model_edit` confirmation made purely of measure operations
 * qualifies; the local what-if answer travels with the first change so the tray shows it.
 */
export function queueableConfirm(card: EditCard): { ops: Operation[]; whatIf?: WhatIf } | undefined {
  if (card.type !== "confirm" || card.answered || card.event.kind !== "model_edit") return undefined;
  const ops = card.event.ops;
  if (!ops.length || !ops.every(isModelOperation)) return undefined;
  return { ops, whatIf: card.event.whatIf };
}

/**
 * The receipts of past chat turns, read back from the workspace's activity log so they are still there after a reload
 * (the server records each turn's `edit_*` events with the turn: `chat.action`, `{request_id, text, edit_events:
 * [{event, data}]}`). Only receipts come back. A question that was pending when the app closed is answered from server
 * memory for half an hour at most, so it is not shown again; a failure is already in the recorded reply text.
 */
export function editCardsFromEvents(events: { sequence: number; type: string; payload: Record<string, unknown> }[]): EditCard[] {
  const cards: EditCard[] = [];
  for (const entry of events) {
    if (entry.type !== "chat.action" || !Array.isArray(entry.payload.edit_events)) continue;
    entry.payload.edit_events.forEach((item, index) => {
      if (!isRecord(item) || item.event !== "edit_applied") return;
      const event = parseChatEditEvent("edit_applied", item.data);
      if (event?.type === "applied") cards.push({ id: `recorded-${entry.sequence}-${index}`, type: "applied", event });
    });
  }
  return cards.slice(-40);
}

/** The activity log is served 250 events at a time, oldest first: follow `after` until a page comes back short. */
export const EVENT_PAGE_SIZE = 250;
export async function loadAllEvents<T extends { sequence: number }>(fetchPage: (after: number) => Promise<T[]>, maxPages = 400): Promise<T[]> {
  const all: T[] = [];
  let after = 0;
  for (let page = 0; page < maxPages; page += 1) {
    const batch = await fetchPage(after);
    all.push(...batch);
    const last = batch.length ? batch[batch.length - 1].sequence : after;
    if (batch.length < EVENT_PAGE_SIZE || last <= after) break;
    after = last;
  }
  return all;
}

/** The sequence of the newest "New chat" in the log: receipts before it belong to a conversation that was cleared. */
export const lastChatClear = (events: { sequence: number; type: string }[]): number =>
  events.reduce((latest, event) => event.type === "chat.cleared" ? Math.max(latest, event.sequence) : latest, 0);

/**
 * The recorded receipts followed by the ones of this session. A receipt that is in both (the log caught up after the
 * turn) appears once, as the session's card, which knows whether it was undone.
 */
export function mergeEditCards(recorded: EditCard[], session: EditCard[]): EditCard[] {
  const sessionRevisions = new Set(session.flatMap(card => card.type === "applied" ? [card.event.revisionId] : []));
  const kept = recorded.filter(card => !(card.type === "applied" && sessionRevisions.has(card.event.revisionId)));
  return [...kept, ...session].slice(-40);
}

// --- driving one job through the problems a request can meet -----------------------------------------------------------

export type Job =
  | { kind: "edit"; key: string; operations: Operation[]; options: Partial<Omit<EditRequest, "operations" | "expected_head" | "idempotency_key">> }
  | { kind: "undo"; key: string; options: { desktop_work?: DesktopWork } }
  | { kind: "restore"; key: string; revisionId: string; confirmedDiscard: boolean; options: { desktop_work?: DesktopWork } };

export type Decision = { action: ProblemAction } | { choice: string };
export type DriveOutcome =
  | { status: "applied"; receipt: EditReceipt }
  | { status: "cancelled" }
  | { status: "failed"; problem: EditProblem };

export type DriveIo = {
  /** The head this client currently believes in. */
  head: () => string;
  /** Asks the user (dialog) what to do about a problem; terminal problems resolve with "dismiss". */
  decide: (problem: EditProblem, jobKind?: Job["kind"]) => Promise<Decision>;
  /** Reloads the report after `head_moved`, and returns the head id it now has. */
  refreshHead: (actual?: string) => Promise<string>;
  newKey?: () => string;
};

const MAX_ROUNDS = 8;

/** Puts a choice's answer into the operations: the only operation of the batch, else the first one. */
export function fillOperations(operations: Operation[], fill: Record<string, unknown>): Operation[] {
  return operations.map((op, index) => index === 0 ? { ...op, ...fill } : op);
}

// The operations that take a per-operation confirmation flag (report_edit_conditional.py). The request-level
// `confirm_replace` is not read by them, so an answered confirmation has to be written into the operations themselves.
const CONDITIONAL_OPS = new Set(["set_conditional_format", "set_number_format", "set_table_layout"]);

/**
 * The operations with the user's answer to a confirmation written in: replacing rule-driven formatting sets
 * `replace_conditional`, removing a page, bookmark, page type or group that other parts of the report use
 * `cleanup_references`. Only operations that declare the flag get it.
 */
export function confirmOperations(operations: Operation[], confirm: Extract<EditProblem, { kind: "needs_confirmation" }>["confirm"]): Operation[] {
  const flag = confirm === "replace_conditional" ? { ops: CONDITIONAL_OPS, name: "replace_conditional" }
    : confirm === "delete_page" ? { ops: new Set(["delete_page"]), name: "cleanup_references" }
    : confirm === "bookmark_references" ? { ops: new Set(["delete_bookmark"]), name: "cleanup_references" }
    : confirm === "page_links" ? { ops: new Set(["clear_page_type"]), name: "cleanup_references" }
    : confirm === "cleanup_references" ? { ops: new Set(["ungroup_visuals"]), name: "cleanup_references" }
    : undefined;
  if (!flag) return operations;
  return operations.map(op => flag.ops.has(op.op) ? { ...op, [flag.name]: true } : op);
}

async function sendJob(client: EditingClient, workspaceId: string, job: Job, head: string): Promise<EditResult> {
  if (job.kind === "edit") return client.edit(workspaceId, { key: job.key, request: { operations: job.operations, expected_head: head, ...job.options } });
  if (job.kind === "undo") return client.undo(workspaceId, { key: job.key, expected_head: head, desktop_work: job.options.desktop_work });
  return client.restore(workspaceId, { key: job.key, revision_id: job.revisionId, expected_head: head, confirmed_discard: job.confirmedDiscard, desktop_work: job.options.desktop_work });
}

/**
 * Sends a job and resolves the conflicts the API defines (P21, P33): unsaved Desktop work asks first
 * and retries with the SAME key; a moved head refreshes and offers to redo (edits only: an Undo or a
 * restore aimed at a head that moved could undo the wrong change); a confirmation retries with a
 * fresh key and the confirming flag; a transient problem retries with the same key.
 */
export async function driveJob(client: EditingClient, workspaceId: string, initial: Job, io: DriveIo): Promise<DriveOutcome> {
  const newKey = io.newKey ?? (() => crypto.randomUUID());
  let job = initial;
  let result: EditResult = await sendJob(client, workspaceId, job, io.head());
  for (let round = 0; round < MAX_ROUNDS; round++) {
    if (result.ok) return { status: "applied", receipt: result.receipt };
    const problem = result.problem;
    if (problem.kind === "head_moved") await io.refreshHead(problem.actual);
    const decision = await io.decide(problem, job.kind);
    const action = "action" in decision ? decision.action : undefined;
    if ("choice" in decision && problem.kind === "needs_choice" && job.kind === "edit") {
      // A choice changes the operations through its `fill`; one without a fill cannot be written in, so nothing is sent.
      const picked = problem.choices.find(choice => choice.value === decision.choice);
      if (!picked?.fill) return { status: "cancelled" };
      job = { ...job, key: newKey(), operations: fillOperations(job.operations, picked.fill) };
      result = await sendJob(client, workspaceId, job, io.head());
      continue;
    }
    if (problem.kind === "still_running" && action === "wait") { result = await client.wait(workspaceId, problem.batchId); continue; }
    if (action === "retry" && (problem.kind === "busy" || problem.kind === "desktop_dialog" || problem.kind === "network")) {
      result = await sendJob(client, workspaceId, job, io.head());
      continue;
    }
    if (action === "save_and_continue" && problem.kind === "desktop_unsaved") {
      job = { ...job, options: { ...job.options, desktop_work: "save" } } as Job;
      result = await sendJob(client, workspaceId, job, io.head());
      continue;
    }
    if (action === "redo" && problem.kind === "head_moved" && job.kind === "edit") {
      job = { ...job, key: newKey() };
      result = await sendJob(client, workspaceId, job, io.head());
      continue;
    }
    if (action === "replace" && problem.kind === "needs_confirmation") {
      if (job.kind === "edit") {
        const flag: Partial<EditRequest> = problem.confirm === "model_edit" ? { confirm_model: true }
          : problem.confirm === "report_tests_broken" ? { confirmed: [...new Set([...(job.options.confirmed ?? []), "report_tests_broken"])] }
          : { confirm_replace: true };
        job = { ...job, key: newKey(), operations: confirmOperations(job.operations, problem.confirm), options: { ...job.options, ...flag } };
      }
      else if (job.kind === "restore") job = { ...job, key: newKey(), confirmedDiscard: true };
      else return { status: "failed", problem };
      result = await sendJob(client, workspaceId, job, io.head());
      continue;
    }
    if (action === "cancel" || action === "keep" || (problem.kind === "needs_choice")) return { status: "cancelled" };
    return { status: "failed", problem };
  }
  return { status: "failed", problem: { kind: "other", message: "The change kept running into conflicts. Nothing was changed; try again." } };
}

/** Heads only move forward: a report payload that was in flight before a receipt must not roll the client back. */
export function newerHead(current: Head | undefined, incoming: Head | undefined): Head | undefined {
  if (!incoming) return current;
  if (!current) return incoming;
  const order = (head: Head) => { const digits = /(\d+)$/.exec(head.id); return digits ? Number(digits[1]) : Number.NaN; };
  const a = order(current), b = order(incoming);
  if (Number.isNaN(a) || Number.isNaN(b)) return incoming;
  return b >= a ? incoming : current;
}

// --- measure editing ---------------------------------------------------------------------------------------------------------------

export type MeasureInfo = { TableName: string; Name: string; Expression: string; Description?: string; FormatString?: string };

/** Only what changed: an update that repeats the current formula would still cost a Power BI cycle. */
export function measureUpdateOp(original: MeasureInfo, draft: { expression: string; format: string; description: string }): Operation | undefined {
  const op: Operation = { op: "update_measure", name: original.Name };
  const expression = draft.expression.trim();
  if (expression && expression !== original.Expression.trim()) op.expression = expression;
  const format = draft.format.trim();
  if (format !== (original.FormatString ?? "").trim() && format) op.format_string = format;
  const description = draft.description.trim();
  if (description !== (original.Description ?? "").trim() && description) op.description = description;
  return Object.keys(op).length > 2 ? op : undefined;
}

export function createMeasureOp(draft: { table: string; name: string; expression: string; format: string; description?: string }): Operation {
  return {
    op: "create_measure", table: draft.table, name: draft.name.trim(), expression: draft.expression.trim(),
    ...(draft.format.trim() ? { format_string: draft.format.trim() } : {}), ...(draft.description?.trim() ? { description: draft.description.trim() } : {}),
  };
}

/** A plain-English problem with a measure name, or undefined when it is fine. */
export function measureNameProblem(name: string, existing: string[], current?: string): string | undefined {
  const trimmed = name.trim();
  if (!trimmed) return "Give the measure a name.";
  if (trimmed.length > 200) return "Measure names can have at most 200 characters.";
  if (existing.some(item => item.toLowerCase() === trimmed.toLowerCase() && item !== current)) return `A measure named “${trimmed}” already exists.`;
  return undefined;
}

// --- what a re-render has to redo ------------------------------------------------------------------------------------------------

// Operations that change how the report looks but never which rows a visual shows. After a batch made only of these the canvas
// re-reads formatting and runs no DAX (render contract, section 10). Fields, sort, type changes, deletes, adds and page
// structure change the query or the set of visuals, so those redraw the page.
const FORMATTING_ONLY = new Set([
  "set_position", "arrange", "set_visual_hidden", "set_page_size",
  // Round 3: layout, accessibility, bookmarks and navigation settings run no new query (a row whose data key moved is
  // refetched by the canvas anyway).
  "set_alt_text", "set_tab_order", "set_keep_layer_order", "set_button_action",
  "set_navigator", "place_on_mobile", "remove_from_mobile", "arrange_mobile_layout", "set_page_view", "align_visuals", "distribute_visuals",
  "swap_visuals", "place_visual", "add_bookmark", "update_bookmark", "rename_bookmark", "move_bookmark", "group_bookmarks", "ungroup_bookmarks",
  "delete_bookmark", "set_bookmark_visual_hidden", "set_visual_tooltip", "set_group",
]);
export const isFormattingOnly = (operations: Operation[]): boolean => operations.length > 0 && operations.every(op => FORMATTING_ONLY.has(op.op));
/** The visuals an edit names, so only they are re-read; undefined (all visuals) when any operation is not about one visual. */
export function touchedVisuals(operations: Operation[]): string[] | undefined {
  const ids = new Set<string>();
  for (const op of operations) {
    if (typeof op.visual !== "string") return undefined;
    ids.add(op.visual);
  }
  return ids.size ? [...ids] : undefined;
}

export const DEFAULT_DATA_COLORS = ["#118DFF", "#12239E", "#E66C37", "#6B007B", "#E044A7", "#744EC2", "#D9B300", "#D64550"];

/** A receipt from an answered question (a choice or a confirmation) as the "applied" card the conversation shows. */
export function receiptToApplied(receipt: EditReceipt): Extract<ChatEditEvent, { type: "applied" }> {
  return {
    type: "applied", revisionId: receipt.revision.id, parentId: receipt.revision.parent ?? undefined, head: receipt.head, ops: [],
    summaries: receipt.summaries, warnings: receipt.warnings, visibleIn: receipt.visible_in, canUndo: true, protectedItems: receipt.protected,
    ...(receipt.review?.length ? { review: receipt.review } : {}), ...(receipt.testLines?.length ? { testLines: receipt.testLines } : {}),
    ...(receipt.modelHealth?.length ? { modelHealth: receipt.modelHealth } : {}),
  };
}
