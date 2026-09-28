/**
 * The run heartbeat and the post-completion exit watchdog — one production copy.
 *
 * ## Why this is a module rather than logic inside the worker's run loop
 *
 * Two of W2-05's claims (#3961, #5840) are about what an operator can *see* while a
 * run is paused: progress output has to keep arriving, and it has to say the run is
 * paused rather than simply going quiet — because going quiet is exactly what a
 * crashed run looks like. Both claims are about this heartbeat.
 *
 * While this logic lived inside `runAgent`, the only way to observe it was to start
 * a whole worker run, and a live pause experiment that builds its own
 * {@link PauseGate} could not be described by it at all: the worker's heartbeat
 * reads the worker's own gate, so its records are evidence about a *different*
 * execution. A parent worker can be healthy and unpaused while an experiment's gate
 * is paused, which makes the parent's heartbeat records the wrong evidence no matter
 * how long the experiment's pause runs. Raising the pause budget does not fix that;
 * sharing the code does.
 *
 * So the emitter takes what it used to read from its surroundings — which gate to
 * consult, when output last arrived, how many turns have happened, whether the query
 * has finished — and the worker passes its own values in. The live runner starts the
 * same production ticker against the gate it is actually pausing, so the heartbeat
 * records and the exit-watchdog decision describe the execution whose pause is being
 * measured.
 *
 * ## What must not change
 *
 * The worker's observable behaviour. An operator's paused-vs-stalled signal and a
 * force-exit are both things a refactor must not alter, so the thresholds (60s of
 * silence, a 30s tick, a 10-minute post-completion bound), the log level, the
 * message wording including its emoji, the `phase` values and the paused-tick fields
 * are all reproduced exactly as `runAgent` emitted them. The tests in
 * `run-heartbeat.test.ts` pin that wording rather than paraphrasing it.
 *
 * Exiting stays the caller's: this module decides and reports, and the worker turns
 * a force-exit decision into `process.exit`. A module that called `process.exit`
 * itself could not be unit-tested at all, and the one thing worth testing here is
 * exactly when it decides to.
 */
import type { PauseGate } from './pause-gate';

/** Silence before the first heartbeat is logged. */
export const HEARTBEAT_SILENCE_THRESHOLD_MS = 60_000;

/** How often the heartbeat wakes up to look. */
export const HEARTBEAT_INTERVAL_MS = 30_000;

/**
 * How long after the query completes a stream may stay open before a force-exit.
 *
 * Generous on purpose — in practice the stream closes within seconds, and the cost
 * of being wrong in this direction is a pod lingering rather than a run destroyed.
 */
export const POST_COMPLETION_TIMEOUT_MS = 10 * 60 * 1000;

/**
 * The run state each tick reads.
 *
 * Every member is a function, not a value, because a pause can begin and end
 * between two ticks: reading a captured boolean would describe the run as it was
 * when the ticker started. This is the same live-read rule the worker's inline
 * version followed.
 */
export interface RunHeartbeatSources {
  /** The control barrier for THIS execution, or `null`/absent when it cannot pause. */
  readonly gate?: () => PauseGate | null | undefined;
  /** When the run last saw output. */
  readonly lastActivityAt: () => number;
  /** Turn count, for the message. */
  readonly turnCount: () => number;
  /** When the query reported completion, or `null` if it has not. */
  readonly queryCompletedAt: () => number | null;
  /** Injectable clock, so tests do not wait out ten minutes. */
  readonly now?: () => number;
}

/** What one tick decided, returned so a caller can act and a test can assert. */
export interface RunHeartbeatTick {
  /** Silence at this tick, in whole seconds — the number the message carries. */
  readonly silentSeconds: number;
  /** The gate reported an active pause. */
  readonly paused: boolean;
  /** A heartbeat record was emitted (silence had reached the threshold). */
  readonly logged: boolean;
  /**
   * The post-completion watchdog decided to force-exit.
   *
   * The caller exits; this module does not. A `true` here during a valid pause is
   * the W2-05 failure `exit_watchdog_fired` exists to catch.
   */
  readonly forceExit: boolean;
  /**
   * The post-completion bound had elapsed, so the watchdog was **eligible** to fire
   * on this tick.
   *
   * Reported because `forceExit: false` alone is ambiguous, and the ambiguity is the
   * kind that produces fake evidence. A tick where the query has not completed — or
   * completed a second ago — never had a decision to make, so reading its `false` as
   * "the pause suppressed the watchdog" would credit the suppression for a bound
   * that was not yet due. Only `watchdogDue && !forceExit` is evidence of
   * suppression, and an observer that cannot tell the two apart should record
   * nothing.
   */
  readonly watchdogDue: boolean;
}

/** Where a tick's output goes. Matches the worker's own `log`/`console.log` pair. */
export interface RunHeartbeatSinks {
  readonly log: (level: string, message: string, context?: Record<string, unknown>) => void;
  readonly console?: (message: string) => void;
}

