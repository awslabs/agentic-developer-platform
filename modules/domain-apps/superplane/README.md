# Superplane — domain app

ADP-side module for the Superplane domain app (EPIC #4910, unit U1 / issue #5037).

**Status: skeleton.** This directory is the landing place every other ADP-side unit in
the EPIC builds into. Today it contains the layout, the feature gate and the offline CI
lane; the substantive contents arrive with the units named below.

## Feature gate

Everything here is behind `FEATURE_SUPERPLANE_ENABLED`, which is **fail-closed** —
absent, empty or invalid configuration resolves to *off*, and only the literal string
`"true"` enables it. With the gate off, no Superplane route is reachable, no navigation
item appears, and the deploy phase does no work.

| Surface | Where the gate is read |
|---|---|
| Gateway backend | `modules/gateway/src/features/routes.py` (`_is_enabled_strict`) |
| Frontend route + nav | `frontend/src/services/features.ts`, `App.tsx`, `components/Navigation.tsx` |
| Deploy | `platform/scripts/deploy-all.sh` (`SUPERPLANE_ENABLED`, default `false`) |
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
| `contracts/` | Versioned domain OpenAPI + event contracts and their thin clients | U8 |
| `integrations/mlflow/` | MLflow integration adapters | U9 |
| `ui/` | Domain-specific UI surfaces | later units |
| `events/` | Event schemas and handlers | U8 |
| `infra/control-plane/` | Terraform for the control plane | U3 |
| `infra/workspaces/` | Terraform for per-workspace resources | U3 |
| `releases/` | Pinned upstream release manifests (digest-pinned images) | U2 |
| `tests/acceptance/` | Acceptance tests, incl. deferred live criteria | per-unit |

### There is deliberately no `api/`

New domain server routes and migrations live **beside the Superplane API upstream**, not
here (design §3 line 171: "ADP installs and tests the pinned release"). A second
writable ADP-hosted domain service was explicitly withdrawn in the design's revision 3.
What lands on the ADP side is versioned contracts, thin API clients and MCP tools, auth
adapters, pinned build/deployment integration, and scoped provider-executor adapters.

If an ADP-side unit finds itself wanting to add `api/`, that is a design question to
raise — not a directory to create.

## Related

- Module-shape precedent: `modules/domain-apps/cyber/` — layout only. Its deploy
  registration is **absent** from `deploy-all.sh`, which is the failure mode this unit
  exists to avoid repeating.
- Offline CI lane: `.github/workflows/superplane-domain-ci.yml` — lint + tests over this
  directory. No AWS account, no image build, no deploy.
- Teardown: registered as the **first** undeploy phase (destroy order is the reverse of
  deploy order — a domain app sits on top of the platform, so it goes first).
