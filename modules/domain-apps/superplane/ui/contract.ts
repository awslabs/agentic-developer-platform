/**
 * The onboarding contract both ADP clients speak — issue #5730 (EPIC #4910).
 *
 * WHY THIS FILE EXISTS, AND WHY IT IS THE ONLY PLACE PATHS APPEAR
 * ---------------------------------------------------------------
 * The browser onboarding UI and the installed `adp` CLI must reach the *same*
 * governed endpoints. The failure mode this file prevents is each client
 * inventing its own idea of the API: #5637 found the previous CLI posting to a
 * `/providers` prefix the gateway proxy did not serve, so every provider call
 * 404'd against a route that looked plausible in the source. Two clients guessing
 * independently produces two different 404s and no single place to fix them.
 *
 * So every request this module's clients make is declared here, once, and each
 * declaration records whether the route is *actually served today*.
 *
 * WHAT "SERVED" MEANS, AND WHY IT IS RECORDED RATHER THAN ASSUMED
 * ---------------------------------------------------------------
 * A Superplane request reaches the domain API through the ADP gateway's domain
 * proxy, which forwards only requests matching its route allowlist
 * (`modules/gateway/src/domain_proxy/superplane_routes.json`) and 404s everything
 * else. An endpoint therefore needs three separate things to work, and having one
 * is not having the others:
 *
 *   1. a route mounted in the domain API,
 *   2. an entry in the gateway proxy allowlist,
 *   3. an entry in the domain permission inventory.
 *
 * All mapped routes below are mounted, allowlisted and inventoried in the
 * composed API. `served: true` records route availability; it does not advertise
 * a deployment capability, authorize a mutation or establish execution
 * readiness. The clients still require the server's capability, approval and
 * readiness responses before presenting those claims.
 *
 * Changing `served` here is not how an endpoint becomes available. The allowlist
 * and the permission inventory are #5535-owned; this file follows them.
 */

/** Path prefix the ADP gateway exposes the Superplane domain API under. */
export const DOMAIN_BASE = '/superplane/v1';

/**
 * Why an onboarding action cannot be performed right now.
 *
 * Kept as distinct reasons rather than one boolean because they call for
 * different user action. "The server does not offer this yet" is a wait; "you
 * are not permitted" is a request to an administrator; "we could not reach it"
 * is a retry. Collapsing them produces the generic error banner that tells a
 * user nothing.
 */
export type UnavailableReason =
  | 'not-deployed'
  | 'not-permitted'
  | 'unreachable'
  | 'unknown';

export interface EndpointDeclaration {
  readonly method: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  /** Path under {@link DOMAIN_BASE}, using `{param}` templates. */
  readonly path: string;
  /**
   * Whether the ADP gateway domain proxy allowlists this method/path pair at
   * the baseline revision. `false` means requests would 404 at the proxy, so the
   * clients must not send them.
   */
  readonly served: boolean;
  /**
   * Required when `served` is false and retained after activation: the product
   * capability this endpoint provides, phrased for the person who hits the wall.
   *
   * This is deliberately NOT a story or ticket number. A diagnostic reading
   * "Tracked in #5535" tells the operator nothing they can act on and nothing
   * they can even look up without repository access; it describes our backlog
   * rather than their system. Naming the capability instead lets them ask their
   * platform owner a precise question, or recognize that the feature is simply
   * not enabled in this environment.
   *
   * Automation keys off the stable `reason` and `endpoint` fields on
   * {@link Unavailable}, never off this prose — see the note there.
   */
  readonly capability?: string;
}

/**
 * Every onboarding request, keyed by the action it performs.
 *
 * The keys are the vocabulary the rest of this module uses; no component or CLI
 * command builds a path string of its own.
 */
