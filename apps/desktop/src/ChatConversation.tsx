import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { ArrowDown, ArrowRight, ArrowUp, Check, Copy, Database, History, MessageSquarePlus, Pencil, RotateCcw, ShieldCheck, Sparkles, Square, X } from "lucide-react";
import { RichText } from "./ProductUI";
import { daxRequestSummary } from "./daxAssistance";
import { ConversationHistory, conversationTitle, type ConversationHistoryItem, type ConversationHistorySelection } from "./ConversationHistory";
import { buildWorkspaceHistoryItems, workspaceArchiveId, type ArchivedConversation, type ChatDaxHandoff } from "./chatHistory";
export type { ChatDaxHandoff } from "./chatHistory";
import type { Draft, Message, Snapshot } from "./types";
import type { ConversationContext } from "./UnifiedWorkspace";

export type ChatOutcome = "stopped" | "interrupted" | undefined;

const localDate = (value: string) => new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);

/** "[superstore_orders_1_641f9576]" reads as "Orders" in drafts made before the planner did this. */
export function readableReferences(text: string, snapshot: Snapshot): string {
  const names = new Map<string, string>();
  for (const entry of snapshot.tables ?? []) {
    const table = entry?.table;
    if (!table?.id) continue;
    names.set(table.id, table.name);
    for (const column of table.columns ?? []) names.set(`${table.id}.${column.id}`, `${table.name}[${column.name}]`);
  }
  return text.replace(/\[([a-z][a-z0-9_.]*)\]/gi, (match, id: string) => names.get(id) ?? match).replace(/\s*\[\d+\]/g, "");
}
const messageTime = (value: string) => localDate(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** Real request activity, not a simulated chain-of-thought or progress percentage. */
export function ResponseActivity({ label = "Preparing a response" }: { label?: string }) {
  return <span className="response-activity" role="status" aria-live="polite">
    <span className="response-dots" aria-hidden="true"><span /><span /><span /></span>
    <span>{label}</span>
  </span>;
}

const ChatMessage = memo(function ChatMessage({ message, streaming = false, onEditMessage, retryMessage, onRetry, onUseDax, sourceRequest = "" }: {
  message: Message;
  streaming?: boolean;
  onEditMessage?: (content: string) => void;
  retryMessage?: string;
  onRetry?: (message: string) => void;
  onUseDax?: (handoff: ChatDaxHandoff) => void;
  sourceRequest?: string;
}) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const assistant = message.role === "assistant";
  const incomplete = assistant && message.status !== undefined && message.status !== "completed";
  const daxRequest = assistant ? undefined : daxRequestSummary(message.content);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };
  const interactive = Boolean(onEditMessage || onRetry);
  return <article className={`chat-message ${assistant ? "from-assistant" : "from-user"}`} data-message-id={message.id} tabIndex={-1} aria-label={assistant ? "Local AI response" : "Your message"}>
    {assistant && <div className="chat-message-author"><span className="qwen-mark" aria-hidden="true"><Sparkles size={17} /></span><strong>Local AI</strong>{streaming && <ResponseActivity label="Responding" />}</div>}
    <div className="chat-message-body">{daxRequest ? <><RichText text={daxRequest.request} /><details className="chat-dax-context"><summary>DAX lab · Included formula context</summary><RichText text={daxRequest.context} /></details></> : <RichText text={message.content} onUseDax={assistant && !streaming && !incomplete && onUseDax ? expression => onUseDax({ expression, requestText: sourceRequest, sourceMessageId: message.id, sourceLabel: conversationTitle(sourceRequest, "Workspace chat") }) : undefined} />}</div>
    {incomplete && <p className="chat-discussion-note" role="status">{message.status === "cancelled" ? "Response stopped before completion." : message.status === "failed" ? "Response failed before completion." : "Response interrupted before completion."}</p>}
    {!!message.citations.length && <div className="message-citations" aria-label="Referenced reviewed fields">{message.citations.map((citation) => <code key={citation}>{citation}</code>)}</div>}
    {!streaming && (interactive || message.created_at) && <div className="chat-message-tools">
      {assistant && <button type="button" data-feedback={copyState} onClick={() => void copy()} title="Copy response" aria-label={copyState === "copied" ? "Response copied" : "Copy response"}>{copyState === "copied" ? <Check size={14} /> : <Copy size={14} />}<span>{copyState === "copied" ? "Copied" : "Copy"}</span></button>}
      {assistant && retryMessage && onRetry && <button type="button" onClick={() => onRetry(retryMessage)} title={`Ask this again: ${retryMessage}`}><RotateCcw size={13} /><span>Try again</span></button>}
      {!assistant && onEditMessage && <button type="button" onClick={() => onEditMessage(message.content)} title="Edit and resend this message"><Pencil size={13} /><span>Edit &amp; resend</span></button>}
      {copyState === "failed" && <span role="status">Clipboard unavailable. Select the response to copy it.</span>}
      {message.created_at && <time className="chat-message-time" dateTime={message.created_at}>{messageTime(message.created_at)}</time>}
    </div>}
  </article>;
});

