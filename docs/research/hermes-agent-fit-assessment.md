# Hermes Agent (NousResearch) — fit assessment for ADP's per-user chief-of-staff

> **Issue**: #4161 ([Sub of #1219])
> **Date**: 2026-08-26
> **Agent**: @agent-architect
> **Parent EPIC**: #1219 (Research & exploration)
> **Related**: #1220 / #1233 / #1283 (gbrain), #4160 (DeepSeek Harness), #4162 (ORCA)
> **Subject**: `NousResearch/hermes-agent` @ `2f9e187` — MIT, Python, 236,705 stars, v0.20.5 (`v2026.8.19`, 2026-08-21)
>
> **Scope**: suitability of Hermes as the foundation for `modules/user-services/chief-of-staff/`.
> Judged against the EPIC #1219 objective: multi-day productive agents on ADP, right level of
> human-in-the-loop, high productivity, low waste, **no breakage of what works**.
>
> **Statuses**: `Yes` = code path exists upstream and would work on ADP. `Partial` = possible but
> incomplete or requires new ADP work. `No` = requires forking Hermes or violates an ADP invariant.

## Verdict: **ADAPT — harvest the design; reject Hermes as the multi-tenant product foundation**

Three findings decide this, in order of weight:

1. **Hermes declares itself single-tenant, in its own security policy.** `SECURITY.md:34-36`:
   *"Hermes Agent is a single-tenant personal agent."* And `SECURITY.md:214-217`: *"Within the
   authorized set, all callers are equally trusted. Hermes Agent does not model per-caller
   capabilities inside a single adapter. Operators who need capability separation should run
   separate agent instances with separate allowlists."* This is not an unimplemented roadmap item
   — it is the upstream trust model. ADP would not be filling a gap; it would be contradicting the
   project's stated posture, permanently, in the flagship user-facing product.

2. **Its learning loop structurally violates user-services invariant #10.** `docs/user-services-overview.md:65`:
   *"No arbitrary code execution from users. Users can configure, customize, and compose — they
   cannot submit executable code that runs on platform infra."* Hermes' headline feature is an
   autonomous background fork that writes executable Python/bash into `~/.hermes/skills/<name>/scripts/`
   which later sessions invoke without re-review (`agent/background_review.py:505-512`,
   `tools/skill_manager_tool.py:1-22`). Disabling that loop removes the reason to adopt Hermes;
   keeping it puts agent-authored code on platform infra. There is no middle setting that preserves
   both.

3. **Its state layer cannot satisfy invariants #1/#2/#4/#7 without a fork.** State is SQLite at
   `~/.hermes/state.db` (`hermes_state.py:362`), schema v26, plus a second `cron/executions.db`,
   `cron/jobs.json`, `memories/*.md`, `skills/**`, and a curator ledger. `SessionDB.__init__`
   accepts a filesystem `Path` only — no DSN, no driver seam (`hermes_state.py:4174,4358`).
   Postgres appears nowhere in the state layer. Cross-process coordination is `fcntl.flock`
   (`hermes_state_common.py:879-887`), and WAL on NFS/SMB/FUSE silently degrades to serialized
   `journal_mode=DELETE` (`hermes_state.py:646-680`), so "shared volume" is not a supported
   multi-host story. There is no `tenant_id`/`org_id` column and no ACL on any store; isolation is
   *directory-per-profile*.

**What this is not.** This is not "Hermes is low quality." It is a serious, well-engineered
project — the approval engine alone is 5,727 lines with genuine anti-obfuscation depth, and its
credential-helper design is better than ADP's own assumption (see Q3). It is the best available
reference implementation of the chief-of-staff *behaviors* ADP has specified but never built. The
recommendation is to treat it as a **design source and an optional single-user power tool**, not as
the substrate of a multi-tenant ADP product.

---

## 1. Answers to the seven questions

### Q1 — Chief-of-staff fit: what does Hermes give for multi-day continuity?

