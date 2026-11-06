import assert from 'node:assert/strict';
import test from 'node:test';
import { pair } from '../static/pair.mjs';

test('an installation is connected only after ADP verifies it', async () => {
  const calls = [];
  await pair(async request => { calls.push(request); return { status: 200, body: { status: 'ready' } }; }, 'a'.repeat(43));
  assert.equal(calls[0].path, '/api/auth/connections/atlassian-mcp/forge/pair');
  assert.deepEqual(calls[0].body, { pairing_code: 'a'.repeat(43) });
  assert.equal(calls[0].headers, undefined);
  for (const response of [{ status: 403, body: {} }, { status: 200, body: { status: 'pending' } }]) {
    await assert.rejects(pair(async () => response, 'a'.repeat(43)));
  }
});

test('invalid codes never leave the browser', async () => {
  let called = false;
  await assert.rejects(pair(async () => { called = true; }, 'invalid'));
  assert.equal(called, false);
});
