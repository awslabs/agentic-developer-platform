# Design Note: AIDLC v2 on ADP Hosted Agents — Runtime Gaps & Gate-Model

**Issue**: #3159 (spike for EPIC #3158)
**Author**: @agent-architect
**Date**: 2026-07-07
**Status**: Proposed — **amended 2026-07-07 with the Inception-Only scope revision (§ Scope Revision, below), which supersedes the full-lifecycle scope assumed in Decisions 3, 5, 6 and the original child breakdown**

---

## Summary

This design note settles how AIDLC v2 (`github.com/awslabs/aidlc-workflows` branch `v2`,
the `dist/claude/` distribution) runs on ADP's headless, ephemeral, comment-triggered
hosted agents. It answers 8 design questions with decided recommendations and rejected
alternatives, and concludes with an ordered child-issue breakdown for EPIC #3158.

---

## Scope Revision (2026-07-07): Inception-Only — AIDLC as ADP's Requirements Layer

**Decision (EPIC owner)**: ADP runs AIDLC's **Ideation + Inception phases only** —
intent capture → requirements analysis → user stories → application design → units
generation → delivery planning. AIDLC's **Construction and Operation phases are out of
scope**: ADP's existing orchestration-issue loop already owns build/deploy, and running
AIDLC bolts alongside it would create two competing code-production systems on one repo.

**Product framing**: ambiguous idea in → executable, test-gated GitHub backlog out.
The user opens an issue from an AIDLC issue template (or tags `@agent-aidlc`), answers
4–6 gated interview rounds as issue comments, and receives — instead of a document —
one EPIC + ordered child stories in the repo's mandatory five-section format, linked as
native GitHub sub-issues, ready for the existing autonomous developer loop.

**The handoff adapter (new component, replaces AIDLC Construction)**: after delivery
planning is approved, a final ADP-owned stage emits GitHub issues from the AIDLC
artifacts:

| AIDLC artifact | Lands in the emitted issues as |
|---|---|
| Requirements + user stories | Child `## Description` + `## Impact analysis` |
| Application design (per-unit slice) | Child `## Design` (files, API contracts, reuse table) |
| Delivery planning (dependency order) | Sub-issue ordering + `## Dependencies` |
| Quality-agent test strategy (per unit) | Child `## Validation` as **deterministic gates**: named test files, coverage thresholds, required CI checks — no judgment-call "verify it works" language |
| Full `aidlc/` audit trail | Committed on the branch; linked from the EPIC as the decision record |

**Effect on the decisions below**:
- **Decisions 1, 2, 4, 7** (bun, conditional `Task`, settings-strip, install script)
  stand unchanged — they are phase-agnostic runtime plumbing.
- **Decision 3** (gate model A) stands, but applies to ~4–6 document gates per intent,
  not 32 stages; document-producing stages fit the pod deadline far more comfortably,
  and the per-bolt draft-PR gate machinery is dropped.
- **Decision 5** (persona mapping): only the Ideation/Inception agents run as `Task`
  subagents (product, design, delivery, architect, aws-platform, compliance, devsecops
  in support, product-lead + architecture-reviewer as reviewers). developer, quality
  (except test-strategy input to the adapter), pipeline-deploy, operations subagents
  are **not dispatched** — their lifecycle phases don't run on ADP.
- **Decision 6** (cost): inception-only workflows are document-producing; the `poc`/
  `workshop` scope allowlist still applies, but expected spend per intent drops well
  below the original full-lifecycle estimates.
- **Decision 8** (child breakdown): superseded by the revised breakdown maintained on
  EPIC #3158 (engine track + UX/adapter track). The original table below is retained
  for reference but items 3/4 shrink (fewer gates), and the draft-PR/bolt items do not
  apply.
