import { InClusterSandboxPodApi, SUPERVISOR_POD_EXPRESSION } from './sandbox-launcher';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { type NetworkPolicySpec } from './sandbox-gateway-network';

const yaml = require('js-yaml');
const sandboxAdmission = yaml.loadAll(readFileSync(resolve(__dirname, '../../k8s/chat-sandbox-admission.yaml'), 'utf8'))[0];
const gatewayEgress = yaml.load(readFileSync(resolve(__dirname, '../../k8s/chat-sandbox-gateway-egress.yaml'), 'utf8'));
import { buildSandboxPod } from './sandbox-pod';
import { gatewayBinding, gatewayCa } from './sandbox-gateway.fixture';

const assignment = {
  runId: 'run-a',
  image: `registry.example.test/chat-sandbox@sha256:${'a'.repeat(64)}`,
  gatewayUrl: 'https://gateway.example.test',
};
const template = buildSandboxPod(assignment, gatewayBinding);

type Policy = { metadata: { name: string }; spec: NetworkPolicySpec };

function controls() {
  return {
    controller: { data: { 'enable-network-policy-controller': 'true' } },
    admission: { spec: structuredClone(sandboxAdmission.spec) },
    binding: { spec: { policyName: 'adp-chat-sandbox-template', validationActions: ['Deny'], matchResources: {
      namespaceSelector: { matchLabels: { 'kubernetes.io/metadata.name': 'adp-gateway-agents' } },
    } } },
    supervisorAdmission: { spec: { failurePolicy: 'Fail', matchConstraints: { resourceRules: [{
      apiGroups: [''], apiVersions: ['v1'], operations: ['CREATE', 'UPDATE', 'DELETE'], resources: ['pods'], scope: 'Namespaced',
    }] }, matchConditions: [{
      name: 'chat-supervisor-only', expression: "request.userInfo.username == 'system:serviceaccount:adp-gateway-agents:adp-chat-supervisor'",
    }], validations: [{ expression: SUPERVISOR_POD_EXPRESSION }] } },
    supervisorBinding: { spec: { policyName: 'adp-chat-supervisor-pods', validationActions: ['Deny'], matchResources: {
      namespaceSelector: { matchLabels: { 'kubernetes.io/metadata.name': 'adp-gateway-agents' } },
    } } },
    network: { spec: { podSelector: { matchLabels: { 'adp.io/chat-sandbox': 'true' } }, policyTypes: ['Ingress', 'Egress'], ingress: [], egress: [] } },
    policies: { items: [
      { metadata: { name: 'chat-sandbox-deny' }, spec: { podSelector: { matchLabels: { 'adp.io/chat-sandbox': 'true' } }, policyTypes: ['Ingress', 'Egress'], egress: [], ingress: [] } },
      structuredClone(gatewayEgress),
    ] as Policy[] },
    gateway: {
      metadata: { name: 'chat-sandbox-gateway', namespace: 'adp-gateway' },
      spec: { type: 'ClusterIP', clusterIP: '10.100.42.17', clusterIPs: ['10.100.42.17'],
        selector: { 'app.kubernetes.io/name': 'chat-sandbox-gateway' },
        ports: [{ name: 'https', protocol: 'TCP', port: 8443, targetPort: 8443 }],
      },
    },
  };
}

type Controls = ReturnType<typeof controls>;
class FakeApi extends InClusterSandboxPodApi {
  readonly calls: string[] = [];
  constructor(private readonly state: Controls) { super(gatewayBinding.caConfigMap); }

  protected override async call(method: 'GET' | 'POST' | 'DELETE', path: string): Promise<unknown> {
    this.calls.push(`${method} ${path}`);
    if (method === 'POST') return { metadata: { name: template.metadata.generateName + 'abcde', uid: '01234567-89ab-cdef-0123-456789abcdef', namespace: template.metadata.namespace }, spec: template.spec };
    if (path.endsWith(`/configmaps/${gatewayBinding.caConfigMap}`)) return gatewayCa;
    if (path.endsWith('/configmaps/amazon-vpc-cni')) return this.state.controller;
    if (path.endsWith('/validatingadmissionpolicies/adp-chat-sandbox-template')) return this.state.admission;
    if (path.endsWith('/validatingadmissionpolicybindings/adp-chat-sandbox-template')) return this.state.binding;
    if (path.endsWith('/validatingadmissionpolicies/adp-chat-supervisor-pods')) return this.state.supervisorAdmission;
    if (path.endsWith('/validatingadmissionpolicybindings/adp-chat-supervisor-pods')) return this.state.supervisorBinding;
    if (path.endsWith('/networkpolicies/chat-sandbox-deny')) return this.state.network;
    if (path.endsWith('/networkpolicies')) return JSON.parse(JSON.stringify(this.state.policies));
    if (path === '/api/v1/namespaces/adp-gateway/services/chat-sandbox-gateway') return this.state.gateway;
    throw new Error('unexpected Kubernetes API request');
  }
}

