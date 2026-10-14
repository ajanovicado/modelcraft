import { useEffect, useState } from "react";
import { LoaderCircle, MonitorUp } from "lucide-react";
import { DESKTOP_WARNING, STAGES, stageLabel } from "./importedEditing";

/**
 * The staged strip shown while a change runs (P33). A change that needs Power BI Desktop closes,
 * writes, opens and saves it, which takes up to a minute with windows appearing and disappearing,
 * so the strip names the step and warns the user off Desktop until Studio is done. Editing is
 * locked by the caller while this shows.
 */
export function EditProgress({ label, stage, desktop, className = "" }: { label: string; stage?: string; desktop: boolean; className?: string }) {
  const current = stage ? STAGES.indexOf(stage as (typeof STAGES)[number]) : -1;
  // Only the steps that involve Desktop are worth a step list; a quick report edit is one line. Until the engine reports a
  // stage (the poll route of a plain request sends none) a list with every step "to do" would say nothing true, so it stays hidden.
  const steps = desktop && current >= 0 ? STAGES.filter(item => item !== "planning") : [];
  return <div className={`ie-progress ${className}`.trim()} role="status" aria-live="polite" aria-atomic="true">
    <LoaderCircle className="spin" size={16} aria-hidden="true" />
    <div className="ie-progress-body">
      <strong>{stageLabel(stage, label)}…</strong>
      {desktop && <span className="ie-progress-warning"><MonitorUp size={13} aria-hidden="true" />{DESKTOP_WARNING}</span>}
      {steps.length > 0 && <ol className="ie-progress-steps" aria-label="Steps">{steps.map(item => {
        const index = STAGES.indexOf(item);
        const state = current < 0 ? "todo" : index < current ? "done" : index === current ? "now" : "todo";
        return <li key={item} className={state} aria-current={state === "now" ? "step" : undefined}>{stageLabel(item)}</li>;
      })}</ol>}
    </div>
  </div>;
}

/** A flag that turns on only after `ms`, so a change that finishes at once never flashes a progress strip. */
export function useDelayedFlag(active: boolean, ms: number): boolean {
  const [on, setOn] = useState(false);
  useEffect(() => {
    if (!active) { setOn(false); return; }
    const timer = window.setTimeout(() => setOn(true), ms);
    return () => window.clearTimeout(timer);
  }, [active, ms]);
  return on && active;
}
