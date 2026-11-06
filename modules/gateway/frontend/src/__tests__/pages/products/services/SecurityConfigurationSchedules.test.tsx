import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SecurityConfiguration from '@/pages/products/services/SecurityConfiguration';
import { getSecurityConfiguration, getSecurityOptions, listSecurityBindings, saveSecurityConfiguration } from '@/services/l5Services';

vi.mock('@/services/l5Services', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/l5Services')>(),
  getSecurityConfiguration: vi.fn(), getSecurityOptions: vi.fn(),
  listSecurityBindings: vi.fn(), saveSecurityConfiguration: vi.fn(),
}));
vi.mock('@/pages/products/services/SecurityEvidencePanel', () => ({ default: () => null }));

const scope = { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '5' };
const connection = { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'connection', record_id: 'connection-one', record_revision: '7' };
const documentSource = { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'document', record_id: 'design-doc', record_revision: '11' };
const commitSource = { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'repository_commit', record_id: 'repo-one', record_revision: 'abc123' };
const options = { connections: [connection], sources: [documentSource, commitSource], regions: ['us-east-1'], capabilities: ['threat_model', 'code_review'], unavailable_reasons: [] };

// These are the readiness entries SecurityBindingAdapter.read actually stores:
// an observed source registration entry plus an optional threat-model entry.
// Neither carries capability, state or reason_codes.
const registrationEntry = {
  kind: 'code_review_source_registration',
  registration: { service_role: 'arn:aws:iam::111122223333:role/adp-security', source_type: 'codeconnections' },
  observed_at: '2026-10-31T09:40:00+00:00',
};
const modelEntry = {
  kind: 'threat_model',
  native_model_ref: { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'threat_model', record_id: 'model-one', record_revision: '2026-10-01T00:00:00+00:00' },
  observed_at: '2026-10-31T09:40:01+00:00',
};
const binding = (bindingId: string, readiness: unknown[]) => ({
  scope, binding_id: bindingId, configuration_id: 'config-one', revision: 2, generation: 3,
  state: 'ready', ownership: 'adp_created', connection_ref: connection, gaps: [], readiness,
});
const modelBinding = binding('binding-model', [registrationEntry, modelEntry]);
const codeBinding = binding('binding-code', [registrationEntry]);

const configurationValue = {
  scope, configuration_id: 'config-one', revision: 4, state: 'needs_input', inherited_fields: [], gaps: [],
  handoff_state: 'linked', hash: 'hash-four',
  settings: {
    profile_ref: { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'product', record_id: 'product-one', record_revision: '3' },
    source_refs: [documentSource, commitSource], connection_ref: connection, region: 'us-east-1',
    capabilities: ['threat_model', 'code_review'], enabled: true, visibility: 'only_me', exclusions: [], overrides: [],
    triggers: [
      { trigger_id: 'trigger-threat', kind: 'schedule', enabled: true, capability: 'threat_model', binding_id: 'binding-model', source_refs: [documentSource], debounce_seconds: 0, schedule: '30 2 * * *' },
      { trigger_id: 'trigger-code', kind: 'schedule', enabled: true, capability: 'code_review', binding_id: 'binding-code', source_refs: [commitSource], debounce_seconds: 0, schedule: '15 3 * * *' },
    ],
  },
};

const mount = () => render(<SecurityConfiguration productId="product-one" projectId="project-one" tenantId="tenant-one" />);

