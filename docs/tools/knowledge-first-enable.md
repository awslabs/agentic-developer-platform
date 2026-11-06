# First enablement of the private Knowledge target

This is an additional protected release prerequisite. Follow the canonical
[agent deployment guide](../adp-platform-deployment/deploy-with-agent.md), retain
each existing owner's configuration and state, and keep retired legacy API
routes disabled. Source support and a successful plan do not qualify live tools.

The tools deployment identity cannot create targets by default. AWS authorizes
`CreateGatewayTarget` against the existing **gateway ARN**; target names,
endpoints and future target IDs cannot be constrained by IAM. The protected
workflow therefore validates the exact saved Terraform plan against a separate
operator-selected binding before applying that artifact.

## Prepare the existing owners

1. Provision and verify the private edge through its owner. Obtain its real
   `mediator_url` and `managed_private_endpoint` outputs. The endpoint must use
   dedicated security groups, private subnets in different availability zones
   and endpoint-specific regional DNS, with execute-api private DNS disabled.
   Do not manufacture a URL, security group or target ID to pass a gate.
2. In the **tools bootstrap** owner, explicitly set
   `enable_gateway_network_service_linked_role=true`. This owns the account-wide
   `AWSServiceRoleForBedrockAgentCoreGatewayNetwork` for
   `bedrock-agentcore.amazonaws.com`. If the role already exists, identify its
   owner first and retain that owner; import only a previously unmanaged role
   into one reviewed bootstrap state. Do not create one copy per environment. `prevent_destroy` retains the
   shared role; keep this independent setting enabled after Knowledge withdrawal.
   AgentCore checks `iam:CreateServiceLinkedRole` during managed private target
   creation even when this role exists. The optional creation policy below permits
   only this exact role with `iam:AWSServiceName=bedrock-agentcore.amazonaws.com`;
   other service-linked roles and IAM administration remain denied.
3. In the **trusted automation** owner, explicitly set
   `tools_knowledge_target_creation` to the edge's actual `vpc_id`, `subnet_ids`
   and resource-gateway `security_group_ids`. Preserve `tools_gateway_arn`, the
   existing target bindings, all other owner inputs and the existing state key.
   Terraform checks that the selected subnets are private, in distinct AZs and
   in the same VPC as the security groups. The default `null` grants nothing.
   If the existing gateway uses a customer-managed encryption key, also set
   `tools_gateway_kms_key_arn` to its verified `kmsKeyArn`. Target creation
   validates that key on behalf of the release caller using `GenerateDataKey`
   and `Decrypt` without encryption context, and `ListGrants` for existing grant
   metadata. Actual configuration encryption uses the gateway service role.
   This opt-in permits only that validation sequence on the exact key through
   the regional AgentCore service and selected caller account. Caller data-key
   use with any encryption context is denied. Direct key
   use, other keys and gateways, key administration and grant creation remain
   denied. Lambda environment decryption keeps its separate function bindings.
4. Review the saved owner plans before applying them. The optional managed policy
   permits target creation only on the existing gateway and ENI creation only
   with the selected subnet/SG resources. New ENI IDs require an account/region
   scoped `network-interface/*` ARN. EC2 Describe calls require `Resource="*"`
   and are restricted to the selected region. Both existing action ceilings
   include the same optional actions. Other gateway, IAM, key, state, resource
   deletion and existing release restrictions remain enforced.

The network service-linked role creates and manages AWS's tagged resource
gateways and related service dependencies. Bootstrap retains ownership of it.
The release caller receives no general VPC Lattice or IAM administration
permission; its optional service-linked-role grant and explicit resource/service
denials are installed together before both action ceilings admit that API.

## Bind the intended target in the protected workflow

The existing protected `ADP_TOOLS_RELEASE_SELECTORS_JSON` secret is required by
both release and drift validation. Its account and region must match the job,
and `components["platform targets"]` must contain the exact `gateway_id` and
complete existing `target_ids`. These selectors are checked against every
retained target, including no-op and output-only plans; the guard does not infer
approval from Terraform's own `before` values. Preserve the other components in
the existing selector document. Read-only access to the selected shared Lambda
remains supported, including Terraform's deferred data-source read action.

Set the `ADP_TOOLS_KNOWLEDGE_CREATE_JSON` variable on the selected protected
`adp-tools-deploy-<environment>` GitHub environment. It is a separate input from
the private Terraform overlay, and does not contain the edge proof. Required
fields, with no extras:

| Field | Source |
|---|---|
| `version` | Integer `1` |
| `gateway_arn` | Reviewed existing tools gateway ARN |
| `endpoint` | Actual edge `mediator_url` |
| `vpc_id` | Actual `managed_private_endpoint.vpc_id` |
| `subnet_ids` | Actual `managed_private_endpoint.subnet_ids` |
| `security_group_ids` | Actual `managed_private_endpoint.security_group_ids` |
| `routing_domain` | Actual endpoint-specific regional routing domain |

