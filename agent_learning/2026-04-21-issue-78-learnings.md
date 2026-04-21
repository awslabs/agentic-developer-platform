# Issue #78 Learnings: Terraform CodeBuild projects + retire non-docker projects

## Date: 2026-04-21

## What Was Done
- Created `platform/infra/modules/codebuild/` Terraform module with 4 CodeBuild projects + 1 shared IAM role
- Rewrote `deploy-all.sh` to run Terraform/npm/kubectl directly instead of through CodeBuild for non-docker tasks
- Migrated `gateway-deploy.yml` frontend build from CodeBuild to ARC runner
- Created `codebuild/bs-agent-gateway.yml` (was only generated inline before — never checked in)

## Key Decisions
- **`state_bucket` derived from account ID**: Added `local.state_bucket = coalesce(var.state_bucket, "adp-terraform-state-${data.aws_caller_identity.current.account_id}")` so fresh clones don't need to pass the bucket name — it's auto-derived from the AWS account
- **`ensure_codebuild_role` kept as fallback**: Even though the role is Terraform-managed, the function still exists as a bootstrap fallback for chicken-and-egg scenarios where CodeBuild is needed before platform infra is applied
- **`run_codebuild` no longer creates/updates projects**: It just starts builds against existing Terraform-managed projects. Fails fast if project doesn't exist.
- **`--local` flag retained**: Now only affects docker build steps (use local Docker daemon vs CodeBuild). All other tasks (terraform, npm, kubectl) always run directly regardless of `--local`.

## Files Changed
- `platform/infra/modules/codebuild/main.tf` — new module: 4 projects + IAM role
- `platform/infra/modules/codebuild/variables.tf` — module inputs
- `platform/infra/modules/codebuild/outputs.tf` — role ARN, project names
- `platform/infra/main.tf` — wires codebuild module, adds `local.state_bucket`
- `platform/infra/variables.tf` — adds `state_bucket` variable
- `platform/infra/outputs.tf` — exposes codebuild role ARN and project names
- `platform/scripts/deploy-all.sh` — major rewrite (1103 -> 684 lines)
- `.github/workflows/gateway-deploy.yml` — frontend job now runs npm+s3 directly
- `codebuild/bs-agent-gateway.yml` — new checked-in buildspec (was inline only)
- `CLAUDE.md` — CodeBuild section updated

## Import Commands for Dev Account
Before first `terraform apply` on the dev account:
```bash
cd platform/infra
for name in gateway-build chat-agent agent-gateway arc-runner; do
  terraform import "module.codebuild.aws_codebuild_project.main[\"$name\"]" "adp-dev-$name"
done
terraform import module.codebuild.aws_iam_role.codebuild adp-dev-codebuild-role
```

## Retired CodeBuild Projects to Delete
```bash
for name in frontend-build platform-infra gateway-deploy gateway-infra gateway-alb-wire agent-factory-infra agent-context-infra agent-context-deploy destroy; do
  aws codebuild delete-project --name "adp-dev-$name" --region us-east-1 || true
done
```

## Gotchas
- `bs-agent-gateway.yml` was never checked in — it was only generated inline in deploy-all.sh and injected into the source zip. The Terraform module references `codebuild/bs-agent-gateway.yml`, so it must exist in the repo.
- The `deploy-all.sh` buildspec generation block was ~300 lines of inline YAML that masked which buildspecs were actually used by workflows vs. only by the script itself. Removing it makes the dependency much clearer.
- `terraform fmt` flagged the `common_tags` alignment in main.tf after adding `state_bucket` to the locals block — always run `terraform fmt` after edits.
- The `AdministratorAccess` policy on the CodeBuild role is intentionally kept (flagged with TODO) per issue scope.
