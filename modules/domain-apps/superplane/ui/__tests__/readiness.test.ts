/**
 * Control-plane health never becomes workspace execution readiness — #5730 AC-04.
 *
 * The final sentence of AC-04 is a prohibition: "Control-plane health alone never
 * marks a workspace execution-ready." The tests below are the evidence for it. The
 * most important one is `describe('the AC-04 prohibition')`, which sets up the
 * precise situation in which the mistake is tempting — a perfectly healthy API and
 * a workspace that cannot run anything — and asserts the module cannot be talked
 * into conflating them.
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ValidationReading, WorkspaceSummary } from '@superplane-ui/contract';
import {
  FRESHNESS_TICK_MS,
  STALE_AFTER_MS,
  buildReadiness,
  controlPlaneReading,
  freshnessOf,
  observationFor,
  providerReading,
  useFreshnessClock,
  workspaceReading,
} from '@superplane-ui/readiness';

/** A fixed clock. Real time in a readiness test would make staleness flaky. */
const NOW = Date.parse('2026-09-23T12:00:00.000Z');
const JUST_NOW = new Date(NOW - 1_000).toISOString();
const LONG_AGO = new Date(NOW - STALE_AFTER_MS - 1_000).toISOString();

function workspace(overrides: Partial<WorkspaceSummary> = {}): WorkspaceSummary {
  return {
    id: 'ws-1',
    org_id: 'org-1',
    name: 'research',
    display_name: 'Research',
    isolation_mode: 'dedicated',
    status: 'Active',
    cluster_health: 'Healthy',
    last_heartbeat: JUST_NOW,
    created_at: JUST_NOW,
    updated_at: JUST_NOW,
    ...overrides,
  };
}

function validation(overrides: Partial<ValidationReading> = {}): ValidationReading {
  return {
    credential_valid: true,
    permissions_sufficient: true,
    quota_available: true,
    observed_capacity: 4,
    checked_at: JUST_NOW,
    ...overrides,
  };
}

describe('the AC-04 prohibition', () => {
  it('reports a healthy control plane and a NOT-ready workspace at the same time', () => {
    // The exact situation onboarding starts in: the API is up and answering, and
    // the workspace being created has not finished provisioning. A screen that
    // rendered one badge from the health check would call this ready.
    const report = buildReadiness(
      {
        health: { status: 'ok' },
        healthObservedAt: JUST_NOW,
        workspace: workspace({ status: 'Provisioning', cluster_health: null, last_heartbeat: null }),
        validation: null,
        admitsNewWork: null,
      },
      NOW,
    );

    expect(report.controlPlane.ready).toBe(true);
    expect(report.workspace.ready).toBe(false);
    expect(report.provider.ready).toBeNull();
  });

  it('exposes no aggregate readiness value a caller could mistake for execution readiness', () => {
    // The absence is the feature, so it is asserted rather than assumed. A future
    // `overallReady` convenience field would let a caller enable "run a workload"
    // from a liveness check, which is the defect this whole module exists to stop.
    const report = buildReadiness(
      {
        health: { status: 'ok' },
        healthObservedAt: JUST_NOW,
        workspace: workspace({ status: 'Provisioning' }),
        validation: null,
        admitsNewWork: null,
      },
      NOW,
    );

    expect(Object.keys(report).sort()).toEqual(['controlPlane', 'provider', 'workspace']);
    for (const forbidden of ['overallReady', 'ready', 'isReady', 'executionReady']) {
      expect(report, forbidden).not.toHaveProperty(forbidden);
    }
  });

  it('says in words that control-plane health does not establish execution readiness', () => {
    // The user-facing half of the same guarantee. Being correct in the data model
    // and misleading in the sentence beside it would still mislead the user.
    const reading = controlPlaneReading({ status: 'ok' }, JUST_NOW, NOW);
    expect(reading.ready).toBe(true);
    expect(reading.reason).toMatch(/does not establish that any workspace can run work/i);
  });

  it('never derives a workspace reading from the health response', () => {
    // Same healthy control plane, three different workspaces, three different
    // workspace readings — demonstrating the readings are independently sourced.
    const common = { health: { status: 'ok' }, healthObservedAt: JUST_NOW, validation: null, admitsNewWork: null };
    const provisioning = buildReadiness({ ...common, workspace: workspace({ status: 'Provisioning' }) }, NOW);
    const silent = buildReadiness({ ...common, workspace: workspace({ last_heartbeat: LONG_AGO }) }, NOW);
    const healthy = buildReadiness({ ...common, workspace: workspace() }, NOW);

    expect(provisioning.controlPlane.ready).toBe(true);
    expect(silent.controlPlane.ready).toBe(true);
    expect(healthy.controlPlane.ready).toBe(true);

    expect(provisioning.workspace.ready).toBe(false);
    expect(silent.workspace.ready).toBeNull();
    expect(healthy.workspace.ready).toBe(true);
  });
});

