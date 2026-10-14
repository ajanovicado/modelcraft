import { useEffect, useRef, type ReactNode } from "react";
import { BarChart3, CalendarRange, ChevronDown, Gauge, Grid2X2, LayoutList, LineChart, Plus, Table2 } from "lucide-react";
import type { Visual } from "./types";
import { reportVisualCatalog } from "./reportVisualCatalog";

function CommandMenu({ label, icon, children, disabled }: { label: string; icon?: ReactNode; children: ReactNode; disabled?: boolean }) {
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const close = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) ref.current?.removeAttribute("open"); };
    const node = ref.current;
    const dismiss = () => { node?.removeAttribute("open"); node?.querySelector("summary")?.focus(); };
    const choose = (event: MouseEvent) => { if ((event.target as HTMLElement).closest("button:not(:disabled)")) dismiss(); };
    const keys = (event: KeyboardEvent) => { if (event.key === "Escape") { event.stopPropagation(); dismiss(); } };
    document.addEventListener("pointerdown", close);
    node?.addEventListener("click", choose);
    node?.addEventListener("keydown", keys);
    return () => { document.removeEventListener("pointerdown", close); node?.removeEventListener("click", choose); node?.removeEventListener("keydown", keys); };
  }, []);
  return <details ref={ref} className="report-command-menu" onToggle={event => {
    if (event.currentTarget.open) event.currentTarget.querySelector(".report-command-options")?.scrollTo({ top: 0 });
  }}>
    <summary aria-disabled={disabled} onClick={event => { if (disabled) event.preventDefault(); }}>{icon}{label}<ChevronDown size={13}/></summary>
    <div className="report-command-options" role="group" aria-label={label}>{children}</div>
  </details>;
}

export function ReportToolbar({ disabled, atLimit, onAdd, onArrange, showVisuals, onVisuals, showGrid, onGrid, onDateFilter }: {
  onDateFilter: () => void; disabled: boolean; atLimit: boolean; onAdd: (recipe: Visual["recipe"], visualType?: string) => void;
  onArrange: (compact: boolean) => void; showVisuals: boolean; onVisuals: () => void; showGrid: boolean; onGrid: () => void;
}) {
  return <div className="report-canvas-toolbar" role="group" aria-label="Report canvas commands">
    <CommandMenu label="Add visual" icon={<Plus size={15}/>} disabled={disabled || atLimit}>
      {reportVisualCatalog.map(visual => {
        const Icon = { kpi: Gauge, trend: LineChart, category: BarChart3, detail: Table2 }[visual.recipe];
        return <button key={visual.id} disabled={disabled || atLimit} onClick={() => onAdd(visual.recipe, visual.id)} title={visual.preview === "aggregate_table" ? "Shows aggregate data locally; final chart renders in Power BI" : undefined}><Icon size={15}/>{visual.label}</button>;
      })}
    </CommandMenu>
    <span className="report-command-divider"/>
    <CommandMenu label="Arrange" disabled={disabled}>
      <button disabled={disabled} onClick={() => onArrange(false)}>Arrange visuals</button>
      <button disabled={disabled} onClick={() => onArrange(true)}>Compact layout</button>
    </CommandMenu>
    <span className="report-command-divider"/>
    <button className="secondary" disabled={disabled} onClick={onDateFilter}><CalendarRange size={15}/>Date filter</button>
    <button className="secondary" aria-pressed={showVisuals} onClick={onVisuals}><LayoutList size={15}/>Visuals</button>
    <button className="secondary icon-only" title="Show canvas grid" aria-label="Show canvas grid" aria-pressed={showGrid} onClick={onGrid}><Grid2X2 size={15}/></button>
  </div>;
}
