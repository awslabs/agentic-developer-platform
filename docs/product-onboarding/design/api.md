# Shared API contract

**Proposed v1.** [openapi.json](openapi.json) is the route/method/schema authority;
[schemas.json](schemas.json) owns strict request/response shapes;
[examples.json](examples.json) supplies executable positive and negative cases.
The OpenAPI catalog includes shared Product operations mapped to CLI commands,
the authenticated callback producer, and the current-people read projection used
by the workspace UI. UI and agent adapters call these operations,
not direct SQL or a second workflow backend.

## Transport, scope and compatibility

Public prefix `/api/v1`; FastAPI mount `/v1` (the existing ingress strips `/api`).
Reuse current bearer session / authenticated agent transport. Tenant comes from
that session's selected workspace; a body tenant is rejected as an unknown field.
Multi-tenant clients explicitly select the workspace using existing selection APIs.
Connection/admin callbacks use gateway-issued service tokens with the
`product-callback` audience and a stored operation-bound scope. Cognito's static
model-invocation scopes do not authorize these callbacks.

The path shorthand **P** below is `/products/{product_id}/projects/{project_id}`,
**S** is `P/onboarding-sessions/{session_id}`. IDs in paths are untrusted selectors.
Server resolves the selected tenant + current relationship + current Product-team
membership or verified platform-admin authority + existing operation rights + source
permissions. Ownership and organization-admin roles alone grant no visibility;
legacy teamless Products are administrator-only until assigned. Personal/team
credentials retain original scope.
Product/Project configuration has no `visibility` input. `Only me` / `Product team`
remains a run/activity choice in #8148; private results cannot feed shared context
implicitly. Product `team_id` is the single mapping for all bound Projects. A
member with the existing operation's write permission can edit directly; owner
acceptance is not an additional condition for ordinary profile changes.
Omitted foreign records are not counted. History and pagination use the same checks.

JSON is UTF-8, maximum request body 256 KiB and discovery response 1 MiB (stop at
the last complete record and return a continuation); unknown keys rejected. Version `1.0` is distinct
from Context snapshot `1.0` and Insights event versions. Product/Project/Financial
field meanings map to the existing Context model (S09); export wraps them in that
model's six-field refs. V1 narrows Product owner to an accountable human and adds
local allocation IDs; export removes local transport-only fields, including Product `team_id` (team
access remains enforced by the live resolver; Context does not gain a new field). It does not
change the upstream Context schema or rename its approval enum. Provider-native
IDs are retained separately from canonical registry IDs.

New optional fields are additive within v1; unknown enum values are rendered
unsupported, never coerced to ready. Breaking changes use `/v2` with a compatibility
window. API advertises proposed `product.profile.v1` and `product.onboarding.v1`
operations through existing `/me/cli-capabilities`; schema/feature/permitted/ready
axes stay separate. Old client can ignore this feature; new client facing an old
server reports unavailable and sends no mutation. No model is required for direct
operations. Guided model calls retain existing budget and consent enforcement.

## Routes and decisive behavior

