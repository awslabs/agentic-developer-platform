> Part of [epic #8887](https://github.com/aws-e/adp/issues/8887). Wave 1 run-only evaluation. Run after all 12 implementation stories have verified merges; no deployment is required. Do not implement anything here.

## Description

Evaluate the code phase of the [POC flow](../README.md). Follow its [verification contract](../verification.md). This evaluation uses the existing tick-owned engine-repository-evidence-v1 reader, with the predecessor/check bindings in proposal.authored.json. It dispatches no custom runner or deployment. Code work starts after initial acceptance; S01 records readiness gaps. Named owners/delegation and private launch inputs must be verified before live operations. No work is dispatched by creating this issue.

## Checks (all must pass — no judgment calls)

1. For each of the 12 direct code predecessors, the native reader resolves the current PR binding and independently verifies its merge receipt. Diagnostic equivalent: `gh api repos/aws-e/adp/pulls/"$PR_NUMBER" --jq '{merged,merge_commit_sha,head:.head.sha}'` returns `merged: true`, the accepted merge SHA and the bound PR head. Unknown or stale bindings fail.
2. `gh api repos/aws-e/adp/commits/"$PR_HEAD"/check-runs` contains every required successful check declared for that predecessor, from app ID 15368 and the bound workflow where supplied. The machine reader performs these checks with the registered installation. Missing, pending, failed or skipped required checks do not pass. Source: all 12 stories' code/CI validation contracts.
3. S01 requires existing Public Documentation `check`; S12 requires Gateway `Lint`/`Test` and `DevLake POC / verify` from `.github/workflows/devlake-poc.yml` (workflow ID `399225481`). Its merged PR #9180 predates `insights-ci.yml`; the applicable DevLake check passed on its exact head in [run 40684995468](https://github.com/aws-e/adp/actions/runs/40684995468). S09 introduces `DevLake POC / verify`; its later consumers require that job alongside their declared component CI. Early stories cannot be blocked on a later-created job.
4. The engine records 12 verified merge/check sources and releases the campaign operation only when its own admission requirements are satisfied. This does not close live feature ACs. Accepted private launch inputs and S07 runner/repair/continuation self-checks are rechecked by the operation before cloud effects.

## Defect protocol

Product checks run before the operations owner can finish, so normal failures stay in its bounded repair/CI/merge/retry loop. The native read-only evaluator only retries observation; it cannot resurrect a passed operation. A remaining provenance/authority or unsupported-scope failure is an explicit exception. If an evaluation check fails:
1. File a NEW five-section defect issue using the repository defect template, quoting the exact failing command, redacted actual output and owning story.
2. Attach it as a native sub-issue of #8887.
3. Under accepted execution authority, dispatch the developer persona on the DEFECT using `adp-trigger`; never dispatch implementation on this evaluation or the merged story.
4. Re-run this evaluation after the defect PR passes checks and its merge is verified. Redeploy only changed installed artifacts; reuse the coherent release for verification-only repairs.
5. Close this issue ONLY when every required check passes in one complete evaluation run. No skipped/not-run required check counts as passed.
6. Post the passing transcript before closing; keep private evidence in its protected store and link a sanitized report.

## Evaluation report

Lead with whether this phase is ready. Include passed, failed, skipped and not-run counts and missing required checks. For each failure name the affected user workflow, next owner/action and defect link. Include exact commands and sanitized transcript under technical details; preserve release/evidence provenance privately. The native machine reader records acceptance from the exact bound evidence. No routine human evaluation is required. Its read-only observation does not implement repairs or grant deployment authority.
