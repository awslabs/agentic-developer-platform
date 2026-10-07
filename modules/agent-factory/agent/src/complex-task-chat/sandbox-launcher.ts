import { createHash } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import { readFile } from 'node:fs/promises';
import { request } from 'node:https';
import { buildSandboxPod, validateSandboxAssignment, type SandboxPodInput } from './sandbox-pod';
import { bindSandboxGateway, SANDBOX_CA_NAME, SANDBOX_GATEWAY_HOST, type SandboxGatewayBinding } from './sandbox-gateway-binding';
import {
  GATEWAY_SERVICE_PATH, GATEWAY_EGRESS_POLICY_NAME,
  isGatewayEgress, isSandboxDeny, type NetworkPolicySpec,
} from './sandbox-gateway-network';

type PodSpec = ReturnType<typeof buildSandboxPod>['spec'];
type PodTemplate = ReturnType<typeof buildSandboxPod>;
type PolicySelector = {
  matchLabels?: Record<string, string>;
  matchExpressions?: Array<{ key: string; operator: string; values?: string[] }>;
};
type KubernetesControl = {
  data?: Record<string, string>;
  metadata?: { continue?: string };
  items?: Array<{ metadata?: { name?: string }; spec?: NetworkPolicySpec }>;
  spec?: {
    failurePolicy?: string;
    validations?: Array<{ expression?: string }>;
    matchConstraints?: {
      resourceRules?: Array<{ apiGroups?: string[]; apiVersions?: string[]; operations?: string[]; resources?: string[]; scope?: string }>;
      excludeResourceRules?: unknown[];
      objectSelector?: unknown;
      namespaceSelector?: unknown;
    };
    matchConditions?: Array<{ name?: string; expression?: string }>;
    policyName?: string;
    validationActions?: string[];
    matchResources?: {
      namespaceSelector?: { matchLabels?: Record<string, string> };
      objectSelector?: unknown;
      resourceRules?: unknown[];
      excludeResourceRules?: unknown[];
    };
    podSelector?: { matchLabels?: Record<string, string> };
    policyTypes?: string[];
    ingress?: unknown[];
    egress?: unknown[];
  };
};

type CreatedPod = {
  metadata?: { name?: string; uid?: string; namespace?: string; labels?: Record<string, string> };
  spec?: Record<string, unknown>;
};

export interface SandboxPodApi {
  resolveGateway(): Promise<SandboxGatewayBinding>;
  create(template: PodTemplate): Promise<CreatedPod>;
  remove(name: string, uid: string): Promise<void>;
}

export interface SandboxPodIdentity {
  name: string;
  uid: string;
}

