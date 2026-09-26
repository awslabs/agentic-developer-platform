# Native paid-worker source preparation

[DESIGN.md](../DESIGN.md) remains authoritative. This document describes an implementation detail and its current limits. It does not change the original **2/6** story tally, close #5927, or claim live workload acceptance.

The existing installer's default offline plan can render an optional `paid_worker` projection. It uses the existing `superplane-paid-worker` entrypoint and Go controller sidecar. It does not introduce another task consumer or settlement engine. Omission preserves existing installations.

**This is source preparation, not a runnable worker installation.** Both `--preflight` and `--execute` refuse with `paid-worker-binding-attestation-unavailable` before external commands. Direct adapter activation has the same guard. The renderer emits a paused ScaledJob with zero maximum replicas and no worker egress grants. These documents are for review; the installer does not apply them. Do not manually unpause them or treat a caller-authored receipt as shared authority.

## Closed input

`paid_worker` requires exactly these fields:

| Field | Contract |
| --- | --- |
| `mode` | `native-controller` only. |
| `namespace` | Exact existing installer management namespace. ServiceAccount and container names are fixed as `superplane-paid-worker` and `paid-worker`. |
| `role_arn`, `queue_observer_role_arn` | Distinct existing same-account roles, separate from API/controller roles. No IAM creation or permission claim. |
| `queue_url`, `queue_arn` | One exact standard SQS queue in the selected account/region. |
| `database_secret` | Existing Kubernetes Secret with raw-text `domain-dsn`, `execution-dsn`, and `ca.pem` keys. No inline values. |
| `workspace_credentials_secret` | Existing execution credentials, separate from the registration manager's read-only Secret. |
| `provider_secret` | Existing Kubernetes Secret with `skypilot-token` key. |
| `operation_schema` | Exact dedicated domain schema; never `public`. Existing worker pools check both database schemas at runtime. |
| `skypilot_url` | Selected installer's private SkyPilot service URL. |
| `management_api_server` | Exact HTTPS management API origin. Live EKS identity matching is still required. |
| `node_selector` | Explicit reviewed node labels. Actual schedulability/native-sidecar compatibility remains unverified. |
| `egress` | Exactly `gateway`, `sts`, `database`, `skypilot`, `workspace`, and `management`, each with one host `cidr` and TCP `port`. These are intent for review, not an installed allow policy or proof of endpoint identity. Link-local/metadata endpoints are refused. Host CIDRs do not verify Service or STS reachability and are not durable public DNS pins; actual selector/endpoint correspondence remains an activation prerequisite. |
| `max_replica_count` | Intended eventual concurrency, 1–4. Rendered preparation stays at zero. |
| `active_deadline_seconds` | Explicit bound, 1–3600 seconds. |

All fields are mandatory when selected; unknown fields, lifecycle modes, caller receipts, ambiguous queue identities and unresolved paid images are refused. The native projection omits lifecycle PVCs, policies and environment variables. Only the trusted worker mounts database/provider/workspace Secrets; the Go sidecar shares the scoped task socket/token volume.

## Separate release image

`superplane-executor` remains the long-running `controller-service` Docker target. `superplane-paid-worker` is a separate build selection, repository and immutable digest using the explicit `paid-worker` target in the same maintained Dockerfile. The app buildspec is `releases/buildspecs/paid-worker.yml`. The app-owned project manifest, pending lock and existing exact-commit dispatcher integration are documented in [paid-worker release](../releases/PAID-WORKER-RELEASE.md). Source enrollment is implemented; infrastructure approval/provisioning, image build, ECR publication and promotion have not been performed. No shared workflow is changed.

A release selecting this projection must already contain a separately resolved `superplane-paid-worker` image and same-release source provenance under its own repository. The installer refuses pending images and reuse of the controller-service digest. This change does not manufacture a sample digest or imply that a paid image has been built. The actual image entrypoint/package/native-controller contract still needs verification through the approved build lane before activation can be implemented.

## Admission and mixed queues

The installer projects trusted `SUPERPLANE_PAID_WORKER_MODE=native-controller` into API settings and worker configuration. API workspace create/delete, lifecycle continuation and retirement-access admission refuse before domain reads, quota or reservation work. The common provisioning/facade boundary uses the existing `runtime_config_sha256` classifier to reject lifecycle requests before shared admission. Native provision and cleanup retain their existing paths; native recovery is unchanged.

The worker checks the authenticated lease or recovery scope before opening database pools or entering lifecycle code, so a lifecycle task on a mixed queue cannot inherit native deployment credentials. This refusal does not retire an existing operation or discharge queue obligations. Mode changes remain unavailable, and future activation must reuse the existing installer quiescence checks before any switch with admitted work. Omitted mode preserves legacy worker/API behavior.

## Missing activation evidence

The current non-consuming shared `producer-readiness` route returns only version, ready, domain and organization mapping. It does not authenticate the complete binding or its revision. App code cannot infer the approved worker registry, queue, database Secret/schema, namespace/ServiceAccount/container/image set or current authority from that response. Task acquisition consumes messages and is not a readiness substitute.

The shared owner must supply an authenticated, current non-consuming binding-read contract. Then activation also needs actual selected-role/OIDC and queue-observer verification; real image entrypoints; KEDA CRD/version and native-sidecar support; Secret metadata and read-only database identity/schema/grant checks; actual endpoint/NetworkPolicy enforcement; immutable workload ownership and zero outstanding obligations; and fresh matching producer readiness. The preparation report marks all live identity/schema/network/binding checks false. A forbidden CRD read is unavailable evidence, not proof that KEDA is absent.

No RBAC widening, registry writes, shared binding writes, queue acquisition, reservations, provider calls or cloud deployment are part of this source preparation. Any future live workload claim requires its separately approved real admission and observed execution/settlement/cleanup. #6241 remains a separate recovery dependency.
