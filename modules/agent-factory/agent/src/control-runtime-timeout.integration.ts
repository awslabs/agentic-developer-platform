/**
 * Live pause-expiry experiments against the real Claude Agent SDK — Issue #5840.
 *
 * ## What this file adds, and why it is a file rather than a test
 *
 * `control-runtime.integration.ts` already measures two real properties of the
 * pause barrier: a held tool cut off by its own hook bound, and large tool output
 * preserved across a pause. What W2-05 also asserts, and nothing measured, is what
 * happens when a pause is left alone until its **budget runs out**: that the run
 * continues by itself, that the model is told once and only once, that nothing
 * mistook the deliberate quiet for a stall, that the budget was trimmed to fit the
 * run's deadline, and that cancelling refuses the work it was holding.
 *
 * Those are claims about a provider and a real subprocess, so a mock cannot
 * testify about them — it can only replay the behaviour assumed while writing it,
 * which is the assumption under test. So this file drives the production
 * `resilientQuery` against the real CLI, through the production adapter and gate,
 * and watches the filesystem: a file existing or not existing is not a matter of
 * interpretation.
 *
 * Like its sibling it is deliberately **not** named `.test.ts`. Jest's `testMatch`
 * collects only that suffix, so this cannot run in unit CI and cannot make a PR
 * check depend on model availability, network egress or spend. It is run
 * explicitly, by the evaluation's protected launcher:
 *
 * ```
 * npx ts-node src/control-runtime-timeout.integration.ts
 * npx ts-node src/control-runtime-timeout.integration.ts --json out.json
 * ```
 *
 * ## The measurement rule
 *
 * Observations go to {@link buildPauseExpiryArtifact}, which ordinary CI covers and
 * which writes `null` for anything unobserved. Nothing here fills a field with a
 * hopeful default: a probe that could not run is recorded as a failure with its
 * cause, and a property this process cannot see is reported missing alongside the
 * exact input the launcher must collect instead. The artifact contract, field by
 * field, is `docs/runbooks/pause-expiry-evidence.md`.
 *
 * Nothing here decides whether the story is accepted. Live acceptance belongs to
 * evaluation #3968, reading this artifact alongside the deployed build.
 */
import { mkdtempSync, existsSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import {
  ClaudeBackgroundWorkObserver,
  ClaudeControlAdapter,
  CLAUDE_SDK_VERSION,
  PAUSE_EXPIRY_ANNOTATION,
  type ClaudePauseHooks,
} from './harnesses/claude-control';
import { PauseGate, DEFAULT_FINALIZATION_MARGIN_MS, type PauseGateEvent } from './pause-gate';
import { createWorkerToolHooks } from './developer-checkpoints';
import { TmpSpillStore } from './utils/spill';
import { controlDeadlineAt } from './control-deadline';
import { IMPLEMENTED_CONTROL_VERBS, type CurrentAttemptRegistry } from './control-runtime';
// The production heartbeat/exit-watchdog emitter — the same module `agent-worker.ts`
// runs. Started here against the gate this experiment pauses, so its records are
// evidence about *this* execution. See HEARTBEAT_OBSERVABLE_FLOOR_MS.
import {
  startRunHeartbeat,
  HEARTBEAT_SILENCE_THRESHOLD_MS,
  HEARTBEAT_INTERVAL_MS,
  POST_COMPLETION_TIMEOUT_MS,
  type RunHeartbeatTick,
} from './run-heartbeat';
import {
  buildPauseExpiryArtifact,
  mergePodObservations,
  mergeExperimentPodObservations,
  recordWatchdogTick,
  EMPTY_WATCHDOG_OBSERVATIONS,
  type WatchdogTickObservations,
  type GateEventObservation,
  type InputDeliveryObservation,
  type StreamEntryObservation,
  type PauseExpiryObservations,
  type CancellationObservations,
  type DeadlineClampObservations,
  type LauncherPodObservations,
  type PauseExpiryArtifact,
  type HeldHookTimeoutObservations,
  type HeartbeatObservation,
} from './control-runtime-timeout';

/** Recorded outcome of one experiment. Mirrors the sibling file's shape. */
interface ExperimentReport {
  readonly name: string;
  readonly ok: boolean;
  readonly detail: string;
  readonly artifact: Record<string, unknown>;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * The shortest pause budget that can yield a heartbeat record.
 *
 * Derived from the production emitter's own constants rather than restated: it logs
 * only once a run has been silent for {@link HEARTBEAT_SILENCE_THRESHOLD_MS}, and it
 * looks every {@link HEARTBEAT_INTERVAL_MS}, so the pause has to survive past the
 * first tick that follows the threshold. A pause shorter than this produces **no
 * record at all**, `heartbeats_during_pause` is then a true zero, and W2-05 fails
 * for a reason that has nothing to do with the pause implementation.
 *
 * Published so the launcher can pick a budget rather than discover this from a
 * failed run. `--heartbeat` selects it automatically.
 *
 * Declared before the budget that reads it: this is evaluated at import, so the
 * order is load-bearing rather than stylistic.
 */
export const HEARTBEAT_OBSERVABLE_FLOOR_MS = HEARTBEAT_SILENCE_THRESHOLD_MS + HEARTBEAT_INTERVAL_MS * 2;

/**
 * Budget override, so the launcher can raise it without editing this file.
 *
 * A malformed or nonpositive value is ignored rather than clamped: silently
 * substituting a default for what an operator asked for is how a run ends up
 * measuring a budget nobody chose.
 */
function envBudgetMs(): number | null {
  // `--heartbeat` selects the smallest budget that can produce a heartbeat record, so
  // the launcher does not have to know the emitter's thresholds to collect the
  // visibility fields. An explicit env value still wins: an operator who named a
  // budget must get that budget.
  const raw =
    process.env.ADP_PAUSE_EXPIRY_BUDGET_MS ??
    (process.argv.includes('--heartbeat') ? String(HEARTBEAT_OBSERVABLE_FLOOR_MS) : undefined);
  if (raw === undefined || raw.trim() === '') return null;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    console.log(
      `ignoring ADP_PAUSE_EXPIRY_BUDGET_MS=${JSON.stringify(raw)}: not a positive number of milliseconds`,
    );
    return null;
  }
  return parsed;
}

/**
 * The shortened pause budget the expiry scenarios configure.
 *
 * Short enough to keep each probe bounded, long enough that the barrier has
 * genuinely held a real tool call across a real subprocess boundary before the
 * timer fires. The expiry itself is produced by the gate's own timer running out
 * this budget — never by calling `resume()`, which would prove nothing about
 * expiry. `explicit_resume_calls` in the artifact is the audit of that.
 *
 * ## Why the launcher must raise this to collect the visibility fields
 *
 * The heartbeat only logs once a run has been silent for a minute, so a pause
 * shorter than {@link HEARTBEAT_OBSERVABLE_FLOOR_MS} produces **no record at all** —
 * and `heartbeats_during_pause` would then be a true zero, which W2-05 rejects. That
 * rejection is correct, not a bug: a 12s pause genuinely has no visibility evidence
 * to offer. So the run that collects those fields passes `--heartbeat` (or an
 * explicit `ADP_PAUSE_EXPIRY_BUDGET_MS`), and the natural-expiry scenario starts the
 * emitter only when the configured budget can actually produce a record.
 */
const SHORT_PAUSE_BUDGET_MS = envBudgetMs() ?? 12_000;

/**
 * Bound on every wait for an observation, so a stuck probe fails rather than hangs.
 *
 * Derived from the configured budget rather than fixed: the longest thing any probe
 * waits for is the expiry itself, so a ceiling below the budget would time out every
 * run that raises it — reporting "unobserved" for an expiry that was merely still
 * pending. The margin covers the release, the annotation delivery and the tool's
 * post-resume side effect.
 */
const OBSERVATION_TIMEOUT_MS = SHORT_PAUSE_BUDGET_MS + 90_000;

/** Wait for a condition. A `false` return means unobserved — never assumed true. */
async function waitFor(predicate: () => boolean, timeoutMs = OBSERVATION_TIMEOUT_MS): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await sleep(50);
  }
  return predicate();
}

