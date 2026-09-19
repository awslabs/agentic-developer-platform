# Design Note: One Persona-Model Resolver on Every Dispatch and Runtime Path (Issue #5425 / PMM-07)

> **Status**: **PROPOSED — pending #5417 synthesis.** This is PMM-07's story-local input to the
> epic's unified design, **not** an independent design of record. Per the #5417 synthesis gate
> (2026-09-18), the epic operator publishes one unified versioned design covering all nine
> stories; this document supplies detail and may not override that canonical design. Four
> prerequisites remain unmet (none of them now an unowned gap). **The one question this note
> previously carried open (Q5′, ARC synchronous-versus-local resolution) is now closed by the
> fourth-pass ruling: ARC must obtain a gateway-authoritative decision before the harness step,
> and local pre-validation is not an available option on that path.** The note is **not
> implementation-ready** while the ARC obligation that ruling creates is unowned. See §0.0d, §0.3,
> §0.6.
> **Author**: @agent-architect
> **Date**: 2026-09-18
> **Issue**: #5425 — [PMM-07] Single persona-model resolver wired into every dispatch and runtime path
> **Parent**: #5417 (EPIC). Decisions D1–D6 locked on #5418; first-pass synthesis rulings S1–S7 on
> this PR; **second-pass unified rulings U1–U6 on #5417, which are binding and supersede
> story-local recommendations wherever they conflict (see §0.0b)**.
> **Depends on**: #5419 (PMM-02 storage), #5420 (PMM-03 catalogue), #5424 (PMM-06 snapshot)
> **Mode**: Per-issue implementation design review + story-local design input
> **Verified at revision**: `82bc735f` (branch `agent/issue-5425`, parent `ae598410` on main). The
> fourth-pass ARC findings in §0.0d and §0.6 were verified directly against this head.
> **Sibling heads reconciled against**: PMM-01 #5436 `8edc055c`, PMM-02 #5437 `e2c7d099`,
> PMM-03 #5434 `25ece717`, PMM-06 #5442 `f9f0ec68`, PMM-08 #5444 `88efc5fe`, PMM-09 #5439 `86c7959a`.
> **Re-fetched during the fourth pass: all six are unchanged from the third pass**, so the
> cross-story claims below are current rather than assumed. No sibling asserts the ARC
> local-pre-validation shape this pass removes (checked against PMM-03, PMM-06 and PMM-09 heads), so
> the §0.0d correction is contained to this note.
> **Four of these moved after the previous revision of this note was written**, and one of the moves
> reverses this note's headline blocker — see §0.5. Sibling heads move faster than any note can
> track: PMM-02 `e2c7d099` landed at 17:09 and PMM-06 `f9f0ec68` at 17:19 on 2026-09-18, after the
> previous revision was pushed. Treat every cross-story claim here as true-as-of these SHAs, and
> note that PMM-02 `:1284` and PMM-09 `:1231` still reconcile against this note at `67db0294`, so
> the staleness is **mutual** — the synthesis, not a story-local note, is where it converges.
> **Code findings below are still stated "at `67db0294`" on purpose**: every commit on this branch
> since then touches only this file, so the tree outside `docs/design-notes/` is byte-identical at
> `67db0294` and `e003df94` (`git diff 67db0294 HEAD -- . ':(exclude)docs/design-notes'` is empty)
> and every line-numbered citation re-verifies unchanged. The two SHAs are not an inconsistency.
> **Related**: #2279 (`/model` directive, closed), #2293 (rejection feedback, open — **now in
> this story's scope per S3**), #2300 (invocability lesson, closed), #4673 (unsafe worker default,
> open), #2684 (Opus latency, open), #1128 (Opus 4.8 verification, open), #3186 / #5195 (authority
> path, open), #4511 (inert-config class), **#5433 (native GPT/Codex personas — supplies the
> compatibility-class requirement in S6)**

---

## 0.0 The synthesis rulings this revision applies

Seven rulings were issued on this PR by the epic operator as part of the #5417 cross-story
synthesis. They are binding on this document and several **overturn positions the first draft
took**. Recorded here as the reader's index, with the section that implements each:

| # | Ruling | Effect on this document | Section |
|---|---|---|---|
| S1 | The trusted **gateway is the authoritative resolver**. Webhook/worker artifacts may carry a versioned generated catalogue for bounded local pre-validation, but there must not be two behaviourally independent "pure resolver" copies | **Overturns** the first draft's two-vendored-copies contract | §4.1, §4.1a |
| S2 | **GitLab, orchestration-engine dispatch and ARC/GitHub Actions persona paths are all in scope.** Any path with a trusted human/service root resolves its mapping; scheduled paths resolve the registered service principal. PMM-09 consolidates residual literals; PMM-07 wires the resolution contract | **Overturns** the first draft's recommendations to exclude the engine and ARC; answers Q1/Q2/Q3 | §2.2, §2.2a |
| S3 | **#2293's actionable requester feedback ships in PMM-07.** D2 cannot reach enforcement without it | **Overturns** the first draft's "PMM-09 owns it"; answers Q4 | §6.2, §0.3 |
| S4 | Repository/project committed settings **never override a root principal mapping**. They may constrain admission only | Confirms the draft's recommendation as a ruling; answers Q5 | §4.4 |
| S5 | **Onboarding templates are inventoried here**, corrected/consolidated in PMM-09 | Confirms and tightens; answers Q6 | §2.3a |
| S6 | **Default lookup is by harness compatibility class.** D4's Sonnet identifier is the Claude-class *candidate* pending live proof; #5433 requires an independent Codex/GPT default and **forbids cross-family fallback** | **Overturns** the draft's single-`canonical_default` shape | §4.1, §4.4, §5 |
| S7 | Mark the document **proposed pending #5417 synthesis**, not an independent design of record | Applied in the header and §1 | header, §1 |

## 0.0b The second-pass unified rulings this revision applies

A second review pass at head `67db0294` and a **binding unified-rulings comment on #5417** landed
after the S1–S7 revision. The epic comment declares itself binding and supersedes story-local
recommendations; the PR review defers to it explicitly ("See #5417's unified rulings"). Where the
review's compressed summary and the epic ruling disagree, **the epic ruling governs** — §0.4
records the one place that happens and why it matters.

| # | Unified ruling (#5417) | Effect on this document | Section |
|---|---|---|---|
| U1 | Service/automation preferences are owned by a **canonical service principal**, with aliases **tenant-scoped and source-qualified** `(org_id, alias_source, alias_id)`. Raw `service_accounts.id`, `agent_name`, `client_id` or ARN never owns a preference | **Overturns** §2.2's "the mechanism already exists" for EventBridge. The existing lookup key is a *global* namespace and does not satisfy this | §2.2, §2.2b |
| U2 | Harness compatibility **class IDs are stable and unversioned** — `claude-agent-sdk`, `codex-sdk` — with harness/contract revision a separate versioned field. **No cross-class fallback, ever.** PMM-03 owns the class registry; #5433 registers `codex-sdk` | Corrects this note's invented class name `codex-gpt`; confirms the no-fallback contract | §4.5, §4.6, §2.3b, §5 |
| U3 | A **versioned runtime posture** (report-only vs enforcing) must be gateway-readable at request time, with a **bounded cache** and **fail closed on an unknown revision**. PMM-09's S2 assigns the mechanism to PMM-02/PMM-07 | **New obligation.** No such concept exists in code; this note must specify the read path | **§4.7 (new)**, §8 |
| U4 | The trusted-root snapshot is resolved from Postgres authority at work admission, persisted with its `snapshot_digest` in a **worker-unwritable** store, and carried per hop by a **short-lived assertion whose `body_digest` is that digest**. The 30-second max TTL is **retained deliberately** because the assertion is reissued per hop. **No caller may assert it already verified the snapshot** | **Overturns** §4.1's `# signature already verified by the caller` and §7's "verify before resolve, never inside" as written; **resolves** §4.3's TTL mismatch as settled rather than open | §4.1, §4.3, §7 |
| U5 | Root ownership follows the **authenticated initiator**, not the bot credential that executes the job. A human `issues:labeled`, `issue_comment` or `workflow_dispatch` event preserves the resolved **canonical human root**; the **service-principal** path is for runs with **no authenticated human initiator**, and fails closed when unregistered | **Partially overturns** this note's Q1′ recommendation (b). The obligation is a **split**, not a single reading | §0.4, §2.2a, §11 Q1′ |
| U6 | One API contract for the persona-model surface; the ALB-gated agent route strips its prefix, so route shapes must be stated in post-strip terms | Confirmed factually; mostly PMM-03/PMM-02 surface, touching this note only at the edges | §2.2b |

**What this revision changed as a result.** Six positions this document previously asserted are
now corrected rather than merely annotated: the caller-verified snapshot affordance (§4.1, §7),
the "TTL does not fit" framing (§4.3), the EventBridge "mechanism already exists" row (§2.2), the
class name `codex-gpt` (§4.5), the single-reading ARC recommendation (§0.4, §11 Q1′), and the
absence of any runtime-posture read path (§4.7). Three previously-open operator decisions are
**settled** and marked as such in §11.

## 0.0c Third pass — the operator's ARC clarification, and what re-verification changed

The operator issued a **clarification to the ARC item** after the second-pass revision was pushed:
*"A human `issues:labeled`, issue-comment, or `workflow_dispatch` event preserves the resolved
canonical human root; scheduled, service-to-service, or workflow-triggered runs without an
authenticated human initiator use a registered canonical service principal. The bot/workflow
execution credential is audit attribution only. Please implement this split rather than a blanket
service principal."* **That is the split §0.4 already states**, so this pass confirms the reading
rather than changing it — and this section records the confirmation explicitly so a reader is not
left guessing whether the clarification was absorbed.

What this pass **did** change came from re-verifying, not re-reading:

| Change | Why |
|---|---|
| **§0.5 / §11 Q4′ reversed from "no story owns the registry" to "PMM-02 owns it"** | PMM-02's head moved to `e2c7d099` **after** the previous revision was written and adopted this note's own recommendation. This was the note's headline blocker; it is now a delivery dependency |
| **Two quotations withdrawn** | PMM-02 no longer contains "the largest open item; it blocks the schema" (§0.5) and PMM-06 no longer contains "a change to a security contract, not a no-op" (§0.3 P4′). Against current heads these were **fabricated citations**, which is worse than stale ones |
| **§0.4 gains the ARC credential and tenant obstacles** | The human arm needs more than an actor field: the canonical-human resolver is gated by a gateway internal secret no persona workflow holds, and the richer resolver needs an installation ID the ARC payload never carries. Without this, "plumb `github.event.sender`" reads as sufficient and is not |
| **New §11 Q5′; §0.6's question promoted into the decisions table** | §0.6 said the ARC authority-versus-latency question was "the operator's call" while §11 listed only Q4′ — so an operator reading the decisions table would never have seen it |
| **Three citation defects corrected** | The scheduled-workflow count (9, not 11), the no-schedule preflight test's real location, and the `grants.py` quote's exact lines. All three were found by re-executing the checks rather than trusting the previous revision |
| **One sibling claim downgraded** | PMM-08 mentions posture **zero** times, so "PMM-08 inherits that record" was asserting a commitment PMM-08 never made (§4.7) |

## 0.0d Fourth pass — the ARC ruling closes Q5′, and it refutes this note's own recommendation

A fourth-pass review at head `82bc735f` issued one ruling, and it is the most consequential
correction in this document's history because it invalidates a recommendation this note made three
times:

> "ARC cannot locally preselect then rely on later gateway resolution: the Actions harness invokes
> Bedrock directly. Require a gateway-authoritative decision before the harness step (or route its
> model traffic through gateway), using machine authentication and the U5 human/service root split;
> never expose the internal API key. Close Q5 and remove the implementation-ready claim while it is
> open."

**The ruling is factually right and this note was wrong.** Q5′ recommended that ARC pre-validate
against the generated catalogue and let the gateway "resolve authoritatively at invocation time".
Verified at `82bc735f`, there is no such later moment on this path:

- All nine persona workflows set `CLAUDE_CODE_USE_BEDROCK: "1"` with **no base-URL override**
  (`agent-developer.yml:203`, `agent-architect.yml:196`, `agent-operations.yml:249`,
  `agent-pm.yml:181`, `agent-product.yml:197`, `agent-reviewer.yml:217`,
  `agent-pt-superpower.yml:255`, `skill-agent.yml:193`, `malware-analysis-agent.yml:208`).
  `ANTHROPIC_BEDROCK_BASE_URL`, `ANTHROPIC_BASE_URL`, `SIGV4_PROXY_PORT` and
  `ADP_GATEWAY_ENDPOINT` are **zero-hit greps across all of `.github/workflows/`**. The harness
  therefore calls the Bedrock service endpoint directly.
- **The contrast with the in-cluster worker is the whole point, and this note never drew it.** The
  worker sets `CLAUDE_CODE_USE_BEDROCK=1` *and* `ANTHROPIC_BEDROCK_BASE_URL=http://127.0.0.1:9090`
  (`entrypoint.py:2188-2189`), a local SigV4 proxy that "re-signs for API GW" (`:2187` comment;
  `_start_sigv4_proxy` at `:2442`, its docstring "re-signs requests … using execute-api SigV4" at
  `:2446-2447`, port at `:2454`). So on the worker path the gateway genuinely **is** in the
  inference path and "resolve authoritatively at invocation time" is a real mechanism. On the ARC
  path it is not a mechanism at all — it names a moment that does not exist.
