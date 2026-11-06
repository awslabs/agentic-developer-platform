jest.mock('node:child_process', () => ({ execFile: jest.fn() }));
jest.mock('@anthropic-ai/claude-agent-sdk', () => ({
  createSdkMcpServer: jest.fn(({ tools }) => ({ tools })),
  tool: jest.fn((name, _description, _schema, handler) => ({ name, handler })),
}));

import { execFile } from 'node:child_process';
import { invokeOperationsStoryTool, operationsStoryTools } from './operations-story-tools';

beforeEach(() => jest.useFakeTimers());
afterEach(() => { jest.useRealTimers(); jest.clearAllMocks(); });

function broker(results: unknown[]) {
  const inputs: string[] = [];
  (execFile as unknown as jest.Mock).mockImplementation((_file, _args, _options, callback) => {
    const result = results.length > 1 ? results.shift() : results[0];
    callback(result instanceof Error ? result : null, result instanceof Error ? '' : JSON.stringify(result));
    return { stdin: { end: (value: string) => inputs.push(value) } };
  });
  return inputs;
}

const run = (status: string) => ({ run: { id: 42, run_attempt: 1, status, conclusion: status === 'completed' ? 'failure' : null } });
const repair = (ready: boolean) => ({ repair: { head: 'a'.repeat(40), ready, merged: false, status: ready ? 'success' : 'pending' } });

it('keeps five minutes of unchanged workflow observations inside one tool call', async () => {
  const inputs = broker([run('queued')]);
  const pending = invokeOperationsStoryTool('actions_wait', { run_id: 42 });
  await jest.advanceTimersByTimeAsync(299999);
  expect(execFile).toHaveBeenCalledTimes(10);
  await jest.advanceTimersByTimeAsync(1);
  expect(JSON.parse(await pending).run.status).toBe('queued');
  expect(execFile).toHaveBeenCalledTimes(11);
  expect(inputs).toEqual(Array(11).fill('{"run_id":42}'));
  expect((execFile as unknown as jest.Mock).mock.calls.every(call =>
    call[1].join(' ') === '-m lib.actions_broker actions_run')).toBe(true);
  expect(jest.getTimerCount()).toBe(0);
});

it.each(['in_progress', 'completed'])('returns when workflow status changes to %s', async status => {
  broker([run('queued'), run('queued'), run(status)]);
  const pending = invokeOperationsStoryTool('actions_wait', { run_id: 42 });
  await jest.advanceTimersByTimeAsync(60000);
  expect(JSON.parse(await pending).run.status).toBe(status);
  expect(execFile).toHaveBeenCalledTimes(3);
  expect(jest.getTimerCount()).toBe(0);
});

it('returns a completed failed workflow immediately for the agent to diagnose', async () => {
  broker([run('completed')]);
  expect(JSON.parse(await invokeOperationsStoryTool('actions_wait', { run_id: 42 })).run.conclusion).toBe('failure');
  expect(execFile).toHaveBeenCalledTimes(1);
  expect(jest.getTimerCount()).toBe(0);
});

it('stops waiting on fresh authorization refusal rather than reusing a prior result', async () => {
  broker([run('queued'), { error: 'mediated operation refused (HTTP 403)', error_code: 'MediatedRefused' }]);
  const pending = invokeOperationsStoryTool('actions_wait', { run_id: 42 });
  await jest.advanceTimersByTimeAsync(30000);
  expect(JSON.parse(await pending).error_code).toBe('MediatedRefused');
  expect(execFile).toHaveBeenCalledTimes(2);
  expect(jest.getTimerCount()).toBe(0);
});

it('waits for repair checks through the same agent tools and returns readiness', async () => {
  const inputs = broker([repair(false), repair(false), repair(true)]);
  const pending = invokeOperationsStoryTool('repair_wait', { pull_number: 7 });
  await jest.advanceTimersByTimeAsync(60000);
  expect(JSON.parse(await pending).repair.ready).toBe(true);
  expect(inputs).toEqual(Array(3).fill('{"pull_number":7}'));
  expect((execFile as unknown as jest.Mock).mock.calls.every(call =>
    call[1].join(' ') === '-m lib.operations_repair repair_status')).toBe(true);
});

