import { createHash } from 'node:crypto';
import { SQSClient, ReceiveMessageCommand, DeleteMessageCommand } from '@aws-sdk/client-sqs';
import { canonicalJson } from '../invocability-probe/canonical-json';
import type { fromTokenFile } from '@aws-sdk/credential-provider-web-identity';
import { createSupervisorQueue, runOneSupervisorDispatch } from './chat-supervisor';
import { buildSandboxPod, sandboxCreationName } from './sandbox-pod';
import { gatewayBinding, runningSandboxPod } from './sandbox-gateway.fixture';
import type { SandboxPodApi } from './sandbox-launcher';

const settings = {
  roleArn: 'arn:aws:iam::000000000000:role/chat-supervisor',
  workerRoleArn: 'arn:aws:iam::000000000000:role/worker',
  tokenFile: '/var/run/secrets/eks.amazonaws.com/serviceaccount/token', region: 'us-east-1',
  queueUrl: 'https://sqs.us-east-1.amazonaws.com/000000000000/chat-tasks.fifo',
  image: `registry.example.test/chat@sha256:${'a'.repeat(64)}`,
  gatewayUrl: 'https://gateway.example.test',
};
const body = '{"message_id":"run-a","session_id":"session-a","task_id":"task-a","session_generation":1}';
const assignment = { runId: 'run-a', image: settings.image, gatewayUrl: settings.gatewayUrl };
const template = buildSandboxPod(assignment, gatewayBinding);
const pod = { name: sandboxCreationName(assignment.runId), uid: '01234567-89ab-cdef-0123-456789abcdef' };
const terminal = { run_id: 'run-a', session_id: 'session-a', lease_generation: 1, attempt: 1,
  sandbox_uid: pod.uid, outcome: 'completed', message_id: 'reply-a', retryable: false,
  automatic_replay_permitted: false, accounting_status: 'settled', terminal: true, finalized_at: 1_800_000_000 };
const unstarted = { run_id: 'run-a', session_id: 'session-a', task_id: 'task-a', session_generation: 1, state: 'unstarted' };
const reservation = { ...unstarted, state: 'create', attempt: 1, pod_name: pod.name, image_digest: settings.image.split('@')[1] };
const completion = { run_id: 'run-a', session_id: 'session-a', task_id: 'task-a', session_generation: 1,
  attempt: 1, lease_generation: 1, sandbox_uid: pod.uid, processing_lock_released: true, input_acknowledgement_ready: true,
  completed_at: terminal.finalized_at, delivery_id: `chat-terminal-${createHash('sha256').update(canonicalJson(terminal)).digest('hex')}` };
const load = jest.fn(() => async () => ({ accessKeyId: 'TESTKEY', secretAccessKey: 'TESTSECRET', sessionToken: 'TESTSESSION' })) as unknown as jest.Mock & typeof fromTokenFile;
const send = jest.fn(async (url: string | URL | Request, _init?: RequestInit) => new Response(JSON.stringify(
  String(url).endsWith('/resume') ? unstarted : String(url).endsWith('/complete') ? completion :
    String(url).endsWith('/session/commit') ? { terminal, completion } :
    String(url).endsWith('/reserve') ? reservation :
    String(url).endsWith('/exit') ? { run_id: 'run-a', pod_uid: pod.uid, terminated: true } :
    String(url).endsWith('/teardown') ? { run_id: 'run-a', pod_uid: pod.uid, removed: true } :
    String(url).endsWith('/finalize') ? terminal :
    { run_id: 'run-a', session_id: 'session-a', lease_generation: 1 }))) as jest.MockedFunction<typeof fetch>;

function podApi(): SandboxPodApi {
  return { resolveGateway: jest.fn(async () => gatewayBinding),
  inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    create: jest.fn(async () => ({ metadata: { ...template.metadata, ...pod }, spec: template.spec })),
    remove: jest.fn(async () => {}) };
}

beforeEach(() => { load.mockClear(); send.mockClear(); });

