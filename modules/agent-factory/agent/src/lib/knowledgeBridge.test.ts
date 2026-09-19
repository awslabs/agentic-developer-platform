import { createServer, Server } from 'node:http';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { handleKnowledgeBridge, KNOWLEDGE_BRIDGE_URL } from './knowledgeBridge';
import { buildKnowledgeLayerHeaders, getKnowledgeLayerMcpConfig } from '../knowledge-layer-config';
import { getDoorAuthHeaders, getDoorHeaders } from './doorAuth';
import { saveExperienceLearnings } from '../experience-save-hook';

jest.mock('./runIdentity', () => ({
  ...jest.requireActual('./runIdentity'),
  workerAwsCredentials: () => async () => ({ accessKeyId: 'TESTONLYACCESSKEY', secretAccessKey: 'test-only-secret', sessionToken: 'test-only-session' }),
}));

describe('protected Knowledge Door bridge', () => {
  const env = process.env;
  const nativeFetch = global.fetch;
  let server: Server;
  let local: string;
  let dir: string;
  let upstream: jest.Mock;

  beforeEach(async () => {
    dir = mkdtempSync(join(tmpdir(), 'knowledge-bridge-'));
    writeFileSync(join(dir, 'run'), 'run-one');
    writeFileSync(join(dir, 'pod'), 'pod-one');
    process.env = {
      ...env, ADP_AGENT_AUTHORITY_ENABLED: 'true',
      ADP_GATEWAY_ENDPOINT: 'https://gateway.execute-api.us-east-1.amazonaws.com/dev',
      ADP_RUN_CREDENTIAL_FILE: join(dir, 'run'), ADP_WORKLOAD_TOKEN_FILE: join(dir, 'pod'),
      DOOR_API_KEY: 'shared-door-key-must-not-be-used', GATEWAY_INTERNAL_API_KEY: 'shared-internal-key-must-not-be-used',
      ADP_GITHUB_LOGIN: 'victim', ADP_GITHUB_TEAMS: 'org/admins', ADP_TENANT_ID: 'victim', ADP_OWNER_SUB: 'victim',
    };
    upstream = jest.fn(async () => new Response('{"status":"ok"}', { headers: { 'content-type': 'application/json' } }));
    global.fetch = jest.fn(upstream);
    server = createServer(async (req, res) => {
      if (!await handleKnowledgeBridge(req, res)) { res.writeHead(418); res.end(); }
    });
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    local = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  });

  afterEach(async () => {
    global.fetch = nativeFetch;
    await new Promise<void>((resolve, reject) => { server.close(error => error ? reject(error) : resolve()); server.closeAllConnections(); });
    process.env = env;
    rmSync(dir, { recursive: true, force: true });
  });

  it('native MCP config holds neither shared keys, identity headers nor a static run token', () => {
    expect(getKnowledgeLayerMcpConfig()).toEqual({ type: 'http', url: KNOWLEDGE_BRIDGE_URL + '/mcp/', headers: {} });
    expect(buildKnowledgeLayerHeaders()).toEqual({});
    expect(getDoorAuthHeaders()).toEqual({});
    expect(getDoorHeaders({ 'X-Owner-Sub': 'victim', 'X-Tenant-Id': 'victim' })).toEqual({});
  });

  it('experience save uses the bridge without relying on mutable identity metadata', async () => {
    process.env.PERSONAL_CONTEXT_SAVE_ENABLED = 'true';
    const saved = await saveExperienceLearnings({
      agentOutput: '### Learnings\n- Confirm the active branch before editing.',
      persona: 'developer', identityHeaders: null,
    });
    expect(saved.saved).toBe(1);
    const [url, options] = upstream.mock.calls[0];
    expect(url).toBe(KNOWLEDGE_BRIDGE_URL + '/call');
    expect(options.headers).toEqual({ 'Content-Type': 'application/json' });
  });

  it('recall uses the actual Door call route without mutable identity headers', async () => {
    process.env.PERSONAL_CONTEXT_RECALL_ENABLED = 'true';
    let recall: typeof import('../complex-task-chat/recall-at-task-start');
    jest.isolateModules(() => { recall = require('../complex-task-chat/recall-at-task-start'); });
    const result = await recall!.recallAtTaskStart(null, 'How do tests run?', 'developer');
    expect(result.attempted).toBe(true);
    const [url, options] = upstream.mock.calls[0];
    expect(url).toBe(KNOWLEDGE_BRIDGE_URL + '/call');
    expect(options.headers).toEqual({ 'Content-Type': 'application/json' });
  });

  it('forwards only fixed paths with fresh own-run proofs and platform signing', async () => {
    const response = await nativeFetch(local + '/__run/knowledge/mcp/', {
      method: 'POST', body: '{"jsonrpc":"2.0","id":1,"method":"initialize"}',
      headers: { 'X-Adp-Run-Credential': 'victim-run', 'X-Adp-Workload-Token': 'victim-pod',
        'X-GitHub-Login': 'victim', 'X-Tenant-Id': 'victim', 'X-Internal-Api-Key': 'leaked-key',
        'Authorization': 'victim-auth', 'Cookie': 'victim-cookie', 'Mcp-Session-Id': 'victim-session' },
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: 'ok' });
    const [url, options] = upstream.mock.calls[0];
    expect(String(url)).toBe('https://gateway.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent/self/knowledge/mcp/');
    const headers = new Headers(options.headers);
    expect(headers.get('x-adp-run-credential')).toBe('run-one');
    expect(headers.get('x-adp-workload-token')).toBe('pod-one');
    expect(headers.get('authorization')).toContain('/us-east-1/execute-api/aws4_request');
    for (const name of ['x-github-login', 'x-tenant-id', 'x-owner-sub', 'x-internal-api-key', 'cookie', 'mcp-session-id']) {
      expect(headers.has(name)).toBe(false);
    }
    expect(options.redirect).toBe('error');
    expect(response.headers.get('cache-control')).toBe('no-store');
    writeFileSync(join(dir, 'run'), 'run-two');
    writeFileSync(join(dir, 'pod'), 'pod-two');
    const second = await nativeFetch(local + '/__run/knowledge/call', { method: 'POST', body: '{}' });
    expect(second.status).toBe(200);
    const rotated = new Headers(upstream.mock.calls[1][1].headers);
    expect(rotated.get('x-adp-run-credential')).toBe('run-two');
    expect(rotated.get('x-adp-workload-token')).toBe('pod-two');
  });

  it.each(['/__run/knowledge/credentials', '/__run/knowledge/call?tenant=victim', '/__run/knowledge/mcp/victim', '/__run/secret'])('refuses unsupported path %s', async path => {
    const response = await nativeFetch(local + path, { method: 'POST', body: '{}' });
    expect(response.status).toBe(404);
    expect(upstream).not.toHaveBeenCalled();
  });

  it('refuses a missing own-run proof without a legacy shared-key fallback', async () => {
    rmSync(join(dir, 'run'));
    const response = await nativeFetch(local + '/__run/knowledge/call', { method: 'POST', body: '{}' });
    expect(response.status).toBe(502);
    expect(await response.text()).toBe('Knowledge service unavailable');
    expect(upstream).not.toHaveBeenCalled();
  });

  it('does not expose the bridge on a legacy run', async () => {
    process.env.ADP_AGENT_AUTHORITY_ENABLED = 'false';
    const response = await nativeFetch(local + '/__run/knowledge/call', { method: 'POST', body: '{}' });
    expect(response.status).toBe(404);
    expect(upstream).not.toHaveBeenCalled();
  });

  it('bounds upload and download and sanitizes upstream refusals', async () => {
    const oversized = await nativeFetch(local + '/__run/knowledge/call', { method: 'POST', body: 'x'.repeat(1024 * 1024 + 1) });
    expect(oversized.status).toBe(413);
    expect(upstream).not.toHaveBeenCalled();
    upstream.mockResolvedValueOnce(new Response('shared-secret-detail', { status: 403 }));
    const refused = await nativeFetch(local + '/__run/knowledge/call', { method: 'POST', body: '{}' });
    expect(refused.status).toBe(403);
    expect(await refused.text()).toBe('Knowledge request refused');
    upstream.mockResolvedValueOnce(new Response('x'.repeat(4 * 1024 * 1024 + 1)));
    const large = await nativeFetch(local + '/__run/knowledge/call', { method: 'POST', body: '{}' });
    expect(large.status).toBe(502);
  });
});
