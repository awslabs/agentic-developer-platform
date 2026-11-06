import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { listProductSources } from '@/services/products';
import SecurityConfiguration from '@/pages/products/services/SecurityConfiguration';
import ProductServices from '@/pages/products/services/ProductServices';
import { getSecurityBinding, getSecurityBindingOperation, getSecurityConfiguration, getSecurityOptions, getSecurityResult, listSecurityFindings, listSecurityBindings, listSecurityRuns, listSecurityResults, reconcileSecurityBindingOperation, saveSecurityConfiguration, startSecurityBindingOperation } from '@/services/l5Services';

vi.mock('@/services/l5Services', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/l5Services')>(), getSecurityConfiguration: vi.fn(), getSecurityOptions: vi.fn(), listSecurityBindings: vi.fn(), listSecurityRuns: vi.fn(), listSecurityResults: vi.fn(), saveSecurityConfiguration: vi.fn(),
  getSecurityBinding: vi.fn(), getSecurityBindingOperation: vi.fn(), getSecurityResult: vi.fn(), listSecurityFindings: vi.fn(), reconcileSecurityBindingOperation: vi.fn(), startSecurityBindingOperation: vi.fn() }));

vi.mock('@/services/products', async importOriginal => ({ ...await importOriginal<typeof import('@/services/products')>(), listProductSources: vi.fn() }));

const reference = { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'connection', record_id: 'connection-one', record_revision: '7' };
const options = { connections: [reference, { ...reference, record_id: 'connection-two', source_connection_id: 'connection-two' }], sources: [], regions: [], capabilities: ['threat_model' as const], unavailable_reasons: ['region_options_unavailable'] };
const configuration = { value: { scope: { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '5' }, configuration_id: 'config-one', revision: 2, state: 'needs_input', handoff_state: 'linked', inherited_fields: ['purpose', 'classification', 'source_refs'], gaps: ['region'], hash: 'old', settings: { purpose: 'Secure collaboration tools', classification: 'Confidential', profile_ref: { ...reference, record_type: 'product', record_id: 'product-one', record_revision: '4' }, capabilities: ['threat_model' as const], source_refs: [{ ...reference, record_type: 'document', record_revision: '12' }], enabled: false, visibility: 'only_me' as const, exclusions: [], overrides: [], triggers: [] } }, etag: '"old"' };

