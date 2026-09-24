/**
 * The onboarding client at the real HTTP boundary — #5730 AC-03/AC-09.
 *
 * These tests go through MSW rather than mocking `apiClient`, because the things
 * being asserted live *at* that boundary: which status code means what, whether a
 * malformed body is rejected, and whether a reply that arrives after an
 * organization switch reaches the screen. Mocking the client away would assert
 * only that my own function calls my own function.
 *
 * Handlers are registered per test with `server.use()` instead of added to the
 * global handler set, so no existing suite changes behaviour. Note that setup.ts
 * runs MSW with `onUnhandledRequest: 'error'` — any request this client makes that
 * a test did not anticipate fails loudly, which is what makes the "never sends an
 * unserved endpoint" assertions meaningful.
 */

import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { server } from '@/mocks/server';
import {
  ScopeGuard,
  adoptWorkspace,
  buildBindBody,
  classify,
  createWorkspace,
  getCapabilities,
  getConnection,
  getOperation,
  getWorkspace,
  isSuperseded,
  listWorkspaces,
  parseCapabilities,
  parseConnection,
  parseCredentialRef,
  listCredentials,
  parseOperationReceipt,
  parsePlan,
  parseValidation,
  parseWorkspaceList,
  previewWorkspace,
  recoverOperation,
  registerConnection,
  revokeConnection,
  validateConnection,
} from '@superplane-ui/client';
import { DOMAIN_BASE, ENDPOINTS } from '@superplane-ui/contract';

import { connectionResponse, expectedBindBody, vaultCredentialRow, validationResponse } from './vault-fixtures';
import { withoutOnboardingEndpoints } from './endpoint-fixtures';

/** `apiClient` prepends `/api`, so MSW must match that full path. */
const API = (path: string) => `/api${DOMAIN_BASE}${path}`;

const WORKSPACE = {
  id: 'ws-1',
  org_id: 'org-1',
  name: 'research',
  display_name: 'Research',
  isolation_mode: 'dedicated',
  status: 'Active',
  cluster_health: 'Healthy',
  last_heartbeat: '2026-09-23T12:00:00.000Z',
  created_at: '2026-09-23T11:00:00.000Z',
  updated_at: '2026-09-23T11:30:00.000Z',
};

let guard: ScopeGuard;

beforeEach(() => {
  guard = new ScopeGuard();
  // A token must be present or `apiClient` sends no Authorization header; the
  // proxy requires a bearer token, so an unauthenticated test would not be
  // exercising the real path.
  window.sessionStorage.setItem('cognito_access_token', 'test-token');
});

