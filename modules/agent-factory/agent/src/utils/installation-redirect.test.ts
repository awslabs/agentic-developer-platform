/** Native fetch redirect regression; both destinations are synthetic loopback servers. */
import * as http from 'node:http';
import { AddressInfo } from 'node:net';
import { resolveInstallationId } from './installation';

const nativeFetch = global.fetch;

async function listen(server: http.Server): Promise<string> {
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
}

async function close(server: http.Server): Promise<void> {
  server.closeAllConnections();
  await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
}

it.each([301, 302, 303, 307, 308])('never contacts a redirected destination after HTTP %i', async (status) => {
  const originalId = process.env.GH_APP_INSTALLATION_ID;
  delete process.env.GH_APP_INSTALLATION_ID;
  let targetRequests = 0;
  const target = http.createServer((_request, response) => {
    targetRequests++;
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ id: 999 }));
  });
  const targetOrigin = await listen(target);
  const source = http.createServer((_request, response) => {
    response.writeHead(status, { location: targetOrigin + '/foreign-installation' });
    response.end();
  });
  const sourceOrigin = await listen(source);
  const requested: string[] = [];
  global.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    requested.push(String(input));
    // Replace only the initial test destination; native fetch owns redirects.
    return nativeFetch(sourceOrigin + new URL(String(input)).pathname, init);
  }) as typeof fetch;
  try {
    await expect(resolveInstallationId('synthetic-jwt', { owner: 'org-a', log: jest.fn() })).resolves.toBeNull();
    expect(targetRequests).toBe(0);
    expect(requested).toEqual([
      'https://api.github.com/orgs/org-a/installation',
      'https://api.github.com/users/org-a/installation',
    ]);
  } finally {
    global.fetch = nativeFetch;
    if (originalId === undefined) delete process.env.GH_APP_INSTALLATION_ID;
    else process.env.GH_APP_INSTALLATION_ID = originalId;
    await close(source);
    await close(target);
  }
});
