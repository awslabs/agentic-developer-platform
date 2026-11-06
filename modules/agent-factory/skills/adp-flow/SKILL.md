---
name: adp-flow
description: Interview an intent, publish a verified backlog, and register an inert delivery-flow draft for owner acceptance.
disable-model-invocation: true
---

# adp-flow

Use this skill only when the owner invokes it with an intent in a repository checkout. The checkout must live under a retained path: the owner's home on a laptop, `/mnt/crew/workspace/<repository>` in a Kiro Crew session on a Dev Box. Never work in a session scratch directory or `/tmp`; a session resume can empty them, and the plan directory below is only safe once it is committed and pushed. Complete the preflight, interview, backlog draft, publication, plan and inert registration in that order. Pause for confirmation of the interview note and the backlog draft, not for extra approvals you invent. Registration is **not** approval: the owner alone decides whether to accept the engine's gate.

Resolve shared authoring resources with `python3 scripts/plan_writer.py --shared-dir` from this skill directory. Every `_shared/` reference below means that returned directory, not a sibling of the installed client skill. In a checkout it is the source skills' `_shared/`; after client setup it stays in the CLI bundle: `skills/` beside the resolved `adp` executable, the parent of its `bin/` directory, or `~/.adp/skills/` (the CLI's `ADP_HOME` override is also respected). Do not copy shared resources into a client directory or guess a different policy if the bundle is missing.

## Hard rules

- Never approve a gate. Never post mentions that trigger agents or add labels that trigger agents.
- Never register without a `proposed_execution_policy` with `expires_at`.
- Never register a story node without `repository_identity`, `provider_repository_id` and `repository_connection`.
- Never create an issue that the ledger or a title search shows exists.
- Never print tokens. Never write outside the delivery home plan directory and the target repositories' issues.

Do not use a dispatch label or request execution in an issue body, title, comment, PR, or command. Do not execute an approval command, even when one is displayed as a suggested owner action. Do not promote a machine evaluation without an existing verified deterministic harness; the default is human acceptance of every wave.

## 0. Preflight: confirm that authoring can succeed before writing

Read [checks.md](checks.md). Check `adp status --json` for a valid user session or the bound Dev Box owner shell; otherwise stop with `adp login` or an instruction to return to the owner shell. Check `gh auth status`. On the laptop, if that fails, set `GH_TOKEN` from `adp github token` without printing it, and retry once. Do not substitute a different account. Check `adp product list --json`, `adp product show --json`, `adp product sources --json`, `adp product budget show --json`, `adp github status --json` and `adp flow list --json`; resolve the selected Product, source repositories, installations and current checkout's `origin`. The orchestration engine feature must be enabled. If the budget command is unavailable, stop before writing rather than guessing Project headroom.

Every participating repository must be a bound Product source with an installed GitHub App and known installation ID. If `adp github status --json` lacks numeric repository IDs, read `gh api repos/OWNER/REPOSITORY --jq .id` for each repository. Never guess IDs or infer a binding from a similarly named repository. The Product delivery home holds the epic, plan and evaluation issues; if several repositories are bound but no delivery home is set, ask the owner which bound repository to use and set it using `adp product update` before continuing. Verify all selected sources before creating anything. Read the current checkout, existing issues and Product flows yourself rather than asking the owner for facts already available through the CLI.

## 1. Interview: confirm a concise intent note

Invoke `adp-grilling` for numbered rounds with recommended answers; "yes" accepts a recommendation. Seed it with seven questions: (1) measurable epic outcome; (2) selected Product and affected bound repositories; (3) delivery home; (4) two to four independently evaluable waves; (5) each wave's completion boundary and prohibited actions; (6) allowed actions, spend limit versus the **Project's available budget**, wall clock and expiry; (7) human evaluation per wave, or a named existing machine harness. Fetch factual answers from the preflight and repository; the owner decides scope and authority. Default to code-only actions and human evaluation. Continue until no unresolved decisions remain.

Write `intent.md` under `docs/delivery-flow/plans/<home-repo-short>-<epic-number>/` in the delivery home, using [intent.template.md](intent.template.md). Record the readable outcome and one fenced JSON interview record with `epic`, `title`, `description`, `story_repositories` (alias to one repository each), `waves` (each with `ref`, `title`, `description`, `stories`, `evaluation`) and optional dependency `edges` and lowered `policy` bounds. Confirm the note with the owner before continuing; on a retry, resume the note instead of restarting the interview.

For each operational wave, also record `actions` drawn from `policy.allowed_actions`, `effects` (`external_spend` and/or `irreversible`, when applicable), and `readiness_gate: {"title": "Owner confirms readiness"}` with the confirmed checkpoint's title. Either `deploy` or `operate`, external spend beyond agent runs, or an irreversible effect requires this gate. Omitted wave actions are code-only actions from the policy; omitted effects mean none. Deployment or operations authority in the policy must have a corresponding declared wave action, not an unused blanket grant. Non-deployment waves retain a code-only completion boundary when operational authority is present. These fields describe confirmed scope, not permission for this skill to operate. The writer inserts the gate after the preceding wave's evaluation and before every story in the affected wave; it remains separate from initial plan acceptance. Do not substitute `policy.human_gates`: those independently require a person for each named action even after a graph checkpoint is answered.