describe('Security scheduled assessments in the configuration page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getSecurityOptions).mockResolvedValue(options as never);
    vi.mocked(getSecurityConfiguration).mockResolvedValue({ value: configurationValue, etag: '"configuration-four"' } as never);
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [modelBinding, codeBinding] } as never);
    vi.mocked(saveSecurityConfiguration).mockResolvedValue({ value: { ...configurationValue, revision: 5 }, etag: '"configuration-five"' } as never);
  });

  it('renders the readiness the Security adapter really stores for a saved schedule', async () => {
    mount();
    const threatRow = await screen.findByLabelText('Scheduled threat_model');
    const codeRow = screen.getByLabelText('Scheduled code_review');
    // The previous rendering read item.capability/state/reason_codes.length and
    // threw a TypeError on these real entries before anything was displayed.
    expect(threatRow).toHaveTextContent('Binding readiness: code review source registration (observed 2026-10-31T09:40:00+00:00); threat model (model model-one, observed 2026-10-31T09:40:01+00:00)');
    expect(codeRow).toHaveTextContent('Binding readiness: code review source registration (observed 2026-10-31T09:40:00+00:00)');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Daily UTC time for threat_model')).toHaveValue('02:30');
    expect(screen.getByLabelText('Daily UTC time for code_review')).toHaveValue('03:15');
  });

  it.each<[Record<string, unknown>, string]>([
    [{ kind: 'threat_model_selection_required' }, 'threat model selection required'],
    [{ kind: 'threat_model_outcome_unknown', candidate_id: 'model-candidate' }, 'threat model outcome unknown (candidate model-candidate)'],
    // Tolerated for services that do report a capability/state matrix; Security
    // itself does not, so neither shape may crash this page.
    [{ capability: 'threat_model', state: 'blocked', reason_codes: ['source_readiness_unverified'] }, 'threat_model: blocked (source_readiness_unverified)'],
  ])('renders an unresolved readiness entry when a ready binding is selected', async (entry, expected) => {
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [binding('binding-other', [registrationEntry, entry])] } as never);
    mount();
    const threatRow = await screen.findByLabelText('Scheduled threat_model');
    expect(threatRow).toHaveTextContent('Binding readiness: select a ready binding');
    await userEvent.selectOptions(screen.getByLabelText('Schedule binding for threat_model'), 'binding-other');
    expect(threatRow).toHaveTextContent(expected);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });


  it('retains source, connection and trigger selections when reference fields arrive in a different order', async () => {
    const reordered = <T extends object,>(ref: T): T => Object.fromEntries(Object.entries(ref).reverse()) as T;
    const manual = { trigger_id: 'manual-code', kind: 'manual', enabled: true, capability: 'code_review', binding_id: 'binding-code', source_refs: [reordered(commitSource)], debounce_seconds: 0 };
    vi.mocked(getSecurityConfiguration).mockResolvedValue({ value: { ...configurationValue, settings: {
      ...configurationValue.settings, source_refs: configurationValue.settings.source_refs.map(reordered),
      connection_ref: reordered(connection), triggers: [...configurationValue.settings.triggers.map(trigger => ({ ...trigger, source_refs: trigger.source_refs.map(reordered) })), manual],
    } }, etag: '"configuration-four"' } as never);
    mount();
    await screen.findByLabelText('Scheduled threat_model');
    expect(screen.getByLabelText('AWS account')).toHaveValue('0');
    expect(within(screen.getByRole('group', { name: 'Source revisions for Security' })).getAllByRole('checkbox').every(input => (input as HTMLInputElement).checked)).toBe(true);
    expect(within(screen.getByRole('group', { name: 'Trigger source revisions' })).getByRole('checkbox')).toBeChecked();
    expect(within(screen.getByLabelText('Scheduled threat_model')).getByRole('checkbox', { name: /design-doc/ })).toBeChecked();
    expect(within(screen.getByLabelText('Scheduled code_review')).getByRole('checkbox', { name: /repo-one/ })).toBeChecked();
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    await waitFor(() => expect(saveSecurityConfiguration).toHaveBeenCalledTimes(1));
    const saved = vi.mocked(saveSecurityConfiguration).mock.calls[0][2];
    expect(saved.connection_ref).toEqual(connection);
    expect(saved.overrides).toEqual([]);
    expect(saved.triggers).toEqual(expect.arrayContaining([manual, ...configurationValue.settings.triggers]));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps a separate ready binding and pinned source per scheduled capability on save', async () => {
    mount();
    await screen.findByLabelText('Scheduled threat_model');
    expect(screen.getByLabelText('Schedule binding for threat_model')).toHaveValue('binding-model');
    expect(screen.getByLabelText('Schedule binding for code_review')).toHaveValue('binding-code');
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    await waitFor(() => expect(saveSecurityConfiguration).toHaveBeenCalledWith('product-one', 'project-one', expect.objectContaining({
      triggers: [
        { trigger_id: 'trigger-threat', kind: 'schedule', enabled: true, capability: 'threat_model', binding_id: 'binding-model', source_refs: [documentSource], debounce_seconds: 0, schedule: '30 2 * * *' },
        { trigger_id: 'trigger-code', kind: 'schedule', enabled: true, capability: 'code_review', binding_id: 'binding-code', source_refs: [commitSource], debounce_seconds: 0, schedule: '15 3 * * *' },
      ],
    }), '"configuration-four"', undefined, undefined));
    expect(await screen.findByText(/Security setup saved/)).toBeInTheDocument();
  });
  it('saves an edited daily time from the run settings action without changing pinned sources', async () => {
    mount();
    const input = await screen.findByLabelText('Daily UTC time for code_review');
    await userEvent.clear(input);
    await userEvent.type(input, '04:45');
    await userEvent.click(screen.getByRole('button', { name: 'Save run settings' }));
    await waitFor(() => expect(saveSecurityConfiguration).toHaveBeenCalledTimes(1));
    expect(vi.mocked(saveSecurityConfiguration).mock.calls[0][2].triggers).toEqual(expect.arrayContaining([
      expect.objectContaining({ trigger_id: 'trigger-code', schedule: '45 4 * * *', binding_id: 'binding-code', source_refs: [commitSource] }),
    ]));
  });

});
