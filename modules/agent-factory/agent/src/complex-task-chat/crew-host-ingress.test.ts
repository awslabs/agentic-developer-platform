import { execFileSync } from 'node:child_process';
import { createHash, generateKeyPairSync, sign, X509Certificate } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { request as requestHttps } from 'node:https';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { bodyDigest, ENVELOPE_ISSUER, ENVELOPE_VERSION } from '../control-envelope';
import { createCrewHostIngress, startCrewHostDashboard } from './crew-host-ingress';
import type { ChatDataClient } from './gateway/chat-data-client';

const runId = '00000000-0000-4000-8000-000000000001';
const requestId = '00000000-0000-4000-8000-000000000002';
const now = Date.parse('2026-10-13T00:00:00Z');
const signing = generateKeyPairSync('ed25519');

function certificate(directory: string, name: string, ca: boolean, subjectAltName?: string) {
  const certificateFile = join(directory, `${name}.pem`);
  const keyFile = join(directory, `${name}.key`);
  const requestFile = join(directory, `${name}.csr`);
  const args = ['req', '-newkey', 'rsa:2048', '-nodes', '-keyout', keyFile,
    '-subj', `/CN=${name}`, ...(subjectAltName ? ['-addext', `subjectAltName=${subjectAltName}`] : [])];
  if (ca) {
    execFileSync('openssl', [...args, '-x509', '-out', certificateFile, '-days', '1',
      '-addext', 'basicConstraints=critical,CA:TRUE'], { stdio: 'ignore' });
  } else {
    execFileSync('openssl', [...args, '-out', requestFile], { stdio: 'ignore' });
    execFileSync('openssl', ['x509', '-req', '-in', requestFile, '-CA', join(directory, 'root.pem'),
      '-CAkey', join(directory, 'root.key'), '-CAcreateserial', '-out', certificateFile,
      '-days', '1', '-copy_extensions', 'copy'], { stdio: 'ignore' });
  }
  return { certificate: readFileSync(certificateFile), key: readFileSync(keyFile) };
}

function signed(body: Buffer) {
  const payload = { v: ENVELOPE_VERSION, iss: ENVELOPE_ISSUER, aud: 'adp-crew-dashboard-mint',
    alg: 'ed25519', kid: 'gateway', tenant_id: 'tenant', principal: 'owner', target_run_id: runId,
    target_generation: 2, action: 'dashboard.mint', command_id: requestId, body_digest: bodyDigest(body),
    authority_kind: 'human_session', iat: new Date(now).toISOString().replace('.000Z', 'Z'),
    nbf: new Date(now).toISOString().replace('.000Z', 'Z'), exp: new Date(now + 30_000).toISOString().replace('.000Z', 'Z') };
  const encoded = Buffer.from(JSON.stringify(payload));
  return `${ENVELOPE_VERSION}.${encoded.toString('base64url')}.${sign(null,
    Buffer.concat([Buffer.from(`${ENVELOPE_VERSION}.`), encoded]), signing.privateKey).toString('base64url')}`;
}

function listen(server: ReturnType<typeof createServer>) {
  return new Promise<number>(resolve => server.listen(0, '127.0.0.1', () => resolve((server.address() as { port: number }).port)));
}
function close(server: ReturnType<typeof createServer>) {
  return new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
}

function upgrade(port: number, ca: Buffer, identity: { certificate: Buffer; key: Buffer }) {
  return new Promise<number>((resolve, reject) => {
    const request = requestHttps({ hostname: '127.0.0.1', port, path: '/api/live', ca,
      cert: identity.certificate, key: identity.key, rejectUnauthorized: true,
      headers: { Connection: 'Upgrade', Upgrade: 'websocket', 'Sec-WebSocket-Version': '13',
        'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==' } });
    request.on('upgrade', response => { response.socket.destroy(); resolve(response.statusCode ?? 0); });
    request.on('response', response => { response.resume(); resolve(response.statusCode ?? 0); });
    request.on('error', reject);
    request.end();
  });
}

function upstream(port: number, ca: Buffer, identity: { certificate: Buffer; key: Buffer } | undefined,
  path: string, body?: Buffer, signature?: string) {
  return new Promise<{ status: number; body: string }>((resolve, reject) => {
    const request = requestHttps({ hostname: '127.0.0.1', port, method: body ? 'POST' : 'GET', path,
      ca, cert: identity?.certificate, key: identity?.key, rejectUnauthorized: true,
      headers: body ? { 'content-type': 'application/json', 'content-length': body.length,
        'x-adp-crew-signature': signature ?? '' } : {} }, response => {
      let content = '';
      response.on('data', chunk => { content += chunk; });
      response.on('end', () => resolve({ status: response.statusCode ?? 0, body: content }));
    });
    request.on('error', reject);
    request.end(body);
  });
}

