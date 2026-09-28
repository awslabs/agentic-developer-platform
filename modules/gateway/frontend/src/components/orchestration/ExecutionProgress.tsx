/**
 * Delivery execution progress and blocks for one node (issue #5145).
 *
 * **This is not a second delivery journey.** It renders *inside* the existing
 * `StoryJourney`, beneath the stage list, because the ledger answers a question
 * that view already raises but cannot: the journey shows a story sitting in
 * "Development status unconfirmed" and this says *why* and *who acts next*. A
 * parallel component would drift from the journey's stage vocabulary, and the
 * divergence would only become visible once live.
 *
 * Read-only. Approving a gate or resuming a node stays with `GateControls` and the
 * existing controls surface — there is no mutating verb on the endpoint behind
 * this, and none of the copy here implies one. `remaining_gates` is shown as
 * information, and the operator acts through the gate controls.
 */

import type { EvaluationCorrectionSummary, EvaluationEvidenceSummary, ExecutionSummary } from '@/types/orchestration';
import {
  absentExecutionNote,
  executionPresentation,
  type ExecutionTone,
  type NodeAcceptance,
} from '@/utils/executionProgress';

const TONE_CLASSES: Record<ExecutionTone, string> = {
  // Attention is amber, never red: blocked is not failed, and a red panel sends an
  // operator looking for a crash that never happened.
  attention: 'text-amber-800 dark:text-amber-200',
  complete: 'text-green-800 dark:text-green-300',
  active: 'text-blue-800 dark:text-blue-300',
  pending: 'text-gray-700 dark:text-gray-300',
  unknown: 'text-gray-600 dark:text-gray-400',
};

export interface ExecutionProgressProps {
  /** The node this execution belongs to; used only for stable test hooks. */
  nodeRef: string;
  /** Newest cycle for this node, or null when the ledger has no row for it. */
  execution: ExecutionSummary | null;
  /** The response's own `server_time` — never a browser clock. */
  serverTime: string;
  /** True when the whole flow predates the ledger. Drives the absence wording. */
  legacy: boolean;
  /** Earlier cycles for this node, counted so a retry is not shown as the first try. */
  earlierCycles?: number;
  /**
   * The **graph's** verdict on this node (`state === 'passed'`), which is the only
   * thing that can make this panel read as complete. Omitted or `undefined` means not
   * known, and renders as acceptance pending — never as delivered. See
   * `executionPresentation`: the ledger records what was attempted, the graph records
   * what was accepted.
   */
  nodeAccepted?: NodeAcceptance;
}

