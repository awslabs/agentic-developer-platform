# Delegated Agent Authority — Implementation Decisions

**Status:** Authorization implemented and isolated live acceptance passed; ordinary flags off
**Issue:** #5028 (child of live-controls epic #3959)
**Related:** #3142 credential-authorization binding (`docs/design/credential-authorization-binding.md`), #5024 control-token expiry, #3967 Wave 1 evaluation
**Last updated:** 2026-09-13

---

## Current runtime checkpoint (2026-09-13)

The branch now registers `POST /internal/v1/agent/bootstrap`, `GET
/internal/v1/agent/status?run=…`, `POST /internal/v1/agent/dispatch`, and `POST
/internal/v1/agent/control/{run}/{action}`, `POST /internal/v1/agent/waves`, and
`POST /internal/v1/agent/revalidate`. These routes require the IAM internal
transport plus dedicated-audience Kubernetes workload proof. All except bootstrap
also require the short-lived credential for the bound invocation and attempt.
Status returns a safe projection. Control verbs still return 501 after authorization.
The authority enablement, table, worker-image allowlist, public key ID and
orchestration repository parameters use SSM `SecureString`; the deployment
reader explicitly requests decryption. These parameters name the stacks' existing
rotating customer-managed KMS keys (webhook DynamoDB key and gateway secrets key);
the deployment reader requires KMS Decrypt on those keys. Private signing material remains in the
gateway Kubernetes Secret, not these parameters.

Authority-enabled workers now use `agent-authority-worker-sa` and a separate
`adp-<env>-agent-authority-worker-role`, with a mandatory permissions boundary.
Live review found both AWS `AdministratorAccess` and EKS cluster-admin access on
the legacy worker role. Removing its inline DynamoDB allow was insufficient;
IAM boundaries also do not constrain EKS access-policy authorization. The new
role has no EKS access entry or worker Kubernetes RBAC grants. Its distinct
service-account subject cannot assume the legacy role through the unsigned STS
web-identity API. KEDA and the gateway registry are wired to the new role.

The boundary preserves model invocation, queue consumption, correlation updates,
logs/evidence and the marker-signing read. It explicitly denies authority-table
access, other secret reads, privilege escalation and unlisted gateway routes even
if a broad identity policy is subsequently attached. GitHub token minting is
forced through the gateway. The gateway authenticates both run and pod proofs on
worker credential-broker requests, validates their current authority, and pins
invocation/user/repository/installation. GitHub child mints use the protected
assignment rather than depending on optional legacy Activity fields. Platform
control clients select refreshable IRSA independently of customer AWS credentials
loaded for Operations tasks.

Enablement remains a coordinated migration, not just a flag change. Drain legacy
workers and ensure their AWS/EKS administrator grants cannot be used by untrusted
code before claiming deployment-wide isolation. The legacy role and service
account remain for flag-off operation; this PR does not revoke live administrator
access. The new boundary deliberately excludes legacy raw GitHub App keys, the
GitLab shared API token, and the Knowledge Layer Door's shared gateway key. Those
legacy credential modes need separate brokered access before they can run with
authority enabled. No child receives a human's customer-cloud credentials merely
because it shares that human root; credential delegation is a separate grant.

A verified GitHub human event creates a protected authority with a seven-day
deadline, a pending execution and a bounded grant before publishing. The worker
binds its actual pod UID before repository execution. It refreshes its 900-second
credential every 300 seconds through the same binding, without resetting its
attempt, generation or journal. A separate listener-token rotation protocol is
implemented as described in Decision 6 below and verified during isolated live acceptance.

When authority is enabled, the existing CLI spawn syntax uses `/dispatch` and
sends intent plus a stable request ID; mutable parent/root environment variables
are excluded. Human-initiated grants currently limit dispatch to the approved
repository and issue. Operations can dispatch developers/reviewers, and a
developer can request review. Child actions are intersected with explicitly
delegable actions. The separate AI-DLC publisher now provisions identities from its resolved human
approval after the node transition commits, before SQS publication. Engine requests
and renewal re-read PostgreSQL approval, node attempt/state and flow state. A
halted or superseded node cannot renew or dispatch. Human-launched coordinators
on the approved intent can select eligible story nodes through the guarded SQL
transition and committed dispatch receipt. The emitter binds native child issues
to the already approved wave through `adp-trigger bind-wave`. A unique committed
wave-launch receipt gives a child Operations coordinator scope over that wave's
stories and evaluation. It receives self/descendant monitoring, not whole-flow
monitoring. Evaluation uses the existing guarded READY-to-RUNNING transition.
Successor kickoff requires the current evaluation to be PASSED, a dependency
edge to ready successor work, and passed intervening human gates. A newer plan
acceptance/amendment invalidates the old approval; a binding also pins its exact
node identities, kinds, issue references and graph addresses.

