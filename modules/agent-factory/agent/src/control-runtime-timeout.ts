/**
 * Turn observations of a real pause expiry into the `pause_expiry` artifact — #5840.
 *
 * ## Why this is a separate module from the experiment that measures
 *
 * Taking the measurements needs a real model, a real CLI subprocess and real
 * spend, so it cannot run in ordinary CI. Deciding *what an observation proves*
 * is ordinary logic — and it is where the dangerous mistakes live: a default that
 * reads as success, a field quietly filled in from a related-but-different
 * observation, a conclusion inferred from the option that was requested rather
 * than from what happened. Those mistakes are only catchable by a test, so they
 * live here, in a file `npx jest` collects and `npx tsc` type-checks, with no
 * import of the SDK anywhere in its graph.
 *
 * `control-runtime-timeout.integration.ts` supplies the observations; this file
 * never measures anything itself.
 *
 * ## The one rule everything here follows
 *
 * A measurement that could not be taken is `null`, never a passing default.
 * `check_w2_05` in `platform/scripts/agent-control-eval.py` requires a specific
 * `true`/`false`/number for every field, so a `null` fails it — which is the
 * intended outcome. "We could not observe it" and "it works" must not produce the
 * same artifact. Nothing in this file writes a hard-coded `true` into a field the
 * evaluator reads.
 *
 * ## What cannot be observed from inside this process
 *
 * One of W2-05's fields is a property of a running pod and nothing else: whether
 * the pod was killed. A process cannot testify that it was not killed, so
 * `pod_killed` is an input ({@link PodObservations}) the evaluation's launcher
 * collects, and its absence leaves the field `null` plus a named entry in
 * `missing_launcher_inputs` saying exactly what to collect. See
 * {@link REQUIRED_LAUNCHER_INPUTS}.
 *
 * The visibility fields — the heartbeat count, the paused-vs-stalled wording, and
 * whether the exit watchdog fired — used to be listed alongside it, because the
 * only copy of that logic lived inside `runAgent`'s run loop. It now lives in
 * `run-heartbeat.ts`, which the worker and the live runner both start, so the
 * runner can emit those records from the production emitter bound to the gate it
 * is actually pausing. They are therefore in-process measurements: a run that
 * collected them satisfies those fields on its own, and a behaviour this runner can
 * exercise is not left as a permanent hand-off.
 *
 * Admissibility then differs *per field*, not per source. A force-exit is a real
 * firing wherever it was seen, so the launcher may corroborate `exit_watchdog_fired`.
 * The heartbeat records it may not supply at all — see
 * {@link LauncherPodObservations} — because a pod-log line cannot be attributed to
 * the paused execution rather than to the ordinary worker beside it.
 *
 * What does NOT change is the honesty rule. A run configured with a pause shorter
 * than the emitter's silence threshold produces no heartbeat record, and the
 * artifact then reports a true zero that `check_w2_05` rejects — correctly, since
 * such a run has no visibility evidence to offer.
 */

/**
 * The neutral pause-coordinator event vocabulary, as published by `PauseGate`.
 *
 * Listed here so the artifact can record that the shared path stayed neutral —
 * i.e. that the expiry arrived as a runtime fact and only the adapter translated
 * it into provider vocabulary. An event type outside this set in the observed
 * stream means a provider-shaped event reached the shared path, which is the leak
 * the neutral contract forbids.
 */
export const NEUTRAL_GATE_EVENT_TYPES = [
  'pause_requested',
  'pause_waiting',
  'pause_confirmed',
  'pause_released',
  'pause_unavailable',
  'active_work',
] as const;

/**
 * Every field the artifact can only report from an input, with how to collect it.
 *
 * `buildPauseExpiryArtifact` lists an entry in `missing_launcher_inputs` only when
 * the corresponding observation is genuinely absent, so an entry here is a
 * statement about what to collect *if* it is missing — not a permanent hand-off.
 * The visibility entries are normally satisfied in-process by the live runner's own
 * production emitter (see the module header); `pod_killed` is the one field no
 * in-process measurement can ever supply.
 */
