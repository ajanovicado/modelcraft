import type { DaxDatePreview, DaxResult } from "./types";

export function periodLabel(value: string, grain: DaxDatePreview["grain"]): string {
  // Calendar strings, not local timestamps: don't shift midnight into yesterday.
  const [year, month, day] = value.slice(0, 10).split("-").map(Number);
  if (!year || !month || !day) return value;
  if (grain === "year") return String(year);
  if (grain === "quarter") return `Q${Math.ceil(month / 3)} ${year}`;
  return new Intl.DateTimeFormat("en", { timeZone: "UTC", year: "numeric", month: "short", ...(grain === "day" ? { day: "numeric" } : {}) }).format(new Date(Date.UTC(year, month - 1, day)));
}

export function dateChartRows(result: DaxResult) {
  const context = result.date_preview;
  if (!context) return [];
  return result.rows.flatMap(row => {
    const period = row[context.column];
    const value = row[context.value_column];
    if (typeof period !== "string") return [];
    return [{ period: period.slice(0, 10), label: periodLabel(period, context.grain), value: typeof value === "number" && Number.isFinite(value) ? value : null }];
  });
}
