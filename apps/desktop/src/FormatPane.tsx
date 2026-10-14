import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { fieldColumns, isAuto, sectionsFor, shownInPowerBiOnly, type ControlSpec, type FormatCatalog, type VisualInfo } from "./importedFormat";
import type { Operation, ThemePalette } from "./importedEditing";
import { analyticsKinds, cfFamily, hasNumbers } from "./formatExtras";
import type { LayerVisual } from "./importedPanes";
import { ActionEditor, AltTextEditor, AnalyticsEditor, ConditionalFormatEditor, TooltipPageEditor } from "./FormatSections";

/** A number that is sent when the user finishes typing (Enter or leaving the box), not on every key. */
function NumberField({ label, value, min, max, step, unit, placeholder, disabled, onCommit }: {
  label: string; value: number | undefined; min?: number; max?: number; step?: number; unit?: string; placeholder?: string; disabled: boolean; onCommit: (value: number) => void;
}) {
  const [draft, setDraft] = useState(value === undefined ? "" : String(value));
  // Enter sends the value, and leaving the box right after (a dialog takes focus) must not send
  // it again: the prop only changes once the edit is applied.
  const sent = useRef<number | undefined>(undefined);
  useEffect(() => { setDraft(value === undefined ? "" : String(value)); sent.current = undefined; }, [value]);
  const commit = () => {
    if (draft.trim() === "") return;
    const parsed = Number(draft);
    if (!Number.isFinite(parsed)) { setDraft(value === undefined ? "" : String(value)); return; }
    const bounded = Math.max(min ?? -Infinity, Math.min(max ?? Infinity, parsed));
    setDraft(String(bounded));
    if (bounded !== value && bounded !== sent.current) { sent.current = bounded; onCommit(bounded); }
  };
  return <span className="ie-number">
    <input type="number" aria-label={label} value={draft} min={min} max={max} step={step} placeholder={placeholder ?? "Auto (theme)"} disabled={disabled}
      onChange={event => setDraft(event.target.value)} onBlur={commit} onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); commit(); } }} />
    {unit && <small>{unit}</small>}
  </span>;
}

function TextField({ label, value, placeholder, disabled, onCommit }: {
  label: string; value: string | undefined; placeholder?: string; disabled: boolean; onCommit: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value ?? "");
  const sent = useRef<string | undefined>(undefined);
  useEffect(() => { setDraft(value ?? ""); sent.current = undefined; }, [value]);
  const commit = () => {
    const next = draft.trim();
    if (next && next !== (value ?? "") && next !== sent.current) { sent.current = next; onCommit(next); }
  };
  return <input type="text" aria-label={label} value={draft} placeholder={placeholder ?? "Auto (theme)"} disabled={disabled} spellCheck={false}
    onChange={event => setDraft(event.target.value)} onBlur={commit} onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); commit(); } }} />;
}

function Row({ label, tags, children }: { label: string; tags?: { auto: boolean; powerBi: boolean }; children: ReactNode }) {
  return <div className="ie-row">
    <span className="ie-row-label">{label}{tags?.powerBi && <em className="ie-tag" title="Studio's canvas does not draw this setting, but Power BI does">Shown in Power BI</em>}</span>
    <div className="ie-row-control">{children}</div>
    {tags?.auto && <small className="ie-auto">Auto (theme)</small>}
  </div>;
}

