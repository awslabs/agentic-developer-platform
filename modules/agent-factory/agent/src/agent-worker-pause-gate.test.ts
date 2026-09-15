/**
 * Pause-gate contract tests — Issue #3961 (S2).
 *
 * The suite is organised around the one claim that matters: **`paused` means no
 * tool can be running.** Each describe block below is a way that claim could be
 * false, so a regression shows up as a named failure rather than as a subtly
 * optimistic state.
 *
 * Time is injected throughout (`now` + a manual scheduler) because every
 * interesting case here is a race or a deadline. Real timers would make these
 * tests slow and flaky, and a flaky pause test is worse than no pause test: it
 * trains people to re-run until green, which is exactly how a real quiescence
 * bug would slip through.
 */
import {
  DEFAULT_FINALIZATION_MARGIN_MS,
  DEFAULT_PAUSE_TIMEOUT_MS,
  PauseGate,
  type AdmissionTicket,
  type PauseGateEvent,
  type PauseGateScheduler,
} from './pause-gate';

/** A scheduler whose timers only fire when a test says so. */
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

  /** Fire the timer scheduled for exactly `ms`, as expiry and settle differ. */
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

  fireAll(): void {
    for (const [id, timer] of [...this.timers]) {
      this.timers.delete(id);
      timer.fn();
    }
  }
}

interface Harness {
  gate: PauseGate;
  scheduler: ManualScheduler;
  events: PauseGateEvent[];
  setBackground: (value: number | null) => void;
  setNow: (value: number) => void;
}

function harness(
  options: {
    deadlineAt?: () => number | null;
    settleTimeoutMs?: number;
    defaultTimeoutMs?: number;
    finalizationMarginMs?: number;
  } = {},
): Harness {
  const scheduler = new ManualScheduler();
  const events: PauseGateEvent[] = [];
  let background: number | null = 0;
  let now = 1_000_000;
  const gate = new PauseGate({
    scheduler,
    now: () => now,
    onEvent: (event) => events.push(event),
    backgroundWorkProbe: () => background,
    ...options,
  });
  return {
    gate,
    scheduler,
    events,
    setBackground: (value) => {
      background = value;
    },
    setNow: (value) => {
      now = value;
    },
  };
}

/** Let queued microtasks (the serialized transition chain) run. */
const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

describe('pause gate: requested versus paused', () => {
  it('confirms immediately when no tool is in flight', async () => {
    const { gate, events } = harness();

    const result = await gate.requestPause();

    expect(result).toEqual({ outcome: 'confirmed' });
    expect(gate.currentPhase()).toBe('paused');
    expect(events.map((e) => e.type)).toEqual(['pause_requested', 'pause_confirmed']);
  });

  it('publishes pause_requested before it can possibly confirm', async () => {
    const { gate, events, scheduler } = harness();
    const admission = await gate.admit('Bash');

    const pending = gate.requestPause();
    await flush();

    // The operator-visible request is out while the tool is still running.
    expect(events.map((e) => e.type)).toContain('pause_requested');
    expect(events.map((e) => e.type)).not.toContain('pause_confirmed');
    expect(gate.currentPhase()).toBe('pause_requested');

    gate.settle(admission.ticket);
    await expect(pending).resolves.toEqual({ outcome: 'confirmed' });
    scheduler.fireAll();
  });

  it('reports requested — never paused — while an admitted tool never finishes', async () => {
    const { gate, scheduler } = harness({ settleTimeoutMs: 5_000 });
    await gate.admit('Bash');

    const pending = gate.requestPause();
    await flush();
    scheduler.fireByDuration(5_000); // the bounded settle wait elapses

    const result = await pending;
    expect(result.outcome).toBe('requested');
    expect(gate.currentPhase()).toBe('pause_requested');
    expect(gate.currentPhase()).not.toBe('paused');
  });

  it('is idempotent: a repeated pause on a confirmed pause stays confirmed', async () => {
    const { gate } = harness();
    await gate.requestPause();

    await expect(gate.requestPause()).resolves.toEqual({ outcome: 'confirmed' });
    expect(gate.currentPhase()).toBe('paused');
  });
});