Dispatch commits the child identity, grant, exact Activity row, reservation and
durable intent together, with live execution/grant/authority conditions. It checks
both concurrency and a cumulative dispatch budget. Children have separate FIFO
groups so waiting parents do not block them. Identical requests recover the same
invocation. Ambiguous sends may reuse the same SQS deduplication ID for less than
240 seconds from reservation; beyond that the service reports unknown outcome
and refuses another send. A completed bootstrap proves a previous send arrived.
This does not claim exactly-once effects across pod loss.

Child bootstrap/refresh checks the protected parent grant and its recorded epoch,
including ancestor cancellation/revocation. Parent completion alone does not
cancel authorized children. Terminal reporting now atomically completes the execution,
removes listener credentials, retains its generation counter and releases its exact
child reservation once. Registration commits its ledger and event counter together;
identical retries recover the same generation. Worker registration/status migration
and conditional IAM narrowing are implemented and live-verified. Cancellation,
queued-action refusal and two-worker isolation passed isolated acceptance.
Signing-key rotation still needs a rollout rehearsal before routine enablement.
Registered EventBridge rules can use a bounded standing human approval as
described below. Agent bot mentions remain forbidden: use `adp-trigger`.

The following original decision sections explain the component design; their
earlier pending-integration descriptions are superseded by this runtime checkpoint.

## What this document is for

#5028 requires that concrete storage, signing/key-management, credential-rotation
and maximum-revocation-delay choices be written down and reviewable rather than
left implicit in code. This is that record.

It documents decisions for the parts that **exist in the branch** and states
plainly which parts are **not yet implemented**. A reader should be able to tell
those apart without reading the diff. Where a decision is provisional pending
live acceptance (AC8), it says so.

This document does not enable any control verb. Pause, resume, steer and abort
remain unimplemented and return 501 after authorization; `SUPPORTED_AGENT_ACTIONS`
contains `monitor` only.

---

## The boundary this closes

`adp-trigger --persona … --issue …` calls the IAM-authenticated
`POST /agent/trigger` route, which checks chain membership and target-repo
tenancy and resolves human-root attribution from stored records. Two properties
of that arrangement are the problem:

1. **Workers share one IAM role.** SigV4 authenticates the role, so it cannot
   distinguish two runs. The invocation identity comes from `ADP_MESSAGE_ID` /
   `ADP_CORRELATION_ID`, which are ordinary environment variables inside a pod
   running agent-authored code.
2. **The record that answers "whose run is this?" is writable by the runs it
   constrains.** `scaledjob-iam.tf` `DynamoDBWebhookEventsUpdate` grants
   `dynamodb:UpdateItem` on `arn:aws:dynamodb:us-east-1:*:table/adp-*-webhook-events`
   with no key or attribute condition (verified by reading the deployed role on
   2026-09-13; IAM simulation returned allowed; no cross-run write was
   attempted).

So knowledge of a parent ID plus the shared role is not caller identity, and a
`webhook-events` attribute cannot be the sole authority for delegation,
ownership or control registration. A human-rooted chain records **provenance**;
provenance is not a grant to control every run that shares it.

---

## Decision 1 — Caller identity: a credential bound to invocation *and* attempt

**Implemented** (`src/agentauth/run_credential.py`).

The authenticated principal is `"<invocation_id>#<attempt>"`, carried inside a
credential the caller cannot author, verified on every request. Transport SigV4
still applies; this is layered on top and answers a different question.

**Why attempt-scoped.** A retried invocation is a different pod with a different
compromise story. A credential that survived into attempt 2 would let a
credential captured from attempt 1 act after the pod that earned it is gone.

**Why permissions are *not* in the credential.** Identity and authority are
separate objects. Bundling claims into the credential would make every renewal an
opportunity to widen scope, and would make revocation impossible without
revoking identity.

**No mint endpoint.** There is deliberately no route exchanging a claimed run ID
plus the shared IAM role for a credential — that would rebuild the exact hole
this closes. Delivery must be assignment-bound (Decision 6, **not yet
implemented**).

---

## Decision 2 — Authority: a reference to a real human decision

**Implemented** (`src/agentauth/grants.py`).

A grant derives from an `AuthorityReference` naming a recorded human
authorization event. For AI-DLC that is the existing verified gate-decision /
genesis model (`orchestration_decisions` row with `actor_kind=HUMAN`). Another
human initiation path must add a `kind` with an equivalent verified record rather
than loosen this one.

Explicitly rejected as authority: an `is_human_rooted` boolean, a root-human ID,
comment text, or any worker-editable invocation row. The first three are
provenance; the fourth is attacker-writable.

