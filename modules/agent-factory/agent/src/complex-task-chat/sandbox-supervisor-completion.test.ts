import { createHash } from 'node:crypto';
import type { fromTokenFile } from '@aws-sdk/credential-provider-web-identity';
import { canonicalJson } from '../invocability-probe/canonical-json';
import { runOneSupervisorDispatch } from './chat-supervisor';
import { RETRY_LOG_LIMIT } from './sandbox-supervisor-completion';
import { buildSandboxPod, sandboxCreationName } from './sandbox-pod';
import { gatewayBinding, runningSandboxPod } from './sandbox-gateway.fixture';
import type { SandboxPodApi } from './sandbox-launcher';

const settings = { roleArn: 'arn:aws:iam::000000000000:role/chat-supervisor',
  tokenFile: '/var/run/secrets/chat-supervisor/token', region: 'us-east-1',
  queueUrl: 'https://sqs.us-east-1.amazonaws.com/000000000000/chat.fifo',
  image: `registry.example.test/chat@sha256:${'a'.repeat(64)}`, gatewayUrl: 'https://gateway.example.test' };
const envelope = { message_id: 'run-a', session_id: 'session-a', task_id: 'task-a', session_generation: 1_800_000_000 };
const template = buildSandboxPod({ runId: envelope.message_id, image: settings.image, gatewayUrl: settings.gatewayUrl }, gatewayBinding);
const pod = { name: sandboxCreationName(envelope.message_id), uid: '01234567-89ab-cdef-0123-456789abcdef' };
const admission = { run_id: 'run-a', session_id: 'session-a', lease_generation: 2 };
const terminal = { ...admission, attempt: 1, sandbox_uid: pod.uid, outcome: 'interrupted', message_id: null,
  retryable: false, automatic_replay_permitted: false, accounting_status: 'unresolved', terminal: true, finalized_at: 1_800_000_100 };
const completion = { ...admission, attempt: 1, sandbox_uid: pod.uid, task_id: envelope.task_id,
  session_generation: envelope.session_generation, processing_lock_released: true, input_acknowledgement_ready: true,
  delivery_id: `chat-terminal-${createHash('sha256').update(canonicalJson(terminal)).digest('hex')}`, completed_at: 1_800_000_110 };
const cleanedTerminal = { phase: 'pre_admission', run_id: envelope.message_id, session_id: envelope.session_id,
  attempt: 1, credential_epoch: 1, sandbox_uid: pod.uid, outcome: 'interrupted', message_id: null,
  terminal: true, retryable: true, automatic_replay_permitted: false, accounting_status: 'not_used', cleanup_required: false,
  finalized_at: 1_800_000_100 };
const cleanedCompletion = { phase: 'pre_admission', run_id: envelope.message_id, session_id: envelope.session_id,
  attempt: 1, credential_epoch: 1, sandbox_uid: pod.uid, task_id: envelope.task_id, session_generation: envelope.session_generation,
  terminal: cleanedTerminal, delivery_id: `chat-terminal-${createHash('sha256').update(canonicalJson(cleanedTerminal)).digest('hex')}`,
  processing_lock_released: true, input_acknowledgement_ready: true, completed_at: 1_800_000_110 };

