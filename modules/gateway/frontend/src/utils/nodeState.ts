/**
 * The nine→five state projection (issue #4212).
 *
 * The engine has nine states (`state.py`); operators are shown **five**
 * (design-contract §1.3). This module is the only place that mapping exists, so
 * the segmented rollup bar, the node chips, and the legend cannot drift apart —
 * three copies of a colour table is how a legend ends up describing a fill the
 * graph no longer uses.
 *
 * **The five are a closed vocabulary.** Labels, glyphs, and fills below are
 * verbatim from §1.3 and are not open to local adjustment; the contract is
 * normative and binding on this issue.
 *
 * **`rejected` and `skipped` do not appear here — that is asserted by a test.**
 * They are phantom states from an earlier draft; the engine raises `ValueError`
 * on both (R-N2c). A glyph map containing either would advertise a state the
 * backend cannot produce, and operators would eventually see a legend entry that
 * never lights up.
 *
 * Two engine states needed a ruling, recorded here because the contract's table
 * covers the five outputs rather than all nine inputs:
 *
 *   - **`rejected_at_gate` → `stalled`.** A real engine state (a gate declined an
 *     attempt), not the banned phantom `rejected`. It needs a human to move it,
 *     which is exactly what "Stalled — needs help" says. Note the substring trap:
 *     `rejected_at_gate` *contains* "rejected", so the no-phantom-state test must
 *     match whole keys, not substrings.
 *   - **`superseded` → no segment.** A superseded attempt was replaced by another
 *     node; counting it would double-count one piece of work in the rollup bar,
 *     making the totals exceed the real node count.
 */

import type { GraphNode, NodeEngineState } from '@/types/orchestration';

/** The five user-facing states. This is the whole vocabulary (§1.3). */
export type DisplayState = 'complete' | 'in_progress' | 'gate' | 'stalled' | 'queued';

export interface DisplayStateStyle {
  /** Verbatim §1.3 label. Shown to operators; also the accessible name. */
  label: string;
  /** Verbatim §1.3 glyph. Never the sole carrier of meaning — always paired. */
  glyph: string;
  /** Verbatim §1.3 background fill. */
  fill: string;
  /**
   * Verbatim §1.3 text colour. Amber (`#fab219`) and grey (`#c3c2b7`) carry
   * **dark** text: white on either fails contrast, and the contract mandates the
   * dark pairing rather than leaving it to each call site.
   */
  text: string;
}

/**
 * §1.3, verbatim. Declaration order is also **render order** — §9.4 requires the
 * rollup bar's `aria-label` enumerate states in the order they visually appear,
 * so a screen-reader user and a sighted user describe the same bar the same way.
 * Ordered intent→done: queued → in progress → gate → stalled → complete.
 */
export const DISPLAY_STATES: Record<DisplayState, DisplayStateStyle> = {
  queued: {
    label: 'Queued (waiting on dependencies)',
    glyph: '○',
    fill: '#c3c2b7',
    text: '#52514e',
  },
  in_progress: {
    label: 'In progress',
    glyph: '▶',
    fill: '#2a78d6',
    text: '#fff',
  },
  gate: {
    label: 'Waiting on a gate',
    glyph: '🚦',
    fill: '#fab219',
    text: '#5c4200',
  },
  stalled: {
    label: 'Stalled — needs help',
    glyph: '⚠',
    fill: '#ec835a',
    text: '#fff',
  },
  complete: {
    label: 'Complete',
    glyph: '✓',
    fill: '#0ca30c',
    text: '#fff',
  },
};

/** Render order for the bar and the legend. See the note on `DISPLAY_STATES`. */
export const DISPLAY_STATE_ORDER: DisplayState[] = ['queued', 'in_progress', 'gate', 'stalled', 'complete'];

/**
 * Project one node onto its display state.
 *
 * `stalled` is checked **before** `state`, and that ordering is the point of AC-3.
 * A stall leaves the node in `failed` with a `node_stalled` decision beside it, so
 * reading `state` first renders every stall as a plain failure and erases the
 * distinction between "needs a human" and "broke". A halt stays `stalled`-styled
 * too — both need intervention — but the two are told apart by the badge the node
 * chip renders, not by collapsing them here.
 */
export function toDisplayState(node: Pick<GraphNode, 'state' | 'stalled'>): DisplayState | null {
  if (node.stalled) return 'stalled';
  return engineStateToDisplayState(node.state);
}

/**
 * The raw nine→five map. Returns `null` for states that occupy no segment.
 *
 * Exported for the rollup counter, which must skip `superseded` rather than
 * bucket it somewhere. Prefer `toDisplayState` for anything rendering a node —
 * this function cannot see the stall flag.
 */
export function engineStateToDisplayState(state: NodeEngineState): DisplayState | null {
  switch (state) {
    case 'passed':
      return 'complete';
    case 'running':
      return 'in_progress';
    case 'awaiting_gate':
      return 'gate';
    case 'failed':
    case 'halted':
    case 'rejected_at_gate':
      return 'stalled';
    case 'pending':
    case 'ready':
      return 'queued';
    case 'superseded':
      // Replaced by another attempt. Counting it would double-count one piece of
      // work and push the rollup's totals past the real node count.
      return null;
    default:
      // The API and the SPA deploy independently, so an unfamiliar state is a
      // normal transient, not a bug. Omitting it from the bar understates
      // progress; inventing a sixth colour would break the closed vocabulary.
      return null;
  }
}

/**
 * Whether this node is where the work currently is (AC-3: "current position").
 *
 * Running and gate-waiting both count: a flow parked on a gate has its position
 * *at* that gate, and an operator asking "where are we" needs that answered
 * whether or not a model is burning tokens at that instant.
 */
export function isCurrentPosition(node: Pick<GraphNode, 'state' | 'stalled'>): boolean {
  if (node.stalled) return false;
  return node.state === 'running' || node.state === 'awaiting_gate';
}

/** Count nodes per display state, skipping those that occupy no segment. */
export function countByDisplayState(nodes: GraphNode[]): Record<DisplayState, number> {
  const counts: Record<DisplayState, number> = {
    queued: 0,
    in_progress: 0,
    gate: 0,
    stalled: 0,
    complete: 0,
  };
  for (const node of nodes) {
    const display = toDisplayState(node);
    if (display) counts[display] += 1;
  }
  return counts;
}
