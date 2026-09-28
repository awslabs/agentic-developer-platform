import { invokeTaskResponses, validResponsesProbe } from './task-responses';

const start = { slot_id: 'slot', model_id: 'openai.fixture', account_id: '123456789012', region: 'us-east-1',
  access_key_id: 'fixture-key', secret_access_key: 'fixture-secret', session_token: 'fixture-session',
  credentials_expires_at: new Date(Date.now() + 60000).toISOString() };

it('signs the exact bounded body for one admitted Bedrock destination', async () => {
  const request = jest.fn().mockResolvedValue(new Response('{}', { status: 200, headers: { 'x-amzn-requestid': 'receipt' } }));
  const receipt = await invokeTaskResponses(start, '{"input":"Reply OK.","max_output_tokens":64}', 10, request);
  expect(request).toHaveBeenCalledTimes(1);
  const [url, options] = request.mock.calls[0];
  expect(url).toBe('https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses');
  expect(options.redirect).toBe('error');
  expect(options.headers.authorization).toContain('/us-east-1/bedrock/aws4_request');
  expect(options.headers['x-amz-security-token']).toBe('fixture-session');
  expect(JSON.parse(options.body)).toEqual({ input: 'Reply OK.', max_output_tokens: 64,
    model: 'openai.fixture', stream: false, store: false, include: ['reasoning.encrypted_content'] });
  expect(receipt.requestId).toBe('receipt');
});

it('bounds streamed receipt bytes and does not retry', async () => {
  const request = jest.fn().mockResolvedValue(new Response('x'.repeat(65537)));
  await expect(invokeTaskResponses(start, '{}', 10, request)).rejects.toThrow('exceeds bound');
  expect(request).toHaveBeenCalledTimes(1);
});

it('refuses arbitrary destination regions before issuing credentials', async () => {
  const request = jest.fn();
  await expect(invokeTaskResponses({ ...start, region: 'attacker.example/x' }, '{}', 10, request)).rejects.toThrow('region');
  expect(request).not.toHaveBeenCalled();
});

const tool = { type: 'function_call', namespace: 'mcp__adp', name: 'task_probe', call_id: 'call', arguments: '{"value":"OK"}' };
const document = { object: 'response', id: 'resp', status: 'completed', output: [tool] };
it('accepts the namespaced tool sentinel without executing it', () => {
  expect(validResponsesProbe(document, true)).toBe(true);
});
it.each([
  { ...document, status: 'incomplete' },
  { ...document, error: { code: 'error' } },
  { ...document, output: [{ ...tool, namespace: 'other' }] },
  { ...document, output: [{ ...tool, arguments: '{"value":"OK","command":"sh"}' }] },
  { ...document, output: [{ ...tool, arguments: '{broken' }] },
  { ...document, output: [tool, tool] },
  { ...document, output: [null] },
])('refuses nonconforming Responses evidence %#', value => {
  expect(validResponsesProbe(value, true)).toBe(false);
});