export async function withSandboxPod<Result>(
  input: SandboxPodInput,
  api: SandboxPodApi,
  run: (pod: SandboxPodIdentity) => Promise<Result>,
  cleanup: () => boolean = () => true,
): Promise<Result> {
  validateSandboxAssignment(input);
  const template = buildSandboxPod(input, await api.resolveGateway());
  const created = await api.create(template);
  const name = created.metadata?.name;
  const uid = created.metadata?.uid;
  if (
    typeof name !== 'string' ||
    !new RegExp(`^${template.metadata.generateName}[a-z0-9]{1,20}$`).test(name) ||
    (input.podName !== undefined && name !== input.podName) ||
    typeof uid !== 'string' ||
    !/^[a-z0-9-]{8,128}$/.test(uid)
  ) {
    throw new Error('Chat sandbox creation returned no bound pod identity');
  }
  let validated = false;
  try {
    const spec = created.spec as (Partial<PodSpec> & Record<string, unknown>) | undefined;
    const expected = template.spec;
    if (
      created.metadata?.namespace !== template.metadata.namespace ||
      !isDeepStrictEqual(created.metadata?.labels, template.metadata.labels) ||
      spec?.serviceAccountName !== expected.serviceAccountName ||
      spec.automountServiceAccountToken !== false ||
      (spec.hostNetwork ?? false) !== false || (spec.hostPID ?? false) !== false || (spec.hostIPC ?? false) !== false ||
      !isDeepStrictEqual(spec.hostAliases, expected.hostAliases) ||
      spec.shareProcessNamespace !== false || spec.enableServiceLinks !== false ||
      spec.restartPolicy !== 'Never' || spec.activeDeadlineSeconds !== expected.activeDeadlineSeconds ||
      spec.terminationGracePeriodSeconds !== expected.terminationGracePeriodSeconds ||
      !isDeepStrictEqual(spec.securityContext, expected.securityContext) ||
      (Array.isArray(spec.imagePullSecrets) && spec.imagePullSecrets.length > 0) ||
      !isDeepStrictEqual(spec.volumes, expected.volumes) ||
      !isDeepStrictEqual(spec.containers?.map(container => ({
        name: container.name,
        image: container.image,
        imagePullPolicy: container.imagePullPolicy,
        command: container.command,
        args: 'args' in container ? container.args : undefined,
        env: container.env,
        envFrom: 'envFrom' in container ? container.envFrom : undefined,
        workingDir: container.workingDir,
        volumeMounts: container.volumeMounts,
        securityContext: container.securityContext,
        resources: container.resources,
      })), expected.containers.map(container => ({
        name: container.name,
        image: container.image,
        imagePullPolicy: container.imagePullPolicy,
        command: container.command,
        args: 'args' in container ? container.args : undefined,
        env: container.env,
        envFrom: 'envFrom' in container ? container.envFrom : undefined,
        workingDir: container.workingDir,
        volumeMounts: container.volumeMounts,
        securityContext: container.securityContext,
        resources: container.resources,
      }))) ||
      (spec as Record<string, unknown>).initContainers != null ||
      (spec as Record<string, unknown>).ephemeralContainers != null
    ) {
      throw new Error('Chat sandbox admission changed the restricted pod identity or mounts');
    }
    validated = true;
    return await run({ name, uid });
  } finally {
    if (!validated || cleanup()) await api.remove(name, uid);
  }
}

const SANDBOX_ADMISSION_FINGERPRINT = 'd08af2b223d91a76ffb112e5facab7c12ceaf1e924e4edff2bae7e9687066435';

export function sandboxAdmissionFingerprint(spec: KubernetesControl['spec']): string {
  const normalized = (expression?: string) => expression?.replace(/\s+/g, ' ').trim() ?? '';
  const rules = spec?.matchConstraints?.resourceRules?.map(rule => ({
    apiGroups: rule.apiGroups, apiVersions: rule.apiVersions, operations: rule.operations,
    resources: rule.resources, scope: rule.scope,
  }));
  const conditions = spec?.matchConditions?.map(condition => [condition.name, normalized(condition.expression)]);
  const validations = spec?.validations?.map(validation => normalized(validation.expression));
  return createHash('sha256').update(JSON.stringify({ rules, conditions, validations })).digest('hex');
}

const NAMESPACE = 'adp-gateway-agents';
const SUPERVISOR_ACTOR = "request.userInfo.username == 'system:serviceaccount:adp-gateway-agents:adp-chat-supervisor'";
export const SUPERVISOR_POD_EXPRESSION = [
  "request.operation == 'DELETE' ?",
  "(oldObject != null && oldObject.spec.serviceAccountName == 'adp-chat-sandbox' &&",
  "has(oldObject.metadata.labels) && oldObject.metadata.labels['adp.io/chat-sandbox'] == 'true' &&",
  "oldObject.metadata.name.matches('^chat-turn-[a-f0-9]{12}-[a-z0-9]{1,20}$')) :",
  "(object != null && object.spec.serviceAccountName == 'adp-chat-sandbox' &&",
  "has(object.metadata.labels) && object.metadata.labels['adp.io/chat-sandbox'] == 'true' &&",
  "has(object.metadata.generateName) &&",
  "object.metadata.generateName.matches('^chat-turn-[a-f0-9]{12}-$'))",
].join(' ');
const TOKEN = '/var/run/secrets/kubernetes.io/serviceaccount/token';
const CA = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt';
const PODS_PATH = `/api/v1/namespaces/${NAMESPACE}/pods`;
const NETWORK_PATH = `/apis/networking.k8s.io/v1/namespaces/${NAMESPACE}/networkpolicies`;

