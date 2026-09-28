# Superplane — domain app

ADP-side module for the Superplane domain app (EPIC #4910, unit U1 / issue #5037).

Start with the **[authoritative Superplane design](DESIGN.md)** for logical,
database, API, CLI and network design. It defines the accepted target architecture
and distinguishes implemented source from pending requirements. Supporting
contracts below elaborate implementation; they do not override that design.

This directory contains the maintained Superplane API, controller, monitor,
governed execution adapters, installer and domain UI. Source delivery and remote
code validation remain separate from deployment and live workload acceptance.
For operational detail, use the [installation contract](installation/README.md),
[execution contract](executor/README.md) and [onboarding/workload UI](ui/README.md).

The governed executor currently supports AWS native EKS capacity only. Restoring
the original AWS + neocloud GPUs in one EKS cluster requires the
[hybrid capacity extensions](executor/HYBRID-CAPACITY.md). The
[mixed-provider demo](tests/acceptance/MIXED-PROVIDER-DEMO.md) defines the required
workload and shared-serving proof; it is not yet an executable acceptance run.

## Feature gate

Everything here is behind `FEATURE_SUPERPLANE_ENABLED`, which is **fail-closed** —
absent, empty or invalid configuration resolves to *off*, and only the literal string
`"true"` enables it. With the gate off, no Superplane route is reachable, no navigation
item appears, and the deploy phase does no work.

| Surface | Where the gate is read |
|---|---|
| Gateway backend | `modules/gateway/src/features/routes.py` (`_is_enabled_strict`) |
| Frontend route + nav | `frontend/src/services/features.ts`, `App.tsx`, `components/Navigation.tsx` |
| Deploy | `modules/domain-apps/superplane/deploy.sh` or the module workflows |
| Undeploy | `platform/scripts/undeploy.sh` `PHASE_ORDER` + `undeploy-phases.sh` `phase_superplane()` |

The strictness is not stylistic. The frontend resolves flags as
`data ?? ALL_FEATURES_ENABLED`, so the default renders both while `/features` is in
flight and whenever that fetch fails. A fail-open default would surface the route on
every cold load and keep surfacing it during exactly the outage it was switched off
for — and it would defeat the documented rollback ("flip the flag off").

## Layout

Per design note §3 (lines 149–162). Each directory is owned by the unit named against it.

| Path | Contents | Owner |
|---|---|---|
| `agent/personas/` | Superplane persona definitions (name **and** `.md` file together) | U4 |
| `agent/skills/` | Agent skills for domain workflows | U4 |
| `tools/superplane-mcp/` | MCP tool surface over the domain contracts | U8 |
| `cli/` | CLI verb implementations | U6 |
| `contracts/` | Versioned observation contracts — authenticated, signed, workspace-scoped fleet-health and budget submission ([README](contracts/README.md), [wire schema](contracts/WIRE-SCHEMA.md)) | U8 |
| `integrations/mlflow/` | MLflow integration adapters | U9 |
| `ui/` | Domain-specific UI surfaces | later units |
| `events/` | Event schemas and handlers — empty; no event schema is needed for R11's criteria, and versioning one ahead of a consumer is premature | U8 |
| `infra/control-plane/` | Terraform for the control plane | U3 |
| `infra/workspaces/` | Terraform for per-workspace resources | U3 |
| `releases/` | Pinned upstream release manifests (digest-pinned images) | U2 |
| `tests/acceptance/` | Acceptance tests, incl. deferred live criteria | per-unit |

### There is deliberately no `api/`

The maintained API and its isolated migration chain live in
`src/superplane-api/`; controller and monitor source live beside it under `src/`.
U22 transferred this source into ADP. Add routes and migrations to that existing
service rather than creating a second domain service or writable upstream tree.

If an ADP-side unit finds itself wanting to add `api/`, that is a design question to
raise — not a directory to create.

## Related

- Module-shape precedent: `modules/domain-apps/cyber/`. Both domain apps deploy
  through their own modules; the basic `deploy-all.sh` does not install either.
- Offline CI lane: `.github/workflows/superplane-domain-ci.yml` — lint + tests over this
  directory. No AWS account, no image build, no deploy.
- Teardown: registered as the **first** undeploy phase (destroy order is the reverse of
  deploy order — a domain app sits on top of the platform, so it goes first), including
  the legacy `deploy-all.sh --destroy` entry point.
