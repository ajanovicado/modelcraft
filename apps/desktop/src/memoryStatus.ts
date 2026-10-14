/**
 * Settings memory line. RAM is reported, never required: the single local model
 * starts whatever memory is free, so there is no pause, threshold or fallback to show.
 */
export function memoryDetail(totalMemoryGb?: number | null) {
  const total = typeof totalMemoryGb === "number" && Number.isFinite(totalMemoryGb) && totalMemoryGb > 0
    ? `${totalMemoryGb.toFixed(1)} GiB usable RAM · ` : "";
  return `${total}Model unloads before Desktop launch`;
}

export type ModelReadiness = {
  model_available: boolean;
  available_memory_gb: number;
  error?: string | null;
  identity?: { name: string; digest: string } | null;
  total_memory_gb?: number | null;
};
