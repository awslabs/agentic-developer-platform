import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mutateSecurityRun, submitSecurityRun, type SecurityConfiguration, type Versioned } from '@/services/l5Services';
import { useSecurityAdmission, type PendingSecurityAdmission } from '@/pages/products/services/useSecurityAdmission';

vi.mock('@/services/l5Services', async original => ({ ...await original<typeof import('@/services/l5Services')>(), submitSecurityRun: vi.fn(), mutateSecurityRun: vi.fn() }));
const scope = { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '1', owner_id: 'owner-one' };
const configuration = { value: { scope, configuration_id: 'configuration-one', revision: 1, hash: 'hash-one' }, etag: '"hash-one"' } as Versioned<SecurityConfiguration>;
const input = { configuration_revision: 1, binding_id: 'binding-one', capability: 'code_review' as const,
  sources: [{ tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'repository_commit', record_id: 'repo-one', record_revision: 'commit-one' }] };
const request: PendingSecurityAdmission = { kind: 'submit', input, receipt: 'original_receipt_123456' };
const accepted = { value: { scope, run_id: 'run-one', revision: 1, state: 'admitted', provider_state: 'not_started', deadline_at: '2026-11-02T00:00:00Z' }, etag: '"run-one"' };
const preparing = (stage = 'queued', delay: unknown = 5) => ({ status: 503, detail: { code: 'source_capture_preparing', stage, retry_after_seconds: delay,
  message: 'private server detail must not be shown', progress: '<script>unsafe</script>' } });
const key = `adp:security-admission:${JSON.stringify(['tenant-one', 'product-one', 'project-one', 'configuration-one', 'owner-one'])}`;
function mount(overrides = {}) {
  const props = { productId: 'product-one', projectId: 'project-one', configuration, selection: 'original', onAccepted: vi.fn(), onDenied: vi.fn(), ...overrides };
  return { ...renderHook(current => useSecurityAdmission(current), { initialProps: props }), props };
}
async function start(view: ReturnType<typeof mount>, submission = request) { await act(async () => { void view.result.current.submit(submission); }); }
async function advance(ms = 5000) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }

describe('Security repository preparation continuation', () => {
  beforeEach(() => { vi.useFakeTimers(); vi.resetAllMocks(); sessionStorage.clear(); vi.mocked(submitSecurityRun).mockResolvedValue(accepted); });
  afterEach(() => { vi.useRealTimers(); sessionStorage.clear(); });

  it('uses one exact input and receipt through queued, capturing and finalizing before one admission', async () => {
    vi.mocked(submitSecurityRun).mockRejectedValueOnce(preparing()).mockRejectedValueOnce(preparing('capturing')).mockRejectedValueOnce(preparing('finalizing'));
    const view = mount(); await start(view);
    expect(view.result.current.preparation?.stage).toBe('Waiting for snapshot capture.');
    expect(view.result.current.error).toBe(''); expect(view.result.current.busy).toBe(true);
    await advance(); expect(view.result.current.preparation?.stage).toBe('Capturing the pinned repository revision.');
    await advance(); expect(view.result.current.preparation?.stage).toBe('Verifying and finalizing the snapshot.');
    await advance();
    const calls = vi.mocked(submitSecurityRun).mock.calls;
    expect(calls).toHaveLength(4);
    calls.forEach(call => expect(call).toEqual(calls[0]));
    expect(view.props.onAccepted).toHaveBeenCalledExactlyOnceWith(accepted);
    expect(view.result.current.pending).toBeNull(); expect(view.result.current.busy).toBe(false);
    expect(sessionStorage.getItem(key)).toBeNull();
  });

  it.each([['code_review', { status: 503, detail: { code: 'worker_unavailable' } }], ['threat_model', preparing()]])('does not automatically repeat %s on an unrelated or unsupported preparation response', async (capability, failure) => {
    vi.mocked(submitSecurityRun).mockRejectedValue(failure);
    const view = mount(); await start(view, { ...request, input: { ...input, capability } } as PendingSecurityAdmission);
    await advance(60_000);
    expect(submitSecurityRun).toHaveBeenCalledTimes(1); expect(view.result.current.pending?.receipt).toBe(request.receipt);
    expect(view.result.current.busy).toBe(false);
    if (capability === 'threat_model') expect(view.result.current.error).toBe('');
  });

  it.each([0, -5, '0', NaN, Infinity])('bounds malformed retry delay %s and ignores arbitrary server progress', async delay => {
    vi.mocked(submitSecurityRun).mockRejectedValueOnce(preparing('unknown-server-data', delay));
    const view = mount(); await start(view);
    expect(view.result.current.preparation).toEqual({ stage: '', paused: false });
    await advance(4999); expect(submitSecurityRun).toHaveBeenCalledTimes(1);
    await advance(1); expect(submitSecurityRun).toHaveBeenCalledTimes(2);
  });

  it.each(['source_capture_failed', 'source_expired', 'source_preparation_unavailable'])('retains the original receipt and gives a fixed action for %s', async code => {
    vi.mocked(submitSecurityRun).mockRejectedValueOnce({ status: 503, detail: { code, cause: 'private provider detail', message: 'private source contents' } });
    const view = mount(); await start(view); await advance(30_000);
    expect(view.result.current.pending).toEqual(request); expect(submitSecurityRun).toHaveBeenCalledTimes(1);
    expect(view.result.current.error).toContain('Refresh assessment status first');
    expect(view.result.current.error).not.toContain('private');
  });

  it('does not render inherited object properties as preparation stages', async () => {
    vi.mocked(submitSecurityRun).mockRejectedValueOnce(preparing('__proto__'));
    const view = mount(); await start(view);
    expect(view.result.current.preparation?.stage).toBe('');
  });

  it.each([
    ['not_found', 'Product membership'], ['connector_unavailable', 'GitHub App installation'],
    ['immutable_source_required', 'intended immutable revision'], ['unsupported_source_snapshot', 'source preflight'],
    ['source_integrity_mismatch', 'snapshot was rejected'], ['source_capture_timed_out', 'GitHub connectivity'],
    ['source_capture_deadline_or_attempt_limit', 'original deadline or retry limit'],
  ])('explains the next action for capture cause %s without repeating raw details', async (cause, action) => {
    vi.mocked(submitSecurityRun).mockRejectedValueOnce({ status: 409,
      detail: { code: 'source_capture_failed', cause, message: 'private source contents' } });
    const view = mount(); await start(view); await advance(30_000);
    expect(view.result.current.error).toContain(action);
    expect(view.result.current.error).not.toContain('private source contents');
    expect(view.result.current.pending).toEqual(request);
    expect(submitSecurityRun).toHaveBeenCalledTimes(1);
  });

  it('stops at ten minutes and retains the original receipt for an explicit continuation', async () => {
    vi.mocked(submitSecurityRun).mockRejectedValue(preparing());
    const view = mount(); await start(view); await advance(600_000);
    expect(view.result.current.busy).toBe(false); expect(view.result.current.preparation?.paused).toBe(true);
    expect(view.result.current.preparation?.explanation).toContain('after ten minutes');
    expect(view.result.current.pending).toEqual(request);
    const count = vi.mocked(submitSecurityRun).mock.calls.length;
    await advance(60_000); expect(submitSecurityRun).toHaveBeenCalledTimes(count);
    vi.mocked(submitSecurityRun).mockResolvedValueOnce(accepted);
    await start(view, view.result.current.pending!);
    expect(vi.mocked(submitSecurityRun).mock.lastCall?.slice(0, 4)).toEqual(['product-one', 'project-one', input, request.receipt]);
  });

  it.each(['selection', 'configuration'])('stops polling when %s changes while retaining the original pending request', async change => {
    vi.mocked(submitSecurityRun).mockRejectedValue(preparing());
    const view = mount(); await start(view);
    const signal = vi.mocked(submitSecurityRun).mock.calls[0][4]!;
    view.rerender({ ...view.props, ...(change === 'selection' ? { selection: 'changed-source' } : { configuration: { ...configuration, value: { ...configuration.value, revision: 2 } } }) });
    await advance(30_000);
    expect(signal.aborted).toBe(true); expect(submitSecurityRun).toHaveBeenCalledTimes(1);
    expect(view.result.current.pending).toEqual(request); expect(view.result.current.busy).toBe(false);
    expect(view.result.current.preparation?.paused).toBe(true);
  });

  it('aborts on unmount and ignores a late acceptance from the old context', async () => {
    let resolve!: (value: typeof accepted) => void;
    vi.mocked(submitSecurityRun).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    const view = mount(); await start(view);
    const signal = vi.mocked(submitSecurityRun).mock.calls[0][4]!;
    view.unmount(); expect(signal.aborted).toBe(true);
    await act(async () => { resolve(accepted); });
    expect(view.props.onAccepted).not.toHaveBeenCalled(); expect(sessionStorage.getItem(key)).not.toBeNull();
  });

  it('restores a scoped receipt after reload and waits for explicit original-request continuation', async () => {
    vi.mocked(submitSecurityRun).mockRejectedValueOnce(preparing());
    const view = mount(); await start(view); view.unmount();
    const reloaded = mount();
    expect(reloaded.result.current.pending).toEqual(request);
    await advance(30_000); expect(submitSecurityRun).toHaveBeenCalledTimes(1);
    await start(reloaded, reloaded.result.current.pending!);
    expect(vi.mocked(submitSecurityRun).mock.lastCall?.slice(0, 4)).toEqual(['product-one', 'project-one', input, request.receipt]);
    const otherOwner = mount({ configuration: { ...configuration, value: { ...configuration.value, scope: { ...scope, owner_id: 'owner-two' } } } });
    expect(otherOwner.result.current.pending).toBeNull();
  });

  it('retains a checked retry receipt and original reason when preparation is pending', async () => {
    vi.mocked(mutateSecurityRun).mockRejectedValueOnce(preparing()).mockResolvedValueOnce(accepted);
    const retry = { kind: 'retry', runId: 'run-original', reason: 'Recover original assessment', receipt: 'retry_original_123456' } as const;
    const view = mount(); await start(view, retry);
    expect(view.result.current.error).toBe(''); expect(view.result.current.pending).toEqual(retry);
    await advance(30_000); expect(mutateSecurityRun).toHaveBeenCalledTimes(1);
    await start(view, view.result.current.pending!);
    expect(vi.mocked(mutateSecurityRun).mock.calls.map(call => call.slice(0, 6))).toEqual(Array(2).fill(['product-one', 'project-one', 'run-original', 'retry', retry.reason, retry.receipt]));
  });

  it('fails closed for a corrupted receipt and clears retained work if access is denied', async () => {
    sessionStorage.setItem(key, '{invalid'); const invalid = mount();
    expect(invalid.result.current.blocked).toBe(true); await start(invalid);
    expect(submitSecurityRun).not.toHaveBeenCalled(); invalid.unmount(); sessionStorage.clear();
    vi.mocked(submitSecurityRun).mockRejectedValueOnce({ status: 403 });
    const denied = mount(); await start(denied);
    expect(denied.props.onDenied).toHaveBeenCalledOnce(); expect(denied.result.current.pending).toBeNull();
    expect(sessionStorage.getItem(key)).toBeNull();
  });
});