describe('freshness', () => {
  it('distinguishes never-observed from observed-too-long-ago', () => {
    // `unknown` and `stale` must not collapse: a workspace that never reported is
    // a different fault from one that stopped reporting, and they are fixed
    // differently.
    expect(freshnessOf(null, NOW)).toBe('unknown');
    expect(freshnessOf(undefined, NOW)).toBe('unknown');
    expect(freshnessOf(LONG_AGO, NOW)).toBe('stale');
    expect(freshnessOf(JUST_NOW, NOW)).toBe('fresh');
  });

  it('treats an unparseable timestamp as unknown rather than as epoch', () => {
    // `Date.parse('garbage')` is NaN, and NaN comparisons are false — so a naive
    // implementation would silently report "fresh" for a corrupt timestamp.
    expect(freshnessOf('not a date', NOW)).toBe('unknown');
  });

  it('matches the domain API staleness threshold exactly', () => {
    // The API degrades a cluster to `Degraded` after five minutes. A longer UI
    // window would render "fresh" beside a server-side `Degraded`.
    expect(STALE_AFTER_MS).toBe(5 * 60 * 1000);
    expect(freshnessOf(new Date(NOW - STALE_AFTER_MS + 1).toISOString(), NOW)).toBe('fresh');
    expect(freshnessOf(new Date(NOW - STALE_AFTER_MS - 1).toISOString(), NOW)).toBe('stale');
  });
});

describe('workspace readiness', () => {
  it('requires both a successful status and a fresh healthy observation', () => {
    expect(workspaceReading(workspace(), NOW).ready).toBe(true);
  });

  it('reports a stale healthy observation as unknown, not ready', () => {
    // A cluster that was healthy ten minutes ago and has said nothing since is
    // not evidence that it is healthy now. This is how a "ready" badge outlives
    // the thing it describes.
    const reading = workspaceReading(workspace({ last_heartbeat: LONG_AGO }), NOW);
    expect(reading.ready).toBeNull();
    expect(reading.freshness).toBe('stale');
    expect(reading.reason).toMatch(/not reported recently/i);
  });

  it('reports a never-reporting workspace distinctly from a stale one', () => {
    const reading = workspaceReading(workspace({ last_heartbeat: null }), NOW);
    expect(reading.ready).toBeNull();
    expect(reading.freshness).toBe('unknown');
    expect(reading.reason).toMatch(/never reported/i);
  });

  it('does not turn a fresh heartbeat without a health reading into a failure or a pass', () => {
    const reading = workspaceReading(workspace({ cluster_health: null }), NOW);
    expect(reading.ready).toBeNull();
    expect(reading.freshness).toBe('fresh');
    expect(reading.reason).toMatch(/health has not been reported/i);
  });

  it('reports an unhealthy cluster as not ready, distinct from unknown', () => {
    const reading = workspaceReading(workspace({ cluster_health: 'Degraded' }), NOW);
    expect(reading.ready).toBe(false);
    expect(reading.reason).toContain('Degraded');
  });

  it('names the blocking status so the user knows whether to wait or act', () => {
    // "Provisioning" is a wait; "Failed" is an action. A generic "not ready"
    // would not tell them which.
    expect(workspaceReading(workspace({ status: 'Provisioning' }), NOW).reason).toContain('Provisioning');
    expect(workspaceReading(workspace({ status: 'Failed' }), NOW).reason).toContain('Failed');
  });

  it('reports no selected workspace as unknown rather than not-ready', () => {
    // Nothing is wrong when no workspace is selected; rendering a red "not ready"
    // on a zero-workspace control plane would invent a fault.
    const reading = workspaceReading(null, NOW);
    expect(reading.ready).toBeNull();
  });
});

