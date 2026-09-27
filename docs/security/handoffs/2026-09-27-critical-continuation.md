# Critical remediation continuation — two-hour outcome

> Superseded for current state by [Critical closure at 18:15 UTC](2026-09-27-critical-closure.md): **0 Critical /153 High**. This document preserves the earlier checkpoint.

Window: **2026-09-27 12:58:45–14:58:45 UTC**. Repository: `aws-e/adp`. Epic: https://github.com/aws-e/adp/issues/6492.

**Incomplete: 32 unique Critical advisories remain.** Tested candidates and merged source are not counted as deployed fixes. This supersedes the current-state claims in [the earlier handoff](2026-09-27-critical-high.md), retained as historical evidence.

## Verified outcome

Inventory collected **14:44:40 UTC**, reconciled **14:45:06 UTC**, using the same frozen Grype database as the baseline.

| Conservative image/package scope | Critical | High | Image/package occurrences |
| --- | ---: | ---: | ---: |
| Starting handoff | 59 | 377 | 2,861 |
| Final, including enabled idle workloads | **32** | **228** | **2,188** |

Of the original 59 Critical advisories, **25 are absent from the entire current register**, 32 remain Critical, and two (`CVE-2026-63073`, `CVE-2026-75803`) remain High in current Fluent Bit OpenSSL packages. Thus 27 have no remaining Critical occurrences, but only 25 are fully closed. An intermediate progress message incorrectly called all 27 closed; this final per-advisory reconciliation corrects it.

Three additional Critical advisories discovered in the enabled idle ingestion image—`CVE-2026-27820`, `CVE-2026-42257`, `CVE-2026-6653`—were fixed and are absent from the final register. They were outside the original 59 and do not inflate baseline closure.

Fresh Dependabot export: **0 Critical, 2 High, 6 Medium, 2 Low**. Both original Critical source alerts were fixed. Source alerts and overlaps are retained separately; do not add these counts blindly to the container totals.

Evidence: [summary](../runs/2026-09-27/continuation-final/summary.json), [all 59 advisories](../runs/2026-09-27/continuation-final/critical-coverage-matrix.md), [detailed matrix](../runs/2026-09-27/continuation-final/critical-coverage-matrix.json), [current register](../runs/2026-09-27/continuation-final/conservative-open-register.json), [scan bindings](../runs/2026-09-27/continuation-final/scan-bindings.json), and [exact dispositions](../runs/2026-09-27/continuation-final/dispositions.json).

All 31 observed digests have scan mappings. Six desired-template rows without observed digests are resolved and included, without resolution errors or omitted carried findings. Ten incomplete owner chains remain explicit. This is point-in-time EKS image/package scope, not a complete source/static/secret/external-compute audit. Baseline reproduction matched 59/377/2,861 exactly.

## Delivered fixes and acceptance

| Component | Delivered result | Acceptance / evidence |
| --- | --- | --- |
| SkyPilot backend and authentication sidecar | Backend `f45198dd5cf1…`, sidecar `e837dfd7a879…`; reviewed 0 Critical each | #6582 merged; deployment **36326591559** succeeded; joint pod 2/2 ready, zero restarts; authenticated/unauthenticated, denied route, health and backend-outage fixtures passed |
| Ingestion | Normal image `a2b0f802670b…`; reviewed 0 Critical /124 High | #6585 merged; normal build **36325502551**, deployment **36325916271** succeeded; migration with fixed candidate exited 0; worker and both CronJobs updated; Ruby, Bundler, Sorbet, LLVM, Chromium, curl/Git TLS fixtures passed; libxml2 tests and 2,166 fuzz inputs passed |
| Legacy Python gateway worker | Image `536cee39ec84…`; reviewed 0 Critical /60 High | #6584 merged; existing Chat namespace role, no IAM/RBAC expansion; maintenance **36325928517** succeeded; nonroot Job observed exact imageID and passed before guarded KEDA template update; 56 focused tests passed |
| DeepWiki | Normal image `ca17d314fae4…`; reviewed 0 Critical /87 High | #6558 merged/deployed; native frontend, SSH and TLS fixtures; exact curl/SSH dispositions |
| Zoekt / BusyBox init | Zoekt `713fa47caf13…` 0 Critical; BusyBox `bdf57e528e45…` 0/0 | #6571 merged/deployed; live digests observed |
| CloudWatch add-on | v6.7.0-eksbuild.1 images have 0 Critical | ACTIVE, 7/7 Fluent Bit and 7/7 CloudWatch Agent ready; logging incident below |
| ARC source dependencies | pgx 5.9.2 / spdystream 0.5.1 | #6569 merged; Critical Dependabot alerts cleared; live controller/listener upgrades remain separate |
| Worker / observability source | Normal worker curl retention and ADOT 0.50 pin | #6576 / #6578 merged; candidates/source are **not live closure** |

