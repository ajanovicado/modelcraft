import { useLayoutEffect, useRef, useState } from "react";
import { BarChart3, ChevronLeft, ChevronRight, Table2, X } from "lucide-react";
import { DesignerVisualPreview, samePreviewBinding } from "./DesignerVisualPreview";
import type { Draft, Measure, Visual } from "./types";

export function ReportVisualDialog({ visual, saved, preview, measure, category, initialView, onClose }: {
  visual: Visual; saved?: Visual; preview?: Draft["previews"][number]; measure?: Measure; category: string;
  initialView: "visual" | "table"; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [view, setView] = useState(initialView);
  const [offset, setOffset] = useState(0);
  const rows = samePreviewBinding(visual, saved) ? preview?.rows.length ?? 0 : 0;
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const node = dialog.current;
    node?.showModal();
    return () => { node?.close(); if (previous?.isConnected) previous.focus(); };
  }, []);
  return <dialog ref={dialog} className="report-focus-dialog" aria-labelledby="report-focus-title" onCancel={onClose}>
    <header><div><h2 id="report-focus-title">{visual.title || "Untitled visual"}</h2><p>Local snapshot preview</p></div>
      <div className="report-focus-actions" role="group" aria-label="Visual view">
        <button className="secondary" aria-pressed={view === "visual"} onClick={() => setView("visual")}><BarChart3 size={15}/>Visual</button>
        <button className="secondary" aria-pressed={view === "table"} onClick={() => setView("table")}><Table2 size={15}/>Show data</button>
      </div><button className="secondary icon-only" aria-label="Close visual view" onClick={onClose}><X size={18}/></button>
    </header>
    <div className="report-focus-content"><DesignerVisualPreview visual={visual} saved={saved} preview={preview} measure={measure} category={category} view={view} rowOffset={offset} rowLimit={50}/></div>
    <footer><span>{rows ? `${rows.toLocaleString()} rows in the saved preview · This is a bounded snapshot result.` : "Save and revalidate bindings to refresh preview data."}</span>
      {view === "table" && rows > 50 && <div><button className="secondary" aria-label="Previous data rows" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 50))}><ChevronLeft size={15}/></button><span>{offset + 1}–{Math.min(offset + 50, rows)}</span><button className="secondary" aria-label="Next data rows" disabled={offset + 50 >= rows} onClick={() => setOffset(offset + 50)}><ChevronRight size={15}/></button></div>}
    </footer>
  </dialog>;
}
