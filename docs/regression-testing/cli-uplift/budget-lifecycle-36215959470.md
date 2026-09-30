# Budget and rate configuration live evidence

Disposable-EC2 [run 36215959470](https://github.com/aws-e/adp/actions/runs/36215959470) passed D06 against served gateway source `b1e266d97a1bf6e4a9c1805a02dd7482b3eafbe0`. The workflow checkout was `1599df2c3a082289ac78a0f40cf66cd1aee51fd7`; the report's `harness_commit` separately identifies its pinned tenant-validation dependency. The installed CLI performed the operations; the run also verified downloaded CLI hashes and real Cognito login/refresh.

D06 passed all four checks: six independent personal/cloud-agent daily/weekly/monthly caps; one exact update with stale-revision and ordinary-user refusal; rate-dimension updates preserving other dimensions with ordinary-user refusal; and unchanged settled usage after removing the policies. All six budget overrides and the rate override were verified absent after cleanup. No usage counter was reset and no inference was launched.

This advances #5589 and #5627 configuration acceptance. Actual Claude/Codex enforcement and restoration, spend-through, other hierarchy scopes, person-policy defaults and TPM accounting remain unqualified. The [machine-readable evidence](budget-lifecycle-36215959470.json) retains checks, exact policy revisions, cleanup, full-report hash and remaining holds.

The overall batch remains failed: D05 completed its six canonical identity checks and retired its exact principal/aliases, but its harness omitted the final success marker. #6309 repairs that reporting bug and tests failure/cleanup paths. The original result is not rewritten. EC2 `i-021ad1502068eab11` was independently confirmed terminated.
