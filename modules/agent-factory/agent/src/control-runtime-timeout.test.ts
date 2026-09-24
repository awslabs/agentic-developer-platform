/**
 * Tests for the `pause_expiry` measurement logic — #5840.
 *
 * These cover the half of the W2-05 producer that ordinary CI can run: given a
 * set of observations, does the artifact say what actually happened? The live
 * measurement itself needs a real model and belongs to
 * `control-runtime-timeout.integration.ts`, which jest cannot collect.
 *
 * The cases are chosen around the ways a producer can be dishonest rather than
 * around code coverage: a missing observation reported as a pass, a natural expiry
 * claimed from an explicit resume, "no extra turn" inferred from the option that
 * was requested instead of from the stream, and a vacuous pass from a run that
 * never held any work.
 *
 * `PauseGate` and `controlDeadlineAt` are the real production objects here, not
 * doubles: the clamp arithmetic being asserted is theirs, and re-stating it in a
 * fake would test the fake.
 */
import {
  buildPauseExpiryArtifact,
  mergePodObservations,
  mergeExperimentPodObservations,
  recordWatchdogTick,
  EMPTY_WATCHDOG_OBSERVATIONS,
  REQUIRED_LAUNCHER_INPUTS,
  NEUTRAL_GATE_EVENT_TYPES,
  type PauseExpiryObservations,
  type HeartbeatObservation,
  type LauncherPodObservations,
  type PodObservations,
} from './control-runtime-timeout';
import { PauseGate, DEFAULT_FINALIZATION_MARGIN_MS } from './pause-gate';
import { controlDeadlineAt } from './control-deadline';

const ANNOTATION = 'Operator pause expired after its time budget and the run resumed automatically.';
/** The call the barrier parked. Its result — not any result — decides the turn question. */
const HELD_ID = 'toolu_held_write';
/** Some other tool completing in the same window. Must never stand in for the held one. */
const OTHER_ID = 'toolu_unrelated_read';

/** A fully observed, honest natural-expiry run. Cases below remove one thing at a time. */
function completeObservations(): PauseExpiryObservations {
  return {
    expectedAnnotationText: ANNOTATION,
    gateEvents: [
      { type: 'pause_requested' },
      { type: 'pause_confirmed' },
      { type: 'pause_released', expired: true },
    ],
    explicitResumeCalls: 0,
    deliveries: [{ kind: 'annotation', text: ANNOTATION, result: 'delivered', at: 1_000 }],
    heldToolUseId: HELD_ID,
    // The *held* tool admitted first, model output only after it: no new turn was
    // provoked. The id is what makes this readable — see the identity cases below.
    stream: [
      { kind: 'tool_result', at: 1_100, toolUseId: HELD_ID },
      { kind: 'assistant', at: 1_200 },
      { kind: 'result', at: 1_300 },
    ],
    pauseWindow: { startedAt: 0, releasedAt: 1_000 },
    idleRetryFired: false,
    spillOutputPreserved: true,
    pod: {
      podKilled: false,
      exitWatchdogFired: false,
      heartbeats: [
        { at: 100, paused: true, controlPhase: 'paused', text: '💓 Heartbeat — paused by operator, no SDK messages for 60s' },
        { at: 900, paused: true, controlPhase: 'paused', text: '💓 Heartbeat — paused by operator, no SDK messages for 120s' },
      ],
    },
    deadlineClamp: {
      grantedMs: 30_000,
      remainingMs: 300_000,
      finalizationMarginMs: DEFAULT_FINALIZATION_MARGIN_MS,
      nonpositiveRequest: { outcome: 'unavailable', failure: 'no_safe_budget' },
    },
    cancellation: {
      heldWorkDecisions: ['deny'],
      unresolvedHeldWork: 0,
      deliveriesAfterCancel: [],
    },
    heldHookTimeout: {
      signalAborted: true,
      safetyReleaseUsed: false,
      phaseAfterTimeout: 'running',
      unavailableReason: 'the harness abandoned a parked tool, so the pause did not take',
      hookTimeoutSeconds: 1860,
      pauseBudgetSeconds: 1800,
      forcedTimeoutSeconds: 1,
    },
  };
}

describe('buildPauseExpiryArtifact — a fully observed run', () => {
  it('reports every W2-05 field from the observations', () => {
    const artifact = buildPauseExpiryArtifact(completeObservations());

    expect(artifact.auto_resumed).toBe(true);
    expect(artifact.annotation_count).toBe(1);
    expect(artifact.extra_assistant_turn).toBe(false);
    expect(artifact.neutral_annotation).toBe(true);
    expect(artifact.resolved_before_release).toBe(true);
    expect(artifact.pod_killed).toBe(false);
    expect(artifact.idle_retry_fired).toBe(false);
    expect(artifact.exit_watchdog_fired).toBe(false);
    expect(artifact.heartbeats_during_pause).toBe(2);
    expect(artifact.paused_distinguishable_from_stalled).toBe(true);
    expect(artifact.spill_output_preserved).toBe(true);
    expect(artifact.missing_launcher_inputs).toEqual([]);
  });

  it('reports the held-hook-timeout block in the shape the evaluator reads', () => {
    const artifact = buildPauseExpiryArtifact(completeObservations());

    expect(artifact.held_hook_timeout).toMatchObject({
      exercised: true,
      state: 'running',
      reason: 'the harness abandoned a parked tool, so the pause did not take',
      hook_timeout_seconds: 1860,
      pause_budget_seconds: 1800,
    });
  });
});

