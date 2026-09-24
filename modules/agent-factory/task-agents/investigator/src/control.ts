/**
 * Task-local control adapter — Task API T5 (#5798).
 *
 * ## Why these types are copied rather than imported
 *
 * The neutral contract this implements lives in
 * `modules/agent-factory/agent/src/control-runtime.ts` (issue #3962, S3), and
 * the design (section 8) directs: "Implement a task-local adapter compatible with
 * the neutral `ControlRuntimeAdapter` semantics ... Keep SDK types out of it ...
 * Reuse narrowly copied protocol/types with source provenance if importing them
 * would pull in or alter the existing agent."
 *
 * Importing would do exactly that. `control-runtime.ts` imports `ControlAction`
 * from `./control-state` inside the Claude agent package, so a single import
 * would put this package's build on that package's dependency tree — the
 * opposite of the isolation T5-AC03 requires, where legacy execution must not
 * initialize task-only dependencies and this package must not pull in theirs.
 * So the *shape* is narrowly copied here with provenance recorded above, and the
 * behavioural correspondence is asserted in `investigator.test.ts`.
 *
 * ## What v1 deliberately does not implement
 *
 * Only `input` and `cancel`. The design is explicit that "V1 does not add
 * pause/resume", so `requestPause`/`resumeFromPause` report unavailable rather
 * than pretending. A capability table is a promise to a caller: advertising a
 * pause that silently does nothing would let an operator read "Paused" as
 * "nothing is running right now" while the task kept working.
 */

import { IMPLEMENTED_CAPABILITIES, type Capability } from './protocol.js';

/**
 * ADP-owned input message.
 *
 * Copied from `control-runtime.ts` `ControlInput`. `steering` may request work;
 * `annotation` records context without starting a turn.
 */
export interface ControlInput {
  kind: 'steering' | 'annotation';
  /** Untrusted caller text. Trust-boundary handling happens before this point. */
  text: string;
  /** Command id from the durable journal, when the input came from a command. */
  command_id?: string;
}

/**
 * Outcome of physically handing input to the agent.
 *
 * Copied from `control-runtime.ts` `InputHandoffResult`. `unknown` is the honest
 * answer for an ambiguous handoff and is never upgraded by inference, because
 * claiming `delivered` lies and claiming `rejected` invites a replay of an
 * instruction that may already have been consumed.
 */
export type InputHandoffResult = 'delivered' | 'rejected' | 'unknown';

/** Copied from `control-runtime.ts` `PauseResult`, minus the states v1 cannot reach. */
export type PauseResult = { outcome: 'unavailable'; reason: string };

/** Copied from `control-runtime.ts` `MAX_REASON_LENGTH`. */
export const MAX_REASON_LENGTH = 200;

/** Copied from `control-runtime.ts` `boundReason`. */
export function boundReason(reason: string): string {
  const collapsed = reason.replace(/\s+/g, ' ').trim();
  return collapsed.length <= MAX_REASON_LENGTH
    ? collapsed
    : `${collapsed.slice(0, MAX_REASON_LENGTH - 1)}…`;
}

/**
 * Typed intentional cancellation.
 *
 * Copied from `control-runtime.ts` `ControlCancelledError`, including the
 * structural marker, and for the same load-bearing reason: retry wrappers
 * classify failures by matching error text for words like `aborted` and
 * `timeout`. A cancellation travelling as an ordinary `Error` would match those
 * patterns and be retried — so a deliberate stop would start a fresh attempt,
 * which is the opposite of cancelling. The design states this directly:
 * "Intentional cancellation must never enter the generic retry path or start
 * another attempt."
 */
export class ControlCancelledError extends Error {
  /** Structural marker: survives a module-boundary identity mismatch. */
  readonly isControlCancellation = true as const;

  /** The command this cancellation is attributable to, when there is one. */
  readonly commandId?: string;

  constructor(reason = 'control runtime cancelled', commandId?: string) {
    super(boundReason(reason));
    this.name = 'ControlCancelledError';
    if (commandId !== undefined) {
      this.commandId = commandId;
    }
  }
}

/**
 * Recognize a cancellation without relying on `instanceof` across realms.
 *
 * Copied from `control-runtime.ts` `isControlCancellation`.
 */
export function isControlCancellation(err: unknown): boolean {
  return (
    err instanceof ControlCancelledError ||
    (typeof err === 'object' &&
      err !== null &&
      (err as { isControlCancellation?: unknown }).isControlCancellation === true)
  );
}

