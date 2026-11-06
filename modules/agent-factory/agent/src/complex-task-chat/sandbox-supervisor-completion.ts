import { createHash } from 'node:crypto';
import { fromTokenFile } from '@aws-sdk/credential-provider-web-identity';
import { canonicalJson } from '../invocability-probe/canonical-json';
import { type SandboxPodApi, type SandboxPodIdentity } from './sandbox-launcher';
import { sandboxCreationName } from './sandbox-pod';
import {
  COMPLETE_PATH, RESUME_PATH, RESERVE_PATH, SESSION_COMMIT_PATH, SandboxStartError, SupervisorResponseError, isRemovalUnconfirmed, reconcileAdmittedSandbox,
  persistentExecutor, supervisorLog, supervisorRequest, waitForSandboxRemoval,
  admitSandboxPod, sandboxPodExited, validateSandboxTerminal, waitForCrewReadiness, withAdmittedSandboxPod,
  type Admission, type Assignment, type RegisteredAssignment, type SandboxTerminalReceipt, type SupervisorIdentity, type SupervisorTiming,
} from './sandbox-supervisor-admission';

type Restored = ({ pod: SandboxPodIdentity; attempt: number; sessionRunId?: string; verifiedCrewOwner?: SandboxPodIdentity;
  crewReadiness?: { run_id: string; lease_generation: number; state: 'provisioning' | 'ready' } } &
  ({ state: 'admitted'; admission: Admission; recoveryRequired?: boolean } | { state: 'pre_admission_cleanup' })) |
  { state: 'queued_completed' } | { state: 'creation_reserved'; attempt: number; pod?: SandboxPodIdentity; crewRunId?: string } |
  { state: 'session_recovery'; assignment: RegisteredAssignment };

/** Distinct retry reasons logged per reconciliation loop; later distinct reasons are counted, not printed. */
export const RETRY_LOG_LIMIT = 8;

function retryReason(error: unknown): { key: string; fields: Record<string, string | number> } {
  if (error instanceof SupervisorResponseError) {
    const detail = error.detail.slice(0, 200);
    return { key: `${error.status} ${detail}`, fields: { http_status: error.status, gateway_error: detail } };
  }
  const name = error instanceof Error ? error.name : 'Error';
  return { key: name, fields: { error: name } };
}

/**
 * Retries a gateway request for up to a minute. Each distinct HTTP status + body (first 200
 * bytes; never a token or payload) is logged once as `[supervisor] step=<path> status=retrying`,
 * bounded by RETRY_LOG_LIMIT, so a 409 the gateway answers on every attempt is visible in the
 * supervisor log rather than only in the API Gateway execution log (#8061, #7990). The deadline
 * error names the last response for the same reason.
 */
async function retrySupervisorRequest(
  request: () => Promise<unknown>,
  waiting: boolean,
  timing: SupervisorTiming,
  context: { runId: string; step: string },
): Promise<unknown> {
  const now = timing.now ?? (() => performance.now());
  const sleep = timing.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  const deadline = now() + 60_000;
  const logged = new Set<string>();
  let last: string | undefined;
  for (;;) {
    if (now() >= deadline) {
      throw new Error(`Chat supervisor reconciliation deadline exceeded${last ? `; last gateway response: ${last}` : ''}`);
    }
    try { return await request(); }
    catch (error) {
      if (!(error instanceof TypeError || (error instanceof Error && ['AbortError', 'TimeoutError'].includes(error.name)) ||
          (error instanceof SupervisorResponseError && ([429, 502, 503, 504].includes(error.status) || (waiting && error.status === 409))))) {
        throw error;
      }
      const reason = retryReason(error);
      last = error instanceof SupervisorResponseError ? `HTTP ${error.status} ${error.detail.slice(0, 200)}` : reason.key;
      if (!logged.has(reason.key) && logged.size < RETRY_LOG_LIMIT) {
        logged.add(reason.key);
        supervisorLog(context.runId, context.step, 'retrying', reason.fields);
      }
    }
    await sleep(Math.max(0, Math.min(1000, deadline - now())));
  }
}

/** Longest a single supervisor keeps retrying completion (also capped by the queue visibility deadline). */
export const COMPLETION_WINDOW_MS = 300_000;
/** Completion retries back off exponentially from one second up to this cap. */
export const COMPLETION_BACKOFF_CAP_MS = 30_000;

