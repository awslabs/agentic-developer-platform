import test from 'node:test';
import assert from 'node:assert/strict';
import { reviewEvents, reviewSignal, reviewOperation, type ReviewObserver } from './review-observer.js';

test('review activity omits reasoning, command output and unvalidated structured verdicts', () => {
  const seen: string[] = [];
  const observed: string[] = [];
  const observer: ReviewObserver = {
    observeEvent: event => observed.push(event.type),
    explanation: text => seen.push(text), activity: text => seen.push(text), session: text => seen.push(text),
    async finish() {}, async fail() {},
  };
  const publish = reviewEvents(observer, true)!;
  publish({ type: 'item.completed', item: { type: 'reasoning', id: 'r', text: 'private' } });
  publish({ type: 'item.completed', item: { type: 'agent_message', id: 'v', text: 'unvalidated verdict' } });
  publish({ type: 'item.completed', item: { type: 'command_execution', id: 'c', command: 'npm test',
    aggregated_output: 'private output', status: 'completed', exit_code: 0 } });
  assert.deepEqual(seen, ['Finished (exit 0): npm test']);
  assert.ok(observed.includes('item.completed'), 'SDK command lifecycle reaches the control observer');
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

test('structured reviewer turns still publish checklist updates before returning a verdict', () => {
  const plans: string[] = [];
  const observer: ReviewObserver = {
    explanation() {}, activity() {}, session() {}, async finish() {}, async fail() {},
    progress(text, detail) { if (detail.category === 'plan') plans.push(text); },
  };
  const publish = reviewEvents(observer, true)!;
  publish({ type: 'item.updated', item: { type: 'todo_list', id: 'p', items: [
    { text: 'Verify history integration', completed: false },
  ] } });
  publish({ type: 'item.updated', item: { type: 'todo_list', id: 'p', items: [
    { text: 'Verify history integration', completed: true },
  ] } });
  assert.match(plans[0]!, /0 of 1 tasks complete/);
  assert.match(plans[1]!, /1 of 1 tasks complete/);
});
