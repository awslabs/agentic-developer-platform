# Engineering Insights

Engineering Insights is an independently deployed module. Its emitter and distiller
services, evidence schemas, deployment infrastructure and CI live here rather than
in the gateway. The gateway may expose only the bounded data owned by the gateway.

The service implementations belong to the separate emitter and distiller stories;
this module initially establishes their deployment boundary. No Insights resources
are provisioned by an agent or by pull-request CI. Platform administrators apply
the one-time runner permissions before the first deployment.

## Offline business-context projection

`projection.py` provides a read-only query over native work/cost facts and the
admitted Context relationship/contribution readback. Call
`query_authorized_projection` only with explicitly requested Project IDs and a
Project-access callback that checks the current caller; source admission alone
never grants read access. Each native fact needs its original six-field qualified
`subject_ref`, its event `effective_at` and `recorded_at`, and a `kind` of `work`
or `cost` (with decimal `amount` and `currency` for costs). Supply approved dated
relationships, admitted Contribution snapshots with pinned work links, and any
approved cost allocations with their allocation policy version and basis.
Native work not explicitly linked by an admitted Contribution still requires
an approved Product mapping from its source adapter; this query does not infer
Product membership from an Initiative or copy raw facts into another Project.

The result keeps original identities and dated relationship/mapping versions.
Combined Portfolio filters deduplicate each native fact before aggregating;
separate overlapping Portfolio totals are **not** additive. `coverage` is
`incomplete` if an input Project/link cannot be shown. `cost_by_currency`,
`allocated_by_currency` and `unallocated_by_currency` never mix currencies; if
an allocation target is hidden, the affected currency's remainder is omitted
rather than reported as zero. Context memberships are historical by default;
`regroup_at` explicitly requests a separately labelled current regrouping.
These in-process fixtures do not establish deployment or live source coverage.

`results.py` adapts a versioned published I22 snapshot to the bounded
`business_context` HTTP contract. Its caller supplies a current Project-read
authorizer, authenticated session resolver, snapshot reader and cursor-signing
secret; the adapter itself cannot mint authorization or publish a snapshot.
It rejects unavailable and incomplete data rather than constructing an empty
success. The local HTTP test uses run-owned in-memory fixtures only. The gateway
packages this canonical reader and projection into its service image and checks
current Product/Project visibility before using its read-only S3 provider. It
loads at most 2 MiB from `results/v1/{tenant_id}/{project_id}/published.json`
inside the versioned Insights evidence bucket; the bucket name comes only from
the protected `/adp/<environment>/gateway/insights-results-bucket` parameter.
No bucket configured or no published snapshot means pending, not an empty result.
A source-owned snapshot publisher is **not** supplied by this read-only story;
its publication must be established before live acceptance.

Run the standalone offline projection and admitted-registry scenarios with
`python3 -m unittest discover -s modules/insights/tests -p 'test_*.py'`.
With local `pytest` installed, `modules/insights/pytest.ini` also enables the
issue's exact `test_i22_registry.py::insights_deduplication`,
`test_i22_registry.py::insights_effective_time` and
`test_i22_registry.py::insights_projection_access` selectors. Their fixtures
are in-memory only; no service is provisioned.

The installed-CLI `test_i23_registry.py::insights_cli_context_read` selector
installs the candidate from a run-owned local HTTP gateway, reads a fixed
registry projection using synthetic login, and independently checks the
authenticated API response. It provisions no external service.

The `test_i23_registry.py::insights_cli_deduplicated_view` selector compares
two-Portfolio and historical CLI results to independent API responses, proving
one native cost and version-pinned membership at both times.

The `test_i23_registry.py::insights_cli_denial_coverage` selector compares
installed CLI denials to separate API reads after Project access is revoked,
against an older server, and while the projection is pending or incomplete.
It also checks that preexisting Product help still works.

The [lineage v2 contracts](schemas/README.md#requirements-and-test-analytics-v2)
add story/criterion revisions, coverage mappings, distinct test definitions,
pack runs, planned instances, results and acceptance assessments. The proposed
initial query path uses S3 snapshots/JSONL, curated Parquet and bounded DuckDB
workers. Schemas and offline validation exist; issue ingestion, Parquet
publication, DuckDB services and acceptance evaluation are not implemented by
this contract change. Existing v1 delivery records remain supported.

The bootstrap policy grants no IAM writes or `PassRole`. Before enabling the
pipeline, the operator must bootstrap and admit the exact emitter and distiller
roles with reviewed permission boundaries through `platform/automation-infra`.
Supply those two role-to-boundary inventory entries as a JSON object in the
protected environment variable `ADP_INSIGHTS_ROLE_BOUNDARIES`; local operator
runs use `TF_VAR_deployment_role_boundaries`. Terraform retains each boundary
and rejects absent or incorrectly targeted entries. This configuration does not
grant admission: the platform's operator-owned policies remain authoritative.

The deployment entrypoint is `deploy.sh`. It supports `--infra`, `--emitter`,
`--distiller`, `--verify`, `--plan` and policy-only `--bootstrap-ci`. Use a
confirmed account, region and protected deployment identity; application
image stages require immutable digests. CI only validates offline and never
provisions AWS resources. See the [architecture and operator handoff](../../docs/engineering-insights/architecture.md)
for permission setup, machine identity registration, live checks and recovery.

The engine's health adapter verifies the complete namespace network-policy set,
including exact DNS and gateway selectors and ports. HTTPS egress must include
the regional S3 prefix list and the target VPC's private STS interface addresses;
additional destinations must fit GitHub's published API ranges. The registered
verification identity needs read access to namespace network policies and EC2
`DescribePrefixLists`, `DescribeVpcEndpoints` and `DescribeNetworkInterfaces`.
When additional destinations are configured, verification reads the public
`https://api.github.com/meta` endpoint without credentials. Missing or unreadable
destination evidence, unexpected policies and broader grants block health
evidence rather than being accepted. These checks do not provision resources or
establish live network connectivity.