export const ENDPOINTS = {
  listWorkspaces: { method: 'GET', path: '/workspaces', served: true },
  getWorkspace: { method: 'GET', path: '/workspaces/{workspace_id}', served: true },
  createWorkspace: { method: 'POST', path: '/workspaces', served: true },
  cancelBatchJob: { method: 'POST', path: '/workspaces/{workspace_id}/batch-jobs/{job_id}/cancellation', served: true },
  cancelDeployment: { method: 'POST', path: '/workspaces/{workspace_id}/deployments/{dep_id}/cancellation', served: true },
  batchProfiles: { method: 'GET', path: '/workspaces/{workspace_id}/batch-profiles', served: true },
  listBatchJobs: { method: 'GET', path: '/workspaces/{workspace_id}/batch-jobs', served: true },
  previewBatchJob: { method: 'POST', path: '/workspaces/{workspace_id}/batch-jobs/preview', served: true },
  createBatchJob: { method: 'POST', path: '/workspaces/{workspace_id}/batch-jobs', served: true },
  previewBatchTeardown: { method: 'POST', path: '/workspaces/{workspace_id}/batch-jobs/{job_id}/teardown-preview', served: true },
  deleteBatchJob: { method: 'DELETE', path: '/workspaces/{workspace_id}/batch-jobs/{job_id}', served: true },
  listDeployments: { method: 'GET', path: '/workspaces/{workspace_id}/deployments', served: true },
  servingProfiles: { method: 'GET', path: '/workspaces/{workspace_id}/deployment-profiles', served: true },
  previewDeployment: { method: 'POST', path: '/workspaces/{workspace_id}/deployments/preview', served: true },
  createDeployment: { method: 'POST', path: '/workspaces/{workspace_id}/deployments', served: true },
  previewDeploymentTeardown: { method: 'POST', path: '/workspaces/{workspace_id}/deployments/{dep_id}/teardown-preview', served: true },
  deleteDeployment: { method: 'DELETE', path: '/workspaces/{workspace_id}/deployments/{dep_id}', served: true },

  registerConnection: {
    method: 'POST',
    path: '/workspaces/{workspace_id}/provider-connections',
    served: true,
  },
  getConnection: {
    method: 'GET',
    path: '/workspaces/{workspace_id}/provider-connections/{connection_id}',
    served: true,
  },
  validateConnection: {
    method: 'POST',
    path: '/workspaces/{workspace_id}/provider-connections/{connection_id}/validation',
    served: true,
  },
  revokeConnection: {
    method: 'DELETE',
    path: '/workspaces/{workspace_id}/provider-connections/{connection_id}',
    served: true,
  },

  /** Vault credential references. Values are entered through ADP's vault, not here. */
  listCredentials: { method: 'GET', path: '/vault/credentials', served: true },

  listLifecycleProposals: { method: 'GET', path: '/workspaces/{workspace_id}/lifecycle-proposals', served: true, capability: 'listing the next workspace lifecycle plan' },
  previewLifecycleProposal: { method: 'POST', path: '/workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/preview', served: true, capability: 'reviewing the next recorded workspace plan' },
  continueLifecycleProposal: { method: 'POST', path: '/workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/continue', served: true, capability: 'continuing an approved workspace lifecycle plan' },

  requestApproval: { method: 'POST', path: '/operation-approvals', served: true, capability: 'requesting approval for a reviewed operation' },
  getApproval: { method: 'GET', path: '/operation-approvals/{approval_id}', served: true, capability: 'reading a requested operation approval' },
  decideApproval: { method: 'POST', path: '/operation-approvals/{approval_id}/decision', served: true, capability: 'deciding an operation approval' },

  /**
   * Adopt a cluster the user already operates (bring-your-own-cluster).
   *
   * The composed route is available. Adoption is offered only when the server
   * advertises the mode and admission still enforces its policy and approvals.
   */
  adoptWorkspace: {
    method: 'POST',
    path: '/workspaces/adopt',
    served: true,
    capability: 'adopting an existing cluster you already operate',
  },

  /**
   * Which lifecycle modes, providers and operations are actually available, and
   * the separate control-plane / provider / workspace readiness readings.
   * Without this the UI cannot claim any workspace is execution-ready.
   */
  capabilities: {
    method: 'GET',
    path: '/capabilities',
    served: true,
    capability: 'reporting which workspace features and providers this environment supports',
  },
  /**
   * Read-only validation of create/adopt inputs returning the reviewable plan:
   * verified target, ownership, capacity, cost-or-unknown, required approval.
   */
  previewWorkspace: {
    method: 'POST',
    path: '/workspaces/preview',
    served: true,
    capability: 'reviewing the exact plan, capacity and cost before anything is created',
  },
  /** Durable operation state, addressed by operation ID. */
  getOperation: {
    method: 'GET',
    path: '/operations/{operation_id}',
    served: true,
    capability: 'tracking a submitted operation through to completion',
  },
  /**
   * Recover the receipt of a submission whose reply was lost, by the original
   * scoped idempotency identity. Without it, a lost reply cannot be resolved
   * without risking a second workspace.
   */
  recoverOperation: {
    method: 'GET',
    path: '/operations/by-idempotency/{idempotency_key}',
    served: true,
    capability: 'recovering the result of a submission whose reply was lost',
  },
} as const satisfies Record<string, EndpointDeclaration>;

