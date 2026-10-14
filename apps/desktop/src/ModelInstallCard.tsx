import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from "react";
import { CheckCircle2, Download, HardDrive, LoaderCircle, Pause, ShieldCheck } from "lucide-react";
import { api } from "./api";
import "./model-install.css";

export type ModelInstallState =
  | "idle" | "missing" | "unavailable" | "starting" | "copying" | "downloading" | "verifying"
  | "installed" | "damaged" | "failed" | "cancelled";

export type ModelInstallStatus = {
  state: ModelInstallState;
  installed: boolean;
  running: boolean;
  model?: string | null;
  source?: string | null;
  bytes_done?: number;
  bytes_total?: number | null;
  bytes_per_second?: number;
  download_available: boolean;
  local_copy?: string | null;
  error?: string | null;
  target?: string | null;
};

type Source = "auto" | "download" | "file";

const WORKING: ModelInstallState[] = ["starting", "copying", "downloading", "verifying"];
const ACTIVITY: Partial<Record<ModelInstallState, string>> = {
  starting: "Starting",
  copying: "Copying Qwen 4B from this PC",
  downloading: "Downloading Qwen 4B",
  verifying: "Checking Qwen 4B before use",
};

export function formatGb(bytes?: number | null): string {
  return bytes == null ? "3.4 GB" : `${(bytes / 1e9).toFixed(1)} GB`;
}

export function ModelInstallPanel({ status, busy, onStart, onCancel }: {
  status: ModelInstallStatus;
  busy: boolean;
  onStart: (source: Source, path?: string) => void;
  onCancel: () => void;
}) {
  const [path, setPath] = useState("");
  const titleId = useId();
  const pathId = useId();
  if (status.state === "unavailable") return null;
  const working = status.running || WORKING.includes(status.state);
  const checkingExisting = status.source === "existing";
  const damaged = status.state === "damaged";
  const total = status.bytes_total ?? undefined;
  const done = status.bytes_done ?? 0;
  const percent = total ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;
  const speed = status.state === "downloading" && status.bytes_per_second
    ? ` · ${(status.bytes_per_second / 1e6).toFixed(1)} MB/s` : "";
  const submitFile = (event: FormEvent) => {
    event.preventDefault();
    if (!busy && !working && path.trim()) onStart("file", path.trim());
  };
  if (status.installed && !working) return (
    <section className="model-install model-install-ready" aria-labelledby={titleId}>
      <div className="model-install-head">
        <CheckCircle2 size={20} aria-hidden="true" />
        <div><h2 id={titleId}>Local AI is ready</h2><p>Qwen 4B is checked and ready to use. Your questions and model responses stay on this PC.</p></div>
      </div>
    </section>
  );
  return (
    <section className="model-install" aria-labelledby={titleId} aria-busy={busy || working}>
      <div className="model-install-head">
        <Download size={20} aria-hidden="true" />
        <div>
          <h2 id={titleId}>{damaged ? "Repair local AI" : checkingExisting && working ? "Checking your local AI model" : "Install Qwen 4B"}</h2>
          <p>
            {damaged ? "The model file could not be verified. Install a checked replacement to use local AI again. Your workspaces and reports are kept."
              : checkingExisting && working ? "We are checking the model on this PC before enabling local AI. This can take a few minutes."
                : `Set up Qwen 4B once for your Windows account (${formatGb(total)}). It runs on this PC and is checked before use.`}
          </p>
        </div>
      </div>
      {working ? (
        <div className="model-install-progress">
          {total ? <div className="model-install-bar" role="progressbar" aria-label={ACTIVITY[status.state] ?? "Installing Qwen 4B"} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><span style={{ width: `${percent}%` }} /></div> : null}
          <span role="status">
            <LoaderCircle className="spin" size={14} aria-hidden="true" />
            {ACTIVITY[status.state] ?? "Installing"}{total ? ` · ${formatGb(done)} of ${formatGb(total)} (${percent}%)${speed}` : "…"}
          </span>
          <button type="button" className="secondary" onClick={onCancel} disabled={busy}>
            <Pause size={15} aria-hidden="true" />{checkingExisting ? "Cancel check" : "Pause"}
          </button>
        </div>
      ) : (
        <div className="model-install-actions">
          {(status.state === "failed" || damaged) && status.error && <p className="model-install-error" role="alert">{status.error}</p>}
          {status.state === "cancelled" && <p>{checkingExisting ? "Model check cancelled. Check the model before using local AI." : status.source === "download" ? "Paused. Downloaded progress is kept so you can continue." : "Setup paused. Try again when you are ready."}</p>}
          {status.state === "cancelled" && checkingExisting ? (
            <button type="button" className="primary" onClick={() => onStart("auto")} disabled={busy}>Check model</button>
          ) : status.local_copy ? (
            <>
              <button type="button" className="primary" onClick={() => onStart("auto")} disabled={busy}>
                <HardDrive size={15} aria-hidden="true" />{damaged ? "Repair from this PC" : "Install from this PC (no download)"}
              </button>
              {status.download_available && (
                <button type="button" className="secondary" onClick={() => onStart("download")} disabled={busy}>Download instead</button>
              )}
            </>
          ) : status.download_available ? (
            <button type="button" className="primary" onClick={() => onStart("download")} disabled={busy}>
              <Download size={15} aria-hidden="true" />
              {busy ? "Starting…" : damaged ? `Download and repair (${formatGb(total)})` : status.state === "cancelled" && status.source === "download" ? "Resume download" : status.state === "cancelled" || status.state === "failed" ? "Try again" : `Download and install (${formatGb(total)})`}
            </button>
          ) : (
            <p>Choose the Qwen 4B model file below to install it.</p>
          )}
          <p className="model-install-note">
            <ShieldCheck size={14} aria-hidden="true" />
            Model downloads come from Ollama's registry. Your questions and reports are not sent with the download.
          </p>
        </div>
      )}
      {!working && (
        <details className="model-install-file">
          <summary>I already have the model file</summary>
          <form onSubmit={submitFile}>
            <label htmlFor={pathId}>Full path to the Qwen 4B file</label>
            <input id={pathId} value={path} onChange={event => setPath(event.target.value)} disabled={busy}
              placeholder="D:\Models\sha256-81fb60c7…" spellCheck={false} />
            <button type="submit" className="secondary" disabled={busy || !path.trim()}>Install this file</button>
          </form>
        </details>
      )}
    </section>
  );
}

