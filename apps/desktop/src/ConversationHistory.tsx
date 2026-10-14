import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ChevronRight, History, Search } from "lucide-react";
import { SegmentedControl } from "./SegmentedControl";
import { conversationTimestamp as timestamp, filterConversationHistory, type ConversationHistoryItem, type ConversationHistoryScope } from "./conversationHistoryModel";
export { conversationTitle, filterConversationHistory } from "./conversationHistoryModel";
export type { ConversationHistoryItem, ConversationHistorySelection } from "./conversationHistoryModel";

const historyScopes = [{ value: "all", label: "All chats" }, { value: "workspace", label: "Workspace" }, { value: "dax", label: "DAX" }] as const;

/** The same compact, searchable history surface for workspace and DAX chats. */
export function ConversationHistory({ items, onSelect, onClose, label = "Chat history", hint = "Saved in this workspace. Open a chat to read it." }: {
  items: ConversationHistoryItem[];
  onSelect: (id: string) => void;
  onClose: () => void;
  label?: string;
  hint?: string;
}) {
  const [query, setQuery] = useState("");
  const [visibleCount, setVisibleCount] = useState(20);
  const [scope, setScope] = useState<ConversationHistoryScope>();
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const filtered = useMemo(() => filterConversationHistory(items, query, scope), [items, query, scope]);
  const mixedScopes = items.some(item => item.scope === "workspace") && items.some(item => item.scope === "dax");
  const containsCurrent = items.some(item => item.current);
  useLayoutEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented && panelRef.current?.contains(document.activeElement)) onClose();
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);
  return <section ref={panelRef} className="conversation-history-panel" aria-label={label}>
    <header><div><History size={16} aria-hidden="true" /><strong>{label}</strong></div>
      <button type="button" onClick={onClose} aria-label="Back to current chat"><ArrowLeft size={14} aria-hidden="true" />Back</button>
    </header>
    <p className="conversation-history-hint">{hint}</p>
    <label className="conversation-history-search"><Search size={15} aria-hidden="true" />
      <input ref={inputRef} value={query} onChange={event => { setQuery(event.target.value); setVisibleCount(20); }} placeholder="Search chats" aria-label="Search chats" type="search" maxLength={200} />
    </label>
    {mixedScopes && <SegmentedControl mode="filter" label="Filter chat history" className="conversation-history-filters" options={historyScopes} value={scope ?? "all"} onChange={value => { setScope(value === "all" ? undefined : value); setVisibleCount(20); }} />}
    <div className="conversation-history-results" role="status">{query || scope ? `${filtered.length} matching ${filtered.length === 1 ? "chat" : "chats"}` : `${items.length} ${containsCurrent ? "" : "saved "}${items.length === 1 ? "chat" : "chats"}`}</div>
    {!filtered.length ? <div className="conversation-history-empty"><strong>{query ? "No matching chats" : "No previous chats yet"}</strong><p>{query ? "Try another word from the conversation." : "Start a new chat when you’re ready. This conversation will stay here."}</p></div> : <ol>
      {filtered.slice(0, visibleCount).map(item => <li key={item.id}><button type="button" className="conversation-history-row" onClick={() => onSelect(item.id)}>
        <span>{(item.scope || item.current) && <span className="conversation-history-badges">{item.scope && <span>{item.scope === "dax" ? "DAX" : "Workspace"}</span>}{item.current && <span className="conversation-history-current">Current</span>}</span>}<strong>{item.title}</strong><span className="conversation-history-preview">{item.preview}</span><small>{timestamp(item.updatedAt) ? new Date(timestamp(item.updatedAt)).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : item.current ? "Current chat" : "Saved chat"} · {item.turnCount} {item.turnCount === 1 ? "message" : "messages"}{item.note ? ` · ${item.note}` : ""}</small></span>
        <ChevronRight size={15} aria-hidden="true" />
      </button></li>)}
    </ol>}
    {filtered.length > visibleCount && <button type="button" className="conversation-history-more" onClick={() => setVisibleCount(count => count + 20)}>Show more chats</button>}
  </section>;
}
