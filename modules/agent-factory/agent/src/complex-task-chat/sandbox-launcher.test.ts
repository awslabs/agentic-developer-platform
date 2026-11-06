import {
  withSandboxPod, InClusterSandboxPodApi, isUnsetSelector, sandboxStartDeadlineMs, sandboxStartState, waitForSandboxStart,
  sandboxPullAllowanceMs, sandboxWaitingReasons, SANDBOX_START_POLL_MS, type ObservedPod, type SandboxPodApi, sandboxPodEnded,
} from './sandbox-launcher';
import { buildSandboxPod, sandboxCreationName } from './sandbox-pod';
import { gatewayBinding, runningSandboxPod } from './sandbox-gateway.fixture';

const input = {
  runId: 'run-a',
  image: `registry.example.test/chat-sandbox@sha256:${'a'.repeat(64)}`,
  gatewayUrl: 'https://gateway.example.test',
};
const identity = { name: `${buildSandboxPod(input, gatewayBinding).metadata.generateName}abcde`, uid: '01234567-89ab-cdef-0123-456789abcdef' };
const metadata = { ...identity, namespace: 'adp-gateway-agents', labels: buildSandboxPod(input, gatewayBinding).metadata.labels,
  annotations: buildSandboxPod(input, gatewayBinding).metadata.annotations };

test('reserved creation uses only the fixed name and retains rejection of an unapproved pod', async () => {
  const assignment = { ...input, podName: sandboxCreationName(input.runId) };
  const template = buildSandboxPod(assignment, gatewayBinding);
  expect(template.metadata.name).toBe(assignment.podName);
  const pods = api({ create: jest.fn(async () => ({ metadata: { ...metadata, name: assignment.podName },
    spec: { ...template.spec, serviceAccountName: 'unapproved' } })) });
  await expect(withSandboxPod(assignment, pods, async () => 'unused')).rejects.toThrow('admission changed');
  expect(pods.remove).toHaveBeenCalledWith(assignment.podName, identity.uid);
});

test('reserved creation rejects a different returned name without deleting it', async () => {
  const pods = api();
  await expect(withSandboxPod({ ...input, podName: sandboxCreationName(input.runId) }, pods, async () => 'unused')).rejects.toThrow('bound pod identity');
  expect(pods.remove).not.toHaveBeenCalled();
});

test('a caller cannot select a different reserved pod name', () => {
  expect(() => buildSandboxPod({ ...input, podName: 'chat-turn-ffffffffffff-abcde' }, gatewayBinding)).toThrow('pinned image');
});

function api(overrides: Partial<SandboxPodApi> = {}): jest.Mocked<SandboxPodApi> {
  return {
    resolveGateway: jest.fn(async () => gatewayBinding),
    create: jest.fn(async template => ({
      metadata: { ...template.metadata, ...identity }, spec: template.spec,
    })),
    inspect: jest.fn(async (name: string, uid: string) => runningSandboxPod(name, uid)),
    remove: jest.fn(async () => {}),
    ...overrides,
  } as jest.Mocked<SandboxPodApi>;
}

const pendingPod = (): ObservedPod => ({ metadata: { ...identity },
  status: { phase: 'Pending', containerStatuses: [{ name: 'chat-agent', state: { waiting: { reason: 'ContainerCreating' } } }] } });
// Scheduled but not yet handed to the kubelet: no container status at all, so no image pull.
const unscheduledPod = (): ObservedPod => ({ metadata: { ...identity }, status: { phase: 'Pending' } });
const waitingPod = (reason: string): ObservedPod => ({ metadata: { ...identity },
  status: { phase: 'Pending', containerStatuses: [{ name: 'chat-agent', state: { waiting: { reason, message: 'back-off' } } }] } });

function clock() {
  let elapsed = 0;
  const sleep = jest.fn(async (ms: number) => { elapsed += ms; });
  return { timing: { now: () => elapsed, sleep }, sleep, elapsed: () => elapsed };
}

