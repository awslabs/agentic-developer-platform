/**
 * Review-then-submit creation — #5730 AC-02 (one operation identity across
 * refresh, timeout and repeated submit) and AC-04 (actionable blocked states).
 *
 * These tests explicitly model both an older deployment without onboarding
 * routes and a deployment serving the full journey. This preserves unavailable
 * route coverage while exercising the idempotency logic that prevents spending
 * twice when the routes are available.
 */

import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { server } from '@/mocks/server';
import { CreateWorkspaceFlow, CREATE_INTENT, buildCreatePayload } from '@superplane-ui/CreateWorkspaceFlow';
import { ScopeGuard } from '@superplane-ui/client';
import {
  DOMAIN_BASE,
  ENDPOINTS,
  OPERATION_ID_FIELD,
  type OnboardingInputs,
  type OnboardingPlan,
} from '@superplane-ui/contract';
import { memoryReceiptStore, readReceipt, type ReceiptStore } from '@superplane-ui/operations';
import { withoutOnboardingEndpoints } from './endpoint-fixtures';

const API = (path: string) => `/api${DOMAIN_BASE}${path}`;
const SCOPE = { deploymentId: 'dev', orgId: 'org-a' };

/**
 * The plan as the SERVER would send it: snake_case, matching every other response
 * in this domain. Deliberately not the camelCase `OnboardingPlan` type — a fixture
 * shaped like the internal type would test the component against a wire format
 * that does not exist and would pass while the real parser rejected every plan.
 */
const PLAN_WIRE = {
  revision: 'rev-7',
  mode: 'managed',
  target: { account: '111122223333', region: 'us-east-1', cluster: null },
  ownership: 'adp-managed',
  requested_capacity: '2 GPUs',
  cost_estimate: null,
  approval_required: false,
} as const;

/** The same plan after parsing, for the pure payload-builder tests. */
const PLAN: OnboardingPlan = {
  revision: 'rev-7',
  mode: 'managed',
  target: { account: '111122223333', region: 'us-east-1', cluster: null },
  ownership: 'adp-managed',
  requestedCapacity: '2 GPUs',
  costEstimate: null,
  approvalRequired: false,
};

/**
 * Force endpoints served for the duration of a test.
 *
 * `served` is a deployment fact, not a behaviour under test — the contract test
 * separately pins it against the real proxy allowlist, so flipping it here cannot
 * hide drift. Each journey declares the routes its deployment provides.
 */
function withServed(names: Array<keyof typeof ENDPOINTS>) {
  const saved = names.map((name) => [name, ENDPOINTS[name].served] as const);
  for (const name of names) {
    (ENDPOINTS[name] as { served: boolean }).served = true;
  }
  return () => {
    for (const [name, value] of saved) {
      (ENDPOINTS[name] as { served: boolean }).served = value;
    }
  };
}

let restore: (() => void) | null = null;
let restoreDeployment: () => void;
let store: ReceiptStore;
let guard: ScopeGuard;
let keyCounter: number;

beforeEach(() => {
  restoreDeployment = withoutOnboardingEndpoints();
  window.sessionStorage.setItem('cognito_access_token', 'test-token');
  store = memoryReceiptStore();
  guard = new ScopeGuard();
  keyCounter = 0;
});

afterEach(() => {
  restore?.();
  restore = null;
  restoreDeployment();
});

const mintKey = () => {
  keyCounter += 1;
  return `key-${keyCounter}`;
};

function renderFlow(overrides: Partial<React.ComponentProps<typeof CreateWorkspaceFlow>> = {}) {
  return render(
    <CreateWorkspaceFlow
      guard={guard}
      scope={SCOPE}
      store={store}
      idempotencySupport={null}
      mintKey={mintKey}
      nowIso={() => '2026-09-23T00:00:00Z'}
      {...overrides}
    />,
  );
}

/** Fill the name and reach the reviewed-plan state. */
async function reachReview(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Workspace name'), 'research');
  await user.click(screen.getByRole('button', { name: /review plan/i }));
  await screen.findByRole('group', { name: /review this plan/i });
}