describe('unserved endpoints are never requested', () => {
  let restoreDeployment: () => void;
  beforeEach(() => { restoreDeployment = withoutOnboardingEndpoints(); });
  afterEach(() => { restoreDeployment(); });
  /**
   * Each unserved operation, with the endpoint name it must report.
   *
   * No MSW handler is registered for any of them. Because setup.ts runs MSW with
   * `onUnhandledRequest: 'error'`, a request reaching the network fails the test
   * outright — so "did not send it" is genuinely asserted, not assumed. The
   * `fetch` spy makes the failure legible when it happens.
   */
  const unserved: [string, (g: ScopeGuard) => Promise<unknown>, string][] = [
    ['previewWorkspace', (g) => previewWorkspace(g, { name: 'x' }), 'previewWorkspace'],
    ['adoptWorkspace', (g) => adoptWorkspace(g, { cluster: 'c' }), 'adoptWorkspace'],
    ['getOperation', (g) => getOperation(g, 'op-1'), 'getOperation'],
    ['recoverOperation', (g) => recoverOperation(g, 'key-1'), 'recoverOperation'],
  ];

  for (const [label, invoke, endpoint] of unserved) {
    it(`${label} reports not-deployed without issuing a request`, async () => {
      const spy = vi.spyOn(globalThis, 'fetch');

      const outcome = (await invoke(guard)) as Awaited<ReturnType<typeof getCapabilities>>;

      expect(spy).not.toHaveBeenCalled();
      expect(outcome.ok).toBe(false);
      if (!outcome.ok && 'unavailable' in outcome) {
        expect(outcome.unavailable.reason).toBe('not-deployed');
        expect(outcome.unavailable.endpoint).toBe(endpoint);
        expect(outcome.unavailable.capability).toBe(ENDPOINTS[endpoint].capability);
        expect(outcome.unavailable.detail).not.toMatch(/#\d+/);
      }
      spy.mockRestore();
    });
  }

  it('explains which method and path is missing so a deployer can check', async () => {
    // "Not available" with no specifics sends the reader to guess. The detail
    // names the exact route to look for in the proxy allowlist.
    const outcome = await previewWorkspace(guard, { name: 'x' });
    if (!outcome.ok && 'unavailable' in outcome) {
      expect(outcome.unavailable.detail).toContain('POST');
      expect(outcome.unavailable.detail).toContain('/superplane/v1/workspaces/preview');
    }
  });
});

describe('successful reads', () => {
  it('returns a parsed workspace list', async () => {
    server.use(
      http.get(API('/workspaces'), () =>
        HttpResponse.json({ workspaces: [WORKSPACE], total: 1 }),
      ),
    );

    const outcome = await listWorkspaces(guard);
    expect(outcome.ok).toBe(true);
    if (outcome.ok) {
      expect(outcome.value.total).toBe(1);
      expect(outcome.value.workspaces[0].id).toBe('ws-1');
    }
  });

  it('sends the bearer token the proxy requires', async () => {
    let seen: string | null = null;
    server.use(
      http.get(API('/workspaces'), ({ request }) => {
        seen = request.headers.get('authorization');
        return HttpResponse.json({ workspaces: [], total: 0 });
      }),
    );

    await listWorkspaces(guard);
    expect(seen).toBe('Bearer test-token');
  });

  it('reports an empty control plane as a successful empty list, not an error', async () => {
    // AC-01 is about onboarding "from an installed control plane with zero
    // workspaces". Zero workspaces is the expected starting state, and rendering
    // it as a failure would make a correct installation look broken.
    server.use(
      http.get(API('/workspaces'), () => HttpResponse.json({ workspaces: [], total: 0 })),
    );

    const outcome = await listWorkspaces(guard);
    expect(outcome.ok).toBe(true);
    if (outcome.ok) expect(outcome.value.workspaces).toEqual([]);
  });

  it('requests a single workspace by id at the declared path', async () => {
    server.use(http.get(API('/workspaces/ws-1'), () => HttpResponse.json(WORKSPACE)));
    const outcome = await getWorkspace(guard, 'ws-1');
    expect(outcome.ok).toBe(true);
  });
});

describe('failure classification', () => {
  /** Each status maps to the reason that implies the right user action. */
  const cases: [number, string, RegExp][] = [
    [403, 'not-permitted', /permission/i],
    [503, 'unreachable', /unavailable|temporary/i],
    [502, 'unreachable', /unusable|returned/i],
  ];

  for (const [status, reason, detail] of cases) {
    it(`maps HTTP ${status} to ${reason} with actionable guidance`, async () => {
      // An empty body on purpose: this asserts the *fallback* guidance, which is
      // what a user sees when the server explains nothing. The separate test
      // below covers a server that does supply a message.
      server.use(
        http.get(API('/workspaces'), () => HttpResponse.json({}, { status })),
      );

      const outcome = await listWorkspaces(guard);
      expect(outcome.ok).toBe(false);
      if (!outcome.ok && 'unavailable' in outcome) {
        expect(outcome.unavailable.reason).toBe(reason);
        expect(outcome.unavailable.detail).toMatch(detail);
      }
    });
  }

  it('distinguishes a 404 on a served route from one on an unserved route', async () => {
    // The gateway proxy 404s an unallowlisted route and the domain API 404s a
    // missing workspace — identical on the wire. The endpoint's own `served` flag
    // is the only thing that separates "not deployed here" from "does not exist",
    // and they need different things from the user.
    const missingResource = classify({ status: 404, message: '' }, 'getWorkspace');
    expect(missingResource.reason).toBe('unknown');
    expect(missingResource.detail).toMatch(/does not exist/i);

    const restoreDeployment = withoutOnboardingEndpoints();
    try {
      const notDeployed = classify({ status: 404, message: '' }, 'previewWorkspace');
      expect(notDeployed.reason).toBe('not-deployed');
      expect(notDeployed.capability).toBe(ENDPOINTS.previewWorkspace.capability);
      expect(notDeployed.detail).not.toMatch(/#\d+/);
    } finally {
      restoreDeployment();
    }
  });

  it('treats a missing status as unreachable rather than as an unknown server refusal', async () => {
    // A thrown value with no status did not come from a completed HTTP response.
    // Claiming the server refused something would be inventing information.
    const outcome = classify(new TypeError('network down'), 'listWorkspaces');
    expect(outcome.reason).toBe('unreachable');
  });

  it('prefers the server message over the generic text when one is given', async () => {
    server.use(
      http.get(API('/workspaces'), () =>
        HttpResponse.json(
          { error: 'forbidden', message: 'Your role cannot list workspaces.' },
          { status: 403 },
        ),
      ),
    );

    const outcome = await listWorkspaces(guard);
    if (!outcome.ok && 'unavailable' in outcome) {
      expect(outcome.unavailable.detail).toBe('Your role cannot list workspaces.');
    }
  });

  it('reports an expired session as not-permitted with a re-authentication hint', async () => {
    // AC-05 covers session expiry during onboarding. `apiClient` redirects to
    // /login on 401; the outcome is still reported so a caller mid-submission
    // stops rather than continuing against a dead session.
    server.use(
      http.get(API('/workspaces'), () =>
        HttpResponse.json({ error: 'unauthorized', message: '' }, { status: 401 }),
      ),
    );

    const outcome = await listWorkspaces(guard);
    expect(outcome.ok).toBe(false);
    if (!outcome.ok && 'unavailable' in outcome) {
      expect(outcome.unavailable.reason).toBe('not-permitted');
      expect(outcome.unavailable.detail).toMatch(/session has expired/i);
    }
  });
});

describe('late responses after an organization switch', () => {
  it('discards a reply whose scope generation is no longer current', async () => {
    // The AC-03 requirement that is easiest to get wrong. The switch happens
    // while the request is in flight, so the reply arrives for a tenant the user
    // has left. Delivering it would render one organization's workspaces under
    // another organization's name.
    let release: (() => void) | null = null;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });

    server.use(
      http.get(API('/workspaces'), async () => {
        await blocked;
        return HttpResponse.json({ workspaces: [WORKSPACE], total: 1 });
      }),
    );

    const pending = listWorkspaces(guard);
    guard.supersede(); // the user switches organization
    release?.();

    const outcome = await pending;
    expect(isSuperseded(outcome)).toBe(true);
    expect(outcome.ok).toBe(false);
  });

  it('reports a superseded scope distinctly from a failure', async () => {
    // Nothing went wrong, so this must not become an error banner on the new
    // tenant's screen. The two are different outcomes with different renderings.
    let release: (() => void) | null = null;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(API('/workspaces'), async () => {
        await blocked;
        return HttpResponse.json({ workspaces: [], total: 0 });
      }),
    );

    const pending = listWorkspaces(guard);
    guard.supersede();
    release?.();

    const outcome = await pending;
    expect(outcome.ok).toBe(false);
    expect('unavailable' in outcome).toBe(false);
    expect(isSuperseded(outcome)).toBe(true);
  });

  it('discards a successful reply that abort could no longer stop', async () => {
    // THE CASE ABORT CANNOT COVER, and the reason the generation counter exists.
    //
    // The two tests above pass even without a generation check, because aborting
    // an in-flight request makes it reject and the rejection is reported as
    // superseded. But a response that has already been received — sitting in the
    // microtask queue when the switch happens — is past the point where abort has
    // any effect. It resolves *successfully*, and the generation check is the only
    // thing standing between it and a component now showing a different tenant.
    //
    // Modelled with a guard whose abort deliberately does nothing, which is
    // precisely what a too-late abort amounts to. Deterministic: no timing race.
    const tooLate = new ScopeGuard();
    const lateGuard = {
      current: () => 0,
      // The switch has happened: generation 0 is no longer current.
      isCurrent: () => false,
      signal: () => ({ signal: new AbortController().signal, done: () => {} }),
      supersede: () => {},
    } as unknown as ScopeGuard;
    expect(tooLate.isCurrent(0)).toBe(true); // control: an untouched guard is current

    server.use(
      http.get(API('/workspaces'), () => HttpResponse.json({ workspaces: [WORKSPACE], total: 1 })),
    );

    const outcome = await listWorkspaces(lateGuard);

    expect(outcome.ok).toBe(false);
    expect(isSuperseded(outcome)).toBe(true);
  });

  it('aborts outstanding requests on supersede', async () => {
    // Abort alone is insufficient for the late-reply case above, but it is still
    // required: it stops work nobody will read.
    let aborted = false;
    server.use(
      http.get(API('/workspaces'), async ({ request }) => {
        request.signal.addEventListener('abort', () => {
          aborted = true;
        });
        await new Promise((resolve) => setTimeout(resolve, 50));
        return HttpResponse.json({ workspaces: [], total: 0 });
      }),
    );

    const pending = listWorkspaces(guard);
    await new Promise((resolve) => setTimeout(resolve, 5));
    guard.supersede();
    await pending;

    expect(aborted).toBe(true);
  });

  it('delivers replies normally while the scope is unchanged', async () => {
    // The guard must not be so eager that it discards ordinary traffic.
    server.use(
      http.get(API('/workspaces'), () => HttpResponse.json({ workspaces: [WORKSPACE], total: 1 })),
    );
    const outcome = await listWorkspaces(guard);
    expect(outcome.ok).toBe(true);
  });

  it('serves a fresh request after a supersede', async () => {
    // A switch must not permanently poison the guard: the new organization's
    // first request has to succeed.
    server.use(
      http.get(API('/workspaces'), () => HttpResponse.json({ workspaces: [], total: 0 })),
    );
    guard.supersede();
    const outcome = await listWorkspaces(guard);
    expect(outcome.ok).toBe(true);
  });
});

