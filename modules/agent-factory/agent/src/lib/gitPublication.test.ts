import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { headIsPublished } from './gitPublication';

it('distinguishes published PR commits from local work and stale tracking refs', () => {
  const dir = mkdtempSync(join(tmpdir(), 'git-publication-'));
  const repo = join(dir, 'repo');
  const remote = join(dir, 'remote');
  const git = (...args: string[]) => execFileSync('git', args, { cwd: repo, stdio: 'pipe' });
  try {
    execFileSync('git', ['init', '-q', repo]);
    execFileSync('git', ['init', '--bare', '-q', remote]);
    git('config', 'user.name', 'Test');
    git('config', 'user.email', 'test@example.invalid');
    git('checkout', '-b', 'main');
    git('commit', '--allow-empty', '-qm', 'base');
    git('remote', 'add', 'origin', remote);
    git('push', '-qu', 'origin', 'main');
    git('checkout', '-b', 'agent/issue-1');
    writeFileSync(join(repo, 'code'), 'repair');
    git('add', 'code');
    git('commit', '-qm', 'repair');
    expect(headIsPublished(repo)).toBe(false);
    git('push', '-qu', 'origin', 'HEAD');
    expect(headIsPublished(repo)).toBe(true); // Ahead of main, but safely on GitHub.
    git('commit', '--allow-empty', '-qm', 'not yet pushed');
    expect(headIsPublished(repo)).toBe(false);
    git('push', '-q', 'origin', 'HEAD');
    execFileSync('git', ['--git-dir', remote, 'update-ref', '-d', 'refs/heads/agent/issue-1']);
    expect(headIsPublished(repo)).toBe(false); // origin/agent/issue-1 is stale.
    git('checkout', '--detach');
    expect(headIsPublished(repo)).toBe(false);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
