import { Link } from 'react-router-dom';
import type { DeliveryProgress, GraphNode } from '@/types/orchestration';
import { storyJourney } from '@/utils/storyJourney';

const stageLabels: Record<string, string> = {
  development: 'Development', delivering: 'Development', repair: 'Repairs', repairing: 'Repairs',
  review: 'Review', awaiting_review: 'Review', checks: 'CI checks', merge: 'Merge', merge_ready: 'Ready to merge',
  handoff: 'PR handoff', binding: 'PR registration', verification: 'Delivery verification',
  provider_unavailable: 'GitHub evidence unavailable', reconciliation: 'Delivery reconciliation',
  historical_delivery: 'Historical delivery', continuation: 'Delivery continuation', complete: 'Complete',
};
const blockerLabels: Record<string, string> = {
  ci_failed: 'CI failed', ci_pending: 'CI is running', ci_missing: 'Successful CI is unverified',
  changes_requested: 'Review requested changes', review_stale: 'Review applies to an older commit',
  review_required: 'Current commit needs approval', review_unverified: 'Review evidence is unverified',
  head_changed: 'PR head differs from registered revision', pr_identity_changed: 'PR identity changed',
  pr_binding_missing: 'PR handoff is missing', evidence_not_observed: 'Fresh evidence is pending',
  provider_unavailable: 'GitHub evidence is unavailable', mergeability_unverified: 'Merge eligibility is unverified',
  merge_evidence_incomplete: 'Merge evidence is incomplete', merge_conflict: 'Merge conflicts', draft: 'PR is a draft',
  github_merge_blocked: 'GitHub merge requirements are unmet', predecessor_pending: 'Predecessor requirements are pending',
  execution_stale: 'Execution belongs to a previous attempt or plan', execution_not_initialized: 'Automatic delivery is not initialized',
  execution_policy_reconciliation: 'Delivery policy obligations need reconciliation', flow_held: 'Flow is held',
};
function words(value: string) {
  const text = value.replace(/[_-]/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}
function DeliveryDiagnosis({ progress }: { progress: DeliveryProgress }) {
  const blockers = progress.blockers.filter(code => code !== 'automation_not_configured');
  return (
    <section aria-label="Current delivery status" className="mt-2 rounded border border-gray-200 p-2 text-xs dark:border-gray-700">
      <p>{progress.detail}</p>
      <dl className="mt-2 space-y-1">
        {progress.actor !== 'none' && <div><dt className="inline font-medium">Responsible: </dt><dd className="inline">{words(progress.actor)}</dd></div>}
        {blockers.length > 0 && <div><dt className="font-medium">Blockers</dt><dd><ul className="list-disc pl-4">
          {blockers.map(code => <li key={code}>{blockerLabels[code] ?? words(code)}</li>)}
        </ul></dd></div>}
        {progress.next_action && <div><dt className="inline font-medium">Next action: </dt><dd className="inline">{progress.next_action}</dd></div>}
        {progress.scheduled_action && progress.next_check_at ? <div>
          <dt className="inline font-medium">Scheduled check: </dt><dd className="inline">{progress.scheduled_action} · <time dateTime={progress.next_check_at}>{new Date(progress.next_check_at).toLocaleString()}</time></dd>
        </div> : progress.automation !== 'not_applicable' && <div><dt className="inline font-medium">Schedule: </dt><dd className="inline">No automatic action is scheduled.</dd></div>}
        {progress.observed_at && <div><dt className="inline font-medium">Evidence recorded: </dt><dd className="inline"><time dateTime={progress.observed_at}>{new Date(progress.observed_at).toLocaleString()}</time></dd></div>}
      </dl>
      {progress.automation === 'not_configured' && <p className="mt-2 text-amber-700 dark:text-amber-300">Automatic review and repair are not configured for this flow.</p>}
      {progress.automation === 'reconciliation_only' && <p className="mt-2 text-gray-600 dark:text-gray-400">Historical delivery; no worker was dispatched. The engine rechecks evidence and predecessor requirements.</p>}
    </section>
  );
}

/**
 * Keep the whole route visible even on narrow story cards.
 *
 * `execution` (issue #5145) is the delivery-ledger panel, passed as a **slot**
 * rather than queried here, for the same reason `controls` is a slot on `NodeChip`:
 * this component stays presentational, so it keeps rendering identically in tests
 * that mount no query client and for a caller with no ledger data at all. It is
 * rendered beneath the stage list, not beside it — the journey shows the stages and
 * the panel says why the current one is waiting and who acts next. Deliberately one
 * journey, not two: a parallel delivery view would drift from this stage
 * vocabulary.
 */
export function StoryJourney({ node, execution }: { node: GraphNode; execution?: React.ReactNode }) {
  const { headline, steps, historyNote } = storyJourney(node);
  const progress = node.delivery_progress;
  return (
    <div className="mt-2" data-testid={`story-journey-${node.node_ref}`}>
      <p className="text-sm font-medium text-blue-700 dark:text-blue-300" data-testid={`node-stage-${node.node_ref}`}>
        {progress ? stageLabels[progress.stage] ?? words(progress.stage) : headline}
      </p>
      {progress && <DeliveryDiagnosis progress={progress} />}
      <ol aria-label="Story journey" className="mt-3 space-y-2 border-l border-gray-200 pl-3 dark:border-gray-700">
        {steps.map(step => (
          <li key={step.id} aria-current={step.progress === 'current' || (step.id === 'merged' && node.state === 'passed') ? 'step' : undefined}
            data-stage={step.id} data-progress={step.progress} className="relative text-xs">
            <span aria-hidden="true" className={`absolute -left-[1.15rem] top-0 rounded-full bg-white dark:bg-gray-900 ${step.progress === 'complete' ? 'text-green-700 dark:text-green-400' : step.progress === 'current' ? 'text-blue-700 dark:text-blue-300' : 'text-gray-500'}`}>
              {step.progress === 'complete' ? '✓' : step.progress === 'current' ? '▶' : step.progress === 'observed' ? '•' : '○'}
            </span>
            <span className={`font-medium ${step.progress === 'current' ? 'text-blue-700 dark:text-blue-300' : 'text-gray-800 dark:text-gray-200'}`}>{step.label}</span>
            <span className="block text-gray-600 dark:text-gray-400">{step.detail}</span>
            {node.run_id && step.run && step.id !== 'development' && (
              <Link to={`/activity?chain=${encodeURIComponent(node.run_id)}&highlight=${encodeURIComponent(step.run.invocation_id)}`}
                className="text-blue-600 underline dark:text-blue-400">
                {step.id === 'review' ? 'View review run' : 'View fixes run'}
              </Link>
            )}
          </li>
        ))}
      </ol>
      {node.bound_pull_request && (
        <a href={node.bound_pull_request.url} target="_blank" rel="noreferrer"
          className="mt-2 inline-block text-xs text-blue-600 underline dark:text-blue-400">
          Pull request #{node.bound_pull_request.pr_number}
        </a>
      )}
      {!progress && node.binding_hold && <p className="mt-1 text-sm text-amber-700 dark:text-amber-300">{node.binding_hold}</p>}
      {historyNote && <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">{historyNote}</p>}
      {execution}
    </div>
  );
}
