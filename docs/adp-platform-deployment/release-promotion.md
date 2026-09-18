# Build once and promote ADP

The release path wraps `deploy-all.sh --update`: it builds images, Lambda ZIPs,
layers and the frontend once, then upgrades each account using the same bytes.
Terraform provider locks, source commit and migration hashes are also recorded.
Existing GitHub App settings, secrets and installation mappings are preserved by
the normal upgrade checks. AWS DevOps Agent is outside this implementation.

| Stage | Account | Profile | Terraform environment |
|---|---|---|---|
| Integration-test | `608380991969` | `adp-integration-test` | `dev` |
| Pre-production | `615296308642` | `adp-pre-production` | `dev` |

Both use `us-east-1`. Production and customer-demo are outside this initial chain.
Installed agent-context or superplane modules fail the release preflight because
this manifest does not yet cover their artifacts.

## Setup

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

## Release and promotion

1. Run **ADP Release and Promote** on `main`, leaving inputs empty for a new
   release. It builds, publishes, upgrades integration-test and validates it.
   It does not automatically promote to pre-production.
2. Review the run's `release-manifest` and `acceptance-integration-test`
   artifacts. Record its release ID, source commit and manifest SHA256.
3. The designated approver runs **Approve ADP Pre-production Promotion**,
   supplying that successful integration run ID. Dispatching is the approval.
   The workflow verifies the actor, workflow, branch, successful conclusion and
   exact integration evidence before assuming the pre-production deployment role.

Artifacts use content-addressed S3 keys. Conditional writes and the bucket
policy prevent overwrite/deletion; an administrator who can change that policy
remains trusted. The manifest is published last, so a partial upload cannot
become a release. Downloads verify the independently recorded manifest hash and
every artifact. Images are copied with `skopeo --preserve-digests` and workloads
use `@sha256` references. Release builds do not publish `latest`.

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

For local diagnosis, use a fresh disposable checkout at the manifest's exact
commit. The script refuses modified source trees:

```bash
AWS_PROFILE=adp-integration-test python3 platform/scripts/release/storage.py download \
  --directory /tmp/adp-selected-release --release-id RELEASE_ID --manifest-sha256 MANIFEST_SHA256
AWS_PROFILE=adp-integration-test python3 platform/scripts/release/upgrade.py \
  --directory /tmp/adp-selected-release --environment integration-test \
  --evidence-directory /tmp/adp-release-evidence
```

Tools: Python 3.12 with boto3, Node 22, Terraform 1.14.6, kubectl, AWS CLI, Git
and skopeo. The checked-in workflow installs these dependencies.
