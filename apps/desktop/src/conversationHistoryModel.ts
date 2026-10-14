export type ConversationHistoryScope = "workspace" | "dax";

export type ConversationHistoryItem = {
  id: string;
  title: string;
  preview: string;
  updatedAt: string;
  turnCount: number;
  searchText?: string;
  note?: string;
  scope?: ConversationHistoryScope;
  current?: boolean;
  /** Native archive id; shared row ids are prefixed with their conversation scope. */
  sourceId?: string;
};

/** One-shot navigation, including repeated visits to the same saved chat or message. */
export type ConversationHistorySelection = {
  id?: string;
  requestId: number;
  messageId?: string | number;
};

export const conversationTimestamp = (value: string) => {
  const time = new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`).getTime();
  return Number.isFinite(time) ? time : 0;
};

export function filterConversationHistory(items: ConversationHistoryItem[], query: string, scope?: ConversationHistoryScope) {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return items.filter(item => {
    if (scope && item.scope !== scope) return false;
    const text = `${item.title} ${item.preview} ${item.searchText ?? ""}`.toLocaleLowerCase();
    return terms.every(term => text.includes(term));
  }).sort((a, b) => conversationTimestamp(b.updatedAt) - conversationTimestamp(a.updatedAt));
}

export const conversationTitle = (text: string, fallback = "Untitled chat") =>
  text.replace(/\s+/g, " ").trim().slice(0, 80) || fallback;
