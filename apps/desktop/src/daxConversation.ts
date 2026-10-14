import type { StudioEvent } from "./types";
import type { DaxAssistInput } from "./api";
import { extractDaxFormula } from "./daxAssistance";

/** Browser-only source identity is stripped before sending the immutable API request. */
export type DaxRecoveryRequest = DaxAssistInput & { sourceIdentity?: string };
export type DaxRequestSource = { request: string; sourceLabel: string; sourceMessageId?: string | number; code: string };
export const daxSourceIdentity = (handoff?: DaxRequestSource): string => handoff
  ? JSON.stringify([handoff.request, handoff.sourceLabel, handoff.sourceMessageId ?? null, handoff.code]) : "";

function validDaxSourceIdentity(value: unknown): value is string {
  if (value === "") return true;
  if (typeof value !== "string" || value.length > 20_000) return false;
  try {
    const parts: unknown = JSON.parse(value);
    return Array.isArray(parts) && parts.length === 4
      && typeof parts[0] === "string" && parts[0].length <= 4000
      && typeof parts[1] === "string" && parts[1].length <= 200
      && (parts[2] === null || (typeof parts[2] === "string" && parts[2].length <= 200) || (Number.isSafeInteger(parts[2]) && Number(parts[2]) >= 1))
      && typeof parts[3] === "string" && parts[3].length <= 4000;
  } catch { return false; }
}

export type DaxTurn = {
  id?: string;
  goal: string;
  response: string;
  kind: "advice" | "explain" | "formula";
  calculationRequest?: string;
  formula?: { expression: string; name: string };
  created?: string;
  createdAt?: string;
  status?: "completed" | "interrupted" | "cancelled" | "failed";
  error?: string;
  chatBoundary?: number;
  editorRetained?: boolean;
};

/** Partition durable DAX evidence without allowing archived context into a new chat. */
export function daxConversationsFromEvents(events: StudioEvent[], snapshotId?: string): { current: DaxTurn[]; archives: DaxArchive[]; hasBoundary: boolean; boundaryId: number } {
  const requests = new Map<string, { goal: string; sequence: number; snapshotId?: string; chatBoundary: number }>();
  const turns: Array<{ turn: DaxTurn; sequence: number; snapshotId?: string }> = [];
  const boundaries: Array<{ sequence: number; through: number; snapshotId?: string; endedAt: string }> = [];
  for (const { event, sequence } of events.map((event, index) => ({ event, sequence: event.sequence ?? index + 1 })).sort((a, b) => a.sequence - b.sequence)) {
    const p = event.payload;
    const scope = typeof p.snapshot_id === "string" ? p.snapshot_id : undefined;
    if (event.type === "dax.chat.cleared") {
      boundaries.push({ sequence, through: Number.isSafeInteger(p.through_sequence) && Number(p.through_sequence) >= 0 ? Number(p.through_sequence) : Math.max(0, sequence - 1), snapshotId: scope, endedAt: event.created_at || "" });
      continue;
    }
    if (typeof p.request_id !== "string") continue;
    if (event.type === "dax.assistant_requested" && typeof p.goal === "string" && !requests.has(p.request_id)) requests.set(p.request_id, { goal: p.goal, sequence, snapshotId: scope, chatBoundary: Number.isSafeInteger(p.chat_boundary) && Number(p.chat_boundary) >= 0 ? Number(p.chat_boundary) : boundaries.filter(boundary => boundary.snapshotId === scope).at(-1)?.through ?? 0 });
    if (event.type !== "dax.assistant_answered" || !["completed", "interrupted", "cancelled", "failed"].includes(String(p.status)) || typeof p.response !== "string") continue;
    const request = requests.get(p.request_id);
    if (!request || request.snapshotId !== scope) continue;
    const kind = p.status !== "completed" || p.mode === "advice" ? "advice" : p.mode === "explain" ? "explain" : "formula";
    turns.push({ sequence: request.sequence, snapshotId: scope, turn: { id: p.request_id, goal: request.goal, response: p.response, kind, chatBoundary: request.chatBoundary, createdAt: event.created_at, status: p.status as DaxTurn["status"], error: typeof p.error === "string" ? p.error : undefined,
      calculationRequest: p.status === "completed" && typeof p.calculation_request === "string" ? p.calculation_request : undefined,
      formula: kind === "formula" ? extractDaxFormula(p.response) : undefined } });
  }
  const current: DaxTurn[] = [];
  const archives = boundaries.map(boundary => ({ id: `dax-chat-${boundary.sequence}`, endedAt: boundary.endedAt, snapshotId: boundary.snapshotId, previousData: boundary.snapshotId !== snapshotId, turns: [] as DaxTurn[] }));
  const previous = new Map<string, DaxArchive>();
  for (const entry of turns) {
    const index = boundaries.findIndex(boundary => boundary.snapshotId === entry.snapshotId && entry.sequence <= boundary.through);
    if (index >= 0) archives[index].turns.push(entry.turn);
    else if (entry.snapshotId === snapshotId) current.push(entry.turn);
    else {
      const key = entry.snapshotId || "no-data";
      const archive = previous.get(key) || { id: `dax-data-${key}`, snapshotId: entry.snapshotId, previousData: true, endedAt: entry.turn.createdAt || "", turns: [] };
      archive.turns.push(entry.turn); archive.endedAt = entry.turn.createdAt || archive.endedAt;
      previous.set(key, archive);
    }
  }
  const scopedBoundaries = boundaries.filter(boundary => boundary.snapshotId === snapshotId);
  return { current: mergeDaxTurns([], current), archives: [...archives, ...previous.values()].filter(archive => archive.turns.length).map(archive => ({ ...archive, turns: mergeDaxTurns([], archive.turns) })), hasBoundary: scopedBoundaries.length > 0, boundaryId: scopedBoundaries.at(-1)?.through ?? 0 };
}

