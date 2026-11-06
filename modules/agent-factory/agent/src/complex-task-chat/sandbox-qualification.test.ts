import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { GatewayHistoryStore } from './context/store/gateway-history';
import { executeSandboxQualification } from './sandbox-qualification';
import { qualificationBinding, qualificationDigest, qualificationManifest } from './qualification-contract';
import { QualificationDiagnostics } from './qualification-diagnostics';

jest.mock('./qualification-diagnostics');
jest.mock('./qualification-data', () => ({ ...jest.requireActual('./qualification-data'), qualificationDataProbe: jest.fn(async () => undefined) }));

const now = Date.parse('2026-10-12T12:00:00Z');
const binding = qualificationBinding.parse({
  nonce: 'a'.repeat(64), manifest_digest: qualificationDigest, manifest_version: 'e45-v1',
  evaluation_id: 'evaluation', target: 'test-target', actor: 'a1', tenant_id: 'tenant', user_id: 'owner', team_id: '',
  session_id: 'session-a', run_id: 'run-a', sandbox_uid: 'pod-a', lease_generation: 1,
  image_digest: `sha256:${'b'.repeat(64)}`, attempt: 1, credential_epoch: 1,
  issued_at: now / 1000, expires_at: now / 1000 + 120, limits: qualificationManifest.limits,
});
const fixtures = {
  owner: binding, message_id: 'msg_a', summary_id: 'sum_a', artifact_id: `art_${'a'.repeat(32)}`, memory_id: `mem_${'b'.repeat(32)}`,
  cursor: 'opaque-cursor', history_version: 3, memory_version: 1, cursor_expires_at: now / 1000 + 300,
  bucket: 'test-bucket', object_key: 'test-key', context_table: 'test-context', secret_canary: 'test-canary', region: 'us-east-1',
};

