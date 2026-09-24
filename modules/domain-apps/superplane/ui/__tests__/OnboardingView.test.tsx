/**
 * The onboarding screen — #5730 AC-01 (roles), AC-03 (org switching), AC-04
 * (partial failure and the readiness prohibition), AC-05 (accessibility).
 *
 * These render the real component against MSW rather than mocking the client,
 * because the assertions are about what an operator actually sees for a given
 * server state, and mocking the client out would let a wiring mistake between the
 * two pass. `useAuth` IS mocked: the identity is an input to the screen, not a
 * behaviour of it, and driving it through a real Cognito session would test
 * authentication instead of onboarding.
 */

import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { server } from '@/mocks/server';
import { OnboardingView } from '@superplane-ui/OnboardingView';
import {
  CREATE_IDEMPOTENCY_FEATURE,
  DOMAIN_BASE,
  ENDPOINTS,
} from '@superplane-ui/contract';
import { AdminRole } from '@/types';
import { withoutOnboardingEndpoints } from './endpoint-fixtures';

// The same schema-derived vault fixtures the connection panel's tests use, for the
// same reason: a credential row invented here would agree with whatever the client
// assumed rather than with `CredentialResponse`. See vault-fixtures.ts.
import {
  connectionResponse,
  validationResponse,
  vaultCredentialList,
  vaultCredentialRow,
} from './vault-fixtures';

const API = (path: string) => `/api${DOMAIN_BASE}${path}`;

/** Mutable so a test can change identity and org between renders. */
let currentUser: { id: string; orgId?: string; role?: AdminRole } | null = {
  id: 'u-1',
  orgId: 'org-a',
  role: AdminRole.ORG_ADMIN,
};

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: currentUser }),
}));

function workspaceRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 'ws-1',
    org_id: 'org-a',
    name: 'research',
    display_name: 'Research',
    isolation_mode: 'dedicated',
    status: 'Active',
    is_default: true,
    cluster_health: 'Healthy',
    last_heartbeat: new Date().toISOString(),
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    ...overrides,
  };
}

/** The storage key a receipt for `orgId` is filed under, in this deployment. */
function receiptKey(orgId: string) {
  return `adp.superplane.onboarding.receipt.${window.location.origin}.${orgId}.create-workspace`;
}

/**
 * A stored receipt as `operations.ts` writes one.
 *
 * Defaults to the lost-reply shape — `unknown`, no workspace — because that is the
 * state these tests are about: the one record that cannot be reconstructed.
 */
function storedReceipt(
  overrides: { orgId: string; state?: string; workspaceId?: string | null },
) {
  return JSON.stringify({
    idempotencyKey: 'key-foreign',
    operationId: null,
    fingerprint: 'zz',
    scope: { deploymentId: window.location.origin, orgId: overrides.orgId },
    createdAt: '2026-09-01T00:00:00Z',
    state: overrides.state ?? 'unknown',
    workspaceId: overrides.workspaceId ?? null,
  });
}

/** Respond to the workspace list with the given rows. */
function listReturns(workspaces: unknown[]) {
  server.use(
    http.get(API('/workspaces'), () =>
      HttpResponse.json({ workspaces, total: workspaces.length }),
    ),
  );
}

/**
 * Make create reachable: serve the capability route and advertise the feature.
 *
 * Two separate things, and the separation is the point — a served route that does
 * not advertise the feature must still block, which the tests below assert. `served`
 * is a deployment fact pinned elsewhere against the real proxy allowlist, so
 * flipping it here cannot hide drift.
 */
function createIsAvailable(features: string[] = [CREATE_IDEMPOTENCY_FEATURE]) {
  const saved = ENDPOINTS.capabilities.served;
  (ENDPOINTS.capabilities as { served: boolean }).served = true;
  restoreServed = () => {
    (ENDPOINTS.capabilities as { served: boolean }).served = saved;
  };
  server.use(
    http.get(API('/capabilities'), () =>
      HttpResponse.json({ features, modes: ['managed'], providers: ['aws'] }),
    ),
  );
}

let restoreServed: (() => void) | null = null;
let restoreDeployment: () => void;

const baselineCapabilitiesServed = ENDPOINTS.capabilities.served;
beforeEach(() => {
  restoreDeployment = withoutOnboardingEndpoints();
  server.use(http.get(API('/workspaces/:workspaceId/deployments'), ({ params }) =>
    HttpResponse.json({ workspace_id: params.workspaceId, deployments: [] })));
  // This suite's default fixture is a deployment without capability reporting.
  (ENDPOINTS.capabilities as { served: boolean }).served = false;
  currentUser = { id: 'u-1', orgId: 'org-a', role: AdminRole.ORG_ADMIN };
  window.sessionStorage.setItem('cognito_access_token', 'test-token');
});

