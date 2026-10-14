import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, ArrowDown, ArrowUp, BarChart3, Calendar, CheckCircle2, ChevronRight, Copy, Hash, History, Loader2, MessageSquarePlus, Play, RotateCcw, Save, Search, Sigma, Sparkles, Square, Table2, ToggleLeft, Type, Undo2, X } from "lucide-react";
import type { DaxResult, Draft, Snapshot } from "./types";
import { daxExamples, extractDaxFormula, isDaxTableExpression, normalizeDaxSuggestion, joinedDaxReferences, nativeBreakdownQuery, nativeGroupingFields } from "./daxAssistance";
import type { DaxAssistInput, DaxAssistAnswer } from "./api";
import { idempotencyKey } from "./api";
import { DaxPreview } from "./DaxPreview";
import { RichText } from "./ProductUI";
import { DaxConversationPersistence } from "./DaxConversationPersistence";
import { formulaNotes, type DaxCheckStatus } from "./daxCheckStatus";
import { activeDaxPending, archiveDaxTurns, currentDaxTurns, daxArchiveId, daxArchiveKey, daxComposerKey, daxConversationKey, daxDiscussion, daxHandoffDiscussion, daxPendingKey, daxRequestCanApply, daxSourceIdentity, isDaxClarification, mergeDaxTurns, readDaxArchives, readDaxComposer, readDaxConversation, readDaxPending, reconcileDaxTurns, type DaxArchive, type DaxRecoveryRequest, type DaxTurn } from "./daxConversation";
import { ConversationHistory, conversationTitle, type ConversationHistoryItem, type ConversationHistorySelection } from "./ConversationHistory";
import { ResponseActivity } from "./ChatConversation";
import { SaveImportedMeasure } from "./SaveImportedMeasure";
import { SaveImportedCalculation } from "./ModelEditorDaxSave";
import { measureHeaderName, queuedMeasureNote, splitMeasureDefinition } from "./daxMeasureSave";
import { measureNameProblem } from "./importedEditing";
import { AsyncActionButton } from "./AsyncActionButton";
import { AsyncActionLifetime, type AsyncActionStatus } from "./asyncAction";
import "./dax-lab.css";

const EMPTY_HISTORY: DaxTurn[] = [];
const EMPTY_TABLES: string[] = [];
const EMPTY_ARCHIVES: DaxArchive[] = [];

