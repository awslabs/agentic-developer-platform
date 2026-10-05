/**
 * The onboarding client: every request goes through here — issue #5730.
 *
 * WHAT THIS ADDS OVER CALLING `apiClient` DIRECTLY
 * -----------------------------------------------
 * Three things, each of which is a defect if a component improvises it:
 *
 * 1. **Unserved endpoints are never requested.** `contract.ts` records which
 *    method/path pairs the gateway proxy actually allowlists. Sending an
 *    unserved one produces a 404 that looks identical to "workspace not found",
 *    so the client refuses locally and reports *why* instead.
 *
 * 2. **Failures are classified, not flattened.** `apiClient` throws the parsed
 *    error body — a plain object, not an `Error`, so `instanceof Error` is false
 *    and a naive `catch (e) { e.message }` yields undefined. Worse, every
 *    failure looks alike to a component, which then renders one generic banner.
 *    A 403 ("ask an administrator"), a 404-from-the-proxy ("not deployed here"),
 *    and a 503 ("retry") call for three different user actions, so they become
 *    three different `UnavailableReason`s here.
 *
 * 3. **Responses from a superseded organization scope are discarded.** See
 *    below — this is the AC-03 requirement that is easiest to get wrong.
 *
 * WHY A GENERATION COUNTER RATHER THAN JUST `AbortController`
 * ----------------------------------------------------------
 * AC-03 requires that "late responses after org switching are denied or
 * discarded". Aborting in-flight requests is necessary but not sufficient: a
 * response can already be in the microtask queue when the switch happens, past
 * the point where an abort has any effect, and it will then resolve into a
 * component that is now displaying a different tenant. The consequence is one
 * tenant's workspace list rendered under another tenant's name.
 *
 * So each scope carries a generation. A response is delivered only if its
 * generation is still current; otherwise it is dropped and the caller is told
 * the scope changed, which is a different outcome from an error. Abort is *also*
 * used, to stop paying for work nobody will read.
 */

import { apiClient } from '@/services/api';

import {
  ENDPOINTS,
  resolvePath,
  unavailableFor,
  type Capabilities,
  type CredentialRef,
  type EndpointName,
  type OnboardingMode,
  type OnboardingPlan,
  type OperationReceipt,
  type OperationApproval,
  type RetirementPreviewRequest,
  type RetirementReview,
  assertNoSecretMaterial,
  type OperationState,
  type ProviderConnection,
  type Unavailable,
  type ValidationReading,
  type WorkspaceListResponse,
  type WorkspaceSummary,
} from './contract';

/**
 * The outcome of every client call.
 *
 * A three-way result rather than a thrown exception, because "this environment
 * does not offer that" is an ordinary, renderable state — not an exceptional one
 * — and `superseded` is neither success nor failure: nothing went wrong, the
 * answer simply belongs to a scope the user has left.
 */
export type Outcome<T> =
  | { ok: true; value: T }
  | { ok: false; unavailable: Unavailable }
  | { ok: false; superseded: true };

export function isSuperseded<T>(outcome: Outcome<T>): boolean {
  return outcome.ok === false && 'superseded' in outcome;
}

/** Tracks which organization scope is current, so late replies can be dropped. */
export class ScopeGuard {
  private generation = 0;
  private controllers = new Set<AbortController>();

  /** The generation a request should be tagged with. */
  current(): number {
    return this.generation;
  }

  /**
   * Abandon the current scope.
   *
   * Aborts outstanding requests and advances the generation so that any reply
   * already past the point of abort is still discarded on arrival.
   */
  supersede(): void {
    this.generation += 1;
    for (const controller of this.controllers) {
      controller.abort();
    }
    this.controllers.clear();
  }

  /** An `AbortSignal` for one request, cleaned up when it settles. */
  signal(): { signal: AbortSignal; done: () => void } {
    const controller = new AbortController();
    this.controllers.add(controller);
    return { signal: controller.signal, done: () => this.controllers.delete(controller) };
  }

  /** Whether a reply tagged `generation` still belongs to the current scope. */
  isCurrent(generation: number): boolean {
    return generation === this.generation;
  }
}

interface ErrorShape {
  status?: number;
  error?: string;
  message?: string;
  detail?: unknown;
}

/**
 * Turn a thrown API failure into a reason the user can act on.
 *
 * The 404 case carries the subtlety. The gateway proxy 404s a request whose
 * method/path is not allowlisted, and the domain API 404s a workspace that does
 * not exist — identical on the wire. The endpoint name disambiguates: a 404 on
 * an endpoint the allowlist does serve is a missing resource, whereas a 404 on
 * one it does not is a deployment gap.
 */
