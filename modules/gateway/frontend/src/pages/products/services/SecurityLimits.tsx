import type { SecurityLimits as Limits } from '@/services/l5Services';
import type { RecordReference } from '@/services/products';

const integers = [
  ['deadline_seconds', 'Run deadline (seconds)', 60, 86400],
  ['max_provider_attempts', 'Maximum provider attempts', 1, 10],
  ['max_polls', 'Maximum status checks', 1, 2880],
  ['concurrency', 'Concurrent assessments', 1, 10],
] as const;
type IntegerKey = typeof integers[number][0];
export type LimitsDraft = Record<IntegerKey | 'max_cost' | 'policy_connection' | 'policy_type' | 'policy_id' | 'policy_revision', string>;
export const limitsDraft = (limits?: Limits): LimitsDraft => ({
  deadline_seconds: String(limits?.deadline_seconds ?? ''), max_provider_attempts: String(limits?.max_provider_attempts ?? ''),
  max_polls: String(limits?.max_polls ?? ''), concurrency: String(limits?.concurrency ?? ''), max_cost: limits?.max_cost ?? '',
  policy_connection: limits?.policy_ref?.source_connection_id ?? '', policy_type: limits?.policy_ref?.record_type ?? '',
  policy_id: limits?.policy_ref?.record_id ?? '', policy_revision: limits?.policy_ref?.record_revision ?? '',
});
export function parseLimits(draft: LimitsDraft, tenantId: string, projectId: string): Limits {
  if (!tenantId || integers.some(([key, , min, max]) => !/^\d+$/.test(draft[key]) || Number(draft[key]) < min || Number(draft[key]) > max)
    || !/^[0-9]+(\.[0-9]{1,6})?$/.test(draft.max_cost)
    || ['policy_connection', 'policy_type', 'policy_id', 'policy_revision'].some(key => !draft[key as keyof LimitsDraft].trim())
    || ['latest', 'HEAD', 'null'].includes(draft.policy_revision.trim())) {
    throw new Error('Complete the assessment limits and an immutable budget policy reference from this project.');
  }
  const policy_ref: RecordReference = { tenant_id: tenantId, project_id: projectId, source_connection_id: draft.policy_connection.trim(),
    record_type: draft.policy_type.trim(), record_id: draft.policy_id.trim(), record_revision: draft.policy_revision.trim() };
  return { deadline_seconds: Number(draft.deadline_seconds), max_provider_attempts: Number(draft.max_provider_attempts),
    max_polls: Number(draft.max_polls), concurrency: Number(draft.concurrency), max_cost: draft.max_cost, currency: 'USD', policy_ref };
}
export default function SecurityLimits({ value, onChange }: { value: LimitsDraft; onChange: (value: LimitsDraft) => void }) {
  const edit = (key: keyof LimitsDraft, next: string) => onChange({ ...value, [key]: next });
  return <fieldset><legend>Assessment limits</legend>
    <p>Set bounds for each assessment and select an existing budget policy. Saving limits does not create or increase that policy’s budget.</p>
    {integers.map(([key, label, min, max]) => <label className="block" key={key}>{label}
      <input type="number" min={min} max={max} step="1" value={value[key]} onChange={event => edit(key, event.target.value)} />
    </label>)}
    <label className="block">Maximum cost per assessment (USD)<input inputMode="decimal" value={value.max_cost} onChange={event => edit('max_cost', event.target.value)} /></label>
    <details><summary>Existing budget policy reference</summary>
      <p>The policy must belong to this tenant and project. Its authority is checked by the backend before execution.</p>
      {([['policy_connection', 'Budget policy connection'], ['policy_type', 'Budget policy record type'], ['policy_id', 'Budget policy ID'], ['policy_revision', 'Budget policy revision']] as const).map(([key, label]) =>
        <label className="block" key={key}>{label}<input value={value[key]} onChange={event => edit(key, event.target.value)} /></label>)}
    </details>
  </fieldset>;
}
