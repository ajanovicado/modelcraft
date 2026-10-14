// Canvas geometry for editing an imported report: moving, resizing and nudging in page pixels,
// and the coalescer that turns a burst of gestures into one edit request (twelve nudges = one
// revision). Pure functions; the overlay component only forwards pointer and key events.

import type { Operation } from "./importedEditing";

export type Rect = { x: number; y: number; width: number; height: number };
export type Size = { width: number; height: number };
export type Handle = "n" | "ne" | "e" | "se" | "s" | "sw" | "w" | "nw";
export const HANDLES: Handle[] = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
export const GRID = 5;
export const MIN_SIZE = 10;

const round2 = (value: number) => Math.round(value * 100) / 100;
/** Snaps to the grid unless the user holds Shift. */
export const snapTo = (value: number, grid: number, enabled: boolean) => enabled ? Math.round(value / grid) * grid : round2(value);
const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(high, value));

export const sameRect = (a: Rect, b: Rect) => a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height;

export function boundsOf(rects: Rect[]): Rect {
  const left = Math.min(...rects.map(r => r.x)), top = Math.min(...rects.map(r => r.y));
  const right = Math.max(...rects.map(r => r.x + r.width)), bottom = Math.max(...rects.map(r => r.y + r.height));
  return { x: left, y: top, width: right - left, height: bottom - top };
}

/** Moves every selected rect by the same offset, keeping them together and inside the page. */
export function moveRects(start: Record<string, Rect>, ids: string[], dx: number, dy: number, page: Size, options: { snap: boolean; grid?: number } = { snap: true }): Record<string, Rect> {
  const chosen = ids.filter(id => start[id]);
  if (!chosen.length) return {};
  const grid = options.grid ?? GRID;
  const box = boundsOf(chosen.map(id => start[id]));
  // Snap the group's top-left corner, then keep the whole group on the page.
  const targetX = clamp(snapTo(box.x + dx, grid, options.snap), 0, Math.max(0, page.width - box.width));
  const targetY = clamp(snapTo(box.y + dy, grid, options.snap), 0, Math.max(0, page.height - box.height));
  const offsetX = round2(targetX - box.x), offsetY = round2(targetY - box.y);
  return Object.fromEntries(chosen.map(id => [id, { ...start[id], x: round2(start[id].x + offsetX), y: round2(start[id].y + offsetY) }]));
}

export function resizeRect(start: Rect, handle: Handle, dx: number, dy: number, page: Size, options: { snap: boolean; grid?: number; min?: number } = { snap: true }): Rect {
  const grid = options.grid ?? GRID, min = options.min ?? MIN_SIZE;
  let left = start.x, top = start.y, right = start.x + start.width, bottom = start.y + start.height;
  if (handle.includes("w")) left = clamp(snapTo(left + dx, grid, options.snap), 0, right - min);
  if (handle.includes("e")) right = clamp(snapTo(right + dx, grid, options.snap), left + min, page.width);
  if (handle === "n" || handle === "nw" || handle === "ne") top = clamp(snapTo(top + dy, grid, options.snap), 0, bottom - min);
  if (handle === "s" || handle === "sw" || handle === "se") bottom = clamp(snapTo(bottom + dy, grid, options.snap), top + min, page.height);
  return { x: round2(left), y: round2(top), width: round2(right - left), height: round2(bottom - top) };
}

const KEY_STEP: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
export const isNudgeKey = (key: string) => key in KEY_STEP;
/** Arrow keys move 1 px, Shift 10 px, and never snap (a nudge is a fine adjustment). */
export function nudgeRects(start: Record<string, Rect>, ids: string[], key: string, shift: boolean, page: Size): Record<string, Rect> {
  const step = KEY_STEP[key];
  if (!step) return {};
  const size = shift ? 10 : 1;
  return moveRects(start, ids, step[0] * size, step[1] * size, page, { snap: false });
}

