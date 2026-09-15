/**
 * Claude Agent SDK control adapter — Issue #3962 (S3), first production adapter.
 *
 * This is the only file in the control path allowed to know that the harness is
 * Claude. Everything provider-shaped is deliberately concentrated here: the
 * `SDKUserMessage` construction, the `shouldQuery` flag, the open async iterable
 * a streaming query needs, `options.resume` and the native `Query` handle.
 * Consumers above talk to {@link ControlRuntimeAdapter} and cannot tell which
 * harness is underneath — which is the property that lets a second harness
 * arrive later without touching pause, abort or steering code.
 *
 * ## Handle ownership
 *
 * `resilientQuery` owns the SDK session handle: it creates it and closes it in
 * the `finally` ending every attempt. This adapter *borrows* that handle and
 * closes only the input channel it created itself. Both sides had a correct
 * dispose-once guard and a double close still happened, because each guard fires
 * once per owner and there were two owners — see
 * {@link ClaudeAttemptEndpoint.dispose}.
 *
 * ## The two Claude-specific translations
 *
 * **Annotation vs steering** maps onto one SDK field. `shouldQuery: false`
 * appends a message to the transcript without triggering an assistant turn,
 * which is exactly ADP's "annotation": record context, do not start work.
 * Steering sends `shouldQuery: true`. Both carry `origin: {kind: 'human'}`,
 * preserving trusted actor attribution — the model sees operator input as
 * operator input rather than as synthetic self-talk.
 *
 * **An open iterable per attempt.** The SDK accepts `string | AsyncIterable`,
 * and only the iterable form can receive a second turn after the query starts.
 * So each attempt gets a fresh {@link AttemptInputChannel}: one bootstrap message and a
 * direct handoff to a waiting SDK reader. It must be *fresh* per attempt because
 * an iterable a previous query already consumed is exhausted — reusing it would
 * produce an attempt that looks live and can never receive input.
 *
 * ## The third Claude-specific translation: the pause barrier (#3961)
 *
 * S2 added `pause`/`resume`, and the shape of the addition is the point. The
 * *decision* — may this run claim to be paused? — lives in the harness-neutral
 * {@link PauseGate}. What lives here is only the translation: `PreToolUse` becomes
 * the admission barrier, a denied admission becomes `permissionDecision: 'deny'`
 * with an operator-facing reason, `PostToolUse` settles the admission, and
 * `Stop`/`SubagentStop`'s `background_tasks` answers "is anything still running
 * behind a finished tool?". An expired pause becomes an annotation
 * (`shouldQuery: false`) — a fact recorded without starting a turn — because the
 * operator gave no new instruction and their silence must not read as one.
 *
 * No `Query.interrupt()` is called anywhere, still: an interrupted turn followed
 * by a new one is a different product behaviour than pause, and adopting it
 * silently would break the "same live execution and context" guarantee. Pause here
 * stops *admission*, not the turn.
 *
 * `steer` and `abort` remain unsupported. The adapter can carry input, but
 * carrying input is not a delivered control, and their runtime proofs are S4's and
 * S6's.
 */
import type { HookInput, SDKUserMessage } from '@anthropic-ai/claude-agent-sdk';
import type { ControlAction } from '../control-state';
import type { AdmissionTicket, PauseGate } from '../pause-gate';
import {
  CONTROL_PROTOCOL_VERSION,
  ControlCancelledError,
  CurrentAttemptRegistry,
  IMPLEMENTED_CONTROL_VERBS,
  boundReason,
  intersectCapabilities,
  newAttemptId,
  noVerbsSupported,
  type AttemptEndpoint,
  type AttemptId,
  type ControlInput,
  type ControlRuntimeAdapter,
  type ControlRuntimeListener,
  type HarnessDescriptor,
  type InputHandoffResult,
  type PauseResult,
  type VerbSupport,
} from '../control-runtime';

/** Adapter identity. Consumers must never branch on this — it is for logs/evidence. */
export const CLAUDE_ADAPTER_ID = 'claude';

