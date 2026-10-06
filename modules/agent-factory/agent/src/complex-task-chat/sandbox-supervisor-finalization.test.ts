import type { fromTokenFile } from '@aws-sdk/credential-provider-web-identity';
import { buildSandboxPod } from './sandbox-pod';
import { gatewayBinding } from './sandbox-gateway.fixture';
import type { SandboxPodApi } from './sandbox-launcher';
import { finalizeSandboxTurn, reconcileAdmittedSandbox, waitForSandboxRemoval } from './sandbox-supervisor-admission';

const assignment = { runId: 'run-a', sessionId: 'session-a', envelopeDigest: 'a'.repeat(64),
  gatewayUrl: 'https://gateway.example.test', image: `registry.example.test/chat@sha256:${'b'.repeat(64)}` };
const template = buildSandboxPod(assignment, gatewayBinding);
const pod = { name: `${template.metadata.generateName}abcde`, uid: '01234567-89ab-cdef-0123-456789abcdef' };
const identity = { roleArn: 'arn:aws:iam::000000000000:role/test-chat-supervisor',
  workerRoleArn: 'arn:aws:iam::000000000000:role/test-worker',
  tokenFile: '/var/run/secrets/adp-chat-supervisor/token', region: 'us-east-1' };
const admission = { run_id: assignment.runId, session_id: assignment.sessionId, lease_generation: 2 };
const terminal = { ...admission, attempt: 1, sandbox_uid: pod.uid, outcome: 'completed', message_id: 'reply-a',
  retryable: false, automatic_replay_permitted: false, accounting_status: 'settled', terminal: true, finalized_at: 1_800_000_000 };
const load = jest.fn(() => async () => ({ accessKeyId: 'TESTKEY', secretAccessKey: 'TESTSECRET', sessionToken: 'TESTSESSION' })) as unknown as jest.Mock & typeof fromTokenFile;

function fixture(result: unknown = terminal) {
  const events: string[] = [];
  const send = jest.fn(async (url: string | URL | Request, _init?: RequestInit) => {
    const operation = String(url).split('/').pop()!;
    events.push(operation);
    const response = operation === 'admit' ? admission : operation === 'exit' ?
      { run_id: assignment.runId, pod_uid: pod.uid, terminated: true } : operation === 'teardown' ?
        { run_id: assignment.runId, pod_uid: pod.uid, removed: true } : result;
    return new Response(JSON.stringify(response));
  }) as jest.MockedFunction<typeof fetch>;
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    create: jest.fn(async (_template: Parameters<SandboxPodApi['create']>[0]) => {
      events.push('create'); return { metadata: { ...template.metadata, ...pod }, spec: template.spec };
    }),
    remove: jest.fn(async (_name: string, _uid: string) => { events.push('delete'); }),
  };
  let elapsed = 0;
  const timing = { now: () => elapsed, sleep: jest.fn(async (ms: number) => { elapsed += ms; }) };
  return { events, send, api, timing };
}

beforeEach(() => { load.mockClear(); });

test.each(['completed', 'failed', 'cancelled', 'interrupted'])('reconciles %s only after UID-bound exit and removal', async outcome => {
  const result = { ...terminal, outcome, message_id: outcome === 'completed' ? 'reply-a' : null,
    retryable: outcome === 'interrupted', accounting_status: outcome === 'interrupted' ? 'not_used' : 'settled' };
  const { api, send, events, timing } = fixture(result);
  await expect(reconcileAdmittedSandbox(assignment, api, identity, send, load, timing)).resolves.toEqual(result);
  expect(events).toEqual(['create', 'admit', 'exit', 'delete', 'teardown', 'finalize']);
  expect(api.remove).toHaveBeenCalledTimes(1);
  expect(api.remove).toHaveBeenCalledWith(pod.name, pod.uid);
  for (const [url, request] of send.mock.calls) {
    expect(String(url).startsWith(`${assignment.gatewayUrl}/internal/v1/agent/chat/data/`)).toBe(true);
    expect(JSON.parse(request!.body as string)).toEqual({ run_id: assignment.runId, envelope_digest: assignment.envelopeDigest,
      pod_name: pod.name, pod_uid: pod.uid });
    expect(request!.redirect).toBe('error');
    expect(request!.signal).toBeInstanceOf(AbortSignal);
    expect(request!.headers).toEqual(expect.objectContaining({ 'X-Adp-Producer-Proof': expect.any(String) }));
    expect(JSON.stringify(request)).not.toContain('TESTSECRET');
  }
  expect(load).toHaveBeenCalledTimes(4);
  expect(load).toHaveBeenCalledWith({ roleArn: identity.roleArn, webIdentityTokenFile: identity.tokenFile,
    clientConfig: { region: identity.region } });
});