Full image references are in inventory and receipts; abbreviated digests above are labels only. Raw scans remain available: source/backport repairs can leave distribution metadata matches, and only archive/config/binary-bound dispositions were applied. Normal ingestion’s SHA-verified libxml2 2.13.9 binary is `b38d226e6b1126549ea9c3e7a903b23c0be88a42cc5eb16f807811197f3fb337`.

## Logging incident caused during this work

The 13:22:27 CloudWatch add-on update omitted the existing `serviceAccountRoleArn`; AWS cleared that association. Ready Fluent Bit pods then lacked the IRSA role and reported `CreateLogStream AccessDeniedException` and chunks that “cannot be retried”. Last old events were observed around 13:35; delivery was absent in the ten-minute check around 14:38. The exact outage boundary and extent of missing logs are not established. **Some logs may be permanently missing.**

Restored `arn:aws:iam::879318057152:role/adp-dev-role-cw-observability` at 14:39 (successful update `a04ba96e-161f-300d-ae25-097fa3f2e2ad`). An annotation alone did not restart existing pods, so a supported add-on pod-annotation rollout ran at 14:41, explicitly retaining the role (successful update `dfdc0c85-15c8-3f78-8953-deef3bb00bdb`). Fresh gateway and Fluent Bit events at approximately 14:41 were ingested seconds later. All 14 Linux agent/logging pods now have the correct role and are ready. All seven Fluent Bit logs had zero authorization-error matches in the final three-minute sample. This verifies resumed delivery, not lossless recovery or every possible log stream.

Receipts: `cloudwatch-final.json`, `cloudwatch-restored-events.json`, `cloudwatch-recovery-receipt.json`, `cloudwatch-recovery-errors.json`, and restoration updates in the evidence directory. Private raw logs remain local. Terraform already preserves the role at `platform/infra/modules/eks/main.tf`; future CLI add-on updates must explicitly preserve it as well.

## Remaining work

Component advisory counts overlap and must not be summed.

| Component | Remaining Critical advisories | Required next action |
| --- | ---: | --- |
| KEDA | Operator/metrics 16 each; webhooks 10 | Tested 2.21 candidates are 0/0. Finish authentication inventory, Helm/CRD migration and cluster acceptance with maintenance access. |
| ARC controller and listeners | 8 per workload | Tested controller 0.14.2 candidate is 0/0. Upgrade CRDs/controller, verify listeners and real runner lifecycle. |
| Superplane API/controller/monitor | 8 /25 /1 | Promote fixed published/locked candidates through authorized namespace maintenance; verify service behavior. SkyPilot sidecar promotion does not fix the separate API deployment. |
| Worker | 8 | Promote tested `adp-agent-runtime@sha256:cb081f259e95108195c22ef7506eeed843fef6d015f7ce33366ad4f69fd8284c` and verify worker lifecycle. Current desired `5c0b7c9f0a8d…` still contains vulnerable distribution curl; its repair review was rejected. |
| ADOT collector / attached Java init | 13 | Deploy source-pinned 0.50 collector and remove unnecessary Java instrumentation through controlled rollout; verify telemetry. |
| S3 CSI | 3 | v1.15 → v2.8 moves host mounts into pods. Accept PV/IRSA/cache/permissions canary, region/throughput options with IMDS hop limit 1, and rollback before migration. |
| `authority-probe-gateway-20260920` | 9 | Establish ownership-aware controlled upgrade or retirement acceptance; do not casually delete the probe. |

Saved maintenance profile `embark1` remained expired (`ExpiredToken`, last recheck around 14:11); default credentials allow read-only Kubernetes and EKS add-on updates, not platform deployment patches. Existing protected context, SkyPilot and Chat workflow roles were used within their namespace grants. Generic `adp-deploy-dev` has no deployment role variable. Prior requests for refreshed maintenance access were unanswered. Do not self-grant IAM/RBAC or duplicate concurrent deployment-identity repairs. Credentials alone do not satisfy KEDA/ARC/S3 migration acceptance.

Account **879318057152**, region **us-east-1**, cluster **adp-dev-eks-cluster**. Existing authorization covers fixes, publication, merges and controlled deployments. Preserve unrelated checkout changes and the `adp-5433-validation-control-plane` Docker container. No subagents were used or authorized.

## Reproduction and local evidence

Preserve `/workspaces/projects/security27-continuation` (private raw evidence), `/workspaces/projects/security27/live-refresh-20260927T144438Z`, and frozen DB `/workspaces/projects/security25/live-audit-20260927/db`. Final reconciliation: `/workspaces/projects/security27-continuation/reconciled-final`. Committed evidence includes reconciliation/generator scripts and a SHA256 manifest; absolute local paths require adjustment on another machine. Full archives/SBOMs/raw scans are local and bound by committed receipts.

Continue from the matrix, perform remaining controlled promotions/migrations, refresh observed and desired inventories, scan every new digest, and reconcile exact dispositions before closing any advisory or Epic #6492.
