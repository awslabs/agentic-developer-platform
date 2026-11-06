import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DevOpsServiceTab from '@/pages/products/services/DevOpsServiceTab';
import { getDevOpsConfiguration, getDevOpsOptions, listDevOpsBindings, listDevOpsRuns, listDevOpsResults, saveDevOpsConfiguration } from '@/services/l5Services';
import { listOwnerGrants } from '@/services/l5OwnerConsent';

vi.mock('@/services/l5Services', async original => ({ ...await original<typeof import('@/services/l5Services')>(),
  getDevOpsConfiguration: vi.fn(), getDevOpsOptions: vi.fn(), saveDevOpsConfiguration: vi.fn(), listDevOpsBindings: vi.fn(), listDevOpsRuns: vi.fn(), listDevOpsResults: vi.fn(),
}));
vi.mock('@/services/l5OwnerConsent', async original => ({ ...await original<typeof import('@/services/l5OwnerConsent')>(), listOwnerGrants: vi.fn() }));
const reference = (projectId: string, recordType = 'connection') => ({ tenant_id: 'tenant-one', project_id: projectId,
  source_connection_id: 'connection-one', record_type: recordType, record_id: recordType === 'product' ? 'product-one' : 'connection-one', record_revision: '2' });
const options = (projectId: string) => ({ connections: [reference(projectId)], sources: [{ ...reference(projectId, 'environment'), record_id: 'environment-one' }],
  regions: [], capabilities: ['investigation' as const], unavailable_reasons: ['region_options_unavailable'] });
const configuration = (projectId: string) => ({ value: {
  scope: { tenant_id: 'tenant-one', product_id: 'product-one', project_id: projectId, relationship_revision: '3', owner_id: 'owner-one' },
  configuration_id: 'config-one', revision: 2, state: 'needs_input' as const, handoff_state: 'pending', hash: 'hash', inherited_fields: ['purpose', 'source_refs'], gaps: ['investigation.needs_space'],
  settings: { service_id: 'devops' as const, profile_ref: reference(projectId, 'product'), source_refs: options(projectId).sources, capabilities: ['investigation' as const], enabled: false, visibility: 'only_me' as const,
    exclusions: [], overrides: [], triggers: [], devops: { hosting_connection_ref: reference(projectId), access_policy_ref: reference(projectId), region: 'us-east-1', space_mode: 'create' as const,
      accounts: [], integrations: [], authentication_mode: 'scoped_iam_federation' as const, permitted_native_actions: [], knowledge_policy: { source_refs: [], visibility: 'only_me' as const, publish_results: false, refresh: 'manual' as const },
      trigger_policies: [], prevention_enabled: false, retention_days: 30, browser_launch_policy: 'disabled' as const },
  },
}, etag: '"two"' });
const view = () => render(<DevOpsServiceTab productId="product-one" projectId="project-one" tenantId="tenant-one" />);
async function navigate(name: string) {
  await userEvent.click(within(screen.getByRole('navigation', { name: 'DevOps service sections' })).getByRole('link', { name, exact: true }));
}
async function edit() {
  await navigate('Setup');
  await waitFor(() => expect(screen.getByRole('button', { name: 'Change setup' })).toBeEnabled());
  await userEvent.click(screen.getByRole('button', { name: 'Change setup' }));
  return screen.getByRole('dialog', { name: 'Edit DevOps setup' });
}
async function close(title = 'Edit DevOps setup') { await userEvent.click(screen.getByRole('button', { name: `Close ${title}` })); }