/** Absolute page-pixel `set_position` operations for the rects that actually changed. */
export function positionOps(base: Record<string, Rect>, next: Record<string, Rect>): Operation[] {
  return Object.entries(next).flatMap(([visual, rect]) => {
    const before = base[visual];
    if (before && sameRect(before, rect)) return [];
    const op: Operation = { op: "set_position", visual, x: round2(rect.x), y: round2(rect.y), width: round2(rect.width), height: round2(rect.height) };
    // A pure move must not resize (the server clamps a size to the page); send only what changed.
    if (before && before.width === rect.width && before.height === rect.height) { delete op.width; delete op.height; }
    return [op];
  });
}

export function alignRects(rects: Record<string, Rect>, ids: string[], how: "left" | "center" | "right" | "top" | "middle" | "bottom"): Record<string, Rect> {
  const chosen = ids.filter(id => rects[id]);
  if (chosen.length < 2) return {};
  const box = boundsOf(chosen.map(id => rects[id]));
  return Object.fromEntries(chosen.map(id => {
    const r = rects[id];
    const next = { ...r };
    if (how === "left") next.x = box.x;
    if (how === "right") next.x = round2(box.x + box.width - r.width);
    if (how === "center") next.x = round2(box.x + (box.width - r.width) / 2);
    if (how === "top") next.y = box.y;
    if (how === "bottom") next.y = round2(box.y + box.height - r.height);
    if (how === "middle") next.y = round2(box.y + (box.height - r.height) / 2);
    return [id, next];
  }));
}

/** Even gaps between three or more visuals along one axis; the outer two stay put. */
export function distributeRects(rects: Record<string, Rect>, ids: string[], axis: "horizontal" | "vertical"): Record<string, Rect> {
  const chosen = ids.filter(id => rects[id]);
  if (chosen.length < 3) return {};
  const horizontal = axis === "horizontal";
  const sorted = [...chosen].sort((a, b) => horizontal ? rects[a].x - rects[b].x : rects[a].y - rects[b].y);
  const first = rects[sorted[0]], last = rects[sorted[sorted.length - 1]];
  const total = horizontal ? last.x + last.width - first.x : last.y + last.height - first.y;
  const used = sorted.reduce((sum, id) => sum + (horizontal ? rects[id].width : rects[id].height), 0);
  const gap = (total - used) / (sorted.length - 1);
  let cursor = horizontal ? first.x : first.y;
  return Object.fromEntries(sorted.map(id => {
    const r = rects[id];
    const next = horizontal ? { ...r, x: round2(cursor) } : { ...r, y: round2(cursor) };
    cursor += (horizontal ? r.width : r.height) + gap;
    return [id, next];
  }));
}

export type HitRect = { id: string; x: number; y: number; width: number; height: number; rank: number; hidden?: boolean; kind?: string; parentGroup?: string | null };

/**
 * The visual under a point, top-most first (rank is the stacking order). A group frame has no
 * content of its own, so a click reaches its members; Alt selects the group instead. Hidden
 * visuals are drawn as ghosts and are chosen from the visuals list, not by clicking.
 */
export function hitTest(rects: HitRect[], x: number, y: number, options: { alt?: boolean } = {}): string | null {
  const inside = rects.filter(r => x >= r.x && x <= r.x + r.width && y >= r.y && y <= r.y + r.height && !r.hidden);
  const ordered = [...inside].sort((a, b) => b.rank - a.rank);
  if (options.alt) {
    const grouped = ordered.find(r => r.parentGroup);
    if (grouped?.parentGroup) return grouped.parentGroup;
    return ordered[0]?.id ?? null;
  }
  return ordered.find(r => r.kind !== "group")?.id ?? null;
}

/** Applies optimistic rects over the server's, so the canvas shows the gesture before the revision lands. */
export function withOverride<T extends Rect & { id: string }>(items: T[], override: Record<string, Rect>): T[] {
  return items.map(item => override[item.id] ? { ...item, ...override[item.id] } : item);
}
export function dropOverride<T>(override: Record<string, T>, ids: Iterable<string>): Record<string, T> {
  const next = { ...override };
  for (const id of ids) delete next[id];
  return next;
}

/**
 * An optimistic rectangle remembers where the server had the visual when the gesture began (`from`). Once a
 * render shows the visual anywhere else, the server has spoken (the change landed, or an Undo or a Desktop
 * save moved it) and the entry is dropped, whatever order the receipt and the render arrive in. While the
 * server still shows `from`, the change is in flight and the entry keeps the visual where the user put it.
 */
