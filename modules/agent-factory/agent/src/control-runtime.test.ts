/**
 * Contract tests for the harness-neutral control runtime — Issue #3962 (S3).
 *
 * The important structural choice here is `describe.each` over BOTH adapters.
 * A suite that only exercised Claude would pass just as happily if the
 * "neutral" contract had quietly hard-coded Claude's assumptions, and the cost
 * would be paid later by whoever adds the second harness. Running one suite
 * against a Claude adapter and a deliberately differently-shaped non-Claude
 * adapter is what turns substitutability from an assertion into evidence.
 *
 * The echo adapter differs on purpose in every dimension the contract touches:
 * callback input instead of a stream, its own opaque attempt-id namespace, an
 * observable work count, and real pause support. That last one is the sharpest
 * test — it supports pause while ADP does not, so it proves the capability
 * intersection actually gates rather than deferring to the adapter.
 */
import { ECHO_PAUSE_EXPIRY_NOTE, EchoControlAdapter } from './harnesses/__fixtures__/echo-control';
import { ClaudeControlAdapter, PAUSE_EXPIRY_ANNOTATION } from './harnesses/claude-control';
import { PauseGate, type PauseGateScheduler } from './pause-gate';
import type { ControlAction } from './control-state';
import {
  CONTROL_PROTOCOL_VERSION,
  ControlCancelledError,
  CurrentAttemptRegistry,
  IMPLEMENTED_CONTROL_VERBS,
  MAX_REASON_LENGTH,
  boundReason,
  intersectCapabilities,
  isControlCancellation,
  listenerActionsFor,
  newAttemptId,
  noVerbsSupported,
  type AttemptEndpoint,
  type AttemptId,
  type ControlRuntimeAdapter,
  type ControlRuntimeEvent,
  type InputHandoffResult,
} from './control-runtime';

const ALL_VERBS: ControlAction[] = ['pause', 'resume', 'steer', 'abort'];

/**
 * The verb set S2's barrier implements, injected into both adapters under test.
 *
 * Deliberately a local constant and not `IMPLEMENTED_CONTROL_VERBS`, which stays
 * empty until pause is proven end to end (see its doc comment and
 * `docs/design-notes/3961-control-authorization-intersection.md`). The barrier is
 * built and its behaviour is what this file asserts; the global is a
 * delivery-stage claim. Keeping them separate is what lets the mechanism ship
 * fully tested while the capability stays honestly disabled.
 */
const PAUSE_AND_RESUME: ReadonlySet<ControlAction> = new Set<ControlAction>(['pause', 'resume']);

/**
 * Each Claude adapter under test, mapped to the gate whose barrier it translates.
 *
 * The gate is deliberately NOT reachable from the neutral interface — a consumer
 * that could fetch it would be reaching around the contract. The shared suite
 * needs it only to *simulate the harness*: admitting a tool at the barrier is
 * something the SDK does, and here there is no SDK. Keeping the handle in a
 * side table rather than on the adapter means the contract surface stays the
 * thing the tests are written against.
 */
const CLAUDE_GATES = new WeakMap<ClaudeControlAdapter, PauseGate>();

/** Messages the Claude case's simulated SDK reader consumed, per adapter. */
const CLAUDE_INBOX = new WeakMap<ClaudeControlAdapter, Array<{ text: string; startsWork: boolean }>>();

/**
 * Read a source file with comments removed.
 *
 * The provider-free assertions below are claims about *code* — what this module
 * imports and what types it requires. Scanning raw text conflated that with
 * prose: the doc comments explaining "no `SDKUserMessage` here" tripped the very
 * assertions checking for `SDKUserMessage`, so a correct file failed and the only
 * ways to pass were to delete the explanation or weaken the check. Stripping
 * comments keeps the assertion strict about the thing that actually matters.
 */
