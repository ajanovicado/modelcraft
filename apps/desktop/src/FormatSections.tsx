import { useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowUp, Pencil, Plus, Trash2, X } from "lucide-react";
import { ColorPicker } from "./ColorPicker";
import { colorHex, type ColorValue, type Operation, type ThemePalette } from "./importedEditing";
import type { VisualInfo } from "./importedFormat";
import {
  altTextOperation, analyticsKinds, analyticsLabel, analyticsOperation, cfFamily, cfFields, cfOperation, cfStyles, cfTargets, clearCfOperation, emptyRule,
  existingRules, ICON_NAMES, ICON_SETS, iconGlyph, lineDraftOf, moveRule, newCfDraft, newLineDraft, numberLines, readAnalyticsLines, removeLineOperation,
  tooltipPageOf, visualTooltipOperation, type CfDraft, type CfTarget, type LineDraft,
} from "./formatExtras";
import { ACTION_TYPES, actionDraft, actionOp, removeTooltipOp, type ActionDraft, type LayerVisual } from "./importedPanes";

type Send = (op: Operation, label: string) => void | Promise<boolean>;
const hexOf = (value: ColorValue | undefined, theme?: ThemePalette) => colorHex(value, theme);

function Color({ label, value, theme, disabled, onPick }: { label: string; value?: ColorValue; theme?: ThemePalette; disabled: boolean; onPick: (color: ColorValue) => void }) {
  return <div className="ie-row"><span className="ie-row-label">{label}</span><div className="ie-row-control"><ColorPicker label={label} value={hexOf(value, theme)} theme={theme} disabled={disabled} onPick={onPick} /></div></div>;
}

function IconPreview({ names }: { names: string[] }) {
  return <span className="ie-icon-preview" aria-hidden="true">{names.map((name, index) => { const { glyph, color } = iconGlyph(name); return <span key={index} style={{ color }}>{glyph}</span>; })}</span>;
}

/**
 * Conditional formatting (Power BI's "fx" dialog), per target: gradient with lowest, center and highest colors and values,
 * a rules table (add, remove, reorder rows), a field value (a measure gives the color), data bars, and icons from a set or
 * from rules. What the visual already has is listed with a Remove button. Apply sends one `set_conditional_format`.
 */
