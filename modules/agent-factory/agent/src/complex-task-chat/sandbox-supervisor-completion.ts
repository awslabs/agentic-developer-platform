import { createHash } from 'node:crypto';
import { fromTokenFile } from '@aws-sdk/credential-provider-web-identity';
import { canonicalJson } from '../invocability-probe/canonical-json';
import type { SandboxPodApi, SandboxPodIdentity } from './sandbox-launcher';
import { sandboxCreationName } from './sandbox-pod';
import {
  COMPLETE_PATH, RESUME_PATH, RESERVE_PATH, SupervisorResponseError, reconcileAdmittedSandbox, supervisorRequest, waitForSandboxRemoval,
  type Admission, type RegisteredAssignment, type SandboxTerminalReceipt, type SupervisorIdentity, type SupervisorTiming,
} from './sandbox-supervisor-admission';

type Restored = ({ pod: SandboxPodIdentity; attempt: number } &
  ({ state: 'admitted'; admission: Admission } | { state: 'pre_admission_cleanup' })) | { state: 'queued_completed' };

async function retrySupervisorRequest(
  request: () => Promise<unknown>,
  waiting: boolean,
  timing: SupervisorTiming,
): Promise<unknown> {
  const now = timing.now ?? (() => performance.now());
  const sleep = timing.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  const deadline = now() + 60_000;
  for (;;) {
    if (now() >= deadline) throw new Error('Chat supervisor reconciliation deadline exceeded');
    try { return await request(); }
    catch (error) {
      if (!(error instanceof TypeError || (error instanceof Error && ['AbortError', 'TimeoutError'].includes(error.name)) ||
          (error instanceof SupervisorResponseError && ([429, 502, 503, 504].includes(error.status) || (waiting && error.status === 409))))) {
        throw error;
      }
    }
    await sleep(Math.max(0, Math.min(1000, deadline - now())));
  }
}

export async function restoreSupervisorTurn(
  assignment: RegisteredAssignment,
  identity: SupervisorIdentity,
  send: typeof fetch,
  load: typeof fromTokenFile,
  timing: SupervisorTiming,
): Promise<Restored | undefined> {
  const value = await retrySupervisorRequest(() => supervisorRequest(assignment, null, identity, RESUME_PATH, send, load), false, timing);
  const receipt = value as Record<string, unknown> | null;
  if (!receipt || receipt.run_id !== assignment.runId || receipt.session_id !== assignment.sessionId ||
      receipt.task_id !== assignment.taskId || receipt.session_generation !== assignment.sessionGeneration) {
    throw new Error('Chat supervisor recovery scope invalid');
  }
  if (receipt.state === 'unstarted') return;
  if (receipt.state === 'queued_completed') {
    verifyQueuedCompletion(assignment, receipt.completion);
    return { state: 'queued_completed' };
  }
  const prefix = `chat-turn-${createHash('sha256').update(assignment.runId).digest('hex').slice(0, 12)}-`;
  if ((receipt.state !== 'admitted' && receipt.state !== 'pre_admission_cleanup') || typeof receipt.pod_name !== 'string' ||
      !receipt.pod_name.startsWith(prefix) || !/^chat-turn-[0-9a-f]{12}-[a-z0-9]{1,20}$/.test(receipt.pod_name) ||
      typeof receipt.sandbox_uid !== 'string' || !/^[a-z0-9-]{8,128}$/.test(receipt.sandbox_uid) ||
      receipt.image_digest !== assignment.image.split('@')[1] ||
      typeof receipt.attempt !== 'number' || !Number.isSafeInteger(receipt.attempt) || receipt.attempt < 1) {
    throw new Error('Chat supervisor recovery binding invalid');
  }
  const binding = { pod: { name: receipt.pod_name, uid: receipt.sandbox_uid }, attempt: receipt.attempt };
  if (receipt.state === 'pre_admission_cleanup') {
    if (typeof receipt.removed !== 'boolean' || 'lease_generation' in receipt) throw new Error('Chat supervisor cleanup receipt invalid');
    return { ...binding, state: 'pre_admission_cleanup' };
  }
  if (typeof receipt.lease_generation !== 'number' || !Number.isSafeInteger(receipt.lease_generation) || receipt.lease_generation < 1) {
    throw new Error('Chat supervisor recovery binding invalid');
  }
  return { ...binding, state: 'admitted',
    admission: { run_id: assignment.runId, session_id: assignment.sessionId, lease_generation: receipt.lease_generation } };
}

export async function waitForTurnCompletion(
  assignment: RegisteredAssignment,
  pod: SandboxPodIdentity,
  terminal: SandboxTerminalReceipt,
  identity: SupervisorIdentity,
  send: typeof fetch,
  load: typeof fromTokenFile,
  timing: SupervisorTiming,
): Promise<void> {
  const value = await retrySupervisorRequest(() => supervisorRequest(assignment, pod, identity, COMPLETE_PATH, send, load), true, timing);
  const receipt = value as Record<string, unknown> | null;
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
): Promise<void> {
  const resume = await restoreSupervisorTurn(assignment, identity, send, load, timing);
  if (resume?.state === 'queued_completed') return;
  if (resume?.state === 'pre_admission_cleanup') {
    try { await api.remove(resume.pod.name, resume.pod.uid); } catch {}
    await waitForSandboxRemoval(assignment, resume.pod, identity, send, load, timing);
    await waitForCleanedTurnCompletion(assignment, resume.pod, resume.attempt, identity, send, load, timing);
    return;
  }
  let launchAssignment = assignment;
  if (!resume) {
    const receipt = await supervisorRequest(assignment, null, identity, RESERVE_PATH, send, load) as Record<string, unknown> | null;
    if (!receipt || receipt.state !== 'create' || receipt.run_id !== assignment.runId || receipt.session_id !== assignment.sessionId ||
        receipt.task_id !== assignment.taskId || receipt.session_generation !== assignment.sessionGeneration ||
        receipt.pod_name !== sandboxCreationName(assignment.runId) || receipt.image_digest !== assignment.image.split('@')[1] ||
        typeof receipt.attempt !== 'number' || !Number.isSafeInteger(receipt.attempt) || receipt.attempt < 1) {
      throw new Error('Chat supervisor creation reservation invalid');
    }
    launchAssignment = { ...assignment, podName: sandboxCreationName(assignment.runId) };
  }
  await reconcileAdmittedSandbox(launchAssignment, api, identity, send, load, timing, {
    resume,
    onTerminal: async (pod, terminal) => {
      if (resume && terminal.attempt !== resume.attempt) throw new Error('Chat supervisor terminal attempt changed');
      await waitForTurnCompletion(assignment, pod, terminal, identity, send, load, timing);
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
): Promise<void> {
  const value = await retrySupervisorRequest(() => supervisorRequest(assignment, pod, identity, COMPLETE_PATH, send, load), true, timing);
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