it.each([{ run_id: 42, seconds: 0 }, { run_id: 42, seconds: 301 }, { run_id: -1 }, { run_id: 42, workflow: 'other.yml' }])(
  'refuses invalid wait scope and unbounded delay before invoking anything: %j', async args => {
    await expect(invokeOperationsStoryTool('actions_wait', args)).rejects.toThrow();
    expect(execFile).not.toHaveBeenCalled();
    expect(jest.getTimerCount()).toBe(0);
  },
);

function callTool(name: string, args: object) {
  const server = operationsStoryTools().mcpServers['pipeline-actions'] as unknown as {
    tools: { name: string; handler: (args: object) => Promise<{ isError: boolean; content: { text: string }[] }> }[];
  };
  return server.tools.find(tool => tool.name === name)!.handler(args);
}

it('carries a large-file read cursor and guarded exact replacement through the operations broker', async () => {
  const digest = 'a'.repeat(64);
  const inputs = broker([{ content: 'value = 1', sha256: digest, next_offset: 8192 }, { sha256: 'b'.repeat(64) }]);
  const read = { path: 'src/large.py', offset: 4096, expected_sha256: digest };
  const edit = { path: 'src/large.py', old_text: 'value = 1', new_text: 'value = 2', expected_sha256: digest };
  expect((await callTool('repair_read', read)).isError).toBe(false);
  expect((await callTool('repair_replace', edit)).isError).toBe(false);
  expect(inputs).toEqual([JSON.stringify(read), JSON.stringify(edit)]);
  expect((execFile as unknown as jest.Mock).mock.calls.map(call => call[1])).toEqual([
    ['-m', 'lib.operations_repair', 'repair_read'], ['-m', 'lib.operations_repair', 'repair_replace'],
  ]);
});

const board = (merged: boolean) => ({ operations_tasks: { cycles: [{ evidence: [
  { kind: 'operations_repair_pr', pull_number: 7, head: 'a'.repeat(40) },
  { kind: 'operations_repair_merge', pull_number: 7, head: (merged ? 'a' : 'b').repeat(40) },
] }] } });

it('keeps a rejected merge on the repair path and refuses dispatch of the unmerged head', async () => {
  broker([{ merge: { merged: false, reason: 'checks_or_branch_not_ready' } }, board(false)]);
  const merge = await callTool('repair_merge', { pull_number: 7, expected_head: 'a'.repeat(40) });
  expect(merge.isError).toBe(true);
  expect(JSON.parse(merge.content[0].text).next_action).toContain('repair_wait');
  const dispatch = await callTool('actions_dispatch', { workflow: 'deploy.yml' });
  expect(dispatch.isError).toBe(true);
  expect(JSON.parse(dispatch.content[0].text).pending_repairs).toEqual([{ pull_number: 7, head: 'a'.repeat(40) }]);
  expect((execFile as unknown as jest.Mock).mock.calls.map(call => call[1][2])).toEqual(['repair_merge', 'repair_tasks']);
});

it('preserves an actionable gateway refusal instead of replacing it with a merge retry instruction', async () => {
  const refusal = { error: 'repair_scope_not_accepted: Ask the flow owner to accept a plan amendment and reapprove invalidated limits.' };
  broker([refusal]);
  const result = await callTool('repair_merge', { pull_number: 7, expected_head: 'a'.repeat(40) });
  expect(result.isError).toBe(true);
  expect(JSON.parse(result.content[0].text)).toEqual(refusal);
});

it('lets the agent dispatch through the existing broker after the exact repair merge is recorded', async () => {
  broker([board(true), { dispatched: true, workflow: 'deploy.yml' }]);
  const dispatch = await callTool('actions_dispatch', { workflow: 'deploy.yml' });
  expect(dispatch.isError).toBe(false);
  expect(JSON.parse(dispatch.content[0].text).dispatched).toBe(true);
  expect((execFile as unknown as jest.Mock).mock.calls.map(call => call[1][2])).toEqual(['repair_tasks', 'actions_dispatch']);
});

it('returns a recoverable tool error to the agent if transport fails without assuming whether a mutation happened', async () => {
  broker([new Error('sensitive provider details')]);
  const result = await callTool('repair_merge', { pull_number: 7, expected_head: 'a'.repeat(40) });
  expect(result.isError).toBe(true);
  expect(result.content[0].text).toContain('before retrying');
  expect(result.content[0].text).not.toContain('sensitive provider details');
});
