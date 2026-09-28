import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { HostBridge, ModelRefusal, frame } from '../protocol.mjs';
import { executionFailure } from '../execution-failure.mjs';
import { startProxy } from '../model-proxy.mjs';

function bridgeFor(fields) {
  const start = { task_id: 'tsk_' + randomUUID() };
  const bridge = new HostBridge(start, request => queueMicrotask(() => bridge.receive(frame('model.result', start.task_id, {
    turn_id: request.turn_id, operation_status: 'rejected', error_code: 'budget_exceeded',
    pre_provider_refusal: 'budget_exceeded', ...fields,
  }))));
  return bridge;
}
test('known host refusal survives the SDK HTTP failure without exposing raw prose', async () => {
  const bridge = bridgeFor({ message: 'secret provider error' });
  const proxy = await startProxy(bridge, { maxTokens: 32 });
  try {
    const result = await fetch(proxy.url + '/v1/messages', { method: 'POST', headers: { authorization: 'Bearer ' + proxy.token }, body: JSON.stringify({ messages: [{ role: 'user', content: 'test' }] }) });
    assert.equal(result.status, 502);
    assert.ok(bridge.failure instanceof ModelRefusal);
    for (const persona of ['coding', 'cyber']) {
      const failure = executionFailure(new Error('SDK wrapped private detail'), bridge.failure, persona);
      assert.equal(failure.code, 'process_failed');
      assert.match(failure.message, /before provider dispatch.*budget/);
      assert.ok(failure.message.startsWith(persona === 'coding' ? 'Coding' : 'Cyber'));
      assert.ok(failure.message.length < 200);
      assert.doesNotMatch(failure.message, /secret|private/);
    }
  } finally { await proxy.close(); }
});
for (const fields of [
  { pre_provider_refusal: undefined }, { pre_provider_refusal: 'private detail' },
  { error_code: 'model_access_denied' }, { usage: { input_tokens: 1 } },
  { content: [] }, { stop_reason: 'end_turn' }, { operation_status: 'unknown' },
]) test('unproven refusal remains generic: ' + JSON.stringify(fields), async () => {
  const bridge = bridgeFor(fields);
  await assert.rejects(bridge.model({ messages: [] }, 32), error => {
    assert.equal(error instanceof ModelRefusal, false);
    assert.doesNotMatch(executionFailure(error, null, 'coding').message, /before provider dispatch/);
    if (fields.operation_status === 'unknown') assert.equal(executionFailure(error, null, 'coding').code, 'model_outcome_unknown');
    return true;
  });
});
test('plain error text cannot impersonate a known refusal', () => {
  assert.equal(executionFailure(new Error('budget_exceeded'), null, 'coding').message, 'Coding SDK execution did not complete with confirmed evidence.');
});
test('known access refusal has bounded static text', async () => {
  const bridge = bridgeFor({ error_code: 'model_access_denied', pre_provider_refusal: 'model_access_denied' });
  await assert.rejects(bridge.model({ messages: [] }, 32), error => {
    assert.ok(error instanceof ModelRefusal);
    assert.match(executionFailure(error, null, 'coding').message, /before provider dispatch.*access/);
    return true;
  });
});
test('cancellation still interrupts a pending model before any refusal arrives', async () => {
  const bridge = new HostBridge({ task_id: 'tsk_' + randomUUID() }, () => {});
  const pending = bridge.model({ messages: [] }, 32);
  await new Promise(resolve => setImmediate(resolve));
  const command_id = randomUUID();
  bridge.receive(frame('cancel', bridge.start.task_id, { command_id, intentional: true }));
  await assert.rejects(pending);
  assert.equal(bridge.cancelCommand, command_id);
  assert.equal(bridge.failure, null);
});
