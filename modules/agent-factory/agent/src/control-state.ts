/**
 * In-pod control state and the bounded command journal — Issue #3960.
 *
 * This module owns the answers to two questions the dashboard asks by polling:
 * "what phase is this run's control channel in?" and "what happened to the
 * command I submitted?" It is deliberately transport-free and SDK-free — no
 * `http`, no Claude SDK import — so that every rule below is unit-testable
 * without a socket or a model, and so a later story can wire real pause/abort
 * behaviour into it without touching request handling.
 *
 * Three properties are load-bearing:
 *
 * **Bounded memory.** A run lives up to six hours and a dashboard can submit
 * commands the whole time. An unbounded journal is a slow memory leak in a
 * long-lived pod, so terminal entries are capped and time-expired. Pending
 * entries are never evicted: dropping a pending command would make an
 * outstanding instruction look like it was never submitted, and the submitter
 * would reasonably retry it (revival-design §2).
 *
 * **Idempotency keyed by command id.** The same id with the same payload returns
 * the recorded outcome without reapplying — a network retry of one abort must not
 * become two aborts. The same id with *different* content is a conflict, not a
 * silent overwrite: two different intents sharing a key is a client bug, and
 * resolving it by guessing which one wins is how a steer gets replaced by an
 * abort nobody sent.
 *
 * **`unknown` is a real answer.** An expired id, an unrecognised id, or a
 * generation change all report `unknown`. They must never report `delivered`
 * (a lie about work reaching the model) and must never trigger a replay (the
 * previous generation may already have consumed the command). This is the same
 * discipline the gateway's liveness module applies to run status: loss of
 * contact is not evidence of what happened.
 */

/** The four verbs the channel routes. Verb-agnostic by construction (ADR-9). */
export type ControlAction = 'pause' | 'resume' | 'steer' | 'abort';

/** The transient control phase of this run. Not an invocation terminal status. */
export type ControlPhase =
  | 'running'
  | 'pause_requested'
  | 'paused'
  | 'abort_requested'
  | 'terminal'
  | 'unavailable';

/**
 * The lifecycle of one submitted command.
 *
 * `delivered` means handed to the SDK at a real boundary — receipt, never
 * comprehension. The UI must not block on a model acknowledgement, so delivery
 * is deterministic and testable while the agent's pivot is best-effort.
 */
export type CommandStatus = 'pending' | 'delivered' | 'applied' | 'cancelled' | 'rejected' | 'unknown';

/** A journal entry as served in a state response. Carries no instruction text. */
export interface CommandRecord {
  command_id: string;
  action: ControlAction;
  status: CommandStatus;
  accepted_at: string | null;
  delivered_at: string | null;
  reason: string | null;
}

/** The state payload the gateway re-projects for the browser. */
export interface ControlStateSnapshot {
  generation: number;
  state: ControlPhase;
  capabilities: Record<ControlAction, boolean>;
  active_tool_count: number | null;
  updated_at: string;
  commands: CommandRecord[];
}

/** Outcome of submitting a command, mapped to an HTTP status by the listener. */
export type SubmitOutcome =
  | { kind: 'accepted'; record: CommandRecord }
  | { kind: 'replayed'; record: CommandRecord }
  | { kind: 'conflict' }
  | { kind: 'queue_full' }
  | { kind: 'unsupported' };

/**
 * Maximum pending commands. Configurable per revival-design §2 (default 10).
 * The cap exists because a paused run accumulates steers it cannot deliver until
 * resume; without a bound, a dashboard bug becomes unbounded pod memory. Over-cap
 * submissions are refused with 429 rather than silently dropped, so the submitter
 * learns the command was not accepted.
 */
export const DEFAULT_MAX_PENDING = 10;

/**
 * Maximum retained terminal entries (default 100) and their retention window
 * (default 30 minutes). Both bound the journal; whichever binds first wins. A
 * dashboard polls every 2 seconds while its modal is open, so 30 minutes is far
 * more than enough for a submitter to observe their own command's outcome, and an
 * outcome older than that is history rather than live state.
 */
export const DEFAULT_MAX_TERMINAL = 100;
export const DEFAULT_TERMINAL_RETENTION_MS = 30 * 60 * 1000;

const TERMINAL_STATUSES: ReadonlySet<CommandStatus> = new Set<CommandStatus>([
  'applied',
  'cancelled',
  'rejected',
]);

