# DeepSeek Harness (dsh) — Fit Assessment for ADP's Long-Running Agent Runtime

> Investigation for issue #4160, sub of EPIC #1219. Architect-led, no implementation.
>
> **Subject:** [`deepseek-ai/deepseek-harness`](https://github.com/deepseek-ai/deepseek-harness) — "DeepSeek Harness: Everything is a Plugin."
> **Objective it is judged against:** highly productive, multi-day long-running agents on ADP, right level of human-in-the-loop, high productivity, low waste — without breaking webhook-ingress dispatch, KEDA-scaled agent workers, gateway auth/metering/budgets, agent-context MCP tools, or adp-trigger lineage.
>
> **Verdict: ADAPT (borrow the architecture) + a narrowly-scoped gated EXPERIMENT. Do not adopt as the worker runtime.**

Evidence base: full clone of the upstream repo at `dsh-v0.1.1-rc.2` (997 commits, 501k LOC TypeScript across 227 workspace packages), its `docs/` tree (~40 architecture docs + ~90 subsystem docs, all bilingual), and the live ADP tree at `0e0d3d5`. Every claim below cites a path. Upstream paths are relative to the checkout; ADP paths are repo-relative.

---

## 1. Summary of the candidate

| Fact | Value | Source |
|---|---|---|
| License | MIT | `LICENSE`, `THIRD_PARTY_NOTICES.md` |
| Language / build | TypeScript, pnpm 11 workspace, Node `^22.19.0 \|\| >=24` | `package.json` |
| Version | `0.1.1-rc.2` — **pre-1.0, every release a prerelease** | `package.json`, `git tag` |
| Repo created | **2026-08-13** | GitHub API |
| Stars / forks / watchers | 197,206 / 22,381 / **857** | GitHub API |
| Scale | 227 workspace packages, 2,101 `.ts` files, 501k LOC, 689 spec files | `find`/`wc` over checkout |
| Foundation | Cordis (vendored, `vendor/cordis`, v4.0.1) — a plugin/DI meta-framework | `docs/cordis-primer.md` |
| Self-description | "developer preview … **THERE WILL BE COMPATIBILITY-BREAKING CHANGES**" | `README.md:11` |

**What it is.** An agent harness where the plugin tree *is* the product. There is no privileged core: the model adapter, tool registry, session log, and **the agent loop itself** are all plugins mounted into a Cordis context and replaceable from configuration (`docs/architecture.md`, "Cordis" section). Composition happens through layered **profiles** and **bundles** — `dsh-base` (models, tools, persistence, sandbox, approval policy), `dsh-web-app` (browser UI), `dsh-headless` (one-shot runner, no server).

**Two observations on the maturity signal.** First, the star:watcher ratio is ~230:1 — an order of magnitude above what a 197k-star project normally shows, and the repo is 13 days old. Star count here is a proxy for launch attention, not for production battle-testing. Second, and cutting the other way: the *engineering* is unusually disciplined for a 13-day-old repo — a documented capability-seam model, generated-and-verified config/API catalogs (`scripts/gen-cordis-catalog.ts`), numbered postmortems (`docs/postmortem/0001`–`0004`), 689 spec files, 18 CI workflows, and 1,498 architecture decision notes under `.agents/notes/{proposed,implemented,rejected,archived}`. This is a serious codebase with a very short track record. Both facts are true and the recommendation has to hold both.

**Notable:** dsh ships first-class **Claude Agent SDK integration** — `packages/subagent/subagent-claude-code` pins `@anthropic-ai/claude-agent-sdk@0.3.220` and delegates to the real Claude Code CLI as a subagent provider, and `packages/hooks/hooks-claude-code` executes an existing Claude Code `hooks.json` on dsh's own interception points. There is also `subagent-codex`. dsh treats Claude Code as a *component*, not a competitor. That materially changes the framing: adopting dsh would not mean abandoning the Claude SDK.

---

## 2. Answers to the six specific questions

### Q1 — Runtime model for long-running work

**dsh's session model is genuinely better than ours, and it is the single most valuable thing in this investigation.**

A dsh `Session` is an **append-only, event-sourced log** of typed `SessionEvent`s, and it is the sole source of truth: "The LLM message history is *derived* from the log, never stored separately; replay is re-derivation from the same events" (`docs/subsystems/session.md:5`). A runtime invariant enforces that anything model-visible must be reconstructable from the log — "**Model-visible means logged**" (`docs/architecture.md`, Session log section). Durability is a separate swappable seam (`ctx.sessionPersistence`) with three interchangeable backends: JSONL, SQLite, and one in-memory (`packages/session/`).

Three properties matter for multi-day work:

1. **Crash recovery does not truncate.** A log reloaded mid-turn finds an open `turn/start` with no `turn/end` and closes it with a synthetic `turn/end { reason: { kind: 'interrupted' } }`, explicitly *because* "a single turn can be huge in a long-horizon task (many steps, large tool output), and those events were durably appended before the crash" (`docs/subsystems/persistence.md:15`). They designed for exactly our failure mode.
2. **Resume and fork are first-class APIs**, not retry hacks: `ctx.sessions.fork(source, boundary?, childSessionId?)` forks from any stable between-turn boundary with lineage metadata (`parentSession`, `seedLength`) in the session header (`docs/subsystems/session.md:536-544`).
3. **Batched write-behind with an explicit flush checkpoint** — `session/flush` is the ordering and error-observation barrier before the loop claims its next turn (`docs/subsystems/persistence.md:13`).

**Compared to ADP today**, and this is the uncomfortable part:

| | dsh | ADP today |
|---|---|---|
| State model | Append-only event log, message history derived | Claude SDK session persisted to **ephemeral container storage** |
| Durability | Swappable seam: JSONL / SQLite | `CLAUDE_CONFIG_DIR` on pod-local disk — `agent-worker.ts:1204-1205` says this "leaves no durable footprint beyond the pod's lifetime" |
| Resume scope | Any session, any time, from any boundary | **Only within one pod's lifetime**, only for transient stream stalls (`utils/resilientQuery.ts:6-12`, issue #2079) |
| Hard ceiling | None architecturally | **6 hours** — `activeDeadlineSeconds: 21600` (`webhook-ingress/infra/variables.tf:171`) |

**The headline finding of this investigation is not about dsh at all: ADP cannot run a multi-day agent today, and the blocker is not the framework.** Our run lifecycle is one-run-per-dispatch with a 6h Kubernetes deadline and session state on ephemeral pod storage. When that pod dies, the reasoning history is gone; `resilientQuery` can resume across a *stream stall* inside one pod, but nothing survives pod replacement. "Multi-day" needs durable cross-pod session state and a resume-from-boundary primitive. **We need that capability whether or not we ever run dsh** — and dsh is the best available reference design for it.

### Q2 — Human-in-the-loop primitives

**dsh has richer HITL vocabulary than us, but its HITL is in-memory and live-UI-bound — which is exactly backwards for our use case.**

Two seams exist. `ctx.approval` (`docs/subsystems/approval.md`) answers "may this action proceed?" with a closed, **fail-closed** outcome set (`'allowed-once' | 'rejected' | 'cancelled' | 'unavailable'`); a missing or throwing answerer becomes `unavailable` and callers deny. That fail-closed default is good design and worth copying verbatim. `ctx.userQuestions` (`docs/subsystems/user-questions.md`) is how an agent asks the human mid-run, including a typed `plan-review` intent whose `approve` option is *named rather than positional* so no UI can infer the verdict from option order — a nice detail.

The **transport is genuinely pluggable** — worth stating clearly, because it is the strongest form of the pro-dsh argument. Answerers are ordinary `ctx.on('approval/request', (req, next) => ...)` listeners dispatched through a waterfall scoped to an agent subtree (`packages/interaction/user-approval/src/index.ts:317-320`), and the repo already ships a **non-UI machine answerer**: the ACP bridge decides approvals by policy over JSON-RPC with no UI at all (`packages/acp/acp/src/index.ts:268-285`). So "a GitHub-comment answerer" is architecturally expressible. That is not the problem.

**The problem is that the durability is wrong for us**, in four citable ways:

- `ask()` is a **blocking in-process Promise** requiring a live registered provider, valid "only for the exact live runtime root" — an owned child agent "has no human answerer and **would block forever**" (`docs/subsystems/user-questions.md:163-172`).
- **Questions have no durable vocabulary at all.** `user-questions` declares *no* `SessionEventMap` events (unlike `user-approval`, which declares two), so there is no `question/asked` to replay. Pending state lives in host process memory (`packages/host/apiproxy/src/api-proxy.ts:1071-1073`), and provider disposal cancels every pending ask with `ASK_ABORTED` (`:1339-1346`). A browser refresh survives via mux replay (`:3334-3345`); **a host restart does not.**
- Approvals are *auditable* but not *resumable*: `approval/asked` / `approval/decided` are log-only and require an open turn (`docs/subsystems/approval.md:88,117-118`). A restart finds a dangling `approval/asked` and repair **closes it rather than re-asking** (`packages/core/session/src/repair.ts`).
- 🔴 **And the profile I recommend for EKS cannot do HITL at all.** Server→client requests are "dead capability — the transport supports them, but the server never sends one" (`packages/sdk/protocol/README.md`; same statement in `packages/sdk/client/README.md`). With no answerer registered, `ctx.approval` returns `unavailable` → **deny**, and `ask_user_question` throws `NO_PROVIDER`. Headless/SDK automation therefore runs with `approval: 'never'` or the `danger-full-access` preset. Only the web host and ACP can satisfy HITL today — and the web host is the profile with no authentication (§Q6).

Note also that the policy axis is coarse: `ApprovalPolicy` is exactly `'ask' | 'never'` (`packages/interaction/user-approval/src/index.ts:94`) and the shipped presets are exactly two — `workspace-write` (ask) and `danger-full-access` (never) (`packages/interaction/permission-presets/src/index.ts:184-190`). There is no allow-list or per-tool grant tier.

For a multi-day agent, a pause gate must **outlive the process**: the agent should checkpoint, exit, free its compute, and be resumed days later by a human action. dsh's model holds a live process (and an open turn) waiting on a Promise. On EKS at our concurrency that is the expensive-waste failure mode the objective explicitly names — an hours-long GitHub-comment round trip would mean paying for an idle pod for the entire wait.

**ADP's existing pattern is architecturally better here and we should say so plainly.** The AIDLC gate flow already implements durable, process-free gating: the worker commits, posts a gate comment, and **exits**; a human answers by GitHub comment; a fresh dispatch resumes and writes a synthetic `HUMAN_TURN` presence event because "there's no interactive human session" (`modules/agent-factory/agent/src/aidlc-presence.ts:1-13`, issue #3232 / EPIC #3158), enforced deterministically by the gate enforcer (`agent-worker.ts:1722-1738`, issue #3231). That is a durable ticket answered out-of-band with zero compute held open. `modules/harness/contracts/README.md` already names `hitl-ticket.schema.json` as the contract to formalize it.

**Conclusion: borrow dsh's HITL *vocabulary* (fail-closed outcomes, named-approve intents, question batching with stable ids), keep ADP's durable-ticket *mechanism*.** Do not adopt dsh's live-Promise model.

### Q3 — Plugin architecture fit

**Yes, ADP capabilities map cleanly onto dsh plugins — with one hard blocker on identity.**

The extension model is real and well-documented. `docs/architecture.md` has a "Where new behavior goes" table mapping goals to seams: model provider → register adapter on `ctx.llm`; model-facing capability → `ctx.tools`; background work → `ctx.jobs`; durable session state → extend `SessionEventMap`. A **capability seam** has three declared roles (Service Definition / Provider / Consumer) and swapping one provider moves the whole execution world — pointing the fs and subprocess providers at a remote sandbox moves Bash, PTY, and LSP with them, no forks (`docs/capability-seams.md`, `docs/architecture.md`). `packages/e2b/` is a working demonstration: three small plugins relocate the entire filesystem+process world into a remote E2B sandbox.

Mapping our three named capabilities:

| ADP capability | dsh seam | Blocker |
|---|---|---|
| Gateway-authenticated model access | `llm-pi-ai` custom provider: `baseURL`, `api`, `headers` (`docs/config-catalog.md:1069-1070`) | None — see Q4 |
| agent-context MCP tools | `mcp-client` `streamable-http` transport (`packages/mcp/mcp-client/src/index.ts:78-88`) | **Yes — static headers** |
| adp-trigger dispatch lineage | A tool plugin on `ctx.tools` + `SessionEventMap` extension for durable lineage events | None architecturally |

**MCP (Q3 second half): dsh is an MCP *client* only, and a partial one.** `packages/mcp/` contains exactly one package, `mcp-client`. It consumes external MCP servers over `stdio` and `streamable-http` with reconnect/backoff policy — but dsh does **not** expose itself as an MCP server. Two further limits matter for us: **tools are the only bridged capability** (Resources and Prompts are deferred; server-initiated sampling and elicitation are unimplemented), and there is **no OAuth support** — zero `oauth`/`authProvider` references anywhere in `packages/mcp/`, so the static header block below is the *entire* auth story. Anything wanting to call *into* dsh uses ACP over JSON-RPC stdio (`packages/acp/acp`) or the Typert `/api` RPC bridge, not MCP. For us that direction is fine (we need dsh to *consume* agent-context), but it means dsh cannot be plugged into `modules/harness/mcp-hub/` as a tool provider.

🔴 **The identity blocker.** `mcp-client` headers are `Record<string, string>` fixed at **config load time** (`packages/mcp/mcp-client/src/index.ts:87-88,123`). ADP's agent-context Door derives *every* ACL decision from per-caller request headers — `x-github-login`, `x-github-teams`, `x-tenant-id`, `x-owner-sub` (`modules/agent-context/door/auth.py:5-8`). A static header block means **one dsh process can only ever assert one identity**. In a multi-tenant deployment that is a cross-tenant read of indexed source code, wikis, and agent memory — precisely the hole #4073 finding #8 closed. Any hosted multi-tenant dsh needs either one process per tenant-user (see §5) or an upstream change threading per-session headers into MCP calls. This is not a detail to discover during implementation.

### Q4 — Cost/waste controls, and can it front our gateway?

**Yes, it can front our gateway — this is the cleanest integration point and the basis of the experiment in §6.**

dsh's `llm-pi-ai` adapter supports **custom providers** with an explicit `baseURL`, a selectable wire protocol, a credential reference, and per-provider `headers` (`docs/user/guide/providers.md`, "Add a custom provider"). Supported protocols include `openai-completions`, `openai-responses`, `anthropic-messages`, and `bedrock-converse-stream` (`packages/llm/llm-pi-ai/src/provider.ts:50`, `packages/llm/llm-pi-ai/src/catalog.ts:291-292`).

Our gateway serves exactly those first two shapes:

- `POST /v1/chat/completions` — OpenAI (`modules/gateway/src/proxy/routes.py:256`)
- `POST /v1/messages` — Anthropic (`modules/gateway/src/proxy/routes.py:318`)

and both are in `ENFORCED_PATHS`, the single source of truth imported by the auth, budget, and rate-limit middlewares (`modules/gateway/src/shared/enforced_paths.py:25-26`). The header of that file documents why it exists: routes that skipped enforcement (#2792, #2809). **So if dsh points at a gateway route in that tuple, budgets / rate limits / metering keep working by construction — no gateway change required.** That is the single most important integration fact in this report.

There is an even lower-friction path. Our workers already reach the gateway through a **localhost sigv4-proxy sidecar** presenting an Anthropic base URL at `127.0.0.1:9090` (`modules/agent-factory/agent/k8s/chat-scaledjob.yaml:45-53`; `ADP_BEDROCK_VIA=gateway`, kill switch `=direct`). A dsh pod in that shape needs one custom-provider row pointing at `http://127.0.0.1:9090` — no new auth path, no new credential handling.

Two compatibility notes from `docs/user/guide/providers.md` ("Request compatibility"), because they will bite in the experiment: pi-ai infers request shape from the URL, and for reasoning models sends the system prompt as `role: "developer"` and the cap as `max_completion_tokens`. If our OpenAI route rejects either, set `compat: { supportsDeveloperRole: false, maxTokensField: max_tokens }` on the provider row. Cheaper still: use the `anthropic-messages` protocol against `/v1/messages` and sidestep the class entirely.

**Waste controls, honestly assessed:**

- ✅ **Compaction** is a real seam with durable `compaction/*` events; the summary rides a `user/message` with `surfaceOp: { op: 'replace', start, end }` — the only surface mutation performed (`docs/subsystems/compaction.md`).
- ✅ **Spill-to-file** persists oversized tool output and hands the model a locator plus retrieval guidance instead of the payload (`docs/subsystems/spill.md`). Direct answer to long-run context bloat.
- ✅ **Token meter** (`ctx.tokenMeter`) prices the current session surface node-by-node, reusing real provider usage as an anchor when the request envelope matches and falling back to a heuristic otherwise (`docs/subsystems/token-meter.md`).
- ✅ **Prompt-cache awareness is architectural, not incidental** — `cacheRetention` on providers, and *every* package README carries a "KV Cache effect" section stating whether the feature perturbs the reusable prefix. This is the most disciplined cache-hygiene practice I have seen in an agent framework, and it is directly a low-waste property.
- ✅ **Goals** give a durable objective with `maxGoalRounds` and phases `active|paused|blocked|complete` (`docs/subsystems/goal.md`) — a bounded multi-round continuation primitive we lack.
- ❌ **No budget enforcement.** The token meter *measures*; it does not deny. There is no spend cap, no per-tenant quota, no model routing by cost. Our gateway does enforce (`src/budget/enforcement_middleware.py`). **This is an argument for keeping the gateway in front, not for adopting dsh's controls** — and it means dsh's metering is complementary observability, not a replacement.

### Q5 — Positioning vs. the current stack

Four options; I recommend the third, plus a do-nothing baseline stated honestly.

**Option A — Replace the agent-worker entrypoint with dsh. 🔴 Reject.**
Breaks or reimplements almost everything the objective says not to break. Our worker is ~1,700 lines of accumulated ADP semantics: adp-trigger lineage and marker signing (`agent-worker-image/lib/marker_signing.py`), AIDLC gates and presence, Check Run transcript streaming, `X-Agent-RunId` usage attribution (`gateway/src/proxy/routes.py:133-137`), SQS visibility heartbeat, knowledge-layer MCP wiring. Re-earning that on a 13-day-old pre-1.0 framework that promises breaking changes is a large rewrite for zero user-visible gain — and it would *not* fix the 6h/ephemeral-state limit, which is ours, not the SDK's.

**Option B — dsh as an additional persona runtime alongside Claude-SDK workers. ⚠️ Possible, not now.**
Technically credible (see §6), and dsh's own `subagent-claude-code` proves the two can coexist in one process. But it doubles the runtime surface — two session models, two HITL models, two upgrade streams — before we have a single user asking for something dsh does and we do not. Revisit if the experiment surfaces such a case, and only after 1.0.

**Option C — Adopt specific ideas into ADP's own harness; run one gated sandbox experiment. ✅ Recommend.**
The ideas transfer without the dependency, and land where `modules/harness/` is *already* being designed — `ARCHITECTURE.md` "Today vs. target" records `contracts/` as 11 planned schemas, none written, and `mcp-hub/` as design docs only. dsh is a validated reference design arriving exactly when our contracts are still on paper. Specifically:

| Borrow | Into | Why |
|---|---|---|
| Append-only session log; message history *derived*; "model-visible means logged" | Agent runtime state model | The prerequisite for multi-day runs — see Q1 |
| Crash recovery that closes an interrupted turn rather than truncating | Worker restart path | Directly fixes ephemeral-state loss |
| `fork(source, boundary)` with lineage in the header | Resume/branch semantics | Composes with adp-trigger lineage |
| Fail-closed approval outcomes incl. `unavailable`; named-approve intent | `hitl-ticket.schema.json` | Contract is planned but unwritten |
| Spill-to-file with model-facing locator | Tool output handling | Cheapest available context-waste win |
| Capability-seam discipline (Definition / Provider / Consumer, all three or it isn't a seam) | `modules/harness/contracts/` | Our six surfaces are seams; this is a proven articulation |
| "KV Cache effect" as a required section in every capability doc | Harness docs + issue template | Makes cache waste reviewable |

**Option D — Do nothing. Costed, since the issue asks.**
Cost is not zero but it is bounded and mostly *not* dsh-shaped. We keep a 6h ceiling and ephemeral session state, so "multi-day agents" stays undeliverable — but that gap is ours to close regardless, and Option C closes it. The genuine do-nothing risk is narrower than the issue's framing suggests: dsh is 13 days old, pre-1.0, MCP-client-only, and cannot serve as an MCP tool provider, so there is no near-term standardization pressure to be locked out of. Ecosystem risk is worth **one revisit at 1.0**, not a hedge purchase now.

### Q6 — Ops and risk

**EKS shape.** dsh is a single long-lived Node process; the web host binds `127.0.0.1` (`docs/subsystems/web-server.md`, Config). Two viable shapes:

- **KEDA ScaledJob, `--profile headless`** — fits our existing model exactly. `dsh-headless` "mounts no Host, HTTP server, Web runtime, or browser plugin", creates one persisted agent, runs one task, prints the last assistant text, exits 0/1 (`packages/bundle/headless/README.md`). This is the right shape for the experiment: no listening port, no long-lived pod, no new ingress.
- **Long-lived pod with the web profile** — needed for the session UI, but see the security findings below. Not for a shared environment.

🔴 **Security posture — three findings, all citable.**

1. **The web server has no auth — and upstream says so twice.** `host` accepts only `127.0.0.1` or `0.0.0.0`, and "there is **no TLS, auth, or origin policy**, so a non-loopback bind exposes the server to that network" (`docs/subsystems/web-server.md`, Config). The `/api` RPC gateway *does* have a trust check, but it is explicitly scoped: `trustedHosts` is "a DNS-rebinding fence, **explicitly not authentication**, so the whole configuration plane stays loopback-same-origin **until a real authentication layer exists**" (`packages/client/connection/src/index.ts:76-79`). The consequence is conceded in-source: *"any caller that may start a session at all can already run commands as this process"* — which is why the authors decline to pin the preset switch, calling it "a fence beside an open gate" (`:100-103`). The loopback-pinned `PRIVILEGED_METHODS` list is a blast-radius reduction on top of that, not a fix. Exposing the web profile in a shared cluster hands anyone with network reach full agent control — i.e. arbitrary command execution as the pod. Any hosted web-profile deployment needs an authenticating proxy in front, and a NetworkPolicy — the same defence-in-depth pair we already apply to agent-context (`modules/agent-context/door/auth.py:26-32`).
2. **Credentials are plaintext on disk.** API keys live in `$DSH_HOME/.credentials.yaml` (`docs/user/guide/providers.md`); settings hold only references. Fine for a laptop, wrong for multi-tenant EKS. Mitigation: don't give it long-lived keys — front the gateway via the localhost sigv4 sidecar, which is how our workers already work.
3. **Telemetry and identity egress default on.** A per-home anonymous UUID is sent to DeepSeek as `x-deepseek-harness-user-id` by `dsh-llm-deepseek`, and reported as OTel resource `user.id`; `DSH_TELEMETRY_DISABLED` "stops telemetry export only; it does **not** suppress direct feedback acknowledgement or the DeepSeek provider header" (`packages/identity/anonymous-user-id/README.md`). Any experiment must not mount `dsh-llm-deepseek` at all, and should run with restricted egress. Note the redaction seam "ships NO rules of its own" — exported data is only as clean as the rules you mount (`docs/subsystems/session-telemetry.md:126`).

Also note `packages/identity/` contains *only* `anonymous-user-id`. There is no user/tenant identity model. Multi-tenancy is not a gap to configure — it is absent by design, consistent with a single-user local tool. Combined with the static-MCP-header blocker (Q3), the only safe hosted shape is **one process per tenant-user, no shared instance**.

**Sandboxing.** `ctx.sandbox` is a real seam with Linux bwrap/Landlock, macOS Seatbelt, and Windows ACL backends, and honestly reports `full` vs `partial` enforcement so callers requiring an absolute boundary can reject `partial` (`docs/subsystems/sandbox.md`). Scope is **filesystem effects only** — "Network and process visibility are outside this vocabulary." It does not replace pod-level isolation; our gVisor/Karpenter work remains the real boundary.

**Upgrade churn — quantified.** 997 commits in ~5 weeks, peaking at 171/day (2026-08-18); four prereleases in 5 days (`rc.7` 08-17 → `rc.1.1-rc.2` 08-21). `README.md:11` promises breaking changes. The SQLite backend explicitly disclaims stability: "neither schema stability nor migration support is guaranteed during pre-release development" (`packages/session/session-persistence-sqlite/README.md:57`). **Pinning is mandatory** and upgrades must be treated as migrations.

**Supply chain.** 1,550 packages in the lockfile, one patched dep (`node-pty`), Cordis vendored rather than consumed from npm (`vendor/README.md` records upstream commits). MIT throughout, with one nuance worth flagging to whoever owns license review: the Claude Code platform payloads are covered by an *identity-scoped distribution authorization* that "does not classify their declared terms as permissive" (`THIRD_PARTY_NOTICES.md:100-102`) — and each payload is large (~257 MB unpacked for darwin-arm64). Relevant only if we ever ship `subagent-claude-code`.

---

## 3. Fit with ADP — where it lands, where it collides

**Solves a current gap:** yes, but as a *reference design*, not as a component. The gap is durable multi-day session state with resume; dsh has the best articulation of it I have read, and we have none. The gap is real and independent of dsh.

**Duplicates existing work:** substantially. Approval/HITL (AIDLC gates, durably and better for our shape), compaction and cross-session memory (`agent/src/complex-task-chat/context/lcm/`, `memory/dynamo-memory.ts` — see `docs/research/openclaw-fit-assessment.md` rows 10-11), metering and budgets (gateway, which *enforces* where dsh only measures), and skills (`modules/harness/skills/`, `.claude/skills/`).

**Collides with:**
- 🔴 Tenant isolation — static MCP headers vs. header-derived ACL (Q3).
- 🟠 `modules/harness/mcp-hub/` — dsh has no MCP server surface, so it cannot be an mcp-hub tool provider; it can only be a *client* of it. Anyone assuming otherwise will design the wrong integration.
- 🟠 Lineage — dsh knows nothing of `adp-correlation` / `adp-root-human` / `adp-sig` (`agent-worker-image/lib/correlation_marker.py`, `marker_signing.py`). A dsh agent that dispatched work would break the signed chain unless adp-trigger is wrapped as a plugin **and** its markers are emitted as durable `SessionEventMap` events.
- 🟡 Cost attribution — usage rows key on `X-Agent-RunId` (`gateway/src/proxy/routes.py:133-137`). A dsh pod must send it or its spend is unattributed.

---

## 4. Relationship to sibling investigations and in-flight work

The issue asks for this explicitly. **The trio (#4160 / #4161 / #4162) partitions cleanly, and the maintainer should not read three "adopt" verdicts as three adoptions.**

- **#4162 (ORCA — parallel-agent cockpit).** ORCA is the *human supervision surface*; dsh is the *runtime*. They answer different halves of "right level of human-in-the-loop" — ORCA the human's view in, dsh the agent's pause primitive. Neither supplies the piece both need: a **durable HITL ticket** that outlives the process. Both investigations converge on the same conclusion from opposite ends, which is a strong signal that `hitl-ticket.schema.json` — planned but unwritten in `modules/harness/contracts/README.md` — is the real blocking work item. It should be prioritized ahead of either adoption.
- **#4161 (Hermes — chief-of-staff).** Different layer: per-user personal agent, not a coding-agent runtime. One genuine overlap: both would need multi-day durable session state per user. If both are pursued, that state model must be built **once** in `modules/harness/`, not twice. dsh's event-sourced log is the better reference for it than anything in this trio.
- **#4077 (orchestration graph).** dsh's `ctx.goals` (durable objective, `maxGoalRounds`, `active|paused|blocked|complete`) is a small, well-specified precedent for durable multi-round objectives. Worth reading before finalizing that design.
- **`modules/harness/` (in progress).** The most consequential relationship. Contracts are unwritten and mcp-hub is design-only (`ARCHITECTURE.md`, "Today vs. target"). Borrowing dsh's seam discipline is cheap *now* and expensive after those schemas are frozen. **This is the timing argument for acting on Option C in this cycle** even though the answer on dsh-the-dependency is "not yet."

---

## 5. Risks

| Risk | Severity | Evidence | Mitigation |
|---|---|---|---|
| Cross-tenant data access via static MCP headers | 🔴 High | `mcp-client/src/index.ts:87-88`; `agent-context/door/auth.py:5-8` | One process per tenant-user; never a shared instance |
| Unauthenticated web server on a shared network → command execution as the pod | 🔴 High | `docs/subsystems/web-server.md` (no TLS/auth/origin); `client/connection/src/index.ts:76-79,100-103` | Headless profile only; authenticating proxy + NetworkPolicy if web is ever needed |
| HITL and security posture mutually exclusive: the only HITL-capable host is the unauthenticated one | 🔴 High | `packages/sdk/protocol/README.md` (server→client = dead capability); §6 | HITL is out of scope until upstream ships auth or SDK-side answerers |
| Headless runs force `approval: 'never'` — every tool call auto-approved | 🟠 Med | `user-approval/src/index.ts:94`; `permission-presets/src/index.ts:184-190` | Enforce read-only structurally (IRSA, credentials, NetworkPolicy), not by prompt |
| Pending HITL state is process-memory only; a host restart loses it | 🟠 Med | `host/apiproxy/src/api-proxy.ts:1071-1073,1339-1346`; no `question/*` in `SessionEventMap` | Keep ADP's durable GitHub-comment gates; borrow vocabulary only |
| Breaking changes at 997 commits / 5 weeks, pre-1.0 | 🔴 High | `README.md:11`; `git log`; SQLite README:57 | Pin exact version; treat upgrades as migrations; revisit at 1.0 |
| Telemetry + identity egress to DeepSeek, not fully disableable | 🟠 Med | `identity/anonymous-user-id/README.md` | Don't mount `dsh-llm-deepseek`; restrict egress; mount redaction rules |
| Plaintext credentials on disk | 🟠 Med | `docs/user/guide/providers.md` | Localhost sigv4 sidecar; no long-lived keys in the pod |
| 1,550-package supply chain, one patched dep | 🟠 Med | `pnpm-lock.yaml`; `patches/` | Lockfile pinning; image scanning in our pipeline |
| Runtime-surface duplication if run alongside our worker | 🟠 Med | §Q5 Option B | Keep to the experiment; don't productionize without a named use case |
| Unattributed spend | 🟡 Low | `gateway/src/proxy/routes.py:133-137` | Send `X-Agent-RunId`; verify in `usage_logs` |
| Governance: single-vendor project, 13 days old | 🟡 Low | GitHub API | Ideas transfer under MIT regardless of project fate — Option C is immune |

---

## 6. Experiment spec — self-contained, reversible, independently deployable

> **Pointer (issue #4188).** This section is the spec; the run design that grounds
> it against the live tree is **[`docs/spikes/spike-4188-dsh-experiment.md`](../spikes/spike-4188-dsh-experiment.md)**
> and the results record is **[`dsh-experiment-results.md`](dsh-experiment-results.md)**.
> **The experiment has not been run** — it remains human-gated.
>
> The run design corrects six assumptions in this section that do not match what
> is actually deployed. Three matter enough to flag here:
>
> - **Test 5's substrate is unspecified below and cannot be left to the runner.**
>   dsh's persistence backends are all node-local, and the cluster has no RWX
>   filesystem; the only `ReadWriteMany` volume in the tree is Mountpoint-for-S3,
>   which provides no random writes and no file locking. Running test 5 on
>   `emptyDir` would produce a "refuted" that is really a statement about our
>   storage. See spike §4.
> - **"read-only agent-context credentials" (invariant 5, and the ⚠️ note) does not
>   exist.** The Door has one unscoped credential and no read-only mode, and 2 of
>   its **7** verbs write. Combined with headless's mandatory `approval: 'never'`,
>   the guard has to come from using a disposable tenant identity. See spike §3.5.
> - **The sigv4 "sidecar" is a subprocess of our Python entrypoint**, not a sidecar
>   container, so a dsh pod does not inherit it — and its role must be registered
>   in the agent registry or every request 403s. See spike §3.4.

**Purpose:** decide Option B (additional persona runtime) on evidence rather than argument, and validate the gateway-fronting claim. Deliberately scoped so it proves the integration questions without touching production paths.

**Hypothesis.** A dsh headless pod can run a real coding task against ADP's gateway with budgets, rate limits, and metering intact, consuming agent-context MCP tools — and its event-sourced session survives a pod kill and resumes.

**Shape.** `--profile headless` in a KEDA ScaledJob in a **dedicated namespace** (`dsh-experiment`) with its own ServiceAccount and IRSA role. No shared state, no production queue, no ingress, no listening port.

**Isolation invariants (all must hold):**
1. Dedicated namespace + SA; IRSA scoped by resource ARN, never `Resource: "*"` — the ADP standard.
2. NetworkPolicy: egress to the gateway route and the agent-context Door **only**. No `dsh-llm-deepseek`; `DSH_TELEMETRY_DISABLED` set (knowing it does not cover the provider header — hence the egress rule).
3. Single tenant-user identity for the whole experiment. The static-header blocker (Q3) makes multi-tenant testing unsafe, so it is out of scope by construction.
4. Pin `dsh-v0.1.1-rc.2` exactly.
5. Reads only. No adp-trigger dispatch, no PR creation, no writes to production tables.

**Configuration:** one custom provider row → `http://127.0.0.1:9090` (existing sigv4 sidecar, `api: anthropic-messages`), plus one `mcp-client` `streamable-http` row → the agent-context Door `/mcp` with the shared-secret and one tenant-user's identity headers. Add `X-Agent-RunId` to provider `headers`.

**Pass/fail criteria — each one falsifiable:**

| # | Test | Pass |
|---|---|---|
| 1 | Gateway fronting | Requests appear in `usage_logs` with correct `agent_run_id`; token counts non-zero |
| 2 | Budget enforcement | With budget deliberately exhausted, dsh requests are **denied** by the middleware, not silently served |
| 3 | Rate limiting | Rate-limit rejections are surfaced by dsh as errors, not swallowed into a hung turn |
| 4 | MCP tools | dsh calls ≥2 agent-context tools and uses results; ACL headers respected (verify a doc outside the tenant is **not** returned) |
| 5 | **Durable resume** — the decisive test | Kill the pod mid-task; a fresh pod resumes from the persisted log with an `interrupted` turn closed and continues without redoing prior work |
| 6 | Waste controls | Compaction and spill fire on a long task; token-meter output is coherent |
| 7 | Cache hygiene | Prompt-cache hit rate on a multi-turn run is comparable to our Claude-SDK worker |
| 8 | Compat | Note whether `compat` overrides were needed (feeds back into Q4) |

**Explicit non-goals:** multi-tenancy, HITL gates, the web UI, replacing any production worker, adp-trigger dispatch.

⚠️ **Why HITL is a non-goal, precisely — and what that forces the runner to accept.** It is not only that dsh's model is wrong for our shape (Q2); the headless profile **cannot do HITL at all**. Server→client requests are dead capability in the SDK transport, so with no answerer registered `ctx.approval` returns `unavailable` → deny, and `ask_user_question` throws `NO_PROVIDER`. The experiment must therefore run with `approval: 'never'` (equivalently the `danger-full-access` preset) — i.e. **every tool call auto-approved**. Two consequences the runner must design for rather than discover:
- Invariant 5 (reads only) stops being a policy preference and becomes the **sole** guard against unwanted writes. Enforce it structurally — read-only IRSA, read-only agent-context credentials, NetworkPolicy — not by prompt instruction.
- Sandbox scope is filesystem-only ("network and process visibility are outside this vocabulary"), so pod-level isolation is the actual boundary. This is exactly why the dedicated namespace in invariant 1 is non-negotiable.

If a future evaluation *does* need HITL, it must use the web host or the ACP bridge — the two answerers that exist — and the web host is the profile with no authentication (Q6 finding 1). That pairing is itself a finding: **today, dsh's HITL and dsh's security posture are mutually exclusive in a hosted deployment.**

**Cost.** One pod, a handful of task runs, single tenant. Bounded by the gateway budget already enforcing test #2 — the experiment cannot overspend, which is itself a nice demonstration of the property under test.

**Teardown.** `kubectl delete namespace dsh-experiment` + destroy the IRSA role. No shared infrastructure touched, no migrations, no production config changed. Rollback is deleting a namespace.

**Note for whoever runs it:** test 5 is the one that matters. Tests 1-4 I expect to pass and they mostly confirm reasoning already established above. Test 5 is what we cannot get from reading code, and it is the capability ADP actually lacks. If the experiment budget only allows one test, run that one.

---

## 7. Recommendation

### ADAPT — borrow the architecture; do not adopt the dependency. Plus one gated experiment.

**Do now (independent of dsh, valuable regardless):**
1. **File the durable-session-state work.** ADP's 6h `activeDeadlineSeconds` + ephemeral `CLAUDE_CONFIG_DIR` is the actual blocker to multi-day agents. Use dsh's event-sourced log + non-truncating crash recovery + `fork(boundary)` as the reference design. This is the highest-value item in the investigation and it is about *us*, not dsh.
2. **Write `hitl-ticket.schema.json`** (`modules/harness/contracts/`), generalizing the AIDLC gate pattern, with dsh's fail-closed outcome set and named-approve intent. #4162 needs the same primitive — build it once.
3. **Adopt seam discipline into `modules/harness/contracts/`** while the schemas are still unwritten. Cheap now, expensive later.
4. **Adopt "KV Cache effect" as a required section** in harness capability docs and the issue template. Small process change, direct low-waste payoff.

**Do next (gated):** run §6, one pod, one tenant, teardown by namespace delete. Weight test 5.

**Do not:** replace the worker entrypoint (Option A); productionize a second runtime without a named use case (Option B); expose the dsh web profile in any shared environment; give a dsh pod long-lived provider credentials; or run a shared multi-tenant instance while MCP headers are static.

**Revisit trigger — one, specific:** dsh reaching **1.0 with a stable plugin API**, at which point re-examine (a) per-session MCP identity headers, (b) whether an MCP server surface exists, and (c) whether a real use case for a second persona runtime has appeared. Absent 1.0, re-litigating this is spending architecture time on a moving target.

**One-line answer to the issue's question.** dsh brings real new value to ADP's long-running-agent objective — but as *architecture we should copy* (durable event-sourced sessions, resume/fork, spill, cache discipline, seam rigour), not as *software we should run*. Its HITL model is live-process-bound where ours is durably ticketed and better; its multi-tenancy is absent by design; its API will break. The most useful thing this investigation surfaced is not about dsh at all: **ADP's 6-hour pod deadline and ephemeral session state — not our choice of framework — are what make multi-day agents undeliverable today.**

---

## References

- Upstream: `deepseek-ai/deepseek-harness` @ `dsh-v0.1.1-rc.2`; `docs/architecture.md`, `docs/capability-seams.md`, `docs/subsystems/{session,persistence,approval,user-questions,spill,compaction,goal,jobs,sandbox,web-server,token-meter,session-telemetry}.md`, `packages/{bundle/headless,mcp/mcp-client,llm/llm-pi-ai,subagent/subagent-claude-code,identity/anonymous-user-id}`; and for the HITL/security findings specifically `packages/interaction/{user-questions,user-approval,permission-presets}/src/index.ts`, `packages/host/apiproxy/src/api-proxy.ts`, `packages/client/connection/src/index.ts`, `packages/acp/acp/src/index.ts`, `packages/core/session/src/{known-event-types.ts,repair.ts}`, `packages/sdk/{protocol,client}/README.md`
- ADP: `ARCHITECTURE.md`; `modules/harness/contracts/README.md`; `modules/agent-factory/agent/src/{agent-worker.ts,aidlc-presence.ts,utils/resilientQuery.ts}`; `modules/agent-factory/webhook-ingress/infra/{scaledjob.tf,variables.tf}`; `modules/agent-factory/agent/k8s/chat-scaledjob.yaml`; `modules/gateway/src/proxy/routes.py`; `modules/gateway/src/shared/enforced_paths.py`; `modules/agent-context/door/{auth.py,mcp_app.py}`; `docs/research/openclaw-fit-assessment.md`
- Issues: EPIC #1219; siblings #4161 (Hermes), #4162 (ORCA); #4077 (orchestration graph); #1220 (gbrain); #2079, #3231, #3232, #4073, #2792, #2809
