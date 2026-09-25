/**
 * The shared control-runtime composition — Issue #5891.
 *
 * Before this file existed, `agent-worker.ts`'s `main()` was the only place that
 * assembled the five pieces a live control channel needs (the pause barrier, the
 * Claude adapter, the command store, the steering queue and the in-pod HTTP
 * listener) into one working runtime. Issue #5891's fixture needs to start the
 * *same* runtime an ordinary run starts — not a second copy that merely looks
 * like it — so the dashboard's pause/resume/steer/abort routes can be proven to
 * reach a real running agent. A separately wired listener, even a faithful one,
 * would not establish that: it would prove the pieces can be assembled, not that
 * the pieces the gateway talks to in production are the ones running.
 *
 * This function is that composition, extracted verbatim from `agent-worker.ts`
 * with no behavior change — same construction order, same options, same
 * teardown obligations. Ordinary runs and the fixture both call it; there is now
 * exactly one place that decides how a control runtime is built.
 */
import { ControlListener, isAgentControlEnabled } from './control-listener';
import { ExplanationEvents, explanationsEnabled } from './explanation-events';
import { revalidateQueuedCommand } from './control-revalidation';
import { parseVerificationKeys } from './control-envelope';
import { ControlStateStore } from './control-state';
import { listenerActionsFor } from './control-runtime';
import { ClaudeControlAdapter, ClaudeBackgroundWorkObserver } from './harnesses/claude-control';
import { PauseGate } from './pause-gate';
import { controlDeadlineAt } from './control-deadline';
import { applyControlCommand, bindRuntimeTransitionsToStore } from './control-command-apply';
import { SteerQueue, type SteerOutcome } from './steer-queue';

export type ControlLogger = (level: string, message: string, context?: Record<string, unknown>) => void;

/** The published runtime a started control listener makes available to a run. */
export interface ControlRuntime {
  readonly adapter: ClaudeControlAdapter;
  readonly gate: PauseGate;
  readonly steerQueue: SteerQueue;
  /** Read the actual journal, including final cancellation, after listener shutdown. */
  readonly snapshot: () => ReturnType<ControlStateStore['snapshot']>;
}

export interface ControlRuntimeStartResult {
  /** `null` when the listener did not start — see `outcome.reason`. */
  readonly runtime: ControlRuntime | null;
  readonly events?: ExplanationEvents;
  readonly listener: ControlListener | null;
  readonly outcome: Awaited<ReturnType<ControlListener['start']>>;
}

/**
 * Build and start one run's control runtime from its process environment.
 *
 * Every value this reads (`ADP_CONTROL_*`) is placed there by the entrypoint's
 * registration step, exactly as it is for an ordinary run — this function does
 * not know or care whether the caller is the ordinary worker or the Wave 2
 * fixture, and that indifference is the point (#5891: "reuse the SAME
 * factory/composition in production and fixture").
 *
 * `onSteerOutcome` is the one caller-specific seam: the ordinary worker uses it
 * to append a marker to the live GitHub comment, and a fixture has no such
 * comment. Passing `undefined` there is a correct, inert choice — the queue
 * still resolves every submission, it just has nothing further to publish.
 */
export async function startControlRuntime(args: {
  env?: NodeJS.ProcessEnv;
  log: ControlLogger;
  onSteerOutcome?: (event: { commandId: string; outcome: SteerOutcome; reason: string }) => void;
}): Promise<ControlRuntimeStartResult> {
  const env = args.env ?? process.env;
  const log = args.log;

  const backgroundWork = new ClaudeBackgroundWorkObserver();
  const pauseGate = new PauseGate({
    deadlineAt: () => controlDeadlineAt(env),
    backgroundWorkProbe: () => backgroundWork.count(),
    log: (msg) => log('DEBUG', msg),
  });
  const controlAdapter = new ClaudeControlAdapter({
    log: (msg) => log('DEBUG', msg),
    pauseGate,
    backgroundWorkObserver: backgroundWork,
  });

  const controlStore = new ControlStateStore({
    generation: Number.parseInt(env.ADP_CONTROL_GENERATION || '1', 10) || 1,
    supportedActions: listenerActionsFor(controlAdapter),
    capabilityProvider: () => controlAdapter.capabilities(),
    revalidate: revalidateQueuedCommand,
  });
  bindRuntimeTransitionsToStore({ adapter: controlAdapter, store: controlStore, log });

  const steerQueue = new SteerQueue({
    store: controlStore,
    submitInput: (input) => controlAdapter.submitInput(input),
    atBoundary: () => controlAdapter.canAcceptInput() &&
      !pauseGate.isPauseActive() && pauseGate.activeToolCount() === 0,
    subscribe: (onBoundary) => {
      const unsubscribe = controlAdapter.subscribe((event) => {
        if (event.type === 'attempt_attached') controlAdapter.notifyWhenInputAccepted(onBoundary);
        onBoundary();
      });
      controlAdapter.notifyWhenInputAccepted(onBoundary);
      return unsubscribe;
    },
    onOutcome: (event) => args.onSteerOutcome?.(event),
    log,
  });

  const events = explanationsEnabled(env) ? new ExplanationEvents(env.ADP_CONTROL_RUN_ID || '', Number(env.ADP_CONTROL_GENERATION || '1')) : undefined;
  const listener = new ControlListener({
    events,
    bindAddress: env.ADP_CONTROL_BIND_ADDRESS || '',
    port: Number.parseInt(env.ADP_CONTROL_PORT || '0', 10),
    token: env.ADP_CONTROL_TOKEN || '',
    tokenExpiresAt: env.ADP_CONTROL_TOKEN_EXPIRES_AT || '',
    credentialFile: env.ADP_CONTROL_CREDENTIAL_FILE,
    generation: Number.parseInt(env.ADP_CONTROL_GENERATION || '1', 10) || 1,
    store: controlStore,
    executor: (action, commandId, reason, instruction) =>
      applyControlCommand({ action, commandId, reason, instruction, steerQueue,
        adapter: controlAdapter, store: controlStore, log }),
    runId: env.ADP_CONTROL_RUN_ID || '',
    envelopeKeys: parseVerificationKeys(env.ADP_CONTROL_ENVELOPE_KEYS),
    envelopeKeysFile: env.ADP_CONTROL_ENVELOPE_KEYS_FILE,
    logger: (level, message, context) => log(level.toUpperCase(), message, context),
  });

  const outcome = await listener.start(env);
  if (outcome.started) {
    if (!isAgentControlEnabled(env)) {
      steerQueue.dispose('read-only listener');
      return { runtime: null, listener, outcome, events };
    }
    return { runtime: { adapter: controlAdapter, gate: pauseGate, steerQueue,
      snapshot: () => controlStore.snapshot() }, listener, outcome, events };
  }
  // A listener that did not start means no command can ever arrive, so the
  // queue is disposed rather than left holding a runtime subscription for the
  // life of the run — same rule the inline version enforced.
  steerQueue.dispose('the control listener did not start; no instruction can be delivered');
  return { runtime: null, listener: null, outcome };
}
