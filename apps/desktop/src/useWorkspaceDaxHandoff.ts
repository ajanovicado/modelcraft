import { useCallback, useMemo, useState } from "react";
import type { ChatDaxHandoff } from "./chatHistory";

type HandoffStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
type HandoffState = { handoff?: WorkspaceDaxHandoff; failed: boolean };
export type WorkspaceDaxHandoff = {
  source: ChatDaxHandoff;
  workspaceId: string;
  snapshotId: string;
  sourceView: "report" | "data" | "model";
};

const safeScopeId = (value: string) => /^[a-z0-9_-]{1,200}$/i.test(value);
export const workspaceDaxHandoffKey = (workspaceId: string, snapshotId: string) =>
  `studio.dax.handoff.v1:${workspaceId}:${snapshotId}`;

const record = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const onlyKeys = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).every(key => keys.includes(key));
const text = (value: unknown, limit: number) => typeof value === "string" && value.length <= limit && !value.includes("\0");

/** Bounded provenance for one active handoff, never conversation history or execution evidence. */
export function validateWorkspaceDaxHandoff(value: unknown, workspaceId: string, snapshotId: string): WorkspaceDaxHandoff | undefined {
  if (!safeScopeId(workspaceId) || !safeScopeId(snapshotId) || !record(value)
    || !onlyKeys(value, ["source", "workspaceId", "snapshotId", "sourceView"])
    || value.workspaceId !== workspaceId || value.snapshotId !== snapshotId
    || typeof value.sourceView !== "string" || !["report", "data", "model"].includes(value.sourceView) || !record(value.source)) return undefined;
  const source = value.source;
  if (!onlyKeys(source, ["expression", "requestText", "sourceMessageId", "sourceArchiveId", "sourceLabel"])
    || !text(source.expression, 4000) || !(source.expression as string).trim()
    || !text(source.requestText, 4000) || !text(source.sourceLabel, 80) || !(source.sourceLabel as string).trim()
    || (source.sourceMessageId !== undefined && (!Number.isSafeInteger(source.sourceMessageId) || Number(source.sourceMessageId) < 1))
    || (source.sourceArchiveId !== undefined && (!text(source.sourceArchiveId, 200) || !(source.sourceArchiveId as string).trim()))) return undefined;
  return {
    workspaceId, snapshotId, sourceView: value.sourceView as WorkspaceDaxHandoff["sourceView"],
    source: {
      expression: source.expression as string, requestText: source.requestText as string, sourceLabel: source.sourceLabel as string,
      ...(source.sourceMessageId !== undefined ? { sourceMessageId: source.sourceMessageId as number } : {}),
      ...(source.sourceArchiveId !== undefined ? { sourceArchiveId: source.sourceArchiveId as string } : {}),
    },
  };
}

export function readWorkspaceDaxHandoff(storage: HandoffStorage, workspaceId: string, snapshotId: string): HandoffState {
  if (!safeScopeId(workspaceId) || !safeScopeId(snapshotId)) return { failed: false };
  const key = workspaceDaxHandoffKey(workspaceId, snapshotId);
  try {
    const raw = storage.getItem(key);
    if (raw === null) return { failed: false };
    let handoff: WorkspaceDaxHandoff | undefined;
    try {
      if (raw.length <= 20_000) {
        const stored: unknown = JSON.parse(raw);
        if (record(stored) && stored.version === 1 && onlyKeys(stored, ["version", "handoff"])) handoff = validateWorkspaceDaxHandoff(stored.handoff, workspaceId, snapshotId);
      }
    } catch { /* Discard malformed provenance instead of attaching it to a new request. */ }
    if (!handoff) storage.removeItem(key);
    return { handoff, failed: false };
  } catch { return { failed: true }; }
}

/** Undefined detaches only this scope; opening another measure uses the same cleanup path. */
export function saveWorkspaceDaxHandoff(storage: HandoffStorage, workspaceId: string, snapshotId: string, value?: WorkspaceDaxHandoff): boolean {
  if (!safeScopeId(workspaceId) || !safeScopeId(snapshotId)) return false;
  try {
    const key = workspaceDaxHandoffKey(workspaceId, snapshotId);
    if (value === undefined) { storage.removeItem(key); return true; }
    const handoff = validateWorkspaceDaxHandoff(value, workspaceId, snapshotId);
    if (!handoff) { storage.removeItem(key); return false; }
    const serialized = JSON.stringify({ version: 1, handoff });
    if (serialized.length > 20_000) { storage.removeItem(key); return false; }
    storage.setItem(key, serialized);
    return true;
  } catch { return false; }
}

/** Keep scoped updates and detach tombstones while storage is unavailable, including tab switches. */
export function createWorkspaceDaxHandoffSession(getStorage: () => HandoffStorage) {
  const updates = new Map<string, HandoffState>();
  return {
    read(workspaceId: string, snapshotId: string): HandoffState {
      if (!safeScopeId(workspaceId) || !safeScopeId(snapshotId)) return { failed: false };
      const updated = updates.get(workspaceDaxHandoffKey(workspaceId, snapshotId));
      if (updated) return updated;
      try { return readWorkspaceDaxHandoff(getStorage(), workspaceId, snapshotId); }
      catch { return { failed: true }; }
    },
    set(workspaceId: string, snapshotId: string, value?: WorkspaceDaxHandoff): HandoffState | undefined {
      if (!safeScopeId(workspaceId) || !safeScopeId(snapshotId)) return;
      const handoff = value === undefined ? undefined : validateWorkspaceDaxHandoff(value, workspaceId, snapshotId);
      let persisted = false;
      try { persisted = saveWorkspaceDaxHandoff(getStorage(), workspaceId, snapshotId, value); }
      catch { /* Browser storage itself can be blocked before a storage method is called. */ }
      const updated = { handoff, failed: !persisted };
      updates.set(workspaceDaxHandoffKey(workspaceId, snapshotId), updated);
      return updated;
    },
  };
}

/** Scope before rendering or saving; storage failure keeps valid provenance in memory. */
export function useWorkspaceDaxHandoff(workspaceId?: string, snapshotId?: string) {
  const [session] = useState(() => createWorkspaceDaxHandoffSession(() => localStorage));
  const [latest, setLatest] = useState<{ workspaceId: string; snapshotId: string; state: HandoffState }>();
  const current = useMemo(() => {
    if (!workspaceId || !snapshotId) return { handoff: undefined, failed: false };
    if (latest?.workspaceId === workspaceId && latest.snapshotId === snapshotId) return latest.state;
    return session.read(workspaceId, snapshotId);
  }, [session, workspaceId, snapshotId, latest]);
  const setHandoff = useCallback((value?: WorkspaceDaxHandoff) => {
    if (!workspaceId || !snapshotId) return;
    const state = session.set(workspaceId, snapshotId, value);
    if (state) setLatest({ workspaceId, snapshotId, state });
  }, [session, workspaceId, snapshotId]);
  const clearHandoff = useCallback(() => setHandoff(undefined), [setHandoff]);
  return { handoff: current.handoff, setHandoff, clearHandoff, storageFailed: current.failed };
}
