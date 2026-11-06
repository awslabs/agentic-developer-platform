import { createHash } from 'node:crypto';
import { validateBaseUrl } from '../lib/url-guard';
import { SANDBOX_CA_FILE, SANDBOX_GATEWAY_HOST, SANDBOX_GATEWAY_URL, validateGatewayBinding, type SandboxGatewayBinding } from './sandbox-gateway-binding';

const IMAGE = /^[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}$/;
const RUN_ID = /^[A-Za-z0-9_.:-]{1,128}$/;
const EXECUTE_API_HOST = /^[a-z0-9]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com$/;
const STAGE_PATH = /^\/[A-Za-z0-9_-]+$/;
// Kubernetes label values: at most 63 characters, alphanumeric at both ends, [-._A-Za-z0-9] inside
// (or empty). A sha256 hex digest is 64 characters and is refused by the API server with 422.
const LABEL_VALUE = /^(?:[A-Za-z0-9](?:[-._A-Za-z0-9]{0,61}[A-Za-z0-9])?)?$/;

/** Where supervisor requests to the gateway's internal plane are sent. */
export type SupervisorGatewayTarget = {
  /** scheme + host (+ port) the request is sent to */
  origin: string;
  /** Host header value (includes a port only on the origin form) */
  host: string;
  hostname: string;
  /** '' for an origin, '/<stage>' for an API Gateway stage URL */
  basePath: string;
  /** region parsed from an execute-api host; undefined on the origin form */
  region?: string;
};

/**
 * Accept exactly two gateway URL forms, or return null:
 *  - an https origin without path/query/credentials ("https://gateway.example.test"), or
 *  - the API Gateway stage URL that fronts the gateway's internal plane with
 *    IAM authorization ("https://<id>.execute-api.<region>.amazonaws.com/<stage>",
 *    one path segment, no trailing slash, no port).
 * When `region` is given the execute-api host must be in that region, so the
 * caller's SigV4 scope and the API's region cannot disagree.
 */
export function supervisorGatewayTarget(gatewayUrl: string, region?: string): SupervisorGatewayTarget | null {
  let url: URL;
  try { url = new URL(gatewayUrl); } catch { return null; }
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) return null;
  try { if (validateBaseUrl(gatewayUrl) !== url.origin) return null; } catch { return null; }
  if (url.pathname === '/') {
    return url.origin === gatewayUrl ? { origin: url.origin, host: url.host, hostname: url.hostname, basePath: '' } : null;
  }
  const host = EXECUTE_API_HOST.exec(url.hostname);
  if (!host || url.port || !STAGE_PATH.test(url.pathname) || `${url.origin}${url.pathname}` !== gatewayUrl ||
      (region !== undefined && host[1] !== region)) {
    return null;
  }
  return { origin: url.origin, host: url.host, hostname: url.hostname, basePath: url.pathname, region: host[1] };
}

export interface SandboxPodInput {
  runId: string;
  image: string;
  gatewayUrl: string;
  podName?: string;
  sessionId?: string;
  sessionMode?: 'ephemeral' | 'persistent';
  ownerKey?: string;
  runBindingId?: string;
  executor?: 'sandbox-loop' | 'claude-code' | 'kirocrew';
  dashboardOrigin?: string;
  crewScope?: 'user';
}

export function sandboxCreationName(runId: string): string {
  const runHash = createHash('sha256').update(runId).digest('hex');
  return `chat-turn-${runHash.slice(0, 12)}-${runHash.slice(12, 32)}`;
}

/**
 * Refuse a pod template whose labels the API server would reject
 * ("metadata.labels: Invalid value: ...: must be no more than 63 bytes"). Full digests belong in
 * annotations, which have no such limit.
 */
export function validateKubernetesLabels(labels: Record<string, string>): void {
  for (const [key, value] of Object.entries(labels)) {
    if (!LABEL_VALUE.test(value)) throw new Error(`Chat pod label ${key} is not a valid Kubernetes label value`);
  }
}

