import { createHash } from 'node:crypto';
import type { fromTokenFile } from '@aws-sdk/credential-provider-web-identity';
import { Hash } from '@smithy/hash-node';
import { SignatureV4 } from '@smithy/signature-v4';
import { canonicalJson } from '../invocability-probe/canonical-json';
import { SupervisorResponseError, activateCrewRun, admitSandboxPod, isRemovalUnconfirmed, mintCrewRun, persistentExecutor, provisionCrewLease, waitForCrewReadiness, registeredSandboxAssignment, sandboxPodExited, waitForSandboxExit, withAdmittedSandboxPod, supervisorRequest, SESSION_COMMIT_PATH } from './sandbox-supervisor-admission';
import { buildSandboxPod, sandboxCreationName } from './sandbox-pod';
import { gatewayBinding, runningSandboxPod } from './sandbox-gateway.fixture';
import type { SandboxPodApi } from './sandbox-launcher';
import { reconcileSupervisorDelivery, restoreSupervisorTurn } from './sandbox-supervisor-completion';

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

const STAGE_URL = 'https://abc123def4.execute-api.us-east-1.amazonaws.com/dev';

// Independent oracle: re-sign the observed request for execute-api with the test
// credentials at the request's own x-amz-date; equality proves which host, path,
// headers and body the supervisor actually signed.
async function executeApiAuthorization(host: string, path: string, headers: Record<string, string>, body: string): Promise<string> {
  const stamp = headers['x-amz-date'];
  const signingDate = new Date(`${stamp.slice(0, 4)}-${stamp.slice(4, 6)}-${stamp.slice(6, 8)}T${stamp.slice(9, 11)}:${stamp.slice(11, 13)}:${stamp.slice(13, 15)}Z`);
  const signed = await new SignatureV4({ credentials: { accessKeyId: 'TESTKEY', secretAccessKey: 'TESTSECRET', sessionToken: 'TESTSESSION' },
    region: 'us-east-1', service: 'execute-api', sha256: Hash.bind(null, 'sha256') }).sign({
    method: 'POST', protocol: 'https:', hostname: host, path, body,
    headers: { host, 'Content-Type': 'application/json', 'X-Adp-Producer-Proof': headers['X-Adp-Producer-Proof'] },
  }, { signingDate });
  return signed.headers.authorization;
}

test.each(['session', 'user'].flatMap(scope => ['gateway', 'create', 'mint-response', 'create-response',
  ...(scope === 'user' ? ['activate-response', 'admit-response', 'ready-publication', 'ready-response'] : [])].map(failure => [scope, failure])))
('recovers the reserved %s-scoped Crew binding after a lost %s handoff', async (executorScope, failure) => {
  const userScope = executorScope === 'user';
  const crewRunId = '00000000-0000-4000-8000-000000000001';
  const name = sandboxCreationName(assignment.runId);
  const persistent = { ...assignment, sessionMode: 'persistent' as const, ownerKey: 'f'.repeat(52), taskId: 'task-a', sessionGeneration: 1,
    ...(userScope ? { executor: 'kirocrew' as const } : {}) };
  let reserved = false;
  let minted = false;
  let created = false;
  let activated = false;
  let admitted = false;
  let ready = false;
  let failed = false;
  const fail = (point: string) => {
    if (point === failure && !failed) { failed = true; throw new Error('transient handoff failure'); }
  };
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => { fail('gateway'); return gatewayBinding; }),
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    create: jest.fn(async requested => {
      fail('create');
      expect(created).toBe(false);
      expect(requested.spec.containers[0].env).toContainEqual({ name: 'ADP_RUN_ID', value: crewRunId });
      if (userScope) expect(requested.metadata.labels['adp.io/owner-key']).toBe('a'.repeat(52));
      created = true;
      fail('create-response');
      return { metadata: { ...requested.metadata, name, uid: pod.uid }, spec: requested.spec };
    }),
    remove: jest.fn(async (_name: string, _uid: string) => {}),
  };
  const terminal = { ...receipt, attempt: 1, sandbox_uid: pod.uid, outcome: 'completed', message_id: 'message-a', retryable: false,
    automatic_replay_permitted: false, accounting_status: 'not_used', terminal: true, finalized_at: 1_800_000_000 };
  const completion = { ...receipt, task_id: 'task-a', session_generation: 1, attempt: 1, sandbox_uid: pod.uid,
    processing_lock_released: true, input_acknowledgement_ready: true, completed_at: terminal.finalized_at,
    delivery_id: `chat-terminal-${createHash('sha256').update(canonicalJson(terminal)).digest('hex')}` };
  const scope = { run_id: assignment.runId, session_id: assignment.sessionId, task_id: 'task-a', session_generation: 1 };
  const creation = { ...scope, state: 'creation_reserved', session_mode: 'persistent', pod_name: name,
    image_digest: assignment.image.split('@')[1], attempt: 1 };
  const request = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const path = String(url).split('/').pop();
    if (path === 'resume' && admitted && userScope) return new Response(JSON.stringify({ ...creation,
      state: 'admitted', session_run_id: persistent.runId, sandbox_uid: pod.uid, lease_generation: receipt.lease_generation, crew_owner_pod: true,
      crew_readiness: { run_id: crewRunId, lease_generation: receipt.lease_generation, state: ready ? 'ready' : 'provisioning' } }));
    if (path === 'resume') return new Response(JSON.stringify(reserved ? { ...creation,
      ...(minted ? { crew_run_id: crewRunId } : {}), ...(created ? { sandbox_uid: pod.uid } : {}) } : { ...scope, state: 'unstarted' }));
    if (path === 'reserve') { reserved = true; return new Response(JSON.stringify({ ...creation, state: 'create' })); }
    if (path === 'crew-run') {
      if (minted && !userScope) return new Response('{}', { status: 409 });
      minted = true;
      fail('mint-response');
      return new Response(JSON.stringify({ run_id: crewRunId, lease_generation: receipt.lease_generation,
        ...(userScope ? { owner_key: 'a'.repeat(52), state: 'provisioning', ...(activated ? { activated: true } : {}) } : {}) }));
    }
    if (path === 'lease') return new Response(JSON.stringify({ run_id: crewRunId, lease_generation: receipt.lease_generation, state: 'provisioning' }));
    if (path === 'activate') {
      expect(activated).toBe(false);
      activated = true;
      fail('activate-response');
      return new Response(JSON.stringify({ run_id: crewRunId, lease_generation: receipt.lease_generation }));
    }
    if (path === 'ready') {
      expect(JSON.parse(String(init?.body))).toMatchObject({ crew_run_id: crewRunId, lease_generation: receipt.lease_generation, pod_uid: pod.uid });
      expect(admitted).toBe(true);
      fail('ready-publication');
      ready = true;
      fail('ready-response');
      return new Response(JSON.stringify({ run_id: crewRunId, lease_generation: receipt.lease_generation, state: 'ready' }));
    }
    if (path === 'admit') {
      admitted = true;
      fail('admit-response');
      return new Response(JSON.stringify(receipt));
    }
    if (path === 'commit') {
      if (userScope) expect(ready).toBe(true);
      return new Response(JSON.stringify({ terminal, completion }));
    }
    throw new Error('Unexpected operation');
  }) as unknown as jest.MockedFunction<typeof fetch>;
  const run = () => reconcileSupervisorDelivery(persistent, api, identity, request, load, {});
  const priorScope = process.env.ADP_EXECUTOR_SCOPE;
  process.env.ADP_EXECUTOR_SCOPE = executorScope;
  try {
    await expect(run()).rejects.toThrow('transient handoff failure');
    expect(api.remove).not.toHaveBeenCalled();
    await expect(run()).resolves.toBeUndefined();
    const admittedFailure = ['admit-response', 'ready-publication', 'ready-response'].includes(failure);
    expect(request.mock.calls.filter(([url]) => String(url).endsWith('/crew-run'))).toHaveLength(userScope && !admittedFailure ? 2 : 1);
    expect(request.mock.calls.filter(([url]) => String(url).endsWith('/admit'))).toHaveLength(1);
    if (admittedFailure) {
      const paths = request.mock.calls.map(([url]) => String(url).split('/').pop());
      expect(paths.slice(paths.lastIndexOf('resume'))).toEqual(failure === 'ready-response' ? ['resume', 'commit'] : ['resume', 'ready', 'commit']);
      expect(request.mock.calls.filter(([url]) => String(url).endsWith('/lease'))).toHaveLength(1);
    }
    expect(api.create).toHaveBeenCalledTimes(failure === 'create' ? 2 : 1);
  } finally {
    if (priorScope === undefined) delete process.env.ADP_EXECUTOR_SCOPE;
    else process.env.ADP_EXECUTOR_SCOPE = priorScope;
  }
});