test('hands the pod to the caller only once it is Running, polling at the bounded interval', async () => {
  const inspect = jest.fn()
    .mockResolvedValueOnce(pendingPod())
    .mockResolvedValueOnce({ ...runningSandboxPod(identity.name, identity.uid), status: { ...runningSandboxPod(identity.name, identity.uid).status, podIP: undefined } })
    .mockResolvedValueOnce(runningSandboxPod(identity.name, identity.uid));
  const pods = api({ inspect });
  const { timing, sleep } = clock();
  const run = jest.fn(async () => {
    expect(inspect).toHaveBeenCalledTimes(3);
    expect(pods.remove).not.toHaveBeenCalled();
    return 'started';
  });
  await expect(withSandboxPod(input, pods, run, () => true, timing)).resolves.toBe('started');
  expect(run).toHaveBeenCalledTimes(1);
  expect(inspect).toHaveBeenCalledWith(identity.name, identity.uid);
  expect(sleep.mock.calls).toEqual([[SANDBOX_START_POLL_MS], [SANDBOX_START_POLL_MS]]);
  expect(run.mock.invocationCallOrder[0]).toBeGreaterThan(inspect.mock.invocationCallOrder[2]);
  expect(pods.remove).toHaveBeenCalledWith(identity.name, identity.uid);
});

test('does not read the pod back before admission has approved the returned template', async () => {
  const pods = api({ create: jest.fn(async () => ({ metadata, spec: { ...buildSandboxPod(input, gatewayBinding).spec, serviceAccountName: 'adp-agent' } })) });
  await expect(withSandboxPod(input, pods, async () => 'unused')).rejects.toThrow('admission changed');
  expect(pods.inspect).not.toHaveBeenCalled();
  expect(pods.remove).toHaveBeenCalledWith(identity.name, identity.uid);
});

test('tears the pod down without dispatch when it has not started by the deadline', async () => {
  const pods = api({ inspect: jest.fn(async () => unscheduledPod()) });
  const { timing, sleep, elapsed } = clock();
  const run = jest.fn();
  await expect(withSandboxPod(input, pods, run, () => false, timing)).rejects.toThrow('Chat sandbox did not start');
  expect(run).not.toHaveBeenCalled();
  expect(elapsed()).toBe(60_000);
  expect(sleep).toHaveBeenCalledTimes(120);
  expect(pods.inspect).toHaveBeenCalledTimes(121);
  // The caller asked to retain pods on failure; a pod that never started is removed regardless.
  expect(pods.remove).toHaveBeenCalledWith(identity.name, identity.uid);
});

