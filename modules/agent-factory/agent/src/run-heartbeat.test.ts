/**
 * Tests for the shared heartbeat / exit-watchdog emitter — #5840.
 *
 * Two jobs. First, pin the worker's observable behaviour: this logic was lifted out
 * of `runAgent`, and an operator's paused-vs-stalled signal and a force-exit are
 * both things a refactor must not alter, so the wording, `phase` values, fields and
 * thresholds are asserted literally rather than paraphrased.
 *
 * Second, cover the pause suppression W2-05 depends on. The post-completion watchdog
 * cannot tell a stream that never closed from a run whose last tool is parked at the
 * barrier, so it must stand down while a pause is in force — otherwise pausing a run
 * to look at something becomes a way to lose it.
 *
 * `PauseGate` is the real production object throughout: the suppression being
 * asserted is its `isPauseActive()`, and a double would be asserting the double.
 */
import {
  runHeartbeatTick,
  startRunHeartbeat,
  HEARTBEAT_INTERVAL_MS,
  HEARTBEAT_SILENCE_THRESHOLD_MS,
  POST_COMPLETION_TIMEOUT_MS,
  type RunHeartbeatSources,
  type RunHeartbeatTick,
} from './run-heartbeat';
import { PauseGate } from './pause-gate';

interface Emitted {
  level: string;
  message: string;
  context?: Record<string, unknown>;
}

function sinks() {
  const logs: Emitted[] = [];
  const consoleLines: string[] = [];
  return {
    logs,
    consoleLines,
    sinks: {
      log: (level: string, message: string, context?: Record<string, unknown>) =>
        logs.push({ level, message, context }),
      console: (message: string) => consoleLines.push(message),
    },
  };
}

const NOW = 1_700_000_000_000;

/** An ordinary run: quiet long enough to log, query still in flight, no gate. */
function sources(overrides: Partial<RunHeartbeatSources> = {}): RunHeartbeatSources {
  return {
    lastActivityAt: () => NOW - 75_000,
    turnCount: () => 3,
    queryCompletedAt: () => null,
    now: () => NOW,
    ...overrides,
  };
}

describe('the heartbeat record the worker used to emit inline', () => {
  it('logs the unpaused wording and fields after the silence threshold', () => {
    const { logs, consoleLines, sinks: s } = sinks();

    const tick = runHeartbeatTick(sources(), s);

    expect(tick).toEqual({
      silentSeconds: 75,
      paused: false,
      logged: true,
      forceExit: false,
      // Nothing to suppress: the query has not completed, so the watchdog was never
      // due. See the watchdogDue describe block for why that distinction is recorded.
      watchdogDue: false,
    });
    expect(consoleLines).toEqual(['💓 Heartbeat — no SDK messages for 75s (turn 3)']);
    expect(logs).toEqual([
      {
        level: 'INFO',
        message: '💓 Heartbeat — no SDK messages for 75s (turn 3)',
        context: { phase: 'heartbeat', silentSeconds: 75, turn: 3 },
      },
    ]);
  });

  it('stays quiet below the silence threshold', () => {
    const { logs, sinks: s } = sinks();

    const tick = runHeartbeatTick(sources({ lastActivityAt: () => NOW - 30_000 }), s);

    expect(tick.logged).toBe(false);
    expect(logs).toEqual([]);
  });

  it('logs exactly at the threshold, not one tick later', () => {
    const { sinks: s } = sinks();

    const tick = runHeartbeatTick(
      sources({ lastActivityAt: () => NOW - HEARTBEAT_SILENCE_THRESHOLD_MS }),
      s,
    );

    expect(tick.logged).toBe(true);
    expect(tick.silentSeconds).toBe(60);
  });

  /**
   * The paused-vs-stalled signal. Going silent is the one thing a pause must not do,
   * because silence is what a hung run looks like — so the record has to both exist
   * and say why it is quiet.
   */
  it('says the run is paused and carries the barrier counts', async () => {
    const gate = new PauseGate({ defaultTimeoutMs: 60_000 });
    await gate.requestPause();
    const { logs, consoleLines, sinks: s } = sinks();

    const tick = runHeartbeatTick(sources({ gate: () => gate }), s);

    expect(tick.paused).toBe(true);
    expect(consoleLines).toEqual(['💓 Heartbeat — paused by operator, no SDK messages for 75s (turn 3)']);
    expect(logs[0].context).toEqual({
      phase: 'heartbeat',
      silentSeconds: 75,
      turn: 3,
      controlPhase: gate.currentPhase(),
      heldTools: 0,
      activeTools: 0,
    });
    // The wording an operator reads must contain the word, not merely the field:
    // the producer reads the message text as well as the structured fields.
    expect(logs[0].message).toMatch(/pause/i);
    gate.cancel();
  });

  it('reads the gate live, so a pause beginning between ticks is reported', async () => {
    const gate = new PauseGate({ defaultTimeoutMs: 60_000 });
    const src = sources({ gate: () => gate });
    const { sinks: s } = sinks();

    expect(runHeartbeatTick(src, s).paused).toBe(false);
    await gate.requestPause();
    expect(runHeartbeatTick(src, s).paused).toBe(true);
    gate.cancel();
  });

  /**
   * The clock the worker actually uses. Every other case injects `now` so it does
   * not have to wait out a threshold, which would leave the real default — the only
   * branch production takes — untested.
   */
  it('uses the wall clock when none is injected', () => {
    const { logs, sinks: s } = sinks();

    const tick = runHeartbeatTick(
      { lastActivityAt: () => Date.now() - 90_000, turnCount: () => 1, queryCompletedAt: () => null },
      s,
    );

    expect(tick.logged).toBe(true);
    expect(tick.silentSeconds).toBeGreaterThanOrEqual(90);
    expect(logs[0].message).toContain('no SDK messages for');
  });

  it('treats a run with no control gate as one that cannot pause', () => {
    const { logs, sinks: s } = sinks();

    const tick = runHeartbeatTick(sources({ gate: () => null }), s);

    expect(tick.paused).toBe(false);
    expect(logs[0].context).not.toHaveProperty('controlPhase');
  });
});

