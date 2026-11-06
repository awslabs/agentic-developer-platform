import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SecurityConfiguration from '@/pages/products/services/SecurityConfiguration';
import { limitsDraft, parseLimits } from '@/pages/products/services/SecurityLimits';
import { getProduct, listProductSources, inspectProductSource } from '@/services/products';
import { getSecurityConfiguration, getSecurityOptions, listSecurityBindings, refreshSecurityProfile, saveSecurityConfiguration } from '@/services/l5Services';

vi.mock('@/services/l5Services', async importOriginal => ({ ...await importOriginal<typeof import('@/services/l5Services')>(),
  getSecurityConfiguration: vi.fn(), getSecurityOptions: vi.fn(), listSecurityBindings: vi.fn(), refreshSecurityProfile: vi.fn(), saveSecurityConfiguration: vi.fn() }));
vi.mock('@/services/products', async importOriginal => ({ ...await importOriginal<typeof import('@/services/products')>(), getProduct: vi.fn(), listProductSources: vi.fn(), inspectProductSource: vi.fn() }));
vi.mock('@/pages/products/services/L5OwnerConsent', () => ({ default: () => null }));
vi.mock('@/pages/products/services/SecurityEvidencePanel', () => ({ default: () => null }));

const scope = { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '1' };
const ref = { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'project-source', record_type: 'product', record_id: 'product-one', record_revision: '2' };
const limits = { deadline_seconds: 3600, max_provider_attempts: 1, max_polls: 30, concurrency: 1, max_cost: '2.50', currency: 'USD' as const,
  policy_ref: { ...ref, record_type: 'policy', record_id: 'existing-budget', record_revision: '6' } };
const configuration = { value: { scope, configuration_id: 'config-one', revision: 3, hash: 'hash-three', state: 'needs_input', inherited_fields: ['purpose'], gaps: [], handoff_state: 'linked',
  settings: { profile_ref: ref, purpose: 'Old purpose', source_refs: [], capabilities: ['threat_model'], enabled: false, visibility: 'only_me', exclusions: [], overrides: [],
    triggers: [{ trigger_id: 'retained-webhook', kind: 'webhook', enabled: false, source_refs: [], debounce_seconds: 30 }], limits } }, etag: '"hash-three"' };
const profile = { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', revision: 4,
  product: { purpose: 'Current purpose' }, configuration: { governance: { data_classification: 'internal' }, basic: { documents: [{ ...ref, record_type: 'document', record_id: 'design', record_revision: '7' }] } } };
const mount = () => render(<SecurityConfiguration productId="product-one" projectId="project-one" tenantId="tenant-one" />);