| Capability | Hermes | ADP today | Fit |
|---|---|---|---|
| Curated cross-session memory | `MEMORY.md` + `USER.md`, frozen into system prompt at session start (`tools/memory_tool.py:11-14`) | DynamoDB `adp-<env>-agent-memory`, 4 scopes, TTL by kind (`memory/dynamo-memory.ts:22-27,200-207`) | Partial |
| Self-nudged persistence | Turn-counted nudge (every 10) forks a second agent to decide what to save (`agent/turn_context.py:739-746`, `agent/background_review.py:1-17`) | **No** — nothing prompts an agent to persist | Yes (idea) |
| Skills acquisition | Agent writes SKILL.md + `scripts/` autonomously (`tools/skill_manager_tool.py:1-22`) | **No** | No (invariant 10) |
| Skill hygiene / decay | Curator: 7-day interval, stale@30d, archive@90d, never deletes (`agent/curator.py:69-77`) | personal-context synthesis CronJob decays `decay_score` (`personal_context/synthesis.py:527-535`) | Yes — ADP already has the analogue |
| Cross-session recall | FTS5 over `state.db`, **agent must choose to call it** (`tools/session_search_tool.py:26-32`) | agent-context `search`/`experience` verbs, MCP port 5100 | Partial |
| Proactive scheduling | In-process cron ticker, 60 s, at-most-once, **no retry** (`cron/__init__.py:11-16`, `cron/executions.py:3-5`) | **No agent-firing scheduler.** EventBridge→Lambda→SQS→agent transport is fully built and permissioned; only the scheduled rule is absent (`webhook-ingress/infra/eventbridge.tf:22-28`) | Partial |
| Delegation / parallelism | In-process thread-pool subagents, max 10 (`tools/delegate_tool.py:122,3951-3970`) | webhook-ingress dispatch + `POST /agent/trigger` (`lambda/github/agent_trigger.py`) | ADP's is stronger |

**Honest read of "multi-day continuity."** It is thinner than the marketing. Mechanically it is:
a ~1,300-token always-on prompt block (`memory_char_limit: 2200` + `user_char_limit: 1375` chars,
`hermes_cli/config_defaults.py:1974-1975`), plus a search tool the model must elect to call, plus
optional per-turn provider prefetch. Two specific gaps worth naming because they'd surprise an
implementer:

- Memory is a **frozen snapshot**. A fact learned at turn 5 does not enter the system prompt until
  the *next session* (`tools/memory_tool.py:11-14`). Deliberate — it preserves the prefix cache —
  but it means "remembers within a long session" is false.
- `README.md:26` advertises *"FTS5 session search with LLM summarization."* The LLM-summary path
  was **removed**; `tools/session_search_tool.py:32` states "no summary LLM path." Retrieval is raw
  BM25 message hits. Cron-sourced sessions are demoted in ranking because their repetitive
  vocabulary caused "recall blindness" (`tools/session_search_tool.py:41-57`).

**Versus the ADP design.** `docs/user-scoped-agents-design.md:225` already commits to the opposite
architecture: *"once user-scoped agents exist, chief-of-staff is 80% a template + UI, not a new
service. Same codepath, same infrastructure, new template."* Adopting Hermes means importing a
second agent runtime, a second memory system, a second scheduler, and a second approval engine —
directly against that committed position. The build order (`modules/user-services/README.md:18`)
is `vault → knowledge → agents → chief-of-staff`; vault (v1) is the only one that exists, and it
shipped *inside the gateway* (`modules/gateway/src/auth/vault_routes.py`,
`secrets_manager.py:94`). Hermes would be dropped in at v3+ on top of two services that don't
exist. **What's missing is the v2/v3 substrate, not the chief-of-staff shell.** Hermes does not
supply that substrate — it supplies its own, incompatible one.

### Q2 — Can Hermes be the front that delegates to ADP's coding agents? **Yes — and this is the strongest fit.**

Hermes is an MCP client with HTTP transport and custom headers
(`cli-config.yaml.example:1307-1320`: `url:` + `headers:`). So the delegation surface is clean and
requires **no Hermes fork and no ADP change**:

- `agent-context` MCP already speaks HTTP on port 5100 with header-based identity
  (`X-Owner-Sub`, `X-Tenant-Id`, `X-Internal-Api-Key`) — a drop-in `mcp_servers` entry.