/**
 * Reduce a live SDK message to the stream facts the extra-turn question needs.
 *
 * A `user` message can carry several `tool_result` blocks, so this returns a list
 * rather than one entry, and it keeps each block's `tool_use_id`. That id is the
 * whole point: the turn verdict is ordering against the **held** call's result,
 * and without ids an unrelated tool completing first makes a later extra turn
 * invisible. A block with no id is recorded with `toolUseId` absent, which the
 * producer treats as "not the held result" rather than as a match.
 */
function streamEntriesOf(message: Record<string, unknown>): StreamEntryObservation[] {
  const at = Date.now();
  if (message.type === 'result') return [{ kind: 'result', at }];
  if (message.type === 'assistant') return [{ kind: 'assistant', at }];
  if (message.type === 'user') {
    const content = (message.message as { content?: unknown } | undefined)?.content;
    if (!Array.isArray(content)) return [];
    return content
      .filter((block) => (block as { type?: string })?.type === 'tool_result')
      .map((block) => {
        const id = (block as { tool_use_id?: unknown }).tool_use_id;
        return {
          kind: 'tool_result' as const,
          at,
          ...(typeof id === 'string' && id.length > 0 ? { toolUseId: id } : {}),
        };
      });
  }
  return [];
}

/** Everything one live paused attempt observed about itself. */
interface LiveRunObservations {
  /** Neutral coordinator events, in the order the gate published them. */
  readonly gateEvents: GateEventObservation[];
  /** Inputs that reached the production transport, with the transport's verdict. */
  readonly deliveries: InputDeliveryObservation[];
  /** Inputs delivered strictly after a cancellation, for the abort measurement. */
  readonly deliveriesAfterCancel: InputDeliveryObservation[];
  /** Live output stream, reduced to assistant / tool_result / result ordering. */
  readonly stream: StreamEntryObservation[];
  /**
   * `tool_use_id` of the call that was parked when the pause was requested.
   *
   * Read from the real `PreToolUse` payload for the tool the scenario pauses on,
   * so the turn verdict is decided against that call's result and no other.
   * `null` means the payload carried no id — reported, not substituted.
   */
  heldToolUseId: string | null;
  /** Barrier decisions for tools that reached it while a pause was in force. */
  readonly heldWorkDecisions: Array<'admit' | 'deny'>;
  /** Parked calls still awaiting a decision when the run finished. */
  unresolvedHeldWork: number;
  /** Every `PauseGate.resume()` call, counted at the gate itself. */
  explicitResumeCalls: number;
  /** Attempts `resilientQuery` constructed. More than one means it retried. */
  attempts: number;
  /** Idle windows that re-armed because the run was intentionally suspended. */
  rearms: number;
  pauseStartedAt: number | null;
  pauseReleasedAt: number | null;
  /** Did the fixture's side effect exist while the barrier was holding it? */
  fixtureExistedDuringHold: boolean | null;
  /** Gate phase sampled mid-hold, so a lapsed pause cannot masquerade as a held one. */
  phaseDuringHold: string | null;
  /** Did the fixture's side effect exist once the run finished? */
  fixtureExistedAtEnd: boolean;
  /**
   * Heartbeat records the production emitter logged for THIS execution.
   *
   * Collected by running the same `run-heartbeat.ts` module `agent-worker.ts` runs,
   * wired to the gate this scenario pauses. That binding is the point: an emitter
   * reading a different gate produces records about a different run, however healthy
   * they look.
   */
  readonly heartbeats: HeartbeatObservation[];
  /**
   * What the production exit watchdog decided, tick by tick.
   *
   * Accumulated by the producer's {@link recordWatchdogTick} rather than inline here,
   * so the rule that decides which ticks count — due-only, attributed by the tick's
   * own `paused` verdict, sticky toward a firing — is covered by ordinary CI. Held as
   * one object rather than four mirrored fields so a later tick cannot update some of
   * them and miss others. Observed from the same emitter as the heartbeats, which is
   * what makes it a measurement of this execution rather than a launcher hand-off.
   */
  watchdog: WatchdogTickObservations;
  /** A non-cancellation error that ended the run, if any. */
  error: string | null;
}

/** What a scenario may do once the barrier is provably holding a real tool. */
interface HeldContext {
  readonly gate: PauseGate;
  readonly adapter: ClaudeControlAdapter;
  readonly target: string;
  readonly observations: LiveRunObservations;
  /** Mark the moment of cancellation, so later deliveries are attributed to it. */
  markCancelled(): void;
}

