import { useEffect, useState } from "react";
import { editTransport } from "./api";
import type { ThemePalette } from "./importedEditing";
import { readPaneMeta, type PaneMeta } from "./importedPanes";

/** `pane`: bookmarks, layers, interactions, synced slicers, phone layout and page type (render contract, sections 1 and 16-20). */
export type RenderMeta = { theme?: ThemePalette; pageHidden?: boolean; pane?: PaneMeta; pageId?: string };

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const colors = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

/** Reads the theme and the page facts out of a formatting render (docs/imported-report-render-contract.md, sections 1 and 10). */
export function readRenderMeta(body: unknown): RenderMeta {
  if (!isRecord(body)) return {};
  const theme = isRecord(body.theme) && colors(body.theme.dataColors).length
    ? {
      name: typeof body.theme.name === "string" ? body.theme.name : undefined,
      dataColors: colors(body.theme.dataColors), palette: colors(body.theme.palette),
      foreground: typeof body.theme.foreground === "string" ? body.theme.foreground : undefined,
      background: typeof body.theme.background === "string" ? body.theme.background : undefined,
    } satisfies ThemePalette : undefined;
  const page = isRecord(body.page) && typeof body.page.hidden === "boolean" ? body.page.hidden : undefined;
  return { theme, pageHidden: page, pane: readPaneMeta(body) };
}

/**
 * The report's effective theme (the conditional formatting color picker offers its colors) and the pane facts come
 * with any formatting render, which runs no query. It is asked for again when the head moves, because an edit can
 * change the pane facts (a bookmark, a group, a page type).
 */
export function useRenderMeta(workspaceId: string, pageId: string | undefined, headId: string | undefined, active: boolean): RenderMeta {
  const [meta, setMeta] = useState<RenderMeta>({});
  useEffect(() => {
    if (!active || !pageId) return;
    let cancelled = false;
    const query = headId ? `?revision=${encodeURIComponent(headId)}` : "";
    editTransport("POST", `/v1/workspaces/${encodeURIComponent(workspaceId)}/imported-report/pages/${encodeURIComponent(pageId)}/render-formatting${query}`, { selections: [], searches: {} })
      .then(response => { if (!cancelled && response.status < 400) setMeta({ ...readRenderMeta(response.body), pageId }); })
      .catch(() => undefined); // the picker still works with Power BI's default palette
    return () => { cancelled = true; };
  }, [workspaceId, pageId, headId, active]);
  return meta;
}
