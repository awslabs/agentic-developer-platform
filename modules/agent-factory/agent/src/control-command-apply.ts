/**
 * Applying control commands and mirroring gate transitions — Issue #3961 (S2).
 *
 * Split out of `agent-worker.ts` for one reason: testability. These two functions
 * are the entire operator-visible contract of a pause — the mapping from a gate
 * outcome to what the dashboard and the journal say — and `agent-worker.ts` cannot
 * be imported from a unit test, because loading it pulls the Claude SDK's ESM entry
 * point into Jest's CommonJS transform and the suite fails to parse. So the code
 * that most needs tests sat in the one module that could not have them.
 *
 * Nothing here reaches into the SDK: both functions take the adapter and the store
 * as structural types, which is what lets this module be imported directly.
 */
import { abortSentinelBindingFromEnv, writeAbortSentinel } from './control-abort-sentinel';
import type { ControlAction, ControlStateStore } from './control-state';
import type { ClaudeControlAdapter } from './harnesses/claude-control';
import type { SteerQueue } from './steer-queue';

/**
 * Default abort recorder: bind the sentinel to this run and write it — #3963.
 *
 * Returns `false` when this run has no control binding, which is also the only
 * state in which an abort could not have been authorized in the first place
 * (the binding comes from a successful control registration). The caller treats
 * `false` as "stopped but not reported as aborted" rather than assuming success.
 */
function writeAbortSentinelForRun(input: {
  commandId: string;
  envelope?: string | null;
  signedBodyBase64?: string | null;
  abortReceipt?: string | null;
}): boolean {
  const binding = abortSentinelBindingFromEnv();
  if (!binding) return false;
  return writeAbortSentinel({
    binding,
    commandId: input.commandId,
    envelope: input.envelope,
    signedBodyBase64: input.signedBodyBase64,
    abortReceipt: input.abortReceipt,
  });
}

/**
 * Mirror attempt-filtered runtime transitions into the state store — Issue #3961.
 *
 * `applyControlCommand` covers every transition an operator *asked* for. Two
 * transitions happen with no command behind them:
 *
 * - the barrier is overridden by the harness mid-park (`pause_unavailable`), and
 * - a pause budget expires and the run auto-resumes (`pause_released`, expired).
 *
 * Both reopen admission. Without this subscriber the store keeps whatever phase the
 * last command set — so the gateway, and the operator reading it, are told the run
 * is `paused` while tools are running again. That is the precise false-`Paused`
 * claim this story exists to make impossible, so it is mirrored at the same moment
 * the gate changes admission rather than at the next command.
 *
 * `pause_confirmed` is mirrored for the same reason: since a late-settling tool can
 * now confirm a pause that earlier reported `requested`, that confirmation also
 * arrives without a command.
 *
 * Exported and parameterised so the store/gate agreement is testable without a
 * running agent, matching {@link applyControlCommand}.
 */
