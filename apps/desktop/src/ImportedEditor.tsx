import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlignCenterHorizontal, AlignCenterVertical, AlignEndHorizontal, AlignEndVertical, AlignHorizontalDistributeCenter, AlignStartHorizontal, AlignStartVertical,
  AlignVerticalDistributeCenter, ArrowLeftRight, BookOpen, Group, History, Info, ListChecks, PanelRightClose, PanelRightOpen, Plus, Redo2, Share2, Undo2, Ungroup, Wand2, X,
} from "lucide-react";
import type { ConversationContext } from "./UnifiedWorkspace";
import type { ImportedReport } from "./ImportedReport";
import { InteractiveReportPage, type OverlayContext, type RenderVisual, type SelectMeta } from "./InteractiveReportPage";
import type { DataPointRef, DrillRequest } from "./reportActions";
import { ImportedOverlay, type InteractionsOverlay } from "./ImportedOverlay";
import { PageTabs } from "./PageTabs";
import { FormatPane } from "./FormatPane";
import { FieldsPane, AddVisualDialog } from "./FieldsPane";
import { HistoryPane } from "./HistoryPane";
import { MAX_PENDING_BATCH, PendingModelChanges } from "./PendingModelChanges";
import { DeliveryControls } from "./DeliveryControls";
import { EditProgress, useDelayedFlag } from "./EditProgress";
import { ProblemDialog } from "./EditorDialog";
import { SelectionList } from "./SelectionList";
import { BookmarksPane } from "./BookmarksPane";
import { SyncSlicersPane } from "./SyncSlicersPane";
import { TestsPane, PinTestDialog, useReportTests } from "./TestsPane";
import { ExplainPanel } from "./ExplainPanel";
import { FindingRow, ReviewPanel } from "./ReviewPanel";
import { CanvasContextMenu } from "./CanvasContextMenu";
import { InsertMenu } from "./InsertMenu";
import { MobileLayoutEditor } from "./MobileLayoutEditor";
import { fieldChoices, type FormatCatalog, type VisualInfo } from "./importedFormat";
import { describeReceipt, historyView, pendingStore, redoTarget, type Operation, type ThemePalette } from "./importedEditing";
import { hasNumbers } from "./formatExtras";
import {
  canFilterOthers, canGroup, contextMenuItems, groupOp, groupToUngroup, insertOp, interactionChoices, interactionOf, layerRows, menuOperations, placeNextToOp,
  readPaneMeta, setInteractionOp, swapOp, ungroupOp, type Bookmark, type InsertChoice, type PaneMeta, type Side,
} from "./importedPanes";
import { reviewDone, reviewFailed, reviewNeedsRerun, reviewStarted, visualTestBadges, type CellValue, type ReviewFinding, type ReviewState } from "./judgement";
import { judgementClient } from "./judgementApi";
import {
  alignRects, distributeRects, dropOverride, GestureCoalescer, isNudgeKey, nudgeRects, overrideRects, positionOps, settleOverride, stageOverride, type OverrideMap, type Rect,
} from "./importedGeometry";
import type { ImportedEditing } from "./useImportedEditing";
import { useRenderMeta } from "./useRenderMeta";
import "./imported-editor.css";

type Tab = "format" | "fields" | "bookmarks" | "selection" | "sync" | "tests" | "history";
const TABS: { id: Tab; label: string }[] = [
  { id: "format", label: "Visual" }, { id: "fields", label: "Fields" }, { id: "bookmarks", label: "Bookmarks" },
  { id: "selection", label: "Selection" }, { id: "sync", label: "Sync slicers" }, { id: "tests", label: "Tests" }, { id: "history", label: "History" },
];

const isEditable = (target: EventTarget | null) => target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));

/** A number the user asked about: the visual, and for a chart or table the data point and the number (from a right-click in reading view). */
type NumberTarget = { visual: string; values?: Record<string, CellValue>; measure?: string };

/**
 * The editor for an imported report (replaces the read-only inspector): toolbar with Undo, Redo, Add
 * visual, Insert, arranging, edit interactions, a page review and delivery, the page rail, the canvas with
 * selection, drag, resize, nudging and a context menu, and a side pane with Visual (conditional formatting first), Fields,
 * Bookmarks, Selection, Sync slicers, Tests and History. Studio does not change styling (colors, fonts, titles,
 * themes); the canvas still draws the report's own. Every change is one request to the edits API; gestures are
 * applied on screen at once and sent as a single batch when they settle.
 */
const PANE_KEY = "studio.importedEditor.paneOpen";

/** Whether the edit pane is open; remembered per browser, open when nothing is stored. */
function readPaneOpen(): boolean {
  try { return localStorage.getItem(PANE_KEY) !== "false"; } catch { return true; }
}

function writePaneOpen(open: boolean): void {
  try { localStorage.setItem(PANE_KEY, String(open)); } catch { /* Remembering the pane is optional. */ }
}

