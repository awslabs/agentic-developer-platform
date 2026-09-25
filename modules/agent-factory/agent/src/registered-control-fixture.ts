/** Authenticated fixture entrypoint using the production runtime and SDK wrapper. */
import { mkdirSync, writeFileSync, renameSync } from 'fs';
import { dirname, join } from 'path';
import { startControlRuntime } from './control-runtime-factory';
import { assistantText } from './reporting-text';
import { ExplanationEvents, HISTORY_BYTES, HISTORY_EVENTS, MAX_SUBSCRIBERS } from './explanation-events';
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
  const authored = new ExplanationEvents(invocation, Number(process.env.ADP_CONTROL_GENERATION));
  const events: Event[] = [];
  let droppedEvents = 0;
  let sdkQueries = 0;
  // Final private evidence, separate from the credential/body-free progress file.
  const sdkInputs: unknown[] = [];
  let inputCaptureComplete = true;
  let attemptDisposals = 0;
  let queryCloses = 0;
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
        ? `This task checks incremental delivery of technical explanations. Read ${join(__dirname, 'explanation-events.ts')} and ${join(__dirname, 'explanation-events.test.ts')} from this pinned source checkout first, then explain the implementation in your own words. These are the expected limits to verify: modules/agent-factory/agent/src/explanation-events.ts retains at most ${HISTORY_EVENTS} events and ${HISTORY_BYTES} encoded bytes, permits ${MAX_SUBSCRIBERS} subscribers, and replay(cursor) emits a reset for expired, foreign or future cursors. Explain the bounded-memory versus complete-history tradeoff. Reference that code path and modules/agent-factory/agent/src/explanation-events.test.ts. Give the reproducible command: cd modules/agent-factory/agent && npm test -- --runInBand src/explanation-events.test.ts. Do not claim you ran it; this fixture only records the command for a reviewer. Include STREAM-MECHANISM. Do not install dependencies. If the source files are unavailable, report that limitation and do not claim source verification. Run foreground Bash sleep 20 so the browser can observe the first explanation before the second. Next explain why reset means a history gap rather than invented continuity, and why incremental delivery does not establish durable cross-pod replay; include STREAM-EVIDENCE. Then run foreground Bash sleep 20 and finish. Do not combine both explanations in one message. Do not use background work.`
      : 'Validate foreground tool execution in this disposable directory. Perform exactly three steps in order. For each step, use Bash to run sleep 60 with timeout 90000, wait for that foreground call to finish, then use Write to put the step number in progress.txt. Use separate calls; never background the command or combine the steps into a shell loop. A later user instruction may change the task. Keep all files within this working directory.';
    const attach = runtime.adapter.onAttemptHandle();
    const inputFactory = runtime.adapter.attemptInputFactory(pauseHooks => ({
      hooks: createWorkerToolHooks({ agentType: 'developer', store: new TmpSpillStore(cwd), pauseHooks }),
    }));
    for await (const message of resilientQuery({
      queryParams: { prompt, options: {
        cwd, model: process.env.ANTHROPIC_MODEL || 'claude-opus-4-6',
        permissionMode: 'bypassPermissions', allowDangerouslySkipPermissions: true,
        allowedTools: ['Bash', 'Read', 'Write', 'Edit'], settingSources: [],
        persistSession: true, maxTurns: 300,
      } },
      maxRetries: 0,
      idleTimeoutMs: 120_000,
      attemptInputFactory: context => {
        const input = inputFactory(context);
        const iterator = input.input[Symbol.asyncIterator]();
        const observed: AsyncIterableIterator<unknown> = {
          [Symbol.asyncIterator]() { return this; },
          async next() {
            const item = await iterator.next();
            if (!item.done) {
              if (sdkInputs.length < 32) sdkInputs.push(item.value);
              else inputCaptureComplete = false;
            }
            return item;
          },
          ...(iterator.return ? { return: iterator.return.bind(iterator) } : {}),
        };
        return { ...input, input: observed, dispose: async () => {
          await input.dispose(); attemptDisposals++;
        } };
      },
      beforeOutput: () => runtime.gate.waitForOutput(),
      cancellation: runtime.adapter.cancellationSource(),
      idleSuspended: () => runtime.gate.isPauseActive(),
      onAttemptHandle: async attempt => {
        sdkQueries++;
        await attach(attempt);
        handle = attempt.session as NativeHandle;
        const close = handle.close.bind(handle);
        handle.close = () => { close(); queryCloses++; };
        record('sdk_attempt_attached', { attempt: attempt.attemptNumber });
      },
      log: () => {},
    })) {
      const event = message as unknown as Record<string, unknown>;
      record('sdk_message', { message_type: event.type, subtype: event.subtype });
      if (event.type === 'assistant') {
        const content = (event.message as { content?: Array<{ type?: unknown; text?: unknown }> } | undefined)?.content;
        if (Array.isArray(content)) {
          const text = assistantText(content);
          authored.publish(text);
          started.events?.publish(text);
        }
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
    if (mode === 'registered-control' && toolStarts === 0) {
      record('fixture_no_tool_execution');
      exitCode = 1;
    }
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
      authored_explanations: authored.replay().events,
      sdk_input_messages: sdkInputs, input_capture_complete: inputCaptureComplete,
      attempt_disposals: attemptDisposals, query_close_calls: queryCloses,
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
