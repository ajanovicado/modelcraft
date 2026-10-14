import { useRef, useState } from "react";
import { ChevronDown, SquarePlus } from "lucide-react";
import { CanvasContextMenu } from "./CanvasContextMenu";
import { BUTTON_KINDS, SHAPES, type InsertChoice, type MenuItem } from "./importedPanes";

const ITEMS: MenuItem[] = [
  { id: "buttons", label: "Button", children: BUTTON_KINDS.map(kind => ({ id: `button:${kind.value}`, label: kind.label })) },
  { id: "navigators", label: "Navigator", children: [{ id: "navigator:page", label: "Page navigator" }, { id: "navigator:bookmark", label: "Bookmark navigator" }] },
  { id: "shapes", label: "Shape", children: SHAPES.map(shape => ({ id: `shape:${shape.value}`, label: shape.label })) },
  { id: "textbox", label: "Text box" },
];

/** What an Insert menu choice id stands for. */
export function insertChoice(id: string): InsertChoice | undefined {
  const [kind, value] = id.split(":");
  if (kind === "button" && value) return { kind: "button", button: value };
  if (kind === "navigator" && (value === "page" || value === "bookmark")) return { kind: "navigator", navigator: value };
  if (kind === "shape" && value) return { kind: "shape", shape: value };
  if (kind === "textbox") return { kind: "textbox" };
  return undefined;
}

/** Desktop's Insert ribbon for the elements that are not data visuals: buttons, navigators, shapes and text boxes. */
export function InsertMenu({ disabled, onInsert }: { disabled: boolean; onInsert: (choice: InsertChoice) => void }) {
  const [at, setAt] = useState<{ x: number; y: number }>();
  const button = useRef<HTMLButtonElement>(null);
  return <>
    <button type="button" ref={button} className="secondary" disabled={disabled} aria-haspopup="menu" aria-expanded={Boolean(at)}
      onClick={() => { const box = button.current?.getBoundingClientRect(); setAt(at ? undefined : { x: box?.left ?? 0, y: (box?.bottom ?? 0) + 4 }); }}>
      <SquarePlus size={14} />Insert<ChevronDown size={12} /></button>
    {at && <CanvasContextMenu x={at.x} y={at.y} items={ITEMS} label="Insert" trigger={button.current} onClose={() => setAt(undefined)}
      onChoose={id => { const choice = insertChoice(id); setAt(undefined); if (choice) onInsert(choice); }} />}
  </>;
}
