# Resuming a story branch

A new worker process does not imply a new source revision. Hosted workers reuse
`agent/issue-<number>` and reconcile it with the repository's current default
branch before starting Claude or Codex. New stories start from that default
branch. Existing story commits and saved task boards remain on the same branch.

Reconciliation uses a merge rather than a rebase: published story history stays
intact, no force push is required, and both the previous story head and the base
remain ancestors of the result. Task-board completion still needs validation
against the refreshed code; bringing in the base does not complete story tasks.

## Runtime coverage

The common Python entrypoint prepares hosted workspaces before selecting the
Claude or Codex adapter. All branch-writing personas, including operations,
developer and architect, use this preparation. Shared platform instructions tell
agents to preserve the board, resolve pending conflicts and test affected behavior.

Report-only Codex product, PM and intent-refinement runs already read the current
default branch and do not own a story branch. Reviewers and engine review/repair
cycles retain the exact assigned PR revision. Their existing authorized repair
controller owns base integration and subsequent review; startup must not silently
change the commit being reviewed.

Standalone Codex developer/architect also adopts the canonical branch. An existing
legacy `agent/codex-issue-<number>` is reused if there is no canonical branch. If
both exist and the canonical branch does not contain the legacy head, startup
stops for consolidation instead of abandoning either history.

## Git and protected workspaces

Ordinary Git workers fetch the default branch and restore shallow history when
needed. Before merging they save the original head under
`refs/adp/before-base-reconciliation/<sha>`. Clean merges remain local until the
normal publication step. Conflicts remain in the index with both versions for
the agent to resolve. Dirty workspaces and failed fetches are refused intact.

Protected workers receive a one-commit archive, without the history required for
a local three-way merge. The typed Gateway operation `reconcile_work_branch`
therefore merges only the assigned default branch into the assigned story branch
at GitHub before materialization. It verifies authority, repository identity and
both ancestors. It cannot target main, accept arbitrary merge content, force
update a ref, or merge a PR. A retry after a lost response observes the completed
merge instead of repeating it. Current branches require only a read token;
integration uses contents/workflows write permission held inside the Gateway.
The downloaded snapshot must match the reconciled head. A branch change during
materialization stops startup for a retry instead of launching on unverified code.

**Protected merge conflicts currently require a separate authorized branch/PR
repair.** Startup reports the branch and both revisions, preserves all work and
stops. Repeated restarts alone cannot resolve that conflict. Providing protected
workers with an authorized three-way conflict workspace is a separate capability;
this change does not grant them raw credentials to bypass their publication scope.

## Rollout and verification

Deploy the updated Gateway before enabling the updated workers. Rebuild and roll
out the shared `adp-agent-runtime` image used by hosted Claude and Codex story
agents, including the shared rules and regenerated Codex catalogue. Deployments
with separate worker images must rebuild each image. An older Gateway refuses the new operation; there is no fallback
that silently resumes outdated code. Existing running workers need a new attempt
to use this startup behavior.

Verify a restarted story retains its branch and task-board contents, incorporates
a file added to the default branch since the previous run, and records both heads
in the reconciliation log. Repeated starts with an unchanged base should be no-ops.
The regression suites cover real Git histories, shallow clones, conflicts, repeat
starts, legacy branch adoption, policy denial, races and lost provider responses.

This does not fix deployment IAM denials, evidence storage or other delivery-flow
failures. It addresses source reconciliation at worker startup.
