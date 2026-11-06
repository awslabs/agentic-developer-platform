import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import WellArchitectedServiceTab from '@/pages/products/services/WellArchitectedServiceTab';
import { getWellArchitectedConfiguration, getWellArchitectedOptions, saveWellArchitectedConfiguration, validateWellArchitectedProviderSelection,
  listWellArchitectedBindings, listWellArchitectedRuns, listWellArchitectedResults,
  type WellArchitectedConfiguration, type WellArchitectedProviderSelection, type Versioned } from '@/services/l5Services';

vi.mock('@/services/l5Services', async original => ({ ...await original<typeof import('@/services/l5Services')>(),
  getWellArchitectedConfiguration: vi.fn(), getWellArchitectedOptions: vi.fn(), saveWellArchitectedConfiguration: vi.fn(), validateWellArchitectedProviderSelection: vi.fn(),
  listWellArchitectedBindings: vi.fn(), listWellArchitectedRuns: vi.fn(), listWellArchitectedResults: vi.fn() }));

const props = { tenantId: 'tenant-one', productId: 'product-one', projectId: 'project-one' };
const connection = { tenant_id: props.tenantId, project_id: props.projectId, source_connection_id: 'connection-one', record_type: 'connection', record_id: 'connection-one', record_revision: '7' };
const source = { ...connection, record_type: 'document', record_id: 'design-one', record_revision: '5' };
const options = { connections: [connection], sources: [source], regions: ['eu-west-1'], capabilities: ['architecture_review' as const], unavailable_reasons: [] };
const roleArn = 'arn:aws:iam::123456789012:role/source-only-review';
const bucket = 'example-private-source-store';
const selection: WellArchitectedProviderSelection = { provider_contacted: true, native_qualified: false,
  execution_role_ref: { ...connection, record_type: 'role', record_id: roleArn, record_revision: 'a'.repeat(64) },
  source_store_ref: { ...connection, record_type: 'artifact_store', record_id: bucket, record_revision: 'b'.repeat(64) } };
const config: Versioned<WellArchitectedConfiguration> = { etag: '"hash-one"', value: {
  scope: { tenant_id: props.tenantId, product_id: props.productId, project_id: props.projectId, relationship_revision: '5' },
  configuration_id: 'config-one', revision: 1, state: 'needs_input', gaps: ['binding_not_ready'], inherited_fields: ['purpose'], handoff_state: 'linked', hash: 'hash-one',
  settings: { service_id: 'well_architected', capabilities: ['architecture_review'], profile_ref: { ...connection, record_type: 'product', record_id: props.productId },
    connection_ref: connection, region: 'eu-west-1', source_refs: [source], purpose: 'Documented decisions', enabled: false, visibility: 'product_team', exclusions: ['private/**'],
    overrides: [{ field: 'classification', reason: 'Retain reviewed classification', value: 'internal' }],
    triggers: [{ trigger_id: 'existing-daily', kind: 'schedule', enabled: false, capability: 'architecture_review', binding_id: 'binding-one', source_refs: [source], debounce_seconds: 0, schedule: '0 5 * * *' }],
    well_architected: { review_mode: 'source_only', pillars: ['SECURITY'], goal_refs: [source] },
  },
} };
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; };
const validateButton = () => screen.getByRole('button', { name: 'Validate provider resources' });
const saveButton = () => screen.getByRole('button', { name: 'Save review draft' });
async function navigate(name: 'Setup' | 'Capabilities' | 'Runs & schedules' | 'Results') {
  const navigation = await screen.findByRole('navigation', { name: 'Well-Architected service sections' });
  await userEvent.click(within(navigation).getByRole('link', { name }));
  expect(screen.getByRole('region', { name })).toBeVisible();
}
async function openSetup() {
  await navigate('Setup');
  const summary = await screen.findByText('Edit review setup');
  if (!summary.closest('details')!.open) await userEvent.click(summary);
  expect(screen.getByLabelText('Execution role ARN')).toBeVisible();
}
async function enterResources() {
  await openSetup();
  await userEvent.type(screen.getByLabelText('Execution role ARN'), roleArn);
  await userEvent.type(screen.getByLabelText('Source bucket name'), bucket);
  await userEvent.type(screen.getByLabelText('Reason for explicit choices'), 'Review exact source-only resources');
}
async function validateAndReview() {
  await userEvent.click(validateButton());
  await userEvent.click(await screen.findByLabelText('I reviewed these pinned provider resources'));
}

