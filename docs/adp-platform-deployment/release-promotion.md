# ADP release and promotion

This is the canonical operator guide for creating and promoting an internal ADP
release. It describes the GitHub Actions path; it is not the fresh-account
deployment procedure.

## What ADP calls a release

An ADP release is an immutable, verified set of deployable artifacts built from
one reviewed commit on `main`. It is identified by all three of these values:

- a release ID;
- the full Git source commit SHA;
- the SHA256 of the release manifest.

The manifest records every package hash, container-image digest, Terraform
provider lock and database-migration hash. Promotion always downloads and
verifies that manifest and deploys the same bytes. It never rebuilds for a later
environment and never deploys a mutable image tag such as `latest`.

Creating a release does **not** create a Git branch. The source commit remains
recoverable from Git history by its SHA. A production release should also receive
an annotated, protected Git tag once a production stage exists. Create a release
branch only if ADP must maintain more than one supported version line.

```text
reviewed main commit
        |
        v
build once -> immutable manifest and artifacts -> integration-test acceptance
                                                    |
                                             manual approval
                                                    |
                                                    v
                                          pre-production acceptance
```

The release path wraps `deploy-all.sh --update`. Existing GitHub App settings,
secrets and installation mappings are preserved by the normal upgrade checks.
AWS DevOps Agent is outside this implementation.

| Stage | Account | Profile | Terraform environment |
|---|---|---|---|
| Integration-test | `608380991969` | `adp-integration-test` | `dev` |
| Pre-production | `615296308642` | `adp-pre-production` | `dev` |

Both stages use `us-east-1` even though their Terraform environment name remains
`dev` for compatibility with the installed resource names.

> **Current boundary:** the implemented chain stops at pre-production.
> Production and customer-demo promotion are not implemented. Do not treat a
> successful pre-production run as production approval. Installed agent-context
> or superplane modules also fail release preflight because the release manifest
> does not yet cover their artifacts.

## One-time setup

Follow the [agent deployment guide](deploy-with-agent.md) to verify the target
account. Setup refuses Terraform deletions/replacements and stores its state
separately at `release-pipeline/terraform.tfstate` in each account's state bucket.

```bash
python3 platform/scripts/release/bootstrap.py --configure-github
AWS_PROFILE=adp-integration-test python3 platform/scripts/release/bootstrap.py --environment integration-test --apply
AWS_PROFILE=adp-pre-production python3 platform/scripts/release/bootstrap.py --environment pre-production --apply
python3 platform/scripts/release/bootstrap.py --verify-github
```

Setup creates a private, encrypted, versioned S3 release bucket, GitHub OIDC
roles, and an EKS access entry for the release deployer. GitHub environments
allow only `main`. The deployment role has `AdministratorAccess`, since full
platform Terraform manages IAM, networking, EKS and credentials. The build role
can start existing CodeBuild projects whose service role is also privileged.
Review release workflow/buildspec changes as privileged platform code.

The current GitHub billing plan rejects required-reviewer protection for this
private repository. Approval uses a separate **manual promotion workflow**.
Only the designated approver in `platform/scripts/release/workflow.py` can run
it (initially GitHub user ID `20402445`, `PranavSharma1000`). Changing that list
requires a reviewed code change. This is a workflow approval, not GitHub's native
environment-reviewer feature. Administrators retain their direct AWS authority.

## Workflows and responsibilities

| Workflow | How it starts | Responsibility |
|---|---|---|
| **ADP Release and Promote** | Manual dispatch on `main` | Builds or retrieves one immutable release, publishes it, upgrades integration-test and runs mandatory acceptance. |
| **Approve ADP Pre-production Promotion** | Separate manual dispatch by the designated approver | Validates the successful integration run and promotes its exact release to pre-production. |
| **Upgrade one ADP account from a release** | Reusable workflow called by the two workflows above | Verifies the target and artifacts, assumes the account-specific role, runs the guarded full update and records acceptance evidence. Do not dispatch it directly. |

All jobs use the `arc-runner-org` self-hosted runner pool. The workflows may
assume only the account-specific roles selected by their GitHub environment and
verify the resulting AWS account before making changes.

## Standard release procedure

### 1. Prepare

Before dispatching:

1. Merge the intended changes into `main`; do not release an unmerged branch.
2. Confirm the required pull-request checks passed for the merged source.
3. Confirm no legacy or manual deployment is running against either target.
4. Confirm the one-time setup above is current with
   `python3 platform/scripts/release/bootstrap.py --verify-github`.
5. Confirm the release contract covers every installed module. The workflow
   deliberately refuses an installed agent-context or superplane deployment.

### 2. Build and validate integration

In GitHub, open **Actions → ADP Release and Promote → Run workflow** and select
`main`.

- Leave `release_id` and `manifest_sha256` empty to create a new release.
- Supply both values only when deliberately retrying an already published
  immutable release.

This dispatch is not a dry run. For a new release it:

1. verifies GitHub environment protection and runs the release/upgrade contracts;
2. assumes `adp-release-build` in integration account `608380991969`;
3. builds images, Lambda ZIPs, layers, the frontend and Terraform provider locks;
4. writes and publishes the manifest last, so a partial upload is not a release;
5. invokes the reusable upgrade workflow for integration-test;
6. assumes `adp-release-deploy`, takes the account release lock and runs
   `deploy-all.sh --update` using only the selected artifacts;
7. runs mandatory acceptance and uploads `acceptance-integration-test`.

Record the GitHub run ID, release ID, source SHA and manifest SHA256. Review the
`release-manifest` and `acceptance-integration-test` artifacts. Do not continue
if the run was cancelled, failed, or lacks acceptance evidence with
`"status": "passed"`.

### 3. Approve and validate pre-production