/**
 * The held-hook bound is the one the adapter does not enforce itself, so
 * `exercised` must be a conjunction of two independent observations rather than an
 * assertion the producer can make about itself.
 */
describe('the held-hook timeout is derived, not asserted', () => {
  const hook = () => completeObservations().heldHookTimeout!;

  it('is not exercised when the CLI never abandoned the hook', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      heldHookTimeout: { ...hook(), signalAborted: false },
    });

    expect((artifact.held_hook_timeout as Record<string, unknown>).exercised).toBe(false);
  });

  it('is unobserved when the abandonment signal itself was never read', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      heldHookTimeout: { ...hook(), signalAborted: null },
    });

    expect((artifact.held_hook_timeout as Record<string, unknown>).exercised).toBeNull();
  });

  it('is not exercised when the probe released the pause itself', () => {
    // The safety release exists so a missing timeout fails after a bounded wait.
    // Counting it as the timeout would let the fixture's own cleanup stand in for
    // the CLI behaviour under test.
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      heldHookTimeout: { ...hook(), safetyReleaseUsed: true },
    });

    expect((artifact.held_hook_timeout as Record<string, unknown>).exercised).toBe(false);
  });

  it('is not exercised when the abandonment produced no reported outcome', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      heldHookTimeout: { ...hook(), unavailableReason: null },
    });

    const block = artifact.held_hook_timeout as Record<string, unknown>;
    expect(block.exercised).toBe(false);
    // Passed through as observed: an empty reason is a defect the evaluator must
    // see, not one this producer repairs on the way out.
    expect(block.reason).toBeNull();
  });

  it('passes a still-paused state through so the evaluator can reject it', () => {
    // A pause whose hook timed out but still reports `paused` is showing
    // containment that has already lapsed. Normalizing it here would hide exactly
    // the defect the check exists to find.
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      heldHookTimeout: { ...hook(), phaseAfterTimeout: 'paused' },
    });

    expect((artifact.held_hook_timeout as Record<string, unknown>).state).toBe('paused');
  });

  it('records both bounds as observed, including a bound that does not exceed the budget', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      heldHookTimeout: { ...hook(), hookTimeoutSeconds: 1800, pauseBudgetSeconds: 1800 },
    });

    const block = artifact.held_hook_timeout as Record<string, unknown>;
    expect(block.hook_timeout_seconds).toBe(1800);
    expect(block.pause_budget_seconds).toBe(1800);
  });
});

describe('a missing observation stays missing', () => {
  /**
   * The central rule. Every field the evaluator reads must be `null` when nothing
   * was observed, because `check_w2_05` requires a specific value and `null` fails
   * it — which is the point. A passing default here would let an experiment that
   * never ran satisfy the wave.
   */
  it('reports null for every field when nothing was observed', () => {
    const artifact = buildPauseExpiryArtifact({ expectedAnnotationText: ANNOTATION });

    expect(artifact.auto_resumed).toBeNull();
    expect(artifact.annotation_count).toBeNull();
    expect(artifact.extra_assistant_turn).toBeNull();
    expect(artifact.neutral_annotation).toBeNull();
    expect(artifact.resolved_before_release).toBeNull();
    expect(artifact.pod_killed).toBeNull();
    expect(artifact.idle_retry_fired).toBeNull();
    expect(artifact.exit_watchdog_fired).toBeNull();
    expect(artifact.heartbeats_during_pause).toBeNull();
    expect(artifact.paused_distinguishable_from_stalled).toBeNull();
    expect(artifact.spill_output_preserved).toBeNull();
    expect(artifact.held_hook_timeout).toBeNull();
    expect(artifact.deadline_clamp).toBeNull();
    expect(artifact.cancellation).toBeNull();
  });

  it('names the exact launcher input to collect for each pod-level gap', () => {
    const artifact = buildPauseExpiryArtifact({ expectedAnnotationText: ANNOTATION });

    expect(artifact.missing_launcher_inputs).toEqual(REQUIRED_LAUNCHER_INPUTS);
    for (const entry of artifact.missing_launcher_inputs) {
      expect(entry.collect.length).toBeGreaterThan(80);
    }
  });

  it('reports only the gaps that are actually missing', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      pod: { podKilled: false, heartbeats: completeObservations().pod!.heartbeats },
    });

    expect(artifact.exit_watchdog_fired).toBeNull();
    expect(artifact.missing_launcher_inputs.map((entry) => entry.field)).toEqual(['exit_watchdog_fired']);
  });

  /**
   * The visibility fields were once a permanent hand-off, because the only copy of
   * the heartbeat lived inside the worker's run loop. The live runner now starts that
   * same emitter against the gate it pauses, so a run can measure them itself — and
   * when it has, the artifact must stop asking the launcher for them. Anything else
   * reads as an outstanding gap for evidence that is already in hand.
   */
  it('stops asking for what the run measured in-process, leaving only pod_killed', () => {
    const observed = completeObservations();
    const artifact = buildPauseExpiryArtifact({
      ...observed,
      // Exactly what the in-process emitter can supply: its own ticks and its own
      // watchdog verdict. Nothing has watched the pod from outside.
      pod: { exitWatchdogFired: false, heartbeats: observed.pod!.heartbeats },
    });

    expect(artifact.heartbeats_during_pause).toBe(2);
    expect(artifact.paused_distinguishable_from_stalled).toBe(true);
    expect(artifact.exit_watchdog_fired).toBe(false);
    expect(artifact.missing_launcher_inputs.map((entry) => entry.field)).toEqual(['pod_killed']);
  });

  /**
   * "Collected" and "favourable" are different questions. A run whose pause was too
   * short to produce a tick measured a real zero, and the artifact must report that
   * zero — which fails W2-05 — rather than relabel it as uncollected, which would
   * invite a launcher to supply the field from somewhere else.
   */
  it('treats a measured zero as measured, not as a gap to be filled', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      pod: { podKilled: false, exitWatchdogFired: false, heartbeats: [] },
    });

    expect(artifact.heartbeats_during_pause).toBe(0);
    expect(artifact.paused_distinguishable_from_stalled).toBe(false);
    expect(artifact.missing_launcher_inputs).toEqual([]);
  });
});

