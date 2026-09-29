import test from 'node:test';
import assert from 'node:assert/strict';
import { reviewEvents, reviewSignal, reviewOperation, type ReviewObserver } from './review-observer.js';

test('review activity omits reasoning, command output and unvalidated structured verdicts', () => {
  const seen: string[] = [];
  const observer: ReviewObserver = {
    explanation: text => seen.push(text), activity: text => seen.push(text), session: text => seen.push(text),
    async finish() {}, async fail() {},
  };
  const publish = reviewEvents(observer, true)!;
  publish({ type: 'item.completed', item: { type: 'reasoning', id: 'r', text: 'private' } });
  publish({ type: 'item.completed', item: { type: 'agent_message', id: 'v', text: 'unvalidated verdict' } });
  publish({ type: 'item.completed', item: { type: 'command_execution', id: 'c', command: 'npm test',
    aggregated_output: 'private output', status: 'completed', exit_code: 0 } });
  assert.deepEqual(seen, ['Finished (exit 0): npm test']);
});

test('operator cancellation reaches both SDK signals and controller effects', async () => {
  const abort = new AbortController();
  const observer: ReviewObserver = {
    explanation() {}, activity() {}, session() {}, async finish() {}, async fail() {},
    control: { socket: '/test', signal: abort.signal, async operation(work) {
      abort.signal.throwIfAborted(); return work();
    } },
  };
  const signal = reviewSignal(new AbortController().signal, observer);
  abort.abort(new Error('Operator aborted'));
  assert.equal(signal.aborted, true);
  await assert.rejects(reviewOperation(observer, async () => assert.fail('no publication after abort')), /Operator aborted/);
});
