export type EditHistory<T> = { past: T[]; present: T; future: T[]; group?: string };
export type HistoryAction<T> =
  | { type: "edit"; value: T | ((current: T) => T); group?: string }
  | { type: "replace"; value: T }
  | { type: "undo" } | { type: "redo" } | { type: "end-group" };

/** Bounded, local edit history. A new saved draft starts a new history. */
export function editHistory<T>(state: EditHistory<T>, action: HistoryAction<T>): EditHistory<T> {
  if (action.type === "replace") return { past: [], present: action.value, future: [] };
  if (action.type === "end-group") return { ...state, group: undefined };
  if (action.type === "undo") {
    if (!state.past.length) return state;
    return { past: state.past.slice(0, -1), present: state.past.at(-1)!, future: [state.present, ...state.future] };
  }
  if (action.type === "redo") {
    if (!state.future.length) return state;
    return { past: [...state.past, state.present].slice(-50), present: state.future[0], future: state.future.slice(1) };
  }
  const next = typeof action.value === "function" ? (action.value as (value: T) => T)(state.present) : action.value;
  if (JSON.stringify(next) === JSON.stringify(state.present)) return state;
  return {
    past: action.group && action.group === state.group ? state.past : [...state.past, state.present].slice(-50),
    present: next, future: [], group: action.group,
  };
}
