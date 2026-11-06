# AWS DevOps Agent configuration manual

Reviewed **2026-10-26** against the AWS DevOps Agent **User Guide** (AWS's name
for the developer/setup documentation), its CLI reference, and ADP source commit
[`925ed8d3d`](https://github.com/aws-e/adp/commit/925ed8d3d9a67aa7b0eb33ea91343e7356d5c5be).
This manual configures an operational Agent Space for an existing product. An
Agent Space that opens successfully is not yet connected to that product's code,
AWS resources or telemetry.

- [Product Profile configuration mapping](product-profile-mapping.md): required inputs, exact ADP fields, implemented discovery and remaining gaps.
- [CLI procedure](cli-reference.md): existing ADP reads and AWS setup/verification commands.
- [L5 architecture](../../architecture.md): configuration, provider binding, execution and publication ownership.

The procedure works for different products and accounts. Resolve deployment
identities privately; names and example values in this manual are not live targets.
Writing this manual did not configure integrations or start investigations.

ADP is the platform running the integration, not a required target Product. Use
the selected project's accounts, repositories, team and policies throughout.
EKS steps apply only to Kubernetes workloads. AWS-native connector options in
this manual may exceed the first ADP adapter release; the proposed
[project portability contract](../architecture.md#project-portability-contract)
defines per-capability readiness and shared runtime delivery for onboarded projects.

## 1. Choose the Product, Project and operational scope

In ADP, open **Products**, select the Product and its governed Project, and review
its information, people and sources. Record the Product/Project IDs and current
profile revision. Use the [CLI reads](cli-reference.md#1-read-the-selected-products-profile)
for the same information. Review the actual saved profile and source bindings;
fields supported by the schema can still be empty for this Product.

Choose the capabilities you are setting up:

| Capability | Inputs that must be available before its useful acceptance test |
| --- | --- |
| AWS incident investigation | AWS account association and resource-reading role, usable telemetry, application scope; EKS access when investigating Kubernetes. Repository/deployment context improves diagnosis. |
| Incident automation | Working manual investigation plus an authenticated alarm/incident source, routing and trigger policy. |
| Release readiness review | Supported Region, connected repository and appropriate GitHub/GitLab access; select automatic or on-demand use. |
| Release testing | Repository/workflow context, a supported verification environment, target endpoints/network access, test credentials and explicitly selected test execution behavior. |
| Proactive prevention | Investigation history and a deliberate evaluation schedule; an empty space has little incident evidence to learn from. |

Record environment names, production status, account/Region coverage, application
resources and dependencies, and the accountable team. A shared account may host
many applications. Use IAM resource boundaries and separate spaces where access
or operational ownership differs. Do not infer a monitoring account, production
scope or native space from a display name or the first item in a list.

**Done when:** the selected Product/Project, capability, environment boundaries
and owner are unambiguous. This is configuration input, not an AWS access grant.

## 2. Select the AWS account, Region and existing or new Agent Space

Verify the AWS identity with `aws sts get-caller-identity` using the intended
operator profile. List Agent Spaces in the selected account and Region before
creating one. Reuse a space only when its ownership and integration scope match;
record its native `agentSpaceId`, not just its name.

For a new space, open the **AWS DevOps Agent console → Create Agent Space**:

1. Enter a name derived from the product/environment and a description of its scope.
2. Choose optional response language and ownership tags.
3. Decide encryption requirements before creation; use the AWS KMS instructions
   if a customer-managed key is required.
4. Complete the AWS-resource and web-app roles described in steps 3 and 4.
5. Record the resulting space ID and its account/Region in the L5 provider binding
   or, until that adapter exists, a private configuration record.

Use the dedicated [Supported Regions][regions] page as the current authority;
other guide pages can lag it. The guide currently places release management in
`us-east-1` during preview. The Agent Space Region is also its data-residency
choice. AWS can discover resources across Regions in associated accounts; an
ADP connection's default Region does not restrict that access.

**Done when:** the intended space exists once, with recorded ownership, Region
and encryption choice. See [Creating an Agent Space][create].

## 3. Grant AWS resource access and associate the accounts

The Agent Space resource role lets the **AWS service** inspect infrastructure.
It is different from the web-app operator role and from ADP's existing account
connection role.

In **Capabilities → Cloud → Primary source**, configure an appropriate role in
the account hosting the space. AWS supports auto-creating a role, selecting an
existing compatible role, or using its policy template. The documented starting
point is `AIDevOpsAgentAccessPolicy`, with any required Resource Explorer
service-linked-role permission. Review the current generated policy against the
selected application's scope.

The role trust uses `aidevops.amazonaws.com` and confused-deputy protection with
the monitoring account and Agent Space ARN. An ADP connection role may trust a
completely different caller; its presence is not proof that DevOps Agent can
assume it. Associate the primary account using `accountType: monitor`.

For each additional workload account:

1. In **Capabilities → Cloud → Secondary sources → Add**, obtain the configuration
   for the selected Agent Space.
2. Create/configure the resource role in that workload account. Its service trust
   is constrained by the **primary** account and Agent Space identity.
3. Attach the necessary resource permissions, then associate it as `accountType: source`.
4. Verify the connection is **Active** and that expected resources are discoverable.

AWS documents a special setup-time `iam:PassRole` check for secondary accounts:
its resource check uses the primary account's `role/*`, rather than an individual
role ARN. Follow the [current secondary-account guide][accounts], including
`iam:PassedToService: aidevops.amazonaws.com`; keep this setup authority separate
from ordinary operator access.

For Terraform-created resources, verify Resource Explorer and resource tags in
addition to CloudFormation discovery. Include supporting resources needed to
explain failures, such as load balancers, networking, databases and log groups.
The [topology][topology] is useful context, but is **not** an authorization filter.
Review effective permissions and service-specific support for resource, tag and
Region restrictions using the [access-boundary guide][boundaries].

**Done when:** every intended account has a verified association; the agent can
read required resources and cannot read resources outside the chosen policy.

## 4. Configure people and the web-app authentication path

Configure the separate **operator app role**, then enable the web app. AWS's
standard operator-role policy is `AIDevOpsOperatorAppAccessPolicy`. Inspect its
current actions and scope: reading the UI, starting investigations, managing
knowledge and administering the space are different authorities.

Choose the appropriate supported authentication path:

| Path | Setup inputs and verification |
| --- | --- |
| IAM | Compatible operator role and permission to launch the chosen space from the console. Verify **Operator access** opens the intended space. |
| IAM Identity Center | The intended Identity Center instance and explicit user/group assignments. Follow the [Identity Center setup][idc]. |
| External OIDC provider | AWS's guide covers Okta and Microsoft Entra ID. Configure issuer, client, protected client secret, required grants and the exact callback returned by AWS; configure assignments in the IdP. See [external IdP setup][idp]. |
| ADP-authenticated launch | The [ADP SSO proof, PR #8817](https://github.com/aws-e/adp/pull/8817), demonstrates IAM federation after checking ADP Product access. It is a prototype, not the deployed Product-to-L5 identity adapter or proof of multi-user revocation/isolation. |

ADP team membership supplies an access-policy input; it does not create AWS
assignments. A GitHub login to ADP does not register GitHub repositories with
DevOps Agent. Do not treat a custom ADP/Cognito issuer as an AWS-supported OIDC
provider merely because the SDK accepts a provider string.

For ADP-authenticated launch, follow the [R3 Human access contract](../architecture.md#human-access):
the Product tab uses the existing authenticated L5 `launchNativeBrowser` API.
Do not provision a Lambda Function URL or separate public launcher. The service
checks current membership, action permission and binding scope before scoped
AWS federation. The POC's CloudFront/Lambda path is historical test infrastructure.

**Done when:** intended people can reach the correct space with their intended
permissions, unauthorized people are denied, and the chosen session/revocation
behavior is recorded. An admin's successful login alone does not qualify team access.

## 5. Add EKS access for Kubernetes workloads

For an EKS-hosted product, account association alone is insufficient for
Kubernetes API access. Follow [AWS EKS access setup][eks] for each in-scope cluster:

1. Confirm the cluster authentication mode includes the EKS API. If a change is
   needed, plan it with the cluster owner under the existing change process.
2. Open **EKS → Cluster → Access → Create access entry**.
3. Use the resource-role ARN shown for the relevant DevOps cloud source. For
   additional accounts, verify which associated role accesses that cluster.
4. Associate `AmazonAIOpsAssistantPolicy`. Select the intended namespaces where
   appropriate, or cluster scope where justified.
5. Check the network/access prerequisites for the cluster and test a read-only
   request such as listing pods or recent events in the selected namespace.

ADP's current AWS environment discovery supplies account/Region/role metadata;
it does not enumerate these cluster access entries or namespace selections.

**Done when:** the agent can retrieve the selected cluster's pod status, events
and logs with the intended scope. Preserve the failing request if it cannot.

## 6. Register GitHub and connect the product's repositories

AWS's [GitHub integration][github] has **two levels**: register GitHub as a
capability provider, then associate selected repositories with the Agent Space.
ADP's GitHub installation and AWS's registration are separate objects.

1. Open the space's **Capabilities → Pipeline → Add → GitHub**. If necessary,
   register the account/organization through the provider setup.
2. Choose the connection type. Prefer the GitHub App path for event-driven use.
   Select **Read-only** for initial code/deployment observation. Select additional
   write permissions only for features that need comments, fixes or workflow execution.
3. Authorize/install the app on the intended organization or user, choosing
   **Only select repositories** where appropriate.
4. Return to AWS, select the registered provider, and connect the exact repositories.
   Record AWS service/association IDs alongside repository native IDs and owner.
5. Inspect **Code Review and Automated Testing** settings before completing setup.
   AWS documents these capabilities as enabled by default when connecting repositories.
   Disable automatic change review/verification for an observation-only setup;
   otherwise deliberately choose filters and a verification environment.
6. Check repository code, workflow/deployment context and the required event flow.

PAT-based registration is available, but it does **not** receive GitHub webhooks;
automatic PR/deployment-driven features must not be assumed to work with it.
Automatic release-readiness PR/MR triggers are currently restricted to **private**
repositories. A public open-source repository remains useful for supported
on-demand analysis; it does not prove automatic review coverage. See
[release readiness][reviews] for filters and limitations.

**Done when:** the selected repository is an Agent Space association, required
code/workflow data is readable, and its automation settings match the chosen scope.

## 7. Connect telemetry, incident systems and private tools

CloudWatch needs no separate provider registration once the AWS account roles
have the required permissions. Verify actual logs, metrics and relevant time
ranges; merely seeing a CloudWatch service in the account is insufficient.

For external telemetry, choose the relevant guide from [telemetry sources][telemetry]
(Datadog, Dynatrace, Grafana, New Relic, Splunk, or a supported custom integration):

1. Register the provider with its correct endpoint/tenant and authentication.
2. Associate it with the intended Agent Space and select its applicable scope.
3. Store credentials in the designated provider/secret system; Product Profile
   holds references and safe metadata, never the secret value.
4. Verify a known log/metric query and, separately, incident delivery if needed.

For S3-hosted logs or documents, grant the resource role access to the specific
buckets/objects (`s3:ListBucket`, `s3:GetObject`, and applicable KMS permissions).
Do not infer S3 object access from the standard resource policy.

Connect Slack, Teams, ServiceNow or PagerDuty only for the selected operational
workflow using their [ticketing/chat guides][communications]. Record destination,
permissions and expected message/update behavior. A Product support-channel
reference does not authorize posting to it.

For custom MCP sources, AWS requires **Streamable HTTP** and supported server
authentication. Register the server, associate the selected tools, and allowlist
only the required read-only tools. For private endpoints, configure the documented
[private connection][private] including routing to any OAuth token endpoint.
**AWS calling an external MCP server** and **an ADP agent calling AWS's MCP server**
are opposite integrations with different credentials and access controls; see
[external MCP setup][mcp-in] and [AWS MCP access][mcp-out].

**Done when:** the selected integrations return real, authorized data. A provider
registration without a space association is not enough.

## 8. Publish useful product knowledge and instructions

Use the profile's purpose, architecture, environment, runbook and policy references
to identify authoritative content. Resolve the authorized revision and decide what
is appropriate to publish into this shared Agent Space. References alone do not
transfer document content or populate AWS knowledge.

For standing instructions, open **Operator Web App → Agents → Managed agents →
selected agent → Agent definition**. Configure global instructions and, where
needed, agent-specific instructions. AWS stores these as `AGENTS.md`; they apply
to newly started sessions. The current guide places instructions on **Agents**,
not the earlier Knowledge/Instructions screen. Keep always-on guidance focused
(the documented hard limit is 25 KB). Include:

- Product purpose, service/environment boundaries and useful component names.
- Evidence requirements: cite resource, Region, time range and deployment/commit.
- Production constraints, escalation destination and known diagnostic limitations.

Publish specialized diagnosis/runbook procedures as [skills][skills]:

1. Open **Knowledge → Skills** and create or upload the skill.
2. Supply its name, a description of when it applies, and the Markdown instructions.
   ZIP uploads require a `SKILL.md` with frontmatter; supported supplementary
   content is non-executable documentation/data.
3. Select the applicable agent scope and choose Active or Inactive deliberately.
4. Record the source reference/revision and returned provider asset/version.

Do not overwrite existing global instructions
without reviewing and combining their applicable content.

Review [managed memories][memories], learned skills and the [topology][topology]
after discovery. These are provider-owned results, not automatically authoritative
Product Profile updates. Any ADP Context publication needs its own scoped adapter,
provenance and freshness checks.

**Done when:** a new session can use the intended instructions/skills, and the
operator can identify their source revision. A sample inactive skill is not
product-specific knowledge.

## 9. Configure triggers and recurring work deliberately

Qualify a manual investigation before relying on automatic incident routing.
For an external incident source, use a supported integration webhook or configure
**Capabilities → Agent Space Webhook → Add webhook**. AWS supports generic HMAC
and API-key webhooks; select the mechanism supported by the sender and follow the
[webhook guide][webhooks] for the exact signed payload and event schema.

Store the one-time secret immediately in the approved secret store; do not put it
in Profile, GitHub documentation or ordinary logs. IaC-created webhooks may require
the documented rotation step to obtain a usable secret. Rotation also requires
updating the sender.

Record severity filters, environment routing, incident correlation/deduplication,
maintenance exclusions, automatic review/testing settings and the ability to pause
work. Inspect **Improvements** as well: AWS documents weekly proactive evaluations
by default, with pause and manual-run controls. See [proactive prevention][prevention].
Do not infer that an empty custom-trigger list means all native automation is off.

Configure [EventBridge status events][events] if ADP or the operations team needs
notifications. AWS emits lifecycle events to the default event bus; an outbound
status rule is not the inbound incident trigger. Keep native investigation IDs
and state, and recover missed events through provider readback when implementing
an ADP adapter. The documented events cover investigations and mitigations, not
custom agent work or proactive evaluations. Reusing the Security service's polling schedule is not a DevOps
configuration requirement.

**Done when:** a controlled test event reaches exactly the intended space and
produces a traceable investigation; pause/filter behavior works as configured.

## 10. Verify operational usefulness

Run the tests appropriate to the enabled capability. Record actual evidence,
including failures, instead of marking the service ready from a profile percentage.
Starting investigations or release tests exercises provider execution and cost;
use the selected environment and existing execution/budget authority.

| Test | Evidence needed |
| --- | --- |
| Sign-in | Correct space and intended identity; unauthorized person cannot launch. |
| AWS discovery | Known application resources and dependencies in the expected accounts/Regions; missing resources explained. |
| Telemetry | A known log entry/metric from a selected time range, with its source. |
| EKS, when applicable | Known pods/events/logs in the intended namespace; excluded scope remains inaccessible. |
| Code/deployment | Correct repository, commit and workflow/deployment record; no invented correlation. |
| Diagnosis | A known nonproduction failure or recorded incident; explanation supported by telemetry and deployment evidence. |
| Trigger, when enabled | Sender event ID maps to the native investigation; routing and pause/filter behavior match configuration. |
| Knowledge | A new session uses the intended instruction/skill revision; sample assets are distinguishable from product assets. |
| Release features, when enabled | Eligible repository event or on-demand request, target verification environment, actual findings/test results and expected posting behavior. |

AWS provides a [test environment guide][test-env] if a disposable fixture is
appropriate. Do not introduce an outage into a shared environment as a setup check.

**Done when:** evidence supports each enabled capability. Record capability-specific
readiness; an unavailable optional integration need not block an otherwise useful
manual investigation service.

## 11. Record the configuration and keep it current

Record account, Region, space ID, source/association IDs, role and secret references,
chosen features, trigger settings, provider asset revisions and validation evidence
in the service configuration record. Preserve the source Profile revision and
explicit overrides. Use the [mapping table](product-profile-mapping.md) to keep
Product-owned information separate from provider state.

At the reviewed ADP revision, Product's Context/L5 handoff returns
`integration_deferred`. The Security-oriented shared L5 architecture is not a
working DevOps setup API. Until the DevOps adapter exists, maintain the provider
configuration and private setup receipt; do not invent an `adp devops configure`
command or record a successful Product handoff that did not occur.

Review changes to membership, source access, account roles, repositories,
secrets, trigger scope and published knowledge. Monitor provider usage and errors
using [vended logs/metrics][metrics] (`AWS/AIDevOps`, dimension `AgentSpaceUUID`).
Profile financial allocations and ADP run limits do not automatically cap work
started directly in the native UI or by native triggers.

For retirement, stop incoming triggers and retain required evidence first. Follow
[AWS's deletion order][delete]: remove the Agent Space before deleting IAM roles
still referenced by it; check shared integrations before deregistering them.
Deleting the space makes its history immediately inaccessible and is irreversible.

## Source and verification notes

The specific AWS pages below were read for this review. Requirements and UI labels
can evolve; use their current versions when executing the procedure. The
[CLI appendix](cli-reference.md) distinguishes commands supported by the reviewed
SDK from settings that still need the console/provider workflow. Examples and
local links were checked offline; this publication does not certify a live setup.

[create]: https://docs.aws.amazon.com/devopsagent/latest/userguide/getting-started-with-aws-devops-agent-creating-an-agent-space.html
[regions]: https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-supported-regions.html
[accounts]: https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-multiple-aws-accounts.html
[boundaries]: https://docs.aws.amazon.com/devopsagent/latest/userguide/aws-devops-agent-security-limiting-agent-access-in-an-aws-account.html
[eks]: https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-aws-eks-access-setup.html
[topology]: https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-what-is-a-devops-agent-topology.html
[idc]: https://docs.aws.amazon.com/devopsagent/latest/userguide/aws-devops-agent-security-setting-up-iam-identity-center-authentication.html
[idp]: https://docs.aws.amazon.com/devopsagent/latest/userguide/aws-devops-agent-security-setting-up-external-identity-provider-idp-authentication.html
[github]: https://docs.aws.amazon.com/devopsagent/latest/userguide/connecting-to-cicd-pipelines-connecting-github.html
[reviews]: https://docs.aws.amazon.com/devopsagent/latest/userguide/release-management-release-readiness-code-review.html
[telemetry]: https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-telemetry-sources-index.html
[communications]: https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-to-ticketing-and-chat-index.html
[private]: https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-to-privately-hosted-tools.html
[mcp-in]: https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-mcp-servers.html
[mcp-out]: https://docs.aws.amazon.com/devopsagent/latest/userguide/connecting-to-devops-agent-remote-servers-connecting-to-the-devops-agent-mcp-server.html
[skills]: https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-devops-agent-skills.html
[memories]: https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-devops-agent-memories.html
[webhooks]: https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-invoking-devops-agent-through-webhook.html
[prevention]: https://docs.aws.amazon.com/devopsagent/latest/userguide/production-operations-proactive-incident-prevention.html
[events]: https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-integrating-devops-agent-into-event-driven-applications-using-amazon-eventbridge-index.html
[test-env]: https://docs.aws.amazon.com/devopsagent/latest/userguide/getting-started-with-aws-devops-agent-creating-a-test-environment.html
[metrics]: https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-vended-logs-and-metrics.html
[delete]: https://docs.aws.amazon.com/devopsagent/latest/userguide/deleting-an-agent-space.html


## Verified access policy readback

`GET /v1/products/{product_id}/projects/{project_id}/services/devops/bindings/{binding_id}/access-policy`
returns the current verified association and effective permission document, together
with a complete `policy_ref` suitable for `devops.access_policy_ref`. The reference
revision is a SHA-256 content address of the exact retained observation: selected
space, binding generation, resource selection, IAM trust, managed policy versions,
inline policies, permission boundary, association, and observation expiry. A caller
must use the returned reference; a fabricated Product source binding is not a policy.

Reads and configuration selection recheck current Product membership, Product and
Project archival state, selected sources, connection ownership and verified secret
versions. Changed, incomplete, expired or revoked evidence is rejected. An owner
can still edit or refresh the configuration to replace or remove an expired policy;
new work cannot bypass the policy checks. Readback does not change provider resources
or promote `verifying` evidence to native readiness. Existing observations without
the full retained selection and policy snapshot must be verified again.
