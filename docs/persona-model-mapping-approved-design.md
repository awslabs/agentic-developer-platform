# Approved design: per-invoker persona-to-model mapping

**Epic:** [#5417](https://github.com/aws-e/adp/issues/5417)

**Status:** Approved canonical design

**Approved:** 2026-09-18

**Owner:** ADP platform architecture

This document is the single normative design for persona-to-model mapping in
ADP. The story-local design notes listed below are supporting analysis. If a
story-local note conflicts with this document, this document wins.

Supporting designs:

- [PMM-01 vocabulary and precedence](design-notes/5417-per-invoker-persona-model-mapping.md)
- [PMM-02 identity, schema, API and audit](design-notes/5419-persona-model-preference-schema-and-api.md)
- [PMM-03 persona and model catalogues](design-notes/5420-persona-and-model-catalogue.md)
- [PMM-04 Agent Models UI](design-notes/5422-agent-models-ui.md)
- [PMM-05 `adp models` CLI](design-notes/5423-cli-persona-model-commands.md)
- [PMM-06 trusted policy snapshot](design-notes/5424-chain-model-policy-snapshot.md)
- [PMM-07 resolver integration](design-notes/5425-persona-model-resolver-wiring.md)
- [PMM-08 attribution and alerts](design-notes/5426-persona-chain-cost-attribution.md)
- [PMM-09 live gate and enforcing flip](design-notes/5427-pmm09-default-consolidation-and-enforcing-flip.md)

## 1. Outcome and boundary

Every human or service principal can save a model choice for each configurable
agent persona. A root invocation uses the root principal's saved mappings for
the whole chain, while every hop selects for its own persona. A mapping chooses
a desired model; it never grants model access, budget, destination access or a
policy exemption.

The design covers:

- human self-service, service-principal self-service and authorized human
  administration of service principals;
- UI and CLI configuration;
- direct, webhook, GitLab, EventBridge, orchestration, chat and ARC/GitHub
  Actions execution paths;
- immutable chain policy, gateway-authoritative resolution, live admission,
  audit, spend attribution and retirement warnings;
- report-only rollout, live proof, enforcement and rollback.

The design does not make classifiers, summarizers, probes, pricing jobs, test
fixtures or unrelated Agent Context workloads adopt persona-execution defaults.
Repository settings may restrict admission, but may not override a root
principal's mapping.

## 2. Canonical vocabulary and ownership

| Concept | Canonical meaning | Owner |
|---|---|---|
| Tenant | The active ADP organization in which the invocation and preference exist | Existing tenancy layer |
| Human principal | Canonical `users.id` | Existing identity layer |
| Service principal | Opaque immutable `canonical_service_principal_id` minted by ADP | PMM-02 |
| Service alias | Tenant- and source-qualified external subject that resolves to one canonical service principal | PMM-02 |
| Persona | Stable key from the authoritative persona registry | PMM-03 |
| Compatibility class | Stable unversioned harness family: initially `claude-agent-sdk`; reserved `codex-sdk` | PMM-03 |
| Harness contract revision | Versioned request/response compatibility contract within a class | PMM-03 |
| Canonical model ID | Pinned provider identifier; never a floating `latest` alias | PMM-03 |
| Preference | Tenant + principal kind + canonical principal ID + persona → canonical model ID | PMM-02 |
| Class default | Versioned platform default for one compatibility class | PMM-02 storage; PMM-03 validation |
| Runtime posture | Versioned `disabled`, `report_only` or `enforcing` policy | PMM-02 storage; PMM-07 consumption |
| Policy snapshot | Immutable root-chain policy facts stored by gateway authority | PMM-06 |
| Resolution decision | Short-lived gateway-issued decision for one hop and persona | PMM-06/PMM-07 |

All current personas execute through the Claude Agent SDK and therefore map to
`claude-agent-sdk`. The existing `codex` persona remains a Claude SDK outer
agent that invokes Codex as a bounded tool; it is not a native `codex-sdk`
persona. Epic #5433 will register native `gpt-*` personas into the PMM-03
registry and must prove a separate Codex/GPT default. Cross-class fallback is
forbidden.

`us.anthropic.claude-sonnet-4-6` is the candidate Claude-class default. It is
not active evidence until a bounded invocation succeeds using the actual Claude
Agent SDK request shape in the target destination.

## 3. Decisions of record

1. Personal selection and organization access policy are separate axes. Policy
   may refuse a selection but must never substitute another model.
2. A valid direct `/model` override applies to the directly invoked hop only.
   Invalid, incompatible or disallowed overrides fail closed.
3. Platform catalogue, compatibility, tenant policy, service restrictions and
   fresh destination invocability become real admission gates after a
   report-only migration.
4. Defaults are per compatibility class. A missing or unproven class default is
   an actionable platform-readiness failure, never a fallback to another class.
5. Gateway authority signs decisions. Workers never receive signing secrets and
   never hold a long-lived offline-verifiable policy bearer token.
6. Harness compatibility is checked for the actual persona at every hop.
7. PMM-02 owns canonical service-principal identity, alias resolution and the
   manageable-principals surface.
8. PMM-03 owns persona-to-class metadata and destination-aware compatibility and
   invocability evidence.
9. Gateway is the only authoritative selector. Edge and worker artefacts may
   reject obviously invalid input using generated catalogue data, but may not
   select a different model.
10. Root ownership follows the authenticated initiator, not the bot or worker
    credential that executes the job.

## 4. Canonical data model

### 4.1 Service-principal identity

`service_principals` is the canonical entity table:

- `id`: opaque immutable `canonical_service_principal_id`;
- `org_id`;
- `display_name`;
- `status`: active, suspended or retired;
- audit timestamps and actor IDs.

`service_principal_aliases` maps an external subject to that entity:

- `org_id`;
- `alias_source`: `sa_registration`, `agent_registry`, `cognito_m2m`,
  `eventbridge` or `github_actions`;
- `alias_id`;
- `canonical_service_principal_id`;
- `status`, `created_at`, `created_by`, `revoked_at`, `revoked_by`.

There is at most one active row for `(org_id, alias_source, alias_id)`. The
migration must use a PostgreSQL partial unique index, or a portable expression
index whose SQLite tests preserve the same revoke-and-re-register semantics.
One canonical principal may have multiple aliases.

Re-registering a revoked or recycled alias creates a new canonical principal by
default. Re-linking it to an existing principal is a separate authorized,
audited operation. Raw `service_accounts.id`, `agent_name`, Cognito `client_id`,
role ARN and caller-supplied text never own a preference.

`Organization.cognito_client_ids` remains an approved-client list, not a
service-principal identity. Cognito M2M self-service is refused until the client
has a tenant-bound alias registration.

Authentication adds an optional canonical principal field to `TokenContext`.
Existing `user_id` semantics remain unchanged for compatibility. Preference
handlers require the canonical field for service callers and never silently
fall back to the raw subject.

### 4.2 Preferences and platform settings

`persona_model_preferences` contains:

- `org_id`, `principal_kind`, `principal_id`, `persona_key`;
- `canonical_model_id` and the submitted `requested_alias`;
- monotonic `revision` for compare-and-set writes;
- `created_at`, `updated_at`, canonical `updated_by` and
  `updated_by_source` provenance.

The unique key is
`(org_id, principal_kind, principal_id, persona_key)`. The service layer also
asserts that a canonical ID has exactly one principal kind.

`persona_model_policy_settings` contains one row per compatibility class:

- `compatibility_class`;
- candidate and active canonical default model IDs;
- `harness_contract_revision`;
- monotonic `revision`;
- `enforcement_posture` and posture revision;
- canonical updating actor and timestamps.

Only a platform administrator can change class defaults or posture. A candidate
cannot become active until PMM-09 records real harness proof.

### 4.3 Catalogues and invocability evidence

The persona catalogue is derived from the authoritative persona source with a
build-time parity test. Each row includes persona key, display metadata,
configurability, compatibility class and harness contract revision.
`pt-superpower` remains visible but non-configurable until #4037 is fixed.

The selectable model set is the intersection of:

1. platform-supported canonical models;
2. compatibility with the target persona's harness contract;
3. tenant/organization policy;
4. service-principal restrictions, when applicable;
5. fresh proof through the resolved destination.

Invocability evidence is durable and keyed by destination, account, region,
canonical model ID, compatibility class, harness contract revision and request
shape revision. It records outcome, timestamp, expiry and provider request ID.
Pricing data, model listings and marketplace agreement status are not proof.

Probing ships disabled with a zero token/spend budget. No UI read invokes a
model. PMM-09 may enable bounded nightly and catalogue/destination-change probes
only after target-account and spend-ceiling approval.

Fable remains unavailable unless this evidence records a successful bounded
real-harness invocation. Existing friendly allowlist values that match no
canonical ID must be corrected in report-only mode before enforcement.

## 5. API, UI and CLI

### 5.1 API

The FastAPI self router exists once. Human JWT callers use `/me/...`; SigV4
service callers use external `/agent/me/...`, whose existing API Gateway proxy
strips `/agent` and reaches the same handler.

Canonical routes:

- `GET /me/persona-models`
- `GET /me/persona-models/catalog?persona_key=...`
- `GET /me/persona-models/explain/{persona_key}`
- `PUT /me/persona-models/{persona_key}`
- `DELETE /me/persona-models/{persona_key}`
- `GET /me/persona-models/manageable-service-principals`
- `GET /service-principals/{canonical_id}/persona-models`
- `GET /service-principals/{canonical_id}/persona-models/explain/{persona_key}`
- `PUT /service-principals/{canonical_id}/persona-models/{persona_key}`
- `DELETE /service-principals/{canonical_id}/persona-models/{persona_key}`

Self routes derive the principal from authentication and accept no target at any
position. Administered routes require a JWT-authenticated human with
`ORG_UPDATE`, an explicit same-tenant check and a canonical ID returned by the
manageable-principals surface. A service principal can manage only itself.

Writes carry `expected_revision`. An absent value means create-only. A conflict
returns `409` with the current safe row representation. Validation refusals use
stable `422 {reason, message}` codes. Clients never echo arbitrary server error
bodies.

### 5.2 UI

The page is **Agent Models** at `/settings/agent-models`. It is one current-UI
page with one `journeys.ts` link to the same route. The browser treats canonical
IDs as opaque, never joins identity aliases, never derives management authority
and requests the catalogue for each persona.

The backend-served `agent_models` feature flag is strict and defaults false in
the backend, frontend fallback, Kubernetes manifest and deployment workflow.
PMM-09 enables it only after enforcement readiness. A deep link is also gated.

The ordinary user flow is choose a model, save, and trigger the persona normally.
Signed decisions, policy snapshots, harness versions, probe evidence and rollout
postures are platform implementation details. They must not add user setup steps
or appear in the normal settings page or human-readable CLI output. Keep their
structured fields available to operators through APIs and CLI JSON. Show model
names, prices, saved/default selection, availability and actionable errors in
plain language; never hide a refusal or claim an unavailable model is ready.
Changes apply to new root runs and their descendants; running chains retain their
existing settings. Operators own readiness and model-availability maintenance.

### 5.3 CLI

The supported surface is:

```text
adp models catalog --persona KEY [--json]
adp models mappings list [--service-principal ID] [--json]
adp models mappings set --persona KEY --model ALIAS [--service-principal ID] [--dry-run] [--yes] [--json]
adp models mappings reset --persona KEY [--service-principal ID] [--yes] [--json]
adp models explain --persona KEY [--service-principal ID] [--json]
adp models service-principals list [--json]
```

Human self and authorized human administration use the existing JWT transport.
Service self-management uses the same schemas over the SigV4 `/agent` edge and
derives identity server-side; it accepts no principal argument. The signer
requires refreshable workload or demonstrably temporary credentials and never
falls back to a bearer token or long-lived static credentials.

Stale evidence is exit 4 and never a successful write. A 409 causes one safe GET
re-read and reports the result as current at the time of that re-read.

## 6. Selection, snapshot and runtime flow

### 6.1 Selection and admission

For each hop, selection is exactly:

1. valid explicit direct override, for this hop only;
2. saved mapping for the trusted root principal and this persona;
3. active proven default for this persona's compatibility class.

A saved but broken mapping does not fall through to the default. It fails with
an actionable reason. After selection, every hop rechecks live compatibility,
catalogue lifecycle, tenant policy, service restrictions, destination evidence,
compliance, budget and rate limits. The snapshot freezes the chosen policy facts,
not revocations or other live admission gates.

### 6.2 Trusted root and immutable policy

The coherent creation protocol is:

1. Ingress creates the protected execution/authority record and an invocation
   ID from authenticated event facts. The queue envelope contains that existing
   reference; it does not contain authoritative model mappings.
2. Before publication, the existing fail-closed SigV4 work-admission call sends
   only the invocation ID. Gateway verifies the protected dispatch, tenant,
   root and grant, reads Postgres preferences/settings, creates the immutable
   policy snapshot and stores it plus `snapshot_digest` in worker-unwritable
   authority storage keyed by the invocation ID.
3. Admission returns success without mutating the already-digested queue
   envelope. Publication occurs only after that success.
4. Worker bootstrap proves workload, run, tenant and root binding. Gateway loads
   the protected snapshot and returns a short-lived `adpe1` assertion bound to
   the workload, chain, audience and snapshot digest.
5. Before each persona/model hop, gateway resolves the persona against the
   protected snapshot, rechecks live admission gates and issues a signed
   per-hop resolution decision containing persona, canonical model, source,
   compatibility class, mapping/default/catalogue/policy revisions,
   `snapshot_digest`, audience and expiry.
6. Assertions expire within 30 seconds and are reissued per hop. The worker
   consumes the decision; it does not contain an independent resolver and never
   receives a signing secret.

The durable snapshot digest stays constant for the chain. The short-lived token
does not.

### 6.3 Invocation-path coverage

The gateway contract applies to GitHub webhook, GitLab, EventBridge,
agent-to-agent, orchestration engine, chat and ARC/GitHub Actions paths.

For human issue-comment, `issues:labeled` and human `workflow_dispatch` events,
the canonical human initiator remains the root. Scheduled, workflow-to-workflow
and service-to-service runs without an authenticated human initiator resolve a
tenant-bound registered canonical service principal and fail closed when it is
missing. The App, bot or worker execution identity is audit attribution only.

ARC currently invokes Bedrock directly, so it must obtain a gateway-authoritative
resolution before setting the harness model, or route model traffic through the
gateway. Local catalogue validation alone is insufficient and ARC must not
receive an internal shared API key.

## 7. Audit, usage evidence and retirement alerts

Protected execution and snapshot facts, never worker-supplied persona text,
populate usage evidence:

- tenant, root invocation and chain IDs;
- persona and compatibility class;
- canonical preference-owner kind and ID;
- authenticated/billing principal and approving-human audit identity as
  separate fields;
- requested and resolved model, resolution source;
- mapping, default, catalogue, policy, snapshot and harness revisions;
- complete pricing-policy revision tuple and price source;
- provider request ID, token counts, cost status and amount.

Unknown, partial and known-zero cost remain distinct. Service-rooted spend is
attributed to the canonical service principal whose mapping selected the model;
the approving human never silently replaces it.

Retirement warnings appear to mapping owners in Agent Models and
`adp models mappings list`/`explain`. Existing asynchronous notifications are
for environment operators only. A durable transition/outbox record keyed by
mapping, model and lifecycle revision is claimed conditionally before publish;
only the winner publishes. Delivery failure becomes retryable state with a
lease. The system does not claim per-owner push notification until a separate
notification-address capability exists.

## 8. Rollout and dependency order

1. PMM-01 establishes this approved vocabulary and precedence.
2. PMM-02 and PMM-03 implement in parallel against the contracts above. PMM-02
   owns identity/storage/API; PMM-03 owns catalogue/compatibility/evidence.
3. PMM-04 and PMM-05 implement UI and CLI in parallel after the shared schemas
   are available.
4. PMM-06 implements authority snapshot/bootstrap, followed by PMM-07 resolver
   integration across every invocation path.
5. PMM-08 adds attribution, explainability and retirement warning delivery.
6. PMM-09 deploys to dev, proves the matrix, enables UI/report-only comparison,
   flips enforcement and proves rollback.

No developer story may silently redefine a canonical field or route from this
document. Shared-file conflicts are resolved by rebasing the later implementation
onto the already-merged contract owner.

The feature initially ships with `agent_models=false`, probes disabled at zero
budget and runtime posture `report_only`. Enforcement requires PMM-06 authority,
#3186/#5195, PMM-07 requester feedback for #2293, corrected allowlist data and
complete path coverage.

In `report_only`, the gateway computes and records the proposed resolution but
the existing legacy model assignment remains the model actually used. Existing
live access, budget and rate gates are never relaxed. Differences between legacy
and proposed outcomes are evidence for PMM-09; they do not silently change a
request before the enforcing flip.

Native `codex-sdk` personas do not block Claude-class rollout because none is
active yet. Enabling a `codex-sdk` persona is separately gated on #5433 and a
proven Codex/GPT default.

## 9. Deployment and rollback

Deployment follows `AGENTS.md` →
`docs/adp-platform-deployment/deploy-with-agent.md` →
`docs/adp-platform-deployment/deploy-quickstart.md`.

The confirmed dev target is AWS account `000000000101` via profile `example-profile`.
An existing environment uses `deploy-all.sh --update`, not the fresh-deploy
path. Each phase must be validated against real AWS and Kubernetes state; a
committed `.adp-deploy-state.json` is not evidence.

Merging code does not authorize deployment or model spend. Immediately before
live invocations, reconfirm STS identity and obtain a spend ceiling.

Operational rollback is:

1. set runtime posture to `report_only` through the audited versioned setting;
2. set `agent_models=false` to remove UI editing;
3. wait the measured bounded gateway cache TTL and verify decisions reverted;
4. use Kubernetes/application rollback only if code rollback is also needed.

An unknown posture revision fails closed in enforcing mode. Additive migrations
remain in place during operational rollback; destructive down-migration is not
part of an emergency rollback.

## 10. Required live acceptance evidence

| Area | Required proof |
|---|---|
| Human self | UI and CLI set/read/reset/explain for at least three personas using distinct invocable models |
| Human administration | Discover canonical service principals, administer one in-tenant, and refuse cross-tenant/unknown IDs |
| Service self | `adp models` through real SigV4/M2M transport with no target argument |
| Identity reconciliation | Postgres, Agent Registry and Cognito aliases resolve to the intended canonical principal, or fail as unregistered |
| Defaults | Claude candidate invokes through the actual Claude harness request shape before activation |
| Direct override | Valid override affects only the direct hop and never descendants |
| Chains | Human-rooted and service-rooted multi-hop chains preserve one snapshot digest while resolving each persona independently |
| Automated roots | Human-triggered ARC preserves the human root; scheduled/service ARC uses a registered service principal |
| Refusals | Invalid, org-disallowed, allowlist-disallowed, retired, stale, non-invocable and harness-incompatible choices fail before billable work |
| Evidence | Invocation cells record provider request IDs, tokens, cost and all revisions; refusal cells prove no billable call; configuration cells record API/audit IDs |
| Attribution | Usage names canonical preference owner separately from billing/authenticated principal and approving human |
| Shadow gate | Report-only comparison covers every invocation path with no unexplained divergence |
| Flip | Enforcement activates only after all prerequisite gates are green |
| Rollback | Runtime posture rollback completes within the measured cache bound without image rebuild |

The live evidence report must name account, region, timestamp and result per
model family. Partial success is reported as partial success; a working probe
mechanism does not certify a model that failed to invoke.

## 11. Approval and change control

This design is approved for implementation. Architecture questions identified
during implementation are resolved by amending this document in a reviewed PR,
not by creating a competing contract in a story branch. Deployment, paid probes
and the enforcing flip retain their separate operator gates.
