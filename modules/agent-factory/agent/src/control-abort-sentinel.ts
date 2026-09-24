/**
 * The abort sentinel: how an aborting run tells the finalizing half what happened
 * — Issue #3963 (S4).
 *
 * A hosted run is two cooperating processes. This one (Node) holds the model
 * conversation, the control listener and the cancellation signal. The other
 * (`entrypoint.py`) owns the closing comment, the invocation status, the check-run
 * conclusion and the queue acknowledgement. Only the Python half can write the
 * invocation row, because only it holds both halves of that row's key
 * (`event_id` AND `arrived_at`); Node is given `ADP_MESSAGE_ID` alone. So an
 * abort decided here has to *travel* to there, and this file is that channel.
 *
 * ## Why a separate file rather than the existing metadata bridge
 *
 * `/tmp/adp-result-metadata.json` already carries Node→Python facts (session id,
 * cost/turns, the spend-cap stop). Abort deliberately does not ride in it, for
 * three reasons that are properties of abort specifically:
 *
 * **It must be atomic.** The metadata bridge is a read-merge-write, which has a
 * window where the file on disk is truncated or half-written. Python reads the
 * abort signal exactly once, during teardown, and a torn read there means an
 * aborted run silently records itself as `failed`. This file is written aside and
 * `rename`d into place, which is atomic within a filesystem, so a reader sees
 * either the whole previous state or the whole new one and never a fragment.
 *
 * **It must be bound to this run and this attempt.** The metadata bridge is
 * keyed by nothing: whatever is in `/tmp` is assumed to belong to the current
 * run. That assumption is fine for cost/turns and fatal for abort, because a
 * stale sentinel is not a harmless wrong number — it is an unrelated run
 * reporting itself deliberately stopped, with its queue message deleted and its
 * controls revoked. So the payload names the run and the control generation it
 * was written for, and the reader rejects anything that does not match.
 *
 * **Its failure mode must be one-directional.** A missing, malformed, truncated,
 * stale or future-versioned sentinel all mean *"no abort happened"* — never
 * "abort happened", and never a raised error. Losing an abort signal costs a
 * mislabelled outcome that an operator can see and re-issue; fabricating one
 * deletes a live run's queue message. Those are not symmetric, so the validation
 * below is deliberately strict and every rejection path returns the same
 * "no abort" answer.
 *
 * ## Why this is not in `agent-worker.ts`
 *
 * `agent-worker.ts` calls `main()` at module load and exports nothing, so a test
 * cannot import it — its sibling suites assert against its source *text*. The
 * sentinel's validation rules are exactly the part that needs real unit tests
 * (five distinct rejection cases), so they live here, in a module with no SDK
 * import and no side effects at load. This mirrors why #3961 split
 * `control-command-apply.ts` out of the same file.
 *
 * Imports are `fs` only, deliberately — the same leaf-module discipline as
 * `lib/tokenFile.ts`. A module the jest suites can import must not reach the SDK
 * transitively.
 */
import { closeSync, fsyncSync, openSync, renameSync, readFileSync, unlinkSync, writeSync } from 'fs';
import { randomUUID } from 'crypto';

/**
 * Schema version of the sentinel payload.
 *
 * Read as an exact match, not a minimum. A *newer* writer's payload is rejected
 * by an older reader rather than partially understood: the fields this reader
 * validates are the run binding and the generation, so "understand what I can and
 * ignore the rest" is precisely how a future field that narrows the abort's scope
 * would be silently dropped. The two halves ship in one image, so an exact match
 * is the normal state and a mismatch means something is genuinely wrong.
 */
export const ABORT_SENTINEL_VERSION = 1;

/** Default sentinel location. Mirrors the sibling `/tmp` bridges. */
export const ABORT_SENTINEL_PATH = '/tmp/adp-abort-sentinel.json';

/**
 * Bound on the reason text.
 *
 * The reason is operator-supplied and ends up in a GitHub comment, so it is
 * length-capped at the boundary rather than trusted. Matches the control
 * contract's `MAX_REASON_LENGTH` so a reason that survived the listener's
 * bounding is not re-bounded to a different size here.
 */
