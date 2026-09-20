# Nightly CLI regression

[Nightly CLI Regression](../../.github/workflows/nightly-cli-regression.yml) is
the single scheduled entry point for the three CLI evaluation suites. It runs
daily at **05:00 UTC**, against **dev (AWS account 879318057152)**. GitHub may
start scheduled workflows later when runners are busy.

```bash
# Run the same complete regression on demand, from the reviewed default branch.
gh workflow run nightly-cli-regression.yml --ref main
gh run list --workflow nightly-cli-regression.yml --limit 5
gh run view RUN_ID
```

The parent first verifies the AWS account and reads the deployed gateway's
revision from the existing deployment evidence. It supplies that full SHA to
the EC2 suite, whose preflight rechecks the deployment and the served CLI hashes.
It never assumes that current `main` has already been deployed.

## Execution order and scenario index

All suites belong to **one GitHub Actions run**. The ARC runner orchestrates
them; the clean EKS pods and disposable EC2 instance remain the places where
the client commands execute. No client inherits the worker's configured agent
credentials.

| Order | Suite | Scenarios and authoritative definitions | Execution |
|---|---|---|---|
| 1 | CLI onboarding | [A: gate off, B: approval matrix, C5–C10: download, discovery, token import, permissions, direct inference, Claude Code and Codex; D: restore/cleanup](../../platform/evals/cli-onboarding/README.md) | Clean EKS pod |
| 2 | Budgets and rate limits | [Cases 1–12 and H: user/team/department/org budgets, reset boundaries, tenant isolation, accounting, RPM/TPM/concurrency and agent attribution](../../platform/evals/budget-ratelimit/README.md) | Clean EKS pod; real API/DB enforcement |
| 3 | CLI Uplift | [E01–E17: install/login/setup, AWS connection and handoff, Bedrock routing/inference, GitHub, parity, cleanup and multi-deployment isolation](../../tests/e2e/cli_uplift/cases.py) | Disposable EC2 |

The EC2 invocation always selects **`full`**, including E16/E17. It does not use
the standalone workflow's `login` checkpoint default. The individual workflows
remain reusable and manually dispatchable for diagnosis, but have no independent
cron. A shared live-suite concurrency group serializes standalone runs with
the nightly. The parent has a separate lock so calling a child cannot deadlock.

Later suites still run after an earlier suite fails, unless the run is cancelled
or the account/revision preflight fails. Each child must report a successful
cleanup sweep before the next child obtains the live-suite lock. A cleanup
failure stops later live mutations and fails the combined result.

## Reading the result

Open the **Nightly regression result** job summary for the combined verdict.
The same Actions run contains each suite's per-scenario summary. The EC2 child
also uploads its existing sanitized report, JUnit and cleanup evidence.

Every required job must succeed, including EC2 recovery, and the run must have a
verified full revision. A failed, cancelled, skipped or missing suite makes the
combined verdict **FAIL / INCOMPLETE**. Configuration or fixture gaps are not
converted into passes. The older per-onboarding tracking-issue step is removed;
there is one Actions result to monitor and no dependency on a missing issue label.

Budget-suite **findings** describe pinned current behavior, separately from
assertions; read them in that suite's summary. Passing an assertion about current
behavior does not establish a capability the finding says is missing.

## Current coverage limits

Scheduling all suites does not resolve their outstanding acceptance gaps:

- The EC2 fixture bindings currently omit destination/provisioner roles,
  isolated GitHub fixtures, hosted-agent fixtures and the three-deployment
  sign-in bindings. The corresponding cases report blocked when unavailable.
- E16/E17 model execution remains disabled until hard Codex output limits
  (256 tokens/request) and an aggregate 48-request limit are implemented.
  Scheduling must not bypass these guards. [#5413](https://github.com/aws-e/adp/issues/5413)
  remains open for live acceptance.
- The onboarding suite seeds the Cognito identity; the real GitHub OAuth browser
  leg is separately tracked in [#4166](https://github.com/aws-e/adp/issues/4166).

The full nightly will remain red while required EC2 cases are blocked. The
[EC2 runbook](cli-uplift-evaluation.md) documents fixture references and recovery.
Existing per-suite time limits, EC2 cost limits, protected environment and scoped
role checks continue to apply. This change does not provision additional fixtures
or deploy a new gateway revision.

The onboarding Codex configuration explicitly sets `web_search = "disabled"`
because the inference route rejects that tool. This preserves the real Codex
conversation and local authentication proxy. See the
[official Codex configuration reference](https://developers.openai.com/codex/config-reference/#web_search).