describe('malformed and older-server responses', () => {
  it('rejects a list response with no workspaces array', async () => {
    // Rendering `undefined.map` would crash the page; reporting a version
    // mismatch tells the operator what to check.
    server.use(http.get(API('/workspaces'), () => HttpResponse.json({ items: [] })));

    const outcome = await listWorkspaces(guard);
    expect(outcome.ok).toBe(false);
    if (!outcome.ok && 'unavailable' in outcome) {
      expect(outcome.unavailable.detail).toMatch(/does not understand|older or newer/i);
    }
  });

  it('drops an unreadable row but keeps the readable ones', async () => {
    // A list that shows four of five workspaces is more useful than an error,
    // and the discrepancy stays visible because `total` is reported separately.
    const parsed = parseWorkspaceList({
      workspaces: [WORKSPACE, { id: 'ws-2' }, null, 'nonsense'],
      total: 4,
    });
    expect(parsed?.workspaces).toHaveLength(1);
    expect(parsed?.total).toBe(4);
  });

  it('rejects a workspace row missing its identity', async () => {
    expect(parseWorkspaceList({ workspaces: [{ name: 'x', status: 'Active' }] })?.workspaces).toEqual([]);
  });

  it('normalises an absent cluster health to null rather than undefined', async () => {
    // `readiness.ts` treats `null` as "not observed". Leaving it `undefined`
    // would work by accident today and break the moment someone checks `in`.
    const parsed = parseWorkspaceList({
      workspaces: [{ id: 'a', name: 'a', status: 'Provisioning' }],
      total: 1,
    });
    expect(parsed?.workspaces[0].cluster_health).toBeNull();
    expect(parsed?.workspaces[0].last_heartbeat).toBeNull();
  });

  it('reads a missing validation reading as unknown, never as a pass', async () => {
    // An older server that omits a reading must not have the omission read as
    // satisfied — that is how an unvalidated credential reaches production.
    const parsed = parseValidation({ credential_valid: true, checked_at: 'x' });
    expect(parsed?.credential_valid).toBe(true);
    expect(parsed?.permissions_sufficient).toBeNull();
    expect(parsed?.quota_available).toBeNull();
  });

  it('reads a non-boolean validation reading as unknown', async () => {
    // A string "true" is not a boolean true. Coercing it would let a server bug
    // or a proxy transformation silently become a pass.
    const parsed = parseValidation({
      credential_valid: 'true',
      permissions_sufficient: 1,
      quota_available: null,
      checked_at: 'x',
    });
    expect(parsed?.credential_valid).toBeNull();
    expect(parsed?.permissions_sufficient).toBeNull();
  });

  it('defaults admits_new_work to false when the server does not report it', async () => {
    // Fail-closed. An older server that cannot say whether a connection admits
    // work must not have silence read as "yes".
    const parsed = parseConnection({ connection_id: 'c-1', provider: 'aws' });
    expect(parsed?.admits_new_work).toBe(false);
    expect(parsed?.allows_renewal).toBe(false);
  });

  it('rejects a connection with no identity', async () => {
    expect(parseConnection({ provider: 'aws' })).toBeNull();
    expect(parseConnection(null)).toBeNull();
    expect(parseConnection([])).toBeNull();
  });
});

