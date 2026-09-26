import { runTaskProbe } from './task-runner';
import profiles from './task-profiles.json';

function fixture(persona: keyof typeof profiles = 'agent-task-investigator') {
  const profile = profiles[persona];
  const claim = { claimed: true, slot_id: 'slot', lease_token: 'token', model_id: 'model',
    compatibility_class: 'anthropic_messages', harness_contract_revision: profile.revision,
    task_probe_json: profile.body, expected_request_shape_sha256: profile.digest, timeout_seconds: 10 };
  const gateway: any = { claim: jest.fn().mockResolvedValue(claim),
    start: jest.fn().mockResolvedValue({ slot_id: 'slot', model_id: 'model', region: 'us-east-1' }),
    complete: jest.fn().mockResolvedValue({ evidence_recorded: true }) };
  return { claim, gateway };
}

it.each(Object.keys(profiles) as (keyof typeof profiles)[])('qualifies exact bounded %s profile only with provider receipt', async persona => {
  const { gateway } = fixture(persona);
  const body = persona === 'agent-task-investigator' ? { content: [{ type: 'text', text: 'OK' }] }
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
    body: Buffer.from(JSON.stringify({ content: failure === 'empty' ? [] : [{ type: 'text', text: 'OK' }] })) });
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