describe('the post-completion exit watchdog', () => {
  const completed = (sinceMs: number) => sources({ queryCompletedAt: () => NOW - sinceMs });

  it('forces an exit when the stream has not closed within the bound', () => {
    const { logs, sinks: s } = sinks();

    const tick = runHeartbeatTick(completed(POST_COMPLETION_TIMEOUT_MS), s);

    expect(tick.forceExit).toBe(true);
    expect(logs).toEqual([
      {
        level: 'WARN',
        message: '⚠️  Force exit — stream did not close 600s after query completed',
        context: { phase: 'post-completion-timeout', elapsedMs: POST_COMPLETION_TIMEOUT_MS },
      },
    ]);
  });

  it('leaves a recently completed run alone', () => {
    const { sinks: s } = sinks();

    expect(runHeartbeatTick(completed(5_000), s).forceExit).toBe(false);
  });

  it('does not fire on a run whose query has not completed', () => {
    const { sinks: s } = sinks();

    expect(runHeartbeatTick(sources(), s).forceExit).toBe(false);
  });

  /**
   * The W2-05 claim. The watchdog cannot distinguish a stream that never closed from
   * a last tool parked at the barrier, so if it fired during a valid pause, pausing a
   * run would be a way to lose it. The pause has its own bound, so standing down here
   * defers to a bound rather than removing one.
   */
  it('stands down while a pause is in force, however long the stream has been open', async () => {
    const gate = new PauseGate({ defaultTimeoutMs: 600_000 });
    await gate.requestPause();
    const { logs, sinks: s } = sinks();

    const tick = runHeartbeatTick(
      sources({ gate: () => gate, queryCompletedAt: () => NOW - POST_COMPLETION_TIMEOUT_MS * 3 }),
      s,
    );

    expect(tick.forceExit).toBe(false);
    expect(logs.some((entry) => entry.context?.phase === 'post-completion-timeout')).toBe(false);
    gate.cancel();
  });

  /**
   * Suppression must be a deferral, not an amnesty: a stream that really is hung is
   * still caught once the pause ends, and the elapsed comparison keeps using the
   * original completion time rather than restarting the clock.
   */
  it('catches a genuinely hung stream on the first tick after the pause ends', async () => {
    const gate = new PauseGate({ defaultTimeoutMs: 600_000 });
    await gate.requestPause();
    const src = sources({
      gate: () => gate,
      queryCompletedAt: () => NOW - POST_COMPLETION_TIMEOUT_MS - 1_000,
    });
    const { sinks: s } = sinks();

    expect(runHeartbeatTick(src, s).forceExit).toBe(false);
    await gate.resume();
    expect(runHeartbeatTick(src, s).forceExit).toBe(true);
    gate.cancel();
  });

  /**
   * `forceExit: false` is ambiguous on its own, and the ambiguity is what let a
   * vacuous observation pass as evidence: a run whose query never completed reports
   * `false` whether or not the pause guard exists. `watchdogDue` is what separates
   * "suppressed" from "never due", so only `watchdogDue && !forceExit` means the
   * guard did anything.
   */
  describe('watchdogDue separates a suppressed watchdog from one that was never due', () => {
    it('is false when the query has not completed, however long the silence', () => {
      const { sinks: s } = sinks();

      const tick = runHeartbeatTick(sources({ lastActivityAt: () => NOW - 3_600_000 }), s);

      expect(tick.watchdogDue).toBe(false);
      expect(tick.forceExit).toBe(false);
    });

    it('is false for a run that completed inside the bound', () => {
      const { sinks: s } = sinks();

      expect(runHeartbeatTick(completed(5_000), s).watchdogDue).toBe(false);
    });

    it('is true once the bound elapses, on the same tick that fires', () => {
      const { sinks: s } = sinks();

      const tick = runHeartbeatTick(completed(POST_COMPLETION_TIMEOUT_MS), s);

      expect(tick.watchdogDue).toBe(true);
      expect(tick.forceExit).toBe(true);
    });

    /**
     * The combination the live runner records as evidence, and the one that breaks if
     * the `!paused` guard is deleted: due, paused, and still no exit. Without the
     * `watchdogDue` half a reader cannot tell this tick from one that had nothing to
     * decide.
     */
    it('reports due-but-not-fired while a pause is in force', async () => {
      const gate = new PauseGate({ defaultTimeoutMs: 600_000 });
      await gate.requestPause();
      const { sinks: s } = sinks();

      const tick = runHeartbeatTick(
        sources({ gate: () => gate, queryCompletedAt: () => NOW - POST_COMPLETION_TIMEOUT_MS * 2 }),
        s,
      );

      expect(tick.watchdogDue).toBe(true);
      expect(tick.paused).toBe(true);
      expect(tick.forceExit).toBe(false);
      gate.cancel();
    });
  });

  it('reports a force-exit instead of exiting, so the caller decides', () => {
    const { sinks: s } = sinks();

    // No process.exit in this module: the assertion is simply that the test process
    // is still running to make it.
    expect(runHeartbeatTick(completed(POST_COMPLETION_TIMEOUT_MS), s).forceExit).toBe(true);
  });
});

