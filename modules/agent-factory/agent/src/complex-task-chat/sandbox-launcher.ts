import { createHash } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import { readFile } from 'node:fs/promises';
import { request } from 'node:https';
import { supervisorLog } from './supervisor-log';
import { buildSandboxPod, validateSandboxAssignment, type SandboxPodInput } from './sandbox-pod';
import { bindSandboxGateway, SANDBOX_CA_NAME, SANDBOX_GATEWAY_HOST, type SandboxGatewayBinding } from './sandbox-gateway-binding';
import {
  GATEWAY_SERVICE_PATH, GATEWAY_EGRESS_POLICY_NAME,
  isGatewayEgress, isSandboxDeny, isCrewDeny, isCrewGatewayEgress, isCrewDashboardIngress, type NetworkPolicySpec,
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
  metadata?: { name?: string; uid?: string; namespace?: string; labels?: Record<string, string>; annotations?: Record<string, string> };
  spec?: Record<string, unknown>;
};

/**
 * The subset of a Pod read back from the API server that decides whether it has started, and,
 * after its turn, whether the API server is still holding the object while the kubelet tears it
 * down (`metadata.deletionTimestamp` set, phase Succeeded/Failed).
 */
export type ObservedPod = {
  metadata?: { name?: string; uid?: string; deletionTimestamp?: string };
  status?: {
    phase?: string;
    podIP?: string;
    containerStatuses?: Array<{ name?: string; state?: Record<string, unknown> }>;
  };
};

export type SandboxStartTiming = { now?: () => number; sleep?: (ms: number) => Promise<void> };

export interface SandboxPodApi {
  resolveGateway(): Promise<SandboxGatewayBinding>;
  create(template: PodTemplate): Promise<CreatedPod>;
  /** Reads the created pod back; `null` once it is gone or its UID no longer matches. */
  inspect(name: string, uid: string): Promise<ObservedPod | null>;
  /**
   * Deletes the pod. Without options the pod's own terminationGracePeriodSeconds applies; with
   * `gracePeriodSeconds: 0` the API server drops the object at once (the kubelet still reclaims
   * the exited container in the background), which is only appropriate once the container exited.
   */
  remove(name: string, uid: string, options?: SandboxRemovalOptions): Promise<void>;
}

export type SandboxRemovalOptions = { gracePeriodSeconds?: 0 };

/** The sandbox container has stopped: terminal pod phase or a terminated container state. */
export function sandboxPodEnded(pod: ObservedPod | null): boolean {
  if (pod === null) return true;
  const phase = pod.status?.phase;
  if (phase === 'Failed' || phase === 'Succeeded') return true;
  return (pod.status?.containerStatuses ?? []).some(status => status.state !== undefined && status.state !== null && 'terminated' in status.state);
}

export interface SandboxPodIdentity {
  name: string;
  uid: string;
}

export const SANDBOX_START_POLL_MS = 500;
const SANDBOX_START_DEADLINE_DEFAULT_MS = 60_000;
const SANDBOX_START_DEADLINE_MIN_MS = 5_000;
const SANDBOX_START_DEADLINE_MAX_MS = 300_000;
const SANDBOX_PULL_ALLOWANCE_DEFAULT_MS = 180_000;
const SANDBOX_PULL_ALLOWANCE_MAX_MS = 600_000;
/**
 * Container waiting reasons under which the sandbox can never start: the kubelet has given up
 * on the image or the container configuration, so waiting for the deadline only delays the
 * failure (#8059).
 */
const SANDBOX_START_FAILURE_REASONS: ReadonlySet<string> = new Set([
  'ImagePullBackOff', 'ErrImagePull', 'InvalidImageName', 'CreateContainerConfigError', 'CreateContainerError',
]);

/**
 * How long the supervisor waits for a created sandbox pod to reach Running before it
 * gives the turn up and deletes the pod. Overridable per deployment through
 * ADP_CHAT_SANDBOX_START_DEADLINE_MS (bounded to 5 s..300 s); defaults to 60 s.
 */
