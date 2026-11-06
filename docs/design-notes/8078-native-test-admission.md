# Native test enrollment and model handoff (#8078)

Status: implemented **model-only application integration** and a **source-only
complete-lifecycle quote/SDK admission boundary**, verified with local
substituted provider transports. This source change does not enroll a real
fixture, issue a live credential or authorize a paid test. The approved admission
primitive is `5b636cb8a`; its accounting and replay rules remain in force. Paid
setup, tool sessions and cleanup have no installed all-in runtime, complete
production contract, HTTP dispatch handoff or settlement/cleanup continuation.
The tool HTTP path therefore refuses designated principals. Real tools evaluation
remains refused.

## Authority and proof boundaries

The initial runtime is one explicitly registered direct GitHub Actions
`workflow_dispatch` job at attempt `1`, using an existing designated fixture
principal in one ADP tenant. Its real Cognito login remains separate from the test
capability. Neither GitHub OIDC nor AWS producer proof is a human login. No human
JWT is synthesized; CI receives no personal or administrator refresh token.

| Fact | Authoritative source |
| --- | --- |
| Existing fixture principal and Cognito subject | Canonical ADP user and current tenant/team membership |
| Fixture designation and approving owner | Protected registration plus explicit authenticated owner approval |
| Existing tool consent and entitlement | Current tools policy/consent and entitlement records; enrollment cannot write them |
| CI job | `arc_model.github_claims` with a dedicated audience and exact repository ID/name, direct workflow/ref/SHA, source SHA, environment-bound subject, runner environment, actor, event, run and attempt |
| Registered producer | Existing `work_routes.verify_producer` AWS proof, bound to the complete exchange envelope and exact approved runner role |
| Source/runtime | Approved signed CI job and source; not independent attestation of an arbitrary local binary |
| Request and quote | Exact native request bytes/route, trusted provider quote and immutable admission journal |
| Spending and cleanup | Existing hierarchy ledgers plus the same $20 test accumulator, retained unknown holds and original owner cleanup obligations |

Reusable workflows are refused: they would also require a pinned and verified
`job_workflow_sha`. `source_sha256` hashes the canonical repository ID, Git source
SHA and workflow SHA tuple. It identifies approved source; it is not a downloaded
artifact digest or executable attestation. Native installation/version evidence
must still come from the approved workflow. Local enrollment, arbitrary fixture
headers, caller-supplied cost/consent assertions and ARC/deployment grants are not
substitutes for these authorities.

## Approval and capability

Issuer: `adp.tools-test-capability/v1`. Capability audience:
`adp-native-test-model-v1`, bound to the registered deployment. The separate
GitHub OIDC audience is `adp-native-test-enrollment-v1`.

An authenticated owner approves an existing designated fixture, exact canonical
membership, source/job/runner binding, scenario, model/route/tool allowlists and
finite dispatch/cleanup windows. The approval is immutable. Enrollment cannot
create a user, membership, entitlement, consent, model permission, destination
grant or cleanup exemption. The independently authenticated fixture login is
required for the CI exchange and every model call.

The capability is opaque and high entropy. Only its digest and server-owned
bindings are persisted; plaintext is returned once and excluded from logs and
evidence. Expiry equals the approved dispatch deadline, at most 15 minutes after
approval. The fixture login must be valid through that deadline at issuance and
is independently rechecked on every model admission. OIDC must remain valid
through exchange; it never becomes a login or refresh token. Revocation stops new
work without deleting spend, holds, approvals or original cleanup obligations.

`NativeTestEnrollment` requires the exact middleware/admission/store composition
before approval or issuance. The application installs `NativeTestRuntimeMiddleware`
after authentication and approval, before model identity, budget and rate limits.
The runtime discovers the current provider POST routes and checks their coverage
against the shared enforced prefixes and required middleware order. A new tagged
provider alias outside those prefixes is denied and invalidates registration.
This is an in-process source check, **not deployed coverage evidence** or binary
attestation. Missing or uncertain issuer, registration or verifier refuses.
Existing quote adapters refuse requests they cannot bound; there is no fallback
quote.

