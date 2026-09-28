jest.mock('@anthropic-ai/claude-agent-sdk', () => ({ query: jest.fn() }));
jest.mock('./model-policy-runtime', () => ({
  prepareModelQuery: async (params: unknown) => params,
  ModelPolicyRefused: class extends Error {},
}));

import { query } from '@anthropic-ai/claude-agent-sdk';
import { experimentSteeringReachesTheModel, experimentSteeringSurvivesRetry } from './control-runtime.integration';

describe('live retry probe wiring through the production wrapper and adapter', () => {
  it('delivers at a quiet input boundary without needing another output message', async () => {
    const received: unknown[] = [];
    const mockQuery = query as jest.Mock;
    mockQuery.mockReset();
    mockQuery.mockImplementation((params: { prompt: AsyncIterable<unknown> }) => {
      async function* messages() {
        const input = params.prompt[Symbol.asyncIterator]();
        await input.next();
        yield { type: 'system', subtype: 'init', session_id: 'probe-session' };
        yield { type: 'assistant', session_id: 'probe-session', message: { content: [] } };
        // The reader parks only after the last output has been consumed.
        received.push((await input.next()).value);
        yield { type: 'assistant', session_id: 'probe-session', message: { content: [] } };
        yield { type: 'result', subtype: 'success', session_id: 'probe-session' };
      }
      return Object.assign(messages(), { close: jest.fn() });
    });
    const report = await experimentSteeringReachesTheModel();
    expect(report.ok).toBe(true);
    expect(report.artifact.stream_ended_at_handoff).toBe(false);
    expect(report.artifact.input_accepted_before_first_boundary).toBe(false);
    expect(received).toHaveLength(1);
  }, 15000);

  it.each([true, false])('requires a handoff on the replacement stream (reader=%s)', async (reader) => {
    const received: unknown[] = [];
    const closes: jest.Mock[] = [];
    const mockQuery = query as jest.Mock;
    mockQuery.mockReset();
    mockQuery.mockImplementation((params: { prompt: AsyncIterable<unknown> }) => {
      const attempt = closes.length + 1;
      const close = jest.fn();
      closes.push(close);
      async function* messages() {
        const input = params.prompt[Symbol.asyncIterator]();
        await input.next();
        yield { type: 'system', subtype: 'init', session_id: 'probe-session' };
        yield { type: 'assistant', session_id: 'probe-session', message: { content: [] } };
        if (attempt === 2 && reader) received.push((await input.next()).value);
        yield { type: 'result', subtype: 'success', session_id: 'probe-session' };
      }
      return Object.assign(messages(), { close });
    });
    const report = await experimentSteeringSurvivesRetry();
    expect(mockQuery).toHaveBeenCalledTimes(2);
    expect(closes.every(close => close.mock.calls.length > 0)).toBe(true);
    expect(report.artifact.first_attempt_failed).toBe(true);
    expect(report.artifact.stale_channel_closed).toBe(true);
    expect(report.ok).toBe(reader);
    expect(report.artifact.post_retry_handoff_result).toBe(reader ? 'delivered' : null);
    expect(received).toHaveLength(reader ? 1 : 0);
    if (reader) expect(JSON.stringify(received[0])).toContain('RETRY_STEERING_RECEIVED');
  }, 15000);
});
