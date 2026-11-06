# Private knowledge mediator contract

The knowledge target uses a private IAM-authenticated API edge and a Gateway
mediator. Source support is disabled by default. A successful source test does
not qualify a deployed target, grant tools, or close the live acceptance matrix.

The target endpoint is `/internal/v1/tools/knowledge/mcp` on a **PRIVATE**
execute-api REST API. `modules/tools/platform/knowledge-edge` supplies an optional
edge module with no resources when `enabled=false`. It reuses an explicitly
selected internal Gateway ALB and VPC link. Its resource policy denies requests
from another role or VPC endpoint; the identity policy permits only that POST
route. The target uses `gateway_iam_role.service=execute-api`, private VPC routing,
and `listing_mode=DYNAMIC`. Direct Door endpoints are refused.

The edge overwrites the SigV4 caller identity and two independent proofs: the
existing Gateway provenance proof and a new private-edge proof. The mediator
requires both proofs and the exact protected AgentCore gateway role. Copying an
ARN, using the ordinary regional API, or calling the pod directly is insufficient.
The private proof is a sensitive module output; distribute it through the
protected deployment configuration, never through public documentation or tools.

## Request authority

For calls, the interceptor injects the verified facade assertion into the
internal `adp_assertion` argument. The mediator verifies the pinned KMS key,
facade role, human/root identity, tool name, signed ingress JSON-RPC request ID,
exact canonical argument hash and 30-second validity. Before Door dispatch, it atomically claims
the signed request in the existing authority table; a second delivery of the
same claims is refused across pods. An unavailable replay store fails closed.
Only a digest, not the claims or private content, is persisted. A dedicated
expiry attribute permits DynamoDB TTL cleanup after the assertion expires;
TTL deletion is asynchronous, not an immediate cleanup guarantee. The transport
argument is stripped before Door dispatch. Any run or
workload binding is refused until the original run authority can be revalidated;
no human or worker identity is synthesized.

JSON-RPC IDs identify requests on each MCP hop. The interceptor checks the exact
ingress ID and its type against the facade's signed claim before forwarding.
AgentCore may assign a different ID when it calls an MCP server target. The
private mediator validates the signed ingress ID, verifies the entire assertion,
and uses the target-side ID for protocol responses and its downstream Door call.
It does not require IDs from different hops to match. The signature continues to
bind the original ID, tool, arguments and identity; changing the relay ID cannot
bypass the atomic claim of the signed request or provider receipt.

Private Knowledge, GitHub, Jira and Slack mediators accept the optional MCP
`params._meta` object (or a serialized null) for transport metadata such as
progress tokens. It is removed before method validation and never becomes tool
arguments, caller authority or downstream provider input. Other extra operation
parameters and malformed metadata remain refused; signed argument and request-ID
checks are unchanged.

The mediator locks the current human User, active non-revoked tenant membership,
canonical team membership, and real linked GitHub identity. It derives Cognito
owner subject and optional GitHub login from those rows. It never maps platform
teams into GitHub ACL teams. Current consent, allow list, entitlement and positive
cost ceiling are checked before forwarding and before releasing the result.
Expiration and deployment binding are checked again after the query.

The mediator owns the serialized outgoing bytes and signs the existing `adpd1`
Door assertion for exact POST `/mcp/`, body hash, current tenant and identity.
Only the existing `context-mcp` service DNS names are permitted. No caller-selected
URL, redirects, proxy, cookies or identity headers are forwarded. Requests are
bounded to 1 MiB, responses to 4 MiB, and the mediator deadline is 25 seconds.
Door retains its existing repository and project ACL checks and host allowlist.

## Metadata bootstrap and freshness

A separate `adpdm1` token with audience `adp-knowledge-catalogue` signs only exact
POST `/mcp/` requests containing JSON-RPC `tools/list`, an ID, and empty params.
It carries no human identity. Door rejects this token for tool calls, REST data
reads, a changed method/path/body, pagination, or mixed identity proofs. Native
Door `tools/list` remains the source of schema metadata.

