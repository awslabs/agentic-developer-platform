import { execFileSync } from 'child_process';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import * as path from 'path';
import { pathToFileURL } from 'url';
import {
  configureMemory,
  ensureAdpBranch,
  readComponentContext,
  writeAgentRecord,
  writeComponentRecord,
} from './memory';

// Exercise the actual Git ref configuration produced by the hosted clone.
// Mocking git fetch as "success" is what hid the missing origin/adp ref.
describe('memory in hosted shallow clones', () => {
  let root: string;
  let remote: string;
  let seed: string;
  let work: string;
  let gitEnv: NodeJS.ProcessEnv;
  let logs: jest.Mock;

  function git(cwd: string, ...args: string[]): string {
    return execFileSync('git', args, {
      cwd, env: gitEnv, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 10_000,
    }).trim();
  }

  function configureGit(cwd: string): void {
    // Match the worker's explicit identity setup. Jest's process.env is a copy;
    // changing it alone does not configure child_process calls inside memory.ts.
    git(cwd, 'config', 'user.name', 'Memory regression');
    git(cwd, 'config', 'user.email', 'test@example.invalid');
    git(cwd, 'config', 'commit.gpgsign', 'false');
    git(cwd, 'config', 'core.hooksPath', path.join(root, 'empty-hooks'));
    git(cwd, 'config', 'protocol.file.allow', 'always');
  }

  function clone(name = 'work'): string {
    const destination = path.join(root, name);
    git(root, 'clone', '--depth=20', pathToFileURL(remote).href, destination);
    configureGit(destination);
    expect(git(destination, 'rev-parse', '--is-shallow-repository')).toBe('true');
    return destination;
  }

  function configure(cwd = work, issueNumber = '42'): void {
    configureMemory({ cwd, issueNumber, agentType: 'reviewer', log: logs });
  }

  function seedMemory(): string {
    git(seed, 'checkout', '--orphan', 'adp');
    git(seed, 'rm', '-rf', '.');
    mkdirSync(path.join(seed, 'agent_context/components/general'), { recursive: true });
    writeFileSync(path.join(seed, 'agent_context/components/general/prior.md'), 'prior knowledge\n');
    git(seed, 'add', '.');
    git(seed, 'commit', '-m', 'prior context');
    git(seed, 'push', 'origin', 'adp');
    return git(seed, 'rev-parse', 'HEAD');
  }

  beforeEach(() => {
    root = mkdtempSync(path.join(tmpdir(), 'adp-memory-git-'));
    gitEnv = {
      ...process.env,
      GIT_CONFIG_GLOBAL: path.join(root, 'empty-gitconfig'), GIT_CONFIG_NOSYSTEM: '1',
    };
    remote = path.join(root, 'remote.git');
    seed = path.join(root, 'seed');
    git(root, 'init', '--bare', '--initial-branch=main', remote);
    git(root, 'init', '--initial-branch=main', seed);
    configureGit(seed);
    writeFileSync(path.join(seed, 'README.md'), 'source tree\n');
    git(seed, 'add', '.');
    git(seed, 'commit', '-m', 'main');
    // Exceed the hosted depth so the clone has an actual shallow boundary.
    for (let index = 0; index < 20; index++) {
      git(seed, 'commit', '--allow-empty', '-m', `main history ${index}`);
    }
    git(seed, 'remote', 'add', 'origin', remote);
    git(seed, 'push', 'origin', 'main');
    logs = jest.fn();
    work = clone();
    configure();
  });

  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  it('limits memory by timestamp rather than issue number, with legacy names last', async () => {
    seedMemory();
    const folder = path.join(seed, 'agent_context/components/general');
    for (const [name, content] of Object.entries({
      'issue-999_2026-01-01T12-00.md': 'old',
      'issue-2_2026-09-01T12-00.md': 'new',
      'run_issue-1_2026-08-01T12-00.md': 'middle',
      'aaa.md': 'legacy',
    })) writeFileSync(path.join(folder, name), content);
    git(seed, 'add', '.');
    git(seed, 'commit', '-m', 'mixed memory chronology');
    git(seed, 'push', 'origin', 'adp');
    configureMemory({ cwd: work, issueNumber: '42', agentType: 'reviewer', log: logs, maxFilesPerFolder: 3 });
    expect(await readComponentContext('general')).toEqual(['new', 'middle', 'old']);
    configureMemory({ cwd: work, issueNumber: '42', agentType: 'reviewer', log: logs, maxFilesPerFolder: 5 });
    expect(await readComponentContext('general')).toEqual(['new', 'middle', 'old', 'prior knowledge', 'legacy']);
  });

  it('reads shell metacharacters and quoted filenames as literal Git paths', async () => {
    seedMemory();
    const names = ['$(touch memory-injected).md', '`touch memory-injected`.md', 'quote" and space.md', 'line\nbreak.md'];
    for (const [index, name] of names.entries()) {
      writeFileSync(path.join(seed, 'agent_context/components/general', name), `literal-${index}`);
    }
    git(seed, 'add', '.');
    git(seed, 'commit', '-m', 'literal filenames');
    git(seed, 'push', 'origin', 'adp');
    expect(await readComponentContext('general')).toEqual(expect.arrayContaining(names.map((_, i) => `literal-${i}`)));
    expect(existsSync(path.join(work, 'memory-injected'))).toBe(false);
  });

  it('loads an existing orphan branch without attempting to recreate it', async () => {
    const memorySha = seedMemory();
    const mainSha = git(work, 'rev-parse', 'HEAD');
    await ensureAdpBranch();
    await ensureAdpBranch();
    expect(git(work, 'rev-parse', 'origin/adp')).toBe(memorySha);
    expect(git(remote, 'rev-parse', 'adp')).toBe(memorySha);
    expect(git(work, 'rev-parse', 'HEAD')).toBe(mainSha);
    expect(await readComponentContext('general')).toEqual(['prior knowledge']);
    expect(logs.mock.calls.some(([, message]) => message.includes('creating orphan'))).toBe(false);
  });

  it('reads memory directly from a fresh shallow clone', async () => {
    seedMemory();
    expect(await readComponentContext('general')).toEqual(['prior knowledge']);
    expect(git(work, 'branch', '--show-current')).toBe('main');
  });

  it('publishes records, preserves prior history and restores the work branch', async () => {
    const memorySha = seedMemory();
    git(work, 'checkout', '-b', 'agent/issue-42');
    const workSha = git(work, 'rev-parse', 'HEAD');
    await ensureAdpBranch();
    await writeComponentRecord('general', 'new knowledge\n');
    await writeAgentRecord('reviewer', 'review summary\n');
    expect(git(work, 'branch', '--show-current')).toBe('agent/issue-42');
    expect(git(work, 'rev-parse', 'HEAD')).toBe(workSha);
    expect(readFileSync(path.join(work, 'README.md'), 'utf8')).toBe('source tree\n');
    expect(git(remote, 'merge-base', '--is-ancestor', memorySha, 'adp')).toBe('');
    const files = git(remote, 'ls-tree', '-r', '--name-only', 'adp').split('\n');
    expect(files).toContain('agent_context/components/general/prior.md');
    const record = files.find(f => f.includes('components/general/issue-42_'))!;
    expect(git(remote, 'show', `adp:${record}`)).toBe('new knowledge');
    expect(files.some(f => f.includes('agents/reviewer/run_issue-42_'))).toBe(true);
    expect(logs.mock.calls.filter(([level]) => level === 'WARN')).toEqual([]);
  });

  it('refreshes the ref between independent writers', async () => {
    seedMemory();
    const other = clone('other');
    await ensureAdpBranch();
    await writeComponentRecord('general', 'first writer');
    configure(other, '43');
    await writeComponentRecord('general', 'second writer');
    const records = await readComponentContext('general');
    expect(records).toEqual(expect.arrayContaining(['first writer', 'second writer', 'prior knowledge']));
    expect(logs.mock.calls.filter(([level]) => level === 'WARN')).toEqual([]);
  });

  it('initializes a genuinely absent branch and can immediately read and write it', async () => {
    await ensureAdpBranch();
    await writeComponentRecord('general', 'first knowledge');
    expect(await readComponentContext('general')).toEqual(['first knowledge']);
    expect(git(work, 'branch', '--show-current')).toBe('main');
    expect(git(remote, 'show', 'main:README.md')).toBe('source tree');
    expect(logs.mock.calls.filter(([level]) => level === 'WARN')).toEqual([]);
  });

  it('does not treat a failed remote lookup as an absent branch', async () => {
    seedMemory();
    writeFileSync(path.join(work, 'README.md'), 'uncommitted source\n');
    git(work, 'remote', 'set-url', 'origin', path.join(root, 'unreachable.git'));
    await expect(ensureAdpBranch()).rejects.toThrow();
    expect(readFileSync(path.join(work, 'README.md'), 'utf8')).toBe('uncommitted source\n');
    expect(git(work, 'branch', '--show-current')).toBe('main');
    expect(git(work, 'branch', '--list', 'adp')).toBe('');
  });

  it('does not write against a stale memory ref when the fetch fails', async () => {
    const memorySha = seedMemory();
    await ensureAdpBranch();
    git(work, 'remote', 'set-url', 'origin', path.join(root, 'unreachable.git'));
    await writeComponentRecord('general', 'must not be committed');
    expect(git(work, 'branch', '--show-current')).toBe('main');
    expect(git(work, 'branch', '--list', 'adp')).toBe('');
    expect(git(remote, 'rev-parse', 'adp')).toBe(memorySha);
    expect(logs.mock.calls.some(([level]) => level === 'WARN')).toBe(true);
  });
});
