# Spike Note: Persona-aware Codex delegation (distilled persona context → codex-bridge) + MCP/skills surface

**Issue:** #2839 (sub of EPIC #2702; absorbs the EPIC Phase-2 "MCP/skills spike")
**Date:** 2026-07-04
**Status:** Complete — recommendation below is **design-only**; no production files changed.
**Author:** @agent-architect
**Mode:** Per-issue spike. Deliverable is this note + named follow-up issues. **No code shipped.**

---

## 0. TL;DR / recommendation

- **Mechanism (Q1): render a per-run `AGENTS.md` into the delegation working directory**, written by the
  codex-bridge wrapper (not the customer repo tree). **Empirically confirmed Codex 0.142.5 reads and obeys
  `AGENTS.md` from cwd** (canary below). Prefer this over prepend-to-instruction (weaker signal, burns the
  same tokens every call) and over `model_instructions_file`/profile surfaces (global, not per-run, and one
  touched the config the #2713 soak depends on).
- **Content (Q2): a parallel `personas/codex-distilled/<persona>.md` fileset**, authored by hand, NOT a
  delimited block spliced out of the full persona at runtime. Transfers mindset + conventions + quality bar;
  strips identity, outer-loop workflow, and mention/trigger rules (which are injection- and confusion-prone
  when handed to Codex).
- **Review-mode (Q3): parameterize `run-codex.sh review` by the active persona.** `AGENT_TYPE` **is present in
  the skill's Bash context — confirmed empirically** (this run's env had `AGENT_TYPE=architect`). Keep the
  existing read-only contract (`"Do not modify any files."`) intact.
- **MCP/skills (Q4): Codex 0.142.5 CAN consume MCP servers** (`mcp_servers` config key + `codex mcp add/list/get`
  subcommands verified). But **do NOT wire agent-context MCP into Codex delegations in v1.** A distilled prompt
  pack (the `AGENTS.md` above) is the right fit for persona context; MCP is a heavier, separate integration
  with its own auth/egress surface. Recommend a *separate, later, opt-in* spike if a concrete need appears.
- **No-second-runtime (Q5): reaffirmed.** Persona passthrough is context-only. It must never grow into
  persona-routed Codex issue execution. Codex stays a supervised, bounded delegate; the Claude worker remains
  the only outer loop.

**Follow-up implementation issues** are named in §8, each to be filed with the five mandatory sections.

---

## 1. What actually reaches Codex today (baseline)

`modules/agent-factory/skills/codex-bridge/scripts/run-codex.sh`:

- **write mode** → Codex gets the supervising agent's instruction string verbatim, as a single argv
  (`INSTRUCTION="$ARG"`, line 61).
- **review mode** → Codex gets a **hardcoded** prompt (lines 68–70):
  `"Review the file '<path>' for correctness, bugs, and clear improvements. Report your findings as a concise
  list. Do not modify any files."`

Neither path carries any persona signal. The persona markdown
(`modules/agent-factory/rules/personas/*.md`) is loaded only by the Claude SDK worker's `loadRules()`
(`agent-worker.ts:627`), which reads `.github-agent/personas/<AGENT_TYPE>.md` (repo-first) → `.adp-rules/personas/<AGENT_TYPE>.md`
(adp fallback). **That context never crosses into the Codex subprocess.** So a reviewer-persona run and a
developer-persona run delegate to Codex with identical, persona-blind context — exactly the gap this spike targets.

`AGENT_TYPE` is exported into the worker/pod env by the image entrypoint
(`modules/agent-factory/agent-worker-image/entrypoint.py:432` — `"AGENT_TYPE": persona`) and read in
`agent-worker.ts:90` (`process.env.AGENT_TYPE || 'developer'`).

---

## 2. Empirical environment (verify, don't trust docs — the #2703 lesson)

All findings below were produced **in this architect ScaledJob pod**, not from documentation.

