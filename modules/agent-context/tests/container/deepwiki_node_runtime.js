// CVE-2026-48930: reject embedded NUL authority names before resolver/connection work.
// Run only in the isolated container fixture with --network none and no auth mounts.
'use strict';
const assert = require('node:assert/strict');
const dns = require('node:dns');
const net = require('node:net');
const invalidHost = 'fixture\u0000.invalid';
for (const [name, invoke] of [
  ['dns.lookup', () => dns.lookup(invalidHost, {}, () => {})],
  ['dns.promises.lookup', () => dns.promises.lookup(invalidHost, {})],
  ['net.createConnection', () => net.createConnection({host: invalidHost, port: 9})],
]) {
  assert.throws(invoke, {name: 'TypeError', code: 'ERR_INVALID_ARG_VALUE'}, name);
}
console.log(JSON.stringify({node: process.version, nulHostnameRejection: 'passed', cases: 3}));