test('receives only one FIFO delivery using the projected supervisor credentials', async () => {
  const mockSend = jest.spyOn(SQSClient.prototype, 'send').mockResolvedValue({ Messages: [{ Body: body, ReceiptHandle: 'receipt' }] } as never);
  try {
    const queue = createSupervisorQueue(settings, load);
    expect(await queue.receive()).toEqual([{ Body: body, ReceiptHandle: 'receipt' }]);
    expect(load).toHaveBeenCalledWith({ roleArn: settings.roleArn, webIdentityTokenFile: settings.tokenFile,
      clientConfig: { region: settings.region } });
    expect(mockSend.mock.calls).toHaveLength(1);
    expect(mockSend.mock.calls[0][0]).toBeInstanceOf(ReceiveMessageCommand);
    expect((mockSend.mock.calls[0][0] as ReceiveMessageCommand).input).toEqual({ QueueUrl: settings.queueUrl,
      MaxNumberOfMessages: 1, WaitTimeSeconds: 20, VisibilityTimeout: 900 });
    await queue.acknowledge('current-receipt');
    expect(mockSend.mock.calls[1][0]).toBeInstanceOf(DeleteMessageCommand);
    expect((mockSend.mock.calls[1][0] as DeleteMessageCommand).input).toEqual({ QueueUrl: settings.queueUrl, ReceiptHandle: 'current-receipt' });
  } finally { mockSend.mockRestore(); }
});

test.each([
  { roleArn: settings.workerRoleArn },
  { roleArn: 'arn:aws:iam::111111111111:role/chat-supervisor' },
  { tokenFile: '/tmp/fake-token' },
  { tokenFile: '/var/run/secrets/../escape' },
  { queueUrl: 'http://sqs.us-east-1.amazonaws.com/000000000000/chat-tasks.fifo' },
  { queueUrl: 'https://sqs.us-east-1.amazonaws.com/111111111111/chat-tasks.fifo' },
  { queueUrl: 'https://sqs.eu-west-1.amazonaws.com/000000000000/chat-tasks.fifo' },
  { queueUrl: 'https://sqs.us-east-1.amazonaws.com/000000000000/chat-tasks' },
  { queueUrl: 'https://sqs.us-east-1.amazonaws.com/000000000000/chat-tasks.fifo?x=1' },
  { image: 'registry.example.test/chat:latest' },
  { gatewayUrl: 'http://gateway.example.test' },
  { gatewayUrl: 'https://abc123def4.execute-api.us-east-1.amazonaws.com/dev/agent' },
  { gatewayUrl: 'https://abc123def4.execute-api.eu-west-1.amazonaws.com/dev' },
  { gatewayUrl: 'https://gateway.example.test/dev' },
])('refuses unsafe queue, identity or pod configuration before reading: %j', async override => {
  const queue = { receive: jest.fn(async () => []), acknowledge: jest.fn() };
  await expect(runOneSupervisorDispatch({ ...settings, ...override }, queue, podApi(), send, load)).rejects.toThrow();
  expect(queue.receive).not.toHaveBeenCalled();
});

test('returns idle without creating a pod if there is no delivery', async () => {
  const api = podApi();
  await expect(runOneSupervisorDispatch(settings, { receive: async () => [], acknowledge: jest.fn() }, api, send, load)).resolves.toBe('idle');
  expect(api.create).not.toHaveBeenCalled();
});

test('accepts the API Gateway stage URL in its own region as the gateway target', async () => {
  const staged = { ...settings, gatewayUrl: 'https://abc123def4.execute-api.us-east-1.amazonaws.com/dev' };
  const api = podApi();
  await expect(runOneSupervisorDispatch(staged, { receive: async () => [], acknowledge: jest.fn() }, api, send, load)).resolves.toBe('idle');
  expect(api.create).not.toHaveBeenCalled();
});

test('recovers a durable wake-up through the original protected digest without relaunching completed work', async () => {
  const digest = createHash('sha256').update(body).digest('hex');
  const notice = JSON.stringify({ ...JSON.parse(body), notification_version: 1, session_mode: 'persistent', envelope_digest: digest });
  const api = podApi();
  const queue = { receive: async () => [{ Body: notice, ReceiptHandle: 'recovered' }], acknowledge: jest.fn() };
  send.mockResolvedValueOnce(new Response(JSON.stringify({ ...unstarted, state: 'admitted', session_mode: 'persistent',
    session_run_id: 'run-a', pod_name: pod.name, sandbox_uid: pod.uid, attempt: 1, lease_generation: 1,
    image_digest: settings.image.split('@')[1] })));
  await expect(runOneSupervisorDispatch(settings, queue, api, send, load)).resolves.toBe('completed');
  expect(JSON.parse(send.mock.calls[0][1]!.body as string)).toEqual({ run_id: 'run-a', envelope_digest: digest });
  expect(api.create).not.toHaveBeenCalled();
  expect(queue.acknowledge).toHaveBeenCalledWith('recovered');
});

