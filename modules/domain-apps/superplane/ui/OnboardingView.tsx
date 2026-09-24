/**
 * Workspace onboarding from an installed control plane — #5730 AC-01/AC-04/AC-05.
 *
 * WHAT THIS SCREEN HAS TO GET RIGHT
 * ---------------------------------
 * The state it is designed for is the one that looks most like a bug: the control
 * plane is installed and answering, and there are no workspaces at all. AC-01
 * requires an authorized admin to be able to begin onboarding from exactly there,
 * and everyone else to see only what they are permitted to do. So zero workspaces
 * renders as a starting point with a next action, not as an error and not as an
 * empty table.
 *
 * WHY READINESS IS SHOWN BEFORE ANY CREATE AFFORDANCE
 * --------------------------------------------------
 * Because the honest answer to "can I run work here?" is often "we cannot tell
 * yet", and a screen that leads with a create button implies the rest is fine.
 * The readiness panel states the three readings separately — see ReadinessPanel —
 * and this view never combines them into an overall verdict.
 *
 * WHY THE CREATE CONTROL CAN BE VISIBLE BUT DISABLED
 * -------------------------------------------------
 * Three different reasons stop a create, and they are not the same thing:
 *   - the user is not permitted        → the control is absent (AC-01)
 *   - the environment cannot serve it  → the control is present but disabled,
 *                                        with the deployment gap named
 *   - the server would silently ignore
 *     the operation identity           → disabled, because a create that appears
 *                                        to succeed while deduplicating nothing
 *                                        is worse than one that is refused
 * Hiding the control in the last two cases would leave an admin with no
 * explanation of why onboarding is impossible in an environment they administer.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { Alert, Button, Spinner } from '@/components/ui';
import { useAuth } from '@/hooks/useAuth';
import { AdminRole } from '@/types';

import { ApprovalLookup } from './ApprovalPanel';
import { CreateWorkspaceFlow } from './CreateWorkspaceFlow';
import { LifecycleProposalPanel } from './LifecycleProposalPanel';
import { ProviderConnectionPanel } from './ProviderConnectionPanel';
import { ReadinessPanel } from './ReadinessPanel';
import { ServingPanel } from './ServingPanel';
import { BatchPanel } from './BatchPanel';
import {
  ScopeGuard,
  getCapabilities,
  isSuperseded,
  listWorkspaces,
  type Outcome,
} from './client';
import {
  CREATE_IDEMPOTENCY_FEATURE,
  advertises,
  ENDPOINTS,
  type Capabilities,
  type Unavailable,
  type WorkspaceSummary,
} from './contract';
import { browserReceiptStore, pruneOtherScopes, type ReceiptScope } from './operations';
import {
  buildReadiness,
  observationFor,
  useFreshnessClock,
  type ProviderObservation,
} from './readiness';

/**
 * Whether this user may begin onboarding.
 *
 * Gated on administrative role rather than a new permission: the `Permission`
 * enum mirrors the backend's exactly and is held to that by a parity test, so
 * inventing a `superplane:*` member here would either break that test or claim an
 * authority the backend does not grant.
 *
 * This is a UI affordance only. The real boundary is the Superplane API's own
 * authorization, which this cannot and must not replace — the comment in
 * Navigation.tsx makes the same point about role claims being cosmetic. A member
 * who forges a role claim still gets 403 from the server, which the client
 * reports as "ask an administrator".
 */
/**
 * The block that holds until capability discovery answers.
 *
 * Worded as an in-progress check rather than a deployment gap, because that is
 * what it is for the fraction of a second it normally lasts — and if it lasts
 * longer, "still checking" is the truthful thing to show.
 */
const CAPABILITY_UNOBSERVED: Unavailable = {
  reason: 'unknown',
  detail:
    'This environment has not yet reported whether it can accept a workspace creation ' +
    'safely. Creation stays disabled until it does.',
};

function canBeginOnboarding(role: AdminRole | undefined): boolean {
  return role === AdminRole.PLATFORM_ADMIN || role === AdminRole.ORG_ADMIN;
}

type LoadState =
  | { phase: 'loading' }
  | { phase: 'loaded'; orgId: string; workspaces: WorkspaceSummary[] }
  | { phase: 'failed'; unavailable: Unavailable };