describe('pause gate: no new tool work is admitted', () => {
  it('holds a tool at the barrier while paused and admits it on resume', async () => {
    const { gate } = harness();
    await gate.requestPause();

    let admitted = false;
    const pending = gate.admit('Write').then((result) => {
      admitted = result.decision === 'admit';
      return result;
    });
    await flush();

    // The decisive assertion: the tool has NOT been allowed to run.
    expect(admitted).toBe(false);
    expect(gate.heldCount()).toBe(1);
    expect(gate.activeToolCount()).toBe(0);

    await gate.resume();
    const result = await pending;
    expect(result.decision).toBe('admit');
    expect(gate.activeToolCount()).toBe(1);
  });

  it('does not count a parked admission as in-flight work', async () => {
    // Otherwise confirmation would wait on the very call the barrier is holding.
    const { gate } = harness();
    const pause = gate.requestPause();
    await flush();
    void gate.admit('Edit');
    await flush();

    await expect(pause).resolves.toEqual({ outcome: 'confirmed' });
    expect(gate.heldCount()).toBe(1);
    expect(gate.activeToolCount()).toBe(0);
  });

  it('admits normally once resumed, with no residual barrier', async () => {
    const { gate } = harness();
    await gate.requestPause();
    await gate.resume();

    const result = await gate.admit('Read');
    expect(result.decision).toBe('admit');
    expect(gate.heldCount()).toBe(0);
  });
});

describe('pause gate: in-flight completion', () => {
  it('waits for several admitted tools and confirms only after the last settles', async () => {
    const { gate } = harness();
    const first = await gate.admit('Bash');
    const second = await gate.admit('Write');

    const pending = gate.requestPause();
    await flush();
    gate.settle(first.ticket);
    await flush();
    expect(gate.currentPhase()).toBe('pause_requested');

    gate.settle(second.ticket);
    await expect(pending).resolves.toEqual({ outcome: 'confirmed' });
    expect(gate.activeToolCount()).toBe(0);
  });

  it('ignores a duplicate settle so the count cannot go negative', async () => {
    const { gate } = harness();
    const first = await gate.admit('Bash');
    await gate.admit('Write');

    gate.settle(first.ticket);
    gate.settle(first.ticket); // e.g. both a completion and a failure edge

    expect(gate.activeToolCount()).toBe(1);
  });

  it('ignores an unknown ticket', async () => {
    const { gate } = harness();
    await gate.admit('Bash');

    gate.settle({ id: 999, toolName: 'Ghost' } as AdmissionTicket);

    expect(gate.activeToolCount()).toBe(1);
  });
});

describe('pause gate: background work blocks confirmation', () => {
  it('stays requested when a completed tool left background work behind', async () => {
    const { gate, setBackground } = harness();
    setBackground(2);

    const result = await gate.requestPause();

    expect(result.outcome).toBe('requested');
    expect(result).toHaveProperty('reason', expect.stringContaining('background'));
    expect(gate.currentPhase()).not.toBe('paused');
  });

  it('stays requested when background work is unobservable (null, not zero)', async () => {
    const { gate, setBackground } = harness();
    setBackground(null);

    const result = await gate.requestPause();

    expect(result.outcome).toBe('requested');
    expect(gate.currentPhase()).not.toBe('paused');
  });

  it('confirms once background work drains', async () => {
    const { gate, setBackground } = harness();
    setBackground(1);
    await gate.requestPause();
    setBackground(0);

    await expect(gate.requestPause()).resolves.toEqual({ outcome: 'confirmed' });
  });
});

