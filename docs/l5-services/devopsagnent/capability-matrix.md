# DevOps capability and catalog qualification (DA01, R2)

This is a **versioned contract**, not an implementation or live readiness report.
[Machine-readable rows](capability-matrix.json) carry the sources, conditional
inputs and blocking owners checked by offline fixtures. Supported schema inputs
are candidates; they are not proof of native access, provider results or approval.
An absent prerequisite changes only the dependent capability's readiness.
The [portable scenarios](portable-fixtures.json) use the same schema with distinct
Product/project, team, repository and environment identities. They check input
gaps separately from the live `blocked_live` status: complete inputs alone do
not authorize a provider job or justify `ready` without fresh provider readback.

## Catalog identity

The checked source catalog contains Portfolio/Initiative models, while the
Insights registry admission schema accepts normalized records without naming an
L5 Module/Feature registration. No verified L5 Module ID or Feature IDs were found
in those source files, and no live registry was queried. The mapping is
**unresolved**, not empty by design: DA01, SA01 and PranavSharma1000 must inspect
an authoritative Module/Feature registry and record the real IDs **before dependent dispatch**. Story labels, directory names and the roadmap Project
are development lineage, not runtime Product/project identifiers. Architecture
preparation and offline contract checks can proceed without invented IDs.

## Source audit and limits

| Source | What it establishes | Remaining qualification / owner |
|---|---|---|
| Gateway dependency pin and [shared SDK audit](../contracts/v1/compatibility.json) | Gateway uses boto3 1.43.111; 1.43.114 is a **worker candidate**, not an approved DevOps model. There is no deployed L5 worker package here. | DA05/DA19 verify exact SDK model, permissions, Region and provider readback before enabling native actions. |
| [SSO POC report at draft revision](https://github.com/aws-e/adp/blob/7f82f551d2f1b8aa4ec46a829a217d6c95daf17e/docs/l5-services/devops-ui-sso-poc.md) | The report says one existing GitHub-backed ADP identity reached the native UI by IAM federation; AWS rejected `ADP` custom OIDC. It is **reported POC evidence**, not an independently repeated DA01 check. | DA08/DA19 qualify two-user policy, membership removal, session expiry/revocation and console-route stability; no arbitrary custom OIDC. |
| [Release-readiness report](https://github.com/aws-e/adp/issues/8012) and [release-verification report](https://github.com/aws-e/adp/issues/8017) | Offline drivers; readiness had native task permission denial, verification lacked isolated target/deployment identity. | DA11/DA12/DA19 obtain native results tied to exact source and target; no invented approval or result. |
| [Context report](https://github.com/aws-e/adp/issues/7973) | S3 source and marker readbacks were reported. | DA06/DA07/DA14/DA19 qualify indexing, grounded citations and scoped reuse. |

## Capability boundaries

| Capability | Required selected inputs | Conditional dependencies | Live qualification owner |
|---|---|---|---|
| Native UI | Space, current Product/project membership, qualified scoped session | No GitHub or EKS dependency | DA08/DA19 |
| Investigation | Account, resources and readable telemetry | Cluster, namespace and network access **only for EKS**; code/deployment source only if requested | DA05/DA09/DA10/DA19 |
| Release readiness | Supported repository, exact base/head, native eligibility | No assumed ADP repository or EKS layout | DA11/DA19 |
| Release verification | Source/deployment linkage, isolated target, credential reference and scenarios | Provider connector and target access actually selected | DA12/DA19 |
| Prevention | Explicit native enablement and verified schedule | Not an ADP run admission or the inbound incident trigger | DA13/DA19 |
| Knowledge/Context | Entitled sources and publication/retrieval evidence | No implied indexing from S3 reads | DA07/DA14/DA19 |

`github` and `cloudwatch` are first-epic **proposed, unqualified** integrations;
`eks` and `aws_resource` are selectable workload kinds. Unsupported `gitlab`,
arbitrary telemetry and custom OIDC selections have explicit reasons in the
matrix and are rejected by the current schema. Native UI needs no repository
connector. No provider method or result is inferred from a manual or an SDK field.
