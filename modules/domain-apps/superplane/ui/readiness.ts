/**
 * Three kinds of readiness, deliberately never combined — issue #5730.
 *
 * THE DEFECT THIS MODULE EXISTS TO PREVENT
 * ----------------------------------------
 * AC-04 ends with a sentence that is really a prohibition: "Control-plane health
 * alone never marks a workspace execution-ready." It is worth being precise about
 * why, because the mistake is easy and the consequence is expensive.
 *
 * `GET /health` on the Superplane API answers whether *the API process* is up and
 * how its authentication is configured. It says nothing about whether any
 * particular workspace has a reconciling controller, a valid provider credential,
 * or any capacity. A screen that reads one green health response and renders a
 * green badge beside a workspace is telling the user they can launch work. They
 * then launch work, it fails, and the platform has spent their trust on a claim
 * it never actually checked.
 *
 * So the three concerns are three values here, each carrying its own reason and
 * its own observation time, and there is no function in this module that reduces
 * them to a single boolean. The absence of that function is the feature.
 *
 * WHY EACH READING CARRIES ITS OWN OBSERVATION TIME
 * -------------------------------------------------
 * "This workspace was healthy when we last heard from it, twenty minutes ago" and
 * "this workspace is healthy" are different facts, and only one of them justifies
 * starting a job. The domain API degrades a cluster to `Degraded` after five
 * minutes without a heartbeat, but the UI must be able to say *stale* rather than
 * silently presenting an old reading as current, so freshness is derived here from
 * the observation timestamp rather than trusted from the status string.
 *
 * AND WHY THE CLOCK THAT DECIDES FRESHNESS HAS TO KEEP RUNNING
 * -----------------------------------------------------------
 * Deriving freshness from a timestamp only works if `now` is really now. The first
 * version of this screen computed the readings in a memo keyed on the selected
 * workspace and passed a `Date.now()` captured inside it, which meant the clock
 * advanced only when the selection changed. On a dashboard left open — the normal
 * way an operator uses one — a reading taken at 09:00 still read "fresh" at 11:00,
 * because the comparison was still being made against 09:00.
 *
 * That is worse than having no freshness logic at all. A screen with no freshness
 * logic makes no claim about currency; this one made the claim and got it wrong,
 * and it got it wrong in the direction that says "go ahead and run work". So
 * {@link useFreshnessClock} exists, and the reading a caller derives from it ages
 * on its own.
 */

import { useEffect, useState } from 'react';

import type { Unavailable, ValidationReading, WorkspaceSummary } from './contract';

/**
 * How current an observation is.
 *
 * `unknown` is for an observation that never happened — distinct from `stale`,
 * which is an observation that happened too long ago. A workspace that has never
 * reported is not the same as one that stopped reporting.
 */
export type Freshness = 'fresh' | 'stale' | 'unknown';

/**
 * Matches the domain API's own heartbeat staleness threshold
 * (`HEARTBEAT_STALE_THRESHOLD`, five minutes). Kept equal deliberately: if the UI
 * used a longer window it would show "fresh" beside a server-side `Degraded`.
 */
export const STALE_AFTER_MS = 5 * 60 * 1000;

export function freshnessOf(
  observedAt: string | null | undefined,
  now: number,
): Freshness {
  if (!observedAt) return 'unknown';
  const observed = Date.parse(observedAt);
  if (Number.isNaN(observed) || observed > now + 30_000) return 'unknown';
  return now - observed > STALE_AFTER_MS ? 'stale' : 'fresh';
}

/**
 * How often the freshness clock ticks.
 *
 * A twelfth of the staleness window, so the worst case is that a reading reads
 * "fresh" for twenty-five seconds after it stopped being fresh. That bound is the
 * property worth stating: the tick interval is not a rendering preference, it is
 * the accuracy of every staleness claim this module makes.
 *
 * Not shorter, because each tick re-renders the readiness panel and the value only
 * changes at one boundary; not much longer, because the gap between the server
 * degrading a cluster and the UI admitting it is exactly this number.
 */
