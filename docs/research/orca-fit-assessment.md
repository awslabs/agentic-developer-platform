# ORCA (stablyai) — Fit Assessment as the Human-in-the-Loop Cockpit for ADP

> Investigation for issue #4162, sub of EPIC #1219. Architect-led, no implementation.
>
> **Subject:** [`stablyai/orca`](https://github.com/stablyai/orca) — "The ADE for working with a fleet of parallel agents. Run any coding agent with your own subscription. Available on desktop, mobile and VPS."
> **Objective it is judged against:** highly productive, multi-day long-running agents on ADP, with the right level of human-in-the-loop, high productivity and low waste — without breaking webhook-ingress dispatch, hosted KEDA agent workers, gateway auth/metering/budgets, or the GitHub-comment control surface.
>
> **Verdict: ADAPT + permit-as-BYO-client. Do not adopt as ADP's supervision surface, and do not build an ORCA↔ADP integration.**
>
> Concretely, three separable decisions:
> 1. **ADAPT** its supervision UX patterns into ADP's own surfaces (#4077, Agent Activity) — the highest-value output of this investigation.
> 2. **PERMIT** it as an unsupported bring-your-own developer client pointed at the gateway — it already works with zero ADP change, and the gateway already meters it. Gate on one credential-scoping guard.
> 3. **REJECT** ORCA-as-cockpit-for-hosted-agents. Its control plane is client-resident by design; the thing ADP needs (a cockpit that survives the human's laptop closing) is architecturally the opposite of what ORCA is.

Evidence base: shallow clone of `stablyai/orca` at `cda2280` (2026-08-26, `main`, 295 MB working tree, TypeScript/Electron), its `docs/reference/` tree, the GitHub API for release/star history, and the live ADP tree at `cd1c99b`. Every claim cites a path. Upstream paths are relative to the ORCA checkout; ADP paths are repo-relative.

---

## 1. Summary of the candidate

| Fact | Value | Source |
|---|---|---|
| License | MIT | `LICENSE` |
| Language / shape | TypeScript, Electron desktop app (`electron.vite.config.ts`) + React Native mobile + `orca` CLI | repo root |
| Version | **`v1.4.188`** — post-1.0, stable channel | GitHub API |
| Repo created | 2026-03-17 (~5 months old) | GitHub API |
| Stars / forks / watchers | 54,149 / 3,726 / **111** | GitHub API |
| Release cadence | **66 releases in 30 days**, ~2/day, mixed stable + `-rc` | GitHub API |
| Backing | YC-backed commercial vendor (`stablyai`), topic `yc-backed` | GitHub API topics |
| Agents supported | "**any CLI agent** — if it runs in a terminal, it runs in Orca" — ~35 named, incl. Claude Code, Codex, **Hermes Agent** | `README.md` |

**What it is.** A desktop "agentic development environment": you point it at a repo, it fans a prompt across N CLI coding agents, each in its own git worktree, and gives you one place to watch them, compare diffs, annotate them, and merge the winner. Around that core: Ghostty-class WebGL terminals with restart-surviving scrollback, an embedded Chromium "Design Mode" that turns a clicked DOM element into prompt context, native GitHub/Linear browsing, SSH remote worktrees, an `orca` CLI so agents can drive ORCA itself, and iOS/Android companions for monitoring and steering from a phone.

**Two framing corrections to the issue body, both material to the recommendation:**

1. **It is not "strictly a local-process orchestrator" (the Q1 hedge), but it is also not a hosted control plane.** It ships `orca serve` for headless Linux/VPS (`docs/reference/headless-linux-server.md`) and two distinct remote models. But in the model that matters for supervision, ORCA is emphatically client-resident: *"Orchestration state (Runs, Tasks, Dispatches, mailboxes) is client-resident"* (`docs/reference/ssh-execution-boundary.md`). This is the load-bearing fact of the whole assessment — see §2 Q1.
2. **It is more mature than the sibling candidates and should not be dismissed on maturity.** Post-1.0 at `v1.4.188`, five months old, and the engineering discipline visible in `docs/reference/` is high: a written execution-boundary doc with a fixed three-value verdict vocabulary, a wire-compatibility contract with three numbered rules and a cross-version enforcement test, and an honest self-critical UX findings document. The churn is real (66 releases/30d) but it is churn on a *stable* line, not pre-1.0 instability. Where I reject ORCA below, it is never on quality.

**Notable for the trio:** ORCA lists **Hermes Agent** (sibling investigation #4161) among its supported CLI agents in `README.md`. The two investigations are not independent — see §4.

---

## 2. Answers to the seven specific questions

### Q1 — Remote/hosted attach: can ORCA supervise agents running server-side on ADP's EKS workers?

**No. Not via SSH, not via `serve`, not with a plausible amount of work.** This is the finding that decides the positioning question, so it is worth being precise about *why*, because the naive answer ("it has SSH and a VPS mode, so yes") is wrong.

ORCA has exactly two remote models, and `docs/reference/ssh-execution-boundary.md` closes with a section titled *"One host, one model"* warning that they *"imply opposite boundaries"* and must never be applied to the same machine.

**Model A — SSH worktrees ("a dumb execution host driven by your client").** The execution host runs the PTYs and git; the client keeps the control plane. The doc is explicit that this is a *deliberate* split and states the consequence plainly:

> On an SSH host, `orca` is a shim (`~/.orca-relay/bin/orca`) that proxies **back to the client's runtime** over the relay socket. […] When the client disconnects, every `orca …` command run on the SSH host fails with `No owning Orca client is connected to the relay`. The PTY stays `live`; its control plane does not.
> — `docs/reference/ssh-execution-boundary.md`

And the direct instruction against relying on it for unattended work:

> An agent on an SSH host should not depend on `orca` for anything it must finish while you are away. **Commit and push early** — unpushed work on a remote box is unavailable to the client until it reconnects.
> — `docs/reference/ssh-execution-boundary.md`

**Model B — paired runtime / peer (`orca environment`, i.e. `orca serve` on a VPS).** Here the control plane *is* host-local, which is the right shape — and the same doc recommends it for exactly our use case: *"For work that must continue while you are offline, use the peer/headless-runtime model on the remote host instead of the direct-SSH model."* But this model does not attach to *existing* processes. It requires ORCA to be the thing that **spawned and owns** the agent: PTYs are children of ORCA's own daemon (per the SSH-model description, *"children of the detached relay daemon"*), state lives in `/home/orca/.config/` on that host, and a client attaches by **pairing** to that runtime's WebSocket listener.

Neither model is an *attach* primitive. ORCA supervises processes **it forked, whose PTY it owns, in a worktree it created**. ADP's hosted agents are none of those things: a KEDA ScaledJob pod started from an SQS message, one clone per pod, no PTY, no listener, no shell for a client to dial, and a lifetime bounded by the pod.

So "ORCA as a cockpit for ADP's EKS workers" would require reimplementing ORCA's entire execution substrate as a remote-attach protocol against Kubernetes — i.e. writing the hard 80% of #4077 and then rendering it in someone else's Electron app. That is strictly more work than #4077 alone, and it lands the result inside a third-party desktop binary on a 2-releases-per-day cadence.

There is a third theoretical shape — **run `orca serve` on EKS as the agent runtime itself**, replacing the worker. That is a different proposal from the issue's (cockpit) and it fails independently on security: the runtime's own guidance is that a non-loopback listener needs care, root operation requires disabling the Chromium sandbox (*"This disables a security boundary. Prefer a dedicated unprivileged service user, especially when the listener is reachable beyond localhost"*, `docs/reference/headless-linux-server.md`), it wants Xvfb and an AppImage in a container, and it holds a *paired-device credential* per client. Against ADP's per-tenant isolation bar this is a large new attack surface for capabilities we would still have to build.

**Answer: for supervision purposes, a client-owned orchestrator of processes it spawns. It cannot be a cockpit for ADP's hosted agents.** One refinement discovered later in the read and recorded here for honesty: ORCA *does* have a durable cross-machine dispatch protocol (`federation_*`, §Q4), so "client-resident" is not the whole story. But federation runs between two *ORCA runtimes* with pinned peer fingerprints, not as an attach primitive for foreign workloads, so the conclusion for ADP's EKS pods stands. Which makes Q2 — what to steal — the valuable question.

### Q2 — Human-in-the-loop value: which supervision primitives should ADP adopt regardless?

ORCA's supervision primitives are genuinely ahead of ADP's, and the issue is right that this is where the value is. ADP's current supervision story is: GitHub comments (durable, but coarse and slow), Agent Activity (a run list), and #3959 live-run steering (in flight). ORCA has been iterating on the *watching many agents at once* problem for five months and has learned things worth taking for free under MIT.

Ranked by value-to-ADP per unit of effort — these are the recommended adoptions, and they are all **pattern** adoptions into `#4077`/Agent Activity, not code:

| # | Primitive | Why ADP should adopt it | Where it lands |
|---|---|---|---|
| 1 | **`live` / `unverifiable` / `exited` — a three-value liveness verdict where "we could not ask" is its own answer** | The single best idea in the repo, and it is a *correctness* fix for ADP, not a UX nicety. See below. | #4077 run-state model; Agent Activity |
| 2 | **Needs-attention as a first-class run state**, distinct from running and finished, with notification on entry | ADP's #4077 DoD alerts on *loop* stalls; it has no concept of "this agent is blocked on a human right now." That is the state a multi-day supervisor most needs to see. | #4077 graph node states; `hitl-ticket.schema.json` |
| 3 | **Diff annotation as the review channel** — comment on diff lines, ship the comments back to the agent as its next instruction | ADP's equivalent today is a human writing a prose GitHub comment. Line-anchored review is a strictly better instruction format for a coding agent, and GitHub PR review comments are a *durable, already-built* substrate for it. | Agent Activity / PR review loop |
| 4 | **Mobile-shaped read+nudge surface** (monitor, get notified on finish, send a follow-up) | Correct scope for a phone: watch and nudge, not administer. ADP should treat "supervise from a phone" as a small responsive projection of #4077, not a product. | #4077 dashboard |
| 5 | **Restart-surviving transcript with an explicitly bounded replay window** | ORCA replays a `REPLAY_BUFFER_MAX` 102,400-code-unit tail and is honest that *"the transcript is truncated; the work stays `live`"*. ADP's Check Run transcripts should state their truncation boundary the same way rather than implying completeness. | Agent Activity |

**On #1, because it is the one I would put in front of the #4077 author today.** The doc exists precisely because this was being got wrong:

> **No asserting what you cannot observe.** Loss of contact is not evidence of `exited`. Report `unverifiable`, never `exited`.
> […] `exited` requires positive evidence of absence from the host that owns the process; a transport failure can only ever produce `unverifiable`.
> — `docs/reference/ssh-execution-boundary.md`

And the failure mode it prevents, stated in the same doc: reporting `unverifiable` as `exited` *"orphans live work and can cold-start a duplicate over the same worktree."*

That is a live risk for #4077. A durable orchestration graph that decides "run N is dead, promote/retry" from *its own* bookkeeping — a heartbeat gap, an SQS visibility timeout, a missing pod — will eventually dispatch a duplicate agent over a run that was merely unreachable. Two agents on one issue is the ADP-shaped version of "duplicate over the same worktree": competing PRs and a corrupted lineage chain. ORCA also supplies the discriminating tests (was the signal produced by the owning host or by the client's own bookkeeping? did every channel go quiet *at once*, indicating a lost link rather than simultaneous death? does the termination event match the *current* incarnation, not a superseded one?) and the stronger-evidence-than-liveness rule: check the durable state the operation should have changed rather than trusting its return.

**One primitive to adopt with a deliberate change, not verbatim: pause/steer.** ORCA can steer a running agent because it holds the PTY's stdin — an in-process, live-connection mechanism. That is the right design for a desktop app and the wrong one for ADP: at our concurrency, a pause that holds a pod idle through an hours-long human round trip is precisely the "waste" the objective names. ADP's AIDLC gate pattern (worker commits, posts a gate comment, **exits**; a human answers by comment; a fresh dispatch resumes) is architecturally better for multi-day work and should stay the mechanism. Borrow ORCA's *vocabulary and UX* for the gate — needs-attention state, notification on entry, line-anchored response — and keep ADP's durable, compute-free ticket underneath. This is the same conclusion the dsh investigation (#4160) reached from the runtime side, which is a meaningful convergence: see §4.

### Q3 — Overlap/conflict with ADP's GitHub integration

**The good news first: ORCA cannot double-trigger ADP, and the reason is structural, not lucky.** ORCA consumes **no** GitHub webhooks and does **no** GitHub polling for work. Its entire GitHub surface is outbound shell-outs to the developer's own `gh` CLI (`src/main/git/command-runner/gh-exec-file.ts:95-153`; the only `api.github.com` reference in the tree is the auto-updater's release feed, `src/main/updater-release-builds.ts:18`). There is no inbound listener, no webhook receiver, and no reactive loop. ORCA browses GitHub because a human clicked; it never wakes up because GitHub changed. So the "two systems both dispatching agents from GitHub events" collision the issue worries about **does not exist**.

**The real collision is narrower, is genuinely there, and is worth naming precisely: ORCA can add labels, and ADP dispatches agents from labels.**

`src/main/github/issue-update.ts:90-112` builds a `gh issue edit` invocation supporting `--add-label`, `--remove-label`, `--add-assignee`, `--remove-assignee`, `--title`, plus `issue close`/`reopen` (`:44-54`) and project-board mutations (`project-view/mutations.ts:111`). ADP's dispatcher maps an added label straight to a persona and dispatches:

> `# issues + labeled → map label to persona`
> — `modules/agent-factory/webhook-ingress/lambda/github/intent_parser.py:182-184`, dispatching via `_handle_issue_labeled` → `Intent(persona=persona, trigger="issue_labeled", label=label_name)` (`:332-342`)

So a developer using ORCA's issue panel to triage — dragging a card, adding a label to organize their board — can **dispatch a hosted ADP agent without ever intending to**, from a UI that gives no indication that a label is an execution trigger. This is not ORCA misbehaving; it is a UI whose semantics are "organize" driving a backend whose semantics are "execute." The same hazard exists with any GitHub client (the web UI included), but a fleet-management tool that makes bulk triage fast makes accidental bulk dispatch equally fast.

**Second collision: identity and lineage.** ORCA's writes carry the **developer's own** `gh` credential, not ADP's GitHub App. `docs/reference/ssh-execution-boundary.md` confirms this is true even for remote work, and flags it as a known inconsistency in its own model:

> | `gh` / GitHub API, `glab` / GitLab | **client** | inconsistent with the rule; PRs carry the client's identity |

Consequences for ADP, in order of severity:

- 🟠 **Lineage break.** ADP's provenance chain rides HTML-comment markers (`adp-correlation`, `adp-root-human`, `adp-is-human-rooted`, `adp-chain-depth`, `adp-sig`) that ADP's own agents emit and sign. An ORCA-authored comment or PR carries none of them. A human-authored comment legitimately has no markers either, so this is *consistent* with how ADP treats humans — the problem is that an ORCA action may be an *agent's* work wearing a human's identity, and nothing distinguishes the two. If ADP ever tightens provenance to "agent-authored artifacts must carry a signed chain," ORCA-mediated agent output is indistinguishable from hand-written human output.
- 🟠 **Competing PRs.** A developer's ORCA-driven local agent and a hosted ADP agent can be working the same issue simultaneously, opening two PRs under two identities with no shared lock. Nothing in either system detects it. This is the "duplicate over the same worktree" hazard from Q2 at the *organizational* level.
- 🟡 **Attribution.** Work done by an ORCA-launched agent appears in GitHub as the developer's own activity, so review and audit cannot see that an agent wrote it.

**De-confliction — concrete, cheap, and mostly on ADP's side:**

1. **Require an explicit dispatch signal that a triage UI won't produce by accident.** The cleanest version: dispatch on a *comment mention* (`@agent-architect`) as the primary path and treat label-dispatch as the narrower, more deliberate one — noting ADP already supports both. Failing that, use a label name no human would add casually, and never one that doubles as a triage/organizational label. This is worth deciding regardless of ORCA, because the hazard belongs to label-as-trigger itself.
2. **State the BYO-client boundary in the developer docs**: your local ORCA agents open PRs as *you*; hosted ADP agents open PRs as the App. Don't drive the same issue from both. A documentation fix, not a code one.
3. **Do not connect ORCA's GitHub panel to ADP's flows** — no ADP-specific labels surfaced in it, no ORCA-side automation of ADP triggers. Keep the two GitHub-facing systems ignorant of each other; that ignorance is what keeps them from fighting.
4. **If provenance is ever tightened, decide deliberately** whether an unmarked artifact means "human" (current, safe-ish) or "reject." ORCA makes the "unmarked ≠ human" case concrete for the first time and is a reason to write that rule down.

**No overlap worth de-conflicting on the *read* side.** ORCA browsing PRs/issues in-app is a strict convenience for the developer and touches nothing ADP owns.

### Q4 — Relation to the orchestration-graph work (#4077)

**This is where the investigation produced its biggest surprise, and it inverts the answer I expected. ORCA has already built a working version of #4077's Scopes A and B — a durable, versioned, engine-enforced orchestration graph with human gates — and it is not mentioned in the README, the feature wall, or the marketing. It is the most valuable artifact in this repo for ADP, and it is not the cockpit.**

`src/main/runtime/orchestration/` is a ~150-file subsystem backed by **SQLite with a 28-version migration chain** (`db/contract-constants.ts:9` enumerates them: *"v2 'heartbeat'+last_heartbeat_at, v3 delivered_at, … v9 durable question threads, v10 Dispatch capabilities, v11 durable mutation receipts, … v27 durable federation acknowledgments, v28 durable local mutation caller identity"*). Read against #4077's three scopes:

| #4077 scope | ORCA counterpart | Evidence |
|---|---|---|
| **A. Loop state as data** (waves=nodes, deps=edges, queryable position) | `tasks` table with `deps TEXT`, `status IN ('pending','ready','dispatched','completed','failed','blocked')`; `dispatch_contexts`; `runs`; `coordinator_runs` | `db/schema/create-graph-tables-sql.ts:88-169` |
| **B. Engine-enforced gates** (block until human approval) | `decision_gates` table — `status IN ('pending','resolved','timeout')`, and the coordinator **re-blocks** any gated task that drifted out of `blocked` | `create-graph-tables-sql.ts:137-152`; `coordinator-decision-gates.ts:51-60` |
| **B. Bounded cycle with a terminal** ("N defect cycles → halt") | Dispatch **circuit breaker**: `DISPATCH_CIRCUIT_BREAK_FAILURES = 3`, `SET status = CASE WHEN failure_count + 1 >= ? THEN 'circuit_broken' ELSE 'failed' END` | `db/dispatch-context/dispatch-circuit-breaker.ts:2`; `dispatch-completion.ts:87` |
| **C. Liveness / stall detection** | `last_heartbeat_at` on `dispatch_contexts` + `getStaleDispatches()` with an explicit first-interval grace | `dispatch-completion.ts:55,60-74` |

Three details raise this from "interesting" to "read this before finalizing #4077":

**1. The gate invariant is enforced, not trusted — and the reason is stated in the code.** #4077's motivating evidence is that promotion was withheld *"because the agent chose to honor 'green promotes,' not because a mechanism required it."* ORCA's comment is the direct answer:

> `// Why: the coordinator never auto-resolves gates (humans do, via orchestration.gateResolve) — that would defeat them as approval checkpoints.`
> — `src/main/runtime/orchestration/coordinator-decision-gates.ts:51`

and the following loop repairs drift rather than assuming it away: *"gate exists but task isn't blocked — re-block to restore the invariant"* (`:56`). That is #4077's Scope B in eight lines, including the failure mode it must survive.

**2. The `unverifiable` discipline from Q2 is not just a doc — it is in the durable schema.** `worker_dispatches.state` is `('starting','ready','start_unknown','failed','succeeded','stopping','stop_unknown','stopped','abandoned')` (`create-graph-tables-sql.ts:33-38`). Both transitions where an operation's outcome can be genuinely unobservable get their **own persisted state** (`start_unknown`, `stop_unknown`) rather than being collapsed into success or failure. Compare `worker-dispatch-outcome.ts:98` (`SET state = 'start_unknown', stage = ?`) and `worker-dispatch-stop.ts:212`. **This is the single most transferable thing in the investigation:** a durable orchestration graph needs "I asked and could not learn the answer" as a first-class node state, or the engine will eventually promote/retry past a run that was merely unreachable and double-dispatch it. #4077's DoD (*"`resume` is idempotent"*, *"a loop-level stall raises an alert"*) is exactly where that bug lives.

**3. `federation_*` is a durable remote-dispatch protocol with an acked message log — and it partially revises Q1.** `federated_dispatches` + `federation_relay_items` (`PRIMARY KEY (dispatch_id, direction, sequence)`, `acked_at`, monotonic `to_home_imported_sequence` / `to_home_acknowledged_sequence`) implement at-least-once ordered delivery between a "home" runtime and a remote worker runtime, with peer identity pinned by `peer_fingerprint` (a SHA-256 of the peer public key, `environment-transport.ts:26`) and a `peer_changed` error when it moves (`federation-sync.ts:66-70`). And `remote_questions` (`status IN ('pending','answered','closed')`, `answer_body`) is **a durable HITL ticket that survives disconnection** — precisely the primitive both this investigation and #4160 concluded ADP is missing.

So the honest Q1 refinement: **ORCA's *federation* model can span machines durably; ORCA's *cockpit* still cannot attach to a process it did not spawn.** Federation is runtime-to-runtime between two ORCA runtimes that agree on a protocol version and exchange fingerprinted identities — not an attach primitive for arbitrary foreign workloads. It does not make Option B viable (ADP's pods are not ORCA runtimes, and making them so is the §Q7 Option B rewrite). But it does mean the *design* to copy for durable cross-host agent supervision exists here in working, migration-tested form.

**What ORCA does *not* have, so #4077 keeps its justification.** No config-by-reference (#4077's Scope B "config trap" — the deploy-account-in-14-issue-bodies problem — has no ORCA counterpart); the graph is per-desktop-install with no multi-tenant or shared-team view; no cost/budget dimension anywhere (§Q6); no cross-run analytics; and decomposition is explicitly unbuilt (*"decomposition isn't implemented yet — tasks must be pre-created before run(); AI-driven decomposition is a future phase"*, `coordinator.ts:145`). ORCA also has no notion of ADP's signed lineage. #4077 remains the right build; it should be built having read this.

**Revised answer to the question as asked.** ORCA **validates #4077's direction more strongly than the issue anticipated** — not as market evidence for a dashboard, but as a working existence proof that the durable-graph-plus-enforced-gates design converges when you actually run fleets of parallel agents. It **provides adoptable patterns** (three above, plus Q2's five). It does **not** argue for "recommend ORCA as the client and focus ADP elsewhere" — that fails on Q1. The concrete recommendation is narrow and cheap: **before #4077's schema is frozen, read `create-graph-tables-sql.ts` and `coordinator-decision-gates.ts`, and adopt the `*_unknown` state discipline and the never-auto-resolve gate invariant as explicit requirements** rather than leaving them to the implementing agent to rediscover.

**MIT-licensed components: technically available, not recommended.** ORCA is MIT throughout (`LICENSE` — "Copyright (c) 2026 Lovecast Inc."), so lifting code is legally clean. It is practically unattractive: Electron/React-Native-shaped, coupled to ORCA's client-resident state model and PTY substrate, ~2 releases/day, so any lift is an immediate hard fork. Take the ideas and the vocabulary; write the components against ADP's own state model. The schema is the one exception worth reading closely — SQL and state vocabularies transfer across stacks in a way UI components do not.

**Relationship to #3959 (live-run steering) and #3970 (dashboard-as-control-surface).** #3959 is the layer where ORCA's steering ideas belong, with the durability change from Q2. #3970's principle — *"driveable with zero pod access"* — is worth restating as the acceptance test that disqualifies ORCA-as-cockpit: ORCA drives agents by owning their PTY, which is the opposite of zero-pod-access, and is why it cannot be that surface for us.

### Q5 — Auth path: credential-handling concerns

**Yes, there are real concerns, and they are not the ones the issue anticipated.** The issue asks about ORCA holding gateway credentials. The finding is stranger and worse: ORCA's credential model is *architecturally hostile* to a corporate gateway, and its default launch posture is a permission bypass. Both are load-bearing on the "permit as BYO client" half of the verdict.

**Finding 1 — ORCA does not inject an API key; it manages *copies of the agent's own credential files*, and it rewrites the developer's real ones in place.** ORCA's "hot-swap accounts without re-logging in" feature (`README.md`) works by overwriting `~/.claude/.credentials.json` (`src/main/claude-accounts/runtime-auth-service.ts:1740-1758` → `writeFileAtomically(credentialsPath, contents, { mode: 0o600 })`; path from `runtime-paths.ts:21`) and reading/writing `~/.codex/auth.json` (`src/main/codex-accounts/service.ts:1062`; write-back at `runtime-home-service.ts:1777-1781`). It deliberately chose in-place rewrite over config-dir isolation:

> `/** Why: persist only per-account auth (not a CLAUDE_CONFIG_DIR swap) so switching accounts doesn't fork Claude's shared chat/session context. */`
> — `src/shared/global-settings-types.ts:281`

ORCA also **owns the OAuth refresh itself**, against a hardcoded Claude Code client ID: `OAUTH_TOKEN_URL = 'https://platform.claude.com/v1/oauth/token'`, `OAUTH_CLIENT_ID = '9d1c250a-…'` (`src/main/claude-accounts/oauth-refresh.ts:9-10`), because *"Orca owns the refresh so a single-use refresh token is rotated and persisted atomically"* (`:5-8`). The stored copies are **macOS Keychain on darwin, but plaintext `0o600` files on Windows and Linux** (`src/main/claude-accounts/claude-managed-auth-storage.ts:76-81`; `managed-auth-path.ts:9-11`), and Codex `auth.json` is plaintext on **all** platforms (`src/main/codex-accounts/service.ts:1204-1207, 828-831`). `electron.safeStorage` exists in the tree (`src/main/host/electron-secret-store.ts:8-20`, wrapped by `src/shared/secret-store.ts`) and is used for a *three-slot* allowlist — `opencodeSessionCookie`, `httpProxyUrl`, `browserKagiSessionLink` (`src/main/protected-secret-persistence.ts:3-7`) — but **not** for agent credentials.

**Finding 2 — the direct gateway conflict. ORCA refuses to launch, or silently strips, exactly the env vars ADP's gateway path needs.** `src/main/claude-accounts/environment.ts:1-6` defines `CLAUDE_AUTH_ENV_VARS = ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'AWS_BEARER_TOKEN_BEDROCK']`, and with a managed account active (`stripAuthEnv: true`, `runtime-auth-service.ts:662`) it **deletes all four** plus any auth-shaped `ANTHROPIC_CUSTOM_HEADERS` (`environment.ts:18-25`, matcher at `:46-51`). If the user sets one explicitly, the spawn is rejected outright:

> `'This Claude launch defines explicit Anthropic auth environment variables. Remove those overrides before using a managed Claude account.'`
> — `src/main/ipc/pty/runtime/spawn-preflight.ts:140-143` (and `src/main/ipc/pty/ipc/spawn-env.ts:22-27`)

This matters concretely for ADP because our shipped `/setup` flow authenticates Claude Code with an **`apiKeyHelper`** in `~/.claude/settings.json` — `"apiKeyHelper": "bash ~/bin/bg-cognito-auth.sh token"` with `apiKeyHelperTtlMs: 3300000` (`modules/gateway/frontend/src/components/setup/SetupInstructions.tsx:32-44`; helper at `modules/gateway/cli/bg-cognito-auth.sh:739-774`) — plus `ANTHROPIC_BASE_URL` or the Bedrock pair `CLAUDE_CODE_USE_BEDROCK=1` + `ANTHROPIC_BEDROCK_BASE_URL` (`SetupInstructions.tsx:35, 51-53`). The `apiKeyHelper` path is *settings-file-based*, not env-based, so it survives ORCA's strip — the reason the "permit" verdict works at all. But it survives by accident, not by design, and it interacts badly with Finding 1: ORCA rewrites `.credentials.json` in the same directory it is reading `settings.json` from, and a managed-account switch can install an OAuth identity alongside our helper. **The failure mode to test in any pilot is silent fail-open: ORCA strips or out-ranks the gateway credential and the CLI authenticates direct-to-Anthropic instead — inference that leaves the tenant entirely unmetered, with no error.**

**Finding 3 — a gateway can only be pinned as unvalidated free text, globally, per agent — never per repo.** The only mechanism is Settings → per-agent "Environment": a whitespace-split `KEY=VALUE` box with no name or value validation (`src/renderer/src/components/settings/agent-default-env-draft.ts:17-35`; UI `AgentLaunchDefaultsEditor.tsx:129-137`), persisted at `agentDefaultEnv` (`src/shared/global-settings-types.ts:371`) and merged into the PTY env at launch (`resolveTuiAgentLaunchEnv` → `src/shared/tui-agent-startup.ts:95`). `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL` appear **nowhere in ORCA's production code** — there is no first-class provider/gateway/proxy concept (the only `baseUrl` in `src/shared` is Bitbucket's, `bitbucket-credentials.ts:12`). And `orca.yaml` cannot set env at all: its schema is `scripts`, `issueCommand`, `defaultTabs`, `environmentRecipes`, `worktree.sharedDirectories` (`src/shared/orca-yaml.ts:225-260`). **So a repo cannot pin its gateway; an org cannot enforce one.** Codex is stricter still — ORCA refuses to add an OAuth account while `~/.codex/config.toml` pins a custom provider (`src/main/codex-accounts/service.ts:1415`).

**Finding 4 — permission bypass is the shipped default, for 25+ agents, and it is back-filled into existing profiles.** `src/shared/tui-agent-permissions.ts:6-30` maps every supported agent to its most permissive flag — Claude to `--dangerously-skip-permissions`, Codex to `--dangerously-bypass-approvals-and-sandbox` (which disables its *sandbox*, not just approvals), Gemini/Cursor/Copilot/Hermes to `--yolo`, and so on. These are not opt-in:

> `export const DEFAULT_TUI_AGENT_ARGS: Partial<Record<TuiAgent, string>> = YOLO_TUI_AGENT_ARGS`
> — `src/shared/tui-agent-launch-defaults.ts:10`

Onboarding ships the checkbox pre-ticked (`src/renderer/src/components/onboarding/AgentStep.tsx:67`, `yoloPermissions = true`, labelled "Yolo / Dangerously skip permissions"), and `agentYoloDefaultsMigrated` (`global-settings-types.ts:373`) is a one-shot migration that *adds* the flags to profiles that never asked for them. Separately, `src/main/agent-trust-presets.ts:38-118` pre-writes folder-trust markers into `~/.cursor/projects/<slug>/.workspace-trusted`, `~/.copilot/config.json`, and `~/.codex/config.toml`. Net: an ORCA-launched agent runs arbitrary commands on a developer laptop with zero prompts, and **the only control is a per-user toggle — there is no policy, MDM, or org-level lock.** For balance, this is the same posture ADP's own hosted workers run under (`permissionMode: 'bypassPermissions'`, `modules/agent-factory/agent/src/agent-worker.ts:1199`) — but ADP's runs inside a single-tenant ephemeral pod with `restartPolicy: Never`, not on a laptop holding the developer's SSO session, cloud creds, and every repo they have checked out. Same flag, categorically different blast radius.

**Finding 5 — hardcoded provider egress that ignores the gateway.** Usage tracking calls consumer endpoints directly with the user's OAuth token and honors no base-URL override: `https://api.anthropic.com/api/oauth/usage` (`src/main/rate-limits/claude-oauth-usage-request.ts:8`), `https://chatgpt.com/backend-api/wham/usage` (`codex-backend-usage-client.ts:68`), plus `platform.claude.com` for refresh. Add ORCA's own: `login.onorca.dev` / `relay.onorca.dev` (`src/main/orca-profiles/profile-cloud-auth-config.ts:19-21`), `share.onorca.dev`, `www.onorca.dev/v1/feedback` (`src/main/ipc/feedback.ts:17`), and `https://us.i.posthog.com` (`src/main/telemetry/client.ts:107-113`). **A strictly-Bedrock-only ADP deployment will still see connections to consumer AI endpoints and a US analytics tenant from any laptop running ORCA.**

**Finding 6 — "AI Vault" is not a credential vault, and its cache is plaintext.** Worth stating because the name will mislead any reviewer who sees it in a security discussion. It is a read-only agent-session/transcript browser across 17 agents (`src/shared/ai-vault-types.ts:4-21`; user-facing name "Agent Session History", `src/relay/ai-vault-handler.ts:41`). It stores **no** credentials. What it *does* store is a plaintext JSON cache of transcript-derived preview text under `userData`, protected only by mode bits, with the tree's own comment conceding they don't help on Windows:

> `// The payload contains transcript-derived preview text; keep it user-only`
> `// (mode bits are inert on Windows — the userData ACL grant is the boundary there).`
> — `src/main/ai-vault/session-parse-cache-persistence.ts:19-22`

Its process isolation is explicitly *not* a security boundary: *"Treating the service process as a security sandbox. It runs trusted Orca code with the same user identity"* is listed under **non-goals** (`docs/ai-vault-process-isolation-plan.md:34`).

**What ORCA does well, stated for fairness, because it bears on the verdict.** Telemetry is opt-in for new users, honors `DO_NOT_TRACK` and `ORCA_TELEMETRY_DISABLED` machine-wide with `DO_NOT_TRACK` winning by documented precedence (`src/main/telemetry/consent.ts:76-90`), fails closed on non-official builds (both a CI-injected build identity *and* a write key are required — `client.ts:33-36`), and its event schemas are `zod` `.strict()` closed enums that **structurally cannot** carry prompt text, paths, or error strings (`src/shared/telemetry-events.ts:354-363` — `agent_prompt_sent` carries only `{agent_kind, launch_source, request_kind, nth_repo_added}`; `:363` *"Enum-only by design: `.strict()` blocks `error_message`/`error_stack`"*). Crash upload is hard-off with no `submitURL` configured (`src/main/crash-reporting/crashpad-capture.ts:65-68`); diagnostic-bundle upload is user-initiated to a build-pinned, un-redirectable endpoint; and a three-location regex redactor scrubs `sk-ant-`, `gh[pousr]_`, `AKIA…`, JWTs and PEM blocks (`src/main/observability/redactor.ts:14-40`). The mobile relay is genuinely E2EE — X25519 + HKDF-SHA256 with a transcript hash, directional keys, the phone pinning the desktop public key from the QR offer and rejecting a mismatch (`src/main/runtime/rpc/mobile-e2ee-v2-key-schedule.ts:3-32`; `mobile/src/transport/mobile-e2ee-v2-client-session.ts:64`), and v2 is *mandatory* over relay (`mobile-socket-wiring.ts:144-160`). The relay operator sees ciphertext plus metadata. **Caveats:** the redactor is regex-based, so a Cognito access token — a JWT, therefore caught — is fine, but a novel corporate token shape may survive; the relay is **not self-hostable** (`ORCA_RELAY_URL` is HTTPS-or-loopback only and loopback-HTTP is blocked in packaged builds, `profile-cloud-auth-config.ts:41-45,73`; no relay server source ships) and requires an Orca Cloud sign-in; and the desktop's long-term X25519 secret key is plaintext JSON, not safeStorage-sealed (`src/main/runtime/e2ee-keypair.ts:15-18`).

**Bearing on ADP's credential model.** ADP's `/setup` credential is a **30-day Cognito refresh token in a `chmod 600` plaintext file** at `~/.bedrock-gateway/tokens.json` (`modules/gateway/cli/bg-cognito-auth.sh:118-127`), exchanged for 60-minute access tokens (`modules/gateway/infra/variables.tf:175-185`) — the UI says so plainly (`ConnectCliPanel.tsx:86-90`, *"This is a long-lived credential"*). Our own local Codex proxy is loopback-pinned and non-configurable by design (`modules/gateway/cli/bg-gateway-proxy.py:53-56`, `BIND_HOST = "127.0.0.1"`, *"a proxy that injects the user's credential must never be reachable from the LAN"*). So ADP is not in a position to lecture ORCA about plaintext token files. The asymmetry that *does* matter is scope: ADP's laptop credential is one tenant-scoped, 60-minute-derived token that the gateway meters, budgets, and can revoke. ORCA's is N consumer OAuth refresh tokens for N accounts in one directory, refreshed by ORCA against a hardcoded client ID, on a machine where every launched agent runs with permissions bypassed.

**The one guard the verdict is conditional on.** Anything that speaks Anthropic Messages / OpenAI chat-completions / OpenAI Responses / Bedrock `InvokeModel` and can set a base URL and a bearer token can use ADP's gateway; nothing in the enforced path inspects User-Agent or otherwise identifies the client (`modules/gateway/src/shared/enforced_paths.py:24-34`). Requests without `X-Agent-RunId` are **admitted and metered**, not rejected — the header is set to `None` and the row is written with `agent_run_id = NULL` (`modules/gateway/src/proxy/routes.py:133-146`; column nullable at `src/shared/models/usage.py:28`). Identity itself is safe: `org_id`/`user_id`/`team_id` come only from the authenticated `TokenContext`, never from headers (`src/usage/service.py:70-85`), and the header-trusting path that once allowed asserting an arbitrary `org_id` was removed in #3985 (`proxy/routes.py:159-163`). So ORCA traffic is correctly *attributed* — it is just indistinguishable from hosted-agent traffic whose header was dropped, and uncapped per attempt (§Q6).

Therefore: **permit ORCA as a BYO client, conditional on ADP making local-client traffic distinguishable and bounded** — a client-class marker on the CLI credential path (so "developer laptop" is a queryable dimension in `usage_logs`, not an absence), plus the per-attempt cap from Q6. Both are ADP-side, cheap, and worth doing for every BYO client, not just this one. And in the developer docs, three sentences: ORCA launches agents with permissions bypassed by default; it will strip `ANTHROPIC_AUTH_TOKEN`-style env credentials if you use its account switcher, which can silently fail your inference open to direct-to-provider; use the `apiKeyHelper` path from `/setup` and verify with one metered request that traffic is landing on the gateway.

### Q6 — Best-of-N economics

**Best-of-N is ORCA's headline feature, it has no cost controls, and ADP's budget layer is per-tenant rather than per-attempt. That combination is the one place where merely *permitting* ORCA has a real downside, and it is cheap to fix.**

The mechanics first: ORCA's pitch is *"Fan one prompt across five agents, each in its own isolated git worktree — compare the results and merge the winner"* (`README.md`). A 5-way fan-out is 5× the tokens for 1× the merged result, and the discarded 4 are pure waste in the objective's terms — unless the compare step buys more than 5× the value, which is exactly the question the issue asks.

**When fan-out is worth it.** The honest answer is that it is worth it when *verification is cheap and much cheaper than generation*, and the outcome distribution is wide. Concretely, fan-out earns its cost when:
- there is a **mechanical oracle** — tests, a build, a lint gate, a benchmark — so "best" is measured, not adjudicated by the human reading five diffs;
- the task has **high variance across attempts** — a tricky bug, an unfamiliar subsystem, a design with several defensible shapes — rather than a mechanical change where all five attempts converge;
- the human's time is the binding constraint, not tokens. This is the real economics: one 5× token bill is cheap against a developer waiting a day for a single attempt to fail. Fan-out is a *latency* purchase, and should be justified as one.

**When it is waste.** All five attempts produce near-identical diffs (mechanical refactors, doc edits, well-specified small changes); "best" requires a human to read five diffs carefully (the compare cost is then the dominant cost, and it scales with N, so the human becomes the bottleneck the fan-out was supposed to relieve); or the task is long-horizon and multi-day, where 5 parallel multi-day agents is a large bill accruing for days before anyone can compare anything.

That last case matters most here, because it is EPIC #1219's case. **Best-of-N and multi-day are in tension, and ADP should say so.** Fan-out's value comes from cheap early comparison; a multi-day agent offers nothing to compare until late. The defensible composition is fan-out **at bounded checkpoints** — fan out 3 ways on the *plan*, or on the first wave, compare at a gate, then continue single-track — not 5 agents running for 3 days in parallel. That is a #4077-shaped capability (a fan-out node with a comparison gate and a declared promotion rule), and it is a better home for the idea than a desktop app.

**Guardrails ADP would need — and the gap.** The gateway's enforcement is per-tenant/per-key. Nothing caps *one attempt*, which is what fan-out multiplies. Required, in order:

1. **Per-run / per-attempt spend cap.** A run declares a ceiling; the gateway denies over it. This composes with the existing `X-Agent-RunId` attribution — the key already exists, the ledger does not. **This is the one guardrail I would require before encouraging fan-out at all**, and it is independently useful for runaway single agents, which is the more common failure.
2. **Early kill.** A fan-out node kills the remaining N−1 as soon as one attempt satisfies the oracle. Without this, fan-out always pays full N× even when attempt 1 was correct in five minutes. ORCA has nothing here; #4077's transition table is the right place for it.
3. **Bounded N, declared per task class.** N as a policy value, not a slider a developer nudges to 8 on a Friday.
4. **Attempt-level attribution in metering.** Fan-out must show up as N attributed attempts under one parent run, or cost review cannot distinguish "one expensive agent" from "five cheap ones" — and the fan-out policy above cannot be tuned against evidence.

Note the asymmetry that makes this urgent even under the permissive verdict: a developer running ORCA against the gateway *today* can fan out 5 ways, and the gateway will meter it correctly and bill it to their tenant, with no per-attempt ceiling and no early kill. The metering works; the guardrail does not exist.

### Q7 — Positioning

Four options plus the do-nothing case.

**Option A — Adopt ORCA as ADP's recommended developer cockpit. 🔴 Reject.**
Fails on Q1: it cannot show ADP's hosted runs, so it would not address EPIC #1219's actual gap while appearing to. Recommending it also implies a support commitment for a third-party Electron binary on a ~2-release/day cadence, with a vendor-operated relay in the mobile path, holding gateway credentials on developer laptops. The support surface is large and the gap stays open.

**Option B — Integrate: ORCA client ↔ ADP hosted agents. 🔴 Reject.**
Requires a remote-attach protocol ORCA does not have and whose absence is *designed* (§Q1, "One host, one model"). We would build the hard part of #4077 and render it in a third party's UI, coupled to a wire contract we do not control. Strictly worse than building #4077.

**Option C — Adapt the patterns into ADP's own surfaces + permit as unsupported BYO client. ✅ Recommend.**
Two independent, cheap moves:
- **Adapt** (the valuable half): the Q2 primitives into #4077 and Agent Activity — three-value liveness verdicts, needs-attention as a run state, line-anchored diff annotation as the review channel, honest transcript truncation, mobile as a read+nudge projection. Land these while #4077 is still design and `modules/harness/contracts/` is still unwritten (`contracts/README.md`: *"some are stubs, some are not yet written"*; `modules/harness/README.md:123` — *"Schemas not yet written"*). Patterns are free under MIT and cost nothing to reverse.
- **Permit**: ORCA already works against the gateway with zero ADP change, because it launches the same CLI agents our shipped `/setup` flow configures. Developers will do this whether or not we bless it. The right posture is explicit-but-unsupported, plus the one credential guard from Q5 and the per-attempt budget cap from Q6. Do **not** document it as a recommended path, and do not carry it in CI.

**Option D — Revisit later. ⚠️ Yes, narrowly, with a named trigger.**
The one upstream change that would reopen Option B is a genuine **remote-attach** capability — a documented way to observe and steer a run ORCA did not spawn. Nothing in the repo suggests it is coming; the execution-boundary doc argues the other way. Revisit if that appears, or if a self-hostable relay ships (which would remove the mobile-path concern). Not a hedge to buy now.

**Do-nothing baseline, costed honestly.** Doing literally nothing costs: (a) #4077 ships without the `unverifiable` verdict and eventually double-dispatches a live run — a real correctness bug we can prevent for the cost of one paragraph in an issue; (b) developers run ORCA against the gateway anyway, unguarded, with unbounded fan-out; (c) we keep re-deriving supervision UX that a 54k-star project has already iterated. Note what do-nothing does *not* cost: no standardization lock-out (ORCA is a client, not a protocol), and no capability loss (it cannot see our hosted runs regardless). The cost is real but it is all in (a) and (b), and both are addressed by Option C's cheap half.

---

## 3. Fit with ADP

Read against the objective — multi-day productive agents, right level of human-in-the-loop, low waste, nothing that works gets broken.

### What ORCA solves that ADP genuinely lacks

| Gap in ADP | Evidence it is a gap | What ORCA has |
|---|---|---|
| **No in-flight visibility into "this agent needs me right now"** | Agent Activity is strictly read-only — every endpoint in `modules/gateway/src/activity/routes.py` is `@router.get` (lines 65, 96, 324, 397, 462, 496, 540, 578, 666, 696); no POST/PUT/PATCH/DELETE exists in the file | `blocked`/`waiting` as first-class agent states (`src/shared/agent-status-types.ts:23`) sourced from agent hooks, never inferred; notification on entry |
| **No liveness vocabulary for "we could not ask"** | ADP infers death from an SQS visibility timeout (`sqs_visibility_timeout` default 300, `webhook-ingress/infra/variables.tf:26`) decoupled from the 6h `activeDeadlineSeconds` (`scaledjob.tf:161`, `variables.tf:168-172`) — a heartbeat gap and a dead worker are the same signal | `unverifiable` as a distinct verdict (`docs/reference/ssh-execution-boundary.md`), and `start_unknown`/`stop_unknown` as durable states (`create-graph-tables-sql.ts:33-38`) |
| **No engine-enforced human gate** | ADP's AIDLC gate is prompt-driven — `rules/personas/aidlc.md` instructs the agent to stop; `docs/design-3159-aidlc-v2-hosted-agents.md:70` states "v1 gate enforcement is prompt-driven" | `decision_gates` with a coordinator that never auto-resolves and *re-blocks* drifted tasks (`coordinator-decision-gates.ts:51-60`) |
| **No durable HITL ticket** | `modules/harness/contracts/` contains **only** `README.md`; `hitl-ticket.schema.json` does not exist; `modules/harness/README.md:119-131` — "Other surfaces (Jobs, Events, Artifacts, HITL): Not yet started" | `remote_questions` — `status IN ('pending','answered','closed')`, `answer_body`, survives disconnection (`create-graph-tables-sql.ts`) |
| **No parallel-attempt substrate** | Repo-wide, ADP uses **no** git worktrees; one clone per pod, `shutil.rmtree(WORK_DIR)` then `git clone --depth=20` (`agent-worker-image/entrypoint.py:44, 1101-1119`) | N isolated worktrees per prompt, with a compare-and-merge review surface |

### What ORCA duplicates — where ADP is already equal or better

- **Dispatch.** ADP's is a hardened single-enforcement-point pipeline: every trigger adapter funnels through `spawn_persona.py`, with FIFO `MessageGroupId = f"{tenant_id}#{repo}#{issue}"` serializing runs per issue (`sqs_publisher.py:70`), worker-side merged-PR idempotency (`entrypoint.py:353-395`), `MAX_CHAIN_DEPTH = 8` and `CROSS_PERSONA_LOOP_THRESHOLD = 4` (`intent_parser.py:51,57`), and blocked deliveries still recorded as audit rows (`spawn_persona.py:661-749`). ORCA has no equivalent because it has no unattended dispatch — a human clicks.
- **Durable gate mechanics.** ADP's commit-and-exit gate holds **zero compute** across a human round trip: the worker commits `aidlc/` state, posts `<!-- aidlc-gate:<stage> -->`, and exits (`aidlc-gate-enforcer.ts:68-136`); the human's reply is a fresh webhook → fresh pod. The design doc explicitly rejected the ORCA-shaped alternative: *"Blocking in-run gate (long-poll for comment): Violates the 15-min pod timeout"* (`docs/design-3159-aidlc-v2-hosted-agents.md:279`). **ADP's mechanism is better for multi-day work than ORCA's; only the UX around it is worse.**
- **Provenance.** ADP has signed lineage (HMAC-SHA256 markers, `marker_signing.py`, verified tri-state at `marker_verify.py:134-173` with `AWSCURRENT`/`AWSPREVIOUS` rotation grace). ORCA has none.
- **Metering and budgets.** ADP meters every request with authenticated attribution and enforces hierarchical caps with a documented fail-closed policy (`budget/enforcement_service.py:116-165`). ORCA has **no cost or budget dimension anywhere** in `src/main/runtime/orchestration/` — verified: the only "budget" hits are the circuit-breaker retry budget (`coordinator-task-dispatch.ts:73`) and a character budget (`worker-output-archive.ts:119`).

### Where it collides

Three, all named above and none fatal: label-triage-as-dispatch (§Q3), identity/lineage on ORCA-authored artifacts (§Q3), and unbounded fan-out against a gateway with no per-attempt cap (§Q6, §Q5). All three mitigations are ADP-side and worth doing independent of ORCA.

**One collision the investigation looked for and did not find:** ORCA cannot double-trigger ADP. No webhook receiver, no work-polling, one `api.github.com` reference in the entire tree and it is the auto-updater's release feed (`src/main/updater-release-builds.ts:18`).

## 4. Relationship to sibling investigations and in-flight ADP work

### The trio (#4160 dsh, #4161 Hermes Agent, #4162 ORCA)

These are **not independent**, and the overlap is load-bearing.

- **ORCA lists Hermes Agent (#4161) as a supported CLI agent** (`README.md`; `YOLO_TUI_AGENT_ARGS` maps `hermes: '--yolo'`, `src/shared/tui-agent-permissions.ts:6-30`). They compose rather than compete: Hermes is a candidate *agent*, ORCA a candidate *supervisor of agents*. A decision to adopt one does not constrain the other — but a decision to adopt Hermes means ORCA becomes a plausible local driver for it, which raises the §Q5 credential findings from hypothetical to operational.
- **#4160 (dsh) and this investigation converged independently on the same missing primitive: a durable HITL ticket.** Two candidates examined from different angles (runtime vs. cockpit) both concluded ADP's gap is not a UI and not a runtime, but a *contract*: a ticket with scope, prompt, response schema, timeout, and approvers that outlives the process that raised it. That is already written down as `hitl-ticket.schema.json` in `modules/harness/contracts/README.md:19` and remains unwritten (`:88-99` — *"if a schema file exists, the contract is real; if only this README mentions it, it's still a target"*). **Two independent investigations landing on the same unwritten schema is the strongest signal either produced.** ORCA additionally supplies a working reference implementation to read: `remote_questions` plus the `decision_gates` gate invariant.

### In-flight ADP work

| Work | Relationship | Concrete action |
|---|---|---|
| **#4077 orchestration graph** | ORCA is a **working existence proof** of Scopes A and B, in migration-tested form. Not competition — prior art. | Before the schema is frozen, read `create-graph-tables-sql.ts` and `coordinator-decision-gates.ts`; adopt the `*_unknown` state discipline and the never-auto-resolve gate invariant as **explicit written requirements** |
| **#3959 live-run steering** | The correct home for ORCA's steering UX — with the durability substitution from Q2 (durable ticket, not held stdin) | Specify steering as *enqueue an instruction for the next dispatch*, never *write to a live PTY* |
| **#3970 dashboard-as-control-surface** | Its principle — *"driveable with zero pod access"* — is the acceptance test that disqualifies ORCA-as-cockpit | Keep that principle as a stated non-negotiable; it is what makes Option A unbuildable |
| **`modules/harness/` contracts** | The unwritten `hitl-ticket.schema.json` is where both this and #4160 land | Write it, informed by `remote_questions`' field set; it unblocks a generalized HITL surface of which the AIDLC gate becomes one instance |
| **CLI credentials flow (#4145/#4154/#4158/#4159)** | This is the surface ORCA rides today, unmodified | Add a client-class dimension so BYO-client traffic is queryable, not merely `agent_run_id IS NULL` |

**Note on citation hygiene:** `modules/harness/ARCHITECTURE.md` **does not exist**, despite being referenced by `modules/harness/contracts/README.md:5`. The harness is covered by the root `ARCHITECTURE.md`. Flagged because a sibling assessment cites the non-existent path.

## 5. Risks

Risks of the recommended path (**adapt patterns + permit as unsupported BYO client**), and of the rejected alternatives, with severity and mitigation.

| # | Risk | Severity | Evidence | Mitigation |
|---|---|---|---|---|
| 1 | **Silent fail-open to direct-to-provider inference.** A developer using ORCA's account switcher has gateway env credentials stripped, and the CLI authenticates direct-to-Anthropic — inference that leaves the tenant entirely unmetered, with no error surfaced | 🔴 High | `environment.ts:1-6, 18-25`; `runtime-auth-service.ts:662`; hard reject at `spawn-preflight.ts:140-143` | Document the `apiKeyHelper` path as the only supported one; add a client-class dimension so gateway-side absence of expected traffic is detectable; make it the pilot's first pass/fail check (§6) |
| 2 | **Unbounded fan-out spend.** ORCA's headline feature multiplies token spend N×; ADP's tightest pre-request cap is a *daily* per-user budget, checked against an eventually-consistent ledger with a flat `$0.05` estimate per request | 🔴 High | `budget/enforcement_middleware.py:37-38, 93`; periods are DAILY/WEEKLY/MONTHLY only (`enforcement_service.py:320-324`); `check_agent_budget` has **zero production callers** (`enforcement_middleware.py:95-106`) | Per-run/per-attempt cap (§Q6 item 1) **before** encouraging fan-out at all; independently valuable against runaway single agents |
| 3 | **Accidental dispatch from triage.** ORCA's issue panel can add labels; ADP dispatches personas from labels. Bulk triage becomes bulk dispatch, from a UI with no execution affordance | 🟠 Medium | `src/main/github/issue-update.ts:90-112`; `intent_parser.py:182-184, 332-342` | Prefer comment-mention as the primary trigger; never use a triage-shaped label as a dispatch label. Note the hazard belongs to label-as-trigger, not to ORCA |
| 4 | **Competing PRs on one issue.** A local ORCA agent and a hosted ADP agent can work the same issue under two identities with no shared lock; ADP's FIFO serialization only covers ADP's own dispatches | 🟠 Medium | `sqs_publisher.py:70` (scope is ADP-internal); `ssh-execution-boundary.md` — *"PRs carry the client's identity"* | Documented BYO boundary; longer term, an issue-level lease that both ADP and any BYO path respect |
| 5 | **Permission-bypass blast radius on laptops.** 25+ agents launched with permissions bypassed by default, back-filled into existing profiles, with no org-level lock | 🟠 Medium | `tui-agent-launch-defaults.ts:10`; `AgentStep.tsx:67`; `global-settings-types.ts:373`; trust markers at `agent-trust-presets.ts:38-118` | Not ADP's to fix — but must be stated in the BYO note. This is a reason the posture is *unsupported*, not *recommended* |
| 6 | **#4077 ships without an `unverifiable` verdict and double-dispatches a live run.** The most expensive risk in this document, and it is a risk of **doing nothing** | 🔴 High | `ssh-execution-boundary.md` — reporting `unverifiable` as `exited` *"orphans live work and can cold-start a duplicate over the same worktree"*; ADP-side, dead-worker detection is a 300s visibility timeout against a 6h deadline | One paragraph in #4077 before the schema freezes. Cheapest high-severity mitigation available |
| 7 | **Unexpected egress from developer laptops** to `api.anthropic.com`, `chatgpt.com`, `platform.claude.com`, `*.onorca.dev`, `us.i.posthog.com` — even on a Bedrock-only deployment | 🟡 Low | `claude-oauth-usage-request.ts:8`; `codex-backend-usage-client.ts:68`; `profile-cloud-auth-config.ts:19-21`; `telemetry/client.ts:107-113` | Egress-allowlist deliberately; `DO_NOT_TRACK=1` / `ORCA_TELEMETRY_DISABLED=1` kill telemetry machine-wide (`telemetry/consent.ts:76-90`) |
| 8 | **Upstream churn if any code is ever lifted.** 66 releases in 30 days on a stable line | 🟡 Low | GitHub API | Adopt patterns and vocabulary only; the verdict already forbids code lifts |
| 9 | **Reputational/expectation risk of "permit".** Developers read "permitted" as "supported" and file tickets against a third-party Electron app | 🟡 Low | — | Explicit unsupported language; not in CI; not in the recommended-path docs |
| 10 | **Risk of the rejected options, recorded so the rejection is auditable.** Option A leaves EPIC #1219's gap open while looking like progress; Option B requires building the hard part of #4077 and rendering it in a UI we don't control | 🔴 High (if taken) | §Q1, §Q7 | Reject both; revisit only on the named trigger in §Q7 Option D |

## 6. Experiment spec (optional, runnable independently)

Two small, falsifiable experiments. Neither requires ADP changes; both are 1–2 hours. They exist so the recommendation can be checked rather than believed.

### Experiment 1 — Does ORCA-launched Claude Code actually land on the ADP gateway, and stay there?

**Hypothesis.** ORCA-launched Claude Code authenticates via the `apiKeyHelper` in `~/.claude/settings.json` and all inference lands on the gateway (metered, budgeted). Activating an ORCA *managed account* breaks this, and the break is **silent**.

**Setup.** One dev laptop. Complete ADP `/setup` for Claude Code (`bg-cognito-auth.sh import`, `apiKeyHelper` + `ANTHROPIC_BASE_URL`). Install ORCA. Add the repo. Do **not** add an ORCA managed Claude account yet.

**Procedure.**
1. Baseline: run Claude Code outside ORCA, one trivial prompt. Record the `usage_logs` row (`org_id`, `user_id`, `agent_run_id`, `cost_usd`).
2. Launch the same agent from ORCA in a worktree, same prompt. Query `usage_logs` for the new row.
3. In ORCA, add a managed Claude account (Settings → Accounts) and activate it. Relaunch. Query again.
4. Set `ANTHROPIC_AUTH_TOKEN` in the ORCA per-agent Environment box and launch.

**Pass/fail — falsifiable.**
- **P1** Step 2 produces exactly one new `usage_logs` row attributed to the developer's `org_id`/`user_id`. *Fail ⇒ the "permit" verdict is wrong and ORCA must be actively blocked, not merely unsupported.*
- **P2** Step 2's row has `agent_run_id IS NULL` and is otherwise indistinguishable from a hosted-agent row with a dropped header. *Confirms Risk #1's detectability gap and justifies the client-class dimension.*
- **P3** Step 3 produces **no** new `usage_logs` row while the agent still answers successfully. *Confirms Risk #1 (silent fail-open) as a real, reproducible defect and makes the developer-doc warning mandatory rather than advisory.*
- **P4** Step 4 fails the launch with the `spawn-preflight.ts:140-143` error, not silently. *Confirms the conflict is loud in the env path and silent only in the managed-account path — which is the precise shape of the warning to write.*

### Experiment 2 — Is best-of-N worth it on ADP-shaped work, and what does it cost?

**Hypothesis.** Fan-out earns its N× cost only where a mechanical oracle exists and attempt variance is high; on ADP's typical issue it does not, and the human compare step becomes the bottleneck.

**Setup.** Three real closed ADP issues, chosen deliberately: (a) a mechanical change (doc/config edit), (b) a well-specified feature with tests, (c) a genuine bug in an unfamiliar subsystem. Run each at N=1 and N=5 in ORCA against the gateway.

**Measure, per issue per N.** Total gateway cost from `usage_logs` (`SUM(cost_usd)` over the window); wall-clock to a mergeable diff; **human minutes spent comparing**; whether any attempt passed CI unmodified; and diff similarity across the 5 attempts (a rough proxy for variance).

**Pass/fail.**
- **P5** For (a), all 5 diffs are near-identical and N=5 costs ≥4× N=1 for no wall-clock gain. *⇒ fan-out is waste on mechanical work; N must be policy-bounded per task class, not a slider (§Q6 item 3).*
- **P6** For (c), at least one of 5 passes CI where N=1 failed. *⇒ fan-out is a real latency purchase on high-variance work, and the §Q6 "fan out at a bounded checkpoint" composition is worth building into #4077.*
- **P7** Human compare minutes grow roughly linearly in N. *⇒ the oracle requirement is not optional; without one, fan-out relocates the bottleneck to the human rather than removing it.*
- **P8** No per-attempt spend ceiling is enforceable at any point during the run. *Confirms Risk #2 by demonstration. This is the pass criterion that gates the Q6 item-1 guardrail as a prerequisite.*

**What either experiment can overturn.** Experiment 1 failing P1 flips "permit" to "block". Experiment 2 passing P6 strengthens the case for a fan-out node in #4077 rather than leaving fan-out to desktop tooling. Neither can overturn §Q1 — that conclusion rests on ORCA's own documented execution boundary, not on measurement.

---

## Verdict, restated

**ADAPT + permit-as-BYO-client.**

1. **ADAPT** the supervision patterns into #4077 and Agent Activity — three-value liveness verdicts (`live`/`unverifiable`/`exited`, and durably `*_unknown`), needs-attention as a first-class run state, the never-auto-resolve gate invariant, line-anchored diff annotation as the review channel, honest transcript-truncation boundaries, mobile as a read+nudge projection. Free under MIT, reversible, and best landed while #4077 is still design.
2. **PERMIT** ORCA as an unsupported BYO developer client against the gateway — it already works, the gateway already meters it — conditional on a client-class dimension in metering and a per-attempt spend cap.
3. **REJECT** ORCA-as-cockpit-for-hosted-agents (Option A) and an ORCA↔ADP integration (Option B). ORCA supervises processes it spawned and whose PTY it owns; ADP's hosted agents are neither. Revisit only if a genuine remote-attach capability appears upstream.

The highest-value single action in this document is not about ORCA at all: **write the `unverifiable` state into #4077 before its schema freezes.**
