/**
 * API client for the live run-control channel — Issue #3966 (S7).
 *
 * Every type here is derived from the backend's declared contract in
 * `modules/gateway/src/activity/control_schemas.py`, not invented for the UI.
 * That direction matters: the panel's honesty depends on rendering exactly the
 * states the server can actually report, and a frontend-only shape would let
 * the two drift until the UI is confidently describing a state the server never
 * sends. When the contract gains a state, it is added there first and mirrored
 * here — never the other way round (revival-design §2, §6).
 *
 * The browser talks only to the gateway. The pod's address, port and per-run
 * bearer token are deliberately absent from every type in this file, because
 * they are absent from the response models the gateway serves; a field here
 * that could hold one is a field a later refactor could populate (FR-7.3).
 */

import { apiClient } from './api';
import type { ApiError } from '@/types/api';

// ---------------------------------------------------------------------------
// Contract types — mirror of control_schemas.py
// ---------------------------------------------------------------------------

/** The four verbs the control channel routes. `ControlAction` in the backend. */
export type ControlAction = 'pause' | 'resume' | 'steer' | 'abort';

/**
 * The transient control phase of a run.
 *
 * `unavailable` is NOT an error: it is the honest answer when the run exists but
 * carries no reachable control registration — the ordinary case for every run
 * started before this feature, or with the flag off.
 *
 * This is a *control phase*, deliberately not part of the invocation terminal
 * status vocabulary in `utils/status.ts`. The two are read by different
 * consumers and are not unified.
 */
export type ControlState =
  | 'running'
  | 'pause_requested'
  | 'paused'
  | 'abort_requested'
  | 'terminal'
  | 'unavailable';

/**
 * The lifecycle of one submitted command, keyed by its `command_id`.
 *
 * `delivered` means the worker handed the command to the agent at a real
 * boundary. It does NOT mean the model understood or acted on it — the UI must
 * never upgrade `delivered` to "applied" or claim comprehension (FR-7.12).
 *
 * `unknown` is the required answer for an expired ID, an unrecognised ID, or a
 * worker-generation change. Rendering such a command as `delivered` would be a
 * lie, and it is the specific failure this story exists to prevent.
 */
export type CommandStatus =
  | 'pending'
  | 'delivered'
  | 'applied'
  | 'cancelled'
  | 'rejected'
  | 'unknown';

/** Which verbs this deployment can actually perform right now. All default false. */
export interface ControlCapabilities {
  pause: boolean;
  resume: boolean;
  steer: boolean;
  abort: boolean;
}

/** One entry in the bounded acknowledgement journal. Carries no instruction text. */
export interface CommandAcknowledgement {
  command_id: string;
  action: ControlAction;
  status: CommandStatus;
  accepted_at?: string | null;
  delivered_at?: string | null;
  reason?: string | null;
}

/**
 * The polled read contract (`GET .../agent/state`).
 *
 * `generation` is what makes a stale poll detectable: a response from a
 * different worker generation describes a process that no longer exists, and
 * must not be attributed to the current one.
 *
 * `active_tool_count` is `null` when the worker cannot observe tool admission.
 * The UI must render that as "unknown", never as zero — an unproven zero reads
 * as "no tools running", which is the false quiescence claim revival-design §4
 * forbids.
 */
export interface ControlStateResponse {
  run_id: string;
  generation?: number | null;
  available: boolean;
  reason?: string | null;
  capabilities: ControlCapabilities;
  state: ControlState;
  active_tool_count?: number | null;
  updated_at?: string | null;
  commands: CommandAcknowledgement[];
}

/**
 * The result of submitting one command.
 *
 * The HTTP status carries meaning the body cannot: 202 is accepted and pending,
 * 200 is already applied or applied synchronously. Neither means the model
 * comprehended anything (FR-7.10).
 */
export interface ControlCommandResponse {
  run_id: string;
  action: ControlAction;
  state: ControlState;
  command_id: string;
  command_status: CommandStatus;
}

/** Bounds on client-supplied text, mirrored from the backend schema. */
export const MAX_INSTRUCTION_CHARS = 4000;
export const MAX_REASON_CHARS = 1000;

/**
 * All-false capabilities — the fail-closed default.
 *
 * Used whenever capabilities are absent or malformed. A default that offered a
 * verb would advertise a button whose handler answers 501, and an operator who
 * believes a run is pausing stops watching it.
 */
export const NO_CAPABILITIES: ControlCapabilities = {
  pause: false,
  resume: false,
  steer: false,
  abort: false,
};