export type EndpointName = keyof typeof ENDPOINTS;

/**
 * The capability feature flag that must be advertised before a create may be
 * submitted with a client-chosen operation identity.
 *
 * WHY SUBMISSION IS REFUSED WITHOUT IT
 * ------------------------------------
 * `POST /workspaces` today accepts a body validated by a Pydantic model that has
 * no operation-identity field. Pydantic ignores unknown fields by default, so a
 * client sending one gets a 201 and *believes* the submission was idempotent
 * while the server deduplicated nothing. A retry after a lost reply would then
 * build a second workspace and spend twice — the exact outcome the idempotency
 * requirement exists to prevent, arrived at through an apparently successful
 * request.
 *
 * Silently-ignored is the worst case, so both clients require the server to say
 * it honours the identity before any create is sent. `adp-superplane.py` gates on
 * the same string (#5637's `create_idempotency_unavailable`) so the two clients
 * cannot diverge on when a create is safe.
 */
export const CREATE_IDEMPOTENCY_FEATURE = 'create-operation-id-v1';

/** Field name carrying the client's operation identity in a create body. */
export const OPERATION_ID_FIELD = 'operation_id';

/**
 * WHY THE IDEMPOTENCY IDENTITY TRAVELS IN THE BODY AND NOT A HEADER
 * -----------------------------------------------------------------
 * `Idempotency-Key` as a header is the more conventional design and it cannot
 * work here: the ADP gateway domain proxy rebuilds the upstream request with
 * exactly two headers, `Authorization` and `Content-Type`, and forwards nothing
 * else. A client header would be dropped in transit with no error — the request
 * would succeed, deduplicate nothing, and report success. A body field survives,
 * and the domain's own provider-operation records are already keyed on a body
 * `idempotency_key`, so the body is also where the server already looks.
 */
export const IDEMPOTENCY_TRANSPORT = 'body' as const;

/** Fill `{param}` templates. Values are percent-encoded as single segments. */
export function resolvePath(
  endpoint: EndpointDeclaration,
  params: Readonly<Record<string, string>> = {},
): string {
  const path = endpoint.path.replace(/\{(\w+)\}/g, (_match, name: string) => {
    const value = params[name];
    if (value === undefined || value === '') {
      throw new Error(`Missing path parameter "${name}" for ${endpoint.path}`);
    }
    // `encodeURIComponent` so a value containing "/" cannot add a path segment
    // and reach a different route than the one declared above.
    return encodeURIComponent(value);
  });
  return `${DOMAIN_BASE}${path}`;
}

// ---------------------------------------------------------------------------
// Wire types, derived from the domain API's response emitters.
// ---------------------------------------------------------------------------

/**
 * A workspace as `GET /workspaces` reports it.
 *
 * `status` and `cluster_health` are deliberately separate and both optional-ish:
 * a workspace row can exist with `status: "Provisioning"` and no cluster health
 * at all. `last_heartbeat` is the observation *time*, kept so the UI can say how
 * stale a reading is instead of presenting it as current.
 */
export interface WorkspaceSummary {
  id: string;
  org_id: string;
  name: string;
  display_name: string;
  isolation_mode: string;
  status: string;
  is_default?: boolean;
  cluster_health?: string | null;
  last_heartbeat?: string | null;
  created_at: string;
  updated_at: string;
  provisioning_operation_id?: string | null;
  operation_state?: OperationState;
}

export interface WorkspaceListResponse {
  workspaces: WorkspaceSummary[];
  total: number;
}

/**
 * The four validation readings of a provider connection, kept separate.
 *
 * The domain contract emits four fields and computes no aggregate, on purpose:
 * "the credential is valid but its permissions are insufficient" is a different
 * operator action from "the credential is invalid". A single `ok` at the wire
 * boundary would undo that distinction for every consumer, so this client keeps
 * them apart too. `observed_capacity: null` means capacity was not measured —
 * which is not the same as measuring zero.
 */
