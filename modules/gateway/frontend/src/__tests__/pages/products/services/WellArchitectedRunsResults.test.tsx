import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import WellArchitectedRunsResults from '@/pages/products/services/WellArchitectedRunsResults';
import {
  getWellArchitectedRun, getWellArchitectedResult, getWellArchitectedPublicationStatus, compareWellArchitectedResults,
  checkWellArchitectedConfiguration, listWellArchitectedBindings, listWellArchitectedResults, listWellArchitectedRuns,
  submitWellArchitectedRun, type SecurityBinding, type Versioned, type WellArchitectedConfiguration,
} from '@/services/l5Services';

vi.mock('@/services/l5Services', async original => ({ ...await original<typeof import('@/services/l5Services')>(),
  checkWellArchitectedConfiguration: vi.fn(), listWellArchitectedBindings: vi.fn(), listWellArchitectedResults: vi.fn(),
  listWellArchitectedRuns: vi.fn(), submitWellArchitectedRun: vi.fn(),
  getWellArchitectedRun: vi.fn(), getWellArchitectedResult: vi.fn(), getWellArchitectedPublicationStatus: vi.fn(), compareWellArchitectedResults: vi.fn(),
}));

const scope = { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '1' };
const connection = { tenant_id: scope.tenant_id, project_id: scope.project_id, source_connection_id: 'connection-one', record_type: 'connection', record_id: 'connection-one', record_revision: '1' };
const source = { ...connection, record_type: 'document', record_id: 'design-one' };
const configuration = { value: { scope, configuration_id: 'config-one', revision: 1, state: 'needs_input',
  settings: { service_id: 'well_architected', enabled: true, source_refs: [source], capabilities: ['architecture_review'] },
} } as Versioned<WellArchitectedConfiguration>;
const binding: SecurityBinding = { scope, binding_id: 'binding-one', configuration_id: 'config-one', revision: 1, generation: 1,
  state: 'ready', ownership: 'adp_created', connection_ref: connection, readiness: [], gaps: [] };
const props = { productId: scope.product_id, projectId: scope.project_id, tenantId: scope.tenant_id,
  configuration, eligible: true, onAccessDenied: vi.fn(), setupContent: null, capabilitiesContent: null };

async function selectBinding(id = 'binding-one') {
  await waitFor(() => expect(screen.getByLabelText('Ready binding')).toBeEnabled());
  await userEvent.selectOptions(screen.getByLabelText('Ready binding'), id);
}

