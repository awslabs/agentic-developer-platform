# Superplane infrastructure — deployment lanes

Issue #5042 (U3), EPIC #4910.

`control-plane/` is the ADP-owned Terraform wrapper that deploys the pinned Superplane
control plane and the SkyPilot API service. It owns the AWS-side surface those images need —
IAM roles, ECR repositories, SSM configuration — and nothing else. Application code and
migrations stay upstream-owned; `../releases/superplane.lock.yaml` pins what runs.

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

## Known limitation: `deploy-all.sh --superplane-only` is platform PLUS superplane

The flag excludes the gateway, webhook-ingress, agent-factory and agent-context, but it still
runs **Step 1 (bootstrap)** and **Step 2 (shared platform infra)**. It is not a domain-only
command despite how the name reads.

This is recorded as a known limitation in the platform-isolation requirement (2026-09-16),
and it is pinned by tests rather than only described:
`control-plane/tests/test_domain_only_scope.py` calls the real `resolve_deploy_scope()` and
asserts that all four other module flags resolve to false, that each module-deploying step in
`deploy-all.sh` is gated by its flag, and that the platform phases are *not* scope-guarded —
so if someone changes that, a test fails and names the docs to update.

**For routine domain work, use `superplane-infra-apply.yml`.** It applies
`control-plane/` and nothing else, which is the genuinely domain-scoped path.

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
| Migrations | **#5045 (U13)** | The pinned upstream Alembic chain declares revision `006` in four files and `007` in three, so `alembic upgrade head` has no resolvable target. `superplane-migrate.yml` reads `schema.single_head` from the lock and stops |
| The three application images | `source_access` in the lock | Building `superplane-api`, `-controller` and `-platform-monitor` needs read access to `aws-innovate/AISuperPlane` that ADP does not have. They are recorded as `pending_images` with **no** digest, and the rollout lane refuses any manifest referencing them |
| Live verification (R3 acc. 2) | named account, spend authorization, named cleanup owner | A backend-contacting plan and a second apply producing an empty plan. Explicitly **not** satisfied by a mocked plan, so it is not claimed anywhere in this module |

The lanes ship in a blocked state rather than not shipping, because the operator who unblocks
them will be reasoning about Alembic revisions or registry access — not about tenancy
boundaries — and the constraints encoded in the guards are not obvious from the outside.

## Environment and account

Unresolved by design. No AWS account ID and no `adp-cred` label are supplied, and neither may
be invented. `environments/dev/modules/superplane.tfvars` ships `account_id = "ACCOUNT_ID"`, a
placeholder that `bootstrap.sh` and `deploy.sh` rewrite with the account the operator is
authenticated to. The placeholder deliberately fails `var.account_id`'s 12-digit validation,
so an unsubstituted deploy stops at validation instead of creating misnamed resources.

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