export function ConditionalFormatEditor({ info, theme, measures, disabled, onEdit }: {
  info: VisualInfo; theme?: ThemePalette; measures: { table: string; name: string }[]; disabled: boolean; onEdit: Send;
}) {
  const targets = cfTargets(info);
  const [draft, setDraft] = useState<CfDraft>(() => newCfDraft(info));
  const [problem, setProblem] = useState<string>();
  // The pane mounts one editor per visual (keyed by its id), so a new visual starts a new draft.
  const fields = useMemo(() => cfFields(info, draft.target), [info, draft.target]);
  const styles = cfStyles(draft.target);
  const existing = existingRules(info);
  if (!targets.length) return null;
  const set = (patch: Partial<CfDraft>) => { setDraft(current => ({ ...current, ...patch })); setProblem(undefined); };
  const setTarget = (target: CfTarget) => { const fresh = cfFields(info, target); set({ target, style: cfStyles(target)[0].value, field: fresh.some(f => f.value === draft.field) ? draft.field : fresh[0]?.value }); };
  const apply = () => {
    const built = cfOperation(info.id, draft, fields);
    if (!built.ok) { setProblem(built.message); return; }
    void onEdit(built.op, "Conditional formatting");
  };
  const table = cfFamily(info) === "table";
  return <div className="ie-cf">
    {existing.length > 0 && <div className="ie-cf-existing"><strong>This visual has</strong><ul>{existing.map((rule, index) => <li key={index}>
      <span>{rule.label}</span><button type="button" className="icon-button" disabled={disabled} aria-label={`Remove ${rule.label}`} title="Remove" onClick={() => void onEdit(clearCfOperation(info.id, rule.target, rule.field), "Removing conditional formatting")}><X size={12} /></button></li>)}</ul></div>}
    <label className="ie-inline">What to format<select value={draft.target} disabled={disabled} onChange={event => setTarget(event.target.value as CfTarget)}>{targets.map(target => <option key={target.value} value={target.value}>{target.label}</option>)}</select></label>
    {fields.length > 0 && <label className="ie-inline">{table ? "Column" : "Field"}<select value={draft.field ?? ""} disabled={disabled} onChange={event => set({ field: event.target.value })}>{fields.map(field => <option key={field.value} value={field.value}>{field.label}</option>)}</select></label>}
    {styles.length > 1 && <div className="ie-segmented" role="radiogroup" aria-label="Format style">{styles.map(style => <label key={style.value}><input type="radio" name={`cf-style-${info.id}`} checked={draft.style === style.value} disabled={disabled} onChange={() => set({ style: style.value })} />{style.label}</label>)}</div>}
    {draft.style === "gradient" && <div className="ie-cf-body">
      <Color label="Lowest value" value={draft.minColor} theme={theme} disabled={disabled} onPick={minColor => set({ minColor })} />
      <label className="ie-inline">Lowest at<input inputMode="decimal" placeholder="The lowest value" value={draft.minValue} disabled={disabled} onChange={event => set({ minValue: event.target.value })} /></label>
      <label className="ie-check"><input type="checkbox" checked={draft.useMid} disabled={disabled} onChange={event => set({ useMid: event.target.checked })} />Add a center color</label>
      {draft.useMid && <><Color label="Center" value={draft.midColor} theme={theme} disabled={disabled} onPick={midColor => set({ midColor })} />
        <label className="ie-inline">Center at<input inputMode="decimal" placeholder="The middle value" value={draft.midValue} disabled={disabled} onChange={event => set({ midValue: event.target.value })} /></label></>}
      <Color label="Highest value" value={draft.maxColor} theme={theme} disabled={disabled} onPick={maxColor => set({ maxColor })} />
      <label className="ie-inline">Highest at<input inputMode="decimal" placeholder="The highest value" value={draft.maxValue} disabled={disabled} onChange={event => set({ maxValue: event.target.value })} /></label>
      <label className="ie-inline">Blank values<select value={draft.blank} disabled={disabled} onChange={event => set({ blank: event.target.value as CfDraft["blank"] })}><option value="as_zero">Colored as zero</option><option value="no_color">No color</option><option value="specific_color">A color of their own</option></select></label>
      {draft.blank === "specific_color" && <Color label="Blank color" value={draft.blankColor} theme={theme} disabled={disabled} onPick={blankColor => set({ blankColor })} />}
    </div>}
    {draft.style === "rules" && <div className="ie-cf-body">
      <p className="ie-note">The first rule that matches decides. Leave a bound empty for "any".</p>
      <table className="ie-rules"><thead><tr><th scope="col">From (≥)</th><th scope="col">To (&lt;)</th><th scope="col">Unit</th><th scope="col">{draft.target === "icon" ? "Icon" : "Color"}</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>{draft.rules.map((rule, index) => {
          const change = (patch: Partial<typeof rule>) => set({ rules: draft.rules.map((r, i) => i === index ? { ...r, ...patch } : r) });
          return <tr key={index}>
            <td><input aria-label={`Rule ${index + 1} from`} inputMode="decimal" value={rule.min} disabled={disabled || rule.blank} onChange={event => change({ min: event.target.value })} /></td>
            <td><input aria-label={`Rule ${index + 1} to`} inputMode="decimal" value={rule.max} disabled={disabled || rule.blank} onChange={event => change({ max: event.target.value })} /></td>
            <td><select aria-label={`Rule ${index + 1} unit`} value={rule.blank ? "blank" : rule.unit} disabled={disabled} onChange={event => event.target.value === "blank" ? change({ blank: true }) : change({ blank: false, unit: event.target.value as "number" | "percent" })}><option value="number">Number</option><option value="percent">Percent</option><option value="blank">Is blank</option></select></td>
            <td>{draft.target === "icon"
              ? <select aria-label={`Rule ${index + 1} icon`} value={rule.icon ?? ""} disabled={disabled} onChange={event => change({ icon: event.target.value || undefined })}><option value="">Pick</option>{ICON_NAMES.map(name => <option key={name} value={name}>{iconGlyph(name).glyph} {name}</option>)}</select>
              : <ColorPicker label={`Rule ${index + 1} color`} value={hexOf(rule.color, theme)} theme={theme} disabled={disabled} onPick={color => change({ color })} />}</td>
            <td className="ie-rule-tools">
              <button type="button" className="icon-button" disabled={disabled || index === 0} aria-label={`Move rule ${index + 1} up`} onClick={() => set({ rules: moveRule(draft.rules, index, -1) })}><ArrowUp size={12} /></button>
              <button type="button" className="icon-button" disabled={disabled || index === draft.rules.length - 1} aria-label={`Move rule ${index + 1} down`} onClick={() => set({ rules: moveRule(draft.rules, index, 1) })}><ArrowDown size={12} /></button>
              <button type="button" className="icon-button" disabled={disabled || draft.rules.length === 1} aria-label={`Remove rule ${index + 1}`} onClick={() => set({ rules: draft.rules.filter((_, i) => i !== index) })}><Trash2 size={12} /></button>
            </td>
          </tr>;
        })}</tbody></table>
      <button type="button" className="secondary" disabled={disabled || draft.rules.length >= 20} onClick={() => set({ rules: [...draft.rules, { ...emptyRule(), icon: draft.target === "icon" ? ICON_NAMES[0] : undefined }] })}><Plus size={12} />Add a rule</button>
    </div>}
    {draft.style === "field_value" && <div className="ie-cf-body">
      <label className="ie-inline">Measure<select value={draft.measure ?? ""} disabled={disabled} onChange={event => set({ measure: event.target.value || undefined })}><option value="">Pick the measure</option>{measures.map(m => <option key={`${m.table}.${m.name}`} value={m.name}>{m.name}</option>)}</select></label>
      <p className="ie-note">{draft.target === "web_url" ? "The measure returns a web address for each row." : "The measure returns a color for each value: a name like \"red\" or a hex code like \"#D64550\"."}</p>
    </div>}
    {draft.style === "data_bars" && <div className="ie-cf-body">
      <Color label="Positive bars" value={draft.positiveColor} theme={theme} disabled={disabled} onPick={positiveColor => set({ positiveColor })} />
      <Color label="Negative bars" value={draft.negativeColor} theme={theme} disabled={disabled} onPick={negativeColor => set({ negativeColor })} />
      <Color label="Axis" value={draft.axisColor} theme={theme} disabled={disabled} onPick={axisColor => set({ axisColor })} />
      <label className="ie-inline">Direction<select value={draft.direction} disabled={disabled} onChange={event => set({ direction: event.target.value as CfDraft["direction"] })}><option value="left_to_right">Left to right</option><option value="right_to_left">Right to left</option></select></label>
      <label className="ie-inline">Lowest at<input inputMode="decimal" placeholder="The lowest value" value={draft.minValue} disabled={disabled} onChange={event => set({ minValue: event.target.value })} /></label>
      <label className="ie-inline">Highest at<input inputMode="decimal" placeholder="The highest value" value={draft.maxValue} disabled={disabled} onChange={event => set({ maxValue: event.target.value })} /></label>
      <label className="ie-check"><input type="checkbox" checked={draft.barOnly} disabled={disabled} onChange={event => set({ barOnly: event.target.checked })} />Show the bar only</label>
    </div>}
    {(draft.style === "icons" || (draft.style === "rules" && draft.target === "icon")) && <div className="ie-cf-body">
      {draft.style === "icons" && <label className="ie-inline">Icon set<select value={draft.iconSet} disabled={disabled} onChange={event => set({ iconSet: event.target.value })}>{ICON_SETS.map(set => <option key={set.value} value={set.value}>{set.label}</option>)}</select></label>}
      {draft.style === "icons" && <IconPreview names={ICON_SETS.find(set => set.value === draft.iconSet)?.icons ?? []} />}
      <label className="ie-inline">Icon position<select value={draft.iconPosition} disabled={disabled} onChange={event => set({ iconPosition: event.target.value as CfDraft["iconPosition"] })}><option value="left">Left of the value</option><option value="right">Right of the value</option><option value="icon_only">Icon only</option></select></label>
      <label className="ie-inline">Alignment<select value={draft.iconAlignment} disabled={disabled} onChange={event => set({ iconAlignment: event.target.value as CfDraft["iconAlignment"] })}><option value="top">Top</option><option value="middle">Middle</option><option value="bottom">Bottom</option></select></label>
    </div>}
    {table && draft.target !== "web_url" && draft.target !== "title_color" && draft.target !== "visual_background" && <label className="ie-inline">Apply to<select value={draft.applyTo} disabled={disabled} onChange={event => set({ applyTo: event.target.value as CfDraft["applyTo"] })}><option value="values">Values only</option><option value="values_and_totals">Values and totals</option><option value="totals">Totals only</option></select></label>}
    {problem && <p className="ie-note ie-error" role="alert">{problem}</p>}
    <div className="ie-pane-actions"><button type="button" className="primary yellow" disabled={disabled} onClick={apply}>Apply</button></div>
  </div>;
}