The HTTP control routes use actual Cognito JWT authentication and current
canonical membership: `POST /v1/native-tests/approvals`,
`POST /v1/native-tests/capabilities/exchange`, and
`POST /v1/native-tests/capabilities/revoke`. Proofs belong in bounded JSON bodies;
duplicate keys and invalid shapes are rejected without echoing credential input.
Responses, including authentication errors, use `Cache-Control: no-store`.
Exchange explicitly reports `scope: model_only` and `all_in_admission: unavailable`.

## Protected storage and issuance

`native_test_enrollment.py` defines strict documents in the protected Dynamo
table. All reads are consistent. None of these rows has a TTL.

| Key | Authority and writer |
| --- | --- |
| `TOOLS_TEST_DEPLOYMENT#{deployment}` / `CONFIGURATION` | Protected operator registration of deployment/tenant, exact approved CI job/source, route/model/tool allowlists, finite expiry and source coverage digest. Scope is `model_only`, or explicit `all_in` with exact effect registrations and additional lifecycle coverage. No gateway writer exists. |
| `TOOLS_TEST_SUBJECT#{subject}` / `DESIGNATION` | Global subject, canonical tenant/user/team/department, owner, deployment, revision and revocation epoch. Protected operator registration only; this slice has no registration writer. |
| `TENANT#{tenant}` / `TOOLS_TEST_APPROVAL#{run}` | Immutable explicit owner approval, job/source, tools/models/routes, deadlines, credential ID and meter epoch. Only the owner approval service writes it. |
| `TENANT#{tenant}` / `TOOLS_TEST_SLOT#{digest}` | Uniqueness fence for fixture subject + GitHub repository/run + scenario. Changing run UUID, attempt or source cannot reset the test allowance. |
| `TENANT#{tenant}` / `TOOLS_TEST_CAPABILITY#{credential}` | Digest-only credential and issuer/audience/deployment/approval/authority bindings, expiry, issuing/active/revoked state. Issuer and approving owner's revocation service only. |
| `TENANT#{tenant}` / `TOOLS_TEST_RUN#{run}` | Existing admission-authority format, cap exactly $20, immutable meter epoch and deadlines. |
| `TENANT#{tenant}` / `TOOLS_TEST_OP#{run}#{operation}` | Existing request journal and single-use dispatch CAS. |

Designation is permanent: revocation retains a tombstone, never deletes the row.
The optional source module `modules/gateway/infra/modules/native-test-authority`
defines a dedicated table with no TTL, deletion protection and point-in-time
recovery. Its resource policy denies row deletion, batch writes and PartiQL
deletion; only the distinct protected registrar may write designation and
deployment registration. Gateway writes are restricted to tenant journal keys,
and its alternate write/table-management paths are denied. Named CI roles have
no direct access. These resource denials also constrain broader identity-policy
allows. The module emits the minimum gateway identity policy but attaches
nothing, creates no registration records and is not instantiated by a deployment.
Protected adoption must validate those bindings and role trust, including that
gateway, fixture and CI cannot assume the registrar or write Redis ledgers.

A Dynamo transaction claims issuance before Redis is touched. The issuer creates
only an absent accumulator hash with the immutable anchor and absolute meter
expiry, then conditionally activates the same capability plus authority. A
pre-existing hash or uncertain acknowledgement refuses. Replay cannot rotate a
credential or reset the meter. Redis and Dynamo are not one transaction: lost
responses can leave an issuing record, meter, or active digest whose plaintext
was not delivered. There is no reinitialization, rotation, takeover or automatic
recovery API in this version. Reconciliation requires the original records; a
new UUID is not recovery.

Admission transactions fence the exact deployment configuration, designation and
active capability alongside the run authority. Revocation between verifier reads
and dispatch CAS therefore refuses the CAS. SQL membership, consent and entitlement are reread
before approval, issuance, preparation and dispatch. SQL, Redis, Dynamo and
provider submission are not one distributed transaction.

## Mandatory model handoff

Every authenticated subject is looked up independently of headers and account
kind. Only a negative consistent registry read permits ordinary admission. A
designated subject cannot omit the capability, change tenant/authentication type,
or use an unsupported paid route to shed test scope. An unknown, expired,
revoked, wrong-audience or mismatched capability refuses before provider I/O.
The signature-verified raw JWT subject is retained privately across service-client
aliasing, so that alias cannot turn a designated subject into an ordinary caller.
Disappearance between the two registry checks is refused rather than treated as
a fresh negative lookup.