/** Internal entry: the record plus the payload fingerprint idempotency needs. */
interface JournalEntry {
  record: CommandRecord;
  /** Fingerprint of the submitted payload — what makes same-id/different-content detectable. */
  fingerprint: string;
  /** Epoch ms when the entry reached a terminal status; null while pending/delivered. */
  settledAt: number | null;
}

export interface ControlStateOptions {
  /** Run generation. A token or state response from another generation is stale. */
  generation: number;
  /** Verbs this build can actually perform. Empty in S1 — every capability false. */
  supportedActions?: ReadonlySet<ControlAction>;
  maxPending?: number;
  maxTerminal?: number;
  terminalRetentionMs?: number;
  /** Injected clock. Expiry must be testable without sleeping or touching a real run. */
  now?: () => number;
}

/**
 * The run's control state and command journal.
 *
 * Single-instance per run. Every mutation goes through a method rather than
 * exposing the map, so the bounds and the idempotency rules cannot be bypassed
 * by a caller that reaches for the underlying structure.
 */
export class ControlStateStore {
  private readonly generation: number;
  private readonly supported: ReadonlySet<ControlAction>;
  private readonly maxPending: number;
  private readonly maxTerminal: number;
  private readonly terminalRetentionMs: number;
  private readonly now: () => number;

  /** Insertion-ordered, which is what makes FIFO delivery and eviction order free. */
  private readonly journal = new Map<string, JournalEntry>();
  private phase: ControlPhase = 'running';
  private activeToolCount: number | null = null;
  private updatedAt: number;

  constructor(options: ControlStateOptions) {
    this.generation = options.generation;
    this.supported = options.supportedActions ?? new Set<ControlAction>();
    this.maxPending = options.maxPending ?? DEFAULT_MAX_PENDING;
    this.maxTerminal = options.maxTerminal ?? DEFAULT_MAX_TERMINAL;
    this.terminalRetentionMs = options.terminalRetentionMs ?? DEFAULT_TERMINAL_RETENTION_MS;
    this.now = options.now ?? (() => Date.now());
    this.updatedAt = this.now();
  }

  /** Whether a verb is supported. S1 supports none, so this is always false. */
  isSupported(action: ControlAction): boolean {
    return this.supported.has(action);
  }

  /**
   * Capabilities as served in state.
   *
   * Built from `supported` rather than from the phase, so a capability cannot
   * read true for a verb with no implementation behind it. S1 reports all four
   * false, and the dashboard therefore renders no control at all.
   */
  capabilities(): Record<ControlAction, boolean> {
    return {
      pause: this.isSupported('pause'),
      resume: this.isSupported('resume'),
      steer: this.isSupported('steer'),
      abort: this.isSupported('abort'),
    };
  }

  /**
   * Submit a command.
   *
   * Order of checks is the contract:
   * 1. unsupported verb — before any journal write, so a 501 leaves no trace of
   *    a command that was never going to run;
   * 2. known id — replay or conflict, before the queue cap, so a retry of an
   *    already-accepted command is not refused by a queue that is full *because
   *    of that same command*;
   * 3. queue cap — 429.
   */
  submit(action: ControlAction, commandId: string, fingerprint: string): SubmitOutcome {
    if (!this.isSupported(action)) {
      return { kind: 'unsupported' };
    }

    this.prune();

    const existing = this.journal.get(commandId);
    if (existing) {
      if (existing.fingerprint !== fingerprint) {
        // Same key, different intent. Refusing is the only safe answer: applying
        // the new payload would silently discard the recorded outcome of the
        // first, and applying the old one would ignore what the caller asked for.
        return { kind: 'conflict' };
      }
      // Same key, same intent — a retry. Return what already happened without
      // reapplying, so a retried abort stays one abort.
      return { kind: 'replayed', record: { ...existing.record } };
    }

    if (this.pendingCount() >= this.maxPending) {
      return { kind: 'queue_full' };
    }

    const record: CommandRecord = {
      command_id: commandId,
      action,
      status: 'pending',
      accepted_at: this.isoNow(),
      delivered_at: null,
      reason: null,
    };
    this.journal.set(commandId, { record, fingerprint, settledAt: null });
    this.touch();
    return { kind: 'accepted', record: { ...record } };
  }

