/**
 * NoopArtifactStore — stub implementation that compiles but does nothing.
 *
 * Used when artifact storage is disabled or during testing.
 */
import { ArtifactStore, ArtifactRef, TurnScope, CallerIdentity } from './port';
import { AgentTool } from '../context/types';

export class NoopArtifactStore implements ArtifactStore {
  async publish(input: {
    sessionId: string;
    taskId?: string;
    localPath: string;
    filename?: string;
    contentType?: string;
    ttl?: number;
    supersedes?: string;
    source?: 'agent' | 'user';
    identity?: CallerIdentity;
  }): Promise<ArtifactRef> {
    const now = new Date().toISOString();
    return {
      id: `art_noop_${Date.now()}`,
      url: '',
      urlExpiresAt: now,
      filename: input.filename ?? 'unknown',
      contentType: input.contentType ?? 'application/octet-stream',
      sizeBytes: 0,
      checksum: '',
      createdAt: now,
      supersedes: input.supersedes,
      source: input.source ?? 'agent',
    };
  }

  async fetch(
    _artifactId: string,
    _destPath: string,
    _identity?: CallerIdentity,
  ): Promise<void> {
    // no-op
  }

  async listBySession(
    _sessionId: string,
    _filter?: { contentType?: string; filename?: string; limit?: number },
    _identity?: CallerIdentity,
  ): Promise<ArtifactRef[]> {
    return [];
  }

  toolsForTurn(_scope: TurnScope): AgentTool[] {
    return [];
  }
}