test.each([
  { crew_readiness: null },
  { crew_readiness: { run_id: 'untrusted', lease_generation: 2, state: 'provisioning' } },
  { crew_readiness: { run_id: '00000000-0000-4000-8000-000000000001', lease_generation: 0, state: 'provisioning' } },
  { crew_readiness: { run_id: '00000000-0000-4000-8000-000000000001', lease_generation: 2, state: 'unknown' } },
  { session_run_id: 'another-founding-run' },
  { crew_owner_pod: undefined },
])('refuses invalid resumed Crew readiness before making lifecycle requests: %j', async changed => {
  const persistent = { ...assignment, sessionMode: 'persistent' as const, executor: 'kirocrew' as const, taskId: 'task-a', sessionGeneration: 1 };
  const resumed = { ...receipt, task_id: 'task-a', session_generation: 1, state: 'admitted', session_mode: 'persistent',
    session_run_id: assignment.runId, crew_owner_pod: true, pod_name: sandboxCreationName(assignment.runId), sandbox_uid: pod.uid,
    attempt: 1, image_digest: assignment.image.split('@')[1],
    crew_readiness: { run_id: '00000000-0000-4000-8000-000000000001', lease_generation: 2, state: 'provisioning' }, ...changed };
  const request = jest.fn(async () => new Response(JSON.stringify(resumed))) as unknown as jest.MockedFunction<typeof fetch>;
  const priorScope = process.env.ADP_EXECUTOR_SCOPE;
  process.env.ADP_EXECUTOR_SCOPE = 'user';
  try {
    await expect(restoreSupervisorTurn(persistent, identity, request, load, {})).rejects.toThrow('Crew supervisor recovery readiness invalid');
    expect(request).toHaveBeenCalledTimes(1);
  } finally {
    if (priorScope === undefined) delete process.env.ADP_EXECUTOR_SCOPE;
    else process.env.ADP_EXECUTOR_SCOPE = priorScope;
  }
});

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

test('untrusted notification cannot choose another owner claim', () => {
  const raw = JSON.stringify({ message_id: 'run-a', session_id: 'session-a', task_id: 'task-a',
    session_generation: 1, session_mode: 'persistent', ownerKey: 'b'.repeat(52) });
  const parsed = registeredSandboxAssignment(raw, assignment.image, assignment.gatewayUrl);
  expect(parsed.ownerKey).toBeUndefined();
  expect(() => buildSandboxPod(parsed, gatewayBinding)).toThrow('trusted owner-bound volume key');
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
  // The transport itself is SigV4-signed for execute-api with the same temporary identity.
  expect(headers['Content-Type']).toBe('application/json');
  expect(headers.host).toBeUndefined();
  expect(headers['x-amz-security-token']).toBe('TESTSESSION');
  expect(headers.authorization).toContain('AWS4-HMAC-SHA256 Credential=TESTKEY/');
  expect(headers.authorization).toContain('/us-east-1/execute-api/aws4_request');
  expect(headers.authorization).toContain('SignedHeaders=content-type;host;x-adp-producer-proof;x-amz-content-sha256;x-amz-date;x-amz-security-token');
  await expect(executeApiAuthorization('gateway.example.test', '/internal/v1/agent/chat/data/admit', headers, request!.body as string))
    .resolves.toBe(headers.authorization);
});

