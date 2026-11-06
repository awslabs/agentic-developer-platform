import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { apiClient } from '@/services/api';
import DevOpsOperations from '@/pages/products/services/DevOpsOperations';
import { reconcileDevOpsRun } from '@/services/l5Services';
import { checkInvestigationReadiness } from '@/services/devOpsReadiness';
import { checkNativeEligibility } from '@/services/devOpsNativeAccess';
import { readDevOpsInvestigationReport } from '@/services/devopsInvestigationReport';
import { configureDevOpsPrevention, getDevOpsBinding, getDevOpsPrevention, getDevOpsPublication, getDevOpsRun, retryDevOpsPublication, launchDevOpsBrowser, listDevOpsBindings, listDevOpsEvents, listDevOpsFindings, listDevOpsResults, listDevOpsRuns, startDevOpsBindingOperation, submitDevOpsRun } from '@/services/l5Services';

vi.mock('@/services/devopsInvestigationReport', async original => ({ ...await original<typeof import('@/services/devopsInvestigationReport')>(), readDevOpsInvestigationReport: vi.fn() }));
vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }));
vi.mock('@/services/devOpsReadiness', async importOriginal => ({ ...await importOriginal<typeof import('@/services/devOpsReadiness')>(), checkInvestigationReadiness: vi.fn() }));
vi.mock('@/services/devOpsNativeAccess', async importOriginal => ({ ...await importOriginal<typeof import('@/services/devOpsNativeAccess')>(), checkNativeEligibility: vi.fn() }));

vi.mock('@/services/l5Services', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/l5Services')>(),
  configureDevOpsPrevention: vi.fn(), getDevOpsBinding: vi.fn(), getDevOpsPrevention: vi.fn(), getDevOpsPublication: vi.fn(), getDevOpsRun: vi.fn(), retryDevOpsPublication: vi.fn(),
  launchDevOpsBrowser: vi.fn(), listDevOpsBindings: vi.fn(), listDevOpsEvents: vi.fn(), listDevOpsFindings: vi.fn(),
  listDevOpsResults: vi.fn(), listDevOpsRuns: vi.fn(), startDevOpsBindingOperation: vi.fn(), submitDevOpsRun: vi.fn(), reconcileDevOpsRun: vi.fn(),
}));

const scope = { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '5' };
const reference = { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'connection', record_id: 'connection-one', record_revision: '4' };
const configuration = { value: { scope, configuration_id: 'config-one', revision: 4, hash: 'configuration-four', state: 'ready', settings: { service_id: 'devops', enabled: true, capabilities: ['investigation'], triggers: [], devops: { space_mode: 'create', integrations: [], trigger_policies: [], permitted_native_actions: [], prevention_enabled: false, browser_launch_policy: 'disabled' } } }, etag: '"configuration-four"' } as never;
const preventionConfiguration = { ...(configuration as object), value: { ...(configuration as { value: object }).value,
  settings: { ...(configuration as { value: { settings: object } }).value.settings, capabilities: ['investigation', 'prevention'] } } } as never;
const investigation = () => {
  const now = Date.now();
  return { ready: true, gaps: [], scope, configuration_id: 'config-one', configuration_revision: 4, configuration_hash: 'configuration-four',
  capability: 'investigation' as const, binding_id: 'binding-one', binding_generation: 2, binding_revision: 2,
  checked_at: new Date(now - 1_000).toISOString(), valid_until: new Date(now + 59_000).toISOString() };
};
const setupConfiguration = { ...configuration as object, value: { ...(configuration as unknown as { value: object }).value, scope: { ...scope, owner_id: 'owner-one' }, state: 'disabled', settings: { service_id: 'devops', enabled: false, capabilities: ['investigation'], triggers: [], devops: { space_mode: 'create', integrations: [], trigger_policies: [], permitted_native_actions: [], prevention_enabled: false, browser_launch_policy: 'disabled' } } } } as never;
const binding = { scope, binding_id: 'binding-one', configuration_id: 'config-one', revision: 2, generation: 2, state: 'ready', ownership: 'adp_created', connection_ref: reference,
  gaps: [], readiness: ['native_ui', 'investigation', 'prevention'].map(capability => ({ capability, state: 'ready', reason_codes: [], configuration_revision: 4, binding_generation: 2, valid_until: '2100-01-01T00:00:00Z' })) };
const run = { scope, run_id: 'run-one', revision: 1, state: 'admitted', provider_state: 'pending', deadline_at: '2100-01-01T00:00:00Z', origin: 'adp_submitted' };
const result = { scope, result_id: 'result-one', run_id: 'run-one', revision: 1, observed_at: '2026-01-01T00:00:00Z', coverage: 'partial', coverage_mode: 'native_observation',
  native_job_ref: reference, provider_outcome: 'outcome_unknown', origin: 'native', publication_id: 'publication-one' };
const prevention = { binding_id: 'binding-one', configuration_revision: 4, status: 'blocked', reason: 'prevention_readback_missing', desired: { enabled: false }, observed: null };