Metadata bootstrap may inspect the uniquely named knowledge target under the
configured gateway while it is CREATING, UPDATING or SYNCHRONIZING. It can return
schemas but cannot publish callable provenance. Before the target ID exists in
protected runtime configuration, only this metadata bootstrap is permitted.
READY status plus the pinned target ID are required for invocation and catalogue
consumption.

The authenticated mediator publishes schemas into protected
`TOOLS_DYNAMIC_CATALOGUE#<gateway-id>` / `TARGET#<target-id>` records. Each record
expires after 60 seconds and binds the current gateway, target, private endpoint,
credential configuration, source revision, update timestamps and schema digest.
Duplicate, oversized, foreign-namespace or authority-bearing schemas are refused.
Readers recheck current control-plane state; removed or changed targets contribute
no callable schemas. The record does not contain user grants.

The AWS control-plane contract for
[`McpServerTargetConfiguration.listingMode`](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_McpServerTargetConfiguration.html)
states that DYNAMIC targets retrieve resources when tools are listed. DEFAULT
cached schemas and static `mcpToolSchema` configurations are refused here. If
provenance is missing or expired, facade discovery makes one authenticated
AgentCore `tools/list` request, then reads authorization again. Direct knowledge
calls perform the same refresh before call authorization. Failure preserves
unrelated static tools and reports the dynamic catalogue unavailable.

Knowledge schemas replace stale metadata in the effective policy response. This
read-time synchronization never modifies persisted allow lists, consent,
entitlements, cost ceilings or budgets.

## Protected configuration required before enablement

| Input | Required binding |
|---|---|
| `ADP_TOOLS_KNOWLEDGE_MEDIATOR_ENABLED` | Defaults disabled; reviewed configuration must explicitly set `true` |
| `ADP_TOOLS_KNOWLEDGE_MEDIATOR_URL` | Exact private-edge module URL, also configured on the AgentCore target |
| `ADP_TOOLS_KNOWLEDGE_EDGE_PROVENANCE` | Sensitive private-edge output, independent of ordinary Gateway provenance |
| `ADP_TOOLS_KNOWLEDGE_GATEWAY_ID` | Reviewed configured tools gateway |
| `ADP_TOOLS_KNOWLEDGE_TARGET_ID` | Reviewed target ID; absent permits metadata bootstrap only |
| `ADP_TOOLS_KNOWLEDGE_SOURCE_REVISION` | Exact reviewed 40-character source SHA backed by deployment evidence |
| Existing facade role, AgentCore role and assertion KMS key | Exact protected role/key bindings; no caller-selected values |
| Existing Gateway Ed25519 key and Door public keyring | Matching reviewed signer and verifier; private key remains in Gateway |
| `ADP_DOOR_SERVICE_URL` | Existing private `context-mcp` service origin |
| `AGENT_AUTHORITY_TABLE` | Protected policy/catalogue table; Gateway is the only catalogue writer |

The canonical Gateway Deployment reads the six `ADP_TOOLS_KNOWLEDGE_*` values
above from the dedicated `adp-tools-knowledge-mediator` Kubernetes Secret in its
namespace, using those exact names as Secret keys. Each reference is optional:
an absent Secret leaves the default configuration disabled. Keep these six keys
absent from other ConfigMaps and environment sources: optional Secret references
do not erase an existing `envFrom` value. Publish only verified private
edge outputs and deployment bindings through the protected deployment identity;
do not place the private proof in the default ConfigMap or a public artifact.
The Secret is operator-owned and is not synthesized by ordinary Gateway deploys.
Roll out Gateway pods after creating or updating it, and verify the effective
settings privately without printing the proof. Additional listeners do not gain
Knowledge configuration through the image-only writer synchronization path;
configure them only if their reviewed route is intended to serve the mediator.