export function classify(error: unknown, endpoint: EndpointName): Unavailable {
  const shape = (error ?? {}) as ErrorShape;
  const status = typeof shape.status === 'number' ? shape.status : undefined;
  const message =
    (typeof shape.message === 'string' && shape.message) ||
    (typeof shape.error === 'string' && shape.error) ||
    '';

  if (status === 403) {
    return {
      reason: 'not-permitted',
      detail:
        message ||
        'You do not have permission to perform this action. An administrator ' +
          'can grant it.',
      endpoint,
    };
  }
  if (status === 401) {
    // `apiClient` has already cleared tokens and started a redirect to /login.
    // Reported rather than swallowed so a caller mid-submission can stop.
    return {
      reason: 'not-permitted',
      detail: 'Your session has expired. Sign in again to continue.',
      endpoint,
    };
  }
  if (status === 404) {
    if (!ENDPOINTS[endpoint].served) return unavailableFor(endpoint);
    return {
      reason: 'unknown',
      detail: message || 'The requested item does not exist, or is not visible to you.',
      endpoint,
    };
  }
  if (status === 503) {
    return {
      reason: 'unreachable',
      detail:
        message ||
        'Superplane is unavailable in this environment right now. This is ' +
          'usually temporary.',
      endpoint,
    };
  }
  if (status === 502) {
    return {
      reason: 'unreachable',
      detail: message || 'The Superplane API returned an unusable response.',
      endpoint,
    };
  }
  if (status === undefined) {
    // No status means the failure did not come from a completed HTTP response —
    // a network error, or a thrown value from somewhere other than `ApiClient`.
    // `unreachable` is the honest reading: we do not know that the server
    // refused anything, only that we did not get an answer.
    return {
      reason: 'unreachable',
      detail:
        message ||
        'The request did not reach the Superplane API, or the reply could not ' +
          'be read. Check connectivity and retry.',
      endpoint,
    };
  }
  return {
    reason: 'unknown',
    detail: message || `The request failed with HTTP ${status}.`,
    endpoint,
  };
}

/**
 * Perform one request, honouring the served-endpoint and scope-generation rules.
 *
 * `parse` validates and narrows the response. A malformed or older-server
 * response must not reach a component as a partially-shaped object: AC-09 asks
 * for malformed-response assertions, and the failure mode being prevented is a
 * screen that renders `undefined` as though it were data.
 */
export async function call<T>(
  guard: ScopeGuard,
  endpoint: EndpointName,
  params: Record<string, string>,
  body: unknown,
  parse: (raw: unknown) => T | null,
  query?: Record<string, string>,
): Promise<Outcome<T>> {
  const declaration = ENDPOINTS[endpoint];
  if (!declaration.served) {
    // Refused locally. Sending it would 404 at the proxy and be indistinguishable
    // from a missing resource.
    return { ok: false, unavailable: unavailableFor(endpoint) };
  }

  const generation = guard.current();
  const { signal, done } = guard.signal();
  const path = resolvePath(declaration, params) + (query ? `?${new URLSearchParams(query).toString()}` : '');

  try {
    const raw =
      declaration.method === 'GET'
        ? await apiClient.get<unknown>(path, signal)
        : declaration.method === 'DELETE'
          ? await apiClient.delete<unknown>(path, body, signal)
          : await apiClient.post<unknown>(path, body, signal);

    if (!guard.isCurrent(generation)) {
      return { ok: false, superseded: true };
    }

    const value = parse(raw);
    if (value === null) {
      return {
        ok: false,
        unavailable: {
          reason: 'unknown',
          detail:
            'The Superplane API returned a response this version of ADP does ' +
            'not understand. The API may be older or newer than this client.',
          endpoint,
        },
      };
    }
    return { ok: true, value };
  } catch (error) {
    // A scope change aborts in flight; that is a supersede, not a failure, and
    // must not surface as an error banner on the new tenant's screen.
    if (!guard.isCurrent(generation)) {
      return { ok: false, superseded: true };
    }
    if (error instanceof DOMException && error.name === 'AbortError') {
      return { ok: false, superseded: true };
    }
    return { ok: false, unavailable: classify(error, endpoint) };
  } finally {
    done();
  }
}

/** Gateway-owned vault metadata and authority calls retain the same scope guard. */
async function vaultCall<T>(
  guard: ScopeGuard,
  method: 'GET' | 'PUT' | 'POST',
  path: string,
  parse: (raw: unknown) => T | null,
): Promise<Outcome<T>> {
  const generation = guard.current();
  const { signal, done } = guard.signal();
  try {
    const raw = method === 'GET'
      ? await apiClient.get<unknown>(path, signal)
      : method === 'PUT'
        ? await apiClient.put<unknown>(path, undefined, signal)
        : await apiClient.post<unknown>(path, undefined, signal);
    if (!guard.isCurrent(generation)) return { ok: false, superseded: true };
    const value = parse(raw);
    if (value === null) return { ok: false, unavailable: {
      reason: 'unknown', endpoint: 'validateConnection',
      detail: 'The vault returned an incomplete response. No validation reading was submitted.',
    } };
    return { ok: true, value };
  } catch (error) {
    if (!guard.isCurrent(generation)) return { ok: false, superseded: true };
    return { ok: false, unavailable: classify(error, 'validateConnection') };
  } finally { done(); }
}