function completionBackoffMs(attempt: number): number {
  return Math.min(1_000 * 2 ** Math.max(0, attempt - 1), COMPLETION_BACKOFF_CAP_MS);
}

function isTransient(error: unknown): boolean {
  return error instanceof TypeError || (error instanceof Error && ['AbortError', 'TimeoutError'].includes(error.name)) ||
    (error instanceof SupervisorResponseError && [429, 502, 503, 504].includes(error.status));
}

/**
 * Requests completion with a bounded, backing-off retry loop. Transient failures and 409 conflicts
 * are retried until COMPLETION_WINDOW_MS or the queue visibility deadline, whichever comes first;
 * the gateway's "sandbox removal not confirmed" 409 additionally re-awaits the pod's removal
 * (force-deleting it once exited) before the next attempt. Every other status is terminal. A
 * supervisor used to retry every second for a minute and then exit non-zero, leaving the delivery
 * in flight and ~44 requests in the gateway log per redelivery (#8047); now one line names the
 * gateway's last error body when the window closes.
 */
async function requestTurnCompletion(
  assignment: RegisteredAssignment,
  pod: SandboxPodIdentity,
  identity: SupervisorIdentity,
  send: typeof fetch,
  load: typeof fromTokenFile,
  timing: SupervisorTiming,
  api?: Pick<SandboxPodApi, 'inspect' | 'remove'>,
): Promise<unknown> {
  const now = timing.now ?? (() => performance.now());
  const sleep = timing.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  const started = now();
  const deadline = Math.min(started + COMPLETION_WINDOW_MS, timing.visibilityDeadline ?? Number.POSITIVE_INFINITY);
  for (let attempt = 1; ; attempt += 1) {
    try {
      const value = await supervisorRequest(assignment, pod, identity, COMPLETE_PATH, send, load);
      supervisorLog(assignment.runId, 'complete', 'completed', { attempt, elapsed_ms: Math.round(now() - started) });
      return value;
    } catch (error) {
      const removal = isRemovalUnconfirmed(error);
      const conflict = !removal && error instanceof SupervisorResponseError && error.status === 409;
      if (!removal && !conflict && !isTransient(error)) throw error;
      const status = removal ? 'removal_unconfirmed' : conflict ? 'conflict' : 'transient';
      const gateway: Record<string, string | number> = error instanceof SupervisorResponseError ?
        { http_status: error.status, gateway_error: error.detail.slice(0, 200) } : {};
      supervisorLog(assignment.runId, 'complete', status, { attempt, elapsed_ms: Math.round(now() - started), ...gateway });
      if (now() >= deadline) {
        supervisorLog(assignment.runId, 'complete', 'deadline_exceeded', { attempt, elapsed_ms: Math.round(now() - started), ...gateway });
        throw new Error(`Chat supervisor completion deadline exceeded after ${attempt} attempts; last gateway response: ` +
          (error instanceof SupervisorResponseError ? `HTTP ${error.status} ${error.detail.slice(0, 200)}` : (error as Error).message));
      }
      if (removal) await waitForSandboxRemoval(assignment, pod, identity, send, load, timing, api);
      await sleep(Math.max(0, Math.min(completionBackoffMs(attempt), deadline - now())));
    }
  }
}

/**
 * Resumes the turn from the gateway's durable state. `pod` is given only by a supervisor that
 * watched its own reserved pod fail to start: the gateway then knows the uid of a pod it may no
 * longer be able to observe and can record the pre-admission cleanup against it (#8061).
 */
