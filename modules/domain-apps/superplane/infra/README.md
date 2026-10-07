# Superplane infrastructure — deployment lanes

Issue #5042 (U3), EPIC #4910.

## Infrastructure ownership

All Superplane-specific infrastructure source belongs under
`modules/domain-apps/superplane/`. Persistent AWS infrastructure is declared in
this app's Terraform modules; app installation and lifecycle entrypoints own
their planning, execution, verification and recovery. App environment inputs,
build declarations and infrastructure utilities belong here as well.

| Concern | App source owner |
|---|---|
| Control-plane IAM, repositories, configuration and build declarations | `control-plane/` and `../codebuild/` |
| Dedicated paid-worker builder and image-production infrastructure | `paid-worker-build/` and `native-image/` |
| Operation queue and runtime worker/observer roles | `domain-runtime/` |
| Protected operation registration and Gateway runtime IAM/RBAC | `shared-operation-authority/` composed by the shared authority root |
| Bootstrap actors and independently retained encryption | `lifecycle-foundations/` |
| Workspace network, EKS and node roles | `workspaces/` |
| Scoped installation-owned provider role, policy shards and child-role boundary | `domain-provider/` |
| Protected provider authority/evidence storage and Gateway grants | `provider-authority/`, composed by the existing webhook state owner |
| Installer role and installation access | `installer-access/` |
| Superplane permissions used by shared automation | App-owned Terraform modules composed by the shared automation root |
| Kubernetes resources, database preparation and installation checks | `../k8s/` and `../installation/` |

The existing ADP management cluster, database instance, state bucket, GitHub OIDC
provider and shared build publisher retain their platform owners. Pass their
references into app modules or read their declared outputs. An app may own a
separate scoped policy on a shared role without owning that role's lifecycle;
it must not replace or delete another application's grants.

The control-plane ownership guard recognizes only the exact app-owned Gateway
route-read policy and its attachment to `adp-<env>-role-gateway-service`. The
managed policy grants only `s3:GetObject` on this app's exact public route object.
A managed policy avoids the shared role's fixed 10,240-byte inline-policy limit.
Both plan sides and the paired policy document must be known and exact; the
shared role remains platform-owned. The legacy inline policy remains recognized
for reviewed removal during upgrades. An existing inline grant is removed and
replaced by the managed grant, so such an upgrade requires the destructive-plan
gate; no Terraform state move can change these distinct resource types.

GitHub workflow entrypoints remain in `.github/workflows/` as required by GitHub.
Shared Terraform roots may compose app-owned modules, and platform teardown may
order app cleanup before its dependencies. These are integration hooks, not a
second implementation of Superplane provisioning. Source ownership and state
ownership are distinct: a shared composition root can retain existing state
while an app-owned child module defines its resources.

Use the explicitly authorized AWS installation operator to prepare and apply
infrastructure. A local operator does not require a new ADP connection merely to
run Terraform. Runtime workspace credential delegation remains a separate
requirement. Keep selected account/role checks and actual saved-plan approvals.

When moving existing resources into child modules, preserve backend keys,
resource names, trust and permission scope and include Terraform `moved` blocks
or a coordinated state-transfer procedure as appropriate. Never let two states
own the same resource. Review the resulting plan for unintended replacement,
deletion or permission expansion. A source change alone does not establish that
live state has migrated; see [build ownership migration](BUILD-OWNERSHIP-MIGRATION.md).

Temporary bootstrap and workspace grants remain operation-owned and are removed
through the app's recorded cleanup path. They must not become permanent admin
permissions merely to simplify installation.

## Control-plane lane history

The following describes the original control-plane lane. Historical issue and
build blockers below are not a current live-installation status report; use the
selected release lock and actual installation receipts for that status.

