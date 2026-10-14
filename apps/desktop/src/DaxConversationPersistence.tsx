import { useEffect, useState } from "react";
import { saveDaxConversationState, type DaxPersistenceFailure, type DaxRecoveryRequest, type DaxTurn } from "./daxConversation";

export function DaxConversationPersistence({ storageKey, turns, composerKey, composer, pendingKey, pending, durableHistory }: {
  storageKey: string; turns: DaxTurn[]; composerKey?: string; composer?: string; pendingKey?: string; pending?: DaxRecoveryRequest; durableHistory?: boolean;
}) {
  const [failures, setFailures] = useState<DaxPersistenceFailure[]>([]);
  useEffect(() => {
    try {
      setFailures(saveDaxConversationState(localStorage, { storageKey, turns, composerKey, composer, pendingKey, pending }));
    }
    catch { setFailures(["typed message", "request recovery", "chat copy"]); }
  }, [storageKey, turns, composerKey, composer, pendingKey, pending]);
  if (!failures.length) return null;
  const cacheOnly = durableHistory && failures.length === 1 && failures[0] === "chat copy";
  return <p className="dax-lab-hint dax-storage-status" role="status">{cacheOnly ? "This device could not cache the chat. Saved workspace responses remain available." : `This device could not save your ${failures.join(", ")}. Keep this tab open to retain them.`}</p>;
}