function Control({ spec, info, disabled: locked, send, field }: {
  spec: ControlSpec; info: VisualInfo; disabled: boolean; send: (op: Operation | undefined, label: string) => void;
  /** The column a field-scoped control applies to (the section's picker). */
  field?: string;
}) {
  const current = spec.read?.(info, field);
  const tags = { auto: isAuto(current, spec.themeDefault), powerBi: shownInPowerBiOnly(spec, info) };
  const apply = (value: unknown) => send(spec.write?.(info, value, field), field ? `${spec.label} of ${field}` : spec.label);
  // A control that needs a column waits for the section's column picker.
  const disabled = locked || Boolean(spec.fieldScoped && !field);
  if (spec.input === "toggle") {
    return <Row label={spec.label} tags={tags}><label className="ie-switch"><input type="checkbox" role="switch" aria-label={spec.label} checked={current === true} disabled={disabled} onChange={event => apply(event.target.checked)} /><span aria-hidden="true" /></label></Row>;
  }
  if (spec.input === "number") {
    return <Row label={spec.label} tags={tags}><NumberField label={spec.label} value={typeof current === "number" && !tags.auto ? current : undefined} min={spec.min} max={spec.max} step={spec.step} unit={spec.unit} placeholder={spec.placeholder} disabled={disabled} onCommit={apply} /></Row>;
  }
  if (spec.input === "select") {
    return <Row label={spec.label} tags={tags}><select aria-label={spec.label} value={tags.auto ? "" : typeof current === "string" ? current : typeof current === "number" ? String(current) : ""} disabled={disabled} onChange={event => { if (event.target.value) apply(event.target.value); }}>
      {(tags.auto || (typeof current !== "string" && typeof current !== "number")) && <option value="">{spec.placeholder ?? "Auto (theme)"}</option>}
      {spec.options?.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select></Row>;
  }
  return <Row label={spec.label} tags={tags}><TextField label={spec.label} value={typeof current === "string" && !tags.auto ? current : undefined} placeholder={spec.placeholder} disabled={disabled} onCommit={apply} /></Row>;
}

/** What the pane needs beyond the catalog for its round-3 sections (conditional formatting, analytics, actions, tooltips). */
export type FormatContext = {
  /** The visual's render row (analytics lines, tooltip page). */
  row?: unknown;
  measures?: { table: string; name: string }[];
  pages?: { id: string; title: string }[];
  /** The report's tooltip pages (`pages[].page_type` "tooltip"). */
  tooltipPages?: { id: string; title: string }[];
  /** The visual's alt text now (`pages[].visuals[].alt_text`). */
  altText?: string;
  bookmarks?: { name: string; displayName: string }[];
  drillthroughs?: { page: string; title: string }[];
  layer?: LayerVisual;
};

// Buttons, shapes and pictures take an action; a navigator's buttons are its pages or bookmarks.
const ACTION_KINDS = new Set(["button", "shape", "image"]);

/**
 * The Visual pane: the conditional formatting editor first (open), then number formats, the structure of a
 * table or matrix (totals, subtotals, layout, values on rows, column names and order), size and position, analytics
 * lines, actions, tooltips and alt text. Studio does not change styling (colors, fonts, titles, borders): the pane
 * offers none of it. Controls show the value the report has now ("Auto (theme)" while the author set nothing) and
 * send one typed edit per change. Editing is locked while a change is being applied.
 */
export function FormatPane({ info, catalog, catalogError, theme, disabled, onEdit, context = {} }: {
  info: VisualInfo | undefined; catalog?: FormatCatalog; catalogError?: string; theme?: ThemePalette; disabled: boolean;
  /** Resolves true when the change was applied. */
  onEdit: (op: Operation, label: string) => void | Promise<boolean>;
  context?: FormatContext;
}) {
  const [columnOf, setColumnOf] = useState<Record<string, string>>({});
  const sections = useMemo(() => info ? sectionsFor(info, catalog) : [], [info, catalog]);
  const columns = useMemo(() => info ? fieldColumns(info) : [], [info]);
  useEffect(() => { setColumnOf({}); }, [info?.id]);
  if (!info) return <p className="ie-empty">Select a visual on the page to see its conditional formatting and settings.</p>;
  if (info.kind === "group") return <p className="ie-empty">This is a group. Select one of the visuals inside it.</p>;
  const send = (op: Operation | undefined, label: string) => op ? onEdit(op, label) : undefined;
  const edit = (op: Operation, label: string) => onEdit(op, label);
  const family = cfFamily(info);
  const kinds = analyticsKinds(info);
  const data = hasNumbers(info.kind);
  // The first two sections start open: conditional formatting, when the visual has it, is one of them.
  const opened = family ? 1 : 2;
  return <div className="ie-format" aria-busy={disabled}>
    {family && <details className="ie-section" open><summary>Conditional formatting</summary>
      <div className="ie-section-body"><ConditionalFormatEditor key={info.id} info={info} theme={theme} measures={context.measures ?? []} disabled={disabled} onEdit={edit} /></div></details>}
    {sections.map((section, index) => {
      const scoped = section.controls.some(spec => spec.fieldScoped);
      const field = scoped ? columnOf[section.id] ?? columns[0]?.value : undefined;
      return <details key={section.id} open={index < opened} className="ie-section">
        <summary>{section.title}</summary>
        <div className="ie-section-body">
          {scoped && (columns.length
            ? <label className="ie-inline ie-column-picker">Column<select value={field ?? ""} disabled={disabled} onChange={event => setColumnOf(current => ({ ...current, [section.id]: event.target.value }))}>{columns.map(column => <option key={column.value} value={column.value}>{column.label}</option>)}</select></label>
            : <p className="ie-note">This visual has no columns yet.</p>)}
          {section.controls.map(spec => <Control key={spec.id} spec={spec} info={info} disabled={disabled} send={send} field={spec.fieldScoped ? field : undefined} />)}
        </div>
      </details>;
    })}
    {catalogError && <p className="ie-note" role="status">{catalogError}</p>}
    {kinds.length > 0 && <details className="ie-section"><summary>Analytics</summary>
      <div className="ie-section-body"><AnalyticsEditor key={info.id} info={info} row={context.row} theme={theme} disabled={disabled} onEdit={edit} /></div></details>}
    {ACTION_KINDS.has(info.kind) && context.pages && <details className="ie-section"><summary>Action</summary>
      <div className="ie-section-body"><ActionEditor info={info} layer={context.layer} pages={context.pages} bookmarks={context.bookmarks ?? []} drillthroughs={context.drillthroughs ?? []} disabled={disabled} onEdit={edit} /></div></details>}
    {data && context.tooltipPages && <details className="ie-section"><summary>Tooltip</summary>
      <div className="ie-section-body"><TooltipPageEditor info={info} row={context.row} tooltipPages={context.tooltipPages} disabled={disabled} onEdit={edit} /></div></details>}
    <details className="ie-section"><summary>Accessibility</summary>
      <div className="ie-section-body"><AltTextEditor key={`${info.id}:${context.altText ?? ""}`} info={info} current={context.altText} disabled={disabled} onEdit={edit} /></div></details>
  </div>;
}