function fixture(crash?: string) {
  let admitted = false;
  let reserved = false;
  let created = false;
  let removed = false;
  let elapsed = 0;
  let receipt = 'first-receipt';
  const events: string[] = [];
  const load = jest.fn(() => async () => ({ accessKeyId: 'TESTKEY', secretAccessKey: 'TESTSECRET', sessionToken: 'TESTSESSION' })) as unknown as typeof fromTokenFile;
  const timing = { now: () => elapsed, sleep: jest.fn(async (ms: number) => { elapsed += ms; }) };
  const api: jest.Mocked<SandboxPodApi> = {
    resolveGateway: jest.fn(async () => gatewayBinding),
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    create: jest.fn(async (requested: Parameters<SandboxPodApi['create']>[0]) => {
      expect(requested.metadata.name).toBe(pod.name);
      created = true;
      events.push('create');
      if (crash === 'create') { crash = undefined; throw new Error('creation response lost'); }
      return { metadata: { ...template.metadata, ...pod }, spec: template.spec };
    }),
    remove: jest.fn(async (_name: string, _uid: string) => { events.push('delete-pod'); if (removed) throw new Error('already absent'); removed = true; }),
  };
  const recovery = () => ({ run_id: 'run-a', session_id: 'session-a', task_id: 'task-a', session_generation: envelope.session_generation,
    ...(admitted ? { state: 'admitted', lease_generation: admission.lease_generation, attempt: 1, pod_name: pod.name, sandbox_uid: pod.uid, image_digest: settings.image.split('@')[1] }
      : reserved && created ? { state: 'pre_admission_cleanup', attempt: 1, pod_name: pod.name, sandbox_uid: pod.uid,
        image_digest: settings.image.split('@')[1], removed } : { state: 'unstarted' }) });
  const send = jest.fn(async (url: string | URL | Request, _request?: RequestInit) => {
    const operation = String(url).split('/').pop()!;
    events.push(operation);
    if (operation === 'resume' && reserved && !created) return new Response('{}', { status: 409 });
    if (operation === 'reserve') {
      if (reserved) return new Response('{}', { status: 409 });
      reserved = true;
    }
    if (operation === 'admit') admitted = true;
    const responses: Record<string, unknown> = { resume: recovery(), reserve: { run_id: 'run-a', session_id: 'session-a', task_id: 'task-a',
      session_generation: envelope.session_generation, state: 'create', attempt: 1, pod_name: pod.name, image_digest: settings.image.split('@')[1] },
      admit: admission, exit: { run_id: 'run-a', pod_uid: pod.uid, terminated: true },
      teardown: { run_id: 'run-a', pod_uid: pod.uid, removed }, finalize: terminal, complete: admitted ? completion : cleanedCompletion };
    if (operation === crash) { crash = undefined; throw new Error('supervisor lost after durable handoff'); }
    if (!(operation in responses)) throw new Error('unexpected operation');
    return new Response(JSON.stringify(responses[operation]));
  }) as jest.MockedFunction<typeof fetch>;
  const queue = { receive: jest.fn(async () => [{ Body: JSON.stringify(envelope), ReceiptHandle: receipt }]),
    acknowledge: jest.fn(async (_handle: string) => {
      events.push('acknowledge');
      if (crash === 'acknowledge') { crash = undefined; throw new Error('queue reply lost'); }
    }) };
  const run = () => runOneSupervisorDispatch(settings, queue, api, send, load, timing);
  const redeliver = () => { receipt = 'redelivery-receipt'; return run(); };
  return { run, redeliver, api, queue, send, timing, events, recovery, load };
}

const queuedTerminal = { phase: 'queued', run_id: 'run-a', session_id: 'session-a', attempt: 1, credential_epoch: 1,
  outcome: 'cancelled', message_id: null, terminal: true, retryable: false, automatic_replay_permitted: false,
  accounting_status: 'not_used', cleanup_required: true, finalized_at: 1_800_000_001 };
const queuedCompletion = { phase: 'queued', run_id: 'run-a', session_id: 'session-a', attempt: 1, credential_epoch: 1,
  terminal: queuedTerminal, creation_fenced: true, task_id: 'task-a', session_generation: envelope.session_generation,
  delivery_id: `chat-terminal-${createHash('sha256').update(canonicalJson(queuedTerminal)).digest('hex')}`,
  processing_lock_released: true, input_acknowledgement_ready: true, completed_at: 1_800_000_002 };

function queuedFixture(crash?: string, terminal = queuedTerminal) {
  const state = fixture(crash === 'acknowledge' ? crash : undefined);
  const original = state.send.getMockImplementation()!;
  state.send.mockImplementation(async (url, request) => {
    if (!String(url).endsWith('/resume')) return original(url, request);
    state.events.push('resume');
    if (crash === 'resume') { crash = undefined; throw new Error('worker lost after queued completion'); }
    return new Response(JSON.stringify({ run_id: envelope.message_id, session_id: envelope.session_id,
      task_id: envelope.task_id, session_generation: envelope.session_generation,
      state: 'queued_completed', completion: { ...queuedCompletion, terminal,
        delivery_id: `chat-terminal-${createHash('sha256').update(canonicalJson(terminal)).digest('hex')}` } }));
  });
  return state;
}

test.each([
  ['cancelled', 'none'], ['cancelled', 'resume'], ['cancelled', 'acknowledge'],
  ['interrupted', 'none'], ['interrupted', 'resume'], ['interrupted', 'acknowledge'],
])('completes unreserved %s outcome after %s loss without touching pods', async (outcome, stage) => {
  const state = queuedFixture(stage, { ...queuedTerminal, outcome, retryable: outcome === 'interrupted' });
  if (stage === 'none') await expect(state.run()).resolves.toBe('completed');
  else await expect(state.run()).rejects.toThrow();
  if (stage === 'resume') expect(state.queue.acknowledge).not.toHaveBeenCalled();
  await expect(state.redeliver()).resolves.toBe('completed');
  expect(state.api.create).not.toHaveBeenCalled();
  expect(state.api.remove).not.toHaveBeenCalled();
  expect(state.api.resolveGateway).not.toHaveBeenCalled();
  expect(state.events.every(event => ['resume', 'acknowledge'].includes(event))).toBe(true);
  expect(state.queue.acknowledge).toHaveBeenLastCalledWith('redelivery-receipt');
});