export const REQUIRED_LAUNCHER_INPUTS: ReadonlyArray<{ field: string; collect: string }> = [
  {
    field: 'pod_killed',
    collect:
      'Whether the fixture pod survived the pause. Collect from the pod itself, not from the ' +
      'worker: `kubectl get pod <fixture-pod> -o jsonpath={.status.containerStatuses[0].restartCount}` ' +
      'before the pause and again after the auto-resume (equal => not killed), plus ' +
      '`kubectl get events --field-selector involvedObject.name=<fixture-pod>` showing no Killing/' +
      'OOMKilled/Evicted event inside the pause window. A worker that logged its own survival ' +
      'cannot testify that it was not killed.',
  },
  {
    field: 'exit_watchdog_fired',
    collect:
      'Measured in-process by the live runner, which starts the production emitter ' +
      '(`run-heartbeat.ts`, the same module `agent-worker.ts` runs) against the gate it pauses — ' +
      'so this is normally already populated. It is listed only because the launcher may still ' +
      'corroborate it from the pod log stream: firing is logged as ' +
      '`{"phase":"post-completion-timeout"}` with a "Force exit — stream did not close" message, ' +
      'and its absence inside the pause window is the expected observation. A firing observed ' +
      'from EITHER source fails the field.',
  },
  {
    field: 'heartbeats_during_pause / paused_distinguishable_from_stalled',
    collect:
      'Measured in-process by the live runner through the same production emitter, bound to the ' +
      'gate whose pause is being measured — which is what makes the records evidence about that ' +
      'pause rather than about a parent worker that happened to be running alongside it. The run ' +
      'must configure a pause budget at or above the emitter\'s observable floor (pass ' +
      '`--heartbeat`), because a shorter pause produces no record at all and the count would be a ' +
      'true zero. This field is NOT collectable from the launcher, and the assembly has no ' +
      'parameter to submit it through: a scraped pod-log heartbeat line cannot be attributed to ' +
      'this execution rather than to the ordinary worker in the same pod, whose records are ' +
      'identical in shape and describe a different gate. If it is missing, re-run the experiment ' +
      'with a budget long enough to produce records — never source it from the pod log. For ' +
      'reading a log by eye, a paused tick carries ' +
      '`{"phase":"heartbeat","controlPhase":"pause_requested"|"paused","heldTools":N,' +
      '"activeTools":N}` with the message "💓 Heartbeat — paused by operator, ...".',
  },
];

/** One observed pause-coordinator event, in order. */
export interface GateEventObservation {
  /** The event's `type`, verbatim. Anything outside {@link NEUTRAL_GATE_EVENT_TYPES} is a leak. */
  readonly type: string;
  /** `pause_released.expired`: the budget ran out, rather than someone resuming. */
  readonly expired?: boolean;
  /** `pause_unavailable.failure`, e.g. `no_safe_budget`. */
  readonly failure?: string;
}

/** One input the adapter handed to the model's transport. */
export interface InputDeliveryObservation {
  /** Neutral input kind. `annotation` records a fact; `steering` may request work. */
  readonly kind: 'annotation' | 'steering';
  readonly text: string;
  /** What the transport reported. Only `delivered` counts as delivered. */
  readonly result: string;
  /** Wall-clock ms, so deliveries can be ordered against the cancellation. */
  readonly at: number;
}

/**
 * One entry of the real model output stream, reduced to what the turn question needs.
 *
 * `assistant` is model output; `tool_result` is the result of a tool call coming
 * back; `result` is the run's terminal message. Recorded from the live stream, in
 * arrival order — not from the options the query was given.
 */
export interface StreamEntryObservation {
  readonly kind: 'assistant' | 'tool_result' | 'result';
  readonly at: number;
  /**
   * For a `tool_result`, the `tool_use_id` it answers. `undefined` = the stream
   * carried no id.
   *
   * Load-bearing rather than context. The turn question is decided by what arrives
   * before the **held** tool's result, so a result of unknown identity cannot stand
   * in for it: an unrelated tool completing first, then a fresh round of model
   * output, then the held tool, is precisely the failure the field exists to catch,
   * and an identity-blind reading calls it a pass.
   */
  readonly toolUseId?: string;
}

/** A worker heartbeat record, as it was logged. */
export interface HeartbeatObservation {
  readonly at: number;
  /** The emitter's own paused branch was taken for this tick. */
  readonly paused: boolean;
  /** `controlPhase` from the record. Present only on a paused tick. */
  readonly controlPhase?: string | null;
  /** The logged message, so the paused-vs-stalled wording is read, not assumed. */
  readonly text: string;
}

/**
 * Facts about the executing process, from whichever source can see them.
 *
 * Two sources now populate this group. `heartbeats` and `exitWatchdogFired` come
 * from the production emitter the live runner starts against the gate it pauses —
 * in-process measurements of the execution under test. `podKilled` can only come
 * from the launcher, watching the pod from outside. Absent from both => `null` plus
 * a named gap.
 */
export interface PodObservations {
  readonly podKilled?: boolean;
  readonly exitWatchdogFired?: boolean;
  readonly heartbeats?: ReadonlyArray<HeartbeatObservation>;
}

