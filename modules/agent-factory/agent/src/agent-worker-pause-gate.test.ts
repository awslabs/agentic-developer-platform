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

  /**
   * The edge, not the second request.
   *
   * The test above drains background work and then calls `requestPause()` again,
   * which confirms — and that second call is precisely what hid the defect these
   * cover. In production nothing issues a second pause command: the operator pressed
   * Pause once. Confirmation has two independent blockers, in-flight tools and
   * background work, and only the first had an edge that re-drove the decision. So a
   * pause withheld on an unobservable probe sat in `pause_requested` for its whole
   * budget after the probe cleared, then auto-resumed — thirty minutes of "pausing…"
   * for a reason that stopped being true in the first second.
   */
  it('confirms a pending pause when the background probe clears, with no second command', async () => {
    const { gate, setBackground, events } = harness();
    setBackground(null);
    await expect(gate.requestPause()).resolves.toMatchObject({ outcome: 'requested' });

    // The observer sees a report clearing the probe and says so. One notification,
    // no new pause request — this is the only thing production does.
    setBackground(0);
    gate.noteBackgroundWorkChanged();
    await flush();

    expect(gate.currentPhase()).toBe('paused');
    expect(events.map((e) => e.type)).toContain('pause_confirmed');
  });

  it('keeps a pause pending when the probe changes but has not cleared', async () => {
    const { gate, setBackground, events } = harness();
    setBackground(null);
    await gate.requestPause();

    // A report arrived and named work still running. The edge fired, but the
    // blocker holds, so nothing may confirm.
    setBackground(3);
    gate.noteBackgroundWorkChanged();
    await flush();

    expect(gate.currentPhase()).toBe('pause_requested');
    expect(events.map((e) => e.type)).not.toContain('pause_confirmed');
  });

  it('will not confirm on a background edge while a tool is still admitted', async () => {
    const { gate, setBackground } = harness();
    await gate.admit('Bash');
    setBackground(null);
    // Not awaited: with work in flight this request only resolves once the settle
    // wait ends, and the manual scheduler never fires a timer by itself.
    const pending = gate.requestPause();
    await flush();
    expect(gate.currentPhase()).toBe('pause_requested');

    setBackground(0);
    gate.noteBackgroundWorkChanged();
    await flush();

    // Both blockers must be clear. Background work draining says nothing about the
    // tool still holding an admission.
    expect(gate.currentPhase()).toBe('pause_requested');
    expect(gate.activeToolCount()).toBe(1);
    void pending;
  });

  it('ignores a background edge outside a pending pause', async () => {
    const { gate, events } = harness();

    // No pause in flight: nothing to re-drive, and confirming here would be a
    // `paused` claim nobody asked for.
    gate.noteBackgroundWorkChanged();
    await flush();

    expect(gate.currentPhase()).toBe('running');
    expect(events).toEqual([]);
  });

  it('blames the blocker that actually held the pause when the budget expires', async () => {
    const { gate, scheduler, setBackground, events } = harness();
    setBackground(null);
    await gate.requestPause();

    scheduler.fireAll();
    await flush();

    // In-flight work is zero, so `settle_timeout` would name a blocker that was
    // never the problem. An operator reads this string to decide whether a retry is
    // worth anything, and a retry against an unobservable probe does the same thing
    // again.
    const unavailable = events.find((e) => e.type === 'pause_unavailable');
    expect(unavailable).toMatchObject({ failure: 'background_work' });
    expect((unavailable as { reason: string }).reason).toContain('background');
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

describe('pause gate: late observers', () => {
  it('reports transitions to a subscriber attached after construction', async () => {
    // The adapter cannot subscribe at construction time: the gate has to exist
    // before the query is built, because it *is* the query's PreToolUse hook, and
    // the thing the adapter does on expiry needs a session that only exists after.
    const { gate, scheduler } = harness({ defaultTimeoutMs: 60_000 });
    const seen: PauseGateEvent[] = [];
    gate.subscribe((event) => seen.push(event));

    await gate.requestPause();
    scheduler.fireByDuration(60_000);
    await flush();

    // The request is published before the barrier settles, so a late subscriber
    // still sees the full arc rather than only the outcome.
    expect(seen.map((e) => e.type)).toEqual(['pause_requested', 'pause_confirmed', 'pause_released']);
    // The expiry flag is the whole reason the adapter listens: a deliberate resume
    // and an expiry both release, but only one of them owes the model an
    // explanation for continuing on its own.
    expect(seen.filter((e) => e.type === 'pause_released')).toEqual([
      { type: 'pause_released', expired: true },
    ]);
  });

  it('stops reporting to an unsubscribed observer', async () => {
    const { gate } = harness();
    const seen: PauseGateEvent[] = [];
    const unsubscribe = gate.subscribe((event) => seen.push(event));

    await gate.requestPause();
    unsubscribe();
    await gate.resume();

    // Unsubscribing has to actually detach: the adapter unsubscribes when its
    // attempt is torn down, and an observer that kept firing would annotate a
    // session that no longer exists.
    expect(seen.map((e) => e.type)).toEqual(['pause_requested', 'pause_confirmed']);
  });

  it('completes the transition even when an observer throws', async () => {
    const { gate } = harness();
    gate.subscribe(() => {
      throw new Error('observer exploded');
    });
    const seen: PauseGateEvent[] = [];
    gate.subscribe((event) => seen.push(event));

    // A listener must not be able to fail a pause. By the time observers run the
    // transition is already decided, so propagating would report failure for a
    // pause that did take effect — the operator's UI and the run would disagree.
    await expect(gate.requestPause()).resolves.toEqual({ outcome: 'confirmed' });
    expect(gate.currentPhase()).toBe('paused');
    // And a broken observer must not silence the ones after it.
    expect(seen.map((e) => e.type)).toEqual(['pause_requested', 'pause_confirmed']);
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

describe('pause gate: a pause that goes quiet after its settle wait', () => {
  // Regression for the review's B4. The bug was that confirmation was a one-shot:
  // `awaitQuiescence` resolved once, and if its bounded timer fired first, nothing
  // ever re-drove the decision. A tool outliving the settle wait therefore left the
  // pause in `pause_requested` for its whole budget even after the run went
  // completely quiet — and then the expiry timer resumed it having never reported
  // that the pause did not take. The operator's view was "pausing…" for 30 minutes
  // followed by a silent resume.

  it('confirms once a late tool finishes, without needing another command', async () => {
    const h = harness({ settleTimeoutMs: 1_000 });
    const admitted = await h.gate.admit('Bash');

    const pausing = h.gate.requestPause();
    await flush();
    // The straggler outlives the bounded wait, so this request answers `requested`.
    h.scheduler.fireByDuration(1_000);
    await expect(pausing).resolves.toEqual({
      outcome: 'requested',
      reason: 'still waiting for 1 admitted tool(s) to finish',
    });
    expect(h.gate.currentPhase()).toBe('pause_requested');

    // The tool now reaches its safe boundary. That edge alone must confirm.
    h.gate.settle(admitted.ticket);
    await flush();

    expect(h.gate.currentPhase()).toBe('paused');
    expect(h.gate.activeToolCount()).toBe(0);
    expect(h.events.map((event) => event.type)).toContain('pause_confirmed');
  });

  it('still refuses to confirm when the late settle leaves background work behind', async () => {
    // The late edge must apply the *same* rules as the request path, or the
    // re-drive becomes a second, laxer definition of `paused`.
    const h = harness({ settleTimeoutMs: 1_000 });
    const admitted = await h.gate.admit('Bash');
    const pausing = h.gate.requestPause();
    await flush();
    h.scheduler.fireByDuration(1_000);
    await pausing;

    h.setBackground(2);
    h.gate.settle(admitted.ticket);
    await flush();

    expect(h.gate.currentPhase()).toBe('pause_requested');
    expect(h.events.map((event) => event.type)).not.toContain('pause_confirmed');
  });

  it('does not confirm a pause that was resumed while its straggler was still running', async () => {
    const h = harness({ settleTimeoutMs: 1_000 });
    const admitted = await h.gate.admit('Bash');
    const pausing = h.gate.requestPause();
    await flush();
    h.scheduler.fireByDuration(1_000);
    await pausing;

    await h.gate.resume();
    expect(h.gate.currentPhase()).toBe('running');

    // The tool finishes *after* the resume. The settle edge must not resurrect a
    // pause the operator already stood down.
    h.gate.settle(admitted.ticket);
    await flush();

    expect(h.gate.currentPhase()).toBe('running');
    expect(h.events.map((event) => event.type)).not.toContain('pause_confirmed');
  });
});

describe('pause gate: no pause is released without first resolving', () => {
  // The structural invariant whose absence let B4 hide. Every pause must reach the
  // operator as either a confirmation or a failure with a reason; `pause_released`
  // is the end of a pause that happened, not a substitute for either.

  it('reports unavailable when a budget expires before admitted work settles', async () => {
    const h = harness({ settleTimeoutMs: 1_000, defaultTimeoutMs: 10_000 });
    await h.gate.admit('Bash'); // never settles
    const pausing = h.gate.requestPause();
    await flush();
    h.scheduler.fireByDuration(1_000);
    await expect(pausing).resolves.toMatchObject({ outcome: 'requested' });

    h.scheduler.fireByDuration(10_000); // the budget expires
    await flush();

    const types = h.events.map((event) => event.type);
    const unavailable = types.indexOf('pause_unavailable');
    const released = types.indexOf('pause_released');
    expect(unavailable).toBeGreaterThanOrEqual(0);
    expect(released).toBeGreaterThan(unavailable);
    expect(h.gate.currentPhase()).toBe('running');
  });

  it('does not report unavailable when an expiry ends a pause that did confirm', async () => {
    // A confirmed pause reaching its budget is the designed auto-resume, not a
    // failure, so it must not be reported as one.
    const h = harness({ defaultTimeoutMs: 10_000 });
    await expect(h.gate.requestPause()).resolves.toEqual({ outcome: 'confirmed' });

    h.scheduler.fireByDuration(10_000);
    await flush();

    const types = h.events.map((event) => event.type);
    expect(types).toContain('pause_released');
    expect(types).not.toContain('pause_unavailable');
  });

  it('records an operator resume of a pending pause as released, not as a failure', async () => {
    // The operator changed their mind. Blaming the mechanism would misreport their
    // own decision back to them.
    const h = harness({ settleTimeoutMs: 1_000 });
    const admitted = await h.gate.admit('Bash');
    const pausing = h.gate.requestPause();
    await flush();
    h.scheduler.fireByDuration(1_000);
    await pausing;

    await h.gate.resume();
    h.gate.settle(admitted.ticket);
    await flush();

    expect(h.events.map((event) => event.type)).not.toContain('pause_unavailable');
  });
});
