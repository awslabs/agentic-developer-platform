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
| 3 | CLI Uplift key scenarios | [E01 install; C01 login/refresh; E20 capabilities/doctor; E21 usage/export; E22 Activity reads/errors; cleanup/recovery](../../tests/e2e/cli_uplift/cases.py) | Disposable EC2 |

The daily EC2 invocation selects **`nightly`**: E01 install, C01 native login and
refresh, E20 capabilities/doctor (#5621), E21 own usage views and bounded export
(#5628), E22 Activity pagination and missing-run errors (#5629), E24 vault metadata and mutation previews (#5631), and E26 own budget daily/weekly/monthly reads (#5589). All product
commands run from the hash-verified served CLI on the disposable EC2 instance.
These three new scenarios add no inference or platform mutations. Missing CLI
helpers, endpoint errors, malformed JSON, or inconsistent exit codes fail the run.

`ec2_scope=login` retains the narrow install/login diagnostic. `ec2_scope=full`
selects E01–E22; blocked/not-run cases keep it red. E02–E19 remain outside the
nightly scope. Passing read regressions does not establish active remote-control
acceptance, marked usage/spend reconciliation, or capability permission contrasts.

## Requirement for every CLI story

Each CLI story must deliver regression scenarios in this existing harness as part
of its implementation PR. Register stable case IDs and story ownership in
`tests/e2e/cli_uplift/cases.py`, wire the existing stage/remote dispatcher, and add
routine bounded scenarios to the `nightly` selection. Update the report schema
and this scenario index together. Do not create another scheduler or dispatcher.

Cover successful commands, JSON/exit-code failures, and relevant authorization,
pagination, retry/idempotency, or recovery behavior. Mutating scenarios must own
fixtures and record resources for durable cleanup; inference scenarios must have
explicit enforced bounds. Missing fixtures must block, never silently pass.
Publish sanitized results tied to the deployed revision. Keep `BG_CONFIG_DIR`,
HOME/XDG, deployment overrides and token stores isolated from operator sessions.

Story completion requires the installed-CLI scenarios to pass against the deployed
revision, plus the story's remaining acceptance criteria. A green offline test or
a merged scenario is not evidence of a successful nightly execution. Active control
fixtures must demonstrate pause/resume, steering uptake, streaming/reconnect and
abort through the existing Task API; read-only E22 does not substitute for them.

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

The budget report checks the current `org` ledger and requires org RPM to enforce.
Phase H reads `root_user` accounting separately from direct billing; it explicitly
reports that a newly triggered agent exhausting a human cap is not tested here.
TPM and inconclusive concurrent-limit coverage remain visible in the findings.

Budget denial cases configure a $1 cap through the API and seed $1.01 of settled
usage in the live test DB before making authenticated requests. This avoids
assuming a fixed request price. Synthetic balances are removed between cases;
case 7 independently verifies real model usage accrual. Spending through a cap
with a newly triggered agent remains outside this key-scenario regression.

E25 (#5639) adds bounded research findings/proposal/source/stat reads to the existing nightly client. Select `--suite research` for this case alone. An existing-domain fixture must declare `research_readback: true` through the existing run config/binding overlay; absent configuration is blocked. This gate is separate from E18 mutation recovery, and actual authenticated CLI reads must prove service readiness. No scan/generate/approve/reject is run by E25.