test('reaches the gateway internal plane through the API Gateway stage URL with a stage-scoped execute-api signature', async () => {
  const staged = { ...assignment, gatewayUrl: STAGE_URL };
  await expect(admitSandboxPod(staged, pod, identity, send, load)).resolves.toEqual(receipt);
  expect(load).toHaveBeenCalledTimes(1);
  const [url, request] = send.mock.calls[0];
  expect(url).toBe('https://abc123def4.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent/chat/data/admit');
  expect(request?.redirect).toBe('error');
  const headers = request!.headers as Record<string, string>;
  const body = request!.body as string;
  expect(JSON.parse(body)).toEqual({ run_id: 'run-a', envelope_digest: 'a'.repeat(64), pod_name: pod.name, pod_uid: pod.uid });
  const proof = JSON.parse(Buffer.from(headers['X-Adp-Producer-Proof'], 'base64').toString());
  expect(proof['x-adp-work-invocation']).toBe(createHash('sha256').update(canonicalJson(JSON.parse(body))).digest('hex'));
  expect(headers.host).toBeUndefined();
  const host = 'abc123def4.execute-api.us-east-1.amazonaws.com';
  await expect(executeApiAuthorization(host, '/dev/internal/v1/agent/chat/data/admit', headers, body)).resolves.toBe(headers.authorization);
  // A signature over the unstaged path would be rejected by API Gateway; prove the oracle distinguishes it.
  await expect(executeApiAuthorization(host, '/internal/v1/agent/chat/data/admit', headers, body)).resolves.not.toBe(headers.authorization);
  expect(JSON.stringify(request)).not.toContain('TESTSECRET');
});

test('accepts the API Gateway stage URL for a registered assignment and refuses other staged hosts', () => {
  const raw = '{"message_id":"run-a","session_id":"session-a","task_id":"task-a","session_generation":1}';
  expect(registeredSandboxAssignment(raw, assignment.image, STAGE_URL).gatewayUrl).toBe(STAGE_URL);
  for (const invalid of [`${STAGE_URL}/`, `${STAGE_URL}/agent`, 'https://gateway.example.test/dev',
    'https://abc123def4.execute-api.us-east-1.amazonaws.com:8443/dev', 'https://ABC.execute-api.us-east-1.amazonaws.com/dev']) {
    expect(() => registeredSandboxAssignment(raw, assignment.image, invalid)).toThrow('HTTPS origin or API Gateway stage URL');
  }
});

test.each([
  { ...assignment, gatewayUrl: 'http://gateway.example.test' },
  { ...assignment, gatewayUrl: 'https://gateway.example.test/redirect' },
  { ...assignment, gatewayUrl: 'https://gateway.example.test@evil.example.test' },
  { ...assignment, gatewayUrl: 'https://abc123def4.execute-api.eu-west-1.amazonaws.com/dev' },
  { ...assignment, gatewayUrl: 'https://abc123def4.execute-api.us-east-1.amazonaws.com/dev/agent' },
  { ...assignment, gatewayUrl: 'https://abc123def4.execute-api.us-east-1.amazonaws.com/dev/' },
  { ...assignment, gatewayUrl: 'https://abc123def4.execute-api.us-east-1.amazonaws.com:8443/dev' },
  { ...assignment, gatewayUrl: 'https://abc123def4.execute-api.us-east-1.amazonaws.com.evil.example.test/dev' },
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
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
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

function startClock() {
  let elapsed = 0;
  const sleep = jest.fn(async (ms: number) => { elapsed += ms; });
  return { timing: { now: () => elapsed, sleep }, sleep };
}

const pendingPod = { metadata: { ...pod }, status: { phase: 'Pending', containerStatuses: [{ name: 'chat-agent', state: { waiting: { reason: 'ContainerCreating' } } }] } };

test('admits the created pod only after Kubernetes reports it Running', async () => {
  const events: string[] = [];
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    create: jest.fn(async (_template: Parameters<SandboxPodApi['create']>[0]) => { events.push('create'); return { metadata: { ...template.metadata, ...pod }, spec: template.spec }; }),
    inspect: jest.fn(async (name: string, uid: string) => {
      events.push('inspect');
      return events.filter(event => event === 'inspect').length < 3 ? pendingPod : runningSandboxPod(name, uid);
    }),
    remove: jest.fn(async (_name: string, _uid: string) => { events.push('remove'); }),
  };
  const observed = jest.fn(async (url: string | URL | Request, _init?: RequestInit) => {
    events.push(String(url).split('/').pop()!);
    return new Response(JSON.stringify(receipt));
  }) as jest.MockedFunction<typeof fetch>;
  const { timing, sleep } = startClock();
  await expect(withAdmittedSandboxPod(assignment, api, identity, async () => 'done', observed, load, true, undefined, timing)).resolves.toBe('done');
  expect(events).toEqual(['create', 'inspect', 'inspect', 'inspect', 'admit', 'remove']);
  expect(sleep.mock.calls).toEqual([[500], [500]]);
  expect(observed).toHaveBeenCalledTimes(1);
});

test('retries admission while the gateway has not yet observed the running pod, then succeeds', async () => {
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    create: jest.fn(async (_template: Parameters<SandboxPodApi['create']>[0]) => ({ metadata: { ...template.metadata, ...pod }, spec: template.spec })),
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    remove: jest.fn(async (_name: string, _uid: string) => {}),
  };
  const notYetVisible = jest.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response(JSON.stringify(receipt))) as jest.MockedFunction<typeof fetch>;
  notYetVisible.mockResolvedValueOnce(new Response(JSON.stringify({ error: 'chat_scope_refused' }), { status: 404 }));
  const run = jest.fn(async () => 'done');
  const { timing, sleep } = startClock();
  await expect(withAdmittedSandboxPod(assignment, api, identity, run, notYetVisible, load, true, undefined, timing)).resolves.toBe('done');
  expect(notYetVisible).toHaveBeenCalledTimes(2);
  expect(notYetVisible.mock.calls.every(([url]) => String(url).endsWith('/admit'))).toBe(true);
  expect(JSON.parse(notYetVisible.mock.calls[1][1]!.body as string)).toEqual(JSON.parse(notYetVisible.mock.calls[0][1]!.body as string));
  expect(sleep.mock.calls).toEqual([[1000]]);
  expect(run).toHaveBeenCalledWith(pod, receipt);
  expect(api.remove).toHaveBeenCalledTimes(1);
});

test('gives up admission after a bounded number of 404 attempts and removes the pod', async () => {
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    create: jest.fn(async (_template: Parameters<SandboxPodApi['create']>[0]) => ({ metadata: { ...template.metadata, ...pod }, spec: template.spec })),
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    remove: jest.fn(async (_name: string, _uid: string) => {}),
  };
  const never = jest.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response('{}', { status: 404 })) as jest.MockedFunction<typeof fetch>;
  const run = jest.fn();
  const { timing, sleep } = startClock();
  await expect(withAdmittedSandboxPod(assignment, api, identity, run, never, load, true, undefined, timing)).rejects.toMatchObject({ status: 404, message: 'Chat sandbox admission refused' });
  expect(never).toHaveBeenCalledTimes(5);
  expect(sleep).toHaveBeenCalledTimes(4);
  expect(run).not.toHaveBeenCalled();
  expect(api.remove).toHaveBeenCalledWith(pod.name, pod.uid);
});

