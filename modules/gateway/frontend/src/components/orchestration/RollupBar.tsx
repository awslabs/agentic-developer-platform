/**
 * The segmented progress bar — "how much is left", at a glance (issue #4212).
 *
 * Widths are proportions of the **whole** journey, pending nodes included (AC-1).
 * A bar over only what has already run always reads as near-complete, which is
 * the specific failure this view exists to fix.
 *
 * **Accessibility (§9.4).** The bar is `role="img"` with a prose `aria-label`
 * enumerating the states **in the order they render**, so a screen-reader user and
 * a sighted user describe the same bar the same way. Order comes from
 * `DISPLAY_STATE_ORDER`, the same constant the segments map over — a hand-written
 * label is exactly what drifts when someone reorders the segments.
 *
 * Colour is never the sole carrier of meaning: every segment also has its glyph
 * and count in the legend beneath, and amber/grey use the contract's dark text
 * rather than white, which fails contrast on both.
 */

import { DISPLAY_STATES, DISPLAY_STATE_ORDER, type DisplayState } from '@/utils/nodeState';

export interface RollupBarProps {
  counts: Record<DisplayState, number>;
  /** Total segmented nodes. Pass explicitly so `superseded` exclusions are visible. */
  total: number;
  stories?: { complete: number; total: number };
  storyScope?: 'all' | 'implementation';
}

export function RollupBar({ counts, total, stories, storyScope = 'all' }: RollupBarProps) {
  const present = DISPLAY_STATE_ORDER.filter((state) => counts[state] > 0);

  // Built from the same ordered array the segments render from (§9.4).
  const description =
    total === 0
      ? 'No work items to show yet.'
      : `${total} work ${total === 1 ? 'item' : 'items'}: ` +
        present.map((state) => `${counts[state]} ${DISPLAY_STATES[state].label}`).join(', ') +
        '.';

  return (
    <div className="space-y-2">
      {stories && stories.total > 0 && (
        <p className="text-sm font-medium" data-testid="story-completion-count">
          {stories.complete} of {stories.total} {storyScope === 'implementation' ? 'implementation ' : ''}stories complete
        </p>
      )}
      <p className="text-xs text-gray-600 dark:text-gray-400">
        All {total} work items, including implementation stories, evaluations and approval gates
      </p>
      <div
        role="img"
        aria-label={description}
        data-testid="rollup-bar"
        className="flex h-4 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700"
      >
        {present.map((state) => (
          <div
            key={state}
            data-testid={`rollup-segment-${state}`}
            data-count={counts[state]}
            // `aria-hidden`: the parent's label already narrates every segment, so
            // exposing these would read the same figures twice.
            aria-hidden="true"
            style={{
              width: `${(counts[state] / total) * 100}%`,
              backgroundColor: DISPLAY_STATES[state].fill,
            }}
          />
        ))}
      </div>

      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs" data-testid="rollup-legend">
        {DISPLAY_STATE_ORDER.map((state) => (
          <li key={state} className="flex items-center gap-1.5" data-testid={`legend-${state}`}>
            <span
              aria-hidden="true"
              className="inline-flex h-4 w-4 items-center justify-center rounded-full text-[10px] leading-none"
              style={{ backgroundColor: DISPLAY_STATES[state].fill, color: DISPLAY_STATES[state].text }}
            >
              {DISPLAY_STATES[state].glyph}
            </span>
            <span className="text-gray-700 dark:text-gray-300">
              {DISPLAY_STATES[state].label}
              <span className="ml-1 font-semibold tabular-nums">{counts[state]}</span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default RollupBar;
