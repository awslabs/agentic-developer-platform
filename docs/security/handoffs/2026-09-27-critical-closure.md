# Critical remediation closure — 2026-09-27

**Verified Critical image/package remainder: 0.** Inventory: **18:15:14 UTC**;
reconciliation: **18:15:31 UTC**, account `879318057152`, cluster
`adp-dev-eks-cluster`, region `us-east-1`. The requested two-hour deadline was
17:56:47 UTC; this result was reached about 19 minutes late.

| Conservative scope, including enabled idle workloads | Critical | High | Open image/package occurrences |
| --- | ---: | ---: | ---: |
| Continuation handoff | 32 | 228 | 2,188 |
| Final | **0** | **153** | **1,239** |

The same frozen Grype database and exact source/binary-bound dispositions were
used. Raw matches remain preserved; repaired curl distribution metadata was
not treated as proof of vulnerability or suppressed by version alone.
Dependabot has **0 Critical, 2 High, 6 Medium, 2 Low** open alerts, counted
separately. All 59 original Critical advisories have no remaining Critical
occurrences. Of these, 57 are absent from the current Critical/High register;
`CVE-2026-63073` and `CVE-2026-75803` remain High. This does not establish
absence of lower-severity occurrences. **Epic #6492 and mixed-severity stories
remain open for High remediation.**

## Deployed results

- **KEDA 2.21.0:** operator, metrics and webhook Ready; production auth inventory
  preserved. A real queue-triggered Job consumed and acknowledged a unique SQS
  message using bounded IRSA. Metrics discovery passed; an overlong ScaledObject
  was rejected by the actual admission webhook. Earlier min/max tests were
  schema rejections and are not counted as webhook acceptance.
- **ARC 0.14.2:** controller, all three listener/scale-set charts and four CRDs
  upgraded together. Controller/listener image `e81b3b3d138d…` has 0 Critical and
  0 High. Completed real GitHub CI jobs are bound to controller lifecycle logs.
  Agent/CIP listeners are Ready; no separate completed job is claimed for those
  idle scale sets. A saved-plan guard now refuses incompatible major/minor
  controller upgrades.
- **Superplane:** controller `aae626d78ec3…`, monitor `d0095f223d42…`, API
  `70edd98cb9ed…` Ready on reviewed images. API health/readiness and missing/invalid
  credential rejection passed. Monitor downstream polls returned zero clusters;
  this is idle-service acceptance, not provisioning a new cluster.
- **S3 CSI v2.8.0-eksbuild.1:** ACTIVE with original driver IRSA preserved.
  Exact EKS driver, registrar and liveness image digests all scan 0 Critical.
  v1 baseline, v2 canary, full downgrade, rollback mount tests, and final v2
  acceptance completed. Writer read/overwrite, reader write refusal and unrelated
  UID refusal passed on real FUSE mounts. Production claim roundtrip passed with
  UID/GID 10001, file mode 0640. Zoekt remounted, copied 16 shards, became Ready,
  and served index/search HTTP 200. Ingestion ScaledJob pause and CronJob suspend
  settings were restored. Source retains region/throughput and ownership options.
- **Gateway probe:** existing `authority-probe-gateway-20260920` upgraded in place
  to `4c2a6f5900c0…`; image-only comparison preserved configuration and role.
  Health/readiness 200; protected routes reject absent/invalid tokens with 401.
- **Worker/ADOT:** prior-turn verified promotions remain documented in
  [closure-progress](../runs/2026-09-27/closure-progress/README.md). Concurrent
  deployments advanced worker/chat/gateway; their actual newer observed and
  desired digests were scanned rather than overwritten. The stale worker tfvars
  pin was removed from this PR. ADOT telemetry checks include CloudWatch logs,
  metrics and X-Ray retrieval; Java auto-instrumentation is disabled.

## Incidents and recovery

An initial controller-only ARC upgrade crossed 0.13→0.14 while scale sets still
had 0.13 labels. ARC deleted their AutoscalingRunnerSets and GitHub scale-set
registrations, interrupting listeners/CI. **CRDs were not deleted.** The cause
was major/minor incompatibility, not the security version suffix. Recovery
upgraded all three charts and CRDs, recreated registrations/listeners and
verified real CI completion. The new guard rejects the original unsafe plan.

The normal Superplane API candidate failed startup, first on a missing JWT key
and then because live schema 016 did not meet its schema-042 requirement. Both
attempts were rolled back. Controller/monitor readiness was temporarily affected.
A package-only API repair preserved the entire `/app` tree, avoiding an unrelated
schema migration. The existing observation secret gained a strong JWT key and
its deployment reference; existing keys were preserved. The normal release lock
remains unchanged. The maintenance Dockerfile and TLS checks are retained in
[api-compat](../runs/2026-09-27/admin-closure/api-compat/README.md).

The earlier CloudWatch log-loss incident remains documented in the preceding
handoff; this closure does not imply lost events were recovered. Zoekt's planned
Recreate remount briefly interrupted search while shards and images initialized.

## Cleanup and limits

All owned KEDA/S3 namespaces, Jobs, PVs, queue, bounded test roles and test-prefix
objects were removed. Production scheduling was restored. The EC2 role's EKS
access exactly matches its original two policy scopes: ClusterAdmin restricted
to `adp-agents` and `adp-gateway`, plus cluster View. A superplane patch permission
check returns **no**; cluster pod read returns **yes**. No runner IAM policy
changes or temporary Terraform override remain.

All 32 active observed image digests and three distinct desired-only references
have scan mappings, with no resolution errors or carried unverified findings.
The inventory also records one historical-only digest, four pending runtime-ID
rows during a concurrent gateway rollout, five desired-template rows without an
observed digest, and 45 incomplete owner chains. Those gaps remain explicit;
pending images and enabled templates are resolved to scanned references.
This is point-in-time EKS image/package closure and current Dependabot scope,
not a complete source/static/secret, node-OS or external-compute audit.

## Evidence and reproduction

[Final summary](../runs/2026-09-27/admin-closure/summary.json),
[advisory matrix](../runs/2026-09-27/admin-closure/critical-coverage-matrix.json),
[remaining High register](../runs/2026-09-27/admin-closure/conservative-open-register.json),
[scan bindings](../runs/2026-09-27/admin-closure/scan-bindings.json),
[exact dispositions](../runs/2026-09-27/admin-closure/dispositions.json),
[coverage gaps](../runs/2026-09-27/admin-closure/coverage-gaps.json),
[access restoration](../runs/2026-09-27/admin-closure/access-restored.json), and
[file hashes](../runs/2026-09-27/admin-closure/file-hashes.json).

Raw archives, SBOMs, scans, Helm/Terraform snapshots and private runtime receipts
remain in `/workspaces/projects/security27-admin-closure`. Reconciliation scripts
are committed alongside the results and reference preserved baseline/continuation
paths; adjust those paths when reproducing elsewhere. Private credentials and
full workload environment values are excluded from public evidence.

Validation: six ARC guard tests, 13 isolated mountpoint probe tests, shell syntax,
Terraform formatting and PR CI passed before evidence publication. The ordinary
mountpoint pytest invocation encountered a missing unrelated `httpx` conftest
dependency; the self-contained probe module passed with `--noconftest`.
