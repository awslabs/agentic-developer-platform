# Isolated DevLake provider adapters

`Discover(plugin.ApiClient, Request)` calls extracted DevLake provider discovery
through a caller-owned client. It returns typed native scopes, bounded page
metadata and complete/partial/unsupported coverage. It does not construct a
client, store credentials, initialize a database or implement ADP authorization.

Production API staging includes this package; `expanded.go` connects it to the
existing `providerPage` dispatch. S14 still owns the served API, broker,
Profile UI/CLI and collection integration after #9437. Discovery success alone
does not complete #9449 or its live acceptance criteria.

## Exact upstream reuse

Source: Apache DevLake commit `6502e549f58eb40c40ccbcd39aab9bc8eea6616f`.
The source build retains upstream `LICENSE` and `NOTICE`; the extraction is
recorded and hashed in `../patches/0003-provider-expansion.patch`,
`../patches/0004-workflow-providers.patch` and the source lock. The original
three-provider patch and upstream revision remain unchanged.

| Provider | Existing upstream behavior actually called | New ADP adaptation |
|---|---|---|
| SonarQube | `plugins/sonarqube/api/remote_api.go`: `listSonarqubeRemoteScopes` → `querySonarqubeProjects`, including Browse-compatible `components/search_projects` and `p`/`ps` paging | Exported injected-client entry; response bytes/count/type/paging checks; native project-key identity and provenance |
| Jenkins | `plugins/jenkins/api/remote_api.go`: `listJenkinsRemoteScopes`, `isGroup`; `api/jobs.go`: `GetJobsPage` query and folder/job mapping | Exported entry; safe encoded parent path; preserve full job/folder identity; ignore provider URLs; reject HTTP 500 even on later pages |
| Argo CD | `plugins/argocd/api/remote_api.go`: `listArgocdRemoteScopes`, including application/project mapping | Exported entry; normalized default project; namespace-aware identity; deterministic order; bounded unpaginated response and explicit partial coverage |
| GitLab | `plugins/gitlab/api/remote_api.go`: native group/project list helpers and `toProjectModel` | Separate one-read stages; groups-to-projects resets native page to one; direct namespace filters; explicit personal namespace input; error-return fixes for upstream unchecked GET/unmarshal |
| PagerDuty | `plugins/pagerduty/api/remote_api.go`: `queryPagerdutyRemoteScopes` and native service mapping | Injected entry; relative services path and validated offset/limit/more envelope |
| Linear | `plugins/linear/api/remote_api.go`: `listLinearRemoteScopes`, existing teams GraphQL query, `mapLinearTeamsToScopeEntries`, `nextPageFrom` | Additive caller-selected limit preserving original default; exact read-query allowlist; GraphQL error/data/cursor validation |
| Slack | `plugins/slack/api/remote_api.go`: `listSlackRemoteScopes` and native channel mapping | Injected entry; refuse error envelopes before legacy private-only retry; validate channel types/native cursors and preserve workspace context |

The envelope checks, identity validation, test transport, coverage metadata and
component dispatch mapping are new ADP code. Collection follows
[actual DevLake normalized model reuse](../../../../docs/engineering-insights/devlake-poc/normalization-reuse.md);
this package implements discovery only. The first patch adds narrow
injected-client wrappers. The second also separates
GitLab group/project pages, disables its synthetic current-user entry on the ADP
path, applies direct namespace query filters and replaces unchecked error/panic
paths. Linear gains an optional bounded page size. Native provider mapping remains
upstream code invoked by those wrappers.

## Caller contract

- Supply a deadline-bound client that enforces the current ADP connection,
  endpoint, user delegation and revocation checks. This package cannot authorize
  a tenant based on a provider page number. Attach tenant/Project/connection,
  source/grant/mapping lineage and the required served provenance at integration.
- Each discovery call performs one bounded native read: GET, or Linear
  GraphQL POST for the exact teams query. The caller owns a discovery
  operation's overall 30-second deadline and five-page allowance; requests here
  reject operation page numbers above five. Pass `NextPage` and `NextCursor`
  together into the next request. They are native pagination state, never durable
  or authorized ADP cursors. Original provider requests keep an empty cursor.