describe('pause gate: hook timeout / abandoned barrier', () => {
  it('drops a confirmed pause the moment the harness overrides the barrier', async () => {
    const { gate, events } = harness();
    const controller = new AbortController();
    await gate.requestPause();
    expect(gate.currentPhase()).toBe('paused');
    const held = gate.admit('Bash', controller.signal);
    await flush();

    // The harness stopped waiting for our answer and may now run the tool. The
    // run must stop claiming "Paused" immediately, not at the next request.
    controller.abort();
    const admission = await held;

    expect(admission.decision).toBe('deny');
    expect(gate.barrierBreached()).toBe(true);
    expect(gate.currentPhase()).not.toBe('paused');
    expect(events.map((e) => e.type)).toContain('pause_unavailable');
  });

  it('refuses later pauses for the rest of the run once the barrier has been overridden', async () => {
    const { gate } = harness();
    const controller = new AbortController();
    await gate.requestPause();
    void gate.admit('Bash', controller.signal);
    await flush();
    controller.abort();
    await flush();

    // A barrier that leaked once cannot substantiate a quiescence claim again.
    const after = await gate.requestPause();
    expect(after.outcome).toBe('unavailable');
    expect(gate.currentPhase()).not.toBe('paused');
  });

  it('denies immediately when the signal is already aborted', async () => {
    const { gate } = harness();
    await gate.requestPause();
    const controller = new AbortController();
    controller.abort();

    const admission = await gate.admit('Bash', controller.signal);

    expect(admission.decision).toBe('deny');
    expect(gate.barrierBreached()).toBe(true);
  });

  it('collapses an already-pending pause to unavailable when the barrier breaks', async () => {
    const { gate, events } = harness({ settleTimeoutMs: 1_000 });
    const controller = new AbortController();
    await gate.admit('Write'); // never settles: the breach must not wait for it

    const pending = gate.requestPause();
    await flush();
    void gate.admit('Bash', controller.signal);
    await flush();
    controller.abort();

    const result = await pending;
    expect(result.outcome).toBe('unavailable');
    expect(events.map((e) => e.type)).toContain('pause_unavailable');
    expect(events.map((e) => e.type)).not.toContain('pause_confirmed');
  });

  it('denies rather than admits the other held tools when the barrier breaks', async () => {
    const { gate } = harness();
    const controller = new AbortController();
    await gate.requestPause();
    const bystander = gate.admit('Write'); // held, with no signal of its own
    const abandoned = gate.admit('Bash', controller.signal);
    await flush();

    controller.abort();

    // A breach is not a resume: nothing the operator paused gets waved through.
    await expect(abandoned).resolves.toMatchObject({ decision: 'deny' });
    await expect(bystander).resolves.toMatchObject({ decision: 'deny' });
    expect(gate.activeToolCount()).toBe(0);
  });
});

describe('pause gate: resume and repeat races', () => {
  it('treats a resume with no pause as a no-op rather than an error', async () => {
    const { gate } = harness();

    await expect(gate.resume()).resolves.toBe(false);
    expect(gate.currentPhase()).toBe('running');
  });

  it('releases exactly once across repeated resumes', async () => {
    const { gate, events } = harness();
    await gate.requestPause();

    const [first, second] = await Promise.all([gate.resume(), gate.resume()]);

    expect([first, second].filter(Boolean)).toHaveLength(1);
    expect(events.filter((e) => e.type === 'pause_released')).toHaveLength(1);
  });

  it('cancels a pending pause when resume wins the race, and never reports paused', async () => {
    const { gate, events } = harness({ settleTimeoutMs: 10_000 });
    const admission = await gate.admit('Bash');

    const pause = gate.requestPause();
    await flush();
    const resumed = await gate.resume();
    gate.settle(admission.ticket);
    const result = await pause;

    expect(resumed).toBe(true);
    expect(result.outcome).not.toBe('confirmed');
    expect(events.map((e) => e.type)).not.toContain('pause_confirmed');
    expect(gate.currentPhase()).toBe('running');
  });

  it('serializes interleaved pause/resume calls into a consistent final phase', async () => {
    const { gate } = harness();

    await Promise.all([gate.requestPause(), gate.resume(), gate.requestPause(), gate.resume()]);

    expect(gate.currentPhase()).toBe('running');
    expect(gate.heldCount()).toBe(0);
  });

  it('a second pause after a resume gets its own epoch and can confirm', async () => {
    const { gate } = harness();
    await gate.requestPause();
    await gate.resume();

    await expect(gate.requestPause()).resolves.toEqual({ outcome: 'confirmed' });
  });
});