type Props = {
  snapshot: Snapshot;
  draft?: Draft;
  messages: Message[];
  streamed: string;
  streaming: boolean;
  activity?: string;
  outcome?: ChatOutcome;
  failureMessage?: string;
  draftSaveWarning?: string;
  recoveryRequest?: { message: string; contextLabel?: string };
  onRecover?: () => void;
  blockedReason?: string;
  onReviewEdits?: () => void;
  discussionOnly?: boolean;
  active: boolean;
  busy: boolean;
  input: string;
  onInputChange: (value: string) => void;
  onSend: (value?: string) => void;
  onStop: () => void;
  onData: () => void;
  onReviewDraft: () => void;
  context?: ConversationContext;
  onClearContext?: () => void;
  archived?: ArchivedConversation[];
  sharedHistoryItems?: ConversationHistoryItem[];
  onSelectSharedHistory?: (id: string) => void;
  historySelection?: ConversationHistorySelection;
  canClear?: boolean;
  onClear?: () => void;
  onEditMessage?: (content: string) => void;
  onRetry?: (message: string) => void;
  composerRef: RefObject<HTMLTextAreaElement | null>;
  revisionPanel?: ReactNode;
  analysisPanel?: ReactNode;
  /** Receipts, questions and progress of an imported-report edit made from chat (EditChatCards). */
  editPanel?: ReactNode;
  onUseDax?: (handoff: ChatDaxHandoff) => void;
};