/**
 * The state a run is assumed to be in when we have no trustworthy answer.
 *
 * Fail-closed in every field: unavailable, no capabilities, no commands. This is
 * what a malformed response normalises to, so a garbled payload cannot enable a
 * control.
 */
export const UNAVAILABLE_STATE: ControlStateResponse = {
  run_id: '',
  generation: null,
  available: false,
  reason: null,
  capabilities: NO_CAPABILITIES,
  state: 'unavailable',
  active_tool_count: null,
  updated_at: null,
  commands: [],
};

const VALID_STATES: readonly ControlState[] = [
  'running',
  'pause_requested',
  'paused',
  'abort_requested',
  'terminal',
  'unavailable',
];

const VALID_COMMAND_STATUSES: readonly CommandStatus[] = [
  'pending',
  'delivered',
  'applied',
  'cancelled',
  'rejected',
  'unknown',
];

const VALID_ACTIONS: readonly ControlAction[] = ['pause', 'resume', 'steer', 'abort'];

// ---------------------------------------------------------------------------
// Normalisation
// ---------------------------------------------------------------------------

/**
 * Re-project an untrusted payload onto the contract, field by field.
 *
 * The gateway already does this against the pod; we do it again against the
 * gateway for a different reason: the panel derives "may this button exist" from
 * these fields, so a missing or wrong-typed field must resolve to the safe
 * answer rather than to `undefined` flowing into a boolean test. An unrecognised
 * state becomes `unavailable` and an unrecognised command status becomes
 * `unknown` — in both cases the honest "we cannot account for this".
 */
