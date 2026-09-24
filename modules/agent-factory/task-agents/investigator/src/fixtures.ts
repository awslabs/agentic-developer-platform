/**
 * Loader for the published contract fixtures — Task API T5 (#5798).
 *
 * The fixtures under `docs/task-api/contracts/v1/fixtures/` are T0's (#5793)
 * frozen corpus and this package consumes them **unchanged**. That is the point:
 * the validator in `protocol.ts` has no JSON Schema library, so the only thing
 * tying it to the contract is that both are checked against the same bytes. If a
 * fixture is edited to make a test pass, the binding is gone and the test is
 * measuring nothing — so these are read from their canonical location rather than
 * copied into the package.
 */

import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/** The `$fixture` envelope every published fixture carries. */
export interface FixtureEnvelope {
  description: string;
  schema: string;
  expect: 'valid' | 'invalid';
  reason?: string;
  violates?: string[];
  owner: string;
  criteria: string[];
}

export interface Fixture {
  /** File name, used in test titles so a failure names the fixture. */
  readonly name: string;
  readonly meta: FixtureEnvelope;
  /** The fixture body with the `$fixture` envelope removed — what a peer would send. */
  readonly body: Record<string, unknown>;
}

/**
 * Walk up from this module to the repository root.
 *
 * Resolved by looking for the fixtures directory rather than by counting `..`
 * segments, because this file is imported from `dist/` at a different depth than
 * `src/` and a hard-coded depth would silently break when either moves.
 */
function repoRoot(): string {
  let dir = dirname(fileURLToPath(import.meta.url));
  for (let i = 0; i < 12; i += 1) {
    if (existsSync(join(dir, 'docs', 'task-api', 'contracts', 'v1', 'fixtures'))) {
      return dir;
    }
    const parent = resolve(dir, '..');
    if (parent === dir) {
      break;
    }
    dir = parent;
  }
  throw new Error('could not locate the task-api contract fixtures from this module');
}

export const FIXTURE_ROOT = join(repoRoot(), 'docs', 'task-api', 'contracts', 'v1', 'fixtures');

/**
 * Load fixtures from `valid/` or `invalid/` whose file name starts with `prefix`.
 *
 * Returns them sorted so test output is stable between runs.
 */
export function loadFixtures(kind: 'valid' | 'invalid', prefix: string): Fixture[] {
  const dir = join(FIXTURE_ROOT, kind);
  return readdirSync(dir)
    .filter((name) => name.startsWith(prefix) && name.endsWith('.json'))
    .sort()
    .map((name) => loadFixture(kind, name));
}

/** Load one fixture by kind and file name. */
export function loadFixture(kind: 'valid' | 'invalid', name: string): Fixture {
  const raw = JSON.parse(readFileSync(join(FIXTURE_ROOT, kind, name), 'utf8')) as Record<
    string,
    unknown
  >;
  const meta = raw['$fixture'] as FixtureEnvelope | undefined;
  if (meta === undefined) {
    throw new Error(`fixture ${kind}/${name} has no $fixture envelope`);
  }
  if (meta.expect !== kind) {
    throw new Error(`fixture ${kind}/${name} declares expect: ${meta.expect}`);
  }
  const body: Record<string, unknown> = { ...raw };
  delete body['$fixture'];
  return { name, meta, body };
}

/**
 * Serialize a fixture body the way a peer would put it on the wire.
 *
 * `parseHostFrame` takes a line rather than an object, so tests exercise the real
 * entry point — including the byte-bound check and JSON parsing — instead of
 * reaching past it into the per-frame validators.
 */
export function asLine(fixture: Fixture): string {
  return JSON.stringify(fixture.body);
}