describe('provider connections', () => {
  it('sends the whole reference the server requires, and never a secret value', async () => {
    // AC-03: onboarding never carries raw secret values. The raw secret is
    // entered into ADP's vault through its own surface; onboarding binds a
    // reference. Asserted on the actual request body.
    //
    // The body is built by `buildBindBody` from a real vault row rather than typed
    // out here. Typing it out was the original defect: this test asserted
    // `{provider, credential_id}` and passed, while `accept_connection_request`
    // requires `credential_id`, `service` AND `label` and 400s without them.
    const row = vaultCredentialRow();
    let body: unknown = null;
    server.use(
      http.post(API('/workspaces/ws-1/provider-connections'), async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(connectionResponse(row), { status: 201 });
      }),
    );

    const credential = parseCredentialRef({ ...row, label: row.name });
    expect(credential).not.toBeNull();
    await registerConnection(guard, 'ws-1', buildBindBody(credential!));

    expect(body).toEqual(expectedBindBody(row));
    // The reference id is the vault handle, not the registry row key. Sending `id`
    // is what made the server's registry lookup fail to find the credential.
    expect((body as { credential_id: string }).credential_id).toBe(row.adp_credential_id);
    expect(JSON.stringify(body)).not.toContain(row.id);
    const serialized = JSON.stringify(body);
    expect(serialized).not.toMatch(/secret|password|private[_-]?key|AKIA/i);
  });

  it('uses the canonical vault label even when the registry display name differs', async () => {
    const row = vaultCredentialRow({ name: 'Production display name' });
    server.use(
      http.get(API('/vault/credentials'), () => HttpResponse.json({ credentials: [row] })),
      http.get('/api/auth/credentials', () => HttpResponse.json([
        { id: row.adp_credential_id, service: row.provider, label: 'team-key', secret: 'must-drop' },
      ])),
    );
    expect(await listCredentials(guard)).toEqual({ ok: true, value: [
      { credential_id: row.adp_credential_id, service: row.provider, label: 'team-key' },
    ] });
  });

  it('submits the Gateway attested report instead of inventing a validation', async () => {
    const row = vaultCredentialRow();
    const report = validationResponse();
    let submitted: unknown;
    server.use(
      http.get(API('/workspaces/ws-1/provider-connections/c-1'), () => HttpResponse.json(connectionResponse(row))),
      http.post(`/api/auth/credentials/${row.adp_credential_id}/workspaces/ws-1/validation`, async ({request}) => {
        expect(await request.text()).toBe('');
        return HttpResponse.json({ credential_id: row.adp_credential_id, workspace_id: 'ws-1', validation: report });
      }),
      http.post(API('/workspaces/ws-1/provider-connections/c-1/validation'), async ({request}) => {
        submitted = await request.json();
        return HttpResponse.json(connectionResponse(row, { validation: report }));
      }),
    );
    expect((await validateConnection(guard, 'ws-1', 'c-1')).ok).toBe(true);
    expect(submitted).toEqual(report);
  });

  it('does not submit a report when Gateway cannot validate the provider', async () => {
    const row = vaultCredentialRow();
    server.use(
      http.get(API('/workspaces/ws-1/provider-connections/c-1'), () => HttpResponse.json(connectionResponse(row))),
      http.post(`/api/auth/credentials/${row.adp_credential_id}/workspaces/ws-1/validation`, () =>
        HttpResponse.json({ detail: 'provider validation unavailable' }, { status: 503 })),
    );
    const result = await validateConnection(guard, 'ws-1', 'c-1');
    expect(result.ok).toBe(false);
    if (!result.ok && 'unavailable' in result) expect(result.unavailable.reason).toBe('unreachable');
  });

  it('treats an empty body from a revoke as success', async () => {
    // A 204 is a successful revoke. Inferring success from response content
    // would report a spurious failure and invite a second revoke.
    server.use(
      http.delete(API('/workspaces/ws-1/provider-connections/c-1'), () =>
        new HttpResponse(null, { status: 204 }),
      ),
    );

    const outcome = await revokeConnection(guard, 'ws-1', 'c-1');
    expect(outcome.ok).toBe(true);
  });

  it('reports a revoked connection as not admitting work', async () => {
    // AC-03 requires revoked credentials to be denied. The connection may still
    // carry a historical passing validation, so `admits_new_work` is the field
    // that decides.
    server.use(
      http.get(API('/workspaces/ws-1/provider-connections/c-1'), () =>
        HttpResponse.json({
          connection_id: 'c-1',
          provider: 'aws',
          status: 'Revoked',
          admits_new_work: false,
          allows_renewal: false,
          validation: {
            credential_valid: true,
            permissions_sufficient: true,
            quota_available: true,
            observed_capacity: 4,
            checked_at: '2026-09-23T11:00:00.000Z',
          },
        }),
      ),
    );

    const outcome = await getConnection(guard, 'ws-1', 'c-1');
    expect(outcome.ok).toBe(true);
    if (outcome.ok) {
      expect(outcome.value.admits_new_work).toBe(false);
      expect(outcome.value.status).toBe('Revoked');
    }
  });
});

