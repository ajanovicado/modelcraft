type WorkspaceStorage = Pick<Storage, "length" | "key" | "getItem" | "removeItem">;

/** Remove only the deleted workspace's chat/formula caches after server deletion succeeds. */
export function clearWorkspaceChatStorage(storage: WorkspaceStorage, workspaceId: string): boolean {
  try {
    const exact = new Set([`studio.chatDraft.${workspaceId}`, `studio.dax.${workspaceId}`]);
    const prefixes = ["studio.chatRecovery.v1", "studio.dax.conversation.v1", "studio.dax.archive.v1", "studio.dax.composer.v1", "studio.dax.pending.v1", "studio.dax.handoff.v1"].map(prefix => `${prefix}:${workspaceId}:`);
    const keys: string[] = [];
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key && (exact.has(key) || prefixes.some(prefix => key.startsWith(prefix)))) keys.push(key);
    }
    for (const key of keys) storage.removeItem(key);
    if (storage.getItem("studio.lastWorkspace") === workspaceId) storage.removeItem("studio.lastWorkspace");
    return true;
  } catch { return false; }
}
