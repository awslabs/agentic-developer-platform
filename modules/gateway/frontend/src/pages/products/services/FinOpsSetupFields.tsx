import type { RecordReference } from '@/services/products';

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const string = (value: unknown) => typeof value === 'string' ? value : '';

/** Edit the existing draft without dropping its advanced, revisioned selections. */
export default function FinOpsSetupFields({ value, onChange, connections, regions }: {
  value: string; onChange: (value: string) => void; connections: RecordReference[]; regions: string[];
}) {
  let draft: Record<string, unknown>;
  try { const parsed: unknown = JSON.parse(value); if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(); draft = record(parsed); }
  catch { return <p>Correct the JSON in Advanced settings before editing the setup fields.</p>; }
  const finops = record(draft.finops);
  const billing = record(finops.billing_scope);
  const overrides = Array.isArray(draft.overrides) ? draft.overrides.map(record) : [];
  const selectedReference = record(draft.connection_ref);
  const selected = connections.findIndex(ref => (['tenant_id', 'project_id', 'source_connection_id', 'record_type', 'record_id', 'record_revision'] as const)
    .every(field => ref[field] === selectedReference[field]));
  const update = (patch: Record<string, unknown>) => onChange(JSON.stringify({ ...draft, ...patch }, null, 2));
  const updateFinops = (patch: Record<string, unknown>) => update({ finops: { ...finops, ...patch } });
  const choose = (field: 'region' | 'connection_ref', selection: unknown) => {
    const previous = overrides.find(item => item.field === field);
    const reason = previous?.reason ?? overrides.find(item => ['region', 'connection_ref'].includes(String(item.field)))?.reason ?? '';
    update({ [field]: selection, overrides: [...overrides.filter(item => item.field !== field), { field, value: selection, reason }] });
  };
  return <div className="space-y-3">
    <label>AWS connection <select value={selected} onChange={event => choose('connection_ref', connections[Number(event.target.value)])}>
      <option value={-1} disabled>Choose an authorized connection</option>
      {connections.map((ref, index) => <option key={index} value={index}>{ref.record_id} (revision {ref.record_revision})</option>)}
    </select></label>
    <label>AWS Region <input list="finops-regions" value={string(draft.region)} onChange={event => choose('region', event.target.value)} /></label>
    <datalist id="finops-regions">{regions.map(region => <option key={region} value={region} />)}</datalist>
    <label>Reason for connection or Region choice <input disabled={!overrides.some(item => ['region', 'connection_ref'].includes(String(item.field)))} placeholder="Choose a connection or Region first" value={string(overrides.find(item => ['region', 'connection_ref'].includes(String(item.field)))?.reason)} onChange={event => update({ overrides: overrides.map(item => ['region', 'connection_ref'].includes(String(item.field)) ? { ...item, reason: event.target.value } : item) })} /></label>
    <label>Agent setup <select value={string(finops.agent_mode)} onChange={event => updateFinops({ agent_mode: event.target.value })}>
      <option value="">Choose setup mode</option><option value="adopt">Use an existing FinOps agent</option><option value="create" disabled>Create a new agent — Not available yet</option>
    </select></label>
    <label>Existing FinOps agent ID <input value={string(finops.native_agent_ref)} onChange={event => updateFinops({ native_agent_ref: event.target.value })} /></label>
    <label>Agent display name <input value={string(finops.native_agent_display_name)} onChange={event => updateFinops({ native_agent_display_name: event.target.value })} /></label>
    <label>Billing account IDs (one per line) <textarea value={Array.isArray(billing.account_ids) ? billing.account_ids.join('\n') : ''}
      onChange={event => updateFinops({ billing_scope: { ...billing, account_ids: event.target.value.split(/[\n,]/) } })}
      onBlur={event => updateFinops({ billing_scope: { ...billing, account_ids: event.target.value.split(/[\n,]/).map(item => item.trim()).filter(Boolean) } })} /></label>
    <label>Billing view reference <input value={string(billing.view_ref)} onChange={event => updateFinops({ billing_scope: { ...billing, view_ref: event.target.value } })} /></label>
    <label>Billing data role reference <input value={string(finops.data_role_ref)} onChange={event => updateFinops({ data_role_ref: event.target.value })} /></label>
    <p className="product-muted">The provider must verify this agent and its billing access before an inquiry can run. Execution limits and any existing policies are retained in Advanced settings.</p>
  </div>;
}
