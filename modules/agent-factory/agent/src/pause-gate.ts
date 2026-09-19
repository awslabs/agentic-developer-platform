/**
 * Harness-neutral pause coordinator — Issue #3961 (S2).
 *
 * This module answers one question honestly: **may a run report "Paused"?**
 *
 * An operator reads "Paused" as "nothing is touching my repository right now."
 * That is a claim about tool side effects, not about the progress display, so
 * this gate is built around admission rather than around output. Two facts must
 * both hold before `paused` is reported:
 *
 * 1. No *new* tool work can start — admission is closed, and anything arriving
 *    afterwards is parked before it runs.
 * 2. Every *already admitted* tool has reached a safe boundary, and no untracked
 *    background work remains behind it.
 *
 * Anything short of both is `requested` or `unavailable` **with a reason**. That
 * asymmetry is the whole design: a false `requested` costs an operator some
 * patience, while a false `paused` invites them to edit files underneath a run
 * that is still writing to them.
 *
 * ## Why admission, and why a pending hook is not a running tool
 *
 * The only place a tool can be stopped *before* it writes a file or calls a
 * service is the moment between the model choosing it and the harness running
 * it. {@link PauseGate.admit} is that moment. A parked admission is a tool that
 * has not started, so it deliberately does **not** count toward in-flight work —
 * counting it would deadlock the gate against itself, because confirmation waits
 * for in-flight work to reach zero and the parked call would keep it at one
 * forever.
 *
 * ## Why "cannot observe" is not "nothing is running"
 *
 * {@link PauseGateOptions.backgroundWorkProbe} may answer `null`, and a `null`
 * blocks confirmation exactly as a positive count does. A completed parent tool
 * is not proof that the process it spawned has stopped, so an unobservable
 * answer is treated as possible activity rather than as silence. This mirrors the
 * neutral runtime's rule that an unknown work count is `null` and never `0`.
 *
 * ## Why a timed-out barrier is a failure and not a pause
 *
 * The harness — not this module — bounds how long a parked admission may block.
 * When that bound is exceeded the harness may run the tool anyway, so a barrier
 * that has been abandoned mid-park is reported as {@link PauseGate.barrierBreached}
 * and collapses the pause to `unavailable`. It is never quietly upgraded to a
 * confirmation on the theory that the tool probably did not run.
 *
 * ## Provider neutrality
 *
 * Nothing here imports a provider SDK or names one. The gate consumes injected
 * primitives — a clock, a scheduler, a background-work probe — and the adapter
 * translates its decisions into whatever its harness understands. `ControlAction`
 * comes from `./control-state`, deliberately reusing Wave 1's vocabulary rather
 * than minting a second state machine.
 */

/** Neutral admission decision returned to whatever calls the barrier. */
export type AdmissionDecision = 'admit' | 'deny';

/**
 * A granted admission, returned so the caller can report the tool's completion.
 *
 * Opaque and single-use: {@link PauseGate.settle} ignores a token it has already
 * seen, so a harness that reports both a success and a failure edge for one tool
 * cannot drive the in-flight count negative and fake quiescence.
 */
export interface AdmissionTicket {
  readonly id: number;
  readonly toolName: string;
}

export interface AdmissionResult {
  decision: AdmissionDecision;
  /** Present only when `decision` is `admit`. */
  ticket?: AdmissionTicket;
  /** Present when denied, for operator-facing diagnostics. */
  reason?: string;
}

/** Lifecycle phase of the gate. Distinct from the public control phase. */
export type PauseGatePhase = 'running' | 'pause_requested' | 'paused' | 'cancelled';

/** Why a pause could not be confirmed, kept short for operator display. */
export type PauseGateFailure =
  | 'barrier_timeout'
  | 'background_work'
  | 'settle_timeout'
  | 'no_safe_budget'
  | 'attempt_ended'
  | 'cancelled';

export type PauseGateEvent =
  | { type: 'pause_requested' }
  | { type: 'pause_confirmed' }
  | { type: 'pause_released'; expired: boolean }
  | { type: 'pause_unavailable'; failure: PauseGateFailure; reason: string }
  | { type: 'active_work'; count: number };