describe('AC-05: keyboard focus follows workspace review and errors', () => {
  it('focuses the new form and then the server-reviewed plan', async () => {
    restore = withServed(['previewWorkspace']);
    server.use(http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)));
    const user = userEvent.setup();
    renderFlow();
    expect(screen.getByRole('heading', { name: 'Create a workspace' })).toHaveFocus();
    await user.type(screen.getByLabelText('Workspace name'), 'research');
    await user.tab();
    await user.click(screen.getByRole('button', { name: /review plan/i }));
    expect(await screen.findByRole('group', { name: /review this plan/i })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Review this plan' })).toHaveFocus());
    expect(screen.getByRole('button', { name: /create this workspace/i })).toBeEnabled();
  });

  it('focuses the error summary after a preview refusal', async () => {
    restore = withServed(['previewWorkspace']);
    server.use(http.post(API('/workspaces/preview'), () => new HttpResponse(null, { status: 503 })));
    const user = userEvent.setup();
    renderFlow();
    await user.type(screen.getByLabelText('Workspace name'), 'research');
    await user.click(screen.getByRole('button', { name: /review plan/i }));
    const problem = await screen.findByRole('group', { name: 'Workspace submission problem' });
    await waitFor(() => expect(problem).toHaveFocus());
    expect(problem).toHaveTextContent(/cannot be submitted/i);
  });
});

describe('AC-04: refusing to submit what cannot be submitted safely', () => {
  it('blocks the plan request when the preview route is not served', async () => {
    // An explicitly unavailable deployment. No request is issued — MSW runs with
    // onUnhandledRequest: 'error', so an attempt would fail this test loudly.
    const user = userEvent.setup();
    renderFlow();

    await user.type(screen.getByLabelText('Workspace name'), 'research');
    await user.click(screen.getByRole('button', { name: /review plan/i }));

    // Leads with the capability the user is missing, and still names the exact
    // method and path afterwards for whoever is diagnosing the deployment.
    expect(
      await screen.findByText(/does not support reviewing the exact plan/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/POST \/superplane\/v1\/workspaces\/preview is not available/i),
    ).toBeInTheDocument();
  });

  it('disables submission when the environment cannot honour an operation identity', async () => {
    restore = withServed(['previewWorkspace']);
    server.use(http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)));
    const user = userEvent.setup();
    renderFlow({
      idempotencySupport: {
        reason: 'not-deployed',
        detail: 'This environment does not confirm operation identity.',
      },
    });

    await reachReview(user);

    expect(screen.getByRole('button', { name: /create this workspace/i })).toBeDisabled();
    expect(screen.getByText(/create-operation-id-v1/)).toBeInTheDocument();
  });

  it('guards the submit handler itself, not only the button', async () => {
    // Belt and braces, because `disabled` is one line from being deleted and the
    // cost of getting this wrong is a double charge. The guard must live in the
    // handler as well as the affordance.
    //
    // This cannot be tested by forcing a click. React will not invoke `onClick`
    // when the fiber's `disabled` prop is true, whatever the DOM property says, so
    // a "forced click" test passes vacuously: the handler is never reached and
    // `created` is never called whether the guard exists or not. I wrote that test
    // first and it passed for exactly that reason.
    //
    // The situation that DOES reach the handler is the real one: the button is
    // enabled at render because idempotency looked supported, the user sits on the
    // reviewed plan, and support stops holding before they click. That is modelled
    // with `verifyBeforeSubmit` — the last-moment re-check that exists because a
    // render-time answer can be minutes stale by the time money is spent.
    restore = withServed(['previewWorkspace', 'createWorkspace']);
    server.use(http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)));
    const created = vi.fn();
    server.use(
      http.post(API('/workspaces'), () => {
        created();
        return HttpResponse.json({ id: 'ws-1', name: 'research', status: 'Provisioning' });
      }),
    );
    const user = userEvent.setup();
    renderFlow({
      idempotencySupport: null,
      verifyBeforeSubmit: () => ({ reason: 'not-deployed', detail: 'withdrawn' }),
    });
    await reachReview(user);

    const button = screen.getByRole('button', { name: /create this workspace/i });
    expect(button).toBeEnabled();
    await user.click(button);

    await waitFor(() =>
      expect(screen.getByText(/cannot be submitted in this environment/i)).toBeInTheDocument(),
    );
    expect(created).not.toHaveBeenCalled();
  });
});

