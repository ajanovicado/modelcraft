import type { Draft, DraftRevisionReview } from "./types";

/** Compare reviewed objects by stable identity; names and titles describe their changes. */
export function compareDrafts(before: Draft, after: Draft, prompt: string): DraftRevisionReview {
  const beforeMeasures = new Map(before.measure_proposal.measures.map(item => [item.id, item]));
  const afterMeasures = new Map(after.measure_proposal.measures.map(item => [item.id, item]));
  const beforePages = new Map(before.pages.map(item => [item.id, item]));
  const afterPages = new Map(after.pages.map(item => [item.id, item]));
  const measureSignature = (item: Draft["measure_proposal"]["measures"][number]) => JSON.stringify(item);
  const pageSignature = (item: Draft["pages"][number]) =>
    JSON.stringify([item.title, item.purpose, item.visuals]);
  const countVisuals = (value: Draft) => value.pages.reduce((sum, item) => sum + item.visuals.length, 0);

  return {
    prompt,
    added_measures: after.measure_proposal.measures
      .filter(item => !beforeMeasures.has(item.id)).map(item => item.name),
    changed_measures: after.measure_proposal.measures.filter(item => {
      const previous = beforeMeasures.get(item.id);
      return previous && measureSignature(previous) !== measureSignature(item);
    }).map(item => item.name),
    removed_measures: before.measure_proposal.measures
      .filter(item => !afterMeasures.has(item.id)).map(item => item.name),
    added_pages: after.pages.filter(item => !beforePages.has(item.id)).map(item => item.title),
    changed_pages: after.pages.filter(item => {
      const previous = beforePages.get(item.id);
      return previous && pageSignature(previous) !== pageSignature(item);
    }).map(item => item.title),
    removed_pages: before.pages.filter(item => !afterPages.has(item.id)).map(item => item.title),
    before: {
      measures: before.measure_proposal.measures.length,
      pages: before.pages.length,
      visuals: countVisuals(before),
    },
    after: {
      measures: after.measure_proposal.measures.length,
      pages: after.pages.length,
      visuals: countVisuals(after),
    },
    approval_reset: after.status === "draft" && !after.semantic_model.approved,
  };
}