export const FRESHNESS_TICK_MS = STALE_AFTER_MS / 12;

/**
 * A wall-clock reading that advances while the page stays open.
 *
 * WHY A HOOK RATHER THAN A `Date.now()` AT THE CALL SITE
 * -----------------------------------------------------
 * Because `Date.now()` at the call site is what the defect was. A component reads
 * the clock during render, React does not re-render when time passes, and the
 * captured value silently becomes the past. Freshness then freezes at whatever it
 * was when something *else* happened to change — a selection, a refetch — which is
 * precisely uncorrelated with the thing being measured.
 *
 * Making time a state value makes its passage a render trigger, which is the only
 * way a derived "stale" can appear on a screen nobody is touching.
 *
 * WHY THE VISIBILITY LISTENER IS NOT OPTIONAL
 * -------------------------------------------
 * Browsers throttle timers in background tabs, heavily, and suspend them outright
 * on a sleeping machine. A tab restored after an hour would therefore paint one
 * frame with a clock from an hour ago and show "fresh" over a dead cluster — the
 * same defect, arrived at through the timer instead of the memo. Reading the clock
 * again on `visibilitychange` closes that, and it is the case an interval alone
 * cannot cover no matter how short the interval is.
 */
export function useFreshnessClock(tickMs: number = FRESHNESS_TICK_MS): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const read = () => setNow(Date.now());
    const timer = setInterval(read, tickMs);
    // Also on becoming visible: a throttled or suspended timer leaves the clock
    // behind, and the moment the user looks at the tab is the moment the reading
    // must be honest.
    const onVisible = () => {
      if (document.visibilityState === 'visible') read();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [tickMs]);

  return now;
}

/**
 * One readiness reading.
 *
 * `ready` is tri-state on purpose. `null` means "we do not know" — not measured,
 * not reachable, or not served by this environment — and a `null` must never be
 * rendered with the same affordance as `false`, because "not ready" invites a
 * fix and "unknown" invites a check.
 */
export interface Reading {
  ready: boolean | null;
  /** Human-readable why, always present when `ready` is not `true`. */
  reason: string;
  freshness: Freshness;
  observedAt: string | null;
}

function unknownReading(reason: string): Reading {
  return { ready: null, reason, freshness: 'unknown', observedAt: null };
}

/**
 * The three readings, side by side and never merged.
 *
 * There is intentionally no `overallReady` field and no helper that computes one.
 * A caller that wants to enable a "run a workload" affordance must look at
 * `workspace` specifically, which is the only reading that speaks to execution.
 */
export interface ReadinessReport {
  /** The API process and its administration surface. Never implies execution. */
  controlPlane: Reading;
  /** A specific workspace's ability to accept work. */
  workspace: Reading;
  /** The provider credential a workspace needs. */
  provider: Reading;
}

/**
 * Control-plane readiness from the public health response.
 *
 * Note what this deliberately does NOT consult: the four adapter-capability
 * booleans that say whether the credential-evidence, provider-authority,
 * inventory and operation-facade ports are really composed. Those are the
 * readings that would justify claiming production capability, and they are
 * currently only reachable on an internal route a browser or `adp` session
 * cannot call. Until #5535 exposes them, this reading is scoped to what it can
 * actually see and says so, rather than reporting full capability from a
 * liveness check.
 */
export function controlPlaneReading(
  health: { status?: string } | null,
  observedAt: string,
  now: number,
): Reading {
  if (!health) {
    return unknownReading('The control plane has not been reached yet.');
  }
  const ok = health.status === 'ok' || health.status === 'healthy';
  return {
    ready: ok,
    reason: ok
      ? 'The control plane is reachable and serving administration requests. ' +
        'This does not establish that any workspace can run work.'
      : `The control plane reported status "${health.status ?? 'unreported'}".`,
    freshness: freshnessOf(observedAt, now),
    observedAt,
  };
}