export async function restoreSupervisorTurn(
  assignment: RegisteredAssignment,
  identity: SupervisorIdentity,
  send: typeof fetch,
  load: typeof fromTokenFile,
  timing: SupervisorTiming,
  pod: SandboxPodIdentity | null = null,
): Promise<Restored | undefined> {
  const value = await retrySupervisorRequest(async () => {
    const value = await supervisorRequest(assignment, pod, identity, RESUME_PATH, send, load) as Record<string, unknown> | null;
    if (value?.state === 'session_pending') throw new SupervisorResponseError(409);
    return value;
  }, true, timing, { runId: assignment.runId, step: 'resume' });
  const receipt = value as Record<string, unknown> | null;
  if (!receipt || receipt.run_id !== assignment.runId || receipt.session_id !== assignment.sessionId ||
      receipt.task_id !== assignment.taskId || receipt.session_generation !== assignment.sessionGeneration) {
    throw new Error('Chat supervisor recovery scope invalid');
  }
  if (receipt.state === 'unstarted') return;
  if (receipt.state === 'session_recovery') {
    const previous = receipt.recovery as Record<string, unknown> | null;
    if (!previous || previous.run_id === assignment.runId || previous.session_id !== assignment.sessionId ||
        previous.session_generation !== assignment.sessionGeneration || previous.image_digest !== assignment.image.split('@')[1] ||
        typeof previous.run_id !== 'string' || !/^[A-Za-z0-9_.:-]{1,128}$/.test(previous.run_id) ||
        typeof previous.task_id !== 'string' || !/^[A-Za-z0-9_.:-]{1,128}$/.test(previous.task_id) ||
        typeof previous.envelope_digest !== 'string' || !/^[a-f0-9]{64}$/.test(previous.envelope_digest)) {
      throw new Error('Chat predecessor recovery scope invalid');
    }
    return { state: 'session_recovery', assignment: { ...assignment, runId: previous.run_id,
      taskId: previous.task_id, envelopeDigest: previous.envelope_digest, sessionMode: 'persistent' } };
  }
  if (receipt.state === 'creation_reserved') {
    if (receipt.session_mode !== 'persistent' || receipt.pod_name !== sandboxCreationName(assignment.runId) ||
        receipt.image_digest !== assignment.image.split('@')[1] || typeof receipt.attempt !== 'number' ||
        !Number.isSafeInteger(receipt.attempt) || receipt.attempt < 1 || 'lease_generation' in receipt ||
        (receipt.crew_run_id !== undefined && (typeof receipt.crew_run_id !== 'string' ||
          !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(receipt.crew_run_id))) ||
        (receipt.sandbox_uid !== undefined && (typeof receipt.sandbox_uid !== 'string' || !/^[a-z0-9-]{8,128}$/.test(receipt.sandbox_uid)))) {
      throw new Error('Chat reserved creation recovery invalid');
    }
    return { state: 'creation_reserved', attempt: receipt.attempt,
      ...(receipt.crew_run_id ? { crewRunId: receipt.crew_run_id as string } : {}),
      ...(receipt.sandbox_uid ? { pod: { name: receipt.pod_name as string, uid: receipt.sandbox_uid as string } } : {}) };
  }
  if (receipt.state === 'queued_completed') {
    verifyQueuedCompletion(assignment, receipt.completion);
    return { state: 'queued_completed' };
  }
  if (receipt.session_mode !== undefined && receipt.session_mode !== 'persistent' && receipt.session_mode !== 'ephemeral') {
    throw new Error('Chat supervisor recovery mode invalid');
  }
  if (receipt.session_mode === 'persistent' && (typeof receipt.session_run_id !== 'string' || !/^[A-Za-z0-9_.:-]{1,128}$/.test(receipt.session_run_id))) {
    throw new Error('Chat supervisor recovery session binding invalid');
  }
  const sessionRunId = receipt.session_mode === 'persistent' ? receipt.session_run_id as string : undefined;
  const prefix = `chat-turn-${createHash('sha256').update(sessionRunId ?? assignment.runId).digest('hex').slice(0, 12)}-`;
  const crewOwner = receipt.crew_owner_pod === true && receipt.state === 'admitted' && sessionRunId !== undefined &&
    persistentExecutor(assignment) === 'kirocrew' && process.env.ADP_EXECUTOR_SCOPE === 'user';
  if (receipt.crew_owner_pod !== undefined && !crewOwner) throw new Error('Crew supervisor recovery owner invalid');
  const crewReadiness = receipt.crew_readiness as Record<string, unknown> | undefined;
  if (crewReadiness !== undefined && (!crewOwner || sessionRunId !== assignment.runId || !crewReadiness ||
      typeof crewReadiness.run_id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(crewReadiness.run_id) ||
      typeof crewReadiness.lease_generation !== 'number' || !Number.isSafeInteger(crewReadiness.lease_generation) || crewReadiness.lease_generation < 1 ||
      !['provisioning', 'ready'].includes(crewReadiness.state as string) ||
      (assignment.runBindingId !== undefined && assignment.runBindingId !== crewReadiness.run_id))) {
    throw new Error('Crew supervisor recovery readiness invalid');
  }
  if ((receipt.state !== 'admitted' && receipt.state !== 'pre_admission_cleanup') || typeof receipt.pod_name !== 'string' ||
      (!receipt.pod_name.startsWith(prefix) && !crewOwner) || !/^chat-turn-[0-9a-f]{12}-[a-z0-9]{1,20}$/.test(receipt.pod_name) ||
      typeof receipt.sandbox_uid !== 'string' || !/^[a-z0-9-]{8,128}$/.test(receipt.sandbox_uid) ||
      receipt.image_digest !== assignment.image.split('@')[1] ||
      typeof receipt.attempt !== 'number' || !Number.isSafeInteger(receipt.attempt) || receipt.attempt < 1) {
    throw new Error('Chat supervisor recovery binding invalid');
  }
  const binding = { pod: { name: receipt.pod_name, uid: receipt.sandbox_uid }, attempt: receipt.attempt, ...(sessionRunId ? { sessionRunId } : {}),
    ...(crewOwner ? { verifiedCrewOwner: { name: receipt.pod_name, uid: receipt.sandbox_uid } } : {}) };
  if (receipt.state === 'pre_admission_cleanup') {
    if (typeof receipt.removed !== 'boolean' || 'lease_generation' in receipt) throw new Error('Chat supervisor cleanup receipt invalid');
    return { ...binding, state: 'pre_admission_cleanup' };
  }
  if (typeof receipt.lease_generation !== 'number' || !Number.isSafeInteger(receipt.lease_generation) || receipt.lease_generation < 1) {
    throw new Error('Chat supervisor recovery binding invalid');
  }
  if (receipt.recovery_required !== undefined && (receipt.recovery_required !== true || !sessionRunId)) {
    throw new Error('Chat recovery fence invalid');
  }
  return { ...binding, state: 'admitted',
    ...(crewReadiness ? { crewReadiness: crewReadiness as { run_id: string; lease_generation: number; state: 'provisioning' | 'ready' } } : {}),
    ...(receipt.recovery_required ? { recoveryRequired: true } : {}),
    admission: { run_id: assignment.runId, session_id: assignment.sessionId, lease_generation: receipt.lease_generation,
      ...(sessionRunId ? { session_mode: 'persistent' as const } : {}) } };
}