test('does not mistake accepted deletion for removal; polls until the gateway confirms absence', async () => {
  const { api, send, timing } = fixture();
  const original = send.getMockImplementation()!;
  let remaining = 2;
  send.mockImplementation(async (url, request) => String(url).endsWith('/teardown') && remaining-- > 0 ?
    new Response(JSON.stringify({ run_id: assignment.runId, pod_uid: pod.uid, removed: false })) : original(url, request));
  await expect(reconcileAdmittedSandbox(assignment, api, identity, send, load, timing)).resolves.toEqual(terminal);
  expect(timing.sleep.mock.calls).toEqual([[1000], [1000]]);
  expect(send.mock.calls.map(call => String(call[0]).split('/').pop())).toEqual(['admit', 'exit', 'teardown', 'teardown', 'teardown', 'finalize']);
});

test.each([false, true])('never finalizes without removal proof, including uncertain deletion: %s', async lostDeletionResponse => {
  const { api, send } = fixture();
  if (lostDeletionResponse) api.remove.mockRejectedValueOnce(new Error('delete response lost'));
  const original = send.getMockImplementation()!;
  send.mockImplementation(async (url, request) => String(url).endsWith('/teardown') ?
    new Response(JSON.stringify({ run_id: assignment.runId, pod_uid: pod.uid, removed: false })) : original(url, request));
  let elapsed = 0;
  await expect(reconcileAdmittedSandbox(assignment, api, identity, send, load,
    { now: () => elapsed, sleep: async () => { elapsed = 60_001; } })).rejects.toThrow('removal unverified');
  expect(send.mock.calls.map(call => String(call[0]).split('/').pop())).toEqual(['admit', 'exit', 'teardown']);
  expect(api.remove).toHaveBeenCalledTimes(1);
});

test('reconciles an ambiguous deletion through trusted removal proof without another pod or delete', async () => {
  const { api, send, timing } = fixture();
  api.remove.mockRejectedValueOnce(new Error('delete response lost'));
  await expect(reconcileAdmittedSandbox(assignment, api, identity, send, load, timing)).resolves.toEqual(terminal);
  expect(api.create).toHaveBeenCalledTimes(1);
  expect(api.remove).toHaveBeenCalledTimes(1);
});

test.each([
  null, {}, { run_id: 'other-run', pod_uid: pod.uid, removed: true },
  { run_id: assignment.runId, pod_uid: 'different-uid', removed: true },
  { run_id: assignment.runId, pod_uid: pod.uid, removed: 'true' },
  { run_id: assignment.runId, pod_uid: pod.uid, terminated: true },
])('rejects substituted and malformed removal evidence: %j', async receipt => {
  const { send, timing } = fixture();
  send.mockResolvedValueOnce(new Response(JSON.stringify(receipt)));
  await expect(waitForSandboxRemoval(assignment, pod, identity, send, load, timing)).rejects.toThrow('removal response invalid');
  expect(send).toHaveBeenCalledTimes(1);
});

test.each([
  { run_id: 'other-run' }, { session_id: 'other-owner-session' }, { sandbox_uid: 'other-pod' },
  { lease_generation: 1 }, { lease_generation: '2' }, { attempt: 0 }, { attempt: 1.5 }, { attempt: '1' },
  { attempt: Number.MAX_SAFE_INTEGER + 1 }, { finalized_at: 0 }, { finalized_at: '1800000000' },
  { finalized_at: 1.5 }, { outcome: 'running' }, { message_id: null }, { message_id: '../reply' },
  { message_id: 'a'.repeat(129) }, { accounting_status: 'ignored' }, { retryable: 'false' },
  { retryable: true }, { automatic_replay_permitted: true }, { automatic_replay_permitted: null },
  { terminal: false }, { terminal: 'true' }, { outcome: 'failed' },
  { outcome: 'interrupted', message_id: null, retryable: true, accounting_status: 'unresolved' },
])('rejects malformed, stale or broadened terminal receipt: %j', async changes => {
  const { send } = fixture({ ...terminal, ...changes });
  await expect(finalizeSandboxTurn(assignment, pod, admission, identity, send, load)).rejects.toThrow('terminal response invalid');
});

