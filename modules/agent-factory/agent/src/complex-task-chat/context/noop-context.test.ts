import { NoopContextManager } from './noop-context';

describe('NoopContextManager', () => {
  const ctx = new NoopContextManager();

  it('assembles empty messages', async () => {
    const result = await ctx.assemble({
      sessionId: 'test-session',
      userMessage: 'hello',
      tokenBudget: 10000,
    });
    expect(result.messages).toEqual([]);
    expect(result.meta.rawMessageCount).toBe(0);
    expect(result.meta.compactionTriggered).toBe(false);
  });

  it('record is a no-op', async () => {
    await expect(
      ctx.record({
        sessionId: 'test-session',
        userMessage: { role: 'user', content: 'hello' },
        assistantMessage: { role: 'assistant', content: 'hi' },
      }),
    ).resolves.toBeUndefined();
  });

  it('assertOwnership always passes', async () => {
    await expect(ctx.assertOwnership('any-session', 'any-user')).resolves.toBeUndefined();
  });

  it('exposes no tools', () => {
    expect(ctx.tools()).toEqual([]);
  });
});
