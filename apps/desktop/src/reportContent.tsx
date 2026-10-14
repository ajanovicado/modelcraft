import { useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, Bookmark, CircleArrowLeft, CircleQuestionMark, Info, MessageCircleQuestionMark, RotateCcw } from "lucide-react";

import {
  paragraphStyle, pictureFit, runStyle, safeColor, safeImage, shapeStyle,
  type Action, type ButtonToken, type FidelityNote, type NavigatorItem, type RenderVisual, type ShapeToken, type TextParagraph,
} from "./reportRender";
import { buttonBoxStyle, buttonStateName, buttonTextStyle, navigatorGridStyle, navigatorStateName, stateToken } from "./reportActions";
import { iconGlyph, type IconGlyph } from "./reportCells";

// Visuals that need no query: a textbox, a shape, an image, a button and a navigator; plus the small pieces the
// canvas draws inside cells (conditional icons), its menus and its confirmation. Payload text is only ever
// rendered as text nodes, colors are re-validated, and image sources are re-checked as data URIs.

export function TextboxBody({ paragraphs }: { paragraphs: TextParagraph[] }) {
  return <div className="report-textbox">{paragraphs.map((paragraph, i) => <p key={i} style={paragraphStyle(paragraph)}>
    {paragraph.runs.length ? paragraph.runs.map((run, j) => <span key={j} style={runStyle(run)}>{run.text}</span>) : " "}
  </p>)}</div>;
}

export function ShapeBody({ shape, box }: { shape: ShapeToken | undefined; box: { width: number; height: number } }) {
  return <div className="report-shape" aria-hidden="true"><div style={shapeStyle(shape, box)} /></div>;
}

export function PictureBody({ src, scaling, label }: { src: string | null | undefined; scaling: string | undefined; label: string }) {
  const safe = safeImage(src);
  // A picture the report sets but Studio cannot read (a web address, a measure) is an empty frame, never a broken image.
  if (!safe) return <div className="report-picture-empty" role="img" aria-label={`${label} (picture shown in Power BI only)`} />;
  return <img className="report-picture" src={safe} alt={label} style={{ objectFit: pictureFit(scaling) }} />;
}

/** The calm frame of a visual Studio does not draw: its box, its name and why, never a red error. */
export function FidelityFrame({ visual, note }: { visual: RenderVisual; note: FidelityNote }) {
  return <div className={`report-frame report-frame-${note.level}`} role="note">
    <strong>{note.level === "desktop" ? note.label : visual.title}</strong>
    <span>{note.detail}</span>
    <em>{note.level === "none" ? note.label : "Preview in Power BI"}</em>
  </div>;
}

/** A small mark on a visual that is not drawn exactly: the server's plain reason is its text. */
export function FidelityBadge({ note }: { note: FidelityNote }) {
  const text = `${note.label}. ${note.detail}`;
  return <span className={`fidelity-badge${note.data ? " data" : ""}`} role="note" title={text} aria-label={text}><Info size={12} aria-hidden="true" /></span>;
}

// --- Conditional icons ---------------------------------------------------------------------------------

const OFF = "#C8C6C4";

