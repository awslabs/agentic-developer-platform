/**
 * Claude Agent SDK control adapter — Issue #3962 (S3), first production adapter.
 *
 * This is the only file in the control path allowed to know that the harness is
 * Claude. Everything provider-shaped is deliberately concentrated here: the
 * `SDKUserMessage` construction, the `shouldQuery` flag, the open async iterable
 * a streaming query needs, `options.resume`, the native `Query` handle and
 * `session.close()`. Consumers above talk to {@link ControlRuntimeAdapter} and
 * cannot tell which harness is underneath — which is the property that lets a
 * second harness arrive later without touching pause, abort or steering code.
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
 * So each attempt gets a fresh {@link AttemptInputChannel}: a queue plus a
 * promise the iterable awaits when empty. It must be *fresh* per attempt because
 * an iterable a previous query already consumed is exhausted — reusing it would
 * produce an attempt that looks live and can never receive input.
 *
 * ## What this story deliberately does not do
 *
 * All four verbs report unsupported. The adapter can carry input, but carrying
 * input is not a delivered control: a pause that closed admission without
 * proving tool quiescence would let the dashboard claim "Paused" while a `Bash`
 * call is still writing to the repository. That proof is S2's, with real tool
 * boundaries and hook timeouts. `pause`/`resume` therefore return
 * `unavailable` with a reason rather than a fabricated success, and no
 * `Query.interrupt()` is called anywhere — an interrupted turn followed by a new
 * one is a different product behaviour than pause, and adopting it silently
 * would break the "same live execution and context" guarantee.
 */
import type { SDKUserMessage } from '@anthropic-ai/claude-agent-sdk';
import type { ControlAction } from '../control-state';
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
 * Reason attached to every unsupported verb in this story.
 *
 * Phrased as the *missing proof* rather than "not implemented", because the
 * distinction is the point: the transport exists, the evidence does not.
 */
const S3_UNSUPPORTED_REASON =
  'control plumbing only in this stage: no verb has a proven runtime boundary yet';

/**
 * One attempt's input channel: a fresh open async iterable plus a disposer.
 *
 * A hand-rolled queue rather than a generator with an internal `while(true)`
 * because the iterable must be resolvable from *outside* — input arrives from
 * the control listener, not from the loop's own control flow.
 */
export class AttemptInputChannel {
  private readonly queue: SDKUserMessage[] = [];
  /** Resolver for an iterable currently parked on an empty queue. */
  private wake: (() => void) | null = null;
  private closed = false;

  /**
   * Push a message toward the harness.
   *
   * Returns false once closed, so a delivery into a torn-down attempt is a
   * refusal rather than a silent enqueue into a queue nobody will ever read.
   */
  push(message: SDKUserMessage): boolean {
    if (this.closed) return false;
    this.queue.push(message);
    // Wake a parked iterable. Cleared first so a throw in the consumer cannot
    // leave a stale resolver that swallows the next wake-up.
    const wake = this.wake;
    this.wake = null;
    wake?.();
    return true;
  }

  /** Close the channel: ends the iterable and refuses further input. */
  close(): void {
    if (this.closed) return;
    this.closed = true;
    const wake = this.wake;
    this.wake = null;
    wake?.();
  }

  isClosed(): boolean {
    return this.closed;
  }

