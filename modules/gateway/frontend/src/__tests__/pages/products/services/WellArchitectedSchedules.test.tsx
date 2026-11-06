import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import WellArchitectedServiceTab from '@/pages/products/services/WellArchitectedServiceTab';
import { getWellArchitectedConfiguration, getWellArchitectedOptions, saveWellArchitectedConfiguration, listWellArchitectedBindings,
  listWellArchitectedRuns, listWellArchitectedResults, startWellArchitectedBinding, submitWellArchitectedRun,
  type SecurityBinding, type SecuritySettings, type Versioned, type WellArchitectedConfiguration } from '@/services/l5Services';

vi.mock('@/pages/products/services/L5OwnerConsent', () => ({ default: () => null }));
vi.mock('@/services/l5Services', async original => ({ ...await original<typeof import('@/services/l5Services')>(),
  getWellArchitectedConfiguration: vi.fn(), getWellArchitectedOptions: vi.fn(), saveWellArchitectedConfiguration: vi.fn(),
  listWellArchitectedBindings: vi.fn(), listWellArchitectedRuns: vi.fn(), listWellArchitectedResults: vi.fn(),
  startWellArchitectedBinding: vi.fn(), submitWellArchitectedRun: vi.fn() }));

const props = { tenantId: 'tenant-one', productId: 'product-one', projectId: 'project-one' };
const connection = { tenant_id: props.tenantId, project_id: props.projectId, source_connection_id: 'connection-one', record_type: 'connection', record_id: 'connection-one', record_revision: '7' };
const sources = [{ ...connection, record_type: 'document', record_id: 'design-one', record_revision: '5' },
  { ...connection, record_type: 'repository_commit', record_id: 'repository-one', record_revision: 'commit-one' }];
const manual = { trigger_id: 'manual-one', kind: 'manual', enabled: true, capability: 'architecture_review', binding_id: 'binding-one', source_refs: sources, debounce_seconds: 0 };
const daily = { ...manual, trigger_id: 'daily-one', kind: 'schedule', schedule: '0 3 * * *', binding_generation: 1, retained_metadata: 'keep-me' };
const config: Versioned<WellArchitectedConfiguration> = { etag: '"hash-one"', value: {
  scope: { tenant_id: props.tenantId, product_id: props.productId, project_id: props.projectId, relationship_revision: '5' },
  configuration_id: 'config-one', revision: 1, state: 'needs_input', gaps: ['provider_unqualified'], inherited_fields: ['purpose'], handoff_state: 'linked', hash: 'hash-one',
  settings: { service_id: 'well_architected', capabilities: ['architecture_review'], profile_ref: { ...connection, record_type: 'product', record_id: props.productId },
    connection_ref: connection, region: 'eu-west-1', source_refs: sources, purpose: 'Documented decisions', enabled: true, visibility: 'product_team', exclusions: ['private/**'],
    monitoring: { poll_interval_seconds: 600 }, overrides: [{ field: 'classification', reason: 'Retain reviewed classification', value: 'internal' }], triggers: [manual],
    well_architected: { review_mode: 'source_only', pillars: ['SECURITY'], goal_refs: [sources[0]],
      execution_role_ref: { ...connection, record_type: 'role', record_id: 'arn:aws:iam::123456789012:role/source-only', record_revision: 'a'.repeat(64) },
      source_store_ref: { ...connection, record_type: 'artifact_store', record_id: 'example-private-sources', record_revision: 'b'.repeat(64) } },
  },
} };
const options = { connections: [connection], sources, regions: ['eu-west-1'], capabilities: ['architecture_review' as const], unavailable_reasons: [] };
const binding: SecurityBinding = { binding_id: 'binding-one', configuration_id: 'config-one', scope: config.value.scope, generation: 2, revision: 3,
  state: 'ready', connection_ref: connection, ownership: 'adp_created', gaps: [], readiness: [] };
