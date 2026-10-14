import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { ChevronRight } from "lucide-react";
import type { MenuItem } from "./importedPanes";

/**
 * The canvas context menu (right-click, Shift+F10 or the Menu key): a menu of the selection's actions with one level of
 * submenus. Arrow keys move, Right/Enter opens a submenu, Left/Escape closes, focus returns to where it was.
 */
export function CanvasContextMenu({ x, y, items, onChoose, onClose, label = "Visual actions", trigger }: {
  x: number; y: number; items: MenuItem[]; onChoose: (id: string) => void; onClose: () => void; label?: string;
  /** The button that opened the menu: a press on it toggles the menu itself, so it is not a click "away". */
  trigger?: HTMLElement | null;
}) {
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState<string>();
  const close = useRef(onClose);
  close.current = onClose;
  const triggerRef = useRef(trigger);
  triggerRef.current = trigger;
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    root.current?.querySelector<HTMLElement>("[role=menuitem]:not([aria-disabled=true])")?.focus();
    const away = (event: PointerEvent) => { if (event.target instanceof Node && !root.current?.contains(event.target) && !triggerRef.current?.contains(event.target)) close.current(); };
    const blur = () => close.current();
    document.addEventListener("pointerdown", away, true);
    window.addEventListener("blur", blur);
    window.addEventListener("resize", blur);
    return () => { document.removeEventListener("pointerdown", away, true); window.removeEventListener("blur", blur); window.removeEventListener("resize", blur); previous?.focus(); };
  }, []);

  const focusIn = (container: Element | null, step: 1 | -1 | "first") => {
    if (!container) return;
    const entries = [...container.querySelectorAll<HTMLElement>(":scope > [role=menuitem], :scope > li > [role=menuitem]")];
    if (!entries.length) return;
    const at = entries.indexOf(document.activeElement as HTMLElement);
    const next = step === "first" ? 0 : (at + step + entries.length) % entries.length;
    entries[next].focus();
  };

  const choose = (item: MenuItem) => {
    if (item.disabled || item.separator) return;
    if (item.children) { setOpen(item.id); requestAnimationFrame(() => focusIn(root.current?.querySelector(`[data-submenu="${item.id}"]`) ?? null, "first")); return; }
    onChoose(item.id);
  };

  const keys = (event: ReactKeyboardEvent, item: MenuItem, parent?: string) => {
    const list = (event.currentTarget as HTMLElement).closest("[role=menu]");
    if (event.key === "ArrowDown") { event.preventDefault(); focusIn(list, 1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); focusIn(list, -1); }
    else if (event.key === "Home") { event.preventDefault(); focusIn(list, "first"); }
    else if ((event.key === "ArrowRight" || event.key === "Enter" || event.key === " ") && item.children) { event.preventDefault(); choose(item); }
    else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(item); }
    else if ((event.key === "ArrowLeft" || event.key === "Escape") && parent) { event.preventDefault(); event.stopPropagation(); setOpen(undefined); root.current?.querySelector<HTMLElement>(`[data-item="${parent}"]`)?.focus(); }
    else if (event.key === "Escape" || event.key === "Tab") { event.preventDefault(); event.stopPropagation(); onClose(); }
  };

  const render = (list: MenuItem[], parent?: string) => list.map(item => item.separator
    ? <li key={item.id} role="separator" className="ie-menu-separator" />
    : <li key={item.id} role="none">
      <div role="menuitem" tabIndex={-1} data-item={item.id} aria-disabled={item.disabled || undefined} aria-haspopup={item.children ? "menu" : undefined} aria-expanded={item.children ? open === item.id : undefined}
        className={`ie-menu-item${item.danger ? " danger" : ""}`} title={item.hint}
        onClick={() => choose(item)} onKeyDown={event => keys(event, item, parent)} onMouseEnter={() => { if (!parent) setOpen(item.children ? item.id : undefined); }}>
        <span>{item.label}</span>{item.children && <ChevronRight size={12} aria-hidden="true" />}
      </div>
      {item.children && open === item.id && <ul role="menu" aria-label={item.label} className="ie-menu ie-submenu" data-submenu={item.id}>{render(item.children, item.id)}</ul>}
    </li>);

  // Keep the menu inside the window.
  const left = Math.max(4, Math.min(x, window.innerWidth - 240));
  const top = Math.max(4, Math.min(y, window.innerHeight - Math.min(420, items.length * 30 + 12)));
  return createPortal(<div ref={root} className="ie-menu-root" style={{ left, top }}>
    <ul role="menu" aria-label={label} className="ie-menu">{render(items)}</ul>
  </div>, document.body);
}