// ---------------------------------------------------------------------------
// Response parsing. Defensive on purpose: these shapes cross a version boundary.
// ---------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Parse one workspace row.
 *
 * Required fields are checked; optional ones are normalised to `null` rather
 * than left `undefined`, so a caller cannot accidentally distinguish "absent"
 * from "not provided by this server version" when the distinction is meaningless.
 */
export function parseWorkspace(raw: unknown): WorkspaceSummary | null {
  if (!isRecord(raw)) return null;
  const id = raw.id;
  const name = raw.name;
  const status = raw.status;
  if (typeof id !== 'string' || typeof name !== 'string' || typeof status !== 'string') {
    return null;
  }
  return {
    id,
    org_id: typeof raw.org_id === 'string' ? raw.org_id : '',
    name,
    display_name: typeof raw.display_name === 'string' ? raw.display_name : name,
    isolation_mode: typeof raw.isolation_mode === 'string' ? raw.isolation_mode : '',
    status,
    is_default: raw.is_default === true,
    cluster_health: typeof raw.cluster_health === 'string' ? raw.cluster_health : null,
    last_heartbeat: typeof raw.last_heartbeat === 'string' ? raw.last_heartbeat : null,
    created_at: typeof raw.created_at === 'string' ? raw.created_at : '',
    updated_at: typeof raw.updated_at === 'string' ? raw.updated_at : '',
    provisioning_operation_id: typeof raw.provisioning_operation_id === 'string' ? raw.provisioning_operation_id : null,
    operation_state: raw.operation_state === undefined ? undefined : parseOperationState(raw.operation_state),
  };
}

export function parseWorkspaceList(raw: unknown): WorkspaceListResponse | null {
  if (!isRecord(raw) || !Array.isArray(raw.workspaces)) return null;
  const workspaces: WorkspaceSummary[] = [];
  for (const entry of raw.workspaces) {
    const parsed = parseWorkspace(entry);
    // One unreadable row does not discard the rest: a list that renders four of
    // five workspaces is far more useful than an error, and dropping the bad row
    // is visible in the count.
    if (parsed) workspaces.push(parsed);
  }
  return {
    workspaces,
    total: typeof raw.total === 'number' ? raw.total : workspaces.length,
  };
}

