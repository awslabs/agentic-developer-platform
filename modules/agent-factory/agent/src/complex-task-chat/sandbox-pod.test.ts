import { buildSandboxPod, sandboxCreationName, validateKubernetesLabels, type SandboxPodInput } from './sandbox-pod';
import { createHash } from 'node:crypto';
import { gatewayBinding } from './sandbox-gateway.fixture';
import { SUPERVISOR_POD_EXPRESSION } from './sandbox-launcher';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const yaml = require('js-yaml');

const image = `registry.example.test/chat-sandbox@sha256:${'a'.repeat(64)}`;
const assignment = { runId: 'run-a', image, gatewayUrl: 'https://gateway.example.test' };

test.each([undefined, 'ephemeral'] as const)('one-shot spec remains byte-identical to the pre-Crew template (%s)', sessionMode => {
  const fixedBinding = { clusterIP: '10.100.42.17', caConfigMap: 'chat-sandbox-gateway-ca-aaaaaaaaaaaaaaaa' };
  const pod = buildSandboxPod({ ...assignment, ...(sessionMode === undefined ? {} : { sessionMode }) }, fixedBinding);
  expect(createHash('sha256').update(JSON.stringify(pod)).digest('hex')).toBe(
    'a8d37ef291d2b403d5f1f278f159a000ee305a52f71417f58ee213458940f0c5');
});

test('selected Claude sessions use only the scratch mount and never change ephemeral pod selection', () => {
  const selected = buildSandboxPod({ ...assignment, sessionMode: 'persistent', sessionId: 'session-a',
    ownerKey: 'a'.repeat(52), runBindingId: '00000000-0000-4000-8000-000000000001', executor: 'claude-code' }, gatewayBinding);
  const pod = selected.spec.containers[0];
  expect(pod.env).toContainEqual({ name: 'ADP_EXECUTOR', value: 'claude-code' });
  expect(pod.volumeMounts).toContainEqual({ name: 'scratch', mountPath: '/tmp' });
  expect(pod.volumeMounts).not.toContainEqual(expect.objectContaining({ name: 'crew-home' }));
  expect(selected.spec.volumes).not.toContainEqual(expect.objectContaining({ name: 'crew-home' }));
  expect(() => buildSandboxPod({ ...assignment, executor: 'claude-code' }, gatewayBinding)).toThrow();
});

test('selected Kiro Crew sessions use only their owner-bound retained home', () => {
  const ownerKey = 'a'.repeat(52);
  const selected = buildSandboxPod({ ...assignment, sessionMode: 'persistent', sessionId: 'session-a', ownerKey,
    runBindingId: '00000000-0000-4000-8000-000000000001', executor: 'kirocrew' }, gatewayBinding);
  expect(selected.spec.volumes).toContainEqual({ name: 'crew-home',
    persistentVolumeClaim: { claimName: `crew-home-${ownerKey}` } });
  expect(selected.spec.containers[0].volumeMounts).toContainEqual({ name: 'crew-home', mountPath: '/tmp/workspace' });
  expect(selected.spec.containers[0].env).toContainEqual({ name: 'KIROCREW_HOME', value: '/tmp/workspace' });
  expect(selected.spec.containers[0].env).toContainEqual({ name: 'ADP_EXECUTOR', value: 'kirocrew' });
  expect(selected.spec.containers[0].lifecycle?.preStop.exec.command).toEqual(['/bin/sh', '-c',
    'kill -TERM 1; while kill -0 1 2>/dev/null; do sleep 1; done']);
  expect(selected.spec.terminationGracePeriodSeconds).toBe(120);
  expect(() => buildSandboxPod({ ...assignment, executor: 'kirocrew' }, gatewayBinding)).toThrow();
  expect(() => buildSandboxPod({ ...assignment, sessionMode: 'persistent', sessionId: 'session-a',
    runBindingId: '00000000-0000-4000-8000-000000000001', executor: 'kirocrew' }, gatewayBinding)).toThrow();
});