export function sandboxStartDeadlineMs(env: NodeJS.ProcessEnv = process.env): number {
  const raw = env.ADP_CHAT_SANDBOX_START_DEADLINE_MS;
  if (raw === undefined || raw === '') return SANDBOX_START_DEADLINE_DEFAULT_MS;
  const value = /^[0-9]{1,6}$/.test(raw) ? Number(raw) : NaN;
  if (!Number.isSafeInteger(value) || value < SANDBOX_START_DEADLINE_MIN_MS || value > SANDBOX_START_DEADLINE_MAX_MS) {
    throw new Error('Chat sandbox start deadline invalid');
  }
  return value;
}

/**
 * Extra time the supervisor grants once, on top of the start deadline, while the kubelet is
 * still creating the sandbox container (which is where a new image digest is pulled; a cold
 * node needed 1 m 49 s on dev, #8059). ADP_CHAT_SANDBOX_PULL_ALLOWANCE_MS, 0..600 s; default
 * 180 s. A failed pull is never waited for (see SANDBOX_START_FAILURE_REASONS).
 */
export function sandboxPullAllowanceMs(env: NodeJS.ProcessEnv = process.env): number {
  const raw = env.ADP_CHAT_SANDBOX_PULL_ALLOWANCE_MS;
  if (raw === undefined || raw === '') return SANDBOX_PULL_ALLOWANCE_DEFAULT_MS;
  const value = /^[0-9]{1,6}$/.test(raw) ? Number(raw) : NaN;
  if (!Number.isSafeInteger(value) || value > SANDBOX_PULL_ALLOWANCE_MAX_MS) {
    throw new Error('Chat sandbox pull allowance invalid');
  }
  return value;
}

export type SandboxStartState = 'running' | 'pending' | 'pulling' | 'failed' | 'ended';

/** Waiting reasons of the expected containers (`ContainerCreating`, `ImagePullBackOff`, ...). */
export function sandboxWaitingReasons(pod: ObservedPod | null, containers: readonly string[]): string[] {
  return (pod?.status?.containerStatuses ?? [])
    .filter(status => typeof status.name === 'string' && containers.includes(status.name))
    .map(status => (status.state as { waiting?: { reason?: unknown } } | undefined)?.waiting?.reason)
    .filter((reason): reason is string => typeof reason === 'string');
}

/**
 * Mirrors the gateway's workload verifier (src/agentauth/workload.py::verify_bound): a pod is
 * started only when its phase is Running, every expected container is in the `running` state
 * and a podIP is assigned. A pod that reached a terminal phase or whose container already
 * terminated cannot come back under restartPolicy Never, so it is reported as ended; a
 * container the kubelet cannot create (image pull or configuration failure) is `failed`.
 * `pulling` is a container still being created — the kubelet reports an image pull as
 * `ContainerCreating` — which earns the one-off pull allowance in waitForSandboxStart.
 */
export function sandboxStartState(pod: ObservedPod | null, containers: readonly string[]): SandboxStartState {
  if (pod === null || sandboxPodEnded(pod)) return 'ended';
  const phase = pod.status?.phase;
  const statuses = pod.status?.containerStatuses ?? [];
  const running = containers.length > 0 && containers.every(name => {
    const matching = statuses.filter(status => status.name === name);
    return matching.length === 1 && matching[0].state !== undefined && matching[0].state !== null && 'running' in matching[0].state;
  });
  if (phase === 'Running' && running && typeof pod.status?.podIP === 'string' && pod.status.podIP.length > 0) return 'running';
  const reasons = sandboxWaitingReasons(pod, containers);
  if (reasons.some(reason => SANDBOX_START_FAILURE_REASONS.has(reason))) return 'failed';
  if (reasons.includes('ContainerCreating')) return 'pulling';
  return 'pending';
}

export type SandboxStartLog = (status: 'pulling' | 'started', fields: Record<string, string | number>) => void;