/** Tri-state: `true`/`false` as given, anything else (including absent) `null`. */
function triState(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

export function parseValidation(raw: unknown): ValidationReading | null {
  if (!isRecord(raw)) return null;
  return {
    credential_valid: triState(raw.credential_valid),
    permissions_sufficient: triState(raw.permissions_sufficient),
    quota_available: triState(raw.quota_available),
    observed_capacity: typeof raw.observed_capacity === 'number' ? raw.observed_capacity : null,
    checked_at: typeof raw.checked_at === 'string' ? raw.checked_at : '',
    detail: typeof raw.detail === 'string' ? raw.detail : null,
  };
}

/**
 * Parse a provider connection.
 *
 * `admits_new_work` and `allows_renewal` default to `false` when absent. That is
 * the fail-closed direction: an older server that does not report whether a
 * connection admits work must not have that read as "yes, it does".
 */
export function parseConnection(raw: unknown): ProviderConnection | null {
  if (!isRecord(raw)) return null;
  const connectionId = raw.connection_id;
  const provider = raw.provider;
  if (typeof connectionId !== 'string' || typeof provider !== 'string') return null;
  const credential = isRecord(raw.credential) ? raw.credential : {};
  return {
    connection_id: connectionId,
    provider,
    status: typeof raw.status === 'string' ? raw.status : 'Unknown',
    workspace_id: typeof raw.workspace_id === 'string' ? raw.workspace_id : '',
    credential: {
      credential_id: typeof credential.credential_id === 'string' ? credential.credential_id : '',
      service: typeof credential.service === 'string' ? credential.service : '',
      label: typeof credential.label === 'string' ? credential.label : '',
    },
    admits_new_work: raw.admits_new_work === true,
    allows_renewal: raw.allows_renewal === true,
    validation: raw.validation === undefined ? undefined : (parseValidation(raw.validation) ?? undefined),
    limitation: typeof raw.limitation === 'string' ? raw.limitation : null,
  };
}

// ---------------------------------------------------------------------------
// The onboarding operations.
// ---------------------------------------------------------------------------

export function listWorkspaces(guard: ScopeGuard): Promise<Outcome<WorkspaceListResponse>> {
  return call(guard, 'listWorkspaces', {}, undefined, parseWorkspaceList);
}

export function getWorkspace(
  guard: ScopeGuard,
  workspaceId: string,
): Promise<Outcome<WorkspaceSummary>> {
  return call(guard, 'getWorkspace', { workspace_id: workspaceId }, undefined, parseWorkspace);
}

export interface HumanWorkspaceAccess {
  workspace_id: string;
  grant_id: string;
  revision: number;
  principal_type: 'human';
  subject: string;
  effective_permissions: string[];
  granted_by: string | null;
  reason: string | null;
  request_id: string | null;
}

function parseHumanWorkspaceAccess(raw: unknown): HumanWorkspaceAccess | null {
  if (!isRecord(raw) || typeof raw.workspace_id !== 'string' ||
      typeof raw.grant_id !== 'string' || !Number.isInteger(raw.revision) ||
      (raw.revision as number) < 1 || raw.principal_type !== 'human' ||
      typeof raw.subject !== 'string' || !Array.isArray(raw.effective_permissions) ||
      !raw.effective_permissions.every((permission) => typeof permission === 'string' &&
        ['workspace:read', 'workspace:spend', 'workspace:provision', 'workspace:renew_credential', 'workspace:administer'].includes(permission)) ||
      (raw.granted_by !== null && typeof raw.granted_by !== 'string') ||
      (raw.reason !== null && typeof raw.reason !== 'string') ||
      (raw.request_id !== null && typeof raw.request_id !== 'string')) return null;
  return raw as unknown as HumanWorkspaceAccess;
}

export function getWorkspaceAccess(guard: ScopeGuard, workspaceId: string): Promise<Outcome<HumanWorkspaceAccess>> {
  return call(guard, 'getWorkspaceAccess', { workspace_id: workspaceId }, undefined,
    (raw) => {
      const parsed = parseHumanWorkspaceAccess(raw);
      return parsed?.workspace_id === workspaceId ? parsed : null;
    });
}

export function grantWorkspaceAccess(
  guard: ScopeGuard, workspaceId: string, targetSubject: string, requestId: string,
): Promise<Outcome<HumanWorkspaceAccess>> {
  return call(guard, 'grantWorkspaceAccess', { workspace_id: workspaceId }, {
    target_subject: targetSubject, principal_type: 'human', permissions: ['workspace:read'],
    reason: 'approver_setup', expected_revision: 0, request_id: requestId,
  }, (raw) => {
    const parsed = parseHumanWorkspaceAccess(raw);
    return parsed?.workspace_id === workspaceId && parsed.subject === targetSubject &&
      parsed.request_id === requestId && parsed.principal_type === 'human' &&
      parsed.effective_permissions.includes('workspace:read') ? parsed : null;
  });
}

export function parseRetirementReview(raw: unknown): RetirementReview | null {
  if (!isRecord(raw) || raw.admission_available !== false ||
      raw.blocked_reason !== 'staged_cleanup_access_required' || raw.approval_request !== null ||
      !Array.isArray(raw.steps) || !Array.isArray(raw.preserved)) return null;
  const fields = [
    'request_id', 'workspace_id', 'source_operation_id', 'source_payload_digest',
    'lifecycle_artifact_id', 'account_id', 'region', 'inventory_sha256',
    'lifecycle_policy_sha256', 'runtime_config_sha256', 'revision',
  ] as const;
  if (fields.some((field) => typeof raw[field] !== 'string' || !raw[field])) return null;
  if (!raw.steps.every((step) => isRecord(step) &&
    ['step_id', 'provider', 'operation_kind', 'target'].every((field) =>
      typeof step[field] === 'string' && Boolean(step[field])))) return null;
  if (!raw.preserved.every((item) => typeof item === 'string')) return null;
  return raw as unknown as RetirementReview;
}

export function previewRetirement(
  guard: ScopeGuard,
  workspaceId: string,
  request: RetirementPreviewRequest,
): Promise<Outcome<RetirementReview>> {
  return call(guard, 'previewRetirement', { workspace_id: workspaceId }, request, (raw) => {
    const review = parseRetirementReview(raw);
    return review?.workspace_id === workspaceId && review.request_id === request.operation_id
      ? review : null;
  });
}

/**
 * Submit a workspace creation.
 *
 * `operationId` is the caller's pre-minted identity from `operations.ts`, sent in
 * the body because the proxy forwards no client headers. The caller is
 * responsible for having confirmed the server advertises
 * `CREATE_IDEMPOTENCY_FEATURE` first — without it the server ignores the field
 * and the request is not idempotent despite appearing to succeed.
 */
export function createWorkspace(
  guard: ScopeGuard,
  body: Record<string, unknown>,
): Promise<Outcome<WorkspaceSummary>> {
  return call(guard, 'createWorkspace', {}, body, parseWorkspace);
}

/**
 * Bind a vault credential to a workspace as a provider connection.
 *
 * Note the shape of what crosses the wire: a credential *reference*. The raw
 * secret is entered into ADP's vault through its own surface and never passes
 * through onboarding, which is what keeps it out of this client's arguments,
 * logs and diagnostics (AC-03).
 */
export function registerConnection(
  guard: ScopeGuard,
  workspaceId: string,
  body: { provider: string; credential_id: string; service: string; label: string },
): Promise<Outcome<ProviderConnection>> {
  return call(guard, 'registerConnection', { workspace_id: workspaceId }, body, parseConnection);
}

/**
 * Build the bind body from the credential row the user actually selected.
 *
 * Takes the whole `CredentialRef` rather than an id, so the three reference
 * fields cannot come from three different places. The server requires all three
 * (`accept_connection_request` refuses a blank `credential_id`, `service` or
 * `label`) and separately requires `service` to equal `provider`, so an
 * id-plus-retyped-metadata call site is a 400 waiting to happen — and worse, a
 * way to bind one credential while labelling it as another.
 */
export function buildBindBody(
  credential: CredentialRef,
): { provider: string; credential_id: string; service: string; label: string } {
  return {
    provider: credential.service,
    credential_id: credential.credential_id,
    service: credential.service,
    label: credential.label,
  };
}

/** Parse a registry ID joined to canonical Gateway metadata, never a friendly name. */
export function parseCredentialRef(raw: unknown): CredentialRef | null {
  if (!isRecord(raw)) return null;
  const credentialId = raw.adp_credential_id;
  if (typeof credentialId !== 'string' || credentialId === '') return null;
  const service = raw.provider;
  const label = raw.label;
  // `service` and `label` are required by the server's own acceptance function
  // (`accept_connection_request` refuses a blank one), so a row that cannot
  // supply them is dropped here rather than sent and refused with a 400 that
  // the user can do nothing about.
  if (typeof service !== 'string' || service === '') return null;
  if (typeof label !== 'string' || label === '') return null;
  return { credential_id: credentialId, service, label };
}

/**
 * Parse the credential list, dropping unusable rows rather than the whole list.
 *
 * One malformed row must not blank the picker: the user would see "no
 * credentials" and go create a duplicate of one that already exists.
 */
export function parseCredentialList(raw: unknown): CredentialRef[] | null {
  const rows = isRecord(raw) ? raw.credentials : raw;
  if (!Array.isArray(rows)) return null;
  return rows
    .map(parseCredentialRef)
    .filter((credential): credential is CredentialRef => credential !== null);
}

/**
 * The vault credential references available to bind.
 *
 * Note what this does NOT do: it never asks the vault for a secret value, and
 * there is no client function that can. Binding sends an id (see
 * {@link registerConnection}), so no code path exists down which a value could
 * travel into onboarding.
 */
export async function listCredentials(guard: ScopeGuard): Promise<Outcome<CredentialRef[]>> {
  const generation = guard.current();
  const registry = await call(guard, 'listCredentials', {}, undefined, (raw) => {
    const rows = isRecord(raw) ? raw.credentials : raw;
    return Array.isArray(rows) ? rows.filter(isRecord) : null;
  });
  if (!registry.ok) return registry;
  if (!guard.isCurrent(generation)) return { ok: false, superseded: true };
  const vault = await vaultCall(guard, 'GET', '/auth/credentials', (raw) =>
    Array.isArray(raw) ? raw.filter(isRecord) : null);
  if (!vault.ok) return vault;
  if (!guard.isCurrent(generation)) return { ok: false, superseded: true };
  const references = registry.value.flatMap((row) => {
    const canonical = vault.value.find((item) => item.id === row.adp_credential_id);
    if (!canonical || canonical.service !== row.provider) return [];
    const ref = parseCredentialRef({
      adp_credential_id: row.adp_credential_id, provider: canonical.service, label: canonical.label,
    });
    return ref ? [ref] : [];
  });
  return { ok: true, value: references };
}

export function getConnection(
  guard: ScopeGuard,
  workspaceId: string,
  connectionId: string,
): Promise<Outcome<ProviderConnection>> {
  return call(
    guard,
    'getConnection',
    { workspace_id: workspaceId, connection_id: connectionId },
    undefined,
    parseConnection,
  );
}

/** Authorize delivery to this workspace before registering its connection. */
export async function delegateCredential(
  guard: ScopeGuard, workspaceId: string, credentialId: string,
): Promise<Outcome<{ delegated: true }>> {
  return vaultCall(guard, 'PUT',
    `/auth/credentials/${encodeURIComponent(credentialId)}/workspaces/${encodeURIComponent(workspaceId)}`,
    () => ({ delegated: true }));
}

/** Request provider evidence from Gateway, then submit only its attested report. */
export async function validateConnection(
  guard: ScopeGuard, workspaceId: string, connectionId: string,
): Promise<Outcome<ProviderConnection>> {
  const generation = guard.current();
  const connection = await getConnection(guard, workspaceId, connectionId);
  if (!connection.ok) return connection;
  if (!guard.isCurrent(generation)) return { ok: false, superseded: true };
  const credentialId = connection.value.credential.credential_id;
  const evidence = await vaultCall(guard, 'POST',
    `/auth/credentials/${encodeURIComponent(credentialId)}/workspaces/${encodeURIComponent(workspaceId)}/validation`,
    (raw) => {
      if (!isRecord(raw) || raw.credential_id !== credentialId || raw.workspace_id !== workspaceId ||
          !isRecord(raw.validation)) return null;
      const report = raw.validation;
      if (typeof report.credential_valid !== 'boolean' ||
          typeof report.permissions_sufficient !== 'boolean' ||
          typeof report.quota_available !== 'boolean' ||
          typeof report.checked_at !== 'string') return null;
      return parseValidation(report);
    });
  if (!evidence.ok) return evidence;
  if (!guard.isCurrent(generation)) return { ok: false, superseded: true };
  return call(guard, 'validateConnection',
    { workspace_id: workspaceId, connection_id: connectionId }, evidence.value, parseConnection);
}

// ---------------------------------------------------------------------------
// Operations the baseline does not serve.
//
// These exist so the UI and the CLI have one honest answer to give instead of
// each improvising. Every one returns `not-deployed`, naming the capability the
// environment lacks, without issuing a request — the alternative is a 404 from
// the proxy that is indistinguishable from "that workspace does not exist",
// which would send a deployer looking for a missing resource rather than a
// missing route.
//
// When the backend serves them, flipping `served` in `contract.ts` is what
// activates them; the allowlist test fails until the flag matches reality in
// both directions, so neither side can drift silently.
// ---------------------------------------------------------------------------

/**
 * Parse the capability report.
 *
 * Every list is filtered to strings rather than taken as-is. A server that sends
 * `features: [null, "create-operation-id-v1"]` would otherwise put a `null` into
 * a `readonly string[]`, and the type would claim otherwise for the rest of the
 * program. Unknown modes are dropped for the same reason: `modes` is a union of
 * two literals, and admitting a third value into it makes every exhaustive switch
 * downstream a lie.
 *
 * A non-object response parses to `null`, which {@link advertises} reads as "no
 * feature advertised" — so a malformed or ancient reply blocks the create rather
 * than accidentally permitting it.
 */
export function parseCapabilities(raw: unknown): Capabilities | null {
  if (!isRecord(raw)) return null;
  const strings = (value: unknown): string[] =>
    Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];

  return {
    features: strings(raw.features),
    modes: strings(raw.modes).filter(
      (mode): mode is OnboardingMode => mode === 'managed' || mode === 'adopt',
    ),
    providers: strings(raw.providers),
    // Read from either spelling the server might use. Both are filtered to
    // strings, and `offeredIsolationModes` then intersects with the schema's
    // pattern — so an advertised mode the validator would reject is never offered.
    isolationModes: strings(raw.isolation_modes ?? raw.isolationModes),
  };
}