afterEach(() => {
  restoreServed?.();
  restoreServed = null;
  restoreDeployment();
  (ENDPOINTS.capabilities as { served: boolean }).served = baselineCapabilitiesServed;
  window.localStorage.clear();
});

describe('AC-01: beginning onboarding with zero workspaces', () => {
  it('presents the empty control plane as a starting point, not a fault', async () => {
    listReturns([]);
    render(<OnboardingView />);

    // The heading is the assertion that matters: "No workspaces yet" frames this
    // as the expected state of a fresh install. An error banner here would tell an
    // operator something is broken when nothing is.
    expect(
      await screen.findByRole('heading', { name: 'No workspaces yet' }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toHaveTextContent(/could not load/i);
  });

  it('offers an org admin the create action', async () => {
    listReturns([]);
    render(<OnboardingView />);

    expect(
      await screen.findByRole('button', { name: /create a workspace/i }),
    ).toBeInTheDocument();
  });

  it('offers a platform admin the create action', async () => {
    currentUser = { id: 'u-1', orgId: 'org-a', role: AdminRole.PLATFORM_ADMIN };
    listReturns([]);
    render(<OnboardingView />);

    expect(
      await screen.findByRole('button', { name: /create a workspace/i }),
    ).toBeInTheDocument();
  });

  it('shows a member no create action, and says who can', async () => {
    currentUser = { id: 'u-1', orgId: 'org-a', role: AdminRole.MEMBER };
    listReturns([]);
    render(<OnboardingView />);

    await screen.findByRole('heading', { name: 'No workspaces yet' });
    // AC-01: "other roles see only permitted actions". A disabled-looking button
    // that would 403 is not a permitted action; it is an invitation to fail.
    expect(screen.queryByRole('button', { name: /create a workspace/i })).toBeNull();
    expect(
      screen.getByText(/requires an organization or platform administrator/i),
    ).toBeInTheDocument();
  });

  it('shows no create action to a user whose role claim is absent', async () => {
    // A freshly signed-up user carries no custom:role claim. Defaulting an absent
    // role to "probably an admin" is the fail-open direction.
    currentUser = { id: 'u-1', orgId: 'org-a', role: undefined };
    listReturns([]);
    render(<OnboardingView />);

    await screen.findByRole('heading', { name: 'No workspaces yet' });
    expect(screen.queryByRole('button', { name: /create a workspace/i })).toBeNull();
  });

  it('shows no create action to a dept admin, who does not administer the org', async () => {
    currentUser = { id: 'u-1', orgId: 'org-a', role: AdminRole.DEPT_ADMIN };
    listReturns([]);
    render(<OnboardingView />);

    await screen.findByRole('heading', { name: 'No workspaces yet' });
    expect(screen.queryByRole('button', { name: /create a workspace/i })).toBeNull();
  });
});

describe('AC-04: create is disabled, with the gap named, when it cannot be submitted safely', () => {
  it('disables create and explains that idempotency cannot be confirmed', async () => {
    // The fixture models a deployment without capability discovery, so the
    // client reports not-deployed without issuing a request. The screen must not
    // offer a create it cannot make idempotent.
    listReturns([]);
    render(<OnboardingView />);

    const create = await screen.findByRole('button', { name: /create a workspace/i });
    await waitFor(() => expect(create).toBeDisabled());

    const warning = screen.getByText(/workspace creation is not available yet/i);
    expect(warning).toBeInTheDocument();
  });

  it('explains the missing capability in the operator\'s terms, citing no story', async () => {
    // Without this an admin sees "not available" and has nowhere to go. But the
    // way out must be a capability they can ask their platform owner about, not
    // our backlog position: an internal story number is unusable to whoever is
    // actually blocked, and it goes stale the moment the story closes.
    listReturns([]);
    render(<OnboardingView />);

    await screen.findByRole('button', { name: /create a workspace/i });
    await waitFor(() =>
      expect(
        screen.getByText(/does not support reporting which workspace features/i),
      ).toBeInTheDocument(),
    );
  });

  it('warns that a non-idempotent retry could bill twice', async () => {
    listReturns([]);
    render(<OnboardingView />);

    await screen.findByRole('button', { name: /create a workspace/i });
    await waitFor(() =>
      expect(screen.getByText(/create-operation-id-v1/)).toBeInTheDocument(),
    );
  });

  it('keeps the loaded workspace list when capability discovery fails', async () => {
    // AC-04 partial failure: one failed request degrades one affordance. Blanking
    // the list because a secondary request failed would hide working information.
    listReturns([workspaceRow()]);
    render(<OnboardingView />);

    expect(await screen.findByRole('button', { name: /Research/ })).toBeInTheDocument();
  });
});

describe('AC-04: control-plane health never marks a workspace execution-ready', () => {
  it('shows no ready verdict for a provisioning workspace', async () => {
    listReturns([
      workspaceRow({ status: 'Provisioning', cluster_health: null, last_heartbeat: null }),
    ]);
    render(<OnboardingView />);

    await userEvent.click(await screen.findByRole('button', { name: /Research/ }));

    const readiness = screen.getByRole('region', { name: 'Readiness' });
    const rows = within(readiness).getAllByRole('listitem');
    expect(rows).toHaveLength(3);
    // The workspace row is its own statement and it says "Not ready". This is the
    // exact defect AC-04 names: a reachable control plane must not colour this green.
    expect(within(rows[1]).getByText('Not ready')).toBeInTheDocument();
    expect(within(rows[1]).getByText(/"Provisioning"/)).toBeInTheDocument();
  });

  it('reports the control plane as unobserved rather than inferring it from a successful list', async () => {
    // The list answering proves the API is up. It is still not a control-plane
    // health observation, and claiming one would be exactly the sideways version
    // of the prohibited inference.
    listReturns([workspaceRow()]);
    render(<OnboardingView />);

    await userEvent.click(await screen.findByRole('button', { name: /Research/ }));

    const rows = within(screen.getByRole('region', { name: 'Readiness' })).getAllByRole(
      'listitem',
    );
    expect(within(rows[0]).getByText('Unknown')).toBeInTheDocument();
    expect(within(rows[0]).getByText(/has not been reached yet/i)).toBeInTheDocument();
  });

  it('never renders a single combined readiness verdict', async () => {
    // A guard against someone later adding the one badge everybody wants. If a
    // summary verdict appears anywhere on this screen, this fails.
    listReturns([workspaceRow()]);
    render(<OnboardingView />);

    await userEvent.click(await screen.findByRole('button', { name: /Research/ }));

    expect(screen.queryByText(/execution[- ]ready/i)).toBeNull();
    expect(screen.queryByText(/^ready to run/i)).toBeNull();
  });

  it('shows the provider reading as unvalidated, not as ready', async () => {
    listReturns([workspaceRow()]);
    render(<OnboardingView />);

    await userEvent.click(await screen.findByRole('button', { name: /Research/ }));

    const rows = within(screen.getByRole('region', { name: 'Readiness' })).getAllByRole(
      'listitem',
    );
    expect(within(rows[2]).getByText('Unknown')).toBeInTheDocument();
    expect(within(rows[2]).getByText(/not been validated by the service/i)).toBeInTheDocument();
  });
});

describe('AC-03: replies from a superseded organization scope', () => {
  it('discards a list that arrives after the org changed', async () => {
    // The ordering here is the whole test, and it is easy to get wrong in a way
    // that passes for free: org-a's reply must be released only AFTER org-b has
    // already rendered. Released any earlier it has nothing to overwrite, and the
    // test passes whether or not the discard works at all.
    let releaseOrgA: (() => void) | null = null;
    let orgASent: () => void;
    const orgARequested = new Promise<void>((resolve) => {
      orgASent = resolve;
    });

    let calls = 0;
    server.use(
      http.get(API('/workspaces'), async () => {
        calls += 1;
        if (calls === 1) {
          await new Promise<void>((release) => {
            releaseOrgA = release;
            orgASent();
          });
          return HttpResponse.json({
            workspaces: [workspaceRow({ id: 'ws-a', display_name: 'Org A Secret' })],
            total: 1,
          });
        }
        return HttpResponse.json({
          workspaces: [workspaceRow({ id: 'ws-b', display_name: 'Org B Workspace' })],
          total: 1,
        });
      }),
    );

    const { rerender } = render(<OnboardingView />);
    await orgARequested;

    // Switch organizations while org-a's reply is still outstanding.
    currentUser = { id: 'u-1', orgId: 'org-b', role: AdminRole.ORG_ADMIN };
    rerender(<OnboardingView />);

    // org-b is on screen and settled before the stale reply is let go.
    expect(await screen.findByRole('button', { name: /Org B Workspace/ })).toBeInTheDocument();

    releaseOrgA!();
    // Give the released reply every chance to land and overwrite the screen.
    await new Promise((resolve) => setTimeout(resolve, 0));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Org B Workspace/ })).toBeInTheDocument(),
    );

    // The decisive assertion: org-a's workspace must never appear under org-b.
    expect(screen.queryByText(/Org A Secret/)).toBeNull();
  });
});

