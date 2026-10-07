import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { withSessionHeartbeat } from './sandbox-session-heartbeat';

const binding = { run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent' as const };

function client() {
  return {
    sessionMode: jest.fn().mockResolvedValue('persistent'),
    renewSession: jest.fn().mockResolvedValue(binding),
    sessionState: jest.fn().mockResolvedValue({ mode: 'persistent', health: 'active', sequence: 1 }),
  } as unknown as jest.Mocked<ChatDataClient>;
}

describe('persistent sandbox lease heartbeat', () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => { jest.clearAllTimers(); jest.useRealTimers(); });

  it('renews the bound session during work and stops when work finishes', async () => {
    const gateway = client();
    let finish!: () => void;
    const work = jest.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    const running = withSessionHeartbeat(gateway, work);
    await jest.advanceTimersByTimeAsync(20_000);
    expect(gateway.renewSession).toHaveBeenCalledTimes(2);
    expect(gateway.sessionState).toHaveBeenCalledTimes(1);
    expect(work).toHaveBeenCalledTimes(1);
    finish();
    await running;
    await jest.advanceTimersByTimeAsync(40_000);
    expect(gateway.renewSession).toHaveBeenCalledTimes(2);
  });

  it('aborts pending work and surfaces a refused renewal', async () => {
    const gateway = client();
    jest.mocked(gateway.renewSession).mockResolvedValueOnce(binding).mockRejectedValueOnce(new ChatDataError('denied', 404));
    const work = jest.fn(async (signal: AbortSignal) => {
      await new Promise<void>((resolve, reject) => {
        signal.addEventListener('abort', () => reject(signal.reason), { once: true });
      });
    });
    const running = withSessionHeartbeat(gateway, work);
    const rejected = expect(running).rejects.toMatchObject({ code: 'denied' });
    await jest.advanceTimersByTimeAsync(20_000);
    await rejected;
    expect(work).toHaveBeenCalledTimes(1);
  });

  it.each(['recovering', 'cleanup_delayed'] as const)('rejects %s health instead of committing after authority loss', async health => {
    const gateway = client();
    jest.mocked(gateway.sessionState).mockResolvedValueOnce({ mode: 'persistent', health, sequence: 1 });
    const work = jest.fn(async (signal: AbortSignal) => {
      await new Promise<void>((resolve, reject) => {
        signal.addEventListener('abort', () => reject(signal.reason), { once: true });
      });
    });
    const running = withSessionHeartbeat(gateway, work);
    const rejected = expect(running).rejects.toMatchObject({ code: 'scope_mismatch' });
    await jest.advanceTimersByTimeAsync(20_000);
    await rejected;
  });

  it('waits for an in-flight renewal before returning a completed turn', async () => {
    const gateway = client();
    let rejectRenewal!: (error: Error) => void;
    jest.mocked(gateway.renewSession).mockResolvedValueOnce(binding)
      .mockImplementationOnce(() => new Promise((resolve, reject) => { rejectRenewal = reject; }));
    let finish!: () => void;
    const running = withSessionHeartbeat(gateway, async () => new Promise<void>(resolve => { finish = resolve; }));
    await jest.advanceTimersByTimeAsync(20_000);
    finish();
    const rejected = expect(running).rejects.toMatchObject({ code: 'denied' });
    rejectRenewal(new ChatDataError('denied', 404));
    await rejected;
  });

  it('does not schedule heartbeat for ephemeral execution', async () => {
    const gateway = client();
    jest.mocked(gateway.sessionMode).mockResolvedValueOnce('ephemeral');
    const work = jest.fn().mockResolvedValue(undefined);
    await withSessionHeartbeat(gateway, work);
    await jest.advanceTimersByTimeAsync(40_000);
    expect(gateway.renewSession).not.toHaveBeenCalled();
    expect(gateway.sessionState).not.toHaveBeenCalled();
  });
});
