/**
 * Live steering: the run-level delivery pump — Issue #3965 (S6).
 *
 * This module is the missing link between a *received* steering command and an
 * instruction the model can actually read. Everything around it already existed
 * before this story:
 *
 * - the bounded run-level FIFO, command-id idempotency and the acknowledgement
 *   ledger live in `ControlStateStore` (#3960). There is no second queue here;
 *   ordering, the pending cap, replay and conflict detection are all read back
 *   out of that journal, which is why a steer cannot be ordered differently by
 *   two components that both believe they own the queue;
 * - the neutral input transport and its retry-safety rules live in
 *   `CurrentAttemptRegistry` / `ControlRuntimeAdapter.submitInput` (#3962);
 * - the authority re-check immediately before a physical handoff lives in
 *   `ControlStateStore.deliverAuthorized` (#5028/#5029);
 * - the untrusted-text envelope lives in `utils/trust-boundary`.
 *
 * What was missing is the thing this file is: something that waits. A steering
 * command can arrive at any instant, and almost none of those instants are a
 * moment the harness can take input — the SDK may be mid-tool, the run may be
 * paused by an operator, or the attempt may have just been retried away. The
 * transport is deliberately refusing to buffer (see
 * `ClaudeAttemptEndpoint.deliver`: a buffer with its own async gap would be a
 * hidden queue that outlives an authorization), so without a pump a command
 * submitted mid-tool would simply be refused.
 *
 * Three properties are load-bearing, and each is the inverse of a specific lie:
 *
 * **Acknowledgement at handoff, never at enqueue.** `delivered` is written by
 * `deliverAuthorized` in the same synchronous turn as the physical push. An
 * operator who submits during a twenty-minute `Bash` sees `pending` for twenty
 * minutes, which is the truth. Marking `delivered` on acceptance would be the
 * easy lie, and the one the dashboard would repeat.
 *
 * **`delivered` is not comprehension.** The terminal status this pump writes
 * carries a reason that says so explicitly. Nothing here waits for, infers, or
 * reports that the model acted on the instruction — the run's own output is the
 * only evidence of that, and it is not this module's to interpret.
 *
 * **No replay, ever.** A command is handed to the transport at most once,
 * structurally: `deliverAuthorized` only accepts a `pending` entry and flips it
 * before calling the handoff. An ambiguous outcome is recorded as `unknown` and
 * settled, not retried. Only commands still `pending` are re-driven after an
 * in-process retry, and they are re-driven by being *left alone* — the journal
 * still holds them, so the next attempt's boundary picks them up with no
 * reattachment step that could double-deliver.
 */
import { DEFAULT_MAX_PENDING, type CommandRecord, type ControlStateStore } from './control-state';
import type { ControlInput, InputHandoffResult } from './control-runtime';
import type { ControlOrigin } from './control-authorization';
import { wrapUntrusted } from './utils/trust-boundary';

/**
 * Instruction bound, mirroring the listener's and the gateway's schema.
 *
 * Re-checked here rather than trusted from the caller: this is the last point
 * before operator text is composed into a prompt, and a bound enforced only at
 * the wire would be bypassed by any future in-process submitter.
 */
export const MAX_STEER_INSTRUCTION_CHARS = 4000;

/**
 * Instructions this pump will hold text for.
 *
 * The journal's pending cap (`DEFAULT_MAX_PENDING`) is the real backpressure —
 * an eleventh submission is refused with 429 before it ever reaches here. This
 * is the defensive ceiling on the text sidecar, so a future caller that bypasses
 * `submit` cannot grow this map without bound.
 */
export const MAX_QUEUED_INSTRUCTIONS = DEFAULT_MAX_PENDING;

/** What became of one steering command, from this pump's point of view. */
export type SteerOutcome = 'delivered' | 'rejected' | 'unknown' | 'cancelled';

/**
 * Journal reason recorded on a successful handoff.
 *
 * Worded to keep the three facts apart. The command was applied in the only
 * sense ADP can prove — the instruction was physically accepted by the harness
 * transport — and the sentence refuses to imply the second and third.
 */
export const STEER_DELIVERED_REASON =
  'instruction handed to the agent runtime; this is receipt, not proof the model acted on it';

