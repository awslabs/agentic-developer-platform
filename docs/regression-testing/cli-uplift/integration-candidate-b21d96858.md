# Reviewed CLI integration gateway candidate

Build-only receipt for source `b21d968586747748f3b605247a33fad328d4697a` (reviewed integration PR #6282).
The image was published to account `000000000101`, region `us-east-1`,
repository `adp-gateway`. No deployment, rollout, migration, model invocation,
IAM or infrastructure change was performed while producing this receipt.

- CodeBuild: `adp-dev-gateway-build:fa556a4c-a2f6-49b6-91a5-5f62a1ea2c48` — `SUCCEEDED`.
- ECR digest: `sha256:4f0f630bbf165b291ed3693377a8166e4e1e2852d7ee7078c721e85193660f70`.
- Immutable image tag: `b21d968586747748f3b605247a33fad328d4697a`.
- Git archive and downloaded S3 source ZIP SHA-256:
  `ecbbd8d58d08cb38791cd1f40ef3be34916d381acd046871b2ad5ddba22a61ac`. The two archives are byte-identical.
- S3 source: `s3://adp-terraform-state-000000000101/codebuild/src/adp-dev-gateway-build/b21d968586747748f3b605247a33fad328d4697a-1790390417-3424226.zip`.
- Build log: `/aws/codebuild/adp-dev-gateway-build`, stream `fa556a4c-a2f6-49b6-91a5-5f62a1ea2c48`.

The canonical `platform/scripts/codebuild-run.sh` ran with
`ADP_RELEASE_BUILD=true` and the exact source SHA. Its gateway buildspec invoked
`publish-shared-image.sh adp-gateway`, including pricing, review-result and
evaluation-contract image selfchecks before publishing the immutable image.
The completed build and ECR metadata were read back independently.

The candidate contains gateway knowledge completion handling, coding Task
steering refusal, and served CLI workspace/provider-connection recovery support.
The source integration also contains Superplane API provider-operation changes
and evaluation harness changes. The Superplane API is a separate deployment unit;
this gateway image does not publish it. Worker/runtime, IAM and infrastructure
sources are unchanged by the integration. The future worker probe correction
in PR #6283 is not included in this candidate.

This receipt registers an available reviewed artifact for later deployment
verification; it does not establish that this image is serving traffic or that
its changes passed live acceptance.