export function bindRuntimeTransitionsToStore(args: {
  adapter: Pick<ClaudeControlAdapter, 'subscribe' | 'activeWorkCount' | 'currentAttempt' | 'isCancelled'>;
  store: Pick<ControlStateStore, 'settle' | 'setPhase' | 'snapshot' | 'setActiveToolCount' | 'annotateDelivered'>;
  log?: (level: string, message: string, context?: Record<string, unknown>) => void;
}): () => void {
  const { adapter, store } = args;
  const log = args.log ?? (() => {});

  // Unknown while no attempt is attached. The adapter supplies the count after
  // attachment; all later work/pause events carry that attempt's identity.
  store.setActiveToolCount(adapter.activeWorkCount());
  let currentAttempt = adapter.currentAttempt();

  /** Settle whichever pause command is still awaiting an outcome, if any. */
  const settlePendingPause = (status: 'applied' | 'rejected', reason: string) => {
    for (const pending of store.snapshot().commands.filter((command) => command.status === 'delivered')) {
      if (pending.action === 'pause') store.settle(pending.command_id, status, reason);
    }
  };

  return adapter.subscribe((event) => {
    // A cancelled run is over, and every transition below reports a *live* run:
    // three of them set the phase to `running` and one to `paused`. Issue #3963
    // makes that reachable and wrong. `attempt_detached` is emitted structurally
    // — the registry passes it even for a superseded attempt — and abort's
    // teardown always produces one, so without this guard the `attempt_detached`
    // arm would overwrite `abort_requested` with `running` microseconds after the
    // operator's abort was recorded, and the dashboard would show an aborting run
    // as healthy right up until the process exited.
    //
    // Checked on the adapter rather than the store's phase because cancellation
    // is the runtime's own synchronous fact, whereas the phase is a projection
    // this very subscriber writes.
    if (adapter.isCancelled()) return;
    if (event.type === 'attempt_attached') {
      currentAttempt = event.attemptId;
      store.setPhase('running');
      store.setActiveToolCount(adapter.activeWorkCount());
      return;
    }
    if (event.attemptId !== currentAttempt) return;
    switch (event.type) {
      case 'attempt_detached':
        currentAttempt = null;
        store.setPhase('running');
        store.setActiveToolCount(null);
        settlePendingPause('rejected', 'the accepting attempt ended; same-execution resume is unavailable');
        return;
      case 'pause_requested':
        store.setPhase('pause_requested');
        for (const command of store.snapshot().commands) {
          if (command.action === 'pause') store.annotateDelivered(command.command_id, 'waiting for admitted tools, output or background work to settle');
        }
        return;
      case 'active_work':
        // Report the barrier's own count, and only the barrier's: `0` here is a
        // quiescence claim and the gate is the only thing entitled to make it.
        store.setActiveToolCount(event.count);
        return;
      case 'pause_waiting':
        for (const command of store.snapshot().commands) {
          if (command.action === 'pause') store.annotateDelivered(command.command_id, event.reason);
        }
        return;
      case 'pause_confirmed':
        store.setPhase('paused');
        settlePendingPause('applied', 'no new tool action can start');
        log('INFO', 'control: pause confirmed', {});
        return;
      case 'pause_unavailable':
        // Back to `running` because that is the truth — admission was reopened, so
        // the run is not paused and must not be displayed as pausing.
        store.setPhase('running');
        settlePendingPause('rejected', event.reason);
        log('WARN', 'control: pause unavailable', { detail: event.reason });
        return;
      case 'pause_released':
        // Unconditional, for both an expiry and an operator resume. It would be
        // tempting to skip the operator case on the grounds that
        // `applyControlCommand` already records it, but the gate can also be resumed
        // through the adapter's `releasePause()` without any command behind it — and
        // then nothing else would return the store to `running`. Every release
        // reopens admission, so every release must say so; the redundant write on
        // the command path is idempotent and costs nothing.
        //
        // Journal settlement deliberately stays with the command path: the gate
        // knows admission reopened, but only the command path knows *which* command
        // the operator is waiting on an answer for.
        store.setPhase('running');
        if (event.expired) log('WARN', 'control: pause expired, run resumed automatically', {});
        return;
      default:
        // Includes `terminal`, which nothing emits today. A handler for it would
        // be untestable through the real runtime and would claim coverage of an
        // edge that cannot occur; the abort path sets its own phase directly in
        // `applyControlCommand` instead. Add one here when an emitter exists.
        return;
    }
  });
}

