import type { Draft, Relationship } from "./types";

/** Joins the engine verified on the full data start included; the reviewer can still
 * untick them. Before, every draft opened with "0 of 2 selected" and one click on
 * Approve built a model with Returns and People unrelated. Rejected joins stay out. */
export function defaultRelationshipSelection(relationships: Relationship[]): Set<string> {
  return new Set(relationships.filter((item) => !item.blockers.length && item.review_status !== "rejected" && (
    item.review_status === "approved"
    || item.recommended_by_model
    || (item.is_active && item.cardinality !== "many_to_many"
      && (item.rely_on_referential_integrity || item.evidence.some((line) => line.startsWith("Subset table:"))))
  )).map((item) => item.id));
}

export type ReviewCheckId = "relationships" | "measures" | "advanced_dax" | "pages";
export type ReviewCheckStatus = "ready" | "action" | "blocked";
export type ReviewCheck = {
  id: ReviewCheckId;
  label: string;
  detail: string;
  status: ReviewCheckStatus;
};

export const EXECUTIVE_OVERVIEW_PROMPT =
  "Create one executive overview page with KPIs, a trend, and a category breakdown from the reviewed data.";

export function pageReviewIssues(pages: Draft["pages"]): string[] {
  const issues: string[] = [];
  if (!pages.length) issues.push("The report needs at least one page.");
  for (const page of pages) {
    if (!page.title.trim()) issues.push("Every report page needs a title.");
    if (!page.purpose.trim()) issues.push(`${page.title || "Untitled page"} needs a purpose.`);
    if (!page.visuals.length) issues.push(`${page.title || "Untitled page"} needs at least one visual.`);
    for (const visual of page.visuals) {
      if (visual.recipe !== "detail" && !visual.measure_id) {
        issues.push(`${visual.title || "A visual"} needs a reviewed measure.`);
      }
    }
  }
  return [...new Set(issues)];
}

export function buildReviewChecklist(draft: Draft, approvedRelationshipIds: Set<string>): ReviewCheck[] {
  const relationships = draft.semantic_model.relationships;
  const blocked = relationships.filter((item) => item.review_status !== "rejected" && item.blockers.length);
  const selectable = relationships.filter((item) => !item.blockers.length);
  let relationshipStatus: ReviewCheckStatus = "ready";
  let relationshipDetail = "No cross-table relationships to review.";
  if (selectable.length) {
    relationshipDetail = `${approvedRelationshipIds.size} of ${selectable.length} selected to include. Unchecked candidates are excluded.`;
  }
  // Blocked candidates cannot be selected and approval rejects them, leaving the
  // tables unrelated. They must not stop the rest of the draft from approving;
  // a visual that needs one is caught below.
  if (blocked.length) {
    const excluded = `${blocked.length} blocked relationship${blocked.length === 1 ? "" : "s"} will be excluded.`;
    relationshipDetail = selectable.length ? `${excluded} ${relationshipDetail}` : excluded;
  }
  // A page that groups a measure by another table's field cannot build unless a
  // relationship between those tables is included; say so before approval fails.
  const measureTables = new Map(draft.measure_proposal.measures.map((measure) => [measure.id, measure.table_id]));
  const needed = draft.pages.flatMap((page) => page.visuals).filter((visual) => {
    const measureTable = visual.measure_id ? measureTables.get(visual.measure_id) : undefined;
    return (visual.recipe === "trend" || visual.recipe === "category") && measureTable && measureTable !== visual.table_id
      && !selectable.some((item) => approvedRelationshipIds.has(item.id)
        && new Set([item.from_table_id, item.to_table_id]).has(measureTable) && new Set([item.from_table_id, item.to_table_id]).has(visual.table_id));
  });
  if (needed.length) {
    relationshipStatus = "action";
    relationshipDetail = `Include the relationship used by “${needed[0].title}” — cross-table visuals need it to build.`;
  }

  const measures = draft.measure_proposal.measures;
  const advanced = measures.filter((measure) => measure.tier === "advanced_dax");
  const advancedPending = advanced.filter((measure) => !measure.approved);
  const pageIssues = pageReviewIssues(draft.pages);

  return [
    { id: "relationships", label: "Relationships", detail: relationshipDetail, status: relationshipStatus },
    {
      id: "measures",
      label: "Measures",
      detail: `${measures.length} catalog-bound measures.`,
      status: "ready",
    },
    {
      id: "advanced_dax",
      label: "Advanced DAX",
      detail: advancedPending.length
        ? `${advancedPending.length} formula${advancedPending.length === 1 ? "" : "s"} still need explicit review.`
        : advanced.length
          ? "Advanced formulas reviewed."
          : "No advanced DAX in this draft.",
      status: advancedPending.length ? "action" : "ready",
    },
    {
      id: "pages",
      label: "Pages",
      detail: pageIssues[0] || `${draft.pages.length} page${draft.pages.length === 1 ? "" : "s"} ready for build.`,
      status: pageIssues.length ? "action" : "ready",
    },
  ];
}

export function reviewBlockReason(
  items: ReviewCheck[],
  extras: { busy?: boolean; designerDirty?: boolean; approved?: boolean } = {},
): string | undefined {
  if (extras.busy) return "Finish the current operation first.";
  if (extras.designerDirty) return "Save or discard unsaved report edits first.";
  const blocked = items.find((item) => item.status === "blocked");
  if (blocked) return blocked.detail;
  const action = items.find((item) => item.status === "action");
  if (action && !extras.approved) return action.detail;
  return undefined;
}

export function canApproveDraft(items: ReviewCheck[], extras: { busy?: boolean; designerDirty?: boolean } = {}): boolean {
  return !reviewBlockReason(items, extras);
}
