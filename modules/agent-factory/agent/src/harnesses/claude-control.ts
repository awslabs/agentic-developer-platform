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
  /** The channel for the attempt currently being built, before its handle exists. */
  private pendingChannel: AttemptInputChannel | null = null;
  private activeChannel: AttemptInputChannel | null = null;
  /** Attachment completion for the wrapper and direct adapter callers. */
  private pendingAttach: Promise<void> = Promise.resolve();

  constructor(options: ClaudeControlAdapterOptions = {}) {
    this.implementedVerbs = options.implementedVerbs ?? IMPLEMENTED_CONTROL_VERBS;
    this.log = options.log ?? (() => {});
    this.registry.cancellationSignal.addEventListener('abort', () => {
      this.activeChannel?.close();
    }, { once: true });
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