test.each([401, 403, 409, 422, 503])('does not retry a %s admission refusal', async status => {
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    create: jest.fn(async (_template: Parameters<SandboxPodApi['create']>[0]) => ({ metadata: { ...template.metadata, ...pod }, spec: template.spec })),
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    remove: jest.fn(async (_name: string, _uid: string) => {}),
  };
  const refused = jest.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response('{}', { status })) as jest.MockedFunction<typeof fetch>;
  const { timing, sleep } = startClock();
  await expect(withAdmittedSandboxPod(assignment, api, identity, jest.fn(), refused, load, true, undefined, timing)).rejects.toMatchObject({ status });
  expect(refused).toHaveBeenCalledTimes(1);
  expect(sleep).not.toHaveBeenCalled();
  expect(api.remove).toHaveBeenCalledWith(pod.name, pod.uid);
});

test('a pod that never starts is torn down before any admission request', async () => {
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    create: jest.fn(async (_template: Parameters<SandboxPodApi['create']>[0]) => ({ metadata: { ...template.metadata, ...pod }, spec: template.spec })),
    inspect: jest.fn(async (_name: string, _uid: string) => ({ metadata: { ...pod }, status: { phase: 'Failed' } })),
    remove: jest.fn(async (_name: string, _uid: string) => {}),
  };
  const run = jest.fn();
  await expect(withAdmittedSandboxPod(assignment, api, identity, run, send, load, false)).rejects.toThrow('Chat sandbox did not start');
  expect(send).not.toHaveBeenCalled();
  expect(load).not.toHaveBeenCalled();
  expect(run).not.toHaveBeenCalled();
  expect(api.remove).toHaveBeenCalledWith(pod.name, pod.uid);
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

test('supervisor selects Kiro Crew with the existing retained home and scoped run', async () => {
  const minted = '00000000-0000-4000-8000-000000000001';
  const persistent = { ...assignment, sessionMode: 'persistent' as const };
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    create: jest.fn(async requested => {
      expect(requested.spec.containers[0].env).toContainEqual({ name: 'ADP_EXECUTOR', value: 'kirocrew' });
      expect(requested.spec.containers[0].env).toContainEqual({ name: 'ADP_CREW_SCOPE', value: 'user' });
      expect(requested.spec.containers[0].env).toContainEqual({ name: 'ADP_RUN_ID', value: minted });
      expect(requested.spec.containers[0].volumeMounts).toContainEqual({ name: 'crew-home', mountPath: '/tmp/workspace' });
      expect(requested.spec.volumes).toContainEqual({ name: 'crew-home', persistentVolumeClaim: { claimName: `crew-home-${'a'.repeat(52)}` } });
      return { metadata: { ...requested.metadata, name: `${requested.metadata.generateName}abcde`, uid: pod.uid }, spec: requested.spec };
    }),
    remove: jest.fn(async (_name: string, _uid: string) => {}),
  };
  const steps: string[] = [];
  const signed = jest.fn(async (url: string | URL | Request, options?: RequestInit) => {
    if (String(url).endsWith('/crew-run')) {
      steps.push('prepare');
      expect(JSON.parse(options!.body as string).executor_scope).toBe('user');
      return new Response(JSON.stringify({ run_id: minted, lease_generation: 1, owner_key: 'a'.repeat(52), state: 'provisioning' }));
    }
    if (String(url).endsWith('/crew-run/lease')) {
      steps.push('lease');
      expect(JSON.parse(options!.body as string)).toEqual({ run_id: assignment.runId, envelope_digest: assignment.envelopeDigest,
        crew_run_id: minted, lease_generation: 1, pod_name: pod.name, pod_uid: pod.uid });
      return new Response(JSON.stringify({ run_id: minted, lease_generation: 1, state: 'provisioning' }));
    }
    if (String(url).endsWith('/crew-run/activate')) {
      steps.push('activate');
      expect(JSON.parse(options!.body as string)).toEqual({ run_id: assignment.runId, envelope_digest: assignment.envelopeDigest,
        crew_run_id: minted, lease_generation: 1, pod_name: pod.name, pod_uid: pod.uid });
      return new Response(JSON.stringify({ run_id: minted, lease_generation: 1 }));
    }
    if (String(url).endsWith('/crew-run/ready')) {
      steps.push('ready');
      return new Response(JSON.stringify({ run_id: minted, lease_generation: 1, state: 'ready' }));
    }
    steps.push('admit');
    return new Response(JSON.stringify(receipt));
  }) as unknown as typeof fetch;
  const prior = process.env.ADP_CHAT_PERSISTENT_EXECUTOR;
  process.env.ADP_CHAT_PERSISTENT_EXECUTOR = 'kirocrew';
  const priorScope = process.env.ADP_EXECUTOR_SCOPE;
  process.env.ADP_EXECUTOR_SCOPE = 'user';
  try {
    await withAdmittedSandboxPod(persistent, api, identity, async () => {}, signed, load);
    const refusedSteps: string[] = [];
    const refused = jest.fn(async (url: string | URL | Request) => {
      if (String(url).endsWith('/crew-run')) return new Response(JSON.stringify({ run_id: minted, lease_generation: 1, owner_key: 'a'.repeat(52), state: 'provisioning' }));
      if (String(url).endsWith('/crew-run/lease')) return new Response(JSON.stringify({ run_id: minted, lease_generation: 1, state: 'provisioning' }));
      if (String(url).endsWith('/crew-run/activate')) return new Response('{}', { status: 409 });
      refusedSteps.push('admit');
      return new Response(JSON.stringify(receipt));
    }) as unknown as typeof fetch;
    await expect(withAdmittedSandboxPod(persistent, api, identity, async () => {}, refused, load))
      .rejects.toThrow('Chat sandbox admission refused');
    expect(refusedSteps).toEqual([]);
  } finally {
    if (prior === undefined) delete process.env.ADP_CHAT_PERSISTENT_EXECUTOR;
    else process.env.ADP_CHAT_PERSISTENT_EXECUTOR = prior;
    if (priorScope === undefined) delete process.env.ADP_EXECUTOR_SCOPE;
    else process.env.ADP_EXECUTOR_SCOPE = priorScope;
  }
  expect(api.create).toHaveBeenCalledTimes(2);
  expect(steps).toEqual(['prepare', 'lease', 'activate', 'admit', 'ready']);
});