/**
 * Pinned SDK version this adapter was proven against (matches the lockfile).
 *
 * Recorded because the streaming-input and `shouldQuery` behaviours it relies on
 * are observed SDK behaviour, not a documented permanent guarantee. A version
 * bump is a prompt to re-run the contract suite, not a no-op.
 */
export const CLAUDE_SDK_VERSION = '0.3.220';

/**
 * Reason `pause`/`resume` report unsupported when no gate is installed (#3961).
 *
 * The gate is optional and its absence is the honest "no" rather than an error:
 * the barrier *is* the `PreToolUse` hook, so a run whose query options never
 * received {@link claudeControlToolHooks} has nothing standing between the model
 * and a `Bash` call. Answering `unavailable` there keeps the promise proportional
 * to the mechanism actually in place.
 */
const NO_PAUSE_GATE_REASON =
  'pause needs the admission barrier installed in this run: no tool-boundary gate is present';

/**
 * Slack added to the barrier's hook timeout above the pause budget itself.
 *
 * A hook timeout equal to the budget races the expiry timer, and the CLI winning
 * that race aborts the parked call — which the gate must read as a breach, because
 * an abandoned park means the tool may run. Sixty seconds is cheap here: the
 * timeout is an upper bound on waiting, not a delay anything pays when a pause ends
 * normally.
 */
const PAUSE_HOOK_TIMEOUT_MARGIN_SECONDS = 60;

/** Reason steer/abort remain unsupported after S2. Their proofs are S4/S6's. */
const NOT_YET_PROVEN_REASON =
  'no proven runtime boundary for this verb yet: steering and abort are later stories';

/**
 * What the model is told when a pause expires and the run continues by itself.
 *
 * Sent as an **annotation** (`shouldQuery: false`), which is the whole reason this
 * mapping belongs in the adapter: the gate publishes a neutral "the pause ended
 * because its budget ran out" fact, and only Claude's transport knows that
 * recording a fact without starting a turn is spelled `shouldQuery: false`. Were
 * it sent as steering, an expiring pause would inject a new instruction into a run
 * that never asked for one — the operator's silence would read as a command.
 */
export const PAUSE_EXPIRY_ANNOTATION =
  'Operator pause expired after its time budget and the run resumed automatically. ' +
  'No new instruction was given; continue the work you were already doing.';

/** One bootstrap message, then direct handoff to a waiting SDK reader.
 * Operator commands remain in the shared authorized queue until a reader exists.
 */
export class AttemptInputChannel {
  readonly attemptId = newAttemptId();
  private reader: ((value: IteratorResult<SDKUserMessage>) => void) | null = null;
  private closed = false;

  constructor(private initial?: SDKUserMessage) {}

  push(message: SDKUserMessage): boolean {
    if (this.closed || !this.reader) return false;
    const reader = this.reader;
    this.reader = null;
    reader({ value: message, done: false });
    return true;
  }

  close(): void {
    this.closed = true;
    this.initial = undefined;
    const reader = this.reader;
    this.reader = null;
    reader?.({ value: undefined, done: true });
  }

  isClosed(): boolean {
    return this.closed;
  }

  iterable(): AsyncIterableIterator<SDKUserMessage> {
    return {
      [Symbol.asyncIterator]() { return this; },
      next: () => {
        if (this.closed) return Promise.resolve({ value: undefined, done: true });
        if (this.initial) {
          const value = this.initial;
          this.initial = undefined;
          return Promise.resolve({ value, done: false });
        }
        if (this.reader) return Promise.reject(new Error('concurrent input reads are unsupported'));
        return new Promise<IteratorResult<SDKUserMessage>>((resolve) => { this.reader = resolve; });
      },
      return: async () => {
        this.close();
        return { value: undefined, done: true };
      },
    };
  }
}

/** Build the SDK message for one neutral input. The whole provider translation. */
export function toSdkUserMessage(input: ControlInput, sessionId = ''): SDKUserMessage {
  return {
    type: 'user',
    message: { role: 'user', content: input.text },
    parent_tool_use_id: null,
    // Trusted actor attribution (ADR-3 / FR-6.2): operator input is human input.
    origin: { kind: 'human' },
    // The single field carrying the annotation/steering distinction.
    // annotation -> false: recorded without starting an assistant turn.
    // steering    -> true: may request work.
    shouldQuery: input.kind === 'steering',
    session_id: sessionId,
  } as SDKUserMessage;
}

