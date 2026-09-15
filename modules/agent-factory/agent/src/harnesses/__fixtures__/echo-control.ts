/**
 * Non-Claude test adapter — Issue #3962 (S3) contract fixture.
 *
 * This adapter exists to make one claim falsifiable: that the neutral contract
 * is genuinely provider-independent rather than Claude's shape with the labels
 * filed off. A second adapter that mirrored Claude's internals would prove
 * nothing — the contract tests would pass for both while still encoding Claude's
 * assumptions, and the next real harness would break them.
 *
 * So this one is built to be **structurally different in every dimension the
 * contract touches**:
 *
 * | Dimension | Claude adapter | This adapter |
 * |---|---|---|
 * | Input transport | open async iterable (`SDKUserMessage`) | synchronous callback + recorded request events |
 * | Handle | native `Query` with `close()` | plain integer generation counter |
 * | Attempt ids | minted from the shared registry | prefixed opaque strings of its own |
 * | Pause | unavailable (no proof) | genuinely supported and confirmable |
 * | Capabilities | all four false | `pause`/`resume` true, `steer`/`abort` false |
 *
 * It imports NO Claude SDK, defines no `Query`, no `SDKUserMessage` and no
 * `AsyncIterable` input. If the neutral interface ever leaks a provider type,
 * this file stops compiling — which is a far better failure than a runtime
 * surprise on the day a second harness lands.
 *
 * The pause support is deliberate and load-bearing in the opposite direction:
 * because this adapter supports pause while ADP has not implemented it, it
 * proves the capability *intersection* actually gates. An adapter saying "yes"
 * must not be enough to turn a verb on.
 */
import type { ControlAction } from '../../control-state';
import {
  CONTROL_PROTOCOL_VERSION,
  CurrentAttemptRegistry,
  IMPLEMENTED_CONTROL_VERBS,
  boundReason,
  intersectCapabilities,
  type AttemptEndpoint,
  type AttemptId,
  type ControlInput,
  type ControlRuntimeAdapter,
  type ControlRuntimeListener,
  type HarnessDescriptor,
  type InputHandoffResult,
  type PauseResult,
  type VerbSupport,
} from '../../control-runtime';

/** A recorded delivery — this harness's "transport" is an append-only log. */
export interface EchoRequestEvent {
  attempt: string;
  kind: 'steering' | 'annotation';
  text: string;
  command_id?: string;
}

/**
 * One attempt of the echo harness.
 *
 * Input arrives by synchronous callback rather than by a stream, which is the
 * shape a request/response harness would have.
 */
class EchoAttemptEndpoint implements AttemptEndpoint {
  readonly attemptId: AttemptId;
  private readonly sink: EchoRequestEvent[];
  private readonly generation: number;
  private live = true;
  private paused = false;
  /** Work this attempt claims to be running; drives pause confirmation. */
  private tracked: number;
  /** Whether disposal already ran — asserted once by the contract suite. */
  readonly disposals: { count: number };

  constructor(args: {
    attemptId: AttemptId;
    sink: EchoRequestEvent[];
    generation: number;
    tracked: number;
    disposals: { count: number };
  }) {
    this.attemptId = args.attemptId;
    this.sink = args.sink;
    this.generation = args.generation;
    this.tracked = args.tracked;
    this.disposals = args.disposals;
  }

  async deliver(input: ControlInput): Promise<InputHandoffResult> {
    if (!this.live) return 'rejected';
    this.sink.push({
      attempt: `echo-gen-${this.generation}`,
      kind: input.kind,
      text: input.text,
      command_id: input.command_id,
    });
    return 'delivered';
  }

  /**
   * A real, cancellation-aware pause.
   *
   * Confirms only once tracked work reaches zero, and honours the abort signal
   * so a cancel resolves the wait rather than leaving it hanging. Returns
   * `requested` — not `confirmed` — while work is outstanding, which is the
   * distinction the UI depends on.
   */
  async requestPause(signal: AbortSignal): Promise<PauseResult> {
    if (!this.live) return { outcome: 'unavailable', reason: 'attempt is not live' };
    if (signal.aborted) return { outcome: 'unavailable', reason: 'cancelled before barrier' };
    this.paused = true;
    if (this.tracked > 0) return { outcome: 'requested' };
    return { outcome: 'confirmed' };
  }

  async releasePause(): Promise<void> {
    this.paused = false;
  }

  /** Observable here, unlike the Claude adapter — so `null` is not the only case tested. */
  activeWorkCount(): number | null {
    return this.tracked;
  }

  /** Test hook: settle tracked work so a pause can confirm. */
  settleWork(): void {
    this.tracked = 0;
  }

  isPaused(): boolean {
    return this.paused;
  }

  async dispose(): Promise<void> {
    this.live = false;
    this.disposals.count += 1;
  }
}

export interface EchoControlAdapterOptions {
  implementedVerbs?: ReadonlySet<ControlAction>;
  /** Tracked work each new attempt starts with. Non-zero keeps pause unconfirmed. */
  trackedWorkPerAttempt?: number;
}

/**
 * A deterministic non-Claude adapter.
 *
 * No model, no network, no SDK — every observable is a counter or an array, so
 * the contract suite's assertions are about the contract rather than about a
 * mock's fidelity.
 */
