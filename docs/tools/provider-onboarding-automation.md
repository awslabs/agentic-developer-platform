# Provider onboarding automation

Reviewed against vendor documentation on 2026-10-19. ADP login and permission to
use an external service are separate: a user can sign in to ADP with GitHub and
then authorize Jira or Slack. No Entra ID integration is needed for this flow.

| Service | What ADP automates | One-time app owner action | Individual user action |
| --- | --- | --- | --- |
| GitHub repositories | Existing GitHub App manifest, registration and callback | Approve creation and installation on the chosen account/organization | Use repositories allowed by the installation and ADP policy |
| Jira Cloud | Provider registration, exact callback generation, consent request, account/site verification and managed token renewal | Create a 3LO app in the developer console; add API permissions, enable sharing, enter credentials in Connections and save the returned callback | Connect, select the intended site(s) and approve requested permissions |
| Slack | App creation from a manifest, credential storage, provider registration, callback update/readback and account verification | Generate a workspace app configuration access token and enter it in Connections; obtain app approval if workspace policy requires it | Connect and approve personal account permissions |

The person creating a Jira app does not inherently need to be a Jira site
administrator. Organization policies can restrict app use and require approval.
Slack similarly applies the workspace's app creation and approval policy. ADP
requires its own human platform administrator to register the shared provider
app; ordinary users only connect their accounts. An administrator's registration
does not grant another user's consent.

## Supported automation

