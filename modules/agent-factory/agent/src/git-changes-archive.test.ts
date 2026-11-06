import { execFileSync } from 'child_process';
import { mkdtempSync, writeFileSync, existsSync, mkdirSync, symlinkSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import path from 'path';
import { archiveGitChanges } from './git-changes-archive';

describe('Git changes archive', () => {
  let root: string;
  const git = (...args: string[]) => execFileSync('git', args, { cwd: root, stdio: 'pipe' });
  beforeEach(() => {
    root = mkdtempSync(path.join(tmpdir(), 'archive-regression-'));
    git('init', '-q');
    writeFileSync(path.join(root, 'baseline'), 'baseline');
    git('add', '.');
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'baseline');
    git('update-ref', 'refs/remotes/origin/main', 'HEAD');
  });
  afterEach(() => rmSync(root, { recursive: true, force: true }));

  it('archives literal metacharacters, leading options, Unicode and newlines without execution', () => {
    const names = ['artifact;touch SENTINEL;#', '$(touch SENTINEL2)', '--checkpoint=1', 'space name', 'new\nline', 'tab\tname', 'café.txt'];
    names.forEach(name => writeFileSync(path.join(root, name), `content:${name}`));
    git('add', '--', ...names);
    const archive = path.join(root, 'changes.tgz');
    expect(archiveGitChanges(root, archive).sort()).toEqual([...names].sort());
    expect(existsSync(path.join(root, 'SENTINEL'))).toBe(false);
    expect(existsSync(path.join(root, 'SENTINEL2'))).toBe(false);
    for (const name of names) {
      expect(execFileSync('tar', ['xOf', archive, '--', name], { encoding: 'utf8' })).toBe(`content:${name}`);
    }
  });

  it('keeps leaf symlinks as links and skips deleted files', () => {
    writeFileSync(path.join(root, 'deleted'), 'deleted');
    git('add', '.');
    rmSync(path.join(root, 'deleted'));
    symlinkSync('/does/not/exist', path.join(root, 'link'));
    git('add', '--', 'link');
    const archive = path.join(root, 'changes.tgz');
    expect(archiveGitChanges(root, archive)).toEqual(['link']);
    mkdirSync(path.join(root, 'out'));
    execFileSync('tar', ['xf', archive, '-C', path.join(root, 'out')]);
    expect(execFileSync('readlink', [path.join(root, 'out/link')], { encoding: 'utf8' }).trim()).toBe('/does/not/exist');
  });
});