/**
 * What the launcher is permitted to contribute to {@link PodObservations}.
 *
 * Deliberately narrower than `PodObservations`, and the omission is the point: there
 * is **no `heartbeats` field**, so a launcher's records cannot enter the artifact by
 * any path, including a future edit to the merge. `Omit` would leave the door findable;
 * a type with no such member makes the substitution a compile error at the call site.
 *
 * ## Why these two fields are admissible and the records are not
 *
 * Provenance differs per field, not per source. `podKilled` is the launcher's alone —
 * a process that logged its own survival cannot testify that it was not killed. A
 * force-exit seen in the pod log is a real firing wherever it was seen, so it is
 * admissible as a failure report. But a heartbeat line proves only that *something*
 * in that pod emitted it: the pod also runs the ordinary worker, whose records look
 * identical and describe a different execution with a different gate. Crediting one
 * to this run is the exact substitution that made the visibility fields unacceptable,
 * and it is worse than a wrong count — it also removes the named gap that would have
 * told the launcher to collect the real observation.
 */
export interface LauncherPodObservations {
  readonly podKilled?: boolean;
  readonly exitWatchdogFired?: boolean;
}

/**
 * Reconcile the run's own pod observations with the launcher's.
 *
 * Field-wise, not whole-object. The two sources populate different fields of one
 * group, so `{...a, ...b}` at the group level would silently drop whichever side was
 * assembled second — a launcher supplying only `podKilled` would erase the heartbeat
 * records the run actually measured, and the artifact would then report an unobserved
 * visibility field for a run that observed it.
 *
 * ## Conflicts resolve toward the failure, never toward the later writer
 *
 * The two boolean fields are failure reports, not opinions to be averaged, and
 * "last one wins" is the wrong rule for them: a spread would let a launcher whose
 * log scrape saw nothing overwrite a firing the in-process emitter actually
 * recorded, which is a real failure silently downgraded to a pass. So a `true` from
 * **either** source wins, matching what `REQUIRED_LAUNCHER_INPUTS` promises. Both
 * reporting `false` is the only way a field becomes `false`; unobserved on both
 * sides stays unobserved.
 *
 * ## Heartbeat records are the run's own, or absent
 *
 * They pass through from the in-process side untouched, and the launcher has nowhere
 * to offer any (see {@link LauncherPodObservations}). A run that measured none is
 * reported as not having measured, which is a named gap; a run that measured an empty
 * set is reported as zero, which is a measurement. Neither is ever filled from
 * another execution's records — that distinction is the whole provenance contract,
 * and this is the function that either keeps or breaks it.
 *
 * Lives here rather than in the live runner so ordinary CI covers it: the runner's
 * import graph reaches the SDK, and this merge decides whether real evidence
 * survives into the artifact.
 */
export function mergePodObservations(
  inProcess: PodObservations | undefined,
  launcher: LauncherPodObservations | undefined,
): PodObservations | undefined {
  if (inProcess === undefined && launcher === undefined) return undefined;

  const podKilled = worstOf(inProcess?.podKilled, launcher?.podKilled);
  const exitWatchdogFired = worstOf(inProcess?.exitWatchdogFired, launcher?.exitWatchdogFired);

  return {
    // Spread conditionally so an unobserved field stays *absent* rather than
    // becoming an explicit `undefined`, which `missing_launcher_inputs` reads as
    // the same thing but a reader of the JSON would not.
    ...(podKilled === undefined ? {} : { podKilled }),
    ...(exitWatchdogFired === undefined ? {} : { exitWatchdogFired }),
    // Same-execution records only. Never `?? launcher.heartbeats` — there is no such
    // field to fall back to, by construction.
    ...(inProcess?.heartbeats === undefined ? {} : { heartbeats: inProcess.heartbeats }),
  };
}

/** A reported failure outranks a reported pass, whichever side reported it. */
function worstOf(a: boolean | undefined, b: boolean | undefined): boolean | undefined {
  if (a === undefined) return b;
  if (b === undefined) return a;
  return a || b;
}

/**
 * Combine the pod views of several **in-process experiments**.
 *
 * A different question from {@link mergePodObservations}, which is why it is a
 * different function rather than the same one applied twice. Here every side is one
 * of this process's own executions, so records may come from whichever experiment
 * measured them: experiment A measures the heartbeats while experiment E measures the
 * watchdog verdict, and a part carrying only a verdict must not erase the other's
 * records — the hazard a group-level `{...a, ...e}` spread creates in either order.
 *
 * Booleans resolve toward the failure, as everywhere else. `heartbeats` are still
 * never concatenated — the first experiment that measured any wins, because two
 * experiments are two executions with two gates, and appending one to the other would
 * report a faulted run's ticks inside an unfaulted run's count. The scenario that
 * faults its completion clock deliberately contributes no heartbeats for this reason.
 *
 * Applying the launcher's provenance restriction here would be the opposite mistake:
 * it would discard A's genuine records whenever E happened to be folded in first.
 */