/**
 * Two sources now write into one observation group: the run's own emitter, and the
 * launcher watching the pod. The merge is here rather than in the live runner
 * precisely so these cases can run — the runner's import graph reaches the SDK.
 */
describe('combining the in-process and launcher views of the pod', () => {
  const inProcess: PodObservations = {
    exitWatchdogFired: false,
    heartbeats: [{ at: 100, paused: true, controlPhase: 'paused', text: '💓 Heartbeat — paused by operator' }],
  };

  it('keeps both sides, since each sees what the other cannot', () => {
    const merged = mergePodObservations(inProcess, { podKilled: false });

    expect(merged).toEqual({ podKilled: false, exitWatchdogFired: false, heartbeats: inProcess.heartbeats });
  });

  /**
   * The regression that motivated a field-wise merge. A launcher that supplies only
   * `podKilled` must not erase heartbeats the run genuinely recorded — the artifact
   * would then report an unobserved visibility field for a run that observed it, and
   * W2-05 would fail on evidence that existed.
   */
  it('does not let a podKilled-only launcher report discard measured heartbeats', () => {
    expect(mergePodObservations(inProcess, { podKilled: false })?.heartbeats).toHaveLength(1);
  });

  it('does not let an absent launcher field overwrite a measured one', () => {
    const merged = mergePodObservations(inProcess, { podKilled: true, exitWatchdogFired: undefined });

    expect(merged?.exitWatchdogFired).toBe(false);
    expect(merged?.podKilled).toBe(true);
  });

  /**
   * A launcher watching the pod log can see a firing the in-process emitter missed —
   * the pod is killed, so nothing in-process gets to report it.
   */
  it('accepts a firing the launcher saw and the run did not', () => {
    expect(mergePodObservations(inProcess, { exitWatchdogFired: true })?.exitWatchdogFired).toBe(true);
  });

  /**
   * The conflict rule, in the direction that matters. These booleans are failure
   * reports, not opinions: a launcher whose log scrape came up empty must not be
   * able to overwrite a firing the in-process emitter actually recorded. Last-writer-
   * wins here is a real failure silently downgraded to a pass, and the contract text
   * says a firing from EITHER source fails the field.
   */
  it('keeps a firing the run recorded even when the launcher reports none', () => {
    const merged = mergePodObservations(
      { exitWatchdogFired: true },
      { exitWatchdogFired: false, podKilled: false },
    );

    expect(merged?.exitWatchdogFired).toBe(true);
    expect(merged?.podKilled).toBe(false);
  });

  it('keeps a kill either side reported, in both orders', () => {
    expect(mergePodObservations({ podKilled: true }, { podKilled: false })?.podKilled).toBe(true);
    expect(mergePodObservations({ podKilled: false }, { podKilled: true })?.podKilled).toBe(true);
  });

  it('agrees on false only when both sides observed false', () => {
    expect(mergePodObservations({ exitWatchdogFired: false }, { exitWatchdogFired: false })?.exitWatchdogFired).toBe(
      false,
    );
  });

  /**
   * A field neither side observed must stay absent, not become an explicit
   * `undefined` member: the producer treats those the same way, but an artifact
   * reader seeing the key would take it for a taken measurement.
   */
  it('leaves an unobserved field off the object entirely', () => {
    const merged = mergePodObservations({ heartbeats: inProcess.heartbeats }, undefined);

    expect(merged).not.toHaveProperty('podKilled');
    expect(merged).not.toHaveProperty('exitWatchdogFired');
    expect(buildPauseExpiryArtifact({ ...completeObservations(), pod: merged }).pod_killed).toBeNull();
  });

  /**
   * Heartbeats are not concatenated. A launcher scraping the pod log cannot tell the
   * experiment's own ticks from a parent worker's records for a different execution,
   * so merging the two would put another execution's evidence into this run's count —
   * the exact substitution that made these fields unacceptable before.
   *
   * The cast is the point of the fix: `LauncherPodObservations` has no `heartbeats`
   * field, so this call does not type-check through the real signature. It is forced
   * here to show the runtime merge agrees with the type rather than relying on it.
   */
  it('never mixes launcher-scraped records into the run\'s own heartbeat count', () => {
    const parentWorkerRecords: HeartbeatObservation[] = [
      { at: 200, paused: false, text: '💓 Heartbeat — no SDK messages for 60s' },
      { at: 300, paused: false, text: '💓 Heartbeat — no SDK messages for 90s' },
    ];

    const merged = mergePodObservations(inProcess, {
      podKilled: false,
      heartbeats: parentWorkerRecords,
    } as LauncherPodObservations);

    expect(merged?.heartbeats).toEqual(inProcess.heartbeats);
  });

  /**
   * No fallback. A run that measured no records of its own reports none, even when the
   * launcher scraped some: those records belong to whichever execution in that pod
   * emitted them, and for the ordinary worker running beside the experiment that is a
   * different gate that was never paused. Reporting them would be a visibility verdict
   * with no observation behind it, and would also erase the gap that tells the launcher
   * the measurement still needs taking.
   */
  it('never substitutes launcher records for records the run did not measure', () => {
    const scraped: HeartbeatObservation[] = [{ at: 200, paused: true, controlPhase: 'paused', text: 'paused' }];

    const merged = mergePodObservations({ podKilled: false }, { heartbeats: scraped } as LauncherPodObservations);

    expect(merged).not.toHaveProperty('heartbeats');
    const artifact = buildPauseExpiryArtifact({
      expectedAnnotationText: ANNOTATION,
      pauseWindow: { startedAt: 100, releasedAt: 900 },
      pod: merged,
    });
    expect(artifact.heartbeats_during_pause).toBeNull();
    expect(artifact.missing_launcher_inputs.map((entry) => entry.field)).toContain(
      'heartbeats_during_pause / paused_distinguishable_from_stalled',
    );
  });

  /**
   * An empty array is a measurement of zero, not an absence, so it must survive the
   * merge intact rather than being treated as "nothing measured" and replaced.
   */
  it('keeps a measured zero distinct from an unmeasured field', () => {
    const merged = mergePodObservations({ heartbeats: [] }, {
      podKilled: false,
      heartbeats: [{ at: 200, paused: true, controlPhase: 'paused', text: 'paused' }],
    } as LauncherPodObservations);

    expect(merged?.heartbeats).toEqual([]);
    expect(
      buildPauseExpiryArtifact({
        expectedAnnotationText: ANNOTATION,
        pauseWindow: { startedAt: 100, releasedAt: 900 },
        pod: merged,
      }).heartbeats_during_pause,
    ).toBe(0);
  });

  it('stays absent when neither source observed anything, so the gap is still named', () => {
    expect(mergePodObservations(undefined, undefined)).toBeUndefined();
    expect(buildPauseExpiryArtifact({ expectedAnnotationText: ANNOTATION }).pod_killed).toBeNull();
  });

  it('carries one side through unchanged when it is the only one', () => {
    expect(mergePodObservations(inProcess, undefined)).toEqual(inProcess);
    expect(mergePodObservations(undefined, { podKilled: false })).toEqual({ podKilled: false });
  });
});