**Attribution stays truthful.** The acting principal is always the
agent/service. The audit record names the human authority *separately*, as the
authority the delegation derives from. An agent never becomes a human, and
cannot approve its own human gate.

### Relationship authority is resolved, never asserted

A grant names explicit `target_run_ids`, or a `TargetRelationship` the **service**
resolves (`self`, `descendant`, `flow_node`). A caller can supply a target ID and
nothing else — "I am this run's parent" from the caller is the forged-lineage
attack with extra steps.

Shared tenancy or a shared human root confers **nothing**. Sibling, ancestor,
cross-flow and cross-tenant targets are refused unless the grant explicitly
permits that relationship or names the run.

### Delegation cannot expand

`delegable_actions ⊆ allowed_actions` is enforced in `__post_init__`, so a grant
whose delegable set exceeds itself cannot be constructed. `child_grant_actions`
intersects rather than unions: a child requesting more than the parent holds gets
the parent's narrower subset, so an over-broad legitimate request still
dispatches with correct authority instead of failing the flow.

---

## Decision 3 — Storage: a protected authority store, injected not hardcoded

**Grant + execution backend implemented (`src/agentauth/store.py`), defined in
Terraform, not yet applied. `TargetResolver` remains a protocol only.**

`GrantStore`, `ExecutionStore` and `TargetResolver` are protocols
(`src/agentauth/policy.py`). The policy does not name a table, so the protected
store lands without rewriting the policy — and `AgentAuthorityStore` now
implements the first two against `adp-<env>-agent-authority`.

**Required property of any backend:** no worker write path. This is the property
`webhook-events` does not have today.

**Shape as implemented:** one DynamoDB table `adp-<env>-agent-authority`
(`modules/agent-factory/webhook-ingress/infra/dynamodb.tf`), tenant-partitioned
`pk = TENANT#<tenant_id>` with four sort-key kinds — `EXEC#<invocation_id>` for
execution state, `GRANT#<principal>` for delegated authority, `RESV#<grant_id>`
for the dispatch concurrency counter, and `RESV#<grant_id>#<reservation_id>` for
the individual reservation each admitted dispatch holds. Permissions:

| Principal | Permission |
|---|---|
| Gateway / trusted dispatch role | `GetItem`, `PutItem`, `UpdateItem` on the table only — no `Query`, no `DeleteItem`, no `/index/*` (`aws_iam_role_policy.gateway_agent_authority`) |
| Shared worker role | **none** (no Allow; no `adp-*-agent-authority` in any worker statement) |

The worker side of that table is structural rather than asserted: both worker
DynamoDB statements in `scaledjob-iam.tf` are name-scoped
(`table/adp-*-webhook-events`, `table/adp-*-correlation-pointers`), and
`-agent-authority` matches neither pattern.

`Query` is withheld deliberately, not for least-privilege tidiness: a query can
match more than one item, and every lookup on this path must resolve to the exact
principal or invocation the verified credential named. `DeleteItem` is withheld so
that "revoked" and "never existed" stay distinguishable — revocation is a state
transition precisely so it remains auditable.

A separate table rather than new attributes on `webhook-events`, because the
worker's `UpdateItem` there is unconditioned — attribute-level protection would
require either per-attribute IAM conditions on a table the worker must keep
writing, or trusting the worker not to touch fields it can reach. A table it
cannot address at all is a boundary that does not depend on condition-key
correctness.

**Why not reuse the `authorized_user_id` approach from #3142:** that design
deliberately chose the run registry for *credential-user* binding, where instant
revocation by clearing an attribute is the win. Delegated authority has the
opposite requirement — the record must be unwritable by the subject — so it needs
a store the worker cannot reach, not a better-guarded attribute on one it can.

### Registration/status writes (AC4) — implemented and live-verified

Worker writes to `webhook-events` must lose the ability to touch another run's
row or another run's fields. Two acceptable mechanisms:

1. Route registration/status writes through a service that derives the exact key
   from the authenticated invocation/attempt and accepts only an allowlisted set
   of fields; or
2. Enforceable per-run **and** per-attribute IAM restrictions.

Field restrictions alone are insufficient: they would still let worker A
overwrite worker B's `control_*` registration or status. Exact-row derivation is
the property that matters.

**Migration order is mandatory and is not optional sequencing:** migrate every
existing registration/status writer first, then narrow
`DynamoDBWebhookEventsUpdate`. A deny deployed ahead of the replacements breaks
normal agent operation on every run — status rows freeze at
`webhook_received`, which is the #1455 failure mode.

