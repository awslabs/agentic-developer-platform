jest.mock('@anthropic-ai/claude-agent-sdk', () => ({ query: jest.fn() }));

import { query } from '@anthropic-ai/claude-agent-sdk';
import { ClaudeControlAdapter } from './harnesses/claude-control';
import { PauseGate } from './pause-gate';
import { ControlStateStore } from './control-state';
import { applyControlCommand, bindRuntimeTransitionsToStore } from './control-command-apply';
import { resilientQuery } from './utils/resilientQuery';

const flush = () => new Promise<void>(resolve => setImmediate(resolve));

function submit(store: ControlStateStore, id: string) {
  expect(store.submit('pause', id, id).kind).toBe('accepted');
  expect(store.markDelivered(id)).toBe(true);
}

it.each(['requested', 'confirmed'] as const)('retrying away from a %s pause ends that pause before the next query runs', async phase => {
  const gate = new PauseGate({ settleTimeoutMs: 5 });
  const adapter = new ClaudeControlAdapter({ pauseGate: gate });
  const store = new ControlStateStore({ generation: 1, supportedActions: new Set(['pause', 'resume']) });
  const unbind = bindRuntimeTransitionsToStore({ adapter, store });
  let fail!: () => void;
  const failureReady = new Promise<void>(resolve => { fail = resolve; });
  let replaced!: () => void;
  const replacement = new Promise<void>(resolve => { replaced = resolve; });
  let attemptNumber = 0;
  (query as jest.Mock).mockImplementation(() => Object.assign((async function* () {
    const current = ++attemptNumber;
    yield { type: 'system', subtype: 'init', session_id: `session-${current}` };
    if (current === 1) { await failureReady; throw new Error('503 Service Unavailable'); }
    yield { type: 'result', subtype: 'success' };
  })(), { close: jest.fn() }));
  const attach = adapter.onAttemptHandle();
  const stream = resilientQuery({
    queryParams: { prompt: 'task', options: {} }, maxRetries: 1, baseDelayMs: 1,
    attemptInputFactory: adapter.attemptInputFactory(() => ({})),
    onAttemptHandle: async handle => { await attach(handle); if (handle.attemptNumber > 1) replaced(); },
    cancellation: adapter.cancellationSource(), beforeOutput: () => gate.waitForOutput(),
  });
  await stream.next();
  const originalAttempt = adapter.currentAttempt();
  const ticket = phase === 'requested' ? (await gate.admit('Read')).ticket : undefined;
  submit(store, 'pause-a');
  await applyControlCommand({ action: 'pause', commandId: 'pause-a', adapter, store });
  expect(gate.currentPhase()).toBe(phase === 'requested' ? 'pause_requested' : 'paused');
  let next: Promise<IteratorResult<unknown>> | undefined;
  let watchdog: ReturnType<typeof setTimeout> | undefined;
  try {
    next = stream.next();
    fail();
    await Promise.race([replacement, new Promise((_, reject) => { watchdog = setTimeout(() => reject(new Error('replacement did not start')), 1500); })]);
    clearTimeout(watchdog);
    expect(adapter.currentAttempt()).not.toBe(originalAttempt);
    expect(gate.currentPhase()).toBe('running');
    expect(store.snapshot().state).toBe('running');
    if (phase === 'requested') expect(store.lookup('pause-a').status).toBe('rejected');
    gate.settle(ticket);
    await flush();
    expect(store.snapshot().state).toBe('running');
  } finally {
    clearTimeout(watchdog);
    await gate.resume();
    gate.settle(ticket);
    await next;
    await stream.return(undefined as never);
    unbind();
    await adapter.dispose();
  }
});

it('a repeated pending pause keeps the original gate and journal despite a nonpositive new budget', async () => {
  const gate = new PauseGate({ settleTimeoutMs: 5 });
  const adapter = new ClaudeControlAdapter({ pauseGate: gate });
  const store = new ControlStateStore({ generation: 1, supportedActions: new Set(['pause', 'resume']) });
  const unbind = bindRuntimeTransitionsToStore({ adapter, store });
  const attempt = adapter.attemptInputFactory()({ attemptNumber: 1, isResume: false, promptText: 'task' });
  await adapter.onAttemptHandle()({ attemptNumber: 1, session: { close() {} } });
  const ticket = (await gate.admit('Read')).ticket;
  try {
    submit(store, 'pause-a');
    await applyControlCommand({ action: 'pause', commandId: 'pause-a', adapter, store });
    submit(store, 'pause-b');
    const repeated = await adapter.requestPause({ timeoutMs: 0 });
    expect(repeated.outcome).toBe('requested');
    expect(gate.currentPhase()).toBe('pause_requested');
    expect(store.snapshot().state).toBe('pause_requested');
    expect(store.lookup('pause-a').status).toBe('delivered');
    expect(store.lookup('pause-b').status).toBe('delivered');
    gate.settle(ticket);
    await flush();
    expect(store.snapshot().state).toBe('paused');
    expect(store.lookup('pause-a').status).toBe('applied');
    expect(store.lookup('pause-b').status).toBe('applied');
  } finally {
    await gate.resume();
    gate.settle(ticket);
    await attempt.dispose();
    unbind();
    await adapter.dispose();
  }
});

it('attempt disposal denies its parked tool and late hooks cannot alter a replacement pause', async () => {
  const gate = new PauseGate();
  const adapter = new ClaudeControlAdapter({ pauseGate: gate });
  const store = new ControlStateStore({ generation: 1, supportedActions: new Set(['pause', 'resume']) });
  const unbind = bindRuntimeTransitionsToStore({ adapter, store });
  const hooks: import('./harnesses/claude-control').ClaudePauseHooks[] = [];
  const factory = adapter.attemptInputFactory(value => { hooks.push(value); return {}; });
  const first = factory({ attemptNumber: 1, isResume: false, promptText: 'task' });
  await adapter.onAttemptHandle()({ attemptNumber: 1, session: { close() {} } });
  await adapter.requestPause();
  const parked = hooks[0].preToolUse({ hook_event_name: 'PreToolUse', tool_name: 'Write', tool_use_id: 'old' } as never, 'old', { signal: new AbortController().signal });
  await flush();
  await first.dispose();
  expect((await parked as any).hookSpecificOutput.permissionDecision).toBe('deny');
  expect(gate.currentPhase()).toBe('running');
  const second = factory({ attemptNumber: 2, isResume: true, promptText: '' });
  await adapter.onAttemptHandle()({ attemptNumber: 2, session: { close() {} } });
  try {
    expect((await adapter.requestPause()).outcome).toBe('confirmed');
    await hooks[0].postToolUse({ hook_event_name: 'PostToolUse', tool_use_id: 'old' } as never);
    await hooks[0].onStop({ hook_event_name: 'Stop', background_tasks: [] } as never);
    expect(gate.currentPhase()).toBe('paused');
    expect(store.snapshot().state).toBe('paused');
  } finally {
    await second.dispose();
    unbind();
    await adapter.dispose();
  }
});

it('a pause queued just before disposal cannot close admission for the replacement', async () => {
  const gate = new PauseGate();
  const adapter = new ClaudeControlAdapter({ pauseGate: gate });
  const first = adapter.attemptInputFactory()({ attemptNumber: 1, isResume: false, promptText: '' });
  await adapter.onAttemptHandle()({ attemptNumber: 1, session: { close() {} } });
  const pause = adapter.requestPause();
  await first.dispose();
  expect((await pause).outcome).toBe('unavailable');
  expect(gate.currentPhase()).toBe('running');
  await adapter.dispose();
});
