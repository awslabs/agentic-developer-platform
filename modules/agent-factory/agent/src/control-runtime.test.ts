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
import { EchoControlAdapter } from './harnesses/__fixtures__/echo-control';
import { ClaudeControlAdapter } from './harnesses/claude-control';
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
  make: () => ControlRuntimeAdapter & { isCancelled(): boolean };
  startAttempt: (adapter: ControlRuntimeAdapter) => Promise<AttemptId>;
}

const CLAUDE_CASE: AdapterCase = {
  name: 'claude adapter',
  make: () => new ClaudeControlAdapter(),
  startAttempt: async (adapter) => {
    const claude = adapter as ClaudeControlAdapter;
    // Drive the two resilientQuery hooks in the order the wrapper drives them:
    // build the attempt's input, then publish the handle.
    claude.attemptInputFactory()({ attemptNumber: 1, isResume: false, promptText: 'task' });
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
};

const ECHO_CASE: AdapterCase = {
  name: 'echo (non-claude) adapter',
  make: () => new EchoControlAdapter(),
  startAttempt: async (adapter) => (adapter as EchoControlAdapter).startAttempt(),
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

  it('reports every verb unsupported at this stage, whatever the adapter claims', () => {
    const adapter = testCase.make();

    // The echo adapter declares pause/resume TRUE. This must still be false,
    // because ADP has implemented no verb — the intersection gates, and an
    // adapter's own optimism cannot put a button on the dashboard.
    expect(adapter.capabilities()).toEqual({
      pause: false,
      resume: false,
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

  it('never reports a successful pause without a proven barrier', async () => {
    const adapter = testCase.make();
    await testCase.startAttempt(adapter);

    const result = await adapter.requestPause();

    // Either unavailable-with-reason, or requested; never a bare "confirmed"
    // from an adapter whose verb ADP has not enabled.
    expect(['unavailable', 'requested']).toContain(result.outcome);
    if (result.outcome === 'unavailable') expect(result.reason).toBeTruthy();
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

  it('defaults to the empty ADP verb set, so S3 enables nothing', () => {
    expect(IMPLEMENTED_CONTROL_VERBS.size).toBe(0);
    expect(intersectCapabilities({ adapter: bothSupported })).toEqual({
      pause: false,
      resume: false,
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
 * These tests are about a seam, not a feature. Before this story the listener
 * held its own empty `SUPPORTED_ACTIONS` and the runtime held its own empty
 * `IMPLEMENTED_CONTROL_VERBS`, and they agreed only because both were empty —
 * the kind of agreement that ends the first time someone edits one of them.
 * Everything below is a statement about which of the two failure directions is
 * now unreachable.
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

  it('advertises nothing in S3, for both real adapters', () => {
    expect([...listenerActionsFor(new ClaudeControlAdapter())]).toEqual([]);
    expect([...listenerActionsFor(new EchoControlAdapter())]).toEqual([]);
  });

  it('refuses to advertise a verb the adapter supports but ADP has not implemented', () => {
    // The echo adapter genuinely supports pause and resume. This is the
    // dangerous direction: advertising them would make the listener answer 200
    // for a command whose ADP-side handling does not exist.
    expect([...listenerActionsFor(new EchoControlAdapter())]).toEqual([]);
    expect(new EchoControlAdapter().describe().capabilities.pause.supported).toBe(true);
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
    expect([...listenerActionsFor(adapterClaiming(allSupported))]).toEqual([]);
  });

  it('is the same set the listener module exports, not a parallel one', () => {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { SUPPORTED_ACTIONS } = require('./control-listener');

    // Identity, not equality: two empty sets are equal today and would stay
    // equal right up until one of them was widened alone.
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
    expect(source).not.toMatch(/from '@anthropic-ai/);
    expect(source).not.toMatch(/require\('@anthropic-ai/);
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
