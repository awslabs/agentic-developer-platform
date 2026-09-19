# PMM-03 — Authoritative persona catalogue and invocable-model catalogue

**Story:** [#5420](https://github.com/aws-e/adp/issues/5420) (PMM-03) · **Parent epic:** [#5417](https://github.com/aws-e/adp/issues/5417) · **Depends on:** [#5418](https://github.com/aws-e/adp/issues/5418) (PMM-01, decisions D1–D6 locked)
**Status:** **proposed** story design — not binding. Eight architect proposals are being reconciled into one canonical #5417 design; that synthesis governs, and where this note differs from it the synthesis wins. Design only: no runtime code in this PR, and merging it does not close #5420, whose completion boundary is merged runtime code with tests and recorded bounded live probe evidence (a separate Developer story).
**Binding inputs consumed:** the six operator decisions on #5418 (D1–D6) **and** the unified architecture rulings posted on #5417 on 2026-09-18 at 16:42 UTC (referred to below as **R1**–**R6**: R1 canonical service principal, R2 compatibility ownership, R3 probe safety, R4 snapshot authority, R5 ARC root identity, R6 one API contract). §12 records what those rulings changed in this note and reconciles it against every sibling design PR at its current head.
**Revision:** rev-4. Rev-2 marked the note proposed rather than approved. Rev-3 applied R1–R6 and made the catalogue class-keyed. **Rev-4 answers the CHANGES_REQUESTED review on `25ece717`:** it makes this note the settled owner of persona→class metadata and deletes the stale open-ownership text (§2.4, §8); names **one durable authoritative invocability-evidence store** (§4.1a); keys evidence by **destination, canonical model, compatibility class, harness/contract revision and actual request-shape revision** (§4.1b); states plainly that **a minimal direct invoke is not proof the real SDK harness works**, with in-repo proof (§4.2a); and corrects three citation defects rev-3 carried (§9a).
**Checked against:** `c4809bb1` (default branch) for rev-4's claims; earlier revisions were checked at `d997a932` / `ae598410`. §9 lists the places this note corrects the issue text; §9a lists the places rev-4 corrects *this note's own* earlier revisions.

---

## 0. Executive summary

This story builds the two lists the Agent Models screen and the CLI are permitted to show, plus the registry that says which execution harness each persona runs under, and nothing else. No preference table, no screen, no command, no dispatch-time resolution.

**List one — personas.** Read live from `modules/agent-factory/webhook-ingress/lambda/common/personas.py`. Twelve keys, verified. Each persona row now also carries its **compatibility class** — the identifier of the harness that executes it — because R2 assigns this story the persona→class binding and nothing in the platform answers it today (§2.4). `pt-superpower`, which dispatches with no persona identity today (#4037), is listed but **not configurable** until that is fixed (§2.3).

**List two — selectable models.** The intersection D3 locked, evaluated for *the caller's own effective AWS destination* and *for the target persona's compatibility class*. A model is selectable only if the platform catalogue knows it, the tenant allowlist permits it, the class has a registered and validated compatibility contract for it (D6/R2), and **a real bounded invocation against that destination, using that class's actual harness request shape, returned a real model response** — recorded in one durable authoritative evidence store (§4.1a), keyed by destination, canonical model, compatibility class, harness/contract revision and the actual request shape (§4.1b), and not yet expired. **A minimal direct `invoke-model` body is not proof that the real SDK harness works** (§4.2a) — the platform has already been burned by that gap twice.

**Consequence of R3 that this story must state plainly.** R3 ships probing **disabled with a zero spend budget**; only PMM-09 may enable it, after the target account and a spend ceiling are approved. Since a real bounded invocation is the *only* admissible proof of invocability (D3/§4.3), a correct PMM-03 implementation ships a catalogue in which **no model is yet certified invocable**, including the D4 Claude-class default. That is the intended fail-closed state, not a defect — but it means PMM-03 delivers the *mechanism and the evidence schema*, and PMM-09 delivers the *evidence*. §4.7 states what the surface returns in that state so PMM-04 and PMM-05 do not build against a shape that only exists after PMM-09.

**The load-bearing rule.** Listing is not evidence. Neither is a price row. #2300 established "validation must invoke, not just resolve" after two agents came online, could not invoke their model, and reported success (#2300, #2301). This design carries that forward as the only admissible proof.

**Three corrections to the story's own premises**, each verified and each changing the design:

| # | The story assumes | Verified state at `c4809bb1` | Design consequence |
|---|---|---|---|
| C1 | Reuse `routing_probe.py` as the invocability probe | That probe **deliberately never succeeds** — `_PROBE_BODY` is malformed on purpose and a *pass* is `ValidationException` (`src/shared/services/routing_probe.py:107-111`, `:188`) | Reuse the *shape*, not the mechanism. A new probe that issues a real call **in the class's actual harness request shape** is required. Unlike the existing one it spends tokens — which is why R3 ships it disabled at a zero budget and gives enabling to #5427 (§4.2, §4.2a, §4.6) |
| C2 | D3's "migration from the currently inert fields" is a wiring change | The seeded values are friendly family names (`claude-sonnet`, `claude-haiku` — `infra/modules/lambda-authorizer/main.tf:133`, `:194`) which fnmatch **zero** canonical IDs the platform invokes | Enabling the gate on current data is an **outage**, not a tightening. Report-only first stage is mandatory and is this story's deliverable (§5) |
| C3 | Extend / reuse the general model-access list | `DEFAULT_ALLOWED_PATTERNS` in `model_resolver.py:85-100` deliberately includes `openai.*` to keep the Codex bridge working (#2709/#2713) | D6 permits Anthropic Claude only for persona execution. A separate, narrower **persona-selection baseline** is required. It is not a duplicate; §3.4 states why |

**Standing of this proposal:** internally consistent and complete as a *proposal*, not an approved contract. It becomes binding only when the #5417 synthesis adopts it.

**No operator decision now blocks implementation of this story, and no ownership question remains open here.** Rev-1 and rev-2 carried one blocking gate — the probe cost/cadence budget — and **R3 ruled it**: probing ships disabled at a zero budget, with cadence and spend as required per-environment configuration, and enabling is PMM-09's gated action. §8 records that gate as settled. **Rev-4 also closes the last piece of open text this note carried:** rev-3's §2.4b asked the operator whether this story's class registry projects #5433's or is its input. That framing is retired. R2 and PMM-01's current head both make **this story the authoritative owner** of the persona→class binding, with #5433 registering `gpt-*` personas *into* this registry rather than maintaining a second one (§2.4). This note no longer records a competing alternative to that ruling.

**What does block this story is an upstream engineering dependency, not a decision.** R1 makes the canonical service-principal ID the only thing that may own a preference, and R2's class-keyed defaults live in PMM-02's versioned Postgres record. Both are PMM-02's to build (#5419), and neither exists today — verified: no `canonical_principal_id`, `canonical_service_principal_id` or `manageable-service-principals` symbol anywhere under `modules/`. §6.3 therefore states what this story's validator *receives* rather than resolving identity itself, and §8 lists the dependency with what can proceed in the meantime.

---

## 1. Scope boundary

**In:** persona catalogue read; **the persona→harness-compatibility-class registry and the class-ID vocabulary (R2, §2.4)**; selectable-model catalogue read; alias→canonical resolution surfaced before save; **model compatibility evidence per class** and invocability evidence with timestamp and expiry; retirement handling; the bounded probe mechanism **shipped disabled at a zero budget (R3)** and its evidence record; the single shared validation function PMM-02 and PMM-06 both call.

**Out:** preference storage and writes (PMM-02); **the versioned Postgres class-keyed default/posture record (PMM-02, R2) — this story supplies the class keys it is keyed by, and the candidate identifier, but does not own the record**; **the canonical service-principal ID and its alias registry (PMM-02, R1)**; UI (PMM-04, #5422); CLI (PMM-05, #5423); snapshot creation and signing (PMM-06, #5424); dispatch-time recheck and enforcement (PMM-07, #5425); retirement *alerting* (PMM-08, #5426); **enabling the probe, approving its account and spend ceiling, and recording the invocations that actually certify any model — including the D4 class default (PMM-09, #5427, per R3)**.

Sibling stories are named by issue number throughout, because the "PMM-0n" labels and the issue numbers have been mismatched across notes (§12 item 6).

**Deliberate non-goal:** this story does not make the allowlist gate *enforcing*. D3 assigns PMM-03 the canonical catalogue, the intersection, provenance/freshness and the migration; PMM-07 owns consistent enforcement and PMM-09 owns the flip. §5 delivers the report-only stage and the data correction that makes the flip survivable.

---

## 2. The persona catalogue

### 2.1 Source of truth — read, never copy

`VALID_PERSONAS` is the union of two asymmetric dicts (`personas.py:62-64`). Verified at `c4809bb1`, exactly 12 keys:

```
aidlc, architect, codex, developer, malware-analysis-agent, operations,
pm, product, pt-superpower, reviewer, superplane-operator, superplane-researcher
```

Five are mention-only (no label trigger): `aidlc`, `codex`, `product`, `superplane-operator`, `superplane-researcher`. The asymmetry is deliberate and documented in-file (`personas.py:30-57`): a stale label on a reopened issue re-dispatches, so personas whose actions are costly are restricted to mentions.

There is **no `testing` persona**. The epic's example row "Reviewer/testing persona" maps to `reviewer`. PMM-01 already ruled that none may be invented; this note does not add one.

**AC-01 is a structural property, not a test to write.** The catalogue endpoint must derive its rows from `VALID_PERSONAS` at request time. Any second list — a frontend enum, a database seed, a hard-coded tuple in the catalogue module — fails AC-01 by construction, because a persona added to `personas.py` would then need two edits.

**Cross-runtime note.** `personas.py` lives in the webhook-ingress Lambda package; the catalogue endpoint lives in the gateway pod. These are separate runtimes and the gateway cannot import from the Lambda (`model_validate.py:5-9` records exactly this constraint, and #2279 ruling 4 forbids the reverse HTTP call for the Lambda's 10-second budget). **This story must not create a third copy.** Two admissible options, for the implementer to choose and justify in the PR:

- **(a) Shared read at build/deploy time** — stage `personas.py` into the gateway image the way `stage-personas.sh` already stages persona prompt files, with a parity test asserting the staged copy equals the source. Preferred: no runtime coupling, and drift is a red test rather than a silent divergence.
- **(b) Promote `personas.py` to a shared location** both runtimes import. Cleaner long-term, wider blast radius; needs an `impact` check on the Lambda package before proposing it.

Whichever is chosen, the parity test is the AC-01 evidence. A copy without a parity test is the #4021 drift defect that `test_persona_catalogue_parity.py` exists to prevent.

### 2.2 Preserve both existing drift guards

- `test_persona_catalogue_parity.py` pins `docs/agent-catalogue.md` ↔ `MENTION_TO_PERSONA` in both directions **and asserts a plausible row count**, because a two-way set comparison passes vacuously on an empty parse (see its docstring, lines 11-18). Any new catalogue test must copy that anti-vacuity discipline.
- `test_persona_prompt_files.py:49-53` asserts `_KNOWN_MISSING_PROMPT_FILES == {"pt-superpower"}` **exactly**, not as a soft skip, so adding a prompt-less persona is a visible diff. The set "must only ever shrink".

Neither test may be weakened. AC-02 is satisfied by these continuing to pass plus a new catalogue-shape test.

### 2.3 `pt-superpower` is listed but not configurable until #4037 is fixed

`pt-superpower` is registered, has no prompt file, and per #4037 "dispatches a pod with no persona identity"; `docs/agent-catalogue.md:36` marks it known-broken.

The catalogue row carries a `configurable` flag. The question is its value here. Offering it lets someone carefully set a model for an agent that will not use a persona identity — configuration that reads as working and changes nothing, the #4511 inert-config class this epic exists to eliminate. Hiding it makes the catalogue disagree with `VALID_PERSONAS`, which is the AC-01 property.

**Resolved on review of PR #5434:** list it, with `configurable: false` and a machine-readable `not_configurable_reason` naming #4037. `pt-superpower` stays non-configurable until #4037 is fixed. That keeps the catalogue honest about what exists (AC-01/AC-02 hold — the key appears), keeps the screen from accepting a choice that cannot take effect, and gives PMM-04 something to render as a disabled row with an explanation rather than an unexplained absence. The flag clears itself when #4037 closes.

This does not block the rest of the catalogue contract. Any later move to make it configurable is a product decision for the epic operator and requires #4037 to be closed first.

### 2.4 The persona→compatibility-class registry — **this story is its settled owner**

R2: "PMM-03 (#5420) owns the persona→harness-compatibility-class registry and model compatibility/invocability evidence. Class IDs are stable and unversioned (`claude-agent-sdk`, `codex-sdk`); harness/contract revision is a separate versioned field and part of evidence/snapshot keys."

**Ownership is settled here, and rev-4 records it as closed rather than pending.** PMM-01 at its current head (#5436, `b8045dbf`) §1.7a is titled "Who owns the persona-to-class binding — **settled**" and states: "U2 settles the owner: PMM-03 (#5420) owns the persona→harness-compatibility-class registry and the model compatibility/invocability evidence… Rev-3 of this note recorded the owner as unresolved and routed a conflict with #5433 to the operator; **that decision has been made and this section records it as closed, not pending**." #5433 is assigned the narrower job of registering `gpt-*` personas **into** this registry rather than maintaining a second one. **This note therefore holds persona→class metadata as the single authority**, and rev-3's competing framing is deleted rather than retained alongside the ruling (§2.4b is gone; see §9a).

**What that ownership obliges this story to publish**, because the siblings consume it and none of them defines it:

- **The class-ID vocabulary** — `claude-agent-sdk`, `codex-sdk`, stable and unversioned.
- **The persona-row class attribute** — the binding itself, absent from the tree today.
- **The field names**, because the siblings have already diverged on them. PMM-02 (#5437, `e2c7d099`) stores `harness_compatibility_class` as the key (UNIQUE, CHECK-constrained) with `harness_contract_revision` as the separate versioned field; PMM-06 (#5442, `f9f0ec68`) spells the second one `harness_compatibility_revision`; PMM-07 (#5438, `82bc735f`) carries a bare `compatibility_class`. **This story fixes the spelling as `compatibility_class` + `harness_contract_revision`** — the class name matching the domain term used epic-wide and the revision name carrying the most in-tree weight (it is CHECK-constrained in PMM-02's table). §12 records the divergence for the synthesis; as owner, this note does not leave it unresolved.

**Why this is a real gap and not a relabelling.** A *compatibility class* is the set of models one execution harness has a registered, validated contract for. Rev-1 and rev-2 of this note put a harness only on **model** rows (`harness{id, contract_revision}`, §6.2) — which answers "is this model valid for that harness", the inverse of what the epic needs. The question the resolver actually asks is "which class is *this persona* in", so it can look up that class's default. Verified absent at `c4809bb1`: no `compatibility_class`, no `harness_id`, no `PERSONA_TO_RUNTIME` and no class-ID string anywhere under `modules/` (the only `claude-agent-sdk` hits are the npm package name and SDK type imports). **The gap is a code gap, not an ownership gap** — a distinction PMM-01's head is careful about: its §1.7a records that this note's rev-3 "adds a §2.4 persona→class registry with stable unversioned class IDs and a separate `harness_contract_revision`", so "the *design* obligation U2 created is met; the *code* gap above is unchanged — nothing merged binds a persona to a harness, which is why this remains an ordering dependency rather than an existing capability" (#5436 `b8045dbf`, `docs/design-notes/5417-per-invoker-persona-model-mapping.md` §1.7a). Rev-4 keeps that framing: this note owns and specifies the binding; the runtime code that emits it is the implementation story's.

**Contract.**

- **Class IDs are stable and unversioned**, per R2: `claude-agent-sdk` for every persona executing directly today, `codex-sdk` reserved for the native GPT personas #5433 introduces. The ID never carries a version — versioning lives in the separate `harness_contract_revision` field, so a harness upgrade revises evidence without renaming a class and invalidating stored keys.
- **The class is derived from the persona key, not stored on a preference row.** PMM-02 requires this (its preference table has no harness column by design), and it is what keeps a harness change from requiring a data migration of every principal's saved rules.
- **Every persona row carries `compatibility_class`.** This is the addition to §6.1. Today all twelve resolve to `claude-agent-sdk`: all direct persona execution runs the pinned Claude Agent SDK (`modules/agent-factory/agent/package.json:14`), and `codex` is not an exception — its outer agent is the Claude SDK worker and Codex is a bounded delegated tool.
- **`codex-sdk` is registered as a class ID with no personas mapped to it.** Reserving the identifier now costs nothing and prevents #5433 minting a second spelling; mapping a persona to it is #5433's act, not this story's.
- **No cross-class fallback, ever.** A model proven invocable for `claude-agent-sdk` is not admissible for `codex-sdk`, because the proof used the Claude harness request shape. A lookup miss is a refusal naming the class, never a substitution — the single failure mode that would hand a persona a model its harness cannot run.

**Consequence for the class default.** R2 states `us.anthropic.claude-sonnet-4-6` is a Claude-class **candidate**, not an active proven default, until PMM-09 records a bounded invocation using the actual Claude harness request shape. §3.3 is corrected accordingly: this story seeds the catalogue *structure* and records the identifier as a candidate with no invocability evidence. It does not seed a proven default, and it does not seed one global default for all classes.

**Two further facts that make the class-keyed shape load-bearing rather than theoretical.** The GPT class is not hypothetical: `modules/agent-factory/agent-worker-image/codex-config.toml:25` already pins `model = "openai.gpt-5.6-sol"` for the delegated Codex tool, and `platform/scripts/enable-bedrock-models.sh` has no `openai.*` entry, so that family has no deploy-time availability gate at all. And no alias resolves to the `us.` form of Sonnet 4.6 today — both maps pin `"sonnet46": "global.anthropic.claude-sonnet-4-6"` (`model_resolver.py:28`, `model_validate.py:31`), so D4's identifier is reachable by canonical ID only. Closing that alias gap is #5427's (PMM-09) per its own note; this story must not silently add the alias, because adding it would make an unproven identifier look routine.

### 2.4b How #5433 composes with this registry — not an open ownership question

Rev-3 carried this section as "Unresolved cross-epic ownership — operator decision", asking whether this story's registry projects #5433's or is its input. **That question is withdrawn as superseded** (§9a): R2 and PMM-01 §1.7a both make this registry authoritative and #5433 a *registrant* in it.

What remains is a narrow sequencing obligation, not a choice between two designs. #5433 (native GPT personas, OPEN) must register its `gpt-*` personas into this registry using the class IDs defined above, and must not mint a second spelling of `codex-sdk` — which is why this story reserves that identifier now with no persona mapped to it (§2.4). PMM-01 §6.7a frames the residual as a file-location detail that "decides where the registry file lives, not whether the binding can be built or what it must contain", needed "before #5433 registers its first `gpt-*` persona, not before PMM-02 or PMM-03 begin". This note does not treat that as a gate and does not retain an alternative recommendation against it.

---

## 3. The selectable-model catalogue

### 3.1 The intersection, per D3

D3's admission gates. PMM-01 at its current head enumerates **eight**, not the five rev-1 of this note listed: the first five below plus compliance policy, budget limits and rate limits. This story owns gates 1–5 and owns none of the last three — compliance, budget and rate limits are evaluated at invocation, not at catalogue-read time, and a catalogue that pretended to answer them would be claiming a spend decision it cannot see. Stated in cheapest-first order so an expensive step never runs for a model already excluded:

1. **Harness compatibility for the target persona's class** (D6/R2) — evaluated first because it is a pure lookup and excludes whole families at once (§2.4, §3.4).
2. **Platform-supported catalogue** — the versioned model set (§3.3).
3. **Tenant/organization allowlist**, inheriting a versioned platform baseline when the org has none — D3 is explicit that "no explicit allowlist inherits a versioned platform baseline allowlist rather than unrestricted access" (§3.5).
4. **Registered service-principal restrictions**, where the caller is a service principal, resolved by its canonical ID (R1, §6.3).
5. **Invocability, freshly proven, through the resolved destination, using the class's harness request shape** (§4). Under R3 this gate is unsatisfied for every model until PMM-09 runs the probes (§4.7).

Ordering is an efficiency property only. Per PMM-01, "admission gates are not precedence levels" — none of them selects a model, and failing any one is a refusal rather than a fallback.

A preference selects only within this set and can never widen access (D3). Per D1, an organization rule may **block** a selection with an actionable explanation but must **never** substitute another model — so the catalogue's job is to exclude, never to swap.

Per D3, "empty, stale or contradictory effective policy fails actionably". An empty intersection is a refusal naming which input emptied it — not an empty list rendered as "no models available", which reads as a UI bug and sends the reader to the wrong place.

### 3.2 Selection vs. authorization, kept visibly separate

Per D1 these are separate policy axes. This story owns only the **authorization-side answer to "may this be offered"**. It does not select, does not resolve at dispatch, and grants nothing: the catalogue is read-only and confers no access to a model, an AWS account, or additional budget. That mirrors the epic's own Authorization clause and #4692's discipline that a mapping authority is not an access grant.

### 3.3 Where the "platform-supported catalogue" comes from

The candidate sources, verified:

| Source | Path | Fit |
|---|---|---|
| Gateway alias map | `model_resolver.py:17-81` | Alias resolution: **yes** (§3.6). As a catalogue: **no** — it contains Claude 3/3.5 and non-Anthropic families, and still maps `claude-sonnet-4` → `anthropic.claude-sonnet-4-20250514-v1:0`, the Legacy ID the Lambda's own comment records as non-invocable (`model_validate.py:34-38`) |
| Lambda alias map | `model_validate.py:19-38` | The invocability-curated list — 8 pinned entries, bare aliases deliberately removed so a choice cannot drift. Its lines 34-38 are the authoritative record of what does *not* invoke |
| Pricing snapshot | `pricing_policy/snapshots/2026-09-12.2.json` | Price context: **yes** (§3.7). As an entitlement source: **no** (§7) |
| `GET /v1/models` | `routes.py:444-459` → `model_resolver.get_available_models` (`model_resolver.py:211-241`) | Returns `{id: alias, object, created, owned_by}` only. No pricing, no capability, no availability. Zero frontend consumers (only `frontend/src/mocks/data/logs.ts:7`, a mock log line) |

**Ruling:** the platform-supported catalogue is a **new versioned artifact**, seeded from the Lambda map's curated 8 entries plus the identifier D4 names for the Claude class, `us.anthropic.claude-sonnet-4-6`. It is not either alias map.

**Corrected in rev-3 — what "seeded" may and may not mean.** Rev-1 called that identifier "the D4 canonical default". Per R2 it is the **Claude-class candidate**, not a proven default, and per R3 nothing can prove it here. So:

- Rows are seeded with **structure and identifier only**. Each carries its compatibility class and an invocability-evidence slot that is **empty**, not assumed.
- The catalogue records **no default at all**. The class-keyed default/posture record is PMM-02's versioned Postgres row (R2); this story supplies its key vocabulary (§2.4) and the candidate identifier, and PMM-09 supplies the proof that promotes a candidate to active.
- Seeding a value as proven would be the inert-config class at platform scale — a configuration that reads as verified and was never invoked, which is #2300 with a wider blast radius.

This correction is why PMM-01 flagged this PR pre-merge rather than as a later amendment: keying the catalogue by class now costs a documentation edit, while keying it after implementation reopens a shipped schema and its seed data.

Rationale: D3 requires a *versioned* catalogue with provenance and freshness. Neither alias map has a version, and the gateway map is demonstrably wrong about invocability. A third structure is justified here precisely because the two existing ones cannot answer the question being asked — per the repo convention of finding ≥2 existing examples first and justifying a new pattern only when neither fits.

**`GET /v1/models` keeps its current shape.** It is OpenAI-compatible and has external API consumers. The catalogue is a **new endpoint**, not an extension of it — additive, per the story's compatibility clause. Do not add availability fields to `/v1/models`; an OpenAI-compatible list with ADP-specific fields is a compatibility hazard for no gain, since it has no frontend consumer to benefit.

### 3.4 Harness compatibility (D6) — and why a separate baseline is required

D6: a model is selectable for a persona only when that persona's harness has an explicitly registered and validated compatibility contract for it. All current direct persona execution uses the pinned Claude Agent SDK (`modules/agent-factory/agent/package.json:14` → `@anthropic-ai/claude-agent-sdk` `0.3.220`). So v1 permits Anthropic Claude only for persona execution, and `@agent-codex` is not an exception: its outer agent is the Claude SDK worker and Codex is a bounded delegated tool.

**This is C3, and it is why the catalogue cannot inherit the general list.** `model_resolver.py:94-99` deliberately includes `openai.*` — without it "check_model_access would 403 every Codex run (#2713 C1)". That pattern is correct for the metered gateway proxy path and wrong for persona selection. Inheriting it would offer `openai.gpt-5.5` as a selectable model for the `codex` persona, which D6 forbids and which would fail at run time.

The catalogue therefore carries a **persona-selection baseline** distinct from `DEFAULT_ALLOWED_PATTERNS`. The note records this as a deliberate divergence with the reason, so a future reader does not "fix" the inconsistency by merging them.

**Both directions are required, and rev-1 had only one.** A model row carries the class and the `harness_contract_revision` it was validated against — "is this model valid for that harness". A persona row carries its `compatibility_class` (§2.4) — "which class is this persona in". The resolver needs the second to find the first; a note carrying only model-side harness metadata cannot answer a class-keyed default lookup. PMM-06 binds the class and revision into the signed snapshot (D6, R4); this story emits them.

**Class ID and contract revision are separate fields, per R2.** The class ID (`claude-agent-sdk`) is stable and unversioned so stored evidence and snapshot keys survive a harness upgrade. The revision is versioned and is part of the evidence key, so upgrading the SDK correctly invalidates evidence gathered under the old request shape rather than silently inheriting it — which matters because R2 and #5427 both require the proof to use the harness's *actual* request shape, not a bare `invoke-model` smoke test.

**Also flagged, not in the epic's inventory:** `modules/agent-factory/agent/src/complex-task-chat/complex-task-chat-agent.ts:443` already reads `persona.modelOverride ?? process.env.ANTHROPIC_MODEL`, with the field declared at `persona-loader.ts:45`. A per-persona model override therefore already exists on the chat path. It is out of scope here, but PMM-07's "single resolver" inventory must include it or it becomes a surviving divergent path.

### 3.5 Tenant allowlist — and the C2 landmine

Verified inert at `c4809bb1`, four independent breaks — PMM-01 fact 3 re-confirmed:

| Break | Evidence |
|---|---|
| Registry value never reaches the authorizing object | `agent_entry_to_token_context` (`src/auth/agent_registry.py:244-277`) copies 10 fields; `allowed_models` (read at `:119`) is not among them, and `TokenContext` (`src/shared/schemas/auth.py:33`) has no such field |
| Resolver config has no production writer | `set_allowed_models` (`model_resolver.py:316-323`) — only callers are `tests/proxy/test_model_resolver.py:141,155,161`. Both construction sites (`routes.py:124`, `service.py:178`) pass no config, so `_allowed_models_config` is always `{}` and `_get_allowed_patterns` (`:242-265`) always returns `DEFAULT_ALLOWED_PATTERNS` |
| Header has no consumer | `X-Agent-AllowedModels` is emitted by the authorizer (`lambda/api-authorizer/handler.py:517`, `:453`) and read nowhere in `src/` |
| Lambda parameter always `None` | `resolve_and_validate(alias, ...)` is called with one positional argument, so `persona_allowed_models` (`model_validate.py:51`) is never supplied |

**C2 — the part the story and D3 both miss.** The gate is not merely unread; the *data* is unusable. Seeded values are friendly family names:

```
infra/modules/lambda-authorizer/main.tf:133   allowed_models = { SS = ["claude-sonnet", "claude-haiku"] }
infra/modules/lambda-authorizer/main.tf:194   allowed_models = { SS = ["claude-sonnet"] }
infra/modules/lambda-authorizer/main.tf:264   allowed_models = { SS = ["*"] }
```

`resolve_and_validate` fnmatches the canonical ID against these patterns. `claude-sonnet` matches **no** canonical ID the platform invokes — not `global.anthropic.claude-sonnet-4-6`, not `us.anthropic.claude-sonnet-4-6`, not `global.anthropic.claude-opus-4-6-v1`. Verified by direct fnmatch evaluation. Only the two `["*"]` rows (`agent-registry-seed.tf:44`, `agent-authority-boundary.tf:216`) would pass anything.

So flipping the gate on against current data denies every model for the two narrowly-seeded agents. That is an outage, not a tightening, and it would look exactly like the #2300 symptom. §5 is the consequence.

### 3.6 Alias→canonical resolution (AC-03)

Reuse `model_resolver.py`'s alias mechanism, restricted to the §3.3 catalogue. Behaviour the contract fixes:

- A friendly family name submitted for validation returns **the canonical versioned identifier it resolves to**, so the caller confirms it before saving. This is the AC-03 requirement and the epic's anti-drift rule: "the saved effective value must not drift silently when a provider changes a 'latest' alias".
- A bare or "latest"-style alias is **refused**, not stored. Both maps already removed bare `opus`/`sonnet`/`haiku` for this reason (`model_validate.py:22-25`, `model_resolver.py:20-22`). `claude-3-5-sonnet-latest` (`model_resolver.py:36`) resolves to a pinned ID, but it must not be *offered*, because the name invites the drift the pin prevents.
- `claude-sonnet-4` → `anthropic.claude-sonnet-4-20250514-v1:0` must not appear as selectable: the Lambda records it as Legacy/access-denied and non-invocable (`model_validate.py:34-38`). Under §4 it is excluded automatically by failing its probe — which is the design working as intended rather than a special case.
- Refusals use the platform's established `422 {reason, message}` vocabulary (`self_routes.py:108-115`), so PMM-04 and PMM-05 branch on one stable reason set.

### 3.7 Price context

Reuse the pricing v2 path: snapshot `pricing_policy/snapshots/2026-09-12.2.json`, migrations `alembic/versions/044_model_pricing_v2.py` and `047_claude_pricing_v2.py`, reader `src/budget/pricing_v2_reader.py` (`get_rate_state`, with the `cached_rate_state`/`refresh_due`/`cache_failure_age_seconds` staleness signals this design mirrors — see §4.4 for where their state machine actually lives).

`POST /budget/calculate-cost` (`src/budget/routes.py:126`) answers one model at a time, so the catalogue reads rates directly rather than fanning out N calls per page load.

**Price context is presentational only.** A missing price row must not remove a model from the list, and a present price row must never make a model selectable (§7). Where a rate is absent, the row says so rather than showing zero — unknown cost is not zero cost.

---

## 4. Invocability evidence — the core of this story

### 4.1 Destination-aware by construction

Invocability is a property of the **destination account and region**, not of the platform. Resolve via `BedrockRoutingResolver.resolve` (`src/proxy/bedrock_routing.py:166`) → `BedrockTarget{account_id, rung, destination_id, region}` (`:45-64`). Evidence from one destination is never admissible for another; that is AC-05's real content. The full key is §4.1b.

### 4.1a One durable authoritative evidence store — new in rev-4

Rev-3 said "evidence row" and "evidence store" without naming one, which left the most load-bearing artifact in this story undefined. **No sibling defines it either** — PMM-01, PMM-02, PMM-04, PMM-05, PMM-06, PMM-07 and PMM-09 were each read at their current heads and all seven defer the store to this story while consuming its outputs. As the settled owner, this note names it.

**The store is a new Postgres table in the gateway schema, `model_invocability_evidence`, and it is the single authority.** Everything else that reports invocability is a reader of it.

**Why Postgres, and why not the alternatives** — each candidate was checked in-tree:

| Candidate | Why not |
|---|---|
| Reuse `bedrock_destination_registry.routing_capable` / `verified_at` (`alembic/versions/037_bedrock_account_routing.py:170`, `:173`) | Those are per-**destination** facts — "can this role assume and reach Bedrock at all" — with no model, class or harness dimension. There is also no unique key to extend: the only index is on `account_id` **alone and non-unique** (`:186`), so per-model rows cannot be attached to it |
| `model_aliases` (`alembic/versions/001_initial_schema.py:74-81`) | Holds `alias_name → bedrock_model_id` only — no version, no evidence, no freshness. PMM-07's head reaches the same conclusion independently and routes the catalogue here |
| A DynamoDB pk/sk+TTL table | No gateway-owned generic one exists to reuse, and PMM-06's head explicitly refuses a DynamoDB projection of relational policy. PMM-02's `validate_selection` call is synchronous and must join evidence in the same transaction as the preference read, which a second datastore turns into a cross-store consistency problem |
| TTL-only expiry (let rows vanish) | AC-08 requires expiry to flip a row to **stale**, and §4.7 requires "unproven" to stay distinguishable from "refused". A row that disappears is indistinguishable from one that never existed |

**Durability requirements, and the in-repo pattern they come from.** `alembic/versions/044_model_pricing_v2.py` is the precedent this table follows, because it already solved the same problem for pricing provenance:

- **A composite primary key** over the full evidence key (§4.1b), as `model_pricing_rates_v2` does (`:172-180`) — so one destination/model/class/revision combination has exactly one authoritative row and a second writer updates rather than appends an ambiguous duplicate.
- **`verified_at NOT NULL`** (`044:164`) — an evidence row with no timestamp is not evidence. This is the schema making the §4.4 freshness rule unrepresentable to violate.
- **A content digest with a format CHECK**, following `source_content_sha256 CHAR(64)` with `~ '^[0-9a-f]{64}$'` (`044:162`, `:232`) — here applied to the probe's actual request body (§4.1b).
- **A status/timestamp consistency CHECK**, following `ck_generations_validated_at_consistent` (`044:127-131`), whose comment names exactly the failure this store must also prevent: "a crashed publisher could leave a row that looks publishable". The analogue here: a row may not read as *proven* unless it carries a request ID and a `verified_at`. `is_usable_for_routing` (`src/shared/models/bedrock_routing.py:112-125`) is the same both-halves discipline at the model layer, and its docstring names the one-half case "the inert mapping of the #4511 class" — which is this epic's own failure mode.
- **Append-only history is not required**, and rev-4 states that deliberately: evidence is recomputable by re-probing, which is why §11's "revert the PR" rollback is valid here. What must never be lost is the *distinction* between unproven, proven and refused.

### 4.1b The evidence key — five parts, per the rev-4 review

Rev-3 keyed evidence by `(canonical_model_id, account_id, region)`. That is insufficient: it cannot tell a proof gathered through the real Claude harness from one gathered by a bare `invoke-model` one-liner, and it silently inherits evidence across a harness upgrade. **The key has five parts:**

| Key part | Field(s) | Why it is in the key |
|---|---|---|
| 1. Destination | `account_id`, `region` | Invocability is a destination property (§4.1). AC-05's whole content is that two principals with different destinations get different answers |
| 2. Canonical model | `canonical_model_id` | The versioned identifier, never an alias. Fable 5.1's three variants are three keys, not one (§7) |
| 3. Compatibility class | `compatibility_class` | A proof obtained under `claude-agent-sdk` says nothing about `codex-sdk`, because the request shapes differ structurally (§4.2a). This is what makes "no cross-class fallback" (§2.4) enforceable at the storage layer rather than by convention |
| 4. Harness/contract revision | `harness_contract_revision` | Per R2, versioned separately from the stable class ID. In the key so that an SDK upgrade **invalidates** evidence instead of inheriting it. The pinned revision is real and already asserted in-tree: `CLAUDE_SDK_VERSION = '0.3.220'` (`modules/agent-factory/agent/src/harnesses/claude-control.ts:81`), whose comment is precisely this rule — "A version bump is a prompt to re-run the contract suite, not a no-op" — and `claude-control.test.ts:183-198` fails the suite if the pin moves without re-proving |
| 5. **Actual request-shape revision** | `request_shape_sha256` (CHAR(64), CHECK `~ '^[0-9a-f]{64}$'`) | **New in rev-4 and the part that closes the review's central gap.** A SHA-256 over the canonicalised request body the probe actually sent. The class ID says *which* harness; the contract revision says *which version* of it; only this says *what was actually on the wire* |

**Why part 5 cannot be replaced by part 4.** A harness revision is a declared intent; the request-shape digest is an observation. They come apart in both directions. A config change can alter the wire shape without moving the SDK pin — `codex-config.toml:34-40` disables `web_search` with no version change. And the digest is what makes the evidence *self-invalidating*: it is computed from the request rather than asserted alongside it, so a probe that quietly stops sending tools produces a different digest and its evidence no longer matches the key any consumer looks up. That is the #4511 inert-config defence applied to evidence itself.

**The digest must be captured, not hand-authored.** Per §4.2a the faithful request shape is not something this design can write down as a literal — it is emitted by the pinned SDK. The probe therefore records the digest of what it sent; a hand-written body whose digest was asserted by a developer would re-introduce exactly the gap the review names.

**Note for PMM-06 and PMM-02.** `request_shape_sha256` is an **evidence-key** field. PMM-06 binds class and revision into the signed snapshot (§3.4) and does not need the digest in the snapshot body; it needs to know that the evidence it read was keyed by one.

### 4.2 C1 — the existing probe cannot be reused as the mechanism

`routing_probe.py` is the right *pattern* and the wrong *mechanism*:

```
src/shared/services/routing_probe.py:107-111
    _PROBE_BODY = json.dumps({"adp_routing_probe": True})
    "Deliberately invalid: no messages, no anthropic_version ...
     generates no tokens either way."
src/shared/services/routing_probe.py:188
    ValidationException → IAM allowed it  (a PASS)
```

It succeeds by *failing* in a specific way, and never obtains a model response. AC-04 requires the opposite: "only models that returned a real model response appear as invocable". A probe that cannot distinguish "IAM permits invocation" from "the model actually answers" would certify `claude-fable-5` — IAM permits it; the data-retention mode is what refuses it — and reproduce #2300 exactly.

**Reuse from it, verbatim in spirit:** one pinned target per probe, a tight timeout (`_PROBE_TIMEOUT_SECONDS = 10`, `:115`), `_PROBE_MAX_ATTEMPTS = 1` (`:116`), and the do-not-change comment discipline that explains *why* an id must not be edited (`:101-116`). That comment style is the reason a future reader will not break this, and the new probe needs its own.

**Differs, and must be stated loudly in the code:** the body is a real request in the class's actual harness shape (§4.2a), not a hand-written minimal one, and the probe therefore **spends tokens**. The existing probe's zero-cost property is the reason it needs no budget approval. This one does (§8).

### 4.2a A minimal direct invoke is not proof the real SDK harness works — new in rev-4

This is the review's central correction and rev-3 was too weak on it. Rev-3 described the probe body as "a real minimal request (valid `anthropic_version`, one trivial message, `max_tokens` at the floor)". **That body is exactly what must not count as proof.** It is very nearly the verification one-liner already in the deploy docs:

```
docs/adp-platform-deployment/deploy-quickstart.md:786
  aws bedrock-runtime invoke-model --model-id us.anthropic.claude-sonnet-4-6 \
    --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":8,
             "messages":[{"role":"user","content":"hi"}]}'
```

A pass there proves entitlement and reachability. It proves **nothing** about whether a persona can execute under its harness, because it exercises none of the request features the harness actually sends. **Three independent in-repo proofs that this gap is real, not theoretical:**

1. **Same family, same entitlement, incompatible thinking parameter.** The hash-verified AWS model cards in `modules/gateway/tests/pricing_policy/fixtures/aws/claude/` (provenance recorded in `card-source-manifest.json` with url/bytes/sha256 and `verified_at: 2026-09-12`) record: Opus 4.6 is `Reasoning: Supported`, unqualified (`model-card-anthropic-claude-opus-4-6.md:22`); Opus 4.7 is `Reasoning: Supported (thinking.type: "adaptive" only)` (`model-card-anthropic-claude-opus-4-7.md:22`), and `:27` states "Unlike Claude Opus 4.6, `thinking.type: "enabled"` with `budget_tokens` is **not supported and will return a 400 error**." A harness that sends `thinking.type: "enabled"` passes the one-liner above against both models and **fails at run time against one of them**. PMM-01's head records the platform already living this: #1128's Opus 4.8 proposal "is not the default because its ADP smoke failed on incompatible thinking parameters and was reverted".
2. **A tool the harness sends that the destination rejects.** `modules/agent-factory/agent-worker-image/codex-config.toml:34-40` disables `web_search` because "bedrock-mantle rejects it: HTTP 400 'The web_search tool is not supported' — every delegation failed (#3897…#3902)". Model access was fine; the *request shape* was not.
3. **The masking failure mode, already recorded.** `platform/scripts/enable-bedrock-models.sh:5-9`: "The Claude SDK inside agent-worker swallows that failure as a graceful 'no changes needed' (0 tokens, 1 turn), which masks the real error (see aws-innovate/adp#337 smoke-test incident, 2026-07-04)." A harness failure can therefore present as a *success* — which is why the proof must be the harness's own request, and why a completion report must not infer a pass.

**What the probe must therefore do.** Issue the request **in the compatibility class's actual harness shape** and record `request_shape_sha256` over what it sent (§4.1b). Two structural constraints the implementer must respect, both verified:

- **The shape cannot be hand-authored from this document.** The Claude Agent SDK's `Options` type exposes **no** `max_tokens`/`maxOutputTokens` lever at all — only `effort` — recorded in-tree at `modules/agent-factory/agent/src/complex-task-chat/run-query.ts:297-303` ("The Claude Agent SDK's Options type has no `maxTokens` / `maxOutputTokens` input field… The supported output-length lever is `effort`") and again at `channel-profiles.ts:106-112`. **Rev-3's "`max_tokens` at the floor" is therefore not implementable through the real harness and is withdrawn** (§9a). Bounding cost uses the levers that exist — `effort`, `maxTurns`, `maxBudgetUsd` — plus the §4.6 per-cycle ceiling.
- **The endpoint choice decides whether the shape survives.** The `/v1/messages` translation path builds its Bedrock body field-by-field and copies only eight: `anthropic_version` (hard-coded), `max_tokens`, `messages`, `system`, `temperature`, `top_p`, `top_k`, `stop_sequences` (`src/proxy/format_translator.py:241-263`). **`tools`, `tool_choice`, `thinking`, `cache_control` and `anthropic_beta` are silently dropped** — and note that `BedrockInvokeRequest` *declares* `tools`/`tool_choice` (`src/proxy/schemas.py:395-396`), so the omission is in the copying, which makes it easy to misread as supported. The `/model/{id}/invoke[-with-response-stream]` path instead passes the body through under `extra: "allow"` (`schemas.py:398`, `service.py:468`). **A probe routed through `/v1/messages` cannot exercise the harness shape** — it would strip the very fields whose compatibility is in question and then record a pass. This is also why `request_shape_sha256` must be computed from what actually went on the wire.

**Consequence for AC-04 and for PMM-09.** This tightens §10's AC-04a: the mechanism is only correct if it sends the harness shape and records its digest. And it aligns with PMM-09's head (#5439, `86c7959a`), which independently requires "a bounded real invocation… using the runtime request shape (the Claude-harness path, per D6 — **not a bare `invoke-model` smoke that skips the harness**), recording the request ID", and warns that the marketplace-agreement path cannot substitute because `normalize()` strips the prefix. Rev-4 and PMM-09 agree; this note supplies the key and the store that make such a proof durable and attributable.

### 4.3 Pass and fail

- **Pass:** a successful invocation **in the class's harness shape** (§4.2a) returning a well-formed model response. Record the full five-part key (§4.1b) plus AWS request ID and timestamp.
- **Fail:** any exception. Record the error code so the operator sees *why*. `AccessDeniedException`, `ValidationException` (the thinking-parameter class — Opus 4.7 returns a 400 for `thinking.type: "enabled"` with `budget_tokens`, `model-card-anthropic-claude-opus-4-7.md:27`), `ResourceNotFoundException` and a data-retention refusal are all non-invocable, and the distinction matters for the operator's next action.
- **Never inferred.** Presence in a listing, a price row, an alias-map entry, or another region's evidence are all inadmissible. This is the #2300 ruling restated as a code invariant.

### 4.4 Freshness, staleness and expiry (AC-06, AC-08)

Follow the `verified_at`-plus-capability precedent from account routing: `alembic/versions/037_bedrock_account_routing.py:173` adds `verified_at`, and `src/shared/models/bedrock_routing.py:125` gates capability on `routing_capable and verified_at is not None` — both halves required, failing for different reasons.

- Each evidence row carries `verified_at` and a configured expiry.
- On expiry the row flips to **stale** rather than remaining certified indefinitely (AC-08). Time must be injectable so a controlled-clock test can prove the flip.
- When the catalogue source or evidence store is unavailable, existing entries return **marked stale** and the API **must not certify a new selection as verified** (AC-06). It must never invent a list — an empty or fabricated catalogue is the "certified to choose a model nobody verified" failure.
- Per D3, stale effective policy fails actionably. A *read* may return stale-marked rows for display; a *validation call* on stale evidence refuses with a distinct reason code. PMM-02's save and PMM-06's snapshot both consume the validation call, so neither can accidentally accept stale evidence as proof.

The `cached_rate_state` / `refresh_due` / `cache_failure_age_seconds` triad is the in-repo shape to mirror for the stale-but-serving pattern. **Refined in rev-4:** those three functions are thin delegations in `src/budget/pricing_v2_reader.py:165-180`; the state machine they call — and the thing actually worth mirroring — is `V2RateCache` in `modules/gateway/pricing_policy/storage.py:223` (note the path: a top-level package, not under `src/`, so the Lambda can import it too). Mirror `V2RateCache`, not the delegating wrappers (§9a).

### 4.5 Retirement (AC-07)

A retired model is flagged `retired` and is **not selectable**. Existing mappings pointing at it stay **visible**, so their owners can be told — the alerting itself is PMM-08's. Retirement is a catalogue-level state, independent of probe outcome: a model can be invocable and retired at once, and must not be offered.

### 4.6 Probe safety — settled by R3, no longer an open decision

R3: "PMM-03 ships probing disabled with a zero spend budget. No page load invokes a model. Configured nightly and catalogue/destination-change probes may be enabled only in PMM-09 after the target account and a spend ceiling are approved. Pricing/listing/agreement status never counts as invocability proof."

Rev-1 and rev-2 carried this as blocking decision D-A. **It is ruled.** What this story implements:

- **Ships disabled, zero budget.** Both the enable flag and the spend budget are **required per-environment configuration with no permissive default** — the flag defaults to disabled and the budget to zero. A missing configuration value must not read as "unlimited"; an absent budget is zero, and a zero budget means no probe runs. This is the difference between a default-off feature and a feature that is off because nobody set it yet.
- **No request path and no page load may trigger a probe.** Catalogue reads, validation calls, saves and snapshot builds all serve recorded evidence only. There is no lazy-probe-on-miss path: a miss is "not proven", never "let me find out", because a lazy path is exactly how a page load starts spending.
- **Two admissible triggers when enabled, both server-side:** a configured nightly schedule, and a catalogue-change or destination-change event (a new model row, a new destination for a principal). Nothing else.
- **Enabling is PMM-09's gated action** (#5427), and requires explicit target-account and spend approval before it runs. Merging this story's implementation enables nothing.
- **Bounded per probe and per cycle.** One attempt, a tight timeout, and a per-cycle ceiling — so that even once enabled, a misconfigured destination list cannot turn a nightly job into an unbounded spend. **Corrected in rev-4:** rev-3 listed "`max_tokens` at the floor" as a per-probe bound. The real harness exposes no such lever (§4.2a, `run-query.ts:297-303`), so the per-probe bounds are the SDK levers that exist — `effort`, `maxTurns`, and `maxBudgetUsd` — and the per-cycle ceiling is the one that actually protects the account. Cost scales as candidate models × destinations × cadence, and destinations grow per-principal (#4692).

### 4.7 What the surface returns while probing is disabled — new in rev-3

This is the state PMM-03 actually ships in, so it is the state the consumers must build against. Left unstated, PMM-04 and PMM-05 would build against a populated catalogue that does not exist until PMM-09.

- Every model row reports `invocable: null` with **no evidence record** — distinct from `invocable: false`, which means a probe ran and the model refused. Conflating "unproven" with "refused" would tell an operator a model is broken when nothing has been tried.
- The row states **why** it is unproven, in the same refusal vocabulary: `probing_disabled` rather than `not_invocable`. An operator reading "not invocable" would go looking for a Bedrock problem that does not exist.
- **Nothing is selectable.** A validation call refuses every model on gate 5, so PMM-02's save path refuses every save while probing is off. That is correct fail-closed behaviour and it is also a product consequence the operator should see plainly: **the Agent Models screen cannot accept a saved preference until PMM-09 has enabled probing and recorded evidence.** #5422's backend-served `agent_models` flag, default false and enabled only in PMM-09, is the consistent way to keep users from meeting that refusal.
- The catalogue read still **succeeds** and still lists personas, classes, models, price context and retirement state. It is the *certification* that is withheld, not the list — an empty or failing read would be indistinguishable from an outage.

---

## 5. Migration from the inert fields — report-only first (D3)

D3 assigns PMM-03 "migration from the currently inert fields" and mandates "report-only first, with existing principals backfilled and compared before the enforcing flip in PMM-09". Given C2, the report-only stage is not a formality — it is the control that keeps the flip from being an outage.

1. **Inventory.** Enumerate every registry row's `allowed_models` and evaluate it against the canonical IDs the platform actually invokes.
2. **Report.** For each row, emit: the patterns, which canonical IDs they match, and whether the set is empty. Per the repo's no-silent-caps rule, an empty result must be *reported*, never rendered as "restricted".
3. **Correct the data.** Rows whose patterns match nothing need a decision per row — widen to a canonical pattern, or replace with the pinned IDs intended. This is a data correction with an operator in the loop, not an automated rewrite: silently widening `claude-sonnet` to `*.anthropic.claude-*` would grant access nobody reviewed.
4. **Only then** may PMM-07 enforce and PMM-09 flip.

This story delivers steps 1–2 and the report. Step 3 is an operator action informed by it. Step 4 belongs to the named siblings.

---

## 6. Contracts

### 6.0 The endpoint paths are fixed by R6 — corrected in rev-3

Rev-1 described these endpoints by shape and named no URL. Every consumer then picked its own: #5422 and #5423 both read PMM-02's `/me/persona-models`, and neither called anything this note defined. **R6 settles it, and this story does not get its own catalogue path:**

- `GET /me/persona-models/catalog?persona_key=...` — the catalogue read this story implements (§6.1, §6.2).
- `GET /me/persona-models/explain/{persona_key}` — the explainer, which consumes this story's refusal reasons.

Per R6 these FastAPI routes exist **once**, mounted at `/me/persona-models`. Human JWT callers reach that path directly; service SigV4 callers reach `/agent/me/persona-models/...`, and the existing API Gateway integration strips `/agent` before the origin — verified: `"/agent/{proxy+}"` integrates at `uri = "http://${var.internal_alb_dns}/{proxy}"` (`modules/gateway/infra/modules/api-gateway/main.tf:270-285`), so the pod receives the path with `/agent` removed and one backend router serves both. **Do not duplicate the backend router** for the service surface.

Consequence for this story: the catalogue is a **route on PMM-02's router**, not a parallel surface. That resolves a real defect in rev-1 — two packages each owning part of one URL namespace would have produced two refusal vocabularies for one screen.

Conventions that still apply: `/me/*` means "derived from your token, takes no target" (`bedrock_routing/self_routes.py:99-105`); no `/api` and no `/admin` prefix, because CloudFront strips the first `/api` before the origin (#4330, guarded by `tests/test_route_prefix_convention.py`); mirror the five-file layout of `src/admin/bedrock_routing/`. Package: `src/admin/persona_models/`, which PMM-02 (#5419) creates. If this story lands first it creates the package and PMM-02 extends it; the PR states which happened.

`persona_key` is a query parameter, not a target parameter — it selects *which persona's* class to filter by, and is not a principal. The principal is always the caller.

### 6.1 Persona catalogue read

Authenticated, no principal parameter. Rows: `key`, `display_name`, `purpose`, `configurable`, `not_configurable_reason` (nullable, §2.3), and **`compatibility_class`** (new in rev-3, §2.4). Derived from `VALID_PERSONAS` per request; the class comes from the §2.4 registry.

### 6.2 Selectable-model catalogue read

Authenticated. **No principal parameter** — the destination is resolved server-side from the caller's identity, so a caller cannot ask "what may *that* principal select". Filtered by the target persona's compatibility class.

Row fields, reconciled in rev-3 against the consumers' current heads (#5422 renders these, #5423 prints them):

| Field | Notes |
|---|---|
| `canonical_model_id` | The versioned identifier. |
| `model_family`, `canonical_version` | Two fields, e.g. `"Sonnet"` and `"4.6"` — **changed in rev-3** from rev-1's single `family_display_name`, adopting #5422's shape, which needs them separately to render the family with its version. |
| `selectable` | The single boolean the consumers branch on. |
| `reason` | Refusal code when `selectable` is false — **renamed in rev-3** from `refusal_reason` to match both consumers. |
| `permitted`, `invocable` | **Independently tri-state** (`true`/`false`/`null`), per #5422. `null` means unevaluated or unproven; §4.7 requires `invocable: null` while probing is disabled, and folding these into one boolean would erase the unproven-vs-refused distinction. |
| `evidence{account_id, region, verified_at, expires_at, stale}` | Nested, and **retained over #5422's flat `evidence_at`** — see the note below. |
| `compatibility_class`, `harness_contract_revision` | Per R2: stable unversioned class ID, separate versioned revision. Replaces rev-1's `harness{id, contract_revision}`. Rev-4 fixes these as the canonical spellings (§2.4). |
| `evidence.request_shape_sha256` | **New in rev-4** (§4.1b). The digest of the request the probe actually sent. Nullable only when there is no evidence row at all. Consumers need not interpret it; it is in the contract so a reader can tell *whether* a proof was harness-shaped and so two proofs are distinguishable when they should not be merged. |
| `retired` | §4.5. |
| `price_context` | Nullable, presentational only (§3.7). |

**Why `evidence` stays nested with the destination in it.** #5422 flattened it to `evidence_at` + `stale`, dropping `account_id`, `region` and `expires_at`. Those cannot be dropped: invocability is a property of a destination (§4.1), AC-05's entire content is that two principals with different destinations see different answers, and an operator debugging "not invocable" cannot act without knowing which account refused. The UI may *render* only the age — but the field must be in the contract, or the CLI's JSON output and the explain endpoint lose the only facts that make a refusal actionable. This is a divergence #5422 should adopt rather than one this note should concede; it is listed in §12 for the synthesis.

### 6.3 The shared validation function — the contract that matters most

One definition of "selectable", called by the catalogue endpoint and by PMM-02's save path. If they disagree, the screen offers a model that save rejects, or save accepts one that dispatch cannot run.

**Signature, adopted in rev-3 from PMM-02's note (#5437) rather than restated loosely.** Rev-1 said "caller context", which PMM-02 correctly rejected as under-specified: a polymorphic identifier string is the #4744 defect class, where a value that looks like an ID silently matches no row.

```
validate_selection(db, *, org_id, principal_kind, canonical_principal_id,
                   persona_key, model: str) -> Selection | Rejection
```

- **`canonical_principal_id`** is the opaque immutable ADP identifier from R1's alias contract, which PMM-02 owns. This story **receives** it and never resolves, guesses or accepts an alias: per R1, "raw `service_accounts.id`, `agent_name`, `client_id`, ARN or caller-supplied text never owns a preference". Self handlers derive it from authentication; administered handlers accept only IDs returned by PMM-02's manageable-service-principals surface. **This replaces rev-1's `principal_source` reasoning**: source-qualification was PMM-02's earlier direction and R1 supersedes it with the alias registry, so the canonical ID is unambiguous by construction and needs no namespace qualifier. Provenance is retained by PMM-02 for audit, not as part of this key.
- **`persona_key`** determines the compatibility class (§2.4), which is gate 1.
- **Out, on success:** the canonical versioned ID, the class it was validated for, and the evidence row that justified it.
- **Out, on refusal:** a stable reason code in the `422 {reason, message}` shape (`bedrock_routing/self_routes.py:108-115`).
- **Invariant:** the catalogue endpoint is a thin caller of this function, not a parallel implementation.

**Reason-code vocabulary, fixed as strings in rev-3.** Rev-1 gave prose labels, and the consumers each invented spellings — a client cannot branch on prose. The vocabulary:

`unknown_model` · `unknown_persona` · `not_permitted` · `harness_incompatible` · `not_invocable` · `probing_disabled` · `evidence_stale` · `retired` · `alias_not_pinned` · `no_class_default`

`probing_disabled` and `not_invocable` are deliberately distinct (§4.7). `no_class_default` is the refusal when a class has no registered default, which per R2 is never a substitution from another class.

**PMM-06 is not a caller — corrected in rev-3.** Rev-1 named #5424's snapshot builder as the third consumer. Its current head resolves mappings directly from PMM-02's Postgres rows at gateway work-admission and re-checks admission live at every hop, deliberately: R4 puts snapshot resolution in the gateway, and the snapshot "freezes the choice, never the permission". That is the correct division, and this note's rev-1 claim was wrong. Two callers, not three. What #5424 needs from this story is the class and contract revision to bind into the signed assertion (§3.4), not a validation call.

**A read must not raise a refusal — resolved in rev-3.** #5437's list endpoint calls this validator *per row* at read time and expects `unavailable`/`disallowed`/`stale` as reported per-entry states; rev-1 said a validation call on stale evidence refuses. Both are needed, so the function returns a `Selection | Rejection` **value** and the caller decides the HTTP shape: a catalogue or list read renders the rejection's reason as that row's `reason` field with `selectable: false`, while a save or an explicit validation converts the same rejection into a 422. One decision of "selectable", two presentations. A function that could only raise would make the list endpoint unimplementable.

**PMM-02's write gate:** #5419 gates its write behind this interface, and if this story has not merged it codes to the interface with a minimal in-repo implementation and says so in its PR, per its own design clause.

### 6.4 Tenancy and security boundary

- Tenant-scoped reads only; cross-tenant read, write and cache key fail closed. Evidence cache keys include tenant and destination — a shared key would leak one tenant's destination facts to another.
- The catalogue grants nothing. Read-only, no new IAM, no broadened scope: the probe runs under the gateway's existing Bedrock access.
- No credential values, role ARNs or account secrets in responses. `account_id` appears inside evidence because the operator cannot debug "not invocable" without knowing which account refused — the same labelled-source reasoning as `BedrockTarget.rung` (`bedrock_routing.py:54-58`).
- The probe is never reachable from an unauthenticated path.

### 6.5 AC-05 constraint the story does not name, and how R1/R5 narrow it

AC-05 requires a human caller and a service-account caller with differing destinations to each see their own intersection. Verified obstacle:

`resolve_routing_principal` (`src/proxy/bedrock_principal.py:42-65`) raises `BedrockRoutingIdentityError("missing_run_id")` when a hosted service caller (`account_type == "service"`, `auth_source == "iam"`, `scope in {internal, platform}`) presents no `agent_run_id`. **A catalogue read has no run id** — it is not an invocation.

The story's own prerequisite table notes `:60-65` (an explicitly service-rooted job keeps its own service routing and does not inherit a person's) but not the missing-run-id refusal, which is the part that breaks a catalogue read.

**Resolution, narrowed in rev-3 by R1.** Rev-1 offered two options. R1 removes the ambiguity about *identity* while leaving the *destination lookup* to this story:

- **Identity is not this story's to resolve.** The caller arrives already resolved to a `canonical_principal_id` (R1: the self API derives it from authentication). This story never joins a role ARN or an `agent_name` to a principal — #5423's P-2 prerequisite correctly places that join in PMM-02 (#5419). Verified: `find_service_account_by_role_arn()` exists (`src/auth/service_account_service.py:324-352`) but its only production caller is `tenant_resolver.py:147-149`, so no code path maps a live SigV4 caller to a principal today. That is PMM-02's gap to close, and it is why §8 lists PMM-02 as this story's hard dependency rather than as a parallel peer.
- **Destination lookup, for a catalogue read only:** resolve from the principal's **registered routing** directly, bypassing the run-bound path, because there is no run to bind to and a catalogue read is not an invocation. The `:60-65` invariant is preserved and is in fact the reason this works: a service-rooted principal keeps its own routing and never inherits a person's, so there is nothing a missing run id was protecting. Option (b) from rev-1 — refusing hosted callers — is **withdrawn**: #5423's CLI and #5422's service-account scope both need a service principal to read its own catalogue before any run exists, so refusing would break AC-05 rather than defer it.

**R5 and this endpoint.** Root ownership follows the authenticated initiator, not the credential that executes a job: a human-initiated event keeps the resolved canonical human root, and a scheduled or service-to-service run with no authenticated human initiator resolves a tenant-bound registered canonical service principal and **fails closed if unregistered**. For a catalogue read that means an unregistered caller is refused rather than defaulted to a tenant-wide answer. Workflow and App execution identities are audit attribution only and never become the reader whose destination is used.

AC-05 must assert **both** directions — each principal sees its own intersection, and neither sees the other's.

---

## 7. The Fable problem — this story owns it

The epic's headline example configures the architect persona on Fable 5.1 and asks to prove Fable, Opus and Sonnet are shown only when actually invocable. Re-verified at `c4809bb1`:

1. **No Fable alias in either map.** Both mention Fable only in an exclusion comment.
2. **Both exclusions are deliberate.** `claude-fable-5` is "listed ACTIVE but do NOT invoke for us" because it "requires non-default data-retention mode" — `model_validate.py:34-38`, `model_resolver.py:31-32`, added by the #2300 fix.
3. **Fable exists only in pricing artifacts.** `pricing_policy/snapshots/2026-09-12.2.json` (which carries `anthropic.claude-fable-5-1` and its `us.`/`global.` profile variants), `alembic/versions/047_claude_pricing_v2.py`, and pricing test fixtures. Grep hits in `src/admin/connections/service.py` and several frontend files are false positives — the substring "diffable" (`service.py:1697`, `personCap.ts:23`, `budget.ts:533`, `bedrockRouting.ts:14`) and "spoofable" (`tests/orchestration/test_github_adapter.py:493`). **No Fable model reference exists in runtime code, configuration or deploy scripts outside pricing** — the exhaustive case-insensitive sweep leaves only pricing artifacts, the two exclusion comments in (1), those substring false positives, and two prose mentions in unrelated design docs (`docs/design-3159-aidlc-v2-hosted-agents.md:296`, describing model pins in AIDLC's *own* shipped `settings.json`, which is not a file in this repo; and `docs/design-notes/4990-claude-bedrock-pricing.md:34`, pricing). Neither prose mention is an alias, an entitlement or an invocability record.
4. **Not deploy-gating.** `platform/scripts/enable-bedrock-models.sh:40-42` requires only `anthropic.claude-opus-4-6-v1` and `anthropic.claude-sonnet-4-6`.
5. **No issue establishes Fable 5.1 as invocable here.** #5417 is the only issue naming it as selectable.

**Pricing coverage is not entitlement and not invocability.** That distinction is the entire lesson of #2300, and a catalogue that lists Fable 5.1 because a price row exists is the named plausible-wrong-result that must fail AC-04.

**Required probe set.** Fable 5.1 is not one identifier. The pricing snapshot carries `anthropic.claude-fable-5-1`, `us.anthropic.claude-fable-5-1` and `global.anthropic.claude-fable-5-1`. D4 prefers versioned `us.` inference profiles over `global.` for account portability, so probe the `us.` profile first, then `global.`, then the bare foundation-model id — and record each separately. A bare-id failure does not establish a profile failure, or vice versa.

**Default disposition — settled, and re-confirmed by R3: Fable is unavailable unless a real bounded probe succeeds.** This is the fail-closed default, not a pending decision, because it is the same rule §4.3 applies to every model; R3 adds that "pricing/listing/agreement status never counts as invocability proof". A successful probe is the only thing that changes it, and under R3 no probe runs until PMM-09 (#5427) is enabled with account and spend approval.

**Strengthened in rev-4 — the contrary evidence is now card-level and names the exact operator action.** Rev-3 rested Fable's contrary evidence on the two in-repo exclusion comments, which name `claude-fable-5`. The repo also holds a **hash-verified AWS model card for Fable 5.1 itself**: `modules/gateway/tests/pricing_policy/fixtures/aws/claude/model-card-anthropic-claude-fable-5-1.md:191` — "To use this model, you must opt in to AWS review by setting your data retention mode to `aws_review` via the **Data Retention API**." Provenance is recorded in `card-source-manifest.json` (url, bytes, sha256, `verified_at: 2026-09-12`). Two consequences:

- **The gate is a different API from the one the platform automates.** `enable-bedrock-models.sh` calls only the marketplace-agreement APIs (`create-foundation-model-agreement`, `get-foundation-model-availability`). A repo-wide search finds **no call to the Data Retention API anywhere** — the only hits for data retention are the two exclusion comments (`model_validate.py:35`, `model_resolver.py:32`). So Fable 5.1 cannot become invocable as a side effect of any existing deploy step; it requires a distinct, explicit, recorded operator action.
- **Fable 5.1 also has a harness-shape dimension**, per §4.2a: `:23` records that adaptive thinking "is always on and cannot be disabled". A harness sending an explicit `thinking` configuration is exercising a parameter this model treats differently from Opus 4.6 — another reason its evidence must be keyed by request shape rather than by model identity alone.

**Corrected in rev-3: Fable is not a special case, and neither is anything else.** Rev-1 read as though Fable were the one model awaiting proof. Under R3, *every* model in the catalogue is unproven at this story's completion — including the D4 Claude-class candidate, which #5427 requires to pass its own bounded invocation with the real harness request shape before consolidation. Fable's distinguishing fact is narrower: it has *contrary* evidence (an explicit non-default data-retention requirement recorded in both exclusion comments), not merely absent evidence. PMM-04 builds the unproven row shape (`invocable: null`, reason `probing_disabled`) for all models, and the `invocable: false` shape for a model that probed and refused.

**Expected outcome, stated in advance so a pass is not read as a surprise:** given (2), the most likely result is non-invocable pending a data-retention mode change. If so:

- Fable is **absent** from the catalogue, or present and marked non-invocable.
- The epic's example table must be **corrected** — it currently certifies a configuration nobody has verified.
- The operator decides whether to enable the required data-retention mode. That is an explicit operator action recorded against the prerequisite; this story never performs it silently.
- AC-04 is **not** recorded as passed because the mechanism worked. Per the story's own completion-report rule, the probe result is reported per model family, separately.

---

## 8. Prerequisites, settled decisions, and what runs in parallel

### No operator decision blocks this story

**Every decision rev-1 and rev-2 raised is now settled.** Recorded here so a reader does not re-open them:

| Was | Now |
|---|---|
| **D-A — probe cadence and per-probe token ceiling** (rev-1's one blocking gate) | **Ruled by R3.** Ships disabled at a zero budget, with cadence and spend as required per-environment configuration; nightly and change-triggered refresh only; enabling belongs to #5427 after target-account and spend approval. Implemented per §4.6; nothing further is asked of the operator here. |
| **D-B — Fable disposition** | **Settled, not a decision.** Fail-closed by the same rule as every model (§7), and R3 re-confirms that listing and agreement status are not proof. Enabling the data-retention mode remains an operator action, but it is a prerequisite for *making Fable available*, not for building this story. |
| §2.3 `pt-superpower` configurability | **Settled:** listed with `configurable: false` and a reason naming #4037. Changing it requires #4037 closed first. |
| D3 — is the allowlist gate in scope? | **Locked: enforced within this epic.** But see C2 — the gate's *data* is broken, which is why §5's report-only stage is this story's deliverable. |
| Effective-destination resolution for a service caller | **Settled** (§6.5): identity arrives canonical per R1; destination resolves from registered routing; rev-1's option (b) withdrawn. |
| §2.1 persona-source strategy (a) or (b) | Implementer's choice with a parity test either way — an engineering decision, not an operator one. |
| Which workflow deploys this | `gateway-deploy.yml` fires on merge for gateway source. Confirm against `docs/adp-platform-deployment/deployment-manifest.md` at implementation time. |

**No ownership question remains open here — corrected in rev-4.** Rev-3 listed the class-registry/#5433 overlap as an open operator decision. R2 and PMM-01 §1.7a both settle it in this story's favour, so §2.4 records this note as the authority and §2.4b records #5433's obligation to register into it (§9a). The only residual is a file-location detail PMM-01 §6.7a itself calls non-blocking, needed "before #5433 registers its first `gpt-*` persona, not before PMM-02 or PMM-03 begin".

### Hard dependency — engineering, not a decision

**PMM-02 (#5419) must land two things this story consumes and neither exists today:**

1. **The canonical service-principal ID and its alias registry** (R1). This story's validator takes `canonical_principal_id` as an input (§6.3) and cannot resolve one. Verified absent: no `canonical_principal_id`, `canonical_service_principal_id` or manageable-service-principals symbol anywhere under `modules/`, and the only ARN→service-account join in the codebase (`service_account_service.py:324-352`) is called solely from `tenant_resolver.py:147-149`.
2. **The versioned class-keyed default/posture record** (R2), which this story supplies the class vocabulary for but does not own.

What proceeds meanwhile: the persona catalogue and class registry (§2.1, §2.4), the catalogue artifact and its evidence schema (§3.3, §4.4), the probe mechanism in its disabled posture (§4.6), the §5 allowlist report, and the validator coded against the §6.3 signature with the principal ID as a parameter. Human-JWT callers are fully serviceable without (1); it is the service-principal path and AC-05's service half that wait.

### Parallelism, reconciled against sibling heads

| Can start now | Must wait |
|---|---|
| **#5422 (UI)** and **#5423 (CLI)** — against the §6.0 paths and the §6.2 row shape, rendering the §4.7 unproven state as the *normal* state rather than an edge case | **#5425 (PMM-07 resolver)** — needs §6.3 merged plus §5's data correction, or enforcement is an outage |
| **#5424 (PMM-06 snapshot)** — consuming §2.4's class IDs and §3.4's contract revision as snapshot keys; it does **not** call §6.3 (§6.3's PMM-06 note) | **#5427 (PMM-09)** — the flip needs §5's report acted on, and it owns enabling the probe and recording every invocation that certifies a model |
| **#5419 (PMM-02)** — in the sense that this story consumes its identity slice: its schema work is upstream of this story's service path, not parallel to it | |

Corrected from rev-1, which listed #5419 as "can start now" against this story's interface. Its own current head states the alias→canonical table blocks its schema, so the dependency runs the other way for the service-principal path.

---

## 9. Where this note corrects the issue text

| Issue claim | Verified state | Section |
|---|---|---|
| "Invocability probe precedent: reuse the bounded-probe pattern" (reads as reuse the mechanism) | That probe deliberately never succeeds; a *pass* is `ValidationException`. Shape reusable, mechanism not | C1, §4.2 |
| D3 "migration from the currently inert fields" reads as wiring | Seeded values are family names matching zero canonical IDs; enabling the gate on current data is an outage | C2, §3.5, §5 |
| "Existing model list endpoint — reuse or extend" | Keep `/v1/models` unchanged (OpenAI-compatible, external consumers); the catalogue is a new endpoint. Its only in-repo reference is a mock log line | §3.3 |
| Gateway alias map "reuse for alias resolution" | Correct for aliases, but it must not be the catalogue: contains Claude 3/3.5, non-Anthropic families, and a known non-invocable Legacy mapping | §3.3, §3.6 |
| "Fable 5.1 exists only in pricing artifacts" | Confirmed for runtime code, configuration and deploy scripts: the remaining grep hits are "diffable"/"spoofable" false positives plus two prose mentions in unrelated design docs, none of them an alias or entitlement record (§7 item 3). Also: Fable 5.1 is **three** identifier variants, each needing its own probe | §7 |
| Service-account destination "partially verified" | One further obstacle: `resolve_routing_principal` refuses a hosted service caller with no `agent_run_id`, and a catalogue read has none | §6.5 |
| The story's AC-04 asks this story to probe Opus, Sonnet and Fable 5.1 | **Incompatible with R3**, which ships probing disabled at a zero budget and gives enabling to #5427. AC-04 splits into the mechanism (here, verified inert) and the invocations (#5427) | §4.6, §4.7, §10 |
| The story does not mention harness compatibility at all | R2 assigns this story the persona→compatibility-class registry, which no platform component provides today. New scope, new AC | §2.4 |
| The story implies one canonical default model | R2 makes defaults class-keyed and the D4 identifier a Claude-class **candidate** pending a bounded invocation with the real harness request shape. This story seeds structure, not a proven default | §3.3 |
| Epic inventory of model-selection paths | Omits `complex-task-chat-agent.ts:443` `persona.modelOverride`, an existing per-persona override. PMM-07's inventory must include it | §3.4 |
| Not in the issue: harness baseline | `DEFAULT_ALLOWED_PATTERNS` includes `openai.*` by design (#2709/#2713). D6 permits Claude only, so a separate persona-selection baseline is required | C3, §3.4 |

---

## 9a. Where rev-4 corrects this note's own earlier revisions

Recorded separately from §9 because these are defects in the design note, not in the issue text. Each was found by re-verifying rev-3's citations at `c4809bb1`, and each is a removal rather than an addition — per the review, superseded alternatives are deleted, not retained alongside the ruling.

| Rev-3 said | Verified state | Fix |
|---|---|---|
| §2.4b: class-registry ownership is an open operator question; §0 and §8 repeated it | **Settled.** R2 assigns it here, and PMM-01's current head (#5436, `b8045dbf`) §1.7a is titled "settled" and says the decision "has been made and this section records it as closed, not pending" | Ownership stated as settled in §2.4; §2.4b rewritten as #5433's registration obligation; the question removed from §0 and §8 |
| §4.2, §4.6: the probe body sets "`max_tokens` at the floor" | **Not implementable through the real harness.** The Claude Agent SDK `Options` type has no `maxTokens`/`maxOutputTokens` field — only `effort` (`run-query.ts:297-303`, `channel-profiles.ts:106-112`) | Claim withdrawn; §4.6 bounds the probe with `effort`/`maxTurns`/`maxBudgetUsd` plus the per-cycle ceiling |
| §4.3: the `ValidationException` class is "the #1128 `thinking.type.enabled is not supported for this model`" error | **False attribution.** Issue #1128 is "[test] Verify agent ScaledJob uses us.anthropic.claude-opus-4-8" and contains no such string. The real, citable evidence is the model card: Opus 4.7 returns a 400 for `thinking.type: "enabled"` with `budget_tokens` (`model-card-anthropic-claude-opus-4-7.md:27`) | Citation replaced with the hash-verified card. PMM-01's head separately records that #1128's Opus 4.8 smoke "failed on incompatible thinking parameters and was reverted", which is the accurate version of the point rev-3 was reaching for |
| §4.1: evidence keyed by `(canonical_model_id, account_id, region)` | Cannot distinguish a harness-shaped proof from a bare invoke, and inherits evidence across a harness upgrade | Five-part key, §4.1b |
| §4.1/§4.4: "evidence store" named no store | No probe-result store exists in the tree; no sibling defines one | §4.1a names `model_invocability_evidence` in Postgres on the migration-044 pattern |
| C1, §4.2: `routing_probe.py` cited bare, at `:107-112` | The file is `modules/gateway/src/shared/services/routing_probe.py`; `_PROBE_BODY` is at `:111` with its comment at `:107-109`; the timeout/attempt constants are at `:115-116` | Path and line ranges corrected |
| §3.7, §4.4: `pricing_v2_reader` cited as the staleness state machine | That module's `cached_rate_state`/`refresh_due`/`cache_failure_age_seconds` are thin delegations (`:165-180`); the state machine is `V2RateCache` in `modules/gateway/pricing_policy/storage.py:223` — a top-level package, not under `src/` | Both citations corrected to name `V2RateCache` as the thing to mirror |
| §12: seven sibling SHAs | **Six of the seven had moved.** Only #5435 (`5fc0fb4f`) was current | §12 refreshed to the heads listed there |

---

## 10. Acceptance evidence — what is provable how

| AC | Provable by | Note |
|---|---|---|
| AC-01 persona added/removed flows through | pytest + the §2.1 parity test | Structural: any second list fails it by construction |
| AC-02 exactly 12 keys, `pt-superpower` handled | pytest; both existing drift guards still pass | Copy the anti-vacuity row-count assertion from `test_persona_catalogue_parity.py` |
| AC-03 alias → canonical; bare "latest" refused | pytest | §3.6 |
| AC-04 probe for Opus, Sonnet, Fable 5.1 | **Not achievable in this story under R3 — see below** | The mechanism and its evidence schema are provable here; the invocations are #5427's |
| AC-05 human vs service-principal destinations | pytest, both directions | Service half needs PMM-02's canonical ID (§8) |
| AC-06 source unavailable → stale, nothing certified | pytest | §4.4 |
| AC-07 retired → flagged, not selectable, mappings visible | pytest | §4.5 |
| AC-08 evidence expires → stale | pytest with injectable clock | §4.4 |
| New, from R2 — persona resolves to a compatibility class; no cross-class fallback | pytest | §2.4. Not in the filed story's AC list; the class registry is new scope R2 assigned |

**AC-04 must be re-scoped, and the operator should see this explicitly.** The filed story asks this story to probe Opus, Sonnet and Fable 5.1 and record the results. R3 forbids exactly that: probing ships disabled at a zero budget and only #5427 may enable it after account and spend approval. These cannot both hold. The resolution consistent with R3 is to split AC-04:

- **AC-04a, provable here:** the probe mechanism exists, issues its call **in the class's actual harness request shape** rather than a hand-written minimal body (§4.2a), records the five-part evidence key including `request_shape_sha256` plus request ID and timestamp into the §4.1a store, treats every exception as non-invocable, and is **verified inert** — a test asserting that with the default configuration no Bedrock call is attempted by any read, save, snapshot or page load. Add two tests rev-3 did not imply: one asserting the probe does **not** route through `/v1/messages` (which strips the fields under test, §4.2a), and one asserting that changing the harness revision or the request shape yields a different evidence key rather than reusing the old row.
- **AC-04b, #5427's:** the actual bounded invocations for each model and each Fable 5.1 variant, with recorded request IDs, in an approved account under an approved ceiling.

A completion report for this story that claims AC-04 passed because the mechanism works would be the #2300 defect in its purest form. The correct statement is "the mechanism is built and inert; no model is certified".

Module checks for the implementation PR: `cd modules/gateway && ruff check src/ tests/ && ruff format --check src/ tests/ && python3 -m pytest tests/ -q`.

**Completion-report discipline (restated because it is the anti-pattern this epic exists to prevent):** report per model family, and per Fable 5.1 variant, with account, region and timestamp — and where nothing was probed, say that plainly rather than reporting the mechanism's success. Pricing coverage, listing and agreement status are never evidence (R3).

---

## 11. Deployment and rollback

- **Environment:** gateway module, dev first. Probe runs under the gateway's existing Bedrock access — no new identity, no broadened IAM scope.
- **Deploy path:** `gateway-deploy.yml` fires on merge for gateway source; confirm against `docs/adp-platform-deployment/deployment-manifest.md` at implementation time. Per CLAUDE.md, Gateway Infra Apply is manual by design — if the evidence store needs a migration, that apply is an operator action, not an automatic consequence of merge.
- **One-time setup:** none. Per R3 the probe's enable flag and spend budget ship as required configuration defaulting to disabled and zero, so a deploy of this story invokes nothing and spends nothing. Enabling it is #5427's separately approved action, and a data-retention mode change for Fable, if chosen, is an explicit recorded operator action.
- **Rollback:** revert the PR. The surface is additive and read-only, so nothing downstream loses a capability it had. Leaving the probe disabled leaves the catalogue serving uncertified rows rather than certifying selections — the fail-closed direction.
- **Rollback is genuinely code-only** here: if the evidence store is a new table, dropping it loses only recomputable evidence, no principal-authored data. That is why "revert the PR" is a valid rollback for this story and would not be for PMM-02.

---

## 12. Reconciliation — what R1–R6 changed, and where siblings still disagree

**Sibling heads re-read for rev-4** — six of the seven SHAs rev-3 cited had already moved, so every claim below was re-verified rather than carried forward (§9a):

| Story | PR | Head at rev-4 | Moved since rev-3? |
|---|---|---|---|
| PMM-01 #5418 | #5436 | `b8045dbf` | yes (was `78787bd1`) — now **rev-5** |
| PMM-02 #5419 | #5437 | `e2c7d099` | yes (was `d9e83968`) |
| PMM-04 #5422 | #5435 | `5fc0fb4f` | no |
| PMM-05 #5423 | #5441 | `b9e8e96e` | yes (was `c24621bb`) |
| PMM-06 #5424 | #5442 | `f9f0ec68` | yes (was `2d7cde2f`) |
| PMM-07 #5425 | #5438 | `82bc735f` | yes (was `353c04e5`) |
| PMM-09 #5427 | #5439 | `86c7959a` | yes (was `b28547c3`) |

### What this revision changed in response to the rulings

| Ruling | Change here |
|---|---|
| R1 canonical service principal | §6.3 takes `canonical_principal_id`; rev-1's `principal_source` qualifier dropped as superseded; §6.5 states this story never resolves identity and names PMM-02 as the owner |
| R2 compatibility ownership | New §2.4 class registry with stable unversioned IDs and separate contract revision; persona rows carry `compatibility_class`; §3.3 no longer seeds a proven default; §3.4 requires both lookup directions; new AC row |
| R3 probe safety | §4.6 rewritten as ships-disabled/zero-budget with required configuration; new §4.7 states the shipped state; §8's blocking gate retired as ruled; §10 splits AC-04 into mechanism (here) and invocations (#5427) |
| R4 snapshot authority | §6.3 corrects rev-1's claim that #5424 calls the validator — it resolves from Postgres at gateway admission and re-checks admission live per hop |
| R5 ARC root identity | §6.5 adds the initiator-not-executor rule and fail-closed treatment of an unregistered scheduled caller |
| R6 one API contract | New §6.0 fixes the catalogue on `/me/persona-models/catalog` and `/explain/{persona_key}`, one router, `/agent` prefix stripped by API Gateway for SigV4 callers — replacing rev-1's unnamed path, which every consumer had already worked around |

### What rev-4 changed in response to the review on `25ece717`

| Review ask | Change here |
|---|---|
| Make PMM-03 the settled owner of persona-to-class metadata | §2.4 states the ownership as settled, citing R2 and PMM-01 §1.7a ("settled… closed, not pending"); §2.4b rewritten from an open question into #5433's registration obligation; §0 and §8 no longer ask the operator |
| Define one durable authoritative invocability-evidence store | New §4.1a — `model_invocability_evidence` in Postgres, on the migration-044 pattern, with the four rejected alternatives and why each fails |
| Key evidence by destination, canonical model, compatibility class, harness/contract revision, actual request-shape revision | New §4.1b — the five-part key, with `request_shape_sha256` as part 5 and the argument for why part 4 cannot substitute for it; §6.2 exposes it |
| A minimal direct invoke body is not proof the real SDK harness works | New §4.2a — grounded in three in-repo proofs (Opus 4.7's 400 on `thinking.type: "enabled"`, the Codex `web_search` 400, the SDK's masking of failures as success) plus the `/v1/messages`-strips-tools asymmetry; rev-3's "`max_tokens` at the floor" withdrawn as not implementable |
| Remove stale open-decision text | §2.4b, §0 and §8 rewritten; §9a records every removal, with superseded alternatives deleted rather than kept alongside the ruling |
| Update the PR description | Done on PR #5434 alongside this revision |

### Divergences the synthesis still has to settle

These are stated as disagreements, not decided here.

1. **Evidence shape.** This note keeps `evidence{account_id, region, verified_at, expires_at, stale}`; #5422 flattened it to `evidence_at` + `stale`. The destination fields are load-bearing for AC-05 and for debugging a refusal (§6.2), so the recommendation is that #5422 adopt the nested shape and render only what it needs. **Recommendation, not a ruling.**
2. **Refusal vocabulary.** Four disjoint sets exist across the epic: this note's ten codes (§6.3), #5422's four, #5442's eleven snapshot codes, and #5438's resolver reasons. One screen branching on four vocabularies needs four parsers. This note's set covers catalogue refusals only and does not claim to cover snapshot or dispatch refusals; the synthesis should declare one union with owners per prefix.
3. **Who closes the `us.` alias gap.** No alias resolves to `us.anthropic.claude-sonnet-4-6` today; #5441 assigns closing that to #5427. This note agrees and explicitly does not add the alias (§2.4).
4. **Field-name spellings for the class and revision.** Real and unresolved across three heads: PMM-02 stores `harness_compatibility_class` + `harness_contract_revision`; PMM-06 spells the second `harness_compatibility_revision`; PMM-07 carries a bare `compatibility_class`. As the settled owner of this metadata (§2.4), this note **fixes the spelling** as `compatibility_class` + `harness_contract_revision` rather than listing options: the revision spelling is the one PMM-02 has CHECK-constrained in a UNIQUE-on-class table, and PMM-01/PMM-05 use it too. PMM-06's `harness_compatibility_revision` should be renamed to match. **This is a ruling within this note's ownership, not a recommendation to the synthesis.**
5. **Where the evidence store lives** — settled here by §4.1a, recorded because all seven siblings deferred it and none may now invent a second one. PMM-02 joins it synchronously; PMM-06 reads it and must not project it into DynamoDB.
6. **PMM-06's head carries a stale reading of this note.** #5442 `f9f0ec68` states that "PMM-03's head currently supplies **no class ID at all** and still calls Sonnet 4.6 canonical". That was true of rev-1/rev-2 and is not true of rev-3 or rev-4: §2.4 defines the class IDs and §3.3 records Sonnet 4.6 as a candidate. PMM-06 should refresh against this head; no design change is needed here.
7. **Story-label vs issue-number mismatches.** #5435 attributes retirement alerting to "#5426" while calling it PMM-08 elsewhere; #5439's dependency list names issue numbers with no PMM labels; PR #5434's branch is `agent/issue-5420`, and a separate PR #5445 exists on `agent/issue-5434` with no design note. This note now uses issue numbers with the PMM label in parentheses, and the synthesis should publish one mapping table.
8. **Nobody ratified the probe posture before R3.** Every sibling said this story owns the probe and that client reads never trigger it; none stated a cadence, a default-off posture or a spend gate. R3 supplies all three, so §4.6 is now the single place it is written down and the siblings' notes are silent rather than contradictory.