- **Trigger/UX additions** (from EPIC-owner review, not in the original 8 questions):
  new `@agent-aidlc` persona in `MENTION_TO_PERSONA` (mind dict-order routing); an
  `issues.opened` handler in webhook-ingress gated on the AIDLC issue-template label
  (today only `issue_comment.created`, `issues.labeled`, and PR events dispatch);
  gate comments carry reply-to-answer instructions and, where multi-field input is
  needed, a prefilled issue-form deep-link. Checkbox-tick and emoji-reaction gates are
  ruled out (no usable webhook). Check-run action buttons are a fast-follow, not v1.
- **v1 gate enforcement is prompt-driven** (the persona prompt instructs commit-state →
  post gate comment → end run); the hook-enforced gate (original children 3/4) hardens
  this after the E2E proves session-resume from committed state works headless.

---

## Background

### What AIDLC v2 Is

AI-DLC 2.0 is a methodology-as-code workflow engine: 5 phases, 32 stages, 11 domain-expert
agents + 2 reviewers + 1 composer (14 total), a deterministic state machine, append-only
audit log, and approval gates at every stage. The engine is bun TypeScript; hooks and tools
are all `.ts` files executed via `bun`. It ships as `dist/claude/` — a `.claude/` directory
(settings, hooks, tools, agents, knowledge, rules, scopes, sensors, skills) plus an `aidlc/`
workspace shell containing `aidlc/spaces/default/memory/`.

### How ADP Hosted Agents Work

ADP's webhook-ingress flow: GitHub event → Lambda → SQS FIFO → KEDA ScaledJob → ephemeral
pod (`adp-agent-runtime` image, `python:3.13-slim-bookworm` + Node 24). The pod runs the
Claude Agent SDK with:
- `settingSources: ['project']` — loads the target repo's `.claude/` tree
- `permissionMode: 'bypassPermissions'`
- `allowedTools: ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch', 'Skill', ...]`
- `maxTurns: 10000`
- Pod lifetime: `activeDeadlineSeconds: 900` (15 min)

Each pod processes ONE SQS message, then exits. AGENT_TYPE (persona) is determined by the
webhook intent parser from comment mentions or labels.

### The Gap

AIDLC v2 is designed for an interactive Claude Code session with a human at the keyboard.
ADP hosted runs are headless and ephemeral. Three structural gaps must be bridged:
1. **Runtime**: bun is required but missing from the worker image
2. **Subagents**: `Task` tool is not in `allowedTools`, but AIDLC's conductor needs it
3. **Gates**: `AskUserQuestion` requires a real human turn; headless pods have no human

---

## Design Decisions

### 1. Runtime: Adding bun to the Agent-Worker Image

**Decision**: Install bun as a pinned binary in Stage 3 of
`modules/agent-factory/agent-worker-image/Dockerfile`, following the existing pattern
of pinned GitHub-release installs (yq, fzf, gh CLI, Terraform).

**Implementation**:
```dockerfile
# Install bun runtime (required for AIDLC v2 hooks and tools)
ARG BUN_VERSION=1.2.15
RUN curl -fsSL "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-x64.zip" \
      -o /tmp/bun.zip && \
    unzip -q /tmp/bun.zip -d /tmp && \
    mv /tmp/bun-linux-x64/bun /usr/local/bin/bun && \
    chmod +x /usr/local/bin/bun && \
    rm -rf /tmp/bun.zip /tmp/bun-linux-x64 && \
    bun --version
```

**Why this works**:
- Follows exact pattern of Terraform install (ARG for version, curl from releases, verify)
- Binary lands in `/usr/local/bin/` which is on PATH for non-interactive shells (AIDLC's
  requirement — hooks run via `bun <script>` from non-interactive context)
- Image rebuild triggers via existing `agent-worker-image.yml` workflow on push to
  `modules/agent-factory/agent-worker-image/**` path
- CodeBuild project `adp-dev-agent-runtime` handles the actual `docker build`
- Adds ~90MB to image (bun is a single static binary + runtime)

**Rejected alternatives**:
- **npm-installed bun** (`npm install -g bun`): npm bun package is a wrapper, not the native
  binary; slower and adds Node dependency to bun invocations