export async function waitForSandboxStart(
  api: Pick<SandboxPodApi, 'inspect'>,
  pod: SandboxPodIdentity,
  containers: readonly string[],
  timing: SandboxStartTiming = {},
  deadlineMs: number = sandboxStartDeadlineMs(),
  pullAllowanceMs: number = sandboxPullAllowanceMs(),
  log: SandboxStartLog = (status, fields) => supervisorLog('', 'sandbox_start', status, fields),
): Promise<void> {
  const now = timing.now ?? (() => performance.now());
  const sleep = timing.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  const started = now();
  let deadline = started + deadlineMs;
  let extended = false;
  for (;;) {
    const observed = await api.inspect(pod.name, pod.uid);
    const state = sandboxStartState(observed, containers);
    if (state === 'running') {
      log('started', { elapsed_ms: Math.round(now() - started), pull_allowance_used: extended ? 1 : 0 });
      return;
    }
    if (state === 'ended') throw new Error('Chat sandbox did not start');
    if (state === 'failed') {
      throw new Error(`Chat sandbox cannot start: ${sandboxWaitingReasons(observed, containers).join(',')}`);
    }
    if (state === 'pulling' && !extended) {
      // Image pulls happen while the container is being created. Grant the allowance once so
      // a cold node can finish pulling a new digest, and leave evidence of it (#8059).
      extended = true;
      deadline += pullAllowanceMs;
      log('pulling', { elapsed_ms: Math.round(now() - started), deadline_ms: deadlineMs, pull_allowance_ms: pullAllowanceMs });
    }
    if (now() >= deadline) throw new Error('Chat sandbox did not start before the supervisor deadline');
    await sleep(Math.max(0, Math.min(SANDBOX_START_POLL_MS, deadline - now())));
  }
}

export async function withSandboxPod<Result>(
  input: SandboxPodInput,
  api: SandboxPodApi,
  run: (pod: SandboxPodIdentity) => Promise<Result>,
  cleanup: () => boolean = () => true,
  timing: SandboxStartTiming = {},
): Promise<Result> {
  validateSandboxAssignment(input);
  const template = buildSandboxPod(input.executor === 'kirocrew'
    ? { ...input, dashboardOrigin: process.env.ADP_CREW_DASHBOARD_ORIGIN } : input, await api.resolveGateway());
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
      // Run/session digests are annotations; admission may add its own (e.g. limit-ranger), so
      // require ours to be present and unchanged rather than the whole map to be equal.
      Object.entries(template.metadata.annotations).some(([key, value]) => created.metadata?.annotations?.[key] !== value) ||
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
    // The gateway admits a pod only once it is Running with a podIP (its workload verifier
    // refuses anything else), so the caller must not be handed the pod before that. A pod
    // that never starts is removed here regardless of the caller's cleanup choice.
    await waitForSandboxStart(api, { name, uid }, expected.containers.map(container => container.name), timing,
      sandboxStartDeadlineMs(), sandboxPullAllowanceMs(),
      (status, fields) => supervisorLog(input.runId, 'sandbox_start', status, { pod: name, ...fields }));
    validated = true;
    return await run({ name, uid });
  } finally {
    if (!validated || cleanup()) await api.remove(name, uid);
  }
}

// Pinned by sandbox-network-controls.test.ts against k8s/chat-sandbox-admission.yaml and
// k8s/chat-crew-pod-admission.yaml; recompute with sandboxAdmissionFingerprint() after editing a policy.
export const SANDBOX_ADMISSION_FINGERPRINT = 'baeefd00e70706fc7135a3422eb0725b80a3942768ca76d804ffe089c47981bb';
export const CREW_ADMISSION_FINGERPRINT = '2514f715b23f21dc24236d68deef30509b9f27ffb00aef5c31548ece72c53aaf';

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
  "(oldObject != null && ((oldObject.spec.serviceAccountName == 'adp-chat-sandbox' &&",
  "has(oldObject.metadata.labels) && oldObject.metadata.labels['adp.io/chat-sandbox'] == 'true') ||",
  "(oldObject.spec.serviceAccountName == 'adp-crew-pod' && has(oldObject.metadata.labels) &&",
  "oldObject.metadata.labels['adp.io/crew-pod'] == 'true') ||",
  "(oldObject.spec.serviceAccountName == 'adp-crew-checkpoint-helper' && has(oldObject.metadata.labels) &&",
  "oldObject.metadata.labels['adp.io/crew-checkpoint-helper'] == 'true') ||",
  "(oldObject.spec.serviceAccountName == 'adp-crew-restore-helper' && has(oldObject.metadata.labels) &&",
  "oldObject.metadata.labels['adp.io/crew-restore-helper'] == 'true')) &&",
  "(oldObject.metadata.name.matches('^chat-turn-[a-f0-9]{12}-[a-z0-9]{1,20}$') ||",
  "oldObject.metadata.name.matches('^chat-checkpoint-[a-f0-9]{12}-[a-z0-9]{1,20}$') ||",
  "oldObject.metadata.name.matches('^chat-restore-[a-f0-9]{12}-[a-z0-9]{1,20}$'))) :",
  "(object != null && ((object.spec.serviceAccountName == 'adp-chat-sandbox' &&",
  "has(object.metadata.labels) && object.metadata.labels['adp.io/chat-sandbox'] == 'true') ||",
  "(object.spec.serviceAccountName == 'adp-crew-pod' && has(object.metadata.labels) &&",
  "object.metadata.labels['adp.io/crew-pod'] == 'true') ||",
  "(object.spec.serviceAccountName == 'adp-crew-checkpoint-helper' && has(object.metadata.labels) &&",
  "object.metadata.labels['adp.io/crew-checkpoint-helper'] == 'true') ||",
  "(object.spec.serviceAccountName == 'adp-crew-restore-helper' && has(object.metadata.labels) &&",
  "object.metadata.labels['adp.io/crew-restore-helper'] == 'true')) &&",
  "has(object.metadata.generateName) &&",
  "(object.metadata.generateName.matches('^chat-turn-[a-f0-9]{12}-$') ||",
  "object.metadata.generateName.matches('^chat-checkpoint-[a-f0-9]{12}-$') ||",
  "object.metadata.generateName.matches('^chat-restore-[a-f0-9]{12}-$')))",
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