function Glyph({ glyph, clip }: { glyph: IconGlyph; clip: string }): ReactNode {
  const color = safeColor(glyph.color) ?? "#605E5C";
  const mark = (symbol: IconGlyph["symbol"], stroke: string) => symbol === "x" ? <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke={stroke} strokeWidth="1.8" strokeLinecap="round" />
    : symbol === "!" ? <><path d="M8 4.5v4.2" stroke={stroke} strokeWidth="1.8" strokeLinecap="round" /><circle cx="8" cy="11.3" r="1" fill={stroke} /></>
    : symbol === "check" ? <path d="M4.8 8.3l2.2 2.2 4.2-4.6" stroke={stroke} strokeWidth="1.8" fill="none" strokeLinecap="round" strokeLinejoin="round" /> : null;
  switch (glyph.shape) {
    case "arrow": return <g transform={`rotate(${glyph.angle ?? 0} 8 8)`}><path d="M2.5 8h10M9 4l4 4-4 4" stroke={color} strokeWidth="2.2" fill="none" strokeLinecap="round" strokeLinejoin="round" /></g>;
    case "circle": return <><circle cx="8" cy="8" r="6" fill={color} stroke={glyph.rim ? "#252423" : "none"} strokeWidth={glyph.rim === "dark" ? 2 : 1} />{mark(glyph.symbol, "#ffffff")}</>;
    case "symbol": return mark(glyph.symbol, color);
    case "flag": return <><path d="M4 2v12" stroke="#605E5C" strokeWidth="1.4" strokeLinecap="round" /><path d="M4.6 2.8h8l-2.2 3 2.2 3h-8z" fill={color} /></>;
    case "triangle": return <path d={glyph.direction === "down" ? "M2 4h12L8 13z" : "M8 3l6 10H2z"} fill={color} />;
    case "diamond": return <path d="M8 2l6 6-6 6-6-6z" fill={color} />;
    case "dash": return <rect x="2" y="6.3" width="12" height="3.4" rx="1" fill={color} />;
    case "bars": return <>{[4, 7, 10, 13].map((height, i) => <rect key={i} x={1.5 + i * 3.6} y={14.5 - height} width="2.6" height={height} fill={i < (glyph.fill ?? 0) ? color : OFF} />)}</>;
    case "star": {
      const star = "M8 1.6l1.9 4.1 4.5.5-3.4 3 .9 4.4L8 11.4l-3.9 2.2.9-4.4-3.4-3 4.5-.5z";
      return <><defs><clipPath id={clip}><rect x="0" y="0" width={16 * (glyph.fill ?? 0)} height="16" /></clipPath></defs>
        <path d={star} fill="#ffffff" stroke={color} strokeWidth="1" /><path d={star} fill={color} clipPath={`url(#${clip})`} /></>;
    }
    case "pie": {
      const fill = glyph.fill ?? 0;
      const angle = fill * 2 * Math.PI;
      const x = 8 + 6 * Math.sin(angle);
      const y = 8 - 6 * Math.cos(angle);
      return <><circle cx="8" cy="8" r="6" fill="#ffffff" stroke={color} strokeWidth="1.2" />
        {fill >= 1 ? <circle cx="8" cy="8" r="6" fill={color} /> : fill > 0 ? <path d={`M8 8V2A6 6 0 ${fill > 0.5 ? 1 : 0} 1 ${x.toFixed(2)} ${y.toFixed(2)}z`} fill={color} /> : null}</>;
    }
    case "quadrant": {
      const quarters = Math.round((glyph.fill ?? 0) * 4);
      return <><rect x="2" y="2" width="12" height="12" fill="#ffffff" stroke={color} strokeWidth="1.2" />
        {[[2, 2], [8, 2], [8, 8], [2, 8]].slice(0, quarters).map(([x, y]) => <rect key={`${x}${y}`} x={x} y={y} width="6" height="6" fill={color} />)}</>;
    }
  }
  return null;
}

/** A conditional-format icon from Power BI's icon name; an unknown name draws nothing. */
export function CfIcon({ name, size = 14 }: { name: string; size?: number }) {
  const clip = useId().replace(/:/g, "");
  const glyph = iconGlyph(name);
  if (!glyph) return null;
  return <svg className="report-cf-icon" width={size} height={size} viewBox="0 0 16 16" role="img" aria-label={name.replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase()}><Glyph glyph={glyph} clip={`cf${clip}`} /></svg>;
}

// --- Buttons and navigators (contract section 16) -----------------------------------------------------

const BUTTON_ICONS: Record<string, typeof ArrowLeft> = {
  leftarrow: ArrowLeft, rightarrow: ArrowRight, back: CircleArrowLeft, reset: RotateCcw, help: CircleQuestionMark,
  information: Info, qna: MessageCircleQuestionMark, bookmarks: Bookmark, bookmark: Bookmark,
};

/** Desktop's button icon name to a glyph (null for `blank` or a name Studio does not know). */
export function buttonIcon(name: string | null | undefined): typeof ArrowLeft | null {
  return name ? BUTTON_ICONS[name.toLowerCase()] ?? null : null;
}

type PointerState = { hovered: boolean; pressed: boolean };

function usePointerState() {
  const [state, setState] = useState<PointerState>({ hovered: false, pressed: false });
  const handlers = {
    onPointerEnter: () => setState(s => ({ ...s, hovered: true })),
    onPointerLeave: () => setState({ hovered: false, pressed: false }),
    onPointerDown: () => setState(s => ({ ...s, pressed: true })),
    onPointerUp: () => setState(s => ({ ...s, pressed: false })),
  };
  return [state, handlers] as const;
}

/** A button drawn from its state tokens: disabled, then pressed ("On press"), then hover, then default. A
 * disabled button stays focusable (aria-disabled) so its reason can be read. */