- **Corepack bun**: corepack doesn't support bun (only pnpm/yarn)
- **Build-time only** (don't ship bun, transpile hooks to Node): would require forking AIDLC's
  `dist/claude/` distribution; breaks version-pinning contract

**Build pipeline path**: Push to main → `agent-worker-image.yml` → CodeBuild
`adp-dev-agent-runtime` → ECR `adp-agent-runtime:latest`. No ARC runner Docker needed.

---

### 2. Subagents: Task Tool Policy

**Decision**: Add `'Task'` to `allowedTools` **conditionally** — enabled only when the
target repository carries an AIDLC installation (presence of `aidlc/` directory at repo root).

**Implementation**:

In `modules/agent-factory/agent/src/agent-worker.ts`, modify the `allowedTools` array:

```typescript
allowedTools: [
  'Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch', 'Skill',
  ...(KNOWLEDGE_LAYER_ENABLED ? KNOWLEDGE_LAYER_TOOLS : []),
  ...(AIDLC_ENABLED ? ['Task'] : []),
],
```

Where `AIDLC_ENABLED` is determined by checking for the `aidlc/` directory in the cloned
workspace:

```typescript
const AIDLC_ENABLED = fs.existsSync(path.join(CWD, 'aidlc'));
```

**Why conditional, not global**:
- AIDLC's conductor is the ONLY entity that uses `Task` — it delegates to 11 agents that
  all have `disallowedTools: Task` in their definitions
- Enabling `Task` globally on all hosted runs (including non-AIDLC repos) exposes an
  unbounded subagent surface: any agent could spawn arbitrary subagents, multiplying cost
  and bypassing per-run controls
- The `aidlc/` directory is the canonical marker of an AIDLC installation (it contains
  `spaces/default/memory/` which the `--doctor` check validates)
- The check is filesystem-only — no network call, no config parsing, zero latency

**Blast radius**:
- When `Task` is enabled: the AIDLC conductor (loaded via `/aidlc` skill from
  `.claude/skills/`) can spawn its 14 subagent personas. Each subagent inherits the
  same `permissionMode: 'bypassPermissions'` and the same `settingSources: ['project']`
- Subagent cost is bounded by AIDLC's own architecture: conductor dispatches agents
  sequentially per stage, and each agent has `disallowedTools: Task` (no recursive spawning)
- Additional cost control is addressed in Decision 6

**Rejected alternatives**:
- **Enable `Task` globally**: Too broad. Every hosted run on every repo gains subagent
  capability. A prompt-injection in any issue body could spawn expensive subagent chains.
- **Per-issue opt-in (label or comment flag)**: Adds friction, requires webhook-ingress
  changes, doesn't align with AIDLC's "install once, invoke via `/aidlc`" model
- **Per-persona restriction** (only `pm` persona gets `Task`): AIDLC's conductor runs as
  a Skill inside whatever persona invoked `/aidlc`, not a fixed persona. Any persona could
  legitimately invoke AIDLC.

---

### 3. Gate Model: Model A (Engine In-Run, Commit-and-Exit Gates)

**Decision**: **Model A** — run the full AIDLC engine inside hosted runs. Each approval
gate triggers: (1) commit `aidlc/` state to the branch, (2) post the gate question as a
structured issue comment, (3) end the run. Resume occurs via a fresh `@agent-<persona>`
mention (by a human or another agent commenting an answer).

**Why Model A over Model B**:
- AIDLC's value IS the state machine + audit trail + gate enforcement. Model B (discard
  the engine, use AIDLC content as prompt material) loses all of this.
- Model A preserves: deterministic stage progression, 68-event audit trail, scope/depth
  configuration, reviewer pass, learning loop, artifact guard
- The commit-and-exit pattern maps naturally onto ADP's existing "ephemeral pod, one
  message, one run" lifecycle
- Session-resume from committed state is a first-class AIDLC feature (`aidlc-state.md`
  + `.aidlc-recovery.md` on disk → resume menu on next `/aidlc` invocation)

**Gate implementation**:

When the AIDLC conductor reaches an approval gate (the `AskUserQuestion` call), a custom
ADP hook intercepts it and:

1. **Commits state**: `git add aidlc/ && git commit -m "aidlc: gate <stage-name> awaiting approval"`
   - Committed files: `aidlc/spaces/<space>/intents/<intent>/aidlc-state.md`,
     `aidlc/spaces/<space>/intents/<intent>/audit/*.md`, all stage artifacts
   - The `aidlc-state.md` will show the current stage as `[?]` (awaiting approval)
2. **Posts gate comment**: Structured comment on the issue:
   ```markdown
   ## 🚪 AIDLC Gate: <stage-name>

   **Phase**: <phase> | **Stage**: <N>/<total> | **Scope**: <scope>

   ### Gate Question
   <summary of what was produced + approval question>

   ### Artifacts
   - `aidlc/spaces/.../artifacts/<stage>/` (committed on branch)

   ### Options
   - **Approve**: reply with "approve" or "✅" to advance to the next stage
   - **Request Changes**: reply with your feedback to trigger revision
   - **Skip Stage**: reply with "skip" to mark `[S]` and advance

   ---
   _AIDLC session paused. Resume by commenting on this issue._
   ```
3. **Ends the run**: The agent posts its completion summary and the pod exits cleanly.
   The SQS message is deleted (normal completion path).

**Resume flow**:

When a human (or agent) replies to the gate comment:
1. The webhook-ingress Lambda routes the comment to the same persona that was running
2. A new pod starts, clones the repo (with committed `aidlc/` state on the branch)
3. The agent's prompt includes the gate answer from the comment
4. `/aidlc` is invoked → detects existing `aidlc-state.md` → "Resume from last checkpoint"
5. The gate answer is fed to the conductor → stage advances (or revises)

**The `mint-presence.ts` hook problem**:

AIDLC's `mint-presence.ts` hook records `HUMAN_TURN` events to prevent fabricated approvals
under autopilot. In headless mode, no `HUMAN_TURN` is ever recorded natively. Solution:

The ADP gate hook that posts the issue comment and receives the reply is the proof of human
presence — the comment IS the human turn. On resume, before invoking `/aidlc`, the agent
writes a synthetic `HUMAN_TURN` event to the audit shard with the comment author and
timestamp. This satisfies `mint-presence.ts`'s check while maintaining the security
invariant (a real human or authorized agent DID act).

**Rejected alternatives**:
- **Model B (discard engine, use as prompt content)**: Loses state machine, audit trail,
  reviewer pass, scope auto-detection, learning loop — the features that make AIDLC v2
  valuable. Reduces to "structured prompt templates" which ADP already has via personas.
- **Blocking in-run gate (long-poll for comment)**: Violates the 15-min pod timeout.
  Even with extended timeouts, holds a pod idle while waiting for human response (hours/days).
  Wasteful and fragile.
- **Auto-approve all gates**: Defeats AIDLC's purpose. The audit trail becomes meaningless
  if gates are rubber-stamped. However, Construction's "Ladder Prompt" autonomous mode IS
  supported — after the walking-skeleton gate, remaining Bolts can auto-advance.

---

### 4. Settings Collision: Install-Time Strip/Merge Rule

**Decision**: At AIDLC install time (when `dist/claude/.claude/` is copied into a target
repo), a strip script removes model/environment configuration from the shipped
`settings.json`, preserving only structural configuration (hooks, tools, skills, agents).

**The collision**:
- AIDLC's shipped `.claude/settings.json` sets: `AWS_REGION=us-east-1`, model pins
  (Fable/Opus/Sonnet/Haiku), Bedrock provider configuration
