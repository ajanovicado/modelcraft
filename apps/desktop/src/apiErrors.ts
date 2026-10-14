/** Show useful validation messages without displaying raw request values or transport objects. */
export function apiErrorMessage(payload: unknown, status: number): string {
  const detail = payload && typeof payload === "object" && "detail" in payload ? payload.detail : undefined;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail.flatMap(issue => issue && typeof issue === "object" && typeof issue.msg === "string" ? [issue.msg] : []).slice(0, 3);
    if (messages.length) return `Some request details are invalid. ${messages.join(". ")}`;
  }
  if (status === 401 || status === 403) return "The local engine connection could not be verified. Restart the app and try again.";
  if (status === 409) return "The workspace changed while this request was running. Reload it and try again.";
  if (status === 422) return "Some request details are invalid. Check your entries and try again.";
  if (status === 503) return "The local engine is unavailable. Keep your question here and retry when it reconnects.";
  return "The local engine could not complete this request. Try again; if it keeps happening, restart the app.";
}
