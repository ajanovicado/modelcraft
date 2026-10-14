import type { ConversationContext } from "./UnifiedWorkspace";
import type { Message } from "./types";

export type ChatAttempt = { workspaceId: string; snapshotId?: string; boundaryId?: number; message: string; context: ConversationContext | null; baseHash: string; key: string };
type RecoveryStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
const storageKey = (workspaceId: string, snapshotId?: string) => `studio.chatRecovery.v1:${workspaceId}:${snapshotId ?? "no-data"}`;
const scopeId = (value: string) => /^[a-z0-9][a-z0-9_-]{0,199}$/i.test(value);
const validBoundary = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const onlyKeys = (value: Record<string, unknown>, allowed: string[]) => Object.keys(value).every(key => allowed.includes(key));

function validatedAttempt(value: unknown, workspaceId: string, snapshotId: string | undefined, boundaryId: number): ChatAttempt | undefined {
  if (!scopeId(workspaceId) || (snapshotId !== undefined && !scopeId(snapshotId)) || !validBoundary(boundaryId)
    || !object(value) || !onlyKeys(value, ["workspaceId", "snapshotId", "boundaryId", "message", "context", "baseHash", "key"])
    || value.workspaceId !== workspaceId || value.snapshotId !== snapshotId
    || (value.boundaryId !== undefined && !validBoundary(value.boundaryId)) || (value.boundaryId ?? 0) !== boundaryId
    || typeof value.message !== "string" || !value.message.trim() || value.message.length > 4000
    || typeof value.key !== "string" || !/^[A-Za-z0-9_-]{8,128}$/.test(value.key)
    || typeof value.baseHash !== "string" || !/^[a-f0-9]{64}$/i.test(value.baseHash)) return;
  let context: ConversationContext | null = null;
  if (value.context !== null) {
    if (!object(value.context) || !onlyKeys(value.context, ["label", "page_id", "visual_id", "table_id", "measure_id", "relationship_id"])
      || typeof value.context.label !== "string" || !value.context.label.trim() || value.context.label.length > 300) return;
    context = { label: value.context.label };
    for (const key of ["page_id", "visual_id", "table_id", "measure_id", "relationship_id"] as const) {
      const id = value.context[key];
      if (id !== undefined && (typeof id !== "string" || !id || id.length > 120)) return;
      if (typeof id === "string") context[key] = id;
    }
  }
  return { workspaceId, snapshotId, ...(value.boundaryId === undefined ? {} : { boundaryId: value.boundaryId as number }), message: value.message, key: value.key, baseHash: value.baseHash, context };
}

/** Server completion and New chat boundaries outrank a leftover local recovery receipt. */
export function readChatAttempt(storage: RecoveryStorage, workspaceId: string, snapshotId?: string, boundaryId = 0, messages: readonly Message[] = []): ChatAttempt | undefined {
  if (!scopeId(workspaceId) || (snapshotId !== undefined && !scopeId(snapshotId)) || !validBoundary(boundaryId)) return;
  const key = storageKey(workspaceId, snapshotId);
  try {
    const raw = storage.getItem(key);
    if (raw === null) return;
    let attempt: ChatAttempt | undefined;
    try { if (raw.length <= 32_000) attempt = validatedAttempt(JSON.parse(raw), workspaceId, snapshotId, boundaryId); }
    catch { /* Malformed browser data never becomes a model request. */ }
    if (attempt && !messages.some(message => message.request_id === attempt.key && message.status === "completed")) return attempt;
    storage.removeItem(key);
  } catch { return; }
}

export function saveChatAttempt(storage: RecoveryStorage, attempt: ChatAttempt): boolean {
  if (!validBoundary(attempt.boundaryId)) return false;
  const validated = validatedAttempt(attempt, attempt.workspaceId, attempt.snapshotId, attempt.boundaryId);
  if (!validated) return false;
  try {
    const serialized = JSON.stringify(validated);
    if (serialized.length > 32_000) return false;
    storage.setItem(storageKey(attempt.workspaceId, attempt.snapshotId), serialized);
    return true;
  }
  catch { return false; }
}

export function clearChatAttempt(storage: RecoveryStorage, workspaceId: string, snapshotId?: string): boolean {
  if (!scopeId(workspaceId) || (snapshotId !== undefined && !scopeId(snapshotId))) return false;
  try { storage.removeItem(storageKey(workspaceId, snapshotId)); return true; }
  catch { return false; }
}
