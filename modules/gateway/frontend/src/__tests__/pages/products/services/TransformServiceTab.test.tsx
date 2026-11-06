import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TransformServiceTab from '@/pages/products/services/TransformServiceTab';
import { checkTransformConfiguration, getTransformConfiguration, getTransformConfigurationVersion, getTransformOptions, listTransformBindings, listTransformResults, listTransformRuns, saveTransformConfiguration } from '@/services/l5Services';

vi.mock('@/services/l5Services', async importOriginal => ({
  ...await importOriginal<typeof import('@/services/l5Services')>(),
  getTransformConfiguration: vi.fn(), getTransformConfigurationVersion: vi.fn(), getTransformOptions: vi.fn(), saveTransformConfiguration: vi.fn(),
  checkTransformConfiguration: vi.fn(), listTransformBindings: vi.fn(), listTransformResults: vi.fn(), listTransformRuns: vi.fn(),
}));
const connection = { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'connection', record_id: 'connection-one', record_revision: '7' };
const source = { ...connection, record_type: 'repository_commit', record_id: 'example/widget', record_revision: 'abcdef123456' };
const profile = { ...connection, record_type: 'product', record_id: 'product-one', record_revision: '5' };
const options = { service_id: 'transform' as const, connections: [connection], sources: [source], regions: [], capabilities: ['refactor' as const], unavailable_reasons: ['executor_unqualified'], readiness: [{ capability: 'refactor', status: 'unqualified', gaps: [{ code: 'executor_unqualified', field: 'transform.executor_binding_ref' }] }] };
const configuration = { value: { scope: { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '5' }, configuration_id: 'config-one', revision: 2, state: 'needs_input', gaps: ['executor_unqualified'], handoff_state: 'pending', hash: 'original', settings: { service_id: 'transform', profile_ref: profile, source_refs: [source], capabilities: ['refactor'], enabled: false, overrides: [], transform: { connection_ref: connection, region: 'example-region', read_paths: ['src'], write_paths: ['src'], excluded_paths: ['.git'], goal_defaults: { kind: 'refactor', summary: 'Improve structure' } } } }, etag: '"original"' };
const props = { productId: 'product-one', projectId: 'project-one', tenantId: 'tenant-one' };

async function navigateTo(name: 'Setup' | 'Capabilities' | 'Runs & schedules' | 'Results') {
  const navigation = screen.getByRole('navigation', { name: 'Transform service sections' });
  await userEvent.click(within(navigation).getByRole('link', { name }));
  const section = screen.getByRole('region', { name });
  expect(section).toBeVisible();
  return section;
}

async function openDisclosure(title: string) {
  const summary = await screen.findByText(title, { selector: 'summary' });
  const details = summary.closest('details')!;
  if (!details.open) await userEvent.click(summary);
  expect(details).toHaveAttribute('open');
}

async function editSetup() {
  await navigateTo('Setup');
  await openDisclosure('Edit Transform setup');
}

async function showStatus() {
  await navigateTo('Setup');
  await openDisclosure('Configuration history and status');
}

