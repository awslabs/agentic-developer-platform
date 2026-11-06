import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import WellArchitectedServiceTab from '@/pages/products/services/WellArchitectedServiceTab';
import ProductServices from '@/pages/products/services/ProductServices';
import { compareWellArchitectedResults, checkWellArchitectedConfiguration, getWellArchitectedConfiguration, getWellArchitectedOptions, saveWellArchitectedConfiguration,
  getWellArchitectedPublicationStatus, getWellArchitectedResult, getWellArchitectedRun, listWellArchitectedBindings,
  listWellArchitectedResults, listWellArchitectedRuns, startWellArchitectedBinding, submitWellArchitectedRun } from '@/services/l5Services';

vi.mock('@/services/l5Services', async original => ({ ...await original<typeof import('@/services/l5Services')>(),
  compareWellArchitectedResults: vi.fn(), checkWellArchitectedConfiguration: vi.fn(), getWellArchitectedConfiguration: vi.fn(),
  getWellArchitectedOptions: vi.fn(), saveWellArchitectedConfiguration: vi.fn(),
  getWellArchitectedPublicationStatus: vi.fn(), getWellArchitectedResult: vi.fn(), getWellArchitectedRun: vi.fn(), listWellArchitectedBindings: vi.fn(),
  listWellArchitectedResults: vi.fn(), listWellArchitectedRuns: vi.fn(), startWellArchitectedBinding: vi.fn(), submitWellArchitectedRun: vi.fn() }));

const props = { productId: 'product-one', projectId: 'project-one', tenantId: 'tenant-one' };
const connection = { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'connection', record_id: 'connection-one', record_revision: '7' };
const source = { ...connection, record_type: 'document', record_id: 'design-one', record_revision: '5' };
const profile = { ...connection, record_type: 'product', record_id: 'product-one' };
const options = { connections: [connection], sources: [source], regions: [], capabilities: ['architecture_review' as const], unavailable_reasons: [] };
const configuration = { value: { scope: { tenant_id: 'tenant-one', project_id: 'project-one', product_id: 'product-one', relationship_revision: '5' },
  configuration_id: 'config-one', revision: 1, state: 'needs_input', gaps: ['binding_not_ready'], handoff_state: 'linked', hash: 'hash-one', inherited_fields: ['purpose'],
  settings: { service_id: 'well_architected', profile_ref: profile, source_refs: [source], capabilities: ['architecture_review'], enabled: false, purpose: 'Review documented decisions',
    well_architected: { review_mode: 'source_only', pillars: ['SECURITY'], goal_refs: [] } } }, etag: '"hash-one"' };

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
async function openStatus() {
  await navigate('Setup');
  const summary = await screen.findByText('Configuration status');
  if (!summary.closest('details')!.open) await userEvent.click(summary);
}

