import type { Visual } from "./types";
import { minimumVisualSize } from "./reportVisualCatalog";
export { minimumVisualSize } from "./reportVisualCatalog";

export type Placement = Pick<Visual, "x" | "y" | "width" | "height">;
// Match the compiler's readable minimums; the report remains a bounded 12 × 12 grid.

export function clampPlacement(visual: Visual, update: Partial<Placement> = {}): Placement {
  const [minWidth, minHeight] = minimumVisualSize[visual.recipe];
  const finite = (value: number, fallback: number) => Number.isFinite(value) ? Math.round(value) : fallback;
  const width = Math.max(minWidth, Math.min(12, finite(update.width ?? visual.width, minWidth)));
  const height = Math.max(minHeight, Math.min(12, finite(update.height ?? visual.height, minHeight)));
  return { width, height,
    x: Math.max(0, Math.min(12 - width, finite(update.x ?? visual.x, 0))),
    y: Math.max(0, Math.min(12 - height, finite(update.y ?? visual.y, 0))),
  };
}

export const overlaps = (a: Placement, b: Placement) =>
  a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

export function canPlace(visuals: Visual[], id: string, place: Placement): boolean {
  return [place.x, place.y, place.width, place.height].every(Number.isInteger) && place.x >= 0 && place.y >= 0
    && place.width > 0 && place.height > 0 && place.x + place.width <= 12 && place.y + place.height <= 12
    && !visuals.some(v => v.id !== id && overlaps(v, place));
}

export function findOpenPlacement(visuals: Visual[], visual: Visual): Placement | undefined {
  const size = clampPlacement(visual);
  if (canPlace(visuals, visual.id, size)) return size;
  for (let y = 0; y <= 12 - size.height; y++) {
    for (let x = 0; x <= 12 - size.width; x++) {
      const place = { ...size, x, y };
      if (canPlace(visuals, visual.id, place)) return place;
    }
  }
}

/** Bounded packing used only by explicit arrange and when insertion needs room. */
export function arrangeVisuals(visuals: Visual[], compact = false): Visual[] | undefined {
  if (visuals.length > 12) return undefined;
  const prepared = visuals.map(v => ({ ...v, ...clampPlacement(v, compact
    ? { width: minimumVisualSize[v.recipe][0], height: minimumVisualSize[v.recipe][1] } : {}) }));
  if (prepared.reduce((area, v) => area + v.width * v.height, 0) > 144) return undefined;
  const ordered = [...prepared].sort((a, b) => Number(b.recipe === "kpi") - Number(a.recipe === "kpi")
    || b.height - a.height || b.width - a.width);
  const result = new Map<string, Placement>();
  let attempts = 0;
  const place = (index: number, rows: number[]): boolean => {
    if (index === ordered.length) return true;
    const v = ordered[index];
    for (let y = 0; y <= 12 - v.height; y++) {
      for (let x = 0; x <= 12 - v.width; x++) {
        if (++attempts > 12000) return false;
        const mask = ((1 << v.width) - 1) << x;
        if (rows.slice(y, y + v.height).some(row => row & mask)) continue;
        const next = [...rows];
        for (let row = y; row < y + v.height; row++) next[row] |= mask;
        result.set(v.id, { x, y, width: v.width, height: v.height });
        if (place(index + 1, next)) return true;
        result.delete(v.id);
      }
    }
    return false;
  };
  return place(0, Array<number>(12).fill(0)) ? prepared.map(v => ({ ...v, ...result.get(v.id)! })) : undefined;
}

export function insertVisual(visuals: Visual[], visual: Visual): { visuals: Visual[]; rearranged: boolean } | undefined {
  if (visuals.length >= 12) return undefined;
  const place = findOpenPlacement(visuals, visual);
  if (place) return { visuals: [...visuals, { ...visual, ...place }], rearranged: false };
  const arranged = arrangeVisuals([...visuals, visual], true);
  return arranged ? { visuals: arranged, rearranged: true } : undefined;
}
