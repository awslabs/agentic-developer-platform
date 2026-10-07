import { createHash } from 'node:crypto';
import type { fromTokenFile } from '@aws-sdk/credential-provider-web-identity';
import { admitSandboxPod, registeredSandboxAssignment, sandboxPodExited, waitForSandboxExit, withAdmittedSandboxPod } from './sandbox-supervisor-admission';
import { buildSandboxPod } from './sandbox-pod';
import { gatewayBinding } from './sandbox-gateway.fixture';
import type { SandboxPodApi } from './sandbox-launcher';

const assignment = {
  runId: 'run-a', sessionId: 'session-a', envelopeDigest: 'a'.repeat(64), gatewayUrl: 'https://gateway.example.test',
  image: `registry.example.test/chat@sha256:${'b'.repeat(64)}`,
};
const template = buildSandboxPod(assignment, gatewayBinding);
const pod = { name: `${template.metadata.generateName}abcde`, uid: '01234567-89ab-cdef-0123-456789abcdef' };
const identity = { roleArn: 'arn:aws:iam::000000000000:role/test-chat-supervisor',
  workerRoleArn: 'arn:aws:iam::000000000000:role/test-worker',
  tokenFile: '/var/run/secrets/adp-chat-supervisor/token', region: 'us-east-1' };
const receipt = { run_id: assignment.runId, session_id: 'session-a', lease_generation: 2 };
const load = jest.fn(() => async () => ({ accessKeyId: 'TESTKEY', secretAccessKey: 'TESTSECRET', sessionToken: 'TESTSESSION' })) as unknown as jest.Mock & typeof fromTokenFile;
const send = jest.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response(JSON.stringify(receipt))) as jest.MockedFunction<typeof fetch>;

beforeEach(() => { load.mockClear(); send.mockClear(); });

test('binds supervisor assignment to the exact gateway-registered queue bytes', () => {
  const raw = '{"message_id":"run-a","session_id":"session-a","task_id":"task-a","session_generation":1,"score":1.0}';
  const parsed = registeredSandboxAssignment(raw, assignment.image, assignment.gatewayUrl);
  expect(parsed).toEqual({ ...assignment, taskId: 'task-a', sessionGeneration: 1, envelopeDigest: createHash('sha256').update(raw).digest('hex') });
  expect(parsed.envelopeDigest).not.toBe(createHash('sha256').update(JSON.stringify(JSON.parse(raw))).digest('hex'));
});

test('binds the selected session mode to gateway-registered envelope bytes', () => {
  const raw = '{"message_id":"run-a","session_id":"session-a","task_id":"task-a","session_generation":1,"session_mode":"persistent"}';
  const parsed = registeredSandboxAssignment(raw, assignment.image, assignment.gatewayUrl);
  expect(parsed.sessionMode).toBe('persistent');
  expect(parsed.envelopeDigest).toBe(createHash('sha256').update(raw).digest('hex'));
  expect(() => registeredSandboxAssignment(raw.replace('"persistent"', '"unknown"'), assignment.image, assignment.gatewayUrl))
    .toThrow('registered envelope');
});

test.each([
  '{}', '[]', 'not-json', JSON.stringify({ message_id: '../other', session_id: 'session-a', task_id: 'task-a' }),
  JSON.stringify({ message_id: 'run-a', session_id: 1, task_id: 'task-a' }),
  JSON.stringify({ message_id: 'run-a', session_id: 'session-a' }), 'x'.repeat(65_537),
])('rejects malformed or oversized queue assignment before a pod is created', raw => {
  expect(() => registeredSandboxAssignment(raw, assignment.image, assignment.gatewayUrl)).toThrow('registered envelope');
});

test('requires trusted pinned image and gateway origin when parsing queued identity', () => {
  const raw = '{"message_id":"run-a","session_id":"session-a","task_id":"task-a","session_generation":1}';
  expect(() => registeredSandboxAssignment(raw, 'latest', assignment.gatewayUrl)).toThrow('pinned image');
  expect(() => registeredSandboxAssignment(raw, assignment.image, 'http://gateway.example.test')).toThrow('Only https: is allowed');
});