describe('image pulls during container creation (#8059)', () => {
  let logged: string[];
  beforeEach(() => { logged = []; jest.spyOn(console, 'log').mockImplementation(line => { logged.push(String(line)); }); });
  afterEach(() => { jest.restoreAllMocks(); });
  const supervisorLines = () => logged.filter(line => line.startsWith('[supervisor] ')).map(line => JSON.parse(line.slice('[supervisor] '.length)));

  test('a container still being created extends the deadline exactly once by the pull allowance and logs it', async () => {
    const pods = api({ inspect: jest.fn(async () => pendingPod()) });
    const { timing, sleep, elapsed } = clock();
    const run = jest.fn();
    await expect(withSandboxPod(input, pods, run, () => true, timing)).rejects.toThrow('Chat sandbox did not start before the supervisor deadline');
    expect(run).not.toHaveBeenCalled();
    expect(elapsed()).toBe(60_000 + 180_000);
    expect(sleep).toHaveBeenCalledTimes(480);
    expect(pods.remove).toHaveBeenCalledWith(identity.name, identity.uid);
    const pulling = supervisorLines().filter(line => line.step === 'sandbox_start' && line.status === 'pulling');
    expect(pulling).toEqual([{ run_id: input.runId, step: 'sandbox_start', status: 'pulling', pod: identity.name,
      elapsed_ms: 0, deadline_ms: 60_000, pull_allowance_ms: 180_000 }]);
  });

  test('the allowance is bounded by the environment and never granted twice', async () => {
    const inspect = jest.fn()
      .mockResolvedValueOnce(pendingPod())
      .mockResolvedValueOnce(unscheduledPod())
      .mockResolvedValue(pendingPod());
    const { timing, elapsed } = clock();
    await expect(waitForSandboxStart({ inspect }, identity, ['chat-agent'], timing, 5_000, 1_500)).rejects.toThrow('supervisor deadline');
    expect(elapsed()).toBe(6_500);
    expect(supervisorLines().filter(line => line.status === 'pulling')).toHaveLength(1);
  });

  test('a pod that starts after the pull is dispatched and the start is logged with the allowance used', async () => {
    const inspect = jest.fn()
      .mockResolvedValueOnce(pendingPod())
      .mockResolvedValueOnce(pendingPod())
      .mockResolvedValueOnce(runningSandboxPod(identity.name, identity.uid));
    const pods = api({ inspect });
    const { timing } = clock();
    await expect(withSandboxPod(input, pods, async () => 'started', () => true, timing)).resolves.toBe('started');
    expect(supervisorLines().map(line => line.status)).toEqual(['pulling', 'started']);
    expect(supervisorLines()[1]).toMatchObject({ run_id: input.runId, step: 'sandbox_start', pod: identity.name, elapsed_ms: 1_000, pull_allowance_used: 1 });
  });

  test.each(['ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError', 'InvalidImageName', 'CreateContainerError'])(
    'fails the turn at once and removes the pod when the kubelet reports %s', async reason => {
      const pods = api({ inspect: jest.fn(async () => waitingPod(reason)) });
      const { timing, sleep } = clock();
      const run = jest.fn();
      await expect(withSandboxPod(input, pods, run, () => true, timing)).rejects.toThrow(`Chat sandbox cannot start: ${reason}`);
      expect(run).not.toHaveBeenCalled();
      expect(sleep).not.toHaveBeenCalled();
      expect(pods.remove).toHaveBeenCalledWith(identity.name, identity.uid);
      expect(supervisorLines().filter(line => line.status === 'pulling')).toEqual([]);
    });

  test('a pull failure after the allowance was granted still fails fast', async () => {
    const inspect = jest.fn().mockResolvedValueOnce(pendingPod()).mockResolvedValueOnce(waitingPod('ImagePullBackOff'));
    const { timing, sleep } = clock();
    await expect(waitForSandboxStart({ inspect }, identity, ['chat-agent'], timing)).rejects.toThrow('Chat sandbox cannot start: ImagePullBackOff');
    expect(sleep).toHaveBeenCalledTimes(1);
  });

  test('start states distinguish creating, failed and other pending containers', () => {
    expect(sandboxStartState(pendingPod(), ['chat-agent'])).toBe('pulling');
    expect(sandboxStartState(unscheduledPod(), ['chat-agent'])).toBe('pending');
    expect(sandboxStartState(waitingPod('ImagePullBackOff'), ['chat-agent'])).toBe('failed');
    expect(sandboxStartState(waitingPod('PodInitializing'), ['chat-agent'])).toBe('pending');
    // Only the expected containers count.
    expect(sandboxStartState({ metadata: { ...identity }, status: { phase: 'Pending',
      containerStatuses: [{ name: 'other', state: { waiting: { reason: 'ImagePullBackOff' } } }] } }, ['chat-agent'])).toBe('pending');
    expect(sandboxWaitingReasons(waitingPod('ErrImagePull'), ['chat-agent'])).toEqual(['ErrImagePull']);
    expect(sandboxWaitingReasons(runningSandboxPod(identity.name, identity.uid), ['chat-agent'])).toEqual([]);
    expect(sandboxWaitingReasons(null, ['chat-agent'])).toEqual([]);
  });

  test('the pull allowance is configurable within bounds and defaults to three minutes', () => {
    expect(sandboxPullAllowanceMs({})).toBe(180_000);
    expect(sandboxPullAllowanceMs({ ADP_CHAT_SANDBOX_PULL_ALLOWANCE_MS: '' })).toBe(180_000);
    expect(sandboxPullAllowanceMs({ ADP_CHAT_SANDBOX_PULL_ALLOWANCE_MS: '0' })).toBe(0);
    expect(sandboxPullAllowanceMs({ ADP_CHAT_SANDBOX_PULL_ALLOWANCE_MS: '600000' })).toBe(600_000);
    for (const value of ['600001', '-1', '60s', '1e5', 'NaN']) {
      expect(() => sandboxPullAllowanceMs({ ADP_CHAT_SANDBOX_PULL_ALLOWANCE_MS: value })).toThrow('pull allowance invalid');
    }
  });
});