- **The runner's IAM grant removes any residual doubt.** `runner-iam/main.tf:93` grants
  `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on `Resource = "*"` (`:123`).
  A refusing-only local check cannot constrain what such a job invokes, because nothing downstream
  inspects the call.

**Why the error mattered, stated plainly.** Q5′'s safety argument was the §4.1a asymmetry — a
satellite may refuse but never select, so a stale catalogue causes a false refusal, never a wrong
model. That argument is valid **only when something authoritative selects afterwards**. On ARC
nothing does, so the asymmetry degrades into: whatever literal the workflow already carries is what
runs, and the local check is decoration. This note would have shipped a control that satisfies
AC-14's "exactly one selecting path" on a technicality — the ARC path selects *nothing*, because a
hard-coded `env:` literal was never a resolver — while leaving nine live workflows pinned to
`global.anthropic.claude-opus-4-6-v1` regardless of any saved preference. That is precisely the
"your setting is a lie" failure §0.1 says this story exists to prevent, reintroduced by the
remedy.

**The ruling is buildable, and §0.6 now specifies it rather than asking.** The two obstacles Q5′
raised against a synchronous call both dissolve under the ruling's "machine authentication"
clause, and the pieces already exist in the tree — see §0.6. The ARC path already holds ambient
AWS IAM identity (IRSA, `arc-runner/main.tf:146-153`, role annotation at `:151`) and `execute-api:*`
(`runner-iam/main.tf:142-143`), so the credential objection was an artifact of assuming the
internal shared secret was the only route.

| Change this pass made | Why |
|---|---|
| **Q5′ closed as a ruling; the superseded recommendation removed, not annotated** | A recommendation left standing beside the ruling that overturned it reads to the next implementer as a live alternative. The previous two passes were both faulted for exactly this |
| **§0.6 rewritten from "unresolved conflict" to "the ruled shape and its cost"** | The question is decided; what an implementer needs now is the mechanism and its unowned work, not the debate |
| **Status and §0.2 verdict: implementation-ready claim removed** | Instructed by the ruling, and independently correct — the corrected ARC obligation is unowned work on nine workflows |
| **§4.1a and §4.2 bounded to the paths where their reasoning holds** | The local-pre-validation shape is still right for webhook and engine, both of which are in a gateway path. Deleting it would overcorrect; leaving it unbounded is what produced this error |
| **AC-09 and AC-02 restated** | AC-09 asserted "no new synchronous gateway call" as a global claim; that is now false on ARC by ruling. AC-02's ARC arm was blocked on Q5′ and can now be written |
| **The internal-API-key route explicitly ruled out** | The ruling forbids exposing it. §0.4's credential obstacle is retained as *history* but must not read as a live option |

---

## 0. Executive summary

### 0.1 What this story is for

Nothing in the epic so far changes which model a run uses. PMM-02 stores the preference,
PMM-03 validates it, PMM-06 carries it. This story is the one that makes the saved
preference actually govern execution — and it is the story where the epic can do real
harm, because a resolver defect routes work to a different model, and model choice is
spend.

The deliverable is one resolution function with one definition of precedence, called by
every path that decides a model, shipped in a posture where it records its answer
without yet deciding the run.

### 0.2 Verdict

**Not implementation-ready.** Every *decision* this note escalated is now settled — the S1–S7
synthesis and the U1–U6 unified rulings closed engine scope, GitLab scope, ARC scope and principal,
#2293 ownership, the snapshot-default shape and the class taxonomy, and the fourth-pass ruling
closed the last one (Q5′, §0.0d). So nothing here waits on an operator to choose. What blocks
implementation is no longer a decision but **unowned work that the ARC ruling exposes**: the nine
persona workflows must obtain a gateway-authoritative model decision before the harness step, and
that is a per-workflow restructure plus a new gateway surface which no story currently owns (§0.6).
The readiness claim the previous revision made is withdrawn on the ruling's instruction and is
independently wrong.

**The correction that matters most is to this note itself.** Its Q5′ recommendation —
pre-validate locally on ARC, let the gateway resolve at invocation time — is impossible, because
the Actions harness invokes Bedrock directly and no later gateway step exists on that path
(§0.0d). Had it shipped, nine workflows would have stayed pinned to a hard-coded model while a
local check reported compliance. The design direction is otherwise right and larger in scope than
the issue describes. What an implementer must carry:

1. **The resolver is one gateway-owned authority, not a library vendored twice (S1).** The
   first draft proposed two independently-behaving copies because the webhook Lambda cannot
   afford a network call. S1 forbids that. The reconciliation — a gateway authority plus a
   *generated, versioned, read-only* catalogue in the satellite artifacts that may refuse early
   but never select differently — is §4.1a, and it composes two working in-repo precedents
   (`pricing_policy` for versioned fail-closed loading, the control-envelope vectors for a
   generated artifact crossing the Python→TypeScript boundary). **That reconciliation is valid only
   for the webhook and engine paths**, whose model traffic passes through the gateway. It does
   **not** hold on ARC, where the harness reaches Bedrock directly, so the catalogue may not stand
   in for resolution there (§0.0d, §0.6).
2. **`spawn_persona` is not the single enforcement point, and all five paths are now in scope
   (S2).** Two live dispatch paths bypass it, one in a different module and container image
   (§2.2). S2 puts GitLab, the orchestration engine and the ARC workflows in scope for the
   *resolution contract*. The ARC path needs work the issue never contemplated: it performs
   **zero identity resolution today** (§2.2a). **U5 now says which principal it must resolve** — a
   split by trigger, human root for human-initiated events and a registered service principal
   otherwise (§0.4) — which closes the scope question and opens an implementation one, because the
   service arm's registry does not exist (§0.5).
3. **The inventory is not nine paths; it is ~30 live sites in agent-factory alone, 60+
   platform-wide** (§2.3), and the issue mis-globs the workflow set. An inventory that is wrong
   at the start produces a test that enforces the wrong set and reports completeness it does
   not have.
4. **There is no single canonical default any more (S6).** The default is keyed by harness
   compatibility class. D4's `us.anthropic.claude-sonnet-4-6` is the **Claude-class candidate
   pending live invocability proof**, and #5433 forbids a `gpt-*` persona ever falling back to
   it. Every "the canonical default" in the first draft was singular and is now wrong (§4.1, §4.4, §5).
5. **#2293's requester feedback ships here (S3)**, which converts the first draft's headline
   sequencing blocker into in-scope work (§6.2).
6. **"No synchronous gateway call on the dispatch path" is flag-dependent, not settled.** The
   path already makes one such call in the deployed configuration and a second fail-closed one
   once the authority flags flip (§4.2). S1 adds a third consideration, since a gateway
   authority is by definition reachable over the network.
7. **The resolver may not accept a pre-verified snapshot (U4).** The first draft and the S1–S7
   revision both took a verified-by-the-caller object as a parameter. U4 forbids that: verification
   is per hop, against the worker-unwritable protected record, behind the mandatory bootstrap. The
   signature change is in §4.1 and the obligation in §7; the in-repo precedent for doing it this way
   is `agentauth/model_identity.py`, which already re-authenticates rather than trusting the caller.
8. **A versioned runtime posture must be readable at request time (U3), and nothing like it
   exists.** `runtime_posture` is a zero-hit grep in the tree. §4.7 specifies the contract from
   three working precedents rather than inventing one.

### 0.3 The four remaining unmet prerequisites

S3 resolved the first draft's P3 (it is now in-scope work, not a prerequisite) and S6 restated
P4. What is genuinely still unmet:

| # | Prerequisite | State at `67db0294` | Consequence if ignored |
|---|---|---|---|
| P1 | PMM-06 snapshot (#5424) | Not built | This story has no data source. It cannot start ahead of #5424 beyond the pieces in §9.1 |
| P2 | D5's gateway-signed snapshot needs the authority path live | `AGENT_AUTHORITY_ENABLED` and `ADP_WORK_CLAIMS_ENABLED` both default `false` (`sqs_publisher.py:54,60`; §4.3) | Enforcement is ungateable. Report-only is still shippable |
| **P3′** | **U1's canonical service principal with tenant-scoped, source-qualified aliases** | **Not built in code, but now owned by design.** `canonical_service_principal`, `alias_source` and `service_principal` remain zero-hit in gateway source; the live lookup key is still `{identity_type, identity_value}` (`webhook-ingress/lambda/common/service_identity.py:71-76`), a **global namespace with no `org_id`**, which is what U1 forbids; and the registration surface is still pinned to `^eventbridge:` (`service_authority.py:51`). **What changed:** PMM-02's head `e2c7d099` now keys uniqueness `(org_id, alias_source, alias_id)` among active rows (`5419…:552-554`) and declares "PMM-02 owns the identity slice. This is no longer an open ownership question" (`:62-67`), with its §12 retitled "Operator decisions — **all settled**". So this is a **delivery dependency on PMM-02, not an unowned gap** — §0.5 | PMM-07's service-rooted call sites cannot be written until PMM-02 ships the registry. Not a blocker needing an operator ruling any more; a sequencing constraint (§0.5, §9.3) |
| **P4′** | **U4's per-hop assertion carrying the snapshot digest** | **Partially exists.** Bootstrap returns an **`adpr1`** HMAC-SHA256 run credential (`agentauth/run_credential.py:61`, format `:19-25`), **not** the `adpe1` Ed25519 assertion U4 describes; the `adpe1` signer has no `audience` parameter and no chain-binding claim. So U4 mandates an **extension of the bootstrap response and the envelope claim set**. PMM-06's current head `f9f0ec68` agrees and has firmed up its status: its C2 is now "✅ **SETTLED — approved by #5417 ruling U4**", and the extension — "a snapshot-specific audience constant plus a chain-binding claim, on both sides of the golden vectors" — is recorded as "a **build instruction, not a pending approval**" (`5424…:1627`). *(The previous revision attributed to PMM-06 the phrase "a change to a security contract, not a no-op"; that wording is not present at `f9f0ec68` and the quotation is withdrawn in favour of the text above.)* | PMM-07 would consume an assertion shape that does not yet carry `body_digest`. Owned and ruled, but **not yet built** — §4.3 states the required extension rather than assuming it |

Two items that are now **scope**, not prerequisites, and must be planned as work:

- **#2293 requester feedback (S3).** Open; the two environment variables it needs are written
  at `entrypoint.py:1685,1687` and have **no consumer anywhere in the tree** (§6.2). D2's
  enforcement flip depends on it, so this story's completion boundary now includes it.
- **The Claude-class default's live proof (S6).** `us.anthropic.claude-sonnet-4-6` is named by
  no model-deciding code path — only `gateway-main.tf:477`, docs and pricing fixtures (§5). The
  deploy gate covers it in normalized form but does **not** pin the worker's real default
  (opus-5), so a fresh environment can still run an ungated model (§5).

### 0.4 U5 settles the ARC principal question — as a split, and the review's summary is narrower

The second-pass PR review summarises the ARC obligation as: *resolve ARC/GitHub Actions through a
tenant-bound registered canonical service principal and fail closed when unregistered; human
trigger identity is audit attribution, not the preference owner.* **U5 says something different
for human-initiated runs**, and U5 governs (the review defers to #5417's rulings explicitly):

> Root ownership follows the authenticated initiator, not the bot credential that happens to
> execute the job. A human `issues:labeled`, issue-comment, or `workflow_dispatch` event preserves
> the resolved canonical human root.

The reconciliation is a **split obligation**, and this note now states it as such:

| ARC trigger | Root principal under U5 | Fail-closed behaviour |
|---|---|---|
| Human-initiated `issues:labeled` (the live trigger on all nine) | **The resolved canonical human root** — the labelling human. The `adp-agent[bot]` App credential is **audit attribution only** | If the initiator cannot be resolved to a canonical human, refuse; do **not** silently fall back to the bot identity or a class default |
| `issue_comment`, `workflow_dispatch` | Canonical human root, same as above | **No live referent** — see the applicability note below |
| `workflow_call` (reusable invocation) | **No authenticated human initiator** in the declared inputs → the registered **canonical service principal** for the calling workflow | **Refuse when unregistered** (U1 + U5). No default, no inherited human |
| Scheduled | Registered canonical service principal | Refuse when unregistered. **No live scheduled persona workflow exists** (§2.2a) |

**This overturns this note's own Q1′ recommendation.** The prior revision recommended option (b)
— register ARC as a service identity — on the evidence that the acting credential is a bot App
identity. U5 rules that this reasoning is *inverted for human-initiated runs*: the bot credential
executing the job is exactly what must **not** own the preference. The in-repo precedent agrees and
is worth quoting, because it already encodes U5's distinction: `agentauth/grants.py:113-115`
documents `AuthorityReference.human_id` as "The human whose act it was, as recorded ON THE ROW.
Carried for audit attribution only: the acting principal stays an agent/service." U5 applies the
same separation in the opposite direction for selection — the human is the *owner* of the
preference even where the agent remains the *acting* principal.

**How far U5 is satisfiable at `67db0294`, precisely:**

1. **The human arm is derivable but not derived — and it needs a credential, not just a field.**
   All nine workflows gate on the label event and already read it (`agent-developer.yml:37`,
   `github.event.label.name`), so the event payload that carries the labelling actor **is present** —
   but no actor field is plumbed anywhere: `github.event.sender` appears **nowhere** in
   `.github/workflows`, and `github.actor` appears in **none of the nine** persona workflows (only
   `credential-binding-flip.yml:92,183`).

   **A GitHub actor is not a canonical human root, and closing that gap is the real cost.** U5 requires
   the *resolved canonical human root*, and the only mechanism that performs that resolution is
   `POST /internal/v1/resolve-user` (`modules/gateway/src/internal/routes.py:8-12`), reached through
   `resolve_user_by_identity(provider, provider_user_id)`
   (`webhook-ingress/lambda/common/gateway_client.py:103-146`). Two obstacles follow, and neither is
   field plumbing:
   - **The endpoint is credential-gated and ARC holds no credential.** It requires the
     `X-Internal-Api-Key` shared secret (`routes.py:19-22`, verified at `:77-90`), and neither
     `GATEWAY_API_URL` nor any internal-API-key reference appears in any persona workflow. (The one
     `X-Internal-Api-Key` occurrence anywhere in `.github/workflows` is
     `agent-context-verb-ops.yml:95`, a different service's `DOOR_API_KEY` — not the gateway secret
     and not a persona workflow, so it is not a counter-example.) Handing a
     gateway internal secret to a GitHub Actions job is a security decision in its own right, not an
     implementation detail — it is currently held only by the webhook Lambda.
   - **The richer resolver needs an installation ID the ARC payload does not carry.** The
     webhook path's `identity_resolver.resolve(installation_id, sender_id)` derives tenant from
     `identity_type="github_installation_id"` *before* resolving the user
     (`identity_resolver.py:294-325`), and `github.event.installation` appears **nowhere** in
     `.github/workflows`. So ARC would use the narrower provider-ID path and obtain tenant some other
     way — an unresolved detail an implementer will hit immediately.

   So "the cheaper half of the work" understated it: the human arm is derivable in principle, but it
   needs an actor field, a tenant source, and a credential decision. **The credential half is now
   ruled** (§0.0d): the call is machine-authenticated with the runner's existing IRSA identity over
   SigV4, and handing a GitHub Actions job the `X-Internal-Api-Key` is **forbidden**, so the
   obstacle described above is history rather than a live option (§0.6). The actor field and tenant
   source remain outstanding work.
2. **The `issue_comment` and `workflow_dispatch` arms have no live trigger at all.** No persona
   workflow declares either; `agent-reviewer.yml:36` *tests* for `workflow_dispatch` but the
   workflow never declares it, making that a dead arm. These two clauses of U5 are therefore
   **forward-looking**, not currently applicable, and an implementer must not build them speculatively.
3. **The `workflow_call` arm cannot be satisfied at all yet**, because the service-principal
   registry it requires does not exist — P3′ and §0.5. Its declared inputs are exactly
   `issue_number`, `repo_owner`, `repo_name`, `target_repo` (`agent-developer.yml:8-29`), so there is
   no initiator to resolve even in principle; it must fail closed as an unregistered service principal.

### 0.5 ~~Unresolved conflict A~~ — **now owned by PMM-02; this is a sequencing dependency, not an open conflict**

**This section previously said no story owned U1's service-principal registry and escalated it to the
epic operator as the note's headline blocker. That is no longer true, and the correction matters
enough to state plainly rather than quietly edit.** PMM-02's head `e2c7d099` — pushed at 17:09 on
2026-09-18, after the previous revision of this note was written — **adopts exactly the recommendation
this note made**, in its own words:

> "PMM-02 owns the identity slice. This is no longer an open ownership question." (`5419…:62-67`)
> "**Uniqueness is `(org_id, alias_source, alias_id)` among active rows — tenant-scoped and
> source-qualified, never a global alias name.** Revision 4 specified `UNIQUE (alias_source,
> alias_id)`; ruling 1 corrects this" (`:552-554`)

So both halves of the previous finding are reversed: PMM-02's key now includes `org_id`, and its §12
is retitled "Operator decisions — **all settled**". **The previous revision also quoted PMM-02 as
recording alias-table ownership as "the largest open item; it blocks the schema". That string is gone
from PMM-02's current head (zero grep hits) and the quotation is withdrawn** — against `e2c7d099` it
would be a fabricated citation, which is worse than a stale one. PMM-03 independently disclaims the
registry and points at PMM-02 (`5420…:43`, `:444-446`), so there is no competing claim.

U1 requires a canonical service principal with `(org_id, alias_source, alias_id)` aliases; U5's
service arm requires resolving against it and failing closed when unregistered. **The design
ownership is settled. What remains is that none of it exists in code yet:**

- The live service-identity lookup is **not tenant-scoped**. `service_identity.py:71-76` keys on
  `{"identity_type": "service_account", "identity_value": service_identity}`; tenant and org are
  *results read from the row* (`:96-97`), not part of the key. A global alias namespace is exactly
  the shape U1 prohibits.
- Worse, the key's value comes from **attacker-shaped event input**: `eventbridge/handler.py:94`
  reads `detail.adp_trigger.service_identity` from the event payload rather than deriving it from
  the rule ARN or IAM principal. The one rule-ARN cross-check (`VerifiedServiceEvent.from_native_event`,
  `:143-153`) is flag-gated and **does not run** while `AGENT_AUTHORITY_ENABLED` is off — which is
  its default.
- The gateway surface that would *register* such a principal is **hard-pinned to EventBridge**:
  `service_authority.py:51` constrains `service_identity` to `^eventbridge:[A-Za-z0-9_.-]{1,128}$`.
  ARC cannot register here without a schema change.
- PMM-06's C1 is settled *on the assumption that the registry exists* and explicitly refuses to
  invent a second identifier for it (`5424…:1627` and the C1 row: "**PMM-06 does not invent a second
  identifier for it**"). That is the correct posture and it is now satisfied by PMM-02's ownership
  rather than left dangling.

**What this means for PMM-07, precisely.** PMM-07 consumes the registry and must not define one — a
second identity namespace is the exact failure mode S1 was issued to prevent. That was true before and
is unchanged. What changed is the *kind* of dependency: this is no longer a decision awaiting the
operator but **a delivery dependency on PMM-02**, and it is the reason the service-rooted arms of
AC-02 cannot go green until PMM-02 ships. **Report-only selection on the human-rooted paths is
unaffected and remains shippable.** One residual form question belongs to the synthesis rather than
to either story: U1 says aliases are tenant-scoped *and* source-qualified, PMM-02 now keys
`(org_id, alias_source, alias_id)` among active rows, and PMM-06 reads only the resolved canonical ID
(`5424…:1584` notes both forms "should not both stand"). PMM-07 also reads only the canonical ID, so
either form satisfies it; it does not need the question resolved to proceed.

### 0.6 The ARC obligation as ruled: a gateway-authoritative decision before the harness step

*(This section previously carried Q5′ as the note's last open question and recommended local
pre-validation. The fourth-pass ruling decided it the other way and refuted the recommendation's
premise — §0.0d. What follows is the ruled shape, what already exists to build it, and the part
that is unowned. The superseded alternative is **removed rather than annotated**: leaving it beside
the ruling would read as a live option.)*

**The ruling.** On the ARC path the gateway must return an authoritative decision **before** the
step that runs the harness — or that job's model traffic must be routed through the gateway —
authenticated machine-to-machine, applying the U5 human/service root split (§0.4), and **never** by
handing the internal API key to a GitHub Actions job.

**Why there is no cheaper option.** Local pre-validation works on webhook and engine because the
gateway is downstream of them in the inference path; the worker literally points the SDK at a local
proxy that re-signs to API Gateway (`entrypoint.py:2188-2189`). ARC sets
`CLAUDE_CODE_USE_BEDROCK: "1"` with no base-URL override in any of the nine workflows and holds
`bedrock:InvokeModel` on `Resource = "*"` (`runner-iam/main.tf:93`, `:123`), so its call never
passes anything that could overrule a local answer (§0.0d).

**What already exists, so the ruling is work with a known shape rather than an aspiration.** The
two obstacles the withdrawn Q5′ raised against a synchronous call were both artifacts of assuming
the internal shared secret was the only way in. Under machine authentication they dissolve:

| Requirement | State at `82bc735f` |
|---|---|
| A machine identity on the ARC job | **Exists.** Runner pods run under IRSA: `serviceAccountName` from `kubernetes_service_account.runner` (`arc-runner/main.tf:122`), whose service account is annotated `eks.amazonaws.com/role-arn = var.runner_role_arn` (`:146-153`, annotation at `:151`). The job already relies on it — it calls `aws secretsmanager get-secret-value` with no configured credentials and no `configure-aws-credentials` step (`agent-developer.yml:58-65`) |
| Permission to call the gateway API | **Exists, no new grant needed.** `runner-iam/main.tf:142-143` holds `execute-api:*` on `Resource = "*"` |
| A gateway route that accepts that identity | **Exists.** `/agent/{proxy+}` is `AWS_IAM`-authed so "API Gateway validates SigV4" natively (`api-gateway/main.tf:269-270`, `lambda-authorizer/main.tf:518`) and strips its prefix before the pod (`api-gateway/main.tf:317`) — which is why U6 requires post-strip route shapes (§2.2b) |
| A signing precedent in-repo | **Exists, and it is the same migration.** Issue #575 moved the worker credential client off a shared secret onto IRSA/SigV4 (`adp_cred/__init__.py:16-21`); the signing call is `botocore.auth.SigV4Auth(credentials, "execute-api", region)` (`adp_cred/client.py:131`), repeated at nine further call sites including `lib/run_identity.py:95` and `webhook-ingress/lambda/common/gateway_client.py:343` |

So the ruling's "machine authentication, never the internal API key" is not merely permitted here —
it is the pattern this repository already standardised on for exactly this problem, and the
`X-Internal-Api-Key` route the previous revision treated as the blocker is the one thing the ruling
forbids. §0.4's credential obstacle is retained there as **history**, not as a live option.

**What is genuinely unowned, and why this note is not implementation-ready.** Three costs remain,
and none of them is a decision for the operator — they are work needing an owner:

1. **A per-workflow restructure, nine times.** The model literals are **step-level `env:`** on the
   step that runs the entrypoint (e.g. `agent-developer.yml:204`, whose `run:` is at `:210`), so a
   resolved value must arrive as a prior step's output and be referenced there. This is not a
   call-site change. The entrypoint is also **not** uniformly `agent-worker.ts`: three of the nine
   run a different one (`agent-pm.yml:182`→`src/agent-pm.ts` at `:190`,
   `skill-agent.yml:194`→`src/skill-agent.ts` at `:198`, and `malware-analysis-agent.yml`), so
   AC-04's worker-side assertion does not cover all nine.
2. **A resolution surface reachable by SigV4 that returns a decision for a *different* principal.**
   Under U5 a human-initiated ARC run resolves the canonical **human** root while the *caller* is
   the runner's machine identity. The existing self-service shapes (`/me/persona-models`, U6) answer
   for the caller, so ARC needs a resolve-for-principal call — authorised as a trusted platform
   caller, not as the human. That is a new authorisation shape and PMM-03/PMM-02 own the surface
   (U6), so it must be agreed with them rather than added here.
3. **The human arm still needs an actor field and a tenant source.** `github.event.sender` appears
   nowhere in `.github/workflows` and `github.actor` in none of the nine; the richer resolver also
   wants an installation ID the ARC payload never carries (§0.4). The machine-auth route fixes *how
   to call*, not *whom to ask about*.

**The fail-closed consequence, which the ruling makes unavoidable.** If the pre-harness decision
cannot be obtained — gateway unreachable, principal unresolvable, service principal unregistered —
the job must **refuse to run the harness** rather than proceed on the hard-coded literal. That is
the #2279-ruling-4 latency concern arriving as a real cost: it makes ARC agent runs depend on
gateway availability. This note previously treated avoiding that as sufficient grounds to
pre-validate locally; the ruling's judgement is that a CI-availability dependency is preferable to
an unenforceable preference, and §0.0d explains why that judgement is right — the local alternative
enforces nothing at all. Report-only posture (§4.7) bounds the exposure during rollout: while
report-only, an unobtainable decision is recorded and the run proceeds; the refusal behaviour
arrives with the enforcement flip, which is D2's gate and not this story's to pull.

---

## 1. Scope and the one-line contract

**In scope:** the gateway-owned resolution authority and its precedence (S1); the generated,
versioned satellite catalogue used for local pre-validation only **on the webhook and engine paths,
where the gateway is downstream in the inference path** (S1, §4.1a) — **not** on ARC, which must
obtain a gateway-authoritative decision before the harness step (fourth-pass ruling, §0.0d, §0.6);
the inventory of
model-deciding sites and its enforcement by test, including the onboarding templates (S5, §2.3a)
and the second harness family's default (S6, §2.3b); per-path consumption for all five dispatch
paths (S2, §2.2); the one-run directive's precedence; recording requested-vs-resolved with
source; **#2293's actionable requester feedback channel (S3, §6.2)**; behaviour when resolution
is unavailable; removal or documented exclusion of competing fallbacks; **per-hop snapshot
verification with no caller-asserted affordance (U4, §4.3, §7)**; and **the request-time read path
for the versioned runtime posture, with its bounded cache and fail-closed unknown revision (U3,
§4.7)** — the posture *record* is PMM-02's, the *read path and its use in a decision* are PMM-07's.

**Out of scope:** storage (PMM-02), catalogue (PMM-03), snapshot construction (PMM-06),
cost attribution (PMM-08), and the *enforcing flip* plus the default-consolidation *decision*
(PMM-09). Per S7 this story ships **report-only**: it may inventory and route, and it may refuse,
but it does not turn the inventory into a merge-blocking gate. S5 splits the onboarding templates
the same way — PMM-07 inventories the three sites, PMM-09 corrects them.

The contract in one line: **after this story, the only inputs that decide a model are the
one-run directive, the signed snapshot, and the default registered for the run's compatibility
class — and every other literal in the tree is either calling the resolver or carries a written
reason why it does not.**

Note the deliberate change of wording from the first draft's "the canonical default". S6 makes
the default **class-scoped**: there is no single canonical value, because a Codex-harness run and
a Claude-harness run cannot share one (§2.3b, §4.6, §5).

---

## 2. The corrected map — what is actually true at `67db0294`

### 2.1 What the issue got right

Verified true, with corrected line numbers where they drifted:

| Claim | Verdict | Actual location |
|---|---|---|
| `/model` is called with one positional arg, so `persona_allowed_models` is always `None` | **TRUE** | `webhook-ingress/lambda/github/handler.py:1777` |
| An unknown alias logs and proceeds on the default (lenient) | **TRUE** | `handler.py:1784-1789` — INFO log, no `return`, execution continues |
| The Lambda alias map has 8 version-pinned Anthropic-only aliases, no invocability check | **TRUE** | `common/model_validate.py:19-46` |
| The gateway map is wider and includes `openai.*`, `amazon.titan-*`, `meta.llama*`, `mistral.*` | **TRUE** | `gateway/src/proxy/model_resolver.py:85-100` — 8 patterns vs the Lambda's 4 |
| `claude-sonnet-4` diverges between the two paths | **TRUE, and worse than stated** | §3.1 |
| Worker reads `model_resolved`, falls back to `ANTHROPIC_MODEL` defaulting to `global.anthropic.claude-opus-5` | **TRUE** | `agent-worker-image/entrypoint.py:1576-1579`, written to the child env at `:1594` |
| `ADP_MODEL_REQUESTED`/`ADP_MODEL_RESOLVED` are exported with no consumer | **TRUE** | written at `entrypoint.py:1685,1687`; repo-wide grep finds **only those two write sites** |
| `ConfigLoader.setupBedrockEnv()` unconditionally writes `ANTHROPIC_MODEL` back | **TRUE** | `agent/src/components/ConfigLoader.ts:19`, `:28-32` |
| `enable-bedrock-models.sh` gates only opus-4-6-v1 and sonnet-4-6 while claiming to be in sync with `entrypoint.py` | **TRUE** | `platform/scripts/enable-bedrock-models.sh:36-43` |
| `model_aliases` is a dormant third naming store with no reader and no writer | **TRUE** | `gateway/src/shared/models/usage.py:77-82`; only refs are the migration, `alembic/env.py:23` and the `__init__` re-export |

### 2.2 🔴 Stale premise 1 — `spawn_persona` is not the single enforcement point

The issue's design rests on this sentence: *"All trigger adapters funnel here, which is what
makes a single resolver feasible."* That is false. There are **four** publish paths, and only
two of them reach `spawn_persona` with model arguments:

| Path | Entry | Reaches `spawn_persona`? | Model handling today |
|---|---|---|---|
| GitHub webhook | `github/handler.py:1834` | Yes | The **only** caller that passes `model_requested`/`model_resolved` (`:1849-1850`) |
| Agent-to-agent | `github/agent_trigger.py:374` | Yes | Passes **no** model arguments — so every agent-to-agent hop already silently drops to the pod default |
| EventBridge | `eventbridge/handler.py:237` | Yes | Passes **no** model arguments — same silent drop |
| **GitLab** | `gitlab/handler.py:229` | **No** | Builds its own envelope and calls `publish_envelope` directly, hard-coding `"model_requested": None, "model_resolved": None` (`gitlab/handler.py:172-173`) |
| **Orchestration engine** | `gateway/src/orchestration/dispatch_pass.py:1096` | **No** | Calls `sqs.send_message` directly from the **gateway** container; its envelopes carry no model fields at all |

The orchestration bypass is not an oversight to be corrected — it is a **ruling**.
`dispatch_pass.py:88-107` documents that `spawn_persona` is deliberately not called, because
its correlation-pointer store is agent-writable (#4304) and the engine must not source
authority from it. It further documents that `publish_envelope` is *not importable* from the
gateway image at all: the gateway Dockerfile copies only `src/`, `alembic/` and `cli/`, so
importing the Lambda's module "would raise `ImportError` in Lambda while passing locally".

**Consequence for this story.** "One resolver called from one place" is not achievable as
described — `spawn_persona` is one enforcement point among several, not the only one.

**S2 rules that all of these paths are in scope**, and S1 rules how. The first draft
recommended excluding the engine and asked the operator whether GitLab was in scope; both
questions are now answered, and the draft's "either wire the engine or record it as PMM-09
scope" is superseded. The resolution obligation per path, keyed to what its root principal is:

| Path | Root principal available today | Resolution obligation under S2 |
|---|---|---|
| GitHub webhook | Trusted human, resolved via `identity_resolver` | Resolve the human's mapping. Already passes both model fields; the one path that works |
| Agent-to-agent | Inherited lineage root (human or service) | Resolve **the chain root's** mapping from the snapshot, not the spawning agent's. Passes no model args today (`agent_trigger.py:374`) |
| EventBridge | A service-identity **key from the event payload**, resolved at `eventbridge/handler.py:118-120` via `resolve_service_identity` | Resolve **U1's canonical service principal**, keyed `(org_id, alias_source, alias_id)`. **The mechanism does not already exist** — corrected from the previous revision. Today's key is a global, non-tenant-scoped namespace (`service_identity.py:71-76`) and its value is read from `detail.adp_trigger.service_identity` (`:94`), i.e. from the event rather than from the rule ARN or IAM principal. U1 requires both to change; see §0.5 |
| GitLab | Trusted human, own resolution path | Resolve the human's mapping. Hard-codes both fields `None` today (`gitlab/handler.py:172-173`) |
| Orchestration engine | Trusted root carried in the envelope's protected identity fields | Resolve the root's mapping. Runs **in the gateway container**, so under S1 it calls the authority directly — no vendored copy, no `ImportError` problem |
| ARC / GitHub Actions | **Split by trigger (U5)** — canonical human root for human-initiated events, canonical service principal otherwise | See §0.4 for the per-trigger table. The human arm needs an actor field, a tenant source **and a credential decision** (§0.4 point 1); the service arm waits on PMM-02 delivering the registry it now owns (§0.5) |

Note that S1 makes the engine path *easier*, not harder: the engine is gateway-resident, so it
reaches the authoritative resolver by ordinary import. The `dispatch_pass.py:100-107` ruling
forbids importing the *webhook Lambda's* `publish_envelope` into the gateway; it says nothing
against the gateway calling its own resolver. The first draft's `stall.py` / `MAX_CHAIN_DEPTH`
mirroring precedent now applies only to the **generated catalogue data** (§4.1a), not to
resolver logic.

**Recommended direction:** wire the four Lambda-side adapters plus the gateway-resident engine
against the single authority. **The ARC path is no longer a gap to carry to the operator** — the
fourth-pass ruling settles its mechanism (a gateway-authoritative decision before the harness step,
machine-authenticated; §0.0d), and §0.6 states what that costs and which parts are unowned.

### 2.2a The ARC path resolves no principal today; U5 says which one it must resolve

S2 requires every path with a trusted human or service root to resolve its mapping, and **U5 now
settles *which* root** — as a split by trigger, per §0.4. The facts below are why that split needs
new plumbing rather than a call-site change; they are unchanged by the ruling, but their consequence
is now directed rather than open.

1. **No identity resolution occurs.** `agent-developer.yml` contains zero calls to any identity
   or tenant resolution (verified: no `resolve*` step, no tenant input). Its triggers are
   `issues: [labeled]` and `workflow_call` whose declared inputs are exactly `issue_number`,
   `repo_owner`, `repo_name`, `target_repo` (`agent-developer.yml:8-29`). No actor, tenant,
   correlation or principal input exists. A mapping lookup needs a principal; there is none.
   **What U5 changes:** the label-event arm has the raw material — the workflows already read
   `github.event.label.name` at `agent-developer.yml:37`, so the payload is in hand — but no actor
   field is extracted anywhere (`github.event.sender` appears nowhere in `.github/workflows`;
   `github.actor` in none of the nine). The `workflow_call` arm has no initiator even in principle
   and must fail closed as an unregistered service principal.
2. **The model is fixed before any step runs.** All nine workflows set the literal as a
   step-level `env:` (`agent-architect.yml:197`, `agent-developer.yml:204`,
   `agent-operations.yml:250`, `agent-pm.yml:182`, `agent-product.yml:198`,
   `agent-pt-superpower.yml:256`, `agent-reviewer.yml:218`, `skill-agent.yml:194`,
   `malware-analysis-agent.yml:209`). Substituting a resolved value means producing it as a
   prior step's output and referencing it — a workflow restructure, not a call-site change.
   Eight of the nine are bare literals with **no override hook at all**; only
   `malware-analysis-agent.yml:209` admits one, and it is a repo variable
   (`vars.MALWARE_ANALYSIS_AGENT_MODEL`), which S4 classifies as a committed/repo-scoped setting
   that may constrain but never select.

A third fact makes this worse rather than better: the workflow literal is **not** the last word
even today. `agent-worker.ts:127` and `ConfigLoader.ts:19` both fall back to
`global.anthropic.claude-opus-5`, `agent-pm.ts:118` and `agent-superpower.ts:35` to
`us.anthropic.claude-sonnet-4-20250514-v1:0`, and the deployed SQS worker default is a fourth
value (`infra/gateway-main.tf:477`, `us.anthropic.claude-sonnet-4-6`). So "the ARC path pins one
model" is itself an oversimplification — it pins one *ceiling input* to a chain that already has
three different downstream defaults. This is additional evidence for AC-01 being generated rather
than transcribed.

There is also **no live scheduled persona workflow** to apply S2's scheduled-path clause to.
`security-agent-nightly.yml` deliberately has no `schedule:` key and says so emphatically at
`:8` ("THERE IS DELIBERATELY NO `schedule:` KEY. DO NOT ADD ONE HERE."), with the reasoning at
`:10-17`; a preflight test fails CI if one appears — the test is
`.github/scripts/tests/test_securityagent_preflight.py:143` (`test_workflow_has_no_schedule_key`,
companion at `:162`), which the workflow merely *cites* in a comment at `:812`. Separately, of the
**9** workflows that do carry a real `schedule:` key, **none sets `ANTHROPIC_MODEL` or invokes a
persona** — the closest, `aidlc-gate-nudge.yml:4-5`, runs `actions/github-script` only.
(The previous revision said "11 workflows" and cited `:812` as the test itself; both are corrected
here. The 11 came from counting three files that mention `schedule:` only in comments —
`eval-bedrock-routing.yml:31`, `eval-budget-ratelimit.yml:35`, `script-tests.yml:163`.) And neither EventBridge rule that
feeds the dispatch Lambda is cron-based: both are event-pattern rules
(`webhook-ingress/infra/eventbridge.tf:36-56`, `:130-147`) and both default to disabled
(`variables.tf:317-321`, `:344-348`). So S2's scheduled-path clause has exactly one live referent
today — the EventBridge adapter, which already resolves a registered service identity via
`service_identity.py:52-112` and fails closed to `unknown_service_identity` on a miss (`:78-83`).

So S2's scheduled-path clause has exactly one live referent today — the EventBridge adapter — and
**U1 rules that its identity mechanism must be replaced, not reused** (§2.2, §0.5), which removes
the "the machinery already exists" argument the previous revision rested its Q1′ recommendation on.

Per `CLAUDE.md`, ARC remains a first-class *execution* model — deterministic pipelines an agent
triggers and monitors — while the webhook path owns open-ended agent work. That distinction is
why the first draft recommended excluding ARC from persona resolution. S2 overrode that
recommendation, and **U5 now supplies the missing principal by ruling**: the authenticated human
initiator for human-triggered runs, a registered canonical service principal otherwise. Q1′ is
therefore **settled** (§11) — what remains open is not *which* principal but *whether the registry
the service arm needs exists*, which is Q4′ and §0.5.

### 2.2b U6: one API contract, stated in post-strip terms

U6 requires a single API contract for the persona-model surface and notes that the ALB-gated agent
route strips its prefix. **Verified true:** `modules/gateway/infra/modules/api-gateway/main.tf:22-23`
documents the split — `/{proxy+}` with `NONE` auth for humans/JWT, `/agent/{proxy+}` with `AWS_IAM`
for agents/SigV4 — and the agent integration at `:270` maps to
`uri = "http://${var.internal_alb_dns}/{proxy}"` at `:285`, so the `/agent` prefix **is** stripped
before the backend sees the path. No `persona-models` route exists yet at this revision.

Consequence for PMM-07: this is largely PMM-02's and PMM-03's surface, and this note neither defines
nor duplicates the route shapes. It touches PMM-07 in exactly one place — an agent-path caller and a
human-path caller reach the **same** backend path, so a resolver-adjacent handler must not branch on
the presence of `/agent` to infer whether the caller is an agent. That inference would read as a
security check while being a routing artifact. Caller kind comes from the authenticated principal
(and, for agents, the bootstrap binding of §4.3), never from the URL.

### 2.3 🔴 Stale premise 2 — the inventory is ~3× larger than stated

The issue's nine-row table is a subset. An exhaustive sweep at `67db0294` finds, restricting
to **live, execution-deciding** defaults in `agent-factory` + `.github/workflows`, roughly
**30 distinct sites across ~24 files**; including `gateway`, `agent-context`, `research` and
the shipped onboarding templates, **60+ sites across 45+ files**. Corrections and additions
the issue misses:

| Correction / addition | Evidence | Why it matters |
|---|---|---|
| The workflow glob is wrong | `agent-*.yml` matches **25** files, but only **7** of them pin a model (architect `:197`, developer `:204`, operations `:250`, pm `:182`, product `:198`, pt-superpower `:256`, reviewer `:218`) — the other 18 are agent-context/infra/CI workflows with no `ANTHROPIC_MODEL`. The issue's count of 9 is right in total but wrong in set: the remaining two are `skill-agent.yml:194` and `malware-analysis-agent.yml:209`, which the glob does **not** match | A test written from the issue's glob both over-collects (18 model-free files) and misses two real ones. Enumerate by `ANTHROPIC_MODEL` content, not filename |
| `chat-scaledjob.yaml` has **two** literals | `:41` `ANTHROPIC_MODEL` **and `:42` `LCM_SUMMARY_MODEL`** | A second model decision on the same path |
| Terraform sets a **sixth, different** literal | `modules/agent-factory/infra/gateway-main.tf:477` `ANTHROPIC_MODEL = "us.anthropic.claude-sonnet-4-6"` | This is the **deployed** worker default, and it is the `us.` prefix. The comment at `:467-476` argues for `us.` deliberately because `global.*` returns "invalid model identifier" on some accounts. So the `global.` literals in code are overridden in practice — the inventory's real behaviour differs from its source reading |
| Summarization path has its own defaults | `complex-task-chat/context/lcm/config.ts:34`, `context/summarize/bedrock-summarizer.ts:27` | Both `global.anthropic.claude-sonnet-4-6` |
| Classifier/probe/grouping paths | `gateway/lambdas/ingest/classifier.py:23`, `infra/modules/lambda-gateway/variables.tf:127`, `gateway/src/shared/services/routing_probe.py:105`, `.github/scripts/author_grouping_plan.py:145` | Non-persona model decisions. Probably out of scope — but must be *named* as out of scope |
| Onboarding templates propagate a dead model to other repos | `rules/workflows/agent-template.yml:95`, `runner-infra/workflow-example.yml:58`, `runner-infra/scripts/full-onboard-repo.sh:121` — all `us.anthropic.claude-sonnet-4-20250514-v1:0` | These ship to customer repos. Leaving them exports the drift beyond this codebase |
| Admin default | `gateway/src/admin/agent_onboarding_schemas.py:113` `default="global.anthropic.claude-sonnet-4-20250514"` | A non-invocable default in an authoring surface |
| `ConfigLoader`'s default is on a **near-dead path** | `entrypoint.py:65` execs `/app/dist/agent-worker.js`; `ConfigLoader` is only reachable via `index.js` → `AgentService.ts:20` | AC-04 as written targets `ConfigLoader.setupBedrockEnv`, but the model the pod actually uses comes from `agent-worker.ts:127`. **A jest test on `ConfigLoader` alone would pass while the real path is unfixed** — a textbook plausible-wrong-result |

**Five mutually inconsistent literals are live**, two of them documented in-repo as
non-invocable: `us.anthropic.claude-sonnet-4-20250514-v1:0` (`model_validate.py:34-37` says
access-denied after 30d unused) and bare `claude-sonnet-4-5-20250929` (no inference-profile
prefix, would not resolve on Bedrock as-is, used at `PlanningAgent:47`, `FixOrchestrator:179`
and `:224`, `CodeGenerationAgent:33`, `MCPOnboardPlanningAgent:37`, `mcp-onboard.ts:73`,
`skill-agent.ts:22`).

**Recommended direction:** the implementer must regenerate the inventory as the first
commit, machine-readable, from a repo sweep — not transcribe the issue's table. AC-01's
enforcing test then asserts against that generated set. Classify every site into exactly
one of: *resolver-wired*, *deliberately-excluded-with-reason*, or *PMM-09 consolidation*.

### 2.3a S5 — the onboarding templates, inventoried here and corrected in PMM-09

S5 rules that the shipped onboarding templates are **inventoried in this story** and
corrected/consolidated in PMM-09. That is the first draft's Q6 recommendation, now a ruling, so
the inventory is recorded here rather than deferred. Three sites, all naming the same model:

| Site | Literal | Ships to |
|---|---|---|
| `modules/agent-factory/rules/workflows/agent-template.yml:95` | `us.anthropic.claude-sonnet-4-20250514-v1:0` | Customer repositories, as the agent workflow template |
| `modules/agent-factory/runner-infra/workflow-example.yml:58` | same | Customer repositories, as the documented example |
| `modules/agent-factory/runner-infra/scripts/full-onboard-repo.sh:121` | same | Written into a customer repository by the onboarding script |

All three name a model documented **in this repository as non-invocable**:
`model_validate.py:34-37` records the Legacy `sonnet-4-20250514-v1:0` identifier as
access-denied after 30 days unused. So onboarding currently exports a known-dead default to
other people's repositories. That is why S5 wants it inventoried now even though the fix is
PMM-09's: the inventory is what makes the exposure countable, and a customer repository is the
one place this platform cannot correct by a later merge.

**What PMM-07 owes:** these three rows in AC-01's generated inventory, classified *PMM-09
consolidation*, plus the invocability citation. **What PMM-07 must not do:** change them, since
S5 assigns the correction to PMM-09 and a template edit changes what new customers receive.

### 2.3b A second harness family already has its own default, and the issue's inventory misses it

Relevant to S6, and not in the issue or the first draft: the worker image already ships a
**second model default belonging to a different harness family**, configured outside every
Python and TypeScript constant the inventory sweeps.

- `agent-worker-image/codex-config.toml:25-26` sets `model = "openai.gpt-5.6-sol"` with
  `model_provider = "adp-gateway"`, and `:48` points at `http://127.0.0.1:9090/openai/v1`.