test.each([
  { phase: 'pre_admission' }, { run_id: 'foreign' }, { session_id: 'foreign' }, { task_id: 'foreign' }, { session_generation: 1 },
  { attempt: 2 }, { credential_epoch: 2 }, { sandbox_uid: 'foreign' }, { lease_generation: 1 }, { creation_fenced: false },
  { delivery_id: 'foreign' }, { processing_lock_released: false }, { input_acknowledgement_ready: false }, { completed_at: 1 },
  { terminal: null }, { terminal: { ...queuedTerminal, outcome: 'interrupted' } }, { terminal: { ...queuedTerminal, retryable: true } },
  { terminal: { ...queuedTerminal, cleanup_required: false } }, { terminal: { ...queuedTerminal, sandbox_uid: 'foreign' } },
  { terminal: { ...queuedTerminal, credential_epoch: 0 } }, { terminal: { ...queuedTerminal, accounting_status: 'unresolved' } },
])('refuses forged queued completion without acknowledgement: %j', async change => {
  const state = queuedFixture();
  state.send.mockResolvedValue(new Response(JSON.stringify({ run_id: envelope.message_id, session_id: envelope.session_id,
    task_id: envelope.task_id, session_generation: envelope.session_generation,
    state: 'queued_completed', completion: { ...queuedCompletion, ...change } })));
  await expect(state.run()).rejects.toThrow('queued completion receipt invalid');
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
  expect(state.api.create).not.toHaveBeenCalled();
});

test.each([403, 404, 409, 503])('withholds queued acknowledgement while recovery is refused or pending: %s', async status => {
  const state = queuedFixture();
  state.send.mockImplementation(async () => new Response('{}', { status }));
  await expect(state.run()).rejects.toThrow();
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
  expect(state.api.create).not.toHaveBeenCalled();
});

test('a lost creation response completes the original reserved pod and acknowledges only redelivery', async () => {
  const state = fixture('create');
  await expect(state.run()).rejects.toThrow('creation response lost');
  expect(state.api.remove).not.toHaveBeenCalled();
  await expect(state.redeliver()).resolves.toBe('completed');
  await expect(state.redeliver()).resolves.toBe('completed');
  expect(state.api.create).toHaveBeenCalledTimes(1);
  expect(state.events.filter(event => event === 'reserve')).toHaveLength(1);
  expect(state.events.filter(event => ['admit', 'finalize'].includes(event))).toEqual([]);
  expect(state.api.remove.mock.calls.every(args => args[0] === pod.name && args[1] === pod.uid)).toBe(true);
  expect(state.queue.acknowledge.mock.calls).toEqual([['redelivery-receipt'], ['redelivery-receipt']]);
});

test('a lost reservation response cannot authorize creation on retry', async () => {
  const state = fixture('reserve');
  await expect(state.run()).rejects.toThrow();
  await expect(state.redeliver()).rejects.toThrow();
  expect(state.events.filter(event => event === 'reserve')).toHaveLength(1);
  expect(state.api.create).not.toHaveBeenCalled();
  expect(state.api.remove).not.toHaveBeenCalled();
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
});

test.each([{ pod_name: 'chat-turn-ffffffffffff-abcde' }, { image_digest: 'sha256:' + 'b'.repeat(64) },
  { session_generation: 1 }, { attempt: 0 }, { state: 'unstarted' }])('rejects substituted creation reservation: %j', async change => {
  const state = fixture();
  const original = state.send.getMockImplementation()!;
  state.send.mockImplementation(async (url, request) => {
    const response = await original(url, request);
    return String(url).endsWith('/reserve') ? new Response(JSON.stringify({ ...await response.json() as Record<string, unknown>, ...change })) : response;
  });
  await expect(state.run()).rejects.toThrow('creation reservation invalid');
  expect(state.api.create).not.toHaveBeenCalled();
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
});

test.each(['none', 'delete-response', 'teardown-response'])(
  'completes the original partially bound pod without admission after %s loss', async loss => {
    const state = fixture(loss === 'teardown-response' ? 'teardown' : undefined);
    const original = state.send.getMockImplementation()!;
    const cleanup = { run_id: 'run-a', session_id: 'session-a', task_id: 'task-a', session_generation: envelope.session_generation,
      state: 'pre_admission_cleanup', attempt: 1, pod_name: pod.name, sandbox_uid: pod.uid,
      image_digest: settings.image.split('@')[1], removed: false };
    state.send.mockImplementation(async (url, request) => String(url).endsWith('/resume') ?
      new Response(JSON.stringify(cleanup)) : original(url, request));
    if (loss === 'delete-response') {
      const remove = state.api.remove.getMockImplementation()!;
      state.api.remove.mockImplementation(async (name, uid) => { await remove(name, uid); throw new Error('delete response lost'); });
    }
    if (loss === 'teardown-response') await expect(state.run()).rejects.toThrow();
    else await expect(state.run()).resolves.toBe('completed');
    await expect(state.redeliver()).resolves.toBe('completed');
    expect(state.api.create).not.toHaveBeenCalled();
    expect(state.api.resolveGateway).not.toHaveBeenCalled();
    expect(state.api.remove.mock.calls).toEqual([[pod.name, pod.uid], [pod.name, pod.uid]]);
    expect(state.events.filter(event => ['admit', 'finalize'].includes(event))).toEqual([]);
    expect(state.queue.acknowledge).toHaveBeenLastCalledWith('redelivery-receipt');
    expect(state.queue.acknowledge).toHaveBeenCalledTimes(loss === 'teardown-response' ? 1 : 2);
  },
);

