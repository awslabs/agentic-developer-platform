import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { githubTools } from './github-tools.js';

for (const persona of ['agent-codex-product', 'agent-codex-pm', 'agent-codex-intent-refinement'])
test(`${persona}: repository progress identifies real reads and outcomes without content`, async () => {
  const root = mkdtempSync(join(tmpdir(), 'adp-github-tools-'));
  const previous = process.env.WORK_DIR;
  process.env.WORK_DIR = root;
  const git = (...args: string[]) => execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
  try {
    git('init', '-q');
    writeFileSync(join(root, 'evidence.txt'), 'committed evidence');
    mkdirSync(join(root, 'source'));
    writeFileSync(join(root, 'source/large.txt'), Array.from({ length: 1000 }, (_, i) => `line ${i + 1}: ${'x'.repeat(100)}`).join('\n'));
    git('add', '.');
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture');
    const revision = git('rev-parse', 'HEAD');
    writeFileSync(join(root, 'evidence.txt'), 'uncommitted replacement');
    let effects = 0;
    const progress: Array<{ text: string; detail: { id: string; state: string } }> = [];
    const tools = githubTools({ persona, repository: 'owner/repo', revision,
      capabilities: ['repository.read'] }, async (_name, _args, work) => { effects++; return work(); }, (text, detail) => progress.push({ text, detail }));
    const signal = AbortSignal.timeout(10000);
    assert.deepEqual(tools.definitions.map(tool => tool.name), ['repository_file', 'repository_files']);
    assert.equal((await tools.execute('repository_file', { path: 'evidence.txt' }, signal)).content, 'committed evidence');
    assert.equal((await tools.execute('repository_files', {}, signal)).content.trim(), 'evidence.txt\nsource');
    assert.equal((await tools.execute('repository_files', { directory: 'source' }, signal)).content, 'large.txt');
    const chunk = await tools.execute('repository_file', { path: 'source/large.txt', start_line: 501, max_lines: 2 }, signal);
    assert.equal(chunk.content, `line 501: ${'x'.repeat(100)}\nline 502: ${'x'.repeat(100)}`);
    const missing = await tools.execute('repository_file', { path: 'missing.txt' }, signal);
    assert.equal(missing.isError, true);
    assert.equal((await tools.execute('repository_file', { path: 'evidence.txt' }, signal)).content, 'committed evidence');
    for (const path of ['../secret', '/etc/passwd', '.git/config', 'x/../../secret', 'file\nname']) {
      await assert.rejects(tools.execute('repository_file', { path }, signal));
    }
    await assert.rejects(tools.execute('shell', { command: 'env' }, signal));
    assert.equal(effects, 6);
    assert.equal(progress.length, 12);
    assert.ok(progress.some(event => event.text === 'Reading source/large.txt (lines 501–502)'));
    assert.ok(progress.some(event => event.text === 'Listed source (directory entries 1–100)'));
    assert.ok(progress.some(event => event.text.includes('missing.txt') && event.detail.state === 'failed'));
    assert.ok(!JSON.stringify(progress).includes('committed evidence'));
    for (let i = 0; i < progress.length; i += 2) {
      assert.equal(progress[i]!.detail.id, progress[i + 1]!.detail.id);
      assert.equal(progress[i]!.detail.state, 'running');
      assert.notEqual(progress[i + 1]!.detail.state, 'running');
    }
  } finally {
    if (previous === undefined) delete process.env.WORK_DIR; else process.env.WORK_DIR = previous;
    rmSync(root, { recursive: true, force: true });
  }
});

test('capabilities and fixed revision are required before repository access', () => {
  const execute = async () => { throw new Error('must not execute'); };
  const input = { persona: 'agent-codex-intent-refinement', repository: 'owner/repo', revision: 'a'.repeat(40), capabilities: [] };
  assert.deepEqual(githubTools(input, execute).definitions, []);
  assert.throws(() => githubTools({ ...input, revision: '--help' }, execute), /revision/);
});
