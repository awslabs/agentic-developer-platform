# ADP's Jira app

This Forge app lets an administrator share Jira with ADP Products using the app's
own identity. Its Configure page pairs the installation with ADP. An hourly
Forge Remote trigger delivers refreshed app system tokens to the same backend.
The existing ADP MCP adapter performs Jira reads; the app adds no EKS container.

## App owner setup

An Atlassian app owner must register the app in the intended Developer Space and
enable the appropriate distribution. Site installation requires a Jira admin.
ADP cannot create those identities or override their organization policies.
Use an environment-specific app/remote configuration for independently deployed
ADP environments.

Install the pinned tooling with `npm ci --ignore-scripts` in this directory. The
Forge CLI reads `FORGE_EMAIL` and `FORGE_API_TOKEN` from its environment. Supply
them through a secure credential mechanism; do not commit them, include them in
a command argument, or paste them into an issue or chat. The application never
uses the app owner's personal credential for Jira context reads.

For a new app, the existing **Deploy Atlassian Forge app** workflow supports
one-time registration. Set the owner credentials in the protected
`adp-tools-deploy-<environment>` GitHub environment, then dispatch from `main`
with `register_app=true`, the owner's `developer_space_id`, and an `app_name`.
This registers the app for that owner and accepts Forge's registration terms.
It refuses a configured app ID and refuses a rerun of a registration attempt.
The registration artifact preserves the new app ID in private S3 evidence for
90 days even if publication later fails. Retrieve it from the location in the
workflow summary using authorized AWS access. Save that ID as
`ADP_ATLASSIAN_FORGE_APP_ID`; all subsequent releases use
`register_app=false`. If registration has an uncertain result, inspect the
artifact and Developer Console before any new registration. Never assume that a
failed workflow means no app was created.

For a local registration instead, put only `adp_base_url` in a private JSON configuration and run
`prepare.py --for-registration --configuration FILE --output PRIVATE_DIRECTORY`.
This creates a copy of the manifest and UI with no app identity; it cannot
overwrite an existing app registration. In that private directory, run `npm ci
--ignore-scripts`, then `npm run forge -- register NAME --developer-space-id SPACE_ID`.
Use the actual developer space selected by the app owner. Follow Atlassian's
registration/terms prompts; registration is a one-time prerequisite. Retrieve
the registered app ID with the developer console. Publish the app to Forge's
**production** environment before enabling installation-link sharing in the
developer console. This is required even when the remote backend is ADP dev:
Forge production identifies a distributable app, not an ADP production rollout.
Use a separate app/remote configuration for each ADP deployment.

## Build and release

Create a private JSON configuration containing `app_id` and `adp_base_url` (the
HTTPS origin of the intended ADP deployment). `prepare.py` renders the template
and copies the source and lockfile into a new private directory:

```bash
python3 modules/tools/atlassian-forge/prepare.py \
  --configuration /private/forge-config.json \
  --output /private/adp-forge-build
cd /private/adp-forge-build
npm ci --ignore-scripts
npm run build
npm test
npm run forge -- deploy --non-interactive --environment production
```

For routine releases, use **Deploy Atlassian Forge app** in GitHub Actions. It
checks out `main`, builds the locked artifact and runs Forge's deployment checks.
The protected ADP tools deployment environment supplies:

| Setting | Location | Purpose |
| --- | --- | --- |
| `FORGE_EMAIL`, `FORGE_API_TOKEN` | GitHub environment secrets | Authenticate the app owner for publication only. |
| `ADP_ATLASSIAN_FORGE_APP_ID` | GitHub environment variable | Registered app identity. |
| `ADP_ATLASSIAN_FORGE_ENVIRONMENT_NAME` | GitHub environment variable | Must be `production`; defaults to it. Development/staging cannot produce the user installation link. |
| `ADP_ATLASSIAN_FORGE_INSTALL_URL` | Optional GitHub environment variable | Installation link after the app owner enables sharing. The first publication can complete before this exists. |
| `ADP_TOOLS_EVAL_GATEWAY_URL` | Existing GitHub environment variable | ADP HTTPS origin receiving Forge requests. |

The workflow validates the registered production environment using the pinned
Forge CLI, keeps Forge's deployment checks enabled, and retains a release
artifact with the app ID, discovered environment UUID, reviewed source commit
and manifest hash. When an installation link is configured, the artifact also
contains the matching gateway configuration. No publishing credential is in the
artifacts. The release artifact uses the existing private S3 evidence transport
with seven-day retention; its location is in the workflow summary. Generated
working files are removed at the end. Publication does not
install the app, grant Product access, or prove live acceptance.

