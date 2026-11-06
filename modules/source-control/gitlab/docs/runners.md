# Operate tenant-scoped GitLab runners

The runner manager runs on EKS. Each job uses one temporary EC2 instance in
the same VPC, in a private subnet with no public address. Each registered
tenant group has a separate manager identity, namespace and autoscaling group.
Job hosts have no manager, administrator or AWS instance-profile credentials.
Successful jobs, failed jobs, cancellation and manager loss must all return
owned job capacity to zero.

This runbook is part of the S3 integration work. **The current draft is not a
deployable release:** the private AMI/custom images and executable negative-case
collection still need production and integration. An upstream
version pin or a passing mock test does not satisfy those requirements. The
release validator must refuse activation until every actual artifact is
produced and verified. Complete the repository evaluation before authorizing
the live-readiness gate.

## Target, source and operation ownership

Use the deployment's selected connection and verify both its AWS account and
assumed role before each operation. Keep the source commit, region, connection
label, GitLab group/project IDs, role ARNs, operation UUIDs, private endpoints
and evidence locations in a private operator manifest. Never test with an
existing tenant's projects or runners. Preserve the approved run and flow
budgets; workflow dispatch is not permission to expand them.

```bash
adp-cred assume --service aws --label "$OPERATOR_LABEL" --exec \
  aws sts get-caller-identity
git rev-parse HEAD
git status --short
```

The source must be the reviewed merged revision. Build, apply and destroy
workflows dispatch from `main`, and their observed workflow `head_sha` must
match that revision. If `main` has moved, review the new revision before
dispatching. Keep a private record of each workflow run ID, source SHA and
conclusion. An accepted dispatch is not a completed deployment.
Each manual workflow also requires `source_revision` and compares it with its
actual dispatch SHA before checkout or AWS authentication. If `main` moves
between review and dispatch, the operation fails before cloud access.

Prepare the existing protected workflow environment with the reviewed roles
and private inputs. Do not grant IAM access or create role trust while running
this procedure; hand the exact IAM plan and PassRole pairs to the access owner.

| Setting | Purpose |
| --- | --- |
| `ADP_DEPLOY_ROLE_ARN`, `ADP_DEPLOY_REGION` | Exact apply/destroy identity and region |
| `GITLAB_RUNNERS_STATE_BUCKET` | Reviewed state bucket |
| `GITLAB_RUNNERS_BACKEND_TFVARS` | Private backend configuration, stored as a workflow secret |
| `GITLAB_RUNNERS_TFVARS` | Private runner bindings and release inputs, stored as a workflow secret |
| `ADP_BUILD_ROLE_ARN` | Reviewed build dispatcher identity |
| `GITLAB_RUNNERS_BUILD_PROJECT`, `GITLAB_RUNNERS_BUILD_SOURCE_BUCKET` | Dedicated build project and isolated source location |
| `GITLAB_RUNNERS_AMI_BUILD_ROLE_ARN` | Separate reviewed AMI build operator with only the builder profile's PassRole grant |
| `GITLAB_RUNNERS_AMI_BUILD_CONFIG` | Private reviewed JSON configuration for the exact AMI operation, stored as a workflow secret |
| `GITLAB_RUNNERS_ACCOUNT_ID`, `GITLAB_RUNNERS_GROUP_IDS` | Exact scheduled reconciliation scope |
| `GITLAB_RUNNERS_RECONCILE_ROLE_ARN` | Separate ownership-scoped cleanup identity |

Inspect the actual environment's protection rules. Its name alone does not
establish required human reviewers. Keep the flow's deployment and teardown
gates even when GitHub's environment has only branch restrictions.

## Registration and private CA

The [module infrastructure map](../README.md) identifies each Terraform root
and its shared platform inputs. Create or verify the module-owned
[CA-exporter identity](runner-ca-exporter-infrastructure.md) before configuring
the instance root; its identity does not require a deployment-local Terraform
definition.

Follow [the registration and CA procedure](runner-registration-live.md) for
the separate exporter identity, approved CA root, HTTPS discovery, tenant
ownership verification and registrar configuration. Apply the instance
prerequisites first, and record a zero-change instance plan before introducing
the runners stack. The CA secret and group token values stay out of Terraform
state, command arguments, workflow artifacts and logs.

Use the private mode-0600 registrar config and a fixed operation UUID:

```bash
adp-cred assume --service aws --label "$REGISTRAR_LABEL" --exec \
  timeout 600s python3 modules/source-control/gitlab/scripts/register-runner.py \
  --environment dev --region "$REGION" --config "$RUN_CONFIG" \
  --operation-id "$CREATE_ID" --create
```

Creation must return `staged`: the runner is paused and its credential is in
`AWSPENDING`. Read back the runner's exact group, tags, `group_type`,
`run_untagged=false`, `locked=true` and paused status through the admin API.
Retain the operation ID and non-secret runner ID. Do not use the deprecated
shared registration-token path.

## Prepare the isolated image builder

The platform's optional `enable_gitlab_runner_build` flag is **not** the
approved setup path on an existing platform state. A targeted plan can still
replace unrelated roles and alter the shared CodeBuild boundary. Do not apply
that flag or a targeted platform plan to obtain this project. The standalone
builder state under `modules/source-control/gitlab/runners/builder/infra` owns
only the dedicated project, its build role and inline policy, and three
immutable ECR repositories. It reads but never changes the existing
`adp-dev-codebuild-boundary` policy. It does not create a new runner host,
GitLab project or validation service.

The access coordinator must review an exact-account, exact-region plan with a
private encrypted backend at `dev/modules/gitlab-runner-build/terraform.tfstate`,
separate from platform and runner states. Use the selected AWS connection,
verify both account and role, initialize with a private backend config and
private variable file, and save a plan. Feed `terraform show -json` for that
plan to `modules/source-control/gitlab/runners/builder/infra/check_plan.py --account "$ACCOUNT_ID" --region "$REGION"`
without publishing the JSON: it refuses incomplete plans, existing managed
state, updates/deletes or resources beyond the six owned creations. If any
resource already exists, reconcile ownership instead of importing or
forcing creation. Apply **only** that same saved plan after the coordinator's
IAM/change approval; a passing offline test is not approval or evidence of a
complete live plan. The account-specific source bucket must be
`adp-terraform-state-<account-id>`; the build workflow rejects a different
bucket before dispatch.

Separately, the access coordinator must add the exact
`adp-dev-gitlab-runner-bootstrap` project to the existing protected trusted
builder's reviewed `build_project_names` inventory and apply a reviewed,
scoped automation policy update. The protected build dispatcher needs
only exact project/build ARNs and the corresponding
`codebuild/src/adp-dev-gitlab-runner-bootstrap/` source prefix. Do not copy
other platform build grants or broaden `iam:PassRole`. Keep the build workflow
disabled until both approved plans are complete, the reviewed role and project
are present, and the GitHub environment variables point to their exact
identities. No build, AMI launch or live qualification occurred during the
offline module checks.