function cleanedFixture(crash?: string) {
  const state = fixture(crash);
  const original = state.send.getMockImplementation()!;
  state.send.mockImplementation(async (url, request) => String(url).endsWith('/resume') ? new Response(JSON.stringify({
    run_id: envelope.message_id, session_id: envelope.session_id, task_id: envelope.task_id,
    session_generation: envelope.session_generation, state: 'pre_admission_cleanup', attempt: 1,
    pod_name: pod.name, sandbox_uid: pod.uid, image_digest: settings.image.split('@')[1], removed: true,
  })) : original(url, request));
  return state;
}

test.each(['complete', 'acknowledge'])('retries cleaned completion after %s loss without re-admission', async stage => {
  const state = cleanedFixture(stage);
  await expect(state.run()).rejects.toThrow();
  if (stage === 'complete') expect(state.queue.acknowledge).not.toHaveBeenCalled();
  await expect(state.redeliver()).resolves.toBe('completed');
  expect(state.api.create).not.toHaveBeenCalled();
  expect(state.events.filter(event => ['reserve', 'admit', 'finalize'].includes(event))).toEqual([]);
  expect(state.queue.acknowledge).toHaveBeenLastCalledWith('redelivery-receipt');
  expect(state.events.slice(-2)).toEqual(['complete', 'acknowledge']);
});

test.each([
  { phase: 'admitted' }, { run_id: 'other-run' }, { task_id: 'other-task' }, { session_generation: 1 },
  { attempt: 2 }, { credential_epoch: 2 }, { sandbox_uid: 'other-pod' }, { lease_generation: 1 },
  { delivery_id: 'chat-terminal-foreign' }, { processing_lock_released: false }, { input_acknowledgement_ready: false },
  { completed_at: 1 }, { terminal: null }, { terminal: { ...cleanedTerminal, retryable: false } },
  { terminal: { ...cleanedTerminal, accounting_status: 'unresolved' } }, { terminal: { ...cleanedTerminal, lease_generation: 1 } },
])('rejects substituted cleaned completion without acknowledgement: %j', async change => {
  const state = cleanedFixture();
  const original = state.send.getMockImplementation()!;
  state.send.mockImplementation(async (url, request) => String(url).endsWith('/complete') ?
    new Response(JSON.stringify({ ...cleanedCompletion, ...change })) : original(url, request));
  await expect(state.run()).rejects.toThrow('cleaned completion receipt invalid');
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
  expect(state.api.create).not.toHaveBeenCalled();
});

test.each([403, 404, 409, 503])('withholds cleaned acknowledgement while completion is refused or pending: %s', async status => {
  const state = cleanedFixture();
  const original = state.send.getMockImplementation()!;
  state.send.mockImplementation(async (url, request) => String(url).endsWith('/complete') ? new Response('{}', { status }) : original(url, request));
  await expect(state.run()).rejects.toThrow();
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
  expect(state.api.create).not.toHaveBeenCalled();
});

test.each([{ state: ['admitted'] }, { sandbox_uid: 'foreign' }, { pod_name: 'foreign' }, { image_digest: 'sha256:' + 'b'.repeat(64) },
  { session_generation: 1 }, { removed: 'true' }, { lease_generation: 1 }])('rejects substituted cleanup receipt: %j', async change => {
  const state = fixture();
  state.send.mockResolvedValue(new Response(JSON.stringify({ run_id: 'run-a', session_id: 'session-a', task_id: 'task-a',
    session_generation: envelope.session_generation, state: 'pre_admission_cleanup', attempt: 1,
    pod_name: pod.name, sandbox_uid: pod.uid, image_digest: settings.image.split('@')[1], removed: false, ...change })));
  await expect(state.run()).rejects.toThrow();
  expect(state.api.create).not.toHaveBeenCalled();
  expect(state.api.remove).not.toHaveBeenCalled();
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
});

test('unverified partial-binding deletion remains unacknowledged', async () => {
  const state = fixture();
  state.send.mockImplementation(async url => new Response(JSON.stringify(String(url).endsWith('/resume') ? {
    run_id: 'run-a', session_id: 'session-a', task_id: 'task-a', session_generation: envelope.session_generation,
    state: 'pre_admission_cleanup', attempt: 1, pod_name: pod.name, sandbox_uid: pod.uid,
    image_digest: settings.image.split('@')[1], removed: false,
  } : { run_id: 'run-a', pod_uid: pod.uid, removed: false })));
  state.api.remove.mockRejectedValue(new Error('API unavailable'));
  await expect(state.run()).rejects.toThrow('removal unverified');
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
});