describe('Security configuration', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listProductSources).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(getSecurityOptions).mockResolvedValue(options);
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [] });
    vi.mocked(listSecurityRuns).mockResolvedValue({ items: [] });
    vi.mocked(listSecurityResults).mockResolvedValue({ items: [] });
    vi.mocked(getSecurityConfiguration).mockResolvedValue(configuration as never);
    vi.mocked(getSecurityBinding).mockImplementation(async (_product, _project, id) => ({ value: (await listSecurityBindings('product-one', 'project-one')).items.find(item => item.binding_id === id)!, etag: '"1"' }));
  });

  it('keeps three sections mounted with visible setup choices and focused workspace tools', async () => {
    const { container } = render(<ProductServices productId="product-one" projectId="project-one" tenantId="tenant-one" />);
    await screen.findByText('Secure collaboration tools');
    const navigation = within(screen.getByRole('navigation', { name: 'Security service sections' }));
    expect(navigation.getAllByRole('link')).toHaveLength(3);
    for (const title of ['Setup', 'Runs & schedules', 'Results']) {
      await userEvent.click(navigation.getByRole('link', { name: title }));
      expect(screen.getByRole('region', { name: title })).toBeVisible();
    }
    await userEvent.click(navigation.getByRole('link', { name: 'Setup' }));
    expect(screen.getByLabelText('AWS account')).toBeVisible();
    expect(screen.getByRole('checkbox', { name: 'Threat modeling' })).toBeChecked();
    expect(screen.getByText('Pen testing · Not available yet')).toBeVisible();
    expect(screen.queryByRole('checkbox', { name: /pen.?test/i })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Maximum cost per assessment (USD)')).not.toBeVisible();
    const save = screen.getByRole('button', { name: 'Save setup' }) as HTMLButtonElement;
    expect(save.form).toBe(screen.getByLabelText('AWS account').closest('form'));
    await userEvent.click(screen.getByRole('button', { name: 'Connect workspace' }));
    expect(screen.getByRole('region', { name: 'Security bindings' })).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Close Security workspace' }));
    await userEvent.click(navigation.getByRole('link', { name: 'Runs & schedules' }));
    expect(screen.getByRole('region', { name: 'Security assessments' })).toBeVisible();
    expect(screen.getByLabelText('Enable scheduled threat_model')).toBeVisible();
    expect(container.querySelector('form form')).toBeNull();
  });

  it('keeps all sections navigable before the first configuration without loading provider records', async () => {
    vi.mocked(getSecurityConfiguration).mockRejectedValue({ status: 404 });
    render(<ProductServices productId="product-one" projectId="project-one" tenantId="tenant-one" />);
    await screen.findByText(/Start with the three sections below/);
    for (const title of ['Setup', 'Runs & schedules', 'Results']) {
      await userEvent.click(screen.getByRole('link', { name: title }));
      expect(screen.getByRole('region', { name: title })).toBeInTheDocument();
    }
    await userEvent.click(screen.getByRole('link', { name: 'Setup' }));
    expect(screen.getByLabelText('AWS account')).toBeVisible();
    expect(listSecurityBindings).not.toHaveBeenCalled();
    expect(listSecurityRuns).not.toHaveBeenCalled();
    expect(listSecurityResults).not.toHaveBeenCalled();
  });

  it('keeps advanced controls collapsed and mounted without losing edits when reopened', async () => {
    render(<ProductServices productId="product-one" projectId="project-one" tenantId="tenant-one" />);
    await screen.findByText('Secure collaboration tools');
    const monitoring = screen.getByText('Monitoring preferences');
    const verification = screen.getByText('Advanced release verification');
    const interval = screen.getByLabelText('Poll interval (seconds)');
    const request = screen.getByLabelText('Request ID');
    expect(interval).not.toBeVisible();
    expect(request).not.toBeVisible();
    await userEvent.click(screen.getByRole('link', { name: 'Runs & schedules' }));
    await userEvent.click(monitoring);
    expect(interval).toBeVisible();
    await userEvent.clear(interval);
    await userEvent.type(interval, '120');
    await userEvent.click(monitoring);
    await userEvent.click(screen.getByRole('link', { name: 'Setup' }));
    await userEvent.click(screen.getByRole('button', { name: 'Advanced settings', exact: true }));
    await userEvent.click(verification);
    expect(screen.getByText(/it does not authorize Security assessments/)).toBeVisible();
    await userEvent.type(request, 'request-one');
    await userEvent.click(verification);
    expect(request).not.toBeVisible();
    await userEvent.click(verification);
    expect(request).toHaveValue('request-one');
    await userEvent.click(screen.getByRole('button', { name: 'Close Advanced Security settings' }));
    await userEvent.click(screen.getByRole('link', { name: 'Runs & schedules' }));
    await userEvent.click(monitoring);
    expect(interval).toBeVisible();
    expect(interval).toHaveValue(120);
    expect(saveSecurityConfiguration).not.toHaveBeenCalled();
    expect(startSecurityBindingOperation).not.toHaveBeenCalled();
  });

  it('excludes unsupported capabilities from setup, triggers and assessment selectors even when returned by the server', async () => {
    vi.mocked(getSecurityOptions).mockResolvedValue({ ...options, capabilities: ['threat_model', 'pentesting', 'unknown'] } as never);
    vi.mocked(getSecurityConfiguration).mockResolvedValue({ ...configuration, value: { ...configuration.value,
      settings: { ...configuration.value.settings, capabilities: ['threat_model', 'pentesting', 'unknown'] },
    } } as never);
    vi.mocked(saveSecurityConfiguration).mockResolvedValue(configuration as never);
    render(<ProductServices productId="product-one" projectId="project-one" tenantId="tenant-one" />);
    await screen.findByText('Secure collaboration tools');
    expect(screen.getByRole('checkbox', { name: 'Threat modeling' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Code scanning' })).toBeDisabled();
    expect(screen.queryByRole('checkbox', { name: /pentesting|unknown/i })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('link', { name: 'Runs & schedules' }));
    for (const label of ['Trigger capability', 'Assessment capability']) {
      const selector = within(screen.getByLabelText(label));
      expect(selector.getAllByRole('option')).toHaveLength(2);
      expect(selector.queryByRole('option', { name: /pentesting|unknown/ })).not.toBeInTheDocument();
    }
    expect(screen.queryByLabelText('Enable scheduled pentesting')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('link', { name: 'Setup' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    await waitFor(() => expect(saveSecurityConfiguration).toHaveBeenCalledWith('product-one', 'project-one', expect.objectContaining({ capabilities: ['threat_model'] }), '"old"', undefined, undefined));
  });

  it('retains unsaved choices across section links and saves setup, capabilities and schedules with the original ETag', async () => {
    const document = { ...reference, record_type: 'document', record_id: 'design-one', record_revision: '12' };
    const commit = { ...reference, record_type: 'repository_commit', record_id: 'repo-one', record_revision: 'abc123' };
    const current = { ...configuration, value: { ...configuration.value, settings: {
      ...configuration.value.settings, connection_ref: reference, region: 'us-east-1', enabled: true, source_refs: [document, commit],
    } } };
    vi.mocked(getSecurityOptions).mockResolvedValue({ ...options, sources: [document, commit], capabilities: ['threat_model', 'code_review'] });
    vi.mocked(getSecurityConfiguration).mockResolvedValue(current as never);
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [{ scope: configuration.value.scope,
      configuration_id: 'config-one', binding_id: 'binding-one', state: 'ready', readiness: [], gaps: [],
    }] } as never);
    vi.mocked(saveSecurityConfiguration).mockResolvedValue({ value: { ...current.value, revision: 3 }, etag: '"new"' } as never);
    const { container } = render(<ProductServices productId="product-one" projectId="project-one" tenantId="tenant-one" />);
    await screen.findByText('Secure collaboration tools');
    const region = screen.getByLabelText('AWS region');
    await userEvent.clear(region);
    await userEvent.type(region, 'us-west-2');
    await userEvent.type(screen.getByLabelText('Why use these Security settings?'), 'Use the verified regional connection');
    const navigation = within(screen.getByRole('navigation', { name: 'Security service sections' }));
    const codeReview = screen.getByRole('checkbox', { name: 'Code scanning' });
    await userEvent.click(codeReview);
    await userEvent.click(navigation.getByRole('link', { name: 'Runs & schedules' }));
    await userEvent.click(screen.getByLabelText('Enable scheduled code_review'));
    await userEvent.type(screen.getByLabelText('Daily UTC time for code_review'), '04:30');
    await userEvent.selectOptions(screen.getByLabelText('Schedule binding for code_review'), 'binding-one');
    await userEvent.click(within(screen.getByLabelText('Scheduled code_review')).getByRole('checkbox', { name: 'repo-one (revision abc123)' }));
    await userEvent.click(navigation.getByRole('link', { name: 'Results' }));
    await userEvent.click(navigation.getByRole('link', { name: 'Setup' }));
    expect(region).toHaveValue('us-west-2');
    expect(codeReview).toBeChecked();
    expect(screen.getByLabelText('Daily UTC time for code_review')).toHaveValue('04:30');
    const save = screen.getByRole('button', { name: 'Save setup' }) as HTMLButtonElement;
    expect(save.form).toBe(region.closest('form'));
    expect((codeReview as HTMLInputElement).form).toBe(save.form);
    expect(container.querySelector('form form')).toBeNull();
    await userEvent.click(save);
    await waitFor(() => expect(saveSecurityConfiguration).toHaveBeenCalledWith('product-one', 'project-one', expect.objectContaining({
      capabilities: ['threat_model', 'code_review'], region: 'us-west-2',
      triggers: [expect.objectContaining({ kind: 'schedule', enabled: true, capability: 'code_review', binding_id: 'binding-one', source_refs: [commit], schedule: '30 4 * * *' })],
    }), '"old"', undefined, undefined));
    expect(getSecurityConfiguration).toHaveBeenCalledTimes(1);
    expect(startSecurityBindingOperation).not.toHaveBeenCalled();
  });

  it('shows inherited fields and saves explicit choices without starting a scan', async () => {
    vi.mocked(saveSecurityConfiguration).mockResolvedValue({ value: { ...configuration.value, revision: 3 }, etag: '"new"' } as never);
    render(<SecurityConfiguration productId="product-one" projectId="project-one" />);
    expect(await screen.findByText('Secure collaboration tools')).toBeInTheDocument();
    expect(screen.getByText('Confidential')).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('AWS account'), '1');
    await userEvent.type(screen.getByLabelText('AWS region'), 'us-west-2');
    await userEvent.type(screen.getByLabelText('Why use these Security settings?'), 'Select the verified AWS connection and region');
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    await waitFor(() => expect(saveSecurityConfiguration).toHaveBeenCalledWith('product-one', 'project-one', expect.objectContaining({ capabilities: ['threat_model'], region: 'us-west-2', connection_ref: options.connections[1], overrides: expect.arrayContaining([expect.objectContaining({ field: 'region', reason: 'Select the verified AWS connection and region' }), expect.objectContaining({ field: 'connection_ref' })]) }), '"old"', undefined, undefined));
    expect(vi.mocked(saveSecurityConfiguration).mock.calls[0][2]).not.toHaveProperty('source_refs');
    expect(await screen.findByText(/Security setup saved/)).toBeInTheDocument();
  });

  it('requires an override reason for explicitly selected source revisions', async () => {
    vi.mocked(getSecurityOptions).mockResolvedValue({ ...options, sources: [{ ...reference, record_type: 'document', record_id: 'document-one', record_revision: '15' }] });
    vi.mocked(getSecurityConfiguration).mockRejectedValue({ status: 404 });
    vi.mocked(saveSecurityConfiguration).mockResolvedValue(configuration as never);
    render(<SecurityConfiguration productId="product-one" projectId="project-one" />);
    await userEvent.click(await screen.findByLabelText('Threat modeling'));
    await userEvent.click(screen.getByLabelText(/document-one/));
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Add a short reason for changing');
    expect(saveSecurityConfiguration).not.toHaveBeenCalled();
    await userEvent.type(screen.getByLabelText('Why use these Security settings?'), 'Scope the assessment to the selected document revision');
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    await waitFor(() => expect(saveSecurityConfiguration).toHaveBeenCalledWith('product-one', 'project-one', expect.objectContaining({
      source_refs: [{ ...reference, record_type: 'document', record_id: 'document-one', record_revision: '15' }],
      overrides: [expect.objectContaining({ field: 'source_refs', reason: 'Scope the assessment to the selected document revision' })],
    }), undefined, undefined, undefined));
  });

  it('pins an enabled manual trigger to one ready binding, capability and source with a monitoring policy', async () => {
    const documentRef = { ...reference, record_type: 'document', record_id: 'strategy', record_revision: '12' };
    const ready = { ...configuration.value, settings: { ...configuration.value.settings, connection_ref: reference,
      source_refs: [documentRef], region: 'us-east-1', enabled: true, monitoring: { poll_interval_seconds: 600, poll_grace_seconds: 300 } } };
    vi.mocked(getSecurityOptions).mockResolvedValue({ ...options, sources: [documentRef] });
    vi.mocked(getSecurityConfiguration).mockResolvedValue({ value: ready, etag: '"four"' } as never);
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [{ scope: configuration.value.scope, binding_id: 'binding-one', configuration_id: 'config-one', revision: 1, generation: 1,
      state: 'ready', ownership: 'adp_created', connection_ref: reference, gaps: [], readiness: [] }] });
    vi.mocked(saveSecurityConfiguration).mockResolvedValue({ value: { ...ready, revision: 3 }, etag: '"five"' } as never);
    render(<SecurityConfiguration productId="product-one" projectId="project-one" />);
    await within(await screen.findByLabelText('Trigger binding')).findByRole('option', { name: /Security connection/ });
    await userEvent.click(screen.getByText('Manual assessment access'));
    await userEvent.click(screen.getByLabelText('Allow manual assessments'));
    await userEvent.selectOptions(screen.getByLabelText('Trigger capability'), 'threat_model');
    await userEvent.selectOptions(screen.getByLabelText('Trigger binding'), 'binding-one');
    await userEvent.click(within(screen.getByRole('group', { name: 'Trigger source revisions' })).getByRole('checkbox', { name: /strategy/ }));
    await userEvent.click(screen.getByText('Monitoring preferences'));
    await userEvent.clear(screen.getByLabelText('Poll interval (seconds)'));
    await userEvent.type(screen.getByLabelText('Poll interval (seconds)'), '120');
    await userEvent.clear(screen.getByLabelText('Poll grace (seconds)'));
    await userEvent.type(screen.getByLabelText('Poll grace (seconds)'), '180');
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    await waitFor(() => expect(saveSecurityConfiguration).toHaveBeenCalledWith('product-one', 'project-one', expect.objectContaining({
      triggers: [expect.objectContaining({ kind: 'manual', enabled: true, capability: 'threat_model', binding_id: 'binding-one', source_refs: [documentRef] })],
      monitoring: { poll_interval_seconds: 120, poll_grace_seconds: 180 },
    }), '"four"', undefined, undefined));
  });

  it('requires a selected capability and reports unavailable choices', async () => {
    vi.mocked(getSecurityConfiguration).mockRejectedValue({ status: 404 });
    render(<SecurityConfiguration productId="product-one" projectId="project-one" />);
    expect(await screen.findByLabelText('AWS region')).toHaveAttribute('placeholder', 'For example, us-east-1');
    expect(screen.getByRole('checkbox', { name: 'Code scanning' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save setup' })).toBeDisabled();
    expect(saveSecurityConfiguration).not.toHaveBeenCalled();
  });
  it('does not replay a stale configuration edit after refreshing the CLI revision', async () => {
    const latest = { ...configuration.value, revision: 3, settings: { ...configuration.value.settings, region: 'us-east-1' } };
    vi.mocked(getSecurityConfiguration).mockResolvedValueOnce(configuration as never).mockResolvedValueOnce({ value: latest, etag: '"fresh"' } as never);
    vi.mocked(saveSecurityConfiguration).mockRejectedValueOnce({ status: 412 }).mockResolvedValueOnce({ value: { ...latest, revision: 4 }, etag: '"saved"' } as never);
    const onRead = vi.fn();
    render(<SecurityConfiguration productId="product-one" projectId="project-one" onRead={onRead} />);
    await screen.findByText('Secure collaboration tools');
    await userEvent.type(screen.getByLabelText('AWS region'), 'us-west-2');
    await userEvent.type(screen.getByLabelText('Why use these Security settings?'), 'Old intent');
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    await waitFor(() => expect(saveSecurityConfiguration).toHaveBeenCalledWith('product-one', 'project-one', expect.objectContaining({ region: 'us-west-2' }), '"old"', undefined, undefined));
    expect(await screen.findByRole('alert')).toHaveTextContent('changed elsewhere');
    expect(onRead).toHaveBeenLastCalledWith(null, null);
    expect(screen.getByRole('button', { name: 'Save setup' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Reload setup' }));
    expect(await screen.findByLabelText('AWS region')).toHaveValue('us-east-1');
    expect(screen.getByText(/stale changes were not replayed/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save setup' })).toBeDisabled();
    expect(saveSecurityConfiguration).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText('Why use these Security settings?')).not.toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText('AWS region'));
    await userEvent.type(screen.getByLabelText('AWS region'), 'us-west-1');
    await userEvent.type(screen.getByLabelText('Why use these Security settings?'), 'New intent');
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    await waitFor(() => expect(saveSecurityConfiguration).toHaveBeenCalledTimes(2));
    expect(vi.mocked(saveSecurityConfiguration).mock.calls[1]).toMatchObject(['product-one', 'project-one', { region: 'us-west-1' }, '"fresh"', undefined, undefined]);
    expect(vi.mocked(saveSecurityConfiguration).mock.calls[1][2]).not.toHaveProperty('source_refs');
  });

  it('removes inherited Security data when the configuration has another Product scope', async () => {
    const onAccessDenied = vi.fn();
    const onRead = vi.fn();
    vi.mocked(getSecurityConfiguration).mockResolvedValue({ value: { ...configuration.value, scope: { ...configuration.value.scope, product_id: 'foreign-product' } }, etag: '"foreign"' } as never);
    render(<SecurityConfiguration productId="product-one" projectId="project-one" onRead={onRead} onAccessDenied={onAccessDenied} />);
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalled());
    expect(onRead).toHaveBeenCalledWith(null, null);
    expect(screen.queryByText('Secure collaboration tools')).not.toBeInTheDocument();
  });

  it('rejects a configuration for a different tenant even when its Product and Project IDs match', async () => {
    vi.mocked(getSecurityConfiguration).mockResolvedValue({ value: { ...configuration.value,
      scope: { ...configuration.value.scope, tenant_id: 'tenant-other' } }, etag: '"foreign"' } as never);
    render(<ProductServices productId="product-one" projectId="project-one" tenantId="tenant-one" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details have been removed');
    expect(screen.queryByText('Secure collaboration tools')).not.toBeInTheDocument();
    expect(listSecurityResults).not.toHaveBeenCalled();
  });

  it('rejects a Product profile reference that points to another Product', async () => {
    const onAccessDenied = vi.fn();
    vi.mocked(getSecurityConfiguration).mockResolvedValue({ value: { ...configuration.value, settings: {
      ...configuration.value.settings, profile_ref: { ...reference, record_type: 'product', record_id: 'product-other' },
    } }, etag: '"foreign"' } as never);
    render(<SecurityConfiguration productId="product-one" projectId="project-one" tenantId="tenant-one" onAccessDenied={onAccessDenied} />);
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledOnce());
    expect(screen.queryByText('Secure collaboration tools')).not.toBeInTheDocument();
  });

  it('removes previously loaded configuration when a refresh returns non-enumerating 404', async () => {
    const onAccessDenied = vi.fn();
    vi.mocked(getSecurityConfiguration).mockResolvedValueOnce(configuration as never).mockRejectedValueOnce({ status: 404 });
    render(<SecurityConfiguration productId="product-one" projectId="project-one" onAccessDenied={onAccessDenied} />);
    await screen.findByText('Secure collaboration tools');
    await userEvent.click(screen.getByRole('button', { name: 'Reload setup' }));
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledOnce());
    expect(screen.queryByText('Secure collaboration tools')).not.toBeInTheDocument();
  });

  it('does not show an unauthorized source or binding choice from a configuration read', async () => {
    const onAccessDenied = vi.fn();
    vi.mocked(getSecurityOptions).mockResolvedValue({ ...options,
      sources: [{ ...reference, tenant_id: 'tenant-other', record_type: 'document' }] });
    render(<SecurityConfiguration productId="product-one" projectId="project-one" tenantId="tenant-one" onAccessDenied={onAccessDenied} />);
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledOnce());
    expect(screen.queryByLabelText(/document connection-one/)).not.toBeInTheDocument();
    expect(screen.queryByText('Secure collaboration tools')).not.toBeInTheDocument();
  });

  it('clears every Security panel after a denied result read and only restores access on a fresh load', async () => {
    vi.mocked(listSecurityResults).mockResolvedValueOnce({ items: [{ result_id: 'result-one', run_id: 'run-one', scope: configuration.value.scope, finding_count: 93 } as never] })
      .mockRejectedValueOnce({ status: 403 }).mockResolvedValueOnce({ items: [] });
    render(<ProductServices productId="product-one" projectId="project-one" tenantId="tenant-one" />);
    await userEvent.click(screen.getByRole('link', { name: 'Results' }));
    await screen.findByRole('option', { name: /result-one/ });
    expect(screen.getByRole('region', { name: 'Security configuration' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Security results' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details have been removed');
    expect(screen.queryByRole('region', { name: 'Security configuration' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Security results' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /result-one/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Security access' }));
    expect(await screen.findByRole('region', { name: 'Security configuration' })).toBeInTheDocument();
    await screen.findByText('Secure collaboration tools');
    expect(screen.queryByRole('option', { name: /result-one/ })).not.toBeInTheDocument();
  });

  it.each([403, 404])('clears the entire workspace after reconciliation returns %s until an explicit fresh read', async (status) => {
    const unknown = { operation_id: 'operation-one', action: 'create', state: 'outcome_unknown', revision: 1, gaps: [] };
    const result = { result_id: 'result-one', run_id: 'run-one', scope: configuration.value.scope, finding_count: 93,
      native_job_ref: reference, observed_at: '2026-10-27T12:00:00Z',
      coverage: 'partial', coverage_mode: 'documents', provider_outcome: 'failed', revision: 1,
      admission: { adapter_revision: 'v1', binding_generation: 1, source_snapshots: [] } };
    vi.mocked(getSecurityConfiguration).mockResolvedValue({ ...configuration, value: { ...configuration.value,
      settings: { ...configuration.value.settings, connection_ref: reference, region: 'us-east-1' } } } as never);
    vi.mocked(listSecurityResults).mockResolvedValue({ items: [result] } as never);
    vi.mocked(getSecurityResult).mockResolvedValue(result as never);
    vi.mocked(listSecurityFindings).mockResolvedValue({ items: [] });
    vi.mocked(startSecurityBindingOperation).mockResolvedValue({ value: unknown, etag: '"1"' } as never);
    vi.mocked(getSecurityBindingOperation).mockResolvedValue(unknown as never);
    vi.mocked(reconcileSecurityBindingOperation).mockRejectedValue({ status });
    render(<ProductServices productId="product-one" projectId="project-one" tenantId="tenant-one" />);
    await userEvent.click(screen.getByRole('link', { name: 'Results' }));
    await screen.findByRole('option', { name: /result-one/ });
    await userEvent.selectOptions(screen.getByLabelText('Visible results'), 'result-one');
    await screen.findByText(/Reported finding count: 93/);
    const panels = ['Security configuration', 'Security bindings', 'Security assessments', 'Security results'];
    for (const panel of panels) expect(document.querySelector(`[aria-label="${panel}"]`)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('link', { name: 'Setup' }));
    await userEvent.click(screen.getByRole('button', { name: 'Connect workspace' }));
    await userEvent.selectOptions(screen.getByLabelText('Connection method'), 'create');
    await userEvent.click(screen.getByRole('button', { name: 'Create workspace' }));
    await userEvent.type(await screen.findByLabelText('Reason to reconcile'), 'Check original operation');
    await userEvent.click(screen.getByRole('button', { name: 'Reconcile original operation' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details have been removed');
    for (const panel of panels) {
      expect(screen.queryByRole('region', { name: panel })).not.toBeInTheDocument();
    }
    expect(screen.queryByText(/Reported finding count:|operation operation-one/)).not.toBeInTheDocument();
    expect(getSecurityConfiguration).toHaveBeenCalledTimes(1);
    vi.mocked(listSecurityResults).mockResolvedValue({ items: [] });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Security access' }));
    await screen.findByText('Secure collaboration tools');
    expect(getSecurityConfiguration).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/Reported finding count:|operation operation-one/)).not.toBeInTheDocument();
    expect(startSecurityBindingOperation).toHaveBeenCalledTimes(1);
  });

});
