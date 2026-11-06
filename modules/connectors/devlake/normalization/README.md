# Native pipeline normalization

This package runs actual tasks from the DevLake revision in `../source-lock.json`:

| Entry point | Upstream tasks | Native output |
|---|---|---|
| `JenkinsBuilds` | `ExtractApiBuilds`, `ConvertBuildsToCicdTasks` | `JenkinsBuild`, `JenkinsBuildCommit`, `devops.CICDPipeline`, `devops.CICDTask` |
| `GitLabPipelines` | `ExtractApiPipelineDetails`, `ConvertDetailPipelines` | `GitlabPipeline`, `devops.CICDPipeline`, `SubtaskState` |
| `SonarProject` | `ConvertProjects`, `ExtractIssues`, `ConvertIssues` | `CqProject`, `CqIssue` |
| `ArgoApplication` | `ExtractApplications`, `ConvertApplications`, `ExtractSyncOperations`, `ConvertSyncOperations` | `ArgocdApplication`, `CicdScope`, `CICDDeployment` |
| `PagerDutyService` | `ConvertServices`, `ExtractIncidents`, `ConvertIncidents` | `Board`, `Issue`, `BoardIssue` |
| `LinearTeam` | `ConvertTeams`, `ExtractIssues`, `ConvertIssues` | `Board`, `Issue`, `BoardIssue` |
| `SlackChannel` | `ExtractChannel`, `ExtractChannelMessage` | Tool-layer channel and messages only |

The wrappers provide task contexts, validate the supported scope and invoke the
upstream implementation. All field mapping, domain IDs, raw provenance, batch
storage and incremental state behavior come from upstream tasks. There is no
ADP `Fact` schema, copied mapping or mock converter. Worker source staging includes
the package; the served collector worker has not yet been wired to call it.

## Integration contract

Supply an already-admitted cell DAL and the native options, an explicit
`RegexEnricher`, config reader and safe logger. Register real
`jenkins/impl.Jenkins{}`, `gitlab/impl.Gitlab{}` and the five additional provider implementations with upstream
`plugin.RegisterPlugin` once at worker startup, before concurrent tasks. Do not
initialize global plugin APIs to run these database-only tasks. The caller must
bind database operations to the supplied context as well as passing that context
to the task wrappers.

Raw responses must already have been collected under the current connection and
source grant. Expanded wrappers process selected staged raw/tool rows and invoke
pinned native extraction/conversion; they do not collect provider HTTP responses.
Missing raw issue, incident, sync-operation or message rows leave those native
entities unobserved, not verified empty. The worker must gate publication on
complete scoped collection; wrapper success alone is not proof of coverage.
Slack channel raw input must be the sole selected channel, and message raw input
must match its selected channel. Argo CD conversion requires an isolated
application in the admitted cell because the pinned converter scans the
connection, not the selected application. A revision without a native sync-operation
repository URL is refused before conversion; the pinned converter otherwise fills
a missing URL with the deployment name, which is not repository evidence. Never
share native connection IDs across independent scopes in a cell.

 Use upstream `api.RawData`, the provider raw table and upstream
scope params. Provision the required native raw/tool/domain tables and GitLab's
`_devlake_subtask_states` through the authorized schema path before invocation;
the runtime receives DML permissions only. The test's `AutoMigrate` is disposable
fixture setup, not a production migration mechanism.

ADP continues to own tenant/Project admission, IAM database authentication,
current authorization, revocation, retention, immutable export and the six-field
evidence identity. Upstream domain IDs and `RawDataOrigin` are neither tenant
authority nor a complete immutable evidence envelope. These wrappers accept no
credentials, choose no database or endpoint and perform no provider HTTP calls.

Upstream tasks can delete and upsert batches and save task state before a later
step fails; the wrapper does not introduce an atomic transaction across tasks.
The worker must retain its governed staging/publication and current-authority
checks, and must not publish partial work after an error. Full-sync replay with
unchanged inputs is tested; empty-input deletion, failure recovery, incremental
collection and concurrent runs require integration qualification. Do not overlap
jobs writing the same native scope in a cell. These pinned GitLab tasks do not
pass the enrichment expressions into their state configuration; the caller must
force full sync when that configuration changes.

## Supported slice and pinned behavior

- Jenkins supports an ordinary selected job only. Multibranch's prefix query is
  rejected by this wrapper. `ConvertJobs` scans all jobs in a connection and is
  deliberately not invoked; scope creation, stages, commit/domain joins and the
  remaining collectors are separate integration work. Pipeline scope IDs still
  use upstream `JenkinsJob`/`GitlabProject` identities.
- Both sources populate `cicd_pipelines`, with native status preserved.
  Jenkins also populates `OriginalResult`; the pinned GitLab converter leaves
  `OriginalResult` empty and records its combined native value in
  `OriginalStatus`. Do not invent a GitLab native result field.
- Jenkins' omitted production regex defaults to `PRODUCTION`. Tests supply
  explicit nonmatching expressions so that this default cannot be confused with
  source evidence. Production integration must retain the actual configuration
  and converter revision with exports.
- GitLab's pinned converter uses the current time when native `created_at` is
  absent. Its tool record and raw payload retain the missing value. This fallback
  must not be presented as an observed source timestamp.
- Empty revisions remain empty in native tool rows; multiple Jenkins revision
  records remain separate. This slice makes no first-match commit join and
  creates no deployment or acceptance claims. Provider-specific collection for
  SonarQube, Argo CD and other tools remains outside this initial pipeline slice.

## Local proof

With Go 1.26.0 and PostgreSQL server binaries installed:

```sh
DEVLAKE_TEST_PG_BIN=/path/to/postgresql/bin \
  modules/connectors/devlake/scripts/test-normalization.sh
```

The runner verifies the immutable upstream lock and patches, uses production
worker source staging, and creates a temporary PostgreSQL cluster listening only
on a private Unix socket. It stops and removes that cluster on exit. No existing
database, ADP deployment or provider is contacted; fetching pinned source may
use GitHub. `DEVLAKE_TEST_PG_BIN` defaults to `pg_config --bindir` when available.

The test executes real extractors, converters and DAL code. It checks a common
pipeline query against independent expected statuses/results, source timestamp
and duration handling, domain/task IDs, raw-to-tool-to-domain provenance,
missing/multiple revisions, repeat execution, scope filtering, cancellation,
missing raw tables, and cross-cell connection denial with colliding native IDs.
Each cell uses its own database and DML-only role; schema changes and switching
to the other cell role are denied. Local trust authentication is confined to the
disposable socket, so this verifies PostgreSQL permissions, not RDS IAM auth or
ADP tenant admission. This component proof does not complete live qualification
or the expanded collection story.