describe('AC-02: the reviewed plan revision binds the submission', () => {
  it('sends the exact revision the user reviewed', async () => {
    restore = withServed(['previewWorkspace', 'createWorkspace']);
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)),
      http.post(API('/workspaces'), async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: 'ws-1', name: 'research', status: 'Provisioning' });
      }),
    );
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.plan_revision).toBe('rev-7');
  });

  it('invalidates the reviewed plan when inputs change afterwards', async () => {
    // The confirmation the user is about to give must describe what would actually
    // be built. An edit after review means it no longer does.
    restore = withServed(['previewWorkspace']);
    server.use(http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)));
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);

    await user.type(screen.getByLabelText('Target account'), '999988887777');

    expect(screen.queryByRole('button', { name: /create this workspace/i })).toBeNull();
    expect(screen.getByRole('button', { name: /review plan/i })).toBeInTheDocument();
  });

  it('marks the plan stale when inputs change while the plan request is in flight', async () => {
    // The race the simple "reset on edit" path does not cover. `requestPlan`
    // captures the inputs as they were when it was called; if the user keeps typing
    // while it is outstanding, the arriving plan describes inputs that are already
    // gone. Rendering it as reviewable would invite a confirmation of something the
    // user has since changed — so it must be flagged rather than presented.
    restore = withServed(['previewWorkspace']);
    let releasePlan: (() => void) | null = null;
    let planRequested: () => void;
    const requested = new Promise<void>((resolve) => {
      planRequested = resolve;
    });
    server.use(
      http.post(API('/workspaces/preview'), async () => {
        await new Promise<void>((release) => {
          releasePlan = release;
          planRequested();
        });
        return HttpResponse.json(PLAN_WIRE);
      }),
    );
    const user = userEvent.setup();
    renderFlow();

    await user.type(screen.getByLabelText('Workspace name'), 'research');
    await user.click(screen.getByRole('button', { name: /review plan/i }));
    await requested;

    // Keep typing while the plan is still being produced.
    await user.type(screen.getByLabelText('Target account'), '999988887777');
    releasePlan!();

    expect(
      await screen.findByText(/inputs changed since this plan was produced/i),
    ).toBeInTheDocument();
    // And critically, it cannot be submitted in that state.
    expect(screen.queryByRole('button', { name: /create this workspace/i })).toBeNull();
  });

  it('carries the operation identity in the body, where the proxy cannot strip it', async () => {
    // The gateway proxy forwards only Authorization and Content-Type. An identity
    // sent as a header would vanish in transit and the create would silently
    // deduplicate nothing.
    restore = withServed(['previewWorkspace', 'createWorkspace']);
    let headers: Headers | null = null;
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)),
      http.post(API('/workspaces'), async ({ request }) => {
        headers = request.headers;
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: 'ws-1', name: 'research', status: 'Provisioning' });
      }),
    );
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body![OPERATION_ID_FIELD]).toBe('key-1');
    expect(headers!.get('Idempotency-Key')).toBeNull();
  });
});