export async function applyControlCommand(args: {
  action: ControlAction;
  commandId: string;
  adapter: Pick<ClaudeControlAdapter, 'requestPause' | 'resumeFromPause' | 'cancel'>;
  store: Pick<ControlStateStore, 'settle' | 'setPhase' | 'snapshot' | 'lookup' | 'annotateDelivered' | 'authorizationProof' | 'signedRequestBody' | 'abortAcceptanceReceipt'>;
  log?: (level: string, message: string, context?: Record<string, unknown>) => void;
  /**
   * Records the abort so the finalizing Python half can report it — Issue #3963.
   *
   * Injected rather than imported directly so the abort path is testable without
   * writing to a real `/tmp`, and so a test can assert what happens when the
   * record does not land. Defaults to the real writer.
   */
  recordAbort?: (input: {
    commandId: string;
    envelope?: string | null;
    signedBodyBase64?: string | null;
    abortReceipt?: string | null;
  }) => boolean;
  /**
   * Operator-supplied reason, already bounded by the listener.
   *
   * Used for pause annotations and logging. Deliberately **not** forwarded into
   * the abort record: an abort's reason reaches the operator-facing comment, and
   * that text must come from the bytes the gateway signed rather than from a
   * parameter this process could have chosen. See
   * `AbortSentinel.signed_body_base64`.
   */
  reason?: string | null;
  /**
   * The operator's steering text — Issue #3965.
   *
   * Separate from `reason` because it has the opposite trust handling: `reason`
   * is quoted back to a human, `instruction` crosses into a model prompt and is
   * wrapped as untrusted input by the queue below. `null` for every other verb.
   */
  instruction?: string | null;
  /**
   * The run's steering delivery pump — Issue #3965.
   *
   * Absent for a run with no queue (which is every run before this story, and
   * every run with the control flag off), in which case `steer` falls through to
   * the no-executor rejection below rather than being accepted and forgotten.
   *
   * Structurally typed for the same reason as `adapter` and `store`: this module
   * is imported by unit tests and must not pull the Claude SDK in behind it.
   */
  steerQueue?: Pick<SteerQueue, 'enqueue'>;
}): Promise<void> {
  const { action, commandId, adapter, store } = args;
  const log = args.log ?? (() => {});

  // Capture only commands handed off before this one. A later queued pause is
  // a new intent and must not be cancelled by an earlier resume's completion.
  const preceding = store.snapshot().commands;
  const commandIndex = preceding.findIndex((command) => command.command_id === commandId);
  const earlierPauses = preceding.slice(0, commandIndex).filter((command) =>
    command.action === 'pause' && command.status === 'delivered');
  const isLatestDelivered = () => {
    const commands = store.snapshot().commands;
    const index = commands.findIndex((command) => command.command_id === commandId);
    return index >= 0 && !commands.slice(index + 1).some((command) => command.delivered_at !== null);
  };

  if (action === 'resume') {
    await adapter.resumeFromPause();
    // Settle any pause still awaiting confirmation as `cancelled`, not
    // `applied`: an operator who paused and changed their mind before the barrier
    // settled did not get a pause, and the journal is the record they will read
    // back. `cancelled` also distinguishes this from a pause the gate refused.
    for (const pending of earlierPauses) {
      if (pending.action === 'pause' && pending.command_id !== commandId) {
        store.settle(pending.command_id, 'cancelled', 'resumed before the pause was confirmed');
      }
    }
    if (isLatestDelivered()) store.setPhase('running');
    store.settle(commandId, 'applied', 'run resumed');
    log('INFO', 'control: run resumed', { command_id: commandId });
    return;
  }

  if (action === 'abort') {
    // Reaching here already means this abort was authorized: the listener routes
    // proof-bearing verbs through `deliverAuthorized`, which re-verifies the
    // gateway envelope against the live authority immediately before handoff and
    // settles the command `rejected` if that check fails. So the executor's job
    // is to stop the run, not to decide whether it may be stopped.
    //
    // Order matters here, and it is the reverse of what reads naturally. The
    // record is written FIRST, before anything stops, because it is the only
    // thing that makes this a *reported* abort rather than an unexplained exit.
    // `adapter.cancel()` tears down the attempt; once that has happened the
    // process is heading for teardown and a later write may not get the chance
    // to run. An abort whose record never landed would finalize by exit code —
    // i.e. as a crash — which is the mislabelling this story exists to remove.
    // The gateway's signature over this exact command, read while the command is
    // still `delivered` — i.e. before the settlement below. It is copied into the
    // record so the finalizing process can distinguish an abort the gateway
    // authorized from a file that merely appeared at the sentinel path: the agent
    // runs with `Bash`, so it can write that file, but it cannot sign an envelope
    // — the signing key exists only in the gateway. Without this the record would
    // be a self-assertion, and honouring it would mean deleting a live run's
    // queue message on the strength of a claim the run made about itself.
    // The envelope AND the bytes it was signed over, both read while the command is
    // still `delivered`. The envelope proves the gateway authorized an abort of
    // this run; the bytes are what let the finalizer prove the *reason* it prints
    // is the operator's. Without the bytes the finalizer holds a signed digest with
    // no preimage, which is how a fabricated reason came to be attributed to a
    // human under an otherwise-valid envelope.
    //
    // `args.reason` is deliberately NOT passed. The finalizer reads the operator's
    // words out of the signed bytes, so handing it a second, unsigned copy of the
    // same text would recreate the field that made the substitution possible.
    // The gateway's signed receipt for this acceptance, read while the command is
    // still `delivered`. This — not the envelope, and not any field this process
    // writes — is what proves to the finalizer that the abort was accepted by a live
    // run rather than merely requested at some point (#3963 review finding 1). The
    // envelope is still carried because it is what binds the operator's reason text
    // to a signature; the receipt is what makes the acceptance checkable.
    const recorded = (args.recordAbort ?? writeAbortSentinelForRun)({
      commandId,
      envelope: store.authorizationProof(commandId),
      signedBodyBase64: store.signedRequestBody(commandId),
      abortReceipt: store.abortAcceptanceReceipt(commandId),
    });

    // The phase the dashboard shows while the run winds down. Set before the
    // cancellation so an operator watching sees the abort take effect
    // immediately, rather than a run that still claims to be running until the
    // process dies.
    store.setPhase('abort_requested');

    // Cancel commands that were accepted but never executed. They were queued
    // for a run that is now stopping, and handing a steer or a pause to an
    // aborting run would either apply an instruction nobody can act on or
    // re-close a barrier that is about to be torn down. `cancelled` rather than
    // `rejected`: the run did not refuse them, it stopped before reaching them.
    for (const queued of store.snapshot().commands) {
      if (queued.command_id === commandId) continue;
      if (queued.status === 'pending' || queued.status === 'delivered') {
        store.settle(queued.command_id, 'cancelled', 'run aborted before this command was applied');
      }
    }

    // The single stop. This cancels the attempt registry, which (a) fires the
    // typed `ControlCancelledError` that `resilientQuery` checks *before* any
    // error-text classification, so a deliberate abort cannot be mistaken for a
    // retryable fault and silently restarted, and (b) transitively cancels the
    // pause gate, which DENIES held work rather than flushing it. Both matter:
    // flushing would run the very side effects the operator aborted to prevent,
    // and a held pause is released without an auto-resume annotation, so a
    // paused run reaches the same ending as an active one.
    adapter.cancel('run aborted by operator');

    // Truthful settlement. An abort whose record did not land still stopped the
    // run — the cancellation above is unconditional — but it will not be
    // *reported* as an abort, because the finalizer falls back to exit-code
    // classification. Saying so in the journal is the honest outcome; claiming
    // `applied` would tell an operator the outcome was recorded when it was not.
    if (recorded) {
      store.settle(commandId, 'applied', 'run aborted; no further work will start');
      log('INFO', 'control: run aborted', { command_id: commandId });
    } else {
      store.settle(
        commandId,
        'unknown',
        'run aborted, but the terminal outcome could not be recorded for finalization',
      );
      log('ERROR', 'control: run aborted but the abort record did not land', { command_id: commandId });
    }
    return;
  }

  if (action === 'steer') {
    // Issue #3965. Note what this arm does *not* do: it does not settle the
    // command, and it does not touch the journal on the success path. The whole
    // point of steering is that acceptance and delivery are separated in time, so
    // the command deliberately stays `pending` from here until the pump reaches a
    // boundary the harness can take input at — and the pump owns every transition
    // out of `pending`, including the authority re-check that must happen
    // immediately before the physical handoff rather than now.
    //
    // A run with no queue is the one case this arm settles, and it settles as
    // `rejected` rather than leaving the command pending forever: the verb was
    // advertised (the capability intersection said yes) but nothing in this
    // process can deliver it, which is a build-level mismatch the operator should
    // see as a refusal rather than as an instruction still in flight.
    if (!args.steerQueue) {
      store.settle(commandId, 'rejected', 'this run has no steering queue attached');
      log('WARN', 'control: steer accepted with no queue attached', { command_id: commandId });
      return;
    }
    // `enqueue` settles the command itself when it refuses the text (absent,
    // empty or over the bound), so there is nothing to record here on `false`.
    if (args.steerQueue.enqueue(commandId, args.instruction ?? '')) {
      log('INFO', 'control: steering instruction accepted, awaiting a handoff boundary',
        { command_id: commandId });
    }
    return;
  }

  if (action !== 'pause') {
    // Unreachable through the listener, which refuses an unsupported verb with a
    // 501 before it ever reaches an executor. Handled anyway, and as a rejection
    // rather than a throw, so that widening `SUPPORTED_ACTIONS` without teaching
    // this function the new verb produces a clear rejected command instead of an
    // accepted one that silently does nothing.
    store.settle(commandId, 'rejected', `no executor implements ${action}`);
    return;
  }

  const result = await adapter.requestPause();
  // A resume may have cancelled this command while quiescence was pending.
  // Its late result must not overwrite either that journal outcome or a new pause.
  if (store.lookup(commandId).status !== 'delivered') return;
  if (result.outcome === 'confirmed') {
    if (isLatestDelivered()) store.setPhase('paused');
    store.settle(commandId, 'applied', 'no new tool action can start');
    log('INFO', 'control: pause confirmed', { command_id: commandId });
    return;
  }
  if (result.outcome === 'requested') {
    // Phase only — the command stays pending. See the doc comment above.
    if (isLatestDelivered()) store.setPhase('pause_requested');
    const reason = result.reason ?? 'waiting for admitted work to reach a safe boundary';
    store.annotateDelivered(commandId, reason);
    log('INFO', 'control: pause requested, awaiting quiescence', { command_id: commandId, detail: reason });
    return;
  }
  // `unavailable`. The phase goes back to `running` because that is the truth:
  // admission was reopened (or never closed), so the run is not paused and must
  // not be displayed as pausing.
  if (isLatestDelivered()) store.setPhase('running');
  store.settle(commandId, 'rejected', result.reason);
  log('WARN', 'control: pause unavailable', { command_id: commandId, detail: result.reason });
}
