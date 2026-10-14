import { useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { editTransport } from "./api";
import {
  createBusyStore, createEditingClient, driveJob, newerHead, desktopStage, isModelOperation, problemUi, stageLabel,
  type Decision, type DriveOutcome, type EditProblem, type EditReceipt, type EditRequest, type Head, type HistoryRow, type Job, type Operation, type ProblemUi,
} from "./importedEditing";
import { SerialQueue } from "./importedGeometry";

export type EditBusy = { label: string; stage?: string; desktop: boolean; since: number };
export type EditDialog = { problem: EditProblem; ui: ProblemUi };
type Pending = EditDialog & { resolve: (decision: Decision) => void };

const busyStore = createBusyStore<EditBusy>();

export type SubmitOptions = Partial<Omit<EditRequest, "operations" | "expected_head" | "idempotency_key">> & { label?: string };

/**
 * One editing session for an imported report: the head the client believes in, one request at a
 * time, the dialogs a conflict needs, and the receipts. All rules live in importedEditing.ts; this
 * hook only holds state and wires the transport.
 */
export function useImportedEditing({ workspaceId, head: payloadHead, desktopOpen, onReportChanged, onBusyChange, onApplied }: {
  workspaceId: string;
  head?: Head;
  /** Power BI Desktop has the working copy open, so a report edit has to close and reopen it. */
  desktopOpen: boolean;
  onReportChanged?: () => Promise<void> | void;
  onBusyChange?: (busy: boolean) => void;
  /** Called with the receipt (and the operations, for an edit) in the same tick as the head moves, so the canvas can pick a re-render kind for it. */
  onApplied?: (receipt: EditReceipt, operations: Operation[] | undefined) => void;
}) {
  const [head, setHeadState] = useState<Head | undefined>(payloadHead);
  const headRef = useRef<Head | undefined>(payloadHead);
  const [busy, setBusy] = useState<EditBusy | null>(null);
  const [dialog, setDialog] = useState<Pending | null>(null);
  const [lastReceipt, setLastReceipt] = useState<EditReceipt>();
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [historyState, setHistoryState] = useState<{ loading: boolean; error?: string }>({ loading: false });
  const queue = useRef(new SerialQueue());
  const busyRef = useRef<EditBusy | null>(null);
  const mounted = useRef(true);
  const changed = useRef(onReportChanged);
  changed.current = onReportChanged;
  const workspaceRef = useRef(workspaceId);
  workspaceRef.current = workspaceId;
  const busyChange = useRef(onBusyChange);
  busyChange.current = onBusyChange;
  const applied = useRef(onApplied);
  applied.current = onApplied;

  const instance = useId();
  const shared = useSyncExternalStore(busyStore.subscribe, () => busyStore.get(workspaceId));
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => () => busyStore.set(workspaceId, instance, null), [workspaceId, instance]);

  const setHead = useCallback((next: Head | undefined) => {
    const merged = newerHead(headRef.current, next);
    headRef.current = merged;
    setHeadState(merged);
  }, []);
  useEffect(() => { setHead(payloadHead); }, [payloadHead, setHead]);

  const updateBusy = useCallback((next: EditBusy | null) => {
    busyRef.current = next;
    if (mounted.current) setBusy(next);
    busyStore.set(workspaceRef.current, instance, next);
    busyChange.current?.(next !== null);
  }, [instance]);

  const client = useMemo(() => createEditingClient(editTransport, {
    onProgress: progress => {
      const current = busyRef.current;
      if (!current || (!progress.stage && !progress.label)) return;
      updateBusy({ ...current, stage: progress.stage ?? current.stage, label: stageLabel(progress.stage ?? current.stage, progress.label), desktop: current.desktop || desktopStage(progress.stage) });
    },
  }), [updateBusy]);

  const refreshHead = useCallback(async (actual?: string): Promise<string> => {
    try {
      const response = await editTransport("GET", `/v1/workspaces/${encodeURIComponent(workspaceId)}/imported-report`);
      const body = response.body as { head?: Head } | undefined;
      if (body?.head?.id) { headRef.current = body.head; setHeadState(body.head); }
    } catch { /* the id the server named is the best answer available */ }
    if (actual && headRef.current?.id !== actual) { headRef.current = { id: actual, hash: "" }; setHeadState(headRef.current); }
    await changed.current?.();
    return headRef.current?.id ?? actual ?? "";
  }, [workspaceId]);

  const decide = useCallback((problem: EditProblem, jobKind?: Job["kind"]) => new Promise<Decision>(resolve => {
    setDialog({ problem, ui: problemUi(problem, jobKind), resolve });
  }), []);
  const answerDialog = useCallback((decision: Decision) => {
    setDialog(current => { current?.resolve(decision); return null; });
  }, []);

  const run = useCallback((job: Job, label: string, expectsDesktop: boolean): Promise<DriveOutcome> => queue.current.run(async () => {
    updateBusy({ label, desktop: expectsDesktop, since: Date.now() });
    try {
      const outcome = await driveJob(client, workspaceId, job, {
        head: () => headRef.current?.id ?? "", decide, refreshHead,
      });
      if (outcome.status === "applied") {
        applied.current?.(outcome.receipt, job.kind === "edit" ? job.operations : undefined);
        setHead(outcome.receipt.head);
        if (mounted.current) setLastReceipt(outcome.receipt);
        await changed.current?.();
      }
      return outcome;
    } finally { updateBusy(null); }
  }), [client, decide, refreshHead, setHead, updateBusy, workspaceId]);

  const submit = useCallback((operations: Operation[], options: SubmitOptions = {}) => {
    const { label, ...rest } = options;
    const model = operations.some(isModelOperation);
    const job: Job = { kind: "edit", key: crypto.randomUUID(), operations, options: rest };
    return run(job, label ?? (model ? "Applying your changes in Power BI" : "Applying your change"), model || desktopOpen);
  }, [desktopOpen, run]);

  const undo = useCallback(() => run({ kind: "undo", key: crypto.randomUUID(), options: {} }, "Undoing the last change", desktopOpen), [desktopOpen, run]);
  const restore = useCallback((revisionId: string, confirmedDiscard: boolean) => run({ kind: "restore", key: crypto.randomUUID(), revisionId, confirmedDiscard, options: {} }, "Restoring an earlier version", desktopOpen), [desktopOpen, run]);

  const loadHistory = useCallback(async () => {
    setHistoryState({ loading: true });
    try {
      const rows = await client.history(workspaceId);
      if (mounted.current) { setHistory(rows); setHistoryState({ loading: false }); }
    } catch (reason) {
      if (mounted.current) setHistoryState({ loading: false, error: reason instanceof Error ? reason.message : "History could not be loaded." });
    }
  }, [client, workspaceId]);

  return {
    /** `activeBusy` also covers a change another tab of the same report is running: lock and show progress from this. */
    client, head, busy, activeBusy: busy ?? shared, dialog, answerDialog, lastReceipt, clearReceipt: () => setLastReceipt(undefined),
    submit, undo, restore, history, historyState, loadHistory, headRef, busyRef,
  };
}
export type ImportedEditing = ReturnType<typeof useImportedEditing>;
