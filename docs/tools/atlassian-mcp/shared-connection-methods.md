# Shared Jira access: installed app or service account

ADP offers two administrator setup methods inside the existing Atlassian
connection: **Install ADP app** and **Connect service account**. Product members
select a shared connection and its Jira sources. Agents use the same MCP tool
regardless of the administrator's choice.

The implementation includes service-account API-key access through Atlassian Rovo
MCP and Forge pairing, app token storage/refresh, Jira API transport and admin UI.
The Forge source and deployment workflow are included; a registered app-owner
account and installed app are still required for live acceptance. Service-account
OAuth is not implemented. Neither method is claimed to have passed live acceptance
using a real machine identity. A working personal OAuth grant is insufficient
evidence. See the [app setup and release guide](../../../modules/tools/atlassian-forge/README.md).

## Administrator and user experience

| Method | Administrator setup | Identity Jira sees | Token lifecycle | Upstream access |
| --- | --- | --- | --- | --- |
| Install ADP app | Install the Forge app on the intended Jira site, approve its permissions, link that installation to ADP, then select Products. | The Forge app's bot identity. | Forge issues short-lived app system tokens. The adapter must securely accept refreshed tokens before expiry. | Jira APIs through a Forge-backed adapter. Compatibility with hosted Rovo MCP is not established. |
| Connect service account | Grant a dedicated Atlassian service account the intended Jira access, enable API-token authentication for Rovo MCP, and register its scoped key and expiry in ADP. Then select Products. | The organization-managed service account. | AgentCore Identity stores the key. An admin replaces it before expiry; ADP verifies the replacement before switching. | Atlassian's hosted Rovo MCP, which documents service-account API-key authentication. |

When both methods are deployed and verified, present **Install ADP app** first.
Keep **Connect service account** as the alternative for organizations that prefer
to manage their own machine account. Before the configured app is available,
show the Forge method disabled with an explanation of the pending platform
setup. Once configured, select it first in the Product setup dialog. The service-account form
must not accept a Forge token or imply that it installs an app.

Only admins choose the setup method. Product members see the connection's label,
site, shared identity and available Jira sources. They do not need a personal Jira
credential for shared access. The existing personal Atlassian connection remains
available for tools that act on the individual user's behalf. Neither shared
method adds a generic Credentials entry or changes GitHub App setup or ADP login.

An installation and a service account may coexist, including on the same site.
Each source explicitly names one connection. ADP must never silently switch to
another connection or personal OAuth after an authentication or permission error:
that would change the identity and potentially the information being shared.
An explicit connection change revalidates its sources and invalidates old cursors.

## Shared authorization and MCP contract

Both methods reuse Product allowlists, current Product membership checks, source
selection, tool policy, entitlement and budget controls. The admin authorizes a
Product to share the information readable by the selected app or account. Jira
permissions and app scopes remain an additional boundary; Product source mappings
do not grant Jira permissions.

```text
ADP user / authorized agent
  -> existing tool facade and AgentCore Gateway
  -> atlassian___get_project_context
  -> current Product, Project, source and connection authorization
  -> existing separate Atlassian adapter
       -> service-account key from AgentCore Identity -> Rovo MCP
       -> verified Forge app system token -> Jira APIs
```

The same public MCP tool and query/response contract serve both methods. Vendor
authentication and API translation belong in the separate adapter, with no Jira
SDK added to the core gateway and no delivery-engine special cases. The browser
preview follows the same authorization and adapter path as tool requests.

The shared outbound identity does not create an unrestricted ADP service
principal. Existing ADP users and authorized agent runs still need current
Product access. General service-root execution remains outside this change.

The adapter exposes verified capabilities, including project and board discovery,
issues, board configuration, backlog and sprints. It must not infer capability
from the setup method alone. A board without a backlog or sprints reports that
limitation; API or scope restrictions must not be presented as an empty dataset.
Responses retain source attribution, retrieval time and bounded pagination.

## Forge installation and unattended access

Forge adds an application artifact and its lifecycle; it is not another API-key
form. An app owner must first register and deploy the Forge app and configure an
allowed ADP remote endpoint. An Atlassian administrator subsequently installs it
and grants access to the intended site. Installation links can be used for
eligible privately distributed apps; a Marketplace listing is not required for
that distribution path.

For independently deployed ADP environments, prepare an environment-specific
Forge app configuration and register its remote endpoint. Do not introduce a
central token relay or accept an arbitrary callback URL from a browser. App
registration and Atlassian administrator consent cannot be bypassed by ADP.