/** Injectable timer, so tests drive expiry without real waiting. */
export interface PauseGateScheduler {
  setTimer(fn: () => void, ms: number): unknown;
  clearTimer(handle: unknown): void;
}

export interface PauseGateOptions {
  now?: () => number;
  scheduler?: PauseGateScheduler;
  /** Default maximum pause duration. 30 minutes per the approved design. */
  defaultTimeoutMs?: number;
  /**
   * Time reserved for finalization after a pause ends.
   *
   * Subtracted from the remaining pod deadline so an auto-resume still has room
   * to write its terminal state; a pause that consumed the entire deadline would
   * be indistinguishable to an operator from a pod that vanished.
   */
  finalizationMarginMs?: number;
  /** Absolute pod deadline in epoch ms, or `null` when unbounded. */
  deadlineAt?: () => number | null;
  /**
   * Bounded wait for admitted tools to reach a safe boundary before the gate
   * reports `requested` instead of `confirmed`.
   */
  settleTimeoutMs?: number;
  /**
   * Untracked background work behind completed tools: a count, or `null` when
   * unobservable. Both a positive count and `null` block confirmation.
   */
  backgroundWorkProbe?: () => number | null;
  onEvent?: (event: PauseGateEvent) => void;
  log?: (msg: string) => void;
}

/** 30 minutes, per revival-design section 4. */
export const DEFAULT_PAUSE_TIMEOUT_MS = 30 * 60 * 1000;
/** Room left for terminal bookkeeping after a pause ends. */
export const DEFAULT_FINALIZATION_MARGIN_MS = 60 * 1000;
/** Bounded confirmation wait: long enough for a normal tool, short enough to answer. */
export const DEFAULT_SETTLE_TIMEOUT_MS = 60 * 1000;

/** Outcome of a pause request, mirroring the neutral runtime's `PauseResult`. */
export type PauseGateResult =
  | { outcome: 'requested'; reason: string }
  | { outcome: 'confirmed' }
  | { outcome: 'unavailable'; reason: string };

interface ParkedAdmission {
  toolName: string;
  release: (decision: AdmissionDecision, reason?: string) => void;
  detach: () => void;
}

/**
 * Serialized pause/resume coordinator.
 *
 * Every public transition runs through {@link PauseGate.serialize}, so a resume
 * racing a pause, two pauses racing each other, or an abort landing mid-pause
 * are ordered rather than interleaved. Without that ordering the interesting
 * bugs are all of the same shape: two transitions each observe the pre-state and
 * both act on it, releasing a pause twice or confirming one that abort already
 * cancelled.
 */
export class PauseGate {
  private phase: PauseGatePhase = 'running';
  private readonly now: () => number;
  private readonly scheduler: PauseGateScheduler;
  private readonly defaultTimeoutMs: number;
  private readonly finalizationMarginMs: number;
  private readonly settleTimeoutMs: number;
  private readonly deadlineAt: () => number | null;
  private readonly backgroundWorkProbe: () => number | null;
  private readonly onEventOption: (event: PauseGateEvent) => void;
  private readonly log: (msg: string) => void;
  /** Late-attached observers, e.g. the adapter that renders expiry into its harness. */
  private readonly listeners = new Set<(event: PauseGateEvent) => void>();

  /** Admitted, not yet settled. Parked admissions are deliberately excluded. */
  private readonly inFlight = new Map<number, AdmissionTicket>();
  private readonly parked = new Set<ParkedAdmission>();
  private readonly outputWaiters = new Set<() => void>();
  private ticketSeq = 0;

  /** Resolvers waiting for in-flight work to reach zero. */
  private quiescenceWaiters = new Set<() => void>();
  private expiryTimer: unknown = null;
  private transitionTail: Promise<unknown> = Promise.resolve();
  /** Set once a parked admission is abandoned by its harness mid-park. */
  private breached = false;
  /** Guards "release or cancel a pending pause exactly once". */
  private pauseEpoch = 0;