- The Codex CLI is installed globally in the image, not as a package dependency:
  `Dockerfile:121-123`, `ARG CODEX_VERSION=0.145.0`.
- The gateway already permits the family: `model_resolver.py:99` includes `openai.*`, added by
  #2709 precisely so Codex runs are not 403'd. The webhook Lambda does **not**
  (`model_validate.py:41-46`, Anthropic-only).

Three consequences for this story:

1. **S6 is not hypothetical.** A class-keyed default is not only preparation for #5433's
   `gpt-*` personas; it describes something the platform already does, in a file no
   model-literal sweep of `.py`/`.ts`/`.yml` would find. The AC-01 inventory must include
   `codex-config.toml:25` or it will again "report completeness it does not have".
2. **The comments are already stale**, which is the same drift class this story exists to fix:
   `Dockerfile:120` and the codex-bridge skill both still say `openai.gpt-5.5` while the TOML
   sets `gpt-5.6-sol`.
3. **The asymmetry between the two alias maps is now a cross-family asymmetry.** The gateway
   admits `openai.*` and the Lambda does not, so a hypothetical `gpt-*` persona dispatched via
   `/model` would be refused at the webhook and admitted at the gateway. Under S1 the gateway is
   authoritative, so §4.1a's generated catalogue must carry the *authority's* patterns — not the
   Lambda's current narrower set — or the local pre-check would refuse a legitimate GPT run.
   This is exactly the "false refusal only" property §4.1a relies on, but a false refusal of an
   entire persona family is not an acceptable resting state.

