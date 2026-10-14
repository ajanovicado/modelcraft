// Imported reports are drawn by Power BI Desktop's own engine, through a private working
// copy. Desktop needs a minute to load, so the page waits and re-checks instead of
// showing an error the user must keep retrying.

export type DesktopWait = { since: number; opened: boolean };

export const OPENED_WAIT_MS = 180_000;
export const PASSIVE_WAIT_MS = 600_000;

/** The render failed only because the report's working copy is not open in Desktop yet. */
export function needsDesktop(message: string): boolean {
  return /power bi desktop/i.test(message) && /working copy|open this report/i.test(message);
}

/**
 * Milliseconds until the next check, or null to stop and offer "Check again".
 * Right after Studio asked Desktop to open, check often, then back off; otherwise check
 * slowly in case the user opens the report in Desktop themselves.
 */
export function nextCheckIn(wait: DesktopWait, now: number): number | null {
  const elapsed = now - wait.since;
  if (wait.opened) return elapsed > OPENED_WAIT_MS ? null : elapsed < 60_000 ? 3_000 : 6_000;
  return elapsed > PASSIVE_WAIT_MS ? null : 8_000;
}
