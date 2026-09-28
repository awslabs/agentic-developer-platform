# CLI AWS/Bedrock lifecycle evaluation: implementation reference

This supports [#5282](https://github.com/aws-e/adp/issues/5282). The issue body
owns scope and acceptance; this reference holds technical starting points.
Inventory and dependency checks are dated **2026-09-17**. This document does not
claim that bootstrap roles, the new suite or its live evidence already exist.

## Selected configuration

Account IDs and region are configurable. This evaluation uses platform
**879318057152** and destination **938500344975**, in **us-east-1**. Keep those
values in dev bindings and examples, with assertions against resolved config.
Existing `config.example.json` still contains the old destination 605440105851;
updating it is implementation work in #5282. Retain the cross-account guard.

| Item | Value / meaning |
|---|---|
| Platform / EC2 / Actions account | **embark1 — 879318057152**, region **us-east-1** |
| User AWS connection and Bedrock proxy destination | **ai-super-plane — 938500344975**, region **us-east-1**, explicitly selected by the user on 2026-09-17 |
| Coordinator profile | `embark1`; this is a local operator alias, never an Actions/EC2 authentication mechanism |
| Earlier destination reference | `embark2` / **605440105851** is superseded for this ticket. Do not provision this ticket's destination roles there. Historical evidence remains historical. |
| Gateway | `https://d1g6cal2ts4iis.cloudfront.net/api` |
| VPC / private subnet | `vpc-0d6115bead9301d25` / `subnet-0860c744097c41a03` |
| Default SG | `sg-0dec921af9dbcbb18` |
| Cognito pool | `us-east-1_JEhv9xSGG` |
| Retained EC2 role/profile | `adp-cli-uplift-eval-instance` |
| Retained orchestration role | `arn:aws:iam::879318057152:role/adp-cli-uplift-eval-orchestrator` |
| Private state bucket | `adp-cli-uplift-eval-state-879318057152` |
| Retained Cognito credential secret reference | `adp/dev/gateway/test-admin-credentials` — values must not be published or reset |
| GitHub tooling dependency | **#5279 is OPEN**, branch `fix/cli-eval-github-cli-ssm`, head `e512fe9e08e6882ca7185f2ef580b21a0caef202`, as of ticket creation |
| GitHub PAT reference | SSM **SecureString** `adp-pat-testin` in 879318057152/us-east-1; dev variable `CLI_UPLIFT_EVAL_GITHUB_PAT_PARAMETER` is set |

#5279 installs pinned gh and the `adp-eval-gh` wrapper. Its fresh EC2 smoke passed and all 375 offline tests passed on Linux ARC (run 35157041441). Reuse that change when integrated; do not recreate it or require GitHub credentials to run independent AWS cases. The PAT authenticates as **PranavSharma1000**, not a newly created regression user. Do not use it to claim GitHub OAuth/test-identity coverage.

The most recent full evaluations, 35129550195 and 35130056153, had **6 passed / 9 blocked / full_acceptance=false**. These are baseline evidence, not proof that AWS/Bedrock cases already work. Re-resolve current main and deployed revisions; historical SHAs are not dispatch inputs.

## Proposed bootstrap interface (to implement)

Reuse existing IaC conventions behind a small Python entry point:

```sh
python -m tests.e2e.cli_uplift.bootstrap --config /path/to/eval-config.json --mode inspect
python -m tests.e2e.cli_uplift.bootstrap --config /path/to/eval-config.json --mode plan
python -m tests.e2e.cli_uplift.bootstrap --config /path/to/eval-config.json --mode apply
```

These are proposed commands, not available commands. Reuse the existing config
loader/precedence: base config, selected bindings, then environment overrides.
Inputs include `platform_account`, `destination_account`, `region`, and new
`source_instance_role_arn` / `source_orchestrator_role_arn` references. For this
run the source roles are `adp-cli-uplift-eval-instance` and
`adp-cli-uplift-eval-orchestrator`, both in the configured platform account.
Credentials use existing authorized IAM-role sessions; they are not config data.

`inspect` reports existence and drift, `plan` reports intended resources and
policies without mutation, and `apply` reconciles the approved retained fixtures.
Successful commands exit zero and produce non-secret JSON; failed identity,
permission or reconciliation checks exit nonzero with the missing action/resource.
Outputs include configured accounts/region, retained-resource identifiers,
`provisioner_role_arn` and `destination_role_arn` for existing workflow bindings.
Missing source-side grants are reported with a ready-to-apply artifact; do not
silently mutate the platform using a destination session. Both grants and trust
must be verified before live dispatch. The coordinator owns applying changes
requiring authority the executor lacks.

Changing account IDs must change policies, output ARNs and expected-account
checks through config only. Rerunning apply reuses fixtures. Inspect/plan must
not claim live AssumeRole proof merely from reading policy documents.

## Roles and lifecycle

| Resource | Lifecycle / authority |
|---|---|
| ARC/orchestrator and EC2 role/profile | Existing retained platform fixtures; no direct EC2 Bedrock bypass. |
| `adp-cli-uplift-eval-provisioner` | Retained in destination; trusts the configured EC2 role. Creates CLI-generated evaluation resources. |
| `adp-cli-uplift-eval-destination` | Retained in destination; trusts the configured orchestrator/recovery role. Supplies scoped evidence/cleanup access. |
| AWS connection and Bedrock invocation roles/stacks | New per evaluation ID, created through real CLI on EC2, removed in dependency order. Bedrock role trusts actual product callers. |
| Test application identities/scopes | Isolated run-owned records, removed after references. No IAM users. |
| State bucket, baseline Cognito fixture and PAT parameter | Retained; never deleted by per-run teardown. |

Build policies from the actual CLI-generated resources, role names and trust
principals. Desired destination bootstrap names above are not proof of existence.
Use name/path boundaries plus ownership evidence; mutable tags alone cannot
permit changing unrelated roles. Dedicated Bedrock roles may grant Bedrock-wide
permissions, never general AWS administration or removal of boundaries.

## Bounded preparation checkpoints

- **Destination bootstrap authority:** coordinator verifies an authorized role
  session in the configured destination. Until then, implement and render the
  bootstrap; supply exact policy/artifact for coordinator application.
- **Invocation observation:** executor identifies the configured Bedrock
  invocation/data-event source and supported query. Produce a redacted example
  showing observed account, role/session, request and run/time correlation for
  each client, with required read permissions. If absent, prepare a scoped
  logging/evidence configuration and identify the applying owner before the live
  stage; do not replace it with config values or STS identity. Shared logging
  changes retain their normal review/authorization requirements.
- **Product cleanup:** executor establishes the supported route for removing
  destination records. If missing, implement the minimal product fix through
  normal review and deployment; no direct database mutation. Schema migration is
  not planned; identify one explicitly if a demonstrated cleanup defect needs it.
- **Dependency #5274:** still open on 2026-09-17. Executor integrates or implements
  the per-client usage assertion under that issue, with no duplicate owner.
  #5279 is optional for these AWS-only scenarios; use it if integrated without
  making PAT/GitHub setup a prerequisite for AWS acceptance.

## Known implementation traps

- Reuse `.github/workflows/eval-cli-uplift.yml`, `tests/e2e/cli_uplift/{config,preflight,stages,live,cleanup,statestore,report}.py`, `personal_aws_worker.py`, `remote/{personal_aws,bedrock_routing,personal_inference,common}.py`, and the pinned #5173 harness (`62b03d343181978aeb54ef1b29634204d050637d`).
- `docs/runbooks/cli-uplift-destination-roles.md` describes the **old cross-account fixture request**. Its IAM examples are starting points to review against actual behavior, not a verified bootstrap for the new target. Correct its stale case references and change destination resource/trust bindings to **938500344975** while keeping source principals in **879318057152**; do not blindly replace source-account ARNs.
- **#5274 is an open prerequisite for L05.** Current `_usage_record()` can accept one Claude row for both clients. Fix/integrate that assertion under its existing issue and require separate client/model/request correlations. Missing Codex usage must fail.
- `_cloudtrail_invocation()` currently uses `LookupEvents` and derives `recorded_account` from config; this is not sufficient invocation proof. Determine the actually configured Bedrock invocation/data-event evidence source, use the supported log/query path, and match observed account, role/session, request and run/time window. CloudTrail management-event history is not a substitute for Bedrock data events. Do not enable broad logging changes in a shared account without a scoped, reviewed configuration.
- Existing E06 destination deletion may leave a registry row when the API lacks a supported delete/unlink route. Leaving it behind is not successful cleanup. Reproduce and fix necessary product deletion support through normal reviewed changes; never patch the database directly or weaken the outstanding-resource assertion.
- Cleanup must respect dependencies: remove routing/connection references before their destination disappears; let CloudFormation delete stack-owned roles; delete orphan roles only when ownership is proven. Preserve API cleanup authentication across worker/instance loss. The current in-memory vault-token cache alone does not survive a lost original EC2/process.
- Role chaining is capped at one hour. Preserve expiry handling and use bounded stages/renewal/continuation as appropriate; do not request an impossible chained duration or introduce static keys.

## Execution and evidence references

Reuse `.github/workflows/eval-cli-uplift.yml` and `tests/e2e/cli_uplift/`.
Jobs remain on self-hosted `arc-runner-org`. All ADP CLI, Claude/Codex, product
provisioning and verification execute on disposable EC2, not the coordinator's
machine. Keep `role-skip-session-tagging: true` under the existing runner boundary.
Do not modify production routing, shared defaults, shared GitHub Apps or queues.

Existing bounds in `config.example.json`: `max_instances=1`,
`max_run_minutes=180`, `instance_ttl_minutes=240`, `daily_budget_usd=5`,
`timeout_seconds=240`, `evidence_wait_seconds=180`, `cleanup_wait_seconds=120`.
Use renewable role sessions for long runs; these limits cannot override STS's
one-hour role-chaining limit. Record any required limit change before rerun.

Before live dispatch, resolve the deployed gateway revision from Lambda
`adp-dev-orchestration-tick`'s `Code.ResolvedImageUri` digest to its unique
40-character tag in ECR `adp-gateway`; `/health` does not expose a product SHA.
Record product, evaluation and served CLI revisions separately. Consult
[the evaluation runbook](../runbooks/cli-uplift-evaluation.md) for current
status/resume/cleanup entry points and update it for the new suite.

Existing Linux offline checks in `eval-cli-uplift.yml` (job: **Offline guards
(no AWS, no gateway, no model calls)**):

```sh
ruff check tests/e2e/cli_uplift tests/unit/test_cli_uplift.py
ruff format --check tests/e2e/cli_uplift tests/unit/test_cli_uplift.py
python -m pytest tests/unit/test_cli_uplift.py -q --timeout=60 --disable-socket
```

Use the workflow's pinned dependencies and offline environment. Preserve its
report-schema and no-secrets checks. New bootstrap/lifecycle cases extend those
guards and the existing harness; do not create a second testing framework.

Proposed suite dispatch, after reviewed integration and verified setup:

```sh
gh workflow run eval-cli-uplift.yml --repo aws-e/adp --ref main \
  -f environment=dev \
  -f expected_revision="$EVAL_DEPLOYED_SHA" \
  -f mode=start -f suites=aws-bedrock-lifecycle
```

`aws-bedrock-lifecycle` does not exist yet. The executor must implement it and
preserve `workflow_call`. Implementation PRs use `fix/cli-eval-*`; avoid merging
from `agent/issue-5282` while execution remains, because the worker's merged-PR
idempotency guard can suppress a resumed run. Use `Refs #5282`, not an automatic
closing reference, until its post-merge acceptance is satisfied.

For each attempt record run URL, tested revisions, failed AC/check, cause,
fix/PR/config change, next run and cleanup result. Keep sensitive transcripts
in the existing private evidence store; publish links/identifiers and redacted
summaries. Never publish credentials, ExternalIds or private manifests.