export function ExecutionProgress({
  nodeRef,
  execution,
  serverTime,
  legacy,
  earlierCycles = 0,
  nodeAccepted,
}: ExecutionProgressProps) {
  if (!execution) {
    // Absence is stated, not omitted. Rendering nothing would let the journey's
    // stage list stand alone and read as a complete account of delivery.
    const { headline, tone } = absentExecutionNote(legacy);
    return (
      <p
        className={`mt-2 text-xs ${TONE_CLASSES[tone]}`}
        data-testid={`execution-absent-${nodeRef}`}
        data-tone={tone}
        data-legacy={legacy ? 'true' : 'false'}
      >
        {headline}
      </p>
    );
  }

  const view = executionPresentation(execution, serverTime, earlierCycles, nodeAccepted);
  const evaluations = execution.actions.filter((action) =>
    action.kind === 'evaluation_evidence' && action.status === 'succeeded' &&
    action.evidence_summary && Array.isArray(action.evidence_summary.criteria) &&
    typeof action.evidence_summary.mandatory_passed === 'boolean'
  );
  const corrections = execution.actions.filter((action) =>
    ['evaluation_correction_issue', 'evaluation_context'].includes(action.kind) &&
    action.evidence_summary && 'evaluation_cycle' in action.evidence_summary && 'stage' in action.evidence_summary &&
    typeof action.evidence_summary.evaluation_cycle === 'number' &&
    ['creation_pending', 'creation_unresolved', 'delivery_pending', 'retest_requested'].includes(String(action.evidence_summary.stage))
  );

  return (
    <section
      className="mt-3 rounded-lg border border-gray-200 p-2 dark:border-gray-700"
      aria-label="Delivery execution"
      data-testid={`execution-progress-${nodeRef}`}
      data-tone={view.tone}
      data-phase={execution.phase}
      data-status={execution.status}
      // The revision the panel is currently showing, so a stale-overwrite
      // regression is observable from the DOM rather than only from the hook.
      data-revision={execution.revision}
    >
      <p className={`text-xs font-medium ${TONE_CLASSES[view.tone]}`} data-testid={`execution-headline-${nodeRef}`}>
        {view.headline}
      </p>

      {view.blockOwner && (
        <p className="mt-1 text-xs text-gray-700 dark:text-gray-300" data-testid={`execution-block-${nodeRef}`}>
          {/* Owner and required input together: a status without a next step is
              what sends an operator to logs that expire. */}
          <span className="font-medium">{view.blockOwner}</span>
          {view.blockRequiredInput ? <> · {view.blockRequiredInput}</> : null}
        </p>
      )}

      {view.remainingGates.length > 0 && (
        <p className="mt-1 text-xs text-amber-800 dark:text-amber-200" data-testid={`execution-gates-${nodeRef}`}>
          Outstanding approvals: {view.remainingGates.join(', ')}
        </p>
      )}

      <dl className="mt-1 flex flex-wrap gap-x-4 text-xs text-gray-600 dark:text-gray-400">
        {view.lastProgress && (
          <div data-testid={`execution-progressed-${nodeRef}`}>
            <dt className="inline">Last progress: </dt>
            <dd className="inline">{view.lastProgress}</dd>
          </div>
        )}
        {view.nextCheck && (
          <div data-testid={`execution-next-check-${nodeRef}`}>
            <dt className="inline">Next check: </dt>
            <dd className="inline">{view.nextCheck}</dd>
          </div>
        )}
        {execution.stage_attempts ? Object.entries(execution.stage_attempts).map(([stage, count]) => (
          <div key={stage}>
            <dt className="inline capitalize">{stage} attempts: </dt>
            <dd className="inline">{count}</dd>
          </div>
        )) : <div>
          <dt className="inline">Total continuation attempts: </dt>
          <dd className="inline">{execution.attempts}</dd>
        </div>}
      </dl>

      {view.evidence.length > 0 && (
        <ul className="mt-2 space-y-1" aria-label="Delivery evidence" data-testid={`execution-evidence-${nodeRef}`}>
          {view.evidence.map((item, index) => (
            <li
              // `operation_key` would be the natural key, but two prepared attempts
              // of one operation share it; the action id is unique per row.
              key={execution.actions[index].id}
              className="text-xs text-gray-700 dark:text-gray-300"
              data-kind={item.kind}
              data-resolved={item.resolved ? 'true' : 'false'}
            >
              <span className="font-medium capitalize">{item.label}</span>: {item.detail}
              {item.reference && (
                // Rendered as text, not a link. The server sanitises references
                // fail-closed, but these are provider identifiers (a PR node id, an
                // S3 key) rather than URLs — building an href from one would guess
                // at a host, and a guessed link is worse than a copyable id.
                <>
                  {' '}
                  <span className="font-mono text-gray-600 dark:text-gray-400">{item.reference}</span>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {evaluations.map((action) => {
        const evidence = action.evidence_summary as EvaluationEvidenceSummary;
        return (
          <div key={action.id} className="mt-2 text-xs" aria-label="Evaluation criteria">
            <p>{evidence.mandatory_passed ? 'Required criteria passed' : 'Required criteria failed; correction required'}</p>
            <p>Release: <span className="font-mono">{evidence.actual_revision}</span></p>
            <p>Harness: <span className="font-mono">{evidence.harness_revision}</span></p>
            <ul>
              {evidence.criteria.map((criterion) => (
                <li key={criterion.criterion_id}>{criterion.criterion_id}: {criterion.outcome}</li>
              ))}
            </ul>
          </div>
        );
      })}

      {corrections.map((action) => {
        const correction = action.evidence_summary as unknown as EvaluationCorrectionSummary;
        return (
          <div key={action.id} className="mt-2 text-xs" aria-label="Evaluation correction">
            <p>{correction.issue_number ? `Correction issue #${correction.issue_number}` : 'Correction issue pending'}</p>
            <p>{correction.stage === 'retest_requested'
              ? `Fresh evaluation requested: cycle ${correction.retest_cycle}`
              : correction.stage === 'delivery_pending'
                ? 'Correction admitted; review, merge and verified deployment are required before retest.'
                : correction.stage === 'creation_unresolved'
                  ? 'Issue creation is unresolved; checking for the existing issue.'
                  : 'Preparing the correction issue.'}</p>
            <p>Correction allowance remaining after this cycle: {correction.remaining_corrections - 1}</p>
          </div>
        );
      })}

      {view.truncationNote && (
        <p className="mt-2 text-xs text-gray-500 dark:text-gray-400" data-testid={`execution-truncated-${nodeRef}`}>
          {view.truncationNote}
        </p>
      )}
      {view.cycleNote && (
        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400" data-testid={`execution-cycle-${nodeRef}`}>
          {view.cycleNote}
        </p>
      )}
    </section>
  );
}
