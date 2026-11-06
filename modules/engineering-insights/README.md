# Engineering Insights — earlier module proposal

The implementation home is **[`modules/insights/`](../insights/README.md)**.
This directory retains the earlier module-location proposal and contains no
second service or deployment. Detailed design remains under
[`docs/engineering-insights/`](../../docs/engineering-insights/README.md).

Engineering Insights is a shared ADP capability for independent projects and
ADP itself. It links versioned story briefs and acceptance criteria to coverage
catalogs, tests, planned executions, results and evidence. The current
[delivery architecture](../../docs/engineering-insights/architecture.md) retains
the broader task, cost, PR, deployment and incident lineage.

The [v2 lineage contracts](../../docs/engineering-insights/lineage-schema.md#11-requirements-and-test-analytics-v2)
and offline examples live in `modules/insights/schemas`. Initial requirements/test
analytics are designed around S3 snapshots/JSONL, curated Parquet and DuckDB;
S3 Tables/Iceberg remains a later option. Project authority and operational state
stay in ADP's existing control plane through explicit interfaces.

GitHub remains ADP's requirements authoring source and test harnesses execute the
tests. Collector, query and acceptance services remain implementation work; the
schema and infrastructure skeleton do not establish a deployed capability.