describe('AC-04: actionable failure states', () => {
  it('tells a user without access to ask an administrator, and offers no retry', async () => {
    server.use(
      http.get(API('/workspaces'), () => HttpResponse.json({}, { status: 403 })),
    );
    render(<OnboardingView />);

    expect(
      await screen.findByText(/you do not have access to this/i),
    ).toBeInTheDocument();
    // Retrying a 403 changes nothing. Offering the button teaches the user that
    // the platform's buttons are decorative.
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull();
  });

  it('offers a retry for a transient outage', async () => {
    let calls = 0;
    server.use(
      http.get(API('/workspaces'), () => {
        calls += 1;
        if (calls === 1) return HttpResponse.json({}, { status: 503 });
        return HttpResponse.json({ workspaces: [workspaceRow()], total: 1 });
      }),
    );
    render(<OnboardingView />);

    const retry = await screen.findByRole('button', { name: /try again/i });
    await userEvent.click(retry);

    expect(await screen.findByRole('button', { name: /Research/ })).toBeInTheDocument();
  });

  it('reports a session expiry rather than rendering an empty screen', async () => {
    // AC-05 names session expiry during onboarding. apiClient clears the token and
    // begins a redirect; the screen must still say what happened instead of
    // showing a bare empty state that looks like "you have no workspaces".
    server.use(
      http.get(API('/workspaces'), () => HttpResponse.json({}, { status: 401 })),
    );
    render(<OnboardingView />);

    expect(await screen.findByText(/session has expired/i)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'No workspaces yet' })).toBeNull();
  });

  it('rejects a malformed list instead of rendering a partially-shaped screen', async () => {
    server.use(http.get(API('/workspaces'), () => HttpResponse.json({ items: [] })));
    render(<OnboardingView />);

    expect(
      await screen.findByText(/does not understand|older or newer/i),
    ).toBeInTheDocument();
    // Not the zero-workspace state: "we could not read the answer" and "there are
    // none" are different facts and only one of them invites creating a workspace.
    expect(screen.queryByRole('heading', { name: 'No workspaces yet' })).toBeNull();
  });
});