export type OverrideEntry = { rect: Rect; from: Rect };
export type OverrideMap = Record<string, OverrideEntry>;

export function stageOverride(current: OverrideMap, next: Record<string, Rect>, server: (id: string) => Rect | undefined): OverrideMap {
  const merged = { ...current };
  for (const [id, rect] of Object.entries(next)) merged[id] = { rect, from: current[id]?.from ?? server(id) ?? rect };
  return merged;
}
export const overrideRects = (map: OverrideMap): Record<string, Rect> => Object.fromEntries(Object.entries(map).map(([id, entry]) => [id, entry.rect]));
export function settleOverride(map: OverrideMap, server: Record<string, Rect | undefined>): OverrideMap {
  const kept: OverrideMap = {};
  for (const [id, entry] of Object.entries(map)) {
    const seen = server[id];
    if (!seen || sameRect(seen, entry.from)) kept[id] = entry;
  }
  return Object.keys(kept).length === Object.keys(map).length ? map : kept;
}

// --- coalescing ---------------------------------------------------------------------------------

export type Scheduler = { set: (fn: () => void, ms: number) => unknown; clear: (handle: unknown) => void };
const realScheduler: Scheduler = { set: (fn, ms) => setTimeout(fn, ms), clear: handle => clearTimeout(handle as ReturnType<typeof setTimeout>) };

/**
 * Collects gesture results per visual and hands them on as ONE batch: at once when a pointer
 * gesture ends ("now"), or after `idleMs` without another key press ("idle"). A later gesture on
 * the same visual replaces the earlier one, because operations carry absolute positions.
 */
export class GestureCoalescer {
  private staged: Record<string, Rect> = {};
  private timer: unknown;
  constructor(private readonly flush: (changes: Record<string, Rect>) => void, private readonly idleMs = 2000, private readonly scheduler: Scheduler = realScheduler) {}

  stage(changes: Record<string, Rect>, settle: "now" | "idle"): void {
    this.staged = { ...this.staged, ...changes };
    this.cancelTimer();
    if (settle === "now") this.flushNow();
    else this.timer = this.scheduler.set(() => { this.timer = undefined; this.flushNow(); }, this.idleMs);
  }
  flushNow(): void {
    this.cancelTimer();
    const changes = this.staged;
    this.staged = {};
    if (Object.keys(changes).length) this.flush(changes);
  }
  pending(): Record<string, Rect> { return this.staged; }
  hasPending(): boolean { return Object.keys(this.staged).length > 0; }
  /** Forgets staged gestures (Esc, a page change); returns the ids so the caller can roll them back. */
  cancel(): string[] { this.cancelTimer(); const ids = Object.keys(this.staged); this.staged = {}; return ids; }
  private cancelTimer() { if (this.timer !== undefined) { this.scheduler.clear(this.timer); this.timer = undefined; } }
}

/** One request at a time, in order: a gesture made while a batch is in flight waits for its receipt (and the new head). */
export class SerialQueue {
  private tail: Promise<unknown> = Promise.resolve();
  private active = 0;
  run<T>(task: () => Promise<T>): Promise<T> {
    this.active += 1;
    const next = this.tail.then(task, task).finally(() => { this.active -= 1; });
    this.tail = next.catch(() => undefined);
    return next;
  }
  get busy(): boolean { return this.active > 0; }
}

/** A free spot of `size` on the page for a new visual: the first grid cell that overlaps nothing, else the corner. */
export function findFreeSpot(existing: Rect[], size: Size, page: Size, grid = 20): { x: number; y: number } {
  const overlaps = (x: number, y: number) => existing.some(r => x < r.x + r.width && x + size.width > r.x && y < r.y + r.height && y + size.height > r.y);
  for (let y = grid; y + size.height <= page.height; y += grid) {
    for (let x = grid; x + size.width <= page.width; x += grid) {
      if (!overlaps(x, y)) return { x, y };
    }
  }
  return { x: grid, y: grid };
}
