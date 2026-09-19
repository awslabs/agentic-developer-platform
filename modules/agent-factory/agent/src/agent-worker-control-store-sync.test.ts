/**
 * Store/gate agreement — Issue #3961 (S2).
 *
 * The gateway does not ask the pause gate anything. It reads the control state
 * store, so what an operator sees is whatever the store says — and the store only
 * learns about a transition if something tells it. Every test here is one way the
 * two could disagree, because a disagreement in this direction is the specific
 * failure the story exists to prevent: the dashboard reporting `paused` while tools
 * are running again.
 *
 * A real `ControlStateStore` is used rather than a mock. The claim under test is
 * "these two agree", and a hand-written double would let me assert agreement with
 * something whose behaviour I chose.
 */
import { applyControlCommand, bindGateTransitionsToStore } from './control-command-apply';
import { ControlStateStore } from './control-state';
import { PauseGate, type PauseGateScheduler } from './pause-gate';

class ManualScheduler implements PauseGateScheduler {
  private seq = 0;
  readonly timers = new Map<number, { fn: () => void; ms: number }>();

  setTimer(fn: () => void, ms: number): unknown {
    this.seq += 1;
    this.timers.set(this.seq, { fn, ms });
    return this.seq;
  }

  clearTimer(handle: unknown): void {
    this.timers.delete(handle as number);
  }

  fireByDuration(ms: number): boolean {
    for (const [id, timer] of this.timers) {
      if (timer.ms === ms) {
        this.timers.delete(id);
        timer.fn();
        return true;
      }
    }
    return false;
  }
}

const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

/**
 * A gate plus a store wired exactly as the worker wires them.
 *
 * `supportedActions` includes pause/resume so the journal accepts the commands
 * these tests submit, matching the implemented capability intersection.
 */
function harness(options: { settleTimeoutMs?: number; defaultTimeoutMs?: number } = {}) {
  const scheduler = new ManualScheduler();
  let now = 1_000_000;
  const gate = new PauseGate({
    scheduler,
    now: () => now,
    settleTimeoutMs: options.settleTimeoutMs ?? 1_000,
    defaultTimeoutMs: options.defaultTimeoutMs ?? 10_000,
  });
  const store = new ControlStateStore({
    generation: 1,
    supportedActions: new Set(['pause', 'resume'] as const),
    now: () => now,
  });
  const unsubscribe = bindGateTransitionsToStore({ gate, store });

  let seq = 0;
  /** Submit a command the way the listener would, then apply it. */
  const command = async (action: 'pause' | 'resume') => {
    seq += 1;
    const commandId = `cmd-${seq}`;
    const outcome = store.submit(action, commandId, `fp-${seq}`);
    if (outcome.kind !== 'accepted') throw new Error(`submit refused: ${outcome.kind}`);
    if (!store.markDelivered(commandId)) throw new Error('command not delivered');
    await applyControlCommand({
      action,
      commandId,
      adapter: {
        requestPause: (opts?: { timeoutMs?: number }) => gate.requestPause({ timeoutMs: opts?.timeoutMs }),
        resumeFromPause: async () => {
          await gate.resume();
        },
      } as never,
      store,
      log: () => {},
    });
    return commandId;
  };

  const statusOf = (commandId: string) =>
    store.snapshot().commands.find((entry) => entry.command_id === commandId)?.status;

  return { gate, store, scheduler, command, statusOf, unsubscribe, setNow: (v: number) => { now = v; } };
}

describe('store/gate agreement: transitions driven by a command', () => {
  it('records a confirmed pause as paused and applied', async () => {
    const h = harness();
    const id = await h.command('pause');
    await flush();

    expect(h.store.snapshot().state).toBe('paused');
    expect(h.gate.currentPhase()).toBe('paused');
    expect(h.statusOf(id)).toBe('applied');
  });

  it('leaves a delivered pause awaiting quiescence, and shows pause_requested', async () => {
    const h = harness();
    await h.gate.admit('Bash');
    const pausePromise = h.command('pause');
    await flush();
    h.scheduler.fireByDuration(1_000);
    const id = await pausePromise;

    expect(h.store.snapshot().state).toBe('pause_requested');
    expect(h.statusOf(id)).toBe('delivered');
  });

  it('publishes pause_requested while the command still awaits a running tool', async () => {
    const h = harness();
    const admission = await h.gate.admit('Write');
    const command = h.command('pause');
    await flush();
    try {
      expect(h.store.snapshot().state).toBe('pause_requested');
      expect(h.store.snapshot().commands.filter((command) => command.status === 'delivered')).toHaveLength(1);
    } finally {
      h.gate.settle(admission.ticket);
      await command;
      await h.gate.resume();
    }
  });

  it('keeps a later pause intact when the cancelled pause finishes late', async () => {
    const h = harness();
    const work = await h.gate.admit('Write');
    const first = h.command('pause');
    await flush();
    await h.command('resume');
    const second = h.command('pause');
    await flush();
    h.gate.settle(work.ticket);
    const [firstId, secondId] = await Promise.all([first, second]);
    expect(h.statusOf(firstId)).toBe('cancelled');
    expect(h.statusOf(secondId)).toBe('applied');
    expect(h.gate.currentPhase()).toBe('paused');
    expect(h.store.snapshot().state).toBe('paused');
    await h.command('resume');
  });

  it('settles a pause cancelled by an operator resume', async () => {
    const h = harness();
    await h.gate.admit('Bash');
    const pausePromise = h.command('pause');
    await flush();
    h.scheduler.fireByDuration(1_000);
    const pauseId = await pausePromise;

    await h.command('resume');
    await flush();

    expect(h.store.snapshot().state).toBe('running');
    expect(h.gate.currentPhase()).toBe('running');
    expect(h.statusOf(pauseId)).toBe('cancelled');
  });
});

