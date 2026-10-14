import { useContext, useEffect, useRef, useState } from "react";
import type { VisualizationSpec } from "vega-embed";
import { ReportPaletteContext } from "./reportPalette";

/** The draft theme colors marks so previews match the Power BI build. Keys must be
 * absent rather than undefined: Vega-Lite crashes on `mark: undefined`. */
export function paletteConfig(palette?: string[]): { range?: { category: string[] }; mark?: { color: string } } {
  return palette?.length ? { range: { category: palette }, mark: { color: palette[0] } } : {};
}

/** Merge a caller's Vega config over the defaults one level deep, so `{ axis: { labelColor } }`
 * keeps the default axis settings it does not name. */
export function mergeConfig(base: Record<string, unknown>, extra?: Record<string, unknown>): Record<string, unknown> {
  if (!extra) return base;
  const merged: Record<string, unknown> = { ...base };
  for (const [key, value] of Object.entries(extra)) {
    const current = merged[key];
    const plain = (item: unknown): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item);
    merged[key] = plain(current) && plain(value) ? { ...current, ...value } : value;
  }
  return merged;
}

export function chartSpec(
  spec: Record<string, unknown>,
  label: string,
  size: { width: number; height: number },
  formatString?: string,
) {
  const encoding = (spec.encoding || {}) as Record<string, Record<string, unknown>>;
  // Polar charts (pie, donut) have no x or y axes to default.
  if (encoding.theta) return spec;
  const x = encoding.x || {};
  const y = encoding.y || {};
  const byMatch = label.match(/^(.*?)\s+by\s+(.+)$/i);
  const trendMatch = label.match(/^(.*?)\s+over\s+time$/i);
  const metric = (byMatch?.[1] || trendMatch?.[1] || "Value").trim();
  const category = (byMatch?.[2] || "Date").trim();
  const temporal = x.type === "temporal";
  const columnChart = x.type === "nominal" || x.type === "ordinal";
  const availableWidth = Math.max(160, size.width);
  const tickCount = availableWidth < 280 ? 2 : availableWidth < 440 ? 4 : 6;
  const categoryLabelLimit = Math.min(110, Math.max(50, Math.floor(availableWidth * 0.3)));
  // Plain "~s" labels 0.4 as "400m" (read as millions) and billions as "G". Abbreviate
  // only from one thousand, with B for billions; smaller values keep ordinary digits.
  const quantitativeAxis = formatString?.includes("%")
    ? { format: `.${formatString.match(/\.(0+)/)?.[1].length ?? 1}%` }
    : { labelExpr: "abs(datum.value) >= 1000 ? replace(format(datum.value, '.3~s'), 'G', 'B') : format(datum.value, ',.3~r')" };
  return {
    ...spec,
    encoding: {
      ...encoding,
      x: {
        ...x,
        axis: temporal
          ? { title: x.title ?? category, format: "%b %Y", labelAngle: 0, labelBound: true, labelOverlap: "greedy", tickCount, ...(x.axis || {}) }
          : columnChart
            ? { title: x.title ?? category, labelAngle: 0, labelLimit: categoryLabelLimit, labelOverlap: "greedy", ...(x.axis || {}) }
            : { title: x.title ?? metric, ...quantitativeAxis, labelBound: true, labelFlush: true, labelOverlap: "greedy", tickCount, ...(x.axis || {}) },
      },
      y: {
        ...y,
        axis: temporal || columnChart
          ? { title: y.title ?? metric, ...quantitativeAxis, labelOverlap: "greedy", tickCount, ...(y.axis || {}) }
          : { title: y.title ?? category, labelLimit: categoryLabelLimit, ...(y.axis || {}) },
      },
    },
  };
}

/** Where the pointer is when a data point is right-clicked or hovered (client coordinates). */
export type DatumPointer = { clientX: number; clientY: number };

/** A data point of the chart's own data (the imported-report canvas puts `row` and `category` on each), or
 * undefined for a mark that is not one (an analytics line, a label, the plot background). */
export function pointDatum(item: unknown): Record<string, unknown> | undefined {
  const datum = item && typeof item === "object" ? (item as { datum?: unknown }).datum : undefined;
  return datum && typeof datum === "object" && Object.hasOwn(datum, "category") ? datum as Record<string, unknown> : undefined;
}

/** `config` is optional Vega config merged over the built-in look (the imported-report canvas passes
 * the report's fonts here). Callers memoize it: a new object re-embeds the chart. The context-menu and
 * hover handlers are read through refs, so passing new ones does not re-embed. */