The job can receive these settings without reading or replacing the existing
GitHub input secret. Set the durable protected environment variable
`ADP_TOOLS_KNOWLEDGE_CONFIG_JSON` to the same seven-field object. The job merges
only its five Knowledge Terraform fields into the inputs derived from the
original `ADP_TOOLS_DEPLOY_TFVARS_JSON` secret. All other fields remain unchanged.
Absent Knowledge fields and their explicit inactive defaults (`""` or `[]`)
can be filled; matching configured values remain unchanged. Conflicting values,
extra fields, another gateway/environment and differing first-create bindings
are refused. Subnet and security-group order is immaterial; duplicates are
refused. With no durable config, ordinary existing release inputs are unchanged.

Keep `ADP_TOOLS_KNOWLEDGE_CONFIG_JSON` for subsequent releases and drift checks.
The separate `ADP_TOOLS_KNOWLEDGE_CREATE_JSON` remains a temporary authorization
for the first creation and can be removed after independent READY readback.
Keeping the durable config does not grant creation when that authorization is
absent. Removing the durable config does not authorize target deletion or a
configuration replacement; those still require a separately reviewed change.

Missing first-create binding means no new Knowledge target. The Knowledge binding permits only one new
`aws_bedrockagentcore_gateway_target.knowledge[0]`, with name `knowledge`, the
existing gateway, `DYNAMIC` MCP server metadata, execute-api IAM credentials,
the exact private mediator URL and exact IPv4 managed network. Unknown authority
fields, unrelated target creation, extra credential/forwarding configurations,
replacement, deletion, state imports and state moves are refused. Existing
target updates remain supported under their existing gateway and target IDs.
Do not set `resource_priority` on DYNAMIC targets: AWS rejects it. The provider
marks this unsupported optional/computed field unknown during creation; the
validator permits only that exact unknown leaf while requiring its configured
value to be absent. Endpoint, listing mode, credentials and network must remain
fully known and match the protected binding.
[Connections targets](provider-onboarding-automation.md#protected-tool-activation)
use separate protected per-target bindings on this same private network; a
Knowledge binding alone cannot authorize their creation.

AgentCore does not support `DYNAMIC` targets on a gateway with semantic tool
search enabled. Configuring a private mediator therefore removes the gateway's
`search_type = "SEMANTIC"` setting in the same saved plan, before target
creation. ADP uses `tools/list` and explicit tool names; the websearch target is
independent of semantic tool search. Gateways without private mediators retain
their previous search setting. The plan validator rejects dynamic targets when
the gateway still enables semantic tool search or its configuration is unknown.
Gateway updates require `iam:PassRole` for the existing gateway service role,
even when only protocol settings change. The tools deployment profile permits
only its environment's exact gateway role and only for
`bedrock-agentcore.amazonaws.com`; other roles, services and IAM APIs remain
explicitly denied.
The release synchronizes cached DEFAULT MCP/OpenAPI targets. DYNAMIC targets
instead require READY configuration and authenticated live discovery; their
configuration revision uses `updatedAt`, with the discovered tool-list hash
recorded separately. See AWS's [MCP target listing modes](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html).

Before assuming the deployment role, the protected workflow compares
`modules/tools/deploy.sh`, `release_plan.py` and `knowledge_overlay.py` with the running workflow revision.
An older `manual_source_revision` cannot resurrect an older plan validator after
IAM opt-in. Select a reviewed main source containing the current guard; do not
remove this comparison to deploy an older source. Activate the optional role
permissions only after this workflow definition is deployed and no older
unguarded release job remains pending.

Release preflight still occurs before Terraform initialization and apply.
The default native qualification requires the prerequisites below. For an
explicitly authorized dev maintenance release, the protected
[operational smoke policy](operational-release-smoke.md) supplies a dedicated
ordinary-user functional test without claiming native financial qualification.
Both policies preserve the exact saved-plan, private network, source and target
guards described above.
Use the original owner approval, explicit fixture, dedicated native access
session and matching protected job/source/definition/run delivery. Do not fake
workflow environment variables, invent a correlation or retry an unknown
operation to obtain these inputs.

## Complete the runtime and target sequence

Apply the exact-key verifier grants through the bootstrap owner, and deploy the
reviewed Gateway, Door, façade and interceptor code. Publish verified mediator
settings privately with the target ID absent so the first target can bootstrap
metadata. Then run the original protected target release, including its READY
checks and authenticated native smoke. Only after independent matching READY
readback may the returned target ID be published to Gateway and a fresh protected
catalogue record accepted. Update the protected existing-target selectors with
that independently verified ID before the next release or drift job. Remove the first-create binding when it is no longer
needed; ordinary existing-target updates do not require it.

Target creation, schema discovery and native release smoke do not establish the
separate two-owner live Knowledge acceptance matrix or paid/write admission.

AWS references:

- [AgentCore actions and resource constraints](https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html)
- [Managed private endpoint prerequisites](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-egress-private-endpoints.html)
- [Gateway service-linked role](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/service-linked-roles.html#gateway-service-linked-role)
- [Gateway encryption permissions and context](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-encryption.html)
- [EC2 action and resource constraints](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html)
