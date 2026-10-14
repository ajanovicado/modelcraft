import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { editTransport } from "./api";
import {
  addEditCard, createEditingClient, editCardsFromEvents, foldChatAnswer, lastChatClear, mergeEditCards, pendingStore, queueableConfirm, readChatAnswer,
  type ChatEditEvent, type DriveOutcome, type EditCard, type Operation,
} from "./importedEditing";
import { suggestionBody, type ReviewFinding } from "./judgement";
import { judgementClient as judgement } from "./judgementApi";
import type { EditProgressState } from "./EditChatCards";

/**
 * The imported-report edit events of a chat turn (docs/imported-report-editing-api.md, "Chat"): progress
 * while the batch runs, receipts, the planner's plan, questions and confirmations, what Studio noticed after
 * an edit, explanations of numbers and suggested tests. After a receipt the report payload is re-read so the
 * canvas moves to the new head. A question is answered with one POST; the reply is a receipt (shown as a new
 * card) or a plain message. `events` is the workspace's activity log: the receipts of earlier chat turns are
 * read back from it, so a reload does not lose them.
 */
export function useChatEdits({ workspaceId, headId, refresh, undo, submit, events }: {
  workspaceId?: string; headId?: string;
  refresh: () => Promise<void> | void;
  /** Undo the newest change through the report editor, which owns the dialogs a conflict needs. */
  undo: () => Promise<DriveOutcome> | undefined;
  /** Applies operations through the report editor (a finding's fix), with its dialogs. */
  submit?: (operations: Operation[], label: string) => Promise<DriveOutcome> | undefined;
  events?: { sequence: number; type: string; payload: Record<string, unknown> }[];
}) {
  const [session, setCards] = useState<EditCard[]>([]);
  const [progress, setProgress] = useState<EditProgressState>();
  const [working, setWorking] = useState(false);
  // Whether the current chat turn changed the report (the "Explanation only" note is only for turns that changed nothing).
  const [appliedThisTurn, setAppliedThisTurn] = useState(false);
  // "New chat" starts the receipts over too: what the log held at that moment is not shown again.
  const [clearedAfter, setClearedAfter] = useState(0);
  const counter = useRef(0);
  const client = useMemo(() => createEditingClient(editTransport), []);
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const newestEvent = useRef(0);
  newestEvent.current = events?.reduce((latest, event) => Math.max(latest, event.sequence), 0) ?? 0;
  // The log remembers "New chat" too, so a reload does not bring the cleared conversation's receipts back.
  const cutoff = useMemo(() => Math.max(clearedAfter, lastChatClear(events ?? [])), [events, clearedAfter]);
  const recorded = useMemo(() => editCardsFromEvents((events ?? []).filter(event => event.sequence > cutoff)), [events, cutoff]);
  const cards = useMemo(() => mergeEditCards(recorded, session), [recorded, session]);

  useEffect(() => { setCards([]); setProgress(undefined); setClearedAfter(0); setAppliedThisTurn(false); }, [workspaceId]);

  const nextId = () => `edit-card-${++counter.current}`;
  const onEditEvent = useCallback((event: ChatEditEvent) => {
    if (event.type === "progress") { setProgress({ stage: event.stage, label: event.label }); return; }
    setCards(current => addEditCard(current, event, nextId()));
    if (event.type === "applied") { setAppliedThisTurn(true); void refreshRef.current(); }
  }, []);
  const finish = useCallback(() => setProgress(undefined), []);
  const beginTurn = useCallback(() => setAppliedThisTurn(false), []);

  const update = (id: string, change: Partial<EditCard> | ((card: EditCard) => EditCard)) =>
    setCards(current => current.map(card => card.id !== id ? card : typeof change === "function" ? change(card) : ({ ...card, ...change } as EditCard)));
  const failed = (message: string) => setCards(current => addEditCard(current, { type: "failed", message }, nextId()));

  // The answer is folded in like the chat stream: every event it carries becomes a card (a choice can lead to a
  // confirmation with Apply and Cancel, a plan, a receipt...), and the reply text shows under the answered card.
  const settled = (reply: ReturnType<typeof readChatAnswer>) => Boolean(reply.receipt) || reply.events.some(event => event.type === "applied");
  const answer = async (card: EditCard, send: () => ReturnType<typeof client.answerChoice>, answered: (reply: ReturnType<typeof readChatAnswer>) => string) => {
    if (!workspaceId) return;
    setWorking(true);
    try {
      const reply = readChatAnswer(await send());
      const label = answered(reply);
      setCards(current => foldChatAnswer(current, card.id, label, reply, nextId).cards);
      if (!reply.problem && settled(reply)) {
        setAppliedThisTurn(true);
        await refreshRef.current();
      }
    } finally { setWorking(false); }
  };

  return {
    onEditEvent, finish, beginTurn, appliedThisTurn,
    reset: () => { setCards([]); setProgress(undefined); setAppliedThisTurn(false); setClearedAfter(newestEvent.current); },
    panel: {
      cards, progress, headId, disabled: working,
      onUndo: (card: EditCard) => {
        const outcome = undo();
        if (outcome) void outcome.then(result => { if (result.status === "applied") update(card.id, { undone: true } as Partial<EditCard>); });
      },
      onChoice: (card: EditCard, index: number) => {
        if (card.type !== "choices" || !workspaceId) return;
        const label = card.event.choices.find(choice => choice.index === index)?.label ?? "";
        void answer(card, () => client.answerChoice(workspaceId, card.event.pendingId, index), () => label);
      },
      // A plan's Apply and Cancel answer its pending question like any confirmation (one revision for every step). A yes
      // that led to another question (Desktop has unsaved work, say) is "accepted", not "applied".
      onConfirm: (card: EditCard, accept: boolean) => {
        if ((card.type !== "confirm" && card.type !== "plan") || !workspaceId) return;
        void answer(card, () => client.answerConfirm(workspaceId, card.event.pendingId, accept), reply => !accept ? "cancelled" : settled(reply) ? "applied" : "Accepted. See below for what comes next.");
      },
      // "Add to pending changes": the measure changes wait with the others, with the what-if answer shown beside them.
      onQueue: (card: EditCard) => {
        const queue = queueableConfirm(card);
        if (!queue || !workspaceId) return;
        queue.ops.forEach((op, index) => pendingStore.add(workspaceId, op, index === 0 && queue.whatIf ? { whatIf: queue.whatIf } : {}));
        update(card.id, { answered: "queued" } as Partial<EditCard>);
      },
      onFix: submit ? (card: EditCard, finding: ReviewFinding) => {
        if (!finding.fix?.length) return;
        const outcome = submit(finding.fix, finding.fixSummary ?? "Fixing what Studio noticed");
        if (outcome) void outcome.then(result => {
          if (result.status === "applied") update(card.id, current => ({ ...current, fixed: [...((current as { fixed?: string[] }).fixed ?? []), finding.id] }) as EditCard);
        });
      } : undefined,
      onAddSuggestion: (card: EditCard, index: number) => {
        if (card.type !== "suggestions" || !workspaceId) return;
        const item = card.event.suggestions.tests[index];
        const body = item ? suggestionBody(item.raw) : undefined;
        if (!item || !body) return;
        setWorking(true);
        judgement.addTest(workspaceId, body)
          .then(() => update(card.id, current => ({ ...current, added: [...((current as { added?: string[] }).added ?? []), item.test.id] }) as EditCard))
          .catch(reason => failed(reason instanceof Error ? reason.message : "The test could not be added."))
          .finally(() => setWorking(false));
      },
    },
  };
}
