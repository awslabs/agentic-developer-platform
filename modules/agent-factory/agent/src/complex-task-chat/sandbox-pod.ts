import { createHash } from 'node:crypto';
import { validateBaseUrl } from '../lib/url-guard';
import { SANDBOX_CA_FILE, SANDBOX_GATEWAY_HOST, SANDBOX_GATEWAY_URL, validateGatewayBinding, type SandboxGatewayBinding } from './sandbox-gateway-binding';

const IMAGE = /^[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}$/;
const RUN_ID = /^[A-Za-z0-9_.:-]{1,128}$/;

export interface SandboxPodInput {
  runId: string;
  image: string;
  gatewayUrl: string;
  podName?: string;
  sessionId?: string;
  sessionMode?: 'ephemeral' | 'persistent';
}

export function sandboxCreationName(runId: string): string {
  const runHash = createHash('sha256').update(runId).digest('hex');
  return `chat-turn-${runHash.slice(0, 12)}-${runHash.slice(12, 32)}`;
}

export function validateSandboxAssignment(input: SandboxPodInput): void {
  if (!RUN_ID.test(input.runId) || !IMAGE.test(input.image) || (input.podName !== undefined && input.podName !== sandboxCreationName(input.runId)) ||
      (input.sessionMode !== undefined && !['ephemeral', 'persistent'].includes(input.sessionMode)) ||
      (input.sessionMode === 'persistent' && (typeof input.sessionId !== 'string' || !RUN_ID.test(input.sessionId)))) {
    throw new Error('Trusted chat sandbox assignment requires a run and pinned image');
  }
  const gateway = new URL(input.gatewayUrl);
  if (gateway.origin !== input.gatewayUrl || gateway.pathname !== '/' || gateway.search || gateway.hash) {
    throw new Error('Chat sandbox gateway URL must be an HTTPS origin');
  }
  validateBaseUrl(input.gatewayUrl);
}

export function buildSandboxPod(input: SandboxPodInput, binding: SandboxGatewayBinding) {
  validateSandboxAssignment(input);
  validateGatewayBinding(binding);
  const runHash = createHash('sha256').update(input.runId).digest('hex');
  return {
    apiVersion: 'v1',
    kind: 'Pod',
    metadata: {
      ...(input.podName === undefined ? {} : { name: input.podName }),
      namespace: 'adp-gateway-agents',
      generateName: `chat-turn-${runHash.slice(0, 12)}-`,
      labels: {
        'app.kubernetes.io/name': 'chat-turn-sandbox',
        'adp.io/chat-sandbox': 'true',
        'adp.io/run-hash': runHash,
        ...(input.sessionMode === 'persistent' ? { 'adp.io/session-hash': createHash('sha256').update(input.sessionId!).digest('hex') } : {}),
      },
    },
    spec: {
      serviceAccountName: 'adp-chat-sandbox',
      automountServiceAccountToken: false,
      enableServiceLinks: false,
      shareProcessNamespace: false,
      hostNetwork: false,
      hostPID: false,
      hostIPC: false,
      hostAliases: [{ ip: binding.clusterIP, hostnames: [SANDBOX_GATEWAY_HOST] }],
      restartPolicy: 'Never',
      ...(input.sessionMode === 'persistent' ? {} : { activeDeadlineSeconds: 900 }),
      terminationGracePeriodSeconds: 10,
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
        { name: 'scratch', emptyDir: { sizeLimit: '512Mi' } },
        { name: 'gateway-ca', configMap: { name: binding.caConfigMap, defaultMode: 292,
          items: [{ key: 'ca.crt', path: 'ca.crt' }] } },
      ],
      containers: [{
        name: 'chat-agent',
        image: input.image,
        imagePullPolicy: 'IfNotPresent',
        command: ['/app/chat-sandbox-entrypoint'],
        workingDir: '/tmp',
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
        ],
        volumeMounts: [
          { name: 'sandbox-identity', mountPath: '/var/run/adp-model', readOnly: true },
          { name: 'scratch', mountPath: '/tmp' },
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
          requests: { cpu: '200m', memory: '512Mi', 'ephemeral-storage': '256Mi' },
          limits: { cpu: '2', memory: '4Gi', 'ephemeral-storage': '2Gi' },
        },
      }],
    },
  };
}
