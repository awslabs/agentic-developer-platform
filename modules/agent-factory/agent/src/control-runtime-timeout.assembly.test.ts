/**
 * Integration coverage for the live runner's own wiring — #5840.
 *
 * ## Why this file exists separately from `control-runtime-timeout.test.ts`
 *
 * The producer's suite covers the *decisions* (`mergePodObservations`,
 * `mergeExperimentPodObservations`, `recordWatchdogTick`). Two defects got past that
 * suite anyway, because both were in how the live runner **composes** those decisions:
 * a `{...acc, ...part}` reduce that dropped one experiment's pod group before the
 * field-wise merge could see it, and a `onForceExit` callback that recorded a verdict
 * it could not attribute. Green unit tests on the pieces say nothing about the
 * assembly of the pieces, so the assembly is exercised here directly.
 *
 * ## Why importing the live runner is safe in ordinary CI
 *
 * `control-runtime-timeout.integration.ts` is deliberately not named `.test.ts` so
 * jest cannot collect *it*, and the reason is spend: its experiments drive the real
 * CLI. But its only SDK-backed dependency (`resilientQuery`) is loaded through a lazy
 * `await import` inside the run function, so importing the module evaluates no SDK and
 * starts no subprocess. This file therefore imports the **real exported functions** —
 * not an AST-extracted or re-implemented copy — and calls only the pure assembly and
 * callback paths. No experiment is invoked, nothing is queried, and nothing spends.
 *
 * That distinction is the whole point: a re-implementation here would be a second copy
 * of the logic under test, and would have agreed with both defects.
 */
import { assemblePauseExpiryArtifact } from './control-runtime-timeout.integration';
import {
  recordWatchdogTick,
  EMPTY_WATCHDOG_OBSERVATIONS,
  type HeartbeatObservation,
  type LauncherPodObservations,
  type PauseExpiryObservations,
} from './control-runtime-timeout';
import {
  runHeartbeatTick,
  POST_COMPLETION_TIMEOUT_MS,
  type RunHeartbeatTick,
  type RunHeartbeatSources,
} from './run-heartbeat';
import { PauseGate } from './pause-gate';

/** The production annotation text the artifact is keyed on. */
const ANNOTATION = 'pause expired; resuming';

const PAUSE_WINDOW = { startedAt: 1_000, releasedAt: 9_000 };

/** A heartbeat record shaped exactly as the runner pairs them, inside the window. */
const RECORDS: HeartbeatObservation[] = [
  {
    at: 5_000,
    paused: true,
    controlPhase: 'paused',
    text: '💓 Heartbeat — paused by operator, no SDK messages for 90s (turn 2)',
  },
];

/**
 * A record as it would come back from a launcher's pod-log scrape: indistinguishable
 * in shape from the run's own, timestamped inside the run's pause window, and in fact
 * emitted by a different execution's gate. Nothing in the line itself reveals that,
 * which is exactly why it is inadmissible.
 */
const SCRAPED: HeartbeatObservation[] = [
  { at: 5_000, paused: true, controlPhase: 'paused', text: '💓 Heartbeat — paused by operator (parent worker)' },
];

