# Design Note: Trusted-Root Model-Policy Snapshot and Per-Hop Chain Propagation (Issue #5424, PMM-06)

**Status:** **Proposed pending #5417 synthesis.** Not an approved design. The integrity model
(§3.3), the resolution point (§5.1), the **gateway-authoritative selection model** (§4.3) and the
bootstrap-before-use rule reflect the operator reviews on PR #5442 and the unified rulings on
#5417. **C1, C2 and C7 are settled and
need no further ruling** (§9). Remaining conditions must be reconciled against #5417's synthesis
before implementation starts, and per that gate **no developer is dispatched on this story until
the operator publishes the unified canonical design** — this note is an input to it, not an
authority over it (§8.2).
**Story:** #5424 (PMM-06), child of EPIC #5417.
**Binding inputs:** the six locked operator decisions D1–D6 on #5418 (comments dated
2026-09-18), reconciled decision-by-decision in §2.6; the #5417 architecture synthesis gate
(added 2026-09-18T13:43:58Z), mapped in §8.2; and the **#5417 unified architecture rulings**
(2026-09-18T16:42:26Z, rulings U1–U6), which are binding and supersede conflicting story-local
recommendations — reconciled in §2.7.
**Verified against:** default branch at `ae598410` (`origin/main`), 2026-09-18.
**Sibling notes re-read at their current unmerged branch heads** (§2.8), none of which is on
`origin/main`: PMM-01 `b8045dbf`, PMM-02 `e2c7d099`, PMM-03 `25ece717`, PMM-07 `1d799351`,
PMM-09 `86c7959a`. **All five moved since the previous revision** (`78787bd1`, `d9e83968`,
`6d5b2d10`, `67db0294`, `b28547c3`), and two of the moves change this note's findings: PMM-03's
rev-3 **closes both dependency gaps** this note previously recorded as blocking, and PMM-07's fourth
pass reaches §4.3's gateway-authoritative conclusion independently while correcting one claim here
(§2.8).
**Scope of this note:** design only. No runtime code, no schema, no deployment.

---

## 0. Executive summary

### 0.1 What this story builds

Agents dispatch other agents. The epic's rule is that the **entire chain obeys the model
choices of the principal who started it** — the authenticated human who typed the request,
or the registered service account whose schedule fired — and that each hop runs on the
choice recorded for *its own persona*. An architect→developer→reviewer chain therefore
uses three different models, all three read from one principal's settings. An intermediate
agent does not become the preference owner, and a mapping edited while the chain is running
must not take effect halfway through it.

That requires one decision taken at the root and then carried, unaltered and tamper-evident,
to every hop. This story builds that carried decision: the **chain model-policy snapshot**.

### 0.2 Verdict, stated up front

The story is **well-scoped and its intent is right**, and the implementation contract is
buildable. But it **cannot be implemented as written**, for two reasons that are independent
of each other:

1. **Three of its repository claims are stale or wrong**, and one of them is load-bearing.
   The story says the envelope's lineage block carries only three fields and that
   `chain_depth` / `parent_invocation_id` travel out of band. On the current default branch
   that is no longer true (§2.1). A design that adds snapshot fields to the wrong structure
   adds them to dead code.
2. **A prerequisite defect makes AC-03 unsatisfiable as specified.** The epic requires a
   service-account-rooted chain to use the service account's mappings. The envelope
   **cannot currently express "service-rooted"**: both the human and the service dispatch
   paths write `is_human_rooted: True` and populate `root_human_id` with a human ID
   (§3.2). There is no field for the principal *kind*. Until the snapshot carries its own
   principal-kind field, "use the service account's mappings, not any human's" has nothing
   to key on.

Neither is a reason to stop. Both are reasons to build the snapshot as a **self-describing,
independently verifiable object that carries its own principal identity** rather than as a
decoration on lineage fields that were designed for a different purpose. That is the central
design decision in this note (§3).

**Recommended verdict: Ready with specified conditions**, with the note itself **proposed
pending #5417 synthesis**. Eleven conditions, all in §9 — **C10 is new in this revision** and
carries the third review's gateway-only-selection ruling. **No condition now awaits an operator
ruling:** C1, C2 and C7 are settled — C1 by the first operator review on PR #5442, C2 and C7 by
#5417's unified rulings U4 and U2 (§2.7). C3/C3a/C10 are operator-directed and replace this note's
earlier resolution-point, token-lifetime and worker-selection proposals (§5.1, §3.3, §4.3). The
remainder, including C8's D1/D3 reconciliation (§2.6) and C9 from U5, are directions a developer can
execute — after the synthesis gate, which still governs dispatch (§8.2).

Two further findings do not block the story but change how it should be built.

A third finding bounds what PMM-06 can claim about the envelope: adding a key is inert for a legacy
worker's *parsing*, but **not** for the work-claim path, where the worker hashes the entire envelope
and the gateway compares that hash. This is what ultimately forces the sixth finding below — the
story adds no envelope key at all — and it retires the hazard rather than managing it (§7.1a).

A fifth finding — from the operator reviews on PR #5442 — moves where the snapshot is built and
then constrains how it can possibly travel. Resolution does **not** belong in the webhook Lambda
behind a DynamoDB projection of PMM-02's Postgres rows; that would make the preference data two
homes for one fact and put the enforcement-path copy in the least trusted component. It belongs in
the **gateway**, inside the fail-closed `/work/admit` call the Lambda already makes before it
publishes anything, where Postgres is an ordinary query and the protected record is already being
written (§5.1).

**A sixth finding closes the gap that opened up once resolution moved there, and it is the most
mechanically constrained result in this note.** A draft then had the gateway hand a reference back
on the admission receipt for the Lambda to embed. That is not implementable: admission runs *inside*
publication, after the envelope's tamper-evident digest has already been computed and stored, so a
post-admission envelope mutation invalidates that digest and **fails every dispatch on the
work-claim path** — not merely model selection (§4.1a). The same draft also gave each hop only a
digest, which is one-way and therefore cannot tell that hop anything about its own model
(§4.2a). Both are resolved by the same move: put **nothing** in the envelope, key the snapshot on
the run identifier already sealed inside it, and answer the **gateway bootstrap call each hop
already makes** — which already proves the workload is bound to the run before it answers — with a
short-lived signed **model decision** for that hop, covered by an `adpe1` assertion over the
decision bytes. This also retires the envelope-size and `Decimal`-serialization hazards for the
snapshot outright (§7.1a).

**A seventh finding, from the third operator review, decides *who* may make that decision, and it
is the reason the previous revision was still wrong.** Everything above moves the snapshot behind
the trusted boundary, but a draft then left the **selection** — look up `mappings[persona]`, fall
back to a class default, re-check the allowlist — as a numbered procedure the *worker* performs on
data it was handed. A worker that selects is a worker that can select wrongly and be believed:
`entrypoint.py:1576-1578` already substitutes a literal compiled into its own image when its input
lacks a model, and `keda.tf` sets no `ANTHROPIC_MODEL`, so that fallback is **live** today. The
correction is structural rather than procedural: the gateway resolves, admits and returns a signed
decision, the worker verifies and obeys, and **no resolver ships in the worker at all** (§4.3).
That superseded procedure is **removed** from this note, not annotated — a step-by-step recipe left
standing beside its replacement is a recipe someone implements.

A fourth finding makes the story **cheaper** than its own framing suggests, though not free. The
story's reuse table points at the shared-HMAC marker signer, which is the wrong mechanism and
disables itself under authority mode. That is easy to read as "D5's signing requirement needs a new
signing service." It does not: a production Ed25519 signer, a worker-side public-key verifier,
cross-language golden vectors, two-slot staged key rotation and a rotation runbook **already
exist** (§3.3 Mechanism A). PMM-06 should reuse them for per-hop assertions and introduce **no new
key material**. Two real constraints attach: their 30-second TTL is deliberate and must stay —
which is why the recommended model signs *per hop* rather than signing the snapshot itself — and
the signer carries no audience parameter and no chain-binding claim, so an audience constant and a
chain-binding claim must be added to that module and to both sides of its golden vectors. #5417's
ruling U4 now **requires** that extension rather than leaving it to be weighed (§2.7).

### 0.3 The one-paragraph design

At root dispatch, **the gateway** — inside the fail-closed `/work/admit` call the webhook Lambda
already makes before it publishes anything — builds a snapshot object containing the root
principal's **kind and ID**, the tenant, the persona→canonical-model mappings, the harness
compatibility class and revision, the policy/default/allowlist revisions, an issued-at and expiry,
an audience, and the correlation and root-invocation IDs. It resolves the mappings from PMM-02's
canonical **Postgres** rows, which it can read directly, and persists the snapshot and its digest
**on the protected execution row for that invocation** — the table whose docstring records that the
worker role has no write permission on it — rather than behind a new worker-held shared secret,
which D5 explicitly forbids.

**The queue message carries nothing about the snapshot at all** — not the mappings, not even a
reference. It cannot: the envelope is sealed with a tamper-evident digest *before* admission runs,
so anything added afterwards would break that digest and fail every dispatch (§4.1a). The snapshot
is keyed instead on the run identifier already inside the sealed envelope.

**The gateway is also the only thing that selects a model.** At the **bootstrap call each hop
already makes** — which verifies that this workload is the one bound to this run before answering —
the gateway looks up *that hop's own persona* in the snapshot it holds, applies the
no-cross-harness-family rule, re-checks live permission, and returns a **signed model decision**:
the chosen model, why it was chosen, and the snapshot digest it was resolved from. The assertion is
minted by the **Ed25519 control-envelope signer that already exists** and that workers already
verify with public keys only. **The worker performs no selection of its own** — no mapping lookup,
no default, and specifically no fallback to the model identifier baked into its container image. It
verifies the decision and invokes the named model, or it fails. Missing, altered, expired,
cross-tenant or cross-chain decisions are rejected with distinct reasons, never defaulted. The whole mechanism ships **report-only**: it computes and records
a decision but refuses nothing, so a defect cannot block platform-wide dispatch. The
enforcing flip is PMM-09's and is gated on #3186/#5195.

---

## 1. Scope

Capability reviewed: creation of an immutable model-policy snapshot at trusted root
dispatch, its propagation to every hop of a chain, per-hop selection by target persona,
and fail-closed rejection of missing, altered or cross-tenant snapshots.

In scope per the story: snapshot contents and creation; propagation to each hop; per-hop
selection; determinism across a running chain; rejection of adversarial snapshots; a persona
absent from the snapshot; the bounded last-known-good cache contract; the one-run override's
non-inheritance.

**Two scope terms the story uses are corrected here.** First, the story frames propagation as
*"carriage through the dispatch envelope"*. §4.1a establishes that this is not implementable: the
envelope is sealed with a tamper-evident digest before the only trusted resolution point runs. The
*capability* is unchanged — every hop still obtains an authentic model choice — but the mechanism is
authenticated delivery at gateway bootstrap, not envelope carriage (§4.2a).

Second, "per-hop selection by target persona" stays in scope as a *capability* but changes
*location*: the selection is performed **by the gateway** for each hop, and the worker receives a
signed decision rather than the inputs to one (§4.3). Read as "the worker selects," this scope line
would authorize the very thing the third operator review rejected.