The supported routes are `/v1/messages` and `/openai/v1/responses`, including
streaming. The middleware captures bounded exact bytes and a stable operation
UUID. `TestRunAdmissionService.prepare_model` owns the only budget admission,
reserving the original hierarchy targets and the $20 test target in the same Lua
call. Rate limits still apply. A replay returns status only, never a provider
handoff. Budget middleware skips its second admission and generic zero cleanup
only for the verified in-process test handoff and exact original reservation ID.

The prepared context, quote, request/reservation ID and all target objects pass
through routing and usage settlement. `begin_model_dispatch` consumes one intent
after awaited preparation, immediately before the provider handoff. Bedrock also
checks the minimum quote/session/authority/all-hold deadline inside its executor
worker immediately before boto3, so queued threads cannot submit expired work.
SDK retries must be disabled. Responses checks again at the HTTPX send handoff;
this does not promise when a remote provider receives bytes or completes
inference. After submission, a timeout remains an uncertain paid effect.

The integration permits the existing deterministic Anthropic-to-Bedrock
conversion and Responses inference-profile prefixing. Changed content, output
maxima, billing model, stream mode or request ID refuses. Header-driven Anthropic
beta changes and asserted agent run IDs are unsupported. The capability header
is removed before downstream logging and is in the header scrubber.

Existing trusted usage writers receive the original reservation and context.
Missing or partial usage retains admitted holds. Lost dispatch acknowledgement
never authorizes retry. Pre-dispatch errors also retain holds for owner
reconciliation: this slice adds no cleanup/release privilege. A started journal
is not proof of measured provider success; usage receipts remain authoritative.

## Protected deployment adoption

The application mount is mandatory. A deployment with no
`ADP_NATIVE_TEST_AUTHORITY_TABLE` has never bound a registry and therefore never
designated a subject, so ordinary model and tool traffic keeps its ordinary path
there; only a caller presenting native test headers is refused. **Once a table is
configured, it must stay readable.** With a configured but unreadable registry,
authenticated model and tool traffic refuses because the application cannot prove
a caller is undesignated. No feature switch or missing-table fallback can remove a
previously designated subject's test scope. A consistently undesignated caller
keeps ordinary behavior even if enrollment configuration is unavailable.

Enrollment additionally requires `ADP_NATIVE_TEST_DEPLOYMENT_ID`, the existing
`AGENT_AUTHORITY_TABLE` for current tools policy, and the configured Redis
reservation backend. Protected registration must bind the reviewed source
coverage digest and exact allowed job to this deployment. There is no public
registration writer. Registration is read consistently and fenced in approval,
issuance, preparation and final dispatch transactions; revocation remains
available after deployment registration is revoked. Offline tests and Terraform
mocked applies do not establish effective live IAM or deployed coverage.

## Complete lifecycle source boundary

The source issuer also defines the separate `adp-native-test-all-in-v1` audience.
It requires explicit `scope: all_in` owner approval and a protected all-in
deployment registration, whose effect contracts and lifecycle coverage are
checked in the same approval, issuance and dispatch transactions. Existing
model-only capabilities cannot authorize effects. All-in activation remains
unavailable until the concrete HTTP dispatch, original cleanup and settlement
runtime supplies its readiness proof; that runtime is not installed by this
source slice.

All-in issuance first creates an `issuing_setup` authority alongside the issuing
capability, then initializes only the absent original meter. The setup adapter
reserves the complete protected setup quote through the same strict hierarchy
and Lua boundary. Activation rechecks that original hold and conditionally
changes the exact staged authority and capability together. Staged authority
cannot authorize model or provider work. Unknown issuance or setup outcomes
retain their journal and holds; retry cannot create a new meter or capability.
Setup has no provider-dispatch method or release privilege. The source-only
issuer tests use an explicit substitute for the unfinished lifecycle runtime.