For a **plan only**, set `BUILD_SETUP_LABEL`, `ACCOUNT_ID`, `REGION`,
`BACKEND_CONFIG`, `BUILD_VARS` and `BUILDER_PLAN` from approved private
configuration; the last three paths must stay outside the checkout. Inspect
the selected role and the backend's exact bucket, key and encryption settings
before proceeding. Do not post the plan JSON, variables or state to a PR.

```sh
BUILDER=modules/source-control/gitlab/runners/builder/infra
adp-cred assume --service aws --label "$BUILD_SETUP_LABEL" --exec aws sts get-caller-identity
adp-cred assume --service aws --label "$BUILD_SETUP_LABEL" --exec terraform -chdir="$BUILDER" init -input=false -backend-config="$BACKEND_CONFIG" -reconfigure
adp-cred assume --service aws --label "$BUILD_SETUP_LABEL" --exec terraform -chdir="$BUILDER" plan -input=false -var-file="$BUILD_VARS" -out="$BUILDER_PLAN"
adp-cred assume --service aws --label "$BUILD_SETUP_LABEL" --exec terraform -chdir="$BUILDER" show -json "$BUILDER_PLAN" | python3 "$BUILDER/check_plan.py" --account "$ACCOUNT_ID" --region "$REGION"
```

A passing guard covers the saved plan's shape, not the operator's role, state
backend, IAM access approval or resource inventory. Obtain those independently
before the access coordinator applies the *same* saved plan.

## Build and qualify release artifacts

The trusted build must consume reviewed source bytes and pinned upstream
inputs, produce the host AMI and custom manager/bootstrap/helper/probe images,
and retain their provenance. Verify the private AMI's owner, architecture,
bootstrap digest, Docker/EIC versions and readiness before adding its ID to
the release lock. Verify each pushed image digest through its registry.

The host AMI must install the actual enforcement code, Docker and EC2 Instance
Connect. Its enforcement service must start before networking and before job
admission, allow the narrowly required host bootstrap traffic, and deny job
and helper access to both metadata addresses. Test real packet behavior and
idempotent restart; matching mocked command strings is insufficient. The
non-root helper must be able to prepare the qualified build/cache volumes.

The image workflow produces bootstrap, helper and job images; the probe uses
the qualified job image. Image construction does not require an already-produced
AMI. The separate Packer procedure below builds an AMI candidate after image
publication. Its dedicated profile and build authorization are required.
The image invocation
uses the reviewed dedicated project:

```bash
gh workflow run gitlab-runners-build.yml --ref main \
  -f source_revision="$SOURCE_REVISION" \
  -f account_id="$ACCOUNT_ID" -f region="$REGION"
```

The bootstrap image uses a complete wheel hash lock for CPython 3.12 on Linux
x86_64. Its recipe gives the non-root process read access to the loader even
when the build checkout has private file modes. Before publication, the build
runs `check_bootstrap_image.py` with both supported UIDs, no network, no added
capabilities and a read-only root filesystem. This exercises the actual loader,
CA parser and `/opt/fleeting/fleeting-plugin-aws` executable without cloud
credentials. The connector is fetched only by the trusted builder from the
reviewed private source prefix, checked against its pinned SHA-256 and amd64
ELF header, then copied into the image as root-owned mode 0555. The smoke check
verifies those permissions and the actual executable's source revision and
architecture before publication. Manager startup must independently verify
and mount this executable; a build result does not establish manager readiness.

