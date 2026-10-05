import * as fs from 'fs';
import { createCodexDeveloperReporter, publicDeveloperText } from './codex-developer-reporting';
import { createCodexPersonaReporter } from './codex-persona-reporting';
import { startControlRuntime } from './control-runtime-factory';

jest.mock('./worker-activity-log', () => ({ createWorkerActivityLog: () => ({ start: async () => {}, log: jest.fn(), flush: async () => {} }) }));
jest.mock('./control-runtime-factory', () => ({ startControlRuntime: jest.fn() }));
jest.mock('node:fs', () => ({ readFileSync: jest.fn(() => '{}'), writeFileSync: jest.fn() }));
jest.mock('fs', () => ({ ...jest.requireActual('fs'), writeFileSync: jest.fn(), renameSync: jest.fn() }));

const fetchMock = jest.fn();
const events = { publish: jest.fn(), finish: jest.fn() };
const stop = jest.fn();
const context = { repository: 'acme/repository', issue: 42, model: 'openai.gpt-6-sol' };

beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
  process.env.ADP_MESSAGE_ID = 'invocation-42';
  process.env.GH_TOKEN = 'private-test-token-value';
  process.env.CHECK_RUN_ID = '321';
  global.fetch = fetchMock;
  fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: 123 }), text: async () => '' });
  (startControlRuntime as jest.Mock).mockResolvedValue({ events, listener: { stop }, outcome: { started: true } });
});
afterEach(() => { jest.useRealTimers(); delete process.env.ADP_MESSAGE_ID; delete process.env.GH_TOKEN; delete process.env.CHECK_RUN_ID; });

test('posts a live issue comment, streams commands and explanations, then publishes the PR outcome and transcript', async () => {
  const reporter = await createCodexDeveloperReporter(context);
  reporter.session('codex-session');
  reporter.explanation('I reproduced the failure and am repairing the parser.');
  reporter.activity('Running: npm test');
  await jest.advanceTimersByTimeAsync(5000);
  const calls = () => fetchMock.mock.calls.map(([url, init]) => ({ url, method: init.method, body: JSON.parse(init.body) }));
  expect(calls().some(c => c.method === 'POST' && c.url.endsWith('/issues/42/comments'))).toBe(true);
  expect(calls().some(c => c.method === 'PATCH' && c.url.endsWith('/issues/comments/123') && c.body.body.includes('Running: npm test'))).toBe(true);
  expect(calls().some(c => c.url.endsWith('/check-runs/321') && c.body.output.text.includes('repairing the parser'))).toBe(true);
  expect(calls().some(c => c.url.endsWith('/check-runs/321') && c.body.output.text.includes('Running: npm test'))).toBe(true);
  expect(events.publish).toHaveBeenCalledWith('I reproduced the failure and am repairing the parser.');
  await reporter.finish({ summary: 'Tests passed and the implementation is ready.', prUrl: 'https://github.com/acme/repository/pull/9' });
  expect(calls().some(c => c.body.body?.includes('https://github.com/acme/repository/pull/9'))).toBe(true);
  expect(fs.writeFileSync).toHaveBeenCalledWith('/tmp/adp-run-transcript.md.tmp', expect.stringContaining('repairing the parser'), 'utf8');
  expect(events.finish).toHaveBeenCalledTimes(1);
  expect(stop).toHaveBeenCalledTimes(1);
});

test('failure updates the same issue comment and closes the live stream without claiming success', async () => {
  const reporter = await createCodexDeveloperReporter(context);
  await reporter.fail(new Error('Tests did not pass'));
  const bodies = fetchMock.mock.calls.map(([, init]) => JSON.parse(init.body).body || '').join('\n');
  expect(bodies).toContain('Tests did not pass');
  expect(events.finish).toHaveBeenCalledTimes(1);
  expect(stop).toHaveBeenCalledTimes(1);
});

test('requires a genuine invocation and redacts credentials from public updates', async () => {
  expect(publicDeveloperText('key=private-test-token-value')).not.toContain('private-test-token-value');
  delete process.env.ADP_MESSAGE_ID;
  await expect(createCodexDeveloperReporter(context)).rejects.toThrow('dispatched ADP invocation');
  expect(fetchMock).not.toHaveBeenCalled();
});

