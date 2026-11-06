import { useEffect, useState } from 'react';

import { getWorkspace, isSuperseded, ScopeGuard } from './client';
import type { ProvisioningProgress } from './contract';

const statusLabels = {
  pending: 'Not started', running: 'In progress', waiting: 'Waiting',
  completed: 'Complete', failed: 'Stopped', paused: 'Needs attention', unknown: 'Status unavailable',
};

export function ProvisioningSteps({ progress, problem }: {
  progress: ProvisioningProgress | null;
  problem?: string | null;
}) {
  return <section aria-label="Deployment progress" className="my-5 rounded-lg border border-gray-200 p-4 dark:border-gray-700">
    <h3 className="font-semibold">Deployment progress</h3>
    {progress ? <>
      <p className="mt-1 text-sm" role="status">{progress.completed} of {progress.total} steps complete · {progress.total - progress.completed} remaining</p>
      <progress className="my-3 h-2 w-full accent-blue-600" value={progress.completed} max={progress.total}
        aria-label="Completed deployment steps" />
      <ol className="space-y-3">
        {progress.steps.map((step, index) => <li key={step.id} aria-current={step.id === progress.current_step ? 'step' : undefined}
          className="flex items-center justify-between gap-3 text-sm">
          <span><span aria-hidden="true" className="mr-2">{step.state === 'completed' ? '✓' : `${index + 1}.`}</span>{step.label}</span>
          <span className={step.state === 'completed' ? 'text-green-700 dark:text-green-400' :
            ['failed', 'paused'].includes(step.state) ? 'text-amber-700 dark:text-amber-400' : 'text-gray-600 dark:text-gray-300'}>
            {statusLabels[step.state]}
          </span>
        </li>)}
      </ol>
      <p className="mt-4 text-sm">{progress.message}</p>
    </> : <p className="mt-2 text-sm">{problem ?? 'Loading deployment steps…'}</p>}
    {progress && problem && <p role="status" className="mt-3 text-sm text-amber-700 dark:text-amber-400">
      {problem} Showing the last recorded progress.
    </p>}
  </section>;
}

export function WorkspaceDeploymentProgress({ workspaceId }: { workspaceId: string }) {
  const [progress, setProgress] = useState<ProvisioningProgress | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    const guard = new ScopeGuard();
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    setProgress(null);
    setProblem(null);
    const refresh = async () => {
      const result = await getWorkspace(guard, workspaceId);
      if (stopped || isSuperseded(result)) return;
      if (result.ok && result.value.id === workspaceId && result.value.provisioning_progress) {
        setProgress(result.value.provisioning_progress);
        setProblem(null);
      } else {
        setProblem('Deployment steps are temporarily unavailable.');
      }
      timer = setTimeout(() => void refresh(), 5000);
    };
    void refresh();
    return () => { stopped = true; clearTimeout(timer); guard.supersede(); };
  }, [workspaceId]);
  return <ProvisioningSteps progress={progress} problem={problem} />;
}
