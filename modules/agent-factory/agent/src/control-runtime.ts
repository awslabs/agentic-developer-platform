/**
 * Harness-neutral control runtime contract — Issue #3962 (S3).
 *
 * This module is the boundary between "ADP wants to pause/steer/abort a run"
 * and "some specific agent harness knows how to do that." Everything above the
 * boundary — the listener, the command journal, the coordinator, the gateway —
 * speaks only the vocabulary defined here. Everything provider-specific lives
 * behind the {@link ControlRuntimeAdapter} interface, in an adapter module.
 *
 * The rule that makes this worth a separate file: **no provider SDK may be
 * imported here, and no provider type may appear in this file's exported
 * surface.** No `Query`, no `SDKUserMessage`, no `AsyncIterable` input
 * requirement, no branching on an adapter's name. A consumer that needs to know
 * whether it is talking to Claude has already broken the contract, because the
 * next harness will not answer to that question. The only provider-free import
 * below is `ControlAction` from `./control-state`, deliberately reused so this
 * story does not mint a second public state machine alongside Wave 1's.
 *
 * ## Why the attempt is the central concept
 *
 * A hosted run is not one continuous conversation with the model. The retry
 * wrapper may tear down a stalled query and build a fresh one several times
 * inside a single run. Each of those is an *attempt*, and every attempt has its
 * own transport: its own handle, its own open input channel, its own event
 * stream.
 *
 * That is precisely where control commands get lost. If a pause or a steer is
 * addressed to "the run" while the code underneath silently swapped attempt #1
 * for attempt #2, the command lands on a dead transport and disappears — the
 * dashboard reports success, the model never hears it, and nobody can tell the
 * difference after the fact. So an attempt here is an explicit, opaque,
 * replaceable endpoint, and {@link CurrentAttemptRegistry} enforces the three
 * properties that make retries safe:
 *
 * - **Replace on retry.** Attaching a new attempt detaches the old one first.
 * - **Invalidate before teardown.** The endpoint stops accepting input *before*
 *   its transport is disposed, so there is no window where a command is
 *   accepted by something already being destroyed.
 * - **Stale is inert, not an error path.** An old attempt cannot deliver input,
 *   cannot update current state, and its late events are discarded. It does not
 *   throw — it does nothing — because a stale event arriving during a normal
 *   retry is expected, not exceptional.
 *
 * ## Why `unknown` outranks `false`
 *
 * Several results here have three states where two would be simpler:
 * active-work counts are `number | null`, and an input handoff can resolve
 * `unknown`. That is not indecision. If a pod crashes midway through handing an
 * instruction to a harness, we genuinely do not know whether the harness
 * consumed it, and both confident answers cause harm: claiming `delivered`
 * lies to the operator, and claiming `not delivered` invites a replay of an
 * instruction that may already be executing. `unknown` is the honest answer and
 * it never triggers automatic replay. Likewise a `null` work count means "we
 * cannot see what is running", which must never be rendered as the quiescence
 * claim `0`.
 */
import type { ControlAction } from './control-state';

/**
 * Version of this contract. An adapter compiled against a different major
 * protocol version must be rejected rather than partially trusted — a
 * capability table is only meaningful if both sides agree what the verbs mean.
 */
export const CONTROL_PROTOCOL_VERSION = 1;

/**
 * Pause/resume have an admission barrier and signed gateway control path; abort
 * has a cancellation path and a terminal finalization that reports it (#3963).
 * The adapter, current attempt and deployment configuration each still veto
 * unavailable controls. Steer remains outside this implementation set.
 * Kept in lockstep with the gateway and both CI capability gates (#5222).
 */
export const IMPLEMENTED_CONTROL_VERBS: ReadonlySet<ControlAction> = new Set<ControlAction>(['pause', 'resume', 'abort']);

