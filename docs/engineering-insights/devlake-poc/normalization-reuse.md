# DevLake normalized model reuse

ADP reuses DevLake's source collectors, tool models and domain converters for
Engineering Insights. Profile discovery remains a bounded source listing and
does not require historical collection or a database. ADP's source-record and
lineage contracts wrap this pipeline; they do not replace its normalized model.

The intended collection path is:

```text
ADP registered connection + current source grant
  → DevLake collector / raw response
  → DevLake provider-specific tool model
  → DevLake domain converter / shared domain model
  → ADP governed evidence, lineage and authorized queries
```

## Inspected reuse boundary

Paths below are relative to `backend/` in the
[pinned Apache DevLake source](https://github.com/apache/devlake/tree/6502e549f58eb40c40ccbcd39aab9bc8eea6616f/backend).
These are actual upstream types and converters, not a new ADP `Fact` schema.

| Source | Shared domain types | Upstream converter entry points under `plugins/` |
|---|---|---|
| Jenkins | `devops.CicdScope`, `CICDPipeline`, `CICDTask` | `jenkins/tasks/job_convertor.go`, `build_cicd_convertor.go`, `stage_convertor.go` |
| GitLab | `code.Repo`, `PullRequest`; `devops.CICDPipeline`, `CICDTask`, `CICDDeployment` | `gitlab/tasks/project_convertor.go`, `mr_convertor.go`, `pipeline_detail_convertor.go`, `job_convertor.go`, `deployment_convertor.go` |
| Argo CD | `devops.CicdScope`, `CICDDeployment`, `CicdDeploymentCommit` | `argocd/tasks/application_convertor.go`, `sync_operation_convertor.go` |
| SonarQube | `codequality.CqProject`, `CqIssue`, quality metrics | `sonarqube/tasks/projects_convertor.go`, `issues_convertor.go`, `filemetrics_convertor.go`, `project_metrics_history_convertor.go` |
| PagerDuty | `ticket.Board`, `Issue`, `BoardIssue` | `pagerduty/tasks/service_converter.go`, `incidents_converter.go` (incidents become issues in this pinned plugin) |
| Linear | `ticket.Board`, `Issue`, `BoardIssue`, `Sprint` | `linear/tasks/board_convertor.go`, `issue_convertor.go`, `cycle_convertor.go` |
| Slack | Tool-layer channel/message records; no domain converter at this revision | `slack/tasks/channel_extractor.go`, `channel_message_extractor.go`, `thread_extractor.go` |

For example, Jenkins and GitLab both populate `cicd_pipelines`, retaining
`OriginalStatus` and `OriginalResult` alongside normalized status/result. This
supports one query across both tools without hiding the original values.
Sharing a type does not establish shared identity or causal relationships:
PagerDuty incidents and Linear work items still need source/type distinctions.
Slack stays explicitly tool-layer unless a separately justified mapping is added.

These converters are tasks using DevLake's DAL, scope options, tool tables and
sometimes enrichment configuration. Importing a Go struct or copying a handful
of field assignments is insufficient reuse. S03/S14 must retain the needed
collector/extractor/converter task chain, migrations and configuration inside
the collection cell, as required by S03. Do not add a second generic map-based
normalization layer or use the current GitHub/Jira `CommonIssue` helper as a
substitute for the upstream engineering domain model.

## ADP authority and lineage

ADP resolves the current tenant, Project, connection and source grant before
collection. Upstream domain IDs encode plugin/model/native keys, often a numeric
connection ID; they are not tenant authorization. Run the necessary raw, tool
and domain tables within the admitted tenant/Project/policy cell DB boundary.
The same upstream ID in another cell must not collide or become queryable.
Sharing a PostgreSQL instance does not imply sharing cell credentials/tables;
the existing IAM-only authentication and cell isolation requirements remain.

Keep original payloads, native IDs, upstream `RawDataOrigin` where available,
the pinned converter/configuration revision and ADP's six-field evidence
identity through export. ADP owns immutable versioning, current read authority,
retention and requirement/test/acceptance lineage. Preserve missing or ambiguous
joins; build success, quality gates and Argo sync alone do not prove acceptance
or deployed state. Review upstream mapping defaults against independent native
readback before accepting derived engineering metrics.

## Implementation and proof

The provider foundation implements discovery and component packaging. It does
not implement the expanded historical collection pipeline. S14 integrates that
pipeline and the served Gateway/Profile/CLI path after #9437. The current
GitHub/Jira worker helper is not proof that DevLake domain normalization is done.

S14 AC-03/SCN-03 must execute real upstream conversion tasks with controlled tool
records: reconcile Jenkins and GitLab through one `cicd_pipelines` query, check
normalized and original values against independent expectations, trace each row
to its native record, and reject cross-cell reads with colliding upstream IDs.
Exercise missing/ambiguous revision cases and preserve Slack as tool-layer data.
Retain the existing live-provider qualification in EC-11. Schema imports, mock
converters or synthetic discovery success cannot satisfy this collection proof.
