const dns = require('node:dns');
const fs = require('node:fs/promises');
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
const { GatewayArtifactStore } = require(path.join(config.agent, 'src/complex-task-chat/artifacts/gateway-artifact-store.ts'));
const store = new GatewayArtifactStore(new ChatDataClient({
  baseUrl: config.url, allowHttp: true, workloadToken: async () => 'chat-token',
}), 'session-a', config.workspace);

async function main() {
  try {
    await store.fetch(config.id, config.destination, 'session-a');
    return { content: await fs.readFile(path.join(config.workspace, config.destination), 'utf8') };
  } catch (error) {
    return { error: error.code, status: error.status };
  }
}

main().then(result => process.stdout.write(JSON.stringify(result))).catch(error => {
  process.stderr.write(JSON.stringify({ error: error.code ?? error.name, status: error.status }));
  process.exitCode = 1;
});