test.each(['admit', 'exit', 'teardown', 'finalize', 'complete', 'acknowledge'])(
  'resumes the original admitted turn after loss at %s without another sandbox or inference', async stage => {
    const state = fixture(stage);
    await expect(state.run()).rejects.toThrow();
    if (stage !== 'acknowledge') expect(state.queue.acknowledge).not.toHaveBeenCalled();
    if (stage === 'admit' || stage === 'exit') expect(state.api.remove).not.toHaveBeenCalled();
    await expect(state.redeliver()).resolves.toBe('completed');
    expect(state.api.create).toHaveBeenCalledTimes(1);
    expect(state.api.resolveGateway).toHaveBeenCalledTimes(1);
    expect(state.events.filter(event => event === 'admit')).toHaveLength(1);
    expect(state.queue.acknowledge).toHaveBeenLastCalledWith('redelivery-receipt');
    expect(state.api.remove.mock.calls.every(args => args[0] === pod.name && args[1] === pod.uid)).toBe(true);
    expect(state.events.slice(-2)).toEqual(['complete', 'acknowledge']);
  },
);

test('a completed turn can be redelivered after acknowledgement without replay', async () => {
  const state = fixture();
  await expect(state.run()).resolves.toBe('completed');
  await expect(state.redeliver()).resolves.toBe('completed');
  expect(state.api.create).toHaveBeenCalledTimes(1);
  expect(state.events.filter(event => event === 'admit')).toHaveLength(1);
  expect(state.queue.acknowledge.mock.calls).toEqual([['first-receipt'], ['redelivery-receipt']]);
});

test.each([
  { run_id: 'other' }, { session_id: 'other' }, { task_id: 'other' }, { session_generation: 1 },
  { attempt: 2 }, { lease_generation: 1 }, { sandbox_uid: 'other-pod' }, { delivery_id: 'chat-terminal-' + 'f'.repeat(64) },
  { processing_lock_released: false }, { input_acknowledgement_ready: false }, { input_acknowledgement_ready: 1 },
  { completed_at: terminal.finalized_at - 1 }, { completed_at: 1.5 },
])('refuses completion substitution without acknowledgement: %j', async change => {
  const state = fixture();
  const original = state.send.getMockImplementation()!;
  state.send.mockImplementation(async (url, request) => String(url).endsWith('/complete') ?
    new Response(JSON.stringify({ ...completion, ...change })) : original(url, request));
  await expect(state.run()).rejects.toThrow('completion receipt invalid');
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
});

test.each([
  { run_id: 'other' }, { session_id: 'other' }, { task_id: 'other' }, { session_generation: 1 }, { state: 'unknown' },
  { pod_name: 'chat-turn-ffffffffffff-abcde' }, { sandbox_uid: '../escape' }, { image_digest: 'sha256:' + 'b'.repeat(64) },
  { attempt: 0 }, { lease_generation: 0 },
])('refuses recovery substitution before pod creation or removal: %j', async change => {
  const state = fixture('admit');
  await expect(state.run()).rejects.toThrow();
  state.api.create.mockClear();
  const original = state.send.getMockImplementation()!;
  state.send.mockImplementation(async (url, request) => String(url).endsWith('/resume') ?
    new Response(JSON.stringify({ ...state.recovery(), ...change })) : original(url, request));
  await expect(state.redeliver()).rejects.toThrow('recovery');
  expect(state.api.create).not.toHaveBeenCalled();
  expect(state.api.remove).not.toHaveBeenCalled();
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
});

test.each([409, 503, 'lost-response'])('waits for completion and retries ambiguous handoff: %s', async failure => {
  const state = fixture();
  let attempts = 0;
  const original = state.send.getMockImplementation()!;
  state.send.mockImplementation(async (url, request) => {
    if (String(url).endsWith('/complete') && attempts++ < 2) {
      if (failure === 'lost-response') throw new TypeError('fetch failed');
      return new Response('pending', { status: failure as number });
    }
    return original(url, request);
  });
  await expect(state.run()).resolves.toBe('completed');
  expect(attempts).toBe(3);
  expect(state.api.create).toHaveBeenCalledTimes(1);
  expect(state.queue.acknowledge).toHaveBeenCalledTimes(1);
});

test.each([{ attempt: 2 }, { lease_generation: 3 }])('fences stale terminal state after recovery: %j', async change => {
  const state = fixture('admit');
  await expect(state.run()).rejects.toThrow();
  const original = state.send.getMockImplementation()!;
  state.send.mockImplementation(async (url, request) => String(url).endsWith('/resume') ?
    new Response(JSON.stringify({ ...state.recovery(), ...change })) : original(url, request));
  await expect(state.redeliver()).rejects.toThrow(/terminal.*(invalid|changed)/);
  expect(state.api.create).toHaveBeenCalledTimes(1);
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
});

