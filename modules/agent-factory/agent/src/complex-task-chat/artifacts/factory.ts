/**
 * Factory for ArtifactStore — selects implementation based on env vars.
 *
 * ARTIFACT_STRATEGY env var:
 *   - "noop" (default): NoopArtifactStore
 *   - "s3": S3ArtifactStore (requires ARTIFACTS_BUCKET + ARTIFACTS_TABLE)
 */
import { ArtifactStore } from './port';
import { NoopArtifactStore } from './noop-artifact-store';
import { GatewayArtifactStore } from './gateway-artifact-store';
import { ChatDataClient } from '../gateway/chat-data-client';

export function buildArtifactStore(
  env: Record<string, string | undefined> = process.env,
  gateway?: { client: ChatDataClient; sessionId: string; workspaceRoot: string },
): ArtifactStore {
  const strategy = env.ARTIFACT_STRATEGY ?? 'noop';
  if (env.ADP_CHAT_DATA_ENABLED === 'true' && strategy !== 'gateway') {
    throw new Error('Scoped chat data requires ARTIFACT_STRATEGY=gateway');
  }

  switch (strategy) {
    case 'gateway':
      if (env.ADP_CHAT_DATA_ENABLED !== 'true' || !gateway) {
        throw new Error('Gateway artifacts require enabled scoped chat data and a workload-bound client');
      }
      return new GatewayArtifactStore(gateway.client, gateway.sessionId, gateway.workspaceRoot);

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
      throw new Error(`Unknown ARTIFACT_STRATEGY: ${strategy}. Valid: noop, s3, gateway`);
  }
}