The upstream [v1.3.1 tag](https://gitlab.com/gitlab-org/fleeting/plugins/aws/-/tags/v1.3.1)
points to `bf5cbcc9183e164637cb4dae42e64e49365e8ade`, while its published Linux
amd64 executable reports version `v1.3.0` and revision `bf5cbcc9`. Retain both
the release tag and actual binary metadata; identify the artifact by its
reviewed checksum and commit instead of inferring identity from the version
banner. This metadata discrepancy does not prove executor compatibility.

The separate `job.Dockerfile` prepares the build and cache directories for
UID 10001. Before publishing any image, `build_runtime_images.py` exercises
the bootstrap and connector as both supported UIDs, runs the actual helper
`cache-init` command on a fresh Docker volume, then checks job writes on that
volume and a fresh cache volume. The helper and job checks use their default
identity, no network, no added capabilities and no-new-privileges. A failing
runtime check prevents all image publication.

The optional CodeBuild setup owns exactly three immutable ECR repositories;
its build role can publish only those images. A lifecycle guard refuses
repository deletion. Registry authentication lives in a temporary
directory outside every Docker build context. The builder verifies each
published digest through ECR and writes a private receipt under the reviewed
source revision and region. Each attempt retains its progress and failures in
`gitlab-runners/artifacts/<source-sha>/<region>/attempts/<attempt-id>/runtime-images.json`
in the private build bucket, including each completed image publication. Only
a successful build creates the write-once `runtime-images.json` at the revision
and region prefix. A failure never overwrites an earlier successful receipt.
If receipt upload fails, the build stops; reconcile the retained attempt records
and immutable ECR tags before retrying. An existing tag or receipt also requires
reconciliation before retry. The
receipt explicitly distinguishes image publication from AMI production and
live qualification. Post-snapshot digest and boot verification remain required
live work.

The AMI procedure uses Packer 1.16.2 and Amazon plugin 1.8.3. The private config
selects an exact Amazon Linux 2023 AMI, the reviewed operator role, EKS cluster,
VPC, private subnet, an ingress-free builder security group, a fixed operation
UUID and the reviewed source revision. Its profile is the separate temporary
`adp-dev-gitlab-runner-image-builder`; job instances receive no profile. The
driver checks the account/role, EKS VPC, subnet ownership, public-IP setting,
security group, profile and upstream AMI through AWS before Packer runs. It
also checks that every image digest matches the reviewed immutable ECR tag,
and refuses an existing operation AMI, active builder or provisioning key.

Prepare builder networking in `runners/image/infra/` using its existing backend.
Set the private `builder_network` input to the reviewed VPC ID and exact SSM,
SSMMessages, ECR API and ECR registry interface endpoint IDs; EC2Messages is
optional. Terraform resolves their current interfaces and adds an ingress-free
builder security group with HTTPS egress only to those IPv4 addresses and the
regional S3 prefix list. Omitting the input preserves an identity-only setup.
Use the `builder_security_group_id` output in the private AMI configuration.

Before applying, verify that the endpoint security groups already allow HTTPS
from the builder subnet and that its route table reaches the regional S3
endpoint. These remain shared platform prerequisites. Review the saved plan:
adding networking to an existing builder identity should create only this
module's security group, with no IAM or shared-network changes. Re-plan when
endpoint interfaces change. Do not reuse a shared image-build group whose
SSM-only egress prevents ECR authentication and pulls.

```bash
python3 modules/source-control/gitlab/runners/scripts/prepare_ami_tools.py "$AMI_TOOLS"
export PATH="$AMI_TOOLS:$PATH"
export PACKER_PLUGIN_PATH="$AMI_TOOLS/plugins"
export ADP_SOURCE_SHA="$SOURCE_REVISION"
python3 modules/source-control/gitlab/runners/scripts/build_job_ami.py \
  --config "$AMI_BUILD_CONFIG" --images "$IMAGE_RECEIPT" \
  --images-sha256 "$IMAGE_RECEIPT_SHA256" --output "$AMI_PREFLIGHT"
```

This invocation only performs preflight and Packer validation. After the saved
configuration and exact builder role/PassRole setup are approved, the same
driver with a fresh output directory and `--execute` performs the build. It
connects through SSM without public IP or inbound SSH, pulls the exact qualified
job/helper digests with temporary authentication in `/run`, restarts Docker and
checks its local image store, then bakes the host guard for enforcement before
networking on subsequent boots. A builder-only shutdown service runs after the
SSM agent stops and removes all SSM state, AWS/Docker credentials, provisioning
and SSH host keys, and cloud-init state. Keep the active SSM unit unmasked during
provisioning so its shutdown ordering remains intact. Cleanup masks it without
reloading systemd only after verifying that the agent has stopped, then clears
the machine ID explicitly for compatibility with AL2023's cloud-init 22.2.
Leave `/etc/machine-id` as an empty regular file: AL2023's early read-only root
needs that mountpoint for systemd to generate a fresh identity. Remove the
separate D-Bus machine-ID copy so it cannot restore the builder's identity.
This avoids a live SSM agent rewriting credentials after cleanup. SSM is masked
in the candidate. Before accepting
the AMI, independently verify the shutdown cleanup marker and the absence of
these identities and caches on a new boot; a source test cannot prove that.
The build retains package versions, source/digest tags and an
encrypted private AMI candidate verified through EC2. It does not edit the
release lock or mark cold-start qualification complete. A failed or ambiguous
build requires inspection of its operation-tagged builder, AMI and snapshots
before retry; an existing operation image is never deregistered automatically.

The protected AMI workflow performs this same operation separately from image
publication so an AMI retry does not rebuild immutable image tags. First review
the successful private `runtime-images.json` and record its SHA-256. Set the
private AMI config to the exact source, target, role and operation UUID. The
workflow compares those bindings before authentication, downloads the receipt
from the reviewed source bucket, verifies its bytes and ECR digests, then uses
the pinned tools and build driver:

```bash
gh workflow run gitlab-runners-ami.yml --ref main \
  -f source_revision="$SOURCE_REVISION" -f account_id="$ACCOUNT_ID" \
  -f region="$REGION" -f operation_id="$AMI_OPERATION_ID" \
  -f images_receipt_sha256="$IMAGE_RECEIPT_SHA256"
```

It shares the image workflow's account lock and retains successful or failed
build evidence privately under `gitlab-runners/ami/<revision>/<region>/<operation>/`
with a separate workflow-attempt directory. Evidence upload failure fails the
workflow. Neither build workflow changes the release lock, activates managers
or waives the live-readiness gate.

Validate GitLab server/Runner major and minor compatibility against the actual
server, then verify the paused manager handshake over the private HTTPS URL.
An operator compatibility review is a prerequisite, not live job evidence.

## Apply and activate

For a new stack, prepare a foundation plan with
`manager_activation_enabled=false` and `runtime_iam_policies_enabled=false`.
Review/import the two identity-only roles through the IAM coordinator first,
then generate an ordinary plan containing only the module's network, cache,
zero-capacity ASGs, inert manager namespaces/service accounts and discovery records.
Bind `deployment_role_arn` to the existing protected GitLab deployment identity
to grant administration only inside those exact manager namespaces. The
foundation owner creates these namespaces because namespace creation is a
cluster-scoped Kubernetes operation; the runtime identity receives no cluster
administration. This stage creates no Kubernetes
manager and cannot consume or activate a staged runner token. Apply only its
reviewed saved plan under the existing deployment approval.

After foundation creation, keep `manager_activation_enabled=false` and set
`runtime_iam_policies_enabled=true`. The ordinary plan now contains the exact
ASG and launch-template identifiers in the manager and reconciler policies.
Have the coordinator apply those policies and reconcile their state. Only then
set `manager_activation_enabled=true`, produce the final ordinary saved plan,
and qualify the promoted registration through the protected manager workflow.
Both flags default to true to preserve existing deployments. Never turn them
off on an active stack as a shortcut: the normal apply guard rejects deletes.
Foundation setup and policy binding do not establish runner or CI readiness.

The apply workflow validates selected identity, discovers read-only platform
references, verifies the separate runner state key, retrieves the already
reviewed private saved plan and checks its entire resource/configuration graph.
Incomplete, errored,
non-executable or deferred plans fail. It applies only that saved plan, under
the account/environment lock shared with destroy.

Before preparing the plan, the authorized deployment owner must verify the
selected account and exact role, private backend/variables, reviewed source,
qualified artifacts and release-lock digest using the operator procedure above.
Use Terraform **1.14.6**, the checked-in provider lock, and the runner module as
the working directory. The release lock must use the portable relative path
`.terraform/runners-release-lock.json`, not an earlier runner's temporary path.
The private file variables below name operator-owned absolute paths; they are
not repository files or live target defaults.

```bash
cd modules/source-control/gitlab/runners
umask 077
terraform init -input=false -backend-config="$PRIVATE_BACKEND_FILE" -reconfigure
install -m 0600 "$PRIVATE_RELEASE_LOCK_FILE" .terraform/runners-release-lock.json
terraform plan -input=false -lock-timeout=5m \
  -var-file="$PRIVATE_RUNNER_VARIABLES" \
  -var='release_lock_file=.terraform/runners-release-lock.json' \
  -var="release_lock_sha256=$APPROVED_LOCK_SHA256" -out="$PRIVATE_PLAN_FILE"
terraform show -json "$PRIVATE_PLAN_FILE" > "$PRIVATE_PLAN_JSON"
python3 -c 'import hashlib,json,sys; print(hashlib.sha256(json.dumps(json.load(open(sys.argv[1])), sort_keys=True, separators=(",", ":")).encode()).hexdigest())' "$PRIVATE_PLAN_JSON"
```

Review that exact plan JSON and retain its full canonical digest as
`APPROVED_PLAN_SHA256`; timestamps and paths remain part of the approval.
Through the authorized private artifact path, store the **same binary plan**
in `GITLAB_RUNNERS_STATE_BUCKET` at
`dev/gitlab-runners/plans/<approved_plan_sha256>/tfplan`. The deployment role
needs read access to that object in the verified account. Do not upload the
plan or its JSON as public Actions artifacts: both can contain secrets.
The workflow downloads that binary, verifies its canonical digest and existing
scope guards, restores the checksum-bound lock from the protected secret at
the exact portable path, and applies the downloaded plan without regenerating
it. A stale state requires a newly reviewed plan; it is not silently replanned.
Runner variable values come from that saved plan; apply does not reload a
separate variables file. Keep the private backend consistent with the plan.

```bash
gh workflow run gitlab-runners-apply.yml --ref main \
  -f source_revision="$SOURCE_REVISION" \
  -f account_id="$ACCOUNT_ID" -f region="$REGION" -f environment=dev \
  -f approved_plan_sha256="$APPROVED_PLAN_SHA256" \
  -f adp_correlation="$ADP_CORRELATION" \
  -f adp_source_revision="$SOURCE_REVISION" \
  -f adp_definition_revision="$ADP_DEFINITION_REVISION" \
  -f adp_contract_digest="$ADP_CONTRACT_DIGEST"
```

The protected infrastructure environment must also supply
`GITLAB_INFRA_DELIVERY_CONTRACT_DIGEST`, matching its accepted contract input.
The registration workflow checks out that approved source from main history,
while retaining the actual dispatched workflow revision separately. It names
the run using the accepted ADP correlation and publishes signed deployment
context before applying infrastructure. Both context and delivery evidence
use the workflow-level S3 transport declarations consumed by the controller.
Source, workflow revision and contract digest mismatches fail before deployment
authentication. Both runner workflows export the validated environment for the
shared signed publisher; selecting a GitHub environment alone is not sufficient.

For initial activation, this dispatch belongs **inside** the lifecycle's
manager switch after credential promotion. `RunnerLifecycle.qualify` promotes
the staged version while the new runner remains paused, calls the manager
integration, verifies the manager uses the new runner ID, then unpauses. The
integration must restart the actual manager on a changed token version and
check a fresh, non-secret runner/system-ID receipt against GitLab. Do not
require token-dependent Helm readiness before the first promotion or use a
manual unpause to work around that dependency.

Use `--qualify` with the original create/rotation UUID and a separate private,
mode-0600 manager configuration. The manager configuration contains
`account_id`, `expected_registrar_role_arn`, `region`, `source_revision`,
`group_id`, `operation_id` and `evidence_path`. The evidence path must be in an
operator-owned mode-0700 directory. Bind the exact reviewed source and selected
registrar identity; do not put credentials in this configuration.

```bash
adp-cred assume --service aws --label "$REGISTRAR_LABEL" --exec \
  python3 modules/source-control/gitlab/scripts/register-runner.py \
  --environment dev --region "$REGION" --config "$RUN_CONFIG" \
  --operation-id "$CREATE_OR_ROTATE_ID" --manager-config "$MANAGER_CONFIG" --qualify
```

The adapter verifies old jobs and attached/detached host capacity have drained
before promotion. It records intent before dispatching the existing protected
apply workflow with the fixed operation and group. A lost dispatch response is
reconciled against the saved workflow identity; retry does not blindly submit
another deployment. Required workflow approval remains in place. A timeout or
failed apply leaves the registration paused with its operation/evidence intact.
Resume using the same configuration and UUID.

The protected apply checks the group's inclusion in the saved plan, the
registrar's `promoted` record and exact `AWSCURRENT` version metadata before
applying. It restarts only that group's manager using a stable operation
annotation, verifies the namespace and service-account IAM role, waits for the
new Deployment generation, and reads a fresh non-secret registration receipt.
The registrar then independently checks its manager heartbeat and GitLab's
actual system ID, pinned Runner version, platform and last contact before
unpausing. A previously qualified operation also needs fresh evidence on retry.
Neither path reads a Kubernetes Secret or prints a runner token.

Configure the deploy role's exact operation-record and secret-metadata reads
and namespace-scoped rollout permissions, plus the registrar's exact manager
receipt/heartbeat reads and regional capacity inventory, through reviewed IAM
and Kubernetes plans. The adapter does not grant these privileges. For initial
setup, the apply's group bindings must include only groups whose credentials
have already been promoted; qualify each newly introduced group in order.

Do not infer success from Helm installation alone. Check EKS Deployment
readiness, the actual GitLab runner/system ID, the exact manager IAM role,
private EC2 placement and the release digests. Preserve the instance stack's
state and resources independently of the runner stack.

## Acceptance and evidence

The checked-in `tests/acceptance/executor.gitlab-ci.yml` defines all 13 executor
attempts using valid GitLab job syntax. Its root-user override, disallowed image
and service requests are distinct jobs; the other jobs execute bounded mount,
namespace, device, raw-socket, Docker-socket and credential-path probes. They
never read host-file contents or print credentials. The debug-trace case requires
an independent full-trace canary scan. A successful job probe remains untrusted
until correlated with host and provider observations.

Use this CI configuration only in a run-owned qualification project at the
reviewed source SHA. Supply the exact qualified job/helper image digests,
`ADP_QUALIFICATION_RUNNER_TAG`, `ADP_QUALIFICATION_RUN_ID` and
`ADP_QUALIFICATION_SUITE=executor` through the owned pipeline. The helper image
is a known published image intentionally outside the job image allowlist, so
image/service refusals cannot pass because an image was simply unavailable.
Expected refusal jobs allow pipeline continuation; pipeline green is never the
acceptance oracle. A refusal must have its precise executor rejection reason
and independently verified absence of a script and build container. Never
invent a UID for a container that was not created. Invalid YAML, TLS failure
or an unrelated failed job cannot satisfy these checks.

`tests/acceptance/dispatch.py` prepares a new private qualification project
inside the explicitly selected, registrar-verified group. It checks the selected
AWS operator, private GitLab discovery, complete release lock and actual AMI
before creating anything. It never adopts an existing unjournaled project or
changes tenant memberships. Its private mode-0600 config contains `run_id`,
`source_revision`, `account_id`, `expected_role_arn`, `region`, `gitlab_url`,
`ca_file`, `tenant_id`, `group_id`, `runner_id`, `runner_tag`, `ami_id` and
`targets_file`. `targets_file` is the private mode-0600 file path containing
the `run_id`/`targets` JSON documented below, not the JSON itself.
The journal belongs in an operator-owned mode-0700 directory. Preserve the
same config, journal and UUID after any failure.

After the existing live gate and trusted host collector are ready:

```bash
python3 modules/source-control/gitlab/tests/acceptance/dispatch.py \
  --config "$DISPATCH_CONFIG" --journal "$DISPATCH_JOURNAL" \
  --aws-profile "$OPERATOR_PROFILE" --prepare
```

Read back the returned project ID and its clone URL using the authenticated
GitLab CLI. Push only the reviewed source commit to the returned
`qualification-<run UUID>` branch using the operator's existing Git credentials.
The project uses the checked-in CI configuration path, so no extra API commit
changes the reviewed SHA. Push events cannot launch this API-only suite. Then:

```bash
python3 modules/source-control/gitlab/tests/acceptance/dispatch.py \
  --config "$DISPATCH_CONFIG" --journal "$DISPATCH_JOURNAL" \
  --aws-profile "$OPERATOR_PROFILE" --dispatch
```

Dispatch independently verifies the branch SHA, actual CI/probe file bytes,
project membership, qualified group runner and absence of variable overrides.
It saves intent before pipeline creation and binds every pipeline variable
to the reviewed release. A lost response is reconciled using GitLab readbacks;
an absent or ambiguous pipeline never triggers an automatic resubmission.
The returned pipeline ID is evidence of dispatch, not acceptance. This adapter
has no teardown or cancellation authority. Start the separately owned group-B
job with the same tag, run UUID and reviewed source; it must remain pending,
not be accepted by another group's runner. Independently poll the **actual**
project/job through the GitLab API while the group-A suite runs. Create B's
**separate run-owned project in the verified B group**, push the same reviewed
source SHA without editing its CI files, and select the checked-in
`executor.gitlab-ci.yml`. Trigger an API pipeline with the same run UUID,
`ADP_QUALIFICATION_SUITE=executor`, the A-only runner tag and reviewed
image/target-file variables. Every B job should stay pending; choose one B
job ID for bounded polling, and inspect the other owned B jobs for unexpected
execution too. A project in A, another SHA or an untagged job is not the B
isolation test. Use the exact B job ID and operator-owned output path, never a
job self-report:

```bash
python3 modules/source-control/gitlab/tests/acceptance/poll_pending.py \
  --fixtures "$FIXTURES" --job-id "$GROUP_B_JOB_ID" \
  --aws-profile "$OPERATOR_PROFILE" --region "$REGION" \
  --expected-role "$OPERATOR_ROLE_ARN" --gitlab-url "$PRIVATE_RUNNER_URL" \
  --admin-secret-arn "$ADMIN_SECRET_ARN" --ca-file "$ROOT_FILE" \
  --output "$PRIVATE_PENDING_HISTORY"
```

The poller writes every observation to a private mode-0600 JSONL file before
checking it, at most 30 seconds apart for at least 300 seconds (330-second
outer limit). Retain that file on failure and **cancel B** through its owned
GitLab job API after polling, including when the poll fails; cancel all the
other run-owned B jobs as well and read back the selected job's final
`canceled` status. Confirm project group, reviewed commit, pipeline/run
ID, exact tag, pending status and absent runner from provider responses.
Copy only validated `observed_at`, `status` and `runner_id` fields into the
private evidence's `gitlab.b.pending_observations`, alongside the independently
read final status. Never rerun an ambiguous dispatched A pipeline merely to
obtain a successful log.

`tests/acceptance/host_observation.py` provides the trusted-host inspection
primitive. Run it as root outside the tenant container with the independently
identified container, job, project, pipeline and source IDs. It reads Docker
and the live kernel, checks the identity again to reject restarts, and emits
only container settings and kernel facts. It excludes environment values,
commands, health checks and arbitrary labels. Unsafe actual settings remain
visible in its result; it never declares acceptance.
It also invokes the installed host guard's read-only verification and retains
the guard/policy hashes and real denial/readiness file timestamps. A failed or
missing guard is explicitly unavailable. These records still need comparison
with the pinned source and job-start timestamps before a cold-start claim.

The AMI recipe installs `host_journal.py` as a root service. It subscribes to
Docker events before SSH admits the manager's first container, records live
kernel inspection on each start, and retains start/die/destroy events in a
private host journal. Its sanitized records also go to the EC2 serial console.
It never exports Docker environment values or arbitrary event attributes.
The observer uses no AWS profile, SSM agent, upload credential or new network
listener. Losing its readiness stops new SSH admission. A restart or missed
short-lived container is retained as incomplete history and requires
investigation; it cannot be replaced with invented kernel facts.

Collect repeatedly while each owned instance exists using EC2's read-only
console API. The collector checks the operator account/role and actual host
placement, IAM-profile absence, ownership tags, source and project before
retaining records. Use one private mode-0700 directory and one output file per
instance. The same file reconciles duplicate snapshots and retains their AWS
request IDs; changed scope, conflicting records and truncated histories fail.
Each receipt retains hashes of only the records returned by that poll. The
combined receipts must cover every retained record, including records that
later console responses no longer contain. EC2 console timestamps are checked
at second precision; host nanoseconds are compared with the ISO evidence at
microsecond precision without floating-point conversion. The original host
timestamps must still order firewall installation, readiness and container start.

```bash
python3 modules/source-control/gitlab/tests/acceptance/collect_host.py \
  --fixtures "$FIXTURES" --instance-id "$OWNED_INSTANCE_ID" \
  --aws-profile "$OPERATOR_PROFILE" --region "$REGION" \
  --expected-role "$OPERATOR_ROLE_ARN" --output "$PRIVATE_HOST_HISTORY"
```

The console API can lag or truncate output. Verify actual post-snapshot boot,
collector readiness and timely provider delivery before dispatching the live
suite. Local Docker tests establish event/inspection behavior only. The
separate dispatcher, group-B poller and verifier correlate GitLab readbacks,
but cold-start, daemon-restart and failed-bootstrap host/provider correlation
still needs its trusted collector. The observer must inspect the **job host's**
firewall: its service requires AF_UNIX and AF_NETLINK for local read-only
verification, without AF_INET/AF_INET6; do not set `PrivateNetwork=yes` on the
real observer. Until this is implemented and independently tested, neither
contiguous console history nor a green pipeline is acceptance. Keep the
existing live gate. The final AWS readback also rejects any job instance with
an IAM profile, independently of the job's credential claims.

After the live-readiness gate records authorization and the deployed source,
create the run-owned group-A/group-B fixtures. Keep fixture/evidence files
owned by the operator and mode 0600 inside a mode-0700 directory. Retain the
exact run UUID, reviewed source SHA, identity/role, group/project/runner IDs,
VPC/subnet/SG/AMI/ASG ownership, source-controlled pipeline jobs and network
targets. Validate `tests/acceptance/fixtures.schema.json` before dispatch;
record every attempt (including failures, restarts and retries) in that run's
private audit directory. Never paste fixture values into CI dispatch inputs or
public comments. The target-file JSON passed to dispatch must have exactly
`{"run_id": <run UUID>, "targets": <fixtures.network_targets>}` and be mode
0600. It is bound to the pipeline as a GitLab **file** variable, not a printed
shell variable. The metadata targets are **exactly** `169.254.169.254:80` and
`fd00:ec2::254:80`, both `tcp`; another address in the same IP family is not
a metadata check. Independently verify each other negative target is listening
from a trusted reference host. For the approved IPv4-only mode, set only the
`metadata_ipv6` target's `expected` value to `disabled_by_provider`. Its actual
probe still runs against `fd00:ec2::254:80`, retains the transport outcome,
connection attempt and errno, and never claims provider state from the job.
Do not replace the IPv6 address or record a synthetic timeout as an observed
denial. All other negative targets require a listening reference control.

In this mode, poll `collect_host.py` while each instance is still in its ASG,
including the network job and all lifecycle hosts. Every console snapshot
includes fresh EC2 instance, ASG membership and exact launch-template-version
readbacks with AWS request IDs. The template and instance must explicitly
report IPv6 metadata disabled; the actual interfaces must have no IPv6
addresses, and the instance and template must have no IAM profile. Missing,
stale or mismatched evidence fails. Retain these observations before teardown
removes the ASG/template; the final check independently rereads the instance.

Trusted startup and container records must show the source-pinned guard and
verified policy before admission and after the required Docker/firewall
restart. Keep `metadata_ipv6_result: disabled_by_provider` in each cold-start
and restart record, and an IPv6 control with `source: ec2_and_autoscaling_api`,
`result: disabled_by_provider`, `reachable: false` and the exact metadata
address/port. The independent provider receipts supply this control; a job
report cannot. The verifier reports this as a distinct executed case and
rejects connection refusal, skipped attempts, connected transports or the use
of this exception for any other target. The operator environment must have
`jsonschema` available for this private, failure-sanitized scope check:

```bash
python3 - "$FIXTURES" <<'PYTHON'
import json
import sys
from pathlib import Path
from jsonschema import Draft202012Validator

try:
    schema = json.loads(Path("modules/source-control/gitlab/tests/acceptance/fixtures.schema.json").read_text())
    fixtures = json.loads(Path(sys.argv[1]).read_text())
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(fixtures)
except Exception:
    raise SystemExit("Private runner fixture schema invalid") from None
PYTHON
```

1. Verify group A runs on its own runner within 10 minutes and identically
   tagged group B stays pending for at least five minutes, with observations
   no more than one minute apart, then cancel group B. Read job/group/runner
   IDs from GitLab's API and use the private poller above; reject a late,
   skipped or cross-run observation.
2. Run the network probe from the actual job. Independently confirm each denied
   destination is listening from a trusted reference host, except for the
   explicitly configured provider-disabled IPv6 metadata case above. DNS errors,
   missing endpoints and invalid URLs are not proof of firewall denial. GitLab
   HTTPS and an actual cache upload/download must succeed.
   Bound each connection to three seconds (the verifier allows at most 3.5
   seconds including measurement). Retain `transport_connected` from every
   probe. TLS or HTTP errors after a TCP connection succeeds prove
   reachability and must fail a denied-target
   check. Connection refusal, missing routes and local CA-file errors also
   leave isolation unproven; only a timed-out network attempt is classified
   as blocked, subject to the independent listening-target controls. In the
   provider-disabled IPv6 case, a real timeout or explicit network/address-family
   unavailability is retained without relabeling it as a firewall drop.
3. Execute every case in `executor-negative-cases.json` through valid GitLab CI
   on the pinned executor. Retain provider job IDs and trusted host inspection
   of build/helper/service containers, namespaces, mounts, identities and
   effective capabilities. Invalid YAML and job self-report are insufficient.
   Use distinct `executor-<case-name>` jobs at the reviewed source commit, with
   pipeline variable `ADP_QUALIFICATION_RUN_ID` set to the fixture run UUID.
   Match pinned Runner refusal strings: the image is not present on the allowed
   list, `too many services requested: 1, only 0 allowed`, or `user "0" is not
   an allowed user: [10001:10001]`, respectively. Require provider
   `runner_system_failure`, the selected helper image for the image case,
   no script stage, and trusted-host evidence that no build container existed.
   The verifier reads each job and its pipeline variables independently; wrong
   source/runner/case/run, reused IDs and uncompleted attempts fail verification.
4. Record three fresh EC2 instance IDs and a Docker/enforcement restart,
   retaining the host's boot ID, durable observer stream, AWS console request
   ID, instance ownership, first job instruction and denial/readiness
   timestamps. Denial must precede executor readiness, which must precede the
   first instruction; show a denied first instruction and working positive
   controls on each start and restart. A failed bootstrap must show executor
   not ready and zero accepted jobs from provider as well as host evidence.
   Gaps, stale snapshots or missing AF_NETLINK firewall verification cannot
   be filled with guessed timestamps. Confirm both metadata families remain
   denied on the actual host.
   Use a **fourth fresh instance** for the restart case and a fifth for failed
   bootstrap. Keep `capacity_per_instance=1` and `max_use_count=1`: never send
   a second job to a host that already completed a cold-start job. The shipped
   launch-template activation performs the controlled restart before SSH
   admission on every fresh host. It starts the trusted observer, stops it
   cleanly, restarts the firewall and Docker, then starts the observer again;
   image checks and the ready marker still precede SSH admission. Firewall
   stop does not clear the deny table, and reload replaces it atomically.
   This requires no operator SSH path, additional job credentials, or manually
   edited launch template. For the fourth instance, retain both observer
   streams on the same boot, different Docker **and** firewall systemd
   invocation IDs, and verified denial before and after restart. The first
   stream must contain no admitted container or ready marker; the second
   stream must bind the instance's only job. Collect all five lifecycle host
   histories through the console poller, with complete per-poll receipt coverage.
   Provider-disabled IPv6 qualification additionally requires the separate
   network job's host history, for six histories in total.
5. For success, failure, cancellation and manager loss, observe every owned ASG
   and independent EC2 inventory until all owned instances are gone within
   15 minutes of idle/drain. Keep the manager READY 1/1 using a fresh
   Kubernetes Deployment readback. Persistent ownership tags must find
   detached instances as well as ASG members; reconcile without terminating
   anything outside the owned account, VPC and groups.

   Run the read-only cleanup poller for each `legal`, `failed`, `cancelled`
   and `manager_loss` job immediately after its actual GitLab finish. It
   retains every AWS/Kubernetes snapshot in a new mode-0600 file inside the
   private mode-0700 directory; include only the final complete row for each
   case in `cleanup`. Keep interim rows as private failure evidence. The
   poller never terminates instances or changes capacity.
   For manager loss, start polling while its Deployment is observably unready
   after the job ends; recovery without an independently observed unready
   resource version cannot establish the manager-loss case.

   ```bash
   python3 modules/source-control/gitlab/tests/acceptance/poll_cleanup.py \
     --fixtures "$FIXTURES" --scenario "$CLEANUP_SCENARIO" \
     --job-id "$CLEANUP_JOB_ID" --instance-id "$OWNED_INSTANCE_ID" \
     --aws-profile "$OPERATOR_PROFILE" --region "$REGION" \
     --expected-role "$OPERATOR_ROLE_ARN" --gitlab-url "$PRIVATE_RUNNER_URL" \
     --admin-secret-arn "$ADMIN_SECRET_ARN" --ca-file "$ROOT_FILE" \
     --cluster "$CLUSTER" --kubeconfig "$KUBECONFIG_FILE" \
     --manager-deployment gitlab-runner --output "$PRIVATE_CLEANUP_JOURNAL"
   ```
6. Through the real selected identities, verify manager CA/own-token access
   and denial to unrelated gateway, agent, other-group and exporter identities.
   Require provider AccessDenied, not a network or credential-expiry error.
7. Inspect the **complete** protected apply logs on success and failure,
   Terraform state and all retained artifacts for actual runner-token canaries
   and CA PEM material. Store logs and raw comparisons only in the private
   mode-0700 audit directory; do not echo a match to a terminal, a GitHub
   comment or an artifact. Check GitHub's run conclusion and `head_sha`
   against the reviewed commit and record whether the full logs and private
   artifact inventory were available. A missing log, state scan, partial
   artifact listing or a nonzero hit is incomplete, not a successful scan.
   Publish only sanitized hit counts and the fact of full coverage.

The acceptance verifier checks retained historical observations and makes
fresh independent GitLab, AWS and Kubernetes readbacks. This example uses the
provider-disabled IPv6 mode; omit `HOST_NETWORK` only for packet-denial mode:

```bash
python3 modules/source-control/gitlab/tests/acceptance/verify.py \
  --fixtures "$FIXTURES" --evidence "$EVIDENCE" \
  --host-history "$HOST_COLD_1" --host-history "$HOST_COLD_2" \
  --host-history "$HOST_COLD_3" --host-history "$HOST_FAILED" \
  --host-history "$HOST_RESTART" --host-history "$HOST_NETWORK" \
  --aws-profile "$OPERATOR_PROFILE" --region "$REGION" \
  --expected-role "$OPERATOR_ROLE_ARN" --gitlab-url "$PRIVATE_RUNNER_URL" \
  --admin-secret-arn "$ADMIN_SECRET_ARN" --ca-file "$ROOT_FILE" \
  --release-lock-file "$PRIVATE_RELEASE_LOCK_FILE" \
  --release-lock-sha256 "$APPROVED_RELEASE_LOCK_SHA256" \
  --cluster "$CLUSTER" --kubeconfig "$KUBECONFIG_FILE" \
  --manager-deployment gitlab-runner --stage pre-teardown
```

The private release lock must be the same reviewed, checksum-bound file used
by the protected runner apply; the bundled incomplete lock cannot qualify a
deployment. Before teardown, record the registered runner IDs, their one
group each and exact token secret names in `ec01.registrations`; retain the
instance-runner IDs from the full paginated admin API in
`ec01.instance_runner_ids`. The verifier compares these receipts with fresh
GitLab and Secrets Manager inventories. For manager, gateway and agent CA
checks, record the selected caller role, `GetSecretValue` target, exit code,
AWS request ID and denial code, without retaining the secret value. A failed
probe, absent provider response or skipped identity is not a passing receipt.

The historical evidence must come from trusted collection. Never populate it
from the verifier's synthetic passing test fixture. Assemble the private
`$EVIDENCE` from GitLab API job/runner/pipeline readbacks, bounded network
artifact **and** listening-target controls, EC2/ASG inventories, Kubernetes
Deployment status, selected-role IAM access denials and per-host journals.
Record the first build/container event job and pipeline IDs for each start,
its observer boot/stream IDs and guard timestamps, and the canceled unassigned
GitLab job ID for the failed bootstrap. The verifier requires a heartbeat with
no readiness marker and no container on the failed host, a fresh provider
readback of that canceled job, and termination of that exact EC2 instance.
If the observer never starts, the failure cannot be qualified by this procedure;
retain the attempt privately and investigate rather than inventing a verdict.
Keep each provider response's request identity/time in the private run record.
Set `collection_sources` and `secret_scan` truthfully; do not use the fixture's
expected values in place of independent results. Run the pre-teardown verifier
while its evidence is fresh (within one hour). Missing collectors, mismatched
source/role/ownership, an early acceptance of B, a reachable denied target,
or unsupported IPv6 observations leave qualification incomplete. A
pre-teardown verdict is **not** final acceptance: revocation, teardown and
the post-teardown instance plan remain required. Record each attempt's
start/end, source and verdict privately; post only sanitized AC-01, AC-02 and
AC-03 results, relevant workflow run links and zero-hit counts to the epic
after the delivery owner has verified all three.

## Rotation and recovery

Use `register-runner.py --rotate` with a new fixed operation UUID and the same
verified group config. Preserve that UUID and the mode-0600 registrar config
for retries:

```bash
adp-cred assume --service aws --label "$REGISTRAR_LABEL" --exec \
  python3 modules/source-control/gitlab/scripts/register-runner.py \
  --environment dev --region "$REGION" --config "$RUN_CONFIG" \
  --operation-id "$ROTATE_ID" --rotate
```
 The lifecycle pauses and drains the old runner,
promotes the pending credential, switches and verifies the manager, activates
the replacement, and only then removes the old runner. Keep a paused state on
failed readiness; preserve operation phases and secret version history.

For a lost provider response or an unfinished operation, reuse its UUID with
`--reconcile`. Do not generate another operation ID to retry, edit the SSM
record or copy credentials into Kubernetes Secrets. Reconcile an ambiguous
workflow dispatch against its retained run ID/source before submitting again.

```bash
adp-cred assume --service aws --label "$REGISTRAR_LABEL" --exec \
  python3 modules/source-control/gitlab/scripts/register-runner.py \
  --environment dev --region "$REGION" --config "$RUN_CONFIG" \
  --operation-id "$CREATE_OR_ROTATE_ID" --reconcile
```

The registration CLI's reconcile action does not itself activate a staged
manager; use the completed manager integration.

## Reconciliation, revocation and teardown

The separate reconciliation workflow runs every five minutes on `main` with
the exact configured group IDs and its own scoped identity. It invokes the
owned-instance reconciler. It must not terminate unrelated instances or
assume the general deployment role. A failed cleanup is a failed check, not
permission to run a broader destroy.

With the existing teardown authorization, drain/cancel run-owned jobs, revoke
the exact registered runner using the registrar procedure and verify the same
stored token version changes from accepted to rejected over verified TLS.
Confirm API absence and scheduled secret deletion. Preserve the shared CA
and existing GitLab instance; do not destroy an instance/CA state. Keep
security groups, job-host restrictions and cleanup control until all jobs
have finished and independent inventories are empty.
Review the complete runner destroy plan and dispatch only the owned state:

```bash
gh workflow run gitlab-runners-destroy.yml --ref main \
  -f source_revision="$SOURCE_REVISION" \
  -f account_id="$ACCOUNT_ID" -f environment=dev -f confirm=gitlab-runners
```

Verify no owned ASG, manager or nonterminated EC2 instance remains. Run the
instance stack's saved-input `terraform plan -detailed-exitcode` again and
require exit 0, just as before and during runner deployment. Run the acceptance
verifier again with `--stage final` and the retained token-revocation and
teardown evidence. Close qualification only when every required check passes
at the deployed source revision; retain the private audit evidence afterward.
### Protected final delivery receipt

`gitlab-runners-qualify.yml` is a **post-teardown, protected, manual** gate,
not a shortcut for the live procedure above. The deployment owner first runs
the product and separate-group control pipelines, all executor and network
probes, three fresh starts and a restart, lifecycle cleanup including manager
loss, and the independent fixture-stack teardown. Collect the EC-04 zero-drift
plans and complete provider job logs with `tests/acceptance/ec04.py`; retain
the raw records privately. Do not dispatch this gate until the final evidence,
run-owned resources and token revocation are available for fresh readback.

The owner places a reviewed, SHA-256-pinned `bundle.tar.gz` under the private
state bucket key `dev/gitlab-runners/qualification/<run UUID>/bundle.tar.gz`.
It contains only mode-0600 regular files: `fixtures.json`, `evidence.json`,
`release-lock.json`, `ca.pem`, `kubeconfig`, `settings.json`, and one or more
`host-history/<name>.json`. The fixtures and host histories are the private
operator-collected inputs to the existing final acceptance verifier; never use
directory entries, links, absolute paths or a parent `./` prefix in the archive;
list only the exact files when creating it. Do not use
synthetic test fixtures as operational evidence. `settings.json` contains only
`region`, `expected_role_arn`, `gitlab_url`, `admin_secret_arn`, `cluster`, and
`manager_deployment` (not token contents). The bundle hash and release-lock
hash must match independently reviewed values. Restrict S3 access to the
run-owned key; do not store the bundle in Git or attach it to a workflow run.

In `fixtures.json`, `group_a` and `group_b` identify runner-owning roots;
`project_namespace_a` and `project_namespace_b` separately identify each
project's immediate GitLab group namespace. Both namespace fields are required,
even when a project belongs directly to its runner root. For product subgroups,
collectors and final readback verify the parent chain through the GitLab API.
An unrelated namespace, missing parent or cycle fails qualification. Runner
registration, secrets, tags and infrastructure cleanup remain scoped to the
runner root, not the product subgroup. These bindings do not grant ownership
of the product project or broaden independent fixture-stack teardown.

Dispatch on protected `main` with `account_id`, `region`, `environment=dev`
and the operational binding inputs
`adp_correlation`, `adp_source_revision`, `adp_definition_revision`,
`adp_contract_digest`. The last three bind the accepted source SHA, actual
workflow definition SHA, and approved v2 contract digest. The protected
`adp-gitlab-deploy-dev` environment supplies `ADP_DEPLOY_ROLE_ARN`,
`GITLAB_RUNNERS_STATE_BUCKET`, `GITLAB_RUNNERS_RELEASE_LOCK_SHA256`,
`GITLAB_QUALIFICATION_RUN_ID`, `GITLAB_QUALIFICATION_BUNDLE_SHA256`, and
`GITLAB_RUNNERS_QUALIFICATION_DELIVERY_CONTRACT_JSON`,
`GITLAB_RUNNERS_QUALIFICATION_DELIVERY_CONTRACT_DIGEST`,
`GITLAB_RUNNERS_QUALIFICATION_DELIVERY_COMPONENT`,
`GITLAB_RUNNERS_QUALIFICATION_DELIVERY_RESOURCE_KIND` and
`GITLAB_RUNNERS_QUALIFICATION_DELIVERY_RESOURCE_ID`. The contract must bind
the same resource and all EC-01–EC-04, product pipeline, cross-group control,
three cold starts/restart, job and manager-loss cleanup, fixture independence,
and qualification cleanup checks with `cleanup_required=true`. The workflow
publishes signed context before verification and retains a distinct versioned
readback for each semantic check only after the complete live verifier passes.
Optional `source_revision`, `qualification_run_id`, and `bundle_sha256` inputs
remain available for explicit operator dispatch; native delivery reads the
reviewed run and bundle binding from the protected environment. The OIDC deployment role needs narrowly
scoped private bundle read, live provider and GitLab secret readback, and
evidence-store publication permissions; the runner needs private GitLab/EKS


### Product runner activation through native delivery

After foundation resources and their exact IAM policies are reconciled, the
protected `gitlab-runners-apply.yml` retrieves the reviewed activation plan and
release lock from versioned private evidence objects. Configure
`GITLAB_RUNNER_CONFIG_JSON` with the accepted source/correlation, original
runner backend, immutable artifact versions/hashes, registered product operation,
actual ASG and launch-template identifiers, approved CA version/hash, and the
reviewed product smoke branch/commit. The deployment role uses its existing
exporter delegation for CA readback. Keep tokens out of configuration and logs.

The workflow permits only manager Helm/ConfigMap changes; IAM, foundation,
instance or destructive changes must be reconciled before activation. It uses
the registrar lifecycle inside the current job to promote, roll out, verify and
unpause the manager. It then preserves Developer membership while enabling CI,
verifies the exact product runner assignment, runs the reviewed smoke commit,
checks its artifact and waits for all owned job instances to disappear. Seven
versioned observations feed the accepted delivery contract. A source merge or
an infrastructure-only foundation apply cannot produce that receipt.

Registration applies start from the approved running instance. If Terraform
fails after stopping it, the protected workflow starts that same instance again
and preserves the failure outcome. It does not change user data during recovery.
The original failed plan must be replaced with a fresh reviewed plan before a
retry, even when the intended user-data bytes remain the same.

Configure `registration_diagnostics_repository_id` in the separate deployment
identity root before registration. Complete Terraform stdout/stderr failures
are retained as base64 fields in an encrypted, immutable private S3 object under
`gitlab-registration-diagnostics/repos/<repository>/runs/<run>/attempts/<attempt>/terraform-failure.json`.
These diagnostics may contain secrets; they are never printed in Actions logs
or attached to the public delivery report. The instance mutation policy uses
AWS's case-sensitive authorization context value `UserData`; the EC2 API's
`userData` parameter spelling is different.


### Independent qualification infrastructure

Use the same `runners` root with a retained `qualification_run_id`, newly
created runner-owning child groups below the approved fixture parent, and a
separate backend `dev/gitlab-runners/qualification/<run UUID>/terraform.tfstate`.
Its cache bucket, cache endpoint security group and reconciler role receive a
run suffix. Group-specific managers, roles and ASGs use the new GitLab group
IDs. The normal product input omits this option and keeps its resource names.
Never enable it on the product backend, import product resources into the
fixture backend, or use product group 37 or parent 106 as fixture groups.
Keep `deployment_role_arn=null` so this stack cannot own the existing EKS
access entry. Any namespace access must be reviewed in its existing owner.

Plan the full fixture stack before applying and retain the separate backend,
run UUID, actual GitLab parent/child readbacks and complete resource inventory.
Create the role-only prerequisites through the coordinator, import them, then
regenerate the plan before infrastructure apply. Bind manager/reconciler
policies only after the actual ASGs and launch templates exist. Qualification
registration inventory is scoped to its own two groups; unrelated product
registrations remain outside its ownership. Preserve product stack and GitLab
instance readbacks before, during and after the owned fixture teardown.

For native ADP deployment runs, record `workflow_sources` in the private
qualification scope. Bind each instance/runner workflow to its exact run and
attempt, repository ID, deployed source revision, workflow revision, correlation,
contract digest, and signed context digest/version reference. The EC-04 collector
authenticates that context using the shared GitHub OIDC evidence reader before
reading complete job logs. This handles a moving `main` while preserving the
approved checkout identity; a different source, workflow, signature, or context
version fails the audit. Legacy scopes retain the single-revision check.
