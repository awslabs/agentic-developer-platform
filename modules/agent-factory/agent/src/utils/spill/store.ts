/**
 * SpillStore port — durable destinations for oversized tool output (#4179).
 *
 * The spill hook needs to persist a payload somewhere the model can read it
 * back from. The two call sites have very different storage available:
 *
 *   - The GitHub-issue worker has the run's workspace directory and (best
 *     effort) a run-scoped S3 prefix.  `TmpSpillStore` below.
 *   - The chat agent has a real artifact abstraction with session scoping and
 *     presigned URLs.  See `artifact-store-adapter.ts`.
 *
 * Hence one narrow interface, two implementations. The interface is
 * deliberately minimal: the hook hands over a key and a body, and gets back a
 * *locator* — a string the model can act on directly (here, a filesystem path
 * it can `Read`).
 */
import * as fs from 'fs';
import * as path from 'path';

export interface SpillStore {
  /**
   * Persist `body` under `key` and return a locator the model can act on.
   *
   * Implementations MUST return a locator that the agent can retrieve with a
   * tool it already has (in practice: an absolute path readable with `Read`).
   * A locator the agent cannot act on is worse than not spilling at all —
   * see the impact table in #4179.
   *
   * Implementations MAY throw. The caller (the spill hook) treats any throw as
   * "do not spill" and passes the original output through unmodified.
   */
  spill(key: string, body: string): Promise<string>;
}

/** Directory name created under the store root to hold spilled payloads. */
export const SPILL_DIR_NAME = '.adp-spill';

/**
 * Filesystem-backed spill store for the GitHub-issue worker.
 *
 * Writes to `<root>/.adp-spill/<key>` and returns the absolute path. `Read` is
 * already in the worker's tool allowlist, so that path is immediately
 * actionable by the model with no allowlist change and no new tool.
 *
 * Optionally also uploads to S3 as a **best-effort** durability leg so the
 * payload survives the pod. That leg is intentionally decoupled: it never
 * changes the returned locator and never throws, because the worker's S3
 * fallback configuration is known-unreliable (#4184). The `/tmp` + `Read`
 * path is sufficient on its own.
 */
export class TmpSpillStore implements SpillStore {
  private excludeRegistered = false;

  constructor(
    private readonly root: string,
    private readonly opts: {
      /** Best-effort S3 upload. Errors are logged and swallowed. */
      uploadToS3?: (key: string, body: string) => Promise<void>;
      log?: (msg: string) => void;
    } = {},
  ) {}

  /**
   * Keep spilled payloads out of the agent's commits.
   *
   * The worker's spill root is the cloned repo, and the worker runs
   * `git add -A` (components/GitHubClient.ts) — so without this, a spilled
   * build log would be staged into the agent's own PR. `.git/info/exclude` is
   * a per-clone gitignore that isn't tracked, which is the same mechanism
   * entrypoint.py uses for `.adp-rules/` and `.claude/skills/`.
   *
   * Best-effort and idempotent: if the root isn't a git repo (the chat path,
   * or a bare workspace) there is nothing to exclude and nothing to fail.
   */
  private async registerGitExclude(): Promise<void> {
    if (this.excludeRegistered) return;
    this.excludeRegistered = true;
    try {
      const excludeFile = path.join(this.root, '.git', 'info', 'exclude');
      if (!fs.existsSync(path.join(this.root, '.git'))) return;
      const entry = `${SPILL_DIR_NAME}/`;
      const existing = fs.existsSync(excludeFile)
        ? await fs.promises.readFile(excludeFile, 'utf8')
        : '';
      if (existing.split('\n').includes(entry)) return;
      await fs.promises.mkdir(path.dirname(excludeFile), { recursive: true });
      await fs.promises.appendFile(excludeFile, `\n${entry}\n`);
    } catch (err) {
      this.opts.log?.(
        `[spill] could not register git exclude for ${SPILL_DIR_NAME}/ (non-fatal): ${(err as Error).message}`,
      );
    }
  }

  async spill(key: string, body: string): Promise<string> {
    await this.registerGitExclude();
    const dir = path.join(this.root, SPILL_DIR_NAME);
    const dest = path.join(dir, key);

    // Guard against a key escaping the spill directory. Keys are generated
    // internally (tool name + tool_use_id), never model-supplied, but the
    // check is cheap and keeps that property from silently regressing.
    const resolvedDir = path.resolve(dir);
    const resolvedDest = path.resolve(dest);
    if (!resolvedDest.startsWith(resolvedDir + path.sep)) {
      throw new Error(`Spill key escapes the spill directory: ${key}`);
    }

    await fs.promises.mkdir(path.dirname(resolvedDest), { recursive: true });
    await fs.promises.writeFile(resolvedDest, body, 'utf8');

    // Best-effort durability leg. Deliberately not awaited into the failure
    // path: an S3 error must not turn a successful local spill into a
    // pass-through, let alone into a run failure.
    if (this.opts.uploadToS3) {
      try {
        await this.opts.uploadToS3(key, body);
      } catch (err) {
        this.opts.log?.(
          `[spill] S3 upload failed (non-fatal, local copy is authoritative): ${(err as Error).message}`,
        );
      }
    }

    return resolvedDest;
  }
}
