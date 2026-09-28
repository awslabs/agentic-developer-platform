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
    if (status === 'confirmed') assert.deepEqual(result, { operationStatus: 'confirmed', turnId: turn_id, response: { status: 'completed' } });
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

for (const current of [true, false]) {
  test(`Task current-authority receipt: ${current}`, async () => {
    const task_id = `tsk_${randomUUID()}`;
    let sent;
    const bridge = new HostBridge({ task_id }, frame => { sent = frame; });
    const pending = bridge.current();
    const checked = current ? pending : assert.rejects(pending, /no longer current/);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(sent.type, 'control.request');
    assert.throws(() => bridge.receive({ task_id, type: 'control.result', request_id: randomUUID(), current }), /uncorrelated/);
    bridge.receive({ task_id, type: 'control.result', request_id: sent.request_id, current });
    await checked;
    assert.equal(bridge.pending.size, 0);
  });
}

test('Task steering is explicit, deduplicated and bound to the next model turn', async () => {
  const task_id = `tsk_${randomUUID()}`;
  const turn = { task_id, type: 'turn', turn_id: randomUUID(), messages: [{ command_id: randomUUID(), text: 'Amended requirement' }] };
  assert.throws(() => new HostBridge({ task_id }, () => {}).receive(turn), /unsolicited/);
  let sent;
  const bridge = new HostBridge({ task_id }, frame => { sent = frame; }, { allowSteering: true });
  bridge.receive(turn); bridge.receive(turn);
  assert.deepEqual(bridge.takeSteering(), [{ turn_id: turn.turn_id, text: 'Amended requirement' }]);
  assert.equal(bridge.takeSteering().length, 0);
  assert.ok(bridge.evidence.has('follow_up_input.' + turn.messages[0].command_id));
  assert.throws(() => bridge.receive({ ...turn, messages: [] }), /changed replayed/);
  const pending = assert.rejects(bridge.responses({ input: 'fixture' }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent.turn_id, turn.turn_id);
  bridge.receive({ task_id, type: 'cancel', command_id: randomUUID(), intentional: true });
  await pending;
});

test('Malformed and overlapping steering cannot mutate admitted turn or evidence', () => {
  const task_id = `tsk_${randomUUID()}`;
  const bridge = new HostBridge({ task_id }, () => {}, { allowSteering: true });
  const turn_id = randomUUID();
  assert.throws(() => bridge.receive({ task_id, type: 'turn', turn_id, messages: [{ command_id: randomUUID(), text: 'valid' }, { command_id: 'invalid', text: 'invalid' }] }), /invalid input/);
  assert.equal(bridge.nextTurn, null);
  assert.equal(bridge.evidence.size, 1);
  assert.equal(bridge.seenTurns.size, 0);
  bridge.receive({ task_id, type: 'turn', turn_id, messages: [{ command_id: randomUUID(), text: 'valid' }] });
  assert.throws(() => bridge.receive({ task_id, type: 'turn', turn_id: randomUUID(), messages: [] }), /not been consumed/);
  assert.equal(bridge.nextTurn, turn_id);
});


test('Task tool IPC snapshots model correlation and refuses authority in replies', async () => {
  const task_id = `tsk_${randomUUID()}`;
  let sent;
  const bridge = new HostBridge({ task_id }, value => { sent = value; });
  const modelCall = { turn_id: randomUUID(), call_id: 'call_123' };
  const expected = { ...modelCall };
  const pending = bridge.tool('repository.read_change', {}, modelCall);
  modelCall.call_id = 'changed';
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(sent.model_call, expected);
  const reply = { ...sent, type: 'tool.result', operation_status: 'unknown' };
  assert.throws(() => decode(encode({ ...reply, owner_token: randomUUID() }).toString()), /authority/);
  bridge.receive(reply);
  assert.equal((await pending).operation_status, 'unknown');
  for (const invalid of [null, [], {}, { ...expected, owner_token: 'secret' }, { ...expected, turn_id: 'bad' }, { ...expected, call_id: '' }]) {
    await assert.rejects(bridge.tool('repository.read_change', {}, invalid), /invalid model call/);
  }
  assert.equal(bridge.pending.size, 0);
});