test('a failed final publication can still be reported as failure', async () => {
  const reporter = await createCodexDeveloperReporter(context);
  fetchMock.mockResolvedValueOnce({ ok: false, status: 503, text: async () => 'unavailable' });
  await expect(reporter.finish({ summary: 'Implementation finished', prUrl: 'https://github.com/acme/repository/pull/9' })).rejects.toThrow('Comment update failed');
  await reporter.fail(new Error('Could not publish the final outcome'));
  expect(fetchMock.mock.calls.some(([, init]) => JSON.parse(init.body).body?.includes('Could not publish the final outcome'))).toBe(true);
});

test('reviewer uses its own persona and publishes tool activity to the live UI stream', async () => {
  const reporter = await createCodexDeveloperReporter({ ...context, persona: 'agent-codex-reviewer' });
  reporter.activity('Running: python3 -m unittest');
  expect(events.publish).toHaveBeenCalledWith('Running: python3 -m unittest');
  await reporter.finish({ summary: 'Reviewer finished: merged' });
  const bodies = fetchMock.mock.calls.map(([, init]) => JSON.stringify(JSON.parse(init.body))).join('\n');
  expect(bodies).toContain('agent-codex-reviewer');
  expect(bodies).toContain('Reviewer finished: merged');
  expect(bodies).not.toContain('Pull request published');
});

test('reviewer controller operations obey pause admission and release the gate on failure', async () => {
  const { PauseGate } = jest.requireActual('./pause-gate');
  const gate = new PauseGate({ defaultTimeoutMs: 10000, settleTimeoutMs: 100 });
  const cancellation = new AbortController();
  const adapter = { signal: cancellation.signal, socket: '/fixture', start: jest.fn(), dispose: jest.fn() };
  (startControlRuntime as jest.Mock).mockResolvedValue({ events, listener: { stop }, outcome: { started: true },
    runtime: { adapter, gate, steerQueue: { flush: async () => {}, dispose: jest.fn() } } });
  const reporter = await createCodexDeveloperReporter({ ...context, persona: 'agent-codex-reviewer' });
  await gate.requestPause({ timeoutMs: 10000 });
  const effect = jest.fn(async () => { throw new Error('Git failed'); });
  const operation = reporter.control!.operation(effect);
  const rejected = expect(operation).rejects.toThrow('Git failed');
  await Promise.resolve();
  expect(effect).not.toHaveBeenCalled();
  await gate.resume();
  await rejected;
  expect(effect).toHaveBeenCalledTimes(1);
  expect(gate.activeToolCount()).toBe(0);
  cancellation.abort(new Error('Operator aborted'));
  await expect(reporter.control!.operation(effect)).rejects.toThrow('Operator aborted');
  expect(effect).toHaveBeenCalledTimes(1);
  await reporter.fail(new Error('Operator aborted'));
});

test('architect reports its own persona, audit progress and design PR', async () => {
  const reporter = await createCodexDeveloperReporter({ ...context, persona: 'agent-codex-architect' });
  reporter.activity('Running: git ls-files');
  await reporter.finish({ summary: 'Design documented in docs/design.md', prUrl: 'https://github.com/acme/repository/pull/7' });
  const bodies = fetchMock.mock.calls.map(([, init]) => JSON.stringify(JSON.parse(init.body))).join('\n');
  expect(bodies).toContain('agent-codex-architect');
  expect(bodies).toContain('Architecture assessment run');
  expect(bodies).toContain('docs/design.md');
  expect(bodies).toContain('https://github.com/acme/repository/pull/7');
  expect(bodies).not.toContain('Reading the issue and developing the change');
});


test.each(['developer', 'architect', 'reviewer'] as const)('%s keeps the current authored explanation in the live comment and transcript', async persona => {
  const reporter = await createCodexDeveloperReporter({ ...context, persona });
  reporter.progress!('I traced the deployment order and found a missing dependency.', { id: 'message-1', category: 'message', state: 'completed' });
  reporter.progress!('Running: git ls-files', { id: 'tool-1', category: 'tool', state: 'running' });
  await jest.advanceTimersByTimeAsync(5000);
  const comments = fetchMock.mock.calls.map(([, init]) => JSON.parse(init.body).body || '');
  expect(comments.some(body => body.includes('### Agent explanation') && body.includes('I traced the deployment order'))).toBe(true);
  await reporter.finish({ summary: 'Design ready.', prUrl: 'https://github.com/acme/repository/pull/9' });
  expect(fs.writeFileSync).toHaveBeenCalledWith('/tmp/adp-run-transcript.md.tmp', expect.stringContaining('I traced the deployment order'), 'utf8');
});