export function PreviewChart({ spec, label, formatString, onDatumClick, onDatumContextMenu, onDatumHover, config }: {
  spec: Record<string, unknown>; label: string; formatString?: string; onDatumClick?: (datum: Record<string, unknown>) => void; config?: Record<string, unknown>;
  /** Right-click on a data point (the browser menu is suppressed only when this is set). */
  onDatumContextMenu?: (datum: Record<string, unknown>, pointer: DatumPointer) => void;
  /** The pointer entered a data point (datum) or left every point (null). */
  onDatumHover?: (datum: Record<string, unknown> | null, pointer: DatumPointer) => void;
}) {
  const node = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [error, setError] = useState(false);
  const palette = useContext(ReportPaletteContext);
  const menuRef = useRef(onDatumContextMenu);
  const hoverRef = useRef(onDatumHover);
  useEffect(() => { menuRef.current = onDatumContextMenu; hoverRef.current = onDatumHover; });
  const wantsMenu = !!onDatumContextMenu;
  const wantsHover = !!onDatumHover;

  useEffect(() => {
    const target = node.current;
    if (!target || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      const next = {
        width: Math.round(entry.contentRect.width),
        height: Math.round(entry.contentRect.height),
      };
      setSize((current) => current.width === next.width && current.height === next.height ? current : next);
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!node.current || !Object.keys(spec).length) return;
    const target = node.current;
    let cancelled = false;
    let finalize: (() => void) | undefined;
    setError(false);
    void import("vega-embed").then(async ({ default: embed }) => {
      if (cancelled) return;
      const prepared = chartSpec(spec, label, size, formatString);
      // Interpret expression ASTs so charts also render under the desktop CSP,
      // which intentionally disallows Function-constructor evaluation.
      const result = await embed(target, {
        ...prepared,
        $schema: "https://vega.github.io/schema/vega-lite/v6.json",
        description: label,
        width: Math.max(160, size.width || target.clientWidth),
        height: Math.max(120, size.height || target.clientHeight),
        autosize: { type: "fit", contains: "padding", resize: true },
        background: "transparent",
        config: mergeConfig({
          font: "Segoe UI, sans-serif",
          axis: {
            labelColor: "#526071",
            titleColor: "#526071",
            gridColor: "#edf0f3",
            labelFontSize: 10,
            titleFontSize: 10,
            titlePadding: 9,
          },
          view: { stroke: null },
          ...paletteConfig(palette),
          line: { strokeWidth: 2 },
          bar: { cornerRadiusEnd: 2 },
        }, config),
      } as VisualizationSpec, { actions: false, renderer: "svg", ast: true });
      // Browsers also expose the inner SVG as an image, so name both surfaces.
      target.querySelector("svg")?.setAttribute("aria-label", label);
      finalize = () => result.finalize();
      if (onDatumClick) result.view.addEventListener("click", (_event, item) => {
        if (item?.datum) onDatumClick(item.datum as Record<string, unknown>);
      });
      if (wantsMenu) result.view.addEventListener("contextmenu", (event, item) => {
        const datum = pointDatum(item);
        const mouse = event as MouseEvent;
        if (!datum) return;
        mouse.preventDefault?.();
        menuRef.current?.(datum, { clientX: mouse.clientX, clientY: mouse.clientY });
      });
      if (wantsHover) {
        let current: unknown = null;
        result.view.addEventListener("mousemove", (event, item) => {
          const datum = pointDatum(item) ?? null;
          const mouse = event as MouseEvent;
          if (datum === current) return;
          current = datum;
          hoverRef.current?.(datum, { clientX: mouse.clientX, clientY: mouse.clientY });
        });
        result.view.addEventListener("mouseout", (event, item) => {
          if (!pointDatum(item) || !current) return;
          current = null;
          const mouse = event as MouseEvent;
          hoverRef.current?.(null, { clientX: mouse.clientX, clientY: mouse.clientY });
        });
      }
      if (cancelled) finalize();
    }).catch(() => {
      if (!cancelled) setError(true);
    });
    return () => {
      cancelled = true;
      finalize?.();
    };
  }, [formatString, label, size, spec, onDatumClick, palette, config, wantsMenu, wantsHover]);

  if (!Object.keys(spec).length) return <div className="chart-empty">Preview evidence unavailable</div>;
  if (error) return <div className="chart-empty">This preview could not be rendered</div>;
  return <div ref={node} className="chart" role="img" aria-label={label} />;
}
