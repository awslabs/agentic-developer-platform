# Developing at EPIC Scale

**How to take a feature idea from one paragraph to a fully built, evaluated, multi-PR delivery — using AIDLC, ADP's AI Development Life Cycle.**

This guide assumes nothing beyond a GitHub account with access to the repo. If you can open an issue and reply to comments, you can drive an EPIC.

---

## What AIDLC does

You write down **what** you want built and **why**. Agents do the rest — in two clearly separated halves, with you approving the boundary between every step of the first half:

| Half | Who works | What happens | Your role |
|------|-----------|--------------|-----------|
| **Inception** (design) | `@agent-aidlc` | Turns your intent into problem framing, an analysis of the existing code, formal requirements, and a delivery plan — one stage at a time | Review each stage's output; approve, give feedback, or skip |
| **Construction** (build) | developer / operations / reviewer agents | Implements the plan as a wave-sequenced series of PRs, each wave verified by a deterministic evaluation before the next begins | Approve the loop plan once, then watch (and review PRs if you want) |

The core contract: **nothing advances without your written approval, and once you approve the delivery loop, it runs itself.** There are exactly two kinds of human decision:

1. **Stage gates** during Inception — you approve each design artifact.
2. **The loop-proposal gate** — the last gate. You approve the execution plan (which stories run in which wave, against which AWS account, verified by which checks). After this, the build is autonomous.

---

## Quick start (the 5-minute version)