- Issue filing / agent triggering / monitoring is GitHub API + `POST /agent/trigger`, reachable via
  a bundled GitHub MCP or a small ADP-authored MCP server.

Two real caveats:
- `POST /agent/trigger` **cannot mint a new root**: it requires `correlation_id` +
  `parent_invocation_id` resolved server-side from the webhook-events GSI, and rejects unknown
  chains with `422 unknown_chain` (`lambda/github/agent_trigger.py:1-14`). A Hermes front standing
  outside any existing chain has no lineage to present. Its viable path is **file/label a GitHub
  issue** and let the normal label-gated webhook dispatch start the chain
  (`intent_parser.py:320-343`) — which is the correct, auditable entry point anyway.
- Subagents are blocked from `cronjob` and `memory` (`tools/delegate_tool.py:52-61`), so the
  learning loop is parent-only.

This capability is worth having **independently of the chief-of-staff question**, and it is
available today with an off-the-shelf Hermes on a developer's own machine.

### Q3 — Model backend: can it run against the gateway with short-lived OAuth? **Yes on Hermes' side. Today, No on ADP's side — and the blocker is ours.**

**Hermes' side is better than the issue assumes.** `key_cmd` (`agent/command_token_source.py`) is a
purpose-built credential-helper hook whose docstring names this exact scenario — *"SSO/OIDC
brokers, cloud IAM, and internal auth proxies all issue SHORT-LIVED bearers... A key copied into
`.env` is stale within the hour"* (lines 3-7) — and explicitly cites Claude Code's `apiKeyHelper`
as the precedent (lines 18-20). It is invoked **per request** on both wire clients, TTL-cached with
60 s leeway (`_TOKEN_REFRESH_LEEWAY_SECONDS`, line 49), honors OAuth `expires_in` and absolute
`expiry`/`expiresOn`, re-mints every 900 s when no TTL is advertised, and withholds stdout/stderr
*and the command string* on failure because a `key_cmd` may embed a client secret (lines 87-97).

**ADP's existing helper already satisfies the contract byte-for-byte.**
`modules/gateway/cli/bg-cognito-auth.sh:739-774` (`cmd_token`) refreshes at T-5min and
`printf "%s"` the bare access token to stdout — exactly Hermes' documented output shape. So the
config is one line: `key_cmd: bash ~/bin/bg-cognito-auth.sh token`. No wrapper, no fork.

**But no currently-enabled gateway route preserves tool calling** — and an agent without tool
calling is not an agent. Mapping Hermes' four `api_mode` values onto the gateway:

| Hermes `api_mode` | Gateway route | Tools preserved? | Verdict |
|---|---|---|---|
| `chat_completions` | `/v1/chat/completions` (`proxy/routes.py:256`) | **No** — `openai_to_bedrock()` never copies `tools`/`tool_choice` (`format_translator.py:116-136`) | Unusable |
| `anthropic_messages` | `/v1/messages` (`proxy/routes.py:318`) | **No** — `anthropic_to_bedrock()` same defect (`format_translator.py:242-263`) | Unusable |
| `bedrock_converse` | *no Converse route exists on the gateway* | n/a | Unusable |
| `codex_responses` | `/openai/v1/responses` (`proxy/routes.py:748`) | **Yes** — body forwarded byte-for-byte (`proxy/mantle_service.py:7`) | **Only viable path** |

This is ADP's own known bug, still open: **#790** *"bug(gateway): tools/tool_choice stripped from
request before Bedrock call"*, confirmed in `modules/gateway/docs/endpoint-audit.md:115` (*"**NO** —
`anthropic_to_bedrock()` ... does not copy `request.tools` or `request.tool_choice`"*). Two
consequences specific to this evaluation:

- The failure is **silent**: the model returns prose instead of tool calls, HTTP 200. An
  experimenter would read it as "Hermes is bad at tool use," not "the gateway ate the schema."
- The one viable route requires `mantle_enabled`, which **defaults to `False`** with the route
  returning 503 (`shared/config.py:126`), and is gated to `openai.*` models
  (`shared/config.py:139`). So the experiment is: Hermes → `api_mode: codex_responses` →
  `/openai/v1/responses` → `openai.gpt-5.6-sol`, with mantle enabled.

