export type ModelStorageMode = "import" | "direct_query" | "dual" | "hybrid" | "direct_lake" | "unknown";
export type ModelStorageKind = ModelStorageMode | "mixed" | "no_local_model";
export type TableStorage = { name: string; mode: ModelStorageMode; partition_modes: ModelStorageMode[]; local_evaluation: "snapshot" | "power_bi" | "unknown" };
export type ModelStorage = { known: boolean; kind: ModelStorageKind; default_mode: ModelStorageMode | null; table_counts: Record<string, number>; tables: TableStorage[]; detail: string };

const MODES: ModelStorageMode[] = ["import", "direct_query", "dual", "hybrid", "direct_lake", "unknown"];
const KINDS: ModelStorageKind[] = [...MODES, "mixed", "no_local_model"];
const record = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const mode = (value: unknown): ModelStorageMode => MODES.includes(value as ModelStorageMode) ? value as ModelStorageMode : "unknown";
const missing: ModelStorage = { known: false, kind: "unknown", default_mode: null, table_counts: {}, tables: [], detail: "Storage modes have not been identified. Open the model in Power BI Desktop to confirm them." };

/** Read only the additive storage receipt; cached rows and model defaults cannot establish a table mode. */
export function readModelStorage(raw: unknown, status = "current"): ModelStorage {
  if (!record(raw)) return { ...missing, table_counts: {}, tables: [] };
  if (status !== "current" && !(status === "none" && raw.kind === "no_local_model")) return { ...missing, table_counts: {}, tables: [], detail: status === "reading" ? "Studio is reading the model. Storage modes are not confirmed yet." : missing.detail };
  const kind = KINDS.includes(raw.kind as ModelStorageKind) ? raw.kind as ModelStorageKind : "unknown";
  const known = raw.known === true && kind !== "unknown";
  const detail = typeof raw.detail === "string" && raw.detail.length <= 2000 ? raw.detail : missing.detail;
  const table_counts = Object.fromEntries(record(raw.table_counts) ? Object.entries(raw.table_counts).flatMap(([key, value]): [string, number][] => MODES.includes(key as ModelStorageMode) && typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? [[key, value]] : []) : []);
  const tables = Array.isArray(raw.tables) ? raw.tables.slice(0, 10000).filter(record).flatMap((table): TableStorage[] => {
    if (typeof table.name !== "string" || !table.name.trim() || table.name.length > 512) return [];
    const storage = mode(table.mode);
    const local = table.local_evaluation === "snapshot" && storage === "import" ? "snapshot" : table.local_evaluation === "power_bi" ? "power_bi" : "unknown";
    return [{ name: table.name, mode: storage, partition_modes: Array.isArray(table.partition_modes) ? [...new Set(table.partition_modes.map(mode))] : [], local_evaluation: local }];
  }) : [];
  return { known, kind: known ? kind : "unknown", default_mode: raw.default_mode == null ? null : mode(raw.default_mode), table_counts, tables, detail };
}

const LABELS: Record<ModelStorageKind, string> = { import: "Import", direct_query: "DirectQuery", dual: "Dual", hybrid: "Hybrid", direct_lake: "Direct Lake", mixed: "Mixed storage", unknown: "Unknown", no_local_model: "No local model" };
export const storageLabel = (value?: ModelStorageKind) => LABELS[value ?? "unknown"];
export const storageDescription = (value?: ModelStorageKind) => ({
  import: "Rows are stored in the model and updated by refresh. Studio can preview a saved local snapshot when one is available.",
  direct_query: "Power BI queries the source when needed. Studio uses the native model; a local snapshot does not reproduce this connection.",
  dual: "Power BI can use cached rows or query the source, depending on the query. Use the native model even when cached rows are available.",
  hybrid: "Import and DirectQuery partitions share a table. Cached rows can cover only its Import partitions; use the native model for the whole table.",
  direct_lake: "Power BI uses data in OneLake. Studio does not reproduce Direct Lake from a local snapshot.",
  mixed: "The tables use different storage modes. This summary identifies the observed modes; it does not identify every source group or connection.",
  unknown: "Storage is not confirmed. Local row availability does not identify the table's storage mode.",
  no_local_model: "This report has no local model. It may use a remote or live semantic model; confirm the connection in Power BI Desktop.",
})[value ?? "unknown"];

export function tableStorage(storage: ModelStorage | undefined, name: string): TableStorage | undefined {
  return storage?.tables.find(table => table.name.toLowerCase() === name.toLowerCase());
}
