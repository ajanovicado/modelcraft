import type { ReportAction } from "./types";
import { memoryDetail, type ModelReadiness } from "./memoryStatus";
import { ModelInstallCard, type ModelInstallState } from "./ModelInstallCard";
import { DataWorkbench } from "./DataWorkbench";
import { ImportedReportWorkspace, type ImportedReport } from "./ImportedReport";
import { EditChatCards } from "./EditChatCards";
import { useChatEdits } from "./useChatEdits";
import type { ImportedEditing } from "./useImportedEditing";
import { loadAllEvents, pendingStore } from "./importedEditing";
import { measureHomeTables } from "./daxMeasureSave";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SettingsWithKnowledge } from "./KnowledgePanel";
import { open } from "@tauri-apps/plugin-dialog";
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  Boxes,
  Check,
  CheckCircle2,
  ChevronDown,
  Database,
  Download,
  FileBarChart2,
  FileSpreadsheet,
  FolderOpen,
  History,
  LayoutDashboard,
  Link2,
  LoaderCircle,
  MemoryStick,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Settings,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { api, addDaxMeasure, browseDirectory, clearChat, deleteWorkspace, emptyHash, evaluateDax, idempotencyKey, relinkBuild, renameWorkspace, reportThemes, reviewAdvancedDax, setWorkspaceFolder, streamChat, streamDaxAssistant, updateDashboard, type DaxAssistInput } from "./api";
import { buildWorkspaceHistoryItems, loadAllMessages, partitionMessages } from "./chatHistory";
import { useWorkspaceChatDraft } from "./useWorkspaceChatDraft";
import { useWorkspaceDaxHandoff } from "./useWorkspaceDaxHandoff";
import { clearChatAttempt, readChatAttempt, saveChatAttempt, type ChatAttempt } from "./chatRecovery";
import { clearWorkspaceChatStorage } from "./workspaceChatStorage";
import { compareDrafts } from "./draftComparison";
import { DashboardDesigner } from "./DashboardDesigner";
import { ModelWorkbench, type ModelWorkbenchMeasure, type ModelWorkbenchReviewState } from "./ModelWorkbench";
import { DaxLab } from "./DaxLab";
import { daxConversationsFromEvents } from "./daxConversation";
import {
  CommandPalette,
  DeleteWorkspaceDialog,
  EvidenceScreen,
  HeaderCommandButton,
  InitialLoading,
  ProductNotice,
  RenameWorkspaceDialog,
  RuntimeStatus,
  WorkspaceMenu,
  WorkspaceTrigger,
  type CommandAction,
} from "./ProductUI";
import { UnifiedWorkspace, ReviewDialog, ReviewChecklist, PathPrompt, type AuthoringView, type ConversationContext } from "./UnifiedWorkspace";
import { DesignSummary } from "./DesignSummary";
import { ReportPaletteContext } from "./reportPalette";
import { EXECUTIVE_OVERVIEW_PROMPT, buildReviewChecklist, canApproveDraft, defaultRelationshipSelection, reviewBlockReason } from "./reviewChecklist";
import "./unified-workspace.css";
import { ChatConversation, type ChatDaxHandoff, type ChatOutcome } from "./ChatConversation";
import type { ConversationHistoryItem, ConversationHistorySelection } from "./ConversationHistory";
import type { Build, Compatibility, Draft, DraftRevisionReview, ExportReceipt, GovernedMeasure, Health, InventoryItem, Measure, Message, OpenResult, ReportThemePalette, Snapshot, StudioEvent, UndoReceipt, Workspace } from "./types";

type Screen = "studio" | "model" | "designer" | "data" | "history" | "evidence" | "settings";

const formatBytes = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
};

function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Private mode / quota failures should not break the studio shell.
  }
}

const localDate = (value: string) => new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
// The whole activity log (the server sends 250 events per request, oldest first).
const loadEvents = (workspaceId: string) => loadAllEvents((after) => api<StudioEvent[]>(`/v1/workspaces/${workspaceId}/events?after=${after}`));
const loadMessages = (workspaceId: string) => loadAllMessages(after => api<Message[]>(`/v1/workspaces/${workspaceId}/messages?after=${after}&limit=200`));
const eventsForSnapshot = (events: StudioEvent[], snapshotId?: string) => {
  if (!snapshotId) return events;
  let boundary = -1;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.type === "ingestion.completed" && event.payload.snapshot_id === snapshotId) {
      boundary = index;
      break;
    }
  }
  return boundary >= 0 ? events.slice(boundary) : [];
};