Registration/status writes in `invocation_status.py` now use the authenticated
self-service routes when authority is enabled. The shared service derives the
exact event key from protected state and accepts only status/registration fields.
The dedicated worker role's boundary blocks direct table access. With the flag
off, the existing direct writer and legacy role remain available.

Writers to migrate before narrowing IAM. Enumerated by searching the worker
image for `update_item` on 2026-09-13. Production calls are in two modules under
`modules/agent-factory/agent-worker-image/lib/`:

| Module | What it writes | Note |
|---|---|---|
| `invocation_status.py` | invocation status transitions **and** the `control_*` registration fields | `register_control_endpoint` / `clear_control_endpoint` live here; `entrypoint.py` only imports them |
| `correlation_store.py` | attributes in `CORRELATION_POINTERS_TABLE` | Uses the separate `DynamoDBTableMgmt` permission. It is not a `WEBHOOK_EVENTS_TABLE` writer and is not affected by narrowing `DynamoDBWebhookEventsUpdate`. Its worker-writable data must not become delegated authority. |

`register_control_endpoint` is the one to study before designing the
replacement: it relies on an atomic DynamoDB `ADD control_generation :one` to
return the new generation, so a service-mediated write must preserve that
atomicity rather than read-modify-write. Losing it would let two attempts
observe the same generation, which is exactly the value the envelope binds
against.

---

## Decision 4 — Signing: Ed25519, workers hold verification keys only

**Implemented** (`src/agentauth/envelope.py`, `agent/src/control-envelope.ts`).

The listener independently verifies a short-lived gateway-signed envelope before
admitting a command. It queries no chain and needs no database access.

**Asymmetric, because the alternative is not an authority.** The existing
lineage-marker HMAC key is readable by workers by design — they sign their own
markers with it. Any authority whose key the attacker holds is not an authority.
The control service holds the Ed25519 private key; workers receive public keys
via `ADP_CONTROL_ENVELOPE_KEYS` and there is no code path in the worker that
signs. Both `crypto.verify(null, …)` in Node and `Ed25519PublicKey.verify` in
Python are native, so the worker gains no dependency.

**`alg` is an allowlist check, never a dispatch.** One permitted value,
compared before any signature work. `alg: "none"` is simply absent from the list.

### What the envelope binds

Issuer, audience, tenant, flow, authenticated caller/attempt, target
invocation **and generation**, action, command ID, request-body digest,
grant ID, revocation epoch, and `iat`/`nbf`/`exp`.

The body digest is over **raw socket bytes**, not a parsed-and-reserialized
object. Reserializing would let two different wire bodies produce one digest,
which is exactly the "same authorization, changed instruction" case.

Every binding is checked against a value the verifier knows **independently** —
its own run ID, its own generation, the action from the request path, the command
ID from the parsed body. Comparing an envelope claim to another envelope claim
would be tautological.

**Claim types are required, not coerced.** `String(value)` on attacker-controlled
JSON is a silent accept, and the two languages disagree about the result:
`String(["k1"])` is `"k1"` in JS but `str(["k1"])` is `"['k1']"` in Python. Both
verifiers validate declared types as a group before any claim is hashed, looked
up or coerced. Shared negative vectors cover this.

### Key management and rotation

| Item | Decision |
|---|---|
| Private key location | `agent-authority-signing` Kubernetes Secret in the gateway namespace; workers receive only public keys |
| Key format | PEM PKCS#8 Ed25519, loaded via `AGENT_CONTROL_ENVELOPE_SIGNING_KEY` |
| Key ID | `AGENT_CONTROL_ENVELOPE_KEY_ID`; listeners select by `kid` rather than trial-verifying |
| Public key distribution | `ADP_CONTROL_ENVELOPE_KEYS` accepts the Terraform JSON map of Ed25519 public PEM keys and the existing `kid:base64,…` format |
| Rotation procedure | Two Terraform key slots and a projected public ConfigMap support staging, signer handover and retirement. Listeners reload per request and report loaded public IDs. Follow [the rotation runbook](../runbooks/agent-authority-key-rotation.md); live acceptance is pending. |
| Malformed key entries | Skipped individually, not fatal to the map — one bad entry must not disarm a listener holding a good key. An empty map verifies nothing, which is the correct fail-closed outcome. |

The signing private key and invocation HMAC key are created outside the worker's
`adp/*` Secrets Manager grant, in a gateway-namespace Kubernetes Secret. Workers
have no Kubernetes Secret read permission. Terraform state contains sensitive key
material and must remain outside worker-readable S3 prefixes. This infrastructure
has not been applied. Live IAM/RBAC and attempted-read evidence remain part of
acceptance; source configuration alone does not prove deployed isolation.

---

## Decision 5 — Revocation delay, stated as a bound rather than implied