| operationId | Route relative to `/v1` | Authorization / effect |
|---|---|---|
| `listProducts` | GET `/products` | Current Product team members or verified platform administrators in the selected tenant; shared metadata, lifecycle, optional authorized team label and only accessible Project IDs. No Product-wide completeness or hidden totals. |
| `createProduct` | POST `/products` | Assigns the caller's current primary team or sole membership; missing or ambiguous membership returns `409 team_assignment_required`. Name, purpose and intended users are required; optional domain is saved atomically. Atomic Product/Project/binding; 201 Profile, Location and Profile-Revision. |
| `listProjects` | GET `/projects` | Only authorized governed Projects, including unbound ones eligible for selection. |
| `bindProject` | POST `/products/{product_id}/projects` | Product owner and Project owner; new or unbound selected Project. Already-current same pair is no-op replay; foreign current Product is 409. |
| `getProfile` | GET P | Returns common Product + only this Project's typed configuration/provenance/progress; Profile-Revision. |
| `updateProduct` | PATCH P | Authorized editor under existing write permissions; partial common-field replacement. Owner/team reassignment additionally requires existing administration rights. Required fields cannot be nulled. |
| `updateProject` | PATCH `P/configuration` | Authorized editor under existing write permissions; name/configuration edits need no owner approval. Owner/state changes retain existing administration rules; team is inherited from Product. Configuration supplied means replacement of that typed document, not recursive JSON merge; expected revision protects it. |
| `unbindProject` | DELETE P | Both owners; closes dated association, never deletes Project. Last active Project 409. Return current Profile using remaining caller-authorized Project; require that such a Project exists before unlink. |
| `changeLifecycle` | POST `P/lifecycle` | Product owner; archive/restore with reason. Archive fences new operations; recovery rechecks current access. |
| `listSources` | GET `P/sources` | Filtered projection from existing connection/vault services, not admin-route proxy. No secret values. Partial provider lookup remains partial. |
| `listBindings` | GET `P/bindings` | Authorized source selections; inaccessible source is the strict RedactedBinding variant (local binding ID/revision/status only), never provider IDs, connection refs or falsely healthy status. |
| `createBinding` | POST `P/bindings` | Authorized editor + source entitlement; selected native identity and known connection. Duplicate same selection returns existing binding; never reconnects provider. |
| `unlinkBinding` | DELETE `P/bindings/{binding_id}` | Authorized editor; tombstone and increment generation, retain provider/shared connection. |
| `verifyBinding` | POST `P/bindings/{binding_id}/verify` | Authorized readback; 202 operation; updates observed evidence, never grants execution. |
| `startOnboarding` | POST `P/onboarding-sessions` | Authorized actor; reuse open actor session. 201 for new, 200 for existing (same Session shape). |
| `getOnboarding` | GET S | Current Product team member or verified platform administrator, subject to existing session/operation permissions; durable state plus authorized, unexpired pending proposals and remaining paths; no conversation transcript required. |
| `updateStep` | PUT `S/steps/{step_id}` | Information/basic/advanced/financials; save/confirm/N/A/later. Authorized editors save changes or confirm source/model suggestions directly, without owner acceptance. Completion derived, never trusted from client. |
| `discoverSources` | POST `S/discoveries` | Authorized read-only selected sources, bounded operation. Returns optional per-attempt `resources` (each with `binding_id`) and `cursor`; explicit continuation with the same selected bindings and a new idempotency key resumes a checkpointed attempt. Partial/denied/unavailable coverage explicit; no auto source creation. |
| `guideOnboarding` | POST `S/guidance` | Optional bounded model-assisted turn through existing metered model API; returns typed proposals, never performs suggested effects. Deterministic guidance is available without a model. |
| `startHandoff` | POST `S/handoffs` | Explicit target/action; credentials/admin/Context/L5/repo-create. Save operation before redirect/effect; apply each target's existing authorization, not session owner alone. |
| `getOperation` | GET `P/operations/{operation_id}` | Current actor/scope checks; reports/reconciles safe observation only, never starts a new external write from a GET. |
| `reconcileOperation` | POST `P/operations/{operation_id}/reconcile` | Owner/initiator under current authority; reconcile uncertain effect by native ID, no blind retry. |
| `cancelOperation` | POST `P/operations/{operation_id}/cancel` | Fence new effects, retain reconciliation for an already committed provider operation; no provider deletion implied. |
| `listChanges` | GET `P/changes` | Authorized scoped event feed/history; metadata and refs only; no raw snapshot/private contents in feed. |
| `getRevision` | GET `P/revisions/{revision}` | Frozen configuration with current access filtering and **current** authorization/readiness overlay; past snapshot cannot revive access. |
| `applyCallback` | POST `/product-callbacks/{operation_id}` | Authenticated stored producer only; match all scope/revision/generation/sequence fields; duplicate/older receipts are safe acknowledgements. Never browser OAuth completion. |

