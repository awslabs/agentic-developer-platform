import { createHash } from 'node:crypto';
import { runNativeProbe, validNativeResponse } from './native-runner';

jest.mock('./native-probe-manifest.json', () => ({ sdk: '0.155.1', personas: {
  'agent-codex-developer': { 'openai.gpt-6-sol': require('node:crypto').createHash('sha256').update('{}').digest('hex') },
  'agent-codex-reviewer': { 'openai.gpt-6-sol': require('node:crypto').createHash('sha256').update('{}').digest('hex') },
} }));
const digest = createHash('sha256').update('{}').digest('hex');
const response = (text: string) => ({ object: 'response', id: 'resp_test', status: 'completed', output: [
  { type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text }] },
] });
function fixture() {
  const claim = { claimed: true, slot_id: 'slot', lease_token: 'lease', model_id: 'openai.gpt-6-sol',
    compatibility_class: 'codex-sdk', harness_contract_revision: '0.155.1', expected_request_shape_sha256: digest,
    max_budget_usd: '0.1', timeout_seconds: 10, lease_expires_at: new Date(Date.now() + 60000).toISOString() };
  const gateway: any = { claim: jest.fn().mockResolvedValue(claim),
    start: jest.fn().mockResolvedValue({ slot_id: 'slot', model_id: claim.model_id, account_id: '111111111111', region: 'us-east-1',
      access_key_id: 'access', secret_access_key: 'secret', session_token: 'token', credentials_expires_at: new Date(Date.now() + 60000).toISOString() }),
    complete: jest.fn().mockResolvedValue({ evidence_recorded: true }) };
  const capture = jest.fn().mockResolvedValue({ body: '{}', digest });
  const invoke = jest.fn().mockResolvedValue({ status: 200, requestId: 'provider-id', body: Buffer.from(JSON.stringify(response('OK'))) });
  return { claim, gateway, capture, invoke };
}
it('captures before paid start and accepts one confirmed provider response', async () => {
  const { gateway, capture, invoke } = fixture();
  await runNativeProbe('agent-codex-developer', gateway, capture, invoke);
  expect(gateway.claim).toHaveBeenCalledWith('scheduled', undefined, 'agent-codex-developer');
  expect(capture.mock.invocationCallOrder[0]).toBeLessThan(gateway.start.mock.invocationCallOrder[0]);
  expect(gateway.start.mock.invocationCallOrder[0]).toBeLessThan(invoke.mock.invocationCallOrder[0]);
  expect(invoke).toHaveBeenCalledTimes(1);
  expect(gateway.complete.mock.calls[0][2]).toMatchObject({ outcome: 'proven', provider_request_id: 'provider-id' });
});
it.each(['claim', 'capture', 'expired'])('refuses %s mismatch before credentials', async failure => {
  const { claim, gateway, capture, invoke } = fixture();
  if (failure === 'claim') claim.expected_request_shape_sha256 = 'wrong';
  if (failure === 'capture') capture.mockResolvedValue({ body: 'changed', digest });
  if (failure === 'expired') claim.lease_expires_at = '2000-01-01';
  await expect(runNativeProbe('agent-codex-developer', gateway, capture, invoke)).rejects.toThrow();
  expect(gateway.start).not.toHaveBeenCalled();
  expect(invoke).not.toHaveBeenCalled();
});
it.each(['receipt', 'transport', 'tool', 'incomplete', 'http'])('does not promote or retry %s failure', async failure => {
  const { gateway, capture, invoke } = fixture();
  const document: any = response('OK');
  if (failure === 'incomplete') document.status = 'incomplete';
  if (failure === 'tool') document.output = [{ type: 'function_call', name: 'exec_command' }];
  if (failure === 'transport') invoke.mockRejectedValue(new Error('timeout'));
  else invoke.mockResolvedValue({ status: failure === 'http' ? 500 : 200, requestId: failure === 'receipt' ? undefined : 'provider-id', body: Buffer.from(JSON.stringify(document)) });
  await runNativeProbe('agent-codex-developer', gateway, capture, invoke);
  expect(invoke).toHaveBeenCalledTimes(1);
  expect(gateway.complete.mock.calls[0][2].outcome).toBe('error');
});
it('does not invoke on an uncertain or mismatched start', async () => {
  const { gateway, capture, invoke } = fixture();
  gateway.start.mockRejectedValueOnce(new Error('lost start receipt'));
  await expect(runNativeProbe('agent-codex-developer', gateway, capture, invoke)).rejects.toThrow();
  gateway.start.mockResolvedValueOnce({ model_id: 'wrong' });
  await runNativeProbe('agent-codex-developer', gateway, capture, invoke);
  expect(invoke).not.toHaveBeenCalled();
});
it('requires reviewer structured output rather than developer text', () => {
  expect(validNativeResponse(response('OK'), 'agent-codex-reviewer')).toBe(false);
  const verdict = { verdict: 'approve', summary: 'OK', findings: [], validationGaps: [] };
  expect(validNativeResponse(response(JSON.stringify(verdict)), 'agent-codex-reviewer')).toBe(true);
  expect(validNativeResponse(response(JSON.stringify({ ...verdict, findings: [{}] })), 'agent-codex-reviewer')).toBe(false);
  expect(validNativeResponse(response(JSON.stringify({ ...verdict, summary: ['OK'] })), 'agent-codex-reviewer')).toBe(false);
});