/** Per-verb support, with a bounded reason when support is absent. */
export interface VerbSupport {
  supported: boolean;
  /**
   * Short operator-facing explanation, required when `supported` is false.
   * Bounded because it is re-projected to the browser: it must carry no native
   * session id, no credential and no unbounded provider error text.
   */
  reason?: string;
}

/** Maximum length of a {@link VerbSupport.reason}, enforced by {@link boundReason}. */
export const MAX_REASON_LENGTH = 200;

/** Truncate an unavailability reason to its bound. */
export function boundReason(reason: string): string {
  const collapsed = reason.replace(/\s+/g, ' ').trim();
  return collapsed.length <= MAX_REASON_LENGTH ? collapsed : `${collapsed.slice(0, MAX_REASON_LENGTH - 1)}…`;
}

/**
 * What an adapter says about itself.
 *
 * Carries no native session data: this is safe to project toward the browser
 * after the gateway's own intersection, so a native turn or session identifier
 * appearing here would leak a provider-private handle to an untrusted client.
 */
export interface HarnessDescriptor {
  protocolVersion: number;
  /** Stable adapter identity, e.g. `claude`. Never used by consumers to branch. */
  adapterId: string;
  adapterVersion: string;
  /** Per-verb support this adapter can prove, before ADP/runtime intersection. */
  capabilities: Readonly<Record<ControlAction, VerbSupport>>;
}

/**
 * Opaque handle for one attempt.
 *
 * Branded so a native session id, a run id or a generation number cannot be
 * passed where an attempt is expected. The value is meaningless outside the
 * runtime: adapters must not encode a resumable native session into it, because
 * anything here may reach a state response.
 */
export type AttemptId = string & { readonly __attempt: unique symbol };

let attemptCounter = 0;

/** Mint a fresh opaque attempt id. Monotonic per process; not a native handle. */
export function newAttemptId(): AttemptId {
  attemptCounter += 1;
  return `attempt-${attemptCounter}` as AttemptId;
}

/**
 * ADP-owned input message.
 *
 * `steering` may request work — it is allowed to start an assistant turn.
 * `annotation` records context *without* starting one. Keeping both in one
 * shape, with the distinction as data rather than as two methods, is what lets
 * the shared layer queue and authorize them identically while each adapter
 * decides how its harness expresses the difference.
 *
 * Note what is absent: no iterator, no stream, no provider message type. An
 * adapter that needs an open async iterable builds one internally.
 */
export interface ControlInput {
  kind: 'steering' | 'annotation';
  /** Untrusted operator text. Trust-boundary wrapping happens before this point. */
  text: string;
  /** Journal id, when this input originates from a submitted command. */
  command_id?: string;
}

/**
 * Outcome of physically handing input to a harness.
 *
 * `unknown` exists for the ambiguous case and is never upgraded by inference.
 */
export type InputHandoffResult = 'delivered' | 'rejected' | 'unknown';

/** Why a pause could not be established. Never reported as a successful pause. */
export interface PauseUnavailable {
  outcome: 'unavailable';
  reason: string;
}

/**
 * Result of a pause request.
 *
 * `requested` means new action admission is closed but tracked work has not yet
 * settled; `confirmed` means the barrier is effective and no new tool side
 * effects can occur. Only `confirmed` may be surfaced as "Paused" — reporting a
 * `requested` state as paused is the exact claim the design forbids, because an
 * operator reads "Paused" as "nothing is touching my repository right now."
 */
export type PauseResult =
  | { outcome: 'requested'; reason?: string }
  | { outcome: 'confirmed' }
  | PauseUnavailable;

/** Terminal outcomes, preserving every distinction downstream automation needs. */
export type TerminalOutcome =
  | 'complete'
  | 'failed'
  | 'blocked'
  | 'skipped'
  | 'budget_stopped'
  | 'credential_retry'
  | 'aborted';

/**
 * Normalized runtime events.
 *
 * Every event carries the attempt it came from so the registry can discard
 * events from a superseded attempt. An adapter never mutates ADP control state
 * directly; it emits these, and the coordinator decides what they mean.
 */
