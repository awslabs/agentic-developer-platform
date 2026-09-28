/**
 * Proactive refresh cadence (issue #4369).
 *
 * The outage: GitHub installation tokens live ~60 min. The worker ticked a
 * proactive refresh every 30 min, and `getToken()` only re-mints inside a 15-min
 * pre-expiry threshold. So the ticks landed at t≈30 (30 min left → skipped) and
 * t≈60 (already expiring) — straddling the window, never refreshing. Every run
 * past an hour then 401ed until it hit the turn cap and produced nothing.
 *
 * The property under test is the INTERACTION of interval and threshold, which is
 * why these tests drive a real fake clock across a simulated hour instead of
 * asserting the two constants separately: either value alone looks perfectly
 * reasonable, and that is exactly how this shipped.
 */

jest.mock('child_process', () => ({ execFileSync: jest.fn() }));

jest.mock('fs', () => ({
  writeFileSync: jest.fn(),
  renameSync: jest.fn(),
  mkdirSync: jest.fn(),
}));

// Every mint returns a token that expires 60 min from "now" — the real GitHub
// contract, and the thing the cadence has to beat.
let mintCount = 0;
jest.mock('@octokit/auth-app', () => ({
  createAppAuth: jest.fn(() =>
    jest.fn().mockImplementation(async () => {
      mintCount++;
      return {
        token: `ghs_minted_${mintCount}`,
        expiresAt: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
      };
    }),
  ),
}));

import { initTokenManager, getToken, needsRefresh, getTokenStatus, setToken } from './token-refresh';
import { writeFileSync } from 'fs';

const MIN = 60 * 1000;
const TOKEN_TTL = 60 * MIN;

/** The cadence agent-worker.ts uses. Mirrors TOKEN_REFRESH_* there. */
const INTERVAL_MS = 5 * MIN;
const THRESHOLD_MS = 20 * MIN;

/** The cadence that caused the outage, kept to prove the test can detect it. */
const OLD_INTERVAL_MS = 30 * MIN;
const OLD_THRESHOLD_MS = 15 * MIN;

const mockedWriteFileSync = writeFileSync as jest.MockedFunction<typeof writeFileSync>;

/**
 * Run a simulated hour of proactive-refresh ticks against a token minted at t=0.
 *
 * @returns the elapsed minutes at which each actual re-mint occurred.
 */
async function simulateRun(opts: { intervalMs: number; thresholdMs: number; durationMs: number }) {
  mintCount = 0;
  const start = Date.now();
  const remintsAtMin: number[] = [];

  initTokenManager({
    appId: '12345',
    privateKey: 'fake-key',
    installationId: '67890',
    owner: 'test-org',
    repo: 'test-repo',
    refreshThresholdMs: opts.thresholdMs,
  });

  // The run starts with a freshly minted 60-min token, as a real pod does.
  setToken('ghs_initial', TOKEN_TTL);

  for (let elapsed = opts.intervalMs; elapsed <= opts.durationMs; elapsed += opts.intervalMs) {
    jest.setSystemTime(start + elapsed);
    const before = getTokenStatus()?.refreshedAt.getTime();
    await getToken();
    const after = getTokenStatus()?.refreshedAt.getTime();
    if (before !== after) {
      remintsAtMin.push(elapsed / MIN);
    }
  }

  return remintsAtMin;
}

