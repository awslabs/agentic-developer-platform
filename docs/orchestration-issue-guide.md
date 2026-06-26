# Guide: Writing an Orchestration Issue

How to create an issue that drives a multi-story build through hosted agents — i.e. an `@agent-operations` issue that dispatches `@agent-developer` runs across many child stories and tracks them to completion.

This guide is written from hard-won experience (the Knowledge Layer multi-tenant build, June 2026). The failure modes below are real and were each hit in practice.

---

## TL;DR — the rules that matter

1. **Keep the orchestrator body SMALL.** A few KB, not 15KB. Link to child stories; do not inline their detail.
2. **One persona `@mention` per comment.** Never put a second `@agent-X` token in a comment that triggers `@agent-Y`. (Humans trigger via a bare `@agent-<persona>`; agent-to-agent dispatch is now marker-gated and automatic — see "How triggering works".)
3. **One mini-orchestrator per wave/EPIC**, each covering a handful of stories — not one mega-issue covering everything.
4. **The orchestrator issue must NOT have its own `agent/issue-<N>` work branch.** Never let a developer run implement *on* the orchestrator issue.
5. **Detail lives in the child stories.** The orchestrator only sequences and links.

---

## Why small matters (the #1 failure)

The agent worker assembles a prompt from the issue body + persona + rules + skills. A large issue body inflates that prompt dramatically. In practice a **~14.5KB issue body produced a ~45KB assembled prompt**, and the operations agent **came online, ran a few turns, then the process terminated ~58s in with no error** — repeatably. A **1KB** mini-orchestrator doing the *same kind of work* (dispatch developers, track to merge) ran fine and dispatched successfully.

**Symptom to recognise:** the agent posts "Agent running — Ns elapsed" once, then goes silent and never posts a plan or completion. If your orchestrator issue is large, suspect the size first.

**Rule:** orchestrator bodies should be ~1–3KB. Put the depth in the child stories (which the developer agent reads when it works that story) and have the orchestrator *link* to them.

---

## Structure: lean index + per-wave mini-orchestrators

Don't build one giant orchestrator. Build:

- **A lean master index issue** (human-readable rollup) — a compact table of waves → mini-orchestrators, the dependency order, what's blocked, and what's out of scope. ~2KB. **No agent runs this**; it's the map.
- **Several small mini-orchestrator issues**, one per wave (or per EPIC), each listing 3–6 stories and tagged to `@agent-operations`. Each is small enough to run reliably.

Example mini-orchestrator body (this shape works):

```markdown
## Description
Small orchestration — drive these stories to merge, in order.

## Stories (dependency order)
1. #1784 — migration (no dependency)
2. #1790 — migration (after #1770, merged)
3. #1772 — ACL filter (after #1770, merged)

## How to run
For each story: comment on it with a single `@agent-developer` mention to dispatch;
wait for its PR to open + merge (CI green); post a one-line status here.
Read each story's own body for detail — do NOT implement yourself.
```

---

## How triggering works (updated 2026-06-26 — dispatch-marker model)

