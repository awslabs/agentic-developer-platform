import { createHash } from 'node:crypto';
import { ChatDataClient } from './chat-data-client';
import { withSessionHeartbeat } from '../sandbox-session-heartbeat';

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

  afterEach(() => { jest.useRealTimers(); jest.restoreAllMocks(); });

  it.each([
    { mode: 'persistent', health: 'active', sequence: 1, pending_mode: 'ephemeral' },
    { mode: 'persistent', health: 'ending', sequence: 1, cleanup_elapsed_seconds: 10 },
    { mode: 'persistent', health: 'cleanup_delayed', sequence: 1, pending_mode: 'ephemeral', cleanup_elapsed_seconds: 120 },
  ])('reads authoritative lifecycle state without changing the bound mode: %j', async state => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' })).mockResolvedValueOnce(json(state));
    await expect(client.sessionState()).resolves.toEqual(state);
    await expect(client.sessionMode()).resolves.toBe('persistent');
  });

  it.each([
    { pending_mode: 'unknown' }, { cleanup_elapsed_seconds: -1 }, { cleanup_elapsed_seconds: 1.5 },
    { mode: 'ephemeral' }, { ownerUserId: 'another-user' },
  ])('rejects malformed or scope-changing lifecycle state: %j', async changes => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockResolvedValueOnce(json({ mode: 'persistent', health: 'active', sequence: 1, ...changes }));
    await expect(client.sessionState()).rejects.toMatchObject({ code: 'scope_mismatch' });
  });

  it('keeps the current turn alive through a heartbeat while a mode switch is pending', async () => {
    jest.useFakeTimers({ doNotFake: ['Date'] });
    fetchMock.mockImplementation(async input => {
      if (String(input).endsWith('/bootstrap')) return json({ ...binding, session_mode: 'persistent' });
      if (String(input).endsWith('/session/state')) {
        return json({ mode: 'persistent', health: 'active', sequence: 1, pending_mode: 'ephemeral' });
      }
      throw new Error('Unexpected gateway request');
    });
    let finish!: () => void;
    let turnSignal!: AbortSignal;
    const running = withSessionHeartbeat(client, async signal => {
      turnSignal = signal;
      await new Promise<void>(resolve => {
        finish = resolve;
        signal.addEventListener('abort', () => resolve(), { once: true });
      });
    }, undefined, 20).then(() => 'completed', error => error.code);
    await jest.advanceTimersByTimeAsync(20);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/session/state'))).toHaveLength(1);
    expect(turnSignal.aborted).toBe(false);
    finish();
    await expect(running).resolves.toBe('completed');
  });

  it('polls only the bootstrap-bound session and validates the next sequence', async () => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockResolvedValueOnce(json({ run_id: 'run-a', session_id: 'session-a', lease_generation: 1,
        turn: { sequence: 1, turn_id: 'turn-a', message: 'follow up' } }));
    await expect(client.nextMailboxTurn(0)).resolves.toEqual({ sequence: 1, turn_id: 'turn-a', message: 'follow up' });
    expect(fetchMock.mock.calls[1]).toEqual([
      'https://gateway.example.test/v1/chat/data/session/next', expect.objectContaining({
        body: JSON.stringify({ run_id: 'run-a', session_id: 'session-a', after: 0 }),
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer synthetic.scoped.capability',
          'X-Adp-Workload-Token': 'sandbox.workload.token' },
      }),
    ]);
  });

  it.each([
    { run_id: 'run-b' }, { session_id: 'session-b' }, { lease_generation: 2 },
    { turn: { sequence: 2, turn_id: 'turn-a', message: 'follow up' } },
  ])('rejects a mismatched mailbox response', async change => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockResolvedValueOnce(json({ run_id: 'run-a', session_id: 'session-a', lease_generation: 1,
        turn: { sequence: 1, turn_id: 'turn-a', message: 'follow up' }, ...change }));
    await expect(client.nextMailboxTurn(0)).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('does not poll for ephemeral assignments or malformed cursors', async () => {
    await expect(client.nextMailboxTurn(-1)).rejects.toMatchObject({ code: 'invalid_request' });
    fetchMock.mockResolvedValueOnce(json(binding));
    await expect(client.nextMailboxTurn(0)).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('advances to a freshly fenced grant only for the next turn in its own session', async () => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockResolvedValueOnce(json({ ...binding, capability: 'next.capability', run_id: 'run-b', lease_generation: 2, session_mode: 'persistent' }));
    await client.admitMailboxTurn({ sequence: 2, turn_id: 'run-b', message: 'Follow up' });
    expect(JSON.parse(fetchMock.mock.calls[1][1]!.body as string)).toEqual({ run_id: 'run-a', session_id: 'session-a', after: 1 });
    await expect(client.sessionScope()).resolves.toEqual({ run_id: 'run-b', session_id: 'session-a' });
  });

  it.each([{ session_id: 'other-session' }, { run_id: 'other-run' }, { lease_generation: 1 }, { session_mode: 'ephemeral' }])(
    'rejects an unrelated follow-up grant %j without changing the cached scope', async change => {
      fetchMock.mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
        .mockResolvedValueOnce(json({ ...binding, run_id: 'run-b', lease_generation: 2, session_mode: 'persistent', ...change }));
      await expect(client.admitMailboxTurn({ sequence: 2, turn_id: 'run-b', message: 'Follow up' })).rejects.toMatchObject({ code: 'scope_mismatch' });
      await expect(client.sessionScope()).resolves.toEqual({ run_id: 'run-a', session_id: 'session-a' });
    },
  );

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

  it('starts persistent polling after its protected initial sequence, not at zero', async () => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockResolvedValueOnce(json({ ...accepted, session_sequence: 2 }))
      .mockResolvedValueOnce(json({ ...accepted, turn: { sequence: 3, turn_id: 'run-next', message: 'Next' } }));
    const initial = await client.nextTurn();
    expect(initial).toEqual({ ...accepted.turn, session_sequence: 2 });
    await expect(client.nextMailboxTurn(initial.session_sequence!)).resolves.toEqual({ sequence: 3, turn_id: 'run-next', message: 'Next' });
    expect(JSON.parse(fetchMock.mock.calls[2][1]!.body as string)).toEqual({ run_id: 'run-a', session_id: 'session-a', after: 2 });
  });

  it.each([
    [undefined, true], [0, true], [100_000_000, true], [1, false],
  ])('refuses a misplaced or forged initial cursor %s for mode %s', async (sequence, persistent) => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, ...(persistent ? { session_mode: 'persistent' } : {}) }))
      .mockResolvedValueOnce(json({ ...accepted, ...(sequence !== undefined ? { session_sequence: sequence } : {}) }));
    await expect(client.nextTurn()).rejects.toMatchObject({ code: 'invalid_response' });
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