async function checkReadiness() {
  await userEvent.click(screen.getByRole('button', { name: 'Check review readiness' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Check review readiness' })).toBeEnabled());
}

describe('Well-Architected current readiness', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [binding] });
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [] });
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [] });
    vi.mocked(checkWellArchitectedConfiguration).mockResolvedValue({ ready: true, gaps: [] });
    vi.mocked(submitWellArchitectedRun).mockResolvedValue({ value: { scope, run_id: 'run-one', revision: 1, state: 'queued' }, etag: '"run-one"' } as never);
  });

  it('admits the first review after a current check even when the stored configuration still says needs_input', async () => {
    render(<WellArchitectedRunsResults {...props} />);
    await selectBinding();
    expect(screen.getByRole('button', { name: 'Start source-only review' })).toBeDisabled();
    await checkReadiness();
    await userEvent.click(screen.getByRole('button', { name: 'Start source-only review' }));
    await waitFor(() => expect(submitWellArchitectedRun).toHaveBeenCalledWith('product-one', 'project-one', 'binding-one', 1, [source]));
    expect(screen.getByRole('button', { name: 'Start source-only review' })).toBeDisabled();
  });

  it.each([
    ['disabled configuration', false, { ready: true, gaps: [] }],
    ['failed readiness', true, { ready: false, gaps: ['execution_delegation'] }],
    ['inconsistent readiness', true, { ready: true, gaps: ['execution_delegation'] }],
  ] as const)('refuses to submit with %s', async (_reason, enabled, readiness) => {
    vi.mocked(checkWellArchitectedConfiguration).mockResolvedValue({ ready: readiness.ready, gaps: [...readiness.gaps] });
    render(<WellArchitectedRunsResults {...props} configuration={{ ...configuration,
      value: { ...configuration.value, settings: { ...configuration.value.settings, enabled } } }} />);
    await selectBinding();
    await checkReadiness();
    expect(screen.getByRole('button', { name: 'Start source-only review' })).toBeDisabled();
    expect(submitWellArchitectedRun).not.toHaveBeenCalled();
  });

  it('requires a new check after changing the selected binding or refreshing its readback', async () => {
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [binding, { ...binding, binding_id: 'binding-two' }] });
    render(<WellArchitectedRunsResults {...props} />);
    await selectBinding();
    await checkReadiness();
    expect(screen.getByRole('button', { name: 'Start source-only review' })).toBeEnabled();
    await selectBinding('binding-two');
    expect(screen.getByRole('button', { name: 'Start source-only review' })).toBeDisabled();
    expect(screen.queryByText('Readiness: ready; gaps: none')).not.toBeInTheDocument();
    await checkReadiness();
    expect(screen.getByRole('button', { name: 'Start source-only review' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Refresh review status' }));
    await waitFor(() => expect(screen.getByLabelText('Ready binding')).toBeEnabled());
    expect(screen.getByRole('button', { name: 'Start source-only review' })).toBeDisabled();
    expect(screen.queryByText('Readiness: ready; gaps: none')).not.toBeInTheDocument();
  });

  it('ignores a late readiness response for an earlier configuration revision', async () => {
    let resolveCheck!: (value: { ready: boolean; gaps: string[] }) => void;
    vi.mocked(checkWellArchitectedConfiguration).mockImplementationOnce(() => new Promise(resolve => { resolveCheck = resolve; }));
    const { rerender } = render(<WellArchitectedRunsResults {...props} />);
    await selectBinding();
    await userEvent.click(screen.getByRole('button', { name: 'Check review readiness' }));
    rerender(<WellArchitectedRunsResults {...props} configuration={{ ...configuration, value: { ...configuration.value, revision: 2 } }} />);
    await waitFor(() => expect(screen.getByLabelText('Ready binding')).toBeEnabled());
    await act(async () => { resolveCheck({ ready: true, gaps: [] }); });
    expect(screen.queryByText('Readiness: ready; gaps: none')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start source-only review' })).toBeDisabled();
    await checkReadiness();
    await userEvent.click(screen.getByRole('button', { name: 'Start source-only review' }));
    await waitFor(() => expect(submitWellArchitectedRun).toHaveBeenCalledWith('product-one', 'project-one', 'binding-one', 2, [source]));
  });

  it('does not reuse a passed check when a refresh reports the binding is blocked', async () => {
    render(<WellArchitectedRunsResults {...props} />);
    await selectBinding();
    await checkReadiness();
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [{ ...binding, state: 'blocked', gaps: ['provider_unqualified'] }] });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh review status' }));
    await checkReadiness();
    expect(screen.queryByRole('option', { name: 'binding-one' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start source-only review' })).toBeDisabled();
    expect(submitWellArchitectedRun).not.toHaveBeenCalled();
  });
});


const reviewRun = { scope, run_id: 'run-one', revision: 1, state: 'completed', provider_state: 'completed' };
const reviewResult = { scope, result_id: 'result-one', run_id: 'run-one', revision: 1, service_id: 'well_architected',
  coverage: 'partial', recommendations: [{ title: 'Retained recommendation', description: 'Review the design.' }] };

describe('Well-Architected retained history', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [binding] });
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [reviewRun] } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [reviewResult] } as never);
    vi.mocked(getWellArchitectedRun).mockResolvedValue({ value: reviewRun } as never);
    vi.mocked(getWellArchitectedResult).mockResolvedValue({ value: reviewResult } as never);
    vi.mocked(getWellArchitectedPublicationStatus).mockResolvedValue({ result_id: 'result-one', revision: 1, consumers: [] });
  });

  it('appends history without duplicates, preserves the inspected review and compares a baseline from a later page', async () => {
    vi.mocked(listWellArchitectedRuns).mockResolvedValueOnce({ items: [reviewRun], next_cursor: 'run-page' } as never)
      .mockResolvedValueOnce({ items: [reviewRun, { ...reviewRun, run_id: 'run-two' }] } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValueOnce({ items: [reviewResult], next_cursor: 'result-page' } as never)
      .mockResolvedValueOnce({ items: [reviewResult, { ...reviewResult, result_id: 'baseline-two' }] } as never);
    vi.mocked(getWellArchitectedRun).mockResolvedValueOnce({ value: { ...reviewRun, revision: 3, state: 'succeeded' } } as never);
    vi.mocked(compareWellArchitectedResults).mockResolvedValue({ baseline_result_id: 'baseline-two', candidate_result_id: 'result-one', comparability: 'comparable', reasons: [] });
    render(<WellArchitectedRunsResults {...props} />);
    await userEvent.click(await screen.findByRole('button', { name: /run-one:/ }));
    await screen.findByText(/Run revision 3/);
    await userEvent.click(screen.getByRole('button', { name: /result-one: coverage/ }));
    await screen.findByText('Retained recommendation');
    await userEvent.click(screen.getByRole('button', { name: 'Load more review runs' }));
    await userEvent.click(screen.getByRole('button', { name: 'Load more review results' }));
    expect(await screen.findByRole('button', { name: /run-two:/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /run-one: succeeded/ })).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /run-one:/ })).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: /result-one: coverage/ })).toHaveLength(1);
    expect(screen.getByText('Retained recommendation')).toBeInTheDocument();
    expect(listWellArchitectedRuns).toHaveBeenLastCalledWith('product-one', 'project-one', 'run-page');
    expect(listWellArchitectedResults).toHaveBeenLastCalledWith('product-one', 'project-one', 'result-page');
    await userEvent.selectOptions(screen.getByLabelText('Baseline review result'), 'baseline-two');
    await userEvent.click(screen.getByRole('button', { name: 'Compare review results' }));
    await screen.findByText(/Result comparison: comparable/);
    expect(compareWellArchitectedResults).toHaveBeenCalledWith('product-one', 'project-one', 'baseline-two', 'result-one');
    expect(screen.queryByRole('button', { name: /Load more review/ })).not.toBeInTheDocument();
  });

  it.each([{ status: 503 }, new Error('offline')])('retains recommendations when publication readback fails with %s', async failure => {
    vi.mocked(getWellArchitectedPublicationStatus).mockRejectedValueOnce(failure);
    render(<WellArchitectedRunsResults {...props} />);
    await userEvent.click(await screen.findByRole('button', { name: /result-one: coverage/ }));
    expect(await screen.findByText('Retained recommendation')).toBeInTheDocument();
    expect(await screen.findByRole('alert')).toHaveTextContent('Reopen this result to retry publication readback');
    expect(screen.getByLabelText('Baseline review result')).toBeEnabled();
    expect(props.onAccessDenied).not.toHaveBeenCalled();
  });

  it.each([403, 404])('clears a retained review when publication authority is revoked (%s)', async status => {
    vi.mocked(getWellArchitectedPublicationStatus).mockRejectedValueOnce({ status });
    render(<WellArchitectedRunsResults {...props} />);
    await userEvent.click(await screen.findByRole('button', { name: /result-one: coverage/ }));
    await waitFor(() => expect(props.onAccessDenied).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('Retained recommendation')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /result-one: coverage/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh review status' })).toBeEnabled();
  });

  it.each(['runs', 'results'] as const)('clears current data and cursors when a %s page is revoked', async kind => {
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [reviewRun], next_cursor: 'run-page' } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [reviewResult], next_cursor: 'result-page' } as never);
    render(<WellArchitectedRunsResults {...props} />);
    await screen.findByRole('button', { name: 'Load more review results' });
    const list = kind === 'runs' ? listWellArchitectedRuns : listWellArchitectedResults;
    vi.mocked(list).mockRejectedValueOnce({ status: 404 });
    await userEvent.click(screen.getByRole('button', { name: `Load more review ${kind}` }));
    await waitFor(() => expect(props.onAccessDenied).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('button', { name: /run-one:|result-one:|Load more review/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh review status' })).toBeEnabled();
  });

  it.each(['runs', 'results'] as const)('rejects foreign scope on a %s page', async kind => {
    vi.mocked(listWellArchitectedRuns).mockResolvedValueOnce({ items: [reviewRun], next_cursor: 'next' } as never)
      .mockResolvedValueOnce({ items: [{ ...reviewRun, scope: { ...scope, tenant_id: 'other' } }] } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValueOnce({ items: [reviewResult], next_cursor: 'next' } as never)
      .mockResolvedValueOnce({ items: [{ ...reviewResult, scope: { ...scope, relationship_revision: 'old' } }] } as never);
    render(<WellArchitectedRunsResults {...props} />);
    await userEvent.click(await screen.findByRole('button', { name: `Load more review ${kind}` }));
    await waitFor(() => expect(props.onAccessDenied).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('button', { name: /Load more review/ })).not.toBeInTheDocument();
  });

  it.each([
    ['runs', 'success'], ['runs', 'denied'], ['results', 'success'], ['results', 'denied'],
  ] as const)('ignores a late %s page %s after the project changes', async (kind, outcome) => {
    let finish!: (value: never) => void;
    let reject!: (reason: unknown) => void;
    const list = kind === 'runs' ? listWellArchitectedRuns : listWellArchitectedResults;
    vi.mocked(list).mockResolvedValueOnce({ items: [kind === 'runs' ? reviewRun : reviewResult], next_cursor: 'old-page' } as never)
      .mockImplementationOnce(() => new Promise((resolve, fail) => { finish = resolve; reject = fail; }));
    const { rerender } = render(<WellArchitectedRunsResults {...props} />);
    await userEvent.click(await screen.findByRole('button', { name: `Load more review ${kind}` }));
    const nextScope = { ...scope, project_id: 'project-two' };
    vi.mocked(listWellArchitectedBindings).mockResolvedValue({ items: [] });
    vi.mocked(listWellArchitectedRuns).mockResolvedValue({ items: [{ ...reviewRun, scope: nextScope, run_id: 'current-run' }] } as never);
    vi.mocked(listWellArchitectedResults).mockResolvedValue({ items: [{ ...reviewResult, scope: nextScope, result_id: 'current-result' }] } as never);
    rerender(<WellArchitectedRunsResults {...props} projectId="project-two" configuration={{ ...configuration, value: { ...configuration.value, scope: nextScope } }} />);
    await screen.findByRole('button', { name: /current-result: coverage/ });
    await act(async () => {
      if (outcome === 'denied') reject({ status: 403 });
      else finish({ items: [kind === 'runs' ? { ...reviewRun, run_id: 'stale-run' } : { ...reviewResult, result_id: 'stale-result' }], next_cursor: 'stale-next' } as never);
    });
    expect(props.onAccessDenied).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /current-result: coverage/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /stale-run:|stale-result:|Load more review/ })).not.toBeInTheDocument();
  });

  it.each([
    ['result', 'success'], ['result', 'denied'], ['publication', 'success'], ['publication', 'denied'],
  ] as const)('ignores a late %s inspection %s after refresh for a new configuration', async (stage, outcome) => {
    let finish!: (value: never) => void;
    let reject!: (reason: unknown) => void;
    const read = stage === 'result' ? getWellArchitectedResult : getWellArchitectedPublicationStatus;
    vi.mocked(read).mockImplementationOnce(() => new Promise((resolve, fail) => { finish = resolve; reject = fail; }));
    const { rerender } = render(<WellArchitectedRunsResults {...props} />);
    await userEvent.click(await screen.findByRole('button', { name: /result-one: coverage/ }));
    await waitFor(() => expect(read).toHaveBeenCalled());
    rerender(<WellArchitectedRunsResults {...props} configuration={{ ...configuration, value: { ...configuration.value, revision: 2 } }} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh review status' })).toBeEnabled());
    await act(async () => {
      if (outcome === 'denied') reject({ status: 404 });
      else finish((stage === 'result' ? { value: reviewResult } : { result_id: 'result-one', revision: 1, consumers: [] }) as never);
    });
    expect(props.onAccessDenied).not.toHaveBeenCalled();
    expect(screen.queryByText('Retained recommendation')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Baseline review result')).not.toBeInTheDocument();
  });
});