/** A live Claude query handle. Structural, so tests need no real SDK process. */
export interface ClaudeSessionHandle {
  close(): void;
}

/**
 * Tool inputs that ask Claude to leave work running behind the tool call.
 *
 * Read from `tool_input`, which the SDK types as `unknown`. Only two shapes
 * matter and both are checked defensively: a shell told to background itself, and
 * a delegating tool whose child keeps working after the parent returns.
 */
function requestsBackgroundWork(toolName: string, toolInput: unknown): boolean {
  if (toolName === 'Task') return true;
  if (toolInput === null || typeof toolInput !== 'object') return false;
  const input = toolInput as { run_in_background?: unknown };
  return input.run_in_background === true;
}

/**
 * Claude's answer to "is anything still running behind a finished tool?" (#3961).
 *
 * The gate refuses to confirm a pause on an unobservable answer, so the useful
 * question is when this observer is *entitled* to say zero. Three states, and the
 * ordering between them is the entire content of this class:
 *
 * - Nothing in this session ever asked for background work → `0`. Not an
 *   assumption: a tool that never requested backgrounding has nothing behind it,
 *   and `PreToolUse` sees every tool call before it runs.
 * - Something did, and a `Stop`/`SubagentStop` has reported `background_tasks`
 *   *since* then → that reported count.
 * - Something did, and no report has arrived since → `null`.
 *
 * The third case is the one worth being pedantic about. A backgrounded `Bash`
 * returns to the model immediately while its process keeps writing; a `Task`
 * returns a summary while its subagent may still hold a file handle. Reporting `0`
 * there would tell an operator that nothing is touching their repository at the
 * exact moment something is. Sequence numbers rather than booleans because the
 * interesting case is a *second* spawn after an all-clear: the old report must not
 * keep vouching for work started after it was written.
 */
export class ClaudeBackgroundWorkObserver {
  private seq = 0;
  private lastSpawnSeq = 0;
  private lastReportSeq = 0;
  private lastReportedCount = 0;

  /** Note a tool call that may leave work running behind it. */
  noteToolStart(toolName: string, toolInput: unknown): void {
    this.seq += 1;
    if (requestsBackgroundWork(toolName, toolInput)) this.lastSpawnSeq = this.seq;
  }

  /** Record a `Stop`/`SubagentStop` report of in-flight background work. */
  noteBackgroundReport(tasks: unknown): void {
    this.seq += 1;
    this.lastReportSeq = this.seq;
    this.lastReportedCount = Array.isArray(tasks) ? tasks.length : 0;
  }

  /** The gate's `backgroundWorkProbe`: a count, or `null` for "cannot tell". */
  count(): number | null {
    if (this.lastSpawnSeq === 0) return 0;
    if (this.lastReportSeq > this.lastSpawnSeq) return this.lastReportedCount;
    return null;
  }
}

/**
 * The Claude-shaped hook callbacks that make a {@link PauseGate} real.
 *
 * Deliberately returned as three named callbacks rather than as an SDK `hooks`
 * object: `PostToolUse` is already occupied by spill and the developer checkpoint
 * reminder, and those compose by merging `hookSpecificOutput` inside a single
 * callback. Handing back a second array entry for the same event would put the
 * merge semantics in the CLI's hands, where `updatedToolOutput` — the spilled
 * payload's locator — is what would quietly go missing.
 */
