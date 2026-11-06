# Seven-provider POC amendment

Owner decision, 2026-11-01: exercise **SonarQube, Jenkins and Argo CD** through
the optional Go connector service, using disposable instances. This amends the
primary source selection in the [canonical architecture](architecture.md).
Existing ADP GitHub/Jira integrations remain compatibility paths. A second Jira
credential is not a prerequisite for demonstrating connector expansion.

Owner addition, 2026-11-04: include **GitLab, PagerDuty, Linear and Slack** in
the same optional service. Seven providers are now selected. GitLab can use an
owned existing test instance; PagerDuty, Linear and Slack require consented SaaS
test scopes. Their synthetic fixtures are not self-hosted product instances.

Status: design and implementation handoff; no new provider is live-qualified.
The generic served-service repair is [#9437](https://github.com/aws-e/adp/issues/9437).
The separate S14 implementation is [#9449](https://github.com/aws-e/adp/issues/9449);
[its story design](stories/s14.md) contains the full contract.
Original AC/SCN identities and results remain historical evidence; they do not
accept the new selection. The original 44 criteria remain required, with the
additional live expansion proof under epic EC-11.

## Scope and delivery

The primary pilot now has **seven providers, seven ADP connections and seven
selected leaf scopes**: one SonarQube project, Jenkins pipeline job, Argo CD
application, GitLab repository, PagerDuty service, Linear team and Slack channel.
Provider grouping nodes are not additional leaf scopes. Slack participation is
read-only, limited to the selected test channel; it does not authorize posting,
DM access or automatic bulk history ingestion.
Native GitHub repository/requirement/CI evidence remains available through its
existing ADP connection for lineage and regression checks; no new primary Jira
fixture is required. Extra owned fixtures for isolation and paging remain under
the original adversarial allowance. Preserve 30-day maximum lookback, two
concurrent collections, 100 records/page, five pages/30 seconds per discovery,
14-day resource expiry and the accepted financial limits. Adding providers does
not increase those limits or grant access to additional real customer sources.

Delivery order:

1. Finish and review #9437's actual gateway-to-Go authority/transport path and
   campaign recovery/evidence code. In parallel, the owner-authorized
   [local provider foundation](provider-foundation.md) implements the seven
   extracted adapters, Go HTTP fixtures, existing API dispatch and component
   staging. Reconcile the narrow shared-file overlap documented in the handoff.
2. S14 consumes the reviewed foundation and #9437 interfaces, integrates all
   seven providers with Profile types, bounded observations and campaign
   consumers, and completes review and merge using code-only delivery.
3. Bind the final merged workflow/image revisions, owned target, source consent,
   protected evidence storage and independent finalizer in the existing flow.
4. Perform one coordinated release and acceptance campaign. Prove connector-only
   onboarding first, then opt into governed historical collection. Retain the
   original >=48-hour/20-update sample, isolation, IAM renewal and recovery proof.
   At least one observed update from each selected provider must contribute;
   duplicate observations are not new updates.
5. Independent evaluation requires both the original acceptance and EC-11;
   S08 records reuse decisions and reconciles all owned resources.

No per-provider platform deployments or extra human gates are introduced. Code
delivery does not establish live acceptance. The flow amendment inserts S14
after the existing repair and before the unstarted release; it preserves passed
nodes, attempts, receipts, budget supplements and historical exposure holds.

## Inspected upstream reuse

All references below are relative to `backend/plugins/` at Apache DevLake
`6502e549f58eb40c40ccbcd39aab9bc8eea6616f` (`v1.0.3-beta18`), already pinned in
`modules/connectors/devlake/source-lock.json`.

| Provider | Existing code to extract | Required ADP adaptation |
|---|---|---|
| SonarQube | `sonarqube/api/remote_api.go`: `querySonarqubeProjects`, `listSonarqubeRemoteScopes`; `components/search_projects` with `p`/`ps` paging | Inject broker transport, retain project key and observed revision; qualify the actual server version and Browse-only access. The endpoint is not a promise that every SonarQube release is compatible. |
| Jenkins | `jenkins/api/remote_api.go`: `listJenkinsRemoteScopes`, `isGroup`; `jenkins/api/jobs.go`: `GetJobsPage` | Preserve folder traversal and full job path; enforce page/deadline bounds and registered base URL. Same job names in different folders cannot collide. |
| Argo CD | `argocd/api/remote_api.go`: `listArgocdRemoteScopes`; `argocd/tasks/application_collector.go`, `sync_operation_collector.go` | Upstream enumerates `applications`, groups by project and has no real server pagination. Bound response bytes/count before decoding; return explicit partial/unsupported coverage on overflow. Stable ordering and namespace/project/application identity are required; upstream name-only scope IDs and Go map order are insufficient. |
| GitLab | `gitlab/api/remote_api.go`: group/project listing helpers | Preserve native repository IDs and full namespace paths. Extract separately bounded group/project reads with explicit stage/page progression; propagate errors rather than panicking. Root listing enumerates groups; an authorized personal namespace is selected explicitly from the registered identity. |
| PagerDuty | `pagerduty/api/remote_api.go`: `queryPagerdutyRemoteScopes` | Discover incident services through the registered endpoint; validate offset/limit/more and reject non-progressing pagination. Service IDs are connection-scoped. |
| Linear | `linear/api/remote_api.go`: teams GraphQL discovery | Inject transport for a fixed read-only teams query with bounded page size; validate GraphQL errors and opaque cursor progression. No arbitrary GraphQL operations or mutations. |
| Slack | `slack/api/remote_api.go`: channel discovery | Preserve channel IDs and opaque cursors. Missing permission must fail or be explicit partial coverage; never silently retry private-only and claim a complete inventory. No message sending. |

Expose these narrow functions in versioned extraction patches. Keep Apache
license/NOTICE, patch hashes and source provenance. Inject ADP's authorized
`plugin.ApiClient`; do not import plugin initialization or DevLake's connection
database merely to perform discovery. Reuse provider logic, not upstream
identity, secret storage or authorization. S14's audit must distinguish extracted
upstream logic from ADP adapters and new fixes.

## Served contract and meaningful observations

Use the gateway's existing durable discovery operation, then the separate Go
service's versioned discovery API. #9437 owns the final served request/context
schema; S14 consumes it and extends the provider allowlist. Connection endpoints
and credentials come from current ADP registration, never an arbitrary URL in a
discovery request. Opaque cursors bind provider, connection revision, namespace,
operation and generation. Recheck authority before both provider access and
Profile admission. A service identity alone supplies no user delegation.

The adapter-to-served mapping is:

| Provider key | Leaf scope kind | Grouping or identity |
|---|---|---|
| `sonarqube` | `quality_project` | Native project key |
| `jenkins` | `ci_job` | Full folder/job path |
| `argocd` | `deployment_application` | Project/namespace/application |
| `gitlab` | `repository` | Native project ID; `namespace` groups |
| `pagerduty` | `incident_service` | Native service ID |
| `linear` | `tracker_team` | Native team ID |
| `slack` | `communication_channel` | Native channel ID |

These types are implemented in the adapter and existing Go discovery dispatch;
S14 still integrates their served Gateway/Profile contract. Update schema,
capability discovery, served UI and installed CLI together. Preserve typed
provenance and saved user decisions. Unsupported versions/capabilities stay
explicit. The master flag gates all seven at gateway, Go and broker; collection
remains separately disabled by default. Stopping Go must fail a selected
DevLake operation with no Python/native fallback. Connector-only success must
not require an Insights grant, cell database or evidence store.

Adapter page/offset/cursor values are internal provider continuation data, not
durable ADP authorization. The caller binds them to the registered connection,
selected namespace, tenant, operation, generation and current authority, and
enforces the aggregate discovery deadline/page limit. Cursor progress, repeated
pages and provider errors must be tested independently of that served binding.

| Source | Bounded facts to demonstrate | Independent oracle and limitation |
|---|---|---|
| SonarQube | Project identity, completed analysis/revision, selected quality measures and quality-gate status | Provider API readback for the owned analysis; missing revision or coverage remains unknown. Do not invent commit linkage or equate quality gate with acceptance. |
| Jenkins | Full job path, build number, result, start/duration, reported SCM commit and artifact reference | Provider build API and generated artifact manifest. A build number is not globally unique and an archive is not a deployment. |
| Argo CD | Project/namespace/application, reported revision, sync operation/status and application health | Provider API plus independent Kubernetes readback of the owned workload/revision. A sync record alone does not prove successful deployment. |
| GitLab | Repository identity, merge-request state/revision and pipeline status/ref/SHA | Native project/MR/pipeline readback. A pipeline success is not deployment or requirement acceptance; absent joins remain unknown. |
| PagerDuty | Incident ID, selected service, status, urgency and reported times | Selected test service's incident API. An incident alone does not establish change failure or a causal deployment link. |
| Linear | Issue ID/identifier, selected team, workflow state and reported times | Selected test team's issue query. Workflow state is not an independent acceptance result. |
| Slack | Selected channel and exact native message/thread timestamps, type and available text | Readback limited to the consented test channel. Preserve timestamp strings exactly; a message is not requirement approval or completion. Retention and field admission follow current source policy. |

Collection reuses [DevLake's normalized domain models and converters](normalization-reuse.md),
including their raw/tool/domain task chain. Do not add a parallel generic `Fact`
schema. ADP source-record metadata and lineage wrap upstream normalization.
Slack has no upstream domain converter at the pin; retain tool-layer messages
and exact native timestamp strings, scoped by the authorized channel request.

Historical collection of these facts uses explicit current source grants and
the existing tenant/Project/policy cell boundary. Preserve the six-field evidence
identity, native records and mapping revisions through normalization and export.
Link facts by actual repository/commit/artifact identifiers where available;
missing or ambiguous joins remain missing/ambiguous. Native requirement/test
evidence still supplies the acceptance chain. This amendment does not replace it
with SonarQube scores, build success or Argo CD sync status.

## Test sources and ownership

All seven providers have Go HTTP/GraphQL fixtures consumed directly by adapter
tests. These require no Docker, Kubernetes or live credentials. Keep one test
path; do not add a separate HTTP simulator or reusable tool-lab lifecycle engine.

For live qualification, reuse the existing POC operations workflow and ownership
ledger/finalizer. Add only the needed declarative manifests and small seed/readback
steps for disposable SonarQube/Jenkins/Argo CD. GitLab uses an owned test source;
PagerDuty, Linear and Slack require consented SaaS test scopes. Missing live access
blocks those checks, not independent code implementation. No test-source writes
are performed by the connector reader, including Slack posting.

The private campaign manifest records exact source/resource IDs, pinned tested
image/plugin versions, reader credential references, expiry and cleanup owner.
Bootstrap rights stay separate from broker reader rights. Do not use `latest`,
mount a Docker socket in Jenkins, borrow another task's cluster, or change the
user's current Kubernetes context. On a shared devbox, qualify headroom before
creation and retain the 2 CPUs/8 GiB RAM/20 GiB disk aggregate ceiling. Pass the
owned cluster's kubeconfig explicitly; use private endpoints and authenticated
provider APIs for broker access. The source setup is part of the one campaign,
not a per-provider platform deployment.

The hosted gateway cannot be assumed to reach loopback fixtures. Live admission
must choose a reachable, owned target and prove broker connectivity. Prefer the
same fixture images in one dedicated namespace on the verified approved target,
with private endpoints and restricted egress, during the coordinated campaign.
Until exact target, permissions and reviewed plan are bound, this is a deployment
prerequisite, not a missing user credential or permission to mutate a cluster.
The observed devbox AWS identity and current kubeconfig target differ; neither
is adopted here. All existing deployment and data-deletion gates remain.

Before creation, qualify the existing independent failure/expiry finalizer and
durable campaign ownership ledger; extend them only where needed. Use idempotent create/seed/read/cleanup phases, record partial
effects and resume the same run. Cleanup enumerates exact owned resources and
verifies absence, revokes fixture credentials and removes only owned volumes,
namespaces and clusters. Never use broad Docker prune, shared-cluster deletion or
shared PostgreSQL teardown. Retain only governed evidence and non-secret receipts
under the accepted retention policy. If collection uses ADP PostgreSQL, its
existing IAM-only cell authentication requirement is unchanged; a disposable
SonarQube application's private internal database is a separate fixture concern.

## Acceptance handoff

S14 must version the provider list in the pilot manifest, field maps, source
contracts, acceptance mapping, native result producer/consumer and finalization
handoff. Do not modify signed contexts or fabricate engine identities. Reject a
manifest that claims the expansion but lacks any of the seven provider results.
Bind fresh requirement/source/tested revisions; legacy two-provider passes do
not satisfy EC-11. Preserve original results and report supplemental outcomes
separately until the combined evaluator consumes them.

EC-11 requires all seven through served UI/API and installed CLI, independently
reconciled observations, denied foreign/revoked scopes, disabled/stopped-service
checks, bounded cursor/restart behavior and owned-resource cleanup. Controlled
HTTP/GraphQL fixtures support code review; live proof requires real disposable
tools and the consented GitLab/SaaS test scopes. The pilot evaluator and S08 closure
owner must both consume this additional proof. Missing implementation,
unavailable fixtures and failed checks
are distinct incomplete states.