test('signs only an exact admission body digest with temporary supervisor web identity', async () => {
  await expect(admitSandboxPod(assignment, pod, identity, send, load)).resolves.toEqual(receipt);
  expect(load).toHaveBeenCalledWith({ roleArn: identity.roleArn, webIdentityTokenFile: identity.tokenFile,
    clientConfig: { region: identity.region } });
  expect(send).toHaveBeenCalledTimes(1);
  const [url, request] = send.mock.calls[0];
  expect(url).toBe('https://gateway.example.test/internal/v1/agent/chat/data/admit');
  expect(request?.redirect).toBe('error');
  expect(request?.method).toBe('POST');
  const body = JSON.parse(request!.body as string);
  expect(body).toEqual({ run_id: 'run-a', envelope_digest: 'a'.repeat(64), pod_name: pod.name, pod_uid: pod.uid });
  const headers = request!.headers as Record<string, string>;
  const proof = JSON.parse(Buffer.from(headers['X-Adp-Producer-Proof'], 'base64').toString());
  const canonical = JSON.stringify({ envelope_digest: body.envelope_digest, pod_name: pod.name, pod_uid: pod.uid, run_id: body.run_id });
  expect(proof['x-adp-work-invocation']).toBe(createHash('sha256').update(canonical).digest('hex'));
  expect(proof.authorization).toContain('SignedHeaders=content-type;host;x-adp-work-invocation;x-amz-date;x-amz-security-token');
  expect(proof.host).toBeUndefined();
  expect(proof['x-amz-content-sha256']).toBeUndefined();
  expect(JSON.stringify(request)).not.toContain('TESTSECRET');
});

test.each([
  { ...assignment, gatewayUrl: 'http://gateway.example.test' },
  { ...assignment, gatewayUrl: 'https://gateway.example.test/redirect' },
  { ...assignment, gatewayUrl: 'https://gateway.example.test@evil.example.test' },
  { ...assignment, envelopeDigest: 'not-a-digest' },
  { ...assignment, runId: '../other' },
  { ...assignment, sessionId: '../other' },
])('refuses malformed assignment before touching a credential', async invalid => {
  await expect(admitSandboxPod(invalid, pod, identity, send, load)).rejects.toThrow();
  expect(load).not.toHaveBeenCalled();
  expect(send).not.toHaveBeenCalled();
});

test('refuses cross-run pod substitution without loading credentials', async () => {
  await expect(admitSandboxPod(assignment, { ...pod, name: 'chat-turn-ffffffffffff-abcde' }, identity, send, load)).rejects.toThrow('dedicated identity');
  expect(load).not.toHaveBeenCalled();
});

test('refuses missing, shared or malformed supervisor identity before credential acquisition', async () => {
  for (const invalid of [
    { ...identity, roleArn: identity.workerRoleArn },
    { ...identity, tokenFile: '/tmp/attacker-token' },
    { ...identity, tokenFile: '/var/run/secrets/../attacker-token' },
    { ...identity, roleArn: 'test-worker' },
  ]) {
    await expect(admitSandboxPod(assignment, pod, invalid, send, load)).rejects.toThrow('dedicated identity');
  }
  expect(load).not.toHaveBeenCalled();
});

test('refuses long-lived credentials and never sends admission', async () => {
  const staticCredentials = jest.fn(() => async () => ({ accessKeyId: 'TESTKEY', secretAccessKey: 'TESTSECRET' })) as unknown as typeof fromTokenFile;
  await expect(admitSandboxPod(assignment, pod, identity, send, staticCredentials)).rejects.toThrow('temporary identity');
  expect(send).not.toHaveBeenCalled();
});

test.each([
  { run_id: 'other', session_id: 'session-a', lease_generation: 2 },
  { run_id: 'run-a', session_id: 'swapped-session', lease_generation: 2 },
  { run_id: 'run-a', session_id: 'session-a', lease_generation: 0 },
  { run_id: 'run-a', session_id: 'session-a', lease_generation: '2' },
  { run_id: 'run-a', session_id: '../other', lease_generation: 2 },
])('rejects mismatched or malformed gateway receipt', async invalid => {
  send.mockResolvedValueOnce(new Response(JSON.stringify(invalid)));
  await expect(admitSandboxPod(assignment, pod, identity, send, load)).rejects.toThrow('response invalid');
});

