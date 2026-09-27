# KEDA 2.21 security candidate — #6514 / #6492

Upgrade the Terraform-owned Helm release from 2.16.0 to 2.21.0, including
matching CRDs, RBAC, operator, metrics server and admission webhooks. Each
image is pinned to the scanned immutable digest. The operator service-account
settings now use `serviceAccount.operator.*`; the former paths are ignored by
the current chart and would lose its SQS IRSA annotation.

## Evidence

The frozen Grype database built 2026-09-26T06:29:14Z reports:

| Image | Live baseline raw Critical / High | Candidate raw Critical / High |
|---|---:|---:|
| Operator | 16 / 58 | 0 / 0 |
| Metrics server | 16 / 58 | 0 / 0 |
| Admission webhooks | 10 / 39 | 0 / 0 |

These are image-level scanner matches, not unique CVEs or live deductions.
The three candidates each retain one Unknown match. No suppression or
only-fixed filter was used. `keda-candidate-evidence.json` records config,
archive, SBOM and scan hashes and the chart archive hash. Raw evidence remains
under `/workspaces/projects/security27/*-candidate-scan`.

`tests/verify_keda_chart.py` renders the actual Terraform Helm settings against
the downloaded 2.21.0 chart for Kubernetes 1.35 (the target EKS version).
It verifies three digest-pinned deployments, all six CRDs, admission and metrics
API resources, operator IRSA, resource bounds, disruption annotation placement,
and the enforced token-audience mode. All four read-accessible live ScaledJobs
validate against the new schema and use `aws-sqs-queue`.

Reproduce from repository root (Python dependencies: python-hcl2 8.1.4,
PyYAML, jsonschema; Helm 3.22.0):

```sh
helm pull keda --repo https://kedacore.github.io/charts --version 2.21.0
python modules/agent-factory/webhook-ingress/tests/verify_keda_chart.py \
  --helm helm --chart ./keda-2.21.0.tgz
```

## Migration review and rollout requirements

Reviewed upstream release notes 2.17–2.21 and the
[2.21 migration guide](https://keda.sh/docs/2.21/migration/).
Source manifests use SQS/IRSA. Removed CPU/memory `type`, external `tlsCertFile`,
NATS Streaming, GCP `subscriptionSize`, Huawei `minMetricValue`, IBM MQ `tls`,
InfluxDB metadata `authToken`, and Temporal version settings do not occur in
these source scaler configurations. Prometheus metric removals require any
installation-specific monitoring rules to be checked before rollout.

CVE-2026-77524 is fixed by enforcing non-API token audiences. Keep the chart's
`enforce-audience` default. Inventory both TriggerAuthentication types for
Vault Kubernetes authentication and bound service-account tokens; migrate
receiver audiences and token mappings if present. Review Temporal legacy
fields and Azure Pipelines in-flight-job semantics for all live scaling
resources. The current dev-box identity cannot list ScaledObjects or either
TriggerAuthentication type; **that part of live preflight is incomplete**.
Do not infer absence from Forbidden responses.

No live rollout occurred. Deployment access is also unavailable. With an
existing authorized deployment identity, save current Helm values/revision,
complete the inventory above, and inspect the scoped Terraform plan. Upgrade
through the Terraform-owned Helm release, preserving installation-specific
settings. Do not patch the three images independently of chart/CRDs/RBAC.
Verify scaler Ready/Active conditions, failure Events, SQS activation and
successful worker completion, metrics discovery and webhook rejection of an
invalid ScaledObject. Recollect actual imageIDs after rollout for #6521.

Rollback must restore the previous Helm revision and any receiver/auth changes;
a pre-fix operator restores the token-forwarding vulnerability. Record that
reopened exposure rather than declaring the story closed.
