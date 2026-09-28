import { execFileSync } from 'node:child_process';

/** Check the live branch ref: commits ahead of main may already be safely pushed. */
export function headIsPublished(cwd: string): boolean {
  const git = (...args: string[]) => execFileSync('git', args, {
    cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 15_000,
  }).trim();
  try {
    const branch = git('symbolic-ref', '--quiet', '--short', 'HEAD');
    const head = git('rev-parse', 'HEAD');
    const ref = `refs/heads/${branch}`;
    return git('ls-remote', '--exit-code', 'origin', ref).split('\n')
      .some(line => line === `${head}\t${ref}`);
  } catch {
    // A detached/deleted/unreachable branch is not evidence of preservation.
    return false;
  }
}
