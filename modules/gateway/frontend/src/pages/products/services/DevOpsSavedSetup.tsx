import type { RecordReference } from '@/services/products';
import type { DevOpsConfiguration } from '@/services/l5Services';
import { workloadName } from './DevOpsPresentation';

export default function DevOpsSavedSetup({ configuration, projectId }: {
  configuration: DevOpsConfiguration; projectId: string;
}) {
  const { settings, inherited_fields: inherited } = configuration;
  const origin = (field: string) => settings.overrides.some(item => item.field === field) ? 'Service override'
    : inherited.includes(field) ? 'From Product profile' : 'Added for DevOps';
  const inScope = (ref: RecordReference) => ref.tenant_id === configuration.scope.tenant_id && ref.project_id === projectId;
  const environmentResources = settings.devops.accounts.flatMap(account => {
    const environments = (account as { environments?: { resource_refs?: RecordReference[] }[] }).environments ?? [];
    return environments.flatMap(environment => environment.resource_refs ?? []);
  });
  const triggers = (settings.devops.trigger_policies as { kind?: string; enabled?: boolean; capability?: string; resource_refs?: RecordReference[]; telemetry_refs?: RecordReference[] }[])
    .filter(trigger => trigger.enabled && trigger.capability === 'investigation');
  const triggerResources = triggers.flatMap(trigger => trigger.resource_refs ?? []);
  const telemetry = [...new Map(triggers.flatMap(trigger => trigger.telemetry_refs ?? []).filter(inScope).map(ref => [ref.record_id, ref])).values()];
  const resources = [...new Map([...triggerResources, ...environmentResources].filter(inScope).map(ref => [ref.record_id, ref])).values()];
  const rows = [
    { label: 'Environment', value: settings.source_refs.map(ref => ref.record_id).join(', ') || 'Choose an environment', source: origin('source_refs') },
    { label: 'Region', value: settings.devops.region || 'Choose a region', source: origin('region') },
    { label: 'Workload', value: resources.map(ref => workloadName(ref.record_id)).join(', ') || 'Select a workload and its logs', source: resources.length ? 'Added for DevOps' : 'Needs your input' },
    { label: 'CloudWatch logs', value: telemetry.map(ref => ref.record_id.split(':log-group:').pop()?.replace(/:\*$/, '')).join(', ') || 'No logs selected', source: telemetry.length ? 'Added for DevOps' : 'Needs your input' },
  ];
  return <section className="devops-saved-overview" aria-label="DevOps setup overview">
    <dl className="devops-saved-settings">{rows.map(row => <div key={row.label}>
      <dt>{row.label}</dt><dd>{row.value}<small>{row.source}</small></dd>
    </div>)}</dl>
  </section>;
}

export function DevOpsSavedReferences({ configuration, productId, projectId }: {
  configuration: DevOpsConfiguration; productId: string; projectId: string;
}) {
  const { settings, inherited_fields: inherited, gaps } = configuration;
  return (
    <section className="devops-reference-details" aria-label="Saved references and profile details">
      <p>Configuration revision {configuration.revision} · Product profile revision {settings.profile_ref.record_revision}.</p>
      <a href={`/products/${encodeURIComponent(productId)}/projects/${encodeURIComponent(projectId)}`}>View Product profile</a>
      <p>Inherited from Profile: {inherited.join(', ') || 'none'}</p>
      <p>{'purpose' in settings && typeof settings.purpose === 'string' ? settings.purpose : 'No purpose saved.'}</p>
      <p>Data classification: {settings.classification || 'Not provided'}</p>
      <p>Configuration: {configuration.state}; handoff: {configuration.handoff_state}.</p>
      <p>Missing inputs: {gaps.join(', ') || 'none reported; check current run readiness before starting.'}</p>
      {settings.overrides.length > 0 && <ul>{settings.overrides.map((override, index) => <li key={index}>
        {override.field.replace(/_/g, ' ')} override: {override.reason}
      </li>)}</ul>}
      <dl className="devops-technical"><dt>Connection</dt><dd>{settings.devops.hosting_connection_ref.record_id}</dd>
        <dt>Saved source revisions</dt><dd>{settings.source_refs.map(ref => <p key={`${ref.record_type}:${ref.record_id}`}>
          {ref.record_id} · revision {ref.record_revision}</p>)}</dd></dl>
      <p className="product-muted">Product profile changes need review before they are applied to this saved setup.</p>
    </section>
  );
}