export function OnboardingView() {
  const { user } = useAuth();
  const orgId = user?.orgId ?? '';
  const mayOnboard = canBeginOnboarding(user?.role);

  const [state, setState] = useState<LoadState>({ phase: 'loading' });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  /**
   * Why create is unavailable in this environment, if it is.
   *
   * Starts blocked, on purpose. `null` would mean "nothing is wrong", and until
   * capability discovery answers, nothing is *known* — which is not the same thing.
   * There is a real window between the workspace list rendering and the capability
   * reply landing, and a button enabled during it would let a user submit under a
   * guarantee nobody has checked. Unobserved therefore reads as blocked, and only
   * an affirmative advertisement clears it.
   *
   * What actually closes that window is the reset in the org effect below, which
   * runs before any button exists; mutating *this* initialiser to `null` changes no
   * observable behaviour, because the first render shows only the loading state.
   * It is kept as an initialiser anyway so the invariant is visible where the state
   * is declared rather than resting on effect ordering — but the effect is the load-
   * bearing line, and it is the one with a test behind it.
   */
  const [createBlocked, setCreateBlocked] = useState<Unavailable | null>(CAPABILITY_UNOBSERVED);
  const [creating, setCreating] = useState(false);
  /**
   * The capability report, when it was readable.
   *
   * Held separately from `createBlocked` because the two answer different
   * questions: the block governs whether a create may be submitted, while the
   * report also names which provider families this environment accepts, which the
   * connection panel needs. `null` means not-read, and the panel says so rather
   * than offering an empty provider list as though none were supported.
   */
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [capabilityProblem, setCapabilityProblem] = useState<Unavailable | null>(null);
  /**
   * The last provider observation the connection panel reported.
   *
   * Held here rather than recomputed, because the readings belong to a request the
   * panel makes — this view has no route of its own for them (there is no
   * "list connections for a workspace"). Carrying the panel's observation up is
   * therefore the only way the readiness rows can describe the same credential the
   * user is looking at, and it is what stops the provider row saying "never
   * validated" underneath a panel showing four fresh readings.
   *
   * It is checked against the selected workspace at the point of use, not cleared
   * on selection change: see the `readiness` memo.
   */
  const [providerObservation, setProviderObservation] = useState<ProviderObservation | null>(
    null,
  );

  /**
   * The latest block reason, readable without a re-render.
   *
   * `CreateWorkspaceFlow.verifyBeforeSubmit` is called at the moment of submit,
   * inside a callback that closed over whatever `createBlocked` was when the
   * callback was created. Reading the state variable there would answer with a
   * value that is potentially minutes stale — which is the entire failure the
   * last-moment re-check exists to prevent, so a ref is what makes the re-check
   * mean anything.
   */
  const blockedRef = useRef<Unavailable | null>(null);
  blockedRef.current = createBlocked;

  /**
   * The receipt scope.
   *
   * `deploymentId` is fixed for a given ADP install, so the organization is what
   * varies here. Both are part of the key because the same org id in a different
   * deployment is a different operation namespace.
   */
  const scope: ReceiptScope = useMemo(
    () => ({ deploymentId: window.location.origin, orgId }),
    [orgId],
  );

  /**
   * Receipts persist in `localStorage`, deliberately not `sessionStorage`.
   *
   * A receipt exists to survive exactly the events that produce duplicate
   * workspaces, and a closed tab is one of them: a user whose submission timed out
   * closes the tab, comes back, and must find the unresolved operation rather than
   * a blank form inviting a second submission. `sessionStorage` would discard it at
   * the moment it becomes most valuable. It holds no secrets -- only identifiers,
   * a fingerprint and timestamps, asserted by `receiptIsSecretFree`.
   */
  const store = useMemo(() => browserReceiptStore(window.localStorage), []);

  /**
   * One guard per organization scope.
   *
   * Recreated when `orgId` changes so that replies belonging to the previous
   * organization are discarded on arrival rather than rendered under the new
   * organization's name (AC-03). A ref rather than state because superseding must
   * happen during the effect's cleanup, before any re-render.
   */
  const guardRef = useRef<ScopeGuard>(new ScopeGuard());

  const load = useCallback(async (guard: ScopeGuard) => {
    setState({ phase: 'loading' });

    const listing: Outcome<{ workspaces: WorkspaceSummary[] }> = await listWorkspaces(guard);

    // A superseded reply is neither success nor failure: the user has moved on,
    // and writing either into state would flash the wrong tenant's data or a
    // spurious error on the new screen.
    if (isSuperseded(listing)) return;

    if (!listing.ok && 'unavailable' in listing) {
      setState({ phase: 'failed', unavailable: listing.unavailable });
      return;
    }
    if (!listing.ok) return;

    setState({ phase: 'loaded', orgId, workspaces: listing.value.workspaces });

    // Capability discovery is a separate, independently-failing request. It tells
    // us whether a create can be submitted safely; its absence disables create
    // but must not blank the workspace list that already loaded successfully
    // (AC-04 partial failure).
    const report: Outcome<Capabilities> = await getCapabilities(guard);
    if (isSuperseded(report)) return;
    if (!report.ok && 'unavailable' in report) {
      setCreateBlocked(report.unavailable);
      // Recorded separately so the connection panel can say the supported
      // provider list is unknown, rather than rendering an empty one as though
      // the environment supported nothing.
      setCapabilityProblem(report.unavailable);
      setCapabilities(null);
      return;
    }
    if (!report.ok) return;

    setCapabilities(report.value);
    setCapabilityProblem(null);

    // Served and readable, but that is not yet permission to submit. The server
    // must say it honours a client-supplied operation identity; a deployment that
    // answers this route while ignoring the identity field would return 201 and
    // deduplicate nothing, so silence here is a block, not a default-on.
    if (!advertises(report.value, CREATE_IDEMPOTENCY_FEATURE)) {
      setCreateBlocked({
        reason: 'not-deployed',
        detail:
          `This environment's Superplane API cannot confirm that it honours a ` +
          `submitted operation identity, so a retried creation could build a ` +
          `second workspace instead of returning the first one.`,
        endpoint: 'createWorkspace',
        capability: 'safely retrying a workspace creation without duplicating it',
      });
      return;
    }
    setCreateBlocked(null);
  }, [orgId]);

  useEffect(() => {
    const guard = new ScopeGuard();
    guardRef.current = guard;
    // Back to unobserved, not to unblocked: the previous organization's capability
    // answer says nothing about this one, and carrying it over would enable create
    // in a tenant whose deployment has never been checked.
    setCreateBlocked(CAPABILITY_UNOBSERVED);
    // Same reasoning as the block above: the previous organization's capability
    // answer says nothing about this one, so the provider list must not carry over.
    setCapabilities(null);
    setCapabilityProblem(null);
    setSelectedId(null);
    setCreating(false);
    // A credential reading belongs to one organization's workspace. Carrying it
    // across a switch would attribute one tenant's provider state to another's
    // screen, which is the same class of error as carrying the workspace list over.
    setProviderObservation(null);
    // Housekeeping, and only housekeeping — two conditions on it, both load-bearing.
    //
    // Gated on a RESOLVED organization, because `orgId` is `''` for the render or
    // two before `useAuth` answers, and `''` is not an organization. Pruning
    // against it treats every genuine scope as "some other scope", so a reload
    // would tidy away the records belonging to the very organization about to be
    // selected — a data loss triggered by nothing more than opening the page.
    //
    // And it prunes rather than clears: `pruneOtherScopes` keeps unresolved
    // records. Isolation between tenants is enforced by the scoped key and the
    // scope check inside `readReceipt`, not by deletion, so an `unknown` receipt
    // from an organization the user stepped away from stays unreadable here while
    // remaining available when they return to it. Deleting it would buy no
    // isolation and would destroy the only record that a lost reply leaves behind.
    if (orgId !== '') {
      pruneOtherScopes(store, { deploymentId: window.location.origin, orgId });
    }
    void load(guard);
    return () => {
      // Leaving this scope — abort what is outstanding and invalidate anything
      // already in flight so it cannot land after the switch.
      guard.supersede();
    };
    // `orgId` in the dependency list is the whole point: an organization switch
    // rebuilds the guard and reloads, and the old guard's replies are discarded.
  }, [orgId, load, store]);

  const workspaces = state.phase === 'loaded' && state.orgId === orgId ? state.workspaces : [];
  const selected = useMemo(
    () => workspaces.find((workspace) => workspace.id === selectedId) ?? null,
    [workspaces, selectedId],
  );

  /**
   * The readiness readings.
   *
   * `health: null` deliberately: the control plane's `/health` route is not in
   * the gateway proxy allowlist, so the browser genuinely cannot observe it. The
   * reading therefore reports "not reached yet" rather than inferring health from
   * the fact that the workspace list happened to answer. Inferring it would be
   * the precise error AC-04 forbids, arrived at sideways — and it is worth noting
   * that a successful list *does* prove the API is up, which is exactly why the
   * temptation exists. It still would not license any claim about a workspace.
   *
   * The provider readings are whatever the connection panel actually observed, or
   * `null` until it observes something. They are NOT invented here and no synthetic
   * `unavailable` entry is supplied for them: inventing one would substitute this
   * component's wording for the module that owns the vocabulary, and would claim a
   * deployment gap where the truth is simply that nothing has been observed.
   *
   * The observation is accepted only when it names the selected workspace. The
   * connection panel remounts on selection change, but this state does not reset
   * with it, so an unchecked read would show workspace A's credential reading under
   * workspace B's name for as long as it took B's panel to report. Matching the id
   * makes the gap read as "unobserved", which is what it is.
   *
   * WHY `now` COMES FROM A RUNNING CLOCK
   * -----------------------------------
   * This memo used to pass `Date.now()` and depend on `[selected]`, which meant the
   * clock only advanced when the user clicked a different workspace. A dashboard
   * left open compared every heartbeat against the time the page happened to
   * settle, so a reading went stale on the server and stayed green here
   * indefinitely. `useFreshnessClock` ticks, the memo depends on the tick, and
   * staleness therefore appears without anyone touching the page.
   */
  const now = useFreshnessClock();
  const readiness = useMemo(() => {
    const observed = observationFor(providerObservation, selected?.id ?? null);
    return buildReadiness(
      {
        health: null,
        healthObservedAt: null,
        workspace: selected,
        validation: observed?.validation ?? null,
        admitsNewWork: observed?.admitsNewWork ?? null,
      },
      now,
    );
  }, [selected, providerObservation, now]);

  if (state.phase === 'loading' || state.phase === 'loaded' && state.orgId !== orgId) {
    return (
      <div className="p-6">
        <Header />
        <div className="mt-6 flex items-center gap-3">
          <Spinner />
          <p className="text-sm text-gray-600 dark:text-gray-400">Loading workspaces…</p>
        </div>
      </div>
    );
  }

  if (state.phase === 'failed') {
    return (
      <div className="p-6">
        <Header />
        <div className="mt-6">
          <Alert variant="error" title={titleFor(state.unavailable)}>
            {state.unavailable.detail}
          </Alert>
          {/* Retry only for a transient cause. Offering it for a permission or
              deployment problem invites the user to click at something that
              cannot change until somebody else acts. */}
          {state.unavailable.reason === 'unreachable' && (
            <Button
              className="mt-4"
              variant="secondary"
              onClick={() => void load(guardRef.current)}
            >
              Try again
            </Button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="p-6">
      <Header />

      <ApprovalLookup key={orgId} />

      {creating && mayOnboard ? (
        <div className="mt-6">
          <CreateWorkspaceFlow
            guard={guardRef.current}
            scope={scope}
            store={store}
            idempotencySupport={createBlocked}
            capabilities={capabilities}
            verifyBeforeSubmit={() => blockedRef.current}
            onCreated={() => {
              void load(guardRef.current);
            }}
            onCancel={() => setCreating(false)}
          />
        </div>
      ) : workspaces.length === 0 ? (
        <ZeroWorkspaces
          mayOnboard={mayOnboard}
          createBlocked={createBlocked}
          onBegin={() => setCreating(true)}
        />
      ) : (
        <div className="mt-6 space-y-6">
          <WorkspaceList
            workspaces={workspaces}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
          <ReadinessPanel report={readiness} workspaceName={selected?.display_name} />
          {selected && <ServingPanel workspaceId={selected.id} scope={scope} store={store} />}
          {selected && <BatchPanel workspaceId={selected.id} scope={scope} store={store} />}
          {ENDPOINTS.listLifecycleProposals.served && selected && <LifecycleProposalPanel
            key={`${scope.orgId}:${selected.id}`}
            workspaceId={selected.id} scope={scope} store={store} mayManage={mayOnboard}
            onProgress={() => { void load(guardRef.current); }}
          />}
          {/* Only for a selected workspace: a connection is bound to one
              workspace, and there is no meaningful "bind to whichever" action. */}
          {selected && (
            <ProviderConnectionPanel
              // Remounts on selection change, which resets the panel's in-flight
              // state and its bound-connection view. Without this the connection
              // just bound to workspace A would still be on screen after
              // selecting workspace B, attributed to the wrong workspace.
              key={selected.id}
              workspaceId={selected.id}
              providers={capabilities?.providers ?? []}
              mayManage={mayOnboard}
              capabilityUnavailable={capabilityProblem}
              // The provider row of the readiness panel is derived from this, so
              // the two statements about one credential come from one observation.
              onObservation={setProviderObservation}
            />
          )}
        </div>
      )}
    </div>
  );
}

function Header() {
  return (
    <div>
      <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Superplane</h1>
      <p className="mt-1 text-gray-600 dark:text-gray-400">
        Workspaces and provider connections for running AI workloads.
      </p>
    </div>
  );
}

function titleFor(unavailable: Unavailable): string {
  switch (unavailable.reason) {
    case 'not-permitted':
      return 'You do not have access to this';
    case 'not-deployed':
      return 'Not available in this environment';
    case 'unreachable':
      return 'Could not reach Superplane';
    default:
      return 'Could not load workspaces';
  }
}

/**
 * The zero-workspace state.
 *
 * This is a correct, expected state for a freshly installed control plane, so it
 * reads as a starting point rather than a fault. What differs by role is only the
 * action offered: AC-01 requires other roles to "see only permitted actions", so a
 * non-admin is told who can do this instead of being shown a button that would 403.
 */
function ZeroWorkspaces({
  mayOnboard,
  createBlocked,
  onBegin,
}: {
  mayOnboard: boolean;
  createBlocked: Unavailable | null;
  onBegin: () => void;
}) {
  return (
    <section
      aria-labelledby="superplane-empty-heading"
      className="mt-6 rounded-lg border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800"
    >
      <h2
        id="superplane-empty-heading"
        className="text-lg font-semibold text-gray-900 dark:text-white"
      >
        No workspaces yet
      </h2>
      <p className="mt-2 max-w-prose text-sm text-gray-600 dark:text-gray-300">
        Superplane is installed in this environment. A workspace is where workloads run — it
        holds the isolation mode, the budget and the provider connection that work is charged
        to.
      </p>

      {mayOnboard ? (
        <div className="mt-4">
          <Button
            disabled={createBlocked !== null}
            aria-describedby={createBlocked ? 'superplane-create-blocked' : undefined}
            onClick={onBegin}
          >
            Create a workspace
          </Button>
          {createBlocked && (
            <div className="mt-3" id="superplane-create-blocked">
              {/* Present-but-disabled with the reason named. An admin who cannot
                  onboard deserves to know whether it is a deployment gap or their
                  own authority — stated as a service condition they can act on,
                  not as our backlog position. */}
              <Alert variant="warning" title="Workspace creation is not available yet">
                {createBlocked.detail}
                {' '}
                Creation stays disabled until this environment can confirm it honours a
                submitted operation identity ({CREATE_IDEMPOTENCY_FEATURE}); without that
                confirmation a retried request could build a second workspace and bill twice.
              </Alert>
            </div>
          )}
        </div>
      ) : (
        <p className="mt-4 text-sm text-gray-600 dark:text-gray-300">
          Creating a workspace requires an organization or platform administrator. Ask an
          administrator to set one up.
        </p>
      )}
    </section>
  );
}

/**
 * The workspace list.
 *
 * Deliberately does not render a readiness badge per row. A per-row badge would
 * have to summarise, and summarising is what AC-04 forbids — so selecting a row
 * reveals the three separate readings in the panel instead. `status` is shown
 * verbatim because it is a provisioning fact, not a readiness verdict.
 */
function WorkspaceList({
  workspaces,
  selectedId,
  onSelect,
}: {
  workspaces: WorkspaceSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <section aria-labelledby="superplane-workspaces-heading">
      <h2
        id="superplane-workspaces-heading"
        className="text-base font-semibold text-gray-900 dark:text-white"
      >
        Workspaces
      </h2>
      <ul className="mt-3 space-y-2">
        {workspaces.map((workspace) => {
          const isSelected = workspace.id === selectedId;
          return (
            <li key={workspace.id}>
              {/* A real button: keyboard reachable and operable with Enter and
                  Space for free, which a clickable div is not (AC-05). */}
              <button
                type="button"
                onClick={() => onSelect(workspace.id)}
                aria-pressed={isSelected}
                className={`w-full rounded-lg border p-3 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 ${
                  isSelected
                    ? 'border-primary-500 bg-primary-50 dark:border-primary-400 dark:bg-primary-900/20'
                    : 'border-gray-200 bg-white hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:hover:bg-gray-700'
                }`}
              >
                <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
                  <span className="font-medium break-words text-gray-900 dark:text-white">
                    {workspace.display_name}
                  </span>
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    {workspace.isolation_mode || 'unspecified'} · {workspace.status}
                  </span>
                </div>
              </button>
            </li>
          );
        })}
      </ul>
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        Select a workspace to see its readiness readings.
      </p>
    </section>
  );
}

export default OnboardingView;