The designated approver opens **Actions → Approve ADP Pre-production Promotion
→ Run workflow** on `main` and supplies the successful integration run ID.
Dispatching this workflow is the approval decision.

Before assuming any pre-production role, the workflow verifies the actor, source
workflow, branch, successful conclusion, release ID, source SHA and manifest
SHA256. It then downloads the exact artifacts that passed integration, upgrades
account `615296308642`, runs the same acceptance checks and uploads
`acceptance-pre-production`.

The approver must verify the pre-production run is successful and retain its run
URL and acceptance evidence. There is currently no next automated production
step.

## Artifact storage and immutability

The canonical release store is in integration account `608380991969`, region
`us-east-1`:

| Content | Location |
|---|---|
| Release manifest | `s3://adp-release-artifacts-608380991969/releases/<release-id>/manifest.json` |
| Packaged files and exported image archives | `s3://adp-release-artifacts-608380991969/objects/sha256/<artifact-sha256>` |
| Built container images | ECR repositories `adp-gateway`, `adp-agent-runtime`, `adp-agent-gateway` and `adp-chat-agent` |

The release bucket is private, encrypted, versioned and protected against object
overwrite and deletion. Objects are written conditionally, and downloads require
an independently recorded manifest hash before every file hash is checked.

During an upgrade, verified Lambda and layer packages are copied by content hash
to `s3://adp-terraform-state-<target-account>/adp-releases/sha256/...`.
Container images are copied into the target account's ECR with
`skopeo --preserve-digests`; workloads reference the digest, not the transport
tag. GitHub stores only the manifest and allowlisted acceptance evidence, not the
canonical deployable artifacts.

An administrator who can change the bucket policy remains trusted. Release
builds do not publish `latest`.

Temporary Terraform override files set code inputs to verified packages while
retaining normal configuration, saved-plan safety gates and reconciliation.
Provider lock files are restored and initialization uses `-lockfile=readonly`.
The same frontend bundle runs in both accounts with public settings loaded from
`runtime-config.js`. Index/configuration use `no-store`; CloudFront invalidation
must complete before acceptance. Prior hashed assets remain available.

## Acceptance and evidence

Acceptance requires the gateway and required Python/TypeScript factory workers
at their release digests, KEDA and image-prepull readiness, exact Lambda and layer
hashes, orchestration Lambda digest, existing admin login, an authenticated
WebSocket connection, public API health, frontend/templates/runtime settings,
unsigned-webhook rejection, required module state and preservation of GitHub
configuration. The underlying upgrade also runs migration and pricing checks.

The WebSocket probe tests authentication/connectivity. It does not submit an
agent task or post a GitHub issue/comment. A business-level agent round trip is
an additional gate to implement before production promotion.

GitHub receives only allowlisted acceptance results and the manifest. Private
Terraform output remains in the local upgrade directory and is uploaded to the
target state bucket at
`adp-release-status/<stage>/private/<attempt>/upgrade.log`. State snapshots and
plans are not uploaded to GitHub. Do not commit the deployment journal, generated
overrides or backend rewrites.

GitHub account concurrency and a DynamoDB lock prevent concurrent release runs.
The lock is `LockID=adp-release-upgrade/<account>` in `adp-terraform-locks`.
Older deployment entry points do not take this whole-release lock: do not run
legacy/manual deployments concurrently. If a runner dies without cleanup,
verify no upgrade remains active before removing that exact lock item.

## Reproducing a reported release and preparing a hotfix

Start with the `source_sha`, `release_id` and `manifest_sha256` recorded in the
target's `adp-release-status/<stage>/current.json` and acceptance evidence. Do
not reproduce from the current tip of `main` unless it has that exact SHA.

Use a separate worktree so normal development remains untouched:

```bash
git fetch origin --tags
git worktree add --detach ../adp-release-repro SOURCE_SHA

AWS_PROFILE=adp-integration-test python3 platform/scripts/release/storage.py download \
  --directory /tmp/adp-selected-release \
  --release-id RELEASE_ID \
  --manifest-sha256 MANIFEST_SHA256
```

This gives both the exact source and the exact deployable bytes. If a fix is
needed, create a short-lived branch from that SHA (or its production tag), prove
the issue and fix there, then merge or cherry-pick the fix back into `main`:

```bash
git switch -c hotfix/ISSUE SOURCE_SHA
```

Publish a **new** release for the fix. Never replace an existing manifest,
retag an existing image digest, or edit an old release in place.

## Retry and recovery

To retry a published release, run **ADP Release and Promote** with its release ID
and recorded manifest SHA256. Packages are reused. Integration must pass again
before a new manual pre-production promotion.

Success updates `adp-release-status/<stage>/current.json` in the target state
bucket only after acceptance. Each attempt records the previous release
coordinates. Recovery uses those artifacts and repeats the gates. The first
transition may replace a legacy layer version; subsequent release layers are
retained and all published release ZIPs remain available for republishing.

There is **no automatic database rollback**. Confirm migration compatibility
before deploying older application code; destructive migrations need a separately
tested recovery procedure. Terraform success alone is not production readiness.

For a guarded integration redeployment, the script refuses modified source
trees:

```bash
AWS_PROFILE=adp-integration-test python3 platform/scripts/release/storage.py download \
  --directory /tmp/adp-selected-release --release-id RELEASE_ID --manifest-sha256 MANIFEST_SHA256
AWS_PROFILE=adp-integration-test python3 platform/scripts/release/upgrade.py \
  --directory /tmp/adp-selected-release --environment integration-test \
  --evidence-directory /tmp/adp-release-evidence
```

Tools: Python 3.12 with boto3, Node 22, Terraform 1.14.6, kubectl, AWS CLI, Git
and skopeo. The checked-in workflow installs these dependencies.