describe('Security setup limits and reviewed Product refresh', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listProductSources).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(getSecurityOptions).mockResolvedValue({ connections: [], sources: [], regions: [], capabilities: ['threat_model'], unavailable_reasons: [] });
    vi.mocked(getSecurityConfiguration).mockResolvedValue(configuration as never);
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [] });
    vi.mocked(getProduct).mockResolvedValue(profile as never);
    vi.mocked(saveSecurityConfiguration).mockResolvedValue(configuration as never);
  });
  it('reads saved limits and writes explicit edits without changing retained triggers', async () => {
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Advanced settings', exact: true }));
    expect(await screen.findByLabelText('Maximum cost per assessment (USD)')).toHaveValue('2.50');
    await userEvent.clear(screen.getByLabelText('Maximum cost per assessment (USD)'));
    await userEvent.type(screen.getByLabelText('Maximum cost per assessment (USD)'), '1.25');
    await userEvent.click(screen.getByRole('button', { name: 'Save advanced settings' }));
    await waitFor(() => expect(saveSecurityConfiguration).toHaveBeenCalledWith('product-one', 'project-one', expect.objectContaining({
      limits: { ...limits, max_cost: '1.25' }, triggers: configuration.value.settings.triggers,
    }), configuration.etag, undefined, undefined));
  });
  it('does not send untouched limits or create a budget', async () => {
    mount();
    await screen.findByLabelText('Run deadline (seconds)');
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    expect(vi.mocked(saveSecurityConfiguration).mock.calls[0][2]).not.toHaveProperty('limits');
    expect(refreshSecurityProfile).not.toHaveBeenCalled();
  });
  it.each(['HEAD', 'latest', 'null'])('rejects a mutable budget reference %s before saving', value => {
    expect(() => parseLimits({ ...limitsDraft(limits), policy_revision: value }, scope.tenant_id, scope.project_id)).toThrow(/immutable budget/);
  });
  it('rejects noninteger limits and excess precision before saving', () => {
    expect(() => parseLimits({ ...limitsDraft(limits), concurrency: '1.5' }, scope.tenant_id, scope.project_id)).toThrow();
    expect(() => parseLimits({ ...limitsDraft(limits), max_cost: '1.0000001' }, scope.tenant_id, scope.project_id)).toThrow();
  });
  it('reads the current Profile without mutation, then applies only the reviewed revision with its original ETag', async () => {
    const refreshed = { value: { ...configuration.value, hash: 'hash-four', revision: 4, settings: { ...configuration.value.settings, profile_ref: { ...ref, record_revision: '4' } } }, etag: '"hash-four"' };
    vi.mocked(refreshSecurityProfile).mockResolvedValue(refreshed as never);
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Review profile updates' }));
    await userEvent.click(screen.getByRole('button', { name: 'Read current Product profile' }));
    expect(await screen.findByText('Current purpose')).toBeInTheDocument();
    expect(refreshSecurityProfile).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Apply reviewed Product profile' })).toBeDisabled();
    await userEvent.click(screen.getByLabelText('I reviewed this Product revision and the effect on inherited Security setup'));
    vi.mocked(getSecurityConfiguration).mockResolvedValue(refreshed as never);
    await userEvent.click(screen.getByRole('button', { name: 'Apply reviewed Product profile' }));
    await waitFor(() => expect(refreshSecurityProfile).toHaveBeenCalledWith('product-one', 'project-one', { ...ref, record_revision: '4' }, configuration.etag, expect.stringMatching(/^[a-f0-9]{32}$/)));
    await waitFor(() => expect(getSecurityConfiguration).toHaveBeenCalledTimes(2));
    expect(saveSecurityConfiguration).not.toHaveBeenCalled();
  });
  it('blocks applying a Profile while local Security edits are unsaved', async () => {
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Review profile updates' }));
    await userEvent.click(screen.getByRole('button', { name: 'Read current Product profile' }));
    await screen.findByText('Current purpose');
    await userEvent.click(screen.getByLabelText('I reviewed this Product revision and the effect on inherited Security setup'));
    await userEvent.click(screen.getByRole('button', { name: 'Close Product profile updates' }));
    await userEvent.click(screen.getByRole('button', { name: 'Advanced settings', exact: true }));
    await userEvent.clear(screen.getByLabelText('Maximum cost per assessment (USD)'));
    await userEvent.type(screen.getByLabelText('Maximum cost per assessment (USD)'), '1');
    await userEvent.click(screen.getByRole('button', { name: 'Close Advanced Security settings' }));
    await userEvent.click(screen.getByRole('button', { name: 'Review profile updates' }));
    expect(screen.getByRole('button', { name: 'Apply reviewed Product profile' })).toBeDisabled();
    expect(refreshSecurityProfile).not.toHaveBeenCalled();
  });
  it('requires readback after an uncertain Profile refresh and never resubmits it automatically', async () => {
    vi.mocked(refreshSecurityProfile).mockRejectedValue(new Error('connection lost'));
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Review profile updates' }));
    await userEvent.click(screen.getByRole('button', { name: 'Read current Product profile' }));
    await screen.findByText('Current purpose');
    await userEvent.click(screen.getByLabelText('I reviewed this Product revision and the effect on inherited Security setup'));
    await userEvent.click(screen.getByRole('button', { name: 'Apply reviewed Product profile' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Profile refresh could not be confirmed');
    expect(screen.getByRole('button', { name: 'Save setup' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Apply reviewed Product profile' })).toBeDisabled();
    expect(refreshSecurityProfile).toHaveBeenCalledTimes(1);
    expect(getSecurityConfiguration).toHaveBeenCalledTimes(1);
  });
  it('locks setup, capabilities and schedule edits while a reviewed Profile refresh is in flight', async () => {
    let finish!: (value: never) => void;
    vi.mocked(refreshSecurityProfile).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Review profile updates' }));
    await userEvent.click(screen.getByRole('button', { name: 'Read current Product profile' }));
    await screen.findByText('Current purpose');
    await userEvent.click(screen.getByLabelText('I reviewed this Product revision and the effect on inherited Security setup'));
    await userEvent.click(screen.getByRole('button', { name: 'Apply reviewed Product profile' }));
    expect(screen.getByLabelText('Maximum cost per assessment (USD)')).toBeDisabled();
    expect(screen.getByLabelText('Threat modeling')).toBeDisabled();
    expect(screen.getByLabelText('Daily UTC time for threat_model')).toBeDisabled();
    await userEvent.type(screen.getByLabelText('Maximum cost per assessment (USD)'), '9');
    expect(screen.getByLabelText('Maximum cost per assessment (USD)')).toHaveValue('2.50');
    const saved = { ...configuration, value: { ...configuration.value, hash: 'hash-four', settings: { ...configuration.value.settings, profile_ref: { ...ref, record_revision: '4' } } } };
    vi.mocked(getSecurityConfiguration).mockResolvedValue(saved as never);
    finish(saved as never);
    await waitFor(() => expect(screen.getByLabelText('Maximum cost per assessment (USD)')).toBeEnabled());
    expect(screen.getByLabelText('Maximum cost per assessment (USD)')).toHaveValue('2.50');
  });
  it('removes foreign Product data without allowing a refresh', async () => {
    vi.mocked(getProduct).mockResolvedValue({ ...profile, tenant_id: 'foreign-tenant' } as never);
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Review profile updates' }));
    await userEvent.click(screen.getByRole('button', { name: 'Read current Product profile' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Read current Product profile' })).not.toBeInTheDocument());
    expect(screen.queryByText('Current purpose')).not.toBeInTheDocument();
    expect(refreshSecurityProfile).not.toHaveBeenCalled();
  });
  it('blocks writes after an uncertain ordinary save until the saved configuration is reloaded', async () => {
    vi.mocked(saveSecurityConfiguration).mockRejectedValue(new Error('response lost'));
    mount();
    await screen.findByLabelText('Run deadline (seconds)');
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Reload Security configuration');
    expect(screen.getByRole('button', { name: 'Save setup' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Reload setup' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save setup' })).toBeEnabled());
    expect(saveSecurityConfiguration).toHaveBeenCalledTimes(1);
  });
});

it('shows the repository name without a home repository and saves the original pinned source', async () => {
  vi.resetAllMocks();
  const source = { ...ref, record_type: 'repository_commit', record_id: '12345', record_revision: 'abcdef1234567890' };
  const github = { connection_ref: { kind: 'github_installation', id: ref.source_connection_id }, provider: 'github', label: 'Example', health: 'healthy', coverage: 'complete', capabilities: ['repository'] } as const;
  vi.mocked(getSecurityOptions).mockResolvedValue({ connections: [], sources: [source], regions: [], capabilities: ['code_review'], unavailable_reasons: [] });
  vi.mocked(getSecurityConfiguration).mockResolvedValue({ ...configuration, value: { ...configuration.value, settings: { ...configuration.value.settings, source_refs: [source], capabilities: ['code_review'] } } } as never);
  vi.mocked(listSecurityBindings).mockResolvedValue({ items: [] });
  vi.mocked(listProductSources).mockResolvedValue({ items: [github], next_cursor: null } as never);
  vi.mocked(inspectProductSource).mockResolvedValue({ items: [{ ...github, resources: [{ kind: 'repository', native_id: '12345', full_name: 'example/application' }] }] } as never);
  vi.mocked(saveSecurityConfiguration).mockResolvedValue(configuration as never);
  mount();
  expect(await screen.findByText('example/application')).toBeVisible();
  expect(getProduct).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: 'Save setup', exact: true }));
  expect(vi.mocked(saveSecurityConfiguration).mock.calls[0][2]).not.toHaveProperty('source_refs');
});

it('selects by AWS account ID and saves the exact associated connection with its change reason', async () => {
  vi.resetAllMocks();
  const first = { ...ref, record_type: 'connection', record_id: 'aws-first', source_connection_id: 'aws-first' };
  const second = { ...first, record_id: 'aws-second', source_connection_id: 'aws-second', record_revision: '8' };
  const metadata = [first, second].map((connection, index) => ({ connection_ref: { kind: 'credential', id: connection.record_id },
    provider: 'aws', label: index ? 'Production' : 'Development', coverage: 'partial', resources: [{ kind: 'environment',
      source_revision: connection.record_revision, verified_environment: { account_id: index ? '999999999999' : '123456789012' } }] }));
  vi.mocked(getSecurityOptions).mockResolvedValue({ connections: [first, second], sources: [], regions: ['us-east-1'], capabilities: ['threat_model'], unavailable_reasons: [] });
  vi.mocked(getSecurityConfiguration).mockResolvedValue({ ...configuration, value: { ...configuration.value, settings: { ...configuration.value.settings, connection_ref: first } } } as never);
  vi.mocked(listSecurityBindings).mockResolvedValue({ items: [] });
  vi.mocked(listProductSources).mockResolvedValue({ items: metadata, next_cursor: null } as never);
  vi.mocked(inspectProductSource).mockImplementation(async (_p, _j, connection) => ({ items: metadata.filter(item => item.connection_ref.id === connection.id) }) as never);
  vi.mocked(saveSecurityConfiguration).mockResolvedValue(configuration as never);
  mount();
  await screen.findByRole('option', { name: '999999999999 · Production' });
  expect(screen.getByLabelText('AWS account')).toHaveValue('0');
  await userEvent.selectOptions(screen.getByLabelText('AWS account'), '1');
  await userEvent.type(screen.getByLabelText('Why use these Security settings?'), 'Assess the production account');
  await userEvent.click(screen.getByRole('button', { name: 'Save setup', exact: true }));
  const saved = vi.mocked(saveSecurityConfiguration).mock.calls[0][2];
  expect(saved.connection_ref).toEqual(second);
  expect(saved.overrides).toContainEqual({ field: 'connection_ref', reason: 'Assess the production account', value: second });
  expect(saved).not.toHaveProperty('limits');
  expect(saved).not.toHaveProperty('source_refs');
});

it('shows server-inherited Product inputs and AWS settings immediately after the first save', async () => {
  vi.resetAllMocks();
  const source = { ...ref, record_type: 'document', record_id: 'design', record_revision: '7' };
  const connection = { ...ref, record_type: 'connection', record_id: 'aws-connection', record_revision: '3' };
  vi.mocked(getSecurityOptions).mockResolvedValue({ connections: [connection], sources: [source], regions: ['us-east-1'], capabilities: ['threat_model'], unavailable_reasons: [] });
  vi.mocked(getSecurityConfiguration).mockRejectedValue({ status: 404 });
  vi.mocked(saveSecurityConfiguration).mockResolvedValue({ ...configuration, value: { ...configuration.value, settings: {
    ...configuration.value.settings, source_refs: [source], connection_ref: connection, region: 'us-east-1',
  } } } as never);
  mount();
  await userEvent.click(await screen.findByLabelText('Threat modeling'));
  await userEvent.click(screen.getByRole('button', { name: 'Save setup', exact: true }));
  await screen.findByText(/Security setup saved/);
  expect(screen.getByLabelText('AWS account')).toHaveValue('0');
  expect(screen.getByLabelText('AWS region')).toHaveValue('us-east-1');
  expect(within(screen.getByRole('group', { name: 'Source revisions for Security' })).getByRole('checkbox', { name: /design/ })).toBeChecked();
  expect(vi.mocked(saveSecurityConfiguration).mock.calls[0][2]).not.toHaveProperty('source_refs');
  expect(screen.queryByLabelText('Why use these Security settings?')).not.toBeInTheDocument();
});
