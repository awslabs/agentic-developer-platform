# L5 implementation boundaries

Use [the modular deployment contract](../../docs/l5-services/modular-deployment-design.md)
and each service's architecture under `docs/l5-services/`.

- Terraform belongs in this module's `infra/<service>/` root, backed by its own
  state. Reuse `infra/modules/` for common code. Do not add new L5 resources or
  service toggles to gateway/platform Terraform. The existing gateway module
  call is legacy state compatibility only; preserve it until migration is proven.
- Workflow YAML belongs in `.github/workflows/`; deployment implementation belongs
  in this module's `scripts/`. Extend the shared CI and release path. Publish one
  immutable package, install selected roots, then qualify each service independently.
- Scope workers, repair scans and queue routing by durable service ownership.
  Product APIs and the broker continue to govern customer provider resources.
- Infrastructure installation, code rollout and native qualification are distinct
  outcomes. Keep unqualified consumers disabled; retain run/outbox history.
- The remaining release stories own runtime assembly, their service drivers,
  installation readback and live acceptance. An artifact-only workflow is insufficient.
- Planning file lists are explanatory. Additional necessary files are allowed;
  a path missing from that list is not a reason to fail a node.

Run the relevant Terraform mock tests, deployment tests and changed runtime tests.
Public documentation stays under `docs/l5-services/` and follows `docs/PUBLISHING.md`.
