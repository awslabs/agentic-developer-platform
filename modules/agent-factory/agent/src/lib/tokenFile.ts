/**
 * GIT_ASKPASS token file (Option B-hybrid — issue #1469).
 *
 * Deliberately a leaf module: only `fs` and `path`. It used to live in
 * `token-refresh.ts`, but that module imports `@octokit/auth-app`, which ships
 * ESM-only and is outside jest's transform set. Any module that needed nothing
 * but `writeTokenFile()` therefore dragged an untransformable dependency into
 * its whole test suite (issue #4272 hit this from `utils/ghPost.ts`). Splitting
 * it out keeps the write path importable from anywhere — including the token
 * broker paths, which have no business loading a local-mint library.
 *
 * `token-refresh.ts` re-exports both symbols, so existing importers are unchanged.
 */

import { writeFileSync, renameSync, mkdirSync } from 'fs';
import { dirname } from 'path';

/**
 * Path to the token file read by GIT_ASKPASS and the gh wrapper at command time.
 * Using /tmp avoids accidental git-add and keeps the token out of the workspace.
 */
export const TOKEN_FILE_PATH = process.env.ADP_TOKEN_FILE || '/tmp/.adp-gh-token';

/**
 * Atomically write the current token to the file read by GIT_ASKPASS and the
 * gh wrapper. Uses write-to-temp + rename for atomicity (no partial reads).
 * File mode 0600 — readable only by the owning user.
 *
 * Non-fatal: if the write fails, the env-var fallback still works for the
 * runtime's own commands (only the SDK subprocess path degrades).
 */
export function writeTokenFile(token: string): void {
  const tmpPath = `${TOKEN_FILE_PATH}.tmp`;
  try {
    mkdirSync(dirname(TOKEN_FILE_PATH), { recursive: true, mode: 0o700 });
    writeFileSync(tmpPath, token, { mode: 0o600 });
    renameSync(tmpPath, TOKEN_FILE_PATH);
  } catch (err) {
    console.error(`[TokenManager] Failed to write token file: ${(err as Error).message}`);
  }
}
