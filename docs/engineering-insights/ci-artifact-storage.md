# ADP CI artifact storage

ADP stores ordinary CI reports and diagnostic bundles in the existing private
S3 CI evidence bucket. GitHub Actions continues to execute the jobs, display
checks and retain its native console logs. Files explicitly produced by a job
(JUnit, JSON, captured logs, screenshots and coverage reports) go to S3 through
[upload-ci-evidence-s3](../../.github/actions/upload-ci-evidence-s3/action.yml).
This does not export the entire GitHub console log or copy historical artifacts.

This migration moves 30 upload steps in 20 workflows. Eight authenticated
handoff uploads in three workflows remain on GitHub, as listed below. This is
an incremental migration, not a complete removal of GitHub artifact storage.

## What a run publishes

The uploader uses the ARC runner's existing IRSA identity and
`adp-{environment}-ci-evidence-{runner-account}` bucket (`dev` by default).
Deployment credentials are removed only inside the uploader process, so a job
targeting another account still writes its CI evidence to the runner account.
No new bucket, IAM role or deployment is introduced by this migration.

Each upload writes a compressed archive and a JSON manifest beneath:

```text
artifacts/{7|14|90}/{owner}/{repository}/{run-id}/{attempt}/{job}/{artifact-name}/
  {sha256}.tar.gz
  {sha256}.manifest.json
```

The manifest records the checked-out source revision, workflow revision,
repository, run/attempt/job, file paths, sizes and SHA-256 hashes. Object receipts
include the actual S3 version. Encryption, size, checksum and version are checked
after publication. Conditional writes prevent overwriting an existing key.
The job summary and action outputs provide the manifest URI and version.
Ordinary CI manifests are diagnostic evidence; they do not authenticate an
approved qualification producer or authorize a deployment.

The action accepts files, directories and newline-separated globs within the
checkout or `RUNNER_TEMP`. Directory contents keep paths relative to that
directory; individually selected files keep their basename. Conflicting archive
paths, symlinks, special files, outside paths and inputs over 512 MiB fail.
Use explicit file allowlists for evidence directories adjacent to private state.
CLI evaluation publishes only `report.json` and `results.xml`.

Missing required files fail publication. Optional diagnostics can use
`if-no-files-found: warn` or `ignore`; no matching files produce an explicit
summary instead of an empty artifact. Partial uploads record
`missing_path_count`. These policies never suppress S3 upload or validation errors.

## Migrated workflows

| Area | Workflows | Files retained in S3 |
|---|---|---|
| Regression selection and security | `adp-regression`, `_regression-security` | Selected module plan, scanner reports |
| Offline harnesses | `harness-jobs-ci`, `orchestration-harness-ci` | Test reports and captured diagnostics |
| Gateway and credentials | `gateway-live-tests`, `credential-binding-adversarial-e2e` | Live results, inventories, adversarial reports |
| Browser E2E | `e2e-chat-playwright`, `e2e-new-ui-playwright` | Test results and shared-helper screenshots under `test-results/screenshots/` |
| GitLab | `gitlab-integration-tests` | Fleet/contract JUnit, latency and coverage evidence |
| CLI evaluation | `eval-cli-uplift` | Sanitized report and JUnit; engine handoffs remain below |
| Agent diagnostics | `agent-worker-image`, `agent-context-verb-ops`, `agent-context-deploy` | Native probe diagnostics, evaluation output, image promotion receipts |
| Runtime and webhook | `shared-runtime-maintenance`, `webhook-code-deploy` | Maintenance and deployment diagnostic receipts |
| IRSA acceptance | `security-s14-continue-ecr`, `security-s14-shared-irsa` | Runtime, build-start and final summaries, bound to the separate source checkout |
| Superplane operations | `superplane-operator-source`, `superplane-paid-worker-build`, `superplane-readonly-discovery` | Source/build/discovery metadata receipts |

Gateway CI, agent-control, agent-explanations, webhook-ingress and Superplane
browser CI already use this same uploader. Release workflows already have S3
handoffs through `release_evidence_s3.py`. Gateway deployment receipts use their
separate [authenticated deployment evidence store](../adp-platform-deployment/deployment-evidence-s3.md).

