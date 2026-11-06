# PostgreSQL validation for #7772

Validated on 2026-10-16 against PostgreSQL 16 in an owned local container.
The implementation baseline is `c9c4e139e7223497ec68dbd900d8ba24ceaeac5e`;
this change corrects its test client and Ruff formatting. No production logic
changed in this validation correction.

The original opt-in PostgreSQL run failed all 13 database cases: synchronous
TestClient moved database work onto a different event loop. Using AsyncClient
with ASGITransport keeps requests on the fixture loop. Three refusal cases also
read expired ORM identities without awaiting; they now retain the node IDs
before expiring the objects. Assertions and refusal expectations are preserved.

## Executed checks

Set `ADP_TEST_POSTGRES_URL` privately to an isolated PostgreSQL asyncpg URL with
CREATE SCHEMA rights. Each test creates and removes its own random schema.
Use nonfunctional AWS credentials for these mocked-provider repository tests.
From `modules/gateway`, using the frozen Python 3.12 dev environment:

```sh
pytest -q tests/orchestration/test_flow_read_performance.py \
  tests/orchestration/test_list_flows.py \
  tests/orchestration/test_read_api.py \
  tests/orchestration/test_progress_projection.py
pytest -q tests/orchestration/test_plan_lineage_preserving.py
ruff check src/ tests/ pricing_policy/ deployment_evidence/
ruff format --check src/ tests/ pricing_policy/ deployment_evidence/
```

Results: **209 passed, zero skipped**, followed by **6 passed**. Both Ruff checks
passed. Existing dependency deprecation warnings remain.

| Acceptance | Executed PostgreSQL cases | Result |
|---|---|---|
| AC-01 | Bounded HTTP lineage queries | Pass |
| SCN-01 | 20 flows with 1, 10 and 30 history hops | Pass, all three cases |
| AC-02 | Missing/foreign receipts, missing predecessor, 99/100/101-hop boundaries, candidate overflow, plan-receipt overflow | Pass, all six cases |
| AC-03 | Next-request amendment visibility, list filters, concurrent amendment during a request | Pass, all three cases |

These tests exercise the actual PostgreSQL router/read path with mocked identity
and activity dependencies. They establish repository behavior and SQL bounds;
they do not establish deployed authentication, page latency, browser behavior,
or the live acceptance of #7777/#7787. Private raw logs remain with the operator.
