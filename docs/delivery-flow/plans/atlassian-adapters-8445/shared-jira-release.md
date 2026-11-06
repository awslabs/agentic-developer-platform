# Release and verify shared Jira connections

Shared Jira is an administrator-managed Product connection, not a personal Jira
login. The two supported identities are an installed Forge app and an
organization-managed service account with a scoped Rovo MCP API key. Both use
`atlassian___get_project_context` through the existing adapter. Ordinary Product
members need neither a personal API token nor a manually configured MCP server.
The [Forge app instructions](../../../../modules/tools/atlassian-forge/README.md)
and [identity boundaries](../../../tools/atlassian-mcp/shared-connection-methods.md)
provide the setup details.

As of 2026-11-01, the implementation in [#9278](https://github.com/aws-e/adp/pull/9278)
and its [provider-schema repair](https://github.com/aws-e/adp/pull/9427) have
merged. Gateway candidate smoke has been reported, but the adapter served by the
target has not yet been verified as the matching candidate. This is **not** a
compatible deployed release or an installed-client acceptance result. The Forge
owner credentials and installation and a real organization-managed key are
still outstanding for this flow. Keep each dependent live result pending.

## Administrators and members

| Forge app role | Action | Required observation |
| --- | --- | --- |
| App owner | Register in the intended Developer Space; prepare the environment-specific remote, deploy the app and retain its installation link. Keep owner credentials in protected secrets. | Confirm the registered app ID, environment UUID and published version independently; a build alone does not install the app. |
| Jira site admin | Install the app on the intended site, grant its Jira scopes, then enter the one-use ADP connection code on the app's Configure page. | Observe the installed site and successful pairing, not merely a deployment receipt. |
| ADP admin | Configure the same app ID, environment UUID and installation link in the gateway; choose **Install ADP app**, select Products and create the connection code. | Verify backend capability and connection readiness; do not advertise the Forge option without deployed configuration. |
| Product member | Select an allowed Jira project or board as a Product source, then use the installed CLI. | Observe real project/issue reads in the allowed Product; record unsupported board views explicitly. |

| Service-account role | Action | Required observation |
| --- | --- | --- |
| Atlassian organization admin | Create a dedicated service account with intended Jira access, enable Rovo MCP API-key authentication and issue a scoped key with an expiry. | Confirm the account's grants; neither personal OAuth nor a Forge token is a service-account key. |
| ADP admin | Choose **Connect service account**; enter the Jira Cloud site ID, key, UTC expiry and allowed Products, then **Verify and save**. Replace before expiry. | Verify project discovery and issue search with that account before sharing sources. Keep the key out of public records. |
| Product member | Select a permitted source and read it with `atlassian___get_project_context` in the installed CLI. | Check the actual account and source, supported board views and a continuation page where real data permits. No member key entry. |

Both methods retain existing Product membership, source and tool-policy checks.
Revoke a connection or Product access and confirm new reads are denied; never
silently fall back to another shared connection or a personal OAuth grant.

## Compatible release and rollback

Use an authorized reviewed `main` revision, the existing protected workflows
and private target configuration. Do not treat a green workflow, source merge or
Forge publication as independent evidence of what users are served.

| Stage | Exact entrypoint and trigger | Independent check before advancing |
| --- | --- | --- |
| Code qualification | **Tools CI** runs on PRs touching `modules/tools/**` (including the Forge README) or by `workflow_dispatch`; check its `forge` and `verify` jobs. | Build/tests and contract checks pass for the reviewed source; no target is changed by CI. |
| Gateway configuration | **Gateway Infra Plan** and **Gateway Infra Apply** use `workflow_dispatch` on `main`; Apply requires `reviewed_source_sha`. **Gateway Deploy** runs on matching code pushes to `main` or by `workflow_dispatch`, not for these prose-only changes. | Privately match `ADP_ATLASSIAN_FORGE_CONFIG_JSON` to the published app, and read back gateway settings, migration state and the served candidate. An absent Forge setting leaves the method unconfigured. |
| Forge publication | **Deploy Atlassian Forge app** uses `workflow_dispatch` on `main` with `environment: dev` or `prod`; the protected tools environment supplies owner secrets and app/environment settings. | Verify published app and remote configuration, then installation and pairing separately. Do not trigger without the owner credentials. |
| Adapter release | **Deploy tools platform** uses `workflow_dispatch` on `main` with `mode: release`, `stage: tool-adapters`, `adapter_action: foundation` and the selected environment; the guarded `handoff` and `rollback` actions use the same workflow. | Independently match gateway/adapter release binding, route readiness, prior artifacts and all four connector destinations. Require Identity/provider grants and connector parity before handoff; fence the group until every destination is read back. |
| Live checks | **Tools live evaluation** uses `workflow_dispatch` on `main` with its required source, definition, gateway revision and correlation inputs. | Read real Jira data through the installed CLI for each available identity and record refresh, revocation and cleanup. Missing fixtures or credentials stay pending, not passed. |

On an interrupted apply, resume with the recorded release identity and inspect
the pending-work ledger before repeating any action. If the candidate cannot be
verified, use the protected `rollback` action to restore **and read back** the
previous complete route group before reopening admission. Preserve the prior
configuration, credential references, operation generations, pending work and
unrelated tools; reconcile unknown writes rather than replaying them. The
foundation stage alone does not move legacy routes. Actual protected handoff,
rollback and installed-client verification belong to the subsequent combined
acceptance stages, not this documentation change.

| Evidence item | State for this guide |
| --- | --- |
| Reviewed Forge/service-account source and provider-schema repair | Merged; does not establish served revision. |
| Compatible gateway, adapter, UI/CLI, Identity and four-route readback | Pending protected release and independent target observation. |
| Real Forge app-owner credentials, publication, installation and refreshed token | Pending owner setup and live observation. |
| Real organization-managed key, discovery and issue search | Pending organization admin setup and live observation. |
| Installed CLI reads, revocation, interruption and exact group rollback | Pending final campaign; preserve separate evidence and cleanup for each case. |
