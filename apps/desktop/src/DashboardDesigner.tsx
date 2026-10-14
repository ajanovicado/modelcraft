import { useEffect, useMemo, useReducer, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { createPortal } from "react-dom";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  BarChart3,
  Copy,
  Gauge,
  LayoutDashboard,
  LineChart,
  Plus,
  Save,
  Table2,
  Trash2,
  Undo2,
  Redo2,
  RotateCcw,
  Maximize2,
  GripVertical,
  MoveDiagonal2,
  X,
  CheckCircle2,
  CalendarRange,
} from "lucide-react";
import type { Draft, Snapshot, Visual, ReportAction } from "./types";
import { DesignerVisualPreview } from "./DesignerVisualPreview";
import { ForecastNumberInput } from "./ForecastNumberInput";
import { AskAiButton } from "./AskAiButton";
import "./DashboardDesigner.css";
import { editHistory, type EditHistory } from "./editHistory";
import { arrangeVisuals, canPlace, clampPlacement, findOpenPlacement, insertVisual, minimumVisualSize, overlaps, type Placement } from "./reportLayout";
import { ReportDateFilter } from "./ReportDateFilter";
import { ReportToolbar } from "./ReportToolbar";
import { ReportVisualDialog } from "./ReportVisualDialog";
import { visualForecastCapability, visualTypesByRecipe } from "./reportVisualCatalog";
import "./report-workbench.css";

type DashboardPage = Draft["pages"][number];
type ProfiledTable = Snapshot["tables"][number]["table"];

export type DashboardDesignerProps = {
  draft: Draft;
  snapshot: Snapshot;
  busy: boolean;
  onSave: (pages: DashboardPage[]) => Promise<void> | void;
  onContext?: (context: { page_id: string; visual_id?: string; label: string }) => void;
  onDirtyChange?: (dirty: boolean) => void;
  toolbarTarget?: HTMLElement | null;
  reportAction?: ReportAction & { requestId: string };
  onSelection?: (context: { page_id: string; visual_id?: string; label: string }) => void;
};

const defaultVisualType = (recipe: Visual["recipe"]) => visualTypesByRecipe[recipe][0].id;
const visualTypeLabel = (visual: Visual) => visualTypesByRecipe[visual.recipe].find((item) => item.id === visual.visual_type)?.label || visual.visual_type;

const labelByRecipe: Record<Visual["recipe"], string> = {
  kpi: "KPI card",
  trend: "Trend line",
  category: "Category chart",
  detail: "Detail table",
};

const iconByRecipe = {
  kpi: Gauge,
  trend: LineChart,
  category: BarChart3,
  detail: Table2,
};

const clonePages = (pages: DashboardPage[]) => structuredClone(pages);
const safeId = (value: string) => value.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 70) || "item";

function uniqueId(base: string, used: Set<string>) {
  let candidate = safeId(base);
  let suffix = 2;
  while (used.has(candidate)) candidate = `${safeId(base)}_${suffix++}`;
  used.add(candidate);
  return candidate;
}

const isGroupedRecipe = (recipe: Visual["recipe"]) => recipe === "trend" || recipe === "category";
const forecastSettings = visualForecastCapability.settings;

const categoryColumnsFor = (table: ProfiledTable | undefined, recipe: Visual["recipe"]) => {
  if (!table || !isGroupedRecipe(recipe)) return [];
  return table.columns.filter((column) => recipe === "trend"
    ? ["date", "datetime"].includes(column.data_type)
    : column.data_type === "string" && column.distinct_count > 1 && column.distinct_count <= 100 && !column.candidate_key);
};