export async function waitForTurnCompletion(
  assignment: RegisteredAssignment,
  pod: SandboxPodIdentity,
  terminal: SandboxTerminalReceipt,
  identity: SupervisorIdentity,
  send: typeof fetch,
  load: typeof fromTokenFile,
  timing: SupervisorTiming,
  api?: Pick<SandboxPodApi, 'inspect' | 'remove'>,
): Promise<void> {
  const value = await requestTurnCompletion(assignment, pod, identity, send, load, timing, api);
  const receipt = value as Record<string, unknown> | null;
  verifyTurnCompletion(assignment, pod, terminal, receipt);
}

function verifyTurnCompletion(assignment: RegisteredAssignment, pod: SandboxPodIdentity, terminal: SandboxTerminalReceipt,
  receipt: Record<string, unknown> | null): void {
  const deliveryId = `chat-terminal-${createHash('sha256').update(canonicalJson(terminal)).digest('hex')}`;
  if (!receipt || receipt.run_id !== assignment.runId || receipt.session_id !== assignment.sessionId ||
      receipt.task_id !== assignment.taskId || receipt.session_generation !== assignment.sessionGeneration ||
      receipt.attempt !== terminal.attempt || receipt.lease_generation !== terminal.lease_generation ||
      receipt.sandbox_uid !== pod.uid || receipt.delivery_id !== deliveryId ||
      receipt.processing_lock_released !== true || receipt.input_acknowledgement_ready !== true ||
      typeof receipt.completed_at !== 'number' || !Number.isSafeInteger(receipt.completed_at) || receipt.completed_at < terminal.finalized_at) {
    throw new Error('Chat supervisor completion receipt invalid');
  }
}

export async function reconcileSupervisorDelivery(
  assignment: RegisteredAssignment,
  api: SandboxPodApi,
  identity: SupervisorIdentity,
  send: typeof fetch,
  load: typeof fromTokenFile,
  timing: SupervisorTiming,
  recoveringPredecessor = false,
): Promise<void> {
  let resume = await restoreSupervisorTurn(assignment, identity, send, load, timing);
  if (resume?.state === 'session_recovery') {
    if (recoveringPredecessor) throw new Error('Chat predecessor recovery cycle refused');
    await reconcileSupervisorDelivery(resume.assignment, api, identity, send, load, timing, true);
    resume = await restoreSupervisorTurn(assignment, identity, send, load, timing);
    if (resume?.state === 'session_recovery') throw new Error('Chat predecessor cleanup not committed');
  }
  if (resume?.state === 'queued_completed') return;
  if (resume?.state === 'pre_admission_cleanup') {
    await completePreAdmissionCleanup(assignment, resume, api, identity, send, load, timing);
    return;
  }
  try {
    await launchSupervisedTurn(assignment, resume, api, identity, send, load, timing);
  } catch (error) {
    if (!(error instanceof SandboxStartError)) throw error;
    await reconcileUnstartedSandbox(assignment, error, api, identity, send, load, timing);
  }
}