| Item | Value (observed) |
|------|-------------------|
| Codex CLI | `codex-cli 0.142.5` (`codex --version`) |
| Identity | `arn:aws:sts::879318057152:assumed-role/adp-dev-agent-scaledjob-role/...` (the KEDA pod IRSA role) |
| Provider (LIVE) | `~/.codex/config.toml` → `model_provider = "adp-gateway"` → sigv4-proxy sidecar `127.0.0.1:9090` (port **open**) → API GW `/agent/{proxy+}` → gateway `/openai/v1/responses`. This is the **#2713 v2 cutover, already live.** |
| `AGENT_TYPE` in Bash | **`architect`** — present in the skill's shell context |
| Soak safety | The `amazon-bedrock` direct-IRSA fallback was **never activated**; provider stayed `adp-gateway` throughout. |

> **Hard constraint honored (per #2839 + Comment 1):** I did **not** persistently modify `~/.codex/config.toml`
> and did **not** switch the provider to the `amazon-bedrock` fallback (which would reset the #2713 soak that
> started 2026-07-03 20:11Z). The canary in §3 ran against the live gateway provider using a `/tmp` scratch cwd.
> Codex auto-appended a `[projects."/tmp/..."]` trust stanza to the config as a side effect; I removed it and
> confirmed `model_provider` remained `adp-gateway`, restoring the file to its pre-test bytes.

---

## 3. Q1 — Mechanism: how to get persona context into Codex

### 3.1 Candidate surfaces observed in Codex 0.142.5

From `codex --help`, `codex exec --help`, `codex mcp --help`, and the platform binary's embedded config keys:

| Surface | Evidence (0.142.5) | Scope | Verdict |
|---|---|---|---|
| **`AGENTS.md` in cwd** | Default project-doc filename; config keys `project_doc_fallback_filenames`, `project_doc_max_bytes` exist in the binary. **Consumption confirmed empirically (§3.2).** | Per-run (the delegation cwd) | ✅ **Recommended** |
| Prepend persona to the instruction argv | `run-codex.sh` already passes one argv string | Per-run | ⚠️ Works but weaker: no structural separation of "who you are" vs. "the task"; re-sends full block every call; harder to cap/measure |
| `model_instructions_file` (config key) | Present in binary | **Global** (config-level) | ❌ Not per-run; editing it touches the soak-critical config |
| `-p/--profile` (`$CODEX_HOME/<name>.config.toml`) | `codex exec -p` documented | Global-ish (layered config) | ❌ Adds config files under `$CODEX_HOME`; per-persona profiles are heavier than needed and blur into the model-path config |
| `-c/--config` dotted overrides | `codex exec -c key=val` documented | Per-run flag | 🟡 Fine for small overrides, but persona text is prose, not a config value |
| `--ignore-rules` / `--ignore-user-config` | documented | — | Context: these let you *suppress* rules; confirms a rules layer exists |

### 3.2 Canary: Codex 0.142.5 DOES consume `AGENTS.md` from cwd (verified)

Read-only test in a `/tmp` scratch dir (no repo, live gateway provider, no config change):

```
$SCRATCH/AGENTS.md:
  # Project conventions
  IMPORTANT: ... When asked for the canary word, reply with exactly: PERSIMMON-4417 ...

$ codex exec --skip-git-repo-check -C "$SCRATCH" -s read-only \
    --dangerously-bypass-approvals-and-sandbox -o "$SCRATCH/out.txt" \
    "What is the canary word for this repo? Answer in one word."
# EXIT 0
# out.txt:  PERSIMMON-4417
# run.log:  provider: adp-gateway   (soak provider unchanged)
```

Codex read `AGENTS.md` from the working root and obeyed it. This is the empirical proof the mechanism works on
the pinned version — not a docs claim.

### 3.3 Recommended mechanism

**The wrapper renders a per-run `AGENTS.md` into the delegation working directory before invoking Codex, and
removes it after.** Key design points:

1. **Where.** Write to the *delegation cwd*, which for codex-bridge is the agent's workspace checkout root
   (`$PWD` when `run-codex.sh` runs). **Blast-radius guard:** an `AGENTS.md` written into a customer repo tree
   can be `git add`-ed by accident and leak persona internals into a customer PR (impact table, row 2). Two
   mitigations, pick both:
   - Wrapper writes `AGENTS.md` at run start and **`trap`-deletes it on exit** (success, failure, timeout).
   - The supervising persona's finalize/commit path must **never stage `AGENTS.md`** (add to the worker's
     commit-exclusion list; `AGENTS.md` is not a repo artifact for ADP repos today — confirm none exists before
     writing, and if one *does* exist in the target repo, append-with-restore rather than overwrite).