/** Which lifecycle modes, providers and readiness readings are really available. */
export function getCapabilities(guard: ScopeGuard): Promise<Outcome<Capabilities>> {
  return call(guard, 'capabilities', {}, undefined, parseCapabilities);
}

/**
 * Parse the reviewable plan.
 *
 * Wire shape is snake_case, matching every other response in this domain. Fields
 * are read fail-closed: `revision` is required because a plan without one cannot
 * be bound to a submission, and `cost_estimate` stays `null` when absent rather
 * than defaulting to zero — "not estimated" and "free" are different claims and
 * only one of them is honest about money.
 *
 * `approval_required` defaults to `true` when absent or non-boolean. That is the
 * safe direction: treating an unreadable field as "no approval needed" would let a
 * plan that needs sign-off through on a server-shape change.
 */
export function parsePlan(raw: unknown): OnboardingPlan | null {
  if (!isRecord(raw)) return null;
  const revision = raw.revision;
  if (typeof revision !== 'string' || revision === '') return null;
  const mode = raw.mode === 'adopt' ? 'adopt' : 'managed';
  const target = isRecord(raw.target) ? raw.target : {};
  const estimate = isRecord(raw.cost_estimate) ? raw.cost_estimate : null;

  return {
    revision,
    mode,
    target: {
      account: typeof target.account === 'string' ? target.account : null,
      region: typeof target.region === 'string' ? target.region : null,
      cluster: typeof target.cluster === 'string' ? target.cluster : null,
    },
    ownership: typeof raw.ownership === 'string' ? raw.ownership : 'unspecified',
    requestedCapacity:
      typeof raw.requested_capacity === 'string' ? raw.requested_capacity : 'unspecified',
    costEstimate:
      estimate && typeof estimate.amount_usd === 'number'
        ? {
            amountUsd: estimate.amount_usd,
            currency: typeof estimate.currency === 'string' ? estimate.currency : 'USD',
            asOf: typeof estimate.as_of === 'string' ? estimate.as_of : '',
            assumptions:
              typeof estimate.assumptions === 'string' ? estimate.assumptions : '',
          }
        : null,
    approvalRequired: raw.approval_required !== false,
    approvalRequest: parseApprovalRequest(raw.approval_request),
  };
}