export class EchoControlAdapter implements ControlRuntimeAdapter {
  static readonly ADAPTER_ID = 'echo-test-harness';

  private readonly registry = new CurrentAttemptRegistry();
  private readonly implementedVerbs: ReadonlySet<ControlAction>;
  private readonly trackedWorkPerAttempt: number;
  /** Every delivery ever recorded, across attempts — the substitutability evidence. */
  readonly requests: EchoRequestEvent[] = [];
  /** Shared disposal ledger, so "disposed exactly once" is checkable. */
  readonly disposals = { count: 0 };
  private generation = 0;
  private attemptSeq = 0;

  constructor(options: EchoControlAdapterOptions = {}) {
    this.implementedVerbs = options.implementedVerbs ?? IMPLEMENTED_CONTROL_VERBS;
    this.trackedWorkPerAttempt = options.trackedWorkPerAttempt ?? 0;
  }

  /** Supports pause/resume, not steer/abort — an intentionally mixed table. */
  adapterCapabilities(): Record<ControlAction, VerbSupport> {
    const missing = boundReason('the echo test harness implements no steering or abort transport');
    return {
      pause: { supported: true },
      resume: { supported: true },
      steer: { supported: false, reason: missing },
      abort: { supported: false, reason: missing },
    };
  }

  describe(): HarnessDescriptor {
    return {
      protocolVersion: CONTROL_PROTOCOL_VERSION,
      adapterId: EchoControlAdapter.ADAPTER_ID,
      adapterVersion: '0.0.1-test',
      capabilities: this.adapterCapabilities(),
    };
  }

  capabilities(): Record<ControlAction, boolean> {
    return intersectCapabilities({
      implemented: this.implementedVerbs,
      adapter: this.adapterCapabilities(),
      available: this.registry.currentAttemptId() === null ? new Set<ControlAction>() : undefined,
    });
  }

  currentAttempt(): AttemptId | null {
    return this.registry.currentAttemptId();
  }

  subscribe(listener: ControlRuntimeListener): () => void {
    return this.registry.subscribe(listener);
  }

  async submitInput(input: ControlInput): Promise<InputHandoffResult> {
    return this.registry.deliver(input);
  }

  /**
   * Start a new attempt — this harness's equivalent of `query()`.
   *
   * Opaque ids in its own namespace, not the shared minter's format, so a
   * consumer that assumed a particular id shape would fail here.
   */
  async startAttempt(): Promise<AttemptId> {
    this.generation += 1;
    this.attemptSeq += 1;
    const attemptId = `echo/${this.generation}/${this.attemptSeq}` as AttemptId;
    const endpoint = new EchoAttemptEndpoint({
      attemptId,
      sink: this.requests,
      generation: this.generation,
      tracked: this.trackedWorkPerAttempt,
      disposals: this.disposals,
    });
    await this.registry.attach(endpoint);
    return attemptId;
  }

  async requestPause(options?: { signal?: AbortSignal }): Promise<PauseResult> {
    const attemptId = this.registry.currentAttemptId();
    if (!attemptId) return { outcome: 'unavailable', reason: 'no live attempt' };
    // Consult the INTERSECTION, not this adapter's own capability table. The
    // endpoint below can genuinely pause, and calling it directly was a real bug:
    // an adapter that supports a verb ADP has not implemented would confirm a
    // pause the platform never enabled, which is precisely the failure the
    // intersection exists to prevent. The adapter does not get the deciding vote.
    if (!this.capabilities().pause) {
      return {
        outcome: 'unavailable',
        reason: boundReason('pause is not enabled for this run despite adapter support'),
      };
    }
    const endpoint = this.currentEndpoint();
    if (!endpoint?.requestPause) return { outcome: 'unavailable', reason: 'attempt cannot pause' };
    const signal = options?.signal ?? new AbortController().signal;
    const result = await endpoint.requestPause(signal);
    if (result.outcome === 'requested') this.registry.emit({ type: 'pause_requested', attemptId });
    if (result.outcome === 'confirmed') this.registry.emit({ type: 'pause_confirmed', attemptId });
    if (result.outcome === 'unavailable') {
      this.registry.emit({ type: 'pause_unavailable', attemptId, reason: result.reason });
    }
    return result;
  }

  async resumeFromPause(): Promise<void> {
    const attemptId = this.registry.currentAttemptId();
    const endpoint = this.currentEndpoint();
    await endpoint?.releasePause?.();
    if (attemptId) this.registry.emit({ type: 'pause_released', attemptId });
  }

  /** Test hook: settle the current attempt's tracked work. */
  settleCurrentWork(): void {
    (this.currentEndpoint() as EchoAttemptEndpoint | null)?.settleWork();
  }

  activeWorkCount(): number | null {
    return this.registry.activeWorkCount();
  }

  cancel(reason?: string): void {
    this.registry.cancel(reason ?? 'echo adapter cancelled');
  }

  isCancelled(): boolean {
    return this.registry.isCancelled();
  }

  async dispose(): Promise<void> {
    await this.registry.dispose();
  }

  /** Reach the live endpoint without exposing it on the neutral interface. */
  private currentEndpoint(): AttemptEndpoint | null {
    const internal = this.registry as unknown as { current: AttemptEndpoint | null };
    return internal.current ?? null;
  }
}