/** The active DAX dialogue only; archived and previous-data turns never become model context. */
export const daxTurnsFromEvents = (events: StudioEvent[], snapshotId?: string): DaxTurn[] => daxConversationsFromEvents(events, snapshotId).current;

export function mergeDaxTurns(recorded: DaxTurn[], local: DaxTurn[]): DaxTurn[] {
  const merged = [...recorded];
  for (const turn of local) {
    const index = merged.findIndex(item => item.id && turn.id ? item.id === turn.id
      : item.goal === turn.goal && item.response === turn.response);
    if (index < 0) merged.push(turn);
    else if ((!merged[index].status || merged[index].status === "completed") && turn.status && turn.status !== "completed") {
      // A stale partial cache must never replace a completed durable receipt.
      merged[index] = { ...turn, ...merged[index], created: turn.created || merged[index].created };
    } else merged[index] = { ...merged[index], ...turn, id: turn.id || merged[index].id };
  }
  return merged;
}

/** Durable receipts own transcript fields; successful browser-only save notes stay local. */
export function reconcileDaxTurns(recorded: DaxTurn[], cached: DaxTurn[]): DaxTurn[] {
  const receipts = new Map(recorded.filter(turn => turn.id).map(turn => [turn.id, turn]));
  return mergeDaxTurns(recorded, cached).map(turn => {
    const receipt = turn.id ? receipts.get(turn.id) : undefined;
    return receipt ? { ...turn, ...receipt, created: !receipt.status || receipt.status === "completed" ? turn.created ?? receipt.created : undefined } : turn;
  });
}

export const daxConversationKey = (workspaceId: string, snapshotId?: string) =>
  `studio.dax.conversation.v1:${workspaceId}:${snapshotId || "no-data"}`;

/** A finished DAX chat, kept so a new chat starts with no prior context. */
export type DaxArchive = { id?: string; endedAt: string; turns: DaxTurn[]; snapshotId?: string; previousData?: boolean };

export const daxArchiveId = (archive: DaxArchive) => archive.id || `${archive.endedAt}:${archive.turns[0]?.id || archive.turns[0]?.goal || "chat"}`;

export const daxComposerKey = (workspaceId: string, snapshotId?: string) => `studio.dax.composer.v1:${workspaceId}:${snapshotId || "no-data"}`;
export function readDaxComposer(key: string): string {
  try { return (localStorage.getItem(key) || "").slice(0, 1000); }
  catch { return ""; }
}