- GitLab root discovery enumerates group namespaces only (`group_namespaces_only`),
  not a complete personal namespace inventory. Obtain personal namespace identity
  from the registered connection and explicitly select `user:<native-user-id>`;
  this package never reads implicit `GetData` user identity. Group selection uses
  the numeric group ID. `groups:N` and `projects:N` are separate native cursor
  stages; changing stage resets the native page to one. Project calls specify
  `with_shared=false` and `include_subgroups=false`, preserving direct native
  namespace ownership. Walk returned subgroups explicitly. `FullName` preserves
  full namespace/project paths alongside stable IDs and display names.
- PagerDuty validates offset/limit/more metadata before invoking native mapping.
  Linear rejects HTTP-200 GraphQL errors and missing/nonprogressing continuations;
  its wrapper refuses every query except the fixed, bounded teams read. No
  GraphQL mutation is supported. Slack requests public and private channels
  together and fails if either permission is missing; it never claims complete
  coverage after a private-only retry. It excludes IM/MPIM objects and does not
  expose history or write endpoints.
- Argo has no upstream pagination. The response is limited to 1 MiB and 1,000
  raw applications before upstream mapping; results are sorted and limited to
  the requested count (maximum 100). Overflow reports partial without an invented
  next page. Byte/count overflow returns an explicit limit error.
- Identity is scoped by provider/kind/namespace/native ID, then by the caller's
  tenant and connection. Same-named Argo apps in different namespaces and Jenkins
  jobs in different folders remain distinct.
- Non-success HTTP responses and malformed provider data fail without returning
  raw response bodies or credentials in error messages.

Run the isolated suite from the repository root with Go 1.26.0 on `PATH`:

```sh
GOMAXPROCS=2 modules/connectors/devlake/scripts/test-providers.sh
```

The suite fetches the locked source, verifies/applies all patches and executes
real extracted helper calls against local HTTP fixtures. It does not start real
provider servers or contact an ADP deployment.

Production API staging includes this package and `cmd/connector-api/expanded.go`
maps it into the existing discovery dispatch. The test runner uses that same
staging path. The shared served prerequisite permits #9449 provider integration
to proceed alongside the remaining #9437 campaign repair.

## Jenkins selected-job Profile slice

The served `/v1/profile-discovery` path now supports an explicitly selected
Jenkins job. The existing `POST /auth/credentials` interface accepts
`service: jenkins`, `credential_type: basic_auth` and a JSON string in `value`
containing `url`, `username` and `token`. This uses the native vault's opaque
secret-value convention; the provider is inferred from `service`. The registered URL
must be HTTPS and may include a Jenkins context path. No credential is sent to
Go or written to a second store.

Use the existing Profile binding API with `provider: jenkins`, `kind: ci_job`,
the full job name as `native_id` and its parent folder as `namespace` (`/` for
a root job). Verify that binding, then run the onboarding session's discovery
with `adapter: devlake` and apply its source enrichment. The result is a typed
source reference in `configuration.advanced.pipelines`; it proves job identity,
not a successful build or deployment. Frontend source types accept this kind;
an unbound Jenkins inventory picker remains future #9449 work.

Verification and discovery read only that exact encoded job path. The broker
uses the secret version observed during verification and checks it again around
the read. It resolves persisted operation authority before the read and rechecks
the actor, binding, connection and selection afterward. Master flag and tenant
admission gate the path. Registered workload permission must cover
`/internal/v1/profile-discovery/resolve` and `/jenkins-page` callbacks.

The callback returns only the selected native job's `fullName`, `name` and
`_class`, wrapped in the `jobs` envelope expected by DevLake. Go then invokes the
existing extracted Jenkins helper and returns one complete observation page.
It accepts no continuation or caller-selected endpoint. FreeStyleProject and
WorkflowJob are supported; parent inventory, folder selection and multibranch
qualification remain outside this slice. Other expanded providers still require
their served Profile integration.

Run the actual Gateway → Go executable → Gateway broker → controlled Jenkins
transport test using the same production source staging:

```sh
DEVLAKE_PROFILE_PYTEST=/path/to/gateway-test-environment/bin/pytest \
  modules/connectors/devlake/scripts/test-providers.sh
```

The optional test creates local HTTPS callback and workload-identity fixtures,
then exercises real Profile binding/verification/discovery/application/readback.
It checks exact folder encoding, unavailable Go without native fallback, revoked
work before provider access, provider failure and sibling substitution. It
creates no live provider, cloud credential or deployment. Live qualification,
remaining providers and collection-worker integration remain #9449 work.
