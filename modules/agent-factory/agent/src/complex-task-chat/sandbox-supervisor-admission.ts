import { createHash } from 'node:crypto';
import { fromTokenFile } from '@aws-sdk/credential-provider-web-identity';
import { Hash } from '@smithy/hash-node';
import { SignatureV4 } from '@smithy/signature-v4';
import { canonicalJson } from '../invocability-probe/canonical-json';
import { sandboxPodEnded, withSandboxPod, type SandboxPodApi, type SandboxPodIdentity } from './sandbox-launcher';
import { supervisorLog } from './supervisor-log';
import { sandboxCreationName, supervisorGatewayTarget, validateSandboxAssignment, type SandboxPodInput } from './sandbox-pod';

type Credentials = { accessKeyId: string; secretAccessKey: string; sessionToken?: string };
export type Admission = { run_id: string; session_id: string; lease_generation: number; session_mode?: 'ephemeral' | 'persistent' };
export type Assignment = SandboxPodInput & { sessionId: string; envelopeDigest: string; sessionRunId?: string; verifiedCrewOwner?: SandboxPodIdentity };
/** Turn executors a persistent session may run (sandbox-pod.ts ADP_EXECUTOR); ephemeral is always sandbox-loop. */
export const CHAT_EXECUTORS = ['sandbox-loop', 'claude-code', 'kirocrew'] as const;
export type ChatExecutor = typeof CHAT_EXECUTORS[number];
const NOTIFICATION_KEYS = 'envelope_digest,message_id,notification_version,session_generation,session_id,session_mode,task_id';
const NOTIFICATION_KEYS_WITH_EXECUTOR = 'envelope_digest,executor,message_id,notification_version,session_generation,session_id,session_mode,task_id';

/**
 * Which executor a persistent pod runs (#8115): the session's own choice, carried on the
 * queue message by the gateway (`executor`), outranks the deployment default
 * `ADP_CHAT_PERSISTENT_EXECUTOR` (rendered from SSM by deploy-chat-scaledjob.sh), which
 * outranks the built-in `sandbox-loop`. An unsupported deployment value fails the dispatch
 * rather than silently falling back. Ephemeral assignments never reach the pod template
 * with an executor; callers apply the result to persistent assignments only.
 */
