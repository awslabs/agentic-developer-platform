import { useEffect, useRef, useState } from 'react';
import type { SecurityBinding, SecuritySettings, Versioned, WellArchitectedConfiguration } from '@/services/l5Services';
import { sameReference, scopedReference } from './wellArchitectedProvider';

type Triggers = SecuritySettings['triggers'];
export type SaveWellArchitectedSchedules = (triggers: Triggers, expected: Versioned<WellArchitectedConfiguration>) => Promise<boolean>;
interface Props {
  configuration: Versioned<WellArchitectedConfiguration>;
  bindings: SecurityBinding[];
  bindingsCurrent: boolean;
  disabled: boolean;
  onSave: SaveWellArchitectedSchedules;
}
interface Draft { id: string; existing: boolean; version: string; time: string; bindingId: string; enabled: boolean; reviewed: boolean }

const dailyTime = (expression?: string) => {
  const match = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(expression ?? '');
  return match && Number(match[1]) < 60 && Number(match[2]) < 24 ? `${match[2].padStart(2, '0')}:${match[1].padStart(2, '0')}` : '';
};
const cron = (time: string) => {
  const match = /^([01][0-9]|2[0-3]):([0-5][0-9])$/.exec(time);
  return match ? `${Number(match[2])} ${Number(match[1])} * * *` : '';
};
export function sameScheduleTriggers(left: Triggers, right: Triggers): boolean {
  const normalize = (value: unknown): unknown => Array.isArray(value) ? value.map(normalize)
    : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, child]) => [key, normalize(child)])) : value;
  return JSON.stringify(normalize(left)) === JSON.stringify(normalize(right));
}