describe('fixed sandbox qualification coordinator', () => {
  let client: ChatDataClient;
  let request: jest.SpiedFunction<ChatDataClient['qualificationRequest']>;
  let submit: jest.SpiedFunction<ChatDataClient['submitTurnResult']>;
  let execute: jest.MockedFunction<QualificationDiagnostics['execute']>;
  let cleanup: jest.MockedFunction<QualificationDiagnostics['cleanup']>;

  beforeEach(() => {
    jest.spyOn(Date, 'now').mockReturnValue(now);
    jest.spyOn(ChatDataClient.prototype, 'qualificationRequired').mockReturnValue(true);
    jest.spyOn(GatewayHistoryStore.prototype, 'acceptedUserTurn').mockResolvedValue({ message_id: 'user-a', ordinal: 1 });
    jest.spyOn(GatewayHistoryStore.prototype, 'getMessagesByIds').mockResolvedValue([
      { role: 'user', content: 'synthetic', ts: new Date(now).toISOString(), tokens: 2 },
    ]);
    jest.spyOn(GatewayHistoryStore.prototype, 'readPage').mockResolvedValue({
      version: 1, entries: [], status: 'empty', next_cursor: null, observed_at: new Date(now).toISOString(),
      coverage: { source: 'session_context', complete: true, missing_source_ids: [] },
    });
    jest.spyOn(GatewayHistoryStore.prototype, 'appendAssistant').mockResolvedValue({ message_id: 'assistant-a', ordinal: 2, version: 2 });
    submit = jest.spyOn(ChatDataClient.prototype, 'submitTurnResult').mockResolvedValue();
    request = jest.spyOn(ChatDataClient.prototype, 'qualificationRequest').mockImplementation(async operation => {
      if (operation === 'pending' || operation === 'claim') return { binding, manifest: qualificationManifest };
      if (operation === 'fixtures') return fixtures;
      if (operation === 'active') return { nonce: binding.nonce, active: true, expires_at: binding.expires_at };
      return { nonce: binding.nonce, result_digest: 'c'.repeat(64) };
    });
    execute = jest.mocked(QualificationDiagnostics.prototype.execute).mockImplementation(async probe => ({
      probe, executed: true, outcome: 'denied', exposure_digests: [],
    }));
    cleanup = jest.mocked(QualificationDiagnostics.prototype.cleanup).mockResolvedValue();
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'synthetic.token' });
  });

  afterEach(() => { jest.restoreAllMocks(); jest.resetAllMocks(); });

  it('matches the gateway manifest digest including all owner-write controls', () => {
    expect(qualificationDigest).toBe('ad64413daf897398f5761d3cc1974a65cbd194f146769e53807f8d6288fbe63e');
    expect(Object.values(qualificationManifest.probes).flat()).toHaveLength(94);
  });

  it('collects executed observations, not model text, with the exact claim', async () => {
    const model = jest.spyOn(client, 'modelDecision');
    await executeSandboxQualification(client);
    const result = request.mock.calls.find(call => call[0] === 'result')![1] as { observations: unknown[] };
    expect(result).toMatchObject({ nonce: binding.nonce, manifest_digest: qualificationDigest, source: 'sandbox_runtime', status: 'completed', failure: null });
    expect(result.observations).toHaveLength(26);
    expect(model).not.toHaveBeenCalled();
    expect(submit).toHaveBeenCalledWith({ outcome: 'completed', message_id: 'assistant-a' });
    expect(cleanup).toHaveBeenCalledWith(true);
  });

  it('records unavailable execution as failed, preserving the missing probe name', async () => {
    execute.mockImplementation(async probe => probe === 'memory_write' ? undefined : ({ probe, executed: true, outcome: 'denied', exposure_digests: [] }));
    await executeSandboxQualification(client);
    expect(request).toHaveBeenCalledWith('result', expect.objectContaining({
      status: 'failed', failure: 'probe_unavailable',
      observations: expect.arrayContaining([{ probe: 'memory_write', executed: false, outcome: 'error', exposure_digests: [] }]),
    }), undefined);
    expect(submit).toHaveBeenCalledWith({ outcome: 'failed' });
    expect(cleanup).toHaveBeenCalledWith(false);
  });

  it('does not erase credential exposure or cross-scope success from observations', async () => {
    execute.mockImplementation(async probe => ({ probe, executed: true, outcome: 'allowed', exposure_digests: ['d'.repeat(64)] }));
    await executeSandboxQualification(client);
    expect(request).toHaveBeenCalledWith('result', expect.objectContaining({ observations: expect.arrayContaining([
      { probe: 'environment', executed: true, outcome: 'allowed', exposure_digests: ['d'.repeat(64)] },
    ]) }), undefined);
  });

  it.each(['expired', 'changed', 'model_refusal'])('never executes an %s plan', async mutation => {
    request.mockImplementation(async operation => mutation === 'model_refusal' ? { content: 'I refuse' } : {
      binding: mutation === 'expired' ? { ...binding, expires_at: now / 1000 } :
        operation === 'claim' ? { ...binding, sandbox_uid: 'replacement' } : binding,
      manifest: qualificationManifest,
    });
    await expect(executeSandboxQualification(client)).rejects.toThrow();
    expect(execute).not.toHaveBeenCalled();
    expect(request.mock.calls.some(call => call[0] === 'result')).toBe(false);
  });

  it('refuses qualification outside the authenticated execution mode', async () => {
    jest.mocked(client.qualificationRequired).mockReturnValue(false);
    await expect(executeSandboxQualification(client)).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(request).not.toHaveBeenCalled();
  });

  it('cancels actual probe work and cleans residue without reporting a successful result', async () => {
    const cancellation = new AbortController();
    execute.mockImplementation(async (_probe, signal) => {
      cancellation.abort();
      signal.throwIfAborted();
      return undefined;
    });
    await expect(executeSandboxQualification(client, cancellation.signal)).rejects.toThrow();
    expect(cleanup).toHaveBeenCalledWith(false);
    expect(request.mock.calls.some(call => call[0] === 'result')).toBe(false);
    expect(submit).not.toHaveBeenCalled();
  });

  it('stops on server-side cancellation and attempts only a failed receipt', async () => {
    const original = request.getMockImplementation()!;
    request.mockImplementation(async (operation, ...args) => {
      if (operation === 'active') throw new ChatDataError('denied', 404);
      return original(operation, ...args);
    });
    await executeSandboxQualification(client);
    expect(request).toHaveBeenCalledWith('result', expect.objectContaining({ status: 'failed', failure: 'deadline' }), undefined);
    expect(cleanup).toHaveBeenCalledWith(false);
    expect(submit).toHaveBeenCalledWith({ outcome: 'failed' });
  });

  it('never completes a turn when cleanup or collection fails', async () => {
    cleanup.mockRejectedValue(new Error('cleanup failed'));
    const original = request.getMockImplementation()!;
    request.mockImplementation(async (operation, ...args) => {
      if (operation === 'result') throw new ChatDataError('unavailable');
      return original(operation, ...args);
    });
    await expect(executeSandboxQualification(client)).rejects.toMatchObject({ code: 'unavailable' });
    expect(request).toHaveBeenCalledWith('result', expect.objectContaining({ status: 'failed', failure: 'execution_error' }), undefined);
    expect(submit).not.toHaveBeenCalled();
  });
});
