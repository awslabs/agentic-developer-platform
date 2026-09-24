// Loaded only by the test child. Fail loudly if any network API is used.
const { syncBuiltinESMExports } = require('node:module');
const deny = (api) => function () {
  process.stderr.write(`network request blocked: ${api}\n`);
  throw new Error(`network forbidden in task investigator: ${api}`);
};
for (const [name, methods] of Object.entries({
  http: ['request', 'get'], https: ['request', 'get'],
  net: ['connect', 'createConnection'], tls: ['connect'],
  dgram: ['createSocket'], dns: ['lookup', 'resolve'],
})) {
  const module = require(`node:${name}`);
  for (const method of methods) module[method] = deny(`${name}.${method}`);
}
require('node:net').Socket.prototype.connect = deny('Socket.connect');
globalThis.fetch = deny('fetch');
globalThis.WebSocket = deny('WebSocket');
syncBuiltinESMExports();
process.stderr.write('network-deny observer active\n');
