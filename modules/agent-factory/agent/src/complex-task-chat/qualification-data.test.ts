import { createHash } from 'node:crypto';
import { ChatDataClient } from './gateway/chat-data-client';
import { qualificationBinding, qualificationDigest, qualificationManifest, QualificationObservation } from './qualification-contract';
import { qualificationDataProbe, qualificationCanary } from './qualification-data';
import { QualificationDiagnostics } from './qualification-diagnostics';
import { executeSandboxQualification } from './sandbox-qualification';

jest.mock('./qualification-diagnostics');

const now = Date.parse('2026-10-12T12:00:00Z');
const timestamp = new Date(now).toISOString();
const owner = qualificationBinding.parse({
  nonce: 'a'.repeat(64), manifest_digest: qualificationDigest, manifest_version: 'e45-v1',
  evaluation_id: 'evaluation', target: 'test-target', actor: 'a1', tenant_id: 'tenant', user_id: 'owner', team_id: '',
  session_id: 'session-a1', run_id: 'run-a1', sandbox_uid: 'pod-a1', lease_generation: 1,
  image_digest: `sha256:${'b'.repeat(64)}`, attempt: 1, credential_epoch: 1,
  issued_at: now / 1000, expires_at: now / 1000 + 120, limits: qualificationManifest.limits,
});
const fixtures = {
  owner, message_id: 'msg-a', summary_id: 'sum-a', artifact_id: `art_${'a'.repeat(32)}`, memory_id: `mem_${'b'.repeat(32)}`,
  cursor: 'bounded-cursor', history_version: 3, memory_version: 1, cursor_expires_at: now / 1000 + 300,
  bucket: 'test-bucket', object_key: 'test-key', context_table: 'test-context', secret_canary: 'test-canary', region: 'us-east-1',
};
const content = qualificationCanary;
const userTurn = `user_${createHash('sha256').update(owner.run_id).digest('hex')}`;
const checksum = createHash('sha256').update(content).digest('hex');
const envelope = { status: 'ok', next_cursor: null, observed_at: timestamp,
  coverage: { source: 'session_context', complete: true, missing_source_ids: [] } };
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('qualification through the real scoped data transport', () => {
  afterEach(() => { jest.restoreAllMocks(); jest.resetAllMocks(); });

  it.each(['a1', 'a2', 'b1'] as const)('executes every fixed %s operation and collects actual transport outcomes', async actor => {
    jest.spyOn(Date, 'now').mockReturnValue(now);
    const binding = { ...owner, actor, session_id: `session-${actor}`, run_id: `run-${actor}`, sandbox_uid: `pod-${actor}` };
    const plan = { binding, manifest: qualificationManifest, ...(actor === 'a1' ? {} : { fixtures }) };
    const calls: { route: string; body: Record<string, unknown>; headers: Record<string, string> }[] = [];
    let collected: { observations: QualificationObservation[]; status: string } | undefined;
    let version = 1;
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const route = String(input).split('/v1/chat/data/')[1];
      const body = JSON.parse(String(init?.body ?? '{}'));
      calls.push({ route, body, headers: init?.headers as Record<string, string> });
      if (route === 'bootstrap') return json({ capability: 'ordinary.capability', run_id: binding.run_id, session_id: binding.session_id,
        attempt: 1, lease_generation: 1, expires_at: now / 1000 + 300, qualification_required: true });
      if (route === 'qualification/pending' || route === 'qualification/claim') return json(plan);
      if (route === 'qualification/active') return json({ nonce: binding.nonce, active: true, expires_at: binding.expires_at });
      if (route === 'qualification/fixtures') {
        expect(version).toBe(3);
        expect(calls.filter(call => ['draft/write', 'history/append', 'history/summary/append', 'artifact/create', 'memory/write'].includes(call.route))).toHaveLength(5);
        return json(fixtures);
      }
      if (route === 'qualification/result') {
        collected = body;
        return json({ nonce: binding.nonce, result_digest: 'c'.repeat(64) });
      }
      if (route === 'turn/result') return json({ run_id: binding.run_id, session_id: binding.session_id, attempt: 1, lease_generation: 1,
        sandbox_uid: binding.sandbox_uid, outcome: body.outcome, message_id: body.message_id ?? null, terminal: false });
      if (actor !== 'a1' && !collected) return json({ detail: { error: 'chat_authorization_refused' } }, 404);
      if (route === 'draft/read') return json({ version: 0 });
      if (route === 'draft/write') return json({ version: 1 });
      if (route === 'history/turn') return json({ message_id: userTurn, ordinal: 1 });
      if (route === 'history/read') return json({ ...envelope, version, entries: [{ ordinal: 1, type: 'msg', ref: userTurn, tokens: 8 }] });
      if (route === 'history/append') return json({ message_id: fixtures.message_id, ordinal: ++version, version });
      if (route === 'history/summary/append') return json({ summary_id: fixtures.summary_id, version: ++version });
      if (route === 'artifact/create') return json({ id: fixtures.artifact_id, checksum });
      if (route === 'memory/write') return json({ memory_id: fixtures.memory_id, version: 1 });
      if (route === 'history/messages') return json({ ...envelope, entries: [{ ref: fixtures.message_id,
        message: { role: 'assistant', content, ts: timestamp, tokens: 8 } }] });
      if (route === 'history/summary') return json({ ...envelope, entries: [{ ref: fixtures.summary_id,
        summary: { depth: 0, kind: 'leaf', content, sourceIds: [userTurn], parentIds: [], earliestAt: timestamp, latestAt: timestamp, tokens: 8 } }] });
      if (route === 'artifact/list') return json({ entries: [{ id: fixtures.artifact_id }] });
      if (route === 'memory/read') return json({ ...envelope, coverage: { ...envelope.coverage, source: 'owned_memory' }, entries: [{
        id: fixtures.memory_id, version: 1, content, kind: 'fact', tags: [], scope: { user: 'owner', tenant: 'tenant' },
        source: { sessionId: owner.session_id, runId: owner.run_id }, createdAt: timestamp, updatedAt: timestamp,
      }] });
      if (route.startsWith('artifact/session-')) return new Response(content, { headers: { 'X-Artifact-Scan-Status': 'not_scanned' } });
      throw new Error(`Unexpected route ${route}`);
    });
    jest.mocked(QualificationDiagnostics.prototype.execute).mockImplementation(async probe => ({ probe, executed: true,
      outcome: probe === 'sandbox_identity' ? 'distinct' : ['environment', 'proc', 'projected_files', 'residue_file', 'residue_process'].includes(probe) ? 'absent' : 'denied', exposure_digests: [] }));
    jest.mocked(QualificationDiagnostics.prototype.cleanup).mockResolvedValue();
    const client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'ordinary.workload', sleep: async () => undefined });
    await client.sessionMode();
    await executeSandboxQualification(client);
    expect(collected?.status).toBe('completed');
    expect(collected?.observations).toHaveLength(qualificationManifest.probes[actor].length);
    for (const row of collected!.observations) {
      expect(row.executed).toBe(true);
    }
    expect(collected!.observations.filter(row => ['error', 'timeout'].includes(row.outcome))).toEqual([]);
    const diagnosticNames = jest.mocked(QualificationDiagnostics.prototype.execute).mock.calls.map(call => call[0]);
    expect(diagnosticNames).toEqual(expect.arrayContaining(['s3_read', 'dynamodb_read', 'secrets_read', 'sandbox_identity']));
    expect(diagnosticNames).not.toEqual(expect.arrayContaining(['message_read']));
    expect(calls.every(call => call.route === 'bootstrap' || call.headers.Authorization === 'Bearer ordinary.capability')).toBe(true);
    expect(calls.filter(call => /model|shell|command/.test(call.route))).toEqual([]);
    if (actor === 'a1') {
      expect(collected!.observations.filter(row => row.outcome === 'allowed')).toHaveLength(13);
    } else {
      expect(collected!.observations.filter(row => row.outcome === 'denied')).toHaveLength(28);
      expect(calls.some(call => call.body.principal === owner.user_id)).toBe(true);
      expect(calls.some(call => call.body.run_id === owner.run_id)).toBe(true);
      expect(calls.some(call => call.body.lease_generation === owner.lease_generation)).toBe(true);
      expect(calls.some(call => call.headers['X-User-Id'] === owner.user_id)).toBe(true);
      expect(calls.some(call => call.headers['X-Tenant-Id'] === owner.tenant_id)).toBe(true);
      expect(calls.some(call => call.route.includes(`?run_id=${owner.run_id}`))).toBe(true);
    }
  });

  it.each([200, 401, 403, 404, 409, 422, 500, 503])('never converts HTTP %s into a fictitious denial', async status => {
    jest.spyOn(Date, 'now').mockReturnValue(now);
    jest.spyOn(globalThis, 'fetch').mockResolvedValueOnce(json({ capability: 'ordinary.capability', run_id: 'run-a2', session_id: 'session-a2',
      attempt: 1, lease_generation: 1, expires_at: now / 1000 + 300, qualification_required: true })).mockResolvedValue(json({}, status));
    const client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'ordinary.workload', sleep: async () => undefined });
    const result = qualificationDataProbe(client, { ...owner, actor: 'a2' }, 'message_read', fixtures, new AbortController().signal);
    if ([200, 401, 403, 404, 422].includes(status)) {
      await expect(result).resolves.toMatchObject({ executed: true, outcome: status === 200 ? 'allowed' : 'denied' });
    } else await expect(result).rejects.toBeDefined();
  });
});