| Step | Automation and validation |
| --- | --- |
| Prepare the app | Version the manifest, required Jira scopes and remote endpoint with the adapter contract. Keep real app/environment identifiers and deployment credentials in protected configuration. |
| Pair the installation | Start from an authenticated ADP admin session. Use an expiring, single-use pairing transaction and validate the Forge Invocation Token before binding the app, installation, site and ADP tenant. Browser-supplied installation IDs alone are not proof. |
| Receive app tokens | Verify the invocation signature, audience, expiry and installation context. Use only the API base URL from the validated context, restricted to expected Atlassian origins and the bound site. Protect token-bearing headers from request logs and receipts. |
| Maintain access | Use documented Forge Remote app system tokens and direct scheduled remote invocations to receive replacements without a logged-in user. Track actual expiry and freshness. The schedule delivers credentials; it does not import Jira content. |
| Verify and activate | Prove app identity, project discovery and issue search against the bound site. Expose shared access only after those reads succeed and the admin grants Products access. |
| Disconnect or lose access | Revoke the ADP binding immediately on admin disconnect. Reject expired tokens and stop reads when upstream authorization fails. Missing scheduled deliveries indicate lost freshness, not definitive proof of uninstall. Never fall back to a different identity. |

Atlassian currently documents no uninstall lifecycle event for this remote-token
path. Do not promise immediate uninstall notification: handle provider rejection,
token expiry and missed refreshes, and show that shared access requires attention.

Reuse AgentCore Identity for supported credential storage and workload-authorized
retrieval. It is not the issuer or renewal mechanism for Forge app tokens.
The Forge token handoff verifies the invocation in the adapter before the core
stores the bearer secret in an AgentCore key provider. A bound, verified delivery
replaces that secret only when its expiry advances. Revocation and delivery lock
the same connection; late callbacks cannot restore access. Only references and
installation metadata are stored in ADP rows. Live lifecycle acceptance remains
required before claiming the app works in an environment.

## Service-account OAuth

Atlassian also documents OAuth client credentials for service accounts in its
centralized user-management experience. This could later replace manual API-key
replacement under **Connect service account**, without adding another top-level
connection choice.

Do not assume those OAuth tokens authenticate to hosted Rovo MCP. Establish that
compatibility with a real service account, or deliberately use a supported Jira
API backend through the same ADP MCP adapter. Evaluate AgentCore Identity's OAuth
provider support for that exact grant before claiming automatic token renewal.
This OAuth alternative is not part of the existing API-key implementation.

## Delivery and acceptance

The shared context and service-account setup path are described in
[Live Jira context for Products and Projects](product-project-context.md).
Forge support uses the existing adapter for its transport and token intake,
without changing the delivery engine or requiring another EKS container. Forge itself
needs a versioned app deployment step alongside the ADP releases. The separate
**Deploy Atlassian Forge app** GitHub Actions workflow publishes that artifact;
deploying the adapter image alone is insufficient. Gateway Terraform manages its
optional app configuration in every environment.

| Work | Required evidence before enabling it |
| --- | --- |
| Common connection model | Explicit method and identity metadata; additive migration for existing service-account connections; independent revisions, revocation and cleanup. No change to personal OAuth or GitHub App registration. |
| Forge release and pairing | Registered app, deployed manifest and reachable protected remote endpoint. Reject forged, expired, replayed and wrong-app/installation/tenant pairing attempts. A failed or cancelled pairing grants no Product access. |
| App token lifecycle | A real installation delivers a system token and a later automatic replacement. Reads continue through replacement without a user session. Expired tokens and disconnected installations cannot restore access through late callbacks. |
| Service account | An actual scoped service-account key passes account, discovery and search verification. Replacement and expiry behavior use that identity, not a personal OAuth substitute. |
| Common context reads | Through an installed CLI, discover sources and read issues and supported board views with each identity. Verify a real continuation page when data permits; do not invent board/backlog/sprint fixtures. Record unsupported views explicitly. |
| Permission boundaries | Revoked Product membership, source removal and connection revocation deny new reads and continuations for both methods. Errors never trigger identity fallback. |
| Setup and release | Admin choices reflect deployed capabilities. Ordinary Product members need no credential setup. Verify the frontend, API, interceptor, adapter and Forge versions together; retain the working service-account route if Forge is unavailable. |

## References

- [Forge requests as the app](https://developer.atlassian.com/platform/forge/apis-reference/fetch-api-product.requestjira/)
- [Forge Remote app system tokens and lifecycle limitations](https://developer.atlassian.com/platform/forge/remote/calling-product-apis/)
- [Scheduled remote invocations and token expiry](https://developer.atlassian.com/platform/forge/remote/scheduled-triggers/)
- [Forge app distribution and installation links](https://developer.atlassian.com/platform/forge/distribute-your-apps/)
- [Hosted MCP service-account API-key authentication](https://developer.atlassian.com/cloud/rovo-mcp/guides/configuring-authentication-via-api-token/)
- [Service-account OAuth client credentials](https://support.atlassian.com/user-management/docs/create-oauth-2-0-credential-for-service-accounts/)