export function ImportedEditor({ report, active, appBusy, pageId, onPage, editing, formatKey, formatIds, onOpenDesktop, onDax, onAsk, onSelection, onReportChanged }: {
  report: ImportedReport; active: boolean; appBusy: boolean; pageId?: string; onPage: (id: string) => void;
  editing: ImportedEditing;
  /** Bumped with the head after a batch that only changed how things look: the canvas then skips the data queries. */
  formatKey: string; formatIds?: string[];
  onOpenDesktop: () => Promise<void>; onDax: (formula: string) => void;
  onAsk?: (context: ConversationContext) => void; onSelection?: (context: ConversationContext | undefined) => void;
  onReportChanged: () => Promise<void> | void;
}) {
  const { client, head, activeBusy: busy, dialog, answerDialog, lastReceipt, clearReceipt, history, loadHistory } = editing;
  const workspaceId = report.workspace_id;
  const page = report.pages.find(p => p.id === pageId) ?? report.pages[0];
  const readOnly = report.capabilities?.edit_report === false;
  const locked = busy !== null || appBusy || readOnly;
  const meta = useRenderMeta(workspaceId, page?.id, head?.id, active);
  const theme: ThemePalette | undefined = meta.theme ?? report.theme;
  // The pane facts (bookmarks, interactions, synced slicers, phone layout, layers) of the head: the formatting read that follows
  // every head is the source; the canvas' own page render stands in until it answers.
  const [rendered, setRendered] = useState<{ page: string; meta: PaneMeta }>();
  const pane = (meta.pageId === page?.id ? meta.pane : undefined) ?? (rendered?.page === page?.id ? rendered?.meta : undefined);

  const [tab, setTab] = useState<Tab>("format");
  const [paneOpen, setPaneOpen] = useState(readPaneOpen);
  const togglePane = (open: boolean) => { setPaneOpen(open); writePaneOpen(open); };
  const [selected, setSelected] = useState<string[]>([]);
  const selectedRef = useRef<string[]>([]);
  const selectionPage = useRef(page?.id);
  const [rows, setRows] = useState<Record<string, RenderVisual>>({});
  const [override, setOverride] = useState<OverrideMap>({});
  const layoutOverride = useMemo(() => overrideRects(override), [override]);
  const [catalogs, setCatalogs] = useState<Record<string, FormatCatalog | string>>({});
  const [adding, setAdding] = useState(false);
  const [announce, setAnnounce] = useState("");
  const [notice, setNotice] = useState<string>();
  // Round 3: reading view, the context menu, edit interactions, the judgement cards.
  const [reading, setReading] = useState(false);
  const [menu, setMenu] = useState<{ x: number; y: number }>();
  const [interactionsOn, setInteractionsOn] = useState(false);
  const [explain, setExplain] = useState<NumberTarget>();
  const [pin, setPin] = useState<NumberTarget>();
  const [review, setReview] = useState<ReviewState>();
  const [receiptFixed, setReceiptFixed] = useState<string[]>([]);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [bookmark, setBookmark] = useState<string>();
  const [drill, setDrill] = useState<{ pageId: string; request: DrillRequest }>();
  const root = useRef<HTMLDivElement>(null);
  const showProgress = useDelayedFlag(busy !== null, 450);
  const tests = useReportTests(workspaceId, head?.id, judgementClient, active);
  const badges = useMemo(() => visualTestBadges(tests.state), [tests.state]);

  // --- selection -------------------------------------------------------------------------------------------------------
  const layoutById = useMemo(() => new Map((page?.visuals ?? []).map(v => [v.id, v])), [page]);
  const captionOf = useCallback((id: string) => rows[id]?.title || layoutById.get(id)?.title || pane?.visuals[id]?.title || id, [rows, layoutById, pane]);
  const kindOf = useCallback((id: string) => rows[id]?.kind ?? pane?.visuals[id]?.kind ?? "", [rows, pane]);
  const serverRect = useCallback((id: string): Rect | undefined => {
    const position = rows[id]?.position;
    if (position) return { x: position.x, y: position.y, width: position.width, height: position.height };
    const item = layoutById.get(id);
    return item ? { x: item.x, y: item.y, width: item.width, height: item.height } : undefined;
  }, [rows, layoutById]);
  const effectiveRect = useCallback((id: string) => override[id]?.rect ?? serverRect(id), [override, serverRect]);

  const captionRef = useRef(captionOf);
  captionRef.current = captionOf;
  const setSelection = useCallback((next: string[]) => {
    selectedRef.current = next;
    setSelected(next);
    if (next.length) setAnnounce(`Selected ${next.length === 1 ? captionRef.current(next[0]) : `${next.length} visuals`}. Arrow keys move it, Shift with an arrow moves ten pixels, Delete removes it, Shift+F10 opens its menu.`);
    else setAnnounce("Nothing selected.");
  }, []);

  const applyEdit = useCallback(async (operation: Operation, label: string) => (await editing.submit([operation], { label: `${label}…` })).status === "applied", [editing]);
  const applyOperations = useCallback(async (operations: Operation[], label: string) => (await editing.submit(operations, { label: `${label}…` })).status === "applied", [editing]);

  const onSelectVisual = useCallback((id: string | null, meta: SelectMeta) => {
    const current = selectedRef.current;
    if (id === null) return setSelection(meta.ctrlKey ? current : []);
    if (meta.ctrlKey) return setSelection(current.includes(id) ? current.filter(item => item !== id) : [...current, id]);
    setSelection([id]);
  }, [setSelection]);

  useEffect(() => { selectionPage.current = page?.id; setSelection([]); setOverride({}); setRows({}); setExplain(undefined); setPin(undefined); setReview(undefined); setMenu(undefined); }, [page?.id, setSelection]);
  // A bookmark shown on one page, or a drillthrough into one, does not follow the user to another page.
  const bookmarksRef = useRef(pane?.bookmarks);
  bookmarksRef.current = pane?.bookmarks;
  const currentPage = page?.id;
  useEffect(() => {
    setBookmark(current => current && bookmarksRef.current?.find(item => item.name === current)?.page === currentPage ? current : undefined);
    setDrill(current => current && current.pageId === currentPage ? current : undefined);
  }, [currentPage]);
  // A visual that no longer exists (deleted, or undone away) cannot stay selected.
  useEffect(() => {
    if (!Object.keys(rows).length) return;
    const alive = selectedRef.current.filter(id => rows[id] || layoutById.has(id));
    if (alive.length !== selectedRef.current.length) setSelection(alive);
  }, [rows, layoutById, setSelection]);
  // A page change renders before its selection reset effect; an old ID never targets the new page.
  const primary = selectionPage.current === page?.id && layoutById.has(selected[0]) ? selected[0] : undefined;
  const primaryLabel = primary ? captionOf(primary) : "";
  const pageKey = page?.id;
  const pageLabel = page?.title;
  // The visible report page remains the conversation target when no visual is selected.
  useEffect(() => {
    if (!onSelection) return;
    onSelection(active && pageKey
      ? primary ? { page_id: pageKey, visual_id: primary, label: primaryLabel } : { page_id: pageKey, label: pageLabel || pageKey }
      : undefined);
  }, [active, primary, pageKey, primaryLabel, pageLabel, onSelection]);
  useEffect(() => () => onSelection?.(undefined), [onSelection]);

  // --- gestures: applied on screen at once, sent as one batch when they settle -------------------------------------------
  const commit = useCallback(async (changes: Record<string, Rect>) => {
    const ids = Object.keys(changes);
    const base: Record<string, Rect> = {};
    for (const id of ids) { const rect = serverRect(id); if (rect) base[id] = rect; }
    const operations = positionOps(base, changes);
    if (!operations.length) { setOverride(current => dropOverride(current, ids)); return; }
    const outcome = await editing.submit(operations, { label: operations.length > 1 ? `Moving ${operations.length} visuals` : "Moving the visual" });
    if (outcome.status !== "applied") {
      // Back to where the report has it, with a plain message: the canvas must not show a change that was not saved.
      setOverride(current => dropOverride(current, ids));
      if (outcome.status === "failed") setNotice("That change was not applied, so the visual is back where it was.");
    }
  }, [editing, serverRect]);
  const commitRef = useRef(commit);
  commitRef.current = commit;
  const coalescer = useRef<GestureCoalescer | null>(null);
  if (!coalescer.current) coalescer.current = new GestureCoalescer(changes => { void commitRef.current(changes); }, 2000);
  useEffect(() => () => coalescer.current?.flushNow(), [page?.id]);

  const preview = useCallback((rects: Record<string, Rect>) => { setOverride(current => stageOverride(current, rects, serverRect)); }, [serverRect]);
  const settle = useCallback((rects: Record<string, Rect>) => { coalescer.current?.stage(rects, "now"); }, []);
  const abort = useCallback((ids: string[]) => { setOverride(current => dropOverride(current, ids)); }, []);

  // --- history: Undo on the newest change only, Redo of what an Undo took back ---------------------------------------------
  const headId = head?.id ?? "";
  useEffect(() => { void loadHistory(); }, [loadHistory, headId]);
  const rowsView = useMemo(() => historyView(history, headId), [history, headId]);
  const canUndo = !locked && rowsView.some(row => row.canUndo);
  const redoId = redoTarget(history, headId);
  const undo = useCallback(() => { if (canUndo) void editing.undo().then(() => loadHistory()); }, [canUndo, editing, loadHistory]);
  const redo = useCallback(() => { if (!locked && redoId) void editing.restore(redoId, true).then(() => loadHistory()); }, [locked, redoId, editing, loadHistory]);

  // Align and distribute move several visuals at once: one batch, like a drag.
  const arrange = (how: "left" | "center" | "right" | "top" | "middle" | "bottom" | "horizontal" | "vertical") => {
    const rects: Record<string, Rect> = {};
    for (const id of selectedRef.current) { const rect = effectiveRect(id); if (rect) rects[id] = rect; }
    const next = how === "horizontal" || how === "vertical" ? distributeRects(rects, selectedRef.current, how) : alignRects(rects, selectedRef.current, how);
    if (!Object.keys(next).length) return;
    setOverride(current => stageOverride(current, next, serverRect));
    coalescer.current?.stage(next, "now");
  };

  // --- the canvas context menu -----------------------------------------------------------------------------------------------
  const layers = useMemo(() => pane?.visuals ?? {}, [pane]);
  const menuItems = useMemo(() => contextMenuItems(selected, layers, { number: selected.length === 1 && hasNumbers(kindOf(selected[0])) && Boolean(rows[selected[0]]), locked, testsAvailable: !report.capabilities?.thin_report }), [selected, layers, kindOf, rows, locked, report.capabilities?.thin_report]);
  const openMenuAt = useCallback((visual: string | undefined, at: { x: number; y: number }) => {
    if (!visual) { setMenu(undefined); return; }
    if (!selectedRef.current.includes(visual)) setSelection([visual]);
    setMenu(at);
  }, [setSelection]);
  const chooseMenu = (choice: string) => {
    setMenu(undefined);
    const ids = selectedRef.current;
    if (!ids.length) return;
    if (choice === "explain") { setExplain({ visual: ids[0] }); return; }
    if (choice === "pin") { setPin({ visual: ids[0] }); return; }
    if (choice === "interactions") { setInteractionsOn(true); return; }
    const operations = menuOperations(choice, ids, layers);
    if (!operations) return;
    coalescer.current?.flushNow();
    if (choice === "delete") setSelection([]);
    void applyOperations(operations, choice.startsWith("align") || choice.startsWith("distribute") ? "Arranging" : choice === "delete" ? "Deleting" : choice === "duplicate" ? "Duplicating" : choice.startsWith("place") ? "Placing the visual" : choice === "swap" ? "Swapping" : choice === "group" ? "Grouping" : choice === "ungroup" ? "Ungrouping" : "Reordering");
  };

  // --- keyboard ------------------------------------------------------------------------------------------------------------
  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || isEditable(event.target) || document.querySelector(".ie-dialog-backdrop, .ie-menu-root")) return;
      const target = event.target instanceof Element ? event.target : null;
      const inEditor = target ? root.current?.contains(target) : false;
      // Not `document.body`: with focus on plain text elsewhere in the app, Delete and the arrow keys are not aimed at the canvas.
      const onCanvas = target ? Boolean(target.closest(".original-canvas-frame")) : false;
      const ctrl = event.ctrlKey || event.metaKey;
      if (ctrl && !event.altKey && inEditor && (event.key === "z" || event.key === "Z") && !event.shiftKey) { event.preventDefault(); undo(); return; }
      if (ctrl && !event.altKey && inEditor && (event.key === "y" || event.key === "Y" || ((event.key === "z" || event.key === "Z") && event.shiftKey))) { event.preventDefault(); redo(); return; }
      if (event.key === "Escape" && inEditor && interactionsOn) { event.preventDefault(); setInteractionsOn(false); return; }
      if (!onCanvas || locked) return;
      const ids = selectedRef.current;
      if (event.key === "Escape") { coalescer.current?.flushNow(); setSelection([]); return; }
      if (!ids.length) return;
      if ((event.key === "F10" && event.shiftKey) || event.key === "ContextMenu") {
        event.preventDefault();
        const frame = document.querySelector(`[data-visual-id="${CSS.escape(ids[0])}"]`)?.getBoundingClientRect();
        setMenu({ x: frame ? frame.left + 12 : 200, y: frame ? frame.top + 12 : 200 });
        return;
      }
      if (ctrl && (event.key === "g" || event.key === "G")) {
        event.preventDefault();
        if (event.shiftKey) { const group = groupToUngroup(ids, layers); if (group) void applyEdit(ungroupOp(group), "Ungrouping"); }
        else if (canGroup(ids, layers)) void applyEdit(groupOp(ids), "Grouping");
        return;
      }
      if (isNudgeKey(event.key) && !ctrl && !event.altKey && page) {
        event.preventDefault();
        const start: Record<string, Rect> = {};
        for (const id of ids) { const rect = effectiveRect(id); if (rect) start[id] = rect; }
        const next = nudgeRects(start, ids, event.key, event.shiftKey, { width: page.width, height: page.height });
        setOverride(current => stageOverride(current, next, serverRect));
        coalescer.current?.stage(next, "idle");
      } else if (event.key === "Delete") {
        event.preventDefault();
        coalescer.current?.flushNow();
        void editing.submit(ids.map(visual => ({ op: "delete_visual", visual })), { label: ids.length > 1 ? "Deleting the visuals" : "Deleting the visual" });
        setSelection([]);
      } else if (ctrl && (event.key === "d" || event.key === "D")) {
        event.preventDefault();
        coalescer.current?.flushNow();
        void editing.submit(ids.map(visual => ({ op: "duplicate_visual", visual })), { label: "Duplicating" });
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [active, locked, page, editing, effectiveRect, serverRect, undo, redo, setSelection, interactionsOn, layers, applyEdit]);

  // --- after a change --------------------------------------------------------------------------------------------------------
  const onRendered = useCallback((visuals: RenderVisual[]) => {
    setRows(Object.fromEntries(visuals.map(visual => [visual.id, visual])));
    // An optimistic rectangle goes once the render shows its visual somewhere other than where the gesture began.
    setOverride(current => settleOverride(current, Object.fromEntries(visuals.map(visual => [visual.id, visual.position ? { x: visual.position.x, y: visual.position.y, width: visual.position.width, height: visual.position.height } : undefined]))));
  }, []);

  useEffect(() => { if (lastReceipt) { setNotice(undefined); setReceiptFixed([]); } }, [lastReceipt]);

  // --- the format catalog of the selected visual's type ------------------------------------------------------------------------
  const selectedRow = primary ? rows[primary] : undefined;
  const visualType = selectedRow?.visual_type ?? (primary ? layoutById.get(primary)?.visual_type : undefined);
  useEffect(() => {
    if (!visualType || catalogs[visualType] !== undefined) return;
    let cancelled = false;
    client.formatCatalog(workspaceId, visualType)
      .then(catalog => { if (!cancelled) setCatalogs(current => ({ ...current, [visualType]: catalog })); })
      .catch(reason => { if (!cancelled) setCatalogs(current => ({ ...current, [visualType]: reason instanceof Error ? reason.message : "The full list of settings could not be loaded." })); });
    return () => { cancelled = true; };
  }, [visualType, catalogs, client, workspaceId]);
  const catalogEntry = visualType ? catalogs[visualType] : undefined;
  const catalog = typeof catalogEntry === "object" ? catalogEntry : undefined;
  const catalogError = typeof catalogEntry === "string" ? catalogEntry : undefined;

  const info: VisualInfo | undefined = selectedRow ? {
    id: selectedRow.id, kind: selectedRow.kind, visualType: selectedRow.visual_type ?? visualType ?? "", title: selectedRow.title,
    rect: effectiveRect(selectedRow.id), hidden: selectedRow.hidden_own, formatting: selectedRow.formatting as Record<string, unknown> | undefined, columns: selectedRow.columns, rows: selectedRow.rows, unrendered: selectedRow.unrendered,
  } : undefined;

  const choices = useMemo(() => fieldChoices(report.tables, report.measures), [report.tables, report.measures]);
  const listed = useMemo(() => (page?.visuals ?? []).map(v => ({ id: v.id, title: rows[v.id]?.title || v.title || v.id, kind: rows[v.id]?.kind ?? v.visual_type, hidden: Boolean(rows[v.id]?.hidden_own) })), [page, rows]);
  const layerList = useMemo(() => layerRows(layers, listed), [layers, listed]);
  const titles = useMemo(() => Object.fromEntries([...selected, ...Object.keys(layers)].map(id => [id, captionOf(id)])), [selected, layers, captionOf]);
  const measureRefs = useMemo(() => report.measures.map(m => ({ table: m.TableName, name: m.Name })), [report.measures]);
  const columnRefs = useMemo(() => report.tables.map(t => ({ table: t.TableName, name: t.ColumnName })), [report.tables]);

  const pages = useMemo(() => report.pages.map(item => item.id === page?.id && meta.pageHidden !== undefined ? { ...item, hidden: meta.pageHidden } : item), [report.pages, page?.id, meta.pageHidden]);
  const receipt = lastReceipt ? describeReceipt(lastReceipt) : undefined;
  const receiptIsHead = lastReceipt && head && lastReceipt.head.id === head.id;

  // --- edit interactions ---------------------------------------------------------------------------------------------------------
  const interactionSource = interactionsOn && primary && canFilterOthers(kindOf(primary)) ? primary : undefined;
  const interactionOverlay: InteractionsOverlay | undefined = interactionSource ? {
    source: interactionSource,
    choices: target => interactionChoices(kindOf(target)),
    current: target => interactionOf(pane?.interactions ?? {}, interactionSource, target),
    onSet: (target, type) => { void applyEdit(setInteractionOp(interactionSource, target, type), "Changing the interaction"); },
  } : undefined;

  // --- judgement: review, fixes, explain, pin -----------------------------------------------------------------------------------
  const fix = useCallback(async (finding: ReviewFinding) => {
    if (!finding.fix?.length) return false;
    return (await editing.submit(finding.fix, { label: finding.fixSummary ?? "Fixing what Studio noticed" })).status === "applied";
  }, [editing]);
  // The review is always asked for the head the editor has at that instant (`headRef`, not a render's closure): right after a
  // Fix the closure still holds the old head and the server answers 409 head_moved. A head that moved re-asks for the new
  // one; the effect below re-reviews whenever the head changes while the panel is open (its own Fix, or any other edit).
  const reviewPageId = page?.id;
  const runReview = useCallback(async (attempt = 0): Promise<void> => {
    if (!reviewPageId) return;
    const sent = editing.headRef.current?.id;
    setReview(current => reviewStarted(current, sent));
    try {
      const found = await judgementClient.review(workspaceId, reviewPageId, sent, true);
      setReview(current => current ? reviewDone(current, found, sent) : current);
    } catch (reason) {
      const now = editing.headRef.current?.id;
      const { state, retry } = reviewFailed(undefined, reason, sent, now);
      if (retry && attempt < 2) { await runReview(attempt + 1); return; }
      // No newer head known yet: the panel waits (still loading) and the effect asks again when the head arrives.
      setReview(current => current ? { ...state, review: current.review, fixed: current.fixed, head: current.head } : current);
    }
  }, [reviewPageId, workspaceId, editing.headRef]);
  useEffect(() => { if (reviewNeedsRerun(review, head?.id)) void runReview(); }, [review, head?.id, runReview]);
  const insert = (choice: InsertChoice) => { if (page) void applyEdit(insertOp(page.id, choice), choice.kind === "button" ? "Adding a button" : choice.kind === "navigator" ? "Adding a navigator" : choice.kind === "shape" ? "Adding a shape" : "Adding a text box"); };
  const applyBookmark = (target: Bookmark | undefined) => {
    if (!target) { setBookmark(undefined); return; }
    if (target.page && target.page !== page?.id && target.captures.page) onPage(target.page);
    setBookmark(target.name);
  };
  // Reading view: a data point's right-click menu also explains or pins that number.
  const pointActions = useCallback((point: DataPointRef, visual: RenderVisual) => {
    if (!hasNumbers(visual.kind)) return [];
    const column = point.column !== null ? visual.columns[point.column] : undefined;
    const measure = column && column.kind === "measure" ? column.query_ref ?? column.name : undefined;
    const values = point.values as Record<string, CellValue>;
    return [
      { label: "Explain this number", run: () => setExplain({ visual: visual.id, values, measure }) },
      { label: "Pin this number as a test", run: () => setPin({ visual: visual.id, values, measure }), disabled: report.capabilities?.thin_report === true },
    ];
  }, [report.capabilities?.thin_report]);

  const explainRow = explain ? rows[explain.visual] : undefined;
  const pinRow = pin ? rows[pin.visual] : undefined;

  if (!page) return <p className="ie-empty">This report has no pages.</p>;
  const ungroupTarget = groupToUngroup(selected, layers);
  return <div className="ie-editor" ref={root} aria-busy={busy !== null}>
    <div className="ie-toolbar" role="toolbar" aria-label="Report editing">
      <button type="button" className="secondary" disabled={!canUndo} title="Undo the last change (Ctrl+Z)" onClick={undo}><Undo2 size={14} />Undo</button>
      <button type="button" className="secondary" disabled={locked || !redoId} title="Redo (Ctrl+Y)" onClick={redo}><Redo2 size={14} />Redo</button>
      <button type="button" className="secondary" aria-pressed={tab === "history"} title="Every change, newest first" onClick={() => { setTab("history"); togglePane(true); }}><History size={14} />History</button>
      <span className="ie-divider" aria-hidden="true" />
      <button type="button" className="secondary" disabled={locked || reading} onClick={() => setAdding(true)}><Plus size={14} />Add visual</button>
      <InsertMenu disabled={locked || reading} onInsert={insert} />
      <button type="button" className="secondary" aria-pressed={interactionsOn} disabled={reading} title="Edit interactions: with a visual selected, choose how each other visual reacts to it" onClick={() => setInteractionsOn(on => !on)}><Share2 size={14} />Edit interactions</button>
      <button type="button" className="secondary" disabled={!page} title="What a Power BI developer would notice on this page" onClick={() => void runReview()}><ListChecks size={14} />Review this page</button>
      <button type="button" className="secondary" aria-pressed={reading} title="Reading view: buttons, bookmarks, drillthrough and tooltips work as in Power BI" onClick={() => { if (reading) { setBookmark(undefined); setDrill(undefined); } setReading(on => !on); setInteractionsOn(false); setMenu(undefined); }}><BookOpen size={14} />Reading view</button>
      {selected.length >= 2 && !reading && <div className="ie-align" role="group" aria-label="Arrange the selected visuals">
        {([["left", "Align left", AlignStartVertical], ["center", "Align centers", AlignCenterVertical], ["right", "Align right", AlignEndVertical], ["top", "Align top", AlignStartHorizontal], ["middle", "Align middles", AlignCenterHorizontal], ["bottom", "Align bottom", AlignEndHorizontal]] as const).map(([how, label, Icon]) =>
          <button type="button" key={how} className="icon-button" disabled={locked} aria-label={label} title={label} onClick={() => arrange(how)}><Icon size={15} /></button>)}
        <button type="button" className="icon-button" disabled={locked || selected.length < 3} aria-label="Distribute horizontally" title="Distribute horizontally (three or more)" onClick={() => arrange("horizontal")}><AlignHorizontalDistributeCenter size={15} /></button>
        <button type="button" className="icon-button" disabled={locked || selected.length < 3} aria-label="Distribute vertically" title="Distribute vertically (three or more)" onClick={() => arrange("vertical")}><AlignVerticalDistributeCenter size={15} /></button>
        <button type="button" className="icon-button" disabled={locked || !canGroup(selected, layers)} aria-label="Group" title="Group (Ctrl+G)" onClick={() => void applyEdit(groupOp(selected), "Grouping")}><Group size={15} /></button>
        {selected.length === 2 && <button type="button" className="icon-button" disabled={locked} aria-label="Swap positions" title="Swap their positions" onClick={() => void applyEdit(swapOp(selected[0], selected[1]), "Swapping")}><ArrowLeftRight size={15} /></button>}
        {selected.length === 2 && <select aria-label="Place the first selected visual next to the second" className="ie-place" value="" disabled={locked} onChange={event => { if (event.target.value) void applyEdit(placeNextToOp(selected[0], selected[1], event.target.value as Side), "Placing the visual"); }}>
          <option value="">Place next to…</option><option value="left_of">To the left of the other</option><option value="right_of">To the right of the other</option><option value="above">Above the other</option><option value="below">Below the other</option></select>}
      </div>}
      {ungroupTarget && !reading && <button type="button" className="secondary" disabled={locked} title="Ungroup (Ctrl+Shift+G)" onClick={() => void applyEdit(ungroupOp(ungroupTarget), "Ungrouping")}><Ungroup size={14} />Ungroup</button>}
      <DeliveryControls workspaceId={workspaceId} reportName={report.name} delivery={report.delivery} client={client} disabled={locked} onChanged={onReportChanged} />
    </div>
    {readOnly && <p className="ie-banner" role="status"><Info size={14} aria-hidden="true" />Editing is not available for this report{report.capabilities?.reasons.length ? `: ${report.capabilities.reasons.join("; ")}` : "."}</p>}
    {report.capabilities?.thin_report && <p className="ie-banner" role="note"><Info size={14} aria-hidden="true" />This report reads its data from a shared model, so Studio cannot preview its data or edit its measures. Layout and formatting changes are saved, but check them in Power BI.</p>}
    {reading && <p className="ie-banner" role="status"><BookOpen size={14} aria-hidden="true" />Reading view: buttons, bookmarks, drillthrough and tooltips work as they do in Power BI. Right-click a number to explain it or pin it as a test.
      <button type="button" className="secondary" onClick={() => { setReading(false); setBookmark(undefined); setDrill(undefined); }}>Back to editing</button></p>}
    {bookmark && !reading && <p className="ie-banner" role="status"><Info size={14} aria-hidden="true" />Previewing the bookmark “{pane?.bookmarks.find(item => item.name === bookmark)?.displayName ?? bookmark}”. Edits still change the page itself.
      <button type="button" className="secondary" onClick={() => setBookmark(undefined)}>Back to the page</button></p>}
    {interactionsOn && !reading && <p className="ie-banner" role="status"><Share2 size={14} aria-hidden="true" />{interactionSource
      ? `When a data point of ${captionOf(interactionSource)} is selected, the other visuals react as their buttons show: filter, highlight or none.`
      : "Select a slicer, chart or table to edit how the other visuals react to it."}
      <button type="button" className="secondary" disabled={locked || !interactionSource} title="Back to Power BI's defaults for this visual" onClick={() => interactionSource && void applyEdit({ op: "reset_interactions", visual: interactionSource }, "Resetting the interactions")}>Reset</button>
      <button type="button" className="icon-button" aria-label="Stop editing interactions" onClick={() => setInteractionsOn(false)}><X size={13} /></button></p>}
    {showProgress && busy && <EditProgress label={busy.label} stage={busy.stage} desktop={busy.desktop} />}
    {receipt && lastReceipt && <div className={`ie-receipt ${receipt.tone}`} role="status">
      <div><strong>{receipt.headline}</strong>{receipt.lines.length > 0 && <ul>{receipt.lines.map((line, index) => <li key={index}>{line}</li>)}</ul>}
        {receipt.warnings.map(warning => <p key={warning} className="ie-warning">{warning}</p>)}
        {receipt.tests.map(line => <p key={line} className="ie-note">{line}</p>)}
        {receipt.health.length > 0 && <p className="ie-note">Model health: {receipt.health.join(" ")}</p>}
        {receipt.chips.length > 0 && <div className="ie-chips">{receipt.chips.map(chip => <span key={chip.label} className="ie-chip">{chip.label}</span>)}</div>}
        {receipt.desktopNote && <small>{receipt.desktopNote}</small>}
        {lastReceipt.review && lastReceipt.review.length > 0 && <div className="ie-findings"><strong><Wand2 size={12} aria-hidden="true" /> Studio noticed</strong>
          <ul className="ie-finding-list">{lastReceipt.review.map(finding => <FindingRow key={finding.id} finding={finding} fixed={receiptFixed.includes(finding.id)} disabled={locked}
            onFix={item => { void fix(item).then(done => { if (done) setReceiptFixed(current => [...current, item.id]); }); }} onShow={ids => setSelection(ids.filter(id => rows[id] || layoutById.has(id)))} />)}</ul></div>}</div>
      <div className="ie-receipt-actions">
        <button type="button" className="secondary" disabled={!canUndo || !receiptIsHead} onClick={undo}><Undo2 size={13} />Undo</button>
        <button type="button" className="secondary" title="Review the whole page" onClick={() => void runReview()}><ListChecks size={13} />Review this page</button>
        <button type="button" className="icon-button" aria-label="Dismiss" onClick={clearReceipt}><X size={14} /></button></div>
    </div>}
    {notice && <p className="ie-banner warn" role="status">{notice}<button type="button" className="icon-button" aria-label="Dismiss" onClick={() => setNotice(undefined)}><X size={13} /></button></p>}
    <PendingModelChanges workspaceId={workspaceId} capabilities={report.capabilities} disabled={locked} onDax={onDax}
      onApply={changes => {
        // The server takes at most 50 operations per batch: the first 50 go now, the rest stay queued for the next Apply.
        const batch = changes.slice(0, MAX_PENDING_BATCH);
        void editing.submit(batch.map(change => change.op), { confirm_model: true, label: "Applying your changes in Power BI" }).then(outcome => { if (outcome.status === "applied") batch.forEach(change => pendingStore.remove(workspaceId, change.id)); });
      }} />
    <div className={`imported-layout ie-layout${paneOpen ? "" : " pane-collapsed"}`}>
      <PageTabs pages={pages} pageId={page.id} disabled={locked} onSelect={id => { setDrill(undefined); onPage(id); }} onEdit={applyOperations}
        pageType={page.page_type ?? pane?.pageType} binding={pane?.binding} columns={columnRefs} onMobile={() => setMobileOpen(true)} />
      <div className="imported-canvas-workspace ie-canvas">
        <InteractiveReportPage key={`${report.id}:${page.id}`} workspaceId={workspaceId} pageId={page.id} active={active} onOpenDesktop={onOpenDesktop} layout={page}
          head={head} mode={reading ? "view" : "edit"} selectedVisualId={primary} onSelectVisual={onSelectVisual} layoutOverride={layoutOverride}
          formattingOnlyKey={formatKey} formattingOnlyIds={formatIds} onHeadMoved={() => { void onReportChanged(); }} onVisualsRendered={onRendered}
          onNavigate={id => onPage(id)} onBookmark={name => setBookmark(name)} onDrillthrough={(id, request) => setDrill({ pageId: id, request })}
          onPageRendered={result => setRendered({ page: page.id, meta: readPaneMeta(result) })}
          bookmark={bookmark} drill={drill?.pageId === page.id ? drill.request.drill : undefined} drillSource={drill?.pageId === page.id ? drill.request.drill_source : undefined}
          pointActions={pointActions}
          overlay={reading ? undefined : (context: OverlayContext) => <ImportedOverlay context={context} selectedIds={selected} selectionRef={selectedRef} locked={locked} onPreview={preview} onCommit={settle} onAbort={abort} titles={titles}
            interactions={interactionOverlay} badges={badges} onContextMenu={openMenuAt} />} />
        <div className="sr-only" role="status" aria-live="polite">{announce}</div>
        {explain && explainRow && <ExplainPanel key={`${explain.visual}:${JSON.stringify(explain.values ?? {})}`} workspaceId={workspaceId} pageId={page.id} visual={explainRow} client={judgementClient}
          initial={{ values: explain.values, measure: explain.measure }} onClose={() => setExplain(undefined)} />}
        {review && !explain && <ReviewPanel review={review.review} loading={review.loading} error={review.error} fixed={review.fixed} disabled={locked}
          onFix={finding => { void fix(finding).then(done => { if (done) setReview(current => current ? { ...current, fixed: [...current.fixed, finding.id] } : current); }); }}
          onShow={ids => setSelection(ids.filter(id => rows[id] || layoutById.has(id)))} onRefresh={() => void runReview()} onClose={() => setReview(undefined)} />}
      </div>
      {!paneOpen ? <aside className="ie-side collapsed" aria-label="Edit pane (collapsed)">
        <button type="button" className="icon-button ie-pane-toggle" aria-label="Show the edit pane" aria-expanded="false" title="Show the edit pane" onClick={() => togglePane(true)}><PanelRightOpen size={16} /></button>
        <span className="ie-pane-rail-label" aria-hidden="true">{TABS.find(item => item.id === tab)?.label ?? "Visual"}</span>
      </aside> : <aside className="ie-side" aria-label="Edit pane">
        <div className="ie-pane-head">
          <button type="button" className="icon-button ie-pane-toggle" aria-label="Hide the edit pane" aria-expanded="true" title="Hide the edit pane" onClick={() => togglePane(false)}><PanelRightClose size={16} /></button>
        </div>
        <div className="ie-tabs" role="tablist" aria-label="Edit pane sections">
          {TABS.map(item => <button type="button" key={item.id} role="tab" id={`ie-tab-${item.id}`} aria-selected={tab === item.id} aria-controls={`ie-panel-${item.id}`} tabIndex={tab === item.id ? 0 : -1} onClick={() => setTab(item.id)}
            onKeyDown={event => { const at = TABS.findIndex(t => t.id === tab); if (event.key === "ArrowRight" || event.key === "ArrowLeft") { event.preventDefault(); const next = TABS[(at + (event.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length]; setTab(next.id); requestAnimationFrame(() => document.getElementById(`ie-tab-${next.id}`)?.focus()); } }}>
            {item.label}{item.id === "tests" && tests.state.tests.length > 0 && <span className={`ie-tab-count${[...badges.values()].includes("failed") ? " failed" : ""}`}>{tests.state.tests.length}</span>}</button>)}
        </div>
        <div className="ie-panel" role="tabpanel" id={`ie-panel-${tab}`} aria-labelledby={`ie-tab-${tab}`}>
          {tab === "format" && (primary && !info ? <p className="ie-empty">Loading this visual…</p> : <FormatPane info={info} catalog={catalog} catalogError={catalogError} theme={theme} disabled={locked} onEdit={applyEdit}
            context={{
              row: selectedRow, measures: measureRefs, pages: report.pages.filter(item => item.id !== page.id).map(item => ({ id: item.id, title: item.title })),
              tooltipPages: report.pages.filter(item => item.id !== page.id && item.page_type === "tooltip").map(item => ({ id: item.id, title: item.title })),
              altText: primary ? layoutById.get(primary)?.alt_text ?? undefined : undefined,
              bookmarks: pane?.bookmarks ?? [], drillthroughs: pane?.drillthroughs ?? [], layer: primary ? layers[primary] : undefined,
            }} />)}
          {tab === "fields" && <FieldsPane info={info} catalog={catalog} choices={choices} bindings={primary ? layoutById.get(primary)?.bindings : undefined} measures={report.measures} disabled={locked} onEdit={applyEdit} onDax={onDax}
            onAsk={onAsk && primary ? () => onAsk({ page_id: page.id, visual_id: primary, label: captionOf(primary) }) : undefined} />}
          {tab === "bookmarks" && <BookmarksPane bookmarks={pane?.bookmarks ?? []} pageId={page.id} selected={selected} visuals={layers} disabled={locked} onEdit={applyOperations} onApply={applyBookmark} applied={bookmark} />}
          {tab === "selection" && <SelectionList visuals={listed} layers={layerList} selectedIds={selected} disabled={locked} bookmarks={pane?.bookmarks ?? []}
            onSelect={(id, additive) => onSelectVisual(id, { altKey: false, ctrlKey: additive, shiftKey: false })}
            onHidden={(id, hidden) => applyEdit({ op: "set_visual_hidden", visual: id, hidden }, hidden ? "Hiding" : "Showing")}
            onArrange={(id, to) => applyEdit({ op: "arrange", visual: id, to }, "Reordering")}
            onBookmarkHidden={(name, id, hidden) => applyEdit({ op: "set_bookmark_visual_hidden", bookmark: name, visual: id, hidden }, hidden ? "Hiding it in the bookmark" : "Showing it in the bookmark")}
            onGroup={canGroup(selected, layers) ? () => void applyEdit(groupOp(selected), "Grouping") : undefined}
            onUngroup={ungroupTarget ? () => void applyEdit(ungroupOp(ungroupTarget), "Ungrouping") : undefined}
            onTabOrder={() => void applyEdit({ op: "set_tab_order", page: page.id, by_position: true }, "Setting the tab order")} />}
          {tab === "sync" && <SyncSlicersPane pageId={page.id} pages={report.pages.map(item => ({ id: item.id, title: item.title }))} visuals={layers} groups={pane?.syncGroups ?? {}} disabled={locked} onEdit={applyOperations} />}
          {tab === "tests" && <TestsPane workspaceId={workspaceId} pageId={page.id} tests={tests} client={judgementClient} measures={measureRefs} columns={columnRefs} disabled={locked} />}
          {tab === "history" && <HistoryPane workspaceId={workspaceId} editing={editing} disabled={locked} />}
        </div>
      </aside>}
    </div>
    {menu && <CanvasContextMenu x={menu.x} y={menu.y} items={menuItems} onChoose={chooseMenu} onClose={() => setMenu(undefined)} />}
    {pin && pinRow && <PinTestDialog key={pin.visual} workspaceId={workspaceId} pageId={page.id} visual={pinRow} client={judgementClient} initial={{ values: pin.values, measure: pin.measure }}
      onCancel={() => setPin(undefined)} onPinned={message => { setPin(undefined); setNotice(message); void tests.reload(); }} />}
    {mobileOpen && <MobileLayoutEditor pageId={page.id} pageTitle={page.title} visuals={layers} mobile={pane?.mobile} disabled={locked} onEdit={applyOperations} onClose={() => setMobileOpen(false)} />}
    {adding && <AddVisualDialog choices={choices} pageId={page.id} pageSize={{ width: page.width, height: page.height }}
      existing={(page.visuals ?? []).map(v => effectiveRect(v.id)).filter((rect): rect is Rect => Boolean(rect))} onCancel={() => setAdding(false)}
      onAdd={(operation, label) => { setAdding(false); applyEdit(operation, label); }} />}
    {dialog && <ProblemDialog problem={dialog.problem} ui={dialog.ui} onDecide={answerDialog} />}
  </div>;
}
