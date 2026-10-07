import { buildSandboxPod } from './sandbox-pod';
import { gatewayBinding } from './sandbox-gateway.fixture';

const image = `registry.example.test/chat-sandbox@sha256:${'a'.repeat(64)}`;
const assignment = { runId: 'run-a', image, gatewayUrl: 'https://gateway.example.test' };

test('fresh pod binds the role-free account, short-lived workload token and bounded scratch', () => {
  const pod = buildSandboxPod(assignment, gatewayBinding);
  expect(pod.metadata.generateName).toMatch(/^chat-turn-[0-9a-f]{12}-$/);
  expect(pod.metadata.labels['adp.io/run-hash']).toMatch(/^[0-9a-f]{64}$/);
  expect(pod.spec.serviceAccountName).toBe('adp-chat-sandbox');
  expect(pod.spec.automountServiceAccountToken).toBe(false);
  expect(pod.spec.enableServiceLinks).toBe(false);
  expect(pod.spec.shareProcessNamespace).toBe(false);
  expect(pod.spec.hostNetwork).toBe(false);
  expect(pod.spec.hostPID).toBe(false);
  expect(pod.spec.hostIPC).toBe(false);
  expect(pod.spec.volumes).toEqual([
    { name: 'sandbox-identity', projected: { defaultMode: 292, sources: [{ serviceAccountToken: {
      audience: 'adp-agent-bootstrap', expirationSeconds: 600, path: 'token',
    } }] } },
    { name: 'scratch', emptyDir: { sizeLimit: '512Mi' } },
    { name: 'gateway-ca', configMap: { name: gatewayBinding.caConfigMap, defaultMode: 292,
      items: [{ key: 'ca.crt', path: 'ca.crt' }] } },
  ]);
  const [container] = pod.spec.containers;
  expect(pod.spec.containers).toHaveLength(1);
  expect(container.image).toBe(image);
  expect(container.securityContext).toMatchObject({
    runAsNonRoot: true, readOnlyRootFilesystem: true, allowPrivilegeEscalation: false,
    capabilities: { drop: ['ALL'] }, seccompProfile: { type: 'RuntimeDefault' },
  });
  expect(container.volumeMounts).toEqual([
    { name: 'sandbox-identity', mountPath: '/var/run/adp-model', readOnly: true },
    { name: 'scratch', mountPath: '/tmp' },
    { name: 'gateway-ca', mountPath: '/var/run/adp-chat-ca', readOnly: true },
  ]);
  expect(container.env).toContainEqual({ name: 'ADP_WORKLOAD_TOKEN_FILE', value: '/var/run/adp-model/token' });
  expect(container.env).toContainEqual({ name: 'ADP_CHAT_DATA_URL', value: 'https://chat-sandbox-gateway.adp-gateway.svc:8443' });
  expect(container.env).toContainEqual({ name: 'NODE_EXTRA_CA_CERTS', value: '/var/run/adp-chat-ca/ca.crt' });
  expect(pod.spec.hostAliases).toEqual([{ ip: gatewayBinding.clusterIP, hostnames: ['chat-sandbox-gateway.adp-gateway.svc'] }]);
  expect(container.env).toEqual(expect.arrayContaining([
    { name: 'ADP_CHAT_DATA_ENABLED', value: 'true' },
    { name: 'CONTEXT_STRATEGY', value: 'gateway' },
    { name: 'MEMORY_STRATEGY', value: 'gateway' },
    { name: 'ARTIFACT_STRATEGY', value: 'gateway' },
  ]));
  expect(JSON.stringify(pod)).not.toMatch(/AWS_ROLE_ARN|AWS_WEB_IDENTITY_TOKEN_FILE|envFrom|secretKeyRef|hostPath/);
  expect(container.command).toEqual(['/app/chat-sandbox-entrypoint']);
  expect(container.workingDir).toBe('/tmp');
  expect(container.env).toContainEqual({ name: 'HOME', value: '/tmp/workspace' });
  expect(container).not.toHaveProperty('args');
});

test('template ignores injected pod authority, service account, mounts and environment', () => {
  const hostile = {
    ...assignment, serviceAccountName: 'adp-agent',
    hostNetwork: true, volumes: [{ hostPath: { path: '/' } }],
    env: [{ name: 'AWS_ROLE_ARN', value: 'platform-role' }],
    command: ['/app/startup.sh'],
  };
  const pod = buildSandboxPod(hostile, gatewayBinding);
  expect(JSON.stringify(pod)).not.toContain('platform-role');
  expect(JSON.stringify(pod)).not.toContain('hostPath');
  expect(pod.spec.serviceAccountName).toBe('adp-chat-sandbox');
  expect(pod.spec.containers[0].command).toEqual(['/app/chat-sandbox-entrypoint']);
});

test.each([
  { ...assignment, image: 'registry.example.test/chat-sandbox:latest' },
  { ...assignment, image: 'registry.example.test/chat;whoami@sha256:' + 'a'.repeat(64) },
  { ...assignment, runId: '../run-a' },
  { ...assignment, gatewayUrl: 'http://gateway.example.test' },
  { ...assignment, gatewayUrl: 'https://gateway.example.test/private' },
  { ...assignment, gatewayUrl: 'https://user:pass@gateway.example.test' },
  { ...assignment, gatewayUrl: 'https://169.254.169.254' },
])('rejects invalid immutable assignment %p', invalid => {
  expect(() => buildSandboxPod(invalid, gatewayBinding)).toThrow();
});