`control-plane/` is the ADP-owned Terraform wrapper that deploys the pinned Superplane
control plane and the SkyPilot API service. It owns the AWS-side surface those images need —
IAM roles, ECR repositories, SSM configuration — and nothing else. Application code is now
ADP-maintained under `../src/` (U22, #5326) rather than upstream-owned, but this module still
owns none of it: `../releases/superplane.lock.yaml` pins what runs, and the migration chain
is U13's (#5045).

## The five lanes

| Lane | Trigger | What it does |
|------|---------|--------------|
| [`superplane-infra-plan.yml`](../../../../.github/workflows/superplane-infra-plan.yml) | PR + dispatch | Runs every `.tftest.hcl` with no AWS identity, then plans against real state |
| [`superplane-infra-apply.yml`](../../../../.github/workflows/superplane-infra-apply.yml) | **dispatch only** | Applies this module. Merging deploys nothing |
| [`superplane-infra-destroy.yml`](../../../../.github/workflows/superplane-infra-destroy.yml) | dispatch only | Destroys this module, behind two independent gates |
| [`superplane-k8s-deploy.yml`](../../../../.github/workflows/superplane-k8s-deploy.yml) | dispatch only | Renders and applies manifests, reading config from SSM |
| [`superplane-migrate.yml`](../../../../.github/workflows/superplane-migrate.yml) | dispatch only | Schema migrations — **currently blocked**, see below |

Apply is `workflow_dispatch`-only by design, matching `cyber-infra-apply.yml`. The plan lane
posts its output to the PR; an operator reads it and then triggers the apply. Merging a PR
never deploys.

## Domain-only deployment

The basic `deploy-all.sh` never applies this module. Use `../deploy.sh` for the
installation flow or `superplane-infra-apply.yml` for this Terraform root. The
former `--superplane-only` platform flag is rejected. The ownership test in
`control-plane/tests/test_domain_only_scope.py` checks this boundary.

## What is deliberately not here

**No database.** Decision 2 (shared instance vs. separate) is unresolved, and provisioning
either shape would decide it by default. The database arrives as a *reference* —
`var.database_secret_name`, a Secrets Manager secret an operator seeds out of band — and this
module makes **no isolation, backup, retention or restore claim** about whatever it points at.

**No Kubernetes provider.** Rollout is a separate lane. A `kubernetes` provider here would
make every plan (including the credential-free test job) require EKS API reachability, and
would put an application rollout behind the same apply as IAM role creation.

**No platform resources.** This module consumes platform outputs read-only via
`data.terraform_remote_state.platform` and owns nothing the platform owns. A separate state
key alone would not establish that, so it is asserted directly — see
`tests/test_platform_isolation.py` (an allowlist of resource types, so a new type fails
closed) and the `terraform state list` guard in the destroy lane, which checks what would
actually be deleted rather than what the source declares.

## Blocked work, and what unblocks it

| Blocked | On | Effect |
|---------|-----|--------|
| Migrations | **#5045 (U13)** | The Alembic chain declares revision `006` in three files and `007` in three more, so `alembic upgrade head` has no resolvable target. `superplane-migrate.yml` reads `schema.single_head` from the lock and stops |
| The three application images | **no build has run yet** | They are recorded as `pending_images` with **no** digest, and the rollout lane refuses any manifest referencing them. The *source* blocker is gone — U22 (#5326) transferred the three components into `../src/`, so their build lanes now build from this repository — but source availability and a verified digest are separate facts, and only a real build produces the second |
| Live verification (R3 acc. 2) | named account, spend authorization, named cleanup owner | A backend-contacting plan and a second apply producing an empty plan. Explicitly **not** satisfied by a mocked plan, so it is not claimed anywhere in this module |

The lanes ship in a blocked state rather than not shipping, because the operator who unblocks
them will be reasoning about Alembic revisions or a first image build — not about tenancy
boundaries — and the constraints encoded in the guards are not obvious from the outside.

### Application code is no longer upstream-owned

U22 (#5326) transferred `superplane-api`, `superplane-controller` and
`superplane-platform-monitor` into `../src/`, with their tests and CI. Ownership of the
*source* is ADP's; ownership of *deployment* is still this module's. The components' own
upstream `deploy/` manifests came along as inventoried, read-only evidence — they carry
upstream's account id and `:latest` tags — and reconciling what the accepted topology needs
from them into `control-plane/` and `k8s/` remains U3's work. `../src/TRANSFER-MANIFEST.md`
inventories them; nothing in this module renders or applies them.

## Environment and account

The committed app environment file
`modules/domain-apps/superplane/environments/dev/superplane.tfvars` ships
`account_id = "ACCOUNT_ID"`. Superplane's workflows substitute their authenticated
target account; direct Terraform operators prepare a private copy for their
explicitly selected account. The app installer prepares its own private inputs.
Core platform deployment does not prepare this app configuration. The placeholder
fails `var.account_id`'s 12-digit validation, so an unsubstituted plan stops instead
of creating misnamed resources.

## Tests

```bash
cd control-plane

# Terraform: 34 runs over 6 files. No AWS credentials required — every run is mock-driven.
terraform init -backend=false && terraform test

# Python: the assertions terraform test structurally cannot make (backend blocks are
# resolved by init, and absence is unassertable from a plan).
python3 -m pytest tests/ -q
```

The plan lane enforces that **100% of `tests/*.tftest.hcl` execute**, by counting files on
disk against files `terraform test` reports as run. This matters because `terraform test`
exits 0 when it finds no tests at all — "0 tests ran" and "all tests passed" are otherwise
indistinguishable.
