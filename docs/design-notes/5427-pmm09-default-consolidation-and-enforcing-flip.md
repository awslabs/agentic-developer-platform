# Design Note: Default consolidation, shadow comparison, live matrix and the enforcing flip (PMM-09 / #5427)

**Status:** PROPOSED, subordinate to the #5417 synthesis.

Three layers of operator ruling now bear on this note, in increasing authority:
the **#5418 lockings** (D1-D6), the **PR-level synthesis contract** of
2026-09-18 (§11, S1-S7), and the **binding #5417 unified rulings** of the same
date (§12, R1-R6) which are stated to supersede conflicting story-local
recommendations. Where they differ, **§12 governs §11, and §11 governs §3.3,
§6.2 and §10.** The epic-level **architecture synthesis gate** additionally makes
this a story-local input that **may not override the canonical design** (§11.1).

**Decision status: one open.** Five of the six decisions raised across revisions
are settled (§10) — decision 6 is withdrawn as answered by R2 (§12.1), and the
target account is now confirmed. What remains is the **Claude-harness invocation
proof for the ruled default**, which R2 reframes as PMM-09's own promotion step
rather than a question to answer first, plus the **unapproved live spend
ceiling** which gates the live phase but is a pre-run gate rather than a design
question. PMM-09 remains blocked on sequencing regardless (§1.2, §11.1).

**Third-pass revision (this one)** responds to the CHANGES_REQUESTED review at
`86c7959a`. It removes the superseded rollback option menu in favour of the one
ruled mechanism (§6.2), closes the D5 sequencing question rather than posing it
as an operator call (§6.5), replaces §7.2's single uniform evidence tuple with
**evidence per outcome kind** (§7.2.0), adds the missing `adp` CLI
service-self SigV4/M2M cell (**L25**, §7.2.1) bringing the matrix to **25
cells**, separates selection-shadow differences from live admission refusals
(§5.3), and pins refusal codes to PMM-03's exact string vocabulary (§7.2.3). It
also re-resolves every citation against current sibling heads (§12.2) — four of
which have moved in ways that change claims here, including PMM-01's withdrawal
of the second-live-class claim that §3.5 had asserted.

The preceding second-pass revision rewrote §7.2 as an executable matrix after
the operator ruled the prior seven-row table insufficient to satisfy S5, added
deployment anchoring (§7.7) and recorded the #5417 unified rulings (§12).

**Parent epic:** #5417. **Depends on:** #5418, #5419, #5420, #5422, #5423,
#5424, #5425, #5426 — **all eight are OPEN at the revision reviewed** (§1.2).

**Revision reviewed:** `86c7959a` on `agent/issue-5427`
(merge-base `ae598410` on the default branch). Every file:line citation below —
repo paths and sibling design notes alike — was re-resolved against the working
tree and the sibling heads tabled in §12.2 on this third pass.

**Scope of this note:** the agent-facing design for PMM-09 only — the
consolidation target, the report-only→enforcing comparison gate, the live
acceptance matrix, the flip and its rollback. It does not redesign any earlier
story's mechanism.

**Citation key.** Citations below use basenames for readability. Several are
ambiguous in this repo (15 files are named `config.py`, 17 `routes.py`), so the
load-bearing ones resolve as follows:

| Cited as | Full path |
|---|---|
| `config.py` | `modules/gateway/src/shared/config.py` |
| `routes.py` | `modules/gateway/src/admin/bedrock_routing/routes.py` |
| `configmap.yaml` | `modules/gateway/k8s/configmap.yaml` |
| `bedrock_routing.py` | `modules/gateway/src/proxy/bedrock_routing.py` |
| `model_validate.py` | `modules/agent-factory/webhook-ingress/lambda/common/model_validate.py` |
| `model_resolver.py` | `modules/gateway/src/proxy/model_resolver.py` |
| `personas.py` | `modules/agent-factory/webhook-ingress/lambda/common/personas.py` |
| `github/handler.py` | `modules/agent-factory/webhook-ingress/lambda/github/handler.py` |
| `api-authorizer/handler.py` | `modules/gateway/lambda/api-authorizer/handler.py` |
| `entrypoint.py` | `modules/agent-factory/agent-worker-image/entrypoint.py` |
| `gateway-main.tf` | `modules/agent-factory/infra/gateway-main.tf` |
| `infra/lambdas.tf` | `modules/agent-factory/webhook-ingress/infra/lambdas.tf` |
| `chat-scaledjob.yaml` | `modules/agent-factory/agent/k8s/chat-scaledjob.yaml` |
| `eks/main.tf` | `platform/infra/modules/eks/main.tf` |
| `enable-bedrock-models.sh`, `preflight-check.sh` | `platform/scripts/` |
| `deploy-quickstart.md` | `docs/adp-platform-deployment/deploy-quickstart.md` |
| `codex-config.toml`, `Dockerfile`, `stage-personas.sh` | `modules/agent-factory/agent-worker-image/` |
| `test_codex_config.py` | `modules/agent-factory/agent-worker-image/tests/test_codex_config.py` |
| `proxy/service.py`, `mantle_service.py`, `proxy/exceptions.py`, `pricing_capture.py`, `proxy/routes.py` | `modules/gateway/src/proxy/` |
| `admin/middleware.py`, `admin/models.py` | `modules/gateway/src/admin/` |
| `usage/service.py` | `modules/gateway/src/usage/service.py` |
| `pricing_policy/policy.py` | `modules/gateway/pricing_policy/policy.py` |
| `envelope.py` | `modules/gateway/src/agentauth/envelope.py` |
| `budget-usage-tracker/handler.py` | `modules/gateway/lambda/budget-usage-tracker/handler.py` |
| `shared/models/usage.py`, `shared/models/audit.py`, `shared/models/budget.py` | `modules/gateway/src/shared/models/` |
| `cli/adp`, `cli/README.md`, `bg-gateway-proxy.py` | `modules/gateway/cli/` |
| `lambda-gateway/variables.tf` | `modules/agent-factory/infra/modules/lambda-gateway/variables.tf` |
| `bedrock-invocation-logging/main.tf` | `platform/infra/modules/bedrock-invocation-logging/main.tf` |
| `5417-…md` … `5426-…md` | `docs/design-notes/` — read at the **sibling heads tabled in §12.2**, not at the default branch |

---

## 0. Executive summary

PMM-09 is the story where the per-invoker persona-to-model feature stops
recording what it would have done and starts deciding. It is also the only
story in the epic permitted to claim live acceptance.

> **Read §12 first, then §11.** §12 records the **binding #5417 unified
> rulings** (R1-R6), which supersede conflicting story-local recommendations
> including this note's. §11 records the PR-level synthesis contract (S1-S7).
> Together they settle five of the six decisions and reassign work: the
> enforcement posture becomes versioned runtime policy owned by PMM-02/PMM-07
> (S2), compatibility ownership is PMM-03's and the ruled default is formally a
> **candidate** until PMM-09 records a Claude-harness invocation (R2), the
> snapshot is a per-hop 30-second assertion over a durable digest rather than a
> long-lived signed blob (R4), and no enforcing flip may precede #3186/#5195 and
> #2293 (S4).
>
> **§7.2 is rewritten** as 25 separately-recorded cells after the operator ruled
> the prior seven-row table insufficient to satisfy S5 — it had collapsed the
> four principal/administration combinations into two rows and omitted
> cross-tenant denial, UI/CLI parity and four of five refusal classes. **§7.7**
> is new: the live run is a deployment, anchored to the repository's own
> procedure. **§12.2** reconciles current sibling heads; two have moved in ways
> that change claims here.
>
> **Then read §11.1**, the epic-level synthesis gate: this note is a story-local
> input that may not override #5417's canonical design, and the unpublished
> synthesis is itself a prerequisite for the first developer wave.

Six things this review establishes, in descending order of consequence:

1. **The issue's headline blocker is stale, and closing it created a new one.**
   The issue states the canonical default is "verified contested" with three
   open proposals and nothing able to start. The operator locked D4 on #5418
   on 2026-09-18: the default is **`us.anthropic.claude-sonnet-4-6`**. But the
   ruled identifier's *exact* form is not among the values this platform has
   proven invocable *by a recorded result*. Both verified alias maps carry the
   **`global.`** form (`model_validate.py:31`, `model_resolver.py:28`); the exact
   ruled literal has one runtime site (`gateway-main.tf:477`) and one documented
   verify procedure (`deploy-quickstart.md:787`), the rest being pricing/fixture
   artifacts. Under D4's own rule — "prove it with a bounded real invocation …
   listing alone is insufficient" — AC-02 cannot pass on inherited evidence.
   **A prefix-specific invocation probe is a prerequisite, not a copy-paste** —
   but the command to run already exists in the deploy guide, so the cost is
   running it in the target account and recording the request ID. §2.
   **R2 now formalises this:** the identifier is a Claude-class *candidate*, and
   PMM-09 is named as the story whose recorded invocation promotes it. §12.

2. **AC-09 as written cannot pass, and the reason is structural.** The story
   requires rollback "without a redeploy". The platform's own precedent for
   exactly this kind of flip — `credential-binding-flip.yml:145-186` — sets the
   SSM parameter *and then triggers `gateway-deploy.yml`*, because the gateway
   reads config from pod environment via `Settings()` at construction
   (`config.py:286-290`) after `gateway-deploy.yml:398` bakes the SSM value
   into the ConfigMap. Its own summary says so: "the pod will pick up … on next
   ConfigMap render." Either AC-09's wording changes to "without rebuilding an
   image", or PMM-07 must read the posture at request time. This is a decision,
   not a defect. §6.

3. **The consolidation inventory in the issue covers about a fifth of the
   real surface.** The issue's nine groups are all real and every literal is
   exact — but they are 28 occurrences across 20 files, against **159 live-path
   occurrences across 64 files** and **17 distinct default literals**, not
   nine. Two of its globs are also wrong in ways that would leave drift behind:
   two of the nine "`agent-*.yml`" workflows are not named `agent-*.yml`
   (`skill-agent.yml:194`, `malware-analysis-agent.yml:209`), and "seven
   `components/*.ts`" is 7 occurrences across 6 files, only 4 of those files
   being under `components/`. §3.

4. **Scope must be bounded explicitly, because "one identifier everywhere" is
   the wrong target.** Of the 64 files, a large block is `agent-context`
   (LiteLLM-prefixed `bedrock/…` values), plus classifier/summarizer/probe
   models that are deliberately cheaper models for non-persona work. Forcing
   those onto the persona default would raise cost and change unrelated
   behaviour. The story needs a **persona-execution boundary**, not a
   repo-wide sweep. §3.3.

5. **Most of the matrix's substrate does not exist yet, and one gap changes an
   acceptance criterion's meaning.** No persona→model resolver, no report-only
   posture flag for model resolution, no drift test, no `resolution_source` on
   the usage record, no `adp models` CLI command (`cli/adp:986-1028`), and no
   signed model-policy snapshot (§7.6). Two consequences beyond "wait for the
   predecessors": AC-07's "model that is not usable" has **no allowlist to
   violate** today — the persona allowlist parameter is never passed at its only
   call site (`github/handler.py:1777` vs `model_validate.py:49`) and both seeds are
   `["*"]` — so AC-07 must state which *kind* of unusability it exercised
   (§7.4), now split into five distinct cells L12-L16 (§7.2.3). And AC-06's
   snapshot-revision assertion cannot borrow the existing pricing snapshot,
   which is explicitly "NOT authentication"
   (`pricing_policy/policy.py:1050`); R4 specifies the replacement, and it
   changes the assertion to a digest identical across hops rather than a reused
   signed token (§7.4, §12).

6. **The epic's headline example names a model the platform excludes by
   design.** `model_validate.py:34-38` deliberately omits `claude-fable-5` as
   "listed ACTIVE but do NOT invoke for us" (non-default data-retention mode).
   Fable 5.1 exists only in pricing data (`047_claude_pricing_v2.py:1927+`) —
   pricing rows are not invocability evidence. The epic's example table also
   names a "Reviewer/testing persona"; there are **12 personas and no testing
   persona** (`personas.py:10-64`). §8.

**What is genuinely reusable and should not be reinvented:** the shadow→enforce
rollout shape is already institutional here. `bedrock_routing` shipped
shadow-then-enforce across #4743/#4744 with an SSM-backed flag
(`config.py:241`, `bedrock_routing.py:292-296`), and #3186 established the
**gated flip** pattern — a `workflow_dispatch` gate that re-asserts every
precondition itself, so a premature dispatch is safe because it fails the gate
and flips nothing. PMM-09 should copy that, not invent a new ceremony. §5, §6.

---

## 1. Scope and dependency reality

### 1.1 What this story does and does not build

In scope: choosing and applying one canonical default across the
persona-execution surface; the deploy-gate and preflight alignment; the
report-only vs enforcing comparison; the live matrix; the flip; a demonstrated
rollback; the correction to the epic's example table.

