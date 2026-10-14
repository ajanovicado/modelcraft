import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { Decision, EditProblem, ProblemAction, ProblemUi } from "./importedEditing";

const FOCUSABLE = "button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])";

/**
 * A modal dialog: focus moves into it (to the element marked data-autofocus, the safe default),
 * Tab stays inside, Escape cancels and focus returns to where it was. Listeners are attached with
 * effects because a dialog role is not an element the a11y lint lets handle keys in JSX.
 */
export function EditorDialog({ title, onCancel, children, footer, tone = "info", wide = false }: {
  title: string; onCancel: () => void; children?: ReactNode; footer: ReactNode; tone?: "error" | "warn" | "info"; wide?: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const cancel = useRef(onCancel);
  cancel.current = onCancel;
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const node = panel.current;
    if (!node) return;
    (node.querySelector<HTMLElement>("[data-autofocus]") ?? node.querySelector<HTMLElement>(FOCUSABLE) ?? node).focus();
    const keys = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); cancel.current(); return; }
      if (event.key !== "Tab") return;
      const items = [...node.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (!items.length) { event.preventDefault(); return; }
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    node.addEventListener("keydown", keys);
    return () => { node.removeEventListener("keydown", keys); previous?.focus(); };
  }, []);
  return createPortal(<div className="ie-dialog-backdrop">
    <div className={`ie-dialog ie-dialog-${tone}${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby={titleId} ref={panel} tabIndex={-1}>
      <h2 id={titleId}>{title}</h2>
      <div className="ie-dialog-body">{children}</div>
      <div className="ie-dialog-actions">{footer}</div>
    </div>
  </div>, document.body);
}

const ACTION_LABELS: Record<ProblemAction, string> = {
  redo: "Redo my change", save_and_continue: "Save into history and continue", cancel: "Cancel", retry: "Try again",
  dismiss: "Close", replace: "Continue", keep: "Keep it", wait: "Keep waiting",
};

/** The dialog for whatever an edit request ran into (docs/imported-report-editing-api.md, error shapes). */
export function ProblemDialog({ problem, ui, onDecide }: { problem: EditProblem; ui: ProblemUi; onDecide: (decision: Decision) => void }) {
  const choices = problem.kind === "needs_choice" ? problem.choices : [];
  const label = (action: ProblemAction) => {
    if (action === "replace" && problem.kind === "needs_confirmation") {
      if (problem.confirm === "replace_conditional") return "Replace";
      if (problem.confirm === "delete_page" || problem.confirm === "bookmark_references" || problem.confirm === "page_links") return "Remove and clean up";
      if (problem.confirm === "cleanup_references") return "Ungroup";
      if (problem.confirm === "report_tests_broken") return "Apply anyway";
    }
    return ACTION_LABELS[action];
  };
  const cancelAction: ProblemAction = ui.actions.includes("cancel") ? "cancel" : ui.actions.includes("keep") ? "keep" : ui.actions.includes("dismiss") ? "dismiss" : ui.actions[0];
  return <EditorDialog title={ui.title} tone={ui.tone} onCancel={() => onDecide({ action: cancelAction })}
    footer={<>
      {choices.map((choice, index) => <button type="button" key={choice.value} className={index === 0 ? "primary yellow" : "secondary"} {...(index === 0 ? { "data-autofocus": true } : {})} onClick={() => onDecide({ choice: choice.value })}>{choice.label}</button>)}
      {ui.actions.map((action, index) => <button type="button" key={action} className={action === "save_and_continue" || action === "replace" || action === "redo" ? "primary yellow" : "secondary"} {...(!choices.length && index === 0 ? { "data-autofocus": true } : {})} onClick={() => onDecide({ action })}>{label(action)}</button>)}
    </>}>
    {ui.body && <p className="ie-dialog-text">{ui.body}</p>}
    {ui.steps && <><p className="ie-dialog-text">To do this in Power BI Desktop:</p><ol className="ie-dialog-steps">{ui.steps.map(step => <li key={step}>{step}</li>)}</ol></>}
  </EditorDialog>;
}