export const MAX_SENTINEL_REASON_LENGTH = 200;

/** The persisted payload. Carries no credential and no native session handle. */
export interface AbortSentinel {
  version: number;
  /** The run this abort belongs to — `ADP_CONTROL_RUN_ID`. */
  run_id: string;
  /** The control generation that accepted the abort. */
  generation: number;
  /** Journal id of the abort command, for correlating evidence. */
  command_id: string;
  /** When the abort was requested, ISO-8601. */
  requested_at: string;
  /** Bounded operator-facing reason, or null when none was supplied. */
  reason: string | null;
}

/** What the reader needs to prove a sentinel belongs to the current run. */
export interface AbortSentinelBinding {
  runId: string;
  generation: number;
}

/**
 * Write the sentinel atomically.
 *
 * Returns `true` only when the payload is durably in place under its final name.
 * The caller must not report a successful abort on `false`: a sentinel that did
 * not land means the Python half will classify this run by its exit code, so
 * claiming an abort would be claiming an outcome that was never recorded.
 *
 * Atomicity is `write to a unique temp name in the same directory, then rename`
 * — the `lib/tokenFile.ts` pattern. Same directory matters: `rename` is only
 * atomic within a filesystem, and `/tmp` may be a different mount from anywhere
 * else. The temp name carries a UUID rather than the pid, because a pid is
 * reusable and `wx` would then fail against a stale leftover.
 *
 * The content is `fsync`ed before the rename, matching
 * `lib/control_renewal.py::_write` for the same document family. Buffered-and-
 * renamed is atomic with respect to *readers* but not with respect to the pod
 * dying, and the reader here runs after this process has exited — precisely the
 * window where an unflushed write is lost.
 */
export function writeAbortSentinel(
  input: {
    binding: AbortSentinelBinding;
    commandId: string;
    reason?: string | null;
    requestedAt?: string;
  },
  options: { sentinelPath?: string; log?: (level: string, message: string) => void } = {},
): boolean {
  const sentinelPath = options.sentinelPath ?? ABORT_SENTINEL_PATH;
  const log = options.log ?? (() => {});

  const payload: AbortSentinel = {
    version: ABORT_SENTINEL_VERSION,
    run_id: input.binding.runId,
    generation: input.binding.generation,
    command_id: input.commandId,
    requested_at: input.requestedAt ?? new Date().toISOString(),
    reason: boundSentinelReason(input.reason),
  };

  // An unbound sentinel is worse than none: the reader's run/generation check is
  // the only thing standing between a leftover file and an unrelated run
  // reporting itself aborted, and a blank binding can never satisfy it. Refusing
  // to write is the honest outcome — the abort still stops the run, it just does
  // not get to claim the aborted outcome.
  if (!payload.run_id || !Number.isInteger(payload.generation) || payload.generation < 1) {
    log('ERROR', 'abort sentinel not written: run identity or generation is missing');
    return false;
  }

  const tempPath = `${sentinelPath}.${randomUUID()}.tmp`;
  try {
    // 'wx' fails rather than truncating if the name somehow exists, so a
    // colliding write is an error instead of two writers interleaving.
    const handle = openSync(tempPath, 'wx', 0o600);
    try {
      writeSync(handle, JSON.stringify(payload));
      fsyncSync(handle);
    } finally {
      closeSync(handle);
    }
    renameSync(tempPath, sentinelPath);
    return true;
  } catch (err) {
    log('ERROR', `abort sentinel write failed: ${(err as Error).message}`);
    return false;
  } finally {
    // The temp name is never read, so cleanup failure is uninteresting. After a
    // successful rename there is nothing at this path and the unlink is a no-op.
    try { unlinkSync(tempPath); } catch { /* renamed, or never created */ }
  }
}