Configuration review must verify the existing private execute-api VPC endpoint,
DNS/TLS routing, internal ALB/VPC link and security groups. The Gateway runtime
requires `kms:Verify` on the exact facade assertion key, current gateway/target
read permissions, and strongly consistent GetItem plus PutItem on the exact
catalogue partition in the authority table. Workers, end users, facade and target
roles must have no catalogue write permission. The edge module grants the target
role only `execute-api:Invoke`; it grants no Door signer or table access.

The shared bootstrap inputs must bind `knowledge_mediator_role_arn` to that
existing Gateway service role when `knowledge_door_endpoint` is configured.
Bootstrap grants only verification on the assertion key through both the exact
key-policy principal and the role policy. The facade and AgentCore target roles
are refused as mediator identities. No Knowledge verifier grant is created while
the endpoint is empty. An IAM role policy alone is insufficient: the assertion
key delegates only administrative actions to account IAM, not `kms:Verify`.
Knowledge also requires the assertion key to be independently configured through
the facade, interceptor or credential binding. Changing only the Knowledge
endpoint to empty withdraws its verification grants while retaining the key;
the key's `prevent_destroy` protection remains in place.

No live IAM, private edge, environment or consent change is made by these source
changes. Review and deploy using the canonical ADP deployment guide with private
operator configuration. Confirm emitted source/image/edge bindings independently.

### Provisioning the missing private route

For an environment without an execute-api endpoint, the edge module accepts an
optional `managed_network` object containing the selected `vpc_id` and at least
two distinct `subnet_ids`. The module verifies that those subnets belong to the
selected VPC, disable public IP assignment and occupy distinct availability
zones. This option cannot be combined with `execute_api_vpc_endpoint_id`. It
creates nothing until the edge's `enabled` flag is true.

The managed network creates a dedicated interface endpoint with
`private_dns_enabled=false`. Its two security groups allow only HTTPS from the
Knowledge resource gateway to the endpoint. Its endpoint policy binds the exact
AgentCore role and the private API's stage, method and route. The existing API
resource policy independently checks the role and source endpoint. Existing
endpoints remain owned by their original configuration.

