const dns = require('node:dns');
const path = require('node:path');

const config = JSON.parse(process.argv[2]);
Date.now = () => config.now * 1000;
dns.lookup = (hostname, options, callback) => {
  if (typeof options === 'function') {
    callback = options;
    options = {};
  }
  if (hostname !== 'chat-gateway.test') throw new Error('Unexpected integration-test destination');
  process.nextTick(() => options?.all
    ? callback(null, [{ address: '127.0.0.1', family: 4 }])
    : callback(null, '127.0.0.1', 4));
};

const { ChatDataClient } = require(path.join(config.agent, 'src/complex-task-chat/gateway/chat-data-client.ts'));
const { GatewayMemoryProvider } = require(path.join(config.agent, 'src/complex-task-chat/memory/gateway-memory.ts'));
const provider = new GatewayMemoryProvider(new ChatDataClient({
  baseUrl: config.url, allowHttp: true, workloadToken: async () => 'chat-token',
}));

async function tool(name, persona, input) {
  return provider.tools({ user: 'forged-owner', tenant: 'forged-tenant', persona }).find(candidate => candidate.name === name).handler(input);
}

async function main() {
  if (config.mode === 'retrieve') {
    try {
      return { records: await provider.retrieve({ query: config.query, kinds: config.kinds }) };
    } catch (error) {
      return { error: error.code };
    }
  }
  if (config.mode === 'owner') {
    await tool('save_preference', 'reviewer', { content: config.preference });
    const records = await provider.retrieve({ query: config.preference, kinds: ['preference'] });
    return {
      id: records[0].id,
      scope: records[0].scope,
      reviewer: await tool('recall_memory', 'reviewer', { query: config.preference }),
      developer: await tool('recall_memory', 'developer', { query: config.preference }),
      component: await provider.retrieve({ query: config.preference, scope: { component: 'gateway' }, kinds: ['preference', 'fact'] }),
    };
  }
  if (config.mode === 'other') {
    const recall = await tool('recall_memory', 'reviewer', { query: config.preference });
    let read;
    try {
      read = { record: await provider.read(config.ownerId) };
    } catch (error) {
      read = { error: error.code, status: error.status };
    }
    await tool('save_preference', 'developer', { content: 'Other owner prefers detailed answers' });
    return { recall, read, own: await tool('recall_memory', 'reviewer', { query: 'Other owner prefers detailed answers' }) };
  }
  throw new Error('Unknown integration-test operation');
}

main().then(result => process.stdout.write(JSON.stringify(result))).catch(error => {
  process.stderr.write(JSON.stringify({ error: error.code ?? error.name, status: error.status }));
  process.exitCode = 1;
});