export default function WellArchitectedScheduleEditor({ configuration, bindings, bindingsCurrent, disabled, onSave }: Props) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  const { settings, scope, configuration_id: configurationId } = configuration.value;
  const version = JSON.stringify([scope, configurationId, configuration.value.revision, configuration.etag]);
  const triggers = settings.triggers ?? [];
  const schedules = triggers.filter(trigger => trigger.kind === 'schedule');
  const current = triggers.find(trigger => trigger.trigger_id === draft?.id);
  const stale = !!draft && draft.version !== version;
  const locked = disabled || saving;
  const sources = settings.source_refs;
  const validSources = sources.length > 0 && sources.length <= 100 && sources.every(ref => scopedReference(ref, scope.tenant_id, scope.project_id)
    && ['document', 'repository_commit'].includes(ref.record_type));
  const readyBindings = bindingsCurrent ? bindings.filter(binding => binding.state === 'ready' && Array.isArray(binding.gaps) && binding.gaps.length === 0
    && binding.configuration_id === configurationId && Number.isInteger(binding.generation) && binding.generation > 0
    && Number.isInteger(binding.revision) && binding.revision > 0
    && binding.scope.tenant_id === scope.tenant_id && binding.scope.product_id === scope.product_id
    && binding.scope.project_id === scope.project_id && binding.scope.relationship_revision === scope.relationship_revision
    && settings.connection_ref && scopedReference(binding.connection_ref, scope.tenant_id, scope.project_id)
    && sameReference(binding.connection_ref, settings.connection_ref)) : [];
  const binding = readyBindings.find(item => item.binding_id === draft?.bindingId);
  const duplicateIds = new Set(triggers.map(item => item.trigger_id)).size !== triggers.length;
  const reviewKey = JSON.stringify([version, binding?.binding_id, binding?.generation, binding?.revision, bindingsCurrent]);
  const [reviewedKey, setReviewedKey] = useState('');
  const reviewed = draft?.reviewed && reviewedKey === reviewKey;
  const unsupported = current && (current.kind !== 'schedule' || current.capability !== 'architecture_review' || !dailyTime(current.schedule));

  function change(values: Partial<Draft>) {
    setError('');
    setDraft(previous => previous ? { ...previous, ...values, reviewed: false } : previous);
  }
  async function persist(next: Triggers) {
    const request = ++generation.current;
    setSaving(true); setError('');
    try {
      if (await onSave(next, configuration) && request === generation.current) setDraft(null);
    } finally { if (request === generation.current) setSaving(false); }
  }
  async function saveDraft() {
    if (!draft || locked || stale || duplicateIds) return;
    if (unsupported || draft.existing && !current) { setError('This saved schedule changed or was removed. Discard this draft and review the current schedules.'); return; }
    if (!cron(draft.time) || !binding || !validSources || !reviewed || !settings.capabilities.includes('architecture_review')
      || draft.enabled && !settings.enabled) {
      setError('Choose a daily UTC time and a current ready binding, then review every saved source revision. Enabling a schedule also requires an enabled saved configuration.'); return;
    }
    const next = { ...current, trigger_id: draft.id, kind: 'schedule', capability: 'architecture_review', enabled: draft.enabled,
      schedule: cron(draft.time), binding_id: binding.binding_id, source_refs: sources, debounce_seconds: current?.debounce_seconds ?? 0,
      // An existing generation pin must follow an explicitly reviewed binding change.
      ...(current && 'binding_generation' in current ? { binding_generation: binding.generation } : {}) };
    await persist(current ? triggers.map(item => item.trigger_id === draft.id ? next : item) : [...triggers, next]);
  }

  return <aside aria-label="Well-Architected schedules" className="space-y-3">
    <h3>Daily review schedules (UTC)</h3>
    <p>Schedules use the saved configuration, including every pinned source revision in its saved order. Unsaved Setup and Capabilities edits are not included.</p>
    <p>Each occurrence checks the current configuration, ready binding, provider qualification and owner consent. Saving a schedule does not grant consent or start a review.</p>
    <p>Next-run and last-tick status are not available here. Review run history below for work that was admitted.</p>
    {error && <p role="alert">{error}</p>}
    {duplicateIds && <p role="alert">Saved trigger identities are duplicated. Schedule editing is blocked until the configuration is repaired.</p>}
    {!settings.enabled && <p>The saved configuration is disabled. You can prepare a paused schedule; enable the configuration in Setup before enabling its schedule.</p>}
    {!bindingsCurrent && <p>Current bindings could not be confirmed. Refresh review status before creating or editing a schedule.</p>}
    {schedules.length === 0 && <p>No saved review schedules.</p>}
    {schedules.map((item, index) => <div key={`${item.trigger_id}:${index}`} aria-label={`Saved schedule ${item.trigger_id}`}>
      <p>{item.trigger_id}: {item.enabled ? 'enabled' : 'paused'} · {item.schedule} UTC · {item.capability ?? 'capability not specified'} · binding {item.binding_id ?? 'not selected'}</p>
      <p>Saved source revisions: {item.source_refs.map(ref => `${ref.record_id} (${ref.record_revision})`).join(', ') || 'none'}.</p>
      {item.capability === 'architecture_review' && dailyTime(item.schedule)
        ? <button type="button" disabled={locked || duplicateIds || !!draft} onClick={() => {
          setError(''); setDraft({ id: item.trigger_id, existing: true, version, time: dailyTime(item.schedule),
            bindingId: item.binding_id ?? '', enabled: item.enabled, reviewed: false });
        }}>Edit schedule {item.trigger_id}</button>
        : <p>This schedule uses settings outside the daily review editor. Its expression and sources are preserved; pausing is supported.</p>}
      {item.enabled && <button type="button" disabled={locked || duplicateIds || !!draft} onClick={() => void persist(triggers.map(trigger => trigger.trigger_id === item.trigger_id ? { ...trigger, enabled: false } : trigger))}>Pause schedule {item.trigger_id}</button>}
    </div>)}
    <button type="button" disabled={locked || duplicateIds || !!draft} onClick={() => {
      setError(''); setDraft({ id: crypto.randomUUID(), existing: false, version, time: '', bindingId: '', enabled: false, reviewed: false });
    }}>Add daily review schedule</button>
    {draft && <fieldset disabled={locked || duplicateIds} className="space-y-2"><legend>Review schedule draft</legend>
      <p>Trigger identity: {draft.id}. Pausing or editing keeps this identity and retained history.</p>
      {stale && <><p>The saved configuration changed after this draft was opened. Review the refreshed configuration and binding before saving again.</p>
        <button type="button" disabled={!bindingsCurrent || !!unsupported || draft.existing && !current} onClick={() => {
          setError(''); setDraft({ ...draft, existing: !!current, version, reviewed: false });
        }}>Review draft against refreshed configuration</button></>}
      {draft.existing && !current && <p>This schedule was removed. Discard this draft to review current schedules.</p>}
      <label>Daily review time (UTC) <input type="time" value={draft.time} onChange={event => change({ time: event.target.value })} /></label>
      <label>Review schedule binding <select value={draft.bindingId} onChange={event => change({ bindingId: event.target.value })}>
        <option value="">Select a ready binding</option>
        {draft.bindingId && !binding && <option value={draft.bindingId} disabled>Previously selected binding is not currently ready</option>}
        {readyBindings.map(item => <option key={item.binding_id} value={item.binding_id}>{item.binding_id} (generation {item.generation})</option>)}
      </select></label>
      <label><input type="checkbox" checked={draft.enabled} onChange={event => change({ enabled: event.target.checked })} />Enable this daily schedule when saved</label>
      <p>Configuration revision {configuration.value.revision}; capability: architecture_review; binding generation: {binding?.generation ?? 'select a ready binding'}.</p>
      <ul aria-label="Exact saved schedule sources">{sources.map((ref, index) => <li key={index}>{ref.record_type}: {ref.record_id} ({ref.record_revision})</li>)}</ul>
      <label><input type="checkbox" checked={!!reviewed} disabled={stale || !binding || !validSources} onChange={event => {
        setDraft({ ...draft, reviewed: event.target.checked }); setReviewedKey(reviewKey);
      }} />Use this binding and every saved source revision for the schedule</label>
      <button type="button" disabled={stale || !!unsupported || draft.existing && !current || !bindingsCurrent} onClick={() => void saveDraft()}>Save review schedule</button>
      <button type="button" onClick={() => { setDraft(null); setError(''); }}>Discard schedule draft</button>
    </fieldset>}
  </aside>;
}
