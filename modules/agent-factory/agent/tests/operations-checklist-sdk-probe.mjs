// Exercise the pinned SDK's real tool registry without a model or cloud call.
import { createServer } from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const { query } = await import(pathToFileURL(process.argv[2]).href);
const options = JSON.parse(process.argv[3]);
const directory = await mkdtemp(join(tmpdir(), 'operations-checklist-sdk-'));
const server = createServer((_request, response) => {
  response.writeHead(503, { 'content-type': 'application/json' });
  response.end(JSON.stringify({ type: 'error', error: {
    type: 'api_error', message: 'Offline tool registry probe',
  } }));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), 15000);
const run = query({ prompt: 'Reply OK.', options: {
  ...options,
  cwd: directory,
  model: 'claude-sonnet-4-6',
  settingSources: [],
  persistSession: false,
  maxTurns: 1,
  abortController: controller,
  env: {
    ...options.env,
    PATH: process.env.PATH,
    ANTHROPIC_API_KEY: 'offline-probe-placeholder',
    ANTHROPIC_BASE_URL: `http://127.0.0.1:${server.address().port}`,
    CLAUDE_CONFIG_DIR: directory,
    CLAUDE_CODE_USE_BEDROCK: '0',
    CLAUDE_CODE_USE_VERTEX: '0',
    CLAUDE_CODE_USE_FOUNDRY: '0',
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1',
  },
} });
try {
  for await (const message of run) {
    if (message.type === 'system' && message.subtype === 'init') {
      process.stdout.write(JSON.stringify(message.tools));
      break;
    }
  }
} finally {
  run.close();
  clearTimeout(timer);
  server.closeAllConnections();
  server.close();
  await rm(directory, { recursive: true, force: true });
}
