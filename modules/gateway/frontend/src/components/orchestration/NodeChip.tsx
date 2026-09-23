/**
 * One executable node — story, eval, or gate (issue #4212).
 *
 * **Stalled vs halted vs running (AC-3).** All three need to be told apart, and
 * `state` alone cannot do it: a stall lands the node in `failed`, so `failed`
 * covers both "stuck, needs a human" and "broke". So there are two signals here —
 * the fill (from the five-state projection) and a **reason badge** that names the
 * specific condition. Stalled and halted share the amber-red fill because both
 * need intervention, and the badge is what separates them.
 *
 * **Current position (AC-3).** The node the work is at gets a ring and an
 * `aria-current`, so "where are we" is answerable without reading every chip.
 *
 * **Queued nodes name what blocks them.** "Queued" with no cause makes an
 * operator hunt the graph for the reason; the blocking predecessor's title is
 * right there in the chip.
 *
 * **Runs are not vertices** (§8.4). `attempts` renders as a count on the story
 * chip — a story that took four tries is one node with four attempts, not four
 * nodes. Rendering runs as vertices makes retry look like fan-out.
 */

import { DISPLAY_STATES, toDisplayState, isCurrentPosition } from '@/utils/nodeState';
import { CostFigureDisplay } from './CostFigureDisplay';
import { StoryJourney } from './StoryJourney';
import type { GraphNode } from '@/types/orchestration';
import { Link } from 'react-router-dom';

export interface NodeChipProps {
  node: GraphNode;
  /** Titles of unfinished predecessors, for the "waiting on" caption. */
  blockedBy?: string[];
  /** Direct dependencies, including completed steps, for inspecting the plan. */
  dependencies?: GraphNode[];
  /**
   * Decision controls for this node (issue #4213), passed as a slot rather than
   * imported here. This chip stays presentational: it has no permission check, no
   * mutation and no query client, so it keeps rendering identically for a caller
   * with no authority and in tests that never mount a provider.
   */
  controls?: React.ReactNode;
  /**
   * Delivery-ledger panel for this node (issue #5145), also a slot. Same reason as
   * `controls`: no query, no permission check, no client here.
   */
  execution?: React.ReactNode;
}

/**
 * The specific condition behind an intervention-needed fill, or null.
 *
 * `halted` and a stall are different news: a halt was a decision (budget, policy,
 * an operator), a stall is the engine giving up after retries. `rejected_at_gate`
 * is a third: a reviewer declined this attempt. Collapsing them into "failed"
 * loses the only information that tells an operator what to actually do.
 */
function reasonBadge(node: GraphNode): string | null {
  if (node.state === 'passed' || node.state === 'superseded') return null;
  if (node.state === 'halted') return 'Halted';
  if (node.stalled) return 'Stalled';
  if (node.state === 'rejected_at_gate') return 'Changes requested at gate';
  if (node.state === 'failed') return 'Failed';
  if (node.display_state === 'stalled') return 'Blocked';
  return null;
}

