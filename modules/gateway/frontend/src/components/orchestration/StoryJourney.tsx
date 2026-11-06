import { Link } from 'react-router-dom';
import type { DeliveryProgress, GraphNode } from '@/types/orchestration';
import { storyJourney } from '@/utils/storyJourney';
import { DeliveryChecks } from './DeliveryChecks';
import { StoryAcceptanceChecklist } from './StoryAcceptanceChecklist';

const stageLabels: Record<string, string> = {
  development: 'Development', delivering: 'Development', repair: 'Repairs', repairing: 'Repairs',
  review: 'Review', awaiting_review: 'Review', checks: 'CI checks', merge: 'Merge', merge_ready: 'Ready to merge',
  handoff: 'PR handoff', binding: 'PR registration', verification: 'Delivery verification',
  provider_unavailable: 'GitHub evidence unavailable', reconciliation: 'Delivery reconciliation',
  historical_delivery: 'Historical delivery', continuation: 'Delivery continuation', complete: 'Complete',
};
const blockerLabels: Record<string, string> = {
  budget_exhausted: 'Budget is blocking this step',
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
/**
 * What `delivery_progress.automation` means to a reader. Amber marks a flow or
 * attempt the engine will not drive; gray explains an expected quiet state. An
 * undispatched story under an accepted policy is `not_started`, never
 * `not_configured` — the old default made every queued story look unautomated.
 */
const automationNotes: Record<string, { tone: 'amber' | 'gray'; text: string }> = {
  not_configured: { tone: 'amber', text: 'Automatic review and repair are not configured for this flow.' },
  not_initialized: { tone: 'amber', text: 'The engine has not initialized delivery tracking for this attempt yet.' },
  not_started: { tone: 'gray', text: 'Not started. The engine dispatches this story once its dependencies and admission requirements are met.' },
  reconciliation_only: { tone: 'gray', text: 'Historical delivery; no worker was dispatched. The engine rechecks evidence and predecessor requirements.' },
};
const quietAutomation = new Set(['not_applicable', 'not_started']);
function words(value: string) {
  const text = value.replace(/[_-]/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}
function DeliveryDiagnosis({ progress }: { progress: DeliveryProgress }) {
  const checks = progress.validations ?? [];
  const hasAttention = checks.some(check => check.status !== 'passed');
  const blockers = progress.blockers.filter(code => code !== 'automation_not_configured');
  return (
    <section aria-label="Current delivery status" className="mt-2 rounded border border-gray-200 p-2 text-xs dark:border-gray-700">
      {!hasAttention && <p>{progress.detail}</p>}
      <DeliveryChecks checks={checks} />
      <dl className="mt-2 space-y-1">
        {progress.actor !== 'none' && <div><dt className="inline font-medium">Responsible: </dt><dd className="inline">{words(progress.actor)}</dd></div>}
        {!checks.length && blockers.length > 0 && <div><dt className="font-medium">Blockers</dt><dd><ul className="list-disc pl-4">
          {blockers.map(code => <li key={code}>{blockerLabels[code] ?? words(code)}</li>)}
        </ul></dd></div>}
        {!hasAttention && progress.next_action && <div><dt className="inline font-medium">Next action: </dt><dd className="inline">{progress.next_action}</dd></div>}
      </dl>
      <details open={checks.length === 0} className="mt-2 text-gray-500 dark:text-gray-400">
        <summary className="cursor-pointer py-1">Timing and automation</summary>
        <dl className="mt-1 space-y-1">
        {progress.scheduled_action && progress.next_check_at ? <div>
          <dt className="inline font-medium">Scheduled check: </dt><dd className="inline">{progress.scheduled_action} · <time dateTime={progress.next_check_at}>{new Date(progress.next_check_at).toLocaleString()}</time></dd>
        </div> : !quietAutomation.has(progress.automation) && <div><dt className="inline font-medium">Schedule: </dt><dd className="inline">No automatic action is scheduled.</dd></div>}
        {progress.observed_at && <div><dt className="inline font-medium">Evidence recorded: </dt><dd className="inline"><time dateTime={progress.observed_at}>{new Date(progress.observed_at).toLocaleString()}</time></dd></div>}
        </dl>
      {automationNotes[progress.automation] && (
        <p className={automationNotes[progress.automation].tone === 'amber' ? 'mt-2 text-amber-700 dark:text-amber-300' : 'mt-2 text-gray-600 dark:text-gray-400'}>
          {automationNotes[progress.automation].text}
        </p>
      )}
      </details>
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
  const journeySteps = (
    <ol aria-label="Story journey" className="mt-3 space-y-2 border-l border-gray-200 pl-3 dark:border-gray-700">
        {steps.map(step => (
          <li key={step.id} aria-current={step.progress === 'current' || (step.id === 'merged' && node.state === 'passed') ? 'step' : undefined}
            data-stage={step.id} data-progress={step.progress} className="relative text-xs">
            <span aria-hidden="true" className={`absolute -left-[1.15rem] top-0 rounded-full bg-white dark:bg-gray-900 ${step.progress === 'complete' ? 'text-green-700 dark:text-green-400' : step.progress === 'current' ? 'text-blue-700 dark:text-blue-300' : 'text-gray-500'}`}>
              {step.progress === 'current' && step.run?.liveness === 'live' && step.run.status === 'in_progress'
                ? <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-r-transparent motion-reduce:animate-none" />
                : step.progress === 'complete' ? '✓' : step.progress === 'current' ? '▶' : step.progress === 'observed' ? '•' : '○'}
            </span>
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className={`font-medium ${step.progress === 'current' ? 'text-blue-700 dark:text-blue-300' : 'text-gray-800 dark:text-gray-200'}`}>{step.id === 'merged' && progress?.acceptance?.status === 'in_progress' ? 'Merged' : step.label}</span>
              {node.run_id && step.id === 'development' && (
                <Link to={`/activity?id=${encodeURIComponent(node.run_id)}`} className="text-blue-600 underline dark:text-blue-400">
                  View development run
                </Link>
              )}
              {node.run_id && step.run && step.id !== 'development' && (
                <Link to={`/activity?chain=${encodeURIComponent(node.run_id)}&highlight=${encodeURIComponent(step.run.invocation_id)}`}
                  className="text-blue-600 underline dark:text-blue-400">
                  {step.id === 'review' ? 'View review run' : 'View fixes run'}
                </Link>
              )}
              {step.id === 'merged' && node.bound_pull_request && (
                <a href={node.bound_pull_request.url} target="_blank" rel="noreferrer" className="text-blue-600 underline dark:text-blue-400">
                  Pull request #{node.bound_pull_request.pr_number}
                </a>
              )}
            </div>
            <span className="block text-gray-600 dark:text-gray-400">{step.id === 'merged' && progress?.acceptance?.status === 'in_progress' ? 'Code merged; engine acceptance continues.' : step.detail}</span>
          </li>
        ))}
    </ol>
  );
  return (
    <div className="mt-2" data-testid={`story-journey-${node.node_ref}`}>
      <p className="text-sm font-medium text-blue-700 dark:text-blue-300" data-testid={`node-stage-${node.node_ref}`}>
        {progress && <span className="text-xs font-normal">Current stage: </span>}
        <span>{progress?.blocker === 'budget_exhausted' ? `${stageLabels[progress.stage] ?? words(progress.stage)} blocked by budget` : progress ? stageLabels[progress.stage] ?? words(progress.stage) : headline}</span>
      </p>
      {progress && <DeliveryDiagnosis progress={progress} />}
      {progress?.acceptance && <StoryAcceptanceChecklist acceptance={progress.acceptance} />}
      {node.executor?.role === 'operate' && node.run_id && <Link to={`/activity?id=${encodeURIComponent(node.run_id)}`} className="mt-2 inline-block text-xs text-blue-600 underline dark:text-blue-400">View operations run</Link>}
      {node.executor?.role !== 'operate' && (progress?.acceptance ? (
        <details className="mt-3 text-xs text-gray-600 dark:text-gray-300">
          <summary className="cursor-pointer py-1 font-medium">Development and review history</summary>
          {journeySteps}
        </details>
      ) : journeySteps)}
      {!progress && node.binding_hold && <p className="mt-1 text-sm text-amber-700 dark:text-amber-300">{node.binding_hold}</p>}
      {node.executor?.role !== 'operate' && historyNote && <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">{historyNote}</p>}
      {execution}
    </div>
  );
}