Out of scope, and this matters for reading §7: **every resolution, storage, UI
and CLI mechanism**. PMM-09 changes configuration and defaults, then runs and
records. If a live cell cannot run because an earlier story has not shipped,
that cell is **blocked**, not inferred from a unit test.

### 1.2 The dependency contract is not yet satisfiable

All eight predecessor stories are OPEN (`gh issue view`, verified at the
reviewed revision), and PMM-01's design note is **not on the default branch** —
`docs/design-notes/` contains no `5417-*` file, and no merged PR corresponds to
it. So PMM-09 cannot start AC-03 onward today regardless of the operator
decisions in §10.

This is not a criticism of the story; it is a sequencing fact that the
completion report must state rather than discover. The consolidation work
(AC-01, AC-02) is the exception: it depends on the D4 ruling and the probe in
§2, **not** on the resolver shipping. That is the parallelism opportunity in
§9.

---

## 2. 🔴 The consolidation target — D4 is ruled, but its exact identifier is unproven

### 2.1 The ruling

D4, locked by the epic operator on #5418 on 2026-09-18:

> **The canonical system default is `us.anthropic.claude-sonnet-4-6`.**
> … The versioned `us.` inference profile is preferred over `global.` for
> account portability. Deployment/bootstrap must enable it and prove it with a
> bounded real invocation using the actual runtime request shape; model/profile
> listing alone is insufficient.

The issue's prerequisite table calls D4 "verified contested … nothing else in
this story can start". **That premise is superseded.** The design records D4 as
settled. Consequently the issue's framing of #4673, #2684 and #1128 as three
competing proposals awaiting a ruling is also superseded — D4 disposes of all
three explicitly (§2.4).

### 2.2 The new blocker the ruling creates

The `us.` preference is well-founded — but it points at a string the platform
has not proven.

| Evidence | What it says |
|---|---|
| `model_validate.py:31` | `"sonnet46": "global.anthropic.claude-sonnet-4-6"` |
| `model_validate.py:21-25` | aliases are "verified ACTIVE via `list-inference-profiles` **AND** verified to invoke via `bedrock-runtime invoke-model` (issue #2300)" |
| `model_resolver.py:28` | same `global.` mapping in the gateway |
| `gateway-main.tf:477` | `ANTHROPIC_MODEL = "us.anthropic.claude-sonnet-4-6"` — the only *runtime* site on the exact ruled literal |
| `deploy-quickstart.md:787` | the documented post-enablement **verify** step already invokes the exact ruled literal, under the heading "source of truth is `invoke`, not agreement status" |

The exact literal `us.anthropic.claude-sonnet-4-6` appears in 4 files at this
revision: the runtime site above, the deploy-guide verify step, and two
pricing/fixture artifacts (`snapshots/2026-09-12.2.json`,
`fixtures/aws/claude/model-card-anthropic-claude-sonnet-4-6.md`) that are not
invocability evidence. Related-but-distinct suffixed forms (e.g.
`…-4-6-v1` at `test_cache_token_pricing.py:108`) are **different identifiers**
and do not bear on the ruled string.

So the *code-comment* invocation evidence trail (#2300's standard, recorded in
the alias maps) covers `global.…sonnet-4-6`, while the ruled default is
`us.…sonnet-4-6`. **Different inference profile ARNs, different account
entitlements.** The whole point of #2300 — which #5427 itself cites as its
invocability discipline — is that a plausible-looking identifier is not an
invocable one.

Three independent sources argue the `us.` form *does* work, which is why this is
a probe and not a redesign:

- `gateway-main.tf:465-477` states the `us.` profile "is available in every
  account we deploy to, whereas `global.` is not enabled on all accounts (e.g.
  test account … returns 'invalid model identifier' for `global.*`)" and names
  three accounts where `us.` works.
- `deploy-quickstart.md:784-793` documents a real `bedrock-runtime invoke-model`
  call on the exact ruled literal as the deploy-time verification step, and
  states that a real JSON message means access is live. This is the closest
  thing the repo has to an invocation procedure for the ruled string — it is a
  *documented procedure*, not a recorded result, so it lowers the probe's cost
  (the command already exists) without discharging it.
- #4673 independently recommends preferring `us.` over `global.` for the same
  reason.

Note these two sources **contradict** the alias maps' implicit assumption that
`global.` is the safe form. That contradiction is itself a finding: the
platform currently holds two opposing beliefs about which prefix is portable,
in code comments, both asserted confidently. PMM-09 is the right place to
settle it with evidence.

**Required before AC-01:** a bounded real invocation of the exact literal
`us.anthropic.claude-sonnet-4-6`, in the named target account and region, using
the runtime request shape (the Claude-harness path, per D6 — not a bare
`invoke-model` smoke that skips the harness), recording the request ID. If it
fails, D4 needs re-ruling to the `global.` form or to an enabled alternative;
that is an operator decision, not a developer workaround.

### 2.3 The deploy gate must move with the default — and both lists are wrong for D4

`enable-bedrock-models.sh:38-42` and `preflight-check.sh:224` both require
exactly `anthropic.claude-opus-4-6-v1` and `anthropic.claude-sonnet-4-6`.

The enablement script normalizes prefixes away (`normalize()` strips
`global.` / `us.` / `eu.` / `apac.`, lines 53-58), so for the *marketplace
agreement* step the D4 default is already covered by the bare
`anthropic.claude-sonnet-4-6` entry. **This is a trap.** A passing
`enable-bedrock-models.sh` proves an agreement exists for the model *family*;
it does not prove the `us.` **inference profile** is invocable, because the
prefix was discarded before the check. AC-02 must therefore assert the profile,
not just the agreement — otherwise it is #2300 repeating itself inside the very
criterion meant to prevent it.

`enable-bedrock-models.sh:36-39` also carries a stale sync comment naming
`entrypoint.py` and `chat-scaledjob.yaml` as the things to keep in step. After
consolidation that comment must name the single canonical constant instead
(§3.4), or the next drift is invisible again.

### 2.4 Disposition of the three competing issues — record, do not re-litigate

D4 disposes of all three. The completion report must state this explicitly:

| Issue | State | D4's disposition |
|---|---|---|
| #4673 — worker default `global.…opus-5` hangs silently; prefer an available `us.` profile + fail loud | OPEN | **Resolved in direction.** D4 adopts its recommendation. Its fail-loud startup probe is complementary and still wanted. |
| #2684 — agents hardcoded to Opus 4.6, switch to Sonnet 4.6 (~6x faster) | OPEN | **Resolved.** D4 cites its performance evidence as support. |
| #1128 — verify ScaledJob uses `us.…opus-4-8` | OPEN | **Explicitly not the default.** D4 records its smoke failed on incompatible thinking parameters and was reverted. |

---

## 3. The consolidation surface — corrected, and bounded

### 3.1 The issue's inventory is accurate but partial

Every one of the nine claimed groups exists at (or within one line of) the
claimed location, and every claimed literal is exact. Verified individually.
Two descriptions are wrong:

- **"Nine `.github/workflows/agent-*.yml`"** — nine workflow files carry the
  literal, but `skill-agent.yml:194` and `malware-analysis-agent.yml:209` do
  not match `agent-*.yml`. A change scoped by that glob leaves two live
  dispatch paths on the old default. `malware-analysis-agent.yml:209` is also
  a conditional (`${{ vars.MALWARE_ANALYSIS_AGENT_MODEL || '…' }}`), so it
  needs different handling from a bare literal.
- **"Seven `components/*.ts` and helper sites"** — 7 occurrences across 6
  files, of which only **4 of the 6 files** (5 of the 7 occurrences) are under
  `components/`: `CodeGenerationAgent.ts:33`, `FixOrchestrator.ts:179` and
  `:224`, `MCPOnboardPlanningAgent.ts:37`, `PlanningAgent.ts:47`; the remaining
  two are `mcp-onboard.ts:73` and `skill-agent.ts:22`. All seven carry the same
  prefixless literal `claude-sonnet-4-5-20250929` as an `ANTHROPIC_MODEL`
  env fallback.
- **"Nine distinct hard-coded defaults"** miscounts in both directions: the
  nine groups contain **5 distinct literals**; the repo holds **17** distinct
  live-path default literals.

### 3.2 Sites the inventory misses

Live-path default sites absent from the issue's list include, non-exhaustively:

| file:line | literal | why it matters |
|---|---|---|
| `chat-scaledjob.yaml:42` | `global.…sonnet-4-6` | `LCM_SUMMARY_MODEL`, the direct sibling of the claimed line 41 |
| `lcm/config.ts:34`, `bedrock-summarizer.ts:27` | `global.…sonnet-4-6` | summarizer defaults behind the same ConfigMap |
| `gateway-main.tf:477` | `us.…sonnet-4-6` | the ScaledJob's real pinned default |
| `.github/scripts/author_grouping_plan.py:145` | `us.anthropic.claude-opus-5` | an 18th variant, in the triage path |
| `agent_onboarding_schemas.py:113` | `global.…sonnet-4-20250514` | a Pydantic default described as "Default model for the agent" |
| `routing_probe.py:105` | `us.…sonnet-4-5-20250929-v1:0` | `_PROBE_MODEL_ID` |
| `agent-template.yml:95`, `workflow-example.yml:58`, `full-onboard-repo.sh:121` | `us.…sonnet-4-20250514-v1:0` | **propagate into onboarded customer repos** — drift escapes the repo |

The last row is the one with lasting consequence: a default left in an
onboarding template is copied into every repo onboarded afterwards, where this
epic's resolver cannot reach it.

### 3.3 🟠 Decision needed: the consolidation boundary

"One identifier everywhere in scope" (AC-01) needs "in scope" defined, because
a literal reading is harmful. The 64 files include three categories that should
**not** become the persona default:

1. **Deliberately cheaper non-persona models** — `classifier.py:23` and
   `lambda-gateway/variables.tf:127` (Haiku for classification),
   `routing_probe.py:105` (a probe), the LCM summarizer sites. Forcing these
   to the persona default raises cost for work that never wanted Opus-class
   capability. The issue's own cost-direction requirement argues against it.
2. **`agent-context` (≈25 sites)** — LiteLLM-prefixed `bedrock/global.…`
   values across `config.env`, manifests, `generator.json`, ingestion configs.
   Different module, different harness, not persona execution. Out of scope.
3. **Allowlists, alias maps, pricing data and test fixtures** — the alias maps
   (`model_validate.py:19-38`, `model_resolver.py:17-81`) and the separate
   allow-pattern lists (`model_validate.py:40-46`,
   `model_resolver.py:85-100`), plus `047_claude_pricing_v2.py`, the frozen rate
   tables, and the Bedrock model-name wildcard in `eks/main.tf:251`'s
   `BedrockInvokeModels` statement. These are *not* defaults.
   Rewriting them would break the alias surface and the pricing snapshots.
   Roughly 3,700 occurrences live in gateway fixtures/migrations/snapshots
   (2,299 in the packaged pricing snapshots alone) and must be left alone; the
   figure is a scoped count, not a certified census.

**Recommended boundary — for operator confirmation:** scope AC-01 to *the
default model for a persona-executing agent hop*. That is the worker entrypoint
and its TS equivalents (`entrypoint.py:1578`, `ConfigLoader.ts:19`,
`agent-worker.ts:127`), the persona dispatch workflows (all 9 files, glob
corrected), the ScaledJob/chat ConfigMap persona values
(`chat-scaledjob.yaml:41`, `gateway-main.tf:477`), the persona-component
defaults (the 7 `components`/helper sites, the `agent-pm.ts` /
`agent-superpower.ts` / 4 × `monitoring.ts` sites), and the onboarding
templates. Everything else is explicitly listed as out of scope **in the PR**,
with its reason — so the exclusion is a recorded decision rather than an
oversight.

### 3.4 Make the inventory test the enforcement, not the prose

AC-01 leans on "the PMM-07 inventory test". **No such test exists** at this
revision — no drift/inventory test over model defaults is present. Whether it
lands in PMM-07 or here, the design requirement is the same and is worth
stating because it is what prevents regression:

- a single canonical constant per runtime (one Python, one TypeScript, one
  ConfigMap/Terraform value), with every other site referencing it rather than
  repeating a literal;
- a test that **fails on any new hard-coded persona-execution default**, with
  the in-scope surface expressed as an allow-list of known sites so a new file
  cannot silently add a 19th variant;
- `enable-bedrock-models.sh`'s sync comment (lines 36-39) repointed at that
  constant.

Without the enforcement half, consolidation decays — which is precisely how
nine variants accumulated.

### 3.5 A live pinned GPT default exists — as a *delegated-tool* default, not a second class's persona-execution default

