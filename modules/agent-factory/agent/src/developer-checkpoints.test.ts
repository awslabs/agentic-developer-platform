import type { PostToolUseHookInput } from '@anthropic-ai/claude-agent-sdk';
import * as fs from 'fs';
import * as path from 'path';
import { createWorkerToolHooks, developerCheckpointGuidance } from './developer-checkpoints';

const interval = 15 * 60 * 1000;
const input: PostToolUseHookInput = {
  hook_event_name: 'PostToolUse', session_id: 'run-1', cwd: '/workspace',
  transcript_path: '/tmp/transcript', tool_name: 'Bash', tool_input: { command: 'git status' },
  tool_response: 'short result', tool_use_id: 'tool-1',
};

function worker(agentType = 'developer', spill = jest.fn(async () => '/tmp/spilled.txt')) {
  const hooks = createWorkerToolHooks({ agentType, store: { spill }, thresholdBytes: 100, log: jest.fn() });
  const callback = hooks.PostToolUse[0].hooks[0];
  return {
    spill,
    call: (event = input) => callback(event, event.tool_use_id, { signal: new AbortController().signal }),
  };
}

describe('developer checkpoint reminders at tool boundaries', () => {
  beforeEach(() => { jest.useFakeTimers(); jest.setSystemTime(0); });
  afterEach(() => jest.useRealTimers());

  it('waits 15 minutes and rate-limits concurrent and subsequent tool results', async () => {
    const { call } = worker();
    expect(await call()).toEqual({});
    jest.setSystemTime(interval - 1);
    expect(await call()).toEqual({});
    jest.setSystemTime(interval);
    const results = await Promise.all([call(), call(), call()]);
    expect(results.filter(result => 'hookSpecificOutput' in result)).toHaveLength(1);
    expect(results[0]).toMatchObject({ hookSpecificOutput: {
      hookEventName: 'PostToolUse', additionalContext: expect.stringContaining('checkpoint'),
    } });
    jest.setSystemTime(interval * 2 - 1);
    expect(await call()).toEqual({});
    jest.setSystemTime(interval * 2);
    expect(await call()).toHaveProperty('hookSpecificOutput.additionalContext');
  });

  it('emits one reminder after a long tool call, without replaying missed intervals', async () => {
    const { call } = worker();
    jest.setSystemTime(interval * 8);
    expect(await call()).toHaveProperty('hookSpecificOutput.additionalContext');
    expect(await call()).toEqual({});
  });

  it('does not share reminder state across runs', async () => {
    const first = worker();
    jest.setSystemTime(interval);
    expect(await first.call()).toHaveProperty('hookSpecificOutput.additionalContext');
    const second = worker();
    expect(await second.call()).toEqual({});
    jest.setSystemTime(interval * 2);
    expect(await second.call()).toHaveProperty('hookSpecificOutput.additionalContext');
  });

  it.each(['reviewer', 'operations', 'codex', 'architect', 'product', 'aidlc']) (
    'does not ask the %s persona to publish developer checkpoints', async (persona) => {
      const { call } = worker(persona);
      jest.setSystemTime(interval);
      expect(await call()).toEqual({});
      expect(developerCheckpointGuidance(persona)).toBe('');
    },
  );

  it('leaves checkpoint ownership with the main developer, not delegated workers', async () => {
    const { call } = worker();
    jest.setSystemTime(interval);
    expect(await call({ ...input, agent_id: 'delegated-task' })).toEqual({});
    expect(await call()).toHaveProperty('hookSpecificOutput.additionalContext');
  });

  it('preserves the spill locator when both hooks act on the same tool result', async () => {
    const { call, spill } = worker();
    jest.setSystemTime(interval);
    const result = await call({ ...input, tool_response: 'x'.repeat(6000) });
    expect(spill).toHaveBeenCalledTimes(1);
    expect(result).toMatchObject({ hookSpecificOutput: {
      hookEventName: 'PostToolUse',
      additionalContext: expect.stringContaining('checkpoint'),
      updatedToolOutput: expect.stringContaining('Locator: /tmp/spilled.txt'),
    } });
  });

  it('continues to remind when spilling fails, leaving the original output intact', async () => {
    const { call } = worker('developer', jest.fn(async () => { throw new Error('storage unavailable'); }));
    jest.setSystemTime(interval);
    const result = await call({ ...input, tool_response: 'x'.repeat(6000) });
    expect(result).toHaveProperty('hookSpecificOutput.additionalContext');
    expect(result).not.toHaveProperty('hookSpecificOutput.updatedToolOutput');
  });

  it('still spills other personas and developer output before a reminder is due', async () => {
    for (const persona of ['developer', 'reviewer']) {
      const { call } = worker(persona);
      const result = await call({ ...input, tool_response: 'x'.repeat(6000) });
      expect(result).toHaveProperty('hookSpecificOutput.updatedToolOutput');
      expect(result).not.toHaveProperty('hookSpecificOutput.additionalContext');
    }
  });
});