/** Stable prefix for the live-comment marker, so consumers can match on it. */
export const STEER_MARKER_PREFIX = 'control: steer';

/**
 * The deterministic live-comment marker for one steering outcome.
 *
 * Deterministic and machine-greppable on purpose: the Wave 3 live evaluation
 * asserts a command-id handoff trace from outside the pod, and a prose line that
 * varied per run would make that assertion impossible. It is appended when the
 * outcome is known — i.e. after handoff — never when the command is accepted.
 * Anchoring it to submission would make a long tool call look like a broken
 * marker.
 *
 * Carries the command id and nothing else. The instruction text is deliberately
 * absent: the live comment is a public artifact on an issue, and echoing
 * operator text there would republish it outside the control channel.
 */
export function steerMarker(commandId: string, outcome: SteerOutcome): string {
  return `${STEER_MARKER_PREFIX} ${outcome} command_id=${commandId}`;
}

/**
 * Compose the prompt text for one steering instruction.
 *
 * Two things are happening, and they are not the same thing. The ADP-owned
 * framing sentence tells the model that a human operator changed the ask
 * mid-run, which is what makes steering steering rather than a stray message.
 * The operator's own words then go through `wrapUntrusted`, because they are
 * untrusted text crossing into a prompt: the envelope is what stops an
 * instruction from carrying its own orders — "ignore your previous
 * instructions", a shell command, a new identity — while still asking the model
 * to act on the *intent* of what it contains (trust-boundary rule 5).
 *
 * The command id is included so the run's own transcript can be correlated with
 * the journal and the live-comment marker after the fact.
 */
export function buildSteeringText(commandId: string, instruction: string, origin?: ControlOrigin): string {
  return [
    `An authorized operator submitted a mid-run steering instruction for this task (command ${commandId}).`,
    ...(origin ? [`Verified principal: ${JSON.stringify(origin.principal)}; authority: ${origin.authorityKind}; human origin: ${origin.authorityKind === 'human_session' ? 'yes' : 'not established by this delegated grant'}.`] : []),
    'Take it into account in the work you are doing now, subject to the trust rules below.',
    '',
    wrapUntrusted(instruction),
  ].join('\n');
}

/** The journal surface this pump uses. Narrow so tests can pass a real store. */
export type SteerJournal = Pick<
  ControlStateStore,
  'lookup' | 'pending' | 'settle' | 'deliverAuthorized' | 'steeringOrigin'
>;

export interface SteerQueueOptions {
  /** The run's single command journal — the FIFO, the cap and the ledger. */
  store: SteerJournal;
  /**
   * The neutral submit-input seam (`ControlRuntimeAdapter.submitInput`).
   *
   * Must reach the transport synchronously up to the physical push: it is
   * invoked from inside `deliverAuthorized`'s handoff, where no `await` is
   * permitted between the bounded authority check and the handoff.
   */
  submitInput: (input: ControlInput) => Promise<InputHandoffResult>;
  /**
   * Whether the harness can take input *right now*.
   *
   * The definition of "a supported handoff boundary", injected because it is
   * assembled from three independent facts the worker owns: the transport has a
   * reader waiting, no operator pause is active, and no admitted tool work is
   * outstanding. An unobservable work count must resolve to `false` here — "I
   * cannot see whether a tool is running" is not a boundary.
   */
  atBoundary: () => boolean;
  /**
   * Runtime event subscription, so the pump re-drives itself when a boundary
   * appears. Without it the pump only runs on submission, and a command accepted
   * mid-tool would wait for the next submission rather than for the boundary.
   */
  subscribe?: (listener: () => void) => () => void;
  /** Observed after every terminal outcome. Where the live-comment marker is written. */
  onOutcome?: (event: { commandId: string; outcome: SteerOutcome; reason: string }) => void;
  /** Injected for tests; production always wraps with the real trust boundary. */
  wrap?: (commandId: string, instruction: string, origin: ControlOrigin) => string;
  log?: (level: string, message: string, context?: Record<string, unknown>) => void;
  maxQueued?: number;
  maxInstructionChars?: number;
}

/**
 * The run's steering delivery pump.
 *
 * One per run. Holds no ordering of its own — `store.pending()` is the order —
 * and holds exactly one thing the journal deliberately does not: the
 * instruction text, which is kept out of `CommandRecord` because that record is
 * projected to the browser.
 */
