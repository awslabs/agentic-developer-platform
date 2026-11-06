# Live Jira context for Products and Projects

ADP supports an installed Forge app and a service-account key under the same
Atlassian connection. This guide describes the common Product context experience
and the service-account setup path. See
[Shared Jira connection methods](shared-connection-methods.md) for the two-method
design, implementation status and additional Forge acceptance requirements.

ADP reads Jira on demand through its existing MCP tool gateway and separate
Atlassian adapter. An administrator registers a shared Atlassian service account
in AgentCore Identity and grants selected Products access. Product members select
the Jira projects and boards their agents should use. There is no scheduled
importer, Jira index, or separate synchronization service in this version.

## User experience

| Who | Action | What ADP does |
| --- | --- | --- |
| Atlassian organization admin | Enable API token authentication for Rovo MCP and create a scoped service account API key. Grant that account access to the intended Jira projects. | Atlassian owns this prerequisite. ADP cannot create an organization administrator or bypass an organization policy. |
| ADP platform admin | Open the Product → Profile setup → Connections & people → Add connection → Jira. Enter the site ID, service account token and expiry in the form that opens, or enable an existing shared connection. | Grant the new connection to this Product only; create the workload and key provider in AgentCore Identity; verify account, project discovery and Jira search through MCP. Refresh project and board choices inside the same dialog after verification. |
| Product member | Open Product connections, choose a shared connection, discover a Jira project and optionally a board, and confirm sharing. | Save the mapping under that Product and Project. All current Product members and their authorized agents can use it. |
| User or agent | Request issues, backlog, board configuration or sprints. | Recheck Product membership, source and connection; fetch a bounded live MCP response with source identity and retrieval time. |
| Admin | Replace an expiring key in the Product’s Jira section. Use Settings → Connections → Shared Jira access for Products to share an existing connection with additional Products or revoke it. | Preserve all existing Product grants when replacing a key from a Product. Verify the replacement before switching; invalidate old pagination through the connection revision; retire the old provider. |
| Product member / admin | Remove a source / revoke shared access. | Stop new reads through that mapping / connection. Provider cleanup can be retried separately after access is revoked. |

The admin grants each selected Product access to **everything the service account
can read**. Product members can then select any accessible Jira project or board.
Use a dedicated account with appropriately limited Jira permissions; separate
connections can provide different sharing boundaries. The UI states this before
saving. Jira continues to enforce the service account's own permissions.

Credential setup is visible only to ADP platform administrators. Product members
can choose sources from connections already granted to their Product without
supplying credentials. When Forge is configured, the same Product setup also
offers app pairing and refreshes the source choices after Jira verifies the app.

Existing personal Atlassian OAuth remains available for personal tools. GitHub
App creation, registration and existing connections are unchanged. Users continue
to sign into ADP through its existing authentication; Entra ID is not required.

## Identity and request path

```text
ADP CLI / authorized agent
  → tool façade (existing policy, entitlement and budget controls)
  → AgentCore Gateway + interceptor
  → Atlassian adapter MCP tool: atlassian___get_project_context
  → ADP authorization (current Product, Project and source)
  → existing private adapter endpoint, core-signed system request
  → AgentCore Identity workload token + GetResourceApiKey
  → Atlassian Rovo MCP
```

The shared identity is the **outbound Jira identity**. ADP still authenticates the
user or an existing authorized agent run and checks that user's current Product
access. It does not impersonate the Jira service account as an ADP user or create
a new unrestricted service principal. General service-root MCP admission and
scheduled background execution are outside this version.

The browser preview uses the Product context API and the same private adapter
read. Forge connections call Jira REST within that adapter; service-account
connections call Atlassian's hosted MCP. Vendor transport and Identity
key retrieval stay in the adapter image; the gateway stages only the lightweight
query schema, not the adapter package.

## Available reads

One public tool keeps discovery small. Start with `view=sources`, supplying
`product_id` and `project_id`; use a returned `source_id` for subsequent reads.

