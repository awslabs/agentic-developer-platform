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