interface LiveRunOptions {
  readonly dirPrefix: string;
  readonly pauseBudgetMs: number;
  /** Production default unless a scenario is specifically exercising the watchdog. */
  readonly idleTimeoutMs?: number;
  /** Runs once the gate reports a parked tool. The run continues underneath it. */
  readonly onHeld?: (ctx: HeldContext) => Promise<void>;
  /** Longest the scenario will wait for the side effect after the stream ends. */
  readonly settleAfterStreamMs?: number;
  /**
   * Run the production heartbeat emitter against this scenario's own gate.
   *
   * Off by default, because a scenario whose pause is shorter than
   * {@link HEARTBEAT_OBSERVABLE_FLOOR_MS} cannot produce a record and would report a
   * true zero. On for the natural-expiry scenario when the launcher selects a budget
   * that outlasts the emitter's silence threshold.
   */
  readonly observeHeartbeat?: boolean;
  /**
   * Make the post-completion exit watchdog genuinely **due** during the pause.
   *
   * ## Why a fault has to be injected at all, and why it is recorded
   *
   * The production watchdog fires ten minutes after the query completes. In this
   * runner the query has not completed while the Write is parked, and once the result
   * arrives the loop breaks and teardown takes seconds — so on a natural-expiry run
   * the watchdog is **never due**, and its `false` says nothing about suppression.
   * Root's review caught exactly that: deleting the `!paused` guard from the
   * production module leaves such a run still reporting `false`, so the observation
   * is not evidence and no regression protects the guard.
   *
   * Making it due needs the clock or the completion time moved, and this is the
   * smaller, more honest lever: the emitter is given a completion time already past
   * the bound, while every other input stays the real one — the same production
   * module, the same `PauseGate` the barrier parked the live Write in, the real
   * `isPauseActive()` doing the suppressing. So what is faked is only *the watchdog
   * being due*; whether it then fires is the shipped code's decision about the real
   * gate.
   *
   * The injected offset is recorded in the artifact as
   * `watchdog_completion_fault_ms` so no reader can mistake this for an unaided
   * observation, and the scenario reports `watchdog_due_ticks` so a zero cannot pass
   * as suppression. Off for every other scenario: a run that did not need the fault
   * must not carry it.
   */
  readonly watchdogProbe?: {
    /**
     * How far in the past to report the query's completion, so the bound has elapsed.
     * Must exceed `POST_COMPLETION_TIMEOUT_MS` or the watchdog is never due and the
     * scenario reports nothing.
     */
    readonly completionFaultMs: number;
    /**
     * Tick spacing while probing. Production is 30s, which is too coarse to sample
     * both sides of a short pause's release. This changes only *how often the
     * question is asked*, never the answer: the decision on each tick is the
     * production module's, against the real gate.
     */
    readonly tickIntervalMs: number;
    /**
     * How long to keep ticking after the run ends, so the released-and-still-due
     * ticks are observed. Bounded, because an unbounded wait on a deferral that never
     * arrives is a hang rather than a measurement.
     */
    readonly observeAfterReleaseMs: number;
  };
}

/**
 * Drive one real attempt that pauses at its first tool, through production wiring.
 *
 * Every seam here is the shipped one: `resilientQuery` with the adapter's
 * `attemptInputFactory`, `onAttemptHandle`, `cancellationSource` and
 * `beforeOutput`, the `createWorkerToolHooks` composition around
 * `createClaudePauseHooks`, and a `PauseGate` whose only non-default setting is the
 * shortened budget. The scenario adds observation, not behaviour: the wrapper
 * around `PreToolUse` requests the pause and records the barrier's own decision,
 * then delegates to the production hook, which is what actually parks the call.
 *
 * Cleanup is unconditional — gate cancelled, adapter disposed, temp dir removed —
 * so a failed probe leaves no live subprocess or directory behind.
 */