test('a user-scoped attachment admits the existing pod without creating or deleting another', async () => {
  const minted = '00000000-0000-4000-8000-000000000001';
  const existingPod = { name: 'chat-turn-ffffffffffff-abcde', uid: 'another-running-pod-uid' };
  const persistent = { ...assignment, sessionMode: 'persistent' as const, executor: 'kirocrew' as const };
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    create: jest.fn(async (_template: Parameters<SandboxPodApi['create']>[0]) => { throw new Error('second pod created'); }),
    remove: jest.fn(async (_name: string, _uid: string) => {}),
  };
  const serve = jest.fn(async () => 'attached');
  const requested = jest.fn(async (url: string | URL | Request, options?: RequestInit) => {
    if (String(url).endsWith('/crew-run')) return new Response(JSON.stringify({ run_id: minted,
      lease_generation: 1, owner_key: 'a'.repeat(52), pod_name: existingPod.name, pod_uid: existingPod.uid }));
    expect(String(url)).toContain('/data/admit');
    expect(JSON.parse(options!.body as string)).toEqual({ run_id: assignment.runId,
      envelope_digest: assignment.envelopeDigest, pod_name: existingPod.name, pod_uid: existingPod.uid });
    return new Response(JSON.stringify(receipt));
  }) as unknown as typeof fetch & jest.Mock;
  const previous = process.env.ADP_EXECUTOR_SCOPE;
  process.env.ADP_EXECUTOR_SCOPE = 'user';
  try {
    await expect(withAdmittedSandboxPod(persistent, api, identity, serve, requested, load)).resolves.toBe('attached');
    expect(serve).toHaveBeenCalledWith(existingPod, receipt, { ...persistent, verifiedCrewOwner: existingPod });
    expect(api.create).not.toHaveBeenCalled();
    expect(api.remove).not.toHaveBeenCalled();
    expect(api.resolveGateway).not.toHaveBeenCalled();
    for (const substituted of [{ ...existingPod, uid: 'different-pod-uid' }, pod]) {
      await expect(supervisorRequest({ ...persistent, verifiedCrewOwner: existingPod }, substituted, identity,
        SESSION_COMMIT_PATH, requested, load)).rejects.toThrow('dedicated identity unavailable');
    }
    const refused = jest.fn(async (url: string | URL | Request) => String(url).endsWith('/crew-run') ?
      new Response(JSON.stringify({ run_id: minted, lease_generation: 1, owner_key: 'a'.repeat(52),
        pod_name: existingPod.name, pod_uid: existingPod.uid })) : new Response('{}', { status: 409 })) as unknown as typeof fetch;
    await expect(withAdmittedSandboxPod(persistent, api, identity, serve, refused, load)).rejects.toMatchObject({ status: 409 });
    const partial = jest.fn(async () => new Response(JSON.stringify({ run_id: minted, lease_generation: 1,
      owner_key: 'a'.repeat(52), pod_name: existingPod.name }))) as unknown as typeof fetch;
    await expect(withAdmittedSandboxPod(persistent, api, identity, serve, partial, load))
      .rejects.toThrow('Crew run mint response invalid');
    expect(serve).toHaveBeenCalledTimes(1);
    expect(api.create).not.toHaveBeenCalled();
    expect(api.remove).not.toHaveBeenCalled();
  } finally {
    if (previous === undefined) delete process.env.ADP_EXECUTOR_SCOPE;
    else process.env.ADP_EXECUTOR_SCOPE = previous;
  }
});

test('two user-scoped sessions share one running pod after the first turn completes', async () => {
  const crewRunId = '00000000-0000-4000-8000-000000000001';
  const first = { ...assignment, sessionMode: 'persistent' as const, executor: 'kirocrew' as const, taskId: 'task-a', sessionGeneration: 1 };
  const second = { ...first, runId: 'run-b', sessionId: 'session-b', envelopeDigest: 'c'.repeat(64) };
  const pod = { name: sandboxCreationName(first.runId), uid: '01234567-89ab-cdef-0123-456789abcdef' };
  const admitted = new Set<string>();
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    create: jest.fn(async requested => ({ metadata: { ...requested.metadata, name: pod.name, uid: pod.uid }, spec: requested.spec })),
    remove: jest.fn(async (_name: string, _uid: string) => {}),
  };
  const send = jest.fn(async (url: string | URL | Request, options?: RequestInit) => {
    const body = JSON.parse(options!.body as string);
    const current = body.run_id === first.runId ? first : second;
    const scope = { run_id: current.runId, session_id: current.sessionId, task_id: current.taskId, session_generation: 1 };
    if (String(url).endsWith('/resume')) return new Response(JSON.stringify(admitted.has(current.runId) ? { ...scope,
      state: 'admitted', session_mode: 'persistent', session_run_id: current.runId, crew_owner_pod: true,
      pod_name: pod.name, sandbox_uid: pod.uid, image_digest: current.image.split('@')[1], attempt: 1, lease_generation: 1 } :
      { ...scope, state: 'unstarted' }));
    if (String(url).endsWith('/reserve')) return new Response(JSON.stringify({ ...scope, state: 'create', session_mode: 'persistent',
      attempt: 1, pod_name: sandboxCreationName(current.runId), image_digest: current.image.split('@')[1] }));
    if (String(url).endsWith('/crew-run')) return new Response(JSON.stringify({ run_id: crewRunId, lease_generation: 1,
      owner_key: 'a'.repeat(52), ...(body.run_id === first.runId ? { state: 'provisioning' } :
        { pod_name: pod.name, pod_uid: pod.uid }) }));
    if (String(url).endsWith('/crew-run/lease')) return new Response(JSON.stringify({ run_id: crewRunId,
      lease_generation: 1, state: 'provisioning' }));
    if (String(url).endsWith('/crew-run/activate')) return new Response(JSON.stringify({ run_id: crewRunId, lease_generation: 1 }));
    if (String(url).endsWith('/crew-run/ready')) return new Response(JSON.stringify({ run_id: crewRunId,
      lease_generation: 1, state: 'ready' }));
    if (String(url).endsWith('/session/commit')) {
      expect(body.pod_name).toBe(pod.name);
      expect(body.pod_uid).toBe(pod.uid);
      const terminal = { run_id: current.runId, session_id: current.sessionId, lease_generation: 1, attempt: 1,
        sandbox_uid: pod.uid, outcome: 'completed', message_id: 'message-a', retryable: false,
        automatic_replay_permitted: false, accounting_status: 'not_used', terminal: true, finalized_at: 1_800_000_000 };
      const completion = { ...scope, lease_generation: 1, attempt: 1, sandbox_uid: pod.uid,
        processing_lock_released: true, input_acknowledgement_ready: true, completed_at: terminal.finalized_at,
        delivery_id: `chat-terminal-${createHash('sha256').update(canonicalJson(terminal)).digest('hex')}` };
      return new Response(JSON.stringify({ terminal, completion }));
    }
    expect(String(url)).toContain('/data/admit');
    expect(body.pod_name).toBe(pod.name);
    expect(body.pod_uid).toBe(pod.uid);
    admitted.add(current.runId);
    return new Response(JSON.stringify({ run_id: body.run_id, session_id: body.run_id === first.runId ? first.sessionId : second.sessionId,
      lease_generation: 1 }));
  }) as unknown as jest.MockedFunction<typeof fetch>;
  const priorScope = process.env.ADP_EXECUTOR_SCOPE;
  const priorExecutor = process.env.ADP_CHAT_PERSISTENT_EXECUTOR;
  process.env.ADP_EXECUTOR_SCOPE = 'user';
  process.env.ADP_CHAT_PERSISTENT_EXECUTOR = 'kirocrew';
  try {
    await reconcileSupervisorDelivery(first, api, identity, send, load, {});
    await reconcileSupervisorDelivery(second, api, identity, send, load, {});
    await reconcileSupervisorDelivery(second, api, identity, send, load, {});
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(api.remove).not.toHaveBeenCalled();
    expect(send.mock.calls.filter(([url]) => String(url).endsWith('/crew-run'))).toHaveLength(2);
    expect(send.mock.calls.filter(([url]) => String(url).endsWith('/session/commit'))).toHaveLength(3);
  } finally {
    if (priorScope === undefined) delete process.env.ADP_EXECUTOR_SCOPE;
    else process.env.ADP_EXECUTOR_SCOPE = priorScope;
    if (priorExecutor === undefined) delete process.env.ADP_CHAT_PERSISTENT_EXECUTOR;
    else process.env.ADP_CHAT_PERSISTENT_EXECUTOR = priorExecutor;
  }
});