A signature cannot express revocation: a signed statement stays valid for its
window regardless of what happens to the grant behind it. Rather than claim
otherwise:

| Case | Maximum delay |
|---|---|
| A **new** request | Zero. The gateway re-checks live authorization and current revocation on every request; nothing reuses an envelope. |
| An **already-forwarded** in-flight command | `MAX_ENVELOPE_TTL_SECONDS` = **30 seconds**. |
| A **queued** action awaiting execution | At most **1 second** between starting online revalidation and SDK handoff. The worker rejects replies reaching that limit, with no cached approval. The gateway requires the exact current grant epoch, live caller/target identities, active ancestors, current flow/approval, delegated action/target, current generation and a still-valid signed forwarding proof. |

An envelope claiming a longer life than policy is **refused, not truncated** —
accepting it would let the signer overrule the platform's revocation bound.

Reauthorization failure must not apply the pending action and must leave a
truthful recorded outcome: the journal records it as rejected, never as applied.
The listener retains the private proof with the journal entry. Proof-bearing
commands cannot use the unguarded `markDelivered` path or settle as applied before
delivery. `deliverAuthorized` makes an authenticated `/revalidate` request through
the worker's refreshed identity files, checks its complete round-trip time, and
hands off synchronously with no intervening await. Concurrent delivery and
cancellation during the check cannot apply the same command twice. A lost SDK
acknowledgement records unknown and cannot replay the effect.

The Node revalidation transport accepts only the configured HTTPS API Gateway
execute-api host and `/<stage>/internal/v1/agent` path. It rejects private hosts,
IP literals, alternate ports, other routes, encoded paths, credentials, queries
and fragments before loading identity material. Proof content supplies only the
request body, and redirects remain forbidden. Custom gateway domains are not
supported by this transport.

Queued commands older than the 30-second forwarding proof are rejected; this
implementation does not silently extend them. Outages and signing-key changes
also fail closed. Tests exercise the actual HTTP revalidation and journal guard,
including a reply delayed beyond one second. Production supports no live verb
yet and continues returning 501; AC8 still requires live timing measurements
when a supported control adapter exists. This bound concerns admission to the
SDK, not cancellation of an effect already handed off.

---

## Decision 6 — Invocation refresh and listener-token rotation

Two separate credentials have separate renewal paths:

- The invocation credential has a 900-second lifetime and refreshes every 300
  seconds through the existing verified bootstrap binding. The gateway checks
  execution, grant, human authority and current engine flow. Refresh issues new
  signed bytes for the same attempt and epoch; it does not reset the worker.
- The listener bearer token initially lasts at most six hours. In authority mode,
  the worker supervisor rotates it through
  `POST /internal/v1/agent/self/control/registration/renew`. Each replacement
  lasts at most one hour. `control_credential_epoch` advances independently of
  `control_generation`; the HTTP socket and command journal remain in place.

The supervisor first atomically writes a mode-0600 local lease containing the
next token and the previous token. The listener reads that file on every request.
Only after the file is staged does the supervisor send the next token, expiry,
expected credential epoch, generation and stable rotation ID to the gateway.
The gateway derives the execution from the credential and pod proof, validates
current flow authority, and atomically changes the event token and protected
registration ledger with execution/grant/authority conditions.

The old token expires at the earlier of its original deadline and 30 seconds
from staging. A retry or acknowledgement never moves that deadline. Missing,
malformed, expired or rolled-back lease files refuse requests without falling
back to the startup token. Token rotation never claims to apply a control verb;
unsupported verbs remain 501.

Refresh normally runs every 300 seconds, sooner if the initial token has less
than ten minutes left. A lost response retains the exact rotation ID, token,
expiry and expected epoch and retries after ten seconds. If an outage outlasts
the staged token, the worker asks
`POST /internal/v1/agent/self/control/registration/state` for its own non-secret
recorded epoch and rotation ID. It then stages a fresh token against that verified
epoch. Revocation or cancellation refuses these operations; an unavailable
authority store cannot extend any deadline. During the outage, control may be
unavailable while the task continues running.

This path is tested with persisted DynamoDB state, lost-response injection and
real loopback HTTP requests to a continuously running listener. Two actual
cluster workers and long-running live acceptance remain required before enabling.
Gateway signing-key rotation uses two managed slots and a ConfigMap directory
projection that listeners reload on each verification. Missing projections fail
closed, and ping reports loaded public IDs for the staged handover. The rotation
runbook requires observed receipt by every active listener before switching or
declaring retirement; it assumes no fixed Kubernetes propagation bound.

Multi-day pod deadlines, durable task checkpoints and pod-loss recovery remain
outside #5028. The in-memory command journal does not provide exactly-once
effects across pod loss.

---