`NativeTestEffectAdmission` uses the original durable journal, strict hierarchy
checks and atomic Lua reservation against the same $20 test meter as model calls.
It requires a distinct server verifier's `verify_effect` method; the existing
model-only enrollment cannot supply tool authority. Every effect quote includes
provider compute/requests, model setup, Lambda handler/INIT/requests/storage,
KMS, Dynamo requests/storage, SQS, network, observability, shared allocation,
runner, evidence and original cleanup. Even a zero term needs a priced quantity
and enforced-limit evidence hash. Missing terms refuse. Policy `max_cost_usd`
values are ceilings and never become quotes on this path.

`ProtectedEffectQuoteAdapter` reads an exact pinned contract from
`TOOLS_TEST_DEPLOYMENT#{deployment}` / `EFFECT_CONTRACT#{contract}`. The existing
optional table policy protects this partition from gateway/CI writes. Each
contract binds a release digest, exact SDK envelope hash, tariff revision,
finite validity and complete component bounds. Its exact document is checked in
the quoted, admitted and final dispatch Dynamo transactions. There is no contract
writer or production tariff installed by this change; evidence hashes reference
registrar-reviewed proofs and do not manufacture missing resource bounds.

`NativeEffectSDKExecutor` is a concrete in-process consumer. It quotes and submits
the same fixed service, endpoint, region, SDK method and JSON parameters,
requires `total_max_attempts=1`, consumes the original dispatch CAS, then checks
the deadline inside the executor immediately before one SDK call. Replays return
status, and changed bytes/source/price/configuration refuse. Provider success,
failure or timeout does not release the original full lifecycle hold. The
complete cleanup allowance stays held; this is not yet a cleanup permit or a
trusted billed-usage settlement writer.

The Browser/Interpreter formula uses their documented fixed 1-vCPU/4-GB and
2-vCPU/8-GB resources. Billable duration is a separately evidenced finite bound:
the documented 28,800-second maximum may be reserved conservatively even when
behavior requires a 300-second session and termination within 600 seconds.
Those behavior deadlines do not promise a durable ledger closure deadline.
The formula rounds each component upward to a micro-dollar and supplies only
compute terms; it never establishes an all-in quote by itself.

The actual tools authorizer checks the protected registry before policy, rate or
budget work for call, list, recover and close, even without test headers. It
checks the raw signed subject, any mapped alias and canonical owner. Workload and
crew owners require their canonical Cognito subject; missing mapping refuses.
Negative reads preserve ordinary behavior; any designation, including revoked,
refuses until an all-in HTTP adapter is installed. A second read fences changes
during authorization. The facade forwards native proofs and the original public
request UUID/execution/TTL. This transport is not yet a paid dispatch capability;
an ordinary JSON allow receipt cannot consume the in-process SDK handoff.

Offline effect tests use synthetic complete quantities/prices, real Moto
transactions and fakeredis Lua. They cover shared model/effect caps, retained
unknown holds, source/endpoint/payload drift, final-transaction revocation, SDK
single submission and consistent-read refusal across tool authentication modes.
No source fixture supplies live credentials, quantities or tariff approval.

## Facade call handoff

The optional `NativeFacadeAdmission` composition carries a fresh all-in call
through `/internal/v1/tools/authorize` and the facade to
`/internal/v1/tools/native-dispatch`. Both internal routes require the verified
facade IAM role and the actual fixture JWT; native capability and operation
headers are additional authority. The dispatch proof is returned once, kept out
of public receipts, and persisted only as a digest in the original operation.
The dispatch route restores the original journal quote and reservation targets,
revalidates current authority and holds, and consumes the original intent CAS.
Replay returns status without proof. A lost acknowledgement cannot authorize a
second send.

A protected `facade_call_v1` contract approves an exact fixed tool payload.
Only operation identities and explicitly bounded original-owned opaque handles
vary; every actual quote still hashes the complete canonical request. The
gateway plans provider-compatible execution identities and one durable
stateful-session slot for the whole test. The facade checks original native run
ownership, makes one claim attempt, and consumes the gateway proof before local
execution storage or KMS work. KMS has one SDK attempt. The provider consumer
checks the deadline again after signing and streams a bounded application body.
That application bound does not establish a total network-transfer bound.
Native AgentCore response headers and body also share one 30-second aggregate
timeout; arriving chunks cannot renew it. Ordinary requests keep their existing
HTTPX timeouts. Expiry follows the existing unknown-result path without retry or
financial release. This bounds the local response wait only: provider work may
continue, and managed retries, final invocation horizons and billed quantities
still require independent evidence.