describe('AC-02: one operation identity across repeated submit, refresh and timeout', () => {
  it('reuses the same identity when the same inputs are submitted twice', async () => {
    restore = withServed(['previewWorkspace', 'createWorkspace']);
    const sent: string[] = [];
    server.use(
      http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)),
      http.post(API('/workspaces'), async ({ request }) => {
        const body = (await request.json()) as Record<string, string>;
        sent.push(body[OPERATION_ID_FIELD]);
        return HttpResponse.json({ id: 'ws-1', name: 'research', status: 'Provisioning' });
      }),
    );
    const user = userEvent.setup();
    const { unmount } = renderFlow();
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));
    await waitFor(() => expect(sent).toHaveLength(1));

    // A reload: the component remounts against the same persisted store, which is
    // what the browser actually does. The identity must survive it.
    unmount();
    renderFlow();
    const user2 = userEvent.setup();
    await reachReview(user2);
    await user2.click(screen.getByRole('button', { name: /create this workspace/i }));

    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[0]).toBe(sent[1]);
  });

  it('records a lost reply as unknown, never as failed', async () => {
    // The single most consequential assertion in this file. "Failed" invites a
    // resubmission; the operation it would duplicate may have succeeded.
    restore = withServed(['previewWorkspace', 'createWorkspace']);
    server.use(
      http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)),
      http.post(API('/workspaces'), () => HttpResponse.json({}, { status: 504 })),
    );
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));

    expect(await screen.findByText(/outcome not confirmed/i)).toBeInTheDocument();
    const receipt = readReceipt(store, SCOPE, CREATE_INTENT);
    expect(receipt!.state).toBe('unknown');
    expect(receipt!.state).not.toBe('failed');
  });

  it('warns on return that an earlier outcome is unknown', async () => {
    // A user coming back to this screen must see the unresolved submission before
    // they start typing a new one.
    restore = withServed(['previewWorkspace']);
    server.use(http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)));
    store.setItem(
      `adp.superplane.onboarding.receipt.dev.org-a.${CREATE_INTENT}`,
      JSON.stringify({
        idempotencyKey: 'key-earlier',
        operationId: null,
        fingerprint: 'abc',
        scope: SCOPE,
        createdAt: '2026-09-22T00:00:00Z',
        state: 'unknown',
        workspaceId: null,
      }),
    );
    renderFlow();

    expect(
      await screen.findByText(/earlier submission's outcome is unknown/i),
    ).toBeInTheDocument();
  });

  it('retries a lost submission under the SAME identity after re-reviewing', async () => {
    // The recovery path that a naive design breaks. Re-reviewing yields a fresh
    // plan revision; if the revision were part of the intent fingerprint, this
    // retry would be read as a changed payload and refused as a conflict — leaving
    // the user unable to retry the one thing the identity exists to make safe.
    restore = withServed(['previewWorkspace', 'createWorkspace']);
    const sent: string[] = [];
    let revision = 'rev-7';
    server.use(
      http.post(API('/workspaces/preview'), () =>
        HttpResponse.json({ ...PLAN_WIRE, revision }),
      ),
      http.post(API('/workspaces'), async ({ request }) => {
        const body = (await request.json()) as Record<string, string>;
        sent.push(body[OPERATION_ID_FIELD]);
        if (sent.length === 1) return HttpResponse.json({}, { status: 504 });
        return HttpResponse.json({ id: 'ws-1', name: 'research', status: 'Provisioning' });
      }),
    );
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));
    await screen.findByText(/outcome not confirmed/i);

    // The server has moved on to a new plan revision.
    revision = 'rev-8';
    await user.click(screen.getByRole('button', { name: /review plan/i }));
    await screen.findByRole('group', { name: /review this plan/i });
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));

    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[0]).toBe(sent[1]);
    expect(screen.queryByText(/different submission is already in progress/i)).toBeNull();
  });

  it('refuses a changed payload while an earlier submission is unresolved', async () => {
    // The mirror image: genuinely different inputs under a live intent is a
    // conflict, and resolving it by guessing discards one of the two requests.
    restore = withServed(['previewWorkspace', 'createWorkspace']);
    server.use(
      http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)),
      http.post(API('/workspaces'), () => HttpResponse.json({}, { status: 504 })),
    );
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));
    await screen.findByText(/outcome not confirmed/i);

    await user.type(screen.getByLabelText('Workspace name'), '-changed');
    await user.click(screen.getByRole('button', { name: /review plan/i }));

    // Scoped to the alert's title: the phrase also appears in the conflict body
    // text, so an unscoped query matches two nodes and throws on ambiguity.
    expect(
      await screen.findByText('A different submission is already in progress'),
    ).toBeInTheDocument();
  });

  it('keeps no secret-shaped material in the persisted receipt', async () => {
    restore = withServed(['previewWorkspace', 'createWorkspace']);
    server.use(
      http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)),
      http.post(API('/workspaces'), () =>
        HttpResponse.json({ id: 'ws-1', name: 'research', status: 'Provisioning' }),
      ),
    );
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));
    // `Provisioning`, so the heading is the accepted-and-building one. Matching
    // "creation accepted" here would have passed while the UI claimed success for
    // a workspace the server had not finished building.
    await screen.findByText(/still being provisioned/i);

    const raw = store.getItem(`adp.superplane.onboarding.receipt.dev.org-a.${CREATE_INTENT}`);
    expect(raw).not.toMatch(/secret|password|token|AKIA/i);
  });
});