After the first publication, open the app in the
[Developer Console](https://developer.atlassian.com/console/myapps/), enable the
intended sharing audience under **Distribution**, and copy its installation
link. [Atlassian's distribution instructions](https://developer.atlassian.com/platform/forge/distribute-your-apps/)
describe the site-admin permissions and sharing choices. Use the release
artifact to prepare the ADP configuration without transcribing its environment
UUID:

```bash
python3 modules/tools/atlassian-forge/release.py \
  --activation-from /private/adp-forge-release.json \
  --install-url 'https://developer.atlassian.com/console/install/your-installation-link' \
  --output /private/adp-forge-gateway-config.json
```

The resulting file contains only the `atlassian_forge` object described below.
Review it against the successful publication and intended ADP origin before
deploying it. The helper prepares configuration; it does not apply Terraform or
mark an installation ready.

## ADP environment configuration

Deploy the compatible gateway, additive migrations, IAM, interceptor, adapter and
frontend using the [existing procedure](../../../docs/adp-platform-deployment/deploy-with-agent.md).
Enable the shared-context feature through the existing adapter preparation and
catalogue publication. Preserve its matching gateway/adapter release binding.

Set the gateway Terraform variable `atlassian_forge` in private environment
configuration to an object with the registered `app_id`, Forge `environment_id`
(UUID, not its name), and `install_url`. Terraform manages the environment's
`adp-atlassian-forge` ConfigMap; the gateway deployment loads it at startup. Roll
out the gateway after updating or removing that configuration. Leaving the
variable null creates no app configuration or additional fresh-install resource.
The service-account method remains available independently.

When using the gateway infrastructure workflows, save that same object as the
protected `ADP_ATLASSIAN_FORGE_CONFIG_JSON` variable in the selected
`adp-deploy-<environment>` GitHub environment. The plan, apply and convergence
checks use it as Terraform's `atlassian_forge` input. Keep it configured for
subsequent releases; an absent value requests the unconfigured state. Its app ID
must match the app publication settings in `adp-tools-deploy-<environment>`, and
its environment UUID must identify the Forge environment named there. Direct
source upgrades retain the same object in private Terraform inputs.

## Connect a site and verify it

1. An ADP admin opens **Product → Profile setup → Connections & people → Add
   connection → Jira**. A configured app is selected by default, with **Connect
   service account** available as the alternative. Until platform setup is
   complete, the app option is disabled and explains what is pending.
   Generate a connection code and copy it with **Copy connection code**. The
   current Product is preselected; broader administration remains in Connections.
2. A Jira admin follows the installation link, approves the app's scopes, opens
   its Configure page and enters that code. This may be the same person.
3. ADP verifies the signed invocation, app, environment, site and app account,
   then proves project discovery and issue search before activating the binding.
   The code expires after 15 minutes and can be used only once.
4. Product members choose a Jira project and optional board in Product
   connections. They use the same `atlassian___get_project_context` MCP tool as
   service-account connections, without personal Jira credentials.
5. Verify actual issue reads, supported board views and a real continuation page
   through the installed CLI. Observe a subsequent hourly token delivery and
   confirm reads continue. Revoke the connection and verify new reads and late
   token deliveries cannot restore access. Use available data; a Kanban board
   without a backlog or sprints does not prove those views.

Forge app tokens are stored as bearer secrets in AgentCore Identity's key vault.
Forge issues and refreshes them; AgentCore provides protected storage and
workload-authorized retrieval. Ordinary refresh preserves the connection revision
and pagination; changing Product sharing or reconnecting invalidates prior
authority. Disconnect revokes ADP access before deleting credential providers.

The service-account option has separate acceptance: an actual organization-owned
key must pass verification and CLI reads. Personal OAuth is not evidence that
either shared machine identity works.

## Release and verification handoff

Follow the [shared Jira release guide](../../../docs/delivery-flow/plans/atlassian-adapters-8445/shared-jira-release.md)
for the compatible release, protected handoff and rollback. Publishing this app
does not enable the connection or move the four connector routes.

| Step | Release owner verifies | Evidence still needed |
| --- | --- | --- |
| Forge publication | Registered app owner, pinned environment and protected remote settings; use **Deploy Atlassian Forge app** on `main`. | App-owner credentials and an installed site are external prerequisites; a green workflow does not prove installation. |
| Organization-managed key | The admin enables scoped Rovo MCP API-key access and verifies the service account in ADP. | A real organization-owned key, discovery and search; personal OAuth is not a substitute. |
| Compatible ADP release | Apply gateway configuration and migrations, then use the protected adapter foundation/handoff on the reviewed release. | Independently read back served artifacts, configuration, all four destinations and rollback readiness before reopening traffic. |
| Installed-client acceptance | Each member selects a Product source and calls `atlassian___get_project_context` from the installed CLI. | Real reads for each configured identity, allowed board views and continuation when data permits; verify revoke and refresh separately. |

See the [connection-method design](../../../docs/tools/atlassian-mcp/shared-connection-methods.md)
for authorization boundaries and Atlassian's documented lifecycle limitations.
