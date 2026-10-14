import { useEffect, useId, useRef, useState } from "react";
import { normalizeHex, pushRecentColor, shadeHex, themeColorValue, themeSwatches, type ColorValue, type ThemePalette } from "./importedEditing";

const STORAGE_KEY = "studio.recent-colors";
// The order of a theme swatch column: the color itself, three lighter tints, two darker shades.
const ROWS = [0, 0.8, 0.6, 0.4, -0.25, -0.5];

function loadRecent(): string[] {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && normalizeHex(item) !== undefined).slice(0, 8) : [];
  } catch { return []; }
}
function saveRecent(colors: string[]) {
  try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(colors)); } catch { /* private window or blocked storage: recents are a convenience */ }
}

const shadeName = (percent: number) => percent === 0 ? "" : percent > 0 ? `, lighter ${Math.round(percent * 100)}%` : `, darker ${Math.round(-percent * 100)}%`;

/**
 * Picks a color for an edit: the report's own theme colors first (ids 0 to 9 with their tints, the
 * only ids Power BI resolves), then a hex box and the colors used recently. `value` is the color the
 * visual has now (a hex string); an untouched property shows "Auto (theme)".
 */
export function ColorPicker({ label, value, auto, theme, disabled, onPick }: {
  label: string; value?: string; auto?: boolean; theme?: ThemePalette; disabled?: boolean; onPick: (color: ColorValue) => void;
}) {
  const [open, setOpen] = useState(false);
  const [hex, setHex] = useState("");
  const [invalid, setInvalid] = useState(false);
  const [recent, setRecent] = useState<string[]>(loadRecent);
  const panelId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  // Escape closes the panel and returns focus to the button that opened it.
  useEffect(() => {
    const node = panel.current;
    if (!open || !node) return;
    const keys = (event: KeyboardEvent) => { if (event.key === "Escape") { event.stopPropagation(); setOpen(false); trigger.current?.focus(); } };
    node.addEventListener("keydown", keys);
    return () => node.removeEventListener("keydown", keys);
  }, [open]);
  const swatches = themeSwatches(theme);
  const current = value ? normalizeHex(value) : undefined;
  const choose = (color: ColorValue, hexValue: string) => {
    const next = pushRecentColor(recent, hexValue);
    setRecent(next); saveRecent(next);
    onPick(color);
  };
  const applyHex = () => {
    const normalized = normalizeHex(hex);
    if (!normalized) { setInvalid(true); return; }
    setInvalid(false); setHex("");
    choose(normalized, normalized);
  };
  return <div className="ie-color">
    <button type="button" ref={trigger} className="ie-color-trigger" disabled={disabled} aria-expanded={open} aria-controls={open ? panelId : undefined} aria-label={`${label}: ${current ?? "Auto (theme)"}`} onClick={() => setOpen(v => !v)}>
      <span className={`ie-swatch${current ? "" : " auto"}`} style={current ? { background: current } : undefined} aria-hidden="true" />
      <span className={auto || !current ? "ie-auto" : undefined}>{auto || !current ? "Auto (theme)" : current}</span>
    </button>
    {open && <div className="ie-color-panel" id={panelId} role="group" aria-label={`${label} colors`} ref={panel}>
      <strong>Theme colors</strong>
      <div className="ie-swatch-grid" role="group" aria-label="Theme colors">
        {ROWS.map(percent => swatches.map(swatch => {
          const shown = shadeHex(swatch.hex, percent);
          return <button type="button" key={`${swatch.id}:${percent}`} className={`ie-swatch-button${current === shown ? " selected" : ""}`} style={{ background: shown }}
            aria-label={`${swatch.label}${shadeName(percent)}`} title={`${swatch.label}${shadeName(percent)}`} aria-pressed={current === shown}
            onClick={() => choose(themeColorValue(swatch.id, percent), shown)} />;
        }))}
      </div>
      {recent.length > 0 && <><strong>Recent colors</strong><div className="ie-swatch-row" role="group" aria-label="Recent colors">{recent.map(color => <button type="button" key={color} className={`ie-swatch-button${current === color ? " selected" : ""}`} style={{ background: color }} aria-label={color} title={color} aria-pressed={current === color} onClick={() => choose(color, color)} />)}</div></>}
      <label className="ie-hex">Hex color
        <span><input value={hex} placeholder="#0B6E4F" spellCheck={false} maxLength={7} aria-invalid={invalid} onChange={event => { setHex(event.target.value); setInvalid(false); }} onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); applyHex(); } }} />
          <button type="button" className="secondary" onClick={applyHex}>Apply</button></span>
        {invalid && <small role="alert">Enter a color like #0B6E4F.</small>}
      </label>
    </div>}
  </div>;
}