/**
 * Combining several of *this process's* experiments, which is a different problem
 * from reconciling the run with a launcher: here the heartbeats and the watchdog
 * verdict are measured by two different scenarios, so whichever is folded in second
 * would win a group-level spread and silently erase the other.
 */
describe('combining the pod views of several experiments', () => {
  const records: HeartbeatObservation[] = [
    { at: 100, paused: true, controlPhase: 'paused', text: '💓 Heartbeat — paused by operator' },
  ];

  it('keeps one experiment’s heartbeats when another contributes only a verdict', () => {
    const merged = mergeExperimentPodObservations([{ heartbeats: records }, { exitWatchdogFired: false }]);

    expect(merged?.heartbeats).toEqual(records);
    expect(merged?.exitWatchdogFired).toBe(false);
  });

  it('is order-independent, so run order cannot change the evidence', () => {
    expect(mergeExperimentPodObservations([{ exitWatchdogFired: false }, { heartbeats: records }])).toEqual(
      mergeExperimentPodObservations([{ heartbeats: records }, { exitWatchdogFired: false }]),
    );
  });

  /**
   * The launcher's provenance restriction must not leak into this merge. Between two of
   * this process's own experiments either side may be the one that measured the records,
   * so a verdict-only part folded in first must not block the records arriving after it —
   * which is what applying the launcher rule uniformly would do.
   */
  it('accepts records from a later part when the earlier one carried only a verdict', () => {
    const merged = mergeExperimentPodObservations([{ exitWatchdogFired: false }, { heartbeats: records }]);

    expect(merged?.heartbeats).toEqual(records);
    expect(merged?.exitWatchdogFired).toBe(false);
  });

  /** A measured zero from an earlier part is a measurement and outranks a later set. */
  it('keeps an earlier part’s measured zero rather than a later part’s records', () => {
    expect(mergeExperimentPodObservations([{ heartbeats: [] }, { heartbeats: records }])?.heartbeats).toEqual([]);
  });

  it('still resolves a disagreement toward the failure', () => {
    expect(
      mergeExperimentPodObservations([{ exitWatchdogFired: true }, { exitWatchdogFired: false }])
        ?.exitWatchdogFired,
    ).toBe(true);
  });

  /**
   * Two experiments are two executions with two gates, one of which faults its
   * completion clock. Appending its ticks to the other's would report a faulted run's
   * records inside an unfaulted run's count.
   */
  it('never concatenates two experiments’ heartbeat streams', () => {
    const other: HeartbeatObservation[] = [{ at: 900, paused: false, controlPhase: null, text: 'other run' }];

    expect(mergeExperimentPodObservations([{ heartbeats: records }, { heartbeats: other }])?.heartbeats).toEqual(
      records,
    );
  });

  it('skips experiments that observed nothing about the pod', () => {
    expect(mergeExperimentPodObservations([undefined, undefined])).toBeUndefined();
    expect(mergeExperimentPodObservations([undefined, { podKilled: true }, undefined])).toEqual({ podKilled: true });
  });
});

