/**
 * LivenessBadge — renders the three-value liveness verdict (issue #4176).
 *
 * One component shared by the table, the card, and the detail modal, so the
 * three surfaces cannot drift in how they express "we could not ask". It renders
 * *beside* the status badge, never in place of it.
 *
 * Renders nothing when the verdict is absent or unrecognised — pre-#4176 rows
 * carry no verdict, and a future backend may add values this build has not seen.
 * A missing badge is correct in both cases; a crash or an invented verdict is not.
 */

import type { LivenessVerdict } from '@/types/activity';
import { describeLiveness } from '@/utils/liveness';

export interface LivenessBadgeProps {
  verdict: LivenessVerdict | string | null | undefined;
  /**
   * When true, only render the badge if the verdict needs attention
   * (`unverifiable`). Used on dense surfaces like the table, where labelling
   * every healthy run "Live" would be noise that buries the one row that is not.
   */
  attentionOnly?: boolean;
  /** Optional test id suffix (usually the invocation id). */
  testIdSuffix?: string;
}

export function LivenessBadge({ verdict, attentionOnly = false, testIdSuffix }: LivenessBadgeProps) {
  const config = describeLiveness(verdict);
  if (!config) return null;
  if (attentionOnly && verdict !== 'unverifiable') return null;

  return (
    <span
      className={`inline-flex items-center gap-1 text-xs font-medium px-1.5 py-0.5 rounded ${config.colorClass}`}
      title={config.description}
      data-testid={testIdSuffix ? `liveness-badge-${verdict}-${testIdSuffix}` : `liveness-badge-${verdict}`}
    >
      <span aria-hidden="true">{config.glyph}</span>
      <span>{config.label}</span>
      {/* The tooltip is hover-only; the full sentence must also reach keyboard
          and screen-reader users. Same pattern as the #4020 skip-reason badge. */}
      <span className="sr-only">: {config.description}</span>
    </span>
  );
}

export default LivenessBadge;