/** Read-only validation of create/adopt inputs, returning the reviewable plan. */
export function previewWorkspace(
  guard: ScopeGuard,
  body: Record<string, unknown>,
): Promise<Outcome<OnboardingPlan>> {
  return call(guard, 'previewWorkspace', {}, body, parsePlan);
}

/**
 * Read an operation receipt, refusing to invent a state.
 *
 * WHY AN UNREADABLE STATE BECOMES `unknown` AND NOT `failed`
 * ---------------------------------------------------------
 * Every unrecognised state maps to `unknown`, and that is the only safe
 * direction. Guessing `failed` invites a resubmission, and the operation it would
 * duplicate may have succeeded — a second cluster, billed. Guessing `succeeded`
 * hides infrastructure that exists. `unknown` is the one answer that prompts the
 * user to go and look, which is what the situation actually calls for.
 *
 * A missing `request_id` returns null rather than an empty string: the whole
 * value of a receipt is naming an operation that can be looked up again, and a
 * receipt with no id is not a receipt. Returning null routes it to the
 * "response this version does not understand" path in `call`, instead of handing
 * the UI a receipt whose recover button cannot work.
 */
export function parseApprovalRequest(raw: unknown): Record<string, unknown> | null {
  if (!isRecord(raw) || typeof raw.workspace_id !== 'string' ||
      (raw.action !== 'provision' && raw.action !== 'teardown') ||
      typeof raw.idempotency_key !== 'string' || !isRecord(raw.parameters) ||
      Object.values(raw.parameters).some((value) => typeof value !== 'string')) return null;
  const request = { workspace_id: raw.workspace_id, action: raw.action,
    idempotency_key: raw.idempotency_key, parameters: raw.parameters };
  assertNoSecretMaterial(request, 'approval request');
  return request;
}

