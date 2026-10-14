import { useEffect, useRef, useState, type CSSProperties, type MutableRefObject, type PointerEvent as ReactPointerEvent } from "react";
import { Ban, Filter, Highlighter, Pin } from "lucide-react";
import type { OverlayContext } from "./InteractiveReportPage";
import { HANDLES, moveRects, resizeRect, type Handle, type Rect } from "./importedGeometry";
import { visualAt, type Interaction } from "./importedPanes";
import type { TestStatus } from "./judgement";

type Gesture = {
  mode: "move" | "resize"; ids: string[]; handle?: Handle; start: Record<string, Rect>;
  origin: { x: number; y: number }; client: { x: number; y: number }; moved: boolean; last?: Record<string, Rect>;
};
const DRAG_THRESHOLD = 3;

/**
 * Selection frames, resize handles and drag-to-move for the edit canvas, drawn inside the scaled
 * page by the `overlay` prop. The canvas itself selects on pointer down (topmost by rank, Alt to
 * reach the one underneath); this layer starts a drag from the same pointer down when it lands on
 * a selected visual, so a click-and-drag moves a visual without a separate click first. Gestures
 * only preview here; the editor commits them as one batch when they settle. Keyboard nudging is in
 * the editor, and Size & position in the Visual pane is the keyboard route for resizing.
 */
/** "Edit interactions": the selected source and, for every other visual, how it reacts and what it can do. */
export type InteractionsOverlay = {
  source: string;
  choices: (target: string) => Exclude<Interaction, "unsupported">[];
  current: (target: string) => Interaction | undefined;
  onSet: (target: string, type: Exclude<Interaction, "unsupported">) => void;
};

const INTERACTION_ICONS = { filter: Filter, highlight: Highlighter, none: Ban } as const;
const INTERACTION_LABELS = { filter: "Filter", highlight: "Highlight", none: "None" } as const;