describe('Transform configuration workspace', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getTransformOptions).mockResolvedValue(options);
    vi.mocked(getTransformConfiguration).mockResolvedValue(configuration as never);
    vi.mocked(checkTransformConfiguration).mockResolvedValue({ ready: false, gaps: ['executor_unqualified'] });
    vi.mocked(listTransformBindings).mockResolvedValue({ items: [] });
    vi.mocked(listTransformRuns).mockResolvedValue({ items: [] });
    vi.mocked(listTransformResults).mockResolvedValue({ items: [] });
  });
  it('keeps all four anchored sections mounted and navigable while setup is loading', async () => {
    vi.mocked(getTransformOptions).mockReturnValue(new Promise(() => {}));
    render(<TransformServiceTab {...props} />);
    const navigation = screen.getByRole('navigation', { name: 'Transform service sections' });
    for (const [name, step] of [['Setup', 'setup'], ['Capabilities', 'capabilities'], ['Runs & schedules', 'runs'], ['Results', 'results']]) {
      await userEvent.click(within(navigation).getByRole('link', { name }));
      expect(screen.getByRole('region', { name })).toHaveAttribute('id', `l5-transform-${step}`);
      expect(screen.getByRole('region', { name })).toBeVisible();
      expect(within(navigation).getByRole('link', { name })).toHaveAttribute('href', `#l5-transform-${step}`);
      expect(screen.queryByRole('button', { name: 'Start Transform run' })).not.toBeInTheDocument();
    }
    for (const step of ['setup', 'capabilities', 'runs', 'results']) expect(document.getElementById(`l5-transform-${step}`)).toBeInTheDocument();
  });
  it('groups controls by purpose and preserves the shared goal and advanced draft during navigation', async () => {
    const advanced = { ...configuration, value: { ...configuration.value, settings: { ...configuration.value.settings,
      transform: { ...configuration.value.settings.transform, executor_binding_ref: { ...connection, record_type: 'executor', record_id: 'executor-one' },
        test_policy_ref: { ...connection, record_type: 'test_policy', record_id: 'checks-one' } } } } };
    vi.mocked(getTransformConfiguration).mockResolvedValue(advanced as never);
    vi.mocked(saveTransformConfiguration).mockResolvedValue(advanced as never);
    render(<TransformServiceTab {...props} />);
    await showStatus();
    expect(screen.getByText(/Revision 2: needs_input/)).toBeVisible();
    await editSetup();
    const setup = screen.getByRole('region', { name: 'Setup' });
    expect(within(setup).getByLabelText('Repository commit')).toBeInTheDocument();
    expect(within(setup).getByLabelText('Transform region')).toHaveValue('example-region');
    const save = within(setup).getByRole('button', { name: 'Save Transform configuration' }) as HTMLButtonElement;
    expect(screen.getAllByRole('button', { name: 'Save Transform configuration' })).toHaveLength(1);
    expect(save.compareDocumentPosition(within(setup).getByText('Owner consent')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(setup).queryByRole('button', { name: 'Start Transform run' })).not.toBeInTheDocument();
    const capabilities = await navigateTo('Capabilities');
    expect(save.form).toBe(within(capabilities).getByLabelText('Capability').closest('form'));
    expect(within(capabilities).getByLabelText('Capability')).toBeInTheDocument();
    expect(within(capabilities).getByRole('article', { name: 'Dependency upgrade' })).toHaveTextContent('Not enabled for this Product');
    const runs = await navigateTo('Runs & schedules');
    expect(within(runs).getByRole('button', { name: 'Start Transform run' })).toBeDisabled();
    expect(within(runs).getByRole('complementary', { name: 'Scheduling availability' })).toHaveTextContent('Scheduled transformations: Not available yet');
    const results = await navigateTo('Results');
    expect(within(results).getByText(/Repository PR publication is unavailable/)).toBeInTheDocument();
    await navigateTo('Capabilities');
    const advancedSettings = screen.getByText('Advanced Transform settings').closest('details');
    expect(advancedSettings).not.toHaveAttribute('open');
    await userEvent.clear(screen.getByLabelText('Transformation goal'));
    await userEvent.type(screen.getByLabelText('Transformation goal'), 'Keep this unsaved goal');
    await editSetup();
    await userEvent.clear(screen.getByLabelText('Transform region'));
    await userEvent.type(screen.getByLabelText('Transform region'), 'changed-region');
    await navigateTo('Results');
    await navigateTo('Capabilities');
    expect(screen.getByLabelText('Transformation goal')).toHaveValue('Keep this unsaved goal');
    await editSetup();
    expect(screen.getByLabelText('Transform region')).toHaveValue('changed-region');
    expect(saveTransformConfiguration).not.toHaveBeenCalled();
    await navigateTo('Capabilities');
    await userEvent.click(screen.getByText('Advanced Transform settings'));
    const draft = JSON.parse((screen.getByLabelText('Transform settings (JSON)') as HTMLTextAreaElement).value);
    expect(draft).toEqual(expect.objectContaining({ region: 'changed-region', read_paths: ['src'], executor_binding_ref: advanced.value.settings.transform.executor_binding_ref,
      test_policy_ref: advanced.value.settings.transform.test_policy_ref, goal_defaults: { kind: 'refactor', summary: 'Keep this unsaved goal' } }));
    await editSetup();
    await userEvent.click(screen.getByRole('button', { name: 'Save Transform configuration' }));
    await waitFor(() => expect(saveTransformConfiguration).toHaveBeenCalledWith('product-one', 'project-one',
      expect.objectContaining({ transform: draft }), '"original"'));
  });
  it.each([
    ['dependency', 'example-package', '2.0.0'],
    ['runtime', 'python', '3.12'],
  ] as const)('saves the visible %s upgrade target with the backend goal shape', async (kind, name, version) => {
    vi.mocked(getTransformOptions).mockResolvedValue({ ...options, capabilities: ['refactor', 'dependency_upgrade'] });
    vi.mocked(saveTransformConfiguration).mockResolvedValue(configuration as never);
    render(<TransformServiceTab {...props} />);
    await showStatus();
    expect(within(screen.getByRole('region', { name: 'Setup' })).getByText(/New Transform binding setup: Not available yet/)).toBeInTheDocument();
    await navigateTo('Capabilities');
    await userEvent.selectOptions(screen.getByLabelText('Capability'), 'dependency_upgrade');
    await editSetup();
    expect(screen.getByRole('button', { name: 'Save Transform configuration' })).toBeDisabled();
    await navigateTo('Capabilities');
    expect(screen.getByText('Choose a target type and enter its name and requested version before saving.')).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Upgrade target type'), kind);
    await userEvent.type(screen.getByLabelText('Upgrade target name'), name);
    await editSetup();
    expect(screen.getByRole('button', { name: 'Save Transform configuration' })).toBeDisabled();
    await navigateTo('Capabilities');
    await userEvent.type(screen.getByLabelText('Requested target version'), version);
    await navigateTo('Results');
    await navigateTo('Capabilities');
    expect(screen.getByLabelText('Upgrade target name')).toHaveValue(name);
    expect(screen.getByLabelText('Requested target version')).toHaveValue(version);
    expect(saveTransformConfiguration).not.toHaveBeenCalled();
    await navigateTo('Runs & schedules');
    expect(screen.getByRole('button', { name: 'Start Transform run' })).toBeDisabled();
    await editSetup();
    await userEvent.click(screen.getByRole('button', { name: 'Save Transform configuration' }));
    await waitFor(() => expect(saveTransformConfiguration).toHaveBeenCalledWith('product-one', 'project-one',
      expect.objectContaining({ capabilities: ['dependency_upgrade'], enabled: false,
        transform: { ...configuration.value.settings.transform, goal_defaults: {
          kind: 'dependency_upgrade', summary: 'Improve structure', target: { kind, name, version },
        } } }), '"original"'));
    expect(vi.mocked(saveTransformConfiguration).mock.calls[0][2]).not.toHaveProperty('source_refs');
  });
  it('preserves an existing upgrade target and removes it explicitly when selecting refactor', async () => {
    const target = { kind: 'dependency', name: 'existing-package', version: '5.0.0' };
    const current = { ...configuration, value: { ...configuration.value, settings: { ...configuration.value.settings,
      capabilities: ['dependency_upgrade'], transform: { ...configuration.value.settings.transform,
        goal_defaults: { kind: 'dependency_upgrade', summary: 'Upgrade the selected package', target } } } } };
    vi.mocked(getTransformOptions).mockResolvedValue({ ...options, capabilities: ['refactor', 'dependency_upgrade'] });
    vi.mocked(getTransformConfiguration).mockResolvedValue(current as never);
    vi.mocked(saveTransformConfiguration).mockResolvedValue(configuration as never);
    render(<TransformServiceTab {...props} />);
    await showStatus();
    await navigateTo('Capabilities');
    expect(screen.getByLabelText('Upgrade target type')).toHaveValue('dependency');
    expect(screen.getByLabelText('Upgrade target name')).toHaveValue('existing-package');
    expect(screen.getByLabelText('Requested target version')).toHaveValue('5.0.0');
    await userEvent.selectOptions(screen.getByLabelText('Capability'), 'refactor');
    expect(screen.queryByLabelText('Upgrade target name')).not.toBeInTheDocument();
    await editSetup();
    await userEvent.click(screen.getByRole('button', { name: 'Save Transform configuration' }));
    await waitFor(() => expect(saveTransformConfiguration).toHaveBeenCalledWith('product-one', 'project-one',
      expect.objectContaining({ capabilities: ['refactor'], transform: { ...configuration.value.settings.transform,
        goal_defaults: { kind: 'refactor', summary: 'Upgrade the selected package' } } }), '"original"'));
  });
  it('keeps malformed advanced JSON intact until the user fixes it', async () => {
    render(<TransformServiceTab {...props} />);
    await showStatus();
    await navigateTo('Capabilities');
    await userEvent.click(screen.getByText('Advanced Transform settings'));
    await userEvent.clear(screen.getByLabelText('Transform settings (JSON)'));
    await userEvent.type(screen.getByLabelText('Transform settings (JSON)'), 'unfinished JSON');
    expect(screen.getByLabelText('Transformation goal')).toBeDisabled();
    await editSetup();
    expect(screen.getByLabelText('Transform region')).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Save Transform configuration' }));
    expect(screen.getByText('Transform settings must be a JSON object with a valid region, paths and goal.')).toBeInTheDocument();
    await navigateTo('Capabilities');
    expect(screen.getByLabelText('Transform settings (JSON)')).toHaveValue('unfinished JSON');
    expect(saveTransformConfiguration).not.toHaveBeenCalled();
  });
  it.each([403, 404])('keeps configuration editing available on binding HTTP %s but removes it on actual configuration revocation', async status => {
    vi.mocked(listTransformBindings).mockRejectedValue({ status, detail: { code: 'delegation_denied' } });
    vi.mocked(saveTransformConfiguration).mockResolvedValue(configuration as never);
    render(<TransformServiceTab {...props} />);
    await navigateTo('Runs & schedules');
    expect(await screen.findByText(/Execution binding or delegation is unavailable/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start Transform run' })).toBeDisabled();
    await editSetup();
    expect(screen.getByRole('button', { name: 'Save Transform configuration' })).toBeEnabled();
    expect(screen.queryByText(/Transform access is unavailable for this workspace/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Save Transform configuration' }));
    await waitFor(() => expect(saveTransformConfiguration).toHaveBeenCalledTimes(1));
    vi.mocked(checkTransformConfiguration).mockRejectedValueOnce({ status: 404 });
    await navigateTo('Runs & schedules');
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Transform status' }));
    expect(await screen.findByText(/Previously shown details were removed/)).toBeInTheDocument();
    await navigateTo('Setup');
    expect(screen.queryByRole('button', { name: 'Save Transform configuration' })).not.toBeInTheDocument();
    for (const name of ['Setup', 'Capabilities', 'Runs & schedules', 'Results'] as const) await navigateTo(name);
    expect(screen.queryByText('Saved references and profile details')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Transformation goal')).not.toBeInTheDocument();
  });
  it('shows readiness and read-only history, then saves against the current ETag without a run', async () => {
    vi.mocked(getTransformConfigurationVersion).mockResolvedValue({ value: { ...configuration.value, revision: 1, state: 'draft' }, etag: '"older"' } as never);
    vi.mocked(saveTransformConfiguration).mockResolvedValue({ value: { ...configuration.value, revision: 3 }, etag: '"new"' } as never);
    render(<TransformServiceTab {...props} />);
    await showStatus();
    expect(await screen.findByText(/Revision 2: needs_input/)).toBeInTheDocument();
    await navigateTo('Capabilities');
    expect(screen.getByText(/refactor: unqualified/)).toBeInTheDocument();
    await showStatus();
    await userEvent.selectOptions(screen.getByLabelText('Configuration history'), '1');
    expect(await screen.findByText(/Historical revision 1: draft/)).toBeInTheDocument();
    await editSetup();
    await userEvent.click(screen.getByRole('button', { name: 'Save Transform configuration' }));
    await waitFor(() => expect(saveTransformConfiguration).toHaveBeenCalledWith('product-one', 'project-one', expect.objectContaining({ service_id: 'transform', capabilities: ['refactor'], transform: expect.objectContaining({ connection_ref: connection }) }), '"original"'));
    expect(vi.mocked(saveTransformConfiguration).mock.calls[0][2]).not.toHaveProperty('source_refs');
    expect(await screen.findByText(/no run or publication was started/)).toBeInTheDocument();
  });
  it('requires a reason for explicit source choice on conditional creation', async () => {
    vi.mocked(getTransformConfiguration).mockRejectedValue({ status: 404 });
    vi.mocked(saveTransformConfiguration).mockResolvedValue(configuration as never);
    render(<TransformServiceTab {...props} />);
    await editSetup();
    await screen.findByRole('option', { name: /example\/widget/ });
    await userEvent.selectOptions(screen.getByLabelText('Repository commit'), '0');
    await userEvent.selectOptions(screen.getByLabelText('Connection'), '0');
    await userEvent.click(screen.getByRole('button', { name: 'Save Transform configuration' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Explain why');
    expect(saveTransformConfiguration).not.toHaveBeenCalled();
    await userEvent.type(screen.getByLabelText('Reason for explicit repository choice'), 'Select pinned revision');
    await userEvent.click(screen.getByRole('button', { name: 'Save Transform configuration' }));
    await waitFor(() => expect(saveTransformConfiguration).toHaveBeenCalledWith('product-one', 'project-one', expect.objectContaining({ source_refs: [source], overrides: [expect.objectContaining({ field: 'source_refs', reason: 'Select pinned revision' })] }), undefined));
  });
  it('clears stale details and does not replay a conflicting edit or a denied history read', async () => {
    vi.mocked(saveTransformConfiguration).mockRejectedValue({ status: 412 });
    render(<TransformServiceTab {...props} />);
    await showStatus();
    await editSetup();
    await userEvent.click(screen.getByRole('button', { name: 'Save Transform configuration' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('changed elsewhere');
    expect(screen.queryByText(/Revision 2:/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save Transform configuration' })).toBeDisabled();
    vi.mocked(getTransformConfiguration).mockResolvedValue({ value: { ...configuration.value, revision: 3 }, etag: '"fresh"' } as never);
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Transform access and configuration' }));
    await showStatus();
    expect(await screen.findByText(/Revision 3: needs_input/)).toBeInTheDocument();
    expect(saveTransformConfiguration).toHaveBeenCalledTimes(1);
    vi.mocked(getTransformConfigurationVersion).mockRejectedValue({ status: 403 });
    await userEvent.selectOptions(screen.getByLabelText('Configuration history'), '1');
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details were removed');
    expect(screen.queryByText(/Revision 3:/)).not.toBeInTheDocument();
  });
});
