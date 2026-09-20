# Deployment runtime verification (ENGINE-D3, #5152)

The production K2 runner handles `awaiting_runtime_verification` through
`deployment_controller.py`. A successful workflow is an input to verification.
It cannot complete delivery or clear a graph evaluation barrier.

D3 requires the current tenant, accepted plan version, claim generation, active
protected worker grant, PR binding and M2 merge receipt. It reads the enabled D1
manifest and accepted deployment/credential authority, then resolves the
registered vault role through the existing ACL and tagged STS service. STS and
EKS identify the account, region, cluster and namespace. Runtime reads use only
that role. The packaged manifest remains unresolved; this implementation grants
no deployment authority and does not activate any feature flag.

The supported adapters are:

| Manifest adapter | Components | Evidence |
| --- | --- | --- |
| `gateway-health-verification` | gateway-backend | Desired/updated/available replicas, observed generation, owned Ready pods at the exact image digest, read-only SQL schema check, matching Active/Successful tick Lambda digest |
| `gateway-health-verification` | gateway-frontend | Approved build hashes compared with the actual published CloudFront bytes in the verified account |
| `alembic-single-head-verification` | gateway-migrations | Serving gateway image digest and database Alembic head compared with the verified image's single head |

The existing deployment workflow builds and updates the gateway and tick. D3
observes that mechanism; it adds no rollout or migration coordinator. EKS uses a
short-lived STS presigned token and TLS with the cluster CA. The schema probe is
a fixed Python command over the Kubernetes WebSocket exec protocol, with a
read-only SQL transaction. It executes no migration. The registered role must
have the required Kubernetes read/exec permissions; unavailable access blocks.
Generic `/health` and `/ready` responses supply no revision or schema proof.

`gateway-deploy.yml` and `run-gateway-migrations.yml` publish
`adp-release-<component>-<run_attempt>`, containing exactly `release.json`.
The producer records the immutable source, workflow/run identity, resolved AWS
target and ECR image digest or frontend build hashes. This evidence is distinct
from D2's pre-deployment context archive. D3 authenticates both artifacts through
scoped GitHub reads, verifies archive digests and limits sizes before decoding.
A rerun cannot reuse an earlier attempt's receipt. Frontend inventory is limited
to 256 files, 8 MiB per file and 32 MiB total; unsupported larger builds block.

A newer release requires a current explicit manifest artifact pin, a provider
comparison proving that it contains the story merge, an approved workflow and
its authenticated release artifacts, and successful runtime readback. D3 never
uses a moving branch name to infer the running revision. `source_revision`
remains the story merge; `actual_revision` identifies the verified containing
release for E1. Every component in the final receipt must use that same revision.

Verification creates a `deployment_verification` ledger action with a bounded
`DeploymentReceipt`. Per-entry receipts have `delivery_complete: false`. Lease
release and the D2 `runtime_verified` marker commit atomically with that receipt.
D3 then returns to `deployment_pending`; D2 selects the next remaining entry or
records an all-entries handoff. D3 aggregates current, unexpired component
receipts into a final `delivery_complete: true` receipt and advances to
`evaluation_pending`. E1 must consume that final receipt; an individual entry
receipt does not prove all affected components were verified. Documentation-only
classification requires the audited changed paths and approved docs entry, and
produces a final receipt with no fabricated runtime components or targets.

Receipts preserve target, claim/merge identity, workflow action references,
actual image digest, migration head, tick digest or verified asset count,
observation time and validity deadline. `artifact_hash` is the authenticated
release archive hash; `image_digest` is the container hash. Frontend byte hashes
remain in the referenced release artifact. Aggregation preserves the oldest
observation's validity rather than refreshing it. Shared execution readback
shows the original and actual revisions, completeness and observation window;
credential inputs stay outside that projection.

Transient transport errors and throttling retry inside the original D2
observation deadline and accepted policy limits. An unknown outcome never causes
redispatch or releases the lease. Failed workflows, stale digests, incomplete
rollouts, unsupported adapters and unverifiable schema evidence block with a
typed prerequisite while retaining the hold. An accepted-scope R2 repair must
be established before corrective work; D3 does not invent that authorization.

No rollback adapter is registered in this pilot. In particular, the manifest's
`gateway-revision-rollback` name alone is insufficient. Application rollback
requires an implemented approved adapter, accepted policy and recorded prior
release; until those exist it remains an explicit prerequisite. Database
downgrade, infrastructure apply, worker drain and key projection are unsupported.
The outstanding Terraform tick image ownership concern in #4298 remains a
separate prerequisite; D3 detects digest drift without applying Terraform.

Code/CI closure, deployed revision readback and parent live qualification are
separate. This child supplies no evaluation verdict and no qualifying run.
