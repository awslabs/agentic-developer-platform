# Nightly CLI regression

[Nightly CLI Regression](../../.github/workflows/nightly-cli-regression.yml) is
the single scheduled entry point for the three CLI evaluation suites. It runs
daily at **05:00 UTC**, against **dev (AWS account 879318057152)**. GitHub may
start scheduled workflows later when runners are busy.

```bash
# Run the nightly key scenarios on demand, from the reviewed default branch.
gh workflow run nightly-cli-regression.yml --ref main
# Run the full acceptance matrix when the additional fixtures are ready:
gh workflow run nightly-cli-regression.yml --ref main -f ec2_scope=full
gh run list --workflow nightly-cli-regression.yml --limit 5
gh run view RUN_ID
```

The parent first verifies the AWS account and reads the deployed gateway's
revision from the existing deployment evidence. The EC2 child resolves its own
revision immediately before building its run config: dev can advance during the
onboarding and budget suites. EC2 preflight still checks that exact revision and
the independently derived served CLI hashes. Recovery uses the same resolved pin.
Both revisions appear in the combined summary; a deployment change between suites
is reported, not presented as single-revision acceptance. Neither pin assumes
that current `main` has already been deployed.

## Execution order and scenario index

All suites belong to **one GitHub Actions run**. The ARC runner orchestrates
them; the clean EKS pods and disposable EC2 instance remain the places where
the client commands execute. No client inherits the worker's configured agent
credentials.

| Order | Suite | Scenarios and authoritative definitions | Execution |
|---|---|---|---|
| 1 | CLI onboarding | [A: gate off, B: approval matrix, C5–C10: complete CLI installation, discovery, token import, permissions, direct inference, Claude Code and Codex; D: restore/cleanup](../../platform/evals/cli-onboarding/README.md) | Clean EKS pod |
| 2 | Budgets and rate limits | [Cases 1–12 and H: user/team/department/org budgets, reset boundaries, tenant isolation, accounting, RPM/TPM/concurrency and agent attribution](../../platform/evals/budget-ratelimit/README.md) | Clean EKS pod; real API/DB enforcement |
| 3 | CLI Uplift key scenarios | [E01: fresh install and release hashes; C01: native admin login, bad credentials and refresh; cleanup/recovery](../../tests/e2e/cli_uplift/cases.py) | Disposable EC2 |

The daily EC2 invocation selects **`login`** (E01 + C01). This is the currently
supported nightly scope, alongside real Claude/Codex inference in the onboarding
pod and the entire budget harness. A green daily run means these key scenarios
passed; it does **not** establish full CLI Uplift acceptance.

Select `ec2_scope=full` on the same manual trigger for E01–E17: admin challenges
and setup, AWS connection/handoff, routing, personal/hosted inference, GitHub,
parity, cleanup and multi-deployment isolation. The full matrix's grading is
unchanged: blocked/not-run cases keep it red. E02–E17 remain outside the nightly
gate and are explicitly listed as such in every combined summary.

The individual workflows remain reusable and manually dispatchable for diagnosis, but have no independent
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
verified full SHA for both the initial and EC2 snapshots. A failed, cancelled, skipped or missing suite makes the
combined verdict **FAIL / INCOMPLETE**. Configuration or fixture gaps are not
converted into passes. The older per-onboarding tracking-issue step is removed;
there is one Actions result to monitor and no dependency on a missing issue label.

Budget-suite **findings** describe pinned current behavior, separately from
assertions; read them in that suite's summary. Passing an assertion about current
behavior does not establish a capability the finding says is missing.

## Current coverage limits

The key-scenario nightly does not resolve full acceptance gaps:

- The EC2 fixture bindings currently omit destination/provisioner roles,
  isolated GitHub fixtures, hosted-agent fixtures and the three-deployment
  sign-in bindings. The corresponding cases report blocked when unavailable.
- E16/E17 model execution remains disabled until hard Codex output limits
  (256 tokens/request) and an aggregate 48-request limit are implemented.
  Scheduling must not bypass these guards. [#5413](https://github.com/aws-e/adp/issues/5413)
  remains open for live acceptance.
- The onboarding suite seeds the Cognito identity; the real GitHub OAuth browser
  leg is separately tracked in [#4166](https://github.com/aws-e/adp/issues/4166).

An explicitly selected full run remains red while required EC2 cases are blocked. The
[EC2 runbook](cli-uplift-evaluation.md) documents fixture references and recovery.
Existing per-suite time limits, EC2 cost limits, protected environment and scoped
role checks continue to apply. This change does not provision additional fixtures
or deploy a new gateway revision.

The onboarding Codex configuration explicitly sets `web_search = "disabled"`
because the inference route rejects that tool. This preserves the real Codex
conversation and local authentication proxy. See the
[official Codex configuration reference](https://developers.openai.com/codex/config-reference/#web_search).

The onboarding pod runs the **deployed installer** so the auth proxy receives all
its sibling modules (including `adp_deployments.py`). Missing proxy downloads fail
live runs. Budget seed SQL supplies the required JSON defaults explicitly, since
raw SQL does not invoke ORM defaults. A fatal seed failure is reported as a failure,
even when the subsequent cleanup succeeds; it never establishes budget coverage.

Budget and rate-limit writes guard organizations/teams/departments by the run tag.
User IDs are Cognito UUIDs: the guard requires an exact match to both the sub and
username recorded when this run seeded the identity. Merely being a UUID or carrying
a test-looking prefix is insufficient. Offline fixtures use UUID-shaped subs too.

All budget actors also have canonical ADP `users` rows and active tenant
memberships in the two disposable organizations. The admin budget API resolves
Cognito subs through those records; claims-only identities cannot receive a user
budget. Cleanup removes memberships and users before the tagged organizations.