/**
 * Decide and emit one heartbeat tick.
 *
 * Exported separately from the ticker so the decision can be tested without timers,
 * and so the live runner can record exactly what each tick concluded.
 */
export function runHeartbeatTick(sources: RunHeartbeatSources, sinks: RunHeartbeatSinks): RunHeartbeatTick {
  const now = (sources.now ?? (() => Date.now()))();
  const silentSeconds = Math.round((now - sources.lastActivityAt()) / 1000);
  const turnCount = sources.turnCount();
  // Read live rather than captured: a pause can begin and end between two ticks.
  const gate = sources.gate?.();
  const paused = gate?.isPauseActive() === true;
  const completedAt = sources.queryCompletedAt();

  // Whether the bound had elapsed at all, computed independently of the pause so a
  // suppressed tick can still report that there *was* something to suppress. Without
  // it an observer cannot distinguish "the pause held the watchdog back" from "the
  // watchdog was never due", and only the first is evidence of suppression.
  const watchdogDue =
    completedAt !== null && completedAt !== 0 && now - completedAt >= POST_COMPLETION_TIMEOUT_MS;

  // Safety net: force exit if the stream hangs after query completion.
  //
  // Skipped while paused. This watchdog exists to catch a stream that never closed,
  // and it cannot distinguish that from a run whose last tool is parked at the
  // admission barrier — so left unguarded it would kill a healthy paused run within
  // POST_COMPLETION_TIMEOUT_MS, i.e. an operator pausing to look at something would
  // come back to a dead pod. The pause has its own bound (the gate's expiry timer,
  // clamped to the pod deadline), so skipping here defers to a bound rather than
  // removing one. Note the suppression is only about *starting* the exit: once the
  // pause is released, the elapsed comparison uses the original completion time, so
  // a stream that really is hung is still caught on the next tick.
  if (watchdogDue && !paused) {
    const elapsed = now - (completedAt as number);
    const msg = `⚠️  Force exit — stream did not close ${Math.round(elapsed / 1000)}s after query completed`;
    sinks.console?.(msg);
    sinks.log('WARN', msg, { phase: 'post-completion-timeout', elapsedMs: elapsed });
    return { silentSeconds, paused, logged: false, forceExit: true, watchdogDue };
  }

  // Visibility is preserved through a pause, not suppressed: the heartbeat keeps
  // logging, and says *why* it is quiet. An operator watching the log of a paused
  // run must be able to tell "paused, holding N tools" apart from "stalled", and a
  // silent log is the one thing that makes those identical.
  if (silentSeconds >= HEARTBEAT_SILENCE_THRESHOLD_MS / 1000) {
    const msg = paused
      ? `💓 Heartbeat — paused by operator, no SDK messages for ${silentSeconds}s (turn ${turnCount})`
      : `💓 Heartbeat — no SDK messages for ${silentSeconds}s (turn ${turnCount})`;
    sinks.console?.(msg);
    sinks.log('INFO', msg, {
      phase: 'heartbeat',
      silentSeconds,
      turn: turnCount,
      ...(paused
        ? {
            controlPhase: gate?.currentPhase(),
            heldTools: gate?.heldCount(),
            activeTools: gate?.activeToolCount(),
          }
        : {}),
    });
    return { silentSeconds, paused, logged: true, forceExit: false, watchdogDue };
  }

  return { silentSeconds, paused, logged: false, forceExit: false, watchdogDue };
}

/** A running heartbeat. Stop it in a `finally`, as the worker always has. */
export interface RunHeartbeatHandle {
  stop(): void;
}

export interface RunHeartbeatOptions extends RunHeartbeatSources {
  /** Called when the post-completion watchdog fires. The caller decides to exit. */
  readonly onForceExit?: (tick: RunHeartbeatTick) => void;
  /** Called after every tick. The live runner records ticks through this. */
  readonly onTick?: (tick: RunHeartbeatTick) => void;
  /** Tick spacing. Defaults to the production interval. */
  readonly intervalMs?: number;
}

/**
 * Start the production heartbeat for one execution.
 *
 * The returned handle's `stop` clears the interval, which is what the worker's
 * `clearInterval(heartbeat)` did. A throwing sink is swallowed per tick: an
 * observability failure must not be able to kill the run it is reporting on, which
 * is the same reasoning `PauseGate.subscribe` applies to its listeners.
 */
export function startRunHeartbeat(options: RunHeartbeatOptions, sinks: RunHeartbeatSinks): RunHeartbeatHandle {
  const timer = setInterval(() => {
    let tick: RunHeartbeatTick;
    try {
      tick = runHeartbeatTick(options, sinks);
    } catch {
      return;
    }
    try {
      options.onTick?.(tick);
      if (tick.forceExit) options.onForceExit?.(tick);
    } catch {
      // As above: a consumer's failure must not break the ticker.
    }
  }, options.intervalMs ?? HEARTBEAT_INTERVAL_MS);
  return {
    stop: () => clearInterval(timer),
  };
}