| View | Atlassian MCP operation | Scope and pagination |
| --- | --- | --- |
| `sources` | Saved ADP mappings | Current Product and Project only. |
| `issues` without a board | `searchJiraIssuesUsingJql` | ADP constructs project-constrained JQL; optional literal text or issue key filter. |
| `issues` with a board | `getJiraBoardIssueData`, dataset `all` | Board plus selected Jira project. |
| `backlog` | `getJiraBoardIssueData`, dataset `backlog` | Exact provider backlog, constrained to the selected project. Kanban boards may not support a backlog. |
| `board` | `getJiraBoardConfig` | Configuration for the selected board. |
| `sprints` | `listJiraBoardSprints` | Sprints for the selected Scrum board. |

Default page size is 20; the maximum is 50. Clients receive opaque, expiring ADP
cursors, bound to the original query, source revision and connection revision.
Each continuation rechecks current authority. A changed query, rotated connection,
removed source or revoked membership cannot reuse the old cursor.

Responses contain provider data, a source reference and retrieval time. Issue
links are retained when supplied by Atlassian. A response is not a claim that the
whole project or backlog was retrieved: continue while `next_cursor` is present.
This feature stores mappings, metadata and short-lived pagination state, without
creating a Jira content index. Existing tool receipts and agent conversations may
retain returned content under their normal retention policy.

The live provider exposes backlog selection over MCP. Its issue responses can
contain JSON text plus a second MCP-app representation (`issues.nodes` and
`issues.pageInfo`). The adapter validates their issue identities and cursor before
returning the tool data, omitting provider UI runtime context.

## Deployment and verification

Use the existing deployment workflows for ADP components. The Forge app has a
separate publication workflow described in the [app release guide](../../../modules/tools/atlassian-forge/README.md).
The feature defaults off until its compatible components are deployed. Live
acceptance with both genuine machine identities remains required.

| Stage | Deployment | Verification |
| --- | --- | --- |
| Database and core | Apply the additive migration and gateway release with the staged Jira query contract. | Existing connection/readiness regressions; secret-safe validation; current membership, rotation, unlink and pagination tests. |
| Permissions | Apply gateway and adapter foundation Terraform. | Core can register environment-scoped providers; the adapter can obtain a system workload token and retrieve keys only from that environment's provider namespace. No external secret is put in Terraform. |
| Interceptor and adapter | Deploy the existing interceptor and adapter release. Prepare adapter configuration with `enable_system_context: true`; publish the matching catalogue through the existing release binding. | Verify the catalogue includes the context tool; gateway and adapter digests match; signed request tampering and expired deadlines are rejected. |
| Frontend | Publish the existing frontend build. | Admin key form, explicit Product sharing, live preview and navigation-race tests; regression coverage for personal connections and existing Product sources. |
| Real service account | Register the administrator-created key through the secure UI. | Observe a successful account, project discovery and issue search using that actual key. A personal OAuth test does not satisfy this stage. |
| End to end | Select an accessible project/board and call the new tool through the installed CLI. | Discover sources; read issues and available board/backlog/sprint views; follow a real cursor when present; check links/time; revoke the source and prove a new read is denied. Do not invent boards, sprints or fixture issues when the account has none. |

Preparation writes the protected `ADP_TOOLS_ATLASSIAN_SYSTEM_ENABLED` binding and
catalogue digests for the gateway and adapter together. Use the documented
adapter release/rollback mechanism; do not turn the flag on manually in only one
component. Application rollback retains the additive database tables and provider
cleanup journal; destructive schema downgrade is refused. An older prepared configuration omits the flag and remains disabled.

Service-account keys expire. AgentCore securely stores the key but does not renew
it with Atlassian. ADP rejects expired connections and supports replacement in the
same screen. The admin can use **Verify again** to refresh the captured MCP schemas
without resubmitting the key after a provider schema change. Failed provisioning references remain journaled so revocation can
retry cleanup without making the candidate usable.

## References

- [Atlassian MCP API token and service account authentication](https://developer.atlassian.com/cloud/rovo-mcp/guides/configuring-authentication-via-api-token/)
- [AgentCore GetResourceApiKey](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_GetResourceApiKey.html)
- [AgentCore IAM actions and resource types](https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html)
- [Canonical ADP deployment procedure](../../adp-platform-deployment/deploy-with-agent.md)