describe('store/gate agreement: transitions the gate makes on its own', () => {
  // Regression for the review's B5. `setPhase` was only ever called from the
  // command path, but the gate changes admission state by itself in three cases.
  // Each one previously left the store asserting a pause that had already ended.

  it('returns the store to running when the harness overrides the barrier', async () => {
    const h = harness();
    const pauseId = await h.command('pause');
    await flush();
    expect(h.store.snapshot().state).toBe('paused');

    // A tool arrives and its harness abandons the parked call: the barrier leaked,
    // so the run is no longer contained.
    const controller = new AbortController();
    const admitting = h.gate.admit('Write', controller.signal);
    controller.abort();
    await admitting;
    await flush();

    expect(h.gate.currentPhase()).toBe('running');
    expect(h.store.snapshot().state).toBe('running');
    // The command stays `applied`: at the time it was answered the pause *had*
    // taken effect, and the journal is an append-only record of what was true then,
    // not a live status field. Rewriting it to `rejected` would erase the fact that
    // the run was genuinely contained for that interval. The phase above is what
    // tells an operator the containment has since ended.
    expect(h.statusOf(pauseId)).toBe('applied');
  });

  it('settles a still-pending pause as rejected when the barrier is overridden', async () => {
    // The other half of the case above: a pause that had *not* yet confirmed has no
    // truthful outcome to preserve, so the breach is its answer.
    const h = harness();
    await h.gate.admit('Bash');
    const pausePromise = h.command('pause');
    await flush();
    h.scheduler.fireByDuration(1_000);
    const pauseId = await pausePromise;
    expect(h.statusOf(pauseId)).toBe('delivered');

    const controller = new AbortController();
    const admitting = h.gate.admit('Write', controller.signal);
    controller.abort();
    await admitting;
    await flush();

    expect(h.store.snapshot().state).toBe('running');
    expect(h.statusOf(pauseId)).toBe('rejected');
  });

  it('returns the store to running when a pause budget expires', async () => {
    const h = harness({ defaultTimeoutMs: 10_000 });
    await h.command('pause');
    await flush();
    expect(h.store.snapshot().state).toBe('paused');

    h.scheduler.fireByDuration(10_000);
    await flush();

    expect(h.gate.currentPhase()).toBe('running');
    expect(h.store.snapshot().state).toBe('running');
  });

  it('confirms in the store when a late tool settles a pending pause', async () => {
    // The B4 re-drive also happens with no command behind it, so the store has to
    // learn about it the same way.
    const h = harness();
    const admitted = await h.gate.admit('Bash');
    const pausePromise = h.command('pause');
    await flush();
    h.scheduler.fireByDuration(1_000);
    const pauseId = await pausePromise;
    expect(h.store.snapshot().state).toBe('pause_requested');

    h.gate.settle(admitted.ticket);
    await flush();

    expect(h.gate.currentPhase()).toBe('paused');
    expect(h.store.snapshot().state).toBe('paused');
    expect(h.statusOf(pauseId)).toBe('applied');
  });

  it('never leaves the store claiming paused after any gate-initiated transition', async () => {
    // The invariant behind the three cases above, asserted directly: whatever the
    // gate's admission state is, the store must not be the more optimistic of the
    // two. Stated as a property so a *new* gate-initiated transition that forgets
    // to notify the store fails here rather than shipping.
    const scenarios: ((h: ReturnType<typeof harness>) => Promise<void>)[] = [
      async (h) => {
        const controller = new AbortController();
        const admitting = h.gate.admit('Write', controller.signal);
        controller.abort();
        await admitting;
      },
      async (h) => {
        h.scheduler.fireByDuration(10_000);
      },
      async (h) => {
        await h.gate.resume();
      },
    ];

    for (const scenario of scenarios) {
      const h = harness({ defaultTimeoutMs: 10_000 });
      await h.command('pause');
      await flush();
      await scenario(h);
      await flush();

      const gatePaused = h.gate.currentPhase() === 'paused';
      const storePaused = h.store.snapshot().state === 'paused';
      expect({ gatePaused, storePaused }).toEqual({ gatePaused, storePaused: gatePaused });
    }
  });
});

