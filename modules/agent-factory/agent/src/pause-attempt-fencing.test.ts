jest.mock('@anthropic-ai/claude-agent-sdk', () => ({ query: jest.fn() }));

import { query } from '@anthropic-ai/claude-agent-sdk';
import { ClaudeControlAdapter } from './harnesses/claude-control';
import { PauseGate } from './pause-gate';
import { ControlStateStore } from './control-state';
import { applyControlCommand, bindRuntimeTransitionsToStore } from './control-command-apply';
import { resilientQuery } from './utils/resilientQuery';

const flush = () => new Promise<void>(resolve => setImmediate(resolve));

it.each(['running-tool', 'unobservable-background'])('publishes the %s pause blocker while the command remains delivered', async blocker => {
  const gate = new PauseGate({ settleTimeoutMs: 5, backgroundWorkProbe: () => blocker === 'unobservable-background' ? null : 0 });
  const adapter = new ClaudeControlAdapter({ pauseGate: gate });
  const store = new ControlStateStore({ generation: 1, supportedActions: new Set(['pause', 'resume']) });
  const unbind = bindRuntimeTransitionsToStore({ adapter, store });
  const attempt = adapter.attemptInputFactory()({ attemptNumber: 1, isResume: false, promptText: '' });
  await adapter.onAttemptHandle()({ attemptNumber: 1, session: { close() {} } });
  const ticket = blocker === 'running-tool' ? (await gate.admit('Read')).ticket : undefined;
  try {
    submit(store, 'pause-reason');
    await applyControlCommand({ action: 'pause', commandId: 'pause-reason', adapter, store });
    const command = store.snapshot().commands.find(command => command.command_id === 'pause-reason');
    expect(command?.status).toBe('delivered');
    expect(command?.reason).toMatch(blocker === 'running-tool' ? /admitted/ : /not observable/);
  } finally { await gate.resume(); gate.settle(ticket); await attempt.dispose(); unbind(); await adapter.dispose(); }
});

it('settles output exactly once before confirming a pending pause, without counting it as a tool', async () => {
  const gate = new PauseGate({ settleTimeoutMs: 5 });
  const admission = await gate.waitForOutput();
  expect(admission).not.toBe(false);
  try {
    expect(gate.activeToolCount()).toBe(0);
    expect(await gate.requestPause()).toMatchObject({ outcome: 'requested', reason: expect.stringContaining('admitted output') });
    if (admission) { admission.release(); admission.release(); }
    await flush();
    expect(gate.currentPhase()).toBe('paused');
  } finally { await gate.resume(); }
});

it('pending diagnostics cannot overwrite a command after resume has cancelled it', () => {
  const store = new ControlStateStore({ generation: 1, supportedActions: new Set(['pause', 'resume']) });
  submit(store, 'pause');
  expect(store.annotateDelivered('pause', 'a'.repeat(2048))).toBe(true);
  expect(store.snapshot().commands[0].reason).toHaveLength(1024);
  store.settle('pause', 'cancelled', 'resumed');
  expect(store.annotateDelivered('pause', 'late blocker')).toBe(false);
  expect(store.annotateDelivered('missing', 'blocker')).toBe(false);
  expect(store.snapshot().commands[0].reason).toBe('resumed');
});

it('updates the blocker when a completed tool leaves unobservable background work', async () => {
  let background: number | null = 0;
  const gate = new PauseGate({ settleTimeoutMs: 5, backgroundWorkProbe: () => background });
  const adapter = new ClaudeControlAdapter({ pauseGate: gate });
  const store = new ControlStateStore({ generation: 1, supportedActions: new Set(['pause', 'resume']) });
  const unbind = bindRuntimeTransitionsToStore({ adapter, store });
  const attempt = adapter.attemptInputFactory()({ attemptNumber: 1, isResume: false, promptText: '' });
  await adapter.onAttemptHandle()({ attemptNumber: 1, session: { close() {} } });
  const ticket = (await gate.admit('Bash')).ticket;
  try {
    submit(store, 'pause');
    await applyControlCommand({ action: 'pause', commandId: 'pause', adapter, store });
    expect(store.snapshot().commands[0].reason).toContain('admitted tool');
    background = null;
    gate.settle(ticket);
    await flush();
    expect(store.snapshot().commands[0]).toMatchObject({ status: 'delivered', reason: expect.stringContaining('not observable') });
  } finally { await gate.resume(); gate.settle(ticket); await attempt.dispose(); unbind(); await adapter.dispose(); }
});

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
  const reading = stream.next();
  const ticket = phase === 'requested' ? (await gate.admit('Read')).ticket : undefined;
  submit(store, 'pause-a');
  await applyControlCommand({ action: 'pause', commandId: 'pause-a', adapter, store });
  expect(gate.currentPhase()).toBe(phase === 'requested' ? 'pause_requested' : 'paused');
  let next: Promise<IteratorResult<unknown>> | undefined;
  let watchdog: ReturnType<typeof setTimeout> | undefined;
  try {
    next = reading;
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

it('neutral attempt invalidation refuses parked admissions without issuing tickets', async () => {
  const gate = new PauseGate();
  await gate.requestPause();
  const parked = gate.admit('Write');
  expect(gate.heldCount()).toBe(1);
  gate.invalidateAttempt();
  expect(await parked).toMatchObject({ decision: 'deny' });
  expect(gate.activeToolCount()).toBe(0);
  expect(gate.heldCount()).toBe(0);
  gate.cancel();
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