async function completePreAdmissionCleanup(
  assignment: RegisteredAssignment,
  resume: Extract<Restored, { state: 'pre_admission_cleanup' }>,
  api: SandboxPodApi,
  identity: SupervisorIdentity,
  send: typeof fetch,
  load: typeof fromTokenFile,
  timing: SupervisorTiming,
): Promise<void> {
  try { await api.remove(resume.pod.name, resume.pod.uid); } catch {}
  await waitForSandboxRemoval(assignment, resume.pod, identity, send, load, timing, api);
  await waitForCleanedTurnCompletion(assignment, resume.pod, resume.attempt, identity, send, load, timing, api);
}

/**
 * A reserved pod that never started was already deleted by the launcher. Instead of exiting and
 * leaving the delivery to come back after the visibility timeout, resume once more naming the
 * pod: the gateway can then record the pre-admission cleanup from the pod's absence (it was never
 * admitted, so there is no capability to revoke) and the turn ends as a retryable interruption
 * within this dispatch (#8061). Anything other than that cleanup state, or a reconciliation that
 * does not commit within the window, re-raises the original start failure so the redelivery
 * path (which records a stable sentinel uid) takes over.
 */
async function reconcileUnstartedSandbox(
  assignment: RegisteredAssignment,
  failure: SandboxStartError,
  api: SandboxPodApi,
  identity: SupervisorIdentity,
  send: typeof fetch,
  load: typeof fromTokenFile,
  timing: SupervisorTiming,
): Promise<void> {
  const pod = { pod_name: failure.pod.name, pod_uid: failure.pod.uid };
  supervisorLog(assignment.runId, 'sandbox_start', 'reconciling', { ...pod, error: failure.message });
  let resume: Restored | undefined;
  try {
    resume = await restoreSupervisorTurn(assignment, identity, send, load, timing, failure.pod);
  } catch (error) {
    supervisorLog(assignment.runId, 'sandbox_start', 'unreconciled', { ...pod, error: error instanceof Error ? error.message : 'unknown' });
    throw failure;
  }
  if (resume?.state !== 'pre_admission_cleanup') {
    supervisorLog(assignment.runId, 'sandbox_start', 'unreconciled', { ...pod, state: resume?.state ?? 'unstarted' });
    throw failure;
  }
  await completePreAdmissionCleanup(assignment, resume, api, identity, send, load, timing);
  supervisorLog(assignment.runId, 'sandbox_start', 'reconciled', { ...pod, sandbox_uid: resume.pod.uid });
}