  /**
   * The iterable handed to `query()`.
   *
   * Stays open while the attempt can accept commands, and ends when closed —
   * it does not wait forever after the run completes, which would hold the
   * query process open past its useful life.
   */
  async *iterable(): AsyncGenerator<SDKUserMessage> {
    while (true) {
      while (this.queue.length > 0) {
        yield this.queue.shift() as SDKUserMessage;
      }
      if (this.closed) return;
      await new Promise<void>((resolve) => {
        this.wake = resolve;
      });
    }
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

export interface ClaudeControlAdapterOptions {
  /** Verbs ADP has implemented. Defaults to the (empty) S3 set. */
  implementedVerbs?: ReadonlySet<ControlAction>;
  log?: (msg: string) => void;
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
  private sessionClosed = false;

  constructor(args: {
    attemptId: AttemptId;
    channel: AttemptInputChannel;
    session: ClaudeSessionHandle | null;
    log: (msg: string) => void;
  }) {
    this.attemptId = args.attemptId;
    this.channel = args.channel;
    this.session = args.session;
    this.log = args.log;
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

  /**
   * Pause is unavailable in this story — never a fabricated success.
   *
   * Claude's mechanism (pre-tool admission plus output gating) is S2's work and
   * requires real tool-boundary evidence before any run may report "Paused".
   */
  async requestPause(): Promise<PauseResult> {
    return { outcome: 'unavailable', reason: boundReason(S3_UNSUPPORTED_REASON) };
  }

  /**
   * Tracked in-flight work is not observable yet, so `null` — "cannot see",
   * which the coordinator must not render as the quiescence claim `0`.
   */
  activeWorkCount(): number | null {
    return null;
  }

  /**
   * Tear down: close input, then close the SDK session.
   *
   * Input first so nothing can be enqueued into a session being destroyed.
   * `session.close()` is preserved on every path including idle timeout (FR-5.6),
   * and guarded by a flag because a double close on a native handle is not
   * reliably harmless.
   */
  async dispose(): Promise<void> {
    this.channel.close();
    if (this.session && !this.sessionClosed) {
      this.sessionClosed = true;
      try {
        this.session.close();
      } catch (err) {
        this.log(`claude adapter: session.close() threw (ignored): ${(err as Error)?.message ?? err}`);
      }
    }
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
  /** The channel for the attempt currently being built, before its handle exists. */
  private pendingChannel: AttemptInputChannel | null = null;
  /**
   * The in-flight attach started by the most recent `onAttemptHandle` call.
   *
   * `resilientQuery` invokes that callback synchronously and swallows throws, so
   * without this the attach outcome would be unobservable — a cancellation that
   * refused the attach would look indistinguishable from a successful one. Tests
   * and the coordinator await this to learn what actually happened.
   */
  private pendingAttach: Promise<void> = Promise.resolve();

  constructor(options: ClaudeControlAdapterOptions = {}) {
    this.implementedVerbs = options.implementedVerbs ?? IMPLEMENTED_CONTROL_VERBS;
    this.log = options.log ?? (() => {});
  }

  /** Adapter-declared support, before ADP/runtime intersection. */
  adapterCapabilities(): Record<ControlAction, VerbSupport> {
    return noVerbsSupported(S3_UNSUPPORTED_REASON);
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

  async requestPause(): Promise<PauseResult> {
    return { outcome: 'unavailable', reason: boundReason(S3_UNSUPPORTED_REASON) };
  }

  /** No pause can exist in this story, so releasing one is a no-op, not an error. */
  async resumeFromPause(): Promise<void> {
    return;
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
   * Runs before every query — attempt 1, true resume and fallback — and mints a
   * fresh channel each time. On a resumed attempt the SDK reloads history, so
   * the prompt text is a continuation nudge and is NOT re-sent as input; on a
   * fallback/initial attempt the prompt already carries the task via
   * `queryParams.prompt`, so re-sending it here would duplicate it. Either way
   * the channel starts empty and pending operator input is reconnected by the
   * caller — S6 owns proving that reconnection after a forced retry.
   */
  attemptInputFactory(): (context: { attemptNumber: number; isResume: boolean; promptText: string }) => {
    input: AsyncIterable<unknown>;
    dispose: () => void;
  } {
    return (context) => {
      const channel = new AttemptInputChannel();
      this.pendingChannel = channel;
      this.log(
        `claude adapter: fresh input channel for attempt ${context.attemptNumber}` +
          `${context.isResume ? ' (true resume)' : ' (initial/fallback)'}`,
      );
      return {
        input: channel.iterable(),
        dispose: () => {
          channel.close();
          if (this.pendingChannel === channel) this.pendingChannel = null;
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
  onAttemptHandle(): (handle: { attemptNumber: number; session: unknown }) => void {
    return (handle) => {
      const channel = this.pendingChannel;
      if (!channel) {
        // No factory ran for this attempt: nothing can receive input, so
        // publishing an endpoint would advertise a channel that does not exist.
        this.log('claude adapter: attempt handle with no input channel — not attaching');
        return;
      }
      this.pendingChannel = null;
      const endpoint = new ClaudeAttemptEndpoint({
        attemptId: newAttemptId(),
        channel,
        session: (handle.session as ClaudeSessionHandle | null) ?? null,
        log: this.log,
      });
      // resilientQuery calls this synchronously and swallows throws, so the
      // async attach cannot propagate from here. It is recorded on
      // `pendingAttach` instead of being truly fire-and-forget, so the outcome
      // stays observable via `whenAttached()` — a cancellation racing attach is
      // expected, not an error to surface, but it must not be invisible.
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
    };
  }

  /**
   * Resolves once the attach started by the latest attempt handle has settled.
   *
   * Exists because `onAttemptHandle` must stay synchronous to match
   * `resilientQuery`'s callback contract, which would otherwise make "is the new
   * attempt reachable yet?" an unanswerable question for anything downstream.
   * Never rejects: a refused attach is a legitimate outcome, so callers read
   * `currentAttempt()` to learn the result rather than catching.
   */
  async whenAttached(): Promise<void> {
    await this.pendingAttach;
  }

  /** The `cancellation` object to hand to `resilientQuery`. */
  cancellationSource(): { isCancelled: () => boolean; error: () => Error } {
    return {
      isCancelled: () => this.registry.isCancelled(),
      error: () => this.registry.cancellationError(),
    };
  }
}