test('launch requires admission, default deny, exact gateway egress and a dedicated Service before pod POST', async () => {
  const api = new FakeApi(controls());
  await api.create(template);
  expect(api.calls).toContain('POST /api/v1/namespaces/adp-gateway-agents/pods');
  expect(api.calls.filter(call => call.startsWith('GET '))).toHaveLength(9);
  expect(api.calls.at(-1)).toBe('POST /api/v1/namespaces/adp-gateway-agents/pods');
});

test('refuses a hollow sandbox admission policy before creating a pod', async () => {
  const state = controls();
  state.admission.spec.validations = [{ expression: 'true' }];
  const api = new FakeApi(state);
  await expect(api.create(template)).rejects.toThrow();
  expect(api.calls).not.toContain('POST /api/v1/namespaces/adp-gateway-agents/pods');
});

test.each([
  { name: 'supervisor admission fail-open', mutate: (state: Controls) => { state.supervisorAdmission.spec.failurePolicy = 'Ignore'; } },
  { name: 'supervisor actor match removed', mutate: (state: Controls) => { state.supervisorAdmission.spec.matchConditions[0].expression = 'true'; } },
  { name: 'supervisor delete unguarded', mutate: (state: Controls) => { state.supervisorAdmission.spec.matchConstraints.resourceRules[0].operations = ['CREATE', 'UPDATE']; } },
  { name: 'supervisor can create arbitrary pod', mutate: (state: Controls) => { state.supervisorAdmission.spec.validations[0].expression = 'true'; } },
  { name: 'supervisor binding does not deny', mutate: (state: Controls) => { state.supervisorBinding.spec.validationActions = ['Audit']; } },
  { name: 'supervisor binding outside namespace', mutate: (state: Controls) => { state.supervisorBinding.spec.matchResources.namespaceSelector.matchLabels['kubernetes.io/metadata.name'] = 'elsewhere'; } },
  { name: 'controller disabled', mutate: (state: Controls) => { state.controller.data['enable-network-policy-controller'] = 'false'; } },
  { name: 'admission fail-open', mutate: (state: Controls) => { state.admission.spec.failurePolicy = 'Ignore'; } },
  { name: 'service-account selector removed', mutate: (state: Controls) => { state.admission.spec.matchConditions[0].expression = 'true'; } },
  { name: 'sandbox admission skips pod updates', mutate: (state: Controls) => { state.admission.spec.matchConstraints.resourceRules[0].operations = ['CREATE']; } },
  { name: 'sandbox admission excludes sandbox pods', mutate: (state: Controls) => { state.admission.spec.matchConstraints.excludeResourceRules = [{ apiGroups: [''], apiVersions: ['v1'], operations: ['CREATE'], resources: ['pods'] }]; } },
  { name: 'binding selectors omit sandbox pods', mutate: (state: Controls) => { (state.binding.spec.matchResources as Record<string, unknown>).objectSelector = { matchLabels: { app: 'not-sandbox' } }; } },
  { name: 'supervisor binding selectors omit supervisor pods', mutate: (state: Controls) => { (state.supervisorBinding.spec.matchResources as Record<string, unknown>).objectSelector = { matchLabels: { app: 'not-sandbox' } }; } },
  { name: 'sandbox admission excludes its service account', mutate: (state: Controls) => { state.admission.spec.matchConditions.push({ name: 'skip', expression: 'false' }); } },
  { name: 'sandbox admission permits foreign identities', mutate: (state: Controls) => { state.admission.spec.validations[0].expression = 'true'; } },
  { name: 'sandbox admission permits foreign mounts', mutate: (state: Controls) => { state.admission.spec.validations[3].expression = 'true'; } },
  { name: 'sandbox admission permits injected environment', mutate: (state: Controls) => { state.admission.spec.validations[5].expression = 'true'; } },
  { name: 'binding does not deny', mutate: (state: Controls) => { state.binding.spec.validationActions = ['Audit']; } },
  { name: 'binding covers another namespace', mutate: (state: Controls) => { state.binding.spec.matchResources.namespaceSelector.matchLabels['kubernetes.io/metadata.name'] = 'other'; } },
  { name: 'deny policy selects no sandboxes', mutate: (state: Controls) => { state.network.spec.podSelector.matchLabels['adp.io/chat-sandbox'] = 'false'; } },
  { name: 'deny policy omits egress', mutate: (state: Controls) => { state.network.spec.policyTypes = ['Ingress']; } },
  { name: 'another policy widens all egress', mutate: (state: Controls) => { state.policies.items.push({ metadata: { name: 'widen-all' }, spec: { podSelector: { matchLabels: {} }, egress: [{}] } }); } },
  { name: 'another policy widens sandbox egress', mutate: (state: Controls) => { state.policies.items.push({ metadata: { name: 'widen-sandbox' }, spec: { podSelector: { matchLabels: { 'adp.io/chat-sandbox': 'true' } }, egress: [{}] } }); } },
  { name: 'another policy widens ingress', mutate: (state: Controls) => { state.policies.items.push({ metadata: { name: 'widen-inbound' }, spec: { podSelector: { matchLabels: {} }, ingress: [{}] } }); } },
  { name: 'deny policy not installed', mutate: (state: Controls) => { state.policies.items = []; } },
  { name: 'policy list is incomplete', mutate: (state: Controls) => { Object.assign(state.policies, { metadata: { continue: 'next-page' } }); } },
  { name: 'gateway egress not installed', mutate: (state: Controls) => { state.policies.items.pop(); } },
  { name: 'gateway egress selects other pods', mutate: (state: Controls) => { state.policies.items[1].spec.podSelector = { matchLabels: { app: 'other' } }; } },
  { name: 'gateway egress does not enforce egress', mutate: (state: Controls) => { state.policies.items[1].spec.policyTypes = ['Ingress']; } },
  { name: 'gateway egress widens ingress', mutate: (state: Controls) => { state.policies.items[1].spec.ingress = [{}]; } },
  { name: 'deny policy differs in list snapshot', mutate: (state: Controls) => { state.policies.items[0].spec.podSelector = { matchLabels: { app: 'other' } }; } },
  { name: 'another policy widens egress via matchExpressions', mutate: (state: Controls) => { state.policies.items.push({ metadata: { name: 'expressions' }, spec: { podSelector: { matchExpressions: [{ key: 'adp.io/chat-sandbox', operator: 'In', values: ['true'] }] }, egress: [{}] } }); } },
  { name: 'another policy grants DNS egress', mutate: (state: Controls) => { state.policies.items.push({ metadata: { name: 'dns' }, spec: { podSelector: {}, egress: [{ ports: [{ protocol: 'UDP', port: 53 }] }] } }); } },
  { name: 'another policy repeats the gateway grant', mutate: (state: Controls) => { state.policies.items.push({ ...structuredClone(gatewayEgress), metadata: { name: 'duplicate-grant' } }); } },
  { name: 'gateway Service selects the general gateway', mutate: (state: Controls) => { state.gateway.spec.selector['app.kubernetes.io/name'] = 'gateway'; } },
  { name: 'gateway Service is in another namespace', mutate: (state: Controls) => { state.gateway.metadata.namespace = 'other'; } },
  { name: 'gateway Service is not the fixed destination', mutate: (state: Controls) => { state.gateway.metadata.name = 'gateway'; } },
  { name: 'gateway Service is being deleted', mutate: (state: Controls) => { Object.assign(state.gateway.metadata, { deletionTimestamp: '2026-10-06T00:00:00Z' }); } },
  { name: 'gateway Service has no selector', mutate: (state: Controls) => { state.gateway.spec.selector = {} as typeof state.gateway.spec.selector; } },
  { name: 'gateway Service exposes HTTP', mutate: (state: Controls) => { state.gateway.spec.ports[0].targetPort = 8000; } },
  { name: 'gateway Service exposes another port', mutate: (state: Controls) => { state.gateway.spec.ports.push({ name: 'admin', port: 9000, targetPort: 9000, protocol: 'TCP' }); } },
  { name: 'gateway Service permits external IPs', mutate: (state: Controls) => { Object.assign(state.gateway.spec, { externalIPs: ['192.0.2.10'] }); } },
  { name: 'gateway Service permits external name', mutate: (state: Controls) => { Object.assign(state.gateway.spec, { externalName: 'elsewhere.example.test' }); } },
  { name: 'gateway Service is a LoadBalancer', mutate: (state: Controls) => { state.gateway.spec.type = 'LoadBalancer'; } },
  { name: 'gateway Service is a NodePort', mutate: (state: Controls) => { state.gateway.spec.type = 'NodePort'; } },
  { name: 'gateway Service is dual-stack', mutate: (state: Controls) => { state.gateway.spec.clusterIPs.push('fd00::1'); } },
])('refuses pod creation when $name', async ({ mutate }) => {
  const state = controls();
  mutate(state);
  const api = new FakeApi(state);
  await expect(api.create(template)).rejects.toThrow(/not configured|gateway trust/);
  expect(api.calls).not.toContain('POST /api/v1/namespaces/adp-gateway-agents/pods');
});