// Unique values alone do not make a key: a unique Product name or List Price is a
// description. Mirrors key_like_name in the relationship engine.
const KEY_NAME_TOKENS = new Set(["id", "key", "code", "number", "num", "no", "sk", "identifier"]);
const isKeyName = (name: string) => KEY_NAME_TOKENS.has((name.replace(/_/g, " ").match(/[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+/g) ?? []).at(-1)?.toLowerCase() ?? "");

const daxTable = (name: string) => `'${name.replaceAll("'", "''")}'`;
const daxMeasure = (id: string | undefined, measures: Map<string, Measure>) => id ? `[${measures.get(id)?.name || id}]` : "[Missing measure]";

function governedExpression(measure: GovernedMeasure, measures: Map<string, Measure>, tableName: string, columnNames: Map<string, string>) {
  const table = daxTable(tableName);
  const field = (id: string | undefined, fallback: string) => `${table}[${id ? columnNames.get(id) || id : fallback}]`;
  const column = field(measure.column_id, "Missing column");
  const date = field(measure.date_column_id, "Missing date");
  const numerator = daxMeasure(measure.numerator_measure_id, measures);
  const denominator = daxMeasure(measure.denominator_measure_id, measures);
  const base = daxMeasure(measure.base_measure_id, measures);
  if (measure.operation === "sum") return `SUM ( ${column} )`;
  if (measure.operation === "count") return measure.column_id ? `COUNTA ( ${column} )` : `COUNTROWS ( ${table} )`;
  if (measure.operation === "distinct_count") return `DISTINCTCOUNT ( ${column} )`;
  if (measure.operation === "average") return `AVERAGE ( ${column} )`;
  if (measure.operation === "minimum") return `MIN ( ${column} )`;
  if (measure.operation === "maximum") return `MAX ( ${column} )`;
  if (measure.operation === "ratio" || measure.operation === "margin") return `DIVIDE ( ${numerator}, ${denominator} )`;
  if (measure.operation === "variance") return `${numerator} - ${denominator}`;
  if (measure.operation === "weighted_average") {
    const weight = field(measure.weight_column_id, "Missing weight");
    return `DIVIDE ( SUMX ( ${table}, ${column} * ${weight} ), SUM ( ${weight} ) )`;
  }
  if (measure.operation === "running_total") return `CALCULATE ( ${base}, FILTER ( ALLSELECTED ( ${date} ), ${date} <= MAX ( ${date} ) ) )`;
  const periodFunction = measure.period === "year" ? "TOTALYTD" : measure.period === "quarter" ? "TOTALQTD" : "TOTALMTD";
  if (measure.operation === "period_to_date") return `${periodFunction} ( ${base}, ${date} )`;
  return `DIVIDE ( ${base} - CALCULATE ( ${base}, DATEADD ( ${date}, -1, ${measure.period.toUpperCase()} ) ), CALCULATE ( ${base}, DATEADD ( ${date}, -1, ${measure.period.toUpperCase()} ) ) )`;
}

function governedDependencies(measure: GovernedMeasure) {
  return [
    measure.column_id,
    measure.weight_column_id,
    measure.date_column_id,
    measure.base_measure_id,
    measure.numerator_measure_id,
    measure.denominator_measure_id,
  ].filter((item): item is string => Boolean(item));
}

export default function App() {
  const [initializing, setInitializing] = useState(true);
  const [health, setHealth] = useState<Health>();
  const [modelSetupState, setModelSetupState] = useState<ModelInstallState>();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspace, setWorkspace] = useState<Workspace>();
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [draft, setDraft] = useState<Draft>();
  const [build, setBuild] = useState<Build>();
  const [themes, setThemes] = useState<ReportThemePalette[]>([]);
  const [builds, setBuilds] = useState<Build[]>([]);
  const [events, setEvents] = useState<StudioEvent[]>([]);
  // Chat receipts are read from the whole log: an imported report has no "ingestion.completed" event to start the snapshot's slice at.
  const [allEvents, setAllEvents] = useState<StudioEvent[]>([]);
  const storeEvents = useCallback((list: StudioEvent[], snapshotId?: string) => { setAllEvents(list); setEvents(eventsForSnapshot(list, snapshotId)); }, []);
  const daxConversations = useMemo(() => daxConversationsFromEvents(allEvents, snapshot?.id), [allEvents, snapshot?.id]);
  const [inventory, setInventory] = useState<InventoryItem[]>([]);
  const [compatibility, setCompatibility] = useState<Compatibility>();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [messages, setMessages] = useState<Message[]>([]);
  const [approvedRelationships, setApprovedRelationships] = useState<Set<string>>(new Set());
  const [selectedModelMeasureId, setSelectedModelMeasureId] = useState<string>();
  const [screen, setScreen] = useState<Screen>("studio");
  const [authoringView, setAuthoringView] = useState<AuthoringView>("report");
  const [conversationOpen, setConversationOpen] = useState(true);
  const [daxConversationOpen, setDaxConversationOpen] = useState(true);
  const visibleConversation = authoringView === "dax" ? daxConversationOpen : conversationOpen;
  const [daxConversationHost, setDaxConversationHost] = useState<HTMLDivElement | null>(null);
  const [workspaceHistorySelection, setWorkspaceHistorySelection] = useState<ConversationHistorySelection>();
  const [daxHistorySelection, setDaxHistorySelection] = useState<ConversationHistorySelection>();
  const historyNavigation = useRef(0);
  const [daxHistory, setDaxHistory] = useState<{ key: string; items: ConversationHistoryItem[] }>();
  const [attachedContext, setAttachedContext] = useState<ConversationContext>();
  const [reportSelection, setReportSelection] = useState<ConversationContext>();
  const [reportAction, setReportAction] = useState<ReportAction & { requestId: string; workspaceId: string }>();
  const [reviewOpen, setReviewOpen] = useState(false);
  const [designerDirty, setDesignerDirty] = useState(false);
  const [designerToolbar, setDesignerToolbar] = useState<HTMLDivElement | null>(null);
  const [daxSuggestion, setDaxSuggestion] = useState<{ code: string; workspaceId: string; snapshotId?: string; label?: string }>();
  const { handoff: daxHandoff, setHandoff: setDaxHandoff, clearHandoff: clearDaxHandoff, storageFailed: daxHandoffStorageFailed } = useWorkspaceDaxHandoff(workspace?.id, snapshot?.id);
  const openDaxSuggestion = useCallback((code: string, label?: string, source?: ChatDaxHandoff) => {
    if (!workspace || (!snapshot && (!workspace.imported_report_id || source))) return;
    setDaxSuggestion({ code, label, workspaceId: workspace.id, snapshotId: snapshot?.id });
    setDaxHandoff(source && snapshot ? { source, workspaceId: workspace.id, snapshotId: snapshot.id, sourceView: authoringView === "dax" ? "report" : authoringView } : undefined);
    setDaxHistorySelection({ requestId: ++historyNavigation.current });
    setAuthoringView("dax");
    setScreen("studio");
    setDaxConversationOpen(true);
  }, [workspace, snapshot, authoringView, setDaxHandoff]);
  const [undoDraftHash, setUndoDraftHash] = useState<string>();
  const goView = (view: AuthoringView) => { setAuthoringView(view); setScreen("studio"); };
  const openConversation = useCallback(() => {
    if (authoringView === "dax") setDaxConversationOpen(true);
    else setConversationOpen(true);
    setScreen("studio");
    requestAnimationFrame(() => authoringView === "dax" ? daxConversationHost?.querySelector<HTMLTextAreaElement>("#dax-goal")?.focus() : composerRef.current?.focus());
  }, [authoringView, daxConversationHost]);
  const attachContext = (context: ConversationContext) => {
    setAttachedContext(context);
    openConversation();
  };
  useEffect(() => {
    if (screen === "data" || screen === "model" || screen === "designer") {
      setAuthoringView(screen === "designer" ? "report" : screen); setScreen("studio");
    }
    if (screen === "evidence") setScreen("history");
  }, [screen]);
  const { value: chatInput, setValue: setChatInput, forgetWorkspace: forgetWorkspaceDraft, storageFailed: chatDraftStorageFailed } = useWorkspaceChatDraft(workspace?.id);
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();
  const [streamed, setStreamed] = useState("");
  const [chatStreaming, setChatStreaming] = useState(false);
  const [chatActivity, setChatActivity] = useState<string>();
  const [sidecarOnline, setSidecarOnline] = useState(false);
  const [engineFeatures, setEngineFeatures] = useState<string[]>([]);
  const [discussionOnly, setDiscussionOnly] = useState(false);
  const [importedReport, setImportedReport] = useState<ImportedReport>();
  // Where DAX Lab's "Save as a measure" can put a measure of an imported report (tables that already hold measures first).
  const importedMeasureTables = useMemo(() => importedReport ? measureHomeTables(importedReport.tables, importedReport.measures) : [], [importedReport]);
  // Editing an imported report: the report editor publishes its session here so the conversation's Undo goes through the same
  // dialogs and queue, and a change made in chat or in the editor re-reads the report payload (new head, delivery state).
  const editingRef = useRef<ImportedEditing | null>(null);
  const [editingBusy, setEditingBusy] = useState(false);
  // Selecting on the canvas retargets the conversation: an Ask-AI chip attached earlier must not win over what is selected now.
  const selectReportVisual = useCallback((context?: ConversationContext) => { setReportSelection(context); if (context?.visual_id) setAttachedContext(undefined); }, []);
  const refreshImportedReport = useCallback(async () => {
    if (!workspace?.imported_report_id) return;
    const next = await api<ImportedReport>(`/v1/workspaces/${workspace.id}/imported-report`).catch(() => undefined);
    if (next) setImportedReport(next);
  }, [workspace?.id, workspace?.imported_report_id]);
  const chatEdits = useChatEdits({ workspaceId: workspace?.id, headId: importedReport?.head?.id, refresh: refreshImportedReport, undo: () => editingRef.current?.undo(),
    submit: (operations, label) => editingRef.current?.submit(operations, { label }), events: allEvents });
  const [pathPrompt, setPathPrompt] = useState<{ title: string; description: string; placeholder?: string; confirmLabel?: string; startPath?: string; acceptPbix?: boolean; reportOnly?: boolean; resolve: (value: string | null) => void }>();
  const requestPath = (spec: { title: string; description: string; placeholder?: string; confirmLabel?: string; startPath?: string; acceptPbix?: boolean; reportOnly?: boolean }) =>
    new Promise<string | null>((resolve) => setPathPrompt({ ...spec, resolve }));
  const settlePathPrompt = (value: string | null) => {
    const prompt = pathPrompt;
    setPathPrompt(undefined);
    prompt?.resolve(value);
  };
  const chatRequestRef = useRef<{ signature: string; key: string } | undefined>(undefined);
  const refreshGenerationRef = useRef(0);
  useEffect(() => {
    let alive = true;
    const check = () => void api<{ ready: boolean; features?: string[] }>("/v1/ping")
      .then(value => { if (alive) { setSidecarOnline(true); setEngineFeatures(value.features || []); } })
      .catch(() => { if (alive) setSidecarOnline(false); });
    check();
    const timer = window.setInterval(check, 15000);
    return () => { alive = false; window.clearInterval(timer); };
  }, []);
  const [chatOutcome, setChatOutcome] = useState<ChatOutcome>();
  const [chatFailure, setChatFailure] = useState<string>();
  const [failedChatRequest, setFailedChatRequest] = useState<ChatAttempt>();
  const [chatRecoveryStorageFailed, setChatRecoveryStorageFailed] = useState(false);
  // An unavailable model (still starting, a stopped runtime) must recover without a reload.
  // RAM and power never pause local AI, so there is no memory-specific recovery path.
  const modelRecoveryNeeded = Boolean(health && !health.model_available);
  useEffect(() => {
    if (!modelRecoveryNeeded || busy) return;
    let alive = true;
    let checking = false;
    const controller = new AbortController();
    const check = async () => {
      if (checking || document.visibilityState === "hidden") return;
      checking = true;
      try {
        const status = await api<ModelReadiness>("/v1/model-health", { signal: controller.signal });
        if (!alive) return;
        setHealth(previous => previous ? {
          ...previous, status: status.model_available ? "ready" : "degraded",
          model_available: status.model_available, model_error: status.error ?? undefined,
          available_memory_gb: status.available_memory_gb,
          model_name: status.identity?.name ?? previous.model_name,
          model_digest: status.identity?.digest ?? previous.model_digest,
          total_memory_gb: status.total_memory_gb,
        } : previous);
      } catch { /* Preserve the last diagnostic while the local engine reconnects. */ }
      finally { checking = false; }
    };
    void check();
    const timer = window.setInterval(() => void check(), 15000);
    document.addEventListener("visibilitychange", check);
    window.addEventListener("focus", check);
    return () => {
      alive = false;
      controller.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", check);
      window.removeEventListener("focus", check);
    };
  }, [modelRecoveryNeeded, busy]);
  const [exportedPath, setExportedPath] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [commandOpen, setCommandOpen] = useState(false);
  const [workspaceMenuOpen, setWorkspaceMenuOpen] = useState(false);
  const [workspaceAdmin, setWorkspaceAdmin] = useState<{ mode: "rename" | "delete"; target: Workspace }>();
  const [navCollapsed, setNavCollapsed] = useState(() => readStorage("studio.navCollapsed") === "true");


  const [revisionReview, setRevisionReview] = useState<DraftRevisionReview>();
  const abortRef = useRef<AbortController | undefined>(undefined);
  const conversationOperationRef = useRef<symbol | undefined>(undefined);
  const databaseWorkspacePending = useRef(false);
  const databaseWorkspaceRequest = useRef<{ name: string; idempotency_key: string; expected_base_hash: string } | undefined>(undefined);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  // A synchronous lease closes the gap before React renders a disabled button.
  // Only its owner may publish results or release the conversation's busy state.
  const claimConversationOperation = () => {
    if (conversationOperationRef.current) return;
    const token = Symbol("conversation operation");
    conversationOperationRef.current = token;
    return {
      current: () => conversationOperationRef.current === token,
      release: () => { if (conversationOperationRef.current === token) conversationOperationRef.current = undefined; },
    };
  };
  useEffect(() => () => {
    conversationOperationRef.current = undefined;
    abortRef.current?.abort();
    abortRef.current = undefined;
  }, []);

  const clearWorkspaceState = useCallback(() => {
    conversationOperationRef.current = undefined;
    abortRef.current?.abort();
    abortRef.current = undefined;
    setChatStreaming(false);
    setWorkspaceHistorySelection(undefined);
    setDaxHistorySelection(undefined);
    setDaxHistory(undefined);
    setDaxSuggestion(undefined);
    setImportedReport(undefined);
    setAttachedContext(undefined);
    setReportSelection(undefined);
    setReportAction(undefined);
    setDiscussionOnly(false);
    setReviewOpen(false);
    setDesignerDirty(false);
    setUndoDraftHash(undefined);
    setSnapshot(undefined);
    setDraft(undefined);
    setBuild(undefined);
    setBuilds([]);
    setEvents([]);
    setAllEvents([]);
    setInventory([]);
    setCompatibility(undefined);
    setMessages([]);
    setApprovedRelationships(new Set());
    setSelectedModelMeasureId(undefined);
    setSelected(new Set());
    setExportedPath(undefined);
    setStreamed("");
    setChatOutcome(undefined);
    setChatFailure(undefined);
    setFailedChatRequest(undefined);
    setChatRecoveryStorageFailed(false);
    chatRequestRef.current = undefined;
    setChatActivity(undefined);


    setRevisionReview(undefined);
  }, []);

  const refreshWorkspace = useCallback(async (id: string) => {
    const generation = ++refreshGenerationRef.current;
    const value = await api<Workspace>(`/v1/workspaces/${id}`);
    const [currentSnapshot, currentDraft, currentMessages, currentBuilds, currentEvents, currentInventory, currentCompatibility, currentImportedReport] = await Promise.all([
      value.active_snapshot_id ? api<Snapshot>(`/v1/snapshots/${value.active_snapshot_id}`) : Promise.resolve(undefined),
      value.active_draft_id ? api<Draft>(`/v1/drafts/${value.active_draft_id}`) : Promise.resolve(undefined),
      loadMessages(id),
      api<Build[]>(`/v1/workspaces/${id}/builds`),
      loadEvents(id),
      value.selected_folder ? api<InventoryItem[]>("/v1/inventory", { method: "POST", body: JSON.stringify({ folder: value.selected_folder }) }).catch(() => []) : Promise.resolve([]),
      value.existing_pbip ? api<Compatibility>("/v1/compatibility", { method: "POST", body: JSON.stringify({ pbip_path: value.existing_pbip }) }).catch(() => undefined) : Promise.resolve(undefined),
      value.imported_report_id ? api<ImportedReport>(`/v1/workspaces/${id}/imported-report`) : Promise.resolve(undefined),
    ]);
    if (generation !== refreshGenerationRef.current) return;
    clearWorkspaceState();
    setImportedReport(currentImportedReport);
    setWorkspace(value);
    writeStorage("studio.lastWorkspace", value.id);
    setSnapshot(currentSnapshot);
    const currentDraftMatchesSnapshot = currentDraft && currentSnapshot && currentDraft.snapshot_id === currentSnapshot.id;
    if (currentDraftMatchesSnapshot) {
      setDraft(currentDraft);
      setApprovedRelationships(defaultRelationshipSelection(currentDraft.semantic_model.relationships));
      setSelectedModelMeasureId(currentDraft.measure_proposal.measures[0]?.id);
    }
    setMessages(currentMessages);
    try {
      const interrupted = readChatAttempt(localStorage, value.id, currentSnapshot?.id, value.chat_boundaries?.at(-1) ?? 0, currentMessages);
      if (interrupted) {
        setFailedChatRequest(interrupted);
        setChatOutcome("interrupted");
        setChatFailure("The previous response did not finish in this tab. Retry the original question when you’re ready.");
      }
    } catch { /* Recovery remains in memory if browser storage is unavailable. */ }
    setBuilds(currentBuilds);
    setBuild(
      currentDraftMatchesSnapshot
        ? currentBuilds.find((item) => item.draft_id === currentDraft.id)
        : undefined,
    );
    storeEvents(currentEvents, currentSnapshot?.id);
    setInventory(currentInventory);
    setSelected(new Set(currentInventory.filter((item) => item.selectable).map((item) => item.id)));
    setCompatibility(currentCompatibility);
  }, [clearWorkspaceState, storeEvents]);

  useEffect(() => {
    // A discarded run (React StrictMode mounts twice) must not load the workspace:
    // its late refresh replaced a workspace the user had created in the meantime.
    let cancelled = false;
    void (async () => {
      try {
        const [healthValue, workspaces] = await Promise.all([
          api<Health>("/v1/health"),
          api<Workspace[]>("/v1/workspaces"),
        ]);
        if (cancelled) return;
        setHealth(healthValue);
        setWorkspaces(workspaces);
        const remembered = readStorage("studio.lastWorkspace");
        const initial = workspaces.find((item) => item.id === remembered) || workspaces[0];
        if (initial) await refreshWorkspace(initial.id);
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "The local sidecar is unavailable");
      } finally {
        if (!cancelled) setInitializing(false);
      }
    })();
    return () => { cancelled = true; };
  }, [refreshWorkspace]);

  useEffect(() => {
    writeStorage("studio.navCollapsed", String(navCollapsed));
  }, [navCollapsed]);

  const chatPartition = useMemo(
    () => partitionMessages(messages, workspace?.chat_boundaries ?? [], snapshot?.created_at ?? ""),
    [messages, workspace, snapshot],
  );
  const historyKey = `${workspace?.id}:${snapshot?.id}`;
  const publishDaxHistory = useCallback((items: ConversationHistoryItem[]) => setDaxHistory({ key: historyKey, items }), [historyKey]);
  const sharedHistoryItems = useMemo(() => [
    ...buildWorkspaceHistoryItems(chatPartition.active, chatPartition.archived),
    ...(daxHistory?.key === historyKey ? daxHistory.items : []),
  ], [chatPartition, daxHistory, historyKey]);
  const selectSharedHistory = (id: string) => {
    const item = sharedHistoryItems.find(entry => entry.id === id);
    if (!item) return;
    const selection = { id: item.current ? undefined : item.sourceId, requestId: ++historyNavigation.current };
    setScreen("studio");
    if (item.scope === "dax") {
      setAuthoringView("dax");
      setDaxConversationOpen(true);
      setDaxHistorySelection(selection);
    } else {
      if (authoringView === "dax") setAuthoringView("report");
      setConversationOpen(true);
      setWorkspaceHistorySelection(selection);
    }
  };
  const activeDaxHandoff = daxHandoff?.workspaceId === workspace?.id && daxHandoff?.snapshotId === snapshot?.id ? daxHandoff : undefined;
  const backToSourceChat = () => {
    if (!activeDaxHandoff) return;
    const source = activeDaxHandoff.source;
    const sourceArchive = chatPartition.archived.find(chat => chat.messages.some(message => message.id === source.sourceMessageId));
    setWorkspaceHistorySelection({
      id: sourceArchive ? String(sourceArchive.messages[0]?.id ?? sourceArchive.endedAt) : source.sourceArchiveId,
      requestId: ++historyNavigation.current,
      messageId: source.sourceMessageId,
    });
    setAuthoringView(activeDaxHandoff.sourceView);
    setConversationOpen(true);
    setScreen("studio");
  };

  useEffect(() => {
    if (!notice) return;
    const timeout = window.setTimeout(() => setNotice(undefined), 4200);
    return () => window.clearTimeout(timeout);
  }, [notice]);

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing = target?.matches("input, textarea, [contenteditable='true']");
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((value) => !value);
      }
      if (!typing && (event.ctrlKey || event.metaKey) && /^[1-7]$/.test(event.key)) {
        event.preventDefault();
        const destinations: Screen[] = ["studio", "model", "designer", "data", "history", "evidence", "settings"];
        const destination = destinations[Number(event.key) - 1];
        if (destination === "studio") openConversation();
        else setScreen(destination);
      }
      if (event.key === "Escape") {
        setCommandOpen(false);
        setWorkspaceMenuOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [openConversation]);

  const importReportFile = async (path: string) => {
    setBusy("Opening the report and importing its embedded tables…");
    setError(undefined);
    try {
      const value = await api<Workspace>("/v1/reports/import", { method: "POST", body: JSON.stringify({ path, idempotency_key: idempotencyKey() }) });
      await refreshWorkspace(value.id);
      setWorkspaces(current => [value, ...current.filter(w => w.id !== value.id)]);
      goView("report");
      setNotice("Report imported. The original PBIX is preserved.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Report import failed"); }
    finally { setBusy(undefined); }
  };
  const openImportedDesktop = async () => {
    if (!workspace || busy) return;
    setBusy("Opening the edited copy in Power BI Desktop…");
    try {
      await api(`/v1/workspaces/${workspace.id}/open-source-report`, { method: "POST" });
      setNotice("Power BI Desktop is opening Studio's edited copy of your report. Your original file is not changed.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not open Power BI Desktop"); }
    finally { setBusy(undefined); }
  };
  const chooseExistingReport = async () => {
    if (designerDirty || busy || chatStreaming) return setNotice("Finish your current changes before opening a report.");
    const result = "__TAURI_INTERNALS__" in window
      ? await open({ directory: false, multiple: false, title: "Open Power BI report", filters: [{ name: "Power BI report", extensions: ["pbix"] }] })
      : await requestPath({ title: "Open Power BI report", description: "Choose a PBIX to open its pages, model and embedded data in a new workspace.", acceptPbix: true, reportOnly: true, confirmLabel: "Open report" });
    if (typeof result === "string") await importReportFile(result);
  };

  const pickDataFolder = async (target?: Workspace) => {
    const current = target || workspace;
    if (!current) return;
    if (busy || chatStreaming) return setNotice("Finish or stop the current operation before choosing a folder.");
    let folder: string | null = null;
    if ("__TAURI_INTERNALS__" in window) {
      try {
        const result = await open({ directory: true, multiple: false, title: "Choose a local data folder" });
        folder = typeof result === "string" ? result : null;
      } catch {
        setError("The native folder picker could not be opened");
        return;
      }
    } else {
      folder = await requestPath({
        title: "Choose local data",
        description: "Pick a folder in the list, or open one and pick a single data file (CSV, Excel, Parquet). Nothing is read until you import it.",
        placeholder: "C:\\Users\\you\\data or C:\\Users\\you\\orders.csv",
        confirmLabel: "Scan",
        acceptPbix: true,
        startPath: current.selected_folder,
      });
    }
    if (!folder) return;
    if (/\.pbix$/i.test(folder)) { await importReportFile(folder); return; }
    setBusy("Scanning folder signatures");
    setError(undefined);
    try {
      const baseName = folder.split(/[\\/]/).filter(Boolean).at(-1)?.replace(/\.(csv|xlsx?|xlsm|parquet|pbip)$/i, "");
      // Re-importing the same file must not produce identical entries in the workspace menu.
      const taken = new Set(workspaces.filter((item) => item.id !== current.id).map((item) => item.name.toLowerCase()));
      let autoName = baseName;
      for (let copy = 2; autoName && taken.has(autoName.toLowerCase()); copy += 1) autoName = `${baseName} (${copy})`;
      const updated = await setWorkspaceFolder(current.id, {
        workspace_id: current.id,
        folder,
        name: current.name === "New local workspace" ? autoName : undefined,
        idempotency_key: idempotencyKey(),
        expected_base_hash: current.base_hash,
      });
      // A file selection resolves to its parent folder; keep only that file checked.
      const items = await api<InventoryItem[]>("/v1/inventory", {
        method: "POST",
        body: JSON.stringify({ folder: updated.selected_folder }),
      });
      const normalize = (path: string) => path.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
      const picked = items.find((item) => normalize(item.absolute_path) === normalize(folder));
      setWorkspace(updated);
      setWorkspaces((list) => list.map((item) => (item.id === updated.id ? updated : item)));
      setInventory(items);
      setSelected(new Set((picked ? [picked] : items).filter((item) => item.selectable).map((item) => item.id)));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Folder scan failed");
    } finally {
      setBusy(undefined);
    }
  };

  const chooseFolder = async () => {
    if (designerDirty) return setNotice("Save or discard your report edits before switching workspaces.");
    if (busy || chatStreaming) return setNotice("Finish or stop the current operation before changing workspaces.");
    setError(undefined);
    if (!("__TAURI_INTERNALS__" in window)) {
      // Browser mode has no native picker: create the workspace first so the
      // blank data canvas stays available, then open the path entry dialog.
      setBusy("Creating a local workspace");
      try {
        const value = await api<Workspace>("/v1/workspaces", {
          method: "POST",
          body: JSON.stringify({
            name: "New local workspace",
            idempotency_key: idempotencyKey(),
            expected_base_hash: emptyHash,
          }),
        });
        // A refresh of the previous workspace still in flight must not land on top.
        refreshGenerationRef.current += 1;
        clearWorkspaceState();
        setWorkspace(value);
        writeStorage("studio.lastWorkspace", value.id);
        setWorkspaces((current) => [value, ...current.filter((item) => item.id !== value.id)]);
        goView("data");
        setBusy(undefined);
        void pickDataFolder(value);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Workspace could not be created");
        setBusy(undefined);
      }
      return;
    }
    let folder: string | null = null;
    try {
      const result = await open({ directory: true, multiple: false, title: "Choose a local data folder" });
      folder = typeof result === "string" ? result : null;
    } catch {
      setError("The native folder picker could not be opened");
      return;
    }
    if (!folder) return;
    setBusy("Scanning folder signatures");
    try {
      const [items, value] = await Promise.all([
        api<InventoryItem[]>("/v1/inventory", {
          method: "POST",
          body: JSON.stringify({ folder }),
        }),
        api<Workspace>("/v1/workspaces", {
          method: "POST",
          body: JSON.stringify({
            name: folder.split(/[\\/]/).filter(Boolean).at(-1) || "Local dashboard",
            selected_folder: folder,
            idempotency_key: idempotencyKey(),
            expected_base_hash: emptyHash,
          }),
        }),
      ]);
      refreshGenerationRef.current += 1;
      clearWorkspaceState();
      setWorkspace(value);
      writeStorage("studio.lastWorkspace", value.id);
      setWorkspaces((current) => [value, ...current.filter((item) => item.id !== value.id)]);
      setInventory(items);
      setSelected(new Set(items.filter((item) => item.selectable).map((item) => item.id)));
      setScreen("data");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Folder scan failed");
    } finally {
      setBusy(undefined);
    }
  };

  const startDatabaseWorkspace = async () => {
    if (busy || chatStreaming || designerDirty || conversationOperationRef.current || databaseWorkspacePending.current) return;
    databaseWorkspacePending.current = true;
    setBusy("Opening a database workspace"); setError(undefined);
    const request = databaseWorkspaceRequest.current ?? {
      name: "Database workspace",
      idempotency_key: idempotencyKey(),
      expected_base_hash: emptyHash,
    };
    databaseWorkspaceRequest.current = request;
    try {
      const value = await api<Workspace>("/v1/workspaces", { method: "POST", body: JSON.stringify(request) });
      setWorkspaces(current => [value, ...current.filter(item => item.id !== value.id)]);
      await refreshWorkspace(value.id);
      databaseWorkspaceRequest.current = undefined;
      goView("data");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The database workspace could not be opened");
    } finally { databaseWorkspacePending.current = false; setBusy(undefined); }
  };

  const rescan = async () => {
    if (!workspace?.selected_folder) return;
    setBusy("Scanning folder signatures");
    try {
      const items = await api<InventoryItem[]>("/v1/inventory", {
        method: "POST",
        body: JSON.stringify({ folder: workspace.selected_folder }),
      });
      setInventory(items);
      setSelected(new Set(items.filter((item) => item.selectable).map((item) => item.id)));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Folder scan failed");
    } finally {
      setBusy(undefined);
    }
  };

  const chooseExistingProject = async () => {
    if (designerDirty) return setNotice("Save or discard your report edits before switching workspaces.");
    if (busy || chatStreaming) return setNotice("Finish or stop the current operation before changing workspaces.");
    setError(undefined);
    let pbip: string | null = null;
    if ("__TAURI_INTERNALS__" in window) {
      try {
        const result = await open({
          directory: false,
          multiple: false,
          title: "Inspect an existing Power BI Project",
          filters: [{ name: "Power BI Project", extensions: ["pbip"] }],
        });
        pbip = typeof result === "string" ? result : null;
      } catch {
        setError("The native Power BI project picker could not be opened");
        return;
      }
    } else {
      pbip = await requestPath({
        title: "Inspect an existing Power BI Project",
        description: "Open a folder and pick its .pbip project file. The project is only inspected, never changed.",
        placeholder: "C:\\path\\to\\project.pbip",
        confirmLabel: "Inspect project",
        startPath: workspace?.selected_folder,
      });
    }
    if (!pbip) return;
    setBusy("Inspecting the existing PBIP without changing it");
    try {
      const report = await api<Compatibility>("/v1/compatibility", {
        method: "POST",
        body: JSON.stringify({ pbip_path: pbip }),
      });
      const folder = pbip.replace(/[\\/][^\\/]+$/, "");
      const [items, value] = await Promise.all([
        api<InventoryItem[]>("/v1/inventory", {
          method: "POST",
          body: JSON.stringify({ folder }),
        }),
        api<Workspace>("/v1/workspaces", {
          method: "POST",
          body: JSON.stringify({
            name: pbip.split(/[\\/]/).at(-1)?.replace(/\.pbip$/i, "") || "Existing PBIP",
            selected_folder: folder,
            existing_pbip: pbip,
            idempotency_key: idempotencyKey(),
            expected_base_hash: emptyHash,
          }),
        }),
      ]);
      clearWorkspaceState();
      setCompatibility(report);
      setWorkspace(value);
      writeStorage("studio.lastWorkspace", value.id);
      setWorkspaces((current) => [value, ...current.filter((item) => item.id !== value.id)]);
      setInventory(items);
      setSelected(new Set(items.filter((item) => item.selectable).map((item) => item.id)));
      setScreen("data");
      if (!report.supported) setError(`Migration blocked: ${report.blocked_features.join(", ")}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "PBIP compatibility inspection failed");
    } finally {
      setBusy(undefined);
    }
  };

  const ingest = async () => {
    if (!workspace) return;
    const itemPaths = inventory.filter((item) => selected.has(item.id)).map((item) => item.absolute_path);
    if (!itemPaths.length) return setError("Select at least one supported data source");
    setBusy("Preserving originals and profiling normalized snapshots");
    setError(undefined);
    try {
      const value = await api<Snapshot>("/v1/ingestions", {
        method: "POST",
        body: JSON.stringify({
          workspace_id: workspace.id,
          item_paths: itemPaths,
          idempotency_key: idempotencyKey(),
          expected_base_hash: workspace.base_hash,
        }),
      });
      setSnapshot(value);
      const refreshed = await api<Workspace>(`/v1/workspaces/${workspace.id}`);
      await refreshWorkspace(workspace.id);
      setNotice("Data imported, profiled and isolated in a new local snapshot");
      if (!workspace.existing_pbip) {
        await createExecutiveOverview(refreshed, value);
        return;
      }
      goView("data");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Local ingestion failed");
    } finally {
      setBusy(undefined);
    }
  };

  const createExecutiveOverview = async (target?: Workspace, source?: Snapshot) => {
    const currentWorkspace = target || workspace;
    const currentSnapshot = source || snapshot;
    if (!currentWorkspace || !currentSnapshot || currentWorkspace.existing_pbip) return;
    setBusy("Creating one executive overview from reviewed data");
    setError(undefined);
    try {
      const value = await api<Draft>("/v1/drafts", {
        method: "POST",
        body: JSON.stringify({
          workspace_id: currentWorkspace.id,
          snapshot_id: currentSnapshot.id,
          prompt: EXECUTIVE_OVERVIEW_PROMPT,
          idempotency_key: idempotencyKey(),
          expected_base_hash: currentWorkspace.base_hash,
        }),
      });
      setDraft(value);
      setApprovedRelationships(defaultRelationshipSelection(value.semantic_model.relationships));
      setConversationOpen(false);
      await refreshWorkspace(currentWorkspace.id);
      goView("report");
      setNotice("Executive overview is ready. Review the checklist, then build.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Executive overview could not be created");
      goView("data");
    } finally {
      setBusy(undefined);
    }
  };

  const sendChat = async (requestedMessage?: string, freshKey = false, contextOverride?: ConversationContext | null, retryAttempt?: ChatAttempt) => {
    const message = (requestedMessage ?? chatInput).trim();
    const requestContext = retryAttempt ? retryAttempt.context : contextOverride === undefined ? attachedContext ?? (authoringView === "report" ? reportSelection : undefined) : contextOverride;
    if (!workspace || !message || chatStreaming || busy) return;
    const boundaryId = workspace.chat_boundaries?.at(-1) ?? 0;
    if (retryAttempt && (retryAttempt.workspaceId !== workspace.id || retryAttempt.snapshotId !== snapshot?.id || (retryAttempt.boundaryId ?? 0) !== boundaryId)) return;
    if (designerDirty) { setNotice("Save or discard report edits before asking the AI. Your unfinished edits are preserved."); return; }
    if (requestContext && !engineFeatures.includes("chat-context")) { setError("Restart the local Python engine to enable attached context. Your message is still here."); return; }
    const operation = claimConversationOperation();
    if (!operation) return;
    setDiscussionOnly(false);
    chatEdits.beginTurn();
    setMessages((current) => [...current, { role: "user", content: message, citations: [], created_at: new Date().toISOString() }]);
    if (!retryAttempt) setChatInput("");
    setStreamed("");
    setBusy("Local AI is responding");
    setChatOutcome(undefined);
    setChatFailure(undefined);
    setFailedChatRequest(undefined);
    setError(undefined);
    const controller = new AbortController();
    abortRef.current = controller;
    setChatStreaming(true);
    let attempt: ChatAttempt | undefined;
    let streamFinished = false;
    try {
      const baseHash = retryAttempt?.baseHash ?? draft?.base_hash ?? workspace.base_hash;
      const signature = `${workspace.id}:${snapshot?.id ?? "no-data"}:${boundaryId}:${baseHash}:${message}:${JSON.stringify(requestContext)}`;
      if (retryAttempt) chatRequestRef.current = { signature, key: retryAttempt.key };
      else if (freshKey || chatRequestRef.current?.signature !== signature) chatRequestRef.current = { signature, key: idempotencyKey() };
      attempt = { workspaceId: workspace.id, snapshotId: snapshot?.id, boundaryId, message, context: requestContext ? { ...requestContext } : null, baseHash, key: chatRequestRef.current.key };
      try { setChatRecoveryStorageFailed(!saveChatAttempt(localStorage, attempt)); }
      catch { setChatRecoveryStorageFailed(true); }
      const canPublish = () => operation.current() && !controller.signal.aborted;
      const updatedDraftId = await streamChat(workspace.id, message, (token) => { if (canPublish()) setStreamed((value) => value + token); }, controller.signal, {
        idempotency_key: attempt.key,
        expected_base_hash: attempt.baseHash,
        chat_boundary: attempt.boundaryId,
        onStatus: text => { if (canPublish()) setChatActivity(text); },
        onEditEvent: event => { if (canPublish()) { chatEdits.onEditEvent(event); if (event.type === "progress") setChatActivity(event.label); } },
        onReportAction: action => { if (canPublish()) { setReportAction({ ...action, requestId: idempotencyKey(), workspaceId: workspace.id }); goView("report"); } },
        context: requestContext ? { page_id: requestContext.page_id, visual_id: requestContext.visual_id, table_id: requestContext.table_id, measure_id: requestContext.measure_id, relationship_id: requestContext.relationship_id } : undefined,
      });
      if (!operation.current()) return;
      streamFinished = true;
      setDiscussionOnly(!updatedDraftId);
      if (updatedDraftId) {
        const updated = await api<Draft>(`/v1/drafts/${updatedDraftId}`);
        if (!operation.current()) return;
        if (draft) { setRevisionReview(compareDrafts(draft, updated, message)); setUndoDraftHash(draft.base_hash); }
        setDraft(updated);
        setApprovedRelationships(defaultRelationshipSelection(updated.semantic_model.relationships));
        setBuild(undefined);
        setExportedPath(undefined);
        setNotice("Draft updated—not yet built in Power BI. Review calculations and pages, then approve and build.");
        const [updatedWorkspace, updatedEvents] = await Promise.all([
          api<Workspace>(`/v1/workspaces/${workspace.id}`),
          loadEvents(workspace.id),
        ]);
        if (!operation.current()) return;
        setWorkspace(updatedWorkspace);
        storeEvents(updatedEvents, snapshot?.id);
      }
      try { clearChatAttempt(localStorage, workspace.id, snapshot?.id); } catch { /* The live request is complete. */ }
      setChatRecoveryStorageFailed(false);
    } catch (reason) {
      if (operation.current()) {
        setChatOutcome(controller.signal.aborted ? "stopped" : "interrupted");
        setFailedChatRequest(attempt);
      }
      if (operation.current() && !controller.signal.aborted) {
        const failure = reason instanceof Error ? reason.message : "Local model chat failed";
        setChatFailure(failure);
        setError(failure);
      }
    } finally {
      // A stopped or failed stream may still have persisted a partial reply;
      // swap the live stream bubble for the stored turn in the same batch.
      if (operation.current()) {
        try {
          const stored = await loadMessages(workspace.id);
          if (operation.current()) {
            setMessages(stored);
            setStreamed("");
            const currentAttempt = attempt;
            const incomplete = currentAttempt && stored.find(item => item.role === "assistant" && item.request_id === currentAttempt.key && item.status !== undefined && item.status !== "completed");
            // Transport completion is not evidence that the model finished its answer.
            if (streamFinished && incomplete && attempt) {
              setChatOutcome(incomplete.status === "cancelled" ? "stopped" : "interrupted");
              setChatFailure("The model returned an incomplete response. Retry the original question when you’re ready.");
              setFailedChatRequest(attempt);
              try { setChatRecoveryStorageFailed(!saveChatAttempt(localStorage, attempt)); }
              catch { setChatRecoveryStorageFailed(true); }
            }
          }
        } catch { /* keep the optimistic transcript when the engine is unreachable */ }
        if (operation.current()) {
          abortRef.current = undefined;
          setChatStreaming(false);
          setChatActivity(undefined);
          chatEdits.finish();
          setBusy(undefined);
          operation.release();
          void api<Health>("/v1/health").then(setHealth).catch(() => undefined);
        }
      }
    }
  };

  const clearConversation = async () => {
    if (!workspace || chatStreaming || busy) return;
    const operation = claimConversationOperation();
    if (!operation) return;
    setBusy("Archiving this conversation");
    setError(undefined);
    try {
      const updated = await clearChat(workspace.id, {
        workspace_id: workspace.id,
        idempotency_key: idempotencyKey(),
        expected_base_hash: workspace.base_hash,
      });
      if (!operation.current()) return;
      setWorkspace(updated);
      chatEdits.reset();
      setStreamed("");
      setChatOutcome(undefined);
      setChatFailure(undefined);
      setFailedChatRequest(undefined);
      setChatRecoveryStorageFailed(false);
      try { clearChatAttempt(localStorage, workspace.id, snapshot?.id); } catch { /* In-memory recovery was cleared. */ }
      chatRequestRef.current = undefined;
      setDiscussionOnly(false);
      const cleared = (updated.chat_boundaries?.length ?? 0) > (workspace.chat_boundaries?.length ?? 0);
      setNotice(cleared ? "Started a new chat. The previous conversation stays in History." : "This conversation is already empty.");
    } catch (reason) {
      if (operation.current()) setError(reason instanceof Error ? reason.message : "The conversation could not be cleared");
    } finally {
      if (operation.current()) { setBusy(undefined); operation.release(); }
    }
  };

  const editMessage = useCallback((content: string) => {
    setChatInput(content);
    composerRef.current?.focus();
  }, [setChatInput]);

  const newDaxChat = async () => {
    if (!workspace || busy || chatStreaming) throw new Error("Finish or stop the current response before starting a new chat.");
    const operation = claimConversationOperation();
    if (!operation) throw new Error("Finish or stop the current response before starting a new chat.");
    setBusy("Saving DAX conversation");
    try {
      const updated = await api<Workspace>(`/v1/workspaces/${workspace.id}/dax/chat/clear`, {
        method: "POST",
        body: JSON.stringify({ workspace_id: workspace.id, snapshot_id: snapshot?.id ?? null, expected_base_hash: workspace.base_hash, idempotency_key: idempotencyKey() }),
      });
      const updatedEvents = await loadEvents(workspace.id);
      if (!operation.current()) throw new DOMException("The conversation context changed.", "AbortError");
      setWorkspace(updated);
      storeEvents(updatedEvents, snapshot?.id);
    } finally { if (operation.current()) { setBusy(undefined); operation.release(); } }
  };

  const approve = async () => {
    if (!workspace || !draft) return;
    setBusy("Recording reviewed semantic model and dashboard approval");
    try {
      const value = await api<Draft>(`/v1/drafts/${draft.id}/approval`, {
        method: "PUT",
        body: JSON.stringify({
          workspace_id: workspace.id,
          approved_relationship_ids: [...approvedRelationships],
          idempotency_key: idempotencyKey(),
          expected_base_hash: draft.base_hash,
        }),
      });
      setDraft(value);
      setNotice("Draft approval recorded in the local evidence trail");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Approval failed");
    } finally {
      setBusy(undefined);
    }
  };

  const saveDashboard = async (pages: Draft["pages"]) => {
    if (!workspace || !draft || busy) return;
    setBusy("Revalidating dashboard bindings and local previews");
    setError(undefined);
    try {
      const value = await updateDashboard(draft.id, {
        workspace_id: workspace.id,
        pages,
        idempotency_key: idempotencyKey(),
        expected_base_hash: draft.base_hash,
      });
      setUndoDraftHash(draft.base_hash);
      setRevisionReview(compareDrafts(draft, value, "Manual report edits"));
      setDraft(value);
      setApprovedRelationships((current) => new Set(value.semantic_model.relationships.filter((item) => item.blockers.length === 0 && (current.has(item.id) || item.review_status === "approved" || item.recommended_by_model)).map((item) => item.id)));
      setBuild(undefined);
      setExportedPath(undefined);
      const refreshedEvents = await loadEvents(workspace.id).catch(() => undefined);
      if (refreshedEvents) storeEvents(refreshedEvents, snapshot?.id);
      setNotice("Dashboard saved and revalidated. Review the updated draft before approving it for Power BI.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Dashboard changes could not be validated");
      throw reason;
    } finally {
      setBusy(undefined);
    }
  };

  const reviewDaxMeasure = async (measureId: string, decision: "approved" | "rejected") => {
    if (!workspace || !draft || busy) return;
    const approved = decision === "approved";
    setBusy(approved ? "Validating and recording advanced DAX approval" : "Removing rejected DAX and dependent visuals");
    setError(undefined);
    try {
      const value = await reviewAdvancedDax(draft.id, measureId, {
        workspace_id: workspace.id,
        draft_id: draft.id,
        measure_id: measureId,
        approved,
        approval_note: approved ? "Approved in the local Model Workbench after formula and dependency review." : "Rejected in the local Model Workbench; dependent dashboard content must not be compiled.",
        idempotency_key: idempotencyKey(),
        expected_base_hash: draft.base_hash,
      });
      setDraft(value);
      setApprovedRelationships((current) => new Set(value.semantic_model.relationships.filter((item) => item.blockers.length === 0 && (current.has(item.id) || item.review_status === "approved" || item.recommended_by_model)).map((item) => item.id)));
      setSelectedModelMeasureId((current) => value.measure_proposal.measures.some((item) => item.id === current) ? current : value.measure_proposal.measures[0]?.id);
      setBuild(undefined);
      setExportedPath(undefined);
      const refreshedEvents = await loadEvents(workspace.id).catch(() => undefined);
      if (refreshedEvents) storeEvents(refreshedEvents, snapshot?.id);
      setNotice(approved ? "Advanced DAX approved and recorded in the evidence trail." : "Advanced DAX rejected and its dependent dashboard content removed.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Advanced DAX review failed");
      throw reason;
    } finally {
      setBusy(undefined);
    }
  };

  const runDax = (expression: string) => {
    if (!workspace) return Promise.reject(new Error("Select a workspace first"));
    return evaluateDax(workspace.id, expression, 50, undefined, undefined, undefined, undefined, importedReport ? "powerbi" : "snapshot");
  };

  const askDax = async (input: DaxAssistInput, signal: AbortSignal, onStatus: (text: string) => void) => {
    if (!workspace || busy || designerDirty) throw new Error("Finish the current action before generating a formula.");
    const operation = claimConversationOperation();
    if (!operation) throw new Error("Finish the current action before generating a formula.");
    setBusy("DAX AI is responding");
    try {
      const answer = await streamDaxAssistant(workspace.id, snapshot?.id, input, signal, text => { if (operation.current() && !signal.aborted) onStatus(text); });
      if (!operation.current()) throw new DOMException("The conversation context changed.", "AbortError");
      return answer;
    } finally {
      if (operation.current()) { setBusy(undefined); operation.release(); }
    }
  };

  const saveDaxMeasure = async (name: string, expression: string, options?: { table?: string; format?: string }) => {
    if (busy || chatStreaming || designerDirty || conversationOperationRef.current) throw new Error("Finish the current action before saving a measure.");
    if (workspace && importedReport) {
      // An imported report has no draft: the measure waits in "Pending Power BI changes" until the user applies it (one Desktop cycle).
      const table = options?.table ?? measureHomeTables(importedReport.tables, importedReport.measures)[0];
      if (!table) throw new Error("This report has no table to add a measure to.");
      pendingStore.add(workspace.id, { op: "create_measure", table, name, expression, ...(options?.format ? { format_string: options.format } : {}) });
      setNotice(`Queued "${name}" in Pending Power BI changes. Apply it from the Model or Report view; it needs Power BI Desktop and takes about a minute.`);
      return;
    }
    if (!workspace || !draft) throw new Error("Create a draft before saving measures.");
    const operation = claimConversationOperation();
    if (!operation) throw new Error("Finish the current action before saving a measure.");
    setBusy("Saving DAX measure proposal");
    try {
      const value = await addDaxMeasure(workspace.id, {
        workspace_id: workspace.id,
        name,
        expression,
        idempotency_key: idempotencyKey(),
        expected_base_hash: draft.base_hash,
      });
      if (!operation.current()) throw new DOMException("The conversation context changed.", "AbortError");
      setDraft(value);
      setBuild(undefined);
      setNotice(`Saved "${name}" as an advanced-DAX proposal — approve it in Review & build before building.`);
    } finally { if (operation.current()) { setBusy(undefined); operation.release(); } }
  };

  useEffect(() => {
    if (!draft || themes.length) return;
    let cancelled = false;
    reportThemes().then((value) => { if (!cancelled) setThemes(value); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [draft, themes.length]);

  const buildProject = async () => {
    if (!workspace || !draft) return;
    setBusy("Compiling and validating the Power BI project");
    setError(undefined);
    try {
      const value = await api<Build>("/v1/builds", {
        method: "POST",
        body: JSON.stringify({
          workspace_id: workspace.id,
          draft_id: draft.id,
          project_name: workspace.name,
          idempotency_key: idempotencyKey(),
          expected_base_hash: workspace.base_hash,
        }),
      });
      setBuild(value);
      setBuilds((current) => [value, ...current.filter((item) => item.id !== value.id)]);
      setReviewOpen(true);
      if (value.state === "failed") setError(value.message);
      else setNotice("Power BI project compiled and validated successfully");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Power BI build failed");
    } finally {
      setBusy(undefined);
    }
  };

  const openPowerBI = async (target?: Build) => {
    if (!workspace || !target) return;
    setBusy("Refreshing local data and verifying the Power BI render");
    try {
      const result = await api<OpenResult>(`/v1/builds/${target.id}/open`, {
        method: "POST",
        body: JSON.stringify({
          workspace_id: workspace.id,
          idempotency_key: idempotencyKey(),
          expected_base_hash: workspace.base_hash,
        }),
      });
      if (!result.opened) throw new Error(result.reason || "Power BI Desktop did not open the project");
      if (!result.data_refreshed || !result.render_verified) {
        throw new Error("Power BI opened, but its local data render was not fully verified");
      }
      setNotice("Power BI refreshed, render-verified, and opened successfully");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Power BI Desktop could not be opened");
    } finally {
      setBusy(undefined);
    }
  };

  const exportBuild = async (target?: Build) => {
    if (!workspace || !target) return;
    let folder: string | null = null;
    if ("__TAURI_INTERNALS__" in window) {
      try {
        const result = await open({ directory: true, multiple: false, title: "Export validated Power BI project" });
        folder = typeof result === "string" ? result : null;
      } catch {
        setError("The native export folder picker could not be opened");
        return;
      }
    } else {
      folder = await requestPath({
        title: "Export validated Power BI project",
        description: "Pick the destination folder for the validated project copy.",
        placeholder: "C:\\path\\to\\export-folder",
        confirmLabel: "Export copy",
        startPath: exportedPath || workspace.selected_folder,
      });
    }
    if (!folder) return;
    setBusy("Exporting the complete validated PBIP project");
    setError(undefined);
    try {
      const receipt = await api<ExportReceipt>(`/v1/builds/${target.id}/export`, {
        method: "POST",
        body: JSON.stringify({
          workspace_id: workspace.id,
          destination_folder: folder,
          idempotency_key: idempotencyKey(),
          expected_base_hash: workspace.base_hash,
        }),
      });
      setExportedPath(receipt.exported_project_path);
      setNotice("Validated PBIP copy exported successfully");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The validated project could not be exported");
    } finally {
      setBusy(undefined);
    }
  };

  const relinkBuildSources = async (target?: Build) => {
    if (!workspace || !target?.project_hash) return;
    setBusy("Relinking the validated project to reviewed original files");
    setError(undefined);
    try {
      const receipt = await relinkBuild(target.id, {
        workspace_id: workspace.id,
        expected_project_hash: target.project_hash,
        idempotency_key: idempotencyKey(),
        expected_base_hash: workspace.base_hash,
      });
      if (!receipt.passed || !receipt.project_hash) throw new Error("Relink validation did not produce a committed project hash");
      const [refreshedBuilds, refreshedEvents] = await Promise.all([
        api<Build[]>(`/v1/workspaces/${workspace.id}/builds`),
        loadEvents(workspace.id),
      ]);
      setBuilds(refreshedBuilds);
      setBuild(refreshedBuilds.find((item) => item.id === target.id));
      storeEvents(refreshedEvents, snapshot?.id);
      setNotice("Power Query sources now point to the reviewed original files; validation and rollback evidence were recorded.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The reviewed original files could not be relinked");
    } finally {
      setBusy(undefined);
    }
  };

  const undoBuild = async (target?: Build) => {
    if (!workspace || !target) return;
    setBusy("Restoring the previous byte-exact project version");
    setError(undefined);
    try {
      const receipt = await api<UndoReceipt>(`/v1/builds/${target.id}/undo`, {
        method: "POST",
        body: JSON.stringify({
          workspace_id: workspace.id,
          idempotency_key: idempotencyKey(),
          expected_base_hash: workspace.base_hash,
        }),
      });
      if (receipt.restored) {
        const restored = { ...target, state: "rolled_back" as const, message: "Previous project version restored byte-for-byte" };
        setBuild(restored);
        setBuilds((current) => current.map((item) => item.id === target.id ? restored : item));
        setNotice("Previous project version restored byte-for-byte");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The previous project version could not be restored");
    } finally {
      setBusy(undefined);
    }
  };

  const switchWorkspace = async (id: string) => {
    if (designerDirty) return setNotice("Save or discard your report edits before switching workspaces.");
    if (busy || chatStreaming) return setNotice("Finish or stop the current operation before changing workspaces.");
    if (id === workspace?.id) return setWorkspaceMenuOpen(false);
    setWorkspaceMenuOpen(false);
    setBusy("Opening local workspace");
    setError(undefined);
    try {
      await refreshWorkspace(id);
      setScreen("studio");
      setNotice("Workspace switched without carrying over prior project state");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Workspace could not be opened");
    } finally {
      setBusy(undefined);
    }
  };

  const requestRenameWorkspace = (target: Workspace) => {
    if (busy || chatStreaming) return setNotice("Finish or stop the current operation before renaming a workspace.");
    setWorkspaceMenuOpen(false);
    setWorkspaceAdmin({ mode: "rename", target });
  };

  const requestDeleteWorkspace = (target: Workspace) => {
    if (busy || chatStreaming) return setNotice("Finish or stop the current operation before deleting a workspace.");
    if (designerDirty && target.id === workspace?.id) return setNotice("Save or discard your report edits before deleting this workspace.");
    setWorkspaceMenuOpen(false);
    setWorkspaceAdmin({ mode: "delete", target });
  };

  const submitWorkspaceRename = async (name: string) => {
    const admin = workspaceAdmin;
    if (!admin || admin.mode !== "rename") return;
    setWorkspaceAdmin(undefined);
    setBusy("Renaming workspace");
    setError(undefined);
    try {
      const fresh = await api<Workspace>(`/v1/workspaces/${admin.target.id}`);
      const updated = await renameWorkspace(admin.target.id, {
        workspace_id: admin.target.id,
        name,
        idempotency_key: idempotencyKey(),
        expected_base_hash: fresh.base_hash,
      });
      setWorkspaces((list) => list.map((item) => (item.id === updated.id ? updated : item)));
      if (workspace?.id === updated.id) setWorkspace(updated);
      setNotice(`Workspace renamed to ${updated.name}.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Workspace could not be renamed");
    } finally {
      setBusy(undefined);
    }
  };

  const submitWorkspaceDelete = async () => {
    const admin = workspaceAdmin;
    if (!admin || admin.mode !== "delete") return;
    setWorkspaceAdmin(undefined);
    setBusy("Deleting workspace");
    setError(undefined);
    try {
      const fresh = await api<Workspace>(`/v1/workspaces/${admin.target.id}`);
      const receipt = await deleteWorkspace(admin.target.id, {
        workspace_id: admin.target.id,
        confirm_name: admin.target.name,
        idempotency_key: idempotencyKey(),
        expected_base_hash: fresh.base_hash,
      });
      const remaining = workspaces.filter((item) => item.id !== admin.target.id);
      forgetWorkspaceDraft(admin.target.id);
      let cachesRemoved = false;
      try { cachesRemoved = clearWorkspaceChatStorage(localStorage, admin.target.id); } catch { /* Show the cache cleanup limitation below. */ }
      setWorkspaces(remaining);
      if (workspace?.id === admin.target.id) {
        clearWorkspaceState();
        setWorkspace(undefined);
        if (remaining.length) await refreshWorkspace(remaining[0].id);
        else {
          try { localStorage.removeItem("studio.lastWorkspace"); } catch { /* storage unavailable */ }
        }
      }
      setNotice(`Deleted ${receipt.name}. Source data and exported projects were left untouched.${cachesRemoved ? "" : " Browser storage could not be cleared; remove the site's cached data to erase typed questions."}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Workspace could not be deleted");
    } finally {
      setBusy(undefined);
    }
  };


  const undoDraftRevision = async () => {
    if (!draft || !workspace || !undoDraftHash || busy || designerDirty) return;
    setBusy("Restoring and validating the previous draft");
    try {
      const restored = await api<Draft>(`/v1/drafts/${draft.id}/restore`, { method: "POST", body: JSON.stringify({
        workspace_id: workspace.id, restore_hash: undoDraftHash, expected_base_hash: draft.base_hash, idempotency_key: idempotencyKey()
      }) });
      setDraft(restored); setUndoDraftHash(undefined); setRevisionReview(undefined); setBuild(undefined);
      setApprovedRelationships(defaultRelationshipSelection(restored.semantic_model.relationships)); setAttachedContext(undefined);
      setNotice("Previous draft restored. Review it again before building. Power BI unchanged.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Draft restore failed"); }
    finally { setBusy(undefined); }
  };

  const refreshHealth = async () => {
    setBusy("Refreshing local runtime diagnostics");
    try {
      setHealth(await api<Health>("/v1/health"));
      setNotice("Local runtime diagnostics refreshed");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Runtime diagnostics could not be refreshed");
    } finally {
      setBusy(undefined);
    }
  };

  const measures = useMemo(() => new Map(draft?.measure_proposal.measures.map((item) => [item.id, item]) || []), [draft]);
  const modelTables = useMemo(() => {
    if (!snapshot || !draft) return [];
    const roles = new Map(draft.semantic_model.tables.map((item) => [item.table_id, item]));
    return snapshot.tables.map(({ table }) => {
      const reviewedTable = roles.get(table.id);
      const hiddenColumnIds = new Set(reviewedTable?.hidden_column_ids || []);
      return {
        id: table.id,
        name: table.name,
        displayName: reviewedTable?.display_name || table.name,
        role: reviewedTable?.role,
        rowCount: table.row_count,
        columns: table.columns.map((column) => ({
          id: column.id,
          name: column.name,
          dataType: column.data_type,
          candidateKey: column.candidate_key && isKeyName(column.name),
          hidden: hiddenColumnIds.has(column.id),
        })),
      };
    });
  }, [draft, snapshot]);
  const modelRelationships = useMemo(() => draft?.semantic_model.relationships.map((relationship) => ({
    id: relationship.id,
    fromTableId: relationship.from_table_id,
    fromColumnId: relationship.from_column_id,
    toTableId: relationship.to_table_id,
    toColumnId: relationship.to_column_id,
    cardinality: relationship.cardinality.replaceAll("_", "-"),
    confidence: relationship.confidence,
    evidence: relationship.evidence,
    blockers: relationship.blockers,
    reviewStatus: relationship.review_status,
    crossFilterDirection: relationship.cross_filter_direction,
    active: relationship.is_active,
    recommendedByModel: relationship.recommended_by_model,
  })) || [], [draft]);
  const modelMeasures = useMemo<ModelWorkbenchMeasure[]>(() => {
    if (!snapshot || !draft) return [];
    const tableProfiles = new Map(snapshot.tables.map(({ table }) => [table.id, table]));
    return draft.measure_proposal.measures.map((measure) => {
      const table = tableProfiles.get(measure.table_id);
      const columnNames = new Map(table?.columns.map((column) => [column.id, column.name]) || []);
      if (measure.tier === "advanced_dax") {
        return {
          id: measure.id,
          name: measure.name,
          tableId: measure.table_id,
          tier: "advanced_dax",
          expression: measure.expression,
          formatString: measure.format_string,
          assumptions: measure.assumptions,
          approvalStatus: measure.approved ? "approved" : "pending",
          validation: { status: "valid", dependencies: measure.dependencies.map((id) => measures.get(id)?.name || id), messages: ["Catalog references and dependency graph validated locally."] },
        };
      }
      return {
        id: measure.id,
        name: measure.name,
        tableId: measure.table_id,
        tier: "governed",
        operation: measure.operation.replaceAll("_", " "),
        expression: governedExpression(measure, measures, table?.name || measure.table_id, columnNames),
        formatString: measure.format_string,
        assumptions: [],
        approvalStatus: "not_required",
        validation: { status: "valid", dependencies: governedDependencies(measure).map((id) => measures.get(id)?.name || columnNames.get(id) || id) },
      };
    });
  }, [draft, measures, snapshot]);
  const modelReviewState = useMemo<ModelWorkbenchReviewState>(() => {
    if (!draft) return "draft";
    const hasBlockingFinding = snapshot?.findings.some((finding) => finding.severity === "error" || finding.severity === "critical") ?? false;
    const hasBlockedRelationship = draft.semantic_model.relationships.some(
      (relationship) => relationship.review_status !== "rejected" && relationship.blockers.length > 0,
    );
    if (draft.status === "failed" || hasBlockingFinding || hasBlockedRelationship) return "blocked";
    if (["approved", "building", "validated"].includes(draft.status)) return "approved";
    const hasPendingReview = draft.semantic_model.relationships.some(
      (relationship) => relationship.review_status === "pending" && relationship.blockers.length === 0,
    ) || draft.measure_proposal.measures.some(
      (measure) => measure.tier === "advanced_dax" && !measure.approved,
    );
    return hasPendingReview ? "review" : "draft";
  }, [draft, snapshot]);
  const reviewChecks = useMemo(() => draft ? buildReviewChecklist(draft, approvedRelationships) : [], [approvedRelationships, draft]);
  // Previews use the draft theme; the default theme uses Power BI's base palette.
  const reportPalette = useMemo(() => themes.find((theme) => theme.id === (draft?.planning_provenance?.report_theme ?? "default"))?.data_colors, [draft?.planning_provenance?.report_theme, themes]);
  // A saved lab measure keeps the draft id, so a build only counts as
  // current when it followed the draft's latest approved content.
  const builtThisDraft = Boolean(draft && build?.state === "committed" && build.draft_id === draft.id
    && draft.status === "approved" && (!draft.updated_at || Date.parse(build.created_at) >= Date.parse(draft.updated_at)));
  const approveBlocked = reviewBlockReason(reviewChecks, { busy: Boolean(busy), designerDirty, approved: draft?.status === "approved" });
  const commandActions: CommandAction[] = [
    { id: "studio", label: "Open assistant", description: "Continue the Local AI chat for this workspace view", icon: <Sparkles size={17} />, shortcut: "Ctrl 1", keywords: "chat conversation dashboard dax", onSelect: openConversation },
    { id: "model", label: "Open Model Workbench", description: "Review relationships, cardinality and DAX approvals", icon: <Network size={17} />, shortcut: "Ctrl 2", keywords: "joins dax semantic", onSelect: () => setScreen("model") },
    { id: "designer", label: "Open Report", description: "Edit report pages, visuals, bindings and layout", icon: <LayoutDashboard size={17} />, shortcut: "Ctrl 3", keywords: "visual page report layout", onSelect: () => setScreen("designer") },
    { id: "data", label: "Review data", description: "Inspect local sources, profiles and quality findings", icon: <Database size={17} />, shortcut: "Ctrl 4", keywords: "sources import", onSelect: () => setScreen("data") },
    { id: "inspect-pbip", label: "Inspect existing PBIP", description: "Check project compatibility; existing report pages are not imported", icon: <FileBarChart2 size={17} />, keywords: "power bi pbix project existing dashboard", onSelect: () => void chooseExistingProject() },
    { id: "open-report", label: "Open Power BI report", description: "Import existing PBIX pages, model and embedded tables", icon: <FileBarChart2 size={17} />, keywords: "pbix dashboard existing import", onSelect: () => void chooseExistingReport() },
    { id: "history", label: "Activity & versions", description: "Inspect validation, export, open and rollback status", icon: <History size={17} />, shortcut: "Ctrl 5", keywords: "power bi pbip", onSelect: () => setScreen("history") },
    { id: "evidence", label: "View validation evidence", description: "Trace snapshots, queries, approvals and validators", icon: <ShieldCheck size={17} />, shortcut: "Ctrl 6", keywords: "audit hashes provenance", onSelect: () => setScreen("evidence") },
    { id: "settings", label: "Settings and health", description: "Check Local AI, memory, Power BI and local tooling", icon: <Settings size={17} />, shortcut: "Ctrl 7", keywords: "diagnostics", onSelect: () => setScreen("settings") },
    { id: "new-workspace", label: "New local workspace", description: "Choose a folder and review supported files", icon: <FolderOpen size={17} />, keywords: "create folder", onSelect: () => void chooseFolder() },
    { id: "database-workspace", label: "Connect to a database", description: "Start a workspace for SQL Server, Azure SQL or Snowflake", icon: <Database size={17} />, keywords: "connect database sql snowflake sources", onSelect: () => void startDatabaseWorkspace() },
    ...(workspace?.selected_folder ? [{ id: "rescan", label: "Rescan source folder", description: "Check file signatures without importing data", icon: <RefreshCw size={17} />, keywords: "refresh inventory", onSelect: () => void rescan() }] : []),
    ...(workspace && snapshot && !draft ? [{ id: "executive-overview", label: "Create executive overview", description: "Generate one KPI, trend, and category page from the reviewed snapshot", icon: <LayoutDashboard size={17} />, keywords: "first run overview dashboard", onSelect: () => void createExecutiveOverview() }] : []),
    ...(workspace && snapshot ? [{ id: "ask-qwen", label: "Ask AI", description: "Open the workspace conversation", icon: <Sparkles size={17} />, keywords: "chat prompt local", onSelect: openConversation }] : []),
    ...(build?.state === "committed" ? [{ id: "open-powerbi", label: "Open in Power BI", description: "Refresh local data, verify every page, and open Desktop", icon: <FileBarChart2 size={17} />, keywords: "desktop pbip refresh render", onSelect: () => void openPowerBI(build) }] : []),
  ];

  return (
    <div className={`app-shell unified-app${navCollapsed ? " nav-collapsed" : ""}`}>
      <a className="skip-link" href="#main-content">Skip to workspace</a>
      <header className="titlebar">
        <div className="brand"><img className="brand-mark" src="/branding/modelcraft-icon.png" alt="" width={32} height={32} aria-hidden="true" /><strong>Modelcraft</strong></div>
        <HeaderCommandButton onClick={() => setCommandOpen(true)} />
        <div className="runtime-strip">
          {initializing ? <span className="status"><LoaderCircle className="spin" size={15} />Connecting locally…</span> : !sidecarOnline ? <span className="status">Local engine disconnected</span> : <RuntimeStatus ready={Boolean(health?.model_available)} error={health?.model_error} installNeeded={/not installed/i.test(health?.model_error ?? "")} setupState={modelSetupState} onInstall={() => setScreen("settings")} />}
          <span className="divider" /><span className="status"><span className="offline-dot" />Offline</span>
        </div>
        <div className="workspace-area">
          <WorkspaceTrigger workspace={workspace} expanded={workspaceMenuOpen} onClick={() => setWorkspaceMenuOpen((value) => !value)} />
          {health && <span className="memory"><MemoryStick size={14} />{health.available_memory_gb.toFixed(1)} GiB free</span>}
          <WorkspaceMenu open={workspaceMenuOpen} workspaces={workspaces} activeId={workspace?.id} onSelect={(id) => void switchWorkspace(id)} onOpenReport={() => { setWorkspaceMenuOpen(false); void chooseExistingReport(); }} onCreate={() => { setWorkspaceMenuOpen(false); void chooseFolder(); }} onRename={requestRenameWorkspace} onDelete={requestDeleteWorkspace} onClose={() => setWorkspaceMenuOpen(false)} />
        </div>
      </header>

      <aside className="sidebar">
        <button className="nav-item navigation-toggle" onClick={() => setNavCollapsed((value) => !value)} aria-label={navCollapsed ? "Expand navigation" : "Collapse navigation"} title={navCollapsed ? "Expand navigation" : "Collapse navigation"} aria-expanded={!navCollapsed}>
          {navCollapsed ? <PanelLeftOpen size={19} /> : <PanelLeftClose size={19} />}
          <span>{navCollapsed ? "Expand" : "Collapse"}</span>
        </button>
        <nav aria-label="Primary navigation">
          <NavItem active={!["history", "settings"].includes(screen)} icon={<LayoutDashboard />} label="Workspace" onClick={() => setScreen("studio")} />
          <NavItem active={screen === "history"} icon={<History />} label="Activity & versions" onClick={() => setScreen("history")} />
          <NavItem active={screen === "settings"} icon={<Settings />} label="Settings" onClick={() => setScreen("settings")} />
        </nav>
        <div className={`sidebar-bottom ${health?.status === "ready" ? "" : "warning"}`}><span className="ready-dot" /><span>{initializing ? "Connecting" : health?.status === "ready" ? "Local runtime" : "Needs attention"}</span></div>
      </aside>

      <ReportPaletteContext.Provider value={reportPalette}>
      <main id="main-content" className="main unified-main">
        {sidecarOnline && !engineFeatures.includes("chat-context") && <div className="engine-update-notice" role="status">Workspace updated. Restart the local Python engine to enable contextual chat and draft undo.</div>}
        {error && !reviewOpen && <div className="error-banner" role="alert"><AlertTriangle size={17} /><span>{error}</span><button onClick={() => setError(undefined)}>Dismiss</button></div>}
        {busy && !reviewOpen && <div className="progress-banner" role="status"><LoaderCircle className="spin" size={17} />{busy}</div>}
        {initializing ? <InitialLoading /> : !workspace ? <WelcomeScreen chooseFolder={chooseFolder} chooseDatabase={() => void startDatabaseWorkspace()} chooseReport={() => void chooseExistingReport()} chooseExistingProject={chooseExistingProject} busy={Boolean(busy)} onModelInstalled={() => void refreshHealth()} onModelStateChange={setModelSetupState} /> : <>
        <UnifiedWorkspace visible={!["history", "settings"].includes(screen)} view={authoringView} onView={setAuthoringView} chatOpen={visibleConversation} onChat={() => authoringView === "dax" ? setDaxConversationOpen(value => !value) : setConversationOpen(value => !value)} daxConversationRef={setDaxConversationHost}
          toolbar={<><span className={`draft-state${designerDirty ? " unsaved" : ""}`} role="status" aria-live="polite" title={importedReport ? "Edits are kept in Studio's copy of the report. Your original file changes only when you save to it or save a copy." : "Your Power BI project stays unchanged until you build."}>{designerDirty ? "Unsaved report edits" : draft?.status === "approved" ? "Approved" : draft ? "Draft" : importedReport ? (importedReport.head?.edited ? "Edited in Studio" : "Imported report") : "Choose data to begin"}</span>
            <div className="unified-draft-actions" ref={setDesignerToolbar} hidden={authoringView !== "report" && !designerDirty} />
            {!designerDirty && !importedReport && <button type="button" className="primary yellow" disabled={!draft || Boolean(busy)} onClick={() => setReviewOpen(true)}>Review &amp; build</button>}</>}
          conversation={snapshot ? <ChatConversation key={`${workspace.id}:${snapshot.id}`} snapshot={snapshot} draft={draft} messages={chatPartition.active} archived={chatPartition.archived} streamed={streamed} streaming={chatStreaming} activity={chatStreaming ? chatActivity : busy} outcome={chatOutcome} failureMessage={chatFailure} recoveryRequest={failedChatRequest?.workspaceId === workspace.id ? { message: failedChatRequest.message, contextLabel: failedChatRequest.context?.label } : undefined} onRecover={failedChatRequest ? () => void sendChat(failedChatRequest.message, false, failedChatRequest.context, failedChatRequest) : undefined} draftSaveWarning={chatRecoveryStorageFailed ? "Retry details could not be saved on this device. Keep this tab open to retain the original question and context." : chatDraftStorageFailed ? "Your typed question could not be saved on this device. Keep this tab open to retain it." : undefined} blockedReason={designerDirty ? "Save your report edits before sending a message." : !sidecarOnline ? "The local engine is disconnected. Your question is kept while it reconnects." : modelRecoveryNeeded ? "Local AI is not ready yet. Your question is kept while it starts." : undefined} onReviewEdits={designerDirty ? () => goView("report") : undefined} discussionOnly={discussionOnly && !chatEdits.appliedThisTurn} active={visibleConversation && authoringView !== "dax" && screen === "studio"} busy={Boolean(busy) || designerDirty || editingBusy} input={chatInput} onInputChange={setChatInput} onSend={(value) => void sendChat(value)} onRetry={(value) => void sendChat(value, true)} onEditMessage={editMessage} onUseDax={handoff => openDaxSuggestion(handoff.expression, "From Workspace chat", handoff)} sharedHistoryItems={sharedHistoryItems} onSelectSharedHistory={selectSharedHistory} historySelection={workspaceHistorySelection} canClear={engineFeatures.includes("chat-clear")} onClear={() => void clearConversation()} onStop={() => abortRef.current?.abort()} onData={() => goView("data")} onReviewDraft={() => goView("report")} composerRef={composerRef} editPanel={importedReport ? <EditChatCards {...chatEdits.panel} disabled={chatEdits.panel.disabled || editingBusy} /> : undefined} context={attachedContext ?? (authoringView === "report" ? reportSelection : undefined)} onClearContext={() => { setAttachedContext(undefined); setReportSelection(undefined); requestAnimationFrame(() => composerRef.current?.focus()); }}
            revisionPanel={revisionReview ? <div className="conversation-change"><DraftRevisionPanel key={draft?.base_hash} review={revisionReview} compact onReview={() => goView("report")} />{undoDraftHash && <button type="button" className="secondary" disabled={Boolean(busy) || designerDirty || !engineFeatures.includes("draft-restore")} title={!engineFeatures.includes("draft-restore") ? "Restart the local Python engine to enable undo" : "Restore the previous draft without changing Power BI"} onClick={() => void undoDraftRevision()}>Undo draft change</button>}</div> : undefined}
          /> : <EmptyStudio onData={() => goView("data")} />}
          report={importedReport ? <ImportedReportWorkspace key={importedReport.id} report={importedReport} snapshot={snapshot} view="report" onContext={attachContext} onSelection={selectReportVisual} onReportChanged={refreshImportedReport} onEditingBusy={setEditingBusy} editingRef={editingRef} active={authoringView === "report" && screen === "studio"} busy={Boolean(busy)} onOpenDesktop={openImportedDesktop} onDax={(code) => openDaxSuggestion(code, "Original report measure")} /> : draft && snapshot ? <div className="dashboard-designer-screen"><DashboardDesigner key={`${workspace.id}:${snapshot.id}`} draft={draft} snapshot={snapshot} busy={Boolean(busy)} onSave={saveDashboard} onContext={attachContext} onDirtyChange={setDesignerDirty} toolbarTarget={designerToolbar} reportAction={reportAction?.workspaceId === workspace.id ? reportAction : undefined} onSelection={selectReportVisual} /></div> : <ToolEmptyScreen icon={<LayoutDashboard size={27} />} title={snapshot ? "Create one executive overview" : "Import data to start the report"} description={snapshot ? "Studio will draft a single page with KPIs, a trend, and a category chart from your reviewed tables. Chat can revise it after you review." : "Import local data before creating your first report."} action={snapshot ? "Create executive overview" : "Choose data"} onAction={() => snapshot ? void createExecutiveOverview() : goView("data")} />}
          data={<DataWorkbench key={workspace?.id} onImportBusy={value => setBusy(value ? "Importing database snapshot" : undefined)} workspace={workspace} snapshot={snapshot} busy={Boolean(busy) || designerDirty} onOpenSourceReport={openImportedDesktop} onImportReport={() => void chooseExistingReport()} onImported={async () => { if (workspace) await refreshWorkspace(workspace.id); }} onAskTable={(tableId, label) => attachContext({ table_id: tableId, label })}><DataScreen workspace={workspace} snapshot={snapshot} compatibility={compatibility} inventory={inventory} selected={selected} setSelected={setSelected} chooseFolder={chooseFolder} pickFolder={() => void pickDataFolder()} rescan={rescan} ingest={ingest} busy={Boolean(busy) || designerDirty} hasDraft={Boolean(draft)} onCreateOverview={() => void createExecutiveOverview()} onAskTable={(tableId, label) => attachContext({ table_id: tableId, label })} /></DataWorkbench>}
          model={importedReport ? <ImportedReportWorkspace key={importedReport.id} report={importedReport} snapshot={snapshot} view="model" busy={Boolean(busy)} onReportChanged={refreshImportedReport} onEditingBusy={setEditingBusy} onOpenDesktop={openImportedDesktop} onDax={(code) => openDaxSuggestion(code, "Original report measure")} /> : draft && snapshot ? <div className="model-workbench-screen"><ModelWorkbench
            tables={modelTables}
            relationships={modelRelationships}
            measures={modelMeasures}
            selectedRelationshipIds={approvedRelationships}
            selectedMeasureId={selectedModelMeasureId}
            reviewState={modelReviewState}
            findings={snapshot.findings.map((finding) => ({ id: finding.id, severity: finding.severity === "error" || finding.severity === "critical" ? "error" : finding.severity === "warning" ? "warning" : "info", message: finding.message }))}
            readOnly={true}
            promptDisabled={Boolean(busy) || designerDirty}
            isPrompting={Boolean(busy)}
            onRelationshipToggle={(relationshipId, approved) => setApprovedRelationships((current) => { const next = new Set(current); if (approved) next.add(relationshipId); else next.delete(relationshipId); return next; })}
            onDaxReview={reviewDaxMeasure}
            onMeasureSelect={setSelectedModelMeasureId}
            onAskContext={attachContext}
          /></div> : <ToolEmptyScreen icon={<Network size={27} />} title="No model draft yet" description="Ask the AI to create a report from your imported data." action="Open assistant" onAction={openConversation} />}
          dax={<DaxLab chatBoundary={daxConversations.boundaryId} active={visibleConversation && authoringView === "dax" && screen === "studio"} externalConversation conversationHost={daxConversationHost} handoffStorageWarning={daxHandoffStorageFailed ? "Source-request storage is unavailable. Changes to this context may not survive reload." : undefined} onOpenConversation={() => setDaxConversationOpen(true)} sharedHistoryItems={sharedHistoryItems} onSelectSharedHistory={selectSharedHistory} historySelection={daxHistorySelection} onHistoryItemsChange={publishDaxHistory} handoff={activeDaxHandoff ? { request: activeDaxHandoff.source.requestText, sourceLabel: activeDaxHandoff.source.sourceLabel, sourceMessageId: activeDaxHandoff.source.sourceMessageId, code: activeDaxHandoff.source.expression, label: "From Workspace chat" } : undefined} onBackToSource={activeDaxHandoff ? backToSourceChat : undefined} onClearHandoff={() => { clearDaxHandoff(); setDaxSuggestion(undefined); }} history={daxConversations.current} archived={daxConversations.archives} durableHistory={engineFeatures.includes("dax-chat-clear")} onNewChat={newDaxChat} key={`${workspace.id}:${snapshot?.id}`} workspaceId={workspace.id} snapshot={snapshot} draft={draft} busy={Boolean(busy) || designerDirty} onRun={runDax} onSave={saveDaxMeasure} onAsk={askDax} importedMeasures={importedReport?.measures} importedTables={importedMeasureTables} suggestionLabel={daxSuggestion?.label} suggestion={daxSuggestion?.workspaceId === workspace.id && daxSuggestion?.snapshotId === snapshot?.id ? daxSuggestion.code : undefined} onDismissSuggestion={() => setDaxSuggestion(undefined)} />}
        />
        <section hidden={screen !== "history"} className="unified-activity"><BuildScreen builds={builds} activeBuild={build || builds[0]} exportedPath={exportedPath} busy={Boolean(busy)} desktopAvailable={Boolean(health?.powerbi_desktop_available)} onOpen={(item) => void openPowerBI(item)} onExport={(item) => void exportBuild(item)} onRelink={(item) => void relinkBuildSources(item)} onUndo={(item) => void undoBuild(item)} /><details><summary>Evidence &amp; validation details</summary><EvidenceScreen snapshot={snapshot} draft={draft} events={events} builds={builds} /></details></section>
        {screen === "settings" && <SettingsWithKnowledge><SettingsScreen health={health} busy={Boolean(busy)} onRefresh={() => void refreshHealth()} onModelStateChange={setModelSetupState} /></SettingsWithKnowledge>}
        </>}
        {reviewOpen && draft && snapshot && <ReviewDialog onClose={() => setReviewOpen(false)}>
          {error && <div className="error-banner" role="alert"><AlertTriangle size={17} /><span>{error}</span><button onClick={() => setError(undefined)}>Dismiss</button></div>}
          {busy && <div className="progress-banner" role="status"><LoaderCircle className="spin" size={17} />{busy}</div>}
          <div className="review-overview"><strong>{draft.pages.length} pages · {draft.measure_proposal.measures.length} measures</strong><p>{draft.pages.map(page => page.title).join(" · ")}</p><ReviewChecklist items={reviewChecks} /><DesignSummary draft={draft} />{revisionReview && <DraftRevisionPanel review={revisionReview} onReview={() => { setReviewOpen(false); goView("report"); }} />}</div>
          <ModelWorkbench
            tables={modelTables}
            relationships={modelRelationships}
            measures={modelMeasures}
            selectedRelationshipIds={approvedRelationships}
            selectedMeasureId={selectedModelMeasureId}
            reviewState={modelReviewState}
            findings={snapshot.findings.map((finding) => ({ id: finding.id, severity: finding.severity === "error" || finding.severity === "critical" ? "error" : finding.severity === "warning" ? "warning" : "info", message: finding.message }))}
            readOnly={draft.status === "approved" || Boolean(busy)}
            promptDisabled={Boolean(busy) || designerDirty}
            isPrompting={Boolean(busy)}
            onRelationshipToggle={(relationshipId, approved) => setApprovedRelationships((current) => { const next = new Set(current); if (approved) next.add(relationshipId); else next.delete(relationshipId); return next; })}
            onDaxReview={reviewDaxMeasure}
            onMeasureSelect={setSelectedModelMeasureId}
          />
          {/* Shown above the footer so a finished build is visible without scrolling. */}
          {build && <div className="review-build-result" role="status"><strong>{build.state === "committed" ? "Project built and definitions validated" : build.message}</strong>{build.state === "committed" && <><p>Opening refreshes data and checks rendering in Power BI Desktop.</p><button type="button" className="primary yellow" disabled={Boolean(busy) || !health?.powerbi_desktop_available} onClick={() => void openPowerBI(build)}>Open in Power BI</button><button type="button" className="secondary" disabled={Boolean(busy)} onClick={() => void exportBuild(build)}>Save As</button></>}</div>}
          <footer className="review-actions"><span>{busy && reviewOpen ? busy : builtThisDraft ? "Built and validated. Open it in Power BI or save a copy above." : draft.status === "approved" ? "Reviewed draft ready to build." : approveBlocked || "Relationships, measures, advanced DAX, and pages are ready. Approve records the review; Build writes Power BI files."}</span>
            {draft.status === "approved" ? <button type="button" className={builtThisDraft ? "secondary" : "primary yellow"} disabled={Boolean(busy) || designerDirty} onClick={() => void buildProject()}>{builtThisDraft ? "Rebuild" : "Build in Power BI"}</button> : <button type="button" className="primary yellow" disabled={!canApproveDraft(reviewChecks, { busy: Boolean(busy), designerDirty })} title={approveBlocked} onClick={() => void approve()}>Approve reviewed draft</button>}
          </footer>
        </ReviewDialog>}
      </main>
      </ReportPaletteContext.Provider>
      <ProductNotice message={notice} onClose={() => setNotice(undefined)} />
      <CommandPalette open={commandOpen} actions={commandActions} onClose={() => setCommandOpen(false)} />
      {pathPrompt && <PathPrompt title={pathPrompt.title} description={pathPrompt.description} placeholder={pathPrompt.placeholder} confirmLabel={pathPrompt.confirmLabel} startPath={pathPrompt.startPath} acceptPbix={pathPrompt.acceptPbix} reportOnly={pathPrompt.reportOnly} onBrowse={browseDirectory} onSubmit={(value) => settlePathPrompt(value)} onClose={() => settlePathPrompt(null)} />}
      {workspaceAdmin?.mode === "rename" && <RenameWorkspaceDialog workspace={workspaceAdmin.target} busy={Boolean(busy)} onSubmit={(name) => void submitWorkspaceRename(name)} onClose={() => setWorkspaceAdmin(undefined)} />}
      {workspaceAdmin?.mode === "delete" && <DeleteWorkspaceDialog workspace={workspaceAdmin.target} busy={Boolean(busy)} onSubmit={() => void submitWorkspaceDelete()} onClose={() => setWorkspaceAdmin(undefined)} />}
    </div>
  );
}

function NavItem({ active, icon, label, onClick }: { active: boolean; icon: React.ReactElement; label: string; onClick: () => void }) {
  return <button className={active ? "nav-item active" : "nav-item"} onClick={onClick} aria-current={active ? "page" : undefined} title={label}>{icon}<span>{label}</span></button>;
}

function WelcomeScreen({ chooseFolder, chooseDatabase, chooseReport, chooseExistingProject, busy, onModelInstalled, onModelStateChange }: { chooseFolder: () => void; chooseDatabase: () => void; chooseReport: () => void; chooseExistingProject: () => void; busy: boolean; onModelInstalled: () => void; onModelStateChange: (state: ModelInstallState | undefined) => void }) {
  // A fresh install has no workspace yet, so the one-time Qwen 4B install is offered here.
  return <section className="welcome">
    <div className="welcome-install"><ModelInstallCard onInstalled={onModelInstalled} onStateChange={onModelStateChange} /></div>
    <div className="welcome-icon"><Sparkles size={26} aria-hidden="true" /></div>
    <h1>Connect data or open a Power BI report</h1>
    <p>Import reviewed files or database tables into a managed snapshot. For other Power BI connectors, configure the source in Power BI Desktop and open its saved report here.</p>
    <ol className="first-run-steps">
      <li>Choose a source</li><li>Review and import its data</li><li>Build or refine your report</li>
    </ol>
    <div className="welcome-action-group">
      <div className="welcome-actions">
        <button type="button" className="primary large" onClick={chooseFolder} disabled={busy}><FolderOpen size={18} aria-hidden="true" />Choose local files</button>
        <button type="button" className="secondary large" onClick={chooseDatabase} disabled={busy}><Database size={18} aria-hidden="true" />Connect to a database</button>
        <button type="button" className="secondary large" onClick={chooseReport} disabled={busy}><FileBarChart2 size={18} aria-hidden="true" />Open Power BI report</button>
      </div>
      <button type="button" className="secondary welcome-inspect" onClick={chooseExistingProject} disabled={busy}>Inspect an existing PBIP project<ArrowRight size={15} aria-hidden="true" /></button>
    </div>
    <div className="trust-row"><span><ShieldCheck size={16} aria-hidden="true" />No cloud inference</span><span><Database size={16} aria-hidden="true" />Managed snapshots</span><span><FileBarChart2 size={16} aria-hidden="true" />Real PBIP output</span></div>
  </section>;
}

function EmptyStudio({ onData }: { onData: () => void }) {
  return <section className="welcome compact"><div className="welcome-icon"><Database size={26} /></div><h1>Select data before starting the conversation</h1><p>Choose reviewed local files or connect to a database on the Data tab. An imported Power BI report can also use its open Desktop model for native queries.</p><button className="primary" onClick={onData}>Choose data sources<ArrowRight size={17} /></button></section>;
}

function ToolEmptyScreen({ icon, title, description, action, onAction }: { icon: React.ReactElement; title: string; description: string; action: string; onAction: () => void }) {
  return <section className="welcome compact"><div className="welcome-icon">{icon}</div><h1>{title}</h1><p>{description}</p><button type="button" className="primary" onClick={onAction}>{action}<ArrowRight size={17} /></button></section>;
}

function DraftRevisionPanel({ review, onReview, compact = false }: { review?: DraftRevisionReview; onReview: () => void; compact?: boolean }) {
  const added = [...(review?.added_measures || []).map((item) => `Measure: ${item}`), ...(review?.added_pages || []).map((item) => `Page: ${item}`)];
  const changed = [...(review?.changed_measures || []).map((item) => `Measure: ${item}`), ...(review?.changed_pages || []).map((item) => `Page: ${item}`)];
  const removed = [...(review?.removed_measures || []).map((item) => `Measure: ${item}`), ...(review?.removed_pages || []).map((item) => `Page: ${item}`)];

  return <div className="draft-revision-zone">
    {review && <section className="revision-review" aria-label="Applied dashboard revision">
      <details className="revision-review-disclosure" open={!compact}>
        <summary className="revision-review-heading">
          <span className="revision-review-icon"><CheckCircle2 size={18} aria-hidden="true" /></span>
          <span><strong>Draft updated</strong><small>Power BI unchanged. Review before building.</small></span>
          <ChevronDown className="revision-review-chevron" size={17} aria-hidden="true" />
        </summary>
        <div className="revision-review-details">
          <blockquote>{review.prompt}</blockquote>
          <div className="revision-metrics"><span><small>Measures</small><strong>{review.before.measures} → {review.after.measures}</strong></span><span><small>Pages</small><strong>{review.before.pages} → {review.after.pages}</strong></span><span><small>Visuals</small><strong>{review.before.visuals} → {review.after.visuals}</strong></span></div>
          {!!(added.length || changed.length || removed.length) && <div className="revision-changes">
            {added.map((item) => <span className="added" key={`added-${item}`}><Plus size={13} />{item}</span>)}
            {changed.map((item) => <span className="changed" key={`changed-${item}`}><RefreshCw size={13} />{item}</span>)}
            {removed.map((item) => <span className="removed" key={`removed-${item}`}><RotateCcw size={13} />Removed {item}</span>)}
          </div>}
        </div>
      </details>
      <div className="revision-review-actions">
        <button type="button" className="secondary revision-review-button" onClick={onReview}>View change <ArrowRight size={15} /></button>
        <span className={review.approval_reset ? "revision-reset success" : "revision-reset warning"}><ShieldCheck size={13} />{review.approval_reset ? "Approval reset" : "Check approval"}</span>
      </div>
    </section>}

 </div>;
}

function DataScreen({ workspace, snapshot, compatibility, inventory, selected, setSelected, chooseFolder, pickFolder, rescan, ingest, busy, hasDraft, onCreateOverview }: { onAskTable?: (tableId: string, label: string) => void; workspace?: Workspace; snapshot?: Snapshot; compatibility?: Compatibility; inventory: InventoryItem[]; selected: Set<string>; setSelected: (value: Set<string>) => void; chooseFolder: () => void; pickFolder: () => void; rescan: () => void; ingest: () => void; busy: boolean; hasDraft?: boolean; onCreateOverview?: () => void }) {
  const selectable = inventory.filter((item) => item.selectable);
  const hasFolder = Boolean(workspace?.selected_folder);
  return <section className="content-page data-authoring-page">
    <div className="page-header compact-authoring-toolbar"><h1 className="sr-only">Data</h1><div className="header-actions"><button className="secondary" disabled={busy} onClick={workspace ? (hasFolder ? rescan : pickFolder) : chooseFolder}><RefreshCw size={16} />{workspace && hasFolder ? "Rescan" : "Choose folder"}</button>{hasFolder && <button className="primary" onClick={ingest} disabled={busy || !selected.size}><Play size={16} />Import {selected.size || ""} selected</button>}{snapshot && !hasDraft && onCreateOverview && <button className="secondary" onClick={onCreateOverview} disabled={busy}>Create executive overview</button>}</div></div>
    {workspace?.selected_folder && <div className="path-bar"><FolderOpen size={16} /><span>{workspace.selected_folder}</span><span>{inventory.length} files found</span><button type="button" className="secondary path-change" onClick={pickFolder} disabled={busy}>Change folder</button></div>}
    {compatibility && <div className={compatibility.supported ? "snapshot-banner" : "error-banner static"}>{compatibility.supported ? <CheckCircle2 size={18} /> : <AlertTriangle size={18} />}<div><strong>{compatibility.supported ? "Existing PBIP can be used as a compatibility reference" : "Existing PBIP is not supported for managed-copy authoring"}</strong><span>{compatibility.supported ? "Builds create a separate validated managed PBIP project. The selected original is never modified or migrated in place." : compatibility.blocked_features.join(" · ")}</span>{compatibility.notes.map((note) => <small key={note}>{note}</small>)}</div></div>}
    {snapshot && <div className="snapshot-banner"><CheckCircle2 size={18} /><div><strong>Imported data</strong><span>{snapshot.tables.length} tables · {snapshot.tables.reduce((sum, item) => sum + item.table.row_count, 0).toLocaleString()} rows · {formatBytes(snapshot.normalized_bytes)}</span></div></div>}
    {!hasFolder ? <div className="empty-panel"><FolderOpen size={28} /><h2>Choose a folder to begin</h2><p>No files are read until you review and select them.</p><button className="primary" onClick={workspace ? pickFolder : chooseFolder}>Choose folder</button></div> : <div className="inventory-card"><div className="inventory-toolbar"><label><input type="checkbox" checked={selectable.length > 0 && selected.size === selectable.length} onChange={(event) => setSelected(event.target.checked ? new Set(selectable.map((item) => item.id)) : new Set())} />Select supported files</label><span>{selected.size} selected</span></div><div className="inventory-head"><span>Name</span><span>Detected format</span><span>Size</span><span>Modified</span></div>{inventory.map((item) => <label key={item.id} className={`inventory-row ${!item.selectable ? "disabled" : ""}`}><input type="checkbox" disabled={!item.selectable} checked={selected.has(item.id)} onChange={(event) => { const next = new Set(selected); if (event.target.checked) next.add(item.id); else next.delete(item.id); setSelected(next); }} /><FileSpreadsheet size={18} /><span className="file-name"><strong>{item.name}</strong><small>{item.relative_path}</small>{item.warning && <em><AlertTriangle size={13} />{item.warning}</em>}</span><span className="format-badge">{item.detected_format.toUpperCase()}</span><span>{formatBytes(item.size_bytes)}</span><span>{new Date(item.modified_at).toLocaleDateString()}</span></label>)}</div>}

  </section>;
}


function BuildScreen({ builds, activeBuild, exportedPath, busy, desktopAvailable, onOpen, onExport, onRelink, onUndo }: { builds: Build[]; activeBuild?: Build; exportedPath?: string; busy: boolean; desktopAvailable: boolean; onOpen: (build: Build) => void; onExport: (build: Build) => void; onRelink: (build: Build) => void; onUndo: (build: Build) => void }) {
  const titleFor = (item: Build) => item.state === "committed" ? "Validated PBIP committed" : item.state === "rolled_back" ? "Previous project restored" : item.state === "failed" ? "Build failed safely" : item.state === "validating" ? "Validating Power BI project" : item.state === "compiling" ? "Compiling Power BI project" : "Build queued";
  return <section className="content-page build-history-page"><div className="page-header"><div><span className="eyebrow">Power BI projects</span><h1>Activity &amp; versions</h1><p>Definitions are validated at build time; opening refreshes local data and verifies the real Desktop render.</p></div>{activeBuild && <span className={`build-summary-state ${activeBuild.state}`}>{activeBuild.state.replaceAll("_", " ")}</span>}</div>{!builds.length ? <div className="empty-panel"><History size={28} /><h2>No desktop builds yet</h2><p>Approve a dashboard draft to produce a PBIP project.</p></div> : <div className="build-history-grid"><div className="build-list" aria-label="Builds">{builds.map((item, index) => <article className={`build-row ${index === 0 ? "current" : ""}`} key={item.id}><div className={`build-icon ${item.state === "committed" ? "success" : item.state === "failed" ? "failed" : "pending"}`}>{item.state === "committed" ? <Check size={19} /> : item.state === "failed" ? <AlertTriangle size={19} /> : <LoaderCircle size={19} className={item.state === "compiling" || item.state === "validating" ? "spin" : ""} />}</div><div><strong>{titleFor(item)}</strong><small>{localDate(item.created_at).toLocaleString()} · {item.id.slice(0, 18)}</small><p>{item.message}</p></div><span className={`build-state ${item.state}`}>{item.state.replaceAll("_", " ")}</span></article>)}</div>{activeBuild && <aside className="build-detail"><span className="eyebrow">Current output</span><h2>{titleFor(activeBuild)}</h2><p>{activeBuild.message}</p>{activeBuild.project_path && <code>{activeBuild.project_path}</code>}{exportedPath && <p className="success-line"><CheckCircle2 size={15} />Exported to {exportedPath}</p>}<div className="build-actions">{activeBuild.state === "committed" && <><button className="secondary" onClick={() => onExport(activeBuild)} disabled={busy}><Download size={17} />Save As</button><button className="secondary" onClick={() => onRelink(activeBuild)} disabled={busy || !activeBuild.project_hash} title="Replace managed snapshot connectors with reviewed original-file paths"><Link2 size={17} />Relink originals</button><button className="secondary" onClick={() => onUndo(activeBuild)} disabled={busy}><RotateCcw size={17} />Undo</button><button className="primary yellow" onClick={() => onOpen(activeBuild)} disabled={busy || !desktopAvailable}><FileBarChart2 size={17} />{desktopAvailable ? "Refresh & open in Power BI" : "Power BI not found"}</button></>}</div><p className="build-safety"><ShieldCheck size={15} />Open, export, relink, and undo verify the recorded project hash before changing or using any files.</p></aside>}</div>}</section>;
}

function SettingsScreen({ health, busy, onRefresh, onModelStateChange }: { health?: Health; busy: boolean; onRefresh: () => void; onModelStateChange: (state: ModelInstallState | undefined) => void }) {
  return <section className="content-page"><div className="page-header"><div><span className="eyebrow">Local runtime</span><h1>Settings & Health</h1><p>Model identity, Power BI tooling, memory and offline boundaries are reported from this machine.</p></div><button className="secondary" onClick={onRefresh} disabled={busy}><RefreshCw size={16} />Refresh diagnostics</button></div><ModelInstallCard onInstalled={onRefresh} onStateChange={onModelStateChange} /><div className="settings-grid"><article><Bot size={21} /><div><span>Local model</span><strong>{health?.model_name || "Checking…"}</strong><small>{health?.model_error || health?.model_digest?.slice(0, 24) || "Digest unavailable"}</small></div><span className={health?.model_available ? "health-ok" : "health-warn"}>{health?.model_available ? "Ready" : "Action needed"}</span></article><article><MemoryStick size={21} /><div><span>Available memory</span><strong>{health?.available_memory_gb.toFixed(2) || "—"} GiB</strong><small>{memoryDetail(health?.total_memory_gb)}</small></div></article><article><FileBarChart2 size={21} /><div><span>Power BI Desktop</span><strong>{health?.powerbi_desktop_available ? "Discovered" : "Not discovered"}</strong><small>{health?.powerbi_desktop_path || "Check the Store or MSI installation"}</small></div><span className={health?.powerbi_desktop_available ? "health-ok" : "health-warn"}>{health?.powerbi_desktop_version || "Required"}</span></article><article><Boxes size={21} /><div><span>Power BI validators</span><strong>{health?.pbi_cli_version || "pbi-cli unavailable"}</strong><small>Authoring {health?.authoring_cli_version || "unavailable"} · Bridge {health?.desktop_bridge_version || "unavailable"}</small></div></article><article><ShieldCheck size={21} /><div><span>Network policy</span><strong>Local AI inference</strong><small>Cloud inference disabled. Installing Qwen 4B downloads the pinned model file once; explicit database connections may access your configured data source.</small></div><span className="health-ok">Local AI</span></article><article><Database size={21} /><div><span>State storage</span><strong>Local application data</strong><small>{health?.state_root || "Checking…"}</small></div></article></div></section>;
}