function CopyDaxAnswer({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  if (!text.trim()) return null;
  return <div className="dax-answer-copy"><button type="button" onClick={() => {
    void (async () => {
      try { await navigator.clipboard.writeText(text); setCopied(true); setFailed(false); }
      catch { setCopied(false); setFailed(true); }
    })();
  }} aria-label="Copy DAX response"><Copy size={13} aria-hidden="true" />{copied ? "Copied" : "Copy"}</button>{failed && <span role="status">Select the response to copy it.</span>}</div>;
}

// Short tags keep measure names readable in the narrow schema column.
const OPERATION_TAGS: Record<string, string> = { average: "avg", minimum: "min", maximum: "max", distinct_count: "distinct", weighted_average: "weighted", running_total: "running", period_to_date: "to date", period_change: "change" };

/** Case-insensitive schema filter; a table-name match keeps all of its columns. */
function matchesSchema(query: string, ...labels: string[]): boolean {
  const needle = query.trim().toLocaleLowerCase();
  return !needle || labels.some((label) => label.toLocaleLowerCase().includes(needle));
}

function FieldTypeIcon({ type }: { type: string }) {
  const Icon = type === "integer" || type === "number" ? Hash
    : type === "date" || type === "datetime" ? Calendar
      : type === "boolean" ? ToggleLeft
        : Type;
  return <Icon className={`dax-field-icon dax-field-${type}`} size={13} aria-hidden="true" />;
}

/** What checked an imported formula, next to it: an unvalidated formula must not look
 * like a validated one. */
function CheckStatus({ status }: { status?: DaxCheckStatus }) {
  if (!status) return null;
  const Icon = status.tone === "ok" ? CheckCircle2 : AlertTriangle;
  return <p className="dax-turn-created dax-check-status" style={{ margin: "0 0 8px", ...(status.tone === "warn" ? { color: "#b45309" } : {}) }}><Icon size={13} aria-hidden="true" />{status.text}</p>;
}

function IncompleteResponse({ turn }: { turn: DaxTurn }) {
  if (!turn.status || turn.status === "completed") return null;
  const label = turn.status === "cancelled" ? "Stopped response" : turn.status === "failed" ? "Response failed" : "Interrupted response";
  return <p className="dax-response-state" role="status"><AlertTriangle size={13} aria-hidden="true" />{label} · Not completed</p>;
}

function CalculationOutcome({ turn, archived = false, editorRetained = false }: { turn: DaxTurn; archived?: boolean; editorRetained?: boolean }) {
  if (turn.status && turn.status !== "completed") return null;
  if (isDaxClarification(turn)) return <div className="dax-calculation-outcome clarification" role="status"><strong>Needs clarification</strong><span>{archived ? "No formula was created for this request." : "Current formula retained. Clarify the request before generating a formula."}</span></div>;
  if (turn.kind === "formula" && turn.formula) return <div className="dax-calculation-outcome" role="status"><strong>Formula ready to test</strong><span>{editorRetained ? "Current formula retained. Use formula to preview this candidate." : "Preview it in the editor. Save or queue it separately to add it to the report."}</span></div>;
  return null;
}

export type DaxHandoff = { request: string; sourceLabel: string; sourceMessageId?: string | number; code: string; label?: string };

/** Browser-side DAX scratchpad: evaluates the imported snapshot through a
 * bounded, read-only subset. Saving promotes the expression to an
 * advanced-DAX proposal that still requires explicit approval. */
export function DaxLab({ workspaceId, snapshot, draft, busy, active = true, chatBoundary = 0, onRun, onSave, onAsk, suggestion, suggestionLabel = "AI suggestion", importedMeasures, importedTables = EMPTY_TABLES, onDismissSuggestion, history = EMPTY_HISTORY, archived = EMPTY_ARCHIVES, durableHistory = false, onNewChat, externalConversation = false, conversationHost, handoff, handoffStorageWarning, onBackToSource, onClearHandoff, onOpenConversation, sharedHistoryItems, onSelectSharedHistory, historySelection, onHistoryItemsChange }: {
  workspaceId: string;
  snapshot?: Snapshot;
  draft?: Draft;
  busy: boolean;
  active?: boolean;
  /** Durable New chat boundary for this workspace and snapshot. */
  chatBoundary?: number;
  onRun: (expression: string) => Promise<DaxResult>;
  /** A draft measure takes a name and a formula. For an imported report `options` also says which table the measure lives in and its number format, and saving queues it in Pending Power BI changes. */
  onSave: (name: string, expression: string, options?: { table?: string; format?: string }) => Promise<void>;
  onAsk: (input: DaxAssistInput, signal: AbortSignal, onStatus: (text: string) => void) => Promise<DaxAssistAnswer>;
  suggestion?: string;
  suggestionLabel?: string;
  importedMeasures?: { TableName: string; Name: string; Expression: string }[];
  /** The tables of an imported model a new measure can live in (see measureHomeTables). */
  importedTables?: string[];
  onDismissSuggestion: () => void;
  history?: DaxTurn[];
  archived?: DaxArchive[];
  durableHistory?: boolean;
  onNewChat?: () => Promise<void>;
  externalConversation?: boolean;
  conversationHost?: HTMLElement | null;
  handoff?: DaxHandoff;
  handoffStorageWarning?: string;
  onBackToSource?: () => void;
  onClearHandoff?: () => void;
  onOpenConversation?: () => void;
  sharedHistoryItems?: ConversationHistoryItem[];
  onSelectSharedHistory?: (id: string) => void;
  historySelection?: ConversationHistorySelection;
  onHistoryItemsChange?: (items: ConversationHistoryItem[]) => void;
}) {
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const assistantAbort = useRef<AbortController | null>(null);
  const chatBoundaryRef = useRef(chatBoundary);
  const handoffRef = useRef(handoff);
  handoffRef.current = handoff;
  useEffect(() => () => assistantAbort.current?.abort(), []);
  const suggestionRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (suggestion) {
      suggestionRef.current?.scrollIntoView({ block: "nearest" });
      suggestionRef.current?.focus({ preventScroll: true });
    }
  }, [suggestion]);
  const [expression, setExpression] = useState(() => {
    try { return localStorage.getItem(`studio.dax.${workspaceId}`) || ""; }
    catch { return ""; }
  });
  const expressionRef = useRef(expression);
  expressionRef.current = expression;
  const [selectedField, setSelectedField] = useState<{ table: string; name: string; ref: string }>();
  const [schemaQuery, setSchemaQuery] = useState("");
  const [nativeGroup, setNativeGroup] = useState("");
  const [result, setResult] = useState<DaxResult>();
  const [error, setError] = useState<string>();
  const [running, setRunning] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [saving, setSaving] = useState(false);
  const [savedNote, setSavedNote] = useState<string>();
  const saveLifetime = useRef(new AsyncActionLifetime());
  const [saveFeedback, setSaveFeedback] = useState<{ action: string; status: AsyncActionStatus; failure?: string }>();
  const composerKey = daxComposerKey(workspaceId, snapshot?.id);
  const [goal, setGoal] = useState(() => readDaxComposer(composerKey));
  const [askError, setAskError] = useState<string>();
  const [assisting, setAssisting] = useState(false);
  const [assistantStatus, setAssistantStatus] = useState("");
  const archiveKey = daxArchiveKey(workspaceId, snapshot?.id);
  const [archives, setArchives] = useState<DaxArchive[]>(() => durableHistory ? archived : readDaxArchives(archiveKey));
  const archivesRef = useRef(archives);
  useEffect(() => { archivesRef.current = archives; }, [archives]);
  const [attempts, setAttempts] = useState<DaxTurn[]>(() => currentDaxTurns(reconcileDaxTurns(history, readDaxConversation(daxConversationKey(workspaceId, snapshot?.id))), archives, chatBoundary));
  useEffect(() => {
    if (durableHistory) { setArchives(archived); archivesRef.current = archived; }
    if (history.length || durableHistory) setAttempts(current => currentDaxTurns(reconcileDaxTurns(history, current), durableHistory ? archived : archivesRef.current, chatBoundary));
  }, [history, archived, durableHistory, chatBoundary]);
  const [startingNewChat, setStartingNewChat] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [viewingArchive, setViewingArchive] = useState<string>();
  const [visibleTurns, setVisibleTurns] = useState(24);
  const pendingKey = daxPendingKey(workspaceId, snapshot?.id);
  const [pendingRequest, setPendingRequest] = useState<DaxRecoveryRequest | undefined>(() => {
    return activeDaxPending(readDaxPending(pendingKey), attempts, archives, chatBoundary);
  });
  useEffect(() => {
    if (pendingRequest && !activeDaxPending(pendingRequest, attempts, archives, chatBoundary)) setPendingRequest(undefined);
  }, [attempts, archives, pendingRequest, chatBoundary]);
  useLayoutEffect(() => {
    if (chatBoundaryRef.current === chatBoundary) return;
    chatBoundaryRef.current = chatBoundary;
    assistantAbort.current?.abort();
    setAttempts(current => currentDaxTurns(current, archivesRef.current, chatBoundary));
    setPendingRequest(undefined); setAskError(undefined); setAssistantStatus(""); setNameDrafts({});
  }, [chatBoundary]);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const following = useRef(true);
  const [awayFromLatest, setAwayFromLatest] = useState(false);
  const [nameDrafts, setNameDrafts] = useState<Record<number, string>>({});
  const threadRef = useRef<HTMLDivElement>(null);
  const [previousFormula, setPreviousFormula] = useState<{ expression: string; name: string; result?: DaxResult; error?: string }>();
  const locked = busy || running || saving || assisting || startingNewChat;
  // An imported report has no draft: a measure can be saved as soon as the model has a table to put it in.
  const [queueTable, setQueueTable] = useState("");
  const homeTable = importedTables.includes(queueTable) ? queueTable : importedTables[0] ?? "";
  const canSaveMeasure = importedMeasures ? Boolean(homeTable) : Boolean(draft);
  const nativeModel = importedMeasures !== undefined;
  const canEvaluate = Boolean(snapshot) || nativeModel;
  const proposed = suggestion ? normalizeDaxSuggestion(suggestion) : undefined;
  const joinedReferences = joinedDaxReferences(expression);
  const isQuery = isDaxTableExpression(expression);
  const saveScope = JSON.stringify([workspaceId, snapshot?.id, draft?.id, chatBoundary, nativeModel]);
  const saveInputRef = useRef({ scope: saveScope, expression, saveName, attempts, nameDrafts, homeTable });
  saveInputRef.current = { scope: saveScope, expression, saveName, attempts, nameDrafts, homeTable };
  const editorSaveAction = JSON.stringify([saveScope, "editor", expression.trim()]);
  const turnIdentity = (turn: DaxTurn) => turn.id ?? JSON.stringify([turn.goal, turn.response, turn.formula]);
  const turnSaveAction = (scope: string, turn: DaxTurn, name: string, table: string) => JSON.stringify([scope, "suggestion", turnIdentity(turn), turn.formula?.expression, name.trim(), table]);
  useLayoutEffect(() => {
    saveLifetime.current.invalidate();
    setSaveFeedback(undefined); setSavedNote(undefined); setSaving(false);
  }, [saveScope]);
  useEffect(() => () => saveLifetime.current.invalidate(), []);
  const resetSaveFeedback = () => {
    saveLifetime.current.invalidate();
    setSaveFeedback(undefined); setSavedNote(undefined); setSaving(false);
  };

  const updateExpression = (value: string) => {
    setExpression(value);
    setNativeGroup("");
    setResult(undefined);
    setError(undefined);
    setSavedNote(undefined);
    resetSaveFeedback();
    setAssistantStatus("");
    try { localStorage.setItem(`studio.dax.${workspaceId}`, value); }
    catch { /* Persistence is best-effort. */ }
  };

  const insertAtCursor = (snippet: string) => {
    setPreviousFormula({ expression, name: saveName, result, error });
    const editor = editorRef.current;
    const start = editor?.selectionStart ?? expression.length;
    const end = editor?.selectionEnd ?? expression.length;
    updateExpression(
      editor
        ? `${expression.slice(0, start)}${snippet}${expression.slice(end)}`
        : expression ? `${expression}\n${snippet}` : snippet,
    );
    requestAnimationFrame(() => editor?.focus());
  };

  const onExpressionChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    updateExpression(event.target.value);
  };

  const run = async () => {
    const text = expression.trim();
    if (!text || locked || !canEvaluate) return;
    if (joinedReferences) { setError("Several expressions have been joined without an operator. Preview a measure from the list to replace this formula, or edit the formula to add the intended operator."); return; }
    setRunning(true);
    setNativeGroup("");
    setResult(undefined);
    setError(undefined);
    setSavedNote(undefined);
    try {
      setResult(await onRun(text));
    } catch (reason) {
      setResult(undefined);
      setError(reason instanceof Error ? reason.message : "The expression could not be evaluated");
    } finally {
      setRunning(false);
    }
  };

  const save = async () => {
    const name = saveName.trim();
    const text = expression.trim();
    if (!name || !text || locked || !draft || isQuery) return;
    const token = saveLifetime.current.begin(JSON.stringify([saveScope, "editor", text, name]));
    if (!token) return;
    setSaving(true);
    setError(undefined);
    setSavedNote(undefined);
    setSaveFeedback({ action: editorSaveAction, status: "pending" });
    const ownsSave = () => {
      const current = saveInputRef.current;
      return saveLifetime.current.owns(token, JSON.stringify([current.scope, "editor", current.expression.trim(), current.saveName.trim()]));
    };
    try {
      await onSave(name, text);
      if (!ownsSave()) return;
      setSaveFeedback({ action: editorSaveAction, status: "success" });
      setSavedNote(`Saved "${name}" to the draft — approve it in Review & build before building.`);
      setSaveName("");
    } catch (reason) {
      if (!ownsSave()) return;
      const failure = reason instanceof Error ? reason.message : "The measure could not be saved";
      setSaveFeedback({ action: editorSaveAction, status: "error", failure });
      setError(failure);
    } finally {
      if (saveLifetime.current.release(token)) setSaving(false);
    }
  };

  const tables = snapshot?.tables ?? [];
  const nativeFields = nativeGroupingFields(tables);
  const measures = draft?.measure_proposal.measures ?? [];
  const responseParts = (response: string) => response.split(/\n\n(?=References supplied)/);
  // Used by both completed AI generations and explicit selection of an older
  // formula. Only the scratchpad and read-only preview change here.
  const applyFormula = async (formula: { expression: string; name: string }, signal?: AbortSignal) => {
    if (!canEvaluate || signal?.aborted) return;
    setRunning(true);
    setPreviousFormula({ expression, name: saveName, result, error });
    updateExpression(formula.expression);
    setSaveName(formula.name);
    setResult(undefined);
    setError(undefined);
    setSavedNote(undefined);
    setAssistantStatus("Checking the formula and calculating…");
    try {
      const preview = await onRun(formula.expression);
      if (signal?.aborted) return;
      setResult(preview);
      setAssistantStatus("Preview ready · Check the result against your request");
    } catch (reason) {
      if (signal?.aborted) return;
      setError(reason instanceof Error ? reason.message : "The expression could not be evaluated");
      setAssistantStatus("The formula needs a correction");
    } finally {
      setRunning(false);
    }
  };
  const previewFormula = async (formula: { expression: string; name: string }) => {
    if (locked) return;
    await applyFormula(formula);
  };
  const createMeasure = async (index: number) => {
    const turn = attempts[index];
    const name = (nameDrafts[index] ?? turn?.formula?.name ?? "").trim();
    if (!turn?.formula || !name || locked || !canSaveMeasure || (importedMeasures && measureNameProblem(name, importedMeasures.map(item => item.Name)))) return;
    const action = turnSaveAction(saveScope, turn, name, homeTable);
    const token = saveLifetime.current.begin(action);
    if (!token) return;
    setSaving(true);
    setError(undefined);
    setSavedNote(undefined);
    setSaveFeedback({ action, status: "pending" });
    const ownsSave = () => {
      const current = saveInputRef.current;
      const currentIndex = current.attempts.findIndex(item => turnIdentity(item) === turnIdentity(turn));
      const currentTurn = current.attempts[currentIndex];
      if (!currentTurn?.formula) return false;
      const currentName = current.nameDrafts[currentIndex] ?? currentTurn.formula.name;
      return saveLifetime.current.owns(token, turnSaveAction(current.scope, currentTurn, currentName, current.homeTable));
    };
    try {
      if (importedMeasures) await onSave(name, turn.formula.expression, { table: homeTable });
      else await onSave(name, turn.formula.expression);
      if (!ownsSave()) return;
      setSaveFeedback({ action, status: "success" });
      setAttempts(value => value.map(item => turnIdentity(item) === turnIdentity(turn) ? { ...item, created: name } : item));
      setSavedNote(importedMeasures ? queuedMeasureNote(name) : `Saved "${name}" to the draft — approve it in Review & build before building.`);
    } catch (reason) {
      if (!ownsSave()) return;
      const failure = reason instanceof Error ? reason.message : "The measure could not be saved";
      setSaveFeedback({ action, status: "error", failure });
    } finally {
      if (saveLifetime.current.release(token)) setSaving(false);
    }
  };
  const ask = async (mode: DaxAssistInput["mode"] = "auto", request = goal, retryInput?: DaxRecoveryRequest) => {
    if (!request.trim() || locked || assistantAbort.current) return;
    if (!snapshot) { setAskError("AI formula assistance needs a saved-data snapshot. You can run native queries in the editor using this report's Power BI Desktop working copy."); return; }
    if (retryInput && !activeDaxPending(retryInput, attempts, archives, chatBoundary)) { setPendingRequest(undefined); return; }
    onOpenConversation?.();
    const controller = new AbortController();
    assistantAbort.current = controller;
    setAssisting(true);
    setArchiveOpen(false);
    setViewingArchive(undefined);
    // Retrying reuses the original receipt and context, even if the editor has changed.
    const input = retryInput ?? { goal: request, expression, error: error?.slice(0, 600), mode, discussion: handoff ? daxHandoffDiscussion(handoff.request, handoff.sourceLabel, attempts) : daxDiscussion(attempts), requestId: idempotencyKey(), chatBoundary, sourceIdentity: daxSourceIdentity(handoff) };
    setPendingRequest(input);
    if (!retryInput && request === goal) setGoal("");
    following.current = true;
    setAwayFromLatest(false);
    setAskError(undefined);
    setAssistantStatus(mode === "auto" ? "Thinking through your request…" : mode === "explain" ? "Explaining your formula…" : "Writing your formula…");
    try {
      const { sourceIdentity: _sourceIdentity, ...apiInput } = input;
      const answer = await onAsk(apiInput, controller.signal, text => {
        if (!controller.signal.aborted) setAssistantStatus(text);
      });
      if (controller.signal.aborted || (input.chatBoundary ?? 0) !== chatBoundaryRef.current) return;
      const formula = answer.kind === "formula" ? extractDaxFormula(answer.text) : undefined;
      const canApply = Boolean(formula && daxRequestCanApply(input, expressionRef.current, chatBoundaryRef.current, handoffRef.current));
      setAttempts(value => mergeDaxTurns(value, [{ id: answer.requestId, goal: request, response: answer.text, kind: answer.kind, calculationRequest: answer.calculationRequest, formula, chatBoundary: input.chatBoundary ?? 0, editorRetained: Boolean(formula && !canApply), createdAt: new Date().toISOString(), status: "completed" }]));
      setPendingRequest(undefined);
      if (answer.kind !== "formula") {
        setAssistantStatus("");
        return;
      }
      if (!formula) {
        setAssistantStatus("");
        setAskError("The AI did not return one complete formula. Refine your request in the conversation. Your formula is unchanged.");
        return;
      }
      if (canApply) await applyFormula(formula, controller.signal);
      else setAssistantStatus("");
    } catch (reason) {
      if ((input.chatBoundary ?? 0) !== chatBoundaryRef.current) return;
      const failure = controller.signal.aborted ? "Response stopped. Retry when you’re ready." : reason instanceof Error ? reason.message : "Could not generate a formula";
      setAttempts(value => mergeDaxTurns(value, [{ id: input.requestId, goal: input.goal, response: "", kind: "advice", status: controller.signal.aborted ? "cancelled" : "failed", error: failure, chatBoundary: input.chatBoundary ?? 0, createdAt: new Date().toISOString() }]));
      if (!controller.signal.aborted) {
        setAskError(failure);
        setAssistantStatus("");
      } else {
        setAssistantStatus("Response stopped. You can retry or ask a follow-up.");
      }
    } finally {
      if (assistantAbort.current === controller) {
        if ((input.chatBoundary ?? 0) !== chatBoundaryRef.current) setAssistantStatus("");
        else if (controller.signal.aborted) setAssistantStatus("Response stopped. Retry when you’re ready.");
        assistantAbort.current = null;
        setAssisting(false);
      }
    }
  };
  // A new chat sends the model no earlier turns: the archived chat stays readable in History.
  const resetThread = () => {
    setPendingRequest(undefined);
    setAskError(undefined);
    setAssistantStatus("");
    setNameDrafts({});
    setArchiveOpen(false);
    setViewingArchive(undefined);
    following.current = true;
    setAwayFromLatest(false);
    setVisibleTurns(24);
  };
  const startNewChat = async () => {
    if (locked) return;
    if (!attempts.length && !pendingRequest) { onClearHandoff?.(); resetThread(); requestAnimationFrame(() => composerRef.current?.focus()); return; }
    setStartingNewChat(true); setAskError(undefined);
    try {
      if (durableHistory) {
        if (!onNewChat) throw new Error("Restart the local engine to enable saved DAX chats.");
        await onNewChat();
      } else {
        const next = archiveDaxTurns(archives, attempts);
        // Preserve the active chat if this device cannot retain its archive.
        try { localStorage.setItem(archiveKey, JSON.stringify(next)); }
        catch { throw new Error("Could not save chat history on this device. Your conversation is still here."); }
        archivesRef.current = next; setArchives(next);
      }
      onClearHandoff?.(); setAttempts([]); resetThread();
      requestAnimationFrame(() => composerRef.current?.focus());
    } catch (reason) { setArchiveOpen(false); setViewingArchive(undefined); setAskError(reason instanceof Error ? reason.message : "Could not start a new chat. Your conversation is still here."); }
    finally { setStartingNewChat(false); }
  };
  const archivedView = viewingArchive === undefined ? undefined : archives.find(archive => daxArchiveId(archive) === viewingArchive);
  const historyItems = useMemo(() => archives.map(archive => ({ id: daxArchiveId(archive), title: conversationTitle(archive.turns[0]?.goal || "", "DAX chat"), preview: conversationTitle(archive.turns.at(-1)?.response.replace(/```\w*/g, "") || ""), updatedAt: archive.endedAt, turnCount: archive.turns.length * 2, searchText: archive.turns.map(turn => `${turn.goal}\n${turn.response}`).join("\n"), note: archive.previousData ? "Previous data" : "DAX Lab" })), [archives]);
  const globalHistoryItems = useMemo<ConversationHistoryItem[]>(() => [
    { id: "dax:current", scope: "dax", current: true, title: conversationTitle(attempts[0]?.goal || "", "Current DAX chat"), preview: conversationTitle(attempts.at(-1)?.response.replace(/```\w*/g, "") || "Your current calculation conversation"), updatedAt: attempts.at(-1)?.createdAt || "", turnCount: attempts.length * 2, searchText: attempts.map(turn => `${turn.goal}\n${turn.response}`).join("\n") },
    ...historyItems.map(item => ({ ...item, id: `dax:${item.id}`, sourceId: item.id, scope: "dax" as const })),
  ], [attempts, historyItems]);
  useEffect(() => { onHistoryItemsChange?.(globalHistoryItems); }, [globalHistoryItems, onHistoryItemsChange]);
  const returnToCurrent = () => { setArchiveOpen(false); setViewingArchive(undefined); setVisibleTurns(24); following.current = true; setAwayFromLatest(false); requestAnimationFrame(() => composerRef.current?.focus()); };
  const lastHistorySelection = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!historySelection || historySelection.requestId === lastHistorySelection.current) return;
    lastHistorySelection.current = historySelection.requestId;
    setArchiveOpen(false); setViewingArchive(historySelection.id); setVisibleTurns(24);
    following.current = historySelection.id === undefined; setAwayFromLatest(false);
    if (historySelection.id === undefined) requestAnimationFrame(() => composerRef.current?.focus());
  }, [historySelection]);

  useLayoutEffect(() => {
    const node = threadRef.current;
    if (active && following.current && node?.clientHeight) node.scrollTo({ top: node.scrollHeight });
  }, [attempts.length, assisting, active, conversationHost, archiveOpen, viewingArchive]);
  useEffect(() => {
    const node = threadRef.current;
    if (!node) return;
    const observer = new ResizeObserver(() => {
      if (active && following.current && node.clientHeight) node.scrollTo({ top: node.scrollHeight });
    });
    observer.observe(node);
    return () => observer.disconnect();
    // The live thread remounts after History is closed; observe the new node.
  }, [archiveOpen, viewingArchive, conversationHost, active]);

  const savedChatCount = sharedHistoryItems?.filter(item => !item.current).length ?? archives.length;
  const conversation = <section className={`dax-conversation${externalConversation ? " shared-assistant-conversation" : ""}`} aria-label="DAX conversation">
        {!archiveOpen && <header className={`dax-conversation-heading${externalConversation ? " chat-session-toolbar" : ""}`}>
          {externalConversation ? <span>{archivedView ? "Saved chat" : "Current chat"}</span> : <><span className="qwen-mark" aria-hidden="true"><Sparkles size={17} /></span><div><h2>DAX conversation</h2><p>Work through your calculation with Local AI</p></div></>}
          <div className="dax-chat-actions">
            <button type="button" className="dax-chat-action" aria-label="Chat history" aria-expanded={archiveOpen} disabled={locked} title={sharedHistoryItems ? "View Workspace and DAX chats" : "View saved DAX chats"} onClick={() => { setViewingArchive(undefined); setArchiveOpen(open => !open); }}><History size={13} aria-hidden="true" />Chat history{savedChatCount > 0 && <span className="dax-history-count" aria-hidden="true">{savedChatCount}</span>}</button>
            <button type="button" className="dax-chat-action" disabled={locked || (!attempts.length && !pendingRequest && !handoff)} title="Save the current chat to history and start fresh. Your formula and typed message stay here." onClick={() => void startNewChat()}>{startingNewChat ? <Loader2 size={13} className="spin" aria-hidden="true" /> : <MessageSquarePlus size={13} aria-hidden="true" />}New chat</button>
          </div>
        </header>}
        <DaxConversationPersistence storageKey={daxConversationKey(workspaceId, snapshot?.id)} turns={attempts} composerKey={composerKey} composer={goal} pendingKey={pendingKey} pending={pendingRequest} durableHistory={durableHistory} />
        {!archiveOpen && !archivedView && handoff && <div className="dax-handoff" role="region" aria-label="Workspace request context"><div><strong>{handoff.label || "From Workspace chat"}</strong>{onClearHandoff && <button type="button" className="icon-button" aria-label="Detach source request" title="Remove the source request from DAX context" disabled={locked} onClick={onClearHandoff}><X size={13} /></button>}</div><p>{handoff.request}</p><small>Original request included in this DAX chat.</small><details className="dax-handoff-formula"><summary>Source formula</summary><pre><code>{handoff.code}</code></pre></details>{onBackToSource && <button type="button" onClick={onBackToSource}>Back to source chat</button>}</div>}
        {!archiveOpen && !archivedView && handoffStorageWarning && <small className="dax-handoff-storage" role="status">{handoffStorageWarning}</small>}
        {archiveOpen && <ConversationHistory items={sharedHistoryItems ?? historyItems} label={sharedHistoryItems ? "Chat history" : "DAX chat history"} hint={durableHistory ? undefined : "Saved on this device. Open a chat to read it."} onSelect={id => { if (onSelectSharedHistory) onSelectSharedHistory(id); else { setViewingArchive(id); setVisibleTurns(24); following.current = false; } setArchiveOpen(false); }} onClose={returnToCurrent} />}
        {!archiveOpen && (archivedView ? <div className="dax-thread" role="region" aria-label="Archived DAX chat" tabIndex={0}>
          <div className="chat-archived-banner" role="status"><span>{archivedView.previousData ? "Previous data · Read only" : "Earlier chat · Read only"}</span><span className="dax-archive-actions"><button type="button" onClick={returnToCurrent}>Return to current chat</button></span></div>
          {archivedView.turns.length > visibleTurns && <button type="button" className="dax-load-earlier" onClick={() => setVisibleTurns(count => count + 24)}>Show earlier messages</button>}
          {archivedView.turns.slice(-visibleTurns).map((turn, index) => { const notes = formulaNotes(turn.response); return <div className="dax-turn" key={turn.id ?? index}>
            <div className="dax-turn-goal"><span>{turn.goal}</span></div>
            <div className="dax-turn-answer"><div className="chat-message-author"><span className="qwen-mark" aria-hidden="true"><Sparkles size={15} /></span><strong>Local AI</strong></div>
              <IncompleteResponse turn={turn} />
              <CalculationOutcome turn={turn} archived />
              {turn.kind === "formula" && (!turn.status || turn.status === "completed") && turn.formula
                ? <><pre><code>{turn.formula.name ? `${turn.formula.name} = ${turn.formula.expression}` : turn.formula.expression}</code></pre><CheckStatus status={notes.status} />{notes.notes && <RichText text={notes.notes} />}{notes.references && <details className="dax-ai-details"><summary>References</summary><RichText text={notes.references} /></details>}{!archivedView.previousData && <div className="dax-turn-actions"><button type="button" className="secondary" disabled={locked || !canEvaluate} onClick={() => void previewFormula(turn.formula!)}><Play size={13} />Use formula</button></div>}</>
                : <><RichText text={responseParts(turn.response)[0] || turn.error || "No response was completed."} />{turn.error && turn.response && <p className="dax-chat-recovery">{turn.error}</p>}{responseParts(turn.response).length > 1 && <details className="dax-ai-details"><summary>References</summary><RichText text={responseParts(turn.response).slice(1).join("\n\n")} /></details>}</>}
              <CopyDaxAnswer text={turn.response || turn.error || ""} />
            </div>
          </div>; })}
        </div> : <div className="dax-thread" ref={threadRef} role="region" aria-live="polite" aria-relevant="additions" aria-label="DAX assistant conversation" tabIndex={0} onScroll={event => {
          const node = event.currentTarget;
          if (!active || !node.clientHeight) return;
          following.current = node.scrollHeight - node.clientHeight - node.scrollTop < 72;
          setAwayFromLatest(!following.current);
        }}>
          {!attempts.length && !pendingRequest && <div className="dax-chat-welcome"><Sparkles size={26} aria-hidden="true" /><h3>Let's work out your calculation</h3><p>Describe what you're trying to understand. We can discuss the meaning, choose the right fields, and refine the formula together.</p><button type="button" className="secondary" onClick={() => { setGoal("Help me decide which calculation I need."); composerRef.current?.focus(); }}>Help me get started</button></div>}
          {attempts.length > visibleTurns && <button type="button" className="dax-load-earlier" onClick={() => { following.current = false; setVisibleTurns(count => count + 24); }}>Show earlier messages</button>}
          {attempts.slice(-visibleTurns).map((turn, localIndex) => { const index = Math.max(0, attempts.length - visibleTurns) + localIndex; const notes = formulaNotes(turn.response); return <div className="dax-turn" key={turn.id ?? index}>
            <div className="dax-turn-goal"><span>{turn.goal}</span></div>
            <div className="dax-turn-answer"><div className="chat-message-author"><span className="qwen-mark" aria-hidden="true"><Sparkles size={15} /></span><strong>Local AI</strong></div>
              <IncompleteResponse turn={turn} />
              <CalculationOutcome turn={turn} editorRetained={turn.editorRetained && turn.formula?.expression !== expression} />
              {turn.kind === "formula" && (!turn.status || turn.status === "completed") && turn.formula ? <>
                <pre><code>{turn.formula.name ? `${turn.formula.name} = ${turn.formula.expression}` : turn.formula.expression}</code></pre>
                <CheckStatus status={notes.status} />
                {turn.created
                  ? <div className="dax-turn-actions"><AsyncActionButton status="success" label={importedMeasures ? "Queue measure" : "Create measure"} pendingLabel={importedMeasures ? "Queueing…" : "Saving…"} successLabel={importedMeasures ? "Queued" : "Saved"} retryLabel={importedMeasures ? "Retry queue" : "Retry save"} icon={Save} /><p className="dax-turn-created" role="status">{importedMeasures ? `Queued “${turn.created}” in Pending Power BI changes.` : <>Saved "{turn.created}" to the draft — approve it in Review & build.</>}</p></div>
                  : <div className="dax-turn-actions">
                    {turn.formula.expression === expression && result
                      ? <span className="dax-formula-in-editor"><CheckCircle2 size={14} aria-hidden="true" />In editor</span>
                      : <button type="button" className="secondary" disabled={locked || !canEvaluate} onClick={() => void previewFormula(turn.formula!)}><Play size={13} />Use formula</button>}
                    {!isDaxTableExpression(turn.formula.expression) && <>
                      <input value={nameDrafts[index] ?? turn.formula.name} placeholder="Measure name" aria-label="Measure name for this suggestion" spellCheck={false} disabled={locked || !canSaveMeasure}
                        onChange={event => { resetSaveFeedback(); setNameDrafts(value => ({ ...value, [index]: event.target.value })); }} />
                      {importedMeasures && importedTables.length > 1 && <select aria-label="Table for this measure" value={homeTable} disabled={locked} onChange={event => { resetSaveFeedback(); setQueueTable(event.target.value); }}>{importedTables.map(item => <option key={item} value={item}>{item}</option>)}</select>}
                      <AsyncActionButton status={saveFeedback?.action === turnSaveAction(saveScope, turn, nameDrafts[index] ?? turn.formula.name, homeTable) ? saveFeedback.status : "idle"} label={importedMeasures ? "Queue measure" : "Create measure"} pendingLabel={importedMeasures ? "Queueing…" : "Saving…"} successLabel={importedMeasures ? "Queued" : "Saved"} retryLabel={importedMeasures ? "Retry queue" : "Retry save"} icon={Save} disabled={locked || !canSaveMeasure || !(nameDrafts[index] ?? turn.formula.name).trim() || Boolean(importedMeasures && measureNameProblem(nameDrafts[index] ?? turn.formula.name, importedMeasures.map(item => item.Name)))}
                        title={importedMeasures ? `Queue in Pending Power BI changes${homeTable ? `, in the table ${homeTable}` : ""}. Nothing changes in your report until you apply it.` : draft ? "Add as an advanced-DAX proposal on the draft" : "Create a draft before saving measures"}
                        onClick={() => void createMeasure(index)} />
                      {saveFeedback?.status === "error" && saveFeedback.action === turnSaveAction(saveScope, turn, nameDrafts[index] ?? turn.formula.name, homeTable) && <p className="dax-action-error" role="alert">{saveFeedback.failure}</p>}
                    </>}
                  </div>}
                {!importedMeasures && !turn.created && !draft && !isDaxTableExpression(turn.formula.expression) && <p className="dax-lab-hint">Create a draft to save measures.</p>}
                {notes.notes && <RichText text={notes.notes} />}
                {notes.references && <details className="dax-ai-details"><summary>References</summary><RichText text={notes.references} /></details>}
              </> : <>
                <RichText text={responseParts(turn.response)[0] || turn.error || "No response was completed."} />
                {turn.error && turn.response && <p className="dax-chat-recovery">{turn.error}</p>}
                {responseParts(turn.response).length > 1 && <details className="dax-ai-details"><summary>References</summary><RichText text={responseParts(turn.response).slice(1).join("\n\n")} /></details>}
                {turn.calculationRequest && (!turn.status || turn.status === "completed") && <div className="dax-turn-actions"><p>{turn.calculationRequest}</p><button type="button" className="secondary dax-ask-button" disabled={locked} onClick={() => void ask("generate", turn.calculationRequest)}>Draft formula</button></div>}
              </>}
              <CopyDaxAnswer text={turn.response || turn.error || ""} />
            </div>
          </div>; })}
          {pendingRequest && <div className="dax-turn">{!attempts.some(turn => turn.id === pendingRequest.requestId) && <div className="dax-turn-goal"><span>{pendingRequest.goal}</span></div>}
            <div className="dax-turn-answer">{assisting ? <ResponseActivity label={assistantStatus || "Preparing a response"} /> : <div className="dax-chat-recovery" role="status">{!attempts.some(turn => turn.id === pendingRequest.requestId) && <p>{askError || assistantStatus || "Response interrupted. Retry when you’re ready."}</p>}<p>Retry uses the original formula and request context.</p><button type="button" className="secondary" disabled={locked} onClick={() => void ask(pendingRequest.mode, pendingRequest.goal, pendingRequest)}><RotateCcw size={14} />Retry</button></div>}</div>
          </div>}
          {!pendingRequest && askError && <div className="dax-lab-error" role="alert"><AlertTriangle size={16} /><span>{askError}</span></div>}
        </div>)}
        {awayFromLatest && !archiveOpen && !archivedView && <button type="button" className="dax-jump-latest secondary" onClick={() => { following.current = true; setAwayFromLatest(false); threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight }); }}><ArrowDown size={14} />Latest messages</button>}
        {!archiveOpen && !archivedView && <form id="dax-assistant" className="dax-chat-composer" aria-label="DAX AI assistant" onSubmit={event => { event.preventDefault(); void ask(); }}>
          <div className="chat-input-box"><label className="sr-only" htmlFor="dax-goal">Message DAX AI</label>
          <textarea ref={composerRef} id="dax-goal" aria-label="Message DAX AI" value={goal} onChange={event => setGoal(event.target.value)} maxLength={1000} rows={3} placeholder={attempts.length ? "Ask a follow-up or refine the calculation…" : "What would you like to calculate?"} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void ask(); } }} />
          <div className="chat-input-actions"><span className="chat-local-label">DAX Lab only · {snapshot ? "Your fields are included" : nativeModel ? "Native model · AI needs imported rows" : "No data imported"}</span>{assisting ? <button key="stop" type="button" className="chat-send-button chat-stop-button" aria-label="Stop DAX response" title="Stop response" onClick={() => assistantAbort.current?.abort()}><Square size={14} fill="currentColor" /></button> : <button key="send" type="submit" className="chat-send-button" aria-label="Send DAX message" title="Send message" disabled={locked || !snapshot || !goal.trim()}><ArrowUp size={19} /></button>}</div></div>
          <p className="dax-chat-hint">Formulas update the scratchpad and preview. Save or queue them separately.</p>
        </form>}
      </section>;

  return <><section className={`content-page dax-lab${externalConversation ? " external-conversation" : ""}`} aria-label="DAX lab">
    <div className="dax-lab-body with-schema">
      {!externalConversation && conversation}
      <aside id="dax-schema" className="dax-lab-schema" aria-label="Tables, columns, and measures">
        <div className="dax-schema-head"><h2>Schema</h2>{tables.length > 0 && <span>{tables.length} {tables.length === 1 ? "table" : "tables"}</span>}</div>
        {tables.length > 0 && <label className="dax-schema-search"><Search size={13} aria-hidden="true" />
          <input value={schemaQuery} onChange={(event) => setSchemaQuery(event.target.value)} placeholder="Filter fields and measures" aria-label="Filter fields and measures" spellCheck={false} />
          {schemaQuery && <button type="button" className="dax-schema-clear" aria-label="Clear filter" onClick={() => setSchemaQuery("")}><X size={12} /></button>}
        </label>}
        {selectedField && <div className="dax-selected-field"><strong>{selectedField.name}</strong><small>{selectedField.table}</small><button type="button" className="secondary" disabled={locked} onClick={() => insertAtCursor(selectedField.ref)}>Insert into formula</button></div>}
        {!snapshot && <p className="dax-lab-hint">{nativeModel ? "Native queries use this report's Power BI Desktop working copy. Open it and complete its source sign-in if needed. AI formula assistance needs a saved-data snapshot." : "Import data on the Data tab to evaluate expressions."}</p>}
        {tables.map((entry) => {
          const columns = entry.table.columns.filter((column) => matchesSchema(schemaQuery, column.name, entry.table.name));
          if (schemaQuery && !columns.length) return null;
          return <details key={`${entry.id}-${schemaQuery ? "filtered" : "all"}`} className="dax-lab-table" open={Boolean(schemaQuery) || tables.length <= 3}>
            <summary><ChevronRight className="dax-schema-chevron" size={14} aria-hidden="true" /><Table2 className="dax-schema-icon" size={14} aria-hidden="true" /><span className="dax-schema-name">{entry.table.name}</span><span className="dax-lab-rows" title={`${entry.table.columns.length} columns`}>{entry.table.row_count.toLocaleString()} rows</span></summary>
            <ul>{columns.map((column) => <li key={column.id}>
              <button type="button" disabled={locked} onClick={() => setSelectedField({ table: entry.table.name, name: column.name, ref: `'${entry.table.name.replaceAll("'", "''")}'[${column.name.replaceAll("]", "]]")}]` })} title={`Select '${entry.table.name}'[${column.name}] · ${column.data_type}`}>
                <FieldTypeIcon type={column.data_type} /><span className="dax-field-name">{column.name}</span><span className="sr-only">{column.data_type}</span>
              </button>
            </li>)}</ul>
          </details>;
        })}
        {importedMeasures && importedMeasures.length > 0 && (() => {
          const shown = importedMeasures.filter((measure) => matchesSchema(schemaQuery, measure.Name, measure.TableName));
          return shown.length > 0 && <details key={`original-${schemaQuery ? "filtered" : "all"}`} className="dax-lab-table dax-lab-measure-group" open>
            <summary><ChevronRight className="dax-schema-chevron" size={14} aria-hidden="true" /><Sigma className="dax-schema-icon" size={14} aria-hidden="true" /><span className="dax-schema-name">Original measures</span><span className="dax-lab-rows">{shown.length}</span></summary>
            <ul className="dax-lab-measures">{shown.map(measure => <li key={`${measure.TableName}.${measure.Name}`}>
              <button type="button" disabled={locked} onClick={() => void previewFormula({ expression: `[${measure.Name.replaceAll("]", "]]")}]`, name: measure.Name })} title={`Preview ${measure.Name}: ${measure.Expression}`}><Sigma className="dax-field-icon" size={13} aria-hidden="true" /><span className="dax-field-name">{measure.Name}</span><span className="dax-measure-tag dax">DAX</span></button>
            </li>)}</ul>
          </details>;
        })()}
        {measures.length > 0 && (() => {
          const shown = measures.filter((measure) => matchesSchema(schemaQuery, measure.name));
          return shown.length > 0 && <details key={`measures-${schemaQuery ? "filtered" : "all"}`} className="dax-lab-table dax-lab-measure-group" open>
            <summary><ChevronRight className="dax-schema-chevron" size={14} aria-hidden="true" /><Sigma className="dax-schema-icon" size={14} aria-hidden="true" /><span className="dax-schema-name">Measures</span><span className="dax-lab-rows">{shown.length}</span></summary>
            <ul className="dax-lab-measures">{shown.map((measure) => <li key={measure.id}>
              <button type="button" disabled={locked} onClick={() => void previewFormula({ expression: `[${measure.name.replaceAll("]", "]]")}]`, name: measure.name })} title={measure.tier === "advanced_dax" ? `${measure.name}: ${measure.expression}` : `${measure.name} · governed ${measure.operation.replaceAll("_", " ")}`}>
                <Sigma className="dax-field-icon" size={13} aria-hidden="true" /><span className="dax-field-name">{measure.name}</span><span className={`dax-measure-tag${measure.tier === "advanced_dax" ? " dax" : ""}`}>{measure.tier === "advanced_dax" ? "DAX" : OPERATION_TAGS[measure.operation] ?? measure.operation.replaceAll("_", " ")}</span>
              </button>
            </li>)}</ul>
          </details>;
        })()}
        {schemaQuery && !tables.some((entry) => entry.table.columns.some((column) => matchesSchema(schemaQuery, column.name, entry.table.name)))
          && !measures.some((measure) => matchesSchema(schemaQuery, measure.name))
          && <p className="dax-lab-hint">No fields or measures match “{schemaQuery.trim()}”.</p>}
      </aside>
      <div className="dax-lab-main" aria-label="Formula and preview">
        {proposed && <section ref={suggestionRef} tabIndex={-1} className="dax-suggestion" aria-label="Suggested DAX formula"><header><strong>{suggestionLabel}</strong><button type="button" className="icon-button" aria-label="Dismiss DAX suggestion" onClick={onDismissSuggestion}><X size={16} /></button></header><pre><code>{proposed.expression}</code></pre><footer><span>Review before running. Your current formula is kept until you use this suggestion.</span><button type="button" className="secondary" disabled={locked || !canEvaluate || !proposed.expression || proposed.expression.length > 4000} onClick={() => { void previewFormula({ ...proposed, name: proposed.name || measureHeaderName(proposed.expression) || "" }); onDismissSuggestion(); requestAnimationFrame(() => editorRef.current?.focus()); }}>Use formula</button></footer>{proposed.expression.length > 4000 && <p role="alert">This suggestion exceeds the 4,000-character editor limit.</p>}</section>}
        <div className="dax-editor-heading"><h2>Formula</h2><div className="dax-formula-actions">
          {expression.trim() && <button type="button" className="ai-action" disabled={locked || !snapshot || joinedReferences || Boolean(error)} title="Explain this formula here in DAX Lab" onClick={() => void ask("explain", "Explain this formula and its filter context briefly.")}><Sparkles size={15} aria-hidden="true" />Explain</button>}
          {error && <button type="button" className="ai-action" disabled={locked || !snapshot} onClick={() => void ask("fix", `Fix the current formula using the local evaluation error.${attempts.length ? ` Intended calculation: ${attempts[attempts.length - 1].goal.slice(0, 700)}` : " Preserve its intended calculation."}`)}><Sparkles size={15} aria-hidden="true" />Fix error</button>}
          {expression && <button type="button" className="secondary" disabled={locked} onClick={() => { setPreviousFormula({ expression, name: saveName, result, error }); updateExpression(""); }}>Clear</button>}
          <button type="button" className="secondary" title="Ctrl + Enter" onClick={() => void run()} disabled={locked || !expression.trim() || !canEvaluate}>{running ? <Loader2 size={15} className="spin" /> : <Play size={15} />}Run</button>
        </div></div>
        {!expression.trim() && <div className="dax-lab-examples" aria-label="Example expressions">
          {daxExamples(snapshot).map((item) => <button key={item.label} type="button" disabled={locked} onClick={() => updateExpression(item.expression)}>{item.label}</button>)}
        </div>}
        <textarea
          ref={editorRef}
          className="dax-lab-editor"
          value={expression}
          disabled={locked}
          maxLength={4000}
          onChange={onExpressionChange}
          onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); void run(); } }}
          placeholder="SUM('Orders'[Sales]) or EVALUATE 'Orders' — Ctrl+Enter to run"
          spellCheck={false}
          autoComplete="off"
          aria-label="DAX expression"
          rows={4}
        />
        {previousFormula && <button type="button" className="dax-undo-suggestion" disabled={locked} onClick={() => { updateExpression(previousFormula.expression); setSaveName(previousFormula.name); setResult(previousFormula.result); setError(previousFormula.error); setPreviousFormula(undefined); }}><Undo2 size={14} />Undo formula change</button>}
        {joinedReferences && !error && <p className="dax-lab-error" role="alert">These expressions need an operator between them. Select a measure to preview it on its own.</p>}
        {error && <div className="dax-lab-error" role="alert"><AlertTriangle size={16} /><span>{error}</span></div>}
        {savedNote && <div className="dax-lab-saved" role="status"><CheckCircle2 size={16} /><span>{savedNote}</span></div>}
        {importedMeasures && <p className="dax-lab-hint">Power BI Desktop engine · Working-copy model context · Original report unchanged.</p>}
        {importedMeasures && !isQuery && result && <label className="dax-native-group">View by<select aria-label="Native preview grouping" value={nativeGroup} disabled={locked} onChange={async event => {
          const id = event.target.value;
          setRunning(true); setError(undefined);
          try {
            const field = nativeFields.find(c => c.id === id);
            setResult(await onRun(field ? nativeBreakdownQuery(expression, field.table, field.name) : expression));
            setNativeGroup(id);
          } catch (reason) { setError(reason instanceof Error ? reason.message : "The grouped preview failed."); }
          finally { setRunning(false); }
        }}><option value="">Overall total</option>{tables.map(t => <optgroup key={t.id} label={t.table.name}>{nativeFields.filter(c => c.tableId === t.id).map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</optgroup>)}</select><small>Preview only · Formula unchanged · Model relationships applied</small></label>}
        {result ? <DaxPreview key={expression} result={result} snapshot={snapshot} expression={expression} workspaceId={workspaceId} disabled={locked} /> : !error && <div className="dax-results-empty"><BarChart3 size={24} /><strong>{running ? "Preparing your preview…" : "Your results will appear here"}</strong><span>Generate a calculation or run your own formula to see a value, chart, or table.</span></div>}
        {importedMeasures && !isQuery && expression.trim() && <SaveImportedMeasure tables={importedTables} existing={importedMeasures.map(item => item.Name)} expression={expression}
          contextKey={saveScope} suggestedName={saveName.trim() || splitMeasureDefinition(expression).name} disabled={locked} onSave={(name, formula, options) => onSave(name, formula, options)} />}
        {importedMeasures && expression.trim() && <SaveImportedCalculation workspaceId={workspaceId} tables={importedTables} measureNames={importedMeasures.map(item => item.Name)}
          columnsOf={table => tables.find(item => item.table.name === table)?.table.columns.map(column => column.name) ?? []} expression={expression} isQuery={isQuery} disabled={locked} onCheck={onRun} />}
        {!importedMeasures && !isQuery && expression.trim() && <details className="dax-save-details"><summary><Save size={14} />Save as a measure</summary><div className="dax-lab-save">
          <input value={saveName} onChange={(event) => { resetSaveFeedback(); setSaveName(event.target.value); }} placeholder="Measure name, e.g. Profit Margin" aria-label="New measure name" spellCheck={false} disabled={locked} />
          <AsyncActionButton status={saveFeedback?.action === editorSaveAction ? saveFeedback.status : "idle"} label="Save to draft" pendingLabel="Saving…" successLabel="Saved" retryLabel="Retry save" icon={Save} onClick={() => void save()} disabled={locked || !draft || !saveName.trim() || !expression.trim() || isQuery}
            title={draft ? "Add as an advanced-DAX proposal on the draft" : "Create a draft before saving measures"} />
          {!draft && <span className="dax-lab-hint">Create a draft to save measures.</span>}
        </div>
        <p className="dax-lab-hint">Saved measures require review before building.</p></details>}
      </div>
    </div>
  </section>{externalConversation && conversationHost ? createPortal(conversation, conversationHost) : null}</>;
}