export interface ClaudePauseHooks {
  /**
   * Seconds the `PreToolUse` matcher must be allowed to block — Issue #3961.
   *
   * The CLI enforces hook timeouts in its own subprocess and applies a default
   * when a matcher does not set one. The barrier's whole job is to park a tool for
   * as long as the operator holds the pause, so any default shorter than the pause
   * budget would abort the park, breach the barrier and collapse *every* long pause
   * to `unavailable`. Publishing the required bound here, derived from the gate's
   * own budget, keeps the two from drifting apart.
   */
  readonly preToolUseTimeoutSeconds: number;
  /** `PreToolUse`: the admission barrier. Denies a tool the operator paused. */
  preToolUse(input: HookInput, toolUseId?: string, options?: { signal: AbortSignal }): Promise<Record<string, unknown>>;
  /** `PostToolUse` / `PostToolUseFailure`: settle the admission for one tool. */
  postToolUse(input: HookInput): Promise<Record<string, unknown>>;
  /** `Stop` / `SubagentStop`: observe background work behind finished tools. */
  onStop(input: HookInput): Promise<Record<string, unknown>>;
}

/**
 * Translate one pause gate into Claude hook callbacks (#3961).
 *
 * This function is the entire Claude-specific half of pause. The gate decides
 * whether a tool may start; this decides how Claude is told — `permissionDecision:
 * 'deny'` with a reason, which the model surfaces as a tool result rather than as
 * a crash, so a paused run reads as "that was not allowed right now" instead of
 * as a failure the model tries to route around.
 *
 * Note what is *not* here: no `Query.interrupt()`, no session restart, no replay.
 * The turn stays exactly where it was, which is what makes resume a continuation
 * of the same execution and the same context.
 */
export function createClaudePauseHooks(
  gate: PauseGate,
  observer: ClaudeBackgroundWorkObserver = new ClaudeBackgroundWorkObserver(),
): ClaudePauseHooks {
  /** tool_use_id → the admission it holds, so completion settles the right one. */
  const outstanding = new Map<string, AdmissionTicket>();

  const toolFields = (input: HookInput) =>
    input as unknown as { tool_name?: string; tool_input?: unknown; tool_use_id?: string };

  return {
    // Ceiling plus a margin, converted to the seconds the matcher expects. The
    // margin matters: equal values race, and losing that race is indistinguishable
    // from a genuine barrier breach — the failure it would cause is the one this
    // number exists to prevent.
    preToolUseTimeoutSeconds: Math.ceil(gate.maxParkDurationMs() / 1000) + PAUSE_HOOK_TIMEOUT_MARGIN_SECONDS,

    async preToolUse(input, toolUseId, options) {
      const fields = toolFields(input);
      const toolName = fields.tool_name ?? 'tool';
      observer.noteToolStart(toolName, fields.tool_input);

      // `options.signal` is the CLI subprocess's abandonment signal for this
      // callback. It is the only way a hook timeout reaches JS — the timeout is
      // enforced on the CLI side and surfaces here purely as an abort — which is
      // why the gate treats an aborted park as a breached barrier rather than as
      // a tool that politely declined to run.
      const result = await gate.admit(toolName, options?.signal);
      const id = fields.tool_use_id ?? toolUseId;
      if (result.decision === 'admit') {
        if (id && result.ticket) outstanding.set(id, result.ticket);
        // `{}` rather than an explicit allow: an allow decision would override a
        // deny from another PreToolUse hook or a permission rule, turning a pause
        // barrier into an escalation of privilege.
        return {};
      }
      return {
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'deny',
          permissionDecisionReason: result.reason ?? 'the run is paused by an operator',
        },
      };
    },

    async postToolUse(input) {
      const fields = toolFields(input);
      const id = fields.tool_use_id;
      if (!id) return {};
      const ticket = outstanding.get(id);
      outstanding.delete(id);
      // The gate ignores an unknown or repeated ticket, so a harness that emits
      // both a completion and a failure edge for one tool cannot drive the
      // in-flight count below the truth.
      gate.settle(ticket);
      return {};
    },

    async onStop(input) {
      const stop = input as unknown as { background_tasks?: unknown };
      observer.noteBackgroundReport(stop.background_tasks);
      // Settle whatever the harness never reported a completion for. The turn has
      // ended, so nothing is still executing *inside* it; a ticket surviving to
      // here is a missing edge (an interrupted tool, a crashed hook), and leaving
      // it outstanding would make every later pause wait for a tool that finished
      // long ago. Background work is not covered by this and deliberately stays
      // the observer's business — that is the claim we must not fabricate.
      for (const ticket of [...outstanding.values()]) gate.settle(ticket);
      outstanding.clear();
      return {};
    },
  };
}

