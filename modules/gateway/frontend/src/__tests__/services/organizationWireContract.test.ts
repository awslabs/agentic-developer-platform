/**
 * Source guard: the org transforms declare only fields their own route sends — Issue #4929.
 *
 * This reads source text and Python schemas rather than rendering anything, which needs
 * justifying. The defect it guards was not a wrong value — it was a wrong *expectation*:
 * `transformOrganization()` declared a wire shape that was the UNION of two server schemas
 * and read `role_mappings` + `member_approval_policy` from every response, including the
 * canonical identity route which has never sent either. Every behavioural test passed,
 * because every behavioural test fed the transform a fixture built from the same wrong
 * belief. That is the recorded #3675 shape exactly: frontend types, mocks and the
 * evaluation all agreeing on a field the backend does not send, with nothing comparing the
 * client's expectations against the server's schema.
 *
 * `__tests__/services/admin.test.ts` covers the transforms behaviourally with hand-written
 * fixtures. A hand-written fixture cannot fail this way — it is written from the same
 * assumption as the code — so the check has to be structural, reading the field list out of
 * the .ts and out of the .py and diffing them. Wave 1 check 36 does this against a LIVE
 * response; this is the same assertion in CI, where it fails on the PR that introduces the
 * drift rather than in a later evaluation run.
 *
 * Deliberately a SUBSET assertion, not equality: a transform is free to ignore fields the
 * server sends (`plan`, `channels`, `cognito_client_ids` are all unread today). Consuming a
 * field the server does NOT send is the bug.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const ADMIN_SERVICE = join(process.cwd(), 'src/services/admin.ts');
/** Canonical route: GET/PATCH /api/admin/identity/organizations. */
const CANONICAL_SCHEMA = join(process.cwd(), '../src/admin/identity/schemas.py');
/** Deprecated route: /admin/organizations. */
const DEPRECATED_SCHEMA = join(process.cwd(), '../src/admin/schemas.py');

/**
 * Top-level field names from a transform's inline object-literal parameter type.
 *
 * Mirrors the awk/grep parse in Wave 1 check 36 rather than inventing a second dialect, so
 * that a source change which defeats one defeats both visibly instead of leaving the eval
 * silently parsing nothing. Comment lines are stripped first: the wire types carry prose
 * that names fields (`role_mappings`, `member_approval_policy`) precisely to explain why
 * they are absent, and a parser that counted those would make documenting the rule violate
 * it. Nested object types must stay on one line — the same constraint check 36 imposes.
 */
function declaredWireFields(source: string, functionName: string): string[] {
  const start = source.indexOf(`\nfunction ${functionName}(data: {\n`);
  expect(start, `${functionName}() not found in a parseable form`).toBeGreaterThan(-1);
  const end = source.indexOf('\n}): Organization {', start);
  expect(end, `${functionName}()'s parameter type has no parseable end`).toBeGreaterThan(start);

  const body = source
    .slice(start, end)
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n');

  const fields = [...body.matchAll(/^ {2}([a-zA-Z_][a-zA-Z0-9_]*)\??:/gm)].map((m) => m[1]);
  return [...new Set(fields)].sort();
}

/**
 * Field names declared on a Pydantic response model — the server's source of truth.
 *
 * Scoped to the named class only: it stops at the next class or at `model_config` /
 * `class Config`, so inherited-model or nested-class fields never leak in and inflate the
 * allowed set (which would make the subset assertion below pass when it should fail).
 */
function schemaFields(source: string, className: string): string[] {
  const start = source.indexOf(`class ${className}(BaseModel):`);
  expect(start, `${className} not found`).toBeGreaterThan(-1);

  const rest = source.slice(start);
  const bodyEnd = rest.slice(1).search(/\n(class |\s+model_config|\s+class Config)/);
  const body = bodyEnd === -1 ? rest : rest.slice(0, bodyEnd + 1);

  const fields = [...body.matchAll(/^ {4}([a-z_][a-z0-9_]*)\s*:/gm)].map((m) => m[1]);
  return [...new Set(fields)].sort();
}

describe('organization wire contract — transforms consume only what their route sends (#4929)', () => {
  const service = readFileSync(ADMIN_SERVICE, 'utf8');

  // Without these, a moved file, a renamed function or a defeated regex would make every
  // assertion below pass vacuously — a green test asserting nothing at all, which is the
  // failure mode that let the original defect through.
  it('finds the transforms and schemas it is meant to be guarding', () => {
    expect(declaredWireFields(service, 'transformOrganization').length).toBeGreaterThan(3);
    expect(declaredWireFields(service, 'transformDeprecatedOrganization').length).toBeGreaterThan(3);
    expect(
      schemaFields(readFileSync(CANONICAL_SCHEMA, 'utf8'), 'OrganizationResponse')
    ).toContain('github_installation_ids');
    expect(schemaFields(readFileSync(DEPRECATED_SCHEMA, 'utf8'), 'OrganizationResponse')).toContain(
      'role_mappings'
    );
  });

  it('canonical transform declares no field absent from the canonical response schema', () => {
    const declared = declaredWireFields(service, 'transformOrganization');
    const available = schemaFields(readFileSync(CANONICAL_SCHEMA, 'utf8'), 'OrganizationResponse');

    // This is check 36, in CI. `[]` or the exact fields that drifted.
    expect(declared.filter((field) => !available.includes(field))).toEqual([]);
  });

  it('canonical transform does not consume the two fields the canonical route never sends', () => {
    // Named explicitly as well as covered by the diff above, so the failure message points
    // straight at the regression instead of at a generic set difference.
    const declared = declaredWireFields(service, 'transformOrganization');

    expect(declared).not.toContain('role_mappings');
    expect(declared).not.toContain('member_approval_policy');
  });

  it('deprecated transform declares no field absent from the deprecated response schema', () => {
    const declared = declaredWireFields(service, 'transformDeprecatedOrganization');
    const available = schemaFields(readFileSync(DEPRECATED_SCHEMA, 'utf8'), 'OrganizationResponse');

    expect(declared.filter((field) => !available.includes(field))).toEqual([]);
  });

  it('deprecated transform keeps consuming both fields, which its route does send', () => {
    // The other half of the contract: the fix must not "fix" check 36 by dropping the
    // fields everywhere. Every current read path is on this route and needs them.
    const declared = declaredWireFields(service, 'transformDeprecatedOrganization');

    expect(declared).toContain('role_mappings');
    expect(declared).toContain('member_approval_policy');
  });
});

/**
 * Fixture provenance — Issue #4929, closing the #3675 hole at the layer where it started.
 *
 * The MSW fixtures back the panel tests, so a fixture carrying an invented field teaches
 * the whole suite that the field exists. #3675 was exactly that. These fixtures model the
 * DEPRECATED route (that is what `mocks/handlers/admin.ts` serves), so they are checked
 * against the deprecated schema.
 */
describe('organization fixtures contain no field the server does not send (#4929)', () => {
  it('every mock organization key exists on the deprecated response schema', async () => {
    const { mockOrganizations } = await import('@/mocks/data/organizations');
    const available = schemaFields(readFileSync(DEPRECATED_SCHEMA, 'utf8'), 'OrganizationResponse');

    expect(mockOrganizations.length).toBeGreaterThan(0);
    for (const org of mockOrganizations) {
      expect(Object.keys(org).filter((key) => !available.includes(key))).toEqual([]);
    }
  });
});