test('Crew activation signs the prepared owner run, generation and exact pod without granting admission', async () => {
  const prepared = { run_id: '00000000-0000-4000-8000-000000000001', lease_generation: 2 };
  const persistent = { ...assignment, sessionMode: 'persistent' as const, executor: 'kirocrew' as const };
  const requested = jest.fn(async (url: string | URL | Request, options?: RequestInit) => {
    const body = JSON.parse(options!.body as string);
    expect(String(url)).toBe(`${assignment.gatewayUrl}/internal/v1/agent/chat/crew-run/activate`);
    expect(body).toEqual({ run_id: assignment.runId, envelope_digest: assignment.envelopeDigest,
      crew_run_id: prepared.run_id, lease_generation: prepared.lease_generation, pod_name: pod.name, pod_uid: pod.uid });
    const signed = JSON.parse(Buffer.from((options!.headers as Record<string, string>)['X-Adp-Producer-Proof'], 'base64').toString());
    expect(signed['x-adp-work-invocation']).toBe(createHash('sha256').update(canonicalJson(body)).digest('hex'));
    return new Response(JSON.stringify({ run_id: prepared.run_id, lease_generation: prepared.lease_generation }));
  }) as unknown as typeof fetch & jest.Mock;
  const previous = process.env.ADP_EXECUTOR_SCOPE;
  process.env.ADP_EXECUTOR_SCOPE = 'user';
  try {
    await activateCrewRun(persistent, pod, prepared, identity, requested, load);
    expect(requested).toHaveBeenCalledTimes(1);
    await expect(activateCrewRun(persistent, pod, { ...prepared, run_id: 'untrusted' }, identity, requested, load))
      .rejects.toThrow('assignment or dedicated identity unavailable');
    expect(requested).toHaveBeenCalledTimes(1);
    const wrong = jest.fn(async () => new Response(JSON.stringify({ ...prepared, lease_generation: 3 }))) as unknown as typeof fetch;
    await expect(activateCrewRun(persistent, pod, prepared, identity, wrong, load)).rejects.toThrow('activation response invalid');
  } finally {
    if (previous === undefined) delete process.env.ADP_EXECUTOR_SCOPE;
    else process.env.ADP_EXECUTOR_SCOPE = previous;
  }
});

test.each([undefined, 'forged', 'A'.repeat(52)])('user-scoped Crew rejects an invalid server owner key: %s', async ownerKey => {
  const previous = process.env.ADP_EXECUTOR_SCOPE;
  process.env.ADP_EXECUTOR_SCOPE = 'user';
  const signed = jest.fn(async () => new Response(JSON.stringify({ run_id: '00000000-0000-4000-8000-000000000001',
    lease_generation: 1, ...(ownerKey === undefined ? {} : { owner_key: ownerKey }) }))) as unknown as typeof fetch;
  try {
    await expect(mintCrewRun({ ...assignment, sessionMode: 'persistent', executor: 'kirocrew' }, identity, signed, load))
      .rejects.toThrow('Crew run mint response invalid');
  } finally {
    if (previous === undefined) delete process.env.ADP_EXECUTOR_SCOPE;
    else process.env.ADP_EXECUTOR_SCOPE = previous;
  }
});

test('missing terminal proof fails closed at the supervisor deadline', async () => {
  const poll = jest.fn(async () => new Response(JSON.stringify({ run_id: assignment.runId,
    pod_uid: pod.uid, terminated: false }))) as unknown as typeof fetch;
  let elapsed = 0;
  await expect(waitForSandboxExit(assignment, pod, identity, poll, load,
    { now: () => elapsed, sleep: async () => { elapsed = 780_001; } })).rejects.toThrow('exit unverified');
  expect(poll).toHaveBeenCalledTimes(1);
});


