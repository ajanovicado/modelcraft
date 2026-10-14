import { Database } from "lucide-react";
import { storageDescription, storageLabel, type ModelStorage, type ModelStorageKind, type TableStorage } from "./modelStorage";
import "./model-storage.css";

export function StorageModeBadge({ mode, title }: { mode?: ModelStorageKind; title?: string }) {
  return <span className={`storage-mode-badge storage-${mode ?? "unknown"}`} title={title ?? storageDescription(mode)}>{storageLabel(mode)}</span>;
}

/** A read-only model receipt. Snapshot availability is kept separate from native storage modes. */
export function ModelStorageSummary({ storage, cachedTables = [] }: { storage?: ModelStorage; cachedTables?: string[] }) {
  const kind = storage?.known ? storage.kind : "unknown";
  const counts = Object.entries(storage?.table_counts ?? {}).filter(([, count]) => count > 0);
  const locallySaved = new Set(cachedTables.map(name => name.toLowerCase()));
  return <details className="model-storage-summary">
    <summary><Database size={14} aria-hidden="true"/><strong>Model storage</strong><StorageModeBadge mode={kind}/>{counts.length > 0 && <span className="storage-counts">{counts.map(([mode, count]) => `${count} ${storageLabel(mode as ModelStorageKind)}`).join(" · ")}</span>}</summary>
    <div className="model-storage-content"><p>{storage?.detail || storageDescription(kind)}</p>
      <p className="storage-snapshot-note">{cachedTables.length ? `${cachedTables.length} ${cachedTables.length === 1 ? "table has" : "tables have"} saved local rows. These rows are separate from the native model and do not prove complete model coverage.` : "No saved local rows are available. Storage metadata does not require a local data snapshot."}</p>
      {storage?.tables.length ? <div className="storage-table-scroll"><table><thead><tr><th scope="col">Table</th><th scope="col">Storage</th><th scope="col">Saved local rows</th><th scope="col">Evaluation</th></tr></thead><tbody>{storage.tables.map(table => <tr key={table.name}><td>{table.name}</td><td><StorageModeBadge mode={table.mode}/>{table.mode === "hybrid" && <small>Import + DirectQuery partitions</small>}</td><td>{locallySaved.has(table.name.toLowerCase()) ? table.mode === "hybrid" ? "Import portion only" : "Available snapshot" : "Unavailable"}</td><td>{table.local_evaluation === "snapshot" ? "Saved snapshot when available" : table.local_evaluation === "power_bi" ? "Power BI native model" : "Not confirmed"}</td></tr>)}</tbody></table></div> : null}
      <details className="storage-mode-help"><summary>What the storage modes mean</summary><dl>{(["import", "direct_query", "dual", "hybrid", "direct_lake", "no_local_model"] as const).map(mode => <div key={mode}><dt>{storageLabel(mode)}</dt><dd>{storageDescription(mode)}</dd></div>)}</dl><p>Storage modes and connections are changed in Power BI Desktop. This view does not convert tables or create connections.</p></details>
    </div>
  </details>;
}

export function TableStorageNote({ table, cachedRows }: { table?: TableStorage; cachedRows?: number }) {
  return <div className="table-storage-note"><div><span>Storage</span><StorageModeBadge mode={table?.mode}/></div><p>{storageDescription(table?.mode)}</p><small>{cachedRows === undefined ? "No saved local rows are available for this table." : `${cachedRows.toLocaleString()} rows in the local snapshot.${table?.mode === "hybrid" ? " This can cover only the Import portion." : table?.mode === "dual" ? " Cached rows do not reproduce Dual query behavior." : " This does not establish complete native model coverage."}`}</small></div>;
}
