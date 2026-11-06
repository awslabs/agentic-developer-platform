# New environment E2E smoke

Use **Actions → ADP New Environment E2E Smoke → Run workflow** after commissioning
an ADP environment: deployment completed, first administrator initialized, and
owned test identities and required connections configured. This is a manually
triggered or reusable commissioning workflow, with no schedule or regular
CI/deployment trigger. It tests the installed platform without deploying it.

The workflow groups existing executable tests in
[`tests/regression/environment-smoke.json`](../../tests/regression/environment-smoke.json).
It does not copy test implementations or select every regression test belonging
to a module. Existing test IDs and scenario tags are preserved where mapped;
the smoke plan also lists native CLI case IDs and exact browser selectors.
Empty catalogue tags explicitly mean mapping is outstanding.

## Smoke scope

Blank `modules` selects the core rows below. Supply IDs separated by commas or
module names separated by semicolons for a focused run. `all` selects all **12
smoke groups**, not all 48 catalogue modules. Unsupported modules fail before
EC2 or browser execution. Authentication, CLI installation/readiness and navigation
always run, even when only one module is requested. Gateway target/revision
checks are mandatory for every run.

| Module ID | Module | Existing smoke tests | Default |
| --- | --- | --- | --- |
| MOD-001 | Agent Models | E38: persona/model catalogue and cost reads | Core |
| MOD-002 | Organizations | E29: administrator organization hierarchy reads | Core |
| MOD-005 | Identity and access | C01/E23: native login and membership reads; real hosted OAuth browser login and restored-session check | Always |
| MOD-007 | Credentials and AWS connections | E24: credential metadata and mutation previews | Core |
| MOD-010 | Budgets and spend | E26: budget period and uncapped read semantics | Core |
| MOD-011 | Rate limits | E36: rate-limit hierarchy reads | Core |
| MOD-023 | Navigation and feature gates | Existing current-page/API journeys, preview entry flag and signed-out redirects | Always |
| MOD-024 | CLI distribution and setup | E01/E20: served CLI installation, capabilities and bounded auth/API doctor checks | Always |
| MOD-045 | Platform deployment verification | Existing gateway revision snapshot before/after the run, plus target identity checks | Core; checks always run |
| MOD-006 | GitHub integration | E28: maintenance status and previews | Optional |
| MOD-008 | GitLab integration | E30: provider discovery and invalid-project refusal | Optional |
| MOD-016 | Chat | E40: readiness/history reads; enabled Chat sends one browser message and checks reply/local persistence; disabled Chat verifies disabled routes/navigation | Optional |

The core CLI selection contains 9 distinct cases, including installation/login.
Browser tests can have several parameter variants. The run records its actual
collected inventory and JUnit results; source-definition counts are not runtime
case counts. Navigation shares routes for dashboard, activity, budgets and
connections even in focused module runs.

A pass establishes these selected journeys only. Organization creation/isolation,
model routing qualification, real AWS connection execution, Git provider delivery,
budget enforcement, load, component digest attestation and full module acceptance
remain outside this pack. Enabled Chat checks response presence and local
persistence; it does not compare generated prose or establish tool correctness.

## Configure a new target

