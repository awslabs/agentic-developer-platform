import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { HostBridge, encode, decode } from '../../../tools/task-sdk/protocol.mjs';

for (const status of ['confirmed', 'unknown', 'rejected']) {
  test(`Task Responses IPC: ${status} uses correlated canonical turn`, async () => {
    const task_id = `tsk_${randomUUID()}`;
    let sent;
    const bridge = new HostBridge({ task_id }, frame => { sent = JSON.parse(encode(frame)); });
    const turn_id = randomUUID();
    bridge.nextTurn = turn_id;
    const body = { input: 'fixture', reasoning: { effort: 'medium' }, max_output_tokens: 64 };
    const pending = bridge.responses(body);
    // Register rejection assertion before delivering a negative operation.
    const checked = status === 'confirmed' ? pending : assert.rejects(pending, /model_outcome_unknown|model request rejected/);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(sent.turn_id, turn_id);
    assert.deepEqual(sent.responses_request, body);
    assert.equal('sdk_request' in sent, false);
    assert.equal('max_tokens' in sent, false);
    const response = { protocol_version: 1, request_id: randomUUID(), task_id, type: 'model.result', turn_id, operation_status: status,
      ...(status === 'confirmed' ? { responses_response: { status: 'completed' } } : {}) };
    bridge.receive(decode(encode(response).toString()));
    const result = await checked;
    if (status === 'confirmed') assert.deepEqual(result, { operationStatus: 'confirmed', response: { status: 'completed' } });
    assert.equal(bridge.pending.size, 0);
  });
}

test('Task Responses IPC: cancellation rejects pending model without replay', async () => {
  const task_id = `tsk_${randomUUID()}`;
  const sent = [];
  const bridge = new HostBridge({ task_id }, frame => sent.push(frame));
  const pending = assert.rejects(bridge.responses({ input: 'fixture' }));
  await new Promise(resolve => setImmediate(resolve));
  bridge.receive({ task_id, type: 'cancel', command_id: randomUUID(), intentional: true });
  await pending;
  await assert.rejects(bridge.responses({ input: 'fixture' }));
  assert.equal(sent.length, 1);
});