export function NodeChip({ node, blockedBy = [], dependencies, controls, execution }: NodeChipProps) {
  const display = toDisplayState(node);
  const current = isCurrentPosition(node);
  const badge = reasonBadge(node);
  const resultSummary = node.kind === 'story'
    ? node.result_summary?.replace(/^Agent finished\./, 'Development finished.')
    : node.result_summary;
  // Decision reasons carry an audit-source prefix, including when no note was
  // entered. Show the reviewer's text while preserving the stored audit value.
  const feedback = node.last_gate_decision?.reason
    ?.replace(/^\[input-path=(?:dashboard|github_comment)\](?:\s|$)/, '')
    .trim();

  // `superseded` and unknown states get no segment in the bar, and no fill here.
  const style = display ? DISPLAY_STATES[display] : null;
  const isQueued = display === 'queued';
  const waiting = dependencies?.filter((dependency) => toDisplayState(dependency) !== 'complete') ?? [];

  return (
    <li
      data-testid={`node-${node.node_ref}`}
      data-node-kind={node.kind}
      data-display-state={display ?? 'none'}
      data-current={current ? 'true' : undefined}
      aria-current={current ? 'step' : undefined}
      className={[
        'rounded-lg border p-3 text-left',
        // Dashed border for queued: "not yet real" is legible before any colour
        // is perceived, which also survives greyscale printing.
        isQueued ? 'border-dashed border-gray-400 dark:border-gray-600' : 'border-gray-200 dark:border-gray-700',
        current ? 'ring-2 ring-offset-1 ring-blue-500' : '',
      ].join(' ')}
    >
      <div className="flex items-start gap-2">
        {style && (
          <span
            aria-hidden="true"
            className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs leading-none"
            style={{ backgroundColor: style.fill, color: style.text }}
          >
            {style.glyph}
          </span>
        )}
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium text-gray-500 dark:text-gray-400">
            {node.kind === 'story' ? 'Implementation story' : node.kind === 'gate' ? 'Approval gate' : node.issue_ref?.trim() ? 'Evaluation story' : 'Evaluation checkpoint'}
          </p>
          <div className="flex items-start gap-2">
            <span className="min-w-0 break-words font-medium text-gray-900 dark:text-gray-100">{node.title}</span>
            {/* Pending tasks keep their issue number; a committed dispatch adds
                the verified repository URL and run link below. */}
            {node.issue_ref && (
              <span
                className="shrink-0 font-mono text-xs text-gray-500 dark:text-gray-400"
                data-testid={`node-issue-${node.node_ref}`}
              >
                #{String(node.issue_ref).replace(/^#/, '')}
              </span>
            )}
          </div>

          {/* The projected state, as text. The fill is a second channel, never
              the only one. */}
          {style && node.kind !== 'story' && <p className="mt-0.5 text-xs text-gray-600 dark:text-gray-400">{style.label}</p>}
          {node.kind === 'story' && <StoryJourney node={node} execution={execution} />}
          {node.configuration_problem && <p className="mt-1 text-sm text-amber-700">{node.configuration_problem}</p>}
          {resultSummary && !node.binding_hold && !node.delivery_progress && <p className="mt-1 text-sm">{resultSummary}</p>}
          <div className="mt-1 flex flex-wrap gap-3 text-xs">
            {node.issue_url && (
              <a href={node.issue_url} target="_blank" rel="noreferrer" className="text-blue-600 underline">View issue and evidence</a>
            )}
            {node.run_id && (
              <Link to={`/activity?id=${encodeURIComponent(node.run_id)}`} className="text-blue-600 underline">View run</Link>
            )}

          </div>

          {badge && (
            <p
              className="mt-1 inline-block rounded bg-orange-100 px-1.5 py-0.5 text-xs font-medium text-orange-900 dark:bg-orange-900/40 dark:text-orange-200"
              data-testid={`node-reason-${node.node_ref}`}
            >
              {badge}
            </p>
          )}

          {node.state === 'rejected_at_gate' && (
            <div className="mt-2 text-sm text-orange-900 dark:text-orange-200" data-testid="gate-feedback">
              <p>{node.last_gate_decision === undefined
                ? 'Refresh to load the recorded feedback.'
                : feedback || 'No change description was provided.'}</p>
              {node.last_gate_decision && (
                <p className="mt-1 text-xs">
                  Recorded {new Date(node.last_gate_decision.created_at).toLocaleString()}
                </p>
              )}
            </div>
          )}

          {dependencies && dependencies.length > 0 && (
            <details className="mt-2 rounded border border-gray-200 p-2 text-xs dark:border-gray-700">
              <summary
                className="cursor-pointer text-gray-600 dark:text-gray-400"
                data-testid={isQueued && waiting.length > 0 ? `node-blocked-by-${node.node_ref}` : undefined}
              >
                {isQueued && waiting.length > 0
                  ? waiting.length === 1 ? `Waiting on ${waiting[0].title}` : `Waiting on ${waiting.length} steps`
                  : `${dependencies.length} ${dependencies.length === 1 ? 'dependency' : 'dependencies'}`}
              </summary>
              <ul className="mt-2 space-y-2">
                {dependencies.map((dependency) => {
                  const state = toDisplayState(dependency);
                  return (
                    <li key={dependency.id}>
                      <span className="block font-medium">{dependency.title}</span>
                      <span className="text-gray-500 dark:text-gray-400">
                        {dependency.wave_ref} · {state ? DISPLAY_STATES[state].label : dependency.state}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </details>
          )}

          {!dependencies && isQueued && blockedBy.length > 0 && (
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400" data-testid={`node-blocked-by-${node.node_ref}`}>
              Waiting on {blockedBy.join(', ')}
            </p>
          )}

          <div className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
            <CostFigureDisplay figure={node.cost} />
            {/* §8.4: attempts are a property of this node, not extra nodes. */}
            {node.attempts > 1 && (
              <span className="text-gray-500 dark:text-gray-400" data-testid={`node-attempts-${node.node_ref}`}>
                {node.attempts} attempts
              </span>
            )}
          </div>

          {controls}
        </div>
      </div>
    </li>
  );
}

export default NodeChip;