test('a durable wake-up may name the session executor the gateway stored (#8115)', async () => {
  const digest = createHash('sha256').update(body).digest('hex');
  const notice = JSON.stringify({ ...JSON.parse(body), notification_version: 1, session_mode: 'persistent', envelope_digest: digest, executor: 'claude-code' });
  const api = podApi();
  const queue = { receive: async () => [{ Body: notice, ReceiptHandle: 'recovered' }], acknowledge: jest.fn() };
  send.mockResolvedValueOnce(new Response(JSON.stringify({ ...unstarted, state: 'admitted', session_mode: 'persistent',
    session_run_id: 'run-a', pod_name: pod.name, sandbox_uid: pod.uid, attempt: 1, lease_generation: 1,
    image_digest: settings.image.split('@')[1] })));
  await expect(runOneSupervisorDispatch(settings, queue, api, send, load)).resolves.toBe('completed');
  expect(JSON.parse(send.mock.calls[0][1]!.body as string)).toEqual({ run_id: 'run-a', envelope_digest: digest });
  expect(api.create).not.toHaveBeenCalled();
  expect(queue.acknowledge).toHaveBeenCalledWith('recovered');
});

test.each([
  { notification_version: 2 }, { envelope_digest: 'invalid' }, { session_mode: 'ephemeral' }, { message: 'untrusted input' },
  { executor: 'bogus' }, { executor: 'claude-code', session_mode: 'ephemeral' },
])('rejects malformed durable notification before contacting the gateway: %j', async change => {
  const notice = JSON.stringify({ ...JSON.parse(body), notification_version: 1, session_mode: 'persistent', envelope_digest: 'a'.repeat(64), ...change });
  const api = podApi();
  await expect(runOneSupervisorDispatch(settings, { receive: async () => [{ Body: notice, ReceiptHandle: 'receipt' }], acknowledge: jest.fn() }, api, send, load)).rejects.toThrow();
  expect(send).not.toHaveBeenCalled();
  expect(api.create).not.toHaveBeenCalled();
});

test('a recovery hint with a mismatched protected digest cannot create or acknowledge work', async () => {
  const notice = JSON.stringify({ ...JSON.parse(body), notification_version: 1, session_mode: 'persistent', envelope_digest: 'b'.repeat(64) });
  send.mockResolvedValueOnce(new Response('{}', { status: 404 }));
  const api = podApi();
  const queue = { receive: async () => [{ Body: notice, ReceiptHandle: 'receipt' }], acknowledge: jest.fn() };
  await expect(runOneSupervisorDispatch(settings, queue, api, send, load)).rejects.toThrow();
  expect(api.create).not.toHaveBeenCalled();
  expect(queue.acknowledge).not.toHaveBeenCalled();
});

test.each([
  { messages: [{ Body: '{}', ReceiptHandle: 'receipt' }] },
  { messages: [{ Body: body }] },
  { messages: [{ Body: body, ReceiptHandle: '' }] },
  { messages: [{ Body: body, ReceiptHandle: 'receipt' }, { Body: body, ReceiptHandle: 'other' }] },
])('refuses malformed queue delivery without starting a pod', async ({ messages }) => {
  const api = podApi();
  await expect(runOneSupervisorDispatch(settings, { receive: async () => messages, acknowledge: jest.fn() }, api, send, load)).rejects.toThrow();
  expect(api.create).not.toHaveBeenCalled();
});

test('acknowledges only after matching completion follows exit, UID removal and finalization', async () => {
  const api = podApi();
  const queue = { receive: jest.fn(async () => [{ Body: body, ReceiptHandle: 'receipt' }]), acknowledge: jest.fn() };
  await expect(runOneSupervisorDispatch(settings, queue, api, send, load)).resolves.toBe('completed');
  expect(api.create).toHaveBeenCalledTimes(1);
  expect(api.remove).toHaveBeenCalledWith(pod.name, pod.uid);
  expect(send.mock.calls.map(call => String(call[0]).split('/').pop())).toEqual(['resume', 'reserve', 'admit', 'exit', 'teardown', 'finalize', 'complete']);
  const deletionOrder = (api.remove as jest.Mock).mock.invocationCallOrder[0];
  expect(deletionOrder).toBeGreaterThan(send.mock.invocationCallOrder[3]);
  expect(deletionOrder).toBeLessThan(send.mock.invocationCallOrder[4]);
  expect(queue.acknowledge).toHaveBeenCalledWith('receipt');
  expect(queue.acknowledge.mock.invocationCallOrder[0]).toBeGreaterThan(send.mock.invocationCallOrder[6]);
  const [, reserved] = send.mock.calls[1];
  expect(JSON.parse(reserved!.body as string)).toEqual({ run_id: 'run-a', envelope_digest: createHash('sha256').update(body).digest('hex'),
    image_digest: settings.image.split('@')[1] });
  const [, options] = send.mock.calls[2];
  expect(JSON.parse(options!.body as string)).toEqual({ run_id: 'run-a', envelope_digest: createHash('sha256').update(body).digest('hex'),
    pod_name: pod.name, pod_uid: pod.uid });
  for (const [, request] of send.mock.calls.slice(2)) {
    expect(request!.body).toBe(options!.body);
    expect(request!.headers).toEqual(expect.objectContaining({ 'X-Adp-Producer-Proof': expect.any(String) }));
  }
});

