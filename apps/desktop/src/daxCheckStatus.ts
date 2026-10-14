/** What checked an imported report's formula: the line the DAX Lab writes under it. */
export type DaxCheckStatus = { text: string; tone: "ok" | "warn" };

// The labels dax_assistant._check_label writes; a blank result or "Not validated" warns.
const CHECK_LABEL = /^(?:Validated by Power BI Desktop\.|Checked on the imported data \(local preview\)\.|Ran (?:in Power BI Desktop|on the imported data)\b|Not validated:)/;
const CHECKED = /^(?:Validated by Power BI Desktop\.|Checked on the imported data \(local preview\)\.)$/;

/** The text after a formula's ```dax block: its check line, the notes the user should
 * read (a missing field, the edits made) and the references, which may stay folded. */
export function formulaNotes(response: string): { status?: DaxCheckStatus; notes: string; references: string } {
  const rest = response.replace(/```dax\s*\n[\s\S]*?```/, "").trim();
  const at = rest.search(/(?:^|\n\n)References supplied/);
  const body = at < 0 ? rest : rest.slice(0, at).trim();
  const references = at < 0 ? "" : rest.slice(at).trim();
  const paragraphs = body.split(/\n\s*\n/).map(part => part.trim()).filter(Boolean);
  const first = paragraphs[0] && CHECK_LABEL.test(paragraphs[0]) ? paragraphs.shift() : undefined;
  return {
    status: first ? { text: first, tone: CHECKED.test(first) ? "ok" : "warn" } : undefined,
    notes: paragraphs.join("\n\n"),
    references,
  };
}
