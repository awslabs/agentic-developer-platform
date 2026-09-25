# CLI uplift evaluation: first live checkpoint

Story #5199 · PR #5221 · workflow `.github/workflows/eval-cli-uplift.yml`.

The [combined nightly regression](nightly-cli-regression.md) invokes this suite
with `login` after onboarding and budget enforcement; its manual `ec2_scope=full`
option selects the full acceptance matrix. The instructions below are
for standalone diagnosis; the `login` default is not used by the nightly.

Start with **login**, the default: Actions launches disposable EC2, transfers the
checked-in scripts through S3/SSM, installs the served ADP CLI, performs native
Cognito admin login and refresh, collects evidence, and cleans up. Every product
command runs on EC2. Actions manages its lifecycle and reads evidence.

A successful checkpoint exits zero with `status: passed`, `partial: true`, and
`full_acceptance: false`. It does **not** satisfy the complete E01–E15 evaluation.
C01 records basic login separately from E02's full challenge/negative matrix.
The login checkpoint does not run admin setup, Bedrock, GitHub, or model calls.

## Approved targets

| Setting | Value |
| --- | --- |
| Environment | `dev` |
| Platform / EC2 account | `879318057152` |
| Bedrock destination for later routing tests | `605440105851` |
| Region | `us-east-1` |
| Gateway | `https://d1g6cal2ts4iis.cloudfront.net/api` |
| VPC | `vpc-0d6115bead9301d25` |
| Private subnet | `subnet-0860c744097c41a03` |
| Cognito pool | `us-east-1_JEhv9xSGG` |

These are test targets, not proof that a particular IAM profile or credential
fixture exists. Verify the existing resources before configuring their references.
The local operator AWS session was expired during preparation; no live AWS setup
or validation is claimed by this change.

## Configure the first run

Configure the following in the repository's **dev environment**. Reuse approved
test resources. Do not invent ARNs or point at a real user's credentials.

| Setting | Type | Purpose |
| --- | --- | --- |
| `AWS_CLI_UPLIFT_EVAL_ROLE_ARN` (fallback `AWS_E2E_ROLE_ARN`) | GitHub secret | Actions OIDC role in platform account 879318057152 |
| `CLI_UPLIFT_EVAL_INSTANCE_PROFILE` | GitHub variable | Existing EC2 instance-profile name; otherwise the example defaults to `adp-cli-uplift-eval-instance` |
| `CLI_UPLIFT_EVAL_STATE_BUCKET` | GitHub variable | Private platform-account S3 bucket for scripts and durable recovery state |
| `CLI_UPLIFT_EVAL_STATE_KMS_KEY_ID` | GitHub variable, when needed | KMS key used by that bucket/state |
| `CLI_UPLIFT_EVAL_CREDENTIAL_SECRET_NAME` | GitHub variable | Secrets Manager **name**, not credential contents |

The credential secret contains JSON with `admin_username` and `admin_password`
for a dedicated native Cognito platform-admin test identity in the pool above.
For this repeatable login checkpoint, use an identity whose initial password
challenge has already been completed; it must still authenticate through the real
CLI password endpoint on every run. No imported tokens. The full admin suite
separately requires a fresh challenge identity (`admin_new_password`) and
`non_admin_username` / `non_admin_password`. Configured MFA also needs its actual
challenge response; it is not established by the basic checkpoint.

The EC2 profile needs SSM, read access to the evaluation bundle and fixture secret,
and the necessary KMS decrypt permissions. It does not need Bedrock access for
login. The Actions role needs EC2 launch/describe/terminate/tag, PassRole for that
profile, SSM command/describe and AMI-parameter reads, profile/Cognito metadata
reads, state/bundle storage permissions, and Lambda/ECR deployment evidence reads.
The subnet must reach SSM, S3, Secrets Manager, and the gateway. Preserve the
existing regional STS/Secrets Manager FIPS endpoint configuration.

For later destination scenarios, additionally configure:

- `CLI_UPLIFT_EVAL_DESTINATION_ROLE_ARN`: destination-account access/evidence role.
- `CLI_UPLIFT_EVAL_PROVISIONER_ROLE_ARN`: destination-account provisioning role,
  assumable from the test EC2 profile and authorized for the CLI's CloudFormation
  role setup. Both ARNs must name account **605440105851**.

Neither destination role nor any GitHub fixture is required for login.

## Dispatch

