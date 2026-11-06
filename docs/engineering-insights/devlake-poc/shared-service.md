# Shared Profile discovery service

This is the transport and authorization prerequisite for [S14 / #9449](stories/s14.md).
It extracts the served Profile path from #9439 independently of campaign evidence
and cleanup work. The seven-provider discovery adapters from #9554 remain in the
same API build. No database migration or deployment is part of this change.

## Served contract

Gateway persists a `discoverSources` operation selected with `adapter: devlake`.
The operation pins the requesting actor, tenant, Product, Project, onboarding
session, selected binding generations, observed source revisions, connection
revisions and registered connector consumer. Each attempt has a bounded lease.
The native adapter remains the default.

Gateway posts to the Go service's `POST /v1/profile-discovery` with
`tenant_id`, `product_id`, `project_id`, `operation_id`, `attempt_id`, `binding_id`,
`provider`, `cursor` and `limit`. The existing GitHub/Jira path requires a page
limit of 100. There is no provider credential or caller-selected endpoint in this
request. The service also exposes `GET /healthz`; no placeholder discovery route
is registered.

Go uses workload credentials to sign its Gateway callback with SigV4. The
registered IAM service requires `connector:profile:read` and current tenant
entitlement. Gateway resolves the persisted work and actor's current membership,
checks the selected connection before provider access, then repeats authority and
selection checks after the read. Disabled, revoked, foreign, expired and changed
work fails without returning provider records.

The callback routes are `POST /internal/v1/profile-discovery/github-page` and
`POST /internal/v1/profile-discovery/jira-boards-page`. GitHub reuses the current
installation reader. Jira reuses the Product-shared Atlassian connection and the
selected project or board. These existing native reads are intentional; S14's
new providers must execute their extracted DevLake discovery functions.

A successful Go response contains `contract_version: 1`, `items`, `coverage`
and `next_cursor`. Complete pages include an explicit empty `next_cursor`.
Partial pages have a bounded advancing cursor. Gateway binds the continuation
to the selected binding, namespace and generation, rejects replay, and checks
current source authority again during readback and Profile application. A Go
failure returns unavailable; a selected DevLake operation has no native fallback.

Profile observations carry `observation_only` provenance and an observation hash.
These are source references for onboarding. They do not replace the actual
[DevLake raw/tool/domain model conversion](normalization-reuse.md) required for
collection.

## Configuration and S14 handoff

The master `FEATURE_DEVLAKE_CONNECTORS_ENABLED` flag and tenant connector
admission must both be enabled. Gateway configuration supplies
`CONNECTOR_PROFILE_DISCOVERY_URL` for the private Kubernetes service and
`CONNECTOR_PROFILE_DISCOVERY_CONSUMER` for the registered connector identity.
Go configuration supplies `CONNECTOR_GATEWAY_URL`, `AWS_REGION` and
`CONNECTOR_API_LISTEN`. The IAM role requires the exact callback API resources;
workload identity, network policy and Gateway identity registration remain part
of the single coordinated deployment. Missing configuration fails closed.

S14 extends the existing contract's provider allowlists, typed Profile fields,
source registration, cursor interpretation and scoped provider transport for
SonarQube, Jenkins, Argo CD, GitLab, PagerDuty, Linear and Slack. Preserve the
foundation's `full_name` and partial-coverage `reason` through that schema change.
Actual normalized collection and its separate grants/cell database remain S14
work. Campaign producers, acceptance evaluation and cleanup remain #9437.

## Verification

`modules/connectors/devlake/scripts/test-providers.sh` stages the production API
and runs provider tests plus the Go subprocess HTTP test. The subprocess executes
`main`, acquires controlled workload credentials, signs a callback and returns a
complete page using the real served JSON contract. All endpoints are local test
fixtures; it creates no cloud or SaaS resources.

Gateway's focused suites cover current authority, late revocation, connection
rotation, tenant/Project boundaries, cursor recovery, default-off behavior,
native integration compatibility and selected source application:
`test_profile_discovery_authority.py`, `test_devlake_enrichment.py`, `test_i09.py`
and `test_jira_context.py` under `modules/gateway/tests/products/`.