async function runLivePausedAttempt(options: LiveRunOptions): Promise<LiveRunObservations> {
  const { resilientQuery } = await import('./utils/resilientQuery');
  const { isControlCancellation } = await import('./control-runtime');
  const dir = mkdtempSync(join(tmpdir(), options.dirPrefix));
  const target = join(dir, 'pause-expiry-probe.txt');
  const obs: LiveRunObservations = {
    gateEvents: [],
    deliveries: [],
    deliveriesAfterCancel: [],
    stream: [],
    heldToolUseId: null,
    heartbeats: [],
    watchdog: EMPTY_WATCHDOG_OBSERVATIONS,
    heldWorkDecisions: [],
    unresolvedHeldWork: 0,
    explicitResumeCalls: 0,
    attempts: 0,
    rearms: 0,
    pauseStartedAt: null,
    pauseReleasedAt: null,
    fixtureExistedDuringHold: null,
    phaseDuringHold: null,
    fixtureExistedAtEnd: false,
    error: null,
  };
  let cancelledAt: number | null = null;
  let held: Promise<void> = Promise.resolve();
  // Fed to the production emitter as this run's own activity/turn/completion state,
  // updated from the live stream exactly as `runAgent` updates its equivalents.
  let lastActivityAt = Date.now();
  let turns = 0;
  let queryCompletedAt: number | null = null;

  const observer = new ClaudeBackgroundWorkObserver();
  const gate = new PauseGate({
    // The shortened budget IS the scenario. Everything else is production default.
    defaultTimeoutMs: options.pauseBudgetMs,
    settleTimeoutMs: 2_000,
    backgroundWorkProbe: () => observer.count(),
    onEvent: (event: PauseGateEvent) => {
      obs.gateEvents.push({
        type: event.type,
        ...(event.type === 'pause_released' ? { expired: event.expired } : {}),
        ...(event.type === 'pause_unavailable' ? { failure: event.failure } : {}),
      });
      if (event.type === 'pause_requested') obs.pauseStartedAt ??= Date.now();
      if (event.type === 'pause_released') obs.pauseReleasedAt = Date.now();
    },
  });
  // Counted at the gate rather than trusted from the scenario: if anything at all —
  // production code or probe — resumed this pause, `auto_resumed` must not be
  // claimed, and this is the observation that decides it.
  const realResume = gate.resume.bind(gate);
  (gate as unknown as { resume: () => Promise<boolean> }).resume = async () => {
    obs.explicitResumeCalls += 1;
    return realResume();
  };

  const adapter = new ClaudeControlAdapter({
    pauseGate: gate,
    backgroundWorkObserver: observer,
    implementedVerbs: IMPLEMENTED_CONTROL_VERBS,
  });
  // Observe the registry delivery used by both submitInput and annotateExpiry.
  // Instrument this adapter instance only; retain its real endpoint and transport.
  const registry = (adapter as unknown as { registry: CurrentAttemptRegistry }).registry;
  const realDeliver = registry.deliver.bind(registry);
  registry.deliver = async (input) => {
    const result = await realDeliver(input);
    const record: InputDeliveryObservation = { kind: input.kind, text: input.text, result, at: Date.now() };
    obs.deliveries.push(record);
    if (cancelledAt !== null) obs.deliveriesAfterCancel.push(record);
    return result;
  };

  const buildOptions = (hooks: ClaudePauseHooks): Record<string, unknown> => {
    obs.attempts += 1;
    const composed = createWorkerToolHooks({
      agentType: 'developer',
      store: new TmpSpillStore(dir),
      pauseHooks: hooks,
    });
    const productionPreToolUse = composed.PreToolUse![0].hooks[0];
    let requested = false;
    return {
      hooks: {
        ...composed,
        PreToolUse: [
          {
            // The production bound, derived by the adapter from the gate's own
            // budget. A number chosen here instead would be a fixture deciding
            // whether the barrier can hold.
            timeout: hooks.preToolUseTimeoutSeconds,
            hooks: [
              async (input: unknown, id?: string, opts?: { signal: AbortSignal }) => {
                const toolName = (input as { tool_name?: string }).tool_name;
                if (toolName === 'Write' && !requested) {
                  requested = true;
                  // The identity of the call this pause parks, taken from the real
                  // hook payload the same way the production pause hooks take it
                  // (`tool_use_id`, falling back to the callback's own id). Recorded
                  // before the pause is requested, so it is available even if the
                  // run then fails: the turn verdict is decided against THIS call's
                  // result, and a missing id must show as missing.
                  const payloadId = (input as { tool_use_id?: unknown }).tool_use_id;
                  const heldId = typeof payloadId === 'string' && payloadId.length > 0 ? payloadId : id;
                  obs.heldToolUseId = heldId !== undefined && heldId.length > 0 ? heldId : null;
                  // Requested through the production adapter, which consults the
                  // capability intersection before it ever reaches the gate.
                  await adapter.requestPause({ timeoutMs: options.pauseBudgetMs });
                  held = (async () => {
                    // Sampled only once the gate reports a genuinely parked call,
                    // so "absent during the hold" is absence during a hold.
                    await waitFor(() => gate.heldCount() > 0, 20_000);
                    await sleep(Math.floor(options.pauseBudgetMs / 3));
                    obs.phaseDuringHold = gate.currentPhase();
                    obs.fixtureExistedDuringHold = existsSync(target);
                    await options.onHeld?.({
                      gate,
                      adapter,
                      target,
                      observations: obs,
                      markCancelled: () => {
                        cancelledAt = Date.now();
                      },
                    });
                  })();
                }
                const pausedAtEntry = gate.isPauseActive();
                if (pausedAtEntry) obs.unresolvedHeldWork += 1;
                const result = await productionPreToolUse(input as never, id, opts);
                if (pausedAtEntry) {
                  const decision = (result as { hookSpecificOutput?: { permissionDecision?: string } })
                    .hookSpecificOutput?.permissionDecision;
                  obs.heldWorkDecisions.push(decision === 'deny' ? 'deny' : 'admit');
                  obs.unresolvedHeldWork = Math.max(0, obs.unresolvedHeldWork - 1);
                }
                return result;
              },
            ],
          },
        ],
      },
    };
  };

  /** This tick's logged record, awaiting the emitter's own paused verdict. */
  let pendingRecord: HeartbeatObservation | null = null;
  // The production heartbeat emitter, on the gate this scenario pauses.
  //
  // This is the binding that makes the visibility fields evidence: `gate` below is
  // the same object the barrier parks tools in and the same object whose expiry timer
  // releases them, so a paused tick here is a tick about the pause being measured.
  // Running the worker's own heartbeat instead — even from inside the same pod — would
  // describe the worker's gate, which this experiment never pauses.
  //
  // Sinks mirror the worker's: the structured `log` is what carries `phase` and the
  // paused-tick fields, and the record is kept verbatim so the producer reads the
  // paused-vs-stalled wording rather than assuming it.
  const heartbeat = options.observeHeartbeat
    ? startRunHeartbeat(
        {
          gate: () => gate,
          lastActivityAt: () => lastActivityAt,
          turnCount: () => turns,
          // The one input a scenario may fault, and only when it asked to: an already
          // elapsed completion time, so the watchdog is genuinely due while the real
          // gate is really paused. Every other input, and the suppression decision
          // itself, stays the production module's. See `forceWatchdogDueOffsetMs`.
          queryCompletedAt: () =>
            options.watchdogProbe === undefined
              ? queryCompletedAt
              : Date.now() - options.watchdogProbe.completionFaultMs,
          ...(options.watchdogProbe === undefined
            ? {}
            : { intervalMs: options.watchdogProbe.tickIntervalMs }),
          // Deliberately NOT wired to record the verdict. `startRunHeartbeat` calls
          // `onTick` and then `onForceExit`, and this callback is handed only the fact
          // that an exit was decided — not whether the pause was in force when it was.
          // Recording from here therefore cannot attribute the firing, and the
          // scenario below *requires* a firing after release, so an unattributed write
          // would overwrite the during-pause `false` and make the experiment fail
          // precisely when it succeeded. Attribution happens per tick, in `onTick`.
          //
          // It stays wired as a no-op for the one thing it is needed for: making sure
          // a force-exit decision is never obeyed. This probe must not exit the process
          // it is measuring — a decision to force-exit during the pause IS the failure
          // `exit_watchdog_fired` reports, so it is captured rather than acted on.
          onForceExit: () => {
            /* observed via onTick; never acted on. */
          },
          onTick: (tick: RunHeartbeatTick) => {
            // The accumulation rule — due-only, attributed by the tick's own `paused`
            // flag, sticky toward a firing — lives in the producer so ordinary CI
            // covers it. See recordWatchdogTick for why each of those matters.
            obs.watchdog = recordWatchdogTick(obs.watchdog, tick);
            // The record is paired with the emitter's own `paused` verdict for the
            // same tick, rather than inferred from which fields it happened to log.
            // `log` runs before `onTick` within a tick, so the pending record is this
            // tick's; a tick that logged nothing leaves nothing to pair.
            if (pendingRecord !== null) {
              obs.heartbeats.push({ ...pendingRecord, paused: tick.paused });
              pendingRecord = null;
            }
          },
        },
        {
          log: (_level, message, context) => {
            if (context?.phase !== 'heartbeat') return;
            pendingRecord = {
              at: Date.now(),
              // Replaced by the emitter's verdict in `onTick`; never read as-is.
              paused: false,
              controlPhase: (context.controlPhase as string | undefined) ?? null,
              text: message,
            };
          },
        },
      )
    : null;

  try {
    const iterator = resilientQuery({
      queryParams: {
        prompt: `Use the Write tool exactly once to write "expired" to ${target}. Then stop.`,
        options: { cwd: dir, permissionMode: 'bypassPermissions', maxTurns: 4 },
      } as never,
      // No retries: a retry would replace the attempt whose pause is being
      // measured, and "same-execution resume" is the claim under test.
      maxRetries: 0,
      ...(options.idleTimeoutMs === undefined ? {} : { idleTimeoutMs: options.idleTimeoutMs }),
      idleSuspended: () => gate.isPauseActive(),
      beforeOutput: () => gate.waitForOutput(),
      attemptInputFactory: adapter.attemptInputFactory(buildOptions),
      onAttemptHandle: adapter.onAttemptHandle(),
      cancellation: adapter.cancellationSource(),
      log: (line: string) => {
        if (line.includes('intentionally suspended')) obs.rearms += 1;
      },
    } as never);

    for await (const message of iterator) {
      // The same three updates `runAgent` makes, so the shared emitter sees the same
      // kind of state here as it does in the worker: output arrived, a turn happened,
      // the query finished. Without them the heartbeat would read this run as silent
      // from the start and its records would not track the real pause window.
      lastActivityAt = Date.now();
      const type = (message as { type?: string }).type;
      if (type === 'assistant') turns += 1;
      obs.stream.push(...streamEntriesOf(message as unknown as Record<string, unknown>));
      if (type === 'result') {
        queryCompletedAt = Date.now();
        break;
      }
    }
  } catch (err) {
    // A cancellation is the measured outcome of the abort scenario, not a fault.
    // Anything else is recorded so the report can say why the probe failed.
    if (!isControlCancellation(err)) obs.error = (err as Error)?.message ?? String(err);
  } finally {
    await held;
    // The parked write lands when the expiry admits it, which can trail the result
    // message. Waiting on the side effect rather than on the model's exit.
    await waitFor(() => existsSync(target), options.settleAfterStreamMs ?? 20_000);
    obs.fixtureExistedAtEnd = existsSync(target);
    // Keep ticking briefly past the run, but only for the watchdog probe: the pause is
    // over by now, so these are the released-and-still-due ticks that show suppression
    // was a deferral rather than an amnesty. Bounded by the caller — waiting forever
    // for a deferral that never arrives is a hang, not a measurement. The gate is
    // still un-cancelled here, so `isPauseActive()` is answering about the real gate.
    if (options.watchdogProbe !== undefined) {
      await waitFor(
        () => obs.watchdog.firedAfterRelease === true,
        options.watchdogProbe.observeAfterReleaseMs,
      );
    }
    // Before the gate is cancelled, so no tick observes the probe's own teardown as
    // the run's state — and unconditional, so a failed probe leaves no live interval.
    heartbeat?.stop();
    gate.cancel('probe finished');
    await adapter.dispose();
    rmSync(dir, { recursive: true, force: true });
  }
  return obs;
}

