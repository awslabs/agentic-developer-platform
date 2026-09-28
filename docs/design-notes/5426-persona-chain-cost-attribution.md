# PMM-08 — Persona and chain cost attribution, explainability, and retirement alerting

Design note for issue **#5426**, a child of EPIC **#5417** (per-invoker agent
persona-to-model mapping). Binding inputs: the epic, and the six operator
decisions locked on **#5418** (D1 two-axis policy, D2 fail-closed overrides,
D3 real allowlist gates, D4 canonical default `us.anthropic.claude-sonnet-4-6`,
D5 gateway-signed snapshots, D6 harness compatibility).

> ## Status: PROPOSED — pending the #5417 synthesis review
>
> This is a **story-local design document, not the canonical contract.** #5417's
> synthesis gate (operator comment, 2026-09-18T13:43:58Z) requires that the eight
> downstream architect runs be reconciled into *one* unified, versioned design for
> the epic before any developer is dispatched, and states that story-local
> documents "may provide detail, but may not override the canonical design
> silently."
>
> Accordingly: where this note and the merged canonical #5417 design differ, **the
> canonical design wins** and this note is to be corrected, not applied. The
> sections this note contributes to that synthesis are the audit/cost/evidence
> schema (§5) and the attribution write path (§4). Merging this note authorizes no
> implementation and no deployment.
>
> **Operator synthesis rulings applied.** The seven first-pass rulings on PR #5444
> (2026-09-18T14:04:29Z) are incorporated: pricing-policy/snapshot revision on
> every spend row (§5.4, R-1), harness identifier consumed from PMM-06 rather than
> invented here (§5.5, R-2), independence from
> #4230 with labelled semantics and a reconciliation test (§6 R-3), PMM-03 as the
> single lifecycle/retirement signal (§7 R-4), split alert delivery — owner-visible
> pull plus operator-only push (§7 R-5), persona/chain sourced from protected
> execution facts (§4, R-6 — ratifies the decision this note already made), and
> this proposed-not-approved standing (R-7).
>
> **Second-pass rulings applied** (PR #5444 review at `443e7491`, plus #5417's
> unified rulings of 2026-09-18T16:42:26Z). Five corrections, each of which changed
> a requirement rather than wording:
>
> | Ask | Applied in | What changed |
> |---|---|---|
> | Canonical service-principal **preference-owner** attribution, distinct from approving-human audit and from the authenticated/billing principal | **§5.6**, §3, §6.2 | Three principal dimensions are now named separately. Previously this note recorded one principal and would have attributed service-account spend to the approving human |
> | Persist the **complete pricing-revision tuple** needed for reproduction, not only generation + snapshot | §5.4 | `pointer_revision`, `policy_version` and `source_kind` added. Generation alone does not identify a rate set (§5.4) |
> | Define durable retirement-alert **transition/deduplication** so a 5-minute scan cannot publish once per tick | **§7.4**, AC-07b | The previous revision asserted "once per event, not per tick" with no mechanism. A durable state transition is now specified |
> | Reconcile the **stale claim that PMM-06 lacks a harness revision** | §5.5, §6.1 | **Withdrawn and corrected.** PMM-06's note now carries `harness_compatibility_revision`. The gap was real against the issue body, not against the current design head |
> | Source chain/persona only from protected **grant/execution/snapshot** facts | §4.3 | Grant added as a named third protected source; §5.6 uses it |
>
> **Third-pass rulings applied** (PR #5444 review at `88efc5fe`). Three asks, all
> three defects in this note rather than wording, and **two of them contradicted
> claims this note was making.** Each is corrected in place; the superseded
> alternative is removed rather than retained beside the ruling.
>
> | Ask | Applied in | What was wrong |
> |---|---|---|
> | Source attribution only from protected facts; **do not label `webhook-events` or `RunBinding` immutable** | **§4.3 (rewritten)**, §3, §4.4 | The trusted-source list admitted `RunBinding` on a **read-side** criterion ("the server builds it, not the caller"). The worker role holds unconditional `UpdateItem` on the table it projects (`dynamodb.tf:230-235`). The criterion is now *the subject cannot write it*, `RunBinding` is off the list, and the one field this story does read from that row carries an explicit, bounded justification |
> | Define a **protected carrier** for canonical service-principal ownership, distinct from approving-human audit identity | **§5.6a (new)** | §5.6 named `service_authority.py:103`'s `service_identity` as the source. `AuthorityReference` has four fields and that is not one of them (`grants.py:110-117`, `store.py:716-721`), so an implementer reaches a dead end and the nearest populated field is the approving human — the exact misattribution §5.6 forbids. The carrier is now a stated requirement with two admissible placements |
> | Make retirement alerting **race-safe with a conditional claim before publish**; remove the contradictory publish-before-marker sequence | **§7.4 (requirements 2, 4, 5 rewritten)**, AC-07b3 | The note specified **publish first, then persist** — the opposite of the mechanism it cited. `stall.py:488-558` claims the transition first precisely because publishing first is the duplicate the guarantee forbids. The lost-alert hazard I was guarding against is real, and is now answered by a two-state claim (`claimed` → `delivered`) that a later tick retries |

**Repository claims were checked against `ae598410` and re-verified at `c4809bb1`
(default branch tip at this revision).** Where the issue body's premise no longer
holds, §2 records the correction; the design is built on the corrected fact, not
the stale one. Sibling-story claims are read at each story's **current head**, not
its filed issue body — see §10. Section numbers are stable so sibling stories can
cite them.

---

## 1. What this story is for

Three capabilities, one shared prerequisite.

1. **Attribution.** Each recorded model call should say which *kind of agent*
   (persona) was running and which *chain of agents* it belonged to, so spend can
   be totalled per persona rather than only per person and per model.
2. **Explainability.** A person should be able to ask "which model is in effect
   for my architect agent, and why?" and get the server's own answer.
3. **Retirement alerting.** When a configured model stops being usable, that
   should surface before the next run fails. Per ruling R-5 this splits by
   audience: the person who configured it sees it **when they look** (the Agent
   Models screen, `adp models explain`/`list`, and this story's explainer), while
   environment **operators** get an asynchronous alert pushed through the one
   existing channel. Per-owner push delivery is out of scope until a
   notification-address capability is separately authorized (§7.2).

The shared prerequisite is that the persona and chain values must be **trustworthy
at the moment the spend row is written**. Attribution that the billed party can
set is not attribution. §4 is therefore the load-bearing section of this note.

---

## 2. Premise corrections against `ae598410`

The issue body was authored before two changes landed. Six of its premises are
stale or inverted. Each correction changes a design decision, so none is cosmetic.

### C-1 — The write side of the *same* attribution pattern shipped two commits ago. **Reuse it; do not invent.**

The issue's reuse table treats `graph_address` as prior art for the *column
discipline* only. It is much more than that: **#4898 / PR #5342 (`ae598410`,
merged 2026-09-18) shipped the complete trustworthy-write mechanism for exactly
this table**, and it is the template this story should follow end to end:

- `modules/gateway/src/orchestration/dispatch.py:172` — `GraphAttribution`, a
  frozen dataclass that **only** `validate_engine_authority` may construct.
- `modules/gateway/src/agentauth/engine.py:245-255` — constructed only after
  live SQL re-proved the caller's assignment.
- `modules/gateway/src/agentauth/model_identity.py:195` — attached to the
  request's `TokenContext` as a pydantic `PrivateAttr`.
- `modules/gateway/src/shared/schemas/auth.py:154` — `_graph_attribution`
  declared as `PrivateAttr`, which **pydantic will not populate from constructor
  input**. No header, body field or query parameter can select it. Unforgeable by
  construction rather than by validation.
- `modules/gateway/src/usage/service.py:115-169` — `_graph_address_for`, with two
  documented must-hold properties: it **cannot raise** (both callers swallow
  exceptions, so an exception would silently drop the entire spend row — HTTP 200
  with unmetered spend and no alarm), and **absent means NULL, never a
  placeholder**.

**Consequence for this story:** persona and chain attribution should be carried by
a sibling frozen value object attached as one more `PrivateAttr` in the same block
at `model_identity.py:195`, and read by a sibling of `_graph_address_for` in the
same writer. This is a much smaller change than the issue implies, and its
security properties are *inherited* rather than re-argued.

**But do not simply add fields to `GraphAttribution` itself.** That object only
exists for `gate_decision` runs, so persona hung off it would be NULL for all
ordinary agent traffic. §4.3 and §4.4 give the correct carrier and why it
matters.

### C-2 — `attributed_org_id` is **not** a `usage_logs` column. The story's AC-05 premise is inverted.

The issue says the `org_id` versus `attributed_org_id` split is a column-level
distinction "in the cost stores" and that attribution must use the billing
dimension. The real shape:

- `attributed_org_id` is a **`TokenContext` field**, not a column anywhere
  (`modules/gateway/src/shared/schemas/auth.py:74-76`, defaulted by
  `_default_attributed_org_id` at `:165-176`).
- `usage_logs.org_id` **already holds** `context.attributed_org_id`
  (`modules/gateway/src/usage/service.py:90`, with the comment at `:87-89`:
  *"usage_logs is an ATTRIBUTION surface … Never context.org_id
  (authenticated-only, authorization's field)"*).
- The invariant is #4132, restated at `modules/gateway/src/auth/middleware.py:424-433`.

**Consequence:** AC-05 is **already satisfied by the existing write path** and
needs no new column or logic. What AC-05 should actually test is that the new
per-persona *read* aggregates on `usage_logs.org_id` (the billing dimension
already stored) and that no new read path substitutes `TokenContext.org_id`. The
story's framing would have a developer add a redundant column. Restate the AC.

### C-3 — `ADP_BEDROCK_VIA=direct` **no longer exists**. The AC-06 completeness caveat needs a new, true justification.

The issue (and the `graph_address` column comment it quotes) says
`ADP_BEDROCK_VIA=direct` writes no usage row. That was true when migration 031
was authored; it is not true now:

- `modules/agent-factory/agent-worker-image/entrypoint.py:2161` —
  `ADP_BEDROCK_VIA must be gateway …; direct/platform bypass modes are no longer
  supported.` Any other value raises before the agent spends anything.
- `entrypoint.py:82-90` — `user` mode is separately retired by #4747 (ruling 3 of
  #4692) *precisely because* it "billed Bedrock to the customer's account while
  writing no usage row".

So the specific hole AC-06 names is closed. **AC-06 is still right to demand a
completeness caveat** — but on the real remaining causes, which are:

1. **Chat-log settlement, not `usage_logs`, is the authoritative ledger for
   Budget & Spend.** `modules/gateway/src/proxy/mantle_service.py:809-811`:
   *"Budget & Spend reads `budget_usage`, not `usage_logs`. Only the S3 event
   consumed by budget-usage-tracker settles that ledger."* `budget_usage`
   (`modules/gateway/src/shared/models/budget.py:24-42`) has **no persona, model
   or run dimension at all**, so a per-persona view can never be reconciled
   against it — only against `usage_logs`.
2. **Both `usage_logs` writers swallow exceptions**
   (`modules/gateway/src/proxy/service.py:479`,
   `mantle_service.py:716`), so a row can be lost silently on either path.
3. **A raw-SQL post-hoc UPDATE can change cost after the fact**
   (`modules/gateway/lambda/budget-usage-tracker/handler.py:314` sets
   `cost_usd` when it was written as 0), so a per-persona total read before
   settlement differs from one read after.

Rewrite AC-06's test to include a row whose cost is settled late, not a
direct-Bedrock row that can no longer be produced.

### C-4 — #4230 is **not** an invoice-reconciliation issue. The "must not overstate" paragraph cites the wrong thing.

The issue leans hard on #4230 as "reconciliation of recorded cost against actual
provider billing … verified open". #4230 is actually **"feat(budget): count
human-triggered agent spend against the triggering human's budget"** — that agent
spend is attributed to the *agent's* identity rather than the human who triggered
it. It is open.

I searched for an invoice/provider-billing reconciliation issue and **found
none**. So:

- The **caution is still correct and should stand**: this story must claim
  internal consistency only, never invoice reconciliation. That claim needs no
  issue citation — it is true because nothing in the platform compares recorded
  cost to a provider invoice.
- But #4230 is **a substantive dependency in a different way**, and the issue
  misses it: #4230 will change *which principal* a row is attributed to. A
  per-persona cost view keyed on today's `user_id` will shift meaning when #4230
  lands. §6 handles this.

### C-5 — The Alembic head advances faster than this design can name it. Do not hard-code a number.

The issue says to confirm the head; the sibling PMM-02 story names `052` as head
and proposes `053`. Both are already wrong, and so was this note's previous
revision:

- at `ae598410`, head was `053_orchestration_flow_slug_unique.py`;
- at `c4809bb1` (tip at this revision) head is
  `modules/gateway/alembic/versions/054_execution_tenant_guards.py`, and the
  directory holds 55 revision files.

Chaining onto a stale number creates a **second head** and `alembic upgrade head`
then fails for everyone — the failure migration 031's own docstring records. The
durable instruction is therefore *not* a number: **resolve `down_revision` from the
live head at the moment the migration is written**, and treat any number written in
this note or in a sibling story's issue body as illustrative. Expect `055`+.

### C-6 — The catalogue has **no retirement or lifecycle field**. AC-07's trigger does not exist.

AC-07 says "mark a configured model retired, then run the alert job". There is
nothing to mark. The pricing snapshot
(`modules/gateway/pricing_policy/snapshots/2026-09-12.2.json`) carries
`snapshot_version`, `policy_version`, `bundle_revision`, `provenance.verified_at`
and per-model `endpoint_availability` — but the union of all per-model keys
contains **no** `retired`, `deprecated`, `sunset`, `end_of_life` or lifecycle
field. `modules/gateway/src/budget/pricing.py:136-138` handles a retired model
only as a *pricing* fallback ("A retired/excluded Claude model still has its
historical model-specific bundled quote until AWS publishes a row") — it has no
retirement signal to read.

**This is a blocking dependency, not a detail.** See §7 / B-1.

---

## 3. Scope and the two dimensions actually needed

The operator asked for "principal / chain / model / harness dimensions". Against
the verified tree:

| Dimension | Status at `c4809bb1` | This story |
|---|---|---|
| Authenticated / billing principal | **Present.** `usage_logs.user_id`, `account_type`, `org_id` (= attributed tenant) | Reuse unchanged (C-2) |
| **Preference-owner principal** (whose mapping selected the model) | **Absent, and not the same as the two above** for service work — see §5.6 | **Add** — required by the second-pass ruling (§5.6) |
| Model | **Present.** `usage_logs.model` | Reuse unchanged |
| Persona | **Absent.** No column; no `X-Agent-Persona` header exists anywhere in the repo; `TokenContext` has no persona field | **Add** (§4, §5) |
| Chain | **Absent from `usage_logs`.** `correlation_id` reaches the request on `RunBinding` (`modules/gateway/src/budget/run_binding.py:281`) but is never written to the spend row. Its source row is worker-writable; §4.3 bounds what may be read from it | **Add** (§4, §5) |
| Pricing revision | **Computed per request but not recorded on the row.** A durable `PricingDecision` carrying `generation_id`, `pointer_revision`, `snapshot_version`, `policy_version` and `source_kind` is already built at both writers; no usage column persists any of it | **Add the full tuple — required** by R-1 as sharpened in the second pass (§5.4) |
| Harness / compatibility revision | **Does not exist in the gateway at all.** No harness identifier in any model, column, header or snapshot. The Claude Agent SDK is pinned at `modules/agent-factory/agent/package.json:14` (`0.3.220`) but that version reaches no gateway surface. **PMM-06's design head defines it** as `harness_compatibility_revision` (§5.5) | **Record — consumed from PMM-06's snapshot, never composed here** (R-2, §5.5) |

Note the first two rows are distinct dimensions, not one. Conflating them is the
defect §5.6 exists to prevent.

---

## 4. Where persona and chain come from — the load-bearing decision

### 4.1 The requirement

Attribution must not be settable by the party being billed. The platform has an
explicit written standard for this: anything the agent pod can write is
attacker-controlled from the platform's point of view
(`modules/agent-factory/webhook-ingress/lambda/common/correlation_store.py`, cited
by PMM-06). A persona supplied as a request header would fail that standard — a
pod could attribute its Opus spend to a persona the principal mapped to Sonnet.

### 4.2 Where persona already exists server-side

Persona is recorded in three places, none of which reaches the write path:

1. **The protected DynamoDB `EXEC#` row** — `"persona": {"S": envelope["persona"]}`
   at `modules/gateway/src/agentauth/bootstrap.py:120` and
   `modules/gateway/src/agentauth/dispatch.py:304`. Written by the platform at
   dispatch; not writable by the worker.
2. **`RunCredential.persona`** —
   `modules/gateway/src/agentauth/run_credential.py:127-129`, documented
   *"Advisory for audit only — authority comes from the grant, never from this
   string."* **This field is inert today:** `mint_credential` accepts
   `persona=` (`run_credential.py:173`) but the sole production caller
   `issue_bound_credential` (`modules/gateway/src/agentauth/bootstrap.py:372-381`)
   does not pass it. A design that reads persona from the credential would read
   `None` on every real request.
3. **The `webhook-events` row** — carries `persona`, but the budget projection
   deliberately excludes it (`modules/gateway/src/budget/run_binding.py:262`),
   so it is not available on that path without widening the projection.

### 4.3 The recommended source

**Read persona from the `EXEC#` row that `AgentModelIdentityMiddleware` already
reads, and attach it as a `PrivateAttr` in the same block that sets
`_graph_attribution`.**

The middleware already reads that row at
`modules/gateway/src/agentauth/model_identity.py:80` (gate_decision path) and
again via `runtime.validate_flow` →
`modules/gateway/src/agentauth/routes.py:112`. `validate_engine_authority` already
reads `envelope["persona"]` into a local at
`modules/gateway/src/agentauth/engine.py:89` — **and discards it**. So on the
engine path the value is already in scope at zero extra I/O.

**One typed gap the developer must close.** For *all* authority kinds,
`runtime.authenticate` returns a `record` built by
`AuthorityStore.load_execution` → `_deserialize_execution`
(`modules/gateway/src/agentauth/store.py:667-712`) from that same `EXEC#` item.
That deserializer projects `workload_binding`, `flow_id`, `repo`, `arrived_at` and
`parent_principal` onto the frozen `ExecutionRecord`
(`modules/gateway/src/agentauth/execution.py:91-143`) — but **not `persona`**,
even though the item carries it. So `record` is the right carrier (it exists on
every path, is frozen, and is store-constructed by design — `execution.py:94-96`)
and it needs one added field plus one added line in `_deserialize_execution`.
`chain_depth` and `parent_principal` are written to the same item
(`dispatch.py:295-318`), so chain depth can ride along the same way.

Doing it here rather than in `engine.py` is what makes §4.4 work: `record` is
available before the `gate_decision` branch, so persona is reachable for
`github_event` and `service_policy` runs too.

Chain identity comes from the same block: `RunBinding` is constructed at
`model_identity.py:169-177`, carrying `correlation_id` from the `webhook-events` row
written at ingress. Note that row's weaker standing and the bound on what this story
reads from it — stated in the source table immediately below, not assumed away.

**The three protected sources, and the criterion that admits them.** The ruling
requires chain and persona to come only from protected grant / execution / snapshot
facts. The criterion is **not** "the server reads it rather than the caller sending
it" — it is **the subject of the attribution cannot write it.** Who performs the read
protects nothing; a server that faithfully reads a field the agent edited has
faithfully recorded the agent's claim.

| Source | Object | Why the subject cannot write it | Used here for |
|---|---|---|---|
| **Execution** | `ExecutionRecord` from the protected `EXEC#` item (`agentauth/execution.py:91-143`) | Table-level: **no worker IAM statement addresses the `-agent-authority` table at all** (`modules/agent-factory/webhook-ingress/infra/dynamodb.tf:237-245` — "absence of a grant requires nothing to be right"). Frozen and store-constructed (`execution.py:94-96`) | Persona (§4.3), chain depth |
| **Grant** | `grant.authority` — from the `GRANT#` item on the same protected table (`bootstrap.py:173-187`, deserialized at `store.py:716-721`) | Same table-level absence | Authority kind, `human_id` — audit only (§5.6) |
| **Snapshot** | PMM-06's signed chain snapshot | Gateway-signed at work admission, worker-unwritable storage, chain- and audience-bound (#5417 ruling 4) | Harness/compatibility revision (§5.5) |

**`RunBinding` is deliberately not on this list**, and the tempting argument for
admitting it — that every field of it is built from the ingress-written row *by the
server* — is the read-side criterion this section just rejected. `RunBinding` is
projected from the `webhook-events` row, and
**the shared agent worker role holds unconditional `dynamodb:UpdateItem` on that
table** — the infrastructure states the consequence in its own words: *"the record
describing a run is editable by that very run — attempt, tenant and human-rooted
provenance included"* (`dynamodb.tf:230-235`). `ExecutionRecord.parent_principal`
exists on the protected table for exactly this reason
(`execution.py:132-143`: lineage *"must live where its subject cannot write it"*),
and `RunBinding`'s own docstring claim that *"none of it is caller-supplied"*
(`run_binding.py:274-277`) is a statement about the request, not about the table.

**The narrow exception this story actually relies on, and why it is narrow.** Chain
identity (§4.3) reads `correlation_id` from that row. Two facts bound the exposure,
and both must be the *stated* reason rather than a coincidence:

1. **The field is written at ingress, before any agent code runs**, and it is **not
   among the attributes the worker updates** — those are eight named status fields
   plus the seven-member `control_*` set
   (`agent-worker-image/lib/invocation_status.py:240-267`, `:326-334`), each written
   with `ConditionExpression="attribute_exists(event_id)"` on an existing row.
   `correlation_id` appears in none of them; the identifier does not occur anywhere
   in that module. The budget path already depends on this same property and states
   it: *"the worker does send `x-agent-correlationid`, but nothing here reads it"*
   (`run_binding.py:20-24`) — so a worker that wants to relabel its chain has to
   reach the row, not the header.
2. **The grant is being withdrawn.** All three worker writers now post to the
   gateway's `/internal/v1/agent/self` routes when `agent_authority_enabled` is
   true, and `DynamoDBWebhookEventsUpdate` is dropped from the worker role under
   that same condition (`dynamodb.tf:247-252`). **But the flag is off by default, so
   the grant is live today** — and this is code, not just a comment:
   `scaledjob-iam.tf:58` reads
   `agent_worker_events_write = var.agent_authority_enabled ? [] : [ ... ]`, and that
   list is concatenated into the worker role's statements at `:118`. The Terraform
   comment states the reason the grant cannot simply be dropped: the gateway path has
   no DynamoDB fallback, so removing it unconditionally *"would freeze every run's
   status at `webhook_received`"* (`dynamodb.tf:253-256`).

So the honest standing is: *chain identity is read from a table the subject can
write, restricted to a field written before the subject existed and not touched by
any current worker write path.* An implementer extending attribution to any other
field of that row must re-establish this, not inherit it. Where a protected
equivalent exists it is preferred — `ExecutionRecord` already carries `arrived_at`
and `parent_principal` for precisely this reason.

Anything not covered above — a request header, a body field, or any other
`webhook-events` attribute — is not an attribution source for this story.

This inherits every property #4898 argued for, and the same two must-hold rules
apply verbatim: **the enrichment cannot raise** (or it drops the spend row), and
**absent must be NULL** (or historical and unattributable rows read as a real
persona).

### 4.4 The coverage limit that must be stated, not glossed

**Verified attribution today covers only one of three authority kinds.**
`modules/gateway/src/agentauth/routes.py:105-108`:

```
if grant.authority.kind in {"github_event", "service_policy"}:
    return None
if grant.authority.kind != "gate_decision":
    raise BootstrapRefusedError("unsupported authority source")
```

So `validate_flow` returns attribution **only** for `gate_decision` (the AI-DLC
orchestration path). `github_event` (ordinary webhook/mention dispatch) and
`service_policy` (scheduled/autonomous service-account work) get `None` — which
is why `graph_address` is legitimately NULL for them.

For **persona** this asymmetry is not acceptable in the same way. A graph address
genuinely does not exist off the engine path, so NULL there is the truth. But
persona is meaningful on all three paths — a `github_event` run absolutely has
one, and `bootstrap.py:120` writes it on exactly that path. **So persona must be
sourced from `record` (§4.3), which every authority kind has, and not from
`attribution`, which only `gate_decision` produces.** NULL is then recorded only
where there is genuinely no protected execution — chain/graph semantics unchanged.

**This is the single most important thing for the developer to get right**, and
the most likely silent failure: hanging persona off `GraphAttribution` — the
obvious move, since that is the object #4898 already threads to the writer —
would yield a per-persona cost view that is **empty for all ordinary agent
traffic** while looking correct in tests, because engine-path fixtures would
populate it and `validate_flow` returns `None` for everything else. AC-03's three-hop test must therefore
cover a non-engine authority kind, or it will pass on a view that reports nothing
in production.

---

## 5. The three deliverables

### 5.1 Attribution columns

Follow `modules/gateway/alembic/versions/031_usage_graph_address.py` exactly —
`ADD COLUMN … NULL`, **no** `server_default`, no backfill, and a partial index
`WHERE … IS NOT NULL` with both `postgresql_where` and `sqlite_where` so the
predicate survives on the SQLite test database.

Why nullable-no-default is not stylistic, in this table's own words: the gateway
pods running the pre-migration image INSERT without the column, and a `NOT NULL`
column with no default **fails every one of those in-flight INSERTs**; because the
writers swallow exceptions, that returns HTTP 200 with no usage row at all —
unmetered spend, no alarm (031's docstring). This is also the honest answer to the
issue's "must not lock the table": `ADD COLUMN … NULL` with no default and no
backfill is metadata-only on PostgreSQL 11+, and the partial index should be
created `CONCURRENTLY` or accepted as a brief lock — **state the measured
behaviour in the PR** rather than asserting it.

Guards that already exist and will catch mistakes: revision id ≤ 32 chars
(`modules/gateway/tests/migrations/test_revision_id_length.py`, #4123) and a
per-migration byte-identity test for pre-existing rows (the `test_031_*` pattern).

Each new column carries the same explanatory comment the three prior columns
carry, stating that NULL means **not captured** and never a real persona.

### 5.2 The explainer

Reuse the `/effective` pattern, and reuse the *function*, not the ladder:
`modules/gateway/src/admin/bedrock_routing/service.py:639` `resolve_effective` is
called by both the admin route (`routes.py:432`) and the self route
(`self_routes.py:210`). One resolution, two surfaces. AC-04's "a second precedence
implementation fails this AC" is satisfied structurally by calling PMM-07's
resolver — not by writing a careful copy of it.

Two properties to carry over that the issue does not name:

- **`own_selection_active` (`self_routes.py:200-232`).** A stored row is not
  evidence that it governs. The bedrock-routing surface computes server-side
  whether the person's own pick is actually in force, because showing a stale
  pick as active is the inert-config defect (#4511). The persona explainer needs
  the exact equivalent: a saved mapping that the allowlist or invocability gate
  (D3) would now reject must render as *not in force*, with a reason — which is
  also precisely what AC-10 asks for.
- **Refusal shape and self-route shape.** 422 `{reason, message}`
  (`self_routes.py:108-115`); `/me/*` prefix with **no target parameter at any
  position**, so a request naming someone else cannot be *formed*
  (`self_routes.py:26-38`). Register by appending the dotted module path to
  `UNIT_MODULES` in `modules/gateway/src/app.py:27`. Do not use an `/api` prefix
  — CloudFront strips it and the route 404s
  (`modules/gateway/tests/test_route_prefix_convention.py`, #4330).

### 5.3 The per-persona cost view

**Reuse `modules/gateway/src/orchestration/cost.py`'s contract.** The issue does
not cite this file; it already solves this story's hardest read problem. Its
three-valued `CostStatus` (`cost.py:138-149`) exists because `SUM` over zero rows
returns `0`, indistinguishable from a measured zero:

- `KNOWN` — rows exist, total > 0
- `NONE_INCURRED` — rows exist, total == 0 (a verified zero)
- `UNKNOWN` — no rows; **we do not know. NOT free.**

And `AggregateCost.partial` (`cost.py:231-249`) marks a rollup as a **lower
bound** when any member is `UNKNOWN`. That is exactly the honesty AC-06 and AC-09
demand: AC-09's "returns an honest empty result … does not imply spend" is
`UNKNOWN` with a reason, not `$0.00`.

Also inherit `cost.py`'s aggregation shape: **one grouped Postgres query**, never
enumerate-from-DynamoDB-then-`WHERE IN`, which had a 30-day TTL cliff that made
totals silently partial. And inherit its scope label — these are agent-run Bedrock
costs only, excluding CodeBuild, EKS, NAT and storage.

**Chain totals without double counting (AC-03)** follows from the ledger's grain:
each `usage_logs` row is one model call, already attributed to one hop. A chain
total is `SUM` over rows sharing a chain identifier, grouped by persona. Double
counting can only be introduced by joining to a per-hop table with its own cost —
so **do not** join `budget_usage` (it is a per-(entity, period) rolling
accumulator with no run dimension; `cost.py` asserts at source level that it
appears in no cost-read query, and `test_cost.py` fails CI if it reappears).

### 5.4 Pricing revision on every spend row (operator ruling R-1)

**Ruling:** *"Record the pricing-policy/snapshot revision on every spend row.
Historical persona totals must remain reproducible across rate changes."* Formerly
D-A in this note; **now settled as a requirement**, and cheaper to satisfy than an
open question would suggest.

**The value is already computed, and already in scope at the write block** — so this
is not a new lookup, and specifically **not** "record the snapshot version from the
bundled JSON", which identifies a file rather than a rate set. The platform builds one
durable `PricingDecision` per completed request, and it already carries a
database-level revision identity:

- `pricing_policy/policy.py:1041-1075` — `PricingDecision` carries
  `generation_id`, `pointer_revision`, `snapshot_version`, `policy_version` and
  `source_kind`.
- `pricing_policy/policy.py:1129-1135` — its docstring states the property this
  story needs verbatim: computed **once** at completion "against a single immutable
  cache generation, before both the S3 settlement emission and the usage-row
  write. Both receive this same object; **a later cache swap cannot change it**."
- `pricing_policy/storage.py:110-127` — `generation_id` is a real row identity in
  `model_pricing_generations` (`alembic/versions/044_model_pricing_v2.py:113`),
  with `pointer_revision` from the single-row active pointer
  (`044:254`). `policy.py:1146-1152` **refuses** a database decision without both.
- Both writers already hold the object where the row is written:
  `modules/gateway/src/proxy/service.py:557-565` (`priced`) and
  `modules/gateway/src/proxy/mantle_service.py:748-758` (`decision`).

**Consequence:** this is a persist, not a lookup. **Do not re-read the pricing
generation in the usage writer** — a second read can observe a different active
generation than the one that priced the row, which reintroduces exactly the
non-reproducibility the ruling exists to remove. Persist the identity carried on
the decision object already in hand.

**Required shape — the complete tuple, not generation plus snapshot.** The previous
revision proposed two columns (`pricing_generation_id`, `pricing_snapshot_version`).
The second-pass ruling requires *"the complete pricing-policy revision tuple needed
for reproduction"*, and two columns are demonstrably not enough. Record five, as
additive nullable columns in §5.1's single migration:

| Column | From `PricingDecision` | Why reproduction needs it |
|---|---|---|
| `pricing_source_kind` | `source_kind` | **The discriminator, and it must come first.** `policy.py:1146-1152` admits exactly two kinds — `database` and `bundled_snapshot` — and *refuses* a database decision without a positive generation and pointer (`policy.py:1149`), while refusing a bundled decision that claims either. Without this column a NULL generation is ambiguous between "priced from the bundle" and "revision not captured" |
| `pricing_generation_id` | `generation_id` | Identifies the immutable rate set row in `model_pricing_generations` (`alembic/versions/044_model_pricing_v2.py:113`) |
| `pricing_pointer_revision` | `pointer_revision` | **Not redundant with the generation.** It is the single-row active-pointer revision (`044:254`), and `policy.py:1146-1152` requires *both* to be positive for a database decision — so a generation alone is not a state the platform itself accepts as identifying |
| `pricing_snapshot_version` | `snapshot_version` | Identifies the published bundle for `bundled_snapshot` rows, where there is no generation at all |
| `pricing_policy_version` | `policy_version` | The policy-logic revision. Two rows can share a rate set and still be priced by different logic — e.g. different cache-write treatment — so rates alone do not reproduce an amount |

**What is deliberately *not* copied onto the row:** the embedded `rates` dict and
`variant_key` (`policy.py:507`, a five-tuple of model, geography, tier, context and
region). Those are already carried in the durable settlement event, and duplicating
a rate table onto every spend row would make `usage_logs` a second pricing source of
truth — the defect this whole section exists to avoid. The tuple above identifies
*which* rates applied; it does not restate them. If a future story needs the rates
themselves at read time, it joins on `pricing_generation_id`, which is exactly what
that column is for.

**Coverage limit, which must be stated in the view rather than glossed.** The
decision is not built for every row:

1. **The Bedrock proxy path computes it only for Anthropic models.**
   `modules/gateway/src/proxy/service.py:554` gates on
   `pricing_capture.is_claude`, defined at `src/proxy/pricing_capture.py:27-28`.
   A non-Anthropic row on that path has no decision, so the revision is NULL.
   Under D6 all direct persona execution is Anthropic-only, so this does not
   affect persona attribution today — but the columns are on a shared table and
   must not imply otherwise.
2. **Pricing can fail.** `service.py:574-579` sets `pricing_failed` and writes
   `cost_usd = 0`. Such a row has no revision. It must read as *unpriced*, not as
   a measured zero — which is precisely §5.3's `CostStatus` distinction.
3. **Late settlement rewrites cost.**
   `bridge_cost_to_usage_logs`
   (`modules/gateway/lambda/budget-usage-tracker/handler.py:314`, whose `UPDATE
   usage_logs SET cost_usd = CASE WHEN cost_usd = 0 …` is at `:339-342`)
   updates `cost_usd` after the fact, from the same
   durable decision (`handler.py:409-431`, which explicitly refuses to re-price a
   present decision). So the recorded revision stays consistent with the settled
   amount — this is the mechanism that makes R-1's reproducibility claim true
   rather than aspirational.

So NULL means **revision not captured**, never "priced at the current rate" — the
same rule as every other column here. A per-persona total spanning rows with mixed
or absent revisions is a `partial` lower bound in §5.3's sense, and must say so.

### 5.5 Harness / compatibility revision (operator ruling R-2)

**Ruling:** *"Record harness ID/compatibility revision from PMM-06; PMM-08 must not
invent it locally."*

This story is the **consumer**. It records whatever identifier PMM-06's signed
snapshot carries, as one further nullable column populated from the verified
snapshot on the request — never composed, normalised or defaulted here. A harness
string minted in this story would be a second source of truth for compatibility,
which is the defect D6 exists to prevent.

**The producer exists**, at PMM-06's current design head (PR #5442, `f9f0ec68`,
`docs/design-notes/5424-chain-model-policy-snapshot.md`). Its snapshot-contents table
defines **`harness_compatibility_revision`**, annotated *"D6 + U2. The **separately
versioned** harness/contract revision, part of the snapshot key. Each hop is checked
against its actual harness"* (§3.1 of that note), and its correction **C4** extends
#5424's AC-01 asserted field list with both `harness_compatibility_class` **and**
`harness_compatibility_revision`.

**Consequence for this story:** the field name to consume is
`harness_compatibility_revision`, and the column records it verbatim. **What is
carried is a revision, not a class.** Per #5417 ruling 2 — adopted as U2 at #5442's
head, which splits §3.1 into a stable class plus a versioned revision — the
compatibility *class* (`claude-agent-sdk`, `codex-sdk`) is stable and unversioned
while the harness/contract *revision* is the versioned part. This story records the
revision because that is what changes and therefore what evidence needs; the class is
derivable from the persona via PMM-03's registry (`compatibility_class` on persona
rows at #5434's rev-3 head, `25ece717`) and must not be duplicated here.

**The dependency is real but narrower than a gap.** PMM-06's AC-01 field list is
corrected in its design note, not yet in its issue, and that note is itself open and
unmerged. So the column is NULL until PMM-06 ships — the honest state, since it is
nullable and additive and NULL means not captured. What is *not* acceptable is
inferring the harness from the model family: under D6 harness compatibility
determines which models are selectable, so deriving one from the other makes the
recorded dimension circular and useless as evidence.

### 5.6 The preference owner is a third principal dimension (second-pass ruling)

**Ruling:** *"Add canonical service-principal preference-owner attribution distinct
from approving-human audit and authenticated/billing principal fields."*

**"Principal" is not one dimension, and treating it as one silently attributes
service-account spend to the wrong party.** The obvious implementation — group the
per-persona view by `usage_logs.user_id` — produces exactly that error. Here is the
mechanism that makes it wrong.

**Why the three collapse into one today.** `AgentModelIdentityMiddleware` sets the
run's principal from the grant's `human_id`
(`modules/gateway/src/agentauth/model_identity.py:71`), and for a `service_policy`
grant that value is **not resolved to a user entity** — the `:72` guard skips the
`:74` `resolve_root_user_entity_id` call for exactly that kind. That `human_id` was written by
`service_authority.py:99` as `{"S": user.user_id}` — *the human who approved the
standing delegation*, alongside `"actor_kind": {"S": "human"}` at `:100`. Both
attributes are on the same `AUTHORITY#` item written at `:96-108`.

So a nightly service-account run whose delegation was approved months ago by one
administrator records that administrator's ID. Ask "what does our architect persona
cost us, by whose mapping?" and the answer names a person who was not involved and
whose own mapping did not select the model. #5417 ruling 1 is explicit about this:
*"the approving human is audit attribution only"* (ruling 4, restating it for
`service_policy`), and *"Raw `service_accounts.id`, `agent_name`, `client_id`, ARN or
caller-supplied text never owns a preference"* (ruling 1).

### 5.6a The carrier gap — nothing on the request can hold this value yet

§5.6 establishes *which party* must own the preference. This section establishes the
harder half: **today there is no object on the request that can carry that party's
identity**, so naming a storage location is not enough to implement against.

The service's own identity is written on the stored `AUTHORITY#` item as
`service_identity` (`service_authority.py:103`). **The object the request path
receives cannot carry it.** `AuthorityReference` has exactly four fields — `kind`,
`reference_id`, `human_id`, `org_id` (`agentauth/grants.py:110-117`) — and
`_deserialize_grant` populates them from the `GRANT#` item's four `authority_*`
attributes (`store.py:716-721`), which `_grant_item` writes as exactly those four
(`bootstrap.py:178-181`). `service_identity` is not among them, and a repo-wide search
finds no read of it outside `service_authority.py` itself.

So an implementer told to "read it from the grant" reaches a dead end at the write
block, and the nearest populated field is `grant.authority.human_id` — the approving
human. **A design that names no reachable carrier does not merely omit a detail; it
steers the implementation into the exact misattribution §5.6 forbids.** That is why
the requirement below is a carrier contract and not a field lookup.

**What this story requires, stated as a contract rather than a field lookup:**

- **The canonical service-principal ID must reach the usage writer on a protected
  object.** Two placements satisfy §4.3's criterion, and the synthesis should pick
  one rather than leaving it to a developer: as a fifth field on
  `AuthorityReference`, resolved in `_deserialize_grant` from a new `authority_*`
  attribute written by `_grant_item`; or as a field on `ExecutionRecord`, stamped at
  dispatch. The first keeps ownership with the approval that conferred it, which is
  where PMM-02's registry resolves it; the second reaches every authority kind. **The
  requirement is that one of them exists before the write-side work starts** — not
  that this note chooses.
- **The value is PMM-02's to mint and resolve, this story's only to record.** Its
  current design head (PR #5437, `e2c7d099`) owns the opaque immutable
  `canonical_service_principal_id`, the `(org_id, alias_source, alias_id)` active-alias
  key, and **canonical resolution inside authentication** (§4.6 and §5.2.1 of that
  note). Resolving an alias in this story would be a second resolver that can
  disagree with the first.
- **`service_identity` is not a substitute.** It is a registered scope string
  (`^eventbridge:...`, `service_authority.py:51`), and ruling 1 forbids raw identity
  text from owning a preference. Even reachable, it would be evidence beside the
  canonical ID, never in place of it.
- **No fallback to the approving human, ever.** Absent canonical ID means the column
  is NULL. A fallback would look populated and correct while naming the wrong party
  — the defect with the worst detection profile available here, which is why AC-13
  asserts the negative case explicitly.

**The three dimensions, kept separate on the row:**

| Dimension | Answers | Source | Column |
|---|---|---|---|
| **Authenticated / billing principal** | Who is billed | `usage_logs.user_id` + `org_id` (attributed tenant, C-2) | Existing, unchanged |
| **Preference owner** | **Whose mapping selected this model** | PMM-02's canonical `canonical_service_principal_id` for service work; the canonical `users.id` for human-rooted work | **New** |
| **Approving human** | Who authorized the delegation — audit only | `grant.authority.human_id` (`service_authority.py:99`, reaching the request at `grants.py:113-116`, which already documents it as *"audit attribution only"*) | **Not a new column.** Already on the `AUTHORITY#` item; joinable by grant reference. Adding it here would invite grouping cost by it, which is the error above |

**Requirements on this story**, beyond §5.6a's carrier contract:

1. **Record the preference owner as its own nullable column**, with a companion kind
   discriminator (`human` / `service_principal`) so a canonical service-principal ID
   can never be read as a `users.id`. The two ID spaces are opaque and must not be
   compared.
2. **The per-persona cost view must state which dimension it grouped on**, and
   default to the preference owner for the question the story exists to answer. This
   is the same labelling obligation as §6.2, and the two interact: #4230 moves the
   *billing* principal, not the preference owner.

**Honest dependency, at PMM-02's current head.** The canonical service principal is
**designed but not built**: PR #5437 (`e2c7d099`) specifies the registry and its
resolution and is open and unmerged, so nothing in `modules/gateway/src/` mentions
`canonical_service_principal_id` today. #5419's *issue body* does not name it — which
is not evidence of a gap, for the reason §6.1 records: the design head is the artifact
a dependent story consumes.

So: for human-rooted work the preference owner is available immediately (the
canonical `users.id` §4.3's path already resolves); for service work the column is
**NULL until PMM-02 ships the canonical ID and §5.6a's carrier exists**. NULL means
not captured, as everywhere else here.

---

## 6. Decisions — now settled by operator ruling

**Every decision this note once raised is settled. Nothing in §6 requires an operator
answer.** Each is folded into the section that owns it, and the open alternatives have
been removed from those sections rather than left beside the ruling — so a developer
reading §5.4, §5.5 or §7.4 finds one instruction, not a choice.

| Was | Ruling | Now specified in |
|---|---|---|
| D-A — record a pricing revision? | **Yes, required**, and the complete tuple, not generation plus snapshot | §5.4 |
| D-B — whose harness identifier? | **PMM-06's** `harness_compatibility_revision`. This story consumes it; it must not invent one locally | §5.5 |
| D-C — #4230 sequencing? | **Proceed independently.** Label the attribution semantics, and add a reconciliation test for when #4230 changes principal attribution | §6.2 |
| D-D — which principal owns a preference? | **The canonical service principal** for service work, distinct from approving-human audit and from the billing principal | §5.6 |
| D-E — how is the retirement alert made once-per-event? | **A durable conditional claim taken *before* the publish**, reusing `stall.py`'s established mechanism, with the claim held in a retryable two-state form so a delivery failure cannot silence the alert permanently | §7.4 |

One decision **is** referred outward, and it is new in this pass rather than left
over: §5.6a's choice between the two admissible carriers for the canonical
service-principal ID. That belongs to the #5417 synthesis because either placement
changes a shared object, and §5.6a states the requirement — that one of them exist
before write-side work starts — without pre-empting the choice.

### 6.1 Withdrawn: the harness "producer gap" was a stale-source error

An earlier revision of this note recorded an open cross-story gap here, asserting that
R-2 directed this story to consume a field PMM-06 did not produce. **That finding is
withdrawn.** It was read from #5424's *issue body*; PMM-06's *design note* defines
`harness_compatibility_revision` and corrects its own AC-01 to assert it. §5.5 now
names the field and its constraints.

**The process lesson is worth keeping, because it will recur across this epic's eight
parallel stories.** A sibling's filed issue body and its current design head are
different artifacts and they diverge as soon as that sibling's architect run lands.
Reading the body and reporting a gap produced a confident, specific, wrong blocker
here. **For every cross-story claim, read the sibling's current design head** — and
say which artifact and revision was read, so a reviewer can tell a real gap from a
stale read. §10 now records this for each sibling claim in this note.

What remains is an ordinary dependency, not a gap: PMM-06 is open and unmerged, so
§5.5's column is NULL until it ships. That is §8's business.

### 6.2 #4230 — proceed independently, with labelled semantics (R-3)

**Ruling:** *"Proceed independently of #4230, but label attribution semantics and
add a reconciliation test when #4230 changes principal attribution."*

#4230 (*"feat(budget): count human-triggered agent spend against the triggering
human's budget"*, **verified OPEN** at the time of writing) will change *which
principal* a row is attributed to — not whether persona is recorded. The two are
orthogonal, which is why proceeding is safe. Two obligations follow:

**Label the semantics, in the API response and not only in prose.** The per-persona
view must state which principal dimension it grouped on, alongside the scope label
§5.3 already inherits from `orchestration/cost.py`. A consumer that cannot tell
whether a total is "spend by the agent identity" or "spend by the triggering human"
cannot safely compare two reports across the #4230 boundary. Today the row's
principal is `usage_logs.user_id`, with `org_id` holding the attributed billing
tenant (C-2); `context.attributed_user_id` already exists as the root-human
dimension (`modules/gateway/src/shared/schemas/auth.py:93`, the #4300 attribution
field) and is written to the chat-log settlement path as `root_human_id`
(`modules/gateway/src/proxy/mantle_service.py:825`), so the eventual shift has a
named target rather than an unknown one.

**#4230 moves the billing principal only — it does not touch the preference owner
(§5.6), and keeping that straight is what makes proceeding independently safe.**
#4230 changes *who is charged* for human-triggered agent spend. Which principal's
mapping *selected the model* is unaffected: a nightly service-account run still
resolved its model from the service principal's mapping no matter whose budget
absorbs the cost. This is precisely why the two dimensions must be separate columns
rather than one. If they were merged, #4230 would silently rewrite the meaning of
every per-persona-by-owner total, and the §6.2 guard below would be checking a
quantity that had already changed definition.

**Add the reconciliation test as a guard that fails when the premise changes.** Its
job is not to test #4230. It is to assert that the per-persona total and the
principal-keyed total agree on the *same* row set under today's attribution — so
that when #4230 lands and they diverge, CI says so instead of the dashboard
quietly changing meaning. Assert internal consistency between the two reads only:
per §2 C-4 there is **no invoice reconciliation anywhere in this platform**, and
this test must not be described as one.

---

## 7. Blocking findings

**B-1 — AC-07 was unimplementable as written. Both halves are now resolved by ruling, and AC-07 must be restated.** The findings below stand as evidence; the *direction* is settled.

### 7.1 The trigger: PMM-03 owns lifecycle state, this story consumes one signal (R-4)

**Ruling:** *"PMM-03 owns explicit lifecycle/retirement state; consume that one
signal."*

The finding stands: per C-6 nothing in the catalogue or pricing snapshot marks a
model retired, deprecated or sunset today, so AC-07's "mark a configured model
retired" has nothing to act on **at `ae598410`**. The resolution is that this is
PMM-03's to add, and **#5420's design head commits to it concretely** (PR #5434,
`25ece717`): its scope includes retirement handling; its model-row table carries a
`retired` field (§6.2 of that note, defined at §4.5); it names retirement *alerting*
as **out** of its own scope and this story's; and its §4.5 states the counterpart
contract verbatim — *"A retired model is flagged `retired` and is not selectable.
Existing mappings pointing at it stay visible, so their owners can be told — the
alerting itself is PMM-08's."*

Two further details from that head sharpen this story's consumption. **Retirement is a
catalogue-level state independent of probe outcome** — *"a model can be invocable and
retired at once"* — so this story must not infer retirement from evidence state. And
`retired` is one member of that note's explicit refusal-reason vocabulary, listed
beside `evidence_stale` and `not_invocable` (§4.5 of that note), which is exactly the
distinction the first obligation below turns on.

So the producer/consumer split is clean and agreed on both sides. Two obligations for
this story:

- **Consume exactly that one signal.** Do not add a lifecycle field, a second
  retirement list, or a heuristic (for example treating a model missing from the
  catalogue, or one whose invocability evidence has expired, as retired). Stale
  evidence and retirement are different states with different remedies, and PMM-03's
  head keeps them as separate refusal reasons for that reason — conflating them
  produces alerts nobody can act on.
- **This story is therefore sequenced after PMM-03's lifecycle field**, and its
  alert job cannot be verified before that field exists. §8 reflects this.

*Absence of a retirement signal is unavailability of the trigger, not a defect in
PMM-03* — #5420 is an open story, not a shipped one that omitted the field.

### 7.2 The channel: owner-visible pull, operator-only push (R-5)

**Ruling:** *"For this epic, deliver owner-visible warnings through Agent Models UI
and `adp models explain/list`, plus environment-operator asynchronous alerting
through the existing channel. Do not claim per-owner push delivery until a
separately authorized notification-address capability exists."*

This settles the channel escalation this note had raised, and it splits delivery by
audience. **Two delivery paths, neither overstated:**

**(a) The mapping owner learns by looking — three surfaces, no new channel.** The
warning is rendered where the person already goes to manage the mapping:

- **Agent Models UI (PMM-04, #5422).** #5422's AC-03 already requires that an
  unavailable or disallowed model is not selectable and "explains why it is
  disabled", with verified/broken/unknown rendering distinctly. A retired model
  behind a saved mapping is that same disabled-with-a-reason state.
- **`adp models list` and `adp models explain` (PMM-05, #5423).** #5423's AC-11
  requires explain to report the effective model and its source *from the server's
  answer*, and its `mappings list` to show "saved and effective values with the
  source". A retired saved model is a case where those two differ, which is exactly
  what the command exists to surface.
- **This story's explainer (§5.2).** The `own_selection_active` property is the
  mechanism: a saved mapping the retirement signal now rejects must render as **not
  in force, with a reason**. That is already what AC-10 demands, so no new contract
  is needed — only that retirement be one of the reasons.

The honest description of this path is **pull, not push**: the owner is told
*when they next look*, reliably and with a reason. It does not wake anyone up. This
story must not describe it as notifying the owner.

**(b) Environment operators get an asynchronous push through the one existing
channel.** `orchestration/notify.py` → SNS topic, driven by the EventBridge
orchestration tick (`modules/gateway/infra/modules/orchestration-tick/main.tf:257-282`,
`rate(5 minutes)`, running the gateway image, so it already has DB access and the
async stack). A periodic job reads mappings whose model carries PMM-03's retirement
flag and publishes an operator alert. This is within the channel's documented
audience and needs no extension.

**(c) What must not be claimed.** Per-owner push delivery is **out of scope until a
notification-address capability is separately authorized.** There is nowhere to
store a person's delivery address today, and `notify.py:50-56` defers per-tenant
delivery targets to a later story by design. **Extending `notify.py` with
per-principal targets is not authorized by this ruling and must not be built here.**

**Consequently AC-07 must be restated** — see §9. As written it requires reaching
"the principal who owns the mapping" asynchronously, which path (a) does not do and
path (b) does not target. Leaving its wording unchanged while shipping (a) + (b)
would be precisely the overstatement the issue warns against.

### 7.3 Why the channel finding still matters

The evidence below is retained because it is the reason the ruling splits delivery
the way it does, and because it bounds what path (b) actually guarantees. I
investigated this specifically because the issue says to reuse an existing surface
and escalate rather than invent one. The finding:

- The gateway has **exactly one** real asynchronous push-to-human channel:
  `modules/gateway/src/orchestration/notify.py` → SNS topic →
  email subscription, driven by the EventBridge-scheduled orchestration tick
  (`modules/gateway/infra/modules/orchestration-tick/main.tf:257-282`,
  `rate(5 minutes)`, running the gateway container image — so it already has DB
  access and the async stack). That tick is the right hook for a periodic job.
- **But its audience is wrong for this story, by its own design.**
  `notify.py:50-56`: *"There is one topic per environment, subscribed by that
  environment's operators … **Per-tenant delivery targets are a later story**;
  what this one guarantees is that no notification about org A is ever
  *attributed* to org B."* AC-07 requires reaching **the principal who owns the
  mapping** — a per-tenant, per-person target. That is the deferred later story.
- `notify.py:8-18` is a prior story's (#4211) written audit of this exact
  question, and it independently confirms: no SES send, no Slack webhook, no
  GitHub-issue posting, no `notifications` table in the gateway backend. I
  re-verified: no `notifications`/`alerts`/`inbox` table in any of the 53
  migrations; no `/notifications` route; no bell/inbox component.
- `var.alert_email_addresses` defaults to `[]`
  (`modules/gateway/infra/modules/orchestration-tick/variables.tf:191`) and I
  found **no tfvars anywhere in the repo that populate it**. Its own description
  adds that each address "must be CONFIRMED by its owner before AWS delivers to
  it", so even the operator channel reaches nobody until someone subscribes and
  confirms.

*The anti-pattern to avoid, with evidence.* `X-Budget-Warning` is the cautionary
precedent: budget warnings are computed
(`modules/gateway/src/budget/enforcement_service.py:1367-1372`) and emitted as a
response header (`modules/gateway/src/budget/headers.py:161-178`) — and **nothing
consumes it**. Grepping the whole repo returns six files: the header builder, the
middleware that attaches it, and four test modules. Zero hits in the frontend,
zero in the CLI, zero consumers anywhere. An alert that is
computed but not delivered is the failure AC-07 explicitly names, and this
codebase already contains one.

So path (b) delivers to a topic that, on the repo's own evidence, **has no
confirmed subscriber in any environment.** That is a deployment task, not a design
gap, but it must not be mistaken for delivery: the alert job is only as real as the
subscription. **PMM-09 (live deployment) should verify a confirmed subscriber
exists before path (b) is reported as working**, and the implementing PR should say
plainly that an unsubscribed topic delivers to nobody. Per §10 this is established
from the repository's tfvars only; absence of a tfvars entry is not proof that no
subscription exists in a live account.

*The escalation is answered and closed.* R-5 selected operator push plus an honest
owner-facing pull, with per-owner push deferred to a separately authorized
notification-address capability. The alternative of extending `notify.py` with
per-principal targets is **not authorized** and is not to be built in this story;
§7.2(c) states the prohibition and this section is the evidence for it.

### 7.4 The alert must be once per transition, not once per tick — claim before publish

Since §7.2(b)'s carrier is the EventBridge tick at `rate(5 minutes)`
(`modules/gateway/infra/modules/orchestration-tick/main.tf:257-282`), a job that
simply queries "mappings whose model is retired" and publishes would publish **288
alerts per day per affected mapping**, indefinitely, because a retired model stays
retired. That is worse than no alert: operators filter the topic, and the real signal
is buried. The deferred-per-owner problem in §7.2(c) would be joined by a
self-inflicted flood.

**`dedupe_key` alone does not solve this, and the codebase says so explicitly.**
`notify.py:133-136` is unambiguous: `dedupe_key` *"is carried so that a downstream
consumer can recognise a repeat; it is NOT what makes delivery once-only here — that
guarantee comes from the caller only notifying on a conditional UPDATE that matched a
row (see `stall.py`)"*. A developer reading `Notification.dedupe_key`
(`notify.py:149-152`) and concluding deduplication is handled would ship the flood.

**The established mechanism, which this story reuses rather than reinvents.**
`stall.py:92-98` states it: *"Notify-once is structural, not a flag. A notification
is emitted only when that conditional UPDATE matched exactly one row. Two overlapping
ticks therefore produce exactly one notification: the loser matches zero rows and
notifies nobody."* The write is routed through `apply_guarded_transition`
(`modules/gateway/src/orchestration/tick.py:237-261`), which guards with
`UPDATE ... WHERE id = :id AND state = :observed_state` and returns
`(rows_affected, allowed)` so a lost race is distinguishable from an authority
rejection. `stall.py:377` adds the other half: a node in a terminal state is excluded
from candidacy, so it *"is never re-examined, re-halted or re-notified on every
subsequent pass."*

**Applied to retirement alerting — the requirement on this story:**

1. **Alert on the transition, not on the state.** The trigger is *"this mapping's
   model became retired since we last looked"*, never *"this mapping's model is
   retired"*. The first is an event and occurs once; the second is a condition and is
   true forever.
2. **Claim the alert with a conditional write, and publish only if the claim
   matched a row.** The order is **claim → publish**, following `stall.py`'s
   discipline exactly (`_propose` at `stall.py:488-558`: `apply_guarded_transition`
   first, `rows != 1` returns early with *"notifying here is exactly the duplicate
   the once-only requirement forbids"*, and `_deliver` runs only after). Two
   overlapping ticks then produce one alert: the loser matches zero rows and
   publishes nothing. No lock, no dedupe table to expire.
   The carrier is a per-mapping claim marker keyed on the *retired model ID plus
   PMM-03's lifecycle revision*, making the guard
   `WHERE ... AND <marker> IS DISTINCT FROM :current`. **Whether that column belongs
   to PMM-02's schema or this story's migration is a schema-ownership question for
   the synthesis review**; the mechanism is the same either way.
3. **Re-arm on change, so a second real event is not swallowed.** If a mapping is
   repointed at a live model and that model is later retired too, the marker must
   permit a new alert. The keyed marker in requirement 2 achieves this: a different
   model or a new lifecycle revision is `DISTINCT FROM` the stored value and re-arms
   naturally, whereas a boolean `alerted` flag needs an explicit reset that some path
   will forget. This is the same reasoning `stall.py:92-98` gives for preferring a
   state transition to a "notified" column.
4. **Make the claim retryable, because claim-before-publish can otherwise suppress
   an alert permanently.** This is the genuine hazard in the correct ordering, and it
   must be designed for rather than traded away. `notify()` raises on any delivery
   failure and *"does not swallow"* (`notify.py:38-43`); `NotificationsDisabledError`
   is raised when no topic is configured (`notify.py:45-49`) so an unconfigured
   environment does not read as delivered. A claim that records *delivered* before
   the publish succeeds therefore converts a transient SNS error into a retirement
   nobody is ever told about — the `X-Budget-Warning` failure in §7.3.

   **So the marker records two states, not one: `claimed` and `delivered`.** The
   conditional write sets `claimed` (that is the concurrency fence); a successful
   publish advances it to `delivered`; a failed publish leaves it `claimed`, which a
   later tick treats as **eligible to retry** rather than as done. The claim is
   therefore a lease over the *attempt*, not a record of the outcome. This is the
   platform's own distinction — `work_claims.py:23-31` on `lease_expires_at`: it
   *"records when contact was expected and did not arrive. It is evidence of lost
   contact, never of an exit"* — and `ClaimState`'s docstring gives the reason for
   two members rather than a boolean (`orchestration/models.py:169-181`): the row is
   reused across generations so its history survives.

   A retry must be bounded and visible: count the failure into the job's report the
   way `stall.py:461-486` does (`notifications_failed`, which forces
   `report.success` to False and logs *"detection succeeded but nobody was told"*),
   and stop retrying a persistently failing claim rather than re-publishing forever.
   The residual failure mode is then a duplicate alert after a partial failure —
   recoverable and visible — instead of either a flood or a silence.
5. **Do not let the alert's durability depend on the tick's transaction.** The tick
   handler commits once, after every pass (`tick_handler.py:445-490`), and
   `detect_stalls` publishes *inside* that transaction — so an existing stall
   notification can be delivered and then rolled back by a later failure, leaving
   the claim unrecorded. That is tolerable for a stall (a re-detect re-notifies);
   it is not for a retirement claim whose whole job is to be durable. **Commit or
   flush the claim before publishing**, so a rollback elsewhere in the tick cannot
   discard the fence that a message already went out under.

**AC-07b is restated in §9 accordingly**: two consecutive ticks over an unchanged
retired mapping must publish exactly **one** message, and that assertion is the
criterion's point. A test that runs the job once cannot distinguish a correct
implementation from the flood.

**B-2 — PMM-01's design note is still not merged, and the epic's synthesis design does not exist yet. Both gate implementation, not this note.** #5418's deliverable is
`docs/design-notes/5417-per-invoker-persona-model-mapping.md`. **Re-verified at this
revision: it does not exist on `origin/main`** (`git ls-tree -r origin/main` finds no
such file), and PR #5436 which carries it is **open, not merged**.

At #5436's current head (**rev-5**, `b8045dbf`) the class-keyed default is in its
baseline: *"When no mapping row exists, the last rung selects the canonical system
default **for the target persona's compatibility class**"* (§0 of that note, citing
D4, D6 and #5433).

**The class-registry gap that earlier blocked §5.2 is closed at PMM-03's head.** An
earlier read of #5436 reported the class key as having no owner and no producer. Both
notes have since moved: #5434's rev-3 (`25ece717`) adds §2.4, the persona→class
registry assigned by U2, with stable unversioned class IDs and `compatibility_class`
on every persona row; and #5436's rev-5 records that correction explicitly, noting its
own earlier report of that gap was stale. So §5.2's explainer has a named producer for
the class it must report.

**What remains open at those heads reaches this story only as a constraint on
expectations, not a dependency.** #5436 rev-5 withdraws the claim that a second
compatibility class is live: `codex-sdk` is registered as a class ID with **no personas
mapped to it**, and all twelve personas resolve to `claude-agent-sdk` (#5434 §2.4). So
recorded data in this story's columns will carry one class for the foreseeable future —
which means **a query or test that assumes class diversity in `usage_logs` will pass
vacuously**, and neither note's remaining open item (the cross-epic question of whether
PMM-03's registry projects #5433's or is its input, #5436 §6.7a / #5434 §2.4b) changes
this note's schema or write path. §5.5 records a harness *revision* from the snapshot,
not a class, and persona/chain attribution (§4) is untouched.

The six operator decisions are locked in issue comments, which is what this note
builds on, and that is sufficient for *design*. Two consequences for implementation:

- **The #5417 synthesis design is a hard prerequisite for the developer wave**, per
  the epic's synthesis gate. This note is one input to it (see Status, above), not a
  substitute for it.
- **§5.2 additionally waits on PMM-03's persona→class registry shipping**, which is now
  designed (#5434 §2.4) but not built — the explainer cannot name the class-keyed
  default it is required to report until it exists in code.
- **§5.6a's carrier is a prerequisite for the service half of the preference-owner
  column**, and the choice between its two placements belongs to the synthesis (§6).

---

## 8. Sequencing and parallelism

Attribution can only record what PMM-07 resolves, so the **write side** is
genuinely gated. The rest is not.

| Work | Depends on | Can start |
|---|---|---|
| Migration + columns (§5.1, §5.4, §5.5, §5.6) | Nothing. Additive, nullable, no reader until the views exist. All columns — persona, chain, the five pricing-revision columns, harness revision, preference owner + kind — land in **one** migration. Resolve `down_revision` from the live head (C-5) | After the #5417 synthesis design merges |
| Write-side persona/chain population (§4) | PMM-07 resolution; the `EXEC#` persona read needs no new dependency | After #5425 |
| Write-side pricing revision (§5.4) | **Nothing beyond the migration.** The `PricingDecision` is already in scope at both writers — independent of PMM-07 | With the migration |
| Write-side harness revision (§5.5) | PMM-06 (#5424) shipping `harness_compatibility_revision`. Column is NULL until then | After #5424 |
| Write-side preference owner — human half (§5.6) | **Nothing** — the canonical `users.id` is already resolved on the request | With the migration |
| Write-side preference owner — service half (§5.6, §5.6a) | **Two things, and both are missing today.** (a) PMM-02's (#5419) canonical service-principal ID — designed at that PR's head `e2c7d099`, built nowhere. (b) **§5.6a's protected carrier**: no object on the request can hold the value, and which of the two admissible placements to add is the synthesis's call (§6). Column is NULL until both exist, and **never** falls back to the approving human | After #5419 **and** after the carrier lands |
| Explainer (§5.2) | PMM-02 storage (#5419) + PMM-07 resolver (#5425) — it must call their resolution, not copy it. Plus PMM-03's persona→class registry (designed at #5434's rev-3 head, not built), without which the class-keyed default cannot be named (B-2) | After all three |
| Per-persona cost view (§5.3) | The columns only. Reads `usage_logs` directly. Includes the #4230 reconciliation guard (§6.2) and the grouped-dimension label (§5.6, requirement 4) | After the migration |
| Retirement alert job (§7.1, §7.2, §7.4) | PMM-03's (#5420) lifecycle field — `retired` at #5434's §4.5 — **and its lifecycle revision** (§7.4's re-arm key). Channel is settled (R-5). The claim marker's schema ownership is a synthesis question (§7.4, requirement 2) | After #5420 |

The pricing-revision write (§5.4) is the one piece of new write-side work with **no
dependency on any sibling story**. It can proceed with the migration, and so can the
human-rooted half of §5.6. Everything else here waits on a sibling, and §5.6a's carrier
is the newest of those waits — worth stating plainly because it is the one dependency
an implementer would otherwise discover only after writing the column.

Rollback: a down-migration dropping the columns. Because they are nullable with no
reader outside the new views, dropping them cannot change any existing cost figure
— and that claim is checkable, because the existing readers
(`orchestration/cost.py`, `activity/cost_service.py`, `usage/service.py`,
`admin/service.py`, `budget/me_routes.py`) name their columns explicitly.

Deployment: `gateway-deploy.yml` fires on merge for gateway source;
`gateway-infra-apply.yml` is manual by design. Confirm against
`docs/adp-platform-deployment/deployment-manifest.md` at implementation time.

---

## 9. Acceptance criteria — required revisions

| AC | Status | Required change |
|---|---|---|
| AC-01 | Usable | Do not name a head number — resolve `down_revision` from the live head when the migration is written (head was `053` at `ae598410`, `054` at `c4809bb1`; C-5). Require the PR to state *measured* lock behaviour, not an assertion. Column set now also includes the five pricing-revision columns (§5.4), the harness revision (§5.5) and the preference owner + kind (§5.6) — **one** migration |
| AC-02 | **Good as written** | The strongest AC in the story. Keep it exactly |
| AC-03 | **Revise** | Must exercise a **non-`gate_decision`** authority kind, or it passes on a view that is empty in production (§4.4) |
| AC-04 | Usable | Add the §5.2 not-in-force property: satisfied by *calling* PMM-07's resolver, not by a careful copy |
| AC-05 | **Revise** | Premise inverted (C-2). Test that the new read aggregates on `usage_logs.org_id` (already the billing dimension); do not add a column |
| AC-06 | **Revise** | The direct-Bedrock row can no longer be produced (C-3). Substitute a late-settled-cost row; keep the completeness caveat |
| AC-07 | **Restate** — see the split below | No longer "blocked": R-4 gives the trigger (PMM-03's lifecycle field) and R-5 gives the channels. But the current wording claims asynchronous delivery to the mapping owner, which is explicitly **not** authorized (§7.2(c)). Split into AC-07a/b/c |
| AC-08 | **Good as written** | Correctly insists on the real FastAPI route; the absence of a target parameter is unobservable at service level |
| AC-09 | Usable | Name the mechanism: `CostStatus.UNKNOWN` with a reason, not `$0.00` (§5.3) |
| AC-10 | **Good as written** | This is the inert-config defect (#4511) and the `own_selection_active` precedent applies directly. Add retirement as one of the not-in-force reasons (§7.2(a)) |
| **AC-11** | **New** — required by R-1 | Pricing revision. Price a request, read the row: it carries the **complete tuple** the request's `PricingDecision` carried — `source_kind`, `generation_id`, `pointer_revision`, `snapshot_version`, `policy_version` (§5.4). Then settle the cost late via the tracker and re-read: the tuple is **unchanged** and the total is reproducible. Also assert (a) a row written with no decision (pricing failure) records NULL across all five, not the current generation, and (b) a `bundled_snapshot` row records a snapshot version with NULL generation and pointer, so the discriminator distinguishes it from "not captured" |
| **AC-12** | **New** — required by R-3 | #4230 reconciliation guard. The per-persona total and the billing-principal-keyed total agree over the same row set under today's attribution, and the response states which principal dimension it grouped on. Internal consistency only — **not** invoice reconciliation (§6.2) |
| **AC-13** | **New** — required by the second-pass ruling | Preference-owner attribution. Run service-account work under a `service_policy` grant approved by human A, where the service principal's own mapping selected the model. The row's **preference owner is the canonical service principal, not human A**, and human A appears only as grant-level audit. Then assert the negative: with no canonical service-principal ID available, the preference owner is **NULL** and has **not** fallen back to the approving human (§5.6). A test that only exercises human-rooted traffic cannot detect this defect, because all three dimensions coincide there |
| **AC-13b** | **New** — required by the third-pass ruling | The carrier is protected, not merely present (§5.6a). Assert that the value the usage writer records arrived on a **protected object** — the grant's `AuthorityReference` or `ExecutionRecord`, whichever the synthesis chose — and **not** from a request header, body field or any `webhook-events` attribute. Evidence: a request that supplies a conflicting service-principal ID by header records the protected value and ignores the header, in the shape `run_binding.py:20-24` already establishes for `x-agent-correlationid`. This is the write-side counterpart of §4.3's criterion, and without it a compliant-looking implementation can satisfy AC-13 while sourcing the value from the party being attributed |

**AC-07, restated in three parts** so each is separately verifiable and none
overstates delivery:

| ID | Action | Expected result |
|---|---|---|
| **AC-07a** | Mark a model retired via PMM-03's lifecycle field, with a saved mapping pointing at it; read the explainer | The mapping renders **not in force** with retirement as the stated reason. No new lifecycle field is defined in this story (§7.1) |
| **AC-07b** | Run the periodic job **twice** over an unchanged retired-model mapping, with the SNS topic configured | **Exactly one** alert is published across both ticks — the second matches no row and publishes nothing (§7.4). The message carries the tenant, persona and retired model. Running the job once cannot distinguish a correct implementation from one that alerts 288×/day, so two ticks is the criterion, not an embellishment |
| **AC-07b2** | Repoint the mapping at a live model, then retire that model too; run the job | A **new** alert is published — the marker re-armed on the changed model/lifecycle revision (§7.4, requirement 3). A boolean `alerted` flag would fail this, which is why the marker is keyed rather than boolean |
| **AC-07b3** | Run the job with no topic configured; then with a publish failure injected, then let a later tick run | Unconfigured raises `NotificationsDisabledError` and the job reports an error — an unconfigured environment must not read as delivered (§7.2(b)). On publish failure the marker is left at **`claimed`, not `delivered`**, the failure is counted into the job's report, and the **next tick retries** the same mapping (§7.4, requirement 4). Assert the retry actually happens: a claim-before-publish design whose claim is never revisited suppresses the alert permanently, which is the one failure this ordering can introduce |
| **AC-07c** | Confirm the delivery claim in the PR text | The PR states that the owner is informed by **pull** (UI / `adp models explain`/`list`) and that per-owner push is deferred pending an authorized notification-address capability. A PR claiming owner notification fails this criterion (§7.2(c)) |

AC-07b's evidence is a deterministic test plus the published-message assertion.
**Whether any environment has a confirmed subscriber is PMM-09's to verify** — the
repo populates no `alert_email_addresses` anywhere (§7.3), so a passing AC-07b
proves the job publishes, not that a human receives. Note also that `notify.py`'s
`dedupe_key` is **not** acceptable evidence for AC-07b: that field is for downstream
consumers by its own docstring (`notify.py:133-136`), and the guarantee must come
from the conditional write (§7.4).

Execution command in the issue is correct:
`cd modules/gateway && ruff check src/ tests/ && ruff format --check src/ tests/ && python3 -m pytest tests/ -q`.

The issue's "plausible wrong result that must fail" is well chosen but aimed at
the wrong hole. The sharper one, per §4.4: **a per-persona cost report that looks
complete because every test fixture ran through the engine path, while ordinary
webhook-dispatched agent traffic records NULL persona and is silently absent.**

---

## 10. Evidence boundary

Every claim above rests on source read at `ae598410` and re-verified at `c4809bb1`,
plus the sibling stories cited (#5417, #5418, #5420, #5422, #5423, #5424, #4230) read
at 2026-09-18. I made **no live AWS or deployed-environment checks**, so:

- Whether migration `031`-style `ADD COLUMN` timing is acceptable on the production
  `usage_logs` volume is **unverified** (§5.1 asks the PR to measure it).
- Whether any environment has subscribed an address to the orchestration alert
  topic is **unverified beyond the repo's tfvars**, which contain none. Absence of a
  tfvars entry is not proof that no subscription exists — it means the repo does not
  create one. §7.3 and AC-07b treat this as PMM-09's to verify.
- The pricing-revision claims in §5.4 are established from source only. That the
  `PricingDecision` is in scope at both writers, and that the tracker reuses rather
  than recomputes it, are read from code paths — **not from an executed request**.
  The implementing PR should confirm by asserting on a real priced row (AC-11).

**Sibling-story claims: which artifact and which revision.** "As-filed, not as-built"
is not precise enough to be safe: §6.1 records a wrong blocker produced by reading an
issue body while the answer sat in that story's design head. Each cross-story claim
here therefore names its artifact and its exact revision, re-verified at the heads
current on 2026-09-18:

| Claim here | Artifact read | Revision | Status |
|---|---|---|---|
| §5.5 — PMM-06 carries `harness_compatibility_revision` | `docs/design-notes/5424-chain-model-policy-snapshot.md` on PR #5442 | `f9f0ec68` | Design head. **Open, unmerged**; #5424's *issue body* still omits the field, corrected by that note's C4 |
| §5.6, §5.6a — PMM-02 owns and resolves `canonical_service_principal_id`; this story only records it | `docs/design-notes/5419-persona-model-preference-schema-and-api.md` on PR #5437 | `e2c7d099` | **Design head, not issue body.** Open, unmerged. The identifier is designed there (§4.6, §5.2.1) and **not built anywhere**, which is why §5.6a specifies a carrier rather than a lookup |
| §5.5, §7.1 — PMM-03 owns the lifecycle/retirement field and the persona→class registry | `docs/design-notes/5420-persona-and-model-catalogue.md` on PR #5434, rev-3 | `25ece717` | **Design head, not issue body.** Open, unmerged. `retired` is defined at §4.5 and `compatibility_class` on persona rows at §2.4 |
| §7.2(a) — owner-visible surfaces | #5422 AC-03, #5423 AC-11, issue bodies | as read 2026-09-18 | **Issue text only** — no design head read. Treat as the weakest cross-story claim here |
| B-2 — PMM-01's class-keyed default | `docs/design-notes/5417-per-invoker-persona-model-mapping.md` on PR #5436, rev-5 | `b8045dbf` | Design head. **Open, unmerged.** Rev-5 narrows its own closure claim to precedence and vocabulary and withdraws the second-live-class claim, leaving one cross-epic registry question open (its §6.7a) — so `codex-sdk` has no persona mapped to it and this story must not expect a second class in recorded data |

None of these has merged code, so each is a commitment rather than an implemented
contract. Four are read at their current design heads; one pair (#5422, #5423) still
rests on issue text whose design pass may move it as PMM-06's did. If any changes
during the #5417 synthesis, the dependent section here changes with it.

**Runtime claims added in this revision are read from source, at the repository state
above.** The `webhook-events` write grant (§4.3) is established from the Terraform
comment and the worker module's update expressions — **not** from an IAM policy
simulation against a live role, so "the grant is live today because the flag defaults
off" is a reading of `scaledjob-iam.tf:58,118`, not an observation of a
deployed role. That `correlation_id` is absent from the worker's written attributes is
established by enumerating those attributes and by the identifier not occurring in that
module; it is a complete read of one module, not a whole-system proof. §5.6a's claim
that nothing reads `service_identity` outside `service_authority.py` is a repo-wide
search result.

**Two claims in this revision are reasoning about a mechanism, not observations of
it.** §7.4's alert-volume figure (288/day) is arithmetic from the tick's
`rate(5 minutes)` schedule, not a measured rate. And §5.6's account of service-account
misattribution is read from the code path (`model_identity.py:71-72`,
`service_authority.py:98-100`) — I did not execute a `service_policy` request and
observe the recorded principal. AC-13 exists to confirm it against a real row.

Nothing in this note has been implemented, and no acceptance criterion has been
verified as passing; this is a design for work not yet begun. Its own standing is
**proposed pending the #5417 synthesis review** (see Status), so it is not an
authorization to implement, dispatch or deploy.