export type ControlRuntimeEvent =
  | { type: 'attempt_attached'; attemptId: AttemptId }
  | { type: 'attempt_detached'; attemptId: AttemptId }
  /** `count: null` means "cannot observe", which is not `0`. */
  | { type: 'active_work'; attemptId: AttemptId; count: number | null }
  | { type: 'pause_requested'; attemptId: AttemptId }
  | { type: 'pause_waiting'; attemptId: AttemptId; reason: string }
  | { type: 'pause_confirmed'; attemptId: AttemptId }
  | { type: 'pause_released'; attemptId: AttemptId; expired?: boolean }
  | { type: 'pause_unavailable'; attemptId: AttemptId; reason: string }
  | { type: 'input_handoff'; attemptId: AttemptId; command_id?: string; result: InputHandoffResult }
  | { type: 'terminal'; attemptId: AttemptId; outcome: TerminalOutcome };

export type ControlRuntimeListener = (event: ControlRuntimeEvent) => void;

/**
 * Typed intentional cancellation.
 *
 * A distinct class, not a message string, because the retry wrapper classifies
 * failures by matching error text against patterns like `aborted` and
 * `timeout`. A cancellation that travelled as an ordinary `Error` would match
 * those patterns and be treated as a transient fault — so a deliberate abort
 * would politely start a brand new attempt, which is the opposite of abort.
 * {@link isControlCancellation} is checked *before* any text matching.
 */
export class ControlCancelledError extends Error {
  /** Structural marker: survives a module-boundary identity mismatch. */
  readonly isControlCancellation = true as const;

  constructor(reason = 'control runtime cancelled') {
    super(reason);
    this.name = 'ControlCancelledError';
  }
}

/** Recognize a cancellation without relying on `instanceof` across realms. */
export function isControlCancellation(err: unknown): boolean {
  return (
    err instanceof ControlCancelledError ||
    (typeof err === 'object' && err !== null && (err as { isControlCancellation?: unknown }).isControlCancellation === true)
  );
}

/**
 * One attempt's transport, owned by an adapter.
 *
 * The adapter implements this per query/session/turn; the registry owns *which*
 * one is current. Splitting it this way is what keeps the staleness rule in one
 * neutral place instead of re-implemented (and eventually mis-implemented) in
 * every adapter.
 */
export interface AttemptEndpoint {
  readonly attemptId: AttemptId;
  /** Physically hand input to this attempt's harness transport. */
  deliver(input: ControlInput): Promise<InputHandoffResult>;
  /** Close admission of new work; resolve only when the barrier is real. */
  requestPause?(signal: AbortSignal): Promise<PauseResult>;
  /** Release a confirmed pause, or cancel a pending one. Idempotent. */
  releasePause?(): Promise<void>;
  /** Tracked in-flight work, or `null` when unobservable. */
  activeWorkCount?(): number | null;
  /** Tear down this attempt's transport. Called at most once by the registry. */
  dispose(): Promise<void>;
}

/**
 * The provider-free interface every harness adapter implements.
 *
 * Consumers (S2/S4/S6, the coordinator) depend only on this.
 */
export interface ControlRuntimeAdapter {
  describe(): HarnessDescriptor;
  /**
   * Effective per-verb capabilities: the intersection of ADP-implemented verbs,
   * adapter support and current availability.
   *
   * Distinct from `describe().capabilities`, which is the adapter's own claim
   * before intersection. Consumers must read *this* — the raw claim is evidence
   * for a decision, not the decision.
   */
  capabilities(): Record<ControlAction, boolean>;
  /** Current attempt, or `null` between attempts and after teardown. */
  currentAttempt(): AttemptId | null;
  /**
   * Submit ADP-owned input to whatever attempt is current *now*.
   *
   * Resolving against the current attempt at call time — rather than against an
   * attempt the caller captured earlier — is the property that keeps a command
   * working across a retry.
   */
  submitInput(input: ControlInput): Promise<InputHandoffResult>;
  requestPause(options?: { signal?: AbortSignal; timeoutMs?: number }): Promise<PauseResult>;
  resumeFromPause(): Promise<void>;
  /** Typed cancellation: prevents further attempts; never a retryable error. */
  cancel(reason?: string): void;
  subscribe(listener: ControlRuntimeListener): () => void;
  /** Idempotent teardown. Safe to call on every exit path. */
  dispose(): Promise<void>;
}