Native discovery uses one protected `tools/list` request. A paginated response
refuses without fetching another page. The gateway filters its existing owner
catalogue to approved tools; discovery does not invent grants or schemas. The
all-in scope can have no model permissions, allowing a tool-only fixture.

All-in behavior deadlines start at the original server approval timestamp,
returned as `run_started_at`. Delivery and exchange consume that same window:
new dispatch ends within 300 seconds and cleanup within 600 seconds. Native
reservations retain every ordinary hierarchy hold for at least the original
test meter lifetime. All-in reservations additionally write persistent retained
markers in the same admission Lua transaction. Ordinary admissions keep counting
those dollars after meter expiry and cannot prune them or reapply a key TTL.
Ordinary partial-usage finalizers cannot reduce these lifecycle holds. No elapsed
time, terminal provider state or failed request restores unsettled headroom.

## Original cleanup and pending accounting

`NativeTestLifecycle` authenticates the original fixture and capability for
cleanup even after new-work revocation. The immutable execution binding locates
the original quote, complete targets and reservation. Explicit close and the
independently authenticated reconciler share one durable cleanup-attempt CAS.
Replay or a lost acknowledgement never supplies another send. Cleanup neither
quotes nor reserves again, and a final 90-second margin prevents new sends near
the original 600-second horizon. Exhaustion preserves unknown execution records
and financial holds. The reconciler requires its exact configured IAM role at
`/internal/v1/tools/native-cleanup`; the facade role cannot use that route.

`GET /v1/native-tests/runs/{run_id}/status` requires the retained fixture JWT and
capability. It separately reports original funds, accounting and per-execution
cleanup proof. A complete retained hold yields `funding_status=held` and
`accounting_status=pending`; `settled_usd=0` means no proven settlement recorded,
not zero provider cost. Confirmed cleanup never changes either financial state.
Missing evidence yields `unknown`, and `governance_complete` remains false until
trusted complete settlement exists. External CloudFront access adds `/api` to
this path. Status responses are never cacheable.
The original setup allowance covers at most three attempted status reads. A
durable original-run CAS consumes each attempt before inventory work, including
a lost acknowledgement; the allowance is never reset or retried automatically.

`NativeTestRuntime` leaves both tool adapters absent by default. An explicit
protected `ADP_NATIVE_TEST_TOOL_BINDING` installs `NativeStatelessRuntime` for
setup, one-page discovery, fixed web search and original-owned close. It admits
only `websearch.search` with empty model and model-route grants. The complete
setup + search + two discoveries must fit the original $20 meter. Browser,
Interpreter and generic recovery are outside this production composition.

The runtime binding pins the deployment and reviewed release digest, authority
and execution tables, and distinct facade/reconciler IAM roles. Coverage checks
the actual mounted authorization, dispatch, cleanup and status handlers and the
runtime's exact adapters. The protected deployment registration must match this
coverage digest. Absence or drift refuses enrollment; readiness returns `None`
only after all original registered contracts and limits pass.

Stateless binding and registration plan version `2.0` additionally require an
explicit `catalogue` binding: the gateway ARN and role, Search target ID, provider
Lambda ARN, and the SHA-256 of canonical `GetGatewayTarget.targetConfiguration`
(including its complete inline schema). These are protected deployment inputs;
there are no inferred IDs or defaults. Earlier bindings/plans are refused.
Coverage includes this binding and the exact catalogue-read contract.

Each native fixture freshness check performs at most one `GetGateway` and one
`GetGatewayTarget`, each with one SDK attempt. A successful check requires both
reads. Missing, foreign, non-READY or changed metadata refuses without fallback
to enumeration. The selected gateway must still be `adp-tools`, use the exact
role with MCP/IAM, and expose the pinned `websearch` Lambda target using gateway
IAM credentials. Ordinary catalogue discovery is unchanged, as are canonical
membership, policy, consent, entitlement and protected contract rechecks.