export const daxPendingKey = (workspaceId: string, snapshotId?: string) => `studio.dax.pending.v1:${workspaceId}:${snapshotId || "no-data"}`;
/** Retain the original request envelope so a retry cannot borrow a different formula. */
export function readDaxPending(key: string): DaxRecoveryRequest | undefined {
  try {
    const item: unknown = JSON.parse(localStorage.getItem(key) || "null");
    if (!item || typeof item !== "object") return undefined;
    const value = item as DaxRecoveryRequest;
    if (typeof value.requestId !== "string" || !value.requestId || value.requestId.length > 200 || typeof value.goal !== "string" || !value.goal.trim() || value.goal.length > 1000
      || typeof value.expression !== "string" || value.expression.length > 4000 || !["auto", "generate", "explain", "fix"].includes(value.mode)
      || (value.chatBoundary !== undefined && (!Number.isSafeInteger(value.chatBoundary) || value.chatBoundary < 0))
      || (value.sourceIdentity !== undefined && !validDaxSourceIdentity(value.sourceIdentity))
      || (value.error !== undefined && (typeof value.error !== "string" || value.error.length > 600))
      || (value.discussion !== undefined && (typeof value.discussion !== "string" || value.discussion.length > 2000))) return undefined;
    return { requestId: value.requestId, goal: value.goal, expression: value.expression, mode: value.mode, error: value.error, discussion: value.discussion, ...(value.chatBoundary === undefined ? {} : { chatBoundary: value.chatBoundary }), ...(value.sourceIdentity === undefined ? {} : { sourceIdentity: value.sourceIdentity }) };
  } catch { return undefined; }
}

export const daxArchiveKey = (workspaceId: string, snapshotId?: string) =>
  `studio.dax.archive.v1:${workspaceId}:${snapshotId || "no-data"}`;

const MAX_ARCHIVES = 20;
const isDaxTurn = (turn: unknown): turn is DaxTurn => {
  if (!turn || typeof turn !== "object") return false;
  const item = turn as DaxTurn;
  return typeof item.goal === "string" && typeof item.response === "string" && ["advice", "explain", "formula"].includes(item.kind)
    && [item.id, item.calculationRequest, item.created, item.createdAt, item.error].every(value => value === undefined || typeof value === "string")
    && (item.status === undefined || ["completed", "interrupted", "cancelled", "failed"].includes(item.status))
    && (item.chatBoundary === undefined || (Number.isSafeInteger(item.chatBoundary) && item.chatBoundary >= 0))
    && (item.editorRetained === undefined || typeof item.editorRetained === "boolean")
    && (!item.formula || (typeof item.formula.expression === "string" && typeof item.formula.name === "string"));
};

export function readDaxArchives(key: string): DaxArchive[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) || "[]");
    if (!Array.isArray(value)) return [];
    return value.filter((item): item is DaxArchive => Boolean(item) && typeof item.endedAt === "string" && Array.isArray(item.turns)
      && (item.id === undefined || typeof item.id === "string")
      && (item.snapshotId === undefined || typeof item.snapshotId === "string")
      && (item.previousData === undefined || typeof item.previousData === "boolean"))
      .map(item => ({ ...item, turns: item.turns.filter(isDaxTurn) })).filter(item => item.turns.length)
      .slice(-MAX_ARCHIVES);
  } catch { return []; }
}

/** The newest chats first, bounded; oldest are dropped when the limit is reached. */
export function archiveDaxTurns(archives: DaxArchive[], turns: DaxTurn[], endedAt = new Date().toISOString()): DaxArchive[] {
  return turns.length ? [...archives, { endedAt, turns }].slice(-MAX_ARCHIVES) : archives;
}

// A cached copy of a turn may lack the engine request id, so both identities count.
const turnIdentities = (turn: DaxTurn) => [turn.id, `${turn.goal}\u0000${turn.response}`].filter(Boolean) as string[];

/** Turns that belong to no archived chat. Engine evidence replays every past turn, so
 * archived ones must be filtered out rather than merely cleared from the view. */
export function currentDaxTurns(turns: DaxTurn[], archives: DaxArchive[], chatBoundary?: number): DaxTurn[] {
  const archived = new Set(archives.flatMap(archive => archive.turns.flatMap(turnIdentities)));
  const archivedIds = new Set(archives.flatMap(archive => archive.turns.map(turn => turn.id).filter(Boolean)));
  return turns.filter(turn => (chatBoundary === undefined || (turn.chatBoundary ?? 0) === chatBoundary)
    && (turn.id ? !archivedIds.has(turn.id) : !turnIdentities(turn).some(identity => archived.has(identity))));
}