describe('the ticker', () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  it('schedules only a function callback and clears the resulting interval', () => {
    const schedule = jest.spyOn(global, 'setInterval');
    const { sinks: heartbeatSinks } = sinks();
    try {
      const handle = startRunHeartbeat({ ...sources(), intervalMs: 321 }, heartbeatSinks);
      expect(typeof schedule.mock.calls[0][0]).toBe('function');
      expect(schedule.mock.calls[0][1]).toBe(321);
      handle.stop();
      expect(jest.getTimerCount()).toBe(0);
    } finally {
      schedule.mockRestore();
    }
  });

  it('ticks at the production interval until stopped', () => {
    const ticks: RunHeartbeatTick[] = [];
    const { sinks: s } = sinks();

    const handle = startRunHeartbeat({ ...sources(), onTick: (tick) => ticks.push(tick) }, s);
    jest.advanceTimersByTime(HEARTBEAT_INTERVAL_MS * 3);
    handle.stop();
    jest.advanceTimersByTime(HEARTBEAT_INTERVAL_MS * 3);

    expect(ticks).toHaveLength(3);
    expect(ticks.every((tick) => tick.logged)).toBe(true);
  });

  it('hands a force-exit decision to the caller', () => {
    const exits: RunHeartbeatTick[] = [];
    const { sinks: s } = sinks();

    const handle = startRunHeartbeat(
      {
        ...sources({ queryCompletedAt: () => NOW - POST_COMPLETION_TIMEOUT_MS }),
        onForceExit: (tick) => exits.push(tick),
      },
      s,
    );
    jest.advanceTimersByTime(HEARTBEAT_INTERVAL_MS);
    handle.stop();

    expect(exits).toHaveLength(1);
    expect(exits[0].forceExit).toBe(true);
  });

  it('survives a throwing sink, because observability must not kill the run', () => {
    const handle = startRunHeartbeat(sources(), {
      log: () => {
        throw new Error('CloudWatch is having a day');
      },
    });

    expect(() => jest.advanceTimersByTime(HEARTBEAT_INTERVAL_MS * 2)).not.toThrow();
    handle.stop();
  });

  it('survives a throwing tick consumer', () => {
    const { sinks: s } = sinks();
    const handle = startRunHeartbeat(
      {
        ...sources(),
        onTick: () => {
          throw new Error('recorder failed');
        },
      },
      s,
    );

    expect(() => jest.advanceTimersByTime(HEARTBEAT_INTERVAL_MS * 2)).not.toThrow();
    handle.stop();
  });
});
