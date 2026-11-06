# Shared authoring policy

`policy.default.json` is the policy template used by the hosted
`aidlc-emit-issues` skill and the intent-to-flow skill. Copy it into
`proposed_execution_policy` while authoring a new proposal; do not submit the
template unchanged. Replace the tenant and repository placeholders with verified
run or preflight facts and compute `expires_at` from the emission time in UTC.
Enumerate **every** eval node address in `evaluation_acceptance` with value
`"human"`, including evals added to the plan after the initial draft. Never
infer missing identity or permission; stop and obtain it instead.
Each story must also bind its verified repository and GitHub installation, and
the flow must name the selected Product. Run `check_proposal.py` to catch a
missing binding before registering the authored draft.

This file is a starting bound, not authority to accept a plan. Only the engine's
human acceptance gate can grant the proposed actions. Existing committed
proposals remain unchanged.

## Authoring guide and templates

The CLI skills archive packs the canonical issue-authoring guide and templates
into `_shared/authoring/` beside this file, so an installed client needs no
aws-e/adp checkout:

- `issue-authoring.md` (from `rules/agents/issue-authoring.md`)
- `epic-issue.md`, `developer-issue.md` (from `rules/templates/`)
- `evaluation-template.md`, `defect-template.md` (from `rules/templates/delivery-loop/`)

In a repository checkout, read the `rules/` sources directly; they are the
authority and these copies follow them release by release.