  constructor(options: PauseGateOptions = {}) {
    this.now = options.now ?? (() => Date.now());
    this.scheduler =
      options.scheduler ??
      {
        setTimer: (fn, ms) => setTimeout(fn, ms),
        clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
      };
    this.defaultTimeoutMs = options.defaultTimeoutMs ?? DEFAULT_PAUSE_TIMEOUT_MS;
    this.finalizationMarginMs = options.finalizationMarginMs ?? DEFAULT_FINALIZATION_MARGIN_MS;
    this.settleTimeoutMs = options.settleTimeoutMs ?? DEFAULT_SETTLE_TIMEOUT_MS;
    this.deadlineAt = options.deadlineAt ?? (() => null);
    this.backgroundWorkProbe = options.backgroundWorkProbe ?? (() => 0);
    this.onEventOption = options.onEvent ?? (() => {});
    this.log = options.log ?? (() => {});
  }

  /**
   * Observe gate transitions after construction.
   *
   * Needed because one of the consumers cannot exist at construction time: the
   * harness adapter has to translate an *expired* pause into a message on a
   * session that only exists once the query has started, and the gate has to be
   * installed as a hook before that. A broken observer is swallowed rather than
   * propagated — an event listener must not be able to fail a pause transition,
   * and by the time these fire the transition has already been decided.
   */
  subscribe(listener: (event: PauseGateEvent) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Fan one event out to the constructor callback and every subscriber. */
  private onEvent(event: PauseGateEvent): void {
    const observers = [this.onEventOption, ...this.listeners];
    for (const observer of observers) {
      try {
        observer(event);
      } catch {
        // See `subscribe`: an observer must not take a transition down with it.
      }
    }
  }

  /** Current gate phase. */
  currentPhase(): PauseGatePhase {
    return this.phase;
  }

  /**
   * Whether a pause is in force, i.e. admission is closed.
   *
   * The worker's idle-retry and post-completion watchdogs consult this: a run
   * that is quiet *because an operator paused it* must not be mistaken for a run
   * that has stalled and killed off.
   */
  isPauseActive(): boolean {
    return this.phase === 'pause_requested' || this.phase === 'paused';
  }

  /** Admitted-and-unsettled tool count. Always observed, so always a number. */
  activeToolCount(): number {
    return this.inFlight.size;
  }

  /** Parked-but-not-started admissions, for evidence and diagnostics. */
  heldCount(): number {
    return this.parked.size;
  }

  /**
   * The longest a single admission can ever be parked here.
   *
   * Published because the harness — not this gate — enforces how long a hook may
   * block, and it needs a number to configure that with. A harness bound *below*
   * this value silently converts every long pause into a barrier breach, so the
   * adapter derives its hook timeout from this rather than from a constant that
   * could drift away from the budget. Neutral: milliseconds, no hook vocabulary.
   *
   * The deadline clamp only ever shortens an individual pause, so the configured
   * default is the true upper bound.
   */
  maxParkDurationMs(): number {
    return this.defaultTimeoutMs;
  }

  /** Whether a parked admission was abandoned by its harness mid-park. */
  barrierBreached(): boolean {
    return this.breached;
  }

  /**
   * The admission barrier: decide whether one tool invocation may start.
   *
   * While running, this admits immediately and counts the tool in. While a pause
   * is pending or confirmed, it parks until resume, then admits — the tool the
   * model chose still runs, just after the operator lets it.
   *
   * `signal` is the harness's abandonment signal for this call. If it fires while
   * parked, the harness has stopped waiting for our answer and may run the tool
   * regardless, so the barrier is marked breached and any pause built on it
   * collapses to `unavailable`.
   */
  async admit(toolName: string, signal?: AbortSignal): Promise<AdmissionResult> {
    if (this.phase === 'cancelled') {
      return { decision: 'deny', reason: 'run cancelled' };
    }
    if (!this.isPauseActive()) {
      return { decision: 'admit', ticket: this.issueTicket(toolName) };
    }
    if (signal?.aborted) {
      this.markBreached(toolName);
      return { decision: 'deny', reason: 'pause barrier abandoned before admission' };
    }

    return new Promise<AdmissionResult>((resolve) => {
      let settled = false;
      const entry: ParkedAdmission = {
        toolName,
        release: (decision, reason) => {
          if (settled) return;
          settled = true;
          entry.detach();
          this.parked.delete(entry);
          if (decision === 'admit') {
            resolve({ decision: 'admit', ticket: this.issueTicket(toolName) });
          } else {
            resolve({ decision: 'deny', reason });
          }
        },
        detach: () => {},
      };

      if (signal) {
        const onAbort = () => {
          // The harness gave up on our answer: it may now run the tool, so no
          // pause resting on this barrier can still claim quiescence.
          this.markBreached(toolName);
          entry.release('deny', 'pause barrier timed out');
        };
        signal.addEventListener('abort', onAbort, { once: true });
        entry.detach = () => signal.removeEventListener('abort', onAbort);
      }

      this.parked.add(entry);
      this.log(`[pause-gate] holding ${toolName} at the admission barrier`);
    });
  }

  /** Hold task output and terminal teardown without counting them as tools. */
  async waitForOutput(): Promise<boolean> {
    while (this.isPauseActive()) {
      await new Promise<void>((resolve) => this.outputWaiters.add(resolve));
    }
    return this.phase !== 'cancelled';
  }

  private wakeOutput(): void {
    const waiting = [...this.outputWaiters];
    this.outputWaiters.clear();
    for (const resolve of waiting) resolve();
  }

  /**
   * Report that an admitted tool reached a safe boundary.
   *
   * Idempotent per ticket, so a harness that emits both a completion and a
   * failure edge for one tool cannot double-decrement into a false quiescence.
   */
  settle(ticket: AdmissionTicket | undefined): void {
    if (!ticket || !this.inFlight.has(ticket.id)) return;
    this.inFlight.delete(ticket.id);
    this.onEvent({ type: 'active_work', count: this.inFlight.size });
    if (this.inFlight.size === 0) {
      const waiters = [...this.quiescenceWaiters];
      this.quiescenceWaiters.clear();
      for (const waiter of waiters) waiter();
      // Quiescence is an *edge the gate observes*, not only the resolution of one
      // bounded wait. A tool that outlives `settleTimeoutMs` used to leave the
      // pause stuck in `pause_requested` for the rest of its budget even after the
      // run went completely quiet, because the only thing that could confirm was a
      // waiter that had already been discarded when its timer fired. The operator
      // saw "pausing…" until expiry silently resumed the run. Re-evaluating here
      // means a late-settling tool confirms the pause it delayed.
      if (this.phase === 'pause_requested') void this.reconfirm();
    }
  }

  /**
   * Report that untracked background work may have changed.
   *
   * Confirmation has two independent blockers — admitted tools still in flight, and
   * background work behind completed ones — and only the first used to have an edge
   * that re-drove the decision. A pause withheld because the probe answered `null`
   * therefore stayed `pause_requested` for its entire budget even after the probe
   * cleared, then auto-resumed. The operator saw "pausing…" for thirty minutes and
   * got a run that never paused, for a reason that had stopped being true almost
   * immediately.
   *
   * Neutral by construction: whoever observes the change calls this, and the gate
   * re-reads its own probe rather than being handed a count. Nothing about what
   * background work *is*, or how it was observed, crosses this boundary — which is
   * what keeps the shared coordinator free of harness vocabulary.
   */
  noteBackgroundWorkChanged(): void {
    if (this.phase !== 'pause_requested') return;
    if (this.inFlight.size !== 0) return;
    void this.reconfirm();
  }

  /**
   * Re-drive confirmation for a pause that is still pending.
   *
   * Serialized like every other transition, and epoch-guarded, so a resume, an
   * abort or a breach that lands between the settle edge and this running owns the
   * outcome instead of being overwritten by a stale confirmation.
   */
  private async reconfirm(): Promise<void> {
    const epoch = this.pauseEpoch;
    await this.serialize(async () => {
      if (this.pauseEpoch !== epoch) return;
      if (this.phase !== 'pause_requested') return;
      if (this.inFlight.size !== 0) return;
      this.confirmIfClear();
    });
  }

  /**
   * The confirmation decision itself: quiescent *and* background work clear.
   *
   * Shared by the request path and the late settle edge so both answer the
   * question with identical rules — a second copy of this predicate is how the two
   * paths would drift into disagreeing about what `paused` means.
   *
   * Returns the reason confirmation was withheld, or `null` once confirmed.
   */
  private confirmIfClear(): string | null {
    const background = this.backgroundWorkProbe();
    if (background === null) {
      return 'background work behind completed tools is not observable';
    }
    if (background > 0) {
      return `${background} background task(s) still running`;
    }
    this.phase = 'paused';
    this.onEvent({ type: 'pause_confirmed' });
    return null;
  }

  /**
   * Request a pause.
   *
   * Publishes `pause_requested` and closes admission *before* awaiting anything,
   * so the operator-visible state and the actual barrier change together. Only
   * after admitted work has settled and background work is observably clear does
   * it confirm.
   */
  async requestPause(options: { timeoutMs?: number; isCurrent?: () => boolean } = {}): Promise<PauseGateResult> {
    // Split into two serialized sections with the wait *between* them, rather
    // than one section spanning the wait. Holding the transition lock across
    // `awaitQuiescence` would queue `resume()` behind a pause that is itself
    // waiting — an operator who paused a long-running tool and changed their mind
    // could not resume until the settle timeout elapsed, which reads as a frozen
    // dashboard. The epoch checked in the second section is what keeps the
    // interleaving safe.
    const started = await this.serialize(async () => {
      if (options.isCurrent && !options.isCurrent()) {
        return { result: { outcome: 'unavailable' as const, reason: 'the accepting attempt ended before pause admission' } };
      }
      return this.beginPause(options.timeoutMs);
    });
    if ('result' in started) return started.result;

    const settled = await this.awaitQuiescence();

    return this.serialize(async () => this.finishPause(started.epoch, settled));
  }

  /** Synchronous half: validate, then close admission and publish the request. */
  private beginPause(timeoutMs?: number): { result: PauseGateResult } | { epoch: number } {
    if (this.phase === 'cancelled') {
      return { result: this.unavailable('cancelled', 'run is cancelled') };
    }
    if (this.breached) {
      // Sticky for the rest of the run, deliberately. The harness has already
      // demonstrated once that it will stop waiting for this barrier and run the
      // tool anyway, so a later "Paused" resting on the same mechanism would be a
      // quiescence claim from a device known to leak. Refusing is the honest
      // answer; re-enabling would need evidence we cannot obtain from in here.
      return {
        result: this.unavailable(
          'barrier_timeout',
          'the admission barrier was overridden by the harness earlier in this run',
        ),
      };
    }
    if (this.phase === 'paused') {
      // Already confirmed: idempotent rather than an error, because a retried
      // command must not turn a good pause into a failure.
      return { result: { outcome: 'confirmed' } };
    }
    if (this.phase === 'pause_requested') {
      // Join the existing epoch. A repeated command owns neither a new budget
      // nor the timer, and cannot reject a barrier that is still holding work.
      return { epoch: this.pauseEpoch };
    }

    const budget = this.safeBudget(timeoutMs);
    if (budget === null) {
      return {
        result: this.unavailable(
          'no_safe_budget',
          'no safe time remains before the run deadline to hold a pause',
        ),
      };
    }

    if (this.phase === 'running') {
      this.phase = 'pause_requested';
      this.pauseEpoch += 1;
      this.onEvent({ type: 'pause_requested' });
      this.armExpiry(budget, this.pauseEpoch);
    }
    return { epoch: this.pauseEpoch };
  }

  /** Confirmation half: only reachable once the quiescence wait has resolved. */
  private finishPause(epoch: number, settled: boolean): PauseGateResult {
    // Resume/abort/breach may have landed while we waited; that transition owns
    // the outcome and this request must not overwrite it.
    if (epoch !== this.pauseEpoch || !this.isPauseActive()) {
      // This is an old waiter's result, not a transition of the current gate.
      return { outcome: 'unavailable', reason: 'pause superseded before confirmation' };
    }
    if (!settled) {
      // Bounded wait elapsed with work still admitted. Reported as `requested`
      // rather than failed: admission *is* closed, so this is a true "pausing" —
      // and `settle` re-drives confirmation once the straggler finishes, so this
      // state resolves itself rather than waiting for another command.
      return {
        outcome: 'requested',
        reason: `still waiting for ${this.inFlight.size} admitted tool(s) to finish`,
      };
    }

    const withheld = this.confirmIfClear();
    if (withheld !== null) return { outcome: 'requested', reason: withheld };
    return { outcome: 'confirmed' };
  }

  /**
   * Resume: release a confirmed pause or cancel a pending one, exactly once.
   *
   * A resume arriving before any pause is a no-op returning `false` rather than
   * an error — an operator double-clicking resume, or a resume racing ahead of
   * its pause, is ordinary and must not fail the run.
   *
   * Returns whether this call was the one that released a pause.
   */
  async resume(): Promise<boolean> {
    return this.serialize(async () => {
      if (!this.isPauseActive()) return false;
      this.releasePause(false);
      return true;
    });
  }

  /** End only the pause owned by a departing attempt, without admitting its tools. */
  invalidateAttempt(): void {
    if (!this.isPauseActive()) return;
    this.phase = 'running';
    this.pauseEpoch += 1;
    this.clearExpiry();
    const reason = 'the paused attempt ended; same-execution resume is unavailable';
    for (const entry of [...this.parked]) entry.release('deny', reason);
    for (const waiter of [...this.quiescenceWaiters]) waiter();
    this.quiescenceWaiters.clear();
    this.wakeOutput();
    this.onEvent({ type: 'pause_unavailable', failure: 'attempt_ended', reason });
  }

  /**
   * Cancel the barrier for an aborting run.
   *
   * Held work is **denied, not admitted**: an abort that flushed its parked
   * tools on the way out would run exactly the side effects the operator aborted
   * to prevent. No auto-resume annotation is produced.
   */
  cancel(reason = 'run aborted'): void {
    if (this.phase === 'cancelled') return;
    this.phase = 'cancelled';
    this.wakeOutput();
    this.pauseEpoch += 1;
    this.clearExpiry();
    for (const entry of [...this.parked]) entry.release('deny', reason);
    const waiters = [...this.quiescenceWaiters];
    this.quiescenceWaiters.clear();
    for (const waiter of waiters) waiter();
    this.log(`[pause-gate] cancelled: ${reason}`);
  }

  /**
   * Compute the pause duration actually available.
   *
   * `null` means no positive safe budget remains, which the design requires be
   * rejected rather than silently shortened: a pause that expires the instant it
   * begins looks to an operator like a pause that never happened.
   */
  safeBudget(requestedMs?: number): number | null {
    const requested = requestedMs === undefined ? this.defaultTimeoutMs : requestedMs;
    if (!Number.isFinite(requested) || requested <= 0) return null;
    const deadline = this.deadlineAt();
    if (deadline === null) return requested;
    const remaining = deadline - this.now() - this.finalizationMarginMs;
    if (remaining <= 0) return null;
    return Math.min(requested, remaining);
  }

  /** Release a pause, optionally because its budget expired. */
  private releasePause(expired: boolean): void {
    // A pause that expires *without ever having confirmed* has to say so before it
    // says it was released. Otherwise the only events an operator's dashboard ever
    // sees are `pause_requested` then `pause_released`: they pressed Pause, watched
    // "pausing…" for the full budget, and the run resumed without anything ever
    // reporting that the pause did not take. `pause_released` is the end of a pause
    // that happened; this is the end of one that did not.
    //
    // Scoped to expiry on purpose. An operator resume of a still-pending pause is
    // *their own* decision, and the journal records that as `cancelled` — calling
    // it `unavailable` would blame the mechanism for a choice the operator made.
    if (expired && this.phase === 'pause_requested') {
      // Name the blocker that actually held the pause. Confirmation has two
      // independent blockers and reporting the wrong one is not cosmetic: this
      // string is what an operator reads to decide whether retrying is worth
      // anything. "Waiting for admitted work" invites a retry; "cannot observe
      // background work" tells them a retry will do exactly the same thing.
      const stillAdmitted = this.inFlight.size > 0;
      const background = stillAdmitted ? null : this.backgroundWorkProbe();
      this.onEvent({
        type: 'pause_unavailable',
        failure: stillAdmitted ? 'settle_timeout' : 'background_work',
        reason: stillAdmitted
          ? `the pause budget expired with ${this.inFlight.size} admitted tool(s) still short of a safe boundary`
          : background === null
            ? 'the pause budget expired while background work behind completed tools stayed unobservable'
            : `the pause budget expired with ${background} background task(s) still running`,
      });
    }
    this.phase = 'running';
    this.pauseEpoch += 1;
    this.clearExpiry();
    const held = [...this.parked];
    for (const entry of held) entry.release('admit');
    this.onEvent({ type: 'pause_released', expired });
    this.wakeOutput();
    this.log(`[pause-gate] released${expired ? ' (expired)' : ''}, admitting ${held.length} held tool(s)`);
  }

  /**
   * Arm the expiry timer for one pause.
   *
   * The epoch check makes a late timer inert: a pause released normally, then a
   * second pause requested, must not be torn down by the first pause's timer.
   */
  private armExpiry(budgetMs: number, epoch: number): void {
    this.clearExpiry();
    this.expiryTimer = this.scheduler.setTimer(() => {
      this.expiryTimer = null;
      if (this.pauseEpoch !== epoch || !this.isPauseActive()) return;
      void this.serialize(async () => {
        if (this.pauseEpoch !== epoch || !this.isPauseActive()) return;
        this.releasePause(true);
      });
    }, budgetMs);
  }

  private clearExpiry(): void {
    if (this.expiryTimer !== null) {
      this.scheduler.clearTimer(this.expiryTimer);
      this.expiryTimer = null;
    }
  }

  /** Wait, bounded, for admitted work to drain. `false` on timeout. */
  private awaitQuiescence(): Promise<boolean> {
    if (this.inFlight.size === 0) return Promise.resolve(true);
    return new Promise<boolean>((resolve) => {
      let done = false;
      const finish = (settled: boolean) => {
        if (done) return;
        done = true;
        this.quiescenceWaiters.delete(waiter);
        this.scheduler.clearTimer(timer);
        resolve(settled);
      };
      const waiter = () => finish(this.inFlight.size === 0);
      this.quiescenceWaiters.add(waiter);
      const timer = this.scheduler.setTimer(() => finish(false), this.settleTimeoutMs);
    });
  }

  private issueTicket(toolName: string): AdmissionTicket {
    this.ticketSeq += 1;
    const ticket: AdmissionTicket = { id: this.ticketSeq, toolName };
    this.inFlight.set(ticket.id, ticket);
    this.onEvent({ type: 'active_work', count: this.inFlight.size });
    return ticket;
  }

  /**
   * Record that the harness overrode the barrier, and stop claiming a pause.
   *
   * The state change is immediate rather than deferred to the next pause
   * request, because the breach may happen while the run is already reporting
   * `paused`: the operator is being told nothing can start at the very moment a
   * tool has been let through. Standing the pause down and emitting
   * `pause_unavailable` replaces a false claim with a true one plus a reason.
   */
  private markBreached(toolName: string): void {
    const wasClaiming = this.isPauseActive();
    this.breached = true;
    this.log(`[pause-gate] barrier abandoned while holding ${toolName}`);
    if (!wasClaiming) return;

    // Stand the pause down, but *deny* the remaining held admissions instead of
    // admitting them the way a deliberate resume does. Two reasons: the operator
    // never asked for this work to proceed, and one of those entries is the abort
    // handler's own — admitting it would resolve the same call both `deny` (from
    // the breach) and `admit` (from the release), whichever landed first winning
    // silently.
    this.phase = 'running';
    this.pauseEpoch += 1;
    this.clearExpiry();
    for (const entry of [...this.parked]) {
      entry.release('deny', 'pause barrier was overridden by the harness');
    }
    // Wake anything waiting on quiescence so the pending request reports the
    // breach instead of blocking until its settle timeout.
    const waiters = [...this.quiescenceWaiters];
    this.quiescenceWaiters.clear();
    for (const waiter of waiters) waiter();
    this.onEvent({
      type: 'pause_unavailable',
      failure: 'barrier_timeout',
      reason: 'the harness released a held tool before the pause took effect',
    });
    this.wakeOutput();
  }

  private unavailable(failure: PauseGateFailure, reason: string): PauseGateResult {
    this.onEvent({ type: 'pause_unavailable', failure, reason });
    return { outcome: 'unavailable', reason };
  }

  /** Run one transition at a time, in call order. */
  private serialize<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.transitionTail.then(fn, fn);
    this.transitionTail = next.catch(() => {});
    return next;
  }
}