describe('provider readiness', () => {
  it('requires all three validated readings to hold', () => {
    expect(providerReading(validation(), true, NOW).ready).toBe(true);
  });

  it('names which reading failed, because each has a different remedy', () => {
    // An invalid credential is re-entered, thin permissions are widened at the
    // provider, and exhausted quota is raised or waited out. One generic
    // "provider not ready" sends the user looking in the wrong place.
    expect(providerReading(validation({ credential_valid: false }), true, NOW).reason)
      .toMatch(/credential is not valid/i);
    expect(providerReading(validation({ permissions_sufficient: false }), true, NOW).reason)
      .toMatch(/permissions are insufficient/i);
    expect(providerReading(validation({ quota_available: false }), true, NOW).reason)
      .toMatch(/no quota is available/i);
  });

  it('reports an unmeasured reading as unknown, not as a pass', () => {
    // `null` means the service did not take the reading. Treating a missing
    // reading as satisfied is how an unvalidated credential reaches production.
    const reading = providerReading(validation({ permissions_sufficient: null }), true, NOW);
    expect(reading.ready).toBeNull();
    expect(reading.reason).toMatch(/incomplete/i);
  });

  it('does not treat unmeasured capacity as zero capacity', () => {
    // `observed_capacity: null` means capacity was not measured. "We did not
    // look" must not read as "there is none".
    const reading = providerReading(validation({ observed_capacity: null }), true, NOW);
    expect(reading.ready).toBe(true);
  });

  it('respects a connection that validates but does not admit work', () => {
    // A revoked or disabled connection can still have a passing historical
    // validation. AC-03 requires revoked credentials to be denied.
    const reading = providerReading(validation(), false, NOW);
    expect(reading.ready).toBe(false);
    expect(reading.reason).toMatch(/does not admit new work/i);
  });

  it('prefers the concrete failure over the not-admitting message', () => {
    // When both are true the specific cause is more actionable than the symptom.
    const reading = providerReading(validation({ credential_valid: false }), false, NOW);
    expect(reading.ready).toBe(false);
    expect(reading.reason).toMatch(/credential is not valid/i);
  });

  it('reports an unvalidated connection as unknown', () => {
    const reading = providerReading(null, true, NOW);
    expect(reading.ready).toBeNull();
    expect(reading.reason).toMatch(/not been validated/i);
  });
});

describe('partial failure produces actionable states', () => {
  it('degrades only the reading whose source failed', () => {
    // AC-04: "partial bootstrap failure ... produce actionable states". One failed
    // request must not blank the screen, because the readings that did arrive are
    // exactly what tells the user what to do next.
    const report = buildReadiness(
      {
        health: null,
        healthObservedAt: null,
        workspace: workspace(),
        validation: validation(),
        admitsNewWork: true,
      },
      NOW,
    );

    expect(report.controlPlane.ready).toBeNull();
    expect(report.workspace.ready).toBe(true);
    expect(report.provider.ready).toBe(true);
  });

  it('reports an unserved endpoint as unknown with the deployment reason', () => {
    // Not-deployed must never read as not-ready: the user cannot fix a route that
    // this environment does not serve, so the message has to say so.
    const report = buildReadiness(
      {
        health: { status: 'ok' },
        healthObservedAt: JUST_NOW,
        workspace: workspace(),
        validation: validation(),
        admitsNewWork: true,
        unavailable: {
          provider: {
            reason: 'not-deployed',
            detail: 'This environment does not serve provider capability reporting yet.',
            capability: 'reporting provider capabilities',
          },
        },
      },
      NOW,
    );

    expect(report.provider.ready).toBeNull();
    expect(report.provider.reason).toMatch(/does not serve/i);
    // The readings that were obtainable are still reported.
    expect(report.workspace.ready).toBe(true);
  });

  it('gives every non-ready reading a reason', () => {
    // A `false` or `null` with an empty reason is a dead end on screen.
    const report = buildReadiness(
      { health: null, healthObservedAt: null, workspace: null, validation: null, admitsNewWork: null },
      NOW,
    );
    for (const [key, reading] of Object.entries(report)) {
      expect(reading.reason.length, key).toBeGreaterThan(0);
    }
  });
});

/**
 * The clock that decides staleness has to keep running — the defect review
 * finding: "Readiness caches `Date.now()` until selection changes and never ages
 * on an open page."
 *
 * The tests below are written to fail if the clock is frozen again, in either of
 * the two ways it can be: no interval at all, or an interval that fires while a
 * suspended tab leaves it behind. Both mutations are exercised in the learnings
 * note; neither survives.
 *
 * Fake timers, and `shouldAdvanceTime` deliberately: RTL's `waitFor` only
 * auto-advances *Jest* fake timers, so a plain `vi.useFakeTimers()` makes every
 * later await poll a frozen clock and hang. Nothing here awaits RTL, but the flag
 * is kept for the same reason the rest of this repo keeps it — a later addition to
 * the block should not have to rediscover it.
 */