There are now **two distinct trigger paths**, because the webhook distinguishes a deliberate dispatch from an `@agent-X` token that merely appears in prose (issues #2149 / #2161 / #2164):

- **Human comment** → a bare `@agent-<persona>` mention triggers, exactly as before. **This is unchanged — humans dispatch agents the same way they always have.** When you (a human/operator) trigger an orchestrator, just `@agent-operations` as usual.
- **Bot/agent comment** → a bare `@agent-X` in the body does **not** trigger. A bot comment only dispatches if it carries an explicit `adp-dispatch:<persona>` marker (inside the HTML correlation comment). The `gh` wrapper in the agent image injects that marker automatically when an agent runs a real cross-issue `gh issue comment … @agent-<persona>` dispatch — agents never hand-write the marker; they dispatch the same way and the wrapper marks it.

**Why this changed:** previously an agent's own status boilerplate (`## @agent-developer Started`, `**Agent**: @agent-architect`, plan/review headers) was read as a dispatch and self-triggered endless runs — the source of runaway loops and the "countless agent online" email storms. Now only deliberately-marked bot comments dispatch; status prose is inert.

### The mention-parser quirk (still applies)

The webhook routes a comment to the **first `@agent-X` mention in MENTION_TO_PERSONA dict order** (developer wins over most others). So:

- **Never** put two different persona mentions in one comment. A comment meant to trigger `@agent-operations` that also contains the literal text `@agent-developer` (even in an instruction like "dispatch via @agent-developer") will route to the **developer** persona instead.
- When you must *describe* dispatching another persona inside an orchestrator's trigger comment, write it as prose ("dispatch to the developer persona") — do **not** write the `@agent-` token. (This still matters for *human*-authored trigger comments, which are marker-free and route purely by mention.)
- Consequence if you get this wrong: a developer agent picks up the orchestrator issue and tries to *implement* it, creating an `agent/issue-<orchestrator>` branch + PR. That then poisons the issue (see next section).

---

## Don't let the orchestrator issue get its own work branch (the #3 failure)

The worker derives a fixed branch `agent/issue-<N>` from the issue number. If a developer run ever fires on the orchestrator issue (e.g. via the mention quirk above), it creates `agent/issue-<orchestrator>` + a PR. After that, **every** subsequent `@agent-operations` run on that issue hits the bootstrap "branch exists with open PR → extend" path, and the `git checkout` can fail — blocking the orchestrator.

**Rules:**
- Orchestrator issues are for *dispatching*, never *implementing*. Keep their branch clean.
- If a stray PR appears on `agent/issue-<orchestrator>`, re-home that work onto the correct `agent/issue-<story>` branch and delete the orchestrator branch before re-triggering.

---

## FIFO queue behaviour (the #4 failure — operational)

Agent dispatches flow through a **FIFO SQS queue** (`adp-dev-agent-submit.fifo`), grouped per `tenant#repo#issue` so runs on the **same issue serialize** (one in-flight at a time) while different issues run in parallel.

**Largely fixed as of 2026-06-26 (#1864 + #2155):** the worker now deletes its message on completion (success path) and an idempotency guard no-ops a redelivered message for an already-merged/closed story. Combined with the dispatch-marker fix (#2149) that stops self-triggering, the runaway-redelivery/loop class is closed. Historically (pre-fix) a message that wasn't deleted sat **in-flight for 6 hours** then redelivered a redundant run; if you see that pattern recur, #1864/#2149 may have regressed.

**Operational guidance (still useful for diagnosis):**
- If dispatch seems stuck or stories appear to "loop," check the queue: `aws sqs get-queue-attributes --queue-url <submit.fifo> --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible`. A high, **climbing** `NotVisible`/visible count with no live pods = a problem; a low serialized drip is normal.
- **Distinguish concurrency from redelivery:** because grouping is per-issue, several *sequential* runs on one issue is FIFO serialization working, not a runaway. A runaway shows as many *different* issues/sessions spawning at once, or a closed issue getting repeated `## @agent-X Started` comments.
- Purging the submit FIFO is safe **only after** confirming any run you care about is already in-flight/processing — purge removes queued messages, not ones already consumed by a running pod. Merged work is safe in `main`; purging only drops pending *dispatch* messages, which you re-trigger deliberately.
- **Emergency stop:** to halt all new agent spawns without code/queue changes, pause the KEDA ScaledJob: `kubectl annotate scaledjob agent-scaledjob -n adp-agents autoscaling.keda.sh/paused=true --overwrite` (remove the annotation to resume). Existing runs finish; no new pods spawn.

---

## Child stories: where the detail goes

Each child story (not the orchestrator) carries the full five-section issue convention (Description / Impact / Design / Deployment / Validation — see `CLAUDE.md`). For build orchestration, each story's **Validation** should make the ownership split explicit:

- **At-PR (developer agent owns):** the unit/integration tests that must be green before merge.
- **Post-merge deploy → verify → smoke (orchestrator/ops agent owns):** the deploy verification + smoke test, because a developer agent **cannot** validate a deployment it didn't trigger — its job ends at a merged PR + green CI.

State the deployment target and credential method in deploy-gated stories (e.g. "verify against account `<id>` via `adp-cred assume --service aws --label <label>`").

---

## Sequencing gotchas

- **Shared modules first.** If two sibling stories both introduce a shared module (e.g. a common `scope.py`), the second to merge will conflict. Either sequence them (shared-module story merges first) or call it out so the second rebases onto the first.
- **Migrations create alembic-head collisions** when filed in parallel; agents may auto-generate a merge-migration to reconcile — fine, but expect it.
- **EPIC auto-close:** a developer PR that says "Closes #<EPIC>" instead of "Closes #<story>" will close the parent EPIC prematurely. Review PR close-references; reopen EPICs that aren't actually complete.

---

## Checklist before triggering an orchestrator

- [ ] Orchestrator body is ~1–3KB; detail is in linked child stories.
- [ ] Each child story exists and is wired under its EPIC as a **native GitHub sub-issue** (not just a text `#N` reference — use `gh api -X POST repos/<org>/<repo>/issues/<parent>/sub_issues -F sub_issue_id=<child .id>`; note `-F`/integer id, not `-f`), with its own Validation (at-PR + post-merge split).
- [ ] Your (human) trigger comment contains exactly **one** `@agent-` mention. (Agents dispatching each other is now marker-gated and automatic via the `gh` wrapper — see "How triggering works".)
- [ ] The orchestrator issue has no stray `agent/issue-<N>` branch/PR.
- [ ] Dependency order is stated; blocked stories (e.g. on an identity-mapping issue) are explicitly excluded.
- [ ] You know how to check the submit FIFO and pause the ScaledJob if dispatch stalls or loops.

---

## References
- `CLAUDE.md` — the five-section issue-authoring convention and the `@mention`-not-labels rule.
- `modules/agent-factory/rules/agents/github-issue-hierarchy-guidelines.md` — native sub-issue hierarchy.
- Bug #1864 — SQS message not deleted on completion → 6h FIFO redelivery (the queue-jam root cause).