## Decision 7 — Delivery must be assignment-bound — NOT yet implemented

A credential on the shared queue is **not** delivery to an assigned worker: the
worker role holds `sqs:ReceiveMessage` on `adp-*-agent-submit.fifo`
collectively, so any worker can receive any message on it.

Delivery must therefore be bound to the assignment at trusted dispatch/bootstrap,
and where the platform supports it, to the workload's own key or identity. The
concrete binding is still to be designed; it must be demonstrated, not assumed,
before controls are enabled.

---

## Enforcement shape: one policy, both adapters

Both the existing trigger (spawn) adapter and the new control adapter call one
`AgentAuthorizationService.authorize()`. A second implementation of "may this
caller do this?" is two checks that agree only on the day they are written.

Order is deliberate:

1. Resolve the caller from its **credential**.
2. Resolve authority, delegation and **current** revocation (read fresh — a
   cached grant is a grant that outlives its revocation).
3. Resolve the target, tenant, flow and generation **independently**.
4. Check action and target relationship against the grant.
5. Enforce budgets, depth, concurrency and retry limits.
6. Record the decision, then dispatch / forward / refuse.

**Limits are enforced after the grant check** so an unauthorized caller cannot
probe another flow's load by watching for a 429.

**Concurrency is claimed, not read.** Step 5 admits a dispatch by *reserving* a
slot, not by reading a count and deciding. `reserve_dispatch` writes an
identified reservation record and the counter in one transaction: the record is
guarded by `attribute_not_exists`, the counter by the ceiling. This is two
requirements, not one. Atomicity alone stops two callers both claiming the last
slot; identity is what stops a duplicate release from freeing a slot a different
child still holds — the failure that lets the *next* dispatch exceed the ceiling.
So a reservation ID must be derived from the unit of work, releases are idempotent
by recorded state rather than by deletion, and `active_dispatch_count` is
reporting only. A spawn path that consults the count and then dispatches has
reintroduced the read-then-dispatch race regardless of how the count was read.

**The recorded outcome is the final one.** A limit refusal is audited as a
refusal — retaining caller, authority reference, target and action — rather than
leaving the grant step's `allowed=true` as the only record of a request that
returned 429 and dispatched nothing.

**Refusals are one opaque status; reasons are logged, not returned.** A caller
that could distinguish "bad signature" from "wrong target" learns whether the run
it just named exists.

**Unsupported verbs return 501 *after* authorization.** An unauthorized caller
must not learn which verbs a deployment implements, and an authorized caller
asking for a verb that does not exist deserves the honest answer rather than a
silent success.

---

## Implementation status

The isolated acceptance on 2026-09-13 exercised real Cognito approval,
PostgreSQL, DynamoDB, SQS, API Gateway IAM authentication and Kubernetes
TokenReview with workers sharing the bounded platform role. See
[the acceptance record](../evaluations/5028-delegated-authority.md) for source
versions, measured results, fixture limitations and cleanup evidence.

| Area | Status |
|---|---|
| Invocation/attempt identity and workload binding | Implemented; real bootstrap, renewal and cross-pod impersonation refusal verified |
| Grant model, relationship resolution and shared policy | Implemented; allowed descendant monitoring and denied ancestor/sibling/cross-flow requests verified |
| Approved Operations workflow | Wave binding, coordinator, developer, reviewer and eligible evaluation dispatch verified through the real CLI/gateway |
| Protected authority and registration/status writers | Implemented; own writes succeed while shared-role authority/event-table tampering is denied |
| Worker isolation | Separate service account and bounded role; live IAM/RBAC denial verified even with AdministratorAccess attached behind the boundary |
| Listener envelope and journal | Real socket adversarial/replay checks passed in the component harness; ordinary verbs remain unsupported |
| Credential renewal and revocation | Real refresh and listener-token rotation preserve attempt/generation/journal; revoked/cancelled flows refuse renewal and dispatch |
| Queued-action revalidation | Real final-source worker request refused after revocation in 456 ms, recorded rejection and made zero handoffs |
| Human-only approval and registered service scope | Human API and service transition checks; child ceilings and concurrent-registration transaction checks verified |
| Ordinary deployment | Flags remain off; the acceptance used disposable infrastructure only |


The isolated evaluation observes the role boundary, registration and workload
checks against real AWS/Kubernetes services. It does not constitute an ordinary
platform rollout. Existing privileged workers and incompatible shared-key modes
remain migration prerequisites, and unsupported SDK verbs remain separate work.

## AC8: what live acceptance requires

Unit and loopback coverage is **not** a substitute, and is not reported as one.
Live acceptance needs a deployed cluster and AWS credentials, and must use **two
actual worker identities, including two workers sharing the platform IAM role**.