S7 rules that defaults are per harness compatibility class, and cites #5433
(OPEN) for a separately proven Codex/GPT default. There is a real finding here,
but it must be stated precisely, because **the stronger version of it was
withdrawn by PMM-01 at its current head.** PMM-01 rev-5
(`5417-per-invoker-persona-model-mapping.md:8`, with the reasoning at `:759-763`
and the withdrawal recorded at `:1195`) retracts rev-4's claim that a second
compatibility class was "already live in the tree": `openai.gpt-5.6-sol`
configures the **Codex CLI as a delegated tool**, the agent executing the
`codex` persona is the same Claude Agent SDK worker as every other persona, and
`codex-sdk` is **a reserved class ID with no persona mapped to it** (`:24`,
`:159`). This note previously asserted the withdrawn version — that "the Codex
class already has a live, pinned, test-guarded default" — and that assertion is
**corrected here**, in favour of the narrower claim the evidence actually
supports.

The accurate finding: **a live, pinned, test-guarded GPT model literal exists in
this repository, governing a delegated tool rather than a persona's execution
harness**, and this note's §3.1-§3.2 inventory missed it entirely because both
sweeps looked only for Claude-shaped literals. The inventory gap is real
regardless of which class the literal belongs to.

| Evidence | What it says |
|---|---|
| `codex-config.toml:25` | `model = "openai.gpt-5.6-sol"` — baked into the agent-worker image at `$HOME/.codex/config.toml` |
| `test_codex_config.py:30-34` | asserts that exact literal — **the delegated-tool pin has the kind of pin test §3.4 wants for the Claude default, which has none** |
| `model_resolver.py:99` | `"openai.*"` in `DEFAULT_ALLOWED_PATTERNS`, so the gateway admits the model on the metered path |
| `personas.py:57` | `"@agent-codex": "codex"` — a persona that *delegates to* Codex. Per PMM-01 `:759`, it still **executes on `claude-agent-sdk`**, so this is not a `codex-sdk` persona mapping |

Three consequences for AC-01, none of them cosmetic.

**First, the consolidation surface is wider than the Claude sweep.** AC-01 as
restated by S7 ("one identifier per harness class") cannot be satisfied by
sweeping Claude literals alone: GPT literals exist, are pinned, and drift
independently. This is *not* a second class needing its own class-keyed default
— PMM-01 `:24` rules `codex-sdk` has no persona mapped to it, so per S7 there is
exactly **one** class with a persona-execution default to consolidate today. It
is a second *literal family* needing its own inventory pass, and this note does
not contain one — §3.1's 159/64 figures are Claude-class counts. Whoever
implements AC-01 must not read those totals as the whole job.