- ADP's worker uses `settingSources: ['project']` which loads this file
- If the shipped model pins disagree with the worker's `MODEL` environment variable
  (set from the SQS envelope's `model_resolved` field), the project settings win for
  skill/hook invocations but the outer query uses the envelope model — inconsistent
  behavior

**Strip rule** (applied by the install script, not at runtime):

The install script (`scripts/install-aidlc.sh` in the child issue) processes the shipped
`.claude/settings.json` to:

1. **REMOVE** any top-level keys: `model`, `provider`, `env` (or `environment`),
   `apiKey`, `region`, `baseUrl`
2. **REMOVE** from `env` object: `AWS_REGION`, `AWS_PROFILE`, `ANTHROPIC_API_KEY`,
   `ANTHROPIC_MODEL`, any `AWS_*` key
3. **PRESERVE**: `hooks`, `tools`, `skills`, `agents`, `knowledge`, `rules`, `scopes`,
   `sensors`, `permissions`, `allowedTools`, `disallowedTools`
4. **MERGE** with existing `.claude/settings.json` if present (AIDLC keys ADD to
   existing; existing keys are never removed)

**Runtime guard** (defense-in-depth):

The worker already controls the model via `options.model` in the SDK query — this is
authoritative regardless of `settings.json` content. The risk is limited to hooks/tools
that might read model config from settings. The strip rule eliminates this at source.

**Rejected alternatives**:
- **Runtime filtering** (patch `settingSources` to strip model keys before passing to SDK):
  Would require SDK internals knowledge; fragile across SDK versions; the SDK doesn't
  expose a settings-filter API
- **Separate `settingSources` mode** (e.g. `['project-filtered']`): Doesn't exist in the
  SDK; would require upstream changes
- **Don't use `settingSources: ['project']`**: Breaks ALL project-level configuration
  (not just AIDLC). ADP relies on this for repo-specific skills, rules, permissions.
- **`.claude/settings.local.json` override**: This file is gitignored by convention; can't
  be committed to the target repo. Also, `settings.local.json` ADDS to settings rather
  than stripping from them.

---

### 5. Persona Mapping: AIDLC Agents → ADP Execution Model

**Decision**: ALL 14 AIDLC agents run as SDK `Task` subagents inside a single hosted run
(the run that invoked `/aidlc`). None map to separate ADP hosted persona runs.

**Rationale**:
- AIDLC's architecture is explicit: the conductor performs ALL delegation; agents never
  invoke each other. This is a single orchestration context, not a multi-persona dispatch.
- Spawning separate ADP pods per AIDLC agent would require: serialized SQS messages per
  agent, state synchronization between pods, loss of the conductor's in-context coordination
- The `Task` tool spawns subagents within the same SDK session — they share the workspace,
  see committed files, and return results to the conductor. This IS AIDLC's design.
- Pod resource limits (4 CPU, 8Gi RAM) are sufficient for sequential subagent execution

**Mapping table**:

| AIDLC Agent | ADP Execution | Notes |
|---|---|---|
| Conductor (SKILL.md) | Runs in the host agent's context (via `/aidlc` skill) | Not a separate agent; it IS the orchestrating skill |
| aidlc-product-agent | `Task` subagent inside host run | Opus model; stages 1.1-1.7, 2.3-2.5 |
| aidlc-design-agent | `Task` subagent inside host run | Opus; stages 1.6, 2.5 |
| aidlc-delivery-agent | `Task` subagent inside host run | Sonnet; stages 1.5, 1.7, 2.8 |
| aidlc-architect-agent | `Task` subagent inside host run | Opus; 9 stages across phases 1-3 |
| aidlc-aws-platform-agent | `Task` subagent inside host run | Opus; stages 2.6, 3.4, 4.2 |
| aidlc-compliance-agent | `Task` subagent inside host run | Opus; support-only |
| aidlc-devsecops-agent | `Task` subagent inside host run | Opus; support-only |
| aidlc-developer-agent | `Task` subagent inside host run | Opus; stages 2.1, 3.5 |
| aidlc-quality-agent | `Task` subagent inside host run | Opus; stages 3.6, 4.6 |
| aidlc-pipeline-deploy-agent | `Task` subagent inside host run | Sonnet; stages 2.2, 3.7, 4.1, 4.3 |
| aidlc-operations-agent | `Task` subagent inside host run | Sonnet; stages 4.4-4.7 |
| aidlc-product-lead-agent | `Task` subagent inside host run | Sonnet; reviewer (no primary artifacts) |
| aidlc-architecture-reviewer-agent | `Task` subagent inside host run | Sonnet; reviewer |
| aidlc-composer-agent | `Task` subagent inside host run | Dispatched on `/aidlc compose` |

**Pod timeout implication**: A single stage (one agent delegation + reviewer pass + gate)
must complete within the pod's 15-minute `activeDeadlineSeconds`. If a stage is complex
(e.g., code-generation on a large unit), the 15-min limit may be tight. Recommendation:
increase `activeDeadlineSeconds` for AIDLC-flagged runs to 30 minutes (configurable via
the ScaledJob's environment or a sidecar annotation). This is addressed in child issue
scope.

**Rejected alternatives**:
- **Map AIDLC agents to ADP personas (1:1)**: AIDLC has 11 domain agents; ADP has 6
  personas. No clean mapping exists. Even if mapped, each would be a separate pod with
  its own SQS message — losing the conductor's single-context orchestration.
- **Map groups of AIDLC agents to ADP personas**: E.g., AIDLC's product+design → ADP's
  product persona. Still breaks the conductor model; the conductor would need to dispatch
  across pods and wait for responses (not supported without custom infrastructure).
- **Hybrid**: Some in-run, some cross-pod. Creates two execution models with different
  state-sync requirements. Complexity not justified by any benefit.

---

### 6. Cost Control

**Decision**: Multi-layered bounds:

1. **Scope allowlist** (initial rollout): Only `poc` and `workshop` scopes enabled for
   hosted runs. These activate 8 and 25 stages respectively (vs. 32 for `feature`/`enterprise`).
   Other scopes are blocked at the ADP gate hook level with a clear error message.
   Expand the allowlist after validating cost patterns.

2. **Per-stage maxTurns**: Each `Task` subagent spawned by the conductor inherits a
   `maxTurns` limit. AIDLC doesn't set this itself (it relies on natural completion), so
   ADP injects it via the SDK query options. Recommended: `maxTurns: 200` per subagent
   (sufficient for any single stage; a full code-generation stage typically uses 50-100
   turns).

3. **Per-run stage cap**: The ADP gate hook tracks stages completed in the current run.
   After completing N stages without a gate (autonomous Construction mode), force a
   commit-and-exit. Recommended: N = 3 (a walking skeleton + 2 autonomous Bolts before
   requiring a check-in).

4. **Pod timeout**: `activeDeadlineSeconds` enforces hard wall-clock. Current: 900s (15 min).
   For AIDLC runs: 1800s (30 min). This is the backstop — if a stage hangs or loops, the
   pod is killed and the state on disk reflects the last committed checkpoint.

5. **Token budget tracking**: The AIDLC `session-cost` skill already tracks token spend
   and can report via the audit trail. On pod exit, the agent posts a cost summary in
   the completion comment. No hard token cap enforced in v1 (the scope allowlist + stage
   cap + pod timeout provide sufficient bounding).

**Cost estimates** (based on AIDLC's stated architecture):

| Scope | Stages | Estimated agents spawned | Approximate tokens (input+output) |
|---|---|---|---|
| poc (8 stages) | 8 | ~12 (including reviewers) | ~500K-1M |
| workshop (25 stages) | 25 | ~40 (including reviewers) | ~2M-4M |
| feature (32 stages) | 32 | ~55 | ~4M-8M |

These are per-workflow totals across ALL resumes (not per-run).

**Rejected alternatives**:
- **Hard token budget per run**: Difficult to enforce across subagents; the SDK doesn't
  expose a cross-agent token meter. Would require custom instrumentation.
- **No scope restriction**: Allowing `enterprise` scope (32 stages, Comprehensive depth)
  on day-1 risks runaway costs before patterns are validated.
- **Time-based billing cap**: Not enforceable at the agent level; would require platform
  infrastructure (rate-limits table integration).

---

### 7. Install Mechanics: How a Target Repo Gets AIDLC

**Decision**: A copy script (`scripts/install-aidlc.sh`) in the ADP repo that:
1. Clones `awslabs/aidlc-workflows` at a pinned tag (e.g., `v2.2.3`)
2. Copies `dist/claude/.claude/` and `dist/claude/aidlc/` into the target repo
3. Applies the settings-strip rule (Decision 4)
4. Merges with existing `.claude/settings.json` if present
5. Commits with message `chore: install AIDLC v2.2.3 (ADP-hosted distribution)`
6. Writes a version marker: `aidlc/.aidlc-version` containing the pinned tag

**Version pinning**:
- AIDLC v2 is GA-preview with breaking changes between releases
- The install script pins to a specific tag, NOT `v2` branch HEAD
- The version marker allows future upgrade scripts to detect and migrate
- ADP itself pins the bun version in the Dockerfile (Decision 1) to match the AIDLC
  release's tested runtime

**Target repo requirements**:
- Must be a git repository (AIDLC audit shards use git-clone-based shard naming)
- Must NOT have conflicting `.claude/hooks/` entries (merge handles additive; conflicts
  are flagged by the install script)

**What gets committed to the target repo**:
```
.claude/
├── agents/           # AIDLC's 14 agent definitions
├── aidlc-common/     # Shared AIDLC utilities
├── hooks/            # 11 TypeScript hook files (run via bun)
├── knowledge/        # Methodology knowledge packs
├── rules/            # Stage-specific rules
├── scopes/           # 9 scope definitions
├── sensors/          # Stage sensors
├── skills/           # AIDLC orchestrator skill (/aidlc entrypoint)
├── tools/            # 14 aidlc-*.ts tool files (run via bun)
├── CLAUDE.md         # AIDLC's system instructions (additive to repo's existing CLAUDE.md)
├── settings.json     # STRIPPED: hooks/tools/skills/agents only, no model/env pins
└── settings.local.json.example  # Reference for local dev (not committed)

aidlc/
├── .aidlc-version    # "v2.2.3" (ADP addition)
└── spaces/
    └── default/
        └── memory/   # Pre-built method tree (required for --doctor)

.mcp.json             # MCP server config (if AIDLC uses one)
```

**Rejected alternatives**:
- **Template repo** (fork `aidlc-workflows` as a GitHub template): Over-engineers
  install; adds a repo dependency. Copy script is simpler and version-pinnable.
- **Git submodule**: Submodules are painful in CI; the agent pod would need submodule
  init logic. Copy-and-commit is atomic and self-contained.
- **npm/pip package**: AIDLC isn't distributed as a package; it's a file-tree distribution.
  The official install method is `cp -r dist/claude/...`.
- **No install script (manual copy)**: Error-prone; misses the settings-strip step;
  operators will forget to pin versions.

---

### 8. Child Issue Breakdown

Ordered list of implementation children for EPIC #3158. Each is sized for a single
hosted developer run (per `docs/orchestration-issue-guide.md`: ~1-3KB issue body,
clear acceptance criteria, single PR output).

#### Phase 1: Runtime Foundation

| # | Title | Files | Depends On | Persona |
|---|---|---|---|---|
| 1 | Add bun to agent-worker image | `modules/agent-factory/agent-worker-image/Dockerfile` | None | developer |
| 2 | Conditional `Task` tool enablement | `modules/agent-factory/agent/src/agent-worker.ts` | None | developer |

#### Phase 2: Gate Infrastructure

| # | Title | Files | Depends On | Persona |
|---|---|---|---|---|
| 3 | AIDLC gate hook — commit state and post gate comment | New: `modules/agent-factory/agent/src/aidlc/gate-hook.ts`, modify `agent-worker.ts` | #1, #2 | developer |
| 4 | AIDLC resume hook — inject HUMAN_TURN and feed gate answer | New: `modules/agent-factory/agent/src/aidlc/resume-hook.ts`, modify `agent-worker.ts` | #3 | developer |
| 5 | Scope allowlist enforcement | New: `modules/agent-factory/agent/src/aidlc/scope-guard.ts` | #3 | developer |

#### Phase 3: Install Tooling

| # | Title | Files | Depends On | Persona |
|---|---|---|---|---|
| 6 | AIDLC install script with settings-strip | New: `platform/scripts/install-aidlc.sh` | None | developer |
| 7 | AIDLC version marker and upgrade detection | Extend install script, new: `platform/scripts/upgrade-aidlc.sh` | #6 | developer |

#### Phase 4: Integration & Pod Configuration

| # | Title | Files | Depends On | Persona |
|---|---|---|---|---|
| 8 | Increase activeDeadlineSeconds for AIDLC runs | `modules/agent-factory/webhook-ingress/infra/scaledjob.tf` | #2 | developer |
| 9 | Per-subagent maxTurns injection | Modify `agent-worker.ts` SDK query options for AIDLC context | #2 | developer |

#### Phase 5: Validation

| # | Title | Files | Depends On | Persona |
|---|---|---|---|---|
| 10 | E2E smoke test: AIDLC poc-scope on test repo | New test repo + test workflow, `modules/agent-factory/agent/src/aidlc/__tests__/` | #1-#9 | developer |

**Dependency graph**:
```
#1 (bun) ──┐
            ├── #3 (gate hook) ── #4 (resume hook)
#2 (Task) ─┘         │                   │
                      #5 (scope guard)    │
                      │                   │
#6 (install) ── #7 (upgrade)             │
                                          │
#8 (timeout) ─────────────────────────────┤
#9 (maxTurns) ────────────────────────────┤
                                          │
                                    #10 (E2E smoke)
```

---

## Open Questions (for EPIC owner to decide)

1. **Autonomous Construction mode**: Should the Ladder Prompt (AIDLC's one-time autonomy
   choice for Construction Bolts) be pre-answered "autonomous" for hosted runs, or should
   each Bolt gate require a comment? Recommendation: pre-answer autonomous with the
   per-run stage cap (Decision 6) as the safety net.

2. **Multi-intent support**: AIDLC supports multiple intents per space. Should ADP limit
   to one intent per issue, or allow multiple? Recommendation: one intent per issue
   (simplest; aligns with ADP's "one issue = one unit of work" model).

3. **AIDLC CLAUDE.md merge**: The shipped `CLAUDE.md` in `.claude/` is additive to the
   repo's existing `CLAUDE.md`. The install script should append (not replace). Validate
   that the combined prompt doesn't exceed agent prompt budget.

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Bun binary incompatible with Debian bookworm | Low | High (all hooks fail) | Test in CI; bun officially supports Debian bookworm |
| AIDLC v2 breaking change before our pin stabilizes | Medium | Medium | Pin exact tag; don't track branch HEAD |
| Gate comment format not machine-parseable on resume | Medium | High (resume fails) | Structured comment with explicit markers; integration tests |
| Pod timeout kills mid-stage (state inconsistent) | Medium | Low (resume from last commit) | Frequent git commits within stage; recovery breadcrumb survives |
| Token cost exceeds expectations on workshop scope | Medium | Medium (budget overrun) | Scope allowlist starts with poc only; expand after measurement |
| `mint-presence.ts` hook update breaks synthetic HUMAN_TURN | Low | Medium | Pin AIDLC version; integration test covers this path |

---

## References

- AIDLC v2: https://github.com/awslabs/aidlc-workflows/tree/v2
- EPIC: #3158
- Agent worker source: `modules/agent-factory/agent/src/agent-worker.ts:1188` (allowedTools)
- Agent worker Dockerfile: `modules/agent-factory/agent-worker-image/Dockerfile`
- CodeBuild buildspec: `codebuild/bs-agent-runtime.yml`
- Image build workflow: `.github/workflows/agent-worker-image.yml`
- Webhook-ingress flow: `modules/agent-factory/webhook-ingress/`
- ScaledJob config: `modules/agent-factory/webhook-ingress/infra/scaledjob.tf`
- Orchestration issue guide: `docs/orchestration-issue-guide.md`
- ADP personas: `modules/agent-factory/rules/personas/` (7 personas: product, pm, architect, developer, reviewer, operations, codex)