export function mergeExperimentPodObservations(
  parts: ReadonlyArray<PodObservations | undefined>,
): PodObservations | undefined {
  return parts.reduce<PodObservations | undefined>((acc, part) => {
    if (acc === undefined) return part;
    if (part === undefined) return acc;
    // Both sides are this process's own experiments, so either may be the one that
    // measured the records. `mergePodObservations` keeps only its first argument's,
    // which is correct against a launcher and wrong between two experiments. Note the
    // explicit `undefined` check: an empty array is a measured zero and must win over
    // an absent one, which a truthiness test would get right only by accident.
    const heartbeats = acc.heartbeats !== undefined ? acc.heartbeats : part.heartbeats;
    return {
      ...mergePodObservations(acc, part),
      ...(heartbeats === undefined ? {} : { heartbeats }),
    };
  }, undefined);
}

/**
 * What a watchdog observer has accumulated across a run's ticks.
 *
 * Kept as a named type so {@link recordWatchdogTick} is a pure function over it and
 * ordinary CI can cover the accumulation rule, which the live runner cannot.
 */
export interface WatchdogTickObservations {
  /** Fired on a tick where the bound was due **and the pause was in force**. */
  readonly firedDuringPause: boolean | null;
  /** Ticks on which the post-completion bound had elapsed at all. */
  readonly dueTicks: number;
  /** Of those, how many were taken while the gate reported an active pause. */
  readonly dueWhilePaused: number;
  /** Fired on a due tick taken after the pause was released. */
  readonly firedAfterRelease: boolean | null;
}

/** A due tick's two facts, as the production emitter reported them. */
export interface WatchdogTickInput {
  readonly watchdogDue: boolean;
  readonly paused: boolean;
  readonly forceExit: boolean;
}

/** A run that has observed no watchdog tick yet: everything unobserved. */
export const EMPTY_WATCHDOG_OBSERVATIONS: WatchdogTickObservations = {
  firedDuringPause: null,
  dueTicks: 0,
  dueWhilePaused: 0,
  firedAfterRelease: null,
};

/**
 * Fold one heartbeat tick into the watchdog observations.
 *
 * ## Why the during-pause verdict is attributed per tick, not per run
 *
 * The scenario that makes `exit_watchdog_fired` mean anything deliberately produces
 * a firing: the pause holds the due watchdog back, and then, once released, the same
 * due condition *must* force an exit — otherwise the guard removed a bound instead of
 * deferring it. So a run whose evidence is complete contains **both** a due-and-
 * suppressed tick and a due-and-fired tick, and the two must not be conflated.
 *
 * Attribution is therefore taken from the tick's own `paused` flag, which is the
 * emitter's verdict for that same tick. Recording a firing without asking whether the
 * pause was in force lets the required after-release firing overwrite the
 * during-pause `false`, which makes the scenario fail exactly when it succeeds — and,
 * worse, reports a paused run as having been killed by the watchdog when it was not.
 *
 * ## Why a firing is sticky but a pass is not
 *
 * `firedDuringPause` latches on `true` and never returns to `false`: across many due
 * ticks, one firing while paused is the failure, and a later quiet tick does not undo
 * it. A `false` is only ever written as the *first* observation, so it cannot
 * overwrite a firing recorded earlier. This is the same rule as
 * {@link mergePodObservations} — conflicts resolve toward the failure — applied
 * across time rather than across sources.
 */
export function recordWatchdogTick(
  prev: WatchdogTickObservations,
  tick: WatchdogTickInput,
): WatchdogTickObservations {
  // A tick whose bound had not elapsed had no decision to make. Recording its `false`
  // would credit the pause for suppressing a watchdog that was never going to fire —
  // an observation that survives deleting the production guard.
  if (!tick.watchdogDue) return prev;

  if (tick.paused) {
    return {
      ...prev,
      dueTicks: prev.dueTicks + 1,
      dueWhilePaused: prev.dueWhilePaused + 1,
      // Sticky toward the failure; a pass only ever fills an unobserved field.
      firedDuringPause: prev.firedDuringPause === true || tick.forceExit,
    };
  }

  return {
    ...prev,
    dueTicks: prev.dueTicks + 1,
    firedAfterRelease: prev.firedAfterRelease === true || tick.forceExit,
  };
}

/** The real coordinator's budget arithmetic, read from its own calculation. */
export interface DeadlineClampObservations {
  /** `PauseGate.safeBudget()` — the pause duration actually granted. */
  readonly grantedMs: number | null;
  /** `deadlineAt - now`: time left before the run's deadline, margin not yet removed. */
  readonly remainingMs: number | null;
  /** The reserve kept back so an auto-resume can still write a terminal state. */
  readonly finalizationMarginMs: number | null;
  /** Outcome of requesting a pause with no room left. */
  readonly nonpositiveRequest?: { readonly outcome: string; readonly failure?: string };
}

