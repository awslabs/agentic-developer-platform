# Protected native Product observation

The Product runtime reader now has a process-local, read-only alternative to
Kubernetes pod execution. `verify_operations_runtime` still calls
`PilotRuntimeReader.inspect`, then `KubernetesRuntime.gateway` and `schema`.
Product observation uses the new protected `POST /internal/v1/runtime-proof/schema`
route; other runtime observations retain their existing transport.

The route accepts only an edge-verified IAM caller whose exact role ARN is in the
deployment's reader allowlist. It does not accept actor tokens, shared API keys,
or an unverified caller header. It compares the requested namespace and pod UID
with the process's Kubernetes Downward API identity before accessing PostgreSQL.
The query runs in a read-only transaction with statement and lock timeouts. It
returns actual database migration heads, the image's Alembic heads, the checksum
of installed CLI manifest bytes, and the process's two Product capability flags.
It returns no database address, credentials, actor data or Product content.

The existing IAM ingress may select a different serving pod. The reader retries
only the wrong-pod response, with a 100-request and 30-second bound; each HTTP
request also has a timeout. Failure to reach every selected UID is incomplete,
never a pass. Redirects and denied or malformed responses refuse. A random nonce,
timestamp, UID and namespace bind each response to its request. The reader
rechecks pod identity and deployment generation, compares every schema/CLI/flag
result with the pinned release, then independently checks the exact dev tick
Lambda's Active/Successful state and resolved image digest.

Before any fixture write, the selected lifecycle driver reads the maintained, signed and versioned
`gateway-backend` release record for the **current Gateway deployment workflow
run and attempt**, checks its source, target, flags and image against the selected
inventory, and invokes this same runtime reader. Cancellation readiness remains a
separate guard after observation and before any fixture provisioning. Completed
evaluation retains the runtime receipt
and its content hash in private API evidence. A standalone live-test dispatch
without that authenticated deployment record cannot substitute a static JSON
claim. The supported parent entrypoint remains `gateway-deploy.yml` with explicit
Product case selection and the matching source revision.

## Deployment prerequisites, not an authorization to apply

Maintain a private `bedrockgateway-runtime-proof` ConfigMap in the Gateway
namespace with these keys; the deployment manifest consumes them without changing
the existing secret or actor configuration:

| Key | Required value |
| --- | --- |
| `reader-role-arns` | Comma-separated exact IAM role ARNs for the registered runtime verifier and protected evaluator; no whitespace or wildcards |
| `internal-invoke-url` | The reviewed regional execute-api stage origin for this Gateway |
| `evaluator-role-arn` | The exact instance role associated with `EVALUATOR_REGISTRY_ID` in [the protected cleanup route](../../../modules/gateway/src/products/evaluation_cleanup_routes.py) |
| `recovery-role-arn` | The distinct orchestrator role associated with `RECOVERY_REGISTRY_ID` in [the protected cleanup route](../../../modules/gateway/src/products/evaluation_cleanup_routes.py) |

The manifest obtains `ADP_POD_UID` and `ADP_POD_NAMESPACE` from Kubernetes, not
operator-supplied identity strings. Missing runtime configuration refuses proof;
missing cleanup role configuration refuses retirement for that identity.

Each reader requires `execute-api:Invoke` on only the stage's
`POST/internal/v1/runtime-proof/schema` resource and `lambda:GetFunction` on only
the selected account/region's `adp-dev-orchestration-tick` function. Retain the
existing namespace-scoped Deployment/ReplicaSet/Pod reads and EKS describe access.
Do not add `pods/exec` or broad IAM permissions. The evaluator additionally needs
`s3:GetObject` on this repository's `gateway-backend` release records in the existing
versioned deployment evidence bucket. Runtime checks constrain the two dynamic
run/attempt path segments to the current protected dispatch.

The retained evaluator's additive Terraform configuration now declares the exact
probe and tick resources. Set its optional `repository_id` to the reviewed numeric
repository identity to declare the release-read policy. The runtime verifier's
selected connection owner must maintain the same two exact probe/tick grants in
that connection's own policy; evaluator grants do not authorize another role.
Apply/read back these resources and the ConfigMap only through the authorized
deployment process, then verify every serving UID after rollout. No installed
permission, live proof, or deployment is established by offline tests.

## Remaining execution work

The cancellation-recovery readiness guard and complete recovery-case dispatch
still need production integration. Do not remove that guard based solely on a
successful schema probe. Preserve protected retirement, actor/session recovery,
all-attempt evidence, shared-resource preservation and the final human B01
decision. Live acceptance remains pending until the selected cases and complete
cleanup actually execute.
