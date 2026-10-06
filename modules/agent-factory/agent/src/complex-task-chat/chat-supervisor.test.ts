import { createHash } from 'node:crypto';
import { SQSClient, ReceiveMessageCommand, DeleteMessageCommand } from '@aws-sdk/client-sqs';
import { canonicalJson } from '../invocability-probe/canonical-json';
import type { fromTokenFile } from '@aws-sdk/credential-provider-web-identity';
import { createSupervisorQueue, runOneSupervisorDispatch } from './chat-supervisor';
import { buildSandboxPod, sandboxCreationName } from './sandbox-pod';
import { gatewayBinding } from './sandbox-gateway.fixture';
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
    String(url).endsWith('/reserve') ? reservation :
    String(url).endsWith('/exit') ? { run_id: 'run-a', pod_uid: pod.uid, terminated: true } :
    String(url).endsWith('/teardown') ? { run_id: 'run-a', pod_uid: pod.uid, removed: true } :
    String(url).endsWith('/finalize') ? terminal :
    { run_id: 'run-a', session_id: 'session-a', lease_generation: 1 }))) as jest.MockedFunction<typeof fetch>;

function podApi(): SandboxPodApi {
  return { resolveGateway: jest.fn(async () => gatewayBinding),
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