/**
 * Workspace readiness from the workspace row.
 *
 * Requires BOTH a terminal-successful provisioning status AND a healthy cluster
 * observation. Either alone is insufficient, and the two failure modes read
 * differently to the user: a workspace still `Provisioning` is a wait, whereas a
 * registered workspace whose cluster is silent is a fault.
 *
 * A stale observation yields `null`, not `true`. A cluster that was healthy ten
 * minutes ago and has said nothing since is not evidence that it is healthy now,
 * and treating it as such is how a "ready" badge outlives the thing it describes.
 */
export function workspaceReading(
  workspace: WorkspaceSummary | null,
  now: number,
): Reading {
  if (!workspace) {
    return unknownReading('No workspace selected.');
  }
  const status = workspace.status;
  if (status !== 'Active' && status !== 'Ready') {
    return {
      ready: false,
      reason: `The workspace is "${status}". It cannot run work in this state.`,
      freshness: freshnessOf(workspace.last_heartbeat, now),
      observedAt: workspace.last_heartbeat ?? null,
    };
  }

  const freshness = freshnessOf(workspace.last_heartbeat, now);
  if (freshness !== 'fresh') {
    return {
      ready: null,
      reason:
        freshness === 'stale'
          ? 'The workspace is registered, but its cluster has not reported ' +
            'recently, so its current ability to run work is unknown.'
          : 'The workspace is registered, but its cluster has never reported, ' +
            'so its ability to run work has not been observed.',
      freshness,
      observedAt: workspace.last_heartbeat ?? null,
    };
  }

  const health = workspace.cluster_health;
  if (!health) return {
    ready: null,
    reason: 'The workspace has reported recently, but cluster health has not been reported.',
    freshness,
    observedAt: workspace.last_heartbeat ?? null,
  };
  const healthy = health === 'Healthy';
  return {
    ready: healthy,
    reason: healthy
      ? 'The workspace is registered and its cluster reported healthy.'
      : `The workspace is registered but its cluster reported "${health ?? 'nothing'}".`,
    freshness,
    observedAt: workspace.last_heartbeat ?? null,
  };
}

/**
 * Provider readiness from a connection's four separate validation readings.
 *
 * All three booleans must hold. They are reported individually in `reason`
 * because the operator action differs per reading: an invalid credential is
 * re-entered, insufficient permissions are widened at the provider, and exhausted
 * quota is raised or waited out. A single "provider not ready" would send the
 * user looking in the wrong place.
 *
 * `observed_capacity` is excluded from the verdict on purpose — the contract
 * leaves it `null` when capacity was not measured, and "we did not look" must not
 * read as "there is none".
 */
export function providerReading(
  validation: ValidationReading | null,
  admitsNewWork: boolean | null,
  now: number,
): Reading {
  if (!validation) {
    return unknownReading(
      'The provider connection has not been validated by the service yet.',
    );
  }

  const failures: string[] = [];
  if (validation.credential_valid === false) failures.push('the credential is not valid');
  if (validation.permissions_sufficient === false)
    failures.push('its permissions are insufficient');
  if (validation.quota_available === false) failures.push('no quota is available');

  const unmeasured =
    validation.credential_valid === null ||
    validation.permissions_sufficient === null ||
    validation.quota_available === null;

  const freshness = freshnessOf(validation.checked_at, now);
  if (freshness !== 'fresh') return {
    ready: null, reason: 'The provider observation is stale or unavailable. Request a fresh validation.',
    freshness, observedAt: validation.checked_at || null,
  };

  if (failures.length > 0) {
    return {
      ready: false,
      reason: `The provider connection cannot admit work: ${failures.join(', ')}.`,
      freshness,
      observedAt: validation.checked_at,
    };
  }
  if (unmeasured) {
    return {
      ready: null,
      reason:
        'The provider validation is incomplete — at least one reading was not ' +
        'taken, so readiness is unknown.',
      freshness,
      observedAt: validation.checked_at,
    };
  }
  if (admitsNewWork === false) {
    return {
      ready: false,
      reason:
        'The credential validated, but the connection does not admit new work ' +
        '(it may be disabled or revoked).',
      freshness,
      observedAt: validation.checked_at,
    };
  }
  if (admitsNewWork !== true) return { ready: null, reason: 'The provider has not reported admission authority.', freshness, observedAt: validation.checked_at };
  return {
    ready: true,
    reason: 'The service validated the credential, its permissions and its quota.',
    freshness,
    observedAt: validation.checked_at,
  };
}

