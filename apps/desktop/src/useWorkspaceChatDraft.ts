import { useCallback, useMemo, useState } from "react";

type DraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export const chatDraftKey = (workspaceId: string) => `studio.chatDraft.${workspaceId}`;

export function readChatDraft(storage: DraftStorage, workspaceId: string) {
  try { return { value: (storage.getItem(chatDraftKey(workspaceId)) ?? "").slice(0, 4000), failed: false }; }
  catch { return { value: "", failed: true }; }
}

export function saveChatDraft(storage: DraftStorage, workspaceId: string, value: string): boolean {
  try {
    if (value) storage.setItem(chatDraftKey(workspaceId), value.slice(0, 4000));
    else storage.removeItem(chatDraftKey(workspaceId));
    return true;
  } catch { return false; }
}

/** Scope the value before rendering or saving, including newly created workspaces. */
export function useWorkspaceChatDraft(workspaceId?: string) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [storageFailures, setStorageFailures] = useState<Record<string, boolean>>({});
  const saved = useMemo(() => {
    if (!workspaceId) return { value: "", failed: false };
    try { return readChatDraft(localStorage, workspaceId); }
    catch { return { value: "", failed: true }; }
  }, [workspaceId]);
  const value = workspaceId ? drafts[workspaceId] ?? saved.value : "";
  const setValue = useCallback((next: string) => {
    if (!workspaceId) return;
    const limited = next.slice(0, 4000);
    setDrafts(current => ({ ...current, [workspaceId]: limited }));
    let persisted = false;
    try { persisted = saveChatDraft(localStorage, workspaceId, limited); }
    catch { /* Keep the in-memory draft when browser storage is disabled. */ }
    setStorageFailures(current => ({ ...current, [workspaceId]: !persisted }));
  }, [workspaceId]);
  const forgetWorkspace = useCallback((id: string) => {
    setDrafts(current => { const next = { ...current }; delete next[id]; return next; });
    setStorageFailures(current => { const next = { ...current }; delete next[id]; return next; });
  }, []);
  return { value, setValue, forgetWorkspace, storageFailed: workspaceId ? storageFailures[workspaceId] ?? saved.failed : false };
}
