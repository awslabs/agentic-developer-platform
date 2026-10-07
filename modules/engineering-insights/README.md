# Engineering Insights

Engineering Insights is a shared ADP platform module for collecting engineering
run evidence and providing project-scoped historical insights. Independent
projects and ADP itself consume the same publishing and query interfaces.

**Status:** module home established; architecture and implementation remain
proposed. This directory currently contains this README only. It does not yet
contain a runnable service, infrastructure or executable capability tests.

The canonical [architecture and review](../../docs/engineering-insights/README.md)
lives under `docs/engineering-insights/`. Keep detailed contracts and decisions
there rather than duplicating them in this README.

## Implementation ownership

This module owns Insights project/source configuration, publishing contracts,
artifact upload and publication, report normalization, analytical schemas,
query services, provider adapters and capability-specific tests. Its artifact
store uses S3; historical analytics use S3 Tables. Project grants and current
publication state use a transactional control plane integrated with ADP.

Reuse ADP's authoritative identity, tenants, membership and revocation. Gateway
routing/authentication and frontend integration remain in the existing gateway
and frontend, with explicit interfaces to this module. Insights does not create
a parallel user or tenant directory.

CI systems and test harnesses continue to execute tests. ADP regression is a
consumer adapter, and its catalog, fixtures and acceptance policies are not
global defaults for the shared module.

## Planned directory layout

The following directories will be introduced with their implementations:

```text
modules/engineering-insights/
  README.md
  contracts/       # Versioned API, envelope and normalized-result schemas
  src/             # Capability services, persistence and ingestion/query logic
  adapters/        # Provider/report adapters, including the ADP consumer
  client/          # Generic publishing client and CI integration helpers
  infra/           # Module-owned AWS resource definitions and permissions
  tests/           # Contract, unit, integration and isolation tests
```

Shared gateway/frontend and platform deployment wiring stay in their existing
locations. Their integration points must be documented and tested with the module.
The layout does not prescribe a new runtime/framework or add deployment hooks.

## Implementation entry point

Follow the architecture's [delivery sequence and release acceptance](../../docs/engineering-insights/README.md#delivery-sequence-and-release-acceptance).
Start with versioned contracts and project authorization, then bounded artifact
publication, normalization and query access. Before offering the capability to
other projects, demonstrate two independent consumers and cross-tenant/project
isolation through the acceptance criteria in that document.