test.each([
  ['Failed', { metadata: { ...identity }, status: { phase: 'Failed', containerStatuses: [{ name: 'chat-agent', state: { terminated: { exitCode: 1 } } }] } }],
  ['Succeeded', { metadata: { ...identity }, status: { phase: 'Succeeded', podIP: '10.100.7.42' } }],
  ['terminated container', { metadata: { ...identity }, status: { phase: 'Running', podIP: '10.100.7.42', containerStatuses: [{ name: 'chat-agent', state: { terminated: { exitCode: 0 } } }] } }],
  ['missing pod', null],
] as Array<[string, ObservedPod | null]>)('fails the turn immediately and tears down a pod that ended (%s)', async (_label, observed) => {
  const pods = api({ inspect: jest.fn(async () => observed) });
  const { timing, sleep } = clock();
  const run = jest.fn();
  await expect(withSandboxPod(input, pods, run, () => true, timing)).rejects.toThrow('Chat sandbox did not start');
  expect(run).not.toHaveBeenCalled();
  expect(sleep).not.toHaveBeenCalled();
  expect(pods.remove).toHaveBeenCalledWith(identity.name, identity.uid);
});

test('a pod is Running only with the expected container running and an assigned address', () => {
  const running = runningSandboxPod(identity.name, identity.uid);
  expect(sandboxStartState(running, ['chat-agent'])).toBe('running');
  expect(sandboxStartState({ ...running, status: { ...running.status, phase: 'Pending' } }, ['chat-agent'])).toBe('pending');
  expect(sandboxStartState({ ...running, status: { ...running.status, podIP: '' } }, ['chat-agent'])).toBe('pending');
  expect(sandboxStartState({ ...running, status: { ...running.status, containerStatuses: [] } }, ['chat-agent'])).toBe('pending');
  expect(sandboxStartState({ ...running, status: { ...running.status, containerStatuses: [{ name: 'other', state: { running: {} } }] } }, ['chat-agent'])).toBe('pending');
  expect(sandboxStartState(running, ['chat-agent', 'sidecar'])).toBe('pending');
  expect(sandboxStartState(running, [])).toBe('pending');
  expect(sandboxStartState({ metadata: { ...identity }, status: { phase: 'Unknown' } }, ['chat-agent'])).toBe('pending');
  expect(sandboxStartState(null, ['chat-agent'])).toBe('ended');
});

test('the start deadline is configurable within bounds and defaults to sixty seconds', () => {
  expect(sandboxStartDeadlineMs({})).toBe(60_000);
  expect(sandboxStartDeadlineMs({ ADP_CHAT_SANDBOX_START_DEADLINE_MS: '' })).toBe(60_000);
  expect(sandboxStartDeadlineMs({ ADP_CHAT_SANDBOX_START_DEADLINE_MS: '120000' })).toBe(120_000);
  for (const value of ['4999', '300001', '-1', '60s', '1e5', 'NaN']) {
    expect(() => sandboxStartDeadlineMs({ ADP_CHAT_SANDBOX_START_DEADLINE_MS: value })).toThrow('start deadline invalid');
  }
});