describe('workspace creation', () => {
  it('sends the operation identity in the body where the proxy will forward it', async () => {
    // The proxy strips client headers, so an `Idempotency-Key` header would be
    // dropped and the create would silently not be idempotent.
    let body: Record<string, unknown> | null = null;
    let headerSeen: string | null = null;
    server.use(
      http.post(API('/workspaces'), async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        headerSeen = request.headers.get('idempotency-key');
        return HttpResponse.json(WORKSPACE, { status: 201 });
      }),
    );

    await createWorkspace(guard, {
      name: 'research',
      isolation_mode: 'dedicated',
      operation_id: 'op-key-1',
    });

    expect(body?.operation_id).toBe('op-key-1');
    expect(headerSeen).toBeNull();
  });

  it('surfaces a provisioning refusal as an actionable failure', async () => {
    // The domain answers 400 when provisioning is refused and 503 when it errors.
    // Both leave a workspace row behind in `Failed`, so the message matters.
    server.use(
      http.post(API('/workspaces'), () =>
        HttpResponse.json(
          { error: 'refused', message: 'The target account has no available capacity.' },
          { status: 400 },
        ),
      ),
    );

    const outcome = await createWorkspace(guard, { name: 'x', isolation_mode: 'dedicated' });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok && 'unavailable' in outcome) {
      expect(outcome.unavailable.detail).toMatch(/no available capacity/i);
    }
  });

  it('does not report a created workspace as ready', async () => {
    // A 201 means the row exists, not that the cluster runs anything. The
    // returned status is passed through verbatim so readiness stays a separate
    // question (AC-04).
    server.use(
      http.post(API('/workspaces'), () =>
        HttpResponse.json(
          { ...WORKSPACE, status: 'Provisioning', cluster_health: null, last_heartbeat: null },
          { status: 201 },
        ),
      ),
    );

    const outcome = await createWorkspace(guard, { name: 'x', isolation_mode: 'dedicated' });
    expect(outcome.ok).toBe(true);
    if (outcome.ok) {
      expect(outcome.value.status).toBe('Provisioning');
      expect(outcome.value.cluster_health).toBeNull();
    }
  });
});