export function parseApproval(raw: unknown): OperationApproval | null {
  if (!isRecord(raw) || typeof raw.approval_id !== 'string' || typeof raw.workspace_id !== 'string' ||
      !['pending', 'allowed-once', 'rejected'].includes(String(raw.result))) return null;
  const envelope = isRecord(raw.envelope) ? raw.envelope : {};
  const request = isRecord(raw.request) ? raw.request : {};
  const parameters = isRecord(request.parameters) ? request.parameters : {};
  const target = Object.fromEntries(['name', 'workspace_name', 'account', 'region', 'cluster_reference', 'mode', 'isolation_mode']
    .flatMap((key) => typeof parameters[key] === 'string' ? [[key, parameters[key] as string]] : []));
  return {
    approval_id: raw.approval_id, workspace_id: raw.workspace_id,
    result: raw.result as OperationApproval['result'], can_decide: raw.can_decide === true,
    action: typeof request.action === 'string' ? request.action : 'not reported', target,
    plan_digest: typeof raw.plan_digest === 'string' ? raw.plan_digest : '',
    expires_at: typeof raw.expires_at === 'string' ? raw.expires_at : '', revoked: raw.revoked === true,
    envelope: {
      max_resource_units: typeof envelope.max_resource_units === 'number' ? envelope.max_resource_units : null,
      max_runtime_seconds: typeof envelope.max_runtime_seconds === 'number' ? envelope.max_runtime_seconds : null,
      max_cost_micros: typeof envelope.max_cost_micros === 'number' ? envelope.max_cost_micros : null,
    },
  };
}

export function requestApproval(guard: ScopeGuard, body: Record<string, unknown>) {
  assertNoSecretMaterial(body, 'approval request');
  return call(guard, 'requestApproval', {}, body, parseApproval);
}

export interface LifecycleProposal {
  artifactId: string;
  workspaceId: string;
  sourceOperationId: string;
  requestRevision: string;
  phase: string;
  accountId: string;
  target: Record<string, string>;
  planFileSha256: string | null;
  planJsonSha256: string | null;
  inventory: unknown;
  estimate: unknown;
  requestId?: string;
  revision?: string;
  approvalRequest?: Record<string, unknown>;
}

function parseLifecycleProposal(raw: unknown): LifecycleProposal | null {
  if (!isRecord(raw) || raw.status !== 'awaiting_plan_approval' ||
      ['artifact_id', 'workspace_id', 'source_operation_id', 'request_revision', 'phase', 'account_id']
        .some((key) => typeof raw[key] !== 'string' || !raw[key])) return null;
  const hash = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value) ? value : null;
  if (raw.phase === 'apply-infrastructure' && (!hash(raw.plan_file_sha256) || !hash(raw.plan_json_sha256))) return null;
  const target = isRecord(raw.target) ? Object.fromEntries(Object.entries(raw.target).filter((entry): entry is [string, string] => typeof entry[1] === 'string')) : {};
  const visible = { target, inventory: raw.inventory ?? null, estimate: raw.estimate ?? null };
  assertNoSecretMaterial(visible, 'lifecycle proposal');
  return {
    artifactId: raw.artifact_id as string, workspaceId: raw.workspace_id as string,
    sourceOperationId: raw.source_operation_id as string, requestRevision: raw.request_revision as string,
    phase: raw.phase as string, accountId: raw.account_id as string, ...visible,
    planFileSha256: hash(raw.plan_file_sha256), planJsonSha256: hash(raw.plan_json_sha256),
    requestId: typeof raw.request_id === 'string' ? raw.request_id : undefined,
    revision: typeof raw.revision === 'string' ? raw.revision : undefined,
    approvalRequest: parseApprovalRequest(raw.approval_request) ?? undefined,
  };
}