describe('the isolation mode and guardrails the schema actually accepts (AC-01)', () => {
  /**
   * WHAT THE REVIEWED HEAD DID
   * --------------------------
   * `isolationMode` was hardcoded to `'shared'` and never offered. The schema is
   * `Field(default="dedicated", pattern="^(dedicated|namespace|research)$")`, so
   * `'shared'` is a 422 — every submission from this form was invalid on a
   * dimension the user could not see or change, and the one field that decides how
   * their workspace is isolated from other tenants was decided for them, wrongly.
   *
   * These tests assert against the server's own constraint, including by driving a
   * real request through MSW and reading the submitted body, so an option that the
   * validator would reject cannot pass here.
   */

  it('offers exactly the modes the schema accepts when the server has not said', async () => {
    renderFlow();
    const select = screen.getByLabelText('Isolation mode');
    const offered = Array.from(select.querySelectorAll('option')).map((o) => o.getAttribute('value'));
    expect(offered).toEqual(['dedicated', 'namespace', 'research']);
    // And never the value that was hardcoded, which the pattern rejects.
    expect(offered).not.toContain('shared');
  });

  it('defaults to the schema default rather than an invented value', async () => {
    renderFlow();
    expect(screen.getByLabelText('Isolation mode')).toHaveValue('dedicated');
  });

  it('narrows to the modes a deployment advertises', async () => {
    renderFlow({
      capabilities: {
        features: [],
        modes: ['managed'],
        providers: [],
        isolationModes: ['namespace'],
      },
    });
    const offered = Array.from(
      screen.getByLabelText('Isolation mode').querySelectorAll('option'),
    ).map((o) => o.getAttribute('value'));
    expect(offered).toEqual(['namespace']);
  });

  it('ignores an advertised mode the schema would reject', async () => {
    // A deployment can advertise anything. It cannot make the validator accept it,
    // so offering it would produce a 422 the user cannot act on.
    renderFlow({
      capabilities: {
        features: [],
        modes: ['managed'],
        providers: [],
        isolationModes: ['shared', 'research'],
      },
    });
    const offered = Array.from(
      screen.getByLabelText('Isolation mode').querySelectorAll('option'),
    ).map((o) => o.getAttribute('value'));
    expect(offered).toEqual(['research']);
  });

  it('refuses research isolation with no account, stating the server rule', async () => {
    // `research_requires_account` raises this server-side. Said here so the user
    // is told before submitting rather than decoding a 422 afterwards.
    const user = userEvent.setup();
    renderFlow();
    await user.type(screen.getByLabelText('Workspace name'), 'research');
    await user.selectOptions(screen.getByLabelText('Isolation mode'), 'research');

    expect(await screen.findByText(/requires a target cloud account/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /review plan/i })).toBeDisabled();
  });

  it('accepts research isolation once an account is given', async () => {
    const user = userEvent.setup();
    renderFlow();
    await user.type(screen.getByLabelText('Workspace name'), 'research');
    await user.selectOptions(screen.getByLabelText('Isolation mode'), 'research');
    // Matched loosely because the field is marked required for research isolation,
    // and `Input` renders that as a `*` inside the label — the accessible name
    // genuinely changes, and asserting the exact old string would be asserting
    // that the requirement is NOT communicated.
    await user.type(screen.getByLabelText(/Target account/), '111122223333');

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /review plan/i })).toBeEnabled(),
    );
  });

  it('submits the chosen mode, region and guardrails in the body', async () => {
    restore = withServed(['previewWorkspace', 'createWorkspace']);
    const bodies: Array<Record<string, unknown>> = [];
    server.use(
      http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)),
      http.post(API('/workspaces'), async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({ id: 'ws-1', name: 'research', status: 'Provisioning' });
      }),
    );
    const user = userEvent.setup();
    renderFlow();
    await user.type(screen.getByLabelText('Workspace name'), 'research');
    await user.selectOptions(screen.getByLabelText('Isolation mode'), 'namespace');
    await user.type(screen.getByLabelText('Region'), 'eu-west-2');
    await user.type(screen.getByLabelText('Daily spend cap (USD)'), '25');
    await user.type(screen.getByLabelText('Maximum GPUs'), '4');
    await user.click(screen.getByRole('button', { name: /review plan/i }));
    await screen.findByRole('group', { name: /review this plan/i });
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));
    await screen.findByText(/still being provisioned/i);

    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toMatchObject({
      isolation_mode: 'namespace',
      region: 'eu-west-2',
      budget_max_daily_usd: 25,
      budget_max_gpus: 4,
    });
  });

  it('sends an untouched guardrail as null rather than as zero', async () => {
    // A zero cap is a real instruction that stops the workspace doing anything.
    // Blank means "no opinion", and the domain then applies its own research
    // default — which a client-invented zero would silently override.
    restore = withServed(['previewWorkspace', 'createWorkspace']);
    const bodies: Array<Record<string, unknown>> = [];
    server.use(
      http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)),
      http.post(API('/workspaces'), async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({ id: 'ws-1', name: 'research', status: 'Provisioning' });
      }),
    );
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));
    await screen.findByText(/still being provisioned/i);

    expect(bodies[0].budget_max_daily_usd).toBeNull();
    expect(bodies[0].budget_max_gpus).toBeNull();
  });
});

