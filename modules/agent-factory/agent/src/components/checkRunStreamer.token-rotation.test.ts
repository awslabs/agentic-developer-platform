/**
 * CheckRunStreamer token handling (#4430, fixes A and B2).
 *
 * The bug: the streamer captured `GITHUB_TOKEN` once at construction and sent
 * it on every PATCH for the life of the run. A run outlives its ~60-min
 * installation token, so once the startup token expired every remaining PATCH
 * 401ed — no matter how many times the token manager re-minted into env/file,
 * because the streamer read neither. The pod fingerprint was `PATCH failed
 * 66/150` with 27× `Bad credentials` (#4430 §2).
 *
 * These tests pin the two halves of the fix:
 *  - Fix A: the token is resolved via `tokenProvider()` AT PATCH TIME, so a
 *    re-mint reaches the very next PATCH. (Fails on the captured-string code:
 *    a value passed by construction cannot change.)
 *  - Fix B2: a failing PATCH surfaces its error through `onPatchError`, in a
 *    form `looksLikeAuthFailure` recognises, so the auth watchdog finally sees
 *    the highest-signal 401s in the run instead of a swallowed WARN log.
 */

import { CheckRunStreamer, CheckRunStreamerConfig } from './checkRunStreamer';
import { looksLikeAuthFailure } from '../lib/authWatchdog';

function makeConfig(overrides: Partial<CheckRunStreamerConfig> = {}): CheckRunStreamerConfig {
  return {
    checkRunId: 42,
    repo: 'acme/adp',
    tokenProvider: () => 'ghs_initial',
    persona: 'developer',
    issueNumber: 4435,
    model: 'global.anthropic.claude-sonnet-4-6',
    log: () => {},
    ...overrides,
  };
}

/** Let the fire-and-forget PATCH promise chain settle. */
async function flushPatches(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('CheckRunStreamer token rotation (fix A)', () => {
  let authHeaders: string[];

  beforeEach(() => {
    authHeaders = [];
    global.fetch = jest.fn().mockImplementation(async (_url: string, init: RequestInit) => {
      authHeaders.push((init.headers as Record<string, string>).Authorization);
      return { ok: true } as Response;
    }) as unknown as typeof fetch;
  });

  it('resolves the token at PATCH time, so a re-mint reaches the next PATCH', async () => {
    // Mirrors production: the token manager rewrites the source the provider
    // reads; the streamer itself holds no copy.
    let currentToken = 'ghs_before_expiry';
    const s = new CheckRunStreamer(makeConfig({ tokenProvider: () => currentToken }));

    s.onResult({ costUsd: 0.01 }); // immediate PATCH #1
    await flushPatches();

    currentToken = 'ghs_after_remint'; // token manager re-mints mid-run
    s.onResult({ costUsd: 0.02 }); // immediate PATCH #2
    await flushPatches();
    s.destroy();

    expect(authHeaders).toEqual(['Bearer ghs_before_expiry', 'Bearer ghs_after_remint']);
  });

  it('calls the provider on every PATCH rather than caching its first result', async () => {
    const tokenProvider = jest.fn().mockReturnValue('ghs_fresh');
    const s = new CheckRunStreamer(makeConfig({ tokenProvider }));

    s.onResult({});
    await flushPatches();
    s.onResult({});
    await flushPatches();
    s.destroy();

    expect(tokenProvider).toHaveBeenCalledTimes(2);
  });
});

describe('CheckRunStreamer onPatchError (fix B2)', () => {
  it('surfaces a 401 PATCH failure in a form the auth watchdog recognises', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 401,
      text: async () => '{"message":"Bad credentials"}',
    }) as unknown as typeof fetch;

    const seen: string[] = [];
    const s = new CheckRunStreamer(makeConfig({ onPatchError: (msg) => seen.push(msg) }));

    s.onResult({});
    await flushPatches();
    s.destroy();

    expect(seen).toHaveLength(1);
    // The exact contract fix B2 relies on: what _doPatch throws must satisfy
    // the watchdog's matcher with NO matcher change.
    expect(looksLikeAuthFailure(seen[0])).toBe(true);
  });

  it('stays fail-soft when the hook itself throws', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 401,
      text: async () => 'Bad credentials',
    }) as unknown as typeof fetch;

    const s = new CheckRunStreamer(
      makeConfig({
        onPatchError: () => {
          throw new Error('watchdog exploded');
        },
      }),
    );

    // Must not reject or throw into the caller — PATCH errors never propagate.
    s.onResult({});
    await flushPatches();
    s.destroy();
  });

  it('does not fire the hook on a successful PATCH', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true }) as unknown as typeof fetch;

    const seen: string[] = [];
    const s = new CheckRunStreamer(makeConfig({ onPatchError: (msg) => seen.push(msg) }));

    s.onResult({});
    await flushPatches();
    s.destroy();

    expect(seen).toHaveLength(0);
  });
});
