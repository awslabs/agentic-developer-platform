# Design Note: Per-Invoker Agent Persona-to-Model Mapping (PMM-01, Issue #5418)

> **Status**: **Rev-5 — binding PMM-01 vocabulary/precedence baseline; operator decisions D1-D6 locked 2026-09-18; unified epic rulings U1-U6 adopted 2026-09-18**
> **Author**: @agent-codex, drafted via Codex CLI delegation, supervisor-reviewed
> **Rev-2**: system default keyed by compatibility class (#5433) — @agent-reviewer, applying the operator and architecture-gate corrections on PR #5436
> **Rev-3**: @agent-architect — the class key rev-2 relies on had no owner, the frozen default map is not chain-scoped (§4.1a), and "compatibility class" is a tenth term — *the owner gap is closed by rev-4 (§1.7a)*
> **Rev-4**: @agent-architect — adopts the operator's unified architecture rulings on #5417: the class owner is **settled** (PMM-03), Sonnet 4.6 is a **candidate** not a proven default, class IDs are stable and unversioned with contract revision separate, snapshot trust is gateway-issued per hop rather than an offline token, and automated-run root identity is a registered canonical service principal (§9)
> **Rev-5**: @agent-architect — withdraws rev-4's claim that a second compatibility class is already live: the pinned Codex model configures a delegated tool, `codex-sdk` has no persona mapped to it, and a config-literal test is not invocability proof (§5.5, §8.8); corrects the stale PMM-03 report; narrows the closure claim to precedence and vocabulary, recording the residual cross-epic registry question in §6.7a
> **Date**: 2026-09-18
> **Issue**: #5418
> **Parent EPIC**: #5417
> **Mode**: documentation story — no runtime surface
> **Scope**: binding PMM-01 vocabulary and precedence baseline for #5417, extended by the eight-story synthesis — not the complete #5417 system design by itself
> **Verdict**: PMM-01 baseline settled; D1-D6 locked; U1-U6 adopted; no open decision remains inside this note's scope; PMM-02 unblocked
> **Related**: #1309, #2279, #2293, #2300, #4511, #4673, #4692, #3172/#3174, #3186, #5078/#5089/#5091/#5092, #5195, #5433

---

## 0. Executive summary

- A mapping is `(tenant, principal kind, principal ID, persona key) -> canonical model ID`.
- Selection and authorization are separate axes: the principal mapping selects a model, while organization and team policy constrain whether that model may run (D1).
- When no mapping row exists, the last rung selects the canonical system default **for the target persona's compatibility class**. For the `claude-agent-sdk` class that value is `us.anthropic.claude-sonnet-4-6`, and it is a **candidate** until a bounded real invocation through the actual Claude harness request shape is recorded (D4, D6, U2, #5433).
- The persona-to-class binding is owned by PMM-03; the versioned default and posture records keyed by class are stored by PMM-02 (U2). All twelve personas are in the `claude-agent-sdk` class today; `codex-sdk` is reserved with no persona mapped to it, because the Codex model pinned in the worker image configures a delegated tool rather than a persona's execution harness (§5.5).
- A broken or disallowed selection fails with an actionable explanation and is never silently replaced (D2).
- A chain follows the trusted root principal's signed policy snapshot, while each hop resolves the model for its own target persona (D2, D5).
- Harness compatibility bounds what is selectable at all, and every hop is checked against its actual execution harness (D6).

### 0.1 What the grounding read changed

**The persona set has no `testing` key.** The implementation defines exactly 12
persona keys, so the epic's “Reviewer/testing persona” example means `reviewer`, not a
new combined or testing persona
(`modules/agent-factory/webhook-ingress/lambda/common/personas.py:62-64`).

**No per-principal model selection exists today.** No migration, model, or route
contains `preferred_model`, `model_preference`, `user_model`, or `model_mapping`; the
existing per-principal ladder routes AWS accounts rather than models
(`modules/gateway/src/proxy/bedrock_routing.py`).

**The allowlists described as a working gate are inert on four separate paths.** The
registry value is omitted from token context, the gateway resolver configuration has no
production writer, the produced header has no consumer, and webhook validation never
receives its persona allowlist
(`modules/gateway/src/auth/agent_registry.py:244-277`,
`modules/gateway/src/proxy/model_resolver.py:129`,
`modules/gateway/src/proxy/model_resolver.py:259-264`,
`modules/gateway/src/proxy/model_resolver.py:316-323`,
`modules/gateway/lambda/api-authorizer/handler.py:453`,
`modules/gateway/lambda/api-authorizer/handler.py:517`,
`modules/agent-factory/webhook-ingress/lambda/github/handler.py:1777`,
`modules/agent-factory/webhook-ingress/lambda/common/model_validate.py:49-52`).

**The `/model` path is silent as well as lenient.** An invalid directive currently
logs that it will proceed with the default, while the promised worker warning remains
open in #2293 and the exported request/resolution values have no consumer
(`modules/agent-factory/webhook-ingress/lambda/github/handler.py:1769-1772`,
`modules/agent-factory/webhook-ingress/lambda/github/handler.py:1784-1789`,
`modules/agent-factory/agent-worker-image/entrypoint.py:1683-1687`).

**Two alias maps exist deliberately and have already diverged.** Local webhook
validation avoids a gateway call to meet GitHub's sub-10-second response requirement,
but its aliases and allowed patterns now differ from the gateway's
(`modules/agent-factory/webhook-ingress/lambda/common/model_validate.py:1-10`,
`modules/gateway/src/proxy/model_resolver.py:45-46`,
`modules/agent-factory/webhook-ingress/lambda/common/model_validate.py:34-37`,
`modules/gateway/src/proxy/model_resolver.py:85-100`,
`modules/agent-factory/webhook-ingress/lambda/common/model_validate.py:42-47`).

## 1. Vocabulary

### 1.1 Persona key

A **persona key** is the stable machine-facing name for the agent role whose model is
being selected. The complete set is:

- `developer`
- `pm`
- `operations`
- `reviewer`
- `architect`
- `product`
- `malware-analysis-agent`
- `pt-superpower`
- `superplane-operator`
- `superplane-researcher`
- `aidlc`
- `codex`

`VALID_PERSONAS` is the union of the values in two deliberately asymmetric dictionaries:
`LABEL_TO_PERSONA` and `MENTION_TO_PERSONA`. The mention-only personas are `product`,
`superplane-operator`, `superplane-researcher`, `aidlc`, and `codex`
(`modules/agent-factory/webhook-ingress/lambda/common/personas.py:10-18`,
`modules/agent-factory/webhook-ingress/lambda/common/personas.py:21-59`,
`modules/agent-factory/webhook-ingress/lambda/common/personas.py:62-64`).

Label names are triggers, not persona keys: `agent-reviewer` maps to `reviewer`, and
`superpower` maps to `pt-superpower`. There is no `testing` persona; the epic's
“Reviewer/testing persona” example row maps to `reviewer`. `pt-superpower` is the
sanctioned no-prompt-file exemption tracked by #4037; the test asserts that the exact
exception set “must only ever shrink”
(`modules/agent-factory/webhook-ingress/lambda/common/tests/test_persona_prompt_files.py:49-53`).

### 1.2 Model alias

A **model alias** is a human-typed, mutable shorthand that must be resolved to a
canonical ID before a selection takes effect. Resolution does not discard the alias:
both the alias as entered and the canonical ID it resolved to are retained in the
mapping and in the audit record. Keeping only the canonical value would lose what the
invoker actually asked for, which is exactly what a rejection message must name to be
actionable under D2. The webhook and gateway intentionally keep separate
alias maps because webhook validation cannot spend its response budget on a gateway
call, and those maps have already diverged: the gateway maps `claude-sonnet-4` to an ID
the webhook records as active but not invocable for this platform
(`modules/agent-factory/webhook-ingress/lambda/common/model_validate.py:1-10`,
`modules/gateway/src/proxy/model_resolver.py:45-46`,
`modules/agent-factory/webhook-ingress/lambda/common/model_validate.py:34-37`).

### 1.3 Canonical model ID

A **canonical model ID** is the version-specific inference-profile identifier stored in
the mapping and audit record after alias resolution. It is distinct from mutable input
shorthand. The two validation copies also differ in allowed patterns: the gateway
admits Anthropic, Titan, Llama, Mistral, regional/global Anthropic profiles, and OpenAI,
while the webhook admits four Anthropic patterns only
(`modules/gateway/src/proxy/model_resolver.py:85-100`,
`modules/agent-factory/webhook-ingress/lambda/common/model_validate.py:42-47`).

### 1.4 Principal kind

A **principal kind** identifies the authenticated invoker category whose preference is
being applied, such as a human or service account. It is part of the tenant-scoped
mapping key and the trusted root identity bound into the signed policy snapshot (D5).

### 1.5 Principal ID

A **principal ID** is the stable identifier for the authenticated root invoker within
its principal kind. It is interpreted within the active tenant, allowing the same human
to hold different persona mappings in different tenants (D1).

### 1.6 Mapping

A **mapping** is one tenant-scoped preference from principal kind, principal ID, and
persona key to a canonical model ID. It selects a desired model but grants no access;
all admission gates still apply (D1, D3).

### 1.7 System default

The **system default** is the canonical model used only when the active principal has
no mapping row for the target persona. It is **not a single global identifier**: it is a
lookup keyed by the target persona's **compatibility class** (its harness, per D6). For
the `claude-agent-sdk` class — every persona executing directly today — the value is
`us.anthropic.claude-sonnet-4-6`, and U2 holds it to **candidate** status until it is
proven (§1.7b).

A **compatibility class** is the set of models a given execution harness has a registered
and validated compatibility contract for (§6.6). Today exactly one class has any persona
mapped to it: all twelve personas execute on the Claude Agent SDK worker, so all twelve are
`claude-agent-sdk`. `codex-sdk` is **a reserved class ID with no persona mapped to it** — the
pinned Codex model in the worker image configures a delegated *tool*, not a persona's
execution harness (§5.5). #5433 both maps the first persona into that class and proves its
default separately (U2). Its identity is a **stable, unversioned
class ID** — `claude-agent-sdk`, `codex-sdk` — and the harness or compatibility-contract
revision is a **separate versioned field**, not part of the class ID (U2). The two are
distinct on purpose: the class ID is what a persona is bound to and what a default is keyed
by, so it must not change when a harness is upgraded; the contract revision is what evidence
and snapshot keys carry, so a compatibility proof gathered under one harness revision is not
silently reused under another. A class ID that embedded its revision would invalidate every
persona binding and every stored default on each harness bump.

The class is *derivable from the persona key* so that no mapping or preference row gains a
class column; only the default is class-keyed. **PMM-03 owns that derivation** — the
persona-to-harness-compatibility-class registry is its authority under U2 (§1.7a).

Each class must have its own separately validated canonical default **before** any persona
in that class becomes configurable. A missing, invalid, or unvalidated class default fails
closed with an actionable error naming the class. There is **never** a cross-harness
fallback: a class whose default is absent does not borrow another class's default. This is
required by #5433, which defines `gpt-*` personas whose harness is Codex and whose family
is OpenAI GPT with Claude support and Claude fallback prohibited, and which names #5417 as
owing the compatibility-class-keyed default contract.

Unavailability or incompatibility of a class's validated default is a platform-readiness
failure *for that class*, not permission to choose another fallback (D4).

### 1.7a Who owns the persona-to-class binding — settled

The class-keyed default rests on being able to answer "which compatibility class does this
persona belong to". **U2 settles the owner: PMM-03 (#5420) owns the
persona→harness-compatibility-class registry and the model compatibility/invocability
evidence.** Rev-3 of this note recorded the owner as unresolved and routed a conflict with
#5433 to the operator; that decision has been made and this section records it as closed,
not pending.

The ownership question was real, because two stories claimed the binding. #5433 requires
"one authoritative registry containing persona key, display name, harness ID, compatible
model family, prompt/rules projection, supported invocation surfaces and lifecycle
capabilities". U2 resolves the overlap in PMM-03's favour and assigns #5433 the narrower
job: it "registers `gpt-*` personas and a separately proven Codex/GPT default". So #5433
registers personas *into* PMM-03's registry rather than maintaining a second one. One
binding, one authority — which is what keeps the platform from holding two answers to
"which model does this run use".

Why the platform needs the registry built rather than discovered — the binding does not
exist in code today:

- `personas.py` binds a persona key to nothing but its trigger strings. `VALID_PERSONAS`
  is a bare set of 12 names with no harness, runtime, or model-family attribute
  (`modules/agent-factory/webhook-ingress/lambda/common/personas.py:10-64`).
- There is no persona-to-harness map anywhere in the tree. #5433 records that #2702
  "explicitly deferred `PERSONA_TO_RUNTIME` dispatch".
- The filed PMM-03 story (#5420) does not name a harness on its persona rows, and rev-1/rev-2
  of its design note carried a harness only on *model* rows — answering "is this model valid
  for this harness" rather than "which class is this persona in".

**PMM-03 has since designed that attribute (corrected in rev-5).** Its rev-3 head adds a §2.4
persona→class registry with stable unversioned class IDs and a separate
`harness_contract_revision`, and persona rows now carry `compatibility_class`. So the *design*
obligation U2 created is met; the *code* gap above is unchanged — nothing merged binds a persona
to a harness, which is why this remains an ordering dependency rather than an existing
capability. Two consequences bind the siblings:

1. **PMM-03 must emit the persona-to-class binding before PMM-07 can resolve a class-keyed
   default.** Until that attribute exists, PMM-07 fails closed naming the class rather than
   guessing one. This is an ordering dependency inside the epic's own DAG, not an open
   decision.
2. **Validation and storage are split, and neither story may absorb the other's half.**
   PMM-03 validates catalogue compatibility and holds the class registry; **PMM-02 owns the
   versioned Postgres default and posture records keyed by class** (U2). A default value is
   therefore stored by PMM-02 under a class ID that PMM-03 defines and PMM-03 proves
   compatible. PMM-02's *preference* row still gains no class column (§6.8); the class-keyed
   records are separate default/posture rows.

### 1.7b Candidate default versus proven default

A class's default has two states, and U2 makes the distinction binding:

- A **candidate default** is a nominated identifier that has not yet been demonstrated to
  work. `us.anthropic.claude-sonnet-4-6` is the `claude-agent-sdk` candidate today.
- A **proven default** is one for which PMM-09 has recorded a **bounded invocation using the
  actual Claude harness request shape** — the real runtime request, not a listing call, a
  pricing lookup, or a model-availability check.

Only a proven default may be relied on as the active no-mapping outcome in an enforcing
posture. Until the proof is recorded, the value is the intended default and may be used in
report-only rollout, but no story may describe it as validated or treat its absence of proof
as proof.

This is not pedantry: the platform has already shipped this exact failure. #2300 (closed) was
precisely a case of aliases resolving to Bedrock IDs that listing reported as available but
that were not invocable. U3 generalizes the lesson — "pricing/listing/agreement status never
counts as invocability proof" — and it applies to the class default as much as to any
catalogue entry. The same standard binds each future class: #5433's Codex/GPT class needs a
**separately proven** default, by its own real Codex-harness invocation, before any `gpt-*`
persona becomes configurable.

### 1.8 Direct override

A **direct override** is a valid, audited, policy-controlled one-run model request for
the directly invoked hop only. It does not persist, does not propagate to descendants,
and fails before model work starts if it is unknown, unavailable, incompatible, or
disallowed (D2).

### 1.9 Chain snapshot

A **chain snapshot** is the immutable, authority-signed model-policy record rooted in
the authenticated invoker and verified by every direct and descendant hop. Section 4
specifies its contents, trust boundary, propagation, and validation rules; this section
defines only the term (D5).

| Term | Implementation owner |
|---|---|
| Persona key | PMM-03 — authoritative persona and invocable-model catalogue |
| Model alias | PMM-03 — authoritative persona and invocable-model catalogue |
| Canonical model ID | PMM-03 — authoritative persona and invocable-model catalogue |
| Principal kind | PMM-02 — human/service-account preference schema, API, and audit; canonical service-principal identity and alias registry (U1) |
| Principal ID | PMM-02 — opaque immutable `canonical_service_principal_id` for service principals; never a raw `service_accounts.id`, `agent_name`, `client_id`, ARN or caller-supplied text (U1) |
| Mapping | PMM-02 — human/service-account preference schema, API, and audit |
| System default | **Split (U2):** PMM-03 defines the class ID and proves per-class compatibility; **PMM-02 stores the versioned Postgres default/posture records keyed by class**; PMM-09 records the bounded real invocation that promotes a candidate to proven (§1.7b), and owns shadow rollout, the live matrix, consolidation of divergent hard-coded defaults, and rollback |
| Compatibility class | **PMM-03 (U2)** — owns the persona→harness-compatibility-class registry and model compatibility/invocability evidence. Class IDs stable and unversioned (`claude-agent-sdk`, `codex-sdk`); harness/contract revision is a separate versioned field carried in evidence and snapshot keys. #5433 registers `gpt-*` personas into this registry rather than maintaining a second one (§1.7a) |
| Direct override | PMM-07 — resolver integration across dispatch and runtime paths |
| Chain snapshot | PMM-06 — trusted root policy snapshot and chain propagation |

## 2. The precedence ladder

### 2.1 Two axes, not one ladder

Selection answers **which model is requested**. Authorization answers **whether that
model may run under the active tenant's policy and resolved destination**. D1 settles
these as separate axes: the principal's preference selects, while organization, team,
and member policy constrains. An organization rule may block a selected model but may
never silently replace it with another model.

### 2.2 The selection ladder

Exactly one selection source wins, in this order:

1. A valid direct one-run override wins for the directly invoked hop only (D2).
2. Otherwise, the saved mapping for the trusted root principal and the target persona wins.
3. Otherwise, **absence of a mapping row** selects the canonical system default **for the
   target persona's compatibility class** (§1.7) — not one global identifier.

Rung 3 is the last rung. Absence is the only condition that reaches the default: an
unknown, retired, incompatible, unavailable, or disallowed value in an existing row is
broken state and fails rather than falling through. Descendant hops do not inherit the
direct override; each resolves its own target persona from the trusted root principal's
snapshot (D2).

### 2.3 The admission gates

Admission gates are **not precedence levels**. They are applied after selection and
may admit or reject the single winner without changing it. The gates are:

1. execution-harness compatibility for the target persona and hop (D6);
2. membership in the versioned platform-supported catalogue;
3. the active tenant or organization allowlist;
4. registered service-account restrictions, where applicable;
5. fresh proof that the resolved destination/account/region can invoke the model;
6. compliance policy;
7. budget limits; and
8. rate limits (D3).

The effective selectable set is the intersection of these constraints. An organization
with no explicit allowlist inherits a versioned platform baseline allowlist, not
unrestricted access. Empty, stale, or contradictory effective policy fails with an
actionable explanation (D3).

Gate 1 is keyed by the **stable class ID** and additionally checks the harness contract
revision the evidence was gathered under (§1.7, U2), so a compatibility proof does not carry
silently across a harness upgrade.

Gate 5's freshness evidence is subject to U3's probe-safety rules, which bound **how** that
proof may be obtained. Probing ships disabled with a zero spend budget; **no page load
invokes a model**; and nightly or catalogue/destination-change probes may be enabled only in
PMM-09 after the target account and a spend ceiling are approved. Pricing, listing, or
agreement status never counts as invocability proof. This matters to the ladder because gate
5 is the one gate whose evidence costs money to produce: a resolver that treated a missing
freshness record as licence to probe on demand would let any UI render or any unmapped
invocation spend against an unapproved budget. The correct behaviour when proof is absent is
to fail the gate actionably, not to go and get the proof.

### 2.4 Decision table

The table is exhaustive by rule class. A valid direct override determines the candidate
regardless of whether a saved row is present; an invalid override fails without trying
the row or default. With no override, a present row determines the candidate and no row
selects the default. After any candidate is selected, rejection by any admission gate
changes the outcome to `FAIL`, never to a different model.

| Situation | Outcome | Resolution source |
|---|---|---|
| No direct override and no mapping row | Run the canonical system default **for the target persona's compatibility class** (§1.7) if every admission gate admits it | system-default |
| No direct override and a valid, admitted mapping row | Run the row's canonical model ID | principal-mapping |
| No direct override and a row naming an unknown or retired model | Stop with an actionable error naming the broken saved selection | FAIL |
| No direct override and a row naming a model rejected by organization or other access policy | Stop with an actionable disallowed-model error | FAIL |
| No direct override and a row naming a harness-incompatible model | Stop with an actionable compatibility error for that persona and hop | FAIL |
| Valid, admitted direct override and no mapping row | Run the override for the directly invoked hop only | explicit-direct |
| Valid, admitted direct override and a valid mapping row | Run the override for the directly invoked hop only; leave the saved row unchanged | explicit-direct |
| Valid, admitted direct override and a broken mapping row | Run the override for the directly invoked hop only; the broken row remains broken for later resolution | explicit-direct |
| Invalid, unavailable, incompatible, or disallowed direct override and no mapping row | Stop before model work; do not use the default as fallback | FAIL |
| Invalid, unavailable, incompatible, or disallowed direct override and a valid mapping row | Stop before model work; do not use the row as fallback | FAIL |
| Invalid, unavailable, incompatible, or disallowed direct override and a broken mapping row | Stop before model work; neither invalid selection may fall through | FAIL |
| No mapping row, and the target class's validated canonical default is unavailable or rejected by a gate | Stop with an actionable platform-readiness error naming the compatibility class | FAIL |
| No mapping row, and the target persona's compatibility class has no validated canonical default yet | Stop with an actionable error naming the class and the missing class default; never borrow another class's default | FAIL |
| Required chain snapshot is missing, invalid, expired, mismatched, or modified | Stop with visible diagnostics; do not reconstruct policy or fall back | FAIL |
| Any selected candidate fails catalogue, allowlist, service-account, destination-freshness, compliance, budget, or rate admission | Stop with the rejecting gate and available corrective action identified | FAIL |

### 2.5 Worked example: selection is not substitution

A developer's saved preference selects Opus. The active organization permits only
Sonnet and Fable. The invocation is blocked with an explanation that Opus is disallowed;
it does **not** run Sonnet (D1).

Substitution would hide that the requested policy and the organization's authorization
disagree. It would also run model work the invoker did not select, making behavior and
audit records misleading instead of giving the invoker or operator a clear condition to
correct.

### 2.6 Missing versus broken

**Missing** means no mapping row exists for the active tenant, trusted root principal,
and target persona. That condition alone reaches the system default for that persona's
compatibility class (§1.7).

**Broken** means a row or direct override exists but names a model that is unknown,
retired, unavailable, incompatible, or disallowed, or cannot be validated under the
current policy. Broken state fails actionably and never masquerades as absence. ADP
cannot make this distinction today because no per-principal model mapping exists, and
the current invalid `/model` path silently proceeds with a default
(`modules/agent-factory/webhook-ingress/lambda/github/handler.py:1784-1789`).

## 3. Principal and chain root semantics

### 3.1 The mapping key and the tenant

Resolution always uses the active tenant or workspace. The mapping key therefore binds
tenant, principal kind, principal ID, and persona key; the same human may hold a
different mapping for the same persona in each tenant (D1). Reads, writes, snapshots,
and cache keys must preserve that tenant boundary. A cross-tenant mismatch fails closed
rather than reading, changing, snapshotting, or reusing another tenant's policy.

### 3.2 The trusted root principal

The preference owner is the trusted root human or service account that started the
chain. An intermediate agent, bot comment, workflow role, or Kubernetes service account
does not become the owner merely because it dispatches the next hop. If a service
account starts a chain on behalf of a human without trusted delegated-root evidence,
the service account remains the root and cannot claim the human's preferences.

**Root ownership follows the authenticated initiator, not the credential that executes the
job** (U5). The two are routinely different in ADP, so the rule is stated by case:

- A human-initiated event — `issues:labeled`, an issue comment, or `workflow_dispatch` —
  preserves the **resolved canonical human root**, even though a GitHub App or bot credential
  performs the work.
- A scheduled, service-to-service, or workflow-triggered run with **no authenticated human
  initiator** resolves a **tenant-bound registered canonical service principal**, and **fails
  closed if that principal is unregistered**.
- The workflow or App execution identity is **audit attribution only** and never silently
  replaces the root preference owner.

The failure this forecloses: a nightly scheduled run executing under a shared platform App
credential must not inherit whichever human's preferences happen to be attached to the
credential, and must not fall back to an unowned default identity. It resolves its own
registered principal or it does not run.

A service principal is identified by the opaque immutable
`canonical_service_principal_id` PMM-02 owns (U1), never by a raw `service_accounts.id`,
`agent_name`, `client_id`, ARN, or caller-supplied text. Aliases are tenant-scoped and
source-qualified as `(org_id, alias_source, alias_id)` and are never keyed globally by alias
name; one canonical principal may hold several aliases. This bears directly on the mapping
key in §3.1: a preference is owned by the canonical ID, so re-registering a service account
— which creates a new canonical ID by default — does not silently transfer that account's
saved model preferences to a new registration. Re-linking to an existing principal is an
explicit authorized, audited operation. Cognito `Organization.cognito_client_ids` is an
org-level approved-client list, not a service-principal identity; a client must be registered
and tenant-bound before it may read or write service-self preferences.

### 3.3 Each hop resolves its own target persona

Every hop selects for its own target persona while retaining the same trusted root
principal. In an Architect -> Developer -> Reviewer chain, hop 1 uses the root's
`architect` mapping, hop 2 uses the root's `developer` mapping, and hop 3 uses the
root's `reviewer` mapping. All three selections come from the same root principal's
policy, not from an intermediate agent's identity. A chain may therefore intentionally
run three different models without changing preference owner.

### 3.4 Identity source

Model-policy identity travels in protected lineage and dispatch fields. It is never
inferred from mutable GitHub text or caller-supplied headers. Credential-delegation
depth and model-policy identity are separate concerns: expiry of delegated vault access
must not silently switch the chain to a different preference owner.

## 4. The chain snapshot and its trust root

### 4.1 Why a snapshot exists

The snapshot makes a multi-agent run deterministic. A mapping edit made after the root
invocation starts applies to the next root invocation, not to some later hops in the
current chain. Keeping one immutable policy view for the chain makes its model choices
reproducible.

### 4.1a What is frozen and what stays live

Determinism applies to **selection**, not to admission. Freezing an admission gate would
be a security defect: a chain that began before an organization revoked a model, or
before a budget was exhausted, would keep spending on the strength of a stale grant.
The boundary follows §2.1's two axes.

| Coordinate | Frozen in the snapshot | Re-evaluated at every hop |
|---|---|---|
| Persona-to-model mappings for the root principal | Yes | No |
| Class-keyed system-default **map** as of the pinned revisions, and its revision | Yes — the entire class-keyed map, not one identifier and not a per-chain subset | No — but the default applied at a hop is the one for *that hop's* class |
| Policy / mapping / catalogue revision identifiers | Yes | No |
| Root principal kind, ID, tenant, correlation ID | Yes | No |
| Compatibility class ID (stable, unversioned — `claude-agent-sdk`, `codex-sdk`) | Yes | No — a persona's class does not change mid-chain |
| Harness / compatibility-contract **revision** (separate versioned field, U2) | Yes — recorded as part of the evidence and snapshot key | Yes — checked against the hop's actual harness revision |
| Tenant/organization allowlist decision | No | Yes |
| Destination invocability and its freshness evidence | No | Yes |
| Compliance policy | No | Yes |
| Budget and rate limits | No | Yes |

Freezing the class-keyed default **map** rather than one identifier is what makes
mixed-harness chains work. #5433 §6 permits a chain to cross harness families in both
directions, so a chain rooted on a Claude persona may dispatch a `gpt-*` child. If the
snapshot froze only the root class's default, that child would be handed a Claude
identifier and rejected by admission gate 1 (§2.3) — a hop failing for no reason other
than that the snapshot froze the wrong class's default. Freezing the map keeps selection
deterministic while letting each hop resolve the default for its own class.

**It must be the whole map as of the pinned revisions, not the subset "reachable by this
chain".** The snapshot is created and signed at root dispatch (§4.2), before any dispatch
decision has been made, and hops choose their children dynamically at run time. Which
classes a chain will reach is therefore unknowable when the signature is produced. An
implementation that froze a computed "reachable" subset — or, equivalently, one resolved
default per hop decided up front — would either have to re-sign mid-chain, which §4.7
forbids, or fail the first hop whose class fell outside the guess: the very mixed-harness
failure this rule exists to prevent. The class-keyed default map is small, bounded by the
number of registered harnesses, so freezing all of it costs nothing. What an implementation
may not do is freeze a single identifier, a per-chain subset, or a per-hop prediction.

So a hop asks the snapshot *which model was selected* and asks live policy *whether it
may still run*. A revocation or exhausted budget mid-chain fails the next hop actionably
under §2.3 rather than being grandfathered; conversely, a mapping edited mid-chain does
not change any hop of the running chain. PMM-06 owns the frozen half and PMM-07 the live
half, so neither story may move a row across this table without amending this note.

The live half is only enforceable because U4 puts a mandatory gateway bootstrap on every hop
(§4.2). The "re-evaluated at every hop" column is a requirement for a live authority
consultation per hop; without one, those rows would silently degrade into frozen ones, which
§4.1a names a security defect.

### 4.2 Who signs it, and what a worker actually receives

The trusted gateway or authority service creates the snapshot only after it authenticates
and resolves the root principal (D5). Workers never receive a private signing key or shared
signing secret.

U4 approves PMM-06's C2 shape, which fixes the mechanism precisely and is **not** an
offline-token design:

1. **Gateway work-admission resolves Postgres authority and persists the snapshot and its
   digest in worker-unwritable storage.** The durable policy record is not carried by the
   worker and cannot be edited by it.
2. **A mandatory gateway bootstrap call verifies workload, run and root** before the worker
   may use the policy, and returns a **fresh, audience- and chain-bound `adpe1` assertion**
   whose body digest is the snapshot digest.
3. **The assertion keeps a 30-second maximum TTL and is reissued per hop.**
4. **Workers receive no signer secret and no long-lived offline-verifiable policy token.**

The distinction is load-bearing rather than cosmetic. A long-lived signed policy token that a
worker verifies by itself would be a bearer credential valid for the life of a chain: it
could be replayed after the grant behind it was revoked, and revocation would have no
mechanism to take effect because nothing contacts the authority again. Per-hop reissue with a
30-second ceiling bounds both. It also supplies the enforcement point §4.1a depends on —
admission gates can only be re-evaluated live if something live is consulted at each hop, and
the bootstrap call is that something.

The cost is a gateway round trip per hop, accepted deliberately for dispatch-time work. Note
the interaction with §5.1's 10-second webhook budget: this per-hop call sits on the dispatch
path, not inside the webhook's response window, and PMM-07 may not satisfy it by adding a
gateway call to webhook validation.

`service_policy` is owned by the **canonical service principal** (U4, U1). Where a human
approved a grant to that principal, the human is **audit attribution only** and does not
become the policy owner.

### 4.3 Why worker-held shared HMAC is rejected

A worker-held shared HMAC is explicitly rejected because any worker possessing it could
impersonate another run's human or service-account lineage. The marker signer already
disables itself when `ADP_AGENT_AUTHORITY_ENABLED` is `true`, explaining that “A shared
HMAC key can impersonate another run's human lineage” and that protected dispatch uses
gateway authority (`modules/agent-factory/agent-worker-image/lib/marker_signing.py:46-52`).
The platform therefore recognized this trust problem in code before this design note
stated the ruling.

### 4.4 Required signed claims

The signed snapshot binds at least:

- tenant;
- root-principal kind and ID;
- root invocation or correlation ID;
- policy revision;
- persona-mapping and default revision;
- issued-at;
- expiry;
- intended audience; and
- harness identifier and version, or compatibility-contract revision, together with
  the resolved model and policy revisions (D6). This same harness/compatibility-class
  identifier is the **key** under which the frozen class-keyed default map (§4.1a) is
  bound and looked up per hop — no additional field is required, but the binding between
  each class key and that class's default must be inside the signature, so a hop cannot be
  handed another class's default without detection.

### 4.5 Verification at every hop

Every direct and descendant hop verifies the assertion signature, expiry, audience, chain and
root binding, and the snapshot digest before using the policy. A missing, invalid, expired,
mismatched, or modified snapshot or assertion fails closed with visible, actionable
diagnostics. No hop silently reconstructs policy or falls back to another policy source.

Verification is **not** self-contained at the worker. Under U4 (§4.2) a hop must complete the
**mandatory gateway bootstrap** — which verifies workload, run and root and mints that hop's
fresh short-lived assertion — before it selects a model from the snapshot. A worker that held
only a previously issued artifact has not satisfied this rule, and an implementation that let
a hop proceed on a still-unexpired assertion issued for an earlier hop would reintroduce the
replayable bearer token U4 rejects.

### 4.6 Enforcement dependency

PMM-06 may introduce snapshot generation and verification in report-only mode, but
enforcement is gated on the gateway-mediated delegated-authority path being live and
accepted. #3186 remains open and marked DO NOT TRIGGER YET, and #5195 remains open;
they are prerequisites for enforcement, not justification for a worker-side signing
mechanism. The chain-identity machinery itself, #3174 and its siblings, is merged.

### 4.7 Personas first encountered by a later hop

The case is a persona the root principal has no mapping for, reached by a hop partway
through a chain — not a persona that did not exist when the snapshot was signed. A
genuinely unknown persona cannot be valid in the signed catalogue, so it cannot be
resolved at all.

Such a hop uses the snapshot's system default **for that hop's own compatibility class**
(§1.7, §4.1a) — not the root hop's class default — provided the persona is valid in the
signed catalogue and policy that the snapshot's revisions pin. This is §2.2's rung 3
applied mid-chain: absence of a mapping row reaches the default for the target persona's
class, at any depth. If the chain crosses harness families, each hop resolves its own
class's default from the frozen class-keyed map (§4.1a); if that hop's class is absent from
that map or has no validated default in the pinned revisions, dispatch fails closed naming
the class rather than falling back across families or re-signing the snapshot. If the
persona is not valid under the pinned revisions, dispatch fails with an explicit
compatibility error rather than extending, re-signing or reconstructing the snapshot.

## 5. Reconciliations

### 5.1 The `/model` carve-out ruling (D2)

The lenient path is reversed, not carved out. Today an unresolvable directive logs
“handler: /model directive %r rejected (unknown or disallowed) — proceeding with
default model (lenient)” and continues
(`modules/agent-factory/webhook-ingress/lambda/github/handler.py:1784-1789`). It is
replaced by an actionable user-visible response naming the rejected request, the reason,
and permitted or available alternatives.

#2293, or a successor feedback path, is required before enforcement. Without it,
reversal trades a silent wrong model for a silent refusal: the worker entrypoint exports
`ADP_MODEL_REQUESTED` and `ADP_MODEL_RESOLVED`, but no consumer uses them
(`modules/agent-factory/agent-worker-image/entrypoint.py:1683-1687`). A valid `/model`
request survives as a legitimate, audited one-run override for the directly invoked hop
only. It does not persist and does not propagate.

**Correction to this story's premise — #2279 has no numbered rulings.** #5418's own
"Starting point and reuse" table attributes to #2279 a "locked ruling 8" that "forbade
per-user layers", and D2 attributes the leniency to "#2279 ruling 5". Neither exists.
#2279's body and comments contain no enumerated rulings at all; its decisions are prose
bullets. Because AC-03 asks a reviewer to verify this reconciliation against cited
evidence, this note cites the verifiable text instead of a ruling number, and §7.1
records the correction.

What #2279 actually decided, and what it means here:

- **It did not forbid a per-user layer — it reserved a slot for one**, and predicted the
  same relative order D2 now locks. Its precedence bullet reads: "explicit `/model` >
  (future per-user pref #1309) > per-persona registry default > pod `ANTHROPIC_MODEL`
  env", and its related-work line calls per-user preference "separate; this is the
  explicit per-trigger override that takes precedence over it". So this epic **fills the
  slot #2279 reserved** rather than reversing a prohibition, and §2.2's ordering
  (override above saved mapping) is continuous with #2279's intent, not a reversal of it.
- **The leniency decision is real but conditional.** Its validation bullet reads:
  "reject the model, run with default + a warning reply (lenient; keeps the task
  moving)". The leniency was explicitly paired with a warning reply — and that warning
  was never delivered (#2293 still open). The shipped behaviour is therefore *worse*
  than what #2279 decided: lenient without the compensating feedback. D2's reversal
  removes the leniency; #2293 supplies the feedback the original decision assumed.
- **The 10-second constraint stands and is documented in code**, which is stronger
  evidence than an issue ruling: "The Lambda must answer GitHub in <10s, so we validate
  locally (no HTTP call to the gateway)"
  (`modules/agent-factory/webhook-ingress/lambda/common/model_validate.py:1-10`). Any
  later story requiring a single resolver must explain how it preserves that budget.

### 5.2 The #5078 organization model-access arc (D1)

#5078 is the parent epic for the organization model-access arc. #5089 settles its
model-access authority, precedence, and dual-UI compatibility contract; #5091 adds
organization-scoped model-access APIs, grants, and authorization; and #5092 migrates
model access into personal and scoped-administrator journeys. These stories build the
constraint axis around this selection ladder, not a competing ladder.

#5089's own precedence decision remains open on its side. Its body says, “The prototype
ordering is a proposal, not an accepted enforcement change,” and its acceptance requires
“the outstanding precedence decision is resolved and recorded before enabling new
member-rule writes.” D1 settles precedence for this epic's selection ladder. #5089 may
still settle ordering among organization, team, and member rules, but it may not
reintroduce silent replacement of a principal-selected model. Consequently, PMM-02
requires no organization or team selection columns in the preference table.

### 5.3 The #1309 disposition (AC-05)

#1309 is explicitly superseded for agent personas. There is nothing to migrate because
it was never implemented: it produced no code, migration, or design-note file.

Its non-persona use cases are not covered here: `chat.conversational`,
`agent-context.ingestion`, `agent-context.wiki`, the LiteLLM proxy configuration, the
ingestion pods, and the OpenViking configuration
(`modules/agent-context/manifests/litellm-config.yaml`,
`modules/agent-context/images/ingestion/*.py`,
`modules/agent-context/manifests/agent-context-configmap.yaml`). They are out of scope
for this epic and need a successor if they are still wanted. This boundary follows the
key itself: this epic maps persona keys from the authoritative agent persona catalogue,
while those consumers are not agent personas. #1309 also did not define the
service-account owners or authoritative multi-agent chain semantics established here.

A successor story must re-derive #1309's consumer inventory rather than copy it: that
inventory has already gone stale. #1309 cites
`modules/agent-context/kubernetes/openviking-configmap.yaml`, which no longer exists at
that path — the OpenViking settings are now in the manifests configmap cited above. This
is a second reason the disposition is "supersede" rather than "absorb".

### 5.4 The inert allowlist statement and D3's answer (AC-06)

Registry `allowed_models`, the `X-Agent-AllowedModels` header, and
`persona_allowed_models` are unenforced today on all four paths:

1. `agent_entry_to_token_context()` does not copy `allowed_models` into the token
   context (`modules/gateway/src/auth/agent_registry.py:244-277`).
2. `ModelResolver._allowed_models_config` has no production writer; its setter is called
   only by tests (`modules/gateway/src/proxy/model_resolver.py:129`,
   `modules/gateway/src/proxy/model_resolver.py:259-264`,
   `modules/gateway/src/proxy/model_resolver.py:316-323`).
3. The API authorizer produces `X-Agent-AllowedModels` for agents and JWT users, but
   nothing consumes it (`modules/gateway/lambda/api-authorizer/handler.py:453`,
   `modules/gateway/lambda/api-authorizer/handler.py:517`).
4. Webhook validation calls `resolve_and_validate` with one argument, so
   `persona_allowed_models` is always `None`
   (`modules/agent-factory/webhook-ingress/lambda/github/handler.py:1777`,
   `modules/agent-factory/webhook-ingress/lambda/common/model_validate.py:49-52`).

D3 makes allowlists a real admission gate. The effective selectable set is the
intersection of the versioned platform-supported catalogue, active tenant or
organization allowlist, target-persona and runtime compatibility, applicable registered
service-account restrictions, and models authorized and freshly proven invocable
through the resolved AWS destination, account, and region. PMM-03 owns the canonical
catalogue, policy intersection, provenance and freshness, and migration from the inert
fields. PMM-07 owns consistent enforcement across every invocation path.

Rollout is report-only first. Existing principals are backfilled and compared before
PMM-09 flips enforcement. This epic must not add to the #4511 inert-config class: a
mapping is validated when authored, not stored without enforcement.

### 5.5 Harness compatibility (D6)

All current direct persona executions use the pinned Claude Agent SDK harness. The
initial mapping surface therefore permits only compatible Anthropic Claude models for
direct persona execution. `@agent-codex` is not an exception: its outer, supervising
agent remains the Claude Agent SDK worker, while Codex runs as a bounded delegated tool
through the existing bridge.

The gateway permits `openai.*` for that bridge path
(`modules/gateway/src/proxy/model_resolver.py:85-100`). That permission does not make a
non-Anthropic model persona-selectable; adding a model to the general catalogue alone
never establishes direct-harness compatibility. A future non-Anthropic outer agent
requires a separately implemented, registered, regression-tested, and operationally
approved harness adapter.

**The pinned Codex model is a delegated-tool default, not a second persona-execution class
(corrected in rev-5).** The agent-worker image bakes `model = "openai.gpt-5.6-sol"` into the
Codex CLI's own config, which Codex reads when the supervising persona delegates a bounded task
to it (`modules/agent-factory/agent-worker-image/codex-config.toml`, copied to
`/home/agent/.codex/config.toml` at `Dockerfile:224`). That value configures the *tool*. The
agent executing the persona is still the Claude Agent SDK worker: the persona's own instructions
state "You run on the same Claude SDK worker as every other persona" and describe Codex output as
"a *proposal*, never a commit" that the supervisor reviews
(`modules/agent-factory/rules/personas/codex.md:12-13,19-20`), and the bridge is gated to
explicitly requested, single bounded tasks
(`.claude/skills/codex-bridge/SKILL.md:3-10`). So the `codex-sdk` class has **no persona mapped
to it today**; the class ID is reserved so that #5433 cannot mint a competing spelling, and
mapping a persona into it is #5433's act together with its own separately proven default (U2).

Rev-4 of this note asserted the opposite — that a second compatibility class was "already live in
the tree" on the strength of that pin — and additionally credited the config test asserting the
literal (`modules/agent-factory/agent-worker-image/tests/test_codex_config.py:30-34`) as evidence
the Codex class was better proven than the Claude class. Both claims are withdrawn. A test that
asserts a configuration literal proves the file contains that literal; it is not invocability
evidence, which §1.7b requires to be a bounded invocation through the harness's actual request
shape. Treating a config assertion as proof is the precise error §1.7b and U3 exist to prevent,
and it contradicted this section's own ruling that `@agent-codex` is not an exception.

**#5433 is that case, and it is already open.** It defines first-class `gpt-*` persona keys
whose outer harness is Codex and whose family is OpenAI GPT, with no Claude support and no
Claude fallback as a permanent compatibility contract — `gpt-developer` and `developer` are
different selectable personas, not aliases. Two consequences bind this note:

1. **The default is class-keyed, not global** (§1.7, §6.4). #5433 names #5417 as owing a
   compatibility-class or harness-specific no-mapping default. A single global default would
   make every unmapped `gpt-*` invocation fail admission gate 1 permanently, reported as a
   platform-readiness error when nothing is wrong with the platform — the default was simply
   keyed wrong.
2. **Mixed-harness chains resolve per hop** (§4.1a, §4.7). #5433 §6 permits chains to cross
   families in both directions, so the snapshot freezes the whole class-keyed default map as
   of the pinned revisions and each hop applies its own class's default.

The compatibility class must stay derivable from the persona key, so this adds no column to
the preference table — the same shape D1 established when it kept organization scope out of
that table. **Who emits that derivation is settled: PMM-03, per U2** (§1.7a). #5433 registers
its `gpt-*` personas and its separately proven Codex/GPT default into that registry rather
than maintaining a competing one, so the platform holds one persona-to-class authority.

## 6. Operator decisions of record

The story was filed listing five open decisions, D1-D5. On 2026-09-18 the operator
locked six decisions on this issue, adding D6 for harness compatibility, which the story
did not anticipate. All six are locked; none blocks PMM-02.

### 6.1 D1 — separate selection and authorization axes

**Personal persona mapping selects a model; organization model access constrains it.**

PMM-02 must store only tenant-scoped human and service-account preferences, with no
organization or team selection columns. PMM-07 must apply organization, team, and
member policy as admission gates after selection and fail actionably rather than replace
the winner. This ruling is applied in §§0, 1.5-1.6, 2.1-2.5, 3.1, and 5.2; it unblocks
PMM-02 and gates PMM-07's resolver integration.

### 6.2 D2 — fail closed and reverse leniency

**An invalid one-run model request fails closed; the lenient fallback is reversed.**

PMM-07 must stop before model work and return the rejected request, reason, and
permitted or available alternatives; it may not substitute a saved mapping or the
system default. PMM-06 must keep descendants on the trusted root snapshot rather than
propagate the direct override, and its acceptance wording must account for the feedback
dependency in §6.7. This ruling is applied in §§0, 1.8, 2.2, 2.4, 2.6, 3.3, and 5.1; it
gates PMM-06's enforcement wording and PMM-07's fail-closed enforcement.

### 6.3 D3 — make allowlists a real gate

**Model allowlists become an enforced admission gate, not stored advisory data.**

PMM-03 must define the versioned catalogue, effective policy intersection, provenance,
freshness, and migration from today's inert fields. PMM-07 must enforce that
intersection on every invocation path, while PMM-09 must keep rollout report-only until
existing principals have been backfilled and compared. This ruling is applied in §§0,
1.6, 2.3-2.4, and 5.4; it unblocks PMM-02 and gates PMM-03, PMM-07, and PMM-09.

### 6.4 D4 — candidate Sonnet 4.6 default for the `claude-agent-sdk` compatibility class

**D4 names `us.anthropic.claude-sonnet-4-6` as the canonical system default for the
`claude-agent-sdk` compatibility class, used only when the mapping row is absent.** The
default contract is a lookup keyed by compatibility class (§1.7), not one global identifier.
D4 sets the value for the only class that executes personas directly today; it does not and
cannot set a default for a class that does not yet exist.

**U2 qualifies D4's status: Sonnet 4.6 is a Claude-class *candidate*, not an active proven
default, until PMM-09 records a bounded invocation using the actual Claude harness request
shape** (§1.7b). D4 settles *which* identifier is intended; the proof settles whether it may
be relied on in an enforcing posture. No sibling story may describe it as a validated or
active default before that record exists.

Each new compatibility class — Codex/GPT under #5433 being the first — must have its own
separately proven canonical default, by its own real-harness invocation, before any persona in
that class becomes configurable. A missing or invalid class default fails closed naming the
class, and there is never a cross-harness fallback. #5433 names #5417 as owing this contract;
§§1.7, 2.2, 2.4, 2.6, 4.1a, 4.4 and 4.7 carry it.

D4 sets a *value* per class; U2 supplies the *key* and the split. PMM-03 owns the
persona→class registry and the class IDs (§1.7a); **PMM-02 stores the versioned default and
posture record keyed by class**; PMM-09 records the invocation that promotes candidate to
proven.

The `us.` profile is preferred over `global.` for account portability. Bootstrap must
prove it with a bounded real invocation using the actual runtime request shape; listing
the model or profile is insufficient, as #2300 demonstrated. Unavailability is a loud
platform-readiness error, while Opus and Fable remain explicit validated preferences
and never fallback candidates.

#4673 should correct the current unsafe default and silent-hang behaviour before the
complete rollout, and PMM-09 must consolidate the remaining divergent hard-coded
defaults. #1128's Opus 4.8 proposal is not the default because its ADP smoke failed on
incompatible thinking parameters and was reverted; #2684's performance evidence
supports Sonnet 4.6. This ruling is applied in §§0, 1.7, 1.7b, 2.2, 2.4, 2.6, 4.1a, 4.4, and
4.7. PMM-03 owns the class registry and per-class compatibility evidence; PMM-02 owns the
stored class-keyed default record; PMM-09 owns the proving invocation, consolidation and
rollout, with #4673 preceding its complete rollout.

### 6.5 D5 — gateway-signed snapshot

**The trusted gateway or authority signs the immutable root policy snapshot; workers never hold signing secrets.**

PMM-06 must bind the root principal, invocation, policy revisions, validity window,
audience, and snapshot integrity, then make every hop verify them. It may begin in
report-only mode, but enforcement must wait for the accepted gateway-mediated authority
path rather than introduce worker-side shared HMAC signing. This ruling is applied in
§§0, 1.4-1.5, 1.9, 3.2-3.4, and 4.1-4.7; it gates PMM-06 enforcement and PMM-07's use
of the verified snapshot.

**U4 fixes the mechanism D5 left open by approving PMM-06's C2 shape** (§4.2): gateway
work-admission resolves Postgres authority and persists the snapshot and digest in
worker-unwritable storage; a mandatory gateway bootstrap verifies workload, run and root and
returns a fresh, audience- and chain-bound `adpe1` assertion; the 30-second maximum TTL is
retained and the assertion is reissued per hop; and workers receive **no signer secret and no
long-lived offline-verifiable policy token**. D5's "workers never hold signing secrets" is
therefore necessary but not sufficient — a worker holding a long-lived verifiable policy token
would satisfy D5 and still violate U4. `service_policy` is owned by the canonical service
principal, with the approving human as audit attribution only.

### 6.6 D6 — harness compatibility

**A model is selectable only when the executing persona harness has a registered and validated compatibility contract for it.**

PMM-03 must catalogue compatibility and invocability evidence; PMM-04 and PMM-05 must
show only compatible choices while leaving the service authoritative. PMM-06 must bind
the harness or compatibility revision into the snapshot, and PMM-07 must validate it at
mapping resolution and again for each hop's actual harness. This ruling is applied in
§§0, 1.3, 1.7, 2.3-2.4, 4.1a, 4.4, 4.7, and 5.5; it gates PMM-03 through PMM-07.

D6 is why the system default is keyed by compatibility class (§1.7): a default that is not
class-keyed is the one selection outcome that could hand a persona a model its harness has
no validated contract for. #5433's `gpt-*` family is the first class beyond the pinned
Claude Agent SDK harness.

### 6.7 Prerequisites this note does not resolve

| Item | Verified status | Owner | PMM story gated |
|---|---|---|---|
| #2293 feedback channel | Open | #2293 or a successor feedback path | D2 enforcement in PMM-07 and the acceptance wording in PMM-06 |
| #3186 + #5195 authority path | Open; #3186 is marked DO NOT TRIGGER YET | #3186 and #5195 | PMM-06 snapshot enforcement, but not report-only work |
| #4673 unsafe worker default | Open | #4673, then PMM-09 | Complete PMM-09 rollout |
| #5089 internal precedence among organization, team, and member rules | Open on #5089's side | #5089 in the #5078 arc | Nothing in this ladder; it must not reintroduce silent replacement |
| Make the allowlist gate real | Required; current fields are inert | PMM-03 catalogue and PMM-07 enforcement | PMM-03 and PMM-07 |
| Bounded real-harness invocation proving the `claude-agent-sdk` class default | Not yet recorded; Sonnet 4.6 is a **candidate** (§1.7b, U2) | PMM-09 | Enforcing posture only. Report-only rollout may proceed with the candidate |
| Approved target account and spend ceiling for invocability probes | Not yet approved; probing ships disabled with a zero spend budget (U3) | Operator, then PMM-09 | Enabling any probe cadence. Not PMM-03's catalogue design |

**No precedence or vocabulary decision inside this note's scope remains open.** Rev-3 carried
the persona-to-class *ownership* question here as an open operator decision; **U2 settled it** —
PMM-03 owns the persona→class registry, PMM-02 stores the class-keyed default records, and
#5433 registers `gpt-*` personas into PMM-03's registry rather than keeping a second one
(§1.7a). It is recorded in §9 as an adopted ruling, not a prerequisite.

The rows above are **external dependencies and sequencing**, not unresolved design questions:
each has a named owner and a defined completion condition. One narrower question survives U2
and is recorded next, rather than being absorbed into a blanket closure claim.

### 6.7a The one cross-epic question U2 does not settle

U2 settles *who owns* the persona→class binding: PMM-03. It does not settle whether PMM-03's
registry is the authoritative source or a projection of #5433's persona/harness registry, which
#5433 independently defines. PMM-03's own head records the same residual question at the same
scope, in its section 2.4b "Unresolved cross-epic ownership — operator decision, non-blocking"
(`docs/design-notes/5420-persona-and-model-catalogue.md` at `25ece717`), and this note agrees
with its framing.

**Why it is not a blocker.** It decides where the registry file lives, not whether the binding
can be built or what it must contain. Either way the requirement is identical and is binding
here: **one registry and one class-ID vocabulary.** If #5433's registry becomes authoritative,
PMM-03's catalogue projects it and keeps the same class IDs.

**What must not happen** is both epics independently minting a spelling of `codex-sdk`. Two
registries claiming one binding is how the platform ends up holding two answers to "which model
does this run use" — the failure this epic exists to eliminate. PMM-03's reservation of
`codex-sdk` as a class ID with no persona mapped to it is the mitigation already in place
(§5.5). The operator's decision is needed before #5433 registers its first `gpt-*` persona, not
before PMM-02 or PMM-03 begin.

D1 and D3, the two decisions the story named as gating PMM-02, are both **ANSWERED**.
PMM-02 is unblocked: its preference row is `(tenant, kind, id, persona) -> model` and gains no
class column. PMM-02 additionally now owns two things U1 and U2 assign it — the canonical
service-principal identity slice and the versioned class-keyed default/posture records — which
add scope but remove blockers, since the class vocabulary its own design note flags as missing
is supplied by PMM-03 under U2.

### 6.8 Class-keyed default: what each sibling story must carry

The class-keyed default contract (§1.7) and the U1-U6 rulings land on specific sibling
stories. This table is stated against each sibling's **current design-note head as of
2026-09-18**, not against the filed story text, because several have already revised:

| Story | State at its current head | Must carry |
|---|---|---|
| PMM-02 (#5419) schema | Already keys a settings record by `harness_compatibility_class` with a UNIQUE constraint — "one authoritative record per class" — and its §8.4 flags the **missing class vocabulary as blocking**, having verified no `harness_id`, `compatibility_class` or `PERSONA_TO_RUNTIME` exists in the tree | That blocker is **answered by U2**: PMM-03 supplies the vocabulary (`claude-agent-sdk`, `codex-sdk`) and PMM-02 stores the versioned default/posture record keyed by it. Also adds U1's canonical service-principal identity slice. Its *preference* row still gains no class column |
| PMM-03 (#5420) catalogue | **Has since adopted U2 and U3 at its head (rev-3, `25ece717`) — corrected in rev-5, which previously reported all four items below as still outstanding.** Its new §2.4 defines the persona→class registry with stable unversioned class IDs and a separate `harness_contract_revision`; **persona rows now carry `compatibility_class`**; §3.3 seeds structure and a *candidate* identifier with an empty invocability slot rather than a proven default; the probe ships **disabled at a zero spend budget** with enablement moved to PMM-09; and it now cites #5433 directly. It also states the same persona/harness fact this note had wrong: all twelve personas resolve to `claude-agent-sdk`, and `codex` is not an exception (§2.4) | No outstanding obligation from this table. What it retains and this note endorses: `codex-sdk` "registered as a class ID with **no personas mapped to it**", so #5433 cannot mint a second spelling; no cross-class fallback; and the class derived from the persona key rather than stored on a preference row. Its one open item is the narrower cross-epic question in §6.7a, not a gap against U1-U6 |
| PMM-07 (#5425) resolver | Already takes `class_defaults` as a class-keyed mapping, treats Sonnet as the Claude-class candidate, refuses a snapshot carrying a singular default, and states it "must not invent a class taxonomy the harness story will own" — leaving the taxonomy owner as an open question against #5433 | That open question is **closed by U2 in PMM-03's favour**: the class taxonomy and the persona→class registry are PMM-03's, so PMM-07 resolves the class from PMM-03's registry rather than #5433's. Its refusal to consume a singular default is correct and retained. Must never probe to manufacture missing freshness evidence (§2.3, U3) |
| PMM-06 (#5424) snapshot | Already designs exactly the shape U4 approves — gateway-side Postgres resolution, `snapshot_digest` on a worker-unwritable authority record, mandatory bootstrap before any selection, per-hop `adpe1` reissue with the 30-second TTL retained, and an explicit rule that "no long-lived, worker-verifiable snapshot token is issued, at any hop" — and carries it as condition C2/C3a awaiting approval | **U4 approves C2**, so those conditions are answered rather than pending. Its C7 (key the default by class) is likewise now settled. Additionally freezes the class-keyed default map as of the pinned revisions (§4.1a) — never a per-chain "reachable" subset — bound under the class key in the signed claims (§4.4), and separates the stable class ID from the versioned contract revision, which its current single `harness_compatibility_revision` field conflates |
| PMM-09 (#5427) rollout | Already treats the Claude-class default as unproven, requiring "a bounded real invocation of the exact literal … using the runtime request shape (the Claude-harness path …)". It also inventories the `openai.gpt-5.6-sol` pin in the worker image **and draws the boundary this note had lost**: "`openai.gpt-5.6-sol` is today a *tool* default, not a persona-execution default", with native `gpt-*` personas belonging to #5433 (the boundary paragraph in its own section 3.5, `docs/design-notes/5427-pmm09-default-consolidation-and-enforcing-flip.md` at `86c7959a`, a sibling branch file not present on this branch) | Records the bounded real-harness invocation that promotes each class default from candidate to proven (§1.7b); consolidates per class and never collapses classes to one identifier; owns the point at which probe cadences may be enabled after account and spend-ceiling approval (U3). **Its section 3.5 heading — "the second harness class already has a default" — overstates what that section's own boundary paragraph then correctly limits**; that heading is the wording this note's rev-4 over-read, and PMM-09 should align it with §5.5 here |
| PMM-04 (#5422) UI and PMM-05 (#5423) CLI | Not re-read at head for this revision — no claim is made here about their current text | Both consume U6's single contract and shared refusal vocabulary rather than defining their own: the self routes exist once at `/me/persona-models`, and a class-keyed default means the UI must show *which class* a shown default belongs to rather than one platform-wide value. Neither may invoke a model to render a page (U3), and both surface D2 refusals verbatim through `explain` (§2.4) |

All five siblings re-read here have adopted U1-U6 at their current heads, and several conditions
earlier revisions of this note listed as pending are answered rather than outstanding. **Rev-4
named PMM-03 as carrying the remaining work; that is no longer true** — its rev-3 head applies all
four items rev-4 listed. What remains across the epic is the external sequencing in §6.7 (the
#2293 feedback channel, the #3186/#5195 authority path, #4673, PMM-09's proving invocation, and
operator approval of a probe account and spend ceiling) plus the narrower cross-epic registry
question in §6.7a — not a design gap in any sibling against these rulings.

## 7. Corrections to this story's premise

This note's value is that every claim is checkable. Where the filed story's own text
does not survive a read of the tree or the referenced issues, the divergence is recorded
here rather than silently propagated into eight downstream stories.

### 7.1 The #2279 ruling numbers do not exist

The story cites “ruling 8” and “ruling 5”, but #2279 has no enumerated rulings. As §5.1
details, #2279 reserved a future per-user slot and predicted the same relative order D2
locks, so this epic fills that slot rather than reversing a prohibition. Its leniency
decision was paired with a warning reply that was never delivered, making shipped
behaviour worse than the original decision.

### 7.2 No `testing` persona

The epic's example row “Reviewer/testing persona” maps to `reviewer`. Section 1.1 quotes
the complete set of exactly 12 persona keys, and none is `testing`.

### 7.3 The allowlist gate is described by the epic as working and is not

Section 5.4 records the four inert paths. This correction has the largest downstream
consequence: a schema or resolver built on the assumption that a working gate already
exists would inherit the #4511 inert-config class rather than fix it.

## 8. Acceptance mapping and verdict

### 8.1 Acceptance mapping

| Acceptance | Sections satisfying it | Evidence for reviewer |
|---|---|---|
| AC-01 — precedence ladder with exactly one winner and absence as the final rung | §§2.1-2.6, 4.1a, 4.7, 6.1-6.2 | Confirm selection produces one candidate, gates never substitute it, and only a missing row reaches the default — including mid-chain (§4.7), with frozen selection separated from live admission (§4.1a), and with the default resolved per compatibility class (§1.7) so a cross-family hop is neither substituted nor permanently failed. |
| AC-02 — nine vocabulary terms defined once, with the persona list quoted and no invented `testing` persona | §§1.1-1.9, 7.2 | Check that each of the nine terms AC-02 names is defined exactly once and appears in the owner table, and that the persona list is the complete 12 keys. **Compatibility class (§1.7) is a tenth term this note adds**, required by the class-keyed default, with `candidate`/`proven` default status as a supporting distinction (§1.7b). Both are additive to AC-02; the owner table assigns the class to PMM-03 per U2. |
| AC-03 — `/model` reconciliation citing `handler.py:1784-1789` and naming #2293 | §§5.1, 6.2, 7.1 | Check the current lenient log citation, the undelivered feedback dependency, and the corrected #2279 account. |
| AC-04 — #5078 reconciliation naming #5089/#5091/#5092/#5078, with #5089's open decision noted | §§5.2, 6.1, 6.7 | Confirm the organization arc constrains selection and its internal precedence remains open without blocking this ladder. |
| AC-05 — #1309 disposition with non-persona cases listed | §5.3 | Check that persona selection is superseded and the six non-persona consumers remain out of scope. |
| AC-06 — inert-allowlist statement with four citations and D3's answer | §§5.4, 6.3, 7.3 | Check all four broken paths, the real-gate ruling, and the PMM-03/PMM-07 ownership split. |

### 8.2 Plausible wrong results that fail review

1. A note that lists the ladder but leaves the organization-scoped arc unmentioned
   fails review; §5.2 forecloses it by reconciling #5078, #5089, #5091, and #5092 with
   the separate authorization axis.
2. A note that describes tenant allowlists as an existing working gate fails review;
   §§5.4 and 7.3 foreclose it by recording the four inert paths and the required repair.
3. A note that fixes one global system default for every persona fails review; §§1.7, 2.2,
   2.4, 4.1a, 4.7, 5.5, 6.4 and 6.8 foreclose it by keying the default to the compatibility
   class, since #5433's `gpt-*` family may take neither a Claude default nor a
   cross-harness fallback.
4. A note that keys the default by compatibility class while *asserting* that the
   persona-to-class lookup already exists in code fails review for the same reason as finding
   2: it would hand PMM-02 and PMM-07 an unenforceable contract and reproduce the #4511
   inert-config class. §1.7a forecloses it by naming PMM-03 as the owner who must **build**
   the persona-row class attribute, while recording that no merged code carries it today.
5. A note that treats `us.anthropic.claude-sonnet-4-6` as an already-validated default fails
   review; §1.7b forecloses it by holding the value at **candidate** until PMM-09 records a
   bounded invocation through the actual harness request shape, on the #2300 precedent that
   listing availability is not invocability.
6. A note that lets a worker verify a long-lived policy token by itself fails review; §§4.2
   and 4.5 foreclose it with U4's mandatory per-hop gateway bootstrap and 30-second assertion
   TTL, because an offline-verifiable chain-lifetime token cannot be revoked mid-chain.
7. A note that counts a **delegated tool's** configured model as evidence of a second live
   persona-execution class fails review — and rev-4 of this note failed it. §5.5 forecloses it:
   the Codex pin in the worker image configures a tool the Claude-SDK worker calls, `codex-sdk`
   has no persona mapped to it, and #5433 both maps the first one and proves its default
   separately (U2). A reader who believed otherwise would skip exactly that registration and
   proof, and offer a model for a harness nothing has run.
8. A note that treats a **test asserting a configuration literal** as invocability evidence
   fails review; §1.7b forecloses it by requiring a bounded invocation through the harness's
   actual request shape. Rev-4 cited the Codex config test as making that class better proven
   than the Claude class; §5.5 withdraws the claim. A config assertion proves the file's
   contents, which is the #2300 / U3 confusion of listing for invocability in miniature.

### 8.3 Verdict

This note is the **binding PMM-01 vocabulary and precedence baseline** for epic #5417. It
is not by itself the complete #5417 system design: the eight sibling stories extend it, and
the cross-story architecture synthesis builds on it. What is settled here — the vocabulary,
the selection/admission separation, the fail-closed ladder, the compatibility-class-keyed
default contract, the candidate-versus-proven default standard, the chain-snapshot trust root
and its per-hop gateway bootstrap, the root-identity rules for automated runs, and the D1-D6
plus U1-U6 record — is binding on those stories. None of them may move a row across §4.1a's
frozen/live boundary, reintroduce a global default, describe a candidate default as proven, or
hand a worker a long-lived offline-verifiable policy token without amending this note.

**No precedence or vocabulary decision inside this note's scope remains open.** Rev-3 left the
persona-to-class owner unresolved; U2 settled it in PMM-03's favour, with PMM-02 storing the
class-keyed default records (§1.7a, §9). Rev-3's *ownership* conflict with #5433 is closed:
#5433 registers personas into PMM-03's registry rather than maintaining a second one. One
narrower cross-epic question survives — whether PMM-03's registry is the source or a projection
of #5433's — and is recorded at that scope in §6.7a rather than claimed closed; it determines
where the registry lives, and is needed before #5433 registers its first `gpt-*` persona.

PMM-02 is unblocked because D1 and D3 are answered and its preference row gains no class
column; the class vocabulary its own note flags as blocking is supplied by PMM-03 under U2.
What remains in §6.7 is external sequencing with named owners — the #2293 feedback channel,
the #3186/#5195 authority path, #4673, PMM-09's proving invocation, and operator approval of a
probe account and spend ceiling — not unresolved design.

Merging this note is documentation only and is **not deployment approval** for any sibling
story. In particular, the candidate class default and the disabled-by-default probe posture
mean nothing here authorizes a paid invocation.

### 8.4 How this note was produced and reviewed

Drafted by the Codex CLI under @agent-codex supervision, in three bounded delegations
(front matter with §§0-2; §§3-5; §§6-8), each diff reviewed before the next was
commissioned. Every file-and-line citation was then checked mechanically against the
tree, and each quoted string matched against its source, by the supervisor.

An independent Codex review pass over the finished document returned approve with three
non-blocking clarifications, all of which were adopted rather than deferred:

| Finding | Resolution |
|---|---|
| Snapshot determinism overstated — live budget, rate, compliance and destination checks were not distinguished from frozen mapping policy, so an implementation could honour a revoked grant | §4.1a added: an explicit frozen-versus-live table, with the rule that freezing an admission gate is a security defect |
| Resolving an alias "before it is audited" risks losing the invoker's original request | §1.2 now requires both the alias as entered and the canonical ID to be retained, since D2's rejection message must name what was asked for |
| "Persona introduced after snapshot creation" was ambiguous — a genuinely new persona cannot be valid in a already-signed catalogue | §4.7 retitled and rewritten as "first encountered by a later hop", tying it to §2.2 rung 3 applied mid-chain |

Two citation ranges from the drafting brief were also corrected during review
(`model_validate.py:1-11` to `:1-10`, and `:35-39` to `:34-37`), and one stale path
inherited from #1309 was replaced and then documented as stale in §5.3.

### 8.5 Rev-2 — the compatibility-class default correction

Rev-1 was reviewed on PR #5436 by the epic operator, who requested changes, and by the
architecture approval gate (`@agent-architect`), which independently reached the same
blocker and specified the corrections. Both found the vocabulary, the selection/admission
split, the fail-closed ladder and the D1-D6 record sound, and the architecture gate
re-verified every load-bearing citation against the branch without finding one wrong.

The defect: Rev-1 fixed **one global** system default, `us.anthropic.claude-sonnet-4-6`.
#5433 defines `gpt-*` personas on the Codex harness with Claude support and Claude fallback
permanently prohibited, and names #5417 as owing a compatibility-class-keyed default. The
consequence was not unsafe — §2.3 orders harness compatibility as admission gate 1, so a
cross-family default is rejected rather than silently substituted — but it was
**unimplementable**: every unmapped `gpt-*` invocation would fail permanently, reported as a
platform-readiness error when the default was merely keyed wrong.

| Rev-2 change | Sections |
|---|---|
| System default redefined as a lookup keyed by compatibility class; class derivable from the persona key, so no preference column is added; each class needs its own validated default; missing class default fails closed; never a cross-harness fallback | §1.7, §0 — *rev-3 corrected "derivable" from an asserted fact to a requirement with a named gap (§1.7a)* |
| Rung 3 and the decision table resolve the default per class; a new row covers a class with no validated default yet | §2.2, §2.4, §2.6 |
| Snapshot stops freezing one default identifier, so mixed-harness chains permitted by #5433 §6 do not fail at a cross-family hop. **The binding rule is §4.1a's: freeze the whole class-keyed default map as of the pinned revisions.** Rev-2's per-chain "reachable set" and per-hop-resolved formulations were both superseded by rev-3 and are not alternatives — neither is computable when the signature is produced | §4.1a |
| A hop first encountering a persona resolves **its own** class's default | §4.7 |
| The existing harness/compatibility claim is stated as the **key** binding each class's default inside the signature; no new field invented | §4.4 |
| D4 restated as canonical for the Claude Agent SDK class only; #5433 named as the first new class; PMM-03 made owner of the class registry and per-class defaults | §6.4, §6.6, §5.5, §1.7 owner table — *rev-3 downgraded PMM-03 from settled owner to proposed owner of the persona-to-class binding, since #5433's registry claims it too (§6.7)* |
| Per-story consequences tabled for PMM-02/03/06/07/09, noting #5434 already seeds the singular default | §6.8 |
| Scope marked as the binding PMM-01 baseline extended by the eight-story synthesis, replacing an unqualified "design-complete" | front matter, §8.3 |

Rev-2 was authored by `@agent-reviewer` during review of PR #5436, under the reviewer's
own-the-repair contract, after the architecture gate completed and released the branch. The
architecture gate's ruling that the default must be class-keyed is adopted as specified; no
part of it was deferred. Because the reviewer authored these edits, the independent approval
required by repository policy remains outstanding and is named on the PR.

### 8.6 Rev-3 — verifying rev-2 rather than assuming it

Rev-3 was authored by `@agent-architect` on operator instruction to revise this PR in place
against every blocking review comment. Rev-2's three specified corrections were re-checked
and stand: the default is class-keyed, the mid-chain default is per hop, and the note is
scoped as the PMM-01 baseline. Every load-bearing citation was re-verified mechanically —
all 25 path citations resolve except the deliberately-cited stale OpenViking path, the
persona set is 12 keys with no `testing`, the quoted lenient log line and HMAC comment are
verbatim, the one-argument `resolve_and_validate` call is as described, and every `§N.N`
cross-reference resolves.

Three defects survived rev-2, each inherited from the review that specified it rather than
introduced by it:

| Rev-3 correction | Why it mattered | Sections |
|---|---|---|
| The persona-to-class lookup was asserted as an existing capability of PMM-03's catalogue. It does not exist: #5420 never mentions a harness, #5434 carries one only on *model* rows (the inverse lookup), and no persona-to-harness map exists in the tree. Restated as a requirement with a named gap, plus the competing claim in #5433's registry routed to the operator | A class-keyed default whose key has no owner is unresolvable. PMM-02/PMM-07 building against "already derivable" would reproduce the #4511 inert-config class this note exists to warn against | §1.7, **§1.7a (new)**, §1.9 owner table, §5.5, §6.4, §6.7, §6.8, §8.2 |
| The snapshot froze the class-default set "reachable by the chain". The snapshot is signed at root dispatch before any dispatch decision exists, and hops choose children dynamically, so reachability is unknowable at signing time. Changed to the whole class-keyed map as of the pinned revisions | A computed "reachable" subset would force either mid-chain re-signing (which §4.7 forbids) or failure of the first hop outside the guess — the exact mixed-harness failure the class-keyed default was introduced to prevent | §4.1a, §4.4, §4.7, §6.8 |
| "Compatibility class" became a load-bearing tenth term with no owner row and an unchanged nine-term acceptance line | A term that carries the default contract but appears in no ownership table is how a contract ends up with no implementer | §1.9 owner table, §8.1 (AC-02) |

Rev-3 changes no operator decision: D1-D6 stand as locked, and the class-keyed default
ruling is kept exactly as the gate specified. It records one prerequisite the gate's own
correction created and did not close (§6.7), and leaves the independent approval outstanding.

**Superseded in part by rev-4:** the first row above describes the state of the tree and of
the sibling stories on 2026-09-18 before the operator's unified rulings. The *code* gap it
reports is still accurate — no persona-to-harness map exists in the tree — but the
*ownership* question it routed to the operator has since been answered: U2 assigns the
persona→class registry to PMM-03 and #5433 registers into it (§1.7a). The other two rows
stand unchanged.

### 8.7 Rev-4 — adopting the unified epic rulings

Rev-4 was authored by `@agent-architect` on operator instruction, against the second-pass
CHANGES_REQUESTED review at head `78787bd1` and the binding unified-rulings comment on #5417. It
adopts rulings U1-U6 (§9), **closes the one decision rev-3 left open**, and reconciles every claim
about a sibling story against that story's **current branch head** rather than its filed text.

| Rev-4 correction | Why it mattered | Sections |
|---|---|---|
| Rev-3 carried the persona-to-class owner as an unresolved operator decision and a live conflict with #5433. **U2 settles it: PMM-03 owns the persona→class registry; PMM-02 stores the versioned class-keyed default/posture records; #5433 registers `gpt-*` personas into PMM-03's registry** | A baseline that leaves its load-bearing class owner unresolved cannot be binding — PMM-02's and PMM-07's own notes were each blocked on exactly this, and PMM-07 had explicitly declined to own the taxonomy | §1.7, §1.7a (rewritten), §1.9 owner table, §5.5, §6.4, §6.7, §6.8, §8.2, §8.3 |
| Sonnet 4.6 was stated as the locked canonical default. It is a **candidate** until PMM-09 records a bounded invocation through the actual Claude harness request shape | Seeding an unproven identifier platform-wide is the #2300 failure — listing availability is not invocability — and would make the inert-config class (#4511) the default posture | **§1.7b (new)**, §0, §1.7, §6.4, §6.7, §8.1, §8.2 |
| Class identity and harness revision were conflated under one "compatibility class" concept. Class IDs are **stable and unversioned**; the contract revision is a **separate versioned field** carried in evidence and snapshot keys | A versioned class ID would invalidate every persona binding and stored default on each harness upgrade, and with no cross-class fallback that fails every persona on the upgraded harness | §1.7, §2.3, §4.1a table, §6.8 |
| §§4.2/4.5 described workers verifying a signed snapshot offline. **U4 approves the gateway-bootstrap shape**: snapshot and digest in worker-unwritable storage, mandatory per-hop bootstrap returning a fresh audience- and chain-bound `adpe1` assertion, 30-second maximum TTL, no signer secret and **no long-lived offline-verifiable policy token** | A chain-lifetime token a worker verifies alone is a replayable bearer credential for model policy that survives revocation of the grant it was minted under. It also supplies the live consultation point §4.1a's re-evaluated gates require | §4.2 (rewritten), §4.5, §4.1a, §6.5 |
| Root identity for automated runs was unspecified beyond "the service account remains the root". **U5**: a human-initiated event preserves the canonical human root; a run with no authenticated human initiator resolves a tenant-bound registered canonical service principal and **fails closed if unregistered**; the executing App identity is audit attribution only. **U1**: preferences are owned by an opaque `canonical_service_principal_id`, never a raw account row, agent name, client ID or ARN | Without this, a nightly job running under a shared platform credential could inherit whichever human's preferences were attached to it, and re-registering a service account could silently transfer saved preferences | §3.2, §1.9 owner table |
| Probe safety was absent, while admission gate 5 requires fresh invocability proof. **U3**: probing ships disabled with a zero spend budget, no page load invokes a model, cadences enabled only in PMM-09 after account and spend-ceiling approval, and pricing/listing/agreement status is never proof | Gate 5 is the only gate whose evidence costs money. A resolver treating missing freshness as licence to probe would let a UI render or an unmapped invocation spend against an unapproved budget | §2.3, §6.7, §6.8 |
| §6.8's per-story table described filed story text, which the review flagged as stale | Four of the five sibling notes had revised; several conditions this note listed as pending were already designed or already answered, and PMM-03 — not the others — is where the remaining work sits | §6.8 (rewritten against current heads) |

Rev-4 changes no operator decision: D1-D6 stand as locked and the class-keyed default ruling is
kept as specified. It closes rev-3's open prerequisite, records U1-U6, and leaves the independent
approval outstanding because the architect authored these edits.

### 8.8 Rev-5 — withdrawing the second-live-class claim

Rev-5 was authored by `@agent-architect` on operator instruction, against the third-pass
CHANGES_REQUESTED review at head `8edc055c`. It changes no operator decision and no ruling:
D1-D6 stand locked and U1-U6 stand adopted. It withdraws one factual claim rev-4 introduced,
corrects one stale sibling report, and narrows one overclaimed closure.

| Rev-5 correction | Why it mattered | Sections |
|---|---|---|
| **Rev-4 claimed a second compatibility class was "already live in the tree"** on the strength of `openai.gpt-5.6-sol` pinned in the agent-worker image. Withdrawn. That value configures the **Codex CLI as a delegated tool**; the agent executing the `codex` persona is the same Claude Agent SDK worker as every other persona. `codex-sdk` is a **reserved class ID with no persona mapped to it**, and U2 assigns native `gpt-*` persona registration *and* a separately proven Codex default to #5433 | A sibling reading rev-4 would treat the second class as populated and proven and skip the separate registration and separate invocability proof U2 requires — offering a model for a harness nothing has executed a persona on. It also contradicted this note's own §5.5, which already ruled `@agent-codex` is not an exception. A baseline answering one question two ways is not binding | **§5.5 (expanded)**, §0, §1.7, §6.8, §8.2 (new findings 7-8) |
| **Rev-4 credited the Codex config test as making that class better proven than the Claude class.** Withdrawn. A test asserting a configuration literal proves the file contains the literal; §1.7b requires a bounded invocation through the harness's actual request shape | This is the #2300 / U3 error — listing or configuration status counted as invocability — committed two sections after §1.7b forbids it. Left standing, it would license every sibling to treat a pin test as proof | §5.5, §8.2 finding 8 |
| **Rev-4 reported PMM-03 as "the furthest from these rulings" carrying four outstanding items.** Its rev-3 head (`25ece717`) has applied all four: persona rows carry `compatibility_class`, §2.4 defines the class registry, §3.3 seeds a candidate with an empty evidence slot, the probe ships disabled at a zero budget, and it cites #5433 | Reporting completed sibling work as outstanding misdirects the operator about where remaining work sits, as surely as the reverse. PMM-03's head also states the same persona/harness fact rev-4 had wrong | §6.8 (PMM-03 and PMM-09 rows, closing paragraph), §1.7a |
| **Rev-4 asserted "no decision inside this note's scope remains open."** Narrowed: no *precedence or vocabulary* decision remains open, and the one surviving cross-epic question — whether PMM-03's registry is the source or a projection of #5433's — is recorded at its own scope | Overclaiming closure is what produced this round's defect. The question decides where the registry lives, not whether it can be built, and is needed before #5433 registers its first `gpt-*` persona | **§6.7a (new)**, §6.7, §8.3 |
| A rev-2 history row still carried the superseded per-chain "reachable set" and per-hop formulations beside the ruling that replaced them | A reader scanning the revision table for the snapshot rule could take away a formulation §4.1a rejects as uncomputable at signing time | §8.5 |

The independent approval remains outstanding: the architect authored these edits.

## 9. Unified epic rulings adopted (U1-U6)

The epic operator published binding unified architecture rulings on #5417 closing the cross-story
questions the second-pass reviews exposed. They are binding inputs to the canonical #5417 design
and supersede conflicting story-local recommendations. Recorded here with their consequence for
this baseline; where a ruling lands wholly inside a sibling's surface, the row says so.

| Ruling | Substance | Where this note carries it |
|---|---|---|
| **U1 — canonical service principal** | PMM-02 owns the opaque immutable `canonical_service_principal_id`, the alias registry, canonical resolution in authentication, and the manageable-service-principals endpoint. Aliases are tenant-scoped and source-qualified `(org_id, alias_source, alias_id)`, never globally keyed by name; one principal may hold several. Re-registration mints a new canonical ID by default; re-linking is an explicit audited operation. Cognito `cognito_client_ids` is an approved-client list, not an identity. Raw `service_accounts.id`, `agent_name`, `client_id`, ARN or caller-supplied text never owns a preference | §3.2, §1.9 owner table (principal kind/ID rows) |
| **U2 — compatibility ownership** | PMM-03 owns the persona→harness-compatibility-class registry and model compatibility/invocability evidence. Class IDs stable and unversioned (`claude-agent-sdk`, `codex-sdk`); harness/contract revision a separate versioned field, part of evidence and snapshot keys. PMM-02 owns the versioned Postgres default/posture records keyed by class. Sonnet 4.6 is a Claude-class **candidate**, not an active proven default, until PMM-09 records a bounded invocation using the actual Claude harness request shape. #5433 registers `gpt-*` personas and a separately proven Codex/GPT default; no cross-class fallback | §1.7, §1.7a, §1.7b, §1.9, §4.1a, §6.4, §6.7, §6.8 |
| **U3 — probe safety** | PMM-03 ships probing disabled with a zero spend budget. No page load invokes a model. Nightly and catalogue/destination-change probes may be enabled only in PMM-09 after the target account and a spend ceiling are approved. Pricing, listing or agreement status never counts as invocability proof | §2.3 (gate 5), §1.7b, §6.7, §6.8 |
| **U4 — snapshot authority** | PMM-06 C2 approved: gateway work-admission resolves Postgres authority and persists snapshot/digest in worker-unwritable storage; mandatory gateway bootstrap verifies workload/run/root and returns a fresh audience- and chain-bound `adpe1` assertion; 30-second maximum TTL retained and reissued per hop; workers receive no signer secret and no long-lived offline-verifiable policy token. `service_policy` is owned by the canonical service principal; the approving human is audit attribution only | §4.2, §4.5, §4.1a, §6.5 |
| **U5 — ARC/GitHub Actions root identity** | Root ownership follows the authenticated initiator, not the bot credential executing the job. A human `issues:labeled`, issue-comment or `workflow_dispatch` event preserves the resolved canonical human root. A scheduled, service-to-service or workflow-triggered run with no authenticated human initiator resolves a tenant-bound registered canonical service principal and fails closed if unregistered. The workflow/App execution identity is audit attribution only and never silently replaces the root preference owner | §3.2 |
| **U6 — one API contract** | The FastAPI self routes exist once at `/me/persona-models`; human JWT calls use that path while service SigV4 calls use external `/agent/me/persona-models`, whose existing API Gateway proxy strips `/agent` and reaches the same handler — the backend router is not duplicated. Canonical surfaces are `GET /me/persona-models`, `GET …/catalog?persona_key=…`, `GET …/explain/{persona_key}`, `PUT\|DELETE …/{persona_key}`, `GET …/manageable-service-principals`, and `GET\|PUT\|DELETE /service-principals/{canonical_id}/persona-models[/{persona_key}]` (human org-admin only, tenant-checked). All callers share one request/response schema and refusal vocabulary; self handlers derive canonical identity, administered handlers accept only canonical IDs from the server discovery surface | **PMM-02's surface, not this note's.** Recorded for completeness; PMM-01 defines no API. Two properties bind this baseline indirectly: one shared **refusal vocabulary** is what makes D2's actionable failures consistent across UI, CLI and service callers, and `explain` is the diagnostic surface through which a ladder outcome (§2.4) becomes visible to the invoker |

U6 is the one ruling with no structural change in this note. The rest are carried in the sections
named above, and §8.7 records what each changed.
