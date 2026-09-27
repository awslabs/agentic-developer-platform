import { useEffect, useState } from 'react';
import { previewReservation, type ReservationPreview, type TaskPolicy } from '@/services/taskPolicies';
export function TaskBudgetPreview({ model, policy, persona, revision }: { model?: string | null; policy: TaskPolicy | null; persona: string; revision?: string | null }) {
  const [preview, setPreview] = useState<ReservationPreview>();
  const [error, setError] = useState(false);
  const output = policy?.limits.max_output_tokens_per_turn ?? 4096;
  useEffect(() => {
    let active = true; setPreview(undefined); setError(false);
    if (model) void previewReservation(model, output).then(value => { if (active) setPreview(value); }).catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, [model, output]);
  const enrolled = policy?.status === 'active' && policy.allowed_personas.includes(persona) && policy.task_scopes.includes('submit');
  const exceeds = preview?.reservation_usd != null && policy && Number(preview.reservation_usd) > Number(policy.limits.max_usd_per_task);
  return <div className="text-sm space-y-2 mt-3" role="status">
    <p>{policy ? `Task budget: $${policy.limits.max_usd_per_task} per Task.` : 'No Task policy is configured for this identity.'}</p>
    {revision === null && <p className="text-amber-700 dark:text-amber-300">An explicit Task model selection is required; a class default is not sufficient.</p>}
    {revision && policy && (policy.model_policy_versions?.[persona] ?? policy.model_policy_version) !== revision && <p className="text-amber-700 dark:text-amber-300">This model selection is not authorized by the Task policy. An administrator must authorize the current selection before new Tasks can run.</p>}
    {!enrolled && <p className="text-amber-700 dark:text-amber-300">This identity is not enabled to submit Tasks for this persona.</p>}
    {preview?.status === 'available' && <p>Conservative reservation: ${preview.reservation_usd} per model request, using the full context capacity and up to {output} output tokens. This is not an actual charge.</p>}
    {exceeds && <p className="text-red-700 dark:text-red-300">This model’s reservation exceeds the Task budget. Requests will be blocked before provider dispatch. Ask an administrator to review the Task policy or select another model.</p>}
    {(error || preview?.status === 'unavailable') && <p className="text-amber-700 dark:text-amber-300">Reservation preview unavailable. Budget compatibility is not confirmed.</p>}
    {model && !preview && !error && <p>Checking reservation…</p>}
    <p className="text-xs text-gray-500">The Task budget covers the whole Task. Organization, department, team and identity budgets also apply. Actual request features are checked at dispatch.</p>
  </div>;
}