export interface ClaudeControlAdapterOptions {
  /** Verbs ADP has implemented. Defaults to the (empty) S3 set. */
  implementedVerbs?: ReadonlySet<ControlAction>;
  log?: (msg: string) => void;
  /**
   * The neutral pause coordinator backing this adapter's pause verbs (#3961).
   *
   * Optional, and its absence is what keeps pause unsupported: with no gate
   * there is no admission barrier, so `requestPause` reports `unavailable` for
   * the same reason S3 did. A run that wants pause constructs the gate, installs
   * {@link createClaudePauseHooks} in its query options, and passes the same gate
   * here. Nothing about the gate is Claude-shaped — this adapter supplies the
   * `PreToolUse`/`PostToolUse` translation.
   */
  pauseGate?: PauseGate;
}

/**
 * One Claude attempt's transport.
 *
 * Owns the SDK handle and the input channel for exactly one `query()`. Created
 * per attempt; never reused, because both of the things it owns are
 * single-use.
 */
class ClaudeAttemptEndpoint implements AttemptEndpoint {
  readonly attemptId: AttemptId;
  private readonly channel: AttemptInputChannel;
  private readonly session: ClaudeSessionHandle | null;
  private readonly log: (msg: string) => void;
  private readonly pauseGate?: PauseGate;

  constructor(args: {
    attemptId: AttemptId;
    channel: AttemptInputChannel;
    session: ClaudeSessionHandle | null;
    log: (msg: string) => void;
    pauseGate?: PauseGate;
  }) {
    this.attemptId = args.attemptId;
    this.channel = args.channel;
    this.session = args.session;
    this.log = args.log;
    this.pauseGate = args.pauseGate;
  }

  /**
   * Physically hand input to this attempt's SDK transport.
   *
   * Synchronous push, deliberately: the authorization revalidation that must
   * happen immediately before delivery lives in the shared journal's
   * `deliverAuthorized`, and it requires no `await` between the bounded check
   * and the handoff. An adapter-side buffer with its own async gap would be a
   * hidden queue that bypasses revocation.
   */
  async deliver(input: ControlInput): Promise<InputHandoffResult> {
    if (this.channel.isClosed()) return 'rejected';
    return this.channel.push(toSdkUserMessage(input)) ? 'delivered' : 'rejected';
  }

  // `AttemptEndpoint`'s optional `requestPause`/`releasePause` are deliberately
  // not implemented here — Issue #3961. They look like the natural place for a
  // per-attempt barrier, and an earlier revision of this story did implement
  // them. Nothing called them: the registry exposes no pause path, and pause
  // enters through `ClaudeControlAdapter.requestPause`, which first consults the
  // three-way capability *intersection*. An endpoint-level entry point would
  // reach the gate without that check — and since the gate can genuinely hold
  // tools, it would confirm a pause for a verb ADP had not enabled. That is the
  // precise failure the intersection exists to prevent, so the second door is
  // left unbuilt rather than built and guarded.
  //
  // `activeWorkCount` below *is* implemented, because the registry does call it.

  /**
   * Tool invocations admitted and not yet finished.
   *
   * `null` without a gate — "cannot see", which the coordinator must not render
   * as the quiescence claim `0`. With a gate the count is genuinely observed at
   * the admission boundary, so `0` is a fact rather than an assumption.
   */
  activeWorkCount(): number | null {
    return this.pauseGate ? this.pauseGate.activeToolCount() : null;
  }

