import { buildContextManager } from './factory';
import { NoopContextManager } from './noop-context';

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
});
