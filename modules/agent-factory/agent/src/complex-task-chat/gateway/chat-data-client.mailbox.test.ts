import { createHash } from 'node:crypto';
import { ChatDataClient } from './chat-data-client';

const NOW = Date.parse('2026-10-04T12:00:00Z');
const binding = {
  capability: 'synthetic.scoped.capability', run_id: 'run-a', session_id: 'session-a',
  lease_generation: 1, attempt: 1, expires_at: NOW / 1000 + 300,
};
const accepted = {
  run_id: 'run-a', session_id: 'session-a', lease_generation: 1,
  turn: {
    ref: `user_${createHash('sha256').update('run-a').digest('hex')}`,
    message: {
      role: 'user', content: 'Review my artifact', ts: '2026-10-04T12:00:00Z', tokens: 5,
      parts: [{ type: 'file', artifactId: 'art_0123456789ab' }],
    },
  },
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('sandbox owner-turn mailbox transport', () => {
  let fetchMock: jest.SpiedFunction<typeof fetch>;
  let workloadToken: jest.Mock<Promise<string>, []>;
  let client: ChatDataClient;

  beforeEach(() => {
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    fetchMock = jest.spyOn(globalThis, 'fetch');
    workloadToken = jest.fn(async () => 'sandbox.workload.token');
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken });
  });

  afterEach(() => jest.restoreAllMocks());

  it('reads the protected owner turn using only the workload-bound assignment', async () => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json(accepted));
    await expect(client.nextTurn()).resolves.toEqual(accepted.turn);
    expect(fetchMock.mock.calls).toEqual([
      ['https://gateway.example.test/v1/chat/data/bootstrap', expect.objectContaining({
        body: '{}', headers: { 'Content-Type': 'application/json', 'X-Adp-Workload-Token': 'sandbox.workload.token' },
      })],
      ['https://gateway.example.test/v1/chat/turn/next', expect.objectContaining({
        body: JSON.stringify({ run_id: 'run-a', session_id: 'session-a' }), redirect: 'error',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer synthetic.scoped.capability',
          'X-Adp-Workload-Token': 'sandbox.workload.token' },
      })],
    ]);
  });

  it.each([
    ['run', { run_id: 'run-other' }],
    ['session', { session_id: 'session-other' }],
    ['generation', { lease_generation: 2 }],
    ['reference', { turn: { ...accepted.turn, ref: `user_${'0'.repeat(64)}` } }],
    ['role', { turn: { ...accepted.turn, message: { ...accepted.turn.message, role: 'assistant' } } }],
    ['owner metadata', { ownerUserId: 'another-human' }],
    ['shell part', { turn: { ...accepted.turn, message: { ...accepted.turn.message, parts: [{ type: 'shell', artifactId: 'art_0123456789ab' }] } } }],
  ])('refuses substituted %s from an untrusted response', async (_name, fields) => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({ ...accepted, ...fields }));
    await expect(client.nextTurn()).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('refuses an assignment without a lease and does not read the mailbox', async () => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, lease_generation: undefined }));
    await expect(client.nextTurn()).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not retry a refused mailbox request or fall back to direct storage', async () => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({ detail: { error: 'chat_authorization_refused' } }, 404));
    await expect(client.nextTurn()).rejects.toMatchObject({ code: 'denied' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('rejects a replaced lease even if bootstrap could return another assignment', async () => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({ detail: { error: 'capability_invalid' } }, 401))
      .mockResolvedValueOnce(json({ ...binding, lease_generation: 2 }));
    await expect(client.nextTurn()).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