Must prove **allowed** behaviour:
legitimate coordinator dispatch and monitoring; delegated controls actually
granted; credential renewal during an active task without restarting the worker
or resetting its generation/journal; identical retries preserving command ID and
recorded outcome.

Must prove **denied** behaviour:
forged parent/flow IDs; unauthorized sibling, ancestor, cross-flow and
cross-tenant targets; database authority tampering; worker-forged envelopes;
changed action/body; old generations; expired and revoked grants;
privilege-expanding child delegation; cancellation during queued work; direct
worker-to-worker listener traffic (NetworkPolicy).

Must also record: actual effects, the measured maximum revocation delay for
queued actions, and complete cleanup.

Refusal tests alone must not break the successful AI-DLC flow — a run where
everything is denied proves nothing about AC1.

Missing prerequisites and unsupported verbs are never reported as successful
controls.

## Operations assignment and workflow dispatch

A verified human launch of an Operations or AI-DLC coordinator on an intent
issue can acquire explicit scope for that intent's approved flow. Bootstrap
resolves the protected launch repository and issue against the configured
`BG_ORCH_DISPATCH_REPO` and the tenant's `orchestration_flows.intent_ref`.
Exactly one active matching flow and an actual human approval decision are
required. Missing approval leaves the original issue-scoped grant; ambiguous or
inactive matches refuse assignment. Neither a body-supplied flow ID nor a common
human root establishes this relationship.

The assignment atomically updates the protected execution and grant. It preserves
action, delegation and budget ceilings, caps validity by both authorities, and
advances the grant epoch so a racing request with the old scope cannot reserve
work. A coordinator that already dispatched children cannot change their
authority by enrolling later. Revoking either the original human launch or the
flow approval blocks subsequent dispatch and credential refresh.

`POST /internal/v1/agent/dispatch` resolves the requested issue to exactly one
story node within this assigned flow. A developer requires a `ready` story and
uses the existing guarded `ready → running` transition. A reviewer requires an
already `running` story and does not advance its attempt. A developer may request
review only for its own assigned story. Human gates are never spawnable. Operations evaluation dispatch requires an
approved wave binding and an eligible evaluation node, as described below.

Dispatch ordering is explicit:

1. Reserve the exact request, protected child identity and budget in DynamoDB.
2. Commit the guarded node transition and an append-only `agent_dispatched`
   receipt in PostgreSQL. The receipt attributes the action to the actual agent
   principal with `actor_kind=service`; it stores hashes and identifiers, not
   the instruction body, and can never root a human approval.
3. Recheck the committed receipt, current flow/node attempt and live grant, then
   publish the reserved envelope while holding the flow/node locks.

A SQL rollback leaves an unpublished reservation that the identical request can
recover. A lost commit acknowledgement is resolved through the receipt; a lost
SQS acknowledgement reuses the same invocation and FIFO deduplication ID.
Uncertain sends older than the existing 240-second replay window refuse further
publication and retain the reservation for investigation. They are not reported
as successful new work. Changing the request content or node attempt cannot
reuse that reservation. Bootstrap validates the committed receipt and current
workflow state again, so queued children of a subsequently cancelled flow cannot
receive usable credentials.

PostgreSQL tests exercise overlapping identical and competing requests, and a
cancellation holding the flow lock. These tests use a local isolated database,
emulated DynamoDB/SQS and HTTP workload-verifier doubles; they do not satisfy
the real two-worker IAM/network evaluation. Run them only against a disposable
database via `ADP_GRAPH_TEST_DATABASE_URL` with
`pytest tests/agentauth/test_graph_dispatch.py`. The fixture creates and removes
its organization and orchestration tables. Without that setting the ordinary
suite uses SQLite and skips the PostgreSQL lock tests.


## Scheduled service runs

A registered EventBridge rule can receive standing delegation through the
operator-plane `POST /agent-authorities/service-grants` endpoint. This endpoint
requires a human JWT with `PLAN_APPROVE` permission in the authenticated tenant.
Neither an internal IAM caller nor a service JWT can approve it. Registration
is a prerequisite, not approval: the identity-index `service_account` row must
name the tenant/org, exact repository, allowed root personas and rule ARN. The
security-dispatch Terraform row now contains these fields; other registered
rules must be migrated before authority mode can dispatch them.

Child dispatch also requires an explicit `allowed_child_personas` ceiling on
that registration. Missing or empty child scope permits no child delegation.
The security rule registers `operations`, `developer` and `reviewer` as children
while retaining `operations` as its only root persona. Approval checks both
subsets and transactionally pins the child attribute's value or absence; a
concurrent registration change cannot widen an approval. The example below
requires that separate child registration.