Review and merge the workflow change to `main` before manual dispatch. It is
currently PR code; its appearance in the Actions API from PR checks does not
mean the manual live path is ready on the default branch. Live/recovery jobs
continue to require main or a reviewed release tag and the configured environment.
Do not run unreviewed PR code with the live role.

Reverify the deployed revision using authorized platform-account credentials.
The gateway's `/health` does not expose a SHA. Deployment evidence is available
from the pinned orchestration image (the deploy workflow verifies its match):

```bash
EVAL_IMAGE_DIGEST=$(aws lambda get-function \
  --function-name adp-dev-orchestration-tick --region us-east-1 \
  --query Code.ResolvedImageUri --output text | cut -d@ -f2)
aws ecr describe-images --repository-name adp-gateway --region us-east-1 \
  --image-ids imageDigest="$EVAL_IMAGE_DIGEST" \
  --query 'imageDetails[0].imageTags' --output json
```

Use the single 40-character commit tag, not `latest`. Do not revert a deployment
to match an old example. Actions fetches this exact commit before comparing all
served artifact hashes with the expected release.

### First checkpoint

```bash
gh workflow run eval-cli-uplift.yml --repo aws-e/adp --ref main \
  -f environment=dev -f expected_revision=<verified-40-character-sha> \
  -f mode=start -f suites=login
```

Commands exercised on EC2: the published installer, `adp version`,
`adp admin login --credentials-stdin` with a negative and a valid login, and
`adp refresh`. The password enters through stdin from Secrets Manager; no
credential goes in command arguments or published logs.

The workflow validates required configuration before assuming the execution role.
Preflight verifies platform identity, subnet/profile/Cognito metadata, deployment,
and release files before EC2 launch. Login does not assume a destination role.

### Full run

The nightly requests this complete matrix. Missing fixtures are recorded as
blocked and prevent full acceptance; they do not silently narrow the selection.
To run the same suite independently:

```bash
gh workflow run eval-cli-uplift.yml --repo aws-e/adp --ref main \
  -f environment=dev -f expected_revision=<verified-40-character-sha> \
  -f mode=start -f suites=full
```

Suites: `nightly`, `story-reads`, `tenant-isolation`, `login`, `install`, `admin`, `personal-aws`, `routing`, `inference`,
`github`, `parity`, `harness`, `multi-deployment`, `superplane`, `full`.

**E16/E17 model execution is currently disabled**, even with reachable gateways.
The `multi_deployment_model_limits` requirement blocks both cases until hard
Codex output limits (at most 256 tokens per request) and the aggregate 48-request
ceiling are implemented before inference. The remote entry point also refuses
execution; there is no configuration override. A short prompt or a check of
receipts after inference cannot enforce these limits. AC-10/AC-11 remain open.
The zero-model-request session checkpoint below remains available.