export class SteerQueue {
  /**
   * command_id → the operator's raw instruction.
   *
   * Keyed by journal id rather than held as a list, so this map cannot express
   * an order that disagrees with the journal's. Entries are dropped the moment
   * their command leaves `pending`, whoever settled it — an abort cancels
   * commands without telling this pump, and a sidecar that kept their text would
   * both leak and misreport the queue depth.
   */
  private readonly instructions = new Map<string, string>();
  private readonly options: SteerQueueOptions;
  private readonly log: (level: string, message: string, context?: Record<string, unknown>) => void;
  private readonly unsubscribe: () => void;
  private readonly maxQueued: number;
  private readonly maxInstructionChars: number;
  private draining = false;
  private kicked = false;
  private closed = false;

  constructor(options: SteerQueueOptions) {
    this.options = options;
    this.log = options.log ?? (() => {});
    this.maxQueued = options.maxQueued ?? MAX_QUEUED_INSTRUCTIONS;
    this.maxInstructionChars = options.maxInstructionChars ?? MAX_STEER_INSTRUCTION_CHARS;
    // Every runtime transition is a candidate boundary: a tool settling changes
    // the work count, a release ends a pause, an attach publishes a new attempt
    // after a retry, and `input_ready` is the transport reporting a parked
    // reader. Rather than enumerate which ones matter — a list that would rot as
    // events are added — the pump re-evaluates `atBoundary()` on all of them and
    // lets that single predicate decide.
    this.unsubscribe = options.subscribe?.(() => this.kick()) ?? (() => {});
  }

  /**
   * Take ownership of an accepted steering command.
   *
   * Called from the listener's executor, i.e. after the journal has already
   * applied the cap, the replay rule and the conflict rule. The command stays
   * `pending` — deliberately, and this is the whole point of the story. Nothing
   * about acceptance is an acknowledgement of delivery.
   *
   * Returns `false` only for a command this pump refuses outright, having
   * settled it `rejected`.
   */
  enqueue(commandId: string, instruction: string): boolean {
    if (this.closed) {
      this.settle(commandId, 'cancelled', 'the run is finishing; this instruction was not delivered');
      return false;
    }
    if (typeof instruction !== 'string' || instruction.length === 0) {
      this.settle(commandId, 'rejected', 'no instruction text accompanied this steering command');
      return false;
    }
    if (instruction.length > this.maxInstructionChars) {
      this.settle(commandId, 'rejected', 'instruction exceeds the steering length bound');
      return false;
    }
    // Drop text for anything the journal has already settled before measuring
    // depth, so an abort that cancelled ten commands does not make the eleventh
    // look like an overflow.
    this.forget();
    if (this.instructions.has(commandId)) {
      // A replayed id never reaches here (the journal answers it with the
      // recorded outcome), so this is a same-id resubmission racing its own
      // acceptance. Keep the first text: two payloads under one id is the
      // conflict case, and the journal already refuses it.
      return true;
    }
    if (this.instructions.size >= this.maxQueued) {
      this.settle(commandId, 'rejected', 'steering queue is full');
      return false;
    }
    this.instructions.set(commandId, instruction);
    this.log('INFO', 'control: steering instruction queued', { command_id: commandId, queued: this.instructions.size });
    this.kick();
    return true;
  }

  /** Instructions accepted and not yet handed off. Diagnostics and tests. */
  queuedCount(): number {
    this.forget();
    return this.instructions.size;
  }

  /**
   * Re-drive the pump.
   *
   * Idempotent and non-blocking. The `kicked` flag collapses a burst of events
   * into one more pass instead of one pass per event, and the `draining` flag is
   * what makes delivery strictly serial — two concurrent drains could both see
   * the same parked reader and race for it.
   *
   * The re-drive in `finally` closes a lost wakeup that a test found rather than
   * reasoning predicted. `drain()` clears `kicked` at the top of each pass, and
   * its `finally` runs a microtask after the loop exits — so a kick landing in
   * that window sets `kicked` and returns early because `draining` is still true,
   * and then `draining` goes false with nobody left to act on the flag. The
   * window is small but the case that falls into it is the common one: a steer
   * whose readiness edge fires in the same tick as its own enqueue. The cost of
   * missing it is not a delay but a strand — the boundary edge has passed, so the
   * instruction waits for whatever unrelated runtime event happens next, which
   * during a long tool call may be minutes away or may never come.
   */
  private idleWaiters: Array<() => void> = [];

