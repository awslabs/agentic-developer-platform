/**
 * Source guard: the membership calls send and read only what T1's schemas declare — #4847.
 *
 * The Members panel is a pure consumer of the REST surface #4840 shipped, and the issue
 * asks for an integration-level assertion that the request shape matches T1's documented
 * contract. A behavioural test cannot make that assertion: the fixtures it asserts against
 * are written from the same belief as the code, so a field the client invents and the
 * server never accepts passes on both sides. That is the #3675 shape, and
 * `organizationWireContract.test.ts` exists because it happened. This is the same
 * structural check for the membership surface — read the field list out of the `.ts`, read
 * it out of the `.py`, and diff.
 *
 * Subset assertions, for the reason stated there: the client may ignore fields the server
 * sends (`source`, `external_id` are accepted on the write side and unused by this panel).
 * Sending — or consuming — a field the server does NOT declare is the bug.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const ADMIN_SERVICE = join(process.cwd(), 'src/services/admin.ts');
/** T1's request/response models: /admin/organizations/{org}/… membership routes. */
const ADMIN_SCHEMA = join(process.cwd(), '../src/shared/schemas/admin.py');
/** The member-budget batch read added by this story. */
const BUDGET_SCHEMA = join(process.cwd(), '../src/budget/schemas.py');

/** Field names declared on a Pydantic model — the server's source of truth. */
function schemaFields(source: string, className: string): string[] {
  const start = source.indexOf(`class ${className}(BaseModel):`);
  expect(start, `${className} not found`).toBeGreaterThan(-1);

  const rest = source.slice(start);
  const bodyEnd = rest.slice(1).search(/\n(class |\s+model_config|\s+class Config)/);
  const body = bodyEnd === -1 ? rest : rest.slice(0, bodyEnd + 1);

  const fields = [...body.matchAll(/^ {4}([a-z_][a-z0-9_]*)\s*:/gm)].map((m) => m[1]);
  return [...new Set(fields)].sort();
}

/**
 * Every snake_case object key appearing in one exported function's source.
 *
 * Comment lines are stripped first for the reason the sibling guard states: these
 * functions carry prose naming wire fields to explain the mapping, and a parser that
 * counted those would make documenting the contract violate it.
 */
function snakeKeysIn(source: string, functionName: string): string[] {
  const start = source.indexOf(`export async function ${functionName}(`);
  expect(start, `${functionName}() not found`).toBeGreaterThan(-1);
  const end = source.indexOf('\n}\n', start);
  expect(end, `${functionName}() has no parseable end`).toBeGreaterThan(start);

  const body = source
    .slice(start, end)
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//') && !line.trimStart().startsWith('*'))
    .join('\n');

  const keys = [...body.matchAll(/([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\s*:/g)].map((m) => m[1]);
  return [...new Set(keys)].sort();
}

/** Field names on `transformTeamMembership`'s inline wire type. */
function transformWireFields(source: string): string[] {
  const start = source.indexOf('\nfunction transformTeamMembership(data: {\n');
  expect(start, 'transformTeamMembership() not found in a parseable form').toBeGreaterThan(-1);
  const end = source.indexOf('\n}): TeamMembership {', start);
  expect(end, "transformTeamMembership()'s parameter type has no parseable end").toBeGreaterThan(
    start
  );

  const body = source
    .slice(start, end)
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n');

  const fields = [...body.matchAll(/^ {2}([a-zA-Z_][a-zA-Z0-9_]*)\??:/gm)].map((m) => m[1]);
  return [...new Set(fields)].sort();
}

describe('team membership wire contract (#4847 consuming #4840)', () => {
  const service = readFileSync(ADMIN_SERVICE, 'utf8');
  const adminSchema = readFileSync(ADMIN_SCHEMA, 'utf8');

  // Without this, a renamed function or a defeated regex makes every assertion below pass
  // vacuously — a green test asserting nothing, which is the failure mode being guarded.
  it('finds the calls and schemas it is meant to be guarding', () => {
    expect(transformWireFields(service).length).toBeGreaterThan(5);
    expect(snakeKeysIn(service, 'addTeamMember')).toContain('user_id');
    expect(schemaFields(adminSchema, 'TeamMembershipResponse')).toContain('is_primary');
    expect(schemaFields(adminSchema, 'TeamMemberAddRequest')).toContain('user_id');
  });

  it('reads no response field absent from TeamMembershipResponse', () => {
    const declared = transformWireFields(service);
    const available = schemaFields(adminSchema, 'TeamMembershipResponse');

    expect(declared.filter((field) => !available.includes(field))).toEqual([]);
  });

  it('reads is_primary, which is what makes one chip the primary one', () => {
    // Named explicitly as well as covered above: the panel's whole multi-team display
    // rests on this field, so its loss should point straight at the regression.
    expect(transformWireFields(service)).toContain('is_primary');
  });

  it('addTeamMember sends only fields TeamMemberAddRequest accepts', () => {
    const sent = snakeKeysIn(service, 'addTeamMember');
    const accepted = schemaFields(adminSchema, 'TeamMemberAddRequest');

    expect(sent.filter((key) => !accepted.includes(key))).toEqual([]);
    // The user is in the BODY here and the team in the path — the mirror image of the
    // replace-set route, and the pair a membership row is keyed by.
    expect(sent).toContain('user_id');
  });

  it('replaceUserTeams sends only fields the replace-set schemas accept', () => {
    const sent = snakeKeysIn(service, 'replaceUserTeams');
    const accepted = [
      ...schemaFields(adminSchema, 'TeamMembershipSetRequest'),
      ...schemaFields(adminSchema, 'TeamMembershipRequest'),
    ];

    expect(sent.filter((key) => !accepted.includes(key))).toEqual([]);
    expect(sent).toContain('team_id');
    expect(sent).toContain('is_primary');
    // The body is the full intended set, so the envelope key must be the one the server
    // reads the list from. Asserted against the source directly because the key is a
    // single word and so is invisible to the snake_case parser above.
    expect(service.slice(service.indexOf('export async function replaceUserTeams('))).toContain(
      'memberships: memberships.map('
    );
    expect(schemaFields(adminSchema, 'TeamMembershipSetRequest')).toContain('memberships');
  });

  it('no membership call sends a team_id for the USER — that column is a server-side cache', () => {
    // The story's central correctness assertion, at the wire layer. `users.team_id` is a
    // pointer the server re-points to follow the primary membership; a client that wrote
    // it would drift the pointer from the table that decides membership. No membership
    // write may carry a user-shaped team field, and `UserUpdateRequest` (the only
    // user-writing schema) declares none for it to carry.
    expect(schemaFields(adminSchema, 'UserUpdateRequest')).not.toContain('team_id');
  });

  it('getMemberBudgets reads no field absent from MemberBudgetResponse', () => {
    const budgetSchema = readFileSync(BUDGET_SCHEMA, 'utf8');
    const read = snakeKeysIn(service, 'getMemberBudgets');
    const available = [
      ...schemaFields(budgetSchema, 'MemberBudgetResponse'),
      ...schemaFields(budgetSchema, 'MemberBudgetListResponse'),
      // Pagination query params, not response fields.
      'page_size',
    ];

    expect(read.filter((key) => !available.includes(key))).toEqual([]);
    // `limit_status` is read rather than inferred from `limit_usd`: "no cap configured"
    // and "a cap of $0" are different states and the server says which.
    expect(read).toContain('limit_status');
  });
});