test('waitForSandboxStart caps the final sleep at the remaining deadline', async () => {
  const inspect = jest.fn().mockResolvedValueOnce(unscheduledPod()).mockResolvedValueOnce(unscheduledPod()).mockResolvedValueOnce(runningSandboxPod(identity.name, identity.uid));
  const { timing, sleep } = clock();
  await expect(waitForSandboxStart({ inspect }, identity, ['chat-agent'], timing, 700)).resolves.toBeUndefined();
  expect(sleep.mock.calls).toEqual([[500], [200]]);
  // The cap follows the extended deadline while the container is being created.
  const pulling = jest.fn().mockResolvedValueOnce(pendingPod()).mockResolvedValueOnce(pendingPod()).mockResolvedValueOnce(runningSandboxPod(identity.name, identity.uid));
  const extended = clock();
  await expect(waitForSandboxStart({ inspect: pulling }, identity, ['chat-agent'], extended.timing, 700, 100, () => {})).resolves.toBeUndefined();
  expect(extended.sleep.mock.calls).toEqual([[500], [300]]);
});

test('reads back only the created pod and treats a replaced or absent pod as gone', async () => {
  class ReadApi extends InClusterSandboxPodApi {
    readonly calls: unknown[][] = [];
    constructor(private readonly answer: unknown) { super(gatewayBinding.caConfigMap); }
    protected override async call(...args: unknown[]): Promise<unknown> { this.calls.push(args); return this.answer; }
  }
  const pods = new InClusterSandboxPodApi();
  await expect(pods.inspect('other-tenant', identity.uid)).rejects.toThrow('created name and UID');
  await expect(pods.inspect(identity.name, '../other-pod')).rejects.toThrow('created name and UID');
  const running = runningSandboxPod(identity.name, identity.uid);
  const found = new ReadApi(running);
  await expect(found.inspect(identity.name, identity.uid)).resolves.toEqual(running);
  expect(found.calls).toEqual([['GET', `/api/v1/namespaces/adp-gateway-agents/pods/${identity.name}`, undefined, true]]);
  await expect(new ReadApi(null).inspect(identity.name, identity.uid)).resolves.toBeNull();
  await expect(new ReadApi({ ...running, metadata: { ...running.metadata, uid: 'replacement-pod-uid' } }).inspect(identity.name, identity.uid)).resolves.toBeNull();
});

test('starts a newly created restricted pod and deletes only its exact UID after completion', async () => {
  const pods = api();
  const result = await withSandboxPod(input, pods, async pod => {
    expect(pod).toEqual(identity);
    expect(pods.remove).not.toHaveBeenCalled();
    return 'result';
  });
  expect(result).toBe('result');
  expect(pods.create).toHaveBeenCalledWith(buildSandboxPod(input, gatewayBinding));
  expect(pods.remove).toHaveBeenCalledWith(identity.name, identity.uid);
});

test('destroys the pod when a turn fails', async () => {
  const pods = api();
  await expect(withSandboxPod(input, pods, async () => { throw new Error('turn failed'); })).rejects.toThrow('turn failed');
  expect(pods.remove).toHaveBeenCalledWith(identity.name, identity.uid);
});

test('accepts annotations admission adds alongside the unchanged run digest', async () => {
  const created = { metadata: { ...metadata, annotations: { ...metadata.annotations, 'kubernetes.io/limit-ranger': 'LimitRanger plugin set: cpu request for container chat-agent' } },
    spec: buildSandboxPod(input, gatewayBinding).spec };
  const pods = api({ create: jest.fn(async () => created) });
  await expect(withSandboxPod(input, pods, async () => 'done')).resolves.toBe('done');
  expect(pods.remove).toHaveBeenCalledWith(identity.name, identity.uid);
});