1. **Open a GitHub issue** describing what you want and why (see [Writing a good intent](#writing-a-good-intent) below).
2. **Add the `aidlc-intent` label** to the issue. That's the trigger — the workflow starts automatically, no mention needed.
3. **Wait a few minutes.** The agent posts a "Started inception" comment and begins the first stage. A live progress tracker appears at the bottom of your issue body.
4. **When a gate comment appears** (🚦), read the linked artifact and reply with exactly one of:
   - `@agent-aidlc approve`
   - `@agent-aidlc feedback: <what to change>`
   - `@agent-aidlc skip`
5. **Repeat** for each stage (typically 4–5 gates).
6. After you approve the **final gate** (`loop-proposal`), the delivery loop materializes and wave 1 starts building. You're done deciding; now you're observing.

> ⚠️ **Gate replies must be comments containing the `@agent-aidlc` mention.** Emoji reactions, checkbox ticks, and replies without the mention are **not seen**. One `@agent-…` mention per comment — never mention two different agents in the same comment.

---

## Writing a good intent

The intent issue is the only input the workflow gets, so quality in = quality out. You don't need a design — that's the agent's job — but you do need to be concrete about the goal.

A strong intent has four parts:

```markdown
## Intent
One or two paragraphs: what should be true when this is done, from the
user's point of view. Plain language. No implementation detail required.

## Why
The problem this solves or the gap it closes. What's painful today.

## Constraints & known facts
Anything you already know that the design must respect: which AWS account,
which existing modules are involved, security requirements, what is
explicitly out of scope, known gotchas. The more you put here, the fewer
feedback cycles you'll spend at gates.

## Definition of done
One paragraph or a short list: the observable end state. Ideally something
a script could check.
```

Real example (condensed from EPIC #3557):

> **Intent**: Users of the ADP dashboard should be able to click a "GitLab" link and land in the hosted GitLab already signed in via the shared Cognito session — no SSM tunnel, no cert warning.
>
> **Why**: Today GitLab sits behind an internal ALB reachable only via an SSM port-forward and a hosts-file hack — fine for a demo, not a product surface.
>
> **Constraints**: CloudFront VPC origin onto the existing internal ALB; the ALB must NOT become internet-facing. Environment: dev / embark1 (879318057152, cred label `adp-embark1`). Out of scope: git-over-SSH, per-tenant instances.
>
> **Definition of done**: A dashboard user clicks the GitLab link, gets a valid cert, lands signed in via SSO, and the agent round-trip plus E2E suite stay green with the old workarounds removed.

Tips:

- **Say which environment / AWS account** the work targets, including the `adp-cred` label if you know it. The delivery plan is required to state these explicitly, and the emitter will stop and ask if they're missing.
- **List retirement goals** if the work replaces workarounds — "once X is live, remove Y and Z" becomes its own delivery phase.
- **Name what's out of scope.** Scope containment is one of the agent's explicit duties; give it a fence to hold.
- If this EPIC is part of a larger effort, note the parent issue — the agent links it as a native GitHub sub-issue.

---

## The Inception stages (what each gate is asking you)

The workflow runs up to four design stages, each ending in a gate. Scope is auto-detected from complexity (a simple PoC may skip stages; skipping never removes the gate on the stages that do run).

| Stage | Artifact produced | What to check before approving |
|-------|-------------------|-------------------------------|
| **intent-capture** | `problem-frame.md` — the agent's restatement of your goal, actors, assumptions | Did it understand you? Are the assumptions right? This is the cheapest place to correct course. |
| **reverse-engineering** | `reverse-engineering.md` — analysis of the existing code the work touches | Are the named modules/files actually the right integration points? Wrong reuse claims here become wrong designs later. |
| **requirements-analysis** | `requirements-analysis.md` — numbered functional + non-functional requirements, acceptance criteria | Is anything missing? This is where you add requirements ("also add a Back-to-ADP link…") via `feedback:`. |
| **delivery-planning** | `delivery-planning.md` — child-issue decomposition, dependency graph, effort estimates, risk gates | Is the breakdown sensible? Does every deployment step name the AWS account, credential label, and a dispatchable CI workflow? |

All artifacts are committed to the work branch under `aidlc/spaces/issue-<N>/inception/` — the gate comment links them directly. The issue-body tracker shows a table of stage → status → artifact at all times.

**Gate discipline is hard-enforced**: the agent executes exactly one stage per run, posts the gate, and terminates. There is no auto-advance mode. If a run dies before posting its gate, a fallback enforcer posts it for you — you never need to guess whether it's your turn.

### Replying to a gate

- `@agent-aidlc approve` — accept the artifact, advance to the next stage.
- `@agent-aidlc feedback: <notes>` — the agent revises the **current** stage's artifact and re-gates. You can iterate as many times as you like. Feedback can add scope ("add a requirement for…"), correct facts, or demand rework.
- `@agent-aidlc skip` — mark the stage skipped and move on. Use sparingly.

If you mention `@agent-aidlc` **without** one of these answers while a gate is open, the agent just re-posts the gate and exits — it never advances on an ambiguous reply.

---

## From plan to backlog: the emission (Run A)

When you approve **delivery-planning**, one automated run does two things:

1. **Emits the backlog.** Your issue becomes the EPIC. One child issue is created per unit of work, each in the repo's mandatory five-section format (Description / Impact analysis / Design / Deployment / Validation) with deterministic validation criteria, and each linked as a **native GitHub sub-issue** — so the EPIC shows an "N of M done" progress bar.

   **Story issues are created inert.** No agent is dispatched on them, no trigger labels, no mentions. Nothing builds yet.

2. **Composes the delivery-loop proposal.** The delivery plan's dependency graph is partitioned into **waves** (wave 1 = no dependencies, wave 2 = depends only on wave 1, …). For each wave the run drafts — as reviewable files on the branch, not as issues:
   - an **orchestrator** body (which stories, in what order, deployed how, to which account),
   - an **evaluation** body (concrete command-and-expected-output checks that prove the wave works).

   The drafts pass a four-rule lint before they're shown to you: every infra deploy must reference a dispatchable CI workflow; every deployment must name the AWS account + `adp-cred` label explicitly; version pins must cite maintained releases; every orchestrator carries the hotfix-branch protocol.

Then the final gate posts: **`loop-proposal`**.

### Reviewing the loop-proposal gate (the one to take seriously)

This is your last decision before autonomous execution. The gate comment shows the wave table, the target account, and per-wave check counts, with links to every draft. Check:

- **Wave partition** — do the dependencies look right? Is anything in wave 1 that should wait?
- **Account + credential label** — present in *every* orchestrator and evaluation, and correct. This is where a wrong-account deploy gets caught.
- **Evaluation checks** — every check must be a concrete command with an expected output ("`curl -s …/health` returns 200"), never "verify it works". If you see a judgment-call check, send `feedback:`.
- **CI workflow references** — every `gh workflow run …` named in the drafts must actually be dispatchable.

Reply `@agent-aidlc feedback: …` to revise the drafts, or `@agent-aidlc approve` to launch.

---

## Autonomous construction (Run B and beyond)

On loop-proposal approval, a single run **materializes** the loop from the approved drafts verbatim (re-linting first; if a draft was hand-edited into an invalid state, the run refuses and re-gates rather than building from a bad plan):

1. Evaluation issues are created first, then orchestrator issues (which reference them), all linked as sub-issues of the EPIC.
2. The run posts **one** dispatch: an `@agent-operations` mention on the **wave-1 orchestrator**. That is the only dispatch the emitter ever performs — story dispatch belongs to the orchestrator.

From here the loop drives itself:

```
wave N orchestrator
  → dispatches each story to @agent-developer, in dependency order
  → each story: PR opened → CI green → reviewed → merged → deployed
  → runs the wave-N evaluation issue (deterministic checks)
      → checks green?  → advance to wave N+1
      → checks red?    → file a DEFECT issue (five-section), dispatch a
                         developer on the DEFECT (never re-mention a merged
                         story), re-run the eval after the fix merges
```

There are **no human checkpoints inside the loop** — that's by design. The human review happened at the gates, where it changes outcomes cheaply. You can of course still review PRs as they appear (they're normal PRs), comment on issues, or intervene — but the loop doesn't wait for you.

The EPIC is done when every wave's evaluation has closed green and the EPIC-level definition of done holds.

---

## Watching progress

- **The EPIC issue body** carries a live tracker (auto-updated between sentinel markers): scope, a progress bar, a stage/status/artifact table, and a callout when a gate is waiting on you. Your original intent text above the tracker is never touched.
- **The EPIC's sub-issue tree** (GitHub sidebar) shows stories, orchestrators, and evaluations with close-state roll-up.
- **Branch artifacts** live under `aidlc/spaces/issue-<N>/` on the `agent/issue-<N>` branch — the full audit trail of every design decision.
- **Agent run comments** on each issue show start/progress/completion with links to logs.

---

## Rules that keep the system healthy

These aren't etiquette — violating them causes duplicate runs, mis-routed dispatches, or stuck work.

1. **Trigger agents with `@agent-<persona>` mention comments, never labels.** `agent-developer`-style labels do not dispatch agents and must not be added to issues. (The `aidlc-intent` label on a *new issue* is the one label-based entry point, and it's for starting the AIDLC workflow only.)
2. **One `@agent-…` mention per comment.** The router picks one mention per comment — a second mention of a different persona mis-routes the whole comment. Refer to other personas in prose without the `@`.
3. **Gate replies need the mention.** `approve` alone does nothing; `@agent-aidlc approve` advances.
4. **Don't hand-edit the loop drafts** between the loop-proposal gate and approval — Run B re-lints and will refuse to materialize from drafts that no longer pass.
5. **Don't dispatch agents on story issues yourself** while a loop is running — the orchestrator sequences them. If a story looks stuck, comment on the **orchestrator**.
6. **Keep intent issues reasonably sized.** Deep design detail belongs in the artifacts the workflow produces, not in a 40KB issue body.

---

## FAQ

**How long does Inception take?**
Each stage runs in minutes; the wall-clock time is dominated by how quickly you answer gates. A focused session gets a complex EPIC from intent to loop-approval in a morning.

**Can I change my mind mid-Inception?**
Yes — `feedback:` at any gate revises the current stage. Adding a requirement at the requirements-analysis gate (that's what it's for) is much cheaper than discovering it in wave 3.

**What if the agent's design is wrong?**
That's what the gates are for. Nothing is built until you approve the loop proposal, and every artifact is reviewable before that.

**What if a wave fails its evaluation?**
The loop files a defect issue and fixes it autonomously, re-running the evaluation until green. You'll see the defect issues appear under the EPIC.

**Can two people run AIDLC intents at the same time?**
Yes. Every intent is isolated to its own issue-scoped workspace (`aidlc/spaces/issue-<N>/`) and work branch; concurrent intents don't interact.

**What does it cost?**
Each stage is a bounded agent run; the tracker's cost column shows per-stage figures when available. The expensive part is Construction, which is proportional to the number of stories — the delivery-planning gate shows effort estimates before you commit to anything.

**Where do I look when something seems stuck?**
The issue's latest comments (is a gate waiting on you?), then the tracker table, then the orchestrator issue for the current wave. If a gate run died silently, re-mention `@agent-aidlc` on the issue — the startup guard re-posts the open gate without advancing anything.

---

## Reference

| Thing | Where |
|-------|-------|
| Persona rules (the contract this doc describes) | `modules/agent-factory/rules/personas/aidlc.md` |
| Emission skill (stories + loop mechanics) | `modules/agent-factory/skills/aidlc-emit-issues/SKILL.md` |
| Orchestrator / evaluation / defect templates | `modules/agent-factory/rules/templates/delivery-loop/` |
| Five-section issue format + orchestration limits | `docs/orchestration-issue-guide.md`, `CLAUDE.md` |
| Worked example: full EPIC end-to-end | Issue #3557 (GitLab via CloudFront) — intent → 4 gates → 7 stories → 4-wave loop |