/**
 * Compute the verbs a worker may advertise.
 *
 * The intersection of three independent facts, all of which must hold:
 * what ADP has implemented, what the selected adapter supports, and what is
 * available right now. Any one of them being false makes the verb false.
 *
 * Written as an intersection rather than a precedence chain because each input
 * has a different owner and they drift: ADP's set moves per story, adapter
 * support is a property of the harness, and availability changes mid-run. An
 * `||` anywhere here would let one owner's optimism override another's veto.
 * The gateway then intersects again with its own allowlist — deliberately
 * redundant, so neither side alone can enable a verb.
 */
export function intersectCapabilities(input: {
  implemented?: ReadonlySet<ControlAction>;
  adapter: Readonly<Record<ControlAction, VerbSupport>>;
  available?: ReadonlySet<ControlAction>;
}): Record<ControlAction, boolean> {
  const implemented = input.implemented ?? IMPLEMENTED_CONTROL_VERBS;
  const verbs: ControlAction[] = ['pause', 'resume', 'steer', 'abort'];
  const result = {} as Record<ControlAction, boolean>;
  for (const verb of verbs) {
    const adapterSupport = input.adapter[verb]?.supported === true;
    const availableNow = input.available === undefined ? true : input.available.has(verb);
    result[verb] = implemented.has(verb) && adapterSupport && availableNow;
  }
  return result;
}

/** All four verbs unsupported, with a reason. Used by adapters and by S3 wiring. */
export function noVerbsSupported(reason: string): Record<ControlAction, VerbSupport> {
  const bounded = boundReason(reason);
  return {
    pause: { supported: false, reason: bounded },
    resume: { supported: false, reason: bounded },
    steer: { supported: false, reason: bounded },
    abort: { supported: false, reason: bounded },
  };
}

/**
 * The verb set a worker's control listener may advertise, derived from an adapter.
 *
 * This exists because the listener and the runtime otherwise hold two
 * independent answers to "which verbs does this build perform". Both are empty
 * today, so nothing currently disagrees — which is exactly why the seam is worth
 * closing now rather than after it first matters. The next story to enable a
 * verb widens one of them, and whichever it forgets produces one of two
 * failures: widen only the listener and the run answers 200 for a verb with no
 * transport behind it, accepting a command it will never perform; widen only the
 * runtime and the run answers 501 for a verb that works. The first is worse,
 * because a caller is told yes.
 *
 * Deriving one from the other makes both states unreachable rather than merely
 * unlikely — the listener's set becomes a *consequence* of the adapter's facts
 * instead of a parallel claim about them.
 *
 * **Availability is deliberately excluded here**, so this is the two-way
 * intersection and not the three-way one. The listener's set answers a
 * build-level question ("is this verb implemented at all") whose answer the
 * store fixes once per run, while availability is a per-moment fact that changes
 * as attempts come and go. Feeding availability in would make a verb report
 * `not_implemented` during any window with no live attempt — a wrong diagnosis
 * that sends an operator looking for a missing feature instead of a transient
 * state. The runtime's own {@link ControlRuntimeAdapter.capabilities} remains the
 * three-way intersection and is what a verb's execution path must consult; a
 * story that enables a verb owns the "advertised but not available right now"
 * response, which is a live-state answer rather than a capability one.
 */
