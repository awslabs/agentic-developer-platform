# #4303 — Transport for engine genesis across the webhook-ingress boundary

**Status:** ruled (spike; produces this note + a follow-up implementation issue)
**Issue:** [#4303](https://github.com/aws-e/adp/issues/4303)
**Parent story:** #4204 (PR [#4305](https://github.com/aws-e/adp/pull/4305))
**EPIC:** #4191 · intent #4120 · wave 4 scaffolding #4248
**Rulings in scope:** D-R12 (genesis derives from the SSO-attributed gate approver), R-O5c / AC-16 (internal plane must not carry promotion state), R-O5d (authority never read from an envelope), R-NF2 (dispatch idempotency)

---

## RULING

**Do not open any of the three routes. There is no transport, because there is no
boundary crossing to transport across.**

The gateway's **orchestration tick Lambda** — which already holds VPC attachment,
`rds-db:connect`, and runs the `adp-gateway` image containing
`src/orchestration/` — resolves genesis in-process and **produces the agent
envelope directly onto `adp-<env>-agent-submit.fifo`**.

`decision_id` never leaves the gateway. Resolution and use happen in the same
process, in the same transaction, one function call apart.

The webhook Lambda is **not modified**. It gains no database credential, no
genesis parameter, and no new responsibility. No route is added under
`/internal/v1/*`. `tests/orchestration/test_internal_plane_guard.py` passes
**unchanged**, which per the issue's own Validation section is the test of
whether a ruling weakened AC-16.

---

## Why the question was mis-framed

All three candidate routes in #4303 share an unstated premise: that the **webhook
Lambda** is the component that must receive or resolve genesis. That premise does
not survive reading the code.

The webhook Lambda is **one producer** onto a queue. It is not the dispatcher, not
the scheduler, and not the consumer.

| Link in the chain | What it actually is | Evidence |
|---|---|---|
| Enqueue | `publish_envelope(envelope: dict) -> str \| None` | `lambda/common/sqs_publisher.py:34` |
| Queue | `adp-<env>-agent-submit.fifo` | `infra/sqs.tf:17` |
| Scale-out | KEDA `aws-sqs-queue` trigger on **queue depth** | `infra/scaledjob.tf:335-339` (`queueLength: "1"`) |
| Consumer | agent-worker pod | `infra/scaledjob.tf` |

Nothing in that chain is coupled to Lambda. `publish_envelope` takes a plain dict
and calls `sqs.send_message` (`sqs_publisher.py:78`); KEDA scales on
`GetQueueAttributes`, so it cannot observe — and does not care — which principal
produced a message. **The consumer contract is the envelope dict, not the
Lambda.**

Once that is seen, the question "how does `decision_id` reach the webhook Lambda?"
dissolves. The correct question is "which principal should produce the envelope
for an engine dispatch?" — and the answer is the one that already owns the
decision rows.

### The three closed routes, re-verified

They are all genuinely closed. The issue's reasoning is correct; only its
conclusion (that one must therefore be opened) is wrong.

| Route | Status | Verified against |
|---|---|---|
| Webhook Lambda reads `orchestration_decisions` | **Closed.** No `vpc_config` on either Lambda; zero `rds-db:connect` in the module; `boto3`-only deps, no SQLAlchemy layer | `infra/lambdas.tf:20`, `infra/gitlab_lambda.tf:18`, `infra/iam.tf` (no RDS grant), `lambda/github/requirements.txt` |
| New genesis route on `/internal/v1/*` | **Closed.** Would break the equality allowlist, and importing `src.orchestration` into `src/internal/` breaks the AST guard | `test_internal_plane_guard.py:54-68` (allowlist), `:148` (equality assert), `:159-185` (import AST check) |
| Webhook Lambda calls `/api/orchestration/*` | **Closed.** Every route depends on `get_current_user` + an explicit permission; the Lambda holds no Cognito human JWT and should hold none | `src/orchestration/routes.py:114`, `:191`, `:285`; permissions asserted at `test_internal_plane_guard.py:329-334` |

**Correction to the issue body:** it cites the guard as
`modules/gateway/tests/test_internal_plane_guard.py`. The file is at
`modules/gateway/tests/orchestration/test_internal_plane_guard.py`. It also cites
`webhook-ingress/terraform/` and `webhook-ingress/src/`; the real tree is
`webhook-ingress/infra/` and `webhook-ingress/lambda/{common,github,gitlab,eventbridge}/`.
#4304 repeats the same two wrong paths. An implementation issue written against
the cited paths will not find its files.

---

## Why the tick is the right producer

### 1. It already has everything the webhook Lambda was said to lack

The three closed routes are all attempts to work around the webhook Lambda's
deliberate minimality. The tick has no such gap — it was built in #4203 with
exactly these capabilities:

| Capability | Tick | Webhook Lambda |
|---|---|---|
| VPC attachment | ✅ `orchestration-tick/main.tf:136-139` | ❌ none |
| `rds-db:connect` (single dbuser) | ✅ `orchestration-tick/iam.tf:47-54` | ❌ none |
| SQLAlchemy / asyncpg / RDS TLS bundle | ✅ runs the `adp-gateway` image (`main.tf:111-121`) | ❌ boto3 only |
| Already reads/writes promotion state | ✅ `src/orchestration/tick.py:259-296` | ❌ and must stay that way |

The tick is already the component that moves nodes `pending -> ready`
(`tick.py:285-296`) and appends decision rows (`tick.py:259-274`). Dispatching the
node it just released is the same component doing the next step of its own job —
not a new privilege.

### 2. It makes D-R12 stronger than any transport could

Every candidate transport shares one weakness: a reference crosses a trust
boundary, so the receiving side must re-resolve it and be trusted to actually do
so. That is a control someone can later remove.

Under this ruling there is nothing to re-resolve, because nothing was
transported:

```
tick_handler.handler                       # EventBridge, rate(5 minutes)
  └─ resolve_engine_genesis(session, org_id=…, decision_id=…)   # genesis.py
  └─ dispatch_node(session, node, genesis)                      # dispatch.py
  └─ publish_envelope(envelope)                                 # same process
```

`org_id` comes from the tick's own query context, never from a message —
satisfying the issue's tenant-isolation requirement structurally. The
`GenesisRefusedError` path (`genesis.py`) cannot be bypassed by a caller because
there is no remote caller.

### 3. The producer set stays closed to agents — this is the load-bearing check

Adding a producer to the queue is only safe if agents cannot join that set. They
cannot:

```
scaledjob-iam.tf:367-377   sqs:ChangeMessageVisibility, sqs:DeleteMessage,
                           sqs:GetQueueAttributes, sqs:ReceiveMessage
                           on arn:aws:sqs:…:adp-*-agent-submit.fifo
```

**No `sqs:SendMessage`.** `grep SendMessage scaledjob-iam.tf warm-pool.tf` returns
nothing. The only `SendMessage` grant on this queue is the webhook Lambda's
(`infra/iam.tf:32-50`).

So after this change the producer set is exactly **{webhook Lambda, tick Lambda}**
— two scheduled/edge components, neither reachable from an agent pod. An agent
that wanted to forge an engine dispatch would have to obtain `sqs:SendMessage`,
which is an IAM change and therefore a review moment. That is the same
"boundary that config cannot silently erase" property the internal-plane guard
was built to give (`test_internal_plane_guard.py:14-16`).

### 4. Gateway → SQS is precedented, not novel

The gateway already produces to SQS from application code:

- `src/knowledge/dispatch.py:224` — `client.send_message(...)`
- IAM: `modules/gateway/infra/main.tf:1421-1440` —
  `sqs:SendMessage` + `sqs:GetQueueUrl` scoped to a queue ARN passed in as a
  variable (`var.agent_context_ingestion_queue_arn`), flag-gated

That is the exact shape needed here, including the cross-module ARN-as-variable
pattern for referencing a queue owned by a different Terraform state. Follow it.

`dispatch.py`'s **row-before-publish** invariant (`dispatch.py:7-14`) is also the
right failure model for this work — see 🟠-3 below.

### 5. Network reachability is already provisioned

A VPC Lambda in private subnets needs a path to the SQS API. Both exist:

- **SQS interface VPC endpoint**, private DNS enabled, on the private subnets —
  `platform/infra/modules/networking/main.tf:418-429`
- The tick's SG already egresses 443 to `0.0.0.0/0` —
  `orchestration-tick/main.tf:57-62`

No networking change. And note `infra/sqs.tf` sets no `kms_master_key_id`, so the
queue uses SSE-SQS and **no KMS grant is required** — do not add one
speculatively.

---

## Rejected and considered alternatives

| Option | Verdict |
|---|---|
| 1. Webhook Lambda reads `orchestration_decisions` | **Rejected.** Requires `vpc_config` + `rds-db:connect` + a SQLAlchemy layer on a deliberately minimal, internet-facing component. Widens blast radius to the whole gateway schema — the issue's own third bug class. |
| 2. Genesis route on `/internal/v1/*` | **Rejected.** Directly violates R-O5c/AC-16. Agent pods can call every internal route with any method (`test_internal_plane_guard.py:6-12`), so this hands agents write access to the approval record with no permission change. The single highest-value control in the EPIC. |
| 3. Webhook Lambda calls `/api/orchestration/*` | **Rejected.** Needs a human Cognito JWT the Lambda must never hold. Giving it a machine credential that passes `get_current_user` is worse: see the note on M2M paths below. |
| 4. **Tick produces the envelope directly** | **RULED.** No transport, no new route, no new credential on the Lambda, precedented producer pattern. |
| 5. Tick produces to a **new, separate** engine-dispatch queue | **Deferred, not rejected.** Cleaner isolation, but needs a second KEDA ScaledJob and a second worker contract for an envelope the worker already understands. Revisit only if engine dispatches need different visibility-timeout or DLQ semantics than webhook triggers. Reuse the existing queue first. |

### A note on why option 3 is worse than it looks

Two machine paths already reach `/api/orchestration/*` without being a human JWT,
and an implementer reaching for "just give the Lambda a service credential"
should know they exist:

1. **SigV4 → agent registry inside `get_current_user`.** If
   `settings.trust_apigw_headers` is set and `x-caller-identity` is present,
   `get_current_user` short-circuits the JWT branch entirely and returns a
   registry-derived context (`src/auth/dependencies.py:141-189`). Presence of the
   header is terminal (`:143-152`), and CloudFront strips it on `/api/*`
   (`infra/modules/cloudfront/main.tf:116-127`) — but API Gateway's `/{proxy+}`
   route is `auth NONE` (`api-gateway/main.tf:220-221`) and does not clear it.
2. **Cognito `client_credentials` M2M** app client with
   `custom:account_type=service` (`cognito/main.tf:461-491`;
   `cognito/lambda/pre_token_generation.py:171-176`). The orchestration router
   does **not** check `account_type` — there is no `require_human_user` on it
   (that guard exists at `src/auth/middleware.py:193-206`, used only by
   `vault_routes.py:95`).

Both currently land on `AdminRole.MEMBER` by least-privilege default
(`src/admin/access_control.py:201-209`), which holds only `USAGE_READ` — so
neither can approve a plan **today**. But that is a default, flippable by
`BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT=false`, and `ORG_ADMIN` does hold
`PLAN_APPROVE` (`src/admin/config.py:107`). Option 4 avoids depending on that
default entirely. Worth its own hardening issue regardless of this ruling.

---

## What the implementation must get right

These are not polish. Each is a way the ruled design fails if implemented naively.

### 1. Envelope dedup key must include `node_id` — else dispatches vanish silently

`sqs_publisher.py:73-74` builds
`MessageDeduplicationId = f"{arrived_at}_{repo}_{issue}"[:128]`, and the queue also
sets `content_based_deduplication = true` (`sqs.tf:20`). SQS FIFO dedup has a
**5-minute window**, and the tick's default schedule is `rate(5 minutes)`
(`orchestration-tick/variables.tf:107`) — the two intervals are the same order of
magnitude, which is the dangerous case.

Two nodes on the same issue, or a legitimate re-dispatch, can collapse to one
message and be **accepted-then-discarded by SQS with a success response**. That
is a false-positive dispatch: `dispatch_node` commits `running`, `publish_envelope`
returns a MessageId, and no run ever starts.

Use a dedup key derived from `node_id` + `decision_id`. Do not reuse the
webhook path's key shape.

### 2. `MessageGroupId` collapses for nodes with no issue

`MessageGroupId = f"{tenant_id}#{repo}#{issue}"` (`sqs_publisher.py:70`).
`OrchestrationNode.issue_ref` is **nullable** — eval and gate nodes frequently
have no issue (`src/orchestration/models.py:182-184`). Every such node in a tenant
would share the group `tenant##`, reintroducing exactly the tenant-wide
head-of-line blocking that `sqs_publisher.py:3-8` says the per-run group was
chosen to avoid.

Group per node, not per issue.

Related scope question the follow-up must answer explicitly: a node with no
`issue_ref` has nothing for an agent to act on. Either engine dispatch is
**story-nodes-only** for now (and eval/gate advance by other means), or
materialising an issue is part of dispatch. Do not leave this implicit.

### 3. Publish is outside the transaction — adopt row-before-publish

`dispatch_node` does not commit (its docstring is explicit: "Nothing is committed
here"). The SQS send cannot be inside the DB transaction. So the ordering must be
chosen deliberately, and the failure mode named:

- Commit, then publish: a publish failure leaves the node `running` with no run.
- Publish, then commit: a commit failure leaves a run with no `running` node —
  which `deviation.py` would correctly flag as off-graph work.

Take **commit-then-publish** (the `knowledge/dispatch.py:7-14` invariant), and
state that the recovery path is the stall/halt detector from **#4211**. If #4211
is not yet merged when this lands, say so in the issue — a node stuck in `running`
with no run and no detector is an invisible stall, the exact pain #4077 was filed
about.

### 4. Do not call `spawn_persona` wholesale

`spawn_persona` (`lambda/common/spawn_persona.py:60`) is the webhook path's
enforcement point and bundles concerns the engine either does not need or must not
inherit: self-mention and self-re-trigger guards (`:337-359`), cross-persona loop
detection (`:361-375`), `MAX_CHAIN_DEPTH` capping (`:377-389`), and DynamoDB
correlation-pointer writes keyed on `channel_key` (`:402`).

Note the docstring at `correlation_store.py:70-88`: pointer provenance is
**advisory, never authoritative**, because the agent pod can write it (that is
#4304). The engine must not acquire its authority from anything in that store.
Build the envelope explicitly; reuse `publish_envelope` only.

### 5. Envelope provenance fields, and the honest limit of this ruling

The envelope carries `correlation.root_human_id` / `is_human_rooted`
(`spawn_persona.py:484-524`). #4303 says a resolved identity must never be
transported — and this ruling **does** put one in a message. That deserves a
straight answer rather than a redefinition:

The constraint exists to stop a *claim* from becoming authority. Here the producer
is the same trust domain that owns the decision rows, the agent pod cannot produce
onto the queue at all (🟠-3 above / §3), and the pod never re-presents these fields
to obtain anything — the authoritative record is the
`orchestration_decisions` row written inside the committed transaction. The
envelope fields are **attribution for the run's audit trail, not a credential.**

That is sound, but it is narrower than "identity is never transported." State it
that way in the follow-up issue rather than claiming the stronger property.

Two concrete follow-ups fall out:

- **Marker signing.** If the engine's dispatch is to carry a verifiable marker,
  the tick needs the signing key. `MARKER_SIGNING_KEY_SECRET_ARN` is injected as
  env on the webhook Lambda (`infra/lambdas.tf:57`) but I found **no matching
  `secretsmanager:GetSecretValue` grant for it in `infra/iam.tf`** — only the
  ScaledJob role gets one (`scaledjob-iam.tf:268-274`). Verify whether webhook
  marker verification is actually live in dev before assuming the tick can mirror
  it; the key loader fails soft (`marker_verify.py:77-80`).
- **Provenance POST.** `post_provenance` targets `/internal/v1/provenance`
  (`lambda/common/gateway_client.py:397-434`), which is already on the allowlist
  (`test_internal_plane_guard.py:64`) and is *not* promotion state — so using it
  is AC-16-safe. But the tick is in-process with the DB and should write
  provenance directly rather than loop back through HTTP.

### 6. IAM and Terraform wiring

- Add `sqs:SendMessage` + `sqs:GetQueueUrl` to the tick role, **scoped to the
  queue ARN** — never `Resource: "*"`. Add to
  `modules/gateway/infra/modules/orchestration-tick/iam.tf`.
- The queue lives in a different Terraform state
  (`modules/agent-factory/webhook-ingress/infra/`). Pass the ARN in as a variable,
  following `var.agent_context_ingestion_queue_arn` (`gateway/infra/main.tf:1421-1440`)
  — or read it from SSM, as the webhook module already does for the gateway URL
  (`webhook-ingress/infra/main.tf:92-93`). Do **not** hardcode.
- No KMS grant (queue is SSE-SQS, `sqs.tf` sets no `kms_master_key_id`).
- **Deploy note:** this puts the change entirely in `gateway/infra` +
  `gateway/src`. It does **not** need `webhook-ingress-deploy.yml`. That
  contradicts #4204's and #4248's deployment sections, which both assert the
  trigger-handler change ships via `webhook-ingress-deploy.yml` — under this
  ruling there is no trigger-handler change. Correct those bodies, or the operator
  will run a deploy that changes nothing and conclude dispatch is wired.

---

## Consequences for the wave-4 evaluation (#4241)

**Check 1 as written cannot pass under this ruling, and must be amended.** It
greps the *webhook* Lambda's log group:

```
aws logs tail /aws/lambda/adp-dev-github-webhook --since 15m | grep -c engine_genesis
```

Under this ruling the webhook Lambda never sees genesis; the log line is emitted
by `resolve_engine_genesis` (`genesis.py`) running in the tick. The correct target
is `/aws/lambda/adp-dev-orchestration-tick` (name pinned by
`orchestration-tick/main.tf:29-31`).

Check 4 (`GET /api/orchestration/flows/{flow_id}` returns a `running` node) is
unaffected by the transport choice — **but that endpoint does not exist**. The
router has exactly three routes: `POST .../amendments`, `GET .../plans`,
`GET .../cost` (`routes.py:114`, `:191`, `:285`), and the guard's
`expected_permissions` equality allowlist confirms that set is complete
(`test_internal_plane_guard.py:329-334`, asserted `:352`). Checks 4 and 9 both
depend on a flow-read endpoint that is unbuilt — that is a gap in wave 4's
evaluation independent of this spike, and it belongs to the graph-view story
(#4191 child 12).

---

## Acceptance criteria for the follow-up implementation issue

1. `resolve_engine_genesis` and `publish_envelope` are called from the **same**
   process; no `decision_id` appears in any HTTP request body or SQS message.
2. `tests/orchestration/test_internal_plane_guard.py` passes **with a zero-line
   diff**. Any edit to it means the ruling was not followed.
3. No file under `modules/agent-factory/webhook-ingress/lambda/` is modified.
4. No `rds-db:connect`, no `vpc_config`, and no DB secret is added to
   `webhook-ingress/infra/`.
5. Tick IAM grants `sqs:SendMessage` scoped to the queue ARN; a test asserts the
   policy contains no `Resource: "*"` for SQS.
6. **Negative test:** a dispatch attempt whose genesis was constructed from a
   caller-supplied `root_human_id` is impossible to express — `EngineGenesis` is
   obtainable only via `resolve_engine_genesis` (asserted at type level or by an
   AST test, per the pattern already used for the `persona`/`actor` ban in #4305).
7. **Negative test:** two dispatches of distinct nodes sharing an `issue_ref`
   produce two distinct `MessageDeduplicationId` values (guards 🟠-1).
8. **Negative test:** a node with `issue_ref = NULL` does not produce a
   `MessageGroupId` of the form `tenant##` (guards 🟠-2).
9. `tests/orchestration/test_genesis.py` (all AC-30 refusals) passes unchanged.
10. #4241 check 1 is amended to the tick's log group before the evaluation runs.

---

## References

- Parent story #4204 · PR #4305 (ships `genesis.py`, `dispatch.py`, `deviation.py`)
- EPIC #4191 · intent #4120 · wave-4 scaffolding #4248 · wave-4 eval #4241
- #4304 — agent pod can write its own DynamoDB chain-provenance attributes
  (bounds what any provenance claim may assert; also carries the wrong
  `terraform/` path)
- #4203 — the tick this ruling makes the dispatch producer
- `docs/design-notes/4010-internal-plane-alb-separation.md` — the routing-layer
  half of internal-plane separation
- `docs/design-notes/4077-orchestration-graph-state-invariants.md`
