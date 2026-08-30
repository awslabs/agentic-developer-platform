/**
 * Auth watchdog escalation (issues #4369, #4430).
 *
 * The bug this guards: a long run whose GitHub installation token expired kept
 * getting `401 Bad credentials` on every push inside the SDK subprocess, retried
 * forever, and exited at the turn cap having produced nothing — while its
 * heartbeat comment kept updating, so it looked healthy the whole time.
 *
 * The first shipped watchdog (#4382) counted CONSECUTIVE failures and reset on
 * any other output. Production streams interleave failures with assistant prose
 * ("Let me retry that..."), so the streak never reached its threshold: 27 and
 * 36 real 401s produced zero escalations (#4430). The interleaved test below is
 * the central regression test — it reproduces exactly what both failing pods
 * did and FAILS on the streak-based implementation.
 *
 * The escalation has two competing failure modes and both are pinned here:
 * escalate too late (or never) and >1h runs keep dying silently; escalate on a
 * single transient 401 and healthy runs get killed and duplicated on redelivery.
 */

import { AuthWatchdog, looksLikeAuthFailure, looksLikeSuccessfulWrite } from './authWatchdog';

describe('looksLikeAuthFailure', () => {
  it.each([
    ['Bad credentials', 'gh API: 401 Bad credentials'],
    ['bare 401 status', 'HTTP 401'],
    ['API status form', 'status: 401'],
    ['401 Unauthorized', 'remote returned 401 Unauthorized'],
    // git-over-HTTPS never prints "401" for a stale token — it says this instead,
    // which is exactly the text a failing `git push` produces.
    ['git auth failure', "fatal: Authentication failed for 'https://github.com/o/r/'"],
    ['git credential rejection', 'remote: Invalid username or password'],
    // What CheckRunStreamer._doPatch throws on a stale token, verbatim — this is
    // the message the onPatchError hook feeds into the watchdog (#4430 fix B2).
    ['streamer PATCH failure', 'HTTP 401: {"message":"Bad credentials"}'],
  ])('detects %s', (_label, text) => {
    expect(looksLikeAuthFailure(text)).toBe(true);
  });

  it.each([
    ['empty output', ''],
    ['ordinary success', 'To github.com:o/r.git\n   abc123..def456  main -> main'],
    // A 404 is the wrong-installation symptom (#4071), a different bug with a
    // different remedy — refreshing the token would not help.
    ['a 404', 'gh: Not Found (HTTP 404)'],
    ['an unrelated 500', 'HTTP 500 internal server error'],
  ])('does not fire on %s', (_label, text) => {
    expect(looksLikeAuthFailure(text)).toBe(false);
  });

  it('matches regardless of case', () => {
    expect(looksLikeAuthFailure('BAD CREDENTIALS')).toBe(true);
  });
});

describe('looksLikeSuccessfulWrite', () => {
  it.each([
    ['a push summary', 'pushed 1 commit'],
    ['git push ref update', 'To https://github.com/o/r.git\n   abc123..def456  main -> main'],
    ['an up-to-date push', 'Everything up-to-date'],
    ['gh pr create output', 'https://github.com/o/r/pull/123'],
  ])('recognises %s', (_label, text) => {
    expect(looksLikeSuccessfulWrite(text)).toBe(true);
  });

  it.each([
    ['empty output', ''],
    // The production killer: prose between failures is NOT proof of health.
    ['assistant prose', 'Let me retry that...'],
    ['a tool result with no write', 'Read 40 lines from src/main.ts'],
  ])('does not treat %s as recovery', (_label, text) => {
    expect(looksLikeSuccessfulWrite(text)).toBe(false);
  });
});