Collection query `limit` defaults 50, range 1..100. Opaque signed cursor binds
tenant, actor, filter, Product/Project, last `(created_at,id)` or `(revision,id)`
and expiry (15 minutes). Invalid/cross-query cursor 400; revoked actor still denied.
No total count from unauthorized rows. Change feed cursor includes high-watermark
revision; new writes enter subsequent pages without offset duplicates. Source
pagination never turns truncated lookup into complete discovery.

`listSources` returns safe connection metadata and bounded `DiscoveredResource`
items, so clients can select a returned native ID rather than type one. To continue
a connection's `resource_cursor`, send it with `connection_kind` and `connection_id`
(all three required together); the same current entitlement checks apply. Build/test
command strings are source-derived informational metadata, never instructions to
execute. Redact credential-bearing commands/URLs; raw source files are not stored
in this response. `verified_at`/`observed_at` retain original times; a cached response
does not turn an old provider observation into current verification.
For an otherwise authorized binding whose `stale_after` has passed, return
effective status `degraded`, retain original observation times, and mark its
progress check `stale`. The UI shows **Needs verification**; Context summaries and
readiness cannot count it as currently verified. This read-time overlay does not
mint a new observation or immutable registry revision. Revoked access still uses
the redacted response, independently of freshness.

## Revisions, idempotency and errors

All mutations except authenticated callbacks require `Idempotency-Key` (16..128
ASCII letters/digits/underscore/hyphen). Existing-aggregate mutations additionally
require `If-Profile-Revision: N`. Profile/Session/Binding responses carry the
committed revision in both body and `Profile-Revision` header. This is an explicit
application precondition (consistent with existing hierarchy expected revisions),
not an HTTP entity tag: live authorization/freshness/progress overlays can change
without a configuration edit. Use `Cache-Control: no-store` for these private
responses. Do not advertise a strong ETag that ignores those changing fields.
Product revision is a concurrency token, separate from normalized record,
relationship and mapping-rule versions (see [shared data model](data-model.md)).
It increments once for each committed semantic aggregate change;
identical replay/no-op does not increment. Header presence is not authority.

Order: authenticate → resolve resource visibility → find actor/operation/key →
compare canonical request hash → reauthorize replay output → if new, check revision
and effect rights → commit. Thus lost-response retry with its original old revision
returns the original operation/resource, while a fresh stale write is 412. Pending
identical request returns 202 and Location; differing payload under the key is
409. Local receipt lives at least 24 hours; external recovery receipts 365 days;
after full-receipt expiry retain tombstone and return 410 with safe read/recovery
action. No client library automatically creates a new key after uncertain outcome.

Error body follows existing FastAPI `detail` convention, adding stable code,
retryability, safe field pointers and request ID. Never include raw body, credentials
or foreign identifiers in errors.

| HTTP / code | Condition | Client behavior |
|---|---|---|
| 400 `invalid_request` / `invalid_cursor` | Syntax/cursor | Correct input, no effects. |
| 401 `authentication_required` | Session invalid | Reauthenticate; original operation key retained privately. |
| 403 `permission_denied` | Known resource, wrong action/approval | Explain needed owner/admin action, no automatic escalation. |
| 404 `not_found` | Missing or hidden scope/source | Identical public response for foreign and nonexistent IDs. |
| 409 `idempotency_conflict` / `binding_conflict` / `last_project` / `callback_generation_conflict` | Semantic conflict | Resolve authoritative state; no blind retry. |
| 409 `team_assignment_required` | Product creation has no current primary or sole team membership | Choose a primary existing ADP team before creating the Product. |
| 410 `operation_expired` / `revision_expired` | Tombstone or expired snapshot | Read current state or explicit recovery; do not rerun old request. |
| 412 `stale_revision` | Aggregate revision mismatch | Refetch, show diff, explicit merge; no lost confirmed values. |
| 422 `validation_error` / `required_check` | Invalid typed/semantic data or selected mandatory N/A | Field errors; accepted state unchanged. |
| 428 `precondition_required` | Missing key or revision | No mutation; caller supplies preconditions. |
| 429 `rate_limited` | Operation bounds | Retry-After with same key, bounded retry. |
| 503 `dependency_unavailable` | Required integration missing | Keep saved state; mark only dependent capability unavailable. |