describe('the reviewable plan, parsed fail-closed', () => {
  const WIRE_PLAN = {
    revision: 'rev-7',
    mode: 'managed',
    target: { account: '111122223333', region: 'us-east-1', cluster: null },
    ownership: 'adp-managed',
    requested_capacity: '2 GPUs',
    cost_estimate: null,
    approval_required: false,
  };

  it('requires a revision, because a plan without one cannot bind a submission', () => {
    // The revision is the entire mechanism by which "what the user confirmed" and
    // "what gets built" are held together. A plan lacking it is unusable, and
    // accepting it would let a confirmation apply to anything.
    expect(parsePlan({ ...WIRE_PLAN, revision: undefined })).toBeNull();
    expect(parsePlan({ ...WIRE_PLAN, revision: '' })).toBeNull();
    expect(parsePlan({ ...WIRE_PLAN, revision: 7 })).toBeNull();
  });

  it('treats an absent approval requirement as REQUIRED', () => {
    // Fail-closed on purpose. If a server revision renames or drops this field,
    // defaulting to "no approval needed" would let plans that need sign-off
    // through silently -- an authorization bypass produced by a schema change.
    // Destructured-and-discarded to build the object *without* the key. Setting it
    // to `undefined` instead would leave the key present, which is a different wire
    // shape and would not test an older server that never sends it at all.
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const { approval_required: _omitted, ...withoutField } = WIRE_PLAN;
    expect(parsePlan(withoutField)?.approvalRequired).toBe(true);
  });

  it('treats a non-boolean approval requirement as REQUIRED', () => {
    // Only an explicit `false` waives approval. A string, a null or a number is
    // unreadable, and unreadable must not mean permitted.
    expect(parsePlan({ ...WIRE_PLAN, approval_required: 'no' })?.approvalRequired).toBe(true);
    expect(parsePlan({ ...WIRE_PLAN, approval_required: null })?.approvalRequired).toBe(true);
    expect(parsePlan({ ...WIRE_PLAN, approval_required: 0 })?.approvalRequired).toBe(true);
  });

  it('waives approval only on an explicit false', () => {
    expect(parsePlan(WIRE_PLAN)?.approvalRequired).toBe(false);
  });

  it('keeps an absent cost estimate null rather than defaulting it to zero', () => {
    // "Not estimated" and "free" are different claims about money. Rendering a
    // missing estimate as $0.00 tells the user their GPU workspace costs nothing.
    expect(parsePlan(WIRE_PLAN)?.costEstimate).toBeNull();
    expect(parsePlan({ ...WIRE_PLAN, cost_estimate: {} })?.costEstimate).toBeNull();
    expect(
      parsePlan({ ...WIRE_PLAN, cost_estimate: { amount_usd: 'lots' } })?.costEstimate,
    ).toBeNull();
  });

  it('reads a real estimate, including a legitimate zero', () => {
    // A server-stated zero IS a claim and is preserved; only absence is null.
    const parsed = parsePlan({
      ...WIRE_PLAN,
      cost_estimate: { amount_usd: 0, currency: 'USD', as_of: '2026-09-23', assumptions: 'idle' },
    });
    expect(parsed?.costEstimate?.amountUsd) .toBe(0);
    expect(parsed?.costEstimate?.currency).toBe('USD');
  });

  it('defaults an unrecognised mode to managed rather than inventing one', () => {
    expect(parsePlan({ ...WIRE_PLAN, mode: 'something-new' })?.mode).toBe('managed');
    expect(parsePlan({ ...WIRE_PLAN, mode: 'adopt' })?.mode).toBe('adopt');
  });

  it('rejects a non-object plan', () => {
    expect(parsePlan(null)).toBeNull();
    expect(parsePlan([])).toBeNull();
    expect(parsePlan('rev-7')).toBeNull();
  });
});

