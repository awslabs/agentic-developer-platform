import { responsesOutputDefault, withResponsesOutputBound } from './responsesOutputBound';

test('Responses defaults are configurable and explicit caps are preserved byte-for-byte', () => {
  expect(responsesOutputDefault({})).toBe(16384);
  const limit = responsesOutputDefault({ SIGV4_PROXY_RESPONSES_MAX_OUTPUT_TOKENS: '2048' });
  const raw = Buffer.from('{"model":"openai.gpt-6-sol","input":"hello"}');
  expect(JSON.parse(withResponsesOutputBound('/openai/v1/responses', raw, limit).toString()).max_output_tokens).toBe(2048);
  for (const value of [128, 0, -1, null, true, '100']) {
    const explicit = Buffer.from(JSON.stringify({ max_output_tokens: value }));
    expect(withResponsesOutputBound('/openai/v1/responses', explicit, limit)).toBe(explicit);
  }
  expect(withResponsesOutputBound('/v1/messages', raw, limit)).toBe(raw);
  const malformed = Buffer.from('{');
  expect(withResponsesOutputBound('/openai/v1/responses', malformed, limit)).toBe(malformed);
});

test.each(['0', '-1', 'NaN', '', '1.5', 'Infinity'])('invalid configured default %s is refused', value => {
  expect(() => responsesOutputDefault({ SIGV4_PROXY_RESPONSES_MAX_OUTPUT_TOKENS: value })).toThrow('positive integer');
});
