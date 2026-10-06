import { buildMemoryProvider } from './factory';
import { NullMemoryProvider } from './null-memory';
import { GatewayMemoryProvider } from './gateway-memory';
import { ChatDataClient } from '../gateway/chat-data-client';

describe('buildMemoryProvider', () => {
  it('returns NullMemoryProvider by default', () => {
    const provider = buildMemoryProvider({});
    expect(provider).toBeInstanceOf(NullMemoryProvider);
  });

  it('returns NullMemoryProvider when MEMORY_STRATEGY=null', () => {
    const provider = buildMemoryProvider({ MEMORY_STRATEGY: 'null' });
    expect(provider).toBeInstanceOf(NullMemoryProvider);
  });

  it('throws for unknown strategy', () => {
    expect(() => buildMemoryProvider({ MEMORY_STRATEGY: 'unknown' })).toThrow(
      'Unknown MEMORY_STRATEGY: unknown',
    );
  });

  it('throws when dynamo strategy missing MEMORY_TABLE', () => {
    expect(() => buildMemoryProvider({ MEMORY_STRATEGY: 'dynamo' })).toThrow(
      'MEMORY_TABLE env var is required',
    );
  });

  it('requires an explicit gate and a workload-bound client for gateway memory', () => {
    const gateway = { client: new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'synthetic.workload.token' }) };
    expect(buildMemoryProvider({ MEMORY_STRATEGY: 'gateway', ADP_CHAT_DATA_ENABLED: 'true' }, gateway)).toBeInstanceOf(GatewayMemoryProvider);
    expect(() => buildMemoryProvider({ MEMORY_STRATEGY: 'gateway' }, gateway)).toThrow('enabled scoped chat data');
    expect(() => buildMemoryProvider({ MEMORY_STRATEGY: 'gateway', ADP_CHAT_DATA_ENABLED: 'true' })).toThrow('workload-bound client');
  });

  it.each(['dynamo', 'null', undefined])('refuses fallback to %s in scoped mode', strategy => {
    expect(() => buildMemoryProvider({ MEMORY_STRATEGY: strategy, ADP_CHAT_DATA_ENABLED: 'true' })).toThrow('requires MEMORY_STRATEGY=gateway');
  });
});