Semantic validators beyond JSON Schema: current identity and cross-ref scope;
trimmed text; ISO currency allowlist; real IANA zones where schedule references
require them; start≤end; parent allocation limit/currency/acyclicity; selected checks
not N/A; active product has a Project; source-native ID readback matches; proven
approval authority for `approved`; no client-generated provenance/verification;
Product team_id references a same-tenant existing team under current administration
rights; Project/Basic have no duplicate team or privacy input. Changing a mapping
does not add users to a team. Record
refs must resolve under current access. Environment selection classification must
agree with `production`; provider/account/region/role are read from verified source
records, never accepted as free-text claim of authority.

## Concrete create / conflict / binding examples

Fictional HTTP exchange; credentials are intentionally omitted. These proposed
requests are not executable against the baseline runtime.

```http
POST /api/v1/products
Content-Type: application/json
Idempotency-Key: example-create-idea-0001

{"name":"Example Planner","purpose":"Coordinate small releases","intended_users":["Maintainers"],"starting_point":"idea","project":{"mode":"create"}}
```

201 returns `Profile` with Product ID `00000000-0000-4000-8000-000000000001`,
Project ID `00000000-0000-4000-8000-000000000002`, revision 1 and
`Profile-Revision: 1`. Owner defaults to the authenticated organization-local human.
No source, environment, allocation, run or external resource is created. Add the
optional top-level `team_id` to create a shared team workspace immediately; otherwise
map it later through Product update. `project.visibility`, `project.team_id` and
`configuration.basic.team_id` are rejected unknown fields. Existing Project binding
previews inherited team access and verifies the caller's association authority.
See the full validated `minimal-profile` response in [examples](examples.json).

```http
PATCH /api/v1/products/00000000-0000-4000-8000-000000000001/projects/00000000-0000-4000-8000-000000000002
If-Profile-Revision: 1
Idempotency-Key: example-update-name-0001
Content-Type: application/json

{"name":"Example Release Planner"}
```

First accepted edit produces revision 2. A different edit with fresh key and
expected revision 1 returns the validated `stale-write` error (412); all other fields and
step progress remain unchanged. Replaying the first key/body returns revision 2,
not a third edit. A hidden Project returns 404, not its current revision.

Binding POST takes `BindingInput`: `repository`, `github`, real selected
`connection_ref`, verified namespace/native ID. Provider URL/name are display
readback, not canonical identity. A new credential/admin handoff is initiated only
when no authorized existing connection suffices. Unsupported provider returns an
unavailable capability, not a fake empty connection list.

A GitHub repository selection may supply `source_revision` as a full lowercase
40-character commit SHA. Verification reads that exact commit through the selected
installation and repository and requires the returned SHA to match. Refresh keeps
the pin even when the default branch advances. An invalid, inaccessible or
mismatched pin stays unverified or revoked; verification never falls back to the
default branch. Omitting the pin (or setting it to `null`) retains default-branch
tracking. Discovery timestamps are metadata, not commit pins.

## Source/admin callbacks and downstream records

Browser returns continue using existing GitHub install-callback and OAuth
`/auth/connections/oauth/return` (S05/S06), including existing state/PKCE/expiry
checks. Gateway receives/looks up a verified connection receipt, then applies the
Profile operation using current tenant, initiating actor, session, binding generation
and expected revision. A browser cannot call `applyCallback` with ordinary session
credentials or choose another tenant. The admin receives only the pending request
reference and required administrative action; the requesting member never receives
the administrator's tokens. A resume GET may show the handoff still pending while
new credentials are being verified.

The connection/admin producer is `gateway-handoff-reconciliation`, invoked by
`reconcileOperation` after an authorized readback of the native effect. It does
not repeat the admin or connection write. For an active handoff it issues a
short-lived token and invokes the same admission handler as the callback route;
no HTTP self-call or browser token exchange is needed. GET remains read-only.
This is an observation-driven producer, not a new OAuth, AWS or GitHub push
subscription. Existing provider completion flows and permissions are unchanged.

