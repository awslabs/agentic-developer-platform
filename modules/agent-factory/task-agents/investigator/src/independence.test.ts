/**
 * Independence of the task agent — Task API T5 (#5798), T5-AC01 and T5-AC03.
 *
 * Two properties are asserted here that no behavioural test can establish:
 *
 * 1. **No GitHub or credential path exists.** T5-AC01 requires the persona to
 *    complete useful work with GitHub credentials absent and GitHub access
 *    denied, with zero GitHub requests observed. Observing zero requests in one
 *    run only proves that run made none; the absence of any code able to make one
 *    proves no run can. So the shipped sources are scanned.
 *
 * 2. **No agent SDK dependency.** T5-AC03 requires the image to keep working
 *    unchanged Claude and Codex packages, with legacy execution not initializing
 *    task-only dependencies. The reciprocal — this package not pulling in theirs —
 *    is what keeps the dependency trees isolated inside one image.
 *
 * Follows the pattern established by `modules/agent-factory/codex-reviewer/src/independence.test.ts`.
 *
 * Test files are deliberately excluded from the source scan: a test that names a
 * forbidden pattern in order to assert its absence would match itself, so
 * including them would make this check fail for the wrong reason (and inviting
 * someone to weaken the pattern to fix it).
 */

import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { test, describe } from 'node:test';

const packageRoot = join(import.meta.dirname, '..');

/** Shipped runtime sources: everything in `src/` that is not a test or fixture loader. */
function runtimeSources(): Array<{ name: string; source: string }> {
  const srcDir = join(packageRoot, 'src');
  return readdirSync(srcDir)
    .filter((name) => name.endsWith('.ts') && !name.endsWith('.test.ts') && name !== 'fixtures.ts')
    .sort()
    .map((name) => ({ name, source: readFileSync(join(srcDir, name), 'utf8') }));
}

describe('T5-AC03: dependency isolation', () => {
  test('declares no runtime dependencies at all', () => {
    // The strongest available form of "no agent SDK dependency": there is nothing
    // to install at runtime, so no version of any shared library can be pulled
    // into the image on this package's behalf.
    const manifest = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8')) as {
      dependencies?: Record<string, string>;
      devDependencies?: Record<string, string>;
    };
    assert.deepEqual(manifest.dependencies ?? {}, {});
    // The build toolchain is dev-only and pruned from the shipped layer.
    assert.deepEqual(Object.keys(manifest.devDependencies ?? {}).sort(), ['@types/node', 'typescript']);
  });

  test('the lockfile contains only the build toolchain', () => {
    const lock = JSON.parse(readFileSync(join(packageRoot, 'package-lock.json'), 'utf8')) as {
      packages: Record<string, unknown>;
    };
    const installed = Object.keys(lock.packages)
      .filter((key) => key.startsWith('node_modules/'))
      .map((key) => key.replace('node_modules/', ''))
      .sort();
    assert.deepEqual(installed, ['@types/node', 'typescript', 'undici-types']);
  });

  test('no source imports an agent SDK or another agent package', () => {
    for (const { name, source } of runtimeSources()) {
      assert.doesNotMatch(source, /claude-agent-sdk/, `${name} must not import the Claude agent SDK`);
      assert.doesNotMatch(source, /@anthropic-ai\//, `${name} must not import a provider SDK`);
      // A relative import escaping the package would reach a sibling agent's
      // sources and defeat the isolation regardless of what package.json says.
      assert.doesNotMatch(source, /from ['"]\.\.\/\.\.\//, `${name} must not import across packages`);
    }
  });

  test('every import is either package-local or a Node built-in', () => {
    const importPattern = /(?:from|import)\s+['"]([^'"]+)['"]/g;
    for (const { name, source } of runtimeSources()) {
      for (const match of source.matchAll(importPattern)) {
        const specifier = match[1] ?? '';
        const local = specifier.startsWith('./');
        const builtin = specifier.startsWith('node:');
        assert.ok(local || builtin, `${name} imports ${specifier}, which is neither local nor built-in`);
      }
    }
  });
});

describe('T5-AC01: no GitHub or credential dependency', () => {
  test('no source reads a credential from the environment', () => {
    for (const { name, source } of runtimeSources()) {
      assert.doesNotMatch(
        source,
        /GITHUB_TOKEN|GH_TOKEN|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|ANTHROPIC_API_KEY/,
        `${name} must not reference a credential environment variable`,
      );
      // The child receives no credential, so it has no reason to read process.env
      // at all. Flagging any read keeps the containment property obvious rather
      // than requiring a reviewer to audit which keys are safe.
      assert.doesNotMatch(source, /process\.env/, `${name} must not read process.env`);
    }
  });

  test('no source reaches GitHub', () => {
    for (const { name, source } of runtimeSources()) {
      assert.doesNotMatch(source, /@octokit|Octokit/, `${name} must not use a GitHub client`);
      assert.doesNotMatch(source, /api\.github\.com|github\.com\//, `${name} must not address GitHub`);
      assert.doesNotMatch(
        source,
        /\bgh\s+(?:pr|issue|api|repo)\b/,
        `${name} must not invoke the GitHub CLI`,
      );
    }
  });

  test('no source opens a network connection or spawns a process', () => {
    for (const { name, source } of runtimeSources()) {
      assert.doesNotMatch(source, /from ['"]node:(?:http|https|net|tls|dgram)['"]/, `${name} must not use network modules`);
      assert.doesNotMatch(source, /\bfetch\s*\(/, `${name} must not call fetch`);
      assert.doesNotMatch(source, /node:child_process|spawnSync|execSync/, `${name} must not spawn processes`);
    }
  });

  test('no source writes to the filesystem', () => {
    // The investigator has no checkout and produces no files: its output is
    // frames on stdout. A write here would be evidence of a capability the
    // design excludes.
    for (const { name, source } of runtimeSources()) {
      assert.doesNotMatch(source, /writeFileSync|createWriteStream|\bmkdirSync/, `${name} must not write files`);
    }
  });
});
