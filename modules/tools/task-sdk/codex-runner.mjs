/** Run the actual Codex CLI with empty stores and explicit loopback-only tools. */
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { setTimeout as delay } from 'node:timers/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import http from 'node:http';
import { randomBytes, randomUUID } from 'node:crypto';
import { MAX_FRAME_BYTES, ProtocolError } from './protocol.mjs';
import { startResponsesProxy } from './responses-proxy.mjs';

export async function startToolServer(definitions, bridge) {
  if (!Array.isArray(definitions) || definitions.length > 16 || new Set(definitions.map(tool => tool.name)).size !== definitions.length) throw new ProtocolError('Invalid Codex tool registry');
  const token = randomBytes(32).toString('hex'), session = randomUUID();
  const server = http.createServer(async (req, res) => {
    if (req.headers.authorization !== `Bearer ${token}`) { res.writeHead(401); res.end(); return; }
    if (req.url !== '/mcp') { res.writeHead(404); res.end(); return; }
    if (req.method === 'DELETE') { res.writeHead(204); res.end(); return; }
    if (req.method !== 'POST') { res.writeHead(405); res.end(); return; }
    let message;
    try {
      let size = 0; const chunks = [];
      for await (const chunk of req) { size += chunk.length; if (size > MAX_FRAME_BYTES) throw new ProtocolError('MCP request exceeds bound'); chunks.push(chunk); }
      message = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (!message || message.jsonrpc !== '2.0' || Array.isArray(message)) throw new ProtocolError('Invalid MCP request');
      if (message.id === undefined && message.method === 'notifications/initialized') { res.writeHead(202); res.end(); return; }
      if (!['string', 'number'].includes(typeof message.id)) throw new ProtocolError('MCP request ID required');
      let result;
      if (message.method === 'initialize') result = { protocolVersion: '2025-03-26', capabilities: { tools: { listChanged: false } }, serverInfo: { name: 'adp-task-repository', version: '1.0.0' } };
      else if (message.method === 'ping') result = {};
      else if (message.method === 'tools/list') result = { tools: definitions.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })) };
      else if (message.method === 'tools/call') {
        const tool = definitions.find(tool => tool.name === message.params?.name);
        if (!tool || bridge.controller.signal.aborted) throw new ProtocolError('Unapproved or cancelled Codex tool');
        try { result = await tool.execute(message.params.arguments || {}); }
        catch { result = { isError: true, content: [{ type: 'text', text: 'The exact repository operation was refused; inspect its inputs and revision.' }] }; }
      } else throw new ProtocolError('Unsupported MCP method');
      const body = JSON.stringify({ jsonrpc: '2.0', id: message.id, result });
      if (Buffer.byteLength(body) > MAX_FRAME_BYTES) throw new ProtocolError('MCP output exceeds bound');
      res.writeHead(200, { 'content-type': 'application/json', 'mcp-session-id': session, 'cache-control': 'no-store' }); res.end(body);
    } catch {
      res.writeHead(400, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ jsonrpc: '2.0', id: message?.id ?? null, error: { code: -32600, message: 'Invalid bounded Task tool request' } }));
    }
  });
  server.requestTimeout = 30000;
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  return { url: `http://127.0.0.1:${server.address().port}/mcp`, token,
    close: async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}

export function codexEnvironment(home, proxy, tools) {
  const env = {};
  for (const name of ['LANG', 'LC_ALL']) if (process.env[name]) env[name] = process.env[name];
  return { ...env, PATH: '/usr/local/bin:/usr/bin:/bin', HOME: home, CODEX_HOME: join(home, '.codex'), BG_CONFIG_DIR: join(home, '.adp'),
    XDG_CONFIG_HOME: join(home, '.config'), XDG_CACHE_HOME: join(home, '.cache'), XDG_DATA_HOME: join(home, '.local'),
    XDG_STATE_HOME: join(home, '.state'), TMPDIR: home, TASK_MODEL_TOKEN: proxy.token, TASK_MCP_TOKEN: tools.token,
    DISABLE_TELEMETRY: '1' };
}

export function codexArguments(home, proxy, tools) {
  const config = value => ['-c', value];
  return ['exec', '--ephemeral', '--skip-git-repo-check', '--sandbox', 'read-only', '--json', '--color', 'never', '-C', home,
    ...config('approval_policy="never"'), ...config('web_search="disabled"'), ...config('features.shell_tool=false'), ...config('features.unified_exec=false'),
    ...config('features.multi_agent=false'), ...config('model="task-authorized"'), ...config('model_provider="task-host"'),
    ...config('model_providers.task-host.name="Task host compatibility transport"'), ...config(`model_providers.task-host.base_url=${JSON.stringify(proxy.url)}`),
    ...config('model_providers.task-host.wire_api="responses"'), ...config('model_providers.task-host.env_key="TASK_MODEL_TOKEN"'),
    ...config('model_providers.task-host.request_max_retries=0'), ...config('model_providers.task-host.stream_max_retries=0'),
    ...config('model_providers.task-host.supports_websockets=false'),
    ...config(`mcp_servers.repository.url=${JSON.stringify(tools.url)}`), ...config('mcp_servers.repository.bearer_token_env_var="TASK_MCP_TOKEN"'),
    ...config('mcp_servers.repository.required=true'),
    ...['list_files', 'read_file', 'replace_text', 'submit_patch'].flatMap(name => config(`mcp_servers.repository.tools.${name}.approval_mode="approve"`)), ...config('project_doc_max_bytes=0'), '-'];
}

