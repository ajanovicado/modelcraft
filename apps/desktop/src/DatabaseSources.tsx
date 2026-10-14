import { useEffect, useRef, useState } from "react";
import { Database, RefreshCw } from "lucide-react";
import { api, idempotencyKey } from "./api";
import type { DatabaseSnapshotSource, Workspace } from "./types";

type ConnectionDriver = { id: string; name: string; installed: boolean; ready?: boolean; detail?: string };
type SourceTable = { name: string; kind: string };
type Sample = { name: string; columns: string[]; rows: (string | null)[][] };
type ImportRequest = { workspace_id: string; source: { provider: string; profile: string; schema_name: string }; tables: string[]; max_rows: number; idempotency_key: string; expected_base_hash: string };
type Operation = { abort: AbortController; scope: string };

export function DatabaseSources({ workspace, busy, existingSource, onImported, onImportBusy }: { workspace?: Workspace; busy: boolean; existingSource?: DatabaseSnapshotSource; onImported: () => Promise<void>; onImportBusy: (value: boolean) => void }) {
  const [provider, setProvider] = useState("snowflake");
  const [profile, setProfile] = useState("");
  const [schema, setSchema] = useState("PUBLIC");
  const [search, setSearch] = useState("");
  const [drivers, setDrivers] = useState<ConnectionDriver[]>([]);
  const [driverAttempt, setDriverAttempt] = useState(0);
  const [checkingDrivers, setCheckingDrivers] = useState(true);
  const [driverError, setDriverError] = useState("");
  const [tables, setTables] = useState<SourceTable[]>();
  const [selected, setSelected] = useState<string[]>([]);
  const [more, setMore] = useState(false);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [rowLimit, setRowLimit] = useState(100000);
  const [sample, setSample] = useState<Sample>();
  const [review, setReview] = useState(false);
  const [importSucceeded, setImportSucceeded] = useState(false);
  const [restoredSource, setRestoredSource] = useState(false);
  const operation = useRef<Operation | undefined>(undefined);
  const importAttempt = useRef<ImportRequest | undefined>(undefined);
  const reviewPanel = useRef<HTMLDivElement>(null);
  const mounted = useRef(true);
  const source = { provider, profile: profile.trim(), schema_name: schema.trim() };
  const currentScope = useRef("");
  currentScope.current = JSON.stringify([workspace?.id, workspace?.base_hash, source]);
  const locked = busy || Boolean(pending);
  const driver = drivers.find(value => value.id === provider);
  const canConnect = !checkingDrivers && !driverError && driver?.ready === true;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; operation.current?.abort.abort(); };
  }, []);
  useEffect(() => {
    if (review) { reviewPanel.current?.scrollIntoView({ block: "nearest" }); reviewPanel.current?.focus({ preventScroll: true }); }
  }, [review]);
  useEffect(() => {
    const abort = new AbortController();
    setCheckingDrivers(true); setDriverError(""); setDrivers([]);
    api<ConnectionDriver[]>("/v1/connections/drivers", { signal: abort.signal })
      .then(value => { if (!abort.signal.aborted) setDrivers(value); })
      .catch(reason => { if (!abort.signal.aborted) setDriverError(reason instanceof Error ? reason.message : "Connection prerequisites could not be checked."); })
      .finally(() => { if (!abort.signal.aborted) setCheckingDrivers(false); });
    return () => abort.abort();
  }, [driverAttempt]);
  useEffect(() => {
    operation.current?.abort.abort(); operation.current = undefined; importAttempt.current = undefined;
    setPending(""); setTables(undefined); setSelected([]); setSample(undefined); setReview(false);
    setMore(false); setSearch(""); setError(""); setImportSucceeded(false); setRestoredSource(false);
  }, [workspace?.id, workspace?.base_hash]);

  const reset = () => {
    operation.current?.abort.abort(); operation.current = undefined; importAttempt.current = undefined;
    setPending(""); setTables(undefined); setSelected([]); setSample(undefined); setReview(false);
    setMore(false); setSearch(""); setError(""); setImportSucceeded(false); setRestoredSource(false);
  };
  const run = async (label: string, action: (signal: AbortSignal, current: () => boolean) => Promise<void>) => {
    if (busy || operation.current) return;
    const request: Operation = { abort: new AbortController(), scope: currentScope.current };
    operation.current = request;
    const current = () => mounted.current && operation.current === request && !request.abort.signal.aborted && currentScope.current === request.scope;
    setPending(label); setError("");
    try { await action(request.abort.signal, current); }
    catch (reason) { if (current()) setError(reason instanceof Error ? reason.message : "Connection request failed."); }
    finally {
      if (operation.current === request) { operation.current = undefined; if (mounted.current) setPending(""); }
    }
  };
  const browse = () => run("Connecting and listing tables…", async (signal, current) => {
    if (!canConnect || !source.profile || !source.schema_name) return;
    setTables(undefined); setMore(false); setSample(undefined); setSelected([]); setReview(false);
    setImportSucceeded(false); setRestoredSource(false); importAttempt.current = undefined;
    const value = await api<{ tables: SourceTable[]; has_more: boolean }>("/v1/connections/browse", { method: "POST", signal, body: JSON.stringify({ source, search }) });
    if (current()) { setTables(value.tables); setMore(value.has_more); }
  });
  const preview = (name: string) => run(`Previewing ${name}…`, async (signal, current) => {
    setSample(undefined);
    const value = await api<Omit<Sample, "name">>("/v1/connections/preview", { method: "POST", signal, body: JSON.stringify({ source, table: name }) });
    if (current()) setSample({ ...value, name });
  });
  const importTables = () => run("Importing and profiling a new snapshot…", async (signal, current) => {
    if (!workspace || !selected.length || importSucceeded || !canConnect) return;
    const request = importAttempt.current ?? { workspace_id: workspace.id, source, tables: [...selected], max_rows: rowLimit, idempotency_key: idempotencyKey(), expected_base_hash: workspace.base_hash };
    importAttempt.current = request;
    onImportBusy(true);
    try {
      await api("/v1/connections/import", { method: "POST", signal, body: JSON.stringify(request) });
      if (!current()) return;
      importAttempt.current = undefined; setImportSucceeded(true); setReview(false);
      await onImported();
    } finally { onImportBusy(false); }
  });
  const refreshImported = () => run("Loading the imported snapshot…", async (_signal, current) => { if (current()) await onImported(); });
  const restoreSource = () => {
    if (locked || !existingSource) return;
    reset();
    setProvider(existingSource.provider); setProfile(existingSource.profile); setSchema(existingSource.schema_name);
    setSelected([...existingSource.tables]); setTables(existingSource.tables.map(name => ({ name, kind: "Saved import selection" })));
    setRowLimit(existingSource.max_rows ?? 100000); setReview(true); setRestoredSource(true);
  };

  return <section className="database-sources" aria-label="Database connections">
    <header><Database size={20}/><div><h3>Connect to a database</h3><p>Browse tables, inspect a preview, then import a bounded local snapshot.</p></div>{existingSource && <button className="secondary" disabled={locked} onClick={restoreSource}>Import again</button>}</header>
    <div className="database-connection-form">
      <label>Source<select value={provider} disabled={locked} onChange={event => { setProvider(event.target.value); setSchema(event.target.value === "snowflake" ? "PUBLIC" : "dbo"); setProfile(""); reset(); }}><option value="snowflake">Snowflake</option><option value="sqlserver">SQL Server / Azure SQL</option></select></label>
      <label>{provider === "snowflake" ? "Connection profile" : "ODBC data source (DSN)"}<input value={profile} disabled={locked} placeholder={provider === "snowflake" ? "Profile in connections.toml" : "Configured system or user DSN"} onChange={event => { setProfile(event.target.value); reset(); }} maxLength={120}/></label>
      <label>Schema<input value={schema} disabled={locked} onChange={event => { setSchema(event.target.value); reset(); }} maxLength={128}/></label>
      <button className="secondary" disabled={locked || !source.profile || !source.schema_name || !canConnect} onClick={() => void browse()}><RefreshCw size={15}/>Connect & browse</button>
    </div>
    <div className="database-readiness">
      {checkingDrivers ? <p role="status">Checking connection prerequisites…</p> : driverError ? <p role="alert" className="database-error">{driverError}</p> : <p role="status">{driver?.detail || "Connection readiness is unavailable. Restart the local engine and check again."}{canConnect && " Connect & browse checks your profile, sign-in and schema permissions."}</p>}
      <button className="secondary" disabled={locked || checkingDrivers} onClick={() => setDriverAttempt(value => value + 1)}><RefreshCw size={14}/>{driverError ? "Retry connection check" : "Check prerequisites"}</button>
    </div>
    <details className="database-setup"><summary>Connection setup</summary><p>{provider === "snowflake" ? "Use a named Snowflake connection profile on this computer. The profile supplies your account, database, warehouse, role and authentication, including browser SSO or key-pair authentication. Use a role with read access to the selected tables." : "Use a configured SQL Server ODBC DSN with authentication and certificate validation managed by your organization. The DSN selects the server and database. A database principal with read-only permissions is recommended."}</p><p>Credentials are handled by the database driver and are never sent to the AI model or saved in workspace chat.</p><a href={provider === "snowflake" ? "https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-connect" : "https://learn.microsoft.com/en-us/sql/connect/odbc/windows/odbc-administrator-dsn-creation"} target="_blank" rel="noreferrer">Official connection setup</a></details>
    {!workspace && <p className="source-mode-note">Choose or create a workspace to import a snapshot. You can check the connection and preview tables first.</p>}
    {pending && <p role="status">{pending}</p>}
    {error && <p role="alert" className="database-error">{error}</p>}
    {importSucceeded && <div className="database-import-success" role="status"><p>The snapshot was imported. Load it before starting another import.</p><button className="secondary" disabled={locked} onClick={() => void refreshImported()}>Load imported snapshot</button></div>}
    {tables && <>
      <div className="database-table-tools"><label>Find a table<input placeholder="Search this schema…" value={search} disabled={locked} maxLength={100} onChange={event => setSearch(event.target.value)}/></label><button className="secondary" disabled={locked || !canConnect} onClick={() => void browse()}>Search</button><span>{tables.length} {tables.length === 1 ? "table" : "tables"}{more ? " · More available; refine your search" : ""}</span></div>
      {restoredSource && <p className="source-mode-note">Saved connection and table selection restored. No connection or query has run. Import snapshot reconnects and reads these tables after your confirmation.</p>}
      <div className="database-table-list">{tables.map(table => <div key={table.name}><label><input type="checkbox" checked={selected.includes(table.name)} disabled={locked || importSucceeded || (!selected.includes(table.name) && selected.length >= 10)} onChange={event => { setSelected(event.target.checked ? [...selected, table.name] : selected.filter(name => name !== table.name)); setReview(false); importAttempt.current = undefined; }}/><strong>{table.name}</strong><small>{table.kind}</small></label><button className="secondary" disabled={locked || !canConnect} onClick={() => void preview(table.name)}>Preview</button></div>)}{!tables.length && <p>No tables found. Check the schema and read permissions.</p>}</div>
      {sample && <section className="database-sample"><h4>{sample.name} · First 50 rows, up to 40 columns</h4><div className="data-grid-scroll"><table className="workbench-grid"><thead><tr>{sample.columns.map(column => <th key={column}>{column}</th>)}</tr></thead><tbody>{sample.rows.map((row, index) => <tr key={index}>{row.map((value, column) => <td key={column}>{value ?? "null"}</td>)}</tr>)}</tbody></table></div></section>}
      <div className="database-import-controls"><label>Maximum rows per table<select disabled={locked || importSucceeded} value={rowLimit} onChange={event => { setRowLimit(Number(event.target.value)); setReview(false); importAttempt.current = undefined; }}><option value={100000}>100,000</option><option value={500000}>500,000</option><option value={1000000}>1,000,000</option></select></label><button className="primary" disabled={locked || !workspace || !selected.length || importSucceeded || !canConnect} onClick={() => setReview(true)}>Review import · {selected.length} {selected.length === 1 ? "table" : "tables"}</button></div>
      {review && <div className="database-import-review" role="region" aria-label="Review database import" tabIndex={-1} ref={reviewPanel}><h4>Import {selected.length} {selected.length === 1 ? "table" : "tables"} into {workspace?.name}</h4><p>{selected.join(", ")}. Maximum {rowLimit.toLocaleString()} rows per table. Tables over the limit fail instead of being silently truncated.</p><p>This creates a new active snapshot and clears the active report draft. Previous snapshots, drafts and builds remain in the evidence store. Database queries may consume warehouse resources.</p><button className="secondary" disabled={locked} onClick={() => setReview(false)}>Back</button><button className="primary" disabled={locked || !workspace || !canConnect || importSucceeded} onClick={() => void importTables()}>Import snapshot</button></div>}
    </>}
  </section>;
}
