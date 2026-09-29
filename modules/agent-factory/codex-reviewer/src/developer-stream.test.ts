import test from 'node:test';
import assert from 'node:assert/strict';
import type { ThreadEvent } from '@openai/codex-sdk';
import { publishDeveloperEvent, runDeveloperStream, type DeveloperReporter } from './developer-stream.js';

function recorder() {
  const seen: string[] = [];
  const reporter: DeveloperReporter = {
    explanation: text => seen.push(`explanation:${text}`), activity: text => seen.push(`activity:${text}`),
    session: id => seen.push(`session:${id}`), async finish() {}, async fail() {},
  };
  return { seen, reporter };
}
const usage = { input_tokens: 10, output_tokens: 5, cached_input_tokens: 0, cache_write_input_tokens: 0, reasoning_output_tokens: 0 };

test('SDK activity is published before execution completes and private reasoning is omitted', async () => {
  const { seen, reporter } = recorder();
  async function* events(): AsyncGenerator<ThreadEvent> {
    yield { type: 'thread.started', thread_id: 'native-session' };
    yield { type: 'item.completed', item: { type: 'reasoning', id: 'private', text: 'PRIVATE REASONING' } };
    yield { type: 'item.completed', item: { type: 'agent_message', id: 'update', text: 'I found the failing parser and am fixing it.' } };
    yield { type: 'item.started', item: { type: 'command_execution', id: 'cmd', command: 'npm test', aggregated_output: '', status: 'in_progress' } };
    assert.ok(seen.some(text => text.includes('Running: npm test')), 'progress must reach sinks while the SDK is still executing');
    yield { type: 'item.completed', item: { type: 'command_execution', id: 'cmd', command: 'npm test', aggregated_output: 'secret output must not be published', status: 'completed', exit_code: 0 } };
    yield { type: 'item.completed', item: { type: 'agent_message', id: 'final', text: 'Tests passed; PR opened.' } };
    yield { type: 'turn.completed', usage };
  }
  const result = await runDeveloperStream({ id: 'native-session', runStreamed: async () => ({ events: events() }) }, 'task', {}, reporter);
  assert.equal(result.finalResponse, 'Tests passed; PR opened.');
  assert.ok(seen.includes('session:native-session'));
  assert.ok(seen.some(text => text.includes('Finished (exit 0): npm test')));
  assert.ok(!seen.join('\n').includes('PRIVATE REASONING'));
  assert.ok(!seen.join('\n').includes('secret output'));
});

test('failed SDK turns are never treated as a completed developer run', async () => {
  const { reporter } = recorder();
  await assert.rejects(runDeveloperStream({ id: null, runStreamed: async () => ({ events: (async function* () {
    yield { type: 'turn.failed', error: { message: 'model quota exceeded' } } as ThreadEvent;
  })() }) }, 'task', {}, reporter), /quota exceeded/);
});

test('tool errors remain visible without exposing raw tool output', () => {
  const { seen, reporter } = recorder();
  publishDeveloperEvent({ type: 'item.completed', item: { type: 'command_execution', id: 'bad', command: 'pytest', aggregated_output: 'private environment', status: 'failed', exit_code: 1 } }, reporter);
  assert.deepEqual(seen, ['activity:Finished (exit 1): pytest']);
});

test('operator cancellation never retries the Codex assignment', async () => {
  const { reporter } = recorder();
  const controller = new AbortController();
  let starts = 0;
  await assert.rejects(runDeveloperStream({ id: 'same-session', runStreamed: async () => {
    starts++;
    return { events: (async function* () {
      controller.abort();
      throw new Error('stream disconnected during operator abort');
    })() };
  } }, 'task', { signal: controller.signal }, reporter), /operator abort/);
  assert.equal(starts, 1);
});

test('shared progress receives partial messages, searches and plan updates with stable identities', () => {
  const { reporter } = recorder();
  const progress: unknown[] = [];
  reporter.progress = (text, detail) => progress.push({ text, ...detail });
  publishDeveloperEvent({ type: 'item.updated', item: { id: 'm', type: 'agent_message', text: 'Checking tests' } }, reporter);
  publishDeveloperEvent({ type: 'item.completed', item: { id: 'm', type: 'agent_message', text: 'Checking tests now.' } }, reporter);
  publishDeveloperEvent({ type: 'item.started', item: { id: 's', type: 'web_search', query: 'SDK docs' } }, reporter);
  publishDeveloperEvent({ type: 'item.completed', item: { id: 's', type: 'web_search', query: 'SDK docs' } }, reporter);
  publishDeveloperEvent({ type: 'item.updated', item: { id: 'p', type: 'todo_list', items: [{ text: 'Run tests', completed: true }] } }, reporter);
  assert.deepEqual(progress, [
    { text: 'Checking tests', id: 'm', category: 'message', state: 'running' },
    { text: 'Checking tests now.', id: 'm', category: 'message', state: 'completed' },
    { text: 'Searching the web: SDK docs', id: 's', category: 'tool', state: 'running' },
    { text: 'Searched the web: SDK docs', id: 's', category: 'tool', state: 'completed' },
    { text: '✓ Run tests', id: 'p', category: 'plan', state: 'running' },
  ]);
});

test('reused SDK item IDs in later turns cannot overwrite an earlier turn in the UI', async () => {
  const { scopedProgress } = await import('./developer-stream.js');
  const { reporter } = recorder();
  const ids: string[] = [];
  reporter.progress = (_text, detail) => ids.push(detail.id);
  const first = scopedProgress(reporter), second = scopedProgress(reporter);
  const event: ThreadEvent = { type: 'item.completed', item: { id: 'item_0', type: 'agent_message', text: 'Done' } };
  publishDeveloperEvent(event, first); publishDeveloperEvent(event, first); publishDeveloperEvent(event, second);
  assert.equal(ids[0], ids[1]); assert.notEqual(ids[0], ids[2]);
});
