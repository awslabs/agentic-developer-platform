import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FinOpsServiceTab from '@/pages/products/services/FinOpsServiceTab';
import ProductServices from '@/pages/products/services/ProductServices';
import { checkFinOpsConfiguration } from '@/services/finopsReadiness';
import {
  getFinOpsConfiguration, getFinOpsOptions, getFinOpsPublication, getFinOpsResult, getFinOpsNativeAnswer,
  getFinOpsRun, listFinOpsBindings, listFinOpsResults, listFinOpsRuns, saveFinOpsConfiguration,
  startFinOpsBinding, submitFinOpsRun,
} from '@/services/l5Services';

vi.mock('@/services/finopsReadiness', async original => ({ ...await original<typeof import('@/services/finopsReadiness')>(), checkFinOpsConfiguration: vi.fn() }));

vi.mock('@/services/l5Services', () => ({
  getFinOpsBindingOperation: vi.fn(), getFinOpsConfiguration: vi.fn(), getFinOpsOptions: vi.fn(),
  getFinOpsPublication: vi.fn(), getFinOpsResult: vi.fn(), getFinOpsNativeAnswer: vi.fn(), getFinOpsRun: vi.fn(),
  listFinOpsBindings: vi.fn(), listFinOpsResults: vi.fn(), listFinOpsRuns: vi.fn(),
  saveFinOpsConfiguration: vi.fn(), startFinOpsBinding: vi.fn(), submitFinOpsRun: vi.fn(),
  matchesSecurityScope: (scope: { tenant_id: string; product_id: string; project_id: string; relationship_revision: string }, expected: typeof scope, product: string, project: string) =>
    Boolean(scope && expected && scope.tenant_id === expected.tenant_id && scope.product_id === product
      && scope.project_id === project && scope.relationship_revision === expected.relationship_revision),
}));

const scope = { tenant_id: 'tenant-example', product_id: 'product-example', project_id: 'project-example', relationship_revision: '1' };
const connection = { tenant_id: scope.tenant_id, project_id: scope.project_id, source_connection_id: 'connection-example', record_type: 'connection', record_id: 'connection-example', record_revision: '1' };
const billing = { account_ids: ['123456789012'], view_ref: 'view-example' };
const config = { scope, configuration_id: 'configuration-example', revision: 1, state: 'ready', gaps: [],
  handoff_state: 'linked', inherited_fields: ['purpose', 'source_refs'], settings: {
    service_id: 'finops', enabled: true, connection_ref: connection,
    finops: { agent_mode: 'create', native_agent_ref: 'agent-example', billing_scope: billing },
    purpose: 'Inherited purpose', source_refs: [], profile_ref: { record_id: 'product-example' },
  } };
const binding = { scope, binding_id: 'binding-example', state: 'ready', gaps: [], configuration_id: config.configuration_id,
  native_configuration_revision: 1, connection_ref: connection, native_agent_display_name: 'Example agent', billing_scope: billing,
  native_resource_ref: { ...connection, record_type: 'finops_agent', record_id: 'agent-example' } };
const run = { scope, run_id: 'run-example', state: 'admitted', revision: 1 };
const artifact = { artifact_id: 'evidence-example', version: '1', sha256: 'a'.repeat(64) };
const query = { start: '2026-01-01', end: '2026-02-01', metric: 'UnblendedCost', granularity: 'MONTHLY', group_by: ['SERVICE'], currency: 'USD' };
const result = { scope, result_id: 'result-example', run_id: 'run-example', revision: 1,
  kind: 'finops_cost_inquiry', query, scope_digest: 'b'.repeat(64), binding_generation: 1,
  attribution: 'unknown', attribution_evidence: [artifact],
  native: { status: 'outcome_unknown', conversation_id: null, turn_id: null, answer_artifact: null,
    observed_at: null, measure: { amount: null, currency: null, estimated: null, source_refreshed_at: null,
      freshness_reason: 'no_native_answer', coverage: 'partial' } },
  oracle: { collection_id: 'collection-example', query_hash: 'c'.repeat(64), observed_at: '2026-02-02T00:00:00Z',
    measure: { amount: '-12.3400', currency: 'USD', estimated: false, source_refreshed_at: null,
      freshness_reason: null, coverage: 'partial' }, pages: [artifact] },
  comparison: { state: 'incomparable', delta: null, display_quantum: null, reason: 'incomplete_or_unknown_source' },
  billing_scope: { account_ids: ['123456789012'], view_ref: 'view-example', snapshot_artifact: artifact } };
const renderTab = () => render(<FinOpsServiceTab productId="product-example" projectId="project-example" tenantId="tenant-example" />);
const visitSection = async (name: 'Setup' | 'Capabilities' | 'Runs & schedules' | 'Results') => {
  const link = within(screen.getByRole('navigation', { name: 'FinOps service sections' })).getByRole('link', { name, exact: true });
  await userEvent.click(link);
  expect(link).toHaveAttribute('aria-current', 'location');
  expect(screen.getByRole('region', { name, exact: true })).toBeVisible();
};
const openSetup = async (title: 'Edit FinOps setup' | 'Provider connection' | 'Owner consent') => {
  await visitSection('Setup');
  const summary = await screen.findByText(title, { selector: 'summary' });
  if (!summary.closest('details')?.open) await userEvent.click(summary);
  expect(summary.closest('details')).toHaveAttribute('open');
};