describe('DevOps simple setup', () => {
  beforeEach(() => {
    vi.resetAllMocks(); sessionStorage.clear(); window.history.replaceState({}, '', '/');
    vi.mocked(getDevOpsOptions).mockImplementation(async (_, projectId) => options(projectId));
    vi.mocked(getDevOpsConfiguration).mockImplementation(async (_, projectId) => configuration(projectId) as never);
    vi.mocked(listDevOpsBindings).mockResolvedValue({ items: [] });
    vi.mocked(listDevOpsRuns).mockResolvedValue({ items: [] });
    vi.mocked(listDevOpsResults).mockResolvedValue({ items: [] });
    vi.mocked(listOwnerGrants).mockResolvedValue({ items: [] });
  });
  it('shows three compact summaries, one refresh action and no editing or recovery controls by default', async () => {
    view(); await waitFor(() => expect(within(screen.getByRole('region', { name: 'Workload', exact: true })).getByText('environment-one', { exact: true })).toBeVisible());
    const setup = within(screen.getByRole('region', { name: 'Setup', exact: true }));
    for (const name of ['Workload', 'AWS connection', 'DevOps workspace']) expect(setup.getByRole('region', { name, exact: true })).toBeVisible();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Read retained setup receipt|Read owner consent/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /Refresh/ })).toHaveLength(1);
    expect(within(screen.getByRole('navigation', { name: 'DevOps service sections' })).getAllByRole('link').map(item => item.textContent)).toEqual(['Setup', 'Runs & schedules', 'Results']);
  });
  it('hydrates reordered references, keeps normal editor fields visible and retains the draft across closing', async () => {
    const current = configuration('project-one');
    const reorder = <T extends object,>(ref: T) => Object.fromEntries(Object.entries(ref).reverse()) as T;
    current.value.settings.devops.hosting_connection_ref = reorder(current.value.settings.devops.hosting_connection_ref);
    current.value.settings.source_refs = current.value.settings.source_refs.map(reorder);
    vi.mocked(getDevOpsConfiguration).mockResolvedValue(current as never);
    view(); const panel = await edit();
    expect(within(panel).getByLabelText('Hosting AWS connection')).toHaveValue('0');
    expect(within(panel).getByRole('checkbox', { name: 'environment-one' })).toBeChecked();
    expect(within(panel).getByLabelText('AWS Region').closest('details')).toBeNull();
    await userEvent.clear(screen.getByLabelText('AWS Region')); await userEvent.type(screen.getByLabelText('AWS Region'), 'us-west-2');
    await close(); await navigate('Results'); await edit();
    expect(screen.getByLabelText('AWS Region')).toHaveValue('us-west-2');
    expect(saveDevOpsConfiguration).not.toHaveBeenCalled();
  });
  it('asks before discarding a new, unsaved configuration', async () => {
    vi.mocked(getDevOpsConfiguration).mockRejectedValue({ status: 404 });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    view(); await edit();
    await userEvent.type(screen.getByLabelText('AWS Region'), 'us-east-1');
    await close(); await userEvent.click(screen.getByRole('button', { name: 'Refresh setup' }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(getDevOpsConfiguration).toHaveBeenCalledOnce();
    await edit(); expect(screen.getByLabelText('AWS Region')).toHaveValue('us-east-1');
    confirm.mockRestore();
  });
  it('saves explicit overrides with the original precondition and closes the editor after confirmation', async () => {
    vi.mocked(saveDevOpsConfiguration).mockResolvedValue(configuration('project-one') as never);
    view(); await edit();
    await userEvent.clear(screen.getByLabelText('AWS Region')); await userEvent.type(screen.getByLabelText('AWS Region'), 'us-west-2');
    await userEvent.type(screen.getByLabelText('Reason for explicit Region or environment override'), 'Use the workload region');
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(saveDevOpsConfiguration).toHaveBeenCalledWith('product-one', 'project-one', expect.objectContaining({
      region: 'us-west-2', overrides: [{ field: 'region', value: 'us-west-2', reason: 'Use the workload region' }], enabled: false,
    }), '"two"'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByText(/Setup saved/)).toBeVisible();
  });
  it('does not silently select a new connection revision', async () => {
    const current = configuration('project-one'); current.value.settings.devops.hosting_connection_ref.record_revision = '1';
    vi.mocked(getDevOpsConfiguration).mockResolvedValue(current as never);
    view(); await screen.findByText('Choose a current AWS connection'); await edit();
    expect(screen.getByLabelText('Hosting AWS connection')).toHaveValue('-1');
  });
  it('shows saved workload and logs and retains existing overrides when saving unchanged settings', async () => {
    const current = configuration('project-one'); const ref = reference('project-one', 'resource');
    const saved = { ...current, value: { ...current.value, settings: { ...current.value.settings, enabled: true, region: 'us-east-1',
      overrides: [{ field: 'region', value: 'us-east-1', reason: 'Workload region' }], devops: { ...current.value.settings.devops,
        trigger_policies: [{ kind: 'manual', enabled: true, capability: 'investigation', resource_refs: [{ ...ref, record_id: 'arn:aws:lambda:us-east-1:111122223333:function:example-worker' }],
          telemetry_refs: [{ ...ref, record_type: 'telemetry', record_id: 'arn:aws:logs:us-east-1:111122223333:log-group:/aws/lambda/example-worker:*' }] }],
      } } } };
    vi.mocked(getDevOpsConfiguration).mockResolvedValue(saved as never); vi.mocked(saveDevOpsConfiguration).mockResolvedValue(saved as never);
    view(); expect(await screen.findByText('example-worker')).toBeVisible(); expect(screen.getByText('/aws/lambda/example-worker')).toBeVisible();
    await edit(); expect(screen.queryByText(/Resource setup requires a saved disabled/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(saveDevOpsConfiguration).toHaveBeenCalledOnce());
    expect(vi.mocked(saveDevOpsConfiguration).mock.calls[0][2]).toMatchObject({ overrides: saved.value.settings.overrides, devops: { trigger_policies: saved.value.settings.devops.trigger_policies } });
  });
  it.each([412, 503])('preserves failed edits and requires a confirmed refresh after %s', async status => {
    vi.mocked(saveDevOpsConfiguration).mockRejectedValue({ status });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    view(); await edit();
    await userEvent.clear(screen.getByLabelText('AWS Region')); await userEvent.type(screen.getByLabelText('AWS Region'), 'us-west-2');
    await userEvent.type(screen.getByLabelText('Reason for explicit Region or environment override'), 'Use workload region');
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled());
    await close(); expect(screen.getByRole('alert')).toBeVisible(); await userEvent.click(screen.getByRole('button', { name: 'Refresh setup' }));
    expect(getDevOpsConfiguration).toHaveBeenCalledOnce(); await edit(); expect(screen.getByLabelText('AWS Region')).toHaveValue('us-west-2'); await close();
    confirm.mockReturnValue(true); await userEvent.click(screen.getByRole('button', { name: 'Refresh setup' }));
    await waitFor(() => expect(getDevOpsConfiguration).toHaveBeenCalledTimes(2)); await edit(); expect(screen.getByLabelText('AWS Region')).toHaveValue('us-east-1');
    expect(saveDevOpsConfiguration).toHaveBeenCalledOnce(); confirm.mockRestore();
  });
  it('keeps errors from access management visible after closing the panel', async () => {
    vi.mocked(listOwnerGrants).mockRejectedValue({ status: 503 });
    view(); await screen.findByText(/Owner permission could not be checked/);
    await userEvent.click(screen.getByRole('button', { name: 'Manage access' }));
    await userEvent.click(screen.getByRole('button', { name: 'Read owner consent' }));
    expect(await within(screen.getByRole('dialog')).findByRole('alert')).toHaveTextContent('Consent could not be read');
    await close('Manage investigation access');
    expect(within(screen.getByRole('region', { name: 'AWS connection', exact: true })).getByText(/Consent could not be read/)).toBeVisible();
  });
  it('ignores late responses from the previous project and rejects foreign choices', async () => {
    let resolve!: (value: ReturnType<typeof options>) => void;
    vi.mocked(getDevOpsOptions).mockImplementation((_, id) => id === 'project-one' ? new Promise(done => { resolve = done; }) : Promise.resolve(options(id)));
    const rendered = view(); rendered.rerender(<DevOpsServiceTab productId="product-one" projectId="project-two" tenantId="tenant-one" />);
    await waitFor(() => expect(within(screen.getByRole('region', { name: 'Workload', exact: true })).getByText('environment-one', { exact: true })).toBeVisible()); resolve(options('project-one'));
    await waitFor(() => expect(getDevOpsConfiguration).toHaveBeenCalled());
    vi.mocked(getDevOpsOptions).mockResolvedValueOnce(options('project-one')); vi.spyOn(window, 'confirm').mockReturnValueOnce(true);
    await userEvent.click(screen.getByRole('button', { name: 'Refresh setup' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('access is unavailable'); expect(screen.queryByText('environment-one')).not.toBeInTheDocument();
  });
  it('removes project data when saving loses access', async () => {
    vi.mocked(saveDevOpsConfiguration).mockRejectedValue({ status: 403 });
    view(); await edit(); await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(await within(screen.getByRole('dialog')).findByRole('alert')).toHaveTextContent('access is unavailable');
    expect(screen.queryByText('environment-one')).not.toBeInTheDocument();
  });
  it('keeps the old capability URL usable by opening run activities', async () => {
    window.history.replaceState({}, '', '/#l5-devops-capabilities'); view();
    await waitFor(() => expect(screen.getByRole('region', { name: 'Runs & schedules', exact: true })).toBeVisible());
    await screen.findByRole('heading', { name: 'Start an investigation' });
    expect(screen.queryByRole('combobox', { name: 'Run activity' })).not.toBeInTheDocument();
    fireEvent(window, new HashChangeEvent('hashchange'));
  });
});
