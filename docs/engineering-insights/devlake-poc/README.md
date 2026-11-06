# DevLake connector reuse POC

The POC evaluates reuse of Apache DevLake connector code for ADP Product Profile
onboarding, with separately optional historical collection for Engineering
Insights. [Epic #8887](https://github.com/aws-e/adp/issues/8887) owns the POC;
[Engineering Insights #7295](https://github.com/aws-e/adp/issues/7295) owns the
broader capability. The POC informs that delivery without replacing its lineage,
evidence or authorization contracts.

This directory is the canonical home for the POC architecture, story designs and
delivery flow. The flow is active; implementation repair and the provider expansion precede
the live campaign. Implementation progress is not live acceptance.

| Start here | Purpose |
|---|---|
| [Selected providers](provider-expansion.md) | Seven-provider selection, disposable tools, consented SaaS test scopes and acceptance amendment. |
| [Architecture](architecture.md) | Shared design for Go connector reuse, Profile integration, tenant/Project isolation, normalization and evidence lineage. |
| [Normalized model reuse](normalization-reuse.md) | Actual upstream domain types/converters, ADP governance boundary and required cross-tool query proof. |
| [S01 evaluation contract](evaluation-contract.md) | Original pilot boundaries/field map, measurement plan and readiness inventory; S14 must migrate the runtime contract for the selected seven providers before live admission. |
| [Optional deployment](optional-deployment.md) | Default-off feature flags and separate connector/collection infrastructure. Connector-only onboarding needs no Insights database; collection uses separate cell databases and roles on the existing PostgreSQL instance with renewable IAM-only authentication. |
| [Story designs and dependencies](backlog.md) | The 13 feature stories, their individual design contracts and implementation prerequisites. |
| [Traceability](traceability.md) | Epic criteria and story acceptance/scenario mappings. |
| [Delivery flow](delivery-flow/README.md) | Code integration, one coordinated acceptance campaign, recommendations and cleanup, with three machine evaluations. |
| [Planned files](delivery-flow/planned-files.md) | Proposed implementation files, owners and ordered edits. |
| [Unattended execution](delivery-flow/unattended-execution.md) | Initial launch admission, scoped repair, continuation, evidence and cleanup. |
| [Source audit](audit.md) | Pinned repository/upstream evidence and historical inventories. |
| [Design verification](verification.md) and [flow validation](delivery-flow/validation-results.md) | Reproducible authoring checks and the limits of the recorded results. |

See the [Engineering Insights overview](../README.md) for the broader capability
and existing module boundaries.