test.each(['product', 'pm', 'intent-refinement'])('Codex %s records distinct repository activity in the transcript', async persona => {
  const reporter = await createCodexPersonaReporter({ ...context, persona: `agent-codex-${persona}` });
  reporter.progress('Starting the repository assessment.');
  reporter.progress('Reading deploy.sh (lines 1–100)', { id: 'read-1', category: 'tool', state: 'running' });
  reporter.progress('Read deploy.sh (lines 1–100)', { id: 'read-1', category: 'tool', state: 'completed' });
  await jest.advanceTimersByTimeAsync(5000);
  await reporter.finish({ response: 'Assessment ready.', threadId: 'session', usage: {} }, {});
  expect(fs.writeFileSync).toHaveBeenCalledWith('/tmp/adp-run-transcript.md.tmp', expect.stringContaining('Reading deploy.sh'), 'utf8');
  expect(fs.writeFileSync).toHaveBeenCalledWith('/tmp/adp-run-transcript.md.tmp', expect.stringContaining('Read deploy.sh'), 'utf8');
});

test.each(['agent-codex-developer', 'agent-codex-reviewer'])('%s retains running checklist updates through tools and failure', async persona => {
  const reporter = await createCodexDeveloperReporter({ ...context, persona });
  const before = '**0 of 2 tasks complete**\n\n- ☐ Implement history\n- ☐ Verify integration';
  const after = '**1 of 2 tasks complete**\n\n- ☑ Implement history\n- ☐ Verify integration';
  reporter.progress!(before, { id: 'plan', category: 'plan', state: 'running' });
  await jest.advanceTimersByTimeAsync(5000);
  const comments = () => fetchMock.mock.calls.filter(([url]) => url.endsWith('/issues/comments/123'))
    .map(([, init]) => JSON.parse(init.body).body);
  expect(comments().at(-1)).toContain(before);
  reporter.progress!(after, { id: 'plan', category: 'plan', state: 'running' });
  for (let i = 0; i < 15; i++) reporter.activity(`Running command ${i}`);
  await jest.advanceTimersByTimeAsync(5000);
  expect(comments().at(-1)).toContain('### Task checklist');
  expect(comments().at(-1)).toContain(after);
  expect(comments().at(-1)).not.toContain(before);
  await reporter.fail(new Error('Execution deadline exhausted'));
  expect(comments().at(-1)).toContain(after);
  expect(comments().at(-1)).not.toContain('2 of 2 tasks complete');
  expect(fs.writeFileSync).toHaveBeenCalledWith('/tmp/adp-run-transcript.md.tmp', expect.stringContaining(after), 'utf8');
});

test('archives Codex assignment progress across repair and inspection SDK sessions', async () => {
  const reporter = await createCodexDeveloperReporter({ ...context, persona: 'agent-codex-reviewer' });
  reporter.session('repair-session');
  reporter.progress!('- ☐ Deliver story', { id: 'repair-plan', category: 'plan', state: 'running' });
  reporter.session('inspection-session');
  reporter.progress!('- ☑ Read diff', { id: 'inspection-plan', category: 'plan', state: 'completed', plan_scope: 'inspection' });
  await reporter.finish({ summary: 'Inspection done; delivery remains open' });
  const writes = (fs.writeFileSync as jest.Mock).mock.calls.filter(([path]) => path === '/tmp/adp-run-transcript.md.tmp');
  const markdown = writes.at(-1)![1] as string;
  const encoded = /^<!-- adp-run-record:v1 ([A-Za-z0-9+/=]+) -->/.exec(markdown)![1];
  const record = JSON.parse(Buffer.from(encoded, 'base64').toString('utf8'));
  expect(record.session_ids).toEqual(['repair-session', 'inspection-session']);
  expect(record.latest_checklist.tasks.map((t: any) => t.text)).toEqual(['Deliver story']);
  expect(record.latest_checklist.tasks[0].status).toBe('pending');
});
