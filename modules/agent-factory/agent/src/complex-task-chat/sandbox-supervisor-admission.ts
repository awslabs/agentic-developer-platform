import { createHash } from 'node:crypto';
import { fromTokenFile } from '@aws-sdk/credential-provider-web-identity';
import { Hash } from '@smithy/hash-node';
import { SignatureV4 } from '@smithy/signature-v4';
import { canonicalJson } from '../invocability-probe/canonical-json';
import { validateBaseUrl } from '../lib/url-guard';
import { withSandboxPod, type SandboxPodApi, type SandboxPodIdentity } from './sandbox-launcher';
import { validateSandboxAssignment, type SandboxPodInput } from './sandbox-pod';

type Credentials = { accessKeyId: string; secretAccessKey: string; sessionToken?: string };
export type Admission = { run_id: string; session_id: string; lease_generation: number };
export type Assignment = SandboxPodInput & { sessionId: string; envelopeDigest: string };
export type SupervisorIdentity = { roleArn: string; tokenFile: string; region: string; workerRoleArn?: string };
export type RegisteredAssignment = Assignment & { taskId: string; sessionGeneration: number };
export type SupervisorTiming = { now?: () => number; sleep?: (ms: number) => Promise<void> };
export type SandboxTerminalReceipt = {
  run_id: string;
  session_id: string;
  lease_generation: number;
  attempt: number;
  sandbox_uid: string;
  outcome: 'completed' | 'failed' | 'cancelled' | 'interrupted';
  message_id: string | null;
  retryable: boolean;
  automatic_replay_permitted: false;
  accounting_status: 'not_used' | 'settled' | 'unresolved';
  terminal: true;
  finalized_at: number;
};

const STS_BODY = 'Action=GetCallerIdentity&Version=2011-06-15';
const ADMIT_PATH = '/internal/v1/agent/chat/data/admit';
const EXIT_PATH = '/internal/v1/agent/chat/data/exit';
const TEARDOWN_PATH = '/internal/v1/agent/chat/data/teardown';
const FINALIZE_PATH = '/internal/v1/agent/chat/data/finalize';
export const COMPLETE_PATH = '/internal/v1/agent/chat/data/complete';
export const RESUME_PATH = '/internal/v1/agent/chat/data/resume';
export const RESERVE_PATH = '/internal/v1/agent/chat/data/reserve';
const ROLE = /^arn:aws(?:-us-gov|-cn)?:iam::[0-9]{12}:role\/[A-Za-z0-9/+=,.@_-]+$/;
const DIGEST = /^[0-9a-f]{64}$/;
const RUN = /^[A-Za-z0-9_.:-]{1,128}$/;

export function registeredSandboxAssignment(rawEnvelope: string, image: string, gatewayUrl: string): RegisteredAssignment {
  if (typeof rawEnvelope !== 'string' || !rawEnvelope || Buffer.byteLength(rawEnvelope) > 65_536) {
    throw new Error('Chat supervisor registered envelope unavailable');
  }
  let envelope: unknown;
  try { envelope = JSON.parse(rawEnvelope); }
  catch { throw new Error('Chat supervisor registered envelope unavailable'); }
  if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) {
    throw new Error('Chat supervisor registered envelope unavailable');
  }
  const fields = envelope as Record<string, unknown>;
  if (typeof fields.message_id !== 'string' || !RUN.test(fields.message_id) ||
      typeof fields.session_id !== 'string' || !RUN.test(fields.session_id) ||
      typeof fields.task_id !== 'string' || !RUN.test(fields.task_id) ||
      typeof fields.session_generation !== 'number' || !Number.isSafeInteger(fields.session_generation) || fields.session_generation < 1) {
    throw new Error('Chat supervisor registered envelope unavailable');
  }
  const assignment = { runId: fields.message_id, sessionId: fields.session_id, taskId: fields.task_id, sessionGeneration: fields.session_generation,
    envelopeDigest: createHash('sha256').update(rawEnvelope).digest('hex'), image, gatewayUrl };
  validateSandboxAssignment(assignment);
  return assignment;
}

export class SupervisorResponseError extends Error {
  constructor(readonly status: number) { super('Chat sandbox admission refused'); }
}

