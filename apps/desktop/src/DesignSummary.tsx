import { LayoutTemplate, Palette } from "lucide-react";
import type { Draft } from "./types";

const SOURCE_LABELS: Record<string, string> = {
  local_model: "Chosen by Local AI",
  prompt_rule: "From your request",
  user: "Your choice",
  default: "Default",
};

function readable(id: string): string {
  const text = id.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** The layout and existing theme of the draft, shown for review. */
export function DesignSummary({ draft }: { draft: Draft }) {
  const provenance = draft.planning_provenance;
  const current = provenance?.report_theme ?? "default";
  const layout = provenance?.layout_template ?? "classic_overview";
  return <section className="design-summary" aria-labelledby="design-summary-title">
    <header>
      <h3 id="design-summary-title">Design</h3>
      <span className="design-layout"><LayoutTemplate size={14} aria-hidden="true" />{readable(layout)} layout
        <small>{SOURCE_LABELS[provenance?.layout_template_source ?? "default"]}</small></span>
    </header>
    <div className="design-theme-heading"><Palette size={14} aria-hidden="true" /><span>{readable(current)} theme</span>
      <small>{SOURCE_LABELS[provenance?.report_theme_source ?? "default"]}</small></div>
  </section>;
}