/**
 * The real held-hook-timeout measurement, as its experiment observed it.
 *
 * Structured rather than a pre-built block, because this is the field where a
 * hand-assembled `{exercised: true}` would be least visible and most damaging: it
 * is the one bound the adapter does not enforce itself. Two independent
 * observations are needed and neither is sufficient alone — the CLI really
 * abandoned the hook (`signalAborted`, without the probe's own safety release),
 * *and* the gate then reported the pause as lapsed with a cause. A run that only
 * measured the first is an abort with no reported outcome; only the second is a
 * reported outcome for an abort that may never have happened.
 */
export interface HeldHookTimeoutObservations {
  /** The CLI's abandonment signal for the parked hook. `true` = the bound fired. */
  readonly signalAborted: boolean | null;
  /** The probe released the pause itself because no timeout arrived in time. */
  readonly safetyReleaseUsed: boolean;
  /** Gate phase once the hook had been abandoned. `paused` is the failure. */
  readonly phaseAfterTimeout: string | null;
  /** The coordinator's own reason for the lapsed pause, verbatim. */
  readonly unavailableReason: string | null;
  /** The production hook bound, in seconds, as the adapter derives it. */
  readonly hookTimeoutSeconds: number | null;
  /** The production pause budget, in seconds, the bound must exceed. */
  readonly pauseBudgetSeconds: number | null;
  /** The shortened bound the probe forced, recorded so the fault is visible. */
  readonly forcedTimeoutSeconds?: number | null;
}

/** What happened to work held at the barrier when the run was cancelled. */
export interface CancellationObservations {
  /** One entry per held admission, as the barrier released it. */
  readonly heldWorkDecisions: ReadonlyArray<'admit' | 'deny'>;
  /** Held admissions still unresolved when the run finished. */
  readonly unresolvedHeldWork: number;
  /** Inputs the adapter delivered at or after the cancellation. */
  readonly deliveriesAfterCancel: ReadonlyArray<InputDeliveryObservation>;
}

/** Everything the live experiments measured, assembled into one artifact. */
export interface PauseExpiryObservations {
  /** The exact annotation text production sends on expiry, for identification. */
  readonly expectedAnnotationText: string;
  /** Coordinator events in published order. `undefined` => nothing was observed. */
  readonly gateEvents?: ReadonlyArray<GateEventObservation>;
  /** Explicit `resume()` calls. A natural expiry must have none. */
  readonly explicitResumeCalls?: number;
  /** Inputs the adapter delivered during the run. */
  readonly deliveries?: ReadonlyArray<InputDeliveryObservation>;
  /** The live output stream, in arrival order. */
  readonly stream?: ReadonlyArray<StreamEntryObservation>;
  /**
   * `tool_use_id` of the call the barrier was holding when the pause expired.
   *
   * Captured at the barrier, which is the only place the run knows which specific
   * call it parked. `undefined` leaves {@link PauseExpiryArtifact.extra_assistant_turn}
   * unobserved: without it there is no way to tell the held tool's result from any
   * other tool's, and guessing from the nearest result is how an extra turn hides.
   */
  readonly heldToolUseId?: string;
  /** When the pause was requested and when it was released, in wall-clock ms. */
  readonly pauseWindow?: { readonly startedAt: number; readonly releasedAt: number };
  /** The real idle-retry watchdog abandoned the attempt during the pause. */
  readonly idleRetryFired?: boolean;
  /** Large tool output survived the pause, measured by the spill experiment. */
  readonly spillOutputPreserved?: boolean;
  readonly pod?: PodObservations;
  readonly deadlineClamp?: DeadlineClampObservations;
  readonly cancellation?: CancellationObservations;
  /** The existing real held-hook-timeout measurement, from its own experiment. */
  readonly heldHookTimeout?: HeldHookTimeoutObservations;
}

/** A `pause_expiry` artifact. Nullable everywhere a measurement can be missing. */
export interface PauseExpiryArtifact {
  readonly auto_resumed: boolean | null;
  readonly annotation_count: number | null;
  readonly extra_assistant_turn: boolean | null;
  readonly neutral_annotation: boolean | null;
  readonly resolved_before_release: boolean | null;
  readonly pod_killed: boolean | null;
  readonly idle_retry_fired: boolean | null;
  readonly exit_watchdog_fired: boolean | null;
  readonly heartbeats_during_pause: number | null;
  readonly paused_distinguishable_from_stalled: boolean | null;
  readonly spill_output_preserved: boolean | null;
  readonly held_hook_timeout: Record<string, unknown> | null;
  readonly deadline_clamp: Record<string, unknown> | null;
  readonly cancellation: Record<string, unknown> | null;
  /** Fields left `null` because the launcher must collect them, with how. */
  readonly missing_launcher_inputs: ReadonlyArray<{ field: string; collect: string }>;
  /** Supporting context. Never read by the evaluator; read by a human reviewer. */
  readonly evidence: Record<string, unknown>;
}

