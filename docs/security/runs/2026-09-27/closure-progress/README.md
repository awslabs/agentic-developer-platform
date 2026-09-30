# Critical remediation progress — 2026-09-27 15:05 UTC

**Incomplete: 31 unique Critical advisories remain.** This continues
[the prior handoff](../../../handoffs/2026-09-27-critical-continuation.md).
Epic #6492 and rollout story #6521 remain open.

The fresh observed-and-desired inventory reconciles to **31 Critical / 223 High,
2,039 image/package occurrences**, compared with 32 / 228 / 2,188 in the handoff.
The same frozen Grype database and exact binary-bound dispositions were used.
`CVE-2026-75595` has no remaining Critical occurrence. Other repaired occurrences
mostly overlap advisories still present on other components. These are scanner
and applicability counts, not a claim that every match is exploitable.

## Delivered and verified

- ADOT now requests and observes
  `public.ecr.aws/aws-observability/aws-otel-collector@sha256:7968fb60db6a2390a47ba6a2df029745638486e285c9b2487da1b722d0855a3e`.
  Its source pin was already merged. A resource-version-guarded patch retained
  the live configuration, service account and rollout strategy, and disabled
  Java auto-annotation/injection as the source specifies. The deployment has
  one ready replica with zero restarts. No Java init containers remain on active
  pods in the follow-up cluster read.
- The collector returned HTTP 200 for health and OTLP logs, metrics and traces.
  A uniquely identified log arrived in CloudWatch Logs, an EMF metric event
  arrived in the metrics log group, and X-Ray returned the submitted trace.
  The collector log sample contained no authorization/export failures. This
  verifies those canaries, not every production telemetry stream or historical
  loss recovery. Exact AWS receipts and pod/image binding are adjacent JSON.
- A worker canary ran the reviewed
  `adp-agent-runtime@sha256:cb081f259e95108195c22ef7506eeed843fef6d015f7ce33366ad4f69fd8284c`
  with the existing worker template's service account and security settings.
  It verified UID 1001, shared-contract validation, entrypoint import and native
  curl/Git/Node/AWS CLI execution, exiting 0 on the exact observed digest.
  The first canary failed because the operator's Python command had a shell
  quoting error; the corrected second Job passed. Both receipts/logs were saved
  before deleting only these two completed canary Jobs.
- A resource-version and old-image guarded patch changed only the
  `agent-scaledjob` container image. The complete remaining spec compared equal
  after the patch, including gradual rollout. No active worker tasks were
  terminated. **Full task acquisition/execution/completion remains unverified**;
  the canary did not consume a task. The dev Terraform input is updated in this
  change to retain the reviewed image on a later apply.

The worker raw scan has 24 Critical matches; the existing exact binary-bound
curl review yields zero reviewed Critical. Raw findings and dispositions remain
separate. See [worker evidence](../worker-normal-curl/README.md). The ADOT raw
candidate scan has zero Critical. Observed digests match these existing scanned
artifacts; no new artifact was inferred clean from a version label.

## Remaining prerequisites

Target identity resolves to account 000000000101, cluster
`adp-dev-eks-cluster`, region `us-east-1`. `example-profile` still returns ExpiredToken.
The current instance role permits the two namespace operations above, but:

- KEDA and ARC deployment patches are denied. KEDA authentication and
  ScaledObject inventory is also denied, so the required audience/CRD migration
  acceptance cannot yet be completed.
- Superplane deployment patches are denied. The SkyPilot deployment workflow
  grants are confined to its own namespace; its sidecar promotion is not a
  Superplane API deployment.
- S3 PV creation/patching and agent-context canary Job creation are denied.
  The live S3 PV has only `allow-delete` and `allow-overwrite` mount options.
  Before a v2 migration, prepare explicit region/throughput for IMDS hop limit 1,
  verify actual ownership/cache/IRSA behavior with a disposable mount and plan
  rollback of v2-created consumers. Do not update the add-on alone merely
  because the current AWS role permits add-on updates.
- `authority-probe-gateway-20260920` is labelled
  `adp-task=authority-activation-20260920`, but has no recorded owner reference.
  The operator wave2 guide explicitly preserves this unknown-owner resource.
  Ownership acceptance was requested before any upgrade/retirement.

No IAM/RBAC grants were expanded. Refresh maintenance credentials and resolve
probe ownership to continue the blocked migrations. The worker's full task
acceptance remains required even after its image promotion.

## Evidence and scope

`summary.json`, `remaining-criticals.json`, `conservative-open-register.json`,
`scan-bindings.json` and `dispositions.json` preserve the current reconciliation.
All active observed references map to scans; desired-image resolution has no
errors or carried findings. Six desired-template rows lack observed digests and
are included using registry resolution. Ten incomplete owner chains remain
explicit. One historical-only digest is reported separately in the inventory.
This is point-in-time EKS image/package scope, not a full security audit.

Private inventory, before/after objects, patch files and canary logs are under
`/workspaces/projects/security27-closure-evidence`. Prior raw evidence and the
frozen database remain in their handoff locations. Public receipts are hashed
in `file-hashes.json`. No advisory/epic is closed based solely on source changes.

Merging the dev tfvars pin can trigger `webhook-ingress-deploy.yml`, including
Terraform work beyond this image-only maintenance. Keep that source promotion
pending until the scoped deployment plan and required migration access can be
reviewed. The two live promotions documented above are already applied.
