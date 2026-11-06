import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SecurityRuns from '@/pages/products/services/SecurityRuns';
import { apiClient } from '@/services/api';
import { getSecurityBinding, getSecurityConfiguration, getSecurityRun, listSecurityBindings, listSecurityRuns, mutateSecurityRun, submitSecurityRun, type SecurityRun } from '@/services/l5Services';

vi.mock('@/services/api', () => ({ apiClient: { post: vi.fn() } }));

vi.mock('@/services/l5Services', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/l5Services')>(), getSecurityBinding: vi.fn(), getSecurityConfiguration: vi.fn(), getSecurityRun: vi.fn(), listSecurityBindings: vi.fn(), listSecurityRuns: vi.fn(), mutateSecurityRun: vi.fn(), submitSecurityRun: vi.fn() }));

const documentRef = { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'document', record_id: 'security-strategy', record_revision: '17' };
const registration = { kind: 'code_review_source_registration', registration: { service_role: 'arn:aws:iam::111122223333:role/security-assessment', bucket: 'example-security-source' }, observed_at: new Date().toISOString() };
const model = { kind: 'threat_model', native_model_ref: { ...documentRef, record_type: 'threat_model', record_id: 'model-one', record_revision: 'model-revision' }, observed_at: new Date().toISOString() };
const binding = { scope: { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '5' }, binding_id: 'binding-one', configuration_id: 'config-one', revision: 2, generation: 3, state: 'ready', ownership: 'adp_created' as const, connection_ref: documentRef,
  provider_identity: { account_id: '111122223333' }, native_resource_ref: { ...documentRef, record_type: 'security_space', record_id: 'space-one' }, gaps: [], readiness: [registration, model] };
const configuration = { value: { scope: { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '5' }, configuration_id: 'config-one', revision: 4, hash: 'config-four', state: 'ready', settings: { enabled: true, capabilities: ['threat_model'], source_refs: [documentRef], triggers: [
  { trigger_id: 'trigger-one', kind: 'manual', enabled: true, capability: 'threat_model', binding_id: 'binding-one', source_refs: [documentRef], debounce_seconds: 0 },
] } }, etag: '"config-four"' } as never;
const observed: SecurityRun = { scope: { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '5' }, run_id: 'run-one', revision: 3, state: 'partial', provider_state: 'completed', deadline_at: '2026-10-27T13:00:00Z',
  admission: { monitoring_policy: { poll_interval_seconds: 120, poll_grace_seconds: 180 } },
  monitoring: { state: 'degraded', progress_state: 'suspected_stalled', poll_attempts: 3, consecutive_poll_errors: 1,
    last_poll_attempt_at: '2026-10-27T12:05:00Z', last_successful_poll_at: '2026-10-27T12:03:00Z', next_poll_at: '2026-10-27T12:07:00Z',
    latest_observation: { native_status: 'COMPLETED', outcome: 'completed', progress_coverage: 'partial',
      phases: [{ name: 'preflight', native_status: 'COMPLETED' }], tasks: { running: 0, completed: 2, failed: 1, stopped: 0, unknown: 1 } } } };

function mount(current = configuration) { return render(<SecurityRuns productId="product-one" projectId="project-one" configuration={current} />); }

async function checkAccess() {
  const summary = screen.getByText('Check access without starting');
  if (!(summary.parentElement as HTMLDetailsElement).open) await userEvent.click(summary);
  await userEvent.click(screen.getByRole('button', { name: 'Check Security readiness' }));
}
async function selectSource(label: RegExp) {
  const checkbox = screen.getByLabelText(label) as HTMLInputElement;
  if (!checkbox.checked) await userEvent.click(checkbox);
}
async function selectRecordedAssessment() {
  const select = await screen.findByLabelText('Visible assessments');
  // The control mounts before the history request resolves, especially on CI.
  await waitFor(() => expect(select).toHaveTextContent('run-one'));
  await userEvent.selectOptions(select, 'run-one');
}

describe('Security assessment lifecycle', () => {
  afterEach(() => vi.useRealTimers());
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(apiClient.post).mockResolvedValue({ ready: true, gaps: [], provider_contacted: false });
    vi.mocked(getSecurityConfiguration).mockResolvedValue(configuration);
    vi.mocked(getSecurityBinding).mockResolvedValue({ value: binding, etag: '"binding-two"' });
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [binding] });
    vi.mocked(listSecurityRuns).mockResolvedValue({ items: [] });
    vi.mocked(getSecurityRun).mockResolvedValue({ value: observed, etag: '"run-three"' });
    vi.mocked(submitSecurityRun).mockResolvedValue({ value: { ...observed, state: 'admitted', provider_state: 'not_started' }, etag: '"run-one"' });
  });

  it('starts from the saved manual selection and checks access before admission', async () => {
    mount();
    await waitFor(() => expect(screen.getByLabelText('Assessment capability')).toHaveValue('threat_model'));
    await waitFor(() => expect(screen.getByLabelText('Security workspace')).toHaveValue('binding-one'));
    expect(screen.getByLabelText(/security-strategy \(revision 17\)/)).toBeChecked();
    expect(apiClient.post).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Start Security assessment' }));
    expect(apiClient.post).toHaveBeenCalledWith('/v1/products/product-one/projects/project-one/services/security/configuration/check', {});
    expect(getSecurityConfiguration).toHaveBeenCalledWith('product-one', 'project-one');
    expect(getSecurityBinding).toHaveBeenCalledWith('product-one', 'project-one', 'binding-one');
    await waitFor(() => expect(submitSecurityRun).toHaveBeenCalledWith('product-one', 'project-one', {
      configuration_revision: 4, binding_id: 'binding-one', capability: 'threat_model', sources: [documentRef],
    }, expect.any(String), expect.any(AbortSignal)));
    expect(submitSecurityRun).toHaveBeenCalledTimes(1);
    expect(mutateSecurityRun).not.toHaveBeenCalled();
  });

  it('admits code review with fresh source registration and no threat model', async () => {
    const source = { ...documentRef, record_type: 'repository_commit', record_id: 'example-repository', record_revision: 'commit-one' };
    const configured = { value: { ...configuration.value, settings: { ...configuration.value.settings, capabilities: ['code_review'], source_refs: [source], triggers: [
      { trigger_id: 'trigger-code', kind: 'manual', enabled: true, capability: 'code_review', binding_id: 'binding-one', source_refs: [source], debounce_seconds: 0 },
    ] } }, etag: '"config-four"' } as never;
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [{ ...binding, readiness: [registration] }] });
    mount(configured);
    await userEvent.selectOptions(await screen.findByLabelText('Assessment capability'), 'code_review');
    await userEvent.selectOptions(screen.getByLabelText('Security workspace'), 'binding-one');
    await selectSource(/example-repository \(revision commit-one\)/);
    await checkAccess();
    await userEvent.click(screen.getByRole('button', { name: 'Start Security assessment' }));
    await waitFor(() => expect(submitSecurityRun).toHaveBeenCalledWith('product-one', 'project-one', expect.objectContaining({ capability: 'code_review', sources: [source] }), expect.any(String), expect.any(AbortSignal)));
  });

  it('shows preparation progress without a generic failure and keeps original work visible after source edits', async () => {
    const source = { ...documentRef, record_type: 'repository_commit', record_id: 'example-repository', record_revision: 'commit-one' };
    const configured = { value: { ...configuration.value, scope: { ...configuration.value.scope, owner_id: 'owner-one' }, settings: { ...configuration.value.settings, capabilities: ['code_review'], source_refs: [source], triggers: [
      { trigger_id: 'trigger-code', kind: 'manual', enabled: true, capability: 'code_review', binding_id: 'binding-one', source_refs: [source], debounce_seconds: 0 },
    ] } }, etag: '"config-four"' } as never;
    vi.mocked(getSecurityConfiguration).mockResolvedValue(configured);
    const readyBinding = { ...binding, scope: { ...binding.scope, owner_id: 'owner-one' }, readiness: [registration] };
    vi.mocked(getSecurityBinding).mockResolvedValue({ value: readyBinding, etag: '"binding-two"' });
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [readyBinding] });
    vi.mocked(submitSecurityRun).mockRejectedValue({ status: 503, detail: { code: 'source_capture_preparing', stage: 'capturing', retry_after_seconds: 5, message: 'private capture failure text' } });
    const view = mount(configured);
    await userEvent.selectOptions(await screen.findByLabelText('Assessment capability'), 'code_review');
    await userEvent.selectOptions(screen.getByLabelText('Security workspace'), 'binding-one');
    await selectSource(/example-repository \(revision commit-one\)/);
    await checkAccess();
    await userEvent.click(screen.getByRole('button', { name: 'Start Security assessment' }));
    expect(await screen.findByText('Preparing the full repository snapshot…')).toBeInTheDocument();
    expect(screen.getByText('Capturing the pinned repository revision.')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText('private capture failure text')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry original assessment receipt' })).toBeDisabled();
    await userEvent.click(screen.getByLabelText(/example-repository \(revision commit-one\)/));
    expect(screen.getByText(/Automatic checks stopped because the assessment context changed/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Retry original assessment receipt' })).toBeEnabled();
    view.rerender(<SecurityRuns productId="product-other" projectId="project-one" configuration={null} />);
    expect(screen.queryByRole('button', { name: 'Retry original assessment receipt' })).not.toBeInTheDocument();
    sessionStorage.clear();
  });

  async function chooseAssessment() {
    await userEvent.selectOptions(await screen.findByLabelText('Assessment capability'), 'threat_model');
    await userEvent.selectOptions(screen.getByLabelText('Security workspace'), 'binding-one');
    await selectSource(/security-strategy \(revision 17\)/);
  }

  it('keeps the directly confirmed active assessment visible while the history list catches up', async () => {
    vi.mocked(getSecurityRun).mockResolvedValue({ value: { ...observed, state: 'admitted', provider_state: 'not_started' }, etag: '"run-one"' });
    mount(); await chooseAssessment();
    await checkAccess();
    await userEvent.click(screen.getByRole('button', { name: 'Start Security assessment' }));
    expect(await screen.findByRole('option', { name: 'run-one — admitted' })).toBeInTheDocument();
    await checkAccess();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
    expect(screen.getAllByRole('option', { name: 'run-one — admitted' })).toHaveLength(1);
  });

  it.each([
    ['work_queue_unavailable', 'Ask the platform administrator to restore the Security worker'],
    ['execution_delegation', 'The Product owner must review and renew execution consent'],
    ['source_revision_stale', 'Review the pinned source revisions and source registration'],
  ])('blocks a ready binding when the current check reports %s', async (gap, action) => {
    vi.mocked(apiClient.post).mockResolvedValue({ ready: false, gaps: [gap], provider_contacted: false });
    mount(); await chooseAssessment();
    await checkAccess();
    expect(screen.getByText(new RegExp(action))).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
    expect(submitSecurityRun).not.toHaveBeenCalled();
  });

  it.each(['configuration_revision', 'configuration_hash', 'configuration_owner', 'binding_generation', 'binding_revision', 'binding_owner', 'binding_expired'])('rejects changed %s after the readiness check', async change => {
    if (change.startsWith('configuration')) {
      vi.mocked(getSecurityConfiguration).mockResolvedValue({ ...configuration, value: { ...configuration.value,
        ...(change === 'configuration_revision' ? { revision: 5 } : change === 'configuration_hash' ? { hash: 'other-hash' }
          : { scope: { ...configuration.value.scope, owner_id: 'another-owner' } }),
      } } as never);
    } else {
      vi.mocked(getSecurityBinding).mockResolvedValue({ value: { ...binding,
        ...(change === 'binding_generation' ? { generation: 4 } : change === 'binding_revision' ? { revision: 3 }
          : change === 'binding_owner' ? { scope: { ...binding.scope, owner_id: 'another-owner' } }
            : { readiness: [{ ...registration, observed_at: '2000-01-01T00:00:00Z' }, model] }),
      }, etag: '"binding-two"' } as never);
    }
    mount(); await chooseAssessment();
    await checkAccess();
    expect(screen.getByRole('alert')).toHaveTextContent('readiness could not be confirmed');
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
  });

  it('invalidates a successful check after readback fails', async () => {
    mount(); await chooseAssessment();
    await checkAccess();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeEnabled();
    vi.mocked(getSecurityBinding).mockRejectedValueOnce({ status: 503 });
    await checkAccess();
    expect(screen.getByRole('alert')).toHaveTextContent('readiness could not be confirmed');
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
  });

  it.each([
    { ready: true, gaps: ['execution_delegation'], provider_contacted: false },
    { ready: 'ready', gaps: [], provider_contacted: false },
  ])('does not treat an incomplete or contradictory response as readiness', async result => {
    vi.mocked(apiClient.post).mockResolvedValue(result);
    mount(); await chooseAssessment();
    await checkAccess();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('readiness could not be confirmed');
    expect(submitSecurityRun).not.toHaveBeenCalled();
  });

  it('clears an earlier successful check when owner access is denied', async () => {
    const onAccessDenied = vi.fn();
    render(<SecurityRuns productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await chooseAssessment();
    await checkAccess();
    vi.mocked(apiClient.post).mockRejectedValueOnce({ status: 403, detail: { code: 'execution_delegation' } });
    await checkAccess();
    expect(onAccessDenied).toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('The Product owner must review and renew execution consent');
  });

  it('invalidates a successful check on refresh and configuration change', async () => {
    const view = mount(); await chooseAssessment();
    await checkAccess();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Refresh assessment status' }));
    expect(screen.queryByText('Ready to start.')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeEnabled();
    await checkAccess();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeEnabled();
    view.rerender(<SecurityRuns productId="product-one" projectId="project-one" configuration={{ ...configuration, value: { ...configuration.value, revision: 5, hash: 'config-five' } } as never} />);
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
  });

  it('keeps the selection fixed while a readiness request is in flight', async () => {
    let resolve!: (value: unknown) => void;
    vi.mocked(apiClient.post).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    mount(); await chooseAssessment();
    await checkAccess();
    expect(screen.getByLabelText('Security workspace')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
    resolve({ ready: true, gaps: [], provider_contacted: false });
    await waitFor(() => expect(screen.getByLabelText('Security workspace')).toBeEnabled());
    expect(submitSecurityRun).not.toHaveBeenCalled();
  });

  it('requires a new check after the 60-second snapshot expires', async () => {
    mount(); await chooseAssessment();
    await checkAccess();
    const clock = vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 61_000);
    try {
      await userEvent.click(screen.getByRole('button', { name: 'Start Security assessment' }));
      expect(apiClient.post).toHaveBeenCalledTimes(2);
      expect(submitSecurityRun).toHaveBeenCalledOnce();
    } finally { clock.mockRestore(); }
  });

  it('rechecks observation expiry when the user starts an assessment', async () => {
    mount();
    await userEvent.selectOptions(await screen.findByLabelText('Assessment capability'), 'threat_model');
    await userEvent.selectOptions(screen.getByLabelText('Security workspace'), 'binding-one');
    await selectSource(/security-strategy \(revision 17\)/);
    await checkAccess();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeEnabled();
    const clock = vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 16 * 60 * 1000);
    try {
      await userEvent.click(screen.getByRole('button', { name: 'Start Security assessment' }));
      expect(screen.getByRole('alert')).toHaveTextContent('Binding observations expired');
      expect(submitSecurityRun).not.toHaveBeenCalled();
    } finally { clock.mockRestore(); }
  });

  it.each([
    [],
    [registration],
    [registration, { kind: 'threat_model_outcome_unknown' }],
    [{ ...registration, observed_at: '2000-01-01T00:00:00Z' }, model],
    [registration, { ...model, native_model_ref: { ...model.native_model_ref, project_id: 'another-project' } }],
  ])('keeps the saved workspace selectable for recovery without treating stale observations as run access', async (...entries) => {
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [{ ...binding, readiness: entries as never }] });
    mount();
    await userEvent.selectOptions(await screen.findByLabelText('Assessment capability'), 'threat_model');
    await waitFor(() => expect(screen.getByLabelText('Security workspace')).toHaveValue('binding-one'));
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
    expect(screen.getByText(/Workspace access needs a fresh check/)).toBeInTheDocument();
    expect(submitSecurityRun).not.toHaveBeenCalled();
  });

  it('shows incomplete native progress and monitoring advisories separately from the local outcome', async () => {
    vi.mocked(listSecurityRuns).mockResolvedValue({ items: [observed] });
    mount();
    await selectRecordedAssessment();
    expect(await screen.findByText('Local run state: partial')).toBeInTheDocument();
    expect(screen.getByText('Provider state: completed')).toBeInTheDocument();
    expect(screen.getByText(/Monitoring freshness: degraded; poll attempts: 3/)).toBeInTheDocument();
    expect(screen.getByText(/suspected_stalled \(advisory only; no automatic restart\)/)).toBeInTheDocument();
    expect(screen.getByText(/Native job status: COMPLETED. Progress coverage: partial/)).toBeInTheDocument();
    expect(screen.getByText(/preflight: COMPLETED/)).toBeInTheDocument();
    expect(screen.getByText(/running 0, completed 2, failed 1/)).toBeInTheDocument();
    expect(screen.getByText(/last successful check: 2026-10-27T12:03:00Z; next check: 2026-10-27T12:07:00Z/)).toBeInTheDocument();
    expect(screen.getByText(/stall advisory after disabled seconds/)).toBeInTheDocument();
  });

  it('shows the retained recovery action and collapses diagnostic details', async () => {
    const failed: SecurityRun = { ...observed, state: 'failed', provider_state: 'not_started', monitoring: {
      ...observed.monitoring!, last_poll_error: { code: 'threat_model_scope_rejected', stage: 'update_threat_model',
        cause: 'ValidationException', action: 'Correct the scope document and start a new run. No assessment was started.' },
    } };
    vi.mocked(listSecurityRuns).mockResolvedValue({ items: [failed] });
    vi.mocked(getSecurityRun).mockResolvedValue({ value: failed, etag: '"failed"' });
    mount();
    await selectRecordedAssessment();
    expect(await screen.findByRole('alert')).toHaveTextContent('Correct the scope document and start a new run. No assessment was started.');
    expect(screen.getByText('Failure details').closest('details')).not.toHaveAttribute('open');
    expect(screen.getByText(/threat_model_scope_rejected at update_threat_model: ValidationException/)).toBeInTheDocument();
    expect(submitSecurityRun).not.toHaveBeenCalled();
  });

  it('renders the provider task records and status fields returned by the live API', async () => {
    const run: SecurityRun = { ...observed, monitoring: { ...observed.monitoring!, latest_observation: {
      status: 'COMPLETED', outcome: 'completed', progress_coverage: 'complete',
      phases: [{ name: 'PREFLIGHT', status: 'COMPLETED' }, { name: 'STATIC_ANALYSIS', status: 'IN_PROGRESS' }],
      tasks: [
        ...Array.from({ length: 7 }, (_, i) => ({ task_id: `completed-${i}`, status: 'COMPLETED' })),
        { task_id: 'running-one', status: 'IN_PROGRESS' }, { task_id: 'running-two', status: 'IN_PROGRESS' },
        { task_id: 'failed-one', status: 'FAILED' }, { task_id: 'stopped-one', status: 'STOPPED' },
        { task_id: 'unknown-one', status: 'NEW_PROVIDER_STATUS' },
      ],
    } } };
    vi.mocked(listSecurityRuns).mockResolvedValue({ items: [run] });
    vi.mocked(getSecurityRun).mockResolvedValue({ value: run, etag: '"native-progress"' });
    mount();
    await selectRecordedAssessment();
    expect(await screen.findByText('Native job status: COMPLETED. Progress coverage: complete.')).toBeInTheDocument();
    expect(screen.getByText('Provider phases: PREFLIGHT: COMPLETED; STATIC_ANALYSIS: IN_PROGRESS')).toBeInTheDocument();
    expect(screen.getByText('Native tasks: running 2, completed 7, failed 1, stopped 1, unknown 1')).toBeInTheDocument();
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
  });

  async function followRunningAssessment(onAccessDenied?: () => void, selected?: SecurityRun) {
    vi.useFakeTimers();
    const running: SecurityRun = selected ?? { ...observed, state: 'running', provider_state: 'running' };
    vi.mocked(listSecurityRuns).mockResolvedValue({ items: [running] });
    vi.mocked(getSecurityRun).mockResolvedValue({ value: running, etag: '"running"' });
    const view = render(<SecurityRuns productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await act(async () => {});
    await act(async () => { fireEvent.change(screen.getByLabelText('Visible assessments'), { target: { value: 'run-one' } }); });
    expect(screen.getByText(`Local run state: ${running.state}`)).toBeInTheDocument();
    return view;
  }

  it('refreshes active progress automatically, preserves selection and stops after completion', async () => {
    const view = await followRunningAssessment();
    try {
      vi.mocked(listSecurityRuns).mockResolvedValue({ items: [{ ...observed, state: 'succeeded' }] });
      vi.mocked(getSecurityRun).mockResolvedValue({ value: { ...observed, state: 'succeeded' }, etag: '"complete"' });
      const bindingReads = vi.mocked(listSecurityBindings).mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(screen.getByText('Local run state: succeeded')).toBeInTheDocument();
      expect(screen.getByLabelText('Visible assessments')).toHaveValue('run-one');
      const completedReads = vi.mocked(getSecurityRun).mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(getSecurityRun).toHaveBeenCalledTimes(completedReads);
      expect(listSecurityBindings).toHaveBeenCalledTimes(bindingReads);
      expect(apiClient.post).not.toHaveBeenCalled();
      expect(submitSecurityRun).not.toHaveBeenCalled();
      expect(mutateSecurityRun).not.toHaveBeenCalled();
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it('continues refreshing an expired assessment while results are being recovered', async () => {
    const recovering: SecurityRun = { ...observed, state: 'expired', monitoring: { ...observed.monitoring!, state: 'recovery_pending' } };
    const view = await followRunningAssessment(undefined, recovering);
    try {
      expect(screen.getByText(/Checking the existing AWS assessment/)).toBeInTheDocument();
      vi.mocked(getSecurityRun).mockResolvedValue({ value: { ...observed, state: 'succeeded' }, etag: '"recovered"' });
      vi.mocked(listSecurityRuns).mockResolvedValue({ items: [{ ...observed, state: 'succeeded' }] });
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(screen.getByText('Local run state: succeeded')).toBeInTheDocument();
      expect(submitSecurityRun).not.toHaveBeenCalled();
      expect(mutateSecurityRun).not.toHaveBeenCalled();
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it('retains the last confirmed progress on a transient error and recovers on the next automatic read', async () => {
    const view = await followRunningAssessment();
    try {
      vi.mocked(getSecurityRun).mockRejectedValueOnce({ status: 503 });
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(screen.getByText('Local run state: running')).toBeInTheDocument();
      expect(screen.getByRole('status')).toHaveTextContent('Showing the last confirmed status');
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(screen.queryByText(/Progress could not be refreshed/)).not.toBeInTheDocument();
      expect(screen.getByLabelText('Visible assessments')).toHaveValue('run-one');
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it.each(['revoked', 'foreign'])('clears progress and stops automatic reads when access is %s', async kind => {
    const onAccessDenied = vi.fn();
    const view = await followRunningAssessment(onAccessDenied);
    try {
      if (kind === 'revoked') vi.mocked(getSecurityRun).mockRejectedValue({ status: 403 });
      else vi.mocked(getSecurityRun).mockResolvedValue({ value: { ...observed, scope: { ...observed.scope, product_id: 'other-product' } }, etag: '"foreign"' });
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(onAccessDenied).toHaveBeenCalledTimes(1);
      expect(screen.queryByText(/Local run state:/)).not.toBeInTheDocument();
      const reads = vi.mocked(getSecurityRun).mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(getSecurityRun).toHaveBeenCalledTimes(reads);
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it('keeps overdue monitoring and missing native progress separate from an unknown provider outcome', async () => {
    const unknown: SecurityRun = { ...observed, state: 'outcome_unknown', provider_state: 'unknown', monitoring: {
      state: 'overdue', progress_state: 'unknown', poll_attempts: 2, consecutive_poll_errors: 0, next_poll_at: '2026-10-27T12:02:00Z',
    } };
    vi.mocked(listSecurityRuns).mockResolvedValue({ items: [unknown] });
    vi.mocked(getSecurityRun).mockResolvedValue({ value: unknown, etag: '"unknown"' });
    mount();
    await selectRecordedAssessment();
    expect(await screen.findByText('Provider state: unknown')).toBeInTheDocument();
    expect(screen.getByText(/Monitoring freshness: overdue; poll attempts: 2/)).toBeInTheDocument();
    expect(screen.getByText(/Progress evidence: unknown/)).toBeInTheDocument();
    expect(screen.getByText(/Native tasks: Unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/Native job status: Unavailable. Progress coverage: unavailable/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reconcile existing assessment' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
    expect(submitSecurityRun).not.toHaveBeenCalled();
  });

  it('never submits another paid assessment with a new key after an uncertain reply', async () => {
    vi.mocked(submitSecurityRun).mockRejectedValueOnce(new Error('response lost'));
    mount();
    await userEvent.selectOptions(await screen.findByLabelText('Assessment capability'), 'threat_model');
    await userEvent.selectOptions(screen.getByLabelText('Security workspace'), 'binding-one');
    await selectSource(/security-strategy \(revision 17\)/);
    await checkAccess();
    await userEvent.click(screen.getByRole('button', { name: 'Start Security assessment' }));
    expect(await screen.findByRole('button', { name: 'Retry original assessment receipt' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
    const original = vi.mocked(submitSecurityRun).mock.calls[0];
    await userEvent.click(screen.getByRole('button', { name: 'Retry original assessment receipt' }));
    await waitFor(() => expect(submitSecurityRun).toHaveBeenCalledTimes(2));
    expect(vi.mocked(submitSecurityRun).mock.calls[1].slice(0, 4)).toEqual(original.slice(0, 4));
  });

  it('denies admission without a trigger or when source coverage is unknown', async () => {
    const missing = { value: { ...configuration.value, settings: { ...configuration.value.settings, triggers: [], source_refs: [] } }, etag: '"config-five"' } as never;
    mount(missing);
    await userEvent.selectOptions(await screen.findByLabelText('Assessment capability'), 'threat_model');
    await userEvent.selectOptions(screen.getByLabelText('Security workspace'), 'binding-one');
    expect(screen.getByText(/No pinned sources match this capability/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
    expect(submitSecurityRun).not.toHaveBeenCalled();
  });
  it('offers access refresh for a saved workspace without capability readiness', async () => {
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [{ ...binding, readiness: [{ capability: 'threat_model', state: 'blocked', reason_codes: ['source_denied'] }] }] });
    mount();
    await userEvent.selectOptions(await screen.findByLabelText('Assessment capability'), 'threat_model');
    await waitFor(() => expect(screen.getByLabelText('Security workspace')).toHaveValue('binding-one'));
    expect(screen.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
    expect(submitSecurityRun).not.toHaveBeenCalled();
  });
  it('withholds all run choices when a list contains another Product scope', async () => {
    const onAccessDenied = vi.fn();
    vi.mocked(listSecurityRuns).mockResolvedValue({ items: [observed, { ...observed, run_id: 'foreign-run', scope: { ...observed.scope, product_id: 'product-other' } }] });
    render(<SecurityRuns productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalled());
    expect(screen.queryByRole('option', { name: /run-one|foreign-run/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Local run state:/)).not.toBeInTheDocument();
  });

  it('clears run detail and alerts the workspace after source access is revoked', async () => {
    const onAccessDenied = vi.fn();
    vi.mocked(listSecurityRuns).mockResolvedValue({ items: [observed] });
    vi.mocked(getSecurityRun).mockRejectedValueOnce({ status: 403 });
    render(<SecurityRuns productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await selectRecordedAssessment();
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalled());
    expect(screen.queryByRole('option', { name: /run-one/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Local run state:/)).not.toBeInTheDocument();
  });

  it('opens progress from the assessment history without starting or checking a new run', async () => {
    const running = { ...observed, state: 'running', input: { configuration_revision: 4, binding_id: 'binding-one', capability: 'threat_model' as const, sources: [documentRef] } };
    vi.mocked(listSecurityRuns).mockResolvedValue({ items: [running] });
    vi.mocked(getSecurityRun).mockResolvedValue({ value: running, etag: '"run-three"' });
    mount();
    await userEvent.click(await screen.findByRole('button', { name: /Threat modeling.*security-strategy.*running/ }));
    expect(await screen.findByLabelText('Observed task progress')).toHaveTextContent('Tasks completed');
    expect(screen.getByText('Local run state: running')).not.toBeVisible();
    expect(screen.getByLabelText('Assessment capability')).toBeVisible();
    await userEvent.click(screen.getByText('Monitoring details'));
    expect(screen.getByText('Local run state: running')).toBeVisible();
    expect(submitSecurityRun).not.toHaveBeenCalled();
    expect(getSecurityBinding).not.toHaveBeenCalled();
  });

});