  /**
   * Tear down this attempt's transport.
   *
   * Closes the input channel only. The SDK session handle is **borrowed, not
   * owned**: `resilientQuery` creates it and closes it unconditionally in the
   * `finally` that ends every attempt, on every path including idle timeout and
   * consumer abandonment (FR-5.6).
   *
   * Closing it here as well was a real double-close, caught by the integrated
   * test in `resilientQuery.test.ts` that drives this adapter through the real
   * wrapper. The sequence on any retry was: the wrapper's `finally` closes
   * attempt N's session, then attaching attempt N+1 disposes the replaced
   * endpoint, which closed the same handle a second time. Neither side's
   * internal guard could see the other — the endpoint's own flag and the
   * registry's dispose-once ledger both correctly fire once *per owner*, and the
   * bug was that there were two owners.
   *
   * A single owner is the fix rather than a shared flag, because the wrapper's
   * close cannot move: 17 existing callers depend on that `finally` being
   * byte-identical. So the rule is one line long and checkable — whoever creates
   * the handle closes it.
   */
  async dispose(): Promise<void> {
    this.channel.close();
    // `session` is retained for diagnostics and to keep the borrowed-handle
    // relationship explicit at the type level; it is deliberately never closed.
    void this.session;
  }
}

/**
 * The Claude control adapter.
 *
 * Retry-safety lives in the shared {@link CurrentAttemptRegistry} rather than
 * here, so the staleness rules are identical for every harness instead of being
 * re-derived (and eventually mis-implemented) per provider. This class supplies
 * the Claude-shaped parts and delegates the rules.
 */
export class ClaudeControlAdapter implements ControlRuntimeAdapter {
  private readonly registry = new CurrentAttemptRegistry();
  private readonly implementedVerbs: ReadonlySet<ControlAction>;
  private readonly log: (msg: string) => void;
  private readonly pauseGate?: PauseGate;
  /** The channel for the attempt currently being built, before its handle exists. */
  private pendingChannel: AttemptInputChannel | null = null;
  private activeChannel: AttemptInputChannel | null = null;
  /** Attachment completion for the wrapper and direct adapter callers. */
  private pendingAttach: Promise<void> = Promise.resolve();

  constructor(options: ClaudeControlAdapterOptions = {}) {
    this.implementedVerbs = options.implementedVerbs ?? IMPLEMENTED_CONTROL_VERBS;
    this.log = options.log ?? (() => {});
    this.pauseGate = options.pauseGate;
    this.registry.cancellationSignal.addEventListener('abort', () => {
      this.activeChannel?.close();
      // Deny anything held at the barrier. An abort that flushed its parked tools
      // on the way out would run exactly the side effects the operator aborted to
      // prevent — the one case where "let the queued work finish" is wrong.
      this.pauseGate?.cancel('run cancelled');
    }, { once: true });
    // Release is observed rather than reported by whoever called `resume()`, and
    // that is what makes "released exactly once" structural: the gate releases a
    // pause once, so exactly one event follows, whether the release came from an
    // operator resume, a double-clicked resume, or the expiry timer. Emitting from
    // the resume path instead would need every caller to agree not to double-emit.
    this.pauseGate?.subscribe((event) => {
      if (event.type !== 'pause_released') return;
      const attemptId = this.registry.currentAttemptId();
      if (attemptId) this.registry.emit({ type: 'pause_released', attemptId });
      // An expired pause is the only transition the operator did not ask for, so
      // it is the only one the run has to explain to the model. The gate publishes
      // the neutral fact; this is where it becomes a Claude annotation.
      if (event.expired) void this.annotateExpiry();
    });
  }

  /**
   * Adapter-declared support, before ADP/runtime intersection.
   *
   * `pause`/`resume` are claimed only with a gate installed, because the claim is
   * about a mechanism rather than about a build: two runs of the same binary, one
   * with the barrier hooked up and one without, honestly differ here. `steer` and
   * `abort` stay false — their runtime proofs are S4's and S6's, and this story
   * widening them would be the "advertised but unproven" failure it exists to
   * avoid.
   */
  adapterCapabilities(): Record<ControlAction, VerbSupport> {
    if (!this.pauseGate) return noVerbsSupported(NO_PAUSE_GATE_REASON);
    const unproven = boundReason(NOT_YET_PROVEN_REASON);
    return {
      pause: { supported: true },
      resume: { supported: true },
      steer: { supported: false, reason: unproven },
      abort: { supported: false, reason: unproven },
    };
  }