test.each([null, [], {}, false])('rejects absent terminal receipt: %j', async result => {
  const { send } = fixture(result);
  await expect(finalizeSandboxTurn(assignment, pod, admission, identity, send, load)).rejects.toThrow('terminal response invalid');
});

test('preserves unresolved accounting and denies automatic replay', async () => {
  const result = { ...terminal, outcome: 'interrupted', message_id: null, accounting_status: 'unresolved' };
  const { api, send, timing } = fixture(result);
  await expect(reconcileAdmittedSandbox(assignment, api, identity, send, load, timing)).resolves.toEqual(result);
  expect(send).toHaveBeenCalledTimes(4);
});

test.each([{ run_id: 'other-run' }, { session_id: 'other-session' }, { lease_generation: 0 }])(
  'refuses substituted admission before requesting finalization: %j', async changes => {
    const { send } = fixture();
    await expect(finalizeSandboxTurn(assignment, pod, { ...admission, ...changes }, identity, send, load)).rejects.toThrow('matching admission');
    expect(load).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
  },
);

test.each(['admit', 'exit', 'teardown', 'finalize'])('stops reconciliation on a refused %s request', async operation => {
  const { api, send, events, timing } = fixture();
  const original = send.getMockImplementation()!;
  send.mockImplementation(async (url, request) => String(url).endsWith(`/${operation}`) ?
    new Response('refused', { status: 403 }) : original(url, request));
  await expect(reconcileAdmittedSandbox(assignment, api, identity, send, load, timing)).rejects.toThrow('admission refused');
  if (operation === 'admit' || operation === 'exit') expect(api.remove).not.toHaveBeenCalled();
  else expect(api.remove).toHaveBeenCalledWith(pod.name, pod.uid);
  expect(events).not.toContain('finalize');
});

test('failed admission cannot be replaced by a later removal receipt', async () => {
  const { api, send, timing } = fixture();
  send.mockResolvedValueOnce(new Response(JSON.stringify({ ...admission, session_id: 'other-session' })));
  await expect(reconcileAdmittedSandbox(assignment, api, identity, send, load, timing)).rejects.toThrow('admission response invalid');
  expect(send).toHaveBeenCalledTimes(1);
  expect(api.remove).not.toHaveBeenCalled();
});

test('retries a lost finalization response using only the same bound reconciliation request', async () => {
  const { send } = fixture();
  send.mockRejectedValueOnce(new Error('finalization response lost'));
  await expect(finalizeSandboxTurn(assignment, pod, admission, identity, send, load)).rejects.toThrow('response lost');
  await expect(finalizeSandboxTurn(assignment, pod, admission, identity, send, load)).resolves.toEqual(terminal);
  expect(send.mock.calls.map(call => call[0])).toEqual(Array(2).fill(`${assignment.gatewayUrl}/internal/v1/agent/chat/data/finalize`));
  expect(send.mock.calls[0][1]!.body).toBe(send.mock.calls[1][1]!.body);
});

test.each(['teardown', 'finalize'])('bounds %s responses and refuses credential fallback', async operation => {
  const { send, timing } = fixture();
  const invoke = (credentials = load) => operation === 'teardown' ?
    waitForSandboxRemoval(assignment, pod, identity, send, credentials, timing) :
    finalizeSandboxTurn(assignment, pod, admission, identity, send, credentials);
  send.mockResolvedValueOnce(new Response('x'.repeat(4097)));
  await expect(invoke()).rejects.toThrow('response too large');
  const staticCredentials = jest.fn(() => async () => ({ accessKeyId: 'TESTKEY', secretAccessKey: 'TESTSECRET' })) as unknown as typeof load;
  await expect(invoke(staticCredentials)).rejects.toThrow('temporary identity unavailable');
  expect(send).toHaveBeenCalledTimes(1);
});