export interface ValidationReading {
  credential_valid: boolean | null;
  permissions_sufficient: boolean | null;
  quota_available: boolean | null;
  observed_capacity: number | null;
  checked_at: string;
  detail?: string | null;
}

/**
 * A vault credential, as onboarding is allowed to see it: a reference.
 *
 * There is no field here for a secret value, and that is the design. The value
 * is entered through ADP's vault, which is its system of record; onboarding binds
 * an id. Because the type has nowhere to put a value, no component can render one
 * and no request can carry one even by mistake.
 */
export interface CredentialRef {
  credential_id: string;
  /** Provider family the credential is for, e.g. `bedrock`. */
  service: string;
  /** Human-chosen name, for recognizing it in a picker. */
  label: string;
}

/**
 * Field names that would indicate secret material rather than a reference.
 *
 * Exported so both clients' tests can assert the same list against anything they
 * are about to send or store, instead of each inventing its own idea of what a
 * secret looks like and missing a case.
 */
export const SECRET_MATERIAL_FIELDS = [
  'secret',
  'secret_value',
  'secretValue',
  'password',
  'token',
  'access_token',
  'accessToken',
  'api_key',
  'apiKey',
  'private_key',
  'privateKey',
  'credential_value',
  'credentialValue',
  'aws_secret_access_key',
  'awsSecretAccessKey',
] as const;

/**
 * Throw if `value` contains a field that looks like secret material, at any depth.
 *
 * A tripwire, not a sanitizer: it is called from tests over request bodies and
 * persisted state so that a future change which starts carrying a secret through
 * onboarding fails loudly rather than shipping. Sanitizing would hide the defect;
 * the point is to make it impossible to introduce unnoticed.
 */
export function assertNoSecretMaterial(value: unknown, where: string): void {
  const walk = (node: unknown, path: string): void => {
    if (Array.isArray(node)) {
      node.forEach((item, index) => walk(item, `${path}[${index}]`));
      return;
    }
    if (typeof node !== 'object' || node === null) return;
    for (const [key, child] of Object.entries(node)) {
      if ((SECRET_MATERIAL_FIELDS as readonly string[]).includes(key)) {
        throw new Error(
          `${where} carries a field named "${key}" at ${path}. Onboarding must ` +
            `carry credential references, never secret values.`,
        );
      }
      walk(child, `${path}.${key}`);
    }
  };
  walk(value, where);
}

/** A provider connection. Carries a credential *reference*, never a value. */
export interface ProviderConnection {
  connection_id: string;
  provider: string;
  status: string;
  workspace_id: string;
  credential: { credential_id: string; service: string; label: string };
  binding?: {
    credential_id: string;
    workspace_id: string;
    bound_by: string;
    bound_at: string;
  };
  admits_new_work: boolean;
  allows_renewal: boolean;
  validation?: ValidationReading;
  limitation?: string | null;
}

/** The two supported onboarding modes. */
export type OnboardingMode = 'managed' | 'adopt';

/**
 * The isolation modes `POST /workspaces` actually accepts.
 *
 * WHY THIS IS A CLOSED SET AND WHERE IT COMES FROM
 * -----------------------------------------------
 * `CreateWorkspaceRequest.isolation_mode` is
 * `Field(default="dedicated", pattern="^(dedicated|namespace|research)$")`, and
 * `models/workspace.py` repeats the same three in `VALID_ISOLATION_MODES`. Anything
 * else is a 422. The create form used to hardcode `'shared'` — a value the schema
 * has never accepted — so a submission that passed every client-side check was
 * rejected by the server, and the user had no control over the one field that
 * decides how their workspace is isolated.
 *
 * Mirrored as a literal union rather than a free string so that a mode the schema
 * does not accept is a type error here, not a 422 discovered by a user. It is a
 * mirror of a server-side constraint and must be updated when that pattern changes
 * — `__tests__/contract.test.ts` asserts the two agree.
 */
export const ISOLATION_MODES = ['dedicated', 'namespace', 'research'] as const;

export type IsolationMode = (typeof ISOLATION_MODES)[number];