describe('pause gate: cancellation for abort', () => {
  it('denies held work instead of admitting it, and emits no release note', async () => {
    const { gate, events } = harness();
    await gate.requestPause();
    const held = gate.admit('Bash');
    await flush();

    gate.cancel('run aborted');
    const admission = await held;

    // The core abort guarantee: the held tool must NOT run.
    expect(admission.decision).toBe('deny');
    expect(gate.currentPhase()).toBe('cancelled');
    expect(events.map((e) => e.type)).not.toContain('pause_released');
  });

  it('refuses new admissions after cancellation', async () => {
    const { gate } = harness();
    gate.cancel();

    await expect(gate.admit('Write')).resolves.toMatchObject({ decision: 'deny' });
  });

  it('refuses a pause requested after cancellation', async () => {
    const { gate } = harness();
    gate.cancel();

    const result = await gate.requestPause();
    expect(result.outcome).toBe('unavailable');
  });

  it('is idempotent', async () => {
    const { gate, events } = harness();
    gate.cancel('first');
    gate.cancel('second');

    expect(gate.currentPhase()).toBe('cancelled');
    expect(events.filter((e) => e.type === 'pause_released')).toHaveLength(0);
  });

  it('unblocks a pause waiting on quiescence rather than hanging', async () => {
    const { gate } = harness({ settleTimeoutMs: 60_000 });
    await gate.admit('Bash');
    const pending = gate.requestPause();
    await flush();

    gate.cancel();

    const result = await pending;
    expect(result.outcome).toBe('unavailable');
  });
});

describe('pause gate: deadline clamp and safe budget', () => {
  it('uses the default timeout when no deadline is set', () => {
    const { gate } = harness();
    expect(gate.safeBudget()).toBe(DEFAULT_PAUSE_TIMEOUT_MS);
  });

  it('clamps to the remaining deadline minus the finalization margin', () => {
    const now = 1_000_000;
    // 10 minutes of pod life left: less than the 30-minute default.
    const { gate } = harness({ deadlineAt: () => now + 10 * 60 * 1000 });

    expect(gate.safeBudget()).toBe(10 * 60 * 1000 - DEFAULT_FINALIZATION_MARGIN_MS);
  });

  it('prefers a shorter explicit request over the clamp', () => {
    const now = 1_000_000;
    const { gate } = harness({ deadlineAt: () => now + 10 * 60 * 1000 });

    expect(gate.safeBudget(30_000)).toBe(30_000);
  });

  it('rejects a pause when the margin leaves no positive budget', async () => {
    const now = 1_000_000;
    const { gate } = harness({ deadlineAt: () => now + DEFAULT_FINALIZATION_MARGIN_MS });

    expect(gate.safeBudget()).toBeNull();
    const result = await gate.requestPause();
    expect(result.outcome).toBe('unavailable');
    expect(result).toHaveProperty('reason', expect.stringContaining('deadline'));
    expect(gate.currentPhase()).toBe('running');
  });

  it('rejects a pause when the deadline has already passed', async () => {
    const now = 1_000_000;
    const { gate } = harness({ deadlineAt: () => now - 1 });

    await expect(gate.requestPause()).resolves.toMatchObject({ outcome: 'unavailable' });
  });

  it('ignores a nonpositive requested timeout in favour of the default', () => {
    const { gate } = harness();
    expect(gate.safeBudget(0)).toBe(DEFAULT_PAUSE_TIMEOUT_MS);
    expect(gate.safeBudget(-5)).toBe(DEFAULT_PAUSE_TIMEOUT_MS);
  });
});