export function ButtonBody({ visual, label, disabled, reason, onRun }: { visual: RenderVisual; label: string; disabled: boolean; reason?: string; onRun?: () => void }) {
  const [pointer, handlers] = usePointerState();
  const token: ButtonToken | undefined = visual.formatting?.button;
  const state = stateToken(token, buttonStateName({ disabled, pressed: pointer.pressed, hovered: pointer.hovered }));
  const text = state?.text?.show !== false ? state?.text?.text ?? "" : "";
  const Icon = state?.icon?.show !== false ? buttonIcon(state?.icon?.name) : null;
  const iconColor = safeColor(state?.icon?.color) ?? safeColor(state?.text?.color) ?? "#252423";
  const placement = (state?.icon?.placement ?? "left").toLowerCase();
  const vertical = placement === "top" || placement === "bottom";
  const tip = reason ?? visual.action?.tooltip ?? undefined;
  const icon = Icon ? <Icon size={Math.max(12, Math.min(48, state?.icon?.size ?? 18))} color={iconColor} strokeWidth={state?.icon?.width ?? 2} aria-hidden="true" /> : null;
  return <button type="button" className={`report-button${disabled ? " is-disabled" : ""}`} aria-disabled={disabled || undefined} title={tip}
    aria-label={text || label} style={{ ...buttonBoxStyle(token, state), flexDirection: vertical ? "column" : "row" }} {...handlers}
    onClick={() => { if (!disabled) onRun?.(); }}>
    {placement !== "right" && placement !== "bottom" && icon}
    {text && <span style={buttonTextStyle(state)}>{text}</span>}
    {(placement === "right" || placement === "bottom") && icon}
  </button>;
}

function NavigatorButton({ item, token, onRun }: { item: NavigatorItem; token: ButtonToken | undefined; onRun?: (action: Action) => void }) {
  const [pointer, handlers] = usePointerState();
  const state = stateToken(token, navigatorStateName({ pressed: pointer.pressed, hovered: pointer.hovered, selected: !!item.selected }));
  return <button type="button" className="report-button" aria-current={item.selected ? "page" : undefined} style={buttonBoxStyle(token, state)} {...handlers} onClick={() => onRun?.(item.action)}>
    <span style={buttonTextStyle(state)}>{item.label}</span>
  </button>;
}

/** A page or bookmark navigator: one button per item, the current one in its `selected` state. */
export function NavigatorBody({ visual, onRun }: { visual: RenderVisual; onRun?: (action: Action) => void }) {
  const token = visual.formatting?.button;
  const items = visual.formatting?.navigator?.items ?? [];
  const kind = visual.formatting?.navigator?.kind === "bookmarks" ? "Bookmarks" : "Pages";
  if (!items.length) return <span className="original-live-message">No {kind.toLowerCase()} to show</span>;
  return <nav className="report-navigator" aria-label={`${kind}: ${visual.title}`} style={navigatorGridStyle(token, items.length)}>
    {items.map((item, index) => <NavigatorButton key={`${item.action?.target ?? ""}:${index}`} item={item} token={token} onRun={onRun} />)}
  </nav>;
}

// --- Menus and confirmations ----------------------------------------------------------------------------

export type MenuItem = { label: string; run: () => void; disabled?: boolean; hint?: string };

/** The right-click menu of a data point: arrow keys move, Enter runs, Escape or a click elsewhere closes. */
export function PointMenu({ title, items, position, onClose }: { title: string; items: MenuItem[]; position: { left: number; top: number }; onClose: () => void }) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    root.current?.querySelector<HTMLElement>("[role=menuitem]:not([aria-disabled=true])")?.focus();
    const away = (event: PointerEvent) => { if (event.target instanceof Node && !root.current?.contains(event.target)) onClose(); };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [onClose]);
  const move = (event: ReactKeyboardEvent) => {
    const all = [...(root.current?.querySelectorAll<HTMLElement>("[role=menuitem]") ?? [])];
    const at = all.indexOf(document.activeElement as HTMLElement);
    if (event.key === "Escape" || event.key === "Tab") { event.preventDefault(); onClose(); return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const next = all[(at + (event.key === "ArrowDown" ? 1 : all.length - 1) + all.length) % all.length];
      next?.focus();
    }
  };
  return <div ref={root} className="report-point-menu" role="menu" aria-label={title} tabIndex={-1} style={position} onKeyDown={move}>
    <div className="report-point-menu-title" aria-hidden="true">{title}</div>
    {items.map(item => <button type="button" role="menuitem" key={item.label} aria-disabled={item.disabled || undefined} title={item.hint}
      onClick={() => { if (item.disabled) return; onClose(); item.run(); }}>{item.label}</button>)}
  </div>;
}

/** Studio never opens a web address a report button points at without asking first. */
export function UrlConfirm({ url, onOpen, onCancel }: { url: string; onOpen: () => void; onCancel: () => void }) {
  const cancel = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  useEffect(() => {
    cancel.current?.focus();
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); onCancel(); } };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [onCancel]);
  return <div className="report-confirm-backdrop">
    <div className="report-confirm" role="alertdialog" aria-modal="true" aria-labelledby={titleId}>
      <strong id={titleId}>Open this web address?</strong>
      <p>This report button links to a page outside Studio. It opens in your browser.</p>
      <code>{url}</code>
      <div className="report-confirm-actions">
        <button type="button" className="secondary" ref={cancel} onClick={onCancel}>Cancel</button>
        <button type="button" className="primary" onClick={onOpen}>Open in browser</button>
      </div>
    </div>
  </div>;
}