2. **Precedence.** If the wrapper both renders `AGENTS.md` *and* the target repo ships its own `AGENTS.md`,
   define a deterministic merge: adp-distilled persona block first (delimited), then the repo's existing content.
   Mirror `loadRules()`' repo-first/adp-fallback *semantics* but invert for safety here: persona conventions are
   adp-owned and must not be silently overridden by repo-controlled text (see §3.4 injection note).
3. **Token cost.** Bounded. `project_doc_max_bytes` exists as a hard cap; the distilled block (§4) targets
   ~1–2 KB (≈0.5–1 K tokens), well under it. Set/confirm `project_doc_max_bytes` so an oversized persona file
   can't blow the context budget (impact table, row 1).

### 3.4 The repo-persona override path (`.github-agent/personas/`) — injection implications

`loadRules()` (`agent-worker.ts:633`) lets a **target repo** override the persona via
`.github-agent/personas/<AGENT_TYPE>.md`, repo-content winning over adp defaults. That is acceptable for the
**Claude** worker (the repo owner is trusted to tune their own agent). It is **NOT** acceptable to pipe
repo-controlled persona text straight into Codex's `AGENTS.md`, because:

- Codex executes with `--dangerously-bypass-approvals-and-sandbox` in the pod; repo-controlled instruction text
  that reaches `AGENTS.md` is repo-steered control over a bypassed-sandbox model (impact table, row 4).
- A malicious/careless repo could put "exfiltrate X" or "ignore prior conventions" in `.github-agent/personas/`.

**Recommendation:** for the Codex-distilled block, **use the adp-owned distilled fileset only** (§4); do NOT
source it from `.github-agent/personas/`. If per-repo Codex tuning is ever wanted, that is a separate, gated
feature with sanitization — explicitly out of scope here.

---

## 4. Q2 — Content: the "distilled for delegation" format

### 4.1 Delimited-block vs. parallel fileset — decision

Two options were posed:
- **(a)** a delimited `## Codex delegation block` inside each existing `personas/<persona>.md`, extracted at runtime.
- **(b)** a parallel `personas/codex-distilled/<persona>.md` fileset.

**Recommend (b), the parallel fileset.** Rationale:
- **Separation of concerns.** The full persona is written for the Claude outer loop (identity, mention-routing,
  memory priorities, GitHub plumbing). A runtime extractor that greps a delimited block out of it is fragile
  (formatting drift breaks extraction) and couples two audiences into one file.
- **Reviewability / safety.** A distinct file makes it obvious to a human reviewer *exactly* what Codex sees.
  What Codex executes on is security-relevant; it deserves its own reviewable artifact, not a substring.
- **No runtime parsing.** The wrapper just reads one file — no delimiter logic, no failure mode where a missing
  delimiter silently sends the whole persona (identity + outer-loop rules included) to Codex.

Cost: light duplication (a maintainer updates two files when a convention changes). Acceptable — the distilled
files are short and change rarely; a lint check (§8, follow-up) can flag drift.

### 4.2 What transfers vs. what is stripped

| Persona section | Transfer to Codex? | Why |
|---|---|---|
| **Mindset** (correctness-first, consistency-first, security-always, reliability-first) | ✅ Yes | This is the persona-calibration Codex should apply to diffs/reviews |
| **Conventions / quality bar** (match existing patterns, test the paths, no secrets, error-handling) | ✅ Yes | Directly improves Codex output; fewer supervisor fix-up cycles |
| **Identity** ("You are @agent-reviewer, the quality gate…") | ❌ Strip | Codex is a delegate, not the persona; identity confuses "who acts" |
| **Outer-loop workflow** (post plans, finalize flow, branch creation, PR description) | ❌ Strip | Codex can't/shouldn't do platform actions; passing these verbatim makes Codex attempt things it can't (impact table, row 3) |
| **Mention/trigger rules** (`@agent-*`, `adp-trigger`, no-double-fire) | ❌ Strip | Actively dangerous: Codex could emit mention text that triggers other agents |
| **Credential access / memory priorities** | ❌ Strip | Pod/worker concerns, irrelevant to a bounded Codex task |

