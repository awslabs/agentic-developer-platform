import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, symlinkSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { COMMON_RULES, phaseRules, projectRules, renderProjection, resolveRuleReferences,
  snapshotSkill, materializeSkill } from './projection.js';
import { snapshotPersona, verifySnapshot } from './persona.js';
const rules = fileURLToPath(new URL('../../rules/', import.meta.url));

for (const persona of ['developer', 'reviewer', 'operations', 'architect', 'product', 'pm', 'aidlc', 'intent-refinement']) {
  test(`${persona}: maintained source selection and deterministic frozen references`, () => {
    const projection = projectRules(rules, persona, [...COMMON_RULES, `personas/${persona}.md`, ...phaseRules(rules, persona)]);
    const refs = { ...projection, sources: projection.sources.map(({ path, sha256 }) => ({ path, sha256 })) };
    const raw = JSON.parse(readFileSync(new URL(`../personas/${persona}.json`, import.meta.url), 'utf8'));
    const snapshot = snapshotPersona(JSON.stringify({ ...raw, sharedRules: refs }), new Map());
    assert.deepEqual(verifySnapshot(snapshot), snapshot);
    const text = resolveRuleReferences(rules, refs);
    assert.ok(text.includes(readFileSync(join(rules, `personas/${persona}.md`), 'utf8')));
    assert.ok(Buffer.byteLength(text + snapshot.instructions) < raw.limits.maxContextBytes);
    assert.ok(Buffer.byteLength(JSON.stringify(snapshot)) < 16000, 'Transport must contain references, not duplicated rule bodies');
    assert.deepEqual(projectRules(rules, persona, projection.sources.map(s => s.path)), projection);
    assert.deepEqual(projection.sources.filter(s => s.path.startsWith('personas/') && !s.path.includes('/shared/')).map(s => s.path), [`personas/${persona}.md`]);
    assert.throws(() => resolveRuleReferences(rules, { ...refs, sources: refs.sources.map((s, i) => i ? s : { ...s, sha256: '0'.repeat(64) }) }), /revision/);
    assert.throws(() => snapshotPersona(JSON.stringify({ ...raw, sharedRules: { ...refs, persona: 'other' } }), new Map()), /another persona/);
  });
}

test('catalogue compiler binds every shipped candidate to the actual maintained files', () => {
  const folder = fileURLToPath(new URL('../personas/', import.meta.url));
  const definitions = readdirSync(folder).filter(f => f.endsWith('.json')).sort().map(f => join(folder, f));
  const compiled = JSON.parse(execFileSync(process.execPath, [fileURLToPath(new URL('../scripts/catalogue.mjs', import.meta.url)), ...definitions], { maxBuffer: 2 * 1024 * 1024 }).toString());
  assert.equal(compiled.snapshots.length, 8);
  for (const snapshot of compiled.snapshots) {
    const verified = verifySnapshot(snapshot);
    const definition = JSON.parse(verified.definition);
    resolveRuleReferences(rules, definition.sharedRules);
  }
});

test('missing files, traversal and symlink escapes refuse; old content stays frozen', () => {
  const root = mkdtempSync(join(tmpdir(), 'adp-projection-test-'));
  try {
    mkdirSync(join(root, 'trusted'));
    writeFileSync(join(root, 'trusted/rule.md'), 'original');
    const before = projectRules(join(root, 'trusted'), 'developer', ['rule.md']);
    writeFileSync(join(root, 'outside.md'), 'outside');
    symlinkSync(join(root, 'outside.md'), join(root, 'trusted/link.md'));
    for (const path of ['missing.md', '../outside.md', '/outside.md', 'link.md']) {
      assert.throws(() => projectRules(join(root, 'trusted'), 'developer', [path]));
    }
    writeFileSync(join(root, 'trusted/rule.md'), 'changed');
    assert.equal(renderProjection(before), '## ADP source: rule.md\noriginal');
    assert.throws(() => resolveRuleReferences(join(root, 'trusted'), before), /revision/);
    assert.notEqual(projectRules(join(root, 'trusted'), 'developer', ['rule.md']).sources[0]!.sha256, before.sources[0]!.sha256);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('portable skill preserves relative scripts and uses frozen executable content', () => {
  const root = mkdtempSync(join(tmpdir(), 'adp-skill-test-'));
  try {
    mkdirSync(join(root, 'skills/check/scripts'), { recursive: true });
    mkdirSync(join(root, 'run'));
    writeFileSync(join(root, 'skills/check/SKILL.md'), '---\nname: check\ndescription: Verify the fixture.\n---\nRun node scripts/check.js.');
    writeFileSync(join(root, 'skills/check/scripts/check.js'), 'process.stdout.write("verified")');
    const bundle = snapshotSkill(join(root, 'skills'), 'check');
    const path = materializeSkill(bundle, join(root, 'run'));
    writeFileSync(join(root, 'skills/check/scripts/check.js'), 'throw new Error("changed")');
    assert.equal(execFileSync(process.execPath, [join(path, '../scripts/check.js')]).toString(), 'verified');
    assert.throws(() => materializeSkill({ ...bundle, digest: '0'.repeat(64) }, join(root, 'run')), /binding/);
    symlinkSync(join(root, 'run'), join(root, 'skills/check/escape'));
    assert.throws(() => snapshotSkill(join(root, 'skills'), 'check'), /Symlink/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