describe('the freshness clock', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('advances without any input, so a reading goes stale on an untouched page', () => {
    // The scenario the review named: nobody clicks anything, and the answer must
    // still change. A `Date.now()` captured in a memo cannot do this.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);

    const { result } = renderHook(() => useFreshnessClock());
    const observedAt = new Date(NOW - STALE_AFTER_MS + 1_000).toISOString();

    // Fresh to begin with: observed four minutes ago against a five-minute window.
    expect(freshnessOf(observedAt, result.current)).toBe('fresh');

    act(() => {
      vi.advanceTimersByTime(STALE_AFTER_MS);
    });

    // Same observation, same page, no interaction — and now honestly stale.
    expect(freshnessOf(observedAt, result.current)).toBe('stale');
  });

  it('keeps the clock within one tick of the true time', () => {
    // The accuracy bound the tick interval really buys. Asserted as a property
    // rather than as a hardcoded number of ticks, because the interval is a
    // staleness-accuracy decision and a future change to it should keep this true
    // or fail here.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);

    const { result } = renderHook(() => useFreshnessClock());

    act(() => {
      vi.advanceTimersByTime(FRESHNESS_TICK_MS * 7 + FRESHNESS_TICK_MS / 3);
    });

    expect(Date.now() - result.current).toBeLessThanOrEqual(FRESHNESS_TICK_MS);
    // And the interval really is fine enough to be useful against the window it
    // guards — a "tick" of an hour would satisfy the bound above and be useless.
    expect(FRESHNESS_TICK_MS).toBeLessThanOrEqual(STALE_AFTER_MS / 2);
  });

  it('re-reads the clock when a hidden tab becomes visible again', () => {
    // Browsers throttle timers in background tabs and suspend them on a sleeping
    // machine, so an interval alone can return to the foreground minutes behind.
    // Simulated the only way it can be in jsdom: the timer is never advanced, so
    // any clock update here must come from the visibility listener.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);

    const { result } = renderHook(() => useFreshnessClock());
    const before = result.current;

    // Time really passed — the tab was asleep and the interval did not fire.
    vi.setSystemTime(NOW + STALE_AFTER_MS * 4);
    expect(result.current).toBe(before);

    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });

    expect(result.current).toBe(NOW + STALE_AFTER_MS * 4);
  });

  it('stops ticking once unmounted', () => {
    // A leaked interval sets state on an unmounted component for as long as the
    // tab lives. The readiness panel mounts per organization switch, so leaking
    // one per switch is a real accumulation, not a theoretical one.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);

    const { unmount } = renderHook(() => useFreshnessClock());
    unmount();

    expect(vi.getTimerCount()).toBe(0);
  });
});

/**
 * Which workspace an observation is allowed to describe.
 *
 * Tested here rather than only through the screen because through the screen it
 * cannot fail: the connection panel remounts per selection and reports its own
 * `null`, so the misattribution window is already closed by a `key` prop in another
 * file. That makes the UI-level test pass with the check deleted — it was written,
 * the mutant survived, and this is the honest response to that. The rule is what
 * stops a future "keep the panel mounted" change reopening the window in silence.
 */
describe('a provider observation describes one workspace only', () => {
  const observation = {
    workspaceId: 'ws-1',
    validation: validation(),
    admitsNewWork: true,
  };

  it('is used for the workspace it was taken on', () => {
    expect(observationFor(observation, 'ws-1')).toBe(observation);
  });

  it('is withheld from a different workspace', () => {
    // The whole point: ws-2's credential state is unobserved, and "unobserved" is
    // the reading that must appear — not ws-1's passing one under ws-2's name.
    expect(observationFor(observation, 'ws-2')).toBeNull();
  });

  it('is withheld when nothing is selected', () => {
    expect(observationFor(observation, null)).toBeNull();
  });

  it('reports no observation as no observation', () => {
    expect(observationFor(null, 'ws-1')).toBeNull();
  });

  it('feeds a withheld observation into readiness as unknown, not as a pass', () => {
    // The consequence stated in the readiness vocabulary, which is what a caller
    // actually renders. A withheld reading must reach `providerReading` as `null`
    // and come back "not validated yet" — never as the previous workspace's pass.
    const withheld = observationFor(observation, 'ws-2');
    const report = buildReadiness(
      {
        health: null,
        healthObservedAt: null,
        workspace: workspace({ id: 'ws-2' }),
        validation: withheld?.validation ?? null,
        admitsNewWork: withheld?.admitsNewWork ?? null,
      },
      NOW,
    );
    expect(report.provider.ready).toBeNull();
    expect(report.provider.reason).toMatch(/not been validated/i);
  });
});


it('does not turn an old provider observation into current admission authority', () => {
  const observed = validation({ checked_at: new Date(NOW - 86_400_000).toISOString() });
  expect(providerReading(observed, true, NOW)).toMatchObject({ ready: null, freshness: 'stale' });
  expect(providerReading(validation(), null, NOW).ready).toBeNull();
});
