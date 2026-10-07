// Real IPv4/IPv6 trust decisions and forwarded-header boundaries.
const assert = require('node:assert/strict');
const proxy = require('/opt/gbrain/node_modules/proxy-addr');
let checks = 0;
for (const [subnet, addr, expected] of [
  ['::ffff:10.0.0.0/8', '203.0.113.9', false],
  ['::ffff:10.0.0.0/8', '::ffff:203.0.113.9', false],
  ['::ffff:10.0.0.0/8', '::1', false],
  ['::/1', '::ffff:10.0.0.1', false],
  ['::ffff:10.0.0.0/104', '10.0.0.1', true],
  ['::ffff:10.0.0.0/104', '::ffff:10.0.0.1', true],
  ['::ffff:10.0.0.0/104', '203.0.113.9', false],
  ['10.0.0.0/8', '10.0.0.1', true],
  ['10.0.0.0/8', '::ffff:10.0.0.1', true],
  ['2001:db8::/32', '2001:db8::1', true],
  [['::ffff:10.0.0.0/8', '10.0.0.0/8'], '203.0.113.9', false],
  [['::ffff:10.0.0.0/8', '10.0.0.0/8'], '10.0.0.1', true],
]) {
  assert.equal(proxy.compile(subnet)(addr), expected, JSON.stringify({subnet, addr}));
  checks++;
}
for (const peer of ['203.0.113.9', '::ffff:203.0.113.9']) {
  const req = {connection: {remoteAddress: peer}, headers: {'x-forwarded-for': '192.0.2.20'}};
  assert.equal(proxy(req, ['::ffff:10.0.0.0/8']), peer);
  checks++;
}
assert.equal(proxy({connection: {remoteAddress: '10.0.0.1'}, headers: {'x-forwarded-for': '192.0.2.20'}}, ['10.0.0.0/8']), '192.0.2.20');
console.log(JSON.stringify({trust_and_forwarded_header_checks: checks + 1}));