describe('Well-Architected Product tab', () => {
  beforeEach(() => { vi.resetAllMocks(); vi.mocked(getWellArchitectedOptions).mockResolvedValue(options);
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(configuration as never);
    vi.mocked(checkWellArchitectedConfiguration).mockResolvedValue({ ready: false, gaps: ['binding_not_ready'] });
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [] });
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [] });
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [] }); });

  it('keeps initial setup and service selection available when no configuration is visible and readiness is unconfirmed', async () => {
    const repository = { ...source, record_type: 'repository_commit', record_id: 'repository-one', record_revision: 'a'.repeat(40) };
    vi.mocked(getWellArchitectedOptions).mockResolvedValue({ ...options, sources: [source, repository], unavailable_reasons: ['provider_unqualified'] });
    vi.mocked(getWellArchitectedConfiguration).mockRejectedValue({ status: 404 });
    vi.mocked(listWellArchitectedBindings).mockRejectedValue({ status: 404 });
    render(<ProductServices {...props} activeService="well_architected" />);
    await screen.findByLabelText('Authorized connection');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('complementary', { name: 'Review setup guidance' })).toHaveTextContent('Review readiness has not been confirmed. Complete and save the source-only provider setup, review owner consent, then set up a binding and check review readiness.');
    expect(screen.queryByText(/Unavailable:|provider_unqualified/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('document: design-one (5)')).toBeEnabled();
    expect(screen.getByLabelText(`repository_commit: repository-one (${'a'.repeat(40)})`)).toBeEnabled();
    expect(screen.getByLabelText('Authorized connection')).toBeEnabled();
    expect(screen.getByLabelText('Enable this review configuration when saved')).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Save review draft' })).toBeDisabled();
    const services = within(screen.getByRole('navigation', { name: 'Product services' }));
    expect(services.getAllByRole('button')).toHaveLength(5);
    services.getAllByRole('button').forEach(button => expect(button).toBeEnabled());
    expect(services.getByRole('button', { name: 'Well-Architected' })).toHaveAttribute('aria-current', 'page');
    const navigation = screen.getByRole('navigation', { name: 'Well-Architected service sections' });
    for (const [name, step] of [['Setup', 'setup'], ['Capabilities', 'capabilities'], ['Runs & schedules', 'runs'], ['Results', 'results']]) {
      await navigate(name as 'Setup' | 'Capabilities' | 'Runs & schedules' | 'Results');
      expect(screen.getByRole('region', { name })).toHaveAttribute('id', `l5-well_architected-${step}`);
      expect(within(navigation).getByRole('link', { name })).toHaveAttribute('href', `#l5-well_architected-${step}`);
    }
    await navigate('Setup');
    expect(screen.queryByRole('button', { name: 'Set up review binding' })).not.toBeInTheDocument();
    await navigate('Runs & schedules');
    expect(screen.queryByRole('button', { name: 'Start source-only review' })).not.toBeInTheDocument();
    expect(listWellArchitectedBindings).not.toHaveBeenCalled();
    expect(listWellArchitectedRuns).not.toHaveBeenCalled();
    expect(listWellArchitectedResults).not.toHaveBeenCalled();
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
    expect(startWellArchitectedBinding).not.toHaveBeenCalled();
    expect(submitWellArchitectedRun).not.toHaveBeenCalled();
  });

  it('separates setup, source-only capabilities, runs, and results without resetting drafts or read receipts', async () => {
    const run = { run_id: 'run-one', scope: configuration.value.scope, revision: 2, state: 'completed', provider_state: 'completed' };
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [run] } as never);
    vi.mocked(getWellArchitectedRun).mockResolvedValue({ value: run, etag: '"run-two"' } as never);
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await userEvent.click(await screen.findByRole('button', { name: 'run-one: completed (completed)' }));
    const runs = screen.getByRole('region', { name: 'Runs & schedules' });
    expect(within(runs).getByRole('button', { name: 'Check review readiness' })).toBeVisible();
    expect(within(runs).getByRole('button', { name: 'Start source-only review' })).toBeDisabled();
    expect(within(runs).getByRole('complementary', { name: 'Well-Architected schedules' })).toHaveTextContent('Daily review schedules (UTC)');
    await openSetup();
    expect(runs).not.toBeVisible();
    const setup = screen.getByRole('region', { name: 'Setup' });
    expect(within(setup).getByRole('group', { name: 'Pinned review sources' })).toBeVisible();
    expect(within(setup).getByRole('button', { name: 'Set up review binding' })).toBeVisible();
    const save = within(setup).getByRole('button', { name: 'Save review draft' }) as HTMLButtonElement;
    expect(screen.getAllByRole('button', { name: 'Save review draft' })).toHaveLength(1);
    expect(save.compareDocumentPosition(within(setup).getByText('Owner consent')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(setup).queryByRole('button', { name: 'Start source-only review' })).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('Provider region'), 'draft-region');
    await navigate('Capabilities');
    const capabilities = screen.getByRole('region', { name: 'Capabilities' });
    expect(save.form).toBe(within(capabilities).getByRole('group', { name: 'Review pillars' }).closest('form'));
    expect(within(capabilities).getByRole('group', { name: 'Review pillars' })).toBeVisible();
    expect(within(capabilities).getByText(/Review mode: source only/)).toBeVisible();
    expect(within(capabilities).getByRole('article', { name: 'AWS resource inspection' })).toHaveTextContent('Not available yet');
    await userEvent.click(screen.getByLabelText('RESILIENCE'));
    await navigate('Results');
    expect(screen.getByText('No retained review results are visible.')).toBeVisible();
    await navigate('Capabilities');
    expect(screen.getByLabelText('RESILIENCE')).toBeChecked();
    await openSetup();
    expect(screen.getByLabelText('Provider region')).toHaveValue('draft-region');
    await navigate('Runs & schedules');
    expect(within(runs).getByText(/Run revision 2: completed/)).toBeVisible();
    expect(listWellArchitectedRuns).toHaveBeenCalledTimes(1);
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
    expect(submitWellArchitectedRun).not.toHaveBeenCalled();
  });

  it('shows provenance and checks readiness without starting a run', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await openStatus();
    expect(await screen.findByText(/Revision 1: needs_input/)).toBeInTheDocument();
    expect(screen.getByText(/Inherited: purpose; purpose: Review documented decisions/)).toBeInTheDocument();
    await navigate('Runs & schedules');
    await userEvent.click(screen.getByRole('button', { name: 'Check review readiness' }));
    expect(await screen.findByText(/Readiness: not ready; gaps: binding_not_ready/)).toBeInTheDocument();
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
  });

  it('saves reasoned source-only choices against the ETag without enabling work', async () => {
    vi.mocked(saveWellArchitectedConfiguration).mockResolvedValue({ ...configuration, value: { ...configuration.value, revision: 2 } } as never);
    render(<WellArchitectedServiceTab {...props} />); await openSetup();
    await userEvent.selectOptions(screen.getByLabelText('Authorized connection'), '0');
    await userEvent.type(screen.getByLabelText('Provider region'), 'example-region');
    await userEvent.type(screen.getByLabelText('Reason for explicit choices'), 'Pin review inputs');
    await userEvent.click(screen.getByRole('button', { name: 'Save review draft' }));
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledWith('product-one', 'project-one',
      expect.objectContaining({ service_id: 'well_architected', capabilities: ['architecture_review'], enabled: false, source_refs: [source],
        well_architected: expect.objectContaining({ pillars: ['SECURITY'], review_mode: 'source_only' }),
        overrides: expect.arrayContaining([expect.objectContaining({ field: 'source_refs', reason: 'Pin review inputs' })]) }), '"hash-one"'));
    expect(screen.getByText(/no review or AWS discovery was started/)).toBeInTheDocument();
  });

  it('preserves an enabled configuration when editing review pillars', async () => {
    const enabledConfiguration = { ...configuration, value: { ...configuration.value,
      settings: { ...configuration.value.settings, enabled: true, connection_ref: connection, region: 'example-region' } } };
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(enabledConfiguration as never);
    vi.mocked(saveWellArchitectedConfiguration).mockResolvedValue(enabledConfiguration as never);
    render(<WellArchitectedServiceTab {...props} />);
    await openSetup();
    await navigate('Capabilities');
    await userEvent.click(screen.getByLabelText('RESILIENCE'));
    await openSetup();
    await userEvent.type(screen.getByLabelText('Reason for explicit choices'), 'Expand review pillars');
    await userEvent.click(screen.getByRole('button', { name: 'Save review draft' }));
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledWith('product-one', 'project-one',
      expect.objectContaining({ enabled: true, well_architected: expect.objectContaining({ pillars: ['SECURITY', 'RESILIENCE'] }) }), '"hash-one"'));
    expect(startWellArchitectedBinding).not.toHaveBeenCalled();
    expect(submitWellArchitectedRun).not.toHaveBeenCalled();
  });

  it('defaults a new review draft to disabled', async () => {
    vi.mocked(getWellArchitectedConfiguration).mockRejectedValue({ status: 404 });
    vi.mocked(saveWellArchitectedConfiguration).mockResolvedValue(configuration as never);
    render(<WellArchitectedServiceTab {...props} />);
    await userEvent.click(await screen.findByLabelText('document: design-one (5)'));
    await navigate('Capabilities');
    await userEvent.click(screen.getByLabelText('SECURITY'));
    await openSetup();
    await userEvent.selectOptions(screen.getByLabelText('Authorized connection'), '0');
    await userEvent.type(screen.getByLabelText('Provider region'), 'example-region');
    await userEvent.type(screen.getByLabelText('Reason for explicit choices'), 'Pin review inputs');
    await userEvent.click(screen.getByRole('button', { name: 'Save review draft' }));
    await waitFor(() => expect(saveWellArchitectedConfiguration).toHaveBeenCalledWith('product-one', 'project-one',
      expect.objectContaining({ enabled: false }), undefined));
  });

  it('requires an explicit connection choice instead of using the first authorized connection', async () => {
    render(<WellArchitectedServiceTab {...props} />);
    await openSetup();
    expect(screen.getByLabelText('Authorized connection')).toHaveValue('');
    await userEvent.type(screen.getByLabelText('Provider region'), 'example-region');
    await userEvent.type(screen.getByLabelText('Reason for explicit choices'), 'Pin review inputs');
    await userEvent.click(screen.getByRole('button', { name: 'Save review draft' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Select an authorized connection and an explicit region');
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
  });

  it('clears foreign-project configuration without exposing details', async () => {
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue({ ...configuration, value: { ...configuration.value,
      scope: { ...configuration.value.scope, project_id: 'foreign-project' } } } as never);
    render(<WellArchitectedServiceTab {...props} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details were removed');
    expect(screen.queryByText(/Revision 1: needs_input/)).not.toBeInTheDocument();
  });

  it('disables ineligible capabilities and explains why', async () => {
    vi.mocked(getWellArchitectedOptions).mockResolvedValue({ ...options, capabilities: [], unavailable_reasons: ['catalog_mapping_unresolved'] });
    render(<WellArchitectedServiceTab {...props} />);
    expect(await screen.findByText(/Ask the platform administrator to complete the Well-Architected capability mapping/)).toBeInTheDocument();
    await openSetup();
    expect(screen.getByRole('button', { name: 'Save review draft' })).toBeDisabled();
  });
});

describe('Well-Architected review actions', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getWellArchitectedOptions).mockResolvedValue(options);
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(configuration as never);
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [] });
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [] });
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [] });
    vi.mocked(getWellArchitectedPublicationStatus).mockResolvedValue({ result_id: 'result-one', revision: 3, consumers: [] });
  });
  it('keeps first-review setup available without requesting results when authorized run history is empty', async () => {
    vi.mocked(listWellArchitectedResults).mockRejectedValue({ status: 404 });
    vi.mocked(startWellArchitectedBinding).mockResolvedValue({ value: { operation_id: 'setup-one', state: 'accepted', gaps: [], revision: 1 }, etag: '"1"' } as never);
    render(<WellArchitectedServiceTab {...props} />);
    await openSetup();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Set up review binding' })).toBeEnabled());
    expect(listWellArchitectedRuns).toHaveBeenCalledWith('product-one', 'project-one');
    expect(listWellArchitectedResults).not.toHaveBeenCalled();
    expect(startWellArchitectedBinding).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Set up review binding' }));
    await waitFor(() => expect(startWellArchitectedBinding).toHaveBeenCalledWith('product-one', 'project-one', 1));
    expect(await screen.findByText(/This is not proof that a native profile is ready/)).toBeInTheDocument();
  });
  it.each([403, 404])('removes setup when the run list denies access with HTTP %s', async status => {
    vi.mocked(listWellArchitectedRuns).mockRejectedValue({ status });
    render(<WellArchitectedServiceTab {...props} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details were removed');
    expect(screen.queryByRole('button', { name: 'Set up review binding' })).not.toBeInTheDocument();
    expect(listWellArchitectedResults).not.toHaveBeenCalled();
  });
  it.each([403, 404])('removes setup when results for existing runs deny access with HTTP %s', async status => {
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [{ scope: configuration.value.scope, run_id: 'run-one' }] } as never);
    vi.mocked(listWellArchitectedResults).mockRejectedValue({ status });
    render(<WellArchitectedServiceTab {...props} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details were removed');
    expect(screen.queryByRole('button', { name: 'Set up review binding' })).not.toBeInTheDocument();
  });
  it.each([
    ['options', true], ['configuration', true], ['configuration', false],
  ] as const)('keeps writes and execution blocked after a failed %s refresh (ready binding: %s) until fresh readback succeeds', async (failedRead, hasBinding) => {
    const current = { ...configuration, value: { ...configuration.value, state: 'ready', gaps: [],
      settings: { ...configuration.value.settings, enabled: true, connection_ref: connection, region: 'eu-west-1' } } };
    const scope = current.value.scope;
    const reviewRun = { scope, run_id: 'run-one', revision: 2, state: 'completed', provider_state: 'completed' };
    const reviewResult = { scope, result_id: 'result-one', run_id: 'run-one', revision: 3, service_id: 'well_architected', coverage: 'partial', coverage_mode: 'selected_snapshot' };
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(current as never);
    vi.mocked(checkWellArchitectedConfiguration).mockResolvedValue({ ready: true, gaps: [] });
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: hasBinding
      ? [{ binding_id: 'binding-one', scope, state: 'ready', configuration_id: 'config-one' }] : [] } as never);
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [reviewRun] } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [reviewResult] } as never);
    vi.mocked(getWellArchitectedResult).mockResolvedValue({ value: reviewResult, etag: '"3"' } as never);
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Results');
    await userEvent.click(await screen.findByRole('button', { name: /result-one: coverage partial/ }));
    await screen.findByText(/Source-only evidence is not deployment verification/);
    await openSetup();
    await userEvent.type(screen.getByLabelText('Reason for explicit choices'), 'Preserve this draft');
    const save = screen.getByRole('button', { name: 'Save review draft' });
    const setup = screen.getByRole('button', { name: 'Set up review binding' });
    if (!hasBinding) expect(setup).toBeEnabled();
    expect(save).toBeEnabled();
    await navigate('Runs & schedules');
    const start = screen.getByRole('button', { name: 'Start source-only review' });
    const check = screen.getByRole('button', { name: 'Check review readiness' });
    const refresh = screen.getByRole('button', { name: 'Refresh Well-Architected access' });
    if (hasBinding) {
      await userEvent.selectOptions(screen.getByLabelText('Ready binding'), 'binding-one');
      await userEvent.click(check);
      await waitFor(() => expect(start).toBeEnabled());
    }
    vi.mocked(failedRead === 'options' ? getWellArchitectedOptions : getWellArchitectedConfiguration).mockRejectedValueOnce({ status: 503 });
    await userEvent.click(refresh);
    expect(await screen.findByRole('alert')).toHaveTextContent('Refresh successfully before saving or starting work');
    expect(start).toBeDisabled();
    await openSetup();
    expect(save).toBeDisabled(); expect(setup).toBeDisabled();
    expect(screen.getByLabelText('Reason for explicit choices')).toHaveValue('Preserve this draft');
    expect(screen.getByLabelText('Reason for explicit choices')).toBeDisabled();
    await navigate('Results');
    expect(screen.getByText(/Source-only evidence is not deployment verification/)).toBeVisible();
    // A fresh readiness result must not unlock actions against failed configuration readback.
    await navigate('Runs & schedules');
    await userEvent.click(check);
    await screen.findByText('Readiness: ready; gaps: none');
    expect(start).toBeDisabled(); await userEvent.click(start);
    await openSetup();
    expect(save).toBeDisabled(); expect(setup).toBeDisabled();
    await userEvent.click(save); await userEvent.click(setup);
    expect(saveWellArchitectedConfiguration).not.toHaveBeenCalled();
    expect(startWellArchitectedBinding).not.toHaveBeenCalled();
    expect(submitWellArchitectedRun).not.toHaveBeenCalled();
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue({ ...current, value: { ...current.value, revision: 2 } } as never);
    await userEvent.click(refresh);
    await screen.findByText(/Latest saved configuration loaded/);
    await waitFor(() => expect(save).toBeEnabled());
    expect(screen.getByLabelText('Reason for explicit choices')).toHaveValue('Preserve this draft');
    expect(screen.getByLabelText('Reason for explicit choices')).toBeEnabled();
    if (hasBinding) {
      await navigate('Runs & schedules');
      await waitFor(() => expect(check).toBeEnabled());
      await userEvent.click(check);
      await waitFor(() => expect(start).toBeEnabled());
    } else await waitFor(() => expect(setup).toBeEnabled());
  });

  it('starts with a ready binding and reads scoped run and source-only result revisions', async () => {
    const scope = configuration.value.scope;
    const binding = { binding_id: 'binding-one', scope, state: 'ready', configuration_id: 'config-one' };
    const reviewRun = { scope, run_id: 'run-one', revision: 2, state: 'queued', provider_state: 'unknown' };
    const reviewResult = { scope, result_id: 'result-one', run_id: 'run-one', revision: 3, service_id: 'well_architected', coverage: 'partial', coverage_mode: 'selected_snapshot' };
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue({ ...configuration, value: { ...configuration.value,
      settings: { ...configuration.value.settings, enabled: true } } } as never);
    vi.mocked(checkWellArchitectedConfiguration).mockResolvedValue({ ready: true, gaps: [] });
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [binding] } as never);
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [reviewRun] } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [reviewResult] } as never);
    vi.mocked(getWellArchitectedResult).mockResolvedValue({ value: reviewResult, etag: '"3"' } as never);
    vi.mocked(submitWellArchitectedRun).mockResolvedValue({ value: reviewRun, etag: '"2"' } as never);
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await screen.findByRole('button', { name: /run-one: queued/ });
    await userEvent.selectOptions(screen.getByLabelText('Ready binding'), 'binding-one');
    expect(screen.getByRole('button', { name: 'Start source-only review' })).toBeDisabled();
    await navigate('Runs & schedules');
    await userEvent.click(screen.getByRole('button', { name: 'Check review readiness' }));
    await screen.findByText('Readiness: ready; gaps: none');
    await userEvent.click(screen.getByRole('button', { name: 'Start source-only review' }));
    await waitFor(() => expect(submitWellArchitectedRun).toHaveBeenCalledWith('product-one', 'project-one', 'binding-one', 1, [source]));
    await navigate('Results');
    await userEvent.click(screen.getByRole('button', { name: /result-one: coverage partial/ }));
    expect(await screen.findByText(/Source-only evidence is not deployment verification/)).toBeInTheDocument();
  });
  it('shows local uncertainty, stale monitoring, source comparison and destination publication debt', async () => {
    const scope = configuration.value.scope;
    const reviewRun = { scope, run_id: 'run-one', revision: 4, state: 'expired', provider_state: 'unknown',
      monitoring: { state: 'degraded', last_successful_poll_at: '2026-10-28T12:00:00Z' } };
    const reviewResult = { scope, service_id: 'well_architected', kind: 'well_architected_result',
      run_id: 'run-one', result_id: 'result-one', revision: 3, review_mode: 'source_only',
      pillars: ['RESILIENCE'], coverage: [{ path: 'src/example.ts' }],
      limitations: ['source_only: deployed state was not inspected'],
      recommendations: [{ native_id: 'recommendation-one', generation_id: 'generation-one', pillar: 'RESILIENCE',
        title: 'Document recovery procedures', description: 'Describe how the service recovers from a dependency failure.',
        comparison: { status: 'not_verifiable', explanation: 'source unavailable' } }] };
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [reviewRun] } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [reviewResult] } as never);
    vi.mocked(getWellArchitectedRun).mockResolvedValue({ value: reviewRun, etag: '"4"' } as never);
    vi.mocked(getWellArchitectedResult).mockResolvedValue({ value: reviewResult, etag: '"3"' } as never);
    vi.mocked(getWellArchitectedPublicationStatus).mockResolvedValue({ result_id: 'result-one', revision: 3,
      consumers: [{ destination: 'product-context', state: 'unavailable', last_error: 'consumer_schema_unadmitted', projection_verified_on_ack: false },
        { destination: 'engineering-insights', state: 'published', receipt_id: 'ack-one', projection_verified_on_ack: true }] });
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await userEvent.click(await screen.findByRole('button', { name: /run-one: expired/ }));
    expect(await screen.findByText(/Local state does not confirm AWS cancellation/)).toBeInTheDocument();
    expect(screen.getByText(/Monitoring is stale or degraded; last successful check: 2026-10-28/)).toBeInTheDocument();
    await navigate('Results');
    await userEvent.click(screen.getByRole('button', { name: /result-one: coverage 1 selected files/ }));
    expect(await screen.findByText(/Result revision 3; coverage: 1 selected source files; mode: source_only/)).toBeInTheDocument();
    expect(screen.getByText(/Independent source comparison: not_verifiable/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Document recovery procedures' })).toBeInTheDocument();
    expect(screen.getByText('Describe how the service recovers from a dependency failure.')).toBeInTheDocument();
    expect(screen.getByText(/Pillar: RESILIENCE/)).toBeInTheDocument();
    expect(screen.getByText(/Native recommendation: recommendation-one; generation: generation-one/)).toBeInTheDocument();
    expect(screen.getByText(/Source comparison: not_verifiable; source unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/Publication debt: product-context: unavailable \(consumer_schema_unadmitted\)/)).toBeInTheDocument();
    expect(screen.getByText(/Publication receipts: engineering-insights: ack-one/)).toBeInTheDocument();
    expect(screen.getByText(/Source-only evidence is not deployment verification/)).toBeInTheDocument();
  });
  it('renders provider recommendation content as text rather than executable markup', async () => {
    const reviewResult = { scope: configuration.value.scope, result_id: 'result-one', run_id: 'run-one', revision: 3,
      service_id: 'well_architected', coverage: [], recommendations: [{ native_id: 'recommendation-one',
        title: '<script>alert("provider")</script>', description: '<img src=x onerror="alert(1)">' }] };
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [{ scope: configuration.value.scope, run_id: 'run-one' }] } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [reviewResult] } as never);
    vi.mocked(getWellArchitectedResult).mockResolvedValue({ value: reviewResult, etag: '"3"' } as never);
    const { container } = render(<WellArchitectedServiceTab {...props} />);
    await navigate('Results');
    await userEvent.click(await screen.findByRole('button', { name: /result-one: coverage 0 selected files/ }));
    expect(await screen.findByRole('heading', { name: reviewResult.recommendations[0].title })).toBeInTheDocument();
    expect(screen.getByText(reviewResult.recommendations[0].description)).toBeInTheDocument();
    expect(container.querySelector('script, img')).toBeNull();
    expect(screen.getByText(/Source-only evidence is not deployment verification/)).toBeInTheDocument();
  });
  it('reads authoritative uncertainty and not-comparable states from the shared scoped fixture', async () => {
    const fixture = (await import('@/__tests__/fixtures/well-architected-client-parity.json')).default;
    const run = { ...fixture.run, ...fixture.states.run };
    const result = { ...fixture.result, ...fixture.states.result };
    const baseline = { ...result, result_id: fixture.states.comparison.baseline_result_id, revision: 2 };
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [run] } as never);
    vi.mocked(getWellArchitectedRun).mockResolvedValue({ value: run, etag: '"2"' } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [baseline, result] } as never);
    vi.mocked(getWellArchitectedResult).mockResolvedValue({ value: result, etag: '"3"' } as never);
    vi.mocked(getWellArchitectedPublicationStatus).mockResolvedValue(fixture.states.publication);
    vi.mocked(compareWellArchitectedResults).mockResolvedValue(fixture.states.comparison as never);
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await userEvent.click(await screen.findByRole('button', { name: /run-one: expired/ }));
    expect(await screen.findByText(/Local state does not confirm AWS cancellation/)).toBeInTheDocument();
    expect(screen.getByText(/Monitoring is stale or degraded; last successful check: 2026-10-28/)).toBeInTheDocument();
    expect(screen.getByText(/Last provider check error: cancel_unsupported/)).toBeInTheDocument();
    await navigate('Results');
    await userEvent.click(screen.getByRole('button', { name: /result-one: coverage 1 selected files/ }));
    expect(await screen.findByText(/Independent source comparison: not_verifiable/)).toBeInTheDocument();
    expect(screen.getByText(/Publication debt: product-context: unavailable/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Baseline review result'), baseline.result_id);
    await userEvent.click(screen.getByRole('button', { name: 'Compare review results' }));
    expect(await screen.findByText(/Result comparison: not_comparable; reasons: source_selection_changed/)).toBeInTheDocument();
    expect(compareWellArchitectedResults).toHaveBeenCalledWith('product-one', 'project-one', baseline.result_id, result.result_id);
    expect(screen.getByText(/Source-only evidence is not deployment verification/)).toBeInTheDocument();
  });
  it('does not treat local cancellation or supported source findings as provider or deployment verification', async () => {
    const fixture = (await import('@/__tests__/fixtures/well-architected-client-parity.json')).default;
    const run = { ...fixture.run, ...fixture.states.run, state: 'cancelled', provider_state: 'running' };
    const result = { ...fixture.result, ...fixture.states.result, recommendations: [{ comparison: { status: 'supported' } }] };
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [run] } as never);
    vi.mocked(getWellArchitectedRun).mockResolvedValue({ value: run, etag: '"2"' } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [result] } as never);
    vi.mocked(getWellArchitectedResult).mockResolvedValue({ value: result, etag: '"3"' } as never);
    vi.mocked(getWellArchitectedPublicationStatus).mockResolvedValue(fixture.states.publication);
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Runs & schedules');
    await userEvent.click(await screen.findByRole('button', { name: /run-one: cancelled/ }));
    expect(await screen.findByText(/Run revision 2: cancelled; provider: running/)).toBeInTheDocument();
    expect(screen.getByText(/Local state does not confirm AWS cancellation/)).toBeInTheDocument();
    expect(screen.getByText(/Last provider check error: cancel_unsupported; cancellation is not confirmed/)).toBeInTheDocument();
    expect(screen.queryByText(/provider: cancelled/)).not.toBeInTheDocument();
    await navigate('Results');
    await userEvent.click(screen.getByRole('button', { name: /result-one: coverage 1 selected files/ }));
    expect(await screen.findByText(/Independent source comparison: supported/)).toBeInTheDocument();
    expect(screen.getByText(/Source-only evidence is not deployment verification/)).toBeInTheDocument();
    expect(screen.getByText(/Publication debt: product-context: unavailable/)).toBeInTheDocument();
    expect(screen.queryByText(/deployment verified/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Publication receipts: product-context/)).not.toBeInTheDocument();
  });
  it('does not display publication receipts from another result revision', async () => {
    const reviewResult = { scope: configuration.value.scope, service_id: 'well_architected',
      run_id: 'run-one', result_id: 'result-one', revision: 3, review_mode: 'source_only', coverage: [] };
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [{ scope: configuration.value.scope, run_id: 'run-one' }] } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [reviewResult] } as never);
    vi.mocked(getWellArchitectedResult).mockResolvedValue({ value: reviewResult, etag: '"3"' } as never);
    vi.mocked(getWellArchitectedPublicationStatus).mockResolvedValue({ result_id: 'result-one', revision: 2,
      consumers: [{ destination: 'product-context', state: 'published', receipt_id: 'stale-receipt', projection_verified_on_ack: true }] });
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Results');
    await userEvent.click(await screen.findByRole('button', { name: /result-one: coverage 0 selected files/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details were removed');
    expect(screen.queryByText(/stale-receipt/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Publication receipts/)).not.toBeInTheDocument();
  });
});

