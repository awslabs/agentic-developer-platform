import { useEffect, useRef, useState } from 'react';
import { Button, Input, Modal, Select } from '@/components/ui';
import type { OrganizationServiceIdentity } from '@/services/organizationServiceIdentities';
import { identityPolicy, saveTaskPolicy, type TaskPolicy, type TaskPolicyView } from '@/services/taskPolicies';
import { TaskBudgetPreview } from './TaskBudgetPreview';

export function TaskPolicyModal({ orgId, identity, onClose }: {
  orgId: string; identity: OrganizationServiceIdentity; onClose: () => void;
}) {
  const [view, setView] = useState<TaskPolicyView>();
  const [draft, setDraft] = useState<TaskPolicy>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [authorizeModels, setAuthorizeModels] = useState(false);
  const [saved, setSaved] = useState(false);
  const [reload, setReload] = useState(0);
  const [mustReload, setMustReload] = useState(false);
  const active = useRef(false);
  const saving = useRef(false);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  useEffect(() => {
    let current = true; setView(undefined); setDraft(undefined); setError(''); setMustReload(false); setSaved(false); setAuthorizeModels(false);
    void identityPolicy(orgId, identity).then(value => {
      if (!current) return;
      setView(value);
      setDraft(value.policy ?? { tenant_id: orgId, canonical_principal_id: value.canonical_principal_id, version: 0,
        status: 'disabled', allowed_personas: [], allowed_tools: [], task_scopes: ['submit', 'read', 'cancel', 'artifacts'],
        model_policy_version: '1', limits: { ...value.platform_limits, max_usd_per_task: Math.min(1, Number(value.platform_limits.max_usd_per_task)), max_duration_minutes: 60 } });
    }).catch((cause: { status?: number }) => {
      if (current) setError(cause.status === 404 || cause.status === 409
        ? 'This account needs an unambiguous canonical registration before Task enrollment. No policy was changed.'
        : 'Task policy could not be loaded. Check your organization permissions or retry.');
    });
    return () => { current = false; };
  }, [orgId, identity, reload]);
  function change(update: Partial<TaskPolicy>) { setDraft(previous => previous && ({ ...previous, ...update })); setSaved(false); }
  function toggle(field: 'allowed_personas' | 'allowed_tools' | 'task_scopes', value: string) {
    if (draft) change({ [field]: draft[field].includes(value) ? draft[field].filter(item => item !== value) : [...draft[field], value] });
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!draft || !view || saving.current || mustReload) return;
    saving.current = true; setBusy(true); setError(''); setSaved(false);
    try {
      const versions = Object.fromEntries(Object.entries(draft.model_policy_versions ?? {}).filter(([persona]) => draft.allowed_personas.includes(persona)));
      if (authorizeModels) for (const selection of view.models) {
        if (selection.revision && draft.allowed_personas.includes(selection.persona)) versions[selection.persona] = selection.revision;
      }
      const result = await saveTaskPolicy(orgId, view.canonical_principal_id, { ...draft, model_policy_versions: versions });
      if (!active.current) return;
      if (result.tenant_id !== orgId || result.canonical_principal_id !== view.canonical_principal_id) throw new Error('Unexpected identity');
      setDraft(result); setSaved(true);
    } catch (cause) {
      if (!active.current) return;
      const status = (cause as { status?: number }).status;
      setMustReload(status !== 422);
      setError(status === 409 ? 'The policy changed while you were editing. Reload before saving again.'
        : status === 422 ? 'Policy rejected. Check the limits and selections against the platform ceiling.'
        : 'Save could not be confirmed. Reload the stored policy before trying again.');
    } finally { saving.current = false; if (active.current) setBusy(false); }
  }
  return <Modal isOpen onClose={() => { if (!busy) onClose(); }} title={`Task policy — ${identity.name}`} size="lg">
    <p className="text-sm mb-4">Controls this service account’s Task API permissions and limits. A Task budget is separate from the organization’s monthly budget. Saving does not grant OAuth scopes or change model selections.</p>
    {error && <p role="alert" className="text-red-700 mb-3">{error}</p>}
    {!draft && !error && <p>Loading Task policy…</p>}
    {draft && view && <form onSubmit={event => void submit(event)} className="space-y-4">
      <p className="text-xs break-all">Service principal: {view.canonical_principal_id} · Policy version {draft.version}</p>
      <Select id="task-policy-status" label="Task access" value={draft.status} onChange={event => change({ status: event.target.value as TaskPolicy['status'] })}
        options={[{ value: 'disabled', label: 'Disabled' }, { value: 'active', label: 'Enabled' }]} />
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Input id="task-policy-usd" aria-label="Maximum spend per Task (USD)" label="Maximum spend per Task (USD)" type="number" min="0.000001" step="0.000001" max={Number(view.platform_limits.max_usd_per_task)} required
          value={draft.limits.max_usd_per_task} onChange={event => change({ limits: { ...draft.limits, max_usd_per_task: event.target.value } })} />
        {([['max_duration_minutes', 'Runtime limit (minutes)'], ['max_turns', 'Maximum model turns'], ['max_output_tokens_per_turn', 'Output tokens per turn']] as const).map(([key, label]) =>
          <Input key={key} id={`task-policy-${key}`} label={label} type="number" min={1} step={1} max={view.platform_limits[key]} required value={draft.limits[key]}
            onChange={event => change({ limits: { ...draft.limits, [key]: Number(event.target.value) } })} />)}
      </div>
      <p className="text-sm">Platform enrollment ceiling: ${view.platform_limits.max_usd_per_task} per Task. Platform operators manage this deployment setting. Raising it does not increase existing account budgets.</p>
      <fieldset><legend className="font-semibold">Allowed personas</legend>
        {[...new Set([...Object.keys(view.persona_tools), ...draft.allowed_personas])].map(persona => <label key={persona} className="block text-sm"><input type="checkbox" checked={draft.allowed_personas.includes(persona)} onChange={() => toggle('allowed_personas', persona)} /> {persona}</label>)}
      </fieldset>
      <fieldset><legend className="font-semibold">Task API permissions</legend>
        {['submit', 'read', 'input', 'cancel', 'artifacts'].map(scope => <label key={scope} className="inline-block mr-4 text-sm"><input type="checkbox" checked={draft.task_scopes.includes(scope)} onChange={() => toggle('task_scopes', scope)} /> {scope}</label>)}
      </fieldset>
      <fieldset><legend className="font-semibold">Allowed tools</legend>
        <p className="text-xs">Only tools also enabled for the persona by the deployment can run.</p>
        {[...new Set([...draft.allowed_tools, ...draft.allowed_personas.flatMap(persona => view.persona_tools[persona] ?? [])])].sort().map(tool => <label key={tool} className="block text-sm"><input type="checkbox" checked={draft.allowed_tools.includes(tool)} onChange={() => toggle('allowed_tools', tool)} /> {tool}</label>)}
      </fieldset>
      <label className="block text-sm"><input type="checkbox" checked={authorizeModels} onChange={event => setAuthorizeModels(event.target.checked)} /> Authorize the displayed model selections for enabled personas when saving</label>
      <p className="text-xs">Model changes require renewed Task authorization. Reload to see selections changed in another tab.</p>
      {view.models.filter(model => draft.allowed_personas.includes(model.persona)).map(model => <div key={model.persona}><h3 className="font-semibold">{model.persona}</h3><p className="text-xs break-all">{model.model || 'No model selected'}</p><TaskBudgetPreview model={model.model} revision={model.revision} persona={model.persona} policy={draft} /></div>)}
      {saved && <p role="status" className="text-green-700">Task policy saved. New Tasks use these limits; existing runs retain their admitted limits and remain subject to current authorization checks.</p>}
      <div className="flex gap-2"><Button type="submit" disabled={busy || mustReload || !draft.allowed_personas.length || !draft.task_scopes.length}>{busy ? 'Saving…' : 'Save Task policy'}</Button>
        <Button type="button" variant="secondary" disabled={busy} onClick={() => setReload(value => value + 1)}>Reload stored policy</Button></div>
    </form>}
    {!draft && error && <Button onClick={() => setReload(value => value + 1)}>Retry</Button>}
  </Modal>;
}