const withTriggers = (triggers: SecuritySettings['triggers']) => ({ ...config, value: { ...config.value, settings: { ...config.value.settings, triggers } } });
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; };
const saveButton = () => screen.getByRole('button', { name: 'Save review schedule' });
const reviewCheckbox = () => screen.getByLabelText('Use this binding and every saved source revision for the schedule');
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
async function openDraft() {
  await userEvent.click(await screen.findByRole('button', { name: 'Add daily review schedule' }));
  await screen.findByRole('option', { name: 'binding-one (generation 2)' });
  fireEvent.change(screen.getByLabelText('Daily review time (UTC)'), { target: { value: '02:30' } });
  await userEvent.selectOptions(screen.getByLabelText('Review schedule binding'), 'binding-one');
  await userEvent.click(reviewCheckbox());
}

describe('Well-Architected saved schedules', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getWellArchitectedOptions).mockResolvedValue(options);
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(config);
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [binding] });
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [] });
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [] });
    vi.mocked(saveWellArchitectedConfiguration).mockImplementation(async (_product, _project, choices) => ({ etag: '"hash-two"', value: {
      ...config.value, revision: 2, settings: { ...config.value.settings, ...choices },
    } }));
  });

  it('creates a paused daily UTC schedule against the exact ETag and saved inputs, preserving unrelated settings and unsaved setup drafts', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await openDraft();
    const runs = screen.getByRole('region', { name: 'Runs & schedules' });
    expect(within(runs).getByRole('complementary', { name: 'Well-Architected schedules' })).toBeInTheDocument();
    expect(screen.getByLabelText('Enable this daily schedule when saved')).not.toBeChecked();
    expect(screen.getByRole('list', { name: 'Exact saved schedule sources' }).textContent).toBe('document: design-one (5)repository_commit: repository-one (commit-one)');
    await openSetup();
    await userEvent.clear(screen.getByLabelText('Provider region'));
    await userEvent.type(screen.getByLabelText('Provider region'), 'unsaved-region');
    await navigate('Capabilities');
    await userEvent.click(screen.getByLabelText('RESILIENCE'));
    await navigate('Runs & schedules');
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
    await userEvent.click(saveButton());
    await screen.findByText(/Schedule changes saved in configuration revision 2/);
    expect(saveWellArchitectedConfiguration).toHaveBeenCalledTimes(1);
    const [product, project, choices, etag] = vi.mocked(saveWellArchitectedConfiguration).mock.calls[0];
    expect([product, project, etag]).toEqual([props.productId, props.projectId, config.etag]);
    expect(choices).toMatchObject({ enabled: true, region: 'eu-west-1', connection_ref: connection, source_refs: sources, well_architected: config.value.settings.well_architected });
    expect(choices.triggers).toEqual([manual, { trigger_id: expect.any(String), kind: 'schedule', enabled: false, capability: 'architecture_review',
      binding_id: binding.binding_id, source_refs: sources, debounce_seconds: 0, schedule: '30 2 * * *' }]);
    for (const retained of ['visibility', 'exclusions', 'monitoring', 'overrides', 'purpose']) expect(choices).not.toHaveProperty(retained);
    await openSetup();
    expect(screen.getByLabelText('Provider region')).toHaveValue('unsaved-region');
    await navigate('Capabilities');
    expect(screen.getByLabelText('RESILIENCE')).toBeChecked();
    expect(startWellArchitectedBinding).not.toHaveBeenCalled(); expect(submitWellArchitectedRun).not.toHaveBeenCalled();
  });

  it('requires a UTC time, ready binding and explicit source review; enabling also needs the saved configuration enabled', async () => {
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue({ ...config, value: { ...config.value, settings: { ...config.value.settings, enabled: false } } });
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await userEvent.click(await screen.findByRole('button', { name: 'Add daily review schedule' }));
    await userEvent.click(saveButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('Choose a daily UTC time and a current ready binding');
    fireEvent.change(screen.getByLabelText('Daily review time (UTC)'), { target: { value: '23:59' } });
    await userEvent.selectOptions(screen.getByLabelText('Review schedule binding'), 'binding-one');
    await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
    await userEvent.click(screen.getByLabelText('Enable this daily schedule when saved'));
    await userEvent.click(reviewCheckbox()); await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('Enabling a schedule also requires an enabled saved configuration');
    await userEvent.click(screen.getByLabelText('Enable this daily schedule when saved'));
    expect(reviewCheckbox()).not.toBeChecked();
    await userEvent.click(reviewCheckbox()); await userEvent.click(saveButton());
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledWith(props.productId, props.projectId,
      expect.objectContaining({ enabled: false, triggers: expect.arrayContaining([expect.objectContaining({ enabled: false, schedule: '59 23 * * *' })]) }), config.etag));
  });

  it('edits one stable identity with a reviewed binding generation and exact saved source order while preserving all other triggers', async () => {
    const other = { ...daily, trigger_id: 'other-schedule', schedule: '0 5 * * *', enabled: false };
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(withTriggers([manual, { ...daily, source_refs: [...sources].reverse() }, other]));
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await userEvent.click(await screen.findByRole('button', { name: 'Edit schedule daily-one' }));
    expect(reviewCheckbox()).not.toBeChecked();
    fireEvent.change(screen.getByLabelText('Daily review time (UTC)'), { target: { value: '04:15' } });
    await userEvent.click(reviewCheckbox()); await userEvent.click(saveButton());
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledTimes(1));
    expect(vi.mocked(saveWellArchitectedConfiguration).mock.calls[0][2].triggers).toEqual([manual,
      { ...daily, schedule: '15 4 * * *', binding_generation: 2, source_refs: sources }, other]);
  });

  it('enables a new daily schedule only after the explicit schedule choice and renewed source review', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await openDraft();
    await userEvent.click(screen.getByLabelText('Enable this daily schedule when saved'));
    expect(reviewCheckbox()).not.toBeChecked();
    await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
    await userEvent.click(reviewCheckbox()); await userEvent.click(saveButton());
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledWith(props.productId, props.projectId,
      expect.objectContaining({ enabled: true, triggers: expect.arrayContaining([expect.objectContaining({ kind: 'schedule', enabled: true })]) }), config.etag));
    expect(submitWellArchitectedRun).not.toHaveBeenCalled();
  });

  it('pauses an advanced schedule without rewriting its expression, sources, identity or metadata even when its binding is unavailable', async () => {
    const advanced = { ...daily, trigger_id: 'advanced-one', schedule: '0 0 1 * *', source_refs: [sources[0]] };
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(withTriggers([manual, advanced, daily]));
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [] });
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await userEvent.click(await screen.findByRole('button', { name: 'Pause schedule advanced-one' }));
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledTimes(1));
    expect(vi.mocked(saveWellArchitectedConfiguration).mock.calls[0][2].triggers).toEqual([manual, { ...advanced, enabled: false }, daily]);
  });

  it.each([
    { configuration_id: 'other-configuration' }, { connection_ref: { ...connection, record_revision: 'stale' } }, { gaps: ['provider_unqualified'] }, { generation: 0 },
  ])('does not offer an unsuitable binding %j', async change => {
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [{ ...binding, ...change }] });
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await userEvent.click(await screen.findByRole('button', { name: 'Add daily review schedule' }));
    expect(screen.queryByRole('option', { name: /generation 2/ })).not.toBeInTheDocument();
    expect(reviewCheckbox()).toBeDisabled();
    await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
  });

  it('requires review again when the binding generation changes during a status refresh', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await openDraft();
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [{ ...binding, generation: 3, revision: 4 }] });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh review status' }));
    await screen.findByRole('option', { name: 'binding-one (generation 3)' });
    expect(reviewCheckbox()).not.toBeChecked();
    await userEvent.click(saveButton());
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
  });

  it('cannot reuse the old ready binding after a failed bindings refresh', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await openDraft();
    vi.mocked(listWellArchitectedBindings).mockRejectedValueOnce({ status: 503 });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh review status' }));
    await screen.findByText(/Current bindings could not be confirmed/);
    expect(saveButton()).toBeDisabled(); expect(reviewCheckbox()).toBeDisabled();
    expect(screen.queryByRole('option', { name: 'binding-one (generation 2)' })).not.toBeInTheDocument();
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
  });

  it.each([412, 503, undefined])('preserves a schedule draft after save status %s and reconciles its same identity against fresh configuration', async status => {
    vi.mocked(saveWellArchitectedConfiguration).mockRejectedValueOnce({ status });
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await openDraft(); await userEvent.click(saveButton());
    await waitFor(() => expect(saveButton()).toBeDisabled());
    const initial = vi.mocked(saveWellArchitectedConfiguration).mock.calls[0][2].triggers!;
    const id = initial[1].trigger_id;
    // An uncertain response may have committed. Readback, never replay, determines the saved identity.
    const other = { ...daily, trigger_id: 'externally-added', enabled: false };
    const refreshed = { ...withTriggers([...initial, other]), etag: '"fresh-etag"', value: { ...withTriggers([...initial, other]).value, revision: 3 } };
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(refreshed);
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Well-Architected access' }));
    await screen.findByText(/Latest saved configuration loaded/);
    expect(screen.getByLabelText('Daily review time (UTC)')).toHaveValue('02:30');
    expect(saveButton()).toBeDisabled();
    const reconcile = screen.getByRole('button', { name: 'Review draft against refreshed configuration' });
    await waitFor(() => expect(reconcile).toBeEnabled()); await userEvent.click(reconcile);
    expect(reviewCheckbox()).not.toBeChecked();
    expect(saveWellArchitectedConfiguration).toHaveBeenCalledTimes(1);
    await userEvent.click(reviewCheckbox()); await userEvent.click(saveButton());
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledTimes(2));
    const call = vi.mocked(saveWellArchitectedConfiguration).mock.calls[1];
    expect(call[3]).toBe('"fresh-etag"');
    expect(call[2].triggers?.filter(item => item.trigger_id === id)).toHaveLength(1);
    expect(call[2].triggers).toContainEqual(other);
  });

  it('blocks all edits if refreshed configuration cannot be read and retains the schedule draft', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await openDraft();
    vi.mocked(getWellArchitectedConfiguration).mockRejectedValueOnce({ status: 503 });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Well-Architected access' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Refresh successfully before saving or starting work');
    expect(saveButton()).toBeDisabled(); expect(screen.getByLabelText('Daily review time (UTC)')).toHaveValue('02:30');
    await openSetup();
    expect(screen.getByRole('button', { name: 'Save review draft' })).toBeDisabled();
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
  });

  it('treats a mismatched saved schedule readback as uncertain and requires refresh', async () => {
    vi.mocked(saveWellArchitectedConfiguration).mockResolvedValue(config);
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await openDraft(); await userEvent.click(saveButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('save outcome is unknown');
    expect(saveButton()).toBeDisabled();
    expect(screen.queryByText(/Schedule changes saved/)).not.toBeInTheDocument();
  });

  it('fences pending schedule save responses and removes the draft when navigating to another Product scope', async () => {
    const pending = deferred<Versioned<WellArchitectedConfiguration>>();
    vi.mocked(saveWellArchitectedConfiguration).mockReturnValue(pending.promise);
    const view = render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await openDraft(); await userEvent.click(saveButton());
    vi.mocked(getWellArchitectedOptions).mockResolvedValue({ ...options, sources: [], connections: [] });
    vi.mocked(getWellArchitectedConfiguration).mockRejectedValue({ status: 404 });
    view.rerender(<WellArchitectedServiceTab {...props} projectId="project-two" />);
    await openSetup();
    await act(async () => pending.resolve(config));
    await navigate('Runs & schedules');
    expect(screen.queryByRole('group', { name: 'Review schedule draft' })).not.toBeInTheDocument();
    expect(screen.queryByText(/Schedule changes saved/)).not.toBeInTheDocument();
  });

  it('blocks duplicate saved trigger identities instead of replacing more than one schedule', async () => {
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(withTriggers([daily, { ...daily }]));
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    expect(await screen.findByRole('alert')).toHaveTextContent('trigger identities are duplicated');
    expect(screen.getByRole('button', { name: 'Add daily review schedule' })).toBeDisabled();
    for (const button of screen.getAllByRole('button', { name: 'Pause schedule daily-one' })) expect(button).toBeDisabled();
  });
});
