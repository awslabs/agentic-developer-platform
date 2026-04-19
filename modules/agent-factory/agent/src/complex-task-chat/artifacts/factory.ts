/**
 * Factory for ArtifactStore — selects implementation based on env vars.
 *
 * ARTIFACT_STRATEGY env var:
 *   - "noop" (default): NoopArtifactStore
 *   - "s3": S3ArtifactStore (requires ARTIFACTS_BUCKET + ARTIFACTS_TABLE)
 */
import { ArtifactStore } from './port';
import { NoopArtifactStore } from './noop-artifact-store';

export function buildArtifactStore(env: Record<string, string | undefined> = process.env): ArtifactStore {
  const strategy = env.ARTIFACT_STRATEGY ?? 'noop';

  switch (strategy) {
    case 'noop':
      return new NoopArtifactStore();

    case 's3': {
      const { S3ArtifactStore } = require('./s3-artifact-store');
      const bucket = env.ARTIFACTS_BUCKET;
      const table = env.ARTIFACTS_TABLE;
      if (!bucket) throw new Error('ARTIFACTS_BUCKET env var is required for ARTIFACT_STRATEGY=s3');
      if (!table) throw new Error('ARTIFACTS_TABLE env var is required for ARTIFACT_STRATEGY=s3');
      return new S3ArtifactStore(bucket, table, env.AWS_REGION ?? 'us-east-1');
    }

    default:
      throw new Error(`Unknown ARTIFACT_STRATEGY: ${strategy}. Valid: noop, s3`);
  }
}
