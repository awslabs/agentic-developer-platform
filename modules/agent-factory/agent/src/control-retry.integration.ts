/** Focused paid SDK acceptance for steering across retry and cancellation in backoff. */
import { mkdtempSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { ClaudeControlAdapter, ClaudeBackgroundWorkObserver } from './harnesses/claude-control';
import { PauseGate } from './pause-gate';
import { ControlStateStore, fingerprintPayload } from './control-state';
import { IMPLEMENTED_CONTROL_VERBS } from './control-runtime';
import { SteerQueue } from './steer-queue';
import { resilientQuery } from './utils/resilientQuery';
import { injectReadFailure } from './control-runtime-fault-probe';

const ids = ['confirmed', 'ambiguous', 'pending'].map((_, n) => `00000000-0000-4000-8000-00000000910${n}`);

export async function steeringRetry() {
  const cwd = mkdtempSync(join(tmpdir(), 'adp-retry-acceptance-'));
  const gate = new PauseGate({ backgroundWorkProbe: () => 0 });
  const adapter = new ClaudeControlAdapter({ pauseGate: gate, backgroundWorkObserver: new ClaudeBackgroundWorkObserver() });
  // This experiment measures queue/SDK mechanics. Authorization is covered by
  // the separate live gateway matrix, not by this controlled local revalidator.
  const store = new ControlStateStore({ generation: 1, supportedActions: IMPLEMENTED_CONTROL_VERBS, revalidate: async () => true });
  const inputs: Array<{ attempt: number; command_id: string }> = [];
  const attempts: string[] = [], sessions: Array<{ attempt: number; session: string }> = [];
  const factories: Array<{ attempt: number; resume: boolean }> = [];
  let number = 0, failure = false, inject = false, capturedSession = false;
  let terminal: string | null = null, error: string | null = null, errorDetail: string | null = null;
  const submit = adapter.submitInput.bind(adapter);
  const queue = new SteerQueue({
    store,
    atBoundary: () => adapter.canAcceptInput() && (number === 2 || !inject),
    subscribe: listener => {
      const unsubscribe = adapter.subscribe(event => {
        if (event.type === 'attempt_attached') adapter.notifyWhenInputAccepted(listener);
        listener();
      });
      adapter.notifyWhenInputAccepted(listener);
      return unsubscribe;
    },
    submitInput: async input => {
      const result = await submit(input);
      if (input.command_id === ids[1] && result === 'delivered') {
        // Lose only the handoff acknowledgement, after the real transport push.
        // The journal must settle unknown and must never retry this command.
        inject = true;
        return 'unknown';
      }
      return result;
    },
  });
  for (const [n, command_id] of ids.entries()) {
    const instruction = `Reply RETRY_${n}_RECEIVED, then wait for the next user instruction. Do not use tools.`;
    const payload = { command_id, instruction };
    const accepted = store.submit('steer', command_id, fingerprintPayload(payload), {
      envelope: 'adpe1.local-retry-fixture', action: 'steer', body_base64: 'e30=',
      principal: 'authorized-retry-fixture', authorityKind: 'human_session', command_id,
    });
    if (accepted.kind !== 'accepted' || !queue.enqueue(command_id, instruction)) throw new Error('fixture queue admission failed');
  }
  const attach = adapter.onAttemptHandle(), factory = adapter.attemptInputFactory();
  const timer = setTimeout(() => adapter.cancel('retry acceptance deadline'), 180_000);
  try {
    for await (const message of resilientQuery({
      queryParams: { prompt: 'Reply READY and wait for further user instructions. Do not use tools.', options: {
        cwd, permissionMode: 'bypassPermissions', allowDangerouslySkipPermissions: true,
        persistSession: true, settingSources: [], maxTurns: 12,
      } },
      maxRetries: 1, baseDelayMs: 1000, idleTimeoutMs: 60_000,
      cancellation: adapter.cancellationSource(),
      onSessionId: () => { capturedSession = true; },
      attemptInputFactory: context => {
        factories.push({ attempt: context.attemptNumber, resume: context.isResume });
        const channel = factory(context), iterator = channel.input[Symbol.asyncIterator]();
        const observed: AsyncIterableIterator<unknown> = {
          [Symbol.asyncIterator]() { return this; },
          async next() {
            const item = await iterator.next();
            if (!item.done) for (const command_id of ids) if (JSON.stringify(item.value).includes(command_id)) {
              inputs.push({ attempt: context.attemptNumber, command_id });
            }
            return item;
          },
          ...(iterator.return ? { return: iterator.return.bind(iterator) } : {}),
        };
        return { ...channel, input: observed };
      },
      onAttemptHandle: async handle => {
        number = handle.attemptNumber; await attach(handle);
        attempts.push(adapter.currentAttempt()!);
        if (number === 1) injectReadFailure(handle.session as AsyncIterable<unknown>,
          () => inject && capturedSession, () => { failure = true; });
      },
      log: () => {},
    })) {
      const value = message as unknown as { type: string; subtype?: string; session_id?: string };
      if (value.session_id) sessions.push({ attempt: number, session: value.session_id });
      if (number === 2 && value.type === 'result' && inputs.some(x => x.command_id === ids[2])) {
        terminal = value.subtype ?? null; break;
      }
    }
  } catch (caught) { error = caught instanceof Error ? caught.name : 'unknown'; errorDetail = caught instanceof Error ? caught.message.slice(0, 500) : null; }
  finally { clearTimeout(timer); queue.dispose('retry experiment finished'); await adapter.dispose(); rmSync(cwd, { recursive: true, force: true }); }
  const first = sessions.find(x => x.attempt === 1)?.session;
  const second = sessions.find(x => x.attempt === 2)?.session;
  return {
    queued_command_id: ids[2], deliveries_of_queued_command: inputs.filter(x => x.command_id === ids[2]).length,
    confirmed_initial_handoffs: inputs.filter(x => x.command_id === ids[0] && x.attempt === 1).length,
    confirmed_handoffs_replayed: inputs.filter(x => x.command_id === ids[0] && x.attempt > 1).length,
    session_preserved: !!first && first === second && factories.some(x => x.attempt === 2 && x.resume),
    attempt_id_before: attempts[0] ?? null, attempt_id_after: attempts[1] ?? null,
    ambiguous_handoff_outcome: store.lookup(ids[1])?.status ?? null,
    ambiguous_handoffs_replayed: inputs.filter(x => x.command_id === ids[1] && x.attempt > 1).length,
    injected_failure_observed: failure, inputs, attempts, factories, sessions, terminal, error, error_detail: errorDetail,
    authorization_scope: 'controlled local revalidator; not gateway authorization acceptance',
  };
}

export async function abortDuringRetry() {
  const cwd = mkdtempSync(join(tmpdir(), 'adp-abort-retry-'));
  const gate = new PauseGate({ backgroundWorkProbe: () => 0 });
  const adapter = new ClaudeControlAdapter({ pauseGate: gate, backgroundWorkObserver: new ClaudeBackgroundWorkObserver() });
  const attach = adapter.onAttemptHandle();
  let attempts = 0, assistant = false, injected = false, backoff = false, cancelled = false, error: string | null = null;
  const timer = setTimeout(() => adapter.cancel('abort retry acceptance deadline'), 120_000);
  try {
    for await (const message of resilientQuery({
      queryParams: { prompt: 'Reply READY. Do not use tools.', options: { cwd, settingSources: [], maxTurns: 2 } },
      attemptInputFactory: adapter.attemptInputFactory(), cancellation: adapter.cancellationSource(),
      maxRetries: 1, baseDelayMs: 5000,
      onAttemptHandle: async handle => {
        attempts++; await attach(handle);
        injectReadFailure(handle.session as AsyncIterable<unknown>, () => assistant, () => { injected = true; });
      },
      log: message => {
        if (message.includes('Retrying in')) {
          backoff = true;
          queueMicrotask(() => { cancelled = true; adapter.cancel('operator abort during retry backoff'); });
        }
      },
    })) if (message.type === 'assistant') assistant = true;
  } catch (caught) { error = caught instanceof Error ? caught.name : 'unknown'; }
  finally { clearTimeout(timer); await adapter.dispose(); rmSync(cwd, { recursive: true, force: true }); }
  return { attempts, injected, backoff, cancelled, error,
    abort_during_retry_started_next_attempt: backoff && cancelled ? attempts > 1 : null };
}

export async function main() {
  if (process.env.ADP_CONTROL_RETRY_EVAL !== 'true' || !process.env.ADP_CONTROL_FIXTURE_OUTPUT || !process.env.ADP_MESSAGE_ID) {
    throw new Error('explicit authenticated retry fixture required');
  }
  const steering = await steeringRetry(), abort = await abortDuringRetry();
  const passed = steering.injected_failure_observed && steering.deliveries_of_queued_command === 1 &&
    steering.confirmed_initial_handoffs === 1 && steering.confirmed_handoffs_replayed === 0 && steering.ambiguous_handoffs_replayed === 0 &&
    steering.session_preserved && steering.ambiguous_handoff_outcome === 'unknown' &&
    steering.attempt_id_before !== steering.attempt_id_after && steering.terminal === 'success' &&
    abort.injected && abort.abort_during_retry_started_next_attempt === false;
  writeFileSync(process.env.ADP_CONTROL_FIXTURE_OUTPUT, JSON.stringify({
    mode: process.env.ADP_CONTROL_FIXTURE_MODE, run_id: process.env.W2_FIXTURE_RUN_ID,
    generation: Number(process.env.ADP_CONTROL_GENERATION),
    invocation_id: process.env.ADP_MESSAGE_ID, source_revision: process.env.ADP_CONTROL_FIXTURE_SOURCE_REVISION,
    observed_at: new Date().toISOString(), steering_retry: { ...steering,
      abort_during_retry_started_next_attempt: abort.abort_during_retry_started_next_attempt }, abort_retry: abort, passed,
  }, null, 2), { mode: 0o600 });
  return passed ? 0 : 1;
}
if (require.main === module) main().then(code => { process.exitCode = code; }).catch(error => { console.error(error.name); process.exitCode = 1; });