/** The observations a live run contributes to the artifact, without re-deriving any. */
function observationsFrom(run: LiveRunObservations): Partial<PauseExpiryObservations> {
  return {
    gateEvents: run.gateEvents,
    explicitResumeCalls: run.explicitResumeCalls,
    deliveries: run.deliveries,
    stream: run.stream,
    // Absent rather than `null` when the hook payload carried no id: the producer
    // then leaves the turn verdict unobserved instead of matching against nothing.
    ...(run.heldToolUseId === null ? {} : { heldToolUseId: run.heldToolUseId }),
    // The heartbeat/exit-watchdog observations this execution's own emitter produced.
    // Omitted entirely when the emitter was not run, so those fields stay unobserved
    // rather than recording a zero the emitter never had a chance to exceed.
    ...(run.heartbeats.length === 0 && run.watchdog.firedDuringPause === null
      ? {}
      : {
          pod: {
            heartbeats: run.heartbeats,
            ...(run.watchdog.firedDuringPause === null ? {} : { exitWatchdogFired: run.watchdog.firedDuringPause }),
          },
        }),
    ...(run.pauseStartedAt !== null && run.pauseReleasedAt !== null
      ? { pauseWindow: { startedAt: run.pauseStartedAt, releasedAt: run.pauseReleasedAt } }
      : {}),
  };
}

/**
 * Experiment A: does a pause left alone until its budget runs out resume itself?
 *
 * The model is asked to create a file, the pause is requested when that tool
 * reaches the barrier, and then **nothing intervenes**. The file's absence during a
 * measured hold is the evidence the barrier held; its appearance afterwards, with
 * `resume()` never called, is the evidence the expiry timer released it. Both
 * halves are needed: absence alone is also what a crashed run produces, and
 * appearance alone is also what a barrier that never engaged produces.
 *
 * The annotation is observed at the production delivery seam, so "exactly one, and
 * it started no turn" is measured from what reached the transport and what the
 * provider then sent back — not from the flag the adapter set on the way in.
 */
async function experimentNaturalExpiry(): Promise<{
  report: ExperimentReport;
  observations: Partial<PauseExpiryObservations>;
}> {
  // The emitter is only started when the configured budget can actually produce a
  // record. Below the floor it would report a true zero, so the fields stay
  // unobserved and the artifact names the budget the launcher needs instead.
  const heartbeatObservable = SHORT_PAUSE_BUDGET_MS >= HEARTBEAT_OBSERVABLE_FLOOR_MS;
  const run = await runLivePausedAttempt({
    dirPrefix: 'adp-expiry-',
    pauseBudgetMs: SHORT_PAUSE_BUDGET_MS,
    observeHeartbeat: heartbeatObservable,
  });
  const expiredRelease = run.gateEvents.some((event) => event.type === 'pause_released' && event.expired === true);
  const admitted = run.heldWorkDecisions.includes('admit');
  const ok =
    run.error === null &&
    run.phaseDuringHold === 'paused' &&
    run.fixtureExistedDuringHold === false &&
    expiredRelease &&
    run.explicitResumeCalls === 0 &&
    admitted &&
    run.fixtureExistedAtEnd &&
    // A run that was supposed to collect visibility evidence and collected none has
    // not measured what it was configured to measure, so it fails rather than
    // reporting a pass with a silently empty field.
    (!heartbeatObservable || run.heartbeats.length > 0);

  return {
    report: {
      name: 'a pause left to its budget auto-resumes and admits the work it held (AC-P3)',
      ok,
      detail: ok
        ? `a real Write was parked for the ${SHORT_PAUSE_BUDGET_MS}ms budget with no file created, then the ` +
          `expiry timer released it and the file appeared — resume() was never called` +
          (heartbeatObservable
            ? `; the production heartbeat logged ${run.heartbeats.length} record(s) for this same gate`
            : `; heartbeat visibility not collected (budget below the ${HEARTBEAT_OBSERVABLE_FLOOR_MS}ms floor)`)
        : `error=${run.error} phaseDuringHold=${run.phaseDuringHold} absentDuringHold=${run.fixtureExistedDuringHold} ` +
          `expiredRelease=${expiredRelease} resumeCalls=${run.explicitResumeCalls} admitted=${admitted} ` +
          `fileAtEnd=${run.fixtureExistedAtEnd} heartbeats=${run.heartbeats.length} ` +
          `heartbeatExpected=${heartbeatObservable} — a bounded pause must hold, then continue by itself, and must ` +
          `not discard the work it held`,
      artifact: {
        adapter_id: 'claude',
        sdk_version: CLAUDE_SDK_VERSION,
        configured_pause_budget_ms: SHORT_PAUSE_BUDGET_MS,
        phase_during_hold: run.phaseDuringHold,
        fixture_write_during_hold: run.fixtureExistedDuringHold,
        fixture_write_after_expiry: run.fixtureExistedAtEnd,
        held_work_decisions: run.heldWorkDecisions,
        held_tool_use_id: run.heldToolUseId,
        explicit_resume_calls: run.explicitResumeCalls,
        attempts_constructed: run.attempts,
        gate_event_types: run.gateEvents.map((event) => event.type),
        // The execution binding, stated in the artifact so a reviewer can see that
        // the heartbeat records belong to the gate that was paused rather than to a
        // parent worker that happened to be running in the same pod.
        heartbeat_emitter: 'run-heartbeat.ts (the module agent-worker.ts runs)',
        heartbeat_gate: 'this experiment’s own PauseGate — the one the barrier parked the Write in',
        heartbeat_observable_floor_ms: HEARTBEAT_OBSERVABLE_FLOOR_MS,
        heartbeat_records: run.heartbeats.length,
        exit_watchdog_fired: run.watchdog.firedDuringPause,
        observed_by: 'fixture filesystem plus the production adapter delivery seam',
      },
    },
    observations: observationsFrom(run),
  };
}

