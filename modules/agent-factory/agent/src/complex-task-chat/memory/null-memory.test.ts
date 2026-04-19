import { NullMemoryProvider } from './null-memory';

describe('NullMemoryProvider', () => {
  const provider = new NullMemoryProvider();

  it('retrieve returns empty array', async () => {
    const records = await provider.retrieve({
      query: 'anything',
      scope: { user: 'test-user' },
    });
    expect(records).toEqual([]);
  });

  it('save returns a record with generated id', async () => {
    const record = await provider.save({
      content: 'test fact',
      scope: { user: 'test-user' },
      kind: 'fact',
    });
    expect(record.id).toMatch(/^mem_null_/);
    expect(record.content).toBe('test fact');
    expect(record.createdAt).toBeTruthy();
  });

  it('delete is a no-op', async () => {
    await expect(provider.delete!('any-id')).resolves.toBeUndefined();
  });

  it('exposes no tools', () => {
    expect(provider.tools()).toEqual([]);
  });

  it('reports all capabilities as false', () => {
    const caps = provider.capabilities();
    expect(caps.semanticSearch).toBe(false);
    expect(caps.keywordSearch).toBe(false);
    expect(caps.tagFiltering).toBe(false);
    expect(caps.scoping).toEqual([]);
    expect(caps.delete).toBe(false);
    expect(caps.asyncExtraction).toBe(false);
    expect(caps.ttl).toBe(false);
  });
});