The issuer `adp-gateway/product-callback` derives a domain-separated signing key
from the existing private `BG_TOKEN_SECRET_KEY`. HS256 tokens live at most 60
seconds and never beyond the operation deadline. The signed producer, tenant,
Product, Project, session, initiating actor, original revision, generation and
scope must match the locked operation. Only this callback route accepts them;
there is no public issuance endpoint and tokens are never returned to the browser.
A missing signing key fails closed and leaves the committed native effect
recoverable. Pre-existing handoffs without a producer field are assigned this
same gateway reconciliation producer on read; no external producer gains access.
Cancellation, expiration and current-permission checks still fence callbacks;
reconciliation retains its separate readback of an already committed outcome.

`Callback` sequence rules are in [database](database.md). A producer receipt must
echo the operation's original Profile revision, even if a newer user edit exists.
New observation at an older revision becomes a candidate, not a blind overwrite.
Callbacks for expired, cancelled or revoked scope are logged and refused. Duplicate
acknowledgement after reporting failure returns the same operation ID/status; it
cannot create another binding or complete an unrelated step.

`Change` is the Profile-side domain event. `PeerReceipt` records actual downstream
acceptance/result reference, not a promise. Consumers must negotiate schema and
authorization transport before enabling a handoff. No Context/L5 URL is invented
here. A handoff request's target/action pairs are allowlisted: Context/read_context,
L5/prepare_setup, connection/connect, administrator/assign_team and
repository_create/create_repository (requires confirmed repository_plan with pinned
template/protection policy), repository_pipeline/propose_pipeline (requires an
existing binding and confirmed template/base-revision plan, creates a PR only). None is
a generic arbitrary URL, shell command or automatic provider-run facility.

## Simple forms, partial confirmation and list semantics

`createProduct` requires name, purpose, intended users and the Project selection.
Clients default the authorized owner and `project.mode=create`; optional details
may reveal existing-Project selection and domain. Incomplete local input is a
draft, not a persisted Product. The server assigns the caller's current primary
team or sole membership without an extra creation field; missing or ambiguous
membership requires choosing a primary ADP team first. Sources and budgets are optional.

Product identity/team is shared. Configuration and onboarding are selected by the
exact Product/Project pair; changing the UI Project selector performs scoped reads,
not a configuration transfer or binding mutation. Creation/binding/unbinding remain
explicit operations. A current registry binding does not infer attribution for all
work, initiatives or releases in that scope.

`ProductSummary` returns `lifecycle_status`, optional authorized `{id,name}` team
metadata and accessible `project_ids`. The list may show that authorized count,
never hidden totals. It does not show an aggregate completeness score or use a
local “saved” flag as the lifecycle. `Progress` is shown for the selected Project.

`updateStep` with `action=save` replaces each supplied configuration section,
leaving omitted sections unchanged. For a section-specific route, only its
corresponding section may be supplied; information updates Product fields, basic
may update Product team with existing administrative rights. Validate this rule
semantically. Clients build the complete edited section from a current read; a
stale revision returns 412 before any partial update. Ordinary `updateProject`
configuration retains its explicitly documented whole-document replacement rule.

For `action=confirm`, require `proposal_id` and nonempty `confirmed_paths`. Do not
accept caller-supplied Product/configuration values in the same confirmation.
Paths must be distinct, nonoverlapping members of that stored proposal's
`remaining_paths`, and resolve to offered typed fields. Arrays are atomic fields;
array-element pointers and positional merges are not supported. Recheck current
source entitlement/revision and compare each selected field's accepted value with
its base value. Unrelated edits do not invalidate an unchanged proposed field;
changed fields return `412 stale_revision` with safe affected paths for review.
An expired proposal returns 410; revoked source data is not disclosed.

Apply selected values and their per-field source provenance atomically, record
actor/time, consume only those paths and leave the other suggestions pending.
Confirmation never changes funding approval or execution authority. Each remaining
path retains its original base/source revisions; do not rebase it silently after
a partial confirmation. A manual replacement uses `save` with reason/optional
override expiry and direct-input provenance; it is not an imported confirmation.

