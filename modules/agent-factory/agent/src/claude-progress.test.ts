import { ClaudeProgress } from './claude-progress';
import { ExplanationEvents } from './explanation-events';

test('Claude public deltas and concurrent tool lifecycles share stable identities without leaking results', () => {
  const events = new ExplanationEvents('run', 1);
  const adapter = new ClaudeProgress(events);
  const stream = (event: unknown) => adapter.observe({ type: 'stream_event', event });
  stream({ type: 'message_start', message: { id: 'm' } });
  stream({ type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } });
  stream({ type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'Checking tests. More' } });
  stream({ type: 'content_block_delta', index: 1, delta: { type: 'thinking_delta', thinking: 'PRIVATE' } });
  expect(events.replay().events[0].payload).toMatchObject({ text: 'Checking tests.', progress: { id: 'm:0', state: 'running' } });
  adapter.observe({ type: 'assistant', message: { id: 'm', content: [
    { type: 'text', text: 'Checking tests. More detail.' },
    { type: 'tool_use', id: 'a', name: 'Bash', input: { command: 'SECRET INPUT' } },
    { type: 'tool_use', id: 'b', name: 'Read' },
  ] } });
  adapter.observe({ type: 'user', message: { content: [
    { type: 'tool_result', tool_use_id: 'b', content: 'PRIVATE OUTPUT' },
    { type: 'tool_result', tool_use_id: 'a', is_error: true, content: 'PRIVATE OUTPUT' },
  ] } });
  const history = events.replay().events;
  expect(history[1].payload.progress).toMatchObject({ id: 'm:0', state: 'completed' });
  expect(history.at(-1)?.payload).toMatchObject({ text: 'Bash failed', progress: { id: 'a', state: 'failed' } });
  expect(JSON.stringify(history)).not.toMatch(/PRIVATE|SECRET INPUT/);
});

test('shared progress bounds update frequency and preserves start time through completion', () => {
  const events = new ExplanationEvents('run', 1);
  const detail = { id: 'a', category: 'message' as const, state: 'running' as const };
  events.publish('First', detail);
  events.publish('Second', detail);
  events.publish('Final', { ...detail, state: 'completed' });
  const history = events.replay().events;
  expect(history).toHaveLength(2);
  expect(history[1].payload.progress?.started_at).toBe(history[0].payload.progress?.started_at);
  events.finish(); events.publish('Too late', detail);
  expect(events.replay().events.at(-1)?.kind).toBe('terminal');
});