  async flush(): Promise<void> {
    this.kick();
    if (this.draining) await new Promise<void>(resolve => this.idleWaiters.push(resolve));
  }

  kick(): void {
    if (this.closed) return;
    this.kicked = true;
    if (this.draining) return;
    this.draining = true;
    void this.drain().finally(() => {
      this.draining = false;
      if (this.kicked && !this.closed) this.kick();
      else for (const resolve of this.idleWaiters.splice(0)) resolve();
    });
  }

  /**
   * Finish deterministically — Issue #3965.
   *
   * A completing run cannot deliver: the query loop has ended, so there is no
   * attempt and no reader, and waiting for one would hang teardown. Every
   * instruction still queued is therefore settled `cancelled`, which is the
   * honest word — the run did not refuse them, it ended before reaching them.
   * Leaving them `pending` would strand them mid-flight in the journal an
   * operator reads back, which is the same class of lie as a never-settled
   * abort.
   *
   * Idempotent, because it is called from teardown paths that also run on the
   * error path.
   */
  dispose(reason = 'the run finished before this instruction was delivered'): void {
    if (this.closed) return;
    this.closed = true;
    this.unsubscribe();
    for (const commandId of [...this.instructions.keys()]) {
      this.instructions.delete(commandId);
      if (this.options.store.lookup(commandId).status === 'pending') {
        this.settle(commandId, 'cancelled', reason);
      }
    }
  }

  /**
   * One pass over the journal, oldest first.
   *
   * Reads `store.pending()` rather than any local list, so FIFO order is the
   * journal's insertion order and cannot drift. Returning early on a closed
   * boundary is not a deferral mechanism — it *is* the "remains pending until an
   * authorized supported handoff boundary" rule: nothing is dropped, nothing is
   * buffered elsewhere, and the next runtime event calls this again.
   */
  private async drain(): Promise<void> {
    while (this.kicked && !this.closed) {
      this.kicked = false;
      for (const record of this.options.store.pending()) {
        if (this.closed) return;
        if (!this.isQueuedSteer(record)) continue;
        // Checked per command, not once per pass: a handoff consumes the parked
        // reader, so the boundary closes again behind every delivery. That is
        // what limits this to one instruction per boundary without a counter.
        if (!this.options.atBoundary()) return;
        await this.deliverOne(record.command_id);
        // A deferred oldest instruction must keep its FIFO position.
        if (this.options.store.lookup(record.command_id).status === 'pending') return;
      }
    }
  }

  private isQueuedSteer(record: CommandRecord): boolean {
    return record.action === 'steer' && this.instructions.has(record.command_id);
  }

