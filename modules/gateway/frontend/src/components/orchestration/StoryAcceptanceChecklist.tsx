import { useId } from 'react';
import type { StoryAcceptance } from '@/types/orchestration';

const states = {
  passed: { label: 'Passed', icon: '✓', tone: 'text-green-700 dark:text-green-400' },
  pending: { label: 'Pending', icon: '○', tone: 'text-gray-500 dark:text-gray-400' },
  running: { label: 'Running', icon: '●', tone: 'text-blue-700 dark:text-blue-300' },
  waiting: { label: 'Waiting', icon: '○', tone: 'text-gray-600 dark:text-gray-300' },
  recovering: { label: 'Recovering', icon: '↻', tone: 'text-blue-700 dark:text-blue-300' },
  blocked: { label: 'Needs attention', icon: '!', tone: 'text-amber-800 dark:text-amber-300' },
  paused: { label: 'Paused', icon: 'Ⅱ', tone: 'text-gray-600 dark:text-gray-300' },
};

/** Uses the shared graph refresh. Receipt-backed results remain visible after completion. */
export function StoryAcceptanceChecklist({ acceptance }: { acceptance: StoryAcceptance }) {
  const headingId = useId();
  const complete = acceptance.checks.filter(check => check.status === 'passed').length;
  return (
    <section aria-labelledby={headingId} className="mt-3 rounded-md border border-gray-200 p-3 text-xs dark:border-gray-700">
      <div className="flex flex-wrap items-baseline justify-between gap-1">
        <h4 id={headingId} className="font-semibold text-gray-900 dark:text-gray-100">Engine acceptance</h4>
        {acceptance.checks.length > 0 && <span role="status" className="text-gray-600 dark:text-gray-300">{complete} of {acceptance.checks.length} checks complete</span>}
      </div>
      <p className="mt-1 text-gray-600 dark:text-gray-300">{acceptance.detail}</p>
      {acceptance.checks.length > 0 && <ol aria-label="Story acceptance checks" className="mt-3 space-y-2">
        {acceptance.checks.map(check => {
          const state = states[check.status];
          return (
            <li key={check.id} data-check={check.id} data-status={check.status}>
              <details>
                <summary className="cursor-pointer rounded py-1 focus-visible:outline-2 focus-visible:outline-blue-500">
                  <span aria-hidden="true" className={`mr-2 ${state.tone}`}>{state.icon}</span>
                  <span className="font-medium text-gray-800 dark:text-gray-100">{check.label}</span>
                  <span className={`ml-2 ${state.tone}`}>{state.label}</span>
                </summary>
                <div className="ml-5 space-y-1 text-gray-600 dark:text-gray-300">
                  <p>{check.detail}</p>
                  {check.observed_at && <p>Observed <time dateTime={check.observed_at}>{new Date(check.observed_at).toLocaleString()}</time></p>}
                  {check.evidence_id && <p className="break-all">Evidence record: <code>{check.evidence_id}</code></p>}
                </div>
              </details>
              {check.status !== 'passed' && check.status !== 'pending' && <p className="ml-5 text-gray-600 dark:text-gray-300">{check.detail}</p>}
            </li>
          );
        })}
      </ol>}
      {acceptance.next_action && <p className="mt-3 text-gray-800 dark:text-gray-200"><span className="font-medium">Next action: </span>{acceptance.next_action}</p>}
      {acceptance.next_check_at && <p className="mt-1 text-gray-600 dark:text-gray-300">Next check: <time dateTime={acceptance.next_check_at}>{new Date(acceptance.next_check_at).toLocaleString()}</time></p>}
    </section>
  );
}
