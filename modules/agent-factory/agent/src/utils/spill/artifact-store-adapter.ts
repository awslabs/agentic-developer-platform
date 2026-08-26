/**
 * ArtifactStore → SpillStore adapter (#4179).
 *
 * The chat agent already has durable, session-scoped, access-controlled
 * artifact storage (`complex-task-chat/artifacts/port.ts`). Spilled tool output
 * belongs there rather than in a second S3 client with its own bucket, key
 * layout and IAM surface — reusing the store means spills automatically inherit
 * its tenant scoping (org/team/user + session), so a spill can never be
 * addressable by a different tenant's run.
 *
 * The store's `publish()` is file-path based, so the adapter stages the payload
 * to a local file first. That staging file is *also* the locator handed to the
 * model: `Read` is in the chat agent's allowlist, so a local path is
 * immediately actionable, whereas the presigned artifact URL would need a fetch
 * the agent may not be permitted to make. The published artifact is the durable
 * copy; the local file is the retrieval path.
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { ArtifactStore, CallerIdentity } from '../../complex-task-chat/artifacts/port';
import { SpillStore } from './store';

export interface ArtifactSpillStoreScope {
  sessionId: string;
  taskId?: string;
  /** Inherited scoping — spills land under the same access control as artifacts. */
  identity?: CallerIdentity;
}

/**
 * Adapts an `ArtifactStore` to the `SpillStore` interface.
 *
 * Publishing is best-effort in the same sense as the worker's S3 leg: if
 * `publish()` fails but the local staging write succeeded, the model still gets
 * a readable locator. Only a failure to write the staging file — which means
 * there is no locator at all — propagates, and the hook's fail-open handler
 * turns that into "pass the original output through".
 */
export class ArtifactSpillStore implements SpillStore {
  constructor(
    private readonly artifacts: ArtifactStore,
    private readonly scope: ArtifactSpillStoreScope,
    private readonly opts: {
      /** Directory for staging files. Defaults to a temp dir. */
      stagingDir?: string;
      log?: (msg: string) => void;
    } = {},
  ) {}

  async spill(key: string, body: string): Promise<string> {
    const stagingDir = this.opts.stagingDir ?? path.join(os.tmpdir(), 'adp-spill');
    await fs.promises.mkdir(stagingDir, { recursive: true });
    const localPath = path.join(stagingDir, key);
    await fs.promises.writeFile(localPath, body, 'utf8');

    try {
      const ref = await this.artifacts.publish({
        sessionId: this.scope.sessionId,
        taskId: this.scope.taskId,
        localPath,
        filename: key,
        contentType: 'text/plain',
        source: 'agent',
        identity: this.scope.identity,
      });
      this.opts.log?.(
        `[spill] published spilled output as artifact ${ref.id} (${ref.sizeBytes} bytes)`,
      );
    } catch (err) {
      // The local staging file is the locator, and it is already written — a
      // publish failure degrades durability, not retrievability.
      this.opts.log?.(
        `[spill] artifact publish failed (non-fatal, local copy is readable): ${(err as Error).message}`,
      );
    }

    return localPath;
  }
}