describe('store/gate agreement: outcomes that are not a confirmation', () => {
  it('rejects the command and shows running when the adapter reports unavailable', async () => {
    // The gateway reads the phase, so an `unavailable` that left it at
    // `pause_requested` would show a run as pausing forever with no pause behind it.
    const h = harness();
    const id = 'cmd-unavailable';
    expect(h.store.submit('pause', id, 'fp-u').kind).toBe('accepted');
    expect(h.store.markDelivered(id)).toBe(true);

    await applyControlCommand({
      action: 'pause',
      commandId: id,
      adapter: {
        requestPause: async () => ({ outcome: 'unavailable', reason: 'no barrier is installed' }),
        resumeFromPause: async () => {},
      } as never,
      store: h.store,
      log: () => {},
    });

    expect(h.store.snapshot().state).toBe('running');
    expect(h.statusOf(id)).toBe('rejected');
    // The reason travels with the rejection: "pause failed" without a cause leaves
    // an operator unable to tell a retry from a dead end.
    expect(
      h.store.snapshot().commands.find((c) => c.command_id === id)?.reason,
    ).toContain('no barrier');
  });

  it('rejects a verb no executor implements instead of accepting a silent no-op', async () => {
    // Unreachable through the listener, which answers an unsupported verb with 501.
    // Asserted anyway because the failure mode of widening the supported set without
    // teaching this function the new verb is an *accepted* command that does
    // nothing — an operator told their abort succeeded when nothing aborted.
    const h = harness();
    const id = 'cmd-steer';
    // Submitted directly: the store's own supported set would refuse it, which is
    // exactly the guard being bypassed to reach the executor's fallback.
    h.store.submit('pause', id, 'fp-s');
    expect(h.store.markDelivered(id)).toBe(true);

    await applyControlCommand({
      action: 'steer' as never,
      commandId: id,
      adapter: {
        requestPause: async () => {
          throw new Error('requestPause must not be reached for an unsupported verb');
        },
        resumeFromPause: async () => {},
      } as never,
      store: h.store,
      log: () => {},
    });

    expect(h.statusOf(id)).toBe('rejected');
    expect(
      h.store.snapshot().commands.find((c) => c.command_id === id)?.reason,
    ).toContain('steer');
  });

  it('applies each transition with no logger supplied', async () => {
    // `log` is optional and the worker always passes one, so the default is only
    // exercised here. A missing default would crash the executor rather than the
    // caller — a control command that throws mid-transition leaves the store and
    // the gate disagreeing, which is the one outcome this module exists to avoid.
    const h = harness();
    const id = 'cmd-nolog';
    expect(h.store.submit('pause', id, 'fp-n').kind).toBe('accepted');
    expect(h.store.markDelivered(id)).toBe(true);

    await applyControlCommand({
      action: 'pause',
      commandId: id,
      adapter: {
        requestPause: () => h.gate.requestPause(),
        resumeFromPause: async () => {
          await h.gate.resume();
        },
      } as never,
      store: h.store,
    });

    expect(h.store.snapshot().state).toBe('paused');
    expect(h.statusOf(id)).toBe('applied');
  });
});

describe('store/gate agreement: the admitted-tool count', () => {
  it('reports the barrier count so a gateway read is runtime truth', async () => {
    const h = harness();
    expect(h.store.snapshot().active_tool_count).toBe(0);

    const first = await h.gate.admit('Bash');
    expect(h.store.snapshot().active_tool_count).toBe(1);
    await h.gate.admit('Read');
    expect(h.store.snapshot().active_tool_count).toBe(2);

    h.gate.settle(first.ticket);
    expect(h.store.snapshot().active_tool_count).toBe(1);
  });

  it('stops reporting once unsubscribed, rather than freezing a stale count', async () => {
    const h = harness();
    await h.gate.admit('Bash');
    expect(h.store.snapshot().active_tool_count).toBe(1);
    h.unsubscribe();
    await h.gate.admit('Read');
    // Still 1: the point is that `unsubscribe` detaches cleanly for a run whose
    // control channel is torn down, not that the count keeps tracking.
    expect(h.store.snapshot().active_tool_count).toBe(1);
  });
});