export function readDaxConversation(key: string): DaxTurn[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) || "[]");
    if (!Array.isArray(value)) return [];
    return value.filter(isDaxTurn);
  } catch { return []; }
}

/** A fresh conversation cannot revive an archived request from a stale browser recovery key. */
export function activeDaxPending(pending: DaxRecoveryRequest | undefined, turns: DaxTurn[], archives: DaxArchive[], chatBoundary = 0): DaxRecoveryRequest | undefined {
  if (!pending || (pending.chatBoundary ?? 0) !== chatBoundary) return undefined;
  if (archives.some(archive => archive.turns.some(turn => turn.id === pending.requestId))) return undefined;
  return turns.some(turn => turn.id === pending.requestId && (!turn.status || turn.status === "completed")) ? undefined : pending;
}

export type DaxStorage = Pick<Storage, "setItem" | "removeItem">;
export type DaxPersistenceFailure = "typed message" | "request recovery" | "chat copy";
export type DaxPersistedState = { storageKey: string; turns: DaxTurn[]; composerKey?: string; composer?: string; pendingKey?: string; pending?: DaxRecoveryRequest };

/** A transcript quota failure must not prevent the smaller composer and retry envelope from saving. */
export function saveDaxConversationState(storage: DaxStorage, state: DaxPersistedState): DaxPersistenceFailure[] {
  const failures: DaxPersistenceFailure[] = [];
  if (state.composerKey) {
    try { storage.setItem(state.composerKey, state.composer || ""); }
    catch { failures.push("typed message"); }
  }
  if (state.pendingKey) {
    try {
      if (state.pending) storage.setItem(state.pendingKey, JSON.stringify(state.pending));
      else storage.removeItem(state.pendingKey);
    } catch { failures.push("request recovery"); }
  }
  try { storage.setItem(state.storageKey, JSON.stringify(state.turns)); }
  catch { failures.push("chat copy"); }
  return failures;
}

/** Fit recent dialogue into the existing request budget, keeping the newest turn. */
export function daxDiscussion(turns: DaxTurn[], budget = 2000): string | undefined {
  if (budget < 3) return undefined;
  const complete = turns.filter(turn => !turn.status || turn.status === "completed");
  if (!complete.length) return undefined;
  const parts: string[] = [];
  let remaining = budget;
  for (const turn of [...complete].reverse()) {
    const answer = turn.response.split(/\n\n(?=References supplied)/)[0];
    const piece = `User: ${turn.goal}\nAssistant: ${answer}`;
    if (piece.length + 2 > remaining && parts.length) break;
    const kept = piece.slice(0, remaining - 2);
    parts.unshift(kept);
    remaining -= kept.length + 2;
    if (remaining < 80) break;
  }
  return parts.join("\n\n");
}

/** Carry the visible source request without combining unrelated Workspace chat history. */
export function daxHandoffDiscussion(request: string, sourceLabel: string, turns: DaxTurn[], budget = 2000): string | undefined {
  if (budget < 3) return undefined;
  const source = request.trim();
  if (!source) return daxDiscussion(turns, budget);
  const origin = `Earlier Workspace request (${sourceLabel.replace(/\s+/g, " ").slice(0, 120)}):\n${source.slice(0, 700)}`.slice(0, Math.max(0, budget));
  const recent = daxDiscussion(turns, Math.max(0, budget - origin.length - 2));
  return recent ? `${origin}\n\n${recent}` : origin;
}

/** An older answer may remain a candidate, but cannot replace a changed editor or source request. */
export function daxRequestCanApply(input: DaxRecoveryRequest, expression: string, chatBoundary: number, handoff?: DaxRequestSource): boolean {
  if (input.expression !== expression || (input.chatBoundary ?? 0) !== chatBoundary) return false;
  if (input.sourceIdentity !== undefined) return input.sourceIdentity === daxSourceIdentity(handoff);
  // An older envelope has no full provenance to prove it is still the same source.
  if (handoff) return false;
  return !input.discussion?.startsWith("Earlier Workspace request (");
}

/** The guarded planner's clarification reply is distinct from a formula evaluation error. */
export function isDaxClarification(turn: DaxTurn): boolean {
  return turn.kind === "advice" && (!turn.status || turn.status === "completed")
    && /No formula or report change was applied\.|No formula was (?:created|generated)\./i.test(turn.response);
}