describe('AC-02: a 201 is acceptance, not completion', () => {
  /**
   * WHY THIS IS A DEFECT AND NOT A WORDING PREFERENCE
   * ------------------------------------------------
   * `POST /workspaces` returns 201 with `status: "Provisioning"` and builds the
   * cluster afterwards. The flow recorded `succeeded` for any readable reply, which
   * is terminal — and terminal is load-bearing: `claimIdentity` lets a CHANGED
   * payload mint a fresh identity once the stored receipt is terminal. So the wrong
   * state both told the user their workspace was ready and re-opened the duplicate
   * path the identity exists to close.
   */

  function respondWith(status: string | undefined) {
    restore = withServed(['previewWorkspace', 'createWorkspace']);
    server.use(
      http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)),
      http.post(API('/workspaces'), () =>
        HttpResponse.json(
          status === undefined
            ? { id: 'ws-1', name: 'research' }
            : { id: 'ws-1', name: 'research', status },
        ),
      ),
    );
  }

  async function submit() {
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));
    return user;
  }

  it('records a Provisioning reply as running, not succeeded', async () => {
    respondWith('Provisioning');
    await submit();
    await screen.findByText(/still being provisioned/i);
    expect(readReceipt(store, SCOPE, CREATE_INTENT)?.state).toBe('running');
  });

  it('says it is not ready yet, and that provisioning can still fail', async () => {
    respondWith('Provisioning');
    await submit();
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent(/not ready yet/i);
    expect(status).toHaveTextContent(/can still fail/i);
  });

  it('records an Active reply as succeeded', async () => {
    // The other half. Without this the change would be indistinguishable from
    // never reporting success, which is its own defect: an operation that can
    // never settle can never be superseded by a new intent.
    respondWith('Active');
    await submit();
    expect(await screen.findByText('Operation completed')).toBeInTheDocument();
    expect(screen.queryByText(/workspace ready/i)).not.toBeInTheDocument();
    expect(readReceipt(store, SCOPE, CREATE_INTENT)?.state).toBe('succeeded');
  });

  it('records a Failed reply as failed and says so', async () => {
    respondWith('Failed');
    await submit();
    expect(await screen.findByText(/reported this failed/i)).toBeInTheDocument();
    expect(readReceipt(store, SCOPE, CREATE_INTENT)?.state).toBe('failed');
  });

  it('does not assume a reply with no status is finished', async () => {
    respondWith(undefined);
    await submit();
    await screen.findByRole('status');
    const state = readReceipt(store, SCOPE, CREATE_INTENT)?.state;
    expect(state === 'succeeded' || state === 'failed').toBe(false);
  });

  it('does not assume an unrecognised status is finished', async () => {
    // The domain's vocabulary is inconsistent already — `Provisioning`/`Teardown`
    // from the router, `pending`/`bootstrapping`/`drift_detected` from the model —
    // and it will grow. Reading anything unfamiliar as success is wrong by default
    // on every future addition.
    respondWith('reconciling');
    await submit();
    await screen.findByRole('status');
    const state = readReceipt(store, SCOPE, CREATE_INTENT)?.state;
    expect(state === 'succeeded' || state === 'failed').toBe(false);
  });

  it('still refuses changed inputs while the first create is provisioning', async () => {
    // The consequence, demonstrated rather than argued: with `Provisioning`
    // recorded as terminal, this submits a SECOND workspace while the first is
    // still being built.
    respondWith('Provisioning');
    const user = await submit();
    await screen.findByText(/still being provisioned/i);

    await user.type(screen.getByLabelText('Workspace name'), '-changed');
    await user.click(screen.getByRole('button', { name: /review plan/i }));

    expect(
      await screen.findByText('A different submission is already in progress'),
    ).toBeInTheDocument();
  });
});