Store pending candidates in the session's separate typed `proposals` document,
not `StepState`. `getOnboarding` returns them after current scope/source filtering,
so UI or CLI can resume without reconstructing chat or polling an unknown operation.
Consumed paths and superseded candidate evidence remain in the immutable change
journal; bound pending-candidate count/size and return a capacity error before
silently evicting unreviewed user work.

`later` leaves an applicable check outstanding. `not_applicable` needs a reason
and is rejected for selected mandatory requirements; it does not erase saved
values. Financial forms can default a Product target, accountable owner, AI
category and proposed funding state, but must show an editable budget period.
An Initiative reference is selected from the portfolio authority's existing
authorized **Initiative** record, not from a Portfolio collection record.
A release target date neither mints a Release nor substitutes for a budget period.

`listChanges` includes `relationship_versions` for registry binding mutations,
using the strict `RegistryRelationship` projection of the Context relationship
contract. Return only currently authorized same-Project endpoint/source references;
filter shared Product changes into the requested Project scope and never expose
other Project counts/IDs through this array. This supplies immutable binding
versions to adapters without a database read or reconstruction from the current
row. Other business relationships belong to their source-owning services. This
Profile change envelope is not an admitted Insights producer record.

For registry record content, an authorized adapter pairs a change's `event_id`
(the immutable `product_changes.id`) with its aggregate `revision`, then calls
`getRevision` in the same Product/Project scope. It emits Product/Project content
from that frozen configuration with `record_revision=event_id`; current access
and readiness overlays are not immutable record content. Reused records retain
their original references. Readback may redact now-inaccessible fields or return
410 after snapshot expiry; neither permits reconstructing historical content from
the current Profile or replacing an emitted immutable record with partial content
under the same reference. Relationship versions remain readable under current access
checks for the Product lifetime, independently of heavyweight snapshot retention.

## Business catalog boundary

The 26 Profile operations own Product/Project configuration, sources and onboarding.
They do not create or edit Portfolios, Portfolio membership, Initiatives,
Contributions or Releases. `RegistryRelationship` intentionally permits only
`product_project`; the broader [Context schema](../../product-context/context-model-v1.schema.json)
defines `portfolio_product` and other source-owned relationships. A Portfolio is
neither a required `CreateProduct` field nor a Financials allocation target.

External selections use authorized six-field references, including the original
record type and revision. Profile persistence of an Initiative reference does not
create a Contribution or authorize access to other Products. Catalog lookup/CRUD,
membership mutation, pagination and physical storage contracts remain owned
catalog/Context implementation work; the mockup's fictional selector and relationship
explanation are not new Profile endpoints. See the [consistency review](consistency-review.md).


## Current people and access

`getProductPeople`: `GET P/people?offset=0&limit=50` returns `ProductPeople`.
It uses the same selected tenant, current Product-team membership or verified
platform-admin authority, and active Project binding as a Product read. A foreign
or revoked scope returns 404. Responses are
`Cache-Control: no-store`; pagination is bounded to 100 members per read.

The projection contains the named owner, selected ADP team, current human members,
verified GitHub usernames, `is_owner`, and management permissions. It excludes
shadow/service users and unproven or foreign-organization identity claims. GitHub
labels follow the canonical primary identity ordering. Only current membership
rows establish team membership; the legacy primary-team pointer is not an access
grant, matching Product authorization.

Team and owner changes reuse `updateProduct` with `team_id` / `owner_user_id`,
`If-Profile-Revision` and `Idempotency-Key`. Team assignment requires `ORG_UPDATE`;
owner changes require `USER_MANAGE`. Existing-person membership uses the existing
organization team API without changing their primary team. New GitHub enrollment
uses the existing platform-admin-only GitHub enrollment API; `enroll_github` follows
that permission, and never grants it. Archived products show no management actions.

The Product list includes optional `team` and named `projects` projections alongside
its existing `project_ids`. These names do not grant additional access. None of
these projections creates a second membership model, Product role or lineage ID.
