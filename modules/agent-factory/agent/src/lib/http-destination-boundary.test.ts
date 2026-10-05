import { createServer, type RequestListener, type Server } from 'node:http';
import { GitLabClient } from '../clients/gitlab_client';
import { VaultGatewayClient } from '../complex-task-chat/vault/gateway-client';
import { LiveStatusComment } from '../github-comments';

async function localServer(handler: RequestListener): Promise<{ origin: string; close: () => Promise<void> }> {
  const server: Server = createServer(handler);
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('local fixture failed to bind');
  return {
    origin: `http://127.0.0.1:${address.port}`,
    close: async () => {
      server.closeAllConnections();
      await new Promise<void>(resolve => server.close(() => resolve()));
    },
  };
}

const cases = [
  {
    name: 'GitLab',
    origin: 'https://gitlab.example.com',
    header: 'private-token',
    credential: 'fixture-gitlab-token',
    invoke: () => new GitLabClient({ baseUrl: 'https://gitlab.example.com', accessToken: 'fixture-gitlab-token' })
      .postIssueComment(1, 2, 'fixture note'),
  },
  {
    name: 'vault',
    origin: 'http://gateway.internal:8080',
    header: 'x-internal-api-key',
    credential: 'fixture-vault-key',
    invoke: () => new VaultGatewayClient({ baseUrl: 'http://gateway.internal:8080', apiKey: 'fixture-vault-key' })
      .listCredentials('fixture-user/other?x=1'),
  },
  {
    name: 'GitHub comments',
    origin: 'https://api.github.com',
    header: 'authorization',
    credential: 'token fixture-github-token',
    invoke: () => new LiveStatusComment([], { owner: 'fixture', repo: 'repository', issueNumber: 1,
      token: 'fixture-github-token' }).post(),
  },
];

const originalEnv = { ...process.env };
afterEach(() => { process.env = { ...originalEnv }; });

test.each(cases)('$name sends credentials to the trusted origin but not a redirect target', async ({ origin, header, credential, invoke }) => {
  process.env.GITLAB_URL = 'https://gitlab.example.com';
  process.env.VAULT_GATEWAY_URL = 'http://gateway.internal:8080';
  delete process.env.GH_APP_TOKEN;
  delete process.env.GH_TOKEN;
  delete process.env.GITHUB_TOKEN;
  const nativeFetch = globalThis.fetch;
  let targetRequests = 0;
  const target = await localServer((_request, response) => {
    targetRequests += 1;
    response.writeHead(200).end('{}');
  });
  let redirect = false;
  const seenHeaders: string[] = [];
  const source = await localServer((request, response) => {
    seenHeaders.push(String(request.headers[header] ?? ''));
    if (redirect) response.writeHead(302, { location: `${target.origin}/untrusted` }).end();
    else response.writeHead(200, { 'content-type': 'application/json' })
      .end(JSON.stringify(header === 'authorization' ? { id: 1 } : header === 'x-internal-api-key' ? [] : {}));
  });
  globalThis.fetch = (async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    const requested = new URL(String(input));
    if (requested.origin !== origin) throw new Error('client attempted an untrusted origin');
    return nativeFetch(`${source.origin}${requested.pathname}${requested.search}`, init);
  }) as typeof fetch;
  try {
    await invoke();
    expect(seenHeaders).toEqual([credential]);
    redirect = true;
    await expect(invoke()).rejects.toThrow();
    expect(seenHeaders).toEqual([credential, credential]);
    expect(targetRequests).toBe(0);
  } finally {
    globalThis.fetch = nativeFetch;
    await source.close();
    await target.close();
  }
});