  describe(): HarnessDescriptor {
    return {
      protocolVersion: CONTROL_PROTOCOL_VERSION,
      adapterId: CLAUDE_ADAPTER_ID,
      adapterVersion: CLAUDE_SDK_VERSION,
      capabilities: this.adapterCapabilities(),
    };
  }

  /** Effective capabilities: the three-way intersection. All false in S3. */
  capabilities(): Record<ControlAction, boolean> {
    return intersectCapabilities({
      implemented: this.implementedVerbs,
      adapter: this.adapterCapabilities(),
      // Availability requires a live attempt: with no attempt attached there is
      // nothing a verb could act on, so nothing may be advertised.
      available: this.registry.currentAttemptId() === null ? new Set<ControlAction>() : undefined,
    });
  }

  currentAttempt(): AttemptId | null {
    return this.registry.currentAttemptId();
  }

  subscribe(listener: ControlRuntimeListener): () => void {
    return this.registry.subscribe(listener);
  }

  /** Resolves against whatever attempt is current now — that is what survives a retry. */
  async submitInput(input: ControlInput): Promise<InputHandoffResult> {
    return this.registry.deliver(input);
  }

  /**
   * Pause the live attempt, reporting only what the barrier can prove (#3961).
   *
   * Consults the **intersection** rather than this adapter's own table. That is
   * not defensive coding: the gate below can genuinely hold tools, so calling it
   * directly would confirm a pause for a verb ADP had not enabled — the exact
   * failure the three-way intersection exists to prevent. The adapter does not get
   * the deciding vote on its own capability.
   *
   * Events mirror the outcome so the run's state store and the dashboard converge
   * on the same three-state answer instead of inferring `paused` from silence.
   */
  async requestPause(options?: { signal?: AbortSignal; timeoutMs?: number }): Promise<PauseResult> {
    const attemptId = this.registry.currentAttemptId();
    if (!attemptId) return { outcome: 'unavailable', reason: boundReason('no live attempt to pause') };
    if (!this.capabilities().pause) {
      const reason = this.adapterCapabilities().pause.reason ?? NO_PAUSE_GATE_REASON;
      return { outcome: 'unavailable', reason: boundReason(reason) };
    }
    if (!this.pauseGate) {
      return { outcome: 'unavailable', reason: boundReason(NO_PAUSE_GATE_REASON) };
    }
    if (options?.signal?.aborted) {
      return { outcome: 'unavailable', reason: boundReason('pause request was cancelled before it began') };
    }

    const gateResult = await this.pauseGate.requestPause({ timeoutMs: options?.timeoutMs });
    if (gateResult.outcome === 'requested') {
      this.registry.emit({ type: 'pause_requested', attemptId });
      return { outcome: 'requested' };
    }
    if (gateResult.outcome === 'confirmed') {
      this.registry.emit({ type: 'pause_confirmed', attemptId });
      return { outcome: 'confirmed' };
    }
    const reason = boundReason(gateResult.reason);
    this.registry.emit({ type: 'pause_unavailable', attemptId, reason });
    return { outcome: 'unavailable', reason };
  }

  /**
   * Release a confirmed pause, or cancel one still pending.
   *
   * Idempotent and never an error: an operator double-clicking resume, or a resume
   * that arrives before its pause has confirmed, is ordinary. The `pause_released`
   * event comes from the gate's own transition (see the constructor), not from
   * here, so a second resume that released nothing emits nothing.
   */
  async resumeFromPause(): Promise<void> {
    await this.pauseGate?.resume();
  }

  /**
   * Tell the model, without instructing it, that an expired pause has ended.
   *
   * Best-effort by design. Delivery can fail for entirely normal reasons — the
   * attempt may have been retried away or finished while paused — and an
   * undeliverable courtesy note must not turn an auto-resume into a failed one.
   * The run continuing is the contract; the annotation is context.
   */
  private async annotateExpiry(): Promise<void> {
    try {
      const result = await this.registry.deliver({ kind: 'annotation', text: PAUSE_EXPIRY_ANNOTATION });
      if (result !== 'delivered') {
        this.log(`claude adapter: pause-expiry annotation not delivered (${result})`);
      }
    } catch (err) {
      this.log(`claude adapter: pause-expiry annotation failed: ${(err as Error)?.message ?? err}`);
    }
  }