Use `managed_private_endpoint` to bind the target's VPC, subnets, resource
gateway security group and `routing_domain`; use `mediator_url` for the target
URL. The routing domain is the endpoint-specific regional DNS name. AgentCore
[preserves the actual target's TLS SNI while routing through that domain](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-egress-private-endpoints.html).
Do not enable wildcard execute-api private DNS: it would intercept the existing
public REGIONAL Gateway API. No private hosted zone is needed for this route.

Deployment must complete the endpoint policy and API resources before publishing
the outputs or creating the target. Then apply the bootstrap's exact-key
verification grant and publish the mediator settings through protected Gateway
configuration. Start metadata bootstrap with the target ID absent, create the
reviewed DYNAMIC target, and publish its returned ID only after matching its
READY control-plane state. Independently verify the endpoint policy, SG rules,
DNS/TLS route, mediator source/image, protected settings and target binding.
Terraform mock checks cover the network contract; they do not prove a deployed
route or satisfy live tool acceptance.

Before disabling or replacing the managed network, withdraw the Knowledge target
or migrate it to its replacement route. Wait for AgentCore to release the old
managed resource gateway and its network interfaces before deleting the old
security groups. Their attachment is owned by AgentCore outside this module;
removing the edge module first can leave the apply blocked by an attached group.

### Dedicated capacity when existing private subnets are full

`modules/tools/platform/knowledge-capacity` is a separate, opt-in owner for two
dedicated IPv4 `/24` subnets in an existing VPC. It requires two distinct
availability zone IDs, disables public address assignment and creates one
explicit route table with only AWS's local VPC routes. The subnets have no
Kubernetes discovery tags; inherited provider defaults with Kubernetes or
Karpenter discovery prefixes are refused. It creates no endpoint, security
group, workload, target or new VPC. The default `enabled=false` performs no
cloud reads and creates nothing.

The deployment coordinator authenticates the selected account, reads every
existing subnet CIDR and the VPC's associated ranges, and confirms the two
chosen ranges are free before preparing the saved plan. Keep these selections
private. Each range must be a canonical `/24` within an associated VPC CIDR;
AWS also refuses overlapping subnet creation. Check VPC DNS and the inherited
network ACL permit the private endpoint path. No internet or NAT route is
needed between a resource gateway and its execute-api interface endpoint in
this VPC. Existing worker subnets and network interfaces remain with their
original owners.

Use a distinct protected state key, for example
`tools/knowledge-capacity/<environment>.tfstate`, and an explicitly selected
provider account/region without workload discovery default tags. This owner is
not wired into automatic deployment. After source review and merge, review a
saved capacity plan containing exactly two subnet creates, one route-table
create and two association creates. Recheck CIDR availability before applying.
Read back the subnet VPC/AZ/CIDR, public-IP settings, tags, remaining capacity
and exact local-only route associations before handing its `managed_network`
output to the edge owner.

Complete that apply first, then copy the resolved VPC and subnet IDs into the
private inputs for a separate edge plan. Passing unknown subnet IDs from a
single combined apply cannot satisfy the edge's subnet data-source keys. During
recovery, refresh the existing edge state and retain any proof, security groups
and rules already created; the next saved edge plan must not replace them or
change bootstrap/platform state. Validate its endpoint, policies and routing
before activating a target. Source/mock checks do not establish live capacity
or tool acceptance.

Teardown follows consumer order: withdraw the target, allow AgentCore to release
its resource gateway interfaces, remove the edge's endpoint, then remove this
capacity owner. Never reclaim capacity by deleting active workload interfaces
or by moving an existing subnet between owners.

## Remaining admission and live acceptance

Read-only code search, structural understanding, impact, browse and secure reads
use existing Door backends. Ordinary human `remember` and `experience` calls
require tenant/team grants, user policy and consent, bounded arguments, and the
original budgeted tool admission. The facade signs the original request and
execution reference; the mediator reopens that admission and claims one durable
dispatch without an expiry. Discovery receipts cannot authorize memory writes.
Memory and semantic scopes of `search` remain `not_ready` with
`knowledge_admission_unavailable`; native/run memory admission remains separate.

Memory calls reserve a conservative $0.01 estimate through the existing budget
hierarchy and reject a lower configured ceiling. This is an estimate, not a
provider-billed final cost. Content is bounded to 8 KiB, recall queries to 2 KiB
and recall limits to 25. A missing or uncertain write acknowledgement retains
the original unknown operation: retries recover that operation instead of
dispatching again. A completed memory operation closes only with a mediator
receipt bound to its owner, arguments, request, execution and result. Closing
settles execution bookkeeping and preserves the saved memory.

| Released Door verb | Locally exercised path | Required backend or outstanding admission |
|---|---|---|
| `search` | `scope=code` with exact indexed citation | Code index; `docs` and `memory` scopes need paid admission |
| `understand` | Symbol lookup | Structural index |
| `impact` | Symbol blast-radius lookup | Structural index |
| `browse` | `ls`, `tree`, `read`, `info` | Indexed content |
| `remember` | Bounded session save under original human admission | Personal context store and embedding backend |
| `experience` | `save`, `recall`, `list_syntheses` under original human admission | Personal context store and embedding backend |
| `secure` | `identify`, `plan`, `verify` | Vulnerability index and ACL store |

The shared runner records separate operation cells, the required backend state,
provider-call deltas and independently confirmed cleanup. Blocked cells are not
counted as successful search or as live qualification.

Live qualification still requires the two-owner CLI, Claude Code and Codex
matrix, exact own sentinel/citation, wrong-project and foreign-tenant denial,
revocation, unavailable/pending indexes, removed target, worker regression and
owned fixture cleanup. The source tests use fictional rows, local keys and
mocked cloud control-plane responses; they do not establish live authority,
index readiness, private routing or deployment success.