export function listenerActionsFor(
  adapter: Pick<ControlRuntimeAdapter, 'describe'>,
  implemented: ReadonlySet<ControlAction> = IMPLEMENTED_CONTROL_VERBS
): ReadonlySet<ControlAction> {
  const claimed = adapter.describe().capabilities;
  const verbs: ControlAction[] = ['pause', 'resume', 'steer', 'abort'];
  return new Set<ControlAction>(
    verbs.filter((verb) => implemented.has(verb) && claimed[verb]?.supported === true)
  );
}

/**
 * Owns which attempt is current, and enforces that only that attempt can act.
 *
 * Neutral on purpose: the retry-safety rules are identical for every harness,
 * so they are implemented once here and inherited by every adapter rather than
 * re-derived per provider.
 */
export class CurrentAttemptRegistry {
  private current: AttemptEndpoint | null = null;
  private readonly listeners = new Set<ControlRuntimeListener>();
  /** Endpoints already disposed — the "dispose exactly once" ledger. */
  private readonly disposed = new WeakSet<AttemptEndpoint>();
  private attachTail: Promise<void> = Promise.resolve();
  private readonly cancellationController = new AbortController();
  readonly cancellationSignal: AbortSignal = this.cancellationController.signal;
  private cancelled = false;
  private cancellationReason: string | undefined;
  private teardown = false;