describe('FinOps Product service', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(checkFinOpsConfiguration).mockResolvedValue({ ready: true, gaps: [] });
    vi.mocked(getFinOpsOptions).mockResolvedValue({ capabilities: ['cost_inquiry'], unavailable_reasons: [], admissions_enabled: true });
    vi.mocked(getFinOpsConfiguration).mockResolvedValue({ value: config, etag: '"config-etag"' } as never);
    vi.mocked(listFinOpsBindings).mockResolvedValue({ items: [binding] } as never);
    vi.mocked(listFinOpsRuns).mockResolvedValue({ items: [run] } as never);
    vi.mocked(listFinOpsResults).mockResolvedValue({ items: [result] } as never);
    vi.mocked(startFinOpsBinding).mockResolvedValue({ value: { operation_id: 'operation-example', state: 'blocked', gaps: ['native_unqualified'] }, etag: '"setup-etag"' } as never);
    vi.mocked(submitFinOpsRun).mockResolvedValue({ value: run, etag: '"run-etag"' } as never);
    vi.mocked(getFinOpsRun).mockResolvedValue({ value: run, etag: '"run-etag"' } as never);
    vi.mocked(getFinOpsResult).mockResolvedValue({ value: result, etag: '"result-etag"' } as never);
    vi.mocked(getFinOpsNativeAnswer).mockResolvedValue({ result_id: result.result_id, run_id: result.run_id,
      revision: result.revision, artifact, text: 'Original example native answer' });
  });

  it('saves with the opaque ETag, disables unsupported creation and keeps 202 inquiry separate from the result', async () => {
    const user = userEvent.setup();
    vi.mocked(saveFinOpsConfiguration).mockResolvedValue({ value: config, etag: '"saved-etag"' } as never);
    renderTab();
    await openSetup('Provider connection');
    expect(await screen.findByText(/Configuration ready; revision 1; ETag/)).toHaveTextContent('"config-etag"');
    await openSetup('Edit FinOps setup');
    await user.click(screen.getByRole('button', { name: 'Save FinOps configuration' }));
    expect(saveFinOpsConfiguration).toHaveBeenCalledWith('product-example', 'project-example', {
      service_id: 'finops', enabled: true, connection_ref: connection, finops: config.settings.finops,
    }, '"config-etag"');
    expect(screen.getByRole('button', { name: 'Start FinOps setup' })).toBeDisabled();
    expect(screen.getByText(/Creating a new FinOps agent: Not available yet/)).toBeInTheDocument();
    expect(startFinOpsBinding).not.toHaveBeenCalled();
    await visitSection('Runs & schedules');
    await user.type(screen.getByLabelText('Original question'), 'What did this cost?');
    await user.type(screen.getByLabelText('Start (inclusive)'), '2026-01-01');
    await user.type(screen.getByLabelText('End (exclusive)'), '2026-02-01');
    await user.click(screen.getByRole('button', { name: 'Check FinOps readiness' }));
    await user.click(screen.getByRole('button', { name: 'Submit cost inquiry' }));
    await waitFor(() => expect(submitFinOpsRun).toHaveBeenCalledWith('product-example', 'project-example', expect.objectContaining({
      service_id: 'finops', capability: 'cost_inquiry', binding_id: 'binding-example', question: 'What did this cost?',
      query: { start: '2026-01-01', end: '2026-02-01', metric: 'UnblendedCost', granularity: 'MONTHLY', group_by: ['SERVICE'], currency: 'USD' },
    }), expect.any(String)));
    expect(screen.getByText(/Inquiry admitted; run run-example/)).toHaveTextContent('Accepted does not mean answered');
    await visitSection('Results');
    await user.click(screen.getByRole('button', { name: 'result-example: outcome_unknown; billing partial' }));
    expect(screen.getByLabelText('FinOps answer and evidence')).toHaveTextContent('native outcome_unknown; billing partial');
    await user.click(screen.getByText('Evidence and technical details'));
    expect(await screen.findByLabelText('FinOps answer and evidence')).toHaveTextContent('"amount": null');
    expect(screen.getByLabelText('FinOps answer and evidence')).toHaveTextContent('"amount": "-12.3400"');
    expect(getFinOpsPublication).not.toHaveBeenCalled();
  });

  it('serves FinOps from the Product services navigation', async () => {
    render(<ProductServices productId="product-example" projectId="project-example" tenantId="tenant-example" activeService="finops" />);
    expect(screen.getByRole('button', { name: 'FinOps' })).toHaveAttribute('aria-current', 'page');
    expect(await screen.findByRole('region', { name: 'FinOps service' })).toHaveTextContent('FinOps cost inquiries');
    expect(getFinOpsConfiguration).toHaveBeenCalledWith('product-example', 'project-example');
  });

  it('starts the first inquiry after current readiness passes while the stored configuration is needs_input', async () => {
    vi.mocked(getFinOpsConfiguration).mockResolvedValue({ value: { ...config, state: 'needs_input' }, etag: '"config-etag"' } as never);
    renderTab();
    await openSetup('Provider connection');
    await screen.findByText(/Configuration needs_input/);
    await visitSection('Runs & schedules');
    await userEvent.type(screen.getByLabelText('Original question'), 'What did this cost?');
    await userEvent.type(screen.getByLabelText('Start (inclusive)'), '2026-01-01');
    await userEvent.type(screen.getByLabelText('End (exclusive)'), '2026-02-01');
    expect(screen.getByRole('button', { name: 'Submit cost inquiry' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Check FinOps readiness' }));
    expect(await screen.findByText('FinOps readiness: ready; gaps: none.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Submit cost inquiry' }));
    await waitFor(() => expect(submitFinOpsRun).toHaveBeenCalledOnce());
    expect(screen.getByRole('button', { name: 'Submit cost inquiry' })).toBeDisabled();
  });

  it.each([false, undefined])('blocks new inquiries when deployment availability is %s despite a ready binding and check', async admissions_enabled => {
    vi.mocked(getFinOpsOptions).mockResolvedValue({ capabilities: ['cost_inquiry'], unavailable_reasons: [], admissions_enabled });
    renderTab();
    await openSetup('Provider connection');
    await screen.findByText(/Configuration ready/);
    await visitSection('Capabilities');
    expect(within(screen.getByRole('article', { name: 'Cost inquiry' })).getByText('Not available yet')).toBeInTheDocument();
    await visitSection('Runs & schedules');
    await userEvent.type(screen.getByLabelText('Original question'), 'Retain this draft question');
    await userEvent.type(screen.getByLabelText('Start (inclusive)'), '2026-01-01');
    await userEvent.type(screen.getByLabelText('End (exclusive)'), '2026-02-01');
    await userEvent.click(screen.getByRole('button', { name: 'Check FinOps readiness' }));
    expect(screen.getByText('FinOps readiness: not ready; gaps: none.')).toBeInTheDocument();
    const submit = screen.getByRole('button', { name: 'Submit cost inquiry' });
    expect(submit).toBeDisabled();
    fireEvent.submit(submit.closest('form')!);
    expect(submitFinOpsRun).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Original question')).toHaveValue('Retain this draft question');
    await openSetup('Edit FinOps setup');
    expect(screen.getByRole('button', { name: 'Save FinOps configuration' })).toBeEnabled();
    await visitSection('Runs & schedules');
    expect(screen.getByRole('button', { name: 'run-example: admitted' })).toBeEnabled();
    await visitSection('Results');
    await userEvent.click(screen.getByRole('button', { name: 'result-example: outcome_unknown; billing partial' }));
    expect(await screen.findByLabelText('FinOps answer and evidence')).toHaveTextContent('-12.3400 USD');
    expect(screen.getByLabelText('FinOps answer and evidence')).toHaveTextContent('Original answer content unavailable');
    expect(getFinOpsResult).toHaveBeenCalledOnce();
  });

  it('removes inquiry availability when a fresh readiness check reports admission disabled', async () => {
    vi.mocked(checkFinOpsConfiguration).mockResolvedValue({ ready: false, gaps: ['finops_admissions_disabled'] });
    renderTab();
    await openSetup('Provider connection');
    await screen.findByText(/Configuration ready/);
    await visitSection('Runs & schedules');
    await userEvent.click(screen.getByRole('button', { name: 'Check FinOps readiness' }));
    await visitSection('Capabilities');
    expect(within(screen.getByRole('article', { name: 'Cost inquiry' })).getByText('Not available yet')).toBeInTheDocument();
    await visitSection('Runs & schedules');
    expect(screen.getByRole('button', { name: 'Submit cost inquiry' })).toBeDisabled();
    expect(screen.getAllByText(/platform administrator must qualify FinOps execution/).length).toBeGreaterThan(0);
    expect(submitFinOpsRun).not.toHaveBeenCalled();
  });

  it.each([
    ['finops_admissions_disabled', 'platform administrator must qualify FinOps execution'],
    ['native_unqualified', 'verify the native executor and adoption readback'],
    ['work_queue_unavailable', 'restore the FinOps work queue configuration'],
    ['finops_authority_unavailable', 'restore the FinOps execution integration'],
  ])('explains %s after submission without losing the original receipt or retrying', async (code, instruction) => {
    vi.mocked(submitFinOpsRun).mockRejectedValue({ status: code === 'work_queue_unavailable' ? 503 : 409,
      detail: { code, message: 'private-provider-secret' } });
    renderTab();
    await openSetup('Provider connection');
    await screen.findByText(/Configuration ready/);
    await visitSection('Runs & schedules');
    await userEvent.type(screen.getByLabelText('Original question'), 'Keep this billing question');
    await userEvent.type(screen.getByLabelText('Start (inclusive)'), '2026-01-01');
    await userEvent.type(screen.getByLabelText('End (exclusive)'), '2026-02-01');
    await userEvent.click(screen.getByRole('button', { name: 'Check FinOps readiness' }));
    await userEvent.click(screen.getByRole('button', { name: 'Submit cost inquiry' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(instruction);
    expect(screen.getByRole('alert')).not.toHaveTextContent('private-provider-secret');
    expect(screen.getByLabelText('Original question')).toHaveValue('Keep this billing question');
    const receipt = vi.mocked(submitFinOpsRun).mock.calls[0][3];
    expect(receipt).toEqual(expect.any(String));
    expect(screen.getByText(/Original inquiry receipt:/)).toHaveTextContent(receipt!);
    expect(screen.getByRole('button', { name: 'Submit cost inquiry' })).toBeDisabled();
    expect(submitFinOpsRun).toHaveBeenCalledOnce();
    if (code === 'finops_admissions_disabled') {
      await visitSection('Capabilities');
      expect(within(screen.getByRole('article', { name: 'Cost inquiry' })).getByText('Not available yet')).toBeInTheDocument();
    }
  });

  it('requires another check after the selected binding or current readback changes', async () => {
    vi.mocked(listFinOpsBindings).mockResolvedValue({ items: [binding, { ...binding, binding_id: 'binding-two' }] } as never);
    renderTab();
    await openSetup('Provider connection');
    await screen.findByText(/Configuration ready/);
    await visitSection('Runs & schedules');
    const submit = screen.getByRole('button', { name: 'Submit cost inquiry' });
    const check = screen.getByRole('button', { name: 'Check FinOps readiness' });
    await userEvent.click(check);
    expect(submit).toBeEnabled();
    await userEvent.selectOptions(screen.getByLabelText('Ready binding'), 'binding-two');
    expect(submit).toBeDisabled();
    expect(screen.queryByText('FinOps readiness: ready; gaps: none.')).not.toBeInTheDocument();
    await userEvent.click(check);
    expect(submit).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Refresh FinOps state' }));
    await waitFor(() => expect(check).toBeEnabled());
    expect(submit).toBeDisabled();
  });

  it('ignores a late readiness response after a refresh loads a different configuration revision', async () => {
    let resolveCheck!: (value: { ready: boolean; gaps: string[] }) => void;
    vi.mocked(checkFinOpsConfiguration).mockImplementationOnce(() => new Promise(resolve => { resolveCheck = resolve; }));
    renderTab();
    await openSetup('Provider connection');
    await screen.findByText(/Configuration ready/);
    await visitSection('Runs & schedules');
    await userEvent.click(screen.getByRole('button', { name: 'Check FinOps readiness' }));
    vi.mocked(getFinOpsConfiguration).mockResolvedValue({ value: { ...config, revision: 2 }, etag: '"config-two"' } as never);
    vi.mocked(listFinOpsBindings).mockResolvedValue({ items: [{ ...binding, native_configuration_revision: 2 }] } as never);
    await userEvent.click(screen.getByRole('button', { name: 'Refresh FinOps state' }));
    await openSetup('Provider connection');
    await screen.findByText(/Configuration ready; revision 2/);
    await visitSection('Runs & schedules');
    await act(async () => resolveCheck({ ready: true, gaps: [] }));
    expect(screen.queryByText('FinOps readiness: ready; gaps: none.')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Submit cost inquiry' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Check FinOps readiness' }));
    expect(screen.getByRole('button', { name: 'Submit cost inquiry' })).toBeEnabled();
  });

  it('invalidates a passed check before a configuration save', async () => {
    vi.mocked(saveFinOpsConfiguration).mockResolvedValue({ value: { ...config, revision: 2 }, etag: '"config-two"' } as never);
    renderTab();
    await openSetup('Provider connection');
    await screen.findByText(/Configuration ready/);
    await visitSection('Runs & schedules');
    await userEvent.click(screen.getByRole('button', { name: 'Check FinOps readiness' }));
    expect(screen.getByRole('button', { name: 'Submit cost inquiry' })).toBeEnabled();
    await openSetup('Edit FinOps setup');
    await userEvent.click(screen.getByRole('button', { name: 'Save FinOps configuration' }));
    await openSetup('Provider connection');
    await screen.findByText(/Configuration ready; revision 2/);
    await visitSection('Runs & schedules');
    expect(screen.getByRole('button', { name: 'Submit cost inquiry' })).toBeDisabled();
    expect(screen.queryByText('FinOps readiness: ready; gaps: none.')).not.toBeInTheDocument();
  });

  it.each([
    ['disabled setup', false, { ready: true, gaps: [] }],
    ['missing authorization', true, { ready: false, gaps: ['execution_delegation'] }],
    ['inconsistent readiness', true, { ready: true, gaps: ['billing_scope_unverified'] }],
  ] as const)('keeps submission disabled for %s', async (_reason, enabled, readiness) => {
    vi.mocked(getFinOpsConfiguration).mockResolvedValue({ value: { ...config, settings: { ...config.settings, enabled } }, etag: '"config-etag"' } as never);
    vi.mocked(checkFinOpsConfiguration).mockResolvedValue({ ready: readiness.ready, gaps: [...readiness.gaps] });
    renderTab();
    await openSetup('Provider connection');
    await screen.findByText(/Configuration ready/);
    await visitSection('Runs & schedules');
    await userEvent.click(screen.getByRole('button', { name: 'Check FinOps readiness' }));
    expect(screen.getByRole('button', { name: 'Submit cost inquiry' })).toBeDisabled();
    expect(submitFinOpsRun).not.toHaveBeenCalled();
  });

  it.each([403, 404])('removes retained details when the current readiness check denies access (%s)', async status => {
    vi.mocked(checkFinOpsConfiguration).mockRejectedValue({ status });
    renderTab();
    await openSetup('Provider connection');
    await screen.findByText(/Configuration ready/);
    await visitSection('Runs & schedules');
    await userEvent.click(screen.getByRole('button', { name: 'Check FinOps readiness' }));
    expect(await screen.findByText('FinOps details are unavailable for this scope.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Submit cost inquiry' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'run-example: admitted' })).not.toBeInTheDocument();
  });

  it('uses the supported adoption path with the exact scoped native reference', async () => {
    const user = userEvent.setup();
    vi.mocked(getFinOpsOptions).mockResolvedValue({ capabilities: ['cost_inquiry'], unavailable_reasons: [], admissions_enabled: false });
    const existing = { ...config, settings: { ...config.settings, finops: { agent_mode: 'adopt', native_agent_ref: 'existing-agent' } } };
    vi.mocked(getFinOpsConfiguration).mockResolvedValue({ value: existing, etag: '"config-etag"' } as never);
    const native = { tenant_id: scope.tenant_id, project_id: scope.project_id, source_connection_id: 'connection-example',
      record_type: 'finops_agent', record_id: 'existing-agent', record_revision: 'observed-revision' };
    renderTab();
    await openSetup('Provider connection');
    await user.click(await screen.findByLabelText('Revisioned scoped FinOps agent reference (JSON)'));
    await user.paste(JSON.stringify(native));
    await user.click(screen.getByRole('button', { name: 'Start FinOps setup' }));
    expect(startFinOpsBinding).toHaveBeenCalledWith('product-example', 'project-example', {
      action: 'link', configuration_revision: 1, native_resource_ref: native,
    });
    expect(await screen.findByRole('alert')).toHaveTextContent('Setup blocked; operation operation-example. Acceptance is not completion.');
    expect(submitFinOpsRun).not.toHaveBeenCalled();
  });

  it('clears data from an inaccessible tenant rather than showing a foreign scope', async () => {
    vi.mocked(getFinOpsConfiguration).mockResolvedValue({ value: { ...config, scope: { ...scope, tenant_id: 'foreign-tenant' } }, etag: '"foreign"' } as never);
    renderTab();
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details were removed');
    expect(screen.queryByText(/Configuration ready; revision/)).not.toBeInTheDocument();
    expect(listFinOpsRuns).not.toHaveBeenCalled();
  });

  it.each([
    [409, 'idempotency_conflict'], [422, 'validation_error'], [428, 'precondition_required'],
  ])('shows HTTP %i and %s without echoing a private host message', async (status, code) => {
    const user = userEvent.setup();
    vi.mocked(saveFinOpsConfiguration).mockRejectedValue({ status, detail: { code, message: 'private-provider-secret' } });
    renderTab();
    await openSetup('Provider connection');
    await screen.findByText(/Configuration ready; revision/);
    await openSetup('Edit FinOps setup');
    await user.click(screen.getByRole('button', { name: 'Save FinOps configuration' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(`HTTP ${status} ${code}`);
    expect(screen.getByRole('alert')).not.toHaveTextContent('private-provider-secret');
    expect(saveFinOpsConfiguration).toHaveBeenCalledTimes(1);
  });

  it('keeps the previous read on a transient error and clears it on scope denial', async () => {
    const user = userEvent.setup();
    vi.mocked(getFinOpsRun).mockRejectedValueOnce({ status: 503, detail: { code: 'service_unavailable', message: 'private-provider-secret' } })
      .mockRejectedValueOnce({ status: 404, detail: { code: 'not_found' } });
    renderTab();
    await visitSection('Runs & schedules');
    await user.click(await screen.findByRole('button', { name: 'run-example: admitted' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 503 service_unavailable');
    expect(screen.getByRole('alert')).not.toHaveTextContent('private-provider-secret');
    expect(screen.getByRole('button', { name: 'run-example: admitted' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'run-example: admitted' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 404 not_found');
    expect(screen.queryByRole('button', { name: 'run-example: admitted' })).not.toBeInTheDocument();
  });

  it('never silently retries a stale configuration write', async () => {
    const user = userEvent.setup();
    vi.mocked(saveFinOpsConfiguration).mockRejectedValue({ status: 412, detail: { code: 'stale_revision', message: 'private-provider-secret' } });
    renderTab();
    await openSetup('Provider connection');
    await screen.findByText(/Configuration ready; revision/);
    await openSetup('Edit FinOps setup');
    await user.click(screen.getByRole('button', { name: 'Save FinOps configuration' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Refresh before editing');
    expect(screen.getByRole('alert')).toHaveTextContent('HTTP 412 stale_revision');
    expect(screen.getByRole('alert')).not.toHaveTextContent('private-provider-secret');
    expect(screen.getByRole('button', { name: 'Save FinOps configuration' })).toBeDisabled();
    expect(saveFinOpsConfiguration).toHaveBeenCalledTimes(1);
  });
  it.each(['answered', 'refused', 'outcome_unknown'] as const)(
    'keeps %s native answer, billed Decimal and attribution evidence separate', async status => {
      const user = userEvent.setup();
      const variant = { ...result,
        native: { ...result.native, status, answer_artifact: status === 'answered' ? artifact : null, measure: { ...result.native.measure,
          amount: status === 'answered' ? '-10.0000' : null } },
        comparison: { ...result.comparison, delta: status === 'answered' ? '-2.3400' : null },
      };
      vi.mocked(listFinOpsResults).mockResolvedValue({ items: [variant] } as never);
      vi.mocked(getFinOpsResult).mockResolvedValue({ value: variant, etag: '"result-etag"' } as never);
      renderTab();
      await visitSection('Results');
      await user.click(await screen.findByRole('button', { name: `result-example: ${status}; billing partial` }));
      const answer = await screen.findByLabelText('FinOps answer and evidence');
      await user.click(screen.getByText('Evidence and technical details'));
      const section = (title: string) => JSON.parse(screen.getByRole('heading', { name: title, exact: true }).nextElementSibling?.textContent ?? 'null');
      expect(section('Original scoped inquiry')).toEqual(query);
      expect(screen.getByRole('heading', { name: 'Answer', exact: true }).nextElementSibling).toHaveTextContent(status === 'answered' ? 'Original example native answer' : 'Original answer content unavailable');
      expect(getFinOpsNativeAnswer).toHaveBeenCalledTimes(status === 'answered' ? 1 : 0);
      expect(section('Native answer receipt')).toEqual(variant.native);
      expect(section('Native answer receipt').measure.amount).toBe(status === 'answered' ? '-10.0000' : null);
      expect(section('Independent billing evidence')).toEqual(variant.oracle);
      expect(section('Independent billing evidence').measure.amount).toBe('-12.3400');
      expect(section('Attribution evidence')).toEqual([artifact]);
      expect(section('Comparison')).toEqual(variant.comparison);
      expect(answer).toHaveTextContent('Publication: not reported');
      expect(submitFinOpsRun).not.toHaveBeenCalled();
    },
  );

  it('does not call an unread publication not reported while keeping the original answer', async () => {
    const user = userEvent.setup();
    const pending = { ...result, publication_id: 'publication-example' };
    vi.mocked(listFinOpsResults).mockResolvedValue({ items: [pending] } as never);
    vi.mocked(getFinOpsResult).mockResolvedValue({ value: pending, etag: '"result-etag"' } as never);
    vi.mocked(getFinOpsPublication).mockRejectedValue({ status: 503, detail: { code: 'service_unavailable' } });
    renderTab();
    await visitSection('Results');
    await user.click(await screen.findByRole('button', { name: 'result-example: outcome_unknown; billing partial' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Publication status is unavailable');
    expect(screen.getByLabelText('FinOps answer and evidence')).toHaveTextContent('Publication: status unavailable');
    await user.click(screen.getByText('Evidence and technical details'));
    expect(JSON.parse(screen.getByText('Native answer receipt').nextElementSibling?.textContent ?? 'null')).toEqual(pending.native);
    expect(submitFinOpsRun).not.toHaveBeenCalled();
  });
  it.each([403, 404])('clears cached details after a deferred publication denial (%s)', async status => {
    const user = userEvent.setup();
    const pending = { ...result, publication_id: 'publication-example' };
    let rejectPublication!: (reason: unknown) => void;
    const publicationRead = new Promise<never>((_resolve, reject) => { rejectPublication = reject; });
    vi.mocked(getFinOpsResult).mockResolvedValue({ value: pending, etag: '"result-etag"' } as never);
    vi.mocked(getFinOpsPublication).mockReturnValue(publicationRead);
    renderTab();
    await visitSection('Results');
    await user.click(await screen.findByRole('button', { name: 'result-example: outcome_unknown; billing partial' }));
    expect(await screen.findByLabelText('FinOps answer and evidence')).toBeInTheDocument();
    await act(async () => rejectPublication({ status, detail: { code: 'not_found' } }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details were removed.');
    expect(screen.queryByLabelText('FinOps answer and evidence')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'run-example: admitted' })).not.toBeInTheDocument();
    expect(screen.getByText('FinOps details are unavailable for this scope.')).toBeInTheDocument();
    expect(submitFinOpsRun).not.toHaveBeenCalled();
  });

  it('loads and selects inquiries and results beyond the first 25 records', async () => {
    const user = userEvent.setup();
    const laterRun = { ...run, run_id: 'run-26' };
    const laterResult = { ...result, result_id: 'result-26', run_id: laterRun.run_id };
    vi.mocked(listFinOpsRuns).mockResolvedValueOnce({
      items: Array.from({ length: 25 }, (_, index) => ({ ...run, run_id: `run-${index + 1}` })), next_cursor: 'runs-page-2',
    } as never).mockResolvedValueOnce({ items: [laterRun] } as never);
    vi.mocked(listFinOpsResults).mockResolvedValueOnce({
      items: Array.from({ length: 25 }, (_, index) => ({ ...result, result_id: `result-${index + 1}` })), next_cursor: 'results-page-2',
    } as never).mockResolvedValueOnce({ items: [laterResult] } as never);
    vi.mocked(getFinOpsRun).mockResolvedValue({ value: laterRun, etag: '"run-26"' } as never);
    vi.mocked(getFinOpsResult).mockResolvedValue({ value: laterResult, etag: '"result-26"' } as never);
    renderTab();
    await visitSection('Runs & schedules');
    await user.click(await screen.findByRole('button', { name: 'Load more inquiries' }));
    await user.click(await screen.findByRole('button', { name: 'run-26: admitted' }));
    expect(listFinOpsRuns).toHaveBeenLastCalledWith('product-example', 'project-example', 'runs-page-2');
    expect(getFinOpsRun).toHaveBeenCalledWith('product-example', 'project-example', 'run-26');
    expect(screen.getAllByRole('button', { name: /^run-\d+: admitted$/ })).toHaveLength(26);
    await visitSection('Results');
    await user.click(screen.getByRole('button', { name: 'Load more results' }));
    await user.click(await screen.findByRole('button', { name: 'result-26: outcome_unknown; billing partial' }));
    expect(listFinOpsResults).toHaveBeenLastCalledWith('product-example', 'project-example', 'results-page-2');
    expect(getFinOpsResult).toHaveBeenCalledWith('product-example', 'project-example', 'result-26');
    expect(screen.getAllByRole('button', { name: /^result-\d+:/ })).toHaveLength(26);
    expect(screen.queryByRole('button', { name: 'Load more results' })).not.toBeInTheDocument();
    await visitSection('Runs & schedules');
    expect(screen.queryByRole('button', { name: 'Load more inquiries' })).not.toBeInTheDocument();
    expect(submitFinOpsRun).not.toHaveBeenCalled();
  });

  it.each(['project', 'tenant'])('discards pending pages and cursors when the %s scope changes', async dimension => {
    const user = userEvent.setup();
    let resolveRuns!: (value: never) => void;
    let resolveResults!: (value: never) => void;
    vi.mocked(listFinOpsRuns).mockResolvedValueOnce({ items: [run], next_cursor: 'old-runs' } as never)
      .mockReturnValueOnce(new Promise<never>(resolve => { resolveRuns = resolve; }));
    vi.mocked(listFinOpsResults).mockResolvedValueOnce({ items: [result], next_cursor: 'old-results' } as never)
      .mockReturnValueOnce(new Promise<never>(resolve => { resolveResults = resolve; }));
    const view = render(<ProductServices productId="product-example" projectId="project-example" tenantId="tenant-example" activeService="finops" />);
    await visitSection('Runs & schedules');
    await user.click(await screen.findByRole('button', { name: 'Load more inquiries' }));
    await visitSection('Results');
    await user.click(screen.getByRole('button', { name: 'Load more results' }));
    const nextScope = { ...scope, [`${dimension}_id`]: `${dimension}-other` };
    vi.mocked(getFinOpsConfiguration).mockResolvedValue({ value: { ...config, scope: nextScope }, etag: '"other"' } as never);
    vi.mocked(listFinOpsBindings).mockResolvedValue({ items: [] });
    vi.mocked(listFinOpsRuns).mockResolvedValue({ items: [] });
    vi.mocked(listFinOpsResults).mockResolvedValue({ items: [] });
    view.rerender(<ProductServices productId="product-example" projectId={nextScope.project_id} tenantId={nextScope.tenant_id} activeService="finops" />);
    await openSetup('Provider connection');
    await waitFor(() => expect(screen.getByText(/Configuration ready/)).toHaveTextContent('ETag "other"'));
    await act(async () => {
      resolveRuns({ items: [run], next_cursor: 'stale-runs' } as never);
      resolveResults({ items: [result], next_cursor: 'stale-results' } as never);
    });
    await visitSection('Runs & schedules');
    expect(screen.queryByRole('button', { name: 'run-example: admitted' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load more/ })).not.toBeInTheDocument();
    await visitSection('Results');
    expect(screen.queryByRole('button', { name: 'result-example: outcome_unknown; billing partial' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load more/ })).not.toBeInTheDocument();
    expect(listFinOpsRuns).toHaveBeenLastCalledWith('product-example', nextScope.project_id);
    expect(listFinOpsResults).toHaveBeenLastCalledWith('product-example', nextScope.project_id);
  });

  it.each(['inquiries', 'results'])('clears details when a later %s page belongs to another tenant', async kind => {
    const user = userEvent.setup();
    const foreignScope = { ...scope, tenant_id: 'tenant-foreign' };
    if (kind === 'inquiries') {
      vi.mocked(listFinOpsRuns).mockResolvedValueOnce({ items: [run], next_cursor: 'runs-next' } as never)
        .mockResolvedValueOnce({ items: [{ ...run, scope: foreignScope, run_id: 'foreign-run' }] } as never);
    } else {
      vi.mocked(listFinOpsResults).mockResolvedValueOnce({ items: [result], next_cursor: 'results-next' } as never)
        .mockResolvedValueOnce({ items: [{ ...result, scope: foreignScope, result_id: 'foreign-result' }] } as never);
    }
    renderTab();
    await visitSection('Results');
    await user.click(await screen.findByRole('button', { name: 'result-example: outcome_unknown; billing partial' }));
    expect(await screen.findByLabelText('FinOps answer and evidence')).toBeInTheDocument();
    await visitSection(kind === 'inquiries' ? 'Runs & schedules' : 'Results');
    await user.click(screen.getByRole('button', { name: `Load more ${kind}` }));
    expect(await screen.findByText('FinOps details are unavailable for this scope.')).toBeInTheDocument();
    expect(screen.queryByLabelText('FinOps answer and evidence')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /foreign-/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load more/ })).not.toBeInTheDocument();
  });

  it('discards a pending page when publication authorization is revoked', async () => {
    const user = userEvent.setup();
    let resolvePage!: (value: never) => void;
    vi.mocked(listFinOpsRuns).mockResolvedValueOnce({ items: [run], next_cursor: 'runs-next' } as never)
      .mockReturnValueOnce(new Promise<never>(resolve => { resolvePage = resolve; }));
    vi.mocked(getFinOpsResult).mockResolvedValue({ value: { ...result, publication_id: 'publication-example' }, etag: '"result-etag"' } as never);
    vi.mocked(getFinOpsPublication).mockRejectedValue({ status: 404 });
    renderTab();
    await visitSection('Runs & schedules');
    await user.click(await screen.findByRole('button', { name: 'Load more inquiries' }));
    expect(screen.getByRole('button', { name: 'Load more inquiries' })).toBeDisabled();
    await visitSection('Results');
    await user.click(screen.getByRole('button', { name: 'result-example: outcome_unknown; billing partial' }));
    expect(await screen.findByText('FinOps details are unavailable for this scope.')).toBeInTheDocument();
    await act(async () => resolvePage({ items: [run], next_cursor: 'stale-next' } as never));
    expect(screen.queryByLabelText('FinOps answer and evidence')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load more/ })).not.toBeInTheDocument();
  });

  it('discards a pending page when refreshing the first page', async () => {
    const user = userEvent.setup();
    let resolvePage!: (value: never) => void;
    vi.mocked(listFinOpsRuns).mockResolvedValueOnce({ items: [run], next_cursor: 'runs-next' } as never)
      .mockReturnValueOnce(new Promise<never>(resolve => { resolvePage = resolve; }))
      .mockResolvedValueOnce({ items: [{ ...run, run_id: 'fresh-run' }] } as never);
    renderTab();
    await visitSection('Runs & schedules');
    await user.click(await screen.findByRole('button', { name: 'Load more inquiries' }));
    await user.click(screen.getByRole('button', { name: 'Refresh FinOps state' }));
    expect(await screen.findByRole('button', { name: 'fresh-run: admitted' })).toBeInTheDocument();
    await act(async () => resolvePage({ items: [run], next_cursor: 'stale-next' } as never));
    expect(screen.queryByRole('button', { name: 'run-example: admitted' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more inquiries' })).not.toBeInTheDocument();
  });

  it.each(['success', 'denial'])('ignores an older publication %s after selecting another answer', async outcome => {
    const user = userEvent.setup();
    const first = { ...result, publication_id: 'publication-first' };
    const second = { ...result, result_id: 'result-other', publication_id: 'publication-second',
      native: { ...result.native, status: 'refused' } };
    let resolveFirst!: (value: never) => void;
    let rejectFirst!: (reason: unknown) => void;
    const firstRead = new Promise<never>((resolve, reject) => { resolveFirst = resolve; rejectFirst = reject; });
    vi.mocked(listFinOpsResults).mockResolvedValue({ items: [first, second] } as never);
    vi.mocked(getFinOpsResult).mockImplementation(async (_product, _project, resultId) =>
      ({ value: resultId === first.result_id ? first : second, etag: '"result-etag"' } as never));
    vi.mocked(getFinOpsPublication).mockImplementation(async (_product, _project, publicationId) =>
      publicationId === 'publication-first' ? firstRead : ({ state: 'pending' } as never));
    renderTab();
    await visitSection('Results');
    await user.click(await screen.findByRole('button', { name: 'result-example: outcome_unknown; billing partial' }));
    await waitFor(() => expect(getFinOpsPublication).toHaveBeenCalledWith(
      'product-example', 'project-example', 'publication-first'));
    await user.click(screen.getByRole('button', { name: 'result-other: refused; billing partial' }));
    expect(await screen.findByLabelText('FinOps answer and evidence')).toHaveTextContent('Publication: pending');
    await act(async () => {
      if (outcome === 'denial') rejectFirst({ status: 404 });
      else resolveFirst({ state: 'failed' } as never);
    });
    expect(screen.getByLabelText('FinOps answer and evidence')).toHaveTextContent('Result result-other; native refused');
    expect(screen.getByLabelText('FinOps answer and evidence')).toHaveTextContent('Publication: pending');
    expect(submitFinOpsRun).not.toHaveBeenCalled();
  });

  it.each([403, 404])('clears cached native details on denied answer readback (%s)', async status => {
    const user = userEvent.setup();
    const answered = { ...result, native: { ...result.native, status: 'answered', answer_artifact: artifact } };
    vi.mocked(listFinOpsResults).mockResolvedValue({ items: [answered] } as never);
    vi.mocked(getFinOpsResult).mockResolvedValue({ value: answered, etag: '"result-etag"' } as never);
    vi.mocked(getFinOpsNativeAnswer).mockRejectedValue({ status, detail: { code: 'not_found' } });
    renderTab();
    await visitSection('Results');
    await user.click(await screen.findByRole('button', { name: 'result-example: answered; billing partial' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Previously shown details were removed.');
    expect(screen.queryByLabelText('FinOps answer and evidence')).not.toBeInTheDocument();
  });

  it('rejects a fabricated native answer revision without showing old result data', async () => {
    const user = userEvent.setup();
    const answered = { ...result, native: { ...result.native, status: 'answered', answer_artifact: artifact } };
    vi.mocked(listFinOpsResults).mockResolvedValue({ items: [answered] } as never);
    vi.mocked(getFinOpsResult).mockResolvedValue({ value: answered, etag: '"result-etag"' } as never);
    vi.mocked(getFinOpsNativeAnswer).mockResolvedValue({ result_id: result.result_id,
      run_id: result.run_id, revision: 2, artifact, text: 'wrong-revision answer' });
    renderTab();
    await visitSection('Results');
    await user.click(await screen.findByRole('button', { name: 'result-example: answered; billing partial' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('previous answer is not a fresh read');
    expect(screen.queryByLabelText('FinOps answer and evidence')).not.toBeInTheDocument();
  });
});
