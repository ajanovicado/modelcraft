import catalog from "../../../src/ai_powerbi_analyst/studio/report_visuals.json";
import type { Visual } from "./types";

export const reportVisualCatalog = catalog.visuals as Array<{
  id: string; label: string; recipe: Visual["recipe"]; preview: string;
}>;
export const visualTypesByRecipe = Object.fromEntries(
  Object.keys(catalog.families).map(recipe => [recipe, reportVisualCatalog.filter(v => v.recipe === recipe)]),
) as Record<Visual["recipe"], typeof reportVisualCatalog>;
export const minimumVisualSize = Object.fromEntries(
  Object.entries(catalog.families).map(([recipe, family]) => [recipe, family.minimum_size]),
) as Record<Visual["recipe"], [number, number]>;

export const visualForecastCapability = catalog.editing.visual_forecast as Omit<typeof catalog.editing.visual_forecast, "defaults"> & {
  defaults: NonNullable<Visual["forecast"]>;
};