  cancel(reason?: string): void {
    this.registry.cancel(reason ?? 'claude control adapter cancelled');
    // Close the in-flight channel too: a cancellation during query construction
    // must not leave an input channel that a later push could still fill.
    this.pendingChannel?.close();
    this.pendingChannel = null;
  }

  isCancelled(): boolean {
    return this.registry.isCancelled();
  }

  async dispose(): Promise<void> {
    this.pendingChannel?.close();
    this.pendingChannel = null;
    await this.registry.dispose();
  }

  /** Observable work count for the current attempt (`null` while unobservable). */
  activeWorkCount(): number | null {
    return this.registry.activeWorkCount();
  }

  /**
   * The `attemptInputFactory` to hand to `resilientQuery`.
   *
   * Seeds the effective task or continuation prompt exactly once because the
   * iterable replaces queryParams.prompt. Later operator input is never buffered.
   */
  attemptInputFactory(): (context: { attemptNumber: number; isResume: boolean; promptText: string }) => {
    input: AsyncIterable<unknown>;
    dispose: () => Promise<void>;
  } {
    return (context) => {
      this.pendingChannel?.close();
      const channel = new AttemptInputChannel(
        context.promptText ? toSdkUserMessage({ kind: 'steering', text: context.promptText }) : undefined,
      );
      this.pendingChannel = channel;
      this.log(
        `claude adapter: fresh input channel for attempt ${context.attemptNumber}` +
          `${context.isResume ? ' (true resume)' : ' (initial/fallback)'}`,
      );
      return {
        input: channel.iterable(),
        dispose: async () => {
          channel.close();
          if (this.pendingChannel === channel) this.pendingChannel = null;
          await this.pendingAttach;
          await this.registry.detachCurrent(channel.attemptId);
        },
      };
    };
  }

  /**
   * The `onAttemptHandle` callback to hand to `resilientQuery`.
   *
   * Attaching here — at the moment `query()` returns — is what makes the
   * endpoint swap coincide with the actual transport swap. `attach` invalidates
   * and disposes the predecessor first, so the previous attempt stops accepting
   * input before the new one becomes reachable, and there is never a window
   * where two attempts both look live.
   */
  onAttemptHandle(): (handle: { attemptNumber: number; session: unknown }) => Promise<void> {
    return async (handle) => {
      const channel = this.pendingChannel;
      if (!channel) {
        // No factory ran for this attempt: nothing can receive input, so
        // publishing an endpoint would advertise a channel that does not exist.
        this.log('claude adapter: attempt handle with no input channel — not attaching');
        return;
      }
      this.pendingChannel = null;
      this.activeChannel = channel;
      const endpoint = new ClaudeAttemptEndpoint({
        attemptId: channel.attemptId,
        channel,
        session: (handle.session as ClaudeSessionHandle | null) ?? null,
        log: this.log,
        pauseGate: this.pauseGate,
      });
      // The wrapper awaits attachment before consuming output.
      this.pendingAttach = this.registry.attach(endpoint).then(
        () => {},
        (err: unknown) => {
          if (err instanceof ControlCancelledError) {
            this.log('claude adapter: attach refused — runtime already cancelled');
            return;
          }
          this.log(`claude adapter: attach failed: ${(err as Error)?.message ?? err}`);
        },
      );
      await this.pendingAttach;
    };
  }

  /** Await the latest attach; currentAttempt() distinguishes refusal. */
  async whenAttached(): Promise<void> {
    await this.pendingAttach;
  }

  /** The `cancellation` object to hand to `resilientQuery`. */
  cancellationSource(): { isCancelled: () => boolean; error: () => Error; signal: AbortSignal } {
    return {
      isCancelled: () => this.registry.isCancelled(),
      signal: this.registry.cancellationSignal,
      error: () => this.registry.cancellationError(),
    };
  }
}