/** The transcript is the only scroller; the input and review controls stay reachable. */
export function ChatConversation({ snapshot, draft, messages, streamed, streaming, activity, outcome, failureMessage, draftSaveWarning, recoveryRequest, onRecover, blockedReason, onReviewEdits, discussionOnly, active, busy, input, onInputChange, onSend, onStop, onData, onReviewDraft, composerRef, revisionPanel, analysisPanel, editPanel, context, onClearContext, archived = [], sharedHistoryItems, onSelectSharedHistory, historySelection, canClear = false, onClear, onEditMessage, onRetry, onUseDax }: Props) {
  const workspaceRef = useRef<HTMLElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const olderAnchor = useRef<number | null>(null);
  const selectionRequest = useRef<number | undefined>(undefined);
  const pendingMessageFocus = useRef<string | undefined>(undefined);
  const [awayFromLatest, setAwayFromLatest] = useState(false);
  const [visibleCount, setVisibleCount] = useState(40);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [viewingArchive, setViewingArchive] = useState<string>();
  const localHistoryItems = useMemo(() => buildWorkspaceHistoryItems(messages, archived), [messages, archived]);
  const historyItems = sharedHistoryItems ?? localHistoryItems;
  const savedHistoryCount = historyItems.filter(item => !item.current).length;
  const sourceRequests = useMemo(() => {
    const requests = new Map<Message, string>();
    let user = "";
    for (const message of messages) {
      if (message.role === "user") user = message.content;
      else requests.set(message, user);
    }
    return requests;
  }, [messages]);
  const hiddenCount = Math.max(0, messages.length - visibleCount);
  const lastAssistantIndex = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === "assistant") return index;
    }
    return -1;
  }, [messages]);
  const archivedView = archived.find(conversation => workspaceArchiveId(conversation) === viewingArchive);
  const viewingHistory = archiveOpen || Boolean(archivedView);
  const returnToCurrent = useCallback(() => {
    pendingMessageFocus.current = undefined;
    setViewingArchive(undefined);
    setArchiveOpen(false);
    following.current = true;
    setAwayFromLatest(false);
    requestAnimationFrame(() => composerRef.current?.focus());
  }, [composerRef]);
  useLayoutEffect(() => {
    if (!historySelection || selectionRequest.current === historySelection.requestId) return;
    selectionRequest.current = historySelection.requestId;
    setViewingArchive(historySelection.id);
    setArchiveOpen(false);
    olderAnchor.current = null;
    following.current = historySelection.messageId === undefined && historySelection.id === undefined;
    pendingMessageFocus.current = historySelection.messageId === undefined ? undefined : String(historySelection.messageId);
    if (historySelection.id === undefined && historySelection.messageId !== undefined) {
      const index = messages.findIndex(message => String(message.id) === String(historySelection.messageId));
      if (index >= 0) setVisibleCount(count => Math.max(count, messages.length - index));
    }
    setAwayFromLatest(historySelection.messageId !== undefined);
  }, [historySelection, messages]);
  useLayoutEffect(() => {
    if (viewingArchive !== undefined) scrollRef.current?.focus({ preventScroll: true });
  }, [viewingArchive]);
  useEffect(() => {
    if (viewingArchive === undefined || !active) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented && workspaceRef.current?.contains(document.activeElement)) returnToCurrent();
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [viewingArchive, active, returnToCurrent]);
  const rowCount = snapshot.tables.reduce((sum, item) => sum + item.table.row_count, 0);
  const reviewLabel = draft?.status === "approved" ? "Approved draft" : "Review draft";
  const contextKind = context?.visual_id ? "Visual" : context?.page_id ? "Page" : context?.table_id ? "Table" : context?.measure_id ? "Measure" : context?.relationship_id ? "Relationship" : "Model";
  const suggestedQuestions = context
    ? context.visual_id ? ["What does this visual show?", "How could I make this visual clearer?"]
    : context.page_id ? ["Walk me through this page.", "What would make this page more useful?"]
    : context.table_id ? ["Explain the fields in this table.", "Are there data quality issues to review?"]
    : context.measure_id ? ["Explain this calculation and its filter context.", "How should I use this measure in a visual?"]
    : context.relationship_id ? ["Explain this relationship and any issues.", "What should I check before approving this relationship?"]
    : ["Explain how these tables fit together.", "What needs attention before I build?"]
    : draft?.understanding.suggested_questions.slice(0, 3).map(question => readableReferences(question, snapshot)) || ["What tables and fields are available?", "What should this dashboard focus on?"];

  const scrollToLatest = () => {
    following.current = true;
    setAwayFromLatest(false);
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  };

  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (!node || !active) return;
    if (pendingMessageFocus.current !== undefined) {
      const target = [...node.querySelectorAll<HTMLElement>("[data-message-id]")].find(message => message.dataset.messageId === pendingMessageFocus.current);
      if (target) {
        pendingMessageFocus.current = undefined;
        following.current = false;
        target.scrollIntoView({ block: "center" });
        target.focus({ preventScroll: true });
      }
      return;
    }
    if (viewingArchive !== undefined) {
      node.scrollTop = 0;
      return;
    }
    if (olderAnchor.current !== null) {
      node.scrollTop = node.scrollHeight - olderAnchor.current;
      olderAnchor.current = null;
    } else if (following.current) {
      node.scrollTop = node.scrollHeight;
    }
  }, [messages, streamed, streaming, activity, active, visibleCount, revisionPanel, editPanel, viewingArchive, historySelection]);

  // Follow wrapping/resize changes as well as tokens, but never pull a reader away
  // from older messages. Disconnect on unmount/workspace change.
  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!content) return;
    const observer = new ResizeObserver(() => {
      if (following.current && active && viewingArchive === undefined && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [active, viewingArchive]);

  useLayoutEffect(() => {
    const node = composerRef.current;
    if (!node || !active) return;
    node.style.height = "0px";
    node.style.height = `${Math.min(node.scrollHeight, 160)}px`;
  }, [input, active, composerRef]);

  const send = (value?: string) => {
    if (viewingHistory || busy || blockedReason || streaming || !(value ?? input).trim()) return;
    scrollToLatest();
    onSend(value);
  };

  return <section ref={workspaceRef} className="chat-workspace" aria-label="Chat with Local AI">
    <div className="chat-context-bar">
      <button type="button" className="chat-source-link" onClick={onData} title="Inspect selected local data"><Database size={14} /><span>{snapshot.tables.length} tables <span aria-hidden="true">·</span> {rowCount.toLocaleString()} rows</span></button>
      {draft && <button type="button" className="chat-draft-link" onClick={onReviewDraft}><span>{reviewLabel}</span><span className="chat-page-count">{draft.pages.length} pages</span><ArrowRight size={14} /></button>}
    </div>
    <div className="chat-session-toolbar" role="group" aria-label="Conversation controls">
      <span>{viewingHistory ? "Saved chats" : "Current chat"}</span>
      <button type="button" className="chat-history-toggle" aria-label="Chat history" aria-expanded={archiveOpen} disabled={busy || streaming} onClick={() => { pendingMessageFocus.current = undefined; setViewingArchive(undefined); setArchiveOpen(open => !open); }}><History size={13} aria-hidden="true" /><span>Chat history</span>{savedHistoryCount > 0 && <small aria-hidden="true">{savedHistoryCount}</small>}</button>
      {canClear && onClear && <button type="button" className="chat-new-button" disabled={busy || streaming || !messages.length} title="Keep this conversation in history and start a new chat" onClick={() => { returnToCurrent(); following.current = true; setVisibleCount(40); onClear(); }}><MessageSquarePlus size={13} aria-hidden="true" /><span>New chat</span></button>}
    </div>
    <div className="chat-transcript-frame">
      {archiveOpen && <ConversationHistory items={historyItems} onSelect={id => { setArchiveOpen(false); if (sharedHistoryItems && onSelectSharedHistory) onSelectSharedHistory(id); else setViewingArchive(historyItems.find(item => item.id === id)?.sourceId); }} onClose={returnToCurrent} />}
      <div className="chat-thread-scroll" hidden={archiveOpen} ref={scrollRef} role="region" tabIndex={0} aria-label="Conversation history" onScroll={(event) => {
        const node = event.currentTarget;
        if (!node.clientHeight) return;
        following.current = node.scrollHeight - node.clientHeight - node.scrollTop < 72;
        setAwayFromLatest(!following.current);
      }}>
        <div className="chat-thread" ref={contentRef}>
          {archivedView ? <>
            <div className="chat-archived-banner" role="status">
              <span>{archivedView.previousData ? "Previous data · " : ""}Saved conversation · read only</span>
              <button type="button" onClick={returnToCurrent}>Back to current chat</button>
            </div>
            {archivedView.messages.map((message, index) => <ChatMessage key={message.id ?? index} message={message} />)}
          </> : <>
          {hiddenCount > 0 && <button type="button" className="chat-history-button" onClick={() => {
            const node = scrollRef.current;
            if (node) olderAnchor.current = node.scrollHeight - node.scrollTop;
            following.current = false;
            setVisibleCount((count) => count + 40);
          }}>Load earlier messages ({hiddenCount})</button>}
          {!messages.length && !streaming && <div className="chat-welcome">
            <span className="qwen-welcome-mark" aria-hidden="true"><Sparkles size={24} /></span>
            <h2>{context ? `Ask about ${context.label}` : "What would you like to explore?"}</h2>
            <p>{context ? "The selected context is attached below. Ask a question or describe what you'd like to change." : "Ask a question, debug a formula, or describe a report change."}</p>
            <div className="chat-suggestions" aria-label="Suggested questions">{suggestedQuestions.map((question) => <button type="button" key={question} disabled={busy} title="Use this question in the message box" onClick={() => { onInputChange(question); composerRef.current?.focus(); }}>{question}<ArrowRight size={14} /></button>)}</div>
          </div>}
          {messages.slice(-visibleCount).map((message, index) => <ChatMessage key={message.id ?? `${message.created_at}-${hiddenCount + index}`} message={message} sourceRequest={sourceRequests.get(message)} onEditMessage={onEditMessage} retryMessage={hiddenCount + index === lastAssistantIndex ? sourceRequests.get(message) : undefined} onRetry={busy || streaming || blockedReason ? undefined : onRetry} onUseDax={busy || streaming ? undefined : onUseDax} />)}
          {streamed && <ChatMessage message={{ role: "assistant", content: streamed, citations: [], created_at: "" }} streaming={streaming} />}
          {streaming && !streamed && <div className="chat-pending"><span className="qwen-mark" aria-hidden="true"><Sparkles size={17} /></span><ResponseActivity label={activity || "Preparing a response"} /></div>}
          {!streaming && outcome && <div className="chat-response-outcome" role="status"><div><span>{outcome === "stopped" ? "Response stopped." : failureMessage || "Response interrupted."}</span>{recoveryRequest && <><p className="chat-recovery-request" title={recoveryRequest.message}>{recoveryRequest.message}</p><small>Retry target: {recoveryRequest.contextLabel || "Whole workspace"}</small></>}</div>{recoveryRequest && onRecover && <button type="button" disabled={busy || Boolean(blockedReason)} title={blockedReason} onClick={() => { scrollToLatest(); onRecover(); }}><RotateCcw size={13} />Retry</button>}</div>}
          {!streaming && discussionOnly && <p className="chat-discussion-note" role="status">Explanation only · No draft or Power BI changes were applied.</p>}
          {revisionPanel}
          {analysisPanel}
          {editPanel}
          {!streaming && activity && <div className="chat-pending"><span className="qwen-mark" aria-hidden="true"><Sparkles size={17} /></span><ResponseActivity label={activity} /></div>}
          </>}
        </div>
      </div>
      {awayFromLatest && !archivedView && <button type="button" className="chat-jump-latest" onClick={scrollToLatest}><ArrowDown size={15} />Jump to latest</button>}
    </div>
    {!viewingHistory && <div className="chat-compose-dock">
      {draftSaveWarning && <p className="chat-draft-warning" role="status">{draftSaveWarning}</p>}
      {blockedReason && <div className="chat-blocked-notice" role="status"><span id="chat-blocked-reason">{blockedReason}</span>{onReviewEdits && <button type="button" onClick={onReviewEdits}>Review report edits<ArrowRight size={13} /></button>}</div>}
      <form className="chat-input-box" onSubmit={(event) => { event.preventDefault(); send(); }}>
        {context && <button type="button" className="chat-context-chip" onClick={onClearContext} aria-label={`Remove attached context: ${context.label}`} title="Remove context and ask about the whole workspace"><Sparkles size={13} aria-hidden="true" /><span><small>{contextKind}</small>{context.label}</span><X size={13} aria-hidden="true" /></button>}
        <textarea ref={composerRef} value={input} onChange={(event) => onInputChange(event.target.value)} onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(); }
        }} rows={1} maxLength={4000} placeholder={context ? `Ask about ${context.label}…` : "Message Local AI…"} aria-label="Message Local AI" aria-describedby={blockedReason ? "chat-input-hint chat-blocked-reason" : "chat-input-hint"} />
        <div className="chat-input-actions">
          <span className="chat-local-label">{context ? "Selected context attached" : "Workspace conversation"}</span>
          {input.length > 3200 && <span className="chat-char-count" role="status">{input.length}/4000</span>}
          {streaming ? <button key="stop" type="button" className="chat-send-button chat-stop-button" onClick={onStop} aria-label="Stop response" title="Stop response"><Square size={14} fill="currentColor" /></button> : <button key="send" type="submit" className="chat-send-button" disabled={!input.trim() || busy || Boolean(blockedReason)} aria-label="Send message" title="Send message"><ArrowUp size={19} /></button>}
        </div>
      </form>
      <div id="chat-input-hint" className="chat-input-hint"><span><ShieldCheck size={12} />Local data. Review AI suggestions before building.</span><span>Shift + Enter for a new line</span></div>
    </div>}
  </section>;
}