describe('the payload builder, shared with the CLI (AC-07)', () => {
  const inputs: OnboardingInputs = {
    mode: 'managed',
    name: 'research',
    isolationMode: 'dedicated',
    account: '111122223333',
    region: 'us-east-1',
  };

  it('binds the revision and the operation identity into the body', () => {
    const body = buildCreatePayload(inputs, PLAN, 'op-1');
    expect(body.plan_revision).toBe('rev-7');
    expect(body[OPERATION_ID_FIELD]).toBe('op-1');
  });

  it('is pure, so the UI and the CLI produce byte-identical bodies', () => {
    // If the two clients built payloads separately they would fingerprint
    // differently, and the same intent retried from the CLI after a UI failure
    // would be read as a new intent — defeating the guarantee exactly when a user
    // reaches for the CLI because the UI broke.
    expect(JSON.stringify(buildCreatePayload(inputs, PLAN, 'op-1'))).toBe(
      JSON.stringify(buildCreatePayload(inputs, PLAN, 'op-1')),
    );
  });

  it('omits absent optional values as null rather than dropping the keys', () => {
    const body = buildCreatePayload(
      // `namespace`, not `'shared'`: the schema's pattern is
      // `^(dedicated|namespace|research)$`, and a fixture using a value the server
      // rejects proves nothing about a payload the server would accept.
      { mode: 'managed', name: 'n', isolationMode: 'namespace' },
      PLAN,
      'op-1',
    );
    expect(body.budget_max_daily_usd).toBeNull();
    expect('budget_max_daily_usd' in body).toBe(true);
  });
});

describe('AC-05: the plan is reviewable and accessible', () => {
  it('reports an absent cost estimate as unestimated, never as zero', async () => {
    // "$0.00" is a claim about price. An absent estimate is the absence of one, and
    // telling a user their GPU workspace costs nothing is a falsehood about money.
    restore = withServed(['previewWorkspace']);
    server.use(http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)));
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);

    expect(screen.getByText(/not estimated by the server/i)).toBeInTheDocument();
    expect(screen.queryByText(/\$?0(\.00)?\s*(USD)?$/)).toBeNull();
  });

  it('surfaces an approval requirement in the reviewed plan', async () => {
    restore = withServed(['previewWorkspace']);
    server.use(
      http.post(API('/workspaces/preview'), () =>
        HttpResponse.json({ ...PLAN_WIRE, approval_required: true }),
      ),
    );
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);

    expect(screen.getByText(/approval required/i)).toBeInTheDocument();
  });

  it('labels every input and names the create region', async () => {
    renderFlow();
    expect(screen.getByLabelText('Workspace name')).toBeInTheDocument();
    expect(screen.getByLabelText('Target account')).toBeInTheDocument();
    expect(
      screen.getByRole('region', { name: 'Create a workspace' }),
    ).toBeInTheDocument();
  });

  it('announces the receipt to assistive technology', async () => {
    restore = withServed(['previewWorkspace', 'createWorkspace']);
    server.use(
      http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)),
      http.post(API('/workspaces'), () =>
        HttpResponse.json({ id: 'ws-1', name: 'research', status: 'Provisioning' }),
      ),
    );
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));

    expect(await screen.findByRole('status')).toHaveTextContent(/still being provisioned/i);
  });

  it('requires a name before a plan can be requested', async () => {
    renderFlow();
    expect(screen.getByRole('button', { name: /review plan/i })).toBeDisabled();
  });
});


