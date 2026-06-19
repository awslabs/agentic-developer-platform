# Learnings: Issue #1617 — CodeBuild Source Race Fix

**Date**: 2026-06-19
**Issue**: #1617
**PR**: #1623
**Agent**: @agent-developer

## Problem

All CodeBuild-based image builds staged source through a **single shared mutable S3 key** (`codebuild/adp-source.zip`). When concurrent builds ran, one build could read another's source — or a stale leftover — silently shipping wrong code.

## Key Technical Decisions

### Per-SHA-RUN_ID key vs. per-SHA key
- Used `${SHA}-${RUN_ID}` (workflows) and `${SHA}-$(date +%s)-$$` (scripts) for full per-invocation isolation
- Plain `${SHA}` would collide when re-running workflows for the same commit
- `RUN_ID` is `GITHUB_RUN_ID` in Actions context; scripts lack this so we use epoch+PID

### `--source-location-override` on `start-build`
- First-class CodeBuild API feature that overrides the project's default source per-invocation
- The project-level `source.location` is kept as a harmless fallback (old key) — this means rollback is trivial (revert workflow changes, builds fall back to default location)
- This was the minimal correct fix vs. switching to Git-source CodeBuild (larger change, auth complexity)

### Composite action vs. inline
- Extracted to `.github/actions/codebuild-run/action.yml` because 9 workflows shared identical logic
- The action handles: upload, start-build with override, poll-wait, output build_id
- Shell scripts can't use composite actions, so a parallel `platform/scripts/codebuild-run.sh` provides identical semantics

### Buildspec sentinel guard
- All buildspecs now check `ADP_SOURCE_SHA` env var at the start of pre_build
- If missing, the build was started without the override → fail fast with a clear error
- This prevents silent fallback to the stale shared key

## What Worked

1. **YAML validation with pyyaml** after each edit batch — caught nothing (all edits were clean) but gave confidence
2. **`bash -n`** for syntax-checking shell scripts before committing
3. **Reading the full architect review** before starting — the expanded scope (12 call sites, not 10) and the IAM-only-for-gbrain insight saved a lot of rework
4. **Keeping the old key as a default** — the approved design explicitly required this for rollback safety; fighting the temptation to "clean up" the old key prevented a breaking change

## Gotchas

1. **Leftover code after `Edit` operations**: When replacing a block that includes the START of a loop/conditional but not the END, leftover `esac`/`done` lines persist. Always read a window after each edit to catch these.
2. **gitignore**: `modules/research/gbrain/terraform/modules/build/` is gitignored (likely `.terraform/` pattern matching). Needed `git add -f` for the specific file.
3. **Security scan workflow lacks `load-deploy-config`**: Unlike other workflows, it resolves ACCOUNT_ID/STATE_BUCKET inline. Had to add `AWS_REGION` export since the composite action needs it explicitly.
4. **Downstream step references**: When replacing inline `start-build` + poll with a composite action, downstream steps that referenced `steps.build.outputs.image_tag` etc. need to be rewired to new step IDs. Check ALL step references in the workflow, not just the replaced block.
5. **`${{ env.X || env.Y }}`**: GitHub Actions supports this expression syntax for fallback env vars, used in arc-runner for CUSTOMER_ACCOUNT_ID handling.

## File Inventory (for future reference)

- **Composite action**: `.github/actions/codebuild-run/action.yml`
- **Bash helper**: `platform/scripts/codebuild-run.sh` (chmod +x)
- **IAM change**: Only `modules/research/gbrain/terraform/modules/build/main.tf` — the other 2 TF modules use AdministratorAccess
- **S3 lifecycle**: `platform/infra/modules/codebuild/main.tf` — uses `data "aws_s3_bucket"` since state bucket is bootstrap-managed
- **Buildspec guard**: Added to 11 buildspec files in `codebuild/`

## Deployment Steps After Merge

1. `terraform apply` in `platform/infra/modules/codebuild/` (lifecycle rule)
2. `terraform apply` in `modules/research/gbrain/terraform/modules/build/` (IAM)
3. Workflow edits take effect on next run automatically
4. Re-trigger context-mcp build to deploy stuck #1611 Neptune fix