export async function supervisorRequest(
  assignment: Assignment,
  pod: SandboxPodIdentity | null,
  identity: SupervisorIdentity,
  path: typeof ADMIT_PATH | typeof EXIT_PATH | typeof TEARDOWN_PATH | typeof FINALIZE_PATH | typeof COMPLETE_PATH | typeof RESUME_PATH | typeof RESERVE_PATH,
  send: typeof fetch,
  load: typeof fromTokenFile,
): Promise<unknown> {
  const url = new URL(assignment.gatewayUrl);
  if (validateBaseUrl(assignment.gatewayUrl) !== assignment.gatewayUrl || url.pathname !== '/' || url.search || url.hash ||
      !RUN.test(assignment.runId) || !RUN.test(assignment.sessionId) || !DIGEST.test(assignment.envelopeDigest) ||
      (pod === null ? path !== RESUME_PATH && path !== RESERVE_PATH :
        !/^chat-turn-[0-9a-f]{12}-[a-z0-9]{1,20}$/.test(pod.name) ||
        !pod.name.startsWith(`chat-turn-${createHash('sha256').update(assignment.runId).digest('hex').slice(0, 12)}-`) || !/^[a-z0-9-]{8,128}$/.test(pod.uid)) ||
      !ROLE.test(identity.roleArn) || identity.roleArn === identity.workerRoleArn ||
      !identity.tokenFile.startsWith('/var/run/secrets/') || !/^[/][A-Za-z0-9_./-]+$/.test(identity.tokenFile) || identity.tokenFile.split('/').includes('..') || !/^[a-z0-9-]+$/.test(identity.region)) {
    throw new Error('Chat supervisor assignment or dedicated identity unavailable');
  }
  const body = {
    run_id: assignment.runId,
    envelope_digest: assignment.envelopeDigest,
    ...(path === RESERVE_PATH ? { image_digest: assignment.image.split('@')[1] } : {}),
    ...(pod === null ? {} : { pod_name: pod.name, pod_uid: pod.uid }),
  };
  const canonical = canonicalJson(body).replace(/[\u007f-\uffff]/g, char => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`);
  const digest = createHash('sha256').update(canonical).digest('hex');
  const credentials: Credentials = await load({ roleArn: identity.roleArn, webIdentityTokenFile: identity.tokenFile,
    clientConfig: { region: identity.region } })();
  if (!credentials.sessionToken) throw new Error('Chat supervisor temporary identity unavailable');
  const hostname = `sts.${identity.region}.amazonaws.com`;
  const proof = await new SignatureV4({ credentials, region: identity.region, service: 'sts',
    applyChecksum: false, sha256: Hash.bind(null, 'sha256') }).sign({
    method: 'POST', protocol: 'https:', hostname, path: '/', body: STS_BODY,
    headers: { host: hostname, 'content-type': 'application/x-www-form-urlencoded', 'x-adp-work-invocation': digest },
  });
  if (proof.headers.authorization.includes('x-amz-content-sha256;')) throw new Error('Unsupported STS proof');
  const headers: Record<string, string> = {};
  for (const [key, value] of Object.entries(proof.headers)) {
    if (key.toLowerCase() !== 'host' && key.toLowerCase() !== 'x-amz-content-sha256') headers[key.toLowerCase()] = value;
  }
  const response = await send(`${assignment.gatewayUrl}${path}`, {
    method: 'POST', redirect: 'error', signal: AbortSignal.timeout(10_000),
    headers: { 'Content-Type': 'application/json', 'X-Adp-Producer-Proof': Buffer.from(JSON.stringify(headers)).toString('base64') },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    await response.body?.cancel();
    throw new SupervisorResponseError(response.status);
  }
  if (!response.body) throw new Error('Chat sandbox admission refused');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 4096) throw new Error('Chat sandbox admission response too large');
      chunks.push(value);
    }
  } finally { await reader.cancel(); }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new Error('Chat sandbox admission response invalid'); }
}

export async function admitSandboxPod(
  assignment: Assignment,
  pod: SandboxPodIdentity,
  identity: SupervisorIdentity,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
): Promise<Admission> {
  const receipt = await supervisorRequest(assignment, pod, identity, ADMIT_PATH, send, load) as Partial<Admission>;
  if (!receipt || receipt.run_id !== assignment.runId || typeof receipt.session_id !== 'string' ||
      receipt.session_id !== assignment.sessionId || !Number.isSafeInteger(receipt.lease_generation) || receipt.lease_generation! < 1) {
    throw new Error('Chat sandbox admission response invalid');
  }
  return receipt as Admission;
}

export async function sandboxPodExited(
  assignment: Assignment,
  pod: SandboxPodIdentity,
  identity: SupervisorIdentity,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
): Promise<boolean> {
  const receipt = await supervisorRequest(assignment, pod, identity, EXIT_PATH, send, load) as
    Partial<{ run_id: string; pod_uid: string; terminated: boolean }>;
  if (!receipt || receipt.run_id !== assignment.runId || receipt.pod_uid !== pod.uid ||
      typeof receipt.terminated !== 'boolean') {
    throw new Error('Chat sandbox exit response invalid');
  }
  return receipt.terminated;
}

export async function waitForSandboxExit(
  assignment: Assignment,
  pod: SandboxPodIdentity,
  identity: SupervisorIdentity,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
  timing: SupervisorTiming = {},
): Promise<void> {
  const now = timing.now ?? (() => performance.now());
  const sleep = timing.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  const deadline = now() + 780_000;
  for (;;) {
    if (now() >= deadline) throw new Error('Chat sandbox exit unverified before supervisor deadline');
    if (await sandboxPodExited(assignment, pod, identity, send, load)) return;
    await sleep(Math.max(0, Math.min(1000, deadline - now())));
  }
}

export async function waitForSandboxRemoval(
  assignment: Assignment,
  pod: SandboxPodIdentity,
  identity: SupervisorIdentity,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
  timing: SupervisorTiming = {},
): Promise<void> {
  const now = timing.now ?? (() => performance.now());
  const sleep = timing.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  const deadline = now() + 60_000;
  for (;;) {
    if (now() >= deadline) throw new Error('Chat sandbox removal unverified before supervisor deadline');
    const receipt = await supervisorRequest(assignment, pod, identity, TEARDOWN_PATH, send, load) as
      Partial<{ run_id: string; pod_uid: string; removed: boolean }>;
    if (!receipt || receipt.run_id !== assignment.runId || receipt.pod_uid !== pod.uid || typeof receipt.removed !== 'boolean') {
      throw new Error('Chat sandbox removal response invalid');
    }
    if (receipt.removed) return;
    await sleep(Math.max(0, Math.min(1000, deadline - now())));
  }
}

export async function finalizeSandboxTurn(
  assignment: Assignment,
  pod: SandboxPodIdentity,
  admission: Admission,
  identity: SupervisorIdentity,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
): Promise<SandboxTerminalReceipt> {
  if (admission.run_id !== assignment.runId || admission.session_id !== assignment.sessionId ||
      !Number.isSafeInteger(admission.lease_generation) || admission.lease_generation < 1) {
    throw new Error('Chat sandbox finalization requires matching admission');
  }
  const receipt = await supervisorRequest(assignment, pod, identity, FINALIZE_PATH, send, load) as Partial<SandboxTerminalReceipt>;
  if (!receipt || receipt.run_id !== assignment.runId || receipt.session_id !== assignment.sessionId ||
      receipt.lease_generation !== admission.lease_generation || receipt.sandbox_uid !== pod.uid ||
      !Number.isSafeInteger(receipt.attempt) || receipt.attempt! < 1 ||
      !Number.isSafeInteger(receipt.finalized_at) || receipt.finalized_at! < 1 ||
      !['completed', 'failed', 'cancelled', 'interrupted'].includes(receipt.outcome ?? '') ||
      !['not_used', 'settled', 'unresolved'].includes(receipt.accounting_status ?? '') ||
      typeof receipt.retryable !== 'boolean' || receipt.automatic_replay_permitted !== false || receipt.terminal !== true ||
      (receipt.retryable && (receipt.outcome !== 'interrupted' || receipt.accounting_status !== 'not_used')) ||
      (receipt.outcome === 'completed' ? typeof receipt.message_id !== 'string' || !RUN.test(receipt.message_id) : receipt.message_id !== null)) {
    throw new Error('Chat sandbox terminal response invalid');
  }
  return receipt as SandboxTerminalReceipt;
}

export async function reconcileAdmittedSandbox(
  assignment: Assignment,
  api: SandboxPodApi,
  identity: SupervisorIdentity,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
  timing: SupervisorTiming = {},
  reconciliation: {
    resume?: { pod: SandboxPodIdentity; admission: Admission };
    onTerminal?: (pod: SandboxPodIdentity, receipt: SandboxTerminalReceipt) => Promise<void>;
  } = {},
): Promise<SandboxTerminalReceipt> {
  let exited: { pod: SandboxPodIdentity; admission: Admission } | undefined;
  try {
    const observe = async (pod: SandboxPodIdentity, admission: Admission) => {
      await waitForSandboxExit(assignment, pod, identity, send, load, timing);
      exited = { pod, admission };
    };
    if (reconciliation.resume) {
      await observe(reconciliation.resume.pod, reconciliation.resume.admission);
      await api.remove(reconciliation.resume.pod.name, reconciliation.resume.pod.uid);
    } else {
      await withAdmittedSandboxPod(assignment, api, identity, observe, send, load, false);
    }
  } catch (error) {
    if (!exited) throw error;
  }
  if (!exited) throw new Error('Chat sandbox exit evidence unavailable');
  await waitForSandboxRemoval(assignment, exited.pod, identity, send, load, timing);
  const terminal = await finalizeSandboxTurn(assignment, exited.pod, exited.admission, identity, send, load);
  await reconciliation.onTerminal?.(exited.pod, terminal);
  return terminal;
}

export async function withAdmittedSandboxPod<Result>(
  assignment: Assignment,
  api: SandboxPodApi,
  identity: SupervisorIdentity,
  run: (pod: SandboxPodIdentity, admission: Admission) => Promise<Result>,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
  cleanupOnFailure = true,
): Promise<Result> {
  let completed = false;
  return withSandboxPod(assignment, api, async pod => {
    const result = await run(pod, await admitSandboxPod(assignment, pod, identity, send, load));
    completed = true;
    return result;
  }, () => cleanupOnFailure || completed);
}