test('accepts Kubernetes omission of false host-namespace flags', async () => {
  const spec: Record<string, unknown> = { ...buildSandboxPod(input, gatewayBinding).spec };
  for (const field of ['hostNetwork', 'hostPID', 'hostIPC']) delete spec[field];
  const pods = api({ create: jest.fn(async () => ({ metadata, spec })) });
  await expect(withSandboxPod(input, pods, async () => 'done')).resolves.toBe('done');
  expect(pods.remove).toHaveBeenCalledWith(identity.name, identity.uid);
});

test.each([
  { metadata: { ...metadata, namespace: 'other-tenant' }, spec: buildSandboxPod(input, gatewayBinding).spec },
  { metadata: { ...metadata, labels: { 'adp.io/chat-sandbox': 'false' } }, spec: buildSandboxPod(input, gatewayBinding).spec },
  { metadata: { ...metadata, labels: { ...metadata.labels, 'adp.io/run-hash': 'f'.repeat(64) } }, spec: buildSandboxPod(input, gatewayBinding).spec },
  { metadata: { ...metadata, annotations: { ...metadata.annotations, 'adp.io/run-hash': 'f'.repeat(64) } }, spec: buildSandboxPod(input, gatewayBinding).spec },
  { metadata: { ...metadata, annotations: {} }, spec: buildSandboxPod(input, gatewayBinding).spec },
  { metadata: { ...metadata, annotations: undefined }, spec: buildSandboxPod(input, gatewayBinding).spec },
  { metadata, spec: { ...buildSandboxPod(input, gatewayBinding).spec, terminationGracePeriodSeconds: 900 } },
  { metadata, spec: { ...buildSandboxPod(input, gatewayBinding).spec, containers: [{ ...buildSandboxPod(input, gatewayBinding).spec.containers[0], imagePullPolicy: 'Always' }] } },
  { metadata, spec: { ...buildSandboxPod(input, gatewayBinding).spec, serviceAccountName: 'adp-agent' } },
  { metadata, spec: { ...buildSandboxPod(input, gatewayBinding).spec, hostAliases: [{ ip: '169.254.169.254', hostnames: ['chat-sandbox-gateway.adp-gateway.svc'] }] } },
  { metadata, spec: { ...buildSandboxPod(input, gatewayBinding).spec, hostAliases: [{ ip: gatewayBinding.clusterIP, hostnames: ['attacker.example'] }] } },
  { metadata, spec: { ...buildSandboxPod(input, gatewayBinding).spec, volumes: [{ name: 'secret', secret: { secretName: 'platform' } }] } },
  { metadata, spec: { ...buildSandboxPod(input, gatewayBinding).spec, containers: [{ ...buildSandboxPod(input, gatewayBinding).spec.containers[0], command: ['/app/startup.sh'] }] } },
  { metadata, spec: { ...buildSandboxPod(input, gatewayBinding).spec, initContainers: [{ name: 'privileged' }] } },
  { metadata, spec: { ...buildSandboxPod(input, gatewayBinding).spec, containers: [{ ...buildSandboxPod(input, gatewayBinding).spec.containers[0], envFrom: [{ secretRef: { name: 'platform' } }] }] } },
  { metadata, spec: { ...buildSandboxPod(input, gatewayBinding).spec, containers: [{ ...buildSandboxPod(input, gatewayBinding).spec.containers[0], args: ['injected'] }] } },
])('refuses admitted pod mutations and removes the affected pod %#', async created => {
  const pods = api({ create: jest.fn(async () => created) });
  const run = jest.fn();
  await expect(withSandboxPod(input, pods, run)).rejects.toThrow('admission changed');
  expect(run).not.toHaveBeenCalled();
  expect(pods.remove).toHaveBeenCalledWith(identity.name, identity.uid);
});