export function persistentExecutor(assignment: Pick<Assignment, 'executor'>, env: NodeJS.ProcessEnv = process.env): ChatExecutor {
  const deploymentDefault = env.ADP_CHAT_PERSISTENT_EXECUTOR;
  if (deploymentDefault !== undefined && !(CHAT_EXECUTORS as readonly string[]).includes(deploymentDefault)) {
    throw new Error('Unsupported persistent chat executor selection');
  }
  if (assignment.executor !== undefined && !(CHAT_EXECUTORS as readonly string[]).includes(assignment.executor)) {
    throw new Error('Unsupported persistent chat executor selection');
  }
  return assignment.executor ?? (deploymentDefault as ChatExecutor | undefined) ?? 'sandbox-loop';
}
export type SupervisorIdentity = { roleArn: string; tokenFile: string; region: string; workerRoleArn?: string };
export type RegisteredAssignment = Assignment & { taskId: string; sessionGeneration: number };
export type SupervisorTiming = {
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
  /**
   * The instant (in `now()` units) after which the queue delivery becomes visible again. Completion
   * retries stop before it: a supervisor that keeps retrying past this point would race its own
   * redelivery, while one that gives up earlier leaves the message in flight for nothing (#8047).
   */
  visibilityDeadline?: number;
};
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
const CREW_RUN_PATH = '/internal/v1/agent/chat/crew-run';
const CREW_LEASE_PATH = '/internal/v1/agent/chat/crew-run/lease';
const CREW_ACTIVATE_PATH = '/internal/v1/agent/chat/crew-run/activate';
const CREW_READY_PATH = '/internal/v1/agent/chat/crew-run/ready';
const EXIT_PATH = '/internal/v1/agent/chat/data/exit';
const TEARDOWN_PATH = '/internal/v1/agent/chat/data/teardown';
const FINALIZE_PATH = '/internal/v1/agent/chat/data/finalize';
export const COMPLETE_PATH = '/internal/v1/agent/chat/data/complete';
export const RESUME_PATH = '/internal/v1/agent/chat/data/resume';
export const RESERVE_PATH = '/internal/v1/agent/chat/data/reserve';
export const SESSION_COMMIT_PATH = '/internal/v1/agent/chat/data/session/commit';
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
  if (fields.notification_version !== undefined && (fields.notification_version !== 1 ||
      fields.session_mode !== 'persistent' || typeof fields.envelope_digest !== 'string' || !DIGEST.test(fields.envelope_digest) ||
      ![NOTIFICATION_KEYS, NOTIFICATION_KEYS_WITH_EXECUTOR].includes(Object.keys(fields).sort().join(',')))) {
    throw new Error('Chat supervisor notification unavailable');
  }
  if (typeof fields.message_id !== 'string' || !RUN.test(fields.message_id) ||
      typeof fields.session_id !== 'string' || !RUN.test(fields.session_id) ||
      typeof fields.task_id !== 'string' || !RUN.test(fields.task_id) ||
      (fields.session_mode !== undefined && fields.session_mode !== 'ephemeral' && fields.session_mode !== 'persistent') ||
      typeof fields.session_generation !== 'number' || !Number.isSafeInteger(fields.session_generation) || fields.session_generation < 1) {
    throw new Error('Chat supervisor registered envelope unavailable');
  }
  // The gateway puts the session's executor choice on persistent envelopes and wake-ups only
  // (#8115); anything else naming one is not a registered chat turn.
  if (fields.executor !== undefined && (fields.session_mode !== 'persistent' ||
      typeof fields.executor !== 'string' || !(CHAT_EXECUTORS as readonly string[]).includes(fields.executor))) {
    throw new Error('Chat supervisor registered envelope unavailable');
  }
  const assignment = { runId: fields.message_id, sessionId: fields.session_id, taskId: fields.task_id, sessionGeneration: fields.session_generation,
    envelopeDigest: fields.notification_version === 1 ? fields.envelope_digest as string : createHash('sha256').update(rawEnvelope).digest('hex'), image, gatewayUrl,
    ...(fields.session_mode !== undefined ? { sessionMode: fields.session_mode as 'ephemeral' | 'persistent' } : {}),
    ...(fields.executor !== undefined ? { executor: fields.executor as ChatExecutor } : {}) };
  validateSandboxAssignment(assignment);
  return assignment;
}

/** The gateway's 409 body when finalize/complete cannot yet confirm the sandbox pod is gone. */
export const REMOVAL_UNCONFIRMED_CODE = 'chat_sandbox_removal_unconfirmed';
const REMOVAL_UNCONFIRMED_MESSAGE = 'chat sandbox removal not confirmed';
const ERROR_DETAIL_LIMIT = 1024;

/**
 * A sandbox pod was created but removed again before any admission request: it never reached
 * Running (image pull or scheduling past the start deadline, an ended container) or came back
 * with a changed spec. The pod identity travels with the error so the supervisor can name it
 * in the log and hand its uid to the gateway's pre-admission cleanup (#8061).
 */
export class SandboxStartError extends Error {
  readonly pod: SandboxPodIdentity;
  constructor(cause: unknown, pod: SandboxPodIdentity) {
    super(cause instanceof Error ? cause.message : 'Chat sandbox did not start');
    this.name = 'SandboxStartError';
    this.pod = pod;
  }
}

export class SupervisorResponseError extends Error {
  /** The first kilobyte of the gateway's error body, if it could be read. */
  readonly detail: string;
  constructor(readonly status: number, detail = '') { super('Chat sandbox admission refused'); this.detail = detail.slice(0, ERROR_DETAIL_LIMIT); }
}

/**
 * True for the one 409 the supervisor can do something about: the gateway's own API-server read
 * still returns the sandbox pod, so completion must wait for its removal rather than give up.
 */