describe('proactive refresh cadence beats the 60-minute expiry', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date('2026-08-29T12:00:00Z'));
    jest.clearAllMocks();
    mintCount = 0;
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('re-mints well before the token enters the danger zone', async () => {
    const reminted = await simulateRun({
      intervalMs: INTERVAL_MS,
      thresholdMs: THRESHOLD_MS,
      durationMs: 60 * MIN,
    });

    expect(reminted.length).toBeGreaterThan(0);
    // The first re-mint lands at t=45: the token has 20 min left at t=40 (not yet
    // inside the threshold) and 15 min left at t=45 (inside it), and ticks fall on
    // a 5-min grid. So 45 is the earliest grid point in the window, not a late
    // one — it leaves a quarter-hour of headroom, versus the old cadence's zero.
    expect(reminted[0]).toBeLessThanOrEqual(45);

    // What actually matters is the headroom, so assert it directly rather than
    // trusting the tick number to imply it.
    const headroomMin = 60 - reminted[0];
    expect(headroomMin).toBeGreaterThanOrEqual(15);
  });

  it('gives at least three chances to refresh inside the pre-expiry window', async () => {
    // Headroom is the point: a single in-window tick means one failed mint (a
    // GitHub blip, a gatekeeper 500) still ends the run in a 401 loop.
    const ticksInWindow = (TOKEN_TTL - THRESHOLD_MS) / MIN;
    expect(Math.floor((60 - ticksInWindow) / (INTERVAL_MS / MIN))).toBeGreaterThanOrEqual(3);
  });

  it('never lets the token reach expiry across a simulated hour', async () => {
    const start = Date.now();

    initTokenManager({
      appId: '12345',
      privateKey: 'fake-key',
      installationId: '67890',
      owner: 'test-org',
      repo: 'test-repo',
      refreshThresholdMs: THRESHOLD_MS,
    });
    setToken('ghs_initial', TOKEN_TTL);

    // Walk a 2-hour run minute by minute; after each proactive tick the token must
    // still be comfortably valid. This is the end-state the operator smoke test
    // checks for ("expiresInMin never drops near 0").
    for (let elapsed = INTERVAL_MS; elapsed <= 120 * MIN; elapsed += INTERVAL_MS) {
      jest.setSystemTime(start + elapsed);
      await getToken();

      const status = getTokenStatus()!;
      expect(status.valid).toBe(true);
      expect(status.expiresIn).toBeGreaterThan(THRESHOLD_MS - INTERVAL_MS);
    }
  });

  it('writes the token file on every re-mint so the SDK subprocess sees it', async () => {
    // The file is what git-askpass-helper and gh-wrapper actually read (#1469).
    // A re-mint that updates only env leaves every subprocess git/gh on the stale
    // token — the run still 401s, and the logs still claim success.
    const reminted = await simulateRun({
      intervalMs: INTERVAL_MS,
      thresholdMs: THRESHOLD_MS,
      durationMs: 60 * MIN,
    });

    expect(mockedWriteFileSync).toHaveBeenCalledTimes(reminted.length);
  });

  it('the old 30-min/15-min cadence never refreshed at all', async () => {
    // The regression itself. If this ever starts passing with a non-empty list,
    // the test harness has stopped modelling the bug and the assertions above are
    // no longer meaningful.
    const reminted = await simulateRun({
      intervalMs: OLD_INTERVAL_MS,
      thresholdMs: OLD_THRESHOLD_MS,
      durationMs: 55 * MIN,
    });

    expect(reminted).toEqual([]);
  });
});

describe('needsRefresh threshold boundary', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date('2026-08-29T12:00:00Z'));
    initTokenManager({
      appId: '12345',
      privateKey: 'fake-key',
      installationId: '67890',
      owner: 'test-org',
      repo: 'test-repo',
      refreshThresholdMs: THRESHOLD_MS,
    });
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('refreshes with 19 minutes left (inside a 20-minute threshold)', () => {
    setToken('ghs_x', 19 * MIN);
    expect(needsRefresh()).toBe(true);
  });

  it('does not refresh with 21 minutes left', () => {
    setToken('ghs_x', 21 * MIN);
    expect(needsRefresh()).toBe(false);
  });

  it('refreshes an already-expired token', () => {
    setToken('ghs_x', -1 * MIN);
    expect(needsRefresh()).toBe(true);
  });
});

describe('getTokenStatus exposes refreshedAt', () => {
  beforeEach(() => {
    initTokenManager({
      appId: '12345',
      privateKey: 'fake-key',
      installationId: '67890',
      owner: 'test-org',
      repo: 'test-repo',
    });
  });

  it('reports when the current token was minted', () => {
    // Without this the worker cannot distinguish a real re-mint from a no-op tick,
    // which is why it logged "Token refreshed proactively" on every tick while the
    // token expired underneath it.
    setToken('ghs_x', TOKEN_TTL);
    const status = getTokenStatus()!;

    expect(status.refreshedAt).toBeInstanceOf(Date);
  });

  it('advances refreshedAt when a new token replaces an old one', () => {
    // This is the comparison the worker's tick relies on to decide whether to
    // claim a refresh in the logs.
    jest.useFakeTimers();
    jest.setSystemTime(new Date('2026-08-29T12:00:00Z'));
    setToken('ghs_first', TOKEN_TTL);
    const first = getTokenStatus()!.refreshedAt.getTime();

    jest.setSystemTime(new Date('2026-08-29T12:40:00Z'));
    setToken('ghs_second', TOKEN_TTL);
    const second = getTokenStatus()!.refreshedAt.getTime();

    expect(second).toBeGreaterThan(first);
    jest.useRealTimers();
  });
});