/** A follow-up input queued for the agent, with its consumption state. */
interface QueuedInput {
  readonly input: ControlInput;
  consumed: boolean;
}

/**
 * The task-local control surface.
 *
 * Deliberately narrower than `ControlRuntimeAdapter`: there is no attempt
 * registry here, because a task child process *is* a single attempt. The host
 * owns retries and generation fencing, so replicating the registry in the child
 * would be a second, quietly diverging copy of a decision already made
 * elsewhere.
 */
export class TaskControlAdapter {
  private admissionClosed = false;

  private cancelled: ControlCancelledError | null = null;

  /** Insertion-ordered; consumption is by command id, and once only. */
  private readonly queue: QueuedInput[] = [];

  /** Command ids ever admitted, so a replayed command is not queued twice. */
  private readonly seenCommandIds = new Set<string>();

  /**
   * Capabilities this adapter can prove.
   *
   * Returns exactly the protocol's implemented set. Kept in lockstep with the
   * `ready` frame rather than duplicated, so the frame and this table cannot
   * disagree about what the agent will honour.
   */
  capabilities(): readonly Capability[] {
    return IMPLEMENTED_CAPABILITIES;
  }

  /** v1 implements no pause barrier, and says so instead of no-op succeeding. */
  requestPause(): PauseResult {
    return {
      outcome: 'unavailable',
      reason: boundReason('task API v1 does not implement pause for task agents'),
    };
  }

  /** Symmetric with {@link requestPause}: nothing to release. */
  resumeFromPause(): PauseResult {
    return this.requestPause();
  }

  /**
   * Admit a follow-up input.
   *
   * Returns `rejected` for a replayed command id rather than queueing it again.
   * The design's consumption boundary is that "one command is appended once to
   * the canonical conversation and included once in its assigned logical turn" —
   * so a command arriving twice (a host retry, a redelivered message) must be
   * admitted once, and the second arrival is not a new instruction.
   */
  admit(input: ControlInput): InputHandoffResult {
    if (this.cancelled !== null || this.admissionClosed) {
      // Cancellation latches and closes admission (design section 8). Accepting
      // input after it would let work resume inside a cancelled task.
      return 'rejected';
    }
    if (input.command_id !== undefined) {
      if (this.seenCommandIds.has(input.command_id)) {
        return 'rejected';
      }
      this.seenCommandIds.add(input.command_id);
    }
    this.queue.push({ input, consumed: false });
    return 'delivered';
  }

  /**
   * Take every input not yet consumed, marking each consumed as it is handed out.
   *
   * Marking at handout is what makes consumption once-only: a second call returns
   * nothing for the same command even if the caller loops, so an input cannot be
   * folded into two different turns.
   */
  takeUnconsumed(): ControlInput[] {
    const pending: ControlInput[] = [];
    for (const entry of this.queue) {
      if (!entry.consumed) {
        entry.consumed = true;
        pending.push(entry.input);
      }
    }
    return pending;
  }

  /** Fence input admission synchronously when the final report is committed. */
  closeAdmission(): void {
    this.admissionClosed = true;
  }

  /** Whether any admitted input is still awaiting consumption. */
  hasUnconsumedInput(): boolean {
    return this.queue.some((entry) => !entry.consumed);
  }

  /** Every command id consumed so far, for the settlement record. */
  consumedCommandIds(): string[] {
    return this.queue
      .filter((entry) => entry.consumed && entry.input.command_id !== undefined)
      .map((entry) => entry.input.command_id as string);
  }

  /**
   * Latch intentional cancellation.
   *
   * Idempotent: the first cancellation wins, so a repeated cancel does not
   * rewrite the reason an operator will eventually read.
   */
  cancel(reason?: string, commandId?: string): void {
    if (this.cancelled === null) {
      this.cancelled = new ControlCancelledError(
        reason ?? 'cancellation requested by the task owner',
        commandId,
      );
    }
  }

  /** True once cancellation has latched. */
  isCancelled(): boolean {
    return this.cancelled !== null;
  }

  /** The latched cancellation, if any. */
  cancellation(): ControlCancelledError | null {
    return this.cancelled;
  }

  /**
   * Throw if cancellation has latched.
   *
   * Called at stage boundaries rather than mid-stage: a stage is the smallest
   * unit whose partial state is coherent enough to report honestly.
   */
  throwIfCancelled(): void {
    if (this.cancelled !== null) {
      throw this.cancelled;
    }
  }
}