These two reads bind target metadata, not the currently deployed Lambda image.
The existing protected release/source coverage and shared release image receipt
remain responsible for code attestation; no numeric Lambda version is required.
The existing gateway catalogue read policy supplies these two control actions;
this change adds no IAM actions. Their bounded count does not establish total
managed retries, wire bytes, prices or financial settlement.

`native_test_registration.py` compiles exact retained AWS offer/GetProducts
bytes, selected SKU/rate/geography and reviewed quantity proofs into immutable
setup/list/search contracts. It uses the highest published marginal tier for
that SKU/unit and rounds each cost term upward. Every quantity proof binds the
reviewed release, action, component, unit, maximum and retained source hashes.
Even zero quantities need explicit evidence. A tariff alone cannot prove a
maximum quantity; unknown network, retries or allocation inputs remain missing.

The separate operator script prepares a private review plan and exact gateway
ConfigMap overlay without AWS access:

```sh
PYTHONPATH=modules/gateway python modules/gateway/scripts/register_native_test.py prepare \
  --inputs /private/native-registration-inputs.json \
  --output /private/native-registration-plan.json \
  --runtime-output /private/native-runtime-overlay.json
```

The input explicitly supplies `quote_inputs`, digest-to-local-path
`evidence_files`, existing `designation`, `deployment` (omitting compiler-owned
`effects` and `effect_coverage_digest`), `account_id`, `region` and the separate
`registration_role_arn`. There is no example query or automatic fixture/grant.
Source tests use explicitly synthetic evidence and do not qualify live prices.

After the canonical deployment/account approval, an authorized operator can
apply the exact reviewed plan hash through the same script's `apply --plan ...
--expected-plan-sha256 ...` command. Its clients make one SDK attempt, verify
STS account and registrar role, then conditionally write the five initial
designation/deployment/contract records in one transaction. Existing records
are never overwritten; a lost acknowledgement requires read-only reconciliation.
This creates no user, credential, consent, native run, meter or budget reset.

Deploy the reviewed gateway/facade/provider source and protected ConfigMap
overlay with the existing deployment procedure. The authority module supplies
tenant-only journal Query and optional exact execution-table GetItem access.
Facade IAM can invoke only authorization/dispatch; reconciler IAM gets the
separate native cleanup route, rendered from the same internal gateway URL.
The table's protected registrar policy remains mandatory. Actual selected
quantity evidence, fixture identity, exact query, deployment readback and
trusted complete settlement are still needed before live qualification. The
source tests exercise real SQL, Moto/Lua and HTTP handlers with simulated
provider traffic; they do not establish deployment or final governance.

The selected search wrapper and MCP/interceptor KMS clients use one SDK attempt
with bounded connect/read timeouts, including ordinary calls through those
clients. This does not bound service-internal AgentCore retries or total billed
network bytes; those quantities still require separate evidence.

## Inputs still required before live enrollment

- Protected designation of an existing fixture principal, real approving owner,
  current canonical membership and existing consent/entitlements.
- Deployment identity, storage/IAM including permanent designation tombstones,
  deployed coverage across every paid route/auth mode, and revocation readback.
- Exact protected CI repository/workflow/ref/source SHA, environment, actor,
  runner-role trust and job/scenario identity; installation/version evidence.
- Finite dispatch/cleanup/meter deadlines and a genuinely new approved test;
  reconciliation of existing unknown holds without resetting spend.
- Supported model quotes/routing, hierarchy headroom, original-ID usage
  settlement, independent provider observation and owner cleanup recovery.
- Tool-session, paid setup and cleanup admission with trustworthy prices and
  lifecycle accounting. The model slice alone cannot establish the all-in cap.

Maintained checks: `tests/agentauth/test_native_test_enrollment.py` and
`test_native_test_runtime.py` use isolated PostgreSQL, Moto, fakeredis Lua, signed
offline JWKS and substituted STS/SDK/HTTP transports. The runtime suite exercises
the actual application, authentication validators, owner approval/exchange,
eight paid route denials, both supported provider services, streaming, replay,
retained holds and registration revocation at dispatch. Existing
`test_test_run_admission*.py` and proxy/budget regressions remain required. These
fixtures do not establish deployed or native acceptance. Real tools evaluation
remains refused. Existing hierarchy budgets and spend are unchanged.