/**
 * Did the run continue by itself?
 *
 * Both halves are required, and the second is the one that matters: a scenario
 * that reached the same end state by calling `resume()` would satisfy "released"
 * while proving nothing about the expiry timer. So an explicit resume anywhere in
 * the run disqualifies the claim rather than being ignored.
 */
function deriveAutoResumed(obs: PauseExpiryObservations): boolean | null {
  if (obs.gateEvents === undefined || obs.explicitResumeCalls === undefined) return null;
  const released = obs.gateEvents.filter((event) => event.type === 'pause_released');
  if (released.length === 0) return false;
  return released.some((event) => event.expired === true) && obs.explicitResumeCalls === 0;
}

/**
 * Was the pause's outcome reported before it ended?
 *
 * The defect this guards is a pause that shows "pausing…" for its whole budget
 * and then resumes silently: the operator is never told whether the pause took.
 * A confirmation (`pause_confirmed`) or a failure (`pause_unavailable`) must
 * appear strictly before the release, in the observed order.
 */
function deriveResolvedBeforeRelease(obs: PauseExpiryObservations): boolean | null {
  if (obs.gateEvents === undefined) return null;
  const releaseIndex = obs.gateEvents.findIndex((event) => event.type === 'pause_released');
  if (releaseIndex < 0) return null;
  return obs.gateEvents
    .slice(0, releaseIndex)
    .some((event) => event.type === 'pause_confirmed' || event.type === 'pause_unavailable');
}

/** Deliveries of the production expiry annotation that the transport accepted. */
function expiryAnnotations(obs: PauseExpiryObservations): ReadonlyArray<InputDeliveryObservation> | null {
  if (obs.deliveries === undefined) return null;
  return obs.deliveries.filter(
    (input) => input.kind === 'annotation' && input.text === obs.expectedAnnotationText && input.result === 'delivered',
  );
}

/**
 * Did the annotation provoke a fresh round of model output?
 *
 * Measured from the live stream, deliberately NOT from the transport flag that
 * carries the annotation/steering distinction. Reading that flag would prove what
 * the adapter *asked for*; the claim is about what the provider then did.
 *
 * The ordering is the measurement, and it is ordering against **one specific**
 * tool result: the one answering the call the barrier was holding. On expiry that
 * call is admitted, so its result is what should arrive next, and only after it
 * more model output. Model output arriving first means the annotation itself
 * started a turn.
 *
 * The identity matters because an identity-blind reading is wrong in a way that
 * always errs toward a pass. A run where some unrelated tool result lands, then
 * the model produces a fresh round of output, then the held tool finally
 * completes, reads as "a tool result came before any assistant message" — which
 * is exactly the extra turn the field exists to reject. So a result of unknown
 * or different identity is not treated as the held one, and an unidentifiable
 * held result leaves the field unobserved rather than guessed.
 */
function deriveExtraAssistantTurn(obs: PauseExpiryObservations): boolean | null {
  const annotations = expiryAnnotations(obs);
  if (annotations === null || annotations.length === 0 || obs.stream === undefined) return null;
  // Without the held call's identity nothing in the stream can be recognised as
  // its result, so the question cannot be answered — not answered optimistically.
  if (obs.heldToolUseId === undefined) return null;
  const deliveredAt = annotations[0].at;
  const after = obs.stream.filter((entry) => entry.at >= deliveredAt);
  const heldResult = after.findIndex(
    (entry) => entry.kind === 'tool_result' && entry.toolUseId === obs.heldToolUseId,
  );
  const firstAssistant = after.findIndex((entry) => entry.kind === 'assistant');
  // The held result never arrived. Assistant output after the note and no held
  // result to justify it is an extra turn; neither observed is simply unobserved.
  if (heldResult < 0) return firstAssistant < 0 ? null : true;
  if (firstAssistant < 0) return false;
  return firstAssistant < heldResult;
}

/**
 * Was the expiry published as a neutral runtime fact?
 *
 * Three observations, all from the shared path rather than from the adapter's
 * intent: every coordinator event stayed inside the neutral vocabulary, the
 * release that triggered the annotation was the neutral `pause_released`+`expired`
 * pair, and what reached the transport was a neutral `annotation` input — not
 * steering, which would inject an instruction into a run that never asked for one.
 */