**Still absent, so not claimed:** no `gpt-*` persona key exists (`VALID_PERSONAS` at
`personas.py:62-64` has 12 keys, none `gpt-*`); nothing selects a harness per persona
(`HarnessDescriptor.adapterId` at `control-runtime.ts:111` is documented "Never used by
consumers to branch", and `agent-worker.ts:2077` constructs `ClaudeControlAdapter`
unconditionally); and no `@openai/codex-sdk` dependency exists in any `package.json`. Codex today
is a tool inside a Claude outer loop, which is what `codex.md:12-17` states and what D6's
per-hop harness check accounts for. #5433 owns changing that; PMM-07 must not assume it has.

### 2.4 🟠 Stale premise 3 — the `/model` path already has a second gap

Beyond the lenient fallback, `resolve_and_validate` has a **pass-through** at
`model_validate.py:71`: `MODEL_ALIASES.get(alias.lower(), alias)`. Any unrecognized string
that happens to match `*.anthropic.claude-*` is accepted **unvalidated** as a model ID. So
today a user can type an arbitrary non-existent `us.anthropic.claude-anything` and it becomes
`model_resolved`, overriding the pod default with a value that will fail at invocation time.
D3's "intersection" gate must close this, not just the alias-miss branch. The issue's D2
framing covers only the rejection branch.

---

## 3. The two divergent maps, and the third empty one

### 3.1 The divergence is worse than "wider aliases"

| | Lambda (`model_validate.py`) | Gateway (`model_resolver.py`) |
|---|---|---|
| Aliases | 8 | 47 |
| Allowed patterns | 4 (Anthropic only) | 8 (incl. `openai.*`, titan, llama, mistral) |
| `claude-sonnet-4` | **not an alias** → pass-through → matches no pattern → **rejected (`None`)** | `:45` → `anthropic.claude-sonnet-4-20250514-v1:0` → matches `anthropic.claude-*` → **allowed** |
| Case handling | lowercases (`:71`) | **case-sensitive** (`resolve_model`, `:145`) |
| Sync claim | `:40` comment says "matches model_resolver.py DEFAULT_ALLOWED_PATTERNS" | **False** — 4 vs 8 patterns |

Two additional facts: the gateway still aliases to the Legacy `sonnet-4-20250514` identifier
at `:45-46`, `:56`, `:62` and `:68` **while its own comment at `:31-32` documents that model
as access-denied**; and `GET /v1/models` (`routes.py:444-459`) advertises alias names filtered
by the wide gateway patterns, so it advertises titan/llama/mistral aliases that the Lambda
`/model` directive cannot accept at all. The two surfaces disagree about what the platform
offers.

**AC-03 is therefore necessary but insufficient as written.** "Identical canonical identifier
from both" must also assert the **case-insensitivity** contract and that neither map resolves
to a non-invocable Legacy identifier. Add the pass-through case: the same unknown-but-pattern-
matching string must be rejected identically on both paths.

### 3.2 The third store — decide, do not defer

`model_aliases` (`usage.py:77-82`, created in `001_initial_schema.py:74`) is tenant-scoped and
has no reader and no writer. The issue correctly says this must be used or explicitly abandoned.

**Recommendation: explicitly abandon it, in this story, with a migration-level comment rather
than a table drop.** Reasons: it holds only `alias_name → bedrock_model_id` with no version,
no invocability evidence, no harness compatibility and no freshness — so it cannot satisfy D3
or D6, which both require provenance the table has no columns for. PMM-03 owns the catalogue
and is the right home. Dropping the table is a destructive migration for zero benefit; leaving
it *unmarked* is how the drift recurs. A comment in the model class plus a test asserting no
production reader is the surgical answer.

---

## 4. The resolver contract

### 4.1 Shape: one gateway-owned authority (S1), class-keyed default (S6)

The first draft of this note proposed a pure library vendored into two separately-built
artifacts, with a contract test pinning them. **S1 overturns that**: there must not be two
behaviourally independent resolver copies, and the trusted gateway is the authority. §4.1a
gives the reconciliation with the webhook latency boundary that motivated the original shape.

The authoritative resolution function lives in the **gateway**:

```
resolve_persona_model(
    persona:           str,              # target persona for THIS hop
    snapshot:          VerifiedSnapshot, # U4: a value this process verified THIS hop (§4.3),
                                         #     never a caller-supplied "already verified" object
    direct_override:   str | None,       # one-run /model directive, this hop only
    class_defaults:    Mapping[str, str],# S6: compatibility class -> default identifier
    posture:           RuntimePosture,   # U3: versioned, gateway-read, fail-closed (§4.7)
) -> Resolution
```

**The snapshot parameter changed under U4, and the change is the point.** The previous revision's
comment read `# signature already verified by the caller`. U4 forbids exactly that: *no caller may
assert it already verified the snapshot.* `VerifiedSnapshot` is therefore a type that **only the
verification step in this process can construct** — it is unforgeable-by-construction rather than
documented-as-verified. A caller holding a raw snapshot cannot call this function at all; it must go
through §4.3's per-hop verification first. This is the difference between a comment and a contract,
and the previous shape made the resolver's most important precondition a matter of caller discipline.

PMM-06's head reaches the same conclusion independently, in stronger language: "root-ness is a
server-side conclusion, never a caller assertion," and for agent-to-agent hops the root "must be
copied from the parent's verified execution record, not from anything the calling agent sent."

`Resolution` carries `model`, `source`, `requested`, `harness`, `compatibility_class`,
`posture_revision`, and `reason` when refused. Five design points, each doing work:

- **The default is keyed by compatibility class, not singular (S6).** `class_defaults` replaces
  the first draft's `canonical_default: str`. D4's `us.anthropic.claude-sonnet-4-6` is the
  **Claude-class candidate pending live proof**, not *the* default. #5433 forbids a `gpt-*`
  persona resolving to it under any circumstance, so a single-valued parameter would make the
  prohibited cross-family fallback the *type-level default behaviour* — the worst place for it.
  A missing entry for the run's class is a refusal, never another class's value (§4.4).
- **Compatibility class is derived from the persona, not carried separately.** #5433 makes the
  harness a property of the persona key in its registry, so PMM-03's catalogue can supply the
  class. The preference table needs no new column; only the *default* becomes class-keyed. This
  is the same shape D1 used to keep organization scope out of the table.