describe('pause gate: expiry auto-resume', () => {
  it('releases the pause and admits held work when the budget expires', async () => {
    const { gate, scheduler, events } = harness({ defaultTimeoutMs: 60_000 });
    await gate.requestPause();
    const held = gate.admit('Bash');
    await flush();

    scheduler.fireByDuration(60_000);
    await flush();

    const release = events.find((e) => e.type === 'pause_released');
    expect(release).toEqual({ type: 'pause_released', expired: true });
    await expect(held).resolves.toMatchObject({ decision: 'admit' });
    expect(gate.currentPhase()).toBe('running');
  });

  it('arms the expiry timer with the clamped budget, not the raw default', async () => {
    const now = 1_000_000;
    const { gate, scheduler } = harness({
      deadlineAt: () => now + 5 * 60 * 1000,
      defaultTimeoutMs: DEFAULT_PAUSE_TIMEOUT_MS,
    });

    await gate.requestPause();

    const expected = 5 * 60 * 1000 - DEFAULT_FINALIZATION_MARGIN_MS;
    expect([...scheduler.timers.values()].some((t) => t.ms === expected)).toBe(true);
  });

  it('clears the expiry timer when a pause is released deliberately', async () => {
    const { gate, scheduler } = harness({ defaultTimeoutMs: 60_000 });
    await gate.requestPause();
    expect(scheduler.timers.size).toBe(1);

    await gate.resume();

    expect(scheduler.timers.size).toBe(0);
  });

  it('a stale expiry callback cannot tear down the pause that followed it', async () => {
    const { gate, scheduler, events } = harness({ defaultTimeoutMs: 60_000 });
    await gate.requestPause();
    // Retain the first pause's callback before its release discards the timer,
    // so the epoch guard is exercised rather than the clear-on-release path.
    const stale = [...scheduler.timers.values()][0].fn;
    await gate.resume();
    await gate.requestPause();

    stale();
    await flush();

    // Exactly one release: the deliberate resume. The second pause survives.
    expect(events.filter((e) => e.type === 'pause_released')).toHaveLength(1);
    expect(gate.currentPhase()).toBe('paused');
  });

  it('does not fire expiry after cancellation', async () => {
    const { gate, scheduler, events } = harness({ defaultTimeoutMs: 60_000 });
    await gate.requestPause();

    gate.cancel();
    scheduler.fireAll();
    await flush();

    expect(events.filter((e) => e.type === 'pause_released')).toHaveLength(0);
  });
});

describe('pause gate: watchdog visibility', () => {
  it('reports an active pause so idle-retry and exit watchdogs can stand down', async () => {
    const { gate } = harness();
    expect(gate.isPauseActive()).toBe(false);

    const admission = await gate.admit('Bash');
    const pending = gate.requestPause();
    await flush();
    // True from the moment of request, not only once confirmed: a run waiting
    // for a tool to settle is deliberately quiet, not stalled.
    expect(gate.isPauseActive()).toBe(true);

    gate.settle(admission.ticket);
    await pending;
    expect(gate.isPauseActive()).toBe(true);

    await gate.resume();
    expect(gate.isPauseActive()).toBe(false);
  });

  it('reports no active pause once cancelled', async () => {
    const { gate } = harness();
    await gate.requestPause();
    gate.cancel();

    expect(gate.isPauseActive()).toBe(false);
  });

  it('emits observed active_work counts on admission and settle', async () => {
    const { gate, events } = harness();
    const admission = await gate.admit('Bash');
    gate.settle(admission.ticket);

    const counts = events.filter((e) => e.type === 'active_work').map((e) => (e as { count: number }).count);
    expect(counts).toEqual([1, 0]);
  });
});

describe('pause gate: defaults', () => {
  it('defaults to a 30 minute pause with a real clock and timers', () => {
    // Constructed with no injected primitives, as the worker will build it.
    const gate = new PauseGate();
    expect(gate.safeBudget()).toBe(DEFAULT_PAUSE_TIMEOUT_MS);
    expect(gate.currentPhase()).toBe('running');
    expect(gate.activeToolCount()).toBe(0);
  });

  it('treats background work as clear by default so an unset probe cannot block forever', async () => {
    const gate = new PauseGate({ scheduler: new ManualScheduler() });
    await expect(gate.requestPause()).resolves.toEqual({ outcome: 'confirmed' });
  });

  it('runs its default timers without throwing', async () => {
    // Exercises the real setTimeout/clearTimeout branch of the default scheduler.
    const gate = new PauseGate({ defaultTimeoutMs: 5_000 });
    await gate.requestPause();
    await gate.resume();
    expect(gate.currentPhase()).toBe('running');
  });
});
