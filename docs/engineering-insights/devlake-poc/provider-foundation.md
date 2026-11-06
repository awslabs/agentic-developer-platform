# Provider foundation

This is the reusable discovery foundation for [S14 / #9449](stories/s14.md):
SonarQube, Jenkins, Argo CD, GitLab, PagerDuty, Linear and Slack. It extends the
existing Go `providerPage` dispatch and production component staging. The
[reuse inventory](../../../modules/connectors/devlake/providers/README.md)
identifies the pinned upstream discovery helpers and ADP adaptations.

The foundation supplies discovery, response validation and isolated tests. It
introduces neither a new normalized `Fact` model nor a tool-lab control plane.
[DevLake normalized model reuse](normalization-reuse.md) remains the collection
architecture: upstream raw/tool/domain tasks inside ADP's governed cell boundary.

## Integration ownership

| Work | Owner and completion boundary |
|---|---|
| Extraction patches, seven adapters, existing Go dispatch and API image staging | Foundation PR; locally reproducible discovery/build checks |
| Served Gateway-to-Go authority/transport | Extract from #9439 as a partial #9449 implementation; review and merge before provider integration |
| Original campaign repair | #9437; proceeds alongside #9449 after the shared integration |
| Registration, capability schema, Profile UI/CLI and actual DevLake collection tasks | #9449; integrates the foundation with #9437 and proves the served path |
| Real tools, consented GitLab/SaaS sources, release and cleanup | Existing POC operations and acceptance nodes; one coordinated campaign |

The foundation merge serves health only. The [shared service](shared-service.md)
adds the authorized Profile transport and GitHub/Jira callbacks used to prove
that contract. Seven-provider routing remains #9449 work; compiling an adapter
into the image does not expose it through Profile. S14 must use
the merged shared served contract, extend provider allowlists and typed scope
schemas, and exercise the actual Go process through Gateway and Profile/CLI.
Native GitHub/Jira remain compatible; the master flag gates all discovery and
historical collection remains separately optional.

The internal adapter cursor is provider continuation state. The served layer
must bind it to tenant/Project, connection revision, namespace, operation,
generation and current delegated authority. It owns the overall deadline and
rechecks authority before Profile admission. The foundation stores no credentials,
selects no endpoint and requires no cell database for discovery.

Shared-file overlap with #9437 now includes `cmd/connector-api/main.go`,
`scripts/build.py`, `scripts/test-discovery.sh` and the current-scope documents.
Preserve #9437's served authorization path and this PR's provider dispatch/staging
when reconciling them. `expanded.go` is the narrow mapping to the existing
discovery result; `full_name` and `reason` must survive the served schema update.
The approved parallel recovery removes S14's dependency on the whole campaign
repair after the shared API merges; budgets and deployment count stay unchanged.
The foundation PR must not close #9449 or claim live POC acceptance.

## Validation

Run with Go 1.26.0 and Python 3.12:

```sh
modules/connectors/devlake/scripts/test-providers.sh
```

The runner verifies/applies pinned extraction patches, uses production API
staging and adds test files only. It runs actual upstream helpers against Go
HTTP/GraphQL fixtures and the existing API dispatch tests, including native
GitHub/Jira regressions. It checks identities, continuation, bounds, errors and
default-off behavior. There is one fixture path in the Go tests, with no second
Python HTTP simulator. CI runs these checks without deployment.

Real-tool setup follows [the shared source procedure](provider-expansion.md#test-sources-and-ownership)
in the existing operations campaign. Retain exact ownership, expiry, cleanup,
private credentials and target gates. Local fixture success does not establish
real-tool compatibility, tenant authorization through Gateway, normalized
collection or completion of EC-11.
