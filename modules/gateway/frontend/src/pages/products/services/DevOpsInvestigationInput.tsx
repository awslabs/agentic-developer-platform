import { sameConsentScope } from '@/services/l5OwnerConsent';
import { useEffect, useRef } from 'react';
import type { DevOpsConfiguration } from '@/services/l5Services';
import type { RecordReference } from '@/services/products';
import { workloadName } from './DevOpsPresentation';

const object = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;

export default function DevOpsInvestigationInput({ configuration, bindingId, value, onChange, disabled }: {
  configuration: DevOpsConfiguration; bindingId: string; value: string; onChange: (value: string) => void; disabled: boolean;
}) {
  let draft: Record<string, unknown> | null = null;
  try { draft = object(JSON.parse(value)); } catch { /* Preserve an incomplete advanced draft. */ }
  const reference = (value: unknown): value is RecordReference => {
    const ref = object(value);
    return Boolean(ref && ref.tenant_id === configuration.scope.tenant_id && ref.project_id === configuration.scope.project_id
      && ['source_connection_id', 'record_type', 'record_id', 'record_revision'].every(key => typeof ref[key] === 'string' && ref[key]));
  };
  const triggers = (configuration.settings.devops.trigger_policies ?? []).map(object).filter(trigger => trigger?.kind === 'manual'
    && trigger.enabled === true && trigger.capability === 'investigation' && trigger.binding_id === bindingId);
  const trigger = triggers.length === 1 ? triggers[0] : null;
  const resources = trigger?.resource_refs;
  const telemetry = trigger?.telemetry_refs;
  const savedScope = Array.isArray(resources) && resources.length > 0 && resources.every(reference)
    && Array.isArray(telemetry) && telemetry.every(reference) ? { resource_refs: resources, telemetry_refs: telemetry } : null;
  const initialized = useRef('');
  const savedKey = JSON.stringify([configuration.configuration_id, bindingId, savedScope]);
  useEffect(() => {
    if (disabled || !savedScope || !draft || draft.kind !== 'investigation' || initialized.current === savedKey) return;
    const previous = initialized.current;
    initialized.current = savedKey;
    if (previous || Array.isArray(draft.resource_refs) && !draft.resource_refs.length && Array.isArray(draft.telemetry_refs) && !draft.telemetry_refs.length) {
      onChange(JSON.stringify({ ...draft, ...savedScope }, null, 2));
    }
  }, [savedKey, disabled, draft, savedScope, onChange]);
  const usesSaved = Boolean(savedScope && draft && sameConsentScope(draft.resource_refs, savedScope.resource_refs) && sameConsentScope(draft.telemetry_refs, savedScope.telemetry_refs));
  const selected = draft && Array.isArray(draft.resource_refs) && Array.isArray(draft.telemetry_refs)
    ? [...draft.resource_refs, ...draft.telemetry_refs].filter(reference) : [];

  return <div className="devops-investigation-input space-y-4">
    <label>Investigation question <textarea placeholder="For example: Why did this workload fail in the last hour, and what should we check next?" maxLength={4000} value={typeof draft?.question === 'string' ? draft.question : ''}
      disabled={disabled || !draft || draft.kind !== 'investigation'}
      onChange={event => onChange(JSON.stringify({ ...draft, question: event.target.value }, null, 2))} /></label>
    <div className="devops-workload-selection"><div><strong>{usesSaved ? 'Using your saved setup' : 'Workload for this run'}</strong>
      {selected.length > 0 && <ul aria-label="Selected Investigation resources" className="devops-run-target">{selected.map((ref, index) => <li key={index}><span>{ref.record_type === 'resource' ? 'Workload' : 'Logs'}</span> {workloadName(ref.record_id)}</li>)}</ul>}</div>
    <button hidden={usesSaved} type="button" className="product-text-button" disabled={disabled || !savedScope || !draft || draft.kind !== 'investigation'} onClick={() => {
      if (savedScope && draft) onChange(JSON.stringify({ kind: 'investigation', question: typeof draft.question === 'string' ? draft.question : '', ...savedScope }, null, 2));
    }}>Use saved workload</button></div>
    {!savedScope && <p>No single enabled Investigation scope is saved for this binding. Complete resource verification and activation in Setup, then refresh the configuration.</p>}
    {!draft && <p>The advanced input is incomplete. Correct it below to use the question and resource controls.</p>}
    {!selected.length && <p>No Investigation resources selected for this run.</p>}
  </div>;
}