/**
 * A policy or binding applied without an object/namespace selector is read back from the
 * API server as `{}` (Kubernetes 1.35 defaults the empty LabelSelector) or with the field
 * absent (older servers). Both select everything; only a selector with keys narrows the match.
 */
export function isUnsetSelector(selector: unknown): boolean {
  if (selector == null) return true;
  return typeof selector === 'object' && !Array.isArray(selector) && Object.keys(selector).length === 0;
}

function matchesSandboxNamespace(binding: KubernetesControl | undefined): boolean {
  const resources = binding?.spec?.matchResources;
  return isDeepStrictEqual(resources?.namespaceSelector, {
    matchLabels: { 'kubernetes.io/metadata.name': NAMESPACE },
  }) && isUnsetSelector(resources?.objectSelector) &&
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
    const labels: Record<string, string> = template.metadata.labels;
    const crew = template.spec.serviceAccountName === 'adp-crew-pod' &&
      labels['adp.io/crew-pod'] === 'true' && !('adp.io/chat-sandbox' in labels);
    const sandbox = template.spec.serviceAccountName === 'adp-chat-sandbox' &&
      labels['adp.io/chat-sandbox'] === 'true' && !('adp.io/crew-pod' in labels);
    if (!crew && !sandbox) throw new Error('Chat pod workload class refused');
    await this.ensureSandboxControls(template);
    if (crew) await this.ensureCrewAdmission();
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
      !isUnsetSelector(admission?.spec?.matchConstraints?.objectSelector) ||
      !isUnsetSelector(admission?.spec?.matchConstraints?.namespaceSelector) ||
      admission?.spec?.matchConditions?.[0]?.name !== 'chat-sandbox-only' ||
      !admission?.spec?.matchConditions?.[0]?.expression?.includes('adp-chat-sandbox') ||
      !admission?.spec?.matchConditions?.[0]?.expression?.includes('adp.io/chat-sandbox') ||
      binding?.spec?.policyName !== 'adp-chat-sandbox-template' ||
      supervisorAdmission?.spec?.failurePolicy !== 'Fail' ||
      (supervisorAdmission?.spec?.matchConstraints?.excludeResourceRules?.length ?? 0) !== 0 ||
      !isUnsetSelector(supervisorAdmission?.spec?.matchConstraints?.objectSelector) ||
      !isUnsetSelector(supervisorAdmission?.spec?.matchConstraints?.namespaceSelector) ||
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
      (template.spec.serviceAccountName === 'adp-crew-pod' && (
        policies.items.filter(policy => policy.metadata?.name === 'chat-crew-deny' && isCrewDeny(policy.spec)).length !== 1 ||
        policies.items.filter(policy => policy.metadata?.name === 'chat-crew-gateway-egress' && isCrewGatewayEgress(policy.spec)).length !== 1 ||
        policies.items.filter(policy => policy.metadata?.name === 'crew-pod-dashboard-ingress' && isCrewDashboardIngress(policy.spec)).length !== 1 ||
        policies.items.some(policy => selectsSandbox(policy.spec?.podSelector, template.metadata.labels) &&
          !(policy.metadata?.name === 'chat-crew-gateway-egress' && isCrewGatewayEgress(policy.spec)) &&
          !(policy.metadata?.name === 'crew-pod-dashboard-ingress' && isCrewDashboardIngress(policy.spec)) &&
          ((policy.spec?.egress?.length ?? 0) > 0 || (policy.spec?.ingress?.length ?? 0) > 0))
      )) ||
      (template.spec.serviceAccountName === 'adp-chat-sandbox' && policies.items.some(policy =>
        selectsSandbox(policy.spec?.podSelector, template.metadata.labels) &&
        !(policy.metadata?.name === GATEWAY_EGRESS_POLICY_NAME && isGatewayEgress(policy.spec)) &&
        ((policy.spec?.egress?.length ?? 0) > 0 || (policy.spec?.ingress?.length ?? 0) > 0)))
    ) {
      throw new Error('Chat sandbox admission or effective network controller is not configured');
    }
  }

  private async ensureCrewAdmission(): Promise<void> {
    const [policy, binding] = await Promise.all([
      this.call('GET', '/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicies/adp-crew-pod-template'),
      this.call('GET', '/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicybindings/adp-crew-pod-template'),
    ]) as KubernetesControl[];
    if (policy?.spec?.failurePolicy !== 'Fail' ||
        sandboxAdmissionFingerprint(policy.spec) !== CREW_ADMISSION_FINGERPRINT ||
        (policy.spec.matchConstraints?.excludeResourceRules?.length ?? 0) !== 0 ||
        !isUnsetSelector(policy.spec.matchConstraints?.objectSelector) ||
        !isUnsetSelector(policy.spec.matchConstraints?.namespaceSelector) ||
        binding?.spec?.policyName !== 'adp-crew-pod-template' ||
        !isDeepStrictEqual(binding.spec.validationActions, ['Deny']) ||
        !matchesSandboxNamespace(binding)) {
      throw new Error('Chat Crew admission is not configured');
    }
  }

  async inspect(name: string, uid: string): Promise<ObservedPod | null> {
    if (!/^chat-turn-[0-9a-f]{12}-[a-z0-9]{1,20}$/.test(name) || !/^[a-z0-9-]{8,128}$/.test(uid)) {
      throw new Error('Chat sandbox inspection requires the created name and UID');
    }
    // Namespace-scoped `get` on the one created pod (Role chat-supervisor-pods); no list/watch.
    const pod = await this.call('GET', `${PODS_PATH}/${name}`, undefined, true) as ObservedPod | null;
    if (pod === null || typeof pod !== 'object' || pod.metadata?.uid !== uid) return null;
    return pod;
  }

  async remove(name: string, uid: string, options: SandboxRemovalOptions = {}): Promise<void> {
    if (!/^chat-turn-[0-9a-f]{12}-[a-z0-9]{1,20}$/.test(name) || !/^[a-z0-9-]{8,128}$/.test(uid)) {
      throw new Error('Chat sandbox deletion requires the created name and UID');
    }
    if (options.gracePeriodSeconds !== undefined && options.gracePeriodSeconds !== 0) {
      throw new Error('Chat sandbox deletion grace period must be the pod default or zero');
    }
    await this.call('DELETE', `${PODS_PATH}/${name}`, {
      apiVersion: 'v1', kind: 'DeleteOptions', preconditions: { uid },
      // A second DELETE with gracePeriodSeconds 0 on a pod that is already Terminating is how the
      // API server is told to stop waiting for the kubelet's confirmation (#8047).
      ...(options.gracePeriodSeconds === 0 ? { gracePeriodSeconds: 0, propagationPolicy: 'Background' } : {}),
    });
  }

  protected async call(method: 'GET' | 'POST' | 'DELETE', path: string, body?: object, absentAsNull = false): Promise<unknown> {
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
          if (absentAsNull && method === 'GET' && response.statusCode === 404) return resolve(null);
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