function readCode(dir: string, relativePath: string): string {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const source = require('node:fs').readFileSync(
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    require('node:path').join(dir, relativePath),
    'utf8',
  ) as string;
  return source
    .replace(/\/\*[\s\S]*?\*\//g, ' ') // block comments, including all JSDoc
    .replace(/(^|[^:])\/\/.*$/gm, '$1'); // line comments, sparing `https://`
}

/** Minimal endpoint for registry-level tests, with observable call counts. */
function fakeEndpoint(
  overrides: Partial<AttemptEndpoint> & { attemptId?: AttemptId } = {},
): AttemptEndpoint & { disposeCount: number; delivered: string[] } {
  const state = { disposeCount: 0, delivered: [] as string[] };
  const endpoint: AttemptEndpoint = {
    attemptId: overrides.attemptId ?? newAttemptId(),
    deliver: overrides.deliver
      ?? (async (input) => {
        state.delivered.push(input.text);
        return 'delivered' as InputHandoffResult;
      }),
    dispose: overrides.dispose
      ?? (async () => {
        state.disposeCount += 1;
      }),
    ...(overrides.activeWorkCount ? { activeWorkCount: overrides.activeWorkCount } : {}),
    ...(overrides.requestPause ? { requestPause: overrides.requestPause } : {}),
  };
  // defineProperty, NOT Object.assign: assign *invokes* getters and copies the
  // resulting values, which would freeze disposeCount at its value right now (0)
  // and make every "disposed exactly once" assertion vacuously fail.
  Object.defineProperties(endpoint, {
    disposeCount: { get: () => state.disposeCount, enumerable: false },
    delivered: { get: () => state.delivered, enumerable: false },
  });
  return endpoint as AttemptEndpoint & { disposeCount: number; delivered: string[] };
}

/**
 * One adapter under test, plus the harness-specific way to start an attempt.
 *
 * `startAttempt` is the only thing the two adapters cannot share — Claude's
 * attempt begins when `resilientQuery` hands it a query handle, the echo
 * harness's when it is asked. Everything else below is contract, not shape.
 */
interface AdapterCase {
  name: string;
  make: (options?: AdapterCaseOptions) => ControlRuntimeAdapter & { isCancelled(): boolean };
  startAttempt: (adapter: ControlRuntimeAdapter) => Promise<AttemptId>;
  /**
   * Create outstanding work on the live attempt, however this harness expresses
   * it, and return a way to finish it.
   *
   * Neutral by necessity: Claude parks a real admission at its `PreToolUse`
   * barrier, the echo harness increments a counter. Without this seam the
   * "requested while work is outstanding, confirmed only after" property could
   * only be asserted against whichever adapter the test author had in mind — and
   * it is exactly the property whose harness-independence the story turns on.
   */
  holdWork: (adapter: ControlRuntimeAdapter) => Promise<() => void>;
  /**
   * Everything this harness has actually received, in its own transport's terms.
   *
   * `startsWork` is the neutral reading of each harness's own "may this begin an
   * assistant turn?" field — Claude's `shouldQuery`, the echo harness's `kind`.
   * Comparing the two is how the suite checks that an expiring pause *records* a
   * fact rather than *issuing* an instruction, without either adapter's spelling
   * of that distinction leaking into the contract.
   */
  delivered: (adapter: ControlRuntimeAdapter) => Array<{ text: string; startsWork: boolean }>;
  /** The note this harness leaves when a pause ends because its budget ran out. */
  expiryNote: string;
}

interface AdapterCaseOptions {
  /** Absolute run deadline in the injected clock, or `null` for unbounded. */
  deadlineAt?: () => number | null;
  now?: () => number;
  finalizationMarginMs?: number;
  /** Injected timer, so a pause expiry is fired by hand rather than waited out. */
  scheduler?: PauseGateScheduler;
}

/**
 * How long an adapter under test waits for admitted work before answering
 * `requested`.
 *
 * Milliseconds, not the production minute. The bound itself is what the contract
 * requires — an unbounded wait is a pause command that never answers — and its
 * *duration* is a tuning decision, so a suite that waited the real value would
 * spend a minute per assertion proving nothing extra. Short enough to be fast,
 * long enough that a settle arriving on the next tick still lands inside it.
 */
const TEST_SETTLE_TIMEOUT_MS = 50;

/** A timer the tests fire by hand, so pause expiry needs no real waiting. */
function manualScheduler(): PauseGateScheduler & { fireAll: () => void } {
  const timers: Array<{ fn: () => void; cancelled: boolean }> = [];
  return {
    setTimer: (fn: () => void) => {
      const entry = { fn, cancelled: false };
      timers.push(entry);
      return entry;
    },
    clearTimer: (handle: unknown) => {
      (handle as { cancelled: boolean }).cancelled = true;
    },
    fireAll: () => {
      for (const entry of [...timers]) {
        if (entry.cancelled) continue;
        entry.cancelled = true;
        entry.fn();
      }
    },
  };
}

/**
 * Real timers that cannot outlive the suite.
 *
 * The default rather than the manual scheduler, because an adapter uses one
 * scheduler for two jobs: the pause expiry a test wants to fire by hand, and the
 * bounded wait for admitted work to settle. A fully manual default deadlocked the
 * second — the settle wait's own timeout never fired, so a pause behind held work
 * waited forever for a timer nobody was going to trigger.
 *
 * `unref` is what makes real timers safe here: a confirmed pause arms an expiry
 * timer for its whole budget (thirty minutes by default), and a test that pauses
 * without resuming would otherwise hold the Jest process open long after its last
 * assertion. Unreffed, the timer exists and would fire, but it is not a reason for
 * the process to stay alive.
 */
function unreffedScheduler(): PauseGateScheduler {
  return {
    setTimer: (fn, ms) => {
      const handle = setTimeout(fn, ms);
      handle.unref?.();
      return handle;
    },
    clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  };
}

/** The scheduler an adapter under test gets when a test does not supply one. */
function defaultScheduler(options?: AdapterCaseOptions): PauseGateScheduler {
  return options?.scheduler ?? unreffedScheduler();
}

const CLAUDE_CASE: AdapterCase = {
  name: 'claude adapter',
  make: (options) => {
    // A Claude adapter with pause enabled is one carrying the neutral gate whose
    // barrier its hooks implement. Without the gate the adapter honestly reports
    // pause unsupported, which is a different (and separately tested) case.
    const gate = new PauseGate({
      now: options?.now,
      deadlineAt: options?.deadlineAt,
      finalizationMarginMs: options?.finalizationMarginMs,
      scheduler: defaultScheduler(options),
      settleTimeoutMs: TEST_SETTLE_TIMEOUT_MS,
    });
    // `implementedVerbs` is injected rather than inherited from
    // `IMPLEMENTED_CONTROL_VERBS`, which is empty: the delivery-stage flag is a
    // statement about what ADP has proven end to end, and everything below is a
    // statement about the barrier's *behaviour*. Reading the global here would
    // make every pause assertion in this file collapse to "unavailable" the
    // moment the flag moves, testing the flag instead of the mechanism.
    const adapter = new ClaudeControlAdapter({
      pauseGate: gate,
      implementedVerbs: PAUSE_AND_RESUME,
    });
    CLAUDE_GATES.set(adapter, gate);
    return adapter;
  },
  startAttempt: async (adapter) => {
    const claude = adapter as ClaudeControlAdapter;
    // Drive the two resilientQuery hooks in the order the wrapper drives them:
    // build the attempt's input, then publish the handle.
    const input = claude.attemptInputFactory()({ attemptNumber: 1, isResume: false, promptText: '' });
    // Stand in for the SDK's reader, recording what it consumed. A real reader has
    // to be running for `deliver` to hand anything off at all, so this doubles as
    // the transport and as the evidence of what reached it.
    const inbox = CLAUDE_INBOX.get(claude) ?? [];
    CLAUDE_INBOX.set(claude, inbox);
    void (async () => {
      for await (const message of input.input) {
        const sdkMessage = message as { message?: { content?: unknown }; shouldQuery?: boolean };
        inbox.push({
          text: String(sdkMessage.message?.content ?? ''),
          // Claude's spelling of "may this begin an assistant turn?".
          startsWork: sdkMessage.shouldQuery === true,
        });
      }
    })();
    claude.onAttemptHandle()({ attemptNumber: 1, session: { close: () => {} } });
    // attach() is async inside a sync callback. Counting microtasks here was
    // wrong AND brittle: attach internally awaits detach -> dispose, so the tick
    // count is an implementation detail the test must not encode. Await the
    // adapter's own attach completion instead.
    await claude.whenAttached();
    const attemptId = claude.currentAttempt();
    if (attemptId === null) {
      // The attach was refused. `onAttemptHandle` cannot itself reject — it is
      // invoked synchronously by resilientQuery, which swallows callback throws —
      // so the refusal surfaces as "no attempt became current". Translating it to
      // the typed error here mirrors what resilientQuery really does on its
      // cancellation path, keeping the shared contract assertion honest instead
      // of special-casing one adapter out of it.
      throw claude.cancellationSource().error();
    }
    return attemptId;
  },
  holdWork: async (adapter) => {
    // Stand in for the SDK: admit a tool at the barrier, exactly as the adapter's
    // `PreToolUse` hook does, and hand back the settle the `PostToolUse` hook
    // would perform. Real hook wiring is asserted in claude-control.test.ts; here
    // the point is only that outstanding work exists.
    const gate = CLAUDE_GATES.get(adapter as ClaudeControlAdapter);
    if (!gate) throw new Error('claude case built without a pause gate');
    const result = await gate.admit('Bash');
    return () => gate.settle(result.ticket);
  },
  delivered: (adapter) => CLAUDE_INBOX.get(adapter as ClaudeControlAdapter) ?? [],
  expiryNote: PAUSE_EXPIRY_ANNOTATION,
};

const ECHO_CASE: AdapterCase = {
  name: 'echo (non-claude) adapter',
  make: (options) =>
    new EchoControlAdapter({
      now: options?.now,
      deadlineAt: options?.deadlineAt,
      finalizationMarginMs: options?.finalizationMarginMs,
      scheduler: defaultScheduler(options),
      // Injected for the same reason as the Claude case above: this file tests
      // the barrier contract, not the delivery-stage flag.
      implementedVerbs: PAUSE_AND_RESUME,
    }),
  startAttempt: async (adapter) => (adapter as EchoControlAdapter).startAttempt(),
  holdWork: async (adapter) => {
    const echo = adapter as EchoControlAdapter;
    echo.holdCurrentWork();
    return () => echo.settleCurrentWork();
  },
  delivered: (adapter) =>
    (adapter as EchoControlAdapter).requests.map((request) => ({
      text: request.text,
      // This harness's spelling of the same distinction: it keeps the neutral
      // `kind` rather than translating it to a provider flag.
      startsWork: request.kind === 'steering',
    })),
  expiryNote: ECHO_PAUSE_EXPIRY_NOTE,
};

describe.each([CLAUDE_CASE, ECHO_CASE])('control runtime contract — $name', (testCase) => {
  it('describes itself with the current protocol version and a bounded reason per unsupported verb', () => {
    const descriptor = testCase.make().describe();

    expect(descriptor.protocolVersion).toBe(CONTROL_PROTOCOL_VERSION);
    expect(descriptor.adapterId).toBeTruthy();
    expect(descriptor.adapterVersion).toBeTruthy();
    for (const verb of ALL_VERBS) {
      const support = descriptor.capabilities[verb];
      expect(typeof support.supported).toBe('boolean');
      if (!support.supported) {
        // A false capability the operator cannot explain is a support ticket.
        expect(support.reason).toBeTruthy();
        expect((support.reason as string).length).toBeLessThanOrEqual(MAX_REASON_LENGTH);
      }
    }
  });

  it('carries no native session or credential data in its descriptor', () => {
    // The descriptor is re-projected toward the browser after the gateway's own
    // intersection, so a native handle here would leak a provider-private id.
    const descriptor = testCase.make().describe();
    const serialized = JSON.stringify(descriptor);

    expect(serialized).not.toMatch(/session_id/i);
    expect(serialized).not.toMatch(/token|secret|credential/i);
    // `resume` legitimately appears as a VERB NAME in the capability table, so a
    // bare /resume/i scan would fail on correct output. What must not leak is a
    // resume *handle* — the SDK's opaque session pointer. Assert on the shape:
    // every capability value is a support record, never a string id.
    expect(Object.keys(descriptor.capabilities).sort()).toEqual([...ALL_VERBS].sort());
    for (const verb of ALL_VERBS) {
      expect(typeof descriptor.capabilities[verb]).toBe('object');
    }
    expect(serialized).not.toMatch(/resumeToken|resume_id|resume_session|sessionHandle/i);
  });

  it('advertises exactly the verbs with a proven boundary, and no others', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);

    // `pause`/`resume` as of S2 (#3961), which supplied the admission barrier that
    // makes a "Paused" claim mean something. `steer`/`abort` stay false: both
    // adapters could carry the input, and carrying input is not a delivered
    // control. Their runtime proofs are S6's and S4's, and an adapter's own
    // optimism must not put a button on the dashboard before then.
    expect(adapter.capabilities()).toEqual({
      pause: true,
      resume: true,
      steer: false,
      abort: false,
    });
  });

  it('has no current attempt before one is started, and one after', async () => {
    const adapter = testCase.make();
    expect(adapter.currentAttempt()).toBeNull();

    const attemptId = await testCase.startAttempt(adapter);

    expect(attemptId).toBeTruthy();
    expect(adapter.currentAttempt()).toBe(attemptId);
  });

  it('replaces the current attempt on retry and leaves the old one unable to act', async () => {
    const adapter = testCase.make();
    const first = await testCase.startAttempt(adapter);
    const events: ControlRuntimeEvent[] = [];
    adapter.subscribe((event) => events.push(event));

    const second = await testCase.startAttempt(adapter);

    expect(second).not.toBe(first);
    expect(adapter.currentAttempt()).toBe(second);
    // The old endpoint was invalidated and detached before the new one attached.
    expect(events.map((e) => e.type)).toEqual(['attempt_detached', 'attempt_attached']);
    expect(events.find((e) => e.type === 'attempt_detached')?.attemptId).toBe(first);
    expect(events.find((e) => e.type === 'attempt_attached')?.attemptId).toBe(second);
  });

  it('delivers input to the attempt that is current now, not one captured earlier', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);
    const second = await testCase.startAttempt(adapter);

    const result = await adapter.submitInput({ kind: 'steering', text: 'change course', command_id: 'c1' });

    // This is the retry-safety property that matters most: a command submitted
    // after a retry reaches the live attempt rather than vanishing into the
    // replaced one.
    expect(result).toBe('delivered');
    expect(adapter.currentAttempt()).toBe(second);
  });

  it('emits a handoff event naming the attempt and command that received the input', async () => {
    const adapter = testCase.make();
    const attemptId = await testCase.startAttempt(adapter);
    const events: ControlRuntimeEvent[] = [];
    adapter.subscribe((event) => events.push(event));

    await adapter.submitInput({ kind: 'steering', text: 'go', command_id: 'cmd-7' });

    expect(events).toEqual([
      { type: 'input_handoff', attemptId, command_id: 'cmd-7', result: 'delivered' },
    ]);
  });

  it('accepts annotations as well as steering, keeping the distinction in the input', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);

    await expect(adapter.submitInput({ kind: 'annotation', text: 'FYI: ticket moved' })).resolves.toBe('delivered');
    await expect(adapter.submitInput({ kind: 'steering', text: 'stop and re-read' })).resolves.toBe('delivered');
  });

  it('rejects input when no attempt is live rather than buffering it', async () => {
    const adapter = testCase.make();

    const result = await adapter.submitInput({ kind: 'steering', text: 'nobody home' });

    // A hidden adapter buffer would bypass the revalidation the shared journal
    // performs immediately before handoff, so refusing is the safe answer.
    expect(result).toBe('rejected');
  });

  it('confirms a pause when nothing is running', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);

    const result = await adapter.requestPause();

    // Nothing outstanding, so `confirmed` is a fact rather than an assumption:
    // admission is closed and there is no admitted work left to finish.
    expect(result.outcome).toBe('confirmed');
  });

  it('reports requested, not paused, while admitted work is still outstanding', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);
    const settle = await testCase.holdWork(adapter);

    const requested = await adapter.requestPause();
    settle();
    const confirmed = await adapter.requestPause();

    // "Pause requested" and "Paused" are different operator-facing claims, and
    // only the second one says no new tool side effects can occur. Reporting the
    // first as the second is what invites an operator to edit files underneath a
    // run that is still writing to them.
    expect(requested.outcome).toBe('requested');
    expect(confirmed.outcome).toBe('confirmed');
  });

  it('emits the request and confirmation as distinct events', async () => {
    const adapter = testCase.make();
    const attemptId = await testCase.startAttempt(adapter);
    const settle = await testCase.holdWork(adapter);
    const events: ControlRuntimeEvent[] = [];
    adapter.subscribe((event) => events.push(event));

    await adapter.requestPause();
    settle();
    await adapter.requestPause();

    // Two events, not one: a consumer that only ever saw `pause_confirmed` would
    // have no way to show the intermediate state, and an operator staring at an
    // unchanged dashboard concludes their command was lost.
    expect(events.filter((e) => e.type === 'pause_requested')).toEqual([{ type: 'pause_requested', attemptId }]);
    expect(events.filter((e) => e.type === 'pause_confirmed')).toEqual([{ type: 'pause_confirmed', attemptId }]);
  });

  it('releases a pause exactly once however many resumes arrive', async () => {
    const adapter = testCase.make();
    const attemptId = await testCase.startAttempt(adapter);
    const events: ControlRuntimeEvent[] = [];
    adapter.subscribe((event) => events.push(event));

    await adapter.requestPause();
    await adapter.resumeFromPause();
    await adapter.resumeFromPause();

    // A double-clicked resume is ordinary and must neither fail nor double-release:
    // a second release would re-open a barrier a *later* pause had legitimately
    // closed, and the operator would never learn their pause had been undone.
    expect(events.filter((e) => e.type === 'pause_released')).toEqual([
      expect.objectContaining({ type: 'pause_released', attemptId }),
    ]);
  });

  it('treats a resume with no pause in force as a no-op rather than an error', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);

    await expect(adapter.resumeFromPause()).resolves.toBeUndefined();
  });

  it('admits new work again after a resume', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);

    await adapter.requestPause();
    await adapter.resumeFromPause();
    const settle = await testCase.holdWork(adapter);

    // The barrier must be an admission *gate*, not a one-way valve: work held or
    // arriving after a resume proceeds, which is what makes resume a continuation
    // rather than a run that quietly stops doing anything.
    const afterResume = await adapter.requestPause();
    expect(afterResume.outcome).toBe('requested');
    settle();
  });

  it.each([0, -1, NaN, Infinity, -Infinity])('rejects explicit unsafe pause budget %s', async (timeoutMs) => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);
    expect((await adapter.requestPause({ timeoutMs })).outcome).toBe('unavailable');
  });

  it('reports pause unavailable when no safe time remains before the deadline', async () => {
    const now = 1_000_000;
    // Deadline inside the finalization margin: any pause would consume the room
    // needed to write a terminal state, so there is no positive safe budget.
    const adapter = testCase.make({
      now: () => now,
      deadlineAt: () => now + 10_000,
      finalizationMarginMs: 60_000,
    });
    await testCase.startAttempt(adapter);

    const result = await adapter.requestPause();

    // Rejected with a reason rather than silently shortened to zero: a pause that
    // expires the instant it begins is indistinguishable to an operator from a
    // pause that never happened.
    expect(result.outcome).toBe('unavailable');
    expect((result as { reason: string }).reason).toBeTruthy();
  });

  it('resumes on expiry with an annotation, never a new instruction', async () => {
    const scheduler = manualScheduler();
    const adapter = testCase.make({ scheduler });
    await testCase.startAttempt(adapter);
    const events: ControlRuntimeEvent[] = [];
    adapter.subscribe((event) => events.push(event));

    expect((await adapter.requestPause({ timeoutMs: 60_000 })).outcome).toBe('confirmed');
    scheduler.fireAll();
    await new Promise((resolve) => setImmediate(resolve));

    // The run continues by itself — an expiry that left a run paused forever would
    // burn the pod deadline with nothing to show. What it must NOT do is steer:
    // the operator issued no instruction, so their silence is recorded as context
    // (`annotation`) rather than acted on.
    expect(events.some((e) => e.type === 'pause_released')).toBe(true);

    await new Promise((resolve) => setImmediate(resolve));
    const note = testCase.delivered(adapter).filter((message) => message.text === testCase.expiryNote);
    // Exactly one note, and it cannot start work. `startsWork: false` is where the
    // "records rather than instructs" claim is actually decided — each harness
    // spells it differently (`shouldQuery` vs `kind`), and the contract only cares
    // that whatever it spells means "do not begin a turn on account of this".
    expect(note).toHaveLength(1);
    expect(note[0].startsWork).toBe(false);

    const settled = await adapter.requestPause();
    expect(settled.outcome).toBe('confirmed');
  });

  it('denies work held at the barrier when the run is cancelled', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);
    await adapter.requestPause();

    adapter.cancel('operator aborted');

    // An abort that flushed its held tools on the way out would run exactly the
    // side effects the operator aborted to prevent. Cancelling must therefore
    // *deny* held admission, not release it.
    expect(adapter.isCancelled()).toBe(true);
    await expect(adapter.requestPause()).resolves.toMatchObject({ outcome: 'unavailable' });
  });

  it('starts no new attempt after cancellation and reports the typed error', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);

    adapter.cancel('operator aborted');

    expect(adapter.isCancelled()).toBe(true);
    // A cancelled runtime must not acquire another attempt: this is the
    // difference between "stop" and "restart".
    await expect(testCase.startAttempt(adapter)).rejects.toThrow();
    await testCase.startAttempt(adapter).catch((err) => {
      expect(isControlCancellation(err)).toBe(true);
    });
  });

  it('refuses input once cancelled', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);

    adapter.cancel();

    await expect(adapter.submitInput({ kind: 'steering', text: 'too late' })).resolves.toBe('rejected');
  });

  it('disposes idempotently on repeated teardown', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);

    await adapter.dispose();
    await adapter.dispose();

    expect(adapter.currentAttempt()).toBeNull();
  });

  it('reports no capability once torn down', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);

    await adapter.dispose();

    // Availability is part of the intersection: with no live attempt there is
    // nothing a verb could act on.
    expect(Object.values(adapter.capabilities())).not.toContain(true);
  });
});

