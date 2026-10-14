// Number formatting the canvas does itself (contract sections 13 and 15): chart data labels with their own
// Power BI format string ("Custom" display units), axis display units, and the values of analytics lines.
// The server already formats `formatted_rows`; this is only for numbers that arrive raw.

type Nullable<T> = T | null | undefined;

/** Power BI's display-unit suffixes (a divisor to its letter). */
export const UNIT_SUFFIX: [number, string][] = [[1e12, "T"], [1e9, "bn"], [1e6, "M"], [1e3, "K"]];

export function unitSuffix(units: number): string {
  return UNIT_SUFFIX.find(([divisor]) => divisor === units)?.[1] ?? "";
}

/** Splits a format string on `;` that is not quoted or escaped. */
export function formatSections(format: string): string[] {
  const sections: string[] = [];
  let current = "";
  let quoted = false;
  for (let i = 0; i < format.length; i++) {
    const c = format[i];
    if (c === "\\" && i + 1 < format.length) { current += c + format[i + 1]; i++; continue; }
    if (c === '"') quoted = !quoted;
    if (c === ";" && !quoted) { sections.push(current); current = ""; continue; }
    current += c;
  }
  sections.push(current);
  return sections;
}

type Token = { kind: "literal"; text: string } | { kind: "digit"; char: string } | { kind: "comma" } | { kind: "point" } | { kind: "percent" };

function tokenize(section: string): Token[] {
  const tokens: Token[] = [];
  for (let i = 0; i < section.length; i++) {
    const c = section[i];
    if (c === "\\" && i + 1 < section.length) { tokens.push({ kind: "literal", text: section[++i] }); continue; }
    if (c === '"') {
      const end = section.indexOf('"', i + 1);
      const stop = end < 0 ? section.length : end;
      tokens.push({ kind: "literal", text: section.slice(i + 1, stop) });
      i = stop;
      continue;
    }
    if (c === "0" || c === "#") tokens.push({ kind: "digit", char: c });
    else if (c === ",") tokens.push({ kind: "comma" });
    else if (c === ".") tokens.push({ kind: "point" });
    else if (c === "%") tokens.push({ kind: "percent" });
    else tokens.push({ kind: "literal", text: c });
  }
  return tokens;
}

/** Formats a number with a Power BI (Excel-style) format string: sections for positive, negative and zero,
 * `0`/`#` digits, `,` grouping (a trailing comma divides by 1000), `.` decimals, `%`, quoted and escaped
 * literals. A string without any digit placeholder (or `General`) falls back to plain digits. */
export function formatWithString(value: number, format: Nullable<string>): string {
  if (!Number.isFinite(value)) return String(value);
  const plain = () => value.toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (!format || /^general$/i.test(format.trim())) return plain();
  const sections = formatSections(format);
  let section = sections[0];
  let number = value;
  let negativeSection = false;
  if (value < 0 && sections.length > 1 && sections[1] !== "") { section = sections[1]; number = -value; negativeSection = true; }
  else if (value === 0 && sections.length > 2 && sections[2] !== "") { section = sections[2]; }
  const tokens = tokenize(section);
  const firstDigit = tokens.findIndex(t => t.kind === "digit");
  if (firstDigit < 0) return tokens.map(t => t.kind === "literal" ? t.text : t.kind === "percent" ? "%" : "").join("") || plain();
  let lastDigit = firstDigit;
  tokens.forEach((t, i) => { if (t.kind === "digit") lastDigit = i; });
  const point = tokens.findIndex((t, i) => t.kind === "point" && i > firstDigit && i <= lastDigit + 1);
  let decimalsMin = 0;
  let decimalsMax = 0;
  let grouping = false;
  let scale = 1;
  tokens.forEach((t, i) => {
    if (t.kind === "digit" && point >= 0 && i > point) { decimalsMax++; if (t.char === "0") decimalsMin++; }
    if (t.kind === "comma") {
      // After the last digit a comma divides by a thousand; among the integer digits it groups.
      if (i > lastDigit) scale *= 1000;
      else if (i > firstDigit && (point < 0 || i < point)) grouping = true;
    }
    if (t.kind === "percent") scale /= 100;
  });
  const minInteger = tokens.slice(firstDigit, point >= 0 ? point : lastDigit + 1).filter(t => t.kind === "digit" && t.char === "0").length;
  const scaled = number / scale;
  const digits = Math.abs(scaled).toLocaleString("en-US", {
    minimumFractionDigits: decimalsMin, maximumFractionDigits: decimalsMax, minimumIntegerDigits: Math.max(1, minInteger), useGrouping: grouping,
  });
  const body = minInteger === 0 && digits.startsWith("0.") ? digits.slice(1) : digits;
  const sign = scaled < 0 && !negativeSection && Number(digits.replace(/[^0-9.]/g, "")) !== 0 ? "-" : "";
  const text = (from: number, to: number) => tokens.slice(from, to).map(t => t.kind === "literal" ? t.text : t.kind === "percent" ? "%" : "").join("");
  const inner = tokens.slice(firstDigit, lastDigit + 1).filter(t => t.kind === "literal" || t.kind === "percent").map(t => t.kind === "literal" ? t.text : "%").join("");
  return `${sign}${text(0, firstDigit)}${body}${inner}${text(lastDigit + 1, tokens.length)}`;
}

/** A raw number with display units (a divisor, or null for none) and decimals (null for automatic).
 * Percent formats are never scaled by units. */
export function formatUnits(raw: number, options: { displayUnits?: Nullable<number>; decimals?: Nullable<number>; format?: Nullable<string> }): string {
  const percent = !!options.format && isPercentFormat(options.format);
  const units = options.displayUnits ?? null;
  const decimals = options.decimals ?? null;
  if (!percent && units && units > 1) {
    return (raw / units).toLocaleString("en-US", { minimumFractionDigits: decimals ?? 0, maximumFractionDigits: decimals ?? 1 }) + unitSuffix(units);
  }
  if (decimals !== null) {
    const digits = { minimumFractionDigits: decimals, maximumFractionDigits: decimals };
    return percent ? `${(raw * 100).toLocaleString("en-US", digits)}%` : raw.toLocaleString("en-US", digits);
  }
  if (options.format) return formatWithString(raw, options.format);
  return raw.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

/** A `%` that is a percentage (not quoted, not escaped). */
export function isPercentFormat(format: string): boolean {
  return tokenize(formatSections(format)[0] ?? "").some(t => t.kind === "percent");
}

/** A Vega expression that labels an axis in display units: `format(datum.value / 1000, ',.1f') + 'K'`. */
export function axisUnitsExpr(units: Nullable<number>, decimals: Nullable<number>): string | undefined {
  const hasUnits = !!units && units > 1;
  if (!hasUnits && (decimals === null || decimals === undefined)) return undefined;
  const divisor = hasUnits ? units : 1;
  const pattern = decimals === null || decimals === undefined ? ",.3~r" : `,.${Math.max(0, Math.min(10, Math.floor(decimals)))}f`;
  const suffix = hasUnits ? unitSuffix(divisor) : "";
  return `format(datum.value / ${divisor}, '${pattern}')${suffix ? ` + '${suffix}'` : ""}`;
}
