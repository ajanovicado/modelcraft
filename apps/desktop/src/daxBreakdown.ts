import type { DaxResult } from "./types";
import { periodLabel } from "./daxDatePreview";

export function breakdownRows(result?: DaxResult) {
  const meta = result?.breakdown;
  if (!meta || !result) return [];
  return result.rows.map(row => {
    const raw = row[meta.column] ?? null;
    const metric = row[meta.value_column];
    const period = meta.grain && typeof raw === "string" ? raw.slice(0, 10) : undefined;
    return {
      key: JSON.stringify(raw), raw, period,
      label: period && meta.grain ? periodLabel(period, meta.grain) : raw === null ? "(Blank)" : raw === "" ? "(Empty text)" : String(raw),
      value: typeof metric === "number" && Number.isFinite(metric) ? metric : null,
    };
  });
}

export function breakdownChartRows(result?: DaxResult) {
  const all = breakdownRows(result);
  if (result?.breakdown?.grain) return all.filter(row => row.period);
  // Stable keys keep blank values, boolean false and literal category names distinct.
  return all.slice(0, 20).map(row => ({ ...row, label: all.filter(other => other.label === row.label).length > 1 ? `${row.label} (${row.raw === null ? "blank" : typeof row.raw})` : row.label }));
}
