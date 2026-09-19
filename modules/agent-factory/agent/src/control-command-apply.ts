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
import type { ControlAction, ControlStateStore } from './control-state';
import type { ClaudeControlAdapter } from './harnesses/claude-control';

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
  adapter: Pick<ClaudeControlAdapter, 'subscribe' | 'activeWorkCount' | 'currentAttempt'>;
  store: Pick<ControlStateStore, 'settle' | 'setPhase' | 'snapshot' | 'setActiveToolCount'>;
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
        return;
      case 'active_work':
        // Report the barrier's own count, and only the barrier's: `0` here is a
        // quiescence claim and the gate is the only thing entitled to make it.
        store.setActiveToolCount(event.count);
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
        return;
    }
  });
}

export async function applyControlCommand(args: {
  action: ControlAction;
  commandId: string;
  adapter: Pick<ClaudeControlAdapter, 'requestPause' | 'resumeFromPause'>;
  store: Pick<ControlStateStore, 'settle' | 'setPhase' | 'snapshot' | 'lookup'>;
  log?: (level: string, message: string, context?: Record<string, unknown>) => void;
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
    log('INFO', 'control: pause requested, awaiting quiescence', { command_id: commandId });
    return;
  }
  // `unavailable`. The phase goes back to `running` because that is the truth:
  // admission was reopened (or never closed), so the run is not paused and must
  // not be displayed as pausing.
  if (isLatestDelivered()) store.setPhase('running');
  store.settle(commandId, 'rejected', result.reason);
  log('WARN', 'control: pause unavailable', { command_id: commandId, detail: result.reason });
}