export function ImportedOverlay({ context, selectedIds, selectionRef, locked, onPreview, onCommit, onAbort, titles, interactions, badges, onContextMenu }: {
  context: OverlayContext;
  selectedIds: string[];
  /** The selection as of this instant (updated synchronously when the canvas picks), so a drag can start on the same pointer down. */
  selectionRef: MutableRefObject<string[]>;
  locked: boolean;
  onPreview: (rects: Record<string, Rect>) => void;
  onCommit: (rects: Record<string, Rect>) => void;
  onAbort: (ids: string[]) => void;
  titles: Record<string, string>;
  /** Shown while "Edit interactions" is on: Filter / Highlight / None on each other visual, like Power BI Desktop. */
  interactions?: InteractionsOverlay;
  /** Visuals with report tests: a small badge, red when one fails. */
  badges?: Map<string, TestStatus>;
  /** A right-click on the canvas: the visual under the pointer (topmost), or none. */
  onContextMenu?: (visual: string | undefined, at: { x: number; y: number }) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const contextRef = useRef(context);
  const lockedRef = useRef(locked);
  const handlers = useRef({ onPreview, onCommit, onAbort });
  const gesture = useRef<Gesture | undefined>(undefined);
  const [hud, setHud] = useState<{ id: string; rect: Rect } | undefined>(undefined);
  const menuRef = useRef(onContextMenu);
  useEffect(() => { contextRef.current = context; lockedRef.current = locked; handlers.current = { onPreview, onCommit, onAbort }; menuRef.current = onContextMenu; });

  // Right-click on the canvas: the menu is about the visual under the pointer (the editor selects it first).
  useEffect(() => {
    const onMenu = (event: MouseEvent) => {
      const canvas = root.current?.closest(".original-live-canvas");
      if (!canvas || !menuRef.current || !(event.target instanceof Node) || !canvas.contains(event.target)) return;
      event.preventDefault();
      const point = contextRef.current.toPage(event.clientX, event.clientY);
      menuRef.current(visualAt(contextRef.current.rects, point), { x: event.clientX, y: event.clientY });
    };
    document.addEventListener("contextmenu", onMenu);
    return () => document.removeEventListener("contextmenu", onMenu);
  }, []);

  const byId = new Map(context.rects.map(rect => [rect.id, rect]));
  const rectOf = (id: string): Rect | undefined => { const r = byId.get(id); return r ? { x: r.x, y: r.y, width: r.width, height: r.height } : undefined; };

  const finish = useRef<() => void>(() => undefined);
  const begin = (next: Gesture) => {
    gesture.current = next;
    const move = (event: PointerEvent) => {
      const current = gesture.current;
      if (!current) return;
      // The button was released outside the window, where no pointerup arrives: end the gesture here.
      if (event.buttons === 0) { up(); return; }
      if (!current.moved && Math.hypot(event.clientX - current.client.x, event.clientY - current.client.y) < DRAG_THRESHOLD) return;
      current.moved = true;
      const ctx = contextRef.current;
      const point = ctx.toPage(event.clientX, event.clientY);
      const dx = point.x - current.origin.x, dy = point.y - current.origin.y;
      const snap = !event.shiftKey;
      current.last = current.mode === "move"
        ? moveRects(current.start, current.ids, dx, dy, ctx.page, { snap })
        : { [current.ids[0]]: resizeRect(current.start[current.ids[0]], current.handle ?? "se", dx, dy, ctx.page, { snap }) };
      setHud(current.last[current.ids[0]] ? { id: current.ids[0], rect: current.last[current.ids[0]] } : undefined);
      handlers.current.onPreview(current.last);
    };
    const up = () => {
      const current = gesture.current;
      finish.current();
      if (current?.moved && current.last) handlers.current.onCommit(current.last);
    };
    const cancel = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      const current = gesture.current;
      finish.current();
      if (current?.moved) handlers.current.onAbort(current.ids);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    window.addEventListener("keydown", cancel, true);
    finish.current = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      window.removeEventListener("keydown", cancel, true);
      gesture.current = undefined;
      setHud(undefined);
      finish.current = () => undefined;
    };
  };

  // A pointer down on the canvas: the canvas' own handler (React, at the root) has already updated
  // the selection, and this document listener runs after it.
  useEffect(() => {
    const onDown = (event: PointerEvent) => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || lockedRef.current || gesture.current) return;
      const canvas = root.current?.closest(".original-live-canvas");
      const target = event.target;
      if (!canvas || !(target instanceof Element) || !canvas.contains(target) || target.closest(".ie-handle") || !target.closest(".report-visuals")) return;
      const ids = selectionRef.current;
      if (!ids.length) return;
      const ctx = contextRef.current;
      const point = ctx.toPage(event.clientX, event.clientY);
      const rects = ctx.rects.filter(r => ids.includes(r.id));
      if (!rects.some(r => point.x >= r.x && point.x <= r.x + r.width && point.y >= r.y && point.y <= r.y + r.height)) return;
      begin({ mode: "move", ids: rects.map(r => r.id), start: Object.fromEntries(rects.map(r => [r.id, { x: r.x, y: r.y, width: r.width, height: r.height }])), origin: point, client: { x: event.clientX, y: event.clientY }, moved: false });
    };
    document.addEventListener("pointerdown", onDown);
    return () => { document.removeEventListener("pointerdown", onDown); finish.current(); };
  }, [selectionRef]);

  const startResize = (event: ReactPointerEvent, id: string, handle: Handle) => {
    if (event.button !== 0 || locked) return;
    event.preventDefault();
    event.stopPropagation();
    const start = rectOf(id);
    if (!start) return;
    begin({ mode: "resize", ids: [id], handle, start: { [id]: start }, origin: context.toPage(event.clientX, event.clientY), client: { x: event.clientX, y: event.clientY }, moved: false });
  };

  const frames = selectedIds.map(id => ({ id, rect: rectOf(id), kind: byId.get(id)?.kind })).filter((item): item is { id: string; rect: Rect; kind: string | undefined } => Boolean(item.rect));
  const single = frames.length === 1 && frames[0].kind !== "group" && !locked;
  const unit = 1 / Math.max(context.scale, 0.1);
  const targets = interactions ? context.rects.filter(rect => rect.id !== interactions.source && rect.kind !== "group" && !rect.hidden && interactions.choices(rect.id).length) : [];
  return <div ref={root} className="ie-overlay" style={{ "--ie-unit": `${unit}px` } as CSSProperties}>
    {frames.map(({ id, rect }, index) => <div key={id} className={`ie-frame${index === 0 ? " ie-frame-main" : ""}${locked ? " locked" : ""}`} style={{ left: rect.x, top: rect.y, width: rect.width, height: rect.height }} aria-hidden="true">
      {single && !interactions && HANDLES.map(handle => <span key={handle} className={`ie-handle ie-handle-${handle}`} onPointerDown={event => startResize(event, id, handle)} />)}
      {hud?.id === id && <span className="ie-hud">{Math.round(hud.rect.x)}, {Math.round(hud.rect.y)} · {Math.round(hud.rect.width)} × {Math.round(hud.rect.height)}</span>}
      {frames.length === 1 && titles[id] && <span className="ie-frame-title">{titles[id]}</span>}
    </div>)}
    {badges && context.rects.filter(rect => badges.has(rect.id) && !rect.hidden).map(rect => {
      const status = badges.get(rect.id);
      return <span key={`test-${rect.id}`} className={`ie-test-badge ${status}`} style={{ left: rect.x + rect.width, top: rect.y }} title={status === "failed" ? "A report test on this visual fails" : status === "passed" ? "Report tests on this visual pass" : "Report tests on this visual were not checked yet"}><Pin aria-hidden="true" /></span>;
    })}
    {interactions && targets.map(rect => {
      const current = interactions.current(rect.id);
      return <div key={`int-${rect.id}`} className="ie-interactions" role="group" aria-label={`How ${titles[rect.id] ?? rect.id} reacts to a selection`} style={{ left: rect.x + rect.width, top: rect.y }}>
        {interactions.choices(rect.id).map(type => {
          const Icon = INTERACTION_ICONS[type];
          return <button type="button" key={type} className={current === type ? "on" : undefined} aria-pressed={current === type} disabled={locked} title={INTERACTION_LABELS[type]} aria-label={`${INTERACTION_LABELS[type]}: ${titles[rect.id] ?? rect.id}`}
            onPointerDown={event => event.stopPropagation()} onClick={() => interactions.onSet(rect.id, type)}><Icon aria-hidden="true" /></button>;
        })}
      </div>;
    })}
  </div>;
}
