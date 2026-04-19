import { buildArtifactStore } from './factory';
import { NoopArtifactStore } from './noop-artifact-store';

describe('buildArtifactStore', () => {
  it('returns NoopArtifactStore by default', () => {
    const store = buildArtifactStore({});
    expect(store).toBeInstanceOf(NoopArtifactStore);
  });

  it('returns NoopArtifactStore when ARTIFACT_STRATEGY=noop', () => {
    const store = buildArtifactStore({ ARTIFACT_STRATEGY: 'noop' });
    expect(store).toBeInstanceOf(NoopArtifactStore);
  });

  it('throws for unknown strategy', () => {
    expect(() => buildArtifactStore({ ARTIFACT_STRATEGY: 'unknown' })).toThrow(
      'Unknown ARTIFACT_STRATEGY: unknown',
    );
  });

  it('throws when s3 strategy missing ARTIFACTS_BUCKET', () => {
    expect(() => buildArtifactStore({ ARTIFACT_STRATEGY: 's3' })).toThrow(
      'ARTIFACTS_BUCKET env var is required',
    );
  });
});
