import { buildContextManager } from './factory';
import { NoopContextManager } from './noop-context';
import { ChatDataClient } from '../gateway/chat-data-client';
import { GatewayContextManager } from './gateway-context';

describe('buildContextManager', () => {
  it('returns NoopContextManager by default', () => {
    const ctx = buildContextManager({});
    expect(ctx).toBeInstanceOf(NoopContextManager);
  });

  it('returns NoopContextManager when CONTEXT_STRATEGY=noop', () => {
    const ctx = buildContextManager({ CONTEXT_STRATEGY: 'noop' });
    expect(ctx).toBeInstanceOf(NoopContextManager);
  });

  it('throws for unknown strategy', () => {
    expect(() => buildContextManager({ CONTEXT_STRATEGY: 'unknown' })).toThrow(
      'Unknown CONTEXT_STRATEGY: unknown',
    );
  });

  it('constructs gateway context only with explicit opt-in and injected scoped dependencies', () => {
    const client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'workload.token' });
    const summarizer = { summarize: jest.fn(async () => 'summary') };
    expect(buildContextManager({ CONTEXT_STRATEGY: 'gateway', ADP_CHAT_DATA_ENABLED: 'true' }, { client, summarizer }))
      .toBeInstanceOf(GatewayContextManager);
    expect(() => buildContextManager({ CONTEXT_STRATEGY: 'gateway' }, { client, summarizer })).toThrow('Gateway context requires');
    expect(() => buildContextManager({ CONTEXT_STRATEGY: 'gateway', ADP_CHAT_DATA_ENABLED: 'true' })).toThrow('Gateway context requires');
    expect(summarizer.summarize).not.toHaveBeenCalled();
  });

  it.each(['noop', 'lcm'])('rejects %s fallback when scoped chat data is enabled', strategy => {
    expect(() => buildContextManager({ CONTEXT_STRATEGY: strategy, ADP_CHAT_DATA_ENABLED: 'true' }))
      .toThrow('Scoped chat data requires CONTEXT_STRATEGY=gateway');
  });
});
