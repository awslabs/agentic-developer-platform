# Connections and existing credentials

Product → Profile setup → Connections & people → Add connection provides setup
inside the Product. Choose a service to open its controls in the same dialog:

| Service | Setup in the Product | External authorization |
| --- | --- | --- |
| GitHub | Use the existing GitHub App, register it if needed, and refresh repository access. | GitHub installation or app approval returns to the originating Product and reopens GitHub setup. |
| AWS account | Enter account details, launch the access-role template, then verify and save without leaving the dialog. | Create the stack in the AWS console tab, then return to verify it. |
| Jira | Administrators choose the configured ADP Forge app, service-account credentials, or an existing shared connection. Forge is the default when configured; a pending app is disabled with a setup explanation. Select the Jira project and optional board in the dialog. | A Jira administrator installs and approves the Forge app and enters ADP's connection code. Atlassian issues tokens for the service-account alternative. |
| Slack | Administrators can create/register the app using a configuration token; users then connect their own accounts. | Slack consent returns to the originating Product and reopens Slack setup. |

The dialog reports readiness from connection/source responses and keeps failures
visible for correction. Closing it refreshes the Product's available sources.
Credentials are never stored with the navigation context; callback navigation is
limited to the same user and workspace and expires after 30 minutes. Product
membership does not grant platform administration. A personal Slack or AWS
connection does not automatically share that account with Product members.

Settings → Connections remains the management page for GitHub, Jira and Slack. There is
one card per service. GitHub repository installation remains the primary GitHub
action. Optional personal profile access is inside that card. It requests only
`read:user`; it does not replace repository access from a PAT or installation.

Settings → Credentials continues to manage AWS roles and other credentials.
There is no GitHub PAT setup form or dedicated legacy-token section. GitHub,
Jira and Slack OAuth connections remain available in Connections; Product setup
reuses the same supported connection controls. Any bearer
credentials retained by another deployment use the ordinary credential list,
with agent consent, expiration and explicit removal. The general vault API and
workspace PAT grace policy are unchanged by this UI change.

## Slack app setup

A platform administrator registers one reusable Slack OAuth app, either in
Product setup or Connections. Other users see **Setup required** until the app is registered.
Failure to read provider metadata is **Unavailable**, never permission to
replace or recreate an existing app.

For personal Atlassian access, use the Atlassian consent controls in Connections.
For shared Product access, use the
[shared Jira connection methods](atlassian-mcp/shared-connection-methods.md).
The Product dialog does not require creating a legacy Jira OAuth 2.0 (3LO) app.

1. For Slack, choose automated setup and enter a short-lived **app configuration
   access token** from Slack's app settings. ADP creates the app, stores its
   client credentials, registers its provider, configures the callback and reads
   it back. The configuration token is never saved. An existing app can instead
   be registered manually; the manual section also offers a manifest link with
   the app settings prefilled.
2. For manual setup, enter the client ID and client secret in the ADP form. ADP stores them in a
   dedicated Secrets Manager secret and registers the AgentCore provider.
3. Save the exact callback URL returned by AgentCore in the vendor's app
   settings. This URL is unique to the provider and cannot be known before
   registration. It is different from ADP's browser return URL.
   Automated Slack setup performs this step.
4. Each user selects Connect and completes Slack's consent flow. ADP
   verifies the provider account before showing **Account verified**.
   Account verification does not grant workspace tool permissions.

Some organizations restrict app creation or approval. Those owner/browser steps
must be completed with an authorized vendor account. Registering an app does
not verify its client credentials, grant a user's consent, or establish live
tool qualification. Private tool activation and live qualification have separate
acceptance requirements; do not represent app registration or a mocked test as
completion of those requirements. GitHub remains ADP's login provider; Entra ID
is not required to authorize these external accounts.

Registration is create-only and audited. It never rotates, deletes or overwrites
an existing provider or the existing GitHub App/login configuration. After an
interrupted registration, refresh status first. A partially stored secret can
be reused only with identical submitted credentials. A mismatch requires an
operator to reconcile it; secrets are not silently replaced. Secret input and
provider exceptions are omitted from API errors and audit receipts. Slack
automation reserves its setup record before app creation. An uncertain create
is never automatically retried. After credentials are safely saved, registration
and callback setup can resume using the same app. Callback updates export the
current manifest first and preserve the owner's other settings. Only apps
created by this automation can be resumed through it.

Existing connections without provider verification show **Reconnect required**.
Reconnect asks for consent explicitly, retains the connection ID, and replaces
its binding only after verification. It refuses a different provider account or
a credential/binding changed since consent started. Failed verification leaves
the existing metadata intact. New permissions are never added to stored grants
by a migration. To switch provider accounts, disconnect and connect explicitly.

New Slack registrations use Slack's standard user-only authorization and token
endpoints through an AgentCore custom provider. A bot token is refused during
verification. Optional `chat:write` remains a separate user consent and posting
still requires the existing approved test-channel fixture. General Slack posting
is not enabled by this onboarding flow.

## Deployment ownership

Deploy the gateway's `identity` managed-policy update before using setup. The
service role receives scoped create/read access to the environment's three
provider names, its OAuth workload, and the dedicated external secret names:
`adp/<environment>/connections/oauth/<provider>`. Agents receive none of these
new registration permissions. Slack automation additionally needs
`secretsmanager:PutSecretValue` only for its dedicated Slack setup secret, to
record progress and save the generated app credentials. Gateway configuration must supply a valid
`BG_OAUTH_WORKLOAD_NAME` and `BG_OAUTH_RETURN_URL` for this deployment.

The UI creates the workload and providers outside tools-platform Terraform.
Do not add the same resources to Terraform state without adoption. For an
installation using Terraform to manage OAuth apps, continue that ownership:
supply private `oauth_provider_secret_ids` and `oauth_return_url`, register the
returned callbacks at the vendors, and use the UI for user consent. Providers
can be configured independently. For a new Terraform-managed Slack app, set
`slack_user_oauth = true` to use the user-only flow. This setting defaults off
to preserve existing native provider configurations until a reviewed migration.
Do not manage the same provider in two states.

Private tools can reference Connections-managed apps through
`external_oauth_provider_arns`; this does not import or modify those apps.
References must match the deployment's exact account, region, environment and
provider names and cannot also appear in `oauth_provider_secret_ids`.
Configure the corresponding `github_mediator_endpoint`, `jira_mediator_endpoint`
and `slack_mediator_endpoint` through the protected private edge. Its vendor
configuration uses `atlassian`, mapped to the `/internal/v1/tools/jira/mcp`
route. Activate each mediator only with the reviewed source revision, private
endpoint, gateway/target IDs and IAM proofs. The API reports remaining tool
setup separately from user verification, and policy/consent remains unchanged.

To adopt UI-created resources into tools-platform Terraform, first inventory
and privately back up the live provider/workload metadata. Use reviewed import
blocks for the existing `aws_bedrockagentcore_workload_identity.oauth_user[0]`
and `aws_bedrockagentcore_oauth2_credential_provider.user_connection["<provider>"]`
addresses, supplying the existing dedicated secret references. Review a saved
plan that preserves names, return URLs, client credentials and callbacks before
applying. Never recreate a provider to resolve an ownership conflict.

See [provider onboarding automation research](provider-onboarding-automation.md)
for the supported vendor mechanisms and the remaining human steps.
