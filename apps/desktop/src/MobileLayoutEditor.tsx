import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { LayoutGrid, Plus, Smartphone, Trash2 } from "lucide-react";
import { EditorDialog } from "./EditorDialog";
import type { Operation } from "./importedEditing";
import type { Rect } from "./importedGeometry";
import { nextPhoneY, PHONE_WIDTH, phoneBox, phoneSize, placeOnMobileOp, type LayerVisual, type PaneMeta } from "./importedPanes";

type Drag = { id: string; mode: "move" | "resize"; start: Rect; origin: { x: number; y: number }; last?: Rect };

/**
 * The phone layout of a page (Desktop's Mobile layout view): a 320 px phone canvas where the page's visuals are dropped,
 * moved and resized, a list of the visuals not on it yet, and an automatic arrangement. Each drop or move is one edit.
 */
export function MobileLayoutEditor({ pageId, pageTitle, visuals, mobile, disabled, onEdit, onClose }: {
  pageId: string; pageTitle: string; visuals: Record<string, LayerVisual>; mobile: PaneMeta["mobile"]; disabled: boolean;
  onEdit: (operations: Operation[], label: string) => Promise<boolean>; onClose: () => void;
}) {
  const [override, setOverride] = useState<Record<string, Rect>>({});
  const [columns, setColumns] = useState(2);
  const drag = useRef<Drag | undefined>(undefined);
  const phone = useRef<HTMLDivElement>(null);
  const idle = useRef<number | undefined>(undefined);
  useEffect(() => { setOverride({}); }, [mobile]);
  useEffect(() => () => window.clearTimeout(idle.current), []);

  const placed = Object.entries(mobile ?? {}).filter(([id]) => visuals[id]).map(([id, box]) => ({ id, box: override[id] ?? box, visual: visuals[id] }));
  const candidates = Object.values(visuals).filter(visual => !mobile?.[visual.id] && visual.kind !== "group" && !visual.parentGroup);
  const height = Math.max(560, ...placed.map(item => item.box.y + item.box.height + 40));
  const toPhone = (clientX: number, clientY: number) => { const rect = phone.current?.getBoundingClientRect(); return { x: clientX - (rect?.left ?? 0), y: clientY - (rect?.top ?? 0) + (phone.current?.scrollTop ?? 0) }; };
  const send = (id: string, box: Rect) => { void onEdit([placeOnMobileOp(id, box)], "Placing the visual on the phone layout"); };

  const start = (event: ReactPointerEvent, id: string, box: Rect, mode: Drag["mode"]) => {
    if (disabled || event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    drag.current = { id, mode, start: box, origin: toPhone(event.clientX, event.clientY) };
    const move = (next: PointerEvent) => {
      const current = drag.current;
      if (!current) return;
      const point = toPhone(next.clientX, next.clientY);
      const dx = point.x - current.origin.x, dy = point.y - current.origin.y;
      const rect = current.mode === "move"
        ? phoneBox({ x: current.start.x + dx, y: current.start.y + dy }, current.start)
        : phoneBox(current.start, { width: current.start.width + dx, height: current.start.height + dy });
      current.last = rect;
      setOverride(existing => ({ ...existing, [current.id]: rect }));
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      const current = drag.current;
      drag.current = undefined;
      const rect = current?.last;
      if (current && rect && (rect.x !== current.start.x || rect.y !== current.start.y || rect.width !== current.start.width || rect.height !== current.start.height)) send(current.id, rect);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  // Arrow keys move a placed visual 10 px (Shift: resize); the change is sent once the keys rest.
  const nudge = (event: ReactKeyboardEvent, id: string, box: Rect) => {
    const step = { ArrowLeft: [-10, 0], ArrowRight: [10, 0], ArrowUp: [0, -10], ArrowDown: [0, 10] }[event.key as "ArrowLeft"];
    if (event.key === "Delete") { event.preventDefault(); void onEdit([{ op: "remove_from_mobile", visuals: [id] }], "Taking the visual off the phone layout"); return; }
    if (!step || disabled) return;
    event.preventDefault();
    const next = event.shiftKey ? phoneBox(box, { width: box.width + step[0], height: box.height + step[1] }) : phoneBox({ x: box.x + step[0], y: box.y + step[1] }, box);
    setOverride(existing => ({ ...existing, [id]: next }));
    window.clearTimeout(idle.current);
    idle.current = window.setTimeout(() => send(id, next), 1200);
  };

  const addBelow = (visual: LayerVisual) => {
    const size = phoneSize(visual.position);
    send(visual.id, phoneBox({ x: 0, y: nextPhoneY(mobile) }, size));
  };

  return <EditorDialog title={`Phone layout of “${pageTitle}”`} wide onCancel={onClose}
    footer={<button type="button" className="secondary" data-autofocus onClick={onClose}>Done</button>}>
    <div className="ie-mobile">
      <div className="ie-mobile-side">
        <p className="ie-note">Drag a visual onto the phone, or use Add. On the phone, drag to move, drag the corner to resize; with the keyboard, arrows move, Shift+arrows resize, Delete takes it off.</p>
        <div className="ie-pane-actions">
          <label className="ie-inline">Columns<select value={columns} onChange={event => setColumns(Number(event.target.value))}><option value={1}>1</option><option value={2}>2</option><option value={3}>3</option></select></label>
          <button type="button" className="secondary" disabled={disabled} title="Lay the page's visuals out again from scratch, in reading order" onClick={() => void onEdit([{ op: "arrange_mobile_layout", page: pageId, columns }], "Arranging the phone layout")}><LayoutGrid size={13} />Arrange automatically</button>
        </div>
        <strong>Not on the phone ({candidates.length})</strong>
        <ul className="ie-mobile-list">{candidates.map(visual => <li key={visual.id} draggable={!disabled}
          onDragStart={event => { event.dataTransfer.effectAllowed = "copy"; event.dataTransfer.setData("text/plain", visual.id); }}>
          <span>{visual.title}<small>{visual.kind}</small></span>
          <button type="button" className="icon-button" disabled={disabled} aria-label={`Add ${visual.title} to the phone layout`} title="Add below the others" onClick={() => addBelow(visual)}><Plus size={13} /></button>
        </li>)}</ul>
      </div>
      <div className="ie-phone-frame" aria-label="Phone canvas">
        <Smartphone size={14} aria-hidden="true" />
        <div ref={phone} className="ie-phone" style={{ width: PHONE_WIDTH, height }}
          onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }}
          onDrop={event => {
            event.preventDefault();
            const id = event.dataTransfer.getData("text/plain");
            const visual = visuals[id];
            if (!visual || disabled) return;
            const size = phoneSize(visual.position);
            const point = toPhone(event.clientX, event.clientY);
            send(id, phoneBox({ x: point.x - size.width / 2, y: point.y - 10 }, size));
          }}>
          {!placed.length && <p className="ie-phone-empty">Nothing on the phone layout yet. Power BI shows the desktop page on phones until you place visuals here.</p>}
          {placed.map(({ id, box, visual }) => <div key={id} role="button" tabIndex={0} className="ie-phone-box" aria-label={`${visual.title} on the phone at ${Math.round(box.x)}, ${Math.round(box.y)}, ${Math.round(box.width)} by ${Math.round(box.height)}`}
            style={{ left: box.x, top: box.y, width: box.width, height: box.height, zIndex: "z" in box && typeof box.z === "number" ? box.z : undefined }}
            onPointerDown={event => start(event, id, box, "move")} onKeyDown={event => nudge(event, id, box)}>
            <span>{visual.title}</span>
            <button type="button" className="icon-button" disabled={disabled} aria-label={`Take ${visual.title} off the phone layout`} onPointerDown={event => event.stopPropagation()} onClick={() => void onEdit([{ op: "remove_from_mobile", visuals: [id] }], "Taking the visual off the phone layout")}><Trash2 size={12} /></button>
            <span className="ie-phone-resize" aria-hidden="true" onPointerDown={event => start(event, id, box, "resize")} />
          </div>)}
        </div>
      </div>
    </div>
  </EditorDialog>;
}
