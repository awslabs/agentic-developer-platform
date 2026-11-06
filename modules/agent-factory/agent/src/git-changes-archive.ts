import { execFileSync } from 'child_process';
import { lstatSync, realpathSync } from 'fs';
import path from 'path';

/** Archive literal Git paths, including newlines, metacharacters and leading '-'. */
export function archiveGitChanges(cwd: string, archivePath: string): string[] {
  const candidates = new Set<string>();
  for (const revisions of [['HEAD'], ['--cached'], ['origin/main..HEAD']]) {
    try {
      const output = execFileSync('git', ['diff', '--name-only', '-z', ...revisions, '--'], {
        cwd, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024,
      });
      for (const name of output.split('\0')) if (name) candidates.add(name);
    } catch (error) {
      // A freshly cloned repository may have no origin/main. Other failures
      // must not silently discard the run's only copy of changed work.
      if (revisions[0] !== 'origin/main..HEAD') throw error;
    }
  }
  const root = realpathSync(cwd);
  const files = [...candidates].filter(name => {
    if (path.isAbsolute(name) || name.split('/').includes('..')) return false;
    try {
      const file = path.resolve(root, name);
      // Reject traversal via an intermediate symlink. A leaf symlink is safe:
      // tar stores the link itself, never the target (no --dereference).
      const parent = realpathSync(path.dirname(file));
      if (parent !== root && !parent.startsWith(root + path.sep)) return false;
      const stat = lstatSync(file);
      return stat.isFile() || stat.isSymbolicLink();
    } catch { return false; } // deleted files have no bytes to preserve
  });
  if (files.length) {
    execFileSync('tar', ['czf', archivePath, '--null', '--verbatim-files-from', '--files-from=-'], {
      cwd: root, input: Buffer.from(files.join('\0') + '\0'), stdio: ['pipe', 'pipe', 'pipe'],
    });
  }
  return files;
}