test('rejects gateway refusal and oversized responses', async () => {
  send.mockResolvedValueOnce(new Response('refused', { status: 403 }));
  await expect(admitSandboxPod(assignment, pod, identity, send, load)).rejects.toThrow('admission refused');
  send.mockResolvedValueOnce(new Response('x'.repeat(4100)));
  await expect(admitSandboxPod(assignment, pod, identity, send, load)).rejects.toThrow('response too large');
});

test('admits the exact created pod before dispatch and always deletes that UID', async () => {
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    create: jest.fn(async (_template: Parameters<SandboxPodApi["create"]>[0]) => ({ metadata: { ...template.metadata, ...pod }, spec: template.spec })),
    remove: jest.fn(async (_name: string, _uid: string) => {}),
  };
  const run = jest.fn(async () => 'done');
  await expect(withAdmittedSandboxPod(assignment, api, identity, run, send, load)).resolves.toBe('done');
  expect(run).toHaveBeenCalledWith(pod, receipt);
  expect(api.remove).toHaveBeenCalledWith(pod.name, pod.uid);
  send.mockResolvedValueOnce(new Response('refused', { status: 403 }));
  await expect(withAdmittedSandboxPod(assignment, api, identity, run, send, load)).rejects.toThrow('admission refused');
  send.mockResolvedValueOnce(new Response(JSON.stringify({ ...receipt, session_id: 'swapped-session' })));
  await expect(withAdmittedSandboxPod(assignment, api, identity, run, send, load)).rejects.toThrow('response invalid');
  expect(run).toHaveBeenCalledTimes(1);
  expect(api.remove).toHaveBeenCalledTimes(3);
});


test('waits for only a signed, UID-bound exit signal before relinquishing the pod', async () => {
  const outcomes = [false, true];
  const poll = jest.fn(async (_url: string | URL | Request, _init?: RequestInit) =>
    new Response(JSON.stringify({ run_id: assignment.runId, pod_uid: pod.uid, terminated: outcomes.shift() }))) as jest.MockedFunction<typeof fetch>;
  let elapsed = 0;
  const sleep = jest.fn(async (ms: number) => { elapsed += ms; });
  await waitForSandboxExit(assignment, pod, identity, poll, load, { now: () => elapsed, sleep });
  expect(sleep).toHaveBeenCalledWith(1000);
  expect(poll).toHaveBeenCalledTimes(2);
  for (const [url, options] of poll.mock.calls) {
    expect(url).toBe(`${assignment.gatewayUrl}/internal/v1/agent/chat/data/exit`);
    expect(JSON.parse(options!.body as string)).toEqual({ run_id: assignment.runId,
      envelope_digest: assignment.envelopeDigest, pod_name: pod.name, pod_uid: pod.uid });
    expect(options!.headers).toEqual(expect.objectContaining({ 'X-Adp-Producer-Proof': expect.any(String) }));
  }
});

test.each([
  { run_id: assignment.runId, pod_uid: 'other-uid', terminated: true },
  { run_id: 'other-run', pod_uid: pod.uid, terminated: true },
  { run_id: assignment.runId, pod_uid: pod.uid, terminated: 'true' },
])('refuses a forged or malformed supervisor exit signal', async response => {
  const poll = jest.fn(async () => new Response(JSON.stringify(response))) as unknown as typeof fetch;
  await expect(sandboxPodExited(assignment, pod, identity, poll, load)).rejects.toThrow('exit response invalid');
});

test('missing terminal proof fails closed at the supervisor deadline', async () => {
  const poll = jest.fn(async () => new Response(JSON.stringify({ run_id: assignment.runId,
    pod_uid: pod.uid, terminated: false }))) as unknown as typeof fetch;
  let elapsed = 0;
  await expect(waitForSandboxExit(assignment, pod, identity, poll, load,
    { now: () => elapsed, sleep: async () => { elapsed = 780_001; } })).rejects.toThrow('exit unverified');
  expect(poll).toHaveBeenCalledTimes(1);
});