describe('non-claude adapter proves substitutability', () => {
  it('imports no Claude SDK and exposes no provider types', () => {
    // Read as code: a type-level claim is only as good as the import list.
    const source = readCode(__dirname, 'harnesses/__fixtures__/echo-control.ts');

    expect(source).not.toMatch(/@anthropic-ai/);
    expect(source).not.toMatch(/SDKUserMessage/);
    expect(source).not.toMatch(/\bQuery\b/);
    // It must not mimic Claude's streaming input shape either.
    expect(source).not.toMatch(/AsyncIterable|AsyncGenerator/);
  });

  it('uses its own opaque attempt-id namespace, not the Claude adapter format', async () => {
    const adapter = new EchoControlAdapter();

    const attemptId = await adapter.startAttempt();

    expect(attemptId).toMatch(/^echo\//);
    expect(attemptId).not.toMatch(/^attempt-/);
  });

  it('records deliveries through a callback transport rather than a stream', async () => {
    const adapter = new EchoControlAdapter();
    await adapter.startAttempt();

    await adapter.submitInput({ kind: 'annotation', text: 'noted', command_id: 'a1' });

    expect(adapter.requests).toEqual([
      { attempt: 'echo-gen-1', kind: 'annotation', text: 'noted', command_id: 'a1' },
    ]);
  });

  it('exposes an observable work count where Claude cannot', async () => {
    const adapter = new EchoControlAdapter({ trackedWorkPerAttempt: 2 });
    await adapter.startAttempt();

    expect(adapter.activeWorkCount()).toBe(2);
  });

  it('withholds pause confirmation while work is outstanding, and confirms once settled', async () => {
    // `implementedVerbs` simulates a FUTURE stage where ADP has implemented pause.
    // Without it the intersection correctly refuses pause and this test would
    // pass for the wrong reason — never reaching the barrier logic it exists to
    // check. Two separate properties, two separate setups: the test above proves
    // the gate blocks, this one proves the barrier is honest once the gate opens.
    const adapter = new EchoControlAdapter({
      trackedWorkPerAttempt: 1,
      implementedVerbs: new Set<ControlAction>(['pause', 'resume']),
    });
    await adapter.startAttempt();

    const requested = await adapter.requestPause();
    adapter.settleCurrentWork();
    const confirmed = await adapter.requestPause();

    // "Pause requested" and "Paused" are different operator-facing claims, and
    // only the second one says no new tool side effects can occur.
    expect(requested.outcome).toBe('requested');
    expect(confirmed.outcome).toBe('confirmed');
    // A pause left standing owns a live expiry timer for its whole budget. Real
    // timers here rather than injected ones, on purpose: this case is the one that
    // proves the production default arms and *releases* a real timer, so disposing
    // is part of the assertion rather than test hygiene. Without it the suite
    // finishes and the process sits for thirty minutes.
    await adapter.dispose();
  });

  it('reports pause unavailable when cancelled before the barrier', async () => {
    // Pause implemented here too, so the refusal under test is the abort signal
    // rather than the capability gate — otherwise this asserts nothing about
    // cancellation.
    const adapter = new EchoControlAdapter({
      trackedWorkPerAttempt: 1,
      implementedVerbs: new Set<ControlAction>(['pause', 'resume']),
    });
    await adapter.startAttempt();
    const controller = new AbortController();
    controller.abort();

    const result = await adapter.requestPause({ signal: controller.signal });

    expect(result).toEqual({ outcome: 'unavailable', reason: 'cancelled before barrier' });
  });

  it('disposes each replaced attempt exactly once across several retries', async () => {
    const adapter = new EchoControlAdapter();

    await adapter.startAttempt();
    await adapter.startAttempt();
    await adapter.startAttempt();
    await adapter.dispose();

    // Three attempts, three disposals: two replaced plus one torn down. A
    // double-close on a real handle is not reliably harmless, so the count is
    // asserted rather than the mere fact of disposal.
    expect(adapter.disposals.count).toBe(3);
  });

  it('respects a missing capability instead of no-oping silently', async () => {
    // `steer` is false on this adapter. Even if ADP implemented it, the
    // intersection must keep it off — the adapter has no transport for it.
    const adapter = new EchoControlAdapter({ implementedVerbs: new Set<ControlAction>(['steer', 'pause']) });
    await adapter.startAttempt();

    expect(adapter.capabilities().steer).toBe(false);
    // pause is true on both sides here, which proves the intersection is a real
    // conjunction rather than a hard-coded all-false.
    expect(adapter.capabilities().pause).toBe(true);
  });
});

describe('capability intersection', () => {
  const bothSupported = {
    pause: { supported: true },
    resume: { supported: true },
    steer: { supported: true },
    abort: { supported: true },
  };

  it('requires all three of implemented, adapter-supported and available', () => {
    expect(
      intersectCapabilities({
        implemented: new Set<ControlAction>(['pause', 'resume']),
        adapter: bothSupported,
        available: new Set<ControlAction>(['pause']),
      }),
    ).toEqual({ pause: true, resume: false, steer: false, abort: false });
  });

  it('treats an unknown adapter with no support as supporting nothing', () => {
    expect(
      intersectCapabilities({
        implemented: new Set<ControlAction>(ALL_VERBS),
        adapter: noVerbsSupported('unknown adapter'),
      }),
    ).toEqual({ pause: false, resume: false, steer: false, abort: false });
  });

  it('defaults to the ADP-implemented set rather than to everything the adapter claims', () => {
    // Omitting the set permits what ADP implements, but never steer.
    expect([...IMPLEMENTED_CONTROL_VERBS]).toEqual(['pause', 'resume', 'abort']);
    expect(intersectCapabilities({ adapter: bothSupported })).toEqual({
      pause: true,
      resume: true,
      steer: false,
      abort: true,
    });
    // The veto is the *default*, not a hardcoded answer: the same adapter with an
    // explicit implemented set still yields its claimed verbs, so this test
    // cannot pass merely because intersection returns false for everything.
    expect(intersectCapabilities({ implemented: PAUSE_AND_RESUME, adapter: bothSupported })).toEqual({
      pause: true,
      resume: true,
      steer: false,
      abort: false,
    });
  });

  it('treats a missing adapter entry as unsupported rather than throwing', () => {
    expect(
      intersectCapabilities({
        implemented: new Set<ControlAction>(ALL_VERBS),
        adapter: { pause: { supported: true } } as never,
      }),
    ).toEqual({ pause: true, resume: false, steer: false, abort: false });
  });

  it('bounds an unavailability reason so it cannot carry unbounded provider text', () => {
    const bounded = boundReason('x'.repeat(MAX_REASON_LENGTH + 50));

    expect(bounded.length).toBe(MAX_REASON_LENGTH);
    expect(boundReason('  spaced   out  ')).toBe('spaced out');
  });
});

/**
 * The verb set the worker's listener advertises, derived rather than declared.
 *
 * These tests are about a seam, not a feature. The listener held its own empty
 * `SUPPORTED_ACTIONS` and the runtime its own empty `IMPLEMENTED_CONTROL_VERBS`,
 * and they agree only because both are empty — the kind of agreement that ends
 * the first time someone edits one of them. They are both *still* empty after
 * this story (see `IMPLEMENTED_CONTROL_VERBS`), so the seam is closed before it
 * first matters rather than after. Everything below is a statement about which
 * of the two failure directions is now unreachable; the sets are passed
 * explicitly so these tests keep proving that once a verb is enabled.
 */
describe('listener verb derivation', () => {
  const allSupported = {
    pause: { supported: true },
    resume: { supported: true },
    steer: { supported: true },
    abort: { supported: true },
  };
  const adapterClaiming = (capabilities: Record<ControlAction, { supported: boolean }>) => ({
    describe: () => ({
      protocolVersion: CONTROL_PROTOCOL_VERSION,
      adapterId: 'fake',
      adapterVersion: '0',
      capabilities,
    }),
  });

  it('advertises pause and resume, and nothing else, for both real adapters', () => {
    // `PAUSE_AND_RESUME` is passed explicitly because the ADP-wide set is empty
    // until pause is proven end to end. What this asserts is that *both* adapters
    // derive the same two verbs from their own capability tables — the
    // harness-neutrality property — not what stage of delivery ADP is at.
    const claude = new ClaudeControlAdapter({ pauseGate: new PauseGate(), implementedVerbs: PAUSE_AND_RESUME });

    expect([...listenerActionsFor(claude, PAUSE_AND_RESUME)].sort()).toEqual(['pause', 'resume']);
    expect(
      [...listenerActionsFor(new EchoControlAdapter({ implementedVerbs: PAUSE_AND_RESUME }), PAUSE_AND_RESUME)].sort(),
    ).toEqual(['pause', 'resume']);
  });

  it('advertises pause and resume for adapters with the proven boundary', () => {
    const claude = new ClaudeControlAdapter({ pauseGate: new PauseGate(), implementedVerbs: PAUSE_AND_RESUME });
    expect(claude.describe().capabilities.pause.supported).toBe(true);
    // `PAUSE_AND_RESUME` is passed explicitly here, so abort is excluded by the
    // ADP-set argument rather than by the adapter — the point of this case is the
    // adapter's own boundary, not the current contents of the implemented set.
    expect([...listenerActionsFor(claude, PAUSE_AND_RESUME)].sort()).toEqual(['pause', 'resume']);
    expect([...listenerActionsFor(new EchoControlAdapter(), PAUSE_AND_RESUME)].sort())
      .toEqual(['pause', 'resume']);
  });

  it('refuses to advertise a verb the adapter supports but ADP has not implemented', () => {
    // The dangerous direction: advertising a verb ADP cannot handle makes the
    // listener answer 200 for a command it will never perform, and a caller told
    // "yes" has no reason to look further. `steer` here is exactly that case —
    // claimed by the adapter, not implemented by ADP.
    const claiming = adapterClaiming(allSupported);

    expect([...listenerActionsFor(claiming, PAUSE_AND_RESUME)].sort()).toEqual(['pause', 'resume']);
    expect(claiming.describe().capabilities.steer.supported).toBe(true);
  });

  it('refuses to advertise pause for a Claude adapter with no barrier installed', () => {
    // ADP implements pause, but this run has no admission barrier hooked up, so
    // there is nothing to hold a tool at. A 501 is the honest answer; a 200 here
    // would accept a pause command whose only effect is a state label.
    //
    // Abort survives the same condition, and that asymmetry is the point (#3963):
    // it is cancellation, not a gated tool boundary, so a run with no barrier can
    // still be stopped. Advertising it here is the honest answer for the same
    // reason refusing pause is — each claim tracks the mechanism actually present.
    expect([...listenerActionsFor(new ClaudeControlAdapter())]).toEqual(['abort']);
  });

  it('refuses to advertise a verb ADP implemented but the adapter cannot perform', () => {
    // The other direction: a 501 is the honest answer when the transport is
    // missing, and it must not become a 200 just because ADP is ready.
    const actions = listenerActionsFor(
      adapterClaiming(noVerbsSupported('no transport')),
      new Set<ControlAction>(ALL_VERBS),
    );

    expect([...actions]).toEqual([]);
  });

  it('advertises exactly the two-way intersection when both sides agree', () => {
    const actions = listenerActionsFor(
      adapterClaiming({
        pause: { supported: true },
        resume: { supported: false },
        steer: { supported: true },
        abort: { supported: false },
      }),
      new Set<ControlAction>(['pause', 'resume']),
    );

    expect([...actions].sort()).toEqual(['pause']);
  });

  it('excludes availability, so a run between attempts does not report a verb missing', () => {
    // The distinction that makes this a two-way and not a three-way
    // intersection: with no attempt attached the adapter's *effective*
    // capabilities are all false, but the build-level answer must not change —
    // otherwise a verb reads as `not_implemented` during an ordinary retry gap,
    // sending an operator after a missing feature instead of a transient state.
    const adapter = new EchoControlAdapter({
      implementedVerbs: new Set<ControlAction>(['pause']),
    });

    expect(adapter.currentAttempt()).toBeNull();
    expect(adapter.capabilities().pause).toBe(false);
    expect([...listenerActionsFor(adapter, new Set<ControlAction>(['pause']))]).toEqual(['pause']);
  });

  it('treats a missing capability entry as unsupported rather than throwing', () => {
    const actions = listenerActionsFor(
      adapterClaiming({ pause: { supported: true } } as never),
      new Set<ControlAction>(ALL_VERBS),
    );

    expect([...actions]).toEqual(['pause']);
  });

  it('defaults to the ADP-implemented set, so a caller cannot widen it by omission', () => {
    // Omitting the second argument must not mean "trust the adapter". This adapter
    // claims all four; the default admits only what ADP has implemented, and
    // `steer` — claimed here — is still excluded.
    expect([...listenerActionsFor(adapterClaiming(allSupported))].sort())
      .toEqual(['abort', 'pause', 'resume']);
    // ...and the default is genuinely the ADP set rather than a hardcoded empty
    // answer: the same adapter with an explicit set still derives those verbs.
    expect([...listenerActionsFor(adapterClaiming(allSupported), PAUSE_AND_RESUME)].sort())
      .toEqual(['pause', 'resume']);
  });

  it('is the same set the listener module exports, not a parallel one', () => {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { SUPPORTED_ACTIONS } = require('./control-listener');

    // Identity, not equality. Two sets with the same contents are equal right up
    // until one of them is widened alone — which is precisely what S4/S6 will do
    // to one of these when they enable abort and steer.
    expect(SUPPORTED_ACTIONS).toBe(IMPLEMENTED_CONTROL_VERBS);
  });
});

describe('typed cancellation', () => {
  it('is recognized structurally, not only by instanceof', () => {
    expect(isControlCancellation(new ControlCancelledError('stop'))).toBe(true);
    // A cross-realm copy loses prototype identity but keeps the marker.
    expect(isControlCancellation({ isControlCancellation: true })).toBe(true);
  });

  it('is not confused with the retryable errors that share its vocabulary', () => {
    // These strings all match RETRYABLE_PATTERNS. If a cancellation were an
    // ordinary Error, it would be classified as transient and retried — which
    // would turn a deliberate abort into a new attempt.
    expect(isControlCancellation(new Error('aborted'))).toBe(false);
    expect(isControlCancellation(new Error('timeout'))).toBe(false);
    expect(isControlCancellation(undefined)).toBe(false);
    expect(isControlCancellation(null)).toBe(false);
  });
});

describe('current attempt registry', () => {
  it('discards events from a superseded attempt', async () => {
    const registry = new CurrentAttemptRegistry();
    const first = fakeEndpoint();
    await registry.attach(first);
    const events: ControlRuntimeEvent[] = [];
    registry.subscribe((event) => events.push(event));
    await registry.attach(fakeEndpoint());

    registry.emit({ type: 'active_work', attemptId: first.attemptId, count: 3 });
    registry.emit({ type: 'terminal', attemptId: first.attemptId, outcome: 'complete' });

    // A late event from a retried-away attempt is routine, so it is dropped
    // silently rather than raised — treating it as an error would make every
    // normal retry look like a failure.
    expect(events.filter((e) => e.type === 'active_work' || e.type === 'terminal')).toEqual([]);
  });

  it('reports unknown when a delivery resolves against a replaced attempt', async () => {
    const registry = new CurrentAttemptRegistry();
    let release: (() => void) | undefined;
    const slow = fakeEndpoint({
      deliver: () => new Promise<InputHandoffResult>((resolve) => {
        release = () => resolve('delivered');
      }),
    });
    await registry.attach(slow);

    const pending = registry.deliver({ kind: 'steering', text: 'racing' });
    await registry.attach(fakeEndpoint()); // retry lands mid-handoff
    release?.();

    // The transport said "delivered" but to an attempt no longer current. The
    // honest answer is unknown: claiming delivery would lie, and claiming
    // rejection would invite a replay of a possibly-consumed instruction.
    await expect(pending).resolves.toBe('unknown');
  });

  it('reports unknown when a handoff throws mid-delivery', async () => {
    const registry = new CurrentAttemptRegistry();
    await registry.attach(fakeEndpoint({
      deliver: async () => {
        throw new Error('socket closed mid-write');
      },
    }));

    await expect(registry.deliver({ kind: 'steering', text: 'ambiguous' })).resolves.toBe('unknown');
  });

  it('reports rejected — not unknown — when the transport refuses via cancellation', async () => {
    const registry = new CurrentAttemptRegistry();
    await registry.attach(fakeEndpoint({
      deliver: async () => {
        throw new ControlCancelledError('cancelled');
      },
    }));

    // A cancellation is an unambiguous refusal: nothing was consumed.
    await expect(registry.deliver({ kind: 'steering', text: 'x' })).resolves.toBe('rejected');
  });

  it('invalidates the old attempt before disposing it', async () => {
    const registry = new CurrentAttemptRegistry();
    const order: string[] = [];
    const first = fakeEndpoint({
      dispose: async () => {
        // At dispose time the endpoint must already be non-current, so a
        // concurrent delivery is refused rather than racing teardown.
        order.push(`current-during-dispose=${registry.currentAttemptId()}`);
      },
    });
    await registry.attach(first);

    await registry.detachCurrent();

    expect(order).toEqual(['current-during-dispose=null']);
  });

  it('disposes an endpoint exactly once even when attach and teardown both reach it', async () => {
    const registry = new CurrentAttemptRegistry();
    const endpoint = fakeEndpoint();
    await registry.attach(endpoint);

    await registry.detachCurrent();
    await registry.detachCurrent();
    await registry.dispose();

    expect(endpoint.disposeCount).toBe(1);
  });

  it('swallows a disposal error so teardown cannot mask the run outcome', async () => {
    const registry = new CurrentAttemptRegistry();
    await registry.attach(fakeEndpoint({
      dispose: async () => {
        throw new Error('close failed');
      },
    }));

    await expect(registry.dispose()).resolves.toBeUndefined();
  });

  it('disposes a new endpoint and throws when attaching after cancellation', async () => {
    const registry = new CurrentAttemptRegistry();
    registry.cancel('done');
    const endpoint = fakeEndpoint();

    await expect(registry.attach(endpoint)).rejects.toBeInstanceOf(ControlCancelledError);

    // The rejected endpoint is not leaked: it is disposed on the way out.
    expect(endpoint.disposeCount).toBe(1);
    expect(registry.currentAttemptId()).toBeNull();
  });

  it('records the first cancellation reason and ignores later ones', () => {
    const registry = new CurrentAttemptRegistry();

    registry.cancel('first reason');
    registry.cancel('second reason');

    expect(registry.cancellationError().message).toBe('first reason');
  });

  it('returns null work count when the endpoint cannot observe it', async () => {
    const registry = new CurrentAttemptRegistry();
    await registry.attach(fakeEndpoint());

    // Not 0: "cannot see" must never be rendered as a quiescence claim.
    expect(registry.activeWorkCount()).toBeNull();
  });

  it('surfaces an observable work count when the endpoint has one', async () => {
    const registry = new CurrentAttemptRegistry();
    await registry.attach(fakeEndpoint({ activeWorkCount: () => 4 }));

    expect(registry.activeWorkCount()).toBe(4);
  });

  it('keeps running when a subscriber throws', async () => {
    const registry = new CurrentAttemptRegistry();
    const seen: string[] = [];
    registry.subscribe(() => {
      throw new Error('bad observer');
    });
    registry.subscribe((event) => seen.push(event.type));

    await registry.attach(fakeEndpoint());

    expect(seen).toEqual(['attempt_attached']);
  });

  it('stops notifying an unsubscribed listener', async () => {
    const registry = new CurrentAttemptRegistry();
    const seen: string[] = [];
    const unsubscribe = registry.subscribe((event) => seen.push(event.type));

    unsubscribe();
    await registry.attach(fakeEndpoint());

    expect(seen).toEqual([]);
  });

  it('mints distinct opaque attempt ids', () => {
    expect(newAttemptId()).not.toBe(newAttemptId());
  });
});

describe('shared surface is provider-free', () => {
  const read = (relativePath: string): string => readCode(__dirname, relativePath);

  it('imports no provider SDK in the neutral contract', () => {
    const source = read('control-runtime.ts');

    // The single most load-bearing assertion in this file: the moment the shared
    // contract imports a provider SDK, every consumer inherits that dependency
    // and the second harness becomes a rewrite instead of an addition.
    //
    // Matched on the package name alone, deliberately. Earlier forms anchored on
    // `from '@anthropic-ai` and `require('@anthropic-ai`, which pinned the import
    // *syntax* rather than the dependency: a double-quoted `import type { Options }
    // from "@anthropic-ai/claude-agent-sdk"` re-exported through this file's public
    // surface passed the whole suite and `tsc`. The sibling assertion below already
    // used the broader form for the other shared modules, so this is the narrower
    // one being brought up to it.
    expect(source).not.toMatch(/@anthropic-ai/);
  });

  it('exposes no provider type and no iterable input requirement', () => {
    const source = read('control-runtime.ts');

    expect(source).not.toMatch(/SDKUserMessage|SDKStreamMessage/);
    // `AsyncIterable` must not appear in the neutral input contract at all —
    // requiring one would force every future harness to fake a stream.
    expect(source).not.toMatch(/AsyncIterable|AsyncGenerator/);
  });

  it('does not branch on an adapter name anywhere in the shared contract', () => {
    const source = read('control-runtime.ts');

    expect(source).not.toMatch(/adapterId\s*===/);
    expect(source).not.toMatch(/===\s*'claude'/);
  });

  it('keeps the shared state modules free of provider SDK imports', () => {
    for (const file of ['control-state.ts', 'control-listener.ts', 'control-envelope.ts']) {
      expect(read(file)).not.toMatch(/@anthropic-ai/);
    }
  });
});


describe('attempt setup races', () => {
  it.each(['cancel', 'dispose'] as const)('cannot attach after %s races predecessor disposal', async (stop) => {
    const registry = new CurrentAttemptRegistry();
    let release!: () => void;
    let closing!: () => void;
    const started = new Promise<void>(r => { closing = r; });
    const closed = new Promise<void>(r => { release = r; });
    const first = fakeEndpoint({ dispose: async () => { closing(); await closed; } });
    const second = fakeEndpoint();
    await registry.attach(first);
    const attach = registry.attach(second);
    const refused = expect(attach).rejects.toBeInstanceOf(ControlCancelledError);
    await started;
    const stopped = registry[stop]();
    release();
    await refused;
    await stopped;
    expect(registry.currentAttemptId()).toBeNull();
    expect(second.disposeCount).toBe(1);
    const later = fakeEndpoint();
    await expect(registry.attach(later)).rejects.toBeInstanceOf(ControlCancelledError);
    expect(later.disposeCount).toBe(1);
  });

  it('serializes competing attaches and disposes each endpoint exactly once', async () => {
    const registry = new CurrentAttemptRegistry();
    const endpoints = [fakeEndpoint(), fakeEndpoint(), fakeEndpoint()];
    await Promise.all(endpoints.map(e => registry.attach(e)));
    expect(registry.currentAttemptId()).toBe(endpoints[2].attemptId);
    await registry.dispose();
    expect(endpoints.map(e => e.disposeCount)).toEqual([1, 1, 1]);
  });

  it('ignores late events immediately on cancellation', async () => {
    const registry = new CurrentAttemptRegistry();
    const endpoint = fakeEndpoint();
    await registry.attach(endpoint);
    const observer = jest.fn();
    registry.subscribe(observer);
    registry.cancel();
    registry.emit({ type: 'input_handoff', attemptId: endpoint.attemptId, result: 'delivered' });
    expect(registry.isCurrent(endpoint.attemptId)).toBe(false);
    expect(observer).not.toHaveBeenCalled();
    await registry.dispose();
  });
});