test.each([
  'None', '', '127.0.0.1', '169.254.169.254', '192.0.2.10', '::1', '10.100.42.999',
])('refuses a gateway Service with unusable or non-private ClusterIP %s', async address => {
  const state = controls();
  state.gateway.spec.clusterIP = address;
  state.gateway.spec.clusterIPs = [address];
  const api = new FakeApi(state);
  await expect(api.create(template)).rejects.toThrow(/not configured|gateway trust/);
  expect(api.calls.some(call => call.startsWith('POST '))).toBe(false);
});

test.each([
  [{}],
  [{ to: [{ namespaceSelector: {} }], ports: [{ protocol: 'TCP', port: 8443 }] }],
  [{ to: [{ podSelector: {} }], ports: [{ protocol: 'TCP', port: 8443 }] }],
  [{ to: [{ ipBlock: { cidr: '0.0.0.0/0' } }], ports: [{ protocol: 'TCP', port: 8443 }] }],
  [{ to: [{ namespaceSelector: { matchLabels: { 'kubernetes.io/metadata.name': 'adp-gateway' } } },
    { podSelector: { matchLabels: { 'app.kubernetes.io/name': 'chat-sandbox-gateway' } } }],
    ports: [{ protocol: 'TCP', port: 8443 }] }],
  [{ ...gatewayEgress.spec.egress[0], ports: [{ protocol: 'TCP', port: 443 }] }],
  [{ ...gatewayEgress.spec.egress[0], ports: [{ protocol: 'UDP', port: 8443 }] }],
  [{ ...gatewayEgress.spec.egress[0], ports: [{ protocol: 'TCP', port: 8443, endPort: 9000 }] }],
  [{ ...gatewayEgress.spec.egress[0], ports: [] }],
  [...gatewayEgress.spec.egress, {}],
].map(egress => ({ egress })))('refuses gateway egress broadening %#', async ({ egress }) => {
  const state = controls();
  state.policies.items[1].spec.egress = egress;
  const api = new FakeApi(state);
  await expect(api.create(template)).rejects.toThrow(/not configured|gateway trust/);
  expect(api.calls.some(call => call.startsWith('POST '))).toBe(false);
});