describe('assemblePauseExpiryArtifact across several experiments', () => {
  /**
   * Root's executed reproduction, against the real exported assembly. Experiment A
   * (natural expiry) supplies the heartbeat records and the pause window; experiment E
   * (the due-watchdog scenario) supplies only its exit verdict; the launcher supplies
   * only `podKilled`. Before the fix the reduce let E's `pod` replace A's wholesale, so
   * the artifact reported no heartbeats for a run that had measured them — W2-05
   * failing on evidence that existed.
   */
  it('keeps experiment A’s heartbeats when experiment E contributes its pod verdict', () => {
    const parts: Array<Partial<PauseExpiryObservations>> = [
      { pauseWindow: PAUSE_WINDOW, pod: { heartbeats: RECORDS } },
      { pod: { exitWatchdogFired: false } },
    ];

    const artifact = assemblePauseExpiryArtifact({ parts, pod: { podKilled: false } });

    expect(artifact.heartbeats_during_pause).toBe(1);
    expect(artifact.exit_watchdog_fired).toBe(false);
    expect(artifact.pod_killed).toBe(false);
  });

  it('produces the same artifact whichever order the experiments ran in', () => {
    const a: Partial<PauseExpiryObservations> = { pauseWindow: PAUSE_WINDOW, pod: { heartbeats: RECORDS } };
    const e: Partial<PauseExpiryObservations> = { pod: { exitWatchdogFired: false } };

    expect(assemblePauseExpiryArtifact({ parts: [a, e] })).toEqual(
      assemblePauseExpiryArtifact({ parts: [e, a] }),
    );
  });

  /**
   * The failure must still outrank the pass through the whole assembly, not merely
   * inside the merge helper: a watchdog that fired during a pause is the W2-05 defect,
   * and a launcher scrape that saw nothing must not be able to file it away.
   */
  it('carries a firing from either source all the way into the artifact', () => {
    const fromExperiment = assemblePauseExpiryArtifact({
      parts: [{ pod: { exitWatchdogFired: true } }],
      pod: { exitWatchdogFired: false, podKilled: false },
    });
    const fromLauncher = assemblePauseExpiryArtifact({
      parts: [{ pod: { exitWatchdogFired: false } }],
      pod: { exitWatchdogFired: true, podKilled: false },
    });

    expect(fromExperiment.exit_watchdog_fired).toBe(true);
    expect(fromLauncher.exit_watchdog_fired).toBe(true);
  });

  /**
   * The run's own records take precedence over a launcher's when both exist. Note this
   * case only establishes *precedence* — it cannot establish that absence is not
   * backfilled, because nothing here is absent. That is the case below, and conflating
   * the two is how the backfill survived a test named for it.
   *
   * `pod` is cast because `LauncherPodObservations` has no `heartbeats` field at all,
   * which is the fix: a launcher cannot submit records through the real signature. The
   * cast reaches past the type to prove the runtime merge agrees with it.
   */
  it('prefers the run’s own records over a launcher’s when both are present', () => {
    const artifact = assemblePauseExpiryArtifact({
      parts: [{ pauseWindow: PAUSE_WINDOW, pod: { heartbeats: RECORDS } }, { pod: { exitWatchdogFired: false } }],
      pod: { podKilled: false, heartbeats: SCRAPED } as LauncherPodObservations,
    });

    expect(artifact.heartbeats_during_pause).toBe(1);
  });

  /**
   * Root's executed reproduction of the backfill, and the case the previous test was
   * named for but did not cover: the natural-expiry experiment contributes a pause
   * window but **no records at all**, and the launcher offers one whose timestamp falls
   * inside that window.
   *
   * Before the fix this reported one heartbeat during the pause and named no missing
   * input — a confident visibility verdict with no underlying observation, drawn from an
   * execution that was never paused. The gap disappearing is the worse half: it is what
   * would have told the launcher to go and collect the real thing.
   */
  it('reports no observation when the experiments measured none, whatever the launcher scraped', () => {
    const artifact = assemblePauseExpiryArtifact({
      parts: [{ pauseWindow: PAUSE_WINDOW }, { pod: { exitWatchdogFired: false } }],
      pod: { podKilled: false, heartbeats: SCRAPED } as LauncherPodObservations,
    });

    expect(artifact.heartbeats_during_pause).toBeNull();
    expect(artifact.paused_distinguishable_from_stalled).toBeNull();
    expect(artifact.evidence.unpaused_heartbeat_baseline).toBeNull();
    expect(artifact.missing_launcher_inputs.map((entry) => entry.field)).toContain(
      'heartbeats_during_pause / paused_distinguishable_from_stalled',
    );
  });

  /**
   * A measured zero is a measurement and must stay one: the experiment ran, the pause
   * was shorter than the emitter's interval, and zero is the honest answer. A launcher's
   * records must not be able to promote that zero into a one — the failure mode is
   * subtler than the absent case, since here nothing looks missing.
   */
  it('keeps an explicitly measured zero at zero rather than taking the launcher’s count', () => {
    const artifact = assemblePauseExpiryArtifact({
      parts: [{ pauseWindow: PAUSE_WINDOW, pod: { heartbeats: [] } }, { pod: { exitWatchdogFired: false } }],
      pod: { podKilled: false, heartbeats: SCRAPED } as LauncherPodObservations,
    });

    expect(artifact.heartbeats_during_pause).toBe(0);
    expect(artifact.paused_distinguishable_from_stalled).toBe(false);
    expect(artifact.missing_launcher_inputs.map((entry) => entry.field)).not.toContain(
      'heartbeats_during_pause / paused_distinguishable_from_stalled',
    );
  });

  /**
   * The restriction applies to the launcher only. Between two of this process's own
   * experiments, records may come from whichever one measured them — so a verdict-only
   * part folded in *first* must not block the records that arrive after it. This is the
   * regression the provenance fix could plausibly introduce, which is why it is pinned
   * through the real assembly rather than only through the merge helper.
   */
  it('still accepts a later experiment’s records when an earlier part carried only a verdict', () => {
    const artifact = assemblePauseExpiryArtifact({
      parts: [{ pod: { exitWatchdogFired: false } }, { pauseWindow: PAUSE_WINDOW, pod: { heartbeats: RECORDS } }],
      pod: { podKilled: false },
    });

    expect(artifact.heartbeats_during_pause).toBe(1);
    expect(artifact.paused_distinguishable_from_stalled).toBe(true);
    expect(artifact.exit_watchdog_fired).toBe(false);
  });

  /**
   * The ordinary complete shape, so the fix is not merely restrictive: every field that
   * has a legitimate source still arrives, from its own source.
   */
  it('assembles records, a separate watchdog verdict and the launcher’s kill together', () => {
    const artifact = assemblePauseExpiryArtifact({
      parts: [{ pauseWindow: PAUSE_WINDOW, pod: { heartbeats: RECORDS } }, { pod: { exitWatchdogFired: false } }],
      pod: { podKilled: false },
    });

    expect(artifact.heartbeats_during_pause).toBe(1);
    expect(artifact.exit_watchdog_fired).toBe(false);
    expect(artifact.pod_killed).toBe(false);
    // Everything collectable was collected, so nothing is named as outstanding.
    expect(artifact.missing_launcher_inputs).toEqual([]);
  });

  /**
   * A firing the launcher saw must still land even under the narrowed type: its
   * provenance is genuine, unlike a heartbeat's, and the two fields must not be
   * restricted together.
   */
  it('still accepts the launcher’s kill and firing reports', () => {
    const artifact = assemblePauseExpiryArtifact({
      parts: [{ pauseWindow: PAUSE_WINDOW, pod: { heartbeats: RECORDS } }],
      pod: { podKilled: true, exitWatchdogFired: true },
    });

    expect(artifact.pod_killed).toBe(true);
    expect(artifact.exit_watchdog_fired).toBe(true);
    // And the run's own records survive the launcher's contribution.
    expect(artifact.heartbeats_during_pause).toBe(1);
  });

  it('leaves a field nobody observed null, with the gap named', () => {
    const artifact = assemblePauseExpiryArtifact({ parts: [{ pauseWindow: PAUSE_WINDOW }] });

    expect(artifact.pod_killed).toBeNull();
    expect(artifact.missing_launcher_inputs.map((entry) => entry.field)).toContain('pod_killed');
  });

  /**
   * A measured zero is a measurement, not a gap. The distinction matters because
   * relabelling it invites someone to fill the field from another execution.
   */
  it('reports a measured zero as zero rather than as an uncollected field', () => {
    const artifact = assemblePauseExpiryArtifact({
      parts: [{ pauseWindow: PAUSE_WINDOW, pod: { heartbeats: [] } }],
      pod: { podKilled: false },
    });

    expect(artifact.heartbeats_during_pause).toBe(0);
    expect(artifact.missing_launcher_inputs.map((entry) => entry.field)).not.toContain('heartbeats_during_pause');
  });
});

