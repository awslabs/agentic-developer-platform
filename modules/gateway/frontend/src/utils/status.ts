/**
 * The single invocation-status renderer (issue #4400).
 *
 * Before this module there were three `STATUS_CONFIG` maps: `pages/AgentActivity.tsx`,
 * `components/activity/ActivityCard.tsx`, and `components/InvocationDetail.tsx`.
 * They were byte-identical except for one label, and every status added since
 * (`blocked`/`skipped` in #4020, `budget_stopped` in #4187) had to be applied
 * three times — with three separate comments explaining the same colour choice.
 * `utils/liveness.ts` already noted the problem in its own header. The budget
 * drill-down (#4400) needs the same rendering, and a fourth copy is where the
 * maps would finally diverge unnoticed: a status added to the drill-down and not
 * to the board falls through to `?? no_op` and renders a real run as "✗ No-op".
 *
 * **Two label variants, deliberately.** `webhook_received` is "Webhook recv" in
 * a table cell and "Webhook received" in the detail modal — a real constraint
 * (the column is narrow, the modal is not), and one the modal has always
 * honoured. Rather than silently collapse it, the divergence is now a named
 * parameter: `describeStatus(status, 'compact' | 'full')`. Every other label is
 * identical in both variants, so the option is inert unless a status actually
 * needs a short form.
 *
 * **Unknown statuses degrade, they do not throw.** The API may add a status on
 * its own cadence and this SPA ships on another, so an unmapped value falls back
 * to the neutral `no_op` presentation with the raw value as its label — visible
 * and inert, rather than a crashed board or a fabricated "Complete". Same
 * fail-soft posture as `skipReason.ts` and `liveness.ts`.
 */

import type { InvocationStatus } from '@/types/activity';

/** How one status renders. */
export interface StatusPresentation {
  /** Decorative glyph — always paired with the text label, never the sole signal. */
  glyph: string;
  /** The label for the requested variant. */
  label: string;
  /** Tailwind classes for the text colour. */
  colorClass: string;
}

/**
 * Which label to use.
 *
 * `compact` for table cells and cards, `full` for the detail modal. They differ
 * for exactly one status today; see the module header.
 */
export type StatusLabelVariant = 'compact' | 'full';

interface StatusEntry {
  glyph: string;
  label: string;
  /** Set only where the full form differs from the compact one. */
  fullLabel?: string;
  colorClass: string;
}

/**
 * Presentation per status — the one copy.
 *
 * The colour register is meaningful and each departure from it is intentional:
 *
 * * `blocked`/`skipped` are neutral grey like `no_op`, NOT red (#4020). A guard
 *   that stopped a spawn and a worker that deduplicated a redelivery are both
 *   correct behaviour; styling them as errors sends operators to investigate
 *   nothing.
 * * `budget_stopped` is amber, not red (#4187). The cap worked as configured, so
 *   this reads as "needs a budget decision", not "something is broken" — and
 *   with the budget drill-down (#4400) it is now the status an operator is most
 *   likely to click through from.
 * * `aborted` is amber for the same reason (#3964): a human stopped the run on
 *   purpose. Red would report an operator's own intervention as a fault.
 */
const STATUS_CONFIG: Record<InvocationStatus, StatusEntry> = {
  webhook_received: {
    glyph: '∘',
    label: 'Webhook recv',
    // The table column cannot fit the full word; the modal can, and has always
    // shown it. See the module header.
    fullLabel: 'Webhook received',
    colorClass: 'text-gray-500 dark:text-gray-400',
  },
  in_progress: { glyph: '●', label: 'In progress', colorClass: 'text-blue-600 dark:text-blue-400' },
  complete: { glyph: '✓', label: 'Complete', colorClass: 'text-green-600 dark:text-green-400' },
  failed: { glyph: '✗', label: 'Failed', colorClass: 'text-red-600 dark:text-red-400' },
  rejected: { glyph: '✗', label: 'Rejected', colorClass: 'text-orange-600 dark:text-orange-400' },
  rate_limited: { glyph: '✗', label: 'Rate limited', colorClass: 'text-yellow-600 dark:text-yellow-400' },
  no_op: { glyph: '✗', label: 'No-op', colorClass: 'text-gray-500 dark:text-gray-400' },
  blocked: { glyph: '✗', label: 'Blocked', colorClass: 'text-gray-500 dark:text-gray-400' },
  skipped: { glyph: '✗', label: 'Skipped', colorClass: 'text-gray-500 dark:text-gray-400' },
  budget_stopped: { glyph: '⊘', label: 'Budget stopped', colorClass: 'text-amber-600 dark:text-amber-400' },
  // Issue #3964: amber like `budget_stopped`, not red. Someone deliberately
  // stopped this run, so it reads as "a decision was taken here", not "something
  // broke" — styling it as an error would send operators to investigate their own
  // intervention. Distinct glyph from `budget_stopped` (■ = stopped by hand, ⊘ =
  // refused by a cap) because the two are different answers to "why did this end?"
  // and the colour alone cannot separate them.
  aborted: { glyph: '■', label: 'Aborted', colorClass: 'text-amber-600 dark:text-amber-400' },
};

/**
 * Look up how to render a status.
 *
 * Never returns null and never throws — an unrecognised value gets the neutral
 * `no_op` styling with the raw value as its label, so a status this build has
 * not heard of is visibly inert rather than mislabelled as something it isn't.
 * `null`/`undefined`/`''` render as "Unknown" for the same reason: the absence
 * is shown, not guessed at.
 */
export function describeStatus(
  status: InvocationStatus | string | null | undefined,
  variant: StatusLabelVariant = 'compact',
): StatusPresentation {
  const entry = status ? STATUS_CONFIG[status as InvocationStatus] : undefined;
  if (!entry) {
    const fallback = STATUS_CONFIG.no_op;
    return { glyph: fallback.glyph, label: status || 'Unknown', colorClass: fallback.colorClass };
  }
  return {
    glyph: entry.glyph,
    label: variant === 'full' ? (entry.fullLabel ?? entry.label) : entry.label,
    colorClass: entry.colorClass,
  };
}

/**
 * Just the label — for `aria-label` strings and other text-only contexts.
 *
 * Kept separate so those call sites do not have to destructure a presentation
 * object they only need one field of.
 */
export function statusLabel(
  status: InvocationStatus | string | null | undefined,
  variant: StatusLabelVariant = 'compact',
): string {
  return describeStatus(status, variant).label;
}

/** Every status this build knows how to render. Exported for tests and filters. */
export const KNOWN_STATUSES = Object.keys(STATUS_CONFIG) as InvocationStatus[];