Slack documents both [manifest links and manifest APIs](https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests/).
The configuration access token is scoped to an owner and workspace and expires
after 12 hours. It is a setup credential, not a personal token used by agents.
ADP does not store it or its refresh token. The
[create endpoint](https://docs.slack.dev/reference/methods/apps.manifest.create/)
returns the generated app credentials, allowing ADP to register them without
copying them through a browser. The
[update endpoint](https://docs.slack.dev/reference/methods/apps.manifest.update/)
replaces the whole manifest, so ADP exports the current configuration first and
adds only its callback. It never uses this path to take over an existing app.

Slack's [user-specific OAuth flow](https://docs.slack.dev/authentication/installing-with-oauth/)
uses `/oauth/v2_user/authorize` and
[`oauth.v2.user.access`](https://docs.slack.dev/reference/methods/oauth.v2.user.access/).
It returns a user access token at the standard top-level location. The older
mixed bot/user flow puts user tokens under `authed_user`, which is not a safe
assumption for a generic OAuth client. New ADP registrations explicitly use the
user-only endpoints, and verification refuses bot accounts.

Atlassian's [Jira Cloud 3LO guide](https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/)
documents creating apps in the developer console. This research did not find a
supported public API or a GitHub-style manifest exchange for creating a 3LO app.
The owner therefore completes the console setup once. After registration, ADP
handles the user authorization flow. `read:me` permits the independent `/me`
identity observation; accessible resources alone do not establish who connected.
`offline_access` requests refresh tokens, avoiding repeated consent while the
grant remains valid. Revocation and expired refresh access still require the
user to reconnect. Sharing must be enabled because a new app is private to its
creator by default.

AgentCore returns a [unique callback per credential provider](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-idp-slack.html).
It is available only after registration. ADP therefore configures Slack's
callback after creating the provider and displays the exact value for manual
Jira setup. It does not guess a callback or reuse ADP's browser return URL.

## Verification boundaries

Automated tests cover create interruption, safe resume, preserved app settings,
explicit reconnect, changed accounts, revoked consent, current site grants,
signed dispatch and secret-free responses. They cannot establish real vendor
approval or live qualification. Acceptance also needs an authorized app owner,
real user consent, a designated Jira issue/site and, for optional Slack writes,
the approved test-channel fixture. Live evidence must identify the actual
deployed revisions and provider results without publishing private identities.

## Protected tool activation

App registration and account verification can finish before private tool
activation. Connections reports these separately. Registration does not change
Terraform state or enable a tool. The operator prepares the existing private
edge, reviewed gateway/façade/interceptor revisions and verifier grants using
[the deployment sequence](deployment.md) and
[the private edge prerequisites](knowledge-first-enable.md).

Connections-created providers remain owned by Connections. The tools stack
accepts exact `external_oauth_provider_arns` references; it neither imports them
into Terraform nor replaces their credentials. These references must use the
selected account, region, environment and provider name. A provider cannot also
appear in `oauth_provider_secret_ids`.

For those providers, the protected tools environment accepts
`ADP_TOOLS_CONNECTIONS_CONFIG_JSON` with this shape (keep actual values private):

```json
{
  "version": 1,
  "targets": {
    "jira": {
      "version": 1,
      "gateway_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example",
      "endpoint": "https://example.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/jira/mcp",
      "vpc_id": "vpc-00000000000000001",
      "subnet_ids": ["subnet-00000000000000001", "subnet-00000000000000002"],
      "security_group_ids": ["sg-00000000000000001"],
      "routing_domain": "vpce-00000000000000001-regional.execute-api.us-east-1.vpce.amazonaws.com",
      "provider_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/adp-tools-dev-atlassian-user"
    }
  }
}
```

Use only independently read, provisioned values. Supported target keys are
`github`, `jira` and `slack`; Jira's provider name uses `atlassian`. The job
adds only the selected mediator endpoints and external provider references to
its original private inputs. Every target must retain the already configured
Knowledge edge's API, stage, VPC, subnets, security groups and regional routing
domain. Conflicting inputs and Terraform-owned providers are refused.

For first creation only, set `ADP_TOOLS_CONNECTIONS_CREATE_JSON` to the same
shape containing the explicitly approved new targets. Both the durable and
creation bindings must match the exact saved plan. Creation is limited to
`github_user[0]`, `jira_user[0]` and `slack_user[0]`, with their matching names,
DYNAMIC private MCP endpoints and execute-api IAM credentials. No native OAuth
target, alternate route, additional credential forwarding, deletion,
replacement, import or state move is permitted. Existing connection target
updates still require the durable binding and retain the existing target ID.

The optional trusted-automation network grant retains its existing
`tools_knowledge_target_creation` name and resource ownership. Its AWS permission
already selects the parent gateway and exact shared network; this change adds
no IAM actions or network resources. Its protected saved-plan validator now
requires separate explicit bindings for these connection targets. Keep the
workflow's source pin and original native smoke approval/session/fixture gates.
Do not bypass them to activate an integration.

After independent READY readback, retain the durable configuration, add the
new target IDs to the complete protected release selectors, remove the
first-create authorization, and publish the verified target settings to the
gateway. Live owner/account/site checks and the authorized read/write acceptance
matrix remain necessary before reporting the integration qualified.

The canonical gateway Deployment reads the following optional keys from the
operator-owned `adp-tools-oauth-mediators` Secret: for each of `GITHUB`, `JIRA`
and `SLACK`, `ADP_TOOLS_<SERVICE>_MEDIATOR_ENABLED`, `_MEDIATOR_URL`, `_GATEWAY_ID`,
`_TARGET_ID` and `_SOURCE_REVISION`. Absence leaves that mediator disabled.
The shared edge proof continues to come from `adp-tools-knowledge-mediator`.
Keep these keys out of other environment sources; optional Secret references
do not erase an existing ConfigMap value. Roll out pods after protected settings
change and verify their effective configuration privately. Optional Slack
posting also requires `ADP_TOOLS_SLACK_POST_ENABLED` and the original approved
`ADP_TOOLS_SLACK_POST_FIXTURE` in the same Secret; never invent that fixture or
turn on posting as a consequence of registration.

Jira's `SCN-8076-01` through `SCN-8076-03` are executable repository integration
contracts for issue identity, denied access, refresh, provider errors and
disconnect. Their results never count as live qualification. For the separate
`SCN-8076-04` live matrix, the runner loads the operator's private
`driver_factory` and `observer` through `ADP_TOOLS_JIRA_FIXTURE_MODULE`, restricted
to the `tests.e2e.tools.private_jira_*` namespace. The module and its real
fixtures remain private. Missing or unqualified bindings block before client
execution. Run that live scenario alone, with current prices covering every
provider and model invocation, and retain independent cleanup evidence.
