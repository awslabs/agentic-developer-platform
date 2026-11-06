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

test('Actions tools require flagged operations capability and use the gateway bridge', async () => {
  const previous = process.env.ADP_ENGINE_OPERATIONS_EXECUTOR;
  try {
    process.env.ADP_ENGINE_OPERATIONS_EXECUTOR = 'true';
    const requests: unknown[] = [];
    const run = githubTools({ persona: 'agent-codex-operations', repository: 'owner/repo', revision: 'a'.repeat(40),
      capabilities: ['actions.write'] }, async (_name, _args, work) => work(), undefined,
      async (name, args) => { requests.push({ name, args }); return '{"dispatched":true}'; });
    assert.deepEqual(run.definitions.map(tool => tool.name), ['actions_dispatch', 'actions_rerun', 'repair_progress']);
    assert.ok(run.definitions.every(tool => /^[a-f0-9]{64}$/.test(tool.schemaDigest ?? '')));
    assert.equal(run.definitions[0]?.readOnly, false);
    assert.equal((await run.execute('actions_dispatch', { workflow: 'deploy.yml' }, AbortSignal.timeout(10000))).content, '{"dispatched":true}');
    assert.deepEqual(requests, [{ name: 'actions_dispatch', args: { workflow: 'deploy.yml' } }]);
    await run.execute('actions_rerun', { run_id: 123 }, AbortSignal.timeout(10000));
    assert.deepEqual(requests[1], { name: 'actions_rerun', args: { run_id: 123 } });
    const progress: string[] = [];
    const refused = githubTools({ persona: 'agent-codex-operations', repository: 'owner/repo', revision: 'a'.repeat(40),
      capabilities: ['actions.write'] }, async (_name, _args, work) => work(), text => progress.push(text),
      async () => { throw new Error('gateway refused'); });
    await assert.rejects(refused.execute('actions_dispatch', { workflow: 'deploy.yml' }, AbortSignal.timeout(10000)), /gateway refused/);
    assert.ok(progress.some(text => text.includes('did not complete')));
    await assert.rejects(run.execute('actions_dispatch', { workflow: '../unsafe.yml' }, AbortSignal.timeout(10000)));
    assert.deepEqual(githubTools({ persona: 'developer', repository: 'owner/repo', revision: 'a'.repeat(40),
      capabilities: ['actions.write'] }, async (_name, _args, work) => work()).definitions, []);
    process.env.ADP_ENGINE_OPERATIONS_EXECUTOR = 'false';
    assert.deepEqual(githubTools({ persona: 'agent-codex-operations', repository: 'owner/repo', revision: 'a'.repeat(40),
      capabilities: ['actions.write'] }, async (_name, _args, work) => work()).definitions, []);
  } finally {
    if (previous === undefined) delete process.env.ADP_ENGINE_OPERATIONS_EXECUTOR;
    else process.env.ADP_ENGINE_OPERATIONS_EXECUTOR = previous;
  }
});
test('Actions reads are bounded, schema-pinned and available without mutation capability', async () => {
  const previous = process.env.ADP_ENGINE_OPERATIONS_EXECUTOR;
  try {
    process.env.ADP_ENGINE_OPERATIONS_EXECUTOR = 'true';
    const input = { persona: 'developer', repository: 'owner/repo', revision: 'a'.repeat(40), capabilities: ['actions.read'] as const };
    const read = githubTools(input, async (_name, _args, work) => work(), undefined,
      async (name) => name === 'actions_logs'
        ? JSON.stringify({ run_id: 42, log_tail: 'x'.repeat(65536), truncated: true })
        : JSON.stringify({ runs: [] }));
    assert.deepEqual(read.definitions.map(tool => tool.name), ['actions_runs', 'actions_run', 'actions_logs']);
    assert.ok(read.definitions.every(tool => tool.readOnly && /^[a-f0-9]{64}$/.test(tool.schemaDigest ?? '')));
    const response = await read.execute('actions_logs', { run_id: 42 }, AbortSignal.timeout(10000));
    assert.ok(Buffer.byteLength(response.content) <= 24576);
    assert.ok(JSON.parse(response.content).log_tail.endsWith('x'));
    await assert.rejects(read.execute('actions_runs', { page: 6 }, AbortSignal.timeout(10000)));
    await assert.rejects(read.execute('actions_run', { run_id: 0 }, AbortSignal.timeout(10000)));
    await assert.rejects(read.execute('actions_dispatch', { workflow: 'deploy.yml' }, AbortSignal.timeout(10000)));
    process.env.ADP_ENGINE_OPERATIONS_EXECUTOR = 'false';
    assert.deepEqual(githubTools(input, async (_name, _args, work) => work()).definitions, []);
  } finally {
    if (previous === undefined) delete process.env.ADP_ENGINE_OPERATIONS_EXECUTOR;
    else process.env.ADP_ENGINE_OPERATIONS_EXECUTOR = previous;
  }
});


test('capabilities and fixed revision are required before repository access', () => {
  const execute = async () => { throw new Error('must not execute'); };
  const input = { persona: 'agent-codex-intent-refinement', repository: 'owner/repo', revision: 'a'.repeat(40), capabilities: [] };
  assert.deepEqual(githubTools(input, execute).definitions, []);
  assert.throws(() => githubTools({ ...input, revision: '--help' }, execute), /revision/);
});