/**
 * Experiment B: does the real idle-retry watchdog leave a paused run alone?
 *
 * The watchdog and the barrier read the same evidence — no SDK messages — and draw
 * opposite conclusions. If it reads the deliberate quiet as a stall it abandons the
 * live attempt and retries, destroying the same-execution continuation pause exists
 * to provide. So the idle window is shortened below the pause budget, guaranteeing
 * it fires mid-pause, and what is recorded is whether the attempt survived.
 *
 * The quiet is genuine, not simulated: a real CLI subprocess with a real tool call
 * parked at the barrier, which is exactly the production situation. A retry would
 * show as a second constructed attempt; `attempts` is that count.
 */
async function experimentIdleWatchdogDuringPause(): Promise<{
  report: ExperimentReport;
  observations: Partial<PauseExpiryObservations>;
}> {
  // Allow real SDK startup/model latency before declaring a stall. Hold the
  // tool longer than this window so suspension must still be exercised.
  const IDLE_TIMEOUT_MS = 60_000;
  const pauseBudgetMs = Math.max(SHORT_PAUSE_BUDGET_MS, 120_000);
  const run = await runLivePausedAttempt({
    dirPrefix: 'adp-expiry-idle-',
    pauseBudgetMs,
    idleTimeoutMs: IDLE_TIMEOUT_MS,
  });
  // Only meaningful if the window actually fired during the pause. Without a
  // re-arm the probe observed nothing, so the field stays unreported rather than
  // recording a watchdog that never ran as a watchdog that behaved.
  const exercised = run.rearms > 0;
  const idleRetryFired = exercised ? run.attempts > 1 : undefined;
  const ok = run.error === null && exercised && idleRetryFired === false && run.fixtureExistedAtEnd;

  return {
    report: {
      name: 'the real idle-retry watchdog re-arms instead of retrying a paused run (AC-P5)',
      ok,
      detail: ok
        ? `the ${IDLE_TIMEOUT_MS}ms idle window fired ${run.rearms}x while the pause held and re-armed each ` +
          `time; the single attempt survived and completed its work`
        : `error=${run.error} rearms=${run.rearms} attempts=${run.attempts} fileAtEnd=${run.fixtureExistedAtEnd} ` +
          `— a run quiet because an operator paused it must not be retried, and a window that never fired ` +
          `proves nothing either way`,
      artifact: {
        production_module: 'utils/resilientQuery',
        idle_timeout_ms: IDLE_TIMEOUT_MS,
        pause_budget_ms: pauseBudgetMs,
        idle_windows_rearmed: run.rearms,
        attempts_constructed: run.attempts,
        idle_retry_fired: idleRetryFired ?? null,
        watchdog_exercised: exercised,
        observed_by: 'production resilientQuery with a real tool parked at the barrier',
      },
    },
    observations: idleRetryFired === undefined ? {} : { idleRetryFired },
  };
}

/**
 * Experiment C: is the pause budget trimmed to fit the run's deadline?
 *
 * Read from the production coordinator's own calculation, with the deadline coming
 * from the production environment reader, so the numbers recorded are the numbers
 * the shipped code would use. Two cases, because they fail differently: a normal
 * deadline must yield a budget that leaves the finalization reserve intact, and a
 * deadline already inside that reserve must be **refused** rather than granted as a
 * pause that expires the instant it begins.
 *
 * No SDK: the clamp is arithmetic over a deadline and a clock, and a model has no
 * part in it. Recording it from the real gate is the measurement; involving a
 * provider would add spend without adding evidence.
 */
async function experimentDeadlineClamp(): Promise<{
  report: ExperimentReport;
  observations: Partial<PauseExpiryObservations>;
}> {
  const now = Date.now();
  const remainingMs = 5 * 60 * 1000;
  // The production env reader, given a real deadline, exactly as the worker does.
  const healthyEnv = { ADP_POD_DEADLINE_AT: new Date(now + remainingMs).toISOString() } as NodeJS.ProcessEnv;
  const clampGate = new PauseGate({ deadlineAt: () => controlDeadlineAt(healthyEnv) });
  const grantedMs = clampGate.safeBudget();

  // No room left: the deadline falls inside the finalization reserve.
  const exhaustedEnv = { ADP_POD_DEADLINE_AT: new Date(now + 5_000).toISOString() } as NodeJS.ProcessEnv;
  const events: GateEventObservation[] = [];
  const refusalGate = new PauseGate({
    deadlineAt: () => controlDeadlineAt(exhaustedEnv),
    onEvent: (event) =>
      events.push({ type: event.type, ...(event.type === 'pause_unavailable' ? { failure: event.failure } : {}) }),
  });
  const exhaustedBudget = refusalGate.safeBudget();
  const refusal = await refusalGate.requestPause();
  const refusalFailure = events.find((event) => event.type === 'pause_unavailable')?.failure;

  const clamp: DeadlineClampObservations = {
    // `null` rather than `0` on a refusal: zero would read downstream as a pause
    // that was granted and expired instantly, which is the opposite of refused.
    grantedMs,
    remainingMs,
    finalizationMarginMs: DEFAULT_FINALIZATION_MARGIN_MS,
    nonpositiveRequest: { outcome: refusal.outcome, ...(refusalFailure ? { failure: refusalFailure } : {}) },
  };

  const fits = grantedMs !== null && grantedMs <= remainingMs - DEFAULT_FINALIZATION_MARGIN_MS;
  const refused = refusal.outcome === 'unavailable' && refusalFailure === 'no_safe_budget';
  const ok = fits && refused && exhaustedBudget === null;

  return {
    report: {
      name: 'the pause budget is clamped to the deadline and a nonpositive budget is refused (AC-P6)',
      ok,
      detail: ok
        ? `granted ${grantedMs}ms against ${remainingMs}ms remaining with a ${DEFAULT_FINALIZATION_MARGIN_MS}ms ` +
          `reserve; a deadline inside the reserve was refused as ${refusalFailure}`
        : `granted=${grantedMs} remaining=${remainingMs} margin=${DEFAULT_FINALIZATION_MARGIN_MS} ` +
          `exhaustedBudget=${exhaustedBudget} refusal=${refusal.outcome}/${refusalFailure} — a pause must leave ` +
          `room to write a terminal state, and one with no room must be refused rather than expire instantly`,
      artifact: {
        production_source: 'PauseGate.safeBudget with controlDeadlineAt',
        granted_ms: grantedMs,
        remaining_ms: remainingMs,
        finalization_margin_ms: DEFAULT_FINALIZATION_MARGIN_MS,
        exhausted_deadline_budget: exhaustedBudget,
        refusal_outcome: refusal.outcome,
        refusal_failure: refusalFailure ?? null,
      },
    },
    observations: { deadlineClamp: clamp },
  };
}

