import { withSandboxPod, InClusterSandboxPodApi, type SandboxPodApi } from './sandbox-launcher';
import { buildSandboxPod, sandboxCreationName } from './sandbox-pod';
import { gatewayBinding } from './sandbox-gateway.fixture';

const input = {
  runId: 'run-a',
  image: `registry.example.test/chat-sandbox@sha256:${'a'.repeat(64)}`,
  gatewayUrl: 'https://gateway.example.test',
};
const identity = { name: `${buildSandboxPod(input, gatewayBinding).metadata.generateName}abcde`, uid: '01234567-89ab-cdef-0123-456789abcdef' };
const metadata = { ...identity, namespace: 'adp-gateway-agents', labels: buildSandboxPod(input, gatewayBinding).metadata.labels };

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
    remove: jest.fn(async () => {}),
    ...overrides,
  } as jest.Mocked<SandboxPodApi>;
}

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