test('repair tools require operations identity and capabilities; reject unsafe edits before execution', async () => {
  const previous = process.env.ADP_ENGINE_OPERATIONS_EXECUTOR;
  let calls = 0;
  const execute = async () => { calls++; return '{}'; };
  try {
    process.env.ADP_ENGINE_OPERATIONS_EXECUTOR = 'true';
    const input = { persona: 'agent-codex-operations', repository: 'owner/repo', revision: 'a'.repeat(40),
      capabilities: ['repository.read', 'repository.write', 'change.create', 'change.merge', 'actions.write'] as const };
    const tools = githubTools(input, execute);
    assert.equal(tools.definitions.filter(tool => tool.name.startsWith('repair_')).length, 9);
    await assert.rejects(tools.execute('repair_progress', { stage: 'diagnose', state: 'done', diagnosis_code: 'secret:raw' }, AbortSignal.timeout(1000)));
    await assert.rejects(tools.execute('repair_write', { path: '../secret', content: 'bad', expected_sha256: null }, AbortSignal.timeout(1000)));
    await assert.rejects(tools.execute('repair_read', { path: 'large.py', limit: 8193 }, AbortSignal.timeout(1000)));
    await assert.rejects(tools.execute('repair_replace', { path: '../secret', old_text: 'old', new_text: 'new', expected_sha256: 'a'.repeat(64) }, AbortSignal.timeout(1000)));
    await assert.rejects(tools.execute('repair_replace', { path: 'large.py', old_text: 'old', new_text: 'new' }, AbortSignal.timeout(1000)));
    await assert.rejects(tools.execute('repair_merge', { pull_number: 1, expected_head: 'main' }, AbortSignal.timeout(1000)));
    assert.equal(calls, 0);
    await tools.execute('repair_read', { path: 'large.py', offset: 8192, expected_sha256: 'a'.repeat(64) }, AbortSignal.timeout(1000));
    await tools.execute('repair_replace', { path: 'large.py', old_text: 'old', new_text: 'new', expected_sha256: 'a'.repeat(64) }, AbortSignal.timeout(1000));
    assert.equal(calls, 2);
    const readOnly = githubTools({ ...input, capabilities: ['repository.read'] }, execute);
    assert.equal(readOnly.definitions.some(tool => tool.name === 'repair_replace'), false);
    const developer = githubTools({ ...input, persona: 'developer' }, execute);
    assert.equal(developer.definitions.filter(tool => tool.name.startsWith('repair_')).length, 0);
    process.env.ADP_ENGINE_OPERATIONS_EXECUTOR = 'false';
    assert.equal(githubTools(input, execute).definitions.filter(tool => tool.name.startsWith('repair_')).length, 0);
  } finally {
    if (previous === undefined) delete process.env.ADP_ENGINE_OPERATIONS_EXECUTOR; else process.env.ADP_ENGINE_OPERATIONS_EXECUTOR = previous;
  }
});

test('story progress is bound to the operations controller and existing artifact capability', async () => {
  const previous = process.env.ADP_ENGINE_OPERATIONS_STORY;
  const calls: unknown[] = [];
  const journal: string[] = [];
  const execute = async (name: string, _args: object, work: () => Promise<string>) => { journal.push(name); return work(); };
  const input = { persona: 'agent-codex-operations', repository: 'owner/repo', revision: 'a'.repeat(40),
    capabilities: ['artifacts.publish'] as const, storyProgress: (digest: string, updates: unknown[]) => { calls.push({ digest, updates }); return { updated: true, progress_only: true }; } };
  try {
    process.env.ADP_ENGINE_OPERATIONS_STORY = 'true';
    const host = githubTools(input, execute);
    assert.deepEqual(host.definitions.map(tool => tool.name), ['story_task_progress']);
    const args = { expected_sha256: 'b'.repeat(64), updates: [{ id: 'AC-t1', status: 'done' }] };
    assert.equal(JSON.parse((await host.execute('story_task_progress', args, AbortSignal.timeout(1000))).content).progress_only, true);
    assert.deepEqual(calls, [{ digest: args.expected_sha256, updates: args.updates }]);
    assert.deepEqual(journal, ['story_task_progress']);
    for (const bad of [ { ...args, expected_sha256: 'bad' }, { ...args, updates: [{ id: 'AC-t1', status: 'accepted' }] },
      { ...args, updates: [{ id: 'new', status: 'open', kind: 'test', title: 'bad\nrow' }] }, { ...args, authority: 'release' } ]) {
      await assert.rejects(host.execute('story_task_progress', bad, AbortSignal.timeout(1000)));
    }
    assert.equal(calls.length, 1);
    for (const candidate of [{ ...input, persona: 'agent-codex-product' }, { ...input, capabilities: [] }, { ...input, storyProgress: undefined }]) {
      assert.equal(githubTools(candidate, execute).definitions.length, 0);
    }
    process.env.ADP_ENGINE_OPERATIONS_STORY = 'false';
    assert.equal(githubTools(input, execute).definitions.length, 0);
  } finally {
    if (previous === undefined) delete process.env.ADP_ENGINE_OPERATIONS_STORY; else process.env.ADP_ENGINE_OPERATIONS_STORY = previous;
  }
});