/**
 * Experiment D: does cancelling refuse the work it was holding?
 *
 * A real tool call is parked at the barrier, then the run is cancelled through the
 * production adapter's cancellation path. Three facts come from what the barrier
 * did with that parked call: it was refused rather than run, it was resolved rather
 * than left dangling, and no "the pause ended, carry on" note followed — an aborted
 * run is not a resumed one.
 *
 * The fixture file is the witness for the first. A cancellation that flushed its
 * parked tools on the way out would create it, which is precisely the side effect
 * the operator aborted to prevent. The budget is long on purpose: an expiry firing
 * mid-probe would be a second explanation for the same evidence.
 */
async function experimentCancellationWithoutAdmission(): Promise<{
  report: ExperimentReport;
  observations: Partial<PauseExpiryObservations>;
}> {
  const run = await runLivePausedAttempt({
    dirPrefix: 'adp-expiry-cancel-',
    pauseBudgetMs: 120_000,
    // A late write is still the side effect the abort was meant to prevent, so the
    // scenario waits for one rather than declaring success at the stream's end.
    settleAfterStreamMs: 4_000,
    onHeld: async ({ adapter, markCancelled }) => {
      markCancelled();
      adapter.cancel('pause-expiry probe: operator abort');
    },
  });

  const cancellation: CancellationObservations = {
    heldWorkDecisions: run.heldWorkDecisions,
    unresolvedHeldWork: run.unresolvedHeldWork,
    deliveriesAfterCancel: run.deliveriesAfterCancel,
  };
  const denied =
    run.heldWorkDecisions.length > 0 && run.heldWorkDecisions.every((decision) => decision === 'deny');
  const annotated = run.deliveriesAfterCancel.some(
    (input) => input.text === PAUSE_EXPIRY_ANNOTATION && input.result === 'delivered',
  );
  const ok = denied && run.unresolvedHeldWork === 0 && !run.fixtureExistedAtEnd && !annotated;

  return {
    report: {
      name: 'cancellation denies the work it held and sends no resume annotation (AC-P3)',
      ok,
      detail: ok
        ? `the parked Write was denied and never ran; no expiry annotation followed the abort`
        : `decisions=${run.heldWorkDecisions.join(',') || 'none'} unresolved=${run.unresolvedHeldWork} ` +
          `fixtureWrite=${run.fixtureExistedAtEnd} annotated=${annotated} — an abort that flushes its parked ` +
          `tools runs exactly the side effects the operator aborted to prevent`,
      artifact: {
        production_path: 'ClaudeControlAdapter.cancel -> PauseGate.cancel',
        held_work_decisions: run.heldWorkDecisions,
        unresolved_held_work: run.unresolvedHeldWork,
        fixture_write_after_cancel: run.fixtureExistedAtEnd,
        deliveries_after_cancel: run.deliveriesAfterCancel.length,
        annotation_after_cancel: annotated,
        observed_by: 'fixture filesystem plus the barrier decision for the parked call',
      },
    },
    observations: { cancellation },
  };
}

/**
 * Experiment E: does a DUE exit watchdog stand down while the pause holds?
 *
 * ## Why this scenario exists separately from A
 *
 * Experiment A reports `exit_watchdog_fired: false`, and on its own that number is
 * worth nothing. The production watchdog fires ten minutes after the query
 * completes; in A the query has not completed while the Write is parked, and once
 * the result arrives teardown takes seconds. The watchdog is therefore **never due**
 * in A, so deleting the `!paused` guard from the production module leaves A still
 * reporting `false`. An observation that survives removing the thing it claims to
 * observe is not evidence of it.
 *
 * So this scenario makes the watchdog genuinely due while the barrier is genuinely
 * holding a real live tool call, and asks the shipped code what it does. Three facts,
 * and all three are needed:
 *
 * 1. `watchdog_due_while_paused > 0` — the bound HAD elapsed, so there was a
 *    decision to make. Without this the rest is vacuous, which is the failure mode
 *    this scenario was written to remove.
 * 2. `exit_watchdog_fired === false` — and it still did not fire, with the real
 *    `PauseGate.isPauseActive()` doing the suppressing.
 * 3. `watchdog_fired_after_release === true` — the same due condition fires once the
 *    pause is over. Suppression must be a deferral; a guard that removed the bound
 *    rather than deferring it would pass (1) and (2) and fail here.
 *
 * Remove the `!paused` guard from `run-heartbeat.ts` and (2) fails. That is what
 * makes this a regression rather than a description.
 *
 * ## What is faked, stated plainly
 *
 * One input: the query's completion time, reported as already past the bound. That
 * is the whole fault, it is recorded in the artifact as
 * `watchdog_completion_fault_ms`, and the alternative — waiting out ten real minutes
 * of parked live tool call — buys no extra fidelity for a large multiple of the
 * spend. Everything the claim rests on is real: the production emitter module, the
 * `PauseGate` the barrier parked the live Write in, and that gate's own
 * `isPauseActive()`. The tick interval is shortened so both sides of the release are
 * sampled, which changes how often the question is asked and not the answer.
 */
async function experimentWatchdogDueDuringPause(): Promise<{
  report: ExperimentReport;
  observations: Partial<PauseExpiryObservations>;
}> {
  // Comfortably past the bound, so a slow tick cannot land before it elapses.
  const COMPLETION_FAULT_MS = POST_COMPLETION_TIMEOUT_MS + 60_000;
  const TICK_MS = 500;
  const run = await runLivePausedAttempt({
    dirPrefix: 'adp-expiry-watchdog-',
    pauseBudgetMs: SHORT_PAUSE_BUDGET_MS,
    observeHeartbeat: true,
    watchdogProbe: {
      completionFaultMs: COMPLETION_FAULT_MS,
      tickIntervalMs: TICK_MS,
      // Bounded: a deferral that has not arrived in this many ms is reported as not
      // arriving, rather than waited on indefinitely.
      observeAfterReleaseMs: 15_000,
    },
  });

  const ok =
    run.error === null &&
    // (1) there was something to suppress
    run.watchdog.dueWhilePaused > 0 &&
    // (2) and the production guard suppressed it
    run.watchdog.firedDuringPause === false &&
    // (3) and only deferred it
    run.watchdog.firedAfterRelease === true;

  return {
    report: {
      name: 'a due exit watchdog stands down while the pause holds, then fires after release (AC-P3)',
      ok,
      detail: ok
        ? `the post-completion bound was already elapsed on ${run.watchdog.dueWhilePaused} tick(s) taken while the ` +
          `barrier held a real Write, and the production watchdog did not force an exit on any of them; the same ` +
          `due condition forced one once the pause was released`
        : `error=${run.error} dueTicks=${run.watchdog.dueTicks} dueWhilePaused=${run.watchdog.dueWhilePaused} ` +
          `firedDuringPause=${run.watchdog.firedDuringPause} firedAfterRelease=${run.watchdog.firedAfterRelease} — a paused ` +
          `run must not be force-exited, and a watchdog that was never due proves nothing either way`,
      artifact: {
        production_module: 'run-heartbeat.ts (the module agent-worker.ts runs)',
        production_guard: 'PauseGate.isPauseActive() — this run’s own gate, holding a real live Write',
        post_completion_timeout_ms: POST_COMPLETION_TIMEOUT_MS,
        // The injected fault, named so no reader can take this for an unaided run.
        watchdog_completion_fault_ms: COMPLETION_FAULT_MS,
        watchdog_tick_interval_ms: TICK_MS,
        watchdog_due_ticks: run.watchdog.dueTicks,
        watchdog_due_while_paused: run.watchdog.dueWhilePaused,
        exit_watchdog_fired: run.watchdog.firedDuringPause,
        watchdog_fired_after_release: run.watchdog.firedAfterRelease,
        phase_during_hold: run.phaseDuringHold,
        observed_by:
          'the production emitter’s own decision on each tick, against the real gate; only the query ' +
          'completion time is injected',
      },
    },
    // Only the watchdog verdict. The pause window and heartbeat records stay
    // experiment A's, whose budget and timing are the unfaulted ones — mixing this
    // run's ticks into that count would put a faulted run's records in an unfaulted
    // run's field.
    observations:
      run.watchdog.firedDuringPause === null
        ? {}
        : { pod: { exitWatchdogFired: run.watchdog.firedDuringPause } },
  };
}