/**
 * What was actually observed about one workspace's provider connection.
 *
 * WHY THIS TYPE EXISTS AT ALL
 * --------------------------
 * Because the readings were being passed as literal `null`s. The provider row of
 * the readiness panel read "not validated by the service yet" permanently — even
 * on a screen where the connection panel, a few hundred pixels below it, was
 * displaying a complete four-way reading it had just read off the server. The
 * panel had the observation and the readiness report did not, so the same screen
 * stated two different things about one credential and the more pessimistic one
 * was the one shaped like a readiness verdict.
 *
 * `workspaceId` is part of the observation rather than implied by where it is
 * stored, and that is load-bearing: the connection panel is remounted on selection
 * change (its `key`), but the *parent's* copy of the last observation is not
 * discarded by a remount. Without the id to check against, selecting workspace B
 * would show B's readiness with A's provider reading — which is the cross-tenant
 * attribution error of AC-03 in miniature, inside one organization.
 *
 * `admitsNewWork` is separate from `validation` because the server computes it
 * separately: a connection can hold three passing readings and still admit no work
 * because it was revoked. `null` on either field means unobserved, never "no".
 */
export interface ProviderObservation {
  workspaceId: string;
  validation: ValidationReading | null;
  admitsNewWork: boolean | null;
}

/**
 * The observation that may be used for `workspaceId`, or `null`.
 *
 * A one-line function, exported and tested on its own for a reason worth stating.
 * On the current screen the connection panel is remounted on selection change and
 * reports `null` readings from its own mount effect, so today the misattribution
 * window closes before anything renders — which means a test driven through the UI
 * passes with this check deleted. It was written, and it survived the mutation.
 *
 * Deleting it on that evidence would be wrong. The reason the window is closed is a
 * property of the child's remount, i.e. of a `key` prop several files away, and
 * nothing states that the parent's cached observation depends on it. Any future
 * change that keeps the panel mounted across selections — an obvious optimization —
 * reopens it silently, and the symptom is one workspace's credential reading shown
 * under another's name.
 *
 * So the rule is a named function with its own test rather than an inline condition
 * whose only coverage is incidental. A guard whose test cannot fail is not tested;
 * this one's can.
 */
export function observationFor(
  observation: ProviderObservation | null,
  workspaceId: string | null,
): ProviderObservation | null {
  if (!observation || !workspaceId) return null;
  return observation.workspaceId === workspaceId ? observation : null;
}

/**
 * Build the report, tolerating partial failure.
 *
 * Partial failure is the normal case during onboarding, not an exception: the
 * control plane answers while a brand-new workspace has never reported and no
 * provider connection exists at all. Each reading is derived from whatever was
 * actually obtained, so one failed request degrades one reading instead of
 * blanking the screen. That is what AC-04's "partial bootstrap failure ...
 * produce actionable states" asks for.
 */
export function buildReadiness(
  sources: {
    health: { status?: string } | null;
    healthObservedAt: string | null;
    workspace: WorkspaceSummary | null;
    validation: ValidationReading | null;
    admitsNewWork: boolean | null;
    /** Set when an endpoint needed for a reading is not served here. */
    unavailable?: Partial<Record<keyof ReadinessReport, Unavailable>>;
  },
  now: number,
): ReadinessReport {
  const blocked = (key: keyof ReadinessReport): Reading | null => {
    const entry = sources.unavailable?.[key];
    return entry ? unknownReading(entry.detail) : null;
  };

  return {
    controlPlane:
      blocked('controlPlane') ??
      controlPlaneReading(sources.health, sources.healthObservedAt ?? '', now),
    workspace: blocked('workspace') ?? workspaceReading(sources.workspace, now),
    provider:
      blocked('provider') ??
      providerReading(sources.validation, sources.admitsNewWork, now),
  };
}