export function validateSandboxAssignment(input: SandboxPodInput): void {
  if (!RUN_ID.test(input.runId) || !IMAGE.test(input.image) || (input.podName !== undefined && input.podName !== sandboxCreationName(input.runId)) ||
      (input.sessionMode !== undefined && !['ephemeral', 'persistent'].includes(input.sessionMode)) ||
      (input.executor !== undefined && !['sandbox-loop', 'claude-code', 'kirocrew'].includes(input.executor)) ||
      ((input.executor === 'claude-code' || input.executor === 'kirocrew') && input.sessionMode !== 'persistent') ||
      (input.sessionMode === 'persistent' && (typeof input.sessionId !== 'string' || !RUN_ID.test(input.sessionId))) ||
      (input.crewScope !== undefined && (input.crewScope !== 'user' || input.executor !== 'kirocrew' || input.sessionMode !== 'persistent'))) {
    throw new Error('Trusted chat sandbox assignment requires a run and pinned image');
  }
  validateBaseUrl(input.gatewayUrl);
  if (!supervisorGatewayTarget(input.gatewayUrl)) {
    throw new Error('Chat sandbox gateway URL must be an HTTPS origin or API Gateway stage URL');
  }
}

export function buildSandboxPod(input: SandboxPodInput, binding: SandboxGatewayBinding) {
  validateSandboxAssignment(input);
  validateGatewayBinding(binding);
  const runHash = createHash('sha256').update(input.runId).digest('hex');
  const persistent = input.sessionMode === 'persistent';
  if (persistent && !/^[a-f0-9]{52}$/.test(input.ownerKey ?? '')) {
    throw new Error('Persistent Crew requires a trusted owner-bound volume key');
  }
  if (persistent && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(input.runBindingId ?? '')) {
    throw new Error('Persistent Crew requires a minted run binding');
  }
  if (input.dashboardOrigin !== undefined) {
    const origin = new URL(input.dashboardOrigin);
    if (input.executor !== 'kirocrew' || origin.protocol !== 'https:' || origin.origin !== input.dashboardOrigin ||
        origin.pathname !== '/' || origin.search || origin.hash) throw new Error('Invalid Crew dashboard origin');
  }
  // Labels carry only selector-sized class markers and the 52-hex owner key. The full sha256 run and
  // session digests (64 hex) exceed the 63-byte label limit, so they travel as annotations; the
  // admission policies and the gateway's workload verifier read them there.
  const labels: Record<string, string> = {
    'app.kubernetes.io/name': persistent ? 'chat-crew-pod' : 'chat-turn-sandbox',
    ...(persistent ? { 'adp.io/crew-pod': 'true' } : { 'adp.io/chat-sandbox': 'true' }),
    ...(persistent ? { 'adp.io/owner-key': input.ownerKey! } : {}),
  };
  validateKubernetesLabels(labels);
  const annotations: Record<string, string> = {
    'adp.io/run-hash': runHash,
    ...(persistent ? { 'adp.io/session-hash': createHash('sha256').update(input.sessionId!).digest('hex') } : {}),
  };
  return {
    apiVersion: 'v1',
    kind: 'Pod',
    metadata: {
      ...(input.podName === undefined ? {} : { name: input.podName }),
      namespace: 'adp-gateway-agents',
      generateName: `chat-turn-${runHash.slice(0, 12)}-`,
      labels,
      annotations,
    },
    spec: {
      serviceAccountName: persistent ? 'adp-crew-pod' : 'adp-chat-sandbox',
      automountServiceAccountToken: false,
      enableServiceLinks: false,
      shareProcessNamespace: false,
      hostNetwork: false,
      hostPID: false,
      hostIPC: false,
      hostAliases: [{ ip: binding.clusterIP, hostnames: [SANDBOX_GATEWAY_HOST] }],
      restartPolicy: 'Never',
      ...(input.sessionMode === 'persistent' ? {} : { activeDeadlineSeconds: 900 }),
      terminationGracePeriodSeconds: persistent ? 120 : 10,
      securityContext: {
        runAsNonRoot: true,
        runAsUser: 10001,
        fsGroup: 10001,
        seccompProfile: { type: 'RuntimeDefault' },
      },
      volumes: [
        {
          name: 'sandbox-identity',
          projected: {
            defaultMode: 292,
            sources: [{ serviceAccountToken: { audience: 'adp-agent-bootstrap', expirationSeconds: 600, path: 'token' } }],
          },
        },
        { name: 'scratch', emptyDir: { sizeLimit: persistent ? '2Gi' : '512Mi' } },
        ...(persistent && input.executor !== 'claude-code' ? [{ name: 'crew-home', persistentVolumeClaim: { claimName: `crew-home-${input.ownerKey}` } }] : []),
        { name: 'gateway-ca', configMap: { name: binding.caConfigMap, defaultMode: 292,
          items: [{ key: 'ca.crt', path: 'ca.crt' }] } },
      ],
      containers: [{
        name: 'chat-agent',
        image: input.image,
        imagePullPolicy: 'IfNotPresent',
        command: ['/app/chat-sandbox-entrypoint'],
        ...(input.executor === 'kirocrew' ? { lifecycle: { preStop: { exec: { command: ['/bin/sh', '-c',
          'kill -TERM 1; while kill -0 1 2>/dev/null; do sleep 1; done'] } } } } : {}),
        workingDir: '/tmp',
        ...(input.executor === 'kirocrew' ? { ports: [{ containerPort: 5476, protocol: 'TCP' }, { containerPort: 5477, protocol: 'TCP' }] } : {}),
        env: [
          { name: 'ADP_CHAT_MODEL_POLICY_ENABLED', value: 'true' },
          { name: 'ADP_CHAT_DATA_ENABLED', value: 'true' },
          { name: 'ADP_CHAT_DATA_URL', value: SANDBOX_GATEWAY_URL },
          { name: 'NODE_EXTRA_CA_CERTS', value: SANDBOX_CA_FILE },
          { name: 'CONTEXT_STRATEGY', value: 'gateway' },
          { name: 'MEMORY_STRATEGY', value: 'gateway' },
          { name: 'ARTIFACT_STRATEGY', value: 'gateway' },
          { name: 'ADP_WORKLOAD_TOKEN_FILE', value: '/var/run/adp-model/token' },
          { name: 'CLAUDE_CONFIG_DIR', value: '/tmp/workspace/.claude' },
          { name: 'HOME', value: '/tmp/workspace' },
          ...(persistent ? [{ name: 'ADP_EXECUTOR', value: input.executor ?? 'sandbox-loop' },
            { name: 'KIROCREW_HOME', value: '/tmp/workspace' },
            { name: 'ADP_RUN_ID', value: input.runBindingId! }] : []),
          ...(input.executor === 'kirocrew' ? [{ name: 'ADP_CREW_POD_IP', valueFrom: { fieldRef: { fieldPath: 'status.podIP' } } },
            { name: 'ADP_CREW_CUSTODY_PORT', value: '5477' },
            { name: 'ADP_CREW_SESSION_MAP', value: '/tmp/workspace/adp-crew-session-map' },
            ...(input.dashboardOrigin ? [{ name: 'ADP_CREW_DASHBOARD_ORIGIN', value: input.dashboardOrigin }] : []),
            ...(input.crewScope === 'user' ? [{ name: 'ADP_CREW_SCOPE', value: 'user' }] : [])] : []),
        ],
        volumeMounts: [
          { name: 'sandbox-identity', mountPath: '/var/run/adp-model', readOnly: true },
          { name: 'scratch', mountPath: '/tmp' },
          ...(persistent && input.executor !== 'claude-code' ? [{ name: 'crew-home', mountPath: '/tmp/workspace' }] : []),
          { name: 'gateway-ca', mountPath: '/var/run/adp-chat-ca', readOnly: true },
        ],
        securityContext: {
          runAsNonRoot: true,
          runAsUser: 10001,
          readOnlyRootFilesystem: true,
          allowPrivilegeEscalation: false,
          capabilities: { drop: ['ALL'] },
          seccompProfile: { type: 'RuntimeDefault' },
        },
        resources: {
          requests: { cpu: persistent ? '500m' : '200m', memory: persistent ? '1Gi' : '512Mi', 'ephemeral-storage': '256Mi' },
          limits: { cpu: '2', memory: persistent ? '6Gi' : '4Gi', 'ephemeral-storage': persistent ? '4Gi' : '2Gi' },
        },
      }],
    },
  };
}