  /**
   * Look up a command's status.
   *
   * An id the journal has never seen — or one already evicted — is `unknown`
   * rather than absent. The distinction matters to the UI: `unknown` says "we
   * cannot tell you what happened", which is honest, whereas a 404 would invite
   * the client to resubmit a command that may already have been consumed.
   */
  lookup(commandId: string): CommandRecord {
    this.prune();
    const entry = this.journal.get(commandId);
    if (!entry) {
      return {
        command_id: commandId,
        action: 'pause',
        status: 'unknown',
        accepted_at: null,
        delivered_at: null,
        reason: 'command is not in this generation’s journal',
      };
    }
    return { ...entry.record };
  }

  /**
   * Mark a command handed to the SDK at a real boundary.
   *
   * Returns false for an unknown id rather than creating an entry: recording a
   * delivery for a command that was never accepted would invent a command.
   */
  markDelivered(commandId: string): boolean {
    const entry = this.journal.get(commandId);
    if (!entry) return false;
    entry.record.status = 'delivered';
    entry.record.delivered_at = this.isoNow();
    this.touch();
    return true;
  }

  /**
   * Settle a command into a terminal status, starting its retention clock.
   *
   * Rejects a non-terminal status so `settle` cannot be used to move an entry
   * back to `pending`, which would make it un-evictable and re-deliverable.
   */
  settle(commandId: string, status: CommandStatus, reason?: string): boolean {
    if (!TERMINAL_STATUSES.has(status)) return false;
    const entry = this.journal.get(commandId);
    if (!entry) return false;
    entry.record.status = status;
    entry.record.reason = reason ?? entry.record.reason;
    entry.settledAt = this.now();
    this.touch();
    this.prune();
    return true;
  }

  /** Commands awaiting delivery, in submission order (FIFO — ADR-2). */
  pending(): CommandRecord[] {
    this.prune();
    return [...this.journal.values()]
      .filter((entry) => entry.record.status === 'pending')
      .map((entry) => ({ ...entry.record }));
  }

  private pendingCount(): number {
    let count = 0;
    for (const entry of this.journal.values()) {
      if (entry.record.status === 'pending') count += 1;
    }
    return count;
  }

  /** Set the control phase. Later stories drive this; S1 only ever sets terminal. */
  setPhase(phase: ControlPhase): void {
    this.phase = phase;
    this.touch();
  }

  /**
   * Record the number of tool invocations currently admitted.
   *
   * Left `null` by S1 on purpose. A reported `0` would read as "no tools are
   * running", which is a quiescence claim this story cannot substantiate — and
   * an unsubstantiated quiescence claim is exactly what the pause design forbids.
   */
  setActiveToolCount(count: number | null): void {
    this.activeToolCount = count;
    this.touch();
  }

  /** The full state payload. */
  snapshot(): ControlStateSnapshot {
    this.prune();
    return {
      generation: this.generation,
      state: this.phase,
      capabilities: this.capabilities(),
      active_tool_count: this.activeToolCount,
      updated_at: new Date(this.updatedAt).toISOString(),
      commands: [...this.journal.values()].map((entry) => ({ ...entry.record })),
    };
  }

  /**
   * Drop settled entries past their retention window or over the count cap.
   *
   * Only settled entries are eligible. The count cap evicts oldest-settled
   * first, which insertion order already gives us. Pending and delivered entries
   * are skipped entirely: a delivered-but-unsettled command is still live work,
   * and a pending one has an outstanding instruction behind it.
   */
  private prune(): void {
    const cutoff = this.now() - this.terminalRetentionMs;
    for (const [id, entry] of this.journal) {
      if (entry.settledAt !== null && entry.settledAt < cutoff) {
        this.journal.delete(id);
      }
    }

    const settled = [...this.journal.entries()].filter(([, entry]) => entry.settledAt !== null);
    const excess = settled.length - this.maxTerminal;
    for (let i = 0; i < excess; i += 1) {
      this.journal.delete(settled[i][0]);
    }
  }

  private touch(): void {
    this.updatedAt = this.now();
  }

  private isoNow(): string {
    return new Date(this.now()).toISOString();
  }
}

/**
 * Fingerprint a command payload for idempotency comparison.
 *
 * Deliberately not a hash: the payloads are small and bounded by the gateway's
 * schema, and a plain canonical string keeps a conflict debuggable without
 * reversing a digest. Keys are sorted so field order in the JSON cannot make one
 * intent look like two.
 */
export function fingerprintPayload(payload: Record<string, unknown>): string {
  const keys = Object.keys(payload).sort();
  return keys.map((key) => `${key}=${String(payload[key])}`).join(' ');
}