Out of scope: wiring every dispatch path (PMM-07, #5425), cost recording (PMM-08, #5426),
default consolidation (PMM-09, #5427), and the credential-delegation model, which is
separate by design and which this note does not touch.

---

## 2. Claim revalidation against the current default branch

The story's request was explicit: *"Revalidate the issue's current repository claims against
the latest default branch; identify stale assumptions rather than copying them."* Each claim
below was checked by reading the cited file at `ae598410`.

### 2.1 🔴 STALE — the envelope lineage gap has closed, and the dataclass is dead code

**Story claim:** *"`chain_depth`, `invocation_id` and `parent_invocation_id` are **not** in
`to_dict()` and travel out of band via the webhook-events row and worker environment."* The
story lists this again under "Prerequisites and unresolved facts" marked **Verified**.

**What is actually true.** The claim is correct *about the dataclass* and wrong *about the
running code*, because the two have drifted apart.

`WebhookEnvelope.to_dict()` at `modules/agent-factory/webhook-ingress/lambda/common/envelope.py:102-106`
does serialize only three correlation fields:

```python
"correlation": {
    "correlation_id": self.correlation.correlation_id,
    "root_human_id": self.correlation.root_human_id,
    "is_human_rooted": self.correlation.is_human_rooted,
},
```

But **that dataclass is not what dispatch uses.** `spawn_persona._build_envelope()` at
`spawn_persona.py:524-616` constructs a **plain dict** from scratch, and its correlation block
(`spawn_persona.py:575-587`) carries five fields, including two the story says are absent:

```python
"correlation": {
    "correlation_id": correlation_ctx.get("correlation_id", ""),
    "root_human_id": correlation_ctx.get("root_human_id", ""),
    "is_human_rooted": correlation_ctx.get("is_human_rooted", True),
    "parent_invocation_id": correlation_ctx.get("parent_invocation_id"),
    "chain_depth": correlation_ctx.get("chain_depth", 0),
    "credential_chain_depth": correlation_ctx.get(
        "credential_chain_depth", correlation_ctx.get("chain_depth", 0)
    ),
},
```

The gateway's agent-to-agent path does the same independently
(`modules/gateway/src/agentauth/dispatch.py:274-282`), and the invocation ID is carried as
the top-level `message_id` (`spawn_persona.py:610`).

`WebhookEnvelope` has **no production consumer anywhere in the tree**. Grepping the whole
repo for the symbol returns only its own definition, its own unit tests
(`common/tests/test_envelope_token_source.py`), and a *comment* in
`lambda/gitlab/handler.py:113` describing the GitLab envelope as
"`WebhookEnvelope`-compatible". The GitLab handler builds a dict too.

**Why this matters, concretely.** The story's implementation contract says
*"snapshot fields on `WebhookEnvelope`"*. A developer who does exactly that ships a diff that
changes a dataclass no dispatch path instantiates, passes its unit tests, and **transmits no
snapshot at all**. This is precisely the inert-config failure class the epic's own sibling
#5418 cites as #4511: configuration that looks saved and changes nothing, with no visible
symptom.

**Direction — and note what it is *not*.** An earlier revision of this note concluded here that
"the snapshot must be added to the four real dict builders." **That direction is withdrawn**:
§4.1a establishes that the envelope is sealed with a tamper-evident digest *before* work
admission runs, so PMM-06 adds **no key to any builder**. What survives from this finding is the
builder *inventory*, which still matters for a different reason — each builder is a dispatch path
that must have a snapshot **associated with its invocation at admission**, and one of them is easy
to miss because it lives outside the webhook-ingress tree:

| Builder | Location (`ae598410`) | Why PMM-06 must know about it |
|---|---|---|
| Webhook `spawn_persona` | `spawn_persona.py:524-616` | The path `/work/admit` gates today (§5.1) |
| Gateway agent-to-agent | `dispatch.py:274-282` | Child hops; inherits the root's snapshot verbatim (§4.2b) |
| Orchestration engine | `dispatch_pass.py:628-667` — `channel: "orchestration"`, its own correlation block reading `genesis.is_human_rooted` at `:652`, published at `:1096` | **Does not call `spawn_persona`**, but it *does* admit: `_dispatch_one` calls `work_admission.admit(...)` at `dispatch_pass.py:953` with an `ENGINE_FLOW` owner. So this path has a real admission seam and can carry a snapshot association without an envelope change |
| GitLab handler | `gitlab/handler.py:229` | No admission seam and no `spawn_persona`; closed by explicit refusal (§5.1) |

PMM-06 should still state whether `WebhookEnvelope` is (a) updated in lockstep as documentation,
or (b) deleted as dead code. Recommendation: **(a) for this story** — do not expand PMM-06's blast
radius by deleting a public-looking symbol — and file the deletion as a separate cleanup. The
contract test this finding originally motivated changes shape accordingly: instead of asserting
that the dataclass and the dict builders carry matching snapshot fields, assert that **no builder's
key set changed** (§7.1a, C4).

### 2.2 🔴 WRONG — the story's own reuse table proposes a mechanism D5 forbids

**Story claim:** under "Starting point and reuse", for *Marker signature*:
*"Reuse the tri-state discipline"* of `marker_verify.py` / `marker_signing.py`.

The tri-state discipline itself is real and worth reusing as a *pattern*
(`marker_verify.verify_marker` at `marker_verify.py:182-212` returns `True`/`False`/`None`,
and refuses known placeholder keys at `marker_verify.py:45-61, 97-104`). That part is sound.

But the **signing mechanism** it points at cannot secure this snapshot, and the code says so
itself. `marker_signing._load_signing_key()` at
`modules/agent-factory/agent-worker-image/lib/marker_signing.py:46-52` disables itself
entirely under the newer authority mode:

```python
if os.environ.get("ADP_AGENT_AUTHORITY_ENABLED") == "true":
    # A shared HMAC key can impersonate another run's human lineage.
    # Protected dispatch uses gateway authority; shared-marker compatibility
    # needs a mediated signer before migration (see #5195).
    _signing_key = None
    _key_loaded = False
    return None
```

D5 locks the same conclusion independently: *"A worker-held shared HMAC is explicitly
rejected because a worker possessing it could impersonate another run's human/service-account
lineage."*

**Direction.** Reuse the tri-state *result discipline* and the placeholder-key refusal.
Do **not** reuse the shared HMAC as the snapshot's integrity root. §3.3 specifies what does —
and note that the story points at the *wrong* signer, not at a missing one: an approved
**asymmetric** signer with worker-side public-key verification already exists
(`modules/gateway/src/agentauth/envelope.py`, `agent/src/control-envelope.ts`). The reuse table
should cite that instead of `marker_signing.py`.

### 2.3 🟠 INCOMPLETE — D6 is binding and the story predates it

The story body lists five prerequisites (D5 and four others) and never mentions harness
compatibility. D6 was locked on #5418 at 2026-09-18T13:20:09Z and states:

> *"The immutable signed chain snapshot must include the harness identifier/version (or
> compatibility-contract revision) together with the resolved model and policy revisions."*

D6 also narrows the selectable set: all direct persona execution runs on the pinned Claude
Agent SDK harness, so only compatible Anthropic Claude models are selectable, and
the `codex` persona is **not** an exception — its outer agent is still the Claude worker,
with Codex invoked as a bounded delegated tool.

**Direction.** The snapshot schema in §3.1 carries a harness compatibility revision, and
AC-01's field list must be extended to assert it. This is a real addition to the story's
stated contract, not a reinterpretation of it.

**D6 is already outrun by #5433, which was filed four minutes after it locked.** #5433
(*"EPIC: Native GPT agent personas powered exclusively by the Codex harness"*, created
2026-09-18T13:24:59Z versus D6's 13:20:09Z) defines `gpt-*` personas that run **exclusively** on
the Codex harness, states that *"a `gpt-*` persona has no Claude support or Claude fallback"*, and
requires that *"the no-mapping default for a `gpt-*` persona must itself be a validated
Codex/GPT-compatible default. The Anthropic Claude system default cannot be used for this persona
family."* It then assigns the duty directly: *"The persona/model mapping design in #5417 must
therefore support a compatibility-class or harness-specific default before these personas enter
enforcement."*

This does not invalidate D6 for today's personas — `codex` remains a Claude-worker persona with
Codex as a delegated tool, as D6 says. But it **does** change this story's schema: §3.1's single
global `system_default_model_id` cannot serve a `gpt-*` persona, and a snapshot that carries one
global default would resolve an absent `gpt-*` mapping to a Claude model that the persona
explicitly cannot run. That is AC-07's "costs what nobody chose" failure with a harness-level
incompatibility on top.

**Direction.** Make `system_default_model_id` **keyed by harness/compatibility class** rather
than a single scalar, so an absent mapping resolves to the default *for that persona's harness*
and a persona whose harness has no default fails with `harness_incompatible` instead of silently
crossing families. This is cheap to design now and expensive to retrofit after PMM-07 consumes
the schema. Carried as **C7** in §9, because it changes a schema field the operator is being asked
to approve. Note this is a forward-compatibility requirement, not a claim that `gpt-*` personas
exist on `origin/main` today — #5433 is an OPEN epic, not merged behaviour.

### 2.4 ✅ VERIFIED — claims that hold exactly as written

| Story claim | Verified location | Note |
|---|---|---|
| `spawn_persona.py` is the single enforcement point | `spawn_persona.py:62`; called from `github/handler.py:1767` as "the SINGLE enforcement point — no drift across trigger adapters" | **Superseded — see §2.4a.** The in-code comment says this, but it is not true of the publish paths as a whole |
| `_advance_chain_depth` is the one place depth advances | `spawn_persona.py:304`, called once at `:195`; docstring calls itself "THE single increment point" (#4268) | Exact |
| `_build_envelope` location | `spawn_persona.py:524` | Exact |
| `Correlation` dataclass has three fields | `envelope.py:26-28` | Exact — but see §2.1, it is dead code |
| `model_requested`/`model_resolved` on the dataclass | `envelope.py:68-69` | Exact |
| `determine_correlation` / `_resolve_pointer_provenance` | `handler.py:960` / `handler.py:826` | Exact |
| `/model` leniency is shipped | `handler.py:1786-1789` logs *"proceeding with default model (lenient)"* | Exact. **Now reversed by D2** |
| `agent_trigger` rejection vocabulary | `agent_trigger.py:27-36` documents `missing_lineage`, `unknown_chain`, `cross_tenant`, `unverified_provenance`, `invalid_chain_depth`, plus `cross_tenant_target` and `guard_rejected` | Exact, and the right vocabulary to extend |
| `is_human_rooted` defaults False in agent-to-agent | `spawn_persona.py:59`, `:300`, `:688-689` | Exact |
| Worker reads `model_resolved`, falls back to `ANTHROPIC_MODEL` | `entrypoint.py:1576-1578`; hard-coded fallback `global.anthropic.claude-opus-5` | Exact line numbers. That fallback is the #4673 defect and the model D4 replaces |
| `ADP_MODEL_RESOLVED` exported with no consumer | `entrypoint.py:1686-1687` | Exact — #2293 |
| Correlation store is agent-writable, so not authoritative | `correlation_store.py` module docstring | Exact, and load-bearing for §3.3 |
| `DispatchRequest` forbids extra fields, has no model field | `dispatch.py:88-93`, `model_config = ConfigDict(extra="forbid")` | Exact. A child genuinely cannot request a model today |
| `bedrock_principal.py` service-rooted treatment | `bedrock_principal.py:63-65`: *"An explicitly service-rooted job has no person's rule to inherit"* | Exact, and see §3.2 — this is the **only** place that reads `is_human_rooted is False` as authoritative |
| 10-second budget forbids a gateway call on the webhook path | #2279 ruling 4; `gateway_client.py` uses `timeout=10` at `:138, :268, :376, :611` — a single call can consume the entire budget | Confirmed, and quantified in §5 |

### 2.4a 🔴 CORRECTED — `spawn_persona` is not a universal entry point

The row above is the one claim in §2.4 that does not survive. PMM-07's review (#5425, PR #5438)
establishes that *"`spawn_persona` is not the single enforcement point, and all five paths are
now in scope."* Verified independently here:

| Publish path | Entry | Reaches `spawn_persona`? |
|---|---|---|
| GitHub webhook | `github/handler.py:1834` | Yes — the only caller passing `model_requested`/`model_resolved` (`:1849-1850`) |
| Agent-to-agent | `github/agent_trigger.py:374` | Yes, with no model arguments |
| EventBridge | `eventbridge/handler.py:237` | Yes, with no model arguments |
| **GitLab** | `gitlab/handler.py:229` | **No** — builds its own envelope, calls `publish_envelope` directly, hard-codes both model fields `None` (`:172-173`) |
| **Orchestration engine** | `orchestration/dispatch_pass.py:1096` | **No** — calls `sqs.send_message` directly from the gateway container |

The orchestration bypass is a **ruling, not an oversight**: `dispatch_pass.py:88-107` records
that `spawn_persona` is deliberately not called because its correlation-pointer store is
agent-writable (#4304), and that `publish_envelope` is not even importable from the gateway
image. So the in-code "SINGLE enforcement point" comment describes the *trigger adapters*, not
every producer.

**Consequence for this note.** Wherever this note leaned on that claim, the load-bearing
guarantee is not "one function" but **"one authority, reached from every producer"** — which is
what §5.1's gateway-side resolution and §3.3's bootstrap-before-use rule actually provide.
§4.1 is corrected accordingly, and the §5.1 GitLab gap is a direct consequence: the two paths
that bypass `spawn_persona` are exactly the two that need naming rather than assuming.

### 2.5 🟠 STALE — the design note this story is built on is not merged

The story depends on #5418 (PMM-01) *"for root semantics and the D5 trust-root ruling"*.
The six decisions are **locked in #5418's comments**, so the substance is available. But the
design note that is #5418's deliverable — proposed path
`docs/design-notes/5417-per-invoker-persona-model-mapping.md` — **does not exist on
`origin/main`** (`git ls-tree origin/main docs/design-notes/` returns no match for it). It
*does* exist on the unmerged branch `agent/issue-5418` (head `b8045dbf` at this revision, rev-5),
which is where §2.8 reconciles it. #5418 is OPEN.

**Consequence.** This note cannot cite section numbers of a *merged* document. It therefore
cites the **locked operator comments directly** by decision ID and timestamp for anything
binding, and treats PMM-01's note only as a sibling head to be reconciled (§2.8) — never as an
authority. §9 carries a condition that the two notes be reconciled before PMM-07 consumes either.

---

### 2.6 Reconciliation against the six locked decisions

D1–D6 are binding inputs, not background. This table states where each one lands in this design
and flags the two that change it. D2, D4, D5 and D6 were already carried through the note; **D1
and D3 were named but not engaged**, and both turn out to have consequences for what the snapshot
may freeze.

| Decision (locked 2026-09-18) | Where it lands | Status |
|---|---|---|
| **D1** — personal mapping selects, organization policy constrains; they are separate axes; resolution uses the **active tenant/workspace**; an org rule never silently substitutes | §2.6a (which tenant is authoritative), §2.6b (what may be frozen) | **Was under-engaged — now addressed** |
| **D2** — invalid one-run requests fail closed; a valid override affects the directly invoked hop only and never descends | §4.4 | Carried |
| **D3** — allowlists become a **real admission gate**; the selectable set is an intersection including **freshly proven** invocability; empty/stale/contradictory policy fails actionably | §2.6b, §3.1 (`allowlist_policy_revision`), §6 | **Was under-engaged — now addressed** |
| **D4** — canonical default is `us.anthropic.claude-sonnet-4-6`; failure is loud, never substitution | §3.1, §8.1 | Carried |
| **D5** — snapshot created and signed by the trusted gateway/authority; workers never hold signing secrets; enforcement gated on #3186/#5195 | §3.3, §5.1, §7.2 | Carried (with the signer-contract caveat, C2) |
| **D6** — harness constrains selection; the snapshot must carry the harness identifier / compatibility revision; each hop checked against its own harness | §2.3, §3.1, §4.3 gateway steps 1–2 | Carried (with C7's harness-keyed default) |

### 2.6a D1: which tenant the snapshot is resolved in

D1 does not only separate the two axes. It also rules that *"Resolution uses the active
tenant/workspace, so a multi-organization human may hold a different persona mapping in each
tenant."* This note previously carried `tenant_id` as a snapshot field and compared it per hop
(§3.1, §6) without ever saying **which** tenant is authoritative at creation. That is a real gap,
because on the two root paths the answer is produced by different machinery:

- **The webhook path has no notion of a user-selected workspace.** The tenant is derived from the
  GitHub **installation** that delivered the event
  (`identity_resolver.py:390-391`, `:434-435`, resolving `pg_install["tenant_id"]`), and
  `/work/admit` likewise reads it from the protected dispatch pointer rather than from anything
  the caller says (`work_admission.py:110`, `TENANT#{org_id}` at `:113`, and the scope assertion
  `grant.tenant_id != org_id` at `:123`).
- **The UI/CLI path does have a selected workspace**, and it is mutable: `select_workspace`
  re-reads memberships under `with_for_update()` and callers *"must refresh tokens after this
  switch"* (`modules/gateway/src/auth/workspaces.py:196-221`;
  `admin/connections/routes.py:305-315`).

**Direction.** The snapshot's `tenant_id` is the tenant of the **protected dispatch record**, not
a workspace preference and not anything a caller supplies — which is what the trusted path
already resolves for itself and the only value a hop can verify against. Two rules follow, and
both should be stated in the implementation rather than discovered:

1. **Write the mapping lookup as `(tenant_id, principal_kind, principal_id, persona)`, with
   `tenant_id` from the dispatch record.** A lookup that keys on principal alone would return one
   organization's choices for a chain running in another — a cross-tenant preference read, which
   §6 otherwise treats as an attack.
2. **An absent mapping in *this* tenant is an absent mapping.** For a multi-organization human it
   must resolve to the system default with `source = system-default`; it must **not** fall back to
   the same person's mapping in another tenant. That fallback would be indistinguishable from
   correct behaviour in a single-tenant test and wrong in exactly the case D1 exists to address.

This is a documentation-level direction, not a new operator decision: D1 already rules it. What
the note owed was saying which of the two tenant sources satisfies it.

### 2.6b D3 vs the immutable snapshot: freeze the choice, never the permission

D3 and the epic's snapshot requirement pull in opposite directions, and this note did not
separate them. D3 makes the effective selectable set an intersection that includes *"models
authorized and freshly proven invocable through the resolved AWS destination/account/region"*,
and requires that *"empty, stale or contradictory effective policy fails actionably."* The
snapshot's whole purpose (AC-04) is the opposite instinct: capture policy once at the root so a
running chain is reproducible and a mid-flight edit cannot take effect halfway through it.

Resolved naively — "the snapshot is immutable, so every hop uses what it froze" — the snapshot
becomes a **permission slip that outlives a revocation**. A model withdrawn from a tenant's
allowlist, or an account that loses access to it, would keep being invoked for the remaining life
of every in-flight chain, under a digest-bound object that looks authoritative at every hop. On a
long chain that is not a narrow window.

**Direction — the split D1 already implies.** D1 says selection and authorization are different
axes; therefore they have different freshness rules, and the snapshot may only freeze one of them:

| Concern | Frozen in the snapshot? | Why |
|---|---|---|
| *Which* model this principal chose for this persona (`mappings`, `system_default_model_id`) | **Yes — verbatim, for the life of the chain** | This is AC-04. A preference edit mid-chain must not take effect halfway through |
| *Whether* that model is still permitted — tenant/org allowlist, persona/harness compatibility, destination invocability, budget, rate limits | **No — re-evaluated at each hop** | D3 requires freshly proven admission; D1 lists these as admission gates rather than precedence levels |
| The policy **revision identifiers** the root resolved against (`policy_revision`, `allowlist_policy_revision`, `harness_compatibility_revision`) | **Yes — as recorded evidence** | So an auditor can explain which policy the root saw. Recording a revision is not the same as honouring it instead of a live check |

So `allowlist_policy_revision` is in §3.1 for **attribution and drift detection**, not as a
cached admission decision. A hop that finds the live allowlist revision has moved does not
thereby refuse — it refuses only if the *live* check refuses, and the revision delta is what makes
that refusal explicable after the fact.

**The consequence to carry into PMM-07.** This means a chain can legitimately fail at hop three
having succeeded at hops one and two, with an unchanged snapshot — a revocation landed in
between. That must surface as a distinct, explicable refusal (`model_unavailable` /
`harness_incompatible` per §6) and must never be reported as snapshot corruption
(`snapshot_altered`), which would send an operator looking for tampering that did not happen.
PMM-07 owns the enforcement; PMM-06 owes it a snapshot that does not pretend to have already
answered the question.

### 2.7 Reconciliation against the #5417 unified rulings (2026-09-18T16:42:26Z)

These rulings are binding and supersede conflicting story-local recommendations. Three of them
**settle conditions this note previously held open**; two **change specifics** the note had left
to the developer. Each is carried at the section named.

| Ruling | Effect on PMM-06 |
|---|---|
| **U4 — Snapshot authority** | **Settles C2 as approved, exactly as §3.3 recommends**: gateway work-admission resolves Postgres authority and persists snapshot + digest in worker-unwritable storage; mandatory bootstrap verifies workload/run/root and returns a fresh, **audience- and chain-bound** `adpe1` assertion; 30-second maximum TTL retained and reissued per hop; workers get no signer secret and **no long-lived offline-verifiable policy token**. The audience-and-chain-binding requirement is the contract widening §3.3 flagged — it is now **ruled, not proposed**. U4 also re-states C1: `service_policy` is owned by the canonical service principal, the approving human is audit attribution only |
| **U2 — Compatibility ownership** | **Settles C7 as approved**, and makes it more precise than this note had it. Compatibility **class IDs are stable and unversioned** (`claude-agent-sdk`, `codex-sdk`) and **harness/contract revision is a separate versioned field** that is part of snapshot keys. So §3.1 splits into a stable `harness_compatibility_class` plus a versioned `harness_compatibility_revision`, and `system_default_model_id` is **keyed by class** (§8.1's drift finding is the reason this matters). Also: `us.anthropic.claude-sonnet-4-6` is a Claude-class **candidate**, not a proven default until PMM-09 records a bounded invocation — so §3.1's "canonical default" wording is corrected, and **no cross-class fallback** is permitted |
| **U1 — Canonical service principal** | Constrains how C1 is implemented. The snapshot's `principal_id` for a service principal must be PMM-02's **opaque immutable `canonical_service_principal_id`**. Aliases are tenant-scoped and source-qualified `(org_id, alias_source, alias_id)` and **never globally keyed**; raw `service_accounts.id`, `agent_name`, `client_id`, ARN or caller-supplied text **never owns a preference**, so none of them may appear as `principal_id`. Cognito `Organization.cognito_client_ids` is an approved-client list, not an identity. **This revision applies U1 to a place the note itself violated it:** §3.2's `service_policy` row previously derived the owner from the envelope's `actor.user_id`, which is exactly caller-supplied text — it is copied verbatim out of the inbound event at `service_authority.py:112`. It now derives from the authority row's verified `service_identity` through PMM-02's alias registry, and the old row is removed rather than kept beside the correction |
| **U5 — ARC/Actions root identity** | New requirement this note did not carry. Root ownership follows the **authenticated initiator**, not the bot credential executing the job. A human-initiated `issues:labeled`, issue-comment or `workflow_dispatch` event preserves the resolved canonical human root; a **scheduled or service-to-service run with no authenticated human initiator resolves a tenant-bound registered canonical service principal and fails closed if unregistered.** This is a snapshot-construction rule: the execution identity is audit attribution only and must never become the preference owner. §3.2's `principal_kind` derivation must therefore fail closed on an unregistered service initiator rather than attributing to the workflow's App identity (new condition **C9**) |
| **U3 — Probe safety** | Bounds the gateway's step 3 in §4.3. Live admission re-checks allowlist and invocability, and **must not invoke a model to do it**: probing ships disabled with a zero spend budget, and pricing/listing/agreement status never counts as invocability proof. So a hop's live check reads PMM-03's recorded evidence; it never probes |
| **U6 — One API contract** | PMM-06 adds no user-facing surface, so this is consumption only: it reads PMM-02's rows behind the canonical `/me/persona-models` handlers and must not introduce a second backend router |

**Net effect on §9:** C1, C2 and C7 are settled; C8's D1/D3 reconciliation stands; C3 narrows to
the GitLab refusal now ruled in §5.1; and **C9 is new**, from U5.

### 2.8 Reconciliation against the sibling notes at their **current** heads

The second-pass review requires reconciling live sibling heads rather than stale versions. Every
row below was read at the head named — not at the version quoted in an earlier revision of this
note, and not on `origin/main`, where **none of these notes exists**.

**All five heads moved again between the previous revision of this note and this one**, so the
table below is re-fetched rather than carried forward. The previous revision's SHAs are kept in the
right-hand column because two of the moves *change this note's findings* — and because a reader
comparing revisions should be able to see that the `6d5b2d10` claims were not wrong when written.

| Story | Branch head read (this revision) | Previous revision read | Its own status line at the new head |
|---|---|---|---|
| PMM-01 #5418 | `agent/issue-5418` @ `b8045dbf`, `docs/design-notes/5417-per-invoker-persona-model-mapping.md` | `78787bd1` | rev-5 — *"withdraw the second-live-class claim, reconcile PMM-03 head"* |
| PMM-02 #5419 | `agent/issue-5419` @ `e2c7d099` | `d9e83968` | *"proposed"*; not architecture-approved |
| PMM-03 #5420 | `agent/issue-5420` @ `25ece717`, `docs/design-notes/5420-persona-and-model-catalogue.md` | `6d5b2d10` | *"**proposed** story design — not binding"*; **rev-3, which applies U1–U6** (its R1–R6) |
| PMM-07 #5425 | `agent/issue-5425` @ `1d799351`, `docs/design-notes/5425-persona-model-resolver-wiring.md` | `67db0294` | *"**PROPOSED — pending #5417 synthesis**"*; fourth pass, *"ARC needs gateway-authoritative resolution"* |
| PMM-09 #5427 | `agent/issue-5427` @ `86c7959a` | `b28547c3` | *"PROPOSED, subordinate to the #5417 synthesis"* |

**🟢 The two findings this note carried as 🔴 dependency gaps are closed at PMM-03's new head, and
this note withdraws them rather than restating them.** At `6d5b2d10` PMM-03 defined no
compatibility class, contradicted U2 on the Sonnet default, and contradicted U3 on probe posture.
Its rev-3 at `25ece717` fixes all three, in U2's own vocabulary:

- **Class vocabulary now exists.** Every persona row carries `compatibility_class`; class IDs are
  *"stable and unversioned"* — `claude-agent-sdk`, with `codex-sdk` *"registered as a class ID with
  no personas mapped to it"* — and `harness_contract_revision` is the separate versioned field
  (`5420…:100`, `:102-103`, `:336`). It also states **"No cross-class fallback, ever"** (`:104`) and
  requires **both** lookup directions, naming PMM-06 as the consumer: *"PMM-06 binds the class and
  revision into the signed snapshot … this story emits them"* (`:173`). §3.1 and C7 now rest on a
  supplied contract rather than an assigned obligation.
- **The Sonnet default is a candidate at PMM-03 too.** *"R2 states `us.anthropic.claude-sonnet-4-6`
  is a Claude-class **candidate**, not an active proven default"*, and the catalogue *"records **no**
  default at all"* (`:106`, `:156`). The disagreement §3.1 recorded is gone.
- **Probe posture now matches U3.** *"R3 ships probing **disabled with a zero spend budget**"*, so a
  correct PMM-03 ships a catalogue in which *"**no model is yet certified invocable**"* — named as
  *"the intended fail-closed state, not a defect"* (`5420…:19`). PMM-06's requirement is unchanged
  (it never probes, it reads recorded evidence), but the note must no longer assert that PMM-03's
  head disagrees, because it no longer does.

  **One consequence PMM-06 must state rather than inherit quietly:** if no model is certified
  invocable until PMM-09 runs, then the gateway's live admission step (§4.3, gateway step 3) has
  **no passing evidence to read** at PMM-06's own completion. That is consistent with report-only
  rollout (§7.2) and with PMM-06's completion boundary, but it means a naive enforcing build would
  refuse *every* hop — correctly, and uselessly. It is a second independent reason the enforcing
  flip belongs to PMM-09, and it is recorded in §8 as a dependency state.

**🟢 PMM-07's fourth pass independently reaches this revision's central correction**, on a
different path, which is the strongest available evidence that the gateway-authoritative model is
right rather than merely responsive to a review. Its Q5′ — open across three of its revisions, where
it had *recommended* local pre-validation — is now closed the other way: *"**Synchronously — a
gateway-authoritative decision before the harness step**, or route the job's model traffic through
the gateway"* (`5425…:1557`). Its stated reason is the same structural one as §4.3's: the nine ARC
workflows set `CLAUDE_CODE_USE_BEDROCK: "1"` with no base-URL override and hold `bedrock:InvokeModel`
on `Resource = "*"`, so *"a refuse-only local check would therefore have enforced nothing while
reporting compliance"*. That is §4.3's argument about `entrypoint.py:1578` transposed to the ARC
path: a component that holds the invocation capability cannot be trusted to police its own
selection. Two notes, two paths, one conclusion — and PMM-07 records its own prior recommendation
as **overturned**, not retained beside the new one.

**🔴 PMM-07's head also supplies a correction to this note, which §4.3 now carries.** Its P4′ records
that bootstrap returns *"an **`adpr1`** HMAC-SHA256 run credential … **not** the `adpe1` Ed25519
assertion U4 describes"* (`5425…:257`). Verified directly at `ae598410`: `issue_bound_credential`
calls `mint_credential` (`bootstrap.py:373`, `:13`) → `adpr1`, HMAC-SHA256
(`run_credential.py:61`, `:215-216`), and neither `routes.py` nor `bootstrap.py` calls
`sign_envelope` on any path. So the signed-decision channel §4.3 specifies is **new work on two
axes** — a new field *and* a signer the response has never reached. §4.3 states this explicitly now
rather than implying the plumbing exists. PMM-07 also **withdraws two quotations it had attributed
to PMM-06** as *"fabricated citations, which is worse than stale ones"* (`5425…:103`); neither
phrase is in this note, and the withdrawal is correct.

**What this changes in this note.**

- **PMM-02's principal contract is further along than §3.1 assumed, and in the same direction.**
  Its head now owns the registry outright — U1 *"assigns this story the canonical
  `canonical_service_principal_id`, the alias registry, canonical resolution in authentication"*
  (`5419…:63`) — and specifies it as an **identity store**, not a settings column:
  `canonical_service_principal_id` is *"the **opaque, immutable** ADP-minted ID. What preference
  rows store"*, explicitly not derived from an `agent_name` or ARN because *"deriving it … would
  make it change when the alias does, defeating immutability"* (`:545`). **This is the exact field
  §3.2's corrected `service_policy` row now names**, so PMM-06's derivation reads a contract PMM-02
  has specified rather than one it hopes for.
- **🟢 The U1 alias-keying divergence the previous revision flagged is closed, in U1's favour.** That
  revision noted PMM-02 keying aliases `UNIQUE (alias_source, alias_id)` while U1 required
  tenant-scoping too. PMM-02's new head adopts U1 and says so: *"**Uniqueness is `(org_id,
  alias_source, alias_id)` among active rows — tenant-scoped and source-qualified, never a global
  alias name.** Revision 4 specified `UNIQUE (alias_source, alias_id)`; ruling 1 corrects this"*
  (`5419…:552-554`), with the reasoning PMM-06 would have given — *"`agent_name` and `client_id` are
  not globally unique namespaces"*, and a table-global constraint creates *"a cross-tenant coupling
  in a table whose purpose is tenant-safe identity"* (`:557-560`). **Nothing for the synthesis to
  settle here any more**, and PMM-06 is unaffected either way because it reads only the resolved
  canonical ID, never an alias.
- **PMM-02 also forbids storing a raw subject in the *actor* column**, which is the same rule §3.2
  now applies to the snapshot's principal: `updated_by` takes *"the acting principal as a **canonical
  ID only**"*, and *"a raw service subject (an S1 row UUID, an S2 `agent_name`, an S3 `client_id`) or
  any presented alias must never be stored here"* (`5419…:435`). Two stories, two tables, one rule —
  which is why reading `actor.user_id` into a snapshot was a defect rather than a shortcut.
- **PMM-02 no longer proposes any DynamoDB projection.** Its revision-1 recommendation is explicitly
  withdrawn (`5419…:18-24`: *"the recommendation is withdrawn"*). §5.1's "no second projection"
  direction is therefore *agreement* with PMM-02's head, not a correction of it.
- **PMM-07's head confirms §2.4a and §5.1 exactly**, and has widened its own scope to five paths:
  *"`spawn_persona` is not the single enforcement point, and all five paths are now in scope"*
  (`5425…:216`, §2.2 at `:518`), with AC-02 required to *"cover **five** publish paths including
  GitLab and the engine"* (`:1474`). That is consistent with §5.1's split: PMM-06 refuses on the
  GitLab channel; **routing** it through a trusted resolution point is PMM-07's, and PMM-07's head
  has accepted that obligation.
- **PMM-09's head confirms the enforcing gate, and its earlier internal contradiction is resolved.**
  S4 rules *"**No enforcing flip** before PMM-06 authority/bootstrap is live (#3186 / #5195
  prerequisites) **and** PMM-07 delivers actionable requester feedback (#2293 behaviour)"*
  (`5427…:1080`), and its decision table now records this as *"✅ **SETTLED by S4,
  conservatively**"* with *"AC-07 may **not** be recorded 'blocked, flip anyway'"* (`:1055`). The
  *"it remains unresolved and it is an operator call"* text the previous revision had to override is
  gone. §7.2 and C3a state the gate per S4. Note the gate is now **two-part**: PMM-06's authority
  path *and* #2293 feedback. PMM-06 needs nothing from the second half, but should not describe the
  gate as single-condition.
- **PMM-01's head independently derives §2.6b, which strengthens it.** *"Determinism applies to
  **selection**, not to admission. Freezing an admission gate would be a security defect"*
  (`5417…:466`), and it binds the two stories together: *"PMM-06 owns the frozen half and PMM-07 the
  live half, so neither story may move a row across this table without amending this note"* (`:506`).
  §2.6b and C8(b) are the same split, reached separately. **The divergence to carry is unchanged:**
  PMM-01 freezes *the whole class-keyed default map*, *"the whole map as of the pinned revisions, not
  the subset 'reachable by this chain'"* (`:492`), so §3.1's `system_default_model_id` must be the
  whole map — otherwise a hop whose class differs from the root's cannot resolve. Adopted into C7's
  implementation direction.
- **🟡 PMM-01's rev-5 withdraws a claim PMM-06 never relied on, but the withdrawal is worth
  recording** because it is the same failure mode this note keeps correcting. Rev-5 *"withdraws
  rev-4's claim that a second compatibility class is already live: the pinned Codex model configures
  a delegated tool, `codex-sdk` has no persona mapped to it, and **a config-literal test is not
  invocability proof**"* (`5417…:8`, §8.8 at `:1186`). PMM-06 asserts nothing about a second live
  class, so nothing here changes. The transferable lesson is the one §8.1 already makes: a literal
  in a config file is not evidence that anything ran.
- **PMM-01's mapping key remains behind PMM-02's contract.** `5417…:21` still defines a mapping as
  `(tenant, principal kind, principal ID, persona key) -> canonical model ID` (repeated `:397`),
  whereas PMM-02 routes service identity through the canonical registry and does not key storage on
  `principal_kind`. PMM-06 keeps `principal_kind` as a **snapshot** field because §3.2 needs it to
  satisfy AC-03 regardless — but it must not be treated as part of the *storage* key. Still flagged
  for the synthesis (§8.2); it does not change this design.
- **One PMM-03 finding is still absent from PMM-07's inventory and PMM-06 should not assume
  coverage.** `5420…:177` flags `complex-task-chat-agent.ts:443` reading `persona.modelOverride ??
  process.env.ANTHROPIC_MODEL` — verified present at `ae598410` — and says *"PMM-07's 'single
  resolver' inventory must include it or it becomes a surviving divergent path"* (repeated at
  `:476`). PMM-07's new head still does not list it. This is a per-persona override outside every
  path PMM-06 touches; it matters here only as a reminder that AC-07's "the local fallback was not
  used" assertion (§8.1) has at least one more site than the inventory admits — the same shape as
  `entrypoint.py:1578`, which is why §4.3 closes that one by contract rather than by convention.

**Net effect.** No sibling head invalidates this design, and this revision **retires both 🔴 gaps
the previous one carried** rather than restating them: PMM-03's rev-3 supplies the
compatibility-class vocabulary and now agrees with U2 on the Sonnet candidate and with U3 on probe
posture. One new dependency state replaces them — **no model is certified invocable until PMM-09
probes**, so live admission has no passing evidence to read at PMM-06's completion (§8) — and one
correction flows *into* this note from PMM-07's P4′: the bootstrap response is `adpr1`-signed today,
so a worker-verifiable decision needs the `adpe1` signer it has never reached (§4.3). PMM-06's own
contract (class-keyed defaults, no cross-class fallback, recorded evidence never a probe,
gateway-only selection) is written to U1–U5 and unchanged by any of it.

---

## 3. The snapshot: contents, principal identity, and integrity

### 3.1 Snapshot contents

The union of the epic's list, the story's list, and D6's harness requirement. Every field is
present because some hop must be able to make a decision with it — a snapshot field with no
reader is the #4511 failure class again.

| Field | Why it must be present |
|---|---|
| `schema_version` | Lets a hop distinguish "older snapshot" from "corrupt snapshot", and is what a legacy worker reports as unsupported (AC-12) |
| `principal_kind` (`human` \| `service_account`) | **New, and required.** Without it AC-03 cannot be satisfied at all — see §3.2, including the exhaustive three-kind mapping and the #4337 D4c reconciliation |
| `principal_id` | The preference owner. For a human, the ADP user ID; for a service principal, PMM-02's **opaque immutable `canonical_service_principal_id`** — per **U1**, never `service_accounts.id`, `agent_name`, `client_id`, an ARN or any caller-supplied text, and never an alias (aliases are tenant-scoped and source-qualified, and resolve *to* the canonical ID) |
| `tenant_id` | Every verification compares this to the hop's own tenant (AC-06) |
| `mappings` (persona → canonical model ID) | The decision itself |
| `system_default_model_id` + `system_default_revision` | A persona absent from `mappings` resolves here, and records that source (AC-07). **Keyed by compatibility class, not a single scalar** — per **U2** and #5433, a `gpt-*` persona must not resolve to the Claude default, and **no cross-class fallback** is permitted. An absent default for the hop's class is a refusal, not an occasion to borrow another class's (§2.7, C7 — settled). The snapshot freezes the **whole** class-keyed map as of the pinned revisions, not the subset reachable by this chain — PMM-01's head is explicit on this (`5417…:403`), and the alternative leaves a hop whose class differs from the root's unable to resolve (§2.8) |
| `policy_revision` | Detects the mid-flight edit (AC-04) and a tampered revision (AC-05) |
| `allowlist_policy_revision` | D3 makes the allowlist a real admission gate. Recorded as **evidence of which revision the root resolved against**, for attribution and drift detection — **not** as a cached admission decision. Admission is re-checked live at each hop (§2.6b) |
| `harness_compatibility_class` | **U2.** The **stable, unversioned** class ID (`claude-agent-sdk`, `codex-sdk`). This is what `system_default_model_id` is keyed by and what "no cross-class fallback" is enforced on. Split out from the revision because U2 rules they are different things, and conflating them would make a class ID move whenever a harness contract did |
| `harness_compatibility_revision` | **D6 + U2.** The **separately versioned** harness/contract revision, part of the snapshot key. Each hop is checked against its actual harness |
| `correlation_id` + `root_invocation_id` | Binds the snapshot to *this* chain, so a genuine snapshot cannot be replayed onto another (AC-05) |
| `issued_at` + `expires_at` | D5 requires both. Bounds replay and the cache contract (AC-11) |
| `audience` | D5 requires it. Prevents a snapshot minted for one consumer being accepted by another |
| `source` (`live` \| `last_known_good_cache`) | AC-11 needs the cached case to be visible in audit, not silently equivalent |

The snapshot carries **no signature field of its own**. Its integrity comes from
`snapshot_digest` on the protected authority record, and per-hop authenticity from a separate
short-lived `adpe1` assertion whose `body_digest` is that digest (§3.3). Keeping the signature
out of the snapshot body is deliberate: it avoids the canonicalization ambiguity of signing a
structure that contains its own signature, and it means a stored snapshot never expires merely
because a key rotated.

Canonical model IDs only, and per the epic the stored effective value must not drift when a
provider moves a "latest" alias — so the snapshot stores resolved canonical IDs, never aliases.

**One correction from U2 about the default itself.** D4 names
`us.anthropic.claude-sonnet-4-6`, and an earlier revision of this note called it "the canonical
default". U2 is narrower: it is a Claude-class **candidate**, and *not an active proven default
until PMM-09 records a bounded invocation using the actual Claude harness request shape.* The
distinction is load-bearing for PMM-06, because `system_default_model_id` is what a persona with
no mapping resolves to — writing an unproven identifier there would make the snapshot authoritative
for a model nobody has demonstrated is invocable. Until PMM-09 records that evidence, a
class-keyed default with no proven value must refuse rather than resolve, which is the same rule
as the no-cross-class-fallback one above and consistent with U3 (recorded evidence only, never a
probe).

### 3.2 🔴 Blocking prerequisite: the envelope cannot express "service-rooted"

This is the most consequential finding in this review, and the story does not mention it.

**AC-03 requires:** *"Start the equivalent chain rooted at a service account with different
mappings → Every hop uses the service account's mappings, not any human's."*

**What the code does.** Both root dispatch paths write the *same* correlation block shape,
and both claim human-rooted:

- Human root — `agent_authority.provision_human_dispatch` produces an authority record with
  `authority_kind = "github_event"` and `human_id` (`agent_authority.py:129-131`).
- Service root — `service_authority.provision_service_dispatch` validates
  `authority_kind == "service_policy"` (`service_authority.py:80`), requires a non-empty
  `human_id` on the authority row (`service_authority.py:82`), and then writes
  (`service_authority.py:117-123`):

```python
"correlation": {
    "correlation_id": flow,
    "root_human_id": authority["human_id"]["S"],
    "is_human_rooted": True,
    "parent_invocation_id": None,
    "chain_depth": 0,
},
```

The gateway's agent-to-agent child envelope hardcodes the same, unconditionally, for every
authority kind (`dispatch.py:281`):

```python
"is_human_rooted": True,
```

So on a service-rooted chain the envelope says `is_human_rooted: True` and carries a **human**
ID in `root_human_id`. The service-account identity is present only in
`actor.user_id` / `actor.kind = "service"` (`service_authority.py:109-116`) — and `actor` is
descriptive, not authoritative.

**Why this is not a bug to fix here.** It is deliberate, and it is correct for its own
purpose. `is_human_rooted` and `root_human_id` exist to answer *"whose vault credentials may
this run use, and for how many hops"* (#3174). `_compute_authorized_user_id`
(`spawn_persona.py:665-712`) is the consumer. A service-rooted flow is authorized *by* a
human's recorded act, so attributing it to that human for audit is right. The story's own
"three verified constraints" item 2 makes exactly this point — credential binding and
preference ownership are different notions that *"must not be conflated in code that already
exists."*

**Why it nevertheless blocks AC-03.** The epic's rule is the opposite for preferences: *"If a
service account starts a chain on behalf of a human without trusted delegated-root evidence,
the service account remains the root. It cannot claim another user's preferences."* Reading
`root_human_id` would select **the authorizing human's mappings**, which is precisely the
failure AC-03 exists to catch. And the one place that does treat
`is_human_rooted is False` as authoritative — `bedrock_principal.py:63-65` — never fires on
these paths, because both set it to `True`.

**Direction.** The snapshot must carry its **own** `principal_kind` and `principal_id`,
derived at creation from the **authority record's `authority_kind`**, which the worker role
cannot write — not from `is_human_rooted`, and not from `actor`. There are **three** live
authority kinds, not two, and the mapping must be exhaustive:

| `authority_kind` | `principal_kind` | `principal_id` | Evidence |
|---|---|---|---|
| `github_event` | `human` | authority `human_id`, resolved to the canonical user entity (`resolve_root_user_entity_id`, as the model path already does for this kind at `model_identity.py:71-74`) | `agent_authority.py:129-131` |
| `service_policy` | `service_account` | PMM-02's **`canonical_service_principal_id`**, resolved *from* the authority row's verified `service_identity` through PMM-02's alias registry — **never `actor.user_id`** | `service_authority.py:80`; authority-row check `:76`; **U1** |
| `gate_decision` | `human` | `genesis.root_human_id` | `engine.py:31, :101`; `coordinator.py:157` |

**Why the `service_policy` row must not read `actor.user_id`, corrected here.** An earlier
revision of this table named `actor.user_id` as the service principal's ID. **That is wrong and
U1 forbids it.** Two independent reasons:

- **U1 rules the identifier out by name.** Preferences are owned by *"an opaque immutable ADP
  `canonical_service_principal_id`"*, and *"raw `service_accounts.id`, `agent_name`, `client_id`,
  ARN or caller-supplied text never owns a preference."* `actor.user_id` is a copy of
  `event.service_identity` (`service_authority.py:112`), which originates in the event's own
  identity block (`VerifiedServiceEvent.from_native_event`, `:29-52`). Whatever verification it
  passes, it is a **source-supplied name**, not a platform-issued canonical ID — exactly the
  category U1 excludes.
- **`actor` is descriptive, not authoritative**, which this note already says three paragraphs
  above. Deriving the preference owner from `actor` would contradict the same section's own rule
  and would make the snapshot's owner field the one value on the record that came from outside.

The correct derivation reads the **authority row**, which the worker role cannot write and whose
`service_identity` the provisioning path has already matched against the event
(`service_authority.py:76` refuses unless `authority["service_identity"]` equals
`event.service_identity`), and then resolves that verified identity **through PMM-02's alias
registry to the canonical principal**. Per U1, aliases are tenant-scoped and source-qualified, so
that resolution is keyed on the tenant too — it is not a global name lookup.

**Fail closed when the alias does not resolve.** Per U5 (and C9), an initiator with no registered
canonical service principal is a construction-time refusal. It must not fall back to the
authorizing human's ID, to the executing App identity, or to the raw `service_identity` — all
three would silently apply the wrong principal's mappings, which is the precise failure AC-03
exists to catch.

**The live model path already branches on exactly this three-way split, which both corroborates the
table and pinpoints what PMM-06 adds.** `model_identity.py` reads `root = grant.authority.human_id`
(`:71`) and then canonicalizes it **only when the authority is not `service_policy`**:
`if grant.authority.kind != "service_policy":` (`:72`) gates the `resolve_root_user_entity_id` call
(`:74`), and inside that same branch `gate_decision` gets its own additional policy-admission path
(`:76-90`). Downstream it attributes with `user_id=root`, `root_human_id=root` and
`is_human_rooted = grant.authority.kind != "service_policy"` (`:169-171`).

Two things follow, and they cut in opposite directions:

- **The three-way distinction is not a PMM-06 invention.** The gateway already treats
  `service_policy` as the kind that must *not* be resolved through the human-entity resolver. The
  table above is the same branch, read for preference ownership instead of attribution.
- **But the service arm currently resolves to nothing usable, and that is the gap PMM-06 fills.** On
  a `service_policy` authority, `root` stays `grant.authority.human_id` — the *authorizing human* —
  unresolved, and is then written to `user_id` and `root_human_id`. For attribution that is correct
  (§3.2's "why this is not a bug" argument). For preferences it is precisely AC-03's failure: the
  only identifier the live path carries for a service-rooted run is the human who approved it. So
  PMM-06 is not overriding an existing service-principal resolution — **there isn't one** — it is
  adding the arm that U1's canonical registry makes possible. Stating it this way matters, because
  "read the canonical service principal instead" reads like a substitution when it is an addition,
  and an implementer looking for the value to replace will not find one.

`gate_decision` is the AI-DLC orchestration root and the existing code already answers it the
same way: `model_identity.py:171` computes `is_human_rooted = grant.authority.kind !=
"service_policy"`, i.e. a gate-rooted chain is human-rooted with `grant.authority.human_id` as
its principal. **A two-way mapping is a live defect, not a simplification**: an implementer who
writes only the first two rows emits a snapshot with no `principal_kind` for every
orchestration-rooted chain, which §6's matrix then rejects — turning the enforcing flip into a
dispatch outage for the whole AI-DLC path. Enumerate the kinds from a single shared mapping and
make an unrecognized kind a loud construction-time failure, never a default to `human`.

The snapshot becomes the authoritative statement of
preference ownership, and the existing lineage fields keep their existing credential meaning,
untouched. This satisfies the story's compatibility requirement that *"expiry of delegated
vault access must not change which principal's model policy governs the chain"* structurally:
the two live in different fields with different producers.

**Reconciling this with #4337 D4c, which forbids exactly this shape of field.**
`run_binding.root_principal_type` (`run_binding.py:300-322`) answers "human or service" as a
**derived property**, and its docstring states the rule as a prohibition: it is *"DERIVED rather
than stored, deliberately… a second field holding the same fact is two homes for one value — the
divergence trap… A property cannot desync."* A stored `principal_kind` looks like precisely the
field that rule bans, and on a `service_policy` root the two will visibly disagree:
`service_authority.py:120` writes `is_human_rooted: True`, so `root_principal_type` reads
`"human"` and billing attributes to the person, while the snapshot in the same message says
`service_account`.

They are not two homes for one fact — they answer different questions (*who pays and whose
credentials apply* versus *whose model preferences govern*), which is the same separation the
story's own constraint 2 demands. But that is a claim this note must make explicitly rather than
leave a developer to discover as an apparent invariant violation. **Direction:** keep
`principal_kind` scoped to preference ownership only, never read it for billing, attribution or
credential decisions, and never derive `root_principal_type` from it. Add a comment at the
snapshot's definition pointing at #4337 D4c and stating why this field is not that field.

**Operator decision required (§9, C1):** confirm that for a `service_policy` authority the
**service account** is the preference owner, and the `human_id` recorded on the authority row
is audit attribution only — and that the resulting divergence from `root_principal_type` on the
same run is intended. This follows from the epic text, but it contradicts the natural
reading of the envelope a developer will see, so it must be explicit.

### 3.3 Integrity: what actually roots the snapshot's trust

D5 requires the snapshot to be *"created and signed by the trusted gateway/authority service"*,
that workers *"never receive private signing keys or a shared signing secret"*, and that every
hop *"verify the signature, expiry, audience, chain/root binding, and snapshot hash."*

There are **two** existing mechanisms in this tree that partially satisfy it, and they have
different shortcomings. Choosing between them is the main design decision, so both are stated
before the recommendation.

#### Mechanism A — asymmetric signer with worker-side verification (exists, wrong lifetime)

An Ed25519 signing and verification system **already exists end to end**, and it matches D5's
literal wording closely:

- Signer: `modules/gateway/src/agentauth/envelope.py` — `sign_envelope` :228-282, Ed25519 via
  `cryptography`, `ALLOWED_ALGORITHMS = frozenset({"ed25519"})` :78. The allowlist is checked at
  `verify_envelope`:342, *before* the signature verification at :356, so JWT `alg`-confusion has
  no foothold. Canonical JSON signing input `_canonical` :204-205; wire format
  `adpe1.<b64 body>.<b64 sig>` :281, with the version string prepended to the signed bytes :280
  (domain separation, so a body cannot be replayed under a future envelope version).
- Claims already include almost exactly what D5 demands: `iss`, `aud`, `alg`, `kid`,
  `tenant_id`, `principal`, `grant_id`, `revocation_epoch`, `body_digest`, `iat`, `nbf`, `exp`,
  and optional `flow_id` / `authority_reference_id` (:255-277).
- **Workers verify it themselves**, in TypeScript: `modules/agent-factory/agent/src/control-envelope.ts`
  mirrors the constants and claim set (:47-70), using Node `crypto.verify` with no added
  dependency. Parity between the two implementations is pinned by shared golden vectors
  (`agent/src/__fixtures__/control-envelope-vectors.json`, checked by
  `modules/gateway/tests/agentauth/test_envelope_vectors.py`).
- **Key distribution and rotation already exist.** `webhook-ingress/infra/agent-authority-bootstrap.tf`
  generates two Ed25519 slots (:11-19), publishes public keys to workers as
  `ADP_CONTROL_ENVELOPE_KEYS` / `..._KEYS_FILE` (:38-42) with `kid` = first 16 hex of the
  PEM's SHA-256 (:27-31), and the private key goes only to the gateway namespace, explicitly
  *"outside the worker's Secrets Manager `adp/*` read grant"* (:1-4). Staged rotation is
  governed by `agent_control_signing_key_slot` and `agent_control_publish_both_keys`
  (`variables.tf:473-487`) with a runbook at `docs/runbooks/agent-authority-key-rotation.md`.
  The worker entrypoint passes these through as **public** keys and says so
  (`entrypoint.py:2761-2763`: *"The keys are public verification keys… There is no signing key in this environment"*).

**Why it cannot be used as-is:** `MAX_ENVELOPE_TTL_SECONDS = 30` (`envelope.py:95`), and the
comment there states the bound's purpose plainly — it *is* the documented maximum revocation
delay for an in-flight command. Both sides enforce it, and note the asymmetry — it is
**signer versus verifier**, not Python versus TypeScript: the signer **clamps** a longer request
down (`envelope.py:253`, `min(int(ttl_seconds), MAX_ENVELOPE_TTL_SECONDS)`) while **both**
verifiers **reject** outright — `envelope.py:389` ("envelope validity exceeds policy", whose
comment explains that truncating "would accept a signed statement whose signer disagreed with our
policy") and `control-envelope.ts:364-366` (`validity_too_long`). So a caller asking for a
one-hour snapshot envelope does not get an error — it gets a silently 30-second one. A chain can
outlive 30 seconds by orders of magnitude. Using this signer for a long-lived policy snapshot
therefore means either raising a security-relevant bound that two independent implementations
enforce on purpose, or re-signing per hop.

**Re-signing per hop is the promising variant**, and it fits the architecture: the gateway
already mediates every agent-to-agent dispatch (`dispatch.py`) and every model call
(`model_identity.py`), so it can re-issue a short-lived snapshot assertion at each hop from
the durable record. That keeps the 30-second bound intact and gives each hop a genuinely
worker-verifiable artifact. It costs a gateway round trip **per hop** — acceptable off the
webhook path, and not required on it (§5).

#### Mechanism B — protected record + digest (what dispatch authority actually does today)

What the delegated-authority path does today is deliberately **not** signing:

- Root dispatch writes an authority/execution record and stores
  `envelope_digest` — a SHA-256 over the canonicalized envelope
  (`agent_authority._digest` at `agent_authority.py:67-72`; stored at `:183`; the service path
  at `service_authority.py:138`; the gateway's own copy is
  `modules/gateway/src/agentauth/bootstrap.py:18` and it is used for the agent-to-agent child
  at `dispatch.py:291, 305, 353`).
- The record lives in the authority table, whose module docstring states plainly:
  *"The worker role has no write permission on this table"* (`agent_authority.py:1-7`).
- The gateway re-derives and compares the digest before honouring a request
  (`bootstrap.py:305`: `if lookup is None or lookup.get("envelope_digest") != {"S": digest}`),
  including inside a conditional transaction (`bootstrap.py:336`).
- The model path already authenticates the worker and resolves the root principal server-side
  in `AgentModelIdentityMiddleware` (`model_identity.py:37-75`), which rejects mismatched run
  and tenant assertions (`model_identity.py:67-70`) and reads the root from
  `grant.authority.human_id` (`:71`) — not from anything the worker sent.

This is a **keyed-record integrity model**: unforgeable because the attacker cannot write the
record, rather than because it cannot compute a MAC. For a snapshot, it is *stronger* than a
worker-verifiable signature in one specific way — it cannot be replayed at all, because the
record is per-invocation — and *weaker* in another: a hop cannot verify offline, it must read
the protected record.

**That weakness is where the ten-second budget bites**, and it is why §5 separates the two
directions of travel.

#### Recommendation: B at creation, A at each gateway-mediated hop

The two mechanisms are not competitors; they cover different hops, and D5's own text splits
along the same seam. The recommended model is therefore **both**, applied where each is sound:

1. **At creation — bind, do not sign (Mechanism B).** The snapshot is written to the protected
   execution row for the invocation, with `snapshot_digest` stored alongside the existing
   `envelope_digest`. **The snapshot is *not* placed in the envelope and is not covered by
   `envelope_digest`** — §4.1a explains why it cannot be: the envelope is sealed before admission
   runs, so adding a key after the fact would break the digest for every dispatch. What makes
   tampering pointless is therefore stronger than detection: the queue message carries no policy
   at all, and the row holding it is one the worker role cannot write (`agent_authority.py:1-7`).
   This needs no new key and no new secret, so it adds no placeholder-key hazard — the exact
   hazard the story's Deployment section warns about — and it fits the webhook path, because the
   ten-second budget (§5) does not permit a synchronous gateway signing call during ingress.
2. **At each gateway-mediated hop — re-issue a short-lived assertion (Mechanism A).** Every hop
   bootstraps against the gateway (`routes.py:279-293`), which verifies the workload/run/root
   binding, **resolves that hop's model decision itself** (§4.3), and mints a fresh `adpe1`
   envelope whose `body_digest` is the digest of exactly the decision bytes it returns (§4.2a).
   What is signed is therefore the *outcome* of selection, not a table the worker would select
   from. This reuses the existing signer, the existing
   `ALLOWED_ALGORITHMS` allowlist, the existing two-slot rotation, the existing worker-side
   verifier and the existing golden-vector parity tests. **`MAX_ENVELOPE_TTL_SECONDS = 30`
   stays untouched**, because the assertion is per-hop and short-lived even though the policy
   it attests to is long-lived. No new signing service and **no new key material**.

**What reuse does not come for free — the signer's contract must be widened.** The key material
and rotation are genuinely free, but `sign_envelope` is not a general-purpose signer, and this
note must not imply it is:

- **It has no `audience` parameter.** `sign_envelope` (`envelope.py:224-241`) hardcodes
  `"aud": ENVELOPE_AUDIENCE` (`:258`), and `ENVELOPE_AUDIENCE = "adp-agent-control-listener"`
  (`:85`) is checked strictly by both verifiers (`envelope.py:346`,
  `control-envelope.ts:318`) with the comment that it is *"not configuration a worker could be
  tricked into widening."* A snapshot assertion minted today would claim to be a control-listener
  command.
- **It has no chain-binding claim.** `_REQUIRED_CLAIMS` (`envelope.py:99-116`) demands
  `target_run_id`, `target_generation`, `action` and `command_id`. `target_run_id` is per-run;
  the only chain-scoped field is the *optional* `flow_id` (`:272-273`).

**U4 rules this extension in, so it is a build instruction rather than an open question.** The
unified ruling requires the bootstrap assertion to be *"audience- and chain-bound"*, which is
precisely the extension this section identified: a snapshot-specific audience constant plus a
chain-binding claim, added on **both** sides of the golden vectors
(`agent/src/__fixtures__/control-envelope-vectors.json`,
`modules/gateway/tests/agentauth/test_envelope_vectors.py`). It remains a change to a security
contract rather than a no-op, and it must not be smuggled in by minting tokens whose `aud` and
`action` misdescribe their purpose. Note also that adding a new `action` member touches the shared
`AgentAction` vocabulary (`grants.py:47-62`), whose supported set is deliberately narrow
(`policy.py` `SUPPORTED_AGENT_ACTIONS`); prefer a distinct audience over widening that verb list.

With that extension in place this gives the literal D5 properties — signature, expiry, audience,
chain binding and snapshot hash, all verified worker-side with public keys only — on every hop
that can afford a gateway round trip, while the snapshot itself remains a durable record rather than a
long-lived bearer token. The long-lived-signed-blob alternative is specifically what this
avoids: a 30-second assertion cannot be replayed a day later, and revocation stays bounded by
the same window the runbook already documents.

#### No worker acts on a model decision before bootstrap has verified its binding

The per-hop assertion is necessary but not sufficient on its own, because it says nothing about
*which* workload is entitled to the decision it is holding. The binding rule is therefore
mandatory and sequenced ahead of any use:

1. **A worker must not invoke a model until gateway bootstrap has verified the workload/run/root
   binding for that worker**, and returned a short-lived signed **model decision** for that hop's
   persona (§4.3). Because selection is gateway-side, this rule is also what makes the worker's
   "no resolver" contract enforceable: a worker with no decision has nothing to act on and no
   table to improvise from. Bootstrap already performs exactly this class of check and already
   compares a digest against the protected record (`bootstrap.py:305`, and inside a conditional
   transaction at `:336`); the model path already refuses mismatched run and tenant assertions
   (`model_identity.py:67-70`) and reads the root server-side rather than from the worker
   (`:71-74`). The decision rides those existing controls; it does not get a parallel, weaker one.
2. **The assertion is reissued per hop**, over that hop's own workload binding, and
   `MAX_ENVELOPE_TTL_SECONDS = 30` (`envelope.py:95`) is retained unchanged — it is the
   documented maximum revocation delay, and `agent_control_publish_both_keys`' own description
   depends on it (`variables.tf:473-487`).
3. **No long-lived, worker-verifiable snapshot token is issued, at any hop.** A snapshot the
   worker can verify offline for the life of a chain is a bearer token for model policy: it
   survives revocation of the grant it was minted under, and it is replayable for as long as it
   is valid. This is the same artifact §3.3's opening recommendation refuses, restated as a
   binding rule so an implementer cannot reintroduce it as a caching optimisation.

**The remaining asymmetry, stated honestly, and its practical consequence.** It is no longer an
asymmetry about what a worker holds: under §4.1a the envelope carries nothing about the snapshot,
and under §4.3 no worker holds a decision it did not receive from bootstrap, so *every* hop —
root and descendant alike — is equally decision-less until bootstrap answers. The asymmetry that
remains is upstream of the worker, in what the **gateway** is relying on when it decides. For a
descendant hop, the dispatch that created it arrived at `/work/admit` carrying an `adpe1`
control-envelope assertion the gateway verified against a published key, so the gateway resolves
that hop's decision on top of an authenticated chain link. For the *first* hop — the
webhook-triggered root run — there is no upstream assertion to verify: the authority row is
written from the inbound event, and the gateway's decision for the root therefore rests on an
unsigned origin rather than a verified one.

This is the honest reason enforcement must wait, and it is narrower than "report-only is fine":
**report-only may observe and record**, because observation makes no trust decision. But
**enforcing mode cannot ship while the first hop is unsigned in practice**, and it is gated on
#3186/#5195 — exactly what D5's last bullet concedes: PMM-06 may introduce generation and
verification in report-only mode, but *"enforcement is gated on the gateway-mediated
delegated-authority path being live and accepted."* Closing it any earlier would mean either
distributing signing key material to workers — forbidden by D5 and already refused by
`marker_signing.py:46-52` — or a synchronous signing call inside the webhook budget.

**Second-order hazard on the Mechanism A leg.** The signer is live, but its *delivery* path is
not. In `routes.py`, `control()` calls `prepare_command(...)` — which mints and records the
envelope successfully — and then raises `PolicyError(501, "... is not implemented in this
deployment")` (:236-247). The comment there explains why this is deliberate: *"Enabling a policy
verb alone must never return success without actually forwarding its effect."* That is the right
instinct and it is exactly the inert-config guard (#4511) applied to control verbs.

The consequence for PMM-06 is specific: envelope **minting** is reachable and testable today,
but a command-shaped **round trip** is not. So the per-hop leg must be sequenced behind that
forward path becoming live — not behind the signer, which is ready. Do not "fix" the 501 inside
PMM-06; it belongs to the authority work, and removing it without implementing forwarding would
convert a loud failure into a silent one.

**Operator decision required (§9, C2):** confirm the split — protected-record binding at
creation, existing-signer per-hop assertions at gateway-mediated hops, `MAX_ENVELOPE_TTL_SECONDS`
unchanged, and no long-lived signed snapshot blob. The alternative the operator may prefer is a
single long-lived signed snapshot verified offline at every hop; that requires raising a
security-relevant TTL bound that two independent implementations enforce deliberately, and
introduces replay exposure across the snapshot's whole lifetime. This note recommends against it.

### 3.4 Key rotation

Under the recommended model, **PMM-06 introduces no new key and no new rotation surface.** The
per-hop leg reuses the existing control-envelope keys, so it inherits the mechanism already in
place, and the story's rotation work reduces to conformance rather than construction:

- **Two-slot staged rotation already exists.** `agent-authority-bootstrap.tf:11-19` generates
  primary and secondary Ed25519 slots; `agent_control_signing_key_slot` selects the signer and
  `agent_control_publish_both_keys` publishes both public keys during overlap
  (`variables.tf:473-487`). `docs/runbooks/agent-authority-key-rotation.md` is the procedure.
- **`kid` selection already exists** on both sides: `kid` = first 16 hex of the PEM's SHA-256
  (`agent-authority-bootstrap.tf:27-31`), and the worker verifier selects by `kid` from the
  published key map (`control-envelope.ts`), so a retired-key assertion stays verifiable
  through the overlap window.
- **Rotation is bounded by 30 seconds, not by snapshot lifetime.** This is the practical payoff
  of recommending per-hop assertions over a long-lived signed blob, and the existing variable
  description states the dependency outright: `agent_control_publish_both_keys` should be set
  false *"only after the old signer is gone and its 30-second forwarding window has elapsed"*
  (`variables.tf:483-487`). Per-hop assertions keep that sentence true unchanged. A long-lived
  signed snapshot would silently invalidate it — the overlap window would have to exceed the
  maximum snapshot lifetime, turning a 30-second operator wait into a days-long one, and the
  existing runbook would become wrong without anyone editing it.
- **Placeholder refusal must be asserted, not assumed.** `marker_verify.py:120-122` is the
  precedent: a rotation *from* a placeholder must not silently accept the placeholder as the
  previous key. PMM-06 should carry a test that a snapshot assertion signed under a placeholder
  or absent key is refused rather than trusted — this is cheap to add and is the failure mode
  the story's own Deployment section flags.

The snapshot record itself is protected by table permissions, not by a key, so a rotation
cannot invalidate stored policy — only in-flight assertions, which expire in 30 seconds anyway.

---

## 4. Creation, propagation, and per-hop selection

### 4.1 Where the snapshot is created

**Not in `spawn_persona` — in the gateway, at work admission (§5.1).** An earlier draft placed
creation in `spawn_persona()` on the story's premise that it is "the single point every trigger
adapter funnels through." §2.4a corrects that premise: two real publish paths (GitLab, the
orchestration engine) never reach `spawn_persona` at all, and the engine's bypass is a
deliberate ruling. A creation site that two producers skip is not a single enforcement point —
it is a default-shaped hole.

The snapshot is therefore **created by the gateway** inside the `/work/admit` call that already
gates publication (`sqs_publisher.py:53-66`), from the protected dispatch record the gateway
resolves for itself, and persisted with its digest on the worker-unwritable authority record.
`spawn_persona` remains where the envelope is built, but it is not where policy is decided or
where trust originates.

#### 4.1a 🔴 The admission protocol: the envelope is sealed *before* admission runs, so nothing may be added to it

A previous revision of this note said the gateway returns a reference on the `/work/admit`
receipt and `spawn_persona` then embeds it in the envelope. **That is impossible, and the
second-pass operator review was right to refuse it.** The existing runtime order is fixed and
runs the other way:

| Order | Location | What happens |
|---|---|---|
| 1 | `spawn_persona.py:241-246` | `provision_human_dispatch` / `provision_service_dispatch` run `prepare_envelope`, then compute and **store** `envelope_digest` over the final envelope (`agent_authority.py:170`, `:183`) |
| 2 | `spawn_persona.py:286` → `sqs_publisher.py:56-61` | `publish_envelope` calls `admit_issue_work(envelope)` — **admission runs inside publication, after the digest is already written** |
| 3 | `sqs_publisher.py:67` | `json.dumps` puts the envelope on the wire |
| 4 | worker start-up | `run_identity.py:69-71` hashes the **entire** envelope; the gateway compares it to the stored digest (`bootstrap.py:305`) and refuses on mismatch |

So a post-admission envelope mutation invalidates the protected digest that was computed at
step 1. The failure is not confined to model policy: bootstrap refuses, and **every dispatch on
the work-claim path fails.** Moving admission earlier is not an option either — it is deliberately
inside `publish_envelope` so that nothing is published unless admission succeeds, and it is keyed
on an `invocation_id` that only exists once the envelope is built.

**Resolution: key the snapshot on the run identifier and put nothing new in the envelope.**
`message_id` — the run identifier — is already inside the envelope before it is sealed
(`spawn_persona.py:610`, echoed as `"message_id": invocation` into the digested dict at
`agent_authority.py:161` before `_digest` at `:170`), and it is the one value `/work/admit`
already receives (`gateway_client.py:324-327`, which sends `{"invocation_id": envelope["message_id"]}` and nothing else; `WorkAdmissionRequest.invocation_id`,
`work_routes.py:36-38`). The gateway therefore stores the snapshot **against that invocation**,
on the same protected execution row it is already touching. The envelope gains **no new key**, so:

- the digest computed at step 1 stays valid and no dispatch breaks;
- there is nothing in the queue message to tamper with, because the message never carries policy;
- the snapshot needs no size budget (§7.1a(b) is retired for this reason);
- the `Decimal` serialization hazard is retired for the snapshot too, because no snapshot value
  is ever hashed into the envelope (§7.1a(a) survives only as a general rule for the Lambda).

The receipt may still report *whether* a snapshot was persisted, for report-only observability.
It must not be the transport: nothing on the receipt can enter the envelope.

**What remains ordered inside `spawn_persona`.** Only that a dispatch about to be blocked never
reaches admission at all — guards and the depth check already run first (`:379`, `:435-449`), so
no policy record is created for a run that never happened. There is no longer any requirement to
insert a snapshot field before `_build_envelope`, and a contract test should pin the opposite:
**the envelope key set is unchanged by this story**, so a legacy worker's digest and a current
worker's digest agree byte-for-byte.

**Root detection.** A root is where `_advance_chain_depth` produced `chain_depth == 0`
(`spawn_persona.py:342`), which happens only for a genuinely new chain. Per the settled rule
in `determine_correlation` (`handler.py:960`), lineage is re-resolved server-side and markers
are advisory — so root-ness is a server-side conclusion, never a caller assertion. A forged
root marker therefore cannot mint a snapshot for another principal, which is AC-05's core.

### 4.2 Propagation and how a hop authentically obtains the snapshot *contents*

**Nothing about the snapshot travels in the envelope** (§4.1a). No mappings, no reference, no
digest — the envelope's key set is unchanged by this story. The snapshot lives on the protected
execution row, keyed by the invocation already named in the sealed envelope.

#### 4.2a 🔴 A digest cannot supply a decision — selection is gateway-authoritative

The second-pass review's other objection: an earlier revision had the worker receive "a reference
plus digest, and an assertion over only the digest." A digest is one-way. It lets a holder
*confirm* a value, and gives it no way to *obtain* one. That design left every hop with nothing to
act on.

**Resolution: the gateway resolves the hop's model itself and returns a signed decision.** The
third-pass review requires this to be a *decision*, not a mapping table — the gateway is the only
authoritative selector and the worker holds no resolver (§4.3). So what the response carries is the
outcome of selection (persona, resolved model, resolution source, the `snapshot_digest` it came
from, and the snapshot-versus-live policy revisions), with the `adpe1` assertion's `body_digest`
over exactly those bytes. The snapshot itself may accompany it as **audit evidence** — so a hop can
record and an operator can explain what governed the run — but it is not an input the worker
computes from, and delivering it is optional to the mechanism. Nothing in the worker reads
`mappings` to choose anything.

The delivery point already exists and already performs the whole check this needs —
`POST /internal/v1/agent/self/bootstrap` (`routes.py:279-293`):

| It already does | Evidence |
|---|---|
| Verifies the calling **workload**, not a bearer claim | `routes.py:249-250` (`self.workloads.verify(token)`), TokenReview against live pod facts (`workload.py:93-102`) |
| Verifies **run and root binding** before answering | `store.bind(...)` at `routes.py:258`, which refuses unless the stored `envelope_digest` matches (`bootstrap.py:305-306`), the pod binding is immutable (`bootstrap.py:318-324`) and the grant and authority still hold (`bootstrap.py:350-351`) |
| Refuses rather than degrades | `BootstrapRefusedError` → 404 (`routes.py:298-299`); work not yet owned → 425 with `Retry-After` (`:295-296`) |
| **Returns a response body to the worker** | `issue_bound_credential(...)` at `routes.py:292` (and `:259`), `Cache-Control: no-store` at `:293` |

**One structural fact that makes the gateway's step 3 cheaper than it looks.** §4.3 has the gateway
re-check live admission at each hop. That is not a new call graph: `bootstrap()` **already invokes
work admission inside itself**, before it binds or returns anything. `routes.py:251-256` imports
`admit_deferred_bootstrap` and runs it after `workloads.verify` (`:250`) and before `store.bind`
(`:258`), and its own
docstring states the ordering as a contract — *"Called after pod verification, before binding or
returning any credential"* (`work_admission.py:184`). It re-reads the protected dispatch pointer for
the tenant (`:189-192`), re-verifies the `envelope_digest` against the stored value (`:194-195`),
enforces the startup deadline (`:198-206`), and calls `admit_pending` (`:208`), surfacing a lost race
as `work_waiting` (`:210-212`) — the 425 the table above records. So the hop-time live gate §4.3
needs is an *extension of a gate that already runs at exactly this point*, not a new synchronous
dependency inserted into the bootstrap path. This is the same argument as §4.1a's, in the other
direction: admission is already co-located with the moment PMM-06 needs it.

So the decision is delivered **on the bootstrap response**, to a workload the gateway has just
proven is the one bound to this run, alongside a fresh short-lived `adpe1` assertion whose
`body_digest` is the digest of the delivered decision bytes. The hop can then confirm that what it
received is what was signed, and an auditor can see what it acted on. The worker never reads the
protected row directly and holds no signing key — public keys only
(`entrypoint.py:2761-2763`: *"The keys are public verification keys… There is no signing key in this environment"*).

**What this response does not carry today.** `issue_bound_credential` returns `credential`,
`invocation_id`, `attempt`, `credential_epoch` and `expires_in` (`bootstrap.py:372-388`). The model
decision and its assertion are **new fields PMM-06 adds**; this note does not claim the channel
already exists, only that the verification and refusal behaviour around it does (§4.3).

**Why this does not weaken the model.** The decision is not a bearer token: it is handed to a
verified workload, it is bound to that run, and the assertion over it expires in
`MAX_ENVELOPE_TTL_SECONDS = 30` (`envelope.py:95`) and is reissued per hop. A leaked decision
authorizes nothing — it names a model, and invoking that model still requires the worker's own
verified credential and passes the live gates the gateway applied before issuing it (§2.6b).

**Reissue per hop.** Each gateway-mediated hop bootstraps for itself and receives **its own
decision** for **its own persona**, with its own assertion; nothing is forwarded agent-to-agent, and
no hop's decision is valid at another. This is what makes the "per hop" property real rather than
nominal — and it is why the gateway, which holds the snapshot, is the natural place for selection to
happen: it is already being asked, once per hop, by a caller it has already authenticated.

#### 4.2b Non-root hops

A non-root hop **uses the same snapshot, resolved server-side from its chain, not copied by its
parent.** It does not re-read preferences, does not merge, and does not refresh — that is what
makes AC-04 (mid-flight edit) hold by construction rather than by a freshness check. The
agent-to-agent path (`dispatch.py:264-285`) must associate the child's execution row with the
**parent's verified** snapshot, from the parent's protected record, not from anything the calling
agent sent — the same reason `correlation_store.py` is not authoritative: the pod can write it.
`dispatch.py` already reads the parent's execution row server-side from the protected table
(`dispatch.py:153`, `self.store._read(pk, f"EXEC#{caller.invocation_id}")`) and derives the child's
grant from the parent's rather than from the request (`:240-253`, including
`actions <= parent.delegable_actions` at `:247`); the store then refuses when parent delegation has
changed (`bootstrap.py:298`, with a cancelled/revoked parent refused at `:284`). So the child's
snapshot association rides an existing server-side control rather than needing a new one.

**"Does not refresh" scopes to the preferences only.** It is not a licence to skip the live
admission checks at that hop: the snapshot is frozen, the *permission* is not (§2.6b, and the
gateway's step 3 in §4.3). Reading this rule as "the snapshot already decided everything" is the
error that would turn a frozen choice into a frozen authorization. Note the check runs
**gateway-side at each hop's bootstrap**, so "the child does not refresh" describes the preference
data, not a relaxation the child could exploit — the child never holds the decision-making code.

### 4.3 Per-hop selection: the gateway decides, the worker verifies and obeys

**The gateway is the only authoritative selector.** An earlier revision of this section listed
five steps a *worker* performed on a snapshot it had been handed: resolve its class, look up its
persona, fall back to a class default, then admit. **That design is withdrawn and its procedure is
removed, not annotated** — the third-pass review is right that handing an agent a mapping table and
a set of rules makes the agent the selector in fact, whatever the surrounding prose says.

The reason is concrete and specific to this codebase. A worker that performs the lookup is a
worker that can *not* perform it: `entrypoint.py:1576-1578` already reads a model from its input
and, when absent, substitutes a literal compiled into its own image
(`global.anthropic.claude-opus-5`), and `keda.tf` sets no `ANTHROPIC_MODEL`, so that in-image
fallback is **live** on the ScaledJob path today (§8.1). Give that worker a table and a
fallback rule and the failure mode is not a rejected snapshot — it is a run that reports success
having invoked a model nobody chose, which is AC-07's core failure and the #4511 inert-config
class. The only structural fix is to not give the worker a decision to make.

**The split.** Selection happens behind the trusted boundary; the worker receives a *result*.

**What the gateway does**, at the bootstrap call each hop already makes (§4.2a), for that hop's
own persona:

1. Resolve **this hop's** `harness_compatibility_class` and judge the persona against the
   snapshot's `harness_compatibility_revision` (**D6**, **U2**) — separate fields because U2
   rules they are separate things (§2.7).
2. Select from the snapshot **it read back from the protected record** — not from anything the
   caller presented: `mappings[persona]` → `source = principal-mapping`;
   absent but valid in the signed catalogue → the `system_default_model_id` entry **for this
   hop's class**, `source = system-default` (AC-07 first half). **No cross-class fallback** — an
   absent entry for this class is a refusal, not an occasion to borrow another class's default.
   Not valid for the harness → explicit compatibility error, no fallback (AC-07 second half).
3. **Admit, live.** Check the selected model against the *current* allowlist, destination
   invocability, budget and rate limits (D1's admission gates, D3's freshly-proven requirement).
   The snapshot supplies the **choice**; it never supplies the **permission** (§2.6b). A refusal
   here is a live-policy refusal and must not be reported as a snapshot defect.
4. Return a **signed model decision** naming `persona`, the `resolved_model_id`, the
   `resolution_source`, the destination, the `snapshot_digest` it was resolved from, and both the
   snapshot's recorded policy revisions and the live revisions actually applied. The `adpe1`
   assertion's `body_digest` covers the decision bytes, and the decision names the
   `snapshot_digest` so the two cannot be recombined across runs.

**What the worker does — and this is the whole of it:**

1. Verify the decision: signature, `alg` allowlist, `iss`, `aud`, expiry/`nbf`, run and tenant
   binding; the received bytes hash to the assertion's `body_digest`; the named
   `snapshot_digest` matches the snapshot the same response delivered; `schema_version`
   understood (§3.3).
2. Invoke the named model, or fail with the decision's reason. **Nothing else.**

**The worker contains no resolver.** No mapping lookup, no class-keyed default, no precedence
rule, no in-image fallback. This is a contract, and three consequences follow that a developer
must implement rather than infer:

- **The in-image fallback must be closed on this path**, not merely left unused.
  `entrypoint.py:1578`'s `or os.environ.get("ANTHROPIC_MODEL", "global.anthropic.claude-opus-5")`
  is precisely the line that would silently outvote a gateway decision when verification fails or
  a field is missing. A missing or unverifiable decision must refuse, never substitute. Test that
  the refusal path does **not** reach the literal — and note the test must assert the literal was
  not used rather than that the right model was used, because a test where the decision and the
  fallback happen to agree proves nothing (§8.1).
- **A hop that has not bootstrapped has no decision and may not proceed.** There is nothing in
  the envelope to fall back to (§4.1a) and it must not manufacture one.
- **The one place precedence is implemented is gateway-side.** The story asked for a shared
  selection function and was right to: the platform already has two copies of model logic that
  drifted — `model_validate.py` versus the gateway's `proxy/model_resolver.py`, which #5418
  records as having diverged on both an alias and the permitted-family list. The correction to
  the story is that the shared function must live **in the gateway**, where the worker cannot
  reimplement or bypass it, rather than in a library the worker also links.

Step 3's live admission stays a separate surface from steps 1–2, because admission is PMM-07's
enforcement surface across every path and folding it into the selection helper would let a caller
that skipped the helper also skip the gate. Both are now gateway-side, so that separation is
internal to the trusted component rather than a boundary the worker straddles.

**Two implementation facts this leans on, stated precisely.** First, `issue_bound_credential`
(`bootstrap.py:372-388`) returns `credential`, `invocation_id`, `attempt`, `credential_epoch` and
`expires_in` — it does **not** carry a model decision today. So PMM-06 adds fields to that
response; this note does not claim the channel already exists.

Second — and this one is sharper than an earlier revision of this note implied — **the bootstrap
response is not signed with the `adpe1` Ed25519 signer at all today.** `issue_bound_credential`
calls `mint_credential` (`bootstrap.py:373`, imported at `:13`), which produces the **symmetric
`adpr1` HMAC-SHA256** run credential (`run_credential.py:61`, `:215-216`) — deliberately symmetric,
because *"no worker ever holds this key"* (`run_credential.py:25-28`, which names
`agentauth/envelope.py` as the asymmetric one precisely because workers *do* verify those). Neither
`routes.py` nor `bootstrap.py` calls `sign_envelope` on any path. So returning a **worker-verifiable
signed decision** means reaching the `adpe1` signer from a response that has never used it. That is
the same extension U4 already mandates (an audience constant plus a chain-binding claim on both
sides of the golden vectors, §3.3), reached from a second direction — and PMM-07's head records it
as its own unmet prerequisite P4′. It is owned and ruled, but it is **not built**, and this note
states it as new work rather than existing plumbing.

What already exists is the part
that is expensive to build and easy to get wrong: the response is only issued to a workload whose
run and root binding the gateway has just verified (`routes.py:250`, `:258`, `bootstrap.py:305-306`,
`:318-324`, `:350-351`), it is `Cache-Control: no-store` (`routes.py:293`), and the gateway already
resolves the root principal server-side rather than from worker input on the model path
(`model_identity.py:71-74`, which reads `grant.authority.human_id` from the live grant and resolves
it through `resolve_root_user_entity_id`, never from a header — and at `:67-70` refuses a worker
whose asserted run or tenant headers disagree with its verified credential). Adding a decision to a response that already
has those properties is a smaller change than building a trusted selection point.

### 4.4 The one-run override does not descend

Today non-inheritance is an accident: `DispatchRequest` forbids extra fields and has no model
field (`dispatch.py:88-93`), so a child physically cannot request a model. D2 makes it a rule —
a valid `/model` is an audited override *"for the directly invoked hop only"* and *"every child
hop continues to resolve its target persona from the trusted root principal policy snapshot."*

**Direction.** The override stays in `model_requested`/`model_resolved`, which are *already*
conditionally set per-dispatch (`spawn_persona.py:596-599`) and never copied into the snapshot.
The rule to make explicit and test: **snapshot construction never reads
`model_requested`/`model_resolved`, and propagation never copies them to a child.** AC-08 tests
the rule, not the accident — so that a future refactor adding a model field to
`DispatchRequest` fails a test instead of silently leaking a model to every descendant.

D2 also reverses the `/model` leniency at `handler.py:1786-1789`. That reversal belongs to
PMM-07, not here; PMM-06 must simply not depend on the lenient behaviour. Note the coupling
D2 states: the reversal requires #2293's feedback channel, or a user gets a bare refusal with
no reason.

---

## 5. Latency: the ten-second budget

The constraint is real. #2279 ruling 4 forbade a gateway HTTP call from the webhook Lambda
because it must answer GitHub within ten seconds, and that ban is why two alias maps exist.
It is quantified by the client itself: `gateway_client.py` uses `timeout=10` at `:138, :268,
:376, :611` — **a single unlucky gateway call can consume the entire budget.**

**Read this section with §5.1.** An earlier draft of this note answered the budget constraint
by having the **Lambda** resolve preferences for itself out of a DynamoDB projection. That
direction is **withdrawn** — the operator review on PR #5442 rejected it and §5.1 carries the
design that replaced it. The paragraphs below are kept because the *constraint* they describe is
real and still governs, but the conclusion they originally led to no longer applies. Where this
section and §5.1 could be read as disagreeing, **§5.1 governs.**

**What the constraint actually rules out.** #5419 (PMM-02) is scoped to *"Alembic migration;
SQLAlchemy model"* — i.e. **Postgres**, which is the correct storage choice for a join-heavy,
admin-UI-queried preference table (and, per §5.1, PMM-06 asks it for no projection). The webhook
Lambda has no Postgres route. So the following is genuinely forbidden and a developer must not
reintroduce it:

- The Lambda **must not** add a *new* synchronous gateway HTTP call in order to read
  preferences. That is precisely the call #2279 banned, and one such call can consume the whole
  ten-second budget.

**What the constraint does not rule out**, and this is the distinction the earlier draft missed:
the Lambda **already makes** a fail-closed authenticated gateway call on this path before it
publishes anything (`sqs_publisher.py:53-66`). Resolving preferences *inside a call already on
the path* adds no round trip. That is why §5.1's gateway-side resolution is not a violation of
#2279 while a preference-reading HTTP call would be. The digest remains local computation —
SHA-256 over a canonical JSON serialization (`agent_authority._digest`,
`agent_authority.py:67-72`).

**Why the projection was the wrong answer**, recorded so it is not proposed again: a projection
makes the preference data **two homes for one fact**, and puts the copy the enforcement path
actually reads in the component least able to be trusted with it. Dual-write skew between
Postgres and the projection is not hypothetical — it is the same desync class #4337 D4c prohibits
by construction (§3.2), except here a stale copy does not merely misreport, it **selects a model
nobody chose** and does so under a snapshot that is digest-bound and therefore looks
authoritative at every downstream hop. It also widens what the Lambda is trusted to resolve, in
the opposite direction from the platform's travel, which is to move authority resolution *behind*
the gateway.

#### 5.1 The trusted path already exists: gateway-side resolution at work admission

There is no need for the Lambda to read preferences at all. When authority and work claims are
enabled, the webhook Lambda **already makes a fail-closed, authenticated gateway call before it
publishes anything**, and that call already runs inside the gateway, where Postgres is an
ordinary query:

| Step | Location | What it establishes |
|---|---|---|
| Publication is gated on admission | `sqs_publisher.py:53-66` | With `ADP_WORK_CLAIMS_ENABLED=true`, `publish_envelope` calls `admit_issue_work(envelope)` and **returns `None` — nothing published — unless it succeeds**. Also requires `AGENT_AUTHORITY_ENABLED=true` (`:59-61`). Fail-closed, and it runs *before* `json.dumps` at `:67` and before `send_message` |
| The call is authenticated by role, not by claim | `gateway_client.py:300-385`; `work_routes.py:41-66` | SigV4 `GetCallerIdentity` proof with the invocation bound into the **signed** headers (`:52`), verified against `ADP_WORK_CLAIM_PRODUCER_ROLES` (`:63`). HTTPS-only, no proxy, no redirect (`gateway_client.py:367-375`) |
| The producer cannot supply authority data | `work_routes.py:36-38` | `WorkAdmissionRequest` is `extra="forbid"` with **one** field, `invocation_id`. `test_work_producer.py:117-124` asserts a 422 for `org_id`, `owner_ref`, `issue_number`, `generation`, `force_handover` |
| The gateway resolves everything itself | `work_admission.py:99-105` | Docstring: *"Resolve every authority field from protected dispatch, not HTTP input… It cannot manufacture a pending execution, select a tenant/issue/owner, release someone else's claim or request a handover."* It reads the protected dispatch pointer and execution row (`:109-115`) and the live grant (`:116`) |
| Postgres is already in hand here | `work_admission.py:72-96`, `:173-176` | `admit` runs `claim_work` **on a SQLAlchemy session**, from the gateway's own session factory. A preference read is one more query in a transaction this path already opens |
| A receipt already flows back | `work_admission.py:96`; `gateway_client.py:377-382` | The endpoint returns `{claim_id, generation, invocation_id, disposition}` and the Lambda already parses it and branches on `disposition` |

So the mapping resolution belongs **here**, not in the Lambda:

1. The Lambda calls `/work/admit` as it does today, passing only `invocation_id` — **unchanged**.
2. The gateway — having independently resolved tenant, principal and grant from the protected
   dispatch record — reads the canonical PMM-02 **Postgres** rows for that principal, builds the
   snapshot, computes `snapshot_digest`, and **persists both on the protected execution row for
   that invocation**, in the table whose docstring states *"The worker role has no write
   permission on this table"* (`agent_authority.py:1-7`).
3. **The envelope is not touched, and the receipt is not a transport** (§4.1a). The envelope was
   already sealed before admission ran, so the snapshot is keyed on `message_id` — already inside
   the sealed envelope (`agent_authority.py:161`) and already the one value admission receives
   (`work_routes.py:36-38`). The receipt may report *whether* a snapshot was persisted, for
   report-only observability; nothing on it enters the envelope.
4. Each hop obtains the contents from `/internal/v1/agent/self/bootstrap`, after the gateway has
   verified that workload's run and root binding, with a signed assertion over the delivered
   bytes (§4.2a).

This inverts the trust direction in the right way. Under the withdrawn projection design the
Lambda read preferences and the envelope carried the authoritative copy; here the **gateway**
resolves from the canonical store, the queue message carries nothing at all, and the contents are
released only to a workload the gateway has proven is bound to the run. It also disposes of the
§7.1a hazards for the snapshot at the source: no snapshot value is read through `boto3.resource`
before hashing, and none enters the envelope, so neither the `Decimal` digest trap nor the 256KB
ceiling applies to it (§7.1a).

**Latency.** This adds **no new synchronous call to the webhook path** — it adds work inside a
call already on it, whose client budget is already `timeout=10` (`gateway_client.py:376`) and
which already performs two DynamoDB reads, a live-grant read and a Postgres claim transaction.
The added cost is one indexed Postgres read per root dispatch. AC-10 must measure it as the
p50/p99 delta on `/work/admit`, not on the Lambda as a whole.

**Two consequences to carry (§9, C3).**

- **Only root dispatch needs this.** A non-root hop reuses the root's snapshot **verbatim, resolved
  server-side from its chain rather than copied by its parent** (§4.2b) and resolves nothing, so no
  preference read is added at any depth.
- **GitLab is closed by explicit refusal, not left open.** The channel is excluded from admission
  (`sqs_publisher.py:55`, `channel != "gitlab"`) and, per PMM-07's corrected finding (§2.4a), it
  also never reaches `spawn_persona` — its handler hardcodes the model fields to `None`
  (`gitlab/handler.py:172-173`) and calls `publish_envelope` directly (`:229`). It therefore has
  **no trusted resolution point**, and the second-pass review requires this be settled rather than
  deferred. **The ruling this note adopts: GitLab gets no snapshot in PMM-06, and a GitLab-channel
  dispatch that would require one is refused with a distinct reason
  (`snapshot_unavailable_channel`) — never defaulted.** That keeps the story's core property
  intact: no path silently selects a model nobody chose. Under report-only the refusal is recorded
  and nothing is blocked, so this changes no GitLab behaviour today; it becomes a real refusal only
  at PMM-09's enforcing flip, which is the correct place to decide whether GitLab is in scope for
  enforcement at all. Routing GitLab through admission is the durable fix and is **out of scope
  here** — it is a change to that channel's publish path, and it belongs to PMM-07's resolver
  contract (§8.2). Recorded as a named follow-up, not an unresolved hole in this design.

**AC-10 evidence.** The story asks for a measured figure in the PR, which is right. It should
be measured as added p50/p99 on the dispatch path with the feature enabled versus disabled,
stated in milliseconds against the ten-second ceiling — not as a total that hides the delta.

---

## 6. Failure behaviour and the adversarial matrix

Every rejection gets a **distinct reason**, extending the vocabulary already documented at
`agent_trigger.py:27-36` rather than inventing a parallel one. Distinctness is the actual
acceptance criterion in AC-05 — a single generic `snapshot_invalid` would pass a naive test
while making the failures indistinguishable in production.

| Attack / condition | Reason code | Behaviour |
|---|---|---|
| Snapshot absent where required | `snapshot_missing` | Reject (report-only: record) |
| **Decision absent, expired or unverifiable at the worker** | `decision_unavailable` | **Refuse the run.** Added by C10: since selection is gateway-side, a worker in this state has no resolver and no table to improvise from, so the *only* correct branch is refusal. Test that it does **not** reach `entrypoint.py:1578`'s literal, and assert the literal was not used rather than that the right model was used (§4.3, §8.1) |
| **Decision valid, but `snapshot_digest` names a snapshot other than the one the same response delivered** | `decision_snapshot_mismatch` | Reject. Distinct from `snapshot_altered`: nothing was tampered with, but the decision and its evidence were recombined across runs |
| Digest mismatch / altered body | `snapshot_altered` | Reject |
| `policy_revision` tampered, body otherwise valid | `snapshot_revision_mismatch` | Reject — distinct from `snapshot_altered`, per AC-05's four-way split |
| `tenant_id` ≠ hop tenant | `cross_tenant` | Reject, reusing the existing code |
| `correlation_id` / `root_invocation_id` ≠ this chain (valid snapshot, replayed) | `snapshot_chain_mismatch` | Reject |
| `expires_at` passed | `snapshot_expired` | Reject |
| `audience` mismatch | `snapshot_audience_mismatch` | Reject |
| Forged root marker attempting to mint a snapshot | `unverified_provenance` | Reject, reusing the existing code. Server-side re-resolution (§4.1) is the actual defence |
| Schema version newer than the reader | `snapshot_unsupported_revision` | **Legacy path only**: run, and report the ignore (AC-12) |
| Persona valid in catalogue, absent from `mappings` | — | Not a failure: system default, `source = system-default` |
| Persona not valid for the harness | `harness_incompatible` | Reject with explicit compatibility error (AC-07, D6) |
| Mapped model unusable / retired | `model_unavailable` | Reject **before billable work** (AC-09) |
| Snapshot intact, but the model was **revoked from the allowlist mid-chain** (live check now refuses what hop 1 admitted) | `model_unavailable` / `harness_incompatible` — **never** `snapshot_altered` | Reject at that hop. Per §2.6b this is correct behaviour, not corruption: the snapshot froze the choice, not the permission. Report the snapshot's recorded `allowlist_policy_revision` beside the live one so the refusal is explicable. Misreporting this as a snapshot defect sends an operator hunting tampering that did not occur |

On the per-hop assertion leg (§3.3 Mechanism A), the existing verifier already supplies the
codes and this story must not duplicate them — but three cases deserve explicit tests because
they are the ones a naive reuse gets wrong:

| Attack / condition | Expected behaviour |
|---|---|
| Assertion `alg` switched to `none`, `hs256`, or an unlisted value | Refused by the `ALLOWED_ALGORITHMS` allowlist *before* any verification work — the allowlist is checked first by construction, so `alg`-confusion has no foothold. Assert it rather than assuming it |
| Assertion signed under a **placeholder or absent** key | Refused, not trusted. `marker_verify.py:120-122` is the precedent: a rotation *from* a placeholder must not accept the placeholder as a valid previous key |
| Assertion `body_digest` ≠ the digest of the decision it is presented with | Reject as `decision_altered`. Without this check the assertion proves only that *some* decision was signed, not *this* one — the substitution attack that makes the whole Mechanism A leg decorative. The decision's own `snapshot_digest` field is covered by the same signature, so binding the assertion to the decision binds it to the snapshot transitively; binding it to the snapshot alone would leave the chosen `resolved_model_id` unsigned |

**Never** on any of these: fall back to a default, to a container-image literal, or to another
principal's decision. A worker that cannot verify its decision has no selection logic to fall
back *to* (§4.3), which is the point: refusal is the only reachable branch.

**Cache contract (AC-11).** A bounded last-known-good snapshot is usable at root **only** when
binding, revision and freshness all pass. Its key must include tenant **and**
principal — a tenant-B key probe must neither read nor write a tenant-A value, and the test
must assert the *write* side too, which AC-06 correctly demands. Two rules the existing
`negative_cache.py` establishes and this cache must copy: **validate freshness on read**
rather than trusting row presence (`negative_cache.py:40-49`, because DDB TTL deletion is
asynchronous and expired rows are returned), and **never cache the error state**
(`negative_cache.py:33-38`, because caching "we could not find out" turns a transient outage
into a TTL-long lockout). A stale or unverifiable cached snapshot fails before billing — never
a guess. `source = last_known_good_cache` makes the degraded path visible in audit.

**AC-09's honest limit.** The story already flags this: "fails actionably" needs a channel to
be actionable *in*, and #2293 is open with `ADP_MODEL_RESOLVED` exported at
`entrypoint.py:1686-1687` and no consumer anywhere. PMM-06 can make dispatch fail with a
correct reason; it cannot make the requester *see* it. Record the gap against #2293 rather
than claiming AC-09 complete.

---

## 7. Compatibility, deployment, rollback

### 7.1 Legacy workers (AC-12)

A snapshot-bearing envelope must not break a worker that predates it. The tree's evidence is
reassuring: the worker reads the envelope as a plain dict with `.get()`
(`entrypoint.py:1576`: `envelope.get("model_resolved")`), so an unknown top-level key is
ignored rather than fatal. There is no strict schema on the worker's envelope parse — unlike
`DispatchRequest`, which is `extra="forbid"` but governs an **inbound API request**, not the
queue message.

So AC-12's first half ("the worker still runs") holds **for the ordinary path** by
construction. Its second half ("and reports that it ignored an unsupported policy revision")
requires a small addition to the worker: on seeing a `schema_version` it does not understand,
log and report `snapshot_unsupported_revision`. That report lands in the same place #2293
would land, which is the second reason §9 keeps #2293 visible as a dependency.

### 7.1a The whole-envelope digest — why this story now adds nothing to the envelope

**Mostly retired by §4.1a, and recorded because it is the reason for that design.** This section
originally treated the whole-envelope digest as a deploy-ordering hazard to be managed while adding
a snapshot key. §4.1a establishes that the key cannot be added at all — the envelope is sealed
before admission runs — so **PMM-06 changes the envelope's key set not at all**, and the hazards
below stop applying to the snapshot. What survives is the *reason*, plus one general rule for the
Lambda (item (a)), and a contract test asserting the key set is unchanged.

`RunIdentitySession` hashes the **entire** envelope
(`modules/agent-factory/agent-worker-image/lib/run_identity.py:69-71`):

```python
self._digest = hashlib.sha256(
    json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
).hexdigest()
```

and the gateway compares that digest against its protected dispatch record
(`bootstrap.py:305`). So on the work-claim path — gated by
`envelope.get("work_claim_required") is True` or `ADP_WORK_CLAIMS_ENABLED`
(`run_identity.py:181-195`; the producer sets the flag at `spawn_persona.py:614-615`) — **every
byte of the envelope is significant.** Any key present in the queued message but absent from
the record the gateway stored, or vice versa, changes the digest and fails run-identity
bootstrap.

**Consequence, as now resolved.** An earlier draft answered this by requiring the snapshot to enter
the envelope before `provision_*` computes the stored digest. §4.1a shows that is not reachable:
admission — the only place with the trusted data to build a snapshot — runs *after* that digest is
written. The resolution is to add nothing to the envelope and key the snapshot on `message_id`,
which is already inside the sealed envelope. **This hazard is then closed by construction rather
than managed**, which is the main reason §4.1a is designed the way it is.

**Deploy order is not the control that closes this, and an earlier draft of this note said it
was.** On the webhook path the **Lambda**, not the gateway, computes *and* stores the digest —
`agent_authority._digest` at `:67-72`, computed at `:170`, stored at `:183` (service path:
`service_authority.py:125, 138`) — inside the same conditional transaction that queues the run.
The gateway only *compares* (`bootstrap.py:305`). Producer and record are therefore
self-consistent per invocation regardless of gateway version, so deploying the gateway first buys
nothing against the digest. Draining the queue across a rollback is still good practice —
an in-flight message outlives the code that made it — but the two failure modes that actually
bite are serialization ones, and neither is neutralized by report-only:

**(a) `Decimal` canonicalization — a total dispatch outage with a misleading reason code.**
**No longer reachable via the snapshot**, and retained as a general rule. Two changes remove the
routes into it: §5.1 resolves snapshot values from Postgres inside the gateway rather than through
`boto3.resource`, and §4.1a puts no snapshot value in the envelope at all, so none is ever hashed
by `_digest`. It stays recorded because the Lambda still reads protected records on this path
(`spawn_persona.py:644` uses `boto3.resource("dynamodb")`, whose deserializer returns **`Decimal`**
for every number), so the rule "never place a raw DynamoDB-deserialized number in the envelope"
still binds any *future* envelope field — and the failure mode is severe enough to be worth
knowing. `_digest` uses a plain `json.dumps` with no `default=`
(`agent_authority.py:67-72`), so a `Decimal` raises
`TypeError: Object of type Decimal is not JSON serializable` — and that `TypeError` is caught by
the broad handler at `agent_authority.py:303` and collapsed into
`AuthorityProvisionError("protected authority unavailable")`, which `spawn_persona.py:249-250`
returns as `authority_provision_failed`. **Every dispatch fails, with a reason code that names
nothing about model policy.** Worse, if the digest ever succeeded, `publish_envelope` serializes
with `json.dumps(..., default=str)` (`sqs_publisher.py:67`), putting `"3"` on the wire where the
digest covered `Decimal('3')` — a guaranteed refusal at `bootstrap.py:305`. The repo already
documents exactly this trap and its fix: `webhook_events.py:82-87` stores its signed tuple as a
canonical JSON **string** precisely because a DynamoDB map *"would round-trip every number
through `Decimal`… silent numeric drift in the one value the signature must reproduce
byte-for-byte."*

**(b) Envelope size — retired.** `prepare_envelope` truncates `payload` wholesale past 256KB and
raises `ValueError` if still over (`sqs_publisher.py:109-121`), collapsing to the same
`authority_provision_failed`. Since §4.1a adds nothing to the envelope, PMM-06 moves envelopes no
closer to that ceiling. This was a second, independent reason not to carry mappings in the message.

**Direction, as it now stands.**

- **Assert the envelope key set is unchanged** by this story, with a contract test comparing the
  built envelope against the pre-change key set. This is the guard that keeps §4.1a's property
  true as the code evolves, and it replaces the earlier "add the snapshot before the digest" test.
- **Keep the coercion rule for any future envelope field**: coerce to `str`/`int` before it enters
  the envelope, mirroring `webhook_events.py:82-87`'s canonical-string approach. Test it against
  `_digest` and the gateway comparison if such a field is ever added — not required by PMM-06.
- **Keep the queue drain** as ordinary practice on any rollback (an in-flight message outlives the
  code that made it), but do not present it as a digest mitigation, and note that PMM-06 changes no
  Lambda code at all — the components it touches are the gateway and the worker image (§7.3).

**What this does to the report-only qualifier.** The earlier draft warned that report-only could
not make snapshot *presence* neutral, because the digest and size paths were upstream of any
selection decision. With the envelope untouched, that is no longer true on those two paths: nothing
upstream of selection changes. The qualifier §7.2 still needs is narrower and real — snapshot
*persistence* happens inside `/work/admit`, so a failure there is upstream of selection and is
**not** neutralized by report-only. It must therefore be fail-soft for the snapshot specifically:
a snapshot that cannot be built or stored must not turn an otherwise-admissible dispatch into a
refusal while the feature is report-only.

### 7.2 Report-only rollout

Ship verification in report-only: compute the decision, record it, refuse nothing. The reason
is proportionate to the blast radius the story itself identifies — *"a defect here selects the
wrong principal's policy or blocks dispatch entirely."* A verification bug in enforcing mode
is a platform-wide dispatch outage. The CLAUDE.md troubleshooting entry for the fail-closed
broker deploy is the precedent for what that class of outage costs.

**Report-only must mean exactly one thing: no selection decision changes behaviour.** Not
"enforce for some personas", not "enforce when the snapshot is present". Otherwise rollback
is not a true no-op and the story's own recovery claim stops being true.

The enforcing flip is PMM-09's (#5427) and is gated on #3186/#5195 per D5 — and per PMM-09's own S4
at its current head, on **#2293 as well**, since S4 requires both the authority path and actionable
requester feedback (`5427…:1080`, §2.8). A third condition falls out of PMM-03's rev-3 rather than
from any ruling: with probing shipped disabled and zero-spend, **no model is certified invocable**
until PMM-09 records one (`5420…:19`), so an enforcing build would correctly refuse every hop for
want of evidence. Three independent reasons, one conclusion. **Merging PMM-06 does not authorize the
flip.**

### 7.3 Deployment and rollback

**Two components change, and the webhook Lambda is not one of them.** This is a consequence of
§4.1a/§5.1 that an earlier revision of this section had backwards. The **gateway** changes most —
it resolves, persists, selects and returns a signed decision (§4.3) — and the **agent worker image**
changes in the narrower way §4.3 now requires: it verifies the decision and invokes the named model,
and its in-image model fallback is closed on this path so it cannot outvote that decision. It gains
no selection logic. The webhook-ingress
Lambda calls `/work/admit` exactly as it does today, passing only `invocation_id` (§5.1 step 1), and
it emits no snapshot — so this story asks no code change of it. Deploy dev first: the gateway via
its own pipeline, the worker image via
`modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh`, which builds the
agent-runtime image and applies Terraform and which CLAUDE.md records as **not** covered by
`deploy-all.sh`.

**Order matters, in one direction only.** The gateway must ship before the worker image, because a
worker that expects a snapshot on the bootstrap response must not meet a gateway that does not
return one. The reverse order is safe by construction: a current worker ignores an unrecognized
response field. No new identity and no IAM broadening — the gateway already reads Postgres and
already writes the authority record on this path, and the Lambda's grants do **not** widen (§5.1
*removes* a preference read from the Lambda rather than adding one). Any authority-table grant added
for snapshot persistence must be scoped by table ARN, not `Resource: "*"`.

**Rollback is the prior gateway and worker image, and it is a genuine no-op.** Because report-only
makes no selection decision, rollback restores current behaviour exactly. Two qualifications earlier
revisions of this section carried are now **removed by §4.1a**: there are no snapshot-bearing
messages to strand, because nothing about the snapshot ever enters the envelope, so no queued
message depends on the rolled-back code; and there is no "gate snapshot *emission* behind its own
flag" work item, because there is no emission. Draining the queue during a worker rollback remains
ordinary practice — an in-flight message outlives the code that made it — but it is **not** a
mitigation for anything in this story (§7.1a). Snapshots left persisted on protected rows after a
rollback are inert: nothing reads them and they carry no authorization (§4.2a). The one rollback
property this design does depend on is C6's fail-soft rule — under report-only, a snapshot that
cannot be built or stored must never turn an admissible dispatch into a refusal.

---

## 8. Dependencies and parallelism

| Dependency | State (verified 2026-09-18) | Effect on PMM-06 |
|---|---|---|
| **#5417 architecture synthesis gate** | **OPEN — added to #5417 at 2026-09-18T13:43:58Z, after this story was filed** | **Hard prerequisite for dispatching any developer.** The epic operator will *"review all outputs together and publish one unified, versioned design"*, and *"a merged canonical design and an operator approval comment are hard prerequisites for the PMM-02/PMM-03 developer wave."* Story-local notes *"may not override the canonical design silently"* — so this note is an **input** to that synthesis, not an authority over it. See §8.2 for which of its conditions belong to the synthesis |
| #5418 PMM-01 decisions D1–D6 | **Locked** in issue comments | Available now. Sufficient to design against. Reconciled decision-by-decision in §2.6 |
| #5418's design-note artifact | **Not on `origin/main`**; present unmerged at `agent/issue-5418` @ `b8045dbf`, rev-5 (§2.5, §2.8) | Cannot be cited as merged authority. Reconciled at its current head in §2.8: its freeze/live split matches §2.6b (`5417…:466`, `:506`), its whole-map freeze is adopted into C7 (`:492`), and its still-`principal_kind`-keyed mapping key (`:21`) lags PMM-02's contract |
| #5419 PMM-02 preference storage | OPEN, not built; head `e2c7d099` (§2.8) | Blocks snapshot *contents* being real. Postgres-only is **correct** for PMM-06 (§5.1); its head has **withdrawn** the DynamoDB projection, so §5.1 agrees with it. What PMM-06 needs is the **canonical-alias contract** (C1, U1), which its head now owns and specifies: `canonical_service_principal_id` opaque and immutable (`5419…:545`), aliases keyed `(org_id, alias_source, alias_id)` per U1 (`:551-554`). §3.2's corrected `service_policy` row reads exactly that field. **Not built** — a delivery dependency, no longer a contract gap |
| #5420 PMM-03 catalogue + invocability | OPEN, not built; head `25ece717`, **rev-3** (§2.8) | **Both gaps the previous revision recorded here are closed.** Rev-3 supplies the U2 class vocabulary — persona rows carry `compatibility_class`, stable unversioned IDs `claude-agent-sdk`/`codex-sdk`, separate `harness_contract_revision`, *"No cross-class fallback, ever"* (`5420…:100-104`, `:336`) — and names PMM-06 as the consumer (`:173`). It now treats Sonnet 4.6 as a **candidate** and records *"no default at all"* (`:106`, `:156`), and it adopts U3's zero-spend disabled probe (`:19`). **The new dependency state:** because a bounded invocation is the only admissible proof and probing ships disabled, *"no model is yet certified invocable"* at PMM-03's completion (`:19`) — so the gateway's live admission (§4.3, gateway step 3) has **no passing evidence to read** until PMM-09 probes. Correct fail-closed behaviour, and a second independent reason the enforcing flip is PMM-09's |
| #5425 PMM-07 resolver wiring | OPEN, not built; head `1d799351`, fourth pass (§2.8) | **Corroborates and corrects.** Its Q5′ closes as *"a gateway-authoritative decision before the harness step"* (`5425…:1557`), reaching §4.3's conclusion on the ARC path independently. Its P4′ supplies the correction §4.3 now carries: bootstrap returns `adpr1` HMAC, *"**not** the `adpe1` Ed25519 assertion U4 describes"* (`:257`) — verified at `ae598410`. Not a blocker on PMM-06's merge; it is the story that consumes the decision on the four non-webhook paths |
| #3186 authority enforcement flip | OPEN, *"DO NOT TRIGGER YET"* | Blocks **enforcement**, not this story's merge |
| #5195 worker credential isolation | OPEN | Same — blocks enforcement |
| #2293 worker feedback channel | OPEN, exported vars have no consumer | Limits AC-09; also needed for AC-12's report half. **And it is now half of the enforcing gate:** PMM-09's S4 requires *"PMM-06 authority/bootstrap … live **and** PMM-07 delivers actionable requester feedback (#2293 behaviour)"* (`5427…:1080`), so the gate is two-part. PMM-06 needs nothing from the second half but must not describe the gate as single-condition |
| #4673 unsafe default model | OPEN | `entrypoint.py:1578` still hard-codes `global.anthropic.claude-opus-5`. D4 replaces it with `us.anthropic.claude-sonnet-4-6`. PMM-09's, not PMM-06's — but see §8.1 |
| Control-envelope signer + worker verifier + two-slot rotation | **Built and live** (§3.3 Mechanism A) | An asset, not a blocker. The per-hop leg reuses it rather than building a signing service |
| `routes.py:236-247` forward path | **Dormant by design** — mints, then raises `PolicyError` 501 | Blocks only the *per-hop assertion* leg end-to-end. Minting is testable now; creation-side binding, the snapshot schema, its persistence on the protected record, and the whole §6 matrix are unaffected. Sequence the per-hop leg behind this route, not behind the signer. **Do not remove the 501 in PMM-06** |

### 8.1 Why the snapshot's `system_default_model_id` must be authoritative, not advisory

The epic asserts defaults "can drift by execution path". A sweep of the tree confirms it and
quantifies it: **at least eight distinct model identifiers are used as a default or fallback**,
including `global.anthropic.claude-opus-5` (`entrypoint.py:1578`,
`agent/src/agent-worker.ts:127`, `agent/src/components/ConfigLoader.ts:19`),
`global.anthropic.claude-opus-4-6-v1` (nine agent workflow YAMLs, e.g.
`.github/workflows/agent-architect.yml:197`), `us.anthropic.claude-sonnet-4-6`
(`modules/agent-factory/infra/gateway-main.tf:477`), `global.anthropic.claude-sonnet-4-6`
(`agent/k8s/chat-scaledjob.yaml:41`), `claude-sonnet-4-5-20250929` (six agent TypeScript
sites), and `us.anthropic.claude-sonnet-4-20250514-v1:0` — an identifier
`model_validate.py:34` itself records as legacy/non-invocable, still present in onboarding
templates. Notably `webhook-ingress/infra/keda.tf` sets no `ANTHROPIC_MODEL`, so the
worker's hard-coded `global.anthropic.claude-opus-5` is **live** on the ScaledJob path — which
is the #4673 silent-hang defect.

**Consequence for PMM-06.** Consolidating these is explicitly PMM-09's (#5427). But it means
the snapshot's `system_default_model_id` must be treated by every hop as **the** default, and
a hop must never fall back to its local literal when the snapshot names a default. Otherwise
the snapshot is advisory and the drift survives underneath it. AC-07's "uses the snapshot's
system default and records that source" is therefore also a test that the local fallback was
**not** used — worth asserting explicitly, because a test where the two happen to agree proves
nothing.

**What can run in parallel now:**

- The snapshot **schema, digest binding, its persistence on the protected record, and the full
  adversarial rejection matrix** (§6) can be built and tested immediately against a fixture
  mappings provider. None of it needs PMM-02's real table. Note there is no "envelope carriage"
  work item: §4.1a removes it, and the **envelope-key-set-unchanged** contract test (C6) replaces
  it.
- The **gateway-side selection function and the signed decision it returns** (§4.3) can be built
  against the same fixture. There is no companion worker-side work item: the worker's share of
  §4.3 is verify-and-obey, and the only worker change is a *removal* — closing
  `entrypoint.py:1578`'s in-image fallback so an unverifiable decision cannot be silently
  substituted.
- The **legacy-worker ignore-and-report path** (§7.1) is independent of everything else.
- The **canonical alias contract** for service principals (§5.1, C1) should be agreed with
  PMM-02 *now*. This no longer changes PMM-02's storage scope — Postgres-only is correct — but
  PMM-06 must resolve a service principal through PMM-02's identifier rather than inventing one,
  and discovering a mismatch after PMM-02 merges means reopening it.
- The **`/work/admit` resolution point** (§5.1) can be built against a fixture preference
  provider before PMM-02's table exists, because the gateway-side seam is already there.
- The **per-hop assertion leg's unit and golden-vector layer** (§3.3 Mechanism A) can be built
  now against the existing signer and verifier, including the three §6 assertion tests. Only its
  end-to-end hop waits on `routes.py`.

**What cannot start:** end-to-end AC-01/02/03 against real stored mappings (needs PMM-02);
AC-07's catalogue-validity half (needs PMM-03); the per-hop assertion leg end-to-end (needs the
dormant forward route); any enforcement (needs #3186/#5195).

**And one process prerequisite that outranks all of the above:** per the synthesis gate, no
developer is dispatched on PMM-06 until the epic operator publishes the unified canonical design
and an approval comment. The parallel work listed above is what becomes *available* once that
clears — it is not authorization to begin now.

### 8.2 What this note contributes to the #5417 synthesis

The synthesis gate names eight contracts it must reconcile. PMM-06 is the primary input to one of
them and a consumer of four. Separating these tells the operator what they must rule on centrally
versus what a developer can execute from this note alone.

| Synthesis contract | PMM-06's relationship | What this note contributes |
|---|---|---|
| **One trusted-root snapshot format, signing authority, revision model and cache behaviour** | **Primary author** — this is PMM-06's contract | §3.1 field schema; §3.3 the two-part integrity model (protected-record binding at creation + short-lived per-hop assertions delivered at bootstrap); §4.1a the admission protocol and why the envelope cannot carry the snapshot; §4.2a how a hop authentically obtains the contents; §3.4 rotation; §6 cache contract. **C2 and C7 are now settled by rulings U4 and U2, so this contract has no open ruling from PMM-06** — the synthesis should adopt §3.3/§4.1a/§4.2a as written or amend them explicitly |
| One persona/harness/model compatibility contract | Consumer + constraint-setter | D6's `harness_compatibility_revision` must be a snapshot field (§3.1) and checked per hop (§4.3). **C7 is settled by U2**, which also splits the stable class ID from the versioned harness/contract revision — both are snapshot fields (§3.1). **The two divergences an earlier revision escalated here are closed at PMM-03's rev-3** (`25ece717`, §2.8): it now supplies `compatibility_class` on persona rows with U2's stable unversioned strings and a separate `harness_contract_revision`, forbids cross-class fallback, and names PMM-06 as the consumer (`5420…:100-104`, `:173`, `:336`). **Nothing left for the synthesis to fix in this row** — what remains is delivery, not agreement |
| One catalogue/allowlist/invocability contract at write and dispatch time | Consumer | §2.6b's freeze-the-choice-never-the-permission split is a constraint the synthesis must preserve, or the snapshot becomes a stale permission slip. PMM-03 owns the gate; PMM-06 owns not short-circuiting it |
| One resolver contract covering all paths and the webhook latency boundary | Consumer, with one unresolved input | §5.1's `/work/admit` resolution point satisfies the latency boundary, and §4.1a fixes the protocol so no envelope mutation is needed. **The GitLab gap is now closed by explicit refusal** (`snapshot_unavailable_channel`, §5.1) rather than left to the synthesis: that channel is excluded from admission *and* bypasses `spawn_persona`, so PMM-06 refuses rather than defaults. **Routing GitLab through admission remains open and belongs to this contract** — it is a change to that channel's publish path, i.e. PMM-07's scope, not PMM-06's |
| One vocabulary and precedence ladder | Consumer | Uses PMM-01's vocabulary; §2.6a adds the tenant-resolution precision D1 implies (lookup keyed by the dispatch record's tenant; no cross-tenant preference fallback). **One conflict for the synthesis** (§2.8): PMM-01's head declares itself *binding* while still keying the mapping on `(tenant, principal kind, principal ID, persona key)`, which PMM-02's settled alias contract has superseded by demoting `principal_source`/`principal_kind` out of the key. PMM-06 keeps `principal_kind` as a **snapshot** field (§3.2 needs it for AC-03) but not as a storage-key component |
| One schema and API surface shared by UI and CLI | Not a contributor | PMM-06 reads PMM-02's rows; it adds no user-facing surface, per U6 it introduces no second backend router. It does need PMM-02's **canonical-alias contract** for service principals (C1, U1), and at `e2c7d099` PMM-02 owns and specifies it: `canonical_service_principal_id` opaque and immutable (`5419…:545`), and the alias key **corrected to U1's form**, `(org_id, alias_source, alias_id)` among active rows (`:552-554`). **The "one form to settle" an earlier revision raised here is resolved** — PMM-02 adopted U1 and says so. §3.2's `service_policy` row now names that exact field |
| One audit/cost/evidence schema | Contributor of required fields | The facts the gateway's signed decision names (§4.3, gateway step 4) — persona, resolved model, resolution source, destination, and snapshot-versus-live policy revisions — are what PMM-08 consumes. **Now stronger than before:** because the decision is signed gateway-side, the audit record is of what the *authority* decided rather than of what a worker reported having chosen |
| One deployment DAG, report-only posture, enforcing gate and rollback | Consumer | §7.2's report-only posture and §7.3's rollback are written to match the epic's DAG; the enforcing flip stays PMM-09's |

**Where this note may be overridden without argument.** If the synthesis rules differently on the
snapshot's delivery point (§4.2a), GitLab's disposition (§5.1), or the snapshot field set (§3.1),
the canonical design wins and this note should be amended rather than treated as a competing
source. C2 and C7 are no longer in this category — U4 and U2 have settled them.
What should survive any such ruling, because each is a verified repository fact rather than a
preference: **admission runs after the envelope digest is written, so no snapshot field can be added
to the envelope** (§4.1a — `spawn_persona.py:241-246` then `sqs_publisher.py:56-61`); **a digest
cannot supply a decision, so some authenticated channel must return one** (§4.2a); **a worker that
holds the invocation capability cannot be trusted to police its own selection, because
`entrypoint.py:1578`'s in-image fallback is live today and would silently outvote any decision it
failed to verify** (§4.3, §8.1 — and PMM-07's head reaches the identical conclusion on the ARC path
for the identical reason, `5425…:1557`); **the bootstrap response is `adpr1`-HMAC-signed and has
never reached the `adpe1` signer, so a worker-verifiable decision is new work on two axes** (§4.3 —
`bootstrap.py:373`, `run_credential.py:61`, and no `sign_envelope` call on either module's paths);
the envelope cannot express "service-rooted" today (§3.2); **`actor.user_id` is a verbatim copy of a
source-supplied value (`service_authority.py:112`) and therefore cannot own a preference** (§3.2,
U1); there are four real envelope builders and the dataclass is dead code (§2.1); `authority_kind`
has three values not two, and the live model path already branches on exactly that split while
resolving *nothing* for the service arm (§3.2 — `model_identity.py:72`, `:169-171`); the `Decimal`
digest hazard fails **all** dispatch under a misleading reason code (§7.1a); and the default-model
drift is at least eight identifiers wide (§8.1).

---

## 9. Verdict and conditions

**Ready with specified conditions.** The design in §3–§7 is buildable and I recommend
proceeding, but the note's status is **proposed pending #5417 synthesis** — the conditions below
must be reconciled against that synthesis before implementation starts, and per that gate no
developer is dispatched until the operator publishes the unified canonical design (§8.2).

Eleven conditions, and **none of them now awaits an operator ruling.** C1 was settled by the first
operator review on PR #5442; **C2 and C7 are settled by #5417's unified rulings U4 and U2**
(§2.7) — the earlier "needs an operator ruling" framing on both is withdrawn. **C3, C3a and the new
C10 are operator-directed** and supersede this note's earlier drafts: C3 withdraws the DynamoDB
projection (§5.1), and **C10 withdraws worker-side selection entirely** (§4.3) — in both cases the
superseded text is *removed* from this note rather than kept beside its replacement, which is what
the third review asked for. C3's GitLab half is **closed by an explicit refusal** rather than left
open. C4–C6, C8 and C9 (from U5) are directions this note gives and a developer can execute.
The synthesis gate still governs dispatch (§8.2); "no open rulings" is not "cleared to start."

All five sibling notes were re-read at their **current branch heads** for this revision (§2.8) —
all five had moved since the previous revision, and the re-read changes findings rather than just
SHAs. **Both 🔴 gaps the previous revision recorded are retired:** PMM-03's rev-3 (`25ece717`)
supplies the U2 compatibility-class vocabulary and now agrees with U2 on the Sonnet candidate and
with U3 on probe posture. **One correction flows into this note:** PMM-07's P4′ (`1d799351`)
establishes that bootstrap signs `adpr1` HMAC today and has never reached the `adpe1` signer, so the
signed-decision channel §4.3 specifies is new work on two axes — stated in §4.3 rather than assumed.
**One new dependency state replaces the retired gaps:** no model is certified invocable until PMM-09
probes, so live admission has no passing evidence to read at PMM-06's completion (§8). And PMM-07's
fourth pass reaches §4.3's gateway-authoritative conclusion **independently, on the ARC path**,
overturning its own three-revision recommendation — the strongest available evidence that this
revision's central correction is right rather than merely compliant.

| # | Condition | Who acts |
|---|---|---|
| **C1** | ✅ **SETTLED — approved on PR #5442.** On a `service_policy` authority the **service account is the preference owner**, and the authority row's `human_id` is **audit attribution only**; the snapshot carries its own `principal_kind`/`principal_id` and never reads `root_human_id` for preferences (§3.2). The service principal is resolved through **PMM-02's canonical alias contract** — PMM-06 does not invent a second identifier for it, and the alias contract is the single place that decides what a service principal's canonical ID is. **Implementation clause, added this revision:** that resolution takes PMM-02's **`canonical_service_principal_id`**, derived from the *authority row's* verified `service_identity` (the value `service_authority.py:76` proves equal before the row is trusted), and **never the envelope's `actor.user_id`** — which is source-supplied, copied verbatim from the inbound event at `service_authority.py:112`, and excluded by U1. An unresolvable alias fails closed (C9). The `principal_kind`/`root_principal_type` divergence is confirmed intended (different axes: preference ownership vs credentials/billing), with the in-code comment pointing at #4337 D4c still required | Operator — **done**; developer implements against PMM-02's alias contract |
| **C2** | ✅ **SETTLED — approved by #5417 ruling U4.** The two-part integrity split stands as designed (§3.3): snapshot + `snapshot_digest` persisted by gateway work-admission in worker-unwritable storage, plus a **mandatory** bootstrap that verifies workload/run/root and returns a **fresh, audience- and chain-bound** `adpe1` assertion, reissued per hop, `MAX_ENVELOPE_TTL_SECONDS = 30` retained, **no signer secret reaches a worker and no long-lived offline-verifiable policy token is issued**. The long-lived-signed-blob alternative is rejected. U4 also **requires** the signer-contract extension this note flagged — a snapshot-specific audience constant plus a chain-binding claim, on both sides of the golden vectors — so it is a build instruction, not a pending approval. No new key material (§2.7, §3.3) | Operator — **done**; developer implements |
| **C3** | **Resolve the snapshot in the gateway, not the Lambda — no second DynamoDB projection of PMM-02's Postgres rows (§5.1).** Extend the existing trusted `/work/admit` path (already fail-closed before SQS publication when authority + work claims are enabled, `sqs_publisher.py:53-66`) to resolve the canonical principal mapping from Postgres and persist the immutable snapshot + `snapshot_digest` on the worker-unwritable execution row, **keyed by `invocation_id`**. **The envelope gains no key and the receipt is not a transport** (§4.1a) — the envelope is already sealed before admission runs, so a post-admission mutation would invalidate the protected digest and fail all dispatch. **GitLab is closed, not open:** that channel gets no snapshot in PMM-06 and a dispatch requiring one is refused with a distinct `snapshot_unavailable_channel` reason, never defaulted; routing it through admission is PMM-07's resolver contract, recorded as a named follow-up (§5.1, §8.2) | Developer + PMM-02 owner |
| **C3a** | **No worker acts on a model decision before mandatory gateway bootstrap** has verified workload/run/root binding and returned a short-lived signed decision whose assertion covers the decision bytes (and, through them, the `snapshot_digest`); reissue per hop; retain `MAX_ENVELOPE_TTL_SECONDS = 30`; **no long-lived worker-verifiable snapshot or decision token at any hop**. Report-only may observe, but enforcing mode is blocked on #3186/#5195, because the gateway's decision for the *root* hop still rests on an unsigned origin (§3.3, §4.3) | Developer |
| **C4** | **Do not add the snapshot to any envelope builder** — §4.1a supersedes the earlier direction here, and the contract test now asserts the **envelope key set is unchanged** by this story. The four-builder finding still matters for the *other* half of this condition: derive `principal_kind` from an exhaustive **three-kind** `authority_kind` mapping including `gate_decision`, failing loudly on an unrecognized kind rather than defaulting to `human`, and note that `orchestration/dispatch_pass.py:628-667` is a real builder outside the webhook-ingress tree which nonetheless **does** admit (`dispatch_pass.py:953`, `ENGINE_FLOW` owner), so it gets its own snapshot association at admission and still no envelope field. Extend AC-01's asserted field list with D6/U2's `harness_compatibility_class` **and** `harness_compatibility_revision` (§2.1, §2.3, §2.7, §3.2) | Developer |
| **C5** | Keep report-only strictly behaviour-neutral (§7.2), and record AC-09/AC-12's reporting limits against #2293 rather than marking them complete (§6, §7.1) | Developer |
| **C6** | **Largely retired by §4.1a** — since the story adds no envelope key, the `Decimal` digest trap and the 256KB ceiling no longer apply to the snapshot. What remains: add the **envelope-key-set-unchanged** contract test (this is the guard that keeps §4.1a true as the code evolves); keep the coercion rule (`str`/`int`, mirroring `webhook_events.py:82-87`) as a standing rule for any *future* envelope field; keep the queue drain as ordinary rollback practice without presenting it as a digest mitigation (and note PMM-06 changes no Lambda code — the gateway and worker image are what ship, §7.3); and make snapshot persistence inside `/work/admit` **fail-soft under report-only**, so a snapshot that cannot be built or stored never turns an otherwise-admissible dispatch into a refusal (§7.1a, §7.2, §7.3) | Developer |
| **C7** | ✅ **SETTLED — approved by #5417 ruling U2**, and made more precise than this note had it. `system_default_model_id` is keyed by **compatibility class**, and **no cross-class fallback** is permitted, so an absent `gpt-*` mapping cannot resolve to the Claude default (#5433). U2 additionally splits the field this note had as one: **class IDs are stable and unversioned** (`claude-agent-sdk`, `codex-sdk`) while **harness/contract revision is a separate versioned field** that is part of snapshot keys — so §3.1 carries both. And `us.anthropic.claude-sonnet-4-6` is a Claude-class **candidate**, not a proven default until PMM-09 records a bounded invocation: a class-keyed default with no proven value must refuse rather than resolve (§2.7, §3.1). **Two implementation details from the sibling heads** (§2.8): freeze the **whole** class-keyed default map as of the pinned revisions, not the subset reachable by this chain, per PMM-01's head (`5417…:403`) — otherwise a hop whose harness class differs from the root's cannot resolve; and PMM-03's head currently supplies **no class ID at all** and still calls Sonnet 4.6 canonical, so the class vocabulary is a real dependency gap to close in PMM-03 (§8) rather than something PMM-06 can assume exists | Operator — **done**; developer implements |
| **C8** | **Reconcile D1 and D3, which this note previously named without engaging (§2.6).** Two parts, both directions the locked decisions already imply. (a) **Tenant**: key the mapping lookup on the **dispatch record's** `tenant_id` — the trusted value the path already resolves (`work_admission.py:110`, `:123`; webhook origin `identity_resolver.py:390-391`) — not on a workspace preference or principal alone; and treat an absent mapping in *this* tenant as absent, never falling back to the same human's mapping in another tenant (§2.6a). (b) **Freeze the choice, never the permission**: the snapshot freezes `mappings`/`system_default_model_id` verbatim for the chain's life (AC-04), but allowlist, harness, invocability, budget and rate limits are re-checked **live at every hop**; the frozen revisions are audit evidence, not a cached admission. A mid-chain revocation must refuse as `model_unavailable`/`harness_incompatible`, never as `snapshot_altered` (§2.6b, §4.3 gateway step 3, §6) | Developer |
| **C9** | **New, from #5417 ruling U5 (ARC/GitHub Actions root identity).** Root ownership follows the **authenticated initiator**, never the bot credential that happens to execute the job. A human-initiated `issues:labeled`, issue-comment or `workflow_dispatch` event preserves the resolved canonical human root; a **scheduled, service-to-service or workflow-triggered run with no authenticated human initiator must resolve a tenant-bound registered canonical service principal and fail closed if unregistered.** For PMM-06 this is a snapshot-construction rule: `principal_kind`/`principal_id` derivation must fail closed on an unregistered service initiator rather than attributing the snapshot to the executing App identity or to an incidental human. The execution identity is audit attribution only (§2.7) | Developer |
| **C10** | **New, from the third operator review — the gateway is the only authoritative selector (§4.3).** Three parts, all buildable. (a) **The gateway resolves each hop's model and returns a signed decision** naming `persona`, `resolved_model_id`, `resolution_source`, destination, the `snapshot_digest` it resolved from, and both the snapshot's recorded and the live-applied policy revisions; the `adpe1` assertion's `body_digest` covers the **decision bytes**, so the chosen model is signed rather than only the snapshot it came from (§6). (b) **The worker contains no resolver** — no mapping lookup, no class-keyed default, no precedence rule, no in-image fallback. Its whole contract is verify-then-invoke-or-fail. **`entrypoint.py:1578`'s `or os.environ.get("ANTHROPIC_MODEL", "global.anthropic.claude-opus-5")` must be closed on this path**, not merely left unused: it is live today because `keda.tf` sets no `ANTHROPIC_MODEL` (§8.1), and a fallback inside the worker is exactly how a gateway decision gets silently outvoted (#4511's inert-config class). The refusal test must assert the **literal was not used**, not that the right model was used. (c) **The shared precedence function lives in the gateway**, not in a library the worker also links — the story asked for a shared function and was right to, but `model_validate.py` versus `proxy/model_resolver.py` already shows what two linkable copies do. **The superseded worker-selection procedure is removed from this note, not annotated.** PMM-07's head reaches the same conclusion independently on the ARC path (`5425…:1557`, §2.8). **One honest cost:** the bootstrap response is `adpr1`-HMAC-signed today and has never reached the `adpe1` signer, so this is new work on two axes — a new field *and* a new signer for that response (§4.3, and PMM-07's P4′) | Developer |

**Completion boundary, restated.** Merged implementation with deterministic and adversarial
tests, deployed report-only. Live multi-hop acceptance with real model invocations is PMM-09's
(#5427). Merging PMM-06 does not authorize the enforcing flip.

### 9.1 Test-design notes the story gets right and should keep

Two points in the story's Execution section are well-judged and should survive into
implementation. Trace AC-02 and AC-05 through a **real entry point** rather than a service-level
seam, because a service-level test cannot observe adapter drift. Note the correction in §2.4a:
`spawn_persona` is *an* entry point, not *the* entry point, so "trace it through `spawn_persona`"
is necessary but no longer sufficient — the GitLab and orchestration-engine paths need their own
coverage, or their absence documented as an explicit gap. And configure **three genuinely
different models** for the chain test — a test that passes because all three hops resolved to the
same model would hide a per-hop selection defect entirely.

One to add: AC-04's mid-flight-edit test must assert the *later hops of the already-running
chain*, not merely that a new chain sees the new value. Verbatim propagation (§4.2b) makes this
hold by construction, so the test is guarding the construction, not the value.

**One more, which C10 makes necessary and which no acceptance criterion currently states.** A test
that the worker uses the decision it was given cannot distinguish "obeyed the decision" from
"resolved the same answer independently" — the two agree whenever the system is working, which is
most of the time. The contract C10(b) asserts is a **negative** one, so it needs negative tests:
give the worker a decision naming a model that its own in-image default would *not* produce, and
assert the named model was used; then give it an unverifiable decision and assert it **refused**
rather than falling back. A worker with no resolver is only demonstrably resolver-free if the test
would fail when a resolver were reintroduced.

---

## 10. Evidence index

All paths relative to the repository root, verified at `ae598410` on 2026-09-18.

**Dispatch and envelope**
- `modules/agent-factory/webhook-ingress/lambda/common/spawn_persona.py` — `spawn_persona` :62; guards :379; depth guard :435-449; `_advance_chain_depth` :304 (called :195); `_build_envelope` :524, correlation block :575-587, conditional model fields :596-599, `token_source` :608-609, `message_id` :610; authority provisioning :220-263; `_get_max_credential_chain_depth` :619-658; `_compute_authorized_user_id` :665-712; `MAX_CHAIN_DEPTH` :40
- `modules/agent-factory/webhook-ingress/lambda/common/envelope.py` — `Correlation` :22-28; model fields :68-69; `to_dict` correlation :102-106; `token_source` conditional :113-116
- `modules/agent-factory/webhook-ingress/lambda/common/sqs_publisher.py` — `prepare_envelope` :109-115
- `modules/agent-factory/webhook-ingress/lambda/github/handler.py` — single-enforcement-point comment :1765-1767; `/model` leniency :1786-1789; `determine_correlation` :960; `_resolve_pointer_provenance` :826
- `modules/agent-factory/webhook-ingress/lambda/github/agent_trigger.py` — rejection vocabulary :27-36; agent-to-agent `spawn_persona` call :374 (no model arguments, §2.4a)
- `modules/agent-factory/webhook-ingress/lambda/eventbridge/handler.py` — `spawn_persona` call :237 (no model arguments); `resolve_service_identity` :120 (§2.4a)
- `modules/agent-factory/webhook-ingress/lambda/gitlab/handler.py` — **bypasses `spawn_persona`**: model fields hardcoded `None` :172-173, direct `publish_envelope` :229 (§2.4a, §5.1 gap)
- `modules/gateway/src/orchestration/dispatch_pass.py` — **bypasses `spawn_persona` by ruling**: "deliberately NOT called (hazard 4)" :88, `ImportError` rationale :100-101, direct `sqs.send_message` :1096 (§2.4a)

**Gateway-side resolution point (§5.1)**
- `modules/agent-factory/webhook-ingress/lambda/common/sqs_publisher.py` — publication **gated fail-closed** on admission :53-66; requires `ADP_WORK_CLAIMS_ENABLED` :54 and `AGENT_AUTHORITY_ENABLED` :59-61; **GitLab excluded** :55; returns `None` (nothing published) on refusal :66 — all **before** `json.dumps` :67
- `modules/agent-factory/webhook-ingress/lambda/common/gateway_client.py` — `admit_issue_work` :300; HTTPS/no-credentials-in-URL validation :313-323; SigV4 `GetCallerIdentity` proof with the invocation in the **signed** headers :325-348; no proxy, no redirect :367-375; receipt parsed and `disposition` branched :377-382
- `modules/gateway/src/agentauth/work_routes.py` — `WorkAdmissionRequest` is `extra="forbid"` with **only** `invocation_id` :36-38; `verify_producer` :41-66 binds the invocation into signed headers :52 and checks the caller role against `ADP_WORK_CLAIM_PRODUCER_ROLES` :63; `POST /admit` :69-79 under prefix `/internal/v1/agent/work` :30
- `modules/gateway/src/orchestration/work_admission.py` — `admit_pending` :99, docstring *"Resolve every authority field from protected dispatch, not HTTP input"* :100-105; protected pointer/execution reads :109-115; live grant :116; **Postgres** `claim_work` on a SQLAlchemy session :72-96 and session factory :173-176; receipt shape :96. **Tenant provenance (§2.6a):** `org_id` read from the protected dispatch pointer's `tenant_id` :110, execution keyed `TENANT#{org_id}` :113, and the scope assertion `grant.tenant_id != org_id` :123 — the tenant is never taken from caller input
- `modules/agent-factory/webhook-ingress/lambda/common/identity_resolver.py` — webhook tenant origin is the GitHub **installation**, not a user-selected workspace: `pg_install["tenant_id"]` at :390-391 and the second write path at :434-435 (§2.6a)
- `modules/gateway/src/auth/workspaces.py` — `select_workspace` :196; memberships re-read under `with_for_update()` :203, :213, :217-219 (a mutable, revocable selection) (§2.6a)
- `modules/gateway/src/admin/connections/routes.py` — `switch-tenant` route :305-315, docstring *"callers must refresh tokens after this switch"* :311, returning `active_tenant_id` :315. The UI/CLI notion of an active tenant is token-scoped and mutable, which is why §2.6a does not use it as the snapshot's `tenant_id`
- `modules/gateway/tests/agentauth/test_work_producer.py` — producer **cannot** choose `org_id`/`owner_ref`/`issue_number`/`generation`/`force_handover` (422) :117-124; stable 409 refusal :127-134

**Authority and integrity**
- `modules/agent-factory/webhook-ingress/lambda/common/agent_authority.py` — worker-no-write docstring :1-7; `_digest` :67-72 (plain `json.dumps`, **no `default=`** — this is the `Decimal` trap of §7.1a); `VerifiedHumanEvent` :36-63; `provision_human_dispatch` :94; authority record :129-140; digest computed :170 and stored :183 **by the Lambda, not the gateway**; broad `except (… TypeError, ValueError)` → `AuthorityProvisionError` :303
- `modules/gateway/src/agentauth/engine.py` / `coordinator.py` — the **third** authority kind `gate_decision` :31, :101 / :157 (§3.2)
- `modules/gateway/src/orchestration/dispatch_pass.py` — the **fourth** envelope builder :628-667, `is_human_rooted` from `genesis` :652, published :1096 (§2.1, C4)
- `modules/gateway/src/budget/run_binding.py` — `root_principal_type` as a **derived** property, #4337 D4c "never a new column" :300-322 (§3.2 reconciliation)
- `modules/agent-factory/webhook-ingress/lambda/common/sqs_publisher.py` — `MAX_SQS_MESSAGE_BYTES` :22; `default=str` on the wire :67; size check/truncate/raise :109-121 (§7.1a)
- `modules/agent-factory/webhook-ingress/lambda/common/webhook_events.py` — the canonical-JSON-**string** precedent that avoids `Decimal` round-trip drift :82-87 (§7.1a fix)
- `modules/agent-factory/webhook-ingress/lambda/common/service_authority.py` — `VerifiedServiceEvent` :20-52, `from_native_event` :29-52 (the source-supplied provenance of `service_identity`); **authority-row `service_identity` equality check :76 — the verified value §3.2's corrected row derives from**; `authority_kind == "service_policy"` :80; `human_id` requirement :82; **`"user_id": event.service_identity` :112 — the copy that makes `actor.user_id` caller-supplied and therefore forbidden by U1 (§2.7, §3.2)**; correlation block :117-123 (`is_human_rooted: True` on the service path); `envelope_digest` :138
- `modules/gateway/src/agentauth/bootstrap.py` — `envelope_digest` :18; **`mint_credential` import :13 and call :373 — the bootstrap response is signed `adpr1` HMAC, never `adpe1` (§2.8, §4.3)**; digest comparison :305 and :305-306; :318-324; :350-351; conditional transaction :336; **`issue_bound_credential` :372, returned fields :382-388 — `credential`, `invocation_id`, `attempt`, `credential_epoch`, `expires_in` and **no model field** (§4.2a, §4.3)**
- `modules/gateway/src/agentauth/run_credential.py` — `CREDENTIAL_VERSION = "adpr1"` :61; `mint_credential` :166, HMAC-SHA256 wire format :215-216; **the docstring that explains why this signer is the wrong one for a worker-verifiable decision — symmetric because *"no worker ever holds this key"*, naming `agentauth/envelope.py` as the asymmetric counterpart :25-28** (§4.3)
- `modules/gateway/src/agentauth/routes.py` — `bootstrap()` method :249-259 (`workloads.verify` :250, **`admit_deferred_bootstrap` imported and run :251-256 — live admission already executes inside bootstrap**, `store.bind` :258, `issue_bound_credential` :259); async route :279-293 with the credential returned :292 and `Cache-Control: no-store` :293; 425 :295-296; 404s :297, :298-299. **Neither this module nor `bootstrap.py` calls `sign_envelope` on any path** — verified by direct search at `ae598410` (§4.3)
- `modules/gateway/src/orchestration/work_admission.py` — **`admit_deferred_bootstrap` :183**, with the ordering stated as a contract in its own docstring *"Called after pod verification, before binding or returning any credential"* :184; protected pointer re-read and tenant resolution :189-192; `envelope_digest` re-verified against the stored value :194-195; startup-deadline refusal :198-206; `admit_pending` :208; lost-race mapped to `work_waiting` :210-212 (the 425 at `routes.py:295-296`). This is why §4.3's per-hop live gate extends a check that **already runs at this point** rather than inserting a new one (§4.2a)
- `modules/gateway/src/agentauth/dispatch.py` — `DispatchRequest` :88-93; `_envelope` :264-285 with `is_human_rooted: True` :281; `_reserve` digest :291, :305, :353
- `modules/gateway/src/agentauth/grants.py` — `AuthorityReference` :99-114
- `modules/gateway/src/agentauth/model_identity.py` — `AgentModelIdentityMiddleware` :37-75, registered `app.py:318`; **refuses a worker whose asserted `X-Agent-RunId`/`X-Agent-OrgId` disagree with its verified credential :67-70**; **root read server-side, never from worker input: `grant.authority.human_id` :71, canonicalized through `resolve_root_user_entity_id` :74** — the existing precedent §4.3 and §3.2 both build on. **The three-way authority branch is already live here:** the canonicalization is gated on `grant.authority.kind != "service_policy"` :72, `gate_decision` takes its own policy-admission path :76-90, and attribution writes `user_id=root`, `root_human_id=root`, `is_human_rooted = grant.authority.kind != "service_policy"` :169-171 — so on a service-rooted run the only identifier carried is the **authorizing human's**, unresolved. That absence is what §3.2's `service_policy` row adds rather than replaces
- `modules/gateway/src/proxy/model_resolver.py` — `ModelResolver` :103; `resolve_model` :131; `get_allowed_models` :200; `set_allowed_models` :316. One of the **two** drifted copies of model logic (`model_validate.py` is the other) that §4.3 cites as the reason the shared selection function must live gateway-side rather than in a library the worker also links
- `modules/gateway/src/proxy/bedrock_principal.py` — service-rooted treatment :63-65

**Asymmetric signer — exists, reused per-hop (§3.3 Mechanism A)**
- `modules/gateway/src/agentauth/envelope.py` — `ENVELOPE_VERSION = "adpe1"` :74; `ALLOWED_ALGORITHMS` :78; `ENVELOPE_ISSUER` :82; `ENVELOPE_AUDIENCE` :85; `SIGNING_KEY_ENV` :88; `SIGNING_KEY_ID_ENV` :91; `MAX_ENVELOPE_TTL_SECONDS = 30` :93-95 (comment: "This IS the documented maximum revocation delay"); `_canonical` :204-205; `sign_envelope` :228-282 — **no `audience` parameter** in its signature :224-241, `aud` hardcoded to `ENVELOPE_AUDIENCE` :258, `_REQUIRED_CLAIMS` :99-116 demand `target_run_id`/`action`/`command_id` with chain scope only via optional `flow_id` :272-273 (§3.3 contract widening); TTL **clamp** :253, claim set :255-277, domain-separated signing input :280, wire format :281; `verify_envelope` :284, `alg` allowlist check :342 **before** `key.verify` :356, strict `aud` check :346, TTL rejection :389
- `modules/agent-factory/agent/src/control-envelope.ts` — worker-side Ed25519 verifier; constants :47-61; version check :278, :297; `alg` allowlist :314; domain-separated verify input :324; expiry/nbf :362-363; TTL **rejection** `validity_too_long` :364-366. Both verifiers reject rather than clamp (`envelope.py:389` is the Python equivalent); only `sign_envelope` clamps (:253)
- `modules/agent-factory/agent/src/__fixtures__/control-envelope-vectors.json` + `modules/gateway/tests/agentauth/test_envelope_vectors.py` — cross-language golden-vector parity
- `modules/agent-factory/webhook-ingress/infra/agent-authority-bootstrap.tf` — private key scoped outside the worker's `adp/*` grant :1-4; two Ed25519 slots :11-19; `kid` = `substr(sha256(pem),0,16)` :27-31; public keys published as `ADP_CONTROL_ENVELOPE_KEYS` / `..._KEYS_FILE` :38-42
- `modules/agent-factory/webhook-ingress/infra/variables.tf` — `agent_control_signing_key_slot`, `agent_control_publish_both_keys` :473-487
- `docs/runbooks/agent-authority-key-rotation.md` — staged rotation procedure
- `modules/agent-factory/agent-worker-image/entrypoint.py` — key plumbing, public-keys-only comment :2757-2766
- `modules/gateway/src/agentauth/routes.py` — **forward path dormant**: `control()` :236-247 mints via `prepare_command` then raises `PolicyError(501)`; comment *"Enabling a policy verb alone must never return success without actually forwarding its effect"* (sequencing hazard, §3.3)

**Signing precedent (pattern reuse only)**
- `modules/agent-factory/agent-worker-image/lib/marker_signing.py` — authority-mode self-disable :46-52 (the *wrong* signer for this story; see §2.2)
- `modules/agent-factory/webhook-ingress/lambda/common/marker_verify.py` — placeholder refusal :45-61, :97-104; overlapping keys :63-122, rotation-from-placeholder :120-122; tri-state `verify_marker` :182-212

**Worker**
- `modules/agent-factory/agent-worker-image/entrypoint.py` — `parse_envelope` :480-491 (presence check only; extra keys ignored); `model_resolved` read :1576; hard-coded fallback `global.anthropic.claude-opus-5` :1578; `ANTHROPIC_MODEL` injection :1594; `ADP_MODEL_RESOLVED` export :1686-1687; `chain_depth` read from nested `correlation` :1273, :1292-1296
- `modules/agent-factory/agent-worker-image/lib/run_identity.py` — whole-envelope digest :69-71; work-claim gate :181-195
- Default-drift sweep (§8.1) — `entrypoint.py:1578`, `agent/src/agent-worker.ts:127`, `agent/src/components/ConfigLoader.ts:19`, `.github/workflows/agent-architect.yml:197` (and eight sibling agent workflows), `modules/agent-factory/infra/gateway-main.tf:477`, `agent/k8s/chat-scaledjob.yaml:41`, `modules/agent-factory/gateway/app/sqs_consumer.py:37`, `modules/agent-factory/rules/workflows/agent-template.yml:95`; legacy-ID note at `model_validate.py:34`; no `ANTHROPIC_MODEL` in `webhook-ingress/infra/keda.tf`

**Cache precedent**
- `modules/agent-factory/webhook-ingress/lambda/common/negative_cache.py` — never cache the error state :33-38; validate TTL on read :40-49

**Latency**
- `modules/agent-factory/webhook-ingress/lambda/common/gateway_client.py` — `timeout=10` :138, :268, :376, :611

**Not present (searched, absent)**
- No JWKS **endpoint** — verification keys reach workers as injected env/file material, not over HTTP. (An asymmetric signer and worker-side verifier *do* exist; see the section above.)
- No signer with a policy-appropriate TTL: the only asymmetric signer caps at 30 seconds by design
- No consumer of `ADP_MODEL_REQUESTED` / `ADP_MODEL_RESOLVED` anywhere in the tree (#2293)
- No production consumer of `WebhookEnvelope` — definition, its own tests, and one comment in `lambda/gitlab/handler.py:113` only
- `docs/design-notes/5417-per-invoker-persona-model-mapping.md` does not exist on `origin/main`
- **No call to `sign_envelope` from `agentauth/routes.py` or `agentauth/bootstrap.py`** — the `adpe1`
  signer is reachable from the control path only, so the bootstrap response has never been
  asymmetrically signed (§2.8's PMM-07 P4′ correction, §4.3)

**Sibling design notes, read at the heads named (§2.8)**
- `agent/issue-5418` @ `b8045dbf` — `docs/design-notes/5417-per-invoker-persona-model-mapping.md`, rev-5
- `agent/issue-5419` @ `e2c7d099` — `docs/design-notes/5419-persona-model-preference-schema-and-api.md`
- `agent/issue-5420` @ `25ece717` — `docs/design-notes/5420-persona-and-model-catalogue.md`, rev-3
- `agent/issue-5425` @ `1d799351` — `docs/design-notes/5425-persona-model-resolver-wiring.md`, fourth pass
- `agent/issue-5427` @ `86c7959a` — `docs/design-notes/5427-pmm09-default-consolidation-and-enforcing-flip.md`