describe('checkpoint planning and worker integration', () => {
  it('supplies the strategy in the plan section and installs the composed hooks', () => {
    // agent-worker calls main() at import, so inspect the assembly site without starting a run.
    const source = fs.readFileSync(path.join(__dirname, 'agent-worker.ts'), 'utf8');
    const planStart = source.indexOf('### Step 2: Post Your Plan');
    const executeStart = source.indexOf('### Step 3: Execute Your Plan');
    expect(source.slice(planStart, executeStart)).toContain('${developerCheckpointGuidance(AGENT_TYPE)}');
    expect(source).toMatch(/hooks: createWorkerToolHooks\(\{\s+agentType: AGENT_TYPE,/);
  });

  it('requires verified publication and preserves read-only, approval and readiness boundaries', () => {
    const guidance = developerCheckpointGuidance('developer');
    for (const requirement of ['plan comment', '15-minute cadence', 'git ls-remote',
      'passed/failed/not run', 'never force-push', 'Do not create draft PRs',
      'branch/commit links', 'Continue the assignment', 'Read-only tasks', 'bypass AI-DLC approvals']) {
      expect(guidance).toContain(requirement);
    }
    const rules = path.resolve(__dirname, '../../rules');
    const persona = fs.readFileSync(path.join(rules, 'personas/developer.md'), 'utf8');
    const phase = fs.readFileSync(path.join(rules, 'phases/construction/code-generation.md'), 'utf8');
    expect(persona).toContain('checkpoint milestones');
    expect(phase).toContain('## Branch Checkpoint Strategy');
    expect(phase).toContain('Branch checkpoints do not trigger Steps 7–8.');
  });
});

describe('pause barrier hook registration', () => {
  // Regression for the review's B6. The barrier is a `PreToolUse` hook whose job is
  // to block for as long as an operator holds the pause — up to the full pause
  // budget. The CLI enforces hook timeouts in its own subprocess and applies a
  // default when a matcher omits one, so leaving `timeout` unset lets an
  // undocumented default decide whether pause works: if it is shorter than the
  // budget, the parked call is aborted, the gate reads that as a breached barrier,
  // and every long pause degrades to `unavailable` instead of pausing.

  const pauseHooks = (timeoutSeconds: number) => ({
    preToolUseTimeoutSeconds: timeoutSeconds,
    preToolUse: jest.fn(async () => ({})),
    postToolUse: jest.fn(async () => ({})),
    // Typed to accept its input so the registration test can assert what each stop
    // event actually delivered; a zero-arg mock records no arguments to check.
    onStop: jest.fn(async (_input?: unknown) => ({})),
    dispose: jest.fn(),
  });

  it('registers the barrier with the timeout the adapter asks for', () => {
    const hooks = createWorkerToolHooks({
      agentType: 'developer', store: { spill: jest.fn() }, thresholdBytes: 100, log: jest.fn(),
      pauseHooks: pauseHooks(1_860),
    });
    expect(hooks.PreToolUse?.[0].timeout).toBe(1_860);
  });

  it('allows the barrier to outlast the default pause budget', () => {
    // The number that actually matters: whatever the adapter derives must exceed the
    // 30-minute default budget, or the timeout fires first and the pause is lost.
    const { createClaudePauseHooks } = require('./harnesses/claude-control');
    const { PauseGate, DEFAULT_PAUSE_TIMEOUT_MS } = require('./pause-gate');
    const hooks = createWorkerToolHooks({
      agentType: 'developer', store: { spill: jest.fn() }, thresholdBytes: 100, log: jest.fn(),
      pauseHooks: createClaudePauseHooks(new PauseGate()),
    });
    const timeoutMs = (hooks.PreToolUse?.[0].timeout ?? 0) * 1000;
    expect(timeoutMs).toBeGreaterThan(DEFAULT_PAUSE_TIMEOUT_MS);
  });

  it('bounds developer commands without granting control or permission authority', async () => {
    // Command bounds do not grant tool permissions or install a pause gate.
    const hooks = createWorkerToolHooks({
      agentType: 'developer', store: { spill: jest.fn() }, thresholdBytes: 100, log: jest.fn(),
    });
    const result = await hooks.PreToolUse![0].hooks[0]({
      ...input, hook_event_name: 'PreToolUse',
    });
    expect(result).toMatchObject({ hookSpecificOutput: { updatedInput: { timeout: 120_000 } } });
    expect(result.hookSpecificOutput).not.toHaveProperty('permissionDecision');
    expect(hooks.Stop).toBeUndefined();
    const reviewer = createWorkerToolHooks({
      agentType: 'reviewer', store: { spill: jest.fn() }, thresholdBytes: 100, log: jest.fn(),
    });
    expect(reviewer.PreToolUse).toBeUndefined();
  });

  /**
   * Regression for the review's B8, and for how it survived: `grep -rn SubagentStop`
   * across the test tree returned nothing, so the branch registered that hook in
   * production and asserted nothing about it.
   *
   * `SubagentStop` carries a required `agent_id` and fires once per finishing
   * subagent, not at turn end. Registering it against a callback that settled a
   * session-global admission ledger meant one subagent finishing settled the main
   * thread's still-running tools, and a pause then confirmed with
   * `active_tool_count: 0` while a long `Bash` was mid-execution.
   *
   * Both stop events stay registered — the settle-on-missing-edge cleanup is real for
   * each scope — so what needs pinning is that the registration exists *and* that
   * both events reach a callback which scopes what it settles.
   */
  it('registers both stop events against the same scoping callback', async () => {
    const pause = pauseHooks(1_860);
    const hooks = createWorkerToolHooks({
      agentType: 'developer', store: { spill: jest.fn() }, thresholdBytes: 100, log: jest.fn(),
      pauseHooks: pause,
    });

    expect(hooks.Stop?.[0].hooks).toHaveLength(1);
    expect(hooks.SubagentStop?.[0].hooks).toHaveLength(1);

    // Each event must actually arrive with its payload intact: the scoping decision
    // is made from `agent_id`, so a registration that dropped the input would
    // reintroduce the session-global settle.
    await hooks.Stop![0].hooks[0]({ hook_event_name: 'Stop' } as never);
    await hooks.SubagentStop![0].hooks[0]({ hook_event_name: 'SubagentStop', agent_id: 'agent-1' } as never);

    expect(pause.onStop).toHaveBeenCalledTimes(2);
    expect(pause.onStop).toHaveBeenNthCalledWith(1, { hook_event_name: 'Stop' });
    expect(pause.onStop).toHaveBeenNthCalledWith(2, { hook_event_name: 'SubagentStop', agent_id: 'agent-1' });
  });
});

describe('developer command bounds', () => {
  it.each([[undefined, 120000], [5000, 5000], [900000, 600000]])('bounds Bash timeout %s', async (supplied, expected) => {
    const hooks = createWorkerToolHooks({agentType: 'developer', store: {spill: jest.fn()}, log: jest.fn()});
    const callback = hooks.PreToolUse![0].hooks[0];
    const result = await callback({...input, hook_event_name: 'PreToolUse', tool_input: {command: 'pytest tests', timeout: supplied}}, 'tool', {signal: new AbortController().signal});
    expect(result).toMatchObject({hookSpecificOutput: {hookEventName: 'PreToolUse', updatedInput: {command: 'pytest tests', timeout: expected}}});
  });
  it('preserves a pause denial instead of allowing the command', async () => {
    const denial = {hookSpecificOutput: {hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: 'cancelled'}};
    const pause = {preToolUse: jest.fn(async () => denial), preToolUseTimeoutSeconds: 400,
      postToolUse: jest.fn(), onStop: jest.fn(), dispose: jest.fn()};
    const hooks = createWorkerToolHooks({agentType: 'developer', store: {spill: jest.fn()}, log: jest.fn(), pauseHooks: pause});
    expect(hooks.PreToolUse![0].timeout).toBe(400);
    const result = await hooks.PreToolUse![0].hooks[0]({...input, hook_event_name: 'PreToolUse'}, 'tool', {signal: new AbortController().signal});
    expect(result).toEqual(denial);
  });
});
