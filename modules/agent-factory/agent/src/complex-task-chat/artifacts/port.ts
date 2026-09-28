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

/** Caller identity for team-level access control (Stage B, #185). */
export interface CallerIdentity {
  orgId?: string;
  teamId?: string;
  userId?: string;
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
  /** Stage B (#185): identity of the caller, written to DDB catalog rows. */
  identity?: CallerIdentity;
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
    /** Stage B (#185): identity fields written to the DDB catalog row. */
    identity?: CallerIdentity;
  }): Promise<ArtifactRef>;

  fetch(
    artifactId: string,
    destPath: string,
    /** Stage B (#185): caller identity for team-level access check. */
    identity?: CallerIdentity,
  ): Promise<void>;

  listBySession(
    sessionId: string,
    filter?: {
      contentType?: string;
      filename?: string;
      limit?: number;
    },
    /** Stage B (#185): caller identity for team-level filtering. */
    identity?: CallerIdentity,
  ): Promise<ArtifactRef[]>;

  /**
   * Return tools bound to the current turn's sessionId + taskId. The orchestrator
   * calls this once per turn so every tool handler operates on the correct
   * session scope (prevents cross-session leakage).
   */
  toolsForTurn(scope: TurnScope): AgentTool[];
}