test('mints persistent identity before pod creation and injects only the returned run ID', async () => {
  const minted = '00000000-0000-4000-8000-000000000001';
  const name = sandboxCreationName(assignment.runId);
  const persistent = { ...assignment, podName: name, sessionMode: 'persistent' as const, ownerKey: 'a'.repeat(52) };
  const events: string[] = [];
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    create: jest.fn(async requested => {
      events.push('create');
      expect(requested.spec.containers[0].env).toContainEqual({ name: 'ADP_RUN_ID', value: minted });
      expect(requested.spec.containers[0].env).toContainEqual({ name: 'ADP_EXECUTOR', value: 'claude-code' });
      expect(requested.spec.containers[0].volumeMounts).not.toContainEqual(expect.objectContaining({ name: 'crew-home' }));
      return { metadata: { ...requested.metadata, name, uid: pod.uid }, spec: requested.spec };
    }),
    remove: jest.fn(async (_name: string, _uid: string) => {}),
  };
  const signed = jest.fn(async (url: string | URL | Request, options?: RequestInit) => {
    const body = JSON.parse(options!.body as string);
    if (String(url).endsWith('/crew-run')) {
      events.push('mint');
      expect(body).toEqual({ run_id: assignment.runId, envelope_digest: assignment.envelopeDigest,
        session_id: assignment.sessionId, target_kind: 'eks-pod', target_ref: name, executor_scope: 'session' });
      const headers = options!.headers as Record<string, string>;
      const proof = JSON.parse(Buffer.from(headers['X-Adp-Producer-Proof'], 'base64').toString());
      expect(proof['x-adp-work-invocation']).toBe(createHash('sha256').update(JSON.stringify(body, Object.keys(body).sort())).digest('hex'));
      return new Response(JSON.stringify({ run_id: minted, lease_generation: 1 }));
    }
    events.push('admit');
    return new Response(JSON.stringify(receipt));
  }) as unknown as typeof fetch;
  const prior = process.env.ADP_CHAT_PERSISTENT_EXECUTOR;
  process.env.ADP_CHAT_PERSISTENT_EXECUTOR = 'claude-code';
  try { await withAdmittedSandboxPod(persistent, api, identity, async () => {}, signed, load); }
  finally {
    if (prior === undefined) delete process.env.ADP_CHAT_PERSISTENT_EXECUTOR;
    else process.env.ADP_CHAT_PERSISTENT_EXECUTOR = prior;
  }
  expect(events).toEqual(['mint', 'create', 'admit']);
  const denied = jest.fn(async () => new Response('refused', { status: 403 })) as unknown as typeof fetch;
  await expect(withAdmittedSandboxPod(persistent, api, identity, async () => {}, denied, load)).rejects.toThrow('admission refused');
  expect(api.create).toHaveBeenCalledTimes(1);
  const bad = jest.fn(async () => new Response(JSON.stringify({ run_id: 'forged', lease_generation: 1 }))) as unknown as typeof fetch;
  await expect(withAdmittedSandboxPod(persistent, api, identity, async () => {}, bad, load)).rejects.toThrow('mint response invalid');
  expect(api.create).toHaveBeenCalledTimes(1);
});

test('Crew readiness retries only native-startup refusal and checks the exact receipt', async () => {
  const crewRun = { run_id: '00000000-0000-4000-8000-000000000001', lease_generation: 1 };
  const persistent = { ...assignment, sessionMode: 'persistent' as const, executor: 'kirocrew' as const };
  const previous = process.env.ADP_EXECUTOR_SCOPE;
  process.env.ADP_EXECUTOR_SCOPE = 'user';
  let elapsed = 0;
  let attempts = 0;
  const signed = jest.fn(async (url: string | URL | Request, options?: RequestInit) => {
    expect(String(url)).toBe(`${assignment.gatewayUrl}/internal/v1/agent/chat/crew-run/ready`);
    expect(JSON.parse(options!.body as string)).toEqual({ run_id: assignment.runId,
      envelope_digest: assignment.envelopeDigest, crew_run_id: crewRun.run_id,
      lease_generation: 1, pod_name: pod.name, pod_uid: pod.uid });
    attempts++;
    return attempts === 1 ? new Response('{}', { status: 425 }) :
      new Response(JSON.stringify({ ...crewRun, state: 'ready' }));
  }) as unknown as typeof fetch;
  try {
    await waitForCrewReadiness(persistent, pod, crewRun, identity, signed, load,
      { now: () => elapsed, sleep: async (ms: number) => { elapsed += ms; } });
    expect(attempts).toBe(2);
    const wrong = jest.fn(async () => new Response(JSON.stringify({ ...crewRun, lease_generation: 2, state: 'ready' }))) as unknown as typeof fetch;
    await expect(waitForCrewReadiness(persistent, pod, crewRun, identity, wrong, load))
      .rejects.toThrow('Crew readiness response invalid');
    const denied = jest.fn(async () => new Response('{}', { status: 409 })) as unknown as typeof fetch;
    await expect(waitForCrewReadiness(persistent, pod, crewRun, identity, denied, load))
      .rejects.toMatchObject({ status: 409 });
    expect(denied).toHaveBeenCalledTimes(1);
    const pending = jest.fn(async () => new Response('{}', { status: 425 })) as unknown as typeof fetch;
    await expect(waitForCrewReadiness(persistent, pod, crewRun, identity, pending, load,
      { now: () => elapsed, sleep: async () => { elapsed += 60_000; } }))
      .rejects.toThrow('Crew native readiness unverified before supervisor deadline');
  } finally {
    if (previous === undefined) delete process.env.ADP_EXECUTOR_SCOPE;
    else process.env.ADP_EXECUTOR_SCOPE = previous;
  }
});

describe('gateway error bodies (#8047)', () => {
  test('a refused response carries a bounded copy of the gateway error body', async () => {
    const refused = jest.fn(async (_url: string | URL | Request, _init?: RequestInit) =>
      new Response(JSON.stringify({ detail: { error: 'chat_sandbox_removal_unconfirmed' } }), { status: 409 })) as jest.MockedFunction<typeof fetch>;
    await expect(admitSandboxPod(assignment, pod, identity, refused, load)).rejects.toMatchObject({
      status: 409, detail: '{"detail":{"error":"chat_sandbox_removal_unconfirmed"}}', message: 'Chat sandbox admission refused' });
    const oversized = jest.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response('x'.repeat(10_000), { status: 503 })) as jest.MockedFunction<typeof fetch>;
    const error = await admitSandboxPod(assignment, pod, identity, oversized, load).catch(e => e) as SupervisorResponseError;
    expect(error).toBeInstanceOf(SupervisorResponseError);
    expect(error.detail.length).toBeLessThanOrEqual(1024);
    const empty = jest.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response(null, { status: 404 })) as jest.MockedFunction<typeof fetch>;
    await expect(admitSandboxPod(assignment, pod, identity, empty, load)).rejects.toMatchObject({ status: 404, detail: '' });
  });

  test.each([
    [new SupervisorResponseError(409, '{"detail":{"error":"chat_sandbox_removal_unconfirmed"}}'), true],
    [new SupervisorResponseError(409, '{"error":"chat_sandbox_removal_unconfirmed"}'), true],
    [new SupervisorResponseError(409, 'chat sandbox removal not confirmed'), true],
    [new SupervisorResponseError(409, '{"detail":{"error":"chat_history_conflict"}}'), false],
    [new SupervisorResponseError(409, ''), false],
    [new SupervisorResponseError(409, 'not json {'), false],
    [new SupervisorResponseError(503, '{"detail":{"error":"chat_sandbox_removal_unconfirmed"}}'), false],
    [new Error('chat sandbox removal not confirmed'), false],
  ])('isRemovalUnconfirmed(%p) is %s', (error, expected) => {
    expect(isRemovalUnconfirmed(error)).toBe(expected);
  });
});


