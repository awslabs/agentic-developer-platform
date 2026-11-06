import { generateKeyPairSync, sign } from 'node:crypto';
import { bodyDigest, ENVELOPE_ISSUER, ENVELOPE_VERSION } from '../control-envelope';
import { createCrewDashboardCustody, mintCrewLink } from './crew-dashboard-custody';
import type { ChatDataClient } from './gateway/chat-data-client';

const runId = '00000000-0000-4000-8000-000000000001';
const requestId = '00000000-0000-4000-8000-000000000002';
const pair = generateKeyPairSync('ed25519');
const now = Date.parse('2026-10-13T00:00:00Z');
const mint = jest.fn(async () => 'token_123456789012345678901234');
const decision = jest.fn(async () => ({ modelId: 'approved', runId, generation: 2, tenantId: 'tenant', userId: 'owner' }));
const keys = jest.fn(async () => new Map([['gateway', pair.publicKey]]));
const client = { modelDecision: decision, dashboardVerificationKeys: keys } as unknown as ChatDataClient;

function signed(body: Buffer, overrides: Record<string, unknown> = {}) {
  const payload = {
    v: ENVELOPE_VERSION, iss: ENVELOPE_ISSUER, aud: 'adp-crew-dashboard-mint', alg: 'ed25519',
    kid: 'gateway', tenant_id: 'tenant', principal: 'owner', target_run_id: runId,
    target_generation: 2, action: 'dashboard.mint', command_id: requestId, body_digest: bodyDigest(body),
    authority_kind: 'human_session', iat: new Date(now).toISOString().replace('.000Z', 'Z'), nbf: new Date(now).toISOString().replace('.000Z', 'Z'),
    exp: new Date(now + 30_000).toISOString().replace('.000Z', 'Z'), ...overrides,
  };
  const encoded = Buffer.from(JSON.stringify(payload));
  return `${ENVELOPE_VERSION}.${encoded.toString('base64url')}.${sign(null,
    Buffer.concat([Buffer.from(`${ENVELOPE_VERSION}.`), encoded]), pair.privateKey).toString('base64url')}`;
}

beforeEach(() => { jest.clearAllMocks(); decision.mockResolvedValue({ modelId: 'approved', runId, generation: 2, tenantId: 'tenant', userId: 'owner' }); });

test.each(['10.1.2.3', '172.16.1.2', '172.31.1.2', '192.168.1.2'])(
  'mints one short-lived owner link for private pod IP %s and refuses request reuse', async podIP => {
  const custody = createCrewDashboardCustody(client, runId, podIP, '/tmp/workspace', mint, () => now);
  await custody.listen(0, '127.0.0.1');
  try {
    const port = (custody.address() as { port: number }).port;
    const body = Buffer.from(JSON.stringify({ run_id: runId, lease_generation: 2, user_id: 'owner', request_id: requestId }));
    const request = () => fetch(`http://127.0.0.1:${port}/mint`, { method: 'POST', headers: {
      'content-type': 'application/json', 'x-adp-crew-signature': signed(body),
    }, body });
    const response = await request();
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ link_token: 'token_123456789012345678901234',
      expires_at: '2026-10-13T00:05:00.000Z', lease_generation: 2 });
    expect((await request()).status).toBe(409);
    expect(mint).toHaveBeenCalledTimes(1);
  } finally { await custody.close(); }
});

test.each(['10.256.1.2', '192.168.1.2.3', '172.16.1.2.3', '172.15.1.2', '172.32.1.2',
  '192.169.1.2', '127.0.0.1', '8.8.8.8'])(
  'rejects invalid or non-private pod IP %s before serving mint requests', podIP => {
  expect(() => createCrewDashboardCustody(client, runId, podIP, '/tmp/workspace', mint))
    .toThrow('Crew custody requires an admitted run and private pod IP');
  expect(mint).not.toHaveBeenCalled();
});

test.each([
  { name: 'missing signature', body: { user_id: 'owner', lease_generation: 2 }, sign: false, status: 401 },
  { name: 'forged signature', body: { user_id: 'owner', lease_generation: 2 }, forged: true, status: 403 },
  { name: 'another owner', body: { user_id: 'stranger', lease_generation: 2 }, status: 403 },
  { name: 'superseded lease', body: { user_id: 'owner', lease_generation: 1 }, status: 409 },
  { name: 'another run', body: { user_id: 'owner', lease_generation: 2, run_id: '00000000-0000-4000-8000-000000000003' }, status: 409 },
])('refuses $name without minting', async scenario => {
  const custody = createCrewDashboardCustody(client, runId, '10.1.2.3', '/tmp/workspace', mint, () => now);
  await custody.listen(0, '127.0.0.1');
  try {
    const body = Buffer.from(JSON.stringify({ run_id: runId, request_id: requestId, ...scenario.body }));
    const port = (custody.address() as { port: number }).port;
    const response = await fetch(`http://127.0.0.1:${port}/mint`, { method: 'POST', headers: {
      'content-type': 'application/json', ...(scenario.sign === false ? {} : {
        'x-adp-crew-signature': scenario.forged ? signed(body).slice(0, -3) + 'bad' : signed(body),
      }),
    }, body });
    expect(response.status).toBe(scenario.status);
    expect(mint).not.toHaveBeenCalled();
  } finally { await custody.close(); }
});

test('calls the loopback Kiro Crew CLI with a five-minute TTL and returns only its token', async () => {
  const token = 'a'.repeat(60) + '.' + 'b'.repeat(60) + '.' + 'c'.repeat(60);
  const runner = jest.fn(async () => ({ stdout: `http://localhost:5476?token=${token}\n`, stderr: '' }));
  await expect(mintCrewLink('/tmp/workspace', runner as unknown as Parameters<typeof mintCrewLink>[1])).resolves.toBe(token);
  expect(runner).toHaveBeenCalledWith('/opt/kirocrew/venv/bin/kirocrew', ['token', '--ttl', '5m', '--port', '5476'],
    expect.objectContaining({ cwd: '/tmp/workspace', timeout: 20_000 }));
});


test('host mint uses the pinned installed Crew profile instead of the pod image', async () => {
  const runner = jest.fn(async () => ({ stdout: 'http://localhost:5476?token=token_123456789012345678901234\n', stderr: '' }));
  await expect(mintCrewLink('/mnt/crew', runner as unknown as Parameters<typeof mintCrewLink>[1], 'host'))
    .resolves.toBe('token_123456789012345678901234');
  expect(runner).toHaveBeenCalledWith('/opt/adp/crew/current/venv/bin/kirocrew',
    ['token', '--ttl', '5m', '--port', '5476'], expect.objectContaining({
      cwd: '/mnt/crew', env: expect.objectContaining({ PATH: '/opt/adp/crew/current/venv/bin:/usr/bin:/bin' }),
    }));
});
