/**
 * ArtifactStore port — durable artifact storage for session deliverables.
 *
 * Implementations: S3ArtifactStore, NoopArtifactStore
 */
import { AgentTool } from '../context/types';

export interface ArtifactRef {
  id: string;
  url: string;
  urlExpiresAt: string;
  filename: string;
  contentType: string;
  sizeBytes: number;
  checksum: string;
  createdAt: string;
  supersedes?: string;
  source: 'agent' | 'user';
}

/**
 * Scope handed to `toolsForTurn`. The orchestrator constructs a fresh tool set
 * per turn so `publish_artifact` / `list_artifacts` always receive the current
 * session and task. `onPublish` lets the orchestrator count successful publishes
 * for the post-turn delivery-consistency check.
 */
export interface TurnScope {
  sessionId: string;
  taskId?: string;
  onPublish?: (ref: ArtifactRef) => void;
}

export interface ArtifactStore {
  publish(input: {
    sessionId: string;
    taskId?: string;
    localPath: string;
    filename?: string;
    contentType?: string;
    ttl?: number;
    supersedes?: string;
    source?: 'agent' | 'user';
  }): Promise<ArtifactRef>;

  fetch(artifactId: string, destPath: string): Promise<void>;

  listBySession(
    sessionId: string,
    filter?: {
      contentType?: string;
      filename?: string;
      limit?: number;
    },
  ): Promise<ArtifactRef[]>;

  /**
   * Return tools bound to the current turn's sessionId + taskId. The orchestrator
   * calls this once per turn so every tool handler operates on the correct
   * session scope (prevents cross-session leakage).
   */
  toolsForTurn(scope: TurnScope): AgentTool[];
}