Also worth flagging: Hermes' *native* Bedrock path is genuinely good — real SigV4 via
`BedrockOpenAISigV4Auth` with the full credential chain including **EKS IRSA**
(`agent/bedrock_adapter.py:221-259,466`) — but it offers no `base_url` override on the
`AnthropicBedrock` client (`agent/anthropic_adapter.py:1015-1022`) and Hermes has no
`SKIP_BEDROCK_AUTH` equivalent. So the Bedrock path would call **real Bedrock directly, bypassing
the gateway entirely** — no metering, no per-user budget, no rate limit. That path must be
explicitly excluded from any experiment, or the whole point (per-user budgets apply) is lost.

**No provider lock-in.** 37 bundled providers; Nous Portal is one profile among them and is
consistently a *fallback* in the auxiliary chain (`agent/auxiliary_client.py:11,24`). Nous coupling
is confined to attribution tags and `TERMINAL_MODAL_MODE=managed`, both avoidable.

### Q4 — HITL and guardrails: **stronger than expected, and headless-capable.**

`tools/approval.py` is a 5,727-line policy engine, not a token gesture. Default mode is `smart` —
an auxiliary LLM guardian adjudicates, with a consecutive-denial circuit breaker
(`hermes_cli/config_defaults.py:2440,2451-2457`). Layered, in order: a **hardline blocklist that
sits below `--yolo`** (`approval.py:431-439`), user deny globs matched *before* the yolo bypass,
sensitive-path gating (`~/.ssh`, `.netrc`, `.pgpass`, macOS `/private/*` symlink coverage,
`approval.py:345-408`), self-protection on `~/.hermes/config.yaml` because the agent could
otherwise flip `approvals.mode=off` mid-session (`approval.py:360-367`), and serious
anti-obfuscation (`$(...)` scanning, interpreter `-c` payload extraction, `sudo -S` stdin guard).
`--yolo` is frozen at import so a mid-session skill cannot escalate (`approval.py:3403-3405`).

Critically for server-side use, **approval is not TUI-bound**: `POST /v1/runs/{run_id}/approval`
(`gateway/platforms/api_server.py:2270`) resolves pending approvals over REST. Context modes fail
closed — `cron_mode: "deny"`, `single_query_mode: "deny"`, `subagent_auto_approve: False`
(`config_defaults.py:2442-2443`, `2044-2049`) — with the rationale that cron falling through to the
gateway branch *"would submit a pending approval with no listener and block the job indefinitely"*
(`approval.py:300-305`). That is operational scar tissue, not theory.

**Two gaps.** Policy is command-pattern-based and **profile-global**: one user clicking "Always
Approve" appends to `command_allowlist` (`config_defaults.py:2489`) and permanently widens the
policy for everyone on that profile. And `SECURITY.md:34-36` is explicit that *"The only security
boundary against an adversarial LLM is the [OS-level isolation]"* — the approval engine is
defence-in-depth, not a boundary.

**NemoClaw relevance: yes, and ADP already owns the equivalent.** `NVIDIA/NemoClaw` (Apache-2.0,
22,282 stars) exists precisely to *"Run agents like Hermes... more securely inside NVIDIA
OpenShell."* Its premise — sandbox the agent, don't trust its policy engine — is correct and matches
Hermes' own stance. But ADP does not need NVIDIA's implementation: gVisor is already deployed
(RuntimeClass `gvisor`/`runsc` at `platform/infra/gvisor-runtime.tf:25-36`, dedicated Karpenter
NodePool with taint at `nodepool-gvisor.tf:41-62`) and **unused** — no pod spec sets
`runtimeClassName` (`docs/gvisor-hardening-session-handover.md:11`). Any server-side Hermes must
set it. That is a prerequisite, not a nice-to-have.

### Q5 — Multi-tenant hosting on EKS: **No. One instance per user, or nothing.**

Upstream is unambiguous (`SECURITY.md:214-217`, quoted in the verdict). Concretely, what breaks:

- **Profiles are not users.** `gateway.multiplex_profiles` serves several `$HERMES_HOME` dirs from
  one process with fail-closed secret scoping (`agent/secret_scope.py:14-19` — good engineering),
  but profiles are operator-provisioned personas, not self-serve accounts. Its own docs frame it
  that way.
- **Shared OS/credential domain.** One process tree, one Unix user, one filesystem. Any authorized
  user can direct the agent at that profile's `.env`, `~/.ssh`, `~/.aws` — and `HOME` is *not*
  rewritten for subprocesses, so tenant code reaches the host's `~/.aws/`
  (`website/docs/user-guide/features/codex-app-server-runtime.md:331-334`) — i.e. the very
  credentials backing Bedrock. Codex runtime also shares `~/.codex/` across profiles by default
  (same doc, 313-325).
- **API auth is one shared bearer.** A single `API_SERVER_KEY` per profile, no per-user tokens, no
  scopes (`api_server.py:1954-1964`). Any holder can drive any session and resolve **anyone's**
  pending approval.
- **Dashboard auth is explicitly not multi-tenant** — *"not suitable for exposing a dashboard
  directly to the public internet"* (`website/docs/user-guide/features/web-dashboard.md:727`).
- **Compose topology is single-operator**: `network_mode: host`, shared `~/.hermes` bind-mount
  (`docker-compose.yml:36-37,68`).

**And it collides with ADP's compute model.** Every ADP agent workload today is an ephemeral KEDA
ScaledJob — `minReplicaCount: 0`, `restartPolicy: Never`, one pod per SQS message
(`webhook-ingress/infra/scaledjob.tf:143,178`), 6 h `activeDeadlineSeconds`
(`scaledjob.tf:161`). **There is no long-lived per-user pod anywhere in the repo.** Hermes' cron
requires a resident process ticking every 60 s (`cron/__init__.py:11-16`,
`gateway/run.py:31584-31591`); no gateway process means jobs simply don't fire, and there is **no
retry** — `cron/executions.py:3-5`: *"The ledger records what is known about each attempt; it is
not a retry queue."* So one-pod-per-user is a **new deployment archetype** for ADP: N resident
StatefulSets with per-user PVCs, each needing an IRSA role, a Secrets Manager namespace, network
policy, and gVisor. Cost is linear in users and **does not scale to zero** — the opposite of the
current model's economics.

Per-user cost attribution would partly work: `usage_logs` already carries `user_id` and budgets
enforce a `user` entity type (`shared/models/usage.py:10-29`, `shared/models/budget.py:14`), so
**model tokens** attribute correctly through the gateway. But **EKS compute would not** — KEDA pods
carry no tags and there is no `Tenant` tag at all (`docs/tagging-and-observability.md:25-33`). A
resident per-user pod makes compute the dominant cost, so this gap stops being cosmetic.

### Q6 — Relationship to gbrain (#1220): **complements, does not supersede. Do not open a third memory track.**

The issue calls gbrain "Hermes-lineage." Verified and slightly refined: `garrytan/gbrain` is
described upstream as *"Garry's Opinionated OpenClaw/Hermes Agent Brain"* — a **brain built for**
Hermes/OpenClaw, i.e. a downstream consumer, not an ancestor. Hermes' own equivalent seam is the
`MemoryProvider` ABC (`agent/memory_provider.py:110`) with 8 bundled providers
(`plugins/memory/`: byterover, hindsight, holographic, honcho, mem0, openviking, retaindb,
supermemory) and a hard "exactly one external provider" rule (`agent/memory_manager.py:450-465`).
So gbrain and Hermes are **layers, not rivals** — adopting one says nothing about the other.

Consolidating the two recommendations:

