import type { Message } from "./types";
import { conversationTitle, type ConversationHistoryItem } from "./conversationHistoryModel";

export type ArchivedConversation = {
  /** created_at of the last message in the segment — when that chat effectively ended. */
  endedAt: string;
  messages: Message[];
  /** Kept for reference after another data snapshot is imported. */
  previousData?: boolean;
};

const localDate = (value: string) => new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);

export type ChatDaxHandoff = {
  expression: string;
  requestText: string;
  sourceMessageId?: number;
  sourceLabel: string;
  sourceArchiveId?: string;
};

export const workspaceArchiveId = (conversation: ArchivedConversation) =>
  String(conversation.messages[0]?.id ?? conversation.endedAt);

export function buildWorkspaceHistoryItems(messages: Message[], archived: ArchivedConversation[]): ConversationHistoryItem[] {
  const item = (messages: Message[], sourceId?: string, previousData?: boolean): ConversationHistoryItem => ({
    id: sourceId === undefined ? "workspace:current" : `workspace:${sourceId}`,
    scope: "workspace",
    current: sourceId === undefined,
    sourceId,
    title: conversationTitle(messages.find(message => message.role === "user")?.content ?? "", "Workspace chat"),
    preview: messages.at(-1)?.content.replace(/\s+/g, " ").slice(0, 120) ?? "No messages yet",
    updatedAt: messages.at(-1)?.created_at ?? "",
    turnCount: messages.length,
    searchText: messages.map(message => message.content).join("\n"),
    note: previousData ? "Previous data" : undefined,
  });
  return [item(messages), ...archived.map(conversation => ({ ...item(conversation.messages, workspaceArchiveId(conversation), conversation.previousData), updatedAt: conversation.endedAt }))];
}

/** Associate a clicked answer with its own preceding request, including older responses. */
export function workspaceDaxHandoff(expression: string, message: Message, messages: Message[], sourceArchiveId?: string): ChatDaxHandoff {
  const index = messages.findIndex(item => item === message || (message.id !== undefined && item.id === message.id));
  const requestText = messages.slice(0, Math.max(index, 0)).reverse().find(item => item.role === "user")?.content ?? "";
  return { expression, requestText, sourceMessageId: message.id, sourceLabel: conversationTitle(requestText, "Workspace chat"), ...(sourceArchiveId ? { sourceArchiveId } : {}) };
}

/**
 * Split the stored conversation log into the active thread and read-only
 * archived segments. `boundaries` holds the id of the last message hidden by
 * each "new chat"; `since` keeps the existing snapshot-era scoping. Messages
 * without a stored id are optimistic local turns and always stay active.
 */
export function partitionMessages(
  messages: Message[],
  boundaries: number[],
  since: string,
): { active: Message[]; archived: ArchivedConversation[] } {
  // Match the engine's second-precision snapshot boundary. All earlier messages
  // remain readable in history, while none can enter the current conversation.
  const sinceTime = since ? Math.floor(localDate(since).getTime() / 1000) * 1000 : 0;
  const isCurrentData = (message: Message) => !sinceTime || !message.created_at || localDate(message.created_at).getTime() >= sinceTime;
  const orderedBoundaries = [...new Set(boundaries.filter(id => Number.isSafeInteger(id) && id > 0))].sort((a, b) => a - b);
  const last = orderedBoundaries.at(-1) ?? 0;
  const active = messages.filter(message => message.id === undefined || (message.id > last && isCurrentData(message)));
  const archived: ArchivedConversation[] = [];
  const archive = (segment: Message[], previousData: boolean) => {
    if (!segment.length) return;
    archived.push({ endedAt: segment.at(-1)!.created_at, messages: segment, ...(previousData ? { previousData: true } : {}) });
  };
  let previous = 0;
  for (const boundary of orderedBoundaries) {
    const segment = messages.filter(
      (message) => message.id !== undefined && message.id > previous && message.id <= boundary,
    );
    previous = boundary;
    archive(segment.filter(message => !isCurrentData(message)), true);
    archive(segment.filter(isCurrentData), false);
  }
  archive(messages.filter(message => message.id !== undefined && message.id > last && !isCurrentData(message)), true);
  return { active, archived };
}

/** Page the append-only log without losing early chats or growing model context. */
export async function loadAllMessages(fetchPage: (after: number) => Promise<Message[]>): Promise<Message[]> {
  const messages: Message[] = [];
  let after = 0;
  for (;;) {
    const page = await fetchPage(after);
    if (!page.length) break;
    const fresh = page.filter(message => message.id !== undefined && message.id > after);
    if (!fresh.length) throw new Error("Chat history could not be fully loaded. Restart the local engine and try again.");
    messages.push(...fresh);
    after = Math.max(...fresh.map(message => message.id!));
    if (page.length < 200) break;
  }
  return messages;
}