Concretely, a distilled reviewer file carries the reviewer **Mindset** (correctness → security → maintainability
→ pragmatic) and **Quality Bar** (tests pass, no secrets, error paths covered) — that's the calibration a
persona-aware Codex review should reflect — and nothing about "run /security-review" or "escalate architectural
concerns," which are outer-loop actions.

---

## 5. Q3 — Review-mode upgrade

### 5.1 `AGENT_TYPE` reaches the wrapper — confirmed

The spike asked to "confirm [`AGENT_TYPE`] reaches the skill's Bash context." **Confirmed empirically:** this
architect run's shell had `AGENT_TYPE=architect`. Since the skill's Bash tool inherits the worker process env,
and entrypoint.py exports `AGENT_TYPE` for the whole pod, `run-codex.sh` can read `${AGENT_TYPE}` directly — no
new plumbing needed.

### 5.2 Proposed change (design only)

Replace the hardcoded review prompt (`run-codex.sh:70`) with a persona-parameterized one:

- Read `${AGENT_TYPE:-developer}`; load `personas/codex-distilled/<AGENT_TYPE>.md` if present.
- Compose: `"<distilled persona block>\n\nReview the file '<path>' for correctness, bugs, and clear
  improvements, applying the standards above. Report your findings as a concise list. Do not modify any files."`
- **Keep the read-only contract intact.** The existing contract test asserts the synthesized prompt contains the
  target path and the literal `"Do not modify any files."` (`test_run_codex_contract.py:154–164`). The new prompt
  must preserve both substrings or the test (and the read-only guarantee) breaks.
- Graceful fallback: if the distilled file is missing, fall back to today's exact hardcoded prompt (no regression).

Same distilled block should be exposed to **write mode** via the `AGENTS.md` mechanism (§3), so both modes are
persona-aware through one source of truth.

---

## 6. Q4 — MCP / skills surface (absorbs EPIC #2702 Phase-2 spike)

### 6.1 Can Codex 0.142.5 consume MCP servers? — Yes (verified)

- The platform binary contains the `mcp_servers` config key.
- `codex mcp` exposes `list / get / add / remove / login / logout` subcommands; `codex mcp-server` even runs
  Codex *as* an MCP server. So MCP is a first-class, supported surface on the pinned version.

I did **not** wire the agent-context MCP endpoint into Codex in this spike (doing so would mean adding an
`mcp_servers` entry, i.e. touching config during the soak — out of bounds). The capability is confirmed from the
CLI surface; a live read-only prototype is deferred to the follow-up spike (§8) so it can run outside the soak
window without soak-reset risk.

### 6.2 Is exposing agent-context MCP (5 tools) to Codex worth it? — Not in v1

- **The need this issue targets is persona *context*, not tool access.** Persona calibration (mindset, quality
  bar, conventions) is static prose — a **prompt pack (`AGENTS.md`) fits it perfectly** and is bounded, cheap,
  and reviewable. MCP solves a different problem (giving Codex *live tools* like semantic search / code search /
  memory), which this issue does not ask for.