describe('Well-Architected result history refresh', () => {
  it('clears prior results when an authorized refresh returns no runs', async () => {
    vi.resetAllMocks();
    vi.mocked(getWellArchitectedOptions).mockResolvedValue(options);
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(configuration as never);
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [] });
    vi.mocked(listWellArchitectedRuns).mockResolvedValueOnce({ items: [{ scope: configuration.value.scope, run_id: 'run-one' }] } as never)
      .mockResolvedValue({ items: [] });
    const result = { service_id: 'well_architected', result_id: 'result-one', run_id: 'run-one', revision: 3,
      scope: configuration.value.scope, coverage: 'partial', coverage_mode: 'selected_snapshot', limitations: ['source_only'] };
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [result] } as never);
    vi.mocked(getWellArchitectedResult).mockResolvedValue({ value: result, etag: '"3"' } as never);
    vi.mocked(getWellArchitectedPublicationStatus).mockResolvedValue({ result_id: 'result-one', revision: 3, consumers: [] });
    render(<WellArchitectedServiceTab {...props} />);
    await navigate('Results');
    await userEvent.click(await screen.findByRole('button', { name: /result-one: coverage partial/ }));
    expect(listWellArchitectedResults).toHaveBeenCalledWith('product-one', 'project-one');
    expect(await screen.findByText(/Result revision 3; coverage: partial/)).toBeInTheDocument();
    expect(screen.getByText(/Source-only evidence is not deployment verification/)).toBeInTheDocument();
    await navigate('Runs & schedules');
    await userEvent.click(screen.getByRole('button', { name: 'Refresh review status' }));
    await navigate('Setup');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Set up review binding' })).toBeEnabled());
    expect(listWellArchitectedResults).toHaveBeenCalledTimes(1);
    await navigate('Results');
    expect(screen.queryByRole('button', { name: /result-one: coverage partial/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Result revision 3/)).not.toBeInTheDocument();
    await openStatus();
    expect(screen.getByText(/Revision 1: needs_input/)).toBeVisible();
  });
});

describe('Well-Architected foreign-project result denial', () => {
  it('drops the displayed Product review when its result page contains a foreign project', async () => {
    const fixture = (await import('@/__tests__/fixtures/well-architected-client-parity.json')).default;
    vi.resetAllMocks();
    vi.mocked(getWellArchitectedOptions).mockResolvedValue(options);
    vi.mocked(getWellArchitectedConfiguration).mockResolvedValue(configuration as never);
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [] });
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [fixture.run] } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [{ ...fixture.result,
      scope: { ...fixture.result.scope, project_id: fixture.foreign_project_id } }] } as never);
    render(<WellArchitectedServiceTab {...props} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details were removed');
    expect(screen.queryByText(/Revision 1: needs_input/)).not.toBeInTheDocument();
    await navigate('Results');
    expect(screen.queryByRole('button', { name: /result-one/ })).not.toBeInTheDocument();
    expect(submitWellArchitectedRun).not.toHaveBeenCalled();
  });
});