describe('Well-Architected explicit provider setup', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [] });
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [] });
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [] });
    vi.mocked(getWellArchitectedOptions).mockResolvedValue(options);
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(config);
    vi.mocked(validateWellArchitectedProviderSelection).mockResolvedValue(selection);
    vi.mocked(saveWellArchitectedConfiguration).mockImplementation(async (_product, _project, choices) => ({ etag: '"hash-two"', value: {
      ...config.value, revision: 2, settings: { ...config.value.settings, ...choices },
    } }));
  });

  it('validates only after an explicit click, reviews pinned resources, and preserves a disabled draft and existing settings', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await enterResources();
    expect(validateWellArchitectedProviderSelection).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Enable this review configuration when saved')).not.toBeChecked();
    await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
    await userEvent.click(validateButton());
    expect(await screen.findByLabelText('I reviewed these pinned provider resources')).not.toBeChecked();
    expect(validateWellArchitectedProviderSelection).toHaveBeenCalledExactlyOnceWith(props.productId, props.projectId, {
      connection_ref: connection, region: 'eu-west-1', execution_role_arn: roleArn, source_bucket: bucket,
    });
    expect(screen.getByText(/Native provider qualification and review readiness still require setup/)).toBeInTheDocument();
    await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
    await userEvent.click(screen.getByLabelText('I reviewed these pinned provider resources'));
    await userEvent.click(saveButton());
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledTimes(1));
    const [, , choices, etag] = vi.mocked(saveWellArchitectedConfiguration).mock.calls[0];
    expect(etag).toBe(config.etag);
    expect(choices).toMatchObject({ enabled: false, well_architected: {
      ...config.value.settings.well_architected, execution_role_ref: selection.execution_role_ref, source_store_ref: selection.source_store_ref,
    }, overrides: expect.arrayContaining(config.value.settings.overrides) });
    // Omitted settings are retained by the configuration edit API; never send fresh defaults.
    for (const retained of ['triggers', 'visibility', 'exclusions', 'monitoring', 'purpose']) expect(choices).not.toHaveProperty(retained);
    expect(await screen.findByRole('status')).toHaveTextContent('saved; disabled');
    expect(screen.queryByLabelText('I reviewed these pinned provider resources')).not.toBeInTheDocument();
  });

  it('requires explicit enablement and resource review before saving an enabled configuration', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await enterResources();
    await userEvent.click(screen.getByLabelText('Enable this review configuration when saved'));
    await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
    await validateAndReview();
    await userEvent.click(saveButton());
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledWith(props.productId, props.projectId, expect.objectContaining({ enabled: true }), config.etag));
    expect(await screen.findByRole('status')).toHaveTextContent('saved; enabled');
  });

  it('keeps missing classification and limits empty when saving a disabled draft', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await openSetup();
    expect(await screen.findByLabelText('Data classification')).toHaveValue('');
    expect(Array.from((screen.getByRole('combobox', { name: 'Data classification' }) as HTMLSelectElement).options, option => option.value))
      .toEqual(['', 'public', 'internal', 'confidential', 'restricted']);
    expect(screen.getByLabelText('Run deadline (seconds)')).toHaveValue(null);
    expect(screen.getByLabelText('Budget policy ID')).toHaveValue('');
    await userEvent.type(screen.getByLabelText('Reason for explicit choices'), 'Preserve disabled draft');
    await userEvent.click(saveButton());
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledTimes(1));
    const choices = vi.mocked(saveWellArchitectedConfiguration).mock.calls[0][2];
    expect(choices.enabled).toBe(false);
    expect(choices).not.toHaveProperty('classification');
    expect(choices).not.toHaveProperty('limits');
    expect(validateWellArchitectedProviderSelection).not.toHaveBeenCalled();
  });

  it('loads saved bounds and sends only explicitly edited classification and limits', async () => {
    const limits = { deadline_seconds: 600, max_provider_attempts: 2, max_polls: 20, concurrency: 1,
      max_cost: '1.00', currency: 'USD' as const, policy_ref: { ...connection, record_type: 'policy', record_id: 'existing-policy' } };
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue({ ...config, value: { ...config.value,
      settings: { ...config.value.settings, classification: 'internal', limits } } });
    render(<WellArchitectedServiceTab {...props} />);
    await openSetup();
    expect(await screen.findByLabelText('Data classification')).toHaveValue('internal');
    expect(screen.getByLabelText('Run deadline (seconds)')).toHaveValue(600);
    expect(screen.getByLabelText('Budget policy ID')).toHaveValue('existing-policy');
    await userEvent.selectOptions(screen.getByLabelText('Data classification'), 'restricted');
    await userEvent.clear(screen.getByLabelText('Maximum cost per assessment (USD)'));
    await userEvent.type(screen.getByLabelText('Maximum cost per assessment (USD)'), '0.50');
    await userEvent.type(screen.getByLabelText('Reason for explicit choices'), 'Restrict review data and spend');
    await userEvent.click(saveButton());
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledTimes(1));
    expect(vi.mocked(saveWellArchitectedConfiguration).mock.calls[0][2]).toMatchObject({ enabled: false,
      classification: 'restricted', limits: { ...limits, max_cost: '0.50' },
      overrides: expect.arrayContaining([{ field: 'classification', value: 'restricted', reason: 'Restrict review data and spend' }]) });
    expect(validateWellArchitectedProviderSelection).not.toHaveBeenCalled();
  });

  it('requires complete explicit limits instead of filling in an unknown budget policy', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await openSetup();
    await screen.findByLabelText('Run deadline (seconds)');
    await userEvent.type(screen.getByLabelText('Run deadline (seconds)'), '600');
    await userEvent.type(screen.getByLabelText('Reason for explicit choices'), 'Set a finite review deadline');
    await userEvent.click(saveButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('Complete the assessment limits and an immutable budget policy reference');
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
  });

  it.each([
    ['foreign tenant', { execution_role_ref: { ...selection.execution_role_ref, tenant_id: 'foreign' } }],
    ['foreign project', { source_store_ref: { ...selection.source_store_ref, project_id: 'foreign' } }],
    ['foreign connection', { execution_role_ref: { ...selection.execution_role_ref, source_connection_id: 'foreign' } }],
    ['different role', { execution_role_ref: { ...selection.execution_role_ref, record_id: `${roleArn}-other` } }],
    ['different bucket', { source_store_ref: { ...selection.source_store_ref, record_id: 'other-bucket' } }],
    ['wrong type', { source_store_ref: { ...selection.source_store_ref, record_type: 'connection' } }],
    ['unpinned revision', { execution_role_ref: { ...selection.execution_role_ref, record_revision: 'latest' } }],
    ['unexpected fields', { execution_role_ref: { ...selection.execution_role_ref, unsafe: 'extra' } }],
    ['qualification claim', { native_qualified: true }],
  ])('rejects a %s response before rendering references or allowing save', async (_name, change) => {
    vi.mocked(validateWellArchitectedProviderSelection).mockResolvedValue({ ...selection, ...change } as WellArchitectedProviderSelection);
    render(<WellArchitectedServiceTab {...props} />);
    await enterResources(); await userEvent.click(validateButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('do not match this selection');
    expect(screen.queryByLabelText('I reviewed these pinned provider resources')).not.toBeInTheDocument();
    await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
  });

  it('validates input format before contacting the provider', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await openSetup();
    await userEvent.type(screen.getByLabelText('Execution role ARN'), 'not-an-arn');
    await userEvent.click(validateButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter an execution role ARN');
    expect(validateWellArchitectedProviderSelection).not.toHaveBeenCalled();
  });

  it('discards a late response even when changed input is restored to its original value', async () => {
    const pending = deferred<WellArchitectedProviderSelection>();
    vi.mocked(validateWellArchitectedProviderSelection).mockReturnValue(pending.promise);
    render(<WellArchitectedServiceTab {...props} />);
    await enterResources(); await userEvent.click(validateButton());
    const region = screen.getByLabelText('Provider region');
    await userEvent.clear(region); await userEvent.type(region, 'eu-west-1');
    await act(async () => pending.resolve(selection));
    expect(screen.queryByLabelText('I reviewed these pinned provider resources')).not.toBeInTheDocument();
    await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
  });

  it.each(['Source bucket name', 'Execution role ARN', 'Provider region', 'RESILIENCE', 'document: design-one (5)'])('invalidates reviewed references when %s changes', async label => {
    render(<WellArchitectedServiceTab {...props} />);
    await enterResources(); await validateAndReview();
    if (label === 'RESILIENCE') await navigate('Capabilities');
    const field = screen.getByLabelText(label);
    if (field.getAttribute('type') === 'checkbox') await userEvent.click(field);
    else await userEvent.type(field, '-changed');
    await openSetup();
    expect(screen.queryByLabelText('I reviewed these pinned provider resources')).not.toBeInTheDocument();
    await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
  });

  it('discards validation and drafts when the Product scope changes', async () => {
    const pending = deferred<WellArchitectedProviderSelection>();
    vi.mocked(validateWellArchitectedProviderSelection).mockReturnValue(pending.promise);
    const view = render(<WellArchitectedServiceTab {...props} />);
    await enterResources(); await userEvent.click(validateButton());
    vi.mocked(getWellArchitectedOptions).mockResolvedValue({ ...options, connections: [], sources: [] });
    vi.mocked(getWellArchitectedConfiguration).mockRejectedValue({ status: 404 });
    view.rerender(<WellArchitectedServiceTab {...props} projectId="project-two" />);
    await waitFor(() => expect(screen.getByLabelText('Execution role ARN')).toHaveValue(''));
    await act(async () => pending.resolve(selection));
    expect(screen.queryByLabelText('I reviewed these pinned provider resources')).not.toBeInTheDocument();
    expect(screen.queryByText(/design-one/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('Enable this review configuration when saved')).not.toBeChecked();
  });

  it('invalidates validation on connection changes and remaps the exact draft connection after refresh', async () => {
    const other = { ...connection, source_connection_id: 'connection-two', record_id: 'connection-two' };
    vi.mocked(getWellArchitectedOptions).mockResolvedValue({ ...options, connections: [connection, other] });
    render(<WellArchitectedServiceTab {...props} />);
    await enterResources(); await validateAndReview();
    await userEvent.selectOptions(screen.getByLabelText('Authorized connection'), '1');
    expect(screen.queryByLabelText('I reviewed these pinned provider resources')).not.toBeInTheDocument();
    vi.mocked(getWellArchitectedOptions).mockResolvedValue({ ...options, connections: [other, connection] });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Well-Architected access' }));
    await waitFor(() => expect(screen.getByLabelText('Authorized connection')).toHaveValue('0'));
    await userEvent.click(validateButton());
    expect(validateWellArchitectedProviderSelection).toHaveBeenLastCalledWith(props.productId, props.projectId, expect.objectContaining({ connection_ref: other }));
  });

  it('invalidates reviewed provider pins on a configuration refresh while retaining draft choices', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await enterResources(); await validateAndReview();
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Well-Architected access' }));
    await screen.findByText(/Latest saved configuration loaded/);
    expect(screen.queryByLabelText('I reviewed these pinned provider resources')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Execution role ARN')).toHaveValue(roleArn);
    await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
  });

  it('ignores save completion after navigating to another scope', async () => {
    const pending = deferred<Versioned<WellArchitectedConfiguration>>();
    vi.mocked(saveWellArchitectedConfiguration).mockReturnValue(pending.promise);
    const view = render(<WellArchitectedServiceTab {...props} />);
    await enterResources(); await validateAndReview(); await userEvent.click(saveButton());
    vi.mocked(getWellArchitectedOptions).mockResolvedValue({ ...options, sources: [], connections: [] });
    vi.mocked(getWellArchitectedConfiguration).mockRejectedValue({ status: 404 });
    view.rerender(<WellArchitectedServiceTab {...props} projectId="project-two" />);
    await openSetup();
    await act(async () => pending.resolve(config));
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Execution role ARN')).toHaveValue('');
  });

  it('lets the owner explicitly disable an existing enabled configuration while retaining saved provider pins', async () => {
    const saved = { ...config, value: { ...config.value, settings: { ...config.value.settings, enabled: true,
      well_architected: { ...config.value.settings.well_architected, execution_role_ref: selection.execution_role_ref, source_store_ref: selection.source_store_ref },
    } } };
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(saved);
    render(<WellArchitectedServiceTab {...props} />);
    await openSetup();
    expect(screen.getByLabelText('Enable this review configuration when saved')).toBeChecked();
    await userEvent.click(screen.getByLabelText('Enable this review configuration when saved'));
    await userEvent.type(screen.getByLabelText('Reason for explicit choices'), 'Pause review admission');
    await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).toHaveBeenCalledWith(props.productId, props.projectId, expect.objectContaining({ enabled: false,
      well_architected: saved.value.settings.well_architected,
    }), config.etag);
    expect(validateWellArchitectedProviderSelection).not.toHaveBeenCalled();
  });

  it('fences an old configuration read after scope navigation', async () => {
    const pending = deferred<Versioned<WellArchitectedConfiguration>>();
    vi.mocked(getWellArchitectedConfiguration).mockReturnValueOnce(pending.promise);
    const view = render(<WellArchitectedServiceTab {...props} />);
    await waitFor(() => expect(getWellArchitectedConfiguration).toHaveBeenCalledTimes(1));
    vi.mocked(getWellArchitectedOptions).mockResolvedValue({ ...options, connections: [], sources: [] });
    vi.mocked(getWellArchitectedConfiguration).mockRejectedValue({ status: 404 });
    view.rerender(<WellArchitectedServiceTab {...props} projectId="project-two" />);
    await openSetup();
    await act(async () => pending.resolve(config));
    expect(screen.queryByText(/Documented decisions/)).not.toBeInTheDocument();
    expect(screen.queryByText(/design-one/)).not.toBeInTheDocument();
  });

  it.each([412, 503, undefined])('retains the draft and requires a refresh without replay after save status %s', async status => {
    vi.mocked(saveWellArchitectedConfiguration).mockRejectedValue({ status });
    render(<WellArchitectedServiceTab {...props} />);
    await enterResources(); await validateAndReview();
    await userEvent.click(saveButton());
    await waitFor(() => expect(saveButton()).toBeDisabled());
    expect(screen.getByRole('alert')).toHaveTextContent(status === 412 ? 'changed elsewhere' : 'outcome is unknown');
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue({ ...config, etag: '"new-current"', value: { ...config.value, revision: 3 } });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Well-Architected access' }));
    await waitFor(() => expect(saveButton()).toBeEnabled());
    expect(screen.getByLabelText('Execution role ARN')).toHaveValue(roleArn);
    expect(screen.getByLabelText('Source bucket name')).toHaveValue(bucket);
    expect(screen.getByLabelText('Reason for explicit choices')).toHaveValue('Review exact source-only resources');
    expect(screen.queryByLabelText('I reviewed these pinned provider resources')).not.toBeInTheDocument();
    expect(saveWellArchitectedConfiguration).toHaveBeenCalledTimes(1);
    await validateAndReview(); await userEvent.click(saveButton());
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledTimes(2));
    expect(vi.mocked(saveWellArchitectedConfiguration).mock.calls[1][3]).toBe('"new-current"');
  });

  it.each([403, 404])('removes resource drafts on validation access denial %s', async status => {
    vi.mocked(validateWellArchitectedProviderSelection).mockRejectedValue({ status });
    render(<WellArchitectedServiceTab {...props} />);
    await enterResources(); await userEvent.click(validateButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details were removed');
    expect(screen.queryByLabelText('Execution role ARN')).not.toBeInTheDocument();
    expect(screen.queryByText(/design-one/)).not.toBeInTheDocument();
  });

  it('shows actionable provider rejection without rendering arbitrary provider content', async () => {
    vi.mocked(validateWellArchitectedProviderSelection).mockRejectedValue({ status: 409, detail: { code: 'environment_selection_required', message: '<script>secret</script>' } });
    render(<WellArchitectedServiceTab {...props} />);
    await enterResources(); await userEvent.click(validateButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('Select and verify this connection’s environment on the Product');
    expect(screen.queryByText(/secret/)).not.toBeInTheDocument();
  });

  it('rejects foreign saved provider resources before rendering their identity', async () => {
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue({ ...config, value: { ...config.value, settings: { ...config.value.settings,
      well_architected: { ...config.value.settings.well_architected, execution_role_ref: { ...selection.execution_role_ref, project_id: 'foreign' }, source_store_ref: selection.source_store_ref },
    } } });
    render(<WellArchitectedServiceTab {...props} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details were removed');
    expect(screen.queryByText(roleArn)).not.toBeInTheDocument();
  });
});
