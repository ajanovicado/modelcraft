import { CheckCircle2, CircleAlert, FlaskConical, HelpCircle, ListChecks, Undo2, Wand2 } from "lucide-react";
import { EditProgress } from "./EditProgress";
import { ExplainView } from "./ExplainPanel";
import { desktopStage, describeReceipt, operationFormulas, queueableConfirm, receiptCanUndo, type EditCard } from "./importedEditing";
import { expectText, needsDesktopOp, planFormulas, planView, severityLabel, type ReviewFinding } from "./judgement";
import "./imported-editor.css";

export type EditProgressState = { stage: string; label: string };

/** The words of a confirmation card, by what it asks (docs/imported-report-editing-api.md, `edit_confirm`). */
export function confirmCopy(card: Extract<EditCard, { type: "confirm" }>): { heading: string; apply: string; cancel: string } {
  const kind = card.event.kind;
  if (kind === "model_edit") return { heading: "This needs Power BI. Nothing changes until you apply it.", apply: "Apply to report (needs Power BI, about 1 min)", cancel: "Cancel" };
  if (kind === "plan") {
    // The steps say what needs Desktop; an interim card without steps falls back to its operations.
    const view = planView(card.event.steps ?? [], card.event.ops.some(op => needsDesktopOp(op.op)));
    return { heading: view.heading, apply: view.applyLabel, cancel: "Cancel" };
  }
  if (kind === "report_tests_broken") return { heading: "This change would break report tests.", apply: "Apply anyway", cancel: "Cancel" };
  if (kind === "restore") return { heading: "Go back to an earlier version?", apply: "Restore", cancel: "Cancel" };
  if (kind === "desktop_unsaved") return { heading: "Power BI Desktop has unsaved changes to this report.", apply: "Save them into the history and continue", cancel: "Cancel" };
  if (kind === "replace_conditional") return { heading: "This replaces conditional formatting.", apply: "Replace", cancel: "Keep it" };
  // The edit layer's reference questions: Apply sets `cleanup_references` on the step that asked (the server maps each kind).
  if (kind === "remove_page_references") return { heading: "Buttons or bookmarks point at this page.", apply: "Remove and clean up", cancel: "Cancel" };
  if (kind === "remove_bookmark_references") return { heading: "Buttons or navigators use this bookmark.", apply: "Remove and clean up", cancel: "Cancel" };
  if (kind === "clear_page_links") return { heading: "Buttons or visual tooltips use this page.", apply: "Remove the links and continue", cancel: "Cancel" };
  if (kind === "cleanup_references") return { heading: "Bookmarks remember this group, or it is hidden.", apply: "Ungroup", cancel: "Cancel" };
  return { heading: "Please confirm this change.", apply: "Continue", cancel: "Cancel" };
}

function Findings({ findings, fixed, disabled, onFix }: { findings: ReviewFinding[]; fixed?: string[]; disabled: boolean; onFix?: (finding: ReviewFinding) => void }) {
  if (!findings.length) return null;
  return <div className="ie-findings" aria-label="Studio noticed">
    <strong><ListChecks size={13} aria-hidden="true" /> Studio noticed</strong>
    <ul>{findings.map(finding => <li key={finding.id} className={finding.severity}>
      <span className={`ie-badge ${finding.severity === "problem" ? "problem" : "suggestion"}`}>{severityLabel(finding)}</span>
      <span>{finding.message}{finding.fixSummary && <small> {finding.fixSummary}</small>}</span>
      {finding.fix && onFix && (fixed?.includes(finding.id)
        ? <span className="ie-card-status">Fixed</span>
        : <button type="button" className="secondary" disabled={disabled} title={finding.fixSummary ?? "Apply Studio's fix (one change you can undo)"} onClick={() => onFix(finding)}><Wand2 size={12} />Fix</button>)}
    </li>)}</ul>
  </div>;
}

/**
 * What an edit made from chat shows in the conversation: the staged progress while it runs, then a
 * receipt (what changed, what only Power BI shows, Undo while it is still the newest change, what Studio
 * noticed with one-click fixes), the planner's plan with Apply and Cancel, the question when the target was
 * ambiguous, the confirmation a model change always needs, an explanation of a number, and suggested tests.
 */