/** Power BI's Analytics pane for a chart: the lines it has (edit, remove) and a new line of any kind. */
export function AnalyticsEditor({ info, row, theme, disabled, onEdit }: { info: VisualInfo; row: unknown; theme?: ThemePalette; disabled: boolean; onEdit: Send }) {
  const kinds = analyticsKinds(info);
  const lines = numberLines(readAnalyticsLines(row));
  const [editing, setEditing] = useState<{ draft: LineDraft; existing?: (typeof lines)[number] }>();
  const [problem, setProblem] = useState<string>();
  if (!kinds.length) return null;
  const measures = (info.columns ?? []).filter(c => c.kind === "measure" && !c.hidden).map(c => c.label || c.name);
  const save = () => {
    if (!editing) return;
    const built = analyticsOperation(info.id, editing.draft, editing.existing);
    if (!built.ok) { setProblem(built.message); return; }
    setEditing(undefined);
    void onEdit(built.op, "Analytics line");
  };
  const draft = editing?.draft;
  const set = (patch: Partial<LineDraft>) => { if (editing) setEditing({ ...editing, draft: { ...editing.draft, ...patch } }); setProblem(undefined); };
  return <div className="ie-analytics">
    {lines.length ? <ul className="ie-line-list">{lines.map(item => <li key={`${item.line.kind}-${item.number}-${item.line.id ?? ""}`}>
      {item.line.color && <span className="ie-swatch" style={{ background: item.line.color.slice(0, 7) }} aria-hidden="true" />}
      <span>{item.line.name ?? `${analyticsLabel(item.line.kind)} ${item.number}`}<small>{analyticsLabel(item.line.kind)}{item.line.value !== null && item.line.value !== undefined ? ` · ${String(item.line.value)}` : ""}</small></span>
      <span className="ie-row-tools">
        {item.line.kind !== "trend" && <button type="button" className="icon-button" disabled={disabled} aria-label={`Edit ${item.line.name ?? analyticsLabel(item.line.kind)}`} onClick={() => setEditing({ draft: lineDraftOf(item.line), existing: item })}><Pencil size={12} /></button>}
        <button type="button" className="icon-button" disabled={disabled} aria-label={`Remove ${item.line.name ?? analyticsLabel(item.line.kind)}`} onClick={() => void onEdit(removeLineOperation(info.id, item.line, item.number), "Removing the analytics line")}><Trash2 size={12} /></button>
      </span></li>)}</ul>
      : <p className="ie-note">No analytics lines on this chart.</p>}
    {!editing && <label className="ie-inline">Add a line<select value="" disabled={disabled} onChange={event => { if (event.target.value) setEditing({ draft: newLineDraft(event.target.value as LineDraft["kind"]) }); }}><option value="">Choose a line…</option>{kinds.map(kind => <option key={kind.value} value={kind.value}>{kind.label}</option>)}</select></label>}
    {editing && draft && <fieldset className="ie-line-form">
      <legend>{editing.existing ? "Edit the line" : analyticsLabel(draft.kind)}</legend>
      {(draft.kind === "constant" || draft.kind === "x_constant") && <label className="ie-inline">Value<input inputMode="decimal" placeholder={draft.kind === "x_constant" ? "A number or YYYY-MM-DD" : "50000"} value={draft.value} onChange={event => set({ value: event.target.value })} /></label>}
      {draft.kind === "percentile" && <label className="ie-inline">Percentile<input inputMode="decimal" value={draft.percentile} onChange={event => set({ percentile: event.target.value })} /></label>}
      {draft.kind === "forecast" && <label className="ie-inline">Points ahead<input inputMode="numeric" value={draft.forecastLength} onChange={event => set({ forecastLength: event.target.value })} /></label>}
      {measures.length > 1 && draft.kind !== "constant" && draft.kind !== "x_constant" && <label className="ie-inline">Series<select value={draft.measure ?? ""} onChange={event => set({ measure: event.target.value || undefined })}><option value="">The first</option>{measures.map(name => <option key={name} value={name}>{name}</option>)}</select></label>}
      <label className="ie-inline">Name<input value={draft.name} maxLength={100} placeholder="Target" onChange={event => set({ name: event.target.value })} /></label>
      <Color label="Color" value={draft.color} theme={theme} disabled={disabled} onPick={color => set({ color })} />
      {draft.kind !== "forecast" && <label className="ie-inline">Style<select value={draft.style} onChange={event => set({ style: event.target.value as LineDraft["style"] })}><option value="solid">Solid</option><option value="dashed">Dashed</option><option value="dotted">Dotted</option></select></label>}
      <label className="ie-inline">Position<select value={draft.position} onChange={event => set({ position: event.target.value as LineDraft["position"] })}><option value="front">In front of the data</option><option value="back">Behind the data</option></select></label>
      <label className="ie-check"><input type="checkbox" checked={draft.dataLabel} onChange={event => set({ dataLabel: event.target.checked })} />Show its data label</label>
      {problem && <p className="ie-note ie-error" role="alert">{problem}</p>}
      <div className="ie-pane-actions"><button type="button" className="primary yellow" disabled={disabled} onClick={save}>{editing.existing ? "Save" : "Add"}</button><button type="button" className="secondary" onClick={() => setEditing(undefined)}>Cancel</button></div>
    </fieldset>}
    {lines.length > 0 && <p className="ie-note">Lines computed from the rows Studio draws can differ slightly from Power BI's on large visuals.</p>}
  </div>;
}

/** Alt text (what a screen reader says for the visual). */
export function AltTextEditor({ info, current, disabled, onEdit }: { info: VisualInfo; current?: string; disabled: boolean; onEdit: Send }) {
  // Prefilled with the report's alt text; the pane remounts this editor when it changes.
  const [text, setText] = useState(current ?? "");
  return <div className="ie-cf-body">
    <label className="ie-field-block">Alt text<textarea rows={2} maxLength={1000} value={text} placeholder="Describe what this visual shows, for screen readers" disabled={disabled} onChange={event => setText(event.target.value)} /></label>
    <div className="ie-pane-actions">
      <button type="button" className="secondary" disabled={disabled || !text.trim() || text.trim() === (current ?? "").trim()} onClick={() => void onEdit(altTextOperation(info.id, text), "Alt text")}>Save alt text</button>
      <button type="button" className="secondary" disabled={disabled || !current} title="Remove the alt text" onClick={() => void onEdit(altTextOperation(info.id, ""), "Alt text")}>Remove</button>
    </div>
  </div>;
}

/** The report page a data visual shows as its tooltip. */
export function TooltipPageEditor({ info, row, tooltipPages, disabled, onEdit }: { info: VisualInfo; row: unknown; tooltipPages: { id: string; title: string }[]; disabled: boolean; onEdit: Send }) {
  const current = tooltipPageOf(row) ?? "";
  // A page that stopped being a tooltip page stays listed while the visual still points at it, so the select shows the truth.
  const listed = current && !tooltipPages.some(page => page.id === current) ? [...tooltipPages, { id: current, title: "(not a tooltip page any more)" }] : tooltipPages;
  return <div className="ie-cf-body">
    <label className="ie-inline">Tooltip<select value={current} disabled={disabled} onChange={event => void onEdit(visualTooltipOperation(info.id, event.target.value || undefined), "Tooltip page")}>
      <option value="">Default (Power BI's tooltip)</option>{listed.map(page => <option key={page.id} value={page.id}>Page: {page.title}</option>)}</select></label>
    {!tooltipPages.length && <p className="ie-note">No page is a tooltip page yet. Make one in Page settings (Page type: Tooltip).</p>}
  </div>;
}

/** What clicking a button (or a shape or picture) does, and its tooltip. */
export function ActionEditor({ info, layer, pages, bookmarks, drillthroughs, disabled, onEdit }: {
  info: VisualInfo; layer?: LayerVisual; pages: { id: string; title: string }[]; bookmarks: { name: string; displayName: string }[];
  drillthroughs: { page: string; title: string }[]; disabled: boolean; onEdit: Send;
}) {
  const [draft, setDraft] = useState<ActionDraft>(() => actionDraft(layer?.action));
  const [problem, setProblem] = useState<string>();
  useEffect(() => { setDraft(actionDraft(layer?.action)); setProblem(undefined); }, [info.id, layer?.action]);
  const save = () => {
    const built = actionOp(info.id, draft);
    if (!built.ok) { setProblem(built.message); return; }
    void onEdit(built.op, "Button action");
  };
  return <div className="ie-cf-body">
    {layer?.action && !layer.action.valid && <p className="ie-note ie-error">The page or bookmark this action pointed at no longer exists.</p>}
    <label className="ie-inline">Action<select value={draft.type} disabled={disabled} onChange={event => { setDraft({ ...draft, type: event.target.value as ActionDraft["type"] }); setProblem(undefined); }}>{ACTION_TYPES.map(type => <option key={type.value} value={type.value}>{type.label}</option>)}</select></label>
    {draft.type === "page" && <label className="ie-inline">Page<select value={draft.page ?? ""} disabled={disabled} onChange={event => setDraft({ ...draft, page: event.target.value || undefined })}><option value="">Pick a page</option>{pages.map(page => <option key={page.id} value={page.id}>{page.title}</option>)}</select></label>}
    {draft.type === "drillthrough" && <label className="ie-inline">Page<select value={draft.page ?? ""} disabled={disabled} onChange={event => setDraft({ ...draft, page: event.target.value || undefined })}><option value="">Pick a drillthrough page</option>{drillthroughs.map(page => <option key={page.page} value={page.page}>{page.title}</option>)}</select></label>}
    {draft.type === "drillthrough" && !drillthroughs.length && <p className="ie-note">No page is a drillthrough page yet (Page settings, Page type).</p>}
    {draft.type === "bookmark" && <label className="ie-inline">Bookmark<select value={draft.bookmark ?? ""} disabled={disabled} onChange={event => setDraft({ ...draft, bookmark: event.target.value || undefined })}><option value="">Pick a bookmark</option>{bookmarks.map(b => <option key={b.name} value={b.name}>{b.displayName}</option>)}</select></label>}
    {draft.type === "web_url" && <label className="ie-inline">Web address<input type="url" value={draft.url ?? ""} placeholder="https://" disabled={disabled} onChange={event => setDraft({ ...draft, url: event.target.value })} /></label>}
    {draft.type === "qna" && <p className="ie-note">Q&amp;A works in Power BI only.</p>}
    <label className="ie-inline">Tooltip<input value={draft.tooltip} maxLength={500} placeholder="Shown when the pointer is over it" disabled={disabled} onChange={event => setDraft({ ...draft, tooltip: event.target.value })} /></label>
    {problem && <p className="ie-note ie-error" role="alert">{problem}</p>}
    <div className="ie-pane-actions"><button type="button" className="secondary" disabled={disabled} onClick={save}>Save action</button>
      {layer?.action?.tooltip && <button type="button" className="secondary" disabled={disabled} title="Take the tooltip off; the action stays" onClick={() => void onEdit(removeTooltipOp(info.id), "Removing the tooltip")}>Remove tooltip</button>}</div>
  </div>;
}