test.each([403, 404, 409, 503])('never acknowledges missing or refused completion: %s', async status => {
  const state = fixture();
  const original = state.send.getMockImplementation()!;
  state.send.mockImplementation(async (url, request) => String(url).endsWith('/complete') ? new Response('unavailable', { status }) : original(url, request));
  await expect(state.run()).rejects.toThrow();
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
  expect(state.api.create).toHaveBeenCalledTimes(1);
});

test('never starts or acknowledges when trusted recovery is refused', async () => {
  const state = fixture();
  state.send.mockResolvedValueOnce(new Response('refused', { status: 403 }));
  await expect(state.run()).rejects.toThrow('admission refused');
  expect(state.api.create).not.toHaveBeenCalled();
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
});

test('a sandbox that ended its turn with a refused model-policy decision is finalized as failed and released (#7968)', async () => {
  const state = fixture();
  const failedTerminal = { ...terminal, outcome: 'failed', retryable: false, accounting_status: 'not_used' };
  const failedCompletion = { ...completion,
    delivery_id: `chat-terminal-${createHash('sha256').update(canonicalJson(failedTerminal)).digest('hex')}` };
  const original = state.send.getMockImplementation()!;
  state.send.mockImplementation(async (url, request) => {
    const operation = String(url).split('/').pop();
    if (operation !== 'finalize' && operation !== 'complete') return original(url, request);
    state.events.push(operation);
    return new Response(JSON.stringify(operation === 'finalize' ? failedTerminal : failedCompletion));
  });
  await expect(state.run()).resolves.toBe('completed');
  expect(state.events.slice(state.events.indexOf('exit'))).toEqual(['exit', 'delete-pod', 'teardown', 'finalize', 'complete', 'acknowledge']);
  expect(state.api.remove).toHaveBeenCalledWith(pod.name, pod.uid);
  expect(state.queue.acknowledge).toHaveBeenCalledWith('first-receipt');
});

test('a failed terminal whose completion receipt does not match is not acknowledged', async () => {
  const state = fixture();
  const failedTerminal = { ...terminal, outcome: 'failed', retryable: false, accounting_status: 'not_used' };
  const original = state.send.getMockImplementation()!;
  state.send.mockImplementation(async (url, request) => String(url).endsWith('/finalize')
    ? new Response(JSON.stringify(failedTerminal)) : original(url, request));
  await expect(state.run()).rejects.toThrow('completion receipt invalid');
  expect(state.queue.acknowledge).not.toHaveBeenCalled();
});

describe('completion retries (#8047)', () => {
  const removalUnconfirmed = () => new Response(JSON.stringify({ detail: { error: 'chat_sandbox_removal_unconfirmed' } }), { status: 409 });

  test('re-awaits removal and retries when complete answers 409 removal-unconfirmed, then acknowledges once', async () => {
    const state = fixture();
    const original = state.send.getMockImplementation()!;
    let completes = 0;
    state.send.mockImplementation(async (url, request) => {
      if (!String(url).endsWith('/complete') || ++completes !== 1) return original(url, request);
      state.events.push('complete-refused');
      return removalUnconfirmed();
    });
    await expect(state.run()).resolves.toBe('completed');
    expect(completes).toBe(2);
    expect(state.events.slice(state.events.indexOf('finalize'))).toEqual(['finalize', 'complete-refused', 'teardown', 'complete', 'acknowledge']);
    expect(state.queue.acknowledge).toHaveBeenCalledTimes(1);
    expect(state.timing.sleep).toHaveBeenCalledWith(1000);
  });

  test('also recognises the gateway message in a plain body', async () => {
    const state = fixture();
    const original = state.send.getMockImplementation()!;
    let completes = 0;
    state.send.mockImplementation(async (url, request) => String(url).endsWith('/complete') && ++completes === 1 ?
      new Response('chat sandbox removal not confirmed', { status: 409 }) : original(url, request));
    await expect(state.run()).resolves.toBe('completed');
    expect(state.events.filter(event => event === 'teardown')).toHaveLength(2);
  });

  test('stops within the completion window, exits non-zero naming the gateway body, and withholds acknowledgement', async () => {
    const state = fixture();
    const original = state.send.getMockImplementation()!;
    let completes = 0;
    state.send.mockImplementation(async (url, request) => String(url).endsWith('/complete') ? (completes++, removalUnconfirmed()) : original(url, request));
    await expect(state.run()).rejects.toThrow(/completion deadline exceeded.*HTTP 409.*chat_sandbox_removal_unconfirmed/);
    expect(state.queue.acknowledge).not.toHaveBeenCalled();
    expect(completes).toBeLessThanOrEqual(20);
    expect(completes).toBeGreaterThan(3);
    expect(state.timing.now()).toBeLessThan(900_000 - 60_000);
    // Redelivery after the visibility window resumes the admitted turn without another sandbox.
    state.send.mockImplementation(original);
    await expect(state.redeliver()).resolves.toBe('completed');
    expect(state.api.create).toHaveBeenCalledTimes(1);
  });

  test('a persistent generic 409 is retried with growing backoff, not every second, and ends non-zero', async () => {
    const state = fixture();
    const original = state.send.getMockImplementation()!;
    let completes = 0;
    state.send.mockImplementation(async (url, request) => String(url).endsWith('/complete') ?
      (completes++, new Response(JSON.stringify({ detail: { error: 'chat_history_conflict' } }), { status: 409 })) : original(url, request));
    await expect(state.run()).rejects.toThrow(/completion deadline exceeded.*chat_history_conflict/);
    expect(completes).toBeLessThanOrEqual(20);
    const delays = state.timing.sleep.mock.calls.map(call => call[0]).filter(ms => ms > 0);
    expect(delays.slice(0, 5)).toEqual([1000, 2000, 4000, 8000, 16_000]);
    expect(Math.max(...delays)).toBe(30_000);
    expect(state.events.filter(event => event === 'teardown')).toHaveLength(1);
    expect(state.queue.acknowledge).not.toHaveBeenCalled();
  });

  test('completion retries end before the queue visibility deadline when it is closer than the window', async () => {
    const state = fixture();
    const original = state.send.getMockImplementation()!;
    state.send.mockImplementation(async (url, request) => String(url).endsWith('/complete') ? removalUnconfirmed() : original(url, request));
    const envelopeBody = JSON.stringify(envelope);
    const queue = { receive: async () => [{ Body: envelopeBody, ReceiptHandle: 'receipt' }], acknowledge: jest.fn() };
    const timing = { ...state.timing, visibilityDeadline: 120_000 };
    await expect(runOneSupervisorDispatch(settings, queue, state.api, state.send, state.load, timing)).rejects.toThrow('completion deadline exceeded');
    expect(state.timing.now()).toBeGreaterThanOrEqual(120_000);
    expect(state.timing.now()).toBeLessThan(130_000);
    expect(queue.acknowledge).not.toHaveBeenCalled();
  });
});