export function ModelInstallCard({ onInstalled, onStateChange }: { onInstalled?: () => void; onStateChange?: (state: ModelInstallState | undefined) => void }) {
  const [status, setStatus] = useState<ModelInstallStatus>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [actionError, setActionError] = useState<string>();
  const mounted = useRef(false);
  const currentStatus = useRef<ModelInstallStatus | undefined>(undefined);
  const onInstalledRef = useRef(onInstalled);
  const onStateChangeRef = useRef(onStateChange);
  const commandPending = useRef(false);
  const generation = useRef(0);
  const readController = useRef<AbortController | undefined>(undefined);
  const commandController = useRef<AbortController | undefined>(undefined);
  useEffect(() => { onInstalledRef.current = onInstalled; }, [onInstalled]);
  useEffect(() => { onStateChangeRef.current = onStateChange; }, [onStateChange]);

  const acceptStatus = useCallback((next: ModelInstallStatus) => {
    const previous = currentStatus.current;
    currentStatus.current = next;
    setStatus(next);
    onStateChangeRef.current?.(next.state);
    if (next.installed && !next.running) setActionError(undefined);
    if (previous && !previous.installed && next.installed && !next.running) onInstalledRef.current?.();
  }, []);

  const refresh = useCallback(async () => {
    if (!mounted.current || readController.current || commandPending.current) return;
    const controller = new AbortController();
    readController.current = controller;
    const expectedGeneration = generation.current;
    try {
      const next = await api<ModelInstallStatus>("/v1/model/install", { signal: controller.signal });
      if (!mounted.current || expectedGeneration !== generation.current) return;
      acceptStatus(next);
      setError(undefined);
    } catch (reason) {
      if (controller.signal.aborted || !mounted.current || expectedGeneration !== generation.current) return;
      setError(reason instanceof Error ? reason.message : "Could not read the model install status.");
    } finally {
      if (readController.current === controller) readController.current = undefined;
    }
  }, [acceptStatus]);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => {
      mounted.current = false;
      onStateChangeRef.current?.(undefined);
      generation.current += 1;
      readController.current?.abort();
      readController.current = undefined;
      commandController.current?.abort();
    };
  }, [refresh]);
  const working = Boolean(status && (status.running || WORKING.includes(status.state)));
  useEffect(() => {
    if (!working) return;
    let stopped = false;
    let timer: number;
    const poll = async () => {
      await refresh();
      if (!stopped) timer = window.setTimeout(() => void poll(), 1000);
    };
    timer = window.setTimeout(() => void poll(), 1000);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [working, refresh]);

  const run = async (path: string, body?: object) => {
    if (commandPending.current || !mounted.current) return;
    commandPending.current = true;
    const expectedGeneration = ++generation.current;
    readController.current?.abort();
    readController.current = undefined;
    const controller = new AbortController();
    commandController.current = controller;
    setBusy(true);
    setActionError(undefined);
    try {
      const next = await api<ModelInstallStatus>(path, { method: "POST", body: JSON.stringify(body ?? {}), signal: controller.signal });
      if (!mounted.current || generation.current !== expectedGeneration) return;
      acceptStatus(next);
      setError(undefined);
    } catch (reason) {
      if (controller.signal.aborted || !mounted.current || generation.current !== expectedGeneration) return;
      setActionError(reason instanceof Error ? reason.message : "The model install could not start.");
    } finally {
      commandPending.current = false;
      if (commandController.current === controller) commandController.current = undefined;
      if (mounted.current && generation.current === expectedGeneration) { setBusy(false); void refresh(); }
    }
  };

  if (!status) return error ? <section className="model-install"><p className="model-install-error" role="alert">{error}</p><button type="button" className="secondary" onClick={() => void refresh()}>Check setup again</button></section>
    : <section className="model-install"><span className="model-install-note" role="status"><LoaderCircle className="spin" size={15} aria-hidden="true" />Checking local AI setup…</span></section>;
  return (
    <>
      <ModelInstallPanel status={status} busy={busy}
        onStart={(source, path) => void run("/v1/model/install", { source, ...(path ? { path } : {}) })}
        onCancel={() => void run("/v1/model/install/cancel")} />
      {error && <p className="model-install-error" role="alert">{error}</p>}
      {actionError && <p className="model-install-error" role="alert">{actionError}</p>}
    </>
  );
}
