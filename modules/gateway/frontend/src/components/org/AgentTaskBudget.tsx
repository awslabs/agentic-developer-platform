import { useEffect, useState } from 'react';
import { getTaskPolicyView, type TaskPolicyView } from '@/services/taskPolicies';
import { TaskBudgetPreview } from './TaskBudgetPreview';
export function AgentTaskBudget({ principal, persona, model, selectionRevision }: { principal?: string; persona: string; model?: string | null; selectionRevision?: number | null }) {
  const [view, setView] = useState<TaskPolicyView>();
  const [error, setError] = useState(false);
  useEffect(() => {
    let active = true; setView(undefined); setError(false);
    void getTaskPolicyView(principal).then(value => { if (active) setView(value); }).catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, [principal, model, selectionRevision]);
  return <div className="px-5 pb-5 sm:px-6">
    <h4 className="font-semibold">Task budget compatibility</h4>
    {error ? <p role="status">Task policy could not be loaded. Budget compatibility is not confirmed.</p>
      : view ? <><TaskBudgetPreview model={model} persona={persona} revision={view.models.find(value => value.persona === persona)?.revision} policy={view.policy} />
        <p className="text-xs mt-2">{principal ? 'Administrators can edit this service account’s Task policy in Organizations.' : 'This is your own Task policy. Service accounts use their own model selections and budgets; select the account above to check it.'}</p></>
        : <p role="status">Loading Task policy…</p>}
  </div>;
}
