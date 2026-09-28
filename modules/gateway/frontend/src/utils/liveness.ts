/**
 * Rendering for the three-value liveness verdict (issue #4176).
 *
 * The backend derives `live` / `unverifiable` / `exited` per run and ships the
 * raw enum. This module is the ONLY place that turns it into something visible,
 * so the four independent `STATUS_CONFIG` maps on this page do not each invent
 * their own wording for it.
 *
 * Three rules:
 *
 * 1. **The verdict is shown BESIDE the status, never instead of it.** They answer
 *    different questions — "what did the run last say" versus "do we still
 *    believe it". Replacing one with the other loses information.
 *
 * 2. **`unverifiable` must never read as finished.** It means we could not learn
 *    whether the run is alive. Wording it as "dead", "stopped", or "failed"
 *    reintroduces the exact bug the field was added to prevent: an operator (or
 *    later, an automation) concluding a merely-unreachable run has ended and
 *    starting a second agent on the same issue.
 *
 * 3. **Unknown values degrade, they do not throw.** The backend may add verdicts
 *    on its own cadence and this UI ships on another. An unmapped value renders
 *    nothing at all rather than crashing the board — the same fail-soft posture
 *    as `skipReason.ts` and the `?? STATUS_CONFIG.no_op` fallbacks.
 */

import type { LivenessVerdict } from '@/types/activity';

interface LivenessPresentation {
  /** Short badge text. */
  label: string;
  /** Tailwind classes for the badge. */
  colorClass: string;
  /** Decorative glyph — always paired with the text label, never the sole signal. */
  glyph: string;
  /** Full sentence for tooltips and screen readers. */
  description: string;
}

/**
 * Presentation per verdict.
 *
 * `live` and `exited` are deliberately understated: they are the ordinary cases
 * and already legible from the status badge next to them. Only `unverifiable`
 * is styled to draw the eye, because it is the one case the board could not
 * previously express at all — an amber "warning" register rather than a red
 * "error" one, since an unverifiable run may well be perfectly healthy.
 */
const LIVENESS_CONFIG: Record<LivenessVerdict, LivenessPresentation> = {
  live: {
    label: 'Live',
    glyph: '◉',
    colorClass: 'text-blue-700 dark:text-blue-400 bg-blue-50 dark:bg-blue-900/20',
    description: 'Recent signal — this run is reporting as alive.',
  },
  unverifiable: {
    label: 'Unverifiable',
    glyph: '?',
    colorClass: 'text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-900/20',
    description:
      'No recent signal and no observed ending — we cannot confirm whether this run is still going. ' +
      'This is not a claim that it stopped.',
  },
  exited: {
    label: 'Exited',
    glyph: '◌',
    colorClass: 'text-gray-600 dark:text-gray-400 bg-gray-100 dark:bg-gray-700/40',
    description: 'This run reported a final outcome — its ending was observed.',
  },
};

/**
 * Look up the presentation for a verdict.
 *
 * Returns null for null, undefined, and any value this build does not recognise
 * — callers render nothing in that case. Pre-#4176 rows carry no verdict, and
 * "no opinion" is the honest rendering of that; inventing one would be worse.
 */
export function describeLiveness(
  verdict: LivenessVerdict | string | null | undefined,
): LivenessPresentation | null {
  if (!verdict) return null;
  return LIVENESS_CONFIG[verdict as LivenessVerdict] ?? null;
}

/**
 * Whether a verdict warrants operator attention.
 *
 * True only for `unverifiable`. Used to decide whether to surface the badge
 * prominently; `live` and `exited` are unremarkable.
 */
export function isAttentionWorthy(verdict: LivenessVerdict | string | null | undefined): boolean {
  return verdict === 'unverifiable';
}

/** Filter options for the liveness dropdown. Empty value = no filtering. */
export const LIVENESS_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'Any liveness' },
  { value: 'live', label: 'Live' },
  { value: 'unverifiable', label: 'Unverifiable' },
  { value: 'exited', label: 'Exited' },
];