- **Auth path is non-trivial.** agent-context's MCP endpoint is SigV4-fronted. Codex would need either (a) to
  reuse the existing sigv4-proxy sidecar pattern (the same one the model path already uses on :9090 — a second
  proxy target or path), or (b) direct SigV4 from Codex (Codex doesn't natively SigV4 arbitrary MCP HTTP). That's
  real integration work with its own egress/allowlist surface — disproportionate to the persona-context goal.
- **Skills-style prompt packs beat MCP for *this* use.** A distilled `AGENTS.md` is the "skills-style prompt
  pack" the issue mentions, and it's strictly better here: no network, no auth, no new failure mode, bounded
  tokens. MCP would only pay off if we later want Codex to *actively query* the code-intelligence tools mid-task
  — a separate capability with its own justification.

**Recommendation:** ship persona context via prompt pack (§3–§5); **do not** add MCP to Codex delegations now.
File a separate, opt-in spike (§8) if/when a concrete "Codex needs live agent-context tools" need appears; that
spike prototypes read-only against a scratch `mcp_servers` entry **outside the #2713 soak window**.

---

## 7. Q5 — No-second-runtime guard (reaffirmed)

Per the 2026-07-02 architecture decision (and EPIC #2702's framing), **Codex is a supervised, bounded delegate —
not a second agent runtime.** This spike's persona passthrough is **context-only**: it makes Codex's *output*
persona-calibrated. It must **not** grow into:

- persona-routed Codex *issue execution* (Codex picking up issues / running an outer loop),
- Codex triggering other agents, posting plans, or driving GitHub state,
- a per-persona Codex profile system that amounts to "Codex personas."

The Claude SDK worker remains the sole outer loop; `@agent-codex` (a Claude persona, `personas/codex.md`) stays
the supervisor that decomposes, delegates bounded tasks, reviews every diff, and owns the PR. The distilled
block deliberately **strips** all outer-loop/identity/mention content (§4.2) precisely to keep this boundary
enforced at the content layer, not just by convention.

---

## 8. Follow-up implementation issues (rough scope; to be filed with the five mandatory sections)

1. **`feat(codex-bridge): render per-run distilled-persona `AGENTS.md` into the delegation cwd`**
   Scope: wrapper writes `personas/codex-distilled/<AGENT_TYPE>.md` (adp-owned only) to cwd before `codex exec`,
   `trap`-deletes on exit; set `project_doc_max_bytes`; ensure finalize never stages `AGENTS.md`; handle a
   pre-existing repo `AGENTS.md` (append-with-restore). Tests: file written+removed, commit-exclusion, token cap.

2. **`feat(personas): add `personas/codex-distilled/{developer,reviewer,operations}.md` distilled filesets`**
   Scope: author the 3 distilled files (mindset + conventions + quality bar only; no identity/outer-loop/mention).
   Add a lint/CI check flagging drift between a persona and its distilled counterpart.

3. **`feat(codex-bridge): persona-parameterize review-mode prompt in run-codex.sh`**
   Scope: read `${AGENT_TYPE}`, prepend distilled block to the review prompt, preserve the `"Do not modify any
   files."` + path substrings (contract test at `tests/test_run_codex_contract.py:154`), fallback to current
   prompt when distilled file absent. Extend contract tests for the persona-parameterized path. Depends on #2.

4. **`spike(codex): opt-in agent-context MCP for Codex delegations (read-only prototype)`** — *lower priority.*
   Scope: prototype an `mcp_servers` entry pointing Codex at the agent-context MCP (5 tools) via the sigv4-proxy
   sidecar pattern; document auth (proxy reuse vs. direct SigV4), egress, and token cost. **Must run outside the
   #2713 soak window** (no config writes to the live `~/.codex/config.toml`; use a scratch `CODEX_HOME`).
   Deliverable: go/no-go note. Only pursue if a concrete need for live Codex tool access is identified.

---

## 9. Answers to the five spike questions (index)

| # | Question | Answer | Where |
|---|---|---|---|
| 1 | Mechanism | Per-run `AGENTS.md` in delegation cwd (consumption verified); not prepend/profile/global-config | §3 |
| 2 | Distilled content format | Parallel `personas/codex-distilled/<persona>.md` fileset; transfer mindset+conventions+quality bar, strip identity/outer-loop/mention | §4 |
| 3 | Review-mode upgrade | Parameterize `run-codex.sh review` by `AGENT_TYPE` (present in Bash — confirmed); keep read-only contract | §5 |
| 4 | MCP / skills surface | Codex 0.142.5 supports MCP (verified); but persona context → prompt pack, NOT MCP, in v1; MCP is a separate opt-in spike | §6 |
| 5 | No-second-runtime guard | Reaffirmed; passthrough is context-only; boundary enforced at the content layer by stripping outer-loop rules | §7 |

**Validation (per #2839):** all five questions answered with empirical Codex 0.142.5 evidence from the pod
(AGENTS.md consumption canary, MCP CLI surface, `AGENT_TYPE` in Bash, live-provider confirmation), and follow-up
implementation issues named with rough scope. Soak constraint honored — `~/.codex/config.toml` left on the
`adp-gateway` provider, no fallback activation.