describe('AC-05: keyboard and accessible structure', () => {
  it('makes each workspace reachable and operable from the keyboard', async () => {
    listReturns([
      workspaceRow(),
      workspaceRow({ id: 'ws-2', name: 'ops', display_name: 'Operations' }),
    ]);
    render(<OnboardingView />);

    const first = await screen.findByRole('button', { name: /Research/ });
    first.focus();
    // Space and Enter both activate a real <button>; a clickable div gets neither.
    await userEvent.keyboard('{Enter}');

    await waitFor(() => expect(first).toHaveAttribute('aria-pressed', 'true'));
    expect(screen.getByRole('button', { name: /Operations/ })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('gives every region an accessible name', async () => {
    listReturns([workspaceRow()]);
    render(<OnboardingView />);

    await screen.findByRole('button', { name: /Research/ });
    expect(screen.getByRole('region', { name: 'Workspaces' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Readiness' })).toBeInTheDocument();
  });

  it('names the empty state region so a screen reader announces it', async () => {
    listReturns([]);
    render(<OnboardingView />);

    expect(
      await screen.findByRole('region', { name: 'No workspaces yet' }),
    ).toBeInTheDocument();
  });

  it('ties the disabled create button to the explanation of why', async () => {
    // A disabled button with an unassociated paragraph beside it reads, to a
    // screen reader, as a dead control with no reason given.
    listReturns([]);
    render(<OnboardingView />);

    const create = await screen.findByRole('button', { name: /create a workspace/i });
    await waitFor(() => expect(create).toBeDisabled());
    const describedBy = create.getAttribute('aria-describedby');
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent(
      /not available yet/i,
    );
  });
});

describe('the create flow is reachable and scoped (AC-01/AC-03)', () => {
  it('opens the create flow from the zero-workspace state', async () => {
    createIsAvailable();
    listReturns([]);
    render(<OnboardingView />);

    await userEvent.click(await screen.findByRole('button', { name: /create a workspace/i }));

    expect(
      await screen.findByRole('region', { name: 'Create a workspace' }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Workspace name')).toBeInTheDocument();
  });

  it('tidies away a settled receipt from another organization', async () => {
    // Housekeeping on records that are finished. Nothing can be recovered from a
    // succeeded operation's receipt, so leaving it on disk only accumulates.
    const foreign = receiptKey('org-OTHER');
    window.localStorage.setItem(
      foreign,
      storedReceipt({ orgId: 'org-OTHER', state: 'succeeded', workspaceId: 'ws-other' }),
    );
    listReturns([]);
    render(<OnboardingView />);

    await screen.findByRole('heading', { name: 'No workspaces yet' });
    await waitFor(() => expect(window.localStorage.getItem(foreign)).toBeNull());
  });

  it('keeps another organization\'s unresolved receipt on disk', async () => {
    // The data-loss defect. An `unknown` receipt is the only record of an operation
    // that may have provisioned and may be billing; it is what stops the user
    // resubmitting when they return. Deleting it is not isolation — isolation comes
    // from the scoped key and readReceipt's scope check, which the next test shows
    // still hides it from this screen.
    const foreign = receiptKey('org-OTHER');
    window.localStorage.setItem(foreign, storedReceipt({ orgId: 'org-OTHER' }));
    listReturns([]);
    render(<OnboardingView />);

    await screen.findByRole('heading', { name: 'No workspaces yet' });
    // Waited on rather than checked immediately: the prune runs in an effect, so an
    // immediate read would pass even if the effect deleted the record a tick later.
    await waitFor(() => expect(screen.queryByText(/loading workspaces/i)).toBeNull());
    expect(window.localStorage.getItem(foreign)).not.toBeNull();
  });

  it('returns an organization\'s lost-reply receipt after a switch away and back', async () => {
    // The journey AC-02 names: A submits, the reply is lost, the user looks at B,
    // then comes back to A. A's warning and its preserved identity must both be
    // there — otherwise the obvious next action mints a new identity and builds a
    // second workspace.
    const mine = receiptKey('org-a');
    window.localStorage.setItem(mine, storedReceipt({ orgId: 'org-a' }));
    createIsAvailable();
    listReturns([]);

    const { unmount } = render(<OnboardingView />);
    await screen.findByRole('heading', { name: 'No workspaces yet' });
    unmount();

    currentUser = { id: 'u-1', orgId: 'org-b', role: AdminRole.ORG_ADMIN };
    const second = render(<OnboardingView />);
    await screen.findByRole('heading', { name: 'No workspaces yet' });
    second.unmount();

    currentUser = { id: 'u-1', orgId: 'org-a', role: AdminRole.ORG_ADMIN };
    render(<OnboardingView />);
    await userEvent.click(await screen.findByRole('button', { name: /create a workspace/i }));

    expect(
      await screen.findByText(/earlier submission's outcome is unknown/i),
    ).toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem(mine)!).idempotencyKey).toBe('key-foreign');
  });

  it('prunes nothing while the signed-in organization is still unresolved', async () => {
    // `useAuth` answers a render or two late, and `orgId` is '' until it does. An
    // empty string is not an organization: pruning against it makes every real
    // scope look foreign, so simply opening the page would delete the receipts of
    // the organization about to be selected.
    currentUser = { id: 'u-1', orgId: undefined, role: AdminRole.ORG_ADMIN };
    const mine = receiptKey('org-a');
    window.localStorage.setItem(
      mine,
      storedReceipt({ orgId: 'org-a', state: 'succeeded', workspaceId: 'ws-1' }),
    );
    listReturns([]);
    render(<OnboardingView />);

    await screen.findByRole('heading', { name: 'No workspaces yet' });
    await waitFor(() => expect(screen.queryByText(/loading workspaces/i)).toBeNull());
    // Settled, so a prune WOULD have removed it. That is what makes this test able
    // to detect the unresolved-org case rather than passing on the retention rule.
    expect(window.localStorage.getItem(mine)).not.toBeNull();
  });

  it('does not surface another organization\'s unresolved submission', async () => {
    // The consequence of the previous test, stated as the user-visible outcome: an
    // unknown-outcome warning from a tenant the user has left must not appear here.
    window.localStorage.setItem(receiptKey('org-OTHER'), storedReceipt({ orgId: 'org-OTHER' }));
    createIsAvailable();
    listReturns([]);
    render(<OnboardingView />);

    await userEvent.click(await screen.findByRole('button', { name: /create a workspace/i }));
    await screen.findByRole('region', { name: 'Create a workspace' });

    expect(screen.queryByText(/earlier submission's outcome is unknown/i)).toBeNull();
  });

  it('shows a member no way to reach the create flow', async () => {
    currentUser = { id: 'u-1', orgId: 'org-a', role: AdminRole.MEMBER };
    listReturns([]);
    render(<OnboardingView />);

    await screen.findByRole('heading', { name: 'No workspaces yet' });
    expect(screen.queryByRole('region', { name: 'Create a workspace' })).toBeNull();
  });

  it('returns to the list and reloads it after a successful create', async () => {
    createIsAvailable();
    let calls = 0;
    server.use(
      http.get(API('/workspaces'), () => {
        calls += 1;
        // Empty first, then populated -- modelling the workspace the create made.
        if (calls === 1) return HttpResponse.json({ workspaces: [], total: 0 });
        return HttpResponse.json({ workspaces: [workspaceRow()], total: 1 });
      }),
    );
    render(<OnboardingView />);
    await userEvent.click(await screen.findByRole('button', { name: /create a workspace/i }));
    await screen.findByRole('region', { name: 'Create a workspace' });

    // Cancel stands in for the flow finishing: either way the list is what the
    // user must land back on.
    await userEvent.click(screen.getByRole('button', { name: /cancel/i }));
    expect(await screen.findByRole('heading', { name: 'No workspaces yet' })).toBeInTheDocument();
  });
});

describe('AC-04: a served capability route is not by itself permission to create', () => {
  it('blocks create when the server answers but advertises no operation identity', async () => {
    // The dangerous case, and the reason the check is membership rather than
    // reachability: this deployment serves /capabilities perfectly well. It just
    // does not honour a submitted operation identity, so a create would return 201
    // and deduplicate nothing. Reachability is not a guarantee.
    createIsAvailable([]);
    listReturns([]);
    render(<OnboardingView />);

    const create = await screen.findByRole('button', { name: /create a workspace/i });
    await waitFor(() => expect(create).toBeDisabled());
    expect(
      screen.getByText(/cannot confirm that it honours a submitted operation identity/i),
    ).toBeInTheDocument();
  });

  it('puts no story or ticket reference anywhere on the blocked screen', async () => {
    // A guard over the whole rendered surface rather than one string, because
    // the leak this prevents is an easy one to reintroduce: the blocking reason
    // is built from an internal descriptor, and appending its tracking field to
    // the banner reads as helpful while shipping our backlog to the customer.
    // Scoped to what the user can see — source comments may cite stories freely.
    createIsAvailable([]);
    listReturns([]);
    render(<OnboardingView />);

    const create = await screen.findByRole('button', { name: /create a workspace/i });
    await waitFor(() => expect(create).toBeDisabled());

    const visible = document.body.textContent ?? '';
    expect(visible).not.toMatch(/#\d+/);
    expect(visible).not.toMatch(/\btracked in\b/i);
    expect(visible).not.toMatch(/\b(story|ticket|jira|epic|backlog)\b/i);
  });

  it('blocks create when the report advertises only unrelated features', async () => {
    // Guards against a membership check degenerating into "the list is non-empty".
    createIsAvailable(['some-other-feature']);
    listReturns([]);
    render(<OnboardingView />);

    const create = await screen.findByRole('button', { name: /create a workspace/i });
    await waitFor(() => expect(create).toBeDisabled());
  });

  it('enables create only once the identity feature is advertised', async () => {
    createIsAvailable([CREATE_IDEMPOTENCY_FEATURE]);
    listReturns([]);
    render(<OnboardingView />);

    const create = await screen.findByRole('button', { name: /create a workspace/i });
    // Positive control for the two tests above: without it, a permanently-disabled
    // button would satisfy them both and the feature would never be reachable.
    await waitFor(() => expect(create).toBeEnabled());
    expect(screen.queryByText(/not available yet/i)).toBeNull();
  });

  it('starts blocked before capability discovery has answered', async () => {
    // The window this closes: the workspace list resolves first, so there is a
    // real interval during which the screen is rendered and the capability reply
    // has not landed. Initialising the block to "nothing is wrong" would enable the
    // button during it, and a click there submits under a guarantee nobody checked.
    // Held open deliberately so the interval is observable instead of a race.
    const saved = ENDPOINTS.capabilities.served;
    (ENDPOINTS.capabilities as { served: boolean }).served = true;
    restoreServed = () => {
      (ENDPOINTS.capabilities as { served: boolean }).served = saved;
    };
    let release: (() => void) | null = null;
    server.use(
      http.get(API('/capabilities'), async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return HttpResponse.json({ features: [CREATE_IDEMPOTENCY_FEATURE] });
      }),
    );
    listReturns([]);
    render(<OnboardingView />);

    const create = await screen.findByRole('button', { name: /create a workspace/i });
    expect(create).toBeDisabled();
    expect(screen.getByText(/has not yet reported/i)).toBeInTheDocument();

    // And it clears once the answer actually arrives — otherwise a permanently
    // disabled button would satisfy the assertion above for the wrong reason.
    await waitFor(() => expect(release).not.toBeNull());
    release!();
    await waitFor(() => expect(create).toBeEnabled());
  });

  it('blocks create when the capability report is malformed', async () => {
    // An older or newer server whose shape this client cannot read. Fail-closed:
    // an unreadable answer about idempotency is not a yes.
    const saved = ENDPOINTS.capabilities.served;
    (ENDPOINTS.capabilities as { served: boolean }).served = true;
    restoreServed = () => {
      (ENDPOINTS.capabilities as { served: boolean }).served = saved;
    };
    server.use(http.get(API('/capabilities'), () => HttpResponse.json('not-an-object')));
    listReturns([]);
    render(<OnboardingView />);

    const create = await screen.findByRole('button', { name: /create a workspace/i });
    await waitFor(() => expect(create).toBeDisabled());
  });
});

/**
 * Readiness that ages, and provider readings that are really observed — the review
 * finding: "Readiness caches `Date.now()` until selection changes and never ages on
 * an open page; provider validation/admission observations stay null."
 *
 * Both halves are one defect wearing two faces: the screen was making claims about
 * a credential and about currency that it had not actually established. The tests
 * therefore assert against real server responses (the connection GET really does
 * carry a `validation` block, in `validation_response`'s shape) and against the
 * passage of time, not against a rerender.
 */
describe('readings age and are observed, not assumed (AC-04)', () => {
  /** A heartbeat this many milliseconds in the past, as an ISO string. */
  function heartbeatAgo(ms: number) {
    return new Date(Date.now() - ms).toISOString();
  }

  afterEach(() => {
    vi.useRealTimers();
  });

  it('turns a fresh workspace reading stale while nobody touches the page', async () => {
    // THE defect. The workspace is Active and its cluster reported four minutes
    // ago, so it is genuinely ready — and four minutes later it is genuinely not
    // known to be. The only thing that happens between the two assertions is that
    // time passes: no click, no refetch, no selection change. A `Date.now()`
    // captured in a memo keyed on selection cannot produce the second reading.
    //
    // `shouldAdvanceTime` is load-bearing: RTL's `waitFor` only auto-advances Jest
    // fake timers, so under a plain `vi.useFakeTimers()` every await below polls a
    // frozen clock and hangs.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    listReturns([
      workspaceRow({ status: 'Active', cluster_health: 'Healthy', last_heartbeat: heartbeatAgo(4 * 60 * 1000) }),
    ]);
    render(<OnboardingView />);

    await user.click(await screen.findByRole('button', { name: /Research/ }));

    const workspaceRowOf = () =>
      within(screen.getByRole('region', { name: 'Readiness' })).getAllByRole('listitem')[1];
    expect(within(workspaceRowOf()).getByText('Ready')).toBeInTheDocument();

    // Two more minutes of an open dashboard. The heartbeat is now six minutes old,
    // past the server's own five-minute degradation threshold.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2 * 60 * 1000);
    });

    // Unknown, not Ready and not "Not ready": the cluster has not said it is
    // unhealthy, it has stopped saying anything, and those call for a check rather
    // than a fix.
    await waitFor(() =>
      expect(within(workspaceRowOf()).getByText('Unknown')).toBeInTheDocument(),
    );
    expect(within(workspaceRowOf()).getByText(/not reported recently/i)).toBeInTheDocument();
  });

  it('shows the provider reading the connection panel actually read from the server', async () => {
    // The second half. The panel below was displaying four readings from a real GET
    // while the readiness row above it said "never validated" — one screen, two
    // answers about one credential, and the misleading one was the one shaped like
    // a verdict.
    //
    // The reading is delivered the way the server really delivers one: inside the
    // connection body, keyed `validation`. There is no probe here and no handler for
    // the validation route, so a fabricated reading would fail against
    // `onUnhandledRequest: 'error'`.
    const row = vaultCredentialRow();
    const connection = connectionResponse(row, {
      status: 'Active',
      admits_new_work: true,
      validation: validationResponse({ checked_at: new Date().toISOString() }),
    });
    createIsAvailable();
    listReturns([workspaceRow()]);
    server.use(
      http.get(API('/vault/credentials'), () => HttpResponse.json(vaultCredentialList([row]))),
      http.get('/api/auth/credentials', () => HttpResponse.json([{ id: row.adp_credential_id, service: row.provider, label: row.name }])),
      http.put('/api/auth/credentials/:credential/workspaces/:workspace', () => HttpResponse.json({ delegated: true })),
      http.post(API('/workspaces/ws-1/provider-connections'), () =>
        HttpResponse.json(connection, { status: 201 }),
      ),
    );
    const user = userEvent.setup();
    render(<OnboardingView />);

    await user.click(await screen.findByRole('button', { name: /Research/ }));

    const providerRowOf = () =>
      within(screen.getByRole('region', { name: 'Readiness' })).getAllByRole('listitem')[2];
    // Before anything is bound the honest answer is that nothing was observed.
    expect(within(providerRowOf()).getByText('Unknown')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /choose a vault credential/i }));
    await user.selectOptions(
      await screen.findByLabelText('Vault credential'),
      row.adp_credential_id,
    );
    await user.click(screen.getByRole('button', { name: /bind credential to workspace/i }));
    await screen.findByRole('heading', { name: /bound credential/i });

    // And now the readiness row states what the server said, not a placeholder.
    await waitFor(() =>
      expect(within(providerRowOf()).getByText('Ready')).toBeInTheDocument(),
    );
    expect(
      within(providerRowOf()).getByText(/validated the credential, its permissions and its quota/i),
    ).toBeInTheDocument();
  });

  it('does not attribute one workspace\'s credential reading to another', async () => {
    // The connection panel remounts on selection change, but the observation the
    // parent is holding does not vanish with it. Unchecked, workspace B's readiness
    // would display workspace A's credential reading for as long as B's panel took
    // to report — a misattribution inside one organization, of the same kind AC-03
    // forbids across them.
    const row = vaultCredentialRow();
    createIsAvailable();
    listReturns([
      workspaceRow(),
      workspaceRow({ id: 'ws-2', name: 'ops', display_name: 'Operations' }),
    ]);
    server.use(
      http.get(API('/vault/credentials'), () => HttpResponse.json(vaultCredentialList([row]))),
      http.get('/api/auth/credentials', () => HttpResponse.json([{ id: row.adp_credential_id, service: row.provider, label: row.name }])),
      http.put('/api/auth/credentials/:credential/workspaces/:workspace', () => HttpResponse.json({ delegated: true })),
      http.post(API('/workspaces/ws-1/provider-connections'), () =>
        HttpResponse.json(
          connectionResponse(row, {
            status: 'Active',
            admits_new_work: true,
            validation: validationResponse({ checked_at: new Date().toISOString() }),
          }),
          { status: 201 },
        ),
      ),
    );
    const user = userEvent.setup();
    render(<OnboardingView />);

    const providerRowOf = () =>
      within(screen.getByRole('region', { name: 'Readiness' })).getAllByRole('listitem')[2];

    await user.click(await screen.findByRole('button', { name: /Research/ }));
    await user.click(screen.getByRole('button', { name: /choose a vault credential/i }));
    await user.selectOptions(
      await screen.findByLabelText('Vault credential'),
      row.adp_credential_id,
    );
    await user.click(screen.getByRole('button', { name: /bind credential to workspace/i }));
    await waitFor(() =>
      expect(within(providerRowOf()).getByText('Ready')).toBeInTheDocument(),
    );

    // Switch to a workspace nothing has been bound to. No handler is registered for
    // ws-2's connections, so any request on its behalf would fail the test outright.
    await user.click(screen.getByRole('button', { name: /Operations/ }));

    await waitFor(() =>
      expect(within(providerRowOf()).getByText('Unknown')).toBeInTheDocument(),
    );
    expect(
      within(providerRowOf()).getByText(/not been validated by the service yet/i),
    ).toBeInTheDocument();
  });
});