## 2. Backlog draft: review granularity before publishing

Read the canonical issue-authoring guide and epic/story/evaluation templates in `_shared/authoring/` (the repository sources are `modules/agent-factory/rules/agents/issue-authoring.md` and `modules/agent-factory/rules/templates/`). Reference those templates; do not copy or replace the authoring core in `aidlc-emit-issues`. One epic belongs in the home repository; one medium-or-smaller story belongs in exactly one affected code repository; one evaluation belongs in the home repository for **each** wave. Split any multi-repository story and join the pieces with a dependency edge. Use six-section story issues with stable acceptance keys, a named completion boundary and a full epic URL reference (not a closing keyword); respect the 8 KiB story limit. Every evaluation lists independent actions and expected results. Declare a gate before a wave that deploys, spends beyond agent runs or performs an irreversible action. Code and tests are not gates. The engine inserts its own plan-acceptance gate.

Present a numbered backlog with issue title, repository, wave, dependencies, evaluation and gates. Check edges are acyclic and there is exactly one evaluation per wave. Pause for explicit owner confirmation of granularity and edges; do not publish unconfirmed issues. Save the confirmed `backlog.json` in the plan directory as `issues` with unique `alias`, `kind` (`epic`, `story` or `eval`), `repository`, `title`, `body` and optional story `depends_on` aliases. Preserve the confirmed `story_repositories` mapping in the interview record.

## 3. Publish: journal every issue as it appears

Run `python3 scripts/publish.py PLAN_DIR` from this skill directory. The script creates the epic first, then stories in dependency order in their own repositories, then home-repository evaluations. It reads and immediately updates `story-mapping.json` for each issue, and searches the exact title in the target repository before creating a missing ledger entry. On retry, inspect and reuse the ledger, not a fresh issue number. It attempts native sub-issue links and reads them back; a cross-repository link failure is reported but does not block planning because the engine uses explicit issue references. Never file a story in the home repository just because linking failed.

The issue number, URL, repository, title and creation timestamp are saved before the node-ID lookup used for linking. A failed lookup leaves an incomplete ledger entry; retry enriches that same issue rather than searching or creating it again.

## 4. Plan: bind every node and enforce budget

Save verified Product, Project budget, home and repository facts to `facts.json` inside the plan directory. Provide `product_id`, `org_id`, `spec_revision` (a non-empty string naming the specification revision, such as the delivery home checkout's commit SHA; never the Product's integer revision), `emitted_at` (UTC), `home_repository`, `repositories` (each with `provider_repository_id` and `installation_id`) and `project_budget.available_usd` (number or `null`). Run `python3 scripts/plan_writer.py PLAN_DIR PLAN_DIR/facts.json`. It produces `proposal.authored.json`, `policy-intent.json` and `README.md`, using `_shared/policy.default.json`. Refuse unbound stories, missing evaluation references, a missing product or policy expiry, and a proposed spend limit exceeding capped Project headroom. Uncapped Projects remain metered and must be labelled accordingly in the README. Every story node must match the interview repository and ledger; all story repositories plus the delivery home belong in `repository_ids`.

An explicitly confirmed `policy.expires_at` is preserved unchanged and must include a timezone and be later than `emitted_at`. Only an omitted override defaults to fourteen days after emission; a blank, null, invalid or already-expired override is refused, never replaced. Missing readiness gates or operational authority without a declared gated wave are also refused before any plan files are written.

Run `.github/scripts/validate_loop_proposal.py` on the authored proposal in a checkout with gateway dependencies. Use `adp flow create`'s preview against the gateway to verify it would register, is bounded and has no unmet prerequisites; repair mechanical mismatches and re-preview, but stop for material scope changes. Never submit a proposal whose evaluation nodes lack their issue references.

## 5. Register and hand over: stop at the owner's gate

Run `adp flow create --file PLAN_DIR/proposal.authored.json --reason "confirmed intent" --json`. Treat exit code 4 with `flow_id`, `acceptance_gate_id` and `plan_hash` as a successfully registered **inert** draft, not an error and not permission to execute. Save `submission.json` with these values, `plan_version` and `status: registered_inert_not_accepted`. If already registered, read the matching existing flow rather than registering a duplicate. If a Dev Box lacks the CLI, commit the plan and give the owner the equivalent laptop registration and inspection commands; do not pretend registration happened.

Commit the plan directory with a message that references the epic issue (for example `#<epic>: delivery plan`); the organisation's commit rules (`commit_refs_rules`) refuse a push whose commits name no issue, and a refused push is amended and retried, never bypassed. Open a PR containing only the delivery-home plan directory after checks pass. Show the owner the gate ID, plan hash, plan-directory PR and the exact owner-side `adp flow gate approve GATE --expect-plan-hash HASH` command, with the actual values substituted. Explain that this command is for the owner on the laptop or dashboard and that this skill stops without running it. No story is dispatched until that gate is answered by the owner.