/**
 * Isolation modes the deployment says it serves, or the schema's set when it has
 * not said.
 *
 * Intersected rather than replaced: a server may advertise fewer modes than the
 * schema allows, but a mode outside the schema cannot be offered whatever it
 * advertises, because the request would be rejected. An empty or absent
 * advertisement falls back to the full schema set — that is not fail-open, since
 * every member is a value the server's own validator accepts.
 */
export function offeredIsolationModes(
  capabilities: Capabilities | null,
): readonly IsolationMode[] {
  const advertised = capabilities?.isolationModes ?? [];
  const allowed = advertised.filter((mode): mode is IsolationMode =>
    (ISOLATION_MODES as readonly string[]).includes(mode),
  );
  return allowed.length > 0 ? allowed : ISOLATION_MODES;
}

/**
 * Whether these inputs can be submitted at all, and why not when they cannot.
 *
 * Mirrors `CreateWorkspaceRequest`'s validators so the refusal is stated in the
 * form rather than arriving as a 422 the user has to interpret:
 * `research_requires_account` makes an account mandatory for research isolation,
 * and both budget guardrails are `ge=0`.
 *
 * Returns the reasons rather than a boolean, because "cannot submit" without the
 * reason is the least useful thing a form can say.
 */
export function inputRefusals(inputs: OnboardingInputs): readonly string[] {
  const refusals: string[] = [];
  if (inputs.name.trim() === '') {
    refusals.push('A workspace name is required.');
  }
  if (inputs.mode === 'adopt' && !inputs.clusterReference?.trim()) {
    refusals.push('The cluster to adopt is required.');
  }
  if (inputs.isolationMode === 'research' && !inputs.account?.trim()) {
    // The server raises this itself; saying it here means the user is told before
    // a submission is attempted rather than after it is rejected.
    refusals.push(
      'Research isolation requires a target cloud account, which the domain API ' +
        'enforces on every research workspace.',
    );
  }
  if (inputs.budgetMaxDailyUsd !== undefined && !(inputs.budgetMaxDailyUsd >= 0)) {
    refusals.push('A daily budget cap cannot be negative.');
  }
  if (inputs.budgetMaxGpus !== undefined && !(inputs.budgetMaxGpus >= 0)) {
    refusals.push('A GPU cap cannot be negative.');
  }
  return refusals;
}

/** Inputs for a create or adopt submission, before any server validation. */
export interface OnboardingInputs {
  mode: OnboardingMode;
  name: string;
  /**
   * Domain isolation mode — one of the three the server's schema accepts.
   *
   * Typed as the union rather than `string` so a value like `'shared'`, which the
   * form once hardcoded and the schema has never accepted, cannot be constructed.
   */
  isolationMode: IsolationMode;
  /** Target cloud account. Required by the domain for research isolation. */
  account?: string;
  region?: string;
  /** BYOC only: the cluster the user already operates. */
  clusterReference?: string;
  budgetMaxDailyUsd?: number;
  budgetMaxGpus?: number;
}

/**
 * A reviewed plan. Submission is bound to `revision`, so a plan the user did not
 * see cannot be submitted under a confirmation they gave for a different one.
 */
export interface OnboardingPlan {
  revision: string;
  mode: OnboardingMode;
  target: { account: string | null; region: string | null; cluster: string | null };
  ownership: string;
  requestedCapacity: string;
  /** `null` when the server has no estimate. Never rendered as zero. */
  costEstimate: { amountUsd: number; currency: string; asOf: string; assumptions: string } | null;
  approvalRequired: boolean;
  approvalRequest?: Record<string, unknown> | null;
}

/**
 * What the server says it can actually do.
 *
 * WHY THIS IS A SET OF STRINGS AND NOT A SET OF BOOLEANS
 * -----------------------------------------------------
 * An absent feature name and a feature named `false` are the same answer — "not
 * advertised" — and the fail-closed reading of both is "no". A boolean field per
 * feature invites the other reading, where a field the server has not yet heard
 * of deserializes to `false` on one revision and is simply missing on another,
 * and the client has to know which. Membership answers both identically.
 *
 * `modes` and `providers` are what they sound like: the lifecycle modes and
 * providers this deployment will accept, so the UI offers adopt only where adopt
 * exists rather than rendering a button that 404s.
 */
