jest.mock('@anthropic-ai/claude-agent-sdk', () => ({ query: jest.fn() }));
jest.mock('./model-policy-runtime', () => ({
  prepareModelQuery: async (params: unknown) => params,
  ModelPolicyRefused: class extends Error {},
}));
import { query } from '@anthropic-ai/claude-agent-sdk';
import { steeringRetry, abortDuringRetry } from './control-retry.integration';

beforeEach(() => (query as jest.Mock).mockReset());

it('measures each SDK input and preserves an unknown handoff across replacement', async () => {
  let attempt = 0;
  (query as jest.Mock).mockImplementation((params: { prompt: AsyncIterable<unknown>; options: { resume?: string } }) => {
    const number = ++attempt;
    if (number === 2) expect(params.options.resume).toBe('same-session');
    async function* output() {
      const input = params.prompt[Symbol.asyncIterator]();
      await input.next();
      yield { type: 'system', subtype: 'init', session_id: 'same-session' };
      for (let n = 0; n < (number === 1 ? 2 : 1); n++) {
        await input.next();
        yield { type: 'assistant', session_id: 'same-session', message: { content: [] } };
      }
      yield { type: 'result', subtype: 'success', session_id: 'same-session' };
    }
    return Object.assign(output(), { close: jest.fn() });
  });
  const result = await steeringRetry();
  expect(result.error).toBeNull();
  expect(result.inputs).toHaveLength(3);
  expect(result.deliveries_of_queued_command).toBe(1);
  expect(result.confirmed_handoffs_replayed).toBe(0);
  expect(result.ambiguous_handoffs_replayed).toBe(0);
  expect(result.ambiguous_handoff_outcome).toBe('unknown');
  expect(result.session_preserved).toBe(true);
  expect(result.injected_failure_observed).toBe(true);
  expect(result.attempt_id_after).not.toBe(result.attempt_id_before);
}, 15000);

it('observes cancellation in actual wrapper backoff before another query is constructed', async () => {
  (query as jest.Mock).mockImplementation((params: { prompt: AsyncIterable<unknown> }) => {
    async function* output() {
      await params.prompt[Symbol.asyncIterator]().next();
      yield { type: 'system', subtype: 'init', session_id: 'abort-session' };
      yield { type: 'assistant', session_id: 'abort-session', message: { content: [] } };
      yield { type: 'result', subtype: 'success', session_id: 'abort-session' };
    }
    return Object.assign(output(), { close: jest.fn() });
  });
  const result = await abortDuringRetry();
  expect(result).toMatchObject({ attempts: 1, injected: true, backoff: true, cancelled: true,
    abort_during_retry_started_next_attempt: false });
  expect(query).toHaveBeenCalledTimes(1);
}, 15000);