/**
 * The accumulation rule for watchdog ticks. This lives in the producer precisely so
 * it is covered here: it decides which ticks count as evidence, and the two ways to
 * get it wrong both manufacture a confident, false answer — crediting the pause for
 * suppressing a watchdog that was never due, or letting the required after-release
 * firing be reported as a firing *during* the pause.
 */
describe('accumulating what the exit watchdog decided, tick by tick', () => {
  const due = { watchdogDue: true, paused: true, forceExit: false };

  it('ignores a tick whose bound had not elapsed', () => {
    const after = recordWatchdogTick(EMPTY_WATCHDOG_OBSERVATIONS, {
      watchdogDue: false,
      paused: true,
      forceExit: false,
    });

    expect(after).toEqual(EMPTY_WATCHDOG_OBSERVATIONS);
    expect(after.firedDuringPause).toBeNull();
  });

  it('records a suppressed firing only once the bound was genuinely due', () => {
    const after = recordWatchdogTick(EMPTY_WATCHDOG_OBSERVATIONS, due);

    expect(after).toEqual({ firedDuringPause: false, dueTicks: 1, dueWhilePaused: 1, firedAfterRelease: null });
  });

  /**
   * Root's reproduction, as a test. The scenario that makes `exit_watchdog_fired`
   * mean anything *requires* a firing after release — so if that firing is recorded
   * without asking whether the pause was in force, the experiment fails exactly when
   * it succeeds, and a healthy paused run is reported as watchdog-killed.
   */
  it('does not let a firing after release contaminate the during-pause verdict', () => {
    const paused = recordWatchdogTick(EMPTY_WATCHDOG_OBSERVATIONS, due);
    const released = recordWatchdogTick(paused, { watchdogDue: true, paused: false, forceExit: true });

    expect(released.firedDuringPause).toBe(false);
    expect(released.firedAfterRelease).toBe(true);
    expect(released.dueWhilePaused).toBe(1);
    expect(released.dueTicks).toBe(2);
  });

  it('latches a firing that happened while paused, so a later quiet tick cannot undo it', () => {
    const fired = recordWatchdogTick(EMPTY_WATCHDOG_OBSERVATIONS, { ...due, forceExit: true });
    const later = recordWatchdogTick(fired, due);

    expect(fired.firedDuringPause).toBe(true);
    expect(later.firedDuringPause).toBe(true);
  });

  /**
   * The converse of the latch, and the failure-downgraded-to-pass class: a first tick
   * reporting `false` must not make a later real firing unrecordable.
   */
  it('lets a later firing overturn an earlier pass', () => {
    const quiet = recordWatchdogTick(EMPTY_WATCHDOG_OBSERVATIONS, due);

    expect(recordWatchdogTick(quiet, { ...due, forceExit: true }).firedDuringPause).toBe(true);
  });

  it('counts a due unpaused tick without claiming anything about the pause', () => {
    const after = recordWatchdogTick(EMPTY_WATCHDOG_OBSERVATIONS, {
      watchdogDue: true,
      paused: false,
      forceExit: false,
    });

    expect(after.firedDuringPause).toBeNull();
    expect(after.firedAfterRelease).toBe(false);
    expect(after.dueWhilePaused).toBe(0);
  });

  it('does not mutate the observations it was given', () => {
    recordWatchdogTick(EMPTY_WATCHDOG_OBSERVATIONS, { ...due, forceExit: true });

    expect(EMPTY_WATCHDOG_OBSERVATIONS).toEqual({
      firedDuringPause: null,
      dueTicks: 0,
      dueWhilePaused: 0,
      firedAfterRelease: null,
    });
  });
});

describe('natural expiry versus an explicit resume', () => {
  /**
   * The substitution this guards against is the easy one to make: pausing, calling
   * `resume()`, and recording the same end state. The events look nearly identical,
   * so the disqualifier has to be the resume call itself.
   */
  it('refuses to claim auto-resume when an explicit resume was called', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      gateEvents: [
        { type: 'pause_requested' },
        { type: 'pause_confirmed' },
        { type: 'pause_released', expired: false },
      ],
      explicitResumeCalls: 1,
    });

    expect(artifact.auto_resumed).toBe(false);
  });

  it('refuses to claim auto-resume when a resume ran even though the release was marked expired', () => {
    const artifact = buildPauseExpiryArtifact({ ...completeObservations(), explicitResumeCalls: 1 });

    expect(artifact.auto_resumed).toBe(false);
  });

  it('reports auto-resume as unobserved when no resume count was recorded', () => {
    const { explicitResumeCalls: _omitted, ...rest } = completeObservations();

    expect(buildPauseExpiryArtifact(rest).auto_resumed).toBeNull();
  });

  it('reports false, not null, when the pause never released at all', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      gateEvents: [{ type: 'pause_requested' }, { type: 'pause_confirmed' }],
    });

    // A pause that never ended is an observed failure of auto-resume, not an
    // absence of evidence — the pod outlived its own pause.
    expect(artifact.auto_resumed).toBe(false);
  });
});

describe('the pause outcome is reported before the pause ends', () => {
  it('fails when the release is the first thing the operator would have seen', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      // "pausing…" for the whole budget, then a silent resume.
      gateEvents: [{ type: 'pause_requested' }, { type: 'pause_released', expired: true }],
    });

    expect(artifact.resolved_before_release).toBe(false);
  });

  it('accepts an honest failure reported before the release', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      gateEvents: [
        { type: 'pause_requested' },
        { type: 'pause_unavailable', failure: 'background_work' },
        { type: 'pause_released', expired: true },
      ],
    });

    expect(artifact.resolved_before_release).toBe(true);
  });
});