/**
 * Assemble the `pause_expiry` artifact from whatever the experiments observed.
 *
 * `preserved` carries the existing real measurements from
 * `control-runtime.integration.ts` — the held-hook timeout and spill preservation —
 * so this producer adds evidence without displacing any. `pod` carries the
 * launcher's pod-level observations, which partly overlap the experiments' own: the
 * exit-watchdog verdict may come from either side, `podKilled` is the launcher's
 * alone, and the heartbeat records are the experiments' alone — the launcher's
 * parameter type has no field for them, because a pod-log line cannot be attributed
 * to this execution rather than to the worker running beside it. Whatever neither
 * side supplied stays `null`, and the artifact names exactly what to collect.
 */
export function assemblePauseExpiryArtifact(input: {
  parts: ReadonlyArray<Partial<PauseExpiryObservations>>;
  preserved?: { heldHookTimeout?: HeldHookTimeoutObservations; spillOutputPreserved?: boolean };
  pod?: LauncherPodObservations;
}): PauseExpiryArtifact {
  const merged = input.parts.reduce<Partial<PauseExpiryObservations>>((acc, part) => ({ ...acc, ...part }), {});
  // `pod` is the one group more than one experiment contributes to, so the spread
  // above is wrong for it specifically: the natural-expiry run measures the
  // heartbeats and the watchdog run measures the exit verdict, and last-part-wins
  // silently drops whichever came first. Merged field-wise across the parts, then
  // against the launcher — both in the producer, so ordinary CI covers a rule that
  // decides whether real evidence reaches the artifact at all.
  const pod = mergePodObservations(
    mergeExperimentPodObservations(input.parts.map((part) => part.pod)),
    input.pod,
  );
  return buildPauseExpiryArtifact({
    ...merged,
    expectedAnnotationText: PAUSE_EXPIRY_ANNOTATION,
    ...(input.preserved?.heldHookTimeout ? { heldHookTimeout: input.preserved.heldHookTimeout } : {}),
    ...(input.preserved?.spillOutputPreserved === undefined
      ? {}
      : { spillOutputPreserved: input.preserved.spillOutputPreserved }),
    ...(pod === undefined ? {} : { pod }),
  });
}

/** The expiry experiments, in run order, exported for the sibling runner. */
export const TIMEOUT_EXPERIMENTS: ReadonlyArray<
  () => Promise<{ report: ExperimentReport; observations: Partial<PauseExpiryObservations> }>
> = [
  experimentNaturalExpiry,
  experimentIdleWatchdogDuringPause,
  experimentDeadlineClamp,
  experimentCancellationWithoutAdmission,
  experimentWatchdogDueDuringPause,
];

/**
 * Run every expiry experiment and collect both reports and observations.
 *
 * Exported so `control-runtime.integration.ts` can emit one artifact carrying its
 * own held-hook and spill measurements alongside these, rather than the evaluation
 * having to stitch two files together.
 */
export async function runTimeoutExperiments(log: (msg: string) => void = console.log): Promise<{
  reports: ExperimentReport[];
  parts: Array<Partial<PauseExpiryObservations>>;
}> {
  const reports: ExperimentReport[] = [];
  const parts: Array<Partial<PauseExpiryObservations>> = [];
  for (const experiment of TIMEOUT_EXPERIMENTS) {
    try {
      const { report, observations } = await experiment();
      reports.push(report);
      parts.push(observations);
      log(`${report.ok ? 'PASS' : 'FAIL'}  ${report.name}\n      ${report.detail}\n`);
    } catch (err) {
      // A probe that could not run is a failure with its cause, never a skip: a
      // missing observation must not read as a satisfied one.
      const detail = `experiment could not run: ${(err as Error)?.message ?? String(err)}`;
      reports.push({ name: experiment.name, ok: false, detail, artifact: {} });
      log(`ERROR ${experiment.name}\n      ${detail}\n`);
    }
  }
  return { reports, parts };
}

async function main(): Promise<number> {
  const jsonFlag = process.argv.indexOf('--json');
  const jsonPath = jsonFlag >= 0 ? process.argv[jsonFlag + 1] : null;

  const { reports, parts } = await runTimeoutExperiments();
  const artifact = assemblePauseExpiryArtifact({ parts });

  if (jsonPath) {
    writeFileSync(
      jsonPath,
      `${JSON.stringify({ sdk_version: CLAUDE_SDK_VERSION, reports, pause_expiry: artifact }, null, 2)}\n`,
    );
    console.log(`wrote ${jsonPath}`);
  }
  if (artifact.missing_launcher_inputs.length > 0) {
    console.log('pause_expiry fields this run did not observe — the launcher must supply them:');
    for (const entry of artifact.missing_launcher_inputs) console.log(`  - ${entry.field}: ${entry.collect}\n`);
  }

  const failed = reports.filter((report) => !report.ok);
  console.log(`${reports.length - failed.length}/${reports.length} experiments passed`);
  return failed.length === 0 ? 0 : 1;
}

if (require.main === module) {
  main().then(
    (code) => process.exit(code),
    (err) => {
      console.error(err);
      process.exit(1);
    },
  );
}

export {
  experimentNaturalExpiry,
  experimentIdleWatchdogDuringPause,
  experimentDeadlineClamp,
  experimentCancellationWithoutAdmission,
  experimentWatchdogDueDuringPause,
};
export type { ExperimentReport, LiveRunObservations };
