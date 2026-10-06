# Exact source for a separately authorized operator

An operator invocation can legitimately own an AWS connection while its assigned
GitHub repository differs from private `aws-e/adp`. The run's GitHub token and
mediated source reader remain bound to its assigned repository. Do not request a
different installation token, move the connection between tenants, share a user
or developer token, or mirror the private repository into another GitHub org.

The two maintained workflows separate the authorities:

- **Superplane Operator Source** runs on protected `aws-e/adp:main`, observes
  authenticated main ancestry, and publishes a real Git bundle plus standalone
  consumer and manifest to the selected account's existing private source bucket.
- **Superplane Paid Worker Build** runs the existing `build_paid_worker.py`
  unchanged in `adp-build-dev`. Its job-local GitHub token performs the existing
  authenticated comparison. Its OIDC build identity dispatches the separately
  prepared builder. The operator never receives that token or build identity.

Neither workflow applies Terraform, deploys a runtime, creates IAM grants or
promotes a release lock. The operator continues using its selected connected AWS
role for installer/build-infrastructure preparation and subsequent deployment.

## Prerequisites and scope

The existing bucket is exactly `adp-terraform-state-<account>`, in the selected
region. Publication and retrieval require enabled versioning, all four public
access blocks, BucketOwnerEnforced object ownership and encrypted objects. The
only source prefix is:

```
superplane/releases/operator-source/<environment>/<full-source-sha>/
```

Bundle, consumer and manifest keys additionally contain their exact SHA-256.
Writes use `If-None-Match: *`; retrieval uses exact VersionIds and independently
pinned hashes. A lost PUT response is reconciled only by reading back an exact
matching version; objects are never overwritten or deleted by this tool.

The independent automation owner enables the two default-off capabilities in
`platform/automation-infra/build-dispatch.tf` after reviewing the saved plan:
`enable_superplane_operator_source` and `enable_superplane_paid_release`.
The flags change only the existing build dispatch inline policy; they do not
change its trust, the connected operator role, or any permission boundary.
The exact [automation-owner handoff](../../../../platform/automation-infra/README.md#superplane-source-and-paid-release-capabilities)
identifies the state, target and review criteria. Required scope:

| Principal | Required scope |
| --- | --- |
| Protected `adp-<environment>-trusted-build` publisher | `s3:PutObject`, `s3:GetObject`, `s3:GetObjectVersion` on the exact selected operator-source prefix; no delete or ACL changes |
| Selected connected operator role | `s3:GetObjectVersion` on the reviewed source prefix and `iam:GetRole` on its own exact role |
| Both transport roles | `s3:GetBucketVersioning`, `s3:GetBucketPublicAccessBlock`, `s3:GetBucketOwnershipControls`, `s3:GetBucketLocation` on the one source bucket |
| SSE-KMS users, if the bucket requires it | Corresponding exact-key encrypt/data-key or decrypt permissions, separately reviewed with the key owner |
| Paid-build dispatcher | Existing exact paid project/source/ECR readback permissions and durable paid dispatch-claim prefix permissions from [PAID-WORKER-RELEASE.md](PAID-WORKER-RELEASE.md) |

The source publisher uses the existing protected `adp-build-dev` environment's
`ADP_BUILD_ROLE_ARN`, main-only protections and trusted-build OIDC action. It checks
that the actual assumed role is `adp-<environment>-trusted-build` in the selected
account. The repository's own `contents: read` token stays in that source job.
An AWS read grant alone is not permission to export private GitHub history: the
source owner must authorize the intended operator and destination prefix.

## Publish and pin

After the source changes and required checks merge, dispatch **Superplane
Operator Source** on `main`, supplying the confirmed account, region and
environment. It exports the workflow's exact `GITHUB_SHA`; it cannot select an
unreviewed arbitrary ref. The bundle contains only that reviewed commit and its
reachable history, with a single `reviewed-main` ref. It carries no `.git/config`,
hooks, remote credential configuration, untracked files or unrelated branch refs.

An independently authorized source reviewer obtains the JSON receipt from that
exact successful workflow's summary or metadata-only artifact, verifies the
workflow/source/account, then delivers that receipt to the legitimate operator.
The manifest hash and VersionId from this trusted channel are the consumer's
trust anchor. A manifest fetched from an arbitrary location, or its own embedded
publisher strings, is not an authentication mechanism. The receipt records an
authenticated publication-time main observation, not a new live GitHub check by
the operator. The paid builder still makes its own live GitHub comparison.

## Bootstrap and retrieve

Use a private directory outside the future source checkout. Stay inside the
already verified selected `adp-cred` execution scope. From the independently
received receipt, download its exact `consumer.key` and `consumer.version_id`
using `s3api get-object`, with the receipt's exact bucket, region and
`--expected-bucket-owner <account>`. Before executing it, verify its SHA-256 equals
`consumer.sha256` and its size equals `consumer.bytes` (bounded to 64 KiB). Read
`head-object` for the same VersionId first to check the size. The standalone
consumer requires only Python's standard library, AWS CLI and Git; no source
repository access or token is needed to bootstrap it.

Invoke the verified consumer with the independently pinned values:

```bash
python3 /private/operator_source.py fetch \
  --account <selected-account> --region <selected-region> \
  --environment <selected-environment> --source-sha <reviewed-full-sha> \
  --manifest-sha256 <receipt-sha256> --manifest-version-id <receipt-version-id> \
  --role-arn <independently-resolved-selected-role-arn> \
  --role-id <independently-resolved-selected-role-id> \
  --output /private/new-operator-source
```

The destination must not exist. The consumer checks STS and the immutable IAM
role identity, bucket privacy, exact manifest/source/target/prefix bindings,
versioned object hashes, bundle structure and Git object completeness. It refuses
shallow, filtered, prerequisite-only, truncated, dirty or mismatched source. It
checks out the original commit with its real history, verifies the tree and
clean status, and writes a private receipt beside `source/`. No replacement Git
commit is manufactured. Installer checks therefore continue to run unchanged.

Run the maintained domain `deploy.sh` from the returned `source/` directory,
keeping environment files, locks, plans and receipts outside that checkout.
The bundle supplies previous component commits needed for source-reuse checks.
It does not supply a human approval, database grant, tenant binding or runtime
readiness evidence.

## Build and recovery

After dedicated paid-build infrastructure exists, dispatch **Superplane Paid
Worker Build** on `main` with the selected account/region/environment and reviewed
Python 3.12 digest pin. The exact source SHA is the job's reviewed main revision.
The existing CLI validates clean source, authenticated ancestry, project contract,
immutable repository and durable one-dispatch claim. A successful build remains
`built-awaiting-image-review`; it neither deploys nor promotes a lock.

A failed source transfer leaves its private output for inspection and claims no
verified source. Do not use a partial checkout. Source publication can reconcile
existing identical conditional objects; new source or changed consumer bytes
receive different content-addressed keys. Source-object retention is owned by the
bucket policy; this tool does not promise permanent retention or broaden it.

A failed or interrupted paid build uses the existing durable claim/child-build
reconciliation procedure, including credential-expiry or workflow-timeout cases.
The workflow retains its local receipt as metadata when available; the durable
S3 claim remains authoritative. Rerunning the workflow does not reset that claim.