function deriveNeutralAnnotation(obs: PauseExpiryObservations): boolean | null {
  const annotations = expiryAnnotations(obs);
  if (annotations === null || obs.gateEvents === undefined) return null;
  if (annotations.length === 0) return false;
  const neutral = new Set<string>(NEUTRAL_GATE_EVENT_TYPES);
  const vocabularyHeld = obs.gateEvents.every((event) => neutral.has(event.type));
  const neutralExpiry = obs.gateEvents.some((event) => event.type === 'pause_released' && event.expired === true);
  const steered = (obs.deliveries ?? []).some(
    (input) => input.kind === 'steering' && input.text === obs.expectedAnnotationText,
  );
  return vocabularyHeld && neutralExpiry && !steered;
}

/** Heartbeat records that fall inside the observed pause window. */
function heartbeatsInWindow(obs: PauseExpiryObservations): ReadonlyArray<HeartbeatObservation> | null {
  const records = obs.pod?.heartbeats;
  if (records === undefined || obs.pauseWindow === undefined) return null;
  const { startedAt, releasedAt } = obs.pauseWindow;
  return records.filter((record) => record.at >= startedAt && record.at <= releasedAt);
}

/**
 * Could a reader tell the paused run from a dead one?
 *
 * A dead run emits nothing, so the claim needs two things from the same records:
 * output continued through the pause, and it said *why* it was quiet. Every tick
 * inside the window must have taken the emitter's paused branch, carry the
 * control phase, and say so in its message — an unmarked tick inside a pause is
 * exactly the record that reads as a stall.
 */
function derivePausedDistinguishable(obs: PauseExpiryObservations): boolean | null {
  const inWindow = heartbeatsInWindow(obs);
  if (inWindow === null) return null;
  if (inWindow.length === 0) return false;
  return inWindow.every(
    (record) =>
      record.paused === true &&
      typeof record.controlPhase === 'string' &&
      record.controlPhase.length > 0 &&
      /pause/i.test(record.text),
  );
}

/**
 * The budget arithmetic W2-05 checks, recorded from the coordinator's own numbers.
 *
 * `remaining_ms` is the whole distance to the deadline with the margin still in
 * it, because the evaluator's predicate is `granted <= remaining - margin`.
 * Recording an already-reduced remaining would make that comparison pass on
 * different arithmetic than the one being claimed.
 */
function deriveDeadlineClamp(obs: PauseExpiryObservations): Record<string, unknown> | null {
  const clamp = obs.deadlineClamp;
  if (clamp === undefined) return null;
  const refusal = clamp.nonpositiveRequest;
  return {
    granted_ms: clamp.grantedMs,
    remaining_ms: clamp.remainingMs,
    finalization_margin_ms: clamp.finalizationMarginMs,
    nonpositive_budget_rejected:
      refusal === undefined ? null : refusal.outcome === 'unavailable' && refusal.failure === 'no_safe_budget',
    observed_by: 'PauseGate.safeBudget with the production deadline source',
  };
}

/**
 * What cancellation did with the work it was holding.
 *
 * `held_work_denied` requires that held work existed and that *all* of it was
 * refused. An empty decision list is `null`, not `true`: a run that held nothing
 * would otherwise satisfy the field vacuously, and the whole point is that an
 * abort must not flush the side effects the operator aborted to prevent. Work
 * left neither admitted nor denied is a failure — those tool calls are unresolved
 * and the aborting run cannot finish cleanly.
 */
function deriveCancellation(obs: PauseExpiryObservations): Record<string, unknown> | null {
  const cancel = obs.cancellation;
  if (cancel === undefined) return null;
  const decisions = cancel.heldWorkDecisions;
  const observed = decisions.length > 0;
  return {
    held_work_admitted: observed ? decisions.includes('admit') : null,
    held_work_denied: observed ? decisions.every((decision) => decision === 'deny') && cancel.unresolvedHeldWork === 0 : null,
    annotation_emitted: cancel.deliveriesAfterCancel.some(
      (input) => input.text === obs.expectedAnnotationText && input.result === 'delivered',
    ),
    held_work_count: decisions.length,
    unresolved_held_work: cancel.unresolvedHeldWork,
    observed_by: 'fixture',
  };
}

/**
 * The held-hook-timeout block W2-05 checks, derived from its experiment's facts.
 *
 * `exercised` is the conjunction described on {@link HeldHookTimeoutObservations}:
 * the CLI abandoned the hook on its own, and the gate reported the consequence.
 * It is deliberately not an input — a field the producer could simply assert would
 * turn the one unenforced bound in the pause design into a self-certification.
 *
 * `state` and `reason` are passed through as observed, including a `paused` state
 * or an empty reason, both of which the evaluator rejects. Repairing them here
 * would hide the exact defect the check exists to find: containment reported after
 * it had already lapsed.
 */