function selectsSandbox(selector: PolicySelector | undefined, labels: Record<string, string>): boolean {
  if (!selector) return true;
  if (Object.entries(selector.matchLabels ?? {}).some(([key, value]) => labels[key] !== value)) return false;
  return (selector.matchExpressions ?? []).every(({ key, operator, values }) => {
    if (operator === 'In') return values?.includes(labels[key]) ?? false;
    if (operator === 'NotIn') return !values?.includes(labels[key]);
    if (operator === 'Exists') return key in labels;
    if (operator === 'DoesNotExist') return !(key in labels);
    return true;
  });
}

function matchesSandboxNamespace(binding: KubernetesControl | undefined): boolean {
  const resources = binding?.spec?.matchResources;
  return isDeepStrictEqual(resources?.namespaceSelector, {
    matchLabels: { 'kubernetes.io/metadata.name': NAMESPACE },
  }) && resources?.objectSelector == null &&
    (resources?.resourceRules?.length ?? 0) === 0 &&
    (resources?.excludeResourceRules?.length ?? 0) === 0;
}

export class InClusterSandboxPodApi implements SandboxPodApi {
  constructor(private readonly caConfigMap = process.env.ADP_CHAT_SANDBOX_CA_CONFIGMAP ?? '') {}

  async resolveGateway(): Promise<SandboxGatewayBinding> {
    if (!SANDBOX_CA_NAME.test(this.caConfigMap)) throw new Error('Chat sandbox gateway trust unavailable');
    const [service, ca] = await Promise.all([
      this.call('GET', GATEWAY_SERVICE_PATH),
      this.call('GET', `/api/v1/namespaces/${NAMESPACE}/configmaps/${this.caConfigMap}`),
    ]);
    return bindSandboxGateway(service, ca, this.caConfigMap);
  }

  async create(template: PodTemplate): Promise<CreatedPod> {
    if (template.metadata.namespace !== NAMESPACE) {
      throw new Error('Chat sandbox namespace refused');
    }
    await this.ensureSandboxControls(template);
    return await this.call('POST', PODS_PATH, template) as CreatedPod;
  }

  private async ensureSandboxControls(template: PodTemplate): Promise<void> {
    const gateway = await this.resolveGateway();
    if (!isDeepStrictEqual(template.spec.hostAliases, [{ ip: gateway.clusterIP, hostnames: [SANDBOX_GATEWAY_HOST] }]) ||
        template.spec.volumes.find(volume => volume.name === 'gateway-ca')?.configMap?.name !== gateway.caConfigMap) {
      throw new Error('Chat sandbox gateway binding changed');
    }
    const [controller, admission, binding, supervisorAdmission, supervisorBinding, network, policies] = await Promise.all([
      this.call('GET', '/api/v1/namespaces/kube-system/configmaps/amazon-vpc-cni'),
      this.call('GET', '/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicies/adp-chat-sandbox-template'),
      this.call('GET', '/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicybindings/adp-chat-sandbox-template'),
      this.call('GET', '/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicies/adp-chat-supervisor-pods'),
      this.call('GET', '/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicybindings/adp-chat-supervisor-pods'),
      this.call('GET', `${NETWORK_PATH}/chat-sandbox-deny`),
      this.call('GET', NETWORK_PATH),
    ]) as KubernetesControl[];
    if (
      controller?.data?.['enable-network-policy-controller'] !== 'true' ||
      admission?.spec?.failurePolicy !== 'Fail' ||
      sandboxAdmissionFingerprint(admission?.spec) !== SANDBOX_ADMISSION_FINGERPRINT ||
      (admission?.spec?.matchConstraints?.excludeResourceRules?.length ?? 0) !== 0 ||
      admission?.spec?.matchConstraints?.objectSelector != null ||
      admission?.spec?.matchConstraints?.namespaceSelector != null ||
      admission?.spec?.matchConditions?.[0]?.name !== 'chat-sandbox-only' ||
      !admission?.spec?.matchConditions?.[0]?.expression?.includes('adp-chat-sandbox') ||
      !admission?.spec?.matchConditions?.[0]?.expression?.includes('adp.io/chat-sandbox') ||
      binding?.spec?.policyName !== 'adp-chat-sandbox-template' ||
      supervisorAdmission?.spec?.failurePolicy !== 'Fail' ||
      (supervisorAdmission?.spec?.matchConstraints?.excludeResourceRules?.length ?? 0) !== 0 ||
      supervisorAdmission?.spec?.matchConstraints?.objectSelector != null ||
      supervisorAdmission?.spec?.matchConstraints?.namespaceSelector != null ||
      !isDeepStrictEqual(supervisorAdmission?.spec?.matchConstraints?.resourceRules, [{
        apiGroups: [''], apiVersions: ['v1'], operations: ['CREATE', 'UPDATE', 'DELETE'], resources: ['pods'], scope: 'Namespaced',
      }]) ||
      !isDeepStrictEqual(supervisorAdmission?.spec?.matchConditions, [{ name: 'chat-supervisor-only', expression: SUPERVISOR_ACTOR }]) ||
      supervisorAdmission?.spec?.validations?.length !== 1 ||
      supervisorAdmission.spec.validations[0].expression?.replace(/\s+/g, ' ').trim() !== SUPERVISOR_POD_EXPRESSION ||
      supervisorBinding?.spec?.policyName !== 'adp-chat-supervisor-pods' ||
      !isDeepStrictEqual(supervisorBinding?.spec?.validationActions, ['Deny']) ||
      !matchesSandboxNamespace(supervisorBinding) ||
      !binding?.spec?.validationActions?.includes('Deny') ||
      !matchesSandboxNamespace(binding) ||
      !isSandboxDeny(network?.spec) ||
      !Array.isArray(policies?.items) ||
      Boolean(policies.metadata?.continue) ||
      policies.items.filter(policy => policy.metadata?.name === 'chat-sandbox-deny' && isSandboxDeny(policy.spec)).length !== 1 ||
      policies.items.filter(policy => policy.metadata?.name === GATEWAY_EGRESS_POLICY_NAME && isGatewayEgress(policy.spec)).length !== 1 ||
      policies.items.some(policy => selectsSandbox(policy.spec?.podSelector, template.metadata.labels) &&
        !(policy.metadata?.name === GATEWAY_EGRESS_POLICY_NAME && isGatewayEgress(policy.spec)) &&
        ((policy.spec?.egress?.length ?? 0) > 0 || (policy.spec?.ingress?.length ?? 0) > 0))
    ) {
      throw new Error('Chat sandbox admission or effective network controller is not configured');
    }
  }