  subscribe(listener: ControlRuntimeListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /**
   * Current attempt id, or `null` between attempts, after cancellation, and
   * after teardown.
   *
   * Cancellation counts as "no live attempt" from the very first synchronous
   * moment, even though the endpoint's disposal is still in flight. Reporting the
   * doomed attempt during that window would be a lie with two consequences:
   * `capabilities()` derives availability from this method, so a cancelled run
   * would keep advertising verbs the dashboard then offers as buttons; and a
   * caller polling for "has the attempt gone?" would wait on an id that is never
   * coming back. `cancel()` is deliberately synchronous for this reason — the
   * intent must be observable before any await resolves.
   */
  currentAttemptId(): AttemptId | null {
    if (this.cancelled || this.teardown) return null;
    return this.current?.attemptId ?? null;
  }

  isCancelled(): boolean {
    return this.cancelled;
  }

  /**
   * Whether an attempt is the live one.
   *
   * The single predicate behind "a stale attempt cannot receive input or update
   * current state." Adapters call it before acting on a captured handle.
   */
  isCurrent(attemptId: AttemptId): boolean {
    return !this.cancelled && !this.teardown && this.current !== null && this.current.attemptId === attemptId;
  }

  /**
   * Install a new attempt, replacing any predecessor.
   *
   * Order matters and is the whole point: the old endpoint is invalidated and
   * disposed *before* the new one becomes current. Publishing the new attempt
   * first would leave a window in which two endpoints both look live, and a
   * command arriving in that window could be delivered to the one being torn
   * down.
   *
   * Rejects after cancellation so a cancelled run cannot acquire a new attempt —
   * without this, cancelling during backoff would be followed by the retry loop
   * cheerfully attaching attempt N+1.
   */
  attach(endpoint: AttemptEndpoint): Promise<void> {
    const attach = this.attachTail.then(async () => {
      await this.detachCurrent();
      // Cancellation/disposal may have arrived while the predecessor closed.
      if (this.cancelled || this.teardown || this.disposed.has(endpoint)) {
        await this.disposeOnce(endpoint);
        throw this.cancellationError();
      }
      this.current = endpoint;
      this.emit({ type: 'attempt_attached', attemptId: endpoint.attemptId });
    });
    this.attachTail = attach.catch(() => {});
    return attach;
  }

  /** Invalidate and dispose the current attempt, if any. */
  async detachCurrent(expected?: AttemptId): Promise<void> {
    const previous = this.current;
    if (!previous || (expected !== undefined && previous.attemptId !== expected)) return;
    // Invalidate first: from here on `isCurrent(previous)` is false, so a
    // concurrent delivery attempt is refused rather than racing disposal.
    this.current = null;
    await this.disposeOnce(previous);
    this.emit({ type: 'attempt_detached', attemptId: previous.attemptId });
  }

  /**
   * Deliver input to the attempt that is current at call time.
   *
   * Returns `rejected` when there is nothing live to deliver to. It does not
   * queue: the shared coordinator owns the run-level FIFO and journal, and a
   * second buffer here would be a hidden queue that bypasses the authorization
   * revalidation performed immediately before handoff.
   */
  async deliver(input: ControlInput): Promise<InputHandoffResult> {
    const endpoint = this.current;
    if (!endpoint || this.cancelled || this.teardown) return 'rejected';
    let result: InputHandoffResult;
    try {
      result = await endpoint.deliver(input);
    } catch (err) {
      // A throw mid-handoff is the ambiguous case: the transport may or may not
      // have consumed the instruction. `unknown` blocks replay; `rejected`
      // would invite one.
      result = isControlCancellation(err) ? 'rejected' : 'unknown';
    }
    // Deliver resolved against an attempt that has since been replaced: report
    // the ambiguity rather than claiming delivery to the live attempt.
    if (!this.isCurrent(endpoint.attemptId) && result === 'delivered') {
      result = 'unknown';
    }
    this.emit({ type: 'input_handoff', attemptId: endpoint.attemptId, command_id: input.command_id, result });
    return result;
  }

  /** Tracked work for the current attempt. `null` when unobservable. */
  activeWorkCount(): number | null {
    const endpoint = this.current;
    if (!endpoint || !this.isCurrent(endpoint.attemptId) || !endpoint.activeWorkCount) return null;
    return endpoint.activeWorkCount();
  }

  /**
   * Emit an event, discarding anything from a superseded attempt.
   *
   * Attach/detach are structural and always pass; everything else must come
   * from the live attempt. Silently dropping is correct — a late event from a
   * retried-away attempt is routine, and treating it as an error would make
   * normal retries look like failures.
   */
  emit(event: ControlRuntimeEvent): void {
    const structural = event.type === 'attempt_attached' || event.type === 'attempt_detached';
    if (!structural && !this.isCurrent(event.attemptId)) return;
    for (const listener of this.listeners) {
      try {
        listener(event);
      } catch {
        // A broken observer must not take the run down with it.
      }
    }
  }

  /**
   * Mark the runtime cancelled.
   *
   * Synchronous and idempotent so it is safe from a signal handler or a command
   * path. It records intent immediately; disposal is asynchronous. That ordering
   * is deliberate — a cancel issued during backoff must be visible to the retry
   * loop before any await completes, otherwise the loop starts another attempt
   * while cancellation is still in flight.
   */
  cancel(reason = 'control runtime cancelled'): void {
    if (this.cancelled) return;
    this.cancelled = true;
    this.cancellationReason = reason;
    this.cancellationController.abort();
  }

  /** The cancellation to throw, so every path reports one typed error. */
  cancellationError(): ControlCancelledError {
    return new ControlCancelledError(this.cancellationReason ?? 'control runtime cancelled');
  }

  /** Idempotent full teardown: invalidate, dispose once, stop accepting input. */
  async dispose(): Promise<void> {
    if (this.teardown) return;
    this.teardown = true;
    this.cancel('control runtime disposed');
    await this.detachCurrent();
    await this.attachTail;
    this.listeners.clear();
  }

  /**
   * Dispose an endpoint at most once.
   *
   * Both `attach` (replacing) and `dispose` (teardown) can reach the same
   * endpoint, and a double `session.close()` on a provider handle is not
   * reliably harmless. The ledger makes the guarantee structural instead of
   * depending on call ordering.
   */
  private async disposeOnce(endpoint: AttemptEndpoint): Promise<void> {
    if (this.disposed.has(endpoint)) return;
    this.disposed.add(endpoint);
    try {
      await endpoint.dispose();
    } catch {
      // Teardown errors must not mask the outcome that caused teardown.
    }
  }
}