export function normalizeControlState(
  payload: unknown,
  fallbackRunId = '',
): ControlStateResponse {
  if (payload === null || typeof payload !== 'object') {
    return { ...UNAVAILABLE_STATE, run_id: fallbackRunId };
  }
  const raw = payload as Record<string, unknown>;

  const rawCaps =
    raw.capabilities !== null && typeof raw.capabilities === 'object'
      ? (raw.capabilities as Record<string, unknown>)
      : {};

  const state = VALID_STATES.includes(raw.state as ControlState)
    ? (raw.state as ControlState)
    : 'unavailable';

  const commands: CommandAcknowledgement[] = Array.isArray(raw.commands)
    ? raw.commands.flatMap((entry) => {
        if (entry === null || typeof entry !== 'object') return [];
        const row = entry as Record<string, unknown>;
        const commandId = row.command_id;
        if (typeof commandId !== 'string' || !commandId) return [];
        if (!VALID_ACTIONS.includes(row.action as ControlAction)) return [];
        return [
          {
            command_id: commandId,
            action: row.action as ControlAction,
            // An unrecognised status is reported as `unknown`, never dropped:
            // silently omitting the entry would make a command the operator
            // submitted disappear from the journal entirely.
            status: VALID_COMMAND_STATUSES.includes(row.status as CommandStatus)
              ? (row.status as CommandStatus)
              : 'unknown',
            accepted_at: typeof row.accepted_at === 'string' ? row.accepted_at : null,
            delivered_at: typeof row.delivered_at === 'string' ? row.delivered_at : null,
            reason: typeof row.reason === 'string' ? row.reason : null,
          },
        ];
      })
    : [];

  return {
    run_id: typeof raw.run_id === 'string' && raw.run_id ? raw.run_id : fallbackRunId,
    generation: typeof raw.generation === 'number' ? raw.generation : null,
    // Strictly `=== true`: a truthy non-boolean must not enable controls.
    available: raw.available === true,
    reason: typeof raw.reason === 'string' ? raw.reason : null,
    capabilities: {
      pause: rawCaps.pause === true,
      resume: rawCaps.resume === true,
      steer: rawCaps.steer === true,
      abort: rawCaps.abort === true,
    },
    state,
    // `null` unless a real number: this is the difference between "no tools
    // running" and "we cannot see whether tools are running".
    active_tool_count: typeof raw.active_tool_count === 'number' ? raw.active_tool_count : null,
    updated_at: typeof raw.updated_at === 'string' ? raw.updated_at : null,
    commands,
  };
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/**
 * A control request that failed, carrying the status the copy is chosen from.
 *
 * `apiClient` throws the parsed error body with `.status` attached. We wrap it
 * so callers get one shape with a guaranteed status rather than having to
 * re-derive it, and so `status === 0` distinguishes "no gateway response confirmed"
 * (offline, DNS, abort, or a lost response) from any answer the gateway gave.
 */
export interface ControlRequestError {
  status: number;
  message: string;
  detail?: string;
}

function toControlError(error: unknown): ControlRequestError {
  const body = (error ?? {}) as ApiError & { detail?: string };
  const status = typeof body.status === 'number' ? body.status : 0;
  const detail = typeof body.detail === 'string' ? body.detail : undefined;
  return {
    status,
    message: describeControlError(status, detail ?? body.message),
    detail,
  };
}

/**
 * Turn a status into the one true sentence for it.
 *
 * Each of these is a different instruction to the reader — retry, sign in,
 * shorten the text, stop and reload — so collapsing them into one generic
 * banner would leave them unable to act. 410 and 409 are the two most important
 * to keep distinct from success: both mean the command was NOT applied.
 */
export function describeControlError(status: number, detail?: string): string {
  switch (status) {
    case 400:
      return 'The request was rejected as invalid. Reload the page and try again.';
    case 401:
      // Retained for completeness and for the unit test, but rarely seen: the
      // shared `apiClient` clears tokens and navigates to /login on any 401
      // before this string can render. Kept distinct rather than folded into the
      // default so a future client that stops redirecting still says the true
      // thing here.
      return 'Your session has expired. Sign in again to control this run.';
    case 403:
      return 'You are not allowed to control this run.';
    case 404:
      // Deliberately fused with "not yours": the backend answers unknown,
      // other-tenant and not-owned identically so a caller learns nothing from
      // the difference, and the UI must not imply it knows which it was.
      return 'This run was not found, or it is not yours to control.';
    case 409:
      return (
        detail ||
        'This run cannot accept that command right now. Its live state may have changed.'
      );
    case 410:
      return 'This run has already finished. The command was not applied.';
    case 413:
      return `That instruction is too long. Keep it under ${MAX_INSTRUCTION_CHARS} characters.`;
    case 429:
      return 'Too many commands are already queued for this run. Wait for them to be handled, then retry.';
    case 501:
      return 'This deployment cannot perform that action yet.';
    case 503:
      return 'Live run controls are switched off in this deployment.';
    case 502:
      return 'The run could not be reached. It may have stopped reporting.';
    case 0:
      return 'The response was lost. The command outcome is unknown; check its status before sending it again.';
    default:
      return 'The command outcome could not be confirmed. Check its status before sending it again.';
  }
}

// ---------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------

/**
 * `encodeURIComponent` on the path segment.
 *
 * The invocation ID reaches us from a URL query parameter, so it is untrusted
 * input being spliced into a request path.
 */
function runPath(invocationId: string): string {
  return `/activity/invocations/${encodeURIComponent(invocationId)}/agent`;
}

/** Read current control capabilities, phase and bounded command history. */
export async function getControlState(
  invocationId: string,
  signal?: AbortSignal,
): Promise<ControlStateResponse> {
  try {
    const response = await apiClient.get<unknown>(`${runPath(invocationId)}/state`, signal);
    return normalizeControlState(response, invocationId);
  } catch (error) {
    throw toControlError(error);
  }
}

/**
 * Submit pause, resume or abort.
 *
 * `commandId` is supplied by the caller and is the idempotency key: the same ID
 * with the same payload returns the recorded outcome without reapplying. The
 * client is the only party that can tell a retry of one intent from two
 * separate intents — a server-minted ID would make a retried abort look like a
 * second abort.
 */
export async function sendControlCommand(
  invocationId: string,
  action: Exclude<ControlAction, 'steer'>,
  commandId: string,
  reason?: string,
): Promise<ControlCommandResponse> {
  const body: { command_id: string; reason?: string } = { command_id: commandId };
  if (reason) body.reason = reason;
  try {
    return await apiClient.post<ControlCommandResponse>(
      `${runPath(invocationId)}/${action}`,
      body,
    );
  } catch (error) {
    throw toControlError(error);
  }
}

/** Submit a steering instruction — the one verb carrying free text. */
export async function sendSteerCommand(
  invocationId: string,
  commandId: string,
  instruction: string,
): Promise<ControlCommandResponse> {
  try {
    return await apiClient.post<ControlCommandResponse>(`${runPath(invocationId)}/steer`, {
      command_id: commandId,
      instruction,
    });
  } catch (error) {
    throw toControlError(error);
  }
}

/**
 * Mint a command ID.
 *
 * UUID format is required by the backend schema so the key space is
 * collision-free without coordination. `crypto.randomUUID` is unavailable on
 * insecure origins and in some older test environments, hence the fallback.
 */
export function newCommandId(): string {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID();
  }
  // RFC 4122 version-4 layout, built from getRandomValues where available.
  const bytes = new Uint8Array(16);
  if (cryptoObj && typeof cryptoObj.getRandomValues === 'function') {
    cryptoObj.getRandomValues(bytes);
  } else {
    for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
