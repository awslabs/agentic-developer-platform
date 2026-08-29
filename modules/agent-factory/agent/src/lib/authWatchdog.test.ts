/**
 * Auth watchdog escalation (issue #4369).
 *
 * The bug this guards: a long run whose GitHub installation token expired kept
 * getting `401 Bad credentials` on every push inside the SDK subprocess, retried
 * forever, and exited at the turn cap having produced nothing — while its
 * heartbeat comment kept updating, so it looked healthy the whole time.
 *
 * The escalation has two competing failure modes and both are pinned here:
 * escalate too late (or never) and >1h runs keep dying silently; escalate on a
 * single transient 401 and healthy runs get killed and duplicated on redelivery.
 */

import { AuthWatchdog, looksLikeAuthFailure } from './authWatchdog';

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

describe('AuthWatchdog escalation', () => {
  const FAIL = 'gh: 401 Bad credentials';
  const OK = 'pushed 1 commit';

  it('stays quiet below the threshold so a transient 401 cannot kill a healthy run', () => {
    const wd = new AuthWatchdog();

    expect(wd.observe(FAIL)).toBe('none');
    expect(wd.observe(FAIL)).toBe('none');
  });

  it('asks for a forced refresh once the failures are consecutive enough to rule out a blip', () => {
    const wd = new AuthWatchdog();

    wd.observe(FAIL);
    wd.observe(FAIL);

    expect(wd.observe(FAIL)).toBe('force_refresh');
  });

  it('aborts when 401s continue after a forced refresh', () => {
    const wd = new AuthWatchdog();

    // First cluster → refresh.
    expect([wd.observe(FAIL), wd.observe(FAIL), wd.observe(FAIL)].pop()).toBe('force_refresh');

    // Second cluster → the credential itself is dead (revoked installation, or a
    // token we cannot reach). Looping to the turn cap produces nothing, so give
    // the task back for redelivery instead.
    wd.observe(FAIL);
    wd.observe(FAIL);
    expect(wd.observe(FAIL)).toBe('abort');
  });

  it('never aborts a run that recovered, however many scattered 401s it saw', () => {
    const wd = new AuthWatchdog();

    // Two failures then success, repeated far more times than the threshold. A
    // watchdog counting cumulative failures rather than consecutive ones would
    // abort here — killing a run that is working fine.
    for (let i = 0; i < 20; i++) {
      expect(wd.observe(FAIL)).toBe('none');
      expect(wd.observe(FAIL)).toBe('none');
      expect(wd.observe(OK)).toBe('none');
    }

    expect(wd.failureCount).toBe(0);
    expect(wd.hasForcedRefresh).toBe(false);
  });

  it('treats a post-refresh recovery as healthy rather than latching toward abort', () => {
    const wd = new AuthWatchdog();

    wd.observe(FAIL);
    wd.observe(FAIL);
    expect(wd.observe(FAIL)).toBe('force_refresh');

    // The refresh worked — this is the SUCCESS case for the whole feature.
    expect(wd.observe(OK)).toBe('none');

    // A much later, unrelated single 401 must not abort just because a refresh
    // happened an hour ago.
    expect(wd.observe(FAIL)).toBe('none');
    expect(wd.observe(FAIL)).toBe('none');
  });

  it('honours a custom threshold', () => {
    const wd = new AuthWatchdog({ threshold: 2 });

    expect(wd.observe(FAIL)).toBe('none');
    expect(wd.observe(FAIL)).toBe('force_refresh');
  });

  it('only requests one forced refresh per cluster', () => {
    const wd = new AuthWatchdog();

    wd.observe(FAIL);
    wd.observe(FAIL);
    expect(wd.observe(FAIL)).toBe('force_refresh');

    // Immediately after escalating, the counter restarts — the next two sightings
    // must not re-request a refresh that is already in flight.
    expect(wd.observe(FAIL)).toBe('none');
    expect(wd.observe(FAIL)).toBe('none');
  });
});