describe('annotation count and duplication', () => {
  it('counts exactly one delivered expiry annotation', () => {
    expect(buildPauseExpiryArtifact(completeObservations()).annotation_count).toBe(1);
  });

  it('counts a duplicate rather than collapsing it to one', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      deliveries: [
        { kind: 'annotation', text: ANNOTATION, result: 'delivered', at: 1_000 },
        { kind: 'annotation', text: ANNOTATION, result: 'delivered', at: 1_050 },
      ],
    });

    expect(artifact.annotation_count).toBe(2);
  });

  it('does not count an annotation the transport rejected', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      deliveries: [{ kind: 'annotation', text: ANNOTATION, result: 'rejected', at: 1_000 }],
    });

    // Undelivered is zero delivered, and zero leaves the model on a stale belief.
    expect(artifact.annotation_count).toBe(0);
    expect(artifact.neutral_annotation).toBe(false);
  });

  it('does not count an unrelated annotation as the expiry note', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      deliveries: [{ kind: 'annotation', text: 'some other operator note', result: 'delivered', at: 1_000 }],
    });

    expect(artifact.annotation_count).toBe(0);
  });
});

describe('the extra-turn question is answered from the stream, not from the request', () => {
  /**
   * `shouldQuery:false` is what the adapter asked for. Whether the provider then
   * started a turn is a different fact, and it is the one W2-05 asserts.
   */
  it('reports an extra turn when model output arrived before the held tool result', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      stream: [
        { kind: 'assistant', at: 1_100 },
        { kind: 'tool_result', at: 1_400, toolUseId: HELD_ID },
        { kind: 'result', at: 1_500 },
      ],
    });

    expect(artifact.extra_assistant_turn).toBe(true);
  });

  it('reports no extra turn when the held tool result arrived first', () => {
    expect(buildPauseExpiryArtifact(completeObservations()).extra_assistant_turn).toBe(false);
  });

  it('ignores stream entries from before the annotation was delivered', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      stream: [
        // Pre-pause model output must not be read as a turn the annotation caused.
        { kind: 'assistant', at: 10 },
        { kind: 'tool_result', at: 1_100, toolUseId: HELD_ID },
        { kind: 'result', at: 1_300 },
      ],
    });

    expect(artifact.extra_assistant_turn).toBe(false);
  });

  /**
   * The regression for the identity-blind reading.
   *
   * An unrelated tool completes, THEN the model produces a fresh round of output,
   * THEN the held call finally comes back. Reading "was there any tool result
   * before any assistant message" answers yes and reports a clean pass — while the
   * run contains exactly the extra turn the field exists to reject. Matching on the
   * held call's own id is what separates the two.
   */
  it('reports an extra turn hidden behind an unrelated tool result', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      stream: [
        { kind: 'tool_result', at: 1_050, toolUseId: OTHER_ID },
        { kind: 'assistant', at: 1_200 },
        { kind: 'tool_result', at: 1_400, toolUseId: HELD_ID },
        { kind: 'result', at: 1_500 },
      ],
    });

    expect(artifact.extra_assistant_turn).toBe(true);
  });

  it('does not accept an unrelated tool result as the held one', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      // Only an unrelated result, and model output after it. The held call never
      // came back, so there is no admitted-work explanation for that output.
      stream: [
        { kind: 'tool_result', at: 1_050, toolUseId: OTHER_ID },
        { kind: 'assistant', at: 1_200 },
        { kind: 'result', at: 1_300 },
      ],
    });

    expect(artifact.extra_assistant_turn).toBe(true);
  });

  it('leaves the question unobserved when the held call was never identified', () => {
    const { heldToolUseId: _dropped, ...withoutIdentity } = completeObservations();

    const artifact = buildPauseExpiryArtifact(withoutIdentity);

    // The stream still looks clean, which is the point: without the id it cannot be
    // read as clean, so the field must stay unobserved and fail W2-05.
    expect(artifact.extra_assistant_turn).toBeNull();
    expect(artifact.evidence.held_tool_use_id).toBeNull();
  });

  it('treats an id-less tool result as not being the held one', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      stream: [
        { kind: 'tool_result', at: 1_100 },
        { kind: 'assistant', at: 1_200 },
        { kind: 'result', at: 1_300 },
      ],
    });

    // A result the stream did not identify cannot testify that the held call came
    // back, so the assistant output after it has no admitted-work explanation.
    expect(artifact.extra_assistant_turn).toBe(true);
  });

  it('records which results were the held call, so the verdict can be re-derived', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      stream: [
        { kind: 'tool_result', at: 1_050, toolUseId: OTHER_ID },
        { kind: 'tool_result', at: 1_100, toolUseId: HELD_ID },
        { kind: 'tool_result', at: 1_150 },
        { kind: 'assistant', at: 1_200 },
      ],
    });

    expect(artifact.evidence.stream_kinds_after_annotation).toEqual([
      'tool_result:other',
      'tool_result:held',
      'tool_result:unidentified',
      'assistant',
    ]);
    expect(artifact.evidence.held_tool_use_id).toBe(HELD_ID);
  });

  it('leaves the question unobserved when neither the held result nor output arrived', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      stream: [{ kind: 'tool_result', at: 1_100, toolUseId: OTHER_ID }],
    });

    expect(artifact.extra_assistant_turn).toBeNull();
  });

  it('leaves the question unobserved when the stream shows nothing after the annotation', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      stream: [{ kind: 'assistant', at: 10 }],
    });

    expect(artifact.extra_assistant_turn).toBeNull();
  });

  it('leaves the question unobserved when no annotation was delivered to anchor it', () => {
    const artifact = buildPauseExpiryArtifact({ ...completeObservations(), deliveries: [] });

    expect(artifact.extra_assistant_turn).toBeNull();
  });
});

