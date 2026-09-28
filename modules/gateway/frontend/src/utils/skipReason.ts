/**
 * Human-readable rendering for `skip_reason` (issue #4020).
 *
 * A webhook delivery that produces no agent run gets a terminal status of
 * `no_op` (nothing asked for work), `blocked` (a loop/validation guard stopped
 * the spawn), or `skipped` (the worker deduplicated a redelivery). The reason
 * itself arrives as a static enum string written by the Lambda / worker.
 *
 * This module is the ONLY place that turns those enums into prose. The API
 * deliberately ships the raw enum rather than a sentence so the wording can
 * change without a backend deploy, and so old rows keep rendering.
 *
 * Two rules:
 *
 * 1. **Unknown reasons must degrade, not break.** Producers can add a new enum
 *    at any time and the UI ships on its own cadence, so an unmapped value is
 *    normal, not exceptional. We humanize it mechanically
 *    (`some_new_reason` → "Some new reason") instead of hiding it — a slightly
 *    awkward label still tells the operator more than a blank badge, which is
 *    the whole complaint #4020 was filed about.
 *
 * 2. **Never treat these strings as trusted markup.** They are rendered as text
 *    content only. The enum contract says reasons never interpolate payload
 *    data, but this layer does not get to assume the producer held that line.
 */

/**
 * Prose for every reason the Lambda and worker currently emit.
 *
 * Phrased from the operator's point of view — "why didn't my agent run" — not
 * from the code's. Keys mirror `lambda/common/skip_reasons.py` plus the
 * `SpawnResult.block_reason` strings that `spawn_persona` has always used.
 */
const SKIP_REASON_TEXT: Record<string, string> = {
  // --- Intent parsing: the delivery never asked for an agent -----------------
  no_mention: 'No agent was mentioned in this comment.',
  bot_mention_no_dispatch_marker:
    'A bot mentioned an agent but did not include the dispatch marker, so it was not treated as a request.',
  bot_dispatch_no_correlation:
    'A bot dispatch arrived without correlation context, so its chain could not be established.',
  label_unmapped: 'The label applied here is not mapped to any agent persona.',
  pr_branch_not_agent: 'This pull request is not on an agent branch, so no agent owns it.',
  pr_draft: 'This pull request is a draft. Review starts after the author marks it ready.',
  automatic_pr_review_disabled: 'Automatic pull request reviews are paused. Explicit agent requests remain available.',
  bot_synchronize_dedup:
    'A bot pushed to this branch — ignored to avoid re-running on the agent’s own commits.',
  no_aidlc_label: 'This issue does not carry an AIDLC label, so no agent was selected.',
  bot_event_ignored: 'This event came from a bot and is not a kind we act on.',
  installation_event:
    'This was a GitHub App installation event — bookkeeping only, no agent work implied.',
  event_type_unhandled: 'This webhook event type has no agent behaviour attached to it.',
  bot_comment_action_unhandled:
    'The agent edited or removed its own comment — ignored, no agent behaviour attached to this action.',

  // --- spawn_persona guards (block_reason strings, reused verbatim) ----------
  invalid_installation_id:
    'The GitHub App installation could not be identified, so the agent had no credentials to run with.',
  unknown_persona: 'The requested agent persona does not exist.',
  self_mention: 'The agent mentioned itself — ignored to prevent a run looping on its own output.',
  self_re_trigger:
    'The agent was re-triggered by its own activity — ignored to prevent an infinite loop.',
  cross_persona_loop:
    'Two agents were triggering each other — the loop guard stopped this dispatch.',
  chain_depth_exceeded:
    'This chain of agent-triggering-agent hit its depth limit and was stopped here.',

  // --- Worker-side dedup ----------------------------------------------------
  idempotency_merged_pr:
    'A merged pull request already exists for this work, so this duplicate delivery was skipped.',
};

/**
 * Fallback for enums this build does not know about: `foo_bar` → "Foo bar".
 *
 * Also guards the pathological case of a reason that is all separators, which
 * would otherwise render as an empty string and look like a UI bug.
 */
function humanizeUnknownReason(reason: string): string {
  const words = reason.replace(/[_-]+/g, ' ').trim();
  if (!words) return reason;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Turn a raw `skip_reason` into a sentence for display.
 *
 * Returns null when there is nothing to say (null/empty reason), so callers can
 * use it directly as a render guard.
 */
export function describeSkipReason(reason: string | null | undefined): string | null {
  if (!reason) return null;
  return SKIP_REASON_TEXT[reason] ?? humanizeUnknownReason(reason);
}

/**
 * Short label for tight spaces (badge tooltips, single-line row text).
 *
 * Strips the trailing period so it reads as a label rather than a sentence
 * fragment when appended after a status name.
 */
export function skipReasonLabel(reason: string | null | undefined): string | null {
  const described = describeSkipReason(reason);
  return described ? described.replace(/\.$/, '') : null;
}

/** Statuses for which a `skip_reason` is meaningful to display. */
export const NON_RUN_STATUSES = ['no_op', 'blocked', 'skipped'] as const;

/** Whether a status means "this delivery produced no agent run". */
export function isNonRunStatus(status: string | null | undefined): boolean {
  return !!status && (NON_RUN_STATUSES as readonly string[]).includes(status);
}