export function listLifecycleProposals(guard: ScopeGuard, workspaceId: string) {
  return call(guard, 'listLifecycleProposals', { workspace_id: workspaceId }, undefined, (raw): LifecycleProposal[] | null => {
    if (!isRecord(raw) || raw.workspace_id !== workspaceId || !Array.isArray(raw.proposals)) return null;
    const proposals = raw.proposals.map(parseLifecycleProposal);
    if (proposals.some((proposal) => !proposal || proposal.workspaceId !== workspaceId)) return null;
    return proposals as LifecycleProposal[];
  });
}

export function previewLifecycleProposal(guard: ScopeGuard, workspaceId: string, artifactId: string, requestId: string) {
  return call(guard, 'previewLifecycleProposal', { workspace_id: workspaceId, artifact_id: artifactId },
    { operation_id: requestId }, (raw) => {
      const proposal = parseLifecycleProposal(raw);
      return proposal?.workspaceId === workspaceId && proposal.artifactId === artifactId &&
        proposal.requestId === requestId && proposal.revision &&
        proposal.approvalRequest?.workspace_id === workspaceId &&
        proposal.approvalRequest.idempotency_key === requestId &&
        proposal.approvalRequest.action === 'provision' ? proposal : null;
    });
}

export function continueLifecycleProposal(guard: ScopeGuard, workspaceId: string, artifactId: string, requestId: string, approvalId: string) {
  return call(guard, 'continueLifecycleProposal', { workspace_id: workspaceId, artifact_id: artifactId },
    { operation_id: requestId, approval_id: approvalId }, (raw) => {
      const receipt = parseOperationReceipt(raw);
      return receipt?.workspaceId === workspaceId && receipt.idempotencyKey === requestId && receipt.operationId ? receipt : null;
    });
}
export function getApproval(guard: ScopeGuard, approvalId: string) {
  return call(guard, 'getApproval', { approval_id: approvalId }, undefined, parseApproval);
}
export function decideApproval(guard: ScopeGuard, approvalId: string, result: 'allowed-once' | 'rejected') {
  return call(guard, 'decideApproval', { approval_id: approvalId }, { result }, parseApproval);
}

export function parseOperationState(raw: unknown): OperationState {
  if (raw === 'pending') return 'accepted';
  return raw === 'accepted' || raw === 'running' || raw === 'succeeded' || raw === 'failed' || raw === 'cancelled'
    ? raw : 'unknown';
}

export function parseOperationReceipt(raw: unknown): OperationReceipt | null {
  if (!isRecord(raw)) return null;
  const requestId = raw.request_id;
  if (typeof requestId !== 'string' || !requestId) return null;
  const operationId = raw.provisioning_operation_id;
  return {
    operationId: typeof operationId === 'string' && operationId ? operationId : null,
    idempotencyKey: requestId,
    state: parseOperationState(raw.state),
    workspaceId: typeof raw.workspace_id === 'string' && raw.workspace_id ? raw.workspace_id : null,
    phase: typeof raw.phase === 'string' ? raw.phase : null,
    reason: typeof raw.reason === 'string' ? raw.reason : null,
    observedAt: typeof raw.observed_at === 'string' ? raw.observed_at : '',
    retryable: raw.retryable === true,
  };
}

/** Submit adoption of the reviewed cluster; return the accepted workspace and real operation ID. */
export function adoptWorkspace(
  guard: ScopeGuard,
  body: Record<string, unknown>,
): Promise<Outcome<WorkspaceSummary>> {
  return call(guard, 'adoptWorkspace', {}, body, parseWorkspace);
}

/** Durable operation state by operation id. */
export function getOperation(
  guard: ScopeGuard,
  operationId: string,
): Promise<Outcome<OperationReceipt>> {
  return call(
    guard,
    'getOperation',
    { operation_id: operationId },
    undefined,
    parseOperationReceipt,
  );
}

/**
 * Recover the receipt of a submission whose reply was lost.
 *
 * This is the route that resolves an `unknown` into a fact. Without it a lost
 * reply stays unknown permanently, because the only other way to discover what
 * happened is to submit again — which is precisely the thing that risks a second
 * workspace. Looking the submission up by the identity it was sent under asks the
 * question without repeating the action.
 */
export function recoverOperation(
  guard: ScopeGuard,
  idempotencyKey: string,
): Promise<Outcome<OperationReceipt>> {
  return call(
    guard,
    'recoverOperation',
    { idempotency_key: idempotencyKey },
    undefined,
    parseOperationReceipt,
  );
}

export function revokeConnection(
  guard: ScopeGuard,
  workspaceId: string,
  connectionId: string,
): Promise<Outcome<{ revoked: true }>> {
  return call(
    guard,
    'revokeConnection',
    { workspace_id: workspaceId, connection_id: connectionId },
    undefined,
    // A revoke that returns an empty body is still a revoke; `apiClient` maps an
    // empty response to `{}`, so success is not inferred from response content.
    () => ({ revoked: true }) as const,
  );
}