describe('the expiry stays a neutral runtime fact', () => {
  it('fails when a provider-shaped event reached the shared path', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      gateEvents: [
        { type: 'pause_requested' },
        { type: 'pause_confirmed' },
        { type: 'should_query_false' },
        { type: 'pause_released', expired: true },
      ],
    });

    expect(artifact.neutral_annotation).toBe(false);
  });

  it('fails when the expiry note was sent as steering instead of an annotation', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      deliveries: [
        { kind: 'annotation', text: ANNOTATION, result: 'delivered', at: 1_000 },
        { kind: 'steering', text: ANNOTATION, result: 'delivered', at: 1_010 },
      ],
    });

    // Steering would turn the operator's silence into an instruction.
    expect(artifact.neutral_annotation).toBe(false);
  });

  it('keeps the neutral vocabulary list aligned with the coordinator it describes', () => {
    // A new PauseGate event type must be added here deliberately; otherwise the
    // first real run emitting it would be recorded as a neutrality leak.
    expect([...NEUTRAL_GATE_EVENT_TYPES]).toEqual([
      'pause_requested',
      'pause_waiting',
      'pause_confirmed',
      'pause_released',
      'pause_unavailable',
      'active_work',
    ]);
  });
});

describe('a paused run is distinguishable from a stalled one', () => {
  const window = { startedAt: 0, releasedAt: 1_000 };

  const withHeartbeats = (heartbeats: HeartbeatObservation[]): PauseExpiryObservations => ({
    ...completeObservations(),
    pauseWindow: window,
    pod: { podKilled: false, exitWatchdogFired: false, heartbeats },
  });

  it('fails when output went silent for the whole pause', () => {
    const artifact = buildPauseExpiryArtifact(withHeartbeats([]));

    expect(artifact.heartbeats_during_pause).toBe(0);
    expect(artifact.paused_distinguishable_from_stalled).toBe(false);
  });

  it('fails when a tick inside the pause did not say it was paused', () => {
    const artifact = buildPauseExpiryArtifact(
      withHeartbeats([
        { at: 100, paused: true, controlPhase: 'paused', text: '💓 Heartbeat — paused by operator' },
        // The record a reader cannot tell from a stall.
        { at: 500, paused: false, text: '💓 Heartbeat — no SDK messages for 120s' },
      ]),
    );

    expect(artifact.heartbeats_during_pause).toBe(2);
    expect(artifact.paused_distinguishable_from_stalled).toBe(false);
  });

  it('fails a paused tick that carries no control phase', () => {
    const artifact = buildPauseExpiryArtifact(
      withHeartbeats([{ at: 100, paused: true, controlPhase: null, text: '💓 Heartbeat — paused by operator' }]),
    );

    expect(artifact.paused_distinguishable_from_stalled).toBe(false);
  });

  it('counts only ticks inside the pause window', () => {
    const artifact = buildPauseExpiryArtifact(
      withHeartbeats([
        { at: -500, paused: false, text: '💓 Heartbeat — no SDK messages for 60s' },
        { at: 100, paused: true, controlPhase: 'paused', text: '💓 Heartbeat — paused by operator' },
        { at: 5_000, paused: false, text: '💓 Heartbeat — no SDK messages for 60s' },
      ]),
    );

    expect(artifact.heartbeats_during_pause).toBe(1);
    expect(artifact.paused_distinguishable_from_stalled).toBe(true);
    // The out-of-window ticks are kept as context, not silently discarded.
    expect(artifact.evidence.unpaused_heartbeat_baseline).toBe(2);
  });

  it('leaves both fields unobserved without a pause window to bound them', () => {
    const { pauseWindow: _omitted, ...rest } = completeObservations();
    const artifact = buildPauseExpiryArtifact(rest);

    expect(artifact.heartbeats_during_pause).toBeNull();
    expect(artifact.paused_distinguishable_from_stalled).toBeNull();
  });
});

