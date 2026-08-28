/**
 * Human-readable rendering for `stop_reason` (issue #4187).
 *
 * A run the gateway stopped because a spend cap was exhausted gets the terminal
 * status `budget_stopped` and a static enum saying which cap it was. That is
 * deliberately not `failed`: nothing malfunctioned, a configured limit did its
 * job, and rendering it as an error sends operators to debug a run that behaved
 * correctly.
 *
 * Same two rules as `skipReason.ts`, for the same reasons: an unknown enum is
 * humanized rather than hidden (producers ship on a different cadence than this
 * UI), and these strings are rendered as text content only.
 */

/**
 * Prose for every stop reason the worker currently emits.
 *
 * Phrased from the operator's point of view — "why did my agent stop early" —
 * and each one names the remedy, because "out of budget" without "who raises it"
 * is a dead end for the person reading it.
 */
const STOP_REASON_TEXT: Record<string, string> = {
  run_cap_exceeded:
    'This run reached its per-run spend cap and was stopped. Its work so far is preserved in the transcript.',
  chain_cap_exceeded:
    'This run and the runs it spawned together reached the per-chain spend cap, so the chain was stopped.',
  root_user_cap_exceeded:
    'Everything you have set in motion this period — this run and the agents it spawned — reached your personal spend budget, so this run was stopped. An administrator can raise your budget.',
  hierarchy_cap_exceeded:
    'Your organization, team, or user budget is exhausted, so this run was stopped. An administrator can raise it.',
  budget_cap_exceeded: 'A spend cap was reached, so this run was stopped.',
};

/**
 * Fallback for enums this build does not know about: `foo_bar` → "Foo bar".
 *
 * Also guards a reason that is all separators, which would otherwise render as
 * an empty string and look like a UI bug.
 */
function humanizeUnknownReason(reason: string): string {
  const words = reason.replace(/[_-]+/g, ' ').trim();
  if (!words) return reason;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Turn a raw `stop_reason` into a sentence for display.
 *
 * Returns null when there is nothing to say, so callers can use it directly as a
 * render guard.
 */
export function describeStopReason(reason: string | null | undefined): string | null {
  if (!reason) return null;
  return STOP_REASON_TEXT[reason] ?? humanizeUnknownReason(reason);
}

/** Whether a status means "a spend cap stopped this run". */
export function isBudgetStoppedStatus(status: string | null | undefined): boolean {
  return status === 'budget_stopped';
}