describe('a reserved pod that never starts (#8061)', () => {
  const pending = (name: string, uid: string) => ({ metadata: { name, uid },
    status: { phase: 'Pending', containerStatuses: [{ name: 'chat-agent', state: { waiting: { reason: 'ContainerCreating' } } }] } });
  const resumeBodies = (state: ReturnType<typeof fixture>) => state.send.mock.calls
    .filter(call => String(call[0]).endsWith('/resume')).map(call => JSON.parse(String((call[1] as RequestInit).body)) as Record<string, unknown>);
  // Collects [supervisor] lines; sandbox_start lines are also sequenced into the fixture's events
  // so their order relative to pod deletion and gateway calls can be asserted.
  const capture = (state: ReturnType<typeof fixture>) => {
    const lines: Record<string, unknown>[] = [];
    const spy = jest.spyOn(console, 'log').mockImplementation((...args: unknown[]) => {
      const line = String(args[0]);
      if (!line.startsWith('[supervisor] ')) return;
      const parsed = JSON.parse(line.slice('[supervisor] '.length)) as Record<string, unknown>;
      lines.push(parsed);
      if (parsed.step === 'sandbox_start') state.events.push(`log:${parsed.status}`);
    });
    return { lines, restore: () => spy.mockRestore() };
  };

  test('is named in the log before deletion and reconciled within the same dispatch with its uid', async () => {
    const state = fixture();
    state.api.inspect.mockImplementation(async (name: string, uid: string) => pending(name, uid));
    const log = capture(state);
    try { await expect(state.run()).resolves.toBe('completed'); } finally { log.restore(); }
    // The pod is named in the log before the launcher's delete call, then named again on resume.
    expect(state.events).toEqual(['resume', 'reserve', 'create', 'log:failed', 'delete-pod', 'log:reconciling', 'resume', 'delete-pod',
      'teardown', 'complete', 'log:reconciled', 'acknowledge']);
    expect(state.api.create).toHaveBeenCalledTimes(1);
    expect(state.timing.now()).toBeGreaterThanOrEqual(60_000);
    const [first, second] = resumeBodies(state);
    expect(Object.keys(first).sort()).toEqual(['envelope_digest', 'run_id']);
    expect(second).toMatchObject({ run_id: 'run-a', pod_name: pod.name, pod_uid: pod.uid });
    expect(state.queue.acknowledge).toHaveBeenCalledWith('first-receipt');
    expect(state.queue.acknowledge).toHaveBeenCalledTimes(1);
    expect(log.lines).toContainEqual({ run_id: 'run-a', step: 'sandbox_start', status: 'failed', pod_name: pod.name, pod_uid: pod.uid });
    expect(log.lines).toContainEqual(expect.objectContaining({ step: 'sandbox_start', status: 'reconciling', pod_uid: pod.uid,
      error: 'Chat sandbox did not start before the supervisor deadline' }));
    expect(log.lines).toContainEqual(expect.objectContaining({ step: 'sandbox_start', status: 'reconciled', pod_uid: pod.uid, sandbox_uid: pod.uid }));
    expect(log.lines).toContainEqual(expect.objectContaining({ step: 'dispatch', status: 'completed' }));
  });

  test('re-raises the start failure when the gateway keeps conflicting, logging each distinct 409 body once, and completes on redelivery', async () => {
    const state = fixture();
    state.api.inspect.mockImplementation(async (name: string, uid: string) => pending(name, uid));
    const original = state.send.getMockImplementation()!;
    let conflicts = 0;
    state.send.mockImplementation(async (url, request) => {
      if (!String(url).endsWith('/resume') || !String(request?.body).includes('pod_uid')) return original(url, request);
      conflicts += 1;
      return new Response(JSON.stringify({ detail: { error: 'chat_history_conflict', reason: conflicts > 30 ? 'later' : 'early' } }), { status: 409 });
    });
    const log = capture(state);
    try { await expect(state.run()).rejects.toThrow('Chat sandbox did not start before the supervisor deadline'); } finally { log.restore(); }
    expect(conflicts).toBeGreaterThan(30);
    expect(conflicts).toBeLessThanOrEqual(61);
    expect(state.queue.acknowledge).not.toHaveBeenCalled();
    const retrying = log.lines.filter(line => line.step === 'resume' && line.status === 'retrying');
    expect(retrying).toEqual([
      { run_id: 'run-a', step: 'resume', status: 'retrying', http_status: 409, gateway_error: expect.stringContaining('"reason":"early"') },
      { run_id: 'run-a', step: 'resume', status: 'retrying', http_status: 409, gateway_error: expect.stringContaining('"reason":"later"') },
    ]);
    expect(retrying.every(line => String(line.gateway_error).length <= 200)).toBe(true);
    expect(log.lines).toContainEqual(expect.objectContaining({ step: 'sandbox_start', status: 'unreconciled', pod_uid: pod.uid,
      error: expect.stringMatching(/reconciliation deadline exceeded; last gateway response: HTTP 409 .*chat_history_conflict/) }));
    expect(log.lines).toContainEqual(expect.objectContaining({ step: 'dispatch', status: 'failed',
      error: 'Chat sandbox did not start before the supervisor deadline' }));
    // The redelivery no longer knows the uid; the gateway's recorded cleanup completes the turn without another sandbox.
    state.send.mockImplementation(original);
    await expect(state.redeliver()).resolves.toBe('completed');
    expect(state.api.create).toHaveBeenCalledTimes(1);
    expect(state.queue.acknowledge).toHaveBeenCalledWith('redelivery-receipt');
    expect(state.events.slice(-3)).toEqual(['teardown', 'complete', 'acknowledge']);
  });

  test('bounds the retry log to RETRY_LOG_LIMIT distinct responses', async () => {
    const state = fixture();
    state.api.inspect.mockImplementation(async (name: string, uid: string) => pending(name, uid));
    const original = state.send.getMockImplementation()!;
    let conflicts = 0;
    state.send.mockImplementation(async (url, request) => {
      if (!String(url).endsWith('/resume') || !String(request?.body).includes('pod_uid')) return original(url, request);
      conflicts += 1;
      return new Response(JSON.stringify({ detail: { error: 'chat_history_conflict', attempt: conflicts } }), { status: 409 });
    });
    const log = capture(state);
    try { await expect(state.run()).rejects.toThrow('Chat sandbox did not start'); } finally { log.restore(); }
    expect(conflicts).toBeGreaterThan(RETRY_LOG_LIMIT);
    expect(log.lines.filter(line => line.step === 'resume' && line.status === 'retrying')).toHaveLength(RETRY_LOG_LIMIT);
  });

  test('a gateway that answers anything but the cleanup state leaves the delivery pending with the original failure', async () => {
    const state = fixture();
    state.api.inspect.mockImplementation(async (name: string, uid: string) => pending(name, uid));
    const original = state.send.getMockImplementation()!;
    state.send.mockImplementation(async (url, request) => String(url).endsWith('/resume') && String(request?.body).includes('pod_uid') ?
      new Response(JSON.stringify({ run_id: 'run-a', session_id: 'session-a', task_id: 'task-a', session_generation: envelope.session_generation,
        state: 'unstarted' })) : original(url, request));
    const log = capture(state);
    try { await expect(state.run()).rejects.toThrow('Chat sandbox did not start before the supervisor deadline'); } finally { log.restore(); }
    expect(log.lines).toContainEqual(expect.objectContaining({ step: 'sandbox_start', status: 'unreconciled', state: 'unstarted' }));
    expect(state.queue.acknowledge).not.toHaveBeenCalled();
    expect(state.events.filter(event => event === 'create')).toHaveLength(1);
  });
});