test('admits the sandbox only after the created pod is Running, and tolerates one gateway 404 before it sees the pod', async () => {
  const api = podApi();
  let reads = 0;
  (api.inspect as jest.Mock).mockImplementation(async (name: string, uid: string) => ++reads < 3
    ? { metadata: { name, uid }, status: { phase: 'Pending', containerStatuses: [{ name: 'chat-agent', state: { waiting: {} } }] } }
    : runningSandboxPod(name, uid));
  const original = send.getMockImplementation()!;
  let admits = 0;
  send.mockImplementation(async (url, init) => String(url).endsWith('/admit') && ++admits === 1
    ? new Response('{}', { status: 404 }) : original(url, init));
  let elapsed = 0;
  const timing = { now: () => elapsed, sleep: jest.fn(async (ms: number) => { elapsed += ms; }) };
  const queue = { receive: async () => [{ Body: body, ReceiptHandle: 'receipt' }], acknowledge: jest.fn() };
  try {
    await expect(runOneSupervisorDispatch(settings, queue, api, send, load, timing)).resolves.toBe('completed');
  } finally { send.mockImplementation(original); }
  const operations = send.mock.calls.map(call => String(call[0]).split('/').pop());
  expect(operations).toEqual(['resume', 'reserve', 'admit', 'admit', 'exit', 'teardown', 'finalize', 'complete']);
  const firstAdmit = send.mock.invocationCallOrder[2];
  expect((api.inspect as jest.Mock).mock.invocationCallOrder).toHaveLength(3);
  expect((api.inspect as jest.Mock).mock.invocationCallOrder.every(order => order < firstAdmit)).toBe(true);
  expect((api.create as jest.Mock).mock.invocationCallOrder[0]).toBeLessThan((api.inspect as jest.Mock).mock.invocationCallOrder[0]);
  expect(timing.sleep.mock.calls).toEqual([[500], [500], [1000]]);
  expect(queue.acknowledge).toHaveBeenCalledWith('receipt');
});

test('fails the turn, deletes the pod and leaves delivery pending when the sandbox never starts', async () => {
  const api = podApi();
  (api.inspect as jest.Mock).mockResolvedValue({ metadata: { ...pod }, status: { phase: 'Pending' } });
  let elapsed = 0;
  const timing = { now: () => elapsed, sleep: jest.fn(async (ms: number) => { elapsed += ms; }) };
  const queue = { receive: async () => [{ Body: body, ReceiptHandle: 'receipt' }], acknowledge: jest.fn() };
  await expect(runOneSupervisorDispatch(settings, queue, api, send, load, timing)).rejects.toThrow('Chat sandbox did not start');
  // After deleting the pod the supervisor resumes once more naming it (#8061); this gateway has no
  // cleanup to offer, so the original failure stands and the delivery stays pending.
  expect(send.mock.calls.map(call => String(call[0]).split('/').pop())).toEqual(['resume', 'reserve', 'resume']);
  expect(JSON.parse(String((send.mock.calls[2][1] as RequestInit).body))).toMatchObject({ run_id: 'run-a', pod_name: pod.name, pod_uid: pod.uid });
  expect(elapsed).toBe(60_000);
  expect(api.remove).toHaveBeenCalledWith(pod.name, pod.uid);
  expect(queue.acknowledge).not.toHaveBeenCalled();
});

