import { buildMemoryProvider } from './factory';
import { NullMemoryProvider } from './null-memory';

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
});