  async remove(name: string, uid: string): Promise<void> {
    if (!/^chat-turn-[0-9a-f]{12}-[a-z0-9]{1,20}$/.test(name) || !/^[a-z0-9-]{8,128}$/.test(uid)) {
      throw new Error('Chat sandbox deletion requires the created name and UID');
    }
    await this.call('DELETE', `${PODS_PATH}/${name}`, {
      apiVersion: 'v1', kind: 'DeleteOptions', preconditions: { uid },
    });
  }

  protected async call(method: 'GET' | 'POST' | 'DELETE', path: string, body?: object): Promise<unknown> {
    const [token, ca] = await Promise.all([readFile(TOKEN, 'utf8'), readFile(CA)]);
    if (!token.trim() || !ca.length) throw new Error('Chat supervisor Kubernetes identity unavailable');
    const payload = body === undefined ? '' : JSON.stringify(body);
    return await new Promise((resolve, reject) => {
      const connection = request({
        hostname: 'kubernetes.default.svc', port: 443, path, method,
        ca, timeout: 10_000, agent: false,
        headers: {
          Authorization: `Bearer ${token.trim()}`,
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(payload),
        },
      }, response => {
        let data = '';
        response.on('data', (chunk: Buffer) => {
          data += chunk.toString('utf8');
          if (data.length > 65_536) connection.destroy(new Error('Chat sandbox Kubernetes response too large'));
        });
        response.on('end', () => {
          if (method === 'DELETE' && response.statusCode === 404) return resolve({});
          if (![200, 201, 202].includes(response.statusCode ?? 0)) {
            return reject(new Error('Chat sandbox Kubernetes operation refused'));
          }
          try {
            resolve(JSON.parse(data));
          } catch {
            reject(new Error('Chat sandbox Kubernetes response invalid'));
          }
        });
      });
      connection.on('timeout', () => connection.destroy(new Error('Chat sandbox Kubernetes request timed out')));
      connection.on('error', () => reject(new Error('Chat sandbox Kubernetes request failed')));
      connection.end(payload);
    });
  }
}