**Second, there is already GPT-literal drift of exactly the kind AC-01 exists to
remove — and it is in documentation rather than code, which is why no test
caught it.** Two live sites still describe the Codex default as the *previous*
model: `modules/agent-factory/skills/codex-bridge/SKILL.md:16` ("Codex runs
headless in this pod against Bedrock (`openai.gpt-5.5`)") and
`agent-worker-image/Dockerfile:120` (same claim in a build comment), while the
config they describe pins `openai.gpt-5.6-sol` (`codex-config.toml:25`). The
default moved in #3907 (MERGED) and the prose did not follow.

That `SKILL.md` is the canonical source tree copy, and `stage-personas.sh:43`
stages `agent-factory/skills/` into the worker image's skill root, so the stale
sentence is what the agent actually reads at runtime — a stale instruction to a
model, not merely a stale build comment. (Verification note for a future
reader: the staged copy also appears at `.claude/skills/codex-bridge/SKILL.md`
inside a running worker, byte-identical and at the same line; cite the
`modules/` path, since the staged tree is not in version control.)

This is in scope for AC-01 under S7 and out of scope for D4, which governs only
the Claude class. It is also cheap: two comment lines, no behaviour change.

**Third, the GPT pin sets the evidence precedent S1 demands, which helps the
Claude side.** That default's own flip was smoke-verified through the gateway
before being pinned — `codex-config.toml:21-24` cites #3904 and the config test
cites #3907/#3908 (#3904 and #3908 both CLOSED, #3907 MERGED). That is the shape
S1 now requires for `us.…sonnet-4-6`: a recorded real invocation, not a listing.
The Claude class has no equivalent pinned-and-proven artifact, which is the
asymmetry §2.2 is really describing. Note the precedent transfers as *method*
only — a delegated tool's smoke test is not proof of the Claude harness request
shape, which is exactly why R2 leaves the Claude default a **candidate** and
names L24 as its promotion.

**Boundary, to avoid over-reading this.** D6 and PMM-01 `:759-763` agree that
`@agent-codex`'s outer loop is the Claude Agent SDK and Codex is a bounded
delegated tool, so `openai.gpt-5.6-sol` is today a *tool* default, not a
persona-execution default. #5433's native `gpt-*` personas are what would make it
one, and that epic is OPEN. So the recommendation is **not** to pull #5433's work
into PMM-09, and it is **not** to have AC-01 name a second class's default: per
R2 that would assert a persona-execution default for a harness on which nothing
has executed a persona — the precise error PMM-01 rev-5 withdrew. It is narrower:
AC-01 must (a) record that the Claude class is the **only** class with a
persona-execution default to consolidate today, while a separate pinned GPT
**tool** literal exists and is owned elsewhere, (b) note the two stale-prose
sites, and (c) keep the canonical-constant design of §3.4 keyed by class, with
**one** key populated and `codex-sdk` reserved-and-empty. Ownership of (b) is
settled by R2 — filed against #5433, not PMM-09 — so decision 6 is withdrawn
(§12.1).

---

## 4. Cost consequence of the default change (AC-01's second half)

AC-01 requires the PR to name each path whose effective default changed and the
expected cost direction. Reading the current literals against D4:

| Current default | Sites | Direction under D4 (→ Sonnet 4.6) |
|---|---|---|
| `global.anthropic.claude-opus-5` | worker entrypoint, `ConfigLoader.ts`, `agent-worker.ts` | **Cheaper per token**, and a capability reduction for unconfigured persona runs |
| `global.anthropic.claude-opus-4-6-v1` | 9 dispatch workflows | **Cheaper per token**, capability reduction |
| `global.anthropic.claude-sonnet-4-6` | chat ConfigMap, `sqs_consumer.py`, `run-query.ts` | **Neutral in family**; prefix change only |
| `us.anthropic.claude-sonnet-4-20250514-v1:0` | `agent-pm.ts`, `agent-superpower.ts`, 4 × `monitoring.ts` | Legacy → current; these are the models `model_validate.py:34-37` records as **access-denied after 30d unused**, so these paths may be failing today |
| bare `claude-sonnet-4-5-20250929` | 7 sites | Prefixless → profile-qualified; **fixes a likely-broken identifier** |

Two notes the completion report should carry. First, the dominant direction is
*cheaper*, so the tenant-forewarning risk in the issue's impact table is
smaller than feared — but the **capability reduction** for unconfigured runs is
the real user-visible change, and it is not a cost story. Anyone relying on the
worker's Opus-5 default silently gets Sonnet 4.6 unless they set a mapping.
That deserves an announcement, which is an operator action.

Second, two of the rows above suggest paths that may be **broken right now**
(legacy access-denied identifiers, prefixless IDs). Consolidation would fix
them incidentally. The PR should say so rather than claim a pure no-op, and
#2301 (agent reports success when all invocations fail — still OPEN) explains
why such breakage could have gone unnoticed.

---

## 5. The shadow comparison as a real gate (AC-03)

### 5.1 Reuse the established shape

`bedrock_routing` already did shadow→enforce: `config.py:241`
(`bedrock_routing_shadow_mode: bool = True`), `bedrock_routing.py:292-296`
(returns `None` without touching the database when shadow is off),
`routes.py:6` (R2 #4743 read the ladder in shadow mode; R3 #4744 enforced).
`config.py:275-285` even shows the disciplined way to retire a bypass flag
afterwards. PMM-09's posture flag should mirror this, including the
default-to-safe posture.

### 5.2 What AC-03 must specify to be a gate rather than a note

"Every divergence is explained" is not yet checkable. The design requires:

- **Comparison subject:** for each dispatch, the legacy effective model (the
  hard-coded default or `/model` result that governs today) versus the
  resolver's answer, recorded as a pair with the persona, principal kind,
  tenant, dispatch path and policy revision.
- **Per-path breakdown**, because the paths have different legacy defaults
  (§4) and an aggregate divergence rate would hide a path that is 100% wrong:
  GitHub mention, GitLab, label dispatch, chat/UI, CLI, ARC workflow,
  orchestration loop, agent-to-agent, scheduled service-account.
- **An expected-divergence baseline.** This is the part that makes the gate
  meaningful: after consolidation, an *unconfigured* principal should diverge
  **zero** times (both sides are the canonical default), and a *configured*
  principal should diverge **exactly** when a mapping exists. Any other
  pattern is unexplained. Stating it this way turns AC-03 from a judgement
  call into an assertion.
- **Window and coverage:** a duration alone is insufficient — a quiet week
  proves nothing. Require a minimum observation count **per path**, and record
  paths with zero traffic as *uncovered* rather than passing. #3186's
  precondition list (7-day soak **and** 100% registry-row coverage **and**
  green adversarial E2E) is the model here.
- **Failure rule:** an unexplained divergence, or an uncovered in-scope path,
  blocks the flip. No override without a recorded operator decision.

### 5.3 A selection difference and an admission refusal are not the same event

The third-pass review requires this separation, and it prevents a specific
misreading that would be dangerous in production. Both things can be described
loosely as "the resolver disagreed with what happens today", so a single
"divergence" bucket invites an implementer to treat a *refusal* as one more
divergence that report-only mode is entitled to suppress. It is not.

| | **Selection difference** | **Admission refusal** |
|---|---|---|
| What it is | The resolver would choose a different model than today's behaviour | A gate refuses to admit the work at all |
| Governed by the posture? | **Yes.** In report-only the difference is recorded and the legacy choice still governs | **No.** Report-only **never** relaxes it |
| Where it shows up | The §5.2 comparison ledger | A refusal reason code returned to the requester (§7.2.3) |
| In the matrix | Feeds L22's clean-comparison precondition | Kind-B cells L12-L16, plus L7's cross-tenant denial |

PMM-07's current head states the rule in one line that this note adopts verbatim
in effect: "**The posture governs selection only.** It never relaxes an
admission gate" (`5425-persona-model-resolver-wiring.md:1067`).

Two consequences for this story. **The comparison ledger must not count refusals
as divergences** — mixing them corrupts the expected-divergence baseline above,
because a refusal is not a case where "both sides chose a model and disagreed".
And **L7 and L12-L16 must refuse identically in report-only and enforcing
posture.** The only cell where posture legitimately changes the outcome is L23,
and what changes there is whether a *selection* difference becomes binding — not
whether a tenancy or catalogue gate holds. A cross-tenant write that succeeded
because "we are only in report-only" would be a tenant-isolation defect, not a
recorded divergence.

---

## 6. 🔴 The flip and the rollback — AC-09 is not achievable as written

### 6.1 The evidence

The story requires: "Return the posture flag to report-only, which restores the
previous behaviour **without redeploying**. Demonstrate this, do not assert
it." The platform's directly analogous flip says otherwise:

- `credential-binding-flip.yml:145-160` sets the SSM parameter;
- **line 162-171 then triggers `gateway-deploy.yml`**, echoing "the
  pod will pick up `ENFORCE_CREDENTIAL_BINDING=true` on next ConfigMap render";
- line 186 documents the rollback as: set SSM false, **"then trigger
  gateway-deploy"**;
- the reason is `gateway-deploy.yml:340` reading SSM into a shell variable and
  `:398` `sed`-substituting it into `configmap.yaml:288`'s
  `__BEDROCK_ROUTING_SHADOW_MODE__` placeholder at deploy time (note
  `platform/scripts/deploy-all.sh:1060` carries the identical substitution, so
  the workflow is not the only renderer);
- and `config.py:286-290` — `Settings()` with `env_prefix: "BG_"` — is
  constructed from the **pod environment**. Note the precise mechanism:
  `get_settings()` is *not* memoized, so each call does re-read `os.environ`.
  The blocker is therefore not an in-process cache but that a ConfigMap-sourced
  env var is fixed for the container's lifetime — so changing it requires
  re-rendering the ConfigMap and recycling pods, which is what
  `config.py:239-240`'s own comment means by "a pod recycle is enough; no
  rebuild".

So for the gateway, changing a posture flag today means re-rendering the
ConfigMap and restarting pods. That is a redeploy of configuration (no image
rebuild), but it is not "without redeploying", and it is not instant.

The webhook-ingress side differs: its flags are Terraform-managed Lambda
environment variables (`infra/lambdas.tf:92-100`, e.g.
`AGENT_AUTHORITY_ENABLED = tostring(var.agent_authority_enabled)`), so a flip
there is a `terraform apply` — again config-only, again not zero-touch.

### 6.2 The ruled mechanism — runtime posture, read per request, fail closed

**This is settled, not a menu.** An earlier revision of this section offered
three options and recommended the cheapest (reword AC-09 to admit a
configuration re-render). The operator ruled against that in S2, and the
third-pass review requires the superseded alternatives be **removed** rather
than retained alongside the ruling, because a live menu lets an implementer
pick the rejected option. The rejected alternatives are recorded in one line
for history only: rewording AC-09 to "no image rebuild", and a two-tier
pre-staged emergency kill. **Neither is the design.**

The design is S2's, refined by the binding ruling and by PMM-07's current head:

1. **The posture is a versioned record, not an environment variable.** PMM-02
   owns it as the `enforcement_posture` field on the class-keyed platform record
   (`5419-persona-model-preference-schema-and-api.md:1152`), keyed by harness
   compatibility class (`:1148`) with a monotonic `revision` (`:1151`) and a
   `UNIQUE` constraint giving one authoritative record per class (`:1155`).
   Writes are platform-admin only and fully audited (`:1158`).
2. **The gateway reads it at request time behind a bounded cache.** This is
   **PMM-07's** to build, and its current head claims the gap explicitly:
   §4.7 is titled "the versioned runtime posture, read at request time, bounded
   cache, fail closed" (`5425-persona-model-resolver-wiring.md:1019`) and states
   that PMM-02 carries the column while "**no note describes a request-time read
   path** — that gap is PMM-07's to close"
   (`5425-persona-model-resolver-wiring.md:1022-1026`).
3. **An unknown or unparseable revision refuses.** Not "assume report-only" —
   PMM-07 `:1060` gives the reason this matters: assuming report-only "would
   silently disable an *enforcing* deployment, which is the failure mode where a
   resolver defect becomes a spend incident with no signal." This closes the
   cold-start fail-open gap an earlier revision of this note had only flagged
   as a caveat.
4. **A stale posture beyond the cache bound with an unreachable authority is a
   refusal, never an extension** (PMM-07 `:1063-1064`).
5. **A revision that changes mid-decision invalidates the decision** — the
   resolver must not select under revision *N* and admit under *N+1*
   (PMM-07 `:1065-1066`).

**Two consequences for PMM-09, which are this story's whole involvement.**
PMM-09 *demonstrates* this mechanism (cell L24); it does not build it. And
because the read is cached with a bound, recovery is **not instantaneous** — it
is bounded by that cache lifetime. So AC-09's demonstration must record a
**measured** recovery time from runs before and after, not assert immediacy.

**One cross-story seam the operator should know about, because it is a real
gap rather than a division of labour.** No single note covers both halves:
PMM-02's note defines only the `enforcement_posture` column and contains no
cache, request-time or fail-closed language anywhere in the file; PMM-07's note
supplies exactly those and says in terms that no note described the read path
before it. The halves are consistent, but the contract between them — the read
interface, and the cache bound as a named constant — is asserted in one note
and not the other. **PMM-07's head does not fix a numeric TTL for the posture
cache**; it requires the bound be "explicit, monotonic, and short" following
`V2RateCache` (`:1063-1064`). Since L24's measured recovery time is bounded by
that number, PMM-09 cannot state its own rollback target until PMM-07 names it.
Note this is *not* the 30-second figure elsewhere in that note, which is the
D5 snapshot assertion's maximum TTL and a different mechanism.

### 6.3 Flip mechanism — reuse #3186's gated dispatch

Do not hand-edit the flag. Copy `credential-binding-flip.yml`'s three-job
shape: assert preconditions → flip → summary, with `dry_run` support, and a
hard-fail if the gate did not pass. The property worth preserving is #3186's:
*a premature dispatch is safe, because the gate re-asserts its own
preconditions and flips nothing if they fail.* PMM-09's gate should assert:
AC-01/AC-02 green at the flip revision, the AC-03 comparison clean with
per-path coverage met, the three deterministic suites green, and all three
artifacts (worker image, webhook-ingress, gateway) carrying the resolver.

The flip is an **authorization boundary**: merging PMM-09's code flips nothing;
the posture change is an explicit operator dispatch recorded on #5427.

### 6.4 Ordering, and the mixed-version requirement

Three artifacts must all carry the resolver before the flip, and they deploy by
different mechanisms (per `CLAUDE.md`): webhook-ingress via
`deploy-webhook-ingress.sh` (not covered by `deploy-all.sh`), the worker image
via `agent-worker-image.yml`, the gateway via `gateway-deploy.yml` with
`gateway-infra-apply.yml` manual by design. The SPA build needs
`VITE_API_URL="/api"` and a CloudFront invalidation.

The story asks to "confirm no mixed-version node remains on the legacy path at
flip time". The concrete check: KEDA ScaledJob pods are per-run, so a
mid-flight run finishes on the image it started with — the requirement is that
no *queued* message be consumed by an old-image pod after the flip. Record the
image digest in use and confirm the ScaledJob's pod template references the new
digest before flipping, rather than asserting it.

### 6.5 D5 / #3186 — is the flip blocked on it?

D5 (locked, #5418) states snapshot enforcement "is gated on the
gateway-mediated delegated-authority path being live and accepted", making
#3186 and #5195 "a prerequisite for enforcement". #3186 is OPEN and marked
**DO NOT TRIGGER YET**, pending a 7-day zero-drift soak.

The issue's own prerequisite table flagged this as unresolved (D5, blocking
AC-06 and AC-09). **It is no longer unresolved, and this section no longer
poses it as an operator call** — the third-pass review requires the settled
answer be stated once rather than contradicting §10's decision 5.

**The ruling: no flip precedes PMM-06 plus #3186, #5195 and #2293.** S4 rules
there is no enforcing flip before PMM-06 authority/bootstrap is live and PMM-07
delivers #2293's requester feedback; the third-pass review restates it in the
same terms. All three of #3186, #5195 and #2293 are **OPEN**, so this is a
sequencing block to state, not a decision to make.

The distinction that earlier made it look decidable is worth keeping as
*reasoning*, because it explains why the conservative answer was the right one:
D5 gates *snapshot-signature enforcement* (PMM-06's chain trust), while PMM-09's
posture flip governs *which model is chosen*. Those are different properties, so
flipping model resolution while snapshot verification stayed report-only was
technically possible. The reason it is nonetheless forbidden: AC-06's multi-hop
cells would then prove per-hop resolution but **not** the trust boundary that
stops a forged root principal, while the completion report would read as though
chain security had been demonstrated. Ordering removes that trap rather than
relying on a caveat in the report.

---

## 7. The live matrix (AC-04..AC-08, AC-11)

### 7.1 The standard, restated as a disqualifier

Per #2300/#2301 and the story's own text: **a cell passes only on a real model
response with a request ID.** A cell marked passed because the catalogue said
the model was invocable is the #2300 defect and must be recorded as failed.
#2301 (OPEN) is why this is not paranoia: an agent run can report success while
every model invocation fails. Therefore a cell's evidence must include the
request ID *and* an assertion that the run produced real model output — not
merely that the run exited zero.

### 7.2 The executable live matrix

**Superseded revision note.** An earlier draft of this section carried a
seven-row summary table. The operator's second-pass review of `b28547c3` ruled
that table **insufficient to satisfy S5**: it collapsed the four
principal/administration combinations into two rows, omitted cross-tenant
denial and UI/CLI parity entirely, and lumped five distinct refusal mechanisms
into one "unusable" cell. The table below replaces it. §11's S5 row no longer
claims the smaller table satisfies S5.

Every cell is **one separately recorded subclaim** with its own pass/fail. A
single verdict for "AC-04" hides two working personas and one broken one.

**Common identity fields, recorded for every cell:** account, region, UTC
timestamp, persona key, principal kind, canonical principal ID, tenant, surface
(UI or CLI), requested model, resolved model, resolution source, and the
policy/posture revision in force.

#### 7.2.0 Evidence is per outcome kind, not one uniform tuple

An earlier revision required one evidence tuple — including a Bedrock request ID
and a spend figure — from **every** cell. The third-pass review rules that
wrong, and it is: the requirement is not merely unachievable for two of the
three kinds, it is **self-contradictory** for one of them. A refusal cell's
entire claim is that no billable work happened. A Bedrock request ID exists only
because a model was invoked. **So a refusal cell carrying a Bedrock request ID
has failed by definition** — the artifact demanded as proof of success is, in
that class, the proof of failure.

Three kinds, each with the evidence its own outcome can actually produce:

| Kind | Cells | Required evidence | What makes it fail |
|---|---|---|---|
| **A — real invocation** | L1-L6, L9-L11 (save + verify legs), L17-L20, L23-L24 | **Bedrock request ID** from provider invocation logging, plus the response's own token usage, plus the `usage_logs` row (`shared/models/usage.py:11`) with `agent_run_id` (`:28`) and `cost_usd` (`:22`) | No request ID; or a request ID with no matching `usage_logs` row; or a zero-exit run with no model output (the #2301 false-green defect, OPEN) |
| **B — refusal, no billable work** | L7, L8, L12-L16 | The **reason code** as a fixed string reaching the requester, **plus positive proof of no billable work**: no `usage_logs` row for the attempt window, and **no** provider invocation-log entry. Absence must be *asserted against a queried surface*, not inferred from silence | Any `usage_logs` row or invocation-log entry for the attempt; **or** a Bedrock request ID being present at all; or the refusal being indistinguishable from an outage (§7.7) |
| **C — configuration change only** | L9-L11 (the persistence leg), L3-L4 administered writes | The **API response identifier and the audit-row ID** — `audit_logs.id` (`admin/models.py:64`) for administrative writes, or `security_audit_logs.id` (`shared/models/audit.py:37`) for security-relevant events — plus the stored record's new `revision` | No audit row; or a saved value not readable back through the other surface (§7.2.2) |

**Cells L9-L11 span kinds B/C and A deliberately** — a parity cell both saves
(kind C evidence) and then resolves (kind A evidence). Recording only one half
would prove the write without the effect, or the effect without provenance.

**Where the request ID actually comes from, and a gap worth flagging.** The
platform records provider request/response logs as an account/region singleton
in shared platform infra — `platform/infra/bedrock-invocation-logging.tf:3`,
delivering to both a CloudWatch log group
(`modules/bedrock-invocation-logging/main.tf:21`, `:204-206`) and S3 with
large-payload overflow (`:199-202`, `:209-212`), both `true` by default
(`platform/infra/variables.tf:177`, `:183`). **That is the source of truth for
kind-A evidence, and it is also the queried surface that makes kind-B's
"no billable work" a positive assertion rather than an absence claim.**

The gap: **the gateway's own code does not capture a Bedrock request ID on the
success path.** `BedrockInvocationError` accepts a `bedrock_request_id`
(`proxy/exceptions.py:101,109`), but that parameter is **never passed a value
anywhere in the repository** — the only construction sites
(`proxy/service.py:149`, `:1202`, `proxy/bedrock_streaming_response.py:76`) all
omit it. `PricingCapture.response()` receives the botocore response as
`metadata` and reads only `serviceTier` from it
(`proxy/pricing_capture.py:78-86`), never `ResponseMetadata.RequestId`; and
`PricingCapture.request_id` is the **gateway's own** correlation ID, not
Bedrock's — `proxy/routes.py:406` sources it from `request.state.request_id`,
which `admin/middleware.py:84` generates as an inbound header or a fresh
`uuid4()`. The same value is what lands in `usage_logs.request_id`
(`usage/service.py:101`). **Consequence:** a matrix run must obtain kind-A
request IDs from the invocation logs and correlate them to `usage_logs` by
timestamp, model and `agent_run_id` — the gateway's `request_id` column will not
contain one. An implementer who assumes `usage_logs.request_id` is the Bedrock
request ID will produce a matrix that looks complete and proves nothing about
provider invocation. Capturing it in-band would be a small change to
`PricingCapture`, but it is PMM-07/PMM-08 surface, not PMM-09's to make.

Per §7.1 a kind-A cell passes **only** on a real model response with a request
ID. A cell passing because the catalogue said the model was invocable is the
#2300 defect and must be recorded as failed.

#### 7.2.1 Principal and administration cells (AC-04, AC-05)

The four combinations are distinct code paths, not variations of one. Per
ruling 6 the self and administered surfaces are *different routes* with
different authorization models, so a pass on one is not evidence for the other.

| Cell | Caller | Acts on | Surface | What it proves | Route (ruling 6) |
|---|---|---|---|---|---|
| **L1** | human, own JWT | self | UI | 3 personas → 3 distinct models, each actually invoked | `GET/PUT /me/persona-models` |
| **L2** | human, own JWT | self | CLI | same 3 mappings resolve identically from the signed CLI path | `adp models mappings list/set` |
| **L3** | human org-admin | another principal (machine) | UI | administered write commits against a **canonical ID obtained from the server discovery surface**, never a constructed one | `GET/PUT /service-principals/{canonical_id}/persona-models` |
| **L4** | human org-admin | another principal (machine) | CLI | same administered write via `--service-account ID`, tenant-checked | `adp models mappings set --service-account ID` |
| **L5** | service principal, SigV4 | self | M2M | the machine's **own** mappings resolve — never the administering human's | external `/agent/me/persona-models` |
| **L6** | service principal, Cognito client-credentials | self | M2M | a second authentication namespace resolves to the **same** canonical principal as L5, or is refused as unregistered | same handler, canonical resolution |
| **L7** | human org-admin, tenant A | principal in tenant B | UI **and** CLI | **cross-tenant denial.** Refused on both surfaces; the refusal names no attribute of the tenant-B principal | tenant check on the administered route |
| **L8** | human, no managed principals | — | UI | no scope selector renders and only self rows are requested (PMM-04 AC-07) | discovery surface returns empty |
| **L25** | service principal, SigV4, **via the `adp` CLI** | self | CLI | the **command-line tool run as a machine identity** manages its own mappings — the unattended path a scheduled job actually takes | `adp models mappings list/set` over signed `execute-api` → external `/agent/me/persona-models` |

L5 and L6 are separate cells deliberately. Ruling 1 states a Cognito client is
"an org-level approved-client list, not a service-principal identity" and must
be "registered and tenant-bound before service-self preference access", and
PMM-04's current head verifies **three** distinct machine identifier spaces
(`5422-agent-models-ui.md:32`, finding F1: DynamoDB `agent_name`, Postgres
`service_accounts.id`, Cognito `client_id`). A single
"service account" cell would prove one namespace and silently assert three.

L7 is the cell whose absence mattered most: it is the only one that fails
*closed* on a tenancy boundary. The webhook-ingress precedent to mirror is
`identity_resolver.py:530-531`, which returns `cross_tenant_denied` and emits a
CloudWatch metric — the refusal must be observable, not merely returned.

**L25 is new on the third pass, and it is a genuinely distinct path rather than
a fourth spelling of L5.** L2 is a *human* at the CLI; L4 is a *human* at the
CLI administering a machine; L5 is a machine calling the service interface
directly. None of them is the CLI itself running **as** a machine identity,
which is what an unattended scheduled job actually does. It is the combination
most likely to ship broken precisely because each of its two halves is covered
separately.

**Today's `adp` CLI cannot do this, and that is the point of the cell.** The
tool has exactly one authentication verb — browser-approval Cognito sign-in —
and says so in terms: "There is exactly ONE authentication verb"
(`modules/gateway/cli/adp:19-22`), with the full verb list at `:33` and the
help text's sign-in block at `:906-913` covering
`login | status | logout | refresh | import | token` and no signed-request path. A repo-wide search for SigV4 in the CLI directory returns
only the *hosted-agent sidecar* comparison (`cli/bg-gateway-proxy.py:16-19`,
which states the auth material "differs (Cognito JWT here, SigV4 there)") and
`bg-auth.sh`, which the CLI's own README marks "Legacy SigV4 credential
exchange (**deprecated**)" (`cli/README.md:15`). So L25 exercises capability
PMM-05 must first build.

PMM-05's current head does design exactly this, which is why L25 is a live
acceptance cell rather than a change request: it names "a registered service
principal, acting on itself … A SigV4 signature over the request, from the role
it already runs as" as a first-class caller
(`5423-cli-persona-model-commands.md:73`), routes it over the `AWS_IAM`
`/agent/{proxy+}` plane (`:747-749`), and — importantly for this cell's
assertions — **derives the mode from the environment rather than a flag**,
because a flag "invites `--as-service-account`, which is the impersonation
surface §1 exists to close" (`:877-885`). Two things L25 must therefore assert
beyond a successful write: that **no** `--service-principal` flag is accepted on
the signed path (PMM-05 `:920-922` requires exit 1), and that the machine path
never silently falls back to a bearer token when signing fails. A cell that
merely proves "the CLI wrote a mapping" would pass while the impersonation
surface was wide open.

#### 7.2.2 UI/CLI parity (AC-08)

| Cell | What it proves |
|---|---|
| **L9** | A change saved in the UI is reflected by the CLI's `explain` for the same persona, with the **same resolved model and the same resolution source string** |
| **L10** | A change saved by the CLI is reflected in the UI without a manual cache clear |
| **L11** | Both surfaces render the **same refusal reason code** for one refused save (per §7.2.3), not two differently-worded messages |

Parity is asserted on the *resolution source and reason vocabulary*, not only
the model ID. Two surfaces agreeing on the model while disagreeing on why is the
failure this cell exists to catch, and PMM-03's head defines the shared
vocabulary (`5420-persona-and-model-catalogue.md:361`).

#### 7.2.3 Refusal-class cells (AC-07) — five distinct mechanisms

The operator ruled these must be **distinct** cells. They exercise different
code and fail at different layers; one "unusable model" cell proves whichever
happened to be checked first.

**Reason codes are PMM-03's fixed strings, not prose.** An earlier revision of
this table used prose labels ("unknown model", "evidence stale"), which is the
exact defect PMM-03's current head calls out — rev-1 "gave prose labels, and the
consumers each invented spellings — a client cannot branch on prose"
(`5420-persona-and-model-catalogue.md:359`). Its fixed ten-code vocabulary is at
`:361`, delivered in the platform's established `422 {reason, message}` shape
(`:356`, `:209`). This table now uses those exact strings, so a cell's recorded
evidence and the implementation's emitted code are comparable without
translation. Note PMM-03 `:538` records that **four disjoint refusal
vocabularies currently exist across the epic** (its ten, PMM-04's four, PMM-06's
eleven snapshot codes, PMM-07's resolver reasons) and asks the synthesis to
declare one union with owners per prefix — so L11's cross-surface parity
assertion should be re-checked against the published synthesis rather than
against this table alone.

| Cell | Class | Mechanism exercised | Reason code |
|---|---|---|---|
| **L12** | **Invalid / unknown** | identifier absent from the catalogue; today `resolve_and_validate` returns `None` (`model_validate.py:82`) and takes the lenient path | `unknown_model` |
| **L13** | **Org-disallowed** | D1's axis: the principal's *selected* model is refused by **organization** policy. The run must be **blocked naming the selected model**, and must **not** silently run the org-permitted one | `not_permitted` |
| **L14** | **Retired** | catalogue-level `retired` state. Per PMM-03 `:263` a model may be **invocable and retired at once** and must still not be offered — so this cell must not be satisfied by an invocation failure | `retired` |
| **L15** | **Stale evidence** | invocability evidence past `expires_at`. Per PMM-03 `:257` a *validation* call on stale evidence refuses with a distinct code even though a *read* may return stale-marked rows for display; `:367` resolves this as one `Selection \| Rejection` value with two presentations, so the cell must assert the **422 on validation**, not merely a flagged row | `evidence_stale` |
| **L16** | **Harness-incompatible** | D6/ruling 2: a model valid for one compatibility class selected for a persona executing on the other. A Claude model for a `gpt-*` persona must be refused, **not** back-filled | `harness_incompatible` |

L13 and L14 are the two that a naive implementation gets wrong in opposite
directions: L13 by substituting a permitted model (which D1 explicitly forbids),
L14 by proving nothing because the model still invokes fine. L16 is required by
ruling 2's "no cross-class fallback" and could not have been a cell before that
ruling existed.

#### 7.2.4 Chain cells (AC-06)

| Cell | What it proves |
|---|---|
| **L17** | human-rooted chain: each hop resolves by **its own** persona, and the recorded snapshot revision is **identical across hops** |
| **L18** | service-principal-rooted chain: same, with the canonical service principal as policy owner (ruling 1: `service_policy` is owned by the canonical principal; the approving human is audit attribution only) |
| **L19** | **ARC / GitHub Actions root identity** (ruling 5): a human-initiated `issues:labeled`, issue-comment or `workflow_dispatch` run preserves the resolved **canonical human root**; the executing App/bot credential appears as audit attribution only |
| **L20** | scheduled or service-to-service run with **no** authenticated human initiator resolves a tenant-bound registered canonical service principal, and **fails closed if unregistered** |
| **L21** | **direct override is not inherited** (D2): a valid `/model` override on the root hop is audited for that hop **only** and does **not** reach child hops, which resolve from the root principal's snapshot |

L19 and L20 are new cells from ruling 5 and are the ones most likely to be
missed, because both run green today while attributing the root to the wrong
identity — the bot credential that executes the job rather than the human who
initiated it. That is a silent misattribution, not a visible failure.

L21 is the assertion that distinguishes "each hop resolves by its own persona"
from "the root's override leaked downward". They are different claims and the
earlier table contained neither separately.

#### 7.2.5 Flip and rollback (AC-09)

| Cell | What it proves |
|---|---|
| **L22** | shadow comparison clean across the per-path baseline before the flip (§5.2) |
| **L23** | enforcing posture active: a previously-recorded L12-L16 refusal now blocks, where in report-only it only recorded |
| **L24** | rollback demonstrated by runs **before and after**, with the **measured** recovery time recorded (bounded by S2's cache TTL, so not instantaneous) |

Personas must come from the real catalogue (`personas.py:10-64`): `developer`,
`pm`, `operations`, `reviewer`, `architect`, `product`,
`malware-analysis-agent`, `pt-superpower`, `superplane-operator`,
`superplane-researcher`, `aidlc`, `codex`. There is no `testing` persona (§8).

**Cell count and cost.** **25 cells** (L1-L25), each bounded to the minimum runs
that produce its own evidence kind (§7.2.0). The kind-B cells — L7, L8 and
L12-L16 — cost **no model invocation by design**: they must fail *before*
billable work, and that is itself the assertion, which is why demanding a
Bedrock request ID from them would be incoherent. That leaves roughly **15**
invoking (kind-A) cells, which is what the spend ceiling in §7.7 must cover.
L25 is new on the third pass and is a kind-A cell, so it invokes.

### 7.3 AC-07's feedback channel — settled, and owned by PMM-07

AC-07 requires an actionable reason **reaching the requester**. Today:

- the `/model` path is lenient — `github/handler.py:1771` and `:1787` log
  "proceeding with default model (lenient)" and run anyway;
- D2 (locked) reverses this to fail-closed, and states "#2293 or its successor
  feedback path is **required before enforcement**";
- **#2293 is OPEN.** The exported model environment variables have no consumer.

**Corrected against PMM-07's current head (§12.2).** An earlier revision of this
section concluded that AC-07's delivery channel "does not exist" and asked the
operator whether to flip with AC-07 blocked. Both are now stale:

- **Ownership is settled, not open.** PMM-07's current head (#5438 `67db0294`,
  §6.2) rules that #2293's actionable requester feedback **ships in PMM-07**,
  superseding its own earlier draft that pushed the channel to PMM-09. So PMM-09
  never has to own it, and the design must not be read as though it might.
- **Ordering is settled too.** S4 rules there is **no enforcing flip** before
  #2293's behaviour is delivered. So AC-07 may **not** be recorded as "blocked,
  flip anyway" — the §7.3 concern that fail-closed without a feedback channel
  trades a silently-wrong model for a *silent failure* is resolved by ordering
  rather than accepted as a risk.

What remains true is the sequencing fact: **#2293 is OPEN**, so AC-07's channel
is designed and assigned but not shipped. That is a dependency to state, not a
decision to make. The half-built wiring PMM-07 inherits is
`entrypoint.py:1685,1687` writing `ADP_MODEL_REQUESTED`/`ADP_MODEL_RESOLVED`
with no consumer anywhere — which is why §7.2.3's refusal cells assert a reason
code **reaching the requester**, not merely being returned internally.

### 7.4 AC-07's "unusable model" needs a defined mechanism, because the allowlist is inert

AC-07 says "configure a mapping to a model that is not usable". There are three
different ways a model can be unusable, and they exercise different code:

1. **Disallowed by policy** — the natural reading. But the persona allowlist is
   **dead in production**: `resolve_and_validate(alias, persona_allowed_models,
   tenant_patterns)` (`model_validate.py:49`) is called at its only site as
   `resolve_and_validate(model_requested)` with no persona and no tenant
   argument (`github/handler.py:1777`), so it always falls through to
   `DEFAULT_ALLOWED_PATTERNS`. Both seeds set `allowed_models = ["*"]`
   (`agent-registry-seed.tf:44`, `agent-authority-boundary.tf:216`). The
   gateway's equivalent accepts an `org_id` and explicitly ignores it
   (`model_resolver.py:141-142`, "future enhancement"). D3 (locked) is what
   makes these real; until PMM-03/PMM-07 land it, there is no allowlist to
   violate.
2. **Not invocable despite being listed** — the #2300 class, e.g. Fable 5 or a
   legacy access-denied identifier. This is testable today and is the most
   faithful to the story's intent.
3. **Unknown/retired identifier** — `resolve_and_validate` returns `None`
   (`model_validate.py:82`), which today takes the lenient path (§7.3).

The design requires AC-07 to state **which** of the three it exercised. Cases 1
and 3 are the ones the epic's "broken rule fails actionably" decision is about;
case 2 is the one the platform has actually been burned by. Ideally record one
cell each, and mark 1 as blocked if D3's enforcement has not shipped.

A related note for AC-06: **no signed model-policy snapshot exists** at this
revision. The only snapshot mechanism is the *pricing* snapshot
(`pricing_policy/policy.py:620-664`), and it is explicitly not authenticated —
`policy.py:1050`: "`content_sha256` is a corruption diagnostic, NOT
authentication." So AC-06 cannot be satisfied by reusing the pricing snapshot.

**R4 now specifies what replaces it, and it changes the assertion.** The
approved shape (ruling 4, matching PMM-06's C2 at `#5442 2d7cde2f:1324`) is
**not** one long-lived signed snapshot: it is a durable `snapshot_digest`
persisted on a **worker-unwritable** authority record at work-admission, plus a
**fresh 30-second `adpe1` assertion minted per hop**, audience- and
chain-bound, with workers holding no signer secret. Consequence for L17/L18: the
cross-hop assertion must be that the **`snapshot_digest` is identical across
hops**, *not* that one signed token was reused across them — a 30-second
assertion cannot span a chain that outlives it by orders of magnitude, and
expecting it to would be the defect PMM-06's note warns about directly.

### 7.5 🟠 AC-08 and AC-11 depend on surfaces that do not exist

- **AC-08 (CLI):** `modules/gateway/cli/adp:925-975` lists the full command
  surface; there is no `models` verb and no persona-mapping command. AC-08's
  CLI half is entirely PMM-05 (#5423, OPEN). The CLI contract note
  (`docs/design-notes/5180-cli-command-contract.md:74`) defines how a command
  registers, so the shape is settled — but the command is unwritten.
- **AC-11 (cost):** `usage_logs` has **no** `resolution_source`,
  `resolved_model` or `requested_model` column in any migration under
  `alembic/versions/`, and the dispatch envelope carries only
  `model_requested` / `model_resolved` from #2279
  (`spawn_persona.py:591-599`). `bedrock_account_id` exists
  (`037_bedrock_account_routing.py:44,96`) as the routing analogue, which is
  the precedent #5426 should follow. Until #5426 ships, per-cell spend must be
  sourced from Bedrock/CloudWatch usage directly and labelled as such.

  **Strengthened against PMM-08's current head (§12.2).** The
  Bedrock/CloudWatch advice above is right for a stronger reason than the
  missing column. PMM-08's head establishes that **Budget & Spend reads
  `budget_usage`, not `usage_logs`** (`mantle_service.py:809-811`), and that
  `budget_usage` has **no persona, model or run dimension at all** — so a
  per-persona spend figure can never be reconciled against the authoritative
  ledger, only against `usage_logs`. And `usage_logs` is itself unreliable for
  evidence: both writers **swallow exceptions** — the `usage_logs` write is
  wrapped in `except Exception` that only logs a warning
  (`modules/gateway/src/proxy/service.py:649-653`, "Failed to write usage_logs
  row"; `mantle_service.py:761`) — so a row can vanish silently, and a raw-SQL
  post-hoc UPDATE can rewrite `cost_usd` afterwards
  (`budget-usage-tracker/handler.py:314`). Therefore each cell's spend must be
  captured **at run time** from the invocation response plus
  Bedrock/CloudWatch, and recorded as **not reconciled against `budget_usage`**.
  An AC-11 that reports a persona spend total as reconciled would be asserting
  something the ledger cannot support.

Neither is a design flaw in PMM-09; both are forward dependencies the
completion report must distinguish from work this story failed to do.

### 7.6 The predecessor surface, stated plainly

For the completion report's "live cells distinguished from deterministic ones",
here is what exists at this revision versus what the live matrix needs:

| Capability the matrix needs | State at `46355f90` |
|---|---|
| Persona→model preference resolver | **Does not exist.** No `persona_model` / `model_mapping` / `model_preference` symbol anywhere. |
| Report-only posture flag for model resolution | **Does not exist.** Precedent only for *account* routing (`config.py:241`). |
| Model-default inventory/drift test | **Does not exist** (§3.4). |
| `resolution_source` on the usage record | **Does not exist**; routing's analogous `rung` is computed but never persisted. |
| `adp models` CLI | **Does not exist** (§7.5). |
| Signed model-policy snapshot | **Does not exist** (§7.4). |
| Alias + allowlist resolvers | **Exist** (`model_resolver.py`, `model_validate.py`) but carry no persona, no principal and **no system-default concept** — an unknown model passes through unchanged (`model_resolver.py:150-151`). |

The last row is the one to note: the epic's precedence rule "no saved row →
canonical default" has no implementation to attach to, because neither resolver
has a default at all. That is PMM-07's work, but it is why PMM-09's
consolidation (§3.4, a single canonical constant) is a genuine prerequisite
rather than tidying.

### 7.7 Deployment anchoring — the live run is a deployment, not a test run

The operator's second-pass review requires this section: the matrix must be
anchored to `AGENTS.md` and
`docs/adp-platform-deployment/deploy-with-agent.md`, with state and
verification evidence. The earlier revision described the live runs without
tying them to the repository's own deployment procedure, which meant a completed
matrix produced evidence nobody could later audit — no record of which account
it ran against, and no proof the stack was healthy when it ran.

**Why this matters beyond bookkeeping.** A refusal cell (L12-L16) and a broken
deployment are indistinguishable from the outside: both produce "the run did not
invoke a model". Without a recorded healthy-stack precondition, L12-L16 passing
proves nothing — the refusal may have been an outage. The verification probe is
what separates the two.

**Target account — now confirmed.** The operator's second-pass review confirms
account **879318057152** via the `embark1` profile. This closes half of decision
4 and of S6. The account is referenced here by the repository's own
by-reference convention (`aidlc/spaces/issue-4120/deploy-target.md`, ruling
D-R11: literals live in one committed place, `deploy-target: adp-dev-embark1`).

**Spend ceiling — still unapproved, and still the gate.** The review states the
live spend ceiling remains unapproved. Per ruling 3, probing ships disabled with
a zero spend budget and "may be enabled only in PMM-09 after the target account
and a spend ceiling are approved". So the account being confirmed does **not**
open the live phase: roughly 15 invoking cells (§7.2.5) have no approved budget
to run against. This is the single remaining operator action before AC-04 onward
can begin.

**Pre-run state.** `deploy-with-agent.md:144-178` defines
`.adp-deploy-state.json` with `account_id`, `aws_profile` and per-phase status.
The matrix run must record, before the first cell:

- the resolved account from `aws sts get-caller-identity` — asserted equal to
  the target, not assumed from the profile name (the `embark1` learning record
  at `agent_learning/2026-06-14-issue-1494-learnings.md:45` is explicit that a
  kubectl context name can point at the wrong account);
- the deploy-state phases relevant to the matrix (`gateway_backend`,
  `webhook_ingress`, `bedrock_model_access`) as `complete`;
- the posture at start (report-only), and the policy/snapshot revision in force.

**Post-run verification evidence.** `deploy-with-agent.md:192-206` gives the
health probes each matrix session must capture alongside its cells — frontend
and `/api/health` both 200, gateway pods `Running`, RDS `available`. For the
agent path, that an agent-worker pod actually spawned in `adp-agents`. These are
the evidence that a refusal cell refused rather than the stack being down.

**What the flip itself is.** Per §6.3 and ruling 4 the flip is an operator
dispatch against a live authority path, not a merge. `AGENTS.md` and
`CLAUDE.md` both state deployment/enforcement approval is separate from merging
code; §11.1 records the same boundary from the #5417 delivery plan. Merging this
note does not authorize any run described in §7.

---

## 8. The epic's example table must be amended (AC-10)

Two corrections, both verified:

1. **Fable.** `model_validate.py:34-38` excludes `claude-fable-5` deliberately:
   "listed ACTIVE but do NOT invoke for us" because it "requires non-default
   data-retention mode", with the #2300 lesson cited. `claude-fable-5-1`
   appears **only** in pricing data (`047_claude_pricing_v2.py:1927+`).
   **Pricing rows are not invocability evidence** — that conflation is the
   #2300 defect in miniature. Neither alias map offers any Fable alias, so
   Fable is not selectable today even before invocability is considered.
   Decision: either the operator enables the data-retention prerequisite and
   PMM-03 probes Fable 5.1 successfully, or the epic's headline example is
   amended to a model the platform serves. The epic must not close describing
   a configuration the platform refuses to serve.
2. **The "testing" persona does not exist.** The epic's example names a
   "Reviewer/testing persona". `personas.py:10-64` defines 12 personas and no
   `testing`. The example should name `reviewer`. This matters beyond
   cosmetics: the epic states "a 'testing' row is offered only if testing is a
   registered persona", so the example contradicts the epic's own rule.

   Counting note for anyone re-verifying: the 12 is the union
   `VALID_PERSONAS = set(MENTION_TO_PERSONA.values()) | set(LABEL_TO_PERSONA.values())`
   (`personas.py:62-64`), computed at import time — there is no literal list of
   12 to read. `MENTION_TO_PERSONA` (21-58) already contributes all 12;
   `LABEL_TO_PERSONA` (10-18) adds 7 that are a strict subset. So the count must
   be confirmed by importing the module, not by grepping the dictionaries — a
   grep scoped to either single map gives 7 or 12 and a naive grep over both
   gives 19 lines.

---

## 9. What can run in parallel

| Track | Work | Blocked by |
|---|---|---|
| A | The `us.…sonnet-4-6` invocation probe (§2.2) | target env/account only |
| B | Inventory correction + canonical-constant refactor + drift test (§3) | D4 (ruled) + §3.3 boundary confirmation. **Independent of the resolver shipping.** |
| C | Deploy-gate/preflight alignment incl. the profile-vs-agreement fix (§2.3) | Track A's result |
| D | Comparison-report tooling and the per-path baseline (§5.2) | PMM-07 report-only mode |
| E | Matrix harness + evidence schema (§7.2) | PMM-02/04/05/06 |
| F | Epic example amendment (§8) | PMM-03's Fable probe |
| G | Flip-gate workflow modelled on `credential-binding-flip.yml` (§6.3) | §6.2 decision, now ruled by S2 |
| H | Recording the pinned GPT **tool** literal in AC-01 while naming the Claude class as the only class with a persona-execution default (§3.5) | nothing — R2 settled ownership (§12.1). The two stale-prose fixes move to #5433 |

Tracks A, B, F and H are startable now and are the only ones not gated on an
unshipped predecessor. Everything from D onward waits on the eight open
stories (§1.2). Note that "startable" here means the *design* work and the
non-live edits: per §11.1, no developer is dispatched on any story until the
#5417 synthesis is published and approved.

---

## 10. Decisions — current status

Six items were raised across this note's revisions. **Five are now settled** by
the #5418 lockings, the §11 PR contract, the §12 epic rulings and the operator's
second-pass review. **One remains open.** Rows below are marked with what
settled them; the "Recommendation" column is historical for settled rows and no
longer describes an open question.

Per §11.1 nothing here is an authority of its own: the settled rows record
operator rulings, and any surviving recommendation is a submission to the epic's
decision table.

| # | Decision | Status | Disposition |
|---|---|---|---|
| **1** | **Prove `us.anthropic.claude-sonnet-4-6` invocable** in the target account, via the Claude-harness runtime shape, with a request ID (§2.2). | 🔴 **OPEN — the one remaining substantive decision, now partly reframed.** | **R2 makes this formal**, not advisory: the identifier is a Claude-class **candidate**, and "not an active proven default until PMM-09 records a bounded invocation using the actual Claude harness request shape". So this is no longer "prove it or re-rule D4" — PMM-09 *is* the promotion step. `deploy-quickstart.md:787` carries the `invoke-model` command, so the tooling exists; per S1 the recorded proof must use the **Claude harness shape**, not that one-liner, and must not infer success from the marketplace agreement since `normalize()` strips the prefix (§2.3). Blocks AC-01, AC-02. |
| **2** | **AC-09's "without a redeploy" is not achievable** with the current config shape (§6.2). | ✅ **SETTLED by S2 — superseded alternatives now removed from §6.2.** | Enforcement posture becomes audited, versioned runtime policy read by the gateway with bounded caching, **fail-closed on unknown revisions**, owned by **PMM-02/PMM-07**. Confirmed landed in PMM-02's current head as a versioned `enforcement_posture` field keyed by class (§12.2). PMM-09 demonstrates it (L24) but does not build it; recovery is bounded by cache TTL, so the measured time is still recorded. |
| **3** | **Confirm the consolidation boundary** (§3.3) — persona-execution defaults only. | ✅ **SETTLED by S3.** | Ratified as written, including that classifier, summarizer, probe, pricing, test-fixture and `agent-context` models are **explicitly inventoried exclusions** listed with reasons, not silently skipped. |
| **4** | **Name the target environment and account, and approve a spend ceiling.** | 🟠 **HALF SETTLED — ceiling still open.** | **Account confirmed** on the second-pass review: **879318057152** via `embark1` (§7.7). The **live spend ceiling remains unapproved**, and per R3 probing stays disabled at zero budget until it is. This is a pre-run gate immediately before the live phase, not a design question — but it does currently block the ~15 invoking cells. |
| **5** | **Rule on D5/#3186 and on #2293** (§6.5, §7.3). | ✅ **SETTLED by S4, conservatively.** | No enforcing flip before PMM-06 authority/bootstrap is live (#3186/#5195) **and** PMM-07 delivers #2293's requester feedback. AC-07 may **not** be recorded "blocked, flip anyway". PMM-07's current head additionally assigns the #2293 channel to itself, so the ownership half of this question is also closed (§12.2). All of #3186, #5195, #2293 remain OPEN — a sequencing block, not an open decision. |
| **6** | **Does PMM-09 own the Codex-class consolidation, or is it filed against #5433?** (§3.5). | ✅ **SETTLED by R2 — withdrawn.** | Compatibility ownership is **PMM-03's**, and #5433 owns `gpt-*` personas with a separately proven Codex default. Neither is PMM-09's. AC-01 names the Claude class as the only one with a persona-execution default and records the pinned GPT **tool** literal as owned elsewhere; per PMM-01 rev-5 `codex-sdk` has no persona mapped to it, so there is no second class default to consolidate. The two stale-prose sites are filed against #5433. Full reasoning in §12.1. |

**Also to be recorded, not decided:** D4 supersedes the issue's "contested
default" premise and disposes of #4673, #2684 and #1128 (§2.4); the issue's
inventory counts and two globs are corrected in §3.1 (and are **Claude-class
counts only**, per §12.1); the epic's example table needs two amendments (§8).

**Not authorized by this note:** no implementation, no deployment, no flip, no
developer dispatch. PMM-09 remains blocked on §1.2 (all eight predecessor
stories open) independently of the five decisions above.

---

## 11. Operator synthesis contract (ruling of 2026-09-18, PR #5439)

The epic operator reviewed this note on PR #5439 and issued the following
contract. Where it differs from §3.3, §6.2 or §10, **this section governs.**
The note's status is accordingly **proposed pending the #5417 synthesis**.

| # | Requirement | Effect on this note |
|---|---|---|
| **S1** | D4's exact Claude-class default must pass a real bounded invocation **with the actual Claude harness request shape** before any consolidation or flip. A listing or normalization check is not evidence. | **Confirms decision 1 and §2.3.** The `normalize()` prefix-stripping trap is explicitly not a substitute for proof. Decision 1 stays open — it needs a recorded result, not a documented procedure. |
| **S2** | Implement enforcement posture as **audited, versioned runtime policy read by the gateway with bounded caching**, so rollback needs no image rebuild or full code deployment. This runtime setting belongs to **PMM-02/PMM-07** and stays **fail-closed on unknown revisions**. | **Rules decision 2: option 2, not option 1.** §6.2's recommendation of option 1 is superseded. Two consequences: AC-09 becomes genuinely satisfiable rather than reworded, and the work lands in PMM-02/PMM-07, so PMM-09 does not absorb it. The fail-closed-on-unknown-revision requirement answers the cold-start gap noted in §6.2 — an unknown or unreadable revision must refuse, not fall through to a permissive default. Bounded caching means recovery is bounded by the cache TTL, so the demonstration must still record the measured recovery time. |
| **S3** | Consolidate **persona-execution defaults only**. Classifier, summarizer, probe, pricing, test-fixture and unrelated `agent-context` models remain **explicitly inventoried exclusions**. | **Ratifies decision 3 and the §3.3 boundary**, including the requirement that exclusions be listed with reasons rather than silently skipped. |
| **S4** | **No enforcing flip** before PMM-06 authority/bootstrap is live (#3186 / #5195 prerequisites) **and** PMM-07 delivers actionable requester feedback (#2293 behaviour). | **Rules decision 5, in the conservative direction.** §6.5 asked whether the model flip could precede snapshot-signature enforcement; the answer is no. §7.3 asked whether to flip with AC-07 blocked; the answer is no — wait on #2293. So AC-07 is not permitted to be recorded as "blocked, flip anyway", and the §7.3 concern about trading a silently-wrong model for a silent failure is resolved by ordering rather than accepted. #3186, #5195 and #2293 are all OPEN, which extends §1.2's sequencing block. |
| **S5** | PMM-09 remains the **true live end-to-end story**: UI and ADP CLI, human and service-account self/admin, direct and multi-hop invocations, invalid/disallowed/retired models, request IDs, usage/pricing evidence, shadow comparison, flip and rollback. | **Not satisfied by the earlier seven-row table — the operator ruled so on the second pass.** That table collapsed the four principal/administration combinations into two rows and omitted cross-tenant denial, UI/CLI parity, and four of the five refusal classes. §7.2 is rewritten as **25 separately-recorded cells** (L1-L25) covering self/administered × human/service × UI/CLI, cross-tenant denial, parity on resolution source and reason vocabulary, five distinct refusal mechanisms, the ARC root-identity cells from ruling 5, non-inherited override, and flip/rollback with measured recovery. S5 is satisfied by §7.2 as rewritten, not by the prose. |
| **S6** | **Dev is first**, but the target AWS account and live spend ceiling remain an **explicit pre-run operator gate**. | **Half closed on the second pass.** The account is now confirmed: **879318057152** via `embark1`. The **live spend ceiling remains unapproved**, and per ruling 3 probing stays disabled at zero budget until it is — so the ~15 invoking cells cannot start. Decision 4 is narrowed to the ceiling alone (§7.7). |
| **S7** | Defaults are **per harness compatibility class**. #5433 requires a separately proven **Codex/GPT default with no Claude fallback**. | **New requirement, not previously in this note.** AC-01's "one identifier everywhere" is now explicitly *one identifier per harness compatibility class*. The canonical Claude-class default is D4's `us.…sonnet-4-6`; the Codex/GPT class needs its own separately proven default, and a Claude model must **not** serve as its fallback. This follows D6 (model selection is constrained by the execution harness) and means the §3.4 canonical-constant design must be keyed by harness class rather than a single global constant. #5433 is OPEN. **But a pinned GPT literal already exists in the tree and this note had not inventoried it — see §3.5, which corrects the assumption that S7 is wholly future work, while withdrawing the stronger "second class is live" reading per PMM-01 rev-5.** **Ownership is since settled by R2: the compatibility registry is PMM-03's and the Codex default is #5433's, so PMM-09 records the literal rather than consolidating it (§12.1).** |

**Consequences for the story's shape.** S2 and S7 both move or reshape work:
the posture mechanism moves out of PMM-09 into PMM-02/PMM-07 (so §1.1's "out of
scope: every resolution mechanism" is preserved, and AC-09's demonstration
becomes a PMM-09 activity against a PMM-07 mechanism), and the consolidation
target becomes per-harness-class rather than single-valued. S4 adds #5195 and
#2293 to the flip's prerequisite set alongside the eight predecessor stories.

**Net status (updated after the second-pass review and §12's rulings):** one
substantive decision open — S1/R2's Claude-harness invocation proof — plus S6's
**spend ceiling**, the account half now being confirmed. Decision 6 is withdrawn
(§12.1). The flip is gated on #3186, #5195, #2293 and #5433 in addition to the
eight predecessors. Merging this note still authorizes nothing.

### 11.1 This note's standing under the epic-level synthesis gate

§11 records the operator's contract on *this PR*. A separate and broader
constraint applies from the epic, and this note must not be read without it.

The operator added an **architecture synthesis gate** to #5417 on 2026-09-18:
the eight downstream architect runs are "parallel investigations, not eight
independent sources of truth". Before any developer is dispatched, the operator
reviews all outputs together and publishes **one unified, versioned design** for
#5417, reconciling at minimum one vocabulary and precedence ladder, one
persona/harness/model compatibility contract, one schema and API surface, one
catalogue/allowlist/invocability contract, one snapshot format and signing
authority, one resolver contract, one audit/cost/evidence schema, and one
deployment DAG with its report-only posture, live matrix, enforcing gate and
rollback.

Three things follow for this document, and they are stronger than "pending":

1. **This note is subordinate, not authoritative.** The gate states that
   story-local documents "may provide detail, but may not override the canonical
   design silently". So where the published #5417 synthesis differs from
   anything here — including §11's own rulings as transcribed — **the synthesis
   governs and this note must be corrected**, not read as a competing source.
   That is why the status line says PROPOSED and not ACCEPTED.
2. **The design conclusions here are inputs to a decision table, not decisions.**
   The gate says conflicting recommendations across the eight runs "will be
   listed in a decision table with an explicit ruling and affected stories". The
   §10 recommendations and the §3.5 finding are therefore submissions to that
   table. PMM-09 is the last story in the DAG, so several of its findings
   (§6.2's posture mechanism, §3.3/§3.5's boundary, §7.4's inert allowlist) are
   really change requests against earlier stories and should arrive at the
   synthesis as such.
3. **A merged canonical design and an operator approval comment are hard
   prerequisites for the first developer wave**, which is PMM-02/PMM-03 — both
   upstream of this story. This compounds §1.2: PMM-09 is blocked not only by
   eight open predecessor stories but by a synthesis that has not been published
   and a PMM-01 note not yet on the default branch (PR #5436 is OPEN, adding
   `docs/design-notes/5417-per-invoker-persona-model-mapping.md`). Merging *this*
   PR does not advance that gate.

The delivery plan on #5417 also confirms two boundaries this note already
assumes, worth recording so they are not re-litigated: architecture work for
#5419-#5427 may proceed in parallel because it is design-only (which is why this
note exists while its predecessors are open), and "deployment/enforcement
approval remains separate from merging code". The second is the same
authorization boundary as §6.3.

### 11.2 Locked decisions this note does not otherwise engage

§11 and §2 cover D4, D5 and D6, and §7 covers D2 and D3. For completeness
against the six locked decisions on #5418, two notes:

- **D1 (personal persona mapping and organization model access are separate
  policy axes)** bears on the live matrix in one specific way. D1 states an
  organization rule "must never silently replace the principal-selected model",
  and that a disallowed selection "fails with an actionable explanation. It does
  not substitute another allowed model". That makes D1 the source of a matrix
  cell this note's §7.2 table does not contain: a principal whose *selected*
  model is refused by *organization* policy, asserting the run is blocked
  naming the selected model rather than silently running the org-permitted one.
  D1's own worked example is exactly this (preference Opus, org allows only
  Sonnet/Fable → blocked naming Opus, does not run Sonnet). **Now carried as
  cell L13** in the rewritten §7.2.3, alongside four other refusal mechanisms,
  since it exercises a different mechanism — access policy rather than
  allowlist, invocability or unknown-identifier.
- **D2's scope limit** is worth restating beside §7.3 so the flip is not
  over-claimed: a valid `/model` request is an audited override "for the
  directly invoked hop only", and does not propagate to descendants. So AC-06's
  multi-hop cells must show a `/model` override on the root hop **not** reaching
  child hops, which resolve from the root principal's snapshot. That is a
  distinct assertion from "each hop resolves by its own persona". **Now carried
  as cell L21** in §7.2.4.

Neither changes a verdict; both were cells the live matrix would otherwise have
missed, and both are now in it.

---

## 12. The binding #5417 unified rulings (2026-09-18)

The epic operator published **unified architecture rulings** on #5417 closing the
cross-story questions the second-pass review exposed. They are stated to be
"binding inputs to the canonical #5417 design" and to **supersede conflicting
story-local recommendations** — including this note's. This section records them
and their effect here. Where §11 or any earlier section differs, **§12 governs**,
because §11 is a PR-level contract and §12 is the epic-level ruling that §11.1
already subordinates this note to.

| # | Ruling | Effect on PMM-09 |
|---|---|---|
| **R1** | **Canonical service principal** owned by PMM-02 (#5419): an opaque immutable `canonical_service_principal_id`, tenant-scoped source-qualified aliases `(org_id, alias_source, alias_id)`, never globally keyed by alias name. Raw `service_accounts.id`, `agent_name`, `client_id` or ARN **never owns a preference**. Cognito `Organization.cognito_client_ids` is an approved-client list, **not** an identity. | Reshapes the service-account cells. L5/L6 must assert that two authentication namespaces resolve to the **same canonical principal** (or refuse as unregistered), and L3/L4 that administered writes accept **only** canonical IDs from the server discovery surface. Verified absent today: no `canonical_service_principal` symbol exists in the tree, so every service cell is blocked on PMM-02. |
| **R2** | **Compatibility ownership** is PMM-03's (#5420): the persona→harness-class registry and invocability evidence. Class IDs are stable and unversioned (`claude-agent-sdk`, `codex-sdk`); harness revision is a separate versioned field in evidence/snapshot keys. PMM-02 owns versioned Postgres default/posture records **keyed by class**. **`us.anthropic.claude-sonnet-4-6` is a Claude-class *candidate*, not an active proven default until PMM-09 records a bounded invocation using the actual Claude harness request shape.** #5433 registers `gpt-*` personas and a separately proven Codex default; **no cross-class fallback**. | **Resolves decision 6** (see §12.1) and hardens decision 1: the ruled default's status is now formally *candidate*, and PMM-09 is named as the story that promotes it. Adds cell **L16** (harness-incompatible) to §7.2.3, which the no-cross-class-fallback rule requires. §3.4's canonical constant must be keyed by class with the harness revision a separate field. |
| **R3** | **Probe safety:** PMM-03 ships probing **disabled with a zero spend budget**; no page load invokes a model. Nightly and catalogue-change probes may be enabled **only in PMM-09 after the target account and a spend ceiling are approved**. Pricing/listing/agreement status never counts as invocability proof. | Names PMM-09 as the story that may enable probing, and makes the **spend ceiling a hard precondition** for doing so (§7.7). Independently confirms §2.3's agreement-vs-invocability trap and §8's pricing-rows point. |
| **R4** | **Snapshot authority:** PMM-06 C2 approved — gateway work-admission resolves Postgres authority and persists snapshot/digest in **worker-unwritable** storage; mandatory gateway bootstrap verifies workload/run/root and returns a fresh audience- and chain-bound `adpe1` assertion; **30-second maximum TTL, reissued per hop**; workers get **no signer secret** and no long-lived offline-verifiable token. `service_policy` is owned by the canonical service principal; the approving human is **audit attribution only**. | **Answers §7.4's open snapshot question.** The chain-snapshot assertion no longer needs the pricing snapshot it could not borrow — but the mechanism is per-hop 30-second assertions over a durable digest, **not** one long-lived signed blob. So L17/L18 must assert the **`snapshot_digest` is identical across hops** (the durable record), not that one signed token was reused; a 30-second assertion cannot span a chain. L18's policy owner is the canonical principal per R1. |
| **R5** | **ARC/GitHub Actions root identity** follows the **authenticated initiator**, not the bot credential executing the job. Human `issues:labeled`, issue-comment or `workflow_dispatch` preserves the resolved canonical human root. Scheduled/service-to-service runs with no authenticated human resolve a **tenant-bound registered canonical service principal and fail closed if unregistered**. Execution identity is audit attribution only. | **Adds cells L19 and L20** — neither existed in any prior revision. These are the highest-risk cells in the matrix because both run green today while attributing the root to the executing bot rather than the initiating human: a silent misattribution, not a visible failure. |
| **R6** | **One API contract:** FastAPI self routes exist **once** at `/me/persona-models`; service SigV4 callers use external `/agent/me/persona-models`, whose API Gateway proxy strips `/agent` and reaches the **same handler** — do not duplicate the backend router. Six canonical surfaces (self read/catalog/explain/put-delete, `manageable-service-principals`, and `/service-principals/{canonical_id}/persona-models`). All callers share one request/response schema and **one refusal vocabulary**. Self handlers derive canonical identity; administered handlers accept only canonical IDs from server discovery. | Gives §7.2 its route column and makes **L11 (shared refusal vocabulary) a contract assertion rather than a nicety**. The `/agent` prefix-strip claim verifies in-tree: `modules/gateway/infra/modules/api-gateway/main.tf:317` states the Bedrock `/agent` proxy strips its prefix because the pod serves at root paths, in contrast to `/internal/*` which preserves it. Confirms the self/administered split as **different routes with different authorization**, which is why §7.2.1 separates L1/L2 from L3/L4. |

### 12.1 Decision 6 is resolved by R2 — withdrawn

The prior revision raised **decision 6**: does PMM-09 own the GPT-literal
consolidation under S7, or is it filed against #5433? **R2 answers it.**
Compatibility ownership — the persona→harness-class registry and invocability
evidence — is **PMM-03's**, and #5433 owns registering `gpt-*` personas with a
separately proven Codex default. Neither is PMM-09's.

So the §3.5 finding stands as a **finding**, and its recommendation narrows:
PMM-09's AC-01 names the Claude class and its candidate default, records the
pinned GPT **tool** literal as **owned by PMM-03/#5433 with its current value
(`codex-config.toml:25`)**, and the two stale-prose sites are filed against
#5433 rather than fixed here. Decision 6 is **withdrawn, not deferred** — the
operator has already answered it at the epic level. §10's decision-6 row and
§0's framing of §3.5 as "the one finding added after the contract" are corrected
accordingly.

The §3.5 evidence itself is unaffected and remains worth carrying to the
synthesis, in the form PMM-01's current head permits: a live pinned GPT literal
with a pin test (`test_codex_config.py:30-34`) that the Claude default lacks,
governing a **delegated tool** rather than a second class's execution harness
(`5417-…:759-763`, `:1195`); and the note's 159/64 inventory figures are
**Claude-class counts only**. §3.5 previously stated the withdrawn
second-class-is-live version and is corrected in place on this pass.

### 12.2 Sibling reconciliation at current heads

The second-pass review requires reconciling **current** sibling heads rather
than stale versions. Each sibling design note was re-read at the head listed
below, and every head OID and line citation in this table was re-resolved on
the third pass. **Four have moved in ways that change claims in this note** —
PMM-01 (withdraws the second-live-class claim, correcting §3.5), PMM-05
(corrects two verb/flag specifics and adds the SigV4-self path behind L25),
PMM-07 (absorbs the #2293 channel) and PMM-08 (inverts the spend-evidence
premise). PMM-02's and PMM-03's rows carried citations that no longer resolved
and are repointed.

| Story | PR / head | Bearing on PMM-09 |
|---|---|---|
| PMM-01 #5418 | #5436 **`b8045dbf`** (note file is `5417-per-invoker-persona-model-mapping.md`) | **Materially moved — the previous row said "Rev-3"; the head is now Rev-5** (`:3`, `:8`). D1-D6 stay locked and the class-keyed system default stands, consistent with R2. **Rev-5 withdraws rev-4's claim that a second compatibility class is "already live in the tree"** (`:8`, `:759-763`, `:1195`): `codex-sdk` is a **reserved class ID with no persona mapped to it** (`:24`, `:159`), because the pinned GPT model configures a delegated tool, not a persona's execution harness. **§3.5 of this note asserted that withdrawn version and is corrected on this pass.** Still **not on the default branch** (§1.2 holds). |
| PMM-02 #5419 | #5437 **`e2c7d099`** | **Re-verified at the current head on the third pass; the previous revision of this row cited `d9e83968` with line numbers that no longer resolve.** Carries `enforcement_posture` on the class-keyed platform record (`:1152`), keyed by `harness_compatibility_class` (`:1148`) with a monotonic `revision` (`:1151`), `UNIQUE` per class (`:1155`), platform-admin-only and audited (`:1158`). **Correction to the earlier claim that "the S2 mechanism has landed in PMM-02's design":** only the *record* has. A search of that head returns **zero** occurrences of cache, request-time or unknown-revision language — the read path is absent by design and is PMM-07's (§6.2). Self surface is `/me/persona-models`, matching R6. |
| PMM-03 #5420 | #5434 **`25ece717`** | **Re-verified; the previous row's `6d5b2d10` citations no longer resolve.** Supplies the **fixed ten-string refusal vocabulary** §7.2.3 now uses (`:361`, with the prose-labels warning at `:359`), the retired-yet-invocable rule (`:263`) behind L14, and the stale-evidence read/validate split (`:257`, `:367`) behind L15. Also records that **four disjoint refusal vocabularies exist across the epic** (`:538`), which qualifies L11's parity assertion. |
| PMM-04 #5422 | #5435 **`5fc0fb4f`** | Verifies **three** machine identifier spaces (`:32`, F1) — the basis for splitting L5/L6 — and the no-selector-when-managing-nothing property behind L8. |
| PMM-05 #5423 | #5441 **`b9e8e96e`** | **Materially moved, and the previous row was wrong on two specifics.** The verb set (`:440-447`) is `adp models catalog`, `mappings list/set/reset`, `explain` and **`service-principals list`** — not `service-accounts list`; and the administered flag is **`--service-principal ID`** taking an opaque canonical ID, with `--service-account` explicitly withdrawn (`:454-459`). More consequentially it now designs the **service-principal-self SigV4 path** (`:73`, `:747-749`, `:877-885`) that cell **L25** exercises — see §7.2.1. §7.5's "no `models` verb exists" remains true of the *tree*. |
| PMM-06 #5424 | #5442 **`f9f0ec68`** | **C2 is the approved R4 shape**: per-hop 30-second `adpe1` assertions over a worker-unwritable `snapshot_digest`, explicitly **not** a long-lived signed blob (`:1627`, restating ruling U4 at `:483`; `MAX_ENVELOPE_TTL_SECONDS = 30` verified in code at `envelope.py:93-95`). C3a (`:1629`) adds that **enforcing mode is blocked on #3186/#5195** and that report-only may observe only — independent corroboration of §6.5's sequencing. This is what L17/L18 must assert against. |
| PMM-07 #5425 | #5438 **`82bc735f`** | **Materially moved — see below.** |
| PMM-08 #5426 | #5444 **`88efc5fe`** | **Materially moved — see below.** |

**PMM-07 has absorbed the #2293 feedback path.** Its current head, §6.2
(`5425-persona-model-resolver-wiring.md:1162-1165`, ruling S3 also tabled at
`:49`), rules that *"#2293's actionable requester feedback ships in PMM-07, and
that D2 cannot reach enforcement without it"*, explicitly superseding its own
earlier draft that pushed the channel to PMM-09 — *"The first draft argued the
opposite — that report-only could ship here and PMM-09 should own the feedback
path. That recommendation is superseded"* (`:1163-1165`; the withdrawn question
is closed at `:1384`). Its sequencing consequence (`:1186-1189`) states the
division directly: *"the D2 flip's dependency is satisfied inside PMM-07 rather
than deferred … PMM-09 still owns the enforcing flip — but the feedback path is
no longer the thing standing between them."*

Consequence for this note: **§7.3's framing is stale.** §7.3 says AC-07's
delivery channel "does not exist" unless #2293 ships first, and §10's decision 5
asks whether to flip with AC-07 blocked. Under PMM-07's current head the channel
is in-scope work with a completion boundary inside PMM-07, so the question is no
longer "who builds it" but simply **ordering**, which S4 already settled (no
flip before #2293's behaviour is delivered). #2293 remains OPEN, so the
sequencing block is unchanged — but PMM-09 must not be written as though it
might have to own the channel. §7.3 is corrected in place.

**PMM-08 inverts AC-11's spend-evidence premise.** Its current head (C-2,
`5426-persona-chain-cost-attribution.md:117`, with the field/column split at
`:123-126`) establishes that `attributed_org_id` is **not** a `usage_logs` column
— it is a `TokenContext` field — and that `usage_logs.org_id` already holds the
attributed tenant. More consequentially for §7.2's evidence
kinds, `:155-160`: **Budget & Spend reads `budget_usage`, not `usage_logs`**
(quoting `mantle_service.py:809-811`), and `budget_usage`
(`shared/models/budget.py:24-42`) has "no persona, model or run dimension at
all", so a per-persona view can never be reconciled against it. Both
`usage_logs` writers **swallow exceptions** (`service.py:649-653`,
`mantle_service.py:761`), so a row can be lost silently, and a post-hoc raw-SQL
UPDATE can change `cost_usd` after the fact
(`bridge_cost_to_usage_logs`, `budget-usage-tracker/handler.py:314`).

*Citation note:* PMM-08's note cites these as `service.py:479` and
`mantle_service.py:716` (`5426-…:162-163`), which are the `_log_usage`
**definitions**; the swallowing handlers are at `service.py:649-653` and
`mantle_service.py:761`, both re-verified in the working tree on this pass. The
claim verifies — the line references do not, and are corrected here.

Consequence: §7.5's advice to source per-cell spend from Bedrock/CloudWatch
directly is **right, and for a stronger reason than stated** — not merely that
`resolution_source` is missing, but that the platform's authoritative spend
ledger cannot carry a persona dimension at all, and the non-authoritative one
can silently lose the row. A cell's spend figure must therefore be captured from
the invocation response and Bedrock/CloudWatch at run time, and **labelled as
not reconciled against `budget_usage`**. §7.5 is corrected in place.