  /**
   * Hand exactly one instruction to the harness.
   *
   * The ordering here is the security-relevant part and is not negotiable:
   *
   * 1. the text is composed and wrapped *before* the authority check, so no work
   *    happens between the check and the push;
   * 2. `deliverAuthorized` re-checks the grant against the live authority, flips
   *    the journal to `delivered`, and calls the handoff with no `await` in
   *    between. An authorization revoked since the 202 stops the command here,
   *    having sat in this queue for however long it sat;
   * 3. the handoff *invokes* `submitInput` synchronously and captures its
   *    promise. It does not await it — awaiting inside the handoff would
   *    reintroduce the gap step 2 exists to remove — and the result is read
   *    immediately afterwards.
   */
  private async deliverOne(commandId: string): Promise<void> {
    const instruction = this.instructions.get(commandId);
    if (instruction === undefined) return;
    const origin = this.options.store.steeringOrigin(commandId);
    if (!origin) {
      this.settle(commandId, 'rejected', 'no verified authorization origin was available for this steering command');
      return;
    }
    const text = (this.options.wrap ?? buildSteeringText)(commandId, instruction, origin);
    const input: ControlInput = { kind: 'steering', text, command_id: commandId };

    let handoff: Promise<InputHandoffResult> | null = null;
    let deferred = false;
    const authorized = await this.options.store.deliverAuthorized(commandId, () => {
      this.instructions.delete(commandId);
      handoff = this.options.submitInput(input);
    }, () => {
      // Re-check after async authorization, immediately before the physical push.
      // A pause or new tool admission during that await must hold the instruction.
      deferred = this.closed || !this.options.atBoundary();
      return !deferred;
    });
    if (!authorized) {
      // `deliverAuthorized` settled it `rejected` on a refused or slow re-check,
      // and `unknown` if the handoff itself threw. The one case it leaves alone
      // is a command with no authorization proof, which for a verb that requires
      // an envelope must fail closed rather than silently take the unauthorized
      // path.
      const status = this.options.store.lookup(commandId).status;
      if (deferred && status === 'pending') return;
      if (status === 'pending') {
        this.settle(commandId, 'rejected', 'no authorization proof was available for this steering command');
      } else {
        this.report(commandId, status === 'unknown' ? 'unknown' : 'rejected',
          this.options.store.lookup(commandId).reason ?? 'steering command was not delivered');
      }
      return;
    }

    const result = await (handoff ?? Promise.resolve<InputHandoffResult>('unknown'));
    if (result === 'delivered') {
      // The only place a steering command reaches a successful terminal status,
      // and it happens after the push rather than before it. `delivered_at` was
      // stamped by `deliverAuthorized` at the handoff itself, so the timestamp an
      // operator reads is the handoff time and not this line's time.
      this.settle(commandId, 'delivered', STEER_DELIVERED_REASON);
      return;
    }
    if (result === 'unknown') {
      // The transport consumed the instruction, or did not, and cannot say
      // which. Terminal and never retried: a replay would be a duplicate
      // instruction to the model, and reporting it as undelivered would invite
      // exactly that replay from the submitter.
      this.settle(commandId, 'unknown', 'the agent runtime gave no clear answer; this instruction may or may not have been delivered');
      return;
    }
    // A definite refusal — the attempt ended or its transport closed between the
    // boundary check and the push. Honest as `rejected`: the instruction was not
    // consumed. It is not moved back to pending, because `pending` would make it
    // eligible for a second handoff of a command that was already authorized.
    this.settle(commandId, 'rejected', 'the agent runtime refused the instruction at the handoff boundary; resubmit as a new command');
  }

  private settle(commandId: string, outcome: SteerOutcome, reason: string): void {
    const status = outcome === 'delivered' ? 'applied' : outcome;
    this.options.store.settle(commandId, status, reason);
    this.report(commandId, outcome, reason);
  }

  private report(commandId: string, outcome: SteerOutcome, reason: string): void {
    this.instructions.delete(commandId);
    this.log(outcome === 'delivered' ? 'INFO' : 'WARN', `control: steering ${outcome}`,
      { command_id: commandId, detail: reason });
    try {
      this.options.onOutcome?.({ commandId, outcome, reason });
    } catch (err) {
      // A marker sink is observability. A failing one must not stop delivery of
      // the next instruction, let alone end the run.
      this.log('WARN', 'control: steering outcome sink failed', { command_id: commandId,
        detail: (err as Error)?.message ?? String(err) });
    }
  }

  /** Drop text for every command the journal no longer lists as pending. */
  private forget(): void {
    for (const commandId of [...this.instructions.keys()]) {
      if (this.options.store.lookup(commandId).status !== 'pending') {
        this.instructions.delete(commandId);
      }
    }
  }
}

/**
 * `settle` maps `delivered` to `applied`, which deserves a note because the
 * words look interchangeable and are not.
 *
 * `delivered` is the *journal* status written at the physical handoff, and it is
 * non-terminal by design: while a command is `delivered` it still holds a slot
 * in the pending cap. A steering command has no later edge to wait for — nothing
 * reports back that the model read the message — so leaving it `delivered`
 * forever would hold its slot forever, and after ten steers an operator could
 * never steer again.
 *
 * So the pump settles it. `applied` is the accurate terminal word for the action
 * ADP actually performed (hand the instruction over), which is the same sense in
 * which an abort is `applied` when the run was cancelled — neither claims
 * anything about the model's behaviour. {@link STEER_DELIVERED_REASON} is
 * carried alongside so the record itself says which of the three facts it
 * attests to.
 */
export const STEER_STATUS_NOTE = STEER_DELIVERED_REASON;