export function EditChatCards({ cards, progress, headId, disabled, onUndo, onChoice, onConfirm, onQueue, onFix, onAddSuggestion }: {
  cards: EditCard[]; progress?: EditProgressState; headId?: string; disabled: boolean;
  onUndo: (card: EditCard) => void;
  onChoice: (card: EditCard, index: number) => void;
  onConfirm: (card: EditCard, accept: boolean) => void;
  /** Offered on a measure change: keep it with the other pending changes instead of applying it now. */
  onQueue?: (card: EditCard) => void;
  /** Applies a finding's fix (one edit through the report editor). */
  onFix?: (card: EditCard, finding: ReviewFinding) => void;
  /** Adds a suggested report test. */
  onAddSuggestion?: (card: EditCard, index: number) => void;
}) {
  if (!cards.length && !progress) return null;
  return <div className="ie-chat-cards" aria-label="Report edits">
    {cards.map(card => {
      if (card.type === "applied") {
        const view = describeReceipt({ summaries: card.event.summaries, warnings: card.event.warnings, protected: card.event.protectedItems, visible_in: card.event.visibleIn, outcome: "updated", testLines: card.event.testLines, modelHealth: card.event.modelHealth });
        const undoable = receiptCanUndo(card, headId);
        return <article key={card.id} className={`ie-card ${view.tone === "warn" ? "warn" : ""}`} aria-label="Report change">
          <h4><CheckCircle2 size={14} aria-hidden="true" /> {card.undone ? "Change undone" : view.headline}</h4>
          {view.lines.length > 0 && <ul>{view.lines.map((line, index) => <li key={index}>{line}</li>)}</ul>}
          {view.warnings.map(warning => <p key={warning} className="ie-warning">{warning}</p>)}
          {view.tests.map(line => <p key={line} className="ie-card-tests"><FlaskConical size={12} aria-hidden="true" /> {line}</p>)}
          {view.health.length > 0 && <div className="ie-findings"><strong>Model health</strong><ul>{view.health.map(line => <li key={line}>{line}</li>)}</ul></div>}
          {view.chips.length > 0 && <div className="ie-chips">{view.chips.map(chip => <span key={chip.label} className="ie-chip">{chip.label}</span>)}</div>}
          {!card.undone && <Findings findings={card.event.review ?? []} fixed={card.fixed} disabled={disabled} onFix={onFix ? finding => onFix(card, finding) : undefined} />}
          <div className="ie-card-actions">
            <button type="button" className="secondary" disabled={disabled || !undoable} title={undoable ? "Undo this change" : card.undone ? "This change was already undone" : "A newer change was made after this one. Use History in the report editor to go back further."} onClick={() => onUndo(card)}><Undo2 size={13} />Undo</button>
          </div>
        </article>;
      }
      if (card.type === "plan") {
        const view = planView(card.event.steps, card.event.needsDesktop);
        const formulas = planFormulas(card.event.ops);
        const answered = card.answered;
        return <article key={card.id} className="ie-card question" aria-label="Planned change">
          <h4><HelpCircle size={14} aria-hidden="true" /> {view.heading}</h4>
          <ol className="ie-plan-steps">{card.event.steps.map((step, index) => <li key={index}>
            <span>{step.summary}</span>
            {view.badges[index]?.labels.map(label => <span key={label} className={`ie-badge ${label.startsWith("Outside") ? "outside" : label === "Needs Power BI" ? "desktop" : ""}`}>{label}</span>)}
          </li>)}</ol>
          {formulas.map((formula, index) => <div key={index}><small>{formula.title}</small><pre className="ie-formula" aria-label={`${formula.language}: ${formula.title}`}>{formula.expression}</pre></div>)}
          {card.event.whatIf && <p><strong>What if:</strong> {card.event.whatIf.question}<br />{card.event.whatIf.answer}</p>}
          {card.event.detail && <details><summary>Details</summary><p className="ie-plan-detail">{card.event.detail}</p></details>}
          {card.event.leftOut.length > 0 && <p className="ie-note">Not in this plan: {card.event.leftOut.join("; ")}</p>}
          {answered ? <p className="ie-card-status">{answered === "applied" ? "Applied to your report." : answered === "cancelled" ? "Cancelled. Your report was not changed." : answered}</p>
            : <div className="ie-card-actions"><button type="button" className="primary yellow" disabled={disabled} onClick={() => onConfirm(card, true)}>{view.applyLabel}</button>
              <button type="button" className="secondary" disabled={disabled} onClick={() => onConfirm(card, false)}>Cancel</button></div>}
          {card.reply && <p className="ie-card-reply">{card.reply}</p>}
        </article>;
      }
      if (card.type === "choices") {
        return <article key={card.id} className="ie-card question" aria-label="Question about your change">
          <h4><HelpCircle size={14} aria-hidden="true" /> {card.event.question}</h4>
          {card.answered ? <p className="ie-card-status">You chose: {card.answered}</p>
            : <div className="ie-card-actions" role="group" aria-label="Choices">{card.event.choices.map(choice => <button type="button" key={choice.index} className="secondary" disabled={disabled} onClick={() => onChoice(card, choice.index)}>{choice.label}</button>)}</div>}
          {card.reply && <p className="ie-card-reply">{card.reply}</p>}
        </article>;
      }
      if (card.type === "confirm") {
        const answered = card.answered;
        const copy = confirmCopy(card);
        const steps = card.event.kind === "plan" ? card.event.steps ?? [] : [];
        const view = steps.length ? planView(steps) : undefined;
        return <article key={card.id} className="ie-card warn" aria-label="Change to confirm">
          <h4><HelpCircle size={14} aria-hidden="true" /> {copy.heading}</h4>
          {steps.length > 0 && <ol className="ie-plan-steps">{steps.map((step, index) => <li key={index}><span>{step.summary}</span>{view?.badges[index]?.labels.map(label => <span key={label} className="ie-badge">{label}</span>)}</li>)}</ol>}
          {card.event.whatIf && <p><strong>What if:</strong> {card.event.whatIf.question}<br />{card.event.whatIf.answer}</p>}
          {card.event.detail && <p className="ie-plan-detail">{card.event.detail}</p>}
          {card.event.tests && card.event.tests.length > 0 && <ul>{card.event.tests.map(test => <li key={test.id}>{test.message}</li>)}</ul>}
          {operationFormulas(card.event.ops).map((formula, index) => <div key={index}><small>{formula.title}</small><pre className="ie-formula" aria-label={`DAX: ${formula.title}`}>{formula.expression}</pre></div>)}
          {answered ? <p className="ie-card-status">{answered === "applied" ? "Applied to your report." : answered === "cancelled" ? "Cancelled. Your report was not changed." : answered === "queued" ? "Added to Pending Power BI changes. Apply it there together with your other changes." : answered}</p>
            : <div className="ie-card-actions"><button type="button" className="primary yellow" disabled={disabled} onClick={() => onConfirm(card, true)}>{copy.apply}</button>
              {onQueue && queueableConfirm(card) && <button type="button" className="secondary" disabled={disabled} title="Keep this change with your other pending Power BI changes and apply them together" onClick={() => onQueue(card)}>Add to pending changes</button>}
              <button type="button" className="secondary" disabled={disabled} onClick={() => onConfirm(card, false)}>{copy.cancel}</button></div>}
          {card.reply && <p className="ie-card-reply">{card.reply}</p>}
        </article>;
      }
      if (card.type === "review") {
        return <article key={card.id} className="ie-card question" aria-label="What Studio noticed">
          <Findings findings={card.event.findings} fixed={card.fixed} disabled={disabled} onFix={onFix ? finding => onFix(card, finding) : undefined} />
        </article>;
      }
      if (card.type === "explanation") {
        return <article key={card.id} className="ie-card question" aria-label="Explanation of a number"><ExplainView explanation={card.event.explanation} compact /></article>;
      }
      if (card.type === "suggestions") {
        return <article key={card.id} className="ie-card question" aria-label="Suggested report tests">
          <h4><FlaskConical size={14} aria-hidden="true" /> Tests worth pinning</h4>
          <ul className="ie-suggestions">{card.event.suggestions.tests.map((item, index) => <li key={item.test.id}>
            <span>{item.text}<small> {item.test.name} {expectText(item.test.expect)}</small></span>
            {card.added?.includes(item.test.id) ? <span className="ie-card-status">Added</span>
              : onAddSuggestion && <button type="button" className="secondary" disabled={disabled} onClick={() => onAddSuggestion(card, index)}>Add</button>}
          </li>)}</ul>
        </article>;
      }
      return <article key={card.id} className="ie-card error" role="alert" aria-label="Change not applied"><h4><CircleAlert size={14} aria-hidden="true" /> That change was not applied</h4><p>{card.event.message}</p></article>;
    })}
    {progress && <EditProgress className="ie-chat-progress" label={progress.label} stage={progress.stage} desktop={desktopStage(progress.stage)} />}
  </div>;
}
