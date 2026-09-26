import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { repositoryEditor, unified } from '../src/coding-driver.mjs';

const content = 'before\n', path = 'sample.py';
const blob = text => createHash('sha1').update(`blob ${Buffer.byteLength(text)}\0`).update(text).digest('hex');
function start() { return { inputs: { repository_snapshot_artifact: 'art-snapshot' }, artifacts: [{ artifact_id: 'art-snapshot', content_type: 'application/json', content: JSON.stringify({ schema_version: '1.0', repository_id: 42, repository: 'owner/repo', commit_sha: 'a'.repeat(40), issue: 1, files: [{ path, blob_sha: blob(content), content }] }) }] }; }

test('coding tools refuse outside files, ambiguous and stale edits', () => {
  const editor = repositoryEditor(start());
  assert.throws(() => editor.read({ path: '../../token' }));
  assert.throws(() => editor.replace({ path, expected_blob_sha: '0'.repeat(40), old_text: 'before', new_text: 'after' }));
  editor.replace({ path, expected_blob_sha: blob(content), old_text: 'before', new_text: 'after' });
  assert.throws(() => editor.replace({ path, expected_blob_sha: blob(content), old_text: 'after', new_text: 'more' }));
  const report = editor.report('Prepared change');
  assert.match(report.recommendations.join(''), /-before\n\+after/);
  assert.match(report.uncertainties[0], /Tests were not executed/);
  assert.deepEqual(report.findings[0].evidence_refs, ['art-snapshot']);
});

test('coding snapshot integrity is required before tools exist', () => {
  const value = start();
  value.artifacts[0].content = value.artifacts[0].content.replace('before', 'tampered');
  assert.throws(() => repositoryEditor(value));
});

test('generated patches apply to actual files including empty and newline changes', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'adp-coding-patch-'));
  try {
    for (const [before, after] of [['before\n', 'after\n'], ['before', 'before\n'], ['a\nb\nc\n', 'a\nchanged\nc\n'], ['', 'created\n'], ['gone\n', '']]) {
      await writeFile(join(directory, path), before);
      const result = spawnSync('git', ['apply', '--unsafe-paths', '-'], { cwd: directory, input: unified(path, before, after), encoding: 'utf8' });
      assert.equal(result.status, 0, result.stderr);
      assert.equal(await readFile(join(directory, path), 'utf8'), after);
    }
  } finally { await rm(directory, { recursive: true, force: true }); }
});