describe('AuthWatchdog escalation', () => {
  const FAIL = 'gh: 401 Bad credentials';
  const OK = 'pushed 1 commit';
  const PROSE = 'Let me retry that...';

  /** Watchdog on a controllable clock. */
  function makeWatchdog(opts: ConstructorParameters<typeof AuthWatchdog>[0] = {}) {
    const clock = { nowMs: 0 };
    const wd = new AuthWatchdog({ now: () => clock.nowMs, ...opts });
    return { wd, clock };
  }

  it('stays quiet below the threshold so a transient 401 cannot kill a healthy run', () => {
    const { wd } = makeWatchdog();

    expect(wd.observe(FAIL)).toBe('none');
    expect(wd.observe(FAIL)).toBe('none');
  });

  it('escalates on INTERLEAVED failures — the stream shape production actually produces (#4430)', () => {
    // The central regression test. Both failing pods alternated
    // tool_result 401s with assistant prose; a streak counter that resets on
    // the prose never escalates. 3 failures inside the window must escalate
    // regardless of what non-write output lands between them.
    const { wd, clock } = makeWatchdog();

    expect(wd.observe(FAIL)).toBe('none');
    clock.nowMs += 30_000;
    expect(wd.observe(PROSE)).toBe('none');
    expect(wd.observe(FAIL)).toBe('none');
    clock.nowMs += 30_000;
    expect(wd.observe(PROSE)).toBe('none');
    expect(wd.observe(FAIL)).toBe('force_refresh');
  });

  it('asks for a forced refresh once failures cluster inside the window', () => {
    const { wd } = makeWatchdog();

    wd.observe(FAIL);
    wd.observe(FAIL);

    expect(wd.observe(FAIL)).toBe('force_refresh');
  });

  it('aborts when 401s are still arriving past the deadline after a forced refresh', () => {
    const { wd, clock } = makeWatchdog();

    // First cluster → refresh.
    expect([wd.observe(FAIL), wd.observe(FAIL), wd.observe(FAIL)].pop()).toBe('force_refresh');

    // Within the post-refresh grace period the re-mint may not have landed yet.
    clock.nowMs += 60_000; // +1 min
    expect(wd.observe(FAIL)).toBe('none');

    // Past the deadline the credential itself is dead (revoked installation, or
    // a token holder we cannot reach). Looping to the turn cap produces
    // nothing, so give the task back for redelivery instead.
    clock.nowMs += 5 * 60_000; // +6 min total
    expect(wd.observe(FAIL)).toBe('abort');
  });

  it('bounds the total time from first 401 to abort', () => {
    // Worst case ≈ windowMs (fill the window) + abortAfterRefreshMs (confirm the
    // refresh did not help). With defaults that is ~15 min — against 68 and
    // 152 min observed hangs on the pods that motivated #4430.
    const { wd, clock } = makeWatchdog();

    wd.observe(FAIL); //  t=0
    clock.nowMs += 5 * 60_000;
    wd.observe(FAIL); //  t=5 min
    clock.nowMs += 4 * 60_000;
    expect(wd.observe(FAIL)).toBe('force_refresh'); // t=9 min — window filled

    clock.nowMs += 5 * 60_000; // t=14 min — deadline passed
    expect(wd.observe(FAIL)).toBe('abort');
  });

  it('never escalates on failures spread beyond the window with successful writes between them', () => {
    // Anti-flap: scattered blips over a long run, each followed by a write that
    // went through, are not a cluster. A cumulative counter — or a window that
    // ignores recovery — would abort here, killing a run that is working fine.
    const { wd, clock } = makeWatchdog();

    for (let i = 0; i < 20; i++) {
      expect(wd.observe(FAIL)).toBe('none');
      expect(wd.observe(FAIL)).toBe('none');
      expect(wd.observe(OK)).toBe('none');
      clock.nowMs += 15 * 60_000; // next blip lands outside the window anyway
    }

    expect(wd.failureCount).toBe(0);
    expect(wd.hasForcedRefresh).toBe(false);
  });

  it('never aborts a run that recovered, however many scattered 401s it saw', () => {
    // Same anti-flap guarantee with NO clock movement: the successful write
    // alone must clear the cluster, even when everything lands in one window.
    const { wd } = makeWatchdog();

    for (let i = 0; i < 20; i++) {
      expect(wd.observe(FAIL)).toBe('none');
      expect(wd.observe(FAIL)).toBe('none');
      expect(wd.observe(OK)).toBe('none');
    }

    expect(wd.failureCount).toBe(0);
    expect(wd.hasForcedRefresh).toBe(false);
  });

  it('treats a post-refresh recovery as healthy rather than latching toward abort', () => {
    const { wd, clock } = makeWatchdog();

    wd.observe(FAIL);
    wd.observe(FAIL);
    expect(wd.observe(FAIL)).toBe('force_refresh');

    // The refresh worked — this is the SUCCESS case for the whole feature. Only
    // a successful WRITE evidences it (prose does not, #4430).
    expect(wd.observe(OK)).toBe('none');

    // A much later, unrelated single 401 must not abort just because a refresh
    // happened an hour ago.
    clock.nowMs += 60 * 60_000;
    expect(wd.observe(FAIL)).toBe('none');
    expect(wd.observe(FAIL)).toBe('none');
  });

  it('does not let assistant prose stand in for recovery (#4430)', () => {
    const { wd } = makeWatchdog();

    wd.observe(FAIL);
    wd.observe(PROSE); // must NOT clear the cluster
    wd.observe(FAIL);
    wd.observe(PROSE);
    expect(wd.failureCount).toBe(2);
    expect(wd.observe(FAIL)).toBe('force_refresh');
  });

  it('drops failures that age out of the window', () => {
    const { wd, clock } = makeWatchdog();

    wd.observe(FAIL);
    wd.observe(FAIL);
    clock.nowMs += 11 * 60_000; // both now older than the 10-min window

    // Only 1 failure in the window → below threshold.
    expect(wd.observe(FAIL)).toBe('none');
    expect(wd.failureCount).toBe(1);
  });

  it('honours a custom threshold', () => {
    const { wd } = makeWatchdog({ threshold: 2 });

    expect(wd.observe(FAIL)).toBe('none');
    expect(wd.observe(FAIL)).toBe('force_refresh');
  });

  it('only requests one forced refresh per cluster', () => {
    const { wd } = makeWatchdog();

    wd.observe(FAIL);
    wd.observe(FAIL);
    expect(wd.observe(FAIL)).toBe('force_refresh');

    // Immediately after escalating, further sightings inside the grace period
    // must not re-request a refresh that is already in flight.
    expect(wd.observe(FAIL)).toBe('none');
    expect(wd.observe(FAIL)).toBe('none');
  });
});