test('fresh pod binds the role-free account, short-lived workload token and bounded scratch', () => {
  const pod = buildSandboxPod(assignment, gatewayBinding);
  expect(pod.metadata.generateName).toMatch(/^chat-turn-[0-9a-f]{12}-$/);
  expect(pod.metadata.annotations['adp.io/run-hash']).toMatch(/^[0-9a-f]{64}$/);
  expect(pod.metadata.labels).not.toHaveProperty(['adp.io/run-hash']);
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

test('persistent pod selects only Crew without changing ephemeral', () => {
  const crew = buildSandboxPod({ ...assignment, sessionMode: 'persistent', sessionId: 'session-a', ownerKey: 'a'.repeat(52), runBindingId: '00000000-0000-4000-8000-000000000001' }, gatewayBinding);
  const ephemeral = buildSandboxPod(assignment, gatewayBinding);
  expect(crew.metadata.labels).toHaveProperty(['adp.io/crew-pod'], 'true');
  expect(crew.metadata.labels).not.toHaveProperty(['adp.io/chat-sandbox']);
  expect(crew.spec.serviceAccountName).toBe('adp-crew-pod');
  expect(crew.spec).not.toHaveProperty('activeDeadlineSeconds');
  expect(crew.spec.terminationGracePeriodSeconds).toBe(120);
  expect(crew.spec.volumes.find(volume => volume.name === 'scratch')?.emptyDir?.sizeLimit).toBe('2Gi');
  expect(crew.spec.containers[0].resources).toEqual({
    requests: { cpu: '500m', memory: '1Gi', 'ephemeral-storage': '256Mi' },
    limits: { cpu: '2', memory: '6Gi', 'ephemeral-storage': '4Gi' },
  });
  expect(ephemeral.spec.activeDeadlineSeconds).toBe(900);
  expect(ephemeral.spec.terminationGracePeriodSeconds).toBe(10);
  expect(ephemeral.metadata.labels).not.toHaveProperty(['adp.io/crew-pod']);
});

test('Crew home uses only the supplied owner-bound claim, separate from bounded scratch', () => {
  const ownerKey = 'a'.repeat(52);
  const crew = buildSandboxPod({ ...assignment, sessionMode: 'persistent', sessionId: 'session-a', ownerKey, runBindingId: '00000000-0000-4000-8000-000000000001' }, gatewayBinding);
  expect(crew.metadata.labels).toHaveProperty(['adp.io/owner-key'], ownerKey);
  expect(crew.spec.volumes).toContainEqual({ name: 'crew-home',
    persistentVolumeClaim: { claimName: `crew-home-${ownerKey}` } });
  expect(crew.spec.volumes).toContainEqual({ name: 'scratch', emptyDir: { sizeLimit: '2Gi' } });
  expect(crew.spec.containers[0].volumeMounts).toContainEqual({ name: 'crew-home', mountPath: '/tmp/workspace' });
  expect(crew.spec.containers[0].env).toContainEqual({ name: 'KIROCREW_HOME', value: '/tmp/workspace' });
  expect(crew.spec.containers[0].env).toContainEqual({ name: 'ADP_EXECUTOR', value: 'sandbox-loop' });
  expect(crew.spec.containers[0].env).toContainEqual({ name: 'ADP_RUN_ID', value: '00000000-0000-4000-8000-000000000001' });
  expect(crew.spec.containers[0].env).toContainEqual({ name: 'ADP_CHAT_DATA_URL', value: 'https://chat-sandbox-gateway.adp-gateway.svc:8443' });
  expect(JSON.stringify(crew)).not.toMatch(/AWS_ROLE_ARN|AWS_WEB_IDENTITY_TOKEN_FILE|AWS_ACCESS_KEY_ID|SIGV4_PROXY_TARGET|ADP_BEDROCK_VIA|envFrom|secretKeyRef/);
  const ephemeral = buildSandboxPod({ ...assignment, ownerKey }, gatewayBinding);
  expect(ephemeral.spec.volumes).not.toContainEqual(expect.objectContaining({ name: 'crew-home' }));
  expect(ephemeral.spec.containers[0].env).not.toContainEqual(expect.objectContaining({ name: 'KIROCREW_HOME' }));
  expect(ephemeral.spec.containers[0].env).not.toContainEqual(expect.objectContaining({ name: 'ADP_EXECUTOR' }));
  expect(ephemeral.spec.containers[0].env).not.toContainEqual(expect.objectContaining({ name: 'ADP_RUN_ID' }));
});

test.each([undefined, '', 'b'.repeat(51), 'z'.repeat(52), 'B'.repeat(52), '../foreign'])
('persistent mode fails closed without a valid trusted owner key: %s', ownerKey => {
  expect(() => buildSandboxPod({ ...assignment, sessionMode: 'persistent', sessionId: 'session-a', ownerKey },
    gatewayBinding)).toThrow('trusted owner-bound volume key');
});

test('both workload templates bind claims to the owner key and restrict helper mounts', () => {
  const load = (name: string): any[] => yaml.loadAll(readFileSync(resolve(__dirname, '../../k8s', name), 'utf8'));
  const [crew] = load('chat-crew-pod-admission.yaml');
  const [helper] = load('chat-crew-helper-admission.yaml');
  for (const policy of [crew, helper]) {
    const expressions: string = policy.spec.validations.map((rule: any) => rule.expression).join(' ');
    expect(expressions).toContain("object.metadata.labels['adp.io/owner-key'].matches('^[a-f0-9]{52}$')");
    expect(expressions).toContain("v.persistentVolumeClaim.claimName == 'crew-home-' + object.metadata.labels['adp.io/owner-key']");
    expect(expressions).toContain('size(object.spec.volumes) == 4');
    expect(expressions).toContain('size(object.spec.containers[0].volumeMounts) == 4');
    expect(expressions).not.toContain('hostPath');
  }
  const helperExpressions: string = helper.spec.validations.map((rule: any) => rule.expression).join(' ');
  expect(helperExpressions).toContain('v.persistentVolumeClaim.readOnly == true');
  expect(helperExpressions).toContain("m.mountPath == '/mnt/crew' && m.readOnly == true");
  expect(helperExpressions).toContain("e.value == object.metadata.labels['adp.io/owner-key']");
  expect(crew.spec.validations.map((rule: any) => rule.expression).join(' ')).toContain(
    "m.name == 'crew-home' && m.mountPath == '/tmp/workspace'");
});

test('owner claim fixture rejects foreign label, foreign claim and helper write access', () => {
  const ownerKey = 'a'.repeat(52);
  const crew: any = buildSandboxPod({ ...assignment, sessionMode: 'persistent', sessionId: 'session-a', ownerKey, runBindingId: '00000000-0000-4000-8000-000000000001' }, gatewayBinding);
  const ownerClaimBound = (pod: any) => pod.spec.volumes.filter((volume: any) => volume.name === 'crew-home').length === 1 &&
    pod.spec.volumes.find((volume: any) => volume.name === 'crew-home').persistentVolumeClaim.claimName ===
      `crew-home-${pod.metadata.labels['adp.io/owner-key']}`;
  expect(ownerClaimBound(crew)).toBe(true);
  const foreignClaim = structuredClone(crew);
  foreignClaim.spec.volumes.find((volume: any) => volume.name === 'crew-home').persistentVolumeClaim.claimName =
    `crew-home-${'b'.repeat(52)}`;
  expect(ownerClaimBound(foreignClaim)).toBe(false);
  const foreignLabel = structuredClone(crew);
  foreignLabel.metadata.labels['adp.io/owner-key'] = 'b'.repeat(52);
  expect(ownerClaimBound(foreignLabel)).toBe(false);
  const helper: any = yaml.loadAll(readFileSync(resolve(__dirname, '../../k8s/chat-crew-helper-admission.yaml'), 'utf8'))[0];
  const rules: string = helper.spec.validations.map((rule: any) => rule.expression).join(' ');
  expect(rules).toContain('v.persistentVolumeClaim.readOnly == true');
  expect(rules).toContain("m.mountPath == '/mnt/crew' && m.readOnly == true");
  // The apiserver compiles CEL with homogeneous aggregate literals, so the owner key must be
  // compared field by field rather than inside a map literal next to a string constant.
  expect(rules).toContain("t.labelSelector.matchLabels['adp.io/owner-key'] == object.metadata.labels['adp.io/owner-key']");
  expect(rules).toContain("t.labelSelector.matchLabels['adp.io/crew-pod'] == 'true'");
  expect(rules).toContain('size(t.labelSelector.matchLabels) == 2');
  expect(rules).not.toContain("{'adp.io/crew-pod'");
  expect(rules).toContain("topologyKey == 'kubernetes.io/hostname'");
});

test('retained EBS class and owner PVC template require explicit owner provisioning', () => {
  const read = (name: string): any => yaml.load(readFileSync(resolve(__dirname, '../../k8s', name), 'utf8'));
  const storage = read('chat-crew-ebs-storage.yaml');
  const claim = read('chat-crew-owner-pvc.yaml');
  expect(storage).toMatchObject({ provisioner: 'ebs.csi.aws.com', reclaimPolicy: 'Retain',
    volumeBindingMode: 'WaitForFirstConsumer' });
  expect(claim.spec).toEqual({ accessModes: ['ReadWriteOnce'], storageClassName: storage.metadata.name,
    resources: { requests: { storage: '20Gi' } } });
  expect(claim.metadata.name).toBe('crew-home-REPLACE_WITH_TRUSTED_OWNER_KEY');
  expect(claim.metadata.labels).toEqual({ 'adp.io/owner-key': 'REPLACE_WITH_TRUSTED_OWNER_KEY' });
  const script = readFileSync(resolve(__dirname, '../../k8s/deploy-chat-scaledjob.sh'), 'utf8');
  expect(script).not.toContain('chat-crew-owner-pvc.yaml');
});

test('Crew and sandbox select separate admission policies before supervisor permits Crew', () => {
  const manifests = (name: string): any[] => yaml.loadAll(readFileSync(resolve(__dirname, '../../k8s', name), 'utf8'));
  const crew = buildSandboxPod({ ...assignment, sessionMode: 'persistent', sessionId: 'session-a', ownerKey: 'a'.repeat(52), runBindingId: '00000000-0000-4000-8000-000000000001' }, gatewayBinding);
  const sandbox = buildSandboxPod(assignment, gatewayBinding);
  const [crewPolicy, binding] = manifests('chat-crew-pod-admission.yaml');
  const [sandboxPolicy] = manifests('chat-sandbox-admission.yaml');
  const [supervisorPolicy] = manifests('chat-supervisor-pod-policy.yaml');
  const matches = (pod: typeof crew, policy: any) => {
    const condition: string = policy.spec.matchConditions[0].expression;
    const accounts = Array.from(condition.matchAll(/serviceAccountName == '([^']+)'/g), match => match[1]);
    const labels = Array.from(condition.matchAll(/'([^']+)' in object.metadata.labels/g), match => match[1]);
    return accounts.includes(pod.spec.serviceAccountName) || labels.some(label => label in pod.metadata.labels);
  };
  expect(matches(crew, crewPolicy)).toBe(true);
  expect(matches(crew, sandboxPolicy)).toBe(false);
  expect(matches(sandbox, crewPolicy)).toBe(false);
  expect(matches(sandbox, sandboxPolicy)).toBe(true);
  expect(sandboxPolicy.spec.validations[0].expression).toContain("!('adp.io/crew-pod' in object.metadata.labels)");
  expect(crewPolicy.spec.validations[0].expression).toContain("!('adp.io/chat-sandbox' in object.metadata.labels)");
  expect(crewPolicy.spec.validations.map((rule: any) => rule.expression).join(' ')).toContain('!has(object.spec.activeDeadlineSeconds)');
  expect(sandboxPolicy.spec.validations.map((rule: any) => rule.expression).join(' ')).toContain('object.spec.activeDeadlineSeconds == 900');
  expect(crewPolicy.spec.validations.map((rule: any) => rule.expression).join(' ')).toContain('request.userInfo.username ==');
  expect(supervisorPolicy.spec.validations[0].expression.replace(/\s+/g, ' ').trim()).toBe(SUPERVISOR_POD_EXPRESSION);
  expect(binding.spec.policyName).toBe(crewPolicy.metadata.name);
});

test('role-free Crew accounts reject role annotations and secret references', () => {
  const load = (name: string): any[] => yaml.loadAll(readFileSync(resolve(__dirname, '../../k8s', name), 'utf8'));
  const [policy, binding] = load('chat-crew-service-account-admission.yaml');
  const accounts = load('chat-crew-service-accounts.yaml');
  expect(accounts.map(account => account.metadata.name)).toEqual(['adp-crew-pod', 'adp-crew-checkpoint-helper', 'adp-crew-restore-helper']);
  for (const account of accounts) {
    expect(account.automountServiceAccountToken).toBe(false);
    expect(account.metadata.annotations).toBeUndefined();
    expect(account.imagePullSecrets).toBeUndefined();
    expect(account.secrets).toBeUndefined();
  }
  expect(policy.spec.matchConstraints.resourceRules[0]).toMatchObject({
    operations: ['CREATE', 'UPDATE'], resources: ['serviceaccounts'],
  });
  expect(policy.spec.matchConditions[0].expression).toContain("'adp-crew-checkpoint-helper'");
  const expression: string = policy.spec.validations[0].expression;
  expect(expression).toContain('object.automountServiceAccountToken == false');
  expect(expression).toContain("object.metadata.annotations.all(key, key == 'kubectl.kubernetes.io/last-applied-configuration')");
  expect(expression).toContain('!has(object.imagePullSecrets) && !has(object.secrets)');
  expect(binding.spec.validationActions).toEqual(['Deny']);
  expect(binding.spec.matchResources.namespaceSelector.matchLabels).toEqual({
    'kubernetes.io/metadata.name': 'adp-gateway-agents',
  });
});

test('helper admission accepts only a separate, uncredentialed checkpoint workload class', () => {
  const [policy, binding]: any[] = yaml.loadAll(readFileSync(
    resolve(__dirname, '../../k8s/chat-crew-helper-admission.yaml'), 'utf8'));
  expect(policy.spec.failurePolicy).toBe('Fail');
  expect(policy.spec.matchConstraints.resourceRules[0].operations).toEqual(['CREATE', 'UPDATE']);
  expect(policy.spec.matchConditions[0].expression).toContain("'adp.io/crew-checkpoint-helper' in object.metadata.labels");
  const expressions: string[] = policy.spec.validations.map((rule: any) => rule.expression);
  const constraints = expressions.join(' ');
  for (const fragment of [
    "request.userInfo.username == 'system:serviceaccount:adp-gateway-agents:adp-chat-supervisor'",
    "object.spec.serviceAccountName == 'adp-crew-checkpoint-helper'",
    "!('adp.io/crew-pod' in object.metadata.labels)",
    "!('adp.io/chat-sandbox' in object.metadata.labels)",
    'object.spec.automountServiceAccountToken == false',
    '!has(object.spec.imagePullSecrets)',
    'size(object.spec.volumes) == 4',
    "serviceAccountToken.audience == 'adp-checkpoint-upload'",
    "object.spec.containers[0].command == ['/app/chat-checkpoint-helper']",
    'size(object.spec.containers[0].env) == 6',
    '!has(object.spec.containers[0].envFrom)',
    '!has(e.valueFrom)',
    "e.name in ['ADP_WORKLOAD_TOKEN_FILE', 'ADP_CHAT_DATA_URL', 'NODE_EXTRA_CA_CERTS',",
    "e.value == '/var/run/adp-checkpoint/token'",
    'size(object.spec.containers[0].volumeMounts) == 4',
    'object.spec.containers[0].securityContext.allowPrivilegeEscalation == false',
  ]) expect(constraints).toContain(fragment);
  expect(constraints).not.toContain('AWS_ROLE_ARN');
  expect(binding.spec.validationActions).toEqual(['Deny']);
  expect(binding.spec.policyName).toBe(policy.metadata.name);
  expect(binding.spec.matchResources.namespaceSelector.matchLabels).toEqual({
    'kubernetes.io/metadata.name': 'adp-gateway-agents',
  });
  const supervisor: any = yaml.loadAll(readFileSync(resolve(__dirname, '../../k8s/chat-supervisor-pod-policy.yaml'), 'utf8'))[0];
  expect(supervisor.spec.validations[0].expression.replace(/\s+/g, ' ').trim()).toBe(SUPERVISOR_POD_EXPRESSION);
  expect(SUPERVISOR_POD_EXPRESSION).toContain("object.spec.serviceAccountName == 'adp-crew-checkpoint-helper'");
});

test('restore pod requires its own supervisor-approved writable claim and scoped identity', () => {
  const load = (name: string): any[] => yaml.loadAll(readFileSync(resolve(__dirname, '../../k8s', name), 'utf8'));
  const [restore, binding] = load('chat-crew-restore-admission.yaml');
  const [capture] = load('chat-crew-helper-admission.yaml');
  const expressions: string = restore.spec.validations.map((rule: any) => rule.expression).join(' ');
  const captureExpressions: string = capture.spec.validations.map((rule: any) => rule.expression).join(' ');
  expect(expressions).toContain("object.spec.serviceAccountName == 'adp-crew-restore-helper'");
  expect(expressions).toContain("request.userInfo.username == 'system:serviceaccount:adp-gateway-agents:adp-chat-supervisor'");
  expect(expressions).toContain("v.persistentVolumeClaim.claimName == 'crew-home-' + object.metadata.labels['adp.io/owner-key']");
  expect(expressions).toContain('v.persistentVolumeClaim.readOnly == false');
  expect(expressions).toContain("m.mountPath == '/mnt/crew' && m.readOnly == false");
  expect(expressions).toContain("serviceAccountToken.audience == 'adp-checkpoint-restore'");
  expect(expressions).toContain("object.spec.containers[0].command == ['/app/chat-checkpoint-restore']");
  expect(expressions).not.toContain('hostPath');
  expect(captureExpressions).toContain('v.persistentVolumeClaim.readOnly == true');
  expect(binding.spec.policyName).toBe('adp-crew-restore-helper-template');
  expect(SUPERVISOR_POD_EXPRESSION).toContain("object.spec.serviceAccountName == 'adp-crew-restore-helper'");
  const [network] = load('chat-crew-restore-egress.yaml');
  expect(network.spec.podSelector.matchLabels).toEqual({ 'adp.io/crew-restore-helper': 'true' });
  expect(network.spec.policyTypes).toEqual(['Ingress', 'Egress']);
  const [s3] = load('chat-crew-restore-s3-egress.yaml');
  expect(s3.spec.podSelector.matchLabels).toEqual(network.spec.podSelector.matchLabels);
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
  { ...assignment, gatewayUrl: 'https://abc123def4.execute-api.us-east-1.amazonaws.com/dev/agent' },
  { ...assignment, gatewayUrl: 'https://abc123def4.execute-api.us-east-1.amazonaws.com/dev/' },
  { ...assignment, gatewayUrl: 'https://abc123def4.execute-api.us-east-1.amazonaws.com:8443/dev' },
  { ...assignment, gatewayUrl: 'https://abc123def4.execute-api.us-east-1.amazonaws.com/dev?x=1' },
])('rejects invalid immutable assignment %p', invalid => {
  expect(() => buildSandboxPod(invalid, gatewayBinding)).toThrow();
});

test('accepts the API Gateway stage URL as the supervisor gateway target without exposing it to the sandbox', () => {
  const stage = 'https://abc123def4.execute-api.us-east-1.amazonaws.com/dev';
  const rendered = buildSandboxPod({ ...assignment, gatewayUrl: stage }, gatewayBinding);
  expect(JSON.stringify(rendered)).not.toContain('execute-api');
});

test('only a Kiro Crew pod receives the dashboard ports, origin and downward pod IP', () => {
  const crew = buildSandboxPod({ ...assignment, sessionMode: 'persistent', sessionId: 'session-a',
    ownerKey: 'a'.repeat(52), runBindingId: '00000000-0000-4000-8000-000000000001',
    executor: 'kirocrew', dashboardOrigin: 'https://crew.example.test' }, gatewayBinding);
  expect(crew.spec.containers[0].ports).toEqual([
    { containerPort: 5476, protocol: 'TCP' }, { containerPort: 5477, protocol: 'TCP' },
  ]);
  expect(crew.spec.containers[0].env).toContainEqual({ name: 'ADP_CREW_POD_IP',
    valueFrom: { fieldRef: { fieldPath: 'status.podIP' } } });
  expect(crew.spec.containers[0].env).toContainEqual({ name: 'ADP_CREW_DASHBOARD_ORIGIN', value: 'https://crew.example.test' });
  expect(() => buildSandboxPod({ ...assignment, dashboardOrigin: 'https://crew.example.test' }, gatewayBinding)).toThrow();
});

test('only a user-scoped Crew pod carries private listener intent', () => {
  const owner = { ...assignment, sessionMode: 'persistent' as const, sessionId: 'session-a',
    ownerKey: 'a'.repeat(52), runBindingId: '00000000-0000-4000-8000-000000000001', executor: 'kirocrew' as const };
  const shared = buildSandboxPod({ ...owner, crewScope: 'user' }, gatewayBinding);
  expect(shared.spec.containers[0].env).toContainEqual({ name: 'ADP_CREW_SCOPE', value: 'user' });
  expect(buildSandboxPod(owner, gatewayBinding).spec.containers[0].env).not.toContainEqual({ name: 'ADP_CREW_SCOPE', value: 'user' });
  expect(() => buildSandboxPod({ ...assignment, crewScope: 'user' }, gatewayBinding)).toThrow();
  expect(() => buildSandboxPod({ ...owner, executor: 'claude-code', crewScope: 'user' }, gatewayBinding)).toThrow();
});

// Kubernetes metadata rules (k8s.io/apimachinery/pkg/util/validation): a label value is at most 63
// characters, alphanumeric at both ends with [-._A-Za-z0-9] inside (or empty); a label/annotation key
// is an optional DNS-subdomain prefix plus a 63-character qualified name; a pod name is a DNS-1123
// subdomain, and a generateName base is truncated to 58 characters before the 5-character suffix.
const LABEL_VALUE = /^(?:[A-Za-z0-9](?:[-._A-Za-z0-9]{0,61}[A-Za-z0-9])?)?$/;
const QUALIFIED_KEY = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\/)?[A-Za-z0-9](?:[-._A-Za-z0-9]{0,61}[A-Za-z0-9])?$/;
const DNS1123_SUBDOMAIN = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$/;
const MAX_GENERATE_NAME_BASE = 58;
const sha256 = (value: string) => createHash('sha256').update(value).digest('hex');

// Longest inputs the assignment validator admits: 128-character run and session ids, a 52-hex owner
// key, a 36-character run binding UUID and a long pinned image reference.
const worstCase: SandboxPodInput = {
  runId: `${'R'.repeat(60)}:${'9'.repeat(67)}`,
  sessionId: `${'S'.repeat(60)}.${'_'.repeat(67)}`,
  ownerKey: 'f'.repeat(52),
  runBindingId: 'ffffffff-ffff-4fff-bfff-ffffffffffff',
  image: `registry.example.test/${'x'.repeat(100)}/chat-sandbox@sha256:${'f'.repeat(64)}`,
  gatewayUrl: 'https://gateway.example.test',
};

test.each<[string, SandboxPodInput]>([
  ['ephemeral sandbox', { ...worstCase, sessionMode: 'ephemeral' }],
  ['reserved ephemeral sandbox', { ...worstCase, sessionMode: 'ephemeral', podName: sandboxCreationName(worstCase.runId) }],
  ['persistent Claude Code Crew', { ...worstCase, sessionMode: 'persistent', executor: 'claude-code' }],
  ['persistent sandbox-loop Crew', { ...worstCase, sessionMode: 'persistent', executor: 'sandbox-loop' }],
  ['persistent Kiro Crew with dashboard', { ...worstCase, sessionMode: 'persistent', executor: 'kirocrew', crewScope: 'user',
    dashboardOrigin: `https://${'d'.repeat(50)}.example.test` }],
])('renders Kubernetes-valid labels and names for worst-case inputs (%s)', (_case, input) => {
  expect(input.runId).toHaveLength(128);
  expect(input.sessionId).toHaveLength(128);
  const pod = buildSandboxPod(input, gatewayBinding);
  for (const [key, value] of Object.entries(pod.metadata.labels)) {
    expect(key).toMatch(QUALIFIED_KEY);
    expect(value).toMatch(LABEL_VALUE);
    expect(Buffer.byteLength(value)).toBeLessThanOrEqual(63);
  }
  for (const key of Object.keys(pod.metadata.annotations)) expect(key).toMatch(QUALIFIED_KEY);
  expect(pod.metadata.generateName).toMatch(/^chat-turn-[0-9a-f]{12}-$/);
  expect(pod.metadata.generateName.length).toBeLessThanOrEqual(MAX_GENERATE_NAME_BASE);
  expect(`${pod.metadata.generateName}abcde`).toMatch(DNS1123_SUBDOMAIN);
  const name = (pod.metadata as { name?: string }).name;
  if (input.podName !== undefined) {
    expect(name).toBe(input.podName);
    expect(name).toMatch(DNS1123_SUBDOMAIN);
    expect(name!.length).toBeLessThanOrEqual(63);
  } else {
    expect(name).toBeUndefined();
  }
  // The full digests travel only as annotations; the admission policies and the gateway read them there.
  expect(pod.metadata.annotations['adp.io/run-hash']).toBe(sha256(input.runId));
  expect(pod.metadata.labels).not.toHaveProperty(['adp.io/run-hash']);
  expect(pod.metadata.labels).not.toHaveProperty(['adp.io/session-hash']);
  if (input.sessionMode === 'persistent') {
    expect(pod.metadata.annotations['adp.io/session-hash']).toBe(sha256(input.sessionId!));
    expect(pod.metadata.labels['adp.io/owner-key']).toBe(input.ownerKey);
  } else {
    expect(pod.metadata.annotations).not.toHaveProperty(['adp.io/session-hash']);
  }
});

test('a sha256 digest is refused as a label value before the API server sees it', () => {
  expect(() => validateKubernetesLabels({ 'adp.io/run-hash': 'a'.repeat(64) })).toThrow('adp.io/run-hash is not a valid Kubernetes label value');
  expect(() => validateKubernetesLabels({ k: '-leading-dash' })).toThrow('not a valid Kubernetes label value');
  expect(() => validateKubernetesLabels({ k: 'trailing.' })).toThrow('not a valid Kubernetes label value');
  expect(() => validateKubernetesLabels({ k: 'has space' })).toThrow('not a valid Kubernetes label value');
  expect(() => validateKubernetesLabels({ 'adp.io/owner-key': 'a'.repeat(63), empty: '', 'app.kubernetes.io/name': 'chat-turn-sandbox', v: 'A-b_c.9' })).not.toThrow();
});

test('an ephemeral sandbox terminates within ten seconds and has no preStop hook to wait on (#8047)', () => {
  const ephemeral = buildSandboxPod(assignment, gatewayBinding);
  expect(ephemeral.spec.terminationGracePeriodSeconds).toBe(10);
  expect(ephemeral.spec.containers[0]).not.toHaveProperty('lifecycle');
  expect(ephemeral.spec.restartPolicy).toBe('Never');
});