function deriveHeldHookTimeout(obs: PauseExpiryObservations): Record<string, unknown> | null {
  const hook = obs.heldHookTimeout;
  if (hook === undefined) return null;
  const exercised =
    hook.signalAborted === null
      ? null
      : hook.signalAborted === true && !hook.safetyReleaseUsed && hook.unavailableReason !== null;
  return {
    exercised,
    state: hook.phaseAfterTimeout,
    reason: hook.unavailableReason,
    hook_timeout_seconds: hook.hookTimeoutSeconds,
    pause_budget_seconds: hook.pauseBudgetSeconds,
    signal_aborted: hook.signalAborted,
    safety_release_used: hook.safetyReleaseUsed,
    ...(hook.forcedTimeoutSeconds === undefined ? {} : { forced_timeout_seconds: hook.forcedTimeoutSeconds }),
    observed_by: 'real CLI hook abandonment plus the coordinator’s reported outcome',
  };
}

/**
 * Build the `pause_expiry` artifact from observations.
 *
 * Pure: every field traces to something in `obs`, and anything absent from `obs`
 * is `null` here. There is no path through this function that turns a missing
 * observation into a value `check_w2_05` accepts.
 */
export function buildPauseExpiryArtifact(obs: PauseExpiryObservations): PauseExpiryArtifact {
  const annotations = expiryAnnotations(obs);
  const inWindow = heartbeatsInWindow(obs);
  // An entry appears only while its observation is actually absent. The two
  // visibility entries are normally supplied in-process by the live runner's
  // production heartbeat emitter, so a run that collected them lists only
  // `pod_killed` — the field no process can report about itself. Note this asks
  // whether the observation was *taken*, not whether it was favourable: an empty
  // heartbeat array is a real measurement of zero and is reported as such rather
  // than downgraded to "not collected".
  const missing = REQUIRED_LAUNCHER_INPUTS.filter(({ field }) => {
    if (field === 'pod_killed') return obs.pod?.podKilled === undefined;
    if (field === 'exit_watchdog_fired') return obs.pod?.exitWatchdogFired === undefined;
    return obs.pod?.heartbeats === undefined || obs.pauseWindow === undefined;
  });

  return {
    auto_resumed: deriveAutoResumed(obs),
    annotation_count: annotations === null ? null : annotations.length,
    extra_assistant_turn: deriveExtraAssistantTurn(obs),
    neutral_annotation: deriveNeutralAnnotation(obs),
    resolved_before_release: deriveResolvedBeforeRelease(obs),
    pod_killed: obs.pod?.podKilled ?? null,
    idle_retry_fired: obs.idleRetryFired ?? null,
    exit_watchdog_fired: obs.pod?.exitWatchdogFired ?? null,
    heartbeats_during_pause: inWindow === null ? null : inWindow.length,
    paused_distinguishable_from_stalled: derivePausedDistinguishable(obs),
    spill_output_preserved: obs.spillOutputPreserved ?? null,
    held_hook_timeout: deriveHeldHookTimeout(obs),
    deadline_clamp: deriveDeadlineClamp(obs),
    cancellation: deriveCancellation(obs),
    missing_launcher_inputs: missing,
    evidence: {
      gate_event_types: obs.gateEvents?.map((event) => event.type) ?? null,
      explicit_resume_calls: obs.explicitResumeCalls ?? null,
      delivered_input_kinds: obs.deliveries?.map((input) => `${input.kind}:${input.result}`) ?? null,
      // Each entry carries whether it is the held call's result, so a reviewer can
      // re-derive the turn verdict from the artifact instead of trusting it.
      stream_kinds_after_annotation:
        annotations && annotations.length > 0 && obs.stream
          ? obs.stream
              .filter((entry) => entry.at >= annotations[0].at)
              .map((entry) =>
                entry.kind === 'tool_result'
                  ? `tool_result:${entry.toolUseId === undefined ? 'unidentified' : entry.toolUseId === obs.heldToolUseId ? 'held' : 'other'}`
                  : entry.kind,
              )
          : null,
      held_tool_use_id: obs.heldToolUseId ?? null,
      pause_window_ms: obs.pauseWindow ? obs.pauseWindow.releasedAt - obs.pauseWindow.startedAt : null,
      unpaused_heartbeat_baseline:
        obs.pod?.heartbeats === undefined ? null : obs.pod.heartbeats.filter((record) => !record.paused).length,
      extra_assistant_turn_observed_from: 'live output stream ordering, not the transport shouldQuery flag',
    },
  };
}