This workflow supports the existing AWS/EKS + Cognito deployment and disposable
EC2/Chromium harnesses. It takes explicit region and AWS resource namespace inputs;
it does not assume resource names use `dev`. Region support still depends on the
installed platform, evaluator infrastructure, network and selected capabilities.
Use the [evaluator setup guide](evaluator-setup.md) and
[target readiness checklist](platform-regression.md#target-record-and-operator-preflight)
first. The pack does not create the deployment, fixture users or missing roles.

Configure two protected GitHub environments, both restricted to reviewed `main`:

| Environment | Configuration |
| --- | --- |
| `<target>` | Evaluator role secret `AWS_CLI_UPLIFT_EVAL_ROLE_ARN` (legacy fallback `AWS_E2E_ROLE_ARN`), `CLI_UPLIFT_EVAL_BINDINGS_JSON`, `CLI_UPLIFT_EVAL_GATEWAY_CATALOG`, and `ENVIRONMENT_SMOKE_TARGET_JSON`. Retain the evaluator's required fixture/recovery variables. |
| `adp-browser-checks-<target>` | Scoped **variable** `ADP_CHECKS_ROLE_ARN`, and an identical `ENVIRONMENT_SMOKE_TARGET_JSON` **secret**. Role must trust this exact protected environment. |

The wider nightly pack's checks/deploy environments are not needed by this
smoke workflow. No deploy role or onboarding/budget mutation suite is used.

Store the following target record privately in both environments. Values below
are fictional and must be replaced; the schema contains no passwords or tokens:

```json
{
  "account_id": "000000000101",
  "region": "eu-west-1",
  "resource_environment": "staging",
  "gateway_url": "https://gateway.example.com/api",
  "dashboard_url": "https://gateway.example.com",
  "cognito_user_pool_id": "eu-west-1_Example001",
  "chat_enabled": false,
  "new_ui_enabled": false
}
```

The dashboard must be the actual SPA origin and the CLI gateway must be that
same origin plus `/api`. Custom split-origin deployments are not supported by
this smoke contract. Account, region, gateway URL and Cognito pool must match
private CLI bindings; the catalog's deployment key, EKS selectors and ECR
repository must identify the intended gateway. The target fingerprint must match
between jobs and protected environments. Feature booleans are assertions about
the installed platform, not commands to change its flags.

For this workflow, CLI bindings must explicitly contain `platform_account`,
`region`, `gateway_url`, `cognito_user_pool_id`,
`gateway_deployment`, `vpc_id`, `private_subnet_id`, `instance_security_group_id`,
`instance_profile`, `credential_secret_name`, `state_bucket`, and the setup-generated
`evaluation_target` ownership tag. Existing CLI
variable overrides still apply and the effective target is checked again before
execution and recovery. No new checked-in `bindings.<environment>.json` file is
needed. These smoke cases do not require a destination account or destination
roles; omit those fields when commissioning core smoke only.

Provide dedicated owned fixture identities with completed initial password
challenges and the memberships/admin permissions needed by the selected reads.
The browser checks role reads
`adp/<resource_environment>/gateway/test-admin-credentials` in the selected region,
containing `username`, `password`, `cognito_user_pool_id`, and `cognito_client_id`.
Its pool must match the target record. The CLI fixture keeps its existing
`admin_username`/`admin_password` contract. These are separate fixture formats.
Ensure hosted OAuth callback URLs, browser network access and model access
(if selecting enabled Chat) are configured. The existing evaluator preflight
checks EC2 network, instance profile, fixture access and recovery prerequisites.

## Run it

Set these variables from private operator configuration, then dispatch from
`main` (after this workflow has been merged):

```bash
gh workflow run adp-environment-smoke.yml --repo "$GITHUB_REPOSITORY" --ref main \
  -f target_environment="$TARGET_ENVIRONMENT" \
  -f environment="$RESOURCE_ENVIRONMENT" \
  -f region="$TARGET_REGION" \
  -f expected_revision="$DEPLOYED_REVISION"
```

`DEPLOYED_REVISION` is the full lowercase 40-character **gateway release** SHA,
not necessarily today's `main`. Add `-f modules=MOD-001,MOD-002` for a focused run
or `-f modules=all` for all smoke groups. Selecting MOD-016 against a target with
`chat_enabled: true` executes a real model-backed message and can incur spend.
Keep that fixture's model permissions and budget bounded.

To preview selection locally without AWS access:

```bash
python3 -m tests.regression.environment_smoke plan --modules MOD-001,MOD-002
```

A commissioning workflow can call the same entry point after deployment,
administrator bootstrap and fixture preparation have succeeded:

```yaml
jobs:
  smoke:
    uses: ./.github/workflows/adp-environment-smoke.yml
    permissions:
      contents: read
      id-token: write
    secrets: inherit
    with:
      target_environment: ${{ inputs.target_environment }}
      environment: ${{ inputs.environment }}
      region: ${{ inputs.region }}
      expected_revision: ${{ inputs.expected_revision }}
```

The caller must declare/provide these inputs and run reviewed `main`. Do not
hold `adp-regression-<target>` or `cli-live-eval-<target>` concurrency groups in
the caller: the smoke coordinator/children acquire those locks themselves.
No existing provisioning workflow is automatically wired by this change.

## Execution and evidence

1. Validate module selection, assume the evaluator OIDC identity, compare the
   explicit target to private bindings and verify the expected gateway revision.
2. Run selected existing CLI cases on disposable EC2. The reusable CLI workflow
   retains its execution limits, durable recovery and independent recovery job.
3. After confirmed CLI cleanup, assume the browser checks role and verify its
   account, target fingerprint and fixture pool. Run existing Playwright tests
   against the explicit SPA URL, checking actual flags against expected flags.
4. Recheck gateway revision and target configuration, then produce one combined
   smoke verdict. Failed CLI execution remains a failure even if subsequent
   browser diagnostics pass. Missing/failed/skipped required jobs and incomplete
   browser inventories cannot pass.
5. Upload plans, CLI JUnit/report, browser JUnit/inventory/screenshots and the
   combined summary through the existing [S3 evidence uploader](../engineering-insights/ci-artifact-storage.md).
   Actions summaries contain S3 receipts. Native Actions console logs remain in
   GitHub; the workflow does not export the entire console log.

The core tests read application data or preview mutations. EC2 resources are
cleaned through the existing harness recovery path. Optional enabled Chat may
leave server-side history in its dedicated fixture account; browser contexts and
local state are discarded, but this pack does not claim server-history deletion.
Keep that account test-owned and apply its normal retention policy.

A failed target/fixture preflight is incomplete commissioning, not a product pass.
Fix the reported prerequisite and rerun. Coordinate with operators to keep the
release stable during the run: before/after snapshots attest gateway revision,
not every component or an uninterrupted deployment lease. A first successful
live run and its S3 evidence are still needed to qualify each new environment.
