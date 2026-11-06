# Native service permissions on Product connections

A verified AWS connection proves its role can be assumed. It does not prove that
the role permits native agent setup or execution. Platform observation roles
may therefore appear in Product options while lacking the permissions needed
by a selected L5 service.

`modules/L5-services/infra/provider-access/` is an independent, operator-owned
Terraform root for the connection's native service permissions. It attaches a
separate inline policy to the selected existing role and preserves its trust
policy and other permissions. Keep one state per connection, for example
`l5-services/dev/us-east-1/provider-access/example-connection/terraform.tfstate`.
Normal worker and release roles cannot expand these permissions.

The initial contract provides:

| Selection | Permissions |
|---|---|
| Security enabled | Explicit native space, code-review and threat-model operations in the selected account and region |
| Security space creation | Tag only the required `adp-setup-operation` key; the broker pins its value to the original setup operation and expires the grant with the work lease |
| Security source buckets | Get/put only below each registered bucket's `security-agent/source/` prefix |
| Security execution roles | Pass only the selected same-account roles to `securityagent.amazonaws.com` |
| FinOps native agents | Region-limited native workspace reads/session creation and connection/integration discovery; selected agents remain enforced by Product binding and executor checks |
| FinOps billing views | Read the selected billing views and their Cost Explorer data |
| DevOps existing spaces | Read selected spaces/goals/tasks/results, submit or cancel finite tasks and open authenticated sessions only for selected space ARNs |
| FinOps data roles | Read selected role metadata and the policy documents needed to validate their billing access |

AWS documents `CreateAgentSpace` and `ListAgentSpaces` without resource-level
permissions, so those two actions require `Resource: "*"` with the selected
region condition. The other Security actions use the documented
`agent-space/*` ARN in the selected account and region. This account-local role
policy is a prerequisite ceiling; broker sessions must narrow existing-space
operations to the selected durable binding. See the
[AWS Security Agent authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_securityagent.html).

That reference also lists `securityagent:TagResource` for tagged
`CreateAgentSpace` requests and `iam:PassRole` for `UpdateAgentSpace`.
The code-review adapter updates the selected space's registered execution role,
so its broker session must allow passing that exact observed same-account role
to `securityagent.amazonaws.com`. A connection-role allow policy alone is
insufficient: STS also intersects it with the broker's session policy. The broker
does not grant role passing to unrelated services or tagging during later
readback, cleanup, or assessment operations. Until AWS returns the new space ID,
tag-on-create uses the selected account/region's `agent-space/*` ARN with the
exact setup tag; later operations use the recorded space ARN.
AWS's tagging permission can also apply that exact tag to an existing space in
the selected account and region. A tag alone is not ownership evidence: setup
reconciliation and cleanup also require the original name, intent-hash
description, and retained resource receipt. The setup tagging grant cannot
update those native identities or delete a space.

The current [FinOps IAM reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_finops-agent.html)
also documents no resource-level permissions for `GetAgentSpace` or
`CreateOneTimeLoginSession`. Their policy therefore requires `Resource: "*"`
with the selected region. `finops.agent_arns` records the selected native agents
and enables this capability; it is not an IAM resource restriction. The Product
binding, temporary broker authorization and isolated executor enforce the
selected workspace. AWS does not provide an agent-level IAM boundary for these
operations. Billing views and data-role access retain exact resource ARNs.

Unselected services add no policy. FinOps native agent creation, IAM mutations,
unrelated S3 access and arbitrary role assumption are not granted. DevOps native
space creation, deletion, service association changes and preventive-goal changes
are not included in this adoption policy. Transform and Well-Architected
permissions remain governed by their service onboarding contracts; this root
does not claim to provision them.

To configure an onboarded Product:

1. Read its verified AWS connection and selected native agent, billing-view,
   source-store and execution-role references through Product configuration.
2. Confirm the operator AWS identity matches the selected account. Create a
   private variable file for this root using those references, and initialize
   its separate S3 state. Do not put live identifiers in repository documents.
3. Plan the change and review the exact role, actions, resources and conditions.
   Validate the generated policy with IAM Access Analyzer. No connection trust
   policy, native resource or unrelated role should change.
4. Apply the saved plan, read the attached policy back and test both the intended
   access and foreign-resource denial. Then verify actual native reads through
   the selected connection; IAM simulation alone is not service acceptance.
5. Continue normal L5 setup and run admission. Product rights, explicit
   delegation, source selection and the broker's narrower per-operation session
   policy still apply. Provider subscriptions and native eligibility are separate
   prerequisites; adding IAM permissions cannot satisfy them.

The shared L5 CI discovers this root and runs its Terraform validation and mock
tests. Installation of this permission policy is preparation, not evidence that
an L5 worker is installed or that a native run succeeded.