`multi-deployment` (E16/E17, #5413) is the one suite whose fixture cannot be
created from this workflow: it needs **three separately reachable ADP
deployments**, each with its own sign-in fixture, supplied as a JSON array in the
`CLI_UPLIFT_EVAL_DEPLOYMENTS` repository variable:

```json
[{"name": "development",  "gateway_url": "https://…/api", "credential_secret_name": "adp/…/dev-fixture"},
 {"name": "integration",  "gateway_url": "https://…/api", "credential_secret_name": "adp/…/int-fixture"},
 {"name": "preprod",      "gateway_url": "https://…/api", "credential_secret_name": "adp/…/preprod-fixture"}]
```

Each entry names a Secrets Manager secret; never a password. Two rules are
enforced before a run starts, because breaking either produces a green result
that proves nothing:

- **Distinct gateway URLs.** `adp deployment add` treats a second name for an
  already-registered URL as an *alias* — one canonical URL, one stable id, one
  session — so three names over fewer URLs would satisfy a count while sharing
  the very session whose independence is under test.
- **A distinct `credential_secret_name` per deployment**, as required by the
  fixture format. Each secret supplies credentials valid for its gateway;
  matching user IDs across different deployments are allowed.

E16 sends a unique `X-Request-ID` from each tool and matches the usage API's
`request_id` field. Each test identity needs access to its own usage logs. A tool
version that does not forward the correlation header fails receipt validation.
E17 requires shell-tool access in the temporary fixture directory: each model
runs a local barrier command, then continues in the same process after the harness
switches the default, refreshes one deployment, and logs out another. The logged-out
Codex session must report an authentication error, and the other two must finish.
Cleanup failure makes the case fail.

With the variable unset, E16/E17 report `blocked` naming `three_deployments`, and
`full_acceptance` stays false. Supplying gateways clears that fixture requirement;
the separate model-limit requirement above still blocks execution.

Before inference, the same EC2 payload can run a session-only checkpoint:

```bash
python3 /home/ec2-user/adp-eval/remote/dispatcher.py \
  multi_deployment_sessions /path/to/multi-deployment-payload.json
```

Run as `ec2-user`, with the installed Claude and Codex binaries on `PATH`.
The payload needs the same instance/account, region, endpoint, CLI path and
three deployment/credential references as E16/E17. It signs in to the real
gateways in one temporary home, launches both tools with `--version`, verifies
three separate proxy identities, switches the default, refreshes one session,
and logs out another. The remaining tokens must still authenticate at their
own gateways. It stops the proxies and deletes its temporary home.

Its report contains `checkpoint_only: true` and `model_requests: 0`. It is not
an E16/E17 acceptance case and cannot establish model routing or spend. Native
platform-admin sessions may legitimately have an empty organization ID; usage
queries must preserve that value and the gateway-reported user ID. Provision
approved routing fixtures separately before attempting the model scenarios.

### The `superplane` suite (E18, #5637)

**E18 is blocked in code until durable mutation recovery is implemented.** An
E18-only run stops during preflight before allocating an EC2 instance. A full run
keeps E18 blocked while other eligible cases proceed. A direct dispatch also
refuses before loading a session or running the CLI; configuration cannot enable
the missing recovery capability.

E18 is intended to drive the served CLI's `adp superplane` commands from the
disposable EC2 instance through the gateway to the Superplane domain service.
It is the live half of #5637: the offline contract suite
(`modules/gateway/tests/cli/test_superplane_contract.py`) proves every emitted
method, path and body matches the gateway's forwarding allowlist and the domain's
own request models. Live acceptance remains incomplete until the guarded journey
can safely run and establish that the deployed service accepts those requests.

The blocker is concrete: `remote/superplane_domain.py` currently receives resource
IDs after CLI output arrives, stores recovery receipts in temporary homes, and
publishes cleanup resources only after the journey returns. The instance can read
its S3 bundle but cannot synchronously publish mutation intent to durable recovery
storage. A lost response or terminated instance can therefore leave a resource
without a recoverable ID. The orchestrator also lacks cleanup sessions bound to
E18's separate ordinary principal. Registering the five Superplane resource kinds
now preserves historical manifest entries, but their live deleters explicitly
refuse and leave them outstanding; they do not guess ownership or delete by name.

Before removing the guards in `preflight.py` and `remote/superplane_domain.py`,
implement and verify all of the following on disposable EC2 and remote CI:

- Persist the CLI's original operation ID, immutable create request, deployment,
  tenant and principal before each mutation; require durable acknowledgement
  before POST. Include the failed-provider compensation path and account handoff.
- Recover lost replies through the same operation identity and preserve receipts
  until resource absence is verified, including after runner and instance loss.
- Supply cleanup with the correct ordinary or administrator identity, and delete
  only proven run-owned resource IDs. Deployment recovery needs its workspace ID
  as well. Confirm provider and vault absence separately and wait for workspace
  teardown to finish.
- Publish resources, removal evidence and unresolved operations incrementally and
  on every failure. Prove interruption, failed cleanup and expired-session cases
  cannot report acceptance or silently clear a recovery obligation.

Its fixture cannot be created from this workflow either. It needs a Superplane
domain service actually deployed behind the gateway, plus a separate onboarded
ordinary-user session, supplied in the config's `superplane` object:

```json
{"base_path": "/superplane/v1", "ordinary_session_secret_name": "adp/…/superplane-ordinary-session", "model_name": "approved/bounded-test-model", "aws_connection_id": "verified-adp-connection-id"}
```

The inherited E02 session is the verified administrator. The additional secret
contains a current token trio plus `client_id`, `user_pool_id`, `region`, and
`expires_at` for a separately onboarded non-admin identity. The intended journey
refreshes through the gateway and requires distinct ordinary/admin principals in
the same tenant.
`model_name` must identify the fixture's approved one-GPU test model.
`aws_connection_id` must name a verified AWS connection owned by the inherited
administrator in the same tenant and matching `destination_account`; it is an
opaque ADP ID, not a secret ARN. The planned account flow registers, reads, retries
and deletes its own binding while preserving the source connection. The planned
workload uses a one-node, one-GPU, $5/day workspace quota and verifies deployment
and workspace removal.

An unauthenticated 401 or 403 proves only that authentication answered; it does
not prove that the domain is deployed. Preflight now reports
`superplane_durable_recovery_unimplemented` without probing the gateway. A
configured domain cannot override this code blocker. E18 has not established live
acceptance and `full_acceptance` remains false.

### Watch it

```bash
gh run list --repo aws-e/adp --workflow eval-cli-uplift.yml --limit 5
gh run watch <run-id> --repo aws-e/adp
gh run download <run-id> --repo aws-e/adp -n cli-uplift-eval-<run-id>-1
```

`report.json` status is the authoritative verdict. Read `full_acceptance` and
`partial` separately. A blocked/not-run case, failed stage, or incomplete cleanup
fails even a partial checkpoint. Result fields are defined in
[report.schema.json](../../tests/e2e/cli_uplift/report.schema.json).

## Resume, status, and cleanup

Use the same evaluation ID, selected suites, and deployment revision. State is
restored from S3 on the new Actions runner:

```bash
gh workflow run eval-cli-uplift.yml --repo aws-e/adp --ref main \
  -f expected_revision=<same-sha> -f suites=login \
  -f mode=resume -f evaluation_id=<evaluation-id>
gh workflow run eval-cli-uplift.yml --repo aws-e/adp --ref main \
  -f expected_revision=<same-sha> -f suites=login \
  -f mode=status -f evaluation_id=<evaluation-id>
gh workflow run eval-cli-uplift.yml --repo aws-e/adp --ref main \
  -f expected_revision=<same-sha> -f suites=login \
  -f mode=cleanup -f evaluation_id=<evaluation-id>
```

Never publish `state.json`, the resource manifest, fixture secrets, or session
files. Only sanitized `report.json` and `results.xml` are Actions artifacts.

Cleanup runs in-process and in an independent recovery job; the instance also
has a self-termination timer. Deletion is scoped to recorded run-owned resources.
Existing test identities, profiles, buckets, and reused roles are preserved.
After cancellation, verify no live instance remains for the exact evaluation ID:

```bash
aws ec2 describe-instances --region us-east-1 \
  --filters "Name=tag:adp:cli-uplift-eval,Values=<evaluation-id>" \
    "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].{Id:InstanceId,State:State.Name}'
```

## Fault injection

Supported injections: `wrong_account`, `missing_usage`, `cleanup_failure`,
`expired_token`, `instance_loss`; `none` disables injection. These apply to the
corresponding scenarios; a login checkpoint does not test inference usage.

## Troubleshooting

- Missing configuration: set the exact reference named by the readiness error.
  Do not substitute placeholder ARNs or put passwords in GitHub variables.
- Installer/release mismatch: confirm the selected deployment and served helper
  bytes; do not replace expected hashes with observed hashes.
- Login failure: check the dedicated identity and native Cognito endpoint. A
  challenge-pending identity needs its challenge completed before the basic
  repeatable checkpoint; E02 covers fresh-password and MFA behavior separately.
- SSM delivery failure: check instance-profile permissions, subnet endpoints,
  boot completion, and access to the evaluation bundle.
- Cleanup failure: use the same evaluation ID to recover; report any exact
  resources still outstanding. Do not declare a passing run until cleanup passes.

The broader implementation remains incomplete: E07 and E09–E12 have no shipped
scenario scripts. E06 destination-row deletion also needs product support;
removing routing rules alone is not complete cleanup. The next developer should
run **login first** and attach its result, then address the exact failures in the
Bedrock path. Do not expand the framework or claim the full epic has passed.

## Acceptance for closing #5199

The first checkpoint is progress only. Closing the full evaluation still requires
two fresh full runs against the same deployed revision, plus interruption/resume,
repeat cleanup, and failure-injection evidence. The shared nightly workflow now
schedules the key install/login checkpoint; full runs remain manually selectable,
and blocked cases still prevent full acceptance.

Tenant story #5622 adds E23 to the default nightly story reads: visible memberships, explicit current selection and unknown selector refusal. E27 (`tenant-isolation`) requires `tenant_isolation.tenant_ids` with two distinct existing memberships for the installed human fixture; it checks concurrent reads through local default changes and Cognito refresh. No membership is granted and no global workspace selection or model inference occurs. E27 is blocked when that fixture is absent; inference, revoked-membership and uncertain-mutation live acceptance remains open.