- **`source` is returned, not inferred.** The `bedrock_routing.py` precedent is exactly right
  here: `BedrockTarget` carries `rung` alongside `account_id` because "the account alone
  cannot be debugged" — an operator "needs to know *which row* to fix" (`bedrock_routing.py:53-61`).
  Same reasoning, same shape.
- **Refusal is a return value, not an exception.** Report-only posture must be able to
  compute "this would have been refused" without aborting a run that currently succeeds.
- **The posture is a parameter, not an ambient flag read (U3).** `posture` arrives as a resolved,
  versioned value and `posture_revision` is returned in the `Resolution` so every recorded decision
  says which posture produced it. A resolver that reads a module-level flag cannot be tested across
  postures and cannot explain a past decision — the audit record would say what the posture is *now*,
  not what it was when the decision was made. §4.7 defines how the value is obtained.

**Separation that must hold (D1):** this function answers *which model*. It must not read
account routing, budget, or rate-limit state. `BedrockRoutingResolver.resolve` answers *which
account pays* and stays untouched. A budget refusal of a legitimately-resolved model is a
budget refusal — AC-10 — and must not re-enter this function.

### 4.1a S1 reconciliation: one authority, a generated catalogue at the edges

S1 permits the satellite artifacts to carry "a versioned generated catalogue for bounded local
pre-validation" but forbids a second independent resolver. The distinction that makes this
implementable, and that the PR must hold to:

| | Gateway authority | Satellite catalogue (webhook Lambda, worker image) |
|---|---|---|
| May **select** a model | Yes — it is the only thing that may | **No, ever** |
| May **refuse** early | Yes | Yes — and only for reasons the authority would also refuse |
| Content | Live resolution over the snapshot + PMM-03 catalogue | A generated, versioned, read-only data file: alias→canonical map, allowed patterns, class defaults, retirement state |
| On version mismatch with the authority | n/a | **Refuse, do not resolve** — fail closed, same discipline as `agentauth/execution.py:31-38` |
| Hand-editable | It is code | **No.** Committed as data, version-stamped, drift-tested in CI |

The asymmetry is the whole safety argument: a stale local catalogue can only cause a *false
refusal*, never a wrong model. A false refusal is visible and recoverable; a silently different
model is the cost incident this story exists to prevent. Two copies that may both *select* have
no such property, which is what S1 is protecting against.

**Two in-repo precedents together give the full shape, and the PR should compose them rather
than invent a pattern.** Each supplies a different half, and the difference between them matters
enough to state plainly.

*The versioning and fail-closed-load half — `modules/gateway/pricing_policy/`:*

- Immutable versioned snapshots committed as data — `pricing_policy/snapshots/2026-09-12.1.json`
  and `2026-09-12.2.json`. `__init__.py:17-21` states the immutability rule: a later bundle adds
  a **new** file and moves the pointer, "it never edits a published one".
- An explicit current version and an explicit compatibility version, as separate constants:
  `policy.py:46` `CURRENT_SNAPSHOT_VERSION = "2026-09-12.2"`, `policy.py:54`
  `COMPATIBILITY_SNAPSHOT_VERSION = "2026-09-12.1"` — deliberately never advanced together, so a
  replayed pre-decision event reproduces its original outcome.
- A loader that **fails closed on a missing version and validates on load** rather than
  degrading: `policy.py:655-666` raises `FileNotFoundError` for an unknown version;
  `snapshot_from_mapping` (`:668-692`) raises `ValueError` on missing required variants or
  duplicate keys. `selfcheck.py:31-73` loads **both** pins at build time and exits non-zero.
- Its own CI path trigger so an edit cannot ship untested: `gateway-ci.yml:30,83` include
  `modules/gateway/pricing_policy/**`, with the comment that without it "a pricing-only PR
  triggers no workflow and reports green". `tests/pricing_policy/test_lambda_packaging.py`
  asserts the Terraform archive uses a `fileset(...)` **glob** rather than a hard-coded list,
  precisely so a later snapshot cannot silently stop shipping.

**One caveat the PR must not misstate:** these snapshots are *hand-assembled from audited
sources and committed*, not script-regenerated — there is no committed generator that writes
`snapshots/*.json`, only source parsers (`aws_sources.py`, `claude_sources.py`) and a runtime
candidate assembler (`refresh.py:46-84`). So `pricing_policy` is the precedent for **versioning,
fail-closed loading and CI drift-gating**, not for generation.

*The generation-and-cross-language-parity half — the control-envelope vectors:*

- A committed generator: `modules/gateway/tests/agentauth/generate_envelope_vectors.py`, whose
  docstring names what is and is not committed and gives the reproduce command.
- A committed artifact that crosses the same Python→TypeScript boundary this story must cross:
  `modules/agent-factory/agent/src/__fixtures__/control-envelope-vectors.json`, carrying a
  `version` field stamped from `ENVELOPE_VERSION`.
- A **version-equality gate on the consuming side** — `agent/src/control-envelope.test.ts:76-80`
  asserts `fixture.version === ENVELOPE_VERSION`, with the comment that a one-sided version bump
  would invalidate every vector. This is exactly the "refuse on version mismatch" row above,
  already implemented once in this repo.
- A staleness message that tells the next engineer what to do:
  `tests/agentauth/test_envelope_vectors.py:54-57` — "It is committed, not generated at test
  time — regenerate with … and commit the result so the TypeScript suite verifies the same
  bytes."
- CI puts the fixture, the verifier, the test **and the generator** on the trigger paths
  (`agent-control-ci.yml:119-122`), and requires the parity test to exist by name (`:263-270`).

The recommendation is therefore: generate the catalogue with a committed script (envelope-vector
pattern), stamp and load it with `pricing_policy`'s two-constant fail-closed discipline, and gate
both on CI paths. Nothing here is new machinery.