async function launchSupervisedTurn(
  assignment: RegisteredAssignment,
  resume: Exclude<Restored, { state: 'queued_completed' | 'pre_admission_cleanup' | 'session_recovery' }> | undefined,
  api: SandboxPodApi,
  identity: SupervisorIdentity,
  send: typeof fetch,
  load: typeof fromTokenFile,
  timing: SupervisorTiming,
): Promise<void> {
  let launchAssignment = resume?.state === 'admitted' && resume.sessionRunId ?
    { ...assignment, sessionMode: 'persistent' as const, sessionRunId: resume.sessionRunId,
      ...(resume.verifiedCrewOwner ? { executor: 'kirocrew' as const, verifiedCrewOwner: resume.verifiedCrewOwner } : {}) } : assignment;
  let reservedCrewPod: SandboxPodIdentity | undefined;
  if (resume?.state === 'creation_reserved') {
    launchAssignment = { ...assignment, sessionMode: 'persistent', podName: sandboxCreationName(assignment.runId),
      ...(resume.crewRunId ? { runBindingId: resume.crewRunId } : {}) };
    const userCrew = persistentExecutor(assignment) === 'kirocrew' && process.env.ADP_EXECUTOR_SCOPE === 'user';
    if (userCrew) reservedCrewPod = resume.pod;
    resume = resume.pod && !userCrew ? { state: 'admitted', pod: resume.pod, attempt: resume.attempt,
      admission: await admitSandboxPod(launchAssignment, resume.pod, identity, send, load) } : undefined;
  } else if (!resume) {
    const receipt = await supervisorRequest(assignment, null, identity, RESERVE_PATH, send, load) as Record<string, unknown> | null;
    if (!receipt || receipt.state !== 'create' || receipt.run_id !== assignment.runId || receipt.session_id !== assignment.sessionId ||
        receipt.task_id !== assignment.taskId || receipt.session_generation !== assignment.sessionGeneration ||
        receipt.pod_name !== sandboxCreationName(assignment.runId) || receipt.image_digest !== assignment.image.split('@')[1] ||
        (receipt.session_mode !== undefined && receipt.session_mode !== 'ephemeral' && receipt.session_mode !== 'persistent') ||
        (assignment.sessionMode !== undefined && receipt.session_mode !== assignment.sessionMode) ||
        typeof receipt.attempt !== 'number' || !Number.isSafeInteger(receipt.attempt) || receipt.attempt < 1) {
      throw new Error('Chat supervisor creation reservation invalid');
    }
    launchAssignment = { ...assignment, podName: sandboxCreationName(assignment.runId),
      ...(receipt.session_mode === 'persistent' ? { sessionMode: 'persistent' as const } : {}) };
  }
  if (resume?.state === 'admitted' && resume.recoveryRequired) {
    try { await api.remove(resume.pod.name, resume.pod.uid); } catch {}
    await reconcileAdmittedSandbox(launchAssignment, api, identity, send, load, timing, {
      resume, onTerminal: async (pod, terminal) => {
        await waitForTurnCompletion(launchAssignment, pod, terminal, identity, send, load, timing, api);
      },
    });
    return;
  }
  if (launchAssignment.sessionMode === 'persistent') {
    const observe = async (pod: SandboxPodIdentity, admission: Admission, verifiedAssignment?: Assignment) => {
      const currentAssignment = { ...launchAssignment, ...verifiedAssignment };
      const now = timing.now ?? (() => performance.now());
      const sleep = timing.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
      const deadline = now() + 900_000;
      while (now() < deadline) {
        try {
          const receipt = await supervisorRequest(currentAssignment, pod, identity, SESSION_COMMIT_PATH, send, load) as {
            terminal: SandboxTerminalReceipt; completion: Record<string, unknown>;
          };
          const terminal = validateSandboxTerminal(launchAssignment, pod, admission, receipt?.terminal);
          verifyTurnCompletion(assignment, pod, terminal, receipt.completion);
          return;
        } catch (error) {
          if (!(error instanceof SupervisorResponseError) || error.status !== 409) throw error;
        }
        if (await sandboxPodExited(currentAssignment, pod, identity, send, load)) {
          await reconcileAdmittedSandbox(currentAssignment, api, identity, send, load, timing, {
            resume: { pod, admission }, onTerminal: async (ended, terminal) => {
              await waitForTurnCompletion(currentAssignment, ended, terminal, identity, send, load, timing, api);
            },
          });
          return;
        }
        await sleep(Math.min(1000, deadline - now()));
      }
      throw new Error('Chat persistent turn reconciliation deadline exceeded');
    };
    if (resume) {
      if (resume.crewReadiness?.state === 'provisioning') {
        await waitForCrewReadiness(launchAssignment, resume.pod, resume.crewReadiness, identity, send, load, timing);
      }
      await observe(resume.pod, resume.admission);
    }
    else await withAdmittedSandboxPod(launchAssignment, api, identity, observe, send, load, false, reservedCrewPod, timing);
    return;
  }
  await reconcileAdmittedSandbox(launchAssignment, api, identity, send, load, timing, {
    resume,
    onTerminal: async (pod, terminal) => {
      if (resume && terminal.attempt !== resume.attempt) throw new Error('Chat supervisor terminal attempt changed');
      await waitForTurnCompletion(assignment, pod, terminal, identity, send, load, timing, api);
    },
  });
}