- gbrain's decision already landed and should not be re-litigated: architect verdict *Experiment*
  (#1220), functionally proven in-account 2026-06-17 (#1233), `GBRAIN_ENABLED` still off, with
  significant un-codified live state. Crucially, **#1283 is already open** to *port gbrain's
  owner-isolation + synthesis ideas into `agent-context`, reusing existing infrastructure* — i.e.
  ADP already chose "harvest the ideas, don't host the upstream."
- **Hermes' memory model should feed that same #1283 workstream**, not a third track. Its
  contribution is the two ideas ADP lacks: the *self-nudge* (turn-counted trigger that forks a
  cheap agent to decide what to persist) and *tiered budgeting* (a hard ~1,300-char always-on block,
  everything else retrieved). ADP already has the synthesis/decay half via the personal-context
  CronJob (`personal_context/synthesis.py:527-535`).
- One concrete caution: Hermes bundles an **OpenViking** memory provider
  (`plugins/memory/openviking/__init__.py`). ADP **removed OpenViking in #1387** — semantic search
  is now S3 + S3 Vectors, memory is Postgres + S3 (`modules/agent-context/README.md:89`). Adopting
  Hermes' memory stack would reintroduce a dependency ADP deliberately retired.

### Q7 — License / community / coupling risk

| Dimension | Finding |
|---|---|
| License | **MIT** (`LICENSE`, © 2025 Nous Research). Clean; no AGPL/BSL concern. Satisfies `modules/agent-context/README.md:23` ("Apache-2.0 / MIT only"). |
| Maturity | v0.20.5; 29 releases; sub-1.0 version numbering. |
| Cadence | **Very high — this is the main risk.** One release window (v0.20.2→v0.20.3, *same day*) landed *"~250 commits across ~461 files (+42,613 / −1,641), ~125 merged PRs"* including an **MCP 2.x SDK migration**. Config schema is on **auto-migration v12+** and state schema is at **v26**. |
| Breaking-change history | Structural churn is routine: state schema v26 with legacy FTS layouts pending `hermes sessions optimize-storage`; `max_async_children` deprecated and ignored; the `CronScheduler` external-provider seam is labelled *"⚠️ EXPERIMENTAL — validated by exactly ONE consumer... signatures may change without deprecation"* (`cron/scheduler_provider.py:1-19`). |
| Auditability | `gateway/run.py` is **1.5 MB**, `cli.py` 1.0 MB, `cli-config.yaml.example` 103 KB. Meaningful security review of a pinned version is a real cost, and it recurs every upgrade. |
| Ecosystem claims | **The issue's ecosystem list is wrong in a way that matters.** `hermes-webui`, `hermes-desktop`, and `hermes-workspace` are **not NousResearch repos** — all three 404 under that org. They are third-party: `nesquena/hermes-webui`, `fathah/hermes-desktop`, `outsourc-e/hermes-workspace`. So the "whole ecosystem" is unaffiliated community projects with their own licenses, maintainers, and security postures. Building the flagship user-facing product on them means depending on individuals, not Nous Research. |
| Coupling exposure | Highest-consequence item: this would be **the user-facing product**, on a sub-1.0 upstream shipping 125 PRs per window, whose own security policy says it is single-tenant. Pinning a version trades churn for an unpatched fork; tracking upstream means continuous re-review of a 1.5 MB file. |

---

## 2. Positioning vs sibling investigations

All three siblings (#4160 dsh, #4161 Hermes, #4162 ORCA) target the same objective from different
angles, and they are **not** substitutes:

- **#4161 Hermes** — *the personal front end.* Its distinctive assets are memory/skills/cron and a
  multi-surface chat presence. Its blocker is tenancy.
- **#4160 DeepSeek Harness** — *the run substrate.* The gap Hermes does not address and ADP most
  needs: `activeDeadlineSeconds: 21600` with **no checkpoint/resume** anywhere
  (`scaledjob.tf:166-170` — eviction *"kills the run silently (no resume) and orphans its SQS
  message"*). Hermes has no answer here either; its cron has no retry.
- **#4162 ORCA** — *the supervision cockpit.* Overlaps Hermes only on HITL surface.

**Recommended reading order for the maintainer:** the multi-day objective is bottlenecked by
**checkpoint/resume (#4160)**, not by the absence of a chief-of-staff. Hermes cannot make agents
survive longer; it can only make a nicer front door to agents that still die at 6 h. Sequence
#4160 first.

---

## 3. Harvest list — what to take, regardless of the verdict

Each of these is an ADP-native change, cites an ADP file, and needs no Hermes dependency:

| # | Pattern | Hermes source | ADP home |
|---|---|---|---|
| H1 | **Self-nudge to persist.** Turn-counted trigger forks a cheap agent asking "should anything be saved?", with a tool allowlist limited to memory tools. Counter rehydrates across sessions. | `agent/turn_context.py:739-746`, `agent/background_review.py:1-17`, `:690-698` | Feed **#1283**; implement over `memory/tools.ts` + agent-context `experience` verb |
| H2 | **Read-before-write on memory edits**, enforced by provenance rather than trust. | `tools/skill_manager_tool.py:76-90`, `tools/skill_provenance.py` | agent-context `experience` verb |
| H3 | **Tiered memory budget** — a hard char cap on the always-on block, forcing consolidation instead of unbounded growth. | `config_defaults.py:1974-1975`, `tools/memory_tool.py:176-189` | #1283 / `design-personal-context-consolidated.md` |
| H4 | **Mutation ledger with content-addressed before/after blobs + `rollback <entry-id>`**, explicitly telemetry-not-a-gate so a ledger failure never blocks. | `tools/skill_ledger.py:1-26` | agent-context memory writes |
| H5 | **Fail-closed approval contexts.** Non-interactive contexts default to `deny` *because* a pending approval with no listener blocks forever. ADP's `ApprovalService.pollForApproval` is an in-pod `while(true)` burning the 6 h budget with no such guard. | `approval.py:300-305`, `config_defaults.py:2442-2443` | `agent/src/services/ApprovalService.ts:16-60` |
| H6 | **`key_cmd` as a documented integration contract.** ADP's `bg-cognito-auth.sh token` already emits the exact shape; document it as the generic third-party-agent auth pattern, not a Claude-Code/Codex special case. | `agent/command_token_source.py:1-35` | `modules/gateway/cli/README.md`, `/setup` page |
| H7 | **At-most-once scheduling discipline** — advance `next_run_at` for all recurring jobs under the file lock *before* any execution; PID+start-time liveness for dead-owner reclaim. Directly relevant when ADP wires its first scheduled rule. | `cron/scheduler.py:7696-7700`, `cron/executions.py:48-50` | `webhook-ingress/infra/eventbridge.tf` |

## 4. Blockers ADP must fix for its own sake (surfaced by this investigation)

These are ADP defects, independent of Hermes:

1. 🔴 **#790 — `tools`/`tool_choice` dropped on both translated inference routes.** Confirmed at
   `format_translator.py:116-136` (OpenAI) and `:242-263` (Anthropic);
   `modules/gateway/docs/endpoint-audit.md:115`. **Any** third-party agent framework pointed at
   `/v1/messages` or `/v1/chat/completions` silently loses tool calling at HTTP 200. This blocks
   #4160 and #4162 too, and it makes ADP's gateway look broken to every external framework
   evaluated under EPIC #1219. Highest-leverage fix in this report.
2. 🟠 **gVisor is deployed and unused.** `runtimeClassName` is set by no pod spec
   (`platform/infra/gvisor-runtime.tf:25-36` vs `scaledjob.tf`). A prerequisite for hosting any
   third-party agent runtime server-side.
3. 🟠 **No compute-level cost attribution.** No `Tenant` tag; KEDA pods untagged
   (`docs/tagging-and-observability.md:25-33`). Cosmetic today, load-bearing the moment a resident
   per-user pod exists.
4. 🟡 **`secretsmanager:GetSecretValue` on `arn:...:secret:adp/*`** for the agent role, constrained
   only by *withholding* KMS decrypt (`scaledjob-iam.tf:239-283`). Invariant #4 namespaces user
   content under `adp/users/<sub>/*` — the same broad prefix. Worth narrowing before any per-user
   agent runtime exists.

---

## 5. Experiment spec (optional — only if the maintainer wants the question closed empirically)

Deliberately scoped to answer **one** question the desk research cannot: *does Hermes' memory +
skills + cron loop actually produce compounding value for one ADP developer over weeks?* It is
explicitly **not** a multi-tenant pilot — Q5 is settled and needs no experiment.

```
ExperimentId: hermes-cos-eval-2026-09
Duration:     3 weeks
Subject:      ONE developer (the maintainer), single user, single instance
Owner:        maintainer
Status:       Proposed — not approved
```

**Topology.** Off-the-shelf Hermes on the maintainer's **own machine or a personal VPS** — *not*
ADP EKS, *not* a shared account. Nothing is added to `modules/`; nothing deploys to ADP. This keeps
adoption/rejection free, which is EPIC #1219's stated requirement for experimental setups.

**Configuration (all of it):**
```yaml
providers:
  adp:
    base_url: https://<gateway>/api/openai/v1
    api_mode: codex_responses          # ONLY tool-preserving gateway route (§Q3)
    model: openai.gpt-5.6-sol
    key_cmd: bash ~/bin/bg-cognito-auth.sh token   # already emits the right shape
mcp_servers:
  adp-knowledge:
    url: http://<agent-context>:5100
    headers: {X-Internal-Api-Key: "...", X-Owner-Sub: "...", X-Tenant-Id: "..."}
approvals:
  mode: manual                          # not smart — we are measuring human oversight cost
```

**Prerequisites (both are hard gates):**
1. `mantle_enabled = true` in the target env — else the route is 503 (`shared/config.py:126`).
2. Confirm no Hermes provider is configured for native Bedrock. The `AnthropicBedrock` path has no
   `base_url` override (`anthropic_adapter.py:1015-1022`) and would bypass the gateway entirely —
   voiding metering and per-user budgets, i.e. voiding the experiment.

**Success criteria** — the experiment passes only if all four hold:

| # | Criterion | Measure | Target |
|---|---|---|---|
| 1 | Tool calling works end-to-end through the gateway | Hermes completes a multi-tool task | 100% of 10 trials |
| 2 | Every token is metered to the user | `SELECT count(*), sum(cost_usd) FROM usage_logs WHERE user_id=<sub>` reconciles with Hermes' own accounting | ±5% |
| 3 | Delegation works without lineage forgery | Hermes files/labels an issue → webhook dispatch → agent PR | ≥3 successful chains |
| 4 | Memory/skills compound | Agent-created skills reused in later sessions without re-explanation | ≥5 reuses in week 3 |

**Kill criteria (stop immediately):** any token appearing in `usage_logs` with a NULL/foreign
`user_id`; any Hermes request reaching Bedrock without traversing the gateway; any agent-authored
skill script performing an action the maintainer did not approve; approval fatigue exceeding ~10
prompts/hour in `manual` mode.

**Teardown:** `rm -rf ~/.hermes` and delete the VPS. No ADP state, no Terraform, no PR.

**What the result changes.** Pass → the harvest list (§3) gets prioritized, and Hermes is
documented as a *supported personal client* of the ADP gateway (like Claude Code and Codex already
are) — **not** as `modules/user-services/chief-of-staff/`. Fail → close #4161 as "not now" and
sequence #4160 (checkpoint/resume) instead.

---

## 6. Recommendation summary

| Question | Answer |
|---|---|
| Adopt Hermes as `modules/user-services/chief-of-staff/`? | **No.** Upstream is single-tenant by policy; the learning loop violates invariant #10; state is local SQLite. |
| Adopt Hermes as a *supported personal client* of the ADP gateway? | **Yes, cheaply** — same category as Claude Code / Codex. Blocked only by #790 + `mantle_enabled`. |
| Harvest its design patterns? | **Yes** — 7 items in §3, all ADP-native, feeding #1283 and existing surfaces. |
| Does it supersede gbrain (#1220)? | **No.** Different layers. Route Hermes' memory ideas into the existing #1283 workstream; do not open a third track. |
| Does it advance the multi-day objective? | **Only marginally.** The real bottleneck is checkpoint/resume (`scaledjob.tf:166-170`), which Hermes does not solve. Sequence **#4160** first. |

**Final verdict: ADAPT** — harvest the patterns, optionally run the single-user experiment, reject
as the multi-tenant product foundation. ADP's own committed design
(`docs/user-scoped-agents-design.md:225`: chief-of-staff is *"80% a template + UI, not a new
service"*) remains the right architecture; what's missing is the v2/v3 substrate beneath it, which
Hermes does not supply.