export interface Capabilities {
  /** Advertised feature names. {@link CREATE_IDEMPOTENCY_FEATURE} is one. */
  features: readonly string[];
  /** Lifecycle modes this deployment serves. */
  modes: readonly OnboardingMode[];
  /** Provider identifiers connections may be registered against. */
  providers: readonly string[];
  /**
   * Isolation modes this deployment will accept, when it says.
   *
   * Empty means it did not say, which is not the same as "none" — see
   * {@link offeredIsolationModes}, which falls back to the schema's own set rather
   * than offering the user nothing.
   */
  isolationModes: readonly string[];
}

/**
 * Whether a capability report advertises a feature.
 *
 * Fail-closed by construction: a `null` report (no observation, malformed
 * response, or an older server that has no such concept) is not a feature, and
 * this is the single place that decision is made so no caller can reach for
 * `features?.includes(...)` and get `undefined` — which is falsy, but only by
 * luck, and is the shape that produces `if (!x)` reading as "supported" the
 * moment somebody inverts a condition.
 */
export function advertises(
  capabilities: Capabilities | null,
  feature: string,
): boolean {
  return capabilities !== null && capabilities.features.includes(feature);
}

export interface OperationApproval {
  approval_id: string;
  workspace_id: string;
  result: 'pending' | 'allowed-once' | 'rejected';
  can_decide: boolean;
  action: string;
  target: Record<string, string>;
  plan_digest: string;
  envelope: { max_resource_units: number | null; max_runtime_seconds: number | null; max_cost_micros: number | null };
  expires_at: string;
  revoked: boolean;
}

/** Terminal and non-terminal operation states, kept distinct from each other. */
export type OperationState =
  | 'accepted'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'unknown';

/**
 * The durable receipt of a submission.
 *
 * `state: 'unknown'` is a first-class value, not an error: an operation whose
 * outcome has not been authoritatively reconciled must stay unknown rather than
 * being guessed at, because guessing "failed" invites a resubmission that builds
 * a second workspace and guessing "succeeded" hides infrastructure that exists.
 */
export interface OperationReceipt {
  operationId: string | null;
  idempotencyKey: string;
  state: OperationState;
  workspaceId: string | null;
  phase?: string | null;
  reason?: string | null;
  observedAt: string;
  retryable?: boolean;
}

/**
 * An action the clients could not perform, with the reason and its source.
 *
 * THE DIVISION OF LABOUR BETWEEN THESE FIELDS
 * -------------------------------------------
 * `reason` and `endpoint` are the stable machine-readable pair: scripts and the
 * CLI's JSON output branch on them, and their values must not be reworded for
 * readability. `detail` and `capability` are prose for a human and carry no
 * guarantee of stability — nothing should parse them.
 *
 * Keeping both means a diagnostic can explain the situation in the user's terms
 * without automation having to scrape that explanation.
 */
export interface Unavailable {
  reason: UnavailableReason;
  detail: string;
  /** Endpoint whose absence caused this, when that is the cause. */
  endpoint?: EndpointName;
  /**
   * The product capability that is missing, in the user's terms. Never a story
   * or ticket reference — see {@link EndpointDeclaration.capability}.
   */
  capability?: string;
}

/** Endpoint names this client will not send because the proxy does not serve them. */
export function unservedEndpoints(): EndpointName[] {
  return (Object.keys(ENDPOINTS) as EndpointName[]).filter(
    (name) => !ENDPOINTS[name].served,
  );
}

/** The {@link Unavailable} to report when an action needs an unserved endpoint. */
export function unavailableFor(name: EndpointName): Unavailable {
  // Widened to the interface deliberately. `as const satisfies` narrows each
  // entry to its own literal type, on which `capability` is absent for the served
  // endpoints — so reading it off the narrow union does not compile. The widening
  // is what makes the optional field readable for any endpoint.
  const endpoint: EndpointDeclaration = ENDPOINTS[name];
  // Leads with the capability, because that is the part the reader can act on:
  // they can ask whether their platform intends to enable it. The method and
  // path follow for whoever is diagnosing the deployment, and the machine-
  // readable `reason`/`endpoint` pair is what automation reads.
  const capability = endpoint.capability
    ? `This environment's Superplane API does not support ${endpoint.capability} yet.`
    : `This environment's Superplane API does not support this action yet.`;
  return {
    reason: 'not-deployed',
    detail: `${capability} (${endpoint.method} ${DOMAIN_BASE}${endpoint.path} is not available.)`,
    endpoint: name,
    capability: endpoint.capability,
  };
}