test('creates a session-bound persistent pod only from the gateway reservation', async () => {
  const api: SandboxPodApi = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    create: jest.fn(async created => ({ metadata: { ...created.metadata, ...pod }, spec: created.spec })),
    remove: jest.fn(async () => {}),
  };
  send.mockResolvedValueOnce(new Response(JSON.stringify(unstarted)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...reservation, session_mode: 'persistent' })));
  const queue = { receive: async () => [{ Body: body, ReceiptHandle: 'receipt' }], acknowledge: jest.fn() };
  await runOneSupervisorDispatch(settings, queue, api, send, load);
  const created = (api.create as jest.Mock).mock.calls[0][0];
  expect(created.metadata.annotations['adp.io/session-hash']).toMatch(/^[a-f0-9]{64}$/);
  expect(created.metadata.labels).not.toHaveProperty(['adp.io/session-hash']);
  expect(created.spec).not.toHaveProperty('activeDeadlineSeconds');
  expect(api.remove).not.toHaveBeenCalled();
  expect(send.mock.calls.some(([url]) => String(url).endsWith('/session/commit'))).toBe(true);
  expect(queue.acknowledge).toHaveBeenCalledWith('receipt');
});

test('continues an admitted follow-up on the original pod without creating or removing it', async () => {
  const api = podApi();
  const followupBody = '{"message_id":"run-b","session_id":"session-a","task_id":"task-b","session_generation":2}';
  const nextTerminal = { ...terminal, run_id: 'run-b', lease_generation: 2 };
  const nextCompletion = { ...completion, run_id: 'run-b', task_id: 'task-b', session_generation: 2, lease_generation: 2,
    delivery_id: `chat-terminal-${createHash('sha256').update(canonicalJson(nextTerminal)).digest('hex')}` };
  const transport = jest.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response(JSON.stringify({ terminal: nextTerminal, completion: nextCompletion })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...unstarted, run_id: 'run-b', task_id: 'task-b', session_generation: 2,
      state: 'admitted', pod_name: pod.name, sandbox_uid: pod.uid, image_digest: settings.image.split('@')[1],
      attempt: 1, lease_generation: 2, session_mode: 'persistent', session_run_id: 'run-a' }))) as jest.MockedFunction<typeof fetch>;
  const queue = { receive: async () => [{ Body: followupBody, ReceiptHandle: 'followup-receipt' }], acknowledge: jest.fn() };
  await runOneSupervisorDispatch(settings, queue, api, transport, load);
  expect(api.create).not.toHaveBeenCalled();
  expect(api.remove).not.toHaveBeenCalled();
  expect(queue.acknowledge).toHaveBeenCalledWith('followup-receipt');
  expect(JSON.parse(transport.mock.calls[1][1]!.body as string)).toMatchObject({ run_id: 'run-b', pod_name: pod.name, pod_uid: pod.uid });
});

test('refuses to create a pod when the reservation disagrees with signed session mode', async () => {
  const api = podApi();
  const queue = { receive: async () => [{ Body: JSON.stringify({ ...JSON.parse(body), session_mode: 'persistent' }), ReceiptHandle: 'receipt' }],
    acknowledge: jest.fn() };
  send.mockResolvedValueOnce(new Response(JSON.stringify(unstarted)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...reservation, session_mode: 'ephemeral' })));
  await expect(runOneSupervisorDispatch(settings, queue, api, send, load)).rejects.toThrow('creation reservation invalid');
  expect(api.create).not.toHaveBeenCalled();
  expect(queue.acknowledge).not.toHaveBeenCalled();
});

test('rejects a swapped admission session without deleting a possibly admitted execution', async () => {
  const api = podApi();
  send.mockResolvedValueOnce(new Response(JSON.stringify(unstarted)));
  send.mockResolvedValueOnce(new Response(JSON.stringify(reservation)));
  send.mockResolvedValueOnce(new Response(JSON.stringify({ run_id: 'run-a', session_id: 'other', lease_generation: 1 })));
  await expect(runOneSupervisorDispatch(settings, { receive: async () => [{ Body: body, ReceiptHandle: 'receipt' }], acknowledge: jest.fn() }, api, send, load))
    .rejects.toThrow('admission response invalid');
  expect(api.remove).not.toHaveBeenCalled();
});


test('retains the admitted pod and leaves delivery pending if gateway cannot verify exit', async () => {
  const api = podApi();
  const queue = { receive: async () => [{ Body: body, ReceiptHandle: 'receipt' }], acknowledge: jest.fn() };
  send.mockResolvedValueOnce(new Response(JSON.stringify(unstarted)));
  send.mockResolvedValueOnce(new Response(JSON.stringify(reservation)));
  send.mockImplementationOnce(async () => new Response(JSON.stringify({ run_id: 'run-a', session_id: 'session-a', lease_generation: 1 })));
  send.mockResolvedValueOnce(new Response('', { status: 503 }));
  await expect(runOneSupervisorDispatch(settings, queue, api, send, load)).rejects.toThrow('admission refused');
  expect(api.remove).not.toHaveBeenCalled();
  expect(queue.acknowledge).not.toHaveBeenCalled();
});