/**
 * The runner's callback composition, driven by the real production emitter.
 *
 * `startRunHeartbeat` calls `onTick` and then `onForceExit`, and the second defect was
 * that recording the verdict from `onForceExit` cannot attribute it — that callback is
 * told a force-exit happened, not whether the pause was in force when it did. These
 * cases replay the exact tick sequence experiment E produces, using `runHeartbeatTick`
 * against a real {@link PauseGate}, and fold each tick through the same
 * `recordWatchdogTick` the runner's `onTick` uses.
 */
describe('the runner’s watchdog recording, over a real tick sequence', () => {
  const sinks = { log: () => {}, console: () => {} };

  /** A run whose post-completion bound is already elapsed, as experiment E faults it. */
  function dueSources(gate: PauseGate, now: number): RunHeartbeatSources {
    return {
      gate: () => gate,
      lastActivityAt: () => now - 90_000,
      turnCount: () => 2,
      queryCompletedAt: () => now - POST_COMPLETION_TIMEOUT_MS - 60_000,
      now: () => now,
    };
  }

  /**
   * Experiment E's whole sequence: due ticks while the pause holds, then the release,
   * then the due tick that must fire. The predicate E asserts is all three together,
   * and before the fix the final firing overwrote the during-pause verdict — so E
   * failed precisely when the production guard behaved correctly.
   */
  it('separates suppression during the pause from the required firing after release', async () => {
    const gate = new PauseGate({ defaultTimeoutMs: 600_000 });
    await gate.requestPause();
    let watchdog = EMPTY_WATCHDOG_OBSERVATIONS;
    const ticks: RunHeartbeatTick[] = [];

    for (const now of [100_000, 100_500]) {
      const tick = runHeartbeatTick(dueSources(gate, now), sinks);
      ticks.push(tick);
      watchdog = recordWatchdogTick(watchdog, tick);
    }
    await gate.resume();
    const afterRelease = runHeartbeatTick(dueSources(gate, 101_000), sinks);
    watchdog = recordWatchdogTick(watchdog, afterRelease);

    // Every tick genuinely had a decision to make, so none of this is vacuous.
    expect(ticks.every((tick) => tick.watchdogDue && tick.paused && !tick.forceExit)).toBe(true);
    expect(afterRelease.forceExit).toBe(true);

    // The three facts E's predicate requires, none contaminating another.
    expect(watchdog.dueWhilePaused).toBe(2);
    expect(watchdog.firedDuringPause).toBe(false);
    expect(watchdog.firedAfterRelease).toBe(true);

    gate.cancel();
  });

  /**
   * The experiment's verdict is what reaches the artifact, so the composition is
   * followed through the real assembly too: a correct run must report
   * `exit_watchdog_fired: false`, never the after-release firing.
   */
  it('publishes the during-pause verdict, not the after-release one', async () => {
    const gate = new PauseGate({ defaultTimeoutMs: 600_000 });
    await gate.requestPause();
    let watchdog = recordWatchdogTick(
      EMPTY_WATCHDOG_OBSERVATIONS,
      runHeartbeatTick(dueSources(gate, 100_000), sinks),
    );
    await gate.resume();
    watchdog = recordWatchdogTick(watchdog, runHeartbeatTick(dueSources(gate, 101_000), sinks));

    const artifact = assemblePauseExpiryArtifact({
      parts: [
        { pauseWindow: PAUSE_WINDOW, pod: { heartbeats: RECORDS } },
        // Exactly what experimentWatchdogDueDuringPause contributes.
        { pod: { exitWatchdogFired: watchdog.firedDuringPause as boolean } },
      ],
      pod: { podKilled: false },
    });

    expect(artifact.exit_watchdog_fired).toBe(false);
    expect(artifact.heartbeats_during_pause).toBe(1);

    gate.cancel();
  });

  /**
   * The guard whose deletion this whole field exists to detect. If `run-heartbeat.ts`
   * stopped suppressing during a pause, the very first due tick would fire and the
   * recorded verdict would become `true` — so the observation does change when the
   * behaviour it observes is removed, which is what makes it evidence.
   */
  it('would report a firing if the production guard stopped suppressing', async () => {
    const gate = new PauseGate({ defaultTimeoutMs: 600_000 });
    await gate.requestPause();

    // Simulates the unguarded module by feeding the tick shape it would return.
    const unguarded = recordWatchdogTick(EMPTY_WATCHDOG_OBSERVATIONS, {
      watchdogDue: true,
      paused: true,
      forceExit: true,
    });

    expect(unguarded.firedDuringPause).toBe(true);
    // And the real module, on the same state, does not.
    expect(runHeartbeatTick(dueSources(gate, 100_000), sinks).forceExit).toBe(false);

    gate.cancel();
  });
});