describe('the capability report, parsed fail-closed', () => {
  it('rejects a non-object as no report at all', () => {
    // Not "an empty report": `null` and "a server that advertises nothing" are the
    // same decision downstream but not the same fact, and `advertises(null, ...)`
    // is the guard that is tested for it. Turning an unreadable body into an empty
    // report here would put the fail-closed decision in two places.
    expect(parseCapabilities('not-an-object')).toBeNull();
    expect(parseCapabilities(null)).toBeNull();
    expect(parseCapabilities(['features'])).toBeNull();
    expect(parseCapabilities(42)).toBeNull();
  });

  it('reads an advertised feature list', () => {
    const parsed = parseCapabilities({
      features: ['create-operation-id-v1'],
      modes: ['managed', 'adopt'],
      providers: ['aws', 'gcp'],
      isolation_modes: ['dedicated', 'research'],
    });
    expect(parsed).toEqual({
      features: ['create-operation-id-v1'],
      modes: ['managed', 'adopt'],
      providers: ['aws', 'gcp'],
      isolationModes: ['dedicated', 'research'],
    });
  });

  it('drops non-string entries rather than letting them into a string list', () => {
    // A `null` inside `features` would sit in a `readonly string[]` and the type
    // would then be lying to every consumer — including `advertises`, whose
    // `.includes` happens to survive it and whose callers would not.
    const parsed = parseCapabilities({
      features: ['create-operation-id-v1', null, 7, { name: 'x' }],
      modes: [],
      providers: ['aws', undefined],
    });
    expect(parsed?.features).toEqual(['create-operation-id-v1']);
    expect(parsed?.providers).toEqual(['aws']);
  });

  it('drops modes it does not know', () => {
    // `modes` is a two-literal union. Admitting a third value makes every
    // exhaustive switch over it wrong, and the compiler cannot catch it because
    // the value arrived at runtime.
    const parsed = parseCapabilities({ modes: ['managed', 'teleport', 'adopt'] });
    expect(parsed?.modes).toEqual(['managed', 'adopt']);
  });

  it('reads absent lists as empty, not as unknown', () => {
    // An old server with no `features` key advertises nothing. That is a readable
    // answer of "no", distinct from an unreadable body.
    const parsed = parseCapabilities({});
    expect(parsed).toEqual({ features: [], modes: [], providers: [], isolationModes: [] });
  });

  it('reads a non-array list as empty', () => {
    const parsed = parseCapabilities({ features: 'create-operation-id-v1' });
    // Not split, not wrapped: a string where a list belongs is a shape this client
    // does not understand, and guessing that it meant a one-element list would
    // enable a create on a misread field.
    expect(parsed?.features).toEqual([]);
  });
});