**Packaging is already solved for the webhook side.** `package-lambdas.sh:84` zips `common/`
into every Lambda bundle, so a generated catalogue placed under `lambda/common/` ships with no
new pipeline. This removes the first draft's concern that the gateway module boundary
(`dispatch_pass.py:100-107`'s `ImportError` ruling) forces two behavioural copies: the *data*
crosses the boundary, the *authority* does not.

**Where this shape applies, and where it must not be carried over.** Local pre-validation plus
downstream authoritative resolution is the ruled shape for the **webhook and engine** paths, and
§4.2 is the latency evidence for it. Its safety rests on a precondition that must be stated
explicitly, because this note previously failed to: **something authoritative selects afterwards.**
The worker satisfies that — the SDK is pointed at a local proxy that re-signs to API Gateway
(`entrypoint.py:2188-2189`), so the gateway is genuinely downstream.

**On the ARC path the precondition does not hold and this shape is forbidden.** The harness invokes
Bedrock directly (`CLAUDE_CODE_USE_BEDROCK: "1"` with no base-URL override in all nine workflows;
`bedrock:InvokeModel` on `Resource = "*"`), so there is no later authoritative step and a refuse-only
catalogue check would enforce nothing while appearing to. The fourth-pass ruling requires a
gateway-authoritative decision **before the harness step** there — §0.0d for the refutation, §0.6 for
the mechanism. Do not generalise this section past webhook and engine.

### 4.2 🟠 The latency constraint is real, and flag-dependent

The issue calls it settled that there is **no synchronous gateway HTTP call** from the webhook
Lambda. That is true of the *default* configuration but not of every configuration, and the
distinction matters for AC-09. Two synchronous gateway round trips exist on the dispatch path,
each behind its own flag:

| Call | Site | Gate | Deployed default |
|---|---|---|---|
| `admit_issue_work(envelope)` — SigV4 HTTPS, **10s timeout** (`gateway_client.py:376`), fail-closed ("Work ownership admission refused; nothing published") | `sqs_publisher.py:52-64` | `ADP_WORK_CLAIMS_ENABLED` **and** `AGENT_AUTHORITY_ENABLED` | **off** — both default `"false"` (§4.3) |
| `resolve_user_by_identity("github", …)` — Postgres cross-validation (#702) | `identity_resolver.py:465` | `RESOLVE_CANONICAL_VIA_GATEWAY` | **on** — `lambdas.tf:109` sets `"true"` |

So the accurate statement is narrower than either the issue's or a flat contradiction of it:
**the dispatch path already makes one synchronous gateway call in the deployed configuration
(identity cross-validation), and a second fail-closed one once the authority flags flip.** The
10-second budget is therefore already partly consumed today, and will be consumed further by a
change this story does not control. This story has less headroom than a green field and must
add none.

**AC-09 must be restated accordingly:** measure the dispatch path with `ADP_WORK_CLAIMS_ENABLED`
both on and off — the "on" case is the future worst case, not today's — and state the added
resolution latency against the *remaining* budget in each. Do not restate the issue's claim as
unqualified fact, and do not invert it: the qualifier is the flag state.

**Why this evidence decides the S1 implementation choice.** S1 makes the gateway authoritative
but does not say the webhook must call it *synchronously on the dispatch path*. These numbers
argue it must not: in the authority-flags-on configuration the path already spends up to 10
seconds on one fail-closed gateway round trip inside a 10-second GitHub budget, plus a second
identity call that is on by default today. Adding a third blocking call would make model
resolution a dispatch-availability risk — turning a preference feature into an outage class,
which is the same reasoning #2279 ruling 4 used to permit a second alias map in the first place.

The shape that satisfies both S1 and the budget **on the webhook path**: the webhook
**pre-validates locally** against the generated catalogue (§4.1a) and publishes; the gateway
**resolves authoritatively** at invocation time, where it is already in the request path and costs
no additional round trip. The local step can refuse early and cheaply; it cannot select. AC-09
should therefore assert that **no new synchronous gateway call was added to the webhook dispatch
path** — correct and checkable under S1.

**This reasoning is path-specific and the fourth-pass ruling rejected its extension to ARC.** The
latency argument presumes an authoritative resolution exists downstream to defer to; on ARC none
does (§0.0d). There the ruling accepts a new synchronous dependency precisely because the
alternative enforces nothing, so AC-09's assertion must be scoped to webhook rather than stated
globally (§10). The trade the ruling makes — CI availability in exchange for an enforceable
preference — is recorded in §0.6, including the fail-closed consequence it implies.

### 4.3 D5 signing — the primitive exists, and U4 settles the TTL by making verification per-hop

D5 requires the snapshot be gateway-signed, with workers holding only verification material
and no shared secret. **That primitive already exists and is the right thing to reuse:**
`gateway/src/agentauth/envelope.py` is Ed25519, gateway-held private key
(`AGENT_CONTROL_ENVELOPE_SIGNING_KEY`, `:88`), `alg` allowlisted to exactly one value (`:80`),
with an explicit rationale at `:17-25` for choosing asymmetric over the existing HMAC —
"the lineage-marker HMAC key is readable by workers… any authority whose key a worker holds
is an authority a worker can forge". That is D5's reasoning, already implemented and shipped.

**The TTL "mismatch" is settled, and the previous revision had it backwards.** That revision argued
that `MAX_ENVELOPE_TTL_SECONDS = 30` (`:95`) "cannot carry a chain snapshot across a multi-hour run"
and that PMM-06 must therefore settle expiry semantics. **U4 retains the 30-second maximum
deliberately**, and the reason dissolves the objection: the assertion is **not** a chain-lifetime
token. It is reissued **per hop**, and it does not carry the policy — it carries a `body_digest` that
equals the `snapshot_digest` of a record persisted in a **worker-unwritable** store. A multi-hour run
is a sequence of short-lived assertions over a durable record, so nothing needs to survive 30 seconds.

This is the correct outcome for the reason `envelope.py:40-55` already gives: the short window is
what makes the absence of instant revocation tolerable, because "the gateway re-checks live
authorization on every request." Lengthening the TTL to span a run would have traded that away, and
would have created precisely the long-lived offline-verifiable policy token U4 prohibits.

**What PMM-07 must consume, and the one extension U4 requires.** The per-hop assertion shape U4
describes is not quite what bootstrap returns today:

- `ENVELOPE_VERSION = "adpe1"` (`envelope.py:74`) is Ed25519 (`ALLOWED_ALGORITHMS = {"ed25519"}`,
  `:78`), TTL-clamped by the signer via `min(int(ttl_seconds), MAX_ENVELOPE_TTL_SECONDS)` (`:253`),
  with audience checking at `:346-347` — the right primitive.
- But `POST /internal/v1/agent/bootstrap` returns an **`adpr1`** credential
  (`run_credential.py:61`, format `:19-25`), which is **HMAC-SHA256 and symmetric** — not the
  Ed25519 assertion U4 specifies — and the `adpe1` signer has **no `audience` parameter and no
  chain-binding claim**. So U4 mandates an **extension** of both the bootstrap response and the
  envelope claim set to carry `body_digest`. PMM-06's head names this "a change to a security
  contract, not a no-op," and PMM-07 must not assume it as present.
- **The freeze/live split PMM-07 depends on:** the snapshot supplies *which* model was chosen for the
  chain; it never supplies *whether* that model is still permitted. Permission is re-evaluated live at
  every hop. This is why §4.5's admission gates are not snapshot-cacheable and why a valid assertion
  is never sufficient grounds to proceed.
- **Mandatory bootstrap, no envelope-copy fallback.** A hop that has not bootstrapped has no
  verified snapshot and must refuse — it must not fall back to a class default, and it must not
  proceed on the envelope's copy of the policy. The live precedent is `agentauth/model_identity.py`,
  which calls `runtime.authenticate(...)` **twice** (pre- and post-upload) because
  "reauthentication re-proves the assignment against live SQL, so this later result supersedes the
  pre-upload one: it is the state immediately before spending" — and which rejects caller assertions
  outright, raising `BootstrapRefusedError("model run assertion mismatch")` when `X-Agent-RunId` or
  `X-Agent-OrgId` disagree with the authenticated caller. That is U4's rule, already shipped, in the
  module closest to this story's concern.

One mismatch does remain, and it is not about the TTL:

1. **Both authority flags default `false`** (`gateway-deploy.yml:356-357`,
   `webhook-ingress/infra/lambdas.tf:100`, SSM-sourced with `"false"` fallback). D5 gates
   snapshot *enforcement* on the gateway-mediated authority path being live and accepted, and
   names #3186 and #5195 as prerequisites — both **open**. So enforcement is not flippable
   today. Report-only is unaffected.

### 4.4 Precedence, stated once

1. **Direct one-run override** — this hop only, never inherited, never persisted (D2).
2. **Snapshot's persona mapping** for the trusted root principal (D1, D5).
3. **The default for this run's compatibility class** — only when no mapping row exists
   (D4 as amended by S6). Not "the canonical default": the lookup is class-keyed, and a missing
   entry for the run's own class is a refusal, never another class's value.

Then, as admission gates and **not** precedence rungs: catalogue membership and tenant
allowlist (D3), harness compatibility (D6), destination invocability, budget, rate limits.
A gate refusal fails actionably; it never re-resolves to a different model.

**Repository and project committed settings are not a rung at all (S4).** A repo-committed file
— `AGENTS.md`, a project settings file, a workflow `env:` — may **constrain admission** (narrow
what this repository permits) but may **never override a root principal's mapping**. The reason
is the same one D1 used for organization rules, and it is stronger here: a committed file is
editable by anyone with write access to the repository, so letting it select a model would let a
repository collaborator redirect another person's spend. Concretely, this means the nine
workflow-level `env: ANTHROPIC_MODEL` literals (§2.2a) are *not* a legitimate selection
mechanism that the resolver must honour — they are inventory to be reconciled, which is why S2
puts the path in scope and PMM-09 owns the literal cleanup.

This also settles the narrower case the first draft raised as Q5: a project-level committed
preference cannot beat a principal mapping. It can only refuse.

### 4.5 Fail-closed, stated precisely

| Situation | Behaviour | Locked by |
|---|---|---|
| No mapping row for the persona | The **run's compatibility-class default**, `source=class-default` | D4 + S6 |
| Mapping row valid | Use it, `source=principal-mapping` | D1 |
| Mapping names unknown / retired / disallowed / incompatible | Refuse before billable work, naming the model and the reason | D1, D2, D6 |
| Direct override invalid | Refuse this hop actionably; do **not** substitute the default | D2 |
| **No default registered for the run's compatibility class** | Refuse as a platform-readiness error naming the class. **Never** substitute another class's default | **S6, #5433** |
| The class default is registered but unavailable at the destination | Platform-readiness error, loud; no substitution, and no cross-family fallback | D4, S6 |
| Snapshot missing / expired / wrong audience / tampered / cross-tenant | Refuse; no reconstruction | D5 |
| Store or catalogue unreachable | PMM-06's bounded cache contract; never invent a model | epic |
| Local generated catalogue version incompatible with the authority | Refuse; do not resolve locally (§4.1a) | S1 |
| Persona absent from snapshot | Snapshot's default **for that persona's class only** if the persona is valid in the signed catalogue; else compatibility error | epic, S6 |
| Repository/project committed setting names a different model | Ignored for selection; may only narrow admission (§4.4) | **S4** |

Two further rows follow from U3, and they are the ones that make the posture itself fail closed
rather than fail open into enforcement:

| Situation | Behaviour | Locked by |
|---|---|---|
| **Runtime posture revision unknown or unparseable** | **Refuse.** Do not assume report-only and do not assume enforcing (§4.7) | **U3** |
| **Posture cache stale beyond its bound and the authority is unreachable** | Refuse per §4.7's bound; never extend the cache to keep serving | **U3** |

The two rows S6 adds are the ones that carry #5433's permanent compatibility contract into
code. A `gpt-*` persona whose class has no registered default must fail visibly, and the failure
must name the class — "no default registered for compatibility class `codex-sdk`" is actionable,
whereas silently running the Claude default is the exact outcome #5433 prohibits and would be
invisible in every audit record, since the resolved value would look entirely legitimate.

**Class identifiers are `claude-agent-sdk` and `codex-sdk` (U2).** The previous revision wrote
`codex-gpt`, which this note invented; U2 fixes the vocabulary and PMM-03 owns the registry. Two
properties of the identifiers matter to PMM-07 and must not be blurred: the class ID is **stable and
unversioned**, while the harness/contract revision is a **separate versioned field**. A resolver that
folded a revision into the class key would make every harness upgrade look like a new, defaultless
class — turning a routine version bump into the platform-readiness refusal above.

**The report-only caveat that must be written into the PR:** in report-only posture *none* of
these refusals actually refuse. They are recorded as "would have refused". Anyone reading a
report-only deployment as evidence that fail-closed works is reading it wrong. Only PMM-09's
flip makes these live.

### 4.6 D6 harness compatibility — verified premise, and S6's class keying

D6's factual basis holds: `agent/package.json:14` pins `"@anthropic-ai/claude-agent-sdk": "0.3.220"`,
a single pinned Claude harness for direct persona execution. So restricting the selectable set
to compatible Anthropic Claude models is a statement about the shipped harness, not a
provider preference. The resolver must therefore take the harness identifier from the snapshot
and refuse a model the harness has no registered compatibility contract for — and, per D6,
**each hop is checked against that hop's own harness**, which matters because `codex` runs a
Claude outer agent with Codex as a delegated tool (`codex.md:12-17`).

**S6 adds that compatibility class is also the key for the default lookup**, not only an
admission gate. The distinction matters for a case D6 alone leaves underspecified: a mapping
that names an incompatible model is a *gate refusal*, but an **absent** mapping under a
single-valued default silently produces a model from the wrong family. Gate 1 would then catch
it — so the run fails rather than cross-invoking — but it fails reported as a *platform-readiness
error* when the real cause is a missing class default. Class-keying the default makes the
diagnosis right at the point of resolution.

**The mixed-harness chain case must be settled by PMM-06, not assumed here.** #5433 permits a
chain whose hops run on different harnesses. If the snapshot freezes one default identifier,
a Claude-rooted chain that dispatches a `gpt-*` child hop freezes only the Claude default and
hands the GPT hop an Anthropic identifier, which gate 1 then refuses. **The snapshot must
therefore carry class-keyed defaults, not a single `system_default` field.** PMM-07 consumes
whatever PMM-06 defines, so this is a constraint PMM-07 places on #5424 and must not paper
over: a snapshot exposing a singular default cannot satisfy S6, and PMM-07 should refuse to
consume one.

### 4.7 U3: the versioned runtime posture, read at request time, bounded cache, fail closed

U3 is the one second-pass ruling that adds a mechanism rather than correcting a position. It requires
that the report-only vs enforcing posture be **versioned**, **readable by the gateway at request
time**, **cached with a bound**, and **fail closed on an unknown revision**. PMM-09's S2 assigns the
mechanism to PMM-02/PMM-07: PMM-02 carries the `enforcement_posture` column, and **no note describes
a request-time read path** — that gap is PMM-07's to close, because PMM-07 is the story with a
request-time decision to make.

**First, what "report-only" must mean — exactly one thing:** *no selection decision changes
behaviour.* The resolver computes, records and explains; nothing it returns alters which model the run
actually uses. It is not "refusals are logged but admission still applies", nor "enforcing for some
paths". The in-repo precedent for the posture flag itself is `shared/config.py:239-241`
(`bedrock_routing_shadow_mode: bool = True`) — a real shadow posture that defaults safe.

**`runtime_posture` does not exist in the tree** (zero hits). Rather than invent a mechanism, this
contract composes three working precedents, each supplying one half of U3:

| U3 requirement | Precedent to follow | What it already does |
|---|---|---|
| Bounded cache over an authoritative record | `V2RateCache` (`pricing_policy/storage.py:223-437`) — "Last-known-good cache over the active generation. One instance per process." | TTL and backoff as named constants (`:71-76`: `SCHEMA_REPROBE_SECONDS = 60.0`, `RATE_CACHE_TTL_SECONDS = 900.0`, `RETRY_MIN_SECONDS = 30.0`, `RETRY_MAX_SECONDS = 900.0`); refresh decision in one place (`needs_refresh`, `:264-303`); **monotonic** clock so "an NTP correction cannot make the cache look fresh for hours" (`:243-258`) |
| Reject a *regressing* revision | same module, `:370-386` | An older revision arriving after a newer one is refused rather than applied — the case a plain TTL misses entirely |
| Fail closed when the revision changes under a decision | `_pricing_revision` / `binds()` (`orchestration/provider_quotes.py:317-324`, `:240-254`) | Composes a revision string from its inputs and returns `QuoteRefusal(REQUEST_CHANGED, "pricing revision changed after quote")`. The governing sentence is `:65`: "A lapsed budget is a refusal, never a pass" |
| Compare-and-set discipline for the caller | `OutcomeKind.STALE` / `expected_revision` (`orchestration/execution_state.py:213-232`, `:418-443`) | "the caller's revision is not the row's… The caller must re-read, never re-apply", and deliberately offers no "retry with the current revision" convenience |

**The contract PMM-07 requires, stated so it is checkable:**

1. The posture is an **in-force versioned record**, tenant- and flow-scoped, read from the same
   authority class as `load_in_force_policy` (`orchestration/policy_admission.py:163-193`), which
   already fails closed for the analogous reason: "Removing an accepted policy withdraws authority. It
   must not turn old workers or the next dispatch into an unrestricted legacy flow."
2. Every resolution **records the `posture_revision` it used** (§4.1). A decision that cannot name its
   posture revision is not auditable. **Correction to the previous revision:** it said "cost
   attribution in PMM-08 inherits that record", which overstated a sibling's commitment — `posture`
   is a **zero-hit grep** in PMM-08's head `88efc5fe`, and its attribution columns (§3, §5.1) carry
   no posture field. This is therefore a **request to PMM-08**, not an inherited fact, and PMM-07
   must not assume the column exists downstream. Note also how PMM-08 consumes this story: by
   **calling PMM-07's resolver** (`5426…:401`, `:988`, `:992` — "it must call their resolution, not
   copy it") and from three protected sources only (`:314-324`), which explicitly exclude worker-side
   request/body values. So `ADP_MODEL_REQUESTED`/`ADP_MODEL_RESOLVED` are the **#2293 requester-feedback
   channel** (§6.2) and are **not** an attribution channel for PMM-08.
3. **Unknown or unparseable revision → refuse.** Not "assume report-only." Assuming report-only sounds
   safe and is not: it would silently disable an *enforcing* deployment, which is the failure mode
   where a resolver defect becomes a spend incident with no signal.
4. **The cache bound is explicit, monotonic, and short**, following `V2RateCache`. A stale posture
   beyond the bound with an unreachable authority is a refusal, never an extension.
5. **A revision that changes mid-decision invalidates the decision**, per `binds()`. The resolver must
   not select under revision *N* and admit under *N+1*.
6. **The posture governs selection only.** It never relaxes an admission gate. A report-only posture
   with a live tenant-allowlist refusal is still a refusal — report-only means the *selection* did not
   change behaviour, not that admission was skipped. Conflating the two would make the shadow posture a
   security bypass.

**Ordering note for §8:** the posture read path must ship *before* any enforcing flip, and PMM-09
owns the flip. Shipping the flip against an unversioned or unread posture would give the platform no
way to answer "which posture was in force when this run chose this model" — retroactively
unanswerable, which is the property that makes an incident unreviewable.

---

## 5. 🔴 D4's default names no model-deciding path, the gate is stale, and S6 makes it class-scoped

**S6 reframes this section.** D4's `us.anthropic.claude-sonnet-4-6` is not "the canonical
default" — it is the **Claude compatibility class's candidate, pending live invocability proof**.
Everything below still holds as evidence; what changes is its scope. Two consequences to carry:

- A deploy gate that pins only this identifier gates only the Claude class. Under S6 the gate
  must cover **every class that has a registered default**, and §2.3b shows a second family is
  already live in the image (`codex-config.toml:25`, `openai.gpt-5.6-sol`) with no gate at all —
  `enable-bedrock-models.sh`'s `REQUIRED_MODELS` contains no `openai.*` entry.
- "Pending live proof" is not satisfied by anything in this repository. Per #2300's lesson, a
  listing is not invocability. PMM-03 owns the bounded probe; PMM-07 must not record the Claude
  candidate as proven, and AC-11 must assert the gate, not the invocation.

A repo-wide sweep at `67db0294` finds that exact
string in **7 files, but in no code path that decides a persona's model**: one Terraform
default (below), two docs, one module README, and three pricing/test fixtures
(`pricing_policy/snapshots/2026-09-12.2.json`, `tests/lambda/test_cache_token_pricing.py`,
`tests/pricing_policy/fixtures/.../model-card-anthropic-claude-sonnet-4-6.md`). Every
model-deciding literal uses `global.anthropic.claude-sonnet-4-6` (the worker chat
config, LCM summarizer, agent-context, DeepWiki, LiteLLM) except
`modules/agent-factory/infra/gateway-main.tf:477`, which sets `us.anthropic.claude-sonnet-4-6`
— matching D4's prefix, for exactly D4's stated reason (`:467-476`: `global.*` returns
"invalid model identifier" on some accounts). So D4's `us.`-over-`global.` ruling is already
validated by deployed Terraform, but no code path names it.

The deploy gate is separately stale. `enable-bedrock-models.sh:36-43`:

```bash
# Keep in sync with:
#   modules/agent-factory/agent-worker-image/entrypoint.py (ANTHROPIC_MODEL)
#   modules/agent-factory/agent/k8s/chat-scaledjob.yaml    (ANTHROPIC_MODEL, LCM_SUMMARY_MODEL)
REQUIRED_MODELS=(
  "anthropic.claude-opus-4-6-v1"
  "anthropic.claude-sonnet-4-6"
)
```

The comment names `entrypoint.py` as the thing to stay in sync with; `entrypoint.py:1578`
defaults to **opus-5**, which is not in the list. `preflight-check.sh:224` has the same drift.
The bare (unprefixed) ids are correct and deliberate — `normalize()` at `:53-58` strips
`global./us./eu./apac.` because the agreement APIs reject profile prefixes — so D4's `us.`
default normalizes to the `anthropic.claude-sonnet-4-6` already present.

**So AC-11 is satisfiable today for Sonnet 4.6 specifically**, and the PR can assert that with
the `normalize()` citation. But the gate does not enforce the default any path actually uses,
and the file's own header (`:5-9`) says it exists to prevent exactly that: a missing agreement
that the SDK reports as "no changes needed" and that surfaces later as `AccessDeniedException`.
A mitigating detail the implementer should know: with no arguments the script also discovers
all ACTIVE Anthropic models and best-effort enables them (`:66-71`), so opus-5 usually gets
enabled — just never *enforced*.

**Recommended direction:** PMM-07 asserts that the Claude-class default is gated via
`normalize()` (it is present in normalized form — assert and cite, do not blindly add a
duplicate), files the opus-5 gate gap to PMM-09 with #4673, which already owns the unsafe-default
correction, and — new under S6 — records that **no gate exists for the Codex/GPT class default**
so the class-keyed gate requirement is visible before PMM-09 ships the flip.

---

## 6. Worker-side consumption

### 6.1 Two assignments, and the one that matters

The issue targets `ConfigLoader.setupBedrockEnv` for AC-04. That is a real second assignment —
`ConfigLoader.ts:28-32` writes `process.env.ANTHROPIC_MODEL = this.config.bedrockModel` where
`bedrockModel` is `process.env.ANTHROPIC_MODEL || 'global.anthropic.claude-opus-5'` (`:19`).
When the env var is set, the round trip is harmless; when unset, it re-materializes the
hard-coded default after the resolved value was already recorded.

**But the pod does not run that code.** `entrypoint.py:65` sets
`AGENT_BINARY = "/app/dist/agent-worker.js"` and execs it at `:2269`. `ConfigLoader` is
reachable only through `index.js` → `AgentService.ts:20`. The model the worker actually uses
is `agent-worker.ts:127`: `const MODEL = process.env.ANTHROPIC_MODEL || 'global.anthropic.claude-opus-5'`,
consumed at `:1378` and `:1529`.

**AC-04 as written is a trap.** A jest test proving `setupBedrockEnv` no longer overwrites a
resolved value can pass in full while the executed path is untouched. AC-04 must assert on
`agent-worker.ts`'s `MODEL` and on the value `entrypoint.py` puts in the child environment,
with `ConfigLoader` as a secondary assertion.

### 6.2 S3 — #2293's requester feedback ships in this story

**S3 rules that #2293's actionable requester feedback ships in PMM-07, and that D2 cannot reach
enforcement without it.** The first draft argued the opposite — that report-only could ship here
and PMM-09 should own the feedback path. That recommendation is superseded: the channel is now
in-scope work with a completion boundary inside this story.

The half-built wiring the implementer inherits: `entrypoint.py:1685,1687` writes
`ADP_MODEL_REQUESTED` and `ADP_MODEL_RESOLVED` into the worker's child environment. A repo-wide
grep finds **only those two write sites** — no TypeScript, no Python, no test, no doc consumes
them. The comment immediately above states the intent (post a warning if the requested model was
rejected); the warning was never implemented. That is #2293, open since #2279.

**Build on those two variables rather than inventing a third channel.** They already cross the
boundary that matters — resolution happens Lambda-side, the user-visible surface is worker-side —
and they are already populated on the one path that passes model fields. What S3 requires added:

- A consumer in the worker that posts the rejection where the requester will see it, naming the
  rejected request, the reason, and the permitted alternatives (D2's three required elements).
- Coverage for the paths that pass **no** model arguments today (`agent_trigger.py:374`,
  `eventbridge/handler.py:237`), since a refusal on an agent-to-agent hop has no requester
  comment to answer on. The feedback target for a machine-rooted hop needs stating; the run's own
  status surface is the candidate, not a GitHub comment.
- A test that fails if the variables regain zero consumers, because "written and never read" is
  precisely how this channel stayed dead for two releases, and is the #4511 inert-config class.

**Sequencing consequence.** The first draft's headline blocker ("P3 blocks the D2 flip, PMM-09
must own it") is resolved by assignment: this story owns it, so the D2 flip's dependency is
satisfied inside PMM-07 rather than deferred. Report-only remains the *deployment* posture (§8),
and PMM-09 still owns the enforcing flip — but the feedback path is no longer the thing standing
between them.

---

## 7. Security and tenant boundaries

The resolver is a pure function over inputs **this process verified on this hop**, so its boundary
obligations are mostly about what it must refuse to trust:

- **Verify per hop, in this process, against the protected record (U4).** The previous revision said
  "the caller verifies the snapshot signature, expiry, audience and chain binding (D5) and passes a
  verified object." **U4 overturns that**: no caller may assert it already verified the snapshot. Each
  hop re-verifies against the worker-unwritable authority record — assertion signature, expiry,
  audience, chain binding, **and `body_digest` equality with the persisted `snapshot_digest`** — behind
  the mandatory bootstrap gate. The `VerifiedSnapshot` type of §4.1 exists so this is enforced by
  construction rather than by convention; a resolver that accepts a caller's word for verification
  *is* the bypass, and "the caller verifies" is how that bypass gets written without anyone choosing it.
- **The bootstrap binding is mandatory and has no fallback.** A hop with no bootstrap has no verified
  snapshot and refuses. It does not read the envelope's copy of the policy and does not fall back to a
  class default. The immutable pod→invocation binding is already implemented this way
  (`agentauth/bootstrap.py:329` `attribute_not_exists(pk)`, conditional `pending → active` at
  `:334-337`, post-commit re-read at `:359-367`), including reading tenant **from the dispatch row
  rather than the request** (`:307`) — which is the same "never from the caller" rule applied to tenancy.
- **Refusals must be indistinguishable where they carry information.** Bootstrap already returns
  `404` for refusals that must not be distinguished and `503` for store errors (`:294-301`), and
  `model_identity.py` maps identity refusals to `403 worker_identity_refused` and store failures to
  `503 worker_identity_unavailable`. A resolution refusal must not become an oracle that reveals
  another tenant's registered principals or class defaults.
- **Nothing agent-writable is authoritative.** `correlation_store.py`'s established rule —
  the agent pod holds write access to the pointer table, so "anything the pod can write is
  attacker-controlled from the platform's point of view" — applies directly. Model policy must
  not be read from any store the pod can write.
- **Tenant scoping is the snapshot's**, and the resolver must not accept a tenant argument that
  could disagree with the snapshot's bound tenant. One source, not two.
- **No new IAM.** The resolver reads no AWS resource. If an implementation finds itself needing
  a new grant, the design has drifted into PMM-06's territory — stop and re-scope.
- **The `/model` directive comes from mutable GitHub comment text.** It is an input to be
  validated, never an identity claim. Root-principal identity stays in protected lineage
  fields per the epic.

---

## 8. Deployment, migration and rollback

Three artifacts move, built by three pipelines. S1 changes **what each one contains** — one
authority plus two data consumers, not three resolver copies:

| Artifact | Pipeline | Contains under S1 |
|---|---|---|
| **Gateway** | `gateway-deploy.yml` | **The authority.** The only code that selects a model: resolver, precedence, class-default registry, proxy path, engine path, **per-hop snapshot verification (U4, §4.3) and the runtime-posture read path (U3, §4.7)** |
| Webhook-ingress Lambda | `webhook-ingress/scripts/deploy-webhook-ingress.sh` (**not** covered by `deploy-all.sh`) | The generated catalogue **as data** under `lambda/common/` + pre-validation call sites in the three adapters and GitLab. No selection logic |
| Agent worker image | its own build | The same catalogue as data + `entrypoint.py` consumption of resolved fields and the #2293 feedback emitter (§6.2). No selection logic |
| **The nine ARC persona workflows** | **no build — merged YAML takes effect immediately** | **A pre-harness step that obtains the gateway-authoritative decision over SigV4 and passes it into the harness step's `env:` (§0.6).** Added by the fourth-pass ruling; the previous revision omitted this row because it assumed local pre-validation needed no deployment change. **Its risk profile is unlike the other three:** there is no image to roll back to and no flag — a merged workflow edit applies to the next run, so a defect here fails agent runs immediately. Gate it behind report-only (§4.7) and stage it on one workflow before the remaining eight |

A fourth artifact is implied and must be named: **the generator and the catalogue it writes**.
Per §4.1a it is a committed script plus a committed version-stamped data file, and both belong on
CI path triggers — `gateway-ci.yml` for the generator's source of truth, and the webhook/worker CI
for the copies, following `agent-control-ci.yml:119-122`, which puts fixture, verifier, test and
generator all on the trigger paths.

**Ordering:** worker image first, then gateway, then Lambda. Rationale: the worker change is
backward-compatible with envelopes that carry no new fields, so a new worker on old envelopes
behaves exactly as today. Deploying the Lambda first would emit fields no worker reads — harmless
in report-only, but it makes the mixed-version window unobservable in the direction that matters.
A mixed-version fleet is the normal transient state and the PR must say so.

**Rollback:** each artifact reverts independently. In report-only posture revert restores current
behaviour exactly, because the resolver's output is recorded and not consumed. The PR must state
this as a checkable claim, not a reassurance — i.e. name the posture revision and the flag whose
`false` value makes it true. Under U3 this claim gets sharper and must be stated in its sharper
form: report-only means **no selection decision changed behaviour**, and the recorded
`posture_revision` on each resolution is the evidence. A rollback story that cannot name the posture
in force during the reverted window cannot substantiate the claim.

**Posture ordering (U3).** The posture read path ships **before** any enforcing flip, and PMM-09 owns
the flip. An unknown posture revision refuses rather than defaulting to report-only (§4.7), so the
posture record must exist and be readable in every environment *before* the resolver's call sites go
live — otherwise a correct fail-closed implementation refuses every resolution in an environment whose
posture was never seeded. This is a deployment-ordering hazard, not a design defect: seed the record,
then deploy the readers.

**Envelope size:** `sqs_publisher.py:111-115` enforces the 256 KB SQS limit, and
`_truncate_payload` (`:118-121`) drops **only** `payload`. A snapshot added at the top level is
therefore never truncated — good — but it also cannot be sacrificed to fit. PMM-06 owns snapshot
size; PMM-07 must not assume unlimited headroom.

---

## 9. What can run in parallel

### 9.1 Can start now, before #5424 lands

- The inventory sweep and AC-01's enforcing test — including the S5 onboarding-template rows
  (§2.3a) and the S6 second-harness rows (§2.3b). This is the highest-value, lowest-dependency
  piece and it is what makes the rest honest.
- The **gateway-side authority** and its precedence unit tests against a stub snapshot type
  (S1: one place, in the gateway — not a library shell vendored twice).
- The **catalogue generator and its version stamp**, plus the consuming-side version-equality
  gate and CI drift test (§4.1a). Independent of the snapshot's contents.
- Map convergence (§3.1) and the `model_aliases` disposition (§3.2).
- **The runtime-posture read path (§4.7)** — its bounded cache, revision-regression rejection and
  fail-closed unknown-revision behaviour are testable against a stub record before PMM-02's column
  lands, since the three precedents it composes are all already in the tree.
- The `enable-bedrock-models.sh` / `preflight-check.sh` assertion (§5), including the absent
  `openai.*` entry noted there.
- **#2293's feedback channel (§6.2)** — no longer conditional on an operator assignment; S3 puts
  it in this story.
- **The ARC SigV4 call mechanics** (§0.6) — the runner's IRSA identity, the `execute-api:*` grant and
  the `AWS_IAM`-authed `/agent/{proxy+}` route all exist today, so a single workflow can be proved
  end-to-end against a stub resolution response before the real surface exists. Worth doing early:
  it retires the assumption that broke this note's previous recommendation. The nine-workflow rollout
  is **not** parallel-ready — it waits on an owner and on the resolve-for-principal surface (§9.3).

### 9.2 Blocked on #5424 (PMM-06)

- Any call site that reads a real snapshot; snapshot verification; per-hop selection; AC-02,
  AC-06, AC-08 end-to-end.
- The class-keyed default contract (§4.6): PMM-07 cannot consume class defaults until the
  snapshot carries them, and per S6 it should **refuse** a snapshot offering a singular
  `system_default` rather than treat it as the Claude-class value.

### 9.3 Blocked beyond this story

- D2's enforcement flip. Note this is **no longer blocked on the feedback path** — S3 moved that
  into this story — but remains PMM-09's flip to make (S7). Under U3 it is additionally blocked on
  the posture record existing and being seeded in every environment (§8).
- **The service-rooted call sites** — EventBridge, ARC `workflow_call`, and any scheduled path —
  wait on U1's canonical service principal and its tenant-scoped alias registry. **PMM-02 owns and
  has accepted this** (§0.5), so it is a delivery-ordering dependency rather than the unowned gap the
  previous revision reported. The **human-rooted** paths are not blocked by it — though the ARC human
  arm has its own prerequisites: an actor field and tenant source (§0.4 point 1). Its credential
  question is **settled** by the fourth-pass ruling (machine authentication via the runner's existing
  IRSA identity; §0.6).
- **The ARC pre-harness resolution step, on all nine workflows.** Ruled in (§0.0d) but the
  restructure has **no owner** — PMM-09 owns residual-literal cleanup, not workflow restructuring,
  and this should be confirmed with the epic rather than assumed into PMM-09's scope. It also needs a
  resolve-for-principal surface that PMM-03/PMM-02 own under U6, since the SigV4 caller is the
  runner's machine identity while the answer concerns the human root (§0.6). **This is the reason
  §0.2's verdict is not-implementation-ready.**
- **U4's assertion extension**: `body_digest` in the per-hop assertion, and an Ed25519 assertion
  from bootstrap rather than today's symmetric `adpr1` credential. PMM-06 owns the security-contract
  change (§4.3); PMM-07 consumes it and must not shim around its absence.
- D5 snapshot enforcement (needs #3186 / #5195; both open).
- Correcting (not inventorying) the onboarding templates, default consolidation across all sites,
  and the live matrix — PMM-09.
- Any Codex-harness execution path: `#5433` is the owner. §2.3b records what already exists and
  what is still absent; PMM-07 neither builds nor blocks on it.

---

## 10. Acceptance criteria — corrections

The eleven ACs are well-formed. Six need amendment given §2 and the rulings, and the second pass adds
three more (AC-15 to AC-17) for the obligations U2, U3 and U4 introduce:

| AC | Amendment | Why |
|---|---|---|
| AC-01 | Inventory must be **generated** from a repo sweep, not transcribed, and must include the corrected workflow set (7 + 2), `chat-scaledjob.yaml:42`, `gateway-main.tf:477`, the three shipped templates (§2.3a) and `codex-config.toml:25` (§2.3b) | The issue's table undercounts ~3× (§2.3), and S5/S6 add rows it never had |
| AC-02 | Must cover **five** publish paths including GitLab and the engine, and must state per-path wired/excluded against the **per-path obligation table** in §2.2. For ARC it must assert the **U5 split** per trigger (§0.4) — canonical human root for human-initiated events, registered canonical service principal otherwise — and must assert **fail-closed-when-unregistered** rather than any fallback. **The ARC arm is now writable** (Q5′ closed): it asserts that a gateway-authoritative decision is obtained **before** the harness step over machine-authenticated SigV4, that the internal API key appears in no workflow, and that an unobtainable decision refuses the harness rather than proceeding on the step-level literal (§0.6). It must record that the service-rooted arms wait on PMM-02's registry and that the ARC arm cannot go green until the nine-workflow restructure has an owner, so a green AC-02 on webhook alone is not whole-path coverage | `spawn_persona` is not the single point (§2.2); U5 settles which principal each trigger resolves (§0.4); PMM-02 owns the registry but has not built it (§0.5); the fourth-pass ruling settles the ARC mechanism (§0.0d) |
| AC-03 | Add case-insensitivity and the unvalidated pass-through (`model_validate.py:71`) as subclaims | The divergence includes case and pass-through, not just alias width (§3.1, §2.4) |
| AC-04 | Assert on `agent-worker.ts:127` and the child env from `entrypoint.py:1594`; `ConfigLoader` secondary | The pod does not execute `ConfigLoader` (§6.1) |
| AC-09 | Measure with `ADP_WORK_CLAIMS_ENABLED` on and off; state headroom against the gateway calls already on the path in each flag state. **Scope the no-new-synchronous-call assertion to the webhook dispatch path**, where local pre-validation plus downstream authoritative resolution is the ruled shape (§4.1a, §4.2). It must **not** be stated globally: on ARC the fourth-pass ruling *requires* a new synchronous gateway call before the harness step, so a global assertion would fail the ruling by design (§0.0d, §0.6). Add an ARC-specific measurement of the pre-harness decision against the job's own startup budget | The dispatch path already makes one synchronous gateway call by default and a second when the authority flags flip (§4.2); the ARC path now has a mandated third of a different kind |
| AC-11 | Assert via `normalize()` that D4's `us.` default maps to the gated bare id, **as the Claude compatibility class's default** — not as a global canonical value; separately record the opus-5 gate gap and the absent `openai.*` gate entry | Satisfiable today, but for a non-obvious reason worth pinning; S6 makes the default class-scoped (§5) |

Six additions — three from the first pass, three from the second:

- **AC-12:** every refusal path returns a reason naming the model and the cause, and is
  distinguishable from a budget refusal (D1/AC-10 boundary, asserted as a distinct code).
- **AC-13:** the authority contains no model literal; **each compatibility class's default**
  arrives as configuration. Asserted by a test that greps the module. Amended from the first
  draft's "the canonical default" per S6 — a test written against a single default would pass
  while making the class-keyed contract unimplementable.
- **AC-14 (new, S1):** a test asserts there is exactly **one** code path that selects a model.
  The satellite catalogue is data; a satellite that can return a *selection* rather than a
  refusal fails this AC. Pair it with the version-equality gate (§4.1a) so a stale catalogue
  refuses rather than resolves.
- **AC-15 (new, U4):** a test asserts the resolver **cannot** be called with a snapshot this process
  did not verify on this hop — i.e. `VerifiedSnapshot` is unconstructable outside the verification
  step (§4.1) — and that a hop which has not bootstrapped refuses rather than falling back to the
  envelope's policy copy or a class default (§4.3). The negative case is the one that matters: a test
  that only proves the happy path would pass against the overturned caller-verified shape.
- **AC-16 (new, U3):** a test asserts an **unknown or unparseable posture revision refuses** — not
  "assumes report-only" — that a **regressing** revision is rejected (`storage.py:370-386`'s case),
  that the cache bound is monotonic, and that a revision change mid-decision invalidates the decision
  (`provider_quotes.py:240-254`'s case). Separately assert that every `Resolution` carries the
  `posture_revision` it used, and that **report-only never relaxes an admission gate** (§4.7 point 6).
- **AC-17 (new, U2):** a test asserts class IDs are the **stable, unversioned** `claude-agent-sdk`
  and `codex-sdk`, that the harness/contract revision is a **separate** field, and that no code path
  composes the two into the default-lookup key — the regression that would make every harness upgrade
  present as a defaultless class.

The issue's stated plausible-wrong-result is correct and worth keeping verbatim: *a resolver
test suite that passes while one dispatch path was never wired.* §2.2 shows there are two such
paths today, and §6.1 shows a second instance of the same hazard inside AC-04.

---

## 11. Operator decisions — all settled; what remains is unowned work

**No decision in this note is open any more.** All ten questions raised across four revisions are
settled: five by the S1–S7 synthesis, three by the U1–U6 unified rulings, one by a sibling story
taking ownership, and the last (Q5′) by the fourth-pass ARC ruling. The closed record is retained
below because an implementer reading only a live list cannot tell a decided question from an
overlooked one. **What blocks implementation is the unowned work in the final table, not a choice.**

**Six questions were open in the first draft. The synthesis rulings answered five of them.** They
are retained below as a closed record, because an implementer reading only the live list would not
know these were decided rather than overlooked.

| # | First-draft question | Resolved by | Ruling |
|---|---|---|---|
| ~~Q1~~ | Is the **orchestration engine** path in scope? | **S1 + S2** | Yes, and S1 makes it *easier* than the first draft assumed: the engine is gateway-resident, so it reaches the authority by direct call, with no vendoring (§2.2) |
| ~~Q2~~ | Is **GitLab** in scope? | **S2** | Yes — confirms the first draft's recommendation |
| ~~Q3~~ | Are the **9 ARC workflows** in or out? | **S2**, then **U5**, then the **fourth-pass ruling** | In, and now with a fully specified mechanism. S2 put them in scope without supplying a principal; **U5 supplied it** as a split by trigger (§0.4); the **fourth-pass ruling supplied the resolution shape** — a gateway-authoritative decision before the harness step, machine-authenticated, never via the internal API key (§0.0d, §0.6). The service-principal registry is owned by PMM-02 (§0.5). Nothing about this path is an open question; the nine-workflow restructure is unowned **work** (§11 final table) |
| ~~Q4~~ | Does **#2293** ship here or in PMM-09? | **S3** | Here. Overturns the first draft's recommendation (§6.2) |
| ~~Q5~~ | May committed settings override a principal's preference? | **S4** | No — confirms the first draft, and §4.4 now states the reason: a repo collaborator must not be able to redirect another person's spend |
| ~~Q6~~ | Are the **onboarding templates** PMM-09's? | **S5** | Split: PMM-07 inventories, PMM-09 corrects (§2.3a) |

**The second pass settled all three of the previously-open decisions.** They are recorded as closed,
for the same reason the S1–S7 block is retained: an implementer reading only the live list would not
know these were decided rather than overlooked.

| # | Open question (previous revision) | Settled by | Ruling, and what it overturned |
|---|---|---|---|
| ~~Q1′~~ | What principal does an **ARC-triggered** run resolve against? Previously recommended **(b)**, register ARC as a service identity | **U5** | **Split by trigger, not a single reading.** Human-initiated events (`issues:labeled`, `issue_comment`, `workflow_dispatch`) resolve the **canonical human root**; runs with no authenticated human initiator (`workflow_call`, scheduled) resolve a **registered canonical service principal** and **fail closed when unregistered**. This **overturns this note's recommendation (b)** for the human arm: U5 rules that the bot credential executing the job is exactly what must not own the preference, so the evidence the previous revision relied on (the acting credential is `adp-agent[bot]`) argued for the wrong conclusion. §0.4, §2.2a |
| ~~Q2′~~ | Does PMM-07 **refuse** a snapshot carrying a singular `system_default`? | **U2** + PMM-01 head `78787bd1` | **Refuse.** U2 fixes stable class IDs (`claude-agent-sdk`, `codex-sdk`) and forbids cross-class fallback; PMM-01's §4.1a settles that the snapshot carries **the entire class-keyed map**, and PMM-06's head is consistent. A singular field cannot satisfy the contract, so the previous recommendation is now a ruling (§4.6) |
| ~~Q3′~~ | Which class does the **Codex** default belong to, and who registers it? | **U2** | Class `codex-sdk`; **PMM-03 owns the class registry**, #5433 registers it. PMM-07 inventories only (§2.3b) and must not invent a taxonomy. Confirms the previous recommendation, with U2's vocabulary replacing this note's invented `codex-gpt` |

**Q4′ is now settled too — by a sibling story adopting this note's recommendation, not by a ruling:**

| # | Question (previous revision) | Settled by | Ruling, and what it overturned |
|---|---|---|---|
| ~~Q4′~~ | **Who builds U1's canonical service principal and its `(org_id, alias_source, alias_id)` alias registry?** Previously escalated as the note's headline blocker, "a cross-story blocker no story owns" | **PMM-02's head `e2c7d099`** | **PMM-02 owns it, and has said so.** It adopts this note's recommendation verbatim — ownership declared "no longer an open ownership question" (`5419…:62-67`) and uniqueness re-keyed to `(org_id, alias_source, alias_id)` among active rows (`:552-554`), which was the exact correction this note asked for. PMM-03 disclaims it and points at PMM-02 (`5420…:43`). **This note's supporting quotation is withdrawn**: "the largest open item; it blocks the schema" no longer exists in PMM-02 (§0.5). What remains is a **delivery dependency**, not a decision — the service-rooted arms of AC-02 wait on PMM-02 shipping; the human-rooted paths do not (§0.5, §9.3) |

**Q5′ is now settled too — by the fourth-pass ruling, which decided it *against* this note's
recommendation.** It is recorded here as closed, with the recommendation it overturned stated rather
than hidden, because an implementer who found only the ruling would not know a plausible-looking
alternative had been tried and refuted:

| # | Question (previous revisions) | Settled by | Ruling, and what it overturned |
|---|---|---|---|
| ~~Q5′~~ | **On the ARC path, does resolution happen synchronously against the gateway authority, or against the generated catalogue with authoritative resolution deferred to invocation time?** This note recommended the second option in three successive revisions | **The fourth-pass ruling** (§0.0d) | **Synchronously — a gateway-authoritative decision before the harness step, or route the job's model traffic through the gateway.** Machine-authenticated (IRSA/SigV4), applying the U5 split, and **never** by exposing the internal API key. **This overturns this note's own recommendation, and on a factual premise, not a preference:** the recommendation assumed an authoritative resolution happened later at invocation time, but all nine workflows set `CLAUDE_CODE_USE_BEDROCK: "1"` with no base-URL override and hold `bedrock:InvokeModel` on `Resource = "*"`, so the harness calls Bedrock directly and no later gateway step exists. A refuse-only local check would therefore have enforced nothing while reporting compliance (§0.0d, §0.6). The credential obstacle the recommendation leaned on also dissolves: the runner already has IRSA machine identity and `execute-api:*`, and `/agent/{proxy+}` already accepts SigV4 (§0.6) |

**Nothing in this note now awaits an operator decision.** What remains is unowned *work*, not an
open choice, and it is why §0.2's verdict is not-implementation-ready:

| Remaining work | Owner needed | Consequence while unowned |
|---|---|---|
| The nine-workflow restructure to obtain and consume a pre-harness decision (step-level `env:` → prior-step output, three distinct entrypoints) | **Unassigned.** PMM-09 owns residual-literal cleanup, not workflow restructuring — this should be confirmed with the epic rather than assumed into PMM-09's scope | AC-02's ARC arm can be *written* (§10) but cannot go green. The nine workflows stay pinned to `global.anthropic.claude-opus-4-6-v1` irrespective of any saved preference |
| A SigV4-reachable resolve-for-principal surface (the caller is the runner's machine identity; the answer is about the human root) | **PMM-03/PMM-02**, who own the API surface under U6 | ARC cannot ask the authority about anyone but itself, so U5's human arm has no mechanism |
| The ARC actor field and tenant source (§0.4) | This story, once the surface above exists | The human arm resolves no principal |

---

## 12. Supporting evidence — design coverage audit

| Spec section | Coverage | Gap |
|---|---|---|
| **Description** | Complete. Scope and completion boundary are crisp; the report-only boundary and "merging does not authorize the flip" are correctly stated | None |
| **Impact analysis** | Strong. The cost framing ("a resolver defect that routes work to a larger model is a cost incident") is right and is the reason for report-only | Does not note that two of the five dispatch paths already drop model info silently (`agent_trigger`, `eventbridge` pass no model args), so the "setting is a lie" failure is **already live** for agent-to-agent hops |
| **Design → reuse table** | Mostly accurate; line numbers drifted but claims hold | The `spawn_persona` "single point" claim is false (§2.2); the `bedrock_routing` reuse pattern is well chosen; misses the existing Ed25519 signing primitive (§4.3) |
| **Design → inventory** | Directionally right, materially incomplete | ~3× undercount; wrong workflow glob; misses `gateway-main.tf:477`, which is the **deployed** default (§2.3) |
| **Design → constraints** | Constraint 2 (model vs budget separation) is correct and matches D1 | Constraint 1 ("no synchronous gateway call") is doubly wrong: on the webhook path it holds only in the default flag state — one such call is live today and a second arrives with the authority flip (§4.2) — and on the ARC path the fourth-pass ruling now **mandates** one before the harness step (§0.0d, §0.6). It cannot be stated as a global constraint |
| **Prerequisites table** | Honest and well-structured; correctly flags D2/D4 as contested | D2 and D4 are now **locked** on #5418, so those rows are resolved; D4's identifier is further narrowed by S6 to the Claude class's candidate (§5). P2 (authority flags `false`) is new and unlisted. The 30-second TTL is **not** a gap — U4 retains it deliberately (§4.3), correcting this note's own earlier reading. Two genuinely new prerequisites are P3′ (U1's service-principal registry — **not built, but owned by PMM-02 as of its head `e2c7d099`**, so a delivery dependency rather than an unowned gap) and P4′ (U4's `body_digest` assertion extension, which PMM-06's head `f9f0ec68` records as "a build instruction, not a pending approval") — §0.3, §0.5 |
| **Deployment** | Correct on the three-artifact problem and the mixed-version window | No ordering given; §8 supplies one with rationale. S1 changes the *contents* of each artifact — one authority plus two data consumers — and implies a fourth artifact, the generator and its catalogue (§8). Envelope-size interaction unmentioned. U3 adds a **seed-before-readers** ordering hazard: a fail-closed unknown-revision posture refuses everything in an environment whose posture record was never seeded (§8). The fourth-pass ruling adds a **fifth deployable with no rollback artifact** — the nine ARC workflows, where merged YAML takes effect on the next run with no image to revert and no flag (§8) |
| **Validation** | The strongest section. AC-01's "prose-only list fails this AC" and the named plausible-wrong-result are exactly right | Six ACs need amendment and **six** are added (§10); AC-04 targets a near-dead code path; AC-13's "canonical default" wording is incompatible with S6's class keying. AC-02 additionally needs the U5 split and its fail-closed clause, since a green AC-02 on the human arm alone would report whole-path coverage it does not have |
| **Security boundary** (against U4) | The pure-function framing is right, and the agent-writable-store rule is correctly applied | The previous revision's "the caller verifies… and passes a verified object" is **overturned by U4** and is now per-hop in-process verification against the protected record, enforced by an unconstructable-outside-verification type (§4.1, §7). The closest in-repo precedent — `model_identity.py`, which re-authenticates twice and rejects mismatched caller assertions — was not cited before and should have been |
| **ARC resolution mechanism** (new under the fourth-pass ruling) | Not covered by the issue, and **actively mis-covered** by this note's three previous revisions, which recommended local pre-validation with authoritative resolution "at invocation time" — a moment that does not exist on a path whose harness calls Bedrock directly (§0.0d) | Now specified in §0.6: a gateway-authoritative decision before the harness step, machine-authenticated over the runner's existing IRSA identity and `execute-api:*` grant against the already-`AWS_IAM`-authed `/agent/{proxy+}` route, never via the internal API key. The mechanism is fully specified; **the nine-workflow restructure and the resolve-for-principal surface are unowned** (§11 final table) |
| **Runtime posture** (new under U3) | Not covered at all by the issue or the previous revision | `runtime_posture` is a zero-hit grep; §4.7 now specifies the read path, bounded cache, revision-regression rejection and fail-closed unknown revision from three in-tree precedents (`V2RateCache`, `provider_quotes.binds()`, `execution_state` compare-and-set). PMM-02 owns the record; the request-time read path was owned by no note before this one |

**Repository alignment.** The proposed shape matches existing platform conventions:
resolver-with-explainer follows `BedrockRoutingResolver`/`BedrockTarget` (`bedrock_routing.py:46-68`);
fail-closed-on-absence follows `agentauth/execution.py:31-38` ("defaulting to allow when we
cannot check would make an outage an authorization bypass"); gateway-signed/worker-verified
follows `agentauth/envelope.py:17-25`; **never-trust-a-caller's-verification follows
`agentauth/model_identity.py`** (double `runtime.authenticate(...)`, caller-assertion mismatch
refusal); **versioned posture with a bounded cache follows `pricing_policy/storage.py`'s
`V2RateCache` plus `provider_quotes.binds()` and `execution_state`'s compare-and-set fence** (§4.7);
**human-as-attribution-vs-acting-principal follows `agentauth/grants.py:98-121`** (§0.4);
**machine-authenticated gateway calls in place of a shared secret follow the #575 IRSA/SigV4
migration** (`adp_cred/__init__.py:16-21`, signing at `adp_cred/client.py:131`, repeated at nine
further call sites) — which is the precedent the fourth-pass ARC ruling relies on, and the reason
"never expose the internal API key" is the established direction here rather than a new constraint
(§0.6).
Every mechanism the rulings require therefore has an in-tree precedent — none of U1–U6 or the
fourth-pass ruling asks this story to invent a pattern, though U1's registry and U4's `body_digest`
ask other stories to build things that do not yet exist (§0.3 P3′/P4′), and the ARC restructure
needs an owner (§11).

**One convention citation is corrected from the first draft.** That draft cited the
vendored-library-plus-contract-test pattern (`stall.py` / `MAX_CHAIN_DEPTH`,
`dispatch_pass.py:100-107`) as the precedent for the resolver itself. Under S1 that citation no
longer applies to the *resolver*, because there is to be only one of those. It still applies to
the **catalogue data**, and it is joined there by two better-fitting precedents: `pricing_policy`
for versioned fail-closed loading with CI path gating, and the control-envelope vectors
(`generate_envelope_vectors.py` → `control-envelope-vectors.json` → the
`control-envelope.test.ts:76-80` version-equality gate) for a generated artifact that crosses the
same Python→TypeScript boundary. See §4.1a, including the caveat that `pricing_policy`'s snapshots
are hand-assembled rather than script-generated. No new convention is proposed, and none is needed.


## Gateway-first SDK decision rollout

The runtime obtains a fresh signed `POST /internal/v1/agent/model-decision`
response immediately before each Claude SDK launch, including retries. The
request contains a random challenge and the supported contract version; root,
tenant and persona come from the protected execution. The response signature
covers the complete proposal, including unavailable report-only proposals, and
binds the challenge, invocation and attempt. Both credential acquisition and
transport are bounded by ten seconds. A missing endpoint or unverifiable
response refuses the launch; startup telemetry is never a fallback authority.

Deploy the gateway endpoint before the corresponding worker image. Bootstrap
capability is sent in the signed `X-Adp-Model-Policy-Contract` header so the
unchanged JSON request remains compatible with old gateway schemas. This does
not make the new SDK client compatible with an old gateway: its missing endpoint
intentionally refuses inference. Roll back the worker before removing the
gateway endpoint. Keep report_only, agent_models=false, probes disabled and
budgets at zero throughout this code rollout. No candidate default is activated.

Repository setup preserves the legacy model. Only a fresh enforcing decision at
the SDK boundary may substitute it. A subsequent report_only rollback therefore
restores the original assignment, including on retries in an existing process.
Posture reads use an independent database connection and never expose pending
settings edits from the request transaction.

## External root and runtime implementation (2026-09-19)

The executable inventory is `modules/agent-factory/persona-model-runtime-inventory.json`.
The former GitLab, chat and ARC implementation gaps are wired behind explicit
rollout switches. These switches are deployment controls, not the runtime
posture: each enabled SDK launch reads the committed gateway posture afresh.
Nothing in this PR activates a default, enables probes or changes agent_models.

Chat ingest registers the authenticated human with the gateway before SQS
publication. Deployment-owned `ADP_MODEL_ROOT_BINDINGS` maps the producer role,
source, tenant and permitted personas. The response supplies the final canonical
queue bytes; the worker hashes those exact bytes. Chat admission verifies a
projected `adp-agent-bootstrap` token, the live pod/container/image and its
immutable run binding. It needs no general internal-plane registry scope. It then uses the same live selector and signed response
as the repository worker. Public Ed25519 keys are discovered only at the
configured HTTPS gateway origin. Each retry repeats admission.

GitLab protected ingress requires a JSON list in its existing webhook secret,
with a distinct high-entropy `token`, trusted HTTPS `instance` and immutable
`project_id` for each project. A global shared token cannot establish a
multi-tenant root. The gateway maps verified instance-qualified numeric GitLab
user IDs (`https://instance#123`) to canonical humans. Migration 059 adds the
provider constraint; rollback refuses existing GitLab identities without
deleting them. Root admission precedes publication and a failed admission
publishes nothing. The GitLab worker currently acknowledges and creates a
branch: it has no inference harness to claim as tested. Model policy is frozen
for that root, but this PR does not turn the spike into a model-executing agent.

All nine ARC workflows include an authority preflight and per-launch SDK guard.
`ADP_ARC_MODEL_BINDINGS` registers exact repository ID/name, workflow ref,
optional reusable-workflow ref, runner role, tenant and persona. The runner's
STS proof binds the full request, including the GitHub OIDC token and nonce.
GitHub's verified actor ID supplies a human root for direct human events;
reusable/scheduled events require an active canonical `github_actions` service
alias. Revocation, unregistered workflows, invalid OIDC and cross-run responses
refuse. No internal shared key or caller-supplied principal grants authority.

The superseded Python chat consumer uses AnthropicBedrock/boto3, not the Claude
Agent SDK. Ingest now publishes only to the TypeScript FIFO consumer. The legacy
consumer preserves its raw model under disabled/report_only, but when opted into
the chat rollout it checks committed gateway posture before either raw API call
and refuses enforcing. Drain/retire that standard queue before PMM-09; do not
record raw API success as harness evidence.

### Source configuration and operator rollout

The older "No new IAM" statement assumed a data-only local resolver and does
not describe the approved remote authority integration. The implementation
therefore includes **optional, exact-endpoint producer grants** for chat and
GitLab, plus gateway pod/job GET access in the chat namespace. No broad grant,
worker authority-table write or signing-secret access is added. This is source
preparation only: the webhook infrastructure hold remains in force, and no
Terraform/IAM apply is part of local PR closure.

Deploy the gateway and migration first. Populate reviewed producer and ARC
bindings, and the chat image-digest allowlist, through deployment SSM parameters `gateway/model-root-bindings`,
`gateway/arc-model-bindings` and `gateway/chat-authority-worker-images`. Both
gateway renderers validate and quote these into the ConfigMap; absent values
refuse unregistered callers. Review the
narrow producer/RBAC plan separately. Deploy producers and workers with their
matching registrations before enabling `ADP_CHAT_MODEL_POLICY_ENABLED` or
`ADP_GITLAB_MODEL_POLICY_ENABLED`. ARC uses repository variable
`ADP_ARC_MODEL_POLICY_ENABLED=true` and `ADP_ARC_MODEL_CONTROL_ENDPOINT` ending
`/internal/v1/agent/arc`. Chat uses `/agent/internal/v1/agent` so its existing
worker Invoke permission applies; the ingest grant is the exact POST ARN for
`/agent/internal/v1/agent/roots/admit`. The chat deployment script projects the
audience token and applies its narrow verifier Role only when opted in.

Before enforcement, verify every enabled path and retire legacy raw consumers;
a partially opted-in fleet is not enforcement readiness. A code merge and mock
invocability records are not live proof. PMM-09 still owns approved paid harness
proof, default activation and the enforcing flip. Roll back runtime posture
through the existing audited setting, wait the bounded cache window, and verify
that the original legacy SDK options return unchanged.

The existing pinned chat persona `intent-refinement` is now present in the
authoritative registry and generated catalogue. The generic skill-assisted
coding workflow uses the registered `developer` persona; it does not create a
second model namespace based on its workflow filename.