## Retention and retrieval

The existing bucket lifecycle supports **7, 14 and 90 days**. Migrated 3-day
diagnostics use 7 days; migrated 30-day diagnostics use 90 days. Existing 7/14/90
classes remain unchanged. Expiration is asynchronous under S3 lifecycle rules.
This increases retention for some bundles; storage cost and access policies
remain owned by the CI evidence infrastructure.

Open the Actions job summary, find **CI evidence: artifact-name**, and copy its
manifest URI, version and SHA-256. An AWS identity authorized for this private
bucket is required; GitHub repository access alone does not grant downloads.
For example, after setting the values from that summary:

```bash
aws s3api get-object --bucket "$EVIDENCE_BUCKET" --key "$MANIFEST_KEY" \
  --version-id "$MANIFEST_VERSION" manifest.json
sha256sum manifest.json
```

Compare the hash with the summary. Read `artifact.bucket`, `artifact.key` and
`artifact.version_id` from the manifest and fetch that exact archive version with
the same `get-object` command. Compare its SHA-256 with `artifact.sha256` before
inspecting it; each extracted file also has a hash in `files`. Version-specific
retrieval requires `s3:GetObjectVersion`; a reader grant must be provisioned by
the bucket owner if the operator does not already have one. Do not make objects
public to provide downloads. Screenshots and raw diagnostics may contain private
environment or UI information.

## Remaining GitHub handoffs

| Workflow | Uploads retained on GitHub | Reader / required cutover |
|---|---|---|
| `eval-cli-uplift` | Authenticated engine context; `e32-qualification-result` | Gateway deployment and repository-evaluation providers authenticate the run and archive; deploy authenticated S3 readers before switching these publishers |
| `orchestration-live-tests` | Evaluation archive; qualification inventory/results | Evaluation provider validates the archive; `resume`/`cleanup` downloads the originating run's inventory through `actions/download-artifact` |

These four uploads and the one inventory download remain deliberately paired
with their consumers. They still depend on GitHub artifact availability/quota.
A follow-up cutover must deploy authenticated readers, validate current-attempt
provenance and cleanup restoration, preserve historical reads, then switch the
publishers. An unsigned CI manifest cannot replace that trust contract. The
uploader tests enumerate these exceptions and reject new ordinary GitHub uploads.

## Validation and Engineering Insights

The existing `CI evidence storage` PR workflow exercises a harmless real upload,
download/checksum verification and rejection of an unconditional write on ARC.
Local tests exercise multi-file packaging, missing inputs, source-checkout
binding, path and size limits, credential isolation, receipt validation and
workflow transport coverage. Live application tests are still run by their
existing workflows and schedules.

This storage is the immediate ADP CI integration. The generic
[Engineering Insights capability](README.md) still needs project onboarding,
authorized downloads, ingestion and S3 Tables projections. None of those
services are implied by moving these files to the existing bucket.

## Security Scan evidence

The dispatch-only `security-scan` workflow retains its authenticated context,
cleanup archive, typed scan receipt and sanitized reconciliation in the private
`adp-<environment>-deployment-evidence-<account>` bucket. It has no GitHub artifact
upload dependency. Raw scanner findings remain in the existing security-scans
bucket, and child cleanup state stays at its existing private location.

Context uses the existing `s3-oidc-v1` deployment evidence transport. Archives
retain their ZIP layout; a GitHub OIDC signature binds each descriptor to its
repository, workflow revision, run and attempt, as well as the exact archive
version, SHA-256 and source revision. The job summary records the signed object
URI, version and digest. The reader downloads the signed archive version and
checks its digest before inspecting files or evaluating predicates. Storage is
private, versioned, encrypted and subject to the deployment-evidence bucket's
30-day lifecycle. Upload failures still fail the evidence step.

Repository evaluation uses the accepted scan producer's account and region and
its authenticated bound run. Historical workflow definitions without the S3
transport marker still use GitHub artifacts. An unbound read-only evaluation of
a new S3 scan must first acquire an accepted scan target and bound run; it cannot
infer authority from a user-provided bucket URL. Deploy the updated gateway/tick
reader before accepting automated evaluations against this workflow revision.