export function DashboardDesigner({ draft, snapshot, busy, onSave, onContext, onDirtyChange, toolbarTarget, reportAction, onSelection }: DashboardDesignerProps) {
  const [history, dispatchHistory] = useReducer(editHistory<DashboardPage[]>, { past: [], present: clonePages(draft.pages), future: [] } as EditHistory<DashboardPage[]>);
  const pages = history.present;
  const setPages = (value: DashboardPage[] | ((current: DashboardPage[]) => DashboardPage[]), group?: string) => dispatchHistory({ type: "edit", value, group });
  const [activePageId, setActivePageId] = useState(draft.pages[0]?.id || "");
  const [activeVisualId, setActiveVisualId] = useState("");
  const [dateFilterTarget, setDateFilterTarget] = useState<{ visualId?: string }>();
  useEffect(() => {
    if (reportAction?.kind === "visual_date_filter") setDateFilterTarget({ visualId: reportAction.visual_id });
  }, [reportAction]);
  const [showVisuals, setShowVisuals] = useState(false);
  const [showGrid, setShowGrid] = useState(false);
  const [layoutMessage, setLayoutMessage] = useState("");
  const [fieldSearch, setFieldSearch] = useState("");
  const [focusView, setFocusView] = useState<{ id: string; view: "visual" | "table" }>();
  const [gesture, setGesture] = useState<{ id: string; place: Placement; valid: boolean }>();
  const gestureRef = useRef<{ id: string; start: Visual; x: number; y: number; cellWidth: number; rowStarts: number[]; resize: boolean; place: Placement } | undefined>(undefined);
  const canvasRef = useRef<HTMLDivElement>(null);
  const previousDraft = useRef(draft);
  const [saveSubmitting, setSaveSubmitting] = useState(false);
  const [saveError, setSaveError] = useState<string>();
  const [forecastInputErrors, setForecastInputErrors] = useState<Record<string, string>>({});
  const [forecastInputVersion, setForecastInputVersion] = useState(0);
  const designerRef = useRef<HTMLElement>(null);
  const pageTitleRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const next = clonePages(draft.pages);
    const previous = previousDraft.current;
    previousDraft.current = draft;
    dispatchHistory({ type: "replace", value: next });
    setLayoutMessage(""); setFocusView(undefined); setGesture(undefined); gestureRef.current = undefined;
    const changed = previous.base_hash !== draft.base_hash ? next.find(page => {
      const before = previous.pages.find(item => item.id === page.id);
      return !before || JSON.stringify(before) !== JSON.stringify(page);
    }) : undefined;
    setActivePageId(current => changed?.id || (next.some(page => page.id === current) ? current : next[0]?.id || ""));
    setActiveVisualId(current => changed ? "" : next.some(page => page.visuals.some(visual => visual.id === current)) ? current : "");
  }, [draft]);

  useEffect(() => {
    if (!pages.some(page => page.id === activePageId)) setActivePageId(pages[0]?.id || "");
    if (!pages.some(page => page.visuals.some(visual => visual.id === activeVisualId))) setActiveVisualId("");
  }, [pages, activePageId, activeVisualId]);
  useEffect(() => {
    const element = designerRef.current;
    const keydown = (event: globalThis.KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || busy || saveSubmitting) return;
      if ((event.target as HTMLElement).closest("input, textarea, select, [contenteditable=true]")) return;
      const key = event.key.toLowerCase();
      if (key !== "z" && key !== "y") return;
      event.preventDefault();
      setLayoutMessage(""); setSaveError(undefined);
      setForecastInputErrors({}); setForecastInputVersion(value => value + 1);
      gestureRef.current = undefined; setGesture(undefined);
      dispatchHistory({ type: key === "y" || event.shiftKey ? "redo" : "undo" });
    };
    element?.addEventListener("keydown", keydown);
    return () => element?.removeEventListener("keydown", keydown);
  }, [busy, saveSubmitting]);
  const activePage = pages.find((page) => page.id === activePageId);
  const activeVisual = activePage?.visuals.find((visual) => visual.id === activeVisualId);
  const forecastEnabled = Boolean(activeVisual?.forecast);
  useEffect(() => { setForecastInputErrors({}); }, [activeVisualId, draft.base_hash, forecastEnabled]);
  useEffect(() => {
    if (activePage) onSelection?.({ page_id: activePage.id, visual_id: activeVisual?.id, label: activeVisual ? `${activePage.title} › ${activeVisual.title}` : activePage.title });
  }, [activePage, activeVisual, onSelection]);
  const dirty = useMemo(() => JSON.stringify(pages) !== JSON.stringify(draft.pages) || Object.keys(forecastInputErrors).length > 0, [draft.pages, pages, forecastInputErrors]);
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (!dirty) return;
    const protect = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [dirty]);
  const measureById = useMemo(() => new Map(draft.measure_proposal.measures.map((measure) => [measure.id, measure])), [draft.measure_proposal.measures]);
  const tableById = useMemo(() => new Map(snapshot.tables.map((table) => [table.table.id, table.table])), [snapshot.tables]);
  const previewById = useMemo(() => new Map(draft.previews.map((preview) => [preview.visual_id, preview])), [draft.previews]);
  const savedVisualById = useMemo(() => new Map(draft.pages.flatMap((page) => page.visuals.map((visual) => [visual.id, visual] as const))), [draft.pages]);
  const compatibleCategoryTablesByMeasureTable = useMemo(() => {
    const compatible = new Map<string, Set<string>>();
    for (const { table } of snapshot.tables) compatible.set(table.id, new Set([table.id]));
    for (const relationship of draft.semantic_model.relationships) {
      if (
        relationship.review_status !== "approved"
        || relationship.blockers.length
        || relationship.is_active === false
        || !["many_to_one", "one_to_one"].includes(relationship.cardinality)
      ) continue;
      const destinations = compatible.get(relationship.from_table_id) ?? new Set([relationship.from_table_id]);
      destinations.add(relationship.to_table_id);
      compatible.set(relationship.from_table_id, destinations);
    }
    return compatible;
  }, [draft.semantic_model.relationships, snapshot.tables]);

  const updatePage = (pageId: string, update: Partial<DashboardPage>, group?: string) => {
    setPages((current) => current.map((page) => page.id === pageId ? { ...page, ...update } : page), group);
  };

  const updateVisual = (visualId: string, update: Partial<Visual>, group?: string) => {
    setPages((current) => current.map((page) => ({
      ...page,
      visuals: page.visuals.map((visual) => visual.id === visualId ? { ...visual, ...update } : visual),
    })), group);
  };

  const resetToDraft = () => {
    const next = clonePages(draft.pages);
    setPages(next);
    const page = next.find((item) => item.id === activePageId) ?? next[0];
    setActivePageId(page?.id || "");
    setActiveVisualId(page?.visuals.find((item) => item.id === activeVisualId)?.id || page?.visuals[0]?.id || "");
    setSaveError(undefined);
    setForecastInputErrors({}); setForecastInputVersion(value => value + 1);
    setLayoutMessage(""); setFocusView(undefined); setGesture(undefined); gestureRef.current = undefined;
    pageTitleRef.current?.focus();
  };

  const changeHistory = (type: "undo" | "redo") => {
    setLayoutMessage(""); setSaveError(undefined);
    setForecastInputErrors({}); setForecastInputVersion(value => value + 1);
    gestureRef.current = undefined; setGesture(undefined);
    dispatchHistory({ type });
  };


  const saveChanges = async () => {
    if (!dirty || busy || saveSubmitting || designIssues.length) return;
    setSaveError(undefined);
    setSaveSubmitting(true);
    try {
      await onSave(clonePages(pages));
      pageTitleRef.current?.focus();
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "Dashboard changes could not be saved and revalidated.");
    } finally {
      setSaveSubmitting(false);
    }
  };

  const selectPage = (page: DashboardPage) => {
    setActivePageId(page.id);
    setActiveVisualId(""); setFieldSearch(""); setLayoutMessage("");
  };

  const movePage = (pageId: string, delta: number) => {
    setPages((current) => {
      const index = current.findIndex((page) => page.id === pageId);
      const target = index + delta;
      if (index < 0 || target < 0 || target >= current.length) return current;
      const next = [...current];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  const selectVisual = (id: string) => { setActiveVisualId(id); setShowVisuals(true); setFieldSearch(""); };
  const applyPlacement = (visual: Visual, place: Placement) => {
    if (!activePage || busy || saveSubmitting) return;
    if (visual.x === place.x && visual.y === place.y && visual.width === place.width && visual.height === place.height) return;
    if (!canPlace(activePage.visuals, visual.id, place)) {
      setLayoutMessage("That position overlaps another visual. Choose an empty area or use Arrange.");
      return;
    }
    updateVisual(visual.id, place);
    setLayoutMessage(visual.width !== place.width || visual.height !== place.height
      ? `${visual.title} resized to ${place.width} columns by ${place.height} rows.`
      : `${visual.title} moved to column ${place.x + 1}, row ${place.y + 1}.`);
  };
  const changeGridField = (visual: Visual, field: keyof Placement, rawValue: number) => {
    if (!Number.isFinite(rawValue)) return;
    applyPlacement(visual, clampPlacement(visual, { [field]: rawValue }));
  };
  const arrangePage = (compact: boolean) => {
    if (!activePage || busy || saveSubmitting) return;
    const next = arrangeVisuals(activePage.visuals, compact);
    if (!next) { setLayoutMessage("These visuals need more space. Remove a visual or move it to another page."); return; }
    updatePage(activePage.id, { visuals: next });
    setLayoutMessage(compact ? "Compact layout applied. Undo restores the previous layout." : "Visuals arranged without overlap. Undo restores the previous layout.");
  };
  const removeVisual = (visual: Visual) => {
    if (!activePage || busy || saveSubmitting) return;
    if (activePage.visuals.length <= 1) { setLayoutMessage("Keep at least one visual on each report page."); return; }
    updatePage(activePage.id, { visuals: activePage.visuals.filter(v => v.id !== visual.id) });
    setActiveVisualId(""); setLayoutMessage(`${visual.title} removed. Undo restores it.`);
  };
  const duplicateVisual = (visual: Visual) => {
    if (!activePage || busy || saveSubmitting) return;
    const copy = { ...structuredClone(visual), id: uniqueId(`${visual.id}_copy`, new Set(pages.flatMap(p => p.visuals.map(v => v.id)))), title: `${visual.title.slice(0, 115)} copy` };
    const result = insertVisual(activePage.visuals, copy);
    if (!result) { setLayoutMessage("No room for another visual on this page. Add a page or remove a visual."); return; }
    updatePage(activePage.id, { visuals: result.visuals }); selectVisual(copy.id);
    setLayoutMessage(result.rearranged ? "Visual duplicated and page compacted to make room. Undo restores the previous layout." : "Visual duplicated. Save draft to calculate its preview.");
  };
  const visualKeys = (event: KeyboardEvent<HTMLElement>, visual: Visual) => {
    if (busy || saveSubmitting || (event.target as HTMLElement).closest("input, textarea, select")) return;
    if (event.key === "Escape") { gestureRef.current = undefined; setGesture(undefined); setShowVisuals(false); setActiveVisualId(""); return; }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "d") { event.preventDefault(); duplicateVisual(visual); return; }
    if (event.key === "Delete" && !(event.target as HTMLElement).closest(".report-visual-actions")) { event.preventDefault(); removeVisual(visual); return; }
    const delta: Record<string, [number, number]> = { ArrowLeft: [-1,0], ArrowRight: [1,0], ArrowUp: [0,-1], ArrowDown: [0,1] };
    if (event.altKey || event.ctrlKey || event.metaKey || !delta[event.key]) return;
    event.preventDefault();
    const [dx, dy] = delta[event.key];
    applyPlacement(visual, clampPlacement(visual, event.shiftKey ? { width: visual.width + dx, height: visual.height + dy } : { x: visual.x + dx, y: visual.y + dy }));
  };
  const startGesture = (event: PointerEvent<HTMLButtonElement>, visual: Visual, resize: boolean) => {
    if (busy || saveSubmitting || event.button !== 0 || !canvasRef.current) return;
    event.preventDefault(); event.currentTarget.focus(); setActiveVisualId(visual.id);
    const canvas = canvasRef.current;
    const style = getComputedStyle(canvas);
    const rows = style.gridTemplateRows.split(" ").map(parseFloat);
    const gap = parseFloat(style.rowGap) || 0;
    const rowStarts = [0]; rows.forEach(height => rowStarts.push(rowStarts[rowStarts.length - 1] + height + gap));
    gestureRef.current = { id: visual.id, start: visual, x: event.clientX, y: event.clientY, cellWidth: (canvas.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) + gap) / 12, rowStarts, resize, place: visual };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const moveGesture = (event: PointerEvent<HTMLButtonElement>) => {
    const g = gestureRef.current;
    if (!g || !activePage) return;
    const dx = Math.round((event.clientX - g.x) / g.cellWidth);
    const anchor = g.resize ? g.start.y + g.start.height : g.start.y;
    const target = g.rowStarts[anchor] + event.clientY - g.y;
    const nearest = g.rowStarts.reduce((best, pos, index) => Math.abs(pos - target) < Math.abs(g.rowStarts[best] - target) ? index : best, 0);
    const dy = nearest - anchor;
    const place = clampPlacement(g.start, g.resize ? { width: g.start.width + dx, height: g.start.height + dy } : { x: g.start.x + dx, y: g.start.y + dy });
    g.place = place;
    setGesture({ id: g.id, place, valid: canPlace(activePage.visuals, g.id, place) });
  };
  const endGesture = (event: PointerEvent<HTMLButtonElement>, cancel = false) => {
    const g = gestureRef.current;
    gestureRef.current = undefined; setGesture(undefined);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    if (g && !cancel) applyPlacement(g.start, g.place);
  };

  const addPage = () => {
    if (pages.length >= 8) return;
    const ids = new Set(pages.map((page) => page.id));
    const table = snapshot.tables[0]?.table;
    if (!table) return;
    const pageId = uniqueId("new_analysis", ids);
    const visual: Visual = {
      id: uniqueId(`${pageId}_detail`, new Set(pages.flatMap((page) => page.visuals.map((item) => item.id)))),
      recipe: "detail",
      title: `${table.name} detail`,
      visual_type: "tableEx",
      table_id: table.id,
      detail_column_ids: table.columns.slice(0, 6).map((column) => column.id),
      x: 0,
      y: 0,
      width: 12,
      height: 8,
    };
    const page: DashboardPage = {
      version: "1.0",
      id: pageId,
      title: "New analysis",
      purpose: "Reviewed local analysis",
      visuals: [visual],
      recommended_relationship_ids: [],
    };
    setPages((current) => [...current, page]);
    setActivePageId(page.id);
    selectVisual(visual.id);
  };

  const duplicatePage = (page: DashboardPage) => {
    if (pages.length >= 8) return;
    const pageId = uniqueId(`${page.id}_copy`, new Set(pages.map((item) => item.id)));
    const visualIds = new Set(pages.flatMap((item) => item.visuals.map((visual) => visual.id)));
    const copy = clonePages([page])[0];
    copy.id = pageId;
    copy.title = `${page.title.slice(0, 75)} copy`;
    copy.visuals = copy.visuals.map((visual) => ({ ...visual, id: uniqueId(`${pageId}_${visual.recipe}`, visualIds) }));
    setPages((current) => [...current, copy]);
    setActivePageId(copy.id);
    selectVisual(copy.visuals[0]?.id || "");
  };

  const deletePage = (pageId: string) => {
    if (pages.length <= 1) return;
    const next = pages.filter((page) => page.id !== pageId);
    setPages(next);
    if (activePageId === pageId) selectPage(next[0]);
  };

  const addVisual = (recipe: Visual["recipe"], visualType = defaultVisualType(recipe)) => {
    if (!activePage || activePage.visuals.length >= 12) return;
    if (!visualTypesByRecipe[recipe].some(v => v.id === visualType)) return;
    const measure = draft.measure_proposal.measures[0];
    const table = tableById.get(measure?.table_id || "") || snapshot.tables[0]?.table;
    if (!table) return;
    const date = table.columns.find((column) => ["date", "datetime"].includes(column.data_type));
    const category = table.columns.find((column) => column.data_type === "string" && column.distinct_count > 1 && column.distinct_count <= 100 && !column.candidate_key);
    const used = new Set(pages.flatMap((page) => page.visuals.map((visual) => visual.id)));
    const width = recipe === "kpi" ? 4 : recipe === "detail" ? 12 : 6;
    const height = recipe === "kpi" ? 2 : recipe === "detail" ? 6 : 5;
    const currentBottom = activePage.visuals.reduce((bottom, item) => Math.max(bottom, item.y + item.height), 0);
    const visual: Visual = {
      id: uniqueId(`${activePage.id}_${recipe}`, used),
      recipe,
      title: visualTypesByRecipe[recipe].find(v => v.id === visualType)?.label || labelByRecipe[recipe],
      visual_type: visualType,
      table_id: table.id,
      measure_id: recipe === "detail" ? undefined : measure?.id,
      category_column_id: recipe === "trend" ? date?.id : recipe === "category" ? category?.id : undefined,
      detail_column_ids: recipe === "detail" ? table.columns.slice(0, 6).map((column) => column.id) : [],
      x: 0,
      y: Math.min(12 - height, currentBottom),
      width,
      height,
    };
    const result = insertVisual(activePage.visuals, visual);
    if (!result) { setLayoutMessage("No room for another visual on this page. Add a page or remove a visual."); return; }
    updatePage(activePage.id, { visuals: result.visuals }); selectVisual(visual.id);
    setLayoutMessage(result.rearranged ? "Visual added and page compacted to make room. Undo restores the previous layout." : "Visual added. Choose its fields, then save draft to calculate the preview.");
  };

  const changeRecipe = (recipe: Visual["recipe"]) => {
    if (!activeVisual) return;
    const measure = measureById.get(activeVisual.measure_id || "") || draft.measure_proposal.measures[0];
    const table = tableById.get(measure?.table_id || activeVisual.table_id) || snapshot.tables[0]?.table;
    if (!table) return;
    const category = table.columns.find((column) => recipe === "trend" ? ["date", "datetime"].includes(column.data_type) : column.data_type === "string" && column.distinct_count > 1 && column.distinct_count <= 100 && !column.candidate_key);
    const changed: Visual = { ...activeVisual, date_filter: null, forecast: null,
      recipe,
      visual_type: defaultVisualType(recipe),
      table_id: table.id,
      measure_id: recipe === "detail" ? undefined : measure?.id,
      category_column_id: ["trend", "category"].includes(recipe) ? category?.id : undefined,
      detail_column_ids: recipe === "detail" ? table.columns.slice(0, 6).map((column) => column.id) : [],
      width: recipe === "kpi" ? 4 : recipe === "detail" ? 12 : Math.max(activeVisual.width, 6),
      height: recipe === "kpi" ? 2 : recipe === "detail" ? 6 : Math.max(activeVisual.height, 5),
    };
    const place = activePage && findOpenPlacement(activePage.visuals, changed);
    if (!place) { setLayoutMessage("This visual type needs more room. Use Compact layout or resize the other visuals first."); return; }
    updateVisual(activeVisual.id, { ...changed, ...place }); setLayoutMessage("");
  };

  const changeMeasure = (measureId: string) => {
    const measure = measureById.get(measureId);
    if (!activeVisual || !measure) return;
    if (!isGroupedRecipe(activeVisual.recipe)) {
      updateVisual(activeVisual.id, { measure_id: measure.id, table_id: measure.table_id, date_filter: null });
      return;
    }
    const reachable = compatibleCategoryTablesByMeasureTable.get(measure.table_id) ?? new Set<string>();
    const currentCategoryTable = tableById.get(activeVisual.table_id);
    const currentTableIsCompatible = reachable.has(activeVisual.table_id) && categoryColumnsFor(currentCategoryTable, activeVisual.recipe).length > 0;
    const categoryTable = currentTableIsCompatible
      ? currentCategoryTable
      : snapshot.tables.map((item) => item.table).find((table) => reachable.has(table.id) && categoryColumnsFor(table, activeVisual.recipe).length > 0);
    const candidates = categoryColumnsFor(categoryTable, activeVisual.recipe);
    const currentCategoryId = candidates.some((column) => column.id === activeVisual.category_column_id) ? activeVisual.category_column_id : undefined;
    updateVisual(activeVisual.id, {
      date_filter: null,
      measure_id: measure.id,
      table_id: categoryTable?.id || measure.table_id,
      category_column_id: currentCategoryId || candidates[0]?.id,
    });
  };

  const changeCategoryTable = (tableId: string) => {
    if (!activeVisual || !selectedMeasure || !isGroupedRecipe(activeVisual.recipe)) return;
    const reachable = compatibleCategoryTablesByMeasureTable.get(selectedMeasure.table_id) ?? new Set<string>();
    const table = tableById.get(tableId);
    if (!table || !reachable.has(table.id)) return;
    const candidates = categoryColumnsFor(table, activeVisual.recipe);
    updateVisual(activeVisual.id, {
      table_id: table.id,
      category_column_id: candidates.some((column) => column.id === activeVisual.category_column_id)
        ? activeVisual.category_column_id
        : candidates[0]?.id,
    });
  };

  const pageIndex = activePage ? pages.indexOf(activePage) : -1;
  const availableColumns = activeVisual ? tableById.get(activeVisual.table_id)?.columns || [] : [];
  const selectedMeasure = measureById.get(activeVisual?.measure_id || "");
  const selectedMeasureReachableTables = selectedMeasure ? compatibleCategoryTablesByMeasureTable.get(selectedMeasure.table_id) : undefined;
  const eligibleCategoryTables = activeVisual && selectedMeasure && isGroupedRecipe(activeVisual.recipe)
    ? snapshot.tables
      .map((item) => item.table)
      .filter((table) => Boolean(selectedMeasureReachableTables?.has(table.id)) && categoryColumnsFor(table, activeVisual.recipe).length > 0)
    : [];
  const categoryColumns = categoryColumnsFor(activeVisual ? tableById.get(activeVisual.table_id) : undefined, activeVisual?.recipe || "kpi");
  const forecastAxis = activeVisual ? tableById.get(activeVisual.table_id)?.columns.find(column => column.id === activeVisual.category_column_id) : undefined;
  const forecastEligibilityReason = !selectedMeasure ? "Choose a reviewed measure to enable forecasting."
    : !forecastAxis || !["date", "datetime"].includes(forecastAxis.data_type) ? "Choose a date field in Date axis to enable forecasting."
      : !selectedMeasureReachableTables?.has(activeVisual!.table_id) ? "Choose an axis table connected to the selected measure to enable forecasting." : undefined;
  const forecastInputKey = `${activeVisualId}:${draft.base_hash}:${forecastInputVersion}`;
  const forecastFieldError = (label: string, error?: string) => setForecastInputErrors(current => {
    const message = error ? `${label}: ${error}` : undefined;
    if (current[label] === message) return current;
    const next = { ...current };
    if (message) next[label] = message;
    else delete next[label];
    return next;
  });
  const designIssues = useMemo(() => {
    const issues: string[] = Object.values(forecastInputErrors);
    if (!pages.length) issues.push("The report needs at least one page.");
    if (pages.length > 8) issues.push("The report cannot contain more than eight pages.");
    const pageIds = new Set<string>();
    const visualIds = new Set<string>();
    for (const page of pages) {
      if (pageIds.has(page.id)) issues.push(`Page ID ${page.id} is duplicated.`);
      pageIds.add(page.id);
      if (!page.title.trim()) issues.push("Every report page needs a title.");
      if (page.title.length > 80) issues.push(`${page.title.slice(0, 24)}… exceeds the 80-character page-title limit.`);
      if (!page.purpose.trim()) issues.push(`${page.title || "Untitled page"} needs a purpose.`);
      if (page.purpose.length > 300) issues.push(`${page.title || "Untitled page"} exceeds the 300-character purpose limit.`);
      if (!page.visuals.length) issues.push(`${page.title || "Untitled page"} needs at least one visual.`);
      if (page.visuals.length > 12) issues.push(`${page.title || "Untitled page"} cannot contain more than 12 visuals.`);
      for (let index = 0; index < page.visuals.length; index++) {
        const visual = page.visuals[index];
        if (page.visuals.slice(index + 1).some(other => overlaps(visual, other))) issues.push(`${visual.title || "A visual"} overlaps another visual. Use Arrange to make room.`);
        const [minWidth, minHeight] = minimumVisualSize[visual.recipe];
        if (![visual.x, visual.y, visual.width, visual.height].every(Number.isInteger) || visual.width < minWidth || visual.height < minHeight) issues.push(`${visual.title || "A visual"} needs a readable ${minWidth} × ${minHeight} grid area.`);
        if (visualIds.has(visual.id)) issues.push(`Visual ID ${visual.id} is duplicated.`);
        visualIds.add(visual.id);
        if (!visual.title.trim()) issues.push(`A visual on ${page.title || "an untitled page"} needs a title.`);
        if (visual.title.length > 120) issues.push(`A visual on ${page.title || "an untitled page"} exceeds the 120-character title limit.`);
        if (!visualTypesByRecipe[visual.recipe].some((item) => item.id === visual.visual_type)) {
          issues.push(`${visual.title || "Untitled visual"} uses a Power BI visual that is not approved for its binding recipe.`);
        }
        if (visual.x < 0 || visual.y < 0 || visual.width < 1 || visual.height < 1 || visual.x + visual.width > 12 || visual.y + visual.height > 12) {
          issues.push(`${visual.title || "Untitled visual"} extends beyond the 12 × 12 report grid.`);
        }
        const table = tableById.get(visual.table_id);
        if (!table) {
          issues.push(`${visual.title || "Untitled visual"} references an unavailable table.`);
          continue;
        }
        if (visual.recipe === "detail") {
          if (!visual.detail_column_ids.length) issues.push(`${visual.title || "Detail visual"} needs at least one field.`);
          if (visual.detail_column_ids.some((columnId) => !table.columns.some((column) => column.id === columnId))) {
            issues.push(`${visual.title || "Detail visual"} contains a field outside ${table.name}.`);
          }
          continue;
        }
        const measure = measureById.get(visual.measure_id || "");
        if (!measure) {
          issues.push(`${visual.title || "Analytical visual"} needs a reviewed measure.`);
          continue;
        }
        if (isGroupedRecipe(visual.recipe)) {
          const axis = table.columns.find(column => column.id === visual.category_column_id);
          if (!axis) {
            issues.push(`${visual.title || "Grouped visual"} needs a reviewed ${visual.recipe === "trend" ? "date axis" : "category"}.`);
          } else if (visual.recipe === "trend" && !["date", "datetime"].includes(axis.data_type)) {
            issues.push(`${visual.title || "Trend visual"} needs a date or datetime axis. Choose a date field in Visual properties.`);
          }
          if (!compatibleCategoryTablesByMeasureTable.get(measure.table_id)?.has(table.id)) {
            issues.push(`${table.name} is not connected directly from the ${measure.name} measure table by an approved many-to-one relationship.`);
          }
        } else if (table.id !== measure.table_id) {
          issues.push(`${visual.title || "KPI"} must use the ${measure.name} measure table.`);
        }
      }
    }
    return [...new Set(issues)];
  }, [compatibleCategoryTablesByMeasureTable, measureById, pages, tableById, forecastInputErrors]);
  const focusedVisual = focusView ? activePage?.visuals.find(v => v.id === focusView.id) : undefined;
  const activePageTitleInvalid = Boolean(activePage && (!activePage.title.trim() || activePage.title.length > 80));
  const activePagePurposeInvalid = Boolean(activePage && (!activePage.purpose.trim() || activePage.purpose.length > 300));
  const editActions = <div className="designer-history-toolbar">
    <div className="designer-history-actions" role="group" aria-label="Report edit history">
      <button type="button" className="secondary icon-only" aria-label="Undo report edit" title="Undo report edit (Ctrl+Z outside text fields)" disabled={busy || saveSubmitting || !history.past.length} onClick={() => changeHistory("undo")}><Undo2 size={15}/></button>
      <button type="button" className="secondary icon-only" aria-label="Redo report edit" title="Redo report edit (Ctrl+Shift+Z outside text fields)" disabled={busy || saveSubmitting || !history.future.length} onClick={() => changeHistory("redo")}><Redo2 size={15}/></button>
    </div>
    {dirty ? <div className="designer-edit-actions" role="group" aria-label="Report draft edits">
    <button type="button" className="secondary" disabled={busy || saveSubmitting} onClick={resetToDraft}><RotateCcw size={15} aria-hidden="true" />Reset</button>
    <button type="button" className="primary yellow" disabled={busy || saveSubmitting || designIssues.length > 0} title="Save and revalidate your draft" aria-describedby={designIssues.length || saveError ? "designer-validation-summary" : undefined} onClick={() => void saveChanges()}><Save size={15} aria-hidden="true" />{saveSubmitting ? "Validating…" : "Save draft"}</button>
  </div> : null}</div>;

  return <section ref={designerRef} className="dashboard-designer report-workbench" aria-label="Dashboard designer" aria-busy={busy || saveSubmitting} onBlurCapture={() => dispatchHistory({ type: "end-group" })}>
    <h1 className="sr-only">Dashboard Designer</h1>
    {toolbarTarget ? createPortal(editActions, toolbarTarget) : editActions}
    {(designIssues.length > 0 || saveError) && <div id="designer-validation-summary" className="designer-validation-summary" role="alert">
      <AlertTriangle size={15} aria-hidden="true" />
      <div><strong>{saveError || `${designIssues.length} draft issue${designIssues.length === 1 ? "" : "s"} must be resolved before saving.`}</strong>{!saveError && <span>{designIssues.slice(0, 2).join(" ")}{designIssues.length > 2 ? ` +${designIssues.length - 2} more.` : ""}</span>}</div>
    </div>}

    <div className={`designer-layout ${showVisuals ? "has-properties" : "no-properties"}`}>
      <aside className="designer-pages" aria-label="Report pages">
        <div className="designer-panel-heading"><span>Pages</span><button type="button" disabled={busy || saveSubmitting || pages.length >= 8} onClick={addPage} aria-label={pages.length >= 8 ? "Maximum of eight report pages reached" : "Add report page"}><Plus size={16} aria-hidden="true" /></button></div>
        <div className="designer-page-list">
          {pages.map((page, index) => <div key={page.id} className={`designer-page-item${page.id === activePageId ? " active" : ""}`}>
            <button type="button" className="designer-page-select" aria-current={page.id === activePageId ? "page" : undefined} onClick={() => selectPage(page)}>
              <LayoutDashboard size={15} aria-hidden="true" /><span><strong>{page.title || "Untitled page"}</strong><small>{page.visuals.length} visual{page.visuals.length === 1 ? "" : "s"}</small></span><em aria-label={`Page ${index + 1}`}>{index + 1}</em>
            </button>
          </div>)}
        </div>
        {activePage && <div className="designer-page-actions">
          <button type="button" disabled={busy || saveSubmitting || pageIndex <= 0} onClick={() => movePage(activePage.id, -1)} aria-label="Move page earlier"><ArrowUp size={15} aria-hidden="true" /></button>
          <button type="button" disabled={busy || saveSubmitting || pageIndex < 0 || pageIndex >= pages.length - 1} onClick={() => movePage(activePage.id, 1)} aria-label="Move page later"><ArrowDown size={15} aria-hidden="true" /></button>
          <button type="button" disabled={busy || saveSubmitting || pages.length >= 8} onClick={() => duplicatePage(activePage)} aria-label={pages.length >= 8 ? "Maximum of eight report pages reached" : "Duplicate page"}><Copy size={15} aria-hidden="true" /></button>
          <button type="button" disabled={busy || saveSubmitting || pages.length <= 1} onClick={() => deletePage(activePage.id)} aria-label="Delete page"><Trash2 size={15} aria-hidden="true" /></button>
        </div>}
      </aside>

      <section className="designer-canvas-panel" aria-label="Report page canvas">
        {activePage ? <>
          <div className="designer-page-title">
            {onContext ? <AskAiButton className="designer-page-ask" about={`page ${activePage.title || "Untitled page"}`} disabled={busy || dirty || saveSubmitting} disabledReason={dirty ? "Save and revalidate your edits before asking about this page" : "Wait for the current action to finish"} onClick={() => onContext({ page_id: activePage.id, label: activePage.title || "Untitled page" })} /> : <LayoutDashboard size={18} aria-hidden="true" />}
            <label><span>Page title</span><input ref={pageTitleRef} disabled={busy || saveSubmitting} maxLength={80} aria-invalid={activePageTitleInvalid} value={activePage.title} onChange={(event) => updatePage(activePage.id, { title: event.target.value }, `page-title-${activePage.id}`)} /></label>
            <label className="designer-page-purpose"><span>Purpose</span><input disabled={busy || saveSubmitting} maxLength={300} aria-invalid={activePagePurposeInvalid} value={activePage.purpose} onChange={(event) => updatePage(activePage.id, { purpose: event.target.value }, `page-purpose-${activePage.id}`)} /></label>
          </div>
          <ReportToolbar disabled={busy || saveSubmitting} atLimit={activePage.visuals.length >= 12} onAdd={addVisual} onArrange={arrangePage} showVisuals={showVisuals} onVisuals={() => setShowVisuals(!showVisuals)} onDateFilter={() => { if (dirty) setLayoutMessage("Save or discard report edits before filtering a visual."); else setDateFilterTarget({ visualId: activeVisual?.id }); }} showGrid={showGrid} onGrid={() => setShowGrid(!showGrid)}/>
          <div ref={canvasRef} className={`designer-canvas${showGrid ? " grid-visible" : ""}`} aria-label={`${activePage.title} visual layout`} style={{ gridTemplateRows: Array.from({ length: 12 }, (_, row) => activePage.visuals.some((visual) => visual.recipe === "kpi" && row >= visual.y && row < visual.y + visual.height) ? "minmax(48px, .6fr)" : "minmax(22px, 1fr)").join(" ") }}>
            {[...activePage.visuals].sort((a, b) => a.y - b.y || a.x - b.x).map((visual) => {
              const Icon = iconByRecipe[visual.recipe];
              const measure = measureById.get(visual.measure_id || "");
              const categoryTable = tableById.get(visual.table_id);
              const categoryColumn = categoryTable?.columns.find((column) => column.id === visual.category_column_id);
              const binding = isGroupedRecipe(visual.recipe) && measure
                ? `${measure.name} by ${categoryTable?.name || visual.table_id}${categoryColumn ? `[${categoryColumn.name}]` : ""}`
                : measure?.name || categoryTable?.name || visual.table_id;
              return <article data-visual-id={visual.id} key={visual.id} className={`designer-visual ${visual.id === activeVisualId ? "active" : ""}`} style={{ gridColumn: `${Math.max(0, Math.min(11, visual.x)) + 1} / span ${Math.max(1, Math.min(visual.width, 12 - Math.max(0, Math.min(11, visual.x))))}`, gridRow: `${Math.max(0, Math.min(11, visual.y)) + 1} / span ${Math.max(1, Math.min(visual.height, 12 - Math.max(0, Math.min(11, visual.y))))}` }}>
                <div className="report-visual-header">
                <button type="button" className="designer-visual-select" onKeyDown={event => visualKeys(event, visual)} aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight Shift+ArrowUp Shift+ArrowDown Shift+ArrowLeft Shift+ArrowRight Control+d Delete" aria-pressed={visual.id === activeVisualId} aria-label={`${visual.title || "Untitled visual"}, ${labelByRecipe[visual.recipe]}, ${binding}`} onClick={() => selectVisual(visual.id)}><span><Icon size={15} aria-hidden="true" /><em>{visualTypeLabel(visual)}</em></span><strong>{visual.title || "Untitled visual"}</strong></button>
                <div className="report-visual-actions">
                  <button type="button" title="Focus visual" aria-label={`Focus ${visual.title}`} onClick={() => setFocusView({ id: visual.id, view: "visual" })}><Maximize2 size={14}/></button>
                  <button type="button" title="Show data" aria-label={`Show data for ${visual.title}`} onClick={() => setFocusView({ id: visual.id, view: "table" })}><Table2 size={14}/></button>
                </div></div>
                <div className="designer-preview"><DesignerVisualPreview visual={visual} saved={savedVisualById.get(visual.id)} preview={previewById.get(visual.id)} measure={measure} category={categoryColumn?.name || "Category"} /></div>
                <small className="designer-visual-binding" title={binding}>{binding}</small>
                {visual.date_filter && <button type="button" className="report-date-filter-badge" disabled={busy || dirty || saveSubmitting} onClick={event => { event.stopPropagation(); setDateFilterTarget({ visualId: visual.id }); }}><CalendarRange size={12}/>{visual.date_filter.start || "Any date"} – {visual.date_filter.end || "Any date"}</button>}
                {visual.id === activeVisualId && <>
                  <button type="button" className="report-drag-handle" onKeyDown={event => visualKeys(event, visual)} disabled={busy || saveSubmitting} title="Drag to move · Arrow keys move one grid cell" aria-label={`Move ${visual.title}`} onPointerDown={event => startGesture(event, visual, false)} onPointerMove={moveGesture} onPointerUp={event => endGesture(event)} onPointerCancel={event => endGesture(event, true)}><GripVertical size={16}/></button>
                  <button type="button" className="report-resize-handle" onKeyDown={event => visualKeys(event, visual)} disabled={busy || saveSubmitting} title="Drag to resize · Shift + arrow keys resize" aria-label={`Resize ${visual.title}`} onPointerDown={event => startGesture(event, visual, true)} onPointerMove={moveGesture} onPointerUp={event => endGesture(event)} onPointerCancel={event => endGesture(event, true)}><MoveDiagonal2 size={14}/></button>
                </>}
              </article>;
            })}
            {gesture && <div className={`report-placement-ghost${gesture.valid ? "" : " invalid"}`} aria-hidden="true" style={{ gridColumn: `${gesture.place.x + 1} / span ${gesture.place.width}`, gridRow: `${gesture.place.y + 1} / span ${gesture.place.height}` }}/>} 
          </div>
          <div className="report-canvas-status"><span>{activePage.title || "Untitled page"} · {activePage.visuals.length} visual{activePage.visuals.length === 1 ? "" : "s"}</span><span title="Local snapshot preview. Save draft revalidates bindings; Review & build requires approval before writing to Power BI."><CheckCircle2 size={13}/>Local snapshot preview</span></div>
        </> : <div className="designer-empty"><LayoutDashboard size={28} aria-hidden="true" /><h2>No report page</h2><button type="button" className="primary" disabled={busy || saveSubmitting} onClick={addPage}>Add a page</button></div>}
      </section>

      <aside className="designer-properties" aria-label="Visual properties" hidden={!showVisuals}>
        <div className="designer-panel-heading"><span>Visual</span><button type="button" aria-label="Close properties" onClick={() => setShowVisuals(false)}><X size={16}/></button></div>
        <div className="report-visual-list" role="group" aria-label="Page visuals">{activePage?.visuals.map(v => { const Icon = iconByRecipe[v.recipe]; return <button key={v.id} aria-pressed={v.id === activeVisualId} onClick={() => selectVisual(v.id)}><Icon size={15}/><span>{v.title || "Untitled visual"}</span></button>; })}</div>
        {activeVisual ? <div className="designer-property-form">
          {onContext && activePage && <AskAiButton about={`visual ${activeVisual.title}`} disabled={busy || dirty || saveSubmitting} disabledReason={dirty ? "Save and revalidate your edits before asking about this visual" : "Wait for the current action to finish"} onClick={() => onContext({ page_id: activePage.id, visual_id: activeVisual.id, label: `${activePage.title} › ${activeVisual.title}` })} />}
          {activeVisual.recipe !== "detail" && <button type="button" className="secondary" disabled={busy || dirty || saveSubmitting} onClick={() => setDateFilterTarget({ visualId: activeVisual.id })}><CalendarRange size={15}/>{activeVisual.date_filter ? "Edit date filter" : "Date filter"}</button>}
          <label><span>Visual title</span><input disabled={busy || saveSubmitting} maxLength={120} aria-invalid={!activeVisual.title.trim() || activeVisual.title.length > 120} value={activeVisual.title} onChange={(event) => updateVisual(activeVisual.id, { title: event.target.value }, `visual-title-${activeVisual.id}`)} /></label>
          <label><span>Visual kind</span><select disabled={busy || saveSubmitting} value={activeVisual.recipe} onChange={(event) => changeRecipe(event.target.value as Visual["recipe"])}>{(Object.keys(labelByRecipe) as Visual["recipe"][]).map((recipe) => <option key={recipe} value={recipe}>{labelByRecipe[recipe]}</option>)}</select></label>
          <label><span>Chart style</span><select disabled={busy || saveSubmitting} value={activeVisual.visual_type} onChange={(event) => updateVisual(activeVisual.id, { visual_type: event.target.value, forecast: event.target.value === visualForecastCapability.visual_type ? activeVisual.forecast : null })}>{visualTypesByRecipe[activeVisual.recipe].map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}</select></label>
          {activeVisual.recipe === visualForecastCapability.recipe && activeVisual.visual_type === visualForecastCapability.visual_type && <fieldset className="designer-forecast-settings" disabled={busy || saveSubmitting}>
            <legend>Forecast</legend>
            <label className="designer-check"><input type="checkbox" checked={Boolean(activeVisual.forecast)} disabled={Boolean(forecastEligibilityReason) && !activeVisual.forecast} aria-describedby={forecastEligibilityReason ? "designer-forecast-eligibility" : undefined} onChange={event => updateVisual(activeVisual.id, { forecast: event.target.checked ? { ...visualForecastCapability.defaults } : null })} />Enable forecast</label>
            {forecastEligibilityReason && <small id="designer-forecast-eligibility" className="designer-field-error">{forecastEligibilityReason}</small>}
            {activeVisual.forecast && <>
              <ForecastNumberInput key={`${forecastInputKey}:length`} label="Forecast length (points)" value={activeVisual.forecast.forecast_length} minimum={forecastSettings.forecast_length.minimum} maximum={forecastSettings.forecast_length.maximum} onValidityChange={error => forecastFieldError("Forecast length (points)", error)} onValueChange={value => updateVisual(activeVisual.id, { forecast: { ...activeVisual.forecast!, forecast_length: value! } }, `forecast-length-${activeVisual.id}`)} />
              <label><span>Confidence interval</span><select value={activeVisual.forecast.confidence} onChange={event => updateVisual(activeVisual.id, { forecast: { ...activeVisual.forecast!, confidence: Number(event.target.value) as NonNullable<Visual["forecast"]>["confidence"] } })}>{forecastSettings.confidence.enum.map(value => <option key={value} value={value}>{value}%</option>)}</select></label>
              <details className="designer-forecast-advanced"><summary>Advanced forecast settings</summary>
                <ForecastNumberInput key={`${forecastInputKey}:ignore`} label="Ignore last (points)" value={activeVisual.forecast.ignore_last} minimum={forecastSettings.ignore_last.minimum} maximum={forecastSettings.ignore_last.maximum} onValidityChange={error => forecastFieldError("Ignore last (points)", error)} onValueChange={value => updateVisual(activeVisual.id, { forecast: { ...activeVisual.forecast!, ignore_last: value! } }, `forecast-ignore-${activeVisual.id}`)} />
                <ForecastNumberInput key={`${forecastInputKey}:seasonality`} label="Seasonality (points)" value={activeVisual.forecast.seasonality} minimum={forecastSettings.seasonality.minimum} maximum={forecastSettings.seasonality.maximum} allowAuto onValidityChange={error => forecastFieldError("Seasonality (points)", error)} onValueChange={value => updateVisual(activeVisual.id, { forecast: { ...activeVisual.forecast!, seasonality: value } }, `forecast-seasonality-${activeVisual.id}`)} />
              </details>
            </>}
            <small className="designer-binding-note">Power BI calculates the forecast after you approve and build. This preview shows historical values.</small>
          </fieldset>}
          {activeVisual.recipe !== "detail" && <label><span>Measure</span><select disabled={busy || saveSubmitting || !draft.measure_proposal.measures.length} aria-invalid={!selectedMeasure} value={selectedMeasure?.id || ""} onChange={(event) => changeMeasure(event.target.value)}><option value="" disabled>Select a reviewed measure</option>{draft.measure_proposal.measures.map((measure) => <option key={measure.id} value={measure.id}>{measure.name}</option>)}</select></label>}
          {isGroupedRecipe(activeVisual.recipe) && <fieldset className="designer-category-binding" disabled={busy || saveSubmitting}><legend>Group by</legend>
            <label><span>{activeVisual.recipe === "trend" ? "Axis table" : "Category table"}</span><select disabled={busy || saveSubmitting || !eligibleCategoryTables.length} aria-invalid={!eligibleCategoryTables.some((table) => table.id === activeVisual.table_id)} value={eligibleCategoryTables.some((table) => table.id === activeVisual.table_id) ? activeVisual.table_id : ""} onChange={(event) => changeCategoryTable(event.target.value)}><option value="" disabled>{eligibleCategoryTables.length ? "Select a connected table" : "No connected table with compatible fields"}</option>{eligibleCategoryTables.map((table) => <option key={table.id} value={table.id}>{table.name}{table.id === selectedMeasure?.table_id ? " (measure table)" : ""}</option>)}</select></label>
            <label><span>{activeVisual.recipe === "trend" ? "Date axis" : "Category field"}</span><select disabled={busy || saveSubmitting || !categoryColumns.length} aria-invalid={!activeVisual.category_column_id || !categoryColumns.some((column) => column.id === activeVisual.category_column_id)} value={categoryColumns.some((column) => column.id === activeVisual.category_column_id) ? activeVisual.category_column_id : ""} onChange={(event) => updateVisual(activeVisual.id, { category_column_id: event.target.value })}><option value="" disabled>{categoryColumns.length ? "Select a reviewed field" : "No compatible reviewed fields"}</option>{categoryColumns.map((column) => <option key={column.id} value={column.id}>{column.name}</option>)}</select></label>
            <small className="designer-binding-note">{selectedMeasure && activeVisual.table_id !== selectedMeasure.table_id ? "Cross-table grouping uses a direct approved relationship from the measure table." : "The grouping field and measure currently use the same table."}</small>
          </fieldset>}
          {activeVisual.recipe === "detail" && <>
            <label><span>Source table</span><select disabled={busy || saveSubmitting} value={activeVisual.table_id} onChange={event => { const table = tableById.get(event.target.value); if (table) updateVisual(activeVisual.id, { table_id: table.id, detail_column_ids: table.columns.slice(0, 6).map(c => c.id) }); setFieldSearch(""); }}>{snapshot.tables.map(({ table }) => <option key={table.id} value={table.id}>{table.name}</option>)}</select></label>
            <fieldset disabled={busy || saveSubmitting}><legend>Detail fields · {activeVisual.detail_column_ids.length}/8</legend>
              <input aria-label="Find detail fields" placeholder="Find fields…" value={fieldSearch} onChange={event => setFieldSearch(event.target.value)}/>
              <div className="report-detail-fields">{availableColumns.filter(c => c.name.toLowerCase().includes(fieldSearch.toLowerCase())).map(column => <label className="designer-check" key={column.id}><input type="checkbox" disabled={!activeVisual.detail_column_ids.includes(column.id) && activeVisual.detail_column_ids.length >= 8} checked={activeVisual.detail_column_ids.includes(column.id)} onChange={event => updateVisual(activeVisual.id, { detail_column_ids: event.target.checked ? [...activeVisual.detail_column_ids, column.id] : activeVisual.detail_column_ids.filter(id => id !== column.id) })}/>{column.name}</label>)}</div>
              <small className="designer-binding-note">Choose up to eight fields.</small>
            </fieldset>
          </>}
          <details className="report-position-details"><summary>Position &amp; size</summary>
            <fieldset disabled={busy || saveSubmitting}><legend className="sr-only">Grid placement</legend><div className="designer-grid-inputs">{(["x", "y", "width", "height"] as const).map(field => <label key={field}><span>{field === "x" ? "Column" : field === "y" ? "Row" : field}</span><input type="number" step={1} min={field === "x" || field === "y" ? 1 : minimumVisualSize[activeVisual.recipe][field === "width" ? 0 : 1]} max={field === "x" ? 13 - activeVisual.width : field === "y" ? 13 - activeVisual.height : 12} value={activeVisual[field] + (field === "x" || field === "y" ? 1 : 0)} onChange={event => changeGridField(activeVisual, field, event.target.valueAsNumber - (field === "x" || field === "y" ? 1 : 0))}/></label>)}</div>
            <small className="designer-binding-note">Drag the handles, or use arrow keys to move and Shift + arrow keys to resize.</small></fieldset>
          </details>
          <div className="report-object-actions"><button className="secondary" type="button" disabled={busy || saveSubmitting || (activePage?.visuals.length ?? 0) >= 12} onClick={() => duplicateVisual(activeVisual)}><Copy size={15}/>Duplicate visual</button>
          <button type="button" className="danger-action" disabled={busy || saveSubmitting || (activePage?.visuals.length ?? 0) <= 1} title="Remove visual · Undo restores it" onClick={() => removeVisual(activeVisual)}><Trash2 size={15}/>Remove visual</button></div>
        </div> : <p className="designer-properties-empty">Select a visual to edit its reviewed bindings and layout.</p>}
      </aside>
    </div>
    {layoutMessage && <div className="report-layout-message" role="status"><span>{layoutMessage}</span><button type="button" aria-label="Dismiss layout message" onClick={() => setLayoutMessage("")}><X size={14}/></button></div>}
    {dateFilterTarget && <ReportDateFilter pages={draft.pages} measures={draft.measure_proposal.measures} snapshot={snapshot} initialVisualId={dateFilterTarget.visualId} busy={busy || dirty || saveSubmitting} onClose={() => setDateFilterTarget(undefined)} onApply={async (visualId, filter) => {
      const next = clonePages(draft.pages);
      const page = next.find(p => p.visuals.some(v => v.id === visualId));
      const visual = page?.visuals.find(v => v.id === visualId);
      if (!page || !visual) throw new Error("This visual no longer exists. Select it again.");
      visual.date_filter = filter;
      await onSave(next);
      setActivePageId(page.id); setActiveVisualId(visual.id);
    }} />}
    {focusedVisual && focusView && <ReportVisualDialog visual={focusedVisual} saved={savedVisualById.get(focusedVisual.id)} preview={previewById.get(focusedVisual.id)} measure={measureById.get(focusedVisual.measure_id || "")} category={tableById.get(focusedVisual.table_id)?.columns.find(c => c.id === focusedVisual.category_column_id)?.name || "Category"} initialView={focusView.view} onClose={() => setFocusView(undefined)}/>}
  </section>;
}
