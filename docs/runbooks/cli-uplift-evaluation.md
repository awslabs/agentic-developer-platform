# CLI uplift evaluation: first live checkpoint

Story #5199 · PR #5221 · workflow `.github/workflows/eval-cli-uplift.yml`.

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

Only request this after the remaining scenarios and fixtures are ready:

```bash
gh workflow run eval-cli-uplift.yml --repo aws-e/adp --ref main \
  -f environment=dev -f expected_revision=<verified-40-character-sha> \
  -f mode=start -f suites=full
```

Suites: `login`, `install`, `admin`, `personal-aws`, `routing`, `inference`,
`github`, `parity`, `harness`, `full`.

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
repeat cleanup, and failure-injection evidence. Scans remain paused.