// Codex may leave plugin-clone descendants after its own close event. Terminate
// the detached group before removing the home; force:true alone does not retry
// ENOTEMPTY when a descendant is still creating files.
export async function cleanupCodexProcess(child, home, { signal = process.kill, wait = delay, remove = rm } = {}) {
  if (child?.pid) {
    const send = kind => {
      try { signal(-child.pid, kind); return true; }
      catch (error) { if (error.code === 'ESRCH') return false; throw error; }
    };
    if (send('SIGTERM')) {
      for (let i = 0; i < 20 && send(0); i++) await wait(100);
      if (send(0)) send('SIGKILL');
    }
  }
  if (home) await remove(home, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
}

export async function runCodexTask(start, bridge, definitions, { spawnProcess = spawn, proxyFactory = startResponsesProxy, toolFactory = startToolServer,
  binary = 'codex' } = {}) {
  const maxTokens = start.limits?.max_output_tokens_per_turn, maxRequests = start.limits?.max_turns;
  const deadline = Date.parse(start.limits?.deadline_at);
  if (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 10000 || !Number.isInteger(maxRequests) || maxRequests < 1 || maxRequests > 1000 || !Number.isFinite(deadline) || deadline <= Date.now()) throw new ProtocolError('Missing live Codex Task limits');
  let home, proxy, tools, child, timer, killer;
  const stop = () => {
    if (!child?.pid) return;
    try { process.kill(-child.pid, 'SIGTERM'); } catch { child.kill('SIGTERM'); }
    killer = setTimeout(() => { try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill('SIGKILL'); } }, 2000);
    killer.unref?.();
  };
  try {
    home = await mkdtemp(join(tmpdir(), 'adp-task-codex-'));
    await mkdir(join(home, '.codex'), { mode: 0o700 });
    tools = await toolFactory(definitions, bridge);
    proxy = await proxyFactory(bridge, { maxTokens, maxRequests, allowedTools: definitions.map(tool => `mcp__repository__${tool.name}`) });
    child = spawnProcess(binary, codexArguments(home, proxy, tools), { cwd: home, env: codexEnvironment(home, proxy, tools), stdio: ['pipe', 'pipe', 'pipe'], detached: true });
    bridge.controller.signal.addEventListener('abort', stop, { once: true });
    timer = setTimeout(() => { bridge.fail(new ProtocolError('Codex Task deadline reached')); stop(); }, Math.min(600000, deadline - Date.now()));
    let bytes = 0, line = '';
    child.stdout.on('data', chunk => {
      bytes += chunk.length;
      if (bytes > 1024 * 1024) { bridge.fail(new ProtocolError('Codex transcript exceeds bound')); return; }
      line += chunk.toString('utf8');
      if (Buffer.byteLength(line) > MAX_FRAME_BYTES) { bridge.fail(new ProtocolError('Codex event exceeds bound')); return; }
      const rows = line.split('\n'); line = rows.pop();
      // Runtime messages are private diagnostics, never public authored progress.
      for (const row of rows) if (row.trim()) { try { JSON.parse(row); } catch { bridge.fail(new ProtocolError('Malformed Codex event')); } }
    });
    child.stderr.on('data', chunk => { bytes += chunk.length; if (bytes > 1024 * 1024) bridge.fail(new ProtocolError('Codex diagnostics exceed bound')); });
    const completion = new Promise((resolve, reject) => { child.once('error', reject); child.once('close', (code, signal) => resolve({ code, signal })); });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify({ instructions: start.instructions, acceptance_criteria: start.acceptance_criteria || [],
      task: 'Use only the repository MCP operations. Inspect the server-verified snapshot and submit the grounded patch using submit_patch. No shell, filesystem, network, provider or publication authority is granted.' }));
    const result = await completion;
    if (bridge.failure) throw bridge.failure;
    if (result.code !== 0 || result.signal || !bridge.report) throw new ProtocolError('Codex exited without an accepted grounded patch');
    return bridge.report;
  } finally {
    clearTimeout(timer); clearTimeout(killer);
    bridge.controller.signal.removeEventListener('abort', stop);
    try {
      await cleanupCodexProcess(child, home);
    } finally {
      await Promise.all([proxy?.close(), tools?.close()]);
    }
  }
}
