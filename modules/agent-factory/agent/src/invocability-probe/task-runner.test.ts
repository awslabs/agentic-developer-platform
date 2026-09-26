import { runTaskProbe } from './task-runner';
import profiles from './task-profiles.json';

function fixture(persona: keyof typeof profiles = 'agent-task-investigator') {
  const profile = profiles[persona];
  const claim = { claimed: true, slot_id: 'slot', lease_token: 'token', model_id: 'model',
    compatibility_class: 'anthropic_messages', harness_contract_revision: profile.revision,
    task_probe_json: profile.body, expected_request_shape_sha256: profile.digest, timeout_seconds: 10 };
  const gateway: any = { claim: jest.fn().mockResolvedValue(claim),
    start: jest.fn().mockResolvedValue({ slot_id: 'slot', model_id: 'model', region: 'us-east-1',
      access_key_id: 'test-access', secret_access_key: 'test-secret', session_token: 'test-token',
      credentials_expires_at: new Date(Date.now() + 60000).toISOString() }),
    complete: jest.fn().mockResolvedValue({ evidence_recorded: true }) };
  return { claim, gateway };
}

it.each(Object.keys(profiles) as (keyof typeof profiles)[])('qualifies exact bounded %s profile only with provider receipt', async persona => {
  const { gateway } = fixture(persona);
  const body = persona === 'agent-task-investigator' ? { type: 'message', role: 'assistant', stop_reason: 'end_turn', content: [{ type: 'text', text: 'OK' }] }
    : { content: [{ type: 'tool_use', name: 'task_probe', input: { value: 'OK' } }] };
  const call = jest.fn().mockResolvedValue({ status: 200, requestId: 'provider-receipt', body: Buffer.from(JSON.stringify(body)) });
  await runTaskProbe(persona, gateway, call);
  expect(gateway.claim).toHaveBeenCalledWith('scheduled', persona);
  expect(call).toHaveBeenCalledTimes(1);
  expect(JSON.parse(call.mock.calls[0][1]).max_tokens).toBe(persona === 'agent-task-investigator' ? 16 : 64);
  expect(gateway.complete.mock.calls[0][2]).toMatchObject({ outcome: 'proven', provider_request_id: 'provider-receipt' });
});

it.each(['missing_receipt', 'empty', 'transport'])('does not manufacture proof or retry for %s', async failure => {
  const { gateway } = fixture();
  const call = jest.fn();
  if (failure === 'transport') call.mockRejectedValue(new Error('timeout'));
  else call.mockResolvedValue({ status: 200, requestId: failure === 'missing_receipt' ? undefined : 'id',
    body: Buffer.from(JSON.stringify({ type: 'message', role: 'assistant', stop_reason: 'end_turn', content: failure === 'empty' ? [] : [{ type: 'text', text: 'OK' }] })) });
  await runTaskProbe('agent-task-investigator', gateway, call);
  expect(call).toHaveBeenCalledTimes(1);
  expect(gateway.complete.mock.calls[0][2].outcome).toBe('error');
});

it('rejects unfamiliar claim before durable start', async () => {
  const { claim, gateway } = fixture(); claim.task_probe_json = '{}';
  await expect(runTaskProbe('agent-task-investigator', gateway, jest.fn())).rejects.toThrow('local bounded profile');
  expect(gateway.start).not.toHaveBeenCalled();
});

it('never invokes after uncertain start or mismatched start', async () => {
  const { gateway } = fixture(); const call = jest.fn();
  gateway.start.mockRejectedValueOnce(new Error('lost receipt'));
  await expect(runTaskProbe('agent-task-investigator', gateway, call)).rejects.toThrow('lost receipt');
  expect(call).not.toHaveBeenCalled();
  gateway.start.mockResolvedValueOnce({ slot_id: 'wrong', model_id: 'model', region: 'us-east-1' });
  await expect(runTaskProbe('agent-task-investigator', gateway, call)).rejects.toThrow('identity mismatch');
  expect(call).not.toHaveBeenCalled();
  expect(gateway.complete.mock.calls[0][2].error_code).toBe('no_request_emitted.start_mismatch');
});


it.each(['invalid', '2000-01-01T00:00:00Z'])('refuses invalid or expired destination credentials: %s', async expiry => {
  const { gateway } = fixture(); const call = jest.fn();
  const start = await gateway.start(); start.credentials_expires_at = expiry;
  await expect(runTaskProbe('agent-task-investigator', gateway, call)).rejects.toThrow('identity mismatch');
  expect(call).not.toHaveBeenCalled();
});

function completedText(text = 'OK.') {
  return { type: 'message', role: 'assistant', stop_reason: 'end_turn', content: [{ type: 'text', text }] };
}

it.each(['OK', 'OK.', '  OK.\n'])('accepts only bounded completed investigator sentinel %j', async text => {
  const { gateway } = fixture();
  const call = jest.fn().mockResolvedValue({ status: 200, requestId: 'original-receipt', body: Buffer.from(JSON.stringify(completedText(text))) });
  await runTaskProbe('agent-task-investigator', gateway, call);
  expect(call).toHaveBeenCalledTimes(1);
  expect(gateway.complete.mock.calls[0][2]).toMatchObject({ outcome: 'proven', provider_request_id: 'original-receipt', error_code: null });
});

it.each([
  ['prose', completedText('OK. Here is more text.')],
  ['empty', completedText('')],
  ['lowercase', completedText('ok')],
  ['other punctuation', completedText('OK!')],
  ['multiple periods', completedText('OK..')],
  ['truncated', { ...completedText(), stop_reason: 'max_tokens' }],
  ['unknown stop', { ...completedText(), stop_reason: 'unknown' }],
  ['missing stop', { ...completedText(), stop_reason: undefined }],
  ['wrong role', { ...completedText(), role: 'user' }],
  ['wrong type', { ...completedText(), type: 'unknown' }],
  ['null body', null],
  ['empty content', { ...completedText(), content: [] }],
  ['null block', { ...completedText(), content: [null] }],
  ['nonstring text', { ...completedText(), content: [{ type: 'text', text: 1 }] }],
  ['tool', { ...completedText(), content: [{ type: 'tool_use', name: 'task_probe', input: { value: 'OK' } }] }],
  ['extra prose block', { ...completedText(), content: [...completedText().content, { type: 'text', text: 'more' }] }],
  ['extra tool block', { ...completedText(), content: [...completedText().content, { type: 'tool_use', name: 'task_probe' }] }],
])('rejects investigator %s without retry', async (_name, body) => {
  const { gateway } = fixture();
  const call = jest.fn().mockResolvedValue({ status: 200, requestId: 'original-receipt', body: Buffer.from(JSON.stringify(body)) });
  await runTaskProbe('agent-task-investigator', gateway, call);
  expect(call).toHaveBeenCalledTimes(1);
  expect(gateway.complete.mock.calls[0][2].outcome).toBe('error');
});

it.each([undefined, 202, 500])('rejects unconfirmed HTTP status %s despite valid body', async status => {
  const { gateway } = fixture();
  const call = jest.fn().mockResolvedValue({ status, requestId: 'original-receipt', body: Buffer.from(JSON.stringify(completedText())) });
  await runTaskProbe('agent-task-investigator', gateway, call);
  expect(call).toHaveBeenCalledTimes(1);
  expect(gateway.complete.mock.calls[0][2].outcome).toBe('error');
});