test('does not treat an unrelated pod policy as a sandbox grant', async () => {
  const state = controls();
  state.policies.items.push({ metadata: { name: 'other-app' }, spec: {
    podSelector: { matchLabels: { app: 'unrelated' } }, egress: [{}], ingress: [{}],
  } });
  await expect(new FakeApi(state).create(template)).resolves.toBeDefined();
});

test('missing gateway Service lookup fails closed before pod creation', async () => {
  const api = new FakeApi(controls());
  const original = api['call'].bind(api);
  jest.spyOn(api as any, 'call').mockImplementation(async (...args: unknown[]) => {
    const [method, path] = args as ['GET' | 'POST' | 'DELETE', string];
    if (path.endsWith('/services/chat-sandbox-gateway')) throw new Error('Service unavailable');
    return original(method, path);
  });
  await expect(api.create(template)).rejects.toThrow('Service unavailable');
  expect(api.calls.some(call => call.startsWith('POST '))).toBe(false);
});

test('Service replacement between discovery and pod creation refuses the stale address', async () => {
  const state = controls();
  const api = new FakeApi(state);
  expect(await api.resolveGateway()).toEqual(gatewayBinding);
  state.gateway.spec.clusterIP = '10.100.42.18';
  state.gateway.spec.clusterIPs = ['10.100.42.18'];
  await expect(api.create(template)).rejects.toThrow('binding changed');
  expect(api.calls.some(call => call.startsWith('POST '))).toBe(false);
});