describe('DevOps operations', () => {
  beforeEach(() => {
    vi.resetAllMocks(); sessionStorage.clear();
    vi.mocked(listDevOpsBindings).mockResolvedValue({ items: [binding] } as never);
    vi.mocked(listDevOpsRuns).mockResolvedValue({ items: [run] } as never);
    vi.mocked(listDevOpsResults).mockResolvedValue({ items: [result] } as never);
    vi.mocked(getDevOpsBinding).mockResolvedValue({ value: binding, etag: '"binding-two"' } as never);
    vi.mocked(listDevOpsEvents).mockResolvedValue({ items: [{ event_id: 'event-one', binding_id: 'binding-one', scope, origin: 'native_event', state: 'reconciled', received_at: '2026-01-01T00:00:00Z', result_ref: reference }] });
    vi.mocked(getDevOpsPrevention).mockResolvedValue(prevention as never);
    vi.mocked(checkInvestigationReadiness).mockImplementation(async () => investigation());
    vi.mocked(checkNativeEligibility).mockImplementation(async () => ({ ...investigation(), capability: 'native_ui' }));
    vi.mocked(startDevOpsBindingOperation).mockImplementation(async (_product, _project, request) => ({ value: { operation_id: 'operation-one', state: 'accepted', action: request.action, revision: 1, gaps: [] }, etag: '"operation-one"' }));
    vi.mocked(submitDevOpsRun).mockResolvedValue({ value: run, etag: '"run-one"' } as never);
    vi.mocked(listDevOpsFindings).mockResolvedValue({ items: [{ finding_id: 'finding-one', claim: 'Needs review', validation: 'candidate', source_refs: [] }] } as never);
    vi.mocked(getDevOpsPublication).mockResolvedValue({ publication_id: 'publication-one', result_id: 'result-one', state: 'retry_wait' } as never);
    vi.mocked(configureDevOpsPrevention).mockResolvedValue(prevention as never);
    vi.mocked(launchDevOpsBrowser).mockRejectedValue({ status: 403 });
  });

  afterEach(() => { vi.useRealTimers(); });

  it('displays the saved workspace before an access check and never opens it automatically', async () => {
    vi.mocked(listDevOpsBindings).mockResolvedValue({ items: [{ ...binding,
      native_resource_ref: { ...reference, record_type: 'agent_space', record_id: 'workspace-example' },
      provider_identity: { account_id: '111122223333', region: 'us-east-1' },
    }] } as never);
    vi.mocked(getDevOpsBinding).mockResolvedValue({ value: { ...binding, native_resource_ref: { ...reference, record_type: 'agent_space', record_id: 'workspace-example' }, provider_identity: { account_id: '111122223333', region: 'us-east-1' } }, etag: '"binding-two"' } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    expect(await screen.findByText('workspace-example', { exact: false })).toBeVisible();
    expect(screen.getByText(/Workspace sign-in is not configured/)).toBeVisible();
    expect(screen.queryByRole('combobox', { name: 'Saved DevOps workspace' })).not.toBeInTheDocument();
    expect(getDevOpsBinding).toHaveBeenCalledWith('product-one', 'project-one', 'binding-one');
    expect(checkNativeEligibility).not.toHaveBeenCalled();
    expect(launchDevOpsBrowser).not.toHaveBeenCalled();
  });

  it('does not display a workspace from another configuration in the same project', async () => {
    vi.mocked(listDevOpsBindings).mockResolvedValue({ items: [{ ...binding, configuration_id: 'other-configuration',
      native_resource_ref: { ...reference, record_id: 'other-workspace' },
    }] } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await screen.findByRole('listitem', { name: /Run run-one: admitted/ });
    expect(screen.queryByText('other-workspace')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Review workspace access' })).not.toBeInTheDocument();
  });

  it('shows the recovery action and collects the existing expired investigation without a new run', async () => {
    const expired = { ...run, state: 'expired', native_job_ref: reference, input: { capability: 'investigation' },
      monitoring: { state: 'degraded', last_poll_error: { code: 'observation_expired', message: 'Report collection timed out.', action: 'Collect the existing report.' } } };
    vi.mocked(listDevOpsRuns).mockResolvedValue({ items: [expired] } as never);
    vi.mocked(reconcileDevOpsRun).mockResolvedValue({ value: { ...expired, revision: 2, monitoring: { state: 'recovery_pending' } }, etag: '"2"' } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    expect(await screen.findByText(/Report collection timed out/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Collect existing report' }));
    expect(reconcileDevOpsRun).toHaveBeenCalledWith('product-one', 'project-one', 'run-one', 'devops-report-run-one-1',
      { revision: 1, native_job_ref: reference });
    expect(screen.getByText(/15 minutes of read-only access/)).toBeInTheDocument();
    expect(await screen.findByText('Collecting the existing investigation report…')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Collect existing report' })).toBeDisabled();
    expect(screen.getByText('Updating automatically')).toBeInTheDocument();
    expect(submitDevOpsRun).not.toHaveBeenCalled();
  });

  it('reuses the recovery receipt after a lost response and rejects another run in the response', async () => {
    const denied = vi.fn();
    const expired = { ...run, state: 'outcome_unknown', native_job_ref: reference, input: { capability: 'investigation' } };
    vi.mocked(listDevOpsRuns).mockResolvedValue({ items: [expired] } as never);
    vi.mocked(reconcileDevOpsRun).mockRejectedValueOnce({ status: 503 })
      .mockResolvedValueOnce({ value: { ...expired, run_id: 'another-run', revision: 2 }, etag: '"2"' } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={denied} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Collect existing report' }));
    expect(await screen.findByText(/Report recovery was not confirmed/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Collect existing report' }));
    expect(reconcileDevOpsRun).toHaveBeenCalledTimes(2);
    expect(vi.mocked(reconcileDevOpsRun).mock.calls[0]).toEqual(vi.mocked(reconcileDevOpsRun).mock.calls[1]);
    expect(denied).toHaveBeenCalledOnce();
    expect(submitDevOpsRun).not.toHaveBeenCalled();
  });

  it('refreshes a running investigation and shows its completed result without another submission', async () => {
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await screen.findByRole('listitem', { name: /Run run-one: admitted/ });
    vi.useFakeTimers();
    vi.mocked(listDevOpsRuns).mockResolvedValue({ items: [{ ...run, state: 'running', revision: 2 }] } as never);
    // The initial timer uses the real clock; refresh rebuilds the polling lifecycle on the fake clock.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Refresh status' })); });
    vi.mocked(listDevOpsRuns).mockResolvedValue({ items: [{ ...run, state: 'succeeded', revision: 3 }] } as never);
    vi.mocked(listDevOpsResults).mockResolvedValue({ items: [{ ...result, provider_outcome: 'completed' }] } as never);
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(screen.getByRole('listitem', { name: /Run run-one: succeeded/ })).toHaveTextContent('succeeded');
    expect(screen.getByRole('listitem', { name: /Result result-one:/ })).toHaveTextContent('completed');
    expect(screen.queryByText('Updating automatically')).not.toBeInTheDocument();
    expect(submitDevOpsRun).not.toHaveBeenCalled();
  });

  it('keeps confirmed progress after a transient failure and clears it on revoked access', async () => {
    const denied = vi.fn();
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={denied} />);
    await screen.findByRole('listitem', { name: /Run run-one: admitted/ });
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Refresh status' })); });
    vi.mocked(listDevOpsRuns).mockRejectedValueOnce({ status: 503 });
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(screen.getByRole('listitem', { name: /Run run-one: admitted/ })).toBeInTheDocument();
    expect(screen.getByText(/Showing the last confirmed status/)).toBeInTheDocument();
    vi.mocked(listDevOpsRuns).mockRejectedValueOnce({ status: 403 });
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(screen.queryByRole('listitem', { name: /Run run-one:|Result result-one:/ })).not.toBeInTheDocument();
    expect(denied).toHaveBeenCalledOnce();
    expect(submitDevOpsRun).not.toHaveBeenCalled();
  });

  async function selectBinding(check = true) {
    await userEvent.click(await screen.findByRole('button', { name: 'Review workspace access' }));
    await screen.findByText(/Binding binding-one: ready/);
    await screen.findByText(/Native-origin events: 1/);
    const advanced = screen.getByText('Advanced run settings');
    if (!(advanced.parentElement as HTMLDetailsElement).open) await userEvent.click(advanced);
    if (check) await userEvent.click(screen.getByRole('button', { name: 'Check Investigation readiness' }));
  }

  it('renders native binding observations without treating them as permission to launch work', async () => {
    vi.mocked(getDevOpsBinding).mockResolvedValue({ value: { ...binding, readiness: [
      { kind: 'resource_access', observation: { checked_at: '2026-11-01T00:00:00Z' } },
      { kind: 'native_prevention', observation: { goals: [] } },
      { capability: 'investigation', state: 'blocked' },
    ] }, etag: '"binding-two"' } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding(false);
    expect(screen.getByText(/resource_access; observation only/)).toBeInTheDocument();
    expect(screen.getByText(/native_prevention; observation only/)).toBeInTheDocument();
    expect(screen.getByText(/investigation: blocked; stale or unverified/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start investigation' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Open AWS DevOps Agent' })).toBeDisabled();
  });

  it('checks native access from real observations without synthetic native readiness or automatic launch', async () => {
    vi.mocked(getDevOpsBinding).mockResolvedValue({ value: { ...binding, readiness: [{ kind: 'resource_access', observation: {} }] }, etag: '"binding-two"' } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding(false);
    expect(checkNativeEligibility).not.toHaveBeenCalled();
    expect(launchDevOpsBrowser).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Open AWS DevOps Agent' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Check native UI access' }));
    expect(checkNativeEligibility).toHaveBeenCalledWith('product-one', 'project-one', 'binding-one', 4);
    expect(screen.getByRole('button', { name: 'Open AWS DevOps Agent' })).toBeEnabled();
    expect(launchDevOpsBrowser).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
    await selectBinding(false);
    expect(screen.getByRole('button', { name: 'Open AWS DevOps Agent' })).toBeDisabled();
  });

  it.each(['expired', 'wrong-binding', 'wrong-revision'])('rejects a %s native eligibility response', async mismatch => {
    vi.mocked(checkNativeEligibility).mockImplementation(async () => ({ ...investigation(), capability: 'native_ui',
      ...(mismatch === 'expired' ? { valid_until: new Date(Date.now() - 1_000).toISOString() }
        : mismatch === 'wrong-binding' ? { binding_id: 'foreign' } : { configuration_revision: 5 }) }));
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding(false);
    await userEvent.click(screen.getByRole('button', { name: 'Check native UI access' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Native UI access could not be confirmed');
    expect(screen.getByRole('button', { name: 'Open AWS DevOps Agent' })).toBeDisabled();
    expect(launchDevOpsBrowser).not.toHaveBeenCalled();
  });

  it('checks real Investigation-only binding observations without requesting Prevention authority', async () => {
    const onAccessDenied = vi.fn();
    let resolveReadiness!: (value: Awaited<ReturnType<typeof checkInvestigationReadiness>>) => void;
    vi.mocked(checkInvestigationReadiness).mockReturnValueOnce(new Promise(resolve => { resolveReadiness = resolve; }));
    vi.mocked(getDevOpsBinding).mockResolvedValue({ value: { ...binding, readiness: [
      { kind: 'resource_access', observation: { checked_at: '2026-11-01T00:00:00Z' } },
      { kind: 'native_prevention', observation: { goals: [] } },
    ] }, etag: '"binding-two"' } as never);
    vi.mocked(getDevOpsPrevention).mockRejectedValue({ status: 403, detail: { code: 'delegation_denied' } });
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await selectBinding(false);
    expect(checkInvestigationReadiness).not.toHaveBeenCalled();
    expect(getDevOpsPrevention).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Start investigation' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Check Investigation readiness' }));
    expect(checkInvestigationReadiness).toHaveBeenCalledWith('product-one', 'project-one');
    expect(screen.getByRole('button', { name: 'Start investigation' })).toBeDisabled();
    resolveReadiness(investigation());
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start investigation' })).toBeEnabled());
    expect(screen.getByText(/Ready to start. Access is checked again/)).toBeInTheDocument();
    expect(onAccessDenied).not.toHaveBeenCalled();
  });

  it.each([403, 503])('keeps Investigation readback after optional Prevention fails with %i', async status => {
    const onAccessDenied = vi.fn();
    vi.mocked(getDevOpsPrevention).mockRejectedValue({ status });
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={preventionConfiguration} onAccessDenied={onAccessDenied} />);
    await selectBinding();
    expect(screen.getByText(/Prevention status is unavailable/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start investigation' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'pause prevention' })).not.toBeInTheDocument();
    expect(onAccessDenied).not.toHaveBeenCalled();
  });

  it('submits a question using the saved resource scope without editing JSON', async () => {
    const resource = { ...reference, record_type: 'resource', record_id: 'verified-lambda' };
    const telemetry = { ...reference, record_type: 'telemetry', record_id: 'verified-log-group' };
    const current = JSON.parse(JSON.stringify(configuration));
    current.value.settings.devops.trigger_policies = [{ kind: 'manual', enabled: true, capability: 'investigation', binding_id: 'binding-one',
      resource_refs: [resource], telemetry_refs: [telemetry] }];
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={current} onAccessDenied={vi.fn()} />);
    await selectBinding();
    fireEvent.change(screen.getByLabelText('Investigation question'), { target: { value: 'Why did this Lambda fail?' } });
    expect(screen.queryByRole('button', { name: 'Use saved workload' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Selected Investigation resources')).toHaveTextContent('verified-lambda');
    expect(screen.getByLabelText('Selected Investigation resources')).toHaveTextContent('verified-log-group');
    expect(submitDevOpsRun).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Start investigation' }));
    expect(submitDevOpsRun).toHaveBeenCalledWith('product-one', 'project-one', {
      service_id: 'devops', configuration_revision: 4, binding_id: 'binding-one', capability: 'investigation',
      devops_input: { kind: 'investigation', question: 'Why did this Lambda fail?', resource_refs: [resource], telemetry_refs: [telemetry] },
    }, expect.any(String));
  });

  it.each(['wrong-binding', 'wrong-project', 'ambiguous'])('does not offer a saved scope with %s', async mismatch => {
    const current = JSON.parse(JSON.stringify(configuration));
    const trigger = { kind: 'manual', enabled: true, capability: 'investigation', binding_id: mismatch === 'wrong-binding' ? 'binding-other' : 'binding-one',
      resource_refs: [{ ...reference, record_type: 'resource', project_id: mismatch === 'wrong-project' ? 'another-project' : 'project-one' }], telemetry_refs: [] };
    current.value.settings.devops.trigger_policies = mismatch === 'ambiguous' ? [trigger, trigger] : [trigger];
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={current} onAccessDenied={vi.fn()} />);
    await selectBinding();
    expect(screen.getByRole('button', { name: 'Use saved workload' })).toBeDisabled();
    expect(screen.getByText(/No single enabled Investigation scope is saved/)).toBeInTheDocument();
    expect(submitDevOpsRun).not.toHaveBeenCalled();
  });

  it.each([
    ['delegation_denied', 'The Product owner must review and grant execution consent'],
    ['resource_readback_expired', 'Read the verified workload references in Setup'],
    ['execution_policy_unavailable', 'Review the saved investigation time limits'],
    ['work_queue_unavailable', 'Ask the platform administrator to restore the DevOps worker'],
    ['integration_not_ready', 'Review the selected integration and its current provider access in Setup'],
  ])('shows an action for readiness gap %s', async (code, action) => {
    vi.mocked(checkInvestigationReadiness).mockResolvedValue({ ...investigation(), ready: false, gaps: [code] });
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding();
    expect(screen.getByText(new RegExp(action))).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start investigation' })).toBeDisabled();
    expect(submitDevOpsRun).not.toHaveBeenCalled();
  });

  it.each([
    { binding_generation: 3 }, { binding_revision: 3 }, { binding_id: 'another-binding' },
    { configuration_revision: 5 }, { configuration_hash: 'another-hash' },
    { valid_until: '2000-01-01T00:00:00Z' }, { valid_until: '2100-01-01T00:00:00Z' },
    { scope: { ...scope, project_id: 'another-project' } },
  ])('rejects a readiness snapshot with changed identity or expiry: %j', async change => {
    vi.mocked(checkInvestigationReadiness).mockResolvedValue({ ...investigation(), ...change });
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding();
    expect(screen.getByRole('alert')).toHaveTextContent('readiness could not be confirmed');
    expect(screen.getByRole('button', { name: 'Start investigation' })).toBeDisabled();
  });

  it('invalidates a successful readiness check when its next check fails', async () => {
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding();
    expect(screen.getByRole('button', { name: 'Start investigation' })).toBeEnabled();
    vi.mocked(checkInvestigationReadiness).mockRejectedValueOnce({ status: 409, detail: { code: 'resource_readback_expired' } });
    await userEvent.click(screen.getByRole('button', { name: 'Check Investigation readiness' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Read the verified workload references in Setup');
    expect(screen.getByRole('button', { name: 'Start investigation' })).toBeDisabled();
  });

  it('preserves an investigation draft and explains consent revoked after readiness', async () => {
    const onAccessDenied = vi.fn();
    vi.mocked(submitDevOpsRun).mockRejectedValueOnce({ status: 403, detail: { code: 'delegation_denied' } });
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await selectBinding();
    const value = JSON.stringify({ kind: 'investigation', question: 'Keep this question', resource_refs: [reference], telemetry_refs: [] });
    fireEvent.change(screen.getByLabelText('Typed run input (JSON)'), { target: { value } });
    await userEvent.click(screen.getByRole('button', { name: 'Start investigation' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The Product owner must review and grant execution consent');
    expect(screen.getByLabelText('Typed run input (JSON)')).toHaveValue(value);
    expect(screen.getByRole('button', { name: 'Start investigation' })).toBeDisabled();
    expect(onAccessDenied).not.toHaveBeenCalled();
  });

  it.each([0, 50])('requires current capability readback and scopes manual runs, binding checks and prevention (binding delay: %i ms)', async (bindingDelay) => {
    vi.mocked(listDevOpsBindings).mockImplementationOnce(async () => {
      await new Promise(resolve => setTimeout(resolve, bindingDelay));
      return { items: [binding] } as never;
    });
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={preventionConfiguration} onAccessDenied={vi.fn()} />);
    await selectBinding();
    expect(screen.getByText(/prevention_readback_missing/)).toBeInTheDocument();
    expect(screen.getByText(/event-one: reconciled/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Check binding' }));
    await waitFor(() => expect(startDevOpsBindingOperation).toHaveBeenCalledWith('product-one', 'project-one', {
      action: 'check', binding_id: 'binding-one', configuration_revision: 4,
    }, '"binding-two"', expect.any(String)));
    await selectBinding();
    fireEvent.change(screen.getByLabelText('Typed run input (JSON)'), { target: { value: JSON.stringify({ kind: 'investigation', question: 'Check this workload', resource_refs: [reference], telemetry_refs: [] }) } });
    await userEvent.click(screen.getByRole('button', { name: 'Start investigation' }));
    await waitFor(() => expect(submitDevOpsRun).toHaveBeenCalledWith('product-one', 'project-one', {
      service_id: 'devops', configuration_revision: 4, binding_id: 'binding-one', capability: 'investigation',
      devops_input: { kind: 'investigation', question: 'Check this workload', resource_refs: [reference], telemetry_refs: [] },
    }, expect.any(String)));
    await userEvent.click(screen.getByRole('button', { name: 'pause prevention' }));
    await waitFor(() => expect(configureDevOpsPrevention).toHaveBeenCalledWith('product-one', 'project-one', 'binding-one', 4, 'pause', '"binding-two"'));
  });

  it('does not launch without current native readiness and never renders a returned session URL', async () => {
    const staleBinding = { ...binding, readiness: binding.readiness.map(item => ({ ...item, valid_until: '2000-01-01T00:00:00Z' })) };
    vi.mocked(getDevOpsBinding).mockResolvedValue({ value: staleBinding, etag: '"two"' } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding(false);
    expect(screen.getByRole('button', { name: 'Open AWS DevOps Agent' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Start investigation' })).toBeEnabled();
    expect(launchDevOpsBrowser).not.toHaveBeenCalled();
    expect(screen.queryByText(/SigninToken/)).not.toBeInTheDocument();
  });

  it('shows results, native origin, finding and publication readback without claiming completion', async () => {
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await screen.findByRole('listitem', { name: /Result result-one: partial coverage; origin native/ });
    await userEvent.click(screen.getByRole('button', { name: 'View result' }));
    expect(await screen.findByText(/Needs review: candidate/)).toBeInTheDocument();
    expect(screen.getByText(/Context publication publication-one: retry_wait/)).toBeInTheDocument();
  });

  it.each(['success', 'denial'])('ignores a delayed publication retry %s after selecting another result', async outcome => {
    const next = { ...result, result_id: 'result-two', publication_id: 'publication-two' };
    vi.mocked(listDevOpsResults).mockResolvedValue({ items: [result, next] } as never);
    vi.mocked(getDevOpsPublication).mockImplementation(async (_product, _project, id) => ({ publication_id: id,
      result_id: id === 'publication-one' ? 'result-one' : 'result-two', state: 'retry_wait' }) as never);
    let resolve!: (value: Awaited<ReturnType<typeof retryDevOpsPublication>>) => void;
    let reject!: (error: unknown) => void;
    const pending = new Promise<Awaited<ReturnType<typeof retryDevOpsPublication>>>((done, fail) => { resolve = done; reject = fail; });
    vi.mocked(retryDevOpsPublication).mockReturnValue(pending);
    const onAccessDenied = vi.fn();
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    const view = await screen.findAllByRole('button', { name: 'View result' });
    await userEvent.click(view[0]);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry publication' }));
    expect(retryDevOpsPublication).toHaveBeenCalledWith('product-one', 'project-one', 'publication-one', 'Retry scoped Context publication');
    await userEvent.click(view[1]);
    await screen.findByText(/Context publication publication-two: retry_wait/);
    await act(async () => {
      if (outcome === 'success') resolve({ publication_id: 'publication-one', result_id: 'result-one', state: 'published' } as never);
      else reject({ status: 403 });
      await pending.catch(() => undefined);
    });
    expect(screen.getByText(/Context publication publication-two: retry_wait/)).toBeInTheDocument();
    expect(screen.queryByText(/Context publication publication-one/)).not.toBeInTheDocument();
    expect(onAccessDenied).not.toHaveBeenCalled();
  });

  it.each(['success', 'denial'])('ignores a delayed run refresh %s after changing project scope', async outcome => {
    let resolve!: (value: Awaited<ReturnType<typeof getDevOpsRun>>) => void;
    let reject!: (error: unknown) => void;
    const pending = new Promise<Awaited<ReturnType<typeof getDevOpsRun>>>((done, fail) => { resolve = done; reject = fail; });
    vi.mocked(getDevOpsRun).mockReturnValue(pending);
    const onAccessDenied = vi.fn();
    const { rerender } = render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Refresh run' }));
    const next = JSON.parse(JSON.stringify(configuration));
    next.value.scope = { ...scope, project_id: 'project-two' };
    vi.mocked(listDevOpsBindings).mockResolvedValue({ items: [] } as never);
    vi.mocked(listDevOpsResults).mockResolvedValue({ items: [] } as never);
    vi.mocked(listDevOpsRuns).mockResolvedValue({ items: [{ ...run, scope: next.value.scope, state: 'running' }] } as never);
    rerender(<DevOpsOperations productId="product-one" projectId="project-two" configuration={next} onAccessDenied={onAccessDenied} />);
    await screen.findByRole('listitem', { name: /Run run-one: running/ });
    await act(async () => {
      if (outcome === 'success') resolve({ value: { ...run, state: 'succeeded', revision: 2 }, etag: '"2"' } as never);
      else reject({ status: 403 });
      await pending.catch(() => undefined);
    });
    expect(screen.getByRole('listitem', { name: /Run run-one: running/ })).toBeInTheDocument();
    expect(screen.queryByRole('listitem', { name: /Run run-one: succeeded/ })).not.toBeInTheDocument();
    expect(onAccessDenied).not.toHaveBeenCalled();
  });

  it.each(['publication_id', 'result_id'])('rejects a publication retry with a different %s', async field => {
    vi.mocked(retryDevOpsPublication).mockResolvedValue({ publication_id: 'publication-one', result_id: 'result-one', state: 'published', [field]: 'foreign' } as never);
    const onAccessDenied = vi.fn();
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await userEvent.click(await screen.findByRole('button', { name: 'View result' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Retry publication' }));
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledOnce());
    expect(screen.queryByText(/Context publication/)).not.toBeInTheDocument();
    expect(screen.queryByText(/foreign/)).not.toBeInTheDocument();
  });

  it('rejects a run refresh for a different run identity', async () => {
    vi.mocked(getDevOpsRun).mockResolvedValue({ value: { ...run, run_id: 'another-run' }, etag: '"1"' } as never);
    const onAccessDenied = vi.fn();
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Refresh run' }));
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledOnce());
    expect(screen.queryByText(/Run another-run/)).not.toBeInTheDocument();
  });

  it('separates setup, capability readiness, execution and evidence without losing run edits', async () => {
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding();
    const setup = within(screen.getByRole('region', { name: 'DevOps setup' }));
    const capabilities = within(screen.getByRole('region', { name: 'DevOps capabilities' }));
    const runs = within(screen.getByRole('region', { name: 'DevOps runs and schedules' }));
    const results = within(screen.getByRole('region', { name: 'DevOps results' }));
    expect(setup.getByRole('button', { name: 'Check binding' })).toBeInTheDocument();
    expect(capabilities.getByText(/investigation: ready/)).toBeInTheDocument();
    expect(runs.getByRole('listitem', { name: /Run run-one: admitted/ })).toBeInTheDocument();
    expect(results.getByRole('listitem', { name: /Result result-one: partial coverage/ })).toBeInTheDocument();
    expect(results.getByText(/event-one: reconciled/)).toBeInTheDocument();
    expect(setup.queryByRole('button', { name: 'Start investigation' })).not.toBeInTheDocument();
    await userEvent.click(runs.getByText('Advanced run settings'));
    const input = runs.getByLabelText('Typed run input (JSON)');
    fireEvent.change(input, { target: { value: '{"kind":"investigation","question":"Preserve this draft"}' } });
    await userEvent.click(runs.getByText('Advanced run settings'));
    await userEvent.click(runs.getByText('Advanced run settings'));
    expect(runs.getByLabelText('Typed run input (JSON)')).toBe(input);
    expect(input).toHaveValue('{"kind":"investigation","question":"Preserve this draft"}');
    expect(submitDevOpsRun).not.toHaveBeenCalled();
  });

  it('offers only investigations and rejects release work from advanced input despite old readiness', async () => {
    vi.mocked(getDevOpsBinding).mockResolvedValue({ value: { ...binding, readiness: [...binding.readiness,
      { ...binding.readiness[0], capability: 'release_verification' }] }, etag: '"binding-two"' } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={preventionConfiguration} onAccessDenied={vi.fn()} />);
    await selectBinding();
    expect(screen.queryByLabelText('Run capability')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'enable prevention' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'resume prevention' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Typed run input (JSON)'), { target: { value: JSON.stringify({ kind: 'release_verification', resource_refs: [reference] }) } });
    const submit = screen.getByRole('button', { name: 'Start investigation' });
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    expect(submitDevOpsRun).not.toHaveBeenCalled();
    expect(screen.getByRole('listitem', { name: /Result result-one: partial coverage/ })).toBeInTheDocument();
  });


  it('allows requesting setup for a disabled Investigation draft and explains missing owner consent', async () => {
    vi.mocked(startDevOpsBindingOperation).mockRejectedValueOnce({ status: 403, detail: { code: 'delegation_denied' } });
    const onAccessDenied = vi.fn();
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={setupConfiguration} onAccessDenied={onAccessDenied} />);
    await screen.findByText(/1 saved workspace/);
    expect(screen.getByRole('button', { name: 'Create provider binding' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Create provider binding' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('current owner authorization for this exact saved configuration');
    expect(onAccessDenied).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Retry original receipt' })).not.toBeInTheDocument();
    expect(screen.getByRole('listitem', { name: /Result result-one: partial coverage/ })).toBeInTheDocument();
  });

  it('does not request new-space setup for an active configuration or a draft with enabled integrations', async () => {
    const current = setupConfiguration as unknown as { value: { settings: { devops: object } }; etag: string };
    const view = render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={{ ...current, value: { ...current.value, settings: { ...current.value.settings, enabled: true } } } as never} onAccessDenied={vi.fn()} />);
    await screen.findByText(/1 saved workspace/);
    expect(screen.queryByRole('button', { name: 'Create provider binding' })).not.toBeInTheDocument();
    view.rerender(<DevOpsOperations productId="product-one" projectId="project-one" configuration={{ ...current, value: { ...current.value, settings: { ...current.value.settings, devops: { ...current.value.settings.devops, integrations: [{ kind: 'cloudwatch' }] } } } } as never} onAccessDenied={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Create provider binding' })).toBeDisabled();
    expect(startDevOpsBindingOperation).not.toHaveBeenCalled();
  });

  it('replays the same receipt after an uncertain binding create, not a second paid request', async () => {
    vi.mocked(startDevOpsBindingOperation).mockRejectedValueOnce(new Error('Connection lost'));
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={setupConfiguration} onAccessDenied={vi.fn()} />);
    await screen.findByText(/1 saved workspace/);
    await userEvent.click(screen.getByRole('button', { name: 'Create provider binding' }));
    const original = vi.mocked(startDevOpsBindingOperation).mock.calls[0];
    await userEvent.click(await screen.findByRole('button', { name: 'Retry original receipt' }));
    await waitFor(() => expect(startDevOpsBindingOperation).toHaveBeenCalledTimes(2));
    expect(vi.mocked(startDevOpsBindingOperation).mock.calls[1]).toEqual(original);
  });

  it('clears stale bindings and results when refresh fails without leaving provider controls enabled', async () => {
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding();
    vi.mocked(listDevOpsRuns).mockRejectedValueOnce({ status: 503 });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('readback is unavailable');
    expect(screen.queryByRole('button', { name: 'Create provider binding' })).not.toBeInTheDocument();
    expect(screen.queryByRole('listitem', { name: /Result result-one: partial/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Binding binding-one: ready/)).not.toBeInTheDocument();
    expect(submitDevOpsRun).not.toHaveBeenCalled();
  });

  it('redacts a denied launch and does not print a secret or the returned session URL', async () => {
    const onAccessDenied = vi.fn();
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await selectBinding();
    await userEvent.click(screen.getByRole('button', { name: 'Check native UI access' }));
    await userEvent.click(screen.getByRole('button', { name: 'Open AWS DevOps Agent' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Native UI access could not be confirmed');
    expect(onAccessDenied).not.toHaveBeenCalled();
    expect(screen.getByText(/Binding binding-one: ready/)).toBeInTheDocument();
    expect(screen.queryByText(/SigninToken/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open AWS DevOps Agent' })).toBeDisabled();
  });

  it('rejects an invalid returned destination without displaying the ticket', async () => {
    vi.mocked(launchDevOpsBrowser).mockResolvedValue({ binding_id: 'binding-one', launch_url: 'https://example.invalid/?SigninToken=private-ticket', expires_at: '2100-01-01T00:00:00Z' });
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding();
    await userEvent.click(screen.getByRole('button', { name: 'Check native UI access' }));
    await userEvent.click(screen.getByRole('button', { name: 'Open AWS DevOps Agent' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Native UI access could not be confirmed');
    expect(screen.queryByText(/private-ticket/)).not.toBeInTheDocument();
  });
  async function attachEvidence(subject: 'native_ui' | 'run_submission') {
    vi.mocked(apiClient.get).mockResolvedValue({ service_id: 'devops', challenge_id: 'challenge-original', snapshot_sha256: 'a'.repeat(64), subject,
      expires_at: '2100-01-01T00:00:00Z', observation_receipt: null,
      snapshot: { selection: { product_id: 'product-one', project_id: 'project-one', binding_id: 'binding-one', configuration_id: 'config-one', configuration_revision: 4 } } });
    fireEvent.click(screen.getByText('Release qualification observation'));
    fireEvent.change(screen.getByLabelText('Delivery flow ID'), { target: { value: 'flow' } });
    fireEvent.change(screen.getByLabelText('Prepared observation ID'), { target: { value: 'challenge-original' } });
    fireEvent.change(screen.getByLabelText('Original UI run receipt'), { target: { value: 'original-ui-receipt-0001' } });
    fireEvent.click(screen.getByRole('button', { name: 'Attach prepared observation' }));
    await screen.findByRole('button', { name: subject === 'native_ui' ? 'Record my native DevOps arrival' : 'Record that I submitted this run in ADP' });
  }

  it('preserves the original observed submission and provides no uncertain replay button', async () => {
    vi.mocked(submitDevOpsRun).mockRejectedValueOnce(new Error('Lost response'));
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding(); await attachEvidence('run_submission');
    fireEvent.change(screen.getByLabelText('Typed run input (JSON)'), { target: { value: JSON.stringify({ kind: 'investigation', question: 'Check this workload', resource_refs: [reference], telemetry_refs: [] }) } });
    await userEvent.click(screen.getByRole('button', { name: 'Start investigation' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Do not repeat an uncertain UI submission');
    expect(submitDevOpsRun).toHaveBeenCalledWith('product-one', 'project-one', expect.any(Object), 'original-ui-receipt-0001', 'challenge-original');
    expect(screen.queryByRole('button', { name: 'Retry original receipt' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start investigation' })).toBeDisabled();
    expect(submitDevOpsRun).toHaveBeenCalledTimes(1);
  });

  it('does not repeat an uncertain observed native launch', async () => {
    vi.mocked(launchDevOpsBrowser).mockRejectedValue(new Error('Lost response'));
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding(); await attachEvidence('native_ui');
    await userEvent.click(screen.getByRole('button', { name: 'Check native UI access' }));
    await userEvent.click(screen.getByRole('button', { name: 'Open AWS DevOps Agent' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Do not repeat an uncertain SSO launch');
    await userEvent.click(screen.getByRole('button', { name: 'Check native UI access' }));
    await userEvent.click(screen.getByRole('button', { name: 'Open AWS DevOps Agent' }));
    expect(launchDevOpsBrowser).toHaveBeenCalledTimes(1);
    expect(launchDevOpsBrowser).toHaveBeenCalledWith('product-one', 'project-one', 'binding-one', 4, 'challenge-original');
    expect(screen.getByRole('button', { name: 'Record my native DevOps arrival' })).toBeDisabled();
  });

  it('opens the retained issued launch on a human click without requesting another federation', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    const launchUrl = 'https://signin.aws.amazon.com/federation?SigninToken=private-ticket';
    vi.mocked(launchDevOpsBrowser).mockResolvedValue({ binding_id: 'binding-one', launch_url: launchUrl, expires_at: '2100-01-01T00:00:00Z', observation_receipt_id: 'issued' });
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await selectBinding(); await attachEvidence('native_ui');
    await userEvent.click(screen.getByRole('button', { name: 'Check native UI access' }));
    await userEvent.click(screen.getByRole('button', { name: 'Open AWS DevOps Agent' }));
    const continueLaunch = await screen.findByRole('button', { name: 'Continue with issued SSO launch' });
    expect(open).not.toHaveBeenCalled();
    await userEvent.click(continueLaunch);
    expect(open).toHaveBeenCalledWith(launchUrl, '_blank', 'noopener,noreferrer');
    expect(launchDevOpsBrowser).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/private-ticket/)).not.toBeInTheDocument();
    open.mockRestore();
  });

  it('appends all history pages without duplicates or replacing a newer run readback', async () => {
    vi.mocked(listDevOpsRuns).mockResolvedValueOnce({ items: [run], next_cursor: 'run-page' } as never)
      .mockResolvedValueOnce({ items: [run, { ...run, run_id: 'run-two' }] } as never);
    vi.mocked(listDevOpsResults).mockResolvedValueOnce({ items: [result], next_cursor: 'result-page' } as never)
      .mockResolvedValueOnce({ items: [result, { ...result, result_id: 'result-two' }] } as never);
    vi.mocked(getDevOpsRun).mockResolvedValue({ value: { ...run, revision: 3, state: 'running' } } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Refresh run' }));
    await screen.findByRole('listitem', { name: /Run run-one: running/ });
    await userEvent.click(screen.getByRole('button', { name: 'View result' }));
    await screen.findByRole('region', { name: /Result result-one: 1 findings/ });
    await userEvent.click(screen.getByRole('button', { name: 'Load more DevOps runs' }));
    await userEvent.click(screen.getByRole('button', { name: 'Load more DevOps results' }));
    await screen.findByRole('listitem', { name: /Run run-two:/ });
    await screen.findByRole('listitem', { name: /Result result-two:/ });
    expect(listDevOpsRuns).toHaveBeenLastCalledWith('product-one', 'project-one', 'run-page');
    expect(listDevOpsResults).toHaveBeenLastCalledWith('product-one', 'project-one', 'result-page');
    expect(screen.getAllByRole('listitem', { name: /Run run-one:/ })).toHaveLength(1);
    expect(screen.getByRole('listitem', { name: /Run run-one: running/ })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /Result result-one: 1 findings/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load more DevOps/ })).not.toBeInTheDocument();
  });

  it('appends findings for the selected result and preserves its publication', async () => {
    const finding = { finding_id: 'finding-one', claim: 'First finding', validation: 'candidate', source_refs: [] };
    vi.mocked(listDevOpsFindings).mockResolvedValueOnce({ items: [finding], next_cursor: 'finding-page' } as never)
      .mockResolvedValueOnce({ items: [finding, { ...finding, finding_id: 'finding-two', claim: 'Second finding' }] } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'View result' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Load more DevOps findings' }));
    await screen.findByRole('region', { name: /Result result-one: 2 findings/ });
    expect(listDevOpsFindings).toHaveBeenLastCalledWith('product-one', 'project-one', 'result-one', 'finding-page');
    expect(screen.getAllByText('First finding: candidate')).toHaveLength(1);
    expect(screen.getByText(/Context publication publication-one: retry_wait/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more DevOps findings' })).not.toBeInTheDocument();
  });

  it.each(['runs', 'results', 'findings'] as const)('clears private data and cursors when %s pagination loses access', async kind => {
    const onAccessDenied = vi.fn();
    vi.mocked(listDevOpsRuns).mockResolvedValue({ items: [run], next_cursor: 'run-page' } as never);
    vi.mocked(listDevOpsResults).mockResolvedValue({ items: [result], next_cursor: 'result-page' } as never);
    vi.mocked(listDevOpsFindings).mockResolvedValue({ items: [], next_cursor: 'finding-page' } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await userEvent.click(await screen.findByRole('button', { name: 'View result' }));
    await screen.findByRole('region', { name: /^Result result-one$/ });
    const list = kind === 'runs' ? listDevOpsRuns : kind === 'results' ? listDevOpsResults : listDevOpsFindings;
    vi.mocked(list).mockRejectedValueOnce({ status: 404 });
    await userEvent.click(screen.getByRole('button', { name: `Load more DevOps ${kind}` }));
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('listitem', { name: /Run run-one:/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('listitem', { name: /Result result-one:/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load more DevOps/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh status' })).toBeEnabled();
  });

  it.each(['runs', 'results', 'findings'] as const)('rejects a foreign scope on the %s page', async kind => {
    const onAccessDenied = vi.fn();
    vi.mocked(listDevOpsRuns).mockResolvedValueOnce({ items: [run], next_cursor: 'next' } as never)
      .mockResolvedValueOnce({ items: [{ ...run, scope: { ...scope, tenant_id: 'other' } }] } as never);
    vi.mocked(listDevOpsResults).mockResolvedValueOnce({ items: [result], next_cursor: 'next' } as never)
      .mockResolvedValueOnce({ items: [{ ...result, scope: { ...scope, relationship_revision: 'old' } }] } as never);
    vi.mocked(listDevOpsFindings).mockResolvedValueOnce({ items: [], next_cursor: 'next' } as never)
      .mockResolvedValueOnce({ items: [{ finding_id: 'foreign', claim: 'Foreign finding', source_refs: [{ ...reference, project_id: 'other' }] }] } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await userEvent.click(await screen.findByRole('button', { name: 'View result' }));
    await screen.findByRole('region', { name: /^Result result-one$/ });
    await userEvent.click(screen.getByRole('button', { name: `Load more DevOps ${kind}` }));
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/Foreign finding/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load more DevOps/ })).not.toBeInTheDocument();
  });

  it.each(['success', 'denied'] as const)('ignores late findings page %s after selecting another result', async outcome => {
    let finish!: (value: never) => void;
    let reject!: (reason: unknown) => void;
    const onAccessDenied = vi.fn();
    vi.mocked(listDevOpsResults).mockResolvedValue({ items: [result, { ...result, result_id: 'result-two', publication_id: undefined }] } as never);
    vi.mocked(listDevOpsFindings).mockResolvedValueOnce({ items: [], next_cursor: 'old-page' } as never)
      .mockImplementationOnce(() => new Promise((resolve, fail) => { finish = resolve; reject = fail; }))
      .mockResolvedValueOnce({ items: [], next_cursor: 'new-page' } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'View result' })).toHaveLength(2));
    await userEvent.click(screen.getAllByRole('button', { name: 'View result' })[0]);
    await userEvent.click(await screen.findByRole('button', { name: 'Load more DevOps findings' }));
    await userEvent.click(screen.getAllByRole('button', { name: 'View result' })[1]);
    await screen.findByRole('region', { name: /^Result result-two$/ });
    await act(async () => {
      if (outcome === 'denied') reject({ status: 403 });
      else finish({ items: [{ finding_id: 'stale', claim: 'Stale finding', source_refs: [] }] } as never);
    });
    expect(onAccessDenied).not.toHaveBeenCalled();
    expect(screen.queryByText(/Stale finding/)).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: /^Result result-two$/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Load more DevOps findings' })).toBeEnabled();
  });

  it.each(['success', 'denied'] as const)('ignores a late run page %s after a refresh', async outcome => {
    let finish!: (value: never) => void;
    let reject!: (reason: unknown) => void;
    const onAccessDenied = vi.fn();
    vi.mocked(listDevOpsRuns).mockResolvedValueOnce({ items: [run], next_cursor: 'old-page' } as never)
      .mockImplementationOnce(() => new Promise((resolve, fail) => { finish = resolve; reject = fail; }))
      .mockResolvedValueOnce({ items: [{ ...run, run_id: 'current-run' }] } as never);
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Load more DevOps runs' }));
    await userEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
    await screen.findByRole('listitem', { name: /Run current-run:/ });
    await act(async () => {
      if (outcome === 'denied') reject({ status: 403 });
      else finish({ items: [{ ...run, run_id: 'stale-run' }], next_cursor: 'stale-next' } as never);
    });
    expect(onAccessDenied).not.toHaveBeenCalled();
    expect(screen.queryByRole('listitem', { name: /Run stale-run:/ })).not.toBeInTheDocument();
    expect(screen.getByRole('listitem', { name: /Run current-run:/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more DevOps runs' })).not.toBeInTheDocument();
  });

  it('offers an explicit retained report read despite a publication outage', async () => {
    vi.mocked(listDevOpsResults).mockResolvedValue({ items: [{ ...result, evidence_ref: { artifact_id: 'report-one', version: 'v1', sha256: 'a'.repeat(64), media_type: 'application/json', bytes: 42 } }] } as never);
    vi.mocked(listDevOpsFindings).mockResolvedValue({ items: [] });
    vi.mocked(getDevOpsPublication).mockRejectedValueOnce({ status: 503 });
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'View result' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Retained findings and report access remain below');
    expect(screen.getByRole('button', { name: 'Read investigation report' })).toBeEnabled();
    expect(readDevOpsInvestigationReport).not.toHaveBeenCalled();
    expect(screen.getByRole('region', { name: /^Result result-one$/ })).toBeInTheDocument();
  });

  it('clears all private DevOps readbacks when the report loses authority', async () => {
    const onAccessDenied = vi.fn();
    vi.mocked(listDevOpsResults).mockResolvedValue({ items: [{ ...result, evidence_ref: { artifact_id: 'report-one', version: 'v1', sha256: 'a'.repeat(64), media_type: 'application/json', bytes: 42 } }] } as never);
    vi.mocked(readDevOpsInvestigationReport).mockRejectedValueOnce({ status: 403 });
    render(<DevOpsOperations productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await userEvent.click(await screen.findByRole('button', { name: 'View result' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Read investigation report' }));
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('listitem', { name: /Run run-one:|Result result-one:/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Read investigation report' })).not.toBeInTheDocument();
  });

});