test('the session executor outranks the deployment default, which outranks sandbox-loop (#8115)', () => {
  expect(persistentExecutor({}, {})).toBe('sandbox-loop');
  expect(persistentExecutor({}, { ADP_CHAT_PERSISTENT_EXECUTOR: 'claude-code' })).toBe('claude-code');
  expect(persistentExecutor({ executor: 'sandbox-loop' }, { ADP_CHAT_PERSISTENT_EXECUTOR: 'claude-code' })).toBe('sandbox-loop');
  expect(persistentExecutor({ executor: 'claude-code' }, { ADP_CHAT_PERSISTENT_EXECUTOR: 'kirocrew' })).toBe('claude-code');
  expect(persistentExecutor({ executor: 'kirocrew' }, {})).toBe('kirocrew');
  expect(() => persistentExecutor({}, { ADP_CHAT_PERSISTENT_EXECUTOR: 'bogus' })).toThrow('Unsupported persistent chat executor selection');
  expect(() => persistentExecutor({ executor: 'bogus' as never }, {})).toThrow('Unsupported persistent chat executor selection');
});

test('a registered envelope or wake-up carries the session executor only for persistent sessions (#8115)', () => {
  const image = assignment.image;
  const gateway = assignment.gatewayUrl;
  const base = { message_id: 'run-a', session_id: 'session-a', task_id: 'task-a', session_generation: 1 };
  const persistent = registeredSandboxAssignment(JSON.stringify({ ...base, session_mode: 'persistent', executor: 'claude-code' }), image, gateway);
  expect(persistent.sessionMode).toBe('persistent');
  expect(persistent.executor).toBe('claude-code');
  expect(registeredSandboxAssignment(JSON.stringify({ ...base, session_mode: 'persistent' }), image, gateway).executor).toBeUndefined();
  expect(registeredSandboxAssignment(JSON.stringify(base), image, gateway).executor).toBeUndefined();
  for (const envelope of [
    { ...base, session_mode: 'ephemeral', executor: 'claude-code' },
    { ...base, executor: 'claude-code' },
    { ...base, session_mode: 'persistent', executor: 'bogus' },
    { ...base, session_mode: 'persistent', executor: 1 },
  ]) {
    expect(() => registeredSandboxAssignment(JSON.stringify(envelope), image, gateway)).toThrow('registered envelope unavailable');
  }
  const notice = { ...base, notification_version: 1, session_mode: 'persistent', envelope_digest: 'a'.repeat(64) };
  expect(registeredSandboxAssignment(JSON.stringify({ ...notice, executor: 'kirocrew' }), image, gateway).executor).toBe('kirocrew');
  expect(registeredSandboxAssignment(JSON.stringify(notice), image, gateway).executor).toBeUndefined();
  expect(() => registeredSandboxAssignment(JSON.stringify({ ...notice, executor: 'bogus' }), image, gateway)).toThrow('registered envelope unavailable');
  expect(() => registeredSandboxAssignment(JSON.stringify({ ...notice, executor: 'claude-code', extra: true }), image, gateway)).toThrow('notification unavailable');
});

test('the persistent pod runs the session executor even when the deployment default differs (#8115)', async () => {
  const minted = '00000000-0000-4000-8000-000000000001';
  const name = sandboxCreationName(assignment.runId);
  const created: string[] = [];
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    create: jest.fn(async requested => {
      const env = requested.spec.containers[0].env as Array<{ name: string; value?: string }>;
      created.push(env.find(entry => entry.name === 'ADP_EXECUTOR')?.value ?? '<none>');
      return { metadata: { ...requested.metadata, name, uid: pod.uid }, spec: requested.spec };
    }),
    remove: jest.fn(async (_name: string, _uid: string) => {}),
  };
  const signed = jest.fn(async (url: string | URL | Request) => String(url).endsWith('/crew-run') ?
    new Response(JSON.stringify({ run_id: minted, lease_generation: 1 })) : new Response(JSON.stringify(receipt))) as unknown as typeof fetch;
  const prior = process.env.ADP_CHAT_PERSISTENT_EXECUTOR;
  process.env.ADP_CHAT_PERSISTENT_EXECUTOR = 'sandbox-loop';
  try {
    const session = { ...assignment, podName: name, sessionMode: 'persistent' as const, ownerKey: 'a'.repeat(52), executor: 'claude-code' as const };
    await withAdmittedSandboxPod(session, api, identity, async () => {}, signed, load);
    const unselected = { ...assignment, podName: name, sessionMode: 'persistent' as const, ownerKey: 'a'.repeat(52) };
    await withAdmittedSandboxPod(unselected, api, identity, async () => {}, signed, load);
    delete process.env.ADP_CHAT_PERSISTENT_EXECUTOR;
    await withAdmittedSandboxPod(unselected, api, identity, async () => {}, signed, load);
    // An ephemeral assignment never names an executor on its pod (the gateway never puts one on
    // an ephemeral envelope and registeredSandboxAssignment refuses it).
    process.env.ADP_CHAT_PERSISTENT_EXECUTOR = 'claude-code';
    await withAdmittedSandboxPod(assignment, api, identity, async () => {}, signed, load);
  } finally {
    if (prior === undefined) delete process.env.ADP_CHAT_PERSISTENT_EXECUTOR;
    else process.env.ADP_CHAT_PERSISTENT_EXECUTOR = prior;
  }
  expect(created).toEqual(['claude-code', 'sandbox-loop', 'sandbox-loop', '<none>']);
});
