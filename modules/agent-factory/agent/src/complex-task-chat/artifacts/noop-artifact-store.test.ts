import { NoopArtifactStore } from './noop-artifact-store';

describe('NoopArtifactStore', () => {
  const store = new NoopArtifactStore();

  it('publish returns a ref with generated id', async () => {
    const ref = await store.publish({
      sessionId: 'test-session',
      localPath: '/tmp/test.txt',
      filename: 'test.txt',
    });
    expect(ref.id).toMatch(/^art_noop_/);
    expect(ref.filename).toBe('test.txt');
    expect(ref.source).toBe('agent');
    expect(ref.sizeBytes).toBe(0);
  });

  it('fetch is a no-op', async () => {
    await expect(store.fetch('any-id', '/tmp/dest')).resolves.toBeUndefined();
  });

  it('listBySession returns empty', async () => {
    const refs = await store.listBySession('test-session');
    expect(refs).toEqual([]);
  });

  it('exposes no tools', () => {
    expect(store.toolsForTurn({ sessionId: 'test-session' })).toEqual([]);
  });
});