test('gateway mTLS reaches the dashboard and signed custody; foreign and fenced requests never mint', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'crew-host-'));
  const dashboard = createServer((_request, reply) => reply.end('private-dashboard'));
  const upgrades: string[] = [];
  dashboard.on('upgrade', (request, socket) => {
    upgrades.push(request.url ?? '');
    socket.end('HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n');
  });
  let ingress: Awaited<ReturnType<typeof startCrewHostDashboard>> | undefined;
  let current = true;
  try {
    const root = certificate(directory, 'root', true);
    const host = certificate(directory, 'host', false, 'IP:127.0.0.1');
    const gateway = certificate(directory, 'gateway', false);
    const other = certificate(directory, 'other', false);
    const tokenDirectory = join(directory, 'model-token');
    mkdirSync(tokenDirectory, { mode: 0o700 });
    const dashboardPort = await listen(dashboard);
    let generation = 2;
    const mint = jest.fn(async () => 'token_123456789012345678901234');
    const client = { modelDecision: async () => ({ modelId: 'approved', runId, generation, tenantId: 'tenant', userId: 'owner' }),
      dashboardVerificationKeys: async () => new Map([['gateway', signing.publicKey]]) } as unknown as ChatDataClient;
    ingress = await startCrewHostDashboard(client, runId, { privateIP: '10.4.0.3', certificate: host.certificate,
      key: host.key, gatewayCA: root.certificate,
      gatewayFingerprint: createHash('sha256').update(new X509Certificate(gateway.certificate).raw).digest('hex'),
      dashboardPort, listenPort: 0, isCurrent: () => current, generation: 2, tokenDirectory }, mint, (server, port) => server.listen(port, '127.0.0.1'), () => now);
    const port = (ingress.address() as { port: number }).port;
    expect(await upstream(port, root.certificate, gateway, '/')).toEqual({ status: 200, body: 'private-dashboard' });
    expect((await upstream(port, root.certificate, other, '/')).status).toBe(403);
    const credential = Buffer.from(JSON.stringify({ run_id: runId, lease_generation: 2,
      token: 'eyJ0ZXN0IjoidG9rZW4ifQ.signature', expires_at: Math.floor(Date.now() / 1000) + 40 }));
    expect((await upstream(port, root.certificate, gateway, '/model-token', credential)).status).toBe(204);
    expect(JSON.parse(readFileSync(join(tokenDirectory, 'token.json'), 'utf8')).token).toBe('eyJ0ZXN0IjoidG9rZW4ifQ.signature');
    expect((await upstream(port, root.certificate, other, '/model-token', credential)).status).toBe(403);
    expect((await upstream(port, root.certificate, gateway, '/model-token', Buffer.from(JSON.stringify({
      ...JSON.parse(credential.toString()), lease_generation: 3 })))).status).toBe(403);
    expect(await upgrade(port, root.certificate, gateway)).toBe(101);
    await expect(upgrade(port, root.certificate, other)).rejects.toThrow();
    expect(upgrades).toEqual(['/api/live']);
    current = false;
    expect((await upstream(port, root.certificate, gateway, '/model-token', credential)).status).toBe(403);
    expect((await upstream(port, root.certificate, gateway, '/')).status).toBe(403);
    await expect(upgrade(port, root.certificate, gateway)).rejects.toThrow();
    current = true;
    await expect(upstream(port, root.certificate, undefined, '/')).rejects.toThrow();
    const body = Buffer.from(JSON.stringify({ run_id: runId, lease_generation: 2, user_id: 'owner', request_id: requestId }));
    expect((await upstream(port, root.certificate, gateway, '/mint', body, signed(body))).status).toBe(200);
    expect((await upstream(port, root.certificate, other, '/mint', body, signed(body))).status).toBe(403);
    generation = 3;
    expect((await upstream(port, root.certificate, gateway, '/mint', body, signed(body))).status).toBe(409);
    expect(mint).toHaveBeenCalledTimes(1);
  } finally {
    if (ingress) await ingress.close();
    if (dashboard.listening) await close(dashboard);
    rmSync(directory, { recursive: true, force: true });
  }
}, 30_000);


test('refuses a nonprivate listener or missing gateway client identity before binding', () => {
  const material = { privateIP: '10.4.0.3', certificate: Buffer.from('certificate'),
    key: Buffer.from('key'), gatewayCA: Buffer.from('ca'), gatewayFingerprint: 'a'.repeat(64), isCurrent: () => true };
  expect(() => createCrewHostIngress({ ...material, privateIP: '0.0.0.0' })).toThrow('private host');
  expect(() => createCrewHostIngress({ ...material, privateIP: '203.0.113.1' })).toThrow('private host');
  expect(() => createCrewHostIngress({ ...material, gatewayFingerprint: '' })).toThrow('gateway mTLS');
  expect(() => createCrewHostIngress({ ...material, gatewayCA: Buffer.alloc(0) })).toThrow('gateway mTLS');
});