/**
 * Read and validate the sentinel.
 *
 * Returns the payload only when it is well-formed, version-exact and bound to
 * the supplied run and generation. Every other case — absent, unreadable,
 * unparseable, wrong shape, wrong version, different run, superseded generation
 * — returns `null`, which callers must treat as "this run was not aborted".
 *
 * Generation is compared for *equality*, not "at least". A sentinel from a
 * superseded generation is a stale file from a previous control registration,
 * and honouring it would let an abort aimed at an attempt that has already ended
 * finalize the attempt that replaced it.
 */
export function readAbortSentinel(
  binding: AbortSentinelBinding,
  options: { sentinelPath?: string } = {},
): AbortSentinel | null {
  const sentinelPath = options.sentinelPath ?? ABORT_SENTINEL_PATH;
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(sentinelPath, 'utf8'));
  } catch {
    // Absent is the overwhelmingly common case (no abort was requested) and is
    // not worth distinguishing from corrupt here: both mean "no abort".
    return null;
  }
  return validateAbortSentinel(parsed, binding);
}

/**
 * The validation rules, separated from the I/O so they can be tested directly
 * and so the Python reader has one unambiguous specification to mirror.
 */
export function validateAbortSentinel(
  parsed: unknown,
  binding: AbortSentinelBinding,
): AbortSentinel | null {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
  const candidate = parsed as Record<string, unknown>;

  if (candidate.version !== ABORT_SENTINEL_VERSION) return null;

  const runId = candidate.run_id;
  const generation = candidate.generation;
  const commandId = candidate.command_id;
  const requestedAt = candidate.requested_at;

  if (typeof runId !== 'string' || !runId) return null;
  if (typeof generation !== 'number' || !Number.isInteger(generation)) return null;
  if (typeof commandId !== 'string' || !commandId) return null;
  if (typeof requestedAt !== 'string' || !requestedAt) return null;

  // The run binding. Both must match: the run id alone would accept a sentinel
  // from a superseded attempt of the same run, and the generation alone would
  // accept one from a different run that happened to share a generation number.
  if (runId !== binding.runId) return null;
  if (generation !== binding.generation) return null;

  const reason = candidate.reason;
  return {
    version: ABORT_SENTINEL_VERSION,
    run_id: runId,
    generation,
    command_id: commandId,
    requested_at: requestedAt,
    reason: typeof reason === 'string' ? boundSentinelReason(reason) : null,
  };
}

/** Collapse whitespace and truncate. `null`/empty become `null`, not `""`. */
export function boundSentinelReason(reason: string | null | undefined): string | null {
  if (typeof reason !== 'string') return null;
  const collapsed = reason.replace(/\s+/g, ' ').trim();
  if (!collapsed) return null;
  return collapsed.length <= MAX_SENTINEL_REASON_LENGTH
    ? collapsed
    : `${collapsed.slice(0, MAX_SENTINEL_REASON_LENGTH - 1)}…`;
}

/**
 * Resolve the run binding from the process environment.
 *
 * These are the same variables the control listener is configured from, placed
 * there by `entrypoint.py` only when the control flag is on and registration
 * succeeded — so a run with no control channel yields no binding, and
 * `writeAbortSentinel` refuses rather than writing an unbindable file.
 *
 * Returns `null` rather than a partial binding: a binding with a blank run id
 * cannot be validated against, and inventing a default here would defeat the
 * staleness check that is the sentinel's whole safety property.
 */
export function abortSentinelBindingFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): AbortSentinelBinding | null {
  const runId = (env.ADP_CONTROL_RUN_ID || '').trim();
  const generation = Number.parseInt(env.ADP_CONTROL_GENERATION || '', 10);
  if (!runId || !Number.isInteger(generation) || generation < 1) return null;
  return { runId, generation };
}

/**
 * Remove a sentinel. Used by tests and by nothing on the run path.
 *
 * Deliberately not called during teardown: the sentinel must survive until the
 * Python half has read it, and that read happens after this process has exited.
 * `/tmp` is pod-local and the pod is torn down with the run, so there is no
 * lifetime to manage beyond the process.
 */
export function clearAbortSentinel(options: { sentinelPath?: string } = {}): void {
  try { unlinkSync(options.sentinelPath ?? ABORT_SENTINEL_PATH); }
  catch { /* already absent */ }
}