test('does not dispatch when Kubernetes returns an unknown pod identity', async () => {
  const pods = api({ create: jest.fn(async () => ({ metadata: { name: 'unrelated', uid: identity.uid } })) });
  const run = jest.fn();
  await expect(withSandboxPod(input, pods, run)).rejects.toThrow('no bound pod identity');
  expect(run).not.toHaveBeenCalled();
  expect(pods.remove).not.toHaveBeenCalled();
});

test('does not create a pod when trusted gateway discovery fails', async () => {
  const pods = api({ resolveGateway: jest.fn(async () => { throw new Error('trust unavailable'); }) });
  const run = jest.fn();
  await expect(withSandboxPod(input, pods, run)).rejects.toThrow('trust unavailable');
  expect(pods.create).not.toHaveBeenCalled();
  expect(run).not.toHaveBeenCalled();
});

test('refuses deletion outside its namespace or without a pinned UID', async () => {
  const pods = new InClusterSandboxPodApi();
  await expect(pods.remove('other-tenant', identity.uid)).rejects.toThrow('created name and UID');
  await expect(pods.remove(identity.name, '../other-pod')).rejects.toThrow('created name and UID');
});

test.each([undefined, null, {}])('treats an absent or API-server-defaulted selector %p as unset', selector => {
  expect(isUnsetSelector(selector)).toBe(true);
});

test.each([
  { matchLabels: {} }, { matchExpressions: [] }, { matchLabels: { app: 'other' } },
  { matchExpressions: [{ key: 'adp.io/chat-sandbox', operator: 'DoesNotExist' }] }, [], 'all', 0,
])('treats a selector with keys or of another shape %p as narrowing', selector => {
  expect(isUnsetSelector(selector)).toBe(false);
});

describe('forced deletion of an exited sandbox (#8047)', () => {
  class RecordingApi extends InClusterSandboxPodApi {
    calls: Array<[string, string, object | undefined]> = [];
    protected async call(method: 'GET' | 'POST' | 'DELETE', path: string, body?: object): Promise<unknown> {
      this.calls.push([method, path, body]);
      return {};
    }
  }

  test('a plain delete keeps the pod grace period; gracePeriodSeconds 0 adds the immediate background delete', async () => {
    const pods = new RecordingApi();
    await pods.remove(identity.name, identity.uid);
    await pods.remove(identity.name, identity.uid, { gracePeriodSeconds: 0 });
    expect(pods.calls).toEqual([
      ['DELETE', `/api/v1/namespaces/adp-gateway-agents/pods/${identity.name}`, { apiVersion: 'v1', kind: 'DeleteOptions', preconditions: { uid: identity.uid } }],
      ['DELETE', `/api/v1/namespaces/adp-gateway-agents/pods/${identity.name}`, { apiVersion: 'v1', kind: 'DeleteOptions', preconditions: { uid: identity.uid },
        gracePeriodSeconds: 0, propagationPolicy: 'Background' }],
    ]);
  });

  test('refuses any other grace period', async () => {
    const pods = new RecordingApi();
    await expect(pods.remove(identity.name, identity.uid, { gracePeriodSeconds: 5 as unknown as 0 })).rejects.toThrow('grace period');
    expect(pods.calls).toEqual([]);
  });

  test.each([
    [{ metadata: { name: 'p', uid: 'u' }, status: { phase: 'Succeeded' } }, true],
    [{ metadata: { name: 'p', uid: 'u' }, status: { phase: 'Failed' } }, true],
    [{ metadata: { name: 'p', uid: 'u', deletionTimestamp: 'now' }, status: { phase: 'Running', containerStatuses: [{ name: 'chat-agent', state: { terminated: {} } }] } }, true],
    [{ metadata: { name: 'p', uid: 'u', deletionTimestamp: 'now' }, status: { phase: 'Running', containerStatuses: [{ name: 'chat-agent', state: { running: {} } }] } }, false],
    [null, true],
  ])('sandboxPodEnded(%j) is %s', (pod, ended) => {
    expect(sandboxPodEnded(pod as never)).toBe(ended);
  });
});