export function isRemovalUnconfirmed(error: unknown): error is SupervisorResponseError {
  if (!(error instanceof SupervisorResponseError) || error.status !== 409) return false;
  if (error.detail.includes(REMOVAL_UNCONFIRMED_MESSAGE)) return true;
  try {
    const body = JSON.parse(error.detail) as { error?: unknown; detail?: { error?: unknown } | string };
    return body?.error === REMOVAL_UNCONFIRMED_CODE ||
      (typeof body?.detail === 'object' && body.detail !== null && body.detail.error === REMOVAL_UNCONFIRMED_CODE);
  } catch { return false; }
}

/**
 * One structured line per supervisor step (#7990): `[supervisor] {"run_id":…,"step":…,"status":…}`.
 * Values are limited to identifiers, step names and numbers; no envelope or receipt content.
 */
export { supervisorLog };

async function readErrorDetail(response: Response): Promise<string> {
  if (!response.body) return '';
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (size < ERROR_DETAIL_LIMIT) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      chunks.push(value);
    }
  } catch { return ''; } finally { await reader.cancel().catch(() => undefined); }
  return Buffer.concat(chunks).toString('utf8').slice(0, ERROR_DETAIL_LIMIT);
}

export async function supervisorRequest(
  assignment: Assignment,
  pod: SandboxPodIdentity | null,
  identity: SupervisorIdentity,
  path: typeof ADMIT_PATH | typeof EXIT_PATH | typeof TEARDOWN_PATH | typeof FINALIZE_PATH | typeof COMPLETE_PATH | typeof RESUME_PATH | typeof RESERVE_PATH | typeof SESSION_COMMIT_PATH | typeof CREW_RUN_PATH | typeof CREW_LEASE_PATH | typeof CREW_ACTIVATE_PATH | typeof CREW_READY_PATH,
  send: typeof fetch,
  load: typeof fromTokenFile,
  crewRun?: { run_id: string; lease_generation: number },
): Promise<unknown> {
  // The gateway is reached either at an https origin or through the API Gateway
  // stage URL (https://<id>.execute-api.<region>.amazonaws.com/<stage>), whose
  // /internal/{proxy+} route forwards /<stage>/internal/... to the gateway's
  // internal-plane ALB with the path preserved and AWS_IAM authorization.
  const target = supervisorGatewayTarget(assignment.gatewayUrl, identity.region);
  if (!target || !RUN.test(assignment.runId) || !RUN.test(assignment.sessionId) || !DIGEST.test(assignment.envelopeDigest) ||
      (pod === null ? path !== RESUME_PATH && path !== RESERVE_PATH && path !== CREW_RUN_PATH :
        !/^chat-turn-[0-9a-f]{12}-[a-z0-9]{1,20}$/.test(pod.name) ||
        (assignment.verifiedCrewOwner !== undefined &&
          (assignment.verifiedCrewOwner.name !== pod.name || assignment.verifiedCrewOwner.uid !== pod.uid)) ||
        (!pod.name.startsWith(`chat-turn-${createHash('sha256').update(assignment.sessionRunId ?? assignment.runId).digest('hex').slice(0, 12)}-`) &&
          !([ADMIT_PATH, SESSION_COMMIT_PATH, EXIT_PATH, TEARDOWN_PATH, FINALIZE_PATH, COMPLETE_PATH].includes(path) &&
            assignment.sessionMode === 'persistent' && assignment.executor === 'kirocrew' && process.env.ADP_EXECUTOR_SCOPE === 'user' &&
            assignment.verifiedCrewOwner?.name === pod.name && assignment.verifiedCrewOwner.uid === pod.uid)) ||
        !/^[a-z0-9-]{8,128}$/.test(pod.uid)) ||
      ((path === CREW_LEASE_PATH || path === CREW_ACTIVATE_PATH || path === CREW_READY_PATH) && (!pod || assignment.executor !== 'kirocrew' || assignment.sessionMode !== 'persistent' ||
        process.env.ADP_EXECUTOR_SCOPE !== 'user' || !crewRun || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(crewRun.run_id) ||
        !Number.isSafeInteger(crewRun.lease_generation) || crewRun.lease_generation < 1)) ||
      !ROLE.test(identity.roleArn) || identity.roleArn === identity.workerRoleArn ||
      !identity.tokenFile.startsWith('/var/run/secrets/') || !/^[/][A-Za-z0-9_./-]+$/.test(identity.tokenFile) || identity.tokenFile.split('/').includes('..') || !/^[a-z0-9-]+$/.test(identity.region)) {
    throw new Error('Chat supervisor assignment or dedicated identity unavailable');
  }
  const body = path === CREW_RUN_PATH ? { run_id: assignment.runId, envelope_digest: assignment.envelopeDigest,
    session_id: assignment.sessionId, target_kind: 'eks-pod',
    executor_scope: assignment.executor === 'kirocrew' && process.env.ADP_EXECUTOR_SCOPE === 'user' ? 'user' : 'session',
    target_ref: assignment.podName ?? sandboxCreationName(assignment.runId) } : {
    run_id: assignment.runId,
    envelope_digest: assignment.envelopeDigest,
    ...(path === CREW_LEASE_PATH || path === CREW_ACTIVATE_PATH || path === CREW_READY_PATH ? {
      crew_run_id: crewRun!.run_id, lease_generation: crewRun!.lease_generation,
    } : {}),
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
  const proofHeaders: Record<string, string> = {};
  for (const [key, value] of Object.entries(proof.headers)) {
    if (key.toLowerCase() !== 'host' && key.toLowerCase() !== 'x-amz-content-sha256') proofHeaders[key.toLowerCase()] = value;
  }
  // Every gateway request is additionally signed for execute-api with the same
  // temporary identity: the API Gateway methods in front of /internal/* are
  // AWS_IAM-authorized, and the gateway itself keeps verifying the producer
  // proof (body digest bound) rather than this transport signature.
  const payload = JSON.stringify(body);
  const requestPath = `${target.basePath}${path}`;
  const signed = await new SignatureV4({ credentials, region: identity.region, service: 'execute-api',
    sha256: Hash.bind(null, 'sha256') }).sign({
    method: 'POST', protocol: 'https:', hostname: target.hostname, path: requestPath, body: payload,
    headers: { host: target.host, 'Content-Type': 'application/json',
      'X-Adp-Producer-Proof': Buffer.from(JSON.stringify(proofHeaders)).toString('base64') },
  });
  const { host: _signedHost, ...requestHeaders } = signed.headers;
  const response = await send(`${target.origin}${requestPath}`, {
    method: 'POST', redirect: 'error', signal: AbortSignal.timeout(10_000),
    headers: requestHeaders,
    body: payload,
  });
  if (!response.ok) {
    // The body is read only far enough to recognise the gateway's error code; it is never
    // parsed as a receipt.
    throw new SupervisorResponseError(response.status, await readErrorDetail(response));
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

export async function mintCrewRun(
  assignment: Assignment,
  identity: SupervisorIdentity,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
): Promise<{ run_id: string; lease_generation: number; owner_key?: string; state?: 'provisioning'; activated?: boolean; pod_name?: string; pod_uid?: string }> {
  const userScope = assignment.executor === 'kirocrew' && process.env.ADP_EXECUTOR_SCOPE === 'user';
  if (assignment.sessionMode !== 'persistent' || (!userScope && !/^[a-f0-9]{52}$/.test(assignment.ownerKey ?? ''))) {
    throw new Error('Persistent Crew requires a trusted owner-bound volume key');
  }
  const receipt = await supervisorRequest(assignment, null, identity, CREW_RUN_PATH, send, load) as Record<string, unknown> | null;
  if (!receipt || typeof receipt.run_id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(receipt.run_id) ||
      !Number.isSafeInteger(receipt.lease_generation) || (receipt.lease_generation as number) < 1 ||
      (assignment.runBindingId !== undefined && receipt.run_id !== assignment.runBindingId) ||
      (receipt.activated !== undefined && (receipt.activated !== true || receipt.state !== 'provisioning')) ||
      (userScope && (typeof receipt.owner_key !== 'string' || !/^[a-f0-9]{52}$/.test(receipt.owner_key) ||
        !(receipt.state === 'provisioning' && receipt.pod_name === undefined && receipt.pod_uid === undefined ||
          receipt.state === undefined && typeof receipt.pod_name === 'string' &&
          /^chat-turn-[0-9a-f]{12}-[a-z0-9]{1,20}$/.test(receipt.pod_name) &&
          typeof receipt.pod_uid === 'string' && /^[a-z0-9-]{8,128}$/.test(receipt.pod_uid))))) {
    throw new Error('Crew run mint response invalid');
  }
  return receipt as { run_id: string; lease_generation: number; owner_key?: string; state?: 'provisioning'; activated?: boolean; pod_name?: string; pod_uid?: string };
}

export async function provisionCrewLease(
  assignment: Assignment,
  pod: SandboxPodIdentity,
  crewRun: { run_id: string; lease_generation: number },
  identity: SupervisorIdentity,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
): Promise<void> {
  const receipt = await supervisorRequest(assignment, pod, identity, CREW_LEASE_PATH, send, load, crewRun) as
    Partial<{ run_id: string; lease_generation: number; state: string }> | null;
  if (!receipt || receipt.run_id !== crewRun.run_id || receipt.lease_generation !== crewRun.lease_generation ||
      receipt.state !== 'provisioning') {
    throw new Error('Crew provisional lease response invalid');
  }
}

export async function activateCrewRun(
  assignment: Assignment,
  pod: SandboxPodIdentity,
  crewRun: { run_id: string; lease_generation: number },
  identity: SupervisorIdentity,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
): Promise<void> {
  const receipt = await supervisorRequest(assignment, pod, identity, CREW_ACTIVATE_PATH, send, load, crewRun) as
    Partial<{ run_id: string; lease_generation: number }> | null;
  if (!receipt || receipt.run_id !== crewRun.run_id || receipt.lease_generation !== crewRun.lease_generation) {
    throw new Error('Crew activation response invalid');
  }
}

export async function waitForCrewReadiness(
  assignment: Assignment,
  pod: SandboxPodIdentity,
  crewRun: { run_id: string; lease_generation: number },
  identity: SupervisorIdentity,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
  timing: SupervisorTiming = {},
): Promise<void> {
  const now = timing.now ?? (() => performance.now());
  const sleep = timing.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  const deadline = now() + 60_000;
  while (now() < deadline) {
    try {
      const receipt = await supervisorRequest(assignment, pod, identity, CREW_READY_PATH, send, load, crewRun) as
        Partial<{ run_id: string; lease_generation: number; state: string }> | null;
      if (!receipt || receipt.run_id !== crewRun.run_id || receipt.lease_generation !== crewRun.lease_generation ||
          receipt.state !== 'ready') throw new Error('Crew readiness response invalid');
      return;
    } catch (error) {
      if (!(error instanceof SupervisorResponseError) || error.status !== 425) throw error;
    }
    await sleep(Math.max(0, Math.min(500, deadline - now())));
  }
  throw new Error('Crew native readiness unverified before supervisor deadline');
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

/** Bounded admission attempts while the gateway has not yet observed the Running pod. */
export const ADMIT_VISIBILITY_ATTEMPTS = 5;
export const ADMIT_VISIBILITY_BACKOFF_MS = 1_000;

/**
 * Admits a pod the supervisor has already seen Running. The gateway verifies the pod
 * through its own API-server read and may observe the Running state a moment after the
 * supervisor did, which it reports as a non-enumerating 404; only that refusal is retried,
 * a bounded number of times. Every other non-2xx status stays terminal.
 */
export async function admitStartedSandboxPod(
  assignment: Assignment,
  pod: SandboxPodIdentity,
  identity: SupervisorIdentity,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
  timing: SupervisorTiming = {},
): Promise<Admission> {
  const sleep = timing.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await admitSandboxPod(assignment, pod, identity, send, load);
    } catch (error) {
      if (!(error instanceof SupervisorResponseError) || error.status !== 404 || attempt >= ADMIT_VISIBILITY_ATTEMPTS) throw error;
    }
    await sleep(ADMIT_VISIBILITY_BACKOFF_MS);
  }
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

/** How long removal is awaited while the API server has not acknowledged the delete at all. */
export const REMOVAL_DEADLINE_MS = 60_000;
/** How long removal is awaited once the pod carries a deletionTimestamp (kubelet teardown pending). */
export const REMOVAL_TERMINATING_DEADLINE_MS = 300_000;

/** Teardown is polled every second at first, then more slowly: the wait can last minutes. */
export function removalPollDelayMs(elapsedMs: number): number {
  if (elapsedMs < 10_000) return 1_000;
  if (elapsedMs < 60_000) return 2_000;
  return 5_000;
}

/**
 * Waits until the gateway confirms the sandbox pod is gone. The gateway reads the pod itself and
 * answers `removed: false` for as long as the API server returns the object, which for an exited
 * pod can be minutes after the delete was accepted (#8047). When the supervisor's pod API is given
 * it therefore also inspects the pod: once the container has exited it deletes the pod again with
 * gracePeriodSeconds 0 (the API server then drops the object without waiting for the kubelet), and
 * while the pod carries a deletionTimestamp the wait is extended to REMOVAL_TERMINATING_DEADLINE_MS.
 * Without the API the original 60 s teardown-only poll is preserved.
 */
export async function waitForSandboxRemoval(
  assignment: Assignment,
  pod: SandboxPodIdentity,
  identity: SupervisorIdentity,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
  timing: SupervisorTiming = {},
  api?: Pick<SandboxPodApi, 'inspect' | 'remove'>,
): Promise<void> {
  const now = timing.now ?? (() => performance.now());
  const sleep = timing.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  const started = now();
  let deadline = started + REMOVAL_DEADLINE_MS;
  let forced = false;
  let terminating = false;
  let polls = 0;
  for (;;) {
    if (now() >= deadline) {
      supervisorLog(assignment.runId, 'sandbox_removal', 'unverified', { polls, terminating, forced, elapsed_ms: Math.round(now() - started) });
      throw new Error('Chat sandbox removal unverified before supervisor deadline');
    }
    const receipt = await supervisorRequest(assignment, pod, identity, TEARDOWN_PATH, send, load) as
      Partial<{ run_id: string; pod_uid: string; removed: boolean }>;
    polls += 1;
    if (!receipt || receipt.run_id !== assignment.runId || receipt.pod_uid !== pod.uid || typeof receipt.removed !== 'boolean') {
      throw new Error('Chat sandbox removal response invalid');
    }
    if (receipt.removed) {
      supervisorLog(assignment.runId, 'sandbox_removal', 'removed', { polls, terminating, forced, elapsed_ms: Math.round(now() - started) });
      return;
    }
    if (api) {
      let observed: Awaited<ReturnType<SandboxPodApi['inspect']>> | undefined;
      try { observed = await api.inspect(pod.name, pod.uid); } catch { observed = undefined; }
      if (observed) {
        if (observed.metadata?.deletionTimestamp && !terminating) {
          terminating = true;
          deadline = started + REMOVAL_TERMINATING_DEADLINE_MS;
          supervisorLog(assignment.runId, 'sandbox_removal', 'terminating', { polls, phase: observed.status?.phase ?? '' });
        }
        if (!forced && sandboxPodEnded(observed)) {
          forced = true;
          try {
            await api.remove(pod.name, pod.uid, { gracePeriodSeconds: 0 });
            supervisorLog(assignment.runId, 'sandbox_removal', 'forced_delete', { polls, phase: observed.status?.phase ?? '' });
          } catch {
            supervisorLog(assignment.runId, 'sandbox_removal', 'forced_delete_failed', { polls });
          }
        }
      }
    }
    await sleep(Math.max(0, Math.min(removalPollDelayMs(now() - started), deadline - now())));
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
  return validateSandboxTerminal(assignment, pod, admission, receipt);
}

export function validateSandboxTerminal(
  assignment: Assignment, pod: SandboxPodIdentity, admission: Admission, receipt: Partial<SandboxTerminalReceipt> | null,
): SandboxTerminalReceipt {
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
      await withAdmittedSandboxPod(assignment, api, identity, observe, send, load, false, undefined, timing);
    }
  } catch (error) {
    if (!exited) throw error;
  }
  if (!exited) throw new Error('Chat sandbox exit evidence unavailable');
  await waitForSandboxRemoval(assignment, exited.pod, identity, send, load, timing, api);
  const terminal = await finalizeSandboxTurn(assignment, exited.pod, exited.admission, identity, send, load);
  await reconciliation.onTerminal?.(exited.pod, terminal);
  return terminal;
}

export async function withAdmittedSandboxPod<Result>(
  assignment: Assignment,
  api: SandboxPodApi,
  identity: SupervisorIdentity,
  run: (pod: SandboxPodIdentity, admission: Admission, verifiedAssignment?: Assignment) => Promise<Result>,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
  cleanupOnFailure = true,
  reservedPod?: SandboxPodIdentity,
  timing: SupervisorTiming = {},
): Promise<Result> {
  let completed = false;
  // Session choice > deployment default > sandbox-loop (#8115).
  const executor = persistentExecutor(assignment);
  if (process.env.ADP_EXECUTOR_SCOPE !== undefined && !['user', 'session'].includes(process.env.ADP_EXECUTOR_SCOPE)) {
    throw new Error('Unsupported persistent chat executor scope');
  }
  const userScope = assignment.sessionMode === 'persistent' && executor === 'kirocrew' && process.env.ADP_EXECUTOR_SCOPE === 'user';
  const minted = assignment.sessionMode === 'persistent' && (!assignment.runBindingId || userScope) ?
    await mintCrewRun({ ...assignment, executor }, identity, send, load) : undefined;
  if (userScope && !minted?.owner_key) throw new Error('Crew owner binding unavailable');
  if (userScope && minted?.pod_name && minted.pod_uid) {
    const existingPod = { name: minted.pod_name, uid: minted.pod_uid };
    const verifiedAssignment = { ...assignment, executor, verifiedCrewOwner: existingPod };
    const admission = await admitSandboxPod(verifiedAssignment, existingPod, identity, send, load);
    return run(existingPod, admission, verifiedAssignment);
  }
  const podAssignment = assignment.sessionMode === 'persistent' ?
    { ...assignment, executor, ownerKey: userScope ? minted!.owner_key : assignment.ownerKey,
      runBindingId: assignment.runBindingId ?? minted!.run_id, crewScope: userScope ? 'user' as const : undefined } : assignment;
  // Observe the pod withSandboxPod creates and removes, so a start failure is attributable: the
  // launcher deletes a pod that never starts before the error surfaces, and without its identity
  // the operator had only "did not start" and the gateway only a pod it could no longer see (#8061).
  let created: SandboxPodIdentity | undefined;
  let started = false;
  let removedUnstarted = false;
  const observed: SandboxPodApi = {
    resolveGateway: () => api.resolveGateway(),
    create: async template => {
      const pod = await api.create(template);
      const name = pod.metadata?.name;
      const uid = pod.metadata?.uid;
      if (typeof name === 'string' && typeof uid === 'string') created = { name, uid };
      return pod;
    },
    inspect: (name, uid) => api.inspect(name, uid),
    remove: async (name, uid, options) => {
      if (!started && created?.name === name && created.uid === uid) {
        removedUnstarted = true;
        supervisorLog(assignment.runId, 'sandbox_start', 'failed', { pod_name: name, pod_uid: uid });
      }
      await (options === undefined ? api.remove(name, uid) : api.remove(name, uid, options));
    },
  };
  const admitPod = async (pod: SandboxPodIdentity) => {
    started = true;
    if (userScope && !minted?.activated) {
      await provisionCrewLease({ ...assignment, executor }, pod, minted!, identity, send, load);
      await activateCrewRun({ ...assignment, executor }, pod, minted!, identity, send, load);
    }
    // withSandboxPod hands the pod over only once it is Running, so admission can succeed;
    // the bounded 404 retry covers the gateway observing that state a moment later.
    const admission = await admitStartedSandboxPod(assignment, pod, identity, send, load, timing);
    if (userScope) await waitForCrewReadiness({ ...assignment, executor }, pod, minted!, identity, send, load, timing);
    const result = await run(pod, admission);
    completed = true;
    return result;
  };
  if (reservedPod) return admitPod(reservedPod);
  try {
    return await withSandboxPod(podAssignment, observed, admitPod, () => completed ? !userScope : cleanupOnFailure, timing);
  } catch (error) {
    if (created && removedUnstarted && !started) throw new SandboxStartError(error, created);
    throw error;
  }
}
