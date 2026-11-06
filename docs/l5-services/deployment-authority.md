# L5 deployment authority

The L5 deployment role is separate from Gateway deployment authority. An
operator prepares it once using `modules/L5-services/infra/deployment/`; a
normal L5 release cannot change its role, boundary, policies, or bootstrap
state. Do not widen the Gateway deployment boundary to install L5 workers.

## Preparation

1. Verify the selected AWS account and region. Read the existing platform's
   state bucket, versioned artifact bucket, VPC, private subnets, database and
   endpoint security groups, Gateway role/API, and agent-registry table. Use
   these real references in a private Terraform variables file.
2. Initialize the deployment root against a separate backend key:
   `l5-services/<environment>/<region>/deployment/terraform.tfstate`. Plan,
   inspect the IAM changes, validate the policy documents, and apply the saved
   plan using operator authority. Keep raw plans and inputs private.
3. Inspect the Gateway role's managed-policy and aggregate inline-policy
   capacity. Apply `infra/foundation/` once using operator authority. Its
   transport policy grants the existing Gateway access to the five named
   service queues and discovery parameters. The default attachment is managed;
   select `gateway_transport_mode = "inline"` if managed attachments are full
   and the same document fits the remaining inline-policy byte allowance.
   Retain that selection in the private infrastructure configuration. Existing
   managed attachment addresses migrate without recreation. Subsequent release plans can read
   this policy but cannot modify it or attach arbitrary policies to Gateway.
4. Confirm database IAM authentication is enabled. After migrations, execute
   `scripts/bootstrap-worker-db.sql` as the database owner. It creates the
   passwordless `adp_l5_worker` principal and grants access to existing `l5_`
   tables only. Rerun it after migrations introduce new L5 tables; it does not
   grant blanket default privileges on the shared schema. Verify this user
   has no access to identity or orchestration tables.
5. Set `ADP_L5_DEPLOY_ROLE_ARN` in the selected protected GitHub environment
   to the root's deployment-role output. Set private
   `ADP_L5_INFRA_CONFIG_JSON` using the contract in
   [modular deployment](modular-deployment-design.md). Use the worker-boundary
   output, the database IAM ARN for `adp_l5_worker`, and each implemented
   service's runtime factory. OIDC trust must select the exact repository and
   protected environment. Existing environment protection remains in force.

`deploy-infra.py` deliberately accepts only foundation and the five service
roots. It cannot select the deployment root. Worker boundaries allow the
internal Gateway, selected database, service queues, logs and work artifacts;
customer credentials continue through Product authority and the broker.
No public Lambda URL is required.

FinOps and Transform executor images use the two exact repositories
`adp-<environment>-l5-finops-executor` and
`adp-<environment>-l5-transform-executor`. Release authority can publish and
inspect images there; it cannot delete repositories or publish to another
repository. The separate `finops-artifacts` and `transform-artifacts` state
keys support preparing image storage before service installation.

For a Transform executor on EKS, set `transform_cluster_name` in the operator's
private deployment-root input. This grants only `eks:DescribeCluster` for that
cluster in the selected account and region, including the worker ceiling.
It does not grant Kubernetes access. Prepare the actual worker access entry,
namespace permissions and isolation under the service's operator-owned
bootstrap before enabling the executor. The normal release cannot create
access entries, associate cluster policies, or elevate its own cluster access.
Leaving the cluster name empty grants no cluster discovery permission.

FinOps result reads also need a small operator-owned Gateway grant. Initialize
`infra/finops-bootstrap/` at
`l5-services/<environment>/<region>/finops-bootstrap/terraform.tfstate`, selecting
the existing Gateway role and the same archive bucket used by the FinOps worker.
Inspect the aggregate inline-policy byte allowance before applying: this root
uses an inline policy and consumes no managed-policy attachment slot. It permits
only versioned reads of original native FinOps answers. The API must still check
current Product authority before releasing any answer. The normal service
release cannot change this grant or read its bootstrap state.

## Verification

Before lifecycle resume enables consumers, every selected worker, repair and
DevOps event/readback entrypoint must pass a versioned, read-only database
privilege probe using its own configured `adp_l5_worker` identity. This checks
existing public L5 table grants and rejects elevated role attributes, unexpected
memberships, protected-table access or database/schema creation rights. It does
not grant privileges, load providers or admit work. Factory loading remains a
separate check, and a missing or malformed probe leaves consumers paused.

A `worker_database_grants_missing` receipt lists bounded table/permission names.
After confirming the reviewed migrations, the database owner reruns
`modules/L5-services/scripts/bootstrap-worker-db.sql`, verifies protected-table
denial, and retries resume. Broader access failures require restoring worker
isolation; connection failures require checking database/IAM connectivity. The
probe does not establish native service qualification.

The offline Terraform tests check exact OIDC scope, mandatory worker boundaries,
policy-size limits, bootstrap-state isolation, and absence of self-modification
or Gateway-policy mutation authority. Validate the rendered policies with IAM
Access Analyzer before applying them to a new target.

Infrastructure preparation does not establish service readiness. Install an
immutable worker package, run the service's real Product-scoped journey, read
the result independently from its provider, and verify retrieval through ADP.
Keep unqualified consumers disabled and preserve failed attempts and receipts.