The approval body is explicit and rejects unknown fields:

```json
{
  "request_id": "6f1746ba-bf54-42d7-94aa-8778f21034a5",
  "service_identity": "eventbridge:adp-dev-security-agent-dispatch",
  "repo": "aws-e/adp",
  "root_personas": ["operations"],
  "child_personas": ["operations", "developer", "reviewer"],
  "child_issue_scope": "repository",
  "max_total_dispatches": 4,
  "max_dispatch_concurrency": 2,
  "max_chain_depth": 4,
  "expires_at": "2026-09-20T12:00:00Z"
}
```

Choose a future expiry no more than 30 days from approval; the example date is
illustrative. `same_issue` is the default child scope. `repository` is an explicit
permission to dispatch child work on other issues in that repository, useful for
nightly Operations runs that create evaluation/remediation issues after launch.
A root with no issue has work-item zero, which grants no wildcard by itself.
The standing policy delegates agent dispatch/monitoring; it does not grant the
approving human's personal credential-vault access.

The protected records are `TENANT#<org>/AUTHORITY#service-approval:<request_id>`
and `TENANT#<org>/SERVICE#<service_identity>`. Approval atomically checks the
registered service mapping and writes both records. Exact retries recover the
same approval; changed intent or an existing binding conflicts. To replace a
policy, use a new request ID and `replaces` containing the current authority
reference. Replacement atomically revokes the previous authority, including its
existing runs. `POST /agent-authorities/service-grants/{reference}/revoke` is
idempotent, preserves the first revoker/time, and returns 503 when the store
cannot confirm revocation. A 503 is an unknown result: retry the same reference.
Approval/revocation HTTP outcomes log actor, tenant, action, authority reference
and status without request bodies or credentials.

EventBridge transformers retain the native event UUID/account and insert the
registered rule ARN. The handler rejects API Gateway wrappers and mismatching
account/rule/repository claims. This relies on the Lambda resource permission
and caller IAM: workers must have neither `lambda:InvokeFunction` access to the
ingress Lambda nor `events:PutEvents` access to a matching source. The payload
fields alone are not cryptographic evidence. Real IAM evaluation remains part of
the two-worker acceptance run.

Each native event/persona gets one deterministic invocation and a distinct flow.
The service writer checks active policy/binding in the transaction that creates
execution, grant and global lookup records. A run expires within seven days, or
earlier at policy expiry. Bootstrap, refresh and child dispatch use the same
protected liveness checks as human-launched runs. The service remains the acting
principal; the approving human is recorded separately. Ingress retries preserve
the Activity row and cannot erase a listener registration or reset run status.

Dispatch ceilings apply per principal: the root has the policy's cumulative
child budget/concurrency (maximum 64/8); a child has at most one dispatch and one
concurrent child. Maximum chain depth is eight. Operations/AIDLC/Codex children
can delegate only their parent's allowed personas, issue scope and actions.
A developer can request a reviewer only when the policy includes reviewer.
A new scheduled event has a new root budget, subject to the existing per-service
rate limiter. This is not a policy-wide aggregate budget across all scheduled
events. No approval or infrastructure change has been applied by these tests.

## Materialized AI-DLC waves and dispatch ceilings

`bind-wave` is available only to a root coordinator already enrolled against a
real engine approval. It cannot mint approval or edit the graph. The gateway
checks both operational issues are native children of the approved epic, then
reserves an immutable DynamoDB mapping and unique issue aliases. A matching
append-only SQL receipt makes that mapping usable. A missing receipt, changed
graph snapshot, conflicting issue alias or replaced approval prevents use.
Exact retries recover the same binding; a new root cannot overwrite it.

Wave Operations runs have a unique durable launch receipt per bound wave and
inherit explicit actions, repository, deadline, concurrency and depth ceilings.
They can dispatch only their wave's stories/evaluation, plus an approved graph
successor after evaluation passes. They cannot create bindings, approve gates,
monitor siblings, or obtain whole-flow target scope. The EVAL node anchors the
coordinator receipt without changing EVAL state; actual evaluation dispatch
commits the existing guarded transition and its separate dispatch receipt.

New human Operations/AIDLC launches retain four root dispatches, two concurrent
children and depth eight. Their protected grant now explicitly permits a child
wave coordinator up to 16 dispatches, sufficient for six developer/reviewer
pairs, evaluation and successor kickoff. Each child wave inherits at most that
same ceiling. This is a per-principal bound, not a flow-wide aggregate budget.
Previously issued grants are not expanded: missing child ceilings fall back to
the parent's existing total, and missing Operations persona permission refuses
handoff. Developer/reviewer limits remain unchanged. Exhaustion requires human
recovery rather than an agent-created replacement identity or increased grant.