describe('durable operation recovery and adoption', () => {
  it('looks up the real provisioning ID and records its current state', async () => {
    restore = withServed(['previewWorkspace', 'createWorkspace', 'getOperation']);
    server.use(
      http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)),
      http.post(API('/workspaces'), () => HttpResponse.json({
        id: 'ws-1', name: 'research', status: 'Provisioning',
        provisioning_operation_id: 'harness-op-9', operation_state: 'running',
      })),
      http.get(API('/operations/harness-op-9'), () => HttpResponse.json({
        request_id: 'key-1', provisioning_operation_id: 'harness-op-9',
        workspace_id: 'ws-1', state: 'succeeded',
      })),
    );
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));
    await waitFor(() => expect(readReceipt(store, SCOPE, CREATE_INTENT)).toMatchObject({
      idempotencyKey: 'key-1', operationId: 'harness-op-9', state: 'succeeded',
    }));
  });

  it('recovers a lost reply by request identity without inventing a server operation', async () => {
    restore = withServed(['previewWorkspace', 'createWorkspace', 'recoverOperation']);
    server.use(
      http.post(API('/workspaces/preview'), () => HttpResponse.json(PLAN_WIRE)),
      http.post(API('/workspaces'), () => HttpResponse.json({}, { status: 504 })),
      http.get(API('/operations/by-idempotency/key-1'), () => HttpResponse.json({
        request_id: 'key-1', provisioning_operation_id: null,
        workspace_id: 'ws-1', state: 'accepted',
      })),
    );
    const user = userEvent.setup();
    renderFlow();
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /create this workspace/i }));
    await waitFor(() => expect(readReceipt(store, SCOPE, CREATE_INTENT)).toMatchObject({
      idempotencyKey: 'key-1', operationId: null, state: 'accepted', workspaceId: 'ws-1',
    }));
  });

  it('sends the reviewed adoption target and revision to the adoption route', async () => {
    restore = withServed(['previewWorkspace', 'adoptWorkspace']);
    let preview: unknown;
    let submitted: unknown;
    server.use(
      http.post(API('/workspaces/preview'), async ({ request }) => {
        preview = await request.json();
        return HttpResponse.json({ ...PLAN_WIRE, mode: 'adopt', target: { ...PLAN_WIRE.target, cluster: 'existing-cluster' } });
      }),
      http.post(API('/workspaces/adopt'), async ({ request }) => {
        submitted = await request.json();
        return HttpResponse.json({ id: 'ws-1', name: 'research', status: 'Provisioning', provisioning_operation_id: 'adopt-op-9' });
      }),
    );
    const user = userEvent.setup();
    renderFlow({ capabilities: { modes: ['managed', 'adopt'], features: [], providers: [], isolationModes: ['dedicated'] } });
    await user.selectOptions(screen.getByLabelText('Workspace source'), 'adopt');
    await user.type(screen.getByLabelText(/Cluster reference/), 'existing-cluster');
    await reachReview(user);
    await user.click(screen.getByRole('button', { name: /adopt this cluster/i }));
    await waitFor(() => expect(submitted).toMatchObject({
      mode: 'adopt', cluster_reference: 'existing-cluster', plan_revision: 'rev-7', operation_id: 'key-1',
    }));
    expect(preview).toMatchObject({ mode: 'adopt', cluster_reference: 'existing-cluster' });
  });
});


it('keeps one identity through preview, approval and submission', async () => {
  restore = withServed(['previewWorkspace', 'createWorkspace', 'requestApproval', 'getApproval']);
  let previewId: string | undefined;
  let submitted: Record<string, unknown> | undefined;
  const ticket = { approval_id: 'approval-9', workspace_id: 'ws-1', result: 'pending', can_decide: false,
    request: { action: 'provision', parameters: {} }, envelope: {}, expires_at: '2999-01-01T00:00:00Z', revoked: false };
  let allowed = false;
  server.use(
    http.post(API('/workspaces/preview'), async ({ request }) => {
      const body = await request.json() as Record<string, string>;
      previewId = body.operation_id;
      return HttpResponse.json({ ...PLAN_WIRE, approval_required: true,
        approval_request: { workspace_id: 'ws-1', action: 'provision', idempotency_key: body.operation_id, parameters: { plan_revision: 'rev-7' } } });
    }),
    http.post(API('/operation-approvals'), async ({ request }) => {
      expect(await request.json()).toEqual({ workspace_id: 'ws-1', action: 'provision', idempotency_key: previewId, parameters: { plan_revision: 'rev-7' } });
      return HttpResponse.json(ticket);
    }),
    http.get(API('/operation-approvals/approval-9'), () => HttpResponse.json({ ...ticket, result: allowed ? 'allowed-once' : 'pending' })),
    http.post(API('/workspaces'), async ({ request }) => {
      submitted = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ id: 'ws-1', name: 'research', status: 'Provisioning', provisioning_operation_id: 'harness-9' });
    }),
  );
  const user = userEvent.setup();
  renderFlow();
  await reachReview(user);
  expect(screen.getByRole('button', { name: /create this workspace/i })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: /request approval for this plan/i }));
  await screen.findByText(/approval-9/);
  expect(readReceipt(store, SCOPE, CREATE_INTENT)).toMatchObject({ idempotencyKey: previewId, approvalId: 'approval-9', submissionStage: 'approval' });
  allowed = true;
  await user.click(screen.getByRole('button', { name: /refresh approval/i }));
  await waitFor(() => expect(screen.getByRole('button', { name: /create this workspace/i })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: /create this workspace/i }));
  await waitFor(() => expect(submitted).toMatchObject({ operation_id: previewId, approval_id: 'approval-9', plan_revision: 'rev-7' }));
});
