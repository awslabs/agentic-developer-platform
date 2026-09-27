import * as fs from 'fs';
import { createCodexDeveloperReporter, publicDeveloperText } from './codex-developer-reporting';
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