describe('operation receipts, parsed so a lost reply stays recoverable', () => {
  const WIRE_RECEIPT = {
    provisioning_operation_id: 'op-77',
    request_id: 'key-77',
    state: 'running',
    workspace_id: 'ws-1',
    phase: 'ProvisioningCluster',
    reason: null,
    observed_at: '2026-09-23T12:00:00.000Z',
    retryable: false,
  };

  it('requires an operation id, because a receipt that names nothing cannot be looked up', () => {
    // The id is the whole point of a receipt: it is what turns "something may have
    // happened" into a question the server can answer. A receipt without one would
    // reach the UI with a recover action that cannot work.
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, request_id: undefined })).toBeNull();
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, request_id: '' })).toBeNull();
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, request_id: 77 })).toBeNull();
  });

  it('maps an unrecognised state to unknown rather than to failed', () => {
    // The single most damaging simplification available here. Reporting a state
    // this client cannot read as `failed` invites a resubmission, and the operation
    // it would duplicate may have succeeded -- a second cluster, billed for.
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, state: 'reconciling' })?.state).toBe('unknown');
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, state: undefined })?.state).toBe('unknown');
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, state: null })?.state).toBe('unknown');
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, state: 7 })?.state).toBe('unknown');
  });

  it('preserves each state the contract declares', () => {
    // The inverse of the test above: mapping everything to `unknown` would satisfy
    // it while making the parser useless.
    for (const state of ['accepted', 'running', 'succeeded', 'failed', 'cancelled'] as const) {
      expect(parseOperationReceipt({ ...WIRE_RECEIPT, state })?.state).toBe(state);
    }
  });

  it('reports an absent workspace as null, never as an empty string', () => {
    // A workspace id is absent until one exists. An empty string would be
    // interpolated into request paths as though it were a real id, producing calls
    // against a workspace that is not there.
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, workspace_id: undefined })?.workspaceId).toBeNull();
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, workspace_id: '' })?.workspaceId).toBeNull();
    expect(parseOperationReceipt(WIRE_RECEIPT)?.workspaceId).toBe('ws-1');
  });

  it('treats an unstated retryable as NOT retryable', () => {
    // Fail-closed, because the cost of a wrong retry is a duplicated cluster. A
    // server that does not say has not granted permission to resubmit.
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, retryable: undefined })?.retryable).toBe(false);
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, retryable: 'yes' })?.retryable).toBe(false);
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, retryable: 1 })?.retryable).toBe(false);
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, retryable: true })?.retryable).toBe(true);
  });

  it('retains request identity before a provisioning operation exists', () => {
    expect(parseOperationReceipt({ ...WIRE_RECEIPT, provisioning_operation_id: null })).toMatchObject({
      operationId: null, idempotencyKey: 'key-77',
    });
  });

  it('rejects a non-object receipt', () => {
    for (const raw of [null, undefined, 'op-77', 42, []]) {
      expect(parseOperationReceipt(raw)).toBeNull();
    }
  });
});

describe('BYOC adoption and operation lookup are refused, not faked', () => {
  let restoreDeployment: () => void;
  beforeEach(() => { restoreDeployment = withoutOnboardingEndpoints(); });
  afterEach(() => { restoreDeployment(); });
  // Model an older deployment without these endpoints. The tests assert the
  // refusal is LOCAL -- no request is issued -- because a request that 404s at the
  // proxy is indistinguishable from a missing workspace, which sends the user
  // hunting for a resource instead of a capability.
  it('adoption names the capability and issues no request', async () => {
    const outcome = await adoptWorkspace(guard, { name: 'existing' });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok && outcome.unavailable) {
      expect(outcome.unavailable.reason).toBe('not-deployed');
      expect(outcome.unavailable.endpoint).toBe('adoptWorkspace');
      expect(outcome.unavailable.capability).toContain('cluster you already operate');
      // No story or ticket reference: the user needs a capability they can ask for.
      expect(outcome.unavailable.detail).not.toMatch(/#\d+|\bstory\b/i);
    }
  });

  it('operation lookup by id is refused locally', async () => {
    const outcome = await getOperation(guard, 'op-77');
    expect(outcome.ok).toBe(false);
    if (!outcome.ok && outcome.unavailable) {
      expect(outcome.unavailable.endpoint).toBe('getOperation');
      expect(outcome.unavailable.capability).toContain('tracking a submitted operation');
    }
  });

  it('recovery by idempotency key is refused locally', async () => {
    const outcome = await recoverOperation(guard, 'key-77');
    expect(outcome.ok).toBe(false);
    if (!outcome.ok && outcome.unavailable) {
      expect(outcome.unavailable.endpoint).toBe('recoverOperation');
      expect(outcome.unavailable.capability).toContain('reply was lost');
    }
  });

  it('parsing remains independent of deployment route availability', () => {
    // An unavailable transport must not change receipt interpretation.
    expect(ENDPOINTS.adoptWorkspace.served).toBe(false);
    expect(ENDPOINTS.getOperation.served).toBe(false);
    expect(ENDPOINTS.recoverOperation.served).toBe(false);
    expect(parseOperationReceipt(WIRE_RECEIPT_FOR_READINESS)?.operationId).toBe('op-88');
  });
});

const WIRE_RECEIPT_FOR_READINESS = {
  provisioning_operation_id: 'op-88',
  request_id: 'key-88',
  state: 'succeeded',
  workspace_id: 'ws-9',
  observed_at: '2026-09-23T12:00:00.000Z',
};
