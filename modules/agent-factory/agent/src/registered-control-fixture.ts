/** Authenticated fixture entrypoint using the production runtime and SDK wrapper. */
import { mkdirSync, writeFileSync, renameSync } from 'fs';
import { dirname, join } from 'path';
import { startControlRuntime } from './control-runtime-factory';
import { assistantText } from './reporting-text';
import { resilientQuery } from './utils/resilientQuery';
import { createWorkerToolHooks } from './developer-checkpoints';
import { TmpSpillStore } from './utils/spill';

type NativeHandle = { interrupt(): Promise<void>; close(): void };
type Event = { type: string; at: string; [key: string]: unknown };

export async function runRegisteredControlFixture(): Promise<number> {
  const mode = process.env.ADP_CONTROL_FIXTURE_MODE;
  const output = process.env.ADP_CONTROL_FIXTURE_OUTPUT;
  const invocation = process.env.ADP_MESSAGE_ID;
  if (!output || !invocation || invocation !== process.env.ADP_CONTROL_RUN_ID ||
      !['registered-control', 'native-interrupt'].includes(mode || '')) {
    throw new Error('authenticated fixture identity and mode are required');
  }
  const events: Event[] = [];
  let droppedEvents = 0;
  let sdkQueries = 0;
  let toolStarts = 0;
  let activeTools: number | null = 0;
  let countersComplete = true;
  let observationSequence = 0;
  // Private, local-only observations for before/after rejection probes. Count
  // admissions from the real pause gate; a tool cannot start without admission.
  // Do not include listener credentials, command bodies, prompts or SDK output.
  const snapshot = () => {
    const progress = {
      invocation_id: invocation, run_id: process.env.W2_FIXTURE_RUN_ID,
      source_revision: process.env.ADP_CONTROL_FIXTURE_SOURCE_REVISION,
      generation: Number(process.env.ADP_CONTROL_GENERATION),
      observed_at: new Date().toISOString(), sequence: ++observationSequence,
      sdk_queries: sdkQueries, tool_starts: toolStarts, active_tools: activeTools,
      counters_complete: countersComplete,
      dropped_events: droppedEvents,
    };
    writeFileSync(output + '.progress.tmp', JSON.stringify(progress), { mode: 0o600 });
    renameSync(output + '.progress.tmp', output + '.progress.json');
  };
  const record = (type: string, fields: Record<string, unknown> = {}) => {
    if (events.length === 2048) { events.shift(); droppedEvents++; }
    events.push({ type, at: new Date().toISOString(), ...fields });
    snapshot();
  };
  const cwd = join(dirname(output), 'workspace');
  mkdirSync(cwd, { recursive: true });
  const started = await startControlRuntime({
    log: () => {},
    onSteerOutcome: ({ commandId, outcome }) => record('steer_outcome', { command_id: commandId, outcome }),
  });
  if (!started.runtime || !started.listener || !started.outcome.started) {
    throw new Error('production control listener did not start');
  }
  const { runtime, listener } = started;
  let handle: NativeHandle | undefined;
  let nativeRequested = false;
  let nativeAcknowledged = false;
  let timedOut = false;
  let resultSeen = false;
  let sdkFailure = false;
  let exitCode = 1;
  const unsubscribe = runtime.adapter.subscribe(event => {
    if (event.type === 'active_work') {
      if (event.count === null || activeTools === null) countersComplete = false;
      else toolStarts += Math.max(0, event.count - activeTools);
      activeTools = event.count;
    }
    record('runtime_' + event.type, {
      attempt_id: event.attemptId,
      ...(event.type === 'active_work' ? { count: event.count } : {}),
      ...(event.type === 'input_handoff' ? { command_id: event.command_id, outcome: event.result } : {}),
      ...(event.type === 'terminal' ? { outcome: event.outcome } : {}),
    });
  });
  record('listener_started', { generation: Number(process.env.ADP_CONTROL_GENERATION) });
  const deadline = setTimeout(() => {
    timedOut = true;
    record('fixture_deadline');
    try { handle?.close(); } catch (error) {
      record('deadline_close_failed', { error_name: error instanceof Error ? error.name : 'unknown' });
    }
  }, 900_000);
  try {
    const prompt = mode === 'native-interrupt'
      ? 'This is an authorized disposable control fixture. Run Bash sleep 30, then report completion. Do not run background commands.'
      : started.events
        ? 'This is an authorized disposable streaming fixture. First explain to the reader that a bounded history supports reconnects, choosing bounded memory over complete live replay. Include the marker STREAM-MECHANISM. Then run foreground Bash sleep 20. Next explain that seeing two live messages proves incremental delivery but not durable cross-pod replay; include STREAM-EVIDENCE. Then run foreground Bash sleep 20 and finish. Do not combine both explanations in one message. Do not use background work.'
      : 'This is an authorized disposable control fixture. Repeatedly use separate foreground Bash calls to sleep 2 seconds, then use Write to update progress.txt with the iteration number. Continue for 100 iterations unless a later user instruction changes the task. Keep all files within this working directory. Do not combine the loop into one Bash call or use background work.';
    const attach = runtime.adapter.onAttemptHandle();
    for await (const message of resilientQuery({
      queryParams: { prompt, options: {
        cwd, model: process.env.ANTHROPIC_MODEL || 'claude-opus-4-6',
        permissionMode: 'bypassPermissions', allowDangerouslySkipPermissions: true,
        allowedTools: ['Bash', 'Read', 'Write', 'Edit'], settingSources: [],
        persistSession: true, maxTurns: 300,
      } },
      maxRetries: 0,
      idleTimeoutMs: 120_000,
      attemptInputFactory: runtime.adapter.attemptInputFactory(pauseHooks => ({
        hooks: createWorkerToolHooks({ agentType: 'developer', store: new TmpSpillStore(cwd), pauseHooks }),
      })),
      beforeOutput: () => runtime.gate.waitForOutput(),
      cancellation: runtime.adapter.cancellationSource(),
      idleSuspended: () => runtime.gate.isPauseActive(),
      onAttemptHandle: async attempt => {
        sdkQueries++;
        await attach(attempt);
        handle = attempt.session as NativeHandle;
        record('sdk_attempt_attached', { attempt: attempt.attemptNumber });
      },
      log: () => {},
    })) {
      const event = message as unknown as Record<string, unknown>;
      record('sdk_message', { message_type: event.type, subtype: event.subtype });
      if (event.type === 'assistant') {
        const content = (event.message as { content?: Array<{ type?: unknown; text?: unknown }> } | undefined)?.content;
        if (Array.isArray(content)) started.events?.publish(assistantText(content));
      }
      if (event.type === 'assistant' && mode === 'native-interrupt' && !nativeRequested) {
        if (!handle || typeof handle.interrupt !== 'function') throw new Error('SDK interruption unavailable');
        nativeRequested = true;
        record('native_interrupt_requested', { active_turn_observed: true });
        await handle.interrupt();
        nativeAcknowledged = true;
        record('native_interrupt_acknowledged');
      }
      if (event.type === 'result') {
        resultSeen = true;
        sdkFailure = event.is_error === true || event.subtype !== 'success';
        record('sdk_result', { subtype: event.subtype, is_error: event.is_error });
        break;
      }
    }
    exitCode = !timedOut && resultSeen && !sdkFailure ? 0 : 1;
    if (mode === 'native-interrupt' && !nativeAcknowledged) exitCode = 1;
  } catch (error) {
    record('sdk_exception', { error_name: error instanceof Error ? error.name : 'unknown' });
    exitCode = 1;
  } finally {
    clearTimeout(deadline);
    const cleanupErrors: string[] = [];
    for (const cleanup of [
      () => runtime.steerQueue.dispose('fixture execution ended'),
      () => runtime.adapter.dispose(),
      () => listener.stop(),
      unsubscribe,
    ]) {
      try { await cleanup(); } catch (error) {
        cleanupErrors.push(error instanceof Error ? error.name : 'unknown');
        exitCode = 1;
      }
    }
    record('runtime_disposed');
    const report = {
      mode, invocation_id: invocation, run_id: process.env.W2_FIXTURE_RUN_ID,
      source_revision: process.env.ADP_CONTROL_FIXTURE_SOURCE_REVISION,
      generation: Number(process.env.ADP_CONTROL_GENERATION),
      native_requested: nativeRequested, native_acknowledged: nativeAcknowledged,
      result_seen: resultSeen, timed_out: timedOut, exit_code: exitCode,
      dropped_events: droppedEvents, cleanup_errors: cleanupErrors, events,
      counters: { sdk_queries: sdkQueries, tool_starts: toolStarts, active_tools: activeTools,
        counters_complete: countersComplete },
    };
    writeFileSync(output + '.tmp', JSON.stringify(report, null, 2), { mode: 0o600 });
    renameSync(output + '.tmp', output);
  }
  return exitCode;
}

if (require.main === module) {
  runRegisteredControlFixture().then(code => { process.exitCode = code; }).catch(() => {
    console.error('Registered control fixture failed before producing complete evidence');
    process.exitCode = 1;
  });
}