describe('the deadline clamp, read from the real coordinator', () => {
  /**
   * These use the production `PauseGate` so the arithmetic asserted is the
   * shipped arithmetic. `safeBudget` is what the gate itself grants; the evaluator
   * then checks `granted <= remaining - margin`, which is why the artifact records
   * remaining with the margin still in it.
   */
  it('records a granted budget that fits inside the deadline less the reserve', () => {
    const now = 1_000_000;
    const deadline = now + 300_000;
    const gate = new PauseGate({
      now: () => now,
      deadlineAt: () => deadline,
      finalizationMarginMs: DEFAULT_FINALIZATION_MARGIN_MS,
      defaultTimeoutMs: 30 * 60 * 1000,
    });

    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      deadlineClamp: {
        grantedMs: gate.safeBudget(),
        remainingMs: deadline - now,
        finalizationMarginMs: DEFAULT_FINALIZATION_MARGIN_MS,
        nonpositiveRequest: { outcome: 'unavailable', failure: 'no_safe_budget' },
      },
    });
    const clamp = artifact.deadline_clamp as Record<string, number | boolean>;

    expect(clamp.granted_ms).toBe(300_000 - DEFAULT_FINALIZATION_MARGIN_MS);
    // The exact predicate check_w2_05 applies.
    expect(clamp.granted_ms as number).toBeLessThanOrEqual(
      (clamp.remaining_ms as number) - (clamp.finalization_margin_ms as number),
    );
    expect(clamp.nonpositive_budget_rejected).toBe(true);
  });

  it('records the refusal when no safe room remains, rather than a zero-length pause', () => {
    const now = 1_000_000;
    // Deadline inside the reserve: there is no room to hold a pause at all.
    const gate = new PauseGate({
      now: () => now,
      deadlineAt: () => now + 10_000,
      finalizationMarginMs: DEFAULT_FINALIZATION_MARGIN_MS,
    });

    expect(gate.safeBudget()).toBeNull();

    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      deadlineClamp: {
        grantedMs: gate.safeBudget(),
        remainingMs: 10_000,
        finalizationMarginMs: DEFAULT_FINALIZATION_MARGIN_MS,
        nonpositiveRequest: { outcome: 'unavailable', failure: 'no_safe_budget' },
      },
    });
    const clamp = artifact.deadline_clamp as Record<string, unknown>;

    // Granted stays null — a refused pause did not grant anything, and recording
    // 0 would read to the evaluator as a pause that was granted and expired at once.
    expect(clamp.granted_ms).toBeNull();
    expect(clamp.nonpositive_budget_rejected).toBe(true);
  });

  it('reports the rejection as unobserved when the refusal case was never exercised', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      deadlineClamp: { grantedMs: 30_000, remainingMs: 300_000, finalizationMarginMs: DEFAULT_FINALIZATION_MARGIN_MS },
    });

    expect((artifact.deadline_clamp as Record<string, unknown>).nonpositive_budget_rejected).toBeNull();
  });

  it('does not accept a pause that was granted instead of refused', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      deadlineClamp: {
        grantedMs: 30_000,
        remainingMs: 300_000,
        finalizationMarginMs: DEFAULT_FINALIZATION_MARGIN_MS,
        nonpositiveRequest: { outcome: 'confirmed' },
      },
    });

    expect((artifact.deadline_clamp as Record<string, unknown>).nonpositive_budget_rejected).toBe(false);
  });

  it('treats an unknown pod deadline as no authority to pause', () => {
    // controlDeadlineAt returns 0 for an unparseable deadline, which makes every
    // budget unsafe. Recorded here so the producer never reads that 0 as unbounded.
    expect(controlDeadlineAt({} as NodeJS.ProcessEnv)).toBe(0);
    const gate = new PauseGate({ deadlineAt: () => controlDeadlineAt({} as NodeJS.ProcessEnv) });

    expect(gate.safeBudget()).toBeNull();
  });
});

describe('cancellation without admitting the work it cancelled', () => {
  it('records held work denied, unresolved-free, and no resume note', () => {
    const artifact = buildPauseExpiryArtifact(completeObservations());
    const cancel = artifact.cancellation as Record<string, unknown>;

    expect(cancel.held_work_admitted).toBe(false);
    expect(cancel.held_work_denied).toBe(true);
    expect(cancel.annotation_emitted).toBe(false);
  });

  it('records an admitted tool as the side effect the abort was meant to prevent', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      cancellation: { heldWorkDecisions: ['deny', 'admit'], unresolvedHeldWork: 0, deliveriesAfterCancel: [] },
    });
    const cancel = artifact.cancellation as Record<string, unknown>;

    expect(cancel.held_work_admitted).toBe(true);
    expect(cancel.held_work_denied).toBe(false);
  });

  it('refuses to report held work as denied while any of it is unresolved', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      cancellation: { heldWorkDecisions: ['deny'], unresolvedHeldWork: 1, deliveriesAfterCancel: [] },
    });
    const cancel = artifact.cancellation as Record<string, unknown>;

    // Neither admitted nor denied leaves those calls dangling, so the aborting run
    // cannot finish cleanly — a failure, not a pass.
    expect(cancel.held_work_denied).toBe(false);
    expect(cancel.unresolved_held_work).toBe(1);
  });

  it('does not pass vacuously for a run that held no work at all', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      cancellation: { heldWorkDecisions: [], unresolvedHeldWork: 0, deliveriesAfterCancel: [] },
    });
    const cancel = artifact.cancellation as Record<string, unknown>;

    expect(cancel.held_work_admitted).toBeNull();
    expect(cancel.held_work_denied).toBeNull();
  });

  it('records an expiry annotation sent on an aborted run', () => {
    const artifact = buildPauseExpiryArtifact({
      ...completeObservations(),
      cancellation: {
        heldWorkDecisions: ['deny'],
        unresolvedHeldWork: 0,
        // An aborted run is not a resumed one; telling the model to carry on is wrong.
        deliveriesAfterCancel: [{ kind: 'annotation', text: ANNOTATION, result: 'delivered', at: 2_000 }],
      },
    });

    expect((artifact.cancellation as Record<string, unknown>).annotation_emitted).toBe(true);
  });
});

describe('the idle-retry watchdog observation', () => {
  it('passes the measured value through without defaulting it', () => {
    expect(buildPauseExpiryArtifact({ ...completeObservations(), idleRetryFired: true }).idle_retry_fired).toBe(true);
    expect(buildPauseExpiryArtifact({ ...completeObservations(), idleRetryFired: false }).idle_retry_fired).toBe(false);
  });

  it('stays null when the watchdog was never exercised', () => {
    const { idleRetryFired: _omitted, ...rest } = completeObservations();

    expect(buildPauseExpiryArtifact(rest).idle_retry_fired).toBeNull();
  });
});