function verifyQueuedCompletion(assignment: RegisteredAssignment, value: unknown): void {
  const receipt = value as Record<string, unknown> | null;
  const terminal = receipt?.terminal as Record<string, unknown> | null;
  if (!receipt || !terminal || typeof terminal !== 'object' || Array.isArray(terminal) || Object.keys(terminal).length !== 13 ||
      terminal.phase !== 'queued' || terminal.run_id !== assignment.runId || terminal.session_id !== assignment.sessionId ||
      typeof terminal.attempt !== 'number' || !Number.isSafeInteger(terminal.attempt) || terminal.attempt < 1 ||
      typeof terminal.credential_epoch !== 'number' || !Number.isSafeInteger(terminal.credential_epoch) || terminal.credential_epoch < 1 ||
      !['cancelled', 'interrupted'].includes(terminal.outcome as string) || terminal.retryable !== (terminal.outcome === 'interrupted') ||
      terminal.terminal !== true || terminal.message_id !== null ||
      terminal.automatic_replay_permitted !== false || terminal.accounting_status !== 'not_used' || terminal.cleanup_required !== true ||
      typeof terminal.finalized_at !== 'number' || !Number.isSafeInteger(terminal.finalized_at) || terminal.finalized_at < 1 ||
      receipt.phase !== 'queued' || receipt.creation_fenced !== true || 'sandbox_uid' in receipt || 'lease_generation' in receipt ||
      receipt.run_id !== assignment.runId || receipt.session_id !== assignment.sessionId || receipt.task_id !== assignment.taskId ||
      receipt.session_generation !== assignment.sessionGeneration || receipt.attempt !== terminal.attempt ||
      receipt.credential_epoch !== terminal.credential_epoch ||
      receipt.delivery_id !== `chat-terminal-${createHash('sha256').update(canonicalJson(terminal)).digest('hex')}` ||
      receipt.processing_lock_released !== true || receipt.input_acknowledgement_ready !== true ||
      typeof receipt.completed_at !== 'number' || !Number.isSafeInteger(receipt.completed_at) || receipt.completed_at < terminal.finalized_at) {
    throw new Error('Chat supervisor queued completion receipt invalid');
  }
}

async function waitForCleanedTurnCompletion(
  assignment: RegisteredAssignment,
  pod: SandboxPodIdentity,
  attempt: number,
  identity: SupervisorIdentity,
  send: typeof fetch,
  load: typeof fromTokenFile,
  timing: SupervisorTiming,
  api?: Pick<SandboxPodApi, 'inspect' | 'remove'>,
): Promise<void> {
  const value = await requestTurnCompletion(assignment, pod, identity, send, load, timing, api);
  const receipt = value as Record<string, unknown> | null;
  const terminal = receipt?.terminal as Record<string, unknown> | null;
  if (!receipt || !terminal || typeof terminal !== 'object' || Array.isArray(terminal) || Object.keys(terminal).length !== 14 ||
      terminal.phase !== 'pre_admission' || terminal.run_id !== assignment.runId || terminal.session_id !== assignment.sessionId ||
      terminal.sandbox_uid !== pod.uid || terminal.attempt !== attempt ||
      typeof terminal.credential_epoch !== 'number' || !Number.isSafeInteger(terminal.credential_epoch) || terminal.credential_epoch < 1 ||
      !['cancelled', 'interrupted'].includes(terminal.outcome as string) || terminal.retryable !== (terminal.outcome === 'interrupted') ||
      terminal.terminal !== true || terminal.message_id !== null || terminal.cleanup_required !== false ||
      terminal.automatic_replay_permitted !== false || terminal.accounting_status !== 'not_used' ||
      typeof terminal.finalized_at !== 'number' || !Number.isSafeInteger(terminal.finalized_at) || terminal.finalized_at < 1 ||
      receipt.phase !== 'pre_admission' || 'lease_generation' in receipt ||
      receipt.run_id !== assignment.runId || receipt.session_id !== assignment.sessionId || receipt.task_id !== assignment.taskId ||
      receipt.session_generation !== assignment.sessionGeneration || receipt.attempt !== attempt || receipt.sandbox_uid !== pod.uid ||
      receipt.credential_epoch !== terminal.credential_epoch ||
      receipt.delivery_id !== `chat-terminal-${createHash('sha256').update(canonicalJson(terminal)).digest('hex')}` ||
      receipt.processing_lock_released !== true || receipt.input_acknowledgement_ready !== true ||
      typeof receipt.completed_at !== 'number' || !Number.isSafeInteger(receipt.completed_at) || receipt.completed_at < terminal.finalized_at) {
    throw new Error('Chat supervisor cleaned completion receipt invalid');
  }
}
